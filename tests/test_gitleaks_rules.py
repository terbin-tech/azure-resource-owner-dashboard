import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FAKE = "00000000-0000-0000-0000-000000000001"


def _rule(rule_id="azure-guid"):
    rules = tomllib.loads((REPO / ".gitleaks.toml").read_text())["rules"]
    rule = next(r for r in rules if r["id"] == rule_id)
    allow = [re.compile(a) for al in rule["allowlists"] for a in al["regexes"]]
    return re.compile(rule["regex"]), allow


def _real(*parts):  # built at runtime so this file passes gitleaks
    return "-".join(parts)


def test_guid_rule_finds_a_guid_glued_to_any_neighbour():
    pattern, _ = _rule()
    real = _real("1a2b3c4d", "1111", "2222", "3333", "444455556666")
    for prefix in ("", "%2F", "_", "x", "/subscriptions/"):
        assert pattern.search(prefix + real + "x").group() == real


def test_allowlist_permits_only_the_placeholder_family():
    pattern, allow = _rule()
    for ok in (FAKE, FAKE[:-2] + "99"):
        assert pattern.search(ok) and any(a.search(ok) for a in allow)
    for real in (
        _real("1a2b3c4d", "1111", "2222", "3333", "444455556666"),
        _real("10000000", "2000", "3000", "4000", "500000000006"),
        _real("00000000", "0000", "0000", "0000", "000000000100"),
    ):
        assert not any(a.search(pattern.search(real).group()) for a in allow)


def test_email_rule_allows_only_reserved_example_domains():
    pattern, allow = _rule("email")
    for ok in ("student-a@school.example", "Student-A@other.example"):
        assert any(a.search(pattern.search(ok).group()) for a in allow)
    real = "@".join(["student", "school-domain.edu"])
    assert pattern.search(real).group() == real
    assert not any(a.search(real) for a in allow)
    for not_email in ("actions/checkout@v4.2.2", "@dataclass", '"@school.example"'):
        assert not pattern.search(not_email)
