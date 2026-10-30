"""Who owns each resource: the rules in order, first match wins.

The dashboard's own group and what Azure made come first, then the numbered rules: 1 a person's
creation event, 2 a parent link, 3 the resource group's creator, 4 an AKS node group whose
cluster is gone, 5 a failed lookup, 6 nobody.

Creation events come from the Activity Log (`events`), parent links from the inventory. Stateless:
nothing here is stored.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any

from .events import Write, creation_of, group_creation, is_person, username
from .model import (
    ACTION_GROUP,
    AKS,
    DISK,
    LOAD_BALANCER,
    NAT_GATEWAY,
    NETWORK_WATCHER,
    NIC,
    PUBLIC_IP,
    SMART_DETECTOR,
    Group,
    Resource,
    tag,
)

METHODS = (
    "event",
    "parent",
    "resource-group",
    "orphaned",
    "lookup-failed",
    "unknown",
    "azure",
    "dashboard",
)

_APP_MADE_AZURE = (SMART_DETECTOR, ACTION_GROUP)

# AKS puts this tag on a cluster's node resource group, with the cluster's name as the value.
# Sources: https://learn.microsoft.com/en-us/troubleshoot/azure/azure-kubernetes/create-upgrade-delete/cannot-scale-cluster-autoscaler-enabled-node-pool
# ("Reconcile node resource group tags", which spells it AKS-Managed-Cluster-Name) and the AKS
# release notes of 2021-10-15, https://github.com/Azure/AKS/releases/tag/2021-10-15 ("Add
# aks-managed-cluster-rg and aks-managed-cluster-name tags to the node resource group"). The two
# spell the key in different case; model.tag matches tag names in any case, as Azure does.
NODE_GROUP_TAG = "AKS-Managed-Cluster-Name"


@dataclass(frozen=True)
class Owner:
    name: str | None  # the sign-in prefix as written; None when nobody is named
    method: str  # one of METHODS
    detail: str = ""
    creation: Write | None = None
    guess: str = ""


def _get(node: Any, *path: str) -> Any:
    for step in path:
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node


def tagged_node_groups(groups: dict[str, Group]) -> set[str]:
    """The keys of the groups AKS tagged as a cluster's node resource group (rule 4)."""
    return {key for key, g in groups.items() if tag(g.tags, NODE_GROUP_TAG) is not None}


class Links:
    """The parent links of rule 2, between the resources of one scan."""

    def __init__(self, resources: list[Resource]) -> None:
        self.index = {r.key: r for r in resources}
        self.node_groups: dict[str, Resource] = {}
        for r in resources:
            node_group = _get(r.properties, "nodeResourceGroup")
            if r.type == AKS and isinstance(node_group, str) and node_group:
                key = f"/subscriptions/{r.subscription}/resourcegroups/{node_group}".casefold()
                self.node_groups[key] = r

    def _target(self, resource_id: Any) -> Resource | None:
        return self.index.get(resource_id.casefold()) if isinstance(resource_id, str) else None

    def _typed_parent(self, r: Resource) -> Resource | None:
        if r.type == DISK:
            return self._target(r.managed_by)
        if r.type == NIC:
            return self._target(_get(r.properties, "virtualMachine", "id"))
        if r.type == PUBLIC_IP:
            config = _get(r.properties, "ipConfiguration", "id")
            if isinstance(config, str):
                # .../networkInterfaces/<nic>/ipConfigurations/<name>, or the same under a
                # load balancer's frontendIPConfigurations: drop the last two segments.
                target = self._target(config.rsplit("/", 2)[0])
                if target is not None and target.type in (NIC, LOAD_BALANCER):
                    return target
            nat = self._target(_get(r.properties, "natGateway", "id"))
            return nat if nat is not None and nat.type == NAT_GATEWAY else None
        return None

    def parents(self, r: Resource) -> list[Resource]:
        """The existing parents of `r` in the order to try: its typed link, then its cluster.

        A link to a resource outside the scan counts for nothing (a disk's `managedBy` can name
        a scale-set instance, which the scan does not list). Anything in an AKS node resource
        group also has its cluster, for when the typed parent is missing or has no owner.
        """
        found = (self._typed_parent(r), self.node_groups.get(r.group_key))
        return [p for p in found if p is not None]

    def parent(self, r: Resource) -> Resource | None:
        """The first of `parents(r)`, or None."""
        return next(iter(self.parents(r)), None)


