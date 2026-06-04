"""Entry-signal logic: the favorite-longshot mode and the edge-to-price guard.

Favorite mode is the LEGITIMATE way to raise win rate + EV together: take more
high-confidence favorites the model thinks are underpriced (at a slightly lower edge
bar), without lowering the bar for coin-flips. The edge-to-price ratio guard keeps
cheap contracts honest (a flat fee/slippage eats small-priced bets)."""
from config import Config
from src.ev_engine import evaluate_entry
from src.market_client import Market
from src.market_parser import Direction, ParsedMarket, Variable
from src.probability_engine import ProbabilityEstimate
from src.signal_engine import BUY_YES, DO_NOT_TRADE, HOLD, R, generate_entry_signal


def _parsed(ticker="T"):
    return ParsedMarket(market_id=ticker, ticker=ticker, variable=Variable.HIGH_TEMP,
                        direction=Direction.ABOVE, location="NYC", threshold=83,
                        settlement_source="NWS", tradeable=True)


def _market(yes_bid, yes_ask, volume=500):
    return Market.from_dict({"ticker": "T", "title": "t", "yes_bid": yes_bid, "yes_ask": yes_ask,
                             "no_bid": round(1 - yes_ask, 2), "no_ask": round(1 - yes_bid, 2),
                             "volume": volume})


def _sig(config, market, p, conf=0.8):
    ev = evaluate_entry(market, p, config)
    est = ProbabilityEstimate(model_probability_yes=p, confidence=conf, method="t")
    return generate_entry_signal(_parsed(), market, est, ev, config)


def test_underpriced_favorite_holds_below_base_threshold_by_default():
    # model 0.80 vs ask 0.72 -> ~0.056 net edge, below the base 0.10 bar -> HOLD.
    assert _sig(Config(), _market(0.71, 0.72), p=0.80).signal_type == HOLD


def test_favorite_mode_takes_underpriced_favorite():
    cfg = Config(favorite_mode=True, favorite_p_floor=0.65, entry_edge_threshold_favorite=0.04)
    sig = _sig(cfg, _market(0.71, 0.72), p=0.80)
    assert sig.signal_type == BUY_YES
    assert R.FAVORITE_EDGE in sig.reason_codes


def test_favorite_mode_does_not_lower_bar_for_coinflip():
    # model 0.55 is not a favorite (< 0.65 floor); the lower favorite bar must not apply.
    cfg = Config(favorite_mode=True, favorite_p_floor=0.65, entry_edge_threshold_favorite=0.04)
    assert _sig(cfg, _market(0.49, 0.50), p=0.55).signal_type == HOLD


def test_edge_to_price_ratio_blocks_low_relative_edge():
    # edge ~0.052 clears the 0.05 bar, but ratio 0.052/0.50 = 0.10 < 0.20 -> blocked.
    cfg = Config(entry_edge_threshold=0.05, min_edge_to_price_ratio=0.20)
    sig = _sig(cfg, _market(0.49, 0.50), p=0.58)
    assert sig.signal_type == DO_NOT_TRADE and R.EDGE_TO_PRICE_LOW in sig.reason_codes


def test_edge_to_price_ratio_off_by_default():
    cfg = Config(entry_edge_threshold=0.05)   # ratio default 0.0 -> guard disabled
    assert _sig(cfg, _market(0.49, 0.50), p=0.58).signal_type == BUY_YES
