"""
detectors.py — Advanced pattern detection for order book microstructure.

THIS IS THE PHASE 2 INTELLIGENCE LAYER.
While metrics.py computes what IS happening, detectors.py identifies
PATTERNS and ANOMALIES that signal market manipulation or conviction.

DETECTORS:

1. SPOOFING DETECTOR
   What: Large orders that appear and disappear rapidly
   Why: Manipulators use phantom orders to fake supply/demand
   How: Track oscillation count per level over a time window

2. ABSORPTION DETECTOR
   What: A wall that holds while trades hit it
   Why: Strong participant defending a price — high-conviction signal
   How: Compare wall size before/after trades at that level

3. SWEEP DETECTOR
   What: Aggressive order eating through multiple price levels
   Why: Signals urgency — someone wants in/out NOW regardless of price
   How: Detect consecutive trades at increasing/decreasing prices in a short window

4. PASSIVE WHALE DETECTOR
   What: Large limit orders suddenly appearing deep in the book
   Why: Institutional positioning — they're building a position quietly
   How: Track size jumps at individual levels

DESIGN NOTE:
Each detector is a pure function that takes the current state and returns
a list of detected events. No side effects. This makes them easy to test,
tune, and replace independently.
"""

import time
import logging
from typing import Optional

from config import settings
from state.orderbook import OrderBook
from state.level_tracker import LevelTracker
from data.models import (
    Side,
    SpoofSignal,
    AbsorptionEvent,
    SweepEvent,
    WhaleEvent,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# 1. SPOOFING DETECTOR
# ──────────────────────────────────────────────────────────────

def detect_spoofing(tracker: LevelTracker) -> list[SpoofSignal]:
    """
    Detect spoofing-like behavior: large orders that rapidly appear and vanish.

    HOW SPOOFING WORKS IN PREDICTION MARKETS:
    A manipulator places a large buy order at 0.55 to make it look like
    there's strong demand. Other traders see this "wall" and buy, pushing
    the price up. The manipulator then cancels their order and sells at
    the now-higher price.

    HOW WE DETECT IT:
    For each price level, we count "oscillations" — cycles where:
      1. Size jumps above a threshold (order placed)
      2. Size drops back to near zero (order cancelled)
      3. Repeat

    If a level oscillates more than SPOOF_OSCILLATION_THRESHOLD times
    within SPOOF_WINDOW_SECONDS, we flag it.

    LIMITATIONS:
    - We can't distinguish spoofing from legitimate order amendments
    - Some oscillations are natural (market makers adjusting)
    - We set a minimum size threshold to avoid noise
    """
    signals = []
    now_ms = int(time.time() * 1000)

    # Check all levels with recent history
    for level in tracker.get_all_levels_with_history():
        oscillations = level.count_oscillations(
            window_seconds=settings.SPOOF_WINDOW_SECONDS,
            min_size=settings.SPOOF_MIN_SIZE,
        )

        if oscillations >= settings.SPOOF_OSCILLATION_THRESHOLD:
            max_size = level.get_max_size_in_window(settings.SPOOF_WINDOW_SECONDS)
            signals.append(SpoofSignal(
                price=level.price,
                side=level.side,
                oscillation_count=oscillations,
                max_size_seen=max_size,
                window_seconds=settings.SPOOF_WINDOW_SECONDS,
                timestamp_ms=now_ms,
            ))

    return signals


# ──────────────────────────────────────────────────────────────
# 2. ABSORPTION DETECTOR
# ──────────────────────────────────────────────────────────────

def detect_absorption(
    ob: OrderBook,
    tracker: LevelTracker,
) -> list[AbsorptionEvent]:
    """
    Detect absorption: a wall that holds while being hit by trades.

    WHAT IS ABSORPTION:
    Imagine a bid wall at 0.55 with 10,000 contracts. Sellers hit it
    with trades totaling 3,000 contracts. Normally the wall should shrink
    to ~7,000. But if the wall stays near 10,000, someone is REFILLING it.

    This means a large participant is actively defending that price.
    It's one of the strongest signals in market microstructure.

    HOW WE DETECT IT:
    For each wall-sized level:
    1. Count trades that hit it in the last N seconds
    2. Compare current size to what it was before the trades
    3. If it retained ≥ (1 - ABSORPTION_SIZE_TOLERANCE) of its size
       despite multiple trades → absorption detected

    WHY IT MATTERS:
    - Absorption on bid side = someone REALLY wants to buy at that price
    - Absorption on ask side = someone REALLY wants to sell at that price
    - Often precedes a strong move once the absorber has built their position
    """
    events = []
    now_ms = int(time.time() * 1000)

    # Check all active levels for absorption patterns
    for level_history in tracker.get_all_active_levels():
        # Only check levels that are large enough to be walls
        if level_history.current_size < settings.SPOOF_MIN_SIZE:
            continue

        # Get trades that hit this level recently
        recent_trades = level_history.get_trades_in_window(
            settings.ABSORPTION_WINDOW_SECONDS
        )

        if len(recent_trades) < settings.ABSORPTION_TRADE_COUNT_THRESHOLD:
            continue  # Not enough trades to judge

        # Calculate total volume absorbed
        volume_absorbed = sum(t.size for t in recent_trades)

        # Get the size this level had before the trades started
        original_size = level_history.get_size_at_time(
            settings.ABSORPTION_WINDOW_SECONDS
        )

        if original_size is None or original_size < settings.SPOOF_MIN_SIZE:
            continue

        # Calculate how much of the original size remains
        current_size = level_history.current_size
        holding_pct = current_size / original_size if original_size > 0 else 0

        # Absorption = level held most of its size despite trades
        min_holding = 1.0 - settings.ABSORPTION_SIZE_TOLERANCE
        if holding_pct >= min_holding:
            events.append(AbsorptionEvent(
                price=level_history.price,
                side=level_history.side,
                wall_size=current_size,
                trades_absorbed=len(recent_trades),
                volume_absorbed=volume_absorbed,
                holding_pct=round(holding_pct, 2),
                timestamp_ms=now_ms,
            ))

    return events


# ──────────────────────────────────────────────────────────────
# 3. SWEEP DETECTOR
# ──────────────────────────────────────────────────────────────

def detect_sweeps(ob: OrderBook) -> list[SweepEvent]:
    """
    Detect sweeps: aggressive orders eating through multiple price levels.

    WHAT IS A SWEEP:
    When someone places a market buy order for 50,000 contracts, but the
    best ask only has 5,000 — the order "sweeps" through multiple ask levels:
      - Fills 5,000 at 0.52
      - Fills 3,000 at 0.53
      - Fills 8,000 at 0.54
      - etc.

    This causes rapid price movement across multiple levels.

    HOW WE DETECT IT:
    Look at recent trades and check if consecutive trades hit
    increasing prices (buy sweep) or decreasing prices (sell sweep)
    within a short time window.

    WHY IT MATTERS:
    - Sweeps signal extreme urgency or conviction
    - A buy sweep = someone is willing to pay ANY price
    - Often triggered by new information (news, leaks)
    - The number of levels consumed indicates how aggressive the buyer/seller is
    """
    events = []
    now_ms = int(time.time() * 1000)

    trades = ob.get_trades_in_window(settings.SWEEP_WINDOW_SECONDS)
    if len(trades) < settings.SWEEP_MIN_LEVELS:
        return events

    # Sort trades by timestamp
    sorted_trades = sorted(trades, key=lambda t: t.timestamp_ms)

    # Detect buy sweeps (consecutive trades at increasing prices)
    buy_streak = _find_streak(sorted_trades, Side.BUY, ascending=True)
    if buy_streak and len(buy_streak) >= settings.SWEEP_MIN_LEVELS:
        events.append(SweepEvent(
            side=Side.BUY,
            levels_consumed=len(buy_streak),
            start_price=buy_streak[0].price,
            end_price=buy_streak[-1].price,
            total_volume=sum(t.size for t in buy_streak),
            timestamp_ms=now_ms,
        ))

    # Detect sell sweeps (consecutive trades at decreasing prices)
    sell_streak = _find_streak(sorted_trades, Side.SELL, ascending=False)
    if sell_streak and len(sell_streak) >= settings.SWEEP_MIN_LEVELS:
        events.append(SweepEvent(
            side=Side.SELL,
            levels_consumed=len(sell_streak),
            start_price=sell_streak[0].price,
            end_price=sell_streak[-1].price,
            total_volume=sum(t.size for t in sell_streak),
            timestamp_ms=now_ms,
        ))

    return events


def _find_streak(trades, side: Side, ascending: bool):
    """
    Find the longest consecutive streak of trades on one side
    at monotonically increasing (ascending) or decreasing prices.
    """
    best_streak = []
    current_streak = []

    for trade in trades:
        if trade.side != side:
            # Different side — breaks the streak
            if len(current_streak) > len(best_streak):
                best_streak = current_streak[:]
            current_streak = []
            continue

        if not current_streak:
            current_streak = [trade]
            continue

        last_price = current_streak[-1].price
        if ascending and trade.price >= last_price:
            current_streak.append(trade)
        elif not ascending and trade.price <= last_price:
            current_streak.append(trade)
        else:
            if len(current_streak) > len(best_streak):
                best_streak = current_streak[:]
            current_streak = [trade]

    # Check final streak
    if len(current_streak) > len(best_streak):
        best_streak = current_streak

    return best_streak


# ──────────────────────────────────────────────────────────────
# 4. PASSIVE WHALE DETECTOR
# ──────────────────────────────────────────────────────────────

def detect_passive_whales(tracker: LevelTracker) -> list[WhaleEvent]:
    """
    Detect large limit orders suddenly appearing in the book.

    Unlike aggressive whale trades (detected in metrics.py), these are
    PASSIVE whale orders — large limit orders placed deep in the book.
    They signal that a big player is positioning themselves.

    HOW WE DETECT:
    Look for levels where the size jumped from near-zero to above
    PASSIVE_WHALE_THRESHOLD in a single update.

    WHY IT MATTERS:
    - Large passive bids = institutional accumulation
    - Large passive asks = institutional distribution
    - Often placed just above support or below resistance
    """
    whales = []
    now_ms = int(time.time() * 1000)

    for level in tracker.get_all_active_levels():
        if level.current_size < settings.PASSIVE_WHALE_THRESHOLD:
            continue

        # Check if this is a recent appearance (size was near zero recently)
        if len(level.entries) < 2:
            continue

        # Look at the entry before the most recent one
        prev_entry = level.entries[-2]
        curr_entry = level.entries[-1]

        # Was the previous size near zero and current size is whale-sized?
        if prev_entry.size < settings.PASSIVE_WHALE_THRESHOLD * 0.1:
            # Also check this happened recently (within last 30 seconds)
            age_seconds = (now_ms - curr_entry.timestamp_ms) / 1000
            if age_seconds < 30:
                whales.append(WhaleEvent(
                    price=level.price,
                    size=level.current_size,
                    side=level.side,
                    is_taker=False,  # Passive (limit order)
                    timestamp_ms=curr_entry.timestamp_ms,
                ))

    return whales


# ──────────────────────────────────────────────────────────────
# COMBINED DETECTION
# ──────────────────────────────────────────────────────────────

def run_all_detectors(
    ob: OrderBook,
    tracker: LevelTracker,
) -> dict:
    """
    Run all Phase 2 detectors and return results.

    Returns a dictionary with all detection results, ready to be
    merged into the Metrics object.
    """
    return {
        "spoof_signals": detect_spoofing(tracker),
        "absorption_events": detect_absorption(ob, tracker),
        "sweep_events": detect_sweeps(ob),
        "passive_whales": detect_passive_whales(tracker),
    }
