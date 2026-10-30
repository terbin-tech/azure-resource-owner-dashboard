import logging
from dataclasses import replace
from datetime import timedelta

from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError

import dashboard.pipeline as pipeline
from dashboard.clients import ARM_SCOPE
from dashboard.config import Config
from dashboard.events import LookupResult, activity_filter, window_start
from dashboard.inventory import GROUPS_QUERY, RESOURCES_QUERY
from dashboard.model import AKS, DISK, NETWORK_WATCHER, NIC, PUBLIC_IP, VM, Incomplete, Subscription
from dashboard.render import DELETE_GROUP_TEXT, SHARED_GROUP_TEXT, help_line
from tests.fakes import (
    FakeClients,
    FakeCredential,
    FakeGraph,
    FakeMonitor,
    FakeResourceClient,
    FakeSubscriptionClient,
    arm_row,
    graph_page,
    http_error,
    sdk_subscription,
)
from tests.helpers import NOW, SUB, SUB2, grp, make_scan, res, rid, write

APP = "00000000-0000-0000-0000-000000000009"
NODE_GROUP = "MC_rg-a_aks-1_eastus"


def nic_on(vm, name="nic-1"):
    return res(NIC, name, properties={"virtualMachine": {"id": vm.id}})


def ip_on(nic, name="ip-1", group="rg-a"):
    return res(
        PUBLIC_IP,
        name,
        group,
        properties={"ipConfiguration": {"id": nic.id + "/ipConfigurations/ipconfig1"}},
    )


def by_id(report):
    return {row.resource.id: row for row in report.rows}


def test_scan_runs_one_lookup_per_resource_group():
    monitors = {SUB: FakeMonitor([]), SUB2: FakeMonitor([])}
    empty = {
        "id": f"/subscriptions/{SUB}/resourceGroups/rg-empty",
        "name": "rg-empty",
        "tags": None,
    }
    clients = FakeClients(
        FakeSubscriptionClient(
            [sdk_subscription(SUB, "Course subscription"), sdk_subscription(SUB2, "Second course")]
        ),
        FakeGraph({RESOURCES_QUERY: [graph_page([])], GROUPS_QUERY: [graph_page([empty])]}),
        {
            SUB: FakeResourceClient(
                [
                    arm_row(rid("rg-b", "Microsoft.Compute/virtualMachines", "vm-2")),
                    arm_row(rid("rg-a", "Microsoft.Compute/virtualMachines", "vm-1")),
                    arm_row(rid("RG-A", "Microsoft.Compute/disks", "disk-1")),
                ]
            ),
            SUB2: FakeResourceClient([], http_error("AuthorizationFailed")),
        },
        monitors,
    )
    data = pipeline.scan(clients, Config(), NOW)

    start = window_start(NOW, 90)
    # rg-a and RG-A are one group; the empty group is on no page, so it is not queried.
    assert monitors[SUB].activity_logs.filters == [
        activity_filter(start, NOW, "rg-a"),
        activity_filter(start, NOW, "rg-b"),
    ]
    assert monitors[SUB2].activity_logs.filters == []
    course = Subscription(SUB, "Course subscription")
    assert [(lk.subscription, lk.group) for lk in data.lookups] == [
        (course, "rg-a"),
        (course, "rg-b"),
    ]
    assert len(data.inventory.groups) == 1
    assert data.inventory.not_scanned == ["Second course"]
    assert (data.generated_at, data.lookback_days, data.dashboard_group) == (
        NOW,
        90,
        "resource-owner-dashboard",
    )


def test_scan_skips_a_subscription_without_resources():
    monitor = FakeMonitor([])
    clients = FakeClients(
        FakeSubscriptionClient([sdk_subscription(SUB, "Course subscription")]),
        FakeGraph({RESOURCES_QUERY: [graph_page([])], GROUPS_QUERY: [graph_page([])]}),
        {SUB: FakeResourceClient([])},
        {SUB: monitor},
    )
    assert pipeline.scan(clients, Config(), NOW).lookups == []
    assert monitor.activity_logs.filters == []


