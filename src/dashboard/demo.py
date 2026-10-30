"""Fake placeholder data for the demo page and the CLI tests.

One row per case the page can show; each name says what the row shows, so nobody
mistakes it for the class.

python -m dashboard demo    # writes out-demo/index.html, no Azure needed
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config
from .events import LookupResult, Write
from .inventory import Inventory
from .model import Group, Resource, Subscription, parse_id
from .people import Roster
from .pipeline import ScanData

NOW = datetime(2026, 10, 9, 5, 0, tzinfo=UTC)  # a scheduled run: 07:00 Central European time
SUB = "00000000-0000-0000-0000-000000000001"
APP = "00000000-0000-0000-0000-000000000099"  # an app's caller is its client ID, not a sign-in
DOMAIN = "school.example"
NODE_GROUP = "MC_rg-aks-lab_aks-cluster_westeurope"
GONE_NODE_GROUP = "MC_rg-old-lab_aks-gone_westeurope"  # tagged by AKS, but no cluster names it
GROUPS = (
    "rg-vm-lab",
    "rg-shared-lab",
    "rg-aks-lab",
    NODE_GROUP,
    GONE_NODE_GROUP,
    "rg-app-lab",
    "NetworkWatcherRG",
    "resource-owner-dashboard",
)
DEMO_ROSTER: Roster = {
    "student-a": ("student-a", "cohort-x"),
    "student-b": ("student-b", "cohort-x"),
    "student-c": ("student-c", "cohort-y"),
}


def _group_id(group: str) -> str:
    return f"/subscriptions/{SUB}/resourceGroups/{group}"


def _id(group: str, type_: str, name: str) -> str:
    return f"{_group_id(group)}/providers/{type_}/{name}"


def _resource(
    type_: str,
    group: str,
    name: str,
    created: datetime,
    *,
    managed_by: str = "",
    properties: dict[str, Any] | None = None,
) -> Resource:
    return Resource(
        id=_id(group, type_, name),
        type=type_.casefold(),
        name=name,
        subscription=SUB,
        group=group,
        created_at=created,
        managed_by=managed_by,
        properties=properties or {},
    )


def _power(code: str) -> dict[str, Any]:
    return {"extended": {"instanceView": {"powerState": {"code": code}}}}


def _made(target: Resource | str, caller: str, when: datetime) -> Write:
    """A succeeded write that answered Created, on a resource or a group ID."""
    key = target.key if isinstance(target, Resource) else target.casefold()
    return Write(key, caller, True, when)


def demo_scan(now: datetime = NOW) -> ScanData:
    day = timedelta(days=1)
    student_a, student_b = f"student-a@{DOMAIN}", f"student-b@{DOMAIN}"

    vm = _resource(
        "Microsoft.Compute/virtualMachines",
        "rg-vm-lab",
        "vm-running-public-ip",
        now - 2 * day,
        properties=_power("PowerState/running"),
    )
    nic = _resource(
        "Microsoft.Network/networkInterfaces",
        "rg-vm-lab",
        "nic-attached-to-its-vm",
        vm.created_at,
        properties={"virtualMachine": {"id": vm.id}},
    )
    ip = _resource(
        "Microsoft.Network/publicIPAddresses",
        "rg-vm-lab",
        "ip-attached-to-its-nic",
        vm.created_at,
        properties={"ipConfiguration": {"id": f"{nic.id}/ipConfigurations/ipconfig1"}},
    )
    disk = _resource(
        "Microsoft.Compute/disks",
        "rg-vm-lab",
        "disk-attached-to-its-vm",
        vm.created_at,
        managed_by=vm.id,
    )
    stopped = _resource(
        "Microsoft.Compute/virtualMachines",
        "rg-shared-lab",
        "vm-stopped-still-billed",
        now - 6 * day,
        properties=_power("PowerState/stopped"),
    )
    # No Graph properties, as when Resource Graph had not read the VM: never hide a billed VM.
    unknown_power = _resource(
        "Microsoft.Compute/virtualMachines",
        "rg-shared-lab",
        "vm-power-state-unknown",
        now - day,
    )
    vnet = _resource(
        "Microsoft.Network/virtualNetworks",
        "rg-shared-lab",
        "vnet-older-than-4-days",
        now - 9 * day,
    )
    no_event = _resource(
        "Microsoft.Compute/disks",
        "rg-shared-lab",
        "disk-no-creation-event",
        now - 120 * day,
    )
    aks = _resource(
        "Microsoft.ContainerService/managedClusters",
        "rg-aks-lab",
        "aks-cluster",
        now - 3 * day,
        properties={"nodeResourceGroup": NODE_GROUP},
    )
    vmss = _resource(
        "Microsoft.Compute/virtualMachineScaleSets",
        NODE_GROUP,
        "vmss-in-a-node-group",
        aks.created_at,
    )
    gone_disk = _resource(
        "Microsoft.Compute/disks",
        GONE_NODE_GROUP,
        "disk-left-by-a-gone-cluster",
        now - 30 * day,
    )
    app_ip = _resource(
        "Microsoft.Network/publicIPAddresses",
        "rg-app-lab",
        "ip-made-by-an-app",
        now - day,
    )
    watcher = _resource(
        "Microsoft.Network/networkWatchers",
        "NetworkWatcherRG",
        "NetworkWatcher_eastus",
        vnet.created_at,
    )
    storage = _resource(
        "Microsoft.Storage/storageAccounts",
        "resource-owner-dashboard",
        "storage-of-this-dashboard",
        now - 20 * day,
    )
    resources = [
        vm, nic, ip, disk, stopped, unknown_power, vnet, no_event, aks, vmss, gone_disk, app_ip,
        watcher, storage,
    ]  # fmt: skip
    # The NIC, IP and disk have no write of their own, so they show who owns them "via" a parent.
    writes = [
        _made(vm, student_a, vm.created_at),
        _made(stopped, student_b, stopped.created_at),
        _made(unknown_power, student_b, unknown_power.created_at),
        _made(vnet, student_a, vnet.created_at),
        _made(aks, student_a, aks.created_at),
        _made(_group_id(NODE_GROUP), APP, aks.created_at),
        _made(vmss, APP, vmss.created_at),
        _made(_group_id(GONE_NODE_GROUP), APP, gone_disk.created_at),
        _made(gone_disk, APP, gone_disk.created_at),
        _made(_group_id("rg-app-lab"), student_b, app_ip.created_at - day),
        _made(app_ip, APP, app_ip.created_at),
    ]
    # AKS tags every node group with its cluster's name and group; these values name nothing real.
    node_tags = {
        NODE_GROUP: {
            "AKS-Managed-Cluster-Name": "aks-cluster",
            "AKS-Managed-Cluster-RG": "rg-aks-lab",
        },
        GONE_NODE_GROUP: {
            "AKS-Managed-Cluster-Name": "aks-gone",
            "AKS-Managed-Cluster-RG": "rg-old-lab",
        },
    }
    groups = [Group(_group_id(name), name, SUB, node_tags.get(name, {})) for name in GROUPS]
    sub = Subscription(SUB, "Course subscription")
    # One lookup per group, in the order scan reads them; a write's group is in its ID.
    lookups = [
        LookupResult(
            sub,
            group,
            [w for w in writes if parse_id(w.resource)[1] == group.casefold()],
            duration_s=0.8,
        )
        for group in sorted(GROUPS, key=str.casefold)
    ]
    inventory = Inventory(
        scanned=[sub],
        not_scanned=["Old course subscription"],
        resources=resources,
        groups={g.key: g for g in groups},
        incomplete=[],
    )
    return ScanData(
        generated_at=now,
        inventory=inventory,
        lookups=lookups,
        lookback_days=Config().lookback_days,
        dashboard_group=Config().dashboard_resource_group,
    )
