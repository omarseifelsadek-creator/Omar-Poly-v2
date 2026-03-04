"""
streamlit_dashboard.py — PolyQuant Analytics Dashboard

Full analytics suite for BTC 5-minute pair trading strategy.
Reads CSV logs directly from data/logs/ with multi-day support.

Run:  streamlit run streamlit_dashboard.py
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import glob
import os

# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="PolyQuant Analytics",
    layout="wide",
    page_icon="⚡",
    initial_sidebar_state="expanded",
)

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs")

COLORS = {
    "green": "#00E676",
    "red": "#FF1744",
    "blue": "#2979FF",
    "amber": "#FFD600",
    "cyan": "#00BCD4",
    "purple": "#AA00FF",
}

ZONE_COLORS = {
    "Sniper": COLORS["green"],
    "Value": COLORS["blue"],
    "Panic": COLORS["red"],
    "Dead": "#666666",
}

CHART_TEMPLATE = "plotly_dark"


def _chart_layout(fig, **kwargs):
    """Apply consistent dark styling to a plotly figure."""
    fig.update_layout(
        template=CHART_TEMPLATE,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=40, r=20, t=40, b=30),
        **kwargs,
    )
    return fig


# ══════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════

def _clean_signed(series: pd.Series) -> pd.Series:
    """Convert +/-/N/A formatted strings to numeric."""
    return pd.to_numeric(
        series.astype(str).str.replace("+", "", regex=False),
        errors="coerce",
    )


@st.cache_data(ttl=30)
def load_buys() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(LOG_DIR, "pair_buys_*.csv")))
    if not files:
        return pd.DataFrame()
    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f)
            date_str = os.path.basename(f).replace("pair_buys_", "").replace(".csv", "")
            df["file_date"] = date_str
            dfs.append(df)
        except Exception:
            continue
    if not dfs:
        return pd.DataFrame()
    df = pd.concat(dfs, ignore_index=True)

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["fee_pct"] = pd.to_numeric(
        df["fee_pct"].astype(str).str.rstrip("%"), errors="coerce"
    )
    df["is_snipe"] = df["is_snipe"].map({"YES": True, "NO": False})
    df["sweep"] = df["sweep"].map({"YES": True, "NO": False})
    df["slippage_cents"] = _clean_signed(df["slippage_cents"])
    df["time_to_hedge_s"] = pd.to_numeric(df["time_to_hedge_s"], errors="coerce")

    num_cols = [
        "qty", "ask_price", "vwap_price", "fill_price", "cost",
        "ask_age_ms", "levels_walked", "yes_qty", "no_qty",
        "pair_cost", "skew", "time_remaining", "obi",
        "flow_pressure", "opposite_ask", "best_bid", "spread",
        "yes_bid_depth", "yes_ask_depth", "no_bid_depth", "no_ask_depth",
        "unhedged_usd",
    ]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["date"] = df["timestamp"].dt.date
    return df


@st.cache_data(ttl=30)
def load_windows() -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(LOG_DIR, "pair_windows_*.csv")))
    if not files:
        return pd.DataFrame()
    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f)
            date_str = os.path.basename(f).replace("pair_windows_", "").replace(".csv", "")
            df["file_date"] = date_str
            dfs.append(df)
        except Exception:
            continue
    if not dfs:
        return pd.DataFrame()
    df = pd.concat(dfs, ignore_index=True)

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["rejection_rate"] = pd.to_numeric(
        df["rejection_rate"].astype(str).str.rstrip("%"), errors="coerce"
    )

    for col in ["pair_profit", "gamble_result", "net_pnl", "cumulative_pnl"]:
        if col in df.columns:
            df[col] = _clean_signed(df[col])

    num_cols = [
        "yes_qty", "no_qty", "yes_avg_cost", "no_avg_cost",
        "completed_pairs", "unmatched_qty", "avg_pair_cost",
        "total_capital", "num_buys", "fills_attempted", "fills_rejected",
        "dead_zone_blocks", "max_unhedged_usd",
        "sniper_fills", "value_fills", "panic_fills",
    ]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["cap_exhausted_at_s"] = pd.to_numeric(df["cap_exhausted_at_s"], errors="coerce")
    df["avg_hedge_time_s"] = pd.to_numeric(df["avg_hedge_time_s"], errors="coerce")
    df["avg_slippage_cents"] = _clean_signed(df.get("avg_slippage_cents", pd.Series()))

    df["date"] = df["timestamp"].dt.date
    return df


# ══════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════

st.sidebar.title("⚡ PolyQuant Analytics")

auto_refresh = st.sidebar.toggle("Auto-refresh (30s)", value=False)

buys_raw = load_buys()
windows_raw = load_windows()

if buys_raw.empty and windows_raw.empty:
    st.warning("No CSV data found in data/logs/. Run the pair trading bot first.")
    st.stop()

# Collect all dates
all_dates = sorted(set(
    list(buys_raw["date"].dropna().unique() if not buys_raw.empty else [])
    + list(windows_raw["date"].dropna().unique() if not windows_raw.empty else [])
))

if len(all_dates) >= 2:
    date_range = st.sidebar.date_input(
        "Date range", value=(min(all_dates), max(all_dates)),
        min_value=min(all_dates), max_value=max(all_dates),
    )
else:
    date_range = (all_dates[0], all_dates[0]) if all_dates else None

# Timeframe filter (extract from market column)
def _extract_tf(market_str):
    if pd.isna(market_str):
        return "Unknown"
    s = str(market_str)
    if "15m" in s:
        return "15m"
    elif "1h" in s:
        return "1h"
    elif "5m" in s:
        return "5m"
    return "Other"

tf_options = ["ALL"]
for df_raw in [windows_raw, buys_raw]:
    if not df_raw.empty and "market" in df_raw.columns:
        tfs = df_raw["market"].apply(_extract_tf).unique().tolist()
        for t in tfs:
            if t not in tf_options:
                tf_options.append(t)
tf_filter = st.sidebar.selectbox("Timeframe", tf_options)

# Mode filter
modes = ["ALL"]
if not buys_raw.empty and "mode" in buys_raw.columns:
    modes += sorted(buys_raw["mode"].dropna().unique().tolist())
mode_filter = st.sidebar.selectbox("Mode", modes)

# Apply filters
buys = buys_raw.copy()
windows = windows_raw.copy()

if date_range and isinstance(date_range, tuple) and len(date_range) == 2:
    buys = buys[(buys["date"] >= date_range[0]) & (buys["date"] <= date_range[1])]
    windows = windows[(windows["date"] >= date_range[0]) & (windows["date"] <= date_range[1])]

if tf_filter != "ALL":
    if not buys.empty and "market" in buys.columns:
        buys = buys[buys["market"].apply(_extract_tf) == tf_filter]
    if not windows.empty and "market" in windows.columns:
        windows = windows[windows["market"].apply(_extract_tf) == tf_filter]

if mode_filter != "ALL":
    buys = buys[buys["mode"] == mode_filter]
    windows = windows[windows["mode"] == mode_filter]

# Recompute cumulative PnL after filtering
if not windows.empty and "net_pnl" in windows.columns:
    windows = windows.sort_values("timestamp").reset_index(drop=True)
    windows["cumulative_pnl"] = windows["net_pnl"].cumsum()
    windows["win"] = windows["net_pnl"] > 0
    windows["peak"] = windows["cumulative_pnl"].cummax()
    windows["drawdown"] = windows["cumulative_pnl"] - windows["peak"]

st.sidebar.markdown("---")
st.sidebar.metric("Total Windows", len(windows))
st.sidebar.metric("Total Buys", len(buys))
st.sidebar.metric("Days", len(all_dates))


# ══════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "Performance", "Execution Quality", "Zone Analytics",
    "Risk & Hedging", "Microstructure", "Raw Data",
])


# ──────────────────────────────────────────────────────────────
# TAB 1: PERFORMANCE OVERVIEW
# ──────────────────────────────────────────────────────────────

with tab1:
    if windows.empty:
        st.info("No window settlement data available.")
    else:
        net_pnl_total = windows["net_pnl"].sum()
        win_rate = windows["win"].mean() * 100 if len(windows) > 0 else 0
        gross_profit = windows.loc[windows["net_pnl"] > 0, "net_pnl"].sum()
        gross_loss = abs(windows.loc[windows["net_pnl"] < 0, "net_pnl"].sum())
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        max_dd = windows["drawdown"].min() if "drawdown" in windows.columns else 0
        pnl_std = windows["net_pnl"].std()
        sharpe = (windows["net_pnl"].mean() / pnl_std * np.sqrt(288)) if pnl_std > 0 else 0
        avg_pair_cost = windows.loc[windows["avg_pair_cost"] > 0, "avg_pair_cost"].mean()

        # KPI row
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Net PnL", f"${net_pnl_total:+.2f}")
        c2.metric("Win Rate", f"{win_rate:.1f}%")
        c3.metric("Profit Factor", f"{profit_factor:.2f}x" if profit_factor != float("inf") else "All Wins")
        c4.metric("Max Drawdown", f"${max_dd:.2f}")
        c5.metric("Sharpe (5m)", f"{sharpe:.2f}")
        c6.metric("Avg Pair Cost", f"${avg_pair_cost:.3f}" if not np.isnan(avg_pair_cost) else "N/A")

        st.markdown("---")

        # Equity curve + Drawdown
        col_eq, col_dd = st.columns(2)
        with col_eq:
            fig = px.area(
                windows, x="timestamp", y="cumulative_pnl",
                title="Equity Curve",
                color_discrete_sequence=[COLORS["green"]],
            )
            fig.add_hline(y=0, line_dash="dot", line_color="gray")
            _chart_layout(fig)
            st.plotly_chart(fig, use_container_width=True)

        with col_dd:
            fig = px.area(
                windows, x="timestamp", y="drawdown",
                title="Drawdown",
                color_discrete_sequence=[COLORS["red"]],
            )
            _chart_layout(fig)
            st.plotly_chart(fig, use_container_width=True)

        # PnL distribution + Streaks
        col_hist, col_streak = st.columns(2)
        with col_hist:
            fig = px.histogram(
                windows, x="net_pnl", nbins=25,
                color="win",
                color_discrete_map={True: COLORS["green"], False: COLORS["red"]},
                title="PnL Distribution",
            )
            fig.add_vline(x=0, line_dash="dot", line_color="gray")
            _chart_layout(fig, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

        with col_streak:
            # Compute win/loss streaks
            wins = windows["win"].values
            streaks = []
            if len(wins) > 0:
                current_type = wins[0]
                current_len = 1
                for i in range(1, len(wins)):
                    if wins[i] == current_type:
                        current_len += 1
                    else:
                        streaks.append({"type": "Win" if current_type else "Loss", "length": current_len})
                        current_type = wins[i]
                        current_len = 1
                streaks.append({"type": "Win" if current_type else "Loss", "length": current_len})

            if streaks:
                sdf = pd.DataFrame(streaks)
                sdf["idx"] = range(len(sdf))
                fig = px.bar(
                    sdf, x="idx", y="length", color="type",
                    color_discrete_map={"Win": COLORS["green"], "Loss": COLORS["red"]},
                    title="Win/Loss Streaks",
                    labels={"idx": "Streak #", "length": "Length"},
                )
                _chart_layout(fig, showlegend=False)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("Not enough data for streak analysis.")

        # Per-window PnL bar chart
        fig = px.bar(
            windows, x="timestamp", y="net_pnl",
            color="win",
            color_discrete_map={True: COLORS["green"], False: COLORS["red"]},
            title="Per-Window PnL",
        )
        fig.add_hline(y=0, line_dash="dot", line_color="gray")
        _chart_layout(fig, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)


# ──────────────────────────────────────────────────────────────
# TAB 2: EXECUTION QUALITY
# ──────────────────────────────────────────────────────────────

with tab2:
    if buys.empty:
        st.info("No buy execution data available.")
    else:
        # Slippage by zone + Rejection rate
        col_slip, col_rej = st.columns(2)
        with col_slip:
            if "zone" in buys.columns and "slippage_cents" in buys.columns:
                fig = px.violin(
                    buys.dropna(subset=["slippage_cents"]),
                    x="zone", y="slippage_cents", color="zone",
                    color_discrete_map=ZONE_COLORS,
                    box=True, points="all",
                    title="Slippage by Zone (cents)",
                )
                fig.add_hline(y=0, line_dash="dot", line_color="gray")
                _chart_layout(fig, showlegend=False)
                st.plotly_chart(fig, use_container_width=True)

        with col_rej:
            if not windows.empty and "rejection_rate" in windows.columns:
                fig = px.bar(
                    windows, x="timestamp", y="rejection_rate",
                    title="Fill Rejection Rate per Window (%)",
                    color_discrete_sequence=[COLORS["amber"]],
                )
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        # Ask age vs fill + Levels walked
        col_age, col_lvl = st.columns(2)
        with col_age:
            if "ask_age_ms" in buys.columns:
                fig = px.scatter(
                    buys.dropna(subset=["ask_age_ms"]),
                    x="ask_age_ms", y="fill_price", color="zone",
                    color_discrete_map=ZONE_COLORS,
                    size="qty", size_max=15,
                    title="Ask Age vs Fill Price",
                    labels={"ask_age_ms": "Ask Age (ms)", "fill_price": "Fill Price ($)"},
                )
                fig.add_vline(x=200, line_dash="dash", line_color=COLORS["amber"],
                              annotation_text="200ms")
                fig.add_vline(x=500, line_dash="dash", line_color=COLORS["red"],
                              annotation_text="500ms")
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        with col_lvl:
            if "levels_walked" in buys.columns:
                lvl_counts = buys["levels_walked"].value_counts().sort_index().reset_index()
                lvl_counts.columns = ["levels", "count"]
                fig = px.bar(
                    lvl_counts, x="levels", y="count",
                    title="Levels Walked Distribution",
                    color_discrete_sequence=[COLORS["cyan"]],
                    labels={"levels": "Book Levels Walked", "count": "Fill Count"},
                )
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        # VWAP vs Raw + Fee distribution
        col_vwap, col_fee = st.columns(2)
        with col_vwap:
            if "ask_price" in buys.columns and "vwap_price" in buys.columns:
                fig = px.scatter(
                    buys, x="ask_price", y="vwap_price", color="zone",
                    color_discrete_map=ZONE_COLORS,
                    title="VWAP Fill vs Raw Ask Price",
                    labels={"ask_price": "Raw Ask ($)", "vwap_price": "VWAP Fill ($)"},
                )
                # Diagonal reference line
                price_range = [buys["ask_price"].min(), buys["ask_price"].max()]
                fig.add_trace(go.Scatter(
                    x=price_range, y=price_range,
                    mode="lines", line=dict(dash="dot", color="gray"),
                    showlegend=False,
                ))
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        with col_fee:
            if "fee_pct" in buys.columns:
                fig = px.histogram(
                    buys.dropna(subset=["fee_pct"]),
                    x="fee_pct", nbins=20,
                    title="Fee Distribution (%)",
                    color_discrete_sequence=[COLORS["purple"]],
                    labels={"fee_pct": "Fee (%)"},
                )
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)


# ──────────────────────────────────────────────────────────────
# TAB 3: ZONE ANALYTICS
# ──────────────────────────────────────────────────────────────

with tab3:
    if buys.empty:
        st.info("No buy execution data available.")
    else:
        # Zone summary table
        if "zone" in buys.columns:
            zone_stats = buys.groupby("zone").agg(
                fills=("qty", "count"),
                total_qty=("qty", "sum"),
                avg_price=("fill_price", "mean"),
                avg_cost=("cost", "mean"),
                avg_slippage=("slippage_cents", "mean"),
                avg_obi=("obi", "mean"),
            ).round(4)
            st.subheader("Zone Performance Summary")
            st.dataframe(zone_stats, use_container_width=True)

        st.markdown("---")

        # Zone fills over time + Zone cost distributions
        col_fills, col_costs = st.columns(2)
        with col_fills:
            if not windows.empty and all(c in windows.columns for c in ["sniper_fills", "value_fills", "panic_fills"]):
                zone_time = windows[["timestamp", "sniper_fills", "value_fills", "panic_fills"]].copy()
                zone_melt = zone_time.melt(
                    id_vars="timestamp",
                    value_vars=["sniper_fills", "value_fills", "panic_fills"],
                    var_name="zone", value_name="fills",
                )
                zone_melt["zone"] = zone_melt["zone"].str.replace("_fills", "").str.title()
                fig = px.bar(
                    zone_melt, x="timestamp", y="fills", color="zone",
                    color_discrete_map=ZONE_COLORS,
                    barmode="stack",
                    title="Zone Fill Counts per Window",
                )
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        with col_costs:
            if "zone" in buys.columns and "fill_price" in buys.columns:
                fig = px.box(
                    buys, x="zone", y="fill_price", color="zone",
                    color_discrete_map=ZONE_COLORS,
                    title="Fill Price Distribution by Zone",
                    labels={"fill_price": "Fill Price ($)"},
                )
                _chart_layout(fig, showlegend=False)
                st.plotly_chart(fig, use_container_width=True)

        # Zone pie chart + cost over time by side
        col_pie, col_side = st.columns(2)
        with col_pie:
            if "zone" in buys.columns:
                zone_counts = buys["zone"].value_counts().reset_index()
                zone_counts.columns = ["zone", "count"]
                fig = px.pie(
                    zone_counts, names="zone", values="count",
                    color="zone", color_discrete_map=ZONE_COLORS,
                    title="Fill Distribution by Zone",
                )
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        with col_side:
            if "side" in buys.columns and "fill_price" in buys.columns:
                fig = px.scatter(
                    buys, x="timestamp", y="fill_price",
                    color="side",
                    color_discrete_map={"YES": COLORS["green"], "NO": COLORS["red"]},
                    size="qty", size_max=12,
                    title="Fill Prices Over Time by Side",
                    labels={"fill_price": "Fill Price ($)"},
                )
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)


# ──────────────────────────────────────────────────────────────
# TAB 4: RISK & HEDGING
# ──────────────────────────────────────────────────────────────

with tab4:
    if buys.empty and windows.empty:
        st.info("No data available.")
    else:
        # Unhedged exposure + Max unhedged per window
        col_exp, col_max = st.columns(2)
        with col_exp:
            if "unhedged_usd" in buys.columns:
                fig = px.scatter(
                    buys.dropna(subset=["unhedged_usd"]),
                    x="timestamp", y="unhedged_usd",
                    color="side",
                    color_discrete_map={"YES": COLORS["green"], "NO": COLORS["red"]},
                    title="Unhedged Exposure at Each Fill",
                    labels={"unhedged_usd": "Unhedged ($)"},
                )
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        with col_max:
            if not windows.empty and "max_unhedged_usd" in windows.columns:
                fig = px.bar(
                    windows, x="timestamp", y="max_unhedged_usd",
                    title="Peak Unhedged Exposure per Window",
                    color_discrete_sequence=[COLORS["amber"]],
                    labels={"max_unhedged_usd": "Max Unhedged ($)"},
                )
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        # Time to hedge + Skew
        col_hedge, col_skew = st.columns(2)
        with col_hedge:
            hedge_data = buys.dropna(subset=["time_to_hedge_s"])
            if not hedge_data.empty:
                fig = px.histogram(
                    hedge_data, x="time_to_hedge_s", nbins=30,
                    title="Time-to-Hedge Distribution (seconds)",
                    color_discrete_sequence=[COLORS["cyan"]],
                    labels={"time_to_hedge_s": "Hedge Time (s)"},
                )
                fig.add_vline(x=30, line_dash="dash", line_color=COLORS["red"],
                              annotation_text="Panic (30s)")
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No hedge timing data available (first-leg buys don't have hedge times).")

        with col_skew:
            if "skew" in buys.columns:
                fig = px.line(
                    buys, x="timestamp", y="skew",
                    title="Position Skew Over Time",
                    color_discrete_sequence=[COLORS["purple"]],
                    labels={"skew": "Skew Ratio"},
                )
                fig.add_hline(y=0.20, line_dash="dash", line_color=COLORS["red"],
                              annotation_text="Max Skew (20%)")
                fig.add_hline(y=0.50, line_dash="dot", line_color="gray",
                              annotation_text="Balanced")
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        # Capital utilization + Cap exhausted
        col_cap, col_exh = st.columns(2)
        with col_cap:
            if not windows.empty and "total_capital" in windows.columns:
                fig = px.bar(
                    windows, x="timestamp", y="total_capital",
                    title="Capital Deployed per Window",
                    color_discrete_sequence=[COLORS["blue"]],
                    labels={"total_capital": "Capital ($)"},
                )
                fig.add_hline(y=100, line_dash="dash", line_color=COLORS["amber"],
                              annotation_text="Standard Cap ($100)")
                fig.add_hline(y=116, line_dash="dash", line_color=COLORS["red"],
                              annotation_text="Panic Cap ($116)")
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        with col_exh:
            if not windows.empty and "unmatched_qty" in windows.columns:
                fig = px.bar(
                    windows, x="timestamp", y="unmatched_qty",
                    color="unmatched_side",
                    title="Unmatched Quantity at Settlement",
                    labels={"unmatched_qty": "Unmatched Qty"},
                )
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)


# ──────────────────────────────────────────────────────────────
# TAB 5: MARKET MICROSTRUCTURE
# ──────────────────────────────────────────────────────────────

with tab5:
    if buys.empty:
        st.info("No buy execution data available.")
    else:
        # OBI distribution + Flow pressure
        col_obi, col_flow = st.columns(2)
        with col_obi:
            if "obi" in buys.columns:
                fig = px.histogram(
                    buys.dropna(subset=["obi"]),
                    x="obi", nbins=30, color="zone",
                    color_discrete_map=ZONE_COLORS,
                    title="OBI Distribution at Fill",
                    labels={"obi": "Order Book Imbalance"},
                )
                fig.add_vline(x=0.75, line_dash="dash", line_color=COLORS["red"],
                              annotation_text="Delay Threshold")
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        with col_flow:
            if "flow_pressure" in buys.columns:
                fig = px.histogram(
                    buys.dropna(subset=["flow_pressure"]),
                    x="flow_pressure", nbins=30, color="zone",
                    color_discrete_map=ZONE_COLORS,
                    title="Flow Pressure Distribution",
                    labels={"flow_pressure": "Flow Pressure"},
                )
                fig.add_vline(x=0.6, line_dash="dash", line_color=COLORS["red"],
                              annotation_text="Delay Threshold")
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        # Spread over time + Book depth
        col_spread, col_depth = st.columns(2)
        with col_spread:
            if "spread" in buys.columns:
                fig = px.scatter(
                    buys.dropna(subset=["spread"]),
                    x="timestamp", y="spread", color="zone",
                    color_discrete_map=ZONE_COLORS,
                    title="Bid-Ask Spread at Fill",
                    labels={"spread": "Spread ($)"},
                )
                fig.add_hline(y=0.02, line_dash="dot", line_color=COLORS["green"],
                              annotation_text="Tight")
                fig.add_hline(y=0.05, line_dash="dot", line_color=COLORS["red"],
                              annotation_text="Wide")
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        with col_depth:
            depth_cols = ["yes_bid_depth", "yes_ask_depth", "no_bid_depth", "no_ask_depth"]
            available = [c for c in depth_cols if c in buys.columns]
            if available:
                depth_melt = buys[["timestamp"] + available].melt(
                    id_vars="timestamp", var_name="depth_type", value_name="depth",
                )
                fig = px.line(
                    depth_melt, x="timestamp", y="depth", color="depth_type",
                    title="Book Depth at Fill",
                    labels={"depth": "Depth (shares)"},
                )
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        # Sweep detection + OBI vs PnL scatter
        col_sweep, col_obi_pnl = st.columns(2)
        with col_sweep:
            if "sweep" in buys.columns:
                sweep_counts = buys["sweep"].value_counts().reset_index()
                sweep_counts.columns = ["sweep", "count"]
                sweep_counts["sweep"] = sweep_counts["sweep"].map({True: "Sweep", False: "Normal"})
                fig = px.pie(
                    sweep_counts, names="sweep", values="count",
                    color="sweep",
                    color_discrete_map={"Sweep": COLORS["red"], "Normal": COLORS["green"]},
                    title="Sweep Detection Rate",
                )
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)

        with col_obi_pnl:
            if "obi" in buys.columns and "cost" in buys.columns:
                fig = px.scatter(
                    buys.dropna(subset=["obi", "cost"]),
                    x="obi", y="cost", color="side",
                    color_discrete_map={"YES": COLORS["green"], "NO": COLORS["red"]},
                    title="OBI vs Fill Cost",
                    labels={"obi": "OBI", "cost": "Fill Cost ($)"},
                )
                _chart_layout(fig)
                st.plotly_chart(fig, use_container_width=True)


# ──────────────────────────────────────────────────────────────
# TAB 6: RAW DATA
# ──────────────────────────────────────────────────────────────

with tab6:
    st.subheader("Pair Buys Log")
    if not buys.empty:
        st.dataframe(buys, use_container_width=True, height=400)
        st.download_button(
            "Download Filtered Buys CSV",
            buys.to_csv(index=False),
            "pair_buys_filtered.csv",
            mime="text/csv",
        )
    else:
        st.info("No buy data.")

    st.markdown("---")

    st.subheader("Window Settlements")
    if not windows.empty:
        st.dataframe(windows, use_container_width=True, height=400)
        st.download_button(
            "Download Filtered Windows CSV",
            windows.to_csv(index=False),
            "pair_windows_filtered.csv",
            mime="text/csv",
        )
    else:
        st.info("No window data.")


# ══════════════════════════════════════════════════════════════
# AUTO-REFRESH
# ══════════════════════════════════════════════════════════════

if auto_refresh:
    import time
    time.sleep(30)
    st.rerun()
