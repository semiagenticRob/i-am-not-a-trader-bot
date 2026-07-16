"""U7: EngineLoop over a scripted snapshot sequence — determinism + invariants.

One full bucket cycle driven tick by tick through ``run_once`` with a scripted
clock (no wall-clock anywhere), covering: outside-window skips, two variants
entering, persistent entry conditions across ticks (one-entry-per-bucket),
a mid-sequence feed exception (failure counter + survival), STOP parking and
resumption, and post-close resolution. The same script against two fresh
ledgers must produce row-for-row identical contents (excluding autoincrement
ids only).

Also pins engine.main.startup: live-config refusal (shadow-only until U11),
frozen-variant fatality against runtime/config-snapshot.yaml, and the
snapshot write on clean start.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import Config, GateConfig, KillConfig, RiskConfig, Variant
from engine.executor import ResolutionPoller, ShadowExecutor, safe_log_line
from engine.ledger import Ledger
from engine.main import CONFIG_SNAPSHOT_NAME, EngineLoop, startup
from engine.risk import RiskManager
from engine.signals import FeatureSnapshot

BUCKET = 1_752_499_800  # multiple of BUCKET_SEC
SLUG = f"btc-updown-5m-{BUCKET}"
CLOSE = BUCKET + 300

WINDOW_PARAMS = {
    "entry_window_sec_min": 60,
    "entry_window_sec_max": 150,
    "min_impulse_usd": 70.0,
}

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
    variants=(
        Variant(
            id="momentum-v1",
            ruleset="momentum_follow",
            status="shadow",
            allocation_usd=0.0,
            params={**WINDOW_PARAMS, "favorite_min_price": 0.70},
        ),
        Variant(
            id="fade-v1",
            ruleset="contrarian_fade",
            status="shadow",
            allocation_usd=0.0,
            params={**WINDOW_PARAMS, "underdog_max_price": 0.30},
        ),
    ),
)
VARIANT_IDS = {v.id for v in CONFIG.variants}


def snap_at(now: float) -> FeatureSnapshot:
    """Entry-favorable snapshot: +100 BTC impulse, 0.70 favorite, 0.26 underdog."""
    return FeatureSnapshot(
        bucket_ts=BUCKET,
        market_slug=SLUG,
        seconds_to_close=CLOSE - now,
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


class ScriptedState:
    """market_state stand-in: serves (or raises) exactly what the script says."""

    def __init__(self):
        self.next: FeatureSnapshot | Exception | None = None

    def snapshot(self, now: float) -> FeatureSnapshot | None:
        if isinstance(self.next, Exception):
            raise self.next
        return self.next


class FakeGamma:
    def __init__(self, outcomes: dict[str, str | None]):
        self.outcomes = outcomes

    def resolve_outcome(self, slug: str, now: float | None = None) -> str | None:
        return self.outcomes.get(slug)


def dump_tables(ledger: Ledger, base_dir: Path) -> dict[str, list[tuple]]:
    """Full ledger contents minus autoincrement ids (ts is script-driven).

    The stop_halt risk event's detail carries the STOP file's absolute path;
    the per-run tmp dir is normalized out so the comparison sees only content.
    """
    queries = {
        "evaluations": "SELECT ts, bucket_ts, variant_id, features_json, decision,"
        " skip_reason FROM evaluations ORDER BY id",
        "trades": "SELECT ts, bucket_ts, variant_id, market_slug, side, mode,"
        " intended_price, filled_price, stake_usd, fee_usd, status FROM trades ORDER BY id",
        "resolutions": "SELECT bucket_ts, market_slug, outcome, resolved_ts"
        " FROM resolutions ORDER BY id",
        "risk_events": "SELECT ts, kind, detail FROM risk_events ORDER BY id",
    }
    def normalize(value):
        if isinstance(value, str):
            return value.replace(str(base_dir), "<run>")
        return value

    return {
        name: [tuple(normalize(v) for v in row) for row in ledger._conn.execute(sql).fetchall()]
        for name, sql in queries.items()
    }


def run_sequence(base_dir: Path) -> dict:
    """One scripted bucket cycle; every timestamp comes from the script."""
    runtime = base_dir / "runtime"
    runtime.mkdir(parents=True)
    ledger = Ledger(base_dir / "ledger.db")
    risk = RiskManager(CONFIG, ledger, runtime)
    state = ScriptedState()
    now_holder = {"now": 0.0}
    loop = EngineLoop(
        config=CONFIG,
        ledger=ledger,
        market_state_factory=lambda: state,
        executor=ShadowExecutor(ledger, clock=lambda: now_holder["now"]),
        risk_manager=risk,
        resolution_poller=ResolutionPoller(ledger, FakeGamma({SLUG: "up"})),
        clock=lambda: now_holder["now"],
        sleeper=lambda seconds: None,
        log_sink=lambda record: safe_log_line(record),  # logging contract holds
    )

    statuses = []

    def tick(now: float, payload) -> None:
        now_holder["now"] = now
        state.next = payload
        statuses.append((now, loop.run_once(now)))

    tick(BUCKET + 30, snap_at(BUCKET + 30))  # outside entry window
    tick(BUCKET + 160, snap_at(BUCKET + 160))  # both variants enter
    tick(BUCKET + 166, snap_at(BUCKET + 166))  # conditions persist -> rejected
    tick(BUCKET + 172, RuntimeError("feed boom"))  # feed outage mid-window
    failures_after_error = risk._consecutive_failures
    tick(BUCKET + 178, snap_at(BUCKET + 178))  # survives, evaluates again
    failures_after_recovery = risk._consecutive_failures

    (runtime / "STOP").touch()
    tick(BUCKET + 184, snap_at(BUCKET + 184))  # parks within one tick
    (runtime / "STOP").unlink()
    tick(BUCKET + 190, snap_at(BUCKET + 190))  # resumes without restart

    now_holder["now"] = float(CLOSE + 10)
    loop.poller.poll(CLOSE + 10.0)  # post-close resolution lands

    dump = dump_tables(ledger, base_dir)
    ledger.close()
    return {
        "statuses": statuses,
        "failures_after_error": failures_after_error,
        "failures_after_recovery": failures_after_recovery,
        "dump": dump,
    }


EXPECTED_STATUSES = ["ok", "ok", "ok", "error", "ok", "parked", "ok"]


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory):
    base = tmp_path_factory.mktemp("replay")
    return run_sequence(base / "a"), run_sequence(base / "b")


class TestDeterministicReplay:
    def test_identical_ledger_contents_row_for_row(self, two_runs):
        first, second = two_runs
        assert first["dump"] == second["dump"]
        assert first["statuses"] == second["statuses"]

    def test_status_sequence(self, two_runs):
        first, _ = two_runs
        assert [status for _, status in first["statuses"]] == EXPECTED_STATUSES


class TestLoopInvariants:
    @pytest.fixture
    def run(self, two_runs):
        return two_runs[0]

    def test_every_variant_evaluated_every_successful_tick(self, run):
        ok_ticks = [now for now, status in run["statuses"] if status == "ok"]
        evals = run["dump"]["evaluations"]
        by_ts: dict[float, list[str]] = {}
        for row in evals:
            by_ts.setdefault(row[0], []).append(row[2])
        assert set(by_ts) == set(ok_ticks)
        for ts in ok_ticks:
            assert sorted(by_ts[ts]) == sorted(VARIANT_IDS)  # once each, no dupes

    def test_exactly_one_trade_per_variant_despite_persistent_conditions(self, run):
        trades = run["dump"]["trades"]
        assert len(trades) == 2
        assert {(t[1], t[2]) for t in trades} == {(BUCKET, v) for v in VARIANT_IDS}
        for trade in trades:
            assert trade[10] == "filled"
        by_variant = {t[2]: t for t in trades}
        # momentum bought the 0.70 favorite up; fade bought the 0.26 underdog down
        assert by_variant["momentum-v1"][4] == "up"
        assert by_variant["momentum-v1"][7] == pytest.approx(0.71)  # ask + 1 tick
        assert by_variant["fade-v1"][4] == "down"
        assert by_variant["fade-v1"][7] == pytest.approx(0.27)

    def test_persistent_conditions_logged_as_risk_rejections(self, run):
        reasons = {row[5] for row in run["dump"]["evaluations"] if row[0] > BUCKET + 160}
        assert reasons == {"risk_rejected_already_entered_bucket"}

    def test_outside_window_skip_logged_with_reason(self, run):
        first_tick = [row for row in run["dump"]["evaluations"] if row[0] == BUCKET + 30]
        assert len(first_tick) == 2
        assert all(row[4] == "skip" and row[5] == "skip_outside_window" for row in first_tick)

    def test_stop_parks_within_one_tick_no_evaluations(self, run):
        parked_ts = [now for now, status in run["statuses"] if status == "parked"]
        assert parked_ts == [BUCKET + 184]
        assert not any(row[0] == BUCKET + 184 for row in run["dump"]["evaluations"])
        # the STOP appearance itself is ledgered as a halt-class risk event
        assert ("stop_halt" in {row[1] for row in run["dump"]["risk_events"]})

    def test_feed_exception_counts_failure_and_loop_recovers(self, run):
        assert run["failures_after_error"] == 1
        assert run["failures_after_recovery"] == 0  # next clean tick reset it
        # the tick after the outage evaluated normally
        assert any(row[0] == BUCKET + 178 for row in run["dump"]["evaluations"])

    def test_resolution_recorded_once_post_close(self, run):
        assert run["dump"]["resolutions"] == [(BUCKET, SLUG, "up", CLOSE + 10.0)]


# ---------------------------------------------------------------------------
# engine.main.startup: live gate, frozen check, config snapshot
# ---------------------------------------------------------------------------

CONFIG_YAML = """\
version: 1
strategy_md_version: "test"
bankroll_usd: 750.0
reference_stake_usd: 5.0
risk:
  per_trade_max_usd: 5.0
  daily_loss_cap_pct: 5.0
  max_live_trades_per_day: 20
  max_spread: 0.03
  min_top_depth_usd: 30.0
  max_quote_staleness_sec: 8.0
  consecutive_failure_halt: 3
  exit_before_sec: 20
