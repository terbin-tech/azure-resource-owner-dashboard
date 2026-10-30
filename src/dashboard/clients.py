"""Azure SDK clients: every reader behind a read-only transport, and the one page writer.

What keeps a local run read-only is the transport, not a role: the maintainer's login is
Owner on the subscription, so nothing in Azure would stop a write. Every reader is built here
with transport=ReadOnlyTransport(...), which refuses any request but a GET or the Resource
Graph query POST before it reaches the network. An azure-core client takes its HTTP transport
through the transport keyword, source: azure-core client library developer guide,
https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/core/azure-core/CLIENT_LIBRARY_DEVELOPER.md

Retries are the SDK default. azure-core's RetryPolicy retries a GET on 408, 429, 500,
502 to 504 and 506 and up (azure-core 1.41.0 policies/_retry.py:78-79), and any 4xx or 5xx that
carries Retry-After, on any method (lines 235-239); a POST without Retry-After only on 500, 503
and 504 (line 203). The Resource Graph throttling guidance does not promise Retry-After on a 429,
so a Resource Graph query still throttled after that lands as incomplete. Source: Microsoft
Learn, "Guidance for throttled requests in Azure Resource Graph",
https://learn.microsoft.com/en-us/azure/governance/resource-graph/concepts/guidance-for-throttled-requests

The page writer is the one write. Its guard holds it to one request shape, a Put Blob of
site/index.html, because its role (Storage Blob Data Contributor) alone would also let it read
and delete any blob in the container. Storage clients retry with their own policy:
ExponentialRetry retries 408 and 5xx except 501 and 505, and not 429 (azure-storage-blob 12.31
_shared/policies.py, is_retry).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from azure.core.credentials import TokenCredential
from azure.core.exceptions import HttpResponseError
from azure.core.pipeline.transport import HttpTransport, RequestsTransport
from azure.mgmt.authorization.v2022_04_01 import AuthorizationManagementClient
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.resource.resources import ResourceManagementClient
from azure.mgmt.resource.subscriptions import SubscriptionClient
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.storage.blob import BlobClient, ContentSettings

# Put in front of the SDK's own User-Agent by azure-core's UserAgentPolicy (the user_agent
# keyword, policies/_universal.py), so the Activity Log shows which tool made a request.
USER_AGENT = "azure-resource-owner-dashboard"
# The public-cloud ARM scope (azure-mgmt-core 1.6.0 tools.py, get_arm_endpoints).
ARM_SCOPE = "https://management.azure.com/.default"
ARM_HOST = "management.azure.com"
# The Resource Graph query (resourcegraph 8.0.1, build_resources_request's template_url).
GRAPH_QUERY_PATH = "/providers/microsoft.resourcegraph/resources"
# The storage OAuth scope (azure-storage-blob 12.31 _shared/constants.py, STORAGE_OAUTH_SCOPE).
STORAGE_SCOPE = "https://storage.azure.com/.default"
PAGE_CONTAINER = "site"
PAGE_BLOB = "index.html"
# Storage account names are lowercase letters and numbers, 3 to 24 characters, source:
# https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules
ACCOUNT_RE = re.compile(r"[a-z0-9]{3,24}", re.ASCII)

# The Azure SDK at DEBUG logs request headers and the signed-in account; azure.identity logs a
# failed sign-in's error text at WARNING.
for _name in ("azure", "urllib3"):
    logging.getLogger(_name).setLevel(logging.WARNING)
logging.getLogger("azure.identity").setLevel(logging.ERROR)


class WriteRefused(RuntimeError):
    """A request that could change Azure was stopped before the network.

    Deliberately not an AzureError: azure-core's RetryPolicy retries those and the scan catches
    them, while a refused write must stop the run.
    """


def blob_url(account: str) -> str:
    if not ACCOUNT_RE.fullmatch(account):
        raise ValueError("not a storage account name")
    return f"https://{account}.blob.core.windows.net"


def allowed(method: str, url: str) -> bool:
    if method == "GET":
        return True
    # The host too: blob clients sit behind the same guard, and a path-only check would let a
    # POST to a storage host through.
    parts = urlsplit(url)
    return (
        method == "POST"
        and parts.hostname == ARM_HOST
        and parts.path.rstrip("/").casefold() == GRAPH_QUERY_PATH
    )


def page_write_allowed(account: str) -> Callable[[Any], bool]:
    """Allows only the Put Blob of site/index.html in `account`.

    Copy Blob and Put Blob From URL are PUTs on the same URL that would pull any URL into the
    page, so a request with x-ms-copy-source is refused (azure-storage-blob 12.31
    _generated/operations/_blob_operations.py, _block_blob_operations.py). A small
    upload_blob(overwrite=True) is one Put Blob; above max_single_put_size (64 MiB) it would
    switch to Put Block, ?comp=block, which this refuses (_upload_helpers.py; the 64 MiB
    default is in _shared/models.py).
    """
    host = urlsplit(blob_url(account)).netloc
    path = f"/{PAGE_CONTAINER}/{PAGE_BLOB}"

    def allow(request: Any) -> bool:
        parts = urlsplit(request.url)
        return (
            request.method == "PUT"
            and parts.scheme == "https"
            and parts.netloc == host
            and parts.path == path
            and not parts.query
            and request.headers.get("x-ms-blob-type") == "BlockBlob"
            and "x-ms-copy-source" not in request.headers
        )

    return allow


class GuardTransport(HttpTransport):
    """Hands a request to the inner transport only if `allow` accepts it.

    Defines no __len__ or __bool__, so it stays truthy: azure-storage-blob replaces a falsy
    transport with a plain RequestsTransport (12.31 _shared/base_client.py, `if not transport:`).
    """

    def __init__(self, inner: HttpTransport, allow: Callable[[Any], bool]) -> None:
        self._inner = inner
        self._allow = allow

    def send(self, request, **kwargs):
        if not self._allow(request):
            # The method only: the URL carries the subscription ID.
            raise WriteRefused(f"{request.method} refused: the dashboard is read-only")
        return self._inner.send(request, **kwargs)

    def open(self) -> None:
        self._inner.open()

    def close(self) -> None:
        self._inner.close()

    def __enter__(self) -> GuardTransport:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_details) -> None:
        self._inner.__exit__(*exc_details)


class ReadOnlyTransport(GuardTransport):
    """Hands reads to the inner transport and refuses everything else."""

    def __init__(self, inner: HttpTransport) -> None:
        super().__init__(inner, lambda request: allowed(request.method, request.url))


class Clients:
    """Each SDK client is built once on first use, on its own read-only transport."""

    def __init__(self, credential: TokenCredential, transport: HttpTransport | None = None) -> None:
        self._credential = credential
        self._transport = transport
        self._subscriptions: SubscriptionClient | None = None
        self._graph: ResourceGraphClient | None = None
        self._resources: dict[str, ResourceManagementClient] = {}
        self._monitor: dict[str, MonitorManagementClient] = {}
        self._authorization: dict[str, AuthorizationManagementClient] = {}

    def _options(self) -> dict:
        return {
            "transport": ReadOnlyTransport(
                self._transport if self._transport is not None else RequestsTransport()
            ),
            "user_agent": USER_AGENT,
        }

    def subscriptions(self) -> SubscriptionClient:
        if self._subscriptions is None:
            self._subscriptions = SubscriptionClient(self._credential, **self._options())
        return self._subscriptions

    def graph(self) -> ResourceGraphClient:
        if self._graph is None:
            self._graph = ResourceGraphClient(self._credential, **self._options())
        return self._graph

    def resources(self, subscription_id: str) -> ResourceManagementClient:
        if subscription_id not in self._resources:
            self._resources[subscription_id] = ResourceManagementClient(
                self._credential, subscription_id, **self._options()
            )
        return self._resources[subscription_id]

    def monitor(self, subscription_id: str) -> MonitorManagementClient:
        if subscription_id not in self._monitor:
            self._monitor[subscription_id] = MonitorManagementClient(
                self._credential, subscription_id, **self._options()
            )
        return self._monitor[subscription_id]

    def authorization(self, subscription_id: str) -> AuthorizationManagementClient:
        if subscription_id not in self._authorization:
            self._authorization[subscription_id] = AuthorizationManagementClient(
                self._credential, subscription_id, **self._options()
            )
        return self._authorization[subscription_id]

    def blob(self, account: str, container: str, name: str) -> BlobClient:
        return BlobClient(
            blob_url(account), container, name, credential=self._credential, **self._options()
        )


class PageWriter:
    """Writes the page blob, through a guard that allows nothing but that one Put Blob."""

    def __init__(
        self, credential: TokenCredential, account: str, transport: HttpTransport | None = None
    ) -> None:
        guard = GuardTransport(
            transport if transport is not None else RequestsTransport(),
            page_write_allowed(account),
        )
        self._client = BlobClient(
            blob_url(account),
            PAGE_CONTAINER,
            PAGE_BLOB,
            credential=credential,
            transport=guard,
            user_agent=USER_AGENT,
        )

    def write(self, html: str) -> None:
        self._client.upload_blob(
            html.encode("utf-8"),
            overwrite=True,
            content_settings=ContentSettings(
                content_type="text/html; charset=utf-8", cache_control="no-store"
            ),
        )


def make_clients(credential: TokenCredential) -> Clients:
    return Clients(credential)


def error_code(exc: Exception) -> str:
    """The ARM error code, else the storage one, else the HTTP status, else the class name.

    A storage error has no parsed `error` for an XML body; azure-storage-blob sets `error_code`
    from it instead, and turns a code it knows into a StorageErrorCode member, which formats as
    `StorageErrorCode.BLOB_NOT_FOUND` on Python 3.14 (12.31 _shared/response_handlers.py).
    """
    if isinstance(exc, HttpResponseError):
        code = getattr(exc.error, "code", None) or getattr(exc, "error_code", None)
        if code:
            return code.value if isinstance(code, Enum) else str(code)
        if exc.status_code is not None:
            return f"HTTP {exc.status_code}"
    return type(exc).__name__
