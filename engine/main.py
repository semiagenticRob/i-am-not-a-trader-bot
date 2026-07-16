"""The shadow engine daemon: poll -> evaluate -> risk -> shadow-execute -> ledger.

Run as ``python -m engine.main --config config/rulesets.yaml --runtime runtime/
[--once]``. Everything the loop touches is injected (clock, sleeper, market
state, executor, risk manager), so the replay tests drive ``run_once`` over a
scripted snapshot sequence with a scripted clock and get byte-identical ledger
contents.

LIVE-MODE GATE STUB: this daemon REFUSES to start if any config variant has
status 'live'. The live execution path lands in U11 behind an explicit
``traderctl go-live``; until then the engine is shadow-only by construction,
not by convention.

Startup checks, in order:
1. ``load_config`` with STRATEGY.md — hash drift is a reportable defect
   (warning + risk_event), not fatal.
2. Frozen-variant check against ``runtime/config-snapshot.yaml`` — fatal on
   violation (live params may never mutate in place).
3. Live-mode gate (above) — fatal while U11 is unbuilt.
4. On a clean start the current config file is copied to
   ``runtime/config-snapshot.yaml`` as the next restart's prior.

Cadence: inside a bucket's entry-relevant span the loop polls every
``POLL_SEC``; past the span it sleeps until the next bucket opens. The span
starts at bucket START (not at the variants' entry window) because SpotFeed's
interval open must be observed at bucket start — waking mid-bucket would
silently corrupt ``btc_open``. It ends at close minus the smallest
``entry_window_sec_min`` across active variants, after which no entry is
possible.

Evaluation logging: EVERY variant gets exactly one evaluations row per tick.
A risk rejection is folded into that same row (decision='enter',
skip_reason='risk_rejected_<reason>') rather than a second row, so
"one row per variant per tick" holds unconditionally.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from analytics.report import generate_report
from engine import rulesets
from engine.config import Config, ConfigError, check_frozen_variants, load_config
from engine.control import ControlProcessor
from engine.evolution import EvolutionManager
from engine.executor import ResolutionPoller, ShadowExecutor, install_safe_logging, safe_log_line
from engine.ledger import Ledger
from engine.market_feed import (
    CaptureLog,
    GammaClient,
    MarketState,
    OrderBookClient,
    SpotFeed,
    bucket_ts,
)
from engine.risk import Approved, RiskManager
from engine.signals import BUCKET_SEC, FeatureSnapshot

POLL_SEC = 3.0
CONFIG_SNAPSHOT_NAME = "config-snapshot.yaml"
EVOLUTION_HOOK_SEC = 3600.0  # evolution runs at most once per hour, not per bucket


def _print_log(record: dict) -> None:
    try:
        print(safe_log_line(record), flush=True)
    except ValueError:
        # The record itself is secret-shaped (e.g. an exception message that
        # embedded leaked credential-looking text) -- never let it through raw.
        print(
            safe_log_line(
                {
                    "event": record.get("event", "unknown"),
                    "ts": record.get("ts"),
                    "error": "redacted (record matched a secret pattern)",
                }
            ),
            flush=True,
        )


class EngineLoop:
    """One long-lived loop instance; all effects flow through injected deps."""

    def __init__(
        self,
        config: Config,
        ledger: Ledger,
        market_state_factory: Callable[[], object],
        executor,
        risk_manager: RiskManager,
        resolution_poller: ResolutionPoller | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        runtime_dir: Path | str | None = None,
        log_sink: Callable[[dict], None] = _print_log,
        evolution=None,  # engine.evolution.EvolutionManager | None; None -> skipped
        control_processor=None,  # engine.control.ControlProcessor | None; None -> skipped
    ):
        self.config = config
        self.ledger = ledger
        self.market_state = market_state_factory()
        self.executor = executor
        self.risk = risk_manager
        self.poller = resolution_poller
        self.clock = clock
        self.sleeper = sleeper
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else None
        self.log = log_sink
        self.evolution = evolution
        self.control_processor = control_processor
        self._last_bucket: int | None = None
        self._last_status: str | None = None
        self._last_evolution_ts: float | None = None

    # -- one tick -----------------------------------------------------------

    def run_once(self, now: float) -> str:
        """One tick: 'parked' | 'no_market' | 'ok' | 'error'.

        Parks (evaluates nothing) while the STOP file is present; removal
        resumes on the next tick without a restart. Any feed/executor
        exception increments the risk manager's failure counter and the loop
        survives; a clean tick resets it.
        """
        if self.risk.halted(now) == "stop_file_present":
            return "parked"
        try:
            snap = self.market_state.snapshot(now)
            if snap is None:
                return "no_market"
            for variant in self.config.active_variants:
                self._evaluate_variant(variant, snap, now)
        except Exception as exc:  # noqa: BLE001 — guards absorb feed/executor faults
            self.risk.record_failure(now)
            self.log({"event": "tick_error", "ts": now, "error": f"{type(exc).__name__}: {exc}"})
            return "error"
        self.risk.record_success()
        return "ok"

    def _evaluate_variant(self, variant, snap: FeatureSnapshot, now: float) -> None:
        window_min = variant.params.get("entry_window_sec_min")
        window_max = variant.params.get("entry_window_sec_max")
        if (
            window_min is not None
            and window_max is not None
            and not window_min <= snap.seconds_to_close <= window_max
        ):
            # Cheap pre-check: outside the window the ruleset can't enter, so
            # skip the dispatch. Reason matches rulesets' skip_outside_window;
            # note it takes precedence over staleness reasons on these ticks.
            self._record_eval(now, snap, variant.id, "skip", "skip_outside_window")
            return

        decision = rulesets.evaluate(variant, snap)
        if decision.action != "enter":
            self._record_eval(now, snap, variant.id, "skip", decision.reason)
            return

        # Shadow mode carries no win-probability estimate by design: evidence
        # generation must be independent of any edge estimate (see risk.py).
        result = self.risk.check(variant, decision, snap, now, win_prob_estimate=None)
        if isinstance(result, Approved):
            self._record_eval(now, snap, variant.id, "enter", None)
            trade_id = self.executor.execute(result, snap)
            self.log(
                {
                    "event": "shadow_trade",
                    "ts": now,
                    "trade_id": trade_id,
                    "variant_id": variant.id,
                    "bucket_ts": result.bucket_ts,
                    "side": result.side,
                    "limit_price": result.limit_price,
                    "stake_usd": result.stake_usd,
                }
            )
        else:
            # Pinned decision: the rejection is folded into the single
            # evaluation row for this (variant, tick) via skip_reason.
            self._record_eval(now, snap, variant.id, "enter", f"risk_rejected_{result.reason}")

    def _record_eval(
        self, now: float, snap: FeatureSnapshot, variant_id: str, decision: str,
        skip_reason: str | None,
    ) -> None:
        self.ledger.record_evaluation(
            ts=now,
            bucket_ts=snap.bucket_ts,
            variant_id=variant_id,
            features=asdict(snap),
            decision=decision,
            skip_reason=skip_reason,
        )

    # -- cadence ------------------------------------------------------------

    def _entry_window_end(self, bucket: int) -> float:
        """Latest moment any active variant could still enter this bucket."""
        close_ts = bucket + BUCKET_SEC
        mins = [v.params.get("entry_window_sec_min", 0) for v in self.config.active_variants]
        return close_ts - (min(mins) if mins else BUCKET_SEC)

    def _seconds_until_next_tick(self, now: float) -> float:
        bucket = bucket_ts(now)
        if now <= self._entry_window_end(bucket):
            return POLL_SEC
        # Computed sleep to the next bucket open — never a busy-wait.
        return max((bucket + BUCKET_SEC) - now, 1.0)

    def _on_bucket_rollover(self, now: float) -> None:
        """Post-close housekeeping, throttled to once per bucket by construction:
        resolve outcomes, regenerate the report, export the ledger snapshot."""
        try:
            if self.poller is not None:
                self.poller.poll(now)
            if self.runtime_dir is not None:
                report = generate_report(
                    self.ledger, self.config, self.runtime_dir / "reports", now
                )
                # Stamp path matches what analytics.report reads back
                # (ledger.path.parent / 'export-stamp.json').
                self.ledger.export_snapshot(
                    self.runtime_dir / "ledger-export.db",
                    self.runtime_dir / "export-stamp.json",
                )
                self.log(
                    {"event": "report_generated", "ts": now,
                     "ledger_rows": report["ledger_rows"]}
                )
            if self.control_processor is not None:
                # Every rollover (cheap directory scans): apply agent-written
                # control files — approved lessons, promotion prose, allocations.
                for action in self.control_processor.process(now):
                    self.log({"event": "control_action", "ts": now, "action": action})
            if self.evolution is not None and (
                self._last_evolution_ts is None
                or now - self._last_evolution_ts >= EVOLUTION_HOOK_SEC
            ):
                self._last_evolution_ts = now
                self.evolution.propose_and_spawn(now)
                self.evolution.review_promotions(now)
        except Exception as exc:  # housekeeping must never kill the loop
            self.risk.record_failure(now)
            self.log(
                {"event": "rollover_error", "ts": now,
                 "error": f"{type(exc).__name__}: {exc}"}
            )

    def run_forever(self) -> None:
        self.log(
            {
                "event": "engine_start",
                "ts": self.clock(),
                "mode": "shadow",
                "variants": [v.id for v in self.config.active_variants],
            }
        )
        while True:
            now = self.clock()
            bucket = bucket_ts(now)
            if self._last_bucket is not None and bucket != self._last_bucket:
                self._on_bucket_rollover(now)
            self._last_bucket = bucket

            status = self.run_once(now)
            if status != self._last_status:
                self.log({"event": "status_change", "ts": now, "status": status})
                self._last_status = status

            if status == "parked":
                self.sleeper(POLL_SEC)  # keep re-checking so STOP removal resumes
            else:
                self.sleeper(self._seconds_until_next_tick(now))


# -- startup ------------------------------------------------------------------


def startup(config_path: Path, runtime_dir: Path) -> Config:
    """Load and verify config; raises SystemExit on any fatal condition.

    Order: parse/validate -> frozen-variant check against the prior snapshot
    (fatal) -> live-mode gate (fatal until U11) -> write the new snapshot.
    STRATEGY.md drift is detected here but ledgered by main() (needs the
    ledger); it is a reportable defect, never fatal.
    """
    strategy_md = config_path.resolve().parent.parent / "STRATEGY.md"
    try:
        config = load_config(config_path, strategy_md if strategy_md.exists() else None)
    except ConfigError as exc:
        _print_log({"event": "config_error", "error": str(exc)})
        raise SystemExit(2) from exc

    snapshot_path = runtime_dir / CONFIG_SNAPSHOT_NAME
    if snapshot_path.exists():
        try:
            prior = load_config(snapshot_path)
            check_frozen_variants(config, prior)
        except ConfigError as exc:
            _print_log({"event": "frozen_variant_violation", "error": str(exc)})
            raise SystemExit(2) from exc

    if config.live_variants:
        _print_log(
            {
                "event": "refused_live_config",
                "error": "live variants present but the live path is not built (U11); "
                "this engine is shadow-only and refuses to start",
                "live_variants": [v.id for v in config.live_variants],
            }
        )
        raise SystemExit(2)

    runtime_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(config_path.read_text())
    return config


def build_loop(config: Config, runtime_dir: Path, config_path: Path | None = None) -> EngineLoop:
    """Wire real clients: Gamma/CLOB/Binance feeds, ledger, risk, shadow executor."""
    logs_dir = runtime_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    capture = CaptureLog(logs_dir / "capture.jsonl")
    gamma = GammaClient(capture=capture)
    books = OrderBookClient(capture=capture)
    spot = SpotFeed(capture=capture)
    ledger = Ledger(runtime_dir / "ledger.db")
    staleness = config.risk.max_quote_staleness_sec
    evolution = (
        EvolutionManager(config_path, ledger, runtime_dir) if config_path is not None else None
    )
    control_processor = (
        ControlProcessor(
            strategy_md_path=config_path.resolve().parent.parent / "STRATEGY.md",
            config_path=config_path,
            runtime_dir=runtime_dir,
            ledger=ledger,
        )
        if config_path is not None
        else None
    )
    return EngineLoop(
        config=config,
        ledger=ledger,
        market_state_factory=lambda: MarketState(gamma, books, spot, staleness_sec=staleness),
        executor=ShadowExecutor(ledger),
        risk_manager=RiskManager(config, ledger, runtime_dir),
        resolution_poller=ResolutionPoller(ledger, gamma),
        runtime_dir=runtime_dir,
        evolution=evolution,
        control_processor=control_processor,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engine.main", description=__doc__)
    parser.add_argument("--config", default="config/rulesets.yaml")
    parser.add_argument("--runtime", default="runtime/")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    runtime_dir = Path(args.runtime)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    install_safe_logging()

    try:
        config = startup(config_path, runtime_dir)
        loop = build_loop(config, runtime_dir, config_path)

        if config.strategy_md_drift:
            now = loop.clock()
            detail = (
                f"config strategy_md_version {config.strategy_md_version} does not match "
                "the hash of STRATEGY.md's rules section"
            )
            loop.ledger.record_risk_event(now, "strategy_md_drift", detail)
            loop.log({"event": "strategy_md_drift", "ts": now, "detail": detail})

        if args.once:
            status = loop.run_once(loop.clock())
            loop.log({"event": "tick", "ts": loop.clock(), "status": status})
            return 0
        loop.run_forever()
        return 0  # pragma: no cover — run_forever never returns
    except Exception as exc:  # noqa: BLE001 — last-resort guard: never let a raw
        # traceback (which could carry leaked secret text from a lower layer)
        # reach launchd's captured stderr. Only the exception's type name is
        # logged, never str(exc) — that's exactly what a lower layer may have
        # refused to log itself.
        _print_log(
            {"event": "fatal_exception", "ts": time.time(), "error_type": type(exc).__name__}
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