def test_check_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    ok = FakeCredential()
    assert pipeline.check_credentials(ok) is None
    assert ok.scopes == (ARM_SCOPE,)
    secret = "student-a@school.example"
    assert (
        pipeline.check_credentials(FakeCredential(CredentialUnavailableError(secret)))
        == "CredentialUnavailableError"
    )
    assert (
        pipeline.check_credentials(FakeCredential(ClientAuthenticationError(secret)))
        == "ClientAuthenticationError"
    )
    assert secret not in caplog.text


def test_build_report_rows_cards_and_sections():
    vm = res(VM, "vm-1")
    disk = res(DISK, "disk-1", "rg-b")
    data = make_scan(resources=[vm, disk], writes=[write(vm), write(disk, APP)])
    roster = {"student-a": ("student-a", "cohort-x"), "student-b": ("student-b", "cohort-x")}
    report = pipeline.build_report(data, roster)

    assert [r.resource.id for r in report.rows] == [vm.id, disk.id]
    assert [c.name for c in report.cards if c.rows] == ["student-a"]
    # student-b is in the roster with nothing left in the subscription.
    assert [c.name for c in report.cards if not c.rows] == ["student-b"]
    assert report.cohorts == ["cohort-x"]
    assert [r.resource.id for r in report.unknown] == [disk.id]
    assert report.orphaned == report.made_by_azure == report.dashboard == []
    assert report.unmapped_users == 0
    assert report.roster_loaded is True


def test_age_comes_from_created_at_else_the_creation_write():
    dated = res(VM, "vm-dated", created_at=NOW - timedelta(days=6))
    undated = res(VM, "vm-undated", created_at=None)
    nic = replace(nic_on(undated), created_at=None)  # rule 2 owner: its age is not the VM's
    unknown = res(VM, "vm-unknown", created_at=None)
    data = make_scan(
        resources=[dated, undated, nic, unknown],
        writes=[write(undated, base=NOW - timedelta(days=3))],
    )
    rows = by_id(pipeline.build_report(data, roster=None))

    assert rows[dated.id].age_days == 6
    assert rows[undated.id].age_days == 3
    assert rows[nic.id].age_days is None
    assert rows[unknown.id].age_days is None


def test_attached_parts():
    vm = res(VM, "vm-1")
    disk = res(DISK, "disk-1", managed_by=vm.id)
    loose_disk = res(DISK, "disk-2")
    nic = nic_on(vm)
    ip = ip_on(nic)
    orphan_nic = res(NIC, "nic-2", properties={"virtualMachine": {"id": rid("rg-a", VM, "gone")}})
    orphan_ip = ip_on(orphan_nic, "ip-2")
    data = make_scan(resources=[vm, disk, loose_disk, nic, ip, orphan_nic, orphan_ip])
    rows = by_id(pipeline.build_report(data, roster=None))

    assert {i for i, row in rows.items() if row.attached} == {disk.id, nic.id, ip.id}


def test_node_group_contents_are_not_attached_but_a_scale_set_disk_is():
    cluster = res(AKS, "aks-1", properties={"nodeResourceGroup": NODE_GROUP})
    instance = rid(
        NODE_GROUP, "Microsoft.Compute/virtualMachineScaleSets/vmss-1/virtualMachines", "0"
    )
    os_disk = res(DISK, "os-disk", NODE_GROUP, managed_by=instance)
    pvc = res(DISK, "pvc-1", NODE_GROUP)
    lb_nic = res(NIC, "nic-lb", NODE_GROUP)
    lb_ip = ip_on(lb_nic, "ip-lb", NODE_GROUP)
    data = make_scan(
        resources=[cluster, os_disk, pvc, lb_nic, lb_ip],
        writes=[write(cluster)],
    )
    rows = by_id(pipeline.build_report(data, roster=None))

    assert {i for i, row in rows.items() if row.attached} == {os_disk.id}
    # They still take the cluster's owner (rule 2), so they keep their own age mark.
    assert {rows[r.id].owner.name for r in (pvc, lb_nic, lb_ip)} == {"student-a"}


def test_vm_public_ip_flag():
    with_ip = res(VM, "vm-1")
    nic = nic_on(with_ip)
    without_ip = res(VM, "vm-2")
    other_nic = nic_on(without_ip, "nic-2")
    data = make_scan(resources=[with_ip, nic, ip_on(nic), without_ip, other_nic])
    rows = by_id(pipeline.build_report(data, roster=None))

    assert rows[with_ip.id].public_ip is True
    assert rows[without_ip.id].public_ip is False


