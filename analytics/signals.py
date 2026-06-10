"""
signals.py — Actionable trade signal generator.

Converts analytics into concrete money-making recommendations:
  "💰 BUY YES @ 0.04 — sweep detected, momentum building"
  "🎯 SELL NO @ 0.96 — wall absorption, strong support"

IMPORTANT DISCLAIMER:
These are SIGNALS, not financial advice. They indicate opportunities
based on order book microstructure. You still need to:
- Manage position size
- Set stop losses
- Consider the fundamental probability
- Never risk more than you can afford to lose

SIGNAL TYPES:
1. MOMENTUM — price trending with confirming flow
2. ABSORPTION — wall holding, strong conviction at a level
3. SWEEP FOLLOW — aggressive buyer/seller just swept, ride the wave
4. MEAN REVERSION — overextended move, fade it
5. BREAKOUT — regime change from quiet, follow direction
6. WHALE FOLLOW — large player entered, follow the smart money

Each signal has a confidence (0-100%) and suggested entry.
"""

import time
from dataclasses import dataclass
from data.models import Metrics, Side


@dataclass
class TradeSignal:
    """A concrete trade recommendation."""
    timestamp_ms: int
    action: str          # "BUY" or "SELL"
    token: str           # "YES" or "NO"
    reason: str          # Short explanation
    confidence: int      # 0-100
    entry_price: float   # Suggested entry
    signal_type: str     # "momentum", "absorption", "sweep", etc.

    @property
    def time_str(self) -> str:
        t = time.localtime(self.timestamp_ms / 1000)
        return time.strftime("%H:%M:%S", t)

    @property
    def display(self) -> str:
        """Formatted display string."""
        emoji = "🟢" if self.action == "BUY" else "🔴"
        return (
            f"{emoji} {self.action} {self.token} @ {self.entry_price:.2f} "
            f"({self.confidence}%) — {self.reason}"
        )


# Dedup state for signals
_last_signals: dict[str, tuple[float, str]] = {}
_SIGNAL_COOLDOWN = 45.0  # Seconds between same signal type

# Keys include price levels — bound the dict so long sessions don't leak.
# Entries past the cooldown can't suppress anything, so pruning is safe.
_MAX_DEDUP_KEYS = 500


def _prune_expired(now: float) -> None:
    """Drop entries past the cooldown window once the dict grows large."""
    if len(_last_signals) <= _MAX_DEDUP_KEYS:
        return
    for key in [k for k, (ts, _) in _last_signals.items() if now - ts >= _SIGNAL_COOLDOWN]:
        del _last_signals[key]


def _should_emit_signal(key: str, value_hash: str = "") -> bool:
    """Dedup check for signals — same logic as insights."""
    now = time.time()
    _prune_expired(now)
    if key not in _last_signals:
        _last_signals[key] = (now, value_hash)
        return True
    last_time, last_hash = _last_signals[key]
    if value_hash and value_hash != last_hash:
        _last_signals[key] = (now, value_hash)
        return True
    if now - last_time >= _SIGNAL_COOLDOWN:
        _last_signals[key] = (now, value_hash)
        return True
    return False


