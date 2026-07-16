"""Risk containment: the single choke point between a Decision and execution.

Every entry intent — shadow or live — passes through ``RiskManager.check``,
which returns either an ``Approved`` (sized, executable) or a ``Rejected``
(machine-readable reason). The executor accepts ONLY ``Approved`` instances,
and this module is the only place one may be constructed (enforced by a test
that greps the rest of the engine). ``Approved.__post_init__`` refuses any
stake outside (0, HARD_PER_TRADE_MAX_USD], so even a bug elsewhere cannot
produce an oversized order.

Sizing lives here, split by mode:
- shadow: always the fixed reference stake — never Kelly. Cold-start variants
  must produce evidence immediately, and the funding gate's EV must be
  independent of the sizing history that produced it.
- live: quarter-Kelly from the caller's win-probability estimate, clamped to
  min(per-trade max, 2% of bankroll, remaining allocation).

Halt-class events (STOP file appearance, consecutive-failure trip, daily loss
cap) are ledgered via ``record_risk_event`` exactly once per occurrence.
Dedup keys are tracked in-memory, so an engine restart may re-record a
still-active halt — acceptable: the ledger is append-only and a duplicate
halt row is noise, not a hazard. Routine rejections are NOT ledgered here;
the caller records them with the evaluation row.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from engine.config import HARD_PER_TRADE_MAX_USD, Config, Variant
from engine.ledger import Ledger
from engine.rulesets import Decision
from engine.signals import FeatureSnapshot

# Daily caps roll over at the operator's midnight, not UTC: the human who
# reads the digests and clears halts lives in Colorado, and "today's losses"
# must mean the same day to both parties.
OPERATOR_TZ = "America/Denver"

# An entry needs room to exit before exit_before_sec; this margin is the
# minimum working time between entry and the exit deadline.
EXIT_MARGIN_SEC = 5.0

STOP_FILENAME = "STOP"


@dataclass(frozen=True)
class Approved:
    """A sized, executable entry. The executor acts on nothing else.

    Only ``RiskManager.check`` may construct one (adversarially tested); the
    constructor itself is the last line of defense against oversized stakes.
    """

    variant_id: str
    bucket_ts: int
    market_slug: str
    side: str
    limit_price: float
    stake_usd: float
    mode: str  # 'shadow' | 'live'

    def __post_init__(self) -> None:
        if not self.stake_usd > 0:
            raise ValueError(f"Approved stake must be > 0, got {self.stake_usd}")
        if self.stake_usd > HARD_PER_TRADE_MAX_USD:
            raise ValueError(
                f"Approved stake {self.stake_usd} exceeds hard per-trade max "
                f"{HARD_PER_TRADE_MAX_USD}"
            )


@dataclass(frozen=True)
class Rejected:
    """A refused entry. ``reason`` is snake_case and machine-readable."""

    variant_id: str
    bucket_ts: int
    reason: str


def _operator_day(now: float) -> str:
    return datetime.fromtimestamp(now, tz=ZoneInfo(OPERATOR_TZ)).date().isoformat()


class RiskManager:
    """Stateful gatekeeper: hard caps, market guards, halts, and sizing.

    One instance lives for the daemon's lifetime; the consecutive-failure
    counter and halt-event dedup keys are in-memory state (see module
    docstring for restart semantics).
    """

    def __init__(self, config: Config, ledger: Ledger, runtime_dir: Path):
        self.config = config
        self.ledger = ledger
        self.stop_path = Path(runtime_dir) / STOP_FILENAME
        self._consecutive_failures = 0
        # Halt-event dedup: once per STOP appearance / failure trip / loss day.
        self._stop_event_recorded = False
        self._failure_event_recorded = False
        self._loss_halt_day: str | None = None

    # -- failure counter (fed by the poll-loop wrapper) ----------------------

    def record_failure(self, now: float | None = None) -> None:
        self._consecutive_failures += 1
        threshold = self.config.risk.consecutive_failure_halt
        if self._consecutive_failures >= threshold and not self._failure_event_recorded:
            self._failure_event_recorded = True
            self.ledger.record_risk_event(
                now if now is not None else time.time(),
                "failure_halt",
                f"{self._consecutive_failures} consecutive API failures (halt at {threshold})",
            )

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._failure_event_recorded = False

    # -- halts ---------------------------------------------------------------

    def halted(self, now: float) -> str | None:
        """Engine-wide halt reason, or None. STOP is fail-safe and wins."""
        if self._stop_active(now):
            return "stop_file_present"
        if self._consecutive_failures >= self.config.risk.consecutive_failure_halt:
            return "failure_halt"
        return None

    def _stop_active(self, now: float) -> bool:
        if self.stop_path.exists():
            if not self._stop_event_recorded:
                self._stop_event_recorded = True
                self.ledger.record_risk_event(now, "stop_halt", str(self.stop_path))
            return True
        # STOP removed: re-arm so the next appearance records a fresh event.
        self._stop_event_recorded = False
        return False

    # -- the choke point -------------------------------------------------------

    def check(
        self,
        variant: Variant,
        decision: Decision,
        snap: FeatureSnapshot,
        now: float,
        win_prob_estimate: float | None = None,
    ) -> Approved | Rejected:
        """Gate one entry intent. Rejection order is fixed: halts first
        (STOP before everything), then the per-bucket invariant, market
        guards, timing, live daily caps, and finally sizing."""

        def reject(reason: str) -> Rejected:
            return Rejected(variant.id, snap.bucket_ts, reason)

        if decision.action != "enter":
            return reject("not_an_entry")  # defensive; skips shouldn't reach risk

        halt = self.halted(now)
        if halt is not None:
            return reject(halt)

        mode = "live" if variant.status == "live" else "shadow"
        if self.ledger.has_entry(snap.bucket_ts, variant.id, mode):
            return reject("already_entered_bucket")

        side = decision.side
        assert side is not None and decision.limit_price is not None  # enter contract
        if snap.quote_stale:
            return reject("stale_quote")
        spread = snap.spread(side)
        # Rounding absorbs float representation noise (0.70 - 0.67 != 0.03);
        # the guard is inclusive: a spread exactly at max passes.
        if spread is None or round(spread, 9) > self.config.risk.max_spread:
            return reject("spread_too_wide")
        ask_depth = snap.up_ask_depth_usd if side == "up" else snap.down_ask_depth_usd
        if ask_depth < self.config.risk.min_top_depth_usd:  # inclusive at min
            return reject("book_too_thin")

        if snap.seconds_to_close < self.config.risk.exit_before_sec + EXIT_MARGIN_SEC:
            return reject("too_close_to_close")

        if mode == "live":
            rejection = self._check_daily_caps(now)
            if rejection is not None:
                return reject(rejection)
            stake = self._live_stake(variant, decision.limit_price, win_prob_estimate)
            if isinstance(stake, str):
                return reject(stake)
        else:
            stake = self.config.reference_stake_usd

        return Approved(
            variant_id=variant.id,
            bucket_ts=snap.bucket_ts,
            market_slug=snap.market_slug,
            side=side,
            limit_price=decision.limit_price,
            stake_usd=stake,
            mode=mode,
        )

    # -- live-only internals ---------------------------------------------------

    def _check_daily_caps(self, now: float) -> str | None:
        day = _operator_day(now)
        cap_usd = self.config.risk.daily_loss_cap_pct / 100 * self.config.bankroll_usd
        realized = self.ledger.live_realized_pnl_on_day(day, OPERATOR_TZ)
        if realized <= -cap_usd:  # inclusive: exactly at the cap halts
            if self._loss_halt_day != day:  # day rollover re-arms the event
                self._loss_halt_day = day
                self.ledger.record_risk_event(
                    now, "daily_loss_halt", f"{day}: realized {realized:.2f} <= -{cap_usd:.2f}"
                )
            return "daily_loss_cap"
        count = self.ledger.live_trade_count_on_day(day, OPERATOR_TZ)
        if count >= self.config.risk.max_live_trades_per_day:
            return "daily_trade_cap"
        return None

    def _live_stake(self, variant: Variant, price: float, win_prob: float | None) -> float | str:
        """Quarter-Kelly stake, or a rejection reason string.

        Kelly for a binary market buying at price p with estimated win prob q:
        f = (q - p) / (1 - p). A live variant must always carry a gate-passing
        estimate — None means something upstream is wrong.
        """
        if win_prob is None:
            return "no_positive_edge"
        if not 0 < price < 1:
            return "sized_to_zero"  # $1 favorite has zero payoff; f is undefined
        fraction = (win_prob - price) / (1.0 - price)
        stake = min(
            0.25 * fraction * variant.allocation_usd,
            self.config.risk.per_trade_max_usd,
            0.02 * self.config.bankroll_usd,
            self._remaining_allocation(variant),
        )
        if stake <= 0:  # q <= p (f <= 0), exhausted allocation, or zero allocation
            return "sized_to_zero"
        return stake

    def _remaining_allocation(self, variant: Variant) -> float:
        """Uncommitted live capital: allocation, plus realized live pnl,
        minus stake already tied up in live trades that are open or awaiting
        a real outcome."""
        realized = sum(row.pnl for row in self.ledger.realized_pnl_rows(variant.id, "live"))
        committed = sum(
            trade.stake_usd
            for trade in self.ledger.open_trades(variant_id=variant.id, mode="live")
        )
        return variant.allocation_usd + realized - committed
