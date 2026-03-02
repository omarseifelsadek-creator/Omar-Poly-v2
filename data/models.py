"""
models.py — Data models for the entire OBI system.

WHY PYDANTIC:
Pydantic gives us "typed dictionaries" with automatic validation.
When raw JSON arrives from the WebSocket, we parse it into these
models. If the data is malformed, Pydantic catches it immediately
instead of causing cryptic errors deep in the analytics engine.

BEGINNER NOTE:
Think of each class below as a "shape" for data. When you create
an OrderLevel(price=0.52, size=200), Python guarantees those fields
exist and are the right type. This prevents bugs.
"""

from pydantic import BaseModel
from enum import Enum
from typing import Optional
import time


# ──────────────────────────────────────────────────────────────
# ENUMS — Named constants instead of raw strings
# ──────────────────────────────────────────────────────────────

class Side(str, Enum):
    """
    Which side of the order book.
    str, Enum means it behaves like both a string AND an enum,
    so you can do: Side.BUY == "BUY"  → True
    """
    BUY = "BUY"
    SELL = "SELL"


class EventType(str, Enum):
    """Types of WebSocket messages we receive from Polymarket."""
    BOOK = "book"
    PRICE_CHANGE = "price_change"
    LAST_TRADE_PRICE = "last_trade_price"
    TICK_SIZE_CHANGE = "tick_size_change"


# ──────────────────────────────────────────────────────────────
# ORDER BOOK DATA
# ──────────────────────────────────────────────────────────────

class OrderLevel(BaseModel):
    """
    A single price level in the order book.
    Example: OrderLevel(price=0.52, size=200)
    means there are 200 contracts available at price 0.52
    """
    price: float
    size: float


