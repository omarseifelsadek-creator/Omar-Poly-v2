# Session Handoff

> Claude: read this FIRST each session. Overwrite (don't append) at session end or after any
> major milestone. Keep under 60 lines — git history is the archive.

**Updated:** 2026-06-10 (22:20) · **Branch:** main · **No runs active**

## Current Focus

EXP-002 CLOSED — v15 paper baseline: **+$11.69/window avg, std $21.46, 61% win rate (n=18)**.
Revamp is complete (audit -> fixes -> tests -> structure -> config -> cleanup). Next: grow the
baseline sample and start param/new-strategy experiments.

## State of the World

- Baseline locked in STRATEGY_LOG Part 2 (EXP-002) + registry. Mean is ~2.3 SE above zero —
  suggestive, not conclusive; paper fill model is an upper bound.
- Omar's overnight run ended early (Ctrl+C 21:48 after 3.5h, 18 windows). No process running now.
- Suite 82 green; ruff clean; everything pushed through the EXP-002 close.
- Experiments are conf edits now: `strategy.conf [pairs]` -> restart runner -> params stamp per window.

## Next Steps (in order)

1. EXP-002b: more baseline windows at different times of day (`--headless` paper, stock conf).
   Pool with n=18. Aim n >= 50 before trusting comparisons.
2. EXP-003: atomic_entry_max_pair / max_pair_cost frontier probe (rejections are 60%, mostly
   atomic_entry_too_wide — details in STRATEGY_LOG EXP-002 follow-up).
3. New-strategy ideation: `obi_velocity_5s/30s` (B17) is computed and unconsumed; intelligence
   layer (CVD, regime, detectors) only feeds the dashboard today.
4. P2 leftovers when convenient: B13-residual, B20 (dedup unify), B21 (regime thresholds).

## Watch Out

- 15m variance is 2x its mean (std $29 vs +$11.40) — needs 3-4x the 5m sample for equal confidence.
- Paper-vs-live fill gap unmeasured — before any live resumption, run dry-run windows or
  tiny-size live and compare fill rates to paper (RUNBOOK ladder).
- pair_params_*.csv only exists for runs started after B12 (Omar's EXP-002 run predates it).

## Open Questions (for Omar)

- (none)
