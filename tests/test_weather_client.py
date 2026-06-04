"""Daily-extreme windowing: civil-clock day vs NWS local-standard-time day.

The NWS climate 'official high' is measured over a local-STANDARD-time 24h window.
During daylight-saving months that differs from the local-clock day by ~1h, which
can move a late-night/early-morning temperature spike into a different day.
"""
from datetime import date

from src.weather_client import (BlendedWeatherProvider, ForecastDistribution,
                                blend_toward, daily_extreme_from_hourly)


def test_civil_window_groups_by_local_clock_date():
    # Local civil-time stamps; 95 falls on the next clock day, so it's excluded.
    times = ["2026-07-15T14:00", "2026-07-15T23:00", "2026-07-16T00:00"]
    values = [88, 70, 95]
    assert daily_extreme_from_hourly(times, values, date(2026, 7, 15), False, "civil") == 88
    assert daily_extreme_from_hourly(times, values, date(2026, 7, 15), True, "civil") == 70


def test_lst_window_uses_standard_time_offset():
    # UTC stamps; NYC standard offset = -5. LST day 07-15 = UTC [07-15 05:00, 07-16 05:00).
    # The 95 at UTC 07-16 04:00 is 23:00 EST on 07-15 -> belongs to the LST 15th.
    times = ["2026-07-15T04:00", "2026-07-15T18:00", "2026-07-16T04:00", "2026-07-16T06:00"]
    values = [70, 88, 95, 60]
    hi = daily_extreme_from_hourly(times, values, date(2026, 7, 15), False, "lst", std_utc_offset=-5)
    assert hi == 95
    # The naive civil grouping on the same UTC strings would only see 70 and 88.
    assert daily_extreme_from_hourly(times, values, date(2026, 7, 15), False, "civil") == 88


def test_returns_none_when_no_data_in_window():
    assert daily_extreme_from_hourly([], [], date(2026, 7, 15), False, "civil") is None
    assert daily_extreme_from_hourly(["2026-07-20T12:00"], [80], date(2026, 7, 15), False, "civil") is None


def test_skips_missing_values():
    times = ["2026-07-15T10:00", "2026-07-15T14:00", "2026-07-15T16:00"]
    values = [None, 91, None]
    assert daily_extreme_from_hourly(times, values, date(2026, 7, 15), False, "civil") == 91


# --- NBM/NWS blend --------------------------------------------------------- #
def test_blend_shifts_mean_toward_nws_and_keeps_spread():
    out = blend_toward([68, 70, 72], 76, 0.5)   # ens mean 70, target = 0.5*70 + 0.5*76 = 73
    assert abs(sum(out) / len(out) - 73.0) < 1e-9
    assert round(out[2] - out[0], 6) == 4.0     # spread unchanged


def test_blend_is_noop_for_zero_weight_or_missing_nws():
    assert blend_toward([70, 72], 90, 0.0) == [70, 72]
    assert blend_toward([70, 72], None, 0.5) == [70, 72]


def test_blend_weight_one_centers_on_nws():
    out = blend_toward([60, 62, 64], 80, 1.0)
    assert abs(sum(out) / len(out) - 80.0) < 1e-9


class _FakeProv:
    def __init__(self, dist):
        self._d = dist

    def get_forecast(self, parsed):
        return self._d


def test_blended_provider_shifts_ensemble_toward_nws():
    base = _FakeProv(ForecastDistribution(variable="high_temp", members=[68, 70, 72], location="NYC"))
    nws = _FakeProv(ForecastDistribution(variable="high_temp", members=[76], location="NYC"))
    dist = BlendedWeatherProvider(base, nws, weight=0.5).get_forecast(parsed=None)
    assert abs(dist.mean - 73.0) < 1e-6


def test_blended_provider_falls_back_to_base_when_nws_missing():
    base = _FakeProv(ForecastDistribution(variable="high_temp", members=[68, 70, 72], location="NYC"))
    nws = _FakeProv(None)
    dist = BlendedWeatherProvider(base, nws, weight=0.5).get_forecast(parsed=None)
    assert dist.members == [68, 70, 72]      # unchanged
