"""Live performance metrics from settled trades — the real profitability test.

The headline number is NOT PnL (too noisy on small samples) but **Brier-vs-market**:
does the model's probability beat the market's implied probability as a forecaster?
If model Brier >= market Brier, you have no forecasting edge, full stop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import calibration
from .position_manager import PositionManager
from .utils import get_logger

log = get_logger("performance")


@dataclass
class Performance:
    n_settled: int = 0
    win_rate: float = 0.0
    realized_pnl: float = 0.0
    roi: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    brier_model: Optional[float] = None
    brier_market: Optional[float] = None
    calib_error_model: Optional[float] = None
    edge_vs_market: Optional[float] = None  # brier_market - brier_model (>0 means we beat it)
    n_scored: int = 0


def _replay_realized(trades: List[dict], starting_bankroll: float):
    """Replay the trade log, capturing per-event realized PnL and the equity path."""
    pm = PositionManager(starting_bankroll)
    realized_events: List[float] = []
    equity_path: List[float] = [starting_bankroll]
    for t in trades:
        a, tk, sd = t["action"], t["ticker"], t["side"]
        c, pr, fee, ts = int(t["contracts"]), float(t["price"]), float(t.get("fee") or 0.0), t.get("ts", "")
        if a == "open":
            pm.apply_open(tk, sd, c, pr, fee, "weather", ts)
        elif a == "close":
            tr = pm.apply_close(tk, sd, c, pr, fee, ts)
            if tr:
                realized_events.append(tr["realized"])
                equity_path.append(equity_path[-1] + tr["realized"])
        elif a == "settle":
            won = pr >= 0.5
            outcome_yes = won if sd == "yes" else (not won)
            tr = pm.apply_settle(tk, sd, outcome_yes, ts)
            if tr:
                realized_events.append(tr["realized"])
                equity_path.append(equity_path[-1] + tr["realized"])
    return pm, realized_events, equity_path


def _max_drawdown(equity_path: List[float]) -> float:
    peak = equity_path[0] if equity_path else 0.0
    mdd = 0.0
    for e in equity_path:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return round(mdd, 4)


def compute_performance(db, config, mode: str = "paper") -> Performance:
    trades = db.query("SELECT * FROM trades WHERE mode=? ORDER BY id", (mode,))
    settle_trades = [t for t in trades if t["action"] == "settle"]
    open_trades = [t for t in trades if t["action"] == "open"]
    perf = Performance(n_settled=len(settle_trades))
    if not settle_trades:
        return perf

    pm, realized_events, equity_path = _replay_realized(trades, config.starting_bankroll)
    wins = [r for r in realized_events if r > 0]
    losses = [r for r in realized_events if r < 0]
    perf.realized_pnl = round(sum(realized_events), 2)
    perf.win_rate = round(len(wins) / len(realized_events), 4) if realized_events else 0.0
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    perf.profit_factor = round(gross_win / gross_loss, 3) if gross_loss > 1e-9 else float("inf")
    deployed = sum(-float(t.get("cash_flow") or 0.0) for t in open_trades if float(t.get("cash_flow") or 0.0) < 0)
    perf.roi = round(perf.realized_pnl / deployed, 4) if deployed > 1e-9 else 0.0
    perf.max_drawdown = _max_drawdown(equity_path)

    # --- Brier vs market (the real edge test) ---
    settlements = db.get_settlements()
    model_probs, market_probs, outcomes_m, outcomes_k = [], [], [], []
    for s in settlements:
        if s.get("mode") and s["mode"] != mode:
            continue
        outcome = int(s["outcome_yes"])
        if s.get("model_probability_yes") is not None:
            model_probs.append(float(s["model_probability_yes"]))
            outcomes_m.append(outcome)
        if s.get("market_implied_yes") is not None:
            market_probs.append(float(s["market_implied_yes"]))
            outcomes_k.append(outcome)
    if model_probs:
        perf.brier_model = round(calibration.brier_score(model_probs, outcomes_m), 4)
        perf.calib_error_model = round(calibration.calibration_error(model_probs, outcomes_m), 4)
        perf.n_scored = len(model_probs)
    if market_probs:
        perf.brier_market = round(calibration.brier_score(market_probs, outcomes_k), 4)
    if perf.brier_model is not None and perf.brier_market is not None:
        perf.edge_vs_market = round(perf.brier_market - perf.brier_model, 4)  # >0 => model beats market
    return perf


# --------------------------------------------------------------------------- #
# Closed-trade stats (testing-phase view) — counts CLOSED positions (early closes
# AND settlements), distinct from the settled-only gate metrics above.
# --------------------------------------------------------------------------- #
@dataclass
class ClosedStats:
    n: int = 0
    win_rate: float = 0.0
    roi: float = 0.0
    realized: float = 0.0
    realized_by_position: Dict[str, float] = field(default_factory=dict)


def realized_by_position(trades: List[dict], starting_bankroll: float) -> Dict[str, float]:
    """Cumulative realized P&L per position_id, reconstructed from the trade log
    (positions.realized_pnl isn't persisted on close, so we replay the trades)."""
    from .position_manager import position_id
    pm = PositionManager(starting_bankroll)
    out: Dict[str, float] = {}
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


def compute_closed_stats(db, config, mode: str = "paper") -> ClosedStats:
    """Win-rate / ROI / realized over CLOSED positions (closes AND settlements)."""
    from .position_manager import position_id
    trades = db.query("SELECT * FROM trades WHERE mode=? ORDER BY id", (mode,))
    closed_pids = [r["position_id"] for r in db.query(
        "SELECT position_id FROM positions WHERE status='closed' AND mode=?", (mode,))]
    realized = realized_by_position(trades, config.starting_bankroll)
    stake: Dict[str, float] = {}
    for t in trades:
        if t["action"] == "open":
            pid = position_id(t["ticker"], t["side"])
            stake[pid] = stake.get(pid, 0.0) + int(t["contracts"]) * float(t["price"])
    n = len(closed_pids)
    wins = sum(1 for p in closed_pids if realized.get(p, 0.0) > 0)
    total_real = sum(realized.get(p, 0.0) for p in closed_pids)
    total_stake = sum(stake.get(p, 0.0) for p in closed_pids)
    return ClosedStats(
        n=n,
        win_rate=round(wins / n, 4) if n else 0.0,
        roi=round(total_real / total_stake, 4) if total_stake > 1e-9 else 0.0,
        realized=round(total_real, 2),
        realized_by_position=realized,
    )


# --------------------------------------------------------------------------- #
# Segment diagnostics — where does the model beat vs. lose to the market?
# --------------------------------------------------------------------------- #
def _replay_realized_by_ticker(trades, starting_bankroll):
    pm = PositionManager(starting_bankroll)
    by_ticker = {}
    for t in trades:
        a, tk, sd = t["action"], t["ticker"], t["side"]
        c, pr, fee, ts = int(t["contracts"]), float(t["price"]), float(t.get("fee") or 0.0), t.get("ts", "")
        if a == "open":
            pm.apply_open(tk, sd, c, pr, fee, "weather", ts)
        elif a == "close":
            tr = pm.apply_close(tk, sd, c, pr, fee, ts)
            if tr:
                by_ticker[tk] = by_ticker.get(tk, 0.0) + tr["realized"]
        elif a == "settle":
            won = pr >= 0.5
            tr = pm.apply_settle(tk, sd, won if sd == "yes" else (not won), ts)
            if tr:
                by_ticker[tk] = by_ticker.get(tk, 0.0) + tr["realized"]
    return by_ticker


def _stake_by_ticker(trades):
    stake = {}
    for t in trades:
        if t["action"] == "open":
            cf = float(t.get("cash_flow") or 0.0)
            if cf < 0:
                stake[t["ticker"]] = stake.get(t["ticker"], 0.0) + (-cf)
    return stake


def _city_of(ticker: str) -> str:
    from .settlement import _city_from_ticker
    c = _city_from_ticker(ticker)
    return c.code if c else "?"


def _price_bucket(p) -> str:
    if p is None:
        return "?"
    p = float(p)
    if p < 0.25:
        return "01-25c"
    if p < 0.50:
        return "25-50c"
    if p < 0.75:
        return "50-75c"
    return "75-99c"


def performance_by_segment(db, config, mode: str = "paper"):
    """Per-segment (city: / price:) realized ROI + Brier-vs-market."""
    trades = db.query("SELECT * FROM trades WHERE mode=? ORDER BY id", (mode,))
    realized = _replay_realized_by_ticker(trades, config.starting_bankroll)
    stake = _stake_by_ticker(trades)
    segs: Dict[str, dict] = {}
    for s in db.get_settlements():
        if s.get("mode") and s["mode"] != mode:
            continue
        tk, outcome = s["ticker"], int(s["outcome_yes"])
        for key in (f"city:{_city_of(tk)}", f"price:{_price_bucket(s.get('market_implied_yes'))}"):
            seg = segs.setdefault(key, {"n": 0, "realized": 0.0, "stake": 0.0,
                                        "mp": [], "om": [], "kp": [], "ok": []})
            seg["n"] += 1
            seg["realized"] += realized.get(tk, 0.0)
            seg["stake"] += stake.get(tk, 0.0)
            if s.get("model_probability_yes") is not None:
                seg["mp"].append(float(s["model_probability_yes"]))
                seg["om"].append(outcome)
            if s.get("market_implied_yes") is not None:
                seg["kp"].append(float(s["market_implied_yes"]))
                seg["ok"].append(outcome)
    out = {}
    for key, seg in segs.items():
        bm = round(calibration.brier_score(seg["mp"], seg["om"]), 4) if seg["mp"] else None
        bk = round(calibration.brier_score(seg["kp"], seg["ok"]), 4) if seg["kp"] else None
        out[key] = {
            "n": seg["n"],
            "roi": round(seg["realized"] / seg["stake"], 4) if seg["stake"] > 1e-9 else 0.0,
            "realized": round(seg["realized"], 2),
            "brier_model": bm, "brier_market": bk,
            "edge_vs_market": (round(bk - bm, 4) if (bm is not None and bk is not None) else None),
        }
    return out


def unprofitable_segments(db, config, mode: str = "paper", min_n: int = 20):
    """Cities whose model Brier loses to the market over a meaningful sample."""
    bad = set()
    for key, s in performance_by_segment(db, config, mode).items():
        if (key.startswith("city:") and s["n"] >= min_n
                and s["edge_vs_market"] is not None and s["edge_vs_market"] < 0):
            bad.add(key.split(":", 1)[1])
    return bad


def format_segments(segs: dict) -> str:
    if not segs:
        return ""
    lines = ["", "  -- by segment (edge_vs_market > 0 means model beats market) --",
             f"  {'segment':<14}{'n':>4}{'roi':>9}{'mBrier':>9}{'kBrier':>9}{'edge':>9}"]
    for key in sorted(segs):
        s = segs[key]
        mb = f"{s['brier_model']:.3f}" if s["brier_model"] is not None else "  -"
        kb = f"{s['brier_market']:.3f}" if s["brier_market"] is not None else "  -"
        ev = f"{s['edge_vs_market']:+.3f}" if s["edge_vs_market"] is not None else "   -"
        lines.append(f"  {key:<14}{s['n']:>4}{s['roi']*100:>8.1f}%{mb:>9}{kb:>9}{ev:>9}")
    return "\n".join(lines)


def format_report(perf: Performance) -> str:
    pf = "inf" if perf.profit_factor == float("inf") else f"{perf.profit_factor:.2f}"
    lines = ["", "=" * 70, "  LIVE PERFORMANCE (settled trades only)", "=" * 70]
    if perf.n_settled == 0:
        lines += ["  No settled trades yet. Run `main.py run` then `main.py settle`.", "=" * 70]
        return "\n".join(lines)
    lines += [
        f"  settled trades : {perf.n_settled}",
        f"  win rate       : {perf.win_rate*100:.1f}%",
        f"  realized PnL   : ${perf.realized_pnl:+.2f}   ROI {perf.roi*100:+.2f}%",
        f"  profit factor  : {pf}    max drawdown {perf.max_drawdown*100:.1f}%",
        "  -- forecast quality (lower Brier is better) --",
        f"  model  Brier   : {perf.brier_model if perf.brier_model is not None else 'n/a'}  (n={perf.n_scored})",
        f"  market Brier   : {perf.brier_market if perf.brier_market is not None else 'n/a'}",
    ]
    if perf.edge_vs_market is not None:
        verdict = "MODEL BEATS MARKET" if perf.edge_vs_market > 0 else "NO EDGE (market >= model)"
        lines.append(f"  edge vs market : {perf.edge_vs_market:+.4f}  -> {verdict}")
    lines += ["  !! Small samples are noisy. Beating the market's Brier over MANY",
              "     independent days is the bar — not a few lucky settlements.", "=" * 70]
    return "\n".join(lines)
