from types import SimpleNamespace

from src import notify
from src.notify import Notifier, format_fills, format_settlements, format_daily


def _cfg(events="fills,summary,settlement,errors", webhook=""):
    return SimpleNamespace(alert_webhook_url=webhook, notify_events=events,
                           notify_enabled=lambda e: (not events) or e in events,
                           edge_proven_min_trades=150)


def test_format_fills_batches_lines():
    fills = [({"ticker": "PM-SHA-20260604-1", "side": "yes", "contracts": 5, "price": 0.42}, 0.18),
             ({"ticker": "PM-HKG-20260604-2", "side": "no", "contracts": 3, "price": 0.31}, None)]
    msg = format_fills(fills)
    assert "2 new fill" in msg
    assert "PM-SHA-20260604-1" in msg and "YES x5 @ 0.42" in msg
    assert "edge +0.180" in msg
    assert "PM-HKG-20260604-2" in msg and "NO x3 @ 0.31" in msg


def test_format_fills_empty_is_blank():
    assert format_fills([]) == ""


def test_format_settlements_shows_outcome_and_model():
    items = [{"ticker": "PM-SHA-20260604-1", "side": "yes", "outcome_yes": 1,
              "observed_high": 31.0, "model_probability_yes": 0.7}]
    msg = format_settlements(items, realized_delta=2.35)
    assert "PM-SHA-20260604-1" in msg
    assert "YES" in msg and "obs 31.0" in msg and "model 0.70" in msg
    assert "+2.35" in msg


def test_format_daily_includes_gate_progress():
    perf = SimpleNamespace(n_settled=112, win_rate=0.74, realized_pnl=8.4, roi=0.06,
                           brier_model=0.09, brier_market=0.10, edge_vs_market=0.01)
    msg = format_daily(SimpleNamespace(mode="paper", equity=108.4, realized_pnl=8.4,
                                       open_positions=3, drawdown=0.02),
                       perf=perf, gate=(False, "insufficient sample: 112/150 settled trades"),
                       min_trades=150)
    assert "112/150" in msg
    assert "win 74.0%" in msg
    assert "edge vs market +0.010" in msg
    assert "GATE: not yet" in msg


def test_trade_fills_respects_event_gate_and_no_webhook():
    posted = []
    n = Notifier(_cfg(events="summary"))  # fills muted, no webhook
    n._post = lambda text: posted.append(text)
    n.trade_fills([({"ticker": "T", "side": "yes", "contracts": 1, "price": 0.5}, 0.1)])
    assert posted == []  # muted


def test_trade_fills_posts_when_enabled():
    posted = []
    n = Notifier(_cfg(events="fills"))
    n._post = lambda text: posted.append(text)
    n.trade_fills([({"ticker": "T", "side": "yes", "contracts": 1, "price": 0.5}, 0.1)])
    assert len(posted) == 1 and "1 new fill" in posted[0]


def test_format_settlements_minimal_item_no_optional_fields():
    msg = format_settlements([{"ticker": "X", "outcome_yes": 0}])
    assert "X" in msg and "-> NO" in msg
    assert "obs" not in msg and "model" not in msg
    assert "realized this pass" not in msg


def test_daily_summary_muted_when_summary_disabled():
    from types import SimpleNamespace
    posted = []
    n = Notifier(_cfg(events="errors"))
    n._post = lambda text: posted.append(text)
    n.daily_summary(SimpleNamespace(mode="paper", equity=100.0, realized_pnl=0.0,
                                    open_positions=0, drawdown=0.0))
    assert posted == []