def test_quiet_sections_never_on_cards():
    watcher = res(NETWORK_WATCHER, "NetworkWatcher_eastus", "NetworkWatcherRG")
    function = res("Microsoft.Web/sites", "dashboard-fn", "resource-owner-dashboard")
    data = make_scan(resources=[watcher, function], writes=[write(function)])
    report = pipeline.build_report(data, roster=None)

    assert [r.resource.id for r in report.made_by_azure] == [watcher.id]
    assert [r.resource.id for r in report.dashboard] == [function.id]
    assert report.unknown == report.orphaned == report.cards == []


def test_group_with_an_unowned_resource_is_not_sole_owned():
    shared_vm = res(VM, "vm-1", "rg-a")
    unowned = res(DISK, "disk-1", "rg-a")
    own_vm = res(VM, "vm-2", "rg-b")
    watcher = res(NETWORK_WATCHER, "NetworkWatcher_eastus", "rg-b")
    # The Activity Log lookup of SUB2's rg-c failed, so its disk is lookup-failed.
    other_vm = res(VM, "vm-3", "rg-c", sub=SUB2)
    unread = res(DISK, "disk-3", "rg-c", sub=SUB2)
    dash = res("Microsoft.Web/sites", "dashboard-fn", "resource-owner-dashboard")
    data = make_scan(
        resources=[shared_vm, unowned, own_vm, watcher, other_vm, unread, dash],
        writes=[write(shared_vm), write(own_vm), write(other_vm), write(dash)],
    )
    data.lookups.append(
        LookupResult(Subscription(SUB2, "Second course"), "rg-c", error="Throttled")
    )
    report = pipeline.build_report(data, roster=None)
    methods = {r.resource.id: r.owner.method for r in report.rows}
    assert (methods[unowned.id], methods[unread.id], methods[dash.id]) == (
        "unknown",
        "lookup-failed",
        "dashboard",
    )
    owners = report.group_owners

    assert owners[shared_vm.group_key] == frozenset({"student-a", None})
    assert owners[other_vm.group_key] == frozenset({"student-a", None})
    # Made by Azure rows go with the group, and the dashboard's group has no owners.
    assert owners[own_vm.group_key] == frozenset({"student-a"})
    assert dash.group_key not in owners


def test_subscription_keys_line_up_whatever_the_case():
    # Azure may write a subscription ID in upper case; every key is casefolded.
    sub = "-".join(["1a2b3c4d", "1111", "2222", "3333", "444455556666"])
    upper = sub.upper()
    vm_id = rid("RG-A", "Microsoft.Compute/virtualMachines", "vm-1", upper)

    def run(arm, graph_rows, monitor):
        clients = FakeClients(
            FakeSubscriptionClient([sdk_subscription(upper, "Course subscription")]),
            FakeGraph(
                {
                    RESOURCES_QUERY: [graph_page(graph_rows)],
                    GROUPS_QUERY: [
                        graph_page(
                            [
                                {
                                    "id": f"/subscriptions/{upper}/resourceGroups/RG-A",
                                    "name": "RG-A",
                                    "tags": None,
                                }
                            ]
                        )
                    ],
                }
            ),
            {sub: arm},
            {sub: monitor},
        )
        return pipeline.build_report(pipeline.scan(clients, Config(), NOW), roster=None)

    # The Activity Log failed: the ARM row's owner is lookup-failed.
    report = run(
        FakeResourceClient([arm_row(vm_id)]), [], FakeMonitor([], http_error("Throttled", 429))
    )
    (row,) = report.rows
    assert (row.resource.subscription, row.owner.method) == (sub, "lookup-failed")

    # The ARM list failed: the Resource Graph row's subscription and group are incomplete.
    graph_row = {
        "id": vm_id,
        "name": "vm-1",
        "type": "Microsoft.Compute/virtualMachines",
        "managedBy": "",
        "tags": None,
        "properties": None,
    }
    report = run(
        FakeResourceClient([], http_error("InternalServerError", 500)),
        [graph_row],
        FakeMonitor([]),
    )
    (row,) = report.rows
    (group,) = report.groups.values()
    assert report.incomplete_subscriptions == frozenset({sub})
    assert row.resource.subscription in report.incomplete_subscriptions
    assert group.subscription in report.incomplete_subscriptions
    assert row.resource.group_key in report.group_owners


