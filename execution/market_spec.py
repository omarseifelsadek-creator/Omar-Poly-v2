"""
market_spec.py — Asset + Timeframe specification for pair trading.

Replaces all hardcoded BTC/5m references with a single immutable
configuration object that flows through the entire stack.

USAGE:
    from execution.market_spec import make_market_spec
    spec = make_market_spec("eth", "15m")
    print(spec.slug_prefix)        # "eth-updown-15m"
    print(spec.binance_symbol)     # "ETHUSDT"
    print(spec.panic_time_seconds) # 29.7

SUPPORTED TIMEFRAMES (verified live on Polymarket 2026-04-08):
    5m, 15m

1h was previously supported but Polymarket has deprecated it —
no `*-updown-1h-*` events are published. Do not re-add without
confirming via a fresh Gamma API probe.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketSpec:
    """Immutable specification for a crypto asset + timeframe pair."""

    # Identity
    asset: str              # "BTC", "ETH", "SOL", "XRP"
    timeframe: str          # "5m", "15m"

    # Derived constants
    interval_seconds: int   # 300, 900
    binance_symbol: str     # "BTCUSDT", "ETHUSDT", "SOLUSDT"
    binance_interval: str   # "5m", "15m"
    chainlink_symbol: str   # "btc/usd", "eth/usd", "sol/usd"
    slug_prefix: str        # "btc-updown-5m", "eth-updown-15m", etc.

    # ── Slug construction ──

    def build_event_slug(self, window_start: int) -> str:
        """Build the Polymarket event slug for a specific window.

        Format: {asset}-updown-{tf}-{unix_window_start}
        Example: btc-updown-5m-1775667000
        """
        return f"{self.slug_prefix}-{window_start}"

    # ── Display ──

    @property
    def display_name(self) -> str:
        """Short label: 'BTC 5m', 'ETH 15m'."""
        return f"{self.asset} {self.timeframe}"

    @property
    def display_name_long(self) -> str:
        """Full label: 'BTC Up/Down 5m', 'ETH Up/Down 15m'."""
        return f"{self.asset} Up/Down {self.timeframe}"

    # ── Timing (proportional to window duration) ──

    @property
    def panic_time_seconds(self) -> float:
        """When panic hedge mode activates (3.3% of window)."""
        return round(self.interval_seconds * 0.033, 1)

    @property
    def theta_full_size_until_s(self) -> float:
        """Full-size orders above this threshold (60% of window)."""
        return round(self.interval_seconds * 0.60, 1)

    @property
    def theta_half_size_until_s(self) -> float:
        """Hedge-only below this (10% of window)."""
        return round(self.interval_seconds * 0.10, 1)

    @property
    def sniper_signal_min_time(self) -> float:
        """Ignore sniper override below this (5m: 30%, 15m: 25%)."""
        ratio = 0.30 if self.interval_seconds <= 300 else 0.25
        return round(self.interval_seconds * ratio, 1)

    @property
    def rotation_early_seconds(self) -> float:
        """How many seconds before expiry to trigger rotation."""
        return max(10.0, round(self.interval_seconds * 0.033, 1))

    @property
    def window_skip_threshold_s(self) -> float:
        """Skip a window if fewer than this many seconds remain (20% of window)."""
        return max(60.0, round(self.interval_seconds * 0.20, 1))

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
    "xrp": ("XRPUSDT", "xrp/usd"),
}

_TIMEFRAMES: dict[str, tuple[int, str]] = {
    # timeframe_key: (interval_seconds, binance_interval)
    "5m":  (300, "5m"),
    "15m": (900, "15m"),
}

SUPPORTED_ASSETS = list(_ASSETS.keys())
SUPPORTED_TIMEFRAMES = list(_TIMEFRAMES.keys())


def make_market_spec(asset: str, timeframe: str) -> MarketSpec:
    """
    Create a MarketSpec from user-friendly names.

    Args:
        asset: "btc", "eth", "sol", or "xrp" (case-insensitive)
        timeframe: "5m" or "15m"

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
