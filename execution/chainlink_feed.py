"""
execution/chainlink_feed.py — Polymarket Chainlink price stream tracker.

Moved verbatim from pair_runner.py (B13). Background WebSocket listener
that tracks the same Chainlink price Polymarket uses to resolve crypto
Up/Down markets — the primary settlement source for pair trading.
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

CHAINLINK_WS = "wss://ws-live-data.polymarket.com"


class ChainlinkTracker:
    """Track crypto price from Polymarket's Chainlink data stream."""

    def __init__(self, symbol: str = "btc/usd"):
        self.symbol = symbol
        self.latest_price: Optional[float] = None
        self.latest_ts: float = 0.0
        self.window_open_price: Optional[float] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def start(self):
        """Launch the background WebSocket listener."""
        self._running = True
        self._task = asyncio.create_task(self._ws_loop())

    def stop(self):
        """Stop the background listener."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    def snapshot_open(self):
        """Record the current price as window open."""
        self.window_open_price = self.latest_price
        if self.latest_price:
            logger.info(f"[CHAINLINK] Window open price: ${self.latest_price:,.2f}")

    def resolve(self) -> Optional[str]:
        """Determine winner from open vs current price."""
        if self.window_open_price is None or self.latest_price is None:
            return None
        if self.latest_price > self.window_open_price:
            winner = "YES"
        elif self.latest_price < self.window_open_price:
            winner = "NO"
        else:
            winner = "NO"  # "Up" requires strictly greater
        logger.warning(
            f"[CHAINLINK] Resolution: {winner} | "
            f"Open=${self.window_open_price:,.2f} "
            f"Close=${self.latest_price:,.2f} "
            f"Δ=${self.latest_price - self.window_open_price:+,.2f}"
        )
        return winner

    async def _ws_loop(self):
        """Connect to Polymarket Chainlink stream and track price."""
        while self._running:
            try:
                async with websockets.connect(
                    CHAINLINK_WS, ping_interval=30, ping_timeout=10,
                ) as ws:
                    # Subscribe to Chainlink price feed
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": json.dumps({"symbol": self.symbol}),
                        }],
                    }))
                    logger.info(f"[CHAINLINK] Subscribed to {self.symbol.upper()} price stream")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            payload = msg.get("payload", {})
                            price = payload.get("value")
                            if price is not None:
                                self.latest_price = float(price)
                                self.latest_ts = time.time()
                        except (json.JSONDecodeError, ValueError):
                            continue

            except asyncio.CancelledError:
                return
            except Exception as e:
                if self._running:
                    logger.warning(f"[CHAINLINK] WS error: {e}, reconnecting in 3s...")
                    await asyncio.sleep(3)
