"""The timer function's run: read the roster blob, scan, and write the page blob.

Settings come from two app settings, DASHBOARD_DATA_ACCOUNT and DASHBOARD_SCAN_CLIENT_ID, and
the committed settings.json. The run is the same scan -> build_report -> render_page chain as
the CLI, and its one write is site/index.html.

A failure is logged as its type and code in the message text and re-raised as a generic error,
so the invocation fails (the failed-run alert reads Success == false) and no traceback or
Azure message, which carry IDs and sign-in names, reaches the log. The Python worker forwards
only the formatted message, level and category, so fields passed with extra= would never reach
Application Insights. Nothing is kept between runs.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import ManagedIdentityCredential

from .access import GUID_RE
from .clients import (
    ACCOUNT_RE,
    ROSTER_BLOB,
    ROSTER_CONTAINER,
    Clients,
    PageWriter,
    error_code,
    make_clients,
)
from .config import Config, ConfigError, load_config
from .model import Incomplete
from .people import Roster, RosterError, parse_roster
from .pipeline import build_report, scan
from .render import render_page

ACCOUNT_SETTING = "DASHBOARD_DATA_ACCOUNT"
CLIENT_ID_SETTING = "DASHBOARD_SCAN_CLIENT_ID"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Deployed:
    data_account: str
    scan_client_id: str


def deployed_from_env(env: Mapping[str, str]) -> Deployed:
    """Both settings, checked. A bad value is named by its setting, never shown."""
    for name in (ACCOUNT_SETTING, CLIENT_ID_SETTING):
        if name not in env:
            raise ConfigError(f"missing app setting {name}")
    if not ACCOUNT_RE.fullmatch(env[ACCOUNT_SETTING]):
        raise ConfigError(f"{ACCOUNT_SETTING} is not a storage account name")
    if not GUID_RE.fullmatch(env[CLIENT_ID_SETTING]):
        raise ConfigError(f"{CLIENT_ID_SETTING} is not a client ID")
    return Deployed(env[ACCOUNT_SETTING], env[CLIENT_ID_SETTING])


def make_credential(deployed: Deployed) -> TokenCredential:
    """The scan identity alone: no credential chain, so no other identity can answer.

    A user-assigned identity's client ID is supported in every hosting environment, source:
    azure-identity 1.27.0 managed_identity.py.
    """
    return ManagedIdentityCredential(client_id=deployed.scan_client_id)


def load_roster_blob(blob) -> tuple[Roster | None, Incomplete | None]:
    """Read the roster from config/ in the data account.

    A missing blob or container (BlobNotFound, ContainerNotFound) means "no roster". Any other
    error becomes an incomplete banner, never a crash. Blob error codes:
    https://learn.microsoft.com/en-us/rest/api/storageservices/blob-service-error-codes
    """
    try:
        body = blob.download_blob().readall()
    except ResourceNotFoundError:
        return None, None
    except AzureError as exc:
        return None, Incomplete("roster", error_code(exc))
    try:
        return parse_roster(body.decode("utf-8")), None
    except UnicodeDecodeError:
        return None, Incomplete("roster", "not UTF-8")
    except RosterError as exc:  # its messages name lines and counts, never values
        return None, Incomplete("roster", str(exc))


def run(clients: Clients, writer: PageWriter, cfg: Config, data_account: str, now: datetime) -> int:
    """One full run. Returns how many parts were incomplete."""
    roster, problem = load_roster_blob(clients.blob(data_account, ROSTER_CONTAINER, ROSTER_BLOB))
    if roster is None and problem is None:
        # Not on the page, which students read; a misplaced upload would otherwise go unnoticed.
        log.warning("no roster at %s/%s", ROSTER_CONTAINER, ROSTER_BLOB)
    data = scan(clients, cfg, now)
    if problem:
        data.inventory.incomplete.append(problem)
    report = build_report(data, roster)
    writer.write(render_page(report, cfg))
    return len(report.incomplete)


def scheduled_run(env: Mapping[str, str], settings_path: Path, now: datetime) -> None:
    try:
        deployed = deployed_from_env(env)
        cfg = load_config(None, settings_path)
        credential = make_credential(deployed)
        incomplete = run(
            make_clients(credential),
            PageWriter(credential, deployed.data_account),
            cfg,
            deployed.data_account,
            now,
        )
    except Exception as exc:
        # Type and code only: an Azure message carries IDs and sign-in names, and a traceback
        # can carry names.
        log.error("scan failed: %s (%s)", type(exc).__name__, error_code(exc))
        raise RuntimeError("scan failed") from None
    if incomplete:  # the scan-incomplete alert counts these lines; it does not read the number
        log.warning("run incomplete: %d", incomplete)
