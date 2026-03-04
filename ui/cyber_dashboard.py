"""
cyber_dashboard.py — Hacker-terminal TUI for Synthetic Market Microstructure Engine.

LVT-style layout: // SECTION headers, braille scatter plots, neon colors,
data-dense sidebars, pipeline status indicators. No heavy borders.

Rendering: Rich Console → capture → ANSI cursor-home blit at 2 FPS.
"""

import sys
import os
import time
from io import StringIO

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns

# ══════════════════════════════════════════════════════════════
# CYBERPUNK PALETTE
# ══════════════════════════════════════════════════════════════

C_CYAN = "#00FFFF"
C_PINK = "#FF1493"
C_LIME = "#00FF41"
C_ORANGE = "#FF6600"
C_PURPLE = "#BF00FF"
C_DIM = "grey37"
C_WHITE = "bright_white"
C_RED = "#FF3333"

# ══════════════════════════════════════════════════════════════
# BRAILLE CANVAS
# ══════════════════════════════════════════════════════════════

BRAILLE_BASE = 0x2800

# Bit mapping for braille dots:
#   col 0: bits 0,1,2,6 (top to bottom)
#   col 1: bits 3,4,5,7
_DOT_MAP = {
    (0, 0): 0, (0, 1): 1, (0, 2): 2, (0, 3): 6,
    (1, 0): 3, (1, 1): 4, (1, 2): 5, (1, 3): 7,
}


class BrailleCanvas:
    """
    Braille-based pixel canvas for terminal charts.
    Each character cell = 2x4 pixel grid using Unicode U+2800-U+28FF.
    """

    def __init__(self, char_width: int = 40, char_height: int = 10):
        self.cw = char_width
        self.ch = char_height
        self.pw = char_width * 2
        self.ph = char_height * 4
        self._grid: set[tuple[int, int]] = set()

    def clear(self):
        self._grid.clear()

    def set(self, px: int, py: int):
        if 0 <= px < self.pw and 0 <= py < self.ph:
            self._grid.add((px, py))

    def line(self, x0: int, y0: int, x1: int, y1: int):
        """Draw a line using Bresenham's algorithm."""
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        while True:
            self.set(x0, y0)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    def render(self) -> list[str]:
        lines = []
        for cy in range(self.ch):
            row = []
            for cx in range(self.cw):
                val = BRAILLE_BASE
                for dx in range(2):
                    for dy in range(4):
                        px = cx * 2 + dx
                        py = cy * 4 + dy
                        if (px, py) in self._grid:
                            val |= (1 << _DOT_MAP[(dx, dy)])
                row.append(chr(val))
            lines.append("".join(row))
        return lines


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def _fmt_time(seconds: float) -> str:
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_price(p: float) -> str:
    return f"${p:.2f}" if p else "—"


def _fmt_dollar(v: float) -> str:
    if abs(v) >= 1000:
        return f"${v:,.0f}"
    return f"${v:.0f}"


def _color_val(val: float, positive_color=C_LIME, negative_color=C_PINK) -> str:
    if val > 0:
        return f"[{positive_color}]+{val:.3f}[/{positive_color}]"
    elif val < 0:
        return f"[{negative_color}]{val:.3f}[/{negative_color}]"
    return f"[{C_DIM}]0.000[/{C_DIM}]"


def _ts_to_hms(ts_ms: int) -> str:
    t = time.localtime(ts_ms / 1000)
    return time.strftime("%H:%M:%S", t)


# ══════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════

