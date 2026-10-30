"""Configuration.

Non-account values (max age, lookback, the dashboard's resource group) live in the committed
settings.json. Config's defaults repeat them for the tests, and a test checks that the two
agree. The only local value, the roster path, stays out of the repo in the gitignored
dashboard.toml.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from typing import Any

SETTINGS_FILE = "settings.json"
SETTINGS_KEYS = frozenset({"max_age_days", "lookback_days", "dashboard_resource_group"})
LOCAL_KEYS = frozenset({"roster_path"})
# An ASCII subset of the resource group naming rule: 1 to 90 characters, letters, digits,
# underscore, parentheses, hyphen and period, not ending in a period. Microsoft's rule also
# allows other Unicode letters and digits. Used with fullmatch. Source: Microsoft Learn,
# "Naming rules and restrictions for Azure resources", Microsoft.Resources, resourcegroups:
# https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules
GROUP_NAME_RE = re.compile(r"[A-Za-z0-9_().-]{0,89}[A-Za-z0-9_()-]", re.ASCII)


class ConfigError(ValueError):
    """Invalid settings or local config file."""


@dataclass(frozen=True)
class Config:
    max_age_days: int = 4  # most labs take 2 days or less
    lookback_days: int = 90
    dashboard_resource_group: str = "resource-owner-dashboard"
    roster_path: str | None = None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _build(
    *,
    max_age_days: Any,
    lookback_days: Any,
    dashboard_resource_group: Any,
    roster_path: str | None = None,
) -> Config:
    """The one validator."""
    if not _is_int(max_age_days) or max_age_days < 1:
        raise ConfigError("max_age_days must be a whole number of at least 1")
    if not _is_int(lookback_days) or not 1 <= lookback_days <= 90:
        raise ConfigError("lookback_days must be 1..90 (the Activity Log keeps 90 days)")
    if not isinstance(dashboard_resource_group, str) or not GROUP_NAME_RE.fullmatch(
        dashboard_resource_group
    ):
        raise ConfigError("dashboard_resource_group is not a valid resource group name")
    return Config(
        max_age_days=max_age_days,
        lookback_days=lookback_days,
        dashboard_resource_group=dashboard_resource_group,
        roster_path=roster_path,
    )


def load_settings(path: str | os.PathLike[str] = SETTINGS_FILE) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found: run from the repo root or pass --settings") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON (line {exc.lineno})") from None
    if not isinstance(data, dict) or set(data) != SETTINGS_KEYS:
        raise ConfigError(f"{path} must have exactly the keys: {', '.join(sorted(SETTINGS_KEYS))}")
    return data


def load_config(
    path: str | None = None, settings_path: str | os.PathLike[str] = SETTINGS_FILE
) -> Config:
    """The committed settings.json plus the optional gitignored dashboard.toml."""
    local: dict[str, Any] = {}
    if path:
        try:
            with open(path, "rb") as fh:
                local = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from None
    extra = set(local) - LOCAL_KEYS
    if extra:
        raise ConfigError(f"{path}: unknown keys {', '.join(sorted(extra))}")
    return _build(**load_settings(settings_path), roster_path=local.get("roster_path"))
