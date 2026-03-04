"""
pair_strategy.py — Pair Trading Strategy for BTC 5-Minute Markets

PARADIGM: We do NOT predict direction. We accumulate matched pairs
(1 YES + 1 NO) at a combined cost < $1.00 for guaranteed profit
at settlement. One side always pays $1.00, the other $0.00.

THE 6 IRONCLAD RULES:
1. Inventory Lock — Never buy the same side twice without balancing
2. Anti-Falling Knife — Never open first leg below $0.15
3. Respect Friction — No taker orders in $0.45-$0.55 dead zone
4. Signals as Filters — OBI/flow delay buys, don't trigger them
5. Pair IS the risk management — No stop losses, complete the pair
6. Settlement Simulation — Hold to resolution, simulate $1/$0 payout

BLOCKCHAIN CLOB EXECUTION MODEL (Polymarket on Polygon):
1. LIMIT-OR-FAIL: No random slippage. Fill at book price or revert.
2. BOOK WALKING (VWAP): Sweep L1→L2→L3. VWAP fill + deterministic fee.
3. TIME-IN-BOOK: Fill prob based on ask age, not token price.
   <200ms=0%, 200-500ms=50%, >500ms=100%.
4. MAKER ORDERS: Dead zone uses 0-fee maker bids with queue sim.
5. POLYGON BLOCK TIME: 2s cooldown between buys.
"""

import time
import random
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# POLYMARKET DYNAMIC FEE (on-chain, deterministic)
# ──────────────────────────────────────────────────────────

POLYMARKET_FEE_RATE = 0.0625


def polymarket_taker_fee(price: float) -> float:
    """Dynamic taker fee: highest at 50¢ (1.56%), lowest at extremes."""
    p = max(0.01, min(0.99, price))
    return p * (1 - p) * POLYMARKET_FEE_RATE


# ──────────────────────────────────────────────────────────
# VWAP BOOK WALKING (Spec Rule 2)
# ──────────────────────────────────────────────────────────

def vwap_fill(ask_levels: list, desired_qty: float, max_levels: int = 3
              ) -> Tuple[float, float, float, int]:
    """
    Walk the order book to fill an order, exactly like Polymarket's
    matching engine does on-chain.

    Args:
        ask_levels: List of OrderLevel objects, sorted cheapest first.
        desired_qty: How many shares we want to buy.
        max_levels: Max depth levels to walk (L1, L2, L3).

    Returns:
        (vwap_price, filled_qty, total_cost_before_fee, levels_walked)

    If there's not enough depth across all levels, we get a partial fill.
    The remaining unfilled portion is cancelled (limit-or-fail).
    """
    filled_qty = 0.0
    total_cost = 0.0
    levels_walked = 0

    for level in ask_levels:
        if filled_qty >= desired_qty:
            break
        if levels_walked >= max_levels:
            break

        price = level.price if hasattr(level, 'price') else level[0]
        size = level.size if hasattr(level, 'size') else level[1]

        if size <= 0:
            continue

        remaining = desired_qty - filled_qty
        take_from_level = min(remaining, size)

        filled_qty += take_from_level
        total_cost += take_from_level * price
        levels_walked += 1

    if filled_qty <= 0:
        return 0.0, 0.0, 0.0, 0

    vwap = total_cost / filled_qty
    return vwap, filled_qty, total_cost, levels_walked


# ──────────────────────────────────────────────────────────
# TIME-IN-BOOK LATENCY MODEL (Spec Rule 3)
# ──────────────────────────────────────────────────────────

def latency_fill_probability(ask_age_ms: float) -> float:
    """
    Fill probability based on how long the best ask has been resting.
    Models home WiFi latency (~100-300ms) vs HFT bots on fast RPCs.

    < 200ms: 0% — HFTs beat us to the relayer. Reject.
    200-500ms: 50% — Race condition.
    > 500ms: 100% — Price is stable, our order lands.
    """
    if ask_age_ms < 200:
        return 0.0
    elif ask_age_ms < 500:
        return 0.50
    else:
        return 1.0


# ──────────────────────────────────────────────────────────
# DATA STRUCTURES
# ──────────────────────────────────────────────────────────

