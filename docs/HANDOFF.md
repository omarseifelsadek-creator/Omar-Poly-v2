# Session Handoff

> Claude: read this FIRST each session. Overwrite (don't append) at session end or after any
> major milestone. Keep under 60 lines — git history is the archive.

**Updated:** 2026-06-10 · **Branch:** main · **Last mode run:** none this session

## Current Focus

Revamp after 2-month gap: audit done, workflow docs created; now fixing the critical bugs (B1-B6) so experiment data is trustworthy.

## State of the World

- Full audit completed 2026-06-10 → [AUDIT-2026-06-10.md](AUDIT-2026-06-10.md); backlog seeded with IDs.
- **Fill logging is broken (B1)**: no CSV buys, no live P&L tracking — every session since the regression produced no fill data. Omar is trading live; recommended pause until B1+B5+B6 land.
- Pairs engine (v15 + Gemini patches) is otherwise healthy; live order path (EIP-712/FOK/Magic wallet) verified correct.
- Trap: `strategy.conf` is ignored in `--pairs` mode — pair params are hardcoded in `pair_runner.py` (B12).

## Next Steps (in order)

1. B1 — dedent `pair_runner.py:778-897` out of the rejection branch; fix zone `"Panic"`→`"Dead"` at :797.
2. B2 — `main.py:187`: derive level side from `msg.side` (BUY→ASK, SELL→BID per `level_tracker.py:274`).
3. B3 — requirements.txt: add `py-clob-client`, `openpyxl`.
4. B4 — bound dedup dicts in `analytics/interpreter.py:35` + `analytics/signals.py:59`.
5. B5+B6 — eager `.env` validation in `LiveExecutor.__init__` + typed live-mode confirmation gate in `main.py`.

## Watch Out

- Don't add sklearn to requirements — its only user (`quant_dashboard.py`) is being retired to `research/legacy/`.
- `ui/cyber_engine.py` is NOT orphaned (default no-flag mode via `main.py:1049`) — do not delete.
- Two venvs exist until Phase C: **`env/` is canonical** (has py-clob-client); `venv/` is an incomplete fresh-install — delete it.

## Open Questions (for Omar)

- Pause live trading until B7/B8 (desync guard, kill-switch timing) also land, or resume after B1-B6?
