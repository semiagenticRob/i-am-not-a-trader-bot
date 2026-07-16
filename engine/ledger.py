"""Append-only SQLite ledger — the system's single source of record.

Every evaluation, trade, fill, resolution, risk event, and variant status
change lands here, attributable per variant. Discipline is enforced at two
layers:

- API surface: the only mutation is ``update_trade_fill`` (open -> terminal).
  There are no general UPDATE/DELETE methods; status history for variants is
  append-only rows, never edits.
- Storage: CHECK constraints pin enum columns, and
  ``UNIQUE(bucket_ts, variant_id, mode)`` on trades is the storage-layer
  backstop for the one-entry-per-bucket invariant.

Single-writer: the engine process is the only writer. WAL mode allows
host-side readers on separate connections. Container-side consumers must not
read the live WAL db (WAL doesn't cross the Docker VM boundary) — they read
the snapshot produced by ``export_snapshot`` instead.

Crash safety relies on SQLite atomicity: writes are committed per call, or
grouped atomically via ``transaction()`` (rolled back wholesale on error).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DECISIONS = ("enter", "skip")
MODES = ("shadow", "live")
TRADE_STATUSES = ("open", "filled", "cancelled", "failed")
TERMINAL_FILL_STATUSES = ("filled", "cancelled", "failed")
OUTCOMES = ("up", "down", "voided", "unresolved_timeout")
GATE_OUTCOMES = ("up", "down")  # only real outcomes count toward gate n / pnl
VARIANT_EVENTS = (
    "created",
    "shadow",
    "pending_promotion",
    "live",
    "vetoed",
    "retired",
    "killed",
    # Evolution (U9): a candidate split whose CI did not exclude zero (or was
    # otherwise ineligible) — logged so "why was no challenger spawned" is
    # answerable from the ledger alone.
    "proposal_rejected",
)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    bucket_ts INTEGER NOT NULL,
    variant_id TEXT NOT NULL,
    features_json TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN {DECISIONS!r}),
    skip_reason TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    bucket_ts INTEGER NOT NULL,
    variant_id TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('up', 'down')),
    mode TEXT NOT NULL CHECK (mode IN {MODES!r}),
    intended_price REAL NOT NULL,
    filled_price REAL,
    stake_usd REAL NOT NULL,
    fee_usd REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL CHECK (status IN {TRADE_STATUSES!r}),
    UNIQUE (bucket_ts, variant_id, mode)
);
CREATE TABLE IF NOT EXISTS resolutions (
    id INTEGER PRIMARY KEY,
    bucket_ts INTEGER NOT NULL,
    market_slug TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN {OUTCOMES!r}),
    resolved_ts REAL,
    UNIQUE (bucket_ts, market_slug)
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS variants (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    variant_id TEXT NOT NULL,
    event TEXT NOT NULL CHECK (event IN {VARIANT_EVENTS!r}),
    params_json TEXT NOT NULL,
    parent_variant_id TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_variant_mode ON trades (variant_id, mode);
CREATE INDEX IF NOT EXISTS idx_trades_mode_ts ON trades (mode, ts);
CREATE INDEX IF NOT EXISTS idx_evaluations_bucket ON evaluations (bucket_ts);
"""

_PNL_JOIN_SQL = f"""
SELECT t.id, t.bucket_ts, t.variant_id, t.market_slug, t.side, t.mode,
       t.ts, t.filled_price, t.stake_usd, t.fee_usd, r.outcome
FROM trades t
JOIN resolutions r ON r.bucket_ts = t.bucket_ts AND r.market_slug = t.market_slug
WHERE t.status = 'filled' AND r.outcome IN {GATE_OUTCOMES!r}
"""


class LedgerError(RuntimeError):
    """Raised on invalid ledger operations (bad transition, bad enum, ...)."""


@dataclass(frozen=True)
class Trade:
    id: int
    ts: float
    bucket_ts: int
    variant_id: str
    market_slug: str
    side: str
    mode: str
    intended_price: float
    filled_price: float | None
    stake_usd: float
    fee_usd: float
    status: str


@dataclass(frozen=True)
class PnlRow:
    trade_id: int
    ts: float
    bucket_ts: int
    variant_id: str
    market_slug: str
    side: str
    mode: str
    filled_price: float
    stake_usd: float
    fee_usd: float
    outcome: str
    pnl: float


