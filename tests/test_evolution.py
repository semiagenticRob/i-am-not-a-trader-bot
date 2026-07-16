"""U9: champion/challenger evolution — proposals, anti-superstition, promotion.

Ledgers are seeded through engine.ledger's public API: evaluations carry
features_json with seconds_to_close / btc_open / btc_last / btc_move_usd,
trades sit at the $5 reference stake, and resolutions provide the outcomes.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from engine.config import load_config
from engine.evolution import (
    VETO_WINDOW_SEC,
    EvolutionError,
    EvolutionManager,
    Proposal,
)
from engine.ledger import Ledger
from engine.main import EngineLoop

BUCKET0 = 1_752_580_800  # 5-minute-aligned unix ts
STAKE = 5.0
NOW = float(BUCKET0 + 200 * 300)  # after all seeded buckets

CONFIG_YAML = """\
version: 1
strategy_md_version: "cafebabe-pinned"
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
    status: shadow
    allocation_usd: 0.0
    params:
      entry_window_sec_min: 60
      entry_window_sec_max: 150
      min_impulse_usd: 70.0
      favorite_min_price: 0.70
  - id: fade-v1
    ruleset: contrarian_fade
    status: shadow
    allocation_usd: 0.0
    params:
      entry_window_sec_min: 60
      entry_window_sec_max: 150
      min_impulse_usd: 70.0
      underdog_max_price: 0.30
