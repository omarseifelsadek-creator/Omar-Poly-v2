"""
terminal.py — Market Intelligence & Analyst Dashboard.

Layout (6 panels):
┌──────────────────────────────────────────────────────────────┐
│ OBI │ Market │ [YES] │ Regime │ Wt.Mid │ Spd │ ΔOBI │ ● 12ms│
├──────────────────────────┬───────────────────────────────────┤
│                          │ MARKET STATE                      │
│     ORDER BOOK           │ Dominant│Conviction│Liq│Agg│Risk  │
│  (heat ladder +          ├───────────────────────────────────┤
│   Vegas Flash)           │ ANALYTICS                         │
│                          │ Wt.Mid│OBI+Vel│CVD│Flow│Depth│Vol │
├──────────────────────────┼───────────────────────────────────┤
│ ACTIVITY INTELLIGENCE    │ TAPE                              │
│ Timeline + CVD + Regime  │ TIME S SIZE PX VAL                │
│ Narrative Event Feed     │                                   │
├──────────────────────────┴───────────────────────────────────┤
│ Ctrl+C │ OBI v5.0 INTEL │ Session 02:15 │ Msgs 847          │
└──────────────────────────────────────────────────────────────┘
"""

import time
from collections import deque
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import settings
from state.orderbook import OrderBook
from state.level_tracker import LevelTracker
from data.models import Metrics, Insight, Side


# ──────────────────────────────────────────────────────────────
# PALETTE
# ──────────────────────────────────────────────────────────────
C_BID = "green"
C_ASK = "red"
C_WARN = "yellow"
C_METRIC = "cyan"
C_DIM = "bright_black"
C_MUTED = "grey50"
C_ACCENT = "bright_white"
C_PANEL = "grey23"

# Vegas Flash styles
C_STACK = "bold cyan"        # Size increased >25% (stacking)
C_PULL = "bold red"          # Size decreased >25% without trade (pulling)
C_FLASH_EXTREME = "reverse"  # Size changed >50% (extreme)

# Sparkline characters (8 levels)
SPARK = "▁▂▃▄▅▆▇█"