def test_node_groups_are_reported():
    cluster = res(AKS, "aks-1", properties={"nodeResourceGroup": NODE_GROUP})
    report = pipeline.build_report(make_scan(resources=[cluster]), roster=None)
    assert report.node_groups == frozenset(
        {f"/subscriptions/{SUB}/resourcegroups/{NODE_GROUP}".casefold()}
    )


def test_tagged_group_of_a_deleted_cluster_is_not_a_node_group_but_its_rows_are_cluster_gone():
    gone = "MC_rg-a_aks-gone_eastus"
    disk = res(DISK, "pvc-1", gone)
    tagged = grp(gone, tags={"AKS-Managed-Cluster-Name": "aks-gone"})
    data = make_scan(resources=[disk], writes=[write(disk, APP)], groups=[tagged])
    report = pipeline.build_report(data, roster=None)
    assert tagged.key not in report.node_groups
    assert [r.resource for r in report.orphaned] == [disk]
    assert report.unknown == []


def test_only_a_live_clusters_node_group_is_a_node_group_beside_a_tagged_deleted_one():
    gone = "MC_rg-a_aks-gone_eastus"
    cluster = res(AKS, "aks-1", properties={"nodeResourceGroup": NODE_GROUP})
    disk = res(DISK, "pvc-1", gone)
    tagged = grp(gone, tags={"AKS-Managed-Cluster-Name": "aks-gone"})
    data = make_scan(resources=[cluster, disk], writes=[write(disk, APP)], groups=[tagged])
    report = pipeline.build_report(data, roster=None)
    assert report.node_groups == frozenset({grp(NODE_GROUP).key})


def test_help_line_for_a_group_whose_cluster_is_gone_follows_the_normal_rule():
    gone = "MC_rg-a_aks-gone_eastus"
    tagged = grp(gone, tags={"AKS-Managed-Cluster-Name": "aks-gone"})
    disk = res(DISK, "pvc-1", gone)
    theirs = res(NIC, "nic-1", gone)
    made = write(tagged.id)  # student-a created the group, so rule 3 gives them the app's disk

    def line(resources, writes, **scan):
        data = make_scan(resources=resources, writes=[made, *writes], groups=[tagged], **scan)
        report = pipeline.build_report(data, roster=None)
        (card,) = (c for c in report.cards if c.name == "student-a")
        assert disk in [r.resource for r in card.rows]
        return help_line(card, tagged.key, report)

    app_disk = write(disk, APP)
    assert line([disk], [app_disk]) == DELETE_GROUP_TEXT
    # A second resource that is another student's makes the group shared.
    other = write(theirs, "student-b@school.example")
    assert line([disk, theirs], [app_disk, other]) == SHARED_GROUP_TEXT
    # So does a subscription whose inventory may be incomplete.
    assert line([disk], [app_disk], arm_failed={SUB}) == SHARED_GROUP_TEXT


def test_graph_failure_is_passed_to_attribution():
    gone = "MC_rg-a_aks-gone_eastus"
    disk = res(DISK, "pvc-1", gone)
    tagged = grp(gone, tags={"AKS-Managed-Cluster-Name": "aks-gone"})
    data = make_scan(
        resources=[disk], writes=[write(disk, APP)], groups=[tagged], graph_failed=True
    )
    report = pipeline.build_report(data, roster=None)
    assert report.orphaned == []
    (row,) = report.unknown
    assert row.owner.detail == "cluster list unavailable (Resource Graph failed)"


def test_incomplete_subscriptions():
    def incomplete(data):
        return pipeline.build_report(data, roster=None).incomplete_subscriptions

    assert incomplete(make_scan(arm_failed={SUB})) == frozenset({SUB})
    graph_failed = make_scan(graph_failed=True)
    graph_failed.inventory.scanned.append(Subscription(SUB2, "Second course"))
    assert incomplete(graph_failed) == frozenset({SUB, SUB2})
    assert incomplete(make_scan()) == frozenset()
    # A failed Activity Log lookup already shows its rows as someone else's.
    assert incomplete(make_scan(failed=True)) == frozenset()