"""


def bucket(i: int) -> int:
    return BUCKET0 + 300 * i


def slug(i: int) -> str:
    return f"btc-updown-5m-{bucket(i)}"


def make_env(root: Path) -> SimpleNamespace:
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "rulesets.yaml"
    config_path.write_text(CONFIG_YAML)
    runtime = root / "runtime"
    ledger = Ledger(runtime / "ledger.db")
    mgr = EvolutionManager(config_path, ledger, runtime, clock=lambda: NOW)
    return SimpleNamespace(config_path=config_path, runtime=runtime, ledger=ledger, mgr=mgr)


@pytest.fixture()
def env(tmp_path: Path) -> SimpleNamespace:
    e = make_env(tmp_path)
    yield e
    e.ledger.close()


def seed_entry(
    ledger: Ledger,
    variant_id: str,
    i: int,
    *,
    side: str = "up",
    seconds_to_close: float = 100.0,
    btc_move: float = 100.0,
    price: float = 0.5,
) -> None:
    """One entered+filled shadow trade with its entry evaluation row."""
    b = bucket(i)
    features = {
        "seconds_to_close": seconds_to_close,
        "btc_open": 118_000.0,
        "btc_last": 118_000.0 + btc_move,
        "btc_move_usd": btc_move,
    }
    ledger.record_evaluation(
        ts=b + 50.0,
        bucket_ts=b,
        variant_id=variant_id,
        features=features,
        decision="enter",
        skip_reason=None,
    )
    trade_id = ledger.record_trade(
        ts=b + 50.0,
        bucket_ts=b,
        variant_id=variant_id,
        market_slug=slug(i),
        side=side,
        mode="shadow",
        intended_price=price,
        stake_usd=STAKE,
    )
    ledger.update_trade_fill(trade_id, filled_price=price, fee_usd=0.0, status="filled")


def resolve(ledger: Ledger, i: int, outcome: str) -> None:
    ledger.record_resolution(bucket(i), slug(i), outcome, resolved_ts=bucket(i) + 300.0)


def variant_events(ledger: Ledger, variant_id: str) -> list[tuple[str, str | None, str | None]]:
    rows = ledger._conn.execute(
        "SELECT event, parent_variant_id, detail FROM variants WHERE variant_id = ? ORDER BY id",
        (variant_id,),
    ).fetchall()
    return [(r["event"], r["parent_variant_id"], r["detail"]) for r in rows]


def raw_variant(config_path: Path, variant_id: str) -> dict:
    raw = yaml.safe_load(config_path.read_text())
    return next(v for v in raw["variants"] if v["id"] == variant_id)


def seed_planted_entry_time_effect(ledger: Ledger, variant_id: str = "momentum-v1") -> None:
    """200 resolved trades: late entries (stc 80 < midpoint 105) win 65%,
    early entries (stc 130) win 45%. btc_move constant (impulse split inert)."""
    late_j = early_j = 0
    for i in range(200):
        if i % 2 == 0:
            win = (late_j % 20) < 13  # 65%
            late_j += 1
            stc = 80.0
        else:
            win = (early_j % 20) < 9  # 45%
            early_j += 1
            stc = 130.0
        seed_entry(ledger, variant_id, i, seconds_to_close=stc, btc_move=100.0)
        resolve(ledger, i, "up" if win else "down")


ENTRY_TIME_PROPOSAL = Proposal(
    parent_id="momentum-v1",
    dimension="entry_time",
    params_diff={"entry_window_sec_max": 105},
    detail={
        "dimension": "entry_time",
        "mean_diff": 2.0,
        "ci_low": 0.2,
        "ci_high": 3.8,
        "n_a": 100,
        "n_b": 100,
        "ci_level": 0.99,
    },
)


def seed_fundable_challenger(ledger: Ledger, variant_id: str, start: int = 0) -> None:
    """100 resolved trades, 65% wins at price 0.5 — clears the gate at n=100."""
    for j in range(100):
        i = start + j
        win = (j % 20) < 13
        seed_entry(ledger, variant_id, i)
        resolve(ledger, i, "up" if win else "down")


def flag_pending(env: SimpleNamespace, variant_id: str) -> Path:
    """shadow -> pending_promotion; returns the veto-notice path."""
    env.mgr.review_promotions(NOW)
    assert raw_variant(env.config_path, variant_id)["status"] == "pending_promotion"
    return env.runtime / "control" / "veto-notices" / f"{variant_id}.json"


def ack_notice(notice_path: Path, ack_ts: float) -> None:
    notice = json.loads(notice_path.read_text())
    notice["delivery_ack_ts"] = ack_ts
    notice_path.write_text(json.dumps(notice))


# -- proposal generation -------------------------------------------------------


def test_planted_effect_yields_exactly_one_entry_time_challenger(env):
    seed_planted_entry_time_effect(env.ledger)
    proposals = env.mgr.propose_challengers(NOW)

    assert len(proposals) == 1
    prop = proposals[0]
    assert prop.dimension == "entry_time"
    assert prop.parent_id == "momentum-v1"
    # Late half wins -> the single diff narrows the window's far edge to the midpoint.
    assert prop.params_diff == {"entry_window_sec_max": 105}
    # Statistical case attached: effect size + CI + n per bucket.
    assert prop.detail["n_a"] == 100 and prop.detail["n_b"] == 100
    assert prop.detail["mean_diff"] == pytest.approx(2.0)
    assert prop.detail["ci_low"] > 0

    # The inert impulse split was evaluated and ledgered as rejected, with stats.
    rejected = [e for e in variant_events(env.ledger, "momentum-v1") if e[0] == "proposal_rejected"]
    assert len(rejected) == 1
    detail = json.loads(rejected[0][2])
    assert detail["dimension"] == "impulse_size"
    assert detail["reason"] == "insufficient_n"

    # High-water mark: an immediate re-review proposes nothing new.
    assert env.mgr.propose_challengers(NOW) == []


def test_spawn_challenger_lineage_and_config(env):
    seed_planted_entry_time_effect(env.ledger)
    before = yaml.safe_load(env.config_path.read_text())
    [prop] = env.mgr.propose_challengers(NOW)
    child_id = env.mgr.spawn_challenger(prop, now=NOW)

    assert child_id == "momentum-v1-c1"
    config = load_config(env.config_path)  # spawn wrote a valid config
    child = next(v for v in config.variants if v.id == child_id)
    assert child.status == "shadow"
    assert child.allocation_usd == 0.0
    parent = next(v for v in config.variants if v.id == "momentum-v1")
    diff = {k for k in child.params if child.params[k] != parent.params.get(k)}
    assert diff == {"entry_window_sec_max"}
    assert child.params["entry_window_sec_max"] == 105

    # Lineage + statistical case in the ledger.
    events = variant_events(env.ledger, child_id)
    assert [e[0] for e in events] == ["created"]
    assert events[0][1] == "momentum-v1"
    created = json.loads(events[0][2])
    assert created["params_diff"] == {"entry_window_sec_max": 105}
    assert created["statistical_case"]["ci_low"] > 0

    # Round-trip: everything except the appended variant is untouched (parsed).
    after = yaml.safe_load(env.config_path.read_text())
    assert after["strategy_md_version"] == before["strategy_md_version"]
    for key in ("version", "bankroll_usd", "reference_stake_usd", "risk", "gate", "kill"):
        assert after[key] == before[key]
    assert after["variants"][: len(before["variants"])] == before["variants"]
    assert len(after["variants"]) == len(before["variants"]) + 1


def test_fair_coin_ledgers_generate_almost_no_proposals(tmp_path):
    """Anti-superstition: seeded random ledgers with no real effect must not
    generate challengers beyond the documented false-positive rate (99% CI,
    two catalog splits, ten seeds -> at most 1 proposal total)."""
    total = 0
    for seed in range(10):
        e = make_env(tmp_path / f"seed{seed}")
        rng = random.Random(seed)
        for i in range(200):
            seed_entry(
                e.ledger,
                "momentum-v1",
                i,
                seconds_to_close=rng.uniform(60.0, 150.0),
                btc_move=rng.uniform(70.0, 140.0),
            )
            resolve(e.ledger, i, "up" if rng.random() < 0.5 else "down")
        total += len(e.mgr.propose_challengers(NOW))
        e.ledger.close()
    assert total <= 1


# -- structural guardrails -------------------------------------------------------


def test_multi_param_proposal_raises(env):
    two_diffs = Proposal(
        parent_id="momentum-v1",
        dimension="entry_time",
        params_diff={"entry_window_sec_max": 105, "min_impulse_usd": 105.0},
        detail={},
    )
    with pytest.raises(EvolutionError, match="exactly one param"):
        env.mgr.spawn_challenger(two_diffs, now=NOW)

    noop = Proposal(
        parent_id="momentum-v1",
        dimension="entry_time",
        params_diff={"entry_window_sec_max": 150},  # equals the parent value
        detail={},
    )
    with pytest.raises(EvolutionError, match="exactly one key"):
        env.mgr.spawn_challenger(noop, now=NOW)


def test_max_two_active_challengers_per_parent(env):
    def prop(key, value):
        return Proposal(
            parent_id="momentum-v1", dimension="entry_time", params_diff={key: value}, detail={}
        )

    assert env.mgr.spawn_challenger(prop("entry_window_sec_max", 105), NOW) == "momentum-v1-c1"
    assert env.mgr.spawn_challenger(prop("min_impulse_usd", 105.0), NOW) == "momentum-v1-c2"
    with pytest.raises(EvolutionError, match="active challengers"):
        env.mgr.spawn_challenger(prop("entry_window_sec_min", 90), NOW)

    # Retiring one frees a slot; ids keep counting (no reuse of c1).
    raw = yaml.safe_load(env.config_path.read_text())
    next(v for v in raw["variants"] if v["id"] == "momentum-v1-c1")["status"] = "retired"
    env.config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    assert env.mgr.spawn_challenger(prop("entry_window_sec_min", 90), NOW) == "momentum-v1-c3"


# -- promotion state machine --------------------------------------------------------


def test_promotion_flow_fail_closed(env):
    env.mgr.spawn_challenger(ENTRY_TIME_PROPOSAL, now=NOW)
    seed_fundable_challenger(env.ledger, "momentum-v1-c1")
    params_before = raw_variant(env.config_path, "momentum-v1")["params"]

    # Gate pass -> pending_promotion + veto notice with null ack.
    notice_path = flag_pending(env, "momentum-v1-c1")
    notice = json.loads(notice_path.read_text())
    assert notice["variant"] == "momentum-v1-c1"
    assert notice["parent"] == "momentum-v1"
    assert notice["delivery_ack_ts"] is None
    assert notice["veto_deadline_ts"] is None
    assert notice["params_diff"] == {"entry_window_sec_max": {"old": 150, "new": 105}}
    assert ("pending_promotion", "momentum-v1", json.dumps({"checkpoint_n": 100})) in (
        variant_events(env.ledger, "momentum-v1-c1")
    )

    # No ack -> stays pending across a simulated 48h; the clock never starts.
    env.mgr.review_promotions(NOW + 48 * 3600.0)
    assert raw_variant(env.config_path, "momentum-v1-c1")["status"] == "pending_promotion"
    assert json.loads(notice_path.read_text())["veto_deadline_ts"] is None

    # Ack written (by the operator agent in U10; here the test) -> deadline stamped.
    ack_ts = NOW + 50 * 3600.0
    ack_notice(notice_path, ack_ts)
    env.mgr.review_promotions(ack_ts + 60.0)
    stamped = json.loads(notice_path.read_text())
    assert stamped["veto_deadline_ts"] == ack_ts + VETO_WINDOW_SEC
    assert raw_variant(env.config_path, "momentum-v1-c1")["status"] == "pending_promotion"

    # Deadline passed, no veto -> live with the notice's allocation.
    env.mgr.review_promotions(ack_ts + VETO_WINDOW_SEC + 1.0)
    child = raw_variant(env.config_path, "momentum-v1-c1")
    assert child["status"] == "live"
    assert child["allocation_usd"] == 100.0
    assert any(e[0] == "live" for e in variant_events(env.ledger, "momentum-v1-c1"))

    # Prose control file: param diff + lineage, human-readable, with a TODO marker.
    prose = (env.runtime / "control" / "pending-strategy-appends" / "momentum-v1-c1.md").read_text()
    assert "TODO(U10)" in prose
    assert "momentum-v1" in prose
    assert "entry_window_sec_max" in prose
    assert "150 -> 105" in prose

    # Frozen-while-live: promotion flips status/allocation, never params.
    assert raw_variant(env.config_path, "momentum-v1")["params"] == params_before
    assert child["params"] == {**params_before, "entry_window_sec_max": 105}
    # Champion had no overlapping trades -> paired comparison cannot retire it.
    assert raw_variant(env.config_path, "momentum-v1")["status"] == "shadow"


def test_veto_cancels_promotion(env):
    env.mgr.spawn_challenger(ENTRY_TIME_PROPOSAL, now=NOW)
    seed_fundable_challenger(env.ledger, "momentum-v1-c1")
    notice_path = flag_pending(env, "momentum-v1-c1")

    # Even with an ack and an elapsed deadline, a veto file wins.
    ack_notice(notice_path, NOW)
    vetoes = env.runtime / "control" / "vetoes"
    vetoes.mkdir(parents=True, exist_ok=True)
    (vetoes / "momentum-v1-c1").write_text("no\n")

    env.mgr.review_promotions(NOW + 2 * VETO_WINDOW_SEC)
    child = raw_variant(env.config_path, "momentum-v1-c1")
    assert child["status"] == "retired"
    assert child["allocation_usd"] == 0.0
    events = [e[0] for e in variant_events(env.ledger, "momentum-v1-c1")]
    assert "vetoed" in events
    assert "live" not in events


def promote(env, challenger_id: str) -> None:
    notice_path = flag_pending(env, challenger_id)
    ack_notice(notice_path, NOW)
    env.mgr.review_promotions(NOW + 1.0)  # stamps the deadline
    env.mgr.review_promotions(NOW + VETO_WINDOW_SEC + 1.0)
    assert raw_variant(env.config_path, challenger_id)["status"] == "live"


def test_champion_retired_when_challenger_wins_paired_overlap(env):
    env.mgr.spawn_challenger(ENTRY_TIME_PROPOSAL, now=NOW)
    # Same 100 buckets: challenger bets up (65% wins), champion bets down (35%).
    seed_fundable_challenger(env.ledger, "momentum-v1-c1")
    for j in range(100):
        seed_entry(env.ledger, "momentum-v1", j, side="down")

    promote(env, "momentum-v1-c1")

    assert raw_variant(env.config_path, "momentum-v1")["status"] == "retired"
    assert raw_variant(env.config_path, "momentum-v1")["allocation_usd"] == 0.0
    retired = [e for e in variant_events(env.ledger, "momentum-v1") if e[0] == "retired"]
    assert len(retired) == 1
    detail = json.loads(retired[0][2])
    assert detail["beaten_by"] == "momentum-v1-c1"
    assert detail["n_overlap"] == 100
    assert detail["mean_diff"] == pytest.approx(3.0)
    assert detail["ci_low"] > 0


def test_champion_stays_when_challenger_does_not_win(env):
    env.mgr.spawn_challenger(ENTRY_TIME_PROPOSAL, now=NOW)
    # Same side in the same buckets -> per-bucket diffs are all zero: no win.
    seed_fundable_challenger(env.ledger, "momentum-v1-c1")
    for j in range(100):
        seed_entry(env.ledger, "momentum-v1", j, side="up")

    promote(env, "momentum-v1-c1")

    assert raw_variant(env.config_path, "momentum-v1")["status"] == "shadow"
    assert not any(e[0] == "retired" for e in variant_events(env.ledger, "momentum-v1"))


# -- engine hook ---------------------------------------------------------------------


class _StubRisk:
    def record_failure(self, now):  # pragma: no cover — rollover must not fail here
        raise AssertionError("rollover failed")


class _StubEvolution:
    def __init__(self):
        self.calls: list[tuple[str, float]] = []

    def propose_and_spawn(self, now):
        self.calls.append(("spawn", now))

    def review_promotions(self, now):
        self.calls.append(("review", now))


def _bare_loop(env, evolution=None, **kwargs) -> EngineLoop:
    return EngineLoop(
        config=load_config(env.config_path),
        ledger=env.ledger,
        market_state_factory=lambda: None,
        executor=None,
        risk_manager=_StubRisk(),
        evolution=evolution,
        **kwargs,
    )


def test_rollover_hook_throttled_to_once_per_hour(env):
    stub = _StubEvolution()
    loop = _bare_loop(env, evolution=stub)
    t0 = float(BUCKET0)
    for offset in (0.0, 300.0, 1800.0, 3599.0, 3600.0, 3900.0, 7500.0):
        loop._on_bucket_rollover(t0 + offset)
    hours_called = [now - t0 for kind, now in stub.calls if kind == "spawn"]
    assert hours_called == [0.0, 3600.0, 7500.0]
    # Both entry points run together, spawn before review.
    assert [kind for kind, _ in stub.calls] == ["spawn", "review"] * 3


def test_loop_without_evolution_unchanged(env):
    # Constructing without the new kwarg and rolling over is a no-op (no
    # runtime_dir, no poller, no evolution): the existing loop behavior holds.
    loop = EngineLoop(
        config=load_config(env.config_path),
        ledger=env.ledger,
        market_state_factory=lambda: None,
        executor=None,
        risk_manager=_StubRisk(),
    )
    assert loop.evolution is None
    loop._on_bucket_rollover(float(BUCKET0))  # must not raise / touch anything
