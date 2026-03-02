"""
main.py — Entry point and async orchestrator for Polymarket OBI.

THIS FILE WIRES EVERYTHING TOGETHER:
1. Discovers the market (REST API)
2. Connects to WebSocket for live data
3. Feeds data through: parser → orderbook → analytics → interpreter → UI

HOW TO RUN:
    python main.py                          # Interactive market selector
    python main.py --token <TOKEN_ID>       # Direct token ID
    python main.py --slug <market-slug>     # From Polymarket URL slug
    python main.py --search "bitcoin"       # Search and pick

BEGINNER NOTE:
`asyncio` is Python's way of running multiple things concurrently.
We need this because we're doing TWO things at the same time:
  1. Listening for WebSocket messages (data in)
  2. Refreshing the terminal UI (display out)
Both need to run continuously without blocking each other.
"""

import asyncio
import argparse
import logging
import json
import sys
import time
from typing import Optional, Union

from rich.console import Console
from rich.live import Live
from rich.prompt import Prompt, IntPrompt

from config import settings
from data.websocket_client import WebSocketClient
from data.rest_client import RestClient
from data.models import BookSnapshot, PriceChangeEvent, TradeEvent, Side
from state.orderbook import OrderBook
from state.level_tracker import LevelTracker
from analytics.metrics import compute_all_metrics
from analytics.interpreter import generate_insights
from analytics.signals import generate_signals, TradeSignal
from analytics.momentum import MomentumEngine
from storage.database import Database
from ui.terminal import TerminalUI
from execution.strategy import StrategyEngine, StrategyConfig, TradingMode
from telegram.telegram_bot import TelegramBot
from execution.trade_logger import log_trade_entry, log_trade_exit, log_signal, log_stats, log_session_summary
from config.live_config import LiveConfig

# Configure logging (only show warnings+ to avoid cluttering the terminal)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler("obi.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# Rich console for the market selector
console = Console()


