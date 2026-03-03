"""
execution/executor.py — Order Execution Layer for OBI Pair Trading Bot

Provides two executors that share a common interface:
  PaperExecutor  — pass-through; engine already simulated the fill
  LiveExecutor   — submits real FOK taker orders via py-clob-client

The executor sits between pair_runner._try_evaluate() and the chain.
pair_strategy.py is never touched.

─────────────────────────────────────────────────────────────────────
  AUTH MODEL — signature_type=1 (Magic/Email L2 Proxy Wallet)
─────────────────────────────────────────────────────────────────────
  Magic wallets use a two-address model:

    POLY_PRIVATE_KEY  →  derives an EOA ("signer") that signs EIP-712 msgs
    POLY_FUNDER       →  proxy wallet address ("maker") that holds USDC

  Every signed order contains:
    OrderData.maker         = POLY_FUNDER        ← proxy holds the funds
    OrderData.signer        = EOA address         ← private key signs it
    OrderData.signatureType = 1                   ← tells exchange: Magic

  Common failure if done wrong (sig_type=0 or funder omitted):
    maker = signer = EOA → proxy wallet never approved this EOA as operator
    → CLOB API rejects: "not approved" / 401

  L2 API request headers (POST /order):
    Built automatically by create_level_2_headers() inside ClobClient.
    Uses HMAC-SHA256 of: timestamp + "POST" + "/order" + body_json
    Headers sent: POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP,
                  POLY_API_KEY, POLY_PASSPHRASE

  Required ClobClient constructor params:
    key            = POLY_PRIVATE_KEY    (EOA key — the signer)
    creds          = ApiCreds(api_key, api_secret, api_passphrase)
    signature_type = 1                   ← DO NOT OMIT or default to 0
    funder         = POLY_FUNDER         ← DO NOT OMIT
    chain_id       = 137                 ← Polygon mainnet

─────────────────────────────────────────────────────────────────────
  ORDER TYPE: FOK (Fill-or-Kill)
─────────────────────────────────────────────────────────────────────
  Mirrors the paper engine's "limit-or-fail" model exactly.
  Either fills at the given price or is cancelled immediately.
  No partial fills, no resting maker orders.

─────────────────────────────────────────────────────────────────────
  ROLLBACK ON FAILURE
─────────────────────────────────────────────────────────────────────
  pair_runner takes a snapshot of engine state BEFORE evaluate() runs.
  LiveExecutor receives this snapshot and restores it if the order
  fails or is rejected, keeping paper P&L in sync with live reality.
"""

import os
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional

from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"


# ─────────────────────────────────────────────────────────────────────
# BASE INTERFACE
# ─────────────────────────────────────────────────────────────────────

class BaseExecutor(ABC):
    """Common interface for all executor implementations."""

    def warm_up(self, token_ids: list[str]) -> None:
        """Pre-warm connections and caches. No-op for paper mode."""
        pass

    def pre_snapshot(self, engine) -> dict:
        """
        Snapshot mutable engine state BEFORE evaluate() runs.

        Called by pair_runner before engine.evaluate() so that a failed
        live order can be rolled back cleanly. PaperExecutor returns {}
        since no rollback is needed.
        """
        return {}

    @abstractmethod
    async def execute(
        self,
        action: dict,
        engine,
        snapshot: dict,
        token_id: str,
    ) -> Optional[dict]:
        """
        Called after PairTradingEngine.evaluate() has returned an action
        dict and already committed paper state to the engine.

        Args:
            action:    Dict returned by engine.evaluate() — contains side,
                       qty, vwap_price, fill_price, cost, etc.
            engine:    The PairTradingEngine instance. LiveExecutor uses
                       this to roll back state on failure.
            snapshot:  Pre-evaluate snapshot from pre_snapshot(). Used by
                       LiveExecutor to restore state on failure.
            token_id:  Polymarket CLOB token ID to submit the order for
                       (YES token_id or NO token_id, already resolved by
                       pair_runner based on action["side"]).

        Returns:
            Enriched fill dict on success (adds "mode" key at minimum),
            or None on failure. None means the engine was rolled back.
        """
        ...


# ─────────────────────────────────────────────────────────────────────
# PAPER EXECUTOR
# ─────────────────────────────────────────────────────────────────────

