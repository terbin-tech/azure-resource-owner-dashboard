import logging
from pathlib import Path

import pytest
from azure.core.pipeline.transport import RequestsTransport
from azure.identity import ManagedIdentityCredential

import dashboard.deployed as deployed
from dashboard.config import Config, ConfigError
from dashboard.deployed import Deployed, deployed_from_env, make_credential, run, scheduled_run
from dashboard.inventory import GROUPS_QUERY, RESOURCES_QUERY
from tests.fakes import (
    FakeBlob,
    FakeClients,
    FakeGraph,
    FakeMonitor,
    FakeResourceClient,
    FakeSubscriptionClient,
    arm_row,
    event,
    graph_page,
    http_error,
    sdk_subscription,
    storage_error,
)
from tests.helpers import SUB, T0, rid

NOW = T0.replace(day=11)
VM = rid("rg-a", "Microsoft.Compute/virtualMachines", "vm-a")
ACCOUNT = "dashdata0001"
CLIENT_ID = "00000000-0000-0000-0000-000000000021"
ENV = {"DASHBOARD_DATA_ACCOUNT": ACCOUNT, "DASHBOARD_SCAN_CLIENT_ID": CLIENT_ID}
SETTINGS = Path(__file__).parent.parent / "settings.json"
NO_ROSTER = storage_error("BlobNotFound", 404)


class RecordingWriter:
    def __init__(self) -> None:
        self.pages: list[str] = []

    def write(self, html: str) -> None:
        self.pages.append(html)


def clients_with(roster: FakeBlob) -> FakeClients:
    """One subscription and one VM, as in test_end_to_end, and the roster blob."""
    graph_vm = {
        "id": VM,
        "name": "vm-a",
        "type": "Microsoft.Compute/virtualMachines",
        "managedBy": "",
        "tags": {},
        "properties": {
            "extended": {"instanceView": {"powerState": {"code": "PowerState/running"}}}
        },
    }
    graph_group = {
        "id": f"/subscriptions/{SUB}/resourceGroups/rg-a",
        "name": "rg-a",
        "tags": {},
    }
    return FakeClients(
        FakeSubscriptionClient([sdk_subscription(SUB, "Course subscription")]),
        FakeGraph(
            {RESOURCES_QUERY: [graph_page([graph_vm])], GROUPS_QUERY: [graph_page([graph_group])]}
        ),
        {SUB: FakeResourceClient([arm_row(VM, created=T0)])},
        {SUB: FakeMonitor([event(VM, "student-a@school.example", when=T0)])},
        blobs={("config", "roster.csv"): roster},
    )


def run_once(roster: FakeBlob) -> tuple[int, str]:
    writer = RecordingWriter()
    count = run(clients_with(roster), writer, Config(max_age_days=7), ACCOUNT, NOW)
    (page,) = writer.pages
    return count, page


def test_run_writes_the_page_and_returns_the_incomplete_count():
    count, page = run_once(FakeBlob(error=NO_ROSTER))

    assert count == 0
    assert 'id="card-student-a"' in page
    assert 'id="stale" data-generated="' in page


def test_run_uses_the_roster_from_the_blob(caplog):
    roster = FakeBlob(b"username,primary_username,cohort\nstudent-a,student-a,cohort-y\n")

    with caplog.at_level(logging.WARNING, logger="dashboard.deployed"):
        count, page = run_once(roster)

    assert count == 0
    assert 'id="card-student-a" data-cohort="cohort-y"' in page
    assert caplog.records == []


def test_missing_roster_is_a_warning_not_incomplete(caplog):
    with caplog.at_level(logging.WARNING, logger="dashboard.deployed"):
        count, page = run_once(FakeBlob(error=NO_ROSTER))

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "no roster at config/roster.csv"
    assert count == 0
    assert 'id="incomplete"' not in page


