import json
from pathlib import Path

import pytest

from dashboard.config import Config, ConfigError, load_config, load_settings

REPO = Path(__file__).resolve().parents[1]
GOOD = {
    "max_age_days": 4,
    "lookback_days": 90,
    "dashboard_resource_group": "resource-owner-dashboard",
}


def _settings(tmp_path, **changes):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(GOOD | changes))
    return str(path)


def test_committed_settings_file_has_the_course_defaults():
    assert load_settings(REPO / "settings.json") == GOOD
    assert load_config(settings_path=REPO / "settings.json") == Config()


def test_settings_and_local_file_combine(tmp_path):
    local = tmp_path / "dashboard.toml"
    local.write_text('roster_path = "roster.csv"\n')
    cfg = load_config(str(local), settings_path=_settings(tmp_path, max_age_days=7))
    assert (cfg.max_age_days, cfg.lookback_days) == (7, 90)
    assert cfg.dashboard_resource_group == "resource-owner-dashboard"
    assert cfg.roster_path == "roster.csv"


def test_local_file_is_optional(tmp_path):
    cfg = load_config(None, settings_path=_settings(tmp_path))
    assert cfg.roster_path is None


def test_local_file_rejects_unknown_keys(tmp_path):
    local = tmp_path / "dashboard.toml"
    local.write_text('max_age_days = 4\ncolour = "red"\n')
    with pytest.raises(ConfigError) as err:
        load_config(str(local), settings_path=_settings(tmp_path))
    assert "unknown keys colour, max_age_days" in str(err.value)


@pytest.mark.parametrize(
    "change",
    [{"lookback_days": None}, {"extra": 1}],
    ids=["missing", "extra"],
)
def test_settings_must_have_exactly_the_three_keys(tmp_path, change):
    data = {k: v for k, v in (GOOD | change).items() if v is not None}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="exactly the keys"):
        load_settings(path)


@pytest.mark.parametrize(
    "change",
    [
        {"lookback_days": 0},
        {"lookback_days": 91},
        {"max_age_days": True},
        {"max_age_days": 0},
    ],
)
def test_rejects_bad_numbers(tmp_path, change):
    with pytest.raises(ConfigError):
        load_config(settings_path=_settings(tmp_path, **change))


@pytest.mark.parametrize("name", ["", "rg/x", "a" * 91, "rg-" + "١", 42, "rg."])
def test_rejects_bad_dashboard_group_names(tmp_path, name):
    with pytest.raises(ConfigError):
        load_config(settings_path=_settings(tmp_path, dashboard_resource_group=name))


@pytest.mark.parametrize("name", ["resource-owner-dashboard", "rg_(lab).1"])
def test_accepts_good_dashboard_group_names(tmp_path, name):
    cfg = load_config(settings_path=_settings(tmp_path, dashboard_resource_group=name))
    assert cfg.dashboard_resource_group == name
