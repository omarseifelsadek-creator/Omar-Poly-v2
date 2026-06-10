"""
metrics.py — Core quantitative metrics computed from order book state.

THIS IS THE ANALYTICS ENGINE.
Takes the raw order book state and computes actionable metrics:
imbalance, flow pressure, VWAP mid, depth analysis, etc.

DESIGN PRINCIPLE:
Every metric is a pure function: (orderbook_state) → number.
No side effects, no state mutation. This makes testing easy
and ensures metrics are always consistent with the current book.

BEGINNER NOTE:
Each function here implements a formula from the blueprint.
Comments explain both WHAT the metric measures and WHY it matters.
"""

import time
import numpy as np
from typing import Optional, TYPE_CHECKING

from config import settings
from state.orderbook import OrderBook
from state.level_tracker import LevelTracker
from data.models import (
    Metrics,
    WallInfo,
    WhaleEvent,
    LiquidityVoid,
    Side,
)
from analytics.detectors import run_all_detectors

if TYPE_CHECKING:
    # Imported lazily at runtime (inside functions) to avoid the
    # metrics <-> momentum circular import; quoted annotations resolve here.
    from analytics.cvd import CVDTracker
    from analytics.momentum import MomentumEngine, MomentumState


def compute_all_metrics(
    ob: OrderBook,
    level_tracker: LevelTracker | None = None,
    momentum_engine: "MomentumEngine | None" = None,
    cvd_tracker: "CVDTracker | None" = None,
) -> Metrics:
    """
    Compute ALL metrics from the current order book state.

    This is the main entry point — called after every order book update.
    Returns a Metrics object containing every computed value.

    Args:
        ob: The current order book state
        level_tracker: Optional Phase 2 level tracker for advanced detection
        momentum_engine: Optional Phase 3 momentum engine for trend/regime analysis

    Returns:
        Metrics object with all computed values
    """
    now_ms = int(time.time() * 1000)

    if not ob.is_initialized:
        return Metrics(timestamp_ms=now_ms)

    # Phase 1 metrics
    obi = compute_imbalance(ob)
    vwap_mid = compute_vwap_mid(ob)
    flow = compute_flow_pressure(ob)
    walls = detect_walls(ob)
    whales = detect_whale_trades(ob)

    # Phase 2 detectors
    spoof_signals = []
    absorption_events = []
    sweep_events = []

    if level_tracker is not None:
        detections = run_all_detectors(ob, level_tracker)
        spoof_signals = detections["spoof_signals"]
        absorption_events = detections["absorption_events"]
        sweep_events = detections["sweep_events"]
        # Add passive whales to the whale list
        whales.extend(detections["passive_whales"])

    # Phase 3: Update momentum engine and get state
    momentum_state = None
    if momentum_engine is not None:
        momentum_engine.update(
            midpoint=ob.midpoint,
            obi=obi,
            spread=ob.spread,
            flow_pressure=flow["pressure"],
            bid_depth=ob.total_bid_depth,
            ask_depth=ob.total_ask_depth,
        )
        momentum_state = momentum_engine.get_state()

    # Compute sentiment (Phase 3 enhanced if momentum available)
    sentiment = compute_sentiment_v3(
        obi=obi,
        flow_pressure=flow["pressure"],
        ob=ob,
        momentum_state=momentum_state,
        absorption_events=absorption_events,
        sweep_events=sweep_events,
    )

    # Phase 4: Liquidity voids
    voids = detect_liquidity_voids(ob)

    # Phase 4: OBI velocity
    obi_vel = compute_obi_velocity(momentum_engine) if momentum_engine else {}

    # Phase 4: CVD
    cvd_data: dict = {}
    if cvd_tracker is not None:
        cvd_5s = cvd_tracker.rolling(5.0)
        cvd_30s = cvd_tracker.rolling(30.0)
        price_trend = 0.0
        if momentum_engine is not None:
            ms = momentum_engine.get_state()
            if ms and ms.is_ready:
                price_trend = ms.price_trend_strength
        cvd_data = {
            "cvd": cvd_tracker.cumulative,
            "cvd_5s": round(cvd_5s, 2),
            "cvd_30s": round(cvd_30s, 2),
            "cvd_divergence": cvd_tracker.check_divergence(price_trend),
        }

    # Phase 3: Build momentum fields dict (avoids post-construction mutation)
    momentum_fields: dict = {}
    if momentum_state and momentum_state.is_ready:
        momentum_fields = {
            "price_velocity": round(momentum_state.price_velocity, 6),
            "price_accel": round(momentum_state.price_accel, 6),
            "price_trend_strength": round(momentum_state.price_trend_strength, 4),
            "obi_velocity": round(momentum_state.obi_velocity, 6),
            "obi_trend_strength": round(momentum_state.obi_trend_strength, 4),
            "depth_divergence": round(momentum_state.depth_divergence, 4),
            "flow_trend_strength": round(momentum_state.flow_trend_strength, 4),
            "volatility": round(momentum_state.volatility, 4),
            "regime": momentum_state.regime.value,
            "regime_confidence": round(momentum_state.regime_confidence, 2),
            "regime_duration_s": round(momentum_state.regime_duration_s, 1),
        }

    return Metrics(
        timestamp_ms=now_ms,
        best_bid=ob.best_bid,
        best_ask=ob.best_ask,
        spread=ob.spread,
        midpoint=ob.midpoint,
        vwap_mid=vwap_mid,
        obi=obi,
        total_bid_depth=ob.total_bid_depth,
        total_ask_depth=ob.total_ask_depth,
        flow_pressure=flow["pressure"],
        buy_volume=flow["buy_volume"],
        sell_volume=flow["sell_volume"],
        walls=walls,
        whale_events=whales,
        spoof_signals=spoof_signals,
        absorption_events=absorption_events,
        sweep_events=sweep_events,
        sentiment=sentiment,
        # Phase 3: Momentum
        **momentum_fields,
        # Phase 4: Intelligence Dashboard
        liquidity_voids=voids,
        obi_velocity_5s=obi_vel.get("velocity_5s", 0.0),
        obi_velocity_30s=obi_vel.get("velocity_30s", 0.0),
        obi_action=obi_vel.get("action", "STABLE"),
        **cvd_data,
    )


