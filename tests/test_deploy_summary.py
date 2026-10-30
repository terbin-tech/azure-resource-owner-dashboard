import json
import re
from pathlib import Path

from scripts.deploy_summary import main, summarize
from tests.helpers import SUB

GUID = re.compile(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


RG = f"/subscriptions/{SUB}/resourceGroups/rg-student-a"
APP = f"{RG}/providers/Microsoft.Web/sites/app-student-a"


def _change(resource_id: str, change_type: str) -> dict:
    return {
        "resourceId": resource_id,
        "changeType": change_type,
        "before": {"properties": {"note": "student-a@school.example"}},
        "after": {"properties": {"note": "secret value"}},
        "delta": [{"path": "properties.note", "propertyChangeType": "Modify"}],
    }


def _storage(name: str) -> str:
    return f"{RG}/providers/Microsoft.Storage/storageAccounts/{name}"


def _what_if(*changes: dict) -> dict:
    return {"status": "Succeeded", "changes": list(changes)}


def test_what_if_summary_counts_by_change_and_type():
    doc = _what_if(
        _change(_storage("dashhost0001"), "Create"),
        _change(_storage("dashdata0001"), "Create"),
        _change(APP, "Create"),
        _change(f"{RG}/providers/Microsoft.Authorization/roleAssignments/{SUB}", "NoChange"),
    )
    assert summarize(doc) == [
        "Create: 3",
        "NoChange: 1",
        "Create Microsoft.Storage/storageAccounts x2",
        "Create Microsoft.Web/sites x1",
        "NoChange Microsoft.Authorization/roleAssignments x1",
    ]


def test_what_if_document_may_nest_the_changes_under_properties():
    doc = {"properties": {"changes": [_change(APP, "Modify")]}}
    assert summarize(doc) == ["Modify: 1", "Modify Microsoft.Web/sites x1"]


def test_summary_never_prints_ids_names_or_values():
    doc = _what_if(
        _change(_storage("dashdata0001"), "Create"),
        _change(APP, "Modify"),
        _change(RG, "Create"),
    )
    doc["potentialChanges"] = [_change(APP, "Delete")]
    doc["error"] = {"code": "InvalidTemplate", "message": f"{SUB} student-a@school.example"}
    out = "\n".join(summarize(doc))
    assert not GUID.search(out)
    assert "/subscriptions/" not in out
    for private in ("dashdata0001", "app-student-a", "rg-student-a", "secret value", "@"):
        assert private not in out


def test_delete_is_called_out():
    out = summarize(_what_if(_change(_storage("dashdata0001"), "Delete")))
    assert any(line.startswith("DELETE") for line in out)
    assert "Delete Microsoft.Storage/storageAccounts x1" in out


def test_potential_changes_are_counted_apart():
    # The CLI's JSON output puts potentialChanges next to changes at the top level.
    role = f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleAssignments/{SUB}"
    doc = _what_if(_change(APP, "Create"))
    doc["potentialChanges"] = [_change(role, "Create")]
    assert summarize(doc) == [
        "Create: 1",
        "Create Microsoft.Web/sites x1",
        "Potential Create: 1",
        "Potential Create Microsoft.Authorization/roleAssignments x1",
    ]


def test_a_potential_delete_is_called_out():
    # The REST shape nests both lists under properties.
    doc = {
        "properties": {
            "changes": [_change(APP, "NoChange")],
            "potentialChanges": [_change(_storage("dashdata0001"), "Delete")],
        }
    }
    assert summarize(doc) == [
        "DELETE: the plan deletes resources, read the file before going on",
        "NoChange: 1",
        "NoChange Microsoft.Web/sites x1",
        "Potential Delete: 1",
        "Potential Delete Microsoft.Storage/storageAccounts x1",
    ]


def test_an_empty_or_null_potential_list_adds_nothing():
    for value in ([], None):
        doc = _what_if(_change(APP, "Create"))
        doc["potentialChanges"] = value
        assert summarize(doc) == ["Create: 1", "Create Microsoft.Web/sites x1"]


def test_no_delete_line_without_a_delete():
    out = summarize(_what_if(_change(APP, "Create")))
    assert not any(line.startswith("DELETE") for line in out)


def test_extension_resources_take_their_own_type():
    container = f"{_storage('dashdata0001')}/blobServices/default/containers/config"
    doc = _what_if(
        _change(f"{container}/providers/Microsoft.Authorization/roleAssignments/{SUB}", "Create"),
        _change(f"{APP}/config/authsettingsV2", "Create"),
        _change(RG, "Create"),
    )
    assert summarize(doc)[1:] == [
        "Create Microsoft.Authorization/roleAssignments x1",
        "Create Microsoft.Resources/resourceGroups x1",
        "Create Microsoft.Web/sites/config x1",
    ]


def test_deployment_result_prints_state_and_error_codes_only():
    doc = {
        "properties": {
            "provisioningState": "Failed",
            "error": {
                "code": "DeploymentFailed",
                "message": f"/subscriptions/{SUB} ...",
                "details": [{"code": "AuthorizationFailed", "message": "x"}],
            },
        }
    }
    assert summarize(doc) == [
        "provisioningState: Failed",
        "error: DeploymentFailed",
        "error: AuthorizationFailed",
    ]


def test_successful_deployment_prints_its_state_only():
    doc = {"properties": {"provisioningState": "Succeeded", "outputs": {"x": {"value": "y"}}}}
    assert summarize(doc) == ["provisioningState: Succeeded"]


def test_an_odd_code_is_not_echoed():
    doc = {"properties": {"provisioningState": "Failed", "error": {"code": "a b@c.example"}}}
    assert summarize(doc) == ["provisioningState: Failed", "error: unrecognised"]


def test_unknown_document_has_no_summary():
    assert summarize({"hello": "world"}) == []
    assert summarize([]) == []


def _run(tmp_path: Path, text: str, capsys) -> tuple[int, str]:
    path = tmp_path / "doc.json"
    path.write_text(text, encoding="utf-8")
    code = main([str(path)])
    return code, capsys.readouterr().out


def test_main_prints_the_summary_and_exits_0(tmp_path, capsys):
    code, out = _run(tmp_path, json.dumps(_what_if(_change(APP, "Create"))), capsys)
    assert code == 0
    assert out.splitlines() == ["Create: 1", "Create Microsoft.Web/sites x1"]


def test_unknown_document_exits_1(tmp_path, capsys):
    code, out = _run(tmp_path, json.dumps({"hello": "world"}), capsys)
    assert code == 1
    assert out == "no summary found (read the file yourself)\n"


def test_unparsable_file_exits_1_without_echoing_it(tmp_path, capsys):
    code, out = _run(tmp_path, f"not json {SUB}", capsys)
    assert code == 1
    assert out == "no summary found (read the file yourself)\n"


def test_missing_file_and_missing_argument_exit_1(tmp_path, capsys):
    assert main([str(tmp_path / "nope.json")]) == 1
    assert capsys.readouterr().out == "no such file\n"
    assert main([]) == 1
    assert "usage" in capsys.readouterr().err


ROLE = "Microsoft.Authorization/roleAssignments"
IDENTITY = f"{RG}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-student-a"


def _unevaluated_extension() -> str:
    # What what-if reports when a role assignment's name depends on a value it cannot know yet.
    storage = _storage("dashdata0001")
    return (
        f"[extensionResourceId('{storage}', '{ROLE}', guid('{storage}', '{IDENTITY}', "
        f"'/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/{SUB}'))]"
    )


def _unevaluated_subscription() -> str:
    return (
        f"[subscriptionResourceId('{SUB}', '{ROLE}', getRoleAssignmentName(createObject("
        f"'scope', '/subscriptions/{SUB}', 'principalId', reference('{IDENTITY}', '2024-11-30'))))]"
    )


def test_an_unevaluated_resource_id_takes_the_type_from_its_type_argument():
    doc = _what_if(
        _change(_unevaluated_extension(), "Unsupported"),
        _change(_unevaluated_subscription(), "Unsupported"),
    )
    assert summarize(doc) == ["Unsupported: 2", f"Unsupported {ROLE} x2"]


def test_an_unevaluated_resource_id_without_a_type_argument_is_unevaluated():
    expression = f"[concat('{_storage('dashdata0001')}', '/x', parameters('p'))]"
    assert summarize(_what_if(_change(expression, "Unsupported"))) == [
        "Unsupported: 1",
        "Unsupported unevaluated x1",
    ]


def test_an_unevaluated_resource_id_is_never_echoed():
    doc = _what_if(
        _change(_unevaluated_extension(), "Unsupported"),
        _change(_unevaluated_subscription(), "Unsupported"),
        _change("[concat('/subscriptions/x', parameters('p'))]", "Unsupported"),
    )
    out = "\n".join(summarize(doc))
    assert not GUID.search(out)
    assert "/subscriptions/" not in out
    for private in ("dashdata0001", "id-student-a", "rg-student-a", "reference", "["):
        assert private not in out
