"""
pair_logger.py — CSV logging for pair trading strategy.

Logs:
  - pair_buys_YYYYMMDD.csv     — Every individual buy (YES or NO leg)
  - pair_windows_YYYYMMDD.csv  — End-of-window settlement results
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
    "time_remaining", "mode",
]


def log_pair_buy(market: str, action: dict, engine_stats: dict):
    """Log a single leg buy."""
    filename = f"pair_buys_{_date_str()}.csv"
    _append_row(filename, [
        _time_str(),
        market,
        action.get("side", ""),
        action.get("qty", 0),
        f"{action.get('raw_price', 0):.4f}",
        f"{action.get('vwap_price', action.get('raw_price', 0)):.4f}",
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
        "PAPER",
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
    "num_buys", "cumulative_pnl", "mode",
]


def log_window_settlement(market: str, result, cumulative_pnl: float):
    """Log end-of-window settlement result."""
    filename = f"pair_windows_{_date_str()}.csv"
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
        "PAPER",
    ], WINDOW_HEADER)
