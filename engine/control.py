"""Host-side control-file processor (U10) — the ONLY code that writes STRATEGY.md.

Agent containers mount this repo read-only and ``runtime/`` read-write, so
agents *request* rulebook changes and allocation moves by dropping files into
``runtime/control/``; this processor (running inside the host engine daemon)
validates and applies them. Three request kinds:

- ``runtime/control/approved-lessons/*.md`` — operator-approved lesson text,
  appended verbatim to STRATEGY.md's "Lessons Learned" appendix.
- ``runtime/control/pending-strategy-appends/*.md`` — evolution's promotion
  prose (see engine/evolution.py ``_write_prose``), same append flow.
- ``runtime/control/allocation-requests/*.json`` — ``{"variant_id": ...,
  "allocation_usd": ...}`` with ABSOLUTE set semantics: the request states the
  variant's new total allocation, it is never added to the current value.
  Setting is naturally idempotent, so a crash between applying the config edit
  and archiving the request file cannot double-apply — the replayed request
  just sets the same value again.

Processed files move to ``runtime/control/processed/``; invalid or malformed
files move to ``runtime/control/rejected/`` with a ``control_rejected`` risk
event — agent-written garbage must never crash the engine. Host-side faults
(STRATEGY.md unreadable/markerless, config that fails validation) are NOT the
request's fault: the file stays in place for a later cycle and a
``control_error`` risk event records the problem.

CRITICAL invariant: appends land only below the ``<!-- rules:end -->`` marker;
the rules section is never modified, so ``rules_hash(STRATEGY.md)`` is
byte-identical before and after every append. Lesson content containing either
rules marker is rejected outright (a lesson must never inject rule markers),
and the append helper verifies the hash after writing, restoring the original
file if it ever changed (mirroring evolution.py's restore-on-invalid pattern).
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from engine.config import ConfigError, load_config, rules_hash
from engine.ledger import Ledger

APPROVED_LESSONS_DIR = "approved-lessons"
PENDING_APPENDS_DIR = "pending-strategy-appends"
ALLOCATION_REQUESTS_DIR = "allocation-requests"
PROCESSED_DIR = "processed"
REJECTED_DIR = "rejected"

MAX_APPEND_CHARS = 4000
RULES_MARKERS = ("<!-- rules:begin -->", "<!-- rules:end -->")
LESSONS_HEADING = "## Lessons Learned"
FUNDABLE_STATUSES = ("live", "pending_promotion")


class ControlError(RuntimeError):
    """Host-side control-processing fault (not agent-written garbage)."""


def _unique_dest(directory: Path, name: str) -> Path:
    """Archive destination that never clobbers an earlier archived file."""
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / name
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while dest.exists():
        dest = directory / f"{stem}-{n}{suffix}"
        n += 1
    return dest


def _archive(path: Path, directory: Path) -> None:
    path.rename(_unique_dest(directory, path.name))


def _validate_append(content: str) -> str | None:
    """Reason the content is unappendable, or None if it is fine."""
    if not content.strip():
        return "empty"
    if len(content) >= MAX_APPEND_CHARS:
        return f"too_long ({len(content)} chars, limit {MAX_APPEND_CHARS})"
    for marker in RULES_MARKERS:
        if marker in content:
            return "contains_rules_marker"
    return None


def _strip_processor_todo(content: str) -> str:
    """Drop evolution's leading TODO(U10) HTML comment — it addressed this
    processor, not the rulebook's readers."""
    stripped = content.lstrip()
    if stripped.startswith("<!-- TODO(U10)"):
        end = stripped.find("-->")
        if end != -1:
            return stripped[end + len("-->") :].lstrip("\n")
    return content


