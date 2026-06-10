# Backlog

> One line per item. IDs are permanent — reference them in commits ("fixes B1") and in HANDOFF.md.
> When done: tick it, move the line to Done with the date. Detail lives in [AUDIT-2026-06-10.md](AUDIT-2026-06-10.md), not here.

## Now (P0 — broken / blocks everything)

- [ ] B1 · Fix dead fill-logging after early return — `pair_runner.py:776-897` (audit C1). No fills logged at all; live P&L wrong.
- [ ] B2 · `main.py:187` hardcodes `Side.BUY` in `record_trade_at_level` — breaks absorption detection (audit C2).
- [ ] B3 · requirements.txt: add `py-clob-client`, `openpyxl` (audit C3).
- [ ] B4 · Bound module-level dedup dicts in `interpreter.py` / `signals.py` — memory leak (audit C4).
- [ ] B5 · Validate `.env` eagerly in `LiveExecutor.__init__` — fail fast with named missing vars (audit C5).
- [ ] B6 · Typed confirmation gate before `--mode live` starts (skippable via `--yes`) (audit C5).

## Next (P1 — urgent while trading live)

- [ ] B7 · Wrap executor calls in `_try_evaluate`; resync/halt on exception so engine never desyncs from CLOB (audit H3).
- [ ] B8 · Kill switch: check before entries (not only post-settlement); warn near threshold (audit H4).
- [ ] B9 · Narrow WebSocket exception handling — distinguish auth failure vs transient timeout (audit H5).
- [ ] B15 · Log/parse malformed CLOB responses instead of silent `bad_response` rejection (audit H6).

## Later (P2 — structural / hygiene)

- [ ] B10 · Delete `venv/` (`env/` is canonical — it has the trading deps); prune `claude/youthful-allen` branch + leftover worktree, `claude/thirsty-tesla`.
- [ ] B11 · Retire superseded scripts to `research/legacy/`: `quant_dashboard.py`, `generate_report.py` (audit S8).
- [ ] B12 · Make a `[pairs]` config section actually drive pairs mode — params currently hardcoded in `pair_runner.py` (audit S3).
- [ ] B13 · Split `main.py` (1102 lines / 6 modes) and `pair_runner.py` (1278 lines) (audit S1/S2).
- [ ] B14 · First tests: analytics pure functions (`metrics.py`, `detectors.py`) (audit S7).
- [ ] B16 · Log DB write-queue overflow instead of dropping silently (audit S6).
- [ ] B17 · `obi_velocity_5s/30s`: wire into signals or remove (audit S5).
- [ ] B18 · Headless Ctrl+C: make settlement cancellable or print "settling…" feedback (audit H7).

## Ideas (unprioritized parking lot — new-strategy candidates graduate to STRATEGY_LOG)

- (empty — new strategy ideas land here first)

## Done

- (move items here with date)
