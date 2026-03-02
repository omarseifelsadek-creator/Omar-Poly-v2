"""
orderbook.py — In-memory order book state manager.

THIS MODULE IS THE "BRAIN'S MEMORY" OF THE SYSTEM.
It maintains the current state of the order book by applying
incremental updates (price_change events) to a stored snapshot.

HOW ORDER BOOKS WORK (for beginners):
- An order book has two sides: BIDS (buyers) and ASKS (sellers)
- Each side has multiple "levels" — a price and how much is available
- BIDS are sorted highest price first (best bid = highest)
- ASKS are sorted lowest price first (best ask = lowest)
- The "spread" is the gap between best bid and best ask
- When a trade happens, the matching engine removes the filled quantity

HOW WE UPDATE IT:
1. On startup: receive a full "book" snapshot → store it
2. On each "price_change": update the specific price level
   - If new size > 0: update or insert the level
   - If new size = 0: remove the level (order cancelled/filled)
3. Periodically: verify with a fresh snapshot (consistency check)
"""

import time
import logging
from collections import deque
from typing import Optional

from data.models import (
    BookSnapshot,
    OrderLevel,
    PriceChangeEvent,
    PriceChangeItem,
    TradeEvent,
    Side,
)

logger = logging.getLogger(__name__)


class OrderBook:
    """
    Maintains the real-time state of a Polymarket order book.

    Stores bids and asks as dictionaries keyed by price for O(1) lookups.
    Also maintains a history ring buffer of recent snapshots for analytics.

    Usage:
        ob = OrderBook()
        ob.apply_snapshot(book_snapshot)      # Initial state
        ob.apply_price_change(price_change)   # Incremental update
        ob.apply_trade(trade_event)           # Record a trade

        print(ob.best_bid, ob.best_ask, ob.spread)
        print(ob.get_sorted_bids())
        print(ob.get_sorted_asks())
    """

    def __init__(self, snapshot_history_size: int = 100):
        """
        Args:
            snapshot_history_size: How many historical snapshots to keep in memory.
                Used for computing momentum and depth changes over time.
        """
        # Core state: price → size dictionaries
        # Using dicts for O(1) lookup/update by price
        self._bids: dict[float, float] = {}  # price → size
        self._asks: dict[float, float] = {}  # price → size

        # Cached sorted lists (rebuilt when dirty)
        self._sorted_bids: list[OrderLevel] = []
        self._sorted_asks: list[OrderLevel] = []
        self._dirty = True  # True = sorted lists need rebuilding

        # Best bid/ask cache (updated on every change)
        self._best_bid: Optional[float] = None
        self._best_ask: Optional[float] = None

        # Trade history (rolling window)
        self._recent_trades: deque[TradeEvent] = deque(maxlen=500)

        # Snapshot history for analytics
        self._snapshot_history: deque[dict] = deque(maxlen=snapshot_history_size)

        # Metadata
        self.asset_id: str = ""
        self.market: str = ""
        self.last_update_ms: int = 0
        self.update_count: int = 0

    # ──────────────────────────────────────────────────────────
    # PROPERTIES (read-only access to state)
    # ──────────────────────────────────────────────────────────

    @property
    def best_bid(self) -> Optional[float]:
        """Highest price someone is willing to buy at."""
        return self._best_bid

    @property
    def best_ask(self) -> Optional[float]:
        """Lowest price someone is willing to sell at."""
        return self._best_ask

    @property
    def spread(self) -> Optional[float]:
        """Difference between best ask and best bid."""
        if self._best_bid is not None and self._best_ask is not None:
            return round(self._best_ask - self._best_bid, 4)
        return None

    @property
    def midpoint(self) -> Optional[float]:
        """Simple midpoint between best bid and best ask."""
        if self._best_bid is not None and self._best_ask is not None:
            return round((self._best_bid + self._best_ask) / 2, 4)
        return None

    @property
    def total_bid_depth(self) -> float:
        """Total size across all bid levels."""
        return sum(self._bids.values())

    @property
    def total_ask_depth(self) -> float:
        """Total size across all ask levels."""
        return sum(self._asks.values())

    @property
    def recent_trades(self) -> list[TradeEvent]:
        """Recent trades, newest first."""
        return list(reversed(self._recent_trades))

    @property
    def is_initialized(self) -> bool:
        """Whether we've received at least one full snapshot."""
        return len(self._bids) > 0 or len(self._asks) > 0

    # ──────────────────────────────────────────────────────────
    # STATE UPDATES
    # ──────────────────────────────────────────────────────────

    def apply_snapshot(self, snapshot: BookSnapshot):
        """
        Replace the entire order book with a new snapshot.

        Called when:
        - We first connect to the WebSocket
        - We receive a "book" event (after trades)
        - We want to resync from REST API
        """
        # Save current state to history before replacing
        if self.is_initialized:
            self._save_to_history()

        # Clear and rebuild
        self._bids.clear()
        self._asks.clear()

        for level in snapshot.bids:
            if level.size > 0:
                self._bids[level.price] = level.size

        for level in snapshot.asks:
            if level.size > 0:
                self._asks[level.price] = level.size

        self.asset_id = snapshot.asset_id
        self.market = snapshot.market
        self.last_update_ms = snapshot.timestamp_ms
        self.update_count += 1
        self._dirty = True
        self._update_best_prices()

        logger.debug(
            f"Snapshot applied: {len(self._bids)} bids, {len(self._asks)} asks, "
            f"spread={self.spread}"
        )

    def apply_price_change(self, event: PriceChangeEvent):
        """
        Apply incremental price level updates.

        Each PriceChangeItem tells us: "at price X, side Y, the new
        total size is Z". If Z=0, that level was completely removed.

        This is how we maintain tick-level state without needing
        a full snapshot every time.
        """
        for change in event.price_changes:
            # Only process changes for our tracked token
            if self.asset_id and change.asset_id != self.asset_id:
                continue

            self._apply_single_change(change)

        self.last_update_ms = event.timestamp_ms
        self.update_count += 1
        self._dirty = True
        self._update_best_prices()

    def _apply_single_change(self, change: PriceChangeItem):
        """Apply a single price level change to the book."""
        book = self._bids if change.side == Side.BUY else self._asks

        if change.size > 0:
            # Update or insert the level
            book[change.price] = change.size
        else:
            # Remove the level (size went to zero)
            book.pop(change.price, None)

        # Update best prices from the change event if provided
        if change.best_bid is not None:
            self._best_bid = change.best_bid
        if change.best_ask is not None:
            self._best_ask = change.best_ask

    def apply_trade(self, trade: TradeEvent):
        """
        Record a trade event.

        Trades don't directly modify our order book (the "book" event
        handles that), but we store them for flow analysis.
        """
        self._recent_trades.append(trade)
        self.last_update_ms = trade.timestamp_ms

    # ──────────────────────────────────────────────────────────
    # DATA ACCESS
    # ──────────────────────────────────────────────────────────

    def get_sorted_bids(self, max_levels: int = 50) -> list[OrderLevel]:
        """Get bids sorted by price (highest first), limited to max_levels."""
        if self._dirty:
            self._rebuild_sorted()
        return self._sorted_bids[:max_levels]

    def get_sorted_asks(self, max_levels: int = 50) -> list[OrderLevel]:
        """Get asks sorted by price (lowest first), limited to max_levels."""
        if self._dirty:
            self._rebuild_sorted()
        return self._sorted_asks[:max_levels]

    def get_bids_dict(self) -> dict[float, float]:
        """Get raw bids dictionary (price → size). Don't modify!"""
        return self._bids

    def get_asks_dict(self) -> dict[float, float]:
        """Get raw asks dictionary (price → size). Don't modify!"""
        return self._asks

    def get_trades_in_window(self, window_seconds: float) -> list[TradeEvent]:
        """Get all trades within the last N seconds."""
        cutoff_ms = int((time.time() - window_seconds) * 1000)
        return [t for t in self._recent_trades if t.timestamp_ms >= cutoff_ms]

    def get_depth_at_price(self, price: float, side: Side) -> float:
        """Get the size available at a specific price level."""
        book = self._bids if side == Side.BUY else self._asks
        return book.get(price, 0.0)

    def get_snapshot_history(self) -> list[dict]:
        """Get historical snapshots for momentum analysis."""
        return list(self._snapshot_history)

    # ──────────────────────────────────────────────────────────
    # INTERNAL HELPERS
    # ──────────────────────────────────────────────────────────

    def _rebuild_sorted(self):
        """Rebuild sorted bid/ask lists from the dictionaries."""
        self._sorted_bids = [
            OrderLevel(price=p, size=s)
            for p, s in sorted(self._bids.items(), reverse=True)
        ]
        self._sorted_asks = [
            OrderLevel(price=p, size=s)
            for p, s in sorted(self._asks.items())
        ]
        self._dirty = False

    def _update_best_prices(self):
        """Recalculate best bid and ask from the current state."""
        self._best_bid = max(self._bids.keys()) if self._bids else None
        self._best_ask = min(self._asks.keys()) if self._asks else None

    def _save_to_history(self):
        """Save current state as a historical snapshot."""
        self._snapshot_history.append({
            "timestamp_ms": self.last_update_ms,
            "best_bid": self._best_bid,
            "best_ask": self._best_ask,
            "total_bid_depth": self.total_bid_depth,
            "total_ask_depth": self.total_ask_depth,
            "bid_levels": len(self._bids),
            "ask_levels": len(self._asks),
        })
