import logging
from datetime import timedelta

import pytest

from dashboard.clients import WriteRefused
from dashboard.events import (
    WINDOW,
    LookupResult,
    activity_filter,
    by_resource,
    creation_of,
    fetch_writes,
    group_creation,
    is_person,
    username,
    window_start,
)
from dashboard.model import Subscription
from tests.fakes import FakeMonitor, event, http_error
from tests.helpers import NOW, SUB, SUB2, T0, grp, res, rid, write

VM_TYPE = "Microsoft.Compute/virtualMachines"
VM_ID = rid("rg-a", VM_TYPE, "vm-1")
SUBSCRIPTION = Subscription(SUB, "Sub")


def fetch(monitor: FakeMonitor, group: str = "rg-a", **kwargs) -> LookupResult:
    return fetch_writes(monitor, SUBSCRIPTION, T0, NOW, group, **kwargs)


def test_filter_is_the_documented_time_range_and_one_group():
    expected = (
        "eventTimestamp ge '2026-10-01T12:00:00Z' and eventTimestamp le '2026-10-02T12:00:00Z'"
        " and resourceGroupName eq 'rg-a'"
    )
    assert activity_filter(T0, NOW, "rg-a") == expected
    monitor = FakeMonitor([])
    fetch(monitor)
    # `caller` is not a documented $select value (Activity Logs - List), so select stays unset.
    assert monitor.activity_logs.filters == [expected]
    assert monitor.activity_logs.selects == [None]
    with pytest.raises(ValueError):
        activity_filter(T0, NOW, "rg-a' or 'x")


def test_every_lookup_names_its_group():
    monitor = FakeMonitor([event(VM_ID)])
    result = fetch(monitor, group="rg-b")
    assert monitor.activity_logs.filters == [activity_filter(T0, NOW, "rg-b")]
    assert result.group == "rg-b"
    # A lookup without a group would match no group key, so its failure would reach nobody.
    with pytest.raises(TypeError):
        fetch_writes(FakeMonitor([]), SUBSCRIPTION, T0, NOW)
    with pytest.raises(TypeError):
        LookupResult(SUBSCRIPTION)
    with pytest.raises(TypeError):
        activity_filter(T0, NOW)


def test_window_starts_an_hour_inside_the_lookback():
    assert window_start(NOW, 90) == NOW - timedelta(days=90) + timedelta(hours=1)


def test_keeps_only_succeeded_writes():
    ids = [rid("rg-a", VM_TYPE, f"vm-{n}") for n in range(8)]
    monitor = FakeMonitor(
        [
            event(ids[0], op="Microsoft.Compute/virtualMachines/write"),
            event(ids[1], op="MICROSOFT.COMPUTE/VIRTUALMACHINES/WRITE"),
            event(ids[2], op="Microsoft.Compute/virtualMachines/delete"),
            event(ids[3], op="Microsoft.Compute/virtualMachines/restart/action"),
            event(ids[4], status="Started"),
            event(ids[5], status="Accepted"),
            event(ids[6], status="Failed"),
            event(ids[7], status="Succeeded"),
        ]
    )
    result = fetch(monitor)
    assert [w.resource for w in result.writes] == [
        ids[0].casefold(),
        ids[1].casefold(),
        ids[7].casefold(),
    ]


@pytest.mark.parametrize(
    ("sub_status", "status_code", "created"),
    [
        ("Created", None, True),
        (None, "Created", True),
        ("OK", "OK", False),
        (None, None, False),
    ],
)
def test_created_from_substatus_or_status_code(sub_status, status_code, created):
    # With sub_status=None the event has no `properties` either unless status_code fills it.
    monitor = FakeMonitor([event(VM_ID, sub_status=sub_status, status_code=status_code)])
    (kept,) = fetch(monitor).writes
    assert kept.created is created
    assert kept.caller == "student-a@school.example"
    assert kept.resource == VM_ID.casefold()
    assert kept.time == T0


def test_events_without_a_resource_id_are_skipped():
    monitor = FakeMonitor([event(None), event(""), event(VM_ID)])
    assert [w.resource for w in fetch(monitor).writes] == [VM_ID.casefold()]


def test_odd_events_are_skipped_not_fatal():
    monitor = FakeMonitor(
        [
            event(VM_ID, when=None),
            event(VM_ID, op=None),
            event(VM_ID, status=None),
            event(VM_ID, caller=None, sub_status=None),
        ]
    )
    result = fetch(monitor)
    assert not result.failed
    assert [(w.caller, w.created) for w in result.writes] == [("", False)]


def test_lookup_failure_keeps_partial_writes():
    monitor = FakeMonitor([event(VM_ID), event(VM_ID)], http_error("AuthorizationFailed"), after=1)
    result = fetch(monitor)
    assert len(result.writes) == 1
    assert result.error == "AuthorizationFailed"
    assert result.failed is True
    assert result.subscription == SUBSCRIPTION


