"""
market_spec.py — Asset + Timeframe specification for pair trading.

Replaces all hardcoded BTC/5m references with a single immutable
configuration object that flows through the entire stack.

USAGE:
    from execution.market_spec import make_market_spec
    spec = make_market_spec("eth", "1h")
    print(spec.slug_prefix)        # "eth-updown-1h"
    print(spec.binance_symbol)     # "ETHUSDT"
    print(spec.panic_time_seconds) # 120.0
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSpec:
    """Immutable specification for a crypto asset + timeframe pair."""

    # Identity
    asset: str              # "BTC", "ETH", "SOL"
    timeframe: str          # "5m", "15m", "1h", "6h"

    # Derived constants
    interval_seconds: int   # 300, 900, 3600, 21600
    binance_symbol: str     # "BTCUSDT", "ETHUSDT", "SOLUSDT"
    binance_interval: str   # "5m", "15m", "1h", "6h"
    chainlink_symbol: str   # "btc/usd", "eth/usd", "sol/usd"
    slug_prefix: str        # "btc-updown-5m", "eth-updown-1h", etc.

    # ── Display ──

    @property
    def display_name(self) -> str:
        """Short label: 'BTC 5m', 'ETH 1h'."""
        return f"{self.asset} {self.timeframe}"

    @property
    def display_name_long(self) -> str:
        """Full label: 'BTC Up/Down 5m', 'ETH Up/Down 1h'."""
        return f"{self.asset} Up/Down {self.timeframe}"

    # ── Timing (proportional to window duration) ──

    @property
    def panic_time_seconds(self) -> float:
        """When panic hedge mode activates. Shorter ratio for longer windows
        because order books stay deep longer — no sudden liquidity cliff."""
        if self.interval_seconds <= 900:        # 5m, 15m: 3.3%
            return round(self.interval_seconds * 0.033, 1)
        elif self.interval_seconds <= 3600:     # 1h: 1.7%
            return round(self.interval_seconds * 0.017, 1)
        else:                                    # 6h: 1.0%
            return round(self.interval_seconds * 0.010, 1)

    @property
    def theta_full_size_until_s(self) -> float:
        """Full-size orders above this threshold. Longer windows need a higher
        ratio because per-second order flow is thinner."""
        if self.interval_seconds <= 900:        # 5m, 15m: 60%
            return round(self.interval_seconds * 0.60, 1)
        elif self.interval_seconds <= 3600:     # 1h: 75%
            return round(self.interval_seconds * 0.75, 1)
        else:                                    # 6h: 80%
            return round(self.interval_seconds * 0.80, 1)

    @property
    def theta_half_size_until_s(self) -> float:
        """Hedge-only below this. Shorter ratio for longer windows because
        books stay liquid — no need to block new opens early."""
        if self.interval_seconds <= 900:        # 5m, 15m: 10%
            return round(self.interval_seconds * 0.10, 1)
        elif self.interval_seconds <= 3600:     # 1h: 5%
            return round(self.interval_seconds * 0.05, 1)
        else:                                    # 6h: 3%
            return round(self.interval_seconds * 0.03, 1)

    @property
    def sniper_signal_min_time(self) -> float:
        """Ignore sniper override below this. Lower ratio for longer windows
        so late-window sniper opportunities aren't blocked."""
        if self.interval_seconds <= 900:        # 5m: 30%, 15m: 25%
            ratio = 0.30 if self.interval_seconds <= 300 else 0.25
            return round(self.interval_seconds * ratio, 1)
        elif self.interval_seconds <= 3600:     # 1h: 15%
            return round(self.interval_seconds * 0.15, 1)
        else:                                    # 6h: 10%
            return round(self.interval_seconds * 0.10, 1)

    @property
    def rotation_early_seconds(self) -> float:
        """How many seconds before expiry to trigger rotation."""
        return max(10.0, round(self.interval_seconds * 0.033, 1))

    @property
    def window_skip_threshold_s(self) -> float:
        """Skip a window if fewer than this many seconds remain."""
        # 20% for short windows (5m/15m), 10% for long windows (1h/6h)
        ratio = 0.10 if self.interval_seconds >= 3600 else 0.20
        return max(60.0, round(self.interval_seconds * ratio, 1))

    @property
    def end_date_validation_range(self) -> tuple:
        """(min, max) delta in seconds for _parse_end_date validation."""
        return (
            int(self.interval_seconds * 0.4),
            int(self.interval_seconds * 2.0),
        )


# ══════════════════════════════════════════════════════════════
# REGISTRY
# ══════════════════════════════════════════════════════════════

_ASSETS: dict[str, tuple[str, str]] = {
    # asset_key: (binance_symbol, chainlink_symbol)
    "btc": ("BTCUSDT", "btc/usd"),
    "eth": ("ETHUSDT", "eth/usd"),
    "sol": ("SOLUSDT", "sol/usd"),
}

_TIMEFRAMES: dict[str, tuple[int, str]] = {
    # timeframe_key: (interval_seconds, binance_interval)
    "5m":  (300,   "5m"),
    "15m": (900,   "15m"),
    "1h":  (3600,  "1h"),
    "6h":  (21600, "6h"),
}

SUPPORTED_ASSETS = list(_ASSETS.keys())
SUPPORTED_TIMEFRAMES = list(_TIMEFRAMES.keys())


def make_market_spec(asset: str, timeframe: str) -> MarketSpec:
    """
    Create a MarketSpec from user-friendly names.

    Args:
        asset: "btc", "eth", or "sol" (case-insensitive)
        timeframe: "5m", "15m", "1h", or "6h"

    Raises:
        ValueError if asset or timeframe is unknown.
    """
    a = asset.lower().strip()
    tf = timeframe.lower().strip()

    if a not in _ASSETS:
        raise ValueError(f"Unknown asset '{asset}'. Choose from: {SUPPORTED_ASSETS}")
    if tf not in _TIMEFRAMES:
        raise ValueError(f"Unknown timeframe '{timeframe}'. Choose from: {SUPPORTED_TIMEFRAMES}")

    binance_sym, chainlink_sym = _ASSETS[a]
    interval_s, binance_interval = _TIMEFRAMES[tf]
    slug_prefix = f"{a}-updown-{tf}"

    return MarketSpec(
        asset=a.upper(),
        timeframe=tf,
        interval_seconds=interval_s,
        binance_symbol=binance_sym,
        binance_interval=binance_interval,
        chainlink_symbol=chainlink_sym,
        slug_prefix=slug_prefix,
    )