def _pnl(side: str, filled_price: float, stake_usd: float, fee_usd: float, outcome: str) -> float:
    """Binary-market realized pnl: stake buys stake/price shares paying $1 on a win."""
    shares = stake_usd / filled_price
    payout = shares if outcome == side else 0.0
    return payout - stake_usd - fee_usd


def _day_bounds(date_str: str, tz: str) -> tuple[float, float]:
    """Epoch [start, end) of a calendar day in the operator's timezone (DST-safe)."""
    zone = ZoneInfo(tz)
    day = date.fromisoformat(date_str)
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    return start.timestamp(), end.timestamp()


class Ledger:
    """Single-writer, append-only ledger over one SQLite file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group several appends atomically; rolls back wholesale on error."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # -- append-only writes ------------------------------------------------

    def record_evaluation(
        self,
        ts: float,
        bucket_ts: int,
        variant_id: str,
        features: dict,
        decision: str,
        skip_reason: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO evaluations (ts, bucket_ts, variant_id, features_json, decision,"
            " skip_reason) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, bucket_ts, variant_id, json.dumps(features), decision, skip_reason),
        )
        return cur.lastrowid

    def record_trade(
        self,
        ts: float,
        bucket_ts: int,
        variant_id: str,
        market_slug: str,
        side: str,
        mode: str,
        intended_price: float,
        stake_usd: float,
        status: str = "open",
        filled_price: float | None = None,
        fee_usd: float = 0.0,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO trades (ts, bucket_ts, variant_id, market_slug, side, mode,"
            " intended_price, filled_price, stake_usd, fee_usd, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                bucket_ts,
                variant_id,
                market_slug,
                side,
                mode,
                intended_price,
                filled_price,
                stake_usd,
                fee_usd,
                status,
            ),
        )
        return cur.lastrowid

    def update_trade_fill(
        self, trade_id: int, filled_price: float | None, fee_usd: float, status: str
    ) -> None:
        """The ledger's ONLY mutation: transition an 'open' trade to a terminal status.

        Terminal rows are immutable; any second transition attempt raises.
        """
        if status not in TERMINAL_FILL_STATUSES:
            raise LedgerError(f"fill status must be one of {TERMINAL_FILL_STATUSES}, got {status}")
        cur = self._conn.execute(
            "UPDATE trades SET filled_price = ?, fee_usd = ?, status = ?"
            " WHERE id = ? AND status = 'open'",
            (filled_price, fee_usd, status, trade_id),
        )
        if cur.rowcount == 1:
            return
        row = self._conn.execute("SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no trade with id {trade_id}")
        raise LedgerError(
            f"trade {trade_id} is terminal ('{row['status']}'); terminal rows are immutable"
        )

    def record_resolution(
        self,
        bucket_ts: int,
        market_slug: str,
        outcome: str,
        resolved_ts: float | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO resolutions (bucket_ts, market_slug, outcome, resolved_ts)"
            " VALUES (?, ?, ?, ?)",
            (bucket_ts, market_slug, outcome, resolved_ts),
        )
        return cur.lastrowid

    def record_risk_event(self, ts: float, kind: str, detail: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO risk_events (ts, kind, detail) VALUES (?, ?, ?)", (ts, kind, detail)
        )
        return cur.lastrowid

    def record_variant_event(
        self,
        ts: float,
        variant_id: str,
        event: str,
        params: dict,
        parent_variant_id: str | None = None,
        detail: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO variants (ts, variant_id, event, params_json, parent_variant_id,"
            " detail) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, variant_id, event, json.dumps(params), parent_variant_id, detail),
        )
        return cur.lastrowid

    # -- typed reads ---------------------------------------------------------

    def _trades(self, where: str, params: tuple) -> list[Trade]:
        rows = self._conn.execute(
            f"SELECT * FROM trades WHERE {where} ORDER BY id", params
        ).fetchall()
        return [Trade(**dict(row)) for row in rows]

    def trades_for_variant(self, variant_id: str, mode: str) -> list[Trade]:
        return self._trades("variant_id = ? AND mode = ?", (variant_id, mode))

    def open_trades(self, variant_id: str | None = None, mode: str | None = None) -> list[Trade]:
        """Trades not yet terminal, plus filled trades whose market has no real outcome.

        Optionally filtered to a single variant and/or mode, pushed into the SQL
        WHERE clause rather than filtered in Python.
        """
        extra_where = ""
        params: list[str] = []
        if variant_id is not None:
            extra_where += " AND t.variant_id = ?"
            params.append(variant_id)
        if mode is not None:
            extra_where += " AND t.mode = ?"
            params.append(mode)
        rows = self._conn.execute(
            f"""
            SELECT t.* FROM trades t
            LEFT JOIN resolutions r
                ON r.bucket_ts = t.bucket_ts AND r.market_slug = t.market_slug
            WHERE (t.status = 'open'
               OR (t.status = 'filled'
                   AND (r.outcome IS NULL OR r.outcome NOT IN {GATE_OUTCOMES!r})))
            {extra_where}
            ORDER BY t.id
            """,
            params,
        ).fetchall()
        return [Trade(**dict(row)) for row in rows]

    def _pnl_rows(self, where: str = "", params: tuple = ()) -> list[PnlRow]:
        rows = self._conn.execute(_PNL_JOIN_SQL + where + " ORDER BY t.id", params).fetchall()
        return [
            PnlRow(
                trade_id=row["id"],
                ts=row["ts"],
                bucket_ts=row["bucket_ts"],
                variant_id=row["variant_id"],
                market_slug=row["market_slug"],
                side=row["side"],
                mode=row["mode"],
                filled_price=row["filled_price"],
                stake_usd=row["stake_usd"],
                fee_usd=row["fee_usd"],
                outcome=row["outcome"],
                pnl=_pnl(
                    row["side"], row["filled_price"], row["stake_usd"], row["fee_usd"],
                    row["outcome"],
                ),
            )
            for row in rows
        ]

    def realized_pnl_rows(self, variant_id: str, mode: str) -> list[PnlRow]:
        """Filled trades on markets with a real outcome; voided/timeout excluded."""
        return self._pnl_rows(" AND t.variant_id = ? AND t.mode = ?", (variant_id, mode))

    def live_realized_pnl_on_day(self, date_str: str, tz: str) -> float:
        """Sum of realized live pnl for trades entered on a calendar day (daily loss cap)."""
        start, end = _day_bounds(date_str, tz)
        rows = self._pnl_rows(" AND t.mode = 'live' AND t.ts >= ? AND t.ts < ?", (start, end))
        return sum(row.pnl for row in rows)

    def live_trade_count_on_day(self, date_str: str, tz: str) -> int:
        """Live trades entered on a calendar day, any status (daily trade cap)."""
        start, end = _day_bounds(date_str, tz)
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE mode = 'live' AND ts >= ? AND ts < ?",
            (start, end),
        ).fetchone()
        return row["n"]

    def has_entry(self, bucket_ts: int, variant_id: str, mode: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM trades WHERE bucket_ts = ? AND variant_id = ? AND mode = ? LIMIT 1",
            (bucket_ts, variant_id, mode),
        ).fetchone()
        return row is not None

    def consecutive_unresolved(self) -> int:
        """Most recent resolutions (by bucket) that are unresolved_timeout, streak length."""
        rows = self._conn.execute(
            "SELECT outcome FROM resolutions ORDER BY bucket_ts DESC, id DESC"
        ).fetchall()
        streak = 0
        for row in rows:
            if row["outcome"] != "unresolved_timeout":
                break
            streak += 1
        return streak

    # -- export ---------------------------------------------------------------

    def export_snapshot(self, export_path: Path | str, stamp_path: Path | str) -> None:
        """Consistent point-in-time copy for container consumers (WAL doesn't cross
        the Docker VM boundary), plus a row-count stamp as the high-water mark.

        Safe while the live db is open: uses the SQLite online backup API.
        """
        export_path = Path(export_path)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(export_path)
        try:
            self._conn.backup(dst)
            counts = {
                table: dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("trades", "evaluations")
            }
        finally:
            dst.close()
        stamp = {
            "rows_trades": counts["trades"],
            "rows_evaluations": counts["evaluations"],
            "exported_ts": time.time(),
        }
        Path(stamp_path).write_text(json.dumps(stamp))
