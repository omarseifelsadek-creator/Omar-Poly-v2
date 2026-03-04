"""
pair_runner.py — Dual WebSocket pair trading runner.

Connects to BOTH Up and Down tokens simultaneously,
feeds real-time order book data to the PairTradingEngine,
and settles at window expiry.

This replaces the directional OBIApp for pair trading mode.
"""

import asyncio
import json
import os
import time
import logging
from typing import Optional, Union
from collections import deque

import websockets
from websockets.exceptions import ConnectionClosed

from rich.console import Console
from rich.text import Text

from config import settings
from data.message_parser import parse_messages
from data.models import BookSnapshot, PriceChangeEvent, TradeEvent, Side
from state.orderbook import OrderBook
from analytics.metrics import compute_all_metrics
from analytics.detectors import LevelTracker
from analytics.momentum import MomentumEngine
from execution.pair_strategy import PairTradingEngine, PairConfig, WindowResult, polymarket_taker_fee
from execution.pair_logger import log_pair_buy, log_window_settlement
from execution.executor import make_executor, BaseExecutor
from execution.pair_dashboard import PairDashboard, build_state
from execution.market_spec import MarketSpec, make_market_spec
from execution.market_rotator import (
    MarketRotator, MarketWindow, fetch_market_resolution, fetch_price_resolution,
)

logger = logging.getLogger(__name__)
console = Console()

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


