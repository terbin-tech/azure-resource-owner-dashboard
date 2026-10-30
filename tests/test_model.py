import pytest

from dashboard.model import VM, parse_id, tag
from tests.helpers import SUB, res, rid


def test_keys_are_casefolded_ids():
    assert res(VM, "Vm-1").key == res(VM, "vm-1").key


def test_tag_names_are_matched_in_any_case():
    tags = {"NAME": "web", "env": ""}
    assert tag(tags, "Name") == "web"
    assert tag(tags, "Env") == ""  # present but empty is not absent
    assert tag(tags, "owner") is None
    assert tag({}, "Name") is None


def test_group_key_joins_subscription_and_group():
    assert res(VM, "x", "RG-A").group_key == f"/subscriptions/{SUB}/resourcegroups/rg-a"


def test_parse_id():
    assert parse_id(rid("RG-A", "Microsoft.Compute/virtualMachines", "x")) == (SUB, "RG-A")
    with pytest.raises(ValueError):
        parse_id("/providers/x")


def test_power_state_reads_the_extended_instance_view():
    props = {"extended": {"instanceView": {"powerState": {"code": "PowerState/stopped"}}}}
    assert res(VM, "x", properties=props).power_state == "PowerState/stopped"


@pytest.mark.parametrize(
    "props",
    [{}, {"extended": None}, {"extended": {"instanceView": "x"}}],
)
def test_power_state_missing_is_none(props):
    assert res(VM, "x", properties=props).power_state is None
