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
from data.message_parser import parse_message
from data.models import BookSnapshot, PriceChangeEvent, TradeEvent, Side
from state.orderbook import OrderBook
from analytics.metrics import compute_all_metrics
from analytics.detectors import LevelTracker
from analytics.momentum import MomentumEngine
from execution.pair_strategy import PairTradingEngine, PairConfig, polymarket_taker_fee
from execution.pair_logger import log_pair_buy, log_window_settlement
from execution.executor import make_executor, BaseExecutor
from execution.pair_dashboard import PairDashboard, build_state
from execution.market_rotator import MarketRotator, MarketWindow, fetch_market_resolution

logger = logging.getLogger(__name__)
console = Console()


class PairRunner:
    """
    Runs pair trading on BTC 5-minute markets.

    Manages two order books (YES/NO), feeds both to PairTradingEngine,
    and handles window rotation with settlement.
    """

    def __init__(self, mode: str = "paper"):
        self.mode = mode

        # Two separate order books
        self.yes_book = OrderBook()
        self.no_book = OrderBook()

        # Analytics for signal filters (OBI, flow, sweeps)
        self.yes_tracker = LevelTracker()
        self.no_tracker = LevelTracker()
        self.yes_momentum = MomentumEngine()
        self.no_momentum = MomentumEngine()

        # The pair trading engine
        self.engine = PairTradingEngine(PairConfig())

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

        # In-memory report buffers (flushed every 12 windows)
        self._report_windows: list = []    # Window summaries for current hour
        self._report_fills: list = []      # Individual fills for current hour
        self._report_hour_start: str = ""  # HH:MM of first window in batch

        # Per-window micro stats (reset each window)
        self._capital_exhausted_time: Optional[int] = None  # elapsed s when cap hit
        self._max_unhedged_exposure: float = 0.0            # peak unhedged $ in window
        self._hedge_times: list = []                        # time-to-hedge per pair

    async def run(self):
        """Main entry point — rotates through 5-minute windows forever."""
        console.print("\n[bold cyan]═══ PAIR TRADING MODE ═══[/bold cyan]")
        console.print(f"[dim]Strategy: Accumulate matched YES/NO pairs < $0.96[/dim]")
        console.print(f"[dim]Mode: {self.mode.upper()} | Settlement at window expiry[/dim]\n")

        rotator = MarketRotator(token_side="auto")
        window = await rotator.start()

        if not window:
            console.print("[red]Could not find BTC 5-min market. Retrying...[/red]")
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
            self._capital_exhausted_time = None
            self._max_unhedged_exposure = 0.0
            self._hedge_times = []

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

            # Auto-generate report every 12 windows
            if self.windows_traded > 0 and self.windows_traded % 12 == 0:
                self._auto_report()

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
                rotator = MarketRotator(token_side="auto")
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

        # Start dashboard ONCE for the entire window
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
                parsed = parse_message(raw_message)
                if parsed is None:
                    continue

                # Route message to correct order book based on asset_id
                asset_id = getattr(parsed, "asset_id", "")

                # For PriceChangeEvent, asset_id is in the changes
                if isinstance(parsed, PriceChangeEvent):
                    for change in parsed.price_changes:
                        if change.asset_id == self.yes_token_id:
                            self._handle_price_change_for(
                                parsed, self.yes_book, self.yes_tracker, "YES"
                            )
                            break
                        elif change.asset_id == self.no_token_id:
                            self._handle_price_change_for(
                                parsed, self.no_book, self.no_tracker, "NO"
                            )
                            break

                elif isinstance(parsed, BookSnapshot):
                    if asset_id == self.yes_token_id:
                        self.yes_book.apply_snapshot(parsed)
                    elif asset_id == self.no_token_id:
                        self.no_book.apply_snapshot(parsed)

                elif isinstance(parsed, TradeEvent):
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

                # After update, try to evaluate pair opportunity
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
            return

        yes_ask = self.yes_book.best_ask
        no_ask = self.no_book.best_ask
        yes_bid = self.yes_book.best_bid
        no_bid = self.no_book.best_bid

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

            # Log the buy
            market_label = f"BTC Up/Down 5m — {self.window.time_label}" if self.window else "BTC 5m"
            fee_pct = polymarket_taker_fee(action["raw_price"]) * 100
            action["fee_pct"] = fee_pct

            log_pair_buy(market_label, action, self.engine.get_stats(),
                         mode=action.get("mode", "PAPER"))

            # Track for dashboard (keep last 10)
            action["timestamp"] = time.time()
            self._recent_buys_display.append(action)
            if len(self._recent_buys_display) > 10:
                self._recent_buys_display = self._recent_buys_display[-10:]

            # Track for hourly report
            raw = action.get("raw_price", 0)
            t_rem = self.engine.time_remaining
            if t_rem < 60:
                zone = "Panic"
            elif raw <= 0.35:
                zone = "Sniper"
            elif raw <= 0.44:
                zone = "Value"
            else:
                zone = "Panic"

            # ── Micro metrics ─────────────────────────────────────────
            fill_side = action.get("side", "")
            opp_side = "NO" if fill_side == "YES" else "YES"

            # Opposite leg best ask at execution moment
            opposite_ask = no_ask if fill_side == "YES" else yes_ask

            # Time-to-Hedge: seconds from earliest opposite leg to this fill.
            # engine.legs[-1] is the fill just committed; search earlier legs.
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

        # Text fallback (also reached if dashboard crashes above)
        while self._running:
            await asyncio.sleep(3)
            if not self._running:
                break
            try:
                stats = self.engine.get_stats()
                console.print(
                    f"  [dim]T-{stats['time_remaining']:.0f}s[/dim] | "
                    f"Pairs: {stats['matched_pairs']:.0f} | "
                    f"PairCost: ${stats['pair_cost']:.4f} | "
                    f"Msgs: {self._msg_count}"
                )
            except Exception:
                pass

    async def _settle_window(self):
        """Settle the current window — fetch actual resolution from Polymarket API."""
        if self.engine.yes_qty == 0 and self.engine.no_qty == 0:
            console.print("[dim]  No positions to settle.[/dim]")
            return

        console.print("[yellow]  Waiting for market resolution...[/yellow]")

        winner = None

        # Method 1: Poll the Gamma API for actual resolution (up to 45s)
        if self.window:
            try:
                winner = await fetch_market_resolution(
                    market_slug=self.window.event_slug,
                    up_token_id=self.yes_token_id,
                    down_token_id=self.no_token_id,
                    max_wait=45.0,
                    poll_interval=3.0,
                )
                if winner:
                    console.print(f"[green]  Resolution from API: {winner} won[/green]")
            except Exception as e:
                logger.error(f"Resolution fetch error: {e}")

        # Method 2: Fallback to order book snapshot
        if winner is None:
            yes_mid = self.yes_book.midpoint
            yes_ask = self.yes_book.best_ask
            no_ask = self.no_book.best_ask
            winner = self.engine.determine_winner(yes_mid, yes_ask)
            console.print(
                f"[yellow]  API resolution unavailable — using book snapshot: "
                f"{winner} (YES mid={yes_mid}, ask={yes_ask}, NO ask={no_ask})[/yellow]"
            )

        # Run settlement
        result = self.engine.settle(winner)

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

        # Log to CSV
        market_label = f"BTC Up/Down 5m — {self.window.time_label}" if self.window else "BTC 5m"
        log_window_settlement(market_label, result, self.cumulative_pnl)

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
        console.print(f"[bold cyan]  SETTLEMENT — {winner} Won[/bold cyan]")
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

            # Smart file naming: PolyQuant_BTC5m_{Date}_{Start}_to_{End}.xlsx
            date_str = time.strftime("%Y-%m-%d")
            hour_start = self._report_hour_start or "0000"
            hour_start_clean = hour_start.replace(":", "")

            # End time from last window
            last_win = self._report_windows[-1]
            hour_end = last_win.get("window_end_time", "")
            hour_end_clean = hour_end.replace(":", "") if hour_end else time.strftime("%H%M")

            filename = f"PolyQuant_BTC5m_{date_str}_{hour_start_clean}_to_{hour_end_clean}.xlsx"
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
