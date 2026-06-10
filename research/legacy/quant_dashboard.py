"""LEGACY — superseded by streamlit_dashboard.py (repo root). Kept for reference.
Requires scikit-learn, which is intentionally NOT in requirements.txt."""
import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import LabelEncoder

st.set_page_config(page_title="PolyQuant God-Mode", layout="wide", page_icon="⚡")
st.title("⚡ PolyQuant Advanced Analytics Engine")

uploaded_file = st.sidebar.file_uploader("Upload pair_report.xlsx", type=["xlsx"])

if uploaded_file:
    try:
        st.sidebar.success("File loaded successfully! Crunching data...")

        # ── 1. Dynamic sheet detection ─────────────────────────────────────
        xl = pd.ExcelFile(uploaded_file)
        all_sheets = xl.sheet_names

        exec_sheet = next((s for s in all_sheets if any(k in s for k in ['Execution', 'execution', 'Fill', 'Buy', 'buy', 'Log', 'log'])), None)
        win_sheet  = next((s for s in all_sheets if any(k in s for k in ['Window', 'window'])), None)
        if not win_sheet:
            win_sheet = next((s for s in all_sheets if any(k in s for k in ['Summary', 'summary'])), None)

        if not exec_sheet or not win_sheet:
            st.error(
                f"Could not find the expected sheets. "
                f"Sheets found: **{all_sheets}**. "
                f"Expected one containing 'Execution'/'Buy' and one containing 'Window'."
            )
            st.stop()

        # ── 2. Read raw to locate header rows ─────────────────────────────
        df_exec_raw = pd.read_excel(uploaded_file, sheet_name=exec_sheet, header=None)
        df_win_raw  = pd.read_excel(uploaded_file, sheet_name=win_sheet,  header=None)

        def _find_header_row(df_raw, keywords):
            """Return the first row index containing any keyword, else 0."""
            matches = df_raw[df_raw.astype(str).apply(
                lambda x: x.str.contains('|'.join(keywords), na=False)
            ).any(axis=1)].index
            return int(matches[0]) if len(matches) > 0 else 0

        exec_idx = _find_header_row(df_exec_raw, ['Timestamp', 'timestamp', 'Time', 'Token', 'token'])
        win_idx  = _find_header_row(df_win_raw,  ['Window Start', 'Window', 'Winner', 'Net PnL', 'PnL'])

        exec_log = pd.read_excel(uploaded_file, sheet_name=exec_sheet, header=exec_idx)
        win_sum  = pd.read_excel(uploaded_file, sheet_name=win_sheet,  header=win_idx)

        st.sidebar.info(f"Sheets: `{exec_sheet}` / `{win_sheet}`")
        with st.sidebar.expander("🔍 Detected columns"):
            st.write("**Execution sheet:**", list(exec_log.columns))
            st.write("**Window sheet:**", list(win_sum.columns))

        # ── 3. Strip whitespace from all column names ──────────────────────
        exec_log.columns = exec_log.columns.str.strip()
        win_sum.columns  = win_sum.columns.str.strip()

        # ── 4. Normalize execution log column names (old format → new) ─────
        exec_col_map = {
            'Market':     'Token',
            'Qty':        'Shares',
            'Ask Price':  'Quoted Ask',
            'Fill Price': 'VWAP Fill',
        }
        exec_log = exec_log.rename(columns={k: v for k, v in exec_col_map.items() if k in exec_log.columns})
        if 'Zone' not in exec_log.columns:
            exec_log['Zone'] = 'N/A'

        # ── 5. Normalize window summary column names (old format → new) ────
        win_col_map = {
            'Pair Cost':  'Avg Pair Cost',
            'Time':       'Window Start',
        }
        win_sum = win_sum.rename(columns={k: v for k, v in win_col_map.items() if k in win_sum.columns and k not in win_sum.columns})
        # Safer rename: only if target doesn't already exist
        for old, new in win_col_map.items():
            if old in win_sum.columns and new not in win_sum.columns:
                win_sum = win_sum.rename(columns={old: new})
        if 'Window Start' not in win_sum.columns:
            win_sum['Window Start'] = range(len(win_sum))

        # ── 6. Clean macro data ────────────────────────────────────────────
        win_sum = win_sum.dropna(subset=['Winner', 'Pairs', 'Net PnL'])
        win_sum['Net PnL'] = win_sum['Net PnL'].astype(str).str.replace('$', '', regex=False).str.replace('+', '', regex=False).astype(float)
        win_sum['Pairs']   = win_sum['Pairs'].astype(str).str.replace(',', '', regex=False).astype(float)

        if 'Avg Pair Cost' in win_sum.columns:
            win_sum['Avg Pair Cost'] = win_sum['Avg Pair Cost'].astype(str).str.replace('$', '', regex=False).astype(float)
        else:
            win_sum['Avg Pair Cost'] = np.nan

        win_sum['Cumulative_PnL'] = win_sum['Net PnL'].cumsum()
        win_sum['Win']   = win_sum['Net PnL'] > 0
        win_sum['Peak']  = win_sum['Cumulative_PnL'].cummax()
        win_sum['Drawdown'] = win_sum['Cumulative_PnL'] - win_sum['Peak']

        max_drawdown = win_sum['Drawdown'].min()
        gross_profit = win_sum[win_sum['Net PnL'] > 0]['Net PnL'].sum()
        gross_loss   = abs(win_sum[win_sum['Net PnL'] < 0]['Net PnL'].sum())
        profit_factor = gross_profit / gross_loss if gross_loss != 0 else float('inf')

        # ── 7. Clean micro data ────────────────────────────────────────────
        exec_log = exec_log.dropna(subset=['Token', 'Shares', 'Quoted Ask', 'VWAP Fill'])
        exec_log['Quoted Ask'] = exec_log['Quoted Ask'].astype(str).str.replace('$', '', regex=False).astype(float)
        exec_log['VWAP Fill']  = exec_log['VWAP Fill'].astype(str).str.replace('$', '', regex=False).astype(float)

        slip_cols = [col for col in exec_log.columns if 'Slippage' in col]
        if slip_cols:
            exec_log['Slippage'] = exec_log[slip_cols[0]].astype(str).str.replace('+', '', regex=False).str.replace('¢', '', regex=False).astype(float)
        else:
            exec_log['Slippage'] = (exec_log['VWAP Fill'] - exec_log['Quoted Ask']) * 100

        # ══════════════════════════════════════════════════════════════════
        # KPI ROW
        # ══════════════════════════════════════════════════════════════════
        st.markdown("### 🏛 Risk & Return Metrics")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Net PnL",  f"${win_sum['Net PnL'].sum():.2f}")
        c2.metric("Win Rate",       f"{(win_sum['Win'].mean() * 100):.1f}%")
        c3.metric("Profit Factor",  f"{profit_factor:.2f}x")
        c4.metric("Max Drawdown",   f"${max_drawdown:.2f}")
        c5.metric("Avg Pair Cost",  f"${win_sum['Avg Pair Cost'].mean():.3f}" if win_sum['Avg Pair Cost'].notna().any() else "N/A")

        st.markdown("---")

        # ══════════════════════════════════════════════════════════════════
        # ROW 1: EQUITY CURVE + UNDERWATER CURVE
        # ══════════════════════════════════════════════════════════════════
        col_eq, col_dd = st.columns(2)
        with col_eq:
            fig_eq = px.area(win_sum, y='Cumulative_PnL', title="📈 Equity Curve",
                             color_discrete_sequence=['#00E676'])
            st.plotly_chart(fig_eq, use_container_width=True)
        with col_dd:
            fig_dd = px.area(win_sum, y='Drawdown', title="📉 Underwater Curve (Drawdowns)",
                             color_discrete_sequence=['#FF1744'])
            st.plotly_chart(fig_dd, use_container_width=True)

        st.markdown("---")

        # ══════════════════════════════════════════════════════════════════
        # ROW 2: PAIR COST SCATTER + SLIPPAGE VIOLIN
        # ══════════════════════════════════════════════════════════════════
        col_scatter, col_density = st.columns(2)
        with col_scatter:
            st.markdown("#### 🎯 Strategy Edge: Pair Cost vs. PnL")
            if win_sum['Avg Pair Cost'].notna().any():
                fig_scatter = px.scatter(
                    win_sum, x='Avg Pair Cost', y='Net PnL',
                    color='Win', size='Pairs',
                    hover_data=['Window Start'],
                    title="Win/Loss Clustering (Bubble Size = Volume)",
                    color_discrete_map={True: '#00E676', False: '#FF1744'},
                )
                fig_scatter.add_hline(y=0, line_dash="dot", line_color="white")
                st.plotly_chart(fig_scatter, use_container_width=True)
            else:
                st.info("Avg Pair Cost data not available in this report format.")

        with col_density:
            st.markdown("#### 🌪 Slippage Probability Density")
            fig_kde = px.violin(
                exec_log, x='Zone', y='Slippage', color='Zone',
                box=True, points="all",
                title="Statistical Distribution of Execution Bleed",
            )
            st.plotly_chart(fig_kde, use_container_width=True)

        st.markdown("---")

        # ══════════════════════════════════════════════════════════════════
        # ML SLIPPAGE PREDICTOR
        # ══════════════════════════════════════════════════════════════════
        st.markdown("### 🧠 ML Slippage Predictor (Random Forest)")
        ml_df = exec_log[['Shares', 'Quoted Ask', 'Zone', 'Slippage']].copy()
        le = LabelEncoder()
        ml_df['Zone_Encoded'] = le.fit_transform(ml_df['Zone'])

        rf_model = RandomForestRegressor(n_estimators=100, random_state=42)
        rf_model.fit(ml_df[['Shares', 'Quoted Ask', 'Zone_Encoded']], ml_df['Slippage'])

        importance = pd.DataFrame({
            'Feature': ['Order Size (Shares)', 'Quoted Price', 'Execution Zone'],
            'Impact on Slippage (%)': rf_model.feature_importances_ * 100,
        }).sort_values(by='Impact on Slippage (%)', ascending=True)

        fig_ml = px.bar(importance, x='Impact on Slippage (%)', y='Feature', orientation='h',
                        title="What variables drive the most slippage?",
                        color_discrete_sequence=['#FF4B4B'])
        st.plotly_chart(fig_ml, use_container_width=True)
        st.success("Analysis complete.")

    except Exception as e:
        st.error(f"Error parsing the Excel file. Details: {e}")
else:
    st.info("Awaiting pair_report.xlsx upload...")