class TerminalUI:

    def __init__(self, orderbook: OrderBook, market_question: str = "Loading...",
                 token_label: str = "Yes", level_tracker: Optional[LevelTracker] = None):
        self.ob = orderbook
        self.market_question = market_question
        self.token_label = token_label
        self.console = Console()
        self._level_tracker = level_tracker

        self._metrics: Optional[Metrics] = None
        self._prev_metrics: Optional[Metrics] = None
        self._insights: deque[Insight] = deque(maxlen=30)
        self._connected = False
        self._messages_count = 0
        self._last_update: float = 0
        self._db_stats: dict = {}
        self._start_time: float = time.time()

        # Activity tracking — richer data per tick
        self._activity_window: deque = deque(maxlen=300)
        # Sparkline buckets: 60 one-second buckets for timeline
        self._spark_buckets: deque[dict] = deque(
            ({"buy": 0, "sell": 0, "events": 0, "ts": 0} for _ in range(60)),
            maxlen=60,
        )
        self._spark_last_second: int = 0
        # Narrative event feed (rich context descriptions)
        self._event_feed: deque = deque(maxlen=20)
        # Dedup keys for narrative events (prevents same event re-emitted every tick)
        self._event_seen: set[str] = set()
        self._event_seen_max: int = 200

        # Layout ratios (adjustable with [ ] keys)
        self._left_ratio = 11
        self._right_ratio = 9
        self._book_ratio = 5
        self._activity_ratio = 5  # Expanded (was 4)

        self._rotation_remaining: float = 0
        self._rotation_count: int = 0

        self._live: Optional[Live] = None

    def update_metrics(self, metrics: Metrics) -> None:
        self._prev_metrics = self._metrics
        self._metrics = metrics
        now = time.time()
        self._last_update = now
        now_sec = int(now)

        # Track activity window
        self._activity_window.append({
            "ts": now,
            "sweeps": len(metrics.sweep_events),
            "whales": len(metrics.whale_events),
            "absorptions": len(metrics.absorption_events),
            "spoofs": len(metrics.spoof_signals),
            "walls": len(metrics.walls),
            "buy_vol": metrics.buy_volume,
            "sell_vol": metrics.sell_volume,
            "flow": metrics.flow_pressure,
        })

        # Update sparkline buckets (deque maxlen auto-evicts oldest)
        if now_sec != self._spark_last_second:
            self._spark_buckets.append({"buy": 0, "sell": 0, "events": 0, "ts": now_sec})
            self._spark_last_second = now_sec

        bucket = self._spark_buckets[-1]
        event_count = (len(metrics.sweep_events) + len(metrics.whale_events)
                       + len(metrics.absorption_events) + len(metrics.spoof_signals))
        bucket["events"] += event_count
        bucket["buy"] += metrics.buy_volume
        bucket["sell"] += metrics.sell_volume

        # Populate narrative event feed from detections
        self._populate_narrative_feed(metrics)

    def _emit_event(self, key: str, message: str) -> None:
        """Emit a narrative event only if not already seen (dedup)."""
        if key in self._event_seen:
            return
        self._event_seen.add(key)
        self._event_feed.append(message)
        # Prevent unbounded growth of seen set
        if len(self._event_seen) > self._event_seen_max:
            # Remove oldest half
            to_keep = list(self._event_seen)[self._event_seen_max // 2:]
            self._event_seen = set(to_keep)

    def _populate_narrative_feed(self, metrics: Metrics) -> None:
        """Generate rich narrative events instead of terse detection logs."""
        for sweep in metrics.sweep_events:
            key = f"sweep:{sweep.side.value}:{sweep.start_price}:{sweep.timestamp_ms}"
            side = "buyer" if sweep.side == Side.BUY else "seller"
            sc = C_BID if sweep.side == Side.BUY else C_ASK
            self._emit_event(key,
                f"[{sc}]⚡ Aggressive {side} swept {sweep.levels_consumed} levels "
                f"{sweep.start_price:.2f}→{sweep.end_price:.2f} "
                f"({_fmt_size(sweep.total_volume)} contracts)[/{sc}]"
            )
        for whale in metrics.whale_events:
            key = f"whale:{whale.side.value}:{whale.price}:{whale.timestamp_ms}"
            side = "Buy" if whale.side == Side.BUY else "Sell"
            sc = C_BID if side == "Buy" else C_ASK
            val = whale.price * whale.size
            taker = "taker" if whale.is_taker else "passive"
            self._emit_event(key,
                f"[{sc}]🐋 Whale {side} ({taker}): {_fmt_size(whale.size)} "
                f"@ {whale.price:.2f} (${val:,.0f})[/{sc}]"
            )
        for ab in metrics.absorption_events:
            key = f"abs:{ab.side.value}:{ab.price}:{ab.timestamp_ms}"
            side = "bid" if ab.side == Side.BUY else "ask"
            if ab.is_institutional:
                self._emit_event(key,
                    f"[bold {C_METRIC}]🏦 INSTITUTIONAL: Wall at {ab.price:.2f} "
                    f"reloaded {ab.reload_count}× while absorbing "
                    f"{_fmt_size(ab.volume_absorbed)} — {ab.holding_pct:.0%} held[/bold {C_METRIC}]"
                )
            else:
                self._emit_event(key,
                    f"[{C_METRIC}]🛡️ Absorption @ {ab.price:.2f} ({side}) — "
                    f"wall held {min(ab.holding_pct, 1.0):.0%} after "
                    f"{ab.trades_absorbed} trades[/{C_METRIC}]"
                )
        for sp in metrics.spoof_signals:
            key = f"spoof:{sp.side.value}:{sp.price}:{sp.timestamp_ms}"
            side = "bid" if sp.side == Side.BUY else "ask"
            self._emit_event(key,
                f"[{C_WARN}]👻 Possible spoof at {sp.price:.2f} ({side}) — "
                f"{sp.oscillation_count} oscillations, peak {_fmt_size(sp.max_size_seen)}[/{C_WARN}]"
            )
        # CVD divergence warning (keyed by second to allow re-emit after cooldown)
        if metrics.cvd_divergence:
            div_key = f"cvd_div:{int(time.time()) // 30}"  # 30s cooldown
            if metrics.price_trend_strength > 0:
                self._emit_event(div_key,
                    f"[bold {C_WARN}]⚠️ DIVERGENCE: Price ↑ but CVD ↓ — "
                    f"sellers absorbing buying momentum[/bold {C_WARN}]"
                )
            else:
                self._emit_event(div_key,
                    f"[bold {C_WARN}]⚠️ DIVERGENCE: Price ↓ but CVD ↑ — "
                    f"buyers accumulating despite price drop[/bold {C_WARN}]"
                )
        # Liquidity voids (keyed by price+side, 10s cooldown)
        for void in metrics.liquidity_voids[:2]:
            key = f"void:{void.side.value}:{void.price}:{int(time.time()) // 10}"
            side = "bid" if void.side == Side.BUY else "ask"
            self._emit_event(key,
                f"[{C_WARN}]🕳️ Flash Zone: Thin liquidity at {void.price:.2f} "
                f"({side}) — {void.void_ratio:.0%} of avg depth[/{C_WARN}]"
            )

    def add_insight(self, insight: Insight):
        self._insights.append(insight)

    def add_insights(self, insights: list[Insight]):
        for insight in insights:
            self._insights.append(insight)

    def set_connected(self, connected: bool):
        self._connected = connected

    def set_messages_count(self, count: int):
        self._messages_count = count

    def set_db_stats(self, stats: dict):
        self._db_stats = stats

    def set_rotation_info(self, seconds_remaining: float, rotation_count: int):
        self._rotation_remaining = seconds_remaining
        self._rotation_count = rotation_count

    # ══════════════════════════════════════════════════════════
    # LAYOUT
    # ══════════════════════════════════════════════════════════

    def build_layout(self) -> Layout:
        layout = Layout()

        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="status", size=1),
        )

        # Left (book + activity) | Right (intel stack)
        layout["main"].split_row(
            Layout(name="left", ratio=self._left_ratio),
            Layout(name="right", ratio=self._right_ratio),
        )

        # Left: book on top, activity fills the rest
        layout["left"].split_column(
            Layout(name="book", ratio=self._book_ratio),
            Layout(name="activity", ratio=self._activity_ratio),
        )

        # Right: state + analytics + tape (paper & signals REMOVED)
        layout["right"].split_column(
            Layout(name="state", size=9),
            Layout(name="analytics"),    # Flexible — absorbs freed space
            Layout(name="tape", size=13),
        )

        layout["header"].update(self._header())
        layout["book"].update(self._orderbook())
        layout["state"].update(self._market_state())
        layout["analytics"].update(self._analytics_panel())
        layout["activity"].update(self._activity_intel())
        layout["tape"].update(self._trade_tape())
        layout["status"].update(self._status_bar())

        return layout

    def handle_key(self, key: str):
        """Handle keyboard input for layout resizing."""
        if key == "[":
            self._book_ratio = max(2, self._book_ratio - 1)
            self._activity_ratio = min(8, self._activity_ratio + 1)
        elif key == "]":
            self._book_ratio = min(8, self._book_ratio + 1)
            self._activity_ratio = max(2, self._activity_ratio - 1)
        elif key == "{":
            self._left_ratio = max(6, self._left_ratio - 1)
            self._right_ratio = min(14, self._right_ratio + 1)
        elif key == "}":
            self._left_ratio = min(14, self._left_ratio + 1)
            self._right_ratio = max(6, self._right_ratio - 1)

    # ══════════════════════════════════════════════════════════
    # HEADER — weighted midpoint + ΔOBI
    # ══════════════════════════════════════════════════════════

    def _header(self) -> Panel:
        m = self._metrics
        t = Text()

        t.append(" OBI ", style="bold white on grey30")
        t.append("  ")
        t.append(self.market_question[:50], style="bold white")
        t.append("  ")

        tok_s = f"bold {C_BID}" if self.token_label.lower() in ("yes", "y") else f"bold {C_ASK}"
        t.append(f"[{self.token_label.upper()}]", style=tok_s)
        t.append("   ")

        if m:
            # Regime
            rc_map = {
                "TRENDING_UP": C_BID, "TRENDING_DOWN": C_ASK,
                "VOLATILE": "magenta", "BREAKOUT": "bold white",
                "RANGING": C_WARN, "QUIET": C_DIM,
            }
            rc = rc_map.get(m.regime, C_DIM)
            t.append(f"{m.regime}", style=rc)
            if m.regime_confidence > 0:
                t.append(f" {m.regime_confidence:.0%}", style=C_DIM)
            t.append("  │  ", style=C_DIM)

            # Weighted midpoint (more predictive than simple mid)
            if m.vwap_mid:
                t.append(f"Wt.Mid {m.vwap_mid:.4f}", style=C_ACCENT)
                # Show delta from prev
                if self._prev_metrics and self._prev_metrics.vwap_mid:
                    delta = m.vwap_mid - self._prev_metrics.vwap_mid
                    if abs(delta) > 0.0001:
                        dc = C_BID if delta > 0 else C_ASK
                        t.append(f" Δ{delta:+.4f}", style=dc)
                t.append("  │  ", style=C_DIM)
            if m.spread is not None:
                t.append(f"Spd {m.spread:.3f}", style=C_METRIC)
                t.append("  │  ", style=C_DIM)

            # ΔOBI indicator
            if m.obi_action != "STABLE":
                oc = C_BID if m.obi_action == "STACKING" else C_ASK
                t.append(f"{m.obi_action}", style=oc)
                t.append("  │  ", style=C_DIM)

        if self._connected:
            t.append("●", style="green")
        else:
            t.append("●", style="red")
        latency = int((time.time() - self._last_update) * 1000) if self._last_update else 0
        t.append(f" {latency}ms", style=C_DIM)

        return Panel(t, style=C_PANEL, padding=(0, 0))

    # ══════════════════════════════════════════════════════════
    # ORDER BOOK — with Vegas Flash
    # ══════════════════════════════════════════════════════════

    def _orderbook(self) -> Panel:
        table = Table(
            show_header=True, header_style=f"bold {C_MUTED}",
            expand=True, padding=(0, 1), show_edge=False,
        )
        table.add_column("BID", justify="right", ratio=2, style=C_BID)
        table.add_column("PRICE", justify="center", ratio=1)
        table.add_column("ASK", justify="left", ratio=2, style=C_ASK)

        bids = self.ob.get_sorted_bids(max_levels=settings.OB_DISPLAY_LEVELS)
        asks = self.ob.get_sorted_asks(max_levels=settings.OB_DISPLAY_LEVELS)
        mx = self._get_max_size()

        wall_px = set()
        if self._metrics and self._metrics.walls:
            wall_px = {w.price for w in self._metrics.walls}

        for ask in reversed(asks):
            is_w = ask.price in wall_px
            sz = _fmt_size(ask.size)
            bar = _heat_bar(ask.size, mx, C_ASK)

            # Vegas Flash detection
            flash_style = self._get_flash_style(ask.price, Side.SELL, ask.size)

            if flash_style:
                ps = flash_style
                sz = f"[{flash_style}]{sz}[/{flash_style}]"
            elif is_w:
                ps = "bold red"
                sz = f"[bold red]{sz}[/bold red]"
            elif ask.size / max(mx, 1) < 0.1:
                ps = C_DIM
                sz = f"[{C_DIM}]{sz}[/{C_DIM}]"
            else:
                ps = "red"
            table.add_row("", f"[{ps}]{ask.price:.2f}[/{ps}]", f"{sz} {bar}")

        spread = self.ob.spread
        if spread is not None:
            table.add_row("", f"[{C_WARN}]── {spread:.3f} ──[/{C_WARN}]", "")

        for bid in bids:
            is_w = bid.price in wall_px
            sz = _fmt_size(bid.size)
            bar = _heat_bar(bid.size, mx, C_BID)

            # Vegas Flash detection
            flash_style = self._get_flash_style(bid.price, Side.BUY, bid.size)

            if flash_style:
                ps = flash_style
                sz = f"[{flash_style}]{sz}[/{flash_style}]"
            elif is_w:
                ps = "bold green"
                sz = f"[bold green]{sz}[/bold green]"
            elif bid.size / max(mx, 1) < 0.1:
                ps = C_DIM
                sz = f"[{C_DIM}]{sz}[/{C_DIM}]"
            else:
                ps = "green"
            table.add_row(f"{bar} {sz}", f"[{ps}]{bid.price:.2f}[/{ps}]", "")

        return Panel(table, title=f"[{C_MUTED}]ORDER BOOK[/{C_MUTED}]", border_style=C_PANEL, padding=(0, 0))

    def _get_flash_style(self, price: float, side: Side, current_size: float) -> Optional[str]:
        """
        Vegas Flash: detect rapid size changes and return the appropriate style.

        Returns None if no flash, or a Rich style string.
        """
        if self._level_tracker is None:
            return None

        prev_size, curr_size = self._level_tracker.get_size_change(
            price, side, settings.VEGAS_FLASH_WINDOW_SECONDS
        )

        if prev_size <= 0:
            return None

        change_pct = (curr_size - prev_size) / prev_size

        # Check if change was "without trade" by seeing if trades exist at this level
        level = self._level_tracker.get_level(price, side)
        if level is not None:
            recent_trades = level.get_trades_in_window(settings.VEGAS_FLASH_WINDOW_SECONDS)
            if recent_trades:
                return None  # Size changed due to trade execution, not manipulation

        if abs(change_pct) >= settings.VEGAS_FLASH_EXTREME:
            return C_FLASH_EXTREME
        elif change_pct >= settings.VEGAS_FLASH_THRESHOLD:
            return C_STACK  # Size increased = stacking
        elif change_pct <= -settings.VEGAS_FLASH_THRESHOLD:
            return C_PULL   # Size decreased without trade = pulling/spoofing

        return None

    # ══════════════════════════════════════════════════════════
    # MARKET STATE
    # ══════════════════════════════════════════════════════════

    def _market_state(self) -> Panel:
        m = self._metrics
        if not m:
            return Panel(f"[{C_DIM}]Awaiting data...[/{C_DIM}]",
                         title=f"[{C_MUTED}]MARKET STATE[/{C_MUTED}]", border_style=C_PANEL)

        is_yes = self.token_label.lower() in ("yes", "y")

        # Dominant
        if m.obi is not None:
            if m.obi >= 0.65:
                dom, ds = ("YES" if is_yes else "NO"), C_BID
            elif m.obi <= 0.35:
                dom, ds = ("NO" if is_yes else "YES"), C_ASK
            else:
                dom, ds = "NEUTRAL", C_MUTED
        else:
            dom, ds = "—", C_DIM

        # Conviction
        cs = sum([
            1 if m.spread is not None and m.spread < 0.02 else 0,
            1 if m.regime_confidence > 0.5 else 0,
            1 if m.obi is not None and (m.obi > 0.7 or m.obi < 0.3) else 0,
            1 if abs(m.flow_pressure) > 0.5 else 0,
        ])
        conv = ["LOW", "LOW", "MEDIUM", "HIGH", "HIGH"][min(cs, 4)]
        cc = {"LOW": C_DIM, "MEDIUM": C_WARN, "HIGH": C_ACCENT}[conv]

        # Liquidity
        td = m.total_bid_depth + m.total_ask_depth
        if td > 1_000_000:
            liq, ls = "THICK", C_BID
        elif td > 100_000:
            liq, ls = "MODERATE", C_MUTED
        else:
            liq, ls = "THIN", C_ASK

        # Aggression
        if m.sweep_events:
            agg, ags = "SWEEPING", C_ASK
        elif m.whale_events or abs(m.flow_pressure) > 0.5:
            agg, ags = "ACTIVE", C_WARN
        else:
            agg, ags = "PASSIVE", C_DIM

        # Risk
        if m.volatility > 0.5 or m.regime == "VOLATILE":
            rsk, rs = "UNSTABLE", C_ASK
        elif m.volatility > 0.2 or m.regime == "BREAKOUT":
            rsk, rs = "ELEVATED", C_WARN
        else:
            rsk, rs = "STABLE", C_BID

        t = Table(show_header=False, expand=True, padding=(0, 1), show_edge=False)
        t.add_column("K", style=C_MUTED, ratio=2)
        t.add_column("V", justify="right", ratio=2)

        t.add_row("Dominant", f"[{ds}]{dom}[/{ds}]")
        t.add_row("Conviction", f"[{cc}]{conv}[/{cc}]")
        t.add_row("Liquidity", f"[{ls}]{liq}[/{ls}]")
        t.add_row("Aggression", f"[{ags}]{agg}[/{ags}]")
        t.add_row("Risk", f"[{rs}]{rsk}[/{rs}]")
        t.add_row("Sentiment", _sentiment_gauge(m.sentiment))

        return Panel(t, title=f"[{C_MUTED}]MARKET STATE[/{C_MUTED}]", border_style=C_PANEL, padding=(0, 0))

    # ══════════════════════════════════════════════════════════
    # ANALYTICS — expanded panel with deltas and velocity
    # ══════════════════════════════════════════════════════════

    def _analytics_panel(self) -> Panel:
        m = self._metrics
        p = self._prev_metrics
        if not m:
            return Panel(f"[{C_DIM}]...[/{C_DIM}]",
                         title=f"[{C_MUTED}]ANALYTICS[/{C_MUTED}]", border_style=C_PANEL)

        t = Table(show_header=False, expand=True, padding=(0, 1), show_edge=False)
        t.add_column("K", style=C_MUTED, ratio=2)
        t.add_column("V", justify="right", ratio=3)

        # Weighted Midpoint + delta
        if m.vwap_mid:
            mid_str = f"[{C_ACCENT}]{m.vwap_mid:.4f}[/{C_ACCENT}]"
            if p and p.vwap_mid:
                delta = m.vwap_mid - p.vwap_mid
                if abs(delta) > 0.0001:
                    dc = C_BID if delta > 0 else C_ASK
                    mid_str += f" [{dc}](Δ{delta:+.4f})[/{dc}]"
            t.add_row("Wt.Mid", mid_str)

        # Spread + delta
        if m.spread is not None:
            spd_str = f"[{C_METRIC}]{m.spread:.3f}[/{C_METRIC}]"
            if p and p.spread is not None:
                ds = m.spread - p.spread
                if abs(ds) > 0.001:
                    dc = C_ASK if ds > 0 else C_BID  # Wider spread = bad
                    spd_str += f" [{dc}](Δ{ds:+.3f})[/{dc}]"
            t.add_row("Spread", spd_str)

        # OBI + bar + velocity label
        if m.obi is not None:
            obi_bar = _imbalance_bar(m.obi)
            ac = C_BID if m.obi_action == "STACKING" else C_ASK if m.obi_action == "PULLING" else C_DIM
            arrow = "↑" if m.obi_action == "STACKING" else "↓" if m.obi_action == "PULLING" else ""
            t.add_row("OBI", f"{m.obi:.0%} {obi_bar} [{ac}]{m.obi_action} {arrow}[/{ac}]")
            # OBI velocity detail
            t.add_row("", f"[{C_DIM}]5s vel: {m.obi_velocity_5s:+.4f}/s  "
                         f"30s vel: {m.obi_velocity_30s:+.4f}/s[/{C_DIM}]")

        # CVD — cumulative + rolling windows
        cvd_arrow = "▲" if m.cvd > 0 else "▼" if m.cvd < 0 else "─"
        cvd_color = C_BID if m.cvd > 0 else C_ASK if m.cvd < 0 else C_DIM
        t.add_row("CVD",
                   f"[{cvd_color}]{m.cvd:+,.0f} {cvd_arrow}[/{cvd_color}]  "
                   f"[{C_DIM}](5s: {m.cvd_5s:+,.0f}  30s: {m.cvd_30s:+,.0f})[/{C_DIM}]")
        if m.cvd_divergence:
            t.add_row("", f"[bold {C_WARN}]⚠ DIVERGENCE: Price/CVD opposing[/bold {C_WARN}]")

        # Flow + buy/sell breakdown + delta
        fc = C_BID if m.flow_pressure > 0.1 else C_ASK if m.flow_pressure < -0.1 else C_MUTED
        flow_label = "BUY PRESSURE" if m.flow_pressure > 0.3 else "SELL PRESSURE" if m.flow_pressure < -0.3 else ""
        t.add_row("Flow",
                   f"[{fc}]{m.flow_pressure:+.2f}[/{fc}] "
                   f"{_h_bar(abs(m.flow_pressure), 10, fc)} "
                   f"[{C_DIM}]{flow_label}[/{C_DIM}]")
        # Buy/sell breakdown + delta
        buy_sell = (f"[{C_BID}]B${m.buy_volume:,.0f}[/{C_BID}] │ "
                    f"[{C_ASK}]S${m.sell_volume:,.0f}[/{C_ASK}]")
        if p:
            delta_vol = (m.buy_volume - m.sell_volume) - (p.buy_volume - p.sell_volume)
            if abs(delta_vol) > 1:
                dvc = C_BID if delta_vol > 0 else C_ASK
                buy_sell += f"  [{dvc}]Δ${abs(delta_vol):,.0f}[/{dvc}]"
        t.add_row("", buy_sell)

        # Depth totals + ratio
        if m.total_bid_depth + m.total_ask_depth > 0:
            ratio = m.total_bid_depth / max(m.total_ask_depth, 1)
            t.add_row("Depth",
                       f"[{C_BID}]B:{_fmt_size(m.total_bid_depth)}[/{C_BID}]  "
                       f"[{C_ASK}]A:{_fmt_size(m.total_ask_depth)}[/{C_ASK}]  "
                       f"[{C_DIM}]Ratio {ratio:.1f}[/{C_DIM}]")

        # Volatility
        vc = C_ASK if m.volatility > 0.5 else C_WARN if m.volatility > 0.2 else C_DIM
        t.add_row("Vol", f"[{vc}]{m.volatility:.2f}[/{vc}] {_vol_bar(m.volatility)}")

        # Liquidity voids
        void_count = len(m.liquidity_voids)
        if void_count > 0:
            nearest = m.liquidity_voids[0]
            side_label = "b" if nearest.side == Side.BUY else "a"
            t.add_row("Voids",
                       f"[{C_WARN}]{void_count} flash zone{'s' if void_count > 1 else ''} "
                       f"(nearest: {nearest.price:.2f}{side_label})[/{C_WARN}]")
        else:
            t.add_row("Voids", f"[{C_DIM}]None[/{C_DIM}]")

        # Detection counts
        dets = []
        if m.spoof_signals:
            dets.append(f"[{C_WARN}]Spf:{len(m.spoof_signals)}[/{C_WARN}]")
        if m.absorption_events:
            inst_count = sum(1 for a in m.absorption_events if a.is_institutional)
            label = f"Abs:{len(m.absorption_events)}"
            if inst_count > 0:
                label += f"({inst_count}🏦)"
            dets.append(f"[{C_METRIC}]{label}[/{C_METRIC}]")
        if m.sweep_events:
            dets.append(f"[{C_ASK}]Swp:{len(m.sweep_events)}[/{C_ASK}]")
        if m.walls:
            dets.append(f"[{C_ACCENT}]Wl:{len(m.walls)}[/{C_ACCENT}]")
        if dets:
            t.add_row("Detect", " ".join(dets))

        return Panel(t, title=f"[{C_MUTED}]ANALYTICS[/{C_MUTED}]", border_style=C_PANEL, padding=(0, 0))

    # ══════════════════════════════════════════════════════════
    # ACTIVITY INTELLIGENCE — expanded with CVD + narrative
    # ══════════════════════════════════════════════════════════

    def _activity_intel(self) -> Panel:
        m = self._metrics
        now = time.time()
        cutoff = now - 60
        recent = [a for a in self._activity_window if a["ts"] > cutoff]

        lines = []

        # ── ROW 1: Sparkline Timeline ──
        spark = self._render_sparkline()
        lines.append(f"  [{C_MUTED}]Timeline 60s[/{C_MUTED}]  {spark}")

        # ── ROW 2: Flow Intensity Bar ──
        flow_intensity, flow_label = self._compute_flow_intensity(recent)
        fi_color = C_METRIC if flow_intensity < 0.6 else C_WARN if flow_intensity < 0.8 else C_ASK
        fi_bar = _h_bar(flow_intensity, 20, fi_color)
        flow_val = m.flow_pressure if m else 0
        fc = C_BID if flow_val > 0.1 else C_ASK if flow_val < -0.1 else C_MUTED
        lines.append(
            f"  [{C_MUTED}]Flow[/{C_MUTED}]  {fi_bar}  "
            f"[{C_ACCENT}]{flow_label}[/{C_ACCENT}]  "
            f"[{fc}]{flow_val:+.2f}[/{fc}]"
        )

        # ── ROW 3: Activity Regime ──
        act_regime, act_color = self._compute_activity_regime(recent)
        lines.append(f"  [{C_MUTED}]Activity[/{C_MUTED}]  [{act_color}]{act_regime}[/{act_color}]")

        # ── ROW 4: Event Distribution Bars ──
        if recent:
            ts = sum(a["sweeps"] for a in recent)
            tw = sum(a["whales"] for a in recent)
            ta = sum(a["absorptions"] for a in recent)
            tp = sum(a["spoofs"] for a in recent)
            mx_ev = max(ts, tw, ta, tp, 1)

            lines.append(
                f"  [{C_MUTED}]Swp[/{C_MUTED}] {_h_bar(ts / mx_ev, 12, C_ASK)} [{C_ACCENT}]{ts}[/{C_ACCENT}]"
                f"   [{C_MUTED}]Whl[/{C_MUTED}] {_h_bar(tw / mx_ev, 12, C_WARN)} [{C_ACCENT}]{tw}[/{C_ACCENT}]"
                f"   [{C_MUTED}]Abs[/{C_MUTED}] {_h_bar(ta / mx_ev, 12, C_METRIC)} [{C_ACCENT}]{ta}[/{C_ACCENT}]"
                f"   [{C_MUTED}]Spf[/{C_MUTED}] {_h_bar(tp / mx_ev, 12, C_WARN)} [{C_ACCENT}]{tp}[/{C_ACCENT}]"
            )

        # ── ROW 5: Buy vs Sell Aggression ──
        buy_total, sell_total = self._compute_aggression()
        agg_max = max(buy_total, sell_total, 1)
        delta = buy_total - sell_total
        delta_label = "BUY DOM" if delta > 0 else "SELL DOM" if delta < 0 else "NEUTRAL"
        delta_color = C_BID if delta > 0 else C_ASK if delta < 0 else C_MUTED

        lines.append(
            f"  [{C_BID}]Buy[/{C_BID}]  {_h_bar(buy_total / agg_max, 14, C_BID)}  "
            f"[{C_ACCENT}]${buy_total:,.0f}[/{C_ACCENT}]"
        )
        lines.append(
            f"  [{C_ASK}]Sell[/{C_ASK}] {_h_bar(sell_total / agg_max, 14, C_ASK)}  "
            f"[{C_ACCENT}]${sell_total:,.0f}[/{C_ACCENT}]   "
            f"[{delta_color}]Δ ${abs(delta):,.0f} {delta_label}[/{delta_color}]"
        )

        # ── ROW 6+: Narrative Event Feed (newest at bottom) ──
        feed = list(self._event_feed)[-12:]
        if feed:
            lines.append("")
            for ev in feed:
                lines.append(f"  {ev}")

        # ── Recent Insights (fill remaining space) ──
        recent_insights = list(self._insights)[-8:]
        if recent_insights:
            lines.append("")
            lines.append(f"  [{C_MUTED}]─── Log ───[/{C_MUTED}]")
            for ins in recent_insights:
                sev_color = C_WARN if ins.severity == "alert" else C_MUTED if ins.severity == "warning" else C_DIM
                lines.append(
                    f"  [{C_DIM}]{ins.time_str}[/{C_DIM}] [{sev_color}]{ins.message[:72]}[/{sev_color}]"
                )

        return Panel(
            "\n".join(lines),
            title=f"[{C_MUTED}]ACTIVITY INTELLIGENCE[/{C_MUTED}]",
            border_style=C_PANEL,
            padding=(0, 0),
        )

    # ══════════════════════════════════════════════════════════
    # TRADE TAPE
    # ══════════════════════════════════════════════════════════

    def _trade_tape(self) -> Panel:
        trades = self.ob.recent_trades[:10]
        if not trades:
            return Panel(f"[{C_DIM}]No trades[/{C_DIM}]",
                         title=f"[{C_MUTED}]TAPE[/{C_MUTED}]", border_style=C_PANEL)

        table = Table(show_header=True, header_style=C_MUTED, expand=True,
                      padding=(0, 1), show_edge=False)
        table.add_column("TIME", style=C_DIM, ratio=2)
        table.add_column("S", justify="center", ratio=1)
        table.add_column("SIZE", justify="right", ratio=1)
        table.add_column("PX", justify="right", ratio=1)
        table.add_column("VAL", justify="right", ratio=1)

        for trade in trades:
            t = time.localtime(trade.timestamp_ms / 1000)
            ms = trade.timestamp_ms % 1000
            ts = time.strftime("%H:%M:%S", t) + f".{ms // 100}"
            sc = C_BID if trade.side == Side.BUY else C_ASK
            ch = "B" if trade.side == Side.BUY else "S"
            val = trade.price * trade.size
            table.add_row(ts, f"[{sc}]{ch}[/{sc}]", _fmt_size(trade.size),
                          f"{trade.price:.2f}", f"${val:,.0f}")

        return Panel(table, title=f"[{C_MUTED}]TAPE[/{C_MUTED}]", border_style=C_PANEL, padding=(0, 0))

    # ══════════════════════════════════════════════════════════
    # STATUS BAR
    # ══════════════════════════════════════════════════════════

    def _status_bar(self) -> Text:
        t = Text()
        elapsed = int(time.time() - self._start_time)
        mins, secs = divmod(elapsed, 60)
        t.append(" Ctrl+C exit", style=C_DIM)
        t.append("  │  ", style=C_DIM)
        t.append("[ ] resize V", style=C_DIM)
        t.append("  ", style=C_DIM)
        t.append("{ } resize H", style=C_DIM)
        t.append("  │  ", style=C_DIM)
        t.append("OBI v5.0 INTEL", style=C_MUTED)
        t.append("  │  ", style=C_DIM)
        t.append(f"Session {mins:02d}:{secs:02d}", style=C_DIM)
        t.append("  │  ", style=C_DIM)
        t.append(f"Msgs {self._messages_count}", style=C_DIM)
        if self._db_stats:
            s = self._db_stats
            t.append("  │  ", style=C_DIM)
            t.append(f"DB {s.get('trades', 0)}t {s.get('ob_snapshots', 0)}s {s.get('events', 0)}e", style=C_DIM)
        if self._rotation_remaining > 0:
            rm, rs = divmod(int(self._rotation_remaining), 60)
            rc = C_ASK if self._rotation_remaining < 30 else C_WARN if self._rotation_remaining < 60 else C_DIM
            t.append("  │  ", style=C_DIM)
            t.append(f"Rotate {rm}:{rs:02d}", style=rc)
            t.append(f" (#{self._rotation_count})", style=C_DIM)
        return t

    # ══════════════════════════════════════════════════════════
    # LIFECYCLE
    # ══════════════════════════════════════════════════════════

    def start(self):
        self._live = Live(
            self.build_layout(), console=self.console,
            refresh_per_second=1 / settings.UI_REFRESH_RATE, screen=True,
        )
        self._live.start()

    def refresh(self):
        if self._live:
            self._live.update(self.build_layout())

    def stop(self):
        if self._live:
            self._live.stop()
            self._live = None

    # ══════════════════════════════════════════════════════════
    # COMPUTATION HELPERS
    # ══════════════════════════════════════════════════════════

    def _get_max_size(self) -> float:
        bids = self.ob.get_sorted_bids(max_levels=settings.OB_DISPLAY_LEVELS)
        asks = self.ob.get_sorted_asks(max_levels=settings.OB_DISPLAY_LEVELS)
        sizes = [lvl.size for lvl in bids] + [lvl.size for lvl in asks]
        return max(sizes) if sizes else 1.0

    def _render_sparkline(self) -> str:
        """Render a 60-char unicode sparkline of activity intensity."""
        values = []
        for b in self._spark_buckets:
            intensity = b["events"] * 3 + (b["buy"] + b["sell"]) / 100
            values.append(intensity)

        if not values or max(values) == 0:
            return f"[{C_DIM}]{'▁' * 60}[/{C_DIM}]"

        mx = max(values)
        chars = []
        for i, v in enumerate(values):
            level = int((v / mx) * 7) if mx > 0 else 0
            level = max(0, min(level, 7))
            c = SPARK[level]

            b = self._spark_buckets[i]
            if b["events"] > 2:
                color = C_WARN
            elif b["buy"] > b["sell"] * 1.5:
                color = C_BID
            elif b["sell"] > b["buy"] * 1.5:
                color = C_ASK
            else:
                color = C_DIM

            chars.append(f"[{color}]{c}[/{color}]")

        return "".join(chars)

    def _compute_flow_intensity(self, recent: list) -> tuple[float, str]:
        """Compute flow intensity 0.0-1.0 and label."""
        if not recent:
            return 0.0, "IDLE"

        total_events = sum(
            a["sweeps"] + a["whales"] + a["absorptions"]
            for a in recent
        )
        total_vol = sum(a["buy_vol"] + a["sell_vol"] for a in recent)

        event_score = min(total_events / 50, 1.0) * 0.6
        vol_score = min(total_vol / 10000, 1.0) * 0.4
        intensity = min(event_score + vol_score, 1.0)

        if intensity > 0.8:
            label = "EXTREME"
        elif intensity > 0.6:
            label = "HIGH"
        elif intensity > 0.3:
            label = "MODERATE"
        elif intensity > 0.1:
            label = "LOW"
        else:
            label = "IDLE"

        return intensity, label

    def _compute_activity_regime(self, recent: list) -> tuple[str, str]:
        """Compute activity regime label from recent data."""
        m = self._metrics
        if not m or not recent:
            return "QUIET", C_DIM

        total_sweeps = sum(a["sweeps"] for a in recent)
        total_events = sum(a["sweeps"] + a["whales"] + a["absorptions"] + a["spoofs"] for a in recent)
        avg_flow = sum(a.get("flow", 0) for a in recent) / max(len(recent), 1)

        if total_events > 30 and total_sweeps > 10:
            if avg_flow > 0.3:
                return "AGGRESSIVE ACCUMULATION", C_BID
            elif avg_flow < -0.3:
                return "DISTRIBUTION", C_ASK
            else:
                return "HIGH ACTIVITY", C_WARN
        elif total_sweeps > 5 and m.volatility > 0.4:
            return "PANIC", "bold red"
        elif total_events > 15:
            if avg_flow > 0.2:
                return "ACCUMULATION", C_BID
            elif avg_flow < -0.2:
                return "DISTRIBUTION", C_ASK
            else:
                return "ACTIVE", C_WARN
        elif total_events > 3:
            return "MODERATE", C_MUTED
        else:
            return "QUIET", C_DIM

    def _compute_aggression(self) -> tuple[float, float]:
        """Compute buy/sell dollar volume from recent trades."""
        buy_total = 0.0
        sell_total = 0.0
        now = time.time()
        for trade in self.ob.recent_trades[:50]:
            age = now - (trade.timestamp_ms / 1000)
            if age > 60:
                continue
            val = trade.price * trade.size
            if trade.side == Side.BUY:
                buy_total += val
            else:
                sell_total += val
        return buy_total, sell_total


