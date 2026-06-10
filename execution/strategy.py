"""
strategy.py — Strategy engine for automated trading.

Consumes TradeSignals from the analytics layer and decides:
1. Whether to enter a position
2. Position size
3. Entry price
4. Exit conditions (stop loss, take profit, time-based)

STRATEGIES:
- SWEEP_FOLLOW: Follow aggressive sweeps with confirmed flow
- ABSORPTION_FADE: Fade rejected moves at strong walls
- MOMENTUM: Ride trending regimes with flow confluence
- RESOLUTION_ARB: Buy winning side on near-resolved markets

IMPORTANT: This is NOT financial advice. Use at your own risk.
Always start with PAPER mode before using real capital.
"""

import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from data.models import Metrics
from analytics.signals import TradeSignal

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# POLYMARKET DYNAMIC FEE CURVE
# ──────────────────────────────────────────────────────────
# On 5-min and 15-min crypto markets, Polymarket charges a
# dynamic taker fee that varies with price (probability).
#
# Formula: fee = price × (1 - price) × FEE_RATE
# FEE_RATE = 0.0625 (6.25 basis points multiplier)
#
# This means:
#   price=0.50 → fee=1.56% (HIGHEST — where we trade most!)
#   price=0.30 → fee=1.31%
#   price=0.10 → fee=0.56%
#   price=0.05 → fee=0.30%
#   price=0.95 → fee=0.30%
#
# On a round trip (buy+sell) at 50¢, total fee cost ≈ 3.12%
# This is 15x higher than the 0.2% we had before.
# ──────────────────────────────────────────────────────────

POLYMARKET_FEE_RATE = 0.0625  # From Polymarket docs


def polymarket_taker_fee(price: float) -> float:
    """
    Calculate Polymarket's dynamic taker fee for crypto markets.

    Args:
        price: Token price (0.0 to 1.0)

    Returns:
        Fee as a fraction of the trade value.
        e.g. 0.0156 means 1.56% fee

    Examples:
        polymarket_taker_fee(0.50) = 0.0156  (1.56%)
        polymarket_taker_fee(0.30) = 0.0131  (1.31%)
        polymarket_taker_fee(0.10) = 0.0056  (0.56%)
    """
    p = max(0.01, min(0.99, price))  # Clamp to avoid edge cases
    return p * (1 - p) * POLYMARKET_FEE_RATE


class TradingMode(str, Enum):
    PAPER = "paper"     # Simulate orders, no real money
    LIVE = "live"       # Real orders on Polymarket


class PositionSide(str, Enum):
    LONG = "long"       # Bought tokens
    SHORT = "short"     # Sold tokens
    FLAT = "flat"       # No position


@dataclass
class Position:
    """Tracks an open position."""
    token_id: str
    token_label: str        # "YES" or "NO"
    side: PositionSide
    entry_price: float
    size: float             # Number of contracts
    entry_time: float       # Unix timestamp
    signal_type: str        # What triggered entry
    stop_loss: float = 0.0
    take_profit: float = 0.0
    unrealized_pnl: float = 0.0

    @property
    def age_seconds(self) -> float:
        return time.time() - self.entry_time

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.size


@dataclass
class TradeRecord:
    """A completed trade for PnL tracking."""
    token_id: str
    token_label: str
    side: str               # "BUY" or "SELL"
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    signal_type: str
    entry_time: float
    exit_time: float
    duration_s: float