@dataclass
class PairLeg:
    """A single buy of one side (YES or NO)."""
    side: str
    qty: float
    raw_price: float        # Best ask at time of buy
    fill_price: float       # After VWAP + fee (taker) or exact bid (maker)
    cost: float             # fill_price * qty
    timestamp: float
    order_type: str = "TAKER"   # "TAKER" or "MAKER"
    ask_age_ms: float = 0.0     # Ask age when we hit it
    levels_walked: int = 1      # Book levels consumed
    vwap_price: float = 0.0     # Pre-fee VWAP


@dataclass
class MakerOrder:
    """A resting maker bid in the dead zone."""
    side: str
    bid_price: float
    qty: float
    placed_time: float
    depth_ahead: float      # Shares resting ahead of us in queue


@dataclass
class PairConfig:
    """All tunable pair trading parameters."""
    target_pair_cost: float = 0.96
    max_pair_cost: float = 0.99
    panic_pair_cost: float = 1.02       # Only used for non-completing buys

    buy_size_usd: float = 10.0
    max_position_usd: float = 100.0
    panic_max_position_usd: float = 116.0

    max_skew_pct: float = 0.50             # Gemini #3: 50% single-side skew cap
    min_first_leg_price: float = 0.15
    panic_hedge_pair_limit: float = 0.97  # Backtest-optimized: cap pair cost even on panic hedges

    # ── PRICE ZONES ──
    # Sniper Zone: ≤ $0.35 — ignore all signals, buy aggressively
    # Value Zone:  $0.36–dynamic — buy with signal filters
    # Dead Zone:   > dynamic — blocked (replaced by dynamic breakeven)
    sniper_threshold: float = 0.35       # At or below = sniper (no signal filter)
    value_zone_high: float = 0.43        # Static fallback; overridden by dynamic_dead_zone when Leg 1 exists

    # Signal filters (only apply in Value Zone)
    obi_delay_threshold: float = 0.75
    flow_delay_threshold: float = 0.6

    # Signal override: don't apply sniper override too close to expiry
    sniper_signal_min_time: float = 90.0  # Only override signals if >90s left

    panic_time_seconds: float = 10.0      # Backtest-optimized: was 30.0

    # ── THETA-BASED POSITION SIZING (Gemini #3) ──
    # Linear decay: 100% at 0-3min, 50% at 3-4.5min, 0% new opens at 4.5-5min
    theta_full_size_until_s: float = 180.0   # 3:00 — 100% size
    theta_half_size_until_s: float = 30.0    # last 30s — hedge only (0% new opens)
    # Note: between theta_full_size_until_s and theta_half_size_until_s = 50% size

    # ── ATOMIC ENTRY (Gemini #2) ──
    # Only open Leg 1 if opposite book liquidity allows pair cost < target
    atomic_entry_max_pair: float = 1.05  # max projected pair cost to allow Leg 1

    # ── BLOCKCHAIN CLOB EXECUTION ──
    min_buy_cooldown_s: float = 2.0       # Polygon block time
    max_book_walk_levels: int = 3          # Sweep L1→L2→L3 max


@dataclass
class WindowResult:
    """End-of-window settlement result."""
    yes_qty: float
    no_qty: float
    yes_avg_cost: float
    no_avg_cost: float
    total_cost: float
    matched_pairs: float
    unmatched_qty: float
    unmatched_side: str
    winner: str
    pair_profit: float
    gamble_result: float
    net_pnl: float
    avg_pair_cost: float
    num_buys: int


# ──────────────────────────────────────────────────────────
# PAIR TRADING ENGINE
# ──────────────────────────────────────────────────────────