def _append_to_strategy(strategy_md_path: Path, content: str, source: str, now: float) -> bool:
    """Append one dated block under "Lessons Learned"; returns False when the
    identical block is already present (crash-before-archive replay dedup).

    Precondition: content passed ``_validate_append`` (no rules markers), so
    the append physically cannot alter the rules section. The rules_hash is
    still verified after the write, restoring the original bytes on any
    mismatch — this processor can never leave a rules-drifted STRATEGY.md.
    """
    try:
        original = strategy_md_path.read_text()
    except OSError as exc:
        raise ControlError(f"cannot read {strategy_md_path}: {exc}") from exc
    hash_before = rules_hash(original)  # ConfigError (host fault) if markers missing
    if LESSONS_HEADING not in original.split(RULES_MARKERS[1], 1)[1]:
        raise ControlError(f"STRATEGY.md has no '{LESSONS_HEADING}' heading below rules:end")

    date = datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d")
    body = _strip_processor_todo(content).strip()
    block = f"### {date} ({source})\n\n{body}\n"
    if block in original.split(RULES_MARKERS[1], 1)[1]:
        return False

    strategy_md_path.write_text(original.rstrip("\n") + "\n\n" + block)
    if rules_hash(strategy_md_path.read_text()) != hash_before:  # pragma: no cover — defensive
        strategy_md_path.write_text(original)
        raise ControlError("append changed the rules hash; STRATEGY.md restored")
    return True


def _reject(path: Path, control_dir: Path, ledger: Ledger, now: float, reason: str) -> str:
    _archive(path, control_dir / REJECTED_DIR)
    ledger.record_risk_event(
        now, "control_rejected", json.dumps({"file": path.name, "reason": reason})
    )
    return f"rejected {path.name}: {reason}"


def _process_appends(
    source_dir: Path,
    kind: str,
    strategy_md_path: Path,
    control_dir: Path,
    ledger: Ledger,
    now: float,
) -> list[str]:
    actions: list[str] = []
    if not source_dir.is_dir():
        return actions
    for path in sorted(source_dir.glob("*.md")):
        try:
            content = path.read_text()
        except OSError as exc:
            actions.append(_reject(path, control_dir, ledger, now, f"unreadable: {exc}"))
            continue
        reason = _validate_append(content)
        if reason is not None:
            actions.append(_reject(path, control_dir, ledger, now, reason))
            continue
        try:
            appended = _append_to_strategy(strategy_md_path, content, path.name, now)
        except (ControlError, ConfigError) as exc:
            # Host-side fault: leave the request in place for a later cycle.
            ledger.record_risk_event(now, "control_error", f"{path.name}: {exc}")
            actions.append(f"deferred {path.name}: {exc}")
            continue
        _archive(path, control_dir / PROCESSED_DIR)
        ledger.record_risk_event(now, kind, path.name)
        actions.append(f"{kind}: {path.name}" + ("" if appended else " (already appended)"))
    return actions


def _validate_allocation_request(request: object, config) -> tuple[str, float] | str:
    """(variant_id, allocation) on success, else the rejection reason."""
    if not isinstance(request, dict):
        return "not_a_json_object"
    variant_id = request.get("variant_id")
    allocation = request.get("allocation_usd")
    if not isinstance(variant_id, str) or not variant_id:
        return "missing_or_invalid_variant_id"
    if isinstance(allocation, bool) or not isinstance(allocation, int | float):
        return "missing_or_invalid_allocation_usd"
    variant = next((v for v in config.variants if v.id == variant_id), None)
    if variant is None:
        return f"unknown_variant '{variant_id}'"
    if variant.status not in FUNDABLE_STATUSES:
        return f"variant_status_'{variant.status}'_not_fundable"
    if not 0 <= allocation <= config.bankroll_usd:
        return f"allocation_out_of_range ({allocation} not in [0, bankroll {config.bankroll_usd}])"
    return variant_id, float(allocation)


def _write_raw_config(config_path: Path, raw: dict) -> None:
    """Full-document rewrite with restore-on-invalid (evolution.py's pattern):
    this module can never leave a broken config behind."""
    original = config_path.read_text()
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    try:
        load_config(config_path)
    except ConfigError:
        config_path.write_text(original)
        raise


