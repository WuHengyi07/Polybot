"""Tests for the paper trading engine: fills, fees, PnL, settlement."""
from config import Config
from src.database import Database
from src.ev_engine import fee_per_contract
from src.market_client import Market
from src.paper_trader import PaperTrader
from src.position_manager import PositionManager

CFG = Config()


def _market(ticker="T"):
    return Market.from_dict({
        "ticker": ticker, "title": "t", "yes_bid": 0.40, "yes_ask": 0.42,
        "no_bid": 0.58, "no_ask": 0.60, "volume": 500,
    })


def _trader():
    pm = PositionManager(100.0)
    db = Database(":memory:")
    return PaperTrader(CFG, pm, db), pm, db


def test_buy_yes_fills_at_ask_and_charges_fee():
    trader, pm, db = _trader()
    m = _market()
    trader.open(m, "yes", 5)
    fee = fee_per_contract(0.42, CFG.fee_rate) * 5
    # cash flow is rounded to 4 dp internally, so allow a sub-cent tolerance
    assert abs(pm.cash - (100.0 - (5 * 0.42 + fee))) < 1e-3
    pos = pm.positions["T:yes"]
    assert pos.contracts == 5 and pos.avg_price == 0.42
    assert db.recent_trades(10)[0]["price"] == 0.42  # filled at the ASK


def test_buy_no_fills_at_no_ask():
    trader, pm, db = _trader()
    trader.open(_market(), "no", 3)
    pos = pm.positions["T:no"]
    assert pos.avg_price == 0.60  # the NO ask


def test_sell_yes_fills_at_bid_and_realizes_loss():
    trader, pm, db = _trader()
    m = _market()
    trader.open(m, "yes", 5)        # buy @ 0.42
    trade = trader.close(m, pm.positions["T:yes"])  # sell @ 0.40 (bid)
    assert trade["price"] == 0.40
    # bought 0.42, sold 0.40 -> negative realized (plus fees)
    assert trade["realized"] < 0
    assert "T:yes" not in pm.positions  # fully closed


def test_settlement_pays_out_winner():
    trader, pm, db = _trader()
    m = _market()
    trader.open(m, "yes", 5)  # cost basis 5 * 0.42 = 2.10
    trade = trader.settle("T", "yes", outcome_yes=True)
    assert trade is not None
    # payout 5 * $1 = 5.00, realized = 5.00 - 2.10 = 2.90
    assert abs(trade["realized"] - 2.90) < 1e-6
    assert pm.realized_pnl > 0


def test_round_trip_cash_conservation_on_flat_settlement():
    trader, pm, db = _trader()
    m = _market()
    trader.open(m, "no", 4)               # buy NO @ 0.60
    trader.settle("T", "no", outcome_yes=False)  # NO wins -> payout $1 each
    # cash should now exceed starting (won), positions empty
    assert pm.open_count() == 0
    assert pm.cash > 100.0


def test_maker_order_respects_fill_probability():
    m = _market()
    # fill prob 0 -> a maker order never fills (returns None, no position)
    pm0 = PositionManager(100.0)
    t0 = PaperTrader(Config(maker_fill_prob=0.0), pm0, Database(":memory:"))
    assert t0.open(m, "yes", 5, price=0.40, fee_rate=0.0175, maker=True) is None
    assert pm0.open_count() == 0
    # fill prob 1 -> always fills, at the maker price + maker fee
    pm1 = PositionManager(100.0)
    t1 = PaperTrader(Config(maker_fill_prob=1.0), pm1, Database(":memory:"))
    trade = t1.open(m, "yes", 5, price=0.40, fee_rate=0.0175, maker=True)
    assert trade is not None and pm1.positions["T:yes"].avg_price == 0.40


def test_replay_reconstructs_state():
    trader, pm, db = _trader()
    m = _market()
    trader.open(m, "yes", 5)
    # Rebuild a fresh manager from the trade log -> identical cash & position.
    pm2 = PositionManager(100.0)
    pm2.replay(db.query("SELECT * FROM trades ORDER BY id"))
    assert abs(pm2.cash - pm.cash) < 1e-6
    assert pm2.positions["T:yes"].contracts == 5
