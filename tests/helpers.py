"""Shared test builders. Never import tests/fakes.py here: fakes imports this module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dashboard.events import Write
from dashboard.model import Group, Resource

SUB = "00000000-0000-0000-0000-000000000001"
SUB2 = "00000000-0000-0000-0000-000000000002"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=1)


def rid(group: str, type_: str, name: str, sub: str = SUB) -> str:
    return f"/subscriptions/{sub}/resourceGroups/{group}/providers/{type_}/{name}"


def res(
    type_: str,
    name: str,
    group: str = "rg-a",
    *,
    sub: str = SUB,
    created_at: datetime | None = T0,
    tags=None,
    managed_by="",
    properties=None,
) -> Resource:
    return Resource(
        id=rid(group, type_, name, sub),
        type=type_.casefold(),
        name=name,
        subscription=sub,
        group=group,
        created_at=created_at,
        tags=tags or {},
        managed_by=managed_by,
        properties=properties or {},
    )


def grp(name: str, sub: str = SUB, tags=None) -> Group:
    return Group(
        id=f"/subscriptions/{sub}/resourceGroups/{name}",
        name=name,
        subscription=sub,
        tags=tags or {},
    )


def write(
    target: Resource | str,
    caller: str = "student-a@school.example",
    *,
    minutes: float = 0,
    created: bool = True,
    base: datetime = T0,
) -> Write:
    """A succeeded write on `target` (a resource, or an ID that is casefolded) at base + minutes."""
    resource = target.key if isinstance(target, Resource) else target.casefold()
    return Write(resource, caller, created, base + timedelta(minutes=minutes))
