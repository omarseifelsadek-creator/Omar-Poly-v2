"""
settings.py — All configurable parameters for Polymarket OBI.

WHY THIS FILE EXISTS:
Instead of scattering magic numbers throughout the codebase, we keep
every tunable value here. When you want to adjust detection sensitivity,
change a market, or tweak the UI — this is the only file you touch.
"""

# ──────────────────────────────────────────────────────────────
# MARKET CONFIGURATION
# ──────────────────────────────────────────────────────────────
# To find a token_id:
#   1. Go to https://polymarket.com and pick a market
#   2. Look at the URL slug (e.g., "will-bitcoin-hit-100k-in-2025")
#   3. Query: https://gamma-api.polymarket.com/markets?slug=<slug>
#   4. The response contains "clobTokenIds": ["<YES_token>", "<NO_token>"]
#
# You want the YES token ID (first one) for most analysis.
# We'll build a helper script to find this automatically.

TOKEN_ID: str = ""  # ← YOU FILL THIS IN (see README or use the market finder)

# Human-readable label for the terminal UI
MARKET_QUESTION: str = "Loading..."

# ──────────────────────────────────────────────────────────────
# API ENDPOINTS
# ──────────────────────────────────────────────────────────────
CLOB_REST_URL: str = "https://clob.polymarket.com"
CLOB_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API_URL: str = "https://gamma-api.polymarket.com"

# ──────────────────────────────────────────────────────────────
# WEBSOCKET SETTINGS
# ──────────────────────────────────────────────────────────────
# How long to wait before reconnecting after a disconnect (seconds)
WS_RECONNECT_BASE_DELAY: float = 1.0
# Maximum reconnect delay (exponential backoff caps here)
WS_RECONNECT_MAX_DELAY: float = 30.0
# Send a ping every N seconds to keep connection alive
WS_PING_INTERVAL: float = 20.0
# Close connection if no pong received within N seconds
WS_PING_TIMEOUT: float = 10.0

# ──────────────────────────────────────────────────────────────
# ORDER BOOK SETTINGS
# ──────────────────────────────────────────────────────────────
# How many price levels to show in the terminal (top N bids + top N asks)
OB_DISPLAY_LEVELS: int = 10
# How many levels to use for imbalance calculation
OB_IMBALANCE_LEVELS: int = 5

# ──────────────────────────────────────────────────────────────
# ANALYTICS THRESHOLDS
# ──────────────────────────────────────────────────────────────
# Liquidity wall: a level is a "wall" if its size exceeds
# mean + (WALL_STD_MULTIPLIER × std_dev) of all level sizes
WALL_STD_MULTIPLIER: float = 2.0

# Whale detection: an order/trade is "whale-sized" if it exceeds this USD value
WHALE_THRESHOLD_SIZE: float = 5000.0

# Spoofing: flag if a price level oscillates more than this many times in the window
SPOOF_OSCILLATION_THRESHOLD: int = 3
SPOOF_WINDOW_SECONDS: float = 60.0

# Order flow pressure: rolling window for computing buy vs sell pressure
FLOW_WINDOW_SECONDS: float = 120.0

# Spread: thresholds for "tight" and "wide" classification
SPREAD_TIGHT_THRESHOLD: float = 0.02
SPREAD_WIDE_THRESHOLD: float = 0.05

# Sentiment score weights (tuned for prediction markets)
# Flow pressure is the strongest signal — it's committed capital
# OBI is second — shows passive interest
# Depth momentum is noisy so weighted lower
SENTIMENT_WEIGHTS: dict = {
    "obi": 0.20,
    "flow_pressure": 0.35,
    "bid_depth_momentum": 0.10,
    "ask_depth_momentum": 0.10,
    "spread_signal": 0.05,
}

# ──────────────────────────────────────────────────────────────
# PHASE 2: ADVANCED DETECTION SETTINGS
# ──────────────────────────────────────────────────────────────

# Absorption detection: a wall is "absorbing" if it stays within
# ABSORPTION_SIZE_TOLERANCE of its original size while trades hit it.
# Tracked over ABSORPTION_WINDOW_SECONDS.
ABSORPTION_TRADE_COUNT_THRESHOLD: int = 3      # Min trades hitting the level
ABSORPTION_SIZE_TOLERANCE: float = 0.3         # Wall keeps ≥70% of size → absorbing
ABSORPTION_WINDOW_SECONDS: float = 30.0

# Sweep detection: an aggressive order eats through N+ levels
SWEEP_MIN_LEVELS: int = 3                      # Minimum levels consumed
SWEEP_WINDOW_SECONDS: float = 5.0              # Time window to detect sweeps

# Level history: how long to track per-level size changes
LEVEL_HISTORY_WINDOW_SECONDS: float = 120.0    # 2 minutes of per-level history
LEVEL_HISTORY_MAX_ENTRIES: int = 200            # Max entries per level

# Spoofing: minimum size for a level to be considered "significant"
# (avoids flagging tiny orders as spoofing)
SPOOF_MIN_SIZE: float = 500.0

