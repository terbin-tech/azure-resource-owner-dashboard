import logging

import pytest
from azure.mgmt.resourcegraph.models import QueryResponse

from dashboard.clients import WriteRefused
from dashboard.inventory import GROUPS_QUERY, RESOURCES_QUERY, Inventory, collect
from dashboard.model import VM, Group, Incomplete, Subscription
from tests.fakes import (
    FakeClients,
    FakeGraph,
    FakeResourceClient,
    FakeSubscriptionClient,
    arm_row,
    graph_page,
    http_error,
    sdk_subscription,
)
from tests.helpers import SUB, SUB2, T0, rid

VM_TYPE = "Microsoft.Compute/virtualMachines"


def graph_row(resource_id: str, **extra) -> dict:
    row = {
        "id": resource_id,
        "name": resource_id.rsplit("/", 1)[-1],
        "type": VM,
        "managedBy": "",
        "tags": None,
        "properties": None,
    }
    return row | extra


def group_row(name: str, sub: str = SUB, tags=None) -> dict:
    return {
        "id": f"/subscriptions/{sub}/resourcegroups/{name}",
        "name": name,
        "tags": tags,
    }


def clients(
    arm: dict[str, FakeResourceClient] | None = None,
    resource_pages: list | None = None,
    group_pages: list | None = None,
    subs: FakeSubscriptionClient | None = None,
) -> FakeClients:
    arm = {SUB: FakeResourceClient([])} if arm is None else arm
    graph = FakeGraph(
        {
            RESOURCES_QUERY: resource_pages if resource_pages is not None else [graph_page([])],
            GROUPS_QUERY: group_pages if group_pages is not None else [graph_page([])],
        }
    )
    subs = subs or FakeSubscriptionClient([sdk_subscription(sub, f"sub {sub[-1]}") for sub in arm])
    return FakeClients(subs, graph, arm)


def test_subscriptions_come_from_the_subscription_list():
    subs = FakeSubscriptionClient([sdk_subscription(SUB.upper(), "Course subscription")])
    inv = collect(clients({SUB: FakeResourceClient([])}, subs=subs))
    assert inv.scanned == [Subscription(SUB, "Course subscription")]


def test_union_of_graph_and_arm_rows():
    vm_arm = rid("RG-A", VM_TYPE, "vm1")
    vm_graph = vm_arm.casefold()
    only_graph = rid("rg-a", "microsoft.compute/disks", "disk1")
    only_arm = rid("RG-B", "Microsoft.Network/publicIPAddresses", "ip1", SUB2)
    arm1 = FakeResourceClient([arm_row(vm_arm, tags={"stale": "arm"})])
    arm2 = FakeResourceClient([arm_row(only_arm, managed_by="/x", tags={"owner": "student-a"})])
    graph_rows = [
        graph_row(
            vm_graph,
            tags={"owner": "student-a"},
            properties={
                "extended": {"instanceView": {"powerState": {"code": "PowerState/running"}}}
            },
        ),
        graph_row(only_graph, type="microsoft.compute/disks", name="disk1"),
    ]
    c = clients(
        {SUB: arm1, SUB2: arm2},
        resource_pages=[graph_page(graph_rows)],
    )
    by_id = {r.id: r for r in collect(c).resources}
    assert len(by_id) == 3
    vm = by_id[vm_graph]
    assert vm.created_at == T0
    assert vm.tags == {"owner": "student-a"}
    assert vm.type == VM
    assert vm.name == "vm1"
    assert vm.power_state == "PowerState/running"
    assert by_id[only_graph].created_at is None
    ip = by_id[only_arm]
    assert (ip.subscription, ip.group, ip.properties) == (SUB2, "RG-B", {})
    assert (ip.type, ip.name, ip.created_at, ip.managed_by, ip.tags) == (
        "microsoft.network/publicipaddresses",
        "ip1",
        T0,
        "/x",
        {"owner": "student-a"},
    )
    assert arm1.expands == ["createdTime"]
    assert arm2.expands == ["createdTime"]


def test_naive_created_time_is_read_as_utc():
    vm = rid("rg-a", VM_TYPE, "vm1")
    c = clients({SUB: FakeResourceClient([arm_row(vm, created=T0.replace(tzinfo=None))])})
    (resource,) = collect(c).resources
    assert resource.created_at == T0
    assert resource.created_at.tzinfo is not None


