# Backlog

> One line per item. IDs are permanent — reference them in commits ("fixes B1") and in HANDOFF.md.
> When done: tick it, move the line to Done with the date. Detail lives in [AUDIT-2026-06-10.md](AUDIT-2026-06-10.md), not here.

## Now (P0)

- [ ] Order Book Analysis menu flow — design TAILOR-MADE with Omar (placeholder at `modes/launcher.py:_order_book_analysis_flow`; raw materials: get_active_markets / search_markets / get_market_by_slug + OBIApp).

- [ ] EXP-002b · Extend the baseline across different times of day (same v15 params — pool with EXP-002's n=18). Just rerun `--headless` paper; windows self-stamp params now.
- [ ] EXP-003 · Param probe from rejection analysis: `atomic_entry_max_pair` and `max_pair_cost` frontier (see EXP-002 follow-up in STRATEGY_LOG).

## Next (P1)

- (empty — next work comes from EXP-002 results)

## Later (P2 — structural)

- [ ] B13-residual · `pair_runner.py` still ~1230 lines after extracting ChainlinkTracker + resolution chain — further decomposition (session stats/reporting) when convenient.
- [ ] B20 · Unify the duplicated `_should_emit` dedup logic in `interpreter.py`/`signals.py` into a shared helper; needs dedup-behavior tests first.
- [ ] B21 · Momentum regime thresholds still hardcoded in `momentum.py:_detect_regime` (audit S4) — move to settings when regime tuning becomes a need.

## Ideas (unprioritized parking lot — new-strategy candidates graduate to STRATEGY_LOG)

- (empty — new strategy ideas land here first)

## Done

- [x] 2026-06-10 · Launcher · `python main.py` = main menu w/ bot registry (Pair Trading submenu: paper/dry-run/live/headless; Data Recorder; Order Book Analysis placeholder). Ctrl+C returns to menu. Deleted: --btc5m + modes/btc5m.py, modes/select.py. Flags remain the scripted bypass.

- [x] 2026-06-10 · Synthetic Market Microstructure Engine DELETED (~1,330 lines: ui/cyber_engine, ui/cyber_dashboard, --slug/--search args, synthetic-only pickers). No-flag default is now the pair-trading menu. Menu-chosen live mode now passes the typed-yes gate (was a bypass).

- [x] 2026-06-10 · EXP-002 · v15 paper baseline CLOSED: **+$11.69/window avg, std $21.46, 61% win rate, n=18** (BTC 5m+15m, 3.5h evening session). Full entry in STRATEGY_LOG Part 2.

- [x] 2026-06-10 · Cleanup sweep · ruff adopted (pyproject.toml, requirements-dev.txt), 113 findings → 0; dead `rotate_early` key removed; lost 🎯 snipe marker restored to BUY output; analysis scripts → `tools/` (test_dashboard → demo_dashboard); RUNBOOK stale B7/B8/B12 claims fixed.
- [x] 2026-06-10 · B19 · `--token` rewired to launch OBIApp directly (was parsed-but-dead); 171 lines of dead selectors removed from modes/select.py.
- [x] 2026-06-10 · B17 · `obi_velocity_5s/30s` kept deliberately — documented as "computed, awaiting consumer" in models.py + metrics.py for new-strategy work.
- [x] 2026-06-10 · B12 · `[pairs]` section in strategy.conf drives PairConfig: per-window reload (never mid-window), active set stamped to `pair_params_*.csv` sidecar, v15 fallbacks, typo'd-key warnings. Experiments = edit conf, no code changes.
- [x] 2026-06-10 · B13 · main.py 1144→325 lines (modes/ package: intelligence, select, btc5m); pair_runner 1393→~1230 (ChainlinkTracker → chainlink_feed.py, resolution chain → window_settler.py). Residual tracked above.
- [x] 2026-06-10 · B14 · 46 analytics characterization tests (metrics/detectors/momentum/CVD) + hermetic conftest (suite can't touch data/logs). 74 tests total.
- [x] 2026-06-10 · B9 · WebSocket failures classified: 401/403/429 handshake rejections log loudly + jump to max backoff; closes/network errors keep normal backoff; callback errors get tracebacks.
- [x] 2026-06-10 · B16 · DB queue overflow: dropped-write counter, warning throttled to 1/min, total surfaced in get_stats + close summary.
- [x] 2026-06-10 · B18 · Settlement under Ctrl+C prints "settling… Ctrl+C again to force" and caps Gamma polling at 60s (book fallback still settles).
- [x] 2026-06-10 · B7 · Fill-ambiguity reconciliation: ambiguous live submissions poll own trade history — adopt confirmed fills, else rollback + halt window. Message-loop crashes now logged loudly + end window after 3 no-progress crashes.
- [x] 2026-06-10 · B8 · Kill switch checked pre-entry (worst-case projection incl. unmatched exposure), warns at 80%, single shared budget across headless runners (was per-runner).
- [x] 2026-06-10 · B15 · CLOB rejection logs now include full response repr + token id.
- [x] 2026-06-10 · B1 · Fill logging restored (dedent out of rejection branch) + regression test. Zone else-branch "Panic"→"Dead".
- [x] 2026-06-10 · B2 · `record_trade_at_level` unused `side` param removed (tracker derives side; behavior was already correct).
- [x] 2026-06-10 · B3 · requirements.txt: added py-clob-client, openpyxl.
- [x] 2026-06-10 · B4 · Dedup dicts bounded (expired-entry pruning past 500 keys).
- [x] 2026-06-10 · B5 · Eager .env validation at LiveExecutor init; fixed load_dotenv("env") pointing at the venv dir.
- [x] 2026-06-10 · B6 · Typed-`yes` live-mode confirmation gate + `--yes` flag.
- [x] 2026-06-10 · B10 · Single venv (`env/` canonical, `venv/` deleted); stale branches + worktree pruned.
- [x] 2026-06-10 · B11 · `quant_dashboard.py`, `generate_report.py` retired to `research/legacy/`.