# ──────────────────────────────────────────────────────────────
# MAIN APPLICATION CLASS
# ──────────────────────────────────────────────────────────────

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
        self.db = Database() if settings.STORAGE_ENABLED else None
        self.ui = TerminalUI(self.orderbook, market_question or "Loading...", token_label)
        self.rest_client = RestClient()

        # Phase 5: Execution + Telegram
        self.live_config = LiveConfig()
        strategy_config = self.live_config.get_config()
        # Override mode from CLI if specified
        if trading_mode == "live":
            strategy_config.mode = TradingMode.LIVE
        self.strategy = StrategyEngine(strategy_config, token_id, token_label)
        self.telegram = TelegramBot(enabled=self.live_config.telegram_enabled)

        # State
        self.token_id = token_id
        self.market_question = market_question
        self.token_label = token_label
        self._prev_metrics = None
        self._ws_client: Optional[WebSocketClient] = None
        self._metrics_store_counter = 0
        self._stats_log_counter = 0

    async def run(self):
        """
        Main run loop. Starts WebSocket, UI, and DB stats concurrently.
        """
        console.print(f"\n[bold cyan]Starting OBI v4.3 for:[/bold cyan] {self.market_question}")
        console.print(f"[dim]Token: {self.token_id[:40]}...[/dim]")
        console.print(f"[dim]Strategy: {self.strategy.config.mode.value.upper()}[/dim]")

        # Initialize database
        if self.db:
            await self.db.initialize()
            console.print(f"[dim]Database: {settings.DB_PATH}[/dim]")

        # Start Telegram
        await self.telegram.start()
        await self.telegram.send_startup(
            self.market_question, self.token_label, self.strategy.config.mode.value
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
            self.level_tracker.record_trade_at_level(
                price=msg.price,
                side=Side.BUY,  # This is the book side the resting order was on
                trade_size=msg.size,
                trade_side=msg.side,
                timestamp_ms=msg.timestamp_ms,
            )
            # Phase 3: Feed trade price to momentum engine for volatility tracking
            self.momentum_engine.update(trade_price=msg.price)
            # Store trade to database
            if self.db:
                await self.db.store_trade(self.token_id, msg)

        # Step 3: Compute metrics + run Phase 2 detectors + Phase 3 momentum
        if self.orderbook.is_initialized:
            metrics = compute_all_metrics(
                self.orderbook,
                self.level_tracker,
                self.momentum_engine,
            )

            # Step 4: Generate insights and trade signals
            insights = generate_insights(metrics, self._prev_metrics, self.token_label)
            trade_signals = generate_signals(metrics, self._prev_metrics, self.token_label)

            # Step 4.5: Phase 5 — Strategy evaluation
            if trade_signals:
                actions = self.strategy.evaluate(trade_signals, metrics)

                # Log all signals (whether acted on or not)
                acted_signal_types = {a.get("signal_type") for a in actions if "Entry" in a.get("reason", "")}
                for sig in trade_signals:
                    acted = sig.signal_type in acted_signal_types
                    log_signal(self.market_question, sig, acted)

                for action in actions:
                    # Log + telegram
                    if "Entry" in action.get("reason", ""):
                        log_trade_entry(self.market_question, action)
                        await self.telegram.send_trade_entry(action)
                    elif "Exit" in action.get("reason", ""):
                        log_trade_exit(self.market_question, action)
                        await self.telegram.send_trade_exit(action)

                # Send high-confidence signal alerts
                for sig in trade_signals:
                    if sig.confidence >= 75:
                        await self.telegram.send_signal(sig)

            # Periodic stats log (every 60s handled by _stats_log_counter)
            self._stats_log_counter += 1
            if self._stats_log_counter >= 120:  # ~60s at 0.5s refresh
                stats = self.strategy.get_stats()
                log_stats(self.market_question, stats)
                self._stats_log_counter = 0

            # Hot-reload config every ~5s (10 ticks at 0.5s)
            if self._stats_log_counter % 10 == 0:
                if self.live_config.check_reload():
                    self.strategy.config = self.live_config.get_config()

            # Update UI with strategy stats
            self.ui.set_strategy_stats(self.strategy.get_stats())

            # Send anomaly alerts
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

            # Periodic state + stats (handled by cooldown inside telegram)
            await self.telegram.send_state_change({}, metrics)
            stats = self.strategy.get_stats()
            if stats["total_trades"] > 0:
                await self.telegram.send_stats(stats)

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
            if trade_signals:
                self.ui.add_signals(trade_signals)

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
        # Log final session summary
        stats = self.strategy.get_stats()
        log_session_summary(self.market_question, stats)

        # Print results to console
        console.print(f"\n[bold cyan]═══ SESSION RESULTS ═══[/bold cyan]")
        console.print(f"  Mode:      [bold]{stats['mode'].upper()}[/bold]")
        console.print(f"  Trades:    {stats['total_trades']} (W: {stats['wins']} / L: {stats['losses']})")
        console.print(f"  Win Rate:  {stats['win_rate']:.0%}")
        console.print(f"  PnL:       [{'green' if stats['total_pnl'] >= 0 else 'red'}]${stats['total_pnl']:+.2f}[/]")
        console.print(f"  Avg Hold:  {stats['avg_hold_time']:.0f}s")
        console.print(f"  Signals:   {stats['signals_received']} received, {stats['signals_filtered']} filtered")
        console.print(f"  Log:       data/logs/trades_{time.strftime('%Y%m%d')}.csv")
        console.print(f"[dim]═══════════════════════[/dim]\n")

        # Send final stats via Telegram
        await self.telegram.send_shutdown(stats)
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


# ──────────────────────────────────────────────────────────────
# MARKET SELECTION (Interactive CLI)
# ──────────────────────────────────────────────────────────────

async def select_market_interactive() -> tuple[str, str]:
    """
    Interactive market selector — helps the user find a market to monitor.

    Returns:
        (token_id, market_question) tuple
    """
    rest = RestClient()

    console.print("\n[bold cyan]═══ POLYMARKET OBI — Market Selector ═══[/bold cyan]\n")

    while True:
        console.print("[bold]Choose an option:[/bold]")
        console.print("  [cyan]1[/cyan] — Search markets by keyword")
        console.print("  [cyan]2[/cyan] — Browse top active markets")
        console.print("  [cyan]3[/cyan] — Enter a token ID directly")
        console.print("  [cyan]4[/cyan] — Enter a market URL slug")
        console.print()

        choice = Prompt.ask("Select", choices=["1", "2", "3", "4"], default="1")

        if choice == "1":
            query = Prompt.ask("Search for")
            console.print(f"\n[dim]Searching for '{query}'...[/dim]")
            markets = await rest.search_markets(query, limit=10)

            if not markets:
                console.print("[yellow]No markets found. Try a different search.[/yellow]\n")
                continue

            result = _display_and_pick_market(markets)
            if result:
                await rest.close()
                return result

        elif choice == "2":
            console.print("\n[dim]Fetching top active markets...[/dim]")
            markets = await rest.get_active_markets(limit=15)

            if not markets:
                console.print("[yellow]Could not fetch markets.[/yellow]\n")
                continue

            result = _display_and_pick_market(markets)
            if result:
                await rest.close()
                return result

        elif choice == "3":
            token_id = Prompt.ask("Enter token ID")
            question = Prompt.ask("Market question (optional)", default="Custom Market")
            await rest.close()
            return token_id, question, "Yes"

        elif choice == "4":
            slug = Prompt.ask("Enter market URL slug")
            console.print(f"\n[dim]Looking up '{slug}'...[/dim]")
            market = await rest.get_market_by_slug(slug)

            if market:
                token_ids = market.get("clobTokenIds", [])
                if isinstance(token_ids, str):
                    try:
                        token_ids = json.loads(token_ids)
                    except (json.JSONDecodeError, TypeError):
                        token_ids = [token_ids]

                if token_ids:
                    # If multiple tokens, let user pick
                    if len(token_ids) > 1:
                        outcomes = market.get("outcomes", [])
                        if isinstance(outcomes, str):
                            try:
                                outcomes = json.loads(outcomes)
                            except (json.JSONDecodeError, TypeError):
                                outcomes = []
                        console.print(f"\n  Tokens available:")
                        for i, tid in enumerate(token_ids):
                            label = outcomes[i] if i < len(outcomes) else f"Token {i+1}"
                            console.print(f"  [cyan]{i+1}[/cyan]  {label}  [dim]{tid[:40]}...[/dim]")
                        tc = IntPrompt.ask("Pick token", default=1)
                        ti = max(0, min(tc - 1, len(token_ids) - 1))
                        chosen_label = outcomes[ti] if ti < len(outcomes) else "Yes"
                        await rest.close()
                        return token_ids[ti], market.get("question", slug), chosen_label
                    else:
                        await rest.close()
                        return token_ids[0], market.get("question", slug), "Yes"
                else:
                    console.print("[yellow]Market found but no token IDs available.[/yellow]\n")
            else:
                console.print("[yellow]Market not found. Check the slug.[/yellow]\n")

    await rest.close()


def _display_and_pick_market(markets: list[dict]) -> Optional[tuple[str, str, str]]:
    """Display a list of markets and let the user pick one. Returns (token_id, question, token_label)."""
    console.print()

    for i, m in enumerate(markets, 1):
        question = m.get("question", "Unknown")
        vol_24h = float(m.get("volume24hr", 0) or 0)
        vol_total = float(m.get("volume", 0) or 0)
        # Show 24hr volume if available, otherwise total
        vol_display = vol_24h if vol_24h > 0 else vol_total
        vol_label = "24h" if vol_24h > 0 else "tot"
        active = "🟢" if m.get("active") and not m.get("closed") else "🔴"
        token_ids = m.get("clobTokenIds", [])
        has_tokens = "✓" if token_ids else "✗"

        console.print(
            f"  [cyan]{i:2d}[/cyan]  {active} {question[:65]}"
            f"  [dim]Vol({vol_label}): ${vol_display:,.0f}  Tokens: {has_tokens}[/dim]"
        )

    console.print(f"\n  [dim] 0  — Go back[/dim]")
    console.print()

    idx = IntPrompt.ask("Pick a market", default=1)

    if idx == 0 or idx > len(markets):
        return None

    selected = markets[idx - 1]
    token_ids = selected.get("clobTokenIds", [])

    if not token_ids:
        console.print("[yellow]This market has no trading tokens available.[/yellow]\n")
        return None

    question = selected.get("question", "Unknown Market")

    # Parse token IDs — they may be a JSON string or already a list
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except (json.JSONDecodeError, TypeError):
            token_ids = [token_ids]

    # Get outcome labels (YES/NO) if available
    outcomes = selected.get("outcomes", [])
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (json.JSONDecodeError, TypeError):
            outcomes = []

    console.print(f"\n[green]Selected:[/green] {question}")

    if len(token_ids) == 1:
        label = outcomes[0] if outcomes else "Yes"
        console.print(f"[dim]Token ({label}): {token_ids[0][:40]}...[/dim]")
        return token_ids[0], question, label

    # Multiple tokens — let user pick YES or NO
    console.print(f"\n  This market has {len(token_ids)} tokens:")
    for i, tid in enumerate(token_ids):
        label = outcomes[i] if i < len(outcomes) else f"Token {i + 1}"
        console.print(f"  [cyan]{i + 1}[/cyan]  {label}  [dim]{tid[:40]}...[/dim]")

    token_choice = IntPrompt.ask("Pick token", default=1)
    token_idx = max(0, min(token_choice - 1, len(token_ids) - 1))
    chosen_token = token_ids[token_idx]
    chosen_label = outcomes[token_idx] if token_idx < len(outcomes) else f"Token {token_idx + 1}"

    console.print(f"[dim]Using {chosen_label} token: {chosen_token[:40]}...[/dim]")

    return chosen_token, question, chosen_label


# ──────────────────────────────────────────────────────────────
# CLI ARGUMENT PARSING
# ──────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Polymarket Order Book Intelligence (OBI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                              # Interactive market selector
  python main.py --search "bitcoin"           # Search for a market
  python main.py --slug will-btc-hit-100k     # Use Polymarket URL slug
  python main.py --token <TOKEN_ID>           # Direct token ID
        """,
    )
    parser.add_argument(
        "--token", type=str, default=None,
        help="Polymarket token ID to monitor",
    )
    parser.add_argument(
        "--slug", type=str, default=None,
        help="Polymarket market URL slug",
    )
    parser.add_argument(
        "--search", type=str, default=None,
        help="Search for markets by keyword",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--mode", type=str, default="paper", choices=["paper", "dry-run", "live"],
        help="Trading mode: paper (simulated) | dry-run (sign only, no post_order) | live (real orders)",
    )
    parser.add_argument(
        "--btc5m", action="store_true",
        help="Auto-rotating BTC 5-minute Up/Down markets",
    )
    parser.add_argument(
        "--btc5m-side", type=str, default="auto", choices=["auto", "up", "down"],
        help="Which side to watch: auto (picks active side), up, or down",
    )
    parser.add_argument(
        "--pairs", action="store_true",
        help="Pair trading mode: accumulate YES+NO pairs for guaranteed profit",
    )
    return parser.parse_args()


# ──────────────────────────────────────────────────────────────
# BTC 5-MINUTE AUTO-ROTATING MODE
# ──────────────────────────────────────────────────────────────

async def run_btc5m(args):
    """
    Auto-rotating BTC 5-minute mode.

    Automatically detects the current 5-minute window,
    connects to it, and rotates to the next one when it expires.
    Keeps paper trading stats across rotations.
    """
    from execution.market_rotator import MarketRotator

    console.print("\n[bold cyan]═══ BTC 5-Minute Auto Mode ═══[/bold cyan]")
    console.print(f"[dim]Mode: {args.mode.upper()} | Config: config/strategy.conf (edit live)[/dim]")
    console.print(f"[dim]Auto-rotates every 5 minutes[/dim]\n")

    live_conf = LiveConfig()
    side = live_conf.rotation_side if args.btc5m_side == "auto" else args.btc5m_side
    rotator = MarketRotator(token_side=side)
    window = await rotator.start()

    if not window:
        console.print("[red]Could not find current BTC 5-min market. Retrying in 10s...[/red]")
        await asyncio.sleep(10)
        window = await rotator.start()

    if not window:
        console.print("[red]Still no market found. Check your internet connection.[/red]")
        return

    console.print(f"[green]Found:[/green] {window.question}")
    console.print(f"[dim]Window: {window.time_label} ({window.seconds_remaining:.0f}s remaining)[/dim]")
    console.print(f"[dim]Token: {rotator.get_active_token_id()[:40]}...[/dim]\n")

    # Run in a loop, rotating every 5 minutes
    while True:
        token_id = rotator.get_active_token_id()
        token_label = rotator.get_token_label()
        question = f"BTC Up/Down 5m — {window.time_label}"

        app = OBIApp(
            token_id=token_id,
            market_question=question,
            token_label=token_label,
            trading_mode=args.mode,
        )

        # Run until window is about to expire
        try:
            # Start components
            if app.db:
                await app.db.initialize()
            await app.telegram.start()

            app._ws_client = WebSocketClient(
                token_id=token_id,
                on_message=app._handle_message,
                on_connected=app._on_connected,
                on_disconnected=app._on_disconnected,
            )

            # Run WebSocket, UI, and key listener concurrently
            # But also run a rotation checker
            async def rotation_watchdog():
                """Watch for window expiry and cancel tasks."""
                while not rotator.should_rotate():
                    remaining = rotator.current_window.seconds_remaining if rotator.current_window else 0
                    app.ui.set_rotation_info(remaining, rotator.rotation_count)
                    await asyncio.sleep(1)
                # Time to rotate
                logger.info("Window expiring, rotating...")

            tasks = [
                asyncio.create_task(app._ws_client.start()),
                asyncio.create_task(app._run_ui()),
                asyncio.create_task(app._key_listener()),
                asyncio.create_task(rotation_watchdog()),
            ]
            if app.db:
                tasks.append(asyncio.create_task(app._update_db_stats_loop()))

            # Wait for rotation watchdog to finish (or Ctrl+C)
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except asyncio.CancelledError:
            break
        except KeyboardInterrupt:
            break
        finally:
            await app._shutdown()

        # Rotate to next window — keep trying until we find one
        console.print(f"\n[yellow]Rotating to next 5-minute window...[/yellow]")
        await asyncio.sleep(3)  # Small delay for next window to appear

        window = None
        retry_count = 0
        max_retries = 60  # Try for up to 5 minutes (60 * 5s)

        while window is None and retry_count < max_retries:
            window = await rotator.rotate()
            if window:
                break
            retry_count += 1
            wait_time = min(5 + retry_count, 15)  # 5s, then up to 15s
            console.print(
                f"[yellow]Waiting for next window... "
                f"(attempt {retry_count}/{max_retries}, "
                f"retry in {wait_time}s)[/yellow]"
            )
            await asyncio.sleep(wait_time)

        if not window:
            console.print("[red]Could not find window after 5 minutes. Retrying from scratch...[/red]")
            await asyncio.sleep(10)
            continue  # Go back to top of while loop instead of breaking

        console.print(f"[green]New window:[/green] {window.question} ({window.time_label})")

    await rotator.stop()
    console.print("\n[cyan]BTC 5m session complete.[/cyan]")


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────

async def main():
    args = parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # BTC 5-minute auto-rotating mode
    if args.btc5m:
        await run_btc5m(args)
        return

    # Pair trading mode
    if args.pairs:
        from execution.pair_runner import PairRunner
        runner = PairRunner(mode=args.mode)
        await runner.run()
        return

    token_id = None
    question = ""
    token_label = "Yes"  # Default assumption

    # Determine token ID from arguments or interactive selection
    if args.token:
        token_id = args.token
        question = "Custom Market"

    elif args.slug:
        rest = RestClient()
        market = await rest.get_market_by_slug(args.slug)
        await rest.close()
        if market:
            token_ids = market.get("clobTokenIds", [])
            if isinstance(token_ids, str):
                try:
                    token_ids = json.loads(token_ids)
                except (json.JSONDecodeError, TypeError):
                    token_ids = [token_ids]
            if token_ids:
                token_id = token_ids[0]
                question = market.get("question", args.slug)
                # First token is typically "Yes"
                outcomes = market.get("outcomes", [])
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except (json.JSONDecodeError, TypeError):
                        outcomes = []
                token_label = outcomes[0] if outcomes else "Yes"

    elif args.search:
        rest = RestClient()
        markets = await rest.search_markets(args.search)
        await rest.close()
        if markets:
            result = _display_and_pick_market(markets)
            if result:
                token_id, question, token_label = result

    # If still no token, go interactive
    if not token_id:
        if settings.TOKEN_ID:
            token_id = settings.TOKEN_ID
            question = settings.MARKET_QUESTION
        else:
            result = await select_market_interactive()
            if result:
                token_id, question, token_label = result

    if not token_id:
        console.print("[red]No market selected. Exiting.[/red]")
        return

    # Create and run the application
    app = OBIApp(token_id=token_id, market_question=question, token_label=token_label,
                 trading_mode=args.mode)
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down OBI...[/yellow]")