@dataclass
class StrategyConfig:
    """All tunable strategy parameters."""
    # General
    mode: TradingMode = TradingMode.PAPER
    enabled_strategies: list[str] = field(default_factory=lambda: [
        "sweep_follow", "absorption_fade", "momentum", "imbalance"
    ])

    # Entry filters
    min_confidence: int = 65            # Minimum signal confidence to enter
    min_flow_confluence: float = 0.2    # Flow must confirm direction
    cooldown_seconds: float = 30.0      # Min time between entries

    # Position sizing
    base_size_usd: float = 10.0         # Base position size in USD
    max_size_usd: float = 50.0          # Max single position
    scale_with_confidence: bool = True   # Bigger size for higher confidence

    # Risk management
    stop_loss_pct: float = 0.30         # Stop loss as % of entry (30%)
    take_profit_pct: float = 0.50       # Take profit as % of entry (50%)
    max_hold_seconds: float = 300.0     # Force exit after 5 minutes
    max_open_positions: int = 3         # Max concurrent positions
    max_daily_loss_usd: float = 50.0    # Stop trading after this loss

    # Realistic execution costs
    slippage_pct: float = 0.01          # 1% slippage per trade (conservative)
    # Fee is DYNAMIC on 5-min/15-min crypto markets (see polymarket_taker_fee())
    # fee_pct is only used as fallback for non-crypto markets
    fee_pct: float = 0.002              # 0.2% fallback for fee-free markets
    use_dynamic_fees: bool = True       # Use Polymarket's real fee curve

    # Strategy-specific
    sweep_min_levels: int = 3           # Min levels for sweep follow
    absorption_min_holds: int = 5       # Min trades absorbed for fade
    momentum_min_regime_conf: float = 0.5


