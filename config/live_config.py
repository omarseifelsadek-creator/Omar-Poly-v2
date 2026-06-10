"""
live_config.py — Hot-reloading strategy configuration.

Watches config/strategy.conf and reloads when it changes.
No restart needed — edit the file and changes apply within seconds.
"""

import os
import configparser
import logging
from typing import Optional

from execution.strategy import StrategyConfig, TradingMode

logger = logging.getLogger(__name__)

CONFIG_PATH = "config/strategy.conf"

# [pairs] section fallbacks = the v15 baseline (EXP-002). A missing
# section, missing key, or unparseable value falls back to these, so an
# absent/broken conf can never silently change trading behavior.
PAIRS_PARAM_DEFAULTS: dict = {
    "target_pair_cost": 0.96,
    "max_pair_cost": 0.96,
    "panic_pair_cost": 1.02,
    "panic_hedge_pair_limit": 0.97,
    "atomic_entry_max_pair": 0.99,
    "buy_size_usd": 10.0,
    "max_position_usd": 100.0,
    "panic_max_position_usd": 116.0,
    "max_unmatched_usd": 30.0,
    "max_skew_pct": 0.30,
    "sniper_threshold": 0.35,
    "value_zone_high": 0.43,
    "min_first_leg_price": 0.15,
    "obi_delay_threshold": 0.85,
    "flow_delay_threshold": 0.75,
    "min_buy_cooldown_s": 2.0,
    "max_book_walk_levels": 3,
}


def load_pairs_params(path: str = None) -> dict:
    """
    Fresh parse of strategy.conf's [pairs] section (B12).

    Called once per window rotation by PairRunner — params are frozen
    for the duration of a window so each window's data reflects exactly
    one param set. Per-key fallback: one bad value doesn't discard the
    rest of the section.

    Returns a dict of PairConfig kwargs (timing params excluded — those
    derive from MarketSpec per timeframe).
    """
    path = path or CONFIG_PATH
    params = dict(PAIRS_PARAM_DEFAULTS)

    if not os.path.exists(path):
        logger.warning(f"[pairs] config not found at {path} — using v15 defaults")
        return params

    cp = configparser.ConfigParser()
    try:
        cp.read(path)
    except configparser.Error as e:
        logger.error(f"[pairs] config unreadable ({e}) — using v15 defaults")
        return params

    if not cp.has_section("pairs"):
        return params

    # A typo'd key would silently do nothing and ruin an experiment's
    # interpretation — call it out loudly.
    for key in cp.options("pairs"):
        if key not in PAIRS_PARAM_DEFAULTS:
            logger.warning(
                f"[pairs] unknown key '{key}' in {path} — typo? It has NO effect."
            )

    for key, default in PAIRS_PARAM_DEFAULTS.items():
        try:
            if isinstance(default, int) and not isinstance(default, bool):
                params[key] = cp.getint("pairs", key, fallback=default)
            else:
                params[key] = cp.getfloat("pairs", key, fallback=default)
        except (ValueError, TypeError):
            logger.error(
                f"[pairs] bad value for '{key}' — keeping default {default}"
            )
    return params


class LiveConfig:
    """
    Watches strategy.conf and reloads on change.

    Usage:
        live = LiveConfig()
        config = live.get_config()  # Returns StrategyConfig

        # Call periodically (e.g., every 5 seconds)
        if live.check_reload():
            new_config = live.get_config()
            strategy.config = new_config
    """

    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self._last_mtime: float = 0
        self._config: Optional[StrategyConfig] = None
        self._rotation_side: str = "auto"
        self._telegram_enabled: bool = False
        self._alert_min_confidence: int = 75

        # Initial load
        self._load()

    def _load(self):
        """Parse the config file into a StrategyConfig."""
        if not os.path.exists(self.path):
            logger.warning(f"Config file not found: {self.path}, using defaults")
            self._config = StrategyConfig()
            return

        try:
            cp = configparser.ConfigParser()
            cp.read(self.path)
            self._last_mtime = os.path.getmtime(self.path)

            # Strategy section
            mode_str = cp.get("strategy", "mode", fallback="paper").strip()
            mode = TradingMode.LIVE if mode_str == "live" else TradingMode.PAPER

            enabled_str = cp.get("strategy", "enabled", fallback="sweep_follow, absorption_fade, momentum, imbalance")
            enabled = [s.strip() for s in enabled_str.split(",")]

            min_conf = cp.getint("strategy", "min_confidence", fallback=65)
            min_flow = cp.getfloat("strategy", "min_flow_confluence", fallback=0.2)
            cooldown = cp.getfloat("strategy", "cooldown", fallback=30)

            # Sizing section
            base_size = cp.getfloat("sizing", "base_size", fallback=10)
            max_size = cp.getfloat("sizing", "max_size", fallback=50)
            scale = cp.getboolean("sizing", "scale_with_confidence", fallback=True)

            # Risk section
            sl = cp.getfloat("risk", "stop_loss_pct", fallback=0.30)
            tp = cp.getfloat("risk", "take_profit_pct", fallback=0.50)
            max_hold = cp.getfloat("risk", "max_hold_seconds", fallback=300)
            max_pos = cp.getint("risk", "max_positions", fallback=3)
            max_daily = cp.getfloat("risk", "max_daily_loss", fallback=50)

            # Rotation section
            self._rotation_side = cp.get("rotation", "side", fallback="auto").strip()

            # Alerts section
            self._telegram_enabled = cp.getboolean("alerts", "telegram", fallback=False)
            self._alert_min_confidence = cp.getint("alerts", "alert_min_confidence", fallback=75)

            self._config = StrategyConfig(
                mode=mode,
                enabled_strategies=enabled,
                min_confidence=min_conf,
                min_flow_confluence=min_flow,
                cooldown_seconds=cooldown,
                base_size_usd=base_size,
                max_size_usd=max_size,
                scale_with_confidence=scale,
                stop_loss_pct=sl,
                take_profit_pct=tp,
                max_hold_seconds=max_hold,
                max_open_positions=max_pos,
                max_daily_loss_usd=max_daily,
            )

            logger.info(f"Config loaded: mode={mode_str}, strategies={enabled}, "
                        f"confidence>={min_conf}, size=${base_size}-${max_size}")

        except Exception as e:
            logger.error(f"Failed to parse config: {e}, keeping current config")

    def check_reload(self) -> bool:
        """Check if config file changed and reload if so. Returns True if reloaded."""
        if not os.path.exists(self.path):
            return False

        try:
            mtime = os.path.getmtime(self.path)
            if mtime > self._last_mtime:
                logger.info("Config file changed, reloading...")
                self._load()
                return True
        except Exception:
            pass
        return False

    def get_config(self) -> StrategyConfig:
        """Get the current strategy config."""
        if not self._config:
            self._config = StrategyConfig()
        return self._config

    @property
    def rotation_side(self) -> str:
        return self._rotation_side

    @property
    def telegram_enabled(self) -> bool:
        return self._telegram_enabled

    @property
    def alert_min_confidence(self) -> int:
        return self._alert_min_confidence
