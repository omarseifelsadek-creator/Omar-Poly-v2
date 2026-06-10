# Backlog

> One line per item. IDs are permanent — reference them in commits ("fixes B1") and in HANDOFF.md.
> When done: tick it, move the line to Done with the date. Detail lives in [AUDIT-2026-06-10.md](AUDIT-2026-06-10.md), not here.

## Now (P0 — broken / blocks everything)

- (empty — P0s cleared 2026-06-10)

## Next (P1 — urgent while trading live)

- [ ] B7 · Wrap executor calls in `_try_evaluate`; resync/halt on exception so engine never desyncs from CLOB (audit H3). **Gate for live per RUNBOOK §1.**
- [ ] B8 · Kill switch: check before entries (not only post-settlement); warn near threshold (audit H4). **Gate for live per RUNBOOK §1.**
- [ ] B9 · Narrow WebSocket exception handling — distinguish auth failure vs transient timeout (audit H5).
- [ ] B15 · Log/parse malformed CLOB responses instead of silent `bad_response` rejection (audit H6).

## Later (P2 — structural / hygiene)

- [ ] B12 · Make a `[pairs]` config section actually drive pairs mode — params currently hardcoded in `pair_runner.py` (audit S3).
- [ ] B13 · Split `main.py` (1100+ lines / 6 modes) and `pair_runner.py` (1280+ lines) (audit S1/S2).
- [ ] B14 · Grow the test suite beyond `tests/test_fill_logging.py` — analytics pure functions next (audit S7).
- [ ] B16 · Log DB write-queue overflow instead of dropping silently (audit S6).
- [ ] B17 · `obi_velocity_5s/30s`: wire into signals or remove (audit S5).
- [ ] B18 · Headless Ctrl+C: make settlement cancellable or print "settling…" feedback (audit H7).

## Ideas (unprioritized parking lot — new-strategy candidates graduate to STRATEGY_LOG)

- (empty — new strategy ideas land here first)

## Done

- [x] 2026-06-10 · B1 · Fill-logging dead code dedented; zone else-branch fixed; regression test added (`039cc7c`).
- [x] 2026-06-10 · B2 · `record_trade_at_level` unused `side` param removed — tracker already derived it correctly; audit's "corruption" claim was overstated (`4d4fcc9`).
- [x] 2026-06-10 · B3 · requirements.txt: `py-clob-client` + `openpyxl` added (`4d4fcc9`).
- [x] 2026-06-10 · B4 · Dedup dicts bounded with behavior-preserving expiry pruning (`1fb2230`).
- [x] 2026-06-10 · B5 · Eager `.env` validation at LiveExecutor construction; also fixed `load_dotenv("env")` pointing at the virtualenv dir (`4d4fcc9`).
- [x] 2026-06-10 · B6 · Typed-`yes` live-mode gate + `--yes` flag (`4d4fcc9`).
- [x] 2026-06-10 · B10 · `venv/` deleted (`env/` canonical); stale branches + worktree pruned (`7fcb209`).
- [x] 2026-06-10 · B11 · `quant_dashboard.py`, `generate_report.py` → `research/legacy/` with supersession notes (`7fcb209`).
