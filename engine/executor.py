"""Execution layer: shadow fills, resolution polling, and the logging contract.

ShadowExecutor is Phase 1's only executor. It acts exclusively on
``engine.risk.Approved`` instances — it never constructs one (risk.py is the
sole constructor, adversarially tested) — and models fills instantaneously:

- fill price = limit + ``TICK`` (pessimistic ask+1tick; Phase 2 calibrates the
  fill model against live order outcomes), clamped to 0.99.
- fee = shares * fee_rate * p * (1 - p) with shares = stake / p — Polymarket's
  published taker formula — when the market's fee flag is on, else 0.

A LiveExecutor (U11) plugs in behind ``ExecutorProtocol``.

Logging contract: every engine log line is produced by ``safe_log_line``,
which refuses (raises) any line matching ``SECRET_PATTERNS``. runtime/logs/
is agent-readable by design, so a leaked credential in a log line would defeat
the credential-isolation decision.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from engine.ledger import Ledger
from engine.risk import Approved
from engine.signals import BUCKET_SEC, FeatureSnapshot

if TYPE_CHECKING:
    from engine.market_feed import GammaClient

# Shadow fill pessimism: assume we pay one tick over the quoted ask. Phase 2
# calibrates this against observed live fills.
TICK = 0.01
# Binary-market prices live in (0, 1); a modeled fill never exceeds 0.99.
MAX_FILL_PRICE = 0.99

DEFAULT_RESOLUTION_TIMEOUT_HOURS = 6.0

# Credential-shaped content that must never reach a log line. Notes:
# - hex_private_key requires the 0x prefix: bare 64-char runs of [0-9a-f]
#   would false-positive on decimal CLOB token ids (order metadata we DO log).
# - base64_run requires both upper- and lowercase within a 40+ run of base64
#   alphabet, again so digit-only token ids pass while API-key-shaped blobs
#   don't. The case lookaheads are scoped to the run's own charset.
# - secret_key_name matches JSON object keys (quoted name followed by ':').
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "hex_private_key": re.compile(r"0x[0-9a-fA-F]{64}"),
    "base64_run": re.compile(
        r"(?=[A-Za-z0-9+/=]{40,})(?=[A-Z0-9+/=]*[a-z])(?=[a-z0-9+/=]*[A-Z])[A-Za-z0-9+/=]{40,}"
    ),
    "secret_key_name": re.compile(
        r'"[^"]*(?:secret|passphrase|private_key|api_key|authorization)[^"]*"\s*:',
        re.IGNORECASE,
    ),
}


def safe_log_line(record: dict) -> str:
    """Serialize one log record to a JSON line, refusing credential-shaped content.

    ALL engine logging goes through this. Raises ValueError — loudly, by
    contract — rather than emit a line matching any SECRET_PATTERNS entry.
    """
    line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    for name, pattern in SECRET_PATTERNS.items():
        if pattern.search(line):
            raise ValueError(f"refusing to log: record matches secret pattern '{name}'")
    return line


class ExecutorProtocol(Protocol):
    """What the engine loop requires of an executor (shadow now, live in U11)."""

    def execute(self, approved: Approved, snap: FeatureSnapshot) -> int:
        """Act on one risk-approved entry; return the ledger trade id."""
        ...


class ShadowExecutor:
    """Instantaneous modeled fills recorded to the ledger; no exchange I/O.

    ``clock`` is injected so replay tests can drive trade timestamps
    deterministically from the script.
    """

    def __init__(self, ledger: Ledger, clock: Callable[[], float] = time.time):
        self._ledger = ledger
        self._clock = clock

    def execute(self, approved: Approved, snap: FeatureSnapshot) -> int:
        # The executor acts on nothing but a real risk-constructed approval:
        # a duck-typed lookalike bypassing risk.check must fail loudly.
        assert isinstance(approved, Approved), "executor accepts only risk-issued approvals"
        assert approved.mode == "shadow", "ShadowExecutor handles shadow mode only (live is U11)"

        filled_price = min(approved.limit_price + TICK, MAX_FILL_PRICE)
        if snap.fees_enabled:
            shares = approved.stake_usd / filled_price
            fee = shares * snap.fee_rate * filled_price * (1.0 - filled_price)
        else:
            fee = 0.0

        # Two ledger writes on purpose: the open row is the order intent, the
        # fill transition is the (modeled) exchange response. Shadow fills are
        # instantaneous by model, so they happen back to back.
        trade_id = self._ledger.record_trade(
            ts=self._clock(),
            bucket_ts=approved.bucket_ts,
            variant_id=approved.variant_id,
            market_slug=approved.market_slug,
            side=approved.side,
            mode="shadow",
            intended_price=approved.limit_price,
            stake_usd=approved.stake_usd,
            status="open",
        )
        self._ledger.update_trade_fill(trade_id, filled_price, fee, "filled")
        return trade_id


class ResolutionPoller:
    """Post-close outcome polling for every bucket that has trades.

    A bucket past its close with no resolution row is polled against Gamma;
    once ``timeout_hours`` elapse without an outcome it is recorded as
    ``unresolved_timeout`` plus a risk event (the ledger's
    consecutive-unresolved streak feeds engine-halt logic).
    """

    def __init__(
        self,
        ledger: Ledger,
        gamma_client: GammaClient,
        timeout_hours: float = DEFAULT_RESOLUTION_TIMEOUT_HOURS,
    ):
        self._ledger = ledger
        self._gamma = gamma_client
        self._timeout_sec = timeout_hours * 3600.0

    def _unresolved_buckets(self) -> list[tuple[int, str]]:
        # API gap workaround: U2's Ledger exposes no "buckets with trades but
        # no resolution row" read, and open_trades() can't distinguish
        # never-resolved from already-timed-out (re-recording would violate
        # the resolutions UNIQUE constraint). Read-only query on the ledger's
        # connection; single-writer discipline is unaffected.
        rows = self._ledger._conn.execute(
            """
            SELECT DISTINCT t.bucket_ts, t.market_slug
            FROM trades t
            LEFT JOIN resolutions r
                ON r.bucket_ts = t.bucket_ts AND r.market_slug = t.market_slug
            WHERE r.id IS NULL
            ORDER BY t.bucket_ts
            """
        ).fetchall()
        return [(row["bucket_ts"], row["market_slug"]) for row in rows]

    def poll(self, now: float) -> int:
        """Resolve what Gamma can; time out what it can't. Returns rows written."""
        recorded = 0
        for bucket, slug in self._unresolved_buckets():
            close_ts = bucket + BUCKET_SEC
            if now < close_ts:
                continue  # market still trading; nothing to resolve yet
            outcome = self._gamma.resolve_outcome(slug, now=now)
            if outcome in ("up", "down"):
                self._ledger.record_resolution(bucket, slug, outcome, resolved_ts=now)
                recorded += 1
            elif now - close_ts > self._timeout_sec:
                self._ledger.record_resolution(
                    bucket, slug, "unresolved_timeout", resolved_ts=now
                )
                self._ledger.record_risk_event(
                    now,
                    "resolution_timeout",
                    f"{slug}: no outcome {self._timeout_sec / 3600:.1f}h after close",
                )
                recorded += 1
        return recorded
