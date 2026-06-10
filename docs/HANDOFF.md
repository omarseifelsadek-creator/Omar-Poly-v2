# Session Handoff

> Claude: read this FIRST each session. Overwrite (don't append) at session end or after any
> major milestone. Keep under 60 lines — git history is the archive.

**Updated:** 2026-06-10 (evening) · **Branch:** main (pushed through Phase 1; Phase 2 commit pending push) · **Last mode run:** paper smoke

## Current Focus

Phases 1+2 of the B-fix plan landed (live safety + resilience). Remaining: EXP-002 baseline, B12 (config-driven pairs), then Phase 3 (B14 analytics tests) → Phase 4 (B13 file splits).

## State of the World

- **Live-safe**: ambiguous-fill reconciliation w/ 10s query timeout + prior-fill guard; pre-entry kill switch w/ shared session budget + unverified-risk accounting; crash-loop escalation; WS auth/geo-block failures distinguished from transient drops (B9); DB drops counted (B16); bounded graceful stop (B18).
- 28 tests green (`env/bin/python -m pytest tests/`). Python-reviewer pass done on Phase 1; its 3 findings fixed.
- **Still no pairs baseline** — EXP-002 (multi-hour headless paper run) is the gate before any tuning or new-strategy comparisons.
- strategy.conf still ignored in `--pairs` mode until B12 (params at `pair_runner.py:164`, `pair_strategy.py:149`).

## Next Steps (in order)

1. EXP-002 **is running** (started 2026-06-10 18:22, pid in `/tmp/exp002.pid`, log: `data/logs/exp002_run.log`). Morning: `kill -INT $(cat /tmp/exp002.pid)`, wait for settle, then compute baseline stats from `pair_windows_20260610/11.csv` → close the entry in STRATEGY_LOG Part 2.
2. B12: `[pairs]` section in strategy.conf → PairConfig (hot-reload like the existing LiveConfig pattern).
3. Phase 3: analytics test suite (metrics/detectors/momentum/cvd pure functions).
4. Phase 4: split main.py → modes/, extract WindowSettler + SessionStats from pair_runner.
5. New-strategy ideation → BACKLOG Ideas → STRATEGY_LOG experiments.

## Watch Out

- After an "AMBIGUOUS LIVE ORDER" banner: kill-switch cap is approximate until positions verified on polymarket.com (cost counted as lost; RUNBOOK §3).
- Two separate `--pairs` terminals don't share the loss budget — use `--headless` for multi-timeframe.
- Live P&L correctness depends on `_live_fills` — only populated since the B1 fix (Jun 10).

## Open Questions (for Omar)

- (none — EXP-002 in flight; keep the MacBook plugged in overnight)