class PaperExecutor(BaseExecutor):
    """
    Paper trading executor — pass-through only.

    PairTradingEngine.evaluate() already simulated the fill and updated
    internal state (yes_qty, yes_cost, legs, etc.). Nothing to do here
    except tag the result dict with mode="PAPER" for logging.
    """

    async def execute(
        self,
        action: dict,
        engine,
        snapshot: dict,
        token_id: str,
    ) -> Optional[dict]:
        return {**action, "mode": "PAPER"}


# ─────────────────────────────────────────────────────────────────────
# LIVE EXECUTOR
# ─────────────────────────────────────────────────────────────────────

# Engine state fields to capture for rollback.
# Intentionally excludes buys_attempted / buys_filtered / filter_reasons
# since those reflect real decision activity even on failed fills.
_ROLLBACK_FIELDS = (
    "yes_qty", "no_qty",
    "yes_cost", "no_cost",
    "yes_locked", "no_locked",
    "last_buy_time",
    "buys_executed",
    "fills_attempted",
    "partial_fills",
)


class LiveExecutor(BaseExecutor):
    """
    Live order executor via Polymarket CLOB.

    Lazy-initialises a ClobClient from .env on first execute() call.
    All blocking HTTP calls are dispatched to a thread-pool executor so
    the asyncio event loop (WebSocket message processing) is not blocked.

    See module docstring for full auth model explanation.
    """

    def __init__(self):
        load_dotenv("env")
        self._client: Optional[ClobClient] = None

    # ── Client init ──────────────────────────────────────────────────

    def _ensure_client(self) -> None:
        """Lazy-init the ClobClient once."""
        if self._client is not None:
            return

        key = os.environ.get("POLY_PRIVATE_KEY", "").strip()
        api_key = os.environ.get("POLY_API_KEY", "").strip()
        api_secret = os.environ.get("POLY_API_SECRET", "").strip()
        api_passphrase = os.environ.get("POLY_API_PASSPHRASE", "").strip()
        funder = os.environ.get("POLY_FUNDER", "").strip()

        missing = [
            name for name, val in [
                ("POLY_PRIVATE_KEY", key),
                ("POLY_API_KEY", api_key),
                ("POLY_API_SECRET", api_secret),
                ("POLY_API_PASSPHRASE", api_passphrase),
                ("POLY_FUNDER", funder),
            ]
            if not val
        ]
        if missing:
            raise RuntimeError(f"[LiveExecutor] Missing .env vars: {missing}")

        self._client = ClobClient(
            host=CLOB_HOST,
            chain_id=POLYGON,       # 137 — Polygon mainnet (production)
            key=key,
            creds=ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            ),
            # ↓ Both of these are REQUIRED for Magic/email proxy wallets.
            # Omitting either causes "not approved operator" rejections.
            signature_type=1,       # Magic L2 Proxy — NOT 0 (EOA)
            funder=funder,          # Proxy wallet that holds USDC
        )

        logger.warning(
            f"[LIVE] ClobClient ready | "
            f"signer={self._client.get_address()} | "
            f"funder={funder[:10]}..."
        )

    # ── Pre-warm ───────────────────────────────────────────────────

    def warm_up(self, token_ids: list[str]) -> None:
        """Pre-warm TLS connection + cache tick_size and neg_risk for tokens.

        Called once per window with both YES and NO token IDs.
        Moves the cold-start penalty (~80-130ms) from the first order
        to the setup phase where latency doesn't matter.
        """
        self._ensure_client()
        self._client.get_ok()                       # TLS + HTTP/2 handshake
        for tid in token_ids:
            self._client.get_tick_size(tid)          # cache tick size (300s TTL)
            self._client.get_neg_risk(tid)           # cache neg_risk (indefinite)
        logger.info(f"[LIVE] Warmed up {len(token_ids)} tokens")

    # ── Snapshot / rollback ──────────────────────────────────────────

    def pre_snapshot(self, engine) -> dict:
        """Capture all mutable position fields before evaluate() runs."""
        snap = {k: getattr(engine, k) for k in _ROLLBACK_FIELDS}
        snap["legs"] = list(engine.legs)   # shallow copy — PairLeg objects are frozen
        return snap

    @staticmethod
    def _rollback(engine, snapshot: dict) -> None:
        """Restore engine state from snapshot after a failed order."""
        for k, v in snapshot.items():
            setattr(engine, k, v)
        engine._update_locks()   # recompute yes_locked / no_locked from restored skew

    # ── Order placement (blocking — runs in thread pool) ────────────

    def _place_order_sync(self, token_id: str, qty: float, price: float) -> dict:
        """
        Build, sign, and submit a FOK taker order.

        Blocking — always called via asyncio.run_in_executor so the WS
        message loop is never stalled.

        Returns the raw response dict from the CLOB API.

        neg_risk note (py-clob-client bug #138):
          client.create_order() resolves neg_risk via:
            `if options and options.neg_risk`  ← truthiness, not `is not None`
          This means:
            neg_risk=True  → trusted as-is, skips API call → uses negRisk exchange
            neg_risk=False → treated as falsy → API call fires anyway
            neg_risk=None  → falsy → API call fires (correct authoritative value)
          Passing neg_risk=False explicitly is therefore no better than None, and
          passing neg_risk=True blindly could sign against the wrong exchange address
          if the market flag ever differs from what we cached.
          Solution: omit neg_risk from PartialCreateOrderOptions. create_order()
          will always call get_neg_risk() (cached after first call per token) and
          select the correct exchange address for the EIP-712 domain separator.
        """
        # tick_size: pre-fetched and cached by ClobClient. Passing it here
        # lets __resolve_tick_size validate it against the market minimum;
        # since we fetched it from the same API it will always pass.
        tick_size = self._client.get_tick_size(token_id)

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=qty,
            side=BUY,           # Pair strategy only ever buys YES or NO tokens
        )

        signed_order = self._client.create_order(
            order_args,
            # neg_risk intentionally omitted — see docstring above.
            # create_order resolves it from the API and caches per token.
            PartialCreateOrderOptions(tick_size=tick_size),
        )

        # FOK = Fill-or-Kill. Matches the paper engine's "limit-or-fail" model:
        # either the best ask is still there and we fill, or the order is
        # cancelled immediately with no resting order left on the book.
        return self._client.post_order(signed_order, OrderType.FOK)

    # ── Main entry point ─────────────────────────────────────────────

    async def execute(
        self,
        action: dict,
        engine,
        snapshot: dict,
        token_id: str,
    ) -> Optional[dict]:
        """
        Submit a live FOK order. Roll back engine state on any failure.

        The engine has already committed the paper state (yes_qty, legs, etc.)
        before this is called. If the live order fails, we restore from
        snapshot so the paper P&L stays in sync with live reality.
        """
        try:
            self._ensure_client()
        except RuntimeError as e:
            logger.error(str(e))
            self._rollback(engine, snapshot)
            return None

        side = action["side"]
        qty = float(action["qty"])
        vwap_price = float(action["vwap_price"])

        logger.warning(
            f"[LIVE] Submitting FOK | {side} {qty:.0f} shares "
            f"@ ${vwap_price:.4f} | token={token_id[:16]}..."
        )

        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._place_order_sync(token_id, qty, vwap_price),
            )
        except Exception as exc:
            logger.error(
                f"[LIVE] Order exception | {side} {qty:.0f} @ ${vwap_price:.4f} | {exc}",
                exc_info=True,
            )
            self._rollback(engine, snapshot)
            return None

        if not isinstance(response, dict):
            logger.error(f"[LIVE] Unexpected response: {response!r}")
            self._rollback(engine, snapshot)
            return None

        # Pull the key fields from the CLOB response.
        # The field names observed in production: "orderID", "status", "errorMsg".
        order_id = response.get("orderID") or response.get("id", "")
        status = response.get("status", "")
        error = response.get("errorMsg") or response.get("error", "")

        # A FOK order that hits the book gets status "matched" or "" (live fill).
        # "unmatched" / "cancelled" means the price moved and the order was rejected.
        if order_id and status not in ("unmatched", "cancelled", "canceled", "error"):
            logger.warning(
                f"[LIVE] FILLED | {side} {qty:.0f} @ ${vwap_price:.4f} | "
                f"orderID={order_id} status={status!r}"
            )
            return {**action, "order_id": order_id, "live_status": status, "mode": "LIVE"}

        logger.warning(
            f"[LIVE] REJECTED | {side} {qty:.0f} @ ${vwap_price:.4f} | "
            f"status={status!r} error={error!r} orderID={order_id!r}"
        )
        self._rollback(engine, snapshot)
        return None


