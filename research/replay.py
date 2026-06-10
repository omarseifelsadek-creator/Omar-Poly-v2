"""
replay.py — Historical data replay engine.

WHAT THIS DOES:
Takes stored data from SQLite and replays it through the analytics
pipeline as if it were happening live. This lets you:

1. Test your signal detection on past data
2. Study how the order book evolved before/after events
3. Debug detection thresholds without waiting for live events
4. Generate metrics for periods you weren't watching

HOW IT WORKS:
1. Load trades and snapshots from SQLite in chronological order
2. Reconstruct the order book state at each point
3. Run the full analytics pipeline on each state
4. Collect all generated metrics and insights

OUTPUT:
Returns a ReplayResult containing time-series of metrics,
all detected events, and all generated insights — ready for
statistical analysis or backtesting.

BEGINNER NOTE:
Think of this like a flight recorder replay. We recorded everything
that happened; now we play it back and analyze it from every angle.
"""

import sqlite3
import json
import time
import logging
from dataclasses import dataclass, field

from config import settings
from state.orderbook import OrderBook
from state.level_tracker import LevelTracker
from analytics.momentum import MomentumEngine
from analytics.metrics import compute_all_metrics
from analytics.interpreter import generate_insights
from data.models import (
    OrderLevel,
    BookSnapshot,
    TradeEvent,
    Metrics,
    Insight,
    Side,
)

logger = logging.getLogger(__name__)


@dataclass
class ReplayResult:
    """
    Complete results from a replay session.

    Contains all metrics, insights, and events generated during
    the replay. Can be passed to the backtester or exported.
    """
    # Time-series data
    metrics_series: list[Metrics] = field(default_factory=list)
    insights: list[Insight] = field(default_factory=list)

    # Summary statistics
    total_snapshots: int = 0
    total_trades: int = 0
    duration_seconds: float = 0.0
    start_time_ms: int = 0
    end_time_ms: int = 0

    # Extracted signal counts
    wall_count: int = 0
    whale_count: int = 0
    spoof_count: int = 0
    absorption_count: int = 0
    sweep_count: int = 0

    @property
    def duration_minutes(self) -> float:
        return self.duration_seconds / 60

    def get_metric_series(self, field_name: str) -> list[tuple[int, float]]:
        """
        Extract a single metric as a time series: [(timestamp_ms, value), ...]

        Usage:
            obi_series = result.get_metric_series("obi")
            sentiment_series = result.get_metric_series("sentiment")
        """
        series = []
        for m in self.metrics_series:
            val = getattr(m, field_name, None)
            if val is not None:
                series.append((m.timestamp_ms, val))
        return series

    def summary(self) -> dict:
        """Generate a summary dictionary of the replay."""
        return {
            "duration_minutes": round(self.duration_minutes, 1),
            "total_snapshots": self.total_snapshots,
            "total_trades": self.total_trades,
            "metrics_computed": len(self.metrics_series),
            "insights_generated": len(self.insights),
            "signals": {
                "walls": self.wall_count,
                "whales": self.whale_count,
                "spoofing": self.spoof_count,
                "absorption": self.absorption_count,
                "sweeps": self.sweep_count,
            },
        }


