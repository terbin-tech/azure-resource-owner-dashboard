"""One fake subscription through the whole chain: scan -> build_report -> render_page.

Every Azure response is built from the SDK's own model classes, in the shape the real API
returns, so a wiring mistake between the modules shows up here, not in the live run.
"""

from datetime import timedelta

from dashboard.config import Config
from dashboard.inventory import GROUPS_QUERY, RESOURCES_QUERY
from dashboard.pipeline import build_report, scan
from dashboard.render import render_page
from dashboard.verify import render_verify
from tests.fakes import (
    FakeClients,
    FakeGraph,
    FakeMonitor,
    FakeResourceClient,
    FakeSubscriptionClient,
    arm_row,
    event,
    graph_page,
    sdk_subscription,
)
from tests.helpers import SUB, T0, rid

NOW = T0 + timedelta(days=10)
VM = rid("rg-a", "Microsoft.Compute/virtualMachines", "vm-a")


def test_scan_build_report_render_page_and_verify():
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
    clients = FakeClients(
        FakeSubscriptionClient([sdk_subscription(SUB, "Course subscription")]),
        FakeGraph(
            {RESOURCES_QUERY: [graph_page([graph_vm])], GROUPS_QUERY: [graph_page([graph_group])]}
        ),
        {SUB: FakeResourceClient([arm_row(VM, created=T0)])},
        {SUB: FakeMonitor([event(VM, "student-a@school.example", when=T0)])},
    )
    cfg = Config(max_age_days=7)

    data = scan(clients, cfg, NOW)
    report = build_report(data, None)
    page = render_page(report, cfg)

    assert data.inventory.incomplete == [] and report.incomplete == []
    (row,) = report.rows
    assert (row.resource.id, row.owner.method, row.card, row.age_days) == (
        VM,
        "event",
        "student-a",
        10,
    )
    assert 'id="card-student-a"' in page
    assert '<span class="muted">older than 7 days</span>' in page
    assert render_verify(data, report, None).startswith("# Verification report")
