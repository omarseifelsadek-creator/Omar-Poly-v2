"""
trade_logger.py — Persistent CSV trade log.

Logs every entry, exit, signal, and periodic stats to CSV files
so you can review performance after a session.

Files created in data/ directory:
  - trades_YYYYMMDD.csv     — All entries and exits
  - signals_YYYYMMDD.csv    — All signals generated (even filtered ones)
  - session_YYYYMMDD.csv    — Periodic stats snapshots
"""

import os
import csv
import time
from typing import Optional
from analytics.signals import TradeSignal


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
# LOG FUNCTIONS
# ──────────────────────────────────────────────────────────

TRADE_HEADER = [
    "timestamp", "market", "token", "action", "size",
    "price", "stop_loss", "take_profit", "reason",
    "signal_type", "pnl", "mode", "type",
]

SIGNAL_HEADER = [
    "timestamp", "market", "token", "action", "price",
    "confidence", "signal_type", "reason", "acted_on",
]

STATS_HEADER = [
    "timestamp", "market", "mode", "open_positions",
    "total_trades", "wins", "losses", "win_rate",
    "total_pnl", "daily_pnl", "signals_received",
    "signals_filtered", "exposure_usd",
]


def log_trade_entry(market: str, action: dict):
    """Log a trade entry."""
    filename = f"trades_{_date_str()}.csv"
    _append_row(filename, [
        _time_str(),
        market,
        action.get("token", ""),
        action.get("action", ""),
        action.get("size", 0),
        f"{action.get('price', 0):.4f}",
        f"{action.get('stop_loss', 0):.4f}",
        f"{action.get('take_profit', 0):.4f}",
        action.get("reason", ""),
        action.get("signal_type", ""),
        "",  # No PnL on entry
        "PAPER" if action.get("paper") else "LIVE",
        "ENTRY",
    ], TRADE_HEADER)


def log_trade_exit(market: str, action: dict):
    """Log a trade exit."""
    filename = f"trades_{_date_str()}.csv"
    _append_row(filename, [
        _time_str(),
        market,
        action.get("token", ""),
        action.get("action", ""),
        action.get("size", 0),
        f"{action.get('price', 0):.4f}",
        "",  # No SL on exit
        "",  # No TP on exit
        action.get("reason", ""),
        "",
        f"{action.get('pnl', 0):.4f}",
        "PAPER" if action.get("paper") else "LIVE",
        "EXIT",
    ], TRADE_HEADER)


def log_signal(market: str, signal: TradeSignal, acted_on: bool):
    """Log every signal, whether acted on or not."""
    filename = f"signals_{_date_str()}.csv"
    _append_row(filename, [
        _time_str(),
        market,
        signal.token,
        signal.action,
        f"{signal.entry_price:.4f}",
        signal.confidence,
        signal.signal_type,
        signal.reason,
        "YES" if acted_on else "NO",
    ], SIGNAL_HEADER)


def log_stats(market: str, stats: dict):
    """Log periodic stats snapshot."""
    filename = f"session_{_date_str()}.csv"
    _append_row(filename, [
        _time_str(),
        market,
        stats.get("mode", ""),
        stats.get("open_positions", 0),
        stats.get("total_trades", 0),
        stats.get("wins", 0),
        stats.get("losses", 0),
        f"{stats.get('win_rate', 0):.2%}",
        f"{stats.get('total_pnl', 0):.4f}",
        f"{stats.get('daily_pnl', 0):.4f}",
        stats.get("signals_received", 0),
        stats.get("signals_filtered", 0),
        f"{stats.get('exposure_usd', 0):.2f}",
    ], STATS_HEADER)


def log_session_summary(market: str, stats: dict):
    """Log final session summary."""
    filename = f"trades_{_date_str()}.csv"
    _append_row(filename, [
        _time_str(),
        market,
        "",
        "SESSION_END",
        "",
        "",
        "",
        "",
        f"Trades: {stats.get('total_trades', 0)} | "
        f"W: {stats.get('wins', 0)} L: {stats.get('losses', 0)} | "
        f"WR: {stats.get('win_rate', 0):.0%} | "
        f"PnL: ${stats.get('total_pnl', 0):+.2f}",
        "",
        f"{stats.get('total_pnl', 0):.4f}",
        "PAPER" if stats.get("mode") == "paper" else "LIVE",
        "SUMMARY",
    ], TRADE_HEADER)
