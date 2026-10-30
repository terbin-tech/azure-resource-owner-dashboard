"""The resource model: what a scan finds, as plain frozen dataclasses."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Resource types are stored casefolded.
VM = "microsoft.compute/virtualmachines"
DISK = "microsoft.compute/disks"
NIC = "microsoft.network/networkinterfaces"
PUBLIC_IP = "microsoft.network/publicipaddresses"
LOAD_BALANCER = "microsoft.network/loadbalancers"
NAT_GATEWAY = "microsoft.network/natgateways"
AKS = "microsoft.containerservice/managedclusters"
NETWORK_WATCHER = "microsoft.network/networkwatchers"
SMART_DETECTOR = "microsoft.alertsmanagement/smartdetectoralertrules"
ACTION_GROUP = "microsoft.insights/actiongroups"

_ID_RE = re.compile(r"/subscriptions/([^/]+)/resourceGroups/([^/]+)", re.IGNORECASE)


@dataclass(frozen=True)
class Subscription:
    id: str  # bare and casefolded
    name: str


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    subscription: str
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.id.casefold()


@dataclass(frozen=True)
class Resource:
    id: str
    type: str
    name: str
    subscription: str
    group: str
    created_at: datetime | None = None
    tags: dict[str, str] = field(default_factory=dict)
    managed_by: str = ""
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.id.casefold()

    @property
    def group_key(self) -> str:
        return f"/subscriptions/{self.subscription}/resourcegroups/{self.group}".casefold()

    @property
    def power_state(self) -> str | None:
        node: Any = self.properties
        for step in ("extended", "instanceView", "powerState", "code"):
            if not isinstance(node, dict):
                return None
            node = node.get(step)
        return node


@dataclass(frozen=True)
class Incomplete:
    what: str
    error: str


def tag(tags: dict[str, str], name: str) -> str | None:
    """The value of tag `name` in any case, or None when it is absent.

    "Tag names are case-insensitive for operations." and "Tag values are case-sensitive.",
    https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/tag-resources
    """
    wanted = name.casefold()
    return next((value for key, value in tags.items() if key.casefold() == wanted), None)


def parse_id(resource_id: str) -> tuple[str, str]:
    """The subscription ID (casefolded) and the resource group name (as written)."""
    match = _ID_RE.match(resource_id)
    if not match:
        raise ValueError(f"not a resource ID with a subscription and group: {resource_id!r}")
    return match.group(1).casefold(), match.group(2)
