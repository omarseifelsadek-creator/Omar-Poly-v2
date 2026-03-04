"""
pair_logger.py — CSV logging for pair trading strategy.

Logs:
  - pair_buys_YYYYMMDD.csv        — Every individual buy (YES or NO leg)
  - pair_windows_YYYYMMDD.csv     — End-of-window settlement results
  - pair_rejections_YYYYMMDD.csv  — Live CLOB order rejections
  - pair_filters_YYYYMMDD.csv     — Strategy evaluate() filter events
"""

import os
import csv
import time
from typing import Optional


LOG_DIR = "data/logs"


def _ensure_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def _date_str() -> str:
    return time.strftime("%Y%m%d")


def _time_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _append_row(filename: str, row: list, header: Optional[list] = None):
    """Append a row to a CSV file, creating it with header if needed."""
    _ensure_dir()
    filepath = os.path.join(LOG_DIR, filename)
    file_exists = os.path.exists(filepath)

    with open(filepath, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists and header:
            writer.writerow(header)
        writer.writerow(row)


# ──────────────────────────────────────────────────────────
# BUY LOG — Every individual leg purchase
# ──────────────────────────────────────────────────────────

BUY_HEADER = [
    "timestamp", "market", "side", "qty", "ask_price",
    "vwap_price", "fill_price", "cost", "fee_pct",
    "order_type", "ask_age_ms", "levels_walked", "is_snipe",
    "yes_qty", "no_qty", "pair_cost", "skew",
    "time_remaining",
    # ── Quant context at fill ──
    "zone", "obi", "flow_pressure", "sweep", "sweep_side",
    "opposite_ask", "best_bid", "spread",
    "yes_bid_depth", "yes_ask_depth", "no_bid_depth", "no_ask_depth",
    "slippage_cents", "time_to_hedge_s", "unhedged_usd",
    "mode",
]


def log_pair_buy(market: str, action: dict, engine_stats: dict,
                 ctx: dict = None, mode: str = "PAPER"):
    """Log a single leg buy with full quant context."""
    if ctx is None:
        ctx = {}
    filename = f"pair_buys_{_date_str()}.csv"

    raw = action.get('raw_price', 0)
    vwap = action.get('vwap_price', raw)
    slippage_cents = round((vwap - raw) * 100, 2) if raw else 0

    _append_row(filename, [
        _time_str(),
        market,
        action.get("side", ""),
        action.get("qty", 0),
        f"{raw:.4f}",
        f"{vwap:.4f}",
        f"{action.get('fill_price', 0):.4f}",
        f"{action.get('cost', 0):.4f}",
        f"{action.get('fee_pct', 0):.2f}%",
        action.get("order_type", "TAKER"),
        f"{action.get('ask_age_ms', 0):.0f}",
        action.get("levels_walked", 1),
        "YES" if action.get("is_snipe") else "NO",
        engine_stats.get("yes_qty", 0),
        engine_stats.get("no_qty", 0),
        f"{engine_stats.get('pair_cost', 0):.4f}",
        f"{engine_stats.get('skew', 0):.3f}",
        f"{engine_stats.get('time_remaining', 0):.0f}",
        # ── Quant context ──
        ctx.get("zone", ""),
        f"{ctx.get('obi', 0):.3f}",
        f"{ctx.get('flow_pressure', 0):.3f}",
        "YES" if ctx.get("has_sweep") else "NO",
        ctx.get("sweep_side", ""),
        f"{ctx.get('opposite_ask', 0):.4f}",
        f"{ctx.get('best_bid', 0):.4f}",
        f"{ctx.get('spread', 0):.4f}",
        f"{ctx.get('yes_bid_depth', 0):.0f}",
        f"{ctx.get('yes_ask_depth', 0):.0f}",
        f"{ctx.get('no_bid_depth', 0):.0f}",
        f"{ctx.get('no_ask_depth', 0):.0f}",
        f"{slippage_cents:+.2f}",
        ctx.get("time_to_hedge_s", "N/A"),
        f"{ctx.get('unhedged_usd', 0):.2f}",
        mode,
    ], BUY_HEADER)


# ──────────────────────────────────────────────────────────
# WINDOW SETTLEMENT LOG
# ──────────────────────────────────────────────────────────

WINDOW_HEADER = [
    "timestamp", "market",
    "yes_qty", "yes_avg_cost", "no_qty", "no_avg_cost",
    "completed_pairs", "unmatched_qty", "unmatched_side",
    "avg_pair_cost", "total_capital",
    "winner", "pair_profit", "gamble_result", "net_pnl",
    "num_buys", "cumulative_pnl",
    # ── Window micro-stats ──
    "fills_attempted", "fills_rejected", "rejection_rate",
    "dead_zone_blocks", "cap_exhausted_at_s",
    "max_unhedged_usd", "avg_hedge_time_s",
    "sniper_fills", "value_fills", "panic_fills",
    "avg_slippage_cents",
    "mode",
]


def log_window_settlement(market: str, result, cumulative_pnl: float,
                          wctx: dict = None, mode: str = "PAPER"):
    """Log end-of-window settlement result with micro-stats."""
    if wctx is None:
        wctx = {}
    filename = f"pair_windows_{_date_str()}.csv"

    fa = wctx.get("fills_attempted", 0)
    fr = wctx.get("fills_rejected", 0)
    rej_rate = f"{fr / fa * 100:.1f}%" if fa > 0 else "0.0%"

    _append_row(filename, [
        _time_str(),
        market,
        f"{result.yes_qty:.0f}",
        f"{result.yes_avg_cost:.4f}",
        f"{result.no_qty:.0f}",
        f"{result.no_avg_cost:.4f}",
        f"{result.matched_pairs:.0f}",
        f"{result.unmatched_qty:.0f}",
        result.unmatched_side,
        f"{result.avg_pair_cost:.4f}",
        f"{result.total_cost:.2f}",
        result.winner,
        f"{result.pair_profit:+.4f}",
        f"{result.gamble_result:+.4f}",
        f"{result.net_pnl:+.4f}",
        result.num_buys,
        f"{cumulative_pnl:+.4f}",
        # ── Micro-stats ──
        fa,
        fr,
        rej_rate,
        wctx.get("dead_zone_blocks", 0),
        wctx.get("cap_exhausted_at_s", "N/A"),
        f"{wctx.get('max_unhedged_usd', 0):.2f}",
        wctx.get("avg_hedge_time_s", "N/A"),
        wctx.get("sniper_fills", 0),
        wctx.get("value_fills", 0),
        wctx.get("panic_fills", 0),
        wctx.get("avg_slippage_cents", "N/A"),
        mode,
    ], WINDOW_HEADER)


# ──────────────────────────────────────────────────────────
# REJECTION LOG — Live CLOB order rejections
# ──────────────────────────────────────────────────────────

REJECTION_HEADER = [
    "timestamp", "market", "side", "qty", "price",
    "order_id", "status", "error_msg",
    "yes_ask", "no_ask", "spread",
    "yes_qty", "no_qty", "pair_cost", "skew",
    "time_remaining", "zone", "mode",
    "lat_ms", "is_technical", "price_moved",
]


def log_pair_rejection(market: str, action: dict, reject_info: dict,
                       engine_stats: dict, ctx: dict = None, mode: str = "LIVE"):
    """Log a CLOB order rejection with full context."""
    if ctx is None:
        ctx = {}
    filename = f"pair_rejections_{_date_str()}.csv"

    # Technical error = exception/bad_response, not market-driven
    status = reject_info.get("status", "")
    is_technical = status in ("exception", "bad_response")

    # Price moved: compare submitted price to current ask after rejection
    # Positive = price moved against us, negative = price improved
    submitted = action.get("vwap_price", 0)
    current_ask = ctx.get("current_ask_after", 0)
    price_moved = round(current_ask - submitted, 4) if current_ask and submitted else ""

    _append_row(filename, [
        _time_str(),
        market,
        action.get("side", ""),
        action.get("qty", 0),
        f"{submitted:.4f}",
        reject_info.get("order_id", ""),
        status,
        reject_info.get("error", ""),
        f"{ctx.get('yes_ask', 0):.4f}",
        f"{ctx.get('no_ask', 0):.4f}",
        f"{ctx.get('spread', 0):.4f}",
        engine_stats.get("yes_qty", 0),
        engine_stats.get("no_qty", 0),
        f"{engine_stats.get('pair_cost', 0):.4f}",
        f"{engine_stats.get('skew', 0):.3f}",
        f"{engine_stats.get('time_remaining', 0):.0f}",
        ctx.get("zone", ""),
        mode,
        f"{ctx.get('lat_ms', 0):.0f}",
        "YES" if is_technical else "NO",
        price_moved,
    ], REJECTION_HEADER)


# ──────────────────────────────────────────────────────────
# FILTER LOG — Strategy evaluate() filter events
# ──────────────────────────────────────────────────────────

FILTER_HEADER = [
    "timestamp", "market", "reason", "value", "threshold",
    "yes_ask", "no_ask", "time_remaining", "zone",
    "spread_bps", "tick_distance",
]


def log_pair_filter(market: str, reason: str, value: float, threshold: float,
                    ctx: dict = None):
    """Log a strategy filter event (evaluate returned None)."""
    if ctx is None:
        ctx = {}
    filename = f"pair_filters_{_date_str()}.csv"

    # Spread in basis points (1 bps = 0.01 cents on a $1 market)
    yes_ask = ctx.get("yes_ask", 0)
    no_ask = ctx.get("no_ask", 0)
    yes_bid = ctx.get("yes_bid", 0)
    mid = (yes_ask + yes_bid) / 2 if yes_ask and yes_bid else 0
    spread_bps = round((yes_ask - yes_bid) * 10000, 0) if yes_ask and yes_bid else ""

    # Tick distance: how many $0.01 ticks from mid to the filtered ask
    tick_distance = round((yes_ask - mid) / 0.01, 1) if mid else ""

    _append_row(filename, [
        _time_str(),
        market,
        reason,
        f"{value:.4f}" if value else "",
        f"{threshold:.4f}" if threshold else "",
        f"{yes_ask:.4f}",
        f"{no_ask:.4f}",
        f"{ctx.get('time_remaining', 0):.0f}",
        ctx.get("zone", ""),
        spread_bps,
        tick_distance,
    ], FILTER_HEADER)
