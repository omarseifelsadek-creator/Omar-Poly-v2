# Session Handoff

> Claude: read this FIRST each session. Overwrite (don't append) at session end or after any
> major milestone. Keep under 60 lines — git history is the archive.

**Updated:** 2026-06-10 · **Branch:** main · **Last mode run:** paper (BTC/5m headless verification runs)

## Current Focus

Revamp complete (audit → docs system → critical fixes → hygiene). Next: a long paper session to
baseline PAIRS-v15 (EXP-002), then new-strategy development on top of the intelligence layer.

## State of the World

- All P0 bugs fixed and committed (B1-B6): fill logging restored + regression-tested, live-mode
  startup gates, deps complete. Hygiene done (B10/B11): one venv (`env/`), stale branches pruned.
- **Fill data from Mar 4 → Jun 10 is empty/untrustworthy** (regression window — see STRATEGY_LOG
  version history). Only Mar 2-3 and post-Jun-10 CSVs are real.
- First test exists: `tests/test_fill_logging.py` (pytest in `env/`). Run before sessions.
- Live trading gates per RUNBOOK §1: **B7 + B8 still open** — they are the prerequisites for
  resuming live. Until then: paper/dry-run only is the recommendation.
- EXP-001 (re-baseline) is ITERATE — verification runs clean; needs n ≥ 50 windows (EXP-002).

## Next Steps (in order)

1. EXP-002: overnight/multi-hour paper run (`--headless`, 5m + 15m), then close it in STRATEGY_LOG
   with net_pnl/window, pairs/window, rejection_rate from `pair_windows_*.csv`.
2. B7 — executor exception guard + engine/CLOB resync (`pair_runner.py:740` area).
3. B8 — kill-switch pre-entry check (`pair_runner.py` settle loop + `_try_evaluate`).
4. Then: live re-enable decision per RUNBOOK ladder.
5. New-strategy ideation → BACKLOG "Ideas" → graduate to EXP entries.

## Watch Out

- `strategy.conf` does NOT drive pairs mode — v15 params live in `pair_runner.py:164` (B12).
- `ui/cyber_engine.py` is NOT orphaned — it's the default no-flag visualization mode.
- macOS has no `timeout` command — background runs use `& PID=$!; sleep N; kill` pattern.

## Open Questions (for Omar)

- Resume live only after B7+B8 (recommended), or earlier with small size and the known risks?
- Which asset/timeframe should EXP-002 baseline prioritize (BTC 5m+15m assumed)?
