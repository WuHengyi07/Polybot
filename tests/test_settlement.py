"""Settlement resolution + outcome mapping."""
from datetime import date, timedelta

from config import Config
from src.market_client import MockMarketClient
from src.settlement import (MockSettlementSource, PolymarketSettlementSource, _city_from_ticker,
                            _date_from_ticker, _pm_ticker_date, mock_outcomes_from_weather,
                            polymarket_outcome)
from src.utils import utcnow
from src.weather_client import MockWeatherProvider


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_mock_source_resolves_from_dict():
    src = MockSettlementSource({"T1": True, "T2": False})
    assert src.resolve("T1").outcome_yes is True
    assert src.resolve("T2").outcome_yes is False
    assert src.resolve("T3") is None


def test_city_from_ticker():
    assert _city_from_ticker("KXHIGHNY-26JUN03-T83").code == "NYC"
    assert _city_from_ticker("KXHIGHCHI-26JUN03-T80").code == "CHI"
    assert _city_from_ticker("NONSENSE") is None


def test_city_from_polymarket_ticker():
    assert _city_from_ticker("PM-SHA-20260605-2424603").code == "SHA"
    assert _city_from_ticker("PM-HKG-20260605-1").code == "HKG"


def test_pm_ticker_date_parses():
    assert _pm_ticker_date("PM-LON-20260605-2424397") == date(2026, 6, 5)
    assert _pm_ticker_date("KXHIGHNY-26JUN03-T83") is None
    assert _pm_ticker_date("garbage") is None


def test_polymarket_resolve_skips_future_dates_without_network(monkeypatch):
    src = PolymarketSettlementSource(Config())
    calls = []
    monkeypatch.setattr(src._session, "get",
                        lambda *a, **k: calls.append(1) or _FakeResp({}))
    future = (utcnow().date() + timedelta(days=2)).strftime("%Y%m%d")
    assert src.resolve(f"PM-LON-{future}-1") is None   # not resolved yet
    assert calls == []                                  # guard short-circuited BEFORE any network


def test_polymarket_resolve_settles_past_resolved_market(monkeypatch):
    src = PolymarketSettlementSource(Config())
    monkeypatch.setattr(src._session, "get",
                        lambda *a, **k: _FakeResp({"closed": True, "outcomePrices": "[\"1\", \"0\"]"}))
    past = (utcnow().date() - timedelta(days=2)).strftime("%Y%m%d")
    res = src.resolve(f"PM-LON-{past}-1")
    assert res is not None and res.outcome_yes is True and res.source == "polymarket"


def test_polymarket_outcome_parsing():
    assert polymarket_outcome({"closed": True, "outcomePrices": "[\"1\", \"0\"]"}) is True
    assert polymarket_outcome({"closed": True, "outcomePrices": "[\"0\", \"1\"]"}) is False
    assert polymarket_outcome({"closed": False, "outcomePrices": "[\"0.3\", \"0.7\"]"}) is None
    assert polymarket_outcome({"closed": True, "outcomePrices": "[\"0.5\", \"0.5\"]"}) is None  # not final


def test_date_from_ticker():
    assert _date_from_ticker("KXHIGHNY-26JUN03-T83") == date(2026, 6, 3)
    assert _date_from_ticker("KXHIGHCHI-26DEC25-B40.5") == date(2026, 12, 25)


def test_mock_outcomes_from_weather_match_thresholds():
    outcomes = mock_outcomes_from_weather(MockMarketClient(), MockWeatherProvider())
    # NYC ensemble mean ~83.9: >83 should settle YES, >86 should settle NO
    assert outcomes.get("KXHIGHNY-26JUN03-T83") is True
    assert outcomes.get("KXHIGHNY-26JUN03-T86") is False
    # the unparseable "hot day" market is excluded entirely
    assert "KXHOTDAY-26JUN03-NYC" not in outcomes
