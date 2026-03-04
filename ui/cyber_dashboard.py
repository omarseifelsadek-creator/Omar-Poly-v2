"""
cyber_dashboard.py — LVT-style hacker terminal for Synthetic Market Microstructure Engine.

No borders. // SECTION headers. Big braille charts. Neon palette on pure black.
3-column layout composed line-by-line. Cursor-home blit at 2 FPS.
"""

import sys
import os
import time
from io import StringIO

from rich.console import Console
from rich.text import Text

# ══════════════════════════════════════════════════════════════
# NEON PALETTE
# ══════════════════════════════════════════════════════════════

C_CYAN = "#00FFFF"
C_PINK = "#FF1493"
C_LIME = "#00FF41"
C_ORANGE = "#FF6600"
C_PURPLE = "#BF00FF"
C_DIM = "grey50"
C_DIMMER = "grey27"
C_WHITE = "bright_white"
C_RED = "#FF3333"
C_YELLOW = "#FFD700"

# ══════════════════════════════════════════════════════════════
# BRAILLE CANVAS
# ══════════════════════════════════════════════════════════════

BRAILLE_BASE = 0x2800
_DOT_MAP = {
    (0, 0): 0, (0, 1): 1, (0, 2): 2, (0, 3): 6,
    (1, 0): 3, (1, 1): 4, (1, 2): 5, (1, 3): 7,
}


class BrailleCanvas:
    """2x4 dot matrix per char cell using Unicode U+2800-U+28FF."""

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
        dx, dy = abs(x1 - x0), abs(y1 - y0)
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
                        px, py = cx * 2 + dx, cy * 4 + dy
                        if (px, py) in self._grid:
                            val |= (1 << _DOT_MAP[(dx, dy)])
                row.append(chr(val))
            lines.append("".join(row))
        return lines


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def _ftime(s: float) -> str:
    return f"{int(s)//3600:02d}:{(int(s)%3600)//60:02d}:{int(s)%60:02d}"


def _fprice(p: float) -> str:
    return f"${p:.2f}" if p else "—"


def _fdollar(v: float) -> str:
    return f"${v:,.0f}" if abs(v) >= 1000 else f"${v:.0f}"


def _hms(ts_ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts_ms / 1000))


# ══════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════

