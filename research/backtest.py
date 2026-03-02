"""
backtest.py — Backtesting framework for OBI signals.

WHAT THIS DOES:
Takes replay results and measures how ACCURATE each signal was
at predicting future price movement. This answers the key question:
"When OBI said bullish, did the price actually go up?"

HOW IT WORKS:
1. For each metric/signal, define a threshold that triggers a "prediction"
2. Look at what the price did AFTER the signal (1s, 5s, 30s, 60s later)
3. Score each signal: did price move in the predicted direction?
4. Aggregate into hit rates, profit factors, and reliability scores

SIGNAL TYPES TESTED:
- OBI imbalance (>0.6 = bullish, <0.4 = bearish)
- Flow pressure (>0.3 = bullish, <-0.3 = bearish)
- Sentiment composite (>0.3 = bullish, <-0.3 = bearish)
- Wall detection (support wall = bullish, resistance = bearish)
- Whale events (buy whale = bullish, sell whale = bearish)
- Regime changes (trend confirmation)
- Absorption (absorbing side = directional)
- Sweeps (sweep direction = continuation)

OUTPUT:
A BacktestReport with hit rates, average returns, and per-signal
statistics. Use this to tune your thresholds and identify which
signals are most predictive for a given market.

BEGINNER NOTE:
A "hit rate" of 55% means the signal predicted correctly 55% of
the time. In prediction markets, even 52-53% accuracy is useful
because the edge compounds over many trades.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Callable

from data.models import Metrics, Side
from research.replay import ReplayResult

logger = logging.getLogger(__name__)


@dataclass
class SignalInstance:
    """A single signal occurrence with outcome tracking."""
    timestamp_ms: int
    signal_name: str
    direction: str              # "bullish" or "bearish"
    strength: float             # 0.0 to 1.0 confidence
    price_at_signal: float      # Midpoint when signal fired
    # Outcomes filled during evaluation
    price_after_5s: Optional[float] = None
    price_after_30s: Optional[float] = None
    price_after_60s: Optional[float] = None
    price_after_300s: Optional[float] = None
    hit_5s: Optional[bool] = None
    hit_30s: Optional[bool] = None
    hit_60s: Optional[bool] = None
    hit_300s: Optional[bool] = None
    return_5s: float = 0.0
    return_30s: float = 0.0
    return_60s: float = 0.0
    return_300s: float = 0.0


@dataclass
class SignalStats:
    """Aggregated statistics for one signal type."""
    name: str
    total_signals: int = 0
    # Hit rates at different horizons
    hit_rate_5s: float = 0.0
    hit_rate_30s: float = 0.0
    hit_rate_60s: float = 0.0
    hit_rate_300s: float = 0.0
    # Average returns
    avg_return_5s: float = 0.0
    avg_return_30s: float = 0.0
    avg_return_60s: float = 0.0
    avg_return_300s: float = 0.0
    # Best/worst
    best_return: float = 0.0
    worst_return: float = 0.0
    # Directional breakdown
    bullish_count: int = 0
    bearish_count: int = 0

    def summary_line(self) -> str:
        """One-line summary for display."""
        return (
            f"{self.name:20s} | n={self.total_signals:4d} | "
            f"5s:{self.hit_rate_5s:5.1%} 30s:{self.hit_rate_30s:5.1%} "
            f"60s:{self.hit_rate_60s:5.1%} 5m:{self.hit_rate_300s:5.1%} | "
            f"avg60s:{self.avg_return_60s:+.4f}"
        )


@dataclass
class BacktestReport:
    """Complete backtesting results."""
    signal_stats: dict[str, SignalStats] = field(default_factory=dict)
    all_signals: list[SignalInstance] = field(default_factory=list)
    total_signals: int = 0
    duration_minutes: float = 0.0

    def print_report(self):
        """Print a formatted report to console."""
        print("\n" + "=" * 90)
        print("  BACKTEST REPORT")
        print(f"  Duration: {self.duration_minutes:.1f} minutes | Total signals: {self.total_signals}")
        print("=" * 90)
        print(f"{'Signal':20s} | {'Count':>5s} | {'5s':>5s} {'30s':>5s} {'60s':>5s} {'5min':>5s} | {'Avg 60s Return':>14s}")
        print("-" * 90)

        # Sort by 60s hit rate descending
        sorted_stats = sorted(
            self.signal_stats.values(),
            key=lambda s: s.hit_rate_60s,
            reverse=True,
        )

        for stats in sorted_stats:
            if stats.total_signals == 0:
                continue
            print(stats.summary_line())

        print("=" * 90)

        # Best and worst signals
        if sorted_stats:
            best = sorted_stats[0]
            worst = sorted_stats[-1]
            print(f"\n  Best signal (60s): {best.name} at {best.hit_rate_60s:.1%}")
            print(f"  Worst signal (60s): {worst.name} at {worst.hit_rate_60s:.1%}")

    def to_dict(self) -> dict:
        """Export as dictionary for JSON serialization."""
        return {
            "duration_minutes": self.duration_minutes,
            "total_signals": self.total_signals,
            "signals": {
                name: {
                    "count": s.total_signals,
                    "hit_rate_5s": s.hit_rate_5s,
                    "hit_rate_30s": s.hit_rate_30s,
                    "hit_rate_60s": s.hit_rate_60s,
                    "hit_rate_300s": s.hit_rate_300s,
                    "avg_return_60s": s.avg_return_60s,
                }
                for name, s in self.signal_stats.items()
            },
        }


# ──────────────────────────────────────────────────────────────
# SIGNAL EXTRACTORS
# ──────────────────────────────────────────────────────────────

def _extract_signals(metrics_series: list[Metrics]) -> list[SignalInstance]:
    """
    Extract all tradeable signals from a metrics time series.

    Each signal extractor checks the metrics at each timestep and
    produces a SignalInstance if the threshold is exceeded.
    """
    signals = []

    for i, m in enumerate(metrics_series):
        if m.midpoint is None:
            continue

        ts = m.timestamp_ms
        price = m.midpoint

        # OBI Imbalance signal
        if m.obi is not None:
            if m.obi >= 0.65:
                signals.append(SignalInstance(
                    timestamp_ms=ts, signal_name="obi_bullish",
                    direction="bullish", strength=min((m.obi - 0.5) * 2, 1.0),
                    price_at_signal=price,
                ))
            elif m.obi <= 0.35:
                signals.append(SignalInstance(
                    timestamp_ms=ts, signal_name="obi_bearish",
                    direction="bearish", strength=min((0.5 - m.obi) * 2, 1.0),
                    price_at_signal=price,
                ))

        # Flow pressure signal
        if m.flow_pressure >= 0.4:
            signals.append(SignalInstance(
                timestamp_ms=ts, signal_name="flow_bullish",
                direction="bullish", strength=min(m.flow_pressure, 1.0),
                price_at_signal=price,
            ))
        elif m.flow_pressure <= -0.4:
            signals.append(SignalInstance(
                timestamp_ms=ts, signal_name="flow_bearish",
                direction="bearish", strength=min(abs(m.flow_pressure), 1.0),
                price_at_signal=price,
            ))

        # Sentiment signal
        if m.sentiment >= 0.35:
            signals.append(SignalInstance(
                timestamp_ms=ts, signal_name="sentiment_bullish",
                direction="bullish", strength=min(m.sentiment, 1.0),
                price_at_signal=price,
            ))
        elif m.sentiment <= -0.35:
            signals.append(SignalInstance(
                timestamp_ms=ts, signal_name="sentiment_bearish",
                direction="bearish", strength=min(abs(m.sentiment), 1.0),
                price_at_signal=price,
            ))

        # Wall signals
        for wall in m.walls:
            if wall.side == Side.BUY and wall.strength > 2.5:
                signals.append(SignalInstance(
                    timestamp_ms=ts, signal_name="wall_support",
                    direction="bullish", strength=min(wall.strength / 5, 1.0),
                    price_at_signal=price,
                ))
            elif wall.side == Side.SELL and wall.strength > 2.5:
                signals.append(SignalInstance(
                    timestamp_ms=ts, signal_name="wall_resistance",
                    direction="bearish", strength=min(wall.strength / 5, 1.0),
                    price_at_signal=price,
                ))

        # Whale signals
        for whale in m.whale_events:
            direction = "bullish" if whale.side == Side.BUY else "bearish"
            signals.append(SignalInstance(
                timestamp_ms=ts,
                signal_name=f"whale_{direction}",
                direction=direction,
                strength=min(whale.size * whale.price / 10000, 1.0),
                price_at_signal=price,
            ))

        # Absorption signals
        for absorption in m.absorption_events:
            direction = "bullish" if absorption.side == Side.BUY else "bearish"
            signals.append(SignalInstance(
                timestamp_ms=ts,
                signal_name=f"absorption_{direction}",
                direction=direction,
                strength=absorption.holding_pct,
                price_at_signal=price,
            ))

        # Sweep signals
        for sweep in m.sweep_events:
            direction = "bullish" if sweep.side == Side.BUY else "bearish"
            signals.append(SignalInstance(
                timestamp_ms=ts,
                signal_name=f"sweep_{direction}",
                direction=direction,
                strength=min(sweep.levels_consumed / 5, 1.0),
                price_at_signal=price,
            ))

        # Regime signals (only when confident)
        if m.regime_confidence > 0.4:
            if m.regime == "TRENDING_UP":
                signals.append(SignalInstance(
                    timestamp_ms=ts, signal_name="regime_uptrend",
                    direction="bullish", strength=m.regime_confidence,
                    price_at_signal=price,
                ))
            elif m.regime == "TRENDING_DOWN":
                signals.append(SignalInstance(
                    timestamp_ms=ts, signal_name="regime_downtrend",
                    direction="bearish", strength=m.regime_confidence,
                    price_at_signal=price,
                ))
            elif m.regime == "BREAKOUT" and m.price_velocity != 0:
                direction = "bullish" if m.price_velocity > 0 else "bearish"
                signals.append(SignalInstance(
                    timestamp_ms=ts,
                    signal_name=f"breakout_{direction}",
                    direction=direction,
                    strength=m.regime_confidence,
                    price_at_signal=price,
                ))

    return signals


# ──────────────────────────────────────────────────────────────
# OUTCOME EVALUATION
# ──────────────────────────────────────────────────────────────

def _evaluate_outcomes(
    signals: list[SignalInstance],
    metrics_series: list[Metrics],
):
    """
    For each signal, look up what the price did afterward.

    We check the price at 5s, 30s, 60s, and 300s (5 min) after
    the signal fired. A "hit" means price moved in the predicted direction.
    """
    # Build a quick timestamp → midpoint lookup
    price_lookup: list[tuple[int, float]] = []
    for m in metrics_series:
        if m.midpoint is not None:
            price_lookup.append((m.timestamp_ms, m.midpoint))

    if not price_lookup:
        return

    def find_price_at(target_ms: int) -> Optional[float]:
        """Find the closest midpoint to a target timestamp."""
        best = None
        best_diff = float('inf')
        for ts, price in price_lookup:
            diff = abs(ts - target_ms)
            if diff < best_diff:
                best_diff = diff
                best = price
            elif ts > target_ms + 10000:
                break  # Past the target, stop searching
        # Only use if within 10 seconds of target
        return best if best_diff < 10000 else None

    horizons = [
        ("5s", 5000),
        ("30s", 30000),
        ("60s", 60000),
        ("300s", 300000),
    ]

    for signal in signals:
        for label, offset_ms in horizons:
            future_price = find_price_at(signal.timestamp_ms + offset_ms)
            if future_price is None:
                continue

            price_return = future_price - signal.price_at_signal

            # Store outcome
            setattr(signal, f"price_after_{label}", future_price)
            setattr(signal, f"return_{label}", price_return)

            # Was it a hit? (price moved in predicted direction)
            if signal.direction == "bullish":
                hit = price_return > 0
            else:
                hit = price_return < 0
            setattr(signal, f"hit_{label}", hit)


# ──────────────────────────────────────────────────────────────
# AGGREGATION
# ──────────────────────────────────────────────────────────────

def _aggregate_stats(signals: list[SignalInstance]) -> dict[str, SignalStats]:
    """Aggregate signal outcomes into per-signal-type statistics."""
    by_name: dict[str, list[SignalInstance]] = {}
    for s in signals:
        by_name.setdefault(s.signal_name, []).append(s)

    stats = {}
    for name, signal_list in by_name.items():
        st = SignalStats(name=name, total_signals=len(signal_list))

        for horizon in ["5s", "30s", "60s", "300s"]:
            hits = [s for s in signal_list if getattr(s, f"hit_{horizon}") is True]
            evaluated = [s for s in signal_list if getattr(s, f"hit_{horizon}") is not None]
            returns = [getattr(s, f"return_{horizon}") for s in signal_list
                       if getattr(s, f"price_after_{horizon}") is not None]

            if evaluated:
                setattr(st, f"hit_rate_{horizon}", len(hits) / len(evaluated))
            if returns:
                # For bearish signals, flip returns (negative return = good)
                adjusted = []
                for s in signal_list:
                    r = getattr(s, f"return_{horizon}")
                    if getattr(s, f"price_after_{horizon}") is not None:
                        adjusted.append(r if s.direction == "bullish" else -r)
                setattr(st, f"avg_return_{horizon}", sum(adjusted) / len(adjusted) if adjusted else 0)

        # Direction counts
        st.bullish_count = sum(1 for s in signal_list if s.direction == "bullish")
        st.bearish_count = sum(1 for s in signal_list if s.direction == "bearish")

        # Best/worst 60s returns
        returns_60 = [s.return_60s for s in signal_list if s.price_after_60s is not None]
        if returns_60:
            st.best_return = max(returns_60)
            st.worst_return = min(returns_60)

        stats[name] = st

    return stats


# ──────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ──────────────────────────────────────────────────────────────

def run_backtest(replay_result: ReplayResult, verbose: bool = False) -> BacktestReport:
    """
    Run a full backtest on replay results.

    Args:
        replay_result: Output from replay.replay_session()
        verbose: Print progress and results

    Returns:
        BacktestReport with signal statistics

    Usage:
        from research.replay import replay_session
        from research.backtest import run_backtest

        result = replay_session(since_minutes=120)
        report = run_backtest(result, verbose=True)
        report.print_report()
    """
    if verbose:
        print(f"Extracting signals from {len(replay_result.metrics_series)} metrics...")

    # Step 1: Extract all signals
    signals = _extract_signals(replay_result.metrics_series)
    if verbose:
        print(f"Found {len(signals)} signal instances")

    # Step 2: Evaluate outcomes
    if verbose:
        print("Evaluating outcomes...")
    _evaluate_outcomes(signals, replay_result.metrics_series)

    # Step 3: Aggregate statistics
    stats = _aggregate_stats(signals)

    report = BacktestReport(
        signal_stats=stats,
        all_signals=signals,
        total_signals=len(signals),
        duration_minutes=replay_result.duration_minutes,
    )

    if verbose:
        report.print_report()

    return report
