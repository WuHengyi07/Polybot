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


def _realized_by_position(trades, starting_bankroll):
    """Cumulative realized P&L per position_id, reconstructed from the trade log.

    The positions table doesn't persist realized P&L when a position closes (the
    close path only writes status/closed_ts), so the stored realized_pnl is 0 for
    closed rows. We recompute it the same way performance.py does — from the trades.
    """
    from src.position_manager import PositionManager, position_id
    pm = PositionManager(starting_bankroll)
    out = {}
    for t in trades:
        a, tk, sd = t["action"], t["ticker"], t["side"]
        c = int(t["contracts"]); pr = float(t["price"])
        fee = float(t.get("fee") or 0.0); ts = t.get("ts", "")
        tr = None
        if a == "open":
            pm.apply_open(tk, sd, c, pr, fee, "weather", ts)
        elif a == "close":
            tr = pm.apply_close(tk, sd, c, pr, fee, ts)
        elif a == "settle":
            tr = pm.apply_settle(tk, sd, outcome_yes=(pr >= 0.5), ts=ts)
        if tr:
            pid = position_id(tk, sd)
            out[pid] = round(out.get(pid, 0.0) + tr["realized"], 4)
    return out


@st.cache_data(ttl=60, show_spinner=False)
def _live_prices():
    """{ticker: (yes_bid, no_bid)} from the live exchange, cached 60s to limit API load.
    Used to mark open positions to market for unrealized P&L. Empty dict on any error."""
    try:
        from src.market_client import build_market_client
        client = build_market_client(config)
        return {m.ticker: (float(m.yes_bid), float(m.no_bid)) for m in client.list_markets()}
    except Exception:
        return {}


def _unrealized(row, prices):
    """Mark an open position to market: contracts * (current_exit_price - avg_price)."""
    px = prices.get(row["ticker"])
    if not px:
        return None
    exit_price = px[0] if row["side"] == "yes" else px[1]  # sell yes@yes_bid, no@no_bid
    return round(int(row["contracts"]) * (exit_price - float(row["avg_price"])), 4)


def _move_after_id(df, col):
    """Reorder so `col` is the second column, right after `id`."""
    if col not in df.columns:
        return df
    cols = [c for c in df.columns if c != col]
    insert_at = cols.index("id") + 1 if "id" in cols else 0
    cols.insert(insert_at, col)
    return df[cols]


def _closed_stats(paper_trades, closed_pids, starting_bankroll):
    """Win-rate / ROI / realized over CLOSED positions (the testing-phase view).

    Unlike performance.compute_performance (settled-only, for the gate), this counts
    every closed position so the dashboard numbers move during testing. Returns the
    realized-per-position map too, so the Closed-positions table can reuse it.
    """
    realized = _realized_by_position(paper_trades, starting_bankroll)
    stake = {}
    for t in paper_trades:
        if t["action"] == "open":
            pid = f'{t["ticker"]}:{t["side"]}'
            stake[pid] = stake.get(pid, 0.0) + int(t["contracts"]) * float(t["price"])
    n = len(closed_pids)
    wins = sum(1 for p in closed_pids if realized.get(p, 0.0) > 0)
    total_real = sum(realized.get(p, 0.0) for p in closed_pids)
    total_stake = sum(stake.get(p, 0.0) for p in closed_pids)
    return {
        "n": n,
        "win_rate": (wins / n) if n else 0.0,
        "roi": (total_real / total_stake) if total_stake > 1e-9 else 0.0,
        "realized": round(total_real, 2),
        "realized_map": realized,
    }


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
        # Real datetime axis in the dashboard tz (readable), with a range selector.
        df_pnl["t"] = (pd.to_datetime(df_pnl["ts"], utc=True, errors="coerce",
                                      format="ISO8601")
                       .dt.tz_convert(_DASH_TZ).dt.tz_localize(None))
        _ranges = {"1D": 1, "1W": 7, "1M": 30, "Max": None}
        _pick = st.radio("Range", list(_ranges), index=3, horizontal=True,
                         key="pnl_range", label_visibility="collapsed")
        _days = _ranges[_pick]
        _plot = df_pnl
        if _days is not None:
            _cutoff = df_pnl["t"].max() - pd.Timedelta(days=_days)
            _plot = df_pnl[df_pnl["t"] >= _cutoff]
        _series = st.radio("Series", ["Bankroll", "Realized P&L"], index=0, horizontal=True,
                           key="pnl_series", label_visibility="collapsed")
        _col = "bankroll" if _series == "Bankroll" else "realized_pnl"
        st.line_chart(_plot.set_index("t")[[_col]])
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
_paper_trades = db.query("SELECT * FROM trades WHERE mode='paper' ORDER BY id")
_closed_pids = [r["position_id"]
                for r in db.query("SELECT position_id FROM positions WHERE status='closed'")]
