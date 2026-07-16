# NanoClaw Setup (one-time, manual)

Wires the NanoClaw fork at `~/nanoclaw` up as the control plane for this
system: a Telegram operator group with the repo mounted, the `trader` skill,
and the nightly verifier task. Parts of this are deliberately manual — they
touch security configuration that agents must not be able to modify.

Trust model recap (see the plan's Key Technical Decisions): agent containers
get this repo **read-only** and only `runtime/` **read-write**. The engine is
a host launchd daemon; agents talk to it exclusively through files under
`runtime/`.

---

## 1. Create the mount allowlist

NanoClaw refuses all `additionalMounts` until an allowlist exists at
`~/.config/nanoclaw/mount-allowlist.json` (it is intentionally outside the
project root so container agents can't edit it). Create it:

```json
{
  "allowedRoots": [
    {
      "path": "~/i-am-not-a-trader-bot/runtime",
      "allowReadWrite": true,
      "description": "Trader runtime: reports, control files, STOP (agent-writable)"
    },
    {
      "path": "~/i-am-not-a-trader-bot",
      "allowReadWrite": false,
      "description": "Trader bot code + STRATEGY.md (read-only for agents)"
    }
  ],
  "blockedPatterns": ["password", "secret", "token"],
  "nonMainReadOnly": false
}
```

Three things in this file are load-bearing:

- **Root order matters.** `findAllowedRoot()` in `src/mount-security.ts`
  returns the *first* root that contains the requested path, and `runtime/`
  sits inside the repo. The runtime root must be listed **before** the repo
  root, or the runtime mount matches the repo root and is clamped read-only.
- **`"nonMainReadOnly": false` is required, and it is a real tradeoff.**
  `validateMount()` in `src/mount-security.ts` forces every non-main group's
  mount to read-only whenever `nonMainReadOnly` is true — regardless of the
  root's `allowReadWrite` — and the template default is `true`. With the
  default, STOP-via-chat and all control-file writes fail *silently* as a
  read-only mount. Setting it `false` loosens the global read-only clamp for
  **all** non-main groups on this instance. Mitigations: keep every other
  allowlist root `allowReadWrite: false` (as above — the repo root is), and
  note that the only path requested read-write is `runtime/`, which contains
  no code — the engine validates and ledgers everything agents write there.
- **Blocked substrings.** `token`, `secret`, `password` (plus built-in
  defaults like `credentials`, `.env`, `.ssh`) are rejected anywhere in a
  mount path. Our container paths (`trader-bot`, `trader-runtime`) avoid
  them; keep it that way if you ever rename.

The allowlist is cached in memory — restart NanoClaw after creating or
editing it (`launchctl kickstart -k gui/$(id -u)/com.nanoclaw`).

## 2. Register the Telegram trader group

1. Create the Telegram group (see step 7 for who may be in it) and get its
   JID (ask the main agent, or check `/workspace/ipc/available_groups.json`).
2. From the **main** group, ask the main agent to register it with
   `register_group`: name `Trader`, folder `telegram_trader`, default
   trigger. (Folder prefix `telegram_` drives the Telegram formatting rules.)
3. Add the container mounts to the group's `registered_groups` DB row
   (`store/messages.db`, column `container_config`). Easiest is asking the
   main agent to set the group's `containerConfig` to exactly:

```json
{
  "additionalMounts": [
    {
      "hostPath": "~/i-am-not-a-trader-bot",
      "containerPath": "trader-bot",
      "readonly": true
    },
    {
      "hostPath": "~/i-am-not-a-trader-bot/runtime",
      "containerPath": "trader-runtime",
      "readonly": false
    }
  ]
}
```

The mounts appear in the trader group's containers at
`/workspace/extra/trader-bot` (read-only) and
`/workspace/extra/trader-runtime` (read-write). Paths avoid the blocked
substrings from step 1.

## 3. Sync the trader skill into the fork

**This repo is canonical; the fork holds a copy.** Never edit the fork's copy
directly — edit `skills/trader/SKILL.md` here, then re-copy:

```bash
mkdir -p ~/nanoclaw/container/skills/trader
cp ~/i-am-not-a-trader-bot/skills/trader/SKILL.md ~/nanoclaw/container/skills/trader/SKILL.md
```

Container skills are synced to **every** group on each run; the skill's
frontmatter description scopes it to the trader group's mounts, which is why
that description must stay narrow.

## 4. Verify the setup (do not skip)

In the trader Telegram group, ask the agent to run each check and report:

1. **Runtime mount is writable:**
   `touch /workspace/extra/trader-runtime/.write-test && rm /workspace/extra/trader-runtime/.write-test`
   — must succeed. If it fails with a read-only error, step 1's
   `nonMainReadOnly` or root ordering is wrong.
2. **Repo mount is read-only:**
   `touch /workspace/extra/trader-bot/.write-test` — must **fail**. If it
   succeeds, stop: the trust boundary is broken; fix the allowlist before
   going further (and delete the stray file).
3. **STOP round-trip:** with the engine running on the host, send
   `trader stop`. Confirm `runtime/STOP` appears on the host and the engine
   log shows `parked` within one tick. Then remove STOP **yourself on the
   host** (`rm ~/i-am-not-a-trader-bot/runtime/STOP`) — only a human clears
   it — and confirm the engine resumes.

## 5. Schedule the nightly verifier

From the trader group (or main with `target_group_jid`), have the agent call
`schedule_task` with:

- `schedule_type`: `cron`, `schedule_value`: `0 2 * * *` (2am local)
- `context_mode`: `isolated`
- `script`: `bash /workspace/extra/trader-bot/ops/verifier-precheck.sh`
- `prompt`:

> You are the nightly trading verifier — a different role from the operator
> agent. Audit the ledger export against the rulebook. Copy the export first:
> `cp /workspace/extra/trader-runtime/ledger-export.db /tmp/audit.db` and query
> `/tmp/audit.db` with sqlite3 (never open ledger.db — WAL does not survive
> the VM mount boundary). Read the rules in
> `/workspace/extra/trader-bot/STRATEGY.md`. Check: rule violations (more than
> one entry per variant per bucket, stakes over $5, trades outside entry
> windows), guard trips and risk_events since the last digest, anomalies
> (fee-rate changes, resolution timeouts, drawdowns), pending veto notices in
> `/workspace/extra/trader-runtime/control/veto-notices/` — escalate any with
> null delivery_ack_ts as UNDELIVERED. You may draft lesson proposals into
> `/workspace/extra/trader-runtime/proposed-lessons/` (never write STRATEGY.md
> or anything under trader-bot). Then send a short Telegram digest via
> send_message with sender "Verifier": one line per variant (n, mean P&L, gate
> status), anomalies, pending notices, data freshness. Single-asterisk bold,
> no ## headers, no markdown links. If the pre-check data shows a stale or
> missing export stamp, lead with a dead-engine alert.

The pre-check keeps this cheap: the LLM wakes only on new ledger rows, a
stale/missing export stamp (dead-engine alarm), or an unacked veto notice.
Test the script in the container sandbox before scheduling, per NanoClaw's
own guidance:

```bash
TRADER_RUNTIME_DIR=/workspace/extra/trader-runtime bash /workspace/extra/trader-bot/ops/verifier-precheck.sh
```

## 6. Hourly liveness alert (Phase 2/3 — while live capital is deployed)

Once real money is on (Phase 2), 24h detection latency for a hung engine is
too slow. Add an hourly `schedule_task` (cron `0 * * * *`, isolated) whose
`script` runs the liveness half only and alerts via a direct bash Telegram
`sendMessage` call — **no LLM wake** (`wakeAgent` always false; the script
does the alerting itself when the export stamp is older than
`STALE_EXPORT_SEC`). Wiring the bot credential into that curl goes through
OneCLI at Phase 2 setup time; do not park a token in the repo or the mounts.
This is documented here as a Phase 2/3 step — do not set it up during shadow
phase, where the nightly dead-engine alarm in step 5 is enough.

## 7. Telegram authorization assumption

**The trader group must contain only the operator (Robert).** Anyone who can
post in that group can stop the engine, move allocations between variants,
ack promotion notices, and approve lessons — group membership *is* the
authorization model. Do not add other people, bots, or bridges to the group.
For defense in depth, add a sender allowlist for the group's JID in
`~/.config/nanoclaw/sender-allowlist.json` (see the main group's CLAUDE.md)
restricted to the operator's sender id.
