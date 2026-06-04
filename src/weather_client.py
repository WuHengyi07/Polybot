"""Weather data ingestion.

Providers return a FORECAST DISTRIBUTION (an ensemble of member outcomes), not a
single point forecast — that distribution is what lets probability_engine produce
a calibrated P(event). Mock data is the default; OpenMeteoProvider is a working
live provider (free, no API key). The other named providers are documented stubs
so you can plug in GEFS/ECMWF/HRRR/NBM/GFS/NWS/METAR sources later.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .market_client import DATA_DIR
from .market_parser import ParsedMarket, Variable
from .utils import get_logger, hours_until, safe_mean, safe_std, utcnow

log = get_logger("weather_client")


@dataclass
class ForecastDistribution:
    """An ensemble forecast for one variable at one location/date."""
    variable: str
    members: List[float]
    unit: str = "F"
    model: str = "mock"
    valid_date: Optional[date] = None
    horizon_hours: Optional[float] = None
    location: Optional[str] = None
    obs_high_so_far: Optional[float] = None  # set by IntradayWeatherProvider (METAR)

    @property
    def mean(self) -> float:
        return round(safe_mean(self.members), 4)

    @property
    def std(self) -> float:
        return round(safe_std(self.members), 4)

    @property
    def n(self) -> int:
        return len(self.members)

    def fraction_at_least(self, x: float) -> float:
        if not self.members:
            return 0.0
        return sum(1 for v in self.members if v > x) / len(self.members)

    def fraction_at_most(self, x: float) -> float:
        if not self.members:
            return 0.0
        return sum(1 for v in self.members if v < x) / len(self.members)

    def fraction_between(self, lo: float, hi: float) -> float:
        if not self.members:
            return 0.0
        return sum(1 for v in self.members if lo <= v <= hi) / len(self.members)


def daily_extreme_from_hourly(times, values, target_date, want_low: bool,
                              window: str = "civil", *, std_utc_offset: int = 0):
    """Daily max (or min) for ``target_date`` from an hourly (time, value) series.

    ``window='civil'``: ``times`` are local-clock ISO strings; group by calendar date
    (the historical behavior). ``window='lst'``: ``times`` are UTC ISO strings and the
    day is the NWS local-STANDARD-time 24h window (start = target 00:00 LST = UTC
    midnight minus ``std_utc_offset`` hours). Returns None if the window holds no data.
    """
    vals: List[float] = []
    if window == "lst":
        start = datetime(target_date.year, target_date.month, target_date.day) \
            - timedelta(hours=std_utc_offset)
        end = start + timedelta(hours=24)
        for t, v in zip(times, values):
            if v is None:
                continue
            try:
                dt = datetime.fromisoformat(str(t).replace("Z", ""))
            except ValueError:
                continue
            if start <= dt < end:
                vals.append(float(v))
    else:  # civil
        prefix = str(target_date)
        for t, v in zip(times, values):
            if v is not None and str(t).startswith(prefix):
                vals.append(float(v))
    if not vals:
        return None
    return min(vals) if want_low else max(vals)


def blend_toward(members: List[float], target_value: Optional[float], weight: float) -> List[float]:
    """Shift an ensemble's MEAN toward ``target_value`` by ``weight`` (0..1), preserving
    spread. Used to fold the NBM/NWS point forecast (most of its skill is in the mean)
    into the ensemble. No-op when weight<=0 or target is None."""
    if not members or target_value is None or weight <= 0:
        return members
    ens_mean = sum(members) / len(members)
    shift = weight * (target_value - ens_mean)
    return [m + shift for m in members]


class WeatherProvider(ABC):
    name = "abstract"

    @abstractmethod
    def get_forecast(self, parsed: ParsedMarket) -> Optional[ForecastDistribution]:
        ...


class BlendedWeatherProvider(WeatherProvider):
    """Wrap a base ensemble provider and shift its mean toward a second provider's point
    forecast (e.g. NWS/NBM). Spread (and any NGR calibration of it) is preserved; only the
    location is moved. Falls back to the base forecast if the secondary is unavailable."""
    name = "blended"

    def __init__(self, base_provider, secondary_provider, weight: float = 0.5):
        self.base = base_provider
        self.secondary = secondary_provider
        self.weight = weight

    def get_forecast(self, parsed: ParsedMarket) -> Optional[ForecastDistribution]:
        dist = self.base.get_forecast(parsed)
        if dist is None:
            return None
        try:
            other = self.secondary.get_forecast(parsed)
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("Blend secondary failed for %s: %s", getattr(parsed, "location", "?"), exc)
            other = None
        if other is not None and other.members:
            dist.members = blend_toward(dist.members, other.mean, self.weight)
            dist.model = f"{dist.model}+nbm{self.weight}"
        return dist


# --------------------------------------------------------------------------- #
# Mock provider (default)
# --------------------------------------------------------------------------- #
class MockWeatherProvider(WeatherProvider):
    name = "mock"

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else DATA_DIR / "mock_weather.json"
        with open(self.path, "r", encoding="utf-8") as f:
            self._data = json.load(f)

    def get_forecast(self, parsed: ParsedMarket) -> Optional[ForecastDistribution]:
        if not parsed.location or not parsed.target_date:
            return None
        city = self._data.get("cities", {}).get(parsed.location, {})
        entry = city.get(str(parsed.target_date))
        if not entry:
            log.debug("No mock weather for %s %s", parsed.location, parsed.target_date)
            return None
        return ForecastDistribution(
            variable=entry.get("variable", "high_temp"),
            members=[float(x) for x in entry.get("members", [])],
            unit=entry.get("unit", "F"),
            model=entry.get("model", "mock_ensemble"),
            valid_date=parsed.target_date,
            horizon_hours=entry.get("horizon_hours"),
            location=parsed.location,
        )


# --------------------------------------------------------------------------- #
# Live provider: Open-Meteo ensemble (free, no API key)
# --------------------------------------------------------------------------- #
class OpenMeteoProvider(WeatherProvider):
    """Daily high/low from the Open-Meteo ensemble API.

    Verified 2026-06-02: GFS (models=gfs025) returns 31 members as
    temperature_2m + temperature_2m_member01..30. We group hourly temps by local
    date and take each member's max (high) or min (low) on the target date.
    """
    name = "open-meteo"
    ENDPOINT = "https://ensemble-api.open-meteo.com/v1/ensemble"

    def __init__(self, config=None, models: str = "gfs025"):
        self.models = (getattr(config, "openmeteo_models", None) or models)
        self.settlement_window = getattr(config, "settlement_window", "civil")
        try:
            import requests  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("pip install requests to use the live Open-Meteo provider") from exc
        import requests

        self._requests = requests
        self._cache: Dict[tuple, dict] = {}

    def _fetch(self, lat: float, lon: float, tz: str, forecast_days: int) -> dict:
        key = (round(lat, 3), round(lon, 3), self.models, forecast_days, tz)
        if key in self._cache:
            return self._cache[key]
        params = {
            "latitude": lat, "longitude": lon, "hourly": "temperature_2m",
            "models": self.models, "forecast_days": forecast_days, "past_days": 1,
            "temperature_unit": "fahrenheit", "timezone": tz or "auto",
        }
        resp = self._requests.get(self.ENDPOINT, params=params, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        self._cache[key] = data
        return data

    def get_forecast(self, parsed: ParsedMarket) -> Optional[ForecastDistribution]:
        if parsed.latitude is None or parsed.longitude is None or not parsed.target_date:
            return None
        # Request extra days + 1 past day so the target LOCAL date is always
        # covered despite UTC-vs-local boundary slop (utcnow may already be the
        # next UTC day while it is still "today" at the station).
        days_ahead = (parsed.target_date - utcnow().date()).days
        forecast_days = max(2, min(16, days_ahead + 2))
        # For the LST window we need UTC stamps (request timezone=GMT) so we can apply
        # the station's fixed standard-time offset ourselves; civil uses the local tz.
        lst = self.settlement_window == "lst"
        fetch_tz = "GMT" if lst else (parsed.timezone or "auto")
        try:
            data = self._fetch(parsed.latitude, parsed.longitude, fetch_tz, forecast_days)
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("Open-Meteo fetch failed for %s: %s", parsed.location, exc)
            return None

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        if not times:
            return None
        window = "lst" if lst else "civil"
        member_keys = [k for k in hourly if k.startswith("temperature_2m")]
        want_low = parsed.variable == Variable.LOW_TEMP
        members: List[float] = []
        for k in member_keys:
            ext = daily_extreme_from_hourly(times, hourly.get(k, []), parsed.target_date,
                                            want_low, window, std_utc_offset=parsed.std_utc_offset)
            if ext is not None:
                members.append(ext)
        if not members:
            return None

        return ForecastDistribution(
            variable=parsed.variable.value,
            members=[float(x) for x in members],
            unit="F",
            model=f"open-meteo:{self.models}",
            valid_date=parsed.target_date,
            horizon_hours=hours_until(None) or max(0.0, days_ahead * 24.0 + 12.0),
            location=parsed.location,
        )


# --------------------------------------------------------------------------- #
# Documented stubs for additional sources (wire up later as needed)
# --------------------------------------------------------------------------- #
class _StubProvider(WeatherProvider):
    """Base for not-yet-implemented sources. Documents where to get the data."""
    source_note = "TODO"

    def get_forecast(self, parsed: ParsedMarket) -> Optional[ForecastDistribution]:  # pragma: no cover
        raise NotImplementedError(
            f"{self.name} provider is a stub. {self.source_note} "
            "OpenMeteoProvider already covers GFS/GEFS/ECMWF for free."
        )


class GEFSProvider(_StubProvider):
    name = "gefs"
    source_note = "GEFS (21+ members) via NOMADS GRIB or Open-Meteo models=gefs025."


class ECMWFEnsProvider(_StubProvider):
    name = "ecmwf-ens"
    source_note = "ECMWF ENS (51 members) via Open-Meteo models=ecmwf_ifs025 or ECMWF open data."


class HRRRProvider(_StubProvider):
    name = "hrrr"
    source_note = "HRRR 3km short-range via NOMADS; best for intraday, not next-day highs."


class NBMProvider(_StubProvider):
    name = "nbm"
    source_note = "National Blend of Models — best-calibrated point temps; via NOMADS/AWS NODD."


class GFSProvider(_StubProvider):
    name = "gfs"
    source_note = "Deterministic GFS via NOMADS or Open-Meteo models=gfs_seamless."


class NWSProvider(WeatherProvider):
    """NWS api.weather.gov gridpoint daily-max temperature (NBM-derived, post-processed,
    free, no key). Returns a 1-member 'distribution' carrying the point Tmax in deg F,
    for BlendedWeatherProvider to fold into an ensemble. None on any failure.

    HONEST SCOPE: this endpoint serves only the CURRENT forecast (no history), so the
    blend can't be validated in marketbacktest/histbacktest — only the live forward-test.
    """
    name = "nws"
    POINTS = "https://api.weather.gov/points/{lat},{lon}"

    def __init__(self, config=None):
        try:
            import requests
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("pip install requests to use the NWS provider") from exc
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "prediction_market_bot/1.0 (research; contact in README)",
            "Accept": "application/geo+json"})
        self._grid_cache: Dict[tuple, str] = {}

    def _grid_url(self, lat: float, lon: float) -> str:
        key = (round(lat, 4), round(lon, 4))
        if key not in self._grid_cache:
            r = self._session.get(self.POINTS.format(lat=lat, lon=lon), timeout=25)
            r.raise_for_status()
            self._grid_cache[key] = r.json()["properties"]["forecastGridData"]
        return self._grid_cache[key]

    def get_forecast(self, parsed: ParsedMarket) -> Optional[ForecastDistribution]:
        if (parsed is None or parsed.latitude is None or parsed.longitude is None
                or not parsed.target_date or parsed.variable != Variable.HIGH_TEMP):
            return None
        try:
            grid = self._grid_url(parsed.latitude, parsed.longitude)
            r = self._session.get(grid, timeout=25)
            r.raise_for_status()
            values = r.json()["properties"]["maxTemperature"]["values"]
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("NWS fetch failed for %s: %s", parsed.location, exc)
            return None
        target = str(parsed.target_date)
        for v in values:
            if str(v.get("validTime", ""))[:10] == target and v.get("value") is not None:
                tmax_f = float(v["value"]) * 9.0 / 5.0 + 32.0  # API returns deg C
                return ForecastDistribution(
                    variable=parsed.variable.value, members=[round(tmax_f, 2)], unit="F",
                    model="nws-nbm", valid_date=parsed.target_date, location=parsed.location)
        return None


class METARProvider(_StubProvider):
    name = "metar"
    source_note = "METAR/ASOS station obs via Iowa State Mesonet — the settlement-relevant temps."


def build_weather_provider(config) -> WeatherProvider:
    """Factory: live -> Open-Meteo (optionally wrapped with intraday METAR);
    fallback to mock on any failure."""
    if config.data_source in ("live", "polymarket"):
        try:
            provider: WeatherProvider = OpenMeteoProvider(config)
            # Fold in the NBM/NWS point forecast (better mean), then intraday obs last so
            # the realized high-so-far conditions whatever forecast we ended up with.
            if getattr(config, "use_nbm_blend", False):
                try:
                    provider = BlendedWeatherProvider(
                        provider, NWSProvider(config), getattr(config, "nbm_blend_weight", 0.5))
                except Exception as exc:  # pragma: no cover
                    log.warning("NBM blend unavailable (%s); using ensemble only.", exc)
            if getattr(config, "use_intraday", False):
                from .intraday import IntradayWeatherProvider
                provider = IntradayWeatherProvider(provider, config=config)
            return provider
        except Exception as exc:  # pragma: no cover
            log.warning("Open-Meteo provider unavailable (%s); using mock weather.", exc)
    return MockWeatherProvider()
