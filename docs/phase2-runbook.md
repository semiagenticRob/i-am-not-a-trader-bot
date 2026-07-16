# Phase 2 Runbook — Live Execution ($100, fill-model validation)

This runbook is the operational half of U11. The code half
(`engine/live_client.py`, `traderctl go-live`) is built and tested against
mocks; **nothing in it may touch real money until Section 1's decision gate
is executed and recorded here by a human.**

Three independent switches must all be on before a live order exists:

1. a variant has `status: live` in `config/rulesets.yaml` (promotion flow),
2. a funding-gate pass is recorded for that variant (analytics),
3. `traderctl go-live` has been run (this runbook, Section 3).

---

## Section 1 — THE VENUE DECISION GATE (do this before anything else)

**Status: NOT EXECUTED.** No live-trading step below this section may run
until this section's findings are filled in and `runtime/venue-verified`
exists.

Background (verified research, 2026-07). There are **two separate Polymarket
venues** and they are not interchangeable:

- **Main CLOB** (`polymarket.com`, `clob.polymarket.com`): the venue this
  system integrates. Client: `py-clob-client-v2` (PyPI `py_clob_client_v2`)
  — the original `py-clob-client` was **archived 2026-05-25 and its orders
  are rejected**; never install it. Auth: L1 EOA key (EIP-712, Polygon chain
  137) derives L2 HMAC creds. BTC 5m Up/Down markets confirmed to exist here.
  **US-user access is pending CFTC action** (filed 2026-04-28, unresolved as
  of 2026-07; a June 2026 probe is ongoing).
- **Polymarket US** (`polymarket.us`, QCX LLC, CFTC-regulated): a **separate
  venue with a separate Ed25519 API** and separate SDK
  (`polymarket-us-python`). `py-clob-client-v2` does **not** work there.
  **Unconfirmed whether it lists BTC 5m markets.**

The operator is US-based with a web account and no API setup, so which venue
the account can actually trade is an open question this gate answers.

### Steps (record every answer in the Findings block below)

1. **Identify the account's venue.** Log in via the web. Which domain hosts
   the account — `polymarket.com` or `polymarket.us`? Can it deposit, hold a
   balance, and place a manual order there today?
2. **Check current CFTC status of US-user access to the main CLOB.** The
   2026-04-28 filing was unresolved as of this writing. Search for a current
   CFTC determination or Polymarket announcement. Do not proceed on "probably
   fine."
3. **Confirm BTC 5m Up/Down markets exist on the accessible venue.** On the
   main CLOB they exist (slug `btc-updown-5m-{unix_ts}`). On Polymarket US,
   check the live market list — this is unconfirmed.
4. **Decide:**
   - **Main CLOB accessible (legally and practically)** → record findings
     below, then `touch runtime/venue-verified` and continue with Section 2.
   - **Only Polymarket US accessible AND it lists BTC 5m markets** → **STOP.**
     This is a separate, pre-scoped integration (`polymarket-us-python`,
     Ed25519 auth) requiring its own plan. **Do not adapt
     `engine/live_client.py` ad hoc** — it is main-CLOB only by design.
     Record findings below and open the follow-up.
   - **Neither venue tradable / no 5m markets on the accessible venue** →
     STOP. The system stays in shadow mode (which needs no venue at all);
     re-check when the CFTC status changes.

### Findings (fill in when executed — this block is the record)

    Date executed:
    Executed by:
    Account venue (polymarket.com / polymarket.us):
    Manual web order possible today (yes/no):
    CFTC status of US access to main CLOB (source + date):
    BTC 5m markets on accessible venue (yes/no, evidence):
    DECISION (proceed main CLOB / stop: Polymarket-US follow-up / stop: no venue):
    Marker created (`touch runtime/venue-verified`) (yes/no):

`runtime/venue-verified` is created **manually and only here**. It is what
`traderctl go-live` check 4 looks for; no code creates it.

---

## Section 2 — Credential setup (after the gate clears, main CLOB only)

1. **Wallet.** Use (or create) a Polygon wallet dedicated to this system —
   never a personal main wallet. Fund it with the Phase 2 allocation only
   (USDC.e on Polygon plus a little POL for gas).
