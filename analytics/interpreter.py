"""
interpreter.py — Converts quantitative metrics into natural language insights.

THIS IS WHAT MAKES OBI DIFFERENT FROM A RAW DATA FEED.
Instead of staring at numbers, you get explanations like:
  "Strong buy wall at 0.62 absorbing sell pressure"
  "Liquidity thinning on ask side → bullish bias"

HOW IT WORKS:
A series of rule-based checks examine the current metrics and generate
Insight objects. Each rule has a condition (when to fire) and a template
(what to say). Rules are ordered by importance — the most significant
insights appear first.

DEDUPLICATION:
Each insight has a "key" (e.g., "obi_high", "spoof_0.07_ask").
Once an insight fires, it won't fire again until either:
  - The cooldown period expires (default 30s for info, 15s for alerts)
  - The underlying value changed significantly

This prevents the feed from being flooded with the same message every tick.
"""

import time
from data.models import Metrics, Insight, Side
from config import settings


# ──────────────────────────────────────────────────────────────
# DEDUPLICATION STATE
# ──────────────────────────────────────────────────────────────

# Tracks when each insight key was last emitted
# Key = insight identifier string, Value = (timestamp_s, value_hash)
_last_emitted: dict[str, tuple[float, str]] = {}

# Cooldown periods by severity (seconds)
_COOLDOWNS = {
    "info": 30.0,
    "warning": 20.0,
    "alert": 15.0,
}

# Keys include price levels, so churning markets mint new keys forever.
# Entries older than the longest cooldown can never suppress an emission,
# so pruning them is behavior-preserving. Cap keeps long sessions bounded.
_MAX_DEDUP_KEYS = 500


def _prune_expired(now: float) -> None:
    """Drop entries past every cooldown window once the dict grows large."""
    if len(_last_emitted) <= _MAX_DEDUP_KEYS:
        return
    max_cooldown = max(_COOLDOWNS.values())
    for key in [k for k, (ts, _) in _last_emitted.items() if now - ts >= max_cooldown]:
        del _last_emitted[key]


def _should_emit(key: str, severity: str, value_hash: str = "") -> bool:
    """
    Check if an insight should be emitted based on dedup rules.

    Returns True if:
    - This key has never been emitted, OR
    - Enough time has passed since last emission, OR
    - The value_hash changed (meaning the situation is materially different)
    """
    now = time.time()
    cooldown = _COOLDOWNS.get(severity, 30.0)
    _prune_expired(now)

    if key not in _last_emitted:
        _last_emitted[key] = (now, value_hash)
        return True

    last_time, last_hash = _last_emitted[key]

    # Value changed materially → emit
    if value_hash and value_hash != last_hash:
        _last_emitted[key] = (now, value_hash)
        return True

    # Cooldown expired → emit
    if now - last_time >= cooldown:
        _last_emitted[key] = (now, value_hash)
        return True

    return False


