"""The page function's answers: the page for a person who may see it, a short message otherwise.

Sign-in is App Service's. The app trusts the X-MS-CLIENT-PRINCIPAL header only while the platform
says sign-in is on: WEBSITE_AUTH_ENABLED is "Read-only. Injected into a Windows or Linux app to
indicate whether App Service authentication is enabled", source:
https://learn.microsoft.com/en-us/azure/app-service/reference-app-settings
With sign-in off, a header sent by anyone reaches the app, so the page refuses before it reads
the header.

Every failure fails closed: no answer but 200 carries the page. A failure is logged as its code or
class only, never its message, which can carry IDs and sign-in names, and never with a traceback.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from azure.core.exceptions import AzureError, ResourceNotFoundError

from .access import Decision, may_view, object_id
from .clients import PAGE_BLOB, PAGE_CONTAINER, Clients, WriteRefused, error_code
from .render import CSP

# frame-ancestors "is not supported in the <meta> element", so the header adds it, source:
# https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/frame-ancestors
HEADER_CSP = CSP + "; frame-ancestors 'none'"
MESSAGE_CSP = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"

SIGN_IN_TEXT = "Please sign in with your school account."
NO_ACCESS_TEXT = (
    "You don't have access to the class subscription, so this page is not for you. "
    "If you think that's wrong, ask a teacher."
)
TRY_LATER_TEXT = "The access check failed. Please try again later."
NOT_BUILT_TEXT = "The page has not been built yet. Please try again later."

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Response:
    status: int
    body: str
    headers: dict[str, str]


# The headers on every answer, source: OWASP HTTP Headers Cheat Sheet,
# https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html
def _headers(csp: str) -> dict[str, str]:
    return {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "Strict-Transport-Security": "max-age=31536000",
    }


def message(status: int, text: str) -> Response:
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Forgotten Azure resources</title></head>"
        f"<body><p>{html.escape(text)}</p></body></html>"
    )
    return Response(status, body, _headers(MESSAGE_CSP))


def serve(
    headers: Mapping[str, str],
    clients: Clients,
    read_page: Callable[[], str],
    auth_enabled: bool,
) -> Response:
    """The answer to one request: 401, 403 or 503 with a message, or 200 with the page."""
    if not auth_enabled:
        log.warning("sign-in is off: refusing")
        return message(401, SIGN_IN_TEXT)
    oid = object_id(headers)
    if oid is None:
        return message(401, SIGN_IN_TEXT)
    try:
        decision = may_view(clients, oid)
        if decision is Decision.DENY:
            return message(403, NO_ACCESS_TEXT)
        if decision is not Decision.ALLOW:
            return message(503, TRY_LATER_TEXT)
        try:
            page = read_page()
        except ResourceNotFoundError:  # no run has written it yet
            return message(503, NOT_BUILT_TEXT)
        except AzureError as exc:
            log.warning("page read failed: %s", error_code(exc))
            return message(503, TRY_LATER_TEXT)
    except WriteRefused:
        raise
    except Exception as exc:
        log.error("page failed: %s", type(exc).__name__)
        return message(503, TRY_LATER_TEXT)
    return Response(200, page, _headers(HEADER_CSP))


def page_reader(clients: Clients, account: str) -> Callable[[], str]:
    """Reads site/index.html when called, so nothing is built for a refused request."""

    def read() -> str:
        blob = clients.blob(account, PAGE_CONTAINER, PAGE_BLOB)
        return blob.download_blob().readall().decode("utf-8")

    return read
