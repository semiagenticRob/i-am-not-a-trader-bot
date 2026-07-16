"""U1: config loader validation."""

import copy
from pathlib import Path

import pytest
import yaml

from engine.config import (
    Config,
    ConfigError,
    check_frozen_variants,
    load_config,
    rules_hash,
)

REPO = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO / "config" / "rulesets.yaml"
STRATEGY_PATH = REPO / "STRATEGY.md"


@pytest.fixture()
def raw_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


def write_config(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "rulesets.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def test_seed_config_loads_with_three_shadow_variants():
    cfg = load_config(CONFIG_PATH)
    assert isinstance(cfg, Config)
    assert len(cfg.variants) == 3
    assert {v.ruleset for v in cfg.variants} == {
        "momentum_follow",
        "contrarian_fade",
        "skew_filter",
    }
    assert all(v.status == "shadow" for v in cfg.variants)
    assert len(cfg.active_variants) == 3
    assert cfg.live_variants == ()


def test_unknown_key_rejected_with_field_name(tmp_path, raw_config):
    raw_config["risk"]["max_sprad"] = 0.03  # typo must not silently pass
    del raw_config["risk"]["max_spread"]
    with pytest.raises(ConfigError, match="max_sprad"):
        load_config(write_config(tmp_path, raw_config))


def test_missing_required_field_names_the_field(tmp_path, raw_config):
    del raw_config["risk"]["daily_loss_cap_pct"]
    with pytest.raises(ConfigError, match="daily_loss_cap_pct"):
        load_config(write_config(tmp_path, raw_config))


def test_stake_above_hard_ceiling_rejected(tmp_path, raw_config):
    raw_config["risk"]["per_trade_max_usd"] = 50.0
    with pytest.raises(ConfigError, match="per_trade_max_usd"):
        load_config(write_config(tmp_path, raw_config))


def test_gate_below_100_trades_rejected(tmp_path, raw_config):
    raw_config["gate"]["min_trades"] = 50
    with pytest.raises(ConfigError, match="min_trades"):
        load_config(write_config(tmp_path, raw_config))


def test_retired_variant_excluded_from_active_set(tmp_path, raw_config):
    raw_config["variants"][0]["status"] = "retired"
    cfg = load_config(write_config(tmp_path, raw_config))
    assert len(cfg.variants) == 3
    assert len(cfg.active_variants) == 2
    assert all(v.status != "retired" for v in cfg.active_variants)


def test_shadow_variant_with_allocation_rejected(tmp_path, raw_config):
    raw_config["variants"][0]["allocation_usd"] = 100.0
    with pytest.raises(ConfigError, match="allocation_usd"):
        load_config(write_config(tmp_path, raw_config))


def test_duplicate_variant_ids_rejected(tmp_path, raw_config):
    raw_config["variants"][1]["id"] = raw_config["variants"][0]["id"]
    with pytest.raises(ConfigError, match="unique"):
        load_config(write_config(tmp_path, raw_config))


def test_frozen_while_live_rejects_param_edit(tmp_path, raw_config):
    raw_config["variants"][0]["status"] = "live"
    raw_config["variants"][0]["allocation_usd"] = 100.0
    prior = load_config(write_config(tmp_path, raw_config))

    edited = copy.deepcopy(raw_config)
    edited["variants"][0]["params"]["min_impulse_usd"] = 90.0
    new = load_config(write_config(tmp_path, edited))
    with pytest.raises(ConfigError, match="frozen"):
        check_frozen_variants(new, prior)


def test_frozen_check_allows_shadow_param_edit(tmp_path, raw_config):
    prior = load_config(write_config(tmp_path, raw_config))
    edited = copy.deepcopy(raw_config)
    edited["variants"][0]["params"]["min_impulse_usd"] = 90.0
    new = load_config(write_config(tmp_path, edited))
    check_frozen_variants(new, prior)  # must not raise


def test_strategy_md_drift_detected(tmp_path, raw_config):
    raw_config["strategy_md_version"] = rules_hash(STRATEGY_PATH.read_text())
    cfg = load_config(write_config(tmp_path, raw_config), STRATEGY_PATH)
    assert cfg.strategy_md_drift is False

    raw_config["strategy_md_version"] = "0" * 64
    cfg = load_config(write_config(tmp_path, raw_config), STRATEGY_PATH)
    assert cfg.strategy_md_drift is True


def test_rules_hash_ignores_lessons_appends(tmp_path):
    text = STRATEGY_PATH.read_text()
    appended = text + "\n\n### Lesson 2026-07-15\nSome machine-appended lesson.\n"
    assert rules_hash(text) == rules_hash(appended)


def test_rules_hash_changes_when_rules_change():
    text = STRATEGY_PATH.read_text()
    edited = text.replace("at most 20 live trades", "at most 21 live trades", 1)
    if edited == text:  # guard: replacement target must exist
        edited = text.replace("<!-- rules:end -->", "extra rule\n<!-- rules:end -->")
    assert rules_hash(text) != rules_hash(edited)