def attribute(
    resources: list[Resource],
    writes: dict[str, list[Write]],
    failed_groups: AbstractSet[str],
    *,
    dashboard_group: str,
    lookback_days: int,
    tagged_groups: AbstractSet[str] = frozenset(),
    graph_failed: bool = False,
) -> dict[str, Owner]:
    """The owner of each resource by key.

    `failed_groups` are the group keys whose lookup failed; `tagged_groups` are the keys of the
    groups AKS tagged as node groups; `graph_failed` means the cluster list is missing.
    """
    return _Attributor(
        resources,
        writes,
        failed_groups,
        dashboard_group,
        lookback_days,
        tagged_groups,
        graph_failed,
    ).run()


class _Attributor:
    def __init__(
        self,
        resources: list[Resource],
        writes: dict[str, list[Write]],
        failed: AbstractSet[str],
        dashboard_group: str,
        lookback_days: int,
        tagged: AbstractSet[str],
        graph_failed: bool,
    ) -> None:
        self.resources = resources
        self.writes = writes
        self.failed = failed
        self.dashboard_group = dashboard_group.casefold()
        self.lookback_days = lookback_days
        self.tagged = tagged
        self.graph_failed = graph_failed
        self.links = Links(resources)
        self.owners: dict[str, Owner] = {}

    def run(self) -> dict[str, Owner]:
        for r in self.resources:
            self.resolve(r, frozenset())
        return self.owners

    def resolve(self, r: Resource, stack: frozenset[str]) -> Owner:
        if r.key in self.owners:
            return self.owners[r.key]
        if r.key in stack:
            return Owner(None, "unknown", "attribution cycle")
        stack = stack | {r.key}
        found = creation_of(r, self.writes.get(r.key, []))
        by_person = found is not None and is_person(found.caller)
        if r.group.casefold() == self.dashboard_group:
            owner = Owner(None, "dashboard")
        elif r.type == NETWORK_WATCHER or (
            r.type in _APP_MADE_AZURE and found is not None and not by_person
        ):
            owner = Owner(None, "azure")
        elif found is not None and by_person:
            owner = Owner(username(found.caller), "event", "", found)
        else:
            owner = (
                self.via_parent(r, stack)
                or self.via_group(r)
                or self.cluster_gone(r)
                or self.fallback(r, found)
            )
        self.owners[r.key] = owner
        return owner

    def via_parent(self, r: Resource, stack: frozenset[str]) -> Owner | None:
        for parent in self.links.parents(r):
            owner = self.resolve(parent, stack)
            if owner.name is not None:
                return Owner(owner.name, "parent", f"via {parent.name}", owner.creation)
        return None

    def via_group(self, r: Resource) -> Owner | None:
        if r.group_key in self.failed:  # partial data: show lookup-failed, name nobody
            return None
        made = group_creation(self.writes.get(r.group_key, []))
        if made is None or not is_person(made.caller):
            return None
        return Owner(username(made.caller), "resource-group", "via resource group", made)

    def cluster_gone(self, r: Resource) -> Owner | None:
        """Rule 4: AKS tagged the group as a node group, but no current cluster names it."""
        if r.group_key not in self.tagged or r.group_key in self.links.node_groups:
            return None  # not a node group, or a live one (rule 2 had its say)
        if self.graph_failed:  # the cluster list is missing: cannot say the cluster is gone
            return Owner(None, "unknown", "cluster list unavailable (Resource Graph failed)")
        return Owner(None, "orphaned", "its Kubernetes cluster no longer exists")

    def fallback(self, r: Resource, found: Write | None) -> Owner:
        if r.group_key in self.failed:
            return Owner(None, "lookup-failed", "unknown (lookup failed)")
        if found is not None:
            return Owner(None, "unknown", "created by an app", found)
        return Owner(
            None,
            "unknown",
            f"no creation event in the last {self.lookback_days} days",
            guess=tag(r.tags, "Name") or "",  # a real Name tag only: the name has its own column
        )