_cs = _closed_stats(_paper_trades, _closed_pids, config.starting_bankroll)
_target = config.edge_proven_min_trades
g1, g2, g3, g4 = st.columns(4)
# Testing-phase view: stats are over CLOSED trades so the numbers move. The real
# edge-proven gate still uses SETTLED trades underneath (shown in the banner).
g1.metric("Closed trades", f"{_cs['n']}/{_target}")
g2.metric("Win rate", f"{_cs['win_rate']*100:.1f}%")
g3.metric("ROI", f"{_cs['roi']*100:+.1f}%")
g4.metric("Edge vs market",
          f"{perf.edge_vs_market:+.3f}" if perf.edge_vs_market is not None else "n/a")
st.progress(min(1.0, _cs["n"] / max(1, _target)))
(st.success if allowed else st.warning)(
    f"Edge-proven gate: {'PROVEN' if allowed else 'NOT YET'} — {_cs['n']}/{_target} closed, "
    f"realized ${_cs['realized']:+.2f}. The real gate needs SETTLED outcomes "
    f"({perf.n_settled} settled so far) and the model to beat the market.")

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
    if op:
        df_op = pd.DataFrame(op)
        prices = _live_prices()  # live marks (cached 60s); empty if the fetch fails
        df_op["unrealized_pnl"] = df_op.apply(lambda r: _unrealized(r, prices), axis=1)
        df_op = _move_after_id(df_op, "unrealized_pnl")
        _localize(df_op, ["opened_ts", "closed_ts"])
        st.dataframe(df_op, use_container_width=True, hide_index=True)
    else:
        st.dataframe(pd.DataFrame(columns=["ticker"]),
                     use_container_width=True, hide_index=True)
with right:
    st.subheader("Closed positions")
    cp = db.query("SELECT * FROM positions WHERE status='closed' ORDER BY id DESC LIMIT 100")
    if cp:
        df_cp = pd.DataFrame(cp)
        # The stored realized_pnl is 0 for closed rows (not persisted on close);
        # reuse the realized-per-position map computed above for the stats panel.
        df_cp["realized_pnl"] = df_cp["position_id"].map(_cs["realized_map"]).fillna(df_cp["realized_pnl"])
        df_cp = _move_after_id(df_cp, "realized_pnl")
        _localize(df_cp, ["opened_ts", "closed_ts"])
        st.dataframe(df_cp, use_container_width=True, hide_index=True)
    else:
        st.dataframe(pd.DataFrame(columns=["ticker"]),
                     use_container_width=True, hide_index=True)

# --------------------------------------------------------------------------- #
# Trade history + recent signals
# --------------------------------------------------------------------------- #
st.subheader("Trade history")
trades = db.recent_trades(limit=200)
if trades:
    df_tr = _localize(pd.DataFrame(trades), ["ts"])

    def _color_trade(row):
        a = str(row.get("action", ""))
        if a == "open":            # buy -> green
            bg = "background-color: rgba(0, 160, 0, 0.18)"
        elif a in ("close", "settle"):  # sell / resolve -> red
            bg = "background-color: rgba(200, 0, 0, 0.18)"
        else:
            bg = ""
        return [bg] * len(row)

    st.dataframe(df_tr.style.apply(_color_trade, axis=1),
                 use_container_width=True, hide_index=True)
else:
    st.dataframe(pd.DataFrame(columns=["ticker"]),
                 use_container_width=True, hide_index=True)

st.subheader("Recent signals")
sigs = db.recent_signals(limit=100)
st.dataframe(_localize(pd.DataFrame(sigs), ["ts"]) if sigs else pd.DataFrame(columns=["ticker"]),
             use_container_width=True, hide_index=True)

st.caption(f"Times shown in {_DASH_TZ} (stored in UTC). "
           "Reddit PnL claims are NOT proof of edge — paper-trade and verify "
           "calibration on real settled outcomes before risking money.")
