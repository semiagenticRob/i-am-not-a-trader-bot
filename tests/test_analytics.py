"""U8: analytics — per-variant stats, diagnostics, funding/kill gates, reports."""

import json
from pathlib import Path

import pytest

from analytics.report import (
    VariantStats,
    compute_variant_stats,
    fundable_at_checkpoint,
    generate_report,
    paired_comparison,
    should_kill,
    wilson_interval,
)
from engine.config import Config, GateConfig, KillConfig, RiskConfig, Variant
from engine.ledger import Ledger

BUCKET0 = 1_752_580_800  # 5-minute-aligned unix ts
STAKE = 5.0
NOW = 1_752_580_800.0


@pytest.fixture()
def ledger(tmp_path: Path) -> Ledger:
    lgr = Ledger(tmp_path / "ledger.db")
    yield lgr
    lgr.close()


def bucket(i: int) -> int:
    return BUCKET0 + 300 * i


def slug(i: int) -> str:
    return f"btc-updown-5m-{bucket(i)}"


def seed_trade(
    ledger: Ledger,
    variant_id: str,
    i: int,
    side: str = "up",
    price: float = 0.5,
    fee: float = 0.0,
    stake: float = STAKE,
    status: str = "filled",
) -> int:
    b = bucket(i)
    trade_id = ledger.record_trade(
        ts=b + 60.0,
        bucket_ts=b,
        variant_id=variant_id,
        market_slug=slug(i),
        side=side,
        mode="shadow",
        intended_price=price,
        stake_usd=stake,
    )
    filled = price if status == "filled" else None
    ledger.update_trade_fill(trade_id, filled_price=filled, fee_usd=fee, status=status)
    return trade_id


def resolve(ledger: Ledger, i: int, outcome: str) -> None:
    ledger.record_resolution(bucket(i), slug(i), outcome, resolved_ts=bucket(i) + 300.0)


def seed_pattern(
    ledger: Ledger,
    variant_id: str,
    wins: list[bool],
    start: int = 0,
    price: float = 0.5,
    fee: float = 0.0,
) -> None:
    """One resolved trade per bool: buy 'up' at `price` with a $5 stake.

    At price 0.5, fee f: win pnl = +5 - f, loss pnl = -5 - f.
    """
    for j, win in enumerate(wins):
        i = start + j
        seed_trade(ledger, variant_id, i, price=price, fee=fee)
        resolve(ledger, i, "up" if win else "down")


def make_config(variant_ids: tuple[str, ...] = ("var-a",)) -> Config:
    return Config(
        version=1,
        strategy_md_version="deadbeef",
        bankroll_usd=750.0,
        reference_stake_usd=STAKE,
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
        variants=tuple(
            Variant(id=v, ruleset="momentum_follow", status="shadow", allocation_usd=0.0, params={})
            for v in variant_ids
        ),
    )


GATE = GateConfig(min_trades=100, checkpoint_interval=50, ci_level=0.95)
KILL = KillConfig(drawdown_pct_of_allocation=20.0, consecutive_daily_cap_hits=3)


def stats_stub(**over) -> VariantStats:
    base = dict(
        variant_id="stub",
        n=150,
        mean_pnl=1.0,
        ci_low=0.5,
        ci_high=1.5,
        win_rate=0.6,
        wilson_low=0.52,
        wilson_high=0.68,
        max_drawdown=5.0,
        total_fees=1.0,
        exit_before_resolution_rate=0.0,
        median_hold_sec=240.0,
        limit_order_pct=1.0,
        size_edge_correlation=None,
        profit_source={"up": 1.0, "down": 0.0},
    )
    base.update(over)
    return VariantStats(**base)


# -- basic statistics ----------------------------------------------------------


