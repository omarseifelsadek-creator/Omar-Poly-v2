"""
cvd.py — Cumulative Volume Delta tracker.

CVD measures the net aggression in the market:
    CVD = Σ(Market Buys) - Σ(Market Sells)

A rising CVD means buyers are more aggressive (lifting asks).
A falling CVD means sellers are more aggressive (hitting bids).

KEY INSIGHT — DIVERGENCE:
If price is rising but CVD is falling, aggressive sellers are being
absorbed by passive buyers. This is unsustainable and often precedes
a reversal. The opposite (price falling, CVD rising) is similarly
significant.

PERSISTENCE:
This tracker lives on the OBIApp instance, NOT on the WebSocket client.
It survives WebSocket reconnects automatically. The CVD accumulates
for the entire session and only resets on restart.
"""

import time
from collections import deque
from dataclasses import dataclass, field

from config import settings
from data.models import TradeEvent, Side


@dataclass
class _CVDEntry:
    """A single timestamped volume delta."""
    timestamp_s: float
    delta: float  # Positive = buy, negative = sell


class CVDTracker:
    """
    Session-persistent Cumulative Volume Delta tracker.

    Tracks both the running session total and rolling windows
    for short-term momentum analysis.
    """

    def __init__(self, max_entries: int = 10_000):
        # Rolling window of recent deltas for windowed calculations
        self._entries: deque[_CVDEntry] = deque(maxlen=max_entries)
        # Session cumulative total (never reset within a session)
        self._cumulative: float = 0.0
        self._trade_count: int = 0

    def record_trade(self, trade: TradeEvent) -> None:
        """
        Record a trade and update the cumulative delta.

        Buy (taker lifts ask) → positive delta
        Sell (taker hits bid) → negative delta
        """
        delta = trade.size if trade.side == Side.BUY else -trade.size
        self._cumulative += delta
        self._trade_count += 1
        self._entries.append(_CVDEntry(
            timestamp_s=trade.timestamp_ms / 1000.0,
            delta=delta,
        ))

    @property
    def cumulative(self) -> float:
        """Session-total CVD."""
        return self._cumulative

    @property
    def trade_count(self) -> int:
        """Total trades recorded this session."""
        return self._trade_count

    def rolling(self, window_seconds: float) -> float:
        """
        CVD over a rolling time window.

        Args:
            window_seconds: Look-back window (e.g., 5.0, 30.0, 60.0)

        Returns:
            Sum of signed deltas within the window.
        """
        cutoff = time.time() - window_seconds
        return sum(
            e.delta for e in self._entries
            if e.timestamp_s >= cutoff
        )

    def check_divergence(self, price_trend: float) -> bool:
        """
        Check for price/CVD divergence.

        A divergence occurs when price and CVD move in opposite
        directions over the 30-second window.

        Args:
            price_trend: Price trend strength (-1 to +1).
                         Positive = price rising, negative = falling.

        Returns:
            True if divergence detected (opposing signals).
        """
        cvd_30s = self.rolling(30.0)
        threshold = settings.CVD_DIVERGENCE_THRESHOLD

        # Price rising but CVD falling = bearish divergence
        if price_trend > threshold and cvd_30s < -threshold:
            return True
        # Price falling but CVD rising = bullish divergence
        if price_trend < -threshold and cvd_30s > threshold:
            return True

        return False
