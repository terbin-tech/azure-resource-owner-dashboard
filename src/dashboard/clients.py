"""Azure SDK clients, all behind a read-only transport.

What keeps a local run read-only is the transport, not a role: the maintainer's login is
Owner on the subscription, so nothing in Azure would stop a write. Every client is built here
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
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from azure.core.credentials import TokenCredential
from azure.core.exceptions import HttpResponseError
from azure.core.pipeline.transport import HttpTransport, RequestsTransport
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.resource.resources import ResourceManagementClient
from azure.mgmt.resource.subscriptions import SubscriptionClient
from azure.mgmt.resourcegraph import ResourceGraphClient

# Put in front of the SDK's own User-Agent by azure-core's UserAgentPolicy (the user_agent
# keyword, policies/_universal.py), so the Activity Log shows which tool made a request.
USER_AGENT = "azure-resource-owner-dashboard"
# The public-cloud ARM scope (azure-mgmt-core 1.6.0 tools.py, get_arm_endpoints).
ARM_SCOPE = "https://management.azure.com/.default"
# The Resource Graph query (resourcegraph 8.0.1, build_resources_request's template_url).
GRAPH_QUERY_PATH = "/providers/microsoft.resourcegraph/resources"

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


def allowed(method: str, url: str) -> bool:
    if method == "GET":
        return True
    return method == "POST" and urlsplit(url).path.rstrip("/").casefold() == GRAPH_QUERY_PATH


class ReadOnlyTransport(HttpTransport):
    """Hands reads to the inner transport and refuses everything else."""

    def __init__(self, inner: HttpTransport) -> None:
        self._inner = inner

    def send(self, request, **kwargs):
        if not allowed(request.method, request.url):
            # The method only: the URL carries the subscription ID.
            raise WriteRefused(f"{request.method} refused: the dashboard is read-only")
        return self._inner.send(request, **kwargs)

    def open(self) -> None:
        self._inner.open()

    def close(self) -> None:
        self._inner.close()

    def __enter__(self) -> ReadOnlyTransport:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_details) -> None:
        self._inner.__exit__(*exc_details)


class Clients:
    """Each SDK client is built once on first use, on its own read-only transport."""

    def __init__(self, credential: TokenCredential, transport: HttpTransport | None = None) -> None:
        self._credential = credential
        self._transport = transport
        self._subscriptions: SubscriptionClient | None = None
        self._graph: ResourceGraphClient | None = None
        self._resources: dict[str, ResourceManagementClient] = {}
        self._monitor: dict[str, MonitorManagementClient] = {}

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


def make_clients(credential: TokenCredential) -> Clients:
    return Clients(credential)


def error_code(exc: Exception) -> str:
    if isinstance(exc, HttpResponseError):
        code = getattr(exc.error, "code", None)
        if code:
            return code
        if exc.status_code is not None:
            return f"HTTP {exc.status_code}"
    return type(exc).__name__
