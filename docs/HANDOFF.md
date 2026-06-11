# Session Handoff

> Claude: read this FIRST each session. Overwrite (don't append) at session end or after any
> major milestone. Keep under 60 lines — git history is the archive.

**Updated:** 2026-06-10 (23:00, end of revamp marathon) · **Branch:** main · **No runs active**

## Current Focus

Launcher landed: `python main.py` = main menu (Pair Trading / Order Book Analysis / Data Recorder).
**Order Book Analysis is a PLACEHOLDER — Omar wants to design it tailor-made, together, next session.**

## State of the World

- **Main menu** (`modes/launcher.py`): bot registry pattern — future bots (weather bot is being
  researched) register one `BotEntry` in `launcher.BOTS` and appear automatically. Ctrl+C in a bot
  returns to menu; q/Ctrl+C at menu exits. Live always passes the typed-`yes` gate.
- Pair Trading submenu: Paper / Dry-run / Live / Headless (headless then asks paper-or-live).
- DELETED today: synthetic engine (`ui/cyber_*`), `--btc5m` + `modes/btc5m.py`, `modes/select.py`
  (launcher owns all menus now), `--slug`/`--search` args.
- CLI flags = scripted bypass (`--headless`, `--pairs --asset --timeframe`, `--token`, `--record`).
- Suite 82 green, ruff clean. **v15 paper baseline: +$11.69/window, std $21.46, n=18** (EXP-002,
  STRATEGY_LOG Part 2). All backlog B-items done except P2 leftovers (B13-residual, B20, B21).

## Next Steps (in order)

1. **Design Order Book Analysis with Omar** (tailor-made — do NOT build without his input).
   Stub: `modes/launcher.py:_order_book_analysis_flow`. Raw materials: `RestClient.get_active_markets`
   (top by 24h volume) + `search_markets` + `get_market_by_slug` (data/rest_client.py), OBIApp
   (modes/intelligence.py). His stated shape: filter by top markets / keyword / slug → dashboard.
2. EXP-002b: grow the baseline sample (`--headless` paper, stock conf, different times of day; pool with n=18, aim n≥50).
3. EXP-003: atomic_entry_max_pair / max_pair_cost frontier probe (60% rejections, mostly atomic_entry_too_wide).
4. Weather-market bot (Omar researching) → will register as a launcher BotEntry when ready.

## Watch Out

- The pairs menu default in the launcher is Paper — Live and headless-live both gate on typed `yes`.
- Paper-vs-live fill gap still unmeasured — RUNBOOK ladder before any live resumption.
- `tools/` scripts run from repo root (`python tools/research_cli.py summary`).

## Open Questions (for Omar)

- Order Book Analysis design session — his requirements first.
