"""U2: append-only SQLite ledger."""

import json
import sqlite3
from pathlib import Path

import pytest

from engine.ledger import Ledger, LedgerError

BUCKET = 1_752_580_800  # a 5-minute-aligned unix ts
SLUG = f"btc-updown-5m-{BUCKET}"
TZ = "America/Denver"


@pytest.fixture()
def ledger(tmp_path: Path) -> Ledger:
    lgr = Ledger(tmp_path / "ledger.db")
    yield lgr
    lgr.close()


def enter_and_fill(
    ledger: Ledger,
    variant_id: str = "mom-a",
    side: str = "up",
    filled_price: float = 0.70,
    stake: float = 5.0,
    fee: float = 0.09,
    bucket: int = BUCKET,
    ts: float = float(BUCKET),
    mode: str = "shadow",
) -> int:
    trade_id = ledger.record_trade(
        ts=ts,
        bucket_ts=bucket,
        variant_id=variant_id,
        market_slug=f"btc-updown-5m-{bucket}",
        side=side,
        mode=mode,
        intended_price=filled_price,
        stake_usd=stake,
    )
    ledger.update_trade_fill(trade_id, filled_price=filled_price, fee_usd=fee, status="filled")
    return trade_id


def test_happy_path_pnl_math_including_fee(ledger):
    ledger.record_evaluation(
        ts=float(BUCKET),
        bucket_ts=BUCKET,
        variant_id="mom-a",
        features={"impulse": 0.004},
        decision="enter",
    )
    enter_and_fill(ledger, filled_price=0.70, stake=5.0, fee=0.09, side="up")
    ledger.record_resolution(BUCKET, SLUG, "up", resolved_ts=float(BUCKET + 300))

    rows = ledger.realized_pnl_rows("mom-a", "shadow")
    assert len(rows) == 1
    # 5/0.70 shares paying $1 each, minus stake, minus fee
    assert rows[0].pnl == pytest.approx(5 / 0.70 - 5 - 0.09)
    assert rows[0].pnl == pytest.approx(2.0529, abs=1e-4)

    # losing side of the same math: outcome != side -> payout 0
    enter_and_fill(ledger, variant_id="fade-a", side="down", filled_price=0.30, fee=0.05)
    (loss,) = ledger.realized_pnl_rows("fade-a", "shadow")
    assert loss.pnl == pytest.approx(-5.05)


def test_three_variants_same_bucket_attribution_intact(ledger):
    for vid in ("mom-a", "fade-a", "skew-a"):
        ledger.record_evaluation(
            ts=float(BUCKET),
            bucket_ts=BUCKET,
            variant_id=vid,
            features={"spread": 0.01},
            decision="skip",
            skip_reason="below_threshold",
        )
    reader = sqlite3.connect(ledger.path)
    rows = reader.execute(
        "SELECT variant_id, decision, skip_reason FROM evaluations WHERE bucket_ts = ?", (BUCKET,)
    ).fetchall()
    reader.close()
    assert len(rows) == 3
    assert {r[0] for r in rows} == {"mom-a", "fade-a", "skew-a"}
    assert all(r[1] == "skip" and r[2] == "below_threshold" for r in rows)


def test_unresolved_trade_excluded_from_pnl_present_in_open_view(ledger):
    trade_id = enter_and_fill(ledger)  # filled but no resolution row yet
    assert ledger.realized_pnl_rows("mom-a", "shadow") == []
    open_ids = [t.id for t in ledger.open_trades()]
    assert open_ids == [trade_id]
    assert ledger.has_entry(BUCKET, "mom-a", "shadow")
    assert not ledger.has_entry(BUCKET, "mom-a", "live")


def test_open_trades_filters_by_variant_and_mode(ledger):
    enter_and_fill(ledger, variant_id="mom-a", mode="shadow", bucket=BUCKET)
    enter_and_fill(ledger, variant_id="mom-a", mode="live", bucket=BUCKET + 300)
    enter_and_fill(ledger, variant_id="fade-a", mode="live", bucket=BUCKET + 600)

    assert [t.mode for t in ledger.open_trades(variant_id="mom-a")] == ["shadow", "live"]
    assert [t.variant_id for t in ledger.open_trades(mode="live")] == ["mom-a", "fade-a"]
    only = ledger.open_trades(variant_id="mom-a", mode="live")
    assert len(only) == 1
    assert only[0].variant_id == "mom-a"
    assert only[0].mode == "live"