def test_hand_computed_mean_win_rate_fees_and_hold(ledger):
    # win +4.9, loss -5.1, win +4.9 (price 0.5, fee 0.1)
    seed_pattern(ledger, "var-a", [True, False, True], fee=0.1)
    stats = compute_variant_stats(ledger, "var-a")
    assert stats.n == 3
    assert stats.mean_pnl == pytest.approx((4.9 - 5.1 + 4.9) / 3)
    assert stats.win_rate == pytest.approx(2 / 3)
    assert stats.total_fees == pytest.approx(0.3)
    # entered at bucket+60, resolved at bucket+300 -> 240s hold
    assert stats.median_hold_sec == pytest.approx(240.0)
    assert stats.limit_order_pct == 1.0
    assert stats.size_edge_correlation is None  # all stakes equal by design (shadow)
    assert stats.exit_before_resolution_rate == 0.0
    # profit attributed by outcome side: wins resolved 'up', the loss 'down'
    assert stats.profit_source["up"] == pytest.approx(9.8)
    assert stats.profit_source["down"] == pytest.approx(-5.1)


def test_bootstrap_ci_brackets_true_mean_for_60pct_variant(ledger):
    wins = [True] * 120 + [False] * 80  # n=200, sample mean exactly +1.0
    seed_pattern(ledger, "var-a", wins)
    stats = compute_variant_stats(ledger, "var-a")
    assert stats.n == 200
    assert stats.mean_pnl == pytest.approx(1.0)
    assert stats.ci_low < stats.mean_pnl < stats.ci_high
    assert stats.ci_low < 1.0 < stats.ci_high  # brackets the true mean


def test_stats_are_reproducible_across_runs(ledger):
    seed_pattern(ledger, "var-a", [True] * 30 + [False] * 20)
    first = compute_variant_stats(ledger, "var-a")
    second = compute_variant_stats(ledger, "var-a")
    assert first == second  # fixed seed: identical VariantStats, bit for bit


def test_max_drawdown_hand_computed(ledger):
    # pnl +5 +5 -5 -5 -5 +5 -> cum 5,10,5,0,-5,0 -> peak 10, trough -5 -> dd 15
    seed_pattern(ledger, "var-a", [True, True, False, False, False, True])
    stats = compute_variant_stats(ledger, "var-a")
    assert stats.max_drawdown == pytest.approx(15.0)


def test_all_wins_variant_wilson_and_degenerate_bootstrap(ledger):
    seed_pattern(ledger, "var-a", [True] * 10)
    stats = compute_variant_stats(ledger, "var-a")
    assert stats.win_rate == 1.0
    assert 0.5 < stats.wilson_low < 1.0  # no div-by-zero, informative lower bound
    assert stats.wilson_low < stats.wilson_high <= 1.0
    # every resample of identical pnls has the same mean: degenerate but defined
    assert stats.ci_low == stats.ci_high == pytest.approx(stats.mean_pnl)


def test_zero_trade_variant_renders_and_is_inert(ledger, tmp_path):
    stats = compute_variant_stats(ledger, "ghost")
    assert stats.n == 0
    assert stats.mean_pnl is None
    assert stats.ci_low is None and stats.ci_high is None
    assert stats.win_rate is None
    assert stats.wilson_low is None and stats.wilson_high is None
    assert stats.max_drawdown == 0.0
    assert stats.total_fees == 0.0
    assert stats.median_hold_sec is None
    assert stats.exit_before_resolution_rate is None
    assert stats.profit_source == {"up": 0.0, "down": 0.0}
    assert fundable_at_checkpoint(ledger, "ghost", GATE, make_config(("ghost",))) == (False, None)
    assert should_kill(stats, KILL, allocation_usd=100.0, consecutive_daily_cap_hits=0) is None
    report = generate_report(ledger, make_config(("ghost",)), tmp_path / "reports", now=NOW)
    assert report["variants"][0]["stats"]["n"] == 0
    assert (tmp_path / "reports" / "latest.json").exists()


