"""Live execution path — the ONLY module that touches ``py_clob_client_v2``.

The v1 client (``py-clob-client``) was archived abruptly on 2026-05-25 and its
orders are rejected by the V2 exchange. That churn is why everything
venue-specific lives behind the ``VenueClient`` seam in this one file:

- ``LiveExecutor`` implements ``engine.executor.ExecutorProtocol`` against the
  seam and never imports the vendor package.
- ``_ClobV2Adapter`` (plus ``derive_l2`` and ``build_venue_client``) is the
  only code that speaks the vendor API, and it imports ``py_clob_client_v2``
  LAZILY: importing this module must always succeed, because the package is an
  optional ``[live]`` extra and shadow mode never installs it.

Hard invariants (grep-able):

- GTC LIMIT orders only. ``ORDER_TYPE`` is the single order-type constant and
  it is "GTC"; no market order (FOK/FAK) is ever constructed anywhere.
- Write-ahead ledger: the 'open' trade row is written BEFORE the order is
  submitted to the exchange. A crash between the two leaves a reconcilable
  open row (handled by ``reconcile_on_startup``), never an untracked order.
- Credentials live OUTSIDE the repo tree (the repo is mounted into agent
  containers) and are never logged: ``LiveCredentials`` masks its repr so
  ``engine.executor.safe_log_line`` can serialize records that mention it.

The runbook (docs/phase2-runbook.md) gates any real use of this module behind
the venue decision gate — this code is main-CLOB only and must not be adapted
ad hoc for Polymarket US (separate venue, separate Ed25519 SDK).
"""

from __future__ import annotations

import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from engine.config import Config
from engine.ledger import Ledger
from engine.risk import Approved
from engine.signals import BUCKET_SEC, FeatureSnapshot

DEFAULT_CREDENTIALS_PATH = "~/.config/i-am-not-a-trader-bot/env"

# Main-CLOB endpoints/chain. Polymarket US is a DIFFERENT venue (Ed25519 API,
# separate SDK) — see docs/phase2-runbook.md before touching any of this.
CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

# The only order type this system ever submits. GTC limit orders rest until
# filled or cancelled at exit-before-sec; there are NO market orders anywhere.
ORDER_TYPE = "GTC"

# Reconciliation match tolerance: exchange-reported price vs our intended
# limit. CLOB prices are 2-decimal ticks, so anything under half a tick is
# "the same order".
_PRICE_MATCH_EPS = 0.001


class CredentialsError(RuntimeError):
    """Missing/insecure credentials. Live mode must refuse to start on this."""


class OrderSubmissionError(RuntimeError):
    """The exchange rejected (or errored on) an order submission."""


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveCredentials:
    """L1 wallet key loaded from an env file OUTSIDE the repo tree.

    The repr/str is hard-masked: this object may end up inside log records,
    and ``safe_log_line`` would (correctly) refuse a line containing the raw
    0x+64hex key. Nothing here ever renders the key.
    """

    private_key: str = field(repr=False)
    extras: dict[str, str] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:  # dataclass skips generating one when defined
        return "LiveCredentials(private_key='0x****', extras=<masked>)"

    __str__ = __repr__

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CREDENTIALS_PATH) -> LiveCredentials:
        """Parse a KEY=VALUE env file; require POLYMARKET_PRIVATE_KEY.

        Refuses (raises CredentialsError) if the file mode has any group or
        other bits set — the file must be 0600 (`chmod 600 <path>`), because
        the host runs agent tooling and the key must be readable by the
        daemon's user only.
        """
        path = Path(path).expanduser()
        if not path.exists():
            raise CredentialsError(
                f"credentials file not found: {path} "
                "(see docs/phase2-runbook.md, section 2, for setup)"
            )
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise CredentialsError(
                f"credentials file {path} has mode {mode:03o} with group/other "
                f"bits set; refusing to load. Fix: chmod 600 {path}"
            )
        pairs: dict[str, str] = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            pairs[key.strip()] = value.strip().strip("'\"")
        private_key = pairs.pop("POLYMARKET_PRIVATE_KEY", "")
        if not private_key:
            raise CredentialsError(f"POLYMARKET_PRIVATE_KEY missing or empty in {path}")
        return cls(private_key=private_key, extras=pairs)


# ---------------------------------------------------------------------------
# the venue seam
# ---------------------------------------------------------------------------


