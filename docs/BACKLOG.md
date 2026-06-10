# Backlog

> One line per item. IDs are permanent — reference them in commits ("fixes B1") and in HANDOFF.md.
> When done: tick it, move the line to Done with the date. Detail lives in [AUDIT-2026-06-10.md](AUDIT-2026-06-10.md), not here.

## Now (P0)

- [ ] EXP-002 · Re-baseline PAIRS-v15: multi-hour paper run (headless 5m+15m), close entry in STRATEGY_LOG with net_pnl/window, pairs/window, rejection_rate.

## Next (P1)

- [ ] B9 · Narrow WebSocket exception handling — distinguish auth failure (alert) vs transient timeout (backoff) (audit H5).
- [ ] B12 · Make a `[pairs]` config section actually drive pairs mode — params hardcoded in `pair_runner.py` (audit S3). Pull forward before any param-tuning experiments.
- [ ] B16 · DB write-queue overflow: add dropped-write counter + throttled warning + surface in stats (audit S6; warning log already exists).
- [ ] B18 · Headless Ctrl+C: "settling…" feedback + settlement deadline after stop requested (audit H7).

## Later (P2 — structural)

- [ ] B13 · Split `main.py` (1100+ lines / 6 modes) and `pair_runner.py` (1300+ lines) (audit S1/S2). Do after B14 grows the test net.
- [ ] B14 · Grow tests: analytics pure functions (`metrics.py`, `detectors.py`, `momentum.py`, `cvd.py`) (audit S7). Started — 19 tests exist (fill logging, kill switch, executor reconcile, runner safety).
- [ ] B17 · `obi_velocity_5s/30s`: wire into signals or remove (audit S5).

## Ideas (unprioritized parking lot — new-strategy candidates graduate to STRATEGY_LOG)

- (empty — new strategy ideas land here first)

## Done

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
