"""
terminal.py — Professional order flow intelligence terminal.

Layout:
┌──────────────────────────────────────────────────────────────┐
│ OBI │ Market │ [YES] │ Regime │ Mid │ Spd │ ● 12ms          │
├──────────────────────────┬───────────────────────────────────┤
│                          │ MARKET STATE                      │
│                          │ Dominant│Conviction│Liq│Agg│Risk  │
│     ORDER BOOK           ├───────────────────────────────────┤
│     (heat ladder)        │ METRICS                           │
│                          │ Bid/Ask│OBI│Depth│Flow│Vol│Detect │
│                          ├───────────────────────────────────┤
│                          │ SIGNALS                           │
│                          │ ▲ BUY YES 0.05 72% Sweep follow  │
├──────────────────────────┴───────────────────────────────────┤
│ ACTIVITY INTELLIGENCE                     │ TAPE             │
│ Timeline ▁▂▃▅▇█▇▅▃▂▁▂▅▇▅▃▁▁▂▃▅▇▇▅▃▂▁   │ TIME S SIZE PX V │
│ Flow ██████████████░░░░ HIGH  +0.83       │ 12:01 B 5.6K .03│
│ Regime: AGGRESSIVE ACCUMULATION           │ 12:00 S  316 .03│
│ Events: Swp ████ 56  Abs ██████████ 76    │                  │
│ Buy  ████████████  $1,408                 │                  │
│ Sell ██           $127    Delta: +$1,281  │                  │
│ ▸ Whale Buy 23K @ 0.03                    │                  │
│ ▸ Absorption @ 0.03 (wall held 92%)       │                  │
│ ▸ 5-level sweep (ask side)                │                  │
├──────────────────────────────────────────────────────────────┤
│ Ctrl+C │ OBI v4.3 │ Session 02:15 │ Msgs 847 │ DB 12t 4s   │
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
from rich.align import Align

from config import settings
from state.orderbook import OrderBook
from data.models import Metrics, Insight, TradeEvent, Side


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

# Sparkline characters (8 levels)
SPARK = "▁▂▃▄▅▆▇█"


class TerminalUI:

    def __init__(self, orderbook: OrderBook, market_question: str = "Loading...", token_label: str = "Yes"):
        self.ob = orderbook
        self.market_question = market_question
        self.token_label = token_label
        self.console = Console()

        self._metrics: Optional[Metrics] = None
        self._insights: deque[Insight] = deque(maxlen=30)
        self._signals: deque = deque(maxlen=6)
        self._connected = False
        self._messages_count = 0
        self._last_update: float = 0
        self._db_stats: dict = {}
        self._start_time: float = time.time()

        # Activity tracking — richer data per tick
        self._activity_window: deque = deque(maxlen=300)
        # Sparkline buckets: 60 one-second buckets for timeline
        self._spark_buckets: list[dict] = [{"buy": 0, "sell": 0, "events": 0, "ts": 0} for _ in range(60)]
        self._spark_last_second: int = 0
        # Event mini-feed (compact event descriptions)
        self._event_feed: deque = deque(maxlen=12)

        # Layout ratios (adjustable with [ ] keys)
        self._left_ratio = 11
        self._right_ratio = 9
        self._book_ratio = 5
        self._activity_ratio = 4

        # Strategy stats (Phase 5)
        self._strategy_stats: dict = {}
        self._rotation_remaining: float = 0
        self._rotation_count: int = 0

        self._live: Optional[Live] = None

    def update_metrics(self, metrics: Metrics):
        self._metrics = metrics
        self._last_update = time.time()
        now = time.time()
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

        # Update sparkline buckets
        if now_sec != self._spark_last_second:
            # Shift buckets
            self._spark_buckets.pop(0)
            self._spark_buckets.append({"buy": 0, "sell": 0, "events": 0, "ts": now_sec})
            self._spark_last_second = now_sec

        bucket = self._spark_buckets[-1]
        event_count = len(metrics.sweep_events) + len(metrics.whale_events) + len(metrics.absorption_events) + len(metrics.spoof_signals)
        bucket["events"] += event_count
        bucket["buy"] += metrics.buy_volume
        bucket["sell"] += metrics.sell_volume

        # Populate event feed from detections
        for sweep in metrics.sweep_events:
            side = "Buy" if sweep.side == Side.BUY else "Sell"
            self._event_feed.append(
                f"[{C_ASK if side == 'Sell' else C_BID}]{side} sweep[/] "
                f"{sweep.levels_consumed}lvl ({sweep.total_volume:,.0f})"
            )
        for whale in metrics.whale_events:
            side = "Buy" if whale.side == Side.BUY else "Sell"
            sc = C_BID if side == "Buy" else C_ASK
            self._event_feed.append(
                f"[{sc}]Whale {side}[/] {_fmt_size(whale.size)} @ {whale.price:.2f}"
            )
        for ab in metrics.absorption_events:
            side = "Bid" if ab.side == Side.BUY else "Ask"
            self._event_feed.append(
                f"[{C_METRIC}]Absorption[/] @ {ab.price:.2f} ({min(ab.holding_pct, 1.0):.0%} held)"
            )
        for sp in metrics.spoof_signals:
            side = "bid" if sp.side == Side.BUY else "ask"
            self._event_feed.append(
                f"[{C_WARN}]Spoof[/] @ {sp.price:.2f} ({side}) {sp.oscillation_count}x"
            )

    def add_insight(self, insight: Insight):
        self._insights.append(insight)

    def add_insights(self, insights: list[Insight]):
        for insight in insights:
            self._insights.append(insight)

    def add_signals(self, signals: list):
        for signal in signals:
            self._signals.append(signal)

    def set_connected(self, connected: bool):
        self._connected = connected

    def set_messages_count(self, count: int):
        self._messages_count = count

    def set_db_stats(self, stats: dict):
        self._db_stats = stats

    def set_strategy_stats(self, stats: dict):
        self._strategy_stats = stats

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

        # Right: state + metrics + paper trading + signals + tape stacked
        layout["right"].split_column(
            Layout(name="state", size=9),
            Layout(name="metrics"),
            Layout(name="paper", size=7),
            Layout(name="signals", size=8),
            Layout(name="tape", size=13),
        )

        layout["header"].update(self._header())
        layout["book"].update(self._orderbook())
        layout["state"].update(self._market_state())
        layout["metrics"].update(self._metrics_panel())
        layout["paper"].update(self._paper_panel())
        layout["signals"].update(self._signals_panel())
        layout["activity"].update(self._activity_intel())
        layout["tape"].update(self._trade_tape())
        layout["status"].update(self._status_bar())

        return layout

    def handle_key(self, key: str):
        """Handle keyboard input for layout resizing."""
        if key == "[":
            # Shrink book, grow activity
            self._book_ratio = max(2, self._book_ratio - 1)
            self._activity_ratio = min(8, self._activity_ratio + 1)
        elif key == "]":
            # Grow book, shrink activity
            self._book_ratio = min(8, self._book_ratio + 1)
            self._activity_ratio = max(2, self._activity_ratio - 1)
        elif key == "{":
            # Shrink left, grow right
            self._left_ratio = max(6, self._left_ratio - 1)
            self._right_ratio = min(14, self._right_ratio + 1)
        elif key == "}":
            # Grow left, shrink right
            self._left_ratio = min(14, self._left_ratio + 1)
            self._right_ratio = max(6, self._right_ratio - 1)

    # ══════════════════════════════════════════════════════════
    # HEADER
    # ══════════════════════════════════════════════════════════

    def _header(self) -> Panel:
        m = self._metrics
        t = Text()

        t.append(" OBI ", style="bold white on grey30")
        t.append("  ")
        t.append(self.market_question[:55], style="bold white")
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

            if m.midpoint:
                t.append(f"Mid {m.midpoint:.4f}", style=C_ACCENT)
                t.append("  │  ", style=C_DIM)
            if m.spread is not None:
                t.append(f"Spd {m.spread:.3f}", style=C_METRIC)
                t.append("  │  ", style=C_DIM)

        if self._connected:
            t.append("●", style="green")
        else:
            t.append("●", style="red")
        latency = int((time.time() - self._last_update) * 1000) if self._last_update else 0
        t.append(f" {latency}ms", style=C_DIM)

        return Panel(t, style=C_PANEL, padding=(0, 0))

    # ══════════════════════════════════════════════════════════
    # ORDER BOOK
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
            ps = "bold red" if is_w else "red"
            if is_w:
                sz = f"[bold red]{sz}[/bold red]"
            elif ask.size / max(mx, 1) < 0.1:
                sz = f"[{C_DIM}]{sz}[/{C_DIM}]"
            table.add_row("", f"[{ps}]{ask.price:.2f}[/{ps}]", f"{sz} {bar}")

        spread = self.ob.spread
        if spread is not None:
            table.add_row("", f"[{C_WARN}]── {spread:.3f} ──[/{C_WARN}]", "")

        for bid in bids:
            is_w = bid.price in wall_px
            sz = _fmt_size(bid.size)
            bar = _heat_bar(bid.size, mx, C_BID)
            ps = "bold green" if is_w else "green"
            if is_w:
                sz = f"[bold green]{sz}[/bold green]"
            elif bid.size / max(mx, 1) < 0.1:
                sz = f"[{C_DIM}]{sz}[/{C_DIM}]"
            table.add_row(f"{bar} {sz}", f"[{ps}]{bid.price:.2f}[/{ps}]", "")

        return Panel(table, title=f"[{C_MUTED}]ORDER BOOK[/{C_MUTED}]", border_style=C_PANEL, padding=(0, 0))

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
    # METRICS
    # ══════════════════════════════════════════════════════════

    def _metrics_panel(self) -> Panel:
        m = self._metrics
        if not m:
            return Panel(f"[{C_DIM}]...[/{C_DIM}]",
                         title=f"[{C_MUTED}]METRICS[/{C_MUTED}]", border_style=C_PANEL)

        t = Table(show_header=False, expand=True, padding=(0, 1), show_edge=False)
        t.add_column("K", style=C_MUTED, ratio=2)
        t.add_column("V", justify="right", ratio=2)

        t.add_row("Bid / Ask",
                   f"[{C_BID}]{m.best_bid:.2f}[/{C_BID}] / [{C_ASK}]{m.best_ask:.2f}[/{C_ASK}]"
                   if m.best_bid and m.best_ask else "—")

        if m.obi is not None:
            t.add_row("OBI", f"{m.obi:.0%} {_imbalance_bar(m.obi)}")

        t.add_row("Depth B/A",
                   f"[{C_BID}]{_fmt_size(m.total_bid_depth)}[/{C_BID}] / "
                   f"[{C_ASK}]{_fmt_size(m.total_ask_depth)}[/{C_ASK}]")

        fc = C_BID if m.flow_pressure > 0.1 else C_ASK if m.flow_pressure < -0.1 else C_MUTED
        t.add_row("Flow",
                   f"[{fc}]{m.flow_pressure:+.2f}[/{fc}] "
                   f"[{C_DIM}]B${m.buy_volume:,.0f} S${m.sell_volume:,.0f}[/{C_DIM}]")

        vc = C_ASK if m.volatility > 0.5 else C_WARN if m.volatility > 0.2 else C_DIM
        t.add_row("Vol", f"[{vc}]{m.volatility:.2f}[/{vc}] {_vol_bar(m.volatility)}")

        if abs(m.price_trend_strength) > 0.05:
            tc = C_BID if m.price_trend_strength > 0 else C_ASK
            t.add_row("Trend", f"[{tc}]{m.price_trend_strength:+.2f}[/{tc}]")

        dets = []
        if m.spoof_signals:
            dets.append(f"[{C_WARN}]{len(m.spoof_signals)} spf[/{C_WARN}]")
        if m.absorption_events:
            dets.append(f"[{C_METRIC}]{len(m.absorption_events)} abs[/{C_METRIC}]")
        if m.sweep_events:
            dets.append(f"[{C_ASK}]{len(m.sweep_events)} swp[/{C_ASK}]")
        if m.walls:
            dets.append(f"[{C_ACCENT}]{len(m.walls)} wall[/{C_ACCENT}]")
        if dets:
            t.add_row("Detect", " ".join(dets))

        return Panel(t, title=f"[{C_MUTED}]METRICS[/{C_MUTED}]", border_style=C_PANEL, padding=(0, 0))

    # ══════════════════════════════════════════════════════════
    # PAPER TRADING — live PnL and position tracking
    # ══════════════════════════════════════════════════════════

    def _paper_panel(self) -> Panel:
        s = self._strategy_stats
        if not s:
            return Panel(f"[{C_DIM}]Paper trading active...[/{C_DIM}]",
                         title=f"[{C_MUTED}]PAPER TRADING[/{C_MUTED}]", border_style=C_PANEL)

        mode = s.get("mode", "paper").upper()
        halted = s.get("halted", False)
        total = s.get("total_trades", 0)
        wins = s.get("wins", 0)
        losses = s.get("losses", 0)
        wr = s.get("win_rate", 0)
        pnl = s.get("total_pnl", 0)
        open_pos = s.get("open_positions", 0)
        exposure = s.get("exposure_usd", 0)

        pnl_color = C_BID if pnl >= 0 else C_ASK
        wr_color = C_BID if wr >= 0.5 else C_ASK if wr < 0.4 else C_WARN
        mode_color = C_WARN if mode == "PAPER" else C_ASK

        lines = []
        # Row 1: mode + halted status
        halt_tag = f"  [{C_ASK}]HALTED[/{C_ASK}]" if halted else ""
        lines.append(
            f"  [{mode_color}]{mode}[/{mode_color}]{halt_tag}"
            f"   [{C_MUTED}]Trades:[/{C_MUTED}] [{C_ACCENT}]{total}[/{C_ACCENT}]"
            f"   [{C_BID}]W{wins}[/{C_BID}]/[{C_ASK}]L{losses}[/{C_ASK}]"
            f"   [{C_MUTED}]WR:[/{C_MUTED}] [{wr_color}]{wr:.0%}[/{wr_color}]"
        )
        # Row 2: PnL + exposure
        lines.append(
            f"  [{C_MUTED}]PnL:[/{C_MUTED}] [{pnl_color}]${pnl:+.2f}[/{pnl_color}]"
            f"   [{C_MUTED}]Open:[/{C_MUTED}] [{C_ACCENT}]{open_pos}[/{C_ACCENT}]"
            f"   [{C_MUTED}]Exp:[/{C_MUTED}] [{C_METRIC}]${exposure:.2f}[/{C_METRIC}]"
        )
        # Row 3: signal stats
        sig_recv = s.get("signals_received", 0)
        sig_filt = s.get("signals_filtered", 0)
        orders = s.get("orders_placed", 0)
        lines.append(
            f"  [{C_MUTED}]Signals:[/{C_MUTED}] {sig_recv} recv  {sig_filt} filtered  {orders} executed"
        )

        return Panel("\n".join(lines), title=f"[{C_MUTED}]PAPER TRADING[/{C_MUTED}]", border_style=C_PANEL, padding=(0, 0))

    # ══════════════════════════════════════════════════════════
    # SIGNALS
    # ══════════════════════════════════════════════════════════

    def _signals_panel(self) -> Panel:
        if not self._signals:
            return Panel(f"[{C_DIM}]Scanning...[/{C_DIM}]",
                         title=f"[{C_MUTED}]SIGNALS[/{C_MUTED}]", border_style=C_PANEL)

        lines = []
        for sig in list(self._signals)[-5:]:
            ac = C_BID if sig.action == "BUY" else C_ASK
            arrow = "▲" if sig.action == "BUY" else "▼"
            cs = C_BID if sig.confidence >= 70 else C_WARN if sig.confidence >= 55 else C_DIM
            lines.append(
                f"[{C_DIM}]{sig.time_str}[/{C_DIM}] "
                f"[{ac}]{arrow} {sig.action} {sig.token} {sig.entry_price:.2f}[/{ac}] "
                f"[{cs}]{sig.confidence}%[/{cs}] "
                f"[{C_MUTED}]{sig.reason[:50]}[/{C_MUTED}]"
            )

        return Panel("\n".join(lines), title=f"[{C_MUTED}]SIGNALS[/{C_MUTED}]", border_style=C_PANEL)

    # ══════════════════════════════════════════════════════════
    # ACTIVITY INTELLIGENCE — the dense panel
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
        fi_bar = _h_bar(flow_intensity, 20, C_METRIC if flow_intensity < 0.6 else C_WARN if flow_intensity < 0.8 else C_ASK)
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

        # ── ROW 6+: Mini Event Feed (newest at bottom) ──
        feed = list(self._event_feed)[-10:]
        if feed:
            lines.append("")
            for ev in feed:
                lines.append(f"  [{C_DIM}]▸[/{C_DIM}] {ev}")

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
        t.append(f" Ctrl+C exit", style=C_DIM)
        t.append(f"  │  ", style=C_DIM)
        t.append(f"[ ] resize V", style=C_DIM)
        t.append(f"  ", style=C_DIM)
        t.append(f"{{ }} resize H", style=C_DIM)
        t.append(f"  │  ", style=C_DIM)
        t.append(f"OBI v4.3", style=C_MUTED)
        t.append(f"  │  ", style=C_DIM)
        t.append(f"Session {mins:02d}:{secs:02d}", style=C_DIM)
        t.append(f"  │  ", style=C_DIM)
        t.append(f"Msgs {self._messages_count}", style=C_DIM)
        if self._db_stats:
            s = self._db_stats
            t.append(f"  │  ", style=C_DIM)
            t.append(f"DB {s.get('trades', 0)}t {s.get('ob_snapshots', 0)}s {s.get('events', 0)}e", style=C_DIM)
        if self._rotation_remaining > 0:
            rm, rs = divmod(int(self._rotation_remaining), 60)
            rc = C_ASK if self._rotation_remaining < 30 else C_WARN if self._rotation_remaining < 60 else C_DIM
            t.append(f"  │  ", style=C_DIM)
            t.append(f"Rotate {rm}:{rs:02d}", style=rc)
            t.append(f" (#{self._rotation_count})", style=C_DIM)
        return t

    # ══════════════════════════════════════════════════════════
    # LIFECYCLE
    # ══════════════════════════════════════════════════════════

    def start(self):
        self._live = Live(
            self.build_layout(), console=self.console,
            refresh_per_second=settings.UI_REFRESH_RATE, screen=True,
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
        sizes = [l.size for l in bids] + [l.size for l in asks]
        return max(sizes) if sizes else 1.0

    def _render_sparkline(self) -> str:
        """Render a 60-char unicode sparkline of activity intensity."""
        values = []
        for b in self._spark_buckets:
            # Intensity = events + normalized volume
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

            # Color: green for buy-dominant seconds, red for sell, yellow for events
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

        # Normalize: events weight + volume weight
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

        # Scoring
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
