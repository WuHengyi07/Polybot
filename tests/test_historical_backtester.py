"""Historical backtest scoring (pure functions, no network)."""
import random
from datetime import date, timedelta

from src.historical_backtester import (model_probability, score_history,
                                       score_history_walkforward,
                                       thresholds_from_actuals)


def _days(n):
    return [date(2025, 1, 1) + timedelta(days=i) for i in range(n)]


def test_thresholds_span_range_and_unique():
    ts = thresholds_from_actuals([50, 55, 60, 65, 70, 75, 80])
    assert ts == sorted(set(ts)) and len(ts) >= 3
    assert 50 <= min(ts) and max(ts) <= 80


def test_model_probability_monotonic_in_threshold():
    members = [70, 72, 74, 76, 78, 80]
    assert model_probability(members, 60) > model_probability(members, 75) > model_probability(members, 90)


def test_good_model_beats_climatology():
    rng = random.Random(0)
    actuals, forecasts = {}, {}
    for d in _days(60):
        true_high = 60 + rng.gauss(0, 8)
        actuals[d] = true_high
        forecasts[d] = [true_high + rng.gauss(0, 1.5) for _ in range(20)]  # tightly on target
    res = score_history(forecasts, actuals, city="NYC")
    assert res.n_days == 60 and res.n_predictions > 0
    assert res.brier_model < res.brier_clim
    assert res.beats_climatology is True
    assert res.mae_forecast < 3.0


def test_no_skill_model_does_not_beat_climatology():
    rng = random.Random(1)
    actuals, forecasts = {}, {}
    for d in _days(80):
        actuals[d] = 60 + rng.gauss(0, 8)
        forecasts[d] = [60.0]   # constant, climatology-equivalent: no day-to-day skill
    res = score_history(forecasts, actuals, city="NYC")
    # after bias+spread calibration a no-skill forecast should ~tie, not beat, climatology
    assert res.brier_model >= res.brier_clim - 0.03


def test_crps_model_beats_climatology_for_sharp_model():
    rng = random.Random(2)
    actuals, forecasts = {}, {}
    for d in _days(60):
        true_high = 60 + rng.gauss(0, 8)
        actuals[d] = true_high
        forecasts[d] = [true_high + rng.gauss(0, 1.5) for _ in range(20)]
    res = score_history(forecasts, actuals, city="NYC")
    assert res.crps_model is not None and res.crps_clim is not None
    assert res.crps_model < res.crps_clim       # sharp forecast beats climatology on CRPS too


def test_walkforward_scores_only_the_test_window():
    rng = random.Random(4)
    actuals, forecasts = {}, {}
    for d in _days(100):
        true_high = 60 + rng.gauss(0, 8)
        actuals[d] = true_high
        forecasts[d] = [true_high + rng.gauss(0, 1.5) for _ in range(20)]
    res = score_history_walkforward(forecasts, actuals, train_frac=0.6, city="NYC")
    assert res.n_days == 40                      # 100 days, 60% train -> 40 held-out test days
    assert res.brier_model < res.brier_clim      # sharp model wins OUT OF SAMPLE, not just in-sample


def test_walkforward_needs_enough_history():
    res = score_history_walkforward({}, {}, city="X")
    assert res.n_days == 0 and res.brier_model is None


def test_identical_forecast_and_actual_is_flagged_as_artifact():
    # forecast == actual => circular data source (e.g. reanalysis vs reanalysis) -> MAE ~0.
    actuals, forecasts = {}, {}
    for i, d in enumerate(_days(40)):
        v = 60.0 + (i % 10)
        actuals[d] = v
        forecasts[d] = [v]
    res = score_history(forecasts, actuals, city="X")
    assert res.mae_forecast is not None and res.mae_forecast < 0.1
    assert res.suspected_artifact is True


def test_realistic_forecast_not_flagged_as_artifact():
    rng = random.Random(9)
    actuals, forecasts = {}, {}
    for d in _days(60):
        t = 60 + rng.gauss(0, 8)
        actuals[d] = t
        forecasts[d] = [t + rng.gauss(0, 1.5) for _ in range(20)]
    res = score_history(forecasts, actuals, city="X")
    assert res.mae_forecast > 0.1 and res.suspected_artifact is False


def test_empty_history():
    res = score_history({}, {}, city="X")
    assert res.n_days == 0 and res.brier_model is None and res.crps_model is None
    assert res.suspected_artifact is False
