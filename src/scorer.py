"""Resolve open paper positions against real settled outcomes and record them.

This is what turns the bot from "opens positions" into "knows whether it was
right". For each open position whose market has settled, it realizes PnL via the
existing paper_trader.settle() and stores the (model_p, market_p, outcome) triple
used by performance.py (Brier-vs-market) and the calibration trainer.
"""
from __future__ import annotations

from typing import List, Tuple

from .settlement import (KalshiSettlementSource, MockSettlementSource,
                         PolymarketSettlementSource, SettlementSource,
                         mock_outcomes_from_weather)
from .utils import get_logger

log = get_logger("scorer")


def run_settlement_pass(db, pm, paper_trader, source: SettlementSource) -> List[Tuple[str, str, bool]]:
    """Settle every open position the source can resolve. Returns (ticker, side, outcome)."""
    resolved: List[Tuple[str, str, bool]] = []
    for pos in list(pm.positions.values()):  # copy — settle mutates pm.positions
        res = source.resolve(pos.ticker)
        if res is None:
            continue
        paper_trader.settle(pos.ticker, pos.side, res.outcome_yes)
        prob = db.latest_probability(pos.ticker)
        snap = db.latest_market_snapshot(pos.ticker)
        market_implied = None
        if snap and snap.get("yes_bid") is not None and snap.get("yes_ask") is not None:
            market_implied = round((snap["yes_bid"] + snap["yes_ask"]) / 2.0, 4)
        db.record_settlement({
            "ticker": pos.ticker, "target_date": "", "station": res.station,
            "outcome_yes": 1 if res.outcome_yes else 0, "observed_high": res.observed_high,
            "model_probability_yes": prob["model_probability_yes"] if prob else None,
            "market_implied_yes": market_implied, "source": res.source, "mode": paper_trader.mode,
        })
        resolved.append((pos.ticker, pos.side, res.outcome_yes))
        log.info("SETTLED %s %s -> outcome_yes=%s", pos.ticker, pos.side, res.outcome_yes)
    return resolved


def settle_cli(config) -> Tuple[List[Tuple[str, str, bool]], object]:
    """Build components, run a settlement pass, return (resolved, position_manager)."""
    from .database import Database
    from .market_client import build_market_client
    from .paper_trader import PaperTrader
    from .position_manager import PositionManager
    from .weather_client import build_weather_provider

    db = Database(config.db_path)
    pm = PositionManager(config.starting_bankroll)
    pm.replay(db.query("SELECT * FROM trades WHERE mode='paper' ORDER BY id"))
    paper = PaperTrader(config, pm, db)

    if config.data_source == "live":
        source: SettlementSource = KalshiSettlementSource(config)
    elif config.data_source == "polymarket":
        source = PolymarketSettlementSource(config)
    else:
        mc = build_market_client(config)
        wx = build_weather_provider(config)
        source = MockSettlementSource(mock_outcomes_from_weather(mc, wx))

    resolved = run_settlement_pass(db, pm, paper, source)
    return resolved, pm
