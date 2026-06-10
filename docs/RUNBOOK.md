# Live Trading Runbook

> The procedure for going from paper to real money. Documents current reality including known
> limitations — update only when the procedure itself changes.

## 0. Environment

- ONE virtualenv: `source env/bin/activate` (Python 3.13). `venv/` was an incomplete duplicate, deleted 2026-06-10.
- Fresh install: `pip install -r requirements.txt` (includes `py-clob-client` + `openpyxl` since 2026-06-10).
- Live-deps check: `python -c "import py_clob_client; print('ok')"`
- Sanity: `python -m pytest tests/ -q` must pass before any session that follows code changes.

## 1. Promotion Ladder (gates, not vibes)

**PAPER** (default — `--mode paper`)
- Gate to advance: ≥ 50 windows logged across ≥ 2 sessions; `pair_buys_*.csv` and
  `pair_windows_*.csv` populating; net_pnl/window non-negative after fees; rejection patterns
  in `pair_filters_*.csv` understood (each top reason explainable).

**DRY-RUN** (`--mode dry-run` — signs real EIP-712 orders, never posts)
- Requires: `.env` with all five `POLY_*` vars (copy `.env.example` → `.env`).
- Gate to advance: zero auth errors across a full session; signed payloads spot-checked in logs
  (maker = POLY_FUNDER, signatureType = 1, signature 130 chars); ≥ 1 clean multi-window session.

**LIVE** (`--mode live` — real FOK orders, real money)
- Hard prerequisites: **B7 (executor desync guard) and B8 (kill-switch timing) closed in
  [BACKLOG.md](BACKLOG.md)** — as of 2026-06-10 both are OPEN; live trading before they close
  is accepting those risks knowingly.
- Start: one asset, one timeframe, minimum size (`buy_size_usd` 10), `--max-loss` set.
- The startup gate requires typing `yes` (or `--yes` for scripted runs) — added 2026-06-10.

## 2. Preflight Checklist (before every live session)

- [ ] `.env` present with all 5 vars: `POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET`,
      `POLY_API_PASSPHRASE`, `POLY_FUNDER` (startup now fails fast naming any missing var)
- [ ] USDC balance confirmed in the funder (proxy) wallet on polymarket.com
- [ ] `--max-loss N` set. **KNOWN LIMITATION (B8): only checked after each window settles** —
      one in-flight window can exceed the cap before the halt fires
- [ ] Params reviewed in `pair_runner.py:164` (v15 overrides) — **strategy.conf does NOT drive
      pairs mode** (B12); editing the conf changes nothing here
- [ ] Telegram alerts firing if configured (optional)
- [ ] `data/logs/` writable; previous session's CSVs archived or noted in STRATEGY_LOG

## 3. Kill Switch & Emergency Stop

- `--max-loss N` halts new windows once cumulative session P&L ≤ −N (post-settlement check; B8).
- **Ctrl+C (once)** = graceful: finishes the current window, settles, flushes reports.
  In headless mode the runner may sit in settlement for ~30s — it is not hung (H7).
- **Ctrl+C (twice)** = force-cancel all runners (headless mode).
- **Hard stop:** kill the process, then **manually flatten on polymarket.com** — the engine holds
  no live state across restarts; the CLOB is the only ground truth for open positions.

## 4. Crash Recovery (mid-window with open legs)

1. Check last fills: tail of `data/logs/pair_buys_<today>.csv` (side, qty, price, cost).
2. Check actual positions: polymarket.com portfolio (or py-clob-client query) — **CLOB is ground
   truth; engine state is memory-only and died with the process.**
3. Reconcile: if one-sided (unmatched legs), decide — complete the pair manually on the website
   if pair cost still < $1.00, or hold to settlement and eat the directional exposure.
4. **Partial procedure until B7 lands** — there is no automated resync; the steps above are manual.

## 5. Post-Session

1. CSVs land in `data/logs/` (`pair_buys_*`, `pair_windows_*`, `pair_filters_*`, `pair_rejections_*`).
2. `python generate_pair_report.py` → Excel hourly report; `streamlit run streamlit_dashboard.py` for the 6-tab analysis.
3. Write/close the experiment entry in [STRATEGY_LOG.md](STRATEGY_LOG.md) (mandatory Verdict).
4. Update [HANDOFF.md](HANDOFF.md) if the session changed where things stand.

## Incident Log (append-only, real incidents only)

| Date | What happened | Root cause | Fix / backlog ID |
|------|---------------|------------|------------------|
| 2026-03-04 → 06-10 | All fills unlogged; live P&L reported from paper engine | Indentation regression in `1832ba1` put success-path bookkeeping after a `return` | Fixed `039cc7c` + regression test (B1) |
