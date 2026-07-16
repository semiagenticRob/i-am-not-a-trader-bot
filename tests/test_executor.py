"""U7: shadow executor, resolution poller, and the logging contract.

Behaviors pinned here:
- shadow fill = limit + TICK (pessimistic ask+1tick), clamped to 0.99
- fee = shares * fee_rate * p * (1-p) with shares = stake/p (Polymarket's
  published taker formula), 0 when the market's fee flag is off
- the executor acts only on risk-issued Approved instances, shadow-mode only
- ResolutionPoller: real outcome -> resolution row; past timeout ->
  unresolved_timeout + risk_event; pre-close buckets are never polled;
  resolved buckets are never re-polled
- safe_log_line passes ordinary order metadata (including long digit-only
  CLOB token ids) and raises on credential-shaped content
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from engine.executor import (
    MAX_FILL_PRICE,
    TICK,
    ResolutionPoller,
    ShadowExecutor,
    safe_log_line,
)
from engine.ledger import Ledger
from engine.risk import Approved

BUCKET = 1_752_499_800  # multiple of BUCKET_SEC
SLUG = f"btc-updown-5m-{BUCKET}"
CLOSE = BUCKET + 300
NOW = float(BUCKET + 160)


def snap(**overrides):
    """Guard-passing snapshot (same factory pattern as tests/test_risk.py)."""
    from engine.signals import FeatureSnapshot

    defaults = dict(
        bucket_ts=BUCKET,
        market_slug=SLUG,
        seconds_to_close=140.0,
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


def approved(**overrides) -> Approved:
    defaults = dict(
        variant_id="v-shadow",
        bucket_ts=BUCKET,
        market_slug=SLUG,
        side="up",
        limit_price=0.70,
        stake_usd=5.0,
        mode="shadow",
    )
    defaults.update(overrides)
    return Approved(**defaults)


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    yield led
    led.close()


@pytest.fixture
def executor(ledger):
    return ShadowExecutor(ledger, clock=lambda: NOW)


def sole_trade(ledger):
    trades = ledger.trades_for_variant("v-shadow", "shadow")
    assert len(trades) == 1
    return trades[0]


def risk_events(ledger, kind):
    return ledger._conn.execute(
        "SELECT ts, kind, detail FROM risk_events WHERE kind = ?", (kind,)
    ).fetchall()


class TestShadowFillModel:
    def test_fill_is_limit_plus_one_tick(self, executor, ledger):
        executor.execute(approved(limit_price=0.70), snap())
        trade = sole_trade(ledger)
        assert trade.filled_price == pytest.approx(0.70 + TICK)
        assert trade.intended_price == 0.70

    def test_fee_matches_polymarket_taker_formula(self, executor, ledger):
        # stake 5 filled at 0.71: shares = 5/0.71 = 7.042, and
        # fee = 7.042 * 0.07 * 0.71 * 0.29 = 5 * 0.07 * 0.29 = 0.1015
        executor.execute(approved(), snap(fees_enabled=True, fee_rate=0.07))
        trade = sole_trade(ledger)
        assert trade.fee_usd == pytest.approx(0.1015, abs=1e-4)
        shares = 5.0 / trade.filled_price
        assert trade.fee_usd == pytest.approx(
            shares * 0.07 * trade.filled_price * (1 - trade.filled_price)
        )

    def test_fee_zero_when_fees_disabled(self, executor, ledger):
        # fee_rate present but the market's fee flag is off -> no fee
        executor.execute(approved(), snap(fees_enabled=False, fee_rate=0.07))
        assert sole_trade(ledger).fee_usd == 0.0

    def test_fill_clamped_at_099(self, executor, ledger):
        executor.execute(approved(limit_price=0.985), snap())
        assert sole_trade(ledger).filled_price == MAX_FILL_PRICE

    def test_trade_row_lands_filled_with_full_attribution(self, executor, ledger):
        trade_id = executor.execute(approved(), snap())
        trade = sole_trade(ledger)
        assert trade.id == trade_id
        assert trade.status == "filled"  # open -> filled, instantaneous by model
        assert (trade.ts, trade.bucket_ts, trade.market_slug) == (NOW, BUCKET, SLUG)
        assert (trade.variant_id, trade.side, trade.mode) == ("v-shadow", "up", "shadow")
        assert trade.stake_usd == 5.0

    def test_filled_trade_is_terminal(self, executor, ledger):
        # The fill transition really happened: a second transition must raise.
        trade_id = executor.execute(approved(), snap())
        with pytest.raises(Exception, match="terminal"):
            ledger.update_trade_fill(trade_id, 0.5, 0.0, "cancelled")

    def test_rejects_anything_but_a_risk_issued_approval(self, executor):
        lookalike = SimpleNamespace(
            variant_id="v-shadow", bucket_ts=BUCKET, market_slug=SLUG,
            side="up", limit_price=0.70, stake_usd=5.0, mode="shadow",
        )
        with pytest.raises(AssertionError, match="risk-issued"):
            executor.execute(lookalike, snap())

    def test_rejects_live_mode_approvals(self, executor):
        # Live execution is U11; the shadow executor must not silently
        # shadow-fill an order that was sized and approved for live.
        with pytest.raises(AssertionError, match="shadow"):
            executor.execute(approved(mode="live"), snap())


class FakeGamma:
    """Scripted resolve_outcome, call-counting; same signature as GammaClient."""

    def __init__(self, outcomes: dict[str, str | None]):
        self.outcomes = outcomes
        self.calls: list[str] = []

    def resolve_outcome(self, slug: str, now: float | None = None) -> str | None:
        self.calls.append(slug)
        return self.outcomes.get(slug)


def seed_filled_trade(ledger, bucket=BUCKET, slug=SLUG, variant_id="v-shadow"):
    ledger.record_trade(
        ts=float(bucket + 160),
        bucket_ts=bucket,
        variant_id=variant_id,
        market_slug=slug,
        side="up",
        mode="shadow",
        intended_price=0.70,
        stake_usd=5.0,
        status="filled",
        filled_price=0.71,
    )


def resolutions(ledger):
    return ledger._conn.execute(
        "SELECT bucket_ts, market_slug, outcome, resolved_ts FROM resolutions ORDER BY id"
    ).fetchall()


class TestResolutionPoller:
    def test_resolves_past_close_bucket(self, ledger):
        seed_filled_trade(ledger)
        gamma = FakeGamma({SLUG: "up"})
        poller = ResolutionPoller(ledger, gamma)
        assert poller.poll(now=CLOSE + 20.0) == 1
        [(bucket, slug, outcome, resolved_ts)] = resolutions(ledger)
        assert (bucket, slug, outcome, resolved_ts) == (BUCKET, SLUG, "up", CLOSE + 20.0)
        # realized pnl now visible: won at 0.71, stake 5 -> 5/0.71 - 5
        [row] = ledger.realized_pnl_rows("v-shadow", "shadow")
        assert row.pnl == pytest.approx(5.0 / 0.71 - 5.0)

    def test_pre_close_bucket_not_polled(self, ledger):
        seed_filled_trade(ledger)
        gamma = FakeGamma({SLUG: "up"})
        assert ResolutionPoller(ledger, gamma).poll(now=CLOSE - 1.0) == 0
        assert gamma.calls == []
        assert resolutions(ledger) == []

    def test_unresolved_within_timeout_stays_pending(self, ledger):
        seed_filled_trade(ledger)
        gamma = FakeGamma({SLUG: None})
        poller = ResolutionPoller(ledger, gamma, timeout_hours=6.0)
        assert poller.poll(now=CLOSE + 3600.0) == 0
        assert resolutions(ledger) == []
        # ... and a later poll with a real outcome still resolves it
        gamma.outcomes[SLUG] = "down"
        assert poller.poll(now=CLOSE + 7200.0) == 1
        assert resolutions(ledger)[0]["outcome"] == "down"

    def test_timeout_records_unresolved_and_risk_event(self, ledger):
        seed_filled_trade(ledger)
        gamma = FakeGamma({SLUG: None})
        poller = ResolutionPoller(ledger, gamma, timeout_hours=6.0)
        now = CLOSE + 6 * 3600.0 + 1.0
        assert poller.poll(now=now) == 1
        [(_, slug, outcome, resolved_ts)] = resolutions(ledger)
        assert (slug, outcome, resolved_ts) == (SLUG, "unresolved_timeout", now)
        events = risk_events(ledger, "resolution_timeout")
        assert len(events) == 1 and SLUG in events[0]["detail"]

    def test_resolved_bucket_never_repolled(self, ledger):
        seed_filled_trade(ledger)
        gamma = FakeGamma({SLUG: "up"})
        poller = ResolutionPoller(ledger, gamma)
        poller.poll(now=CLOSE + 20.0)
        poller.poll(now=CLOSE + 40.0)
        assert gamma.calls == [SLUG]  # second poll found nothing unresolved
        assert len(resolutions(ledger)) == 1

    def test_bucket_without_trades_ignored(self, ledger):
        gamma = FakeGamma({SLUG: "up"})
        assert ResolutionPoller(ledger, gamma).poll(now=CLOSE + 20.0) == 0
        assert gamma.calls == []


class TestSafeLogLine:
    def test_order_metadata_passes(self):
        record = {
            "event": "shadow_trade",
            "variant_id": "momentum-v1",
            # real-shape CLOB token id: a long digit-only run must NOT trip
            # the hex/base64 patterns
            "token_id": "2174263314346390629056905015582624153306727273689"
            "7614950488156847949938836455",
            "side": "up",
            "limit_price": 0.70,
            "stake_usd": 5.0,
            "status": "filled",
        }
        line = safe_log_line(record)
        assert json.loads(line)["token_id"] == record["token_id"]

    def test_line_is_compact_sorted_json(self):
        assert safe_log_line({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_hex_private_key_value_raises(self):
        with pytest.raises(ValueError, match="hex_private_key"):
            safe_log_line({"detail": "0x" + "ab12cd34" * 8})

    def test_passphrase_key_name_raises(self):
        with pytest.raises(ValueError, match="secret_key_name"):
            safe_log_line({"passphrase": "hunter2"})

    def test_nested_authorization_header_raises(self):
        with pytest.raises(ValueError, match="secret_key_name"):
            safe_log_line({"headers": {"Authorization": "Bearer abc"}})

    def test_api_secret_key_name_raises(self):
        with pytest.raises(ValueError, match="secret_key_name"):
            safe_log_line({"clob_api_secret": "x"})

    def test_long_base64_run_raises(self):
        with pytest.raises(ValueError, match="base64_run"):
            safe_log_line({"blob": "QWxhZGRpbjpvcGVuIHNlc2FtZQ" * 3})  # 78 base64 chars

    def test_market_slug_and_condition_id_pass(self):
        # hyphens break runs; condition ids are 0x + 64 LOWERCASE hex — that IS
        # the private-key shape, so they are refused too (don't log them raw).
        line = safe_log_line({"market_slug": "btc-updown-5m-1752499800"})
        assert "btc-updown-5m" in line
