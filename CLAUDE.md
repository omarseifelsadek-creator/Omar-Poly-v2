# OBI — Polymarket Order Book Intelligence

Async Python trading bot for prediction markets. Real-time order book analysis
via WebSocket, multi-phase signal generation, paper/dry-run/live execution.

## Session Protocol

**Session start:** read `docs/HANDOFF.md`, then the "Now" section of `docs/BACKLOG.md`, before doing anything else.

**During work:** reference backlog IDs (B1, B2, …) in commit messages. Before touching strategy parameters, read the relevant registry entry in `docs/STRATEGY_LOG.md`.

**Session end, after any major milestone, or when Omar says "handoff":** rewrite `docs/HANDOFF.md` (overwrite, never append, ≤60 lines), tick/move BACKLOG items, append a STRATEGY_LOG experiment entry if an experiment concluded. Refresh proactively after milestones — sessions often end by abandonment, not cleanly. Commit HANDOFF.md together with the work it describes.

### Docs Map

| File | Purpose | Update when |
|------|---------|-------------|
| `docs/HANDOFF.md` | Where things stand right now | Every session (overwrite) |
| `docs/BACKLOG.md` | Prioritized work items, stable IDs | Whenever work happens |
| `docs/STRATEGY_LOG.md` | Strategy registry + experiment lab notebook | Per experiment |
| `docs/RUNBOOK.md` | Paper→dry-run→live procedure, recovery | Procedure changes only |
| `docs/AUDIT-2026-06-10.md` | Frozen audit snapshot (evidence for backlog) | Never |

## How to Run

```bash
source venv/bin/activate

# Interactive market selector
python main.py

# Direct market connection
python main.py --token <TOKEN_ID>
python main.py --slug <market-slug>
python main.py --search "keyword"

# BTC 5-minute auto-rotating
python main.py --btc5m [--btc5m-side auto|up|down]

# Pair trading (YES+NO accumulation)
python main.py --pairs [--asset btc|eth|sol|xrp] [--timeframe 5m|15m]

# Headless (no dashboard, CSV logging only, both BTC timeframes: 5m + 15m)
python main.py --headless

# Record L2 snapshots for backtesting
python main.py --record

# Trading mode (default: paper)
python main.py --mode paper|dry-run|live

# Kill switch
python main.py --max-loss 50
```

## Data Pipeline

```
WebSocket msg
  -> message_parser
  -> OrderBook + LevelTracker (state/)
  -> compute_all_metrics + MomentumEngine (analytics/metrics.py)
  -> generate_insights (analytics/interpreter.py)
  -> generate_signals (analytics/signals.py)
  -> StrategyEngine.evaluate (execution/strategy.py)
  -> UI (ui/terminal.py) + Telegram + CSV + SQLite
```

## Module Map

| Directory       | Responsibility                                             |
|-----------------|------------------------------------------------------------|
| `config/`       | `settings.py` (static thresholds), `strategy.conf` (hot-reloadable trading params), `live_config.py` (conf loader) |
| `data/`         | `models.py` (Pydantic models), `rest_client.py` (Gamma/CLOB HTTP), `websocket_client.py` (WS stream), `message_parser.py` |
| `state/`        | `orderbook.py` (bid/ask state + trade history), `level_tracker.py` (per-level time-series for Phase 2) |
| `analytics/`    | `metrics.py` (Phase 1-3 computation), `detectors.py` (spoofing/absorption/sweep), `momentum.py` (EMA/regime), `interpreter.py` (insights), `signals.py` (trade signals) |
| `execution/`    | `strategy.py` (signal evaluation + position management), `executor.py` (order placement), `market_rotator.py` (side switching), `pair_*.py` (pair trading), `trade_logger.py` |
| `ui/`           | `terminal.py` (Rich dashboard)                            |
| `telegram/`     | `telegram_bot.py` (alert notifications)                    |
| `storage/`      | `database.py` (async SQLite via queue)                     |
| `research/`     | Research CLI and backtesting tools                         |

## Key Design Patterns

- **Analytics = pure functions**: metrics/detectors take state, return results, no side effects
- **Pydantic for external data** (API responses), **dataclasses for internal mutable state** (OrderBook)
- **Signal/insight dedup**: `_should_emit(key, cooldown)` pattern — module-level state, resets on restart
- **Two config systems**:
  - `config/settings.py` — static thresholds, requires restart
  - `config/strategy.conf` — INI format, hot-reloadable (sizing, risk, strategy toggles)
