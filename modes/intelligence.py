"""
modes/intelligence.py — OBIApp, the single-token intelligence dashboard.

Moved verbatim from main.py (B13). The full pipeline for one token:
WebSocket -> orderbook/level tracker -> metrics + detectors + momentum
+ CVD -> insights -> Rich terminal UI + Telegram + SQLite.

Reachable via --btc5m (auto-rotating windows). NOTE: the --token direct
path is currently a dead CLI arg (B19) — parse_args accepts it but
main() never wires it here.
"""

import asyncio
import logging
import time
from typing import Optional, Union

from rich.console import Console
from rich.live import Live

from config import settings
from config.live_config import LiveConfig
from data.websocket_client import WebSocketClient
from data.rest_client import RestClient
from data.models import BookSnapshot, PriceChangeEvent, TradeEvent, Side
from state.orderbook import OrderBook
from state.level_tracker import LevelTracker
from analytics.metrics import compute_all_metrics
from analytics.interpreter import generate_insights
from analytics.momentum import MomentumEngine
from analytics.cvd import CVDTracker
from storage.database import Database
from ui.terminal import TerminalUI
from telegram.telegram_bot import TelegramBot

logger = logging.getLogger(__name__)
console = Console()


class OBIApp:
    """
    The main application that coordinates all components.

    Phase 3 additions:
    - MomentumEngine: EMA-smoothed trend tracking, regime detection, volatility
    - Enhanced sentiment scoring with momentum, regime, and detection inputs

    Lifecycle:
    1. __init__: Create all components
    2. run: Initialize DB, start WebSocket + UI + DB stats loop
    3. Ctrl+C: Clean shutdown (flush DB writes)
    """

    def __init__(self, token_id: str, market_question: str = "", token_label: str = "Yes",
                 trading_mode: str = "paper"):
        # Core components
        self.orderbook = OrderBook()
        self.level_tracker = LevelTracker()       # Phase 2
        self.momentum_engine = MomentumEngine()   # Phase 3
        self.cvd_tracker = CVDTracker()           # Phase 4: session-persistent CVD
        self.db = Database() if settings.STORAGE_ENABLED else None
        self.ui = TerminalUI(self.orderbook, market_question or "Loading...", token_label,
                             level_tracker=self.level_tracker)
        self.rest_client = RestClient()

        # Telegram + config
        self.live_config = LiveConfig()
        self.telegram = TelegramBot(enabled=self.live_config.telegram_enabled)

        # State
        self.token_id = token_id
        self.market_question = market_question
        self.token_label = token_label
        self._prev_metrics = None
        self._ws_client: Optional[WebSocketClient] = None
        self._metrics_store_counter = 0

    async def run(self):
        """
        Main run loop. Starts WebSocket, UI, and DB stats concurrently.
        """
        console.print(f"\n[bold cyan]Starting OBI v5.0 Intelligence Dashboard for:[/bold cyan] {self.market_question}")
        console.print(f"[dim]Token: {self.token_id[:40]}...[/dim]")
        console.print("[dim]Mode: Market Intelligence (read-only)[/dim]")

        # Initialize database
        if self.db:
            await self.db.initialize()
            console.print(f"[dim]Database: {settings.DB_PATH}[/dim]")

        # Start Telegram
        await self.telegram.start()
        await self.telegram.send_startup(
            self.market_question, self.token_label, "intelligence"
        )

        console.print("[dim]Connecting to Polymarket WebSocket...[/dim]\n")

        # Create WebSocket client with our message handler
        self._ws_client = WebSocketClient(
            token_id=self.token_id,
            on_message=self._handle_message,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
        )

        try:
            tasks = [
                self._ws_client.start(),
                self._run_ui(),
                self._key_listener(),
            ]
            # Add DB stats updater if storage is enabled
            if self.db:
                tasks.append(self._update_db_stats_loop())

            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    async def _handle_message(
        self, msg: Union[BookSnapshot, PriceChangeEvent, TradeEvent]
    ):
        """
        Handle a parsed WebSocket message.

        PHASE 2 DATA PIPELINE:
        1. Apply update to order book state
        2. Update level tracker (per-level history)
        3. Compute metrics + run detectors
        4. Generate insights
        5. Store to database (async, non-blocking)
        6. Update UI
        """
        # Step 1: Update order book state
        if isinstance(msg, BookSnapshot):
            self.orderbook.apply_snapshot(msg)
            # On full snapshot, record all levels in tracker
            for level in msg.bids:
                self.level_tracker.record_change(
                    level.price, Side.BUY, level.size, msg.timestamp_ms
                )
            for level in msg.asks:
                self.level_tracker.record_change(
                    level.price, Side.SELL, level.size, msg.timestamp_ms
                )

        elif isinstance(msg, PriceChangeEvent):
            self.orderbook.apply_price_change(msg)
            # Step 2: Update level tracker for each price change
            for change in msg.price_changes:
                if not self.orderbook.asset_id or change.asset_id == self.orderbook.asset_id:
                    self.level_tracker.record_change(
                        change.price, change.side, change.size, msg.timestamp_ms
                    )

        elif isinstance(msg, TradeEvent):
            self.orderbook.apply_trade(msg)
            # Record trade at the relevant price level for absorption detection
            # (level side is derived from trade direction inside the tracker)
            self.level_tracker.record_trade_at_level(
                price=msg.price,
                trade_size=msg.size,
                trade_side=msg.side,
                timestamp_ms=msg.timestamp_ms,
            )
            # Phase 3: Feed trade price to momentum engine for volatility tracking
            self.momentum_engine.update(trade_price=msg.price)
            # Phase 4: Feed trade to CVD tracker (persists across reconnects)
            self.cvd_tracker.record_trade(msg)
            # Store trade to database
            if self.db:
                await self.db.store_trade(self.token_id, msg)

        # Step 3: Compute metrics + run Phase 2 detectors + Phase 3 momentum + Phase 4 intelligence
        if self.orderbook.is_initialized:
            metrics = compute_all_metrics(
                self.orderbook,
                self.level_tracker,
                self.momentum_engine,
                self.cvd_tracker,
            )

            # Step 4: Generate insights
            insights = generate_insights(metrics, self._prev_metrics, self.token_label)

            # Send anomaly alerts via Telegram
            if metrics.sweep_events and len(metrics.sweep_events) >= 3:
                await self.telegram.send_anomaly("sweep",
                    f"{len(metrics.sweep_events)} sweeps detected")
            if metrics.whale_events:
                for w in metrics.whale_events[:1]:
                    val = w.price * w.size
                    if val >= 1000:
                        side = "Buy" if w.side == Side.BUY else "Sell"
                        await self.telegram.send_anomaly("whale",
                            f"Whale {side}: {w.size:,.0f} @ {w.price:.2f} (${val:,.0f})")

            # Periodic state update (handled by cooldown inside telegram)
            await self.telegram.send_state_change({}, metrics)

            # Step 5: Store to database (rate-limited)
            if self.db:
                await self.db.store_snapshot(self.token_id, self.orderbook, metrics)

                # Store metrics every 10th update (avoid flooding DB)
                self._metrics_store_counter += 1
                if self._metrics_store_counter >= 10:
                    await self.db.store_metrics(self.token_id, metrics)
                    self._metrics_store_counter = 0

                # Store significant insights as events
                if insights:
                    significant = [i for i in insights if i.severity in ("alert", "warning")]
                    if significant:
                        await self.db.store_insights(self.token_id, significant)

            # Step 6: Update UI
            self.ui.update_metrics(metrics)
            if insights:
                self.ui.add_insights(insights)

            self._prev_metrics = metrics

        # Update connection stats in UI
        if self._ws_client:
            self.ui.set_messages_count(self._ws_client.messages_received)

    async def _on_connected(self):
        """Called when WebSocket connects."""
        self.ui.set_connected(True)
        logger.info("WebSocket connected")

    async def _on_disconnected(self):
        """Called when WebSocket disconnects."""
        self.ui.set_connected(False)
        logger.warning("WebSocket disconnected")

    async def _key_listener(self):
        """Listen for keyboard input to resize panels."""
        import sys
        import tty
        import termios

        # Save terminal settings
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        try:
            # Set terminal to raw mode (non-blocking single char reads)
            tty.setcbreak(fd)
            loop = asyncio.get_event_loop()

            while True:
                # Read one character non-blocking via executor
                char = await loop.run_in_executor(None, sys.stdin.read, 1)
                if char:
                    self.ui.handle_key(char)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # Fail silently — keyboard resize is optional
        finally:
            # Restore terminal settings
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass

    async def _run_ui(self):
        """
        UI refresh loop — redraws the terminal at a fixed rate.

        Runs independently from the data loop. This means the UI
        stays responsive even if data processing is slow.
        """
        # Small delay to let WebSocket connect first
        await asyncio.sleep(1.0)

        with Live(
            self.ui.build_layout(),
            console=self.ui.console,
            refresh_per_second=1 / settings.UI_REFRESH_RATE,
            screen=True,  # Full-screen mode
        ) as live:
            while True:
                try:
                    live.update(self.ui.build_layout())
                    await asyncio.sleep(settings.UI_REFRESH_RATE)
                except asyncio.CancelledError:
                    break

    async def _shutdown(self):
        """Clean shutdown of all components."""
        elapsed = int(time.time() - self.ui._start_time)
        mins, secs = divmod(elapsed, 60)

        # Print session summary
        console.print("\n[bold cyan]═══ SESSION SUMMARY ═══[/bold cyan]")
        console.print("  Mode:      [bold]INTELLIGENCE[/bold]")
        console.print(f"  Duration:  {mins}m {secs}s")
        console.print(f"  Messages:  {self.ui._messages_count}")
        console.print(f"  CVD:       {self.cvd_tracker.cumulative:+,.0f} ({self.cvd_tracker.trade_count} trades)")
        console.print("[dim]═══════════════════════[/dim]\n")

        # Send final status via Telegram
        await self.telegram.send_shutdown({"mode": "intelligence", "duration": elapsed})
        await self.telegram.stop()

        if self._ws_client:
            await self._ws_client.stop()
        if self.db:
            await self.db.close()
        await self.rest_client.close()
        logger.info("OBI v4.3 shutdown complete")

    async def _update_db_stats_loop(self):
        """Periodically update database stats in the UI footer."""
        while True:
            try:
                if self.db:
                    stats = await self.db.get_stats(self.token_id)
                    self.ui.set_db_stats(stats)
                await asyncio.sleep(10)  # Update every 10 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"DB stats error: {e}")
                await asyncio.sleep(10)
