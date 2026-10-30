"""Fakes for the Azure SDK: a credential, a transport that never reaches the network, and errors.

May import tests/helpers.py; helpers.py never imports this module.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from datetime import datetime
from http import HTTPStatus
from typing import Any

from azure.core.credentials import AccessToken
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ResourceNotModifiedError,
    map_error,
)
from azure.core.pipeline.transport import HttpTransport
from azure.core.utils import CaseInsensitiveDict
from azure.mgmt.core.exceptions import ARMErrorFormat
from azure.mgmt.monitor.models import EventData
from azure.mgmt.resource.resources.models import GenericResourceExpanded
from azure.mgmt.resource.subscriptions.models import Subscription as SdkSubscription
from azure.mgmt.resourcegraph.models import QueryRequest, QueryResponse
from azure.storage.blob._shared.response_handlers import process_storage_error

from tests.helpers import T0


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


class _FakeXmlResponse(_FakeResponse):
    def __init__(self, status: int, code: str) -> None:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f"<Error><Code>{code}</Code><Message>x</Message></Error>"
        )
        super().__init__(status, body)
        self.content_type = "application/xml"
        self.headers = CaseInsensitiveDict(
            {"x-ms-error-code": code, "Content-Type": self.content_type}
        )


def storage_error(code: str, status: int) -> HttpResponseError:
    """A storage error with the shape azure-storage-blob raises it.

    As the generated operations do: map_error with their error map, else a plain
    HttpResponseError, then process_storage_error (_shared/response_handlers.py), which raises
    ResourceNotFoundError for a 404 and sets error_code.
    """
    response = _FakeXmlResponse(status, code)
    error_map = {
        401: ClientAuthenticationError,
        404: ResourceNotFoundError,
        409: ResourceExistsError,
        304: ResourceNotModifiedError,
    }
    try:
        try:
            map_error(status_code=status, response=response, error_map=error_map)
            raise HttpResponseError(response=response)
        except HttpResponseError as error:
            process_storage_error(error)
    except HttpResponseError as processed:
        return processed
    raise AssertionError("process_storage_error did not raise")


def lazy(items: Iterable, error: Exception | None = None, after: int = 0) -> Iterator:
    """Yields `after` items, then raises `error` if set, else the rest.

    The SDK's list calls return a lazy ItemPaged, which raises only while it is iterated.
    """
    items = list(items)
    yield from items[:after]
    if error is not None:
        raise error
    yield from items[after:]


def sdk_subscription(sub_id: str, name: str) -> SdkSubscription:
    """A subscription as the SDK deserializes the REST JSON of the subscription list."""
    return SdkSubscription({"subscriptionId": sub_id, "displayName": name})


def arm_row(
    resource_id: str,
    *,
    created: datetime | None = T0,
    managed_by: str | None = None,
    tags: dict[str, str] | None = None,
) -> GenericResourceExpanded:
    """A resource as the SDK deserializes one row of the ARM resource list."""
    body: dict[str, Any] = {
        "id": resource_id,
        "name": resource_id.rsplit("/", 1)[-1],
        "type": "/".join(resource_id.split("/providers/", 1)[-1].split("/")[:2]),
        "tags": tags,
        "managedBy": managed_by,
    }
    if created is not None:
        body["createdTime"] = created.isoformat()
    return GenericResourceExpanded(body)


def graph_page(rows: list[dict], skip_token: str | None = None) -> QueryResponse:
    """A Resource Graph response in objectArray format."""
    body: dict[str, Any] = {
        "totalRecords": len(rows),
        "count": len(rows),
        "resultTruncated": "false",
        "data": rows,
    }
    if skip_token is not None:
        body["$skipToken"] = skip_token
    return QueryResponse.deserialize(body)


class FakeSubscriptionClient:
    def __init__(self, subs: list[SdkSubscription], error: Exception | None = None) -> None:
        self.subscriptions = self
        self._subs = subs
        self._error = error

    def list(self) -> Iterator[SdkSubscription]:
        return lazy(self._subs, self._error)


class FakeResourceClient:
    def __init__(
        self, rows: list[GenericResourceExpanded], error: Exception | None = None, after: int = 0
    ) -> None:
        self.resources = self
        self._rows = rows
        self._error = error
        self._after = after
        self.expands: list[str | None] = []

    def list(self, *, expand: str | None = None) -> Iterator[GenericResourceExpanded]:
        self.expands.append(expand)
        return lazy(self._rows, self._error, self._after)


class FakeGraph:
    """Answers each query text from its list of responses in order; an Exception is raised."""

    def __init__(self, responses: dict[str, list[QueryResponse | Exception]]) -> None:
        self._responses = {query: list(pages) for query, pages in responses.items()}
        self.requests: list[QueryRequest] = []

    def resources(self, query: QueryRequest) -> QueryResponse:
        self.requests.append(query)
        response = self._responses[query.query].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def event(
    resource_id: str | None,
    caller: str | None = "student-a@school.example",
    *,
    op: str | None = "Microsoft.Compute/virtualMachines/write",
    status: str | None = "Succeeded",
    sub_status: str | None = "Created",
    status_code: str | None = None,
    when: datetime | None = T0,
) -> EventData:
    """An Activity Log event as the SDK deserializes the REST JSON; a None leaves the field out.

    `status_code` fills `properties.statusCode` only, as the schema's own sample does.
    """

    def localizable(value: str | None) -> dict[str, str] | None:
        return None if value is None else {"value": value, "localizedValue": value}

    body: dict[str, Any] = {
        "caller": caller,
        "resourceId": resource_id,
        "operationName": localizable(op),
        "status": localizable(status),
        "subStatus": localizable(sub_status),
        "properties": None if status_code is None else {"statusCode": status_code},
        "eventTimestamp": None if when is None else when.isoformat(),
    }
    return EventData.deserialize(body)


class FakeActivityLogs:
    def __init__(self, events: list[EventData], error: Exception | None, after: int) -> None:
        self._events = events
        self._error = error
        self._after = after
        self.filters: list[str] = []
        self.selects: list[str | None] = []

    def list(self, filter: str, select: str | None = None) -> Iterator[EventData]:
        self.filters.append(filter)
        self.selects.append(select)
        return lazy(self._events, self._error, self._after)


class FakeMonitor:
    """A MonitorManagementClient whose Activity Log list records its filter and select."""

    def __init__(
        self, events: list[EventData], error: Exception | None = None, after: int = 0
    ) -> None:
        self.activity_logs = FakeActivityLogs(events, error, after)


class _Download:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class FakeBlob:
    """A BlobClient whose download_blob raises `error` (the real SDK sends its GET there)."""

    def __init__(self, data: bytes | None = None, error: Exception | None = None) -> None:
        self._data = data
        self._error = error

    def download_blob(self) -> _Download:
        if self._error is not None:
            raise self._error
        return _Download(self._data or b"")


class FakeRoleAssignments:
    """An AuthorizationManagementClient whose role assignment list records its filter."""

    def __init__(self, items: list | None = None, error: Exception | None = None) -> None:
        self.role_assignments = self
        self._items = items or []
        self._error = error
        self.filters: list[str | None] = []

    def list_for_subscription(self, filter: str | None = None) -> Iterator:
        self.filters.append(filter)
        return lazy(self._items, self._error)


class FakeClients:
    """The Clients factories over fakes."""

    def __init__(
        self,
        subscriptions: FakeSubscriptionClient,
        graph: FakeGraph | None = None,
        resources: dict[str, FakeResourceClient] | None = None,
        monitors: dict[str, Any] | None = None,
        *,
        authorizations: dict[str, FakeRoleAssignments] | None = None,
        blobs: dict[tuple[str, str], FakeBlob] | None = None,
    ) -> None:
        self._subscriptions = subscriptions
        self._graph = graph
        self._resources = resources or {}
        self._monitors = monitors or {}
        self._authorizations = authorizations or {}
        self._blobs = blobs or {}

    def subscriptions(self) -> FakeSubscriptionClient:
        return self._subscriptions

    def graph(self) -> FakeGraph:
        return self._graph

    def resources(self, subscription_id: str) -> FakeResourceClient:
        return self._resources[subscription_id]

    def monitor(self, subscription_id: str) -> Any:
        return self._monitors[subscription_id]

    def authorization(self, subscription_id: str) -> FakeRoleAssignments:
        return self._authorizations[subscription_id]

    def blob(self, account: str, container: str, name: str) -> FakeBlob:
        return self._blobs[(container, name)]
