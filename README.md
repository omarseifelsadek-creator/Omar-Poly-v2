# Polymarket Order Book Intelligence (OBI) — v5.0

A real-time market microstructure intelligence engine for Polymarket prediction markets.

**OBI monitors the order book in real time and surfaces what matters — manipulation, whale activity, structural shifts — so you don't have to stare at raw numbers.**

Instead of scrolling through a ladder of prices, you get:
- "🏦 INSTITUTIONAL: Wall at 0.30 reloaded 3x while absorbing 15K contracts"
- "⚡ Aggressive buyer swept 4 levels 0.03→0.07 (12K contracts)"
- "⚠️ DIVERGENCE: Price rising but CVD falling — sellers absorbing buying momentum"
- "👻 Possible spoof at 0.58 ask — 4 oscillations, peak 8K"
- "🕳️ Flash Zone: Thin liquidity at 0.48 (ask) — 6% of avg depth"
- "🐋 Whale Buy (taker): 23K contracts @ 0.03 ($690)"
- "🛡️ Absorption @ 0.55 (bid) — wall held 92% after 5 trades"

No API keys required. All Polymarket order book data is public.

---

## Quick Start (5 minutes)

### Step 1: Install Python
You need Python 3.10 or higher. Check your version:
```bash
python --version
```

