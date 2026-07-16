"""Market data layer: Gamma market discovery, CLOB order books, Binance spot.

Every network call goes through an injected httpx.Client so tests replay
fixtures via httpx.MockTransport — nothing here ever needs the real network
under test. All successfully parsed payloads are appended to a rotating
JSON-lines capture log (the source data for replay tests).

Field-name notes (parsed defensively, defaults never crash):
- Gamma /markets returns a JSON list; `clobTokenIds`, `outcomes`, and
  `outcomePrices` arrive as JSON-encoded strings.
- Fees: `feeRateBps` / `fee_rate_bps` (basis points) or a `fee` object with
  `rate`/`enabled`; absent → fee_rate 0.0, fees_enabled False, with a warning.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import httpx

from engine.signals import BUCKET_SEC, FeatureSnapshot, compute_snapshot

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
# binance.com is ToS/geo-restricted for US IPs; data-api.binance.vision is
# Binance's published unauthenticated market-data host. Fallback second.
SPOT_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")
DEFAULT_STALENESS_SEC = 8.0
_SLUG_TS_RE = re.compile(r"-(\d+)$")


def bucket_ts(now_ts: float) -> int:
    """UTC floor of a unix timestamp to the 5-minute boundary."""
    return int(now_ts // BUCKET_SEC) * BUCKET_SEC


def market_slug(bucket: int) -> str:
    return f"btc-updown-5m-{bucket}"


@dataclass(frozen=True)
class MarketInfo:
    slug: str
    condition_id: str
    token_id_up: str
    token_id_down: str
    end_ts: float
    fee_rate: float
    fees_enabled: bool
    active: bool
    closed: bool


@dataclass(frozen=True)
class BookTop:
    """Top of book for one outcome token. Depths are resting notional in USD
    at the best level; ts is local receive time (staleness is measured against
    our clock, not the exchange's)."""

    best_bid: float | None
    best_ask: float | None
    bid_depth_usd: float
    ask_depth_usd: float
    ts: float


class CaptureLog:
    """Append-only JSON-lines log of parsed payloads, size-rotated.

    Rotation keeps exactly one previous generation (`<name>.1`) — enough for
    replay-test capture without unbounded growth.
    """

    def __init__(self, path: Path | str, max_bytes: int = 10_000_000):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, source: str, payload: object, ts: float) -> None:
        line = json.dumps(
            {"ts": ts, "source": source, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
            with self.path.open("a") as fh:
                fh.write(line + "\n")
        except OSError as exc:  # capture is best-effort; never take down the feed
            logger.warning("capture log write failed: %s", exc)


def _as_list(value: object) -> list | None:
    """Gamma encodes list fields as JSON strings; accept either form."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, list) else None


def _parse_fees(raw: dict, slug: str) -> tuple[float, bool]:
    rate: float | None = None
    enabled_hint: bool | None = None
    for key in ("feeRateBps", "fee_rate_bps"):
        if raw.get(key) is not None:
            try:
                rate = float(raw[key]) / 10_000.0
            except (TypeError, ValueError):
                rate = None
            break
    if rate is None and isinstance(raw.get("fee"), dict):
        fee = raw["fee"]
        try:
            rate = float(fee.get("rate", 0.0))
        except (TypeError, ValueError):
            rate = None
        if isinstance(fee.get("enabled"), bool):
            enabled_hint = fee["enabled"]
    if rate is None:
        logger.warning(
            "market %s: no recognizable fee fields in payload; "
            "defaulting fee_rate=0.0, fees_enabled=False",
            slug,
        )
        return 0.0, False
    enabled = raw.get("feesEnabled")
    if not isinstance(enabled, bool):
        enabled = enabled_hint if enabled_hint is not None else rate > 0
    return rate, enabled


def _parse_end_ts(raw: dict, slug: str) -> float | None:
    for key in ("endDate", "endDateIso", "end_date_iso"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    match = _SLUG_TS_RE.search(slug)
    if match:
        logger.warning("market %s: no parseable end date; deriving from slug", slug)
        return float(int(match.group(1)) + BUCKET_SEC)
    return None


class GammaClient:
    """Market discovery and post-close resolution via the Gamma REST API."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        base_url: str = GAMMA_BASE,
        capture: CaptureLog | None = None,
    ):
        self._client = client or httpx.Client(timeout=5.0)
        self._base = base_url.rstrip("/")
        self._capture = capture

    def _get_market(self, params: dict, now: float | None) -> dict | None:
        try:
            resp = self._client.get(f"{self._base}/markets", params=params)
        except httpx.HTTPError as exc:
            logger.warning("gamma request %s failed: %s", params, exc)
            return None
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            logger.warning("gamma request %s returned %s", params, resp.status_code)
            return None
        try:
            data = resp.json()
        except ValueError:
            logger.warning("gamma request %s returned non-JSON body", params)
            return None
        if isinstance(data, dict):
            data = data.get("data", [])
        if not isinstance(data, list) or not data:
            return None
        market = data[0]
        if not isinstance(market, dict):
            return None
        if self._capture is not None:
            self._capture.append("gamma_market", market, now if now is not None else time.time())
        return market

    def resolve_market(self, slug: str, now: float | None = None) -> MarketInfo | None:
        """Slug → MarketInfo, or None when the market doesn't exist (yet)."""
        raw = self._get_market({"slug": slug}, now)
        if raw is None:
            return None
        tokens = _as_list(raw.get("clobTokenIds"))
        if not tokens or len(tokens) < 2:
            logger.warning("market %s: missing/short clobTokenIds; unusable", slug)
            return None
        outcomes = _as_list(raw.get("outcomes")) or ["Up", "Down"]
        lowered = [str(o).strip().lower() for o in outcomes]
        try:
            up_i, down_i = lowered.index("up"), lowered.index("down")
        except ValueError:
            logger.warning("market %s: outcomes %s not Up/Down; assuming order", slug, outcomes)
            up_i, down_i = 0, 1
        end_ts = _parse_end_ts(raw, slug)
        if end_ts is None:
            logger.warning("market %s: no end timestamp derivable; unusable", slug)
            return None
        fee_rate, fees_enabled = _parse_fees(raw, slug)
        return MarketInfo(
            slug=str(raw.get("slug") or slug),
            condition_id=str(raw.get("conditionId") or raw.get("condition_id") or ""),
            token_id_up=str(tokens[up_i]),
            token_id_down=str(tokens[down_i]),
            end_ts=end_ts,
            fee_rate=fee_rate,
            fees_enabled=fees_enabled,
            active=bool(raw.get("active", False)),
            closed=bool(raw.get("closed", False)),
        )

    def resolve_outcome(self, slug_or_condition_id: str, now: float | None = None) -> str | None:
        """'up' | 'down' once the market has resolved; None while unresolved."""
        key = "condition_ids" if slug_or_condition_id.startswith("0x") else "slug"
        raw = self._get_market({key: slug_or_condition_id}, now)
        if raw is None or not raw.get("closed", False):
            return None
        outcomes = _as_list(raw.get("outcomes")) or ["Up", "Down"]
        prices = _as_list(raw.get("outcomePrices"))
        if not prices:
            return None
        try:
            values = [float(p) for p in prices]
        except (TypeError, ValueError):
            return None
        winner = max(range(len(values)), key=values.__getitem__)
        if values[winner] < 0.99 or winner >= len(outcomes):
            return None  # closed but not definitively settled yet
        name = str(outcomes[winner]).strip().lower()
        return name if name in ("up", "down") else None


def _parse_side(levels: object, want_max: bool) -> tuple[float | None, float]:
    if not isinstance(levels, list):
        raise ValueError("book side is not a list")
    parsed = [(float(lvl["price"]), float(lvl["size"])) for lvl in levels]
    if not parsed:
        return None, 0.0  # empty side is a valid (thin) book, not malformed
    price, size = (max if want_max else min)(parsed, key=lambda p: p[0])
    return price, price * size


class OrderBookClient:
    """CLOB order book tops via REST polling (GET /book?token_id=...).

    Keeps last good state per token; a malformed payload never clobbers it but
    immediately marks the token stale until the next good fetch.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        base_url: str = CLOB_BASE,
        capture: CaptureLog | None = None,
    ):
        self._client = client or httpx.Client(timeout=5.0)
        self._base = base_url.rstrip("/")
        self._capture = capture
        self._tops: dict[str, BookTop] = {}
        self._last_update_ts: dict[str, float] = {}
        self._fetch_failed: dict[str, bool] = {}

    def fetch(self, token_id: str, now: float) -> BookTop | None:
        """Poll the book; on any failure return (and keep) last good state."""
        try:
            resp = self._client.get(f"{self._base}/book", params={"token_id": token_id})
            resp.raise_for_status()
            payload = resp.json()
            best_bid, bid_depth = _parse_side(payload["bids"], want_max=True)
            best_ask, ask_depth = _parse_side(payload["asks"], want_max=False)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("book fetch for %s failed (%s); keeping last good state", token_id, exc)
            self._fetch_failed[token_id] = True
            return self._tops.get(token_id)
        top = BookTop(best_bid, best_ask, bid_depth, ask_depth, ts=now)
        self._tops[token_id] = top
        self._last_update_ts[token_id] = now
        self._fetch_failed[token_id] = False
        if self._capture is not None:
            self._capture.append("clob_book", payload, now)
        return top

    def top(self, token_id: str) -> BookTop | None:
        return self._tops.get(token_id)

    def last_update_ts(self, token_id: str) -> float | None:
        return self._last_update_ts.get(token_id)

    def is_stale(self, token_id: str, now: float, threshold: float) -> bool:
        if self._fetch_failed.get(token_id, False):
            return True
        ts = self._last_update_ts.get(token_id)
        return ts is None or (now - ts) > threshold

    def start_websocket(self) -> None:
        """SEAM — Phase-1 optimization: the CLOB `market`-channel WebSocket
        plugs in here, pushing updates into _tops/_last_update_ts so fetch()
        becomes the snapshot fallback. Deliberately unimplemented in U3."""
        raise NotImplementedError("WebSocket book feed is a Phase-1 optimization")


class SpotFeed:
    """BTC spot via Binance public market data, with per-bucket interval open.

    The open for a bucket is the first price seen at-or-after bucket start —
    but only when the feed was already observing by then. An engine started
    mid-bucket has no honest open for that bucket, so open_for_bucket returns
    None until the next bucket (rule-sets must skip the impulse feature).
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        hosts: tuple[str, ...] = SPOT_HOSTS,
        symbol: str = "BTCUSDT",
        capture: CaptureLog | None = None,
    ):
        self._client = client or httpx.Client(timeout=5.0)
        self._hosts = hosts
        self._symbol = symbol
        self._capture = capture
        self.last_price: float | None = None
        self.last_ts: float | None = None
        self._opens: dict[int, float] = {}
        self._first_seen_ts: float | None = None

    def fetch(self, now: float) -> float | None:
        for host in self._hosts:
            try:
                resp = self._client.get(
                    f"{host}/api/v3/ticker/price", params={"symbol": self._symbol}
                )
                if resp.status_code != 200:
                    continue
                payload = resp.json()
                price = float(payload["price"])
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                logger.warning("spot fetch via %s failed: %s", host, exc)
                continue
            self._record(price, now, payload)
            return price
        return None

    def _record(self, price: float, now: float, payload: dict) -> None:
        if self._first_seen_ts is None:
            self._first_seen_ts = now
        self.last_price = price
        self.last_ts = now
        bucket = bucket_ts(now)
        if bucket not in self._opens and bucket >= self._first_seen_ts:
            self._opens[bucket] = price
        for old in [b for b in self._opens if b < bucket - 2 * BUCKET_SEC]:
            del self._opens[old]
        if self._capture is not None:
            self._capture.append("binance_ticker", payload, now)

    def open_for_bucket(self, bucket: int) -> float | None:
        return self._opens.get(bucket)

    def is_stale(self, now: float, threshold: float) -> bool:
        return self.last_ts is None or (now - self.last_ts) > threshold


