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

# Timestamps are STORED in UTC; convert to the configured zone for DISPLAY only.
_DASH_TZ = getattr(config, "dashboard_timezone", "America/New_York") or "UTC"


def _fmt_ts(ts):
    """Format a tz-aware Timestamp like 'Jun 4, 2026 10:24:21 PM EDT'. No leading zeros
    on day/hour; platform-independent (avoids %-d/%-I, which fail on Windows)."""
    hour12 = ts.hour % 12 or 12
    ampm = "AM" if ts.hour < 12 else "PM"
    return (f"{ts.strftime('%b')} {ts.day}, {ts.year} "
            f"{hour12}:{ts.minute:02d}:{ts.second:02d} {ampm} {ts.strftime('%Z')}")


def _localize(df, cols):
    """Render UTC ISO timestamp columns in `_DASH_TZ` (e.g. US Eastern). Display only."""
    if df is None or getattr(df, "empty", True):
        return df
    for c in cols:
        if c in df.columns:
            try:
                s = pd.to_datetime(df[c], utc=True, errors="coerce",
                                   format="ISO8601").dt.tz_convert(_DASH_TZ)
                df[c] = s.map(lambda x: _fmt_ts(x) if pd.notna(x) else "")
            except Exception:  # bad tz / unparseable -> leave the raw UTC value
                pass
    return df

# Auto-reload the whole page every N seconds so an unattended dashboard reflects
# the service's latest DB writes without a manual refresh. Dependency-free and
# version-agnostic: a tiny script in a 0-height component reloads the parent page.
# Set DASHBOARD_REFRESH_SECONDS=0 to disable.
_refresh = getattr(config, "dashboard_refresh_seconds", 5)
if _refresh and _refresh > 0:
    from streamlit.components.v1 import html as _html
    _html(
        f"<script>setTimeout(function(){{window.parent.location.reload();}}, "
        f"{int(_refresh) * 1000});</script>",
        height=0,
    )

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
        _localize(df_pnl, ["ts"])
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
    st.dataframe(_localize(pd.DataFrame(op), ["opened_ts", "closed_ts"]) if op
                 else pd.DataFrame(columns=["ticker"]),
                 use_container_width=True, hide_index=True)
with right:
    st.subheader("Closed positions")
    cp = db.query("SELECT * FROM positions WHERE status='closed' ORDER BY id DESC LIMIT 100")
    st.dataframe(_localize(pd.DataFrame(cp), ["opened_ts", "closed_ts"]) if cp
                 else pd.DataFrame(columns=["ticker"]),
                 use_container_width=True, hide_index=True)

# --------------------------------------------------------------------------- #
# Trade history + recent signals
# --------------------------------------------------------------------------- #
st.subheader("Trade history")
trades = db.recent_trades(limit=200)
st.dataframe(_localize(pd.DataFrame(trades), ["ts"]) if trades else pd.DataFrame(columns=["ticker"]),
             use_container_width=True, hide_index=True)

st.subheader("Recent signals")
sigs = db.recent_signals(limit=100)
st.dataframe(_localize(pd.DataFrame(sigs), ["ts"]) if sigs else pd.DataFrame(columns=["ticker"]),
             use_container_width=True, hide_index=True)

st.caption(f"Times shown in {_DASH_TZ} (stored in UTC). "
           "Reddit PnL claims are NOT proof of edge — paper-trade and verify "
           "calibration on real settled outcomes before risking money.")
