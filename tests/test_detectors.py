"""
Characterization tests for analytics/detectors.py (B14).

Spoofing, absorption, and sweep detection on synthetic level histories
and trade tapes. Pins current thresholds from config/settings.py:
SPOOF_MIN_SIZE=500, SPOOF_OSCILLATION_THRESHOLD=3, SWEEP_MIN_LEVELS=3,
ABSORPTION_TRADE_COUNT_THRESHOLD=3, ABSORPTION_SIZE_TOLERANCE=0.3.
"""

from analytics.detectors import detect_absorption, detect_spoofing, detect_sweeps
from data.models import Side
from state.level_tracker import LevelTracker
from helpers import make_book, make_trade, now_ms


# ── Spoofing ──────────────────────────────────────────────────────────

def _oscillate(tracker: LevelTracker, price: float, side: Side, cycles: int):
    """size >= 500 appearing/vanishing `cycles` times, recent timestamps."""
    t = -50.0  # within the 60s spoof window
    tracker.record_change(price, side, 600, now_ms(t))
    for _ in range(cycles):
        t += 2
        tracker.record_change(price, side, 0, now_ms(t))
        t += 2
        tracker.record_change(price, side, 600, now_ms(t))


def test_spoofing_flagged_after_three_oscillations():
    tracker = LevelTracker()
    _oscillate(tracker, 0.55, Side.BUY, cycles=3)
    signals = detect_spoofing(tracker)
    assert len(signals) == 1
    assert signals[0].price == 0.55
    assert signals[0].oscillation_count >= 3


def test_two_oscillations_not_spoofing():
    tracker = LevelTracker()
    _oscillate(tracker, 0.55, Side.BUY, cycles=2)
    assert detect_spoofing(tracker) == []


def test_small_orders_never_spoofing():
    # Oscillates but below SPOOF_MIN_SIZE=500 — noise, not spoofing
    tracker = LevelTracker()
    t = -50.0
    for _ in range(4):
        tracker.record_change(0.55, Side.BUY, 100, now_ms(t)); t += 2
        tracker.record_change(0.55, Side.BUY, 0, now_ms(t)); t += 2
    assert detect_spoofing(tracker) == []


# ── Absorption ────────────────────────────────────────────────────────

def _wall_under_fire(holding_size: float) -> LevelTracker:
    """A 1000-size bid wall hit by 3 sells, ending at holding_size."""
    tracker = LevelTracker()
    tracker.record_change(0.55, Side.BUY, 1000, now_ms(-28))
    for offset in (-20, -15, -10):
        # SELL trades hit the bid — tracker derives the BUY level itself
        tracker.record_trade_at_level(
            price=0.55, trade_size=50, trade_side=Side.SELL,
            timestamp_ms=now_ms(offset),
        )
    tracker.record_change(0.55, Side.BUY, holding_size, now_ms(-2))
    return tracker


def test_absorption_when_wall_holds_through_trades():
    tracker = _wall_under_fire(holding_size=900)   # holds 90% >= 70%
    ob = make_book(bids=[(0.55, 900)], asks=[(0.60, 100)])
    events = detect_absorption(ob, tracker)
    assert len(events) == 1
    assert events[0].price == 0.55 and events[0].side == Side.BUY
    assert events[0].trades_absorbed == 3
    assert events[0].holding_pct >= 0.7


def test_no_absorption_when_wall_collapses():
    tracker = _wall_under_fire(holding_size=200)   # lost 80%
    ob = make_book(bids=[(0.55, 200)], asks=[(0.60, 100)])
    assert detect_absorption(ob, tracker) == []


def test_no_absorption_with_too_few_trades():
    tracker = LevelTracker()
    tracker.record_change(0.55, Side.BUY, 1000, now_ms(-28))
    tracker.record_trade_at_level(
        price=0.55, trade_size=50, trade_side=Side.SELL, timestamp_ms=now_ms(-10)
    )
    tracker.record_change(0.55, Side.BUY, 950, now_ms(-2))
    ob = make_book(bids=[(0.55, 950)], asks=[(0.60, 100)])
    assert detect_absorption(ob, tracker) == []   # 1 trade < threshold of 3


# ── Sweeps ────────────────────────────────────────────────────────────

def test_buy_sweep_on_ascending_prices():
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    for i, price in enumerate((0.50, 0.51, 0.52)):
        ob.apply_trade(make_trade(price, 100, Side.BUY, offset_seconds=-3 + i))
    events = detect_sweeps(ob)
    assert len(events) == 1
    sweep = events[0]
    assert sweep.side == Side.BUY
    assert sweep.levels_consumed == 3
    assert (sweep.start_price, sweep.end_price) == (0.50, 0.52)
    assert sweep.total_volume == 300


def test_sell_sweep_on_descending_prices():
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    for i, price in enumerate((0.52, 0.51, 0.50)):
        ob.apply_trade(make_trade(price, 100, Side.SELL, offset_seconds=-3 + i))
    events = detect_sweeps(ob)
    assert len(events) == 1 and events[0].side == Side.SELL


def test_opposite_side_trade_breaks_streak():
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    sequence = [
        (0.50, Side.BUY), (0.51, Side.BUY),
        (0.51, Side.SELL),                    # breaks the buy streak
        (0.52, Side.BUY),
    ]
    for i, (price, side) in enumerate(sequence):
        ob.apply_trade(make_trade(price, 100, side, offset_seconds=-4 + i))
    assert detect_sweeps(ob) == []            # no streak reaches 3


def test_choppy_prices_are_not_a_sweep():
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    for i, price in enumerate((0.50, 0.49, 0.51, 0.48)):
        ob.apply_trade(make_trade(price, 100, Side.BUY, offset_seconds=-4 + i))
    assert detect_sweeps(ob) == []


def test_flat_prices_count_toward_buy_sweep():
    # Current behavior: equal prices extend an ascending streak (>=)
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    for i, price in enumerate((0.50, 0.50, 0.51)):
        ob.apply_trade(make_trade(price, 100, Side.BUY, offset_seconds=-3 + i))
    events = detect_sweeps(ob)
    assert len(events) == 1 and events[0].levels_consumed == 3
