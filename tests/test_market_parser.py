"""Tests for weather market question parsing."""
from src.market_client import Market
from src.market_parser import (CITY_REGISTRY, Direction, Variable, parse_market,
                               series_ticker_for_city)

# Polymarket international cities added for forecast validation (histbacktest only).
INTL_CODES = ["HKG", "SHA", "CAN", "SZX", "BJS", "CTU", "CKG", "WUH", "TAO", "SEL",
              "PUS", "TYO", "TPE", "SIN", "KUL", "MNL", "KHI", "LKO", "JED", "TLV",
              "ANK", "IST", "MOW", "LON", "PAR", "AMS", "MAD", "MIL", "MUC", "WAW",
              "HEL", "YTO", "MEX", "PTY", "BUE", "SAO", "CPT", "WLG"]


def _market(title, ticker="KXHIGHNY-26JUN03-T83",
            settlement="NWS Daily Climate Report - NYC Central Park (KNYC)"):
    return Market.from_dict({
        "market_id": ticker, "ticker": ticker, "title": title,
        "yes_bid": 0.40, "yes_ask": 0.42, "volume": 500,
        "settlement_source": settlement,
    })


def test_parses_clear_high_temp_above():
    p = parse_market(_market("Will the high temp in NYC be >83° on Jun 3, 2026?"))
    assert p.tradeable is True
    assert p.variable == Variable.HIGH_TEMP
    assert p.direction == Direction.ABOVE
    assert p.threshold == 83
    assert p.location == "NYC"
    assert p.latitude is not None and p.longitude is not None
    assert str(p.target_date) == "2026-06-03"


def test_city_from_ticker_when_title_vague():
    # No city words, but ticker prefix KXHIGHCHI identifies Chicago.
    p = parse_market(_market("Will the high temp be >87° on Jun 3, 2026?",
                             ticker="KXHIGHCHI-26JUN03-T87",
                             settlement="NWS Chicago Midway (KMDW)"))
    assert p.location == "CHI"
    assert p.threshold == 87


def test_between_bracket_from_ticker():
    p = parse_market(_market("Will the high temp in NYC be 81.5° to 83.5° on Jun 3, 2026?",
                             ticker="KXHIGHNY-26JUN03-B82.5"))
    assert p.direction == Direction.BETWEEN
    assert p.threshold == 81.5 and p.threshold_high == 83.5


def test_vague_settlement_marks_do_not_trade():
    p = parse_market(_market("Will it be a hot day in NYC on Jun 3, 2026?",
                             ticker="KXHOTDAY-26JUN03-NYC",
                             settlement="Exchange discretion"))
    assert p.tradeable is False
    assert "settlement" in p.reason.lower() or "variable" in p.reason.lower()


def test_unmodeled_variable_blocked():
    p = parse_market(_market("Will it rain >1 inch in NYC on Jun 3, 2026?",
                             ticker="KXRAINNY-26JUN03-T1",
                             settlement="NWS Daily Climate Report"))
    assert p.tradeable is False
    assert "rain" in p.reason.lower()


def test_below_direction():
    p = parse_market(_market("Will the low temp in NYC be <40° on Jan 3, 2026?",
                             ticker="KXLOWNY-26JAN03-T40"))
    assert p.variable == Variable.LOW_TEMP
    assert p.direction == Direction.BELOW
    assert p.threshold == 40


def test_series_ticker_helper():
    assert series_ticker_for_city("NYC") == "KXHIGHNY"
    assert series_ticker_for_city("CHI") == "KXHIGHCHI"
    assert series_ticker_for_city("ZZZ") is None


# --- Polymarket markets: Celsius buckets, "or higher/below", WU/HKO settlement ---
def _pm(title, ticker="PM-SHA-20260605-B23",
        settlement="https://www.wunderground.com/history/daily/cn/shanghai/ZSPD"):
    return Market.from_dict({"market_id": ticker, "ticker": ticker, "title": title,
                             "yes_bid": 0.40, "yes_ask": 0.42, "volume": 500,
                             "settlement_source": settlement})


def test_polymarket_celsius_exact_bucket_converts_to_f():
    p = parse_market(_pm("Highest temperature in Shanghai on Jun 5, 2026 - 23°C"))
    assert p.tradeable is True and p.location == "SHA"
    assert p.threshold_unit == "C"
    assert p.direction == Direction.BETWEEN
    # 22.5..23.5 C -> 72.5..74.3 F
    assert abs(p.threshold - 72.5) < 0.2 and abs(p.threshold_high - 74.3) < 0.2


def test_polymarket_celsius_or_higher_is_above():
    p = parse_market(_pm("Highest temperature in Shanghai on Jun 5, 2026 - 30°C or higher",
                         ticker="PM-SHA-20260605-T30"))
    assert p.direction == Direction.ABOVE
    assert abs(p.threshold - 86.0) < 0.2          # 30C -> 86F


def test_polymarket_celsius_or_below_is_below_and_hko_ok():
    p = parse_market(_pm("Highest temperature in Hong Kong on Jun 5, 2026 - 22°C or below",
                         ticker="PM-HKG-20260605-U22", settlement="Hong Kong Observatory"))
    assert p.tradeable is True and p.location == "HKG"
    assert p.direction == Direction.BELOW
    assert abs(p.threshold - 71.6) < 0.2          # 22C -> 71.6F


def test_kalshi_fahrenheit_unchanged():
    p = parse_market(_market("Will the high temp in NYC be >83° on Jun 3, 2026?"))
    assert p.threshold_unit == "F" and p.threshold == 83 and p.tradeable is True


def test_international_cities_present_with_valid_coords():
    for code in INTL_CODES:
        assert code in CITY_REGISTRY, f"missing international city {code}"
        c = CITY_REGISTRY[code]
        assert -90.0 <= c.latitude <= 90.0, code
        assert -180.0 <= c.longitude <= 180.0, code
        assert c.timezone and "/" in c.timezone, code
        assert isinstance(c.std_utc_offset, (int, float)), code
    assert len(INTL_CODES) >= 38


def test_all_kalshi_suffixes_unique():
    # Guards the _SUFFIX_TO_CODE map against collisions as the registry grows.
    suffixes = [c.kalshi_suffix for c in CITY_REGISTRY.values()]
    assert len(suffixes) == len(set(suffixes))