def test_duplicate_bucket_variant_mode_raises(ledger):
    enter_and_fill(ledger)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_trade(
            ts=float(BUCKET + 3),
            bucket_ts=BUCKET,
            variant_id="mom-a",
            market_slug=SLUG,
            side="up",
            mode="shadow",
            intended_price=0.71,
            stake_usd=5.0,
        )
    # a different mode for the same (bucket, variant) is allowed
    ledger.record_trade(
        ts=float(BUCKET + 3),
        bucket_ts=BUCKET,
        variant_id="mom-a",
        market_slug=SLUG,
        side="up",
        mode="live",
        intended_price=0.71,
        stake_usd=5.0,
    )


def test_update_trade_fill_on_terminal_trade_raises(ledger):
    trade_id = enter_and_fill(ledger)
    with pytest.raises(LedgerError, match="terminal"):
        ledger.update_trade_fill(trade_id, filled_price=0.99, fee_usd=0.0, status="cancelled")
    with pytest.raises(LedgerError, match="no trade"):
        ledger.update_trade_fill(9999, filled_price=None, fee_usd=0.0, status="failed")
    other = ledger.record_trade(
        ts=float(BUCKET),
        bucket_ts=BUCKET,
        variant_id="fade-a",
        market_slug=SLUG,
        side="down",
        mode="shadow",
        intended_price=0.30,
        stake_usd=5.0,
    )
    with pytest.raises(LedgerError, match="fill status"):
        ledger.update_trade_fill(other, filled_price=None, fee_usd=0.0, status="open")


def test_no_general_update_or_delete_surface():
    public = [name for name in dir(Ledger) if not name.startswith("_")]
    updaters = [name for name in public if "update" in name.lower()]
    assert updaters == ["update_trade_fill"]
    assert [name for name in public if "delete" in name.lower()] == []


def test_unresolved_timeout_excluded_from_pnl_and_gate_counts(ledger):
    enter_and_fill(ledger, bucket=BUCKET)
    ledger.record_resolution(BUCKET, SLUG, "unresolved_timeout")
    next_bucket = BUCKET + 300
    enter_and_fill(ledger, bucket=next_bucket, ts=float(next_bucket))
    ledger.record_resolution(
        next_bucket, f"btc-updown-5m-{next_bucket}", "up", resolved_ts=float(next_bucket + 300)
    )

    rows = ledger.realized_pnl_rows("mom-a", "shadow")
    assert [r.bucket_ts for r in rows] == [next_bucket]  # gate n counts only real outcomes
    # the timed-out position stays visible in the open view, not silently dropped
    assert [t.bucket_ts for t in ledger.open_trades()] == [BUCKET]
    assert ledger.consecutive_unresolved() == 0  # newest resolution is a real outcome

    third = next_bucket + 300
    ledger.record_resolution(third, f"btc-updown-5m-{third}", "unresolved_timeout")
    assert ledger.consecutive_unresolved() == 1


def test_live_daily_pnl_and_trade_count_respect_tz_day(ledger):
    # 2026-07-14 23:30 vs 2026-07-15 00:30 in America/Denver (UTC-6)
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(TZ)
    late = datetime(2026, 7, 14, 23, 30, tzinfo=tz).timestamp()
    early = datetime(2026, 7, 15, 0, 30, tzinfo=tz).timestamp()
    b1, b2 = int(late) // 300 * 300, int(early) // 300 * 300

    enter_and_fill(ledger, mode="live", bucket=b1, ts=late, side="up", filled_price=0.5, fee=0.1)
    ledger.record_resolution(b1, f"btc-updown-5m-{b1}", "down")  # loss: -5.10
    enter_and_fill(ledger, mode="live", bucket=b2, ts=early, side="up", filled_price=0.5, fee=0.1)
    ledger.record_resolution(b2, f"btc-updown-5m-{b2}", "up")  # win: +4.90

    assert ledger.live_realized_pnl_on_day("2026-07-14", TZ) == pytest.approx(-5.10)
    assert ledger.live_realized_pnl_on_day("2026-07-15", TZ) == pytest.approx(4.90)
    assert ledger.live_trade_count_on_day("2026-07-14", TZ) == 1
    assert ledger.live_trade_count_on_day("2026-07-15", TZ) == 1
    # shadow trades never count against the live daily caps
    enter_and_fill(ledger, mode="shadow", bucket=b2, ts=early)
    assert ledger.live_trade_count_on_day("2026-07-15", TZ) == 1


