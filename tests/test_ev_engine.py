"""Tests for the EV / edge engine."""
from config import Config
from src.ev_engine import evaluate_entry, fee_per_contract, maker_price, remaining_edge
from src.market_client import Market

CFG = Config()


def _market(yes_bid=0.39, yes_ask=0.40):
    return Market.from_dict({
        "ticker": "T", "title": "t", "yes_bid": yes_bid, "yes_ask": yes_ask,
        "no_bid": round(1 - yes_ask, 2), "no_ask": round(1 - yes_bid, 2), "volume": 500,
    })


def test_fee_formula():
    assert abs(fee_per_contract(0.5, 0.07) - 0.0175) < 1e-9
    assert fee_per_contract(0.0, 0.07) == 0.0
    assert fee_per_contract(1.0, 0.07) == 0.0


def test_entry_uses_ask_and_subtracts_costs():
    m = _market(yes_bid=0.39, yes_ask=0.40)
    ev = evaluate_entry(m, 0.60, CFG)
    expected = 0.60 - 0.40 - fee_per_contract(0.40, CFG.fee_rate) - CFG.slippage_buffer
    assert abs(ev.edge_yes_net - round(expected, 4)) < 1e-6
    assert ev.best_side == "yes"
    assert ev.entry_price == 0.40  # the ASK, not the mid


def test_picks_no_when_yes_overpriced():
    m = _market(yes_bid=0.69, yes_ask=0.70)  # no_ask = 0.31
    ev = evaluate_entry(m, 0.20, CFG)
    assert ev.best_side == "no"
    assert ev.edge_no_net > ev.edge_yes_net


def test_no_edge_returns_none_side():
    m = _market(yes_bid=0.49, yes_ask=0.51)
    ev = evaluate_entry(m, 0.50, CFG)
    assert ev.best_side is None  # fairly priced -> no executable edge


def test_remaining_edge_uses_bid():
    m = _market(yes_bid=0.39, yes_ask=0.40)
    assert remaining_edge("yes", m, 0.60) == round(0.60 - 0.39, 4)
    # NO position values at no_bid = 1 - yes_ask = 0.60
    assert remaining_edge("no", m, 0.60) == round((1 - 0.60) - m.no_bid, 4)


def test_maker_price_helper():
    assert maker_price(0.40, 0.44, 0.01) == 0.41   # one tick above the bid
    assert maker_price(0.40, 0.41, 0.01) == 0.40   # no room inside -> join the bid


def test_maker_edge_beats_taker():
    m = _market(yes_bid=0.40, yes_ask=0.44)
    taker = evaluate_entry(m, 0.62, Config(use_maker_orders=False))
    maker = evaluate_entry(m, 0.62, Config(use_maker_orders=True))
    assert taker.is_maker is False and maker.is_maker is True
    assert maker.entry_price < taker.entry_price        # rests on the bid side, cheaper
    assert maker.edge_yes_net > taker.edge_yes_net       # cheaper price + ~4x lower fee


def test_multi_model_ensemble_is_default():
    cfg = Config()
    assert "gfs" in cfg.openmeteo_models and "ecmwf" in cfg.openmeteo_models
