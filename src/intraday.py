"""METAR / intraday observations — the aviation-data edge.

For SAME-DAY markets, a pure forecast is blind to the temperature already realized.
This module pulls the "max temp observed so far today" from airport ASOS/METAR
observations (Iowa State Mesonet) and uses it as a HARD FLOOR on each ensemble
member's daily high. Consequences:
  * if the day's high already exceeds an "above" threshold, P(YES) -> 1 (certainty);
  * late in the day, once the peak has passed, the market becomes near-deterministic.
This lets the bot trade same-day markets it would otherwise skip, and improves
calibration overall. Implements the METAR provider that was a stub in V1.
"""
from __future__ import annotations

import re
from typing import List, Optional

from .market_parser import ParsedMarket, Variable
from .utils import clamp, get_logger, utcnow

log = get_logger("intraday")


def _mesonet_station_id(station_label: str) -> str:
    """'NWS NYC Central Park (KNYC)' -> 'NYC' (Mesonet ASOS id = ICAO minus leading K)."""
    m = re.search(r"\(([A-Z0-9]{3,4})\)", station_label or "")
    sid = m.group(1) if m else (station_label or "").split()[-1].strip("()")
    return sid[1:] if sid.startswith("K") and len(sid) == 4 else sid


class ObservationClient:
    """Fetches today's observed max temperature (deg F) for a station."""

    BASE = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

    def __init__(self, config=None):
        try:
            import requests
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("pip install requests for METAR observations") from exc
        self._requests = requests
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "prediction_market_bot/1.0"})

    def max_temp_so_far(self, station_label: str, timezone: str, on_date,
                        as_of: Optional[str] = None) -> Optional[float]:
        station = _mesonet_station_id(station_label)
        if not station:
            return None
        url = (f"{self.BASE}?station={station}&data=tmpf&tz={timezone or 'UTC'}"
               f"&format=onlycomma&missing=null&year1={on_date.year}&month1={on_date.month}"
               f"&day1={on_date.day}&year2={on_date.year}&month2={on_date.month}&day2={on_date.day}")
        try:
            resp = self._session.get(url, timeout=25)
            resp.raise_for_status()
            times, temps = [], []
            for line in resp.text.splitlines()[1:]:
                parts = line.split(",")
                if len(parts) >= 3:
                    times.append(parts[1])
                    temps.append(parts[2])
            return max_observed_before(times, temps, as_of)
        except Exception as exc:  # pragma: no cover - network dependent
            log.debug("METAR obs failed for %s: %s", station, exc)
            return None


class IntradayWeatherProvider:
    """Wraps a base ensemble provider and conditions member highs on the observed
    high-so-far. ``floor`` mode (default) just clamps members up to the observed high;
    ``bayesian`` mode additionally shrinks each member's remaining upside by the diurnal
    headroom, collapsing the distribution to near-certainty once the daily peak passes."""

    name = "intraday"

    def __init__(self, base_provider, obs_client: Optional[ObservationClient] = None, config=None):
        self.base = base_provider
        self.obs = obs_client or ObservationClient()
        self.mode = getattr(config, "intraday_mode", "floor")
        self.peak_hour = getattr(config, "intraday_peak_hour", 15)
        self.peak_decay_hours = getattr(config, "intraday_peak_decay_hours", 4.0)

    def get_forecast(self, parsed: ParsedMarket):
        dist = self.base.get_forecast(parsed)
        if dist is None:
            return None
        # Only same-day HIGH-temp markets benefit (we observe a max-so-far).
        # Low-temp would need a min-so-far and is left to the pure forecast.
        if parsed.variable == Variable.HIGH_TEMP and \
                parsed.target_date == utcnow().date() and parsed.station:
            hsf = self.obs.max_temp_so_far(parsed.station, parsed.timezone, parsed.target_date)
            if hsf is not None:
                if self.mode == "bayesian":
                    # Local hour from the station's standard offset (DST-approx is fine
                    # for a decay factor); how far past the daily peak are we?
                    now = utcnow()
                    local_hour = (now.hour + now.minute / 60.0 + parsed.std_utc_offset) % 24
                    hours_past_peak = local_hour - self.peak_hour
                    dist.members = apply_bayesian_update(dist.members, hsf, hours_past_peak,
                                                         self.peak_decay_hours)
                    dist.model = f"{dist.model}+metar_bayes"
                else:
                    dist.members = apply_high_floor(dist.members, hsf)
                    dist.model = f"{dist.model}+metar"
                dist.obs_high_so_far = hsf
                log.info("Intraday (%s) for %s: high-so-far=%.1fF", self.mode, parsed.ticker, hsf)
        return dist


def apply_high_floor(members: List[float], high_so_far: float) -> List[float]:
    """The realized daily high can't be below what's already been observed."""
    return [max(high_so_far, m) for m in members]


def diurnal_headroom_factor(hours_past_peak: float, peak_decay_hours: float = 4.0) -> float:
    """Fraction of a member's forecast upside (above the observed high) still plausible.
    1.0 up to the daily peak, decaying linearly to 0 over ``peak_decay_hours`` after it."""
    if hours_past_peak <= 0:
        return 1.0
    return clamp(1.0 - hours_past_peak / max(1e-6, peak_decay_hours), 0.0, 1.0)


def apply_bayesian_update(members: List[float], obs_high_so_far: float,
                          hours_past_peak: float, peak_decay_hours: float = 4.0) -> List[float]:
    """Condition daily-high members on the observed high-so-far and time of day.

    The realized high is a floor; the forecast UPSIDE above it is scaled by the diurnal
    headroom factor, which decays to 0 once the peak has clearly passed -> P collapses to
    near-certainty late in the day (the edge vs a market that lags the observations).
    At factor 1.0 (before the peak) this is exactly ``apply_high_floor``.
    """
    factor = diurnal_headroom_factor(hours_past_peak, peak_decay_hours)
    return [obs_high_so_far + max(0.0, m - obs_high_so_far) * factor for m in members]


def max_observed_before(times, temps, as_of: Optional[str] = None) -> Optional[float]:
    """Max temperature among (time, temp) rows whose timestamp is <= ``as_of``.

    ``as_of=None`` means no cutoff (use all rows). ISO-8601 strings compare correctly
    lexicographically. This is the look-ahead guard: a decision at time T must never see
    an observation recorded after T.
    """
    vals = []
    for ts, t in zip(times, temps):
        if t in (None, "", "null", "M"):
            continue
        if as_of is not None and str(ts) > str(as_of):
            continue
        try:
            vals.append(float(t))
        except (TypeError, ValueError):
            continue
    return round(max(vals), 1) if vals else None
