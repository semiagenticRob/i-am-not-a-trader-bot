# Code review synthesis — run 20260716-062402-2b67dbe2

Scope: `git diff ce7a260482e103336cd1550c3cc8f390fc8627d9..HEAD` (all 11 implementation units, U1-U11).
Mode: autofix. Plan: `docs/plans/2026-07-15-001-feat-evidence-gated-trading-system-plan.md` (plan_source: explicit).
12 reviewers dispatched (correctness, testing, maintainability, project-standards, agent-native, learnings-researcher,
security, performance, reliability, adversarial, cli-readiness, kieran-python). All 12 returned successfully.

**Requirements completeness (R1-R11): all 11 met.** No unaddressed plan requirements. R3 (shadow fill pessimism) and
R7 (learning stack veto window) each have a confirmed defect below, but the requirement itself is implemented.

## STATUS: fixer stage was interrupted (session limit) before applying ANY changes.
Working tree is clean except this `.context/` directory. Nothing below has been applied yet.

## P0 — verified by direct code read, not just reviewer claim

1. **LiveExecutor writes terminal ledger status ("failed"/"cancelled") without positively confirming exchange
   order state**, in three places, all in `engine/live_client.py`:
   - `execute()` line 224: `except Exception` on `post_order` writes `status="failed"` even though the order may
     have actually been accepted exchange-side (ambiguous submission).
   - `reconcile_on_startup()` line 315: `if match is None` writes `status="failed"` — but a FILLED order also
     disappears from `get_open_orders()`, so a filled position looks identical to "never placed" and gets silently
     lost.
   - `cancel_stale()` / `reconcile_on_startup()`'s stale-cancel branch, line 276: writes `status="cancelled"`
     without re-querying the exchange after `cancel_order()` — a fill racing the cancel gets mis-recorded as a
     zero-cost cancellation.
   - Root cause: no `order_id` is persisted to the ledger at submission time, and none of these three paths calls
     `get_order()` to positively confirm the order's fate before writing a terminal status.
   - Fix direction: persist `order_id` on the trade row at submission (even a best-effort second write right after
     `post_order()` succeeds), and query `get_order()`/trade history before ever writing `failed`/`cancelled`.
   - Reviewers: reliability (P0, live_client.py:315) + adversarial (P0×2, live_client.py:224 and :276). Cross-corroborated.
   - **Currently unreachable**: `LiveExecutor` is not wired into `engine/main.py`'s loop yet (main.py refuses any
     `status: live` variant — see `docs/phase2-runbook.md` section 3 step 4). Must fix before that wiring lands.

2. **`safe_log_line`'s credential redaction is not enforced process-wide.** `engine/main.py`'s `main()` (line
   347-379) has no top-level exception guard — an uncaught exception during live trading would let Python's default
   excepthook print a raw traceback to stderr, captured by launchd into `runtime/logs/engine.log`, which is
   agent-readable (RW mount). Separately, `engine/market_feed.py` uses stdlib `logging.warning()` (11 call sites)
   which never routes through `safe_log_line` at all.
   - Reviewer: security (P0, engine/main.py:378, confidence 75).
   - Fix direction: wrap `main()`'s entrypoint in try/except that logs only a redacted summary via `safe_log_line`;
     route `market_feed.py`'s `logger.warning()` calls through the same contract (or a logging Handler that pipes
     through it) before `LiveCredentials`/`derive_l2`/`build_venue_client` are wired into the startup path.
   - **Verified low current risk**: market_feed.py never touches credentials (public Gamma/Binance/CLOB market data
     only), so today's actual exposure is limited to future live-trading exception text. Must close before Phase 2.