class StrategyEngine:
    """
    Core strategy engine.

    Pipeline: Signal → Filter → Size → Risk Check → Order
    """

    def __init__(self, config: StrategyConfig, token_id: str, token_label: str):
        self.config = config
        self.token_id = token_id
        self.token_label = token_label

        # State
        self.positions: list[Position] = []
        self.trade_history: list[TradeRecord] = []
        self.daily_pnl: float = 0.0
        self.last_entry_time: float = 0.0
        self._halted = False

        # Session stats
        self.signals_received = 0
        self.signals_filtered = 0
        self.orders_placed = 0

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def has_open_position(self) -> bool:
        return len(self.positions) > 0

    @property
    def total_exposure_usd(self) -> float:
        return sum(p.cost_basis for p in self.positions)

    def halt(self, reason: str = ""):
        """Emergency stop — no more trades."""
        self._halted = True
        logger.warning(f"Strategy HALTED: {reason}")

    def resume(self):
        """Resume trading after halt."""
        self._halted = False
        logger.info("Strategy RESUMED")

    # ──────────────────────────────────────────────────────────
    # MAIN EVALUATION LOOP
    # ──────────────────────────────────────────────────────────

    def evaluate(
        self,
        signals: list[TradeSignal],
        metrics: Metrics,
    ) -> list[dict]:
        """
        Evaluate signals and return order actions.

        Returns list of actions:
        [
            {"action": "BUY", "token": "NO", "size": 100, "price": 0.03, "reason": "..."},
            {"action": "SELL", "token": "NO", "size": 50, "price": 0.04, "reason": "exit: TP"},
        ]
        """
        if self._halted:
            return []

        actions = []
        self.signals_received += len(signals)

        # Check exits first
        exit_actions = self._check_exits(metrics)
        actions.extend(exit_actions)

        # Check daily loss limit
        if self.daily_pnl <= -self.config.max_daily_loss_usd:
            self.halt(f"Daily loss limit hit: ${self.daily_pnl:.2f}")
            return actions

        # Evaluate new entries
        for signal in signals:
            action = self._evaluate_entry(signal, metrics)
            if action:
                actions.append(action)

        return actions

    def _evaluate_entry(
        self, signal: TradeSignal, metrics: Metrics
    ) -> Optional[dict]:
        """Decide whether to enter based on a signal."""

        # Filter 1: Strategy enabled?
        if signal.signal_type not in self.config.enabled_strategies:
            return None

        # Filter 2: Minimum confidence
        if signal.confidence < self.config.min_confidence:
            self.signals_filtered += 1
            return None

        # Filter 3: Cooldown
        if time.time() - self.last_entry_time < self.config.cooldown_seconds:
            return None

        # Filter 4: Max positions
        if len(self.positions) >= self.config.max_open_positions:
            return None

        # Filter 5: Flow confluence
        if signal.action == "BUY" and metrics.flow_pressure < self.config.min_flow_confluence:
            self.signals_filtered += 1
            return None
        if signal.action == "SELL" and metrics.flow_pressure > -self.config.min_flow_confluence:
            self.signals_filtered += 1
            return None

        # Filter 6: Don't enter opposing an existing position
        for pos in self.positions:
            if signal.action == "BUY" and pos.side == PositionSide.SHORT:
                return None
            if signal.action == "SELL" and pos.side == PositionSide.LONG:
                return None

        # Passed all filters — compute size
        size_usd = self._compute_size(signal)

        # REALISTIC PRICING: Buy at ask + slippage + dynamic fee
        # Polymarket 5-min markets charge ~1.56% at 50¢, less at extremes
        if signal.action == "BUY":
            raw_price = metrics.best_ask if metrics.best_ask else signal.entry_price
            slippage = raw_price * self.config.slippage_pct
            if self.config.use_dynamic_fees:
                fee = polymarket_taker_fee(raw_price)  # Dynamic: ~1.56% at 50¢
            else:
                fee = raw_price * self.config.fee_pct
            fill_price = raw_price + slippage + (raw_price * fee)
        else:
            raw_price = metrics.best_bid if metrics.best_bid else signal.entry_price
            slippage = raw_price * self.config.slippage_pct
            if self.config.use_dynamic_fees:
                fee = polymarket_taker_fee(raw_price)
            else:
                fee = raw_price * self.config.fee_pct
            fill_price = raw_price - slippage - (raw_price * fee)

        fill_price = max(fill_price, 0.001)  # Floor at minimum
        size_contracts = int(size_usd / fill_price)

        if size_contracts < 1:
            return None

        # Compute stop/TP based on realistic fill price
        if signal.action == "BUY":
            stop = fill_price * (1 - self.config.stop_loss_pct)
            tp = fill_price * (1 + self.config.take_profit_pct)
        else:
            stop = fill_price * (1 + self.config.stop_loss_pct)
            tp = fill_price * (1 - self.config.take_profit_pct)

        # Create position (tracked internally)
        pos = Position(
            token_id=self.token_id,
            token_label=signal.token,
            side=PositionSide.LONG if signal.action == "BUY" else PositionSide.SHORT,
            entry_price=fill_price,
            size=size_contracts,
            entry_time=time.time(),
            signal_type=signal.signal_type,
            stop_loss=max(stop, 0.0),
            take_profit=min(tp, 1.0),
        )
        self.positions.append(pos)
        self.last_entry_time = time.time()
        self.orders_placed += 1

        cost_str = f"(raw: {raw_price:.3f}, fee: {fee*100:.2f}%, slip+fee: {fill_price - raw_price:+.4f})"

        logger.info(
            f"{'[PAPER] ' if self.config.mode == TradingMode.PAPER else ''}"
            f"ENTRY: {signal.action} {size_contracts} {signal.token} "
            f"@ {fill_price:.4f} {cost_str} "
            f"(SL: {stop:.3f}, TP: {tp:.3f}) "
            f"— {signal.signal_type} {signal.confidence}%"
        )

        return {
            "action": signal.action,
            "token": signal.token,
            "size": size_contracts,
            "price": fill_price,
            "raw_price": raw_price,
            "stop_loss": stop,
            "take_profit": tp,
            "reason": f"Entry: {signal.signal_type} ({signal.confidence}%)",
            "signal_type": signal.signal_type,
            "paper": self.config.mode == TradingMode.PAPER,
        }

    def _check_exits(self, metrics: Metrics) -> list[dict]:
        """Check all open positions for exit conditions."""
        actions = []
        to_remove = []

        mid = metrics.midpoint or 0
        # Realistic exit prices: longs sell at bid, shorts buy at ask
        bid = metrics.best_bid or mid
        ask = metrics.best_ask or mid

        for i, pos in enumerate(self.positions):
            exit_reason = None

            # For exits: longs sell at bid - costs, shorts buy at ask + costs
            if pos.side == PositionSide.LONG:
                exit_raw = bid
                slip = exit_raw * self.config.slippage_pct
                if self.config.use_dynamic_fees:
                    fee_rate = polymarket_taker_fee(exit_raw)
                else:
                    fee_rate = self.config.fee_pct
                realistic_exit = exit_raw - slip - (exit_raw * fee_rate)
            else:
                exit_raw = ask
                slip = exit_raw * self.config.slippage_pct
                if self.config.use_dynamic_fees:
                    fee_rate = polymarket_taker_fee(exit_raw)
                else:
                    fee_rate = self.config.fee_pct
                realistic_exit = exit_raw + slip + (exit_raw * fee_rate)

            exit_price = realistic_exit

            # Stop loss (compare against raw mid for trigger, use realistic for fill)
            if pos.side == PositionSide.LONG and mid <= pos.stop_loss and pos.stop_loss > 0:
                exit_reason = "Stop loss"
                exit_price = min(realistic_exit, pos.stop_loss)  # Might slip past stop
            elif pos.side == PositionSide.SHORT and mid >= pos.stop_loss and pos.stop_loss > 0:
                exit_reason = "Stop loss"
                exit_price = max(realistic_exit, pos.stop_loss)

            # Take profit
            if pos.side == PositionSide.LONG and mid >= pos.take_profit and pos.take_profit > 0:
                exit_reason = "Take profit"
                exit_price = realistic_exit  # Still get hit by spread on exit
            elif pos.side == PositionSide.SHORT and mid <= pos.take_profit and pos.take_profit > 0:
                exit_reason = "Take profit"
                exit_price = realistic_exit

            # Time-based exit
            if pos.age_seconds >= self.config.max_hold_seconds:
                exit_reason = f"Max hold ({self.config.max_hold_seconds:.0f}s)"
                exit_price = realistic_exit

            if exit_reason:
                # Calculate PnL
                if pos.side == PositionSide.LONG:
                    pnl = (exit_price - pos.entry_price) * pos.size
                else:
                    pnl = (pos.entry_price - exit_price) * pos.size

                self.daily_pnl += pnl

                # Record trade
                record = TradeRecord(
                    token_id=pos.token_id,
                    token_label=pos.token_label,
                    side="BUY" if pos.side == PositionSide.LONG else "SELL",
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    size=pos.size,
                    pnl=pnl,
                    signal_type=pos.signal_type,
                    entry_time=pos.entry_time,
                    exit_time=time.time(),
                    duration_s=pos.age_seconds,
                )
                self.trade_history.append(record)
                to_remove.append(i)

                exit_action = "SELL" if pos.side == PositionSide.LONG else "BUY"

                logger.info(
                    f"{'[PAPER] ' if self.config.mode == TradingMode.PAPER else ''}"
                    f"EXIT: {exit_action} {pos.size} {pos.token_label} "
                    f"@ {exit_price:.2f} — {exit_reason} "
                    f"PnL: ${pnl:+.2f} ({pos.age_seconds:.0f}s)"
                )

                actions.append({
                    "action": exit_action,
                    "token": pos.token_label,
                    "size": pos.size,
                    "price": exit_price,
                    "reason": f"Exit: {exit_reason} (PnL: ${pnl:+.2f})",
                    "pnl": pnl,
                    "paper": self.config.mode == TradingMode.PAPER,
                })

        # Remove closed positions (reverse order to preserve indices)
        for i in sorted(to_remove, reverse=True):
            self.positions.pop(i)

        return actions

    def _compute_size(self, signal: TradeSignal) -> float:
        """Compute position size in USD."""
        base = self.config.base_size_usd

        if self.config.scale_with_confidence:
            # Scale: 65% conf = 1x, 80% conf = 1.5x, 90% = 2x
            scale = 1.0 + (signal.confidence - 65) / 50
            scale = max(0.5, min(scale, 2.0))
            base *= scale

        return min(base, self.config.max_size_usd)

    # ──────────────────────────────────────────────────────────
    # STATS
    # ──────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Get session statistics."""
        wins = [t for t in self.trade_history if t.pnl > 0]
        losses = [t for t in self.trade_history if t.pnl <= 0]
        total_trades = len(self.trade_history)

        return {
            "mode": self.config.mode.value,
            "halted": self._halted,
            "open_positions": len(self.positions),
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / max(total_trades, 1),
            "total_pnl": sum(t.pnl for t in self.trade_history),
            "daily_pnl": self.daily_pnl,
            "avg_pnl": sum(t.pnl for t in self.trade_history) / max(total_trades, 1),
            "avg_hold_time": sum(t.duration_s for t in self.trade_history) / max(total_trades, 1),
            "signals_received": self.signals_received,
            "signals_filtered": self.signals_filtered,
            "orders_placed": self.orders_placed,
            "exposure_usd": self.total_exposure_usd,
        }
