"""
Characterization tests for analytics/metrics.py pure functions (B14).

These pin CURRENT behavior so the Phase 4 refactor (and future tuning)
can prove behavior preservation. Synthetic books/trades only — no network.
"""

import pytest

from analytics.metrics import (
    compute_flow_pressure,
    compute_imbalance,
    compute_vwap_mid,
    detect_walls,
    detect_whale_trades,
)
from data.models import Side
from helpers import make_book, make_trade


# ── OBI ────────────────────────────────────────────────────────────────

def test_obi_balanced_book_is_neutral():
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    assert compute_imbalance(ob) == pytest.approx(0.5)


def test_obi_bid_heavy_book():
    ob = make_book(
        bids=[(0.49, 100), (0.48, 100), (0.47, 100)],
        asks=[(0.51, 100)],
    )
    assert compute_imbalance(ob) == pytest.approx(0.75)


def test_obi_uses_only_top_five_levels():
    # 6 bid levels — the 6th must be ignored (OB_IMBALANCE_LEVELS = 5)
    bids = [(0.49 - i * 0.01, 100) for i in range(6)]
    ob = make_book(bids=bids, asks=[(0.51, 100)])
    assert compute_imbalance(ob) == pytest.approx(500 / 600, abs=1e-4)


def test_obi_empty_book_returns_none():
    assert compute_imbalance(make_book()) is None


def test_obi_asks_only_is_zero():
    ob = make_book(asks=[(0.51, 100)])
    assert compute_imbalance(ob) == 0.0


# ── VWAP midpoint ─────────────────────────────────────────────────────

def test_vwap_mid_equal_sizes_is_simple_mid():
    ob = make_book(bids=[(0.48, 500)], asks=[(0.52, 500)])
    assert compute_vwap_mid(ob) == pytest.approx(0.50)


def test_vwap_mid_leans_toward_thin_side():
    # Docstring example: thin asks -> vwap closer to the ask
    ob = make_book(bids=[(0.48, 1000)], asks=[(0.52, 100)])
    vwap = compute_vwap_mid(ob)
    assert vwap == pytest.approx((0.48 * 100 + 0.52 * 1000) / 1100, abs=1e-4)
    assert vwap > 0.50


def test_vwap_mid_one_sided_book_returns_none():
    assert compute_vwap_mid(make_book(bids=[(0.48, 100)])) is None


# ── Flow pressure ─────────────────────────────────────────────────────

def test_flow_pressure_all_buys_is_plus_one():
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    for _ in range(3):
        ob.apply_trade(make_trade(0.51, 100, Side.BUY))
    assert compute_flow_pressure(ob)["pressure"] == pytest.approx(1.0)


def test_flow_pressure_all_sells_is_minus_one():
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    ob.apply_trade(make_trade(0.49, 100, Side.SELL))
    assert compute_flow_pressure(ob)["pressure"] == pytest.approx(-1.0)


def test_flow_pressure_is_dollar_weighted():
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    ob.apply_trade(make_trade(0.50, 100, Side.BUY))   # $50 buy
    ob.apply_trade(make_trade(0.50, 40, Side.SELL))   # $20 sell
    result = compute_flow_pressure(ob)
    assert result["pressure"] == pytest.approx((50 - 20) / 70, abs=1e-4)
    assert result["buy_volume"] == pytest.approx(50.0)
    assert result["sell_volume"] == pytest.approx(20.0)


def test_flow_pressure_no_trades_is_zero():
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    assert compute_flow_pressure(ob)["pressure"] == 0.0


def test_flow_pressure_ignores_trades_outside_window():
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    ob.apply_trade(make_trade(0.50, 100, Side.BUY, offset_seconds=-200))
    assert compute_flow_pressure(ob)["pressure"] == 0.0  # 120s window


# ── Walls ─────────────────────────────────────────────────────────────

def test_wall_detected_on_outsized_level():
    bids = [(0.49 - i * 0.01, 100) for i in range(5)] + [(0.43, 1500)]
    asks = [(0.51 + i * 0.01, 100) for i in range(5)]
    walls = detect_walls(make_book(bids=bids, asks=asks))
    assert len(walls) == 1
    assert walls[0].price == 0.43 and walls[0].side == Side.BUY
    assert walls[0].strength > 0


def test_uniform_book_has_no_walls():
    bids = [(0.49 - i * 0.01, 100) for i in range(5)]
    asks = [(0.51 + i * 0.01, 100) for i in range(5)]
    assert detect_walls(make_book(bids=bids, asks=asks)) == []


def test_tiny_book_has_no_walls():
    assert detect_walls(make_book(bids=[(0.49, 100)], asks=[(0.51, 5000)])) == []


# ── Whale trades ──────────────────────────────────────────────────────

def test_whale_trade_flagged_above_dollar_threshold():
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    ob.apply_trade(make_trade(0.50, 12_000, Side.BUY))   # $6,000 >= $5,000
    whales = detect_whale_trades(ob)
    assert len(whales) == 1 and whales[0].side == Side.BUY


def test_small_trade_not_a_whale():
    ob = make_book(bids=[(0.49, 100)], asks=[(0.51, 100)])
    ob.apply_trade(make_trade(0.50, 8_000, Side.BUY))    # $4,000 < $5,000
    assert detect_whale_trades(ob) == []
