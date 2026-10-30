"""Owner -> person -> cohort. Sign-in prefixes only, never real names."""

from __future__ import annotations

import csv
import io
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

ROSTER_COLUMNS = ("username", "primary_username", "cohort")
# Entra username policy, ASCII only: A-Za-z0-9 ' . - _ ! # ^ ~, up to 64 characters before the @,
# and no "." right before it. https://learn.microsoft.com/en-us/entra/identity/authentication/concept-sspr-policy
SIGNIN_NAME = re.compile(r"[A-Za-z0-9'._!#^~-]{0,63}[A-Za-z0-9'_!#^~-]", re.ASCII)
# Keyed by the casefolded username; values are (primary_username as written, cohort).
Roster = dict[str, tuple[str, str]]


class RosterError(ValueError):
    """Bad roster file. Messages name line numbers, counts and expected columns, never values."""


@dataclass(frozen=True)
class Person:
    username: str  # casefolded
    card: str  # card title: the roster's primary_username, else the owner name as written
    cohort: str | None


def parse_roster(text: str) -> Roster:
    """Read usernames and cohorts from a roster CSV.

    Errors name line numbers or counts, never values, so a roster with a real-name column
    can't leak it into a log or banner.
    """
    reader = csv.DictReader(io.StringIO(text.removeprefix("\ufeff")))
    try:
        fieldnames = reader.fieldnames = [f.strip() for f in reader.fieldnames or []]
        rows = list(reader)
    except csv.Error:
        # A lone carriage return or a field over the csv module's limit; its text is not shown.
        # line_num counts the lines read before the one that failed.
        raise RosterError(f"line {reader.line_num + 1}: not valid CSV") from None
    missing = [c for c in ROSTER_COLUMNS if c not in fieldnames]
    if missing:
        raise RosterError(f"roster is missing columns: {', '.join(missing)}")
    if sorted(fieldnames) != sorted(ROSTER_COLUMNS):
        raise RosterError(
            f"roster must have only the columns {', '.join(ROSTER_COLUMNS)} "
            f"(found {len(fieldnames)} columns)"
        )
    entries: list[tuple[int, str, str, str]] = []
    seen: set[str] = set()
    for line, row in enumerate(rows, start=2):
        if None in row:  # more values than columns
            raise RosterError(f"line {line}: more values than columns")
        user = (row["username"] or "").strip()
        if not user or user.startswith("#"):
            continue
        if user.casefold() in seen:
            raise RosterError(f"line {line}: duplicate username")
        seen.add(user.casefold())
        primary = (row["primary_username"] or "").strip() or user
        entries.append((line, user, primary, (row["cohort"] or "").strip()))
    for line, _, primary, _ in entries:
        if primary.casefold() not in seen and not SIGNIN_NAME.fullmatch(primary):
            raise RosterError(
                f"line {line}: primary_username is neither a roster username nor a sign-in name"
            )
    return {user.casefold(): (primary, cohort) for _, user, primary, cohort in entries}


def load_roster_file(path: str | None) -> Roster | None:
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return parse_roster(fh.read())


def map_people(roster: Roster | None, owner_names: Iterable[str]) -> dict[str, Person]:
    """Build one Person per casefolded name over the roster usernames and the owner names.

    The roster needs no Azure record: a deleted user's resources still find their cohort.
    Two spellings of one name are one person, and cards that differ only in case share one
    spelling, so a cohort filter and the help line count one card.
    """
    roster = roster or {}
    spellings: dict[str, set[str]] = {}
    for name in owner_names:
        if name:
            spellings.setdefault(name.casefold(), set()).add(name)
    cards: dict[str, str] = {}
    for key in sorted(roster.keys() | spellings.keys()):
        cards[key] = roster[key][0] if key in roster else sorted(spellings[key])[0]
    card_of_fold: dict[str, str] = {}
    for card in cards.values():
        card_of_fold[card.casefold()] = min(card, card_of_fold.get(card.casefold(), card))
    return {
        key: Person(key, card_of_fold[card.casefold()], roster.get(key, ("", ""))[1] or None)
        for key, card in cards.items()
    }
