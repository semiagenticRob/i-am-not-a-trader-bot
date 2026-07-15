"""U5: rule-sets — three pure decision functions plus registry.

Every test builds FeatureSnapshot fixtures by hand via `snap()`. Boundary
semantics under test (all inclusive unless stated):
- entry window: entry_window_sec_min <= seconds_to_close <= entry_window_sec_max
- impulse: abs(btc_move_usd) >= min_impulse_usd enters (== min enters)
- momentum favorite: best_ask >= favorite_min_price AND best_ask < 1.0
- fade underdog: 0 < best_ask <= underdog_max_price
- skew: skew_ratio >= min_notional_imbalance -> up; <= 1/min -> down (both inclusive)
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from engine import rulesets
from engine.config import VALID_RULESETS, Variant
from engine.rulesets import (
    RULESETS,
    Decision,
    contrarian_fade,
    evaluate,
    momentum_follow,
    skew_filter,
)
from engine.signals import FeatureSnapshot

WINDOW = {"entry_window_sec_min": 60, "entry_window_sec_max": 150}
MOM_PARAMS = {**WINDOW, "min_impulse_usd": 70.0, "favorite_min_price": 0.70}
FADE_PARAMS = {**WINDOW, "min_impulse_usd": 70.0, "underdog_max_price": 0.30}
SKEW_PARAMS = {**WINDOW, "min_impulse_usd": 70.0, "min_notional_imbalance": 2.0}

ALL = [
    (momentum_follow, MOM_PARAMS),
    (contrarian_fade, FADE_PARAMS),
    (skew_filter, SKEW_PARAMS),
]


def snap(**overrides) -> FeatureSnapshot:
    """Canonical snapshot: BTC +100 move, UP the favorite, skew 2.5 toward UP.

    With default params all three rulesets enter on this snapshot
    (momentum: up @ 0.74; fade: down @ 0.26; skew: up @ 0.74).
    """
    defaults = dict(
        bucket_ts=1_752_500_000,
        market_slug="btc-updown-5m-test",
        seconds_to_close=100.0,
        btc_open=118_000.0,
        btc_last=118_100.0,
        up_best_bid=0.72,
        up_best_ask=0.74,
        down_best_bid=0.24,
        down_best_ask=0.26,
        up_bid_depth_usd=500.0,
        up_ask_depth_usd=500.0,
        down_bid_depth_usd=200.0,
        down_ask_depth_usd=200.0,
        fee_rate=0.0,
        fees_enabled=False,
        quote_stale=False,
        spot_stale=False,
    )
    defaults.update(overrides)
    return FeatureSnapshot(**defaults)


def down_move_snap(**overrides) -> FeatureSnapshot:
    """Mirror snapshot: BTC -100 move, DOWN the favorite, skew 2.5 toward DOWN."""
    defaults = dict(
        btc_last=117_900.0,
        up_best_bid=0.24,
        up_best_ask=0.26,
        down_best_bid=0.72,
        down_best_ask=0.74,
        up_bid_depth_usd=200.0,
        down_bid_depth_usd=500.0,
    )
    defaults.update(overrides)
    return snap(**defaults)


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------


def test_decision_is_frozen():
    decision = Decision(action="skip", side=None, limit_price=None, reason="skip_stale_quote")
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.action = "enter"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Defense-in-depth guards — identical across all three rulesets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("ruleset", "params"), ALL)
def test_stale_quote_skips(ruleset, params):
    decision = ruleset(snap(quote_stale=True), params)
    assert decision == Decision("skip", None, None, "skip_stale_quote")


@pytest.mark.parametrize(("ruleset", "params"), ALL)
def test_stale_spot_skips(ruleset, params):
    # spot_stale also makes impulse_available False; the specific stale-spot
    # reason must win over the generic impulse-unavailable one.
    decision = ruleset(snap(spot_stale=True), params)
    assert decision.reason == "skip_stale_spot"
    assert decision.action == "skip"


@pytest.mark.parametrize(("ruleset", "params"), ALL)
@pytest.mark.parametrize("missing", ["btc_open", "btc_last"])
def test_impulse_unavailable_skips(ruleset, params, missing):
    decision = ruleset(snap(**{missing: None}), params)
    assert decision.reason == "skip_impulse_unavailable"
    assert decision.action == "skip"


@pytest.mark.parametrize(("ruleset", "params"), ALL)
@pytest.mark.parametrize("seconds", [59.999, 150.001, 0.0, 300.0])
def test_outside_window_skips(ruleset, params, seconds):
    decision = ruleset(snap(seconds_to_close=seconds), params)
    assert decision.reason == "skip_outside_window"


@pytest.mark.parametrize(("ruleset", "params"), ALL)
@pytest.mark.parametrize("seconds", [60.0, 150.0])
def test_window_bounds_are_inclusive(ruleset, params, seconds):
    # Exactly at either window edge the guard must pass (canonical snap enters).
    decision = ruleset(snap(seconds_to_close=seconds), params)
    assert decision.action == "enter"


@pytest.mark.parametrize(("ruleset", "params"), ALL)
def test_impulse_exactly_at_min_enters(ruleset, params):
    decision = ruleset(snap(btc_last=118_070.0), params)  # move == 70.0 == min
    assert decision.action == "enter"


@pytest.mark.parametrize(("ruleset", "params"), ALL)
def test_impulse_below_min_skips(ruleset, params):
    decision = ruleset(snap(btc_last=118_069.99), params)  # move == 69.99
    assert decision == Decision("skip", None, None, "skip_impulse_below_min")


# ---------------------------------------------------------------------------
# momentum_follow
# ---------------------------------------------------------------------------


def test_momentum_canonical_up_entry():
    decision = momentum_follow(snap(), MOM_PARAMS)
    assert decision == Decision("enter", "up", 0.74, "entered_momentum_up")


def test_momentum_canonical_down_entry():
    decision = momentum_follow(down_move_snap(), MOM_PARAMS)
    assert decision == Decision("enter", "down", 0.74, "entered_momentum_down")


def test_momentum_favorite_exactly_at_min_price_enters():
    decision = momentum_follow(snap(up_best_ask=0.70), MOM_PARAMS)
    assert decision == Decision("enter", "up", 0.70, "entered_momentum_up")


def test_momentum_favorite_below_min_price_skips():
    decision = momentum_follow(snap(up_best_ask=0.699), MOM_PARAMS)
    assert decision == Decision("skip", None, None, "skip_no_favorite")


def test_momentum_favorite_at_one_dollar_skips():
    # ask must be strictly < 1.0 — a $1 favorite has zero payoff.
    decision = momentum_follow(snap(up_best_ask=1.0), MOM_PARAMS)
    assert decision == Decision("skip", None, None, "skip_no_favorite")


@pytest.mark.parametrize("missing", ["up_best_ask", "down_best_ask"])
def test_momentum_missing_ask_skips(missing):
    decision = momentum_follow(snap(**{missing: None}), MOM_PARAMS)
    assert decision == Decision("skip", None, None, "skip_missing_price")


def test_momentum_both_qualify_chooses_higher_ask_over_move_direction():
    # Pathological wide book: both asks >= 0.70; DOWN is stronger despite an
    # upward move — the stronger side wins.
    decision = momentum_follow(snap(up_best_ask=0.72, down_best_ask=0.75), MOM_PARAMS)
    assert decision == Decision("enter", "down", 0.75, "entered_momentum_down")


def test_momentum_both_qualify_up_stronger():
    decision = momentum_follow(snap(up_best_ask=0.76, down_best_ask=0.71), MOM_PARAMS)
    assert decision == Decision("enter", "up", 0.76, "entered_momentum_up")


def test_momentum_only_anti_directional_side_qualifies_skips():
    # Move is up but only DOWN prices like a favorite: never enter against the
    # move unless both sides qualify (the pathological both-favorites case).
    decision = momentum_follow(snap(up_best_ask=0.40, down_best_ask=0.80), MOM_PARAMS)
    assert decision == Decision("skip", None, None, "skip_no_favorite")


def test_momentum_both_qualify_tie_skips():
    decision = momentum_follow(snap(up_best_ask=0.72, down_best_ask=0.72), MOM_PARAMS)
    assert decision == Decision("skip", None, None, "skip_ambiguous_tie")


def test_momentum_zero_move_has_no_direction_even_with_zero_min_impulse():
    params = {**MOM_PARAMS, "min_impulse_usd": 0.0}
    decision = momentum_follow(snap(btc_last=118_000.0), params)
    assert decision == Decision("skip", None, None, "skip_impulse_below_min")


# ---------------------------------------------------------------------------
# contrarian_fade
# ---------------------------------------------------------------------------


def test_fade_canonical_entry_fades_up_move():
    decision = contrarian_fade(snap(), FADE_PARAMS)
    assert decision == Decision("enter", "down", 0.26, "entered_fade_down")


def test_fade_canonical_entry_fades_down_move():
    decision = contrarian_fade(down_move_snap(), FADE_PARAMS)
    assert decision == Decision("enter", "up", 0.26, "entered_fade_up")


def test_fade_underdog_exactly_at_max_price_enters():
    decision = contrarian_fade(snap(down_best_ask=0.30), FADE_PARAMS)
    assert decision == Decision("enter", "down", 0.30, "entered_fade_down")


def test_fade_underdog_above_max_price_skips():
    decision = contrarian_fade(snap(down_best_ask=0.301), FADE_PARAMS)
    assert decision == Decision("skip", None, None, "skip_underdog_too_expensive")


def test_fade_underdog_zero_ask_skips():
    decision = contrarian_fade(snap(down_best_ask=0.0), FADE_PARAMS)
    assert decision == Decision("skip", None, None, "skip_invalid_price")


def test_fade_underdog_missing_ask_skips():
    decision = contrarian_fade(snap(down_best_ask=None), FADE_PARAMS)
    assert decision == Decision("skip", None, None, "skip_missing_price")


def test_fade_ignores_favorite_side_price():
    # Only the underdog's ask matters; a missing favorite-side ask is fine.
    decision = contrarian_fade(snap(up_best_ask=None), FADE_PARAMS)
    assert decision == Decision("enter", "down", 0.26, "entered_fade_down")


# ---------------------------------------------------------------------------
# skew_filter
# ---------------------------------------------------------------------------


def test_skew_canonical_up_entry():
    decision = skew_filter(snap(), SKEW_PARAMS)  # skew 2.5 >= 2.0, move up
    assert decision == Decision("enter", "up", 0.74, "entered_skew_up")


def test_skew_canonical_down_entry():
    # skew 200/500 = 0.4 <= 0.5, move down -> agree
    decision = skew_filter(down_move_snap(), SKEW_PARAMS)
    assert decision == Decision("enter", "down", 0.74, "entered_skew_down")


def test_skew_exactly_at_imbalance_is_up():
    decision = skew_filter(snap(up_bid_depth_usd=400.0, down_bid_depth_usd=200.0), SKEW_PARAMS)
    assert decision == Decision("enter", "up", 0.74, "entered_skew_up")


def test_skew_exactly_at_inverse_imbalance_is_down():
    # skew 100/200 = 0.5 == 1/2.0 exactly; needs a down move to agree
    decision = skew_filter(
        down_move_snap(up_bid_depth_usd=100.0, down_bid_depth_usd=200.0), SKEW_PARAMS
    )
    assert decision == Decision("enter", "down", 0.74, "entered_skew_down")


def test_skew_in_dead_zone_skips():
    decision = skew_filter(snap(up_bid_depth_usd=300.0, down_bid_depth_usd=200.0), SKEW_PARAMS)
    assert decision == Decision("skip", None, None, "skip_no_skew")


@pytest.mark.parametrize("field", ["up_bid_depth_usd", "down_bid_depth_usd"])
def test_skew_zero_depth_skips(field):
    decision = skew_filter(snap(**{field: 0.0}), SKEW_PARAMS)
    assert decision == Decision("skip", None, None, "skip_skew_unavailable")


def test_skew_up_but_move_down_disagrees():
    # skew still 2.5 toward UP but the BTC move is down
    decision = skew_filter(snap(btc_last=117_900.0), SKEW_PARAMS)
    assert decision == Decision("skip", None, None, "skip_skew_move_disagree")


def test_skew_down_but_move_up_disagrees():
    decision = skew_filter(snap(up_bid_depth_usd=100.0, down_bid_depth_usd=500.0), SKEW_PARAMS)
    assert decision == Decision("skip", None, None, "skip_skew_move_disagree")


def test_skew_chosen_side_missing_ask_skips():
    decision = skew_filter(snap(up_best_ask=None), SKEW_PARAMS)
    assert decision == Decision("skip", None, None, "skip_missing_price")


def test_skew_ignores_other_side_price():
    decision = skew_filter(snap(down_best_ask=None), SKEW_PARAMS)
    assert decision == Decision("enter", "up", 0.74, "entered_skew_up")


# ---------------------------------------------------------------------------
# registry + evaluate
# ---------------------------------------------------------------------------


def test_registry_matches_valid_rulesets():
    assert set(RULESETS) == VALID_RULESETS
    assert RULESETS["momentum_follow"] is momentum_follow
    assert RULESETS["contrarian_fade"] is contrarian_fade
    assert RULESETS["skew_filter"] is skew_filter


def make_variant(ruleset: str, params: dict, vid: str = "v-test") -> Variant:
    return Variant(id=vid, ruleset=ruleset, status="shadow", allocation_usd=0.0, params=params)


def test_evaluate_dispatches_via_registry():
    variant = make_variant("momentum_follow", MOM_PARAMS)
    assert evaluate(variant, snap()) == momentum_follow(snap(), MOM_PARAMS)


def test_evaluate_unknown_ruleset_raises():
    variant = make_variant("not_a_ruleset", MOM_PARAMS)
    with pytest.raises(ValueError, match="not_a_ruleset"):
        evaluate(variant, snap())


def test_cross_variant_same_snapshot_independent_decisions():
    # Same snapshot, two momentum variants differing only in min_impulse_usd:
    # each decision reflects its own params.
    market = snap(btc_last=118_080.0)  # move == 80
    loose = make_variant("momentum_follow", {**MOM_PARAMS, "min_impulse_usd": 70.0}, "mom-loose")
    strict = make_variant("momentum_follow", {**MOM_PARAMS, "min_impulse_usd": 100.0}, "mom-strict")
    assert evaluate(loose, market) == Decision("enter", "up", 0.74, "entered_momentum_up")
    assert evaluate(strict, market) == Decision("skip", None, None, "skip_impulse_below_min")


# ---------------------------------------------------------------------------
# purity
# ---------------------------------------------------------------------------


def test_module_source_has_no_impure_imports():
    source = Path(rulesets.__file__).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"httpx", "sqlite3", "os", "random", "datetime", "time", "socket", "requests"}
    assert not imported & forbidden, f"impure imports in rulesets module: {imported & forbidden}"
    # No clock reads hiding behind attribute access either.
    for needle in ("datetime.now", "time.time", "monotonic", "urandom"):
        assert needle not in source


@pytest.mark.parametrize(("ruleset", "params"), ALL)
def test_same_inputs_same_decision(ruleset, params):
    market = snap()
    assert ruleset(market, params) == ruleset(market, params)