# ──────────────────────────────────────────────────────────────
# INDIVIDUAL METRICS
# ──────────────────────────────────────────────────────────────

def compute_imbalance(ob: OrderBook) -> Optional[float]:
    """
    Order Book Imbalance (OBI).

    Formula:
        OBI = Σ(top N bid sizes) / (Σ(top N bid sizes) + Σ(top N ask sizes))

    Range: 0.0 (all asks, no bids) → 1.0 (all bids, no asks)
    Neutral: 0.5

    WHY IT MATTERS:
    When OBI > 0.6, there's significantly more buying interest than selling.
    This often precedes upward price movement because sellers face a "wall"
    of buy orders they must eat through.

    IMPORTANT:
    We use only the TOP N levels (not the entire book) because deep
    levels are less meaningful — they're far from the action and may
    be stale or strategic placement.
    """
    bids = ob.get_sorted_bids(max_levels=settings.OB_IMBALANCE_LEVELS)
    asks = ob.get_sorted_asks(max_levels=settings.OB_IMBALANCE_LEVELS)

    bid_total = sum(level.size for level in bids)
    ask_total = sum(level.size for level in asks)
    total = bid_total + ask_total

    if total == 0:
        return None  # Empty book

    return round(bid_total / total, 4)


def compute_vwap_mid(ob: OrderBook) -> Optional[float]:
    """
    Volume-Weighted Average Price midpoint.

    Formula:
        VWAP_mid = (best_bid × best_ask_size + best_ask × best_bid_size)
                   / (best_bid_size + best_ask_size)

    WHY IT MATTERS:
    Simple midpoint (bid+ask)/2 assumes equal liquidity on both sides.
    VWAP mid gives more weight to the side with LESS liquidity,
    which better reflects where the "true" price sits.

    Example: If best bid is 0.48 with 1000 contracts, and best ask
    is 0.52 with only 100 contracts — the VWAP mid will be closer
    to 0.52 because it would be easier to push price up (thin asks).
    """
    if ob.best_bid is None or ob.best_ask is None:
        return None

    bids = ob.get_sorted_bids(max_levels=1)
    asks = ob.get_sorted_asks(max_levels=1)

    if not bids or not asks:
        return None

    bid_size = bids[0].size
    ask_size = asks[0].size
    total_size = bid_size + ask_size

    if total_size == 0:
        return ob.midpoint

    vwap = (ob.best_bid * ask_size + ob.best_ask * bid_size) / total_size
    return round(vwap, 4)


