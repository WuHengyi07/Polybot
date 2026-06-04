"""Tests for the risk manager: every limit must block with the right reason."""
from datetime import date

from config import Config
from src.ev_engine import evaluate_entry
from src.market_client import Market
from src.market_parser import Direction, ParsedMarket, Variable
from src.position_manager import PositionManager
from src.probability_engine import ProbabilityEstimate
from src.risk_manager import RR, RiskManager, kelly_position_size
from src.signal_engine import BUY_YES, Signal


def _market(ticker="T", yes_bid=0.39, yes_ask=0.40, volume=500):
    return Market.from_dict({
        "ticker": ticker, "title": "t", "yes_bid": yes_bid, "yes_ask": yes_ask,
        "no_bid": round(1 - yes_ask, 2), "no_ask": round(1 - yes_bid, 2), "volume": volume,
    })


def _parsed(ticker="T", tradeable=True, reason=""):
    return ParsedMarket(market_id=ticker, ticker=ticker, variable=Variable.HIGH_TEMP,
                        direction=Direction.ABOVE, location="NYC", threshold=83,
                        settlement_source="NWS", tradeable=tradeable, reason=reason)


def _prob(conf=0.8, p=0.65):
    return ProbabilityEstimate(model_probability_yes=p, confidence=conf, method="t")


def _eval(config, market, p=0.65, conf=0.8, pm=None, bankroll=100.0):
    pm = pm or PositionManager(config.starting_bankroll)
    ev = evaluate_entry(market, p, config)
    sig = Signal(ticker=market.ticker, signal_type=BUY_YES, side=ev.best_side)
    rm = RiskManager(config)
    return rm.evaluate_entry(sig, _parsed(market.ticker), market, _prob(conf, p), ev, bankroll, pm)


def test_approved_and_sized():
    d = _eval(Config(), _market())
    assert d.approved is True
    assert d.allowed_contracts >= 1
    assert RR.APPROVED in d.reason_codes
    # 3% of 100 / 0.40 ask = 7 contracts
    assert d.allowed_contracts == 7


def test_spread_too_wide_blocked():
    d = _eval(Config(), _market(yes_bid=0.40, yes_ask=0.50), p=0.80)
    assert d.approved is False and RR.MAX_SPREAD in d.reason_codes


def test_low_volume_blocked():
    d = _eval(Config(), _market(volume=50))
    assert d.approved is False and RR.MIN_LIQUIDITY in d.reason_codes


def test_low_confidence_blocked():
    d = _eval(Config(), _market(), conf=0.40)
    assert d.approved is False and RR.MIN_CONFIDENCE in d.reason_codes


def test_price_out_of_range_blocked():
    d = _eval(Config(), _market(yes_bid=0.02, yes_ask=0.03), p=0.20)
    assert d.approved is False and RR.PRICE_OUT_OF_RANGE in d.reason_codes


def test_kill_switch_blocked():
    d = _eval(Config(emergency_stop=True), _market())
    assert d.approved is False and RR.KILL_SWITCH in d.reason_codes


def test_max_open_positions_blocked():
    cfg = Config(max_open_positions=2)
    pm = PositionManager(cfg.starting_bankroll)
    pm.apply_open("AAA", "yes", 1, 0.40, 0.0, "weather", "2026-06-03T00:00:00")
    pm.apply_open("BBB", "yes", 1, 0.40, 0.0, "weather", "2026-06-03T00:00:00")
    d = _eval(cfg, _market(ticker="CCC"), pm=pm)
    assert d.approved is False and RR.MAX_OPEN_POSITIONS in d.reason_codes


def test_market_exposure_caps_size():
    cfg = Config(max_market_exposure=0.01)  # $1 max in one market
    d = _eval(cfg, _market())
    assert d.binding_constraint == "max_market_exposure"
    assert d.allowed_contracts <= 2


def test_daily_loss_limit_blocked():
    cfg = Config(max_daily_loss=0.05)  # $5 on a $100 bankroll
    pm = PositionManager(cfg.starting_bankroll)
    today = date.today().isoformat()
    # Simulate a realized loss today by closing a losing position.
    pm.apply_open("XXX", "yes", 100, 0.50, 0.0, "weather", today + "T00:00:00")
    pm.apply_close("XXX", "yes", 100, 0.40, 0.0, today + "T01:00:00")  # -$10 realized
    d = _eval(cfg, _market(), pm=pm)
    assert d.approved is False and RR.DAILY_LOSS_LIMIT in d.reason_codes


# --- confidence-scaled fractional Kelly sizing ----------------------------- #
def test_kelly_size_grows_with_confidence():
    lo = kelly_position_size(0.10, 0.50, 1000, 0.25, 0.60)
    hi = kelly_position_size(0.10, 0.50, 1000, 0.25, 0.95)
    assert hi > lo                          # more conviction -> bigger bet


def test_kelly_confidence_scaling_can_be_disabled():
    scaled = kelly_position_size(0.10, 0.50, 1000, 0.25, 0.60, confidence_scaled=True)
    unscaled = kelly_position_size(0.10, 0.50, 1000, 0.25, 0.60, confidence_scaled=False)
    assert unscaled > scaled                # full fraction when not down-scaled


def test_kelly_fraction_cap_limits_size():
    # A reckless kelly_fraction of 5.0 is clamped to the 0.5 cap.
    capped = kelly_position_size(0.30, 0.50, 1000, 5.0, 1.0, fraction_cap=0.5)
    at_cap = kelly_position_size(0.30, 0.50, 1000, 0.5, 1.0, fraction_cap=0.5)
    assert capped == at_cap


def test_kelly_zero_when_no_edge():
    assert kelly_position_size(0.0, 0.50, 1000, 0.25, 0.9) == 0
    assert kelly_position_size(-0.1, 0.50, 1000, 0.25, 0.9) == 0


# --- order-book depth realism (caps fills to resting size; 0 => can't trade) ---
def test_order_book_depth_caps_size():
    from src.market_client import OrderBook, OrderBookLevel
    m = _market()                                   # yes_ask 0.40; flat-size would be 7
    m.order_book = OrderBook(yes_asks=[OrderBookLevel(0.40, 3)])
    d = _eval(Config(), m)
    assert d.approved is True and d.allowed_contracts == 3
    assert d.binding_constraint == "order_book_depth"


def test_empty_book_blocks_trade():
    from src.market_client import OrderBook
    m = _market()
    m.order_book = OrderBook(yes_asks=[])            # nothing resting to buy
    d = _eval(Config(), m)
    assert d.approved is False and RR.INSUFFICIENT_SIZE in d.reason_codes
