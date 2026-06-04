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
