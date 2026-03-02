# Polymarket Order Book Intelligence (OBI) — v2.0

A real-time market microstructure analysis engine for Polymarket prediction markets.

**OBI monitors the Polymarket order book and tells you what's happening in plain English.**

Instead of staring at raw numbers, you get insights like:
- "Strong buy wall at 0.62 absorbing sell pressure"
- "Liquidity thinning on ask side → bullish bias"
- "🐋 Whale BUY: 10,000 contracts @ 0.55 ($5,500)"
- "⚠️ Spoofing signal at 0.58: 4 rapid appear/disappear cycles in 60s"
- "🛡️ Support absorption at 0.55: wall absorbed 5 sell trades — still holding 92%"
- "🔥 BUY sweep ↑: aggressive order ate through 4 levels (0.52 → 0.55)"
- "Spread widening → uncertainty increasing"

---

## Quick Start (5 minutes)

### Step 1: Install Python
You need Python 3.10 or higher. Check your version:
```bash
python --version
# or
python3 --version
```

If you don't have Python, download it from: https://www.python.org/downloads/
- **Mac**: `brew install python` (if you have Homebrew)
- **Windows**: Download the installer and CHECK "Add to PATH" during install
- **Linux**: `sudo apt install python3 python3-pip python3-venv`

### Step 2: Set Up the Project
```bash
# Navigate to the project folder
cd polymarket-obi

# Create a virtual environment (keeps dependencies isolated)
python3 -m venv venv

# Activate the virtual environment
# Mac/Linux:
source venv/bin/activate
# Windows:
venv\Scripts\activate

# You should see (venv) at the start of your terminal prompt

# Install dependencies
pip install -r requirements.txt
```

### Step 3: Run
```bash
python main.py
```

This opens an interactive market selector where you can:
1. **Search** for any Polymarket market by keyword
2. **Browse** the most active markets
3. **Enter** a specific token ID or URL slug

### Alternative: Direct Launch
```bash
# Search for a market
python main.py --search "bitcoin"

# Use a Polymarket URL slug (from the market page URL)
python main.py --slug will-btc-hit-100k-in-2025

# Use a token ID directly (for advanced users)
python main.py --token 6581861965756881347434186865230894207980491928738042219289221113140879312542
```

---

## How to Find a Market's Token ID

