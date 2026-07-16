"""U6: risk containment — the single choke point between Decision and execution.

Behaviors pinned here that the spec left open:
- pending_promotion variants trade in 'shadow' mode (only status == 'live' is live)
- spread comparison is inclusive at max_spread, with the computed spread rounded
  to 9 decimals so float representation noise (0.74 - 0.71 != 0.03) cannot flip
  an at-the-boundary quote into a rejection
- depth comparison is inclusive at min_top_depth_usd
- remaining live allocation = allocation_usd + realized live pnl - open live stake
- win_prob <= price (Kelly f <= 0) -> 'sized_to_zero'; only a missing estimate
  is 'no_positive_edge'
- halt-class risk events dedup in-memory (one per STOP appearance / failure trip /
  loss-cap day); STOP removal re-arms the dedup so a reappearance records again
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine.config import (
    HARD_PER_TRADE_MAX_USD,
    Config,
    GateConfig,
    KillConfig,
    RiskConfig,
    Variant,
)
from engine.ledger import Ledger
from engine.risk import OPERATOR_TZ, Approved, Rejected, RiskManager
from engine.rulesets import Decision
from engine.signals import FeatureSnapshot

SLUG = "btc-updown-5m-test"
BUCKET = 1_752_500_000
DAY_SEC = 86_400
# Fixed 'now': noon in the operator's timezone, so every seeded same-day trade
# is unambiguously inside the operator calendar day.
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo(OPERATOR_TZ)).timestamp()

CONFIG = Config(
    version=1,
    strategy_md_version="test",
    bankroll_usd=750.0,
    reference_stake_usd=5.0,
    risk=RiskConfig(
        per_trade_max_usd=5.0,
        daily_loss_cap_pct=5.0,
        max_live_trades_per_day=20,
        max_spread=0.03,
        min_top_depth_usd=30.0,
        max_quote_staleness_sec=8.0,
        consecutive_failure_halt=3,
        exit_before_sec=20,
    ),
    gate=GateConfig(min_trades=100, checkpoint_interval=50, ci_level=0.95),
    kill=KillConfig(drawdown_pct_of_allocation=20.0, consecutive_daily_cap_hits=3),
    variants=(),
)

ENTER_UP = Decision(action="enter", side="up", limit_price=0.70, reason="entered_momentum_up")
ENTER_DOWN = Decision(action="enter", side="down", limit_price=0.26, reason="entered_fade_down")
SKIP = Decision(action="skip", side=None, limit_price=None, reason="skip_no_favorite")


def snap(**overrides) -> FeatureSnapshot:
    """Canonical guard-passing snapshot: tight book, deep top, 100s to close."""
    defaults = dict(
        bucket_ts=BUCKET,
        market_slug=SLUG,
        seconds_to_close=100.0,
        btc_open=118_000.0,
        btc_last=118_100.0,
        up_best_bid=0.68,
        up_best_ask=0.70,
        down_best_bid=0.24,
        down_best_ask=0.26,
        up_bid_depth_usd=500.0,
        up_ask_depth_usd=500.0,
        down_bid_depth_usd=200.0,
        down_ask_depth_usd=200.0,
        fee_rate=0.0,
        fees_enabled=False,
        quote_stale=False,
        spot_stale=False,
    )
    defaults.update(overrides)
    return FeatureSnapshot(**defaults)


def variant(vid: str = "v-shadow", status: str = "shadow", allocation: float = 0.0) -> Variant:
    return Variant(
        id=vid, ruleset="momentum_follow", status=status, allocation_usd=allocation, params={}
    )


def live_variant(vid: str = "v-live", allocation: float = 100.0) -> Variant:
    return variant(vid, "live", allocation)


def risk_events(ledger: Ledger, kind: str) -> list:
    return ledger._conn.execute(
        "SELECT ts, kind, detail FROM risk_events WHERE kind = ?", (kind,)
    ).fetchall()


def seed_live_loss(ledger: Ledger, vid: str, stake: float, ts: float, bucket: int) -> None:
    """One filled live trade @ 0.5 on the losing side: realized pnl == -stake."""
    ledger.record_trade(
        ts=ts,
        bucket_ts=bucket,
        variant_id=vid,
        market_slug=SLUG,
        side="up",
        mode="live",
        intended_price=0.5,
        stake_usd=stake,
        status="filled",
        filled_price=0.5,
    )
    ledger.record_resolution(bucket_ts=bucket, market_slug=SLUG, outcome="down", resolved_ts=ts)


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    yield led
    led.close()


@pytest.fixture
def runtime_dir(tmp_path):
    d = tmp_path / "runtime"
    d.mkdir()
    return d


@pytest.fixture
def rm(ledger, runtime_dir):
    return RiskManager(CONFIG, ledger, runtime_dir)


# ---------------------------------------------------------------------------
# Approved / Rejected types
# ---------------------------------------------------------------------------


def test_approved_rejects_zero_stake():
    with pytest.raises(ValueError, match="stake"):
        Approved("v", BUCKET, SLUG, "up", 0.70, 0.0, "shadow")


def test_approved_rejects_negative_stake():
    with pytest.raises(ValueError, match="stake"):
        Approved("v", BUCKET, SLUG, "up", 0.70, -1.0, "live")


def test_approved_rejects_stake_over_hard_max():
    with pytest.raises(ValueError, match="stake"):
        Approved("v", BUCKET, SLUG, "up", 0.70, HARD_PER_TRADE_MAX_USD + 0.01, "live")


def test_approved_at_hard_max_is_allowed():
    approved = Approved("v", BUCKET, SLUG, "up", 0.70, HARD_PER_TRADE_MAX_USD, "live")
    assert approved.stake_usd == HARD_PER_TRADE_MAX_USD


def test_no_other_module_constructs_approved():
    # Adversarial: Approved is the executor's only acceptable input, so risk.py
    # must be the only module that constructs one. This protects the choke
    # point; when the executor lands (U7) it must still not construct Approved.
    engine_dir = Path(__file__).resolve().parent.parent / "engine"
    offenders = [
        path.name
        for path in engine_dir.glob("*.py")
        if path.name != "risk.py" and "Approved(" in path.read_text()
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# happy paths + sizing
# ---------------------------------------------------------------------------


def test_shadow_happy_path_fixed_reference_stake(rm):
    result = rm.check(variant(), ENTER_UP, snap(), NOW)
    assert isinstance(result, Approved)
    assert result == Approved(
        variant_id="v-shadow",
        bucket_ts=BUCKET,
        market_slug=SLUG,
        side="up",
        limit_price=0.70,
        stake_usd=5.0,
        mode="shadow",
    )


def test_pending_promotion_trades_as_shadow(rm):
    result = rm.check(variant("v-pp", "pending_promotion"), ENTER_UP, snap(), NOW)
    assert isinstance(result, Approved)
    assert result.mode == "shadow"
    assert result.stake_usd == 5.0


def test_shadow_stake_ignores_win_prob(rm):
    # Shadow is never Kelly-sized: evidence generation must be independent of
    # any edge estimate.
    result = rm.check(variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.99)
    assert isinstance(result, Approved)
    assert result.stake_usd == 5.0


def test_live_happy_path_quarter_kelly(rm):
    # f = (0.75 - 0.70) / (1 - 0.70) = 0.1667; stake = 0.25 * f * 100 = 4.1667
    result = rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.75)
    assert isinstance(result, Approved)
    assert result.mode == "live"
    assert result.stake_usd == pytest.approx(4.1667, abs=1e-3)


def test_live_clamped_by_per_trade_max(rm):
    # allocation 2000 -> raw quarter-Kelly 83.3; 2% of bankroll is 15, so the
    # $5 per-trade max binds.
    result = rm.check(
        live_variant(allocation=2000.0), ENTER_UP, snap(), NOW, win_prob_estimate=0.75
    )
    assert isinstance(result, Approved)
    assert result.stake_usd == 5.0


def test_live_clamped_by_remaining_allocation(rm, ledger):
    # $97 of the $100 allocation already lost (on a prior day, so daily caps
    # stay clear): only $3 remains and the stake clamps to it.
    seed_live_loss(ledger, "v-live", stake=97.0, ts=NOW - 2 * DAY_SEC, bucket=BUCKET - 3000)
    result = rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.75)
    assert isinstance(result, Approved)
    assert result.stake_usd == pytest.approx(3.0)


def test_live_missing_win_prob_rejected(rm):
    result = rm.check(live_variant(), ENTER_UP, snap(), NOW)
    assert result == Rejected("v-live", BUCKET, "no_positive_edge")


def test_live_win_prob_equal_to_price_sized_to_zero(rm):
    result = rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.70)
    assert result == Rejected("v-live", BUCKET, "sized_to_zero")


def test_live_negative_edge_never_yields_a_stake(rm):
    # Kelly f < 0 must reject, never construct an Approved (whose constructor
    # would refuse the negative stake anyway — defense in depth).
    result = rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.40)
    assert result == Rejected("v-live", BUCKET, "sized_to_zero")


def test_skip_decision_rejected_defensively(rm):
    result = rm.check(variant(), SKIP, snap(), NOW)
    assert result == Rejected("v-shadow", BUCKET, "not_an_entry")


# ---------------------------------------------------------------------------
# one-entry-per-bucket
# ---------------------------------------------------------------------------


def test_second_entry_same_bucket_rejected(rm, ledger):
    ledger.record_trade(
        ts=NOW,
        bucket_ts=BUCKET,
        variant_id="v-shadow",
        market_slug=SLUG,
        side="up",
        mode="shadow",
        intended_price=0.70,
        stake_usd=5.0,
    )
    result = rm.check(variant(), ENTER_UP, snap(), NOW)
    assert result == Rejected("v-shadow", BUCKET, "already_entered_bucket")


def test_different_variant_same_bucket_approved(rm, ledger):
    ledger.record_trade(
        ts=NOW,
        bucket_ts=BUCKET,
        variant_id="v-shadow",
        market_slug=SLUG,
        side="up",
        mode="shadow",
        intended_price=0.70,
        stake_usd=5.0,
    )
    result = rm.check(variant("v-other"), ENTER_UP, snap(), NOW)
    assert isinstance(result, Approved)


def test_same_variant_next_bucket_approved(rm, ledger):
    ledger.record_trade(
        ts=NOW,
        bucket_ts=BUCKET,
        variant_id="v-shadow",
        market_slug=SLUG,
        side="up",
        mode="shadow",
        intended_price=0.70,
        stake_usd=5.0,
    )
    result = rm.check(variant(), ENTER_UP, snap(bucket_ts=BUCKET + 300), NOW)
    assert isinstance(result, Approved)


def test_bucket_check_is_mode_scoped(rm, ledger):
    # A shadow row for the same (bucket, variant) does not block a live entry:
    # the invariant is per (bucket, variant, mode).
    ledger.record_trade(
        ts=NOW,
        bucket_ts=BUCKET,
        variant_id="v-live",
        market_slug=SLUG,
        side="up",
        mode="shadow",
        intended_price=0.70,
        stake_usd=5.0,
    )
    result = rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.75)
    assert isinstance(result, Approved)


def test_live_second_entry_same_bucket_rejected(rm, ledger):
    ledger.record_trade(
        ts=NOW,
        bucket_ts=BUCKET,
        variant_id="v-live",
        market_slug=SLUG,
        side="up",
        mode="live",
        intended_price=0.70,
        stake_usd=5.0,
    )
    result = rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.75)
    assert result == Rejected("v-live", BUCKET, "already_entered_bucket")


# ---------------------------------------------------------------------------
# market guards
# ---------------------------------------------------------------------------


def test_stale_quote_rejected(rm):
    result = rm.check(variant(), ENTER_UP, snap(quote_stale=True), NOW)
    assert result == Rejected("v-shadow", BUCKET, "stale_quote")


def test_spread_over_max_rejected(rm):
    result = rm.check(variant(), ENTER_UP, snap(up_best_bid=0.669), NOW)  # spread 0.031
    assert result == Rejected("v-shadow", BUCKET, "spread_too_wide")


def test_spread_exactly_at_max_approved(rm):
    # 0.70 - 0.67 is not exactly 0.03 in floats; the module must still treat
    # an at-the-boundary spread as inside the guard (inclusive).
    result = rm.check(variant(), ENTER_UP, snap(up_best_bid=0.67), NOW)
    assert isinstance(result, Approved)


def test_missing_bid_means_unknown_spread_rejected(rm):
    result = rm.check(variant(), ENTER_UP, snap(up_best_bid=None), NOW)
    assert result == Rejected("v-shadow", BUCKET, "spread_too_wide")


def test_guards_apply_to_chosen_side(rm):
    # UP book is pristine, but the decision buys DOWN whose spread is wide.
    result = rm.check(variant(), ENTER_DOWN, snap(down_best_bid=0.20), NOW)
    assert result == Rejected("v-shadow", BUCKET, "spread_too_wide")


def test_thin_book_rejected(rm):
    result = rm.check(variant(), ENTER_UP, snap(up_ask_depth_usd=29.99), NOW)
    assert result == Rejected("v-shadow", BUCKET, "book_too_thin")


def test_depth_exactly_at_min_approved(rm):
    result = rm.check(variant(), ENTER_UP, snap(up_ask_depth_usd=30.0), NOW)
    assert isinstance(result, Approved)


def test_too_close_to_close_rejected(rm):
    # exit_before_sec 20 + 5s margin: below 25s there is no room to exit.
    result = rm.check(variant(), ENTER_UP, snap(seconds_to_close=24.9), NOW)
    assert result == Rejected("v-shadow", BUCKET, "too_close_to_close")


def test_exactly_at_exit_margin_approved(rm):
    result = rm.check(variant(), ENTER_UP, snap(seconds_to_close=25.0), NOW)
    assert isinstance(result, Approved)


# ---------------------------------------------------------------------------
# daily loss cap (live only)
# ---------------------------------------------------------------------------


def test_daily_loss_exactly_at_cap_halts_live(rm, ledger):
    # -5% of 750 = -37.5 realized today: at the cap counts as hit (inclusive).
    seed_live_loss(ledger, "v-live", stake=37.5, ts=NOW - 600, bucket=BUCKET - 3000)
    result = rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.75)
    assert result == Rejected("v-live", BUCKET, "daily_loss_cap")


def test_daily_loss_halt_event_recorded_once(rm, ledger):
    seed_live_loss(ledger, "v-live", stake=37.5, ts=NOW - 600, bucket=BUCKET - 3000)
    rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.75)
    rm.check(live_variant(), ENTER_UP, snap(bucket_ts=BUCKET + 300), NOW, win_prob_estimate=0.75)
    assert len(risk_events(ledger, "daily_loss_halt")) == 1


def test_daily_loss_cap_ignores_shadow(rm, ledger):
    seed_live_loss(ledger, "v-live", stake=37.5, ts=NOW - 600, bucket=BUCKET - 3000)
    result = rm.check(variant(), ENTER_UP, snap(), NOW)
    assert isinstance(result, Approved)


def test_daily_loss_cap_rearms_on_day_rollover(rm, ledger):
    seed_live_loss(ledger, "v-live", stake=37.5, ts=NOW - 600, bucket=BUCKET - 3000)
    assert isinstance(
        rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.75), Rejected
    )
    result = rm.check(live_variant(), ENTER_UP, snap(), NOW + DAY_SEC, win_prob_estimate=0.75)
    assert isinstance(result, Approved)


def test_loss_just_inside_cap_still_trades(rm, ledger):
    seed_live_loss(ledger, "v-live", stake=37.49, ts=NOW - 600, bucket=BUCKET - 3000)
    result = rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.75)
    assert isinstance(result, Approved)


# ---------------------------------------------------------------------------
# daily trade cap (live only)
# ---------------------------------------------------------------------------


def seed_live_trades(ledger: Ledger, n: int) -> None:
    for i in range(n):
        ledger.record_trade(
            ts=NOW - 3600,
            bucket_ts=BUCKET - 300 * (i + 10),
            variant_id="v-live",
            market_slug=SLUG,
            side="up",
            mode="live",
            intended_price=0.70,
            stake_usd=5.0,
        )


def test_twentieth_live_trade_approved(rm, ledger):
    seed_live_trades(ledger, 19)
    result = rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.75)
    assert isinstance(result, Approved)


def test_twenty_first_live_trade_rejected(rm, ledger):
    seed_live_trades(ledger, 20)
    result = rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.75)
    assert result == Rejected("v-live", BUCKET, "daily_trade_cap")


def test_daily_trade_cap_ignores_shadow(rm, ledger):
    seed_live_trades(ledger, 20)
    result = rm.check(variant(), ENTER_UP, snap(), NOW)
    assert isinstance(result, Approved)


# ---------------------------------------------------------------------------
# STOP file
# ---------------------------------------------------------------------------


def test_stop_file_rejects_everything(rm, ledger, runtime_dir):
    (runtime_dir / "STOP").touch()
    assert rm.check(variant(), ENTER_UP, snap(), NOW) == Rejected(
        "v-shadow", BUCKET, "stop_file_present"
    )
    assert rm.check(live_variant(), ENTER_UP, snap(), NOW, win_prob_estimate=0.75) == Rejected(
        "v-live", BUCKET, "stop_file_present"
    )
    assert rm.halted(NOW) == "stop_file_present"


def test_stop_event_recorded_once_per_appearance(rm, ledger, runtime_dir):
    stop = runtime_dir / "STOP"
    stop.touch()
    rm.check(variant(), ENTER_UP, snap(), NOW)
    rm.check(variant(), ENTER_UP, snap(), NOW + 5)
    assert len(risk_events(ledger, "stop_halt")) == 1

    stop.unlink()
    result = rm.check(variant(), ENTER_UP, snap(), NOW + 10)  # resumes, same manager
    assert isinstance(result, Approved)

    stop.touch()  # a new appearance is a new halt event
    rm.check(variant(), ENTER_UP, snap(bucket_ts=BUCKET + 300), NOW + 15)
    assert len(risk_events(ledger, "stop_halt")) == 2


def test_stop_removal_resumes_without_new_manager(rm, runtime_dir):
    stop = runtime_dir / "STOP"
    stop.touch()
    assert isinstance(rm.check(variant(), ENTER_UP, snap(), NOW), Rejected)
    stop.unlink()
    assert isinstance(rm.check(variant(), ENTER_UP, snap(), NOW + 5), Approved)
    assert rm.halted(NOW + 5) is None


# ---------------------------------------------------------------------------
# consecutive-failure halt
# ---------------------------------------------------------------------------


def test_three_failures_halt(rm, ledger):
    for _ in range(3):
        rm.record_failure()
    assert rm.halted(NOW) == "failure_halt"
    result = rm.check(variant(), ENTER_UP, snap(), NOW)
    assert result == Rejected("v-shadow", BUCKET, "failure_halt")
    assert len(risk_events(ledger, "failure_halt")) == 1


def test_failure_halt_event_not_repeated_while_tripped(rm, ledger):
    for _ in range(5):
        rm.record_failure()
    assert len(risk_events(ledger, "failure_halt")) == 1


def test_success_resets_failure_counter(rm):
    rm.record_failure()
    rm.record_failure()
    rm.record_success()
    assert rm.halted(NOW) is None
    assert isinstance(rm.check(variant(), ENTER_UP, snap(), NOW), Approved)


def test_new_failure_trip_after_reset_records_again(rm, ledger):
    for _ in range(3):
        rm.record_failure()
    rm.record_success()
    for _ in range(3):
        rm.record_failure()
    assert len(risk_events(ledger, "failure_halt")) == 2
