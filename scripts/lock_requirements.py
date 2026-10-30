"""Write requirements.txt from a clean venv's pip freeze, with every file's SHA-256 from PyPI.

Run from the repo root with the freeze of a fresh Python 3.14 venv that holds only
pyproject.toml's dependencies:

    pip freeze --all --exclude pip --exclude setuptools | python scripts/lock_requirements.py

Each requirement carries the hash of every file PyPI lists for that version (the JSON API's
urls[].digests.sha256), so pip's hash-checking mode accepts whichever wheel fits the platform
it installs on.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.request import urlopen

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
HEADER = (
    "# Installed by the Flex remote build (python-build-options), in pip's hash mode.\n"
    "# Every pin, transitive ones too, from a clean Python 3.14 venv of pyproject.toml's\n"
    "# dependencies, with the SHA-256 of every file PyPI lists for that version.\n"
    "# Regenerate with scripts/lock_requirements.py when a pin changes.\n"
)
# A freeze line of an index install. Anything else (`name @ file:///...` for an editable or
# local install) is refused: it would carry a local path, and pip cannot hash-check it.
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.+!_-]*)")
SHA256 = re.compile(r"[0-9a-f]{64}")


def normalize(name: str) -> str:
    """The PEP 503 normalized name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_freeze(text: str) -> dict[str, str]:
    pins = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = PIN.fullmatch(line.strip())
        if match is None:
            raise ValueError(f"not a name==version pin: {line.strip()!r}")
        pins[match[1]] = match[2]
    return pins


def render(pins: dict[str, str], hashes: dict[str, list[str]]) -> str:
    lines = ["--require-hashes", "--only-binary :all:", HEADER.rstrip("\n")]
    for name in sorted(pins, key=normalize):
        pin = f"{name}=={pins[name]}"
        if PIN.fullmatch(pin) is None:
            raise ValueError(f"not a name==version pin: {pin!r}")
        digests = hashes[name]
        if not digests or not all(SHA256.fullmatch(d) for d in digests):
            raise ValueError(f"no valid SHA-256 for {pin}")
        lines.append(f"{pin} \\")
        lines.extend(f"    --hash=sha256:{d} \\" for d in digests[:-1])
        lines.append(f"    --hash=sha256:{digests[-1]}")
    return "\n".join(lines) + "\n"


def fetch_hashes(name: str, version: str) -> list[str]:
    url = PYPI_JSON.format(name=name, version=version)
    with urlopen(url, timeout=30) as response:  # noqa: S310 (a fixed https URL)
        release = json.load(response)
    return [file["digests"]["sha256"] for file in release["urls"]]


def main() -> int:
    try:
        pins = parse_freeze(sys.stdin.read())
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    hashes = {name: fetch_hashes(name, version) for name, version in pins.items()}
    REQUIREMENTS.write_text(render(pins, hashes), encoding="utf-8")
    print(f"wrote {len(pins)} pins to {REQUIREMENTS.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
