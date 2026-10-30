import os
import re
import stat
from datetime import UTC, datetime, timedelta

import pytest
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import AzureCliCredential, CredentialUnavailableError

import dashboard.__main__ as cli
from dashboard.config import Config
from dashboard.demo import DEMO_ROSTER, demo_scan
from dashboard.events import is_person, username
from dashboard.model import parse_id
from dashboard.pipeline import build_report
from dashboard.render import STOPPED_TEXT, UNKNOWN_POWER_TEXT, checks
from tests.fakes import FakeCredential, http_error

APP = "00000000-0000-0000-0000-000000000099"


def _config(tmp_path, text=""):
    path = tmp_path / "dashboard.toml"
    path.write_text(text)
    return str(path)


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _no_azure(monkeypatch, calls):
    """Record every attempt to reach Azure instead of making it."""
    monkeypatch.setattr(cli, "make_credential", lambda: calls.append("credential"))
    monkeypatch.setattr(cli, "make_clients", lambda credential: calls.append("clients"))


def test_demo_writes_only_the_page_with_private_modes(tmp_path):
    out = tmp_path / "out-demo"
    assert cli.main(["demo", "--out", str(out), "--config", _config(tmp_path)]) == 0
    assert sorted(os.listdir(out)) == ["index.html"]
    page = (out / "index.html").read_text()
    assert "<title>Forgotten Azure resources</title>" in page
    assert "$" not in page
    assert '<strong class="check">Check this:</strong>' in page
    assert _mode(out) == 0o700
    assert _mode(out / "index.html") == 0o600


def test_scan_writes_page_and_verify_report_and_nothing_else(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "make_credential", FakeCredential)
    monkeypatch.setattr(cli, "make_clients", lambda credential: object())
    monkeypatch.setattr(cli, "check_credentials", lambda credential: None)
    monkeypatch.setattr(cli, "scan", lambda clients, cfg, now: demo_scan())
    out = tmp_path / "out"
    assert cli.main(["scan", "--out", str(out), "--config", _config(tmp_path)]) == 0
    assert sorted(os.listdir(out)) == ["index.html", "verify.md"]  # who created what is never saved
    assert "# Verification report" in (out / "verify.md").read_text()
    assert _mode(out / "verify.md") == 0o600


def test_demo_data_uses_placeholders_only():
    data = demo_scan()
    inventory = data.inventory
    ids = [r.id for r in inventory.resources] + [g.id for g in inventory.groups.values()]
    assert all(i.startswith("/subscriptions/00000000-0000-0000-0000-0000000000") for i in ids)
    callers = {w.caller for lk in data.lookups for w in lk.writes}
    assert all(c.endswith("@school.example") or c == APP for c in callers), callers
    owners = {username(c) for c in callers if is_person(c)}
    owners |= {r.owner.name for r in build_report(data, DEMO_ROSTER).rows if r.owner.name}
    assert owners <= {"student-a", "student-b", "student-c"}


def test_demo_reads_one_lookup_per_group_as_a_scan_does():
    data = demo_scan()
    held = sorted({r.group for r in data.inventory.resources}, key=str.casefold)
    assert [lk.group for lk in data.lookups] == held
    for lk in data.lookups:  # each lookup holds only its own group's writes
        assert all(parse_id(w.resource)[1] == lk.group.casefold() for w in lk.writes)
    assert not any(lk.failed for lk in data.lookups)


def test_explicit_config_that_does_not_exist_is_an_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["demo", "--out", str(tmp_path / "o"), "--config", str(tmp_path / "nope.toml")])
    assert exc.value.code == 2
    assert not (tmp_path / "o").exists()


def test_out_dir_parents_are_created(tmp_path):
    out = tmp_path / "a" / "b" / "out-demo"
    assert cli.main(["demo", "--out", str(out), "--config", _config(tmp_path)]) == 0
    assert (out / "index.html").exists()


def test_bad_roster_fails_before_any_azure_call(tmp_path, monkeypatch):
    calls = []
    _no_azure(monkeypatch, calls)
    roster = tmp_path / "roster.csv"
    roster.write_text("username\nstudent-a\n")
    cfg = _config(tmp_path, f'roster_path = "{roster}"\n')
    with pytest.raises(SystemExit) as exc:
        cli.main(["scan", "--out", str(tmp_path / "out"), "--config", cfg])
    assert exc.value.code == 2
    assert calls == []


