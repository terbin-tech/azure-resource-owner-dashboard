"""Pre-commit hook: block commits that contain private terms.

Terms (sign-in names, the school email domain, tenant and subscription IDs, ...) come from
the gitignored file `.denylist`, one per line; `#` starts a comment. Without the file the
hook warns and passes, so a successor without access can still commit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DENYLIST = Path(".denylist")


def load_terms(path: Path = DENYLIST) -> list[str]:
    if not path.exists():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.split("#", 1)[0].strip()
        if term:
            terms.append(term.lower())
    return terms


def find_hits(files: list[str], terms: list[str]) -> list[tuple[str, int, str]]:
    if not terms:
        return []
    alternatives = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    pattern = re.compile(rf"(?<![a-z0-9])({alternatives})(?![a-z0-9])", re.IGNORECASE)
    hits = []
    for name in files:
        try:
            text = Path(name).read_text(encoding="utf-8")
        except UnicodeDecodeError, FileNotFoundError, IsADirectoryError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for match in pattern.finditer(line):
                hits.append((name, number, match.group(1).lower()))
    return hits


def main(argv: list[str] | None = None) -> int:
    files = sys.argv[1:] if argv is None else argv
    terms = load_terms()
    if not terms:
        print("check-denylist: no .denylist file, skipping", file=sys.stderr)
        return 0
    hits = find_hits(files, terms)
    for name, number, _term in hits:
        # The term itself is not printed: CI logs of a public repo are public.
        print(f"{name}:{number}: private term from .denylist", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