def test_wal_concurrent_reader_sees_committed_rows(ledger):
    reader = sqlite3.connect(ledger.path)
    assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    ledger.record_risk_event(1.0, "halt", "daily cap")
    assert reader.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0] == 1
    ledger.record_risk_event(2.0, "resume", "day rollover")
    assert reader.execute("SELECT COUNT(*) FROM risk_events").fetchone()[0] == 2
    reader.close()


def test_export_snapshot_counts_match_and_export_is_queryable(ledger, tmp_path):
    enter_and_fill(ledger)
    ledger.record_evaluation(float(BUCKET), BUCKET, "mom-a", {}, "enter")
    ledger.record_evaluation(float(BUCKET), BUCKET, "fade-a", {}, "skip", "spread")
    export = tmp_path / "ledger-export.db"
    stamp_path = tmp_path / "ledger-export.stamp"
    ledger.export_snapshot(export, stamp_path)

    stamp = json.loads(stamp_path.read_text())
    assert stamp["rows_trades"] == 1
    assert stamp["rows_evaluations"] == 2
    assert stamp["exported_ts"] > 0

    snap = sqlite3.connect(export)
    assert snap.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert snap.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 2
    assert snap.execute("SELECT status FROM trades").fetchone()[0] == "filled"
    snap.close()
    # live db keeps writing after export; snapshot stays frozen
    ledger.record_evaluation(float(BUCKET), BUCKET, "skew-a", {}, "skip", "depth")
    snap = sqlite3.connect(export)
    assert snap.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 2
    snap.close()


def test_schema_creation_is_idempotent(ledger, tmp_path):
    enter_and_fill(ledger)
    reopened = Ledger(ledger.path)  # second open must not clobber or error
    assert reopened.has_entry(BUCKET, "mom-a", "shadow")
    reopened.close()


def test_aborted_transaction_leaves_no_partial_rows(ledger):
    with pytest.raises(RuntimeError, match="boom"):
        with ledger.transaction():
            ledger.record_evaluation(float(BUCKET), BUCKET, "mom-a", {}, "enter")
            ledger.record_trade(
                ts=float(BUCKET),
                bucket_ts=BUCKET,
                variant_id="mom-a",
                market_slug=SLUG,
                side="up",
                mode="shadow",
                intended_price=0.7,
                stake_usd=5.0,
            )
            raise RuntimeError("boom")  # crash before commit
    reader = sqlite3.connect(ledger.path)
    assert reader.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0
    assert reader.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    reader.close()
    # ledger still usable after the rollback
    assert ledger.record_evaluation(float(BUCKET), BUCKET, "mom-a", {}, "enter") == 1


def test_variant_history_is_append_only_rows(ledger):
    ledger.record_variant_event(1.0, "mom-a", "created", {"threshold": 0.003})
    ledger.record_variant_event(2.0, "mom-a", "shadow", {"threshold": 0.003})
    ledger.record_variant_event(
        3.0, "mom-a1", "created", {"threshold": 0.004}, parent_variant_id="mom-a"
    )
    reader = sqlite3.connect(ledger.path)
    rows = reader.execute(
        "SELECT variant_id, event, parent_variant_id FROM variants ORDER BY id"
    ).fetchall()
    reader.close()
    assert rows == [
        ("mom-a", "created", None),
        ("mom-a", "shadow", None),
        ("mom-a1", "created", "mom-a"),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record_variant_event(4.0, "mom-a", "promoted", {})  # not a valid event
