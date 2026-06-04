"""Intraday METAR flooring + Bayesian update + the obs-decided certainty path."""
from datetime import date

from config import Config
from src.intraday import (apply_bayesian_update, apply_high_floor,
                          diurnal_headroom_factor, max_observed_before)
from src.market_parser import Direction, ParsedMarket, Variable
from src.probability_engine import estimate_probability
from src.weather_client import ForecastDistribution

CFG = Config()


def test_apply_high_floor():
    assert apply_high_floor([70, 75, 80], 78) == [78, 78, 80]
    assert apply_high_floor([90, 91], 78) == [90, 91]  # already above floor -> unchanged


# --- intraday Bayesian update (diurnal headroom decay) --------------------- #
def test_headroom_factor_full_before_peak_zero_long_after():
    assert diurnal_headroom_factor(-2.0, 4.0) == 1.0     # before peak: full forecast upside
    assert diurnal_headroom_factor(0.0, 4.0) == 1.0
    assert diurnal_headroom_factor(8.0, 4.0) == 0.0      # long past peak: no further rise
    assert 0.0 < diurnal_headroom_factor(2.0, 4.0) < 1.0


def test_bayesian_update_equals_floor_before_peak():
    assert (apply_bayesian_update([70, 78, 82], 80, hours_past_peak=-1, peak_decay_hours=4)
            == apply_high_floor([70, 78, 82], 80))


def test_bayesian_update_collapses_to_observed_after_peak():
    # Well past the peak the daily high is ~ the high already observed (market often lags).
    assert apply_bayesian_update([85, 90, 95], 82, hours_past_peak=10, peak_decay_hours=4) == [82, 82, 82]


def test_bayesian_update_halves_upside_midafternoon():
    out = apply_bayesian_update([88], 80, hours_past_peak=2, peak_decay_hours=4)
    assert abs(out[0] - 84.0) < 1e-9                     # 80 + (88-80)*0.5


def test_max_observed_before_excludes_future_obs():
    # The decision-time cutoff must hide a later peak (no look-ahead).
    times = ["2026-06-03T12:00", "2026-06-03T16:00"]
    temps = [70, 95]
    assert max_observed_before(times, temps, "2026-06-03T13:00") == 70
    assert max_observed_before(times, temps, "2026-06-03T23:00") == 95
    assert max_observed_before(times, temps, None) == 95   # no cutoff -> max of all


def _parsed(threshold, direction=Direction.ABOVE):
    return ParsedMarket(market_id="m", ticker="t", variable=Variable.HIGH_TEMP, direction=direction,
                        location="NYC", station="KNYC", latitude=40.0, longitude=-73.0,
                        timezone="America/New_York", target_date=date(2026, 6, 3),
                        threshold=threshold, settlement_source="NWS", tradeable=True)


def test_observation_forces_certainty_when_already_exceeded():
    dist = ForecastDistribution(variable="high_temp", members=[82, 83, 84],
                                valid_date=date(2026, 6, 3), location="NYC", obs_high_so_far=85)
    est = estimate_probability(_parsed(80), dist, CFG)
    assert est.model_probability_yes > 0.98          # high already passed 80 -> YES
    assert est.components["obs_decided"] is True
    assert est.confidence >= 0.99


def test_not_decided_when_below_threshold():
    dist = ForecastDistribution(variable="high_temp", members=[82, 83, 84],
                                valid_date=date(2026, 6, 3), location="NYC", obs_high_so_far=70)
    est = estimate_probability(_parsed(85), dist, CFG)
    assert est.components["obs_decided"] is False     # 70 < 85, could still rise