2. **Key file.** Put the L1 private key in an env file **outside the repo
   tree** (the repo is mounted read-write into agent containers; the home
   config dir is not mounted at all):

       mkdir -p ~/.config/i-am-not-a-trader-bot
       $EDITOR ~/.config/i-am-not-a-trader-bot/env   # add: POLYMARKET_PRIVATE_KEY=0x...
       chmod 600 ~/.config/i-am-not-a-trader-bot/env

   `LiveCredentials.load` **refuses any file whose mode has group/other
   bits** (must be exactly 0600) and refuses a missing
   `POLYMARKET_PRIVATE_KEY`. The key never appears in logs: the credentials
   object masks its repr, and `safe_log_line` hard-fails on any
   credential-shaped log content as the backstop.
3. **Install the live extra** (shadow installs never carry it):

       .venv/bin/pip install '.[live]'

   This installs `py_clob_client_v2`. Pin the version in a lockfile once the
   smoke test passes — the v1 client was archived abruptly and v2 may churn.
4. **L2 derivation** happens automatically at engine startup
   (`derive_l2`: L1 key → `create_or_derive_api_key()` → client re-init with
   the derived HMAC creds). No manual step, nothing else to store.
5. **Key rotation / compromise procedure:**
   1. `ops/traderctl stop` (writes STOP first, fail-safe).
   2. On Polymarket, cancel all open orders and withdraw the balance to a
      fresh wallet.
   3. Rotate: new L1 key into the env file (`chmod 600` again); the next
      startup re-derives L2 creds from the new key.
   4. Audit: compare the exchange's trade history against the ledger
      (`runtime/ledger.db`, `trades` table) for orders the ledger does not
      know — any such order means the old key was used by someone else;
      treat the old wallet as hostile and never re-fund it.

---

## Section 3 — Go-live checklist

Preconditions, in order (each is one of the three independent switches or a
verification of it):

1. **Funding-gate pass recorded.** `runtime/reports/latest.json` (analytics)
   shows the candidate variant passing the funding gate (n ≥ gate.min_trades,
   CI excludes zero). No pass, no promotion — full stop.
2. **Promotion.** Human edits `config/rulesets.yaml`: variant `status: live`,
   `allocation_usd: 100`. Commit the change (the integrity check requires a
   clean tree — the commit IS the human review record).
