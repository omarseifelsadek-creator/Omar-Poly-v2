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

**Key params — ALL tunables live in `config/strategy.conf [pairs]` since B12 (2026-06-10);
re-read per window rotation, stamped per window to `data/logs/pair_params_*.csv`:**

| Param | v15 value | Notes |
|---|---|---|
| target_pair_cost / max_pair_cost | 0.96 / **0.96** | v15 tightened max from 0.99 |
| max_skew_pct | **0.30** | v15 tightened from 0.50 |
| atomic_entry_max_pair | **0.99** | was 1.05 |
| obi_delay_threshold / flow_delay_threshold | **0.85 / 0.75** | v15 loosened for earlier entry |
| buy_size_usd / max_position_usd | 10 / 100 (panic 116) | |
| max_unmatched_usd | 30 | |
| min_first_leg_price / sniper_threshold / value_zone_high | 0.15 / 0.35 / 0.43 | |
| panic_time_seconds, theta_*, sniper_signal_min_time | spec-derived (10s/180s/30s/90s @ 5m) | NOT in conf — `market_spec.py` per timeframe |

**Known weaknesses:** unmatched-leg risk at settlement; fee curve `price·(1−price)·0.0625` eats
thin spreads near 50¢; ~60% rejection rate (EXP-002 — dominated by atomic_entry_too_wide);
paper fill model optimism unquantified vs live. (B7 desync guard + B8 pre-entry kill switch
closed 2026-06-10.)

**Baseline (EXP-002, 2026-06-10, paper):** +$11.69/window avg, std $21.46, 61% win rate,
n=18 (BTC 5m+15m). This is the bar every param change and new strategy compares against.

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

### EXP-002 · 2026-06-10 · pairs: v15 baseline established (paper, BTC 5m+15m)
**Type:** paper
**Hypothesis:** Stock v15 has positive expectancy after fees in current BTC conditions; establish
the mean AND variance per window that all future experiments compare against.
**Change:** none — stock v15 params (run predates B12, so params came from the code; identical
values now live in `strategy.conf [pairs]`).
**Data:** `data/logs/pair_windows_20260610.csv` (n=18 settled windows, 18:35–21:30 settles,
run 18:26–21:48 in Omar's terminal), `pair_buys_20260610.csv` (157 real fills; 11 synthetic
test rows excluded by market label `BTC 5m` without window suffix), `pair_filters_20260610.csv`.
**Result:**
| | ALL (n=18) | 5m (n=10) | 15m (n=8) |
|---|---|---|---|
| net P&L total | **+$210.45** | +$119.24 | +$91.21 |
| avg / window | **+$11.69** (std $21.46) | +$11.92 (std $14.30) | +$11.40 (std $29.25) |
| median / window | +$7.92 | +$11.00 | +$1.25 |
| win rate | 61% | 70% | 50% |
| range | −$18.44 .. +$57.78 | −$4.09 .. +$41.48 | −$18.44 .. +$57.78 |
| pairs / window | 51.4 | 40.5 | 65.0 |
| avg pair cost | $0.757 | $0.729 | $0.791 |
| rejection rate | 60.1% | 59.6% | 60.6% |
| participation | — | 10/~40 windows (25%) | 8/~13 (62%) |

Avg fee 1.42%/fill; slippage ≈ 0.1¢ (paper VWAP model); max unhedged avg $14.81; zones:
sniper 44 / value 29 / panic 11 (panic fills only on 5m). Pair costs avg $0.73–0.79 — well
under the $0.96 ceiling, so completed pairs locked 17–27¢ gross.
**Stats honesty:** mean is ~2.3 SE above zero (SE ≈ $5.06) — suggestive, not conclusive at n=18.
Single 3.5h evening session (15:30–18:30 UTC), single asset, paper fill model is optimistic
(no queue competition; 100% fill ≥ 500ms ask age). Treat as upper bound.
**Verdict:** ITERATE — baseline locked in as the comparison bar (+$11.69/win, std $21.46).
Not ADOPT-for-live until the sample covers more sessions/times-of-day and the paper-vs-live
fill gap is measured (dry-run or small-size live windows).
**Follow-up:** (a) extend baseline across different times of day — pool into EXP-002 (params
identical); (b) EXP-003 candidate from rejection analysis: 60% rejection dominated by
atomic_entry_too_wide — probe `atomic_entry_max_pair` 0.99→1.00 and `max_pair_cost` 0.96→0.94
in opposite directions to map the frontier; (c) 15m variance (std $29) is 2× its mean —
needs 3–4× the sample of 5m for the same confidence.

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