1. Go to [polymarket.com](https://polymarket.com)
2. Find a market you want to analyze
3. Look at the URL: `https://polymarket.com/event/some-market-slug`
4. Use the slug: `python main.py --slug some-market-slug`

The app will automatically look up the token ID for you.

---

## Understanding the Terminal Dashboard

```
╔══════════════════════════════════════════════════════════════════════╗
║  POLYMARKET OBI — "Will X happen by Y?"                            ║
╠══════════════════════════════════════════════════════════════════════╣
║  ORDER BOOK          │  METRICS                                     ║
║  (bid/ask ladder)    │  (imbalance, spread, flow, sentiment)        ║
║                      │                                              ║
║  LIVE INSIGHTS                                                      ║
║  (natural language explanations of what's happening)                ║
║                                                                     ║
║  TRADE TAPE                                                         ║
║  (recent trades with price, size, direction)                        ║
╚══════════════════════════════════════════════════════════════════════╝
```

### Key Metrics Explained

| Metric | What It Means |
|--------|--------------|
| **Imbalance** | Ratio of bids to total liquidity (>60% = bullish) |
| **Spread** | Gap between best bid and ask (tight = confident market) |
| **Flow Pressure** | Are aggressive traders buying or selling? (-1 to +1) |
| **Sentiment** | Composite score combining all signals (-1 to +1) |
| **VWAP Mid** | Volume-weighted midpoint (more accurate than simple mid) |
| **Walls** | Abnormally large orders acting as support/resistance |
| **⚠️ Spoof** | Rapid appear/disappear cycles at a price level (manipulation) |
| **🛡️ Absorb** | Wall holding its size while trades hit it (strong conviction) |
| **🔥 Sweep** | Aggressive order eating through multiple levels (urgency) |

### Color Coding
- 🟢 **Green** = Bids / Bullish signals
- 🔴 **Red** = Asks / Bearish signals
- 🟡 **Yellow** = Alerts / Important changes
- ⚪ **Gray** = Neutral / Informational

---

## Project Structure

```
polymarket-obi/
├── main.py                  # Live OBI entry point
├── research_cli.py          # Offline research tools CLI (Phase 4)
├── config/
│   └── settings.py          # All tunable parameters
├── data/
│   ├── models.py            # Data types (OrderLevel, Trade, Metrics, etc.)
│   ├── message_parser.py    # Raw JSON → typed Python objects
│   ├── rest_client.py       # HTTP client for market discovery
│   └── websocket_client.py  # Real-time WebSocket connection
├── state/
│   ├── orderbook.py         # In-memory order book state manager
│   └── level_tracker.py     # Per-price-level history (Phase 2)
├── analytics/
│   ├── metrics.py           # Quantitative metric calculations
│   ├── detectors.py         # Spoofing, absorption, sweep detection (Phase 2)
│   ├── momentum.py          # EMA tracking, regime detection (Phase 3)
│   └── interpreter.py       # Metrics → natural language insights
├── storage/
│   └── database.py          # Async SQLite storage (Phase 2)
├── research/                # Phase 4: Offline analysis
│   ├── heatmap.py           # Interactive HTML order book heatmap
│   ├── replay.py            # Historical data replay engine
│   ├── backtest.py          # Signal accuracy backtesting
│   └── export.py            # CSV/JSON data export
├── ui/
│   └── terminal.py          # Rich terminal dashboard
├── requirements.txt         # Python dependencies
└── README.md                # This file
```

---

## Phase 2 Features

### Spoofing Detection
Identifies orders that rapidly appear and disappear — a classic market manipulation pattern. The detector tracks oscillation cycles at each price level and flags suspicious activity.

### Absorption Analysis
Detects when a large wall holds its size while being hit by trades. This means someone is actively refilling the wall — a strong signal of conviction by a large participant.

### Sweep Detection
Identifies aggressive orders that eat through multiple price levels at once. This signals extreme urgency — someone wants in/out regardless of price.

### Passive Whale Detection
Spots large limit orders suddenly appearing deep in the book. Unlike aggressive whale trades, these are quiet positioning by institutional-size participants.

### SQLite Storage
All trades, metrics, order book snapshots, and detected events are persisted to a local SQLite database (`data/obi.db`) for future research and backtesting. Disable with `STORAGE_ENABLED = False` in settings.

### Querying Your Data
```python
import sqlite3
conn = sqlite3.connect("data/obi.db")

# See all whale/spoof/absorption events
for row in conn.execute("SELECT * FROM events WHERE event_type IN ('spoof','absorption','sweep') ORDER BY timestamp_ms DESC LIMIT 20"):
    print(row)

# Get metrics time series
for row in conn.execute("SELECT timestamp_ms, obi, spread, sentiment FROM metrics ORDER BY timestamp_ms DESC LIMIT 50"):
    print(row)
```

---

## Configuration

Edit `config/settings.py` to tune:

- **Detection sensitivity**: `WALL_STD_MULTIPLIER`, `WHALE_THRESHOLD_SIZE`, `SPOOF_MIN_SIZE`
- **Analysis window**: `FLOW_WINDOW_SECONDS`, `OB_IMBALANCE_LEVELS`, `ABSORPTION_WINDOW_SECONDS`
- **Spoofing**: `SPOOF_OSCILLATION_THRESHOLD`, `SPOOF_WINDOW_SECONDS`
- **Absorption**: `ABSORPTION_TRADE_COUNT_THRESHOLD`, `ABSORPTION_SIZE_TOLERANCE`
- **Sweeps**: `SWEEP_MIN_LEVELS`, `SWEEP_WINDOW_SECONDS`
- **Storage**: `STORAGE_ENABLED`, `DB_PATH`, `SNAPSHOT_INTERVAL_SECONDS`
- **Display**: `OB_DISPLAY_LEVELS`, `UI_MAX_INSIGHTS`, `UI_REFRESH_RATE`
- **WebSocket**: Reconnection delays, ping intervals

---

## Troubleshooting

**"No markets found"**
→ Try broader search terms. Polymarket's search is basic — use single keywords.

**"WebSocket disconnected"**
→ Normal! The app auto-reconnects. Check your internet connection if it persists.

**Terminal looks garbled**
→ Make your terminal window wider (at least 100 columns). Use a modern terminal
  (iTerm2 on Mac, Windows Terminal on Windows, any modern Linux terminal).

**"ModuleNotFoundError"**
→ Make sure your virtual environment is activated: `source venv/bin/activate`

---

## What's Next (Roadmap)

- ~~**Phase 1**: Live order book + basic metrics~~ ✅
- ~~**Phase 2**: Spoofing detection, absorption analysis, sweep detection, SQLite storage~~ ✅
- ~~**Phase 3**: Momentum tracking, regime detection, volatility estimation, advanced sentiment~~ ✅
- ~~**Phase 4**: Order book heatmap, historical replay, backtesting, CSV/JSON export~~ ✅
- **Phase 5**: Signal abstraction, paper trading, alert system (Telegram/Discord), bot foundation

---

## Phase 4: Research Tools

Run offline analysis on stored data after a trading session:

```bash
# Show what's in the database
python research_cli.py summary

# Generate interactive order book heatmap (opens in browser)
python research_cli.py heatmap
python research_cli.py heatmap --minutes 120    # Last 2 hours

# Replay data through analytics pipeline
python research_cli.py replay --minutes 60

# Backtest signal accuracy (the most powerful tool)
python research_cli.py backtest --minutes 120
python research_cli.py backtest --minutes 120 --export report.json

# Export data for external tools (Jupyter, Excel, etc.)
python research_cli.py export --minutes 60 --output-dir my_exports/
```

### Backtest Report Example
```
══════════════════════════════════════════════════════════════════
  BACKTEST REPORT
  Duration: 120.0 minutes | Total signals: 847
══════════════════════════════════════════════════════════════════
Signal               | Count |    5s   30s   60s  5min | Avg 60s Return
──────────────────────────────────────────────────────────────────
sweep_bullish        |    12 | 75.0% 66.7% 58.3% 50.0% |      +0.0034
absorption_bullish   |    23 | 65.2% 60.9% 56.5% 52.2% |      +0.0021
obi_bullish          |   156 | 58.3% 55.1% 53.2% 51.3% |      +0.0012
sentiment_bullish    |   203 | 56.2% 54.7% 52.7% 50.2% |      +0.0008
══════════════════════════════════════════════════════════════════
```

---

## No API Key Required

Reading the Polymarket order book is completely public and unauthenticated.
You don't need a wallet, API key, or any account to use OBI for analysis.
