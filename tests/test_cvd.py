"""Characterization tests for analytics/cvd.py (B14)."""

import pytest

from analytics.cvd import CVDTracker
from data.models import Side
from helpers import make_trade


def test_buys_add_sells_subtract():
    cvd = CVDTracker()
    cvd.record_trade(make_trade(0.50, 100, Side.BUY))
    cvd.record_trade(make_trade(0.50, 30, Side.SELL))
    assert cvd.cumulative == pytest.approx(70.0)
    assert cvd.trade_count == 2


def test_rolling_window_excludes_old_trades():
    cvd = CVDTracker()
    cvd.record_trade(make_trade(0.50, 100, Side.BUY, offset_seconds=-60))
    cvd.record_trade(make_trade(0.50, 10, Side.BUY))
    assert cvd.rolling(30.0) == pytest.approx(10.0)
    assert cvd.cumulative == pytest.approx(110.0)   # cumulative keeps all


def test_divergence_price_up_cvd_down():
    cvd = CVDTracker()
    cvd.record_trade(make_trade(0.50, 5, Side.SELL))      # cvd_30s = -5
    assert cvd.check_divergence(price_trend=0.5) is True   # bearish divergence


def test_divergence_price_down_cvd_up():
    cvd = CVDTracker()
    cvd.record_trade(make_trade(0.50, 5, Side.BUY))        # cvd_30s = +5
    assert cvd.check_divergence(price_trend=-0.5) is True  # bullish divergence


def test_no_divergence_when_aligned():
    cvd = CVDTracker()
    cvd.record_trade(make_trade(0.50, 5, Side.BUY))
    assert cvd.check_divergence(price_trend=0.5) is False


def test_no_divergence_below_threshold():
    # Both sides must clear CVD_DIVERGENCE_THRESHOLD (0.3)
    cvd = CVDTracker()
    cvd.record_trade(make_trade(0.50, 5, Side.SELL))
    assert cvd.check_divergence(price_trend=0.1) is False


def test_entry_cap_bounds_rolling_but_not_cumulative():
    cvd = CVDTracker(max_entries=3)
    for _ in range(5):
        cvd.record_trade(make_trade(0.50, 1, Side.BUY))
    assert cvd.cumulative == pytest.approx(5.0)     # session total intact
    assert cvd.rolling(60.0) == pytest.approx(3.0)  # deque capped at 3
