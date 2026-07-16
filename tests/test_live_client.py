"""U11: live client — everything mocked, zero network, vendor package not required.

Behaviors pinned here:
- engine.live_client imports without py_clob_client_v2 installed (lazy import)
- LiveCredentials: missing file / group-other perms / missing key all refuse;
  0600 loads; repr masks the key so safe_log_line can serialize records
- LiveExecutor.execute: write-ahead ledger row BEFORE submission; GTC limit
  params asserted on the mock; submission failure -> 'failed' row + raise
- poll_fills: exchange fill price + fee (formula fallback when no fee reported)
- cancel_stale: orders inside the exit-before window -> cancel + 'cancelled'
- reconcile_on_startup: ledger-only -> failed + event; exchange-only ->
  cancel + event; matched-but-stale -> cancelled + event; matched-live adopted
- integrity_check: clean tmp git repo True; dirty tracked engine/ file False
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from engine.executor import safe_log_line
from engine.ledger import Ledger
from engine.live_client import (
    CLOB_HOST,
    POLYGON_CHAIN_ID,
    CredentialsError,
    LiveCredentials,
    LiveExecutor,
    OrderSubmissionError,
    derive_l2,
    integrity_check,
)
from engine.risk import Approved
from engine.signals import FeatureSnapshot

BUCKET = 1_752_499_800  # multiple of BUCKET_SEC
SLUG = f"btc-updown-5m-{BUCKET}"
CLOSE = BUCKET + 300
NOW = float(BUCKET + 160)
EXIT_BEFORE_SEC = 20
TOKEN_UP = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
PRIVATE_KEY = "0x" + "ab" * 32


def snap(**overrides) -> FeatureSnapshot:
    defaults = dict(
        bucket_ts=BUCKET,
        market_slug=SLUG,
        seconds_to_close=140.0,
        btc_open=118_000.0,
        btc_last=118_100.0,
        up_best_bid=0.68,
        up_best_ask=0.70,
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


def approved(**overrides) -> Approved:
    defaults = dict(
        variant_id="v-live",
        bucket_ts=BUCKET,
        market_slug=SLUG,
        side="up",
        limit_price=0.70,
        stake_usd=5.0,
        mode="live",
    )
    defaults.update(overrides)
    return Approved(**defaults)


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    yield led
    led.close()


@pytest.fixture
def client():
    mock = MagicMock()
    mock.resolve_token_id.return_value = TOKEN_UP
    mock.post_order.return_value = "ord-1"
    mock.get_open_orders.return_value = []
    return mock


@pytest.fixture
def executor(ledger, client):
    config = SimpleNamespace(risk=SimpleNamespace(exit_before_sec=EXIT_BEFORE_SEC))
    return LiveExecutor(ledger, client, config, clock=lambda: NOW)


def risk_event_kinds(ledger) -> list[str]:
    rows = ledger._conn.execute("SELECT kind FROM risk_events ORDER BY id").fetchall()
    return [row["kind"] for row in rows]


def only_trade(ledger, variant_id="v-live"):
    trades = ledger.trades_for_variant(variant_id, "live")
    assert len(trades) == 1
    return trades[0]


# ---------------------------------------------------------------------------
# module import + credentials
# ---------------------------------------------------------------------------


def test_module_imports_without_vendor_package():
    # engine.live_client was imported at collection time; if that had pulled
    # in the vendor package (installed or not), it would appear here. Lazy
    # import is the contract: shadow-mode installs never carry the package.
    assert "py_clob_client_v2" not in sys.modules


def test_credentials_missing_file_raises(tmp_path):
    with pytest.raises(CredentialsError, match="not found"):
        LiveCredentials.load(tmp_path / "env")


def test_credentials_group_other_perms_refused(tmp_path):
    path = tmp_path / "env"
    path.write_text(f"POLYMARKET_PRIVATE_KEY={PRIVATE_KEY}\n")
    path.chmod(0o644)
    with pytest.raises(CredentialsError, match="chmod 600"):
        LiveCredentials.load(path)


def test_credentials_0600_loads_with_extras_and_comments(tmp_path):
    path = tmp_path / "env"
    path.write_text(
        "# main-CLOB credentials\n"
        f"POLYMARKET_PRIVATE_KEY='{PRIVATE_KEY}'\n"
        "\n"
        "POLYMARKET_PROXY_ADDRESS=0xdead\n"
    )
    path.chmod(0o600)
    creds = LiveCredentials.load(path)
    assert creds.private_key == PRIVATE_KEY
    assert creds.extras == {"POLYMARKET_PROXY_ADDRESS": "0xdead"}


def test_credentials_missing_key_raises(tmp_path):
    path = tmp_path / "env"
    path.write_text("SOMETHING_ELSE=1\n")
    path.chmod(0o600)
    with pytest.raises(CredentialsError, match="POLYMARKET_PRIVATE_KEY"):
        LiveCredentials.load(path)


def test_credentials_repr_masks_key_and_passes_safe_log_line(tmp_path):
    path = tmp_path / "env"
    path.write_text(f"POLYMARKET_PRIVATE_KEY={PRIVATE_KEY}\nAPI_SECRET=hunter2\n")
    path.chmod(0o600)
    creds = LiveCredentials.load(path)
    for rendered in (repr(creds), str(creds)):
        assert PRIVATE_KEY not in rendered
        assert "hunter2" not in rendered
        assert "0x****" in rendered
    # The masked repr survives the logging choke point...
    assert safe_log_line({"creds": repr(creds)})
    # ...while the raw key would (correctly) trip it — the mask is load-bearing.
    with pytest.raises(ValueError, match="hex_private_key"):
        safe_log_line({"creds": creds.private_key})


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def test_execute_writes_ledger_before_submitting(executor, ledger, client):
    seen_at_submission = {}

    def post(**kwargs):
        # Write-ahead contract: the open row must already exist when the
        # exchange call happens (a crash here leaves a reconcilable row).
        trades = ledger.trades_for_variant("v-live", "live")
        seen_at_submission["rows"] = [(t.status, t.intended_price) for t in trades]
        return "ord-1"

    client.post_order.side_effect = post
    trade_id = executor.execute(approved(), snap())
    assert seen_at_submission["rows"] == [("open", 0.70)]
    trade = only_trade(ledger)
    assert (trade.id, trade.status, trade.filled_price) == (trade_id, "open", None)


def test_execute_submits_gtc_limit_only(executor, client):
    executor.execute(approved(), snap())
    client.post_order.assert_called_once_with(
        token_id=TOKEN_UP,
        side="BUY",
        price=0.70,
        size=pytest.approx(5.0 / 0.70),
        order_type="GTC",
    )
    client.resolve_token_id.assert_called_once_with(SLUG, "up")


def test_execute_refuses_non_live_or_non_approved(executor):
    with pytest.raises(AssertionError):
        executor.execute(approved(mode="shadow"), snap())
    lookalike = SimpleNamespace(**approved().__dict__)
    with pytest.raises(AssertionError):
        executor.execute(lookalike, snap())


def test_execute_submission_failure_marks_failed_and_raises(executor, ledger, client):
    client.post_order.side_effect = RuntimeError("exchange said no")
    client.get_open_orders.return_value = []  # genuinely never landed
    with pytest.raises(OrderSubmissionError):
        executor.execute(approved(), snap())
    trade = only_trade(ledger)
    assert (trade.status, trade.filled_price, trade.fee_usd) == ("failed", None, 0.0)
    # A failed submission leaves nothing to poll or cancel.
    assert executor.poll_fills(NOW) == 0
    assert executor.cancel_stale(CLOSE) == 0


def test_execute_ambiguous_submission_recovers_matched_order(executor, ledger, client):
    # post_order() raised (e.g. a timeout), but the order actually landed on
    # the exchange -- it must be adopted, not silently lost as 'failed'.
    client.post_order.side_effect = TimeoutError("response lost")
    client.get_open_orders.return_value = [
        {"order_id": "ord-recovered", "token_id": TOKEN_UP, "price": 0.70}
    ]
    trade_id = executor.execute(approved(), snap())
    trade = only_trade(ledger)
    assert (trade.id, trade.status, trade.order_id) == (trade_id, "open", "ord-recovered")
    assert risk_event_kinds(ledger) == ["ambiguous_submission_recovered"]
    # It's tracked again: a later fill lands in the ledger normally.
    client.get_order.return_value = {"status": "filled", "filled_price": 0.70, "fee_usd": 0.01}
    assert executor.poll_fills(NOW + 5) == 1
    client.get_order.assert_called_once_with("ord-recovered")
    assert only_trade(ledger).status == "filled"


# ---------------------------------------------------------------------------
# poll_fills
# ---------------------------------------------------------------------------


def test_poll_fills_records_exchange_price_and_fee(executor, ledger, client):
    trade_id = executor.execute(approved(), snap())
    client.get_order.return_value = {"status": "filled", "filled_price": 0.71, "fee_usd": 0.02}
    assert executor.poll_fills(NOW + 5) == 1
    client.get_order.assert_called_once_with("ord-1")
    trade = only_trade(ledger)
    assert (trade.id, trade.status) == (trade_id, "filled")
    assert trade.filled_price == 0.71
    assert trade.fee_usd == 0.02
    # Terminal orders leave the tracking map: no re-poll, no double transition.
    client.get_order.reset_mock()
    assert executor.poll_fills(NOW + 10) == 0
    client.get_order.assert_not_called()


def test_poll_fills_open_order_stays_open(executor, ledger, client):
    executor.execute(approved(), snap())
    client.get_order.return_value = {"status": "open", "filled_price": None, "fee_usd": None}
    assert executor.poll_fills(NOW + 5) == 0
    assert only_trade(ledger).status == "open"


def test_poll_fills_formula_fee_when_exchange_reports_none(executor, ledger, client):
    executor.execute(approved(), snap(fee_rate=0.07, fees_enabled=True))
    client.get_order.return_value = {"status": "filled", "filled_price": 0.71, "fee_usd": None}
    executor.poll_fills(NOW + 5)
    shares = 5.0 / 0.71
    assert only_trade(ledger).fee_usd == pytest.approx(shares * 0.07 * 0.71 * (1 - 0.71))


# ---------------------------------------------------------------------------
# cancel_stale
# ---------------------------------------------------------------------------


def test_cancel_stale_inside_exit_window(executor, ledger, client):
    executor.execute(approved(), snap())
    client.get_order.return_value = {"status": "cancelled", "filled_price": None, "fee_usd": None}
    assert executor.cancel_stale(float(CLOSE - EXIT_BEFORE_SEC)) == 1
    client.cancel_order.assert_called_once_with("ord-1")
    # cancel_order alone doesn't confirm fate -- the exchange is re-queried.
    client.get_order.assert_called_once_with("ord-1")
    trade = only_trade(ledger)
    # The non-fill outcome is Phase 2 fill-rate data: intended price, zero fee.
    assert (trade.status, trade.filled_price, trade.fee_usd) == ("cancelled", 0.70, 0.0)


def test_cancel_stale_leaves_orders_outside_window(executor, ledger, client):
    executor.execute(approved(), snap())
    assert executor.cancel_stale(float(CLOSE - EXIT_BEFORE_SEC - 60)) == 0
    client.cancel_order.assert_not_called()
    assert only_trade(ledger).status == "open"


def test_cancel_stale_fill_races_cancel_records_filled_not_cancelled(executor, ledger, client):
    # cancel_order() can succeed on the exchange side of a race it already
    # lost: the order filled a moment before the cancel landed. Re-querying
    # after cancel_order() must catch this instead of blindly writing
    # 'cancelled' with a zero-cost, wrong outcome.
    executor.execute(approved(), snap())
    client.get_order.return_value = {"status": "filled", "filled_price": 0.70, "fee_usd": 0.03}
    assert executor.cancel_stale(float(CLOSE - EXIT_BEFORE_SEC)) == 1
    client.cancel_order.assert_called_once_with("ord-1")
    trade = only_trade(ledger)
    assert (trade.status, trade.filled_price, trade.fee_usd) == ("filled", 0.70, 0.03)


def test_cancel_stale_unconfirmed_after_cancel_stays_open_for_retry(executor, ledger, client):
    executor.execute(approved(), snap())
    client.get_order.return_value = {"status": "open", "filled_price": None, "fee_usd": None}
    assert executor.cancel_stale(float(CLOSE - EXIT_BEFORE_SEC)) == 0
    assert only_trade(ledger).status == "open"
    assert risk_event_kinds(ledger) == ["cancel_not_confirmed"]


# ---------------------------------------------------------------------------
# reconcile_on_startup
# ---------------------------------------------------------------------------


def open_ledger_row(ledger, variant_id="v-live") -> int:
    return ledger.record_trade(
        ts=NOW,
        bucket_ts=BUCKET,
        variant_id=variant_id,
        market_slug=SLUG,
        side="up",
        mode="live",
        intended_price=0.70,
        stake_usd=5.0,
        status="open",
    )


def test_reconcile_ledger_orphan_marked_failed(executor, ledger, client):
    open_ledger_row(ledger)
    client.get_open_orders.return_value = []
    executor.reconcile_on_startup(NOW)
    assert only_trade(ledger).status == "failed"
    assert risk_event_kinds(ledger) == ["reconcile_orphan_ledger"]


def test_reconcile_exchange_orphan_cancelled(executor, ledger, client):
    client.get_open_orders.return_value = [
        {"order_id": "ghost-1", "token_id": TOKEN_UP, "price": 0.70}
    ]
    executor.reconcile_on_startup(NOW)
    client.cancel_order.assert_called_once_with("ghost-1")
    assert risk_event_kinds(ledger) == ["reconcile_orphan_exchange"]


def test_reconcile_matched_but_stale_cancelled(executor, ledger, client):
    trade_id = open_ledger_row(ledger)
    client.get_open_orders.return_value = [
        {"order_id": "ord-9", "token_id": TOKEN_UP, "price": 0.70}
    ]
    client.get_order.return_value = {"status": "cancelled", "filled_price": None, "fee_usd": None}
    executor.reconcile_on_startup(float(CLOSE - EXIT_BEFORE_SEC + 5))
    client.cancel_order.assert_called_once_with("ord-9")
    # cancel_order alone doesn't confirm fate -- the exchange is re-queried.
    client.get_order.assert_called_once_with("ord-9")
    trade = only_trade(ledger)
    assert (trade.id, trade.status, trade.filled_price) == (trade_id, "cancelled", 0.70)
    assert risk_event_kinds(ledger) == ["reconcile_stale_cancelled"]


def test_reconcile_matched_but_stale_fill_races_cancel(executor, ledger, client):
    trade_id = open_ledger_row(ledger)
    client.get_open_orders.return_value = [
        {"order_id": "ord-9", "token_id": TOKEN_UP, "price": 0.70}
    ]
    client.get_order.return_value = {"status": "filled", "filled_price": 0.70, "fee_usd": 0.02}
    executor.reconcile_on_startup(float(CLOSE - EXIT_BEFORE_SEC + 5))
    client.cancel_order.assert_called_once_with("ord-9")
    trade = only_trade(ledger)
    assert (trade.id, trade.status, trade.filled_price, trade.fee_usd) == (
        trade_id, "filled", 0.70, 0.02,
    )
    assert risk_event_kinds(ledger) == ["reconcile_stale_cancelled"]


def test_reconcile_matched_live_order_is_adopted(executor, ledger, client):
    open_ledger_row(ledger)
    client.get_open_orders.return_value = [
        {"order_id": "ord-9", "token_id": TOKEN_UP, "price": 0.70}
    ]
    executor.reconcile_on_startup(NOW)  # 140s to close: still live
    client.cancel_order.assert_not_called()
    assert risk_event_kinds(ledger) == ["reconcile_adopted"]
    # The adopted order is tracked again: a later fill lands in the ledger.
    client.get_order.return_value = {"status": "filled", "filled_price": 0.72, "fee_usd": 0.01}
    assert executor.poll_fills(NOW + 5) == 1
    client.get_order.assert_called_once_with("ord-9")
    assert only_trade(ledger).filled_price == 0.72


def test_reconcile_uses_persisted_order_id_directly(executor, ledger, client):
    """Same-process crash mid-lifecycle: the ledger row already carries the
    order_id attached at submission. reconcile_on_startup must query it
    directly via get_order rather than fuzzy-matching against
    get_open_orders."""
    trade_id = open_ledger_row(ledger)
    ledger.attach_order_id(trade_id, "ord-9")
    client.get_open_orders.return_value = []  # irrelevant: order_id is known
    client.get_order.return_value = {"status": "open", "filled_price": None, "fee_usd": None}

    executor.reconcile_on_startup(NOW)  # 140s to close: still live

    client.get_order.assert_called_once_with("ord-9")
    client.resolve_token_id.assert_called_once_with(SLUG, "up")
    assert risk_event_kinds(ledger) == ["reconcile_adopted"]
    assert only_trade(ledger).status == "open"


def test_reconcile_filled_order_no_longer_open_is_recorded_not_lost(executor, ledger, client):
    """The P0 case: a FILLED order also disappears from get_open_orders(),
    so it looks identical to 'never placed' by fuzzy matching alone. With
    order_id persisted, get_order() proves it was filled -- it must be
    recorded as such, never silently lost as 'failed'."""
    trade_id = open_ledger_row(ledger)
    ledger.attach_order_id(trade_id, "ord-9")
    client.get_open_orders.return_value = []  # filled orders vanish from here
    client.get_order.return_value = {"status": "filled", "filled_price": 0.71, "fee_usd": 0.02}

    executor.reconcile_on_startup(NOW)

    trade = only_trade(ledger)
    assert (trade.id, trade.status, trade.filled_price, trade.fee_usd) == (
        trade_id, "filled", 0.71, 0.02,
    )
    assert risk_event_kinds(ledger) == ["reconcile_filled"]


def test_reconcile_cancelled_order_confirmed_via_order_id(executor, ledger, client):
    trade_id = open_ledger_row(ledger)
    ledger.attach_order_id(trade_id, "ord-9")
    client.get_open_orders.return_value = []
    client.get_order.return_value = {"status": "cancelled", "filled_price": None, "fee_usd": None}

    executor.reconcile_on_startup(NOW)

    trade = only_trade(ledger)
    assert (trade.id, trade.status) == (trade_id, "cancelled")
    assert risk_event_kinds(ledger) == ["reconcile_cancelled"]


# ---------------------------------------------------------------------------
# derive_l2 (two-step auth sequence, factory injected)
# ---------------------------------------------------------------------------


def test_derive_l2_sequence():
    factory = MagicMock()
    creds = LiveCredentials(private_key=PRIVATE_KEY)
    api_creds = factory.return_value.create_or_derive_api_key.return_value

    result = derive_l2(factory, creds)

    assert factory.call_count == 2
    first, second = factory.call_args_list
    assert first.kwargs == {"host": CLOB_HOST, "key": PRIVATE_KEY, "chain_id": POLYGON_CHAIN_ID}
    assert second.kwargs == {
        "host": CLOB_HOST,
        "key": PRIVATE_KEY,
        "chain_id": POLYGON_CHAIN_ID,
        "creds": api_creds,
    }
    factory.return_value.create_or_derive_api_key.assert_called_once_with()
    assert result is factory.return_value


# ---------------------------------------------------------------------------
# integrity_check (tmp git repo, never the real one)
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "engine").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "engine" / "risk.py").write_text("CAP = 5.0\n")
    (tmp_path / "config" / "rulesets.yaml").write_text("version: 1\n")
    (tmp_path / "README.md").write_text("readme\n")
    git("add", ".")
    git("commit", "-qm", "init")
    return tmp_path


def test_integrity_check_clean_repo(git_repo):
    assert integrity_check(git_repo) is True


def test_integrity_check_dirty_engine_file(git_repo):
    (git_repo / "engine" / "risk.py").write_text("CAP = 500.0\n")
    assert integrity_check(git_repo) is False


def test_integrity_check_untracked_config_file(git_repo):
    (git_repo / "config" / "extra.yaml").write_text("sneaky: true\n")
    assert integrity_check(git_repo) is False


def test_integrity_check_ignores_dirt_outside_guarded_paths(git_repo):
    (git_repo / "README.md").write_text("edited readme\n")
    assert integrity_check(git_repo) is True
