"""Settlement resolution — the keystone of the forward-test.

For each market the bot traded, find the ACTUAL outcome so paper positions can be
resolved to realized PnL and the model's probability can be scored against reality.

Two sources:
  * KalshiSettlementSource — authoritative binary outcome from Kalshi's own
    `GET /markets/{ticker}` `result` field (what actually paid out), plus a
    best-effort observed daily high from the station (for calibration richness).
  * MockSettlementSource — deterministic outcomes for offline runs / tests.

The binary outcome (Kalshi result) is what matters for PnL + Brier; the observed
high is optional enrichment used later by the calibration trainer.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional

from .market_parser import CITY_REGISTRY
from .utils import get_logger, to_float

log = get_logger("settlement")


@dataclass
class SettlementResult:
    outcome_yes: bool
    observed_high: Optional[float] = None
    station: str = ""
    source: str = ""


class SettlementSource(ABC):
    @abstractmethod
    def resolve(self, ticker: str) -> Optional[SettlementResult]:
        """Return the settled outcome for a ticker, or None if not yet settled."""


# --------------------------------------------------------------------------- #
def _city_from_ticker(ticker: str):
    u = ticker.upper()
    # Polymarket synthetic ticker: PM-{CODE}-{YYYYMMDD}-{marketid}
    m = re.match(r"PM-([A-Z]+)-", u)
    if m:
        return CITY_REGISTRY.get(m.group(1))
    m = re.match(r"KXHIGH([A-Z]+)-", u)
    if not m:
        return None
    suffix = m.group(1)
    for city in CITY_REGISTRY.values():
        if city.kalshi_suffix.upper() == suffix:
            return city
    return None


def polymarket_outcome(market_json: dict) -> Optional[bool]:
    """Resolved YES/NO from a Polymarket market object, or None if not finalized.

    A settled market's outcomePrices collapse to ~[1,0] (YES won) or ~[0,1] (NO won).
    """
    if not market_json or not market_json.get("closed"):
        return None
    raw = market_json.get("outcomePrices")
    try:
        prices = raw if isinstance(raw, list) else json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return None
    if len(prices) < 2:
        return None
    yes = to_float(prices[0])
    if yes >= 0.99:
        return True
    if yes <= 0.01:
        return False
    return None  # closed but not cleanly resolved (treat as not-yet-final)


def _date_from_ticker(ticker: str) -> Optional[date]:
    from .market_parser import _MONTHS  # reuse month map
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker.upper() + "-")
    if m and m.group(2) in _MONTHS:
        try:
            return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
        except ValueError:
            return None
    return None


class KalshiSettlementSource(SettlementSource):
    """Authoritative outcome from Kalshi + best-effort station obs."""

    def __init__(self, config):
        self.config = config
        self.base = config.kalshi_api_base.rstrip("/")
        try:
            import requests
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("pip install requests for live settlement") from exc
        self._requests = requests
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json", "User-Agent": "prediction_market_bot/1.0"})

    def resolve(self, ticker: str) -> Optional[SettlementResult]:
        outcome = self._market_result(ticker)
        if outcome is None:
            return None
        city = _city_from_ticker(ticker)
        observed = None
        if city is not None:
            observed = self._observed_high(city, _date_from_ticker(ticker))
        return SettlementResult(outcome_yes=outcome, observed_high=observed,
                                station=city.station if city else "", source="kalshi")

    def _market_result(self, ticker: str) -> Optional[bool]:
        try:
            resp = self._session.get(f"{self.base}/markets/{ticker}", timeout=20)
            resp.raise_for_status()
            m = resp.json().get("market", {})
        except Exception as exc:  # pragma: no cover - network dependent
            log.debug("Kalshi market fetch failed for %s: %s", ticker, exc)
            return None
        status = str(m.get("status", "")).lower()
        result = str(m.get("result", "")).lower()
        if result == "yes":
            return True
        if result == "no":
            return False
        if status in ("settled", "finalized") and m.get("settlement_value") is not None:
            return to_float(m.get("settlement_value")) >= 0.5
        return None  # not settled yet

    def _observed_high(self, city, target_date: Optional[date]) -> Optional[float]:
        """Best-effort daily high (deg F) from Iowa State Mesonet ASOS. Resilient."""
        if target_date is None:
            return None
        # Mesonet station id is typically the ICAO without the leading 'K'.
        icao = re.search(r"\(([A-Z0-9]{3,4})\)", city.station)
        station = icao.group(1) if icao else city.station.split()[-1].strip("()")
        station = station[1:] if station.startswith("K") and len(station) == 4 else station
        url = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
               f"?station={station}&data=tmpf&tz={city.timezone}&format=onlycomma&missing=null"
               f"&year1={target_date.year}&month1={target_date.month}&day1={target_date.day}"
               f"&year2={target_date.year}&month2={target_date.month}&day2={target_date.day}")
        try:
            resp = self._session.get(url, timeout=25)
            resp.raise_for_status()
            highs = []
            for line in resp.text.splitlines()[1:]:
                parts = line.split(",")
                if len(parts) >= 3 and parts[2] not in ("", "null", "M"):
                    highs.append(float(parts[2]))
            return round(max(highs), 1) if highs else None
        except Exception as exc:  # pragma: no cover
            log.debug("Mesonet obs failed for %s: %s", station, exc)
            return None


class PolymarketSettlementSource(SettlementSource):
    """Resolved outcome from Polymarket's gamma API (read-only). observed_high is left
    None for now — it's only used to train bias/NGR, not for the edge-proven gate."""

    def __init__(self, config):
        self.config = config
        self.gamma_base = getattr(config, "polymarket_gamma_base",
                                  "https://gamma-api.polymarket.com").rstrip("/")
        try:
            import requests
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("pip install requests for Polymarket settlement") from exc
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json",
                                      "User-Agent": "prediction_market_bot/1.0"})

    def resolve(self, ticker: str) -> Optional[SettlementResult]:
        market_id = ticker.rsplit("-", 1)[-1]  # PM-{CODE}-{YYYYMMDD}-{marketid}
        try:
            resp = self._session.get(f"{self.gamma_base}/markets/{market_id}", timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # pragma: no cover - network dependent
            log.debug("Polymarket market fetch failed for %s: %s", ticker, exc)
            return None
        market = data[0] if isinstance(data, list) and data else data
        outcome = polymarket_outcome(market if isinstance(market, dict) else {})
        if outcome is None:
            return None
        city = _city_from_ticker(ticker)
        return SettlementResult(outcome_yes=outcome, observed_high=None,
                                station=city.station if city else "", source="polymarket")


class MockSettlementSource(SettlementSource):
    """Deterministic outcomes from an explicit dict (tests) or mock weather."""

    def __init__(self, outcomes: Optional[Dict[str, bool]] = None,
                 observed: Optional[Dict[str, float]] = None):
        self.outcomes = outcomes or {}
        self.observed = observed or {}

    def resolve(self, ticker: str) -> Optional[SettlementResult]:
        if ticker not in self.outcomes:
            return None
        return SettlementResult(outcome_yes=bool(self.outcomes[ticker]),
                                observed_high=self.observed.get(ticker), source="mock")


def mock_outcomes_from_weather(market_client, weather_provider) -> Dict[str, bool]:
    """Deterministic 'truth' for mock markets: use the ensemble MEAN as the actual
    high and evaluate each market's threshold. Lets `main.py settle` demo end-to-end."""
    from .market_parser import Direction, parse_market
    outcomes: Dict[str, bool] = {}
    for market in market_client.list_markets():
        parsed = parse_market(market)
        if not parsed.tradeable or parsed.threshold is None:
            continue
        dist = weather_provider.get_forecast(parsed)
        if dist is None or dist.n == 0:
            continue
        actual = dist.mean  # treat ensemble mean as the realized high
        if parsed.direction == Direction.ABOVE:
            outcomes[market.ticker] = actual > parsed.threshold
        elif parsed.direction == Direction.BELOW:
            outcomes[market.ticker] = actual < parsed.threshold
        elif parsed.direction == Direction.BETWEEN and parsed.threshold_high is not None:
            outcomes[market.ticker] = parsed.threshold <= actual <= parsed.threshold_high
    return outcomes