def replay_session(
    db_path: str = None,
    token_id: str = None,
    since_minutes: float = 60,
    compute_interval: int = 1,
    verbose: bool = False,
) -> ReplayResult:
    """
    Replay stored data through the full analytics pipeline.

    Args:
        db_path: Path to SQLite database
        token_id: Token to replay (None = use latest)
        since_minutes: How far back to replay
        compute_interval: Compute metrics every N snapshots (1 = every snapshot)
        verbose: Print progress to console

    Returns:
        ReplayResult with all metrics, insights, and events
    """
    db_path = db_path or settings.DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Find token_id
    if not token_id:
        row = conn.execute("SELECT DISTINCT token_id FROM ob_snapshots ORDER BY timestamp_ms DESC LIMIT 1").fetchone()
        if not row:
            conn.close()
            raise ValueError("No data found in database.")
        token_id = row["token_id"]

    cutoff_ms = int((time.time() - since_minutes * 60) * 1000)

    # Load snapshots
    snapshots = conn.execute(
        "SELECT timestamp_ms, bids_json, asks_json, best_bid, best_ask "
        "FROM ob_snapshots WHERE token_id = ? AND timestamp_ms >= ? ORDER BY timestamp_ms",
        (token_id, cutoff_ms),
    ).fetchall()

    # Load trades
    trades = conn.execute(
        "SELECT timestamp_ms, price, size, side "
        "FROM trades WHERE token_id = ? AND timestamp_ms >= ? ORDER BY timestamp_ms",
        (token_id, cutoff_ms),
    ).fetchall()
    conn.close()

    if not snapshots:
        raise ValueError(f"No snapshots found in the last {since_minutes} minutes.")

    if verbose:
        print(f"Replaying {len(snapshots)} snapshots and {len(trades)} trades...")

    # Create fresh analytics components
    ob = OrderBook()
    tracker = LevelTracker()
    momentum = MomentumEngine()

    result = ReplayResult(
        start_time_ms=snapshots[0]["timestamp_ms"],
        end_time_ms=snapshots[-1]["timestamp_ms"],
        total_snapshots=len(snapshots),
        total_trades=len(trades),
    )
    result.duration_seconds = (result.end_time_ms - result.start_time_ms) / 1000

    # Build trade lookup by timestamp (for interleaving)
    trade_idx = 0
    prev_metrics = None

    for snap_idx, snap in enumerate(snapshots):
        snap_ts = snap["timestamp_ms"]

        # Apply any trades that occurred before this snapshot
        while trade_idx < len(trades) and trades[trade_idx]["timestamp_ms"] <= snap_ts:
            t = trades[trade_idx]
            trade_event = TradeEvent(
                asset_id=token_id,
                price=t["price"],
                size=t["size"],
                side=Side(t["side"]),
                timestamp_ms=t["timestamp_ms"],
            )
            ob.apply_trade(trade_event)
            tracker.record_trade_at_level(
                price=t["price"],
                trade_size=t["size"],
                trade_side=Side(t["side"]),
                timestamp_ms=t["timestamp_ms"],
            )
            momentum.update(trade_price=t["price"])
            trade_idx += 1

        # Reconstruct order book from snapshot
        bids_raw = json.loads(snap["bids_json"])
        asks_raw = json.loads(snap["asks_json"])

        book_snap = BookSnapshot(
            asset_id=token_id,
            market="",
            bids=[OrderLevel(price=b["p"], size=b["s"]) for b in bids_raw],
            asks=[OrderLevel(price=a["p"], size=a["s"]) for a in asks_raw],
            timestamp_ms=snap_ts,
        )
        ob.apply_snapshot(book_snap)

        # Record levels in tracker
        for level in book_snap.bids:
            tracker.record_change(level.price, Side.BUY, level.size, snap_ts)
        for level in book_snap.asks:
            tracker.record_change(level.price, Side.SELL, level.size, snap_ts)

        # Compute metrics at interval
        if snap_idx % compute_interval == 0:
            metrics = compute_all_metrics(ob, tracker, momentum)
            result.metrics_series.append(metrics)

            # Generate insights
            insights = generate_insights(metrics, prev_metrics)
            result.insights.extend(insights)

            # Count signals
            result.wall_count += len(metrics.walls)
            result.whale_count += len(metrics.whale_events)
            result.spoof_count += len(metrics.spoof_signals)
            result.absorption_count += len(metrics.absorption_events)
            result.sweep_count += len(metrics.sweep_events)

            prev_metrics = metrics

        if verbose and snap_idx % 100 == 0:
            pct = (snap_idx + 1) / len(snapshots) * 100
            print(f"  Progress: {pct:.0f}% ({snap_idx + 1}/{len(snapshots)})")

    if verbose:
        print(f"Replay complete. {len(result.metrics_series)} metrics, {len(result.insights)} insights.")

    return result
