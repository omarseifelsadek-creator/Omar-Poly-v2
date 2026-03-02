"""
telegram_bot.py — Telegram notification bot for OBI.

Sends real-time alerts to your phone:
- Trade signals (BUY/SELL recommendations)
- Position entries and exits
- PnL updates
- Market state changes
- Anomaly alerts (spoofs, sweeps, whales)

SETUP:
1. Message @BotFather on Telegram, create a bot, get the token
2. Get your chat ID: message @userinfobot
3. Set environment variables:
   export OBI_TELEGRAM_TOKEN="your-bot-token"
   export OBI_TELEGRAM_CHAT_ID="your-chat-id"

Or pass them directly to TelegramBot()
"""

import os
import time
import asyncio
import logging
from collections import deque
from typing import Optional

import httpx

from data.models import Metrics, Side
from analytics.signals import TradeSignal

logger = logging.getLogger(__name__)


class TelegramBot:
    """
    Async Telegram bot for OBI alerts.

    Sends formatted messages with rate limiting to prevent spam.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: bool = True,
    ):
        self.token = token or os.environ.get("OBI_TELEGRAM_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("OBI_TELEGRAM_CHAT_ID", "")
        self.enabled = enabled and bool(self.token) and bool(self.chat_id)

        self._client: Optional[httpx.AsyncClient] = None
        self._message_queue: deque = deque(maxlen=50)
        self._last_send_time: float = 0
        self._min_interval: float = 1.0  # Min 1 second between messages
        self._send_lock = asyncio.Lock()

        # Rate limiting per category
        self._category_cooldowns: dict[str, float] = {}
        self._cooldown_times = {
            "signal": 30.0,       # One signal alert per 30s
            "trade": 0.0,         # Always send trade alerts
            "state_change": 60.0, # State changes every 60s
            "anomaly": 20.0,      # Anomalies every 20s
            "stats": 300.0,       # Stats summary every 5 min
            "heartbeat": 600.0,   # Heartbeat every 10 min
        }

        if self.enabled:
            logger.info("Telegram bot enabled")
        else:
            if not self.token:
                logger.info("Telegram bot disabled (no token). Set OBI_TELEGRAM_TOKEN to enable.")
            elif not self.chat_id:
                logger.info("Telegram bot disabled (no chat ID). Set OBI_TELEGRAM_CHAT_ID to enable.")

    async def start(self):
        """Initialize the HTTP client."""
        if not self.enabled:
            return
        self._client = httpx.AsyncClient(timeout=10.0)

    async def stop(self):
        """Clean up."""
        if self._client:
            await self._client.aclose()

    def _should_send(self, category: str) -> bool:
        """Check rate limit for category."""
        now = time.time()
        cooldown = self._cooldown_times.get(category, 10.0)
        last = self._category_cooldowns.get(category, 0)
        if now - last < cooldown:
            return False
        self._category_cooldowns[category] = now
        return True

    async def _send(self, text: str, parse_mode: str = "HTML"):
        """Send a message to Telegram."""
        if not self.enabled or not self._client:
            return

        async with self._send_lock:
            # Rate limit
            now = time.time()
            if now - self._last_send_time < self._min_interval:
                await asyncio.sleep(self._min_interval - (now - self._last_send_time))

            try:
                url = f"https://api.telegram.org/bot{self.token}/sendMessage"
                resp = await self._client.post(url, json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                })
                if resp.status_code != 200:
                    logger.warning(f"Telegram send failed: {resp.status_code} {resp.text[:100]}")
                self._last_send_time = time.time()
            except Exception as e:
                logger.warning(f"Telegram error: {e}")

    # ──────────────────────────────────────────────────────────
    # ALERT METHODS
    # ──────────────────────────────────────────────────────────

    async def send_startup(self, market: str, token_label: str, mode: str):
        """Send startup notification."""
        await self._send(
            f"🟢 <b>OBI Started</b>\n"
            f"Market: {market}\n"
            f"Token: {token_label}\n"
            f"Mode: <b>{mode.upper()}</b>\n"
            f"Time: {time.strftime('%H:%M:%S')}"
        )

    async def send_shutdown(self, stats: dict):
        """Send shutdown summary."""
        await self._send(
            f"🔴 <b>OBI Stopped</b>\n"
            f"Trades: {stats.get('total_trades', 0)}\n"
            f"Win Rate: {stats.get('win_rate', 0):.0%}\n"
            f"PnL: <b>${stats.get('total_pnl', 0):+.2f}</b>\n"
            f"Time: {time.strftime('%H:%M:%S')}"
        )

    async def send_signal(self, signal: TradeSignal):
        """Send a trade signal alert."""
        if not self._should_send("signal"):
            return

        arrow = "🟢 ▲" if signal.action == "BUY" else "🔴 ▼"
        await self._send(
            f"{arrow} <b>Signal: {signal.action} {signal.token}</b>\n"
            f"Price: {signal.entry_price:.2f}\n"
            f"Confidence: {signal.confidence}%\n"
            f"Type: {signal.signal_type}\n"
            f"Reason: {signal.reason}\n"
            f"Time: {signal.time_str}"
        )

    async def send_trade_entry(self, action: dict):
        """Send trade entry notification."""
        if not self._should_send("trade"):
            return

        mode_tag = "[PAPER] " if action.get("paper") else ""
        arrow = "🟢" if action["action"] == "BUY" else "🔴"
        await self._send(
            f"{arrow} <b>{mode_tag}ENTRY: {action['action']} {action['token']}</b>\n"
            f"Size: {action['size']} contracts\n"
            f"Price: {action['price']:.2f}\n"
            f"Stop: {action.get('stop_loss', 0):.2f}\n"
            f"Target: {action.get('take_profit', 0):.2f}\n"
            f"Reason: {action['reason']}"
        )

    async def send_trade_exit(self, action: dict):
        """Send trade exit notification."""
        if not self._should_send("trade"):
            return

        mode_tag = "[PAPER] " if action.get("paper") else ""
        pnl = action.get("pnl", 0)
        emoji = "💰" if pnl > 0 else "💸"
        await self._send(
            f"{emoji} <b>{mode_tag}EXIT: {action['action']} {action['token']}</b>\n"
            f"Size: {action['size']} contracts\n"
            f"Price: {action['price']:.2f}\n"
            f"PnL: <b>${pnl:+.2f}</b>\n"
            f"Reason: {action['reason']}"
        )

    async def send_anomaly(self, anomaly_type: str, details: str):
        """Send anomaly alert (spoof, whale, large sweep)."""
        if not self._should_send("anomaly"):
            return

        emoji_map = {
            "spoof": "⚠️",
            "whale": "🐋",
            "sweep": "🔥",
            "regime_change": "📊",
        }
        emoji = emoji_map.get(anomaly_type, "⚡")
        await self._send(
            f"{emoji} <b>{anomaly_type.upper()}</b>\n"
            f"{details}\n"
            f"Time: {time.strftime('%H:%M:%S')}"
        )

    async def send_state_change(
        self,
        market_state: dict,
        metrics: Metrics,
    ):
        """Send periodic market state summary."""
        if not self._should_send("state_change"):
            return

        flow_arrow = "↑" if metrics.flow_pressure > 0.1 else "↓" if metrics.flow_pressure < -0.1 else "→"
        await self._send(
            f"📊 <b>Market State</b>\n"
            f"Regime: {metrics.regime} ({metrics.regime_confidence:.0%})\n"
            f"OBI: {metrics.obi:.0%}\n"
            f"Flow: {metrics.flow_pressure:+.2f} {flow_arrow}\n"
            f"Sentiment: {metrics.sentiment:+.2f}\n"
            f"Vol: {metrics.volatility:.2f}\n"
            f"Mid: {metrics.midpoint:.4f}"
        )

    async def send_stats(self, stats: dict):
        """Send periodic performance stats."""
        if not self._should_send("stats"):
            return

        await self._send(
            f"📈 <b>Session Stats</b>\n"
            f"Mode: {stats.get('mode', '?').upper()}\n"
            f"Trades: {stats.get('total_trades', 0)} "
            f"(W: {stats.get('wins', 0)} / L: {stats.get('losses', 0)})\n"
            f"Win Rate: {stats.get('win_rate', 0):.0%}\n"
            f"PnL: <b>${stats.get('total_pnl', 0):+.2f}</b>\n"
            f"Open: {stats.get('open_positions', 0)}\n"
            f"Signals: {stats.get('signals_received', 0)} "
            f"(filtered: {stats.get('signals_filtered', 0)})"
        )

    async def send_heartbeat(self, market: str, connected: bool, uptime_s: float):
        """Periodic heartbeat — "I'm alive" message."""
        if not self._should_send("heartbeat"):
            return

        mins = int(uptime_s // 60)
        status = "🟢 Connected" if connected else "🔴 Disconnected"
        await self._send(
            f"💓 <b>Heartbeat</b>\n"
            f"Market: {market[:40]}\n"
            f"Status: {status}\n"
            f"Uptime: {mins}m"
        )
