import re
import tomllib
from pathlib import Path

import pytest

from scripts.lock_requirements import normalize, render

REPO = Path(__file__).resolve().parents[1]
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


def _requirements() -> tuple[list[str], list[str]]:
    """The option lines and the requirements, each with its continuation lines joined."""
    options, requirements, current = [], [], ""
    for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if not current and (not line.strip() or line.startswith("#")):
            continue
        current += line.removesuffix("\\").strip() + " "
        if line.endswith("\\"):
            continue
        (options if current.startswith("--") else requirements).append(current.strip())
        current = ""
    assert current == "", "requirements.txt ends on a continuation line"
    return options, requirements


def test_requirements_pin_every_runtime_dependency():
    options, requirements = _requirements()
    assert "--require-hashes" in options
    assert "--only-binary :all:" in options
    pins = {}
    for requirement in requirements:
        pin, *hashes = requirement.split()
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.+!_-]*", pin), pin
        assert hashes, pin
        assert all(HASH.fullmatch(h) for h in hashes), pin
        name, version = pin.split("==")
        pins[normalize(name)] = version
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    for dependency in pyproject["project"]["dependencies"]:
        name, version = dependency.split("==")
        assert pins.get(normalize(name)) == version, dependency
    # The Python quickstart's requirements.txt: "DO NOT include azure-functions-worker in this
    # file".
    assert "azure-functions-worker" not in pins


@pytest.mark.parametrize(
    "pins",
    [
        {"a @ file:///home/u/a": "1"},
        {"a": "1 @ file:///x"},
        {"a/b": "1"},
        {"a": "1/2"},
        {"a b": "1"},
        {"a": "1 2"},
    ],
)
def test_render_refuses_a_pin_that_is_not_name_equals_version(pins):
    with pytest.raises(ValueError):
        render(pins, dict.fromkeys(pins, ["0" * 64]))


def test_render_writes_sorted_hashed_pins():
    text = render({"b": "2", "a": "1"}, {"a": ["0" * 64], "b": ["1" * 64]})
    assert text.endswith(
        f"a==1 \\\n    --hash=sha256:{'0' * 64}\nb==2 \\\n    --hash=sha256:{'1' * 64}\n"
    )
    assert text.splitlines()[:2] == ["--require-hashes", "--only-binary :all:"]
