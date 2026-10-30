"""Masked summary of a what-if or deployment result in out/deploy/.

    python scripts/deploy_summary.py out/deploy/<file>.json

Reads the JSON of `az deployment sub what-if --no-pretty-print` or of `az deployment sub create`
(or `show`), the WhatIfChange and DeploymentExtended shapes:
https://learn.microsoft.com/en-us/rest/api/resources/deployments/what-if-at-subscription-scope
It prints change counts by change type and resource type, a DELETE warning, and for a deployment
its provisioning state and error codes. Never a resource ID, name, before/after/delta value,
parameter or message: those carry subscription IDs, resource names and sign-in names. The full
file stays in out/deploy/ for the maintainer to read.

What-if also lists `potentialChanges` beside `changes` (azure-mgmt-resource-deployments
WhatIfOperationResult, key `properties.potentialChanges`; the CLI's JSON output moves both to the
top level), counted the same way on lines that start with "Potential".
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

NO_SUMMARY = "no summary found (read the file yourself)"
# https://learn.microsoft.com/en-us/rest/api/resources/deployments/what-if-at-subscription-scope#changetype
CHANGE_TYPES = {
    name.lower(): name
    for name in ("Create", "Delete", "Deploy", "Ignore", "Modify", "NoChange", "Unsupported")
}
BARE_GROUP = re.compile(r"/subscriptions/[^/]+/resourceGroups/[^/]+", re.IGNORECASE)
# A single-quoted resource type argument, `Namespace.Name/type[/type...]`; an ID argument starts with `/`.
TYPE_ARGUMENT = re.compile(r"'([A-Za-z0-9]+(?:\.[A-Za-z0-9]+)+(?:/[A-Za-z0-9]+)+)'")
WORD = re.compile(r"[A-Za-z]{1,40}")
CODE = re.compile(r"[A-Za-z0-9._-]{1,80}")


def resource_type(resource_id: str) -> str:
    """The type from a resource ID, without any name: `Namespace/type/childType/...`."""
    if resource_id.startswith("["):
        # What-if could not evaluate the name (it depends on `reference()` to something made in
        # the same deployment), so the ID is the template expression itself, which holds names and
        # IDs. Only its type argument is safe to print.
        found = TYPE_ARGUMENT.search(resource_id)
        return found.group(1) if found else "unevaluated"
    if "/providers/" not in resource_id:
        return (
            "Microsoft.Resources/resourceGroups" if BARE_GROUP.fullmatch(resource_id) else "unknown"
        )
    # The last /providers/ segment: an extension resource (a role assignment on a container) is
    # its own type, not its parent's.
    parts = [p for p in resource_id.rsplit("/providers/", 1)[1].split("/") if p]
    return "/".join([parts[0], *parts[1::2]])


def _changes(doc: dict, key: str) -> list | None:
    changes = doc.get(key)
    if changes is None and isinstance(doc.get("properties"), dict):
        changes = doc["properties"].get(key)
    return changes if isinstance(changes, list) else None


def _error_codes(error: object) -> list[str]:
    """The code of an error and of its details, depth first, each once, nothing else."""
    codes: list[str] = []
    if isinstance(error, dict):
        code = error.get("code")
        if isinstance(code, str):
            codes.append(code if CODE.fullmatch(code) else "unrecognised")
        for detail in error.get("details") or []:
            codes.extend(_error_codes(detail))
    return list(dict.fromkeys(codes))


def _what_if_lines(changes: list, prefix: str = "") -> tuple[list[str], bool]:
    """The count lines for one change list, and whether it deletes anything."""
    by_change: Counter[str] = Counter()
    by_type: Counter[tuple[str, str]] = Counter()
    for change in changes:
        if not isinstance(change, dict):
            continue
        raw = change.get("changeType")
        kind = CHANGE_TYPES.get(raw.lower(), "Unknown") if isinstance(raw, str) else "Unknown"
        resource_id = change.get("resourceId")
        by_change[kind] += 1
        by_type[
            (kind, resource_type(resource_id) if isinstance(resource_id, str) else "unknown")
        ] += 1
    if not by_change:
        return [f"{prefix}No changes"], False
    lines = [f"{prefix}{kind}: {n}" for kind, n in sorted(by_change.items())]
    lines += [f"{prefix}{kind} {rtype} x{n}" for (kind, rtype), n in sorted(by_type.items())]
    return lines, by_change["Delete"] > 0


def summarize(doc: object) -> list[str]:
    """The masked lines for a what-if or deployment document, [] when it is neither."""
    if not isinstance(doc, dict):
        return []
    lines: list[str] = []
    deletes = False
    for key, prefix in (("changes", ""), ("potentialChanges", "Potential ")):
        changes = _changes(doc, key)
        # An empty potential list says nothing; an empty changes list is "No changes".
        if changes is not None and (changes or not prefix):
            found, deleted = _what_if_lines(changes, prefix)
            lines += found
            deletes = deletes or deleted
    if deletes:
        lines.insert(0, "DELETE: the plan deletes resources, read the file before going on")
    props = doc.get("properties")
    if isinstance(props, dict) and "provisioningState" in props:
        state = props["provisioningState"]
        lines.append(
            f"provisioningState: {state if isinstance(state, str) and WORD.fullmatch(state) else 'unrecognised'}"
        )
    for error in (doc.get("error"), props.get("error") if isinstance(props, dict) else None):
        lines += [f"error: {code}" for code in _error_codes(error)]
    return lines


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: deploy_summary.py out/deploy/<file>.json", file=sys.stderr)
        return 1
    path = Path(args[0])
    if not path.is_file():
        print("no such file")
        return 1
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        doc = None
    lines = summarize(doc)
    if not lines:
        print(NO_SUMMARY)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
