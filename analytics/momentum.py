"""
momentum.py — Momentum tracking and trend detection engine.

THIS IS THE PHASE 3 CORE — TIME-SERIES INTELLIGENCE.

Phase 1-2 computed metrics from the CURRENT state only.
Phase 3 tracks how metrics CHANGE OVER TIME to detect:

1. PRICE MOMENTUM: Is the midpoint trending up/down? Accelerating?
2. OBI MOMENTUM: Is imbalance steadily shifting one direction?
3. DEPTH MOMENTUM: Is liquidity growing or shrinking on each side?
4. FLOW MOMENTUM: Is aggressive flow building or fading?
5. VOLATILITY: How unstable is the spread and price?

KEY CONCEPT — EXPONENTIAL MOVING AVERAGE (EMA):
We use EMAs instead of simple averages because EMAs weight
recent data more heavily. In fast markets, what happened 5 seconds
ago matters far more than what happened 60 seconds ago.

EMA formula:  EMA_new = α × value + (1 - α) × EMA_old
Where α (alpha) = 2 / (period + 1)

REGIME DETECTION:
Based on momentum signals, we classify the market into one of:
- TRENDING_UP:  sustained price + OBI upward movement
- TRENDING_DOWN: sustained price + OBI downward movement
- RANGING:      price oscillating, no clear direction
- VOLATILE:     wide swings, spread expanding
- BREAKOUT:     sudden shift from ranging to trending
- QUIET:        low activity, tight spread, minimal changes

BEGINNER NOTE:
Think of this module as the "memory" of the analytics engine.
While metrics.py gives you a snapshot ("imbalance is 65% right now"),
momentum.py tells you the story ("imbalance has been climbing for the
last 30 seconds and is accelerating").
"""

import time
import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from config import settings


# ──────────────────────────────────────────────────────────────
# MARKET REGIME CLASSIFICATION
# ──────────────────────────────────────────────────────────────

class MarketRegime(str, Enum):
    """
    Current market regime — the overall behavioral state.

    Each regime suggests different trading approaches:
    - TRENDING_UP:    favor buying dips, ride momentum
    - TRENDING_DOWN:  favor selling rallies, avoid catching knives
    - RANGING:        fade extremes, sell resistance, buy support
    - VOLATILE:       widen stops, reduce size, be cautious
    - BREAKOUT:       follow the break direction with conviction
    - QUIET:          low opportunity, wait for a catalyst
    """
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    BREAKOUT = "BREAKOUT"
    QUIET = "QUIET"


# ──────────────────────────────────────────────────────────────
# EMA TRACKER — Single metric with smoothing
# ──────────────────────────────────────────────────────────────

