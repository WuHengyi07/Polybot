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


def test_format_daily_leads_with_closed_and_gate():
    closed = SimpleNamespace(n=15, win_rate=0.533, roi=0.021, realized=2.54)
    msg = format_daily(SimpleNamespace(mode="paper", equity=100.89, realized_pnl=2.54,
                                       open_positions=5, drawdown=0.019),
                       closed=closed,
                       gate=(False, "insufficient sample: 0/150 settled trades"),
                       target=150)
    assert "closed 15/150" in msg
    assert "win 53.3%" in msg
    assert "roi +2.1%" in msg
    assert "realized $+2.54" in msg
    assert "GATE (real money): not yet" in msg


def test_format_daily_without_closed_or_gate_is_just_base():
    msg = format_daily(SimpleNamespace(mode="paper", equity=100.0, realized_pnl=0.0,
                                       open_positions=0, drawdown=0.0))
    assert "daily:" in msg and "closed" not in msg and "GATE" not in msg


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