def compute_flow_pressure(ob: OrderBook) -> dict:
    """
    Order Flow Pressure — are buyers or sellers more aggressive?

    Formula:
        pressure = (buy_volume - sell_volume) / (buy_volume + sell_volume)

    Range: -1.0 (pure selling) → +1.0 (pure buying)

    HOW IT WORKS:
    We look at all trades in a rolling time window. If trade.side == BUY,
    that means a buyer aggressively "lifted the ask" (took liquidity).
    If trade.side == SELL, a seller "hit the bid".

    WHY IT MATTERS:
    Aggressive flow tells you who's in a hurry. Passive limit orders
    can be cancelled; aggressive market orders are committed capital.
    Sustained buy flow pressure often leads to price increases.
    """
    trades = ob.get_trades_in_window(settings.FLOW_WINDOW_SECONDS)

    buy_volume = 0.0
    sell_volume = 0.0

    for trade in trades:
        volume = trade.price * trade.size  # Dollar volume
        if trade.side == Side.BUY:
            buy_volume += volume
        else:
            sell_volume += volume

    total = buy_volume + sell_volume
    if total == 0:
        pressure = 0.0
    else:
        pressure = round((buy_volume - sell_volume) / total, 4)

    return {
        "pressure": pressure,
        "buy_volume": round(buy_volume, 2),
        "sell_volume": round(sell_volume, 2),
    }


def detect_walls(ob: OrderBook) -> list[WallInfo]:
    """
    Detect liquidity walls — abnormally large resting orders.

    A "wall" is a price level where the size is significantly larger
    than the average level. We use standard deviation to define "significant".

    Formula:
        wall threshold = mean(all sizes) + K × std(all sizes)
        K = WALL_STD_MULTIPLIER (default 2.0)

    WHY IT MATTERS:
    - A bid wall (support) absorbs sell pressure → price floor
    - An ask wall (resistance) absorbs buy pressure → price ceiling
    - Walls can be real (institutional orders) or fake (spoofing)
    - Watching whether a wall holds or breaks is a key trading signal
    """
    walls = []

    # Collect all level sizes for statistics
    all_sizes = []
    for bids in ob.get_sorted_bids(max_levels=50):
        all_sizes.append(bids.size)
    for asks in ob.get_sorted_asks(max_levels=50):
        all_sizes.append(asks.size)

    if len(all_sizes) < 3:
        return walls  # Not enough data

    mean_size = float(np.mean(all_sizes))
    std_size = float(np.std(all_sizes))

    if std_size == 0:
        return walls  # All levels same size

    threshold = mean_size + settings.WALL_STD_MULTIPLIER * std_size

    # Check bids for support walls
    for level in ob.get_sorted_bids(max_levels=20):
        if level.size >= threshold:
            strength = (level.size - mean_size) / std_size
            walls.append(WallInfo(
                price=level.price,
                size=level.size,
                side=Side.BUY,
                strength=round(strength, 2),
            ))

    # Check asks for resistance walls
    for level in ob.get_sorted_asks(max_levels=20):
        if level.size >= threshold:
            strength = (level.size - mean_size) / std_size
            walls.append(WallInfo(
                price=level.price,
                size=level.size,
                side=Side.SELL,
                strength=round(strength, 2),
            ))

    return walls


