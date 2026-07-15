"""Unit tests for FeatureSnapshot and the pure compute_snapshot assembly."""

from __future__ import annotations

import pytest

from engine.market_feed import BookTop, MarketInfo
from engine.signals import FeatureSnapshot, compute_snapshot

BUCKET = 1784182200


def make_snap(**overrides) -> FeatureSnapshot:
    base = dict(
        bucket_ts=BUCKET,
        market_slug=f"btc-updown-5m-{BUCKET}",
        seconds_to_close=180.0,
        btc_open=118432.10,
        btc_last=118458.55,
        up_best_bid=0.54,
        up_best_ask=0.58,
        down_best_bid=0.42,
        down_best_ask=0.46,
        up_bid_depth_usd=118.8,
        up_ask_depth_usd=43.5,
        down_bid_depth_usd=54.6,
        down_ask_depth_usd=25.3,
        fee_rate=0.02,
        fees_enabled=True,
        quote_stale=False,
        spot_stale=False,
    )
    base.update(overrides)
    return FeatureSnapshot(**base)


def make_market(**overrides) -> MarketInfo:
    base = dict(
        slug=f"btc-updown-5m-{BUCKET}",
        condition_id="0xdd22472e",
        token_id_up="111",
        token_id_down="222",
        end_ts=float(BUCKET + 300),
        fee_rate=0.02,
        fees_enabled=True,
        active=True,
        closed=False,
    )
    base.update(overrides)
    return MarketInfo(**base)


class TestFeatureSnapshotComputedFields:
    def test_btc_move_usd_hand_computed(self):
        snap = make_snap(btc_open=118432.10, btc_last=118458.55)
        assert snap.btc_move_usd == pytest.approx(26.45)

    def test_btc_move_usd_none_when_open_missing(self):
        assert make_snap(btc_open=None).btc_move_usd is None
        assert make_snap(btc_last=None).btc_move_usd is None

    def test_spread_each_side_hand_computed(self):
        snap = make_snap()
        assert snap.spread("up") == pytest.approx(0.58 - 0.54)
        assert snap.spread("down") == pytest.approx(0.46 - 0.42)

    def test_spread_none_when_side_missing(self):
        assert make_snap(up_best_ask=None).spread("up") is None
        assert make_snap(down_best_bid=None).spread("down") is None

    def test_skew_ratio_hand_computed(self):
        snap = make_snap(up_bid_depth_usd=120.0, down_bid_depth_usd=60.0)
        assert snap.skew_ratio() == pytest.approx(2.0)

    def test_skew_ratio_none_on_zero_depth(self):
        assert make_snap(up_bid_depth_usd=0.0).skew_ratio() is None
        assert make_snap(down_bid_depth_usd=0.0).skew_ratio() is None

    def test_impulse_available(self):
        assert make_snap().impulse_available is True
        assert make_snap(btc_open=None).impulse_available is False
        assert make_snap(spot_stale=True).impulse_available is False


class TestSerialization:
    def test_to_json_byte_identical_for_identical_inputs(self):
        assert make_snap().to_json() == make_snap().to_json()

    def test_from_json_round_trip(self):
        snap = make_snap(btc_open=None, quote_stale=True)
        assert FeatureSnapshot.from_json(snap.to_json()) == snap


class TestComputeSnapshot:
    NOW = float(BUCKET + 120)

    def books(self):
        up = BookTop(0.54, 0.58, 118.8, 43.5, ts=self.NOW - 1.0)
        down = BookTop(0.42, 0.46, 54.6, 25.3, ts=self.NOW - 1.0)
        return up, down

    def test_happy_path_hand_computed(self):
        up, down = self.books()
        snap = compute_snapshot(
            make_market(), up, down, 118432.10, 118458.55, self.NOW - 2.0, self.NOW, 8.0
        )
        assert snap == make_snap(seconds_to_close=180.0)
        assert snap.seconds_to_close == pytest.approx(300 - 120)
        assert snap.bucket_ts == BUCKET

    def test_empty_book_side_depth_zero(self):
        up = BookTop(None, 0.58, 0.0, 43.5, ts=self.NOW)
        _, down = self.books()
        snap = compute_snapshot(make_market(), up, down, None, 118458.55, self.NOW, self.NOW, 8.0)
        assert snap.up_best_bid is None
        assert snap.up_bid_depth_usd == 0.0
        assert snap.spread("up") is None
        assert snap.skew_ratio() is None  # zero depth: defined as None, no div-by-zero

    def test_missing_book_object_is_stale_with_zero_depth(self):
        _, down = self.books()
        snap = compute_snapshot(make_market(), None, down, None, 118458.55, self.NOW, self.NOW, 8.0)
        assert snap.quote_stale is True
        assert snap.up_best_bid is None and snap.up_best_ask is None
        assert snap.up_bid_depth_usd == 0.0 and snap.up_ask_depth_usd == 0.0

    def test_mid_bucket_start_no_open_impulse_unavailable(self):
        up, down = self.books()
        snap = compute_snapshot(
            make_market(), up, down, None, 118458.55, self.NOW - 1.0, self.NOW, 8.0
        )
        assert snap.btc_open is None
        assert snap.btc_move_usd is None
        assert snap.impulse_available is False

    def test_staleness_flags_fresh_vs_aged(self):
        up, down = self.books()
        fresh = compute_snapshot(
            make_market(), up, down, 1.0, 118458.55, self.NOW - 1.0, self.NOW, 8.0
        )
        assert fresh.quote_stale is False and fresh.spot_stale is False

        aged_up = BookTop(0.54, 0.58, 118.8, 43.5, ts=self.NOW - 8.1)
        stale_q = compute_snapshot(
            make_market(), aged_up, down, 1.0, 118458.55, self.NOW - 1.0, self.NOW, 8.0
        )
        assert stale_q.quote_stale is True

        stale_s = compute_snapshot(
            make_market(), up, down, 1.0, 118458.55, self.NOW - 8.1, self.NOW, 8.0
        )
        assert stale_s.spot_stale is True
        assert stale_s.impulse_available is False

    def test_deterministic_byte_identical_serialization(self):
        def build():
            up, down = self.books()
            return compute_snapshot(
                make_market(), up, down, 118432.10, 118458.55, self.NOW - 2.0, self.NOW, 8.0
            )

        assert build().to_json() == build().to_json()
