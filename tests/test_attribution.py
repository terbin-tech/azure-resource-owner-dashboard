from dataclasses import replace

import pytest

from dashboard.attribution import (
    METHODS,
    NODE_GROUP_TAG,
    Links,
    Owner,
    attribute,
    tagged_node_groups,
)
from dashboard.model import (
    ACTION_GROUP,
    AKS,
    DISK,
    LOAD_BALANCER,
    NAT_GATEWAY,
    NETWORK_WATCHER,
    NIC,
    PUBLIC_IP,
    SMART_DETECTOR,
    VM,
)
from tests.helpers import SUB, SUB2, grp, res, rid, write

APP = "00000000-0000-0000-0000-000000000009"


def run(
    resources,
    writes=(),
    failed=frozenset(),
    *,
    lookback_days=90,
    dashboard="dash-rg",
    tagged=frozenset(),
    graph_failed=False,
):
    by_key: dict[str, list] = {}
    for w in writes:
        by_key.setdefault(w.resource, []).append(w)
    return attribute(
        resources,
        by_key,
        set(failed),
        dashboard_group=dashboard,
        lookback_days=lookback_days,
        tagged_groups=set(tagged),
        graph_failed=graph_failed,
    )


def group_write(name, caller="student-a@school.example", sub=SUB, **kwargs):
    return write(grp(name, sub).key, caller, **kwargs)


def test_methods_are_the_documented_set():
    assert METHODS == (
        "event",
        "parent",
        "resource-group",
        "orphaned",
        "lookup-failed",
        "unknown",
        "azure",
        "dashboard",
    )
    assert Owner(None, "unknown").detail == ""


def test_dashboard_group_is_quiet():
    r = res(VM, "vm-1", "Resource-Owner-Dashboard")
    owners = run([r], [write(r)], dashboard="resource-owner-dashboard")
    assert owners[r.key].method == "dashboard"
    assert owners[r.key].name is None


def test_network_watcher_is_made_by_azure():
    r = res(NETWORK_WATCHER, "nw-1")
    owner = run([r], [write(r)])[r.key]
    assert (owner.method, owner.name) == ("azure", None)


def test_smart_detector_and_action_group_by_an_app_are_azure():
    detector = res(SMART_DETECTOR, "sd-1")
    action = res(ACTION_GROUP, "ag-1")
    owners = run([detector, action], [write(detector, APP), write(action, "Microsoft.Insights/x")])
    assert owners[detector.key].method == "azure"
    assert owners[action.key].method == "azure"
    assert owners[detector.key].name is None
    by_person = run([detector, action], [write(detector), write(action)])
    assert (by_person[detector.key].method, by_person[detector.key].name) == ("event", "student-a")
    assert by_person[action.key].method == "event"


def test_azure_helper_without_any_event_falls_through():
    r = res(SMART_DETECTOR, "sd-1", "student-a-rg")
    owner = run([r], [group_write("student-a-rg", "student-a@school.example")])[r.key]
    assert (owner.method, owner.name) == ("resource-group", "student-a")


def test_person_event_wins_even_for_an_attached_disk():
    vm = res(VM, "vm-1")
    disk = res(DISK, "disk-1", managed_by=vm.id)
    own = write(disk, "student-a@school.example")
    owner = run([vm, disk], [own, write(vm, "student-b@school.example")])[disk.key]
    assert (owner.method, owner.name, owner.creation) == ("event", "student-a", own)


def test_attached_disk_takes_its_vm_owner():
    vm = res(VM, "vm-1")
    disk = res(DISK, "disk-1", managed_by=vm.id)
    made = write(vm, "student-b@school.example")
    owner = run([vm, disk], [made])[disk.key]
    assert (owner.method, owner.name, owner.detail, owner.creation) == (
        "parent",
        "student-b",
        "via vm-1",
        made,
    )


def test_managed_by_matches_without_regard_to_case():
    vm = res(VM, "vm-1")
    disk = res(DISK, "disk-1", managed_by=vm.id.upper())
    assert run([vm, disk], [write(vm)])[disk.key].method == "parent"


def test_nic_takes_its_vm_owner():
    vm = res(VM, "vm-1")
    nic = res(NIC, "nic-1", properties={"virtualMachine": {"id": vm.id}})
    owner = run([vm, nic], [write(vm, "student-b@school.example")])[nic.key]
    assert (owner.method, owner.name, owner.detail) == ("parent", "student-b", "via vm-1")