# ══════════════════════════════════════════════════════════════
# STANDALONE HELPERS
# ══════════════════════════════════════════════════════════════

def _fmt_size(size: float) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f}M"
    elif size >= 10_000:
        return f"{size / 1_000:.0f}K"
    elif size >= 1_000:
        return f"{size / 1_000:.1f}K"
    else:
        return f"{size:,.0f}"


def _heat_bar(size: float, max_size: float, color: str, max_width: int = 12) -> str:
    if max_size == 0:
        return ""
    ratio = min(size / max_size, 1.0)
    width = max(int(ratio * max_width), 0)
    if width == 0:
        return ""
    char = "█" if ratio > 0.7 else "▓" if ratio > 0.3 else "░"
    return f"[{color}]{char * width}[/{color}]"


def _h_bar(ratio: float, width: int, color: str) -> str:
    """Simple horizontal bar from 0.0-1.0 ratio."""
    ratio = max(0.0, min(ratio, 1.0))
    filled = int(ratio * width)
    empty = width - filled
    return f"[{color}]{'█' * filled}[/{color}][{C_DIM}]{'░' * empty}[/{C_DIM}]"


def _imbalance_bar(obi: float, width: int = 10) -> str:
    filled = int(obi * width)
    empty = width - filled
    return f"[{C_BID}]{'█' * filled}[/{C_BID}][{C_DIM}]{'░' * empty}[/{C_DIM}]"


def _vol_bar(vol: float, width: int = 6) -> str:
    filled = int(min(vol, 1.0) * width)
    c = C_ASK if vol > 0.5 else C_WARN if vol > 0.2 else C_DIM
    return f"[{c}]{'█' * filled}{'░' * (width - filled)}[/{c}]"


def _sentiment_gauge(sent: float) -> str:
    width = 14
    pos = int((sent + 1) / 2 * width)
    pos = max(0, min(pos, width))
    left = "─" * pos
    right = "─" * (width - pos)
    ms = C_BID if sent > 0.3 else C_ASK if sent < -0.3 else C_MUTED
    return f"[{C_ASK}]◄{left}[/{C_ASK}][{ms}]●[/{ms}][{C_BID}]{right}►[/{C_BID}]"
