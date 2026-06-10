"""
database.py — Async SQLite storage for historical data.

WHY STORE DATA:
1. Research: Replay past sessions to study patterns
2. Backtesting: Test if your signals predicted price moves
3. Analysis: Query historical spreads, imbalance, flow over time
4. ML: Training data for future machine learning models

HOW IT WORKS:
- Uses `aiosqlite` for non-blocking database writes
- The main data loop calls store_* methods after computing metrics
- Writes happen asynchronously so they never slow down the live feed
- Snapshots are taken at SNAPSHOT_INTERVAL_SECONDS intervals (not every tick)

TABLES:
- ob_snapshots: Periodic order book state
- trades: Every trade event
- metrics: Computed metrics time series
- events: Detected anomalies (walls, whales, spoofing, absorption, sweeps)

BEGINNER NOTE:
SQLite is a file-based database — no server needed. Your data lives in
a single file (default: data/obi.db). You can query it with any SQLite
tool (DB Browser for SQLite, Python's sqlite3 module, etc.)
"""

import json
import time
import logging
import os
import asyncio
from typing import Optional

import aiosqlite

from config import settings
from state.orderbook import OrderBook
from data.models import (
    Metrics,
    Insight,
    TradeEvent,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# SQL SCHEMA
# ──────────────────────────────────────────────────────────────

SCHEMA = """
-- Order book snapshots (periodic, every SNAPSHOT_INTERVAL_SECONDS)
CREATE TABLE IF NOT EXISTS ob_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms INTEGER NOT NULL,
    token_id TEXT NOT NULL,
    bids_json TEXT NOT NULL,
    asks_json TEXT NOT NULL,
    best_bid REAL,
    best_ask REAL,
    spread REAL,
    obi REAL,
    total_bid_depth REAL,
    total_ask_depth REAL
);

-- Individual trades from the WebSocket
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms INTEGER NOT NULL,
    token_id TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    side TEXT NOT NULL,
    dollar_value REAL
);

-- Computed metrics time series
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms INTEGER NOT NULL,
    token_id TEXT NOT NULL,
    obi REAL,
    spread REAL,
    midpoint REAL,
    vwap_mid REAL,
    flow_pressure REAL,
    buy_volume REAL,
    sell_volume REAL,
    sentiment REAL,
    total_bid_depth REAL,
    total_ask_depth REAL,
    -- Phase 3: Momentum & Regime
    price_velocity REAL,
    price_trend_strength REAL,
    volatility REAL,
    regime TEXT,
    regime_confidence REAL
);

-- Detected events (walls, whales, spoofing, absorption, sweeps)
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms INTEGER NOT NULL,
    token_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    description TEXT NOT NULL,
    price REAL,
    size REAL,
    side TEXT,
    metadata_json TEXT
);

-- Indexes for efficient time-series queries
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON ob_snapshots(token_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(token_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_metrics_time ON metrics(token_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(token_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, timestamp_ms);
"""


class Database:
    """
    Async SQLite database for persisting OBI data.

    Usage:
        db = Database()
        await db.initialize()

        await db.store_trade(token_id, trade_event)
        await db.store_snapshot(token_id, orderbook, metrics)
        await db.store_metrics(token_id, metrics)
        await db.store_event(token_id, "whale", "alert", "Large buy detected", ...)

        await db.close()
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or settings.DB_PATH
        self._db: Optional[aiosqlite.Connection] = None
        self._last_snapshot_time: float = 0
        self._write_queue: asyncio.Queue = asyncio.Queue(
            maxsize=settings.DB_WRITE_QUEUE_SIZE
        )
        self._writer_task: Optional[asyncio.Task] = None
        # Overflow accounting (B16): drops are counted and surfaced, not
        # just logged once per write (which would spam a saturated queue).
        self.dropped_writes: int = 0
        self._last_drop_warning: float = 0.0

    async def initialize(self):
        """
        Create the database file and tables.

        Called once at startup. Creates the file if it doesn't exist,
        and ensures all tables are present.
        """
        # Ensure the directory exists
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._db = await aiosqlite.connect(self._db_path)

        # Enable WAL mode for better concurrent read/write performance
        await self._db.execute("PRAGMA journal_mode=WAL")
        # Reduce sync for better write performance (acceptable for analytics)
        await self._db.execute("PRAGMA synchronous=NORMAL")

        # Create tables
        await self._db.executescript(SCHEMA)
        await self._db.commit()

        # Start background writer task
        self._writer_task = asyncio.create_task(self._write_loop())

        logger.info(f"Database initialized: {self._db_path}")

    async def close(self):
        """Clean shutdown: flush pending writes and close connection."""
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass

        # Flush any remaining items
        while not self._write_queue.empty():
            try:
                sql, params = self._write_queue.get_nowait()
                if self._db:
                    await self._db.execute(sql, params)
            except asyncio.QueueEmpty:
                break

        if self._db:
            await self._db.commit()
            await self._db.close()
            if self.dropped_writes:
                logger.warning(
                    f"Database closed — {self.dropped_writes} writes were "
                    f"dropped this session (queue overflow); analytics data "
                    f"for those moments is incomplete"
                )
            else:
                logger.info("Database closed")

    # ──────────────────────────────────────────────────────────
    # WRITE METHODS (non-blocking, queued)
    # ──────────────────────────────────────────────────────────

    async def store_trade(self, token_id: str, trade: TradeEvent):
        """Store a single trade event."""
        dollar_value = trade.price * trade.size
        sql = """
            INSERT INTO trades (timestamp_ms, token_id, price, size, side, dollar_value)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        params = (
            trade.timestamp_ms,
            token_id,
            trade.price,
            trade.size,
            trade.side.value,
            dollar_value,
        )
        await self._enqueue(sql, params)

    async def store_snapshot(
        self,
        token_id: str,
        ob: OrderBook,
        metrics: Optional[Metrics] = None,
    ):
        """
        Store an order book snapshot.

        Rate-limited to SNAPSHOT_INTERVAL_SECONDS to avoid flooding
        the database with data (we get updates every millisecond but
        only need snapshots every few seconds for research).
        """
        now = time.time()
        if now - self._last_snapshot_time < settings.SNAPSHOT_INTERVAL_SECONDS:
            return  # Too soon since last snapshot
        self._last_snapshot_time = now

        bids = ob.get_sorted_bids(max_levels=50)
        asks = ob.get_sorted_asks(max_levels=50)

        bids_json = json.dumps([{"p": lvl.price, "s": lvl.size} for lvl in bids])
        asks_json = json.dumps([{"p": lvl.price, "s": lvl.size} for lvl in asks])

        sql = """
            INSERT INTO ob_snapshots
            (timestamp_ms, token_id, bids_json, asks_json, best_bid, best_ask,
             spread, obi, total_bid_depth, total_ask_depth)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            ob.last_update_ms,
            token_id,
            bids_json,
            asks_json,
            ob.best_bid,
            ob.best_ask,
            ob.spread,
            metrics.obi if metrics else None,
            ob.total_bid_depth,
            ob.total_ask_depth,
        )
        await self._enqueue(sql, params)

    async def store_metrics(self, token_id: str, metrics: Metrics):
        """Store computed metrics as a time-series row."""
        sql = """
            INSERT INTO metrics
            (timestamp_ms, token_id, obi, spread, midpoint, vwap_mid,
             flow_pressure, buy_volume, sell_volume, sentiment,
             total_bid_depth, total_ask_depth,
             price_velocity, price_trend_strength, volatility,
             regime, regime_confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            metrics.timestamp_ms,
            token_id,
            metrics.obi,
            metrics.spread,
            metrics.midpoint,
            metrics.vwap_mid,
            metrics.flow_pressure,
            metrics.buy_volume,
            metrics.sell_volume,
            metrics.sentiment,
            metrics.total_bid_depth,
            metrics.total_ask_depth,
            metrics.price_velocity,
            metrics.price_trend_strength,
            metrics.volatility,
            metrics.regime,
            metrics.regime_confidence,
        )
        await self._enqueue(sql, params)

    async def store_event(
        self,
        token_id: str,
        event_type: str,
        severity: str,
        description: str,
        price: Optional[float] = None,
        size: Optional[float] = None,
        side: Optional[str] = None,
        metadata: Optional[dict] = None,
    ):
        """
        Store a detected event (whale, spoof, absorption, sweep, wall).

        These are the interesting anomalies that you'll want to query
        later for research: "Show me all whale events in the last 24h"
        """
        sql = """
            INSERT INTO events
            (timestamp_ms, token_id, event_type, severity, description,
             price, size, side, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            int(time.time() * 1000),
            token_id,
            event_type,
            severity,
            description,
            price,
            size,
            side,
            json.dumps(metadata) if metadata else None,
        )
        await self._enqueue(sql, params)

    async def store_insights(self, token_id: str, insights: list[Insight]):
        """Store a batch of insights as events."""
        for insight in insights:
            await self.store_event(
                token_id=token_id,
                event_type="insight",
                severity=insight.severity,
                description=insight.message,
            )

    # ──────────────────────────────────────────────────────────
    # READ METHODS (for future research tools)
    # ──────────────────────────────────────────────────────────

    async def get_trades(
        self,
        token_id: str,
        since_ms: Optional[int] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Query stored trades."""
        if not self._db:
            return []

        if since_ms:
            cursor = await self._db.execute(
                "SELECT * FROM trades WHERE token_id = ? AND timestamp_ms >= ? ORDER BY timestamp_ms DESC LIMIT ?",
                (token_id, since_ms, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM trades WHERE token_id = ? ORDER BY timestamp_ms DESC LIMIT ?",
                (token_id, limit),
            )

        rows = await cursor.fetchall()
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    async def get_events(
        self,
        token_id: str,
        event_type: Optional[str] = None,
        since_ms: Optional[int] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query stored events by type."""
        if not self._db:
            return []

        query = "SELECT * FROM events WHERE token_id = ?"
        params: list = [token_id]

        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        if since_ms:
            query += " AND timestamp_ms >= ?"
            params.append(since_ms)

        query += " ORDER BY timestamp_ms DESC LIMIT ?"
        params.append(limit)

        cursor = await self._db.execute(query, params)
        rows = await cursor.fetchall()
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    async def get_stats(self, token_id: str) -> dict:
        """Get database statistics for the current session."""
        if not self._db:
            return {}

        stats = {}
        for table in ["ob_snapshots", "trades", "metrics", "events"]:
            cursor = await self._db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE token_id = ?",
                (token_id,),
            )
            row = await cursor.fetchone()
            stats[table] = row[0] if row else 0

        stats["dropped_writes"] = self.dropped_writes
        return stats

    # ──────────────────────────────────────────────────────────
    # INTERNAL: Background writer
    # ──────────────────────────────────────────────────────────

    async def _enqueue(self, sql: str, params: tuple):
        """Add a write operation to the queue."""
        try:
            self._write_queue.put_nowait((sql, params))
        except asyncio.QueueFull:
            self.dropped_writes += 1
            now = time.time()
            if now - self._last_drop_warning >= settings.DB_DROP_WARN_INTERVAL_SECONDS:
                self._last_drop_warning = now
                logger.warning(
                    f"Database write queue full — {self.dropped_writes} "
                    f"writes dropped so far this session"
                )

    async def _write_loop(self):
        """
        Background task that processes the write queue.

        Batches writes together for efficiency — commits every 50 writes
        or every 2 seconds, whichever comes first.
        """
        batch_size = 50
        flush_interval = 2.0  # the queue-get timeout IS the time-based flush

        while True:
            try:
                count = 0
                while count < batch_size:
                    try:
                        sql, params = await asyncio.wait_for(
                            self._write_queue.get(),
                            timeout=flush_interval,
                        )
                        if self._db:
                            await self._db.execute(sql, params)
                        count += 1
                    except asyncio.TimeoutError:
                        break

                # Commit the batch
                if count > 0 and self._db:
                    await self._db.commit()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Database write error: {e}")
                await asyncio.sleep(1)