def test_unparseable_created_time_counts_as_missing():
    # The SDK leaves a createdTime it cannot parse as the raw string.
    row = arm_row(rid("rg-a", VM_TYPE, "vm1"))
    row["createdTime"] = "not a date"
    inv = collect(clients({SUB: FakeResourceClient([row])}))
    (resource,) = inv.resources
    assert resource.created_at is None
    assert inv.arm_failed == set()
    assert inv.incomplete == []


def test_graph_pages_follow_the_skip_token():
    a = rid("rg-a", "microsoft.compute/disks", "d1")
    b = rid("rg-a", "microsoft.compute/disks", "d2")
    c = clients(
        resource_pages=[graph_page([graph_row(a)], "t1"), graph_page([graph_row(b)])],
    )
    inv = collect(c)
    assert {r.id for r in inv.resources} == {a, b}
    first, second, _groups = c.graph().requests
    assert first.options.skip_token is None
    assert second.options.skip_token == "t1"
    for request in c.graph().requests:
        assert request.options.top == 1000
        assert request.options.result_format == "objectArray"


def test_one_resources_query_and_one_groups_query_for_all_subscriptions():
    c = clients({SUB: FakeResourceClient([]), SUB2: FakeResourceClient([])})
    collect(c)
    requests = c.graph().requests
    assert [r.query for r in requests] == [RESOURCES_QUERY, GROUPS_QUERY]
    assert [r.subscriptions for r in requests] == [[SUB, SUB2], [SUB, SUB2]]


def test_unreadable_subscription_is_not_scanned():
    vm = rid("rg-a", VM_TYPE, "vm1")
    c = clients(
        {
            SUB: FakeResourceClient([arm_row(vm)]),
            SUB2: FakeResourceClient([], http_error("AuthorizationFailed")),
        },
        subs=FakeSubscriptionClient(
            [sdk_subscription(SUB, "Course subscription"), sdk_subscription(SUB2, "Second course")]
        ),
    )
    inv = collect(c)
    assert inv.not_scanned == ["Second course"]
    assert [s.id for s in inv.scanned] == [SUB]
    assert [r.subscriptions for r in c.graph().requests] == [[SUB], [SUB]]
    assert inv.incomplete == []
    assert [r.id for r in inv.resources] == [vm]


def test_error_after_the_first_page_drops_the_partial_list():
    partial = rid("rg-a", "microsoft.compute/disks", "partial")
    kept = rid("rg-a", "microsoft.compute/disks", "kept")
    c = clients(
        {
            SUB: FakeResourceClient(
                [arm_row(partial)], http_error("InternalServerError", 500), after=1
            )
        },
        resource_pages=[graph_page([graph_row(kept)])],
        subs=FakeSubscriptionClient([sdk_subscription(SUB, "Course subscription")]),
    )
    inv = collect(c)
    assert [r.id for r in inv.resources] == [kept]
    assert inv.incomplete == [
        Incomplete("resource list in Course subscription", "InternalServerError")
    ]
    assert inv.arm_failed == {SUB}
    assert [s.id for s in inv.scanned] == [SUB]


def test_graph_failure_is_incomplete_and_keeps_the_arm_rows():
    vm = rid("rg-a", VM_TYPE, "vm1")
    c = clients(
        {SUB: FakeResourceClient([arm_row(vm)])},
        resource_pages=[http_error("RateLimiting", 429)],
    )
    inv = collect(c)
    assert inv.incomplete == [Incomplete("Resource Graph", "RateLimiting")]
    assert inv.graph_failed is True
    assert [r.id for r in inv.resources] == [vm]


def counted_page(rows, total, skip_token=None, truncated="false"):
    """A Resource Graph page whose totalRecords is set apart from its rows."""
    body = {"totalRecords": total, "count": len(rows), "resultTruncated": truncated, "data": rows}
    if skip_token is not None:
        body["$skipToken"] = skip_token
    return QueryResponse.deserialize(body)


def test_truncated_flag_alone_is_a_complete_result():
    # resultTruncated is also true "when there are less resources available than a query is
    # requesting": a small subscription answers top=1000 with one full page.
    disk = rid("rg-a", "microsoft.compute/disks", "d1")
    page = counted_page([graph_row(disk)], 1, truncated="true")
    inv = collect(clients(resource_pages=[page]))
    assert inv.incomplete == []
    assert inv.graph_failed is False
    assert [r.id for r in inv.resources] == [disk]


