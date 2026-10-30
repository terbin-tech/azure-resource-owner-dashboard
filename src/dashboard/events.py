"""Who created what: the creation events of the Activity Log.

The Activity Log keeps 90 days and "is the only place that stores the creator of a resource"
(https://learn.microsoft.com/en-us/azure/azure-monitor/platform/activity-log). Its `$filter` takes
a time range plus one of a few scopes, and both ends of the range must fall inside the 90 days;
operation, status and caller are filtered here. Stateless: nothing is stored.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from azure.core.exceptions import AzureError

from .clients import error_code
from .model import Resource, Subscription

if TYPE_CHECKING:
    from azure.mgmt.monitor import MonitorManagementClient
    from azure.mgmt.monitor.models import EventData

log = logging.getLogger(__name__)

WINDOW = timedelta(minutes=15)  # reused names: accept only events this close to creation


@dataclass(frozen=True)
class Write:
    resource: str  # the casefolded ARM ID
    caller: str
    created: bool  # the write answered "Created" (HTTP 201)
    time: datetime


@dataclass
class LookupResult:
    subscription: Subscription
    group: str  # the resource group the lookup read, as written; every lookup names its group
    writes: list[Write] = field(default_factory=list)
    duration_s: float = 0.0
    error: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.error)


def window_start(now: datetime, lookback_days: int) -> datetime:
    """The start of the lookup: an hour inside the lookback, so both ends fall inside 90 days."""
    return now - timedelta(days=lookback_days) + timedelta(hours=1)


def activity_filter(start: datetime, end: datetime, group: str) -> str:
    """The documented `$filter`: a time range plus one resource group.

    https://learn.microsoft.com/en-us/rest/api/monitor/activity-logs/list documents a time range
    plus one of resourceGroupName, resourceUri, resourceProvider or correlationId.
    """

    def stamp(moment: datetime) -> str:
        return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    found = f"eventTimestamp ge '{stamp(start)}' and eventTimestamp le '{stamp(end)}'"
    if "'" in group:  # a group name cannot hold one (config.GROUP_NAME_RE); never break the filter
        raise ValueError("a resource group name cannot contain a quote")
    return f"{found} and resourceGroupName eq '{group}'"


def _as_write(event: EventData) -> Write | None:
    """The succeeded write an event records, or None. Every EventData field is optional."""
    resource_id = event.resource_id
    when = event.event_timestamp
    if not resource_id or when is None:
        return None
    operation = event.operation_name.value if event.operation_name else None
    status = event.status.value if event.status else None
    if not operation or not status:
        return None
    if not operation.casefold().endswith("/write") or status.casefold() != "succeeded":
        return None
    sub_status = (event.sub_status.value if event.sub_status else None) or ""
    status_code = str((event.properties or {}).get("statusCode") or "")
    created = "created" in (sub_status.casefold(), status_code.casefold())
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return Write(resource_id.casefold(), event.caller or "", created, when)


def fetch_writes(
    monitor: MonitorManagementClient,
    sub: Subscription,
    start: datetime,
    end: datetime,
    group: str,
    clock: Callable[[], float] = time.monotonic,
) -> LookupResult:
    """Read the succeeded writes between `start` and `end` in one resource group of `sub`.

    An AzureError ends the lookup but keeps the writes read so far, so the owners that remain
    unknown show `lookup-failed`. It is logged with the group and the code only, never the
    message, as inventory failures are. A refused write is not an AzureError and propagates.
    """
    result = LookupResult(sub, group)
    began = clock()
    try:
        for event in monitor.activity_logs.list(filter=activity_filter(start, end, group)):
            if (found := _as_write(event)) is not None:
                result.writes.append(found)
    except AzureError as exc:
        result.error = error_code(exc)
        log.warning("Activity Log lookup failed for group %s: %s", group, result.error)
    result.duration_s = clock() - began
    return result


def by_resource(results: list[LookupResult]) -> dict[str, list[Write]]:
    merged: dict[str, list[Write]] = {}
    for result in results:
        for found in result.writes:
            merged.setdefault(found.resource, []).append(found)
    return merged


def is_person(caller: str) -> bool:
    return "@" in caller


def username(caller: str) -> str:
    return caller.split("@", 1)[0]


def creation_of(resource: Resource, writes: list[Write]) -> Write | None:
    """The write that created `resource`; `writes` are the ones on `resource.key`.

    With a created time, the `Created` writes within WINDOW of it, nearest first; else the
    earliest write within WINDOW (a long-running create answers 202); else None, as when an old
    resource was only edited. Without a created time, the latest `Created` write, because the
    earliest could name the creator of a deleted resource with the same name.
    """
    created_at = resource.created_at
    if created_at is None:
        return max((w for w in writes if w.created), key=lambda w: w.time, default=None)
    near = [w for w in writes if abs(w.time - created_at) <= WINDOW]
    made = [w for w in near if w.created]
    if made:
        return min(made, key=lambda w: abs(w.time - created_at))
    return min(near, key=lambda w: w.time, default=None)


def group_creation(writes: list[Write]) -> Write | None:
    """A group has no created time: the latest `Created` write is the current group's."""
    return max((w for w in writes if w.created), key=lambda w: w.time, default=None)