def test_public_ip_goes_through_its_nic_to_the_vm():
    vm = res(VM, "vm-1")
    nic = res(NIC, "nic-1", properties={"virtualMachine": {"id": vm.id}})
    ip = res(
        PUBLIC_IP,
        "ip-1",
        properties={"ipConfiguration": {"id": nic.id + "/ipConfigurations/ipconfig1"}},
    )
    made = write(vm, "student-b@school.example")
    owner = run([vm, nic, ip], [made])[ip.key]
    assert (owner.method, owner.name, owner.detail, owner.creation) == (
        "parent",
        "student-b",
        "via nic-1",
        made,
    )


def test_public_ip_on_a_load_balancer_and_a_nat_gateway():
    lb = res(LOAD_BALANCER, "lb-1")
    nat = res(NAT_GATEWAY, "nat-1")
    on_lb = res(
        PUBLIC_IP,
        "ip-lb",
        properties={"ipConfiguration": {"id": lb.id + "/frontendIPConfigurations/fe"}},
    )
    on_nat = res(PUBLIC_IP, "ip-nat", properties={"natGateway": {"id": nat.id}})
    owners = run(
        [lb, nat, on_lb, on_nat],
        [write(lb, "student-a@school.example"), write(nat, "student-b@school.example")],
    )
    assert (owners[on_lb.key].name, owners[on_lb.key].detail) == ("student-a", "via lb-1")
    assert (owners[on_nat.key].name, owners[on_nat.key].detail) == ("student-b", "via nat-1")


def test_public_ip_on_another_parent_type_has_no_parent():
    agw = res("Microsoft.Network/applicationGateways", "agw-1")
    ip = res(
        PUBLIC_IP,
        "ip-1",
        properties={"ipConfiguration": {"id": agw.id + "/frontendIPConfigurations/fe"}},
    )
    assert Links([agw, ip]).parent(ip) is None


def test_public_ip_nat_gateway_link_counts_only_for_a_nat_gateway():
    vm = res(VM, "vm-1")
    ip = res(PUBLIC_IP, "ip-1", properties={"natGateway": {"id": vm.id}})
    assert Links([vm, ip]).parent(ip) is None


def aks_in_rg_a():
    return res(AKS, "aks-1", "rg-a", properties={"nodeResourceGroup": "MC_rg-a_aks-1_eastus"})


def test_node_group_resources_take_the_cluster_owner():
    cluster = aks_in_rg_a()
    disk = res(DISK, "pvc-1", "mc_rg-a_aks-1_eastus")
    owner = run([cluster, disk], [write(cluster, "student-b@school.example"), write(disk, APP)])[
        disk.key
    ]
    assert (owner.method, owner.name, owner.detail) == ("parent", "student-b", "via aks-1")


def test_node_group_disk_with_an_outside_managed_by_takes_the_cluster():
    cluster = aks_in_rg_a()
    outside = rid(
        "mc_rg-a_aks-1_eastus",
        "Microsoft.Compute/virtualMachineScaleSets/vmss-1/virtualMachines",
        "0",
    )
    disk = res(DISK, "pvc-1", "mc_rg-a_aks-1_eastus", managed_by=outside)
    assert Links([cluster, disk]).parent(disk) is cluster


def test_node_group_resource_falls_back_to_the_cluster_when_its_parent_has_no_owner():
    cluster = aks_in_rg_a()
    vm = res(VM, "vm-9")  # in the scan, outside the node group, nobody found for it
    disk = res(DISK, "pvc-1", "mc_rg-a_aks-1_eastus", managed_by=vm.id)
    owner = run([cluster, vm, disk], [write(cluster, "student-a@school.example")])[disk.key]
    assert (owner.method, owner.name, owner.detail) == ("parent", "student-a", "via aks-1")


def test_dangling_link_outside_a_node_group_gives_none():
    disk = res(DISK, "disk-1", managed_by=rid("rg-a", VM, "gone"))
    nic = res(NIC, "nic-1", properties={"virtualMachine": {"id": rid("rg-a", VM, "gone")}})
    links = Links([disk, nic])
    assert links.parent(disk) is None
    assert links.parent(nic) is None


def test_parent_must_still_exist():
    disk = res(DISK, "disk-1", "student-a-rg", managed_by=rid("student-a-rg", VM, "gone"))
    owner = run([disk], [write(disk, APP), group_write("student-a-rg")])[disk.key]
    assert (owner.method, owner.name) == ("resource-group", "student-a")


def test_app_created_resource_takes_the_group_creator():
    r = res(VM, "vm-1", "student-a-rg")
    made = group_write("student-a-rg", "student-a@school.example")
    owner = run([r], [write(r, APP), made])[r.key]
    assert (owner.method, owner.name, owner.detail, owner.creation) == (
        "resource-group",
        "student-a",
        "via resource group",
        made,
    )