def test_unmapped_counts_owners_without_cohort():
    vm = res(VM, "vm-1")
    other = res(VM, "vm-2")
    data = make_scan(
        resources=[vm, other],
        writes=[write(vm), write(other, "Student-A@school.example")],
    )
    assert pipeline.build_report(data, roster=None).unmapped_users == 1
    roster = {"student-a": ("student-a", "cohort-x")}
    assert pipeline.build_report(data, roster).unmapped_users == 0


def test_roster_maps_an_owner_to_its_primary_card():
    vm = res(VM, "vm-1")
    data = make_scan(resources=[vm], writes=[write(vm, "lab-user@school.example")])
    report = pipeline.build_report(data, roster={"lab-user": ("student-a", "cohort-x")})
    (card,) = [c for c in report.cards if c.rows]
    assert (card.name, card.cohort) == ("student-a", "cohort-x")
    assert report.unmapped_users == 0


def test_owner_without_a_name_still_gets_a_no_owner_row():
    vm = res(VM, "vm-1")
    data = make_scan(resources=[vm], writes=[write(vm, "@school.example")])
    report = pipeline.build_report(data, roster=None)
    assert [r.resource.id for r in report.unknown] == [vm.id]
    assert report.cards == []


def test_incomplete_lists_inventory_then_lookups():
    vm = res(VM, "vm-1")
    data = make_scan(
        resources=[vm], failed=True, incomplete=[Incomplete("Resource Graph", "RateLimiting")]
    )
    report = pipeline.build_report(data, roster=None)
    assert report.incomplete == [
        "Resource Graph (RateLimiting)",
        "Activity Log in Course subscription, 1 of 1 groups (AuthorizationFailed)",
    ]
    assert report.rows[0].owner.method == "lookup-failed"


def test_failed_lookups_collapse_to_one_entry_per_subscription():
    data = make_scan()
    first = data.lookups[0].subscription
    second = Subscription(SUB2, "Second course")
    data.lookups = [
        LookupResult(first, "rg-a", error="Throttled"),
        LookupResult(first, "rg-b", error="AuthorizationFailed"),
        LookupResult(first, "rg-c", error="Throttled"),
        LookupResult(first, "rg-d"),
        LookupResult(second, "rg-e"),
        LookupResult(second, "rg-f", error="Throttled"),
        LookupResult(Subscription("00000000-0000-0000-0000-000000000003", "Third"), "rg-g"),
    ]
    report = pipeline.build_report(data, roster=None)
    assert report.incomplete == [  # codes distinct and sorted; a clean subscription is left out
        "Activity Log in Course subscription, 3 of 4 groups (AuthorizationFailed, Throttled)",
        "Activity Log in Second course, 1 of 2 groups (Throttled)",
    ]


def test_failed_group_reaches_attribution_as_a_group_key():
    # The lookup names its group as written; Resource.group_key is casefolded.
    lost = res(VM, "vm-1", "rg-a")
    kept = res(VM, "vm-2", "rg-b")
    writes = [
        write(lost, APP),
        write(kept, APP),
        write(f"/subscriptions/{SUB}/resourceGroups/rg-a"),
        write(f"/subscriptions/{SUB}/resourceGroups/rg-b"),
    ]
    data = make_scan(resources=[lost, kept], writes=writes)
    sub = data.lookups[0].subscription
    data.lookups = [
        LookupResult(sub, "RG-A", writes[::2], error="Throttled"),
        LookupResult(sub, "rg-b", writes[1::2]),
    ]
    report = pipeline.build_report(data, roster=None)
    rows = by_id(report)
    assert (rows[lost.id].owner.method, rows[kept.id].owner.method) == (
        "lookup-failed",
        "resource-group",
    )
    assert report.incomplete == ["Activity Log in Course subscription, 1 of 2 groups (Throttled)"]


def test_report_names_scanned_and_not_scanned_subscriptions():
    report = pipeline.build_report(make_scan(not_scanned=["Second course"]), roster=None)
    assert report.scanned == ["Course subscription"]
    assert report.not_scanned == ["Second course"]