def test_lookup_failure_is_logged_with_the_group_and_code_only(caplog):
    message = "distinctive-error-text-8"
    monitor = FakeMonitor([], http_error("AuthorizationFailed", 403, message))
    with caplog.at_level(logging.DEBUG):
        fetch(monitor, group="rg-b")
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [(r.levelno, r.getMessage()) for r in warnings] == [
        (logging.WARNING, "Activity Log lookup failed for group rg-b: AuthorizationFailed")
    ]
    # The error message must reach neither the log text nor an attached traceback.
    assert all(r.exc_info is None and message not in r.getMessage() for r in caplog.records)


def test_success_is_not_failed():
    result = fetch(FakeMonitor([event(VM_ID)]))
    assert result.failed is False
    assert result.error == ""


def test_a_refused_write_is_not_swallowed():
    monitor = FakeMonitor([event(VM_ID)], WriteRefused("PUT"))
    with pytest.raises(WriteRefused):
        fetch(monitor)


def test_duration_covers_the_whole_iteration():
    ticks = iter([10.0, 22.5])
    result = fetch(FakeMonitor([event(VM_ID)]), clock=lambda: next(ticks))
    assert result.duration_s == 12.5


def test_person_and_app_callers():
    assert is_person("student-a@school.example")
    for app in ("00000000-0000-0000-0000-000000000009", "Microsoft.Insights/alertRules", ""):
        assert not is_person(app)
    assert username("Student-A@school.example") == "Student-A"


def test_by_resource_merges_subscriptions_casefolded():
    upper = rid("RG-A", VM_TYPE, "VM-1", SUB2).replace("virtualMachines", "VIRTUALMACHINES")
    lower = upper.casefold()
    first = fetch(FakeMonitor([event(upper)]))
    second = fetch_writes(FakeMonitor([event(lower)]), Subscription(SUB2, "Other"), T0, NOW, "rg-a")
    merged = by_resource([first, second])
    assert list(merged) == [lower]
    assert len(merged[lower]) == 2


def test_reused_name_picks_the_created_write_nearest_created_time():
    resource = res(VM_TYPE, "vm-1", created_at=T0 - timedelta(days=1) + timedelta(minutes=2))
    old = write(resource, "student-a@school.example", minutes=-20 * 24 * 60)
    new = write(resource, "student-b@school.example", minutes=-24 * 60)
    assert creation_of(resource, [old, new]) == new


def test_created_write_outside_the_window_is_ignored():
    resource = res(VM_TYPE, "vm-1", created_at=T0)
    assert creation_of(resource, [write(resource, minutes=-180)]) is None


def test_window_is_fifteen_minutes():
    assert WINDOW == timedelta(minutes=15)
    resource = res(VM_TYPE, "vm-1", created_at=T0)
    assert creation_of(resource, [write(resource, minutes=15)]) is not None
    assert creation_of(resource, [write(resource, minutes=15.5)]) is None


def test_falls_back_to_the_earliest_write_in_the_window():
    resource = res(VM_TYPE, "vm-1", created_at=T0)
    late = write(resource, minutes=9, created=False)
    early = write(resource, minutes=1, created=False)
    assert creation_of(resource, [late, early]) == early


def test_created_write_beats_an_earlier_plain_write():
    resource = res(VM_TYPE, "vm-1", created_at=T0)
    plain = write(resource, minutes=1, created=False)
    made = write(resource, "student-b@school.example", minutes=5)
    assert creation_of(resource, [plain, made]) == made


def test_edit_of_an_old_resource_is_not_a_creation():
    resource = res(VM_TYPE, "vm-1", created_at=T0 - timedelta(days=200))
    assert creation_of(resource, [write(resource, minutes=-7 * 24 * 60, created=False)]) is None


def test_no_created_time_takes_the_latest_created_write():
    resource = res(VM_TYPE, "vm-1", created_at=None)
    old = write(resource, "student-a@school.example", minutes=-20 * 24 * 60)
    new = write(resource, "student-b@school.example", minutes=-24 * 60)
    assert creation_of(resource, [old, new]) == new
    assert creation_of(resource, [write(resource, created=False)]) is None


def test_group_creator_is_the_latest_created_write():
    group = grp("rg-a")
    old = write(group.key, "student-a@school.example", minutes=-20 * 24 * 60)
    new = write(group.key, "student-b@school.example", minutes=-24 * 60)
    plain = write(group.key, "student-c@school.example", created=False)
    assert group_creation([old, new, plain]) == new
    assert group_creation([plain]) is None
    assert group_creation([]) is None
