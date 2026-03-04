"""
cyber_engine.py — Synthetic Market Microstructure Engine.

Dual-stream ingest for YES+NO tokens. Pure visualization/diagnostic tool.
No trading logic, no executor, no fills.

Usage:
    engine = SyntheticEngine(yes_token_id, no_token_id, market_question)
    await engine.run()
"""

import asyncio
import json
import time
import os
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

from config import settings
from data.message_parser import parse_messages
from data.models import (
    BookSnapshot, PriceChangeEvent, TradeEvent, Side, Metrics,
)
from state.orderbook import OrderBook
from state.level_tracker import LevelTracker
from analytics.metrics import compute_all_metrics
from analytics.momentum import MomentumEngine

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# STATE DATACLASS
# ══════════════════════════════════════════════════════════════

@dataclass
class SyntheticState:
    """Aggregated state for the dashboard to render."""

    # Market metadata
    market_question: str = ""
    uptime_s: float = 0.0
    cycle: int = 0

    # YES book
    yes_best_bid: float = 0.0
    yes_best_ask: float = 0.0
    yes_midpoint: float = 0.0
    yes_spread: float = 0.0
    yes_total_bid_depth: float = 0.0
    yes_total_ask_depth: float = 0.0
    yes_bids: list = field(default_factory=list)
    yes_asks: list = field(default_factory=list)

    # NO book
    no_best_bid: float = 0.0
    no_best_ask: float = 0.0
    no_midpoint: float = 0.0
    no_spread: float = 0.0
    no_total_bid_depth: float = 0.0
    no_total_ask_depth: float = 0.0
    no_bids: list = field(default_factory=list)
    no_asks: list = field(default_factory=list)

    # Synthetic pricing
    synthetic_mid: float = 0.0
    arb_gap: float = 0.0
    cross_book_obi: float = 1.0
    pair_cost: float = 0.0
    edge_pct: float = 0.0
    favor: str = "—"

    # Regime
    regime: str = "QUIET"
    regime_confidence: float = 0.0

    # Time series
    scatter_points: list = field(default_factory=list)
    voltage_points: list = field(default_factory=list)
    flow_delta_bars: list = field(default_factory=list)
    cumulative_delta: float = 0.0

    # Triggers
    price_velocity: float = 0.0
    volume_spike: float = 0.0
    order_imbalance: float = 0.0

    # Level classification
    level_heatmap: list = field(default_factory=list)

    # Anomaly feed
    anomaly_feed: list = field(default_factory=list)

    # Pipeline state
    pipeline_stage: str = "IDLE"

    # Connection/perf
    connected: bool = False
    msg_count: int = 0
    msg_rate: float = 0.0
    latency_ms: float = 0.0
    yes_trades: int = 0
    no_trades: int = 0


# ══════════════════════════════════════════════════════════════
# ENGINE
# ══════════════════════════════════════════════════════════════