def test_bad_config_fails_before_any_azure_call(tmp_path, monkeypatch):
    calls = []
    _no_azure(monkeypatch, calls)
    cfg = _config(tmp_path, 'colour = "red"\n')
    with pytest.raises(SystemExit) as exc:
        cli.main(["scan", "--out", str(tmp_path / "out"), "--config", cfg])
    assert exc.value.code == 2
    assert calls == []


def test_local_login_is_the_cli_credential_alone():
    # AzureCliCredential locally, no credential chain. Building it makes no call.
    assert type(cli.make_credential()) is AzureCliCredential


@pytest.mark.parametrize(
    "error",
    [
        CredentialUnavailableError(
            "Please run 'az login' to set up an account for student-a@school.example"
        ),
        ClientAuthenticationError("token for student-a@school.example expired"),
    ],
)
def test_expired_login_stops_before_the_scan(tmp_path, monkeypatch, capsys, error):
    monkeypatch.setattr(cli, "make_credential", lambda: FakeCredential(error))
    monkeypatch.setattr(cli, "make_clients", lambda credential: pytest.fail("no clients"))
    monkeypatch.setattr(cli, "scan", lambda clients, cfg, now: pytest.fail("scan must not run"))
    out = tmp_path / "out"
    assert cli.main(["scan", "--out", str(out), "--config", _config(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert type(error).__name__ in captured.err
    assert "Run 'az login'" in captured.err
    assert "student-a@school.example" not in captured.err + captured.out  # the class name only
    assert not (out / "index.html").exists()


def test_an_azure_error_in_the_scan_prints_its_code_never_its_message(
    tmp_path, monkeypatch, capsys
):
    # An Azure error message can name the signed-in account or carry IDs.
    def failing_scan(clients, cfg, now):
        raise http_error("SomethingFailed", 500, message="student-a@school.example")

    monkeypatch.setattr(cli, "make_credential", FakeCredential)
    monkeypatch.setattr(cli, "make_clients", lambda credential: object())
    monkeypatch.setattr(cli, "scan", failing_scan)
    out = tmp_path / "out"
    assert cli.main(["scan", "--out", str(out), "--config", _config(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "SomethingFailed" in captured.err
    assert "student-a" not in captured.err + captured.out
    assert not (out / "index.html").exists()


def test_missing_settings_file_is_an_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "demo",
                "--out",
                str(tmp_path / "o"),
                "--config",
                _config(tmp_path),
                "--settings",
                str(tmp_path / "nope.json"),
            ]
        )
    assert exc.value.code == 2


def test_demo_page_is_dated_now_so_it_is_never_stale(tmp_path):
    out = tmp_path / "o"
    before = datetime.now(UTC).replace(second=0, microsecond=0)
    assert cli.main(["demo", "--out", str(out), "--config", _config(tmp_path)]) == 0
    stamp = re.search(r'data-generated="([^"]+)"', (out / "index.html").read_text()).group(1)
    assert datetime.fromisoformat(stamp) >= before - timedelta(minutes=1)


def test_demo_shows_each_case_with_a_name_that_says_what_it_is():
    data = demo_scan()
    report = build_report(data, DEMO_ROSTER)
    details = {r.owner.detail for r in report.rows}
    assert {
        "via vm-running-public-ip",
        "via nic-attached-to-its-vm",
        "via aks-cluster",
        "via resource group",
    } <= details
    assert [r.owner.detail for r in report.unknown] == ["no creation event in the last 90 days"]
    assert len(report.made_by_azure) == 1
    assert len(report.orphaned) == 1
    assert len(report.dashboard) == 1
    assert [c.name for c in report.cards if not c.rows] == ["student-c"]  # an all-clear card
    assert report.cohorts == ["cohort-x", "cohort-y"]
    assert report.not_scanned == ["Old course subscription"]
    assert report.node_groups
    owners = report.group_owners.values()
    assert any(len(cards - {None}) == 2 for cards in owners)  # a group two students share
    assert any(None in cards for cards in owners)  # and one holding a row without a card
    reasons = {why for r in report.rows for why in checks(r, Config())}
    assert reasons == {
        "older than 4 days",
        "running with a public IP",
        STOPPED_TEXT,
        UNKNOWN_POWER_TEXT,
    }
    assert data.generated_at == datetime(
        2026, 10, 9, 5, 0, tzinfo=UTC
    )  # 07:00 Central European time, the morning run