3. **Veto-notice `delivery_ack_ts` is trusted unconditionally from an agent-editable JSON file with zero
   validation**, collapsing the 24h operator veto window that the whole champion/challenger design was built to
   guarantee. `engine/evolution.py:575`: `notice["veto_deadline_ts"] = notice["delivery_ack_ts"] + VETO_WINDOW_SEC`
   — no check that `delivery_ack_ts` is a finite number within `[created_ts, now]`. Confirmed by reading
   `skills/trader/SKILL.md:97-99`: the NanoClaw agent directly edits this JSON file and sets `delivery_ack_ts` to
   "the current time" on its own say-so — enforcement is prompt-level trust only ("never ack a notice you haven't
   actually sent"), not code-level.
   - Reviewer: adversarial (P0, engine/evolution.py:575, confidence 90).
   - Fix direction: reject/ignore any `delivery_ack_ts` outside `[created_ts, now]` or non-numeric; consider routing
     the ack write through a host-validated pipeline instead of trusting an unconditionally agent-edited file.
   - This directly undermines the "fail-closed... clock starts only after delivery-ack, never from write time"
     design goal established during brainstorming. High priority even though not yet exploited (no live variants
     have gone through promotion yet).

## P1 (12 findings — not individually re-verified beyond reviewer evidence; anchors 75-90, all plausible on inspection)

- **executor.py:97** — Shadow fill clamp `min(limit_price + TICK, 0.99)` can produce `filled_price < limit_price`
  for asks near the top tick (>0.98), slightly inflating shadow-mode EV for high-priced entries. Verified: no
  upper bound on `limit_price` anywhere in rulesets.py/risk.py. [correctness]
- **risk.py / ledger.py:382** — `Ledger.consecutive_unresolved()` is implemented and unit-tested but never called
  by `RiskManager` or the main loop; `ResolutionPoller`'s docstring falsely claims it "feeds engine-halt logic".
  Verified via grep — zero call sites outside its own test. [correctness]
- **control.py:152** — host-fault "deferred, don't crash" branches (unreadable file, ControlError/ConfigError
  during append/allocation processing) are never exercised by any test. [testing]
- **evolution.py:459** — `EvolutionManager.propose_and_spawn`, the actual method wired into `engine/main.py`'s
  rollover hook, is never invoked by any test (only its two constituents are unit-tested separately). [testing]
- **report.py:118** — Bootstrap CI resamples the FULL historical trade set on every 5-minute rollover, unbounded
  as the ledger grows; same pattern reused hourly in evolution.py. Verified: `realized_pnl_rows()` has no
  limit/pagination. Practical urgency is low today given ~20 live trades/day caps, but worth fixing before scale.
  [performance]
- **executor.py:107** — No startup reconciliation for shadow trades left `status='open'` by a crash between the
  two ledger writes (open row + fill transition) — silently corrupts the evidence-generation pipeline (undercounts
  shadow trades toward the funding gate) with no risk_event or operator visibility. [reliability]
- **live_client.py:388** — Vendor CLOB client constructed with no explicit request timeout. [reliability]
- **evolution.py:496** — An exception partway through `review_promotions()` blackholes config persistence for
  every variant processed earlier in the same cycle, silently discarding a genuine operator ack on retry. [adversarial]
- **evolution.py:293** — `config/rulesets.yaml` is read-modify-written by both `evolution.py` and `control.py` with
  no locking/CAS, racing the documented manual-edit promotion workflow. Same line as the maintainability P2 below
  (duplicated rewrite logic) — a shared fix location for both. [adversarial]
- **executor.py:52** — `safe_log_line`'s `secret_key_name` regex is snake_case-only (`api_key`) and would not match
  the vendor's own camelCase field names (e.g. `orderID`). NOTE: do not blindly widen this — `live_client.py`
  intentionally logs `orderID`-shaped response dicts in error messages (e.g. "no order id in post_order response
  keys=[...]"); over-broadening the pattern could make `safe_log_line` reject legitimate order-tracking logs.
  Needs a human check, not an automatic regex widen. [adversarial]
- **tests/test_risk.py:179** — The "risk.py is the sole constructor of `Approved`" invariant is enforced only by a
  literal substring grep (`'Approved('`) that any import-aliasing or whitespace variation would bypass. [adversarial]
- **ops/traderctl:84** — `status` has no machine-readable (`--json`) output mode and always exits 0 regardless of
  STOP/running state — agent callers must scrape prose. [cli-readiness]

## P2/P3 — lower priority, see individual reviewer artifacts for full list
- maintainability: duplicated config-rewrite-with-restore-on-invalid logic between evolution.py:293 and control.py.
- kieran-python: 5 type-hint gaps (3 of which are in the safe_auto queue below).
- cli-readiness: 4 more advisory UX items (logs arg parsing, --help exit code, report --json mode).
- reliability: consecutive-failure halt counter conflates unrelated failure domains (feed/executor vs housekeeping).
- agent-native (unstructured): trader skill never tells the agent it can read `runtime/logs/engine.log` for crash
  diagnostics, despite having RW access to that file already — a capability-hiding gap, not an intentional asymmetry.

## Safe_auto fixer queue (APPROVED, not yet applied — this is what the interrupted fixer agent was doing)

1. `engine/ledger.py` `open_trades()` — add optional `variant_id`/`mode` kwargs, push filter into SQL; update
   `engine/risk.py`'s `_remaining_allocation` caller to use them instead of Python-side filtering. Add a
   `tests/test_ledger.py` case.
2. `engine/ledger.py` — add `CREATE INDEX IF NOT EXISTS idx_trades_mode_ts ON trades (mode, ts)` to schema setup.
3. `engine/signals.py` `compute_snapshot()` — type-annotate `market`/`up_book`/`down_book` params via a
   `TYPE_CHECKING`-guarded import from `engine.market_feed` (circular-import avoidance).
4. `engine/executor.py` `ResolutionPoller.__init__` — type-annotate `gamma_client` the same way.
5. `analytics/report.py` `bootstrap_mean_ci` + `engine/evolution.py` `bootstrap_diff_ci` — annotate sample args as
   `Iterable[float]`.

Full fixer prompt (for exact re-dispatch) is in this run's conversation; the gist is above. Re-run via a single
fixer subagent, then `.venv/bin/pytest -q` + `.venv/bin/ruff check .`, then commit as
`fix(review): apply autofix feedback`.

## Verdict: Ready with fixes
Shadow-mode-only merge (current PR scope) is not blocked — all 3 P0s live exclusively in U11's live-executor path,
which is not wired into the running engine and is explicitly gated behind `docs/phase2-runbook.md`'s manual
venue-decision + go-live steps. **All 3 P0s must be fixed before that gate is opened.** Recommend adding them as
named checklist items in `docs/phase2-runbook.md` section 3 (go-live checklist) so they can't be lost.