If you don't have Python:
- **Mac**: `brew install python`
- **Windows**: Download from [python.org](https://www.python.org/downloads/) — check "Add to PATH"
- **Linux**: `sudo apt install python3 python3-pip python3-venv`

### Step 2: Set Up
```bash
cd Omar-Poly-v2

python3 -m venv venv
source venv/bin/activate    # Mac/Linux
# venv\Scripts\activate     # Windows

pip install -r requirements.txt
```

### Step 3: Run
```bash
python main.py
```

Interactive market selector opens — search for any market, browse active ones, or enter a token ID.

### Direct Launch
```bash
python main.py --search "bitcoin"
python main.py --slug will-btc-hit-100k-in-2025
python main.py --token <TOKEN_ID>
```

### Advanced Modes
```bash
# BTC 5-minute auto-rotating
python main.py --btc5m

# Pair trading (YES+NO accumulation)
python main.py --pairs --asset btc --timeframe 5m

# Headless mode (no dashboard, CSV logging only)
python main.py --headless

# Record L2 snapshots for backtesting
python main.py --record
```

---

## The Dashboard

```
┌──────────────────────────────────────────────────────────────┐
│ OBI │ Market Question │ [YES] │ REGIME │ Wt.Mid │ Spd │ ● 12ms│
├──────────────────────────┬───────────────────────────────────┤
│                          │ MARKET STATE                      │
│     ORDER BOOK           │ Dominant│Conviction│Liq│Agg│Risk  │
│  (heat ladder +          ├───────────────────────────────────┤
│   Vegas Flash)           │ ANALYTICS                         │
│                          │ Wt.Mid│OBI+Vel│CVD│Flow│Depth│Vol │
├──────────────────────────┼───────────────────────────────────┤
│ ACTIVITY INTELLIGENCE    │ TAPE                              │
│ Timeline + Flow + Regime │ TIME S SIZE PX VAL                │
│ Narrative Event Feed     │                                   │
├──────────────────────────┴───────────────────────────────────┤
│ Ctrl+C │ OBI v5.0 INTEL │ Session 02:15 │ Msgs 847          │
└──────────────────────────────────────────────────────────────┘
```

**6 panels**, all intelligence — no trading UI.

---

## Key Metrics

| Metric | What It Means |
|--------|--------------|
| **OBI** | Order Book Imbalance — ratio of bids to total liquidity (>60% = bullish) |
| **Weighted Midpoint** | VWAP-weighted mid, more accurate than simple (bid+ask)/2 |
| **OBI Velocity** | Rate of change in imbalance — STACKING (book building) / PULLING (book thinning) |
| **CVD** | Cumulative Volume Delta — net market aggression. Rising = buyers dominating |
| **CVD Divergence** | Price and CVD moving opposite directions — often precedes reversal |
| **Flow Pressure** | Aggressive buyer/seller ratio (-1 to +1) |
| **Sentiment** | Composite score blending all phases (-1 to +1) |
| **Regime** | Market state: TRENDING_UP/DOWN, RANGING, VOLATILE, BREAKOUT, QUIET |
| **Liquidity Voids** | Flash Zones — gaps where depth < 10% of average (price can teleport) |
| **Vegas Flash** | Rapid size changes in the book without trades — cyan = stacking, red = pulling |
| **Walls** | Abnormally large orders acting as support/resistance |
| **Spoofing** | Rapid appear/disappear cycles at a price level (manipulation) |
| **Absorption** | Wall holding its size while being hit by trades (strong conviction) |
| **Institutional Absorption** | Wall that reloads after being partially consumed — likely algo/institution |
| **Sweeps** | Aggressive orders eating through multiple price levels (urgency) |

---

## Architecture

### Data Pipeline
```
WebSocket msg
  → message_parser
  → OrderBook + LevelTracker (state/)
  → compute_all_metrics + MomentumEngine (analytics/)
  → generate_insights (analytics/interpreter.py)
  → Terminal Dashboard (ui/terminal.py) + Telegram + CSV + SQLite
```

### Three-Phase Analytics
1. **Phase 1 — Snapshots**: OBI, walls, whales, flow pressure, base sentiment
2. **Phase 2 — Time-series patterns**: Spoofing (oscillations), Absorption (wall holding), Sweeps (level consumption)
3. **Phase 3 — Momentum & Regime**: EMA smoothing, volatility tracking, market regime detection with hysteresis

### Intelligence Layer (v5.0)
- **CVD Tracker**: Session-persistent, survives WebSocket reconnects
- **OBI Velocity**: 5s/30s rate of change with STACKING/PULLING classification
- **Liquidity Void Detection**: Identifies thin spots where price can gap
- **Institutional Absorption**: Detects wall reload patterns (algo refilling)
- **Vegas Flash**: Visual highlighting of rapid order book manipulation

### Project Structure
```
├── main.py                  # Entry point — OBIApp lifecycle
├── config/
│   ├── settings.py          # All tunable thresholds
│   └── strategy.conf        # Hot-reloadable trading parameters
├── data/
│   ├── models.py            # Pydantic data models
│   ├── rest_client.py       # Gamma/CLOB HTTP client
│   ├── websocket_client.py  # Real-time WebSocket stream
│   └── message_parser.py    # Raw JSON → typed objects
├── state/
│   ├── orderbook.py         # In-memory order book state
│   └── level_tracker.py     # Per-level history (spoofing, absorption, flash)
├── analytics/
│   ├── metrics.py           # Core metric computations (pure functions)
│   ├── cvd.py               # Cumulative Volume Delta tracker
│   ├── detectors.py         # Spoofing, absorption, sweep detection
│   ├── momentum.py          # EMA, regime detection, volatility
│   ├── interpreter.py       # Metrics → natural language insights
│   └── signals.py           # Trade signal generation
├── execution/               # Strategy, pair trading, market rotation
├── storage/
│   └── database.py          # Async SQLite via queue
├── ui/
│   └── terminal.py          # Rich terminal dashboard (6-panel)
├── telegram/
│   └── telegram_bot.py      # Alert notifications
└── research/                # Offline analysis: heatmap, replay, backtest
```

---

## Research Tools

Run offline analysis on stored session data:

```bash
python research_cli.py summary          # Database overview
python research_cli.py heatmap          # Interactive order book heatmap
python research_cli.py replay --minutes 60  # Replay through analytics
python research_cli.py backtest --minutes 120  # Signal accuracy testing
python research_cli.py export --output-dir exports/  # CSV/JSON export
```

---

## Configuration

Edit `config/settings.py` for static thresholds (requires restart):
- Detection: `WALL_STD_MULTIPLIER`, `WHALE_THRESHOLD_SIZE`, `SPOOF_MIN_SIZE`
- Windows: `FLOW_WINDOW_SECONDS`, `ABSORPTION_WINDOW_SECONDS`
- Intelligence: `CVD_DIVERGENCE_THRESHOLD`, `VEGAS_FLASH_THRESHOLD`, `LIQUIDITY_VOID_THRESHOLD`

Edit `config/strategy.conf` for hot-reloadable trading parameters (no restart needed).

---

## Tech Stack

- **Python 3.10+** with async/await throughout
- **WebSockets** for real-time order book streaming
- **Pydantic** for data validation and serialization
- **Rich** for the terminal dashboard
- **NumPy** for numerical computations
- **aiosqlite** for async database operations
- **httpx** for async HTTP requests

---

## Troubleshooting

**"No markets found"** → Use broader search terms. Polymarket's search is basic.

**"WebSocket disconnected"** → Normal — auto-reconnects with exponential backoff.

**Terminal looks garbled** → Widen to 100+ columns. Use a modern terminal (iTerm2, Windows Terminal).

**"ModuleNotFoundError"** → Activate your venv: `source venv/bin/activate`