def detect_whale_trades(ob: OrderBook) -> list[WhaleEvent]:
    """
    Detect whale trades — abnormally large recent trades.

    A "whale" trade exceeds WHALE_THRESHOLD_SIZE in dollar value.

    WHY IT MATTERS:
    Large trades signal informed participants. A whale buying aggressively
    (lifting asks) often means they have information or conviction that
    price will go up. Whale activity is one of the strongest signals
    for short-term direction.
    """
    whales = []
    # Look at trades in the last 60 seconds
    trades = ob.get_trades_in_window(60.0)

    for trade in trades:
        dollar_value = trade.price * trade.size
        if dollar_value >= settings.WHALE_THRESHOLD_SIZE:
            whales.append(WhaleEvent(
                price=trade.price,
                size=trade.size,
                side=trade.side,
                is_taker=True,  # Trades are always taker events
                timestamp_ms=trade.timestamp_ms,
            ))

    return whales


def compute_sentiment_v3(
    obi: Optional[float],
    flow_pressure: float,
    ob: OrderBook,
    momentum_state: "MomentumState | None" = None,
    absorption_events: list = None,
    sweep_events: list = None,
) -> float:
    """
    Phase 3 Enhanced Composite Sentiment Score.

    Combines Phase 1 basics + Phase 2 detections + Phase 3 momentum into
    a single -1.0 to +1.0 score.

    IMPROVEMENTS OVER PHASE 1:
    - Uses EMA-smoothed momentum instead of raw snapshot-to-snapshot diffs
    - Incorporates regime context (trending markets get momentum boost)
    - Factors in absorption signals (strong conviction indicator)
    - Factors in sweep signals (urgency indicator)
    - Volatility acts as a confidence dampener (high vol = less certain)

    The score is an OPINION, not a prediction. It summarizes the current
    microstructure state into a single number for quick assessment.
    """
    from analytics.momentum import MarketRegime

    absorption_events = absorption_events or []
    sweep_events = sweep_events or []
    weights = settings.SENTIMENT_WEIGHTS
    score = 0.0

    # ── PHASE 1 SIGNALS (unchanged) ──

    # OBI signal: convert 0-1 range to -1 to +1
    if obi is not None:
        obi_signal = (obi - 0.5) * 2
        score += weights["obi"] * obi_signal

    # Flow pressure: already -1 to +1
    score += weights["flow_pressure"] * flow_pressure

    # Depth momentum from snapshots
    history = ob.get_snapshot_history()
    if len(history) >= 2:
        prev = history[-2]
        curr_bid = ob.total_bid_depth
        curr_ask = ob.total_ask_depth

        if prev["total_bid_depth"] > 0:
            bid_mom = (curr_bid - prev["total_bid_depth"]) / prev["total_bid_depth"]
            score += weights["bid_depth_momentum"] * max(min(bid_mom, 1), -1)

        if prev["total_ask_depth"] > 0:
            ask_mom = (curr_ask - prev["total_ask_depth"]) / prev["total_ask_depth"]
            score -= weights["ask_depth_momentum"] * max(min(ask_mom, 1), -1)

    # Spread signal
    if ob.spread is not None and ob.midpoint is not None and ob.midpoint > 0:
        rel_spread = ob.spread / ob.midpoint
        if rel_spread < settings.SPREAD_TIGHT_THRESHOLD:
            score += weights["spread_signal"] * 0.3
        elif rel_spread > settings.SPREAD_WIDE_THRESHOLD:
            score -= weights["spread_signal"] * 0.3

    # ── PHASE 3 SIGNALS (new) ──

    if momentum_state is not None and momentum_state.is_ready:
        # Price momentum: trend strength directly contributes
        score += settings.SENTIMENT_MOMENTUM_WEIGHT * momentum_state.price_trend_strength

        # Regime alignment bonus: if regime confirms the direction, boost confidence
        regime = momentum_state.regime
        regime_signal = 0.0
        if regime == MarketRegime.TRENDING_UP:
            regime_signal = 0.5 * momentum_state.regime_confidence
        elif regime == MarketRegime.TRENDING_DOWN:
            regime_signal = -0.5 * momentum_state.regime_confidence
        elif regime == MarketRegime.BREAKOUT:
            # Breakout direction from price velocity
            regime_signal = 0.7 if momentum_state.price_velocity > 0 else -0.7
            regime_signal *= momentum_state.regime_confidence
        score += settings.SENTIMENT_REGIME_WEIGHT * regime_signal

        # Volatility dampener: high volatility reduces sentiment certainty
        vol_dampener = 1.0 - (momentum_state.volatility * settings.SENTIMENT_VOLATILITY_WEIGHT * 5)
        vol_dampener = max(vol_dampener, 0.5)  # Don't reduce more than 50%
        score *= vol_dampener

    # ── PHASE 2 DETECTION SIGNALS ──

    # Absorption: strong conviction signal
    for absorption in absorption_events:
        if absorption.side == Side.BUY:
            score += settings.SENTIMENT_DETECTION_WEIGHT * 0.3  # Bullish: bid wall absorbing
        else:
            score -= settings.SENTIMENT_DETECTION_WEIGHT * 0.3  # Bearish: ask wall absorbing

    # Sweeps: urgency signal
    for sweep in sweep_events:
        if sweep.side == Side.BUY:
            score += settings.SENTIMENT_DETECTION_WEIGHT * 0.5  # Strong bullish: buy sweep
        else:
            score -= settings.SENTIMENT_DETECTION_WEIGHT * 0.5  # Strong bearish: sell sweep

    # Clamp to [-1, 1]
    return round(max(min(score, 1.0), -1.0), 4)


