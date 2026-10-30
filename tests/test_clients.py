import logging

import pytest
from azure.core.exceptions import AzureError
from azure.core.rest import HttpRequest
from azure.identity import CredentialUnavailableError
from azure.mgmt.authorization.v2022_04_01.models import RoleAssignmentCreateParameters
from azure.mgmt.resourcegraph.models import QueryRequest

import dashboard.clients
from dashboard.clients import (
    ARM_SCOPE,
    STORAGE_SCOPE,
    Clients,
    PageWriter,
    WriteRefused,
    allowed,
    blob_url,
    error_code,
    make_clients,
    page_write_allowed,
)
from tests.fakes import FakeCredential, Reached, RecordingTransport, http_error, storage_error
from tests.helpers import SUB

ARM = "https://management.azure.com"
BLOB = "https://dashdata0001.blob.core.windows.net"
PAGE = f"{BLOB}/site/index.html"

# One read per factory, as the scan makes it.
READS = {
    "subscriptions": lambda c: next(iter(c.subscriptions().subscriptions.list())),
    "resources": lambda c: next(iter(c.resources(SUB).resources.list(expand="createdTime"))),
    "monitor": lambda c: next(iter(c.monitor(SUB).activity_logs.list(filter="x"))),
    "graph": lambda c: c.graph().resources(QueryRequest(subscriptions=[SUB], query="resources")),
    "authorization": lambda c: next(
        iter(c.authorization(SUB).role_assignments.list_for_subscription(filter="atScope()"))
    ),
    "blob": lambda c: c.blob("dashdata0001", "config", "roster.csv").download_blob(),
}

