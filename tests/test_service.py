"""The unattended service must settle each data source with the RIGHT source — never
mock-settle a real-data source against its own forecast (that bug 'settled' future markets)."""
from config import Config
from src.market_client import MockMarketClient
from src.service import build_settlement_source
from src.settlement import (KalshiSettlementSource, MockSettlementSource,
                            PolymarketSettlementSource)
from src.weather_client import MockWeatherProvider


def test_polymarket_uses_real_settlement_source():
    src = build_settlement_source(Config(data_source="polymarket"), None, None)
    assert isinstance(src, PolymarketSettlementSource)   # NOT the mock-against-forecast source


def test_live_uses_kalshi_settlement_source():
    src = build_settlement_source(Config(data_source="live"), None, None)
    assert isinstance(src, KalshiSettlementSource)


def test_mock_uses_mock_settlement_source():
    src = build_settlement_source(Config(data_source="mock"),
                                  MockMarketClient(), MockWeatherProvider())
    assert isinstance(src, MockSettlementSource)


def test_service_posts_new_fills(monkeypatch, tmp_path):
    import dataclasses
    from config import Config
    from src.service import Service

    # Config is a frozen dataclass — override fields at construction, not by mutation.
    cfg = dataclasses.replace(
        Config.from_env(),
        data_source="mock",
        db_path=str(tmp_path / "svc.db"),
        loop_interval_seconds=0,
        notify_events="fills,summary,settlement,errors",
    )

    svc = Service(cfg)
    posted = []
    svc.notifier._post = lambda text: posted.append(text)

    svc.run_forever(max_cycles=1)

    # If the mock cycle opened any positions, a batched fill message was posted.
    trades = svc.engine.db.trades_since(0)
    if trades:
        assert any("new fill" in p for p in posted)
    # Watermark advanced so a second pass would not re-post the same fills.
    assert svc._last_trade_id == svc.engine.db.max_trade_id()


def test_service_does_not_post_historical_fills_on_startup(tmp_path):
    import dataclasses
    from config import Config
    from src.service import Service

    # Config is a frozen dataclass — override fields at construction, not by mutation.
    cfg = dataclasses.replace(
        Config.from_env(),
        data_source="mock",
        db_path=str(tmp_path / "svc2.db"),
    )

    # Pre-seed a trade BEFORE the service starts.
    from src.database import Database
    pre = Database(cfg.db_path)
    pre.record_trade({"ticker": "OLD", "side": "yes", "action": "open", "price": 0.4,
                      "contracts": 1, "fee": 0.0, "cash_flow": -0.4, "mode": "paper",
                      "position_id": "OLD:yes"})
    pre.close()

    svc = Service(cfg)
    # Watermark must start at the pre-existing max id, so OLD is never posted.
    assert svc._last_trade_id >= 1
