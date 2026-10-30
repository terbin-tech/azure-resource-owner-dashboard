"""Shared test builders."""

from __future__ import annotations

from datetime import UTC, datetime

from dashboard.model import Resource

SUB = "00000000-0000-0000-0000-000000000001"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


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