@dataclass
class EMATracker:
    """
    Tracks a single metric over time with EMA smoothing.

    Provides:
    - Current smoothed value (EMA)
    - Rate of change (first derivative: is it going up or down?)
    - Acceleration (second derivative: is the change speeding up?)
    - Recent min/max for range detection

    Usage:
        tracker = EMATracker(period=20)
        tracker.update(0.55)
        tracker.update(0.57)
        print(tracker.ema)        # Smoothed value
        print(tracker.velocity)   # Rate of change
        print(tracker.accel)      # Acceleration
    """
    period: int = 20                 # EMA period (number of samples)
    _alpha: float = 0.0              # Computed from period
    ema: float = 0.0                 # Current EMA value
    _prev_ema: float = 0.0           # Previous EMA (for velocity)
    _prev_velocity: float = 0.0      # Previous velocity (for acceleration)
    velocity: float = 0.0            # First derivative (rate of change)
    accel: float = 0.0               # Second derivative (acceleration)
    raw: float = 0.0                 # Latest raw (unsmoothed) value
    count: int = 0                   # Total updates received
    _recent: deque = field(default_factory=lambda: deque(maxlen=60))  # Raw values for min/max

    def __post_init__(self):
        self._alpha = 2.0 / (self.period + 1)

    def update(self, value: float):
        """
        Feed a new value into the tracker.

        On the first call, EMA is set to the value directly.
        After that, EMA is updated incrementally.
        """
        self.raw = value
        self._recent.append(value)

        if self.count == 0:
            # First value — initialize EMA directly
            self.ema = value
            self._prev_ema = value
        else:
            self._prev_ema = self.ema
            self._prev_velocity = self.velocity

            # EMA update: α × new_value + (1 - α) × old_ema
            self.ema = self._alpha * value + (1 - self._alpha) * self.ema

            # Velocity: how fast the EMA is changing
            self.velocity = self.ema - self._prev_ema

            # Acceleration: is the velocity itself changing?
            self.accel = self.velocity - self._prev_velocity

        self.count += 1

    @property
    def is_initialized(self) -> bool:
        """Need at least a few samples for meaningful EMA."""
        return self.count >= 3

    @property
    def recent_min(self) -> float:
        """Minimum value in the recent window."""
        return min(self._recent) if self._recent else 0.0

    @property
    def recent_max(self) -> float:
        """Maximum value in the recent window."""
        return max(self._recent) if self._recent else 0.0

    @property
    def recent_range(self) -> float:
        """Range of values in the recent window (max - min)."""
        return self.recent_max - self.recent_min

    @property
    def trend_strength(self) -> float:
        """
        How strong and consistent is the trend?

        Combines velocity magnitude with consistency (velocity and
        acceleration in same direction = strong trend).

        Range: -1.0 (strong down) to +1.0 (strong up)
        """
        if not self.is_initialized:
            return 0.0

        # Base trend from velocity direction
        strength = self.velocity * 100  # Scale up (velocities are small)

        # Boost if acceleration confirms the direction
        if self.velocity > 0 and self.accel > 0:
            strength *= 1.3  # Accelerating uptrend
        elif self.velocity < 0 and self.accel < 0:
            strength *= 1.3  # Accelerating downtrend
        elif self.velocity > 0 and self.accel < 0:
            strength *= 0.7  # Decelerating uptrend (weakening)
        elif self.velocity < 0 and self.accel > 0:
            strength *= 0.7  # Decelerating downtrend (weakening)

        return max(min(strength, 1.0), -1.0)


# ──────────────────────────────────────────────────────────────
# VOLATILITY TRACKER
# ──────────────────────────────────────────────────────────────

@dataclass
class VolatilityTracker:
    """
    Tracks market volatility using multiple methods.

    Methods:
    1. Spread volatility: standard deviation of recent spreads
    2. Price volatility: std dev of midpoint changes
    3. Trade price variance: how much do trade prices scatter?

    Combined into a single 0.0 → 1.0 volatility score.
    """
    _spreads: deque = field(default_factory=lambda: deque(maxlen=120))
    _midpoints: deque = field(default_factory=lambda: deque(maxlen=120))
    _trade_prices: deque = field(default_factory=lambda: deque(maxlen=200))

    def record_spread(self, spread: float):
        self._spreads.append(spread)

    def record_midpoint(self, midpoint: float):
        self._midpoints.append(midpoint)

    def record_trade_price(self, price: float):
        self._trade_prices.append(price)

    @property
    def spread_volatility(self) -> float:
        """Standard deviation of recent spreads."""
        if len(self._spreads) < 5:
            return 0.0
        values = list(self._spreads)
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        return math.sqrt(variance)

    @property
    def price_volatility(self) -> float:
        """Standard deviation of midpoint returns (changes)."""
        if len(self._midpoints) < 5:
            return 0.0
        mids = list(self._midpoints)
        returns = [mids[i] - mids[i - 1] for i in range(1, len(mids))]
        if not returns:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return math.sqrt(variance)

    @property
    def trade_price_volatility(self) -> float:
        """Standard deviation of recent trade prices."""
        if len(self._trade_prices) < 5:
            return 0.0
        values = list(self._trade_prices)
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        return math.sqrt(variance)

    @property
    def composite_volatility(self) -> float:
        """
        Combined volatility score, normalized to 0.0 - 1.0.

        Uses a sigmoid-like normalization so the score approaches 1.0
        for very high volatility but stays proportional for normal levels.
        """
        raw = (
            self.spread_volatility * 10 +
            self.price_volatility * 50 +
            self.trade_price_volatility * 20
        )
        # Sigmoid normalization: maps 0→inf to 0→1
        return raw / (raw + 1.0) if raw > 0 else 0.0


# ──────────────────────────────────────────────────────────────
# MOMENTUM ENGINE — The full tracking system
# ──────────────────────────────────────────────────────────────

