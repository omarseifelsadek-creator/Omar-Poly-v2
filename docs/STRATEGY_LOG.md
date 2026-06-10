# Strategy Log

> Part 1 = current truth per strategy (edit in place). Part 2 = append-only experiments, newest first.
> An experiment without a Verdict is unfinished — close it before starting the next.
> Every Result must cite the actual data files (`data/logs/pair_windows_*.csv` etc.) so claims stay auditable.

---

# Part 1 — Strategy Registry

## PAIRS-v15 — YES+NO accumulation

**Status:** live-tested (real-money sessions ran; see data caveat below) · **Since:** 2026-03-02 (v13 lineage)
**Code:** `execution/pair_strategy.py` (engine + PairConfig), `execution/pair_runner.py` (orchestrator + v15 overrides), `execution/market_spec.py` (per-timeframe timing)

**Thesis:** In Polymarket crypto Up/Down windows (BTC/ETH/SOL/XRP, 5m/15m), accumulate matched
YES+NO pairs whenever combined cost < $1.00 − fees. A completed pair pays $1.00 at settlement
regardless of outcome — the edge is transient one-sided flow mispricing one leg. Target pair
cost ≤ $0.96 → ≥ ~4¢ gross spread per pair.

**Rules (the "6 ironclad" + Gemini patches):**
1. **Inventory lock** — if skew > 30%, lock the heavy side until rebalanced.
2. **Anti-falling-knife** — never open a first leg below $0.15 (panic hedge exempt).
3. **Price zones** — Sniper ≤ $0.35 (buy aggressively, ignore signal filters), Value $0.36–dynamic (buy with OBI/flow filters), Dead > dynamic breakeven (blocked).
4. **Dynamic dead zone** — second-leg ceiling derived from first-leg cost (breakeven-aware).
5. **Atomic entry** — only open Leg 1 if the opposite book can complete the pair ≤ $0.99.
6. **Panic hedge** — last 10s: aggressive matching at breakeven to avoid unmatched settlement.
7. **Theta sizing** — full size until 180s remain (5m), half until 30s, no new opens in last 30s.
8. **Unmatched cap** — |yes_cost − no_cost| ≤ $30 (now `PairConfig.max_unmatched_usd`).

**Execution model (paper):** VWAP book-walking L1→L5; time-in-book fill probability
(0% < 200ms ask age, 50% 200–500ms, 100% > 500ms). Live: FOK orders via CLOB, engine-state
rollback on rejection. Settlement: Chainlink → Binance → Gamma → order-book fallback.

**Key params (where defined matters — see CLAUDE.md warning):**

| Param | Value | Defined in |
|---|---|---|
| target_pair_cost | 0.96 | PairConfig default (`pair_strategy.py`) |
| max_pair_cost | **0.96** (v15; default 0.99) | override in `pair_runner.py:164` |
| max_skew_pct | **0.30** (v15; default 0.50) | override in `pair_runner.py` |
| atomic_entry_max_pair | **0.99** (was 1.05) | override in `pair_runner.py` |
| obi_delay_threshold / flow_delay_threshold | **0.85 / 0.75** (v15) | override in `pair_runner.py` |
| buy_size_usd / max_position_usd | 10 / 100 (panic 116) | PairConfig defaults |
| max_unmatched_usd | 30 | PairConfig (config-driven since 2026-06-10) |
| min_first_leg_price / sniper_threshold | 0.15 / 0.35 | PairConfig defaults |
| panic_time_seconds, theta_*, sniper_signal_min_time | spec-derived (10s/180s/30s/90s @ 5m) | `market_spec.py` properties |

**Known weaknesses:** unmatched-leg risk at settlement; fee curve `price·(1−price)·0.0625` eats
thin spreads near 50¢; rejection rate at thin books; kill switch only checked post-settlement (B8);
engine/CLOB desync possible on executor exception (B7).

**Version history (git archaeology, 2026-06-10):**
- **v8/v9** (pre-main, deleted branch) — original pair bot + Bloomberg-style dashboard prototype.
- **v13** `a66dbcf` (Mar 2) — panic 30s, extended cap 16, new report format.
- **v14** `ae928c9` (Mar 2) — live executor, dry-run mode, Magic-wallet sig_type=1 auth. Same day: live PnL tracking + quant CSV context (`8676135`), graceful Ctrl+C (`584d154`), Binance instant resolution (`340e49e`).
- **Mar 3** — Chainlink resolution as Polymarket's exact price source (`bf1bf77`); **4 Gemini patches + backtester** (`cd6ae70`): atomic entry, panic hedge, theta sizing, dynamic dead zone; multi-asset/multi-timeframe (`404f2df`); unified interactive menu (`4173d9a`).
- **Mar 4** — headless multi-runner (`7a9e265`); atomic_entry 0.95→1.05 unblock (`416b64c`); **v15 edge refinements** (`61ca11d`): skew 0.50→0.30, max_pair 0.99→0.96, OBI/flow thresholds loosened for earlier entry, $30 unmatched cap; kill switch + L2 recorder (`eec0458`).
- **⚠️ Mar 4** `1832ba1` (diagnostic CSV logging) — **introduced the fill-logging regression**: all success-path bookkeeping became dead code. **Every session from Mar 4 to Jun 10 logged zero fills and live P&L fell back to paper numbers.** Trustworthy fill data exists only for Mar 2–3 (`pair_buys_20260302/0303.csv`).
- **Jun 10** `039cc7c` — regression fixed + first regression test. Fill data trustworthy from here.

---

# Part 2 — Experiment Log

### EXP-001 · 2026-06-10 · pairs: re-baseline v15 with working fill logging
**Type:** paper
**Hypothesis:** With fill logging restored (B1), a paper session produces complete, auditable
fill/window records — establishing the v15 baseline that all future experiments compare against.
(There is no trustworthy baseline: pre-Mar-4 data predates v15's final params; post-Mar-4 data is empty.)
**Change:** none — stock v15 params (table above).
**Data:** `data/logs/pair_filters_20260610.csv`, `data/logs/pair_windows_20260610.csv`,
`data/logs/pair_buys_20260610.csv` + `tests/test_fill_logging.py` (synthetic fill → all bookkeeping asserted).
**Result:** *(short verification runs, BTC/5m headless paper)* — pipeline boots clean, 18k+ WS msgs
processed, filter decisions logging with full context (atomic_entry_too_wide / falling_knife
rejections at pair_cost ≈ $1.01 — correct refusals, market offered no edge in the observed windows).
Synthetic test proves the success path end-to-end: zone counts, report fills, dashboard, CSV row.
**Verdict:** ITERATE — logging verified; needs a multi-hour paper session for a statistically
meaningful baseline (n ≥ 50 windows) before any param experiment.
**Follow-up:** EXP-002 = overnight paper run, both timeframes; report net_pnl/window, pairs/window,
rejection_rate, max_unhedged from `pair_windows_*.csv`.
