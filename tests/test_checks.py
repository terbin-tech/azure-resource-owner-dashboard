from dashboard.config import Config
from dashboard.model import DISK, PUBLIC_IP, VM
from dashboard.render import STOPPED_TEXT, UNKNOWN_POWER_TEXT, checks
from tests.helpers import make_row, res

CFG = Config(max_age_days=4)
VNET = "Microsoft.Network/virtualNetworks"


def _vm(state, age_days=1, public_ip=False):
    code = {"extended": {"instanceView": {"powerState": {"code": state}}}}
    properties = code if state is not None else {}
    return make_row(res(VM, "vm-1", properties=properties), age_days=age_days, public_ip=public_ip)


def test_anything_older_than_max_age_days_gets_a_mark():
    for type_ in (DISK, PUBLIC_IP, VNET):
        assert checks(make_row(res(type_, "x"), age_days=5), CFG) == ("older than 4 days",)
    assert checks(make_row(res(DISK, "disk-1"), age_days=4), CFG) == ()
    assert checks(make_row(res(DISK, "disk-1"), age_days=None), CFG) == ()


def test_running_vm_with_a_public_ip_gets_a_mark():
    assert checks(_vm("PowerState/running", public_ip=True), CFG) == ("running with a public IP",)
    assert checks(_vm("PowerState/running", age_days=9, public_ip=True), CFG) == (
        "older than 4 days",
        "running with a public IP",
    )
    assert checks(_vm("PowerState/running"), CFG) == ()


def test_stopped_vm_is_still_billed():
    assert checks(_vm("PowerState/stopped"), CFG) == (STOPPED_TEXT,)
    assert checks(_vm("PowerState/stopped", public_ip=True), CFG) == (STOPPED_TEXT,)
    assert checks(_vm("PowerState/stopped", age_days=9), CFG) == ("older than 4 days", STOPPED_TEXT)
    assert (
        STOPPED_TEXT == "stopped but still billed: use Stop in the portal so it shows Deallocated"
    )


def test_vm_without_a_power_state_is_marked():
    # A failed power state read must never hide a billed VM.
    assert checks(_vm(None), CFG) == (UNKNOWN_POWER_TEXT,)
    assert UNKNOWN_POWER_TEXT == "power state unknown: check it is Deallocated"


def test_deallocated_and_transitional_vms_get_no_power_mark():
    for state in ("deallocated", "deallocating", "starting", "stopping"):
        assert checks(_vm(f"PowerState/{state}", public_ip=True), CFG) == ()
        assert checks(_vm(f"PowerState/{state}", age_days=9), CFG) == ("older than 4 days",)


def test_attached_parts_get_no_mark():
    for type_ in (DISK, PUBLIC_IP, "Microsoft.Network/networkInterfaces"):
        attached = make_row(res(type_, "x"), age_days=30, attached=True)
        assert checks(attached, CFG) == ()


def test_power_state_codes_compare_without_case():
    assert checks(_vm("PowerState/Stopped"), CFG) == (STOPPED_TEXT,)
    assert checks(_vm("PowerState/RUNNING", public_ip=True), CFG) == ("running with a public IP",)
    assert checks(_vm("PowerState/Deallocated"), CFG) == ()


def test_unrecognised_power_state_is_marked_unknown():
    # Fail closed: a code the page does not know must not hide a billed VM.
    for state in ("PowerState/unknown", "", 5):
        assert checks(_vm(state), CFG) == (UNKNOWN_POWER_TEXT,)
