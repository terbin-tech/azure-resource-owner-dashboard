"""Wire inventory, events, attribution and people together for the CLI and the Function.

`scan` is the only step that talks to Azure; `build_report` is pure. Nothing is saved
between runs: creator data lives only in memory.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from azure.core.exceptions import ClientAuthenticationError

from .attribution import Links, Owner, attribute, tagged_node_groups
from .clients import ARM_SCOPE, Clients
from .events import LookupResult, by_resource, fetch_writes, window_start
from .inventory import Inventory, collect
from .model import DISK, NIC, PUBLIC_IP, VM, Group, Resource, Subscription
from .people import Person, Roster, map_people

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential

    from .config import Config

QUIET = ("azure", "dashboard")  # never on a card, and not counted in a group's owners


@dataclass
class ScanData:
    generated_at: datetime
    inventory: Inventory
    lookups: list[LookupResult]
    lookback_days: int
    dashboard_group: str


def check_credentials(credential: TokenCredential) -> str | None:
    """Return an error code if the sign-in is missing or expired, else None.

    It runs before the scan, so a dead session fails in a second, not after minutes of
    empty results. The code is the exception's class name only: its message can name the
    signed-in account. CredentialUnavailableError is a ClientAuthenticationError.
    """
    try:
        credential.get_token(ARM_SCOPE)
    except ClientAuthenticationError as exc:
        return type(exc).__name__
    return None


def _groups_of(inventory: Inventory, sub: Subscription) -> list[str]:
    """The groups holding `sub`'s resources, one spelling each, sorted case-insensitively.

    Empty groups appear nowhere on the page, so they are not looked up.
    """
    names: dict[str, str] = {}
    for r in inventory.resources:
        if r.subscription == sub.id:
            names.setdefault(r.group.casefold(), r.group)
    return [names[key] for key in sorted(names)]


def scan(clients: Clients, cfg: Config, now: datetime) -> ScanData:
    """Read the inventory, then the Activity Log one resource group at a time, in turn.

    One query per group, not per subscription: the live subscription-wide query took over a
    quarter of the Function's 10-minute timeout.
    """
    inventory = collect(clients)
    start = window_start(now, cfg.lookback_days)
    lookups = [
        fetch_writes(clients.monitor(sub.id), sub, start, now, group)
        for sub in inventory.scanned
        for group in _groups_of(inventory, sub)
    ]
    return ScanData(
        generated_at=now,
        inventory=inventory,
        lookups=lookups,
        lookback_days=cfg.lookback_days,
        dashboard_group=cfg.dashboard_resource_group,
    )


@dataclass(frozen=True)
class Row:
    resource: Resource
    owner: Owner
    person: Person | None
    age_days: int | None
    attached: bool = False  # a part of an existing VM: never marked
    public_ip: bool = False  # a VM with a public IP on one of its NICs

    @property
    def card(self) -> str | None:
        if self.person:
            return self.person.card
        return self.owner.name or None

    @property
    def cohort(self) -> str | None:
        return self.person.cohort if self.person else None


@dataclass
class Card:
    name: str
    cohort: str | None
    rows: list[Row]


@dataclass
class Report:
    generated_at: datetime
    scanned: list[str]
    not_scanned: list[str]
    rows: list[Row]
    cards: list[Card]
    unknown: list[Row]
    orphaned: list[Row]
    made_by_azure: list[Row]
    dashboard: list[Row]
    incomplete: list[str]
    unmapped_users: int  # for the Mapping section of verify.md
    roster_loaded: bool  # for the Mapping section of verify.md
    cohorts: list[str]
    groups: dict[str, Group]
    group_owners: dict[str, frozenset[str | None]]  # by group key; None for a row without a card
    node_groups: frozenset[str]  # only a live cluster's: the help line says delete the cluster
    incomplete_subscriptions: frozenset[str]  # whose inventory the scan may not have seen whole


def group_owners(rows: Iterable[Row]) -> dict[str, frozenset[str | None]]:
    """The cards that own something in each group, for the help line.

    A row without a card ("No owner found", "Cluster gone", a failed lookup) counts as
    someone else's (None); Made by Azure and dashboard rows go with the group and count for
    nobody.
    """
    found: dict[str, set[str | None]] = defaultdict(set)
    for row in rows:
        if row.owner.method not in QUIET:
            found[row.resource.group_key].add(row.card)
    return {key: frozenset(cards) for key, cards in found.items()}


def _vm_of(r: Resource, links: Links) -> Resource | None:
    """The VM in the scan that a NIC, or a public IP on such a NIC, belongs to.

    `links.parent` falls back to the AKS cluster, which is neither a NIC nor a VM, so
    node-group contents get no VM here and keep their own age mark.
    """
    if r.type == PUBLIC_IP:
        nic = links.parent(r)
        return _vm_of(nic, links) if nic is not None and nic.type == NIC else None
    if r.type != NIC:
        return None
    vm = links.parent(r)
    return vm if vm is not None and vm.type == VM else None


def _attached(r: Resource, links: Links) -> bool:
    # A disk with managedBy set counts even when its VM is outside the scan (a scale set).
    if r.type == DISK:
        return bool(r.managed_by)
    return _vm_of(r, links) is not None


def _age(now: datetime, r: Resource, owner: Owner) -> int | None:
    created = r.created_at
    if created is None and owner.method == "event" and owner.creation:
        created = owner.creation.time
    return (now - created).days if created else None


def build_report(data: ScanData, roster: Roster | None) -> Report:
    inventory = data.inventory
    resources = inventory.resources
    # In the casefolded form Resource.group_key has; Subscription.id is already casefolded.
    failed_groups = {
        f"/subscriptions/{lk.subscription.id}/resourcegroups/{lk.group}".casefold()
        for lk in data.lookups
        if lk.failed
    }
    tagged_groups = tagged_node_groups(inventory.groups)
    owners = attribute(
        resources,
        by_resource(data.lookups),
        failed_groups,
        dashboard_group=data.dashboard_group,
        lookback_days=data.lookback_days,
        tagged_groups=tagged_groups,
        graph_failed=inventory.graph_failed,
    )
    people = map_people(roster, (o.name for o in owners.values() if o.name))
    links = Links(resources)
    with_public_ip = {
        vm.key for r in resources if r.type == PUBLIC_IP and (vm := _vm_of(r, links)) is not None
    }
    now = data.generated_at

    rows = []
    for r in resources:
        owner = owners[r.key]
        person = people.get(owner.name.casefold()) if owner.name else None
        rows.append(
            Row(
                r,
                owner,
                person,
                _age(now, r, owner),
                attached=_attached(r, links),
                public_ip=r.key in with_public_ip,
            )
        )

    grouped: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if row.card:
            grouped[row.card].append(row)
    cards = []
    for name, card_rows in grouped.items():
        person = next((r.person for r in card_rows if r.person), None)
        cards.append(Card(name, person.cohort if person else None, card_rows))
    seen_cards = set(grouped)
    for p in people.values():  # roster people with a cohort and nothing left get a green card
        if p.cohort and p.card not in seen_cards:
            seen_cards.add(p.card)
            cards.append(Card(p.card, p.cohort, []))

    unmapped = {row.person.username for row in rows if row.person and not row.cohort}
    incomplete = [f"{i.what} ({i.error})" for i in inventory.incomplete]
    by_sub: dict[str, list[LookupResult]] = defaultdict(list)
    for lk in data.lookups:
        by_sub[lk.subscription.id].append(lk)
    for lookups in by_sub.values():  # one entry per subscription, so the banner stays small
        failed = [lk for lk in lookups if lk.failed]
        if failed:
            codes = ", ".join(sorted({lk.error for lk in failed}))
            incomplete.append(
                f"Activity Log in {lookups[0].subscription.name},"
                f" {len(failed)} of {len(lookups)} groups ({codes})"
            )
    if inventory.graph_failed:
        partial = frozenset(s.id for s in inventory.scanned)
    else:
        partial = frozenset(inventory.arm_failed)
    return Report(
        generated_at=now,
        scanned=[s.name for s in inventory.scanned],
        not_scanned=list(inventory.not_scanned),
        rows=rows,
        cards=cards,
        # Every row without a card is listed somewhere, so nothing drops off the page.
        unknown=[r for r in rows if not r.card and r.owner.method not in ("orphaned", *QUIET)],
        orphaned=[r for r in rows if r.owner.method == "orphaned"],
        made_by_azure=[r for r in rows if r.owner.method == "azure"],
        dashboard=[r for r in rows if r.owner.method == "dashboard"],
        incomplete=incomplete,
        unmapped_users=len(unmapped),
        roster_loaded=roster is not None,
        cohorts=sorted({c.cohort for c in cards if c.cohort}),
        groups=inventory.groups,
        group_owners=group_owners(rows),
        node_groups=frozenset(links.node_groups),
        incomplete_subscriptions=partial,
    )