# Passive whale detection (large limit orders, not just trades)
PASSIVE_WHALE_THRESHOLD: float = 3000.0        # Large resting order threshold

# ──────────────────────────────────────────────────────────────
# PHASE 3: MOMENTUM & REGIME DETECTION
# ──────────────────────────────────────────────────────────────
# EMA periods for momentum tracking.
# Fast EMA reacts quickly (good for short-term signals).
# Slow EMA smooths more (good for trend confirmation).
MOMENTUM_FAST_PERIOD: int = 15        # ~15 updates for fast EMA
MOMENTUM_SLOW_PERIOD: int = 50        # ~50 updates for slow EMA (trend baseline)

# Minimum number of updates before momentum signals are meaningful
MOMENTUM_MIN_SAMPLES: int = 10

# Regime detection: volatility score above this = VOLATILE regime
REGIME_VOLATILITY_THRESHOLD: float = 0.3

# Regime switching: minimum score advantage to change regime
# Higher = more stable (less regime flipping), lower = more responsive
REGIME_SWITCH_THRESHOLD: float = 1.5

# Advanced sentiment: weights for Phase 3 signals in composite score
SENTIMENT_MOMENTUM_WEIGHT: float = 0.20   # Price momentum contribution
SENTIMENT_REGIME_WEIGHT: float = 0.15     # Regime alignment contribution
SENTIMENT_VOLATILITY_WEIGHT: float = 0.08 # Volatility penalty (dampener)
SENTIMENT_DETECTION_WEIGHT: float = 0.20  # Absorption/sweep signal contribution

# ──────────────────────────────────────────────────────────────
# STORAGE SETTINGS
# ──────────────────────────────────────────────────────────────
# SQLite database file path
DB_PATH: str = "data/obi.db"
# How often to save an order book snapshot (seconds)
SNAPSHOT_INTERVAL_SECONDS: float = 5.0
# Enable/disable storage (set False to run without writing to disk)
STORAGE_ENABLED: bool = False

# ──────────────────────────────────────────────────────────────
# PHASE 4: INTELLIGENCE DASHBOARD
# ──────────────────────────────────────────────────────────────
# CVD divergence: minimum opposing strength to flag a divergence
CVD_DIVERGENCE_THRESHOLD: float = 0.3

# OBI velocity: rate of change thresholds (per second)
OBI_VELOCITY_STACKING_THRESHOLD: float = 0.02   # OBI accelerating = "STACKING"
OBI_VELOCITY_PULLING_THRESHOLD: float = -0.02   # OBI decelerating = "PULLING"

# Liquidity voids: levels below this fraction of average = Flash Zone
LIQUIDITY_VOID_THRESHOLD: float = 0.10           # < 10% of 10-level avg

# Vegas Flash: order book size-change visual highlighting
VEGAS_FLASH_THRESHOLD: float = 0.25              # 25% change = flash
VEGAS_FLASH_EXTREME: float = 0.50                # 50% change = extreme flash
VEGAS_FLASH_WINDOW_SECONDS: float = 3.0          # Look-back for size comparison

# Institutional absorption: minimum reload cycles to flag
INSTITUTIONAL_ABSORPTION_RELOADS: int = 2

# ──────────────────────────────────────────────────────────────
# TERMINAL UI SETTINGS
# ──────────────────────────────────────────────────────────────
# How often to refresh the terminal display (seconds)
UI_REFRESH_RATE: float = 0.5
# Maximum number of insight messages to show in the feed
UI_MAX_INSIGHTS: int = 15
# Maximum number of trades to show in the tape
UI_MAX_TRADES: int = 10

# ──────────────────────────────────────────────────────────────
# LIVE EXECUTION SAFETY (B7/B8)
# ──────────────────────────────────────────────────────────────
# Ambiguous-order reconciliation: when a live FOK submission errors or
# returns an unparseable response, the order may still have matched.
# We poll our own trade history this many times before declaring it
# unfilled and halting the window.
LIVE_RECONCILE_ATTEMPTS: int = 3
LIVE_RECONCILE_DELAY_SECONDS: float = 1.5
LIVE_RECONCILE_QTY_TOLERANCE: float = 0.05       # ±5% size match
LIVE_RECONCILE_QUERY_TIMEOUT_SECONDS: float = 10.0

# Message-loop crash escalation: end the window instead of reconnect-
# spinning after this many consecutive crashes with no progress.
MSG_CRASH_STREAK_LIMIT: int = 3
MSG_CRASH_PROGRESS_MESSAGES: int = 10            # msgs that reset the streak

# Kill switch: warn when projected loss reaches this fraction of the cap
KILL_SWITCH_WARN_FRACTION: float = 0.8

# Database write queue (B16): bounded so a stalled disk can't grow memory;
# overflow drops are counted and warned at most once per interval.
DB_WRITE_QUEUE_SIZE: int = 1000
DB_DROP_WARN_INTERVAL_SECONDS: float = 60.0

# Settlement under a stop request (B18): give resolution polling this long
# after Ctrl+C before falling back to order-book price resolution.
SETTLE_STOP_DEADLINE_SECONDS: float = 60.0
