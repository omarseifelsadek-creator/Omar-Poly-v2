"""
level_tracker.py — Per-price-level history tracker.

WHY THIS MODULE EXISTS:
Phase 1 only tracked the CURRENT state of each price level.
Phase 2 needs to know HOW each level changed OVER TIME to detect:

  1. SPOOFING: "Did this level's size rapidly oscillate?"
     (large order appears → vanishes → appears → vanishes)

  2. ABSORPTION: "Is this wall holding while trades hit it?"
     (wall stays at ~same size despite trades executing at that price)

  3. PASSIVE WHALES: "Did a large order suddenly appear at a level?"
     (size jumps from 0 to 5000 in a single update)

HOW IT WORKS:
For every price level that changes, we store a timestamped history:
  { price: 0.52, side: BUY, entries: [
      (t=1000, size=0),
      (t=1001, size=5000),  ← large order placed
      (t=1005, size=0),     ← cancelled (spoofing?)
      (t=1008, size=4800),  ← placed again
      (t=1012, size=0),     ← cancelled again
  ]}

We also track which trades occurred at each level to detect absorption.

MEMORY MANAGEMENT:
- Each level keeps at most LEVEL_HISTORY_MAX_ENTRIES
- Entries older than LEVEL_HISTORY_WINDOW_SECONDS are pruned
- Levels with no recent activity are garbage-collected
"""

import time
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from config import settings
from data.models import Side, TradeEvent

logger = logging.getLogger(__name__)


@dataclass
class LevelEntry:
    """A single timestamped size record for a price level."""
    timestamp_ms: int
    size: float


@dataclass
class TradeAtLevel:
    """A trade that occurred at a specific price level."""
    timestamp_ms: int
    size: float
    side: Side  # Taker side


@dataclass
class LevelHistory:
    """
    Complete history for a single price level on one side.

    Stores:
    - Size changes over time (for spoofing detection)
    - Trades at this level (for absorption detection)
    - Peak size seen (for whale detection)
    """
    price: float
    side: Side
    entries: deque = field(default_factory=lambda: deque(maxlen=settings.LEVEL_HISTORY_MAX_ENTRIES))
    trades: deque = field(default_factory=lambda: deque(maxlen=100))
    peak_size: float = 0.0
    first_seen_ms: int = 0

    @property
    def current_size(self) -> float:
        """Most recent recorded size."""
        return self.entries[-1].size if self.entries else 0.0

    @property
    def is_alive(self) -> bool:
        """Whether this level currently has any liquidity."""
        return self.current_size > 0

    def record_size(self, timestamp_ms: int, new_size: float):
        """Record a new size observation."""
        self.entries.append(LevelEntry(timestamp_ms=timestamp_ms, size=new_size))
        if new_size > self.peak_size:
            self.peak_size = new_size
        if self.first_seen_ms == 0:
            self.first_seen_ms = timestamp_ms

    def record_trade(self, trade: TradeAtLevel):
        """Record a trade that occurred at this price level."""
        self.trades.append(trade)

    def count_oscillations(self, window_seconds: float, min_size: float) -> int:
        """
        Count rapid appear/disappear cycles within a time window.

        An "oscillation" is when the size goes from:
          significant (≥ min_size) → zero → significant again

        This is the core spoofing detection metric.

        Returns:
            Number of oscillation cycles detected
        """
        cutoff_ms = int((time.time() - window_seconds) * 1000)
        recent = [e for e in self.entries if e.timestamp_ms >= cutoff_ms]

        if len(recent) < 3:
            return 0

        oscillations = 0
        # State machine: track transitions between "significant" and "empty"
        was_significant = recent[0].size >= min_size

        for entry in recent[1:]:
            is_significant = entry.size >= min_size

            if was_significant and not is_significant:
                # Order disappeared — half an oscillation
                pass
            elif not was_significant and is_significant:
                # Order reappeared — complete oscillation
                oscillations += 1

            was_significant = is_significant

        return oscillations

    def get_max_size_in_window(self, window_seconds: float) -> float:
        """Get the largest size seen within a time window."""
        cutoff_ms = int((time.time() - window_seconds) * 1000)
        recent = [e for e in self.entries if e.timestamp_ms >= cutoff_ms]
        return max((e.size for e in recent), default=0.0)

    def get_trades_in_window(self, window_seconds: float) -> list[TradeAtLevel]:
        """Get trades at this level within a time window."""
        cutoff_ms = int((time.time() - window_seconds) * 1000)
        return [t for t in self.trades if t.timestamp_ms >= cutoff_ms]

    def get_size_at_time(self, window_seconds_ago: float) -> Optional[float]:
        """Get the size at approximately N seconds ago (nearest entry)."""
        target_ms = int((time.time() - window_seconds_ago) * 1000)
        best = None
        best_diff = float('inf')
        for entry in self.entries:
            diff = abs(entry.timestamp_ms - target_ms)
            if diff < best_diff:
                best_diff = diff
                best = entry.size
        return best