class SyntheticEngine:
    """
    Pure visualization engine for dual-token market microstructure.
    Adapts pair_runner's dual-WebSocket pattern, removes ALL trading logic.
    """

    def __init__(
        self,
        yes_token_id: str,
        no_token_id: str,
        market_question: str = "Synthetic Market",
    ):
        self.yes_token_id = yes_token_id
        self.no_token_id = no_token_id
        self.market_question = market_question

        # Dual order books
        self.yes_book = OrderBook()
        self.no_book = OrderBook()

        # Dual analytics stacks
        self.yes_tracker = LevelTracker()
        self.no_tracker = LevelTracker()
        self.yes_momentum = MomentumEngine()
        self.no_momentum = MomentumEngine()

        # Time series buffers
        self._scatter: deque = deque(maxlen=300)
        self._voltage: deque = deque(maxlen=120)
        self._flow_bars: deque = deque(maxlen=60)
        self._prev_synthetic_mid: float = 0.0
        self._prev_mid_time: float = 0.0

        # Volume tracking
        self._buy_vol: float = 0.0
        self._sell_vol: float = 0.0
        self._cum_delta: float = 0.0
        self._flow_bucket_vol: dict = {"buy": 0.0, "sell": 0.0}
        self._flow_bucket_time: float = time.time()

        # Anomaly feed
        self._anomaly_feed: deque = deque(maxlen=50)
        self._last_anomaly_check: float = 0.0

        # Metrics cache
        self._yes_metrics: Optional[Metrics] = None
        self._no_metrics: Optional[Metrics] = None

        # Connection state
        self._running = False
        self._connected = False
        self._msg_count = 0
        self._msg_rate_count = 0
        self._msg_rate_time = time.time()
        self._msg_rate = 0.0
        self._last_ws_time: float = 0.0
        self._start_time: float = time.time()
        self._cycle: int = 0
        self._yes_trade_count = 0
        self._no_trade_count = 0

        # Pipeline state
        self._pipeline_stage = "IDLE"

        # Dashboard (created at run time)
        self._dashboard = None

    async def run(self):
        """Main entry point — runs forever until Ctrl+C."""
        from ui.cyber_dashboard import CyberDashboard

        self._dashboard = CyberDashboard()
        self._running = True
        self._start_time = time.time()

        try:
            self._dashboard.start()
            await asyncio.gather(
                self._ws_loop(),
                self._ui_loop(),
            )
        except KeyboardInterrupt:
            pass
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            if self._dashboard:
                self._dashboard.stop()

    async def _ws_loop(self):
        """Single WebSocket connection subscribing to both tokens."""
        uri = settings.CLOB_WS_URL
        reconnect_count = 0

        while self._running:
            try:
                self._pipeline_stage = "WS"
                async with websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._connected = True

                    if reconnect_count > 0:
                        logger.info(f"WebSocket reconnected (attempt {reconnect_count})")

                    # Subscribe to BOTH tokens
                    subscribe_msg = json.dumps({
                        "type": "market",
                        "assets_ids": [self.yes_token_id, self.no_token_id],
                    })
                    await ws.send(subscribe_msg)

                    await self._message_loop(ws)

            except ConnectionClosed:
                reconnect_count += 1
                self._connected = False
                logger.info(f"WS closed, reconnecting (attempt {reconnect_count})")
                await asyncio.sleep(1)
            except Exception as e:
                reconnect_count += 1
                self._connected = False
                logger.error(f"WS error: {e}")
                await asyncio.sleep(2)

    async def _message_loop(self, ws):
        """Process messages and route to correct book."""
        try:
            async for raw_message in ws:
                if not self._running:
                    break

                self._msg_count += 1
                self._msg_rate_count += 1
                self._last_ws_time = time.time()

                self._pipeline_stage = "PARSE"
                for parsed in parse_messages(raw_message):
                    asset_id = getattr(parsed, "asset_id", "")

                    if isinstance(parsed, PriceChangeEvent):
                        self._pipeline_stage = "BOOK"
                        has_yes = any(
                            c.asset_id == self.yes_token_id
                            for c in parsed.price_changes
                        )
                        has_no = any(
                            c.asset_id == self.no_token_id
                            for c in parsed.price_changes
                        )
                        if has_yes:
                            self._handle_price_change(
                                parsed, self.yes_book, self.yes_tracker
                            )
                        if has_no:
                            self._handle_price_change(
                                parsed, self.no_book, self.no_tracker
                            )

                    elif isinstance(parsed, BookSnapshot):
                        self._pipeline_stage = "BOOK"
                        if asset_id == self.yes_token_id:
                            self.yes_book.apply_snapshot(parsed)
                        elif asset_id == self.no_token_id:
                            self.no_book.apply_snapshot(parsed)

                    elif isinstance(parsed, TradeEvent):
                        self._pipeline_stage = "BOOK"
                        if asset_id == self.yes_token_id:
                            self.yes_book.apply_trade(parsed)
                            self.yes_momentum.update(trade_price=parsed.price)
                            self._yes_trade_count += 1
                        elif asset_id == self.no_token_id:
                            self.no_book.apply_trade(parsed)
                            self.no_momentum.update(trade_price=parsed.price)
                            self._no_trade_count += 1

                        # Track volume for flow delta
                        if parsed.side == Side.BUY:
                            self._buy_vol += parsed.size * parsed.price
                            self._flow_bucket_vol["buy"] += parsed.size * parsed.price
                        else:
                            self._sell_vol += parsed.size * parsed.price
                            self._flow_bucket_vol["sell"] += parsed.size * parsed.price
                        self._cum_delta = self._buy_vol - self._sell_vol

                # Update analytics after each batch
                self._update_analytics()

        except ConnectionClosed:
            logger.info("WS closed in message loop")

    def _handle_price_change(self, msg, book, tracker):
        """Apply price change to specific book + tracker."""
        book.apply_price_change(msg)
        for change in msg.price_changes:
            tracker.record_change(
                change.price, change.side, change.size, msg.timestamp_ms
            )

    def _update_analytics(self):
        """Compute metrics, synthetic pricing, time series, anomalies."""
        self._pipeline_stage = "CALC"
        now = time.time()

        # Compute per-side metrics
        if self.yes_book.is_initialized:
            self._yes_metrics = compute_all_metrics(
                self.yes_book, self.yes_tracker, self.yes_momentum
            )
            if self._yes_metrics:
                self.yes_momentum.update(
                    midpoint=self._yes_metrics.midpoint,
                    obi=self._yes_metrics.obi,
                    spread=self._yes_metrics.spread,
                    flow_pressure=self._yes_metrics.flow_pressure,
                    bid_depth=self._yes_metrics.total_bid_depth,
                    ask_depth=self._yes_metrics.total_ask_depth,
                )

        if self.no_book.is_initialized:
            self._no_metrics = compute_all_metrics(
                self.no_book, self.no_tracker, self.no_momentum
            )
            if self._no_metrics:
                self.no_momentum.update(
                    midpoint=self._no_metrics.midpoint,
                    obi=self._no_metrics.obi,
                    spread=self._no_metrics.spread,
                    flow_pressure=self._no_metrics.flow_pressure,
                    bid_depth=self._no_metrics.total_bid_depth,
                    ask_depth=self._no_metrics.total_ask_depth,
                )

        # Scatter: record (yes_best_bid, no_best_bid) over time
        yb = self.yes_book.best_bid or 0
        nb = self.no_book.best_bid or 0
        if yb > 0 and nb > 0:
            self._scatter.append((now, yb, nb))

        # Voltage: synthetic mid velocity
        syn_mid = self._compute_synthetic_mid()
        if syn_mid > 0 and self._prev_synthetic_mid > 0:
            dt = now - self._prev_mid_time if self._prev_mid_time else 1.0
            if dt > 0:
                velocity = (syn_mid - self._prev_synthetic_mid) / dt
                self._voltage.append((now, velocity))
        self._prev_synthetic_mid = syn_mid
        self._prev_mid_time = now

        # Flow delta bars: bucket every 5 seconds
        if now - self._flow_bucket_time >= 5.0:
            buy_v = self._flow_bucket_vol["buy"]
            sell_v = self._flow_bucket_vol["sell"]
            if buy_v > 0 or sell_v > 0:
                self._flow_bars.append((now, buy_v, sell_v))
            self._flow_bucket_vol = {"buy": 0.0, "sell": 0.0}
            self._flow_bucket_time = now

        # Anomaly detection (throttled to 1/sec)
        if now - self._last_anomaly_check >= 1.0:
            self._detect_anomalies()
            self._last_anomaly_check = now

        # Message rate
        rate_dt = now - self._msg_rate_time
        if rate_dt >= 2.0:
            self._msg_rate = self._msg_rate_count / rate_dt
            self._msg_rate_count = 0
            self._msg_rate_time = now

        self._cycle += 1
        self._pipeline_stage = "DRAW"

    def _compute_synthetic_mid(self) -> float:
        """P_syn = (Price_Yes + (1 - Price_No)) / 2"""
        yes_mid = self.yes_book.midpoint or 0
        no_mid = self.no_book.midpoint or 0
        if yes_mid and no_mid:
            return (yes_mid + (1.0 - no_mid)) / 2.0
        return yes_mid or (1.0 - no_mid) if no_mid else 0.0

    def _compute_arb_gap(self) -> float:
        """gap = Price_Yes + Price_No - 1.0"""
        yes_mid = self.yes_book.midpoint or 0
        no_mid = self.no_book.midpoint or 0
        if yes_mid and no_mid:
            return yes_mid + no_mid - 1.0
        return 0.0

    def _compute_cross_book_obi(self) -> float:
        """(yes_bids + no_asks) / (yes_asks + no_bids)"""
        numerator = self.yes_book.total_bid_depth + self.no_book.total_ask_depth
        denominator = self.yes_book.total_ask_depth + self.no_book.total_bid_depth
        if denominator == 0:
            return 1.0
        return numerator / denominator

    def _detect_anomalies(self):
        """Generate system-log style anomaly entries."""
        now_ms = int(time.time() * 1000)

        for label, metrics in [("YES", self._yes_metrics), ("NO", self._no_metrics)]:
            if metrics is None:
                continue

            for sp in metrics.spoof_signals:
                side = "bid" if sp.side == Side.BUY else "ask"
                self._anomaly_feed.append({
                    "ts": now_ms, "type": "SPOOF",
                    "message": f"@ ${sp.price:.2f} ({label} {side}, {sp.oscillation_count}x)",
                    "severity": "warning",
                })

            for ab in metrics.absorption_events:
                side = "bid" if ab.side == Side.BUY else "ask"
                self._anomaly_feed.append({
                    "ts": now_ms, "type": "ABSORPTION",
                    "message": f"@ ${ab.price:.2f} ({label} {side}, held {ab.holding_pct:.0%})",
                    "severity": "alert",
                })

            for sw in metrics.sweep_events:
                side = "buy" if sw.side == Side.BUY else "sell"
                self._anomaly_feed.append({
                    "ts": now_ms, "type": "SWEEP",
                    "message": f"{sw.levels_consumed} lvls ({label} {side}, {sw.total_volume:,.0f} vol)",
                    "severity": "alert",
                })

            for wh in metrics.whale_events:
                side = "Buy" if wh.side == Side.BUY else "Sell"
                val = wh.price * wh.size
                if val >= 500:
                    self._anomaly_feed.append({
                        "ts": now_ms, "type": "WHALE",
                        "message": f"${val:,.0f} {side} ({label} @ ${wh.price:.2f})",
                        "severity": "info",
                    })

            # Toxic flow
            if abs(metrics.flow_pressure) > 0.7:
                direction = "buy" if metrics.flow_pressure > 0 else "sell"
                self._anomaly_feed.append({
                    "ts": now_ms, "type": "TOXIC",
                    "message": f"sustained {direction} flow {metrics.flow_pressure:+.2f} ({label})",
                    "severity": "warning",
                })

        # Arb gap anomaly
        gap = self._compute_arb_gap()
        if abs(gap) > 0.02:
            self._anomaly_feed.append({
                "ts": now_ms, "type": "ARB_GAP",
                "message": f"deviation {gap:+.3f} (YES+NO={1.0+gap:.3f})",
                "severity": "warning" if abs(gap) > 0.03 else "info",
            })

    def _classify_levels(self) -> list:
        """Classify active levels as Flicker, Iron Wall, or Absorbing."""
        results = []
        now_ms = int(time.time() * 1000)

        for token_label, tracker in [
            ("YES", self.yes_tracker),
            ("NO", self.no_tracker),
        ]:
            for level_hist in tracker.get_all_active_levels():
                entries = level_hist.entries
                if not entries:
                    continue

                latest = entries[-1]
                if latest.size <= 0:
                    continue

                age_s = (now_ms - level_hist.first_seen_ms) / 1000.0 if level_hist.first_seen_ms else 0
                osc_count = level_hist.count_oscillations(60.0, 50.0)

                # Classification
                if osc_count >= 3:
                    category = "FLICKER"
                elif age_s > 30 and latest.size > 100:
                    category = "IRON"
                elif len(level_hist.trades) >= 3:
                    category = "ABSORB"
                else:
                    category = "NORMAL"

                if category != "NORMAL":
                    results.append({
                        "token": token_label,
                        "side": "bid" if level_hist.side == Side.BUY else "ask",
                        "price": level_hist.price,
                        "size": latest.size,
                        "age_s": age_s,
                        "category": category,
                        "osc": osc_count,
                    })

        # Sort: FLICKER first, then IRON, then ABSORB
        priority = {"FLICKER": 0, "IRON": 1, "ABSORB": 2}
        results.sort(key=lambda x: priority.get(x["category"], 3))
        return results[:8]

    def _build_state(self) -> SyntheticState:
        """Build the complete state for the dashboard."""
        now = time.time()
        syn_mid = self._compute_synthetic_mid()
        arb_gap = self._compute_arb_gap()
        cross_obi = self._compute_cross_book_obi()

        # Pair cost + edge
        ya = self.yes_book.best_ask or 0
        na = self.no_book.best_ask or 0
        pair_cost = ya + na if (ya > 0 and na > 0) else 0
        edge_pct = ((1.0 - pair_cost) / pair_cost * 100) if pair_cost > 0 else 0

        # Favor direction
        if cross_obi > 1.05:
            favor = "YES"
        elif cross_obi < 0.95:
            favor = "NO"
        else:
            favor = "EVEN"

        # Regime from YES momentum (primary)
        regime = "QUIET"
        regime_conf = 0.0
        if self._yes_metrics:
            regime = self._yes_metrics.regime
            regime_conf = self._yes_metrics.regime_confidence

        # Triggers
        pv = self._yes_metrics.price_velocity if self._yes_metrics else 0
        vi = 0.0
        if self._flow_bars:
            _, last_buy, last_sell = self._flow_bars[-1]
            total = last_buy + last_sell
            if total > 0:
                avg_bucket = sum(b + s for _, b, s in self._flow_bars) / len(self._flow_bars)
                if avg_bucket > 0:
                    vi = total / avg_bucket
        oi = self._yes_metrics.obi if self._yes_metrics and self._yes_metrics.obi else 0.5

        # Level heatmap
        try:
            heatmap = self._classify_levels()
        except Exception:
            heatmap = []

        return SyntheticState(
            market_question=self.market_question,
            uptime_s=now - self._start_time,
            cycle=self._cycle,

            yes_best_bid=self.yes_book.best_bid or 0,
            yes_best_ask=self.yes_book.best_ask or 0,
            yes_midpoint=self.yes_book.midpoint or 0,
            yes_spread=self.yes_book.spread or 0,
            yes_total_bid_depth=self.yes_book.total_bid_depth,
            yes_total_ask_depth=self.yes_book.total_ask_depth,
            yes_bids=self.yes_book.get_sorted_bids(8),
            yes_asks=self.yes_book.get_sorted_asks(8),

            no_best_bid=self.no_book.best_bid or 0,
            no_best_ask=self.no_book.best_ask or 0,
            no_midpoint=self.no_book.midpoint or 0,
            no_spread=self.no_book.spread or 0,
            no_total_bid_depth=self.no_book.total_bid_depth,
            no_total_ask_depth=self.no_book.total_ask_depth,
            no_bids=self.no_book.get_sorted_bids(8),
            no_asks=self.no_book.get_sorted_asks(8),

            synthetic_mid=syn_mid,
            arb_gap=arb_gap,
            cross_book_obi=cross_obi,
            pair_cost=pair_cost,
            edge_pct=edge_pct,
            favor=favor,

            regime=regime,
            regime_confidence=regime_conf,

            scatter_points=list(self._scatter),
            voltage_points=list(self._voltage),
            flow_delta_bars=list(self._flow_bars),
            cumulative_delta=self._cum_delta,

            price_velocity=pv,
            volume_spike=vi,
            order_imbalance=oi,

            level_heatmap=heatmap,
            anomaly_feed=list(self._anomaly_feed),

            pipeline_stage=self._pipeline_stage,

            connected=self._connected,
            msg_count=self._msg_count,
            msg_rate=self._msg_rate,
            latency_ms=(now - self._last_ws_time) * 1000 if self._last_ws_time else 0,
            yes_trades=self._yes_trade_count,
            no_trades=self._no_trade_count,
        )

    async def _ui_loop(self):
        """Render dashboard at 2 FPS."""
        while self._running:
            try:
                state = self._build_state()
                self._dashboard.render(state)
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"UI render error: {e}")
                await asyncio.sleep(1)
