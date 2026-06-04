"""The edge-proven gate — the precondition for ANY real-money order.

Building the live machinery does not enable it. This gate reads the PAPER
forward-test track record and refuses to let the bot go live unless, over a
meaningful sample, it has shown a positive after-cost expectancy AND beaten the
market's implied probabilities as a forecaster (model Brier < market Brier).

It is ANDed into the live-arming decision (see live_trader.LiveTrader.armed),
so even with every flag set and API keys present, the bot stays in PAPER mode
until the numbers earn the right to trade real money.
"""
from __future__ import annotations

from typing import Tuple

from .utils import get_logger

log = get_logger("live_gate")


def edge_proven(db, config) -> Tuple[bool, str]:
    """Return (allowed, reason). allowed=True only when the paper record clears the bar."""
    from .performance import compute_performance

    perf = compute_performance(db, config, mode="paper")

    if perf.n_settled < config.edge_proven_min_trades:
        return False, f"insufficient sample: {perf.n_settled}/{config.edge_proven_min_trades} settled trades"

    if perf.realized_pnl <= 0 or perf.roi <= 0:
        return False, f"after-cost expectancy not positive (realized={perf.realized_pnl}, roi={perf.roi})"

    if config.edge_proven_require_brier_beat:
        if perf.brier_model is None or perf.brier_market is None:
            return False, "Brier-vs-market not computable (need scored forecasts)"
        if perf.edge_vs_market is None or perf.edge_vs_market <= 0:
            return False, (f"model does not beat market Brier "
                           f"(model={perf.brier_model}, market={perf.brier_market})")

    return True, (f"edge proven over {perf.n_settled} trades "
                  f"(roi={perf.roi*100:.1f}%, brier edge={perf.edge_vs_market})")
