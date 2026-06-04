"""Tests for the probability engine."""
from datetime import date

from config import Config
from src.market_parser import Direction, ParsedMarket, Variable
from src.probability_engine import estimate_probability
from src.weather_client import ForecastDistribution

CFG = Config()
MEMBERS = [78, 80, 81, 81, 82, 82, 83, 83, 83, 84, 84, 84, 84, 85, 85, 85, 86, 86, 87, 88, 90]


def _parsed(threshold, direction=Direction.ABOVE, hi=None):
    return ParsedMarket(market_id="m", ticker="t", variable=Variable.HIGH_TEMP,
                        direction=direction, location="NYC", station="KNYC",
                        latitude=40.78, longitude=-73.97, timezone="America/New_York",
                        target_date=date(2026, 6, 3), threshold=threshold,
                        threshold_high=hi, settlement_source="NWS", tradeable=True)


def _dist(members=MEMBERS):
    return ForecastDistribution(variable="high_temp", members=list(members),
                                valid_date=date(2026, 6, 3), horizon_hours=24, location="NYC")


def test_probability_in_unit_interval_and_has_confidence():
    est = estimate_probability(_parsed(83), _dist(), CFG)
    assert est is not None
    assert 0.0 <= est.model_probability_yes <= 1.0
    assert 0.0 <= est.confidence <= 1.0
    assert est.components["n_members"] == len(MEMBERS)


def test_empirical_fraction_matches_above():
    est = estimate_probability(_parsed(83), _dist(), CFG)
    # 12 of 21 members are strictly > 83 (components are rounded to 4 dp)
    assert abs(est.components["empirical_p"] - 12 / 21) < 1e-3


def test_monotonic_in_threshold_for_above():
    p80 = estimate_probability(_parsed(80), _dist(), CFG).model_probability_yes
    p85 = estimate_probability(_parsed(85), _dist(), CFG).model_probability_yes
    p90 = estimate_probability(_parsed(90), _dist(), CFG).model_probability_yes
    assert p80 > p85 > p90


def test_below_is_complement_direction():
    above = estimate_probability(_parsed(83, Direction.ABOVE), _dist(), CFG)
    below = estimate_probability(_parsed(83, Direction.BELOW), _dist(), CFG)
    # empirical above (>83) + below (<83) + ties(=83) == 1
    assert above.components["empirical_p"] + below.components["empirical_p"] <= 1.0 + 1e-9


def test_between_probability():
    est = estimate_probability(_parsed(82, Direction.BETWEEN, hi=85), _dist(), CFG)
    assert 0.0 < est.model_probability_yes < 1.0


def test_degenerate_distribution_does_not_crash():
    est = estimate_probability(_parsed(83), _dist([83] * 20), CFG)
    assert est is not None
    assert 0.0 <= est.model_probability_yes <= 1.0


def test_no_forecast_returns_none():
    assert estimate_probability(_parsed(83), None, CFG) is None


def test_ngr_overrides_spread_when_enabled():
    cfg = Config(use_ngr=True)
    ngr = {"a": 0.0, "b": 1.0, "c": 100.0, "d": 0.0}   # forces sigma = sqrt(100) = 10
    on = estimate_probability(_parsed(83), _dist(), cfg, ngr=ngr)
    assert abs(on.components["std_used"] - 10.0) < 1e-6
    off = estimate_probability(_parsed(83), _dist(), CFG)   # use_ngr False by default
    assert off.components["std_used"] < 6.0                 # raw ensemble std * inflation


def test_ngr_ignored_when_disabled():
    ngr = {"a": 0.0, "b": 1.0, "c": 100.0, "d": 0.0}
    off = estimate_probability(_parsed(83), _dist(), CFG, ngr=ngr)  # use_ngr False -> no effect
    assert off.components["std_used"] < 6.0
