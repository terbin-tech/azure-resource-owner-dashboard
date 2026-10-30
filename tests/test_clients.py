import logging

import pytest
from azure.core.exceptions import AzureError
from azure.core.rest import HttpRequest
from azure.identity import CredentialUnavailableError
from azure.mgmt.resourcegraph.models import QueryRequest

import dashboard.clients
from dashboard.clients import (
    ARM_SCOPE,
    Clients,
    WriteRefused,
    allowed,
    error_code,
    make_clients,
)
from tests.fakes import FakeCredential, Reached, RecordingTransport, http_error
from tests.helpers import SUB

ARM = "https://management.azure.com"

# One read per factory, as the scan makes it.
READS = {
    "subscriptions": lambda c: next(iter(c.subscriptions().subscriptions.list())),
    "resources": lambda c: next(iter(c.resources(SUB).resources.list(expand="createdTime"))),
    "monitor": lambda c: next(iter(c.monitor(SUB).activity_logs.list(filter="x"))),
    "graph": lambda c: c.graph().resources(QueryRequest(subscriptions=[SUB], query="resources")),
}

# The raw send method of each client: send_request on the resources and subscriptions
# clients, _send_request on the Resource Graph and monitor clients (pinned wheels).
SENDERS = {
    "subscriptions": lambda c: c.subscriptions().send_request,
    "resources": lambda c: c.resources(SUB).send_request,
    "monitor": lambda c: c.monitor(SUB)._send_request,
    "graph": lambda c: c.graph()._send_request,
}


@pytest.fixture
def inner():
    return RecordingTransport()


@pytest.fixture
def clients(inner):
    return Clients(FakeCredential(), transport=inner)


def test_a_resource_group_delete_is_refused_before_the_network(clients, inner):
    with pytest.raises(WriteRefused):
        clients.resources(SUB).resource_groups.begin_delete("rg-x")
    assert inner.requests == []


def test_a_provider_registration_is_refused(clients, inner):
    with pytest.raises(WriteRefused):
        clients.resources(SUB).providers.register("Microsoft.Compute")
    assert inner.requests == []


@pytest.mark.parametrize("name", READS)
def test_reads_pass_through(inner, name):
    credential = FakeCredential()
    clients = Clients(credential, transport=inner)
    with pytest.raises(Reached):
        READS[name](clients)
    assert [r.method for r in inner.requests] == ["POST" if name == "graph" else "GET"]
    if name == "resources":
        assert "createdTime" in inner.requests[0].url
    assert credential.scopes == (ARM_SCOPE,)


@pytest.mark.parametrize("name", SENDERS)
def test_every_factory_is_guarded_on_the_default_path(monkeypatch, name):
    monkeypatch.setattr(dashboard.clients, "RequestsTransport", RecordingTransport)
    clients = make_clients(FakeCredential())
    send = SENDERS[name](clients)
    with pytest.raises(WriteRefused):
        send(HttpRequest("DELETE", f"{ARM}/x"))
    with pytest.raises(Reached):
        send(HttpRequest("GET", f"{ARM}/x"))


class FalsyTransport(RecordingTransport):
    def __len__(self) -> int:
        return 0


class NetworkTransport(RecordingTransport):
    """Stands in for RequestsTransport: if a request lands here, the given transport was skipped."""

    def send(self, request, **kwargs):
        raise AssertionError("the default transport was used")


def test_a_given_transport_is_used_even_when_falsy(monkeypatch):
    monkeypatch.setattr(dashboard.clients, "RequestsTransport", NetworkTransport)
    inner = FalsyTransport()
    with pytest.raises(Reached):
        READS["subscriptions"](Clients(FakeCredential(), transport=inner))
    assert len(inner.requests) == 1


@pytest.mark.parametrize(
    ("method", "url", "expected"),
    [
        ("GET", f"{ARM}/subscriptions?api-version=2022-12-01", True),
        ("GET", "https://example.invalid/anything", True),
        ("POST", f"{ARM}/providers/Microsoft.ResourceGraph/resources?api-version=2021-03-01", True),
        ("POST", f"{ARM}/subscriptions/{SUB}/providers/Microsoft.Compute/register", False),
        ("PUT", f"{ARM}/subscriptions/{SUB}/resourcegroups/rg-x", False),
        ("PATCH", f"{ARM}/subscriptions/{SUB}/resourcegroups/rg-x", False),
        ("DELETE", f"{ARM}/subscriptions/{SUB}/resourcegroups/rg-x", False),
        ("POST", f"{ARM}/providers/Microsoft.ResourceGraph/resourcesHistory", False),
        ("POST", f"{ARM}/providers/Microsoft.ResourceGraph/resources;x", False),
    ],
)
def test_allowed(method, url, expected):
    assert allowed(method, url) is expected


def test_refusal_is_not_an_azure_error():
    # azure-core's RetryPolicy retries AzureErrors and the scan catches them; a refusal must
    # stop the run.
    assert not issubclass(WriteRefused, AzureError)


@pytest.mark.parametrize("name", READS)
def test_requests_carry_the_user_agent(clients, inner, name):
    with pytest.raises(Reached):
        READS[name](clients)
    assert inner.requests[0].headers["User-Agent"].startswith("azure-resource-owner-dashboard")


def test_error_code():
    assert error_code(http_error("AuthorizationFailed")) == "AuthorizationFailed"
    assert error_code(http_error(None, 502)) == "HTTP 502"
    assert error_code(ValueError()) == "ValueError"
    # An HttpResponseError with no response has no status: fall through to the class name.
    assert error_code(CredentialUnavailableError("x")) == "CredentialUnavailableError"


def test_library_loggers_never_below_warning():
    for name in ("azure", "urllib3"):
        assert logging.getLogger(name).level == logging.WARNING
    assert logging.getLogger("azure.identity").level == logging.ERROR
