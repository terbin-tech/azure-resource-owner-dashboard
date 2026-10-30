from datetime import timedelta

import pytest

from dashboard.events import LookupResult
from dashboard.model import DISK, VM
from dashboard.pipeline import build_report
from dashboard.verify import name_hint, render_verify
from tests.helpers import NOW, T0, make_row, make_scan, res, write

ROSTER_B = {"student-b": ("student-b", "cohort-x")}


def test_name_hint_matches_whole_names_only():
    people = ["student-a", "abc"]
    assert name_hint(make_row(res(VM, "student-a-web")), people) == "student-a"
    assert name_hint(make_row(res(VM, "vm-1", tags={"Name": "x-student-a"})), people) == "student-a"
    assert name_hint(make_row(res(VM, "STUDENT-A-web")), people) == "student-a"
    assert name_hint(make_row(res(VM, "student-ab")), people) is None
    assert name_hint(make_row(res(VM, "abc")), people) is None  # too short


@pytest.mark.parametrize("key", ["name", "NAME"])
def test_name_hint_reads_the_name_tag_in_any_case(key):
    row = make_row(res(VM, "vm-1", tags={key: "x-student-a"}))
    assert name_hint(row, ["student-a"]) == "student-a"


def test_report_sections():
    agree = res(VM, "student-a-web")
    disagree = res(VM, "student-b-db")
    data = make_scan(resources=[agree, disagree], writes=[write(agree), write(disagree)])
    data.lookups[0].duration_s = 12.5
    text = render_verify(data, build_report(data, ROSTER_B), ROSTER_B)

    assert text.startswith("# Verification report\n")
    assert "## 1. Activity Log lookup duration" in text
    assert "| subscription | duration s | writes | failed |" in text
    assert "| Course subscription | 12.5 | 2 | no |" in text
    assert "Agreements: 1" in text
    assert f"| {disagree.id} | Course subscription | student-b | student-a | event |" in text
    assert "Local only: never commit this file." in text
    assert "## 6. Incomplete" in text
    assert "- none" in text[text.index("## 6. Incomplete") : text.index("## 7. Mapping")]
    assert "## 7. Mapping" in text
    assert "- users with resources but no cohort: 1" in text  # student-a, not in the roster
    assert "None" not in text
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings == [
        "# Verification report",
        "## 1. Activity Log lookup duration",
        "## 2. Creation events",
        "## 3. Owner method per type",
        "## 4. Name hint vs owner",
        "## 5. Sign-in names on more than one domain",
        "## 6. Incomplete",
        "## 7. Mapping",
        "## 8. Spot check (manual)",
    ]


def test_duration_is_shown_with_one_decimal():
    data = make_scan(writes=[])
    data.lookups[0].duration_s = 0.123456
    assert "| Course subscription | 0.1 | 0 | no |" in render_verify(
        data, build_report(data, None), None
    )


def test_failed_lookup_counts_its_groups():
    data = make_scan(failed=True)
    text = render_verify(data, build_report(data, None), None)
    assert "| Course subscription | 0.0 | 0 | 1 of 1 groups |" in text
    assert "- Activity Log in Course subscription, 1 of 1 groups (AuthorizationFailed)" in text


def test_lookup_duration_sums_the_groups_of_a_subscription():
    data = make_scan(writes=[write(res(VM, "vm-1")), write(res(VM, "vm-2"))])
    first = data.lookups[0]
    first.duration_s = 10.0
    data.lookups.append(
        LookupResult(first.subscription, "rg-b", [write(res(VM, "vm-3", "rg-b"))], 2.5)
    )
    text = render_verify(data, build_report(data, None), None)
    assert "| Course subscription | 12.5 | 3 | no |" in text
    assert "- slowest group: 10.0 s" in text

    data.lookups[1].error = "Throttled"
    text = render_verify(data, build_report(data, None), None)
    assert "| Course subscription | 12.5 | 3 | 1 of 2 groups |" in text


def test_no_lookups_have_no_slowest_group():
    data = make_scan()
    data.lookups = []
    text = render_verify(data, build_report(data, None), None)
    assert "- slowest group: none" in text


