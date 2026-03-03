"""
pair_backtest.py — Pair Strategy Backtester

Replays historical fills from pair_buys CSV with modified parameters
and recalculates settlement PnL using known winners.

USAGE:
    # Single backtest with parameter overrides
    python pair_backtest.py --data 20260303 --param panic_pair_cost=0.99

    # Parameter sweep
    python pair_backtest.py --data 20260303 --sweep panic_pair_cost --sweep-range 0.95,1.03,0.01

    # Quiet mode (summary only)
    python pair_backtest.py --data 20260303 --param max_pair_cost=0.97 --quiet

    # Multiple overrides
    python pair_backtest.py --data 20260303 --param panic_pair_cost=0.99 --param panic_time_seconds=15
"""

import argparse
import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


# ══════════════════════════════════════════════════════════════
# DEFAULT PARAMETERS  (mirrors PairConfig in pair_strategy.py)
# ══════════════════════════════════════════════════════════════

DEFAULTS = {
    "buy_size_usd":             10.0,
    "max_position_usd":         100.0,
    "panic_max_position_usd":   116.0,
    "target_pair_cost":         0.96,
    "max_pair_cost":            0.99,
    "panic_pair_cost":          1.02,
    "sniper_threshold":         0.35,
    "value_zone_high":          0.43,       # Backtest-optimized: was 0.44
    "min_first_leg_price":      0.15,
    "obi_delay_threshold":      0.75,
    "flow_delay_threshold":     0.6,
    "panic_time_seconds":       10.0,       # Backtest-optimized: was 30.0
    "max_skew_pct":             0.50,       # Gemini #3: was 0.20
    "sniper_signal_min_time":   90.0,
    "panic_hedge_pair_limit":   0.97,       # Backtest-optimized: cap pair cost on panic hedges
}


class BacktestConfig:
    """Merged defaults + user overrides, with attribute access."""

    def __init__(self, overrides: dict = None):
        merged = {**DEFAULTS}
        if overrides:
            for k, v in overrides.items():
                if k not in DEFAULTS:
                    raise ValueError(f"Unknown parameter: {k}")
                merged[k] = float(v)
        for k, v in merged.items():
            setattr(self, k, v)


# ══════════════════════════════════════════════════════════════
# CSV LOADING & CLEANING
# ══════════════════════════════════════════════════════════════

DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "logs")


def _clean_signed(s: pd.Series) -> pd.Series:
    """Strip +/- prefix, %, N/A from numeric columns."""
    return (
        s.astype(str)
        .str.replace("+", "", regex=False)
        .str.replace("%", "", regex=False)
        .str.replace("N/A", "", regex=False)
        .str.strip()
        .replace("", np.nan)
        .astype(float)
    )