@dataclass
class MomentumState:
    """
    Snapshot of all momentum indicators at a point in time.
    This is what gets fed to the interpreter for insights.
    """
    # Price momentum
    price_ema: float = 0.0
    price_velocity: float = 0.0
    price_accel: float = 0.0
    price_trend_strength: float = 0.0

    # OBI momentum
    obi_ema: float = 0.5
    obi_velocity: float = 0.0
    obi_trend_strength: float = 0.0

    # Depth momentum
    bid_depth_ema: float = 0.0
    bid_depth_velocity: float = 0.0
    ask_depth_ema: float = 0.0
    ask_depth_velocity: float = 0.0
    depth_divergence: float = 0.0   # bid growing + ask shrinking = positive

    # Flow momentum
    flow_ema: float = 0.0
    flow_velocity: float = 0.0
    flow_trend_strength: float = 0.0

    # Spread momentum
    spread_ema: float = 0.0
    spread_velocity: float = 0.0

    # Volatility
    volatility: float = 0.0
    spread_volatility: float = 0.0
    price_volatility: float = 0.0

    # Regime
    regime: MarketRegime = MarketRegime.QUIET
    regime_confidence: float = 0.0   # 0.0 to 1.0
    regime_duration_s: float = 0.0   # How long we've been in this regime

    # Whether we have enough data for meaningful signals
    is_ready: bool = False