def test_voided_and_unresolved_timeout_excluded_from_n(ledger):
    seed_pattern(ledger, "var-a", [True] * 5)  # 5 real resolutions
    seed_trade(ledger, "var-a", 5)
    resolve(ledger, 5, "voided")
    seed_trade(ledger, "var-a", 6)
    resolve(ledger, 6, "unresolved_timeout")
    seed_trade(ledger, "var-a", 7, status="cancelled")  # exit before close
    stats = compute_variant_stats(ledger, "var-a")
    assert stats.n == 5  # only real outcomes count
    assert stats.exit_before_resolution_rate == pytest.approx(1 / 8)


# -- funding gate: checkpoint discipline ----------------------------------------


def marginal_then_strong(ledger, variant_id, n_strong: int) -> None:
    """Trades 1-100 marginal-negative (95 wins of +0.0505, 5 losses of -5),
    then `n_strong` wins of +5 each."""
    marginal = [j % 20 != 19 for j in range(100)]
    seed_pattern(ledger, variant_id, marginal, start=0, price=0.99)
    seed_pattern(ledger, variant_id, [True] * n_strong, start=100, price=0.5)


def test_transient_ci_crossing_between_checkpoints_does_not_fund(ledger):
    marginal_then_strong(ledger, "var-a", 23)  # n=123 total
    # Full-history CI at n=123 is positive — the transient crossing is real...
    full = compute_variant_stats(ledger, "var-a")
    assert full.n == 123
    assert full.ci_low > 0
    # ...but the gate only looks at the first 100 trades (last checkpoint),
    # where the variant is marginal-negative. Verdict: not fundable.
    assert fundable_at_checkpoint(ledger, "var-a", GATE, make_config()) == (False, 100)


def test_same_data_extended_to_next_checkpoint_funds(ledger):
    marginal_then_strong(ledger, "var-a", 50)  # n=150: checkpoint at 150 reached
    fundable, checkpoint_n = fundable_at_checkpoint(ledger, "var-a", GATE, make_config())
    assert (fundable, checkpoint_n) == (True, 150)


def test_stellar_n99_is_not_fundable(ledger):
    seed_pattern(ledger, "var-a", [True] * 99)  # perfect record, below min_trades
    assert fundable_at_checkpoint(ledger, "var-a", GATE, make_config()) == (False, None)


def test_n100_ci_low_barely_positive_funds(ledger):
    seed_pattern(ledger, "var-a", [True] * 62 + [False] * 38)  # mean +1.2, ci_low ~ +0.25
    stats = compute_variant_stats(ledger, "var-a")
    assert stats.ci_low > 0
    assert fundable_at_checkpoint(ledger, "var-a", GATE, make_config()) == (True, 100)


def test_n100_ci_low_barely_negative_does_not_fund(ledger):
    seed_pattern(ledger, "var-a", [True] * 58 + [False] * 42)  # mean +0.8, ci_low ~ -0.16
    stats = compute_variant_stats(ledger, "var-a")
    assert stats.ci_low < 0
    assert fundable_at_checkpoint(ledger, "var-a", GATE, make_config()) == (False, 100)


# -- kill switch -----------------------------------------------------------------


def test_should_kill_ci_negative():
    stats = stats_stub(mean_pnl=-1.0, ci_low=-1.5, ci_high=-0.2)
    assert should_kill(stats, KILL, allocation_usd=100.0, consecutive_daily_cap_hits=0) == (
        "ci_negative"
    )


def test_should_kill_drawdown():
    stats = stats_stub(max_drawdown=25.0)  # threshold: 20% of $100 = $20
    assert should_kill(stats, KILL, allocation_usd=100.0, consecutive_daily_cap_hits=0) == (
        "drawdown"
    )


def test_should_kill_daily_cap_hits():
    stats = stats_stub()
    assert should_kill(stats, KILL, allocation_usd=100.0, consecutive_daily_cap_hits=3) == (
        "daily_cap_hits"
    )


def test_should_kill_healthy_variant_none():
    stats = stats_stub()
    assert should_kill(stats, KILL, allocation_usd=100.0, consecutive_daily_cap_hits=2) is None


