"""Champion/challenger parameter evolution with anti-superstition guardrails (U9).

Per STRATEGY.md's Learning rules: every ~100 resolved trades a variant may
spawn challengers — ONE parameter changed, backed by a statistical case from
the ledger — which run in shadow and face the same funding gate. Promotion to
real money starts a 24-hour veto clock that begins only when the operator's
notice is confirmed delivered; an undelivered notice blocks promotion
(fail-closed — promotion moves real capital).

Anti-superstition rule: a proposal REQUIRES the bootstrap CI on the split's
mean-pnl difference to exclude zero — noise never generates challengers.
Rejected splits are ledgered as ``proposal_rejected`` variant events so the
absence of a challenger is as auditable as its presence. The proposal CI level
(99%) is deliberately stricter than the funding gate's 95%: a challenger costs
shadow capacity and operator attention, so the bar to *hypothesize* is higher
than the bar to *fund* an already-running hypothesis.

Ownership: this module OWNS writes to config/rulesets.yaml (variant blocks
only — everything else must survive a read-modify-write untouched) and to
``runtime/control/`` notice files. It NEVER touches STRATEGY.md: promotion
prose is written to ``runtime/control/pending-strategy-appends/<variant>.md``
and the engine's control-file processor (U10) performs the actual append.

Fixed candidate-dimension catalog (one dimension at a time, never combined):

- ``entry_time``: split resolved trades by entry ``seconds_to_close`` above or
  below the entry window's midpoint. A winning half narrows the window by a
  single param (``entry_window_sec_max`` if late wins, ``_min`` if early wins).
- ``impulse_size``: split by ``|btc_move_usd|`` above/below 1.5x the variant's
  ``min_impulse_usd``. Only "big impulses win" is expressible as a single
  tightening (raise ``min_impulse_usd`` to the threshold); the opposite result
  is logged as rejected — there is no max-impulse param to lower.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import yaml

from analytics.report import (
    FIXED_SEED,
    N_BOOTSTRAP,
    fundable_at_checkpoint,
    paired_comparison,
)
from engine.config import ConfigError, load_config
from engine.ledger import Ledger, PnlRow

REVIEW_INTERVAL_TRADES = 100  # re-review a variant only after this many NEW resolved trades
PROPOSAL_CI_LEVEL = 0.99  # stricter than the 95% funding gate — see module docstring
MIN_BUCKET_N = 20  # both halves of a split need at least this many resolved trades
MAX_CHALLENGERS_PER_PARENT = 2  # concurrently active (shadow/live/pending_promotion)
IMPULSE_SPLIT_FACTOR = 1.5
VETO_WINDOW_SEC = 24 * 3600.0
DEFAULT_ALLOCATION_USD = 100.0

STATE_FILE = "evolution-state.json"
CHALLENGER_ID_RE = re.compile(r"^(?P<parent>.+)-c(?P<n>\d+)$")
ACTIVE_STATUSES = ("shadow", "live", "pending_promotion")


class EvolutionError(RuntimeError):
    """Raised on invalid evolution operations (multi-param diff, cap, ...)."""


@dataclass(frozen=True)
class Proposal:
    """A single-parameter challenger candidate carrying its statistical case.

    ``params_diff`` maps exactly one param key to its proposed new value;
    ``detail`` is the case (dimension, effect size, CI, n per bucket).
    """

    parent_id: str
    dimension: str
    params_diff: dict = field(hash=False)
    detail: dict = field(hash=False)


def bootstrap_diff_ci(
    a: Iterable[float],
    b: Iterable[float],
    ci_level: float = PROPOSAL_CI_LEVEL,
    *,
    seed: int = FIXED_SEED,
    resamples: int = N_BOOTSTRAP,
) -> tuple[float | None, float | None]:
    """Percentile bootstrap CI on mean(a) - mean(b) for independent samples.

    Mirrors ``analytics.report.bootstrap_mean_ci``'s methodology (fresh seeded
    Generator per call, percentile interval) — that helper covers one sample's
    mean, this one covers a difference of two independent means, so it is
    implemented here rather than duplicated there. (None, None) if either
    sample is empty.
    """
    arr_a = np.asarray(list(a), dtype=float)
    arr_b = np.asarray(list(b), dtype=float)
    if arr_a.size == 0 or arr_b.size == 0:
        return None, None
    rng = np.random.default_rng(seed)
    idx_a = rng.integers(0, arr_a.size, size=(resamples, arr_a.size))
    idx_b = rng.integers(0, arr_b.size, size=(resamples, arr_b.size))
    diffs = arr_a[idx_a].mean(axis=1) - arr_b[idx_b].mean(axis=1)
    alpha = (1.0 - ci_level) / 2.0
    low, high = np.quantile(diffs, [alpha, 1.0 - alpha])
    return float(low), float(high)


def _as_number(x: float) -> float | int:
    """Config params use ints where whole (window seconds); keep them tidy."""
    return int(x) if float(x).is_integer() else float(x)


def _btc_move(features: dict) -> float | None:
    """Signed BTC move from an evaluations features_json dict.

    ``btc_move_usd`` is a FeatureSnapshot *property*, so persisted dicts carry
    btc_open/btc_last instead; accept either form.
    """
    if features.get("btc_move_usd") is not None:
        return features["btc_move_usd"]
    btc_open, btc_last = features.get("btc_open"), features.get("btc_last")
    if btc_open is None or btc_last is None:
        return None
    return btc_last - btc_open


@dataclass(frozen=True)
class _Split:
    """One catalog split: two halves plus the single-param expression of each
    half winning (None when a direction has no single-param expression)."""

    dimension: str
    a_label: str
    b_label: str
    a: list  # pnls
    b: list  # pnls
    param_if_a_wins: tuple[str, float | int] | None
    param_if_b_wins: tuple[str, float | int] | None


def _entry_time_split(params: dict, samples: list[tuple[float, dict]]) -> _Split | None:
    lo = params.get("entry_window_sec_min")
    hi = params.get("entry_window_sec_max")
    if lo is None or hi is None:
        return None
    midpoint = (lo + hi) / 2.0
    late, early = [], []
    for pnl, features in samples:
        stc = features.get("seconds_to_close")
        if stc is None:
            continue
        (late if stc < midpoint else early).append(pnl)
    return _Split(
        dimension="entry_time",
        a_label=f"late (seconds_to_close < {midpoint:g})",
        b_label=f"early (seconds_to_close >= {midpoint:g})",
        a=late,
        b=early,
        # Late wins -> only enter late: cap the window's far edge at the midpoint.
        param_if_a_wins=("entry_window_sec_max", _as_number(midpoint)),
        # Early wins -> only enter early: raise the window's near edge.
        param_if_b_wins=("entry_window_sec_min", _as_number(midpoint)),
    )


def _impulse_size_split(params: dict, samples: list[tuple[float, dict]]) -> _Split | None:
    min_impulse = params.get("min_impulse_usd")
    if min_impulse is None:
        return None
    threshold = IMPULSE_SPLIT_FACTOR * min_impulse
    big, small = [], []
    for pnl, features in samples:
        move = _btc_move(features)
        if move is None:
            continue
        (big if abs(move) >= threshold else small).append(pnl)
    return _Split(
        dimension="impulse_size",
        a_label=f"big (|btc_move_usd| >= {threshold:g})",
        b_label=f"small (|btc_move_usd| < {threshold:g})",
        a=big,
        b=small,
        param_if_a_wins=("min_impulse_usd", float(threshold)),
        # "Small impulses win" has no single-param expression (no max-impulse
        # param exists); logged as rejected rather than inventing a new knob.
        param_if_b_wins=None,
    )


_CATALOG = (_entry_time_split, _impulse_size_split)


class EvolutionManager:
    """Owns challenger proposal/spawn and the fail-closed promotion machine."""

    def __init__(
        self,
        config_path: Path | str,
        ledger: Ledger,
        runtime_dir: Path | str,
        clock=time.time,
    ):
        self.config_path = Path(config_path)
        self.ledger = ledger
        self.runtime_dir = Path(runtime_dir)
        self.clock = clock

    # -- paths ----------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self.runtime_dir / STATE_FILE

    @property
    def _notices_dir(self) -> Path:
        return self.runtime_dir / "control" / "veto-notices"

    @property
    def _vetoes_dir(self) -> Path:
        return self.runtime_dir / "control" / "vetoes"

    @property
    def _appends_dir(self) -> Path:
        return self.runtime_dir / "control" / "pending-strategy-appends"

    def _load_state(self) -> dict:
        if self._state_path.exists():
            return json.loads(self._state_path.read_text())
        return {"reviewed_n": {}}

    def _save_state(self, state: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    # -- ledger reads (read-only connection, mirroring analytics) --------------

    def _read_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.ledger.path}?mode=ro", uri=True)

    def _entry_features(self, variant_id: str) -> dict[int, dict]:
        """bucket_ts -> features_json of the entry evaluation (decision='enter',
        no risk rejection). One entry per bucket per variant by invariant."""
        conn = self._read_conn()
        try:
            cur = conn.execute(
                "SELECT bucket_ts, features_json FROM evaluations WHERE variant_id = ?"
                " AND decision = 'enter' AND skip_reason IS NULL ORDER BY id",
                (variant_id,),
            )
            out: dict[int, dict] = {}
            for bucket_ts, features_json in cur.fetchall():
                out.setdefault(bucket_ts, json.loads(features_json))
            return out
        finally:
            conn.close()

    def _creation_case(self, variant_id: str) -> dict | None:
        """Statistical case recorded with the challenger's 'created' event."""
        conn = self._read_conn()
        try:
            row = conn.execute(
                "SELECT detail FROM variants WHERE variant_id = ? AND event = 'created'"
                " ORDER BY id DESC LIMIT 1",
                (variant_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row[0] is None:
            return None
        return json.loads(row[0])

    def _resolved_rows(self, variant_id: str) -> list[PnlRow]:
        rows: list[PnlRow] = []
        for mode in ("shadow", "live"):
            rows.extend(self.ledger.realized_pnl_rows(variant_id, mode))
        rows.sort(key=lambda r: (r.ts, r.trade_id))
        return rows

    # -- config yaml read-modify-write -----------------------------------------

    def _read_raw_config(self) -> dict:
        return yaml.safe_load(self.config_path.read_text())

    def _write_raw_config(self, raw: dict) -> None:
        """Full-document rewrite; restores the original bytes if the result
        fails validation, so this module can never leave a broken config."""
        original = self.config_path.read_text()
        self.config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        try:
            load_config(self.config_path)
        except ConfigError:
            self.config_path.write_text(original)
            raise

    # -- proposal generation ----------------------------------------------------

    def propose_challengers(self, now: float) -> list[Proposal]:
        """Scan each active shadow/live variant for parameter-conditional splits.

        Runs at most once per REVIEW_INTERVAL_TRADES new resolved trades per
        variant (high-water mark in runtime/evolution-state.json). Splits whose
        CI does not exclude zero are ledgered as ``proposal_rejected`` — noise
        never generates challengers.
        """
        config = load_config(self.config_path)
        state = self._load_state()
        reviewed_n: dict = state.setdefault("reviewed_n", {})
        proposals: list[Proposal] = []
        for variant in config.variants:
            if variant.status not in ("shadow", "live"):
                continue
            rows = self._resolved_rows(variant.id)
            if len(rows) - reviewed_n.get(variant.id, 0) < REVIEW_INTERVAL_TRADES:
                continue
            reviewed_n[variant.id] = len(rows)
            features = self._entry_features(variant.id)
            samples = [
                (row.pnl, features[row.bucket_ts]) for row in rows if row.bucket_ts in features
            ]
            for make_split in _CATALOG:
                split = make_split(variant.params, samples)
                if split is None:
                    continue  # dimension inapplicable to this variant's params
                proposal = self._evaluate_split(variant.id, variant.params, split, now)
                if proposal is not None:
                    proposals.append(proposal)
        self._save_state(state)
        return proposals

    def _evaluate_split(
        self, parent_id: str, parent_params: dict, split: _Split, now: float
    ) -> Proposal | None:
        n_a, n_b = len(split.a), len(split.b)
        stats: dict = {
            "dimension": split.dimension,
            "a_label": split.a_label,
            "b_label": split.b_label,
            "n_a": n_a,
            "n_b": n_b,
            "mean_a": sum(split.a) / n_a if n_a else None,
            "mean_b": sum(split.b) / n_b if n_b else None,
            "ci_level": PROPOSAL_CI_LEVEL,
        }
        if n_a < MIN_BUCKET_N or n_b < MIN_BUCKET_N:
            return self._reject(parent_id, parent_params, now, stats, "insufficient_n")
        ci_low, ci_high = bootstrap_diff_ci(split.a, split.b)
        stats.update(mean_diff=stats["mean_a"] - stats["mean_b"], ci_low=ci_low, ci_high=ci_high)
        if ci_low is not None and ci_low > 0:
            winner = split.param_if_a_wins
        elif ci_high is not None and ci_high < 0:
            winner = split.param_if_b_wins
        else:
            # THE anti-superstition rule: the CI on the difference must exclude
            # zero or no challenger exists, only a ledgered rejection.
            return self._reject(parent_id, parent_params, now, stats, "ci_includes_zero")
        if winner is None:
            return self._reject(parent_id, parent_params, now, stats, "no_single_param_expression")
        key, value = winner
        return Proposal(
            parent_id=parent_id,
            dimension=split.dimension,
            params_diff={key: value},
            detail=stats,
        )

    def _reject(
        self, parent_id: str, parent_params: dict, now: float, stats: dict, reason: str
    ) -> None:
        self.ledger.record_variant_event(
            ts=now,
            variant_id=parent_id,
            event="proposal_rejected",
            params=parent_params,
            detail=json.dumps({"reason": reason, **stats}, sort_keys=True),
        )
        return None

    # -- spawning -----------------------------------------------------------------

    def _active_challengers(self, raw: dict, parent_id: str) -> list[dict]:
        pattern = re.compile(rf"^{re.escape(parent_id)}-c\d+$")
        return [
            v for v in raw["variants"] if pattern.match(v["id"]) and v["status"] in ACTIVE_STATUSES
        ]

    def _next_challenger_id(self, raw: dict, parent_id: str) -> str:
        pattern = re.compile(rf"^{re.escape(parent_id)}-c(\d+)$")
        taken = [int(m.group(1)) for v in raw["variants"] if (m := pattern.match(v["id"]))]
        return f"{parent_id}-c{max(taken, default=0) + 1}"

    def spawn_challenger(self, proposal: Proposal, now: float | None = None) -> str:
        """Append a shadow challenger (parent params + EXACTLY one diff) to the
        config yaml and ledger its creation with full lineage. Returns the id."""
        now = self.clock() if now is None else now
        if len(proposal.params_diff) != 1:
            raise EvolutionError(
                f"challenger must differ from parent in exactly one param, "
                f"got {len(proposal.params_diff)} diffs: {sorted(proposal.params_diff)}"
            )
        raw = self._read_raw_config()
        parent = next((v for v in raw["variants"] if v["id"] == proposal.parent_id), None)
        if parent is None:
            raise EvolutionError(f"parent variant '{proposal.parent_id}' not in config")
        if len(self._active_challengers(raw, proposal.parent_id)) >= MAX_CHALLENGERS_PER_PARENT:
            raise EvolutionError(
                f"parent '{proposal.parent_id}' already has "
                f"{MAX_CHALLENGERS_PER_PARENT} active challengers"
            )
        new_params = dict(parent["params"])
        new_params.update(proposal.params_diff)
        changed = [
            k
            for k in set(new_params) | set(parent["params"])
            if new_params.get(k) != parent["params"].get(k)
        ]
        if len(changed) != 1:
            raise EvolutionError(
                f"challenger params must differ from parent in exactly one key, "
                f"differ in {sorted(changed)}"
            )

        child_id = self._next_challenger_id(raw, proposal.parent_id)
        raw["variants"].append(
            {
                "id": child_id,
                "ruleset": parent["ruleset"],
                "status": "shadow",
                "allocation_usd": 0.0,
                "params": new_params,
            }
        )
        self._write_raw_config(raw)
        self.ledger.record_variant_event(
            ts=now,
            variant_id=child_id,
            event="created",
            params=new_params,
            parent_variant_id=proposal.parent_id,
            detail=json.dumps(
                {
                    "params_diff": proposal.params_diff,
                    "dimension": proposal.dimension,
                    "statistical_case": proposal.detail,
                },
                sort_keys=True,
            ),
        )
        return child_id

    def propose_and_spawn(self, now: float) -> list[str]:
        """Propose, then spawn every proposal that clears the structural checks.

        A cap-violating (or otherwise unspawnable) proposal is dropped and
        ledgered as a risk event — its statistical case is not lost, just not
        acted on this cycle.
        """
        spawned = []
        for proposal in self.propose_challengers(now):
            try:
                spawned.append(self.spawn_challenger(proposal, now))
            except EvolutionError as exc:
                self.ledger.record_risk_event(now, "evolution_spawn_skipped", str(exc))
        return spawned

    # -- promotion state machine (fail-closed) --------------------------------------

    def review_promotions(self, now: float) -> None:
        """Advance every challenger through the promotion machine.

        shadow --gate--> pending_promotion (+ veto notice, ack null)
        pending + ack --> veto deadline stamped (ack + 24h)
        pending + veto file --> retired ('vetoed')
        pending + no ack --> stays pending forever (escalation is the
            verifier's job — nothing more is written here)
        pending + ack + deadline passed + no veto --> live (+ prose file),
            then paired comparison retires the champion iff the challenger
            wins the overlapping window.
        """
        config = load_config(self.config_path)
        raw = self._read_raw_config()
        changed = False
        for variant in raw["variants"]:
            match = CHALLENGER_ID_RE.match(variant["id"])
            if match is None:
                continue
            parent_id = match.group("parent")
            if variant["status"] == "shadow":
                changed |= self._maybe_flag_pending(raw, variant, parent_id, config, now)
            elif variant["status"] == "pending_promotion":
                changed |= self._advance_pending(raw, variant, parent_id, config, now)
        if changed:
            self._write_raw_config(raw)

    def _params_diff(self, raw: dict, parent_id: str, variant: dict) -> dict:
        parent = next((v for v in raw["variants"] if v["id"] == parent_id), None)
        if parent is None:
            return {}
        return {
            k: {"old": parent["params"].get(k), "new": variant["params"].get(k)}
            for k in set(variant["params"]) | set(parent["params"])
            if variant["params"].get(k) != parent["params"].get(k)
        }

    def _maybe_flag_pending(
        self, raw: dict, variant: dict, parent_id: str, config, now: float
    ) -> bool:
        fundable, checkpoint_n = fundable_at_checkpoint(
            self.ledger, variant["id"], config.gate, config
        )
        if not fundable:
            return False
        notice = {
            "variant": variant["id"],
            "parent": parent_id,
            "params_diff": self._params_diff(raw, parent_id, variant),
            "statistical_case": self._creation_case(variant["id"]),
            "checkpoint_n": checkpoint_n,
            "proposed_allocation_usd": DEFAULT_ALLOCATION_USD,
            "created_ts": now,
            "delivery_ack_ts": None,
            "veto_deadline_ts": None,
        }
        self._notices_dir.mkdir(parents=True, exist_ok=True)
        (self._notices_dir / f"{variant['id']}.json").write_text(
            json.dumps(notice, indent=2, sort_keys=True) + "\n"
        )
        self.ledger.record_variant_event(
            ts=now,
            variant_id=variant["id"],
            event="pending_promotion",
            params=variant["params"],
            parent_variant_id=parent_id,
            detail=json.dumps({"checkpoint_n": checkpoint_n}),
        )
        variant["status"] = "pending_promotion"
        return True

    def _advance_pending(
        self, raw: dict, variant: dict, parent_id: str, config, now: float
    ) -> bool:
        variant_id = variant["id"]
        # A veto cancels the promotion at any point in the window.
        if (self._vetoes_dir / variant_id).exists():
            variant["status"] = "retired"
            variant["allocation_usd"] = 0.0
            self.ledger.record_variant_event(
                ts=now,
                variant_id=variant_id,
                event="vetoed",
                params=variant["params"],
                parent_variant_id=parent_id,
                detail="operator veto file present",
            )
            return True

        notice_path = self._notices_dir / f"{variant_id}.json"
        if not notice_path.exists():
            # Fail-closed: no notice, no promotion path. The verifier surfaces it.
            return False
        notice = json.loads(notice_path.read_text())

        if notice.get("delivery_ack_ts") is None:
            # Fail-closed: the veto clock never starts on an undelivered notice.
            return False
        if notice.get("veto_deadline_ts") is None:
            notice["veto_deadline_ts"] = notice["delivery_ack_ts"] + VETO_WINDOW_SEC
            notice_path.write_text(json.dumps(notice, indent=2, sort_keys=True) + "\n")
        if now < notice["veto_deadline_ts"]:
            return False

        # Ack + deadline passed + no veto -> promote. Status and allocation flip;
        # params NEVER change here (frozen-while-live starts now).
        variant["status"] = "live"
        variant["allocation_usd"] = float(
            notice.get("proposed_allocation_usd") or DEFAULT_ALLOCATION_USD
        )
        self.ledger.record_variant_event(
            ts=now,
            variant_id=variant_id,
            event="live",
            params=variant["params"],
            parent_variant_id=parent_id,
            detail=json.dumps(
                {
                    "allocation_usd": variant["allocation_usd"],
                    "veto_deadline_ts": notice["veto_deadline_ts"],
                }
            ),
        )
        self._write_prose(variant_id, parent_id, notice, now)
        self._maybe_retire_champion(raw, parent_id, variant_id, config, now)
        return True

    def _maybe_retire_champion(
        self, raw: dict, parent_id: str, challenger_id: str, config, now: float
    ) -> None:
        """Retire the champion iff the challenger wins the PAIRED comparison on
        the overlapping window — never a full-history comparison (which rewards
        whichever variant sampled the friendlier regime)."""
        result = paired_comparison(
            self.ledger, parent_id, challenger_id, ci_level=config.gate.ci_level
        )
        if not result.challenger_wins:
            return
        parent = next((v for v in raw["variants"] if v["id"] == parent_id), None)
        if parent is None or parent["status"] == "retired":
            return
        parent["status"] = "retired"
        parent["allocation_usd"] = 0.0
        self.ledger.record_variant_event(
            ts=now,
            variant_id=parent_id,
            event="retired",
            params=parent["params"],
            detail=json.dumps(
                {
                    "beaten_by": challenger_id,
                    "n_overlap": result.n_overlap,
                    "mean_diff": result.mean_diff,
                    "ci_low": result.ci_low,
                    "ci_high": result.ci_high,
                },
                sort_keys=True,
            ),
        )

    # -- promotion prose (control file; U10's processor appends to STRATEGY.md) -----

    def _write_prose(self, variant_id: str, parent_id: str, notice: dict, now: float) -> None:
        date = datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d")
        diff_lines = [
            f"- `{key}`: {change['old']} -> {change['new']}"
            for key, change in sorted(notice.get("params_diff", {}).items())
        ] or ["- (params diff unavailable; see the variant's 'created' ledger event)"]
        case = notice.get("statistical_case") or {}
        stats = case.get("statistical_case") or {}
        if stats.get("mean_diff") is not None:
            case_line = (
                f"On the {case.get('dimension', 'unknown')} split, the winning half "
                f"outperformed by ${stats['mean_diff']:.2f} per trade "
                f"({stats.get('ci_level', PROPOSAL_CI_LEVEL):.0%} CI "
                f"[{stats['ci_low']:.2f}, {stats['ci_high']:.2f}], "
                f"n={stats.get('n_a')} vs n={stats.get('n_b')})."
            )
        else:
            case_line = (
                "Statistical case recorded with the variant's 'created' event in the ledger."
            )
        text = "\n".join(
            [
                "<!-- TODO(U10): the engine's control-file processor appends this block to",
                "     STRATEGY.md's Lessons Learned appendix. Agents never write STRATEGY.md",
                "     directly. -->",
                "",
                f"### Variant {variant_id} (promoted to live {date})",
                "",
                f"Lineage: challenger of `{parent_id}`; inherits every parameter of "
                f"`{parent_id}` except the single change below.",
                "",
                "Parameter changed:",
                "",
                *diff_lines,
                "",
                f"Statistical case: {case_line}",
                "",
                f"Promotion: passed the funding gate at checkpoint "
                f"n={notice.get('checkpoint_n')}; the 24-hour veto window elapsed after "
                f"delivery acknowledgment with no operator veto. Initial allocation "
                f"${float(notice.get('proposed_allocation_usd') or DEFAULT_ALLOCATION_USD):.2f}.",
                "",
            ]
        )
        self._appends_dir.mkdir(parents=True, exist_ok=True)
        (self._appends_dir / f"{variant_id}.md").write_text(text)