# The raw send method of each ARM client: send_request on the resources and subscriptions
# clients, _send_request on the Resource Graph, monitor and authorization clients (pinned
# wheels). The blob client has no raw send; the blob tests below cover its guard.
SENDERS = {
    "subscriptions": lambda c: c.subscriptions().send_request,
    "resources": lambda c: c.resources(SUB).send_request,
    "monitor": lambda c: c.monitor(SUB)._send_request,
    "graph": lambda c: c.graph()._send_request,
    "authorization": lambda c: c.authorization(SUB)._send_request,
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
    assert credential.scopes == ((STORAGE_SCOPE,) if name == "blob" else (ARM_SCOPE,))


def test_a_role_assignment_create_is_refused(clients, inner):
    parameters = RoleAssignmentCreateParameters(
        role_definition_id=f"/subscriptions/{SUB}/providers/Microsoft.Authorization/"
        "roleDefinitions/00000000-0000-0000-0000-000000000002",
        principal_id="00000000-0000-0000-0000-000000000003",
    )
    with pytest.raises(WriteRefused):
        clients.authorization(SUB).role_assignments.create(
            f"/subscriptions/{SUB}", "00000000-0000-0000-0000-000000000004", parameters
        )
    assert inner.requests == []


def test_a_blob_upload_through_the_reader_is_refused(clients, inner):
    with pytest.raises(WriteRefused):
        clients.blob("dashdata0001", "site", "index.html").upload_blob(b"x", overwrite=True)
    assert inner.requests == []


def test_the_page_writer_sends_one_put_to_the_page_blob(inner):
    with pytest.raises(Reached):
        PageWriter(FakeCredential(), "dashdata0001", transport=inner).write("<p>x</p>")
    [request] = inner.requests
    assert (request.method, request.url) == ("PUT", PAGE)
    assert request.headers["x-ms-blob-content-type"] == "text/html; charset=utf-8"
    assert request.headers["x-ms-blob-cache-control"] == "no-store"


# Requests the page writer's own BlobClient could make besides its one Put Blob: a delete, a
# Copy Blob (a PUT on the page URL with x-ms-copy-source), a Put Block (?comp=block), a read.
FORBIDDEN = {
    "delete": lambda b: b.delete_blob(),
    "copy": lambda b: b.start_copy_from_url("https://school.example/x"),
    "put block": lambda b: b.stage_block("00000000", b"x"),
    "read": lambda b: b.download_blob(),
}


@pytest.mark.parametrize("default_path", [False, True])
@pytest.mark.parametrize("name", FORBIDDEN)
def test_the_page_writer_refuses_everything_but_its_one_put(monkeypatch, default_path, name):
    inner = RecordingTransport()
    if default_path:
        monkeypatch.setattr(dashboard.clients, "RequestsTransport", lambda: inner)
        writer = PageWriter(FakeCredential(), "dashdata0001")
    else:
        writer = PageWriter(FakeCredential(), "dashdata0001", transport=inner)
    with pytest.raises(WriteRefused):
        FORBIDDEN[name](writer._client)
    assert inner.requests == []


BLOCK = {"x-ms-blob-type": "BlockBlob"}


@pytest.mark.parametrize(
    ("method", "url", "headers", "expected"),
    [
        ("PUT", PAGE, BLOCK, True),
        ("PUT", f"{PAGE}?comp=block", BLOCK, False),
        ("PUT", f"{BLOB}/site/other.html", BLOCK, False),
        ("PUT", f"{BLOB}/config/roster.csv", BLOCK, False),
        ("PUT", f"{PAGE}/x", BLOCK, False),
        ("PUT", "https://dashdata0002.blob.core.windows.net/site/index.html", BLOCK, False),
        ("PUT", PAGE.replace("https://", "http://"), BLOCK, False),
        ("PUT", PAGE, {**BLOCK, "x-ms-copy-source": "https://school.example/x"}, False),
        ("PUT", PAGE, {"x-ms-blob-type": "AppendBlob"}, False),
        ("PUT", PAGE, {"x-ms-copy-source": "https://school.example/x"}, False),
        ("PUT", PAGE, {}, False),
        ("GET", PAGE, BLOCK, False),
        ("DELETE", PAGE, BLOCK, False),
        ("POST", PAGE, BLOCK, False),
        ("PATCH", PAGE, BLOCK, False),
        ("HEAD", PAGE, BLOCK, False),
    ],
)
def test_page_write_allowed(method, url, headers, expected):
    request = HttpRequest(method, url, headers=headers)
    assert page_write_allowed("dashdata0001")(request) is expected


def test_blob_reader_and_page_writer_are_guarded_on_the_default_path(monkeypatch):
    inner = RecordingTransport()
    monkeypatch.setattr(dashboard.clients, "RequestsTransport", lambda: inner)
    blob = make_clients(FakeCredential()).blob("dashdata0001", "site", "index.html")
    with pytest.raises(WriteRefused):
        blob.upload_blob(b"x", overwrite=True)
    assert inner.requests == []
    with pytest.raises(Reached):
        blob.download_blob()
    with pytest.raises(Reached):
        PageWriter(FakeCredential(), "dashdata0001").write("x")
    assert [(r.method, r.url) for r in inner.requests] == [("GET", PAGE), ("PUT", PAGE)]


@pytest.mark.parametrize("account", ["Dash", "ab", "a" * 25, "x.evil.example/"])
def test_blob_url_refuses_a_bad_account_name(account):
    with pytest.raises(ValueError):
        blob_url(account)


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
        ("POST", f"{BLOB}/providers/Microsoft.ResourceGraph/resources", False),
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
    for code, status in [
        ("BlobNotFound", 404),
        ("AuthorizationPermissionMismatch", 403),
        ("AuthorizationFailure", 403),  # a StorageErrorCode member in the SDK
    ]:
        found = error_code(storage_error(code, status))
        assert found == code
        assert type(found) is str
        assert f"{found}" == code


def test_library_loggers_never_below_warning():
    for name in ("azure", "urllib3"):
        assert logging.getLogger(name).level == logging.WARNING
    assert logging.getLogger("azure.identity").level == logging.ERROR