# -- paired comparison -------------------------------------------------------------


def test_paired_comparison_uses_only_overlapping_buckets(ledger):
    # Champion: buckets 0-99 alone, winning +5 each (its glorious early era).
    # Buckets 100-199: both trade; champion is on the wrong side (-5),
    # challenger wins (+5). Regime control: only the overlap may count.
    for i in range(100):
        seed_trade(ledger, "champ", i, side="up")
        resolve(ledger, i, "up")
    for i in range(100, 200):
        seed_trade(ledger, "champ", i, side="down")
        seed_trade(ledger, "chall", i, side="up")
        resolve(ledger, i, "up")

    result = paired_comparison(ledger, "champ", "chall")
    assert result.n_overlap == 100  # buckets 0-99 excluded: challenger wasn't there
    assert result.mean_diff == pytest.approx(10.0)  # +5 - (-5), not diluted by early era
    assert result.ci_low == pytest.approx(10.0)  # degenerate: all diffs identical
    assert result.challenger_wins is True


def test_paired_comparison_no_overlap(ledger):
    seed_pattern(ledger, "champ", [True] * 5, start=0)
    for i in range(10, 15):
        seed_trade(ledger, "chall", i)
        resolve(ledger, i, "up")
    result = paired_comparison(ledger, "champ", "chall")
    assert result.n_overlap == 0
    assert result.mean_diff is None
    assert result.challenger_wins is False


# -- report generation ---------------------------------------------------------------


def test_report_json_and_md_render_with_verdicts(ledger, tmp_path):
    seed_pattern(ledger, "momentum-v1", [True] * 62 + [False] * 38)
    # export stamp passthrough: a stamp sitting next to the ledger is included
    stamp = {"rows_trades": 100, "rows_evaluations": 0, "exported_ts": 1.0}
    (ledger.path.parent / "export-stamp.json").write_text(json.dumps(stamp))

    out = tmp_path / "reports"
    report = generate_report(ledger, make_config(("momentum-v1", "ghost")), out, now=NOW)

    payload = json.loads((out / "latest.json").read_text())
    assert payload == json.loads(json.dumps(report))  # what's returned is what's written
    assert payload["generated_ts"] == NOW
    assert payload["ledger_export_stamp"] == stamp
    assert payload["ledger_rows"]["trades"] == 100  # staleness stamp: source row counts
    by_id = {e["variant_id"]: e for e in payload["variants"]}
    assert by_id["momentum-v1"]["gate"]["fundable"] is True
    assert by_id["ghost"]["gate"]["fundable"] is False

    md = (out / "latest.md").read_text()
    assert "momentum-v1" in md and "ghost" in md
    assert "fundable — profit confidence interval is above zero at checkpoint n=100" in md
    assert "not fundable — needs at least 100 resolved trades" in md


def test_report_is_byte_identical_given_fixed_now(ledger, tmp_path):
    seed_pattern(ledger, "var-a", [True] * 30 + [False] * 20)
    cfg = make_config(("var-a",))
    generate_report(ledger, cfg, tmp_path / "r1", now=NOW)
    generate_report(ledger, cfg, tmp_path / "r2", now=NOW)
    assert (tmp_path / "r1" / "latest.json").read_bytes() == (
        tmp_path / "r2" / "latest.json"
    ).read_bytes()
    assert (tmp_path / "r1" / "latest.md").read_bytes() == (
        tmp_path / "r2" / "latest.md"
    ).read_bytes()


# -- wilson interval unit sanity -----------------------------------------------------


def test_wilson_interval_matches_known_value():
    # wins=60, n=100, 95%: standard Wilson result ~ (0.502, 0.691)
    low, high = wilson_interval(60, 100, 0.95)
    assert low == pytest.approx(0.5020, abs=1e-3)
    assert high == pytest.approx(0.6906, abs=1e-3)
    with pytest.raises(ValueError):
        wilson_interval(0, 0)