3. **`ops/traderctl go-live`.** Runs five checks, refusing on the first
   failure:
   1. tracked `engine/` + `config/` clean vs git,
   2. at least one `status: live` variant in config,
   3. `~/.config/i-am-not-a-trader-bot/env` exists with mode 600,
   4. `runtime/venue-verified` exists (Section 1's manual marker),
   5. typed confirmation — literally `GO LIVE`, read from the tty.

   On success it writes `runtime/control/go-live` with a UTC timestamp.

   *Manual test (safe, arms nothing):* run `ops/traderctl go-live` with a
   dirty tree or without the venue marker and confirm it refuses at the
   right step with the right message; run it fully only when you mean it.
4. **Flip the engine's live-mode gate.** `engine/main.py:startup()` currently
   **refuses to start when any variant is live** (the
   `refused_live_config` block, `if config.live_variants:` — around line
   287). That refusal is correct until this runbook is executed. Flipping it
   is a deliberate, reviewed one-line-site code change: replace the refusal
   with wiring that (a) requires `runtime/control/go-live` to exist, (b)
   loads `LiveCredentials`, builds the venue client
   (`live_client.build_venue_client`), and constructs `LiveExecutor` for
   live variants, and (c) calls `reconcile_on_startup` **before the first
   poll tick** — reconciliation before any new live order is mandatory
   (orphaned GTC orders from a crash must never ride to resolution
   untracked). Commit the change; the integrity check must pass again
   afterwards.
5. **Start small.** $100 total allocation. The engine's own caps hold
   regardless: $5 hard per-trade max, daily loss cap, max live trades/day.
6. **First order reconciliation (manual).** Watch the first live order
   ($1–5, quarter-Kelly will size it small anyway) end to end:
   - ledger row `open` appears **before** the order is visible on the
     exchange (write-ahead order),
   - the order appears in the Polymarket UI with the exact intended price
     and GTC limit type,
   - on fill: ledger `filled` row's price and fee match the UI,
   - on non-fill: cancellation at exit-before-sec is visible in the UI and
     the ledger row is `cancelled`.
   Any mismatch → `ops/traderctl stop-trading`, investigate, and check the
   adapter guesses (Section 6) before resuming.

   Known residual gap (12-persona review, P0 #1, fixed but not fully closed):
   `execute()` attaches `order_id` to the ledger row right after submission
   and reconciles/cancels via `get_order` confirmation, so a filled order can
   no longer be silently lost as `failed`/`cancelled` in the common case. The
   one race this cannot catch is a `post_order` call that raises AND the
   order fills before the recovery check — it has no `order_id` and
   `VenueClient` has no trade-history endpoint to query. Watch for a `failed`
   live trade whose token/price also shows a fill in the Polymarket UI during
   this step; if seen, treat as a live incident (stop-trading, reconcile
   manually) before resuming.

---

## Section 4 — Fill-model validation (the point of Phase 2)

Phase 2 exists to measure the gap between the shadow fill model
(pessimistic ask+1-tick, instant fill) and reality — **especially selective
fill**: limit orders fill preferentially when the move goes against you, a
bias invisible in fill *prices* alone.

- **Minimum sample: 50–100 live trades before ANY Phase 3 scaling
  decision.** At n=20 the win-rate standard error is ~11 points — that
  cannot detect the selective-fill bias this phase exists to measure. Do not
  eyeball an early winner.
- **Compare fill RATES and non-fill outcomes, not just fill prices:**
  - live fill rate (filled / (filled + cancelled)) vs the shadow model's
    100% assumption — the `cancelled` rows written by `cancel_stale` are
    this denominator, which is why cancellations are ledger data, not noise;
  - win rate conditional on fill, live vs shadow, same variant, same period;
  - the counterfactual on cancelled orders: what shadow says would have
    happened had they filled — if unfilled orders were disproportionately
    the winners, that is selective fill, and shadow EV overstates live EV;
  - realized fee per trade vs the formula fee.
- **Acceptance thresholds: TO BE FILLED IN after venue verification**, once
  actual tick size and queue behavior on the 5m books are observed.
  Placeholders to beat, pending calibration:

      Live fill rate:                >= ____ %   (fill in)
      Live-vs-shadow EV gap:         <= ____ ¢/trade (fill in)
      Selective-fill counterfactual: unfilled-order shadow EV not
                                     significantly better than filled (test: ____)

- **Scaling is gradual.** A passing comparison at $100 justifies the next
  increment (e.g. $250), not a jump to full bankroll. Repeat the comparison
  at each increment; any threshold breach → back to shadow and recalibrate
  the shadow fill model with the observed data.

---

## Section 5 — Rollback (any time, any reason)

Fastest first; each step is independent and fail-safe:

1. **STOP file:** `touch runtime/STOP` (or `ops/traderctl stop-trading`) —
   the engine parks within one poll tick. Agents can do this too; STOP only
   ever halts trading, never starts it.
2. **Full stop:** `ops/traderctl stop` — writes STOP, then unloads/kills the
   daemon.
3. **Disarm live:** remove `runtime/control/go-live`; revert the variant to
   `status: retired` (allocation-request 0 / defund) in config and commit.
4. **Defund:** withdraw the wallet balance. The ledger keeps the full record
   either way — rollback loses no evidence.

After any rollback with resting orders possible, the next live startup's
`reconcile_on_startup` cancels/adopts/fails orphans and ledgers every action
as a `risk_event` — check `risk_events` for `reconcile_*` rows after restart.

---

## Section 6 — Known vendor-API guesses (verify in the first smoke test)

`engine/live_client.py` isolates every `py_clob_client_v2` call inside
`_ClobV2Adapter` + `derive_l2`. Tests mock the seam, so these specifics are
**unverified guesses** (marked `GUESS` in the code) that the Section 3
first-order smoke test must confirm:

- order placement call shape (`OrderArgs` → `create_order` → `post_order`
  with `OrderType.GTC`) and the order-id key in the response
  (`orderID`/`order_id`/`id`),
- `get_order` response statuses (`LIVE`/`MATCHED`/`CANCELED`) and whether a
  fill's actual price/fee are reported (fee falls back to the published
  formula `shares × feeRate × p × (1−p)` when absent),
- `get_orders` open-order list field names (`id`, `asset_id`, `price`),
- `create_or_derive_api_key()` factory kwargs (`host`, `key`, `chain_id`,
  `creds`),
- share-size rounding / minimum order size on 5m books (not handled yet —
  observe and add if the venue rejects fractional sizes).

Confirmed by research (2026-07): GTC/GTD limit + FOK/FAK market are the
order types (this system submits **GTC limit only** — grep `ORDER_TYPE`);
L1→L2 two-step auth exists under that method name; fees are taker-only and
read per-market.