@pytest.mark.parametrize(
    "roster,reason",
    [
        (FakeBlob(b"\xff\xfe"), "not UTF-8"),
        (
            FakeBlob(b"username,primary_username,cohort,email\nstudent-a,student-a,cohort-y,x\n"),
            "roster must have only the columns username, primary_username, cohort (found 4 columns)",
        ),
        (
            FakeBlob(error=storage_error("AuthorizationPermissionMismatch", 403)),
            "AuthorizationPermissionMismatch",
        ),
        (
            FakeBlob(b"username,primary_username,cohort\rstudent-a,student-a,cohort-y\r"),
            "line 1: not valid CSV",
        ),
    ],
    ids=["not-utf8", "extra-column", "forbidden", "lone-cr"],
)
def test_bad_roster_is_incomplete_and_the_page_is_still_written(roster, reason):
    count, page = run_once(roster)

    assert count == 1
    assert f"roster ({reason})" in page
    assert 'id="card-student-a"' in page


def test_settings_come_from_two_app_settings():
    assert deployed_from_env(ENV) == Deployed(ACCOUNT, CLIENT_ID)
    for name in ENV:
        with pytest.raises(ConfigError, match=f"missing app setting {name}"):
            deployed_from_env({k: v for k, v in ENV.items() if k != name})
    for name, value in (
        ("DASHBOARD_DATA_ACCOUNT", "Dash-Data"),
        ("DASHBOARD_SCAN_CLIENT_ID", "not-a-guid"),
    ):
        with pytest.raises(ConfigError) as exc:
            deployed_from_env(ENV | {name: value})
        assert name in str(exc.value)
        assert value not in str(exc.value)


def test_failure_raises_a_generic_error(monkeypatch):
    def boom(*args):
        raise http_error("AuthorizationFailed", message="caller student-a@school.example")

    monkeypatch.setattr(deployed, "run", boom)
    with pytest.raises(RuntimeError) as exc:
        scheduled_run(ENV, SETTINGS, NOW)
    assert str(exc.value) == "scan failed"
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


def test_failure_log_has_only_the_error_type_and_code(monkeypatch, caplog):
    def boom(*args):
        raise http_error(
            "AuthorizationFailed", message="caller student-a@school.example is not authorized"
        )

    monkeypatch.setattr(deployed, "run", boom)
    with pytest.raises(RuntimeError, match="^scan failed$"):
        scheduled_run(ENV, SETTINGS, NOW)

    (record,) = [r for r in caplog.records if r.name == "dashboard.deployed"]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "scan failed: HttpResponseError (AuthorizationFailed)"
    assert record.exc_info is None
    assert "school.example" not in caplog.text
    assert "not authorized" not in caplog.text


def test_a_bad_app_setting_fails_the_run_without_its_value(caplog):
    with pytest.raises(RuntimeError, match="^scan failed$"):
        scheduled_run(ENV | {"DASHBOARD_DATA_ACCOUNT": "Dash-Data"}, SETTINGS, NOW)

    (record,) = caplog.records
    assert record.getMessage() == "scan failed: ConfigError (ConfigError)"
    assert "Dash-Data" not in caplog.text


def test_incomplete_run_logs_one_warning_with_the_count_in_the_message(monkeypatch, caplog):
    monkeypatch.setattr(deployed, "run", lambda *args: 2)
    with caplog.at_level(logging.WARNING, logger="dashboard.deployed"):
        scheduled_run(ENV, SETTINGS, NOW)

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "run incomplete: 2"


def test_complete_run_logs_nothing(monkeypatch, caplog):
    monkeypatch.setattr(deployed, "run", lambda *args: 0)
    with caplog.at_level(logging.WARNING):
        scheduled_run(ENV, SETTINGS, NOW)

    assert caplog.records == []


def test_the_credential_is_the_scan_identity_alone(monkeypatch):
    def refuse(self, request, **kwargs):
        raise AssertionError("building the credential sent a request")

    monkeypatch.setattr(RequestsTransport, "send", refuse)
    credential = make_credential(Deployed(ACCOUNT, CLIENT_ID))
    assert type(credential) is ManagedIdentityCredential

    seen = {}

    class Spy:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(deployed, "ManagedIdentityCredential", Spy)
    make_credential(Deployed(ACCOUNT, CLIENT_ID))
    assert seen == {"client_id": CLIENT_ID}
