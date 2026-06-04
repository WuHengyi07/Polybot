"""Backtester.

V1 runs a controlled Monte-Carlo experiment over a fixed market set (mock data by
default; reproducible). For each market it runs the real pipeline (parse ->
forecast -> probability -> EV -> signal -> risk sizing) to collect the trades the
bot WOULD take, then simulates settlement many times under two "truth" models:

  * truth='model'   — outcomes drawn from the MODEL's probability. This tests the
                      strategy MECHANICS (sizing, fees, thresholds). If the model
                      were perfectly calibrated, this is roughly your upside.
  * truth='market'  — outcomes drawn from the MARKET's implied probability, i.e.
                      you have NO real edge. This shows what fees+slippage cost
                      you if your "edge" is just model error. It is usually a LOSS.

The gap between the two columns is the whole game: a real edge has to come from a
model that beats the market, proven on REAL settled outcomes — not from this
self-referential check. A real historical backtest would replay recorded
market+weather+settlement snapshots from the database (collect them first by
running the bot live read-only for a while).

Metrics: win rate, ROI, average edge, profit factor, max drawdown, average
holding time, calibration error (Brier + ECE), Sharpe-like ratio, number of
trades, and performance by market type.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import signal_engine as S
from .calibration import brier_score, calibration_error
from .ev_engine import evaluate_entry, fee_per_contract
from .market_client import MockMarketClient
from .market_parser import parse_market
from .probability_engine import estimate_probability
from .risk_manager import RiskManager
from .signal_engine import generate_entry_signal
from .utils import clamp, get_logger
from .weather_client import MockWeatherProvider

log = get_logger("backtester")


@dataclass
class CandidateTrade:
    ticker: str
    market_type: str
    side: str
    entry_price: float
    contracts: int
    model_p_yes: float
    market_p_yes: float       # market mid as implied probability
    edge: float
    fee: float
    hold_hours: Optional[float]


@dataclass
class BacktestMetrics:
    truth: str
    n_trades: int
    win_rate: float
    roi: float
    avg_edge: float
    profit_factor: float
    max_drawdown: float
    avg_hold_hours: Optional[float]
    brier: float
    calibration_error: float
    sharpe_like: float
    by_type: Dict[str, dict] = field(default_factory=dict)


def _collect_candidates(config) -> List[CandidateTrade]:
    """Run the real pipeline over the mock market set to get would-be trades."""
    from .position_manager import PositionManager

    market_client = MockMarketClient()
    weather = MockWeatherProvider()
    risk = RiskManager(config)
    pm = PositionManager(config.starting_bankroll)  # fresh, empty book

    candidates: List[CandidateTrade] = []
    for market in market_client.list_markets():
        parsed = parse_market(market)
        if not (parsed.location and parsed.target_date and parsed.threshold is not None):
            continue
        dist = weather.get_forecast(parsed)
        prob_est = estimate_probability(parsed, dist, config) if dist else None
        if prob_est is None:
            continue
        ev = evaluate_entry(market, prob_est.model_probability_yes, config)
        sig = generate_entry_signal(parsed, market, prob_est, ev, config)
        if sig.signal_type not in (S.BUY_YES, S.BUY_NO):
            continue
        decision = risk.evaluate_entry(sig, parsed, market, prob_est, ev,
                                       config.starting_bankroll, pm)
        if not decision.approved:
            continue
        from .utils import hours_until
        candidates.append(CandidateTrade(
            ticker=market.ticker,
            market_type=f"{parsed.variable.value}:{parsed.direction.value}",
            side=decision.side,
            entry_price=decision.entry_price,
            contracts=decision.allowed_contracts,
            model_p_yes=prob_est.model_probability_yes,
            market_p_yes=clamp(market.yes_mid, 0.001, 0.999),
            edge=ev.best_edge_net,
            fee=fee_per_contract(decision.entry_price, config.fee_rate) * decision.allowed_contracts,
            hold_hours=hours_until(market.close_time),
        ))
    return candidates


def _simulate(candidates: List[CandidateTrade], truth: str, trials: int, seed: int) -> BacktestMetrics:
    rng = random.Random(seed)
    if not candidates:
        return BacktestMetrics(truth, 0, 0, 0, 0, 0, 0, None, 0, 0, 0)

    trial_rois: List[float] = []
    drawdowns: List[float] = []
    gross_win = gross_loss = 0.0
    total_wins = total_trades = 0
    cal_probs: List[float] = []
    cal_outcomes: List[int] = []

    for t in range(trials):
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        stake = 0.0
        for c in candidates:
            p_yes = c.model_p_yes if truth == "model" else c.market_p_yes
            outcome_yes = 1 if rng.random() < p_yes else 0
            won = (c.side == "yes" and outcome_yes) or (c.side == "no" and not outcome_yes)
            cost = c.contracts * c.entry_price + c.fee
            payout = c.contracts * 1.0 if won else 0.0
            pnl = payout - cost
            cum += pnl
            stake += cost
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
            total_trades += 1
            total_wins += 1 if won else 0
            if pnl >= 0:
                gross_win += pnl
            else:
                gross_loss += -pnl
            if len(cal_probs) < 20000:  # accumulate (model_p vs realized YES) for stable calibration
                cal_probs.append(c.model_p_yes)
                cal_outcomes.append(outcome_yes)
        trial_rois.append(cum / stake if stake else 0.0)
        drawdowns.append(max_dd / stake if stake else 0.0)

    n = len(candidates)
    roi_mean = statistics.fmean(trial_rois)
    roi_std = statistics.pstdev(trial_rois) if len(trial_rois) > 1 else 0.0
    sharpe = roi_mean / roi_std if roi_std > 1e-9 else 0.0
    profit_factor = gross_win / gross_loss if gross_loss > 1e-9 else float("inf")

    # per market-type ROI (under same truth)
    by_type: Dict[str, dict] = {}
    for c in candidates:
        by_type.setdefault(c.market_type, {"n": 0, "stake": 0.0, "ev": 0.0})
        p_yes = c.model_p_yes if truth == "model" else c.market_p_yes
        win_prob = p_yes if c.side == "yes" else (1 - p_yes)
        ev = win_prob * (c.contracts * (1 - c.entry_price)) - (1 - win_prob) * (c.contracts * c.entry_price) - c.fee
        bt = by_type[c.market_type]
        bt["n"] += 1
        bt["stake"] += c.contracts * c.entry_price + c.fee
        bt["ev"] += ev
    for bt in by_type.values():
        bt["roi"] = round(bt["ev"] / bt["stake"], 4) if bt["stake"] else 0.0

    hold_vals = [c.hold_hours for c in candidates if c.hold_hours is not None]
    return BacktestMetrics(
        truth=truth,
        n_trades=n,
        win_rate=round(total_wins / total_trades, 4) if total_trades else 0.0,
        roi=round(roi_mean, 4),
        avg_edge=round(statistics.fmean([c.edge for c in candidates]), 4),
        profit_factor=round(profit_factor, 3) if profit_factor != float("inf") else float("inf"),
        max_drawdown=round(statistics.fmean(drawdowns), 4),
        avg_hold_hours=round(statistics.fmean(hold_vals), 1) if hold_vals else None,
        brier=round(brier_score(cal_probs, cal_outcomes), 4),
        calibration_error=round(calibration_error(cal_probs, cal_outcomes), 4),
        sharpe_like=round(sharpe, 3),
        by_type=by_type,
    )


def run_backtest(config, trials: int = 3000, seed: int = 42) -> dict:
    candidates = _collect_candidates(config)
    log.info("Backtest collected %d candidate trades", len(candidates))
    return {
        "n_candidates": len(candidates),
        "model": _simulate(candidates, "model", trials, seed),
        "market": _simulate(candidates, "market", trials, seed + 1),
        "candidates": candidates,
    }


def format_report(report: dict) -> str:
    lines = []
    lines.append("\n" + "=" * 78)
    lines.append("  BACKTEST (Monte-Carlo, mock market set)")
    lines.append("=" * 78)
    lines.append(f"  candidate trades: {report['n_candidates']}")
    if report["n_candidates"] == 0:
        lines.append("  No trades passed the filters — nothing to simulate.")
        lines.append("=" * 78)
        return "\n".join(lines)

    def block(m: BacktestMetrics, label: str):
        pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
        hold = f"{m.avg_hold_hours}h" if m.avg_hold_hours is not None else "n/a"
        lines.append(f"\n  [{label}]")
        lines.append(f"    trades={m.n_trades}  win_rate={m.win_rate*100:.1f}%  "
                     f"ROI={m.roi*100:+.2f}%  avg_edge={m.avg_edge:+.3f}")
        lines.append(f"    profit_factor={pf}  max_drawdown={m.max_drawdown*100:.1f}%  "
                     f"sharpe~={m.sharpe_like:.2f}  avg_hold={hold}")
        lines.append(f"    brier={m.brier:.3f}  calibration_error={m.calibration_error:.3f}")
        for typ, bt in m.by_type.items():
            lines.append(f"      by_type {typ:<22} n={bt['n']} roi={bt['roi']*100:+.1f}%")

    block(report["model"], "truth = MODEL  (assumes your model is calibrated)")
    block(report["market"], "truth = MARKET (assumes you have NO real edge)")
    lines.append("\n  !! The MODEL column is NOT proof of edge -- outcomes were drawn from the")
    lines.append("     model itself. The MARKET column shows fee/slippage bleed when the model")
    lines.append("     is wrong. Prove real edge only on REAL settled outcomes over many days.")
    lines.append("=" * 78)
    return "\n".join(lines)
