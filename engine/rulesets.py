"""Rule-sets: pure decision functions over (FeatureSnapshot, params).

Each ruleset maps one snapshot plus one variant's params to a Decision — no
I/O, no randomness, no clock reads. Everything a decision depends on arrives
in the snapshot, so identical inputs always yield identical Decisions
(replayability). Skip reasons are machine-readable ledger data: every refusal
carries the specific reason, never a generic one.

Boundary semantics (all inclusive unless stated otherwise):
- entry window: entry_window_sec_min <= seconds_to_close <= entry_window_sec_max
- impulse: abs(btc_move_usd) >= min_impulse_usd qualifies (== min enters)
- momentum favorite: best_ask >= favorite_min_price AND best_ask < 1.0
- fade underdog: 0 < best_ask <= underdog_max_price
- skew: skew_ratio >= min_notional_imbalance -> up; <= 1/min -> down
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.config import Variant
from engine.signals import FeatureSnapshot


@dataclass(frozen=True)
class Decision:
    """One ruleset's verdict for one (tick, market, variant) evaluation.

    limit_price is the chosen side's best ask at decision time; the executor
    applies its own fill model on top. reason is snake_case and ledgered.
    """

    action: str  # 'enter' | 'skip'
    side: str | None  # 'up' | 'down' | None
    limit_price: float | None
    reason: str


def _skip(reason: str) -> Decision:
    return Decision(action="skip", side=None, limit_price=None, reason=reason)


def _enter(side: str, ask: float, reason: str) -> Decision:
    return Decision(action="enter", side=side, limit_price=ask, reason=reason)


def _common_guards(snap: FeatureSnapshot, params: dict) -> Decision | None:
    """Defense-in-depth refusals shared by every ruleset (ahead of risk).

    Order matters for reason attribution: spot_stale also makes
    impulse_available False, so the specific stale-spot reason is checked
    first. Window bounds are inclusive at both edges.
    """
    if snap.quote_stale:
        return _skip("skip_stale_quote")
    if snap.spot_stale:
        return _skip("skip_stale_spot")
    if not snap.impulse_available:
        return _skip("skip_impulse_unavailable")
    window_min, window_max = params["entry_window_sec_min"], params["entry_window_sec_max"]
    if not window_min <= snap.seconds_to_close <= window_max:
        return _skip("skip_outside_window")
    return None


def _directional_move(snap: FeatureSnapshot, params: dict) -> float | Decision:
    """Signed BTC move if it clears min_impulse_usd, else a skip Decision.

    A zero move is always 'below min' — even with min_impulse_usd 0 there is
    no direction to act on.
    """
    move = snap.btc_move_usd
    assert move is not None  # guaranteed by _common_guards (impulse_available)
    if move == 0 or abs(move) < params["min_impulse_usd"]:
        return _skip("skip_impulse_below_min")
    return move


def momentum_follow(snap: FeatureSnapshot, params: dict) -> Decision:
    """Buy the favorite in the direction of the BTC impulse."""
    guard = _common_guards(snap, params)
    if guard is not None:
        return guard
    move = _directional_move(snap, params)
    if isinstance(move, Decision):
        return move
    if snap.up_best_ask is None or snap.down_best_ask is None:
        return _skip("skip_missing_price")

    minimum = params["favorite_min_price"]
    up_ok = minimum <= snap.up_best_ask < 1.0
    down_ok = minimum <= snap.down_best_ask < 1.0
    if up_ok and down_ok:
        # Pathological wide book: both sides price like favorites. Take the
        # stronger (higher-ask) side; a dead tie is unreadable -> skip.
        if snap.up_best_ask > snap.down_best_ask:
            return _enter("up", snap.up_best_ask, "entered_momentum_up")
        if snap.down_best_ask > snap.up_best_ask:
            return _enter("down", snap.down_best_ask, "entered_momentum_down")
        return _skip("skip_ambiguous_tie")

    side = "up" if move > 0 else "down"
    side_ok, ask = (up_ok, snap.up_best_ask) if side == "up" else (down_ok, snap.down_best_ask)
    if not side_ok:
        return _skip("skip_no_favorite")
    return _enter(side, ask, f"entered_momentum_{side}")


def contrarian_fade(snap: FeatureSnapshot, params: dict) -> Decision:
    """Buy the cheap underdog against the BTC impulse."""
    guard = _common_guards(snap, params)
    if guard is not None:
        return guard
    move = _directional_move(snap, params)
    if isinstance(move, Decision):
        return move

    side = "down" if move > 0 else "up"
    ask = snap.down_best_ask if side == "down" else snap.up_best_ask
    if ask is None:
        return _skip("skip_missing_price")
    if ask <= 0:
        return _skip("skip_invalid_price")
    if ask > params["underdog_max_price"]:
        return _skip("skip_underdog_too_expensive")
    return _enter(side, ask, f"entered_fade_{side}")


def skew_filter(snap: FeatureSnapshot, params: dict) -> Decision:
    """Follow resting-notional imbalance, but only when the move agrees."""
    guard = _common_guards(snap, params)
    if guard is not None:
        return guard
    move = _directional_move(snap, params)
    if isinstance(move, Decision):
        return move

    skew = snap.skew_ratio()
    if skew is None:
        return _skip("skip_skew_unavailable")
    minimum = params["min_notional_imbalance"]
    if skew >= minimum:
        side = "up"
    elif skew <= 1 / minimum:
        side = "down"
    else:
        return _skip("skip_no_skew")

    if (side == "up") != (move > 0):
        return _skip("skip_skew_move_disagree")
    ask = snap.up_best_ask if side == "up" else snap.down_best_ask
    if ask is None:
        return _skip("skip_missing_price")
    return _enter(side, ask, f"entered_skew_{side}")


RULESETS = {
    "momentum_follow": momentum_follow,
    "contrarian_fade": contrarian_fade,
    "skew_filter": skew_filter,
}


def evaluate(variant: Variant, snap: FeatureSnapshot) -> Decision:
    """Dispatch one variant's ruleset over one snapshot with its own params."""
    try:
        ruleset = RULESETS[variant.ruleset]
    except KeyError:
        raise ValueError(f"unknown ruleset '{variant.ruleset}'") from None
    return ruleset(snap, variant.params)
