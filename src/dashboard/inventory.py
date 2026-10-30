"""Read-only inventory: the subscriptions, the ARM resource list and Resource Graph.

The ARM list is the source of createdTime, which the age mark needs; Resource Graph is
the source of properties (the VM power state) and of the resource groups with their tags. Each
resource is the union of the two, keyed by its casefolded ID.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from azure.core.exceptions import AzureError
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions

from .clients import Clients, error_code
from .model import Group, Incomplete, Resource, Subscription, parse_id

if TYPE_CHECKING:
    from azure.mgmt.resource.resources.models import GenericResourceExpanded
    from azure.mgmt.resourcegraph import ResourceGraphClient

log = logging.getLogger(__name__)

# Query rules. One query for all subscriptions, because Microsoft marks a per-subscription
# loop "NOT RECOMMENDED"; a string `id` column, no `limit`, and `order by`, so that the skip
# token pages through a stable order. Source: Microsoft Learn, "Work with large Azure resource
# data sets", https://learn.microsoft.com/en-us/azure/governance/resource-graph/concepts/work-with-data
# and "Guidance for throttled requests in Azure Resource Graph",
# https://learn.microsoft.com/en-us/azure/governance/resource-graph/concepts/guidance-for-throttled-requests
RESOURCES_QUERY = (
    "resources | project id, name, type, managedBy, tags, properties | order by id asc"
)
GROUPS_QUERY = (
    "resourcecontainers | where type =~ 'microsoft.resources/subscriptions/resourcegroups'"
    " | project id, name, tags | order by id asc"
)
# What a malformed row raises while it is converted (a null field, a missing key, an ID without a
# resource group). Caught with AzureError so that one bad row degrades to "incomplete" (fail
# closed). A refused write is a RuntimeError, so it is not caught and still stops the run.
MALFORMED = (AzureError, ValueError, KeyError, AttributeError, TypeError)
PAGE_SIZE = 1000  # the most a request may ask for (QueryRequestOptions.top, maximum 1000)


@dataclass
class Inventory:
    scanned: list[Subscription]
    not_scanned: list[str]  # display names of subscriptions the login cannot read
    resources: list[Resource]
    groups: dict[str, Group]  # by Group.key
    incomplete: list[Incomplete]
    graph_failed: bool = False
    arm_failed: set[str] = field(default_factory=set)  # scanned subscriptions whose ARM list failed


def query_all(
    graph: ResourceGraphClient, subscription_ids: list[str], query: str
) -> list[dict[str, Any]]:
    """Every row of query over the subscriptions, following the skip token."""
    rows: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        response = graph.resources(
            QueryRequest(
                subscriptions=subscription_ids,
                query=query,
                options=QueryRequestOptions(
                    top=PAGE_SIZE, skip_token=token, result_format="objectArray"
                ),
            )
        )
        rows += response.data
        token = response.skip_token
        if not token:
            # Fewer rows than totalRecords after the last page: rows are missing (https://learn.microsoft.com/en-us/azure/governance/resource-graph/concepts/work-with-data).
            if len(rows) < response.total_records:
                raise ValueError("Resource Graph returned fewer rows than totalRecords")
            return rows


def _from_graph(row: dict[str, Any]) -> Resource:
    # The subscription and group come from the ID, as for an ARM row: a null ID, or one without
    # a group, raises here, inside collect's try, instead of crashing a later step.
    subscription, group = parse_id(row["id"])
    return Resource(
        id=row["id"],
        type=row["type"].casefold(),
        name=row["name"],
        subscription=subscription,
        group=group,
        tags=row.get("tags") or {},
        managed_by=row.get("managedBy") or "",
        properties=row.get("properties") or {},
    )


def _group(row: dict[str, Any]) -> Group:
    subscription, _ = parse_id(row["id"])  # validates the ID, as _from_graph does
    return Group(
        id=row["id"],
        name=row["name"],
        subscription=subscription,
        tags=row.get("tags") or {},
    )


def _as_utc(moment: object) -> datetime | None:
    # The SDK leaves a createdTime it cannot parse as the raw string: it counts as missing, so
    # one odd value never drops the subscription's list. A createdTime with no offset is read
    # as naive; it is UTC, as in events.py.
    if not isinstance(moment, datetime):
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _from_arm(row: GenericResourceExpanded) -> Resource:
    subscription, group = parse_id(row.id)
    return Resource(
        id=row.id,
        type=row.type.casefold(),
        name=row.name,
        subscription=subscription,
        group=group,
        created_at=_as_utc(row.created_time),
        tags=row.tags or {},
        managed_by=row.managed_by or "",
    )


def collect(clients: Clients) -> Inventory:
    try:
        sdk_subs = list(clients.subscriptions().subscriptions.list())
    except AzureError as exc:
        code = error_code(exc)
        log.warning("subscription list failed: %s", code)
        return Inventory([], [], [], {}, [Incomplete("subscription list", code)])

    scanned: list[Subscription] = []
    not_scanned: list[str] = []
    incomplete: list[Incomplete] = []
    arm_failed: set[str] = set()
    merged: dict[str, Resource] = {}

    for sdk_sub in sdk_subs:
        sub = Subscription(sdk_sub.subscription_id.casefold(), sdk_sub.display_name)
        try:
            # The list is lazy: an error surfaces while iterating. Rows are converted here too, and
            # a failure anywhere drops the subscription's whole ARM list (a partial list is not kept).
            rows = [
                _from_arm(row)
                for row in clients.resources(sub.id).resources.list(expand="createdTime")
            ]
        except MALFORMED as exc:
            code = error_code(exc)
            if code == "AuthorizationFailed":
                not_scanned.append(sub.name)
                continue
            what = f"resource list in {sub.name}"
            log.warning("%s failed: %s", what, code)
            incomplete.append(Incomplete(what, code))
            arm_failed.add(sub.id)
            scanned.append(sub)
            continue
        scanned.append(sub)
        merged |= {row.key: row for row in rows}

    groups: dict[str, Group] = {}
    graph_failed = False
    if scanned:
        ids = [s.id for s in scanned]
        graph = clients.graph()
        try:
            # Everything a bad row can raise, the key included, happens inside the try.
            found = {r.key: r for r in map(_from_graph, query_all(graph, ids, RESOURCES_QUERY))}
        except MALFORMED as exc:
            graph_failed = True
            code = error_code(exc)
            log.warning("Resource Graph failed: %s", code)
            incomplete.append(Incomplete("Resource Graph", code))
        else:
            for key, resource in found.items():
                prior = merged.get(key)
                merged[key] = replace(resource, created_at=prior.created_at) if prior else resource
        try:
            found_groups = {g.key: g for g in map(_group, query_all(graph, ids, GROUPS_QUERY))}
        except MALFORMED as exc:
            graph_failed = True
            code = error_code(exc)
            log.warning("Resource Graph resource groups failed: %s", code)
            incomplete.append(Incomplete("Resource Graph resource groups", code))
        else:
            groups |= found_groups

    return Inventory(
        scanned, not_scanned, list(merged.values()), groups, incomplete, graph_failed, arm_failed
    )