def generate_insights(
    metrics: Metrics,
    prev_metrics: Metrics | None = None,
    token_label: str = "Yes",
) -> list[Insight]:
    """
    Generate natural language insights from current metrics.

    Args:
        metrics: Current computed metrics
        prev_metrics: Previous metrics (for detecting changes)
        token_label: Which token we're viewing ("Yes" or "No")
                     Used to make imbalance labels context-aware.

    Returns:
        List of Insight objects, most important first
    """
    candidates: list[tuple[str, str, Insight]] = []  # (key, value_hash, insight)
    now_ms = int(time.time() * 1000)

    # Context-aware labels based on which token we're watching
    is_yes_token = token_label.lower() in ("yes", "y")
    # When viewing YES token: high OBI = bullish (bids = people buying YES)
    # When viewing NO token: high OBI = bearish (bids = people buying NO = bearish on YES)
    bid_meaning = "YES buyers" if is_yes_token else "NO buyers"
    ask_meaning = "YES sellers" if is_yes_token else "NO sellers"
    high_obi_label = "Bullish" if is_yes_token else "Bearish (NO demand)"
    low_obi_label = "Bearish" if is_yes_token else "Bullish (NO weak)"

    def _add(key: str, message: str, severity: str, value_hash: str = ""):
        """Helper to add a candidate insight with its dedup key."""
        candidates.append((key, value_hash, Insight(
            timestamp_ms=now_ms, message=message, severity=severity,
        )))

    # ──────────────────────────────────────────────────────────
    # IMBALANCE INSIGHTS
    # ──────────────────────────────────────────────────────────

    if metrics.obi is not None:
        # Bucket OBI to nearest 5% so hash only changes on meaningful shifts
        obi_bucket = str(round(metrics.obi * 20) / 20)

        if metrics.obi >= 0.75:
            _add("obi_high",
                 f"{high_obi_label}: {metrics.obi:.0%} of top-of-book is {bid_meaning} — strong bid pressure",
                 "alert", obi_bucket)
        elif metrics.obi >= 0.60:
            _add("obi_bullish",
                 f"Book tilting toward {bid_meaning}: {metrics.obi:.0%} bid liquidity at top levels",
                 "info", obi_bucket)
        elif metrics.obi <= 0.25:
            _add("obi_low",
                 f"{low_obi_label}: only {metrics.obi:.0%} bid liquidity — {ask_meaning} dominating",
                 "alert", obi_bucket)
        elif metrics.obi <= 0.40:
            _add("obi_bearish",
                 f"Book tilting toward {ask_meaning}: {metrics.obi:.0%} bid liquidity, asks building",
                 "info", obi_bucket)

        # Imbalance SHIFT detection (requires previous metrics)
        if prev_metrics and prev_metrics.obi is not None:
            delta = metrics.obi - prev_metrics.obi
            if delta > 0.10:
                _add("obi_shift_up",
                     f"Rapid imbalance shift toward YES: +{delta:.0%} in last update cycle",
                     "warning")
            elif delta < -0.10:
                _add("obi_shift_down",
                     f"Rapid imbalance shift toward NO: {delta:.0%} in last update cycle",
                     "warning")

    # ──────────────────────────────────────────────────────────
    # SPREAD INSIGHTS
    # ──────────────────────────────────────────────────────────

    if metrics.spread is not None:
        spread_bucket = str(round(metrics.spread, 3))

        if metrics.spread <= 0.01:
            _add("spread_tight",
                 f"Spread extremely tight at {metrics.spread:.2f} — high conviction, very liquid",
                 "info", spread_bucket)
        elif metrics.spread >= settings.SPREAD_WIDE_THRESHOLD:
            _add("spread_wide",
                 f"Spread widening to {metrics.spread:.2f} — uncertainty increasing, market makers pulling back",
                 "warning", spread_bucket)

        # Spread change detection
        if prev_metrics and prev_metrics.spread is not None:
            spread_delta = metrics.spread - prev_metrics.spread
            if spread_delta > 0.02:
                _add("spread_widen",
                     f"Spread widened by {spread_delta:.2f} → reduced conviction or liquidity pull",
                     "warning")
            elif spread_delta < -0.02:
                _add("spread_tighten",
                     f"Spread tightened by {abs(spread_delta):.2f} → conviction growing, liquidity returning",
                     "info")

    # ──────────────────────────────────────────────────────────
    # WALL INSIGHTS
    # ──────────────────────────────────────────────────────────

    for wall in metrics.walls:
        wall_type = "Support" if wall.side == Side.BUY else "Resistance"
        verb = "absorbing sell pressure" if wall.side == Side.BUY else "capping upside"
        _add(f"wall_{wall.price}_{wall.side.value}",
             f"{wall_type} wall at {wall.price:.2f} — {wall.size:,.0f} contracts ({wall.strength:.1f}σ above mean) {verb}",
             "alert" if wall.strength > 3.0 else "warning",
             str(round(wall.size, -2)))  # Re-alert if size changes by 100+

    # ──────────────────────────────────────────────────────────
    # FLOW PRESSURE INSIGHTS
    # ──────────────────────────────────────────────────────────

    if abs(metrics.flow_pressure) > 0.5:
        direction = "BUY" if metrics.flow_pressure > 0 else "SELL"
        intensity = "Aggressive" if abs(metrics.flow_pressure) > 0.7 else "Sustained"
        flow_bucket = str(round(metrics.flow_pressure, 1))
        _add("flow_strong",
             f"{intensity} {direction} flow: pressure at {metrics.flow_pressure:+.2f} — "
             f"${metrics.buy_volume:,.0f} bought vs ${metrics.sell_volume:,.0f} sold",
             "alert" if abs(metrics.flow_pressure) > 0.7 else "info",
             flow_bucket)
    elif metrics.buy_volume > 0 or metrics.sell_volume > 0:
        flow_bucket = str(round(metrics.flow_pressure, 1))
        if metrics.flow_pressure > 0.2:
            _add("flow_mild_buy",
                 f"Mild buy pressure: flow at {metrics.flow_pressure:+.2f}",
                 "info", flow_bucket)
        elif metrics.flow_pressure < -0.2:
            _add("flow_mild_sell",
                 f"Mild sell pressure: flow at {metrics.flow_pressure:+.2f}",
                 "info", flow_bucket)

    # ──────────────────────────────────────────────────────────
    # WHALE INSIGHTS
    # ──────────────────────────────────────────────────────────

    for whale in metrics.whale_events:
        dollar_val = whale.price * whale.size
        # Unique per whale event (by timestamp)
        _add(f"whale_{whale.timestamp_ms}",
             f"🐋 Whale {'BUY' if whale.side == Side.BUY else 'SELL'}: "
             f"{whale.size:,.0f} contracts @ {whale.price:.2f} (${dollar_val:,.0f})",
             "alert")

    # ──────────────────────────────────────────────────────────
    # DEPTH INSIGHTS
    # ──────────────────────────────────────────────────────────

    if prev_metrics:
        if prev_metrics.total_bid_depth > 0:
            bid_change = (metrics.total_bid_depth - prev_metrics.total_bid_depth) / prev_metrics.total_bid_depth
            if bid_change < -0.3:
                _add("depth_bid_pull",
                     f"⚠️ Bid liquidity pulled: {bid_change:.0%} drop — support weakening",
                     "alert")
            elif bid_change > 0.3:
                _add("depth_bid_surge",
                     f"Bid depth surging: +{bid_change:.0%} — support building",
                     "warning")

        if prev_metrics.total_ask_depth > 0:
            ask_change = (metrics.total_ask_depth - prev_metrics.total_ask_depth) / prev_metrics.total_ask_depth
            if ask_change < -0.3:
                _add("depth_ask_thin",
                     f"Ask liquidity thinning: {ask_change:.0%} drop → bullish signal (less resistance)",
                     "warning")
            elif ask_change > 0.3:
                _add("depth_ask_build",
                     f"Ask depth building: +{ask_change:.0%} — resistance increasing",
                     "warning")

    # ──────────────────────────────────────────────────────────
    # PHASE 2: SPOOFING INSIGHTS
    # ──────────────────────────────────────────────────────────

    for spoof in metrics.spoof_signals:
        side_label = "bid" if spoof.side == Side.BUY else "ask"
        _add(f"spoof_{spoof.price}_{side_label}",
             f"⚠️ Spoofing signal at {spoof.price:.2f} ({side_label}): "
             f"{spoof.oscillation_count} rapid appear/disappear cycles in {spoof.window_seconds:.0f}s "
             f"(max size seen: {spoof.max_size_seen:,.0f})",
             "alert",
             str(spoof.oscillation_count))  # Re-alert only if count increases

    # ──────────────────────────────────────────────────────────
    # PHASE 2: ABSORPTION INSIGHTS
    # ──────────────────────────────────────────────────────────

    for absorption in metrics.absorption_events:
        side_label = "Support" if absorption.side == Side.BUY else "Resistance"
        absorbing = "sell" if absorption.side == Side.BUY else "buy"
        _add(f"absorb_{absorption.price}_{absorption.side.value}",
             f"🛡️ {side_label} absorption at {absorption.price:.2f}: "
             f"wall ({absorption.wall_size:,.0f}) absorbed {absorption.trades_absorbed} "
             f"{absorbing} trades ({absorption.volume_absorbed:,.0f} volume) — "
             f"still holding {min(absorption.holding_pct, 1.0):.0%} of original size",
             "alert",
             str(absorption.trades_absorbed))

    # ──────────────────────────────────────────────────────────
    # PHASE 2: SWEEP INSIGHTS
    # ──────────────────────────────────────────────────────────

    for sweep in metrics.sweep_events:
        direction = "BUY sweep ↑" if sweep.side == Side.BUY else "SELL sweep ↓"
        _add(f"sweep_{sweep.side.value}_{sweep.start_price}_{sweep.end_price}",
             f"🔥 {direction}: aggressive order ate through {sweep.levels_consumed} levels "
             f"({sweep.start_price:.2f} → {sweep.end_price:.2f}), "
             f"{sweep.total_volume:,.0f} contracts swept — strong conviction",
             "alert",
             str(sweep.levels_consumed))  # Re-alert only if sweep gets bigger

    # ──────────────────────────────────────────────────────────
    # PHASE 3: MOMENTUM INSIGHTS
    # ──────────────────────────────────────────────────────────

    # Price momentum
    if abs(metrics.price_trend_strength) > 0.4:
        direction = "upward" if metrics.price_trend_strength > 0 else "downward"
        accel_note = ""
        if metrics.price_accel != 0:
            if (metrics.price_velocity > 0 and metrics.price_accel > 0) or \
               (metrics.price_velocity < 0 and metrics.price_accel < 0):
                accel_note = " and accelerating"
            elif (metrics.price_velocity > 0 and metrics.price_accel < 0) or \
                 (metrics.price_velocity < 0 and metrics.price_accel > 0):
                accel_note = " but decelerating"

        _add("price_momentum",
             f"📈 Price momentum {direction}{accel_note} "
             f"(trend strength: {metrics.price_trend_strength:+.2f})",
             "warning" if abs(metrics.price_trend_strength) > 0.6 else "info",
             str(round(metrics.price_trend_strength, 1)))

    # OBI momentum
    if abs(metrics.obi_trend_strength) > 0.3:
        direction = "YES (bullish)" if metrics.obi_trend_strength > 0 else "NO (bearish)"
        _add("obi_momentum",
             f"📊 Imbalance steadily shifting toward {direction} "
             f"(OBI trend: {metrics.obi_trend_strength:+.2f})",
             "info",
             str(round(metrics.obi_trend_strength, 1)))

    # Depth divergence
    if abs(metrics.depth_divergence) > 50:
        div_dir = "bullish" if metrics.depth_divergence > 0 else "bearish"
        if metrics.depth_divergence > 0:
            _add("depth_divergence",
                 "↗️ Depth divergence: bids growing while asks shrinking — hidden bullish signal",
                 "warning", div_dir)
        else:
            _add("depth_divergence",
                 "↘️ Depth divergence: asks growing while bids shrinking — hidden bearish signal",
                 "warning", div_dir)

    # Volatility
    if metrics.volatility > 0.5:
        _add("volatility",
             f"⚡ High volatility detected ({metrics.volatility:.2f}) — prices and spreads fluctuating rapidly",
             "warning",
             str(round(metrics.volatility, 1)))
    elif metrics.volatility > 0.3:
        _add("volatility",
             f"⚡ Volatility rising ({metrics.volatility:.2f}) — market becoming unsettled",
             "info",
             str(round(metrics.volatility, 1)))

    # ──────────────────────────────────────────────────────────
    # PHASE 3: REGIME INSIGHTS
    # ──────────────────────────────────────────────────────────

    if metrics.regime_confidence > 0.3:
        regime = metrics.regime
        conf = metrics.regime_confidence
        duration = metrics.regime_duration_s

        regime_messages = {
            "TRENDING_UP": ("🟢📈", "Market in UPTREND", "price and OBI moving up consistently"),
            "TRENDING_DOWN": ("🔴📉", "Market in DOWNTREND", "price and OBI moving down consistently"),
            "RANGING": ("↔️", "Market RANGING", "price oscillating, no clear direction — fade the extremes"),
            "VOLATILE": ("🌊", "Market VOLATILE", "wide swings and spread expansion — exercise caution"),
            "BREAKOUT": ("💥", "BREAKOUT detected", "sudden shift from consolidation — follow the direction"),
            "QUIET": ("😴", "Market QUIET", "low activity, minimal changes — waiting for a catalyst"),
        }

        if regime in regime_messages:
            emoji, label, desc = regime_messages[regime]
            _add("regime",
                 f"{emoji} {label} ({conf:.0%} confidence, {duration:.0f}s duration): {desc}",
                 "alert" if regime == "BREAKOUT" else "info",
                 regime)  # Re-alert only when regime changes

    # Regime CHANGE detection
    if prev_metrics and prev_metrics.regime != "QUIET" and metrics.regime != prev_metrics.regime:
        if metrics.regime_confidence > 0.3:
            _add(f"regime_change_{metrics.regime}",
                 f"🔄 Regime change: {prev_metrics.regime} → {metrics.regime} — market behavior shifting",
                 "alert")

    # ──────────────────────────────────────────────────────────
    # SENTIMENT SUMMARY
    # ──────────────────────────────────────────────────────────

    if abs(metrics.sentiment) > 0.4:
        if metrics.sentiment > 0.4:
            emoji = "🟢"
            label = "BULLISH" if metrics.sentiment > 0.6 else "Leaning bullish"
        else:
            emoji = "🔴"
            label = "BEARISH" if metrics.sentiment < -0.6 else "Leaning bearish"

        sent_bucket = str(round(metrics.sentiment, 1))
        _add("sentiment",
             f"{emoji} Composite sentiment: {label} ({metrics.sentiment:+.2f})",
             "info", sent_bucket)

    # ──────────────────────────────────────────────────────────
    # DEDUP FILTER — only emit insights that pass cooldown
    # ──────────────────────────────────────────────────────────

    insights = []
    for key, value_hash, insight in candidates:
        if _should_emit(key, insight.severity, value_hash):
            insights.append(insight)

    return insights
