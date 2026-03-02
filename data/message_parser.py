"""
message_parser.py — Parse raw WebSocket JSON into typed data models.

WHY THIS MODULE:
The WebSocket sends us raw JSON strings. This module is the "translator"
that converts messy JSON into clean, typed Python objects. If Polymarket
changes their message format, we only fix this one file.

BEGINNER NOTE:
"Parsing" means taking unstructured text (JSON) and converting it into
structured objects (our Pydantic models) that the rest of the code can
work with safely.
"""

import json
import logging
from typing import Optional, Union

from data.models import (
    BookSnapshot,
    OrderLevel,
    PriceChangeEvent,
    PriceChangeItem,
    TradeEvent,
    EventType,
    Side,
)

# Set up logging — prints warnings/errors to terminal
logger = logging.getLogger(__name__)


ParsedMessage = Union[BookSnapshot, PriceChangeEvent, TradeEvent]


def _parse_single(data: dict) -> Optional[ParsedMessage]:
    """Parse a single JSON object into the appropriate data model."""
    event_type = data.get("event_type")

    if event_type == EventType.BOOK:
        return _parse_book(data)
    elif event_type == EventType.PRICE_CHANGE:
        return _parse_price_change(data)
    elif event_type == EventType.LAST_TRADE_PRICE:
        return _parse_trade(data)
    elif event_type == EventType.TICK_SIZE_CHANGE:
        logger.info(f"Tick size changed: {data}")
        return None
    else:
        logger.debug(f"Unknown event type: {event_type}")
        return None


def parse_messages(raw: str) -> list[ParsedMessage]:
    """
    Parse a raw WebSocket message, returning ALL results.

    Polymarket sometimes sends arrays of messages in a single frame.
    This function handles both single objects and arrays, returning
    every successfully parsed message.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse JSON: {e}")
        return []

    items = data if isinstance(data, list) else [data]
    results = []
    for item in items:
        parsed = _parse_single(item)
        if parsed is not None:
            results.append(parsed)
    return results


def parse_message(raw: str) -> Optional[ParsedMessage]:
    """
    Parse a raw WebSocket message, returning the first result.

    For callers that only need a single result. For full batch
    support, use parse_messages() instead.
    """
    results = parse_messages(raw)
    return results[0] if results else None


def _parse_book(data: dict) -> Optional[BookSnapshot]:
    """
    Parse a "book" event into a BookSnapshot.

    The "book" event gives us the FULL order book — all bids and asks.
    This arrives when we first subscribe and after each trade.

    Raw format:
    {
        "event_type": "book",
        "asset_id": "6581...",
        "market": "0xbd31...",
        "bids": [{"price": ".48", "size": "30"}, ...],
        "asks": [{"price": ".52", "size": "25"}, ...],
        "timestamp": "123456789000",
        "hash": "0x..."
    }
    """
    try:
        # Convert string prices/sizes to floats
        bids = [
            OrderLevel(price=float(b["price"]), size=float(b["size"]))
            for b in data.get("bids", [])
            if float(b.get("size", 0)) > 0  # Skip empty levels
        ]
        asks = [
            OrderLevel(price=float(a["price"]), size=float(a["size"]))
            for a in data.get("asks", [])
            if float(a.get("size", 0)) > 0
        ]

        # Sort: bids highest first, asks lowest first
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return BookSnapshot(
            asset_id=data.get("asset_id", ""),
            market=data.get("market", ""),
            bids=bids,
            asks=asks,
            timestamp_ms=int(data.get("timestamp", "0")),
            hash=data.get("hash", ""),
        )
    except (ValueError, KeyError, TypeError) as e:
        logger.warning(f"Failed to parse book event: {e}")
        return None


def _parse_price_change(data: dict) -> Optional[PriceChangeEvent]:
    """
    Parse a "price_change" event.

    This is the MOST FREQUENT event — emitted every time an order is
    placed or cancelled. Each event can contain multiple price level changes.

    Raw format:
    {
        "event_type": "price_change",
        "market": "0x5f65...",
        "price_changes": [
            {
                "asset_id": "7132...",
                "price": "0.5",
                "size": "200",      ← NEW total size at this level
                "side": "BUY",
                "hash": "5662...",
                "best_bid": "0.5",
                "best_ask": "1"
            }
        ],
        "timestamp": "1757908892351"
    }
    """
    try:
        changes = []
        for pc in data.get("price_changes", []):
            # Parse the side string into our enum
            side_str = pc.get("side", "").upper()
            if side_str not in ("BUY", "SELL"):
                continue

            # Parse best_bid and best_ask (may be absent or "0")
            best_bid = None
            best_ask = None
            if pc.get("best_bid") and pc["best_bid"] != "0":
                best_bid = float(pc["best_bid"])
            if pc.get("best_ask") and pc["best_ask"] != "0":
                best_ask = float(pc["best_ask"])

            changes.append(PriceChangeItem(
                asset_id=pc.get("asset_id", ""),
                price=float(pc["price"]),
                size=float(pc["size"]),
                side=Side(side_str),
                hash=pc.get("hash", ""),
                best_bid=best_bid,
                best_ask=best_ask,
            ))

        if not changes:
            return None

        return PriceChangeEvent(
            market=data.get("market", ""),
            price_changes=changes,
            timestamp_ms=int(data.get("timestamp", "0")),
        )
    except (ValueError, KeyError, TypeError) as e:
        logger.warning(f"Failed to parse price_change event: {e}")
        return None


def _parse_trade(data: dict) -> Optional[TradeEvent]:
    """
    Parse a "last_trade_price" event.

    Emitted when a trade is executed. Tells us the price, size, and
    whether the taker was buying or selling.

    Raw format:
    {
        "event_type": "last_trade_price",
        "asset_id": "1141...",
        "price": "0.456",
        "size": "219.217767",
        "side": "BUY",
        "market": "0x6a67...",
        "fee_rate_bps": "0",
        "timestamp": "1750428146322"
    }
    """
    try:
        side_str = data.get("side", "").upper()
        if side_str not in ("BUY", "SELL"):
            logger.warning(f"Unknown trade side: {side_str}")
            return None

        return TradeEvent(
            asset_id=data.get("asset_id", ""),
            price=float(data["price"]),
            size=float(data["size"]),
            side=Side(side_str),
            timestamp_ms=int(data.get("timestamp", "0")),
            fee_rate_bps=data.get("fee_rate_bps", "0"),
        )
    except (ValueError, KeyError, TypeError) as e:
        logger.warning(f"Failed to parse trade event: {e}")
        return None
