"""
pair_dashboard.py — Bloomberg-style Terminal Dashboard v2

8 Panels: Header, Pair Core, Books+Zones, Inventory, Execution,
          Market Flow Tape, Bot Execution Tape, Session P&L

Rendering: rich.Console → capture → ANSI cursor-home blit at 2 FPS.
"""

import sys
import time
import os
from io import StringIO

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# ──────────────────────────────────────────────────────
# STATE BUILDER (crash-proof)
# ──────────────────────────────────────────────────────

def build_state(runner) -> dict:
    try:
        stats = runner.engine.get_stats()
    except Exception:
        stats = {}

    def s(key, default=0):
        val = stats.get(key)
        return val if val is not None else default

    try:
        ya, na = runner.yes_book.best_ask or 0, runner.no_book.best_ask or 0
        yb, nb = runner.yes_book.best_bid or 0, runner.no_book.best_bid or 0
        ym, nm = runner.yes_book.midpoint or 0, runner.no_book.midpoint or 0
        ys, ns = runner.yes_book.spread or 0, runner.no_book.spread or 0
        # L1 depth (size at best bid/ask)
        y_bids = runner.yes_book.get_sorted_bids(1)
        y_asks = runner.yes_book.get_sorted_asks(1)
        n_bids = runner.no_book.get_sorted_bids(1)
        n_asks = runner.no_book.get_sorted_asks(1)
        ybs = y_bids[0].size if y_bids else 0
        yas_sz = y_asks[0].size if y_asks else 0
        nbs = n_bids[0].size if n_bids else 0
        nas_sz = n_asks[0].size if n_asks else 0
        # Total depth for OBI
        y_bd = runner.yes_book.total_bid_depth
        y_ad = runner.yes_book.total_ask_depth
        n_bd = runner.no_book.total_bid_depth
        n_ad = runner.no_book.total_ask_depth
    except Exception:
        ya = na = yb = nb = ym = nm = ys = ns = 0
        ybs = yas_sz = nbs = nas_sz = 0
        y_bd = y_ad = n_bd = n_ad = 0

    mkt = runner.spec.display_name_long
    try:
        if runner.window:
            mkt = f"{runner.spec.display_name} — {runner.window.time_label}"
    except Exception:
        pass

    return {
        "market": mkt,
        "t": s("time_remaining"),
        "wn": getattr(runner, "windows_traded", 0) + 1,
        "msgs": getattr(runner, "_msg_count", 0),
        "ws_t": getattr(runner, "_last_ws_time", 0),
        "vol": getattr(runner, "_window_volume", 0),
        "yes": {"bid": yb, "ask": ya, "mid": ym, "spread": ys,
                "bid_sz": ybs, "ask_sz": yas_sz, "bid_depth": y_bd, "ask_depth": y_ad},
        "no": {"bid": nb, "ask": na, "mid": nm, "spread": ns,
               "bid_sz": nbs, "ask_sz": nas_sz, "bid_depth": n_bd, "ask_depth": n_ad},
        "ya_age": s("yes_ask_age_ms"), "na_age": s("no_ask_age_ms"),
        "yq": s("yes_qty"), "nq": s("no_qty"),
        "yavg": s("yes_avg"), "navg": s("no_avg"),
        "pc": s("pair_cost"), "mp": s("matched_pairs"),
        "skew": s("skew"), "cap": s("total_capital"),
        "cap_limit": s("max_position_usd", 100),
        "yl": s("yes_locked", False), "nl": s("no_locked", False),
        "panic": s("in_panic", False),
        "exe": s("buys_executed"), "filt": s("buys_filtered"),
        "fa": s("fills_attempted"), "fr": s("fills_rejected"),
        "pf": s("partial_fills"),
        "fb": s("filter_reasons", {}),
        "buys": getattr(runner, "_recent_buys_display", []),
        "tape": list(getattr(runner, "_market_tape", [])),
        "sess": {
            "pnl": getattr(runner, "cumulative_pnl", 0),
            "wt": getattr(runner, "windows_traded", 0),
            "wp": getattr(runner, "windows_profitable", 0),
            "tp": getattr(runner, "total_pairs", 0),
            "last": getattr(runner, "_last_window_result", {}),
        },
        "panic_s": runner.spec.panic_time_seconds,
        "half_s": runner.spec.theta_half_size_until_s,
    }


# ──────────────────────────────────────────────────────
# DASHBOARD
# ──────────────────────────────────────────────────────

