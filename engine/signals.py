"""FeatureSnapshot: the frozen, serializable input to every rule-set.

Pure data + pure computation, no I/O. The snapshot is what gets persisted to
the ledger's evaluations rows, so identical inputs must serialize
byte-identically (replayability).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.market_feed import BookTop, MarketInfo


@dataclass(frozen=True)
class FeatureSnapshot:
    """Everything a rule-set may look at for one (tick, market) evaluation.

    Depth fields are top-of-book resting notional in USD. Prices are in
    dollars per share (0..1). None means "unknown" — rule-sets must skip
    when a field they need is None or flagged stale.
    """

    bucket_ts: int  # unix ts of the 5m window open (UTC floor)
    market_slug: str
    seconds_to_close: float
    # BTC spot within the active interval
    btc_open: float | None
    btc_last: float | None
    # order book, per outcome token
    up_best_bid: float | None
    up_best_ask: float | None
    down_best_bid: float | None
    down_best_ask: float | None
    up_bid_depth_usd: float
    up_ask_depth_usd: float
    down_bid_depth_usd: float
    down_ask_depth_usd: float
    # fees, read from the API per market — never hardcoded
    fee_rate: float
    fees_enabled: bool
    # staleness flags — guards and rule-sets both consume these
    quote_stale: bool
    spot_stale: bool

    @property
    def btc_move_usd(self) -> float | None:
        """Signed BTC move within the active interval; None if unknown."""
        if self.btc_open is None or self.btc_last is None:
            return None
        return self.btc_last - self.btc_open

    @property
    def impulse_available(self) -> bool:
        return self.btc_move_usd is not None and not self.spot_stale

    def spread(self, side: str) -> float | None:
        """Bid/ask spread for 'up' or 'down'; None if either side missing."""
        bid, ask = {
            "up": (self.up_best_bid, self.up_best_ask),
            "down": (self.down_best_bid, self.down_best_ask),
        }[side]
        if bid is None or ask is None:
            return None
        return ask - bid

    def skew_ratio(self) -> float | None:
        """Resting-notional imbalance: UP-side bid depth over DOWN-side bid
        depth. > 1 means more resting money supporting UP. None when either
        side has zero depth (undefined, and thin books must be skipped
        anyway)."""
        if self.up_bid_depth_usd <= 0 or self.down_bid_depth_usd <= 0:
            return None
        return self.up_bid_depth_usd / self.down_bid_depth_usd

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> FeatureSnapshot:
        return cls(**json.loads(text))


BUCKET_SEC = 300


def compute_snapshot(
    market: MarketInfo,
    up_book: BookTop | None,
    down_book: BookTop | None,
    spot_open: float | None,
    spot_last: float | None,
    spot_ts: float | None,
    now: float,
    staleness_sec: float,
) -> FeatureSnapshot:
    """Pure assembly of a FeatureSnapshot from already-fetched market state.

    No I/O; independently testable. `market` must expose slug, end_ts,
    fee_rate, fees_enabled (engine.market_feed.MarketInfo). `up_book` and
    `down_book` must expose best_bid, best_ask, bid_depth_usd, ask_depth_usd,
    ts (engine.market_feed.BookTop) or be None when never fetched. Staleness
    here is purely age-based: a side is stale when its last update is older
    than staleness_sec (or missing entirely).
    """

    def aged(ts: float | None) -> bool:
        return ts is None or (now - ts) > staleness_sec

    quote_stale = up_book is None or down_book is None or aged(up_book.ts) or aged(down_book.ts)
    spot_stale = spot_last is None or aged(spot_ts)
    return FeatureSnapshot(
        bucket_ts=int(now // BUCKET_SEC) * BUCKET_SEC,
        market_slug=market.slug,
        seconds_to_close=market.end_ts - now,
        btc_open=spot_open,
        btc_last=spot_last,
        up_best_bid=up_book.best_bid if up_book else None,
        up_best_ask=up_book.best_ask if up_book else None,
        down_best_bid=down_book.best_bid if down_book else None,
        down_best_ask=down_book.best_ask if down_book else None,
        up_bid_depth_usd=up_book.bid_depth_usd if up_book else 0.0,
        up_ask_depth_usd=up_book.ask_depth_usd if up_book else 0.0,
        down_bid_depth_usd=down_book.bid_depth_usd if down_book else 0.0,
        down_ask_depth_usd=down_book.ask_depth_usd if down_book else 0.0,
        fee_rate=market.fee_rate,
        fees_enabled=market.fees_enabled,
        quote_stale=quote_stale,
        spot_stale=spot_stale,
    )
