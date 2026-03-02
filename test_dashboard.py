#!/usr/bin/env python3
"""test_dashboard.py — Standalone test. No WebSocket needed."""
import sys, os, time, traceback

print("Testing dashboard...")
print(f"Terminal: {os.get_terminal_size()}")
print(f"TERM: {os.environ.get('TERM', 'not set')}")

try:
    from execution.pair_dashboard import PairDashboard, build_state
    print("✅ imported")
except Exception as e:
    print(f"❌ {e}"); traceback.print_exc(); sys.exit(1)

now = time.time()
fake = {
    "market": "BTC 5m — 20:05-20:10 UTC", "t": 187, "wn": 3,
    "msgs": 4521, "ws_t": now - 0.04,
    "yes": {"bid": 0.32, "ask": 0.34, "mid": 0.33, "spread": 0.02,
            "bid_sz": 450, "ask_sz": 320, "bid_depth": 2800, "ask_depth": 1900},
    "no": {"bid": 0.58, "ask": 0.61, "mid": 0.595, "spread": 0.03,
           "bid_sz": 280, "ask_sz": 510, "bid_depth": 3200, "ask_depth": 2400},
    "ya_age": 650, "na_age": 180,
    "yq": 75, "nq": 50, "yavg": 0.3212, "navg": 0.5634,
    "pc": 0.8846, "mp": 50, "skew": 0.33, "cap": 42.50,
    "yl": False, "nl": True, "panic": False,
    "exe": 14, "filt": 847, "fa": 17, "fr": 3, "pf": 1,
    "fb": {"dead_zone_nuked": 412, "cooldown": 203, "obi_delay": 98},
    "buys": [
        {"side": "YES", "qty": 25, "raw_price": 0.30, "vwap_price": 0.302, "timestamp": now-60, "is_snipe": True},
        {"side": "NO",  "qty": 25, "raw_price": 0.56, "vwap_price": 0.575, "timestamp": now-45, "is_snipe": False},
        {"side": "YES", "qty": 25, "raw_price": 0.28, "vwap_price": 0.282, "timestamp": now-30, "is_snipe": True},
        {"side": "NO",  "qty": 25, "raw_price": 0.58, "vwap_price": 0.581, "timestamp": now-15, "is_snipe": False},
        {"side": "YES", "qty": 25, "raw_price": 0.33, "vwap_price": 0.332, "timestamp": now-5,  "is_snipe": False},
    ],
    "tape": [
        {"time": now-10, "token": "YES", "size": 50,  "price": 0.33, "side": "BUY"},
        {"time": now-8,  "token": "NO",  "size": 200, "price": 0.60, "side": "SELL"},
        {"time": now-6,  "token": "YES", "size": 30,  "price": 0.34, "side": "BUY"},
        {"time": now-5,  "token": "NO",  "size": 150, "price": 0.59, "side": "SELL"},
        {"time": now-3,  "token": "YES", "size": 80,  "price": 0.32, "side": "BUY"},
        {"time": now-1,  "token": "NO",  "size": 25,  "price": 0.61, "side": "BUY"},
    ],
    "sess": {"pnl": 12.45, "wt": 5, "wp": 3, "tp": 189, "last": {"net_pnl": 3.21, "winner": "YES"}},
}

dash = PairDashboard()

# Quick buffer sanity check
from io import StringIO
from rich.console import Console
cols, rows = os.get_terminal_size()
c = Console(file=StringIO(), width=cols, height=rows, force_terminal=True, color_system="256", highlight=False)
with c.capture() as cap:
    c.print(dash._layout(fake))
out = cap.get()
nl = out.count("\n")
print(f"✅ rendered: {len(out)} chars, {nl} lines (term {cols}x{rows})")
if nl < 5:
    print(f"⚠️  suspiciously small output")

print("\nDashboard in 2s... Ctrl+C to stop.")
time.sleep(2)

try:
    dash.start()
    for i in range(30):  # 15 seconds
        fake["t"] = max(187 - i * 3, 0)
        fake["msgs"] += 120
        fake["ws_t"] = time.time()
        fake["mp"] += 1 if i % 2 == 0 else 0
        fake["cap"] += 0.32 if i % 2 == 0 else 0

        # Simulate panic mode in last frames
        if fake["t"] < 60:
            fake["panic"] = True

        # Add market trades
        fake["tape"].append({
            "time": time.time(),
            "token": "YES" if i % 2 == 0 else "NO",
            "size": 40 + i * 8,
            "price": 0.33 + (i % 4) * 0.01,
            "side": "BUY" if i % 3 != 0 else "SELL",
        })
        if len(fake["tape"]) > 12:
            fake["tape"] = fake["tape"][-12:]

        try:
            dash.render(fake)
        except Exception:
            sys.stdout.write(f"\033[H\033[2J\033[91m═══ CRASH frame {i} ═══\n{traceback.format_exc()}\033[0m\n")
            sys.stdout.flush()
            time.sleep(5)
            break
        time.sleep(0.5)

    dash.stop()
    print("\n✅ Dashboard ran 15 seconds!")
except KeyboardInterrupt:
    dash.stop()
    print("\n✅ Stopped.")
except Exception as e:
    try: dash.stop()
    except: sys.stdout.write("\033[?25h\033[?1049l"); sys.stdout.flush()
    print(f"\n❌ {e}"); traceback.print_exc()
