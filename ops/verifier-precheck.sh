#!/bin/bash
# verifier-precheck.sh — NanoClaw schedule_task pre-check for the nightly verifier.
#
# Contract (src/task-scheduler.ts): this script runs before the LLM agent; the
# LAST line of stdout must be JSON {"wakeAgent": bool, "data": {...}}. When
# wakeAgent is false the verifier LLM is never invoked (cost control).
#
# Wake conditions (OR):
#   - export stamp file missing entirely (engine never exported / runtime broken)
#   - new trade/evaluation rows in runtime/export-stamp.json since the last
#     digest's high-water mark (runtime/verifier-highwater.json)
#   - dead-engine alarm: zero new rows while the engine should be active — the
#     5m BTC market runs 24/7, so an export stamp older than STALE_EXPORT_SEC
#     (default 900s; the engine exports every 5-minute bucket rollover) means
#     the daemon is dead or hung. launchd KeepAlive covers crashes; this covers
#     hangs. "No new rows" must never silence the alarm it exists to raise.
#   - any veto notice in runtime/control/veto-notices/ with a null/absent
#     delivery_ack_ts: an undelivered promotion notice blocks promotion
#     (fail-closed) and MUST escalate rather than sit silently.
#
# Sleep (wakeAgent:false) ONLY when: stamp exists and is fresh AND no new rows
# since high-water AND no pending (unacked) veto notices.
#
# The high-water file is updated only when waking, so an anomaly that the
# verifier saw once is not re-reported forever, but an unwoken verifier never
# advances its own baseline.
#
# Runtime dir resolution: $1 if given, else $TRADER_RUNTIME_DIR, else the
# container mount path /workspace/extra/trader-runtime.
#
# jq-free: JSON handling is inline python3 (present in the NanoClaw container).
# If the python check itself fails, we fail toward waking — a broken pre-check
# must page the verifier, not silence it.

set -u

RUNTIME_DIR="${1:-${TRADER_RUNTIME_DIR:-/workspace/extra/trader-runtime}}"
STALE_EXPORT_SEC="${STALE_EXPORT_SEC:-900}"
export RUNTIME_DIR STALE_EXPORT_SEC

python3 - <<'PY'
import json
import os
import time
from pathlib import Path

runtime = Path(os.environ["RUNTIME_DIR"])
stale_sec = float(os.environ["STALE_EXPORT_SEC"])
now = time.time()

stamp_path = runtime / "export-stamp.json"
highwater_path = runtime / "verifier-highwater.json"
notices_dir = runtime / "control" / "veto-notices"

reasons = []
data = {}

# -- pending (unacked) veto notices: undelivered promotion notices escalate ----
pending_notices = []
if notices_dir.is_dir():
    for notice_path in sorted(notices_dir.glob("*.json")):
        try:
            notice = json.loads(notice_path.read_text())
            unacked = notice.get("delivery_ack_ts") is None
        except (OSError, ValueError):
            unacked = True  # unreadable notice is worse than an unacked one
        if unacked:
            pending_notices.append(notice_path.name)
if pending_notices:
    reasons.append("unacked_veto_notice")
    data["pending_veto_notices"] = pending_notices

# -- export stamp: new rows / dead-engine alarm --------------------------------
stamp = None
if not stamp_path.exists():
    reasons.append("missing_stamp")
    data["stamp_path"] = str(stamp_path)
else:
    try:
        stamp = json.loads(stamp_path.read_text())
        rows = {
            "rows_trades": int(stamp["rows_trades"]),
            "rows_evaluations": int(stamp["rows_evaluations"]),
        }
        exported_ts = float(stamp["exported_ts"])
    except (OSError, ValueError, KeyError, TypeError):
        reasons.append("unreadable_stamp")
        stamp = None
if stamp is not None:
    highwater = {"rows_trades": 0, "rows_evaluations": 0}
    if highwater_path.exists():
        try:
            prior = json.loads(highwater_path.read_text())
            highwater = {k: int(prior.get(k, 0)) for k in highwater}
        except (OSError, ValueError, TypeError):
            pass  # corrupt high-water -> treat everything as new
    new_rows = {k: rows[k] - highwater[k] for k in rows}
    age_sec = now - exported_ts
    data["rows"] = rows
    data["new_rows"] = new_rows
    data["export_age_sec"] = round(age_sec, 1)
    if any(v > 0 for v in new_rows.values()):
        reasons.append("new_rows")
    if age_sec > stale_sec:
        # Dead-engine alarm: the engine exports every bucket rollover, so a
        # stale stamp means dead or hung regardless of row deltas.
        reasons.append("stale_export")

wake = bool(reasons)
if wake:
    data["reason"] = reasons
    if stamp is not None:
        # Advance the baseline only when actually waking the verifier.
        highwater_path.write_text(json.dumps(rows, sort_keys=True) + "\n")

print(json.dumps({"wakeAgent": wake, "data": data}, sort_keys=True))
PY

status=$?
if [ "$status" -ne 0 ]; then
    # A broken pre-check must wake the verifier, never silence it.
    echo "{\"wakeAgent\": true, \"data\": {\"reason\": [\"precheck_error\"], \"exit_status\": $status}}"
fi
exit 0
