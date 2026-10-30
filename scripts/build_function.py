"""Build the Functions package: the entry point, its settings and src/dashboard -> build/function.zip.

    python scripts/build_function.py      # never touches Azure

The package sits at the zip root next to function_app.py, as the Python v2 folder layout keeps
importable helper folders, with the one host.json at the root, sources:
https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python
https://learn.microsoft.com/en-us/azure/azure-functions/python-build-options
No dependencies go in: the remote build installs requirements.txt.

Reproducible: sorted entries, fixed timestamps and modes, no bytecode. The same sources give the
same zip.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZIP = ROOT / "build" / "function.zip"
TOP_FILES = ("function_app.py", "host.json", "requirements.txt", "settings.json")
FIXED_TIME = (1980, 1, 1, 0, 0, 0)  # the earliest time a zip entry can hold


def build(root: Path, dest: Path) -> list[str]:
    """Write the zip to `dest` and return its entry names, sorted."""
    entries = {name: root / name for name in TOP_FILES}
    pkg = root / "src" / "dashboard"
    for path in pkg.rglob("*.py"):
        rel = path.relative_to(pkg)
        if "__pycache__" not in rel.parts:
            entries[f"dashboard/{rel.as_posix()}"] = path
    names = sorted(entries)
    with zipfile.ZipFile(dest, "w") as zf:
        for name in names:
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.external_attr = 0o644 << 16
            zf.writestr(
                info,
                entries[name].read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    return names


def main() -> int:
    ZIP.parent.mkdir(exist_ok=True)
    build(ROOT, ZIP)
    print(f"wrote {ZIP.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
