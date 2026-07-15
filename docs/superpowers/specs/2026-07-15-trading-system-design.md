# I Am Not A Trader, Bot — Trading System Design

**Date:** 2026-07-15
**Rep:** 30 of the 100 Reps Project
**Status:** Approved design, pre-implementation

## Purpose

An evidence-gated, self-improving trading system for Polymarket BTC 5-minute
Up/Down markets, operated through the user's NanoClaw agent. The system has a
deterministic mathematical core, a plain-English strategy layer the operator
can read and edit, and a learning loop that adapts strategy parameters based
on tracked trade results — without ever risking money on unproven ideas.

The goal is **actual profit**, pursued honestly: the system's first job is to
cheaply and quickly falsify losing strategies. Real money flows only to
rule-sets with statistically demonstrated edge.

## Operator Profile (design constraints)

- **Trading experience:** none. The system must protect the operator by
  default; the plain-English layer is a safety requirement, not a nicety.
- **Math comfort:** statistically literate — can read backtest reports
  critically, understands confidence intervals and sample-size traps. All
  system claims must be auditable from raw ledger data.
- **Risk capital:** $500–1,000 total, treated as tuition. Full loss is
  survivable; blowups are not acceptable as a failure mode.
- **Autonomy posture:** auto-within-limits. The system trades autonomously
  inside hard caps and reports daily in plain English. Capital-allocation
  changes notify the operator with veto rights.
- **Tempo:** high-frequency small bets (BTC 5m markets, many trades/day,
  $2–5 each) to accumulate statistical evidence fast.
- **Logistics:** operator has a Polymarket web account, no API access yet.
  Phase 0 covers programmatic setup.

## Architecture: three layers

### Layer 1 — `STRATEGY.md` (plain English, source of truth)

Every entry rule, exit rule, risk cap, and lesson learned, written as prose
readable in two minutes. Strategy discussions happen by editing this file.
When the English rulebook and machine config disagree, the English wins.

### Layer 2 — `config/rulesets.yaml` (machine translation)

Parameters derived from `STRATEGY.md`: thresholds, caps, timings, per
rule-set variant. The agent verifies rulebook/config consistency; drift is a
reportable defect.

### Layer 3 — `engine/` (deterministic Python)

The LLM never makes a per-trade decision — too slow, too expensive, too
persuadable for a 5-minute market. Claude is the manager and explainer;
Python is the trader.

Modules:

- `market_feed.py` — Polymarket CLOB API client (REST + WebSocket), current
  5m market resolution (slug `btc-updown-5m-<bucket>`), order book snapshots,
  and a BTC spot feed for impulse measurement (prefer the same price source
  Polymarket's resolution oracle uses).
- `signals.py` — computes features on each poll: seconds to close, BTC move
  within the active interval, market skew, spread, top-of-book depth,
  quote staleness.
- `rulesets.py` — each candidate strategy is a **pure function**
  (features → trade/skip decision with side and limit price). Pure functions
  make unit testing and replay testing trivial.
- `executor.py` — dry-run (shadow) or live execution. Limit orders only.
  Re-checks all risk caps immediately before order submission.
- `risk.py` — hard caps enforced in code (see Risk Containment).
- `ledger.py` — SQLite ledger recording every evaluation (including skips
  and why), orders, fills, market resolution, and per-rule-set / per-variant
  attribution. The ledger is what makes profit claims falsifiable.
- `analytics/report.py` — per-variant statistics and the funding-gate check.

## Candidate rule-sets (initial population)

Three hypotheses run head-to-head in shadow mode against live markets:

1. **Momentum-follow** (from the 5min-btc-polymarket reference repo, treated
   as an unvalidated hypothesis): with ~2 minutes left, if BTC has moved
   $70+ in the interval and the favorite trades ≥ $0.70, buy the favorite.
2. **Contrarian fade**: same setup, buy the underdog when it is cheap
   relative to remaining time — betting the crowd overreacts near close.
3. **Skew filter**: trade only when order-book imbalance (resting notional,
   not just price) confirms direction. Fewer trades, higher bar.

Shadow fill model is deliberately pessimistic: hypothetical entries are
logged at the ask plus one tick, and per-market taker fees are read from the
API (Polymarket charges fees on short-duration crypto markets) and deducted.
Shadow results are known to be optimistic even so (no queue position, no
adverse selection); Phase 2 exists to validate the fill model with tiny real
stakes.

## Math backend (all numbers auditable from the ledger)

- **Edge measurement, per variant:** mean P&L per trade with bootstrap 95%
  confidence interval; win rate with Wilson interval; max drawdown; the five
  gambling-vs-trading diagnostics (exit-before-resolution rate, median hold
  time, size–edge correlation, limit-order percentage, profit source).
- **Funding gate (the only door to real money):** a variant is fundable only
  when it has **≥ 100 shadow trades** AND the **lower bound of the 95% CI on
  per-trade EV is above zero** after fees and the pessimistic fill model.
- **Sizing:** quarter-Kelly, capped at min(2% of bankroll, $5) per trade
  initially. Kelly assumes the edge estimate is correct; quarter-Kelly
  assumes it is probably somewhat wrong.
- **Kill criteria (symmetric with the funding gate):** a live variant is
  defunded when the 95% CI upper bound on per-trade EV falls below zero, or
  drawdown exceeds 20% of its allocation, or it hits the daily loss cap 3
  consecutive times. Killing strategies is a feature.

## Learning stack (three speeds)

### 1. Lessons (qualitative, nightly)

The verifier agent reviews each day's ledger against `STRATEGY.md`, drafts
lessons from failures ("filled badly whenever spread > 2¢ — add a spread
guard"), and proposes them as new rulebook rules. Lessons append to
`STRATEGY.md` only with operator approval.

### 2. Champion/challenger parameter evolution (weekly cadence)

Every ~100 trades per variant, the system reviews the ledger and, where the
data suggests a better parameter, spawns a **challenger variant** with that
parameter changed. Guardrails:

- **Never mutate a live strategy in place.** A funded variant's parameters
  are frozen while it trades. All adaptation happens in shadow first —
  otherwise results cannot be attributed to anything.
- **One parameter per challenger.** Change two things and you learn nothing
  about either.
- **Statistical case required.** Every challenger proposal must include the
  effect size and confidence interval from the ledger that motivates it.
  Sub-threshold "patterns" (e.g., day-of-week effects on small n) are noise
  and must not generate challengers.
- **Challengers run in shadow alongside the champion** on identical markets
  and are promoted only by passing the same funding gate the champion did.
  Champions that lose to their challengers are retired.
- **Autonomy split:** spawning shadow challengers is autonomous (free and
  safe). Promotion of a challenger to real money happens automatically only
  if it passes the funding gate, and always sends the operator a
  plain-English promotion notice with veto rights.

### 3. Capital flow (fund/kill gates)

Selection pressure at the money level: variants that demonstrate edge get
capital; variants that lose it are defunded. The population of variants plus
gate-driven capital flow is the system's slowest, most reliable learning.

## NanoClaw integration

- The repo is mounted into a dedicated NanoClaw group's container. Skill at
  `skills/trader/SKILL.md` teaches the agent the system's operating manual.
- Credentials live in `.env` outside the repository; never committed.
- **Two separated agent roles (maker-checker):**
  - **Operator agent** — runs/monitors the engine, answers questions from
    the ledger ("why did you skip the 3:05 market?"), relays commands.
  - **Verifier agent** — nightly scheduled task with a different prompt.
    Audits the ledger against the rulebook (rule violations, guard trips,
    anomalies), drafts lessons and challenger proposals, and sends the
    operator's daily digest in plain English. The verifier never sees the
    operator agent's reasoning, only the ledger and the rulebook.
- Chat commands: `trader status`, `trader report`, `trader start shadow`,
  `trader fund <variant> $<amount>`, `trader kill <variant>`, `trader stop`.

## Risk containment (enforced in code, not prompts)

Hard caps live in the engine where no agent can talk its way past them:

- Per-trade max stake (initially $5).
- Daily loss cap: 5% of bankroll → engine halts for the calendar day.
- Max 20 trades/day across all variants.
- Limit orders only; no market orders anywhere in the codebase.
- Stale-quote guard: skip if market data older than threshold.
- Spread and depth guards: skip when spread too wide or book too thin.
- Halt after 3 consecutive API/DNS/execution failures.
- **Kill-switch file:** engine checks for a `STOP` file every loop iteration
  and halts if present. The chat kill command just writes that file.

## Phases

- **Phase 0 — setup (days).** Polymarket API credentials via
  `py-clob-client`, wallet funding/allowances, NanoClaw group + container
  mount. Unit tests for all rule-set pure functions; replay tests against
  recorded market data.
- **Phase 1 — shadow (2 weeks).** All variants run dry against live markets.
  Daily digests flow. **No parameter tuning during week 1** — no
  peek-and-tweak; that is how overfitting happens.
- **Phase 2 — validation ($100).** If a variant passes the funding gate,
  fund it at tiny stakes. The real purpose is validating that actual fills
  match the shadow fill model. If real fills are materially worse, revise
  the fill model and return to shadow.
- **Phase 3 — scale.** Full tuition bankroll under quarter-Kelly,
  auto-within-limits, champion/challenger evolution active, monthly go/kill
  review.

## Testing

- Unit tests: every rule-set and the risk module (pure functions, exhaustive
  edge cases: boundary prices, zero depth, stale quotes, day-cap boundaries).
- Replay tests: recorded market feed data replayed through the full
  engine-in-dry-run path; assertions on ledger contents.
- Soak test: 48h continuous shadow run before Phase 1 officially starts.
- Fill-model validation: Phase 2's explicit purpose.

## Out of scope (deliberately)

- Combinatorial/cross-market arbitrage (Bregman projection, Frank-Wolfe,
  integer programming): requires ~$500K capital and sub-second execution
  infrastructure to compete. Not viable at this scale.
- Market making, copy trading, and multi-venue trading.
- Any LLM-in-the-loop per-trade decision-making.

## Honest expected outcome

Decent odds all three initial rule-sets fail the funding gate. If so, the
system worked: it cost $0 to learn what gamblers pay thousands to learn, and
the infrastructure — ledger, gates, learning loops — is ready for hypothesis
number four.