gate:
  min_trades: 100
  checkpoint_interval: 50
  ci_level: 0.95
kill:
  drawdown_pct_of_allocation: 20.0
  consecutive_daily_cap_hits: 3
variants:
  - id: momentum-v1
    ruleset: momentum_follow
    status: {status}
    allocation_usd: {allocation}
    params:
      min_impulse_usd: {min_impulse}
"""


def write_config(path: Path, status="shadow", allocation=0.0, min_impulse=70.0) -> Path:
    path.write_text(
        CONFIG_YAML.format(status=status, allocation=allocation, min_impulse=min_impulse)
    )
    return path


class TestStartup:
    def test_live_variant_refused_until_u11(self, tmp_path):
        config_path = write_config(tmp_path / "rulesets.yaml", status="live", allocation=100.0)
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        with pytest.raises(SystemExit) as excinfo:
            startup(config_path, runtime)
        assert excinfo.value.code == 2
        assert not (runtime / CONFIG_SNAPSHOT_NAME).exists()  # refused before snapshot

    def test_frozen_live_params_violation_is_fatal(self, tmp_path):
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        write_config(
            runtime / CONFIG_SNAPSHOT_NAME, status="live", allocation=100.0, min_impulse=70.0
        )
        config_path = write_config(
            tmp_path / "rulesets.yaml", status="live", allocation=100.0, min_impulse=80.0
        )
        with pytest.raises(SystemExit) as excinfo:
            startup(config_path, runtime)
        assert excinfo.value.code == 2

    def test_clean_start_writes_config_snapshot(self, tmp_path):
        config_path = write_config(tmp_path / "rulesets.yaml")
        runtime = tmp_path / "runtime"
        config = startup(config_path, runtime)
        assert config.variants[0].id == "momentum-v1"
        assert (runtime / CONFIG_SNAPSHOT_NAME).read_text() == config_path.read_text()

    def test_shadow_param_change_allowed_across_restarts(self, tmp_path):
        # Shadow params are NOT frozen; only live->live edits are.
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        write_config(runtime / CONFIG_SNAPSHOT_NAME, min_impulse=70.0)
        config_path = write_config(tmp_path / "rulesets.yaml", min_impulse=90.0)
        config = startup(config_path, runtime)
        assert config.variants[0].params["min_impulse_usd"] == 90.0
        assert (runtime / CONFIG_SNAPSHOT_NAME).read_text() == config_path.read_text()
