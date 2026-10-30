import json
import os
from datetime import timedelta
from pathlib import Path

import azure.functions as func
import pytest
from azure.functions.decorators.core import BindingDirection

import dashboard.deployed as deployed
import dashboard.web as web
import function_app
from dashboard.deployed import Deployed
from dashboard.web import Response
from tests.fakes import FakeCredential

REPO = Path(__file__).resolve().parents[1]
FAKE = Deployed("dashdata0001", "00000000-0000-0000-0000-000000000021")


@pytest.fixture(scope="module")
def functions() -> dict[str, func.decorators.function_app.Function]:
    # get_functions() records each name it sees and refuses a repeat, so the app is read once
    # (azure-functions 2.3.0 FunctionRegister.validate_function_names).
    return {f.get_function_name(): f for f in function_app.app.get_functions()}


@pytest.fixture
def page_env(monkeypatch):
    """The page function with fake settings and credential, and a fresh cache each test."""
    monkeypatch.setattr(deployed, "deployed_from_env", lambda env: FAKE)
    monkeypatch.setattr(deployed, "make_credential", lambda d: FakeCredential())
    function_app._deployed.cache_clear()
    function_app._clients.cache_clear()
    yield
    function_app._deployed.cache_clear()
    function_app._clients.cache_clear()


def _request() -> func.HttpRequest:
    return func.HttpRequest("GET", "/api/page", headers={}, body=b"")


def test_two_functions_with_the_designed_names_and_triggers(functions):
    assert set(functions) == {"scan", "page"}
    scan = functions["scan"].get_trigger()
    assert scan.schedule == function_app.SCHEDULE
    assert scan.run_on_startup is False
    page = functions["page"].get_trigger()
    assert page.route == "page"
    assert page.methods == [func.HttpMethod.GET]
    assert page.auth_level == func.AuthLevel.ANONYMOUS


def bindings(f: func.decorators.function_app.Function) -> list[tuple[str, int]]:
    return sorted((b.type, b.direction) for b in f.get_bindings())


IN, OUT = BindingDirection.IN.value, BindingDirection.OUT.value


def test_no_function_has_a_binding_that_can_write(functions):
    # An output binding writes through the Functions host, not an SDK client, so the transport
    # guard and the checks in test_read_only.py never see it. The page's http output is its
    # response. (azure-functions 2.3.0 Function.get_bindings; Binding.direction is the int value.)
    assert {name: bindings(f) for name, f in functions.items()} == {
        "scan": [("timerTrigger", IN)],
        "page": [("http", OUT), ("httpTrigger", IN)],
    }


def test_page_function_passes_the_response_through(functions, page_env, monkeypatch):
    monkeypatch.setattr(
        web,
        "serve",
        lambda *a, **k: Response(403, "x", {"Content-Type": "text/html; charset=utf-8"}),
    )
    r = functions["page"].get_user_function()(_request())
    assert isinstance(r, func.HttpResponse)
    assert r.status_code == 403
    assert r.get_body() == b"x"
    assert r.headers["Content-Type"] == "text/html; charset=utf-8"


@pytest.mark.parametrize(
    ("value", "enabled"),
    [
        (None, False),
        ("", False),
        ("False", False),
        ("false", False),
        ("0", False),
        ("True", True),
        ("true", True),
    ],
)
def test_page_reads_the_sign_in_flag(functions, page_env, monkeypatch, value, enabled):
    seen = []

    def serve(headers, clients, read_page, auth_enabled):
        seen.append(auth_enabled)
        return Response(401, "x", {})

    monkeypatch.setattr(web, "serve", serve)
    if value is None:
        monkeypatch.delenv("WEBSITE_AUTH_ENABLED", raising=False)
    else:
        monkeypatch.setenv("WEBSITE_AUTH_ENABLED", value)
    functions["page"].get_user_function()(_request())
    assert seen == [enabled]


def test_scan_function_calls_the_scheduled_run(functions, monkeypatch):
    calls = []
    monkeypatch.setattr(deployed, "scheduled_run", lambda *args: calls.append(args))
    functions["scan"].get_user_function()(object())
    assert len(calls) == 1
    env, settings, now = calls[0]
    assert env is os.environ
    assert settings == function_app.SETTINGS == REPO / "settings.json"
    assert now.utcoffset() == timedelta(0)


def test_host_json():
    assert json.loads((REPO / "host.json").read_text()) == {
        "version": "2.0",
        "functionTimeout": "00:10:00",
        "logging": {
            "logLevel": {"default": "Warning", "Host.Results": "Information"},
            "applicationInsights": {
                "samplingSettings": {"isEnabled": True, "excludedTypes": "Request;Exception"}
            },
        },
        "extensionBundle": {
            "id": "Microsoft.Azure.Functions.ExtensionBundle",
            "version": "[4.*, 5.0.0)",
        },
    }