def test_group_creator_uses_the_latest_created_write_and_ignores_edits():
    r = res(VM, "vm-1", "student-a-rg")
    owner = run(
        [r],
        [
            group_write("student-a-rg", "student-b@school.example", minutes=-60),
            group_write("student-a-rg", "student-a@school.example", minutes=-10),
            group_write("student-a-rg", "student-c@school.example", created=False),
        ],
    )[r.key]
    assert owner.name == "student-a"


def test_group_made_by_an_app_or_never_falls_through():
    by_app = res(VM, "vm-1", "app-rg")
    never = res(VM, "vm-2", "other-rg")
    owners = run([by_app, never], [write(by_app, APP), group_write("app-rg", APP)])
    assert (owners[by_app.key].method, owners[by_app.key].detail) == (
        "unknown",
        "created by an app",
    )
    assert (owners[never.key].method, owners[never.key].detail) == (
        "unknown",
        "no creation event in the last 90 days",
    )


def test_failed_lookup_skips_only_the_group_rule():
    vm = res(VM, "vm-1", "student-a-rg")
    disk = res(DISK, "disk-1", "student-a-rg", managed_by=vm.id)
    app_made = res(VM, "vm-2", "student-a-rg")
    same_sub = res(VM, "vm-3", "student-b-rg")
    other_sub = res(VM, "vm-4", "student-a-rg", sub=SUB2)
    owners = run(
        [vm, disk, app_made, same_sub, other_sub],
        [
            write(vm, "student-b@school.example"),
            write(app_made, APP),
            group_write("student-a-rg"),
            group_write("student-b-rg", "student-b@school.example"),
            group_write("student-a-rg", sub=SUB2),
        ],
        failed={vm.group_key},
    )
    assert owners[vm.key].method == "event"
    assert owners[disk.key].method == "parent"
    lost = owners[app_made.key]
    assert (lost.method, lost.name, lost.detail) == (
        "lookup-failed",
        None,
        "unknown (lookup failed)",
    )
    # Only the failed group loses rule 3: another group in the same subscription keeps it.
    assert (owners[same_sub.key].method, owners[same_sub.key].name) == (
        "resource-group",
        "student-b",
    )
    assert owners[other_sub.key].method == "resource-group"


def tagged_group(name, sub=SUB):
    return grp(name, sub, tags={NODE_GROUP_TAG: "aks-gone"})


def test_cluster_gone():
    disk = res(DISK, "pvc-1", "mc_rg-a_aks-gone_eastus")
    node_group = tagged_group("mc_rg-a_aks-gone_eastus")
    owner = run([disk], [write(disk, APP)], tagged={node_group.key})[disk.key]
    assert (owner.method, owner.name, owner.detail) == (
        "orphaned",
        None,
        "its Kubernetes cluster no longer exists",
    )


def test_tagged_group_that_a_current_cluster_names_is_a_live_node_group():
    cluster = aks_in_rg_a()
    disk = res(DISK, "pvc-1", "mc_rg-a_aks-1_eastus")
    node_group = tagged_group("mc_rg-a_aks-1_eastus")
    # Nobody owns the cluster: the disk is "No owner found", not "Cluster gone".
    owner = run([cluster, disk], [write(disk, APP)], tagged={node_group.key})[disk.key]
    assert owner.method == "unknown"
    assert owner.detail == "created by an app"


def test_graph_failure_gives_unknown_not_orphaned():
    disk = res(DISK, "pvc-1", "mc_rg-a_aks-gone_eastus")
    node_group = tagged_group("mc_rg-a_aks-gone_eastus")
    owner = run([disk], [write(disk, APP)], tagged={node_group.key}, graph_failed=True)[disk.key]
    assert (owner.method, owner.name, owner.detail) == (
        "unknown",
        None,
        "cluster list unavailable (Resource Graph failed)",
    )


def test_graph_failure_leaves_an_untagged_group_alone():
    disk = res(DISK, "disk-1")
    owner = run([disk], [write(disk, APP)], graph_failed=True)[disk.key]
    assert owner.detail == "created by an app"


def test_group_creator_comes_before_cluster_gone():
    disk = res(DISK, "pvc-1", "mc_rg-a_aks-gone_eastus")
    node_group = tagged_group("mc_rg-a_aks-gone_eastus")
    owners = run(
        [disk],
        [write(disk, APP), group_write("mc_rg-a_aks-gone_eastus")],
        tagged={node_group.key},
    )
    assert (owners[disk.key].method, owners[disk.key].name) == ("resource-group", "student-a")


