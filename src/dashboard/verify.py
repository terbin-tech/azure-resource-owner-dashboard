"""Write the live verification report.

It goes to out/verify.md and contains usernames and resource IDs, so it is never
committed.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime

from .attribution import METHODS
from .events import LookupResult, by_resource, creation_of, is_person, username
from .model import tag
from .people import Roster
from .pipeline import Report, Row, ScanData

MIN_HINT_LENGTH = 4


def name_hint(row: Row, people: Iterable[str]) -> str | None:
    text = f"{row.resource.name} {tag(row.resource.tags, 'Name') or ''}".lower()
    for person in sorted(people, key=len, reverse=True):
        p = person.lower()
        if len(p) >= MIN_HINT_LENGTH and re.search(
            rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", text
        ):
            return person
    return None


def _domains(data: ScanData) -> dict[str, set[str]]:
    """The email domains each sign-in prefix wrote from, over every person caller."""
    found: dict[str, set[str]] = {}
    for lookup in data.lookups:
        for w in lookup.writes:
            if is_person(w.caller):
                domain = w.caller.split("@", 1)[1].casefold()
                found.setdefault(username(w.caller).casefold(), set()).add(domain)
    return found


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat()


def render_verify(data: ScanData, report: Report, roster: Roster | None) -> str:
    sub_names = {s.id: s.name for s in data.inventory.scanned}
    out = [
        "# Verification report",
        "",
        f"Generated {data.generated_at.isoformat()}. Local only: never commit this file.",
        "",
        "## 1. Activity Log lookup duration",
        "",
        "| subscription | duration s | writes | failed |",
        "|---|---|---|---|",
    ]
    by_sub: dict[str, list[LookupResult]] = {}  # one row per subscription, over its groups
    for lk in data.lookups:
        by_sub.setdefault(lk.subscription.id, []).append(lk)
    for lookups in by_sub.values():
        duration = sum(lk.duration_s for lk in lookups)
        writes_read = sum(len(lk.writes) for lk in lookups)
        failed = sum(1 for lk in lookups if lk.failed)
        state = f"{failed} of {len(lookups)} groups" if failed else "no"
        out.append(f"| {lookups[0].subscription.name} | {duration:.1f} | {writes_read} | {state} |")
    slowest = max((lk.duration_s for lk in data.lookups), default=None)
    out += ["", f"- slowest group: {'none' if slowest is None else f'{slowest:.1f} s'}"]

    # event owners whose creation is not a Created write came from the earliest-write fallback
    events = [
        row.owner for row in report.rows if row.owner.method == "event" and row.owner.creation
    ]
    writes = by_resource(data.lookups)
    outside = []  # writes on the ID, but none is the creation
    for row in report.rows:
        found = writes.get(row.resource.key)
        if (
            row.resource.created_at is not None
            and found
            and creation_of(row.resource, found) is None
        ):
            first = min(w.time for w in found)
            gap = round((first - row.resource.created_at).total_seconds() / 60)
            sub = sub_names.get(row.resource.subscription, row.resource.subscription)
            outside.append(
                f"  - {row.resource.id} | {sub} | created {_utc(row.resource.created_at)}"
                f" | first write {_utc(first)} | {gap} min"
            )
    out += [
        "",
        "## 2. Creation events",
        "",
        f"- owners from a Created write: {sum(1 for o in events if o.creation.created)}",
        f"- owners from the earliest-write fallback: {sum(1 for o in events if not o.creation.created)}",
        "- resources with writes on their ID but no creation within 15 minutes of createdTime: "
        f"{len(outside)}",
        *sorted(outside),
        "",
        "## 3. Owner method per type",
        "",
        "| type | " + " | ".join(METHODS) + " |",
        "|---" * (len(METHODS) + 1) + "|",
    ]
    by_type: dict[str, Counter] = {}
    for row in report.rows:
        by_type.setdefault(row.resource.type, Counter())[row.owner.method] += 1
    for type_, counts in sorted(by_type.items()):
        out.append(f"| {type_} | " + " | ".join(str(counts[m]) for m in METHODS) + " |")

    # The people are every owner name found plus every roster username: Azure has no
    # user list without Graph.
    people = {name.casefold(): name for name in (roster or {})}
    for row in report.rows:
        if row.owner.name:
            people.setdefault(row.owner.name.casefold(), row.owner.name)
    agreements, disagreements = 0, []
    for row in report.rows:
        hint = name_hint(row, people.values())
        if hint is None:
            continue
        creator = row.owner.name or "nobody"
        if creator.casefold() == hint.casefold():
            agreements += 1
        else:
            sub = sub_names.get(row.resource.subscription, row.resource.subscription)
            disagreements.append(
                f"| {row.resource.id} | {sub} | {hint} | {creator} | {row.owner.method} |"
            )
    out += [
        "",
        "## 4. Name hint vs owner",
        "",
        f'Agreements: {agreements}. Every disagreement below needs an explanation before "done".',
        "",
        "| resource | subscription | name hint | owner | method |",
        "|---|---|---|---|---|",
        *disagreements,
    ]

    several = sorted((p, d) for p, d in _domains(data).items() if len(d) > 1)
    out += [
        "",
        "## 5. Sign-in names on more than one domain",
        "",
        *([f"- {p}: {len(d)} domains" for p, d in several] or ["- none"]),
        "",
        "## 6. Incomplete",
        "",
        *([f"- {m}" for m in report.incomplete] or ["- none"]),
        "",
        "## 7. Mapping",
        "",
        f"- users with resources but no cohort: {report.unmapped_users}",
        f"- roster: {'loaded' if report.roster_loaded else 'not loaded (absent or denied)'}",
        "",
        "## 8. Spot check (manual)",
        "",
        "Confirm 3 to 5 owners you know personally (oldest first):",
        "",
    ]
    oldest = sorted(
        report.rows, key=lambda r: (r.age_days is None, -(r.age_days or 0), r.resource.id)
    )
    for row in oldest[:5]:
        owner = row.owner.name or row.owner.method
        sub = sub_names.get(row.resource.subscription, row.resource.subscription)
        out.append(f"- [ ] {row.resource.type} {row.resource.id} ({sub}) → {owner}")
    return "\n".join(out) + "\n"
