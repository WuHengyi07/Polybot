"""Fast historical forecast backtest — test in seconds, not days.

For the last ~90 past days (where the answer is already known) it scores how well
the model would have forecast each city's daily high, against a CLIMATOLOGY
baseline. This is the decisive first filter: a model that can't beat climatology
will never beat the market either, so you learn that instantly instead of waiting
weeks for live settlements.

HONEST SCOPE: this measures forecast skill + calibration vs climatology — NOT edge
vs the market (no free historical Kalshi price archive). ERA5 archive ≈ the actual
high (a grid value, not the exact NWS settlement station). Passing here is
necessary but not sufficient; beating the market still needs the live forward-test.

Data (free, no key): Open-Meteo **Historical Forecast API** for the archived
deterministic forecast as it was issued (real ensemble member archives aren't free
for old dates), and the Archive API (ERA5 `temperature_2m_max`) for the actual highs.
The forecast request PINS a real model (`gfs_seamless`); without it the API defaults to
`best_match`, which returns ERA5 reanalysis for past dates == the "actual" (circular, gives
a fake ~0 MAE). Any row with MAE < 0.1F is flagged `suspected_artifact` as a safeguard.
The probability is a Normal around the (bias-corrected) forecast with sigma set from
the realized day-ahead error — a simple MOS-style calibration.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from .calibration import brier_score, calibration_error, crps_ensemble, crps_gaussian
from .market_parser import CITY_REGISTRY
from .utils import clamp, get_logger, normal_cdf, safe_mean, safe_std, utcnow

log = get_logger("historical_backtester")

HISTFORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


# --------------------------------------------------------------------------- #
# Pure scoring core (no network — unit-testable)
# --------------------------------------------------------------------------- #
def _percentile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = q * (len(sorted_vals) - 1)
    lo = int(i)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] * (1 - (i - lo)) + sorted_vals[hi] * (i - lo)


def thresholds_from_actuals(actuals: List[float]) -> List[int]:
    """Integer thresholds (like Kalshi temp lines) spanning the realistic range,
    so probabilities are non-trivial (not all 0 or 1)."""
    s = sorted(actuals)
    qs = (0.1, 0.3, 0.5, 0.7, 0.9)
    return sorted({int(round(_percentile(s, q))) for q in qs})


def model_probability(members: List[float], threshold: float,
                      spread_inflation: float = 1.15, sigma: Optional[float] = None) -> float:
    """P(high > threshold). With an explicit ``sigma`` (deterministic forecast +
    calibrated spread) it's a Normal around the forecast mean; otherwise it's the
    ensemble blend (member-fraction + Normal-CDF) the live bot uses."""
    if not members:
        return 0.5
    mean = safe_mean(members)
    if sigma is not None:
        return clamp(1.0 - normal_cdf(threshold, mean, max(0.5, sigma)), 0.001, 0.999)
    std = max(0.5, safe_std(members) * spread_inflation)
    emp = sum(1 for m in members if m > threshold) / len(members)
    par = 1.0 - normal_cdf(threshold, mean, std)
    return clamp(0.5 * emp + 0.5 * par, 0.001, 0.999)


@dataclass
class HistResult:
    city: str
    n_days: int = 0
    n_predictions: int = 0
    brier_model: Optional[float] = None
    brier_clim: Optional[float] = None
    calib_error_model: Optional[float] = None
    crps_model: Optional[float] = None        # mean CRPS of the Normal forecast (deg F)
    crps_clim: Optional[float] = None         # mean CRPS of the climatology distribution
    mae_forecast: Optional[float] = None      # mean |ensemble mean - actual| (deg F)
    suspected_artifact: bool = False          # MAE ~0 => forecast == actual (non-independent data)
    beats_climatology: bool = False
    model_probs: List[float] = field(default_factory=list)
    clim_probs: List[float] = field(default_factory=list)
    outcomes: List[int] = field(default_factory=list)


def _score_window(fit_days: List[date], eval_days: List[date],
                  forecasts_by_day: Dict[date, List[float]],
                  actuals_by_day: Dict[date, float],
                  thresholds: Optional[List[int]], city: str) -> HistResult:
    """Fit bias/sigma/climatology on ``fit_days``, score the model on ``eval_days``.

    When fit_days == eval_days this is the in-sample score; when fit precedes eval it
    is an out-of-sample (walk-forward) score that exposes overfitting.
    """
    res = HistResult(city=city, n_days=len(eval_days))
    if not fit_days or not eval_days:
        return res
    fit_actuals = [actuals_by_day[d] for d in fit_days]
    if thresholds is None:
        thresholds = thresholds_from_actuals(fit_actuals)
    # climatology P(high>thr) = unconditional base rate over the FIT window
    clim = {thr: clamp(sum(1 for a in fit_actuals if a > thr) / len(fit_actuals), 0.001, 0.999)
            for thr in thresholds}
    # Bias-correct the point forecast and set sigma from the FIT-window day-ahead error
    # (a simple MOS calibration) so the probability is well-formed for one forecast/day.
    bias = safe_mean([safe_mean(forecasts_by_day[d]) - actuals_by_day[d] for d in fit_days])
    sigma = max(1.5, safe_std([safe_mean(forecasts_by_day[d]) - actuals_by_day[d] for d in fit_days]))

    abs_errors, crps_model_days, crps_clim_days = [], [], []
    for d in eval_days:
        actual = actuals_by_day[d]
        point = safe_mean(forecasts_by_day[d])
        corrected = point - bias
        abs_errors.append(abs(point - actual))   # raw (uncorrected) forecast error
        # CRPS scores the whole predictive distribution (sharpness + calibration),
        # not just threshold hits: Normal(corrected, sigma) vs the climatology spread.
        crps_model_days.append(crps_gaussian(corrected, sigma, actual))
        crps_clim_days.append(crps_ensemble(fit_actuals, actual))
        for thr in thresholds:
            res.model_probs.append(model_probability([corrected], thr, sigma=sigma))
            res.clim_probs.append(clim[thr])
            res.outcomes.append(1 if actual > thr else 0)

    res.n_predictions = len(res.outcomes)
    res.brier_model = round(brier_score(res.model_probs, res.outcomes), 4)
    res.brier_clim = round(brier_score(res.clim_probs, res.outcomes), 4)
    res.calib_error_model = round(calibration_error(res.model_probs, res.outcomes), 4)
    res.crps_model = round(safe_mean(crps_model_days), 3)
    res.crps_clim = round(safe_mean(crps_clim_days), 3)
    res.mae_forecast = round(safe_mean(abs_errors), 2)
    # A genuine forecast can never match the actual to within 0.1F — that only happens
    # when the "forecast" and "actual" come from the same source (e.g. reanalysis vs
    # reanalysis). Flag it so the result is not mistaken for skill.
    res.suspected_artifact = res.mae_forecast is not None and res.mae_forecast < 0.1
    res.beats_climatology = res.brier_model < res.brier_clim
    return res


def score_history(forecasts_by_day: Dict[date, List[float]],
                  actuals_by_day: Dict[date, float],
                  thresholds: Optional[List[int]] = None,
                  spread_inflation: float = 1.15,
                  city: str = "?") -> HistResult:
    """In-sample score of the model vs climatology over the common days. Pure (no I/O)."""
    common = sorted(d for d in forecasts_by_day if d in actuals_by_day and forecasts_by_day[d])
    return _score_window(common, common, forecasts_by_day, actuals_by_day, thresholds, city)


def score_history_walkforward(forecasts_by_day: Dict[date, List[float]],
                              actuals_by_day: Dict[date, float],
                              train_frac: float = 0.6,
                              thresholds: Optional[List[int]] = None,
                              city: str = "?") -> HistResult:
    """Out-of-sample score: fit bias/sigma/climatology on the first ``train_frac`` of
    the days, then score ONLY the held-out remainder. A model that merely overfits the
    history will not beat climatology here. Pure (no I/O)."""
    common = sorted(d for d in forecasts_by_day if d in actuals_by_day and forecasts_by_day[d])
    if len(common) < 4:
        return HistResult(city=city)
    n_train = max(1, min(len(common) - 1, int(len(common) * train_frac)))
    return _score_window(common[:n_train], common[n_train:], forecasts_by_day,
                         actuals_by_day, thresholds, city)


# --------------------------------------------------------------------------- #
# Network fetch
# --------------------------------------------------------------------------- #
def fetch_city_history(city, past_days: int = 90, models: str = "gfs_seamless"
                       ) -> Tuple[Dict[date, List[float]], Dict[date, float]]:
    import requests

    sess = requests.Session()
    sess.headers.update({"User-Agent": "prediction_market_bot/1.0"})
    tz = city.timezone
    today = utcnow().date()
    start = (today - timedelta(days=past_days)).isoformat()

    # --- archived deterministic forecast (as issued), grouped to daily highs ---
    # Pin a REAL forecast model. Without `models` the API defaults to best_match, which for
    # past dates returns ERA5 reanalysis == the Archive "actual" below (a circular compare).
    ff = sess.get(HISTFORECAST_URL, params={
        "latitude": city.latitude, "longitude": city.longitude, "hourly": "temperature_2m",
        "models": models, "start_date": start, "end_date": today.isoformat(),
        "temperature_unit": "fahrenheit", "timezone": tz}, timeout=60)
    ff.raise_for_status()
    hourly = ff.json().get("hourly", {})
    day_vals = defaultdict(list)
    for t, v in zip(hourly.get("time", []), hourly.get("temperature_2m", [])):
        if v is not None:
            day_vals[str(t)[:10]].append(v)
    forecasts: Dict[date, List[float]] = {
        date.fromisoformat(d): [max(vs)] for d, vs in day_vals.items() if vs}

    # --- actual daily highs (ERA5 archive) ---
    af = sess.get(ARCHIVE_URL, params={
        "latitude": city.latitude, "longitude": city.longitude, "daily": "temperature_2m_max",
        "start_date": (today - timedelta(days=past_days)).isoformat(), "end_date": today.isoformat(),
        "temperature_unit": "fahrenheit", "timezone": tz}, timeout=60)
    af.raise_for_status()
    daily = af.json().get("daily", {})
    actuals: Dict[date, float] = {}
    for dstr, hi in zip(daily.get("time", []), daily.get("temperature_2m_max", [])):
        if hi is not None:
            actuals[date.fromisoformat(dstr)] = float(hi)
    return forecasts, actuals


def run_historical_backtest(config, cities: Optional[List[str]] = None, past_days: int = 90,
                            walk_forward: bool = False) -> dict:
    cities = cities or config.weather_cities
    # Deterministic forecast model for the archived forecast — NOT openmeteo_models (that is
    # an ensemble id list and would not give a genuine deterministic forecast here).
    models = getattr(config, "histbacktest_forecast_model", "gfs_seamless")
    spread = getattr(config, "spread_inflation", 1.15)
    per_city: Dict[str, HistResult] = {}
    for code in cities:
        city = CITY_REGISTRY.get(code.upper())
        if not city:
            log.warning("Unknown city %s; skipping.", code)
            continue
        try:
            forecasts, actuals = fetch_city_history(city, past_days, models)
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("History fetch failed for %s: %s", code, exc)
            continue
        if walk_forward:
            per_city[code] = score_history_walkforward(forecasts, actuals, city=code)
        else:
            per_city[code] = score_history(forecasts, actuals, spread_inflation=spread, city=code)

    # overall = pooled predictions across cities
    overall = HistResult(city="OVERALL")
    for r in per_city.values():
        overall.model_probs += r.model_probs
        overall.clim_probs += r.clim_probs
        overall.outcomes += r.outcomes
        overall.n_days += r.n_days
    if overall.outcomes:
        overall.n_predictions = len(overall.outcomes)
        overall.brier_model = round(brier_score(overall.model_probs, overall.outcomes), 4)
        overall.brier_clim = round(brier_score(overall.clim_probs, overall.outcomes), 4)
        overall.calib_error_model = round(calibration_error(overall.model_probs, overall.outcomes), 4)
        overall.beats_climatology = overall.brier_model < overall.brier_clim
        # CRPS is a per-day metric, so pool it day-weighted (not per-prediction).
        for attr in ("crps_model", "crps_clim"):
            pairs = [(getattr(r, attr), r.n_days) for r in per_city.values()
                     if getattr(r, attr) is not None and r.n_days]
            tot = sum(w for _, w in pairs)
            if tot:
                setattr(overall, attr, round(sum(v * w for v, w in pairs) / tot, 3))
    return {"per_city": per_city, "overall": overall, "past_days": past_days,
            "walk_forward": walk_forward}


def format_report(result: dict) -> str:
    mode = "  [WALK-FORWARD: out-of-sample]" if result.get("walk_forward") else ""
    lines = ["", "=" * 88, f"  HISTORICAL FORECAST BACKTEST (last {result['past_days']} days){mode}", "=" * 88]
    lines.append(f"  {'city':<8}{'days':>6}{'mBrier':>9}{'climBrier':>11}{'mCRPS':>8}{'cCRPS':>8}"
                 f"{'MAE_F':>7}{'calErr':>8}  verdict")
    lines.append("  " + "-" * 84)

    def row(r: HistResult):
        if r.brier_model is None:
            lines.append(f"  {r.city:<8}{'no data':>6}")
            return
        mae = f"{r.mae_forecast:.1f}" if r.mae_forecast is not None else "  -"
        cm = f"{r.crps_model:.2f}" if r.crps_model is not None else "  -"
        cc = f"{r.crps_clim:.2f}" if r.crps_clim is not None else "  -"
        verdict = ("!! ARTIFACT?" if r.suspected_artifact
                   else ("BEATS clim" if r.beats_climatology else "fails clim"))
        lines.append(f"  {r.city:<8}{r.n_days:>6}{r.brier_model:>9.3f}{r.brier_clim:>11.3f}"
                     f"{cm:>8}{cc:>8}{mae:>7}{r.calib_error_model:>8.3f}  {verdict}")

    for code in sorted(result["per_city"]):
        row(result["per_city"][code])
    lines.append("  " + "-" * 84)
    row(result["overall"])
    ov = result["overall"]
    lines.append("")
    if ov.brier_model is not None:
        if ov.beats_climatology:
            lines.append("  PASS: the model out-forecasts climatology. Worth running the live forward-test.")
        else:
            lines.append("  FAIL: the model does NOT beat climatology -> no forecasting edge. Fix the")
            lines.append("        model (calibration/bias/more models) before trading anything.")
    flagged = [code for code in sorted(result["per_city"])
               if result["per_city"][code].suspected_artifact]
    if flagged:
        lines.append(f"  !! ARTIFACT ({', '.join(flagged)}): forecast == actual (MAE ~0) -- the forecast")
        lines.append("     and 'actual' are NOT independent there; do NOT trust those rows.")
    lines.append("  !! Forecast skill only -- NOT proof of edge vs the MARKET (no historical prices).")
    lines.append("     ERA5 archive approximates the actual high; live settlement uses the NWS station.")
    lines.append("=" * 88)
    return "\n".join(lines)
