"""Tests for the market data layer, all network I/O via httpx.MockTransport."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from engine.market_feed import (
    CLOB_BASE,
    GAMMA_BASE,
    CaptureLog,
    GammaClient,
    MarketState,
    OrderBookClient,
    SpotFeed,
    bucket_ts,
    market_slug,
)

FIXTURES = Path(__file__).parent / "replay" / "fixtures"
BUCKET = 1784182200
SLUG = f"btc-updown-5m-{BUCKET}"
UP_TOKEN = "21742633143463906290569050155826241533067272736897614950488156847949938836455"
DOWN_TOKEN = "48331043336612883890938759509493159234755048973500640148014422747788308965732"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def gamma_with(payload, status_code: int = 200) -> GammaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gamma-api.polymarket.com"
        return httpx.Response(status_code, json=payload)

    return GammaClient(client=mock_client(handler))


class TestBucketAndSlug:
    def test_ts_exactly_on_boundary_is_that_bucket(self):
        assert bucket_ts(BUCKET) == BUCKET

    def test_one_second_before_boundary_is_previous_bucket(self):
        assert bucket_ts(BUCKET - 1) == BUCKET - 300

    def test_mid_bucket_floors(self):
        assert bucket_ts(BUCKET + 299.9) == BUCKET

    def test_slug_format(self):
        assert market_slug(BUCKET) == SLUG


class TestGammaClient:
    def test_resolve_market_happy_path(self):
        requested = {}

        def handler(request: httpx.Request) -> httpx.Response:
            requested["slug"] = request.url.params["slug"]
            return httpx.Response(200, json=fixture("gamma_market.json"))

        market = GammaClient(client=mock_client(handler)).resolve_market(SLUG)
        assert requested["slug"] == SLUG
        assert market is not None
        assert market.slug == SLUG
        assert market.condition_id == (
            "0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917"
        )
        assert market.token_id_up == UP_TOKEN
        assert market.token_id_down == DOWN_TOKEN
        assert market.end_ts == float(BUCKET + 300)  # 2026-07-16T06:15:00Z
        assert market.fee_rate == pytest.approx(0.02)  # feeRateBps "200"
        assert market.fees_enabled is True
        assert market.active is True
        assert market.closed is False

    def test_gamma_404_returns_none(self):
        assert gamma_with({"error": "not found"}, status_code=404).resolve_market(SLUG) is None

    def test_gamma_empty_list_returns_none(self):
        assert gamma_with([]).resolve_market(SLUG) is None

    def test_missing_fee_fields_default_with_warning(self, caplog):
        raw = fixture("gamma_market.json")[0].copy()
        del raw["feeRateBps"]
        del raw["feesEnabled"]
        with caplog.at_level(logging.WARNING, logger="engine.market_feed"):
            market = gamma_with([raw]).resolve_market(SLUG)
        assert market is not None
        assert market.fee_rate == 0.0
        assert market.fees_enabled is False
        assert any("fee" in rec.message for rec in caplog.records)

    def test_resolve_outcome_resolved_market(self):
        gamma = gamma_with(fixture("gamma_market_resolved.json"))
        assert gamma.resolve_outcome(SLUG) == "up"

    def test_resolve_outcome_unresolved_returns_none(self):
        assert gamma_with(fixture("gamma_market.json")).resolve_outcome(SLUG) is None


class TestOrderBookClient:
    def test_book_happy_path(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["token_id"] == UP_TOKEN
            return httpx.Response(200, json=fixture("clob_book.json"))

        top = OrderBookClient(client=mock_client(handler)).fetch(UP_TOKEN, now=100.0)
        assert top is not None
        assert top.best_bid == pytest.approx(0.52)
        assert top.best_ask == pytest.approx(0.55)
        assert top.bid_depth_usd == pytest.approx(0.52 * 120)
        assert top.ask_depth_usd == pytest.approx(0.55 * 80)
        assert top.ts == 100.0

    def test_empty_book_side_is_valid_zero_depth(self):
        payload = {"bids": [], "asks": [{"price": "0.55", "size": "80"}]}
        books = OrderBookClient(client=mock_client(lambda r: httpx.Response(200, json=payload)))
        top = books.fetch(UP_TOKEN, now=100.0)
        assert top.best_bid is None
        assert top.bid_depth_usd == 0.0
        assert top.best_ask == pytest.approx(0.55)
        assert books.is_stale(UP_TOKEN, now=101.0, threshold=8.0) is False

    def test_malformed_payload_keeps_last_good_state_and_marks_stale(self):
        payloads = [fixture("clob_book.json"), {"bids": "garbage-no-asks"}]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payloads.pop(0))

        books = OrderBookClient(client=mock_client(handler))
        good = books.fetch(UP_TOKEN, now=100.0)
        assert books.is_stale(UP_TOKEN, now=101.0, threshold=8.0) is False

        returned = books.fetch(UP_TOKEN, now=102.0)  # malformed
        assert returned == good  # last good state retained
        assert books.top(UP_TOKEN) == good
        assert books.is_stale(UP_TOKEN, now=102.0, threshold=8.0) is True

    def test_staleness_fresh_vs_aged(self):
        books = OrderBookClient(
            client=mock_client(lambda r: httpx.Response(200, json=fixture("clob_book.json")))
        )
        books.fetch(UP_TOKEN, now=100.0)
        assert books.is_stale(UP_TOKEN, now=100.5, threshold=8.0) is False
        assert books.is_stale(UP_TOKEN, now=108.0, threshold=8.0) is False  # exactly at threshold
        assert books.is_stale(UP_TOKEN, now=108.1, threshold=8.0) is True

    def test_never_fetched_token_is_stale(self):
        books = OrderBookClient(client=mock_client(lambda r: httpx.Response(200, json={})))
        assert books.is_stale(UP_TOKEN, now=100.0, threshold=8.0) is True

    def test_websocket_seam_is_explicitly_unimplemented(self):
        books = OrderBookClient(client=mock_client(lambda r: httpx.Response(200, json={})))
        with pytest.raises(NotImplementedError):
            books.start_websocket()


class TestSpotFeed:
    def test_fetch_happy_path_and_staleness(self):
        spot = SpotFeed(
            client=mock_client(lambda r: httpx.Response(200, json=fixture("binance_ticker.json")))
        )
        price = spot.fetch(now=float(BUCKET))
        assert price == pytest.approx(118432.10)
        assert spot.last_price == pytest.approx(118432.10)
        assert spot.is_stale(now=BUCKET + 2.0, threshold=8.0) is False
        assert spot.is_stale(now=BUCKET + 8.1, threshold=8.0) is True

    def test_fallback_host_used_when_primary_unreachable(self):
        hosts_hit = []

        def handler(request: httpx.Request) -> httpx.Response:
            hosts_hit.append(request.url.host)
            if request.url.host == "data-api.binance.vision":
                raise httpx.ConnectError("geo blocked", request=request)
            return httpx.Response(200, json=fixture("binance_ticker.json"))

        price = SpotFeed(client=mock_client(handler)).fetch(now=float(BUCKET))
        assert price == pytest.approx(118432.10)
        assert hosts_hit == ["data-api.binance.vision", "api.binance.com"]

    def test_interval_open_captured_at_bucket_start(self):
        spot = SpotFeed(
            client=mock_client(lambda r: httpx.Response(200, json={"price": "118000.0"}))
        )
        spot.fetch(now=float(BUCKET))  # first price at bucket start
        spot.fetch(now=float(BUCKET + 5))
        assert spot.open_for_bucket(BUCKET) == pytest.approx(118000.0)

    def test_mid_bucket_start_no_open_until_next_bucket(self):
        prices = iter(["118100.0", "118200.0", "118300.0"])
        spot = SpotFeed(
            client=mock_client(lambda r: httpx.Response(200, json={"price": next(prices)}))
        )
        spot.fetch(now=float(BUCKET + 37))  # engine restarted mid-bucket
        assert spot.open_for_bucket(BUCKET) is None  # honest: we missed the open
        spot.fetch(now=float(BUCKET + 42))
        assert spot.open_for_bucket(BUCKET) is None  # never backfilled

        spot.fetch(now=float(BUCKET + 301))  # first price seen in the next bucket
        assert spot.open_for_bucket(BUCKET + 300) == pytest.approx(118300.0)


class TestCaptureLog:
    def test_parsed_payloads_appear_as_json_lines(self, tmp_path):
        capture = CaptureLog(tmp_path / "capture.jsonl")
        spot = SpotFeed(
            client=mock_client(lambda r: httpx.Response(200, json=fixture("binance_ticker.json"))),
            capture=capture,
        )
        books = OrderBookClient(
            client=mock_client(lambda r: httpx.Response(200, json=fixture("clob_book.json"))),
            capture=capture,
        )
        spot.fetch(now=100.0)
        books.fetch(UP_TOKEN, now=101.0)

        lines = [json.loads(ln) for ln in (tmp_path / "capture.jsonl").read_text().splitlines()]
        assert [ln["source"] for ln in lines] == ["binance_ticker", "clob_book"]
        assert lines[0]["payload"] == fixture("binance_ticker.json")
        assert lines[1]["payload"] == fixture("clob_book.json")
        assert lines[0]["ts"] == 100.0

    def test_failed_fetch_writes_nothing(self, tmp_path):
        capture = CaptureLog(tmp_path / "capture.jsonl")
        books = OrderBookClient(
            client=mock_client(lambda r: httpx.Response(200, json={"bids": "garbage"})),
            capture=capture,
        )
        books.fetch(UP_TOKEN, now=100.0)
        assert not (tmp_path / "capture.jsonl").exists()

    def test_rotation_keeps_previous_generation(self, tmp_path):
        capture = CaptureLog(tmp_path / "capture.jsonl", max_bytes=50)
        capture.append("binance_ticker", {"price": "1"}, 1.0)
        capture.append("binance_ticker", {"price": "2"}, 2.0)
        assert (tmp_path / "capture.jsonl.1").exists()
        assert (tmp_path / "capture.jsonl").exists()


def build_market_state(tick_holder: dict) -> MarketState:
    """MarketState wired to a scripted transport that serves the current tick's
    recorded payloads from replay_ticks.json."""
    gamma_payload = fixture("gamma_market.json")

    def handler(request: httpx.Request) -> httpx.Response:
        tick = tick_holder["tick"]
        if request.url.host == httpx.URL(GAMMA_BASE).host:
            return httpx.Response(200, json=gamma_payload)
        if request.url.host == httpx.URL(CLOB_BASE).host:
            token = request.url.params["token_id"]
            side = "book_up" if token == UP_TOKEN else "book_down"
            return httpx.Response(200, json=tick[side])
        return httpx.Response(200, json=tick["spot"])  # binance hosts

    client = mock_client(handler)
    return MarketState(
        gamma=GammaClient(client=client),
        books=OrderBookClient(client=client),
        spot=SpotFeed(client=client),
        staleness_sec=8.0,
    )


def run_replay() -> list[str]:
    ticks = fixture("replay_ticks.json")
    holder: dict = {}
    state = build_market_state(holder)
    out = []
    for tick in ticks:
        holder["tick"] = tick
        snap = state.snapshot(float(tick["now"]))
        assert snap is not None
        out.append(snap.to_json())
    return out


class TestMarketState:
    def test_snapshot_assembles_hand_checkable_fields(self):
        ticks = fixture("replay_ticks.json")
        holder = {"tick": ticks[0]}
        state = build_market_state(holder)
        snap = state.snapshot(float(ticks[0]["now"]))
        assert snap.bucket_ts == BUCKET
        assert snap.market_slug == SLUG
        assert snap.seconds_to_close == pytest.approx(300.0)
        assert snap.btc_open == pytest.approx(118432.10)  # first tick is at bucket start
        assert snap.up_best_bid == pytest.approx(0.50)
        assert snap.down_best_ask == pytest.approx(0.50)
        assert snap.fee_rate == pytest.approx(0.02)
        assert snap.quote_stale is False and snap.spot_stale is False

    def test_no_market_yet_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == httpx.URL(GAMMA_BASE).host:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=fixture("binance_ticker.json"))

        client = mock_client(handler)
        state = MarketState(
            gamma=GammaClient(client=client),
            books=OrderBookClient(client=client),
            spot=SpotFeed(client=client),
        )
        assert state.snapshot(float(BUCKET)) is None

    def test_replay_of_recorded_sequence_is_deterministic(self):
        first, second = run_replay(), run_replay()
        assert len(first) == 4
        assert first == second  # byte-identical snapshot sequence across runs
        # sanity: the sequence actually evolves (not a frozen state repeated)
        assert len(set(first)) == 4
