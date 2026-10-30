"""Fakes for the Azure SDK: a credential, a transport that never reaches the network, and errors.

May import tests/helpers.py; helpers.py never imports this module.
"""

from __future__ import annotations

import json
import time
from http import HTTPStatus

from azure.core.credentials import AccessToken
from azure.core.exceptions import HttpResponseError
from azure.core.pipeline.transport import HttpTransport
from azure.mgmt.core.exceptions import ARMErrorFormat


class FakeCredential:
    """A TokenCredential that records the scopes asked for and never signs in."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.scopes: tuple[str, ...] = ()

    def get_token(self, *scopes: str, **kwargs) -> AccessToken:
        self.scopes = scopes
        if self.error is not None:
            raise self.error
        return AccessToken("fake", int(time.time()) + 3600)


class Reached(Exception):
    """A request got past every policy to the transport."""


class RecordingTransport(HttpTransport):
    """Records each request it is handed, then raises Reached instead of sending it."""

    def __init__(self) -> None:
        self.requests: list = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        raise Reached(request.method)

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __exit__(self, *exc_details) -> None:
        pass


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status_code = status
        self.reason = HTTPStatus(status).phrase
        self._body = body

    def text(self, encoding: str | None = None) -> str:
        return self._body


def http_error(code: str | None, status: int = 403, message: str = "x") -> HttpResponseError:
    """An HttpResponseError parsed by azure-core from an ARM error body, as the SDK raises it."""
    body = "not json" if code is None else json.dumps({"error": {"code": code, "message": message}})
    return HttpResponseError(response=_FakeResponse(status, body), error_format=ARMErrorFormat)
