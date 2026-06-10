"""
websocket_client.py — Real-time WebSocket connection to Polymarket.

THIS IS THE HEART OF THE DATA LAYER.
It maintains a persistent connection to Polymarket's WebSocket server
and pushes every order book update into a callback function.

KEY CONCEPTS FOR BEGINNERS:
- WebSocket = a persistent two-way connection (unlike HTTP which is request/response)
- async/await = Python's way of doing multiple things concurrently
- Callback = a function we pass in that gets called every time new data arrives

RELIABILITY:
- Automatic reconnection with exponential backoff
- Ping/pong heartbeats to detect dead connections
- Clean shutdown on program exit
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional, Awaitable

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidHandshake,
    InvalidStatus,
)

from config import settings
from data.message_parser import parse_message
from data.models import BookSnapshot, PriceChangeEvent, TradeEvent

logger = logging.getLogger(__name__)

# Type alias: our callback accepts any of these parsed message types
MessageCallback = Callable[
    [BookSnapshot | PriceChangeEvent | TradeEvent],
    Awaitable[None],
]


class WebSocketClient:
    """
    Manages the WebSocket connection to Polymarket's market channel.

    Usage:
        async def on_message(msg):
            print(f"Got: {type(msg).__name__}")

        client = WebSocketClient(token_id="6581...", on_message=on_message)
        await client.start()  # Runs forever, reconnects on failure
    """

    def __init__(
        self,
        token_id: str,
        on_message: MessageCallback,
        on_connected: Optional[Callable[[], Awaitable[None]]] = None,
        on_disconnected: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        """
        Args:
            token_id: The Polymarket token ID to subscribe to
            on_message: Async function called with each parsed message
            on_connected: Optional callback when connection is established
            on_disconnected: Optional callback when connection is lost
        """
        self.token_id = token_id
        self._on_message = on_message
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

        # Connection state
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._connected = False
        self._reconnect_delay = settings.WS_RECONNECT_BASE_DELAY

        # Statistics
        self.messages_received: int = 0
        self.last_message_time: float = 0
        self.connect_time: float = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def latency(self) -> float:
        """Seconds since last message received."""
        if self.last_message_time == 0:
            return 0
        return time.time() - self.last_message_time

    async def start(self):
        """
        Start the WebSocket connection. Runs forever with auto-reconnect.

        This is the main loop:
        1. Connect to Polymarket WebSocket
        2. Subscribe to the market channel
        3. Listen for messages and parse them
        4. If disconnected, wait and reconnect
        """
        self._running = True
        logger.info(f"Starting WebSocket client for token {self.token_id[:20]}...")

        while self._running:
            # Classify the failure (B9): a normal close or a flaky network
            # is routine and gets the standard backoff; a handshake
            # rejection (auth / geo-block / rate limit) will keep failing,
            # so it logs loudly and jumps straight to the max delay.
            slow_down = False
            try:
                await self._connect_and_listen()
            except ConnectionClosedOK:
                logger.info("WebSocket closed normally by server")
            except ConnectionClosedError as e:
                logger.warning(f"WebSocket closed abnormally: {e}")
            except InvalidStatus as e:
                slow_down = self._log_handshake_rejection(e)
            except InvalidHandshake as e:
                logger.error(f"WebSocket handshake failed: {e}")
            except OSError as e:
                # DNS failure, connection refused, network unreachable —
                # transient by nature, normal backoff applies.
                logger.warning(f"Network error (transient): {e}")
            except Exception:
                logger.exception("Unexpected WebSocket error")

            # If we get here, we disconnected
            if self._running:
                self._connected = False
                if self._on_disconnected:
                    await self._on_disconnected()

                if slow_down:
                    self._reconnect_delay = settings.WS_RECONNECT_MAX_DELAY

                # Exponential backoff: wait longer each time we fail
                logger.info(
                    f"Reconnecting in {self._reconnect_delay:.1f}s..."
                )
                await asyncio.sleep(self._reconnect_delay)

                # Double the delay for next time (capped at max)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    settings.WS_RECONNECT_MAX_DELAY,
                )

    @staticmethod
    def _log_handshake_rejection(exc: InvalidStatus) -> bool:
        """
        Log a handshake rejection with the right severity.

        Returns True when the cause will not fix itself (auth/geo-block,
        rate limiting) so the caller should jump straight to max backoff
        instead of hammering the server.
        """
        status = getattr(exc.response, "status_code", None)
        if status in (401, 403):
            logger.error(
                f"WebSocket handshake rejected (HTTP {status}) — likely "
                f"auth or geo-block, NOT transient. Backing off to max; "
                f"investigate before expecting reconnects to succeed."
            )
            return True
        if status == 429:
            logger.error(
                "WebSocket rate-limited (HTTP 429) — backing off to max delay"
            )
            return True
        logger.error(f"WebSocket handshake rejected (HTTP {status})")
        return False

    async def stop(self):
        """Gracefully stop the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
        logger.info("WebSocket client stopped.")

    async def _connect_and_listen(self):
        """
        Internal: establish connection, subscribe, and process messages.

        The subscription message tells Polymarket which market we want:
        {
            "type": "market",
            "assets_ids": ["<our_token_id>"]
        }

        After subscribing, we receive:
        - A "book" event (full snapshot) immediately
        - "price_change" events for every order placed/cancelled
        - "last_trade_price" events for every trade
        """
        uri = settings.CLOB_WS_URL

        async with websockets.connect(
            uri,
            ping_interval=settings.WS_PING_INTERVAL,
            ping_timeout=settings.WS_PING_TIMEOUT,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            self._connected = True
            self.connect_time = time.time()
            # Reset backoff on successful connection
            self._reconnect_delay = settings.WS_RECONNECT_BASE_DELAY

            logger.info("WebSocket connected!")

            if self._on_connected:
                await self._on_connected()

            # Subscribe to market updates for our token
            subscribe_msg = json.dumps({
                "type": "market",
                "assets_ids": [self.token_id],
            })
            await ws.send(subscribe_msg)
            logger.info(f"Subscribed to token {self.token_id[:20]}...")

            # Main message loop — runs until connection drops
            async for raw_message in ws:
                if not self._running:
                    break

                self.last_message_time = time.time()
                self.messages_received += 1

                # Parse the raw JSON into a typed object
                parsed = parse_message(raw_message)
                if parsed is not None:
                    try:
                        await self._on_message(parsed)
                    except Exception:
                        # Full traceback — a crashing callback is a bug in
                        # our pipeline, not a connection problem.
                        logger.exception("Error in message callback")
