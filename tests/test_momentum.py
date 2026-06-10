"""
Characterization tests for analytics/momentum.py building blocks (B14).

Covers EMATracker (EMA/velocity/accel/trend_strength) and
VolatilityTracker. The full MomentumEngine regime state machine is
deliberately NOT pinned here — it is wall-clock-coupled (duration-based
hysteresis), so unit tests would be timing-flaky; it gets covered by the
paper-run smoke instead.
"""

import pytest

from analytics.momentum import EMATracker, VolatilityTracker


# ── EMATracker ────────────────────────────────────────────────────────

def test_first_update_seeds_ema_directly():
    t = EMATracker(period=20)
    t.update(0.55)
    assert t.ema == pytest.approx(0.55)
    assert t.velocity == 0.0
    assert not t.is_initialized          # needs 3 samples


def test_constant_series_has_zero_velocity_and_strength():
    t = EMATracker(period=20)
    for _ in range(10):
        t.update(0.50)
    assert t.ema == pytest.approx(0.50)
    assert t.velocity == pytest.approx(0.0)
    assert t.trend_strength == pytest.approx(0.0)


def test_rising_series_has_positive_velocity():
    t = EMATracker(period=20)
    for v in (0.50, 0.52, 0.54, 0.56, 0.58):
        t.update(v)
    assert t.velocity > 0
    assert t.trend_strength > 0


def test_ema_update_formula():
    t = EMATracker(period=19)            # alpha = 2/20 = 0.1
    t.update(0.50)
    t.update(0.60)
    assert t.ema == pytest.approx(0.1 * 0.60 + 0.9 * 0.50)


def test_trend_strength_saturates_at_one_on_steep_moves():
    # Known behavior (audit note): velocity*100 with the 1.3x boost
    # saturates the clamp quickly — steep moves all read exactly +/-1.0.
    t = EMATracker(period=20)
    for v in (0.0, 1.0, 1.0, 1.0):
        t.update(v)
    assert t.trend_strength == 1.0

    t = EMATracker(period=20)
    for v in (1.0, 0.0, 0.0, 0.0):
        t.update(v)
    assert t.trend_strength == -1.0


def test_recent_range_tracks_min_max():
    t = EMATracker(period=20)
    for v in (0.40, 0.60, 0.50):
        t.update(v)
    assert t.recent_min == pytest.approx(0.40)
    assert t.recent_max == pytest.approx(0.60)
    assert t.recent_range == pytest.approx(0.20)


# ── VolatilityTracker ─────────────────────────────────────────────────

def test_volatility_needs_five_samples():
    v = VolatilityTracker()
    for _ in range(4):
        v.record_midpoint(0.50)
    assert v.price_volatility == 0.0


def test_constant_market_has_zero_volatility():
    v = VolatilityTracker()
    for _ in range(20):
        v.record_midpoint(0.50)
        v.record_spread(0.02)
        v.record_trade_price(0.50)
    assert v.price_volatility == pytest.approx(0.0)
    assert v.spread_volatility == pytest.approx(0.0)
    assert v.trade_price_volatility == pytest.approx(0.0)


def test_choppy_market_has_positive_bounded_volatility():
    v = VolatilityTracker()
    for i in range(40):
        v.record_midpoint(0.40 if i % 2 else 0.60)
        v.record_spread(0.01 if i % 2 else 0.05)
        v.record_trade_price(0.40 if i % 2 else 0.60)
    assert v.price_volatility > 0
    assert 0.0 < v.composite_volatility <= 1.0


def test_composite_volatility_monotone_in_chop():
    calm, wild = VolatilityTracker(), VolatilityTracker()
    for i in range(40):
        calm.record_midpoint(0.50 + (0.001 if i % 2 else -0.001))
        wild.record_midpoint(0.30 if i % 2 else 0.70)
    assert wild.composite_volatility > calm.composite_volatility