def generate_signals(
    metrics: Metrics,
    prev_metrics: "Metrics | None" = None,
    token_label: str = "Yes",
) -> list[TradeSignal]:
    """
    Generate actionable trade signals from current metrics.

    Args:
        metrics: Current metrics snapshot
        prev_metrics: Previous metrics (for change detection)
        token_label: Which token we're viewing ("Yes" or "No")

    Returns:
        List of TradeSignal recommendations
    """
    signals = []
    now_ms = int(time.time() * 1000)
    is_yes = token_label.lower() in ("yes", "y")

    if metrics.midpoint is None:
        return signals

    mid = metrics.midpoint
    bid = metrics.best_bid or mid
    ask = metrics.best_ask or mid

    # ── SWEEP FOLLOW ──
    # Someone just aggressively swept multiple levels — follow them
    for sweep in metrics.sweep_events:
        if sweep.side == Side.BUY:
            sig_token = "YES" if is_yes else "NO"
            key = f"sweep_buy_{sweep.start_price}_{sweep.end_price}"
            if _should_emit_signal(key, str(sweep.levels_consumed)):
                signals.append(TradeSignal(
                    timestamp_ms=now_ms,
                    action="BUY", token=sig_token,
                    reason=f"Sweep: {sweep.levels_consumed} levels eaten ↑ — follow the aggressor",
                    confidence=min(55 + sweep.levels_consumed * 8, 85),
                    entry_price=ask,
                    signal_type="sweep",
                ))
        else:
            sig_token = "NO" if is_yes else "YES"
            key = f"sweep_sell_{sweep.start_price}_{sweep.end_price}"
            if _should_emit_signal(key, str(sweep.levels_consumed)):
                signals.append(TradeSignal(
                    timestamp_ms=now_ms,
                    action="SELL" if is_yes else "BUY", token="YES" if is_yes else "NO",
                    reason=f"Sweep: {sweep.levels_consumed} levels eaten ↓ — selling pressure",
                    confidence=min(55 + sweep.levels_consumed * 8, 85),
                    entry_price=bid,
                    signal_type="sweep",
                ))

    # ── ABSORPTION SIGNAL ──
    # Bid wall absorbing sells = support holding = bullish
    # Ask wall absorbing buys = resistance holding = bearish
    for absorption in metrics.absorption_events:
        hold_pct = min(absorption.holding_pct, 1.0)  # Cap at 100%
        if hold_pct > 0.7 and absorption.trades_absorbed >= 3:
            key = f"absorb_{absorption.price}_{absorption.side.value}"
            if _should_emit_signal(key, str(absorption.trades_absorbed)):
                if absorption.side == Side.BUY:
                    # Bid wall absorbing sell pressure = SUPPORT holding = bullish
                    signals.append(TradeSignal(
                        timestamp_ms=now_ms,
                        action="BUY", token="YES" if is_yes else "NO",
                        reason=f"Support holding at {absorption.price:.2f} — {hold_pct:.0%} held after {absorption.trades_absorbed} sells absorbed",
                        confidence=min(60 + absorption.trades_absorbed * 5, 80),
                        entry_price=ask,
                        signal_type="absorption",
                    ))
                else:
                    # Ask wall absorbing buy pressure = RESISTANCE holding = bearish
                    signals.append(TradeSignal(
                        timestamp_ms=now_ms,
                        action="SELL" if is_yes else "BUY",
                        token="NO" if is_yes else "YES",
                        reason=f"Resistance holding at {absorption.price:.2f} — {hold_pct:.0%} held after {absorption.trades_absorbed} buys absorbed",
                        confidence=min(60 + absorption.trades_absorbed * 5, 80),
                        entry_price=bid,
                        signal_type="absorption",
                    ))

    # ── WHALE FOLLOW ──
    for whale in metrics.whale_events:
        dollar_val = whale.price * whale.size
        if dollar_val >= 500:
            key = f"whale_{whale.timestamp_ms}"
            if _should_emit_signal(key):
                if whale.side == Side.BUY:
                    signals.append(TradeSignal(
                        timestamp_ms=now_ms,
                        action="BUY", token="YES" if is_yes else "NO",
                        reason=f"Whale bought {whale.size:,.0f} @ {whale.price:.2f} (${dollar_val:,.0f}) — follow smart money",
                        confidence=60,
                        entry_price=ask,
                        signal_type="whale",
                    ))
                else:
                    signals.append(TradeSignal(
                        timestamp_ms=now_ms,
                        action="SELL" if is_yes else "BUY",
                        token="YES" if is_yes else "NO",
                        reason=f"Whale sold {whale.size:,.0f} @ {whale.price:.2f} (${dollar_val:,.0f}) — big player exiting",
                        confidence=60,
                        entry_price=bid,
                        signal_type="whale",
                    ))

    # ── MOMENTUM + FLOW CONFLUENCE ──
    # Price trending AND flow confirming = strong signal
    if metrics.regime in ("TRENDING_UP", "BREAKOUT") and metrics.regime_confidence > 0.4:
        if metrics.flow_pressure > 0.3 and metrics.price_trend_strength > 0.3:
            key = "momentum_bullish"
            conf_hash = f"{round(metrics.price_trend_strength, 1)}_{round(metrics.flow_pressure, 1)}"
            if _should_emit_signal(key, conf_hash):
                conf = int(50 + metrics.regime_confidence * 20 + metrics.flow_pressure * 15)
                signals.append(TradeSignal(
                    timestamp_ms=now_ms,
                    action="BUY", token="YES" if is_yes else "NO",
                    reason=f"Momentum + flow aligned bullish (trend {metrics.price_trend_strength:+.2f}, flow {metrics.flow_pressure:+.2f})",
                    confidence=min(conf, 85),
                    entry_price=ask,
                    signal_type="momentum",
                ))

    if metrics.regime in ("TRENDING_DOWN",) and metrics.regime_confidence > 0.4:
        if metrics.flow_pressure < -0.3 and metrics.price_trend_strength < -0.3:
            key = "momentum_bearish"
            conf_hash = f"{round(metrics.price_trend_strength, 1)}_{round(metrics.flow_pressure, 1)}"
            if _should_emit_signal(key, conf_hash):
                conf = int(50 + metrics.regime_confidence * 20 + abs(metrics.flow_pressure) * 15)
                signals.append(TradeSignal(
                    timestamp_ms=now_ms,
                    action="SELL" if is_yes else "BUY",
                    token="YES" if is_yes else "NO",
                    reason=f"Momentum + flow aligned bearish (trend {metrics.price_trend_strength:+.2f}, flow {metrics.flow_pressure:+.2f})",
                    confidence=min(conf, 85),
                    entry_price=bid,
                    signal_type="momentum",
                ))

    # ── BREAKOUT SIGNAL ──
    if metrics.regime == "BREAKOUT" and metrics.regime_confidence > 0.5:
        if prev_metrics and prev_metrics.regime in ("QUIET", "RANGING"):
            direction = "up" if metrics.price_velocity > 0 else "down"
            key = f"breakout_{direction}"
            if _should_emit_signal(key):
                if direction == "up":
                    signals.append(TradeSignal(
                        timestamp_ms=now_ms,
                        action="BUY", token="YES" if is_yes else "NO",
                        reason=f"BREAKOUT from {prev_metrics.regime} — price breaking upward",
                        confidence=int(55 + metrics.regime_confidence * 25),
                        entry_price=ask,
                        signal_type="breakout",
                    ))
                else:
                    signals.append(TradeSignal(
                        timestamp_ms=now_ms,
                        action="SELL" if is_yes else "BUY",
                        token="YES" if is_yes else "NO",
                        reason=f"BREAKOUT from {prev_metrics.regime} — price breaking downward",
                        confidence=int(55 + metrics.regime_confidence * 25),
                        entry_price=bid,
                        signal_type="breakout",
                    ))

    # ── STRONG IMBALANCE + FLOW ──
    # Extremely one-sided book with matching flow
    if metrics.obi is not None:
        if metrics.obi >= 0.75 and metrics.flow_pressure > 0.4:
            key = "imbalance_bullish"
            if _should_emit_signal(key, str(round(metrics.obi, 1))):
                signals.append(TradeSignal(
                    timestamp_ms=now_ms,
                    action="BUY", token="YES" if is_yes else "NO",
                    reason=f"Book {metrics.obi:.0%} bid-heavy + aggressive buying — one-sided pressure",
                    confidence=int(55 + (metrics.obi - 0.5) * 40),
                    entry_price=ask,
                    signal_type="imbalance",
                ))
        elif metrics.obi <= 0.25 and metrics.flow_pressure < -0.4:
            key = "imbalance_bearish"
            if _should_emit_signal(key, str(round(metrics.obi, 1))):
                signals.append(TradeSignal(
                    timestamp_ms=now_ms,
                    action="SELL" if is_yes else "BUY",
                    token="YES" if is_yes else "NO",
                    reason=f"Book {metrics.obi:.0%} ask-heavy + aggressive selling — one-sided pressure",
                    confidence=int(55 + (0.5 - metrics.obi) * 40),
                    entry_price=bid,
                    signal_type="imbalance",
                ))

    return signals
