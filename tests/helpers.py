"""Shared builders for analytics tests — synthetic books, trades, trackers."""

import time

from data.models import BookSnapshot, OrderLevel, Side, TradeEvent
from state.orderbook import OrderBook


def now_ms(offset_seconds: float = 0.0) -> int:
    """Wall-clock ms timestamp, shifted by offset (negative = past)."""
    return int((time.time() + offset_seconds) * 1000)


def make_book(bids=(), asks=(), asset_id: str = "tok") -> OrderBook:
    """OrderBook from [(price, size), ...] lists."""
    ob = OrderBook()
    ob.apply_snapshot(BookSnapshot(
        asset_id=asset_id,
        market="test",
        bids=[OrderLevel(price=p, size=s) for p, s in bids],
        asks=[OrderLevel(price=p, size=s) for p, s in asks],
        timestamp_ms=now_ms(),
    ))
    return ob


def make_trade(price: float, size: float, side: Side,
               offset_seconds: float = 0.0, asset_id: str = "tok") -> TradeEvent:
    return TradeEvent(
        asset_id=asset_id,
        price=price,
        size=size,
        side=side,
        timestamp_ms=now_ms(offset_seconds),
    )
