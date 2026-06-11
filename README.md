# Omar-Poly-v2 — Polymarket Trading & Intelligence

Async Python toolkit for Polymarket prediction markets. Two distinct products
ship from the same repo — pick the one you want and jump straight to its
flow below.

> **Two products, one repo**
>
> 1. **OBI Intelligence Dashboard** — read-only real-time market microstructure
>    analytics. No credentials, no money at risk.
> 2. **Pair Trading Bot** — YES+NO accumulation across BTC / ETH / SOL / XRP
>    Up/Down markets, with paper, dry-run, and live modes.

---

## Install (shared)

```bash
git clone https://github.com/omarseifelsadek-creator/Omar-Poly-v2.git
cd Omar-Poly-v2

python3 -m venv venv
source venv/bin/activate          # Mac/Linux
# venv\Scripts\activate            # Windows

pip install -r requirements.txt
```

Python 3.10+ is required.

---

# Product 1 — OBI Intelligence Dashboard

Real-time market microstructure intelligence. Watches a Polymarket order book
and surfaces what matters — manipulation, whale activity, structural shifts —
so you don't have to stare at raw numbers.

Instead of scrolling through a ladder of prices, you get:

- "🏦 INSTITUTIONAL: Wall at 0.30 reloaded 3x while absorbing 15K contracts"
- "⚡ Aggressive buyer swept 4 levels 0.03→0.07 (12K contracts)"
- "⚠️ DIVERGENCE: Price rising but CVD falling — sellers absorbing buying momentum"
- "👻 Possible spoof at 0.58 ask — 4 oscillations, peak 8K"
- "🕳️ Flash Zone: Thin liquidity at 0.48 (ask) — 6% of avg depth"
- "🐋 Whale Buy (taker): 23K contracts @ 0.03 ($690)"
- "🛡️ Absorption @ 0.55 (bid) — wall held 92% after 5 trades"

**No credentials required.** All Polymarket order book data is public.

## Quick Start

```bash
# Pair trading — interactive menu (the default)
python3 main.py

# Intelligence dashboard on any market's token
python3 main.py --token <TOKEN_ID>
```

## Recording Mode

Record L2 order book snapshots + trades to CSV for backtesting — no trading,
no dashboard:

```bash
python3 main.py --record
python3 main.py --record --asset btc --timeframe 5m
```

Output lands in `data/logs/l2_*.csv`.

## BTC 5-Minute Auto-Rotate (Intelligence only)

Watch the current BTC 5-minute Up/Down market and automatically rotate to the
next window when it expires:

```bash
python3 main.py --btc5m
python3 main.py --btc5m --btc5m-side auto      # default
python3 main.py --btc5m --btc5m-side up
python3 main.py --btc5m --btc5m-side down
```

## The Dashboard

```
┌──────────────────────────────────────────────────────────────┐
│ OBI │ Market Question │ [YES] │ REGIME │ Wt.Mid │ Spd │ ●12ms│
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

## Key Metrics

| Metric | What It Means |
|--------|--------------|
| **OBI** | Order Book Imbalance — bids / total (>60% = bullish) |
| **Weighted Midpoint** | VWAP-weighted mid, more accurate than (bid+ask)/2 |
| **OBI Velocity** | Rate of change — STACKING (book building) / PULLING |
| **CVD** | Cumulative Volume Delta — net market aggression |
| **CVD Divergence** | Price and CVD moving opposite directions (often precedes reversals) |
| **Flow Pressure** | Aggressive buyer/seller ratio (-1 to +1) |
| **Sentiment** | Composite score across all phases (-1 to +1) |
| **Regime** | TRENDING_UP/DOWN, RANGING, VOLATILE, BREAKOUT, QUIET |
| **Liquidity Voids** | Flash Zones — depth < 10% of average |
| **Vegas Flash** | Rapid size changes without trades (cyan = stacking, red = pulling) |
| **Walls** | Abnormally large orders acting as support/resistance |
| **Spoofing** | Rapid appear/disappear cycles at a price level |
| **Absorption** | Wall holding its size while being hit |
| **Institutional Absorption** | Wall that reloads after being consumed (likely algo) |
| **Sweeps** | Aggressive orders eating through multiple levels |

---

# Product 2 — Pair Trading Bot

YES+NO accumulation strategy for Polymarket's binary Up/Down crypto markets.
The bot opportunistically fills both sides of a market below the combined
breakeven price, so every settled pair is guaranteed profit net of fees.

Supported markets (verified live on Polymarket):

| Asset | 5m | 15m |
|-------|----|-----|
| BTC   | ✅ | ✅  |
| ETH   | ✅ | ✅  |
| SOL   | ✅ | ✅  |
| XRP   | ✅ | ✅  |

> Polymarket previously offered 1-hour Up/Down markets but has deprecated
> them — do not pass `--timeframe 1h` (it's no longer an accepted choice).

## Flow 1: First-Time Setup

**1. Install** — see the [Install](#install-shared) section above.

**2. Create your `.env` file** using the template:

```bash
cp .env.example .env
```

Then edit `.env` and fill in your Polymarket credentials:

```
POLY_PRIVATE_KEY=your-eoa-private-key-here
POLY_FUNDER=your-proxy-wallet-address-here
POLY_API_KEY=your-clob-api-key-here
POLY_API_SECRET=your-clob-api-secret-here
POLY_API_PASSPHRASE=your-clob-api-passphrase-here

