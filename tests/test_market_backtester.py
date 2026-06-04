"""Market-backtest scoring: Brier(model) vs Brier(market) + the PnL simulation."""
from config import Config
from src.market_backtester import score_markets

CFG = Config()


def _rows(triples):
    return [{"model_p": m, "market_p": k, "outcome": o} for m, k, o in triples]


def test_sharp_model_beats_market_brier():
    rows = _rows([(0.95, 0.5, 1), (0.05, 0.5, 0), (0.92, 0.5, 1), (0.08, 0.5, 0)])
    res = score_markets(rows, entry_threshold=0.10, fee_rate=0.07)
    assert res.brier_model < res.brier_market
    assert res.beats_market is True


def test_model_loses_when_market_is_sharper():
    rows = _rows([(0.5, 0.95, 1), (0.5, 0.05, 0), (0.5, 0.9, 1), (0.5, 0.1, 0)])
    res = score_markets(rows, 0.10, 0.07)
    assert res.beats_market is False


def test_pnl_buy_yes_and_no_sides():
    yes = score_markets(_rows([(0.9, 0.5, 1)]), 0.10, 0.07)  # model 0.9 > market 0.5, YES happens
    assert yes.n_trades == 1 and yes.realized > 0
    no = score_markets(_rows([(0.1, 0.5, 0)]), 0.10, 0.07)   # model 0.1 < market 0.5, NO happens
    assert no.n_trades == 1 and no.realized > 0


def test_no_trade_when_edge_below_threshold():
    res = score_markets(_rows([(0.52, 0.50, 1), (0.49, 0.50, 0)]), 0.10, 0.07)
    assert res.n_trades == 0          # no edge clears the 10% bar
    assert res.n_rows == 2 and res.brier_model is not None  # but Brier still scored


def test_empty():
    res = score_markets([], 0.10, 0.07)
    assert res.n_rows == 0 and res.brier_model is None