def _load_buys(date_str: str) -> pd.DataFrame:
    """Load and clean pair_buys CSV for a given date."""
    path = os.path.join(DATA_DIR, f"pair_buys_{date_str}.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No buys file: {path}")

    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()

    # Clean numeric columns
    for col in ["qty", "ask_price", "vwap_price", "fill_price", "cost",
                "obi", "flow_pressure", "time_remaining", "skew",
                "yes_qty", "no_qty", "pair_cost", "opposite_ask",
                "best_bid", "spread", "ask_age_ms", "levels_walked",
                "yes_bid_depth", "yes_ask_depth", "no_bid_depth", "no_ask_depth"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "fee_pct" in df.columns:
        df["fee_pct"] = _clean_signed(df["fee_pct"])
    if "slippage_cents" in df.columns:
        df["slippage_cents"] = _clean_signed(df["slippage_cents"])
    if "unhedged_usd" in df.columns:
        df["unhedged_usd"] = _clean_signed(df["unhedged_usd"])
    if "time_to_hedge_s" in df.columns:
        df["time_to_hedge_s"] = _clean_signed(df["time_to_hedge_s"])

    # Boolean columns
    for col in ["is_snipe", "sweep"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().str.upper() == "YES"

    return df


def _load_windows(date_str: str) -> pd.DataFrame:
    """Load and clean pair_windows CSV for a given date."""
    path = os.path.join(DATA_DIR, f"pair_windows_{date_str}.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"No windows file: {path}")

    df = pd.read_csv(path)
    df.columns = df.columns.str.strip()

    for col in ["yes_qty", "yes_avg_cost", "no_qty", "no_avg_cost",
                "completed_pairs", "unmatched_qty", "avg_pair_cost",
                "total_capital", "num_buys", "sniper_fills", "value_fills",
                "panic_fills"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ["pair_profit", "gamble_result", "net_pnl", "cumulative_pnl",
                "avg_slippage_cents"]:
        if col in df.columns:
            df[col] = _clean_signed(df[col])

    if "rejection_rate" in df.columns:
        df["rejection_rate"] = _clean_signed(df["rejection_rate"])

    return df


# ══════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════

@dataclass
class SimulatedWindow:
    """Results for one window: original vs simulated."""
    market: str
    winner: str
    # Original (from CSV)
    orig_pnl: float
    orig_pairs: float
    orig_pair_cost: float
    # Simulated
    sim_yes_qty: float
    sim_no_qty: float
    sim_yes_cost: float
    sim_no_cost: float
    sim_pairs: float
    sim_pair_cost: float
    sim_pair_profit: float
    sim_gamble: float
    sim_pnl: float
    # Fill stats
    fills_total: int
    fills_accepted: int
    fills_rejected: int
    rejection_reasons: dict = field(default_factory=dict)
    # Data completeness (buys CSV vs windows CSV)
    complete: bool = True  # True if buys CSV qty matches windows CSV qty


@dataclass
class BacktestResult:
    """Aggregate results across all windows."""
    date: str
    overrides: dict
    windows: list = field(default_factory=list)

    @property
    def total_orig_pnl(self) -> float:
        return sum(w.orig_pnl for w in self.windows)

    @property
    def total_sim_pnl(self) -> float:
        return sum(w.sim_pnl for w in self.windows)

    @property
    def pnl_delta(self) -> float:
        return self.total_sim_pnl - self.total_orig_pnl

    @property
    def orig_win_rate(self) -> float:
        if not self.windows:
            return 0.0
        return sum(1 for w in self.windows if w.orig_pnl > 0) / len(self.windows)

    @property
    def sim_win_rate(self) -> float:
        if not self.windows:
            return 0.0
        return sum(1 for w in self.windows if w.sim_pnl > 0) / len(self.windows)

    @property
    def total_fills(self) -> int:
        return sum(w.fills_total for w in self.windows)

    @property
    def total_accepted(self) -> int:
        return sum(w.fills_accepted for w in self.windows)

    @property
    def total_rejected(self) -> int:
        return sum(w.fills_rejected for w in self.windows)

    @property
    def aggregate_rejection_reasons(self) -> dict:
        agg = {}
        for w in self.windows:
            for reason, count in w.rejection_reasons.items():
                agg[reason] = agg.get(reason, 0) + count
        return dict(sorted(agg.items(), key=lambda x: -x[1]))


# ══════════════════════════════════════════════════════════════
# WINDOW SIMULATOR — the core replay engine
# ══════════════════════════════════════════════════════════════

class WindowSimulator:
    """Replays fills for one window with modified parameters."""

    def __init__(self, config: BacktestConfig, fills_df: pd.DataFrame,
                 winner: str, orig_pnl: float, orig_pairs: float,
                 orig_pair_cost: float, market: str):
        self.config = config
        self.fills = fills_df.sort_values("time_remaining", ascending=False)
        self.winner = winner
        self.orig_pnl = orig_pnl
        self.orig_pairs = orig_pairs
        self.orig_pair_cost = orig_pair_cost
        self.market = market

        # Simulated state
        self.yes_qty = 0.0
        self.no_qty = 0.0
        self.yes_cost = 0.0
        self.no_cost = 0.0
        self.yes_locked = False
        self.no_locked = False
        self.accepted = []
        self.rejected_reasons = {}

    @property
    def total_capital(self) -> float:
        return self.yes_cost + self.no_cost

    @property
    def matched_pairs(self) -> float:
        return min(self.yes_qty, self.no_qty)

    @property
    def yes_avg(self) -> float:
        return self.yes_cost / self.yes_qty if self.yes_qty > 0 else 0.0

    @property
    def no_avg(self) -> float:
        return self.no_cost / self.no_qty if self.no_qty > 0 else 0.0

    @property
    def pair_cost(self) -> float:
        if self.yes_qty > 0 and self.no_qty > 0:
            return self.yes_avg + self.no_avg
        return 0.0

    @property
    def skew(self) -> float:
        total = self.yes_qty + self.no_qty
        if total == 0:
            return 0.0
        return abs(self.yes_qty - self.no_qty) / total

    def simulate(self) -> SimulatedWindow:
        """Replay all fills and compute settlement."""
        fills_total = len(self.fills)

        for _, row in self.fills.iterrows():
            accepted, reason = self._should_accept_fill(row)
            if accepted:
                self._apply_fill(row)
                self.accepted.append(row)
            else:
                self.rejected_reasons[reason] = self.rejected_reasons.get(reason, 0) + 1

        # Settlement
        sim_pair_profit, sim_gamble, sim_pnl = self._settle()

        return SimulatedWindow(
            market=self.market,
            winner=self.winner,
            orig_pnl=self.orig_pnl,
            orig_pairs=self.orig_pairs,
            orig_pair_cost=self.orig_pair_cost,
            sim_yes_qty=self.yes_qty,
            sim_no_qty=self.no_qty,
            sim_yes_cost=self.yes_cost,
            sim_no_cost=self.no_cost,
            sim_pairs=self.matched_pairs,
            sim_pair_cost=self.pair_cost,
            sim_pair_profit=sim_pair_profit,
            sim_gamble=sim_gamble,
            sim_pnl=sim_pnl,
            fills_total=fills_total,
            fills_accepted=len(self.accepted),
            fills_rejected=fills_total - len(self.accepted),
            rejection_reasons=self.rejected_reasons,
        )

    def _should_accept_fill(self, row) -> tuple:
        """
        7-gate filter pipeline. Mirrors _evaluate_side() in pair_strategy.py.
        Returns (accepted: bool, reason: str).
        """
        cfg = self.config
        side = str(row["side"]).strip()
        ask_price = float(row["ask_price"])
        fill_price = float(row["fill_price"])
        qty = float(row["qty"])
        time_remaining = float(row["time_remaining"])
        obi = float(row.get("obi", 0.5)) if pd.notna(row.get("obi")) else 0.5
        flow = float(row.get("flow_pressure", 0.0)) if pd.notna(row.get("flow_pressure")) else 0.0

        in_panic = time_remaining <= cfg.panic_time_seconds

        is_first_leg = (self.yes_qty == 0 and self.no_qty == 0)
        is_completing = (
            (side == "YES" and self.no_qty > 0 and self.yes_qty < self.no_qty) or
            (side == "NO" and self.yes_qty > 0 and self.no_qty < self.yes_qty)
        )
        panic_hedge = in_panic and is_completing

        # Gate 1: Capital limit
        cap = cfg.max_position_usd
        if in_panic and is_completing:
            cap = cfg.panic_max_position_usd
        if self.total_capital >= cap:
            return False, "capital_limit"

        # Gate 2: Inventory lock (skew check)
        if side == "YES" and self.yes_locked:
            return False, "yes_locked"
        if side == "NO" and self.no_locked:
            return False, "no_locked"

        # Gate 3: Anti-falling knife
        if ask_price < cfg.min_first_leg_price and is_first_leg:
            return False, "falling_knife_first_leg"
        if ask_price < cfg.min_first_leg_price and not is_completing:
            return False, "falling_knife_not_completing"

        # Gate 4: Panic hedge bypass
        if panic_hedge:
            if fill_price >= 1.00:
                return False, "panic_hedge_over_dollar"
            # Skip zone/signal gates, fall through to pair cost check
        else:
            # Gate 5: Dead zone
            if ask_price > cfg.value_zone_high:
                return False, "dead_zone"

            # Gate 6: Signal filters
            in_sniper = ask_price <= cfg.sniper_threshold

            if in_sniper:
                # Sniper: ignore signals if >90s left (opportunity zone)
                if time_remaining <= cfg.sniper_signal_min_time:
                    return False, "sniper_too_late"
            else:
                # Value zone: check OBI/flow
                obi_for_side = obi if side == "YES" else (1.0 - obi)
                flow_for_side = flow if side == "YES" else -flow
                if obi_for_side > cfg.obi_delay_threshold:
                    return False, "obi_delay"
                if flow_for_side > cfg.flow_delay_threshold:
                    return False, "flow_delay"

        # Gate 7: Pair cost check
        if panic_hedge:
            if fill_price >= 1.00:
                return False, "panic_fill_over_dollar"
            # Optional: enforce pair cost limit even on panic hedges
            limit = cfg.panic_hedge_pair_limit
            if limit > 0 and self.yes_qty > 0 and self.no_qty > 0:
                new_yes_qty = self.yes_qty + (qty if side == "YES" else 0)
                new_no_qty = self.no_qty + (qty if side == "NO" else 0)
                new_yes_cost = self.yes_cost + (float(row["cost"]) if side == "YES" else 0)
                new_no_cost = self.no_cost + (float(row["cost"]) if side == "NO" else 0)
                if new_yes_qty > 0 and new_no_qty > 0:
                    projected = (new_yes_cost / new_yes_qty) + (new_no_cost / new_no_qty)
                    if projected > limit:
                        return False, "panic_hedge_cost_exceeded"
        else:
            if not self._would_pair_cost_be_ok(side, qty, fill_price, in_panic):
                return False, "pair_cost_exceeded"

        return True, "accepted"

    def _would_pair_cost_be_ok(self, side: str, qty: float,
                                fill_price: float, in_panic: bool) -> bool:
        """Check if buying would keep pair cost under limit."""
        cfg = self.config

        new_yes_qty = self.yes_qty + (qty if side == "YES" else 0)
        new_no_qty = self.no_qty + (qty if side == "NO" else 0)
        new_yes_cost = self.yes_cost + (fill_price * qty if side == "YES" else 0)
        new_no_cost = self.no_cost + (fill_price * qty if side == "NO" else 0)

        if new_yes_qty > 0 and new_no_qty > 0:
            new_pair_cost = (new_yes_cost / new_yes_qty) + (new_no_cost / new_no_qty)
            if in_panic:
                return new_pair_cost <= cfg.panic_pair_cost
            return new_pair_cost <= cfg.max_pair_cost

        # First leg heuristic
        max_first_leg = cfg.sniper_threshold + 0.15
        if in_panic:
            max_first_leg = 0.65
        return fill_price <= max_first_leg

    def _apply_fill(self, row):
        """Apply an accepted fill to simulated state."""
        side = str(row["side"]).strip()
        qty = float(row["qty"])
        # Use CSV cost directly (includes exact fee rounding)
        cost = float(row["cost"]) if pd.notna(row.get("cost")) else float(row["fill_price"]) * qty

        if side == "YES":
            self.yes_qty += qty
            self.yes_cost += cost
        else:
            self.no_qty += qty
            self.no_cost += cost

        self._update_locks()

    def _update_locks(self):
        """Update inventory locks based on skew."""
        total = self.yes_qty + self.no_qty
        if total < 10:
            self.yes_locked = False
            self.no_locked = False
            return

        if self.skew > self.config.max_skew_pct:
            if self.yes_qty > self.no_qty:
                self.yes_locked = True
                self.no_locked = False
            else:
                self.no_locked = True
                self.yes_locked = False
        else:
            self.yes_locked = False
            self.no_locked = False

    def _settle(self) -> tuple:
        """
        Compute settlement PnL. Mirrors settle() in pair_strategy.py.
        Returns (pair_profit, gamble_result, net_pnl).
        """
        matched = self.matched_pairs
        unmatched_qty = abs(self.yes_qty - self.no_qty)

        if unmatched_qty > 0:
            unmatched_side = "YES" if self.yes_qty > self.no_qty else "NO"
        else:
            unmatched_side = "NONE"

        # Pair profit
        if matched > 0 and self.pair_cost > 0:
            pair_profit = matched * (1.00 - self.pair_cost)
        else:
            pair_profit = 0.0

        # Gamble result (unmatched legs)
        if unmatched_qty > 0 and unmatched_side != "NONE":
            if unmatched_side == self.winner:
                gamble_payout = unmatched_qty * 1.00
                if unmatched_side == "YES":
                    unmatched_cost = self.yes_cost - (matched * self.yes_avg)
                else:
                    unmatched_cost = self.no_cost - (matched * self.no_avg)
                gamble_result = gamble_payout - unmatched_cost
            else:
                if unmatched_side == "YES":
                    unmatched_cost = self.yes_cost - (matched * self.yes_avg)
                else:
                    unmatched_cost = self.no_cost - (matched * self.no_avg)
                gamble_result = -unmatched_cost
        else:
            gamble_result = 0.0

        # Cross-check with total payout method
        if self.winner == "YES":
            winning_payout = self.yes_qty * 1.00
        else:
            winning_payout = self.no_qty * 1.00
        net_pnl = winning_payout - self.total_capital

        return pair_profit, gamble_result, net_pnl


# ══════════════════════════════════════════════════════════════
# MAIN BACKTEST RUNNER
# ══════════════════════════════════════════════════════════════

def run_backtest(date_str: str, overrides: dict = None,
                 verbose: bool = True) -> BacktestResult:
    """
    Run backtest on a single day's data with parameter overrides.

    Args:
        date_str: Date in YYYYMMDD format
        overrides: Dict of parameter overrides (e.g. {"panic_pair_cost": 0.99})
        verbose: Print per-window table and summary

    Returns:
        BacktestResult with all simulated windows
    """
    overrides = overrides or {}
    config = BacktestConfig(overrides)
    buys_df = _load_buys(date_str)
    windows_df = _load_windows(date_str)

    # Build market -> winner lookup from windows
    win_lookup = {}
    pnl_lookup = {}
    pairs_lookup = {}
    cost_lookup = {}
    for _, row in windows_df.iterrows():
        market = str(row["market"]).strip()
        win_lookup[market] = str(row["winner"]).strip()
        pnl_lookup[market] = float(row["net_pnl"]) if pd.notna(row["net_pnl"]) else 0.0
        pairs_lookup[market] = float(row["completed_pairs"]) if pd.notna(row["completed_pairs"]) else 0.0
        cost_lookup[market] = float(row["avg_pair_cost"]) if pd.notna(row["avg_pair_cost"]) else 0.0

    # Build window qty lookup for data completeness check
    win_yes_qty = {}
    win_no_qty = {}
    for _, row in windows_df.iterrows():
        m = str(row["market"]).strip()
        win_yes_qty[m] = float(row["yes_qty"]) if pd.notna(row["yes_qty"]) else 0.0
        win_no_qty[m] = float(row["no_qty"]) if pd.notna(row["no_qty"]) else 0.0

    result = BacktestResult(date=date_str, overrides=overrides)

    # Group buys by market (= window) and simulate each
    for market, group in buys_df.groupby("market"):
        market = str(market).strip()
        if market not in win_lookup:
            continue  # No settlement data

        sim = WindowSimulator(
            config=config,
            fills_df=group,
            winner=win_lookup[market],
            orig_pnl=pnl_lookup[market],
            orig_pairs=pairs_lookup[market],
            orig_pair_cost=cost_lookup[market],
            market=market,
        )
        window_result = sim.simulate()

        # Check data completeness: do buys CSV totals match windows CSV?
        buys_yes = group[group["side"] == "YES"]["qty"].sum()
        buys_no = group[group["side"] == "NO"]["qty"].sum()
        expected_yes = win_yes_qty.get(market, 0)
        expected_no = win_no_qty.get(market, 0)
        if abs(buys_yes - expected_yes) > 0.5 or abs(buys_no - expected_no) > 0.5:
            window_result.complete = False

        result.windows.append(window_result)

    # Also add windows with zero fills in buys (they show up in windows CSV only)
    markets_with_buys = set(str(m).strip() for m in buys_df["market"].unique())
    for _, row in windows_df.iterrows():
        market = str(row["market"]).strip()
        if market not in markets_with_buys:
            # Window with no fills at all
            winner = str(row["winner"]).strip()
            orig_pnl = float(row["net_pnl"]) if pd.notna(row["net_pnl"]) else 0.0
            has_fills = float(row["yes_qty"]) + float(row["no_qty"]) > 0
            result.windows.append(SimulatedWindow(
                market=market, winner=winner,
                orig_pnl=orig_pnl, orig_pairs=0, orig_pair_cost=0,
                sim_yes_qty=0, sim_no_qty=0, sim_yes_cost=0, sim_no_cost=0,
                sim_pairs=0, sim_pair_cost=0, sim_pair_profit=0,
                sim_gamble=0, sim_pnl=0,
                fills_total=0, fills_accepted=0, fills_rejected=0,
                complete=not has_fills,  # incomplete if window had fills but buys CSV didn't
            ))

    if verbose:
        _print_comparison(result)
        _print_summary(result)

    return result


# ══════════════════════════════════════════════════════════════
# PARAMETER SWEEP
# ══════════════════════════════════════════════════════════════

def sweep_parameter(date_str: str, param: str, values: list,
                    base_overrides: dict = None) -> pd.DataFrame:
    """
    Sweep a single parameter across a range of values.

    Returns DataFrame with columns:
        [param_value, total_pnl, pnl_delta, win_rate, fills_accepted, fills_rejected]
    """
    base_overrides = base_overrides or {}
    rows = []

    # Run baseline (no overrides beyond base)
    baseline = run_backtest(date_str, base_overrides, verbose=False)

    for val in values:
        overrides = {**base_overrides, param: val}
        result = run_backtest(date_str, overrides, verbose=False)
        # Use only complete windows for consistent comparison
        cw = [w for w in result.windows if w.complete]
        rows.append({
            "value": val,
            "total_pnl": sum(w.sim_pnl for w in cw),
            "pnl_delta": sum(w.sim_pnl for w in cw) - sum(w.orig_pnl for w in cw),
            "win_rate": sum(1 for w in cw if w.sim_pnl > 0) / len(cw) * 100 if cw else 0,
            "wins": sum(1 for w in cw if w.sim_pnl > 0),
            "fills_accepted": sum(w.fills_accepted for w in cw),
            "fills_rejected": sum(w.fills_rejected for w in cw),
        })

    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════
# OUTPUT FORMATTING
# ══════════════════════════════════════════════════════════════

def _short_market(market: str) -> str:
    """Shorten market name for display."""
    # "BTC Up/Down 5m — 02:20-02:25 UTC" -> "02:20-02:25"
    if "—" in market:
        return market.split("—")[-1].strip().replace(" UTC", "")
    return market[:25]


def _print_comparison(result: BacktestResult):
    """Print per-window comparison table."""
    print()
    print("=" * 100)
    print(f"  PAIR STRATEGY BACKTEST — {result.date}")
    if result.overrides:
        params = ", ".join(f"{k}={v}" for k, v in result.overrides.items())
        print(f"  Overrides: {params}")
    print("=" * 100)
    print(f"{'Window':<18} {'Win':>3} {'Orig PnL':>9} {'Sim PnL':>9} "
          f"{'Delta':>8} {'Pairs':>7} {'PairCost':>9} {'Fills':>7}")
    print("-" * 100)

    for w in result.windows:
        win_str = w.winner[:1]
        pairs_str = f"{w.sim_pairs:.0f}/{w.orig_pairs:.0f}"
        cost_str = f"${w.sim_pair_cost:.3f}" if w.sim_pair_cost > 0 else "  ---  "
        delta = w.sim_pnl - w.orig_pnl
        fills_str = f"{w.fills_accepted}/{w.fills_total}"
        flag = " *" if not w.complete else ""

        # Color-code delta
        delta_str = f"{'+'if delta>=0 else ''}{delta:.2f}"

        print(f"{_short_market(w.market):<18} {win_str:>3} "
              f"${w.orig_pnl:>+8.2f} ${w.sim_pnl:>+8.2f} "
              f"{delta_str:>8} {pairs_str:>7} {cost_str:>9} {fills_str:>7}{flag}")

    print("-" * 100)


def _print_summary(result: BacktestResult):
    """Print aggregate summary."""
    n = len(result.windows)
    complete = [w for w in result.windows if w.complete]
    incomplete = [w for w in result.windows if not w.complete]
    nc = len(complete)

    orig_wins = sum(1 for w in complete if w.orig_pnl > 0)
    sim_wins = sum(1 for w in complete if w.sim_pnl > 0)
    orig_pnl = sum(w.orig_pnl for w in complete)
    sim_pnl = sum(w.sim_pnl for w in complete)
    delta = sim_pnl - orig_pnl

    print()
    print("=" * 60)
    print(f"  SUMMARY — {n} windows ({nc} complete, {len(incomplete)} partial)")
    print("=" * 60)

    if result.overrides:
        params = ", ".join(f"{k}={v}" for k, v in result.overrides.items())
        print(f"  Overrides: {params}")
        print()

    if incomplete:
        print(f"  NOTE: {len(incomplete)} windows have incomplete fill data")
        print(f"        (buys CSV missing fills). Stats below use")
        print(f"        only the {nc} complete windows.")
        print()

    print(f"  Original PnL:  ${orig_pnl:>+8.2f}  (complete windows)")
    print(f"  Simulated PnL: ${sim_pnl:>+8.2f}")
    print(f"  PnL Delta:     ${delta:>+8.2f}")
    print()
    wr_orig = orig_wins / nc * 100 if nc > 0 else 0
    wr_sim = sim_wins / nc * 100 if nc > 0 else 0
    print(f"  Win Rate: {orig_wins}/{nc} ({wr_orig:.1f}%) "
          f"-> {sim_wins}/{nc} ({wr_sim:.1f}%)")

    total_fills = sum(w.fills_total for w in complete)
    total_acc = sum(w.fills_accepted for w in complete)
    total_rej = sum(w.fills_rejected for w in complete)
    print(f"  Fills: {total_fills} total -> "
          f"{total_acc} accepted, {total_rej} rejected")
    print()

    reasons = {}
    for w in complete:
        for reason, count in w.rejection_reasons.items():
            reasons[reason] = reasons.get(reason, 0) + count
    reasons = dict(sorted(reasons.items(), key=lambda x: -x[1]))
    if reasons:
        print("  Top rejection reasons:")
        for reason, count in list(reasons.items())[:8]:
            print(f"    {reason:<30s} {count:>5}")

    print("=" * 60)
    print()


def _print_sweep(sweep_df: pd.DataFrame, param_name: str):
    """Print parameter sweep results."""
    print()
    print("=" * 80)
    print(f"  PARAMETER SWEEP: {param_name}")
    print("=" * 80)
    print(f"{'Value':>10} {'Total PnL':>12} {'Delta':>10} "
          f"{'Win Rate':>10} {'Wins':>6} {'Accepted':>10} {'Rejected':>10}")
    print("-" * 80)

    best_row = sweep_df.loc[sweep_df["total_pnl"].idxmax()]

    for _, row in sweep_df.iterrows():
        marker = " <-- BEST" if row["value"] == best_row["value"] else ""
        print(f"{row['value']:>10.3f} ${row['total_pnl']:>+10.2f} "
              f"${row['pnl_delta']:>+9.2f} {row['win_rate']:>9.1f}% "
              f"{row['wins']:>5.0f} {row['fills_accepted']:>9.0f} "
              f"{row['fills_rejected']:>9.0f}{marker}")

    print("-" * 80)
    print(f"  Best value: {param_name} = {best_row['value']:.3f} "
          f"(PnL: ${best_row['total_pnl']:+.2f})")
    print("=" * 80)
    print()


# ══════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Pair Strategy Backtester — replay fills with modified parameters",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pair_backtest.py --data 20260303 --param panic_pair_cost=0.99
  python pair_backtest.py --data 20260303 --param panic_pair_cost=0.99 --param panic_time_seconds=15
  python pair_backtest.py --data 20260303 --sweep panic_pair_cost --sweep-range 0.95,1.03,0.01
  python pair_backtest.py --data 20260303 --quiet

Available parameters:
  buy_size_usd, max_position_usd, panic_max_position_usd,
  target_pair_cost, max_pair_cost, panic_pair_cost,
  sniper_threshold, value_zone_high, min_first_leg_price,
  obi_delay_threshold, flow_delay_threshold,
  panic_time_seconds, max_skew_pct, sniper_signal_min_time
        """,
    )
    parser.add_argument("--data", required=True,
                        help="Date YYYYMMDD (e.g. 20260303)")
    parser.add_argument("--param", action="append", default=[],
                        help="Parameter override key=value (repeatable)")
    parser.add_argument("--sweep", type=str, default=None,
                        help="Parameter name to sweep")
    parser.add_argument("--sweep-range", type=str, default=None,
                        help="Sweep range as start,stop,step (e.g. 0.95,1.03,0.01)")
    parser.add_argument("--quiet", action="store_true",
                        help="Show summary only (skip per-window table)")

    args = parser.parse_args()

    # Parse --param overrides
    overrides = {}
    for p in args.param:
        if "=" not in p:
            print(f"Error: --param must be key=value, got: {p}")
            return
        key, val = p.split("=", 1)
        key = key.strip()
        if key not in DEFAULTS:
            print(f"Error: unknown parameter '{key}'")
            print(f"Available: {', '.join(sorted(DEFAULTS.keys()))}")
            return
        overrides[key] = float(val)

    try:
        if args.sweep:
            if not args.sweep_range:
                print("Error: --sweep requires --sweep-range start,stop,step")
                return
            if args.sweep not in DEFAULTS:
                print(f"Error: unknown parameter '{args.sweep}'")
                return

            parts = args.sweep_range.split(",")
            if len(parts) != 3:
                print("Error: --sweep-range must be start,stop,step")
                return
            start, stop, step = float(parts[0]), float(parts[1]), float(parts[2])
            values = np.arange(start, stop + step / 2, step).tolist()

            print(f"Sweeping {args.sweep} from {start} to {stop} "
                  f"(step {step}, {len(values)} runs)...")
            sweep_df = sweep_parameter(args.data, args.sweep, values, overrides)
            _print_sweep(sweep_df, args.sweep)
        else:
            result = run_backtest(args.data, overrides,
                                  verbose=not args.quiet)
            if args.quiet:
                _print_summary(result)

    except FileNotFoundError as e:
        print(f"Error: {e}")
    except ValueError as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