# Optional Telegram alerts
OBI_TELEGRAM_TOKEN=
OBI_TELEGRAM_CHAT_ID=
```

> **Paper mode does NOT require any of these.** You only need credentials for
> `--mode dry-run` and `--mode live`.

**3. First run — paper mode** (no credentials needed):

```bash
python3 main.py --pairs
```

You'll get an interactive menu to pick asset, timeframe, and mode.

**4. Validate — dry-run mode** (signs orders locally but never posts them):

```bash
python3 main.py --pairs --mode dry-run
```

**5. Go live** (real orders, real money):

```bash
python3 main.py --pairs --mode live
```

⚠️ **Never jump straight to `--mode live` without paper + dry-run validation.**

## Flow 2: Quick Run (Power User)

Skip the interactive menu entirely by passing all three args:

```bash
# Paper (default)
python3 main.py --pairs --asset btc --timeframe 5m
python3 main.py --pairs --asset eth --timeframe 15m
python3 main.py --pairs --asset sol --timeframe 5m
python3 main.py --pairs --asset xrp --timeframe 15m

# Dry-run (sign only, never post)
python3 main.py --pairs --asset btc --timeframe 5m --mode dry-run

# Live (real orders)
python3 main.py --pairs --asset btc --timeframe 5m --mode live
```

**Accepted values**

| Flag         | Choices                     |
|--------------|-----------------------------|
| `--asset`    | `btc`, `eth`, `sol`, `xrp`  |
| `--timeframe`| `5m`, `15m`                 |
| `--mode`     | `paper` (default), `dry-run`, `live` |

## Headless — both BTC timeframes in parallel

No dashboard, no interactive menu, CSV logging only. Runs BTC 5m and BTC 15m
side-by-side — ideal for long unattended sessions:

```bash
python3 main.py --headless
```

Keep your laptop awake on macOS:

```bash
cd ~/Desktop/Omar-Poly-v2 && git pull && caffeinate -dims python3 main.py --headless
```

## Kill Switch

Stop trading automatically after losing a dollar threshold:

```bash
python3 main.py --pairs --max-loss 50
python3 main.py --pairs --asset btc --timeframe 5m --mode live --max-loss 25
```

---

## Architecture

### Data Pipeline
```
WebSocket msg
  → message_parser
  → OrderBook + LevelTracker (state/)
  → compute_all_metrics + MomentumEngine (analytics/)
  → generate_insights (analytics/interpreter.py)
  → StrategyEngine.evaluate (execution/strategy.py)
  → Terminal Dashboard (ui/terminal.py) + Telegram + CSV + SQLite
```

### Three-Phase Analytics
1. **Phase 1 — Snapshots**: OBI, walls, whales, flow pressure, base sentiment
2. **Phase 2 — Time-series patterns**: Spoofing, Absorption, Sweeps
3. **Phase 3 — Momentum & Regime**: EMA, volatility, regime with hysteresis

### Project Structure
```
├── main.py                  # Entry point — argparse + dispatcher
├── config/
│   ├── settings.py          # Static thresholds (restart to apply)
│   └── strategy.conf        # Hot-reloadable trading parameters
├── data/                    # Models, HTTP client, WebSocket, parser
├── state/                   # OrderBook, LevelTracker
├── analytics/               # metrics, detectors, momentum, signals
├── execution/               # strategy, executor, pair_*, market_rotator
├── storage/                 # Async SQLite
├── ui/                      # Rich terminal dashboard
├── telegram/                # Alert notifications
└── research/                # Offline heatmap, replay, backtest
```

---

## Research Tools

```bash
python3 tools/research_cli.py summary
python3 tools/research_cli.py heatmap
python3 tools/research_cli.py replay --minutes 60
python3 tools/research_cli.py backtest --minutes 120
python3 tools/research_cli.py export --output-dir exports/
```

---

## Configuration

- **`config/settings.py`** — static thresholds (`WALL_STD_MULTIPLIER`,
  `WHALE_THRESHOLD_SIZE`, `SPOOF_MIN_SIZE`, window sizes, etc.).
  Restart required.
- **`config/strategy.conf`** — hot-reloadable trading params (sizing,
  risk limits, strategy toggles). No restart needed.

---

## Tech Stack

- Python 3.10+ with async/await throughout
- WebSockets for real-time order book streaming
- Pydantic for external data, dataclasses for internal state
- Rich for the terminal dashboard
- NumPy, httpx, aiosqlite

---

## Troubleshooting

**`No market found after retry.`** (pair trading)
The bot now prints the exact Gamma API URL, HTTP status, and response body
snippet before exiting. Common causes:
- You asked for a timeframe that Polymarket no longer offers (e.g. `1h`).
- Network block between you and `gamma-api.polymarket.com`.
- The current window's market hasn't been published yet — wait a few seconds
  and retry.

**`ModuleNotFoundError` for `websockets` / `rich` / `pydantic`**
Activate your venv: `source venv/bin/activate`, then
`pip install -r requirements.txt`.

**`POLY_PRIVATE_KEY not set` in live/dry-run mode**
Your `.env` is missing or not being loaded. Check that `.env` exists at the
repo root and contains all `POLY_*` variables. Paper mode does not need any
of these.

**WebSocket disconnected**
Normal — the client auto-reconnects with exponential backoff. Don't add
aggressive retries.

**Terminal looks garbled**
Widen to 100+ columns. Use a modern terminal (iTerm2, Windows Terminal).