def test_fewer_rows_than_total_records_is_incomplete():
    disk = graph_row(rid("rg-a", "microsoft.compute/disks", "d1"))
    vm = rid("rg-a", VM_TYPE, "vm1")
    inv = collect(
        clients(
            {SUB: FakeResourceClient([arm_row(vm)])},
            resource_pages=[counted_page([disk], 2)],
        )
    )
    assert inv.incomplete == [Incomplete("Resource Graph", "ValueError")]
    assert inv.graph_failed is True
    assert [r.id for r in inv.resources] == [vm]


def test_pages_that_sum_to_total_records_are_complete():
    a = rid("rg-a", "microsoft.compute/disks", "d1")
    b = rid("rg-a", "microsoft.compute/disks", "d2")
    pages = [counted_page([graph_row(a)], 2, "t1"), counted_page([graph_row(b)], 2)]
    inv = collect(clients(resource_pages=pages))
    assert inv.incomplete == []
    assert {r.id for r in inv.resources} == {a, b}


def test_groups_query_failure_is_incomplete():
    c = clients(group_pages=[http_error("RateLimiting", 429)])
    inv = collect(c)
    assert inv.incomplete == [Incomplete("Resource Graph resource groups", "RateLimiting")]
    assert inv.graph_failed is True
    assert inv.groups == {}


def test_groups_come_from_resourcecontainers():
    row = group_row("RG-A", SUB.upper())
    row["id"] = f"/subscriptions/{SUB.upper()}/resourceGroups/RG-A"
    inv = collect(clients(group_pages=[graph_page([row])]))
    key = row["id"].casefold()
    assert inv.groups == {key: Group(row["id"], "RG-A", SUB, {})}
    assert inv.groups[key].key == key


def test_subscription_list_failure_is_incomplete():
    c = clients(subs=FakeSubscriptionClient([], http_error(None, 503)))
    assert collect(c) == Inventory([], [], [], {}, [Incomplete("subscription list", "HTTP 503")])
    assert c.graph().requests == []


def test_a_refused_write_is_never_swallowed():
    c = clients({SUB: FakeResourceClient([], WriteRefused("PUT refused"))})
    with pytest.raises(WriteRefused):
        collect(c)


def test_failures_are_logged_with_the_code_only(caplog):
    message = "distinctive-error-text-7"
    c = clients(
        {SUB: FakeResourceClient([], http_error("InternalServerError", 500, message))},
        subs=FakeSubscriptionClient([sdk_subscription(SUB, "Course subscription")]),
    )
    with caplog.at_level(logging.DEBUG):
        collect(c)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        "resource list in Course subscription failed: InternalServerError"
    ]
    # The error message must reach neither the log text nor an attached traceback.
    assert all(r.exc_info is None and message not in r.getMessage() for r in caplog.records)


def test_every_subscription_unreadable_sends_no_graph_request():
    c = clients(
        {
            SUB: FakeResourceClient([], http_error("AuthorizationFailed")),
            SUB2: FakeResourceClient([], http_error("AuthorizationFailed")),
        },
        subs=FakeSubscriptionClient(
            [sdk_subscription(SUB, "Course subscription"), sdk_subscription(SUB2, "Second course")]
        ),
    )
    inv = collect(c)
    assert inv.not_scanned == ["Course subscription", "Second course"]
    assert inv.scanned == []
    assert c.graph().requests == []
    assert inv.incomplete == []


def test_malformed_arm_row_makes_the_subscription_list_incomplete(caplog):
    no_group = f"/subscriptions/{SUB}/providers/Microsoft.X/y/z"
    kept = rid("rg-a", "microsoft.compute/disks", "kept")
    c = clients(
        {SUB: FakeResourceClient([arm_row(rid("rg-a", VM_TYPE, "vm1")), arm_row(no_group)])},
        resource_pages=[graph_page([graph_row(kept)])],
        subs=FakeSubscriptionClient([sdk_subscription(SUB, "Course subscription")]),
    )
    with caplog.at_level(logging.DEBUG):
        inv = collect(c)
    assert inv.incomplete == [Incomplete("resource list in Course subscription", "ValueError")]
    assert inv.arm_failed == {SUB}
    assert [r.id for r in inv.resources] == [kept]  # the ARM rows are dropped whole
    assert all(no_group not in r.getMessage() and r.exc_info is None for r in caplog.records)