class MomentumEngine:
    """
    Tracks all momentum indicators and detects market regimes.

    Usage:
        engine = MomentumEngine()

        # Call on every metrics update:
        engine.update(
            midpoint=0.55,
            obi=0.62,
            spread=0.03,
            flow_pressure=0.25,
            bid_depth=15000,
            ask_depth=12000,
            trade_price=0.54,  # Optional, from trade events
        )

        state = engine.get_state()
        print(state.regime)           # MarketRegime.TRENDING_UP
        print(state.price_velocity)   # 0.003 (price moving up)
    """

    def __init__(self):
        # EMA trackers for each metric (fast and slow periods)
        self._price = EMATracker(period=settings.MOMENTUM_FAST_PERIOD)
        self._price_slow = EMATracker(period=settings.MOMENTUM_SLOW_PERIOD)
        self._obi = EMATracker(period=settings.MOMENTUM_FAST_PERIOD)
        self._flow = EMATracker(period=settings.MOMENTUM_FAST_PERIOD)
        self._spread = EMATracker(period=settings.MOMENTUM_FAST_PERIOD)
        self._bid_depth = EMATracker(period=settings.MOMENTUM_FAST_PERIOD)
        self._ask_depth = EMATracker(period=settings.MOMENTUM_FAST_PERIOD)

        # Volatility tracker
        self._volatility = VolatilityTracker()

        # Regime tracking
        self._current_regime = MarketRegime.QUIET
        self._regime_start_time = time.time()
        self._prev_regime = MarketRegime.QUIET

        # Update counter
        self._update_count = 0

    def update(
        self,
        midpoint: Optional[float] = None,
        obi: Optional[float] = None,
        spread: Optional[float] = None,
        flow_pressure: Optional[float] = None,
        bid_depth: Optional[float] = None,
        ask_depth: Optional[float] = None,
        trade_price: Optional[float] = None,
    ):
        """
        Feed new metric values into the momentum engine.

        Call this after every metrics computation.
        Pass None for metrics that aren't available this tick.
        """
        if midpoint is not None:
            self._price.update(midpoint)
            self._price_slow.update(midpoint)
            self._volatility.record_midpoint(midpoint)

        if obi is not None:
            self._obi.update(obi)

        if spread is not None:
            self._spread.update(spread)
            self._volatility.record_spread(spread)

        if flow_pressure is not None:
            self._flow.update(flow_pressure)

        if bid_depth is not None:
            self._bid_depth.update(bid_depth)

        if ask_depth is not None:
            self._ask_depth.update(ask_depth)

        if trade_price is not None:
            self._volatility.record_trade_price(trade_price)

        self._update_count += 1

        # Update regime detection (only after enough data)
        if self._update_count >= settings.MOMENTUM_MIN_SAMPLES:
            self._detect_regime()

    def get_state(self) -> MomentumState:
        """Get a snapshot of all momentum indicators."""
        # Depth divergence: positive when bids growing + asks shrinking (bullish)
        depth_div = 0.0
        if self._bid_depth.is_initialized and self._ask_depth.is_initialized:
            depth_div = self._bid_depth.velocity - self._ask_depth.velocity

        return MomentumState(
            # Price
            price_ema=self._price.ema,
            price_velocity=self._price.velocity,
            price_accel=self._price.accel,
            price_trend_strength=self._price.trend_strength,
            # OBI
            obi_ema=self._obi.ema,
            obi_velocity=self._obi.velocity,
            obi_trend_strength=self._obi.trend_strength,
            # Depth
            bid_depth_ema=self._bid_depth.ema,
            bid_depth_velocity=self._bid_depth.velocity,
            ask_depth_ema=self._ask_depth.ema,
            ask_depth_velocity=self._ask_depth.velocity,
            depth_divergence=depth_div,
            # Flow
            flow_ema=self._flow.ema,
            flow_velocity=self._flow.velocity,
            flow_trend_strength=self._flow.trend_strength,
            # Spread
            spread_ema=self._spread.ema,
            spread_velocity=self._spread.velocity,
            # Volatility
            volatility=self._volatility.composite_volatility,
            spread_volatility=self._volatility.spread_volatility,
            price_volatility=self._volatility.price_volatility,
            # Regime
            regime=self._current_regime,
            regime_confidence=self._compute_regime_confidence(),
            regime_duration_s=time.time() - self._regime_start_time,
            # Readiness
            is_ready=self._update_count >= settings.MOMENTUM_MIN_SAMPLES,
        )

    # ──────────────────────────────────────────────────────────
    # REGIME DETECTION
    # ──────────────────────────────────────────────────────────

    def _detect_regime(self):
        """
        Classify the current market regime.

        Uses a scoring system across multiple momentum indicators.
        The regime with the highest score wins.

        LOGIC:
        - TRENDING_UP: price EMA > slow EMA, positive OBI velocity, buy flow
        - TRENDING_DOWN: price EMA < slow EMA, negative OBI velocity, sell flow
        - RANGING: price oscillating around slow EMA, no clear OBI direction
        - VOLATILE: high spread/price volatility, rapid changes
        - BREAKOUT: sudden regime change from QUIET/RANGING
        - QUIET: low volatility, tight spread, small changes
        """
        scores = {r: 0.0 for r in MarketRegime}

        # Price trend signals
        price_above_slow = self._price.ema > self._price_slow.ema
        price_vel = self._price.velocity
        price_strength = self._price.trend_strength

        # OBI signals
        obi_strength = self._obi.trend_strength

        # Flow signals
        flow_val = self._flow.ema
        flow_vel = self._flow.velocity

        # Volatility
        vol = self._volatility.composite_volatility

        # Spread
        spread_vel = self._spread.velocity

        # ── TRENDING UP scoring ──
        if price_above_slow and price_vel > 0:
            scores[MarketRegime.TRENDING_UP] += 2.0
        if price_strength > 0.3:
            scores[MarketRegime.TRENDING_UP] += price_strength * 2
        if obi_strength > 0.2:
            scores[MarketRegime.TRENDING_UP] += obi_strength
        if flow_val > 0.2:
            scores[MarketRegime.TRENDING_UP] += flow_val
        if self._bid_depth.velocity > 0 and self._ask_depth.velocity < 0:
            scores[MarketRegime.TRENDING_UP] += 1.0  # Depth divergence (bullish)

        # ── TRENDING DOWN scoring ──
        if not price_above_slow and price_vel < 0:
            scores[MarketRegime.TRENDING_DOWN] += 2.0
        if price_strength < -0.3:
            scores[MarketRegime.TRENDING_DOWN] += abs(price_strength) * 2
        if obi_strength < -0.2:
            scores[MarketRegime.TRENDING_DOWN] += abs(obi_strength)
        if flow_val < -0.2:
            scores[MarketRegime.TRENDING_DOWN] += abs(flow_val)
        if self._bid_depth.velocity < 0 and self._ask_depth.velocity > 0:
            scores[MarketRegime.TRENDING_DOWN] += 1.0

        # ── VOLATILE scoring ──
        if vol > settings.REGIME_VOLATILITY_THRESHOLD:
            scores[MarketRegime.VOLATILE] += vol * 5
        if abs(spread_vel) > 0.001:
            scores[MarketRegime.VOLATILE] += abs(spread_vel) * 100
        if abs(price_vel) > 0.005:
            scores[MarketRegime.VOLATILE] += 1.0

        # ── RANGING scoring ──
        price_range = self._price.recent_range
        if price_range > 0 and price_range < 0.05:
            scores[MarketRegime.RANGING] += 2.0
        if abs(price_strength) < 0.2:
            scores[MarketRegime.RANGING] += 1.0
        if abs(obi_strength) < 0.15:
            scores[MarketRegime.RANGING] += 0.5
        if vol < settings.REGIME_VOLATILITY_THRESHOLD:
            scores[MarketRegime.RANGING] += 0.5

        # ── QUIET scoring ──
        if vol < 0.1:
            scores[MarketRegime.QUIET] += 2.0
        if abs(price_vel) < 0.0005:
            scores[MarketRegime.QUIET] += 1.5
        if abs(flow_val) < 0.1:
            scores[MarketRegime.QUIET] += 1.0
        if abs(spread_vel) < 0.0001:
            scores[MarketRegime.QUIET] += 0.5

        # ── BREAKOUT scoring ──
        # Breakout = sudden shift FROM quiet/ranging
        if self._current_regime in (MarketRegime.QUIET, MarketRegime.RANGING):
            if abs(price_strength) > 0.5:
                scores[MarketRegime.BREAKOUT] += abs(price_strength) * 3
            if abs(flow_vel) > 0.05:
                scores[MarketRegime.BREAKOUT] += abs(flow_vel) * 10
            if vol > 0.3 and self._volatility.composite_volatility > 0.3:
                scores[MarketRegime.BREAKOUT] += 2.0

        # Pick the regime with the highest score
        new_regime = max(scores, key=scores.get)

        # Hysteresis: require a minimum score advantage to switch regimes
        # This prevents rapid flipping between states
        current_score = scores[self._current_regime]
        new_score = scores[new_regime]

        if new_regime != self._current_regime:
            advantage = new_score - current_score
            if advantage > settings.REGIME_SWITCH_THRESHOLD:
                self._prev_regime = self._current_regime
                self._current_regime = new_regime
                self._regime_start_time = time.time()

    def _compute_regime_confidence(self) -> float:
        """
        How confident are we in the current regime classification?

        Based on: consistency of signals, duration in regime, score margin.
        """
        if not self._price.is_initialized:
            return 0.0

        confidence = 0.0

        # Duration boost: the longer we're in a regime, the more confident
        duration = time.time() - self._regime_start_time
        confidence += min(duration / 30.0, 0.3)  # Cap at 0.3 from duration

        # Signal alignment
        regime = self._current_regime
        if regime == MarketRegime.TRENDING_UP:
            if self._price.velocity > 0:
                confidence += 0.2
            if self._obi.ema > 0.55:
                confidence += 0.2
            if self._flow.ema > 0:
                confidence += 0.15
            if self._bid_depth.velocity > 0:
                confidence += 0.15

        elif regime == MarketRegime.TRENDING_DOWN:
            if self._price.velocity < 0:
                confidence += 0.2
            if self._obi.ema < 0.45:
                confidence += 0.2
            if self._flow.ema < 0:
                confidence += 0.15
            if self._ask_depth.velocity > 0:
                confidence += 0.15

        elif regime == MarketRegime.VOLATILE:
            vol = self._volatility.composite_volatility
            confidence += min(vol, 0.5)

        elif regime == MarketRegime.QUIET:
            if abs(self._price.velocity) < 0.001:
                confidence += 0.3
            if self._volatility.composite_volatility < 0.1:
                confidence += 0.3

        elif regime == MarketRegime.BREAKOUT:
            confidence += min(abs(self._price.trend_strength), 0.5)
            if duration < 10:
                confidence += 0.2  # Fresh breakouts are more confident

        else:  # RANGING
            confidence += 0.3 if abs(self._price.trend_strength) < 0.2 else 0.1

        return min(confidence, 1.0)