def _process_allocations(
    source_dir: Path, config_path: Path, control_dir: Path, ledger: Ledger, now: float
) -> list[str]:
    actions: list[str] = []
    if not source_dir.is_dir():
        return actions
    for path in sorted(source_dir.glob("*.json")):
        try:
            request = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            actions.append(_reject(path, control_dir, ledger, now, f"malformed_json: {exc}"))
            continue
        try:
            config = load_config(config_path)
        except ConfigError as exc:  # current config broken — host fault, defer
            ledger.record_risk_event(now, "control_error", f"{path.name}: {exc}")
            actions.append(f"deferred {path.name}: {exc}")
            continue
        result = _validate_allocation_request(request, config)
        if isinstance(result, str):
            actions.append(_reject(path, control_dir, ledger, now, result))
            continue
        variant_id, allocation = result
        raw = yaml.safe_load(config_path.read_text())
        for variant in raw["variants"]:
            if variant["id"] == variant_id:
                variant["allocation_usd"] = allocation
        try:
            _write_raw_config(config_path, raw)
        except ConfigError as exc:  # pragma: no cover — pre-validated; defensive
            ledger.record_risk_event(now, "control_error", f"{path.name}: {exc}")
            actions.append(f"deferred {path.name}: {exc}")
            continue
        # Apply-then-archive: a crash here replays the request next cycle, and
        # absolute set semantics make the replay a no-op (same value set twice).
        _archive(path, control_dir / PROCESSED_DIR)
        ledger.record_risk_event(
            now,
            "allocation_applied",
            json.dumps(
                {"file": path.name, "variant_id": variant_id, "allocation_usd": allocation},
                sort_keys=True,
            ),
        )
        actions.append(f"allocation_applied: {variant_id} -> ${allocation:.2f} ({path.name})")
    return actions


def process_control_files(
    strategy_md_path: Path | str,
    config_path: Path | str,
    runtime_dir: Path | str,
    ledger: Ledger,
    now: float,
) -> list[str]:
    """One pass over ``runtime/control/``; returns human-readable actions taken.

    Cheap when idle (three directory scans), so the engine calls it on every
    bucket rollover. Every applied or rejected request lands in the ledger as
    a risk event — control-plane activity is as auditable as trading activity.
    """
    strategy_md_path = Path(strategy_md_path)
    config_path = Path(config_path)
    control_dir = Path(runtime_dir) / "control"
    actions: list[str] = []
    actions += _process_appends(
        control_dir / APPROVED_LESSONS_DIR,
        "lesson_appended",
        strategy_md_path,
        control_dir,
        ledger,
        now,
    )
    actions += _process_appends(
        control_dir / PENDING_APPENDS_DIR,
        "promotion_prose_appended",
        strategy_md_path,
        control_dir,
        ledger,
        now,
    )
    actions += _process_allocations(
        control_dir / ALLOCATION_REQUESTS_DIR, config_path, control_dir, ledger, now
    )
    return actions


class ControlProcessor:
    """Thin wiring wrapper so main.py's rollover hook mirrors the evolution
    pattern (optional injected collaborator with a single entry point)."""

    def __init__(
        self,
        strategy_md_path: Path | str,
        config_path: Path | str,
        runtime_dir: Path | str,
        ledger: Ledger,
        clock=time.time,
    ):
        self.strategy_md_path = Path(strategy_md_path)
        self.config_path = Path(config_path)
        self.runtime_dir = Path(runtime_dir)
        self.ledger = ledger
        self.clock = clock

    def process(self, now: float | None = None) -> list[str]:
        return process_control_files(
            self.strategy_md_path,
            self.config_path,
            self.runtime_dir,
            self.ledger,
            self.clock() if now is None else now,
        )