# ─────────────────────────────────────────────────────────────────────
# DRY-RUN EXECUTOR
# ─────────────────────────────────────────────────────────────────────

class DryRunExecutor(LiveExecutor):
    """
    Full flow without post_order — the safe step before real money.

    Runs exactly what LiveExecutor does up to and including create_order():
      ✓  _ensure_client()          — validates .env vars, builds ClobClient
      ✓  get_tick_size(token_id)   — confirms API connectivity
      ✓  create_order(...)         — calls get_neg_risk(), builds OrderData,
                                     computes EIP-712 domain + struct hash,
                                     signs with POLY_PRIVATE_KEY
      ✗  post_order(...)           — skipped (no order reaches the relayer)

    The signed order is logged at INFO level so you can verify:
      • maker  = POLY_FUNDER  (not the EOA — confirms sig_type=1 is wired)
      • signer = EOA derived from POLY_PRIVATE_KEY
      • signatureType = 1
      • makerAmount / takerAmount look correct for the price + qty
      • signature starts with 0x and is 130 chars (65-byte ECDSA)

    Engine state IS committed (paper P&L runs normally) so you get a full
    simulated session with real credential + signing validation baked in.
    Rollback only happens on sign failure, same as LiveExecutor on fill failure.
    """

    def _sign_only_sync(self, token_id: str, qty: float, price: float):
        """
        Build and sign the order without submitting it.
        Returns SignedOrder (not a response dict).
        Blocking — called via run_in_executor.
        """
        tick_size = self._client.get_tick_size(token_id)
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=qty,
            side=BUY,
        )
        # neg_risk omitted — same reasoning as LiveExecutor._place_order_sync.
        return self._client.create_order(
            order_args,
            PartialCreateOrderOptions(tick_size=tick_size),
        )

    async def execute(
        self,
        action: dict,
        engine,
        snapshot: dict,
        token_id: str,
    ) -> Optional[dict]:
        try:
            self._ensure_client()
        except RuntimeError as e:
            logger.error(str(e))
            self._rollback(engine, snapshot)
            return None

        side = action["side"]
        qty = float(action["qty"])
        vwap_price = float(action["vwap_price"])

        logger.warning(
            f"[DRY-RUN] Signing | {side} {qty:.0f} shares "
            f"@ ${vwap_price:.4f} | token={token_id[:16]}..."
        )

        try:
            loop = asyncio.get_running_loop()
            signed = await loop.run_in_executor(
                None,
                lambda: self._sign_only_sync(token_id, qty, vwap_price),
            )
        except Exception as exc:
            logger.error(
                f"[DRY-RUN] Sign failed | {side} {qty:.0f} @ ${vwap_price:.4f} | {exc}",
                exc_info=True,
            )
            self._rollback(engine, snapshot)
            return None

        # Log the signed order so the user can verify every field before going live.
        d = signed.dict()
        logger.warning(
            f"[DRY-RUN] SIGNED (not posted) | {side} {qty:.0f} @ ${vwap_price:.4f}\n"
            f"  maker={d['maker']}  signer={d['signer']}\n"
            f"  signatureType={d['signatureType']}  feeRateBps={d['feeRateBps']}\n"
            f"  makerAmount={d['makerAmount']}  takerAmount={d['takerAmount']}\n"
            f"  tokenId={d['tokenId']}\n"
            f"  sig={d['signature'][:20]}...{d['signature'][-6:]}"
        )

        import time as _time
        return {
            **action,
            "order_id": f"DRY-{int(_time.time() * 1000)}",
            "mode": "DRY-RUN",
        }


# ─────────────────────────────────────────────────────────────────────
# FACTORY
# ─────────────────────────────────────────────────────────────────────

def make_executor(mode: str) -> BaseExecutor:
    """
    Return the right executor for the given trading mode.

    Args:
        mode: "paper", "dry-run", or "live"

    Returns:
        PaperExecutor  — pure simulation, no network calls for orders
        DryRunExecutor — signs real orders, skips post_order
        LiveExecutor   — full live trading
    """
    if mode == "live":
        return LiveExecutor()
    if mode == "dry-run":
        return DryRunExecutor()
    return PaperExecutor()
