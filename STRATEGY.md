# STRATEGY.md — The Rulebook

This file is the single source of truth for what this system trades and why,
written in plain English. When this file and `config/rulesets.yaml` disagree,
this file wins and the disagreement is a defect to report.

Everything from here to the "Lessons Learned" heading is covered by the
`strategy_md_version` hash in the config. Machine-appended sections (lessons,
promotion blocks) go under "Lessons Learned" and never change the hash.

<!-- rules:begin -->

## What this system is

We trade Polymarket's BTC 5-minute Up/Down markets. Every 5 minutes a market
asks: will Bitcoin's price be at or above its opening price when this window
closes? We run several candidate strategies ("variants") side by side. New
variants trade on paper only ("shadow mode"). A variant earns real money only
by passing the funding gate below. Variants that stop performing lose their
money again. The system's first job is to prove cheaply which ideas are wrong.

## Strategy hypotheses

**Momentum-follow** (from the 5min-btc-polymarket reference repo; unproven):
in the last 1–2.5 minutes of a window, if Bitcoin has already moved $70 or
more within the window and the market's favorite side costs at least $0.70,
buy the favorite. The bet: late momentum tends to hold to the close.

**Contrarian fade**: same late window and the same $70+ move, but buy the
*underdog* when it costs $0.30 or less. The bet: the crowd overreacts to the
move and cheap underdogs are slightly underpriced.

**Skew filter**: only trade when the order book's resting money (not just
price) leans at least 2:1 toward one side in the late window with a real
move behind it, and go with the money. The bet: real resting capital is
better informed than price alone. Fewer trades, higher bar.

## Non-negotiable risk rules

These are enforced in engine code. No agent, prompt, or chat message can
override them.

1. At most one entry per variant per 5-minute market, shadow and live.
2. Live trades only ever use limit orders. No market orders exist in this
   codebase.
3. A live trade risks at most $5 or 2% of bankroll, whichever is smaller.
4. If realized live losses reach 5% of bankroll in a calendar day, the
   engine halts live trading until the next day.
5. At most 20 live trades per day across all variants.
6. Skip any market where the spread is wider than 3 cents, the top of book
   is thinner than $30, or our market data is older than 8 seconds.
7. Exit (cancel resting orders) at least 20 seconds before window close.
8. Three consecutive API failures halt the engine.
9. A `STOP` file in `runtime/` halts everything within one polling tick.
   Anyone or anything can write it. Nothing but a human restart clears it.

## Sizing rules

Shadow trades are always recorded at a fixed $5 reference stake so every
variant's evidence is comparable and a brand-new variant can build a record.
Live trades are sized by quarter-Kelly from the variant's measured edge,
clamped by risk rule 3. Kelly assumes the edge estimate is right;
quarter-Kelly assumes it is probably somewhat wrong.

## The funding gate

A variant may trade real money only when BOTH hold, checked only at
pre-registered sample sizes (100, 150, 200, … shadow trades — never
continuously, so a lucky streak between checkpoints cannot sneak through):

- it has at least 100 shadow trades, and
- the lower bound of the 95% confidence interval on its per-trade profit,
  after fees and the pessimistic fill model, is above zero.

## The kill criteria

A live variant is defunded when any of these holds:

- the upper bound of the 95% CI on its per-trade profit falls below zero;
- its drawdown exceeds 20% of its allocation;
- it participates in 3 consecutive daily-loss-cap halts.

## Learning rules

Nightly, a separate verifier agent audits the ledger against this file and
may propose lessons; a lesson becomes a rule here only after operator
approval, applied by the engine's control-file processor (agents never write
this file directly). Every ~100 trades the system may spawn challenger
variants — one parameter changed, backed by a statistical case from the
ledger — which run in shadow and face the same funding gate. Promotion to
real money starts a 24-hour veto clock that begins only when the operator's
notice is confirmed delivered; an undelivered notice blocks promotion. Every
variant that reaches live status must be described in plain English in this
file's appendix.

<!-- rules:end -->

## Lessons Learned

Machine-appended below. Each entry carries a date, the evidence, and the rule
change it motivated. Nothing above the `rules:end` marker may be edited by
automation.