class BookSnapshot(BaseModel):
    """
    A complete order book at a point in time.
    This is what we receive on the "book" WebSocket event.
    """
    asset_id: str
    market: str
    bids: list[OrderLevel]          # Buy orders, sorted highest price first
    asks: list[OrderLevel]          # Sell orders, sorted lowest price first
    timestamp_ms: int               # Unix timestamp in milliseconds
    hash: str = ""

    @property
    def best_bid(self) -> Optional[float]:
        """Highest price someone is willing to buy at."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        """Lowest price someone is willing to sell at."""
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        """Difference between best ask and best bid."""
        if self.best_bid is not None and self.best_ask is not None:
            return round(self.best_ask - self.best_bid, 4)
        return None

    @property
    def midpoint(self) -> Optional[float]:
        """Simple midpoint between best bid and best ask."""
        if self.best_bid is not None and self.best_ask is not None:
            return round((self.best_bid + self.best_ask) / 2, 4)
        return None


# ──────────────────────────────────────────────────────────────
# WEBSOCKET EVENT MODELS
# ──────────────────────────────────────────────────────────────

class PriceChangeItem(BaseModel):
    """
    A single price level change from the "price_change" event.
    This tells us: at this price, for this side, the new total size is X.
    """
    asset_id: str
    price: float
    size: float                     # New TOTAL size at this level (not delta)
    side: Side
    hash: str = ""
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None


class PriceChangeEvent(BaseModel):
    """
    Emitted when orders are placed or cancelled.
    Contains one or more price level updates.
    """
    market: str
    price_changes: list[PriceChangeItem]
    timestamp_ms: int


class TradeEvent(BaseModel):
    """
    Emitted when a trade is executed (maker + taker matched).
    side = BUY means the taker was a buyer (they lifted the ask).
    side = SELL means the taker was a seller (they hit the bid).
    """
    asset_id: str
    price: float
    size: float
    side: Side
    timestamp_ms: int
    fee_rate_bps: str = "0"


# ──────────────────────────────────────────────────────────────
# ANALYTICS OUTPUT MODELS
# ──────────────────────────────────────────────────────────────

class WallInfo(BaseModel):
    """A detected liquidity wall."""
    price: float
    size: float
    side: Side                      # BUY = support wall, SELL = resistance wall
    strength: float                 # How many std devs above mean (e.g., 2.5)


class WhaleEvent(BaseModel):
    """A detected large order or trade."""
    price: float
    size: float
    side: Side
    is_taker: bool                  # True = aggressive (market order), False = passive (limit)
    timestamp_ms: int


class SpoofSignal(BaseModel):
    """
    A detected spoofing-like pattern.

    Spoofing = placing large orders with no intention to fill them,
    then cancelling before they execute. Used to manipulate perceived
    supply/demand.

    We detect this by tracking rapid size oscillations at a price level:
    large order appears → disappears → appears → disappears within a window.
    """
    price: float
    side: Side
    oscillation_count: int          # How many appear/disappear cycles
    max_size_seen: float            # Largest size that appeared
    window_seconds: float           # Over what time period
    timestamp_ms: int


class AbsorptionEvent(BaseModel):
    """
    A detected absorption pattern.

    Absorption = a large resting order (wall) that holds its ground
    while aggressive trades hit it. The wall absorbs selling/buying
    pressure without breaking.

    This is a STRONG signal — it means a large participant is
    committed to defending that price level.
    """
    price: float
    side: Side                      # BUY = absorbing sells, SELL = absorbing buys
    wall_size: float                # Current wall size
    trades_absorbed: int            # Number of trades the wall survived
    volume_absorbed: float          # Total volume absorbed
    holding_pct: float              # What % of original size remains
    timestamp_ms: int


class SweepEvent(BaseModel):
    """
    A detected sweep — an aggressive order eating through multiple levels.

    Sweeps happen when someone places a large market order that exceeds
    the best level's liquidity, causing it to "sweep" through multiple
    price levels. This signals urgency and strong conviction.
    """
    side: Side                      # BUY = buyer sweeping asks upward
    levels_consumed: int            # How many levels were eaten
    start_price: float              # Price where sweep started
    end_price: float                # Price where sweep ended
    total_volume: float             # Total contracts swept
    timestamp_ms: int


class Metrics(BaseModel):
    """
    All computed metrics at a point in time.
    This is the main output of the analytics engine.
    """
    timestamp_ms: int

    # Order book structure
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None
    midpoint: Optional[float] = None
    vwap_mid: Optional[float] = None

    # Imbalance
    obi: Optional[float] = None     # 0.0 = all asks, 1.0 = all bids
    total_bid_depth: float = 0.0
    total_ask_depth: float = 0.0

    # Flow
    flow_pressure: float = 0.0      # -1.0 = pure selling, +1.0 = pure buying
    buy_volume: float = 0.0         # Total buy volume in window
    sell_volume: float = 0.0        # Total sell volume in window

    # Detections
    walls: list[WallInfo] = []
    whale_events: list[WhaleEvent] = []
    spoof_signals: list["SpoofSignal"] = []
    absorption_events: list["AbsorptionEvent"] = []
    sweep_events: list["SweepEvent"] = []

    # Composite
    sentiment: float = 0.0          # -1.0 to +1.0

    # Phase 3: Momentum & Regime
    price_velocity: float = 0.0      # Rate of midpoint change
    price_accel: float = 0.0         # Acceleration of midpoint
    price_trend_strength: float = 0.0  # -1 to +1 trend strength
    obi_velocity: float = 0.0       # Rate of imbalance change
    obi_trend_strength: float = 0.0  # OBI trend strength
    depth_divergence: float = 0.0    # Bid depth growing + ask shrinking = positive
    flow_trend_strength: float = 0.0 # Flow momentum strength
    volatility: float = 0.0         # 0.0 to 1.0 composite volatility
    regime: str = "QUIET"           # Current market regime
    regime_confidence: float = 0.0   # 0.0 to 1.0
    regime_duration_s: float = 0.0   # Seconds in current regime


class Insight(BaseModel):
    """A single natural language insight with metadata."""
    timestamp_ms: int
    message: str
    severity: str = "info"          # "info", "warning", "alert"

    @property
    def time_str(self) -> str:
        """Format timestamp as HH:MM:SS for display."""
        t = time.localtime(self.timestamp_ms / 1000)
        return time.strftime("%H:%M:%S", t)
