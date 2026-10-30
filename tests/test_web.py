import pytest

import dashboard.web as web
from dashboard.access import OID_CLAIMS, Decision
from dashboard.clients import WriteRefused
from dashboard.config import Config
from dashboard.render import CSP, render_page
from dashboard.web import (
    HEADER_CSP,
    MESSAGE_CSP,
    NO_ACCESS_TEXT,
    NOT_BUILT_TEXT,
    SIGN_IN_TEXT,
    TRY_LATER_TEXT,
    Response,
    message,
    page_reader,
    serve,
)
from tests.fakes import FakeBlob, FakeClients, FakeSubscriptionClient, storage_error
from tests.helpers import make_report, principal

OID = "00000000-0000-0000-0000-000000000011"
ALLOWED = principal([(OID_CLAIMS[0], OID)])
PAGE = "<!doctype html><p>the page</p>"
COMMON = {
    "Content-Type": "text/html; charset=utf-8",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
}


class Untouchable:
    """Clients that fail the test on any use."""

    def __getattr__(self, name):
        raise AssertionError(f"clients.{name} used")


class Calls:
    """Stands in for may_view and read_page: records each call, then returns or raises."""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def view(monkeypatch):
    def patch(result=None, error=None) -> Calls:
        calls = Calls(result, error)
        monkeypatch.setattr(web, "may_view", calls)
        return calls

    return patch


def test_no_principal_is_401_and_reads_nothing(view):
    may_view = view(Decision.ALLOW)
    read_page = Calls(PAGE)
    r = serve({}, Untouchable(), read_page, auth_enabled=True)
    assert r == message(401, SIGN_IN_TEXT)
    assert may_view.calls == []
    assert read_page.calls == []


def test_sign_in_off_is_401_even_with_a_principal(view, caplog):
    may_view = view(Decision.ALLOW)
    read_page = Calls(PAGE)
    r = serve(ALLOWED, Untouchable(), read_page, auth_enabled=False)
    assert r == message(401, SIGN_IN_TEXT)
    assert may_view.calls == []
    assert read_page.calls == []
    assert [rec.getMessage() for rec in caplog.records] == ["sign-in is off: refusing"]


def test_unexpected_error_is_503_and_logs_the_class_only(view, caplog):
    view(error=ValueError("student-a@school.example"))
    read_page = Calls(PAGE)
    r = serve(ALLOWED, Untouchable(), read_page, auth_enabled=True)
    assert r == message(503, TRY_LATER_TEXT)
    assert read_page.calls == []
    assert [rec.getMessage() for rec in caplog.records] == ["page failed: ValueError"]
    assert all(rec.exc_info is None for rec in caplog.records)
    assert "school.example" not in caplog.text


def test_unexpected_error_reading_the_page_is_503(view, caplog):
    view(Decision.ALLOW)
    r = serve(
        ALLOWED,
        Untouchable(),
        Calls(error=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "x")),
        auth_enabled=True,
    )
    assert r == message(503, TRY_LATER_TEXT)
    assert [rec.getMessage() for rec in caplog.records] == ["page failed: UnicodeDecodeError"]


def test_allowed_viewer_gets_the_page_with_its_headers(view):
    may_view = view(Decision.ALLOW)
    clients = Untouchable()
    read_page = Calls(PAGE)
    r = serve(ALLOWED, clients, read_page, auth_enabled=True)
    assert r == Response(200, PAGE, COMMON | {"Content-Security-Policy": HEADER_CSP})
    assert may_view.calls == [(clients, OID)]
    assert read_page.calls == [()]


def test_header_csp_is_the_page_csp_plus_frame_ancestors():
    assert f'content="{CSP}"' in render_page(make_report(), Config())
    assert HEADER_CSP == CSP + "; frame-ancestors 'none'"


def test_denied_viewer_gets_403_and_no_page(view):
    view(Decision.DENY)
    read_page = Calls(PAGE)
    r = serve(ALLOWED, Untouchable(), read_page, auth_enabled=True)
    assert r == message(403, NO_ACCESS_TEXT)
    assert read_page.calls == []


def test_error_decision_is_503(view):
    view(Decision.ERROR)
    read_page = Calls(PAGE)
    r = serve(ALLOWED, Untouchable(), read_page, auth_enabled=True)
    assert r == message(503, TRY_LATER_TEXT)
    assert read_page.calls == []


def test_missing_page_blob_is_503(view, caplog):
    view(Decision.ALLOW)
    read_page = Calls(error=storage_error("BlobNotFound", 404))
    r = serve(ALLOWED, Untouchable(), read_page, auth_enabled=True)
    assert r == message(503, NOT_BUILT_TEXT)
    assert caplog.records == []


def test_page_read_failure_is_503_and_logs_the_code(view, caplog):
    view(Decision.ALLOW)
    read_page = Calls(error=storage_error("AuthorizationPermissionMismatch", 403))
    r = serve(ALLOWED, Untouchable(), read_page, auth_enabled=True)
    assert r == message(503, TRY_LATER_TEXT)
    assert [rec.getMessage() for rec in caplog.records] == [
        "page read failed: AuthorizationPermissionMismatch"
    ]


@pytest.mark.parametrize("where", ["may_view", "read_page"])
def test_a_refused_write_propagates(view, where):
    refused = WriteRefused("PUT refused: the dashboard is read-only")
    view(error=refused) if where == "may_view" else view(Decision.ALLOW)
    read_page = Calls(error=refused) if where == "read_page" else Calls(PAGE)
    with pytest.raises(WriteRefused):
        serve(ALLOWED, Untouchable(), read_page, auth_enabled=True)


def test_messages_are_inert_html_with_a_strict_csp():
    for status, text in [
        (401, SIGN_IN_TEXT),
        (403, NO_ACCESS_TEXT),
        (503, TRY_LATER_TEXT),
        (503, NOT_BUILT_TEXT),
    ]:
        r = message(status, text)
        assert r.status == status
        assert r.headers == COMMON | {"Content-Security-Policy": MESSAGE_CSP}
        assert "<script" not in r.body
        assert "<title>Forgotten Azure resources</title>" in r.body
    assert "<p>&lt;b&gt;x&lt;/b&gt;</p>" in message(503, "<b>x</b>").body


def test_page_reader_reads_the_page_blob():
    clients = FakeClients(
        FakeSubscriptionClient([]), blobs={("site", "index.html"): FakeBlob(PAGE.encode())}
    )
    assert page_reader(clients, "dashdata0001")() == PAGE


def test_page_reader_builds_nothing_until_called():
    page_reader(Untouchable(), "dashdata0001")