class PairTradingEngine:
    """
    Accumulates matched YES/NO pairs at combined cost < $1.00.
    Execution model: blockchain CLOB (Polymarket on Polygon).
    """

    def __init__(self, config: Optional[PairConfig] = None,
                 window_duration: float = 300.0):
        self.config = config or PairConfig()
        self.window_duration = window_duration
        self.window_start = time.time()

        # Position state
        self.yes_qty: float = 0.0
        self.no_qty: float = 0.0
        self.yes_cost: float = 0.0
        self.no_cost: float = 0.0
        self.legs: List[PairLeg] = []

        # Lock state
        self.yes_locked: bool = False
        self.no_locked: bool = False

        # Execution state
        self.last_buy_time: float = 0.0
        self.fills_attempted: int = 0
        self.fills_rejected: int = 0
        self.partial_fills: int = 0

        # Ask age tracking
        self._yes_ask_price: Optional[float] = None
        self._yes_ask_since: float = 0.0
        self._no_ask_price: Optional[float] = None
        self._no_ask_since: float = 0.0

        # Stats
        self.buys_attempted: int = 0
        self.buys_executed: int = 0
        self.buys_filtered: int = 0
        self.filter_reasons: dict = {}
        self.last_filter_reason: str = ""
        self.last_filter_value: float = 0.0
        self.last_filter_threshold: float = 0.0

    def reset(self):
        """Reset for a new window."""
        self.yes_qty = 0.0
        self.no_qty = 0.0
        self.yes_cost = 0.0
        self.no_cost = 0.0
        self.legs = []
        self.yes_locked = False
        self.no_locked = False
        self.last_buy_time = 0.0
        self.fills_attempted = 0
        self.fills_rejected = 0
        self.partial_fills = 0
        self._yes_ask_price = None
        self._yes_ask_since = 0.0
        self._no_ask_price = None
        self._no_ask_since = 0.0
        self.buys_attempted = 0
        self.buys_executed = 0
        self.buys_filtered = 0
        self.filter_reasons = {}
        self.last_filter_reason = ""
        self.last_filter_value = 0.0
        self.last_filter_threshold = 0.0
        self.window_start = time.time()

    # ──────────────────────────────────────────────────────
    # ASK AGE TRACKING (for latency model)
    # ──────────────────────────────────────────────────────

    def update_ask_age(self, side: str, current_ask: Optional[float]):
        """Track how long the current best ask has been at this price."""
        now = time.time()
        if side == "YES":
            if current_ask != self._yes_ask_price:
                self._yes_ask_price = current_ask
                self._yes_ask_since = now
        else:
            if current_ask != self._no_ask_price:
                self._no_ask_price = current_ask
                self._no_ask_since = now

    def get_ask_age_ms(self, side: str) -> float:
        """Get age of current best ask in milliseconds."""
        now = time.time()
        if side == "YES" and self._yes_ask_since > 0:
            return (now - self._yes_ask_since) * 1000
        elif side == "NO" and self._no_ask_since > 0:
            return (now - self._no_ask_since) * 1000
        return 0.0

    # ──────────────────────────────────────────────────────
    # PROPERTIES
    # ──────────────────────────────────────────────────────

    @property
    def time_remaining(self) -> float:
        elapsed = time.time() - self.window_start
        return max(0, self.window_duration - elapsed)

    @property
    def in_panic_mode(self) -> bool:
        return self.time_remaining <= self.config.panic_time_seconds

    @property
    def yes_avg(self) -> float:
        return self.yes_cost / self.yes_qty if self.yes_qty > 0 else 0.0

    @property
    def no_avg(self) -> float:
        return self.no_cost / self.no_qty if self.no_qty > 0 else 0.0

    @property
    def matched_pairs(self) -> float:
        return min(self.yes_qty, self.no_qty)

    @property
    def pair_cost(self) -> float:
        if self.yes_qty > 0 and self.no_qty > 0:
            return self.yes_avg + self.no_avg
        return 0.0

    @property
    def total_capital(self) -> float:
        return self.yes_cost + self.no_cost

    @property
    def skew(self) -> float:
        total = self.yes_qty + self.no_qty
        if total == 0:
            return 0.0
        return abs(self.yes_qty - self.no_qty) / total

    @property
    def heavier_side(self) -> str:
        if self.yes_qty > self.no_qty:
            return "YES"
        elif self.no_qty > self.yes_qty:
            return "NO"
        return "BALANCED"

    # ──────────────────────────────────────────────────────
    # CORE DECISION
    # ──────────────────────────────────────────────────────

    def _theta_size_multiplier(self) -> float:
        """
        Gemini #3: Theta-based position sizing.
        Returns multiplier: 1.0 (full), 0.5 (half), 0.0 (hedge-only).
        """
        t = self.time_remaining
        cfg = self.config
        if t > cfg.theta_full_size_until_s:
            return 1.0   # Plenty of time: full size
        elif t > cfg.theta_half_size_until_s:
            return 0.5   # Winding down: half size
        else:
            return 0.0   # Last 30s: hedge-only, no new opens

    def _dynamic_dead_zone(self, side: str) -> float:
        """
        Gemini #4: Dynamic dead zone based on first leg's average cost.
        Returns the max ask price for the second leg (Leg 2).
        If no opposite leg exists, returns the static fallback.

        When Leg 1 is cheap ($0.35), Leg 2 can go up to ~$0.63.
        When Leg 1 is expensive ($0.45), Leg 2 is capped at ~$0.53.
        Hard ceiling at $0.65 to avoid adverse selection regardless.
        """
        cfg = self.config
        # Determine opposite leg avg cost
        if side == "YES" and self.no_qty > 0:
            avg_leg1 = self.no_avg
        elif side == "NO" and self.yes_qty > 0:
            avg_leg1 = self.yes_avg
        else:
            return cfg.value_zone_high  # No opposite leg yet: static fallback

        # Breakeven: leg1_avg + leg2_ask * (1 + fee) <= 0.99
        # Dynamic fee at estimated Leg 2 price, plus 1 cent safety buffer
        est_leg2_price = 1.00 - avg_leg1
        target_leg2 = 0.99 - avg_leg1 - polymarket_taker_fee(est_leg2_price) * est_leg2_price
        # Hard ceiling: never buy Leg 2 above $0.65 (adverse selection)
        # Floor: never below sniper threshold
        return max(cfg.sniper_threshold, min(target_leg2, 0.65))

    def evaluate(
        self,
        yes_ask: Optional[float],
        no_ask: Optional[float],
        yes_bid: Optional[float],
        no_bid: Optional[float],
        yes_ask_levels: Optional[list] = None,
        no_ask_levels: Optional[list] = None,
        yes_bid_depth: float = 0.0,
        no_bid_depth: float = 0.0,
        obi: float = 0.5,
        flow_pressure: float = 0.0,
        has_sweep: bool = False,
        sweep_side: str = "",
    ) -> Optional[dict]:
        """Evaluate whether to buy YES, NO, or nothing."""
        self.buys_attempted += 1

        # Polygon block time cooldown
        now = time.time()
        if self.last_buy_time > 0:
            if (now - self.last_buy_time) < self.config.min_buy_cooldown_s:
                self._filter("cooldown")
                return None

        self._update_locks()

        yes_action = self._evaluate_side(
            "YES", yes_ask, yes_bid, yes_ask_levels, yes_bid_depth,
            obi, flow_pressure, has_sweep, sweep_side,
            opposite_ask=no_ask,
        ) if yes_ask else None

        no_action = self._evaluate_side(
            "NO", no_ask, no_bid, no_ask_levels, no_bid_depth,
            1.0 - obi, -flow_pressure, has_sweep, sweep_side,
            opposite_ask=yes_ask,
        ) if no_ask else None

        if yes_action and no_action:
            if self.yes_qty > self.no_qty + 5:
                return no_action
            elif self.no_qty > self.yes_qty + 5:
                return yes_action
            else:
                if yes_action.get("fill_price", 1) < no_action.get("fill_price", 1):
                    return yes_action
                return no_action

        return yes_action or no_action

    def _evaluate_side(
        self, side: str, ask_price: float, bid_price: Optional[float],
        ask_levels: Optional[list], bid_depth: float,
        obi_for_side: float, flow_for_side: float,
        has_sweep: bool, sweep_side: str,
        opposite_ask: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Evaluate one side with full gate logic.

        GATES:
        #1 Capital limit
        #2 Inventory lock (skew)
        #3 Anti-falling knife
        #4 Theta: hedge-only window (no new opens in last 30s)
        #5 Panic hedge with dynamic breakeven (Gemini #1)
        #6 Atomic entry: Leg 1 only if Leg 2 liquidity exists (Gemini #2)
        #7 Dynamic dead zone (Gemini #4) / Signal filters
        #8 Latency + VWAP + pair cost check
        """
        cfg = self.config

        is_first_leg = (self.yes_qty == 0 and self.no_qty == 0)
        is_completing_pair = (
            (side == "YES" and self.no_qty > 0 and self.yes_qty < self.no_qty) or
            (side == "NO" and self.yes_qty > 0 and self.no_qty < self.yes_qty)
        )
        is_opening_new = not is_completing_pair

        # Gate 1: Capital limit
        cap = cfg.max_position_usd
        if self.in_panic_mode and is_completing_pair:
            cap = cfg.panic_max_position_usd
        if self.total_capital >= cap:
            self._filter("capital_limit", self.total_capital, cap)
            return None

        # Gate 2: Inventory Lock
        if side == "YES" and self.yes_locked:
            self._filter("yes_locked")
            return None
        if side == "NO" and self.no_locked:
            self._filter("no_locked")
            return None

        # Gate 3: Anti-Falling Knife
        if ask_price < cfg.min_first_leg_price and is_first_leg:
            self._filter("falling_knife_first_leg")
            return None
        if ask_price < cfg.min_first_leg_price and not is_completing_pair:
            self._filter("falling_knife_not_completing")
            return None

        # Gate 4: THETA POSITION SIZING (Gemini #3)
        # In the hedge-only window (last 30s), block all new opens
        theta = self._theta_size_multiplier()
        if theta <= 0.0 and is_opening_new:
            self._filter("theta_hedge_only")
            return None

        # Gate 5: PANIC HEDGE with DYNAMIC BREAKEVEN (Gemini #1)
        panic_hedge = (self.in_panic_mode and is_completing_pair)

        if panic_hedge:
            # Dynamic breakeven: max_hedge_price = 1.00 - fee - avg_first_leg
            if side == "YES":
                avg_first_leg = self.no_avg  # NO is Leg 1
            else:
                avg_first_leg = self.yes_avg  # YES is Leg 1

            fee_rate = polymarket_taker_fee(ask_price)
            check_fill = ask_price * (1.0 + fee_rate)
            # $0.99 ceiling (1 cent safety margin against slippage)
            # minus Leg 1 avg cost, minus projected Leg 2 fee
            max_hedge_price = 0.99 - avg_first_leg - polymarket_taker_fee(ask_price) * ask_price

            if check_fill >= max_hedge_price:
                logger.warning(
                    f"[RISK MANAGER] Hedge Aborted: Leg2 ${ask_price:.4f} + "
                    f"fee {fee_rate*100:.2f}% (fill ${check_fill:.4f}) exceeds "
                    f"max ${max_hedge_price:.4f} (Leg1 avg ${avg_first_leg:.4f}). "
                    f"Holding directional gamble instead."
                )
                self._filter("dynamic_breakeven_abort")
                return None

            # Also enforce backtest-optimized pair cost limit
            if cfg.panic_hedge_pair_limit > 0 and self.yes_qty > 0 and self.no_qty > 0:
                qty_est = int(cfg.buy_size_usd / ask_price) or 1
                if not self._would_panic_hedge_cost_ok(side, qty_est, check_fill, cfg.panic_hedge_pair_limit):
                    self._filter("panic_hedge_cost_exceeded")
                    return None

            # SKIP zone/signal gates -> fall through to execution
        else:
            # Gate 6: ATOMIC ENTRY (Gemini #2)
            # First leg: only buy if opposite book has liquidity for pair < $0.95
            if is_first_leg and opposite_ask is not None:
                fee_this = polymarket_taker_fee(ask_price)
                fee_opp = polymarket_taker_fee(opposite_ask)
                projected_pair = (ask_price * (1.0 + fee_this)) + (opposite_ask * (1.0 + fee_opp))
                if projected_pair > cfg.atomic_entry_max_pair:
                    self._filter("atomic_entry_too_wide", projected_pair, cfg.atomic_entry_max_pair)
                    return None

            # Gate 7: DYNAMIC DEAD ZONE (Gemini #4)
            dead_zone_limit = self._dynamic_dead_zone(side)
            if ask_price > dead_zone_limit:
                self._filter("dead_zone_dynamic", ask_price, dead_zone_limit)
                return None

            # Gate 8: SIGNAL FILTERS
            in_sniper_zone = ask_price <= cfg.sniper_threshold

            if in_sniper_zone:
                if self.time_remaining <= cfg.sniper_signal_min_time:
                    self._filter("sniper_too_late")
                    return None
            else:
                # Value zone: apply signal filters
                if obi_for_side > cfg.obi_delay_threshold:
                    self._filter("obi_delay", obi_for_side, cfg.obi_delay_threshold)
                    return None
                if flow_for_side > cfg.flow_delay_threshold:
                    self._filter("flow_delay", flow_for_side, cfg.flow_delay_threshold)
                    return None

        # === BLOCKCHAIN CLOB EXECUTION ===

        # TIME-IN-BOOK latency
        ask_age = self.get_ask_age_ms(side)
        fill_prob = latency_fill_probability(ask_age)
        self.fills_attempted += 1

        if fill_prob <= 0.0:
            self.fills_rejected += 1
            self._filter("ask_too_fresh", ask_age, 200.0)
            return None
        if fill_prob < 1.0:
            if random.random() > fill_prob:
                self.fills_rejected += 1
                self._filter("latency_race_lost", ask_age, 500.0)
                return None

        # THETA-ADJUSTED ORDER SIZE (Gemini #3)
        effective_size = cfg.buy_size_usd * max(theta, 0.5 if is_completing_pair else theta)
        desired_qty = int(effective_size / ask_price)
        if desired_qty < 1:
            self._filter("size_too_small")
            return None

        # VWAP BOOK WALKING
        if ask_levels and len(ask_levels) > 0:
            vwap_price, filled_qty, raw_cost, levels_walked = vwap_fill(
                ask_levels, desired_qty, cfg.max_book_walk_levels
            )
            if filled_qty < 1:
                self._filter("no_depth")
                return None
            if filled_qty < desired_qty:
                self.partial_fills += 1
            qty = filled_qty
        else:
            vwap_price = ask_price
            qty = desired_qty
            levels_walked = 1

        # DETERMINISTIC FEE
        fee_rate = polymarket_taker_fee(vwap_price)
        fill_price = vwap_price * (1.0 + fee_rate)

        # PAIR COST CHECK
        if panic_hedge:
            if fill_price >= 1.00:
                self._filter("panic_fill_over_dollar")
                return None
        else:
            if not self._would_pair_cost_be_ok(side, qty, fill_price):
                self._filter("pair_cost_exceeded", fill_price, cfg.max_pair_cost)
                return None

        is_snipe = (ask_price <= cfg.sniper_threshold) and has_sweep

        return self._execute_buy(
            side, qty, ask_price, vwap_price, fill_price,
            is_snipe, ask_age, levels_walked
        )

    def _would_pair_cost_be_ok(self, side: str, qty: float,
                                fill_price: float) -> bool:
        """Check if buying would keep pair cost under limit."""
        cfg = self.config

        new_yes_qty = self.yes_qty + (qty if side == "YES" else 0)
        new_no_qty = self.no_qty + (qty if side == "NO" else 0)
        new_yes_cost = self.yes_cost + (fill_price * qty if side == "YES" else 0)
        new_no_cost = self.no_cost + (fill_price * qty if side == "NO" else 0)

        if new_yes_qty > 0 and new_no_qty > 0:
            new_pair_cost = (new_yes_cost / new_yes_qty) + (new_no_cost / new_no_qty)
            if self.in_panic_mode:
                return new_pair_cost <= cfg.panic_pair_cost
            return new_pair_cost <= cfg.max_pair_cost

        max_first_leg = cfg.sniper_threshold + 0.15
        if self.in_panic_mode:
            max_first_leg = 0.65
        return fill_price <= max_first_leg

    def _would_panic_hedge_cost_ok(self, side: str, qty: float,
                                    fill_price: float, limit: float) -> bool:
        """Check if a panic hedge would keep pair cost under the hard limit."""
        new_yes_qty = self.yes_qty + (qty if side == "YES" else 0)
        new_no_qty = self.no_qty + (qty if side == "NO" else 0)
        new_yes_cost = self.yes_cost + (fill_price * qty if side == "YES" else 0)
        new_no_cost = self.no_cost + (fill_price * qty if side == "NO" else 0)

        if new_yes_qty > 0 and new_no_qty > 0:
            projected = (new_yes_cost / new_yes_qty) + (new_no_cost / new_no_qty)
            return projected <= limit
        return True

    def _execute_buy(
        self, side: str, qty: float,
        raw_price: float, vwap_price: float, fill_price: float,
        is_snipe: bool, ask_age_ms: float, levels_walked: int
    ) -> dict:
        """Record a paper taker buy — limit-or-fail, no random slippage."""
        cost = fill_price * qty

        leg = PairLeg(
            side=side, qty=qty, raw_price=raw_price,
            fill_price=fill_price, cost=cost, timestamp=time.time(),
            order_type="TAKER", ask_age_ms=ask_age_ms,
            levels_walked=levels_walked, vwap_price=vwap_price,
        )
        self.legs.append(leg)

        if side == "YES":
            self.yes_qty += qty
            self.yes_cost += cost
        else:
            self.no_qty += qty
            self.no_cost += cost

        self.buys_executed += 1
        self.last_buy_time = time.time()
        self._update_locks()

        fee_pct = polymarket_taker_fee(vwap_price) * 100
        snipe_str = " 🎯 SNIPE" if is_snipe else ""
        walk_str = f" L1→L{levels_walked}" if levels_walked > 1 else ""

        logger.info(
            f"[PAIR] TAKER BUY {qty:.0f} {side} @ ${fill_price:.4f} "
            f"(VWAP: ${vwap_price:.3f}, fee: {fee_pct:.1f}%, "
            f"age: {ask_age_ms:.0f}ms{walk_str}){snipe_str} | "
            f"YES: {self.yes_qty:.0f} NO: {self.no_qty:.0f} | "
            f"Pairs: {self.matched_pairs:.0f} PairCost: {self.pair_cost:.4f}"
        )

        return {
            "action": "BUY",
            "side": side,
            "qty": qty,
            "raw_price": raw_price,
            "fill_price": fill_price,
            "vwap_price": vwap_price,
            "cost": cost,
            "is_snipe": is_snipe,
            "order_type": "TAKER",
            "ask_age_ms": ask_age_ms,
            "levels_walked": levels_walked,
            "pair_cost": self.pair_cost,
            "matched_pairs": self.matched_pairs,
            "skew": self.skew,
            "reason": f"Taker {side} @ ${vwap_price:.3f}{snipe_str}{walk_str}",
        }

    def _update_locks(self):
        """Update inventory locks based on skew (Rule 1)."""
        total = self.yes_qty + self.no_qty
        if total < 10:
            self.yes_locked = False
            self.no_locked = False
            return

        if self.skew > self.config.max_skew_pct:
            if self.yes_qty > self.no_qty:
                self.yes_locked = True
                self.no_locked = False
            else:
                self.no_locked = True
                self.yes_locked = False
        else:
            self.yes_locked = False
            self.no_locked = False

    def _filter(self, reason: str, value: float = 0.0, threshold: float = 0.0):
        self.buys_filtered += 1
        self.filter_reasons[reason] = self.filter_reasons.get(reason, 0) + 1
        self.last_filter_reason = reason
        self.last_filter_value = value
        self.last_filter_threshold = threshold

    # ──────────────────────────────────────────────────────
    # SETTLEMENT (Rule 6)
    # ──────────────────────────────────────────────────────

    def settle(self, winner: str) -> WindowResult:
        """Simulate end-of-window settlement."""
        matched = self.matched_pairs
        unmatched_qty = abs(self.yes_qty - self.no_qty)
        unmatched_side = self.heavier_side if unmatched_qty > 0 else "NONE"

        if matched > 0 and self.pair_cost > 0:
            pair_profit = matched * (1.00 - self.pair_cost)
        else:
            pair_profit = 0.0

        if unmatched_qty > 0 and unmatched_side != "NONE":
            if unmatched_side == winner:
                gamble_result = unmatched_qty * 1.00
                if unmatched_side == "YES":
                    unmatched_cost = self.yes_cost - (matched * self.yes_avg)
                else:
                    unmatched_cost = self.no_cost - (matched * self.no_avg)
                gamble_result -= unmatched_cost
            else:
                if unmatched_side == "YES":
                    unmatched_cost = self.yes_cost - (matched * self.yes_avg)
                else:
                    unmatched_cost = self.no_cost - (matched * self.no_avg)
                gamble_result = -unmatched_cost
        else:
            gamble_result = 0.0

        if winner == "YES":
            winning_payout = self.yes_qty * 1.00
        else:
            winning_payout = self.no_qty * 1.00
        net_pnl = winning_payout - self.total_capital

        result = WindowResult(
            yes_qty=self.yes_qty, no_qty=self.no_qty,
            yes_avg_cost=self.yes_avg, no_avg_cost=self.no_avg,
            total_cost=self.total_capital,
            matched_pairs=matched, unmatched_qty=unmatched_qty,
            unmatched_side=unmatched_side, winner=winner,
            pair_profit=pair_profit, gamble_result=gamble_result,
            net_pnl=net_pnl, avg_pair_cost=self.pair_cost,
            num_buys=self.buys_executed,
        )

        logger.info(
            f"\n{'='*60}\n"
            f"[SETTLEMENT] {winner} Won\n"
            f"  YES: {self.yes_qty:.0f} @ ${self.yes_avg:.4f} | "
            f"NO: {self.no_qty:.0f} @ ${self.no_avg:.4f}\n"
            f"  Pairs: {matched:.0f} | Unmatched: {unmatched_qty:.0f} {unmatched_side}\n"
            f"  PairCost: ${self.pair_cost:.4f} | Capital: ${self.total_capital:.2f}\n"
            f"  PairProfit: ${pair_profit:+.2f} | Gamble: ${gamble_result:+.2f} | "
            f"Net: ${net_pnl:+.2f}\n"
            f"  Buys: {self.buys_executed} | "
            f"Rejected: {self.fills_rejected}/{self.fills_attempted} | "
            f"Partial: {self.partial_fills}\n"
            f"{'='*60}"
        )

        return result

    def determine_winner(self, yes_mid: Optional[float],
                         yes_ask: Optional[float]) -> str:
        if yes_ask and yes_ask >= 0.95:
            return "YES"
        if yes_mid and yes_mid > 0.50:
            return "YES"
        return "NO"

    # ──────────────────────────────────────────────────────
    # STATS FOR UI
    # ──────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        return {
            "yes_qty": self.yes_qty,
            "no_qty": self.no_qty,
            "yes_avg": self.yes_avg,
            "no_avg": self.no_avg,
            "pair_cost": self.pair_cost,
            "matched_pairs": self.matched_pairs,
            "unmatched": abs(self.yes_qty - self.no_qty),
            "heavier_side": self.heavier_side,
            "skew": self.skew,
            "total_capital": self.total_capital,
            "max_position_usd": self.config.max_position_usd,
            "time_remaining": self.time_remaining,
            "in_panic": self.in_panic_mode,
            "yes_locked": self.yes_locked,
            "no_locked": self.no_locked,
            "buys_executed": self.buys_executed,
            "buys_filtered": self.buys_filtered,
            "fills_attempted": self.fills_attempted,
            "fills_rejected": self.fills_rejected,
            "partial_fills": self.partial_fills,
            "yes_ask_age_ms": self.get_ask_age_ms("YES"),
            "no_ask_age_ms": self.get_ask_age_ms("NO"),
            "fill_rate": (
                f"{(self.fills_attempted - self.fills_rejected) / self.fills_attempted:.0%}"
                if self.fills_attempted > 0 else "N/A"
            ),
            "filter_reasons": dict(self.filter_reasons),
        }
