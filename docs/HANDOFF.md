# Session Handoff

> Claude: read this FIRST each session. Overwrite (don't append) at session end or after any
> major milestone. Keep under 60 lines — git history is the archive.

**Updated:** 2026-06-10 (PM) · **Branch:** main · **Last mode run:** paper smoke (headless btc 5m)

## Current Focus

Phase 1 (live-safety: B7/B8/B15) just landed. Next: EXP-002 baseline run, then B9/B12 (config-driven pairs params before tuning).

## State of the World

- **Live trading is now gated safely**: ambiguous CLOB submissions reconcile against trade history (adopt or halt window); kill switch checks before every entry with shared session budget; crash-looping message loop ends the window instead of spinning.
- All P0 criticals fixed (B1-B6) + hygiene done (B10/B11). 19 tests green (`env/bin/python -m pytest tests/`).
- **No trustworthy pairs baseline yet** — all pre-Jun-10 fill CSVs are empty/stale (B1 bug since Mar 4). EXP-002 is the next milestone.
- Commits are LOCAL ONLY — not yet pushed to origin.

## Next Steps (in order)

1. EXP-002: overnight/multi-hour `--headless` paper run (btc 5m+15m), then write the entry in STRATEGY_LOG.md Part 2 from `pair_windows_*.csv`.
2. B12: `[pairs]` section in strategy.conf driving `PairConfig` (prereq for cheap param experiments).
3. B9 + B16 + B18 (resilience batch — see plan in chat 2026-06-10 / Phase 2).
4. New-strategy ideation → BACKLOG Ideas → EXP entries.

## Watch Out

- `strategy.conf` still ignored in `--pairs` mode until B12 — pair params live in `pair_runner.py:164` + `pair_strategy.py:149` (PairConfig).
- Ambiguity halt is per-window: after an "AMBIGUOUS LIVE ORDER" banner, verify positions on polymarket.com before letting the next window trade.
- Live P&L correctness depends on `_live_fills` — only populated since the B1 fix.

## Open Questions (for Omar)

- Push the 7+ local commits to origin?
- Resume live trading now that B7/B8 landed, or wait for EXP-002 baseline first (recommended)?
