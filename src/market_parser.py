"""Parse weather market questions into structured, machine-tradeable objects.

If a market cannot be parsed unambiguously (unknown variable, no threshold,
unknown city, or a vague/non-NWS settlement source) it is marked
``tradeable=False`` with a reason — the bot will then emit DO_NOT_TRADE rather
than guess. Misreading a settlement rule is one of the top ways weather bots
lose money, so we fail closed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Dict, List, Optional

from .market_client import Market
from .utils import celsius_to_fahrenheit, get_logger

log = get_logger("market_parser")


class Variable(str, Enum):
    HIGH_TEMP = "high_temp"
    LOW_TEMP = "low_temp"
    RAIN = "rain"
    SNOW = "snow"
    WIND = "wind"
    UNKNOWN = "unknown"


class Direction(str, Enum):
    ABOVE = "above"
    BELOW = "below"
    BETWEEN = "between"
    UNKNOWN = "unknown"


@dataclass
class City:
    code: str
    names: List[str]
    kalshi_suffix: str  # used to build series tickers, e.g. KXHIGH + 'NY' = KXHIGHNY
    station: str
    latitude: float
    longitude: float
    timezone: str
    std_utc_offset: float = 0  # standard-time UTC offset (hrs) for the LST settlement window
    verified: bool = False  # True only for cities whose Kalshi ticker we confirmed live


# NYC and CHI were confirmed live (KXHIGHNY / KXHIGHCHI) on 2026-06-02.
# Others are best-effort: verify the kalshi_suffix + settlement station before live use.
# std_utc_offset is the WINTER (standard-time) offset, used only by SETTLEMENT_WINDOW=lst.
CITY_REGISTRY: Dict[str, City] = {
    "NYC": City("NYC", ["nyc", "new york city", "new york", "manhattan", "central park"],
                "NY", "NWS NYC Central Park (KNYC)", 40.7790, -73.9693, "America/New_York",
                std_utc_offset=-5, verified=True),
    "CHI": City("CHI", ["chicago", "chi"],
                "CHI", "NWS Chicago Midway (KMDW)", 41.7861, -87.7522, "America/Chicago",
                std_utc_offset=-6, verified=True),
    "LAX": City("LAX", ["los angeles", "la ", "lax"],
                "LAX", "NWS Los Angeles (KLAX)", 33.9381, -118.3889, "America/Los_Angeles",
                std_utc_offset=-8),
    "MIA": City("MIA", ["miami", "mia"],
                "MIA", "NWS Miami (KMIA)", 25.7959, -80.2870, "America/New_York",
                std_utc_offset=-5),
    "DEN": City("DEN", ["denver", "den"],
                "DEN", "NWS Denver (KDEN)", 39.8466, -104.6562, "America/Denver",
                std_utc_offset=-7),
    "AUS": City("AUS", ["austin", "aus"],
                "AUS", "NWS Austin (KAUS)", 30.1975, -97.6664, "America/Chicago",
                std_utc_offset=-6),
    "PHIL": City("PHIL", ["philadelphia", "philly", "phil"],
                 "PHIL", "NWS Philadelphia (KPHL)", 39.8729, -75.2437, "America/New_York",
                 std_utc_offset=-5),

    # --- International cities (FORECAST-VALIDATION ONLY) ---------------------
    # These trade on POLYMARKET, not Kalshi: no Kalshi series and no NWS settlement,
    # so they are NOT live-tradeable in this bot (parse_market marks them DO_NOT_TRADE).
    # They exist so `histbacktest` can score forecast skill (Open-Meteo is global).
    # Coords = the Weather Underground / observatory resolution station; non-EU cities
    # don't observe DST so std_utc_offset is their year-round offset. See plan.
    "HKG": City("HKG", ["hong kong", "hongkong"], "HKG", "Hong Kong Observatory", 22.302, 114.174, "Asia/Hong_Kong", std_utc_offset=8),
    "SHA": City("SHA", ["shanghai"], "SHA", "Shanghai Pudong (ZSPD)", 31.143, 121.805, "Asia/Shanghai", std_utc_offset=8),
    "CAN": City("CAN", ["guangzhou"], "CAN", "Guangzhou Baiyun (ZGGG)", 23.392, 113.299, "Asia/Shanghai", std_utc_offset=8),
    "SZX": City("SZX", ["shenzhen"], "SZX", "Shenzhen Bao'an (ZGSZ)", 22.639, 113.811, "Asia/Shanghai", std_utc_offset=8),
    "BJS": City("BJS", ["beijing"], "BJS", "Beijing Capital (ZBAA)", 40.080, 116.585, "Asia/Shanghai", std_utc_offset=8),
    "CTU": City("CTU", ["chengdu"], "CTU", "Chengdu Shuangliu (ZUUU)", 30.578, 103.947, "Asia/Shanghai", std_utc_offset=8),
    "CKG": City("CKG", ["chongqing"], "CKG", "Chongqing Jiangbei (ZUCK)", 29.719, 106.642, "Asia/Shanghai", std_utc_offset=8),
    "WUH": City("WUH", ["wuhan"], "WUH", "Wuhan Tianhe (ZHHH)", 30.784, 114.208, "Asia/Shanghai", std_utc_offset=8),
    "TAO": City("TAO", ["qingdao"], "TAO", "Qingdao Liuting (ZSQD)", 36.266, 120.374, "Asia/Shanghai", std_utc_offset=8),
    "SEL": City("SEL", ["seoul"], "SEL", "Seoul Incheon (RKSI)", 37.469, 126.451, "Asia/Seoul", std_utc_offset=9),
    "PUS": City("PUS", ["busan"], "PUS", "Busan Gimhae (RKPK)", 35.180, 128.938, "Asia/Seoul", std_utc_offset=9),
    "TYO": City("TYO", ["tokyo"], "TYO", "Tokyo Haneda (RJTT)", 35.553, 139.781, "Asia/Tokyo", std_utc_offset=9),
    "TPE": City("TPE", ["taipei"], "TPE", "Taipei Songshan (RCSS)", 25.069, 121.552, "Asia/Taipei", std_utc_offset=8),
    "SIN": City("SIN", ["singapore"], "SIN", "Singapore Changi (WSSS)", 1.359, 103.989, "Asia/Singapore", std_utc_offset=8),
    "KUL": City("KUL", ["kuala lumpur"], "KUL", "Kuala Lumpur KLIA (WMKK)", 2.746, 101.710, "Asia/Kuala_Lumpur", std_utc_offset=8),
    "MNL": City("MNL", ["manila"], "MNL", "Manila NAIA (RPLL)", 14.509, 121.020, "Asia/Manila", std_utc_offset=8),
    "KHI": City("KHI", ["karachi"], "KHI", "Karachi Jinnah (OPKC)", 24.907, 67.161, "Asia/Karachi", std_utc_offset=5),
    "LKO": City("LKO", ["lucknow"], "LKO", "Lucknow Amausi (VILK)", 26.761, 80.889, "Asia/Kolkata", std_utc_offset=5.5),
    "JED": City("JED", ["jeddah"], "JED", "Jeddah King Abdulaziz (OEJN)", 21.680, 39.157, "Asia/Riyadh", std_utc_offset=3),
    "TLV": City("TLV", ["tel aviv", "tel-aviv"], "TLV", "Tel Aviv Ben Gurion (LLBG)", 32.011, 34.887, "Asia/Jerusalem", std_utc_offset=2),
    "ANK": City("ANK", ["ankara"], "ANK", "Ankara Esenboga (LTAC)", 40.128, 32.995, "Europe/Istanbul", std_utc_offset=3),
    "IST": City("IST", ["istanbul"], "IST", "Istanbul (LTFM)", 41.262, 28.742, "Europe/Istanbul", std_utc_offset=3),
    "MOW": City("MOW", ["moscow"], "MOW", "Moscow Vnukovo (UUWW)", 55.591, 37.261, "Europe/Moscow", std_utc_offset=3),
    "LON": City("LON", ["london"], "LON", "London City (EGLC)", 51.505, 0.055, "Europe/London", std_utc_offset=0),
    "PAR": City("PAR", ["paris"], "PAR", "Paris Le Bourget (LFPB)", 48.969, 2.441, "Europe/Paris", std_utc_offset=1),
    "AMS": City("AMS", ["amsterdam"], "AMS", "Amsterdam Schiphol (EHAM)", 52.309, 4.764, "Europe/Amsterdam", std_utc_offset=1),
    "MAD": City("MAD", ["madrid"], "MAD", "Madrid Barajas (LEMD)", 40.472, -3.561, "Europe/Madrid", std_utc_offset=1),
    "MIL": City("MIL", ["milan", "milano"], "MIL", "Milan Malpensa (LIMC)", 45.630, 8.728, "Europe/Rome", std_utc_offset=1),
    "MUC": City("MUC", ["munich", "munchen"], "MUC", "Munich (EDDM)", 48.354, 11.786, "Europe/Berlin", std_utc_offset=1),
    "WAW": City("WAW", ["warsaw", "warszawa"], "WAW", "Warsaw Chopin (EPWA)", 52.166, 20.967, "Europe/Warsaw", std_utc_offset=1),
    "HEL": City("HEL", ["helsinki"], "HEL", "Helsinki Vantaa (EFHK)", 60.317, 24.963, "Europe/Helsinki", std_utc_offset=2),
    "YTO": City("YTO", ["toronto"], "YTO", "Toronto Pearson (CYYZ)", 43.677, -79.631, "America/Toronto", std_utc_offset=-5),
    "MEX": City("MEX", ["mexico city", "ciudad de mexico"], "MEX", "Mexico City Benito Juarez (MMMX)", 19.436, -99.072, "America/Mexico_City", std_utc_offset=-6),
    "PTY": City("PTY", ["panama city", "panama"], "PTY", "Panama City Albrook (MPMG)", 8.973, -79.556, "America/Panama", std_utc_offset=-5),
    "BUE": City("BUE", ["buenos aires"], "BUE", "Buenos Aires Ezeiza (SAEZ)", -34.822, -58.536, "America/Argentina/Buenos_Aires", std_utc_offset=-3),
    "SAO": City("SAO", ["sao paulo", "são paulo"], "SAO", "Sao Paulo Guarulhos (SBGR)", -23.435, -46.473, "America/Sao_Paulo", std_utc_offset=-3),
    "CPT": City("CPT", ["cape town"], "CPT", "Cape Town (FACT)", -33.965, 18.602, "Africa/Johannesburg", std_utc_offset=2),
    "WLG": City("WLG", ["wellington"], "WLG", "Wellington (NZWN)", -41.327, 174.805, "Pacific/Auckland", std_utc_offset=12),
}

# Map a Kalshi series suffix back to a city code (for parsing tickers like KXHIGHNY-...).
_SUFFIX_TO_CODE = {c.kalshi_suffix.upper(): code for code, c in CITY_REGISTRY.items()}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


@dataclass
class ParsedMarket:
    market_id: str
    ticker: str
    variable: Variable
    direction: Direction
    location: Optional[str] = None
    city: Optional[City] = None
    station: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: Optional[str] = None
    target_date: Optional[date] = None
    threshold: Optional[float] = None
    threshold_high: Optional[float] = None  # for BETWEEN
    threshold_unit: str = "F"               # unit the market stated; thresholds stored in F
    settlement_source: Optional[str] = None
    std_utc_offset: int = 0  # standard-time UTC offset (hrs), for the LST settlement window
    tradeable: bool = False
    reason: str = ""
    notes: List[str] = field(default_factory=list)


def series_ticker_for_city(code: str) -> Optional[str]:
    """Kalshi daily-high series ticker for a city code, e.g. NYC -> 'KXHIGHNY'."""
    c = CITY_REGISTRY.get(code.upper())
    return f"KXHIGH{c.kalshi_suffix}" if c else None


def _find_city(title: str, ticker: str) -> Optional[City]:
    # Prefer the ticker suffix (unambiguous), then fall back to title text.
    m = re.match(r"KXHIGH([A-Z]+)-", ticker.upper())
    if m and m.group(1) in _SUFFIX_TO_CODE:
        return CITY_REGISTRY[_SUFFIX_TO_CODE[m.group(1)]]
    low = f" {title.lower()} "
    for c in CITY_REGISTRY.values():
        for name in c.names:
            if name in low:
                return c
    return None


def _find_variable(title: str) -> Variable:
    t = title.lower()
    if "high temp" in t or "high temperature" in t or "highest temp" in t:
        return Variable.HIGH_TEMP
    if "low temp" in t or "low temperature" in t or "lowest temp" in t:
        return Variable.LOW_TEMP
    if "snow" in t:
        return Variable.SNOW
    if "rain" in t or "precip" in t:
        return Variable.RAIN
    if "wind" in t:
        return Variable.WIND
    return Variable.UNKNOWN


def _find_threshold(title: str, ticker: str):
    """Return (direction, threshold, threshold_high)."""
    t = title.lower()
    # "between 84 and 86" / "84° to 86°"
    m = re.search(r"between\s+(\d+(?:\.\d+)?)\s*(?:°|degrees)?\s*(?:and|to|-)\s*(\d+(?:\.\d+)?)", t)
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)\s*°?\s*to\s*(\d+(?:\.\d+)?)\s*°", t)
    if m:
        lo, hi = sorted((float(m.group(1)), float(m.group(2))))
        return Direction.BETWEEN, lo, hi
    # Polymarket bucket phrasing: "23C or higher" / "22C or below" (edge brackets).
    m = re.search(r"(\d+(?:\.\d+)?)\s*°?\s*[cf]?\s*or\s+(?:higher|above|more|warmer)", t)
    if m:
        return Direction.ABOVE, float(m.group(1)), None
    m = re.search(r"(\d+(?:\.\d+)?)\s*°?\s*[cf]?\s*or\s+(?:below|lower|less|colder)", t)
    if m:
        return Direction.BELOW, float(m.group(1)), None
    # ">83" / "above 83" / "greater than 83"
    m = re.search(r"(?:>|above|greater than|over|at least|exceed[s]?)\s*(\d+(?:\.\d+)?)", t)
    if m:
        return Direction.ABOVE, float(m.group(1)), None
    # "<40" / "below 40" / "less than 40" / "under 40"
    m = re.search(r"(?:<|below|less than|under|at most)\s*(\d+(?:\.\d+)?)", t)
    if m:
        return Direction.BELOW, float(m.group(1)), None
    # Polymarket exact 1-degree bucket like "23°C": the high rounds to that degree
    # => the [n-0.5, n+0.5] band. Requires a degree symbol + C/F so it can't catch dates.
    m = re.search(r"(\d+(?:\.\d+)?)\s*°\s*[cf]\b", t)
    if m:
        c = float(m.group(1))
        return Direction.BETWEEN, c - 0.5, c + 0.5
    # Kalshi ticker fallback: "-T83" means > 83.
    m = re.search(r"-T(\d+(?:\.\d+)?)$", ticker.upper())
    if m:
        return Direction.ABOVE, float(m.group(1)), None
    # Kalshi range bucket ticker fallback: "-B84" (between, ~2F wide bracket).
    m = re.search(r"-B(\d+(?:\.\d+)?)$", ticker.upper())
    if m:
        center = float(m.group(1))
        return Direction.BETWEEN, center - 1.0, center + 1.0
    return Direction.UNKNOWN, None, None


def _find_date(title: str, ticker: str) -> Optional[date]:
    # Ticker date: "26JUN03"
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker.upper() + "-")
    if m and m.group(2) in _MONTHS:
        yy, mon, dd = int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3))
        try:
            return date(2000 + yy, mon, dd)
        except ValueError:
            pass
    # Title date WITH year: "on Jun 3, 2026"
    m = re.search(r"on\s+([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", title.lower())
    if m and m.group(1)[:3].upper() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1)[:3].upper()], int(m.group(2)))
        except ValueError:
            pass
    # Title date WITHOUT year, e.g. Polymarket "on June 4?" — infer the year (these are
    # near-term daily markets); roll to next year if that date is already well past.
    m = re.search(r"on\s+([a-z]{3})[a-z]*\.?\s+(\d{1,2})\b", title.lower())
    if m and m.group(1)[:3].upper() in _MONTHS:
        from .utils import utcnow
        today = utcnow().date()
        try:
            d = date(today.year, _MONTHS[m.group(1)[:3].upper()], int(m.group(2)))
            return date(today.year + 1, d.month, d.day) if (today - d).days > 60 else d
        except ValueError:
            pass
    return None


def _settlement_is_clear(source: str) -> bool:
    s = (source or "").lower()
    if not s:
        return False
    vague = ("discretion", "tbd", "to be determined", "manual")
    if any(v in s for v in vague):
        return False
    # We require an official observation source for weather settlement. NWS for Kalshi (US);
    # Weather Underground / Hong Kong Observatory for Polymarket international markets.
    return any(k in s for k in ("nws", "national weather service", "climate report", "metar", "noaa",
                                "weather underground", "wunderground", "hong kong observatory",
                                "hko", "observatory"))


def parse_market(market: Market) -> ParsedMarket:
    """Parse one normalized Market into a ParsedMarket; fail closed on ambiguity."""
    variable = _find_variable(market.title)
    direction, threshold, threshold_high = _find_threshold(market.title, market.ticker)
    city = _find_city(market.title, market.ticker)
    target_date = _find_date(market.title, market.ticker)

    # Polymarket international markets state thresholds in Celsius; the bot is internally
    # Fahrenheit (forecasts are F). Convert here so everything downstream stays in F.
    title_l = market.title.lower()
    threshold_unit = "C" if ("°c" in title_l or "celsius" in title_l) else "F"
    if threshold_unit == "C":
        if threshold is not None:
            threshold = round(celsius_to_fahrenheit(threshold), 2)
        if threshold_high is not None:
            threshold_high = round(celsius_to_fahrenheit(threshold_high), 2)

    parsed = ParsedMarket(
        market_id=market.market_id,
        ticker=market.ticker,
        variable=variable,
        direction=direction,
        location=city.code if city else None,
        city=city,
        station=city.station if city else None,
        latitude=city.latitude if city else None,
        longitude=city.longitude if city else None,
        timezone=city.timezone if city else None,
        std_utc_offset=city.std_utc_offset if city else 0,
        target_date=target_date,
        threshold=threshold,
        threshold_high=threshold_high,
        threshold_unit=threshold_unit,
        settlement_source=market.settlement_source,
    )

    reasons: List[str] = []
    if variable == Variable.UNKNOWN:
        reasons.append("unrecognized weather variable")
    if variable in (Variable.RAIN, Variable.SNOW, Variable.WIND):
        reasons.append(f"{variable.value} not modeled in V1 (temperature only)")
    if city is None:
        reasons.append("unknown city/station")
    if direction == Direction.UNKNOWN or threshold is None:
        reasons.append("no parseable threshold/direction")
    if direction == Direction.BETWEEN and threshold_high is None:
        reasons.append("incomplete range bounds")
    if target_date is None:
        reasons.append("no parseable target date")
    if not _settlement_is_clear(market.settlement_source):
        reasons.append("settlement source unclear/non-NWS")

    parsed.tradeable = not reasons
    parsed.reason = "; ".join(reasons)
    if not parsed.tradeable:
        log.debug("DO_NOT_TRADE %s: %s", market.ticker, parsed.reason)
    return parsed