class CyberDashboard:
    """LVT-style 3-column hacker terminal. No borders, big charts, neon colors."""

    def __init__(self):
        self._on = False
        self.frame = 0

    def start(self):
        # Alt screen + hide cursor + force pure black background
        sys.stdout.write("\033[?1049h\033[?25l\033[48;2;0;0;0m\033[J")
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

        # 3-column widths: left sidebar | center charts | right sidebar
        LEFT_W = min(34, cols // 4)
        RIGHT_W = min(36, cols // 3)
        CENTER_W = cols - LEFT_W - RIGHT_W - 6  # 6 = " │ " * 2
        content_h = rows - 4  # header + 2 separator lines + status bar

        # Build each column as list of (str | Text)
        left = self._build_left(state, LEFT_W, content_h)
        center = self._build_center(state, CENTER_W, content_h)
        right = self._build_right(state, RIGHT_W, content_h)

        # Pad to exact height
        for col in (left, center, right):
            while len(col) < content_h:
                col.append("")

        # Compose frame
        frame: list[Text] = []
        frame.append(self._header_text(state, cols))
        frame.append(Text("═" * cols, style=C_DIMMER))

        sep = Text(" │ ", style=C_DIMMER)
        for i in range(content_h):
            row = Text()
            row.append_text(self._pad(left[i], LEFT_W))
            row.append_text(sep)
            row.append_text(self._pad(center[i], CENTER_W))
            row.append_text(sep)
            row.append_text(self._pad(right[i], RIGHT_W))
            frame.append(row)

        frame.append(Text("═" * cols, style=C_DIMMER))
        frame.append(self._status_text(state, cols))

        # Render through Rich for truecolor ANSI — force black background
        c = Console(
            file=StringIO(), width=cols,
            force_terminal=True, color_system="truecolor", highlight=False,
            style="on #000000",
        )
        with c.capture() as cap:
            for line in frame:
                c.print(line, overflow="crop", no_wrap=True)

        out_lines = cap.get().split("\n")[:rows]
        # \033[48;2;0;0;0m = RGB black bg, persists for \033[J clear-to-end
        sys.stdout.write("\033[H\033[48;2;0;0;0m" + "\n".join(out_lines) + "\033[J")
        sys.stdout.flush()

    def _pad(self, item, width: int) -> Text:
        """Pad markup string or Text to exact visible width."""
        if isinstance(item, Text):
            t = item.copy()
        elif not item:
            return Text(" " * width)
        else:
            t = Text.from_markup(str(item))
        if len(t) > width:
            t.truncate(width)
        pad = width - len(t)
        if pad > 0:
            t.append(" " * pad)
        return t

    # ── HEADER / STATUS BAR ──────────────────────────────────

    def _header_text(self, s, w) -> Text:
        pid = f"0x{os.getpid():05X}"
        if s.connected:
            dot = f"[{C_LIME}]●[/{C_LIME}]"
            tag = f"[{C_LIME}]SYN ENGINE ONLINE[/{C_LIME}]"
        else:
            dot = f"[{C_RED}]●[/{C_RED}]"
            tag = f"[{C_RED}]CONNECTING[/{C_RED}]"
        return Text.from_markup(
            f" {dot} {tag}  [{C_DIMMER}]│[/{C_DIMMER}]  "
            f"[{C_WHITE}]MICRO v1.0[/{C_WHITE}]  [{C_DIMMER}]│[/{C_DIMMER}]  "
            f"[{C_DIM}]{_ftime(s.uptime_s)}[/{C_DIM}]  [{C_DIMMER}]│[/{C_DIMMER}]  "
            f"[{C_DIM}]CYCLE[/{C_DIM}] [{C_CYAN}]#{s.cycle}[/{C_CYAN}]  "
            f"[{C_DIMMER}]│[/{C_DIMMER}]  [{C_DIM}]pid: {pid}[/{C_DIM}]"
        )

    def _status_text(self, s, w) -> Text:
        ws_c = C_LIME if s.connected else C_RED
        ws = "ON" if s.connected else "OFF"
        return Text.from_markup(
            f" [{C_DIM}]WS:[/{C_DIM}] [{ws_c}]{ws}[/{ws_c}]"
            f"  [{C_DIMMER}]│[/{C_DIMMER}]  [{C_DIM}]{s.msg_count:,} msgs[/{C_DIM}]"
            f"  [{C_DIMMER}]│[/{C_DIMMER}]  [{C_CYAN}]{s.msg_rate:.0f}/s[/{C_CYAN}]"
            f"  [{C_DIMMER}]│[/{C_DIMMER}]  [{C_DIM}]{s.latency_ms:.0f}ms[/{C_DIM}]"
            f"  [{C_DIMMER}]│[/{C_DIMMER}]  [{C_DIM}]SYN MID ENGINE v1.0[/{C_DIM}]"
            f"  [{C_DIMMER}]│[/{C_DIMMER}]  [{C_DIM}]Ctrl+C exit[/{C_DIM}]"
        )

    # ── LEFT COLUMN ──────────────────────────────────────────

    def _build_left(self, s, w, h) -> list:
        L = []

        # // LIVE FEED
        L.append(f"[{C_DIM}]// LIVE FEED[/{C_DIM}]")
        feed = s.anomaly_feed[-(h - 12):] if s.anomaly_feed else []
        if not feed:
            L.append(f"  [{C_DIM}]awaiting data…[/{C_DIM}]")
        for item in feed:
            ts = _hms(item["ts"])
            typ = item["type"]
            msg = item["message"]
            color = {
                "SPOOF": C_PINK, "ABSORPTION": C_CYAN, "SWEEP": C_ORANGE,
                "WHALE": C_LIME, "ARB_GAP": C_PURPLE, "TOXIC": C_RED,
            }.get(typ, C_DIM)
            L.append(
                f"  [{C_DIM}]{ts}[/{C_DIM}] "
                f"[{color}][bold]\\[{typ}][/bold][/{color}] "
                f"[{C_DIM}]{msg}[/{C_DIM}]"
            )

        L.append("")

        # // TRIGGERS
        L.append(f"[{C_DIM}]// TRIGGERS[/{C_DIM}]")

        pv = s.price_velocity
        pv_c = C_LIME if pv > 0.001 else C_PINK if pv < -0.001 else C_DIM
        L.append(f"  [{C_DIM}]price_velocity[/{C_DIM}]  [{pv_c}]{pv:+.4f}[/{pv_c}]")

        vs = s.volume_spike
        vs_c = C_ORANGE if vs > 1.5 else C_DIM
        L.append(f"  [{C_DIM}]volume_spike[/{C_DIM}]    [{vs_c}]{vs:.1f}x[/{vs_c}]")

        oi = s.order_imbalance
        oi_c = C_CYAN if oi > 0.6 else C_PINK if oi < 0.4 else C_DIM
        L.append(f"  [{C_DIM}]order_imbalance[/{C_DIM}] [{oi_c}]{oi:.0%}[/{oi_c}]")

        L.append("")

        # // PIPELINE
        L.append(f"[{C_DIM}]// PIPELINE[/{C_DIM}]")
        stages = ["WS", "PARSE", "BOOK", "CALC", "DRAW"]
        parts = []
        for st in stages:
            if st == s.pipeline_stage:
                parts.append(f"[{C_LIME} on grey11]\\[{st}][/{C_LIME} on grey11]")
            else:
                parts.append(f"[{C_DIMMER}]\\[{st}][/{C_DIMMER}]")
        L.append("  " + " ".join(parts))

        return L

    # ── CENTER COLUMN ────────────────────────────────────────

    def _build_center(self, s, w, h) -> list:
        L = []
        chart_cw = max(20, w - 2)

        # Adaptive chart heights
        scatter_h = max(8, (h - 8) * 2 // 5)
        voltage_h = max(5, (h - 8) * 2 // 7)

        # // SCATTER
        L.append(f"[{C_DIM}]// SCATTER — YES vs NO[/{C_DIM}]")
        L.extend(self._scatter_lines(s, chart_cw, scatter_h))

        # thin separator
        L.append(f"[{C_DIMMER}]{'─' * w}[/{C_DIMMER}]")

        # // VOLTAGE
        L.append(f"[{C_DIM}]// VOLTAGE — Price Velocity[/{C_DIM}]")
        L.extend(self._voltage_lines(s, chart_cw, voltage_h))

        # thin separator
        L.append(f"[{C_DIMMER}]{'─' * w}[/{C_DIMMER}]")

        # // FLOW DELTA
        L.append(f"[{C_DIM}]// FLOW DELTA — Cumulative Vol[/{C_DIM}]")
        L.extend(self._flow_lines(s, w - 4))

        return L

    # ── RIGHT COLUMN ─────────────────────────────────────────

    def _build_right(self, s, w, h) -> list:
        R = []

        # // TRADE INTEL
        R.append(f"[{C_DIM}]// TRADE INTEL[/{C_DIM}]")

        pc = s.pair_cost
        if pc > 0:
            pc_c = C_LIME if pc < 0.96 else C_ORANGE if pc < 0.99 else C_RED
            edge_c = C_LIME if s.edge_pct > 0 else C_RED
            R.append(
                f"  [{C_DIM}]pair_cost[/{C_DIM}]  [{pc_c}]${pc:.2f}[/{pc_c}]"
                f"  [{C_DIM}]([/{C_DIM}][{edge_c}]{s.edge_pct:+.1f}%[/{edge_c}][{C_DIM}])[/{C_DIM}]"
            )
        else:
            R.append(f"  [{C_DIM}]pair_cost  —[/{C_DIM}]")

        fav_c = C_CYAN if s.favor == "YES" else C_PINK if s.favor == "NO" else C_DIM
        R.append(
            f"  [{C_DIM}]favor[/{C_DIM}]      [{fav_c}]{s.favor}[/{fav_c}]"
            f"  [{C_DIM}](obi {s.cross_book_obi:.2f})[/{C_DIM}]"
        )

        rc_map = {
            "TRENDING_UP": C_LIME, "TRENDING_DOWN": C_PINK,
            "VOLATILE": C_ORANGE, "BREAKOUT": C_PURPLE,
            "RANGING": C_CYAN, "QUIET": C_DIM,
        }
        rc = rc_map.get(s.regime, C_DIM)
        R.append(f"  [{C_DIM}]regime[/{C_DIM}]     [{rc}]{s.regime}[/{rc}]")
        R.append(f"  [{C_DIM}]syn_mid[/{C_DIM}]    [{C_WHITE}]{_fprice(s.synthetic_mid)}[/{C_WHITE}]")

        gap_c = C_LIME if abs(s.arb_gap) < 0.01 else C_ORANGE if abs(s.arb_gap) < 0.03 else C_RED
        R.append(f"  [{C_DIM}]arb_gap[/{C_DIM}]    [{gap_c}]{s.arb_gap:+.3f}[/{gap_c}]")

        R.append("")

        # // BOOKS
        R.append(f"[{C_DIM}]// BOOKS[/{C_DIM}]")
        R.append(
            f"  [{C_CYAN}]YES[/{C_CYAN}]  "
            f"[{C_DIM}]bid[/{C_DIM}] [{C_LIME}]{_fprice(s.yes_best_bid)}[/{C_LIME}]"
            f" [{C_DIMMER}]│[/{C_DIMMER}] "
            f"[{C_DIM}]ask[/{C_DIM}] [{C_RED}]{_fprice(s.yes_best_ask)}[/{C_RED}]"
        )
        R.append(
            f"  [{C_PINK}]NO [/{C_PINK}]  "
            f"[{C_DIM}]bid[/{C_DIM}] [{C_LIME}]{_fprice(s.no_best_bid)}[/{C_LIME}]"
            f" [{C_DIMMER}]│[/{C_DIMMER}] "
            f"[{C_DIM}]ask[/{C_DIM}] [{C_RED}]{_fprice(s.no_best_ask)}[/{C_RED}]"
        )
        R.append(
            f"  [{C_DIM}]sprd {s.yes_spread:.3f}/{s.no_spread:.3f}"
            f"  dpth {s.yes_total_bid_depth:,.0f}/{s.no_total_bid_depth:,.0f}[/{C_DIM}]"
        )

        R.append("")

        # // YES DEPTH
        R.append(f"[{C_DIM}]// YES DEPTH[/{C_DIM}]")
        R.extend(self._depth_lines(s.yes_bids, s.yes_asks, C_CYAN))

        # // NO DEPTH
        R.append(f"[{C_DIM}]// NO DEPTH[/{C_DIM}]")
        R.extend(self._depth_lines(s.no_bids, s.no_asks, C_PINK))

        R.append("")

        # // RISK LEVELS
        R.append(f"[{C_DIM}]// RISK LEVELS[/{C_DIM}]")
        if not s.level_heatmap:
            R.append(f"  [{C_DIM}]no notable levels[/{C_DIM}]")
        else:
            for lv in s.level_heatmap[:5]:
                cat = lv["category"]
                cc = {"FLICKER": C_PINK, "IRON": C_LIME, "ABSORB": C_CYAN}.get(cat, C_DIM)
                if cat == "FLICKER" and self.frame % 4 < 2:
                    cc = C_DIMMER  # blink
                extra = f"{lv['osc']}x" if cat == "FLICKER" else f"{lv['age_s']:.0f}s"
                R.append(
                    f"  [{cc}]{cat:6s}[/{cc}]  "
                    f"${lv['price']:.2f}  "
                    f"[{C_DIM}]{lv['token']} {lv['side']}  {extra}[/{C_DIM}]"
                )

        R.append("")

        # // SESSION
        R.append(f"[{C_DIM}]// SESSION[/{C_DIM}]")
        R.append(f"  [{C_DIM}]uptime[/{C_DIM}]       [{C_WHITE}]{_ftime(s.uptime_s)}[/{C_WHITE}]")
        R.append(f"  [{C_DIM}]messages[/{C_DIM}]     [{C_WHITE}]{s.msg_count:,}[/{C_WHITE}]")
        R.append(f"  [{C_DIM}]msg_rate[/{C_DIM}]     [{C_CYAN}]{s.msg_rate:.0f}/s[/{C_CYAN}]")
        R.append(f"  [{C_DIM}]latency[/{C_DIM}]      [{C_WHITE}]{s.latency_ms:.0f}ms[/{C_WHITE}]")
        R.append(f"  [{C_DIM}]yes_trades[/{C_DIM}]   [{C_CYAN}]{s.yes_trades:,}[/{C_CYAN}]")
        R.append(f"  [{C_DIM}]no_trades[/{C_DIM}]    [{C_PINK}]{s.no_trades:,}[/{C_PINK}]")

        q = s.market_question
        if len(q) > w - 2:
            q = q[:w - 3] + "…"
        R.append(f"  [{C_DIM}]{q}[/{C_DIM}]")

        return R

    # ── CHART RENDERERS ──────────────────────────────────────

    def _scatter_lines(self, s, cw, ch) -> list:
        points = s.scatter_points
        if len(points) < 3:
            return [f"  [{C_DIM}]collecting data…[/{C_DIM}]"]

        cw = min(cw, 80)
        canvas_y = BrailleCanvas(cw, ch)
        canvas_n = BrailleCanvas(cw, ch)

        times = [p[0] for p in points]
        y_vals = [p[1] for p in points]
        n_vals = [p[2] for p in points]
        all_v = y_vals + n_vals
        v_min, v_max = min(all_v) - 0.01, max(all_v) + 0.01
        v_range = v_max - v_min or 0.01
        t_min = times[0]
        t_range = times[-1] - t_min if len(times) > 1 else 1.0

        prev_ytx, prev_yy, prev_ntx, prev_ny = None, None, None, None
        for t, yv, nv in points:
            tx = int((t - t_min) / t_range * (canvas_y.pw - 1)) if t_range > 0 else 0
            yy = int((1.0 - (yv - v_min) / v_range) * (canvas_y.ph - 1))
            ny = int((1.0 - (nv - v_min) / v_range) * (canvas_n.ph - 1))
            # Connect consecutive points with lines
            if prev_ytx is not None:
                canvas_y.line(prev_ytx, prev_yy, tx, yy)
                canvas_n.line(prev_ntx, prev_ny, tx, ny)
            # Thick dot at each data point (3x3 cluster)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    canvas_y.set(tx + dx, yy + dy)
                    canvas_n.set(tx + dx, ny + dy)
            prev_ytx, prev_yy = tx, yy
            prev_ntx, prev_ny = tx, ny

        yes_r = canvas_y.render()
        no_r = canvas_n.render()
        empty = chr(BRAILLE_BASE)

        result = []
        for i in range(min(len(yes_r), len(no_r))):
            row = Text(" ")
            for j in range(min(len(yes_r[i]), len(no_r[i]))):
                yc, nc = yes_r[i][j], no_r[i][j]
                if yc != empty and nc != empty:
                    row.append(chr(ord(yc) | ord(nc)), style=C_PURPLE)
                elif yc != empty:
                    row.append(yc, style=C_CYAN)
                elif nc != empty:
                    row.append(nc, style=C_PINK)
                else:
                    row.append(empty, style="grey15")
            result.append(row)

        result.append(Text.from_markup(
            f"  [{C_CYAN}]● YES {_fprice(y_vals[-1])}[/{C_CYAN}]"
            f"   [{C_PINK}]● NO {_fprice(n_vals[-1])}[/{C_PINK}]"
        ))
        return result

    def _voltage_lines(self, s, cw, ch) -> list:
        points = s.voltage_points
        if len(points) < 3:
            return [f"  [{C_DIM}]collecting data…[/{C_DIM}]"]

        cw = min(cw, 80)
        canvas = BrailleCanvas(cw, ch)

        times = [p[0] for p in points]
        vals = [p[1] for p in points]
        v_max = max(abs(v) for v in vals) or 0.001
        v_range = v_max * 2
        t_min = times[0]
        t_range = times[-1] - t_min if len(times) > 1 else 1.0

        # Dashed zero line
        zero_y = canvas.ph // 2
        for px in range(0, canvas.pw, 3):
            canvas.set(px, zero_y)

        # Velocity line (thick — draw +1 offset for boldness)
        prev = None
        for t, v in points:
            tx = int((t - t_min) / t_range * (canvas.pw - 1)) if t_range > 0 else 0
            vy = int((1.0 - (v + v_max) / v_range) * (canvas.ph - 1))
            vy = max(0, min(vy, canvas.ph - 1))
            if prev:
                canvas.line(prev[0], prev[1], tx, vy)
                canvas.line(prev[0], prev[1] + 1, tx, vy + 1)  # thickness
            canvas.set(tx, vy)
            prev = (tx, vy)

        result = []
        for row in canvas.render():
            result.append(Text(" " + row, style=C_CYAN))

        curr = vals[-1] if vals else 0
        vc = C_LIME if curr > 0 else C_PINK if curr < 0 else C_DIM
        result.append(Text.from_markup(
            f"  [{C_DIM}]vel:[/{C_DIM}] [{vc}]{curr:+.6f}[/{vc}]"
            f"   [{C_DIM}]EMA(200)[/{C_DIM}]"
        ))
        return result

    def _flow_lines(self, s, bar_w) -> list:
        bars = s.flow_delta_bars
        if not bars:
            return [f"  [{C_DIM}]awaiting trades…[/{C_DIM}]"]

        max_vol = max(max(b, sl) for _, b, sl in bars) or 1
        bw = min(bar_w // 2 - 4, 18)
        if bw < 3:
            bw = 3

        result = []
        for _, buy_v, sell_v in bars[-8:]:
            bl = int(buy_v / max_vol * bw)
            sl = int(sell_v / max_vol * bw)
            net = buy_v - sell_v

            row = Text("  ")
            row.append("█" * bl + "░" * (bw - bl), style=C_LIME)
            row.append("  ", style=C_DIM)
            row.append("░" * (bw - sl) + "█" * sl, style=C_PINK)

            nc = C_LIME if net > 0 else C_PINK
            sign = "+" if net > 0 else ""
            row.append(f"  {sign}{_fdollar(net)}", style=nc)
            result.append(row)

        cd_c = C_LIME if s.cumulative_delta > 0 else C_PINK
        result.append(Text.from_markup(
            f"  [{C_DIM}]cumulative:[/{C_DIM}] [{cd_c}]{_fdollar(s.cumulative_delta)}[/{cd_c}]"
        ))
        return result

    def _depth_lines(self, bids, asks, accent) -> list:
        all_levels = list(bids[:2]) + list(asks[:2])
        if not all_levels:
            return [f"  [{C_DIM}]no data[/{C_DIM}]"]
        max_sz = max(l.size for l in all_levels) or 1
        bw = 10
        result = []
        for level in reversed(asks[:2]):
            fill = int(level.size / max_sz * bw)
            bar = "░" * (bw - fill) + "█" * fill
            result.append(
                f"  [{C_RED}]{bar}[/{C_RED}] "
                f"{level.price:.2f} [{C_DIM}]{level.size:,.0f}[/{C_DIM}]"
            )
        for level in bids[:2]:
            fill = int(level.size / max_sz * bw)
            bar = "█" * fill + "░" * (bw - fill)
            result.append(
                f"  [{accent}]{bar}[/{accent}] "
                f"{level.price:.2f} [{C_DIM}]{level.size:,.0f}[/{C_DIM}]"
            )
        return result