class LevelTracker:
    """
    Tracks per-price-level history across the entire order book.

    This is the foundation for Phase 2 detection:
    - Spoofing: rapid oscillations at a level
    - Absorption: wall holding despite trades
    - Passive whales: sudden large order appearances

    Usage:
        tracker = LevelTracker()
        tracker.record_change(price=0.52, side=Side.BUY, new_size=5000, timestamp_ms=...)
        tracker.record_trade_at_level(price=0.52, side=Side.BUY, trade=...)

        oscillations = tracker.get_level(0.52, Side.BUY).count_oscillations(60, 500)
    """

    def __init__(self):
        # Key = (price, side) → LevelHistory
        self._levels: dict[tuple[float, str], LevelHistory] = {}
        self._last_cleanup: float = time.time()

    def record_change(
        self,
        price: float,
        side: Side,
        new_size: float,
        timestamp_ms: int,
    ):
        """
        Record a size change at a price level.

        Called every time we process a price_change event.
        Creates the level history if it doesn't exist yet.
        """
        key = (price, side.value)

        if key not in self._levels:
            self._levels[key] = LevelHistory(price=price, side=side)

        self._levels[key].record_size(timestamp_ms, new_size)

        # Periodic cleanup of stale levels
        if time.time() - self._last_cleanup > 30:
            self._cleanup()

    def record_trade_at_level(
        self,
        price: float,
        side: Side,
        trade_size: float,
        trade_side: Side,
        timestamp_ms: int,
    ):
        """
        Record a trade that occurred near a price level.

        We match trades to the OPPOSITE side's level:
        - A BUY trade (taker lifts ask) → record on the ASK level
        - A SELL trade (taker hits bid) → record on the BID level

        This is used for absorption detection.
        """
        # Trade executed against the opposite side's resting order
        opposite_side = Side.SELL if trade_side == Side.BUY else Side.BUY
        key = (price, opposite_side.value)

        if key in self._levels:
            self._levels[key].record_trade(TradeAtLevel(
                timestamp_ms=timestamp_ms,
                size=trade_size,
                side=trade_side,
            ))

    def get_level(self, price: float, side: Side) -> Optional[LevelHistory]:
        """Get the history for a specific price level."""
        key = (price, side.value)
        return self._levels.get(key)

    def get_all_active_levels(self, side: Optional[Side] = None) -> list[LevelHistory]:
        """Get all levels that currently have liquidity."""
        levels = self._levels.values()
        if side is not None:
            levels = [l for l in levels if l.side == side]
        return [l for l in levels if l.is_alive]

    def get_all_levels_with_history(self, side: Optional[Side] = None) -> list[LevelHistory]:
        """Get all tracked levels (including empty ones with recent history)."""
        levels = list(self._levels.values())
        if side is not None:
            levels = [l for l in levels if l.side == side]
        return levels

    def _cleanup(self):
        """Remove levels with no recent activity to free memory."""
        self._last_cleanup = time.time()
        cutoff_ms = int((time.time() - settings.LEVEL_HISTORY_WINDOW_SECONDS) * 1000)

        stale_keys = []
        for key, level in self._levels.items():
            # Remove if no entries within the tracking window and level is empty
            if not level.is_alive and level.entries:
                latest = level.entries[-1].timestamp_ms
                if latest < cutoff_ms:
                    stale_keys.append(key)

        for key in stale_keys:
            del self._levels[key]

        if stale_keys:
            logger.debug(f"Cleaned up {len(stale_keys)} stale level histories")
