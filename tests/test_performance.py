"""Performance metrics: realized PnL, win rate, and the Brier-vs-market edge test."""
from config import Config
from src.database import Database
from src.performance import (compute_performance, performance_by_segment,
                             unprofitable_segments)

CFG = Config(starting_bankroll=100)


def _seed(db, n=4, model_p=0.9, market_p=0.5, win=True):
    for i in range(n):
        tk = f"T{i}"
        db.record_trade({"ticker": tk, "side": "yes", "action": "open", "price": 0.40,
                         "contracts": 5, "fee": 0.0, "cash_flow": -2.0, "mode": "paper",
                         "position_id": f"{tk}:yes"})
        db.record_trade({"ticker": tk, "side": "yes", "action": "settle", "price": 1.0 if win else 0.0,
                         "contracts": 5, "fee": 0.0, "cash_flow": 5.0 if win else 0.0, "mode": "paper",
                         "position_id": f"{tk}:yes"})
        db.record_settlement({"ticker": tk, "target_date": "", "station": "", "outcome_yes": 1 if win else 0,
                              "observed_high": None, "model_probability_yes": model_p,
                              "market_implied_yes": market_p, "source": "mock", "mode": "paper"})


def test_no_settlements_empty():
    perf = compute_performance(Database(":memory:"), CFG)
    assert perf.n_settled == 0


def test_wins_and_brier_edge():
    db = Database(":memory:")
    _seed(db, n=4, model_p=0.9, market_p=0.5, win=True)
    perf = compute_performance(db, CFG)
    assert perf.n_settled == 4
    assert perf.win_rate == 1.0
    assert perf.realized_pnl > 0
    assert perf.brier_model < perf.brier_market      # model beats market
    assert perf.edge_vs_market > 0


def test_model_worse_than_market():
    db = Database(":memory:")
    # model says 0.5 but outcome is YES; market says 0.9 -> market is the better forecaster
    _seed(db, n=4, model_p=0.5, market_p=0.9, win=True)
    perf = compute_performance(db, CFG)
    assert perf.edge_vs_market < 0


def _seed_city(db, prefix, n, model_p, market_p, outcome):
    for i in range(n):
        db.record_settlement({"ticker": f"{prefix}-T{i}", "target_date": "", "station": "",
                              "outcome_yes": outcome, "observed_high": None,
                              "model_probability_yes": model_p, "market_implied_yes": market_p,
                              "source": "mock", "mode": "paper"})


def test_segments_split_by_city_and_pruning():
    db = Database(":memory:")
    _seed_city(db, "KXHIGHNY", n=25, model_p=0.9, market_p=0.5, outcome=1)   # model is RIGHT
    _seed_city(db, "KXHIGHCHI", n=25, model_p=0.9, market_p=0.5, outcome=0)  # model is WRONG
    segs = performance_by_segment(db, CFG)
    assert segs["city:NYC"]["edge_vs_market"] > 0     # NYC: model beats market
    assert segs["city:CHI"]["edge_vs_market"] < 0     # CHI: model loses to market
    bad = unprofitable_segments(db, CFG, min_n=20)
    assert "CHI" in bad and "NYC" not in bad          # only the losing city is pruned
