# Backlog

> One line per item. IDs are permanent — reference them in commits ("fixes B1") and in HANDOFF.md.
> When done: tick it, move the line to Done with the date. Detail lives in [AUDIT-2026-06-10.md](AUDIT-2026-06-10.md), not here.

## Now (P0)

- [ ] EXP-002 · Re-baseline PAIRS-v15: multi-hour paper run (headless 5m+15m), close entry in STRATEGY_LOG with net_pnl/window, pairs/window, rejection_rate.

## Next (P1)

- (empty — next work comes from EXP-002 results)

## Later (P2 — structural)

- [ ] B13-residual · `pair_runner.py` still ~1230 lines after extracting ChainlinkTracker + resolution chain — further decomposition (session stats/reporting) best done alongside B12.
- [ ] B17 · `obi_velocity_5s/30s`: wire into signals or remove (audit S5).
- [ ] B19 · `--token` CLI arg is parsed but never consumed (silently falls through to the synthetic engine); `select_market_interactive`/`_display_and_pick_market` are dead code; OBIApp is only reachable via `--btc5m`. Decide: rewire the direct-token intelligence path or remove the arg + dead selectors (now in `modes/select.py`). CLAUDE.md/README document `--token` as working.

## Ideas (unprioritized parking lot — new-strategy candidates graduate to STRATEGY_LOG)

- (empty — new strategy ideas land here first)

## Done

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