def test_cluster_gone_comes_before_the_failed_lookup():
    disk = res(DISK, "pvc-1", "mc_rg-a_aks-gone_eastus")
    node_group = tagged_group("mc_rg-a_aks-gone_eastus")
    owner = run([disk], [write(disk, APP)], failed={disk.group_key}, tagged={node_group.key})[
        disk.key
    ]
    assert owner.method == "orphaned"


def test_tagged_node_groups_are_the_groups_with_the_aks_tag_in_any_case():
    lower = grp("mc_a", tags={"aks-managed-cluster-name": "aks-1"})
    mixed = grp("mc_b", tags={"AKS-Managed-Cluster-Name": "aks-1", "other": "x"})
    other_tag = grp("mc_c", tags={"AKS-Managed-Cluster-RG": "rg-a"})
    plain = grp("rg-d")
    groups = {g.key: g for g in (lower, mixed, other_tag, plain)}
    assert tagged_node_groups(groups) == {lower.key, mixed.key}


def test_no_owner_found_names_the_lookback_and_guesses_from_the_name_tag():
    tagged = res(VM, "vm-1", tags={"Name": "demo box"})
    plain = res(VM, "vm-2")
    owners = run([tagged, plain], lookback_days=30)
    assert owners[tagged.key].detail == "no creation event in the last 30 days"
    assert owners[tagged.key].guess == "demo box"
    assert owners[plain.key].guess == ""  # the name has its own column: no "Name tag" repeat
    assert owners[plain.key].name is None


@pytest.mark.parametrize("key", ["name", "NAME"])
def test_the_name_tag_guess_matches_the_tag_name_in_any_case(key):
    tagged = res(VM, "vm-1", tags={key: "demo box"})
    assert run([tagged])[tagged.key].guess == "demo box"


def test_parent_cycle_terminates():
    a = res(DISK, "disk-a")
    b = res(DISK, "disk-b", managed_by=a.id)
    a = res(DISK, "disk-a", managed_by=b.id)
    owners = run([a, b])
    assert owners[a.key].method == "unknown"
    assert owners[b.key].method == "unknown"


def test_every_resource_gets_an_owner():
    items = [res(VM, "vm-1"), res(DISK, "disk-1"), res(NIC, "nic-1")]
    assert set(run(items)) == {r.key for r in items}


def test_node_groups_map_group_keys_to_clusters():
    cluster = res(
        AKS, "aks-1", "Rg-A", sub=SUB2, properties={"nodeResourceGroup": "MC_Rg-A_aks-1_eastus"}
    )
    plain = res(AKS, "aks-2")
    links = Links([cluster, plain])
    key = f"/subscriptions/{SUB2}/resourcegroups/mc_rg-a_aks-1_eastus"
    assert links.node_groups == {key: cluster}


MALFORMED = [
    pytest.param(DISK, {}, "no-slash-id", id="managed-by-without-group"),
    pytest.param(NIC, {"virtualMachine": "vm-1"}, "", id="virtual-machine-string"),
    pytest.param(NIC, {"virtualMachine": None}, "", id="virtual-machine-none"),
    pytest.param(NIC, {"virtualMachine": {"id": 5}}, "", id="virtual-machine-id-int"),
    pytest.param(
        PUBLIC_IP, {"ipConfiguration": None, "natGateway": "nat-1"}, "", id="ip-config-none"
    ),
    pytest.param(PUBLIC_IP, {"ipConfiguration": {"id": "nic"}}, "", id="ip-config-no-slash"),
    pytest.param(PUBLIC_IP, {"ipConfiguration": {"id": "/a/b/c/d"}}, "", id="ip-config-no-group"),
    pytest.param(AKS, {"nodeResourceGroup": 7}, "", id="node-group-int"),
    pytest.param(AKS, {"nodeResourceGroup": None}, "", id="node-group-none"),
    pytest.param(NIC, ["not", "a", "dict"], "", id="properties-list"),
    pytest.param(AKS, ["not", "a", "dict"], "", id="aks-properties-list"),
]


@pytest.mark.parametrize(("type_", "properties", "managed_by"), MALFORMED)
def test_malformed_properties_never_raise(type_, properties, managed_by):
    r = res(type_, "x-1", managed_by=managed_by)
    r = replace(r, properties=properties)
    other = res(DISK, "disk-1", "mc_rg-a_aks-1_eastus")
    owners = run([r, other], [write(r, APP)])
    assert isinstance(owners[r.key], Owner)
    assert isinstance(owners[other.key], Owner)