- **All magic numbers** live in `config/settings.py`
- **Async everywhere** — never block the event loop
- **Background DB writes** via asyncio queue

## The Metrics Object

Central data structure flowing through the entire pipeline (`analytics/metrics.py` -> `data/models.py`).

**Field groups**:
- **Order book**: OBI (0-1), VWAP midpoint, spread, best bid/ask
- **Imbalance**: bid/ask depth ratio, depth at N levels
- **Flow**: buy/sell volume (120s rolling), flow pressure (-1 to +1)
- **Detections**: walls (WallInfo[]), whales (WhaleEvent[]), spoofing, absorption, sweeps
- **Momentum** (Phase 3): price/OBI/flow EMA + velocity, depth divergence
- **Regime**: TRENDING_UP/DOWN, RANGING, VOLATILE, BREAKOUT, QUIET + confidence
- **Composite**: sentiment_v3 (-1 to +1), blending all phases with configurable weights

## Three-Phase Analytics

1. **Phase 1 — Snapshots**: OBI, walls, whales, flow pressure, base sentiment
2. **Phase 2 — Time-series patterns**: Spoofing (oscillations), Absorption (wall holding), Sweeps (level consumption)
3. **Phase 3 — Momentum & Regime**: EMA smoothing, volatility tracking, market regime detection with hysteresis

## External APIs

| API           | Base URL                               | Auth | Purpose              |
|---------------|----------------------------------------|------|----------------------|
| Gamma API     | `https://gamma-api.polymarket.com`     | None | Market discovery     |
| CLOB REST     | `https://clob.polymarket.com`          | None | Order book polling   |
| CLOB WebSocket| `wss://ws-subscriptions-clob.polymarket.com/ws/market` | None | Real-time book stream |
| Telegram      | Bot API                                | Token| Alert notifications  |

**Gamma API caveat**: No reliable server-side text search. `_q`, `question_contains`, `title_contains` params don't actually filter. Client-side filtering required.

## Environment Variables

- `OBI_TELEGRAM_TOKEN` — Telegram bot token (optional)
- `OBI_TELEGRAM_CHAT_ID` — Telegram chat ID (optional)
- No auth needed for read-only market data APIs (paper mode needs no credentials)
- **Live/dry-run trading** requires five vars in `.env` (see `.env.example` + `docs/RUNBOOK.md` §2):
  `POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`, `POLY_FUNDER`
- Live/dry-run also requires the `py-clob-client` package (in requirements.txt); paper mode runs without it

## Common Tasks

| Task                    | Where to change                                              |
|-------------------------|--------------------------------------------------------------|
| New detection pattern   | `analytics/detectors.py` -> add to `data/models.py` -> wire in `analytics/metrics.py` -> interpret in `analytics/interpreter.py` -> signal in `analytics/signals.py` |
| New metric              | `data/models.py` (add field) -> `analytics/metrics.py` (compute) -> `ui/terminal.py` (display) |
| Strategy behavior       | `config/strategy.conf` (hot) or `execution/strategy.py` (code) |
| New CLI mode            | `parse_args()` in `main.py` + new `async run_*()` function  |
| Tune sensitivity        | `config/settings.py` (thresholds section)                    |
| New asset for pairs     | `execution/pair_runner.py` (add slug pattern)                |

## Critical Warnings

- **NEVER** use `--mode live` without paper testing first
- **strategy.conf is largely IGNORED in `--pairs` mode** — it configures the intelligence dashboard
  (`StrategyEngine`); pair-trading params are hardcoded in `execution/pair_runner.py` (`PairConfig`).
  Tuning the conf and expecting pairs behavior to change is a trap (backlog B12).
- **Polymarket fee curve**: `price * (1 - price) * 0.0625` (~3.12% round-trip at 50c)
- **WebSocket reconnection** uses exponential backoff — don't add aggressive retries
- **Signal/insight dedup** is module-level state: persists within session, resets on restart
- **No tests exist yet** — analytics pure functions are the ideal starting point for TDD
- **strategy.conf** is read by `LiveConfig` at runtime — invalid INI will crash the config loader
