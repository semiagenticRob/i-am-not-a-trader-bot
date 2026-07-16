"""Config loading and validation for config/rulesets.yaml.

The YAML is the machine translation of STRATEGY.md. Validation is strict:
unknown keys are rejected so a typo can never silently disable a guard.
Hard ceilings live here as code constants — config can tighten them, never
loosen them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Absolute ceilings. Config values may be lower, never higher. Raising these
# is a deliberate code change reviewed by a human, not a config edit.
HARD_PER_TRADE_MAX_USD = 5.0
HARD_DAILY_LOSS_CAP_PCT = 10.0
HARD_MAX_LIVE_TRADES_PER_DAY = 40

VALID_STATUSES = {"shadow", "live", "pending_promotion", "retired"}
VALID_RULESETS = {"momentum_follow", "contrarian_fade", "skew_filter"}

RULES_BLOCK_RE = re.compile(r"<!-- rules:begin -->(.*?)<!-- rules:end -->", re.DOTALL)


class ConfigError(ValueError):
    """Raised when config/rulesets.yaml fails validation."""


@dataclass(frozen=True)
class RiskConfig:
    per_trade_max_usd: float
    daily_loss_cap_pct: float
    max_live_trades_per_day: int
    max_spread: float
    min_top_depth_usd: float
    max_quote_staleness_sec: float
    consecutive_failure_halt: int
    exit_before_sec: int


@dataclass(frozen=True)
class GateConfig:
    min_trades: int
    checkpoint_interval: int
    ci_level: float


@dataclass(frozen=True)
class KillConfig:
    drawdown_pct_of_allocation: float
    consecutive_daily_cap_hits: int


@dataclass(frozen=True)
class Variant:
    id: str
    ruleset: str
    status: str
    allocation_usd: float
    params: dict = field(hash=False)

    @property
    def active(self) -> bool:
        return self.status in ("shadow", "live", "pending_promotion")


@dataclass(frozen=True)
class Config:
    version: int
    strategy_md_version: str
    bankroll_usd: float
    reference_stake_usd: float
    risk: RiskConfig
    gate: GateConfig
    kill: KillConfig
    variants: tuple[Variant, ...]
    strategy_md_drift: bool = False

    @property
    def active_variants(self) -> tuple[Variant, ...]:
        return tuple(v for v in self.variants if v.active)

    @property
    def live_variants(self) -> tuple[Variant, ...]:
        return tuple(v for v in self.variants if v.status == "live")


def _require(mapping: dict, key: str, typ, section: str):
    if key not in mapping:
        raise ConfigError(f"missing required field '{key}' in {section}")
    value = mapping[key]
    if typ is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, typ) or isinstance(value, bool):
        raise ConfigError(f"field '{key}' in {section} must be {typ.__name__}")
    return value


def _reject_unknown(mapping: dict, allowed: set[str], section: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise ConfigError(f"unknown field(s) {sorted(unknown)} in {section}")


def rules_hash(strategy_md_text: str) -> str:
    """Hash of the rules section only — lesson appends never change it."""
    match = RULES_BLOCK_RE.search(strategy_md_text)
    if not match:
        raise ConfigError("STRATEGY.md is missing rules:begin/rules:end markers")
    return hashlib.sha256(match.group(1).encode()).hexdigest()


def _parse_risk(raw: dict) -> RiskConfig:
    allowed = {
        "per_trade_max_usd",
        "daily_loss_cap_pct",
        "max_live_trades_per_day",
        "max_spread",
        "min_top_depth_usd",
        "max_quote_staleness_sec",
        "consecutive_failure_halt",
        "exit_before_sec",
    }
    _reject_unknown(raw, allowed, "risk")
    cfg = RiskConfig(
        per_trade_max_usd=_require(raw, "per_trade_max_usd", float, "risk"),
        daily_loss_cap_pct=_require(raw, "daily_loss_cap_pct", float, "risk"),
        max_live_trades_per_day=_require(raw, "max_live_trades_per_day", int, "risk"),
        max_spread=_require(raw, "max_spread", float, "risk"),
        min_top_depth_usd=_require(raw, "min_top_depth_usd", float, "risk"),
        max_quote_staleness_sec=_require(raw, "max_quote_staleness_sec", float, "risk"),
        consecutive_failure_halt=_require(raw, "consecutive_failure_halt", int, "risk"),
        exit_before_sec=_require(raw, "exit_before_sec", int, "risk"),
    )
    if not 0 < cfg.per_trade_max_usd <= HARD_PER_TRADE_MAX_USD:
        raise ConfigError(
            f"risk.per_trade_max_usd must be in (0, {HARD_PER_TRADE_MAX_USD}] "
            f"(hard ceiling), got {cfg.per_trade_max_usd}"
        )
    if not 0 < cfg.daily_loss_cap_pct <= HARD_DAILY_LOSS_CAP_PCT:
        raise ConfigError(
            f"risk.daily_loss_cap_pct must be in (0, {HARD_DAILY_LOSS_CAP_PCT}], "
            f"got {cfg.daily_loss_cap_pct}"
        )
    if not 0 < cfg.max_live_trades_per_day <= HARD_MAX_LIVE_TRADES_PER_DAY:
        raise ConfigError(
            f"risk.max_live_trades_per_day must be in (0, {HARD_MAX_LIVE_TRADES_PER_DAY}], "
            f"got {cfg.max_live_trades_per_day}"
        )
    for name, lo, hi in (
        ("max_spread", 0, 0.5),
        ("max_quote_staleness_sec", 0, 60),
        ("exit_before_sec", 5, 120),
        ("consecutive_failure_halt", 1, 10),
    ):
        value = getattr(cfg, name)
        if not lo < value <= hi:
            raise ConfigError(f"risk.{name} must be in ({lo}, {hi}], got {value}")
    return cfg


def _parse_gate(raw: dict) -> GateConfig:
    _reject_unknown(raw, {"min_trades", "checkpoint_interval", "ci_level"}, "gate")
    cfg = GateConfig(
        min_trades=_require(raw, "min_trades", int, "gate"),
        checkpoint_interval=_require(raw, "checkpoint_interval", int, "gate"),
        ci_level=_require(raw, "ci_level", float, "gate"),
    )
    if cfg.min_trades < 100:
        raise ConfigError(f"gate.min_trades must be >= 100, got {cfg.min_trades}")
    if cfg.checkpoint_interval < 1:
        raise ConfigError("gate.checkpoint_interval must be >= 1")
    if not 0.5 < cfg.ci_level < 1:
        raise ConfigError(f"gate.ci_level must be in (0.5, 1), got {cfg.ci_level}")
    return cfg


def _parse_kill(raw: dict) -> KillConfig:
    _reject_unknown(raw, {"drawdown_pct_of_allocation", "consecutive_daily_cap_hits"}, "kill")
    cfg = KillConfig(
        drawdown_pct_of_allocation=_require(raw, "drawdown_pct_of_allocation", float, "kill"),
        consecutive_daily_cap_hits=_require(raw, "consecutive_daily_cap_hits", int, "kill"),
    )
    if not 0 < cfg.drawdown_pct_of_allocation <= 100:
        raise ConfigError("kill.drawdown_pct_of_allocation must be in (0, 100]")
    if cfg.consecutive_daily_cap_hits < 1:
        raise ConfigError("kill.consecutive_daily_cap_hits must be >= 1")
    return cfg


def _parse_variant(raw: dict, index: int) -> Variant:
    section = f"variants[{index}]"
    _reject_unknown(raw, {"id", "ruleset", "status", "allocation_usd", "params"}, section)
    variant = Variant(
        id=_require(raw, "id", str, section),
        ruleset=_require(raw, "ruleset", str, section),
        status=_require(raw, "status", str, section),
        allocation_usd=_require(raw, "allocation_usd", float, section),
        params=_require(raw, "params", dict, section),
    )
    if variant.ruleset not in VALID_RULESETS:
        raise ConfigError(f"{section}.ruleset '{variant.ruleset}' not in {sorted(VALID_RULESETS)}")
    if variant.status not in VALID_STATUSES:
        raise ConfigError(f"{section}.status '{variant.status}' not in {sorted(VALID_STATUSES)}")
    if variant.allocation_usd < 0:
        raise ConfigError(f"{section}.allocation_usd must be >= 0")
    if variant.status == "shadow" and variant.allocation_usd != 0:
        raise ConfigError(f"{section}: shadow variants must have allocation_usd 0")
    return variant


def load_config(path: Path | str, strategy_md_path: Path | str | None = None) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    _reject_unknown(
        raw,
        {
            "version",
            "strategy_md_version",
            "bankroll_usd",
            "reference_stake_usd",
            "risk",
            "gate",
            "kill",
            "variants",
        },
        "root",
    )
    raw_variants = _require(raw, "variants", list, "root")
    variants = tuple(_parse_variant(v, i) for i, v in enumerate(raw_variants))
    ids = [v.id for v in variants]
    if len(ids) != len(set(ids)):
        raise ConfigError("variant ids must be unique")

    strategy_md_version = _require(raw, "strategy_md_version", str, "root")
    drift = False
    if strategy_md_path is not None:
        actual = rules_hash(Path(strategy_md_path).read_text())
        drift = actual != strategy_md_version

    bankroll = _require(raw, "bankroll_usd", float, "root")
    reference_stake = _require(raw, "reference_stake_usd", float, "root")
    if bankroll <= 0:
        raise ConfigError("bankroll_usd must be > 0")
    if not 0 < reference_stake <= HARD_PER_TRADE_MAX_USD:
        raise ConfigError(f"reference_stake_usd must be in (0, {HARD_PER_TRADE_MAX_USD}]")

    return Config(
        version=_require(raw, "version", int, "root"),
        strategy_md_version=strategy_md_version,
        bankroll_usd=bankroll,
        reference_stake_usd=reference_stake,
        risk=_parse_risk(_require(raw, "risk", dict, "root")),
        gate=_parse_gate(_require(raw, "gate", dict, "root")),
        kill=_parse_kill(_require(raw, "kill", dict, "root")),
        variants=variants,
        strategy_md_drift=drift,
    )


def check_frozen_variants(new: Config, prior: Config) -> None:
    """Live variants' params are frozen: reject any param edit while live.

    Evolution adapts by spawning shadow challengers, never by mutating a
    funded variant — otherwise results can't be attributed to anything.
    """
    prior_by_id = {v.id: v for v in prior.variants}
    for variant in new.variants:
        old = prior_by_id.get(variant.id)
        if old is None:
            continue
        if old.status == "live" and variant.status == "live" and variant.params != old.params:
            raise ConfigError(
                f"variant '{variant.id}' is live; params are frozen while live "
                "(spawn a challenger instead)"
            )