class MarketState:
    """Aggregates Gamma + CLOB books + spot into per-tick FeatureSnapshots.

    Pure assembly lives in engine.signals.compute_snapshot; this class only
    sequences the fetches and layers the book clients' fetch-failure staleness
    (which age alone can't see) on top of the age-based flags.
    """

    def __init__(
        self,
        gamma: GammaClient,
        books: OrderBookClient,
        spot: SpotFeed,
        staleness_sec: float = DEFAULT_STALENESS_SEC,
    ):
        self.gamma = gamma
        self.books = books
        self.spot = spot
        self.staleness_sec = staleness_sec
        self._markets: dict[int, MarketInfo] = {}

    def market_for(self, now: float) -> MarketInfo | None:
        """Resolve (and cache per bucket) the active 5m market; None means the
        market doesn't exist yet — not cached, so it's retried next tick."""
        bucket = bucket_ts(now)
        market = self._markets.get(bucket)
        if market is None:
            market = self.gamma.resolve_market(market_slug(bucket), now=now)
            if market is None:
                return None
            self._markets = {bucket: market}  # prune prior buckets
        return market

    def snapshot(self, now: float) -> FeatureSnapshot | None:
        market = self.market_for(now)
        if market is None:
            return None
        bucket = bucket_ts(now)
        self.spot.fetch(now)
        self.books.fetch(market.token_id_up, now)
        self.books.fetch(market.token_id_down, now)
        snap = compute_snapshot(
            market,
            self.books.top(market.token_id_up),
            self.books.top(market.token_id_down),
            self.spot.open_for_bucket(bucket),
            self.spot.last_price,
            self.spot.last_ts,
            now,
            self.staleness_sec,
        )
        if self.books.is_stale(market.token_id_up, now, self.staleness_sec) or self.books.is_stale(
            market.token_id_down, now, self.staleness_sec
        ):
            snap = replace(snap, quote_stale=True)
        return snap