class VenueClient(Protocol):
    """What LiveExecutor requires of a venue. Only _ClobV2Adapter implements
    this for real; tests implement it with mocks. Normalized shapes:

    - ``get_order`` returns ``{'status': 'open'|'filled'|'cancelled',
      'filled_price': float|None, 'fee_usd': float|None}``.
    - ``get_open_orders`` returns ``[{'order_id': str, 'token_id': str,
      'price': float}, ...]``.
    """

    def resolve_token_id(self, market_slug: str, side: str) -> str: ...

    def post_order(
        self, *, token_id: str, side: str, price: float, size: float, order_type: str
    ) -> str: ...

    def get_order(self, order_id: str) -> dict: ...

    def get_open_orders(self) -> list[dict]: ...

    def cancel_order(self, order_id: str) -> None: ...


@dataclass
class _OpenOrder:
    """In-memory tracking for one resting live order (lost on crash; rebuilt
    by reconcile_on_startup from ledger + exchange state)."""

    trade_id: int
    order_id: str
    token_id: str
    bucket_ts: int
    intended_price: float
    stake_usd: float
    fee_rate: float
    fees_enabled: bool


class LiveExecutor:
    """Real-order execution behind ``ExecutorProtocol``: GTC limit orders,
    write-ahead ledger rows, fill polling, stale-order cancellation, and
    crash reconciliation.

    ``clock`` is injected for deterministic tests; ``client`` is any
    ``VenueClient`` (the real one comes from ``build_venue_client``).
    """

    def __init__(
        self,
        ledger: Ledger,
        client: VenueClient,
        config: Config,
        clock: Callable[[], float] = time.time,
    ):
        self._ledger = ledger
        self._client = client
        self._config = config
        self._clock = clock
        self._orders: dict[int, _OpenOrder] = {}  # trade_id -> resting order

    # -- ExecutorProtocol ----------------------------------------------------

    def execute(self, approved: Approved, snap: FeatureSnapshot) -> int:
        # Same choke-point discipline as ShadowExecutor: only a risk-issued
        # approval, and only in this executor's mode.
        assert isinstance(approved, Approved), "executor accepts only risk-issued approvals"
        assert approved.mode == "live", "LiveExecutor handles live mode only"

        token_id = self._client.resolve_token_id(approved.market_slug, approved.side)
        shares = approved.stake_usd / approved.limit_price

        # Write-ahead: ledger first, exchange second. If we crash between the
        # two, reconcile_on_startup finds an open row with no exchange order
        # and marks it failed — an untracked live order can never exist.
        trade_id = self._ledger.record_trade(
            ts=self._clock(),
            bucket_ts=approved.bucket_ts,
            variant_id=approved.variant_id,
            market_slug=approved.market_slug,
            side=approved.side,
            mode="live",
            intended_price=approved.limit_price,
            stake_usd=approved.stake_usd,
            status="open",
        )
        try:
            order_id = self._client.post_order(
                token_id=token_id,
                side="BUY",  # binary market: we only ever BUY the chosen outcome token
                price=approved.limit_price,
                size=shares,
                order_type=ORDER_TYPE,
            )
        except Exception as exc:
            self._ledger.update_trade_fill(trade_id, None, 0.0, "failed")
            raise OrderSubmissionError(
                f"order submission failed for trade {trade_id} "
                f"({approved.market_slug} {approved.side})"
            ) from exc
        self._orders[trade_id] = _OpenOrder(
            trade_id=trade_id,
            order_id=order_id,
            token_id=token_id,
            bucket_ts=approved.bucket_ts,
            intended_price=approved.limit_price,
            stake_usd=approved.stake_usd,
            fee_rate=snap.fee_rate,
            fees_enabled=snap.fees_enabled,
        )
        return trade_id

    # -- order lifecycle -------------------------------------------------------

    def poll_fills(self, now: float) -> int:
        """Check resting orders; ledger fills (exchange price + fee) and
        exchange-side cancellations. Returns the number of fills recorded."""
        filled = 0
        for trade_id, order in list(self._orders.items()):
            status = self._client.get_order(order.order_id)
            state = status.get("status")
            if state == "filled":
                price = status.get("filled_price")
                if price is None:
                    price = order.intended_price
                fee = status.get("fee_usd")
                if fee is None:
                    fee = self._formula_fee(order, price)
                self._ledger.update_trade_fill(trade_id, price, fee, "filled")
                del self._orders[trade_id]
                filled += 1
            elif state == "cancelled":
                # Cancelled on the exchange side (not by us): still a terminal
                # non-fill outcome the Phase 2 comparison needs.
                self._ledger.update_trade_fill(trade_id, order.intended_price, 0.0, "cancelled")
                del self._orders[trade_id]
        return filled

    def cancel_stale(self, now: float) -> int:
        """Cancel any resting order inside the exit-before window. The
        resulting 'cancelled' row IS the data: non-fill outcomes feed the
        Phase 2 shadow-vs-live fill-rate comparison."""
        cancelled = 0
        for trade_id, order in list(self._orders.items()):
            close_ts = order.bucket_ts + BUCKET_SEC
            if close_ts - now <= self._config.risk.exit_before_sec:
                self._client.cancel_order(order.order_id)
                self._ledger.update_trade_fill(trade_id, order.intended_price, 0.0, "cancelled")
                del self._orders[trade_id]
                cancelled += 1
        return cancelled

    # -- crash reconciliation ----------------------------------------------------

    def reconcile_on_startup(self, now: float) -> None:
        """Reconcile ledger open rows against exchange open orders. MUST run
        before any new live order after a restart: a crash between place and
        cancel leaves an orphaned GTC order that could fill in the final
        seconds and ride to resolution untracked.

        Every action is ledgered as a risk_event:
        - ledger row with no exchange order -> 'failed' (the write-ahead row
          whose submission never happened, or an order that vanished).
        - exchange order with no ledger row -> cancel on the exchange (we
          never adopt an order we cannot attribute).
        - matched pair already past exit-before-sec -> cancel + 'cancelled'.
        - matched pair still live -> adopted into in-memory tracking.
        """
        exchange = list(self._client.get_open_orders())
        ledger_open = [
            t for t in self._ledger.open_trades() if t.mode == "live" and t.status == "open"
        ]
        claimed: set[str] = set()
        for trade in ledger_open:
            token_id = self._client.resolve_token_id(trade.market_slug, trade.side)
            match = next(
                (
                    o
                    for o in exchange
                    if o["order_id"] not in claimed
                    and o.get("token_id") == token_id
                    and abs(float(o.get("price", -1)) - trade.intended_price) < _PRICE_MATCH_EPS
                ),
                None,
            )
            if match is None:
                self._ledger.update_trade_fill(trade.id, None, 0.0, "failed")
                self._ledger.record_risk_event(
                    now,
                    "reconcile_orphan_ledger",
                    f"trade {trade.id} ({trade.market_slug} {trade.side} "
                    f"@ {trade.intended_price}) has no matching exchange order; marked failed",
                )
                continue
            claimed.add(match["order_id"])
            close_ts = trade.bucket_ts + BUCKET_SEC
            if close_ts - now <= self._config.risk.exit_before_sec:
                self._client.cancel_order(match["order_id"])
                self._ledger.update_trade_fill(trade.id, trade.intended_price, 0.0, "cancelled")
                self._ledger.record_risk_event(
                    now,
                    "reconcile_stale_cancelled",
                    f"trade {trade.id} order {match['order_id']} past exit-before-sec; cancelled",
                )
            else:
                # Adopted: fee_rate is unknown post-crash, so a later fill
                # relies on the exchange-reported fee (formula falls back to 0).
                self._orders[trade.id] = _OpenOrder(
                    trade_id=trade.id,
                    order_id=match["order_id"],
                    token_id=token_id,
                    bucket_ts=trade.bucket_ts,
                    intended_price=trade.intended_price,
                    stake_usd=trade.stake_usd,
                    fee_rate=0.0,
                    fees_enabled=False,
                )
                self._ledger.record_risk_event(
                    now,
                    "reconcile_adopted",
                    f"trade {trade.id} order {match['order_id']} still live; re-tracked",
                )
        for order in exchange:
            if order["order_id"] not in claimed:
                self._client.cancel_order(order["order_id"])
                self._ledger.record_risk_event(
                    now,
                    "reconcile_orphan_exchange",
                    f"exchange order {order['order_id']} has no ledger row; cancelled",
                )

    # -- internals ----------------------------------------------------------------

    @staticmethod
    def _formula_fee(order: _OpenOrder, price: float) -> float:
        """Polymarket's published taker formula, used only when the exchange
        response carries no fee: shares * fee_rate * p * (1-p)."""
        if not order.fees_enabled:
            return 0.0
        shares = order.stake_usd / price
        return shares * order.fee_rate * price * (1.0 - price)


