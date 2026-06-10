# Session Handoff

> Claude: read this FIRST each session. Overwrite (don't append) at session end or after any
> major milestone. Keep under 60 lines — git history is the archive.

**Updated:** 2026-06-10 (late evening) · **Branch:** main · **EXP-002 paper run LIVE in Omar's terminal**

## Current Focus

Everything done: audit → criticals → live-safety → resilience → tests → splits → B12 → full cleanup (ruff, dead code, --token rewired, tools/). EXP-002 accumulating. Next: close EXP-002, then new-strategy work.

## State of the World

- **EXP-002 running in Omar's own terminal** (foreground, started ~18:25 Jun 10, btc 5m+15m paper, caffeinate). Morning: Omar Ctrl+C once → settle → say "close EXP-002".
- Suite: **82 tests green**; `env/bin/python -m ruff check .` fully clean (config in pyproject.toml; dev deps in requirements-dev.txt).
- Structure: main.py = thin dispatcher → `modes/`; analysis scripts in `tools/` (research_cli, streamlit_dashboard, generate_pair_report, pair_backtest, demo_dashboard); `--token` works again (OBIApp direct).
- 21 backlog items done today (B1-B19 except residuals). Open: B13-residual, B20 (dedup unify), B21 (regime thresholds → settings) — all P2.

## Next Steps (in order)

1. **Close EXP-002**: stats from `pair_windows_20260610/11.csv` — EXCLUDE windows settled before 18:25 Jun 10 (discarded Claude run); in `pair_buys_*`, synthetic test rows have market label `BTC 5m` (no window-time suffix) — exclude those too. Write STRATEGY_LOG Part 2 entry with per-TF net_pnl/window, pairs/window, rejection_rate, variance + verdict. (Early peek at 12 windows: +$10.37/window avg, range −$18→+$58.)
2. EXP-003+: param experiments are now a conf edit (B12 done) — edit `[pairs]` in strategy.conf, restart runner, new params apply per window and stamp to `pair_params_*.csv`.
3. New-strategy ideation off the baseline data → BACKLOG Ideas.
4. B19 decision (dead `--token` path) and B17 (obi_velocity) when convenient.

## Watch Out

- **Omar's EXP-002 terminal process predates B12** — it runs the OLD code (conf still ignored for it). Conf edits affect only runners started after tonight. Don't edit `[pairs]` values until EXP-002 closes anyway (baseline purity).
- After any "AMBIGUOUS LIVE ORDER" banner: verify positions on polymarket.com; kill-switch cap approximate until then (RUNBOOK §3).
- Don't run paper smokes or fill-producing tests outside pytest while EXP-002 runs — conftest redirects test CSVs, but ad-hoc `main.py --pairs` runs write into the same dated CSVs.

## Open Questions (for Omar)

- (none — sleep optional, the bot doesn't need supervision)
