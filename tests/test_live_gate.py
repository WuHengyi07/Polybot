"""The edge-proven gate is the most safety-critical code: real money is
impossible until the paper track record earns it. These tests prove it."""
from config import Config
from src.database import Database
from src.live_gate import edge_proven
from src.live_trader import LiveTrader


def _armed_config(**kw):
    base = dict(paper_trading=False, live_trading=True, auto_trade=True,
                kalshi_api_key_id="x", kalshi_private_key_path="y", edge_proven_min_trades=3)
    base.update(kw)
    return Config(**base)


def _seed_winning_record(db, n=4):
    for i in range(n):
        tk = f"KXHIGHNY-26JUN0{i}-T80"
        db.record_trade({"ticker": tk, "side": "yes", "action": "open", "price": 0.40,
                         "contracts": 5, "fee": 0.05, "cash_flow": -2.05, "mode": "paper",
                         "position_id": f"{tk}:yes"})
        db.record_trade({"ticker": tk, "side": "yes", "action": "settle", "price": 1.0,
                         "contracts": 5, "fee": 0.0, "cash_flow": 5.0, "mode": "paper",
                         "position_id": f"{tk}:yes"})
        db.record_settlement({"ticker": tk, "target_date": "", "station": "", "outcome_yes": 1,
                              "observed_high": None, "model_probability_yes": 0.9,
                              "market_implied_yes": 0.5, "source": "mock", "mode": "paper"})


def test_gate_blocks_with_no_history():
    ok, reason = edge_proven(Database(":memory:"), _armed_config())
    assert ok is False and "insufficient" in reason.lower()


def test_gate_overrides_armed_flags():
    cfg = _armed_config()
    assert cfg.is_live_trading_armed() is True            # flags + keys are set
    ok, _ = edge_proven(Database(":memory:"), cfg)
    assert ok is False                                    # ...but the gate still blocks


def test_live_trader_armed_false_without_proven_edge():
    lt = LiveTrader(_armed_config(), client=None, db=Database(":memory:"))
    assert lt.armed() is False


def test_gate_allows_when_record_is_proven():
    cfg = _armed_config(edge_proven_min_trades=3)
    db = Database(":memory:")
    _seed_winning_record(db, n=4)
    ok, reason = edge_proven(db, cfg)
    assert ok is True, reason


def test_gate_blocks_when_model_loses_to_market():
    cfg = _armed_config(edge_proven_min_trades=3)
    db = Database(":memory:")
    _seed_winning_record(db, n=4)
    # Overwrite settlements so the MARKET forecasts better than the model.
    for i in range(4):
        tk = f"KXHIGHNY-26JUN0{i}-T80"
        db.record_settlement({"ticker": tk, "target_date": "", "station": "", "outcome_yes": 1,
                              "observed_high": None, "model_probability_yes": 0.5,
                              "market_implied_yes": 0.95, "source": "mock", "mode": "paper"})
    ok, reason = edge_proven(db, cfg)
    assert ok is False and "brier" in reason.lower()