# Keep old function as alias for backward compatibility
def compute_sentiment(obi, flow_pressure, ob):
    return compute_sentiment_v3(obi, flow_pressure, ob)


# ──────────────────────────────────────────────────────────────
# PHASE 4: INTELLIGENCE DASHBOARD METRICS
# ──────────────────────────────────────────────────────────────

def detect_liquidity_voids(ob: OrderBook) -> list[LiquidityVoid]:
    """
    Detect 'Flash Zones' — levels where liquidity is abnormally thin.

    A liquidity void exists when a level's depth is less than
    LIQUIDITY_VOID_THRESHOLD (default 10%) of the 10-level average.
    Price can teleport through these zones because there's nothing
    to absorb momentum.

    Returns:
        List of LiquidityVoid objects, sorted by price.
    """
    voids = []

    for side, levels in [
        (Side.BUY, ob.get_sorted_bids(max_levels=10)),
        (Side.SELL, ob.get_sorted_asks(max_levels=10)),
    ]:
        if len(levels) < 3:
            continue

        sizes = [level.size for level in levels]
        avg_depth = sum(sizes) / len(sizes) if sizes else 0.0

        if avg_depth <= 0:
            continue

        threshold = avg_depth * settings.LIQUIDITY_VOID_THRESHOLD

        for level in levels:
            if level.size < threshold and level.size > 0:
                voids.append(LiquidityVoid(
                    price=level.price,
                    side=side,
                    depth=level.size,
                    avg_depth=round(avg_depth, 2),
                    void_ratio=round(level.size / avg_depth, 4),
                ))

    return voids


def compute_obi_velocity(momentum_engine: "MomentumEngine") -> dict:
    """
    Compute 5-second and 30-second OBI rate of change.

    Uses the momentum engine's OBI EMA tracker to compute
    windowed linear slopes, then classifies the action as
    STACKING (book building up), PULLING (book thinning), or STABLE.

    STATUS (B17): computed on every tick but consumed by no signal,
    insight, or UI yet — kept deliberately as a ready-made ingredient
    for new-strategy development. Wire it or delete it consciously.

    Returns:
        dict with velocity_5s, velocity_30s, and action label.
    """
    state = momentum_engine.get_state()
    if state is None or not state.is_ready:
        return {"velocity_5s": 0.0, "velocity_30s": 0.0, "action": "STABLE"}

    # Use the OBI velocity from momentum state as the 5s proxy
    vel_5s = round(state.obi_velocity, 6)
    # Use OBI trend strength as 30s proxy (already smoothed over slow EMA)
    vel_30s = round(state.obi_trend_strength, 4)

    # Classify action
    if vel_5s > settings.OBI_VELOCITY_STACKING_THRESHOLD:
        action = "STACKING"
    elif vel_5s < settings.OBI_VELOCITY_PULLING_THRESHOLD:
        action = "PULLING"
    else:
        action = "STABLE"

    return {
        "velocity_5s": vel_5s,
        "velocity_30s": vel_30s,
        "action": action,
    }