class PairRunner:
    """
    Runs pair trading on crypto Up/Down markets.

    Manages two order books (YES/NO), feeds both to PairTradingEngine,
    and handles window rotation with settlement.

    Supports any asset/timeframe via MarketSpec.
    """

    def __init__(self, mode: str = "paper", spec: MarketSpec = None, headless: bool = False,
                 max_loss: float = None):
        self.mode = mode
        self.spec = spec or make_market_spec("btc", "5m")
        self.headless = headless
        self.max_loss = max_loss  # Kill switch: stop after losing this much

        # Two separate order books
        self.yes_book = OrderBook()
        self.no_book = OrderBook()

        # Analytics for signal filters (OBI, flow, sweeps)
        self.yes_tracker = LevelTracker()
        self.no_tracker = LevelTracker()
        self.yes_momentum = MomentumEngine()
        self.no_momentum = MomentumEngine()

        # The pair trading engine — timing params from MarketSpec + edge refinements
        config = PairConfig(
            panic_time_seconds=self.spec.panic_time_seconds,
            theta_full_size_until_s=self.spec.theta_full_size_until_s,
            theta_half_size_until_s=self.spec.theta_half_size_until_s,
            sniper_signal_min_time=self.spec.sniper_signal_min_time,
            # ── Edge refinements (v15) ──
            max_skew_pct=0.30,              # was 0.50 — tighter imbalance lock
            max_pair_cost=0.96,             # was 0.99 — only cheap pairs
            atomic_entry_max_pair=0.99,     # was 1.05 — stricter first-leg gate
            obi_delay_threshold=0.85,       # was 0.75 — allow more early fills
            flow_delay_threshold=0.75,      # was 0.60 — allow more early fills
        )
        self.engine = PairTradingEngine(config, window_duration=float(self.spec.interval_seconds))

        # Order executor — paper (no-op) or live (real CLOB orders)
        self.executor: BaseExecutor = make_executor(mode)

        # Market state
        self.window: Optional[MarketWindow] = None
        self.yes_token_id: str = ""
        self.no_token_id: str = ""

        # Latest metrics for each side
        self.yes_metrics = None
        self.no_metrics = None

        # Connection state
        self._ws = None
        self._running = False
        self._connected = False
        self._msg_count = 0

        # Cumulative stats across windows
        self.cumulative_pnl: float = 0.0
        self.windows_traded: int = 0
        self.windows_profitable: int = 0
        self.total_pairs: float = 0.0

        # Dashboard state
        self._recent_buys_display: list = []  # Last N buys for dashboard
        self._last_window_result: dict = {}   # Last settlement for session panel
        self._last_ws_time: float = 0.0       # For lag indicator
        self._dashboard = PairDashboard()
        self._live = False  # True when dashboard has terminal control
        self._market_tape = deque(maxlen=12)  # Last 12 market trades for flow tape
        self._window_volume: float = 0.0     # Total shares traded this window

        # In-memory report buffers (flushed every 12 windows)
        self._report_windows: list = []    # Window summaries for current hour
        self._report_fills: list = []      # Individual fills for current hour
        self._report_hour_start: str = ""  # HH:MM of first window in batch

        # Per-window micro stats (reset each window)
        self._capital_exhausted_time: Optional[int] = None  # elapsed s when cap hit
        self._max_unhedged_exposure: float = 0.0            # peak unhedged $ in window
        self._hedge_times: list = []                        # time-to-hedge per pair
        self._zone_counts: dict = {"Sniper": 0, "Value": 0, "Panic": 0}
        self._slippages: list = []                          # slippage in cents per fill

        # Live fill tracking — confirmed CLOB fills for current window
        self._live_fills: list[dict] = []

        # Chainlink price tracker — same source as Polymarket resolution
        self._chainlink = ChainlinkTracker(symbol=self.spec.chainlink_symbol)

        # Graceful stop: set by Ctrl+C, checked after window settlement
        self._stop_requested: bool = False

    def request_stop(self):
        """Signal this runner to stop after the current window."""
        self._stop_requested = True

    async def run(self):
        """Main entry point — rotates through 5-minute windows forever."""
        console.print("\n[bold cyan]═══ PAIR TRADING MODE ═══[/bold cyan]")
        console.print(f"[dim]Strategy: Accumulate matched YES/NO pairs < $0.96[/dim]")
        console.print(f"[dim]Mode: {self.mode.upper()} | Settlement at window expiry[/dim]\n")

        # Install graceful stop handler ONLY when running standalone (not headless).
        # In headless mode, main.py manages signal handling for all runners.
        if not self.headless:
            import signal
            def _request_stop(sig, frame):
                if not self._stop_requested:
                    self._stop_requested = True
                    console.print(
                        "\n[bold yellow]⚠  Stop requested — finishing current window then exiting...[/bold yellow]"
                    )
            signal.signal(signal.SIGINT, _request_stop)

        # Start Chainlink price stream (runs alongside order book WS)
        self._chainlink.start()

        rotator = MarketRotator(spec=self.spec, token_side="auto")
        window = await rotator.start()

        if not window:
            console.print(f"[red]Could not find {self.spec.display_name} market. Retrying...[/red]")
            await asyncio.sleep(10)
            window = await rotator.start()

        if not window:
            console.print("[red]No market found. Check connection.[/red]")
            return

        while True:
            self.window = window
            self.yes_token_id = window.up_token_id
            self.no_token_id = window.down_token_id

            console.print(f"\n[green]{'='*60}[/green]")
            console.print(f"[green]Window:[/green] {window.question}")
            console.print(f"[dim]Time: {window.time_label} ({window.seconds_remaining:.0f}s remaining)[/dim]")
            console.print(f"[dim]end_ts: {window.end_ts} (now: {int(time.time())})[/dim]")
            console.print(f"[dim]YES (Up):  {self.yes_token_id[:30]}...[/dim]")
            console.print(f"[dim]NO (Down): {self.no_token_id[:30]}...[/dim]")
            console.print(f"[green]{'='*60}[/green]\n")

            # Reset engine for new window
            self.engine.reset()
            self.engine.window_start = time.time()
            self.engine.window_duration = window.seconds_remaining
            self.yes_book = OrderBook()
            self.no_book = OrderBook()
            self.yes_tracker = LevelTracker()
            self.no_tracker = LevelTracker()
            self.yes_momentum = MomentumEngine()
            self.no_momentum = MomentumEngine()
            self.yes_metrics = None
            self.no_metrics = None
            self._msg_count = 0
            self._live_fills = []
            self._capital_exhausted_time = None
            self._max_unhedged_exposure = 0.0
            self._hedge_times = []
            self._zone_counts = {"Sniper": 0, "Value": 0, "Panic": 0}
            self._slippages = []
            self._window_volume = 0.0

            # Snapshot Chainlink price at window open
            self._chainlink.snapshot_open()

            # Pre-warm executor (TLS + tick_size + neg_risk caches)
            try:
                self.executor.warm_up([self.yes_token_id, self.no_token_id])
            except Exception as e:
                logger.warning(f"Executor warm-up failed (non-fatal): {e}")

            # Run this window
            try:
                await self._run_window()
            except asyncio.CancelledError:
                break
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Window error: {e}")

            # Settle the window (async — polls API for actual resolution)
            await self._settle_window()

            # Auto-generate report: every 12 windows for 5m, every window for slower TFs
            report_interval = 12 if self.spec.timeframe == "5m" else 1
            if self.windows_traded > 0 and self.windows_traded % report_interval == 0:
                self._auto_report()

            # Kill switch: stop if cumulative loss exceeds max_loss
            if self.max_loss and self.cumulative_pnl <= -self.max_loss:
                self._chainlink.stop()
                console.print(
                    f"\n[bold red]  KILL SWITCH — Lost ${abs(self.cumulative_pnl):.2f} "
                    f"(limit: ${self.max_loss:.0f}). Stopping.[/bold red]"
                )
                break

            # Graceful stop: Ctrl+C was pressed — exit after settlement
            if self._stop_requested:
                self._chainlink.stop()
                console.print("[bold yellow]  Graceful stop complete. Exiting.[/bold yellow]")
                break

            # Rotate to next window — NEVER give up
            console.print(f"\n[yellow]Rotating to next window...[/yellow]")
            await asyncio.sleep(3)

            window = None
            outer_attempts = 0

            while window is None:
                outer_attempts += 1

                # Inner retry loop: try 60 times (~5 minutes)
                retry = 0
                while window is None and retry < 60:
                    window = await rotator.rotate()
                    if window:
                        break
                    retry += 1
                    wait = min(5 + retry, 15)
                    console.print(f"[yellow]Waiting... (attempt {retry}/60, retry in {wait}s)[/yellow]")
                    await asyncio.sleep(wait)

                if window:
                    break

                # All 60 attempts failed — full reset
                console.print(f"[red]Rotation failed (round {outer_attempts}). Full reset...[/red]")
                await asyncio.sleep(10)

                # Kill and recreate the rotator (fresh HTTP client)
                try:
                    await rotator.stop()
                except Exception:
                    pass
                rotator = MarketRotator(spec=self.spec, token_side="auto")
                window = await rotator.start()

                if window:
                    break

                # Still nothing — wait longer before next round
                backoff = min(30 * outer_attempts, 120)
                console.print(f"[red]Still no window. Waiting {backoff}s before retry round {outer_attempts + 1}...[/red]")
                await asyncio.sleep(backoff)

        await rotator.stop()
        self._print_session_summary()

    async def _run_window(self):
        """Run a single 5-minute window with dual WebSocket."""
        self._running = True
        uri = settings.CLOB_WS_URL
        reconnect_count = 0

        # Start dashboard ONCE for the entire window (skip in headless mode)
        if self.headless:
            self._live = False
        else:
            try:
                self._dashboard.start()
                self._live = True
            except Exception as e:
                import sys, traceback as tb_mod
                sys.stdout.write(f"\033[91m═══ DASHBOARD START CRASH ═══\n{tb_mod.format_exc()}\033[0m\n")
                sys.stdout.flush()
                self._live = False

        # Start UI + watchdog tasks that persist across WS reconnects
        ui_task = asyncio.create_task(self._ui_loop())
        watchdog_task = asyncio.create_task(self._rotation_watchdog())

        try:
            while self._running and self.engine.time_remaining > 5:
                try:
                    async with websockets.connect(
                        uri,
                        ping_interval=20,
                        ping_timeout=10,
                        close_timeout=5,
                    ) as ws:
                        self._ws = ws
                        self._connected = True

                        if reconnect_count > 0:
                            logger.info(f"WebSocket reconnected (attempt {reconnect_count})")

                        # Subscribe to BOTH tokens
                        subscribe_msg = json.dumps({
                            "type": "market",
                            "assets_ids": [self.yes_token_id, self.no_token_id],
                        })
                        await ws.send(subscribe_msg)
                        logger.info(f"Subscribed to YES + NO tokens")

                        # Run message loop until WS drops or window ends
                        msg_task = asyncio.create_task(self._message_loop(ws))

                        # Wait for ANY of: message loop done, watchdog done, ui done
                        combined = {msg_task, ui_task, watchdog_task}
                        done, pending = await asyncio.wait(
                            combined, return_when=asyncio.FIRST_COMPLETED
                        )

                        # Only cancel the message task (UI + watchdog persist)
                        if msg_task in pending:
                            msg_task.cancel()
                            try:
                                await msg_task
                            except asyncio.CancelledError:
                                pass

                        # Retrieve exceptions from done tasks
                        for task in done:
                            try:
                                task.result()
                            except ConnectionClosed:
                                pass
                            except asyncio.CancelledError:
                                pass
                            except Exception as e:
                                logger.debug(f"Task ended with: {e}")

                        # If watchdog finished, window is done
                        if not self._running:
                            break

                        # If WS message loop ended (dropped), reconnect
                        if msg_task in done:
                            reconnect_count += 1
                            if self.engine.time_remaining > 10:
                                logger.info(f"WS dropped, reconnecting ({self.engine.time_remaining:.0f}s left)")
                                await asyncio.sleep(1)
                                continue
                            else:
                                break

                except ConnectionClosed:
                    reconnect_count += 1
                    if self.engine.time_remaining > 10:
                        logger.info(f"WS connection closed, reconnecting ({self.engine.time_remaining:.0f}s left)")
                        await asyncio.sleep(1)
                    else:
                        break
                except Exception as e:
                    reconnect_count += 1
                    logger.error(f"WebSocket error: {e}")
                    if self.engine.time_remaining > 10:
                        await asyncio.sleep(2)
                    else:
                        break

        finally:
            # Cancel UI + watchdog
            for task in [ui_task, watchdog_task]:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            # Stop dashboard ONCE when window ends
            if self._live:
                try:
                    self._dashboard.stop()
                except Exception:
                    pass
                self._live = False

            self._connected = False

    async def _message_loop(self, ws):
        """Process WebSocket messages and route to correct order book."""
        try:
            async for raw_message in ws:
                if not self._running:
                    break

                self._msg_count += 1
                self._last_ws_time = time.time()

                for parsed in parse_messages(raw_message):
                    # Route message to correct order book based on asset_id
                    asset_id = getattr(parsed, "asset_id", "")

                    if isinstance(parsed, PriceChangeEvent):
                        # Check which books need updating — a single event
                        # can contain changes for BOTH tokens
                        has_yes = any(c.asset_id == self.yes_token_id for c in parsed.price_changes)
                        has_no = any(c.asset_id == self.no_token_id for c in parsed.price_changes)
                        if has_yes:
                            self._handle_price_change_for(
                                parsed, self.yes_book, self.yes_tracker, "YES"
                            )
                        if has_no:
                            self._handle_price_change_for(
                                parsed, self.no_book, self.no_tracker, "NO"
                            )

                    elif isinstance(parsed, BookSnapshot):
                        if asset_id == self.yes_token_id:
                            self.yes_book.apply_snapshot(parsed)
                        elif asset_id == self.no_token_id:
                            self.no_book.apply_snapshot(parsed)

                    elif isinstance(parsed, TradeEvent):
                        self._window_volume += parsed.size
                        if asset_id == self.yes_token_id:
                            self.yes_book.apply_trade(parsed)
                            self.yes_momentum.update(trade_price=parsed.price)
                            self._market_tape.append({
                                "time": parsed.timestamp_ms / 1000,
                                "token": "YES",
                                "size": parsed.size,
                                "price": parsed.price,
                                "side": parsed.side.value,
                            })
                        elif asset_id == self.no_token_id:
                            self.no_book.apply_trade(parsed)
                            self.no_momentum.update(trade_price=parsed.price)
                            self._market_tape.append({
                                "time": parsed.timestamp_ms / 1000,
                                "token": "NO",
                                "size": parsed.size,
                                "price": parsed.price,
                                "side": parsed.side.value,
                            })

                    # After each update, try to evaluate pair opportunity
                    await self._try_evaluate()

        except ConnectionClosed:
            # Expected — WS dropped, outer loop will reconnect
            logger.info("WebSocket closed in message loop — will reconnect")

    def _handle_price_change_for(self, msg, book, tracker, label):
        """Apply price change to specific book."""
        book.apply_price_change(msg)
        for change in msg.price_changes:
            tracker.record_change(
                change.price, change.side, change.size, msg.timestamp_ms
            )

    async def _try_evaluate(self):
        """Evaluate pair trading opportunity with current state."""
        # Need both books initialized
        if not self.yes_book.is_initialized or not self.no_book.is_initialized:
            if self._msg_count % 500 == 1:
                logger.warning(
                    f"[DIAG] Books not init: YES={self.yes_book.is_initialized} "
                    f"(bids={len(self.yes_book._bids)}, asks={len(self.yes_book._asks)}) "
                    f"NO={self.no_book.is_initialized} "
                    f"(bids={len(self.no_book._bids)}, asks={len(self.no_book._asks)}) "
                    f"msgs={self._msg_count}"
                )
            return

        yes_ask = self.yes_book.best_ask
        no_ask = self.no_book.best_ask
        yes_bid = self.yes_book.best_bid
        no_bid = self.no_book.best_bid

        # ── Periodic diagnostic: log what the engine sees ──
        if self._msg_count % 500 == 0:
            yes_asks_top = self.yes_book.get_sorted_asks(max_levels=3)
            no_asks_top = self.no_book.get_sorted_asks(max_levels=3)
            logger.warning(
                f"[DIAG] YES ask=${yes_ask} bid=${yes_bid} "
                f"NO ask=${no_ask} bid=${no_bid} | "
                f"YES asks={[(l.price, l.size) for l in yes_asks_top]} | "
                f"NO asks={[(l.price, l.size) for l in no_asks_top]} | "
                f"pair_cost=${(yes_ask or 0)+(no_ask or 0):.4f} | "
                f"T-{self.engine.time_remaining:.0f}s | msgs={self._msg_count}"
            )

        if not yes_ask or not no_ask:
            return

        # ── Update ask age tracking (for latency model) ──
        self.engine.update_ask_age("YES", yes_ask)
        self.engine.update_ask_age("NO", no_ask)

        # ── Multi-level ask depth for VWAP book walking ──
        yes_ask_levels = self.yes_book.get_sorted_asks(max_levels=5)
        no_ask_levels = self.no_book.get_sorted_asks(max_levels=5)

        # ── Bid depth at best bid for maker queue simulation ──
        yes_bids = self.yes_book.get_sorted_bids(max_levels=1)
        no_bids = self.no_book.get_sorted_bids(max_levels=1)
        yes_bid_depth = yes_bids[0].size if yes_bids else 0.0
        no_bid_depth = no_bids[0].size if no_bids else 0.0

        # Compute metrics for signal filters
        try:
            self.yes_metrics = compute_all_metrics(
                self.yes_book, self.yes_tracker, self.yes_momentum
            )
            self.no_metrics = compute_all_metrics(
                self.no_book, self.no_tracker, self.no_momentum
            )
        except Exception:
            pass

        # Get OBI and flow for filtering
        obi = self.yes_metrics.obi if self.yes_metrics and self.yes_metrics.obi is not None else 0.5
        flow = self.yes_metrics.flow_pressure if self.yes_metrics else 0.0

        # Detect sweeps
        has_sweep = False
        sweep_side = ""
        if self.yes_metrics and self.yes_metrics.sweep_events:
            has_sweep = True
            sweep_side = "YES"
        if self.no_metrics and self.no_metrics.sweep_events:
            has_sweep = True
            sweep_side = "NO"

        # Cap unmatched exposure at $30 — prevent runaway one-sided positions
        unmatched_usd = abs(self.engine.yes_cost - self.engine.no_cost)
        if unmatched_usd > 30.0:
            return

        # Snapshot engine state BEFORE evaluate() so LiveExecutor can roll
        # back if the real CLOB order fails. PaperExecutor returns {} here.
        snapshot = self.executor.pre_snapshot(self.engine)

        # Run the pair engine evaluation
        action = self.engine.evaluate(
            yes_ask=yes_ask,
            no_ask=no_ask,
            yes_bid=yes_bid,
            no_bid=no_bid,
            yes_ask_levels=yes_ask_levels,
            no_ask_levels=no_ask_levels,
            yes_bid_depth=yes_bid_depth,
            no_bid_depth=no_bid_depth,
            obi=obi,
            flow_pressure=flow,
            has_sweep=has_sweep,
            sweep_side=sweep_side,
        )

        if action:
            # Resolve the token ID for this leg (YES or NO side)
            buy_token_id = (
                self.yes_token_id if action["side"] == "YES" else self.no_token_id
            )

            # Execute — paper: instant pass-through, live: real FOK order.
            # On live failure the engine state is restored and None is returned.
            action = await self.executor.execute(
                action, self.engine, snapshot, buy_token_id
            )
            if action is None:
                return   # live order rejected — engine state already rolled back

            # Track confirmed live fill for live PnL
            if action.get("mode") == "LIVE":
                self._live_fills.append({
                    "side": action["side"],
                    "qty": float(action["qty"]),
                    "price": float(action["vwap_price"]),
                    "cost": float(action["cost"]),
                })

            # ── Compute quant context for logging ──────────────────
            raw = action.get("raw_price", 0)
            t_rem = self.engine.time_remaining
            if t_rem < self.engine.config.panic_time_seconds:
                zone = "Panic"
            elif raw <= 0.35:
                zone = "Sniper"
            elif raw <= 0.44:
                zone = "Value"
            else:
                zone = "Panic"
            self._zone_counts[zone] = self._zone_counts.get(zone, 0) + 1

            fill_side = action.get("side", "")
            opp_side = "NO" if fill_side == "YES" else "YES"
            opposite_ask = no_ask if fill_side == "YES" else yes_ask
            best_bid = yes_bid if fill_side == "YES" else no_bid
            spread = (action.get("raw_price", 0) - best_bid) if best_bid else 0

            # Time-to-Hedge: seconds from earliest opposite leg to this fill
            opp_legs = [l for l in self.engine.legs[:-1] if l.side == opp_side]
            time_to_hedge = (
                round(time.time() - opp_legs[0].timestamp, 1) if opp_legs else None
            )
            if time_to_hedge is not None:
                self._hedge_times.append(time_to_hedge)

            # Capital exhaustion: first moment total deployed >= cap
            if (self._capital_exhausted_time is None and
                    self.engine.total_capital >= self.engine.config.max_position_usd):
                self._capital_exhausted_time = int(
                    self.engine.window_duration - self.engine.time_remaining
                )

            # Max unhedged exposure: peak $ held in a single unbalanced leg
            y_qty = self.engine.yes_qty
            n_qty = self.engine.no_qty
            if y_qty > n_qty and y_qty > 0:
                unhedged = (y_qty - n_qty) * (self.engine.yes_cost / y_qty)
            elif n_qty > y_qty and n_qty > 0:
                unhedged = (n_qty - y_qty) * (self.engine.no_cost / n_qty)
            else:
                unhedged = 0.0
            self._max_unhedged_exposure = max(self._max_unhedged_exposure, unhedged)

            # Slippage tracking
            vwap = action.get("vwap_price", raw)
            slippage_cents = round((vwap - raw) * 100, 2) if raw else 0
            self._slippages.append(slippage_cents)

            # Build context dict for CSV logging
            fill_ctx = {
                "zone": zone,
                "obi": obi,
                "flow_pressure": flow,
                "has_sweep": has_sweep,
                "sweep_side": sweep_side,
                "opposite_ask": opposite_ask or 0,
                "best_bid": best_bid or 0,
                "spread": spread,
                "yes_bid_depth": self.yes_book.total_bid_depth,
                "yes_ask_depth": self.yes_book.total_ask_depth,
                "no_bid_depth": self.no_book.total_bid_depth,
                "no_ask_depth": self.no_book.total_ask_depth,
                "time_to_hedge_s": time_to_hedge if time_to_hedge is not None else "N/A",
                "unhedged_usd": unhedged,
            }

            # Log the buy with full quant context
            market_label = f"{self.spec.display_name_long} — {self.window.time_label}" if self.window else self.spec.display_name
            fee_pct = polymarket_taker_fee(action["raw_price"]) * 100
            action["fee_pct"] = fee_pct

            log_pair_buy(market_label, action, self.engine.get_stats(),
                         ctx=fill_ctx, mode=action.get("mode", "PAPER"))

            # Track for dashboard (keep last 10)
            action["timestamp"] = time.time()
            self._recent_buys_display.append(action)
            if len(self._recent_buys_display) > 10:
                self._recent_buys_display = self._recent_buys_display[-10:]

            # Track for hourly report
            self._report_fills.append({
                "timestamp": time.strftime("%H:%M:%S"),
                "window_id": self.window.time_label if self.window else "",
                "token": fill_side,
                "shares": action.get("qty", 0),
                "zone": zone,
                "quoted_ask": raw,
                "vwap_fill": action.get("vwap_price", raw),
                "fee_pct": f"{action.get('fee_pct', 0):.2f}%",
                "opposite_leg_ask": f"{opposite_ask:.4f}" if opposite_ask else "N/A",
                "ask_age_ms": round(action.get("ask_age_ms", 0)),
                "obi_ratio": round(obi, 3),
                "time_to_hedge_sec": time_to_hedge if time_to_hedge is not None else "N/A",
            })

            # Console output (only when dashboard is not active)
            if not self._live:
                side = action["side"]
                color = "green" if side == "YES" else "red"
                snipe = " 🎯" if action.get("is_snipe") else ""
                vwap = action.get("vwap_price", action.get("fill_price", 0))
                console.print(
                    f"  [{color}]BUY {action['qty']:.0f} {side}[/{color}] "
                    f"@ VWAP ${vwap:.3f} "
                    f"(fill: ${action['fill_price']:.4f}) | "
                    f"Pairs: {self.engine.matched_pairs:.0f} "
                    f"Cost: ${self.engine.pair_cost:.4f}"
                )

    async def _rotation_watchdog(self):
        """Watch for window expiry."""
        while self._running:
            remaining = self.engine.time_remaining
            if remaining <= 0:
                logger.info("Window expired — settling")
                self._running = False
                return
            await asyncio.sleep(0.5)

    async def _ui_loop(self):
        """Render dashboard at 2 FPS using ANSI cursor control."""
        import traceback as tb_module

        if self._live:
            # Full dashboard mode
            while self._running:
                await asyncio.sleep(0.5)
                if not self._running:
                    break
                try:
                    state = build_state(self)
                    self._dashboard.render(state)
                except Exception as e:
                    # PANIC HATCH: clear screen and show error in red
                    import sys
                    sys.stdout.write("\033[H\033[2J")  # Clear screen
                    sys.stdout.write("\033[91m")        # Red text
                    sys.stdout.write("═══ DASHBOARD CRASH ═══\n\n")
                    sys.stdout.write(tb_module.format_exc())
                    sys.stdout.write("\n\033[93m")      # Yellow text
                    sys.stdout.write("Falling back to text mode...\n")
                    sys.stdout.write("\033[0m")          # Reset color
                    sys.stdout.flush()
                    logger.error(f"Dashboard render crash: {e}", exc_info=True)
                    # Exit dashboard mode, fall through to text
                    try:
                        self._dashboard.stop()
                    except Exception:
                        pass
                    self._live = False
                    await asyncio.sleep(3)
                    break

        # Text fallback (also reached if dashboard crashes above, or headless)
        tag = f"[{self.spec.asset.upper()}/{self.spec.timeframe}]" if self.headless else ""
        while self._running:
            await asyncio.sleep(3)
            if not self._running:
                break
            try:
                stats = self.engine.get_stats()
                console.print(
                    f"  {tag} [dim]T-{stats['time_remaining']:.0f}s[/dim] | "
                    f"Pairs: {stats['matched_pairs']:.0f} | "
                    f"PairCost: ${stats['pair_cost']:.4f} | "
                    f"Msgs: {self._msg_count}"
                )
            except Exception:
                pass

    def _compute_live_settlement(self, winner: str) -> WindowResult:
        """Compute settlement PnL from confirmed live CLOB fills."""
        yes_fills = [f for f in self._live_fills if f["side"] == "YES"]
        no_fills = [f for f in self._live_fills if f["side"] == "NO"]

        yes_qty = sum(f["qty"] for f in yes_fills)
        no_qty = sum(f["qty"] for f in no_fills)
        yes_cost = sum(f["cost"] for f in yes_fills)
        no_cost = sum(f["cost"] for f in no_fills)
        total_capital = yes_cost + no_cost

        yes_avg = yes_cost / yes_qty if yes_qty > 0 else 0
        no_avg = no_cost / no_qty if no_qty > 0 else 0

        matched = min(yes_qty, no_qty)
        avg_pair_cost = (yes_avg + no_avg) if matched > 0 else 0
        pair_profit = matched * (1.0 - avg_pair_cost) if matched > 0 else 0

        unmatched_qty = abs(yes_qty - no_qty)
        if yes_qty > no_qty:
            unmatched_side = "YES"
        elif no_qty > yes_qty:
            unmatched_side = "NO"
        else:
            unmatched_side = "NONE"

        winning_payout = (yes_qty if winner == "YES" else no_qty) * 1.0
        net_pnl = winning_payout - total_capital
        gamble_result = net_pnl - pair_profit

        return WindowResult(
            yes_qty=yes_qty, no_qty=no_qty,
            yes_avg_cost=yes_avg, no_avg_cost=no_avg,
            total_cost=total_capital,
            matched_pairs=matched, unmatched_qty=unmatched_qty,
            unmatched_side=unmatched_side, winner=winner,
            pair_profit=pair_profit, gamble_result=gamble_result,
            net_pnl=net_pnl, avg_pair_cost=avg_pair_cost,
            num_buys=len(self._live_fills),
        )

    async def _settle_window(self):
        """Settle the current window — fetch actual resolution from Polymarket API."""
        if self.engine.yes_qty == 0 and self.engine.no_qty == 0:
            console.print("[dim]  No positions to settle.[/dim]")
            return

        console.print("[yellow]  Resolving market...[/yellow]")

        winner = None

        # Method 1: Chainlink — exact same source as Polymarket resolution
        if self._chainlink.latest_price and self._chainlink.window_open_price:
            winner = self._chainlink.resolve()
            if winner:
                console.print(f"[green]  Resolution from Chainlink: {winner} won[/green]")

        # Method 2: Binance candle (fallback if Chainlink stream disconnected)
        if winner is None and self.window:
            console.print("[yellow]  Chainlink unavailable, checking Binance...[/yellow]")
            try:
                winner = await fetch_price_resolution(
                    self.window.start_ts, self.window.end_ts,
                    spec=self.spec,
                )
                if winner:
                    console.print(f"[green]  Resolution from Binance: {winner} won[/green]")
            except Exception as e:
                logger.warning(f"Binance resolution error: {e}")

        # Method 3: Poll the Gamma API (last resort)
        if winner is None and self.window:
            console.print("[yellow]  Price feeds unavailable, polling Gamma API...[/yellow]")
            try:
                winner = await fetch_market_resolution(
                    market_slug=self.window.event_slug,
                    up_token_id=self.yes_token_id,
                    down_token_id=self.no_token_id,
                    max_wait=90.0,
                    poll_interval=5.0,
                )
                if winner:
                    console.print(f"[green]  Resolution from Gamma API: {winner} won[/green]")
            except Exception as e:
                logger.error(f"Resolution fetch error: {e}")

        # Method 4: Fallback to order book snapshot (emergency only)
        if winner is None:
            yes_mid = self.yes_book.midpoint
            yes_ask = self.yes_book.best_ask
            no_ask = self.no_book.best_ask
            winner = self.engine.determine_winner(yes_mid, yes_ask)
            console.print(
                f"[red]  WARNING: All resolution methods failed — guessing from book: "
                f"{winner} (YES mid={yes_mid}, ask={yes_ask}, NO ask={no_ask})[/red]"
            )

        # Run settlement — always settle the paper engine to keep it clean
        paper_result = self.engine.settle(winner)

        # Use live fills for PnL when in LIVE mode, paper engine otherwise
        if self.mode == "live" and self._live_fills:
            result = self._compute_live_settlement(winner)
            logger.warning(
                f"[LIVE SETTLE] Live PnL: ${result.net_pnl:+.2f} | "
                f"Paper PnL: ${paper_result.net_pnl:+.2f} | "
                f"Delta: ${result.net_pnl - paper_result.net_pnl:+.2f}"
            )
        else:
            result = paper_result

        # Update cumulative stats
        self.cumulative_pnl += result.net_pnl
        self.windows_traded += 1
        if result.net_pnl > 0:
            self.windows_profitable += 1
        self.total_pairs += result.matched_pairs

        # Track for dashboard
        self._last_window_result = {
            "net_pnl": result.net_pnl,
            "winner": winner,
            "matched_pairs": result.matched_pairs,
            "pair_cost": result.avg_pair_cost,
        }
        self._recent_buys_display = []  # Reset buys for new window

        # Log to CSV with micro-stats
        market_label = f"{self.spec.display_name_long} — {self.window.time_label}" if self.window else self.spec.display_name
        stats = self.engine.get_stats()
        wctx = {
            "fills_attempted": stats.get("fills_attempted", 0),
            "fills_rejected": stats.get("fills_rejected", 0),
            "dead_zone_blocks": stats.get("filter_reasons", {}).get("dead_zone_nuked", 0),
            "cap_exhausted_at_s": (
                self._capital_exhausted_time
                if self._capital_exhausted_time is not None else "N/A"
            ),
            "max_unhedged_usd": self._max_unhedged_exposure,
            "avg_hedge_time_s": (
                f"{sum(self._hedge_times) / len(self._hedge_times):.1f}"
                if self._hedge_times else "N/A"
            ),
            "sniper_fills": self._zone_counts.get("Sniper", 0),
            "value_fills": self._zone_counts.get("Value", 0),
            "panic_fills": self._zone_counts.get("Panic", 0),
            "avg_slippage_cents": (
                f"{sum(self._slippages) / len(self._slippages):+.2f}"
                if self._slippages else "N/A"
            ),
        }
        log_window_settlement(market_label, result, self.cumulative_pnl,
                              wctx=wctx, mode=self.mode.upper())

        # Track for hourly report
        win_start = ""
        win_end = ""
        if self.window:
            tl = self.window.time_label  # "HH:MM-HH:MM UTC"
            parts = tl.replace(" UTC", "").split("-")
            if len(parts) == 2:
                win_start = parts[0].strip()
                win_end = parts[1].strip()

        if not self._report_hour_start:
            self._report_hour_start = win_start

        stats = self.engine.get_stats()
        self._report_windows.append({
            "window_start_time": win_start,
            "window_end_time": win_end,
            "window_id": self.window.time_label if self.window else "",
            "winner": winner,
            "completed_pairs": str(result.matched_pairs),
            "avg_pair_cost": str(result.avg_pair_cost),
            "total_capital": str(result.total_cost),
            "net_pnl": str(result.net_pnl),
            "fills_attempted": str(stats.get("fills_attempted", 0)),
            "fills_rejected": str(stats.get("fills_rejected", 0)),
            "dead_zone_blocks": str(stats.get("filter_reasons", {}).get("dead_zone_nuked", 0)),
            "capital_exhaustion_time": (
                str(self._capital_exhausted_time)
                if self._capital_exhausted_time is not None else "N/A"
            ),
            "max_unhedged_exposure": f"{self._max_unhedged_exposure:.2f}",
            "avg_time_to_hedge": (
                f"{sum(self._hedge_times) / len(self._hedge_times):.1f}"
                if self._hedge_times else "N/A"
            ),
        })

        # Print settlement report
        pnl_color = "green" if result.net_pnl >= 0 else "red"
        pair_color = "green" if result.pair_profit >= 0 else "red"
        gamble_color = "green" if result.gamble_result >= 0 else "red"

        console.print(f"\n[bold cyan]{'═'*60}[/bold cyan]")
        console.print(f"[bold cyan]  [{self.spec.display_name}] SETTLEMENT — {winner} Won[/bold cyan]")
        console.print(f"[bold cyan]{'═'*60}[/bold cyan]")
        console.print(f"  YES Shares:     {result.yes_qty:.0f} @ avg ${result.yes_avg_cost:.4f}")
        console.print(f"  NO Shares:      {result.no_qty:.0f} @ avg ${result.no_avg_cost:.4f}")
        console.print(f"  Completed Pairs: {result.matched_pairs:.0f}")
        console.print(f"  Unmatched:      {result.unmatched_qty:.0f} {result.unmatched_side}")
        console.print(f"  Avg Pair Cost:  ${result.avg_pair_cost:.4f}")
        console.print(f"  Capital Used:   ${result.total_cost:.2f}")
        console.print(f"  ─────────────────────────────────")
        console.print(f"  Pair Profit:    [{pair_color}]${result.pair_profit:+.2f}[/{pair_color}]")
        console.print(f"  Gamble Result:  [{gamble_color}]${result.gamble_result:+.2f}[/{gamble_color}]")
        console.print(f"  [bold]Net PnL:        [{pnl_color}]${result.net_pnl:+.2f}[/{pnl_color}][/bold]")
        console.print(f"  ─────────────────────────────────")
        console.print(f"  Session PnL:    [{('green' if self.cumulative_pnl >= 0 else 'red')}]${self.cumulative_pnl:+.2f}[/]")
        console.print(f"  Windows: {self.windows_traded} ({self.windows_profitable} profitable)")
        console.print(f"  Total Pairs: {self.total_pairs:.0f}")
        console.print(f"  Buys: {result.num_buys} executed, {self.engine.buys_filtered} filtered")

        # Filter breakdown
        if self.engine.filter_reasons:
            top_filters = sorted(
                self.engine.filter_reasons.items(),
                key=lambda x: x[1], reverse=True
            )[:5]
            filter_str = ", ".join(f"{k}:{v}" for k, v in top_filters)
            console.print(f"  Filters: {filter_str}")

        console.print(f"[bold cyan]{'═'*60}[/bold cyan]\n")

    def _auto_report(self):
        """Auto-generate Excel report every 12 windows with smart naming + memory cleanup."""
        try:
            from generate_pair_report import generate_from_memory

            if not self._report_windows:
                console.print("[yellow]  Auto-report: no window data buffered.[/yellow]")
                return

            # Smart file naming: PolyQuant_{Asset}{TF}_{Date}_{Start}_to_{End}.xlsx
            date_str = time.strftime("%Y-%m-%d")
            hour_start = self._report_hour_start or "0000"
            hour_start_clean = hour_start.replace(":", "")

            # End time from last window
            last_win = self._report_windows[-1]
            hour_end = last_win.get("window_end_time", "")
            hour_end_clean = hour_end.replace(":", "") if hour_end else time.strftime("%H%M")

            filename = f"PolyQuant_{self.spec.asset}{self.spec.timeframe}_{date_str}_{hour_start_clean}_to_{hour_end_clean}.xlsx"
            filepath = os.path.join("data/logs", filename)

            path = generate_from_memory(
                window_records=self._report_windows,
                fill_records=self._report_fills,
                session_pnl=self.cumulative_pnl,
                output_path=filepath,
            )

            if path:
                n_wins = len(self._report_windows)
                n_fills = len(self._report_fills)
                report_pnl = sum(
                    float(w.get('net_pnl', 0))
                    for w in self._report_windows
                )
                wr = self.windows_profitable / self.windows_traded * 100 if self.windows_traded > 0 else 0

                console.print(f"\n[bold magenta]{'─'*60}[/bold magenta]")
                console.print(
                    f"[bold magenta]  📊 HOURLY REPORT (Window #{self.windows_traded})[/bold magenta]"
                )
                console.print(f"[magenta]  Saved: {path}[/magenta]")
                console.print(
                    f"[magenta]  Report: {n_wins} windows, {n_fills} fills, "
                    f"PnL: ${report_pnl:+.2f}[/magenta]"
                )
                console.print(
                    f"[magenta]  Session: ${self.cumulative_pnl:+.2f} | "
                    f"Win Rate: {wr:.0f}% | "
                    f"Pairs: {self.total_pairs:.0f}[/magenta]"
                )
                console.print(f"[bold magenta]{'─'*60}[/bold magenta]\n")

                # Memory cleanup: flush report buffers but keep session stats
                self._report_windows.clear()
                self._report_fills.clear()
                self._report_hour_start = ""

            else:
                console.print("[yellow]  Auto-report: generation returned empty.[/yellow]")
        except Exception as e:
            console.print(f"[red]  Auto-report failed: {e}[/red]")
            logger.error(f"Auto-report error: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _print_session_summary(self):
        """Print final session summary and generate report."""
        pnl_color = "green" if self.cumulative_pnl >= 0 else "red"
        wr = self.windows_profitable / self.windows_traded * 100 if self.windows_traded > 0 else 0

        # Generate final report from any remaining buffered data
        if self._report_windows:
            self._auto_report()
        elif self.windows_traded > 0:
            # Fallback: generate from CSVs if buffer was already flushed
            try:
                from generate_pair_report import generate_pair_report
                generate_pair_report()
            except Exception:
                pass

        console.print(f"\n[bold cyan]{'═'*60}[/bold cyan]")
        console.print(f"[bold cyan]  PAIR TRADING SESSION COMPLETE[/bold cyan]")
        console.print(f"[bold cyan]{'═'*60}[/bold cyan]")
        console.print(f"  Windows Traded:   {self.windows_traded}")
        console.print(f"  Windows Won:      {self.windows_profitable} ({wr:.0f}%)")
        console.print(f"  Total Pairs:      {self.total_pairs:.0f}")
        console.print(f"  [bold]Session PnL:    [{pnl_color}]${self.cumulative_pnl:+.2f}[/{pnl_color}][/bold]")
        console.print(f"[bold cyan]{'═'*60}[/bold cyan]\n")
