# Session Handoff

> Claude: read this FIRST each session. Overwrite (don't append) at session end or after any
> major milestone. Keep under 60 lines — git history is the archive.

**Updated:** 2026-06-10 (night) · **Branch:** main · **EXP-002 paper run LIVE in Omar's terminal**

## Current Focus

All planned phases done (audit → criticals → live-safety → resilience → tests → splits). EXP-002 baseline accumulating overnight. Next session: close EXP-002, then B12.

## State of the World

- **EXP-002 running in Omar's own terminal** (foreground, started ~18:25 Jun 10, btc 5m+15m paper, caffeinate). Morning: Omar Ctrl+C once → settle → say "close EXP-002".
- Suite: **74 tests green** (`env/bin/python -m pytest tests/`). Pyflakes clean of real issues.
- Structure: main.py is a 325-line dispatcher → `modes/` (intelligence/select/btc5m); pair_runner ~1230 with `chainlink_feed.py` + `window_settler.py` extracted.
- 18 backlog items done today (B1-B11, B13-B16, B18 + criticals). Open: B12, B17, B19, B13-residual.

## Next Steps (in order)

1. **Close EXP-002**: stats from `pair_windows_20260610/11.csv` — EXCLUDE windows settled before 18:25 Jun 10 (discarded Claude run); in `pair_buys_*`, synthetic test rows have market label `BTC 5m` (no window-time suffix) — exclude those too. Write STRATEGY_LOG Part 2 entry with per-TF net_pnl/window, pairs/window, rejection_rate, variance + verdict.
2. B12: `[pairs]` section in strategy.conf → PairConfig (hot-reload per window; stamp active params into pair_windows CSV rows).
3. New-strategy ideation off the baseline data → BACKLOG Ideas → EXP-003+.
4. B19 decision (dead `--token` path) and B17 (obi_velocity) when convenient.

## Watch Out

- strategy.conf still ignored in `--pairs` mode until B12 (params: `pair_runner.py` PairConfig block + `pair_strategy.py:149`).
- After any "AMBIGUOUS LIVE ORDER" banner: verify positions on polymarket.com; kill-switch cap approximate until then (RUNBOOK §3).
- Don't run paper smokes or fill-producing tests outside pytest while EXP-002 runs — conftest redirects test CSVs, but ad-hoc `main.py --pairs` runs write into the same dated CSVs.

## Open Questions (for Omar)

- (none — sleep optional, the bot doesn't need supervision)
