"""Per-variant statistics, diagnostics, funding/kill gates, and reports (U8).

Everything here is a pure function of the ledger plus config — analytics is a
host-side *reader* (its own SQLite connection, SELECT only; the engine is the
single writer).

Methodology pins (reproducibility is a hard requirement — same ledger must
produce byte-identical output):

- Bootstrap: ``N_BOOTSTRAP`` (10,000) resamples of the per-trade pnl, percentile
  CI on the mean, ``numpy.random.default_rng(FIXED_SEED)``. No global RNG state.
- Wilson score interval implemented directly (no scipy); the normal quantile
  uses Acklam's rational approximation (deterministic, ~1e-9 accurate).
- Funding gate evaluates ONLY at pre-registered checkpoints
  n = min_trades + k * checkpoint_interval, on the FIRST checkpoint_n resolved
  trades in chronological order. Between checkpoints the verdict is the last
  checkpoint's verdict — continuous evaluation would fund a zero-edge variant
  on any lucky streak that momentarily clears the CI bound (optional stopping).
- Champion-vs-challenger comparison is restricted to buckets where BOTH
  variants have resolved trades (controls for market regime). A full-history
  comparison is deliberately not offered as an API.

Diagnostics notes:

- ``limit_order_pct`` is 1.0 by construction — the ledger schema has no
  market-order representation (every trade row carries an ``intended_price``
  limit) — but it is computed from the data anyway rather than hardcoded.
- ``size_edge_correlation`` is None for shadow variants: all stakes equal the
  reference stake by design, so the correlation is undefined (zero variance).
  A non-None value only ever appears if stakes vary (live sizing experiments).
- ``profit_source`` attributes realized pnl by market outcome side
  ("up"/"down"), answering "which regime pays this variant".
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from engine.config import Config, GateConfig, KillConfig
from engine.ledger import Ledger, PnlRow, Trade

FIXED_SEED = 20260715
N_BOOTSTRAP = 10_000


# -- primitives --------------------------------------------------------------


def _normal_quantile(p: float) -> float:
    """Inverse standard-normal CDF (Acklam's rational approximation)."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"quantile probability must be in (0, 1), got {p}")
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(
            (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


def wilson_interval(wins: int, n: int, ci_level: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. Safe at p=0 and p=1."""
    if n <= 0:
        raise ValueError("wilson_interval requires n > 0")
    z = _normal_quantile(0.5 + ci_level / 2.0)
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def bootstrap_mean_ci(
    values: Iterable[float],
    ci_level: float = 0.95,
    *,
    seed: int = FIXED_SEED,
    resamples: int = N_BOOTSTRAP,
) -> tuple[float | None, float | None]:
    """Percentile bootstrap CI on the mean. Fresh seeded Generator per call
    so identical inputs always give identical intervals. (None, None) if empty."""
    arr = np.asarray(list(values), dtype=float)
    n = arr.size
    if n == 0:
        return None, None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(resamples, n))
    means = arr[idx].mean(axis=1)
    alpha = (1.0 - ci_level) / 2.0
    low, high = np.quantile(means, [alpha, 1.0 - alpha])
    return float(low), float(high)


def max_drawdown(pnls) -> float:
    """Max peak-to-trough decline of the cumulative pnl curve, in trade order.

    The curve starts at 0, so an opening losing streak counts as drawdown.
    Returned as a non-negative dollar amount.
    """
    cum = peak = worst = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


# -- per-variant statistics ---------------------------------------------------


@dataclass(frozen=True)
class VariantStats:
    variant_id: str
    n: int  # resolved trades (voided/unresolved_timeout excluded by the ledger join)
    mean_pnl: float | None
    ci_low: float | None
    ci_high: float | None
    win_rate: float | None
    wilson_low: float | None
    wilson_high: float | None
    max_drawdown: float
    total_fees: float
    exit_before_resolution_rate: float | None
    median_hold_sec: float | None
    limit_order_pct: float | None
    size_edge_correlation: float | None
    profit_source: dict = field(hash=False)


def _read_conn(path: Path) -> sqlite3.Connection:
    """Read-only connection for the analytics reader; never creates or writes.

    Raises sqlite3.OperationalError if the ledger file is missing/unreadable —
    callers must let that propagate rather than emit stale data as fresh.
    """
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _resolution_ts_map(ledger: Ledger) -> dict[tuple[int, str], float]:
    conn = _read_conn(ledger.path)
    try:
        cur = conn.execute("SELECT bucket_ts, market_slug, resolved_ts FROM resolutions")
        return {(b, s): ts for b, s, ts in cur.fetchall() if ts is not None}
    finally:
        conn.close()


def _row_counts(ledger: Ledger) -> dict[str, int]:
    conn = _read_conn(ledger.path)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in ("trades", "evaluations", "resolutions")
        }
    finally:
        conn.close()


def _build_stats(
    variant_id: str,
    rows: list[PnlRow],
    trades: list[Trade],
    resolved_ts: dict[tuple[int, str], float],
    ci_level: float,
) -> VariantStats:
    rows = sorted(rows, key=lambda r: (r.ts, r.trade_id))
    n = len(rows)
    pnls = [r.pnl for r in rows]
    wins = sum(1 for r in rows if r.outcome == r.side)

    if n:
        mean_pnl = sum(pnls) / n
        win_rate = wins / n
        wilson_low, wilson_high = wilson_interval(wins, n, ci_level)
    else:
        mean_pnl = win_rate = wilson_low = wilson_high = None
    ci_low, ci_high = bootstrap_mean_ci(pnls, ci_level)

    holds = []
    for r in rows:
        rts = resolved_ts.get((r.bucket_ts, r.market_slug))
        if rts is not None:
            holds.append(rts - r.ts)
    median_hold = float(statistics.median(holds)) if holds else None

    exit_rate = sum(1 for t in trades if t.status == "cancelled") / len(trades) if trades else None
    # No market-order representation exists in the schema; every row is a limit
    # order with an intended_price. Computed from data anyway (1.0 by construction).
    limit_pct = (
        sum(1 for t in trades if t.intended_price is not None) / len(trades) if trades else None
    )

    corr = None
    if n >= 2:
        stakes = np.asarray([r.stake_usd for r in rows], dtype=float)
        parr = np.asarray(pnls, dtype=float)
        if stakes.std() > 0.0 and parr.std() > 0.0:
            corr = float(np.corrcoef(stakes, parr)[0, 1])
        # else: stakes constant (shadow: all trades at reference stake by
        # design) or pnl constant — correlation undefined, stays None.

    profit_source = {"up": 0.0, "down": 0.0}
    for r in rows:
        profit_source[r.outcome] += r.pnl

    return VariantStats(
        variant_id=variant_id,
        n=n,
        mean_pnl=mean_pnl,
        ci_low=ci_low,
        ci_high=ci_high,
        win_rate=win_rate,
        wilson_low=wilson_low,
        wilson_high=wilson_high,
        max_drawdown=max_drawdown(pnls),
        total_fees=sum(r.fee_usd for r in rows),
        exit_before_resolution_rate=exit_rate,
        median_hold_sec=median_hold,
        limit_order_pct=limit_pct,
        size_edge_correlation=corr,
        profit_source=profit_source,
    )


def compute_variant_stats(
    ledger: Ledger,
    variant_id: str,
    mode: str = "shadow",
    ci_level: float = 0.95,
    reference_stake_usd: float | None = None,
) -> VariantStats:
    """Full-history stats for one variant. Voided/unresolved_timeout markets are
    excluded from n and pnl by ``realized_pnl_rows``. If ``reference_stake_usd``
    is given, only trades at exactly that stake count (mixing stakes would
    corrupt the per-trade CI)."""
    rows = ledger.realized_pnl_rows(variant_id, mode)
    if reference_stake_usd is not None:
        rows = [r for r in rows if r.stake_usd == reference_stake_usd]
    trades = ledger.trades_for_variant(variant_id, mode)
    return _build_stats(variant_id, rows, trades, _resolution_ts_map(ledger), ci_level)


# -- funding / kill gates ------------------------------------------------------


def checkpoint_for(n: int, gate: GateConfig) -> int | None:
    """Largest pre-registered checkpoint (min_trades + k*interval) <= n, or None."""
    if n < gate.min_trades:
        return None
    k = (n - gate.min_trades) // gate.checkpoint_interval
    return gate.min_trades + k * gate.checkpoint_interval


def is_fundable(stats: VariantStats, gate: GateConfig) -> bool:
    """Pure funding predicate: n >= min_trades AND the CI lower bound is above zero.

    ``stats`` MUST be checkpoint-truncated (first checkpoint_n trades,
    chronological) — use ``fundable_at_checkpoint``, which does the truncation.
    Applying this to continuously-updated stats reintroduces optional stopping.
    """
    return stats.n >= gate.min_trades and stats.ci_low is not None and stats.ci_low > 0


def fundable_at_checkpoint(
    ledger: Ledger, variant_id: str, gate: GateConfig, cfg: Config
) -> tuple[bool, int | None]:
    """Funding verdict evaluated only at the last reached checkpoint.

    Truncates the variant's resolved shadow trades (at the reference stake,
    chronological order) to the FIRST checkpoint_n trades and applies the CI
    test there. Between checkpoints this is by construction the last
    checkpoint's verdict: a transient CI crossing at, say, n=123 with
    checkpoints at 100/150 does not fund. Returns (verdict, checkpoint_n);
    (False, None) if the first checkpoint has not been reached.
    """
    rows = [
        r
        for r in ledger.realized_pnl_rows(variant_id, "shadow")
        if r.stake_usd == cfg.reference_stake_usd
    ]
    rows.sort(key=lambda r: (r.ts, r.trade_id))
    checkpoint_n = checkpoint_for(len(rows), gate)
    if checkpoint_n is None:
        return False, None
    trades = ledger.trades_for_variant(variant_id, "shadow")
    stats = _build_stats(
        variant_id, rows[:checkpoint_n], trades, _resolution_ts_map(ledger), gate.ci_level
    )
    return is_fundable(stats, gate), checkpoint_n


def should_kill(
    stats_live: VariantStats,
    kill: KillConfig,
    allocation_usd: float,
    consecutive_daily_cap_hits: int,
) -> str | None:
    """Kill-switch check on a live variant's stats. Returns the reason or None."""
    if stats_live.ci_high is not None and stats_live.ci_high < 0:
        return "ci_negative"
    if stats_live.max_drawdown > kill.drawdown_pct_of_allocation / 100.0 * allocation_usd:
        return "drawdown"
    if consecutive_daily_cap_hits >= kill.consecutive_daily_cap_hits:
        return "daily_cap_hits"
    return None


# -- paired champion-vs-challenger comparison ----------------------------------


@dataclass(frozen=True)
class PairedResult:
    champion_id: str
    challenger_id: str
    n_overlap: int
    mean_diff: float | None  # challenger - champion, per overlapping bucket
    ci_low: float | None
    ci_high: float | None
    challenger_wins: bool


def _bucket_pnl(ledger: Ledger, variant_id: str) -> dict[int, float]:
    out: dict[int, float] = {}
    for mode in ("shadow", "live"):
        for r in ledger.realized_pnl_rows(variant_id, mode):
            out[r.bucket_ts] = out.get(r.bucket_ts, 0.0) + r.pnl
    return out


def paired_comparison(
    ledger: Ledger, champion_id: str, challenger_id: str, ci_level: float = 0.95
) -> PairedResult:
    """Champion-vs-challenger on ONLY the buckets both have resolved trades in.

    The overlapping window is the only comparison that controls for market
    regime — full-history CIs reward whichever variant sampled the friendlier
    era, so no full-history comparison API exists. ``challenger_wins`` iff the
    bootstrap CI on the mean per-bucket difference is entirely above zero.
    """
    champ = _bucket_pnl(ledger, champion_id)
    chall = _bucket_pnl(ledger, challenger_id)
    overlap = sorted(set(champ) & set(chall))
    diffs = [chall[b] - champ[b] for b in overlap]
    if not diffs:
        return PairedResult(champion_id, challenger_id, 0, None, None, None, False)
    ci_low, ci_high = bootstrap_mean_ci(diffs, ci_level)
    return PairedResult(
        champion_id=champion_id,
        challenger_id=challenger_id,
        n_overlap=len(diffs),
        mean_diff=sum(diffs) / len(diffs),
        ci_low=ci_low,
        ci_high=ci_high,
        challenger_wins=ci_low is not None and ci_low > 0,
    )


# -- report generation ----------------------------------------------------------


def _verdict(
    stats: VariantStats, fundable: bool, checkpoint_n: int | None, gate: GateConfig
) -> str:
    if checkpoint_n is None:
        return (
            f"not fundable — needs at least {gate.min_trades} resolved trades to reach "
            f"the first checkpoint, has {stats.n}"
        )
    if fundable:
        return f"fundable — profit confidence interval is above zero at checkpoint n={checkpoint_n}"
    return f"not fundable — profit is not distinguishable from zero at checkpoint n={checkpoint_n}"


def _fmt(x, spec: str = ".4f") -> str:
    return "n/a" if x is None else format(x, spec)


def _render_md(report: dict) -> str:
    ts = datetime.fromtimestamp(report["generated_ts"], tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = report["ledger_rows"]
    pct = f"{report['ci_level']:.0%}"
    lines = [
        "# Variant report",
        "",
        f"Generated {ts} from a ledger with {rows['trades']} trades, "
        f"{rows['evaluations']} evaluations, {rows['resolutions']} resolutions.",
        "",
        f"| variant | n | mean pnl/trade | {pct} CI | win rate | max drawdown | fees | verdict |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for e in report["variants"]:
        s = e["stats"]
        ci = "n/a" if s["ci_low"] is None else f"[{s['ci_low']:.4f}, {s['ci_high']:.4f}]"
        lines.append(
            f"| {e['variant_id']} | {s['n']} | {_fmt(s['mean_pnl'])} | {ci} "
            f"| {_fmt(s['win_rate'], '.3f')} | {_fmt(s['max_drawdown'], '.2f')} "
            f"| {_fmt(s['total_fees'], '.2f')} | {e['gate']['verdict']} |"
        )
    lines += [
        "",
        "## Diagnostics (gambling vs. trading)",
        "",
        "| variant | exit-before-resolution | median hold (s) | limit orders "
        "| size-edge corr | pnl from up | pnl from down |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for e in report["variants"]:
        s = e["stats"]
        lines.append(
            f"| {e['variant_id']} | {_fmt(s['exit_before_resolution_rate'], '.3f')} "
            f"| {_fmt(s['median_hold_sec'], '.0f')} | {_fmt(s['limit_order_pct'], '.2f')} "
            f"| {_fmt(s['size_edge_correlation'], '.3f')} "
            f"| {s['profit_source']['up']:.2f} | {s['profit_source']['down']:.2f} |"
        )
    lines += [
        "",
        "Funding requires the profit confidence interval to sit fully above zero "
        f"at a pre-registered checkpoint (n = {report['gate']['min_trades']}, then every "
        f"{report['gate']['checkpoint_interval']} trades). Between checkpoints the last "
        "checkpoint's verdict stands.",
        "",
    ]
    return "\n".join(lines)


def generate_report(ledger: Ledger, config: Config, out_dir: Path | str, now: float) -> dict:
    """Write latest.json + latest.md under ``out_dir``. Deterministic except for
    ``now`` (the caller supplies the timestamp; passing a constant makes the
    output byte-identical for the same ledger). Raises if the ledger is
    unreadable — never emits stale data as fresh."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = _row_counts(ledger)  # raises on an unreadable ledger, by design

    stamp = None
    stamp_path = ledger.path.parent / "export-stamp.json"
    if stamp_path.exists():
        stamp = json.loads(stamp_path.read_text())

    entries = []
    for variant in config.variants:
        stats = compute_variant_stats(
            ledger,
            variant.id,
            mode="shadow",
            ci_level=config.gate.ci_level,
            reference_stake_usd=config.reference_stake_usd,
        )
        fundable, checkpoint_n = fundable_at_checkpoint(ledger, variant.id, config.gate, config)
        entries.append(
            {
                "variant_id": variant.id,
                "status": variant.status,
                "stats": asdict(stats),
                "gate": {
                    "fundable": fundable,
                    "checkpoint_n": checkpoint_n,
                    "verdict": _verdict(stats, fundable, checkpoint_n, config.gate),
                },
            }
        )

    report = {
        "generated_ts": now,  # the ONLY nondeterministic field, supplied by the caller
        "ledger_rows": counts,
        "ledger_export_stamp": stamp,
        "ci_level": config.gate.ci_level,
        "gate": {
            "min_trades": config.gate.min_trades,
            "checkpoint_interval": config.gate.checkpoint_interval,
        },
        "variants": entries,
    }
    (out_dir / "latest.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (out_dir / "latest.md").write_text(_render_md(report))
    return report