class CyberDashboard:
    """
    Hacker-terminal dashboard with LVT-style sections.
    Cursor-home blit rendering at 2 FPS.
    """

    def __init__(self):
        self._on = False
        self.frame = 0

    def start(self):
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()
        self._on = True

    def stop(self):
        if self._on:
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()
            self._on = False

    def render(self, state):
        self.frame += 1

        try:
            cols, rows = os.get_terminal_size()
        except Exception:
            cols, rows = 120, 40
        cols = max(cols, 100)
        rows = max(rows, 30)

        c = Console(
            file=StringIO(), width=cols, height=rows,
            force_terminal=True, color_system="truecolor", highlight=False,
        )

        layout = self._build_layout(state, cols)

        with c.capture() as cap:
            c.print(layout)

        out = cap.get()
        lines = out.split("\n")
        if len(lines) > rows:
            lines = lines[:rows]

        sys.stdout.write("\033[H" + "\n".join(lines) + "\033[J")
        sys.stdout.flush()

    def _build_layout(self, s, cols) -> Layout:
        root = Layout()
        root.split_column(
            Layout(name="header", size=1),
            Layout(name="main", ratio=1),
            Layout(name="status", size=1),
        )

        # Main: left col (feed + charts) | right col (sidebar)
        root["main"].split_row(
            Layout(name="left", ratio=3),
            Layout(name="right", ratio=2),
        )

        # Left: feed top | voltage mid | flow bottom
        root["left"].split_column(
            Layout(name="feed_scatter", ratio=3),
            Layout(name="voltage", ratio=2),
            Layout(name="flow", ratio=2),
        )

        # Feed + scatter side by side
        root["feed_scatter"].split_row(
            Layout(name="feed", ratio=2),
            Layout(name="scatter", ratio=3),
        )

        # Right sidebar: trade_intel | books | risk | session
        root["right"].split_column(
            Layout(name="trade_intel", ratio=2),
            Layout(name="books", ratio=2),
            Layout(name="risk_levels", ratio=2),
            Layout(name="session", ratio=2),
        )

        # Populate
        root["header"].update(self._header(s))
        root["feed"].update(self._feed(s))
        root["scatter"].update(self._scatter(s))
        root["voltage"].update(self._voltage(s))
        root["flow"].update(self._flow_delta(s))
        root["trade_intel"].update(self._trade_intel(s))
        root["books"].update(self._books(s))
        root["risk_levels"].update(self._risk_levels(s))
        root["session"].update(self._session(s))
        root["status"].update(self._status_bar(s))

        return root

    # ── HEADER ──
    def _header(self, s) -> Text:
        pid = f"0x{os.getpid():05X}"
        dot = f"[{C_LIME}]*[/{C_LIME}]" if s.connected else f"[{C_PINK}]*[/{C_PINK}]"
        status = f"[{C_LIME}]SYN ENGINE ONLINE[/{C_LIME}]" if s.connected else f"[{C_PINK}]CONNECTING[/{C_PINK}]"

        line = Text.from_markup(
            f"  {dot} {status}  [{C_DIM}]|[/{C_DIM}]  "
            f"[{C_WHITE}]MICRO v1.0[/{C_WHITE}]  [{C_DIM}]|[/{C_DIM}]  "
            f"[{C_DIM}]{_fmt_time(s.uptime_s)}[/{C_DIM}]  [{C_DIM}]|[/{C_DIM}]  "
            f"[{C_DIM}]CYCLE[/{C_DIM}] [{C_CYAN}]#{s.cycle}[/{C_CYAN}]  [{C_DIM}]|[/{C_DIM}]  "
            f"[{C_DIM}]pid:[/{C_DIM}] [{C_DIM}]{pid}[/{C_DIM}]"
        )
        return line

    # ── LIVE FEED (left top) ──
    def _feed(self, s) -> Panel:
        lines = []

        # Section: anomaly log
        lines.append(Text.from_markup(f"[{C_DIM}]// LIVE FEED[/{C_DIM}]"))

        feed_items = s.anomaly_feed[-10:] if s.anomaly_feed else []
        if not feed_items:
            lines.append(Text.from_markup(f"  [{C_DIM}]awaiting data...[/{C_DIM}]"))
        for item in feed_items:
            ts = _ts_to_hms(item["ts"])
            typ = item["type"]
            msg = item["message"]
            color = {
                "SPOOF": C_PINK, "ABSORPTION": C_CYAN, "SWEEP": C_ORANGE,
                "WHALE": C_LIME, "ARB_GAP": C_PURPLE, "TOXIC": C_PINK,
            }.get(typ, C_DIM)
            lines.append(Text.from_markup(
                f"  [{C_DIM}]{ts}[/{C_DIM}] [{color}][{typ}][/{color}] {msg}"
            ))

        # Section: triggers
        lines.append(Text(""))
        lines.append(Text.from_markup(f"[{C_DIM}]// TRIGGERS[/{C_DIM}]"))
        pv_color = C_LIME if abs(s.price_velocity) > 0.001 else C_DIM
        vs_color = C_ORANGE if s.volume_spike > 1.5 else C_DIM
        oi_val = s.order_imbalance
        oi_color = C_CYAN if oi_val > 0.6 else C_PINK if oi_val < 0.4 else C_DIM
        lines.append(Text.from_markup(f"  [{C_DIM}]price_velocity[/{C_DIM}]   [{pv_color}]{s.price_velocity:+.4f}[/{pv_color}]"))
        lines.append(Text.from_markup(f"  [{C_DIM}]volume_spike[/{C_DIM}]     [{vs_color}]{s.volume_spike:.1f}x[/{vs_color}]"))
        lines.append(Text.from_markup(f"  [{C_DIM}]order_imbalance[/{C_DIM}]  [{oi_color}]{oi_val:.0%}[/{oi_color}]"))

        # Section: pipeline
        lines.append(Text(""))
        lines.append(Text.from_markup(f"[{C_DIM}]// PIPELINE[/{C_DIM}]"))
        stages = ["WS", "PARSE", "BOOK", "CALC", "DRAW"]
        parts = []
        for stage in stages:
            if stage == s.pipeline_stage:
                parts.append(f"[{C_LIME}][{stage}][/{C_LIME}]")
            else:
                parts.append(f"[{C_DIM}][{stage}][/{C_DIM}]")
        lines.append(Text.from_markup("  " + " ".join(parts)))

        content = Text("\n").join(lines)
        return Panel(content, border_style=C_DIM, padding=(0, 1))

    # ── SCATTER PLOT (right top) ──
    def _scatter(self, s) -> Panel:
        lines = []
        lines.append(Text.from_markup(f"[{C_DIM}]// SCATTER — YES vs NO[/{C_DIM}]"))

        points = s.scatter_points
        if len(points) < 3:
            lines.append(Text.from_markup(f"  [{C_DIM}]collecting data...[/{C_DIM}]"))
            content = Text("\n").join(lines)
            return Panel(content, border_style=C_DIM, padding=(0, 1))

        # Get chart dimensions from terminal
        chart_w = 36
        chart_h = 8
        canvas = BrailleCanvas(chart_w, chart_h)

        # Extract yes/no bids over time
        times = [p[0] for p in points]
        yes_vals = [p[1] for p in points]
        no_vals = [p[2] for p in points]

        all_vals = yes_vals + no_vals
        v_min = min(all_vals) - 0.01
        v_max = max(all_vals) + 0.01
        v_range = v_max - v_min if v_max > v_min else 0.01
        t_min = times[0]
        t_range = times[-1] - t_min if len(times) > 1 else 1.0

        # Plot YES dots
        for t, yv, nv in points:
            tx = int((t - t_min) / t_range * (canvas.pw - 1)) if t_range > 0 else 0
            # YES dot
            yy = int((1.0 - (yv - v_min) / v_range) * (canvas.ph - 1))
            canvas.set(tx, yy)

        yes_text = canvas.render()

        # Reset and plot NO dots on a separate canvas for coloring
        canvas2 = BrailleCanvas(chart_w, chart_h)
        for t, yv, nv in points:
            tx = int((t - t_min) / t_range * (canvas2.pw - 1)) if t_range > 0 else 0
            ny = int((1.0 - (nv - v_min) / v_range) * (canvas2.ph - 1))
            canvas2.set(tx, ny)

        no_text = canvas2.render()

        # Overlay: combine both into colored text
        for i in range(min(len(yes_text), len(no_text))):
            combined = Text()
            for j in range(min(len(yes_text[i]), len(no_text[i]))):
                yc = yes_text[i][j]
                nc = no_text[i][j]
                if yc != chr(BRAILLE_BASE) and nc != chr(BRAILLE_BASE):
                    # Both have dots — merge braille characters
                    merged = chr(ord(yc) | ord(nc))
                    combined.append(merged, style=C_PURPLE)
                elif yc != chr(BRAILLE_BASE):
                    combined.append(yc, style=C_CYAN)
                elif nc != chr(BRAILLE_BASE):
                    combined.append(nc, style=C_PINK)
                else:
                    combined.append(chr(BRAILLE_BASE), style=C_DIM)
            lines.append(combined)

        # Axis labels
        lines.append(Text.from_markup(
            f"  [{C_CYAN}]YES {_fmt_price(yes_vals[-1])}[/{C_CYAN}]"
            f"  [{C_PINK}]NO {_fmt_price(no_vals[-1])}[/{C_PINK}]"
        ))

        content = Text("\n").join(lines)
        return Panel(content, border_style=C_DIM, padding=(0, 1))

    # ── VOLTAGE (left mid) ──
    def _voltage(self, s) -> Panel:
        lines = []
        lines.append(Text.from_markup(f"[{C_DIM}]// VOLTAGE — Price Velocity[/{C_DIM}]"))

        points = s.voltage_points
        if len(points) < 3:
            lines.append(Text.from_markup(f"  [{C_DIM}]collecting data...[/{C_DIM}]"))
            content = Text("\n").join(lines)
            return Panel(content, border_style=C_DIM, padding=(0, 1))

        chart_w = 40
        chart_h = 6
        canvas = BrailleCanvas(chart_w, chart_h)

        times = [p[0] for p in points]
        vals = [p[1] for p in points]

        v_max = max(abs(v) for v in vals) or 0.001
        v_range = v_max * 2
        t_min = times[0]
        t_range = times[-1] - t_min if len(times) > 1 else 1.0

        # Zero line
        zero_y = canvas.ph // 2
        for px in range(0, canvas.pw, 4):
            canvas.set(px, zero_y)

        # Plot velocity line
        prev_px, prev_py = None, None
        for t, v in points:
            tx = int((t - t_min) / t_range * (canvas.pw - 1)) if t_range > 0 else 0
            vy = int((1.0 - (v + v_max) / v_range) * (canvas.ph - 1))
            vy = max(0, min(vy, canvas.ph - 1))
            if prev_px is not None:
                canvas.line(prev_px, prev_py, tx, vy)
            prev_px, prev_py = tx, vy

        rendered = canvas.render()
        for row in rendered:
            # Color based on whether line is above or below zero
            lines.append(Text(row, style=C_CYAN))

        # Current velocity label
        curr_v = vals[-1] if vals else 0
        v_color = C_LIME if curr_v > 0 else C_PINK if curr_v < 0 else C_DIM
        lines.append(Text.from_markup(
            f"  [{C_DIM}]vel:[/{C_DIM}] [{v_color}]{curr_v:+.6f}[/{v_color}]"
            f"  [{C_DIM}]EMA(200)[/{C_DIM}]"
        ))

        content = Text("\n").join(lines)
        return Panel(content, border_style=C_DIM, padding=(0, 1))

    # ── FLOW DELTA (left bottom) ──
    def _flow_delta(self, s) -> Panel:
        lines = []
        lines.append(Text.from_markup(f"[{C_DIM}]// FLOW DELTA — Cumulative Vol[/{C_DIM}]"))

        bars = s.flow_delta_bars
        if not bars:
            lines.append(Text.from_markup(f"  [{C_DIM}]awaiting trades...[/{C_DIM}]"))
            content = Text("\n").join(lines)
            return Panel(content, border_style=C_DIM, padding=(0, 1))

        # Find max for scaling
        max_vol = max(max(b, sl) for _, b, sl in bars) if bars else 1
        if max_vol == 0:
            max_vol = 1
        bar_width = 20

        recent = bars[-8:]
        for _, buy_v, sell_v in recent:
            buy_len = int(buy_v / max_vol * bar_width)
            sell_len = int(sell_v / max_vol * bar_width)
            net = buy_v - sell_v

            buy_bar = "█" * buy_len + "░" * (bar_width - buy_len)
            sell_bar = "░" * (bar_width - sell_len) + "█" * sell_len

            net_color = C_LIME if net > 0 else C_PINK
            net_str = f"[{net_color}]{'+' if net > 0 else ''}{_fmt_dollar(net)}[/{net_color}]"

            line = Text()
            line.append(f"  {buy_bar[:bar_width]} ", style=C_LIME)
            line.append(f"{sell_bar[:bar_width]} ", style=C_PINK)
            line.append_text(Text.from_markup(net_str))
            lines.append(line)

        # Cumulative
        cd_color = C_LIME if s.cumulative_delta > 0 else C_PINK
        lines.append(Text.from_markup(
            f"  [{C_DIM}]cumulative:[/{C_DIM}] [{cd_color}]{_fmt_dollar(s.cumulative_delta)}[/{cd_color}]"
        ))

        content = Text("\n").join(lines)
        return Panel(content, border_style=C_DIM, padding=(0, 1))

    # ── TRADE INTEL (right sidebar top) ──
    def _trade_intel(self, s) -> Panel:
        lines = []
        lines.append(Text.from_markup(f"[{C_DIM}]// TRADE INTEL[/{C_DIM}]"))

        # Pair cost + edge
        pc = s.pair_cost
        if pc > 0:
            pc_color = C_LIME if pc < 0.96 else C_ORANGE if pc < 0.99 else C_RED
            lines.append(Text.from_markup(
                f"  [{C_DIM}]pair_cost[/{C_DIM}]  [{pc_color}]${pc:.2f}[/{pc_color}]"
                f"  [{C_DIM}](edge[/{C_DIM}] [{C_LIME}]{s.edge_pct:.1f}%[/{C_LIME}][{C_DIM}])[/{C_DIM}]"
            ))
        else:
            lines.append(Text.from_markup(f"  [{C_DIM}]pair_cost  —[/{C_DIM}]"))

        # Favor
        fav_color = C_CYAN if s.favor == "YES" else C_PINK if s.favor == "NO" else C_DIM
        lines.append(Text.from_markup(
            f"  [{C_DIM}]favor[/{C_DIM}]      [{fav_color}]{s.favor}[/{fav_color}]"
            f"  [{C_DIM}](obi {s.cross_book_obi:.2f})[/{C_DIM}]"
        ))

        # Regime
        regime_colors = {
            "TRENDING_UP": C_LIME, "TRENDING_DOWN": C_PINK,
            "VOLATILE": C_ORANGE, "BREAKOUT": C_PURPLE,
            "RANGING": C_CYAN, "QUIET": C_DIM,
        }
        rc = regime_colors.get(s.regime, C_DIM)
        lines.append(Text.from_markup(
            f"  [{C_DIM}]regime[/{C_DIM}]     [{rc}]{s.regime}[/{rc}]"
        ))

        # Synthetic mid + arb gap
        lines.append(Text.from_markup(
            f"  [{C_DIM}]syn_mid[/{C_DIM}]    [{C_WHITE}]{_fmt_price(s.synthetic_mid)}[/{C_WHITE}]"
        ))

        gap_color = C_LIME if abs(s.arb_gap) < 0.01 else C_ORANGE if abs(s.arb_gap) < 0.03 else C_RED
        lines.append(Text.from_markup(
            f"  [{C_DIM}]arb_gap[/{C_DIM}]    [{gap_color}]{s.arb_gap:+.3f}[/{gap_color}]"
        ))

        content = Text("\n").join(lines)
        return Panel(content, border_style=C_DIM, padding=(0, 1))

    # ── BOOKS (right sidebar mid) ──
    def _books(self, s) -> Panel:
        lines = []
        lines.append(Text.from_markup(f"[{C_DIM}]// BOOKS[/{C_DIM}]"))

        # YES line
        lines.append(Text.from_markup(
            f"  [{C_CYAN}]YES[/{C_CYAN}]  "
            f"[{C_DIM}]bid[/{C_DIM}] [{C_LIME}]{_fmt_price(s.yes_best_bid)}[/{C_LIME}]"
            f" [{C_DIM}]|[/{C_DIM}] "
            f"[{C_DIM}]ask[/{C_DIM}] [{C_RED}]{_fmt_price(s.yes_best_ask)}[/{C_RED}]"
        ))
        # NO line
        lines.append(Text.from_markup(
            f"  [{C_PINK}]NO [/{C_PINK}]  "
            f"[{C_DIM}]bid[/{C_DIM}] [{C_LIME}]{_fmt_price(s.no_best_bid)}[/{C_LIME}]"
            f" [{C_DIM}]|[/{C_DIM}] "
            f"[{C_DIM}]ask[/{C_DIM}] [{C_RED}]{_fmt_price(s.no_best_ask)}[/{C_RED}]"
        ))

        # Spreads + depths
        lines.append(Text.from_markup(
            f"  [{C_DIM}]sprd[/{C_DIM}] {s.yes_spread:.3f}/{s.no_spread:.3f}"
            f"  [{C_DIM}]dpth[/{C_DIM}] {s.yes_total_bid_depth:,.0f}/{s.no_total_bid_depth:,.0f}"
        ))

        # Mini depth bars
        lines.append(Text(""))
        lines.append(Text.from_markup(f"  [{C_DIM}]// YES DEPTH[/{C_DIM}]"))
        self._render_mini_book(lines, s.yes_bids, s.yes_asks, C_CYAN)

        lines.append(Text.from_markup(f"  [{C_DIM}]// NO DEPTH[/{C_DIM}]"))
        self._render_mini_book(lines, s.no_bids, s.no_asks, C_PINK)

        content = Text("\n").join(lines)
        return Panel(content, border_style=C_DIM, padding=(0, 1))

    def _render_mini_book(self, lines, bids, asks, color):
        """Render compact book depth bars."""
        max_sz = 1
        for level in list(bids[:3]) + list(asks[:3]):
            if level.size > max_sz:
                max_sz = level.size

        bar_w = 12
        # Top 3 asks (reversed so lowest ask is closest to center)
        for level in reversed(asks[:3]):
            fill = int(level.size / max_sz * bar_w)
            bar = "░" * (bar_w - fill) + "█" * fill
            lines.append(Text.from_markup(
                f"  [{C_RED}]{bar}[/{C_RED}] {level.price:.2f} [{C_DIM}]{level.size:,.0f}[/{C_DIM}]"
            ))

        # Top 3 bids
        for level in bids[:3]:
            fill = int(level.size / max_sz * bar_w)
            bar = "█" * fill + "░" * (bar_w - fill)
            lines.append(Text.from_markup(
                f"  [{color}]{bar}[/{color}] {level.price:.2f} [{C_DIM}]{level.size:,.0f}[/{C_DIM}]"
            ))

    # ── RISK LEVELS (right sidebar) ──
    def _risk_levels(self, s) -> Panel:
        lines = []
        lines.append(Text.from_markup(f"[{C_DIM}]// RISK LEVELS[/{C_DIM}]"))

        if not s.level_heatmap:
            lines.append(Text.from_markup(f"  [{C_DIM}]no notable levels[/{C_DIM}]"))
        else:
            for lv in s.level_heatmap[:6]:
                cat = lv["category"]
                cat_colors = {
                    "FLICKER": C_PINK, "IRON": C_LIME, "ABSORB": C_CYAN,
                }
                cc = cat_colors.get(cat, C_DIM)

                # Blink flickers
                if cat == "FLICKER" and self.frame % 4 < 2:
                    cc = C_DIM

                extra = f" {lv['osc']}x" if cat == "FLICKER" else f" {lv['age_s']:.0f}s"
                lines.append(Text.from_markup(
                    f"  [{cc}]{cat:6s}[/{cc}] "
                    f"${lv['price']:.2f} "
                    f"[{C_DIM}]{lv['token']} {lv['side']}[/{C_DIM}]"
                    f" [{C_DIM}]{extra}[/{C_DIM}]"
                ))

        content = Text("\n").join(lines)
        return Panel(content, border_style=C_DIM, padding=(0, 1))

    # ── SESSION (right sidebar bottom) ──
    def _session(self, s) -> Panel:
        lines = []
        lines.append(Text.from_markup(f"[{C_DIM}]// SESSION[/{C_DIM}]"))
        lines.append(Text.from_markup(f"  [{C_DIM}]uptime[/{C_DIM}]       [{C_WHITE}]{_fmt_time(s.uptime_s)}[/{C_WHITE}]"))
        lines.append(Text.from_markup(f"  [{C_DIM}]messages[/{C_DIM}]     [{C_WHITE}]{s.msg_count:,}[/{C_WHITE}]"))
        lines.append(Text.from_markup(f"  [{C_DIM}]msg_rate[/{C_DIM}]     [{C_CYAN}]{s.msg_rate:.0f}/s[/{C_CYAN}]"))
        lines.append(Text.from_markup(f"  [{C_DIM}]latency[/{C_DIM}]      [{C_WHITE}]{s.latency_ms:.0f}ms[/{C_WHITE}]"))
        lines.append(Text.from_markup(f"  [{C_DIM}]yes_trades[/{C_DIM}]   [{C_CYAN}]{s.yes_trades:,}[/{C_CYAN}]"))
        lines.append(Text.from_markup(f"  [{C_DIM}]no_trades[/{C_DIM}]    [{C_PINK}]{s.no_trades:,}[/{C_PINK}]"))

        # Market question (truncated)
        q = s.market_question[:40] + "..." if len(s.market_question) > 40 else s.market_question
        lines.append(Text.from_markup(f"  [{C_DIM}]{q}[/{C_DIM}]"))

        content = Text("\n").join(lines)
        return Panel(content, border_style=C_DIM, padding=(0, 1))

    # ── STATUS BAR ──
    def _status_bar(self, s) -> Text:
        ws_color = C_LIME if s.connected else C_PINK
        ws_label = "ON" if s.connected else "OFF"

        return Text.from_markup(
            f"  [{C_DIM}]WS:[/{C_DIM}] [{ws_color}]{ws_label}[/{ws_color}]"
            f"  [{C_DIM}]|[/{C_DIM}]  "
            f"[{C_DIM}]{s.msg_count:,} msgs[/{C_DIM}]"
            f"  [{C_DIM}]|[/{C_DIM}]  "
            f"[{C_CYAN}]{s.msg_rate:.0f}/s[/{C_CYAN}]"
            f"  [{C_DIM}]|[/{C_DIM}]  "
            f"[{C_DIM}]{s.latency_ms:.0f}ms[/{C_DIM}]"
            f"  [{C_DIM}]|[/{C_DIM}]  "
            f"[{C_DIM}]SYN MID ENGINE v1.0[/{C_DIM}]"
            f"  [{C_DIM}]|[/{C_DIM}]  "
            f"[{C_DIM}]Ctrl+C exit[/{C_DIM}]"
        )
