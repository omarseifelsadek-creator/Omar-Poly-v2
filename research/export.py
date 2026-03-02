"""
export.py — Data export tools for external analysis.

Exports stored OBI data to CSV and JSON formats for use in:
- Jupyter notebooks (pandas.read_csv)
- Spreadsheets (Google Sheets, Excel)
- External tools (R, Tableau, custom scripts)
- Sharing with collaborators

USAGE:
    from research.export import export_trades_csv, export_metrics_csv, export_all

    # Export everything from the last 2 hours
    files = export_all(since_minutes=120, output_dir="exports/")

    # Export specific data
    export_trades_csv(output_path="my_trades.csv", since_minutes=60)
    export_metrics_csv(output_path="my_metrics.csv")
"""

import csv
import json
import os
import sqlite3
import time
import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


def export_trades_csv(
    output_path: str = "exports/trades.csv",
    db_path: str = None,
    token_id: str = None,
    since_minutes: float = 60,
) -> str:
    """
    Export trades to CSV.

    Columns: timestamp, datetime, price, size, side, dollar_value
    """
    db_path = db_path or settings.DB_PATH
    _ensure_dir(output_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    token_id = token_id or _get_latest_token(conn)
    cutoff_ms = int((time.time() - since_minutes * 60) * 1000)

    rows = conn.execute(
        "SELECT timestamp_ms, price, size, side, dollar_value "
        "FROM trades WHERE token_id = ? AND timestamp_ms >= ? ORDER BY timestamp_ms",
        (token_id, cutoff_ms),
    ).fetchall()
    conn.close()

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_ms", "datetime", "price", "size", "side", "dollar_value"])
        for r in rows:
            dt = _ms_to_datetime(r["timestamp_ms"])
            writer.writerow([r["timestamp_ms"], dt, r["price"], r["size"], r["side"], r["dollar_value"]])

    logger.info(f"Exported {len(rows)} trades to {output_path}")
    return output_path


def export_metrics_csv(
    output_path: str = "exports/metrics.csv",
    db_path: str = None,
    token_id: str = None,
    since_minutes: float = 60,
) -> str:
    """
    Export metrics time series to CSV.

    Columns: all metric fields including Phase 3 momentum/regime data.
    """
    db_path = db_path or settings.DB_PATH
    _ensure_dir(output_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    token_id = token_id or _get_latest_token(conn)
    cutoff_ms = int((time.time() - since_minutes * 60) * 1000)

    rows = conn.execute(
        "SELECT * FROM metrics WHERE token_id = ? AND timestamp_ms >= ? ORDER BY timestamp_ms",
        (token_id, cutoff_ms),
    ).fetchall()
    conn.close()

    if not rows:
        logger.warning("No metrics data to export.")
        return output_path

    columns = rows[0].keys()

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        # Add a datetime column
        writer.writerow(["datetime"] + list(columns))
        for r in rows:
            dt = _ms_to_datetime(r["timestamp_ms"])
            writer.writerow([dt] + [r[c] for c in columns])

    logger.info(f"Exported {len(rows)} metric rows to {output_path}")
    return output_path


def export_events_csv(
    output_path: str = "exports/events.csv",
    db_path: str = None,
    token_id: str = None,
    since_minutes: float = 60,
    event_type: str = None,
) -> str:
    """
    Export detected events to CSV.

    Optionally filter by event_type: 'whale', 'spoof', 'absorption', 'sweep', 'insight'
    """
    db_path = db_path or settings.DB_PATH
    _ensure_dir(output_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    token_id = token_id or _get_latest_token(conn)
    cutoff_ms = int((time.time() - since_minutes * 60) * 1000)

    query = "SELECT * FROM events WHERE token_id = ? AND timestamp_ms >= ?"
    params = [token_id, cutoff_ms]
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)
    query += " ORDER BY timestamp_ms"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        logger.warning("No events to export.")
        return output_path

    columns = rows[0].keys()

    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["datetime"] + list(columns))
        for r in rows:
            dt = _ms_to_datetime(r["timestamp_ms"])
            writer.writerow([dt] + [r[c] for c in columns])

    logger.info(f"Exported {len(rows)} events to {output_path}")
    return output_path


def export_snapshots_json(
    output_path: str = "exports/snapshots.json",
    db_path: str = None,
    token_id: str = None,
    since_minutes: float = 60,
    limit: int = 500,
) -> str:
    """
    Export order book snapshots to JSON.

    Each snapshot includes full bid/ask levels, best prices, and OBI.
    JSON format preserves the nested bid/ask structure better than CSV.
    """
    db_path = db_path or settings.DB_PATH
    _ensure_dir(output_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    token_id = token_id or _get_latest_token(conn)
    cutoff_ms = int((time.time() - since_minutes * 60) * 1000)

    rows = conn.execute(
        "SELECT timestamp_ms, bids_json, asks_json, best_bid, best_ask, spread, obi, "
        "total_bid_depth, total_ask_depth "
        "FROM ob_snapshots WHERE token_id = ? AND timestamp_ms >= ? "
        "ORDER BY timestamp_ms LIMIT ?",
        (token_id, cutoff_ms, limit),
    ).fetchall()
    conn.close()

    snapshots = []
    for r in rows:
        snapshots.append({
            "timestamp_ms": r["timestamp_ms"],
            "datetime": _ms_to_datetime(r["timestamp_ms"]),
            "bids": json.loads(r["bids_json"]),
            "asks": json.loads(r["asks_json"]),
            "best_bid": r["best_bid"],
            "best_ask": r["best_ask"],
            "spread": r["spread"],
            "obi": r["obi"],
            "total_bid_depth": r["total_bid_depth"],
            "total_ask_depth": r["total_ask_depth"],
        })

    with open(output_path, "w") as f:
        json.dump({"token_id": token_id, "count": len(snapshots), "snapshots": snapshots}, f, indent=2)

    logger.info(f"Exported {len(snapshots)} snapshots to {output_path}")
    return output_path


def export_backtest_json(
    report,
    output_path: str = "exports/backtest_report.json",
) -> str:
    """
    Export a BacktestReport to JSON.
    """
    _ensure_dir(output_path)
    with open(output_path, "w") as f:
        json.dump(report.to_dict(), f, indent=2)
    logger.info(f"Backtest report exported to {output_path}")
    return output_path


def export_all(
    output_dir: str = "exports",
    db_path: str = None,
    since_minutes: float = 60,
) -> dict[str, str]:
    """
    Export all data types at once.

    Returns a dict of {data_type: filepath} for all exported files.
    """
    os.makedirs(output_dir, exist_ok=True)
    files = {}

    try:
        files["trades"] = export_trades_csv(
            os.path.join(output_dir, "trades.csv"), db_path, since_minutes=since_minutes)
    except Exception as e:
        logger.error(f"Failed to export trades: {e}")

    try:
        files["metrics"] = export_metrics_csv(
            os.path.join(output_dir, "metrics.csv"), db_path, since_minutes=since_minutes)
    except Exception as e:
        logger.error(f"Failed to export metrics: {e}")

    try:
        files["events"] = export_events_csv(
            os.path.join(output_dir, "events.csv"), db_path, since_minutes=since_minutes)
    except Exception as e:
        logger.error(f"Failed to export events: {e}")

    try:
        files["snapshots"] = export_snapshots_json(
            os.path.join(output_dir, "snapshots.json"), db_path, since_minutes=since_minutes)
    except Exception as e:
        logger.error(f"Failed to export snapshots: {e}")

    logger.info(f"Exported {len(files)} files to {output_dir}/")
    return files


def get_db_summary(db_path: str = None) -> dict:
    """
    Get a quick summary of what's in the database.

    Useful for checking data availability before running exports.
    """
    db_path = db_path or settings.DB_PATH
    if not os.path.exists(db_path):
        return {"error": f"Database not found: {db_path}"}

    conn = sqlite3.connect(db_path)
    summary = {}

    for table in ["ob_snapshots", "trades", "metrics", "events"]:
        try:
            row = conn.execute(f"SELECT COUNT(*) as cnt, MIN(timestamp_ms) as first_ms, MAX(timestamp_ms) as last_ms FROM {table}").fetchone()
            count = row[0]
            first_ms = row[1]
            last_ms = row[2]
            summary[table] = {
                "count": count,
                "first": _ms_to_datetime(first_ms) if first_ms else None,
                "last": _ms_to_datetime(last_ms) if last_ms else None,
                "span_minutes": round((last_ms - first_ms) / 60000, 1) if first_ms and last_ms else 0,
            }
        except Exception:
            summary[table] = {"count": 0}

    conn.close()
    return summary


# ──────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────

def _ensure_dir(path: str):
    """Create directory for path if it doesn't exist."""
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)


def _get_latest_token(conn: sqlite3.Connection) -> str:
    """Get the most recently used token_id from the database."""
    row = conn.execute(
        "SELECT DISTINCT token_id FROM ob_snapshots ORDER BY timestamp_ms DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise ValueError("No data found in database.")
    return row[0]


def _ms_to_datetime(ms: int) -> str:
    """Convert millisecond timestamp to ISO datetime string."""
    from datetime import datetime
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
