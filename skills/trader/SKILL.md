---
name: trader
description: Operate the i-am-not-a-trader-bot trading system. ONLY for the trader Telegram group, and only when its mounts are present — /workspace/extra/trader-bot (code, read-only) and /workspace/extra/trader-runtime (runtime, read-write). Use when the operator sends trader status, trader report, trader stop, trader fund, trader kill, asks about veto notices, or approves a proposed lesson. Do not use in any other group.
---

# Trader

Operates the evidence-gated Polymarket BTC 5m trading system. The engine is a
host daemon; you never touch the process. You interact with it exclusively
through files:

| Container Path | What it is | Access |
|----------------|------------|--------|
| `/workspace/extra/trader-bot` | Engine code, STRATEGY.md, config | **read-only by design** |
| `/workspace/extra/trader-runtime` | Reports, control files, STOP | read-write |

The engine polls `trader-runtime/control/` on every 5-minute bucket rollover,
validates every request, and ledgers every action. Malformed control files are
rejected (moved to `control/rejected/`), so write formats exactly as specified
below.

---

## Data Freshness — check before every answer

Read `/workspace/extra/trader-runtime/export-stamp.json`:

```json
{"rows_trades": 123, "rows_evaluations": 4567, "exported_ts": 1752650000.0}
```

- `exported_ts` within ~15 minutes of now → data is current.
- Older than 15 minutes → the engine is dead or hung. **Say so explicitly**
  ("data is N minutes/hours stale — the engine may be down; run
  `ops/traderctl status` on the host") in every reply until it recovers.
- Stamp missing → report that setup or the engine is broken. Do not guess.

**Never open `trader-runtime/ledger.db` or `ledger-export.db` with sqlite3 for
status answers.** `ledger.db` is a WAL database read across a VM file-sharing
boundary — reads can silently return wrong data. The reports and the stamp
exist precisely so you never need to.

---

## trader status / trader report

1. Read `/workspace/extra/trader-runtime/reports/latest.md` (human summary)
   and `/workspace/extra/trader-runtime/reports/latest.json` (numbers).
2. Read the export stamp for freshness (above).
3. Reply with the per-variant picture: trades, mean P&L, CI, gate/kill status,
   plus the freshness line.
4. If any file under `trader-runtime/control/veto-notices/` exists, surface it
   prominently in every status reply (see Veto Handling).

If `reports/latest.*` is missing, say the reports have not been generated and
suggest `ops/traderctl status` on the host. Never invent numbers.

## trader stop

1. Write the kill-switch file: `touch /workspace/extra/trader-runtime/STOP`
2. Confirm to the operator: the engine parks within one polling tick (~3s).
   Only a human removing the file on the host resumes trading — you never
   remove STOP.

## trader fund <variant> $<amount> / trader kill <variant>

Allocation requests use **absolute set semantics**: the amount is the
variant's new total allocation, not an increment. `trader kill x` is exactly
`trader fund x $0`.

1. **Only on explicit operator instruction.** Echo back your exact
   interpretation first, e.g. "Setting *momentum-v1-c1* allocation to *$50*
   (absolute — replaces the current allocation). Writing the request now."
2. Write `/workspace/extra/trader-runtime/control/allocation-requests/<unix_ts>-<variant>.json`:

```json
{"variant_id": "momentum-v1-c1", "allocation_usd": 50.0, "reason": "operator instruction via Telegram"}
```

3. Tell the operator the engine applies it at the next bucket rollover (≤5
   min) and that rejections land in `control/rejected/` — check there if the
   allocation does not show up in the next report. The engine only accepts
   allocations for variants with status `live` or `pending_promotion`, between
   $0 and the bankroll.

## Veto Handling (promotion notices)

When evolution promotes a challenger, it writes a notice to
`trader-runtime/control/veto-notices/<variant>.json`. Promotion is
**fail-closed**: the 24-hour veto clock starts only after you confirm delivery.

- **Listing**: read every JSON in `control/veto-notices/`. A notice with
  `"delivery_ack_ts": null` is UNDELIVERED — deliver it immediately: message
  the operator with the variant, parent, params diff, statistical case, and
  proposed allocation from the notice.
- **Acknowledging delivery**: only after your message to the operator has
  actually been sent, edit the notice JSON and set `delivery_ack_ts` to the
  current unix epoch seconds (leave every other field untouched). This starts
  the 24h veto window. Treat this as capital-affecting: never ack a notice you
  did not just deliver.
- **Operator vetoes**: on an explicit veto instruction, echo back the variant
  being vetoed, then create the (empty) file
  `/workspace/extra/trader-runtime/control/vetoes/<variant_id>`. The engine
  retires the variant and cancels the promotion.
- No instruction from the operator within the window means the promotion
  proceeds — remind them of the deadline (`delivery_ack_ts` + 24h) when
  relevant.

## Lesson Approval Flow

The nightly verifier drafts lessons into
`/workspace/extra/trader-runtime/proposed-lessons/`.

1. Relay each proposed lesson to the operator verbatim (it is short markdown).
2. **On explicit approval only**, copy the file unchanged into
   `/workspace/extra/trader-runtime/control/approved-lessons/`.
3. The engine's host-side processor appends it to STRATEGY.md's Lessons
   Learned appendix — you never write STRATEGY.md, and the repo mount would
   refuse anyway.
4. On rejection, delete the proposal from `proposed-lessons/` and say so.

---

## Hard Rules

- **NEVER write, edit, or delete anything under `/workspace/extra/trader-bot`.**
  It is mounted read-only by design; the engine's risk caps live there. If a
  write to it ever succeeds, stop and report a setup breach immediately.
- **Never fabricate numbers.** If reports are missing or stale, say exactly
  that and suggest `ops/traderctl status` on the host. A wrong number is worse
  than no number.
- **Capital-affecting actions — fund, kill, veto, delivery-ack — happen only on
  explicit operator instruction**, and you echo back the exact interpretation
  (variant, amount, absolute semantics) before writing the control file.
- If the mounts are missing or empty, report the setup failure (likely the
  mount allowlist or containerConfig — see docs/nanoclaw-setup.md in the repo)
  instead of describing state you cannot see.

## Telegram Formatting

- `*bold*` — single asterisks, NEVER `**double**`
- No `##` headings — use `*bold*` lines instead
- No `[markdown](links)` — paste bare URLs if needed
- Plain English, short lines, `•` bullets
- **Every data-bearing reply includes freshness**, e.g.
  `_data as of 2 min ago_` or `*data is 3 hours stale — engine may be down*`