# ---------------------------------------------------------------------------
# vendor adapter (the ONLY code that speaks py_clob_client_v2)
# ---------------------------------------------------------------------------


def derive_l2(client_factory: Callable[..., Any], creds: LiveCredentials) -> Any:
    """py-clob-client-v2 two-step auth: an L1 client (EOA private key, EIP-712,
    Polygon chain 137) derives L2 HMAC API credentials via
    ``create_or_derive_api_key()``, then the full client is re-initialized
    with both the key and the derived creds.

    ``client_factory`` is injected (the real ``ClobClient`` class in
    production, a mock in tests) so the two-step sequence is testable without
    the package installed.
    """
    l1_client = client_factory(host=CLOB_HOST, key=creds.private_key, chain_id=POLYGON_CHAIN_ID)
    api_creds = l1_client.create_or_derive_api_key()
    return client_factory(
        host=CLOB_HOST, key=creds.private_key, chain_id=POLYGON_CHAIN_ID, creds=api_creds
    )


class _ClobV2Adapter:
    """Normalizes the vendor client to the VenueClient seam.

    CAUTION — vendor-API guesswork zone: py_clob_client_v2's exact call and
    response shapes were NOT exercised against a live venue when this was
    written (the venue gate blocks that). Every method below follows the v1
    client's surface, which v2 documents as compatible for main-CLOB order
    flow, but the Phase 2 smoke test (runbook section 3) must verify each
    call before real money moves. Confirmed-by-research facts: GTC/GTD limit
    + FOK/FAK market order types; L1->L2 auth via create_or_derive_api_key.
    """

    def __init__(self, client: Any, gamma_client: Any):
        self._client = client
        self._gamma = gamma_client  # engine.market_feed.GammaClient (slug -> token ids)

    def resolve_token_id(self, market_slug: str, side: str) -> str:
        market = self._gamma.resolve_market(market_slug)
        if market is None:
            raise OrderSubmissionError(f"cannot resolve market '{market_slug}' via Gamma")
        return market.token_id_up if side == "up" else market.token_id_down

    def post_order(
        self, *, token_id: str, side: str, price: float, size: float, order_type: str
    ) -> str:
        if order_type != ORDER_TYPE:
            raise OrderSubmissionError(f"only {ORDER_TYPE} limit orders are permitted")
        # GUESS (verify in smoke test): v1-style OrderArgs + create_and_post_order.
        from py_clob_client_v2.clob_types import OrderArgs, OrderType
        from py_clob_client_v2.order_builder.constants import BUY

        assert side == "BUY"
        args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        signed = self._client.create_order(args)
        resp = self._client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        if not order_id:
            raise OrderSubmissionError(f"no order id in post_order response keys={list(resp)}")
        return str(order_id)

    def get_order(self, order_id: str) -> dict:
        # GUESS (verify in smoke test): v1-style get_order response with
        # status LIVE/MATCHED/CANCELED, price, size_matched.
        raw = self._client.get_order(order_id)
        status = str(raw.get("status", "")).upper()
        normalized = {"LIVE": "open", "MATCHED": "filled", "CANCELED": "cancelled"}.get(
            status, "open"
        )
        price = raw.get("price")
        return {
            "status": normalized,
            "filled_price": float(price) if price is not None else None,
            "fee_usd": float(raw["fee"]) if raw.get("fee") is not None else None,
        }

    def get_open_orders(self) -> list[dict]:
        # GUESS (verify in smoke test): v1-style get_orders() open-order list
        # with id + asset_id + price fields.
        return [
            {
                "order_id": str(raw.get("id") or raw.get("orderID")),
                "token_id": str(raw.get("asset_id") or raw.get("token_id")),
                "price": float(raw.get("price", 0.0)),
            }
            for raw in self._client.get_orders()
        ]

    def cancel_order(self, order_id: str) -> None:
        self._client.cancel(order_id)


def build_venue_client(creds: LiveCredentials) -> VenueClient:
    """Construct the real main-CLOB client. Imports the vendor package lazily:
    shadow mode never has it installed, and this module must import cleanly
    regardless."""
    try:
        from py_clob_client_v2.client import ClobClient
    except ImportError as exc:
        raise RuntimeError(
            "py_clob_client_v2 is not installed; live mode requires "
            "`pip install '.[live]'` (see docs/phase2-runbook.md, section 2)"
        ) from exc
    from engine.market_feed import GammaClient

    client = derive_l2(ClobClient, creds)
    return _ClobV2Adapter(client, GammaClient())


# ---------------------------------------------------------------------------
# go-live integrity check
# ---------------------------------------------------------------------------


def integrity_check(repo_root: Path | str) -> bool:
    """True iff tracked ``engine/`` and ``config/`` paths are clean vs git.

    Live mode refuses to start on a dirty tree: those directories enforce the
    hard caps, and the last human-reviewed commit is the trust anchor (agent
    containers mount code read-only, but defense in depth is the point).
    Used by ``traderctl go-live``.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain", "--", "engine", "config"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip() == ""