class PairDashboard:
    def __init__(self):
        self.blink = False
        self.frame = 0
        self._on = False

    def start(self):
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()
        self._on = True

    def stop(self):
        if self._on:
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()
            self._on = False

    def render(self, st: dict):
        self.frame += 1
        self.blink = self.frame % 2 == 0

        try:
            cols, rows = os.get_terminal_size()
        except Exception:
            cols, rows = 120, 40
        cols, rows = max(cols, 80), max(rows, 24)

        c = Console(
            file=StringIO(), width=cols, height=rows,
            force_terminal=True, color_system="256", highlight=False,
        )
        with c.capture() as cap:
            c.print(self._layout(st))

        out = cap.get()
        lines = out.split("\n")
        if len(lines) > rows:
            lines = lines[:rows]

        sys.stdout.write("\033[H" + "\n".join(lines) + "\033[J")
        sys.stdout.flush()

    # ── LAYOUT ──────────────────────────────────────

    def _layout(self, s: dict) -> Layout:
        r = Layout()
        r.split_column(
            Layout(name="hdr", size=3),
            Layout(name="top", ratio=3),
            Layout(name="mid", ratio=3),
            Layout(name="bot", ratio=3),
        )
        r["top"].split_row(
            Layout(name="books", ratio=2),
            Layout(name="core", ratio=3),
        )
        r["mid"].split_row(
            Layout(name="inv", ratio=1),
            Layout(name="exec", ratio=1),
            Layout(name="tape", ratio=2),
        )
        r["bot"].split_row(
            Layout(name="bot_tape", ratio=3),
            Layout(name="sess", ratio=2),
        )

        r["hdr"].update(self._header(s))
        r["books"].update(self._books(s))
        r["core"].update(self._core(s))
        r["inv"].update(self._inv(s))
        r["exec"].update(self._exec(s))
        r["tape"].update(self._flow_tape(s))
        r["bot_tape"].update(self._bot_tape(s))
        r["sess"].update(self._session(s))
        return r

    # ── 1. HEADER ───────────────────────────────────

    def _header(self, s: dict) -> Panel:
        t_rem = s.get("t", 0)
        panic = s.get("panic", False)
        lag = (time.time() - s.get("ws_t", 0)) * 1000 if s.get("ws_t", 0) > 0 else 0

        lc = "green" if lag < 100 else "yellow" if lag < 300 else "bold red"
        half_s = s.get("half_s", 30)
        panic_s = s.get("panic_s", 10)
        tc = "bold green" if t_rem > half_s else "bold yellow" if t_rem > panic_s else ("bold red" if self.blink else "dim red")

        tx = Text()
        tx.append(" ⚡ ", style="bold cyan")
        tx.append(s.get("market", ""), style="bold cyan")
        tx.append(f"  W#{s.get('wn',0)}", style="dim")
        tx.append("  T-", style="dim")
        t_str = f"{t_rem/60:.1f}m" if t_rem >= 120 else f"{t_rem:.0f}s"
        tx.append(t_str, style=tc)
        tx.append(f"  Msgs:{s.get('msgs',0):,}", style="dim")
        tx.append("  Lag:", style="dim")
        tx.append(f"{lag:.0f}ms", style=lc)
        vol = s.get("vol", 0)
        tx.append("  Vol:", style="dim")
        tx.append(f"{vol:,.0f}", style="bold white")

        bc = "bold red" if panic and self.blink else "cyan"
        return Panel(tx, border_style=bc, height=3)

    # ── 2. PAIR CORE ───────────────────────────────

    def _core(self, s: dict) -> Panel:
        pc = s.get("pc", 0)
        mp = s.get("mp", 0)
        skew = s.get("skew", 0)
        cap = s.get("cap", 0)
        panic = s.get("panic", False)

        tgt = 1.02 if panic else 0.96
        lbl = f"Panic:{tgt}" if panic else f"Cap:{tgt}"

        if pc <= 0:
            cs = "dim"
        elif pc < 0.90:
            cs = "bold green"
        elif pc < 0.95:
            cs = "bold yellow"
        else:
            cs = "bold red" if self.blink else "red"

        ratio = min(pc / tgt, 1.0) if tgt > 0 and pc > 0 else 0
        bc = "green" if pc < 0.90 else "yellow" if pc < 0.95 else "red"
        bw = 30
        filled = int(ratio * bw)

        tx = Text()
        tx.append("\n  PAIR COST  ", style="dim")
        tx.append(f"${pc:.4f}" if pc > 0 else "—", style=cs)
        tx.append(f"  ({lbl})\n  ", style="dim")
        tx.append("█" * filled, style=bc)
        tx.append("░" * (bw - filled), style="dim")
        tx.append(f" {ratio*100:.0f}%\n\n", style=bc)
        tx.append(f"  Pairs:", style="dim")
        tx.append(f"{mp:.0f}", style="bold")
        tx.append(f"  Skew:", style="dim")
        ss = "green" if skew < 0.15 else "yellow" if skew < 0.3 else "bold red"
        tx.append(f"{skew:.2f}", style=ss)

        # Capital with limit indicator
        cap_limit = s.get("cap_limit", 100)
        cap_pct = (cap / cap_limit * 100) if cap_limit > 0 else 0
        if cap >= cap_limit:
            cap_sty = "bold red"
            cap_tag = " MAXED"
        elif cap_pct > 85:
            cap_sty = "bold yellow"
            cap_tag = ""
        else:
            cap_sty = "cyan"
            cap_tag = ""
        tx.append(f"\n  Capital:", style="dim")
        tx.append(f"${cap:.0f}", style=cap_sty)
        tx.append(f"/${cap_limit:.0f}", style="dim")
        if cap_tag:
            tx.append(cap_tag, style="bold red" if self.blink else "dim red")

        tx.append(f"\n  YES:", style="dim")
        tx.append(f"${s.get('yavg',0):.4f}", style="green")
        tx.append(f"  NO:", style="dim")
        tx.append(f"${s.get('navg',0):.4f}", style="red")

        title = "[bold]═ PAIR ENGINE ═[/]"
        brd = "bold cyan"
        if panic:
            title = "[bold red]⚠ PANIC: AGGRESSIVE MATCHING ⚠[/]"
            brd = "bold red" if self.blink else "yellow"

        return Panel(tx, title=title, border_style=brd)

    # ── 3. ORDER BOOKS + ZONES ─────────────────────

    def _books(self, s: dict) -> Panel:
        y, n = s.get("yes", {}), s.get("no", {})

        t = Table(box=None, expand=True, padding=(0, 1), show_header=True, header_style="bold")
        t.add_column("", width=4, no_wrap=True)
        t.add_column("Bid", justify="right", width=7, no_wrap=True)
        t.add_column("BdSz", justify="right", width=5, no_wrap=True)
        t.add_column("Ask", justify="right", width=7, no_wrap=True)
        t.add_column("AkSz", justify="right", width=5, no_wrap=True)
        t.add_column("Sprd", justify="right", width=6, no_wrap=True)
        t.add_column("Age", justify="right", width=6, no_wrap=True)

        def age_s(ms):
            return "green" if ms > 500 else "yellow" if ms > 200 else "bold red"

        def sprd_s(sp):
            return "green" if sp < 0.02 else "yellow" if sp < 0.05 else "red"

        ya_ms, na_ms = s.get("ya_age", 0), s.get("na_age", 0)
        y_sp, n_sp = y.get("spread", 0), n.get("spread", 0)

        t.add_row(
            Text("YES", style="bold green"),
            f"${y.get('bid',0):.3f}",
            Text(f"{y.get('bid_sz',0):.0f}", style="cyan"),
            f"${y.get('ask',0):.3f}",
            Text(f"{y.get('ask_sz',0):.0f}", style="cyan"),
            Text(f"${y_sp:.3f}", style=sprd_s(y_sp)),
            Text(f"{ya_ms:.0f}ms", style=age_s(ya_ms)),
        )
        t.add_row(
            Text("NO", style="bold red"),
            f"${n.get('bid',0):.3f}",
            Text(f"{n.get('bid_sz',0):.0f}", style="cyan"),
            f"${n.get('ask',0):.3f}",
            Text(f"{n.get('ask_sz',0):.0f}", style="cyan"),
            Text(f"${n_sp:.3f}", style=sprd_s(n_sp)),
            Text(f"{na_ms:.0f}ms", style=age_s(na_ms)),
        )

        # Zone indicator
        ask = y.get("ask", 0)
        if ask <= 0:
            zone = Text("  WAITING", style="dim")
        elif ask <= 0.35:
            zone = Text("  ■ SNIPER ≤$0.35", style="bold green")
        elif ask <= 0.44:
            zone = Text("  ■ VALUE $0.36-$0.44", style="bold cyan")
        else:
            zone = Text("  ■ DEAD >$0.44", style="dim")

        # OBI bar — combined YES+NO book imbalance
        y_bd = y.get("bid_depth", 0)
        y_ad = y.get("ask_depth", 0)
        n_bd = n.get("bid_depth", 0)
        n_ad = n.get("ask_depth", 0)
        total_bid = y_bd + n_bd
        total_ask = y_ad + n_ad
        total = total_bid + total_ask

        obi_line = Text("\n  OBI ", style="dim")
        if total > 0:
            ratio = total_bid / total
            pct = ratio * 100
            bar_w = 20
            bid_bars = int(ratio * bar_w)
            ask_bars = bar_w - bid_bars
            obi_line.append("◀" + "█" * ask_bars, style="red")
            obi_line.append("█" * bid_bars + "▶", style="green")
            label = "BID" if ratio > 0.55 else "ASK" if ratio < 0.45 else "BAL"
            lc = "green" if ratio > 0.55 else "red" if ratio < 0.45 else "dim"
            obi_line.append(f" {pct:.0f}%{label}", style=lc)
        else:
            obi_line.append("—", style="dim")

        class _Group:
            def __init__(self, *renderables):
                self.renderables = renderables
            def __rich_console__(self, console, options):
                for r in self.renderables:
                    yield from console.render(r, options)

        return Panel(
            _Group(t, zone, obi_line),
            title="[bold]BOOKS & ZONES[/]", border_style="blue",
        )

    # ── 4. INVENTORY & RISK ────────────────────────

    def _inv(self, s: dict) -> Panel:
        yq, nq = s.get("yq", 0), s.get("nq", 0)
        yl, nl = s.get("yl", False), s.get("nl", False)
        ya, na = s.get("yavg", 0), s.get("navg", 0)

        un = abs(yq - nq)
        hv = "YES" if yq > nq else "NO" if nq > yq else "—"
        ha = ya if yq > nq else na
        uv = un * ha

        tx = Text()
        tx.append(" YES:", style="green")
        tx.append(f"{yq:.0f}", style="bold")
        if yl:
            tx.append(" LOCKED", style="black on yellow")
        tx.append("\n NO: ", style="red")
        tx.append(f"{nq:.0f}", style="bold")
        if nl:
            tx.append(" LOCKED", style="black on yellow")

        tx.append("\n\n ")
        if un > 0:
            rs = "bold red" if uv > 5 else "yellow" if uv > 2 else "green"
            tx.append(f"+{un:.0f} {hv}", style="bold")
            tx.append(f" [-${uv:.2f}]", style=rs)
            if yl or nl:
                tx.append("\n ", style="")
                tx.append("SEEKING HEDGE", style="bold yellow")
        else:
            tx.append("BALANCED ✓", style="green")

        return Panel(tx, title="[bold]INVENTORY[/]", border_style="blue")

    # ── 5. EXECUTION TELEMETRY ─────────────────────

    def _exec(self, s: dict) -> Panel:
        exe = s.get("exe", 0)
        fr = s.get("fr", 0)
        fa = s.get("fa", 0)
        filt = s.get("filt", 0)
        fb = s.get("fb", {})

        tx = Text()
        tx.append(f" Fills:", style="dim")
        tx.append(f"{exe}", style="bold green")
        tx.append(f" Rej:", style="dim")
        tx.append(f"{fr}/{fa}", style="bold red" if fr > 0 else "dim")
        tx.append(f"\n Blocked:", style="dim")
        tx.append(f"{filt}\n", style="yellow" if filt > 0 else "dim")

        top3 = sorted(fb.items(), key=lambda x: -x[1])[:3] if isinstance(fb, dict) else []
        for reason, cnt in top3:
            tx.append(f" {reason[:20]:<20}{cnt:>4}\n", style="dim")

        return Panel(tx, title="[bold]EXECUTION[/]", border_style="blue")

    # ── 6. MARKET FLOW TAPE ────────────────────────

    def _flow_tape(self, s: dict) -> Panel:
        tape = s.get("tape", [])

        t = Table(box=None, expand=True, padding=(0, 0), show_header=True, header_style="bold dim")
        t.add_column("Time", width=8, no_wrap=True)
        t.add_column("Tkn", width=3, no_wrap=True)
        t.add_column("Size", justify="right", width=5, no_wrap=True)
        t.add_column("$Val", justify="right", width=6, no_wrap=True)
        t.add_column("Price", justify="right", width=6, no_wrap=True)
        t.add_column("", width=7, no_wrap=True)

        shown = tape[-10:] if len(tape) > 10 else tape
        for tr in shown:
            side = tr.get("side", "")
            sz = tr.get("size", 0)
            px = tr.get("price", 0)
            dv = sz * px
            is_sweep = sz > 100
            base = "green" if side == "BUY" else "red"
            sty = f"bold reverse {base}" if is_sweep else base

            ts = tr.get("time", 0)
            tstr = time.strftime("%H:%M:%S", time.localtime(ts)) if ts > 0 else "—"
            tkn = tr.get("token", "?")[:3]
            tag = "[SWEEP]" if is_sweep else ("LIFT" if side == "BUY" else "HIT")

            t.add_row(
                Text(tstr, style=sty),
                Text(tkn, style=sty),
                Text(f"{sz:.0f}", style=sty),
                Text(f"${dv:.0f}", style=sty),
                Text(f"${px:.3f}", style=sty),
                Text(tag, style=sty),
            )

        if not shown:
            t.add_row(*[Text("—", style="dim")] * 6)

        return Panel(t, title="[bold]MARKET FLOW[/]", border_style="magenta")

    # ── 7. BOT EXECUTION TAPE ──────────────────────

    def _bot_tape(self, s: dict) -> Panel:
        buys = s.get("buys", [])

        t = Table(box=None, expand=True, padding=(0, 1), show_header=True, header_style="bold")
        t.add_column("Time", width=8, no_wrap=True)
        t.add_column("Side", width=3, no_wrap=True)
        t.add_column("Qty", justify="right", width=4, no_wrap=True)
        t.add_column("Ask", justify="right", width=6, no_wrap=True)
        t.add_column("VWAP", justify="right", width=7, no_wrap=True)
        t.add_column("", width=3, no_wrap=True)

        shown = buys[-7:] if len(buys) > 7 else buys
        for i, b in enumerate(shown):
            side = b.get("side", "?")
            base = "green" if side == "YES" else "red"
            sty = "bold " + base if i == len(shown) - 1 else base

            ts = b.get("timestamp", 0)
            tstr = time.strftime("%H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) and ts > 0 else "—"
            raw = b.get("raw_price", 0)
            vwap = b.get("vwap_price", raw)
            slip = vwap - raw
            vwap_sty = "bold red" if slip > 0.01 else sty

            tag = ""
            if b.get("is_snipe"):
                tag = "[S]"
            elif raw <= 0.35:
                tag = "[S]"

            t.add_row(
                Text(tstr, style=sty),
                Text(side[:1], style=sty),
                Text(f"{b.get('qty',0):.0f}", style=sty),
                Text(f"${raw:.3f}", style=sty),
                Text(f"${vwap:.4f}", style=vwap_sty),
                Text(tag, style="bold cyan"),
            )

        if not shown:
            t.add_row(*[Text("—", style="dim")] * 6)

        return Panel(t, title="[bold]BOT FILLS[/]", border_style="blue")

    # ── 8. SESSION P&L ─────────────────────────────

    def _session(self, s: dict) -> Panel:
        ss = s.get("sess", {})
        pnl = ss.get("pnl", 0)
        wt = ss.get("wt", 0)
        wp = ss.get("wp", 0)
        tp = ss.get("tp", 0)
        last = ss.get("last", {})

        wr = (wp / wt * 100) if wt > 0 else 0
        ps = "bold green" if pnl >= 0 else "bold red"

        tx = Text()
        tx.append("\n PnL: ", style="dim")
        tx.append(f"${pnl:+.2f}\n", style=ps)
        tx.append(f"\n W/L: ", style="dim")
        tx.append(f"{wp}", style="green")
        tx.append(f"/{wt - wp}", style="red")
        tx.append(f" ({wr:.0f}%)\n", style="dim")
        tx.append(f" Pairs: ", style="dim")
        tx.append(f"{tp:.0f}\n", style="bold")

        if last:
            lp = last.get("net_pnl", 0)
            tx.append(f"\n Last: ", style="dim")
            tx.append(f"${lp:+.2f}", style="green" if lp >= 0 else "red")
            tx.append(f" ({last.get('winner','?')})", style="dim")

        return Panel(tx, title="[bold]SESSION[/]", border_style="blue")