def test_creation_event_counts():
    created = res(VM, "vm-1")
    fallback = res(VM, "vm-2")
    late = res(VM, "vm-3")
    data = make_scan(
        resources=[created, fallback, late],
        writes=[
            write(created),
            write(fallback, created=False),  # a long-running create answers 202
            write(late, minutes=180),  # three hours after createdTime: not its creation
        ],
    )
    text = render_verify(data, build_report(data, None), None)
    assert "- owners from a Created write: 1" in text
    assert "- owners from the earliest-write fallback: 1" in text
    assert (
        "- resources with writes on their ID but no creation within 15 minutes of createdTime: 1"
        in text
    )
    # the resource is listed under the count: ID, subscription, created time, first write, gap
    assert (
        f"createdTime: 1\n  - {late.id} | Course subscription | created {T0.isoformat()}"
        f" | first write {(T0 + timedelta(minutes=180)).isoformat()} | 180 min\n"
    ) in text


def test_resources_outside_the_creation_window_are_listed_by_id():
    b, a = res(VM, "vm-b"), res(VM, "vm-a")
    data = make_scan(
        resources=[b, a],
        writes=[
            write(b, minutes=200),
            write(b, minutes=100),  # the earliest write counts
            write(a, minutes=-60),
        ],
    )
    text = render_verify(data, build_report(data, None), None)
    section = text[text.index("## 2.") : text.index("## 3.")]
    lines = [line for line in section.splitlines() if line.startswith("  - ")]
    assert [line.split(" | ")[0] for line in lines] == [f"  - {a.id}", f"  - {b.id}"]
    assert lines[0].endswith(f"first write {(T0 - timedelta(minutes=60)).isoformat()} | -60 min")
    assert lines[1].endswith(f"first write {(T0 + timedelta(minutes=100)).isoformat()} | 100 min")


def test_no_list_lines_when_every_resource_has_its_creation():
    vm = res(VM, "vm-1")
    data = make_scan(resources=[vm], writes=[write(vm)])
    text = render_verify(data, build_report(data, None), None)
    section = text[text.index("## 2.") : text.index("## 3.")]
    assert "  - " not in section
    assert "createdTime: 0\n\n## 3." in text


def test_owner_method_per_type():
    vm = res(VM, "vm-1")
    disks = [res(DISK, "disk-1"), res(DISK, "disk-2")]
    data = make_scan(resources=[vm, *disks], writes=[write(vm), write(disks[0])])
    text = render_verify(data, build_report(data, None), None)
    assert (
        "| type | event | parent | resource-group | orphaned | lookup-failed | unknown | azure"
        " | dashboard |"
    ) in text
    assert "| microsoft.compute/virtualmachines | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |" in text
    assert "| microsoft.compute/disks | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |" in text


def test_people_are_owners_plus_roster_usernames():
    db = res(VM, "student-c-db")
    data = make_scan(resources=[db], writes=[write(db)])
    roster = {"student-c": ("student-c", "cohort-y")}

    with_roster = render_verify(data, build_report(data, roster), roster)
    assert f"| {db.id} | Course subscription | student-c | student-a | event |" in with_roster

    without = render_verify(data, build_report(data, None), None)
    assert "student-c | student-a" not in without
    assert "Agreements: 0" in without


def test_prefix_on_two_domains_is_listed():
    a, b = res(VM, "vm-1"), res(VM, "vm-2")
    two = make_scan(
        resources=[a, b],
        writes=[
            write(a, "student-a@school.example"),
            write(b, "Student-A@other.example"),
            write(b, "student-b@school.example", minutes=1),
        ],
    )
    text = render_verify(two, build_report(two, None), None)
    section = text[text.index("## 5.") : text.index("## 6.")]
    assert "- student-a: 2 domains" in section
    assert "student-b" not in section

    one = make_scan(
        resources=[a, b],
        writes=[write(a, "student-a@school.example"), write(b, "Student-A@school.example")],
    )
    section = render_verify(one, build_report(one, None), None)
    section = section[section.index("## 5.") : section.index("## 6.")]
    assert "- none" in section


def test_apps_are_not_counted_as_domains():
    a = res(VM, "vm-1")
    data = make_scan(resources=[a], writes=[write(a, "00000000-0000-0000-0000-000000000009")])
    text = render_verify(data, build_report(data, None), None)
    section = text[text.index("## 5.") : text.index("## 6.")]
    assert "- none" in section


def test_spot_check_lists_the_oldest_first():
    vols = [
        res(DISK, f"disk-a{i}", created_at=NOW - timedelta(days=days))
        for i, days in ((1, 2), (2, 30), (3, 9))
    ]
    data = make_scan(resources=vols, writes=[write(v, base=v.created_at) for v in vols])
    text = render_verify(data, build_report(data, None), None)
    spot = text[text.index("## 8. Spot check") :]
    assert spot.index("disk-a2 ") < spot.index("disk-a3 ") < spot.index("disk-a1 ")  # 30, 9, 2 days
    assert "- roster: not loaded (absent or denied)" in text
