"""Streamlit dashboard for prediction_market_bot.

Run with:  streamlit run src/dashboard.py

Shows active markets (model prob, price, edge, signal, reason codes), open/closed
positions, bankroll, realized/unrealized PnL, drawdown, trade history, the current
bot MODE (PAPER/LIVE), and the emergency-stop status (with a button to engage it).
"""
from __future__ import annotations

import os
import sys

# Make `config` and `src.*` importable when run as a script via `streamlit run`.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from config import Config  # noqa: E402
from src.database import Database  # noqa: E402
from src.execution_engine import ExecutionEngine  # noqa: E402

st.set_page_config(page_title="Prediction Market Bot", layout="wide")
config = Config.from_env()
db = Database(config.db_path)

# --------------------------------------------------------------------------- #
# Header: mode + safety
# --------------------------------------------------------------------------- #
armed = config.is_live_trading_armed()
stopped = config.emergency_stop_engaged()
mode = "LIVE" if armed else "PAPER"

st.title("🌦️  Prediction Market Weather Bot")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Trading mode", mode)
c2.metric("Data source", config.data_source)
c3.metric("Live armed?", "YES" if armed else "no")
c4.metric("Emergency stop", "ENGAGED" if stopped else "clear")

if mode == "LIVE":
    st.error("LIVE TRADING IS ARMED — real orders can be placed within risk limits.")
else:
    st.success("Paper mode — no real orders can be placed. (Live needs explicit .env flags + keys.)")

cols = st.columns([1, 1, 4])
if cols[0].button("▶ Run one cycle"):
    with st.spinner("Running cycle..."):
        engine = ExecutionEngine(config)
        res = engine.run_cycle()
        st.session_state["last_rows"] = res.rows
        st.session_state["last_summary"] = res
    st.rerun()
if cols[1].button("⛔ Engage emergency stop"):
    with open(config.emergency_stop_file, "w", encoding="utf-8") as f:
        f.write("engaged via dashboard\n")
    st.rerun()

# --------------------------------------------------------------------------- #
# Portfolio summary (from latest pnl row)
# --------------------------------------------------------------------------- #
pnl_rows = db.pnl_history(limit=500)
st.subheader("Portfolio")
if pnl_rows:
    latest = pnl_rows[0]
    p1, p2, p3, p4, p5 = st.columns(5)
    p1.metric("Equity", f"${latest['bankroll']:.2f}")
    p2.metric("Realized PnL", f"${latest['realized_pnl']:.2f}")
    p3.metric("Unrealized PnL", f"${latest['unrealized_pnl']:.2f}")
    p4.metric("Drawdown", f"{latest['drawdown']*100:.1f}%")
    p5.metric("Open positions", int(latest["open_positions"]))
    df_pnl = pd.DataFrame(list(reversed(pnl_rows)))
    if not df_pnl.empty:
        st.line_chart(df_pnl.set_index("ts")[["bankroll", "realized_pnl"]])
else:
    st.info("No cycles recorded yet. Click 'Run one cycle'.")

# --------------------------------------------------------------------------- #
# Forward-test progress toward the edge-proven gate + notify status
# --------------------------------------------------------------------------- #
st.subheader("Forward-test progress (toward live)")
from src.performance import compute_performance  # noqa: E402
from src.live_gate import edge_proven  # noqa: E402

perf = compute_performance(db, config)
allowed, reason = edge_proven(db, config)
g1, g2, g3, g4 = st.columns(4)
g1.metric("Settled trades", f"{perf.n_settled}/{config.edge_proven_min_trades}")
g2.metric("Win rate", f"{perf.win_rate*100:.1f}%")
g3.metric("ROI", f"{perf.roi*100:+.1f}%")
g4.metric("Edge vs market",
          f"{perf.edge_vs_market:+.3f}" if perf.edge_vs_market is not None else "n/a")
st.progress(min(1.0, perf.n_settled / max(1, config.edge_proven_min_trades)))
(st.success if allowed else st.warning)(
    f"Edge-proven gate: {'PROVEN' if allowed else 'NOT YET'} — {reason}")

webhook_set = bool((config.alert_webhook_url or "").strip())
st.caption(f"Discord push: {'configured' if webhook_set else 'not configured'} · "
           f"events = {config.notify_events}")

# --------------------------------------------------------------------------- #
# Active markets (from the last cycle in this session)
# --------------------------------------------------------------------------- #
st.subheader("Active markets — last cycle")
rows = st.session_state.get("last_rows")
if rows:
    df = pd.DataFrame(rows)
    df["reasons"] = df["reasons"].apply(lambda r: ", ".join(r) if isinstance(r, list) else r)
    show_cols = [c for c in ["ticker", "model_probability", "confidence", "yes_bid", "yes_ask",
                             "spread", "edge_net", "signal", "side", "action", "reasons"] if c in df.columns]
    st.dataframe(df[show_cols], use_container_width=True, hide_index=True)
else:
    st.info("Run a cycle to populate the market table.")

# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #
left, right = st.columns(2)
with left:
    st.subheader("Open positions")
    op = db.open_positions()
    st.dataframe(pd.DataFrame(op) if op else pd.DataFrame(columns=["ticker"]),
                 use_container_width=True, hide_index=True)
with right:
    st.subheader("Closed positions")
    cp = db.query("SELECT * FROM positions WHERE status='closed' ORDER BY id DESC LIMIT 100")
    st.dataframe(pd.DataFrame(cp) if cp else pd.DataFrame(columns=["ticker"]),
                 use_container_width=True, hide_index=True)

# --------------------------------------------------------------------------- #
# Trade history + recent signals
# --------------------------------------------------------------------------- #
st.subheader("Trade history")
trades = db.recent_trades(limit=200)
st.dataframe(pd.DataFrame(trades) if trades else pd.DataFrame(columns=["ticker"]),
             use_container_width=True, hide_index=True)

st.subheader("Recent signals")
sigs = db.recent_signals(limit=100)
st.dataframe(pd.DataFrame(sigs) if sigs else pd.DataFrame(columns=["ticker"]),
             use_container_width=True, hide_index=True)

st.caption("Reddit PnL claims are NOT proof of edge. Paper-trade and verify "
           "calibration on real settled outcomes before risking money.")