def test_arm_row_with_a_null_field_makes_the_subscription_list_incomplete():
    row = arm_row(rid("rg-a", VM_TYPE, "vm1"))
    row["type"] = None
    inv = collect(clients({SUB: FakeResourceClient([row])}))
    assert inv.incomplete == [Incomplete("resource list in sub 1", "AttributeError")]
    assert inv.arm_failed == {SUB}


def _collect_with_bad_graph_row(bad: dict, *, group: bool = False) -> tuple[Inventory, str]:
    """Collect one good ARM row plus one malformed Resource Graph row (resource or group)."""
    vm = rid("rg-a", VM_TYPE, "vm1")
    pages = {"group_pages" if group else "resource_pages": [graph_page([bad])]}
    return collect(clients({SUB: FakeResourceClient([arm_row(vm)])}, **pages)), vm


def test_graph_row_missing_a_key_is_incomplete_and_keeps_the_arm_rows():
    bad = graph_row(rid("rg-a", "microsoft.compute/disks", "d1"))
    del bad["type"]
    inv, vm = _collect_with_bad_graph_row(bad)
    assert inv.incomplete == [Incomplete("Resource Graph", "KeyError")]
    assert inv.graph_failed is True
    assert [r.id for r in inv.resources] == [vm]


@pytest.mark.parametrize(
    ("resource_id", "error"),
    [
        (None, "TypeError"),
        (f"/subscriptions/{SUB}/providers/Microsoft.Compute/disks/d1", "ValueError"),  # no group
        (
            f"/subscriptions/{SUB}/resourceGroups//providers/Microsoft.Compute/disks/d1",
            "ValueError",
        ),
    ],
    ids=["null id", "id without a group", "id with an empty group"],
)
def test_graph_row_with_a_bad_id_is_incomplete_and_keeps_the_arm_rows(resource_id, error):
    bad = graph_row(rid("rg-a", "microsoft.compute/disks", "d1")) | {"id": resource_id}
    inv, vm = _collect_with_bad_graph_row(bad)
    assert inv.incomplete == [Incomplete("Resource Graph", error)]
    assert inv.graph_failed is True
    assert [r.id for r in inv.resources] == [vm]


def test_graph_row_takes_its_subscription_and_group_from_the_id():
    # The query no longer projects these columns; were they present and empty, they would be
    # ignored: the subscription and group, as for an ARM row, come from the ID.
    disk = rid("RG-B", "microsoft.compute/disks", "d1")
    row = graph_row(disk, resourceGroup="", subscriptionId=None)
    inv = collect(clients(resource_pages=[graph_page([row])]))
    assert inv.incomplete == []
    assert inv.graph_failed is False
    (found,) = inv.resources
    assert (found.subscription, found.group) == (SUB, "RG-B")


def test_graph_group_row_missing_a_key_is_incomplete():
    row = group_row("rg-a")
    del row["name"]
    inv, vm = _collect_with_bad_graph_row(row, group=True)
    assert inv.incomplete == [Incomplete("Resource Graph resource groups", "KeyError")]
    assert inv.graph_failed is True
    assert inv.groups == {}
    assert [r.id for r in inv.resources] == [vm]


@pytest.mark.parametrize(
    ("group_id", "error"),
    [(None, "TypeError"), (f"/subscriptions/{SUB}/resourceGroups/", "ValueError")],
    ids=["null id", "id with an empty group"],
)
def test_graph_group_row_with_a_bad_id_is_incomplete(group_id, error):
    row = group_row("rg-a") | {"id": group_id}
    inv, vm = _collect_with_bad_graph_row(row, group=True)
    assert inv.incomplete == [Incomplete("Resource Graph resource groups", error)]
    assert inv.graph_failed is True
    assert inv.groups == {}
    assert [r.id for r in inv.resources] == [vm]


def test_graph_group_row_takes_its_subscription_from_the_id():
    row = group_row("rg-a") | {"subscriptionId": None}
    inv = collect(clients(group_pages=[graph_page([row])]))
    assert inv.incomplete == []
    assert [g.subscription for g in inv.groups.values()] == [SUB]


class _RefusingRow:
    """An ARM row whose conversion hits a refused write."""

    id = rid("rg-a", VM_TYPE, "vm1")

    @property
    def type(self):
        raise WriteRefused("PUT refused")


def test_a_refused_write_while_converting_a_row_propagates():
    with pytest.raises(WriteRefused):
        collect(clients({SUB: FakeResourceClient([_RefusingRow()])}))
