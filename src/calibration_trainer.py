"""Close the calibration loop: learn from settled outcomes.

settle -> score -> TRAIN -> better probabilities. Reads the settled
(model_probability, outcome) pairs and the (forecast_mean, observed_high) pairs
recorded by the scorer, fits an isotonic recalibrator + a per-station bias table,
and persists them. `probability_engine.estimate_probability` already accepts the
`calibrator` and `bias_table` it produces — so the next cycle's probabilities are
corrected by what actually happened.

Until enough data exists it does nothing (safe no-op), so the bot never trades on
a calibrator fit to 3 points.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Optional, Tuple

from .calibration import IsotonicCalibrator, brier_score
from .emos import fit_ngr
from .utils import get_logger

log = get_logger("calibration_trainer")

MIN_PAIRS_FOR_ISOTONIC = 30   # don't fit a recalibrator on a handful of points
MIN_SAMPLES_FOR_BIAS = 5      # per-station minimum
MIN_PAIRS_FOR_NGR = 80        # NGR has 4 params; needs more history than isotonic


def train_from_db(db, *, min_pairs: int = MIN_PAIRS_FOR_ISOTONIC,
                  ngr_min: int = MIN_PAIRS_FOR_NGR) -> dict:
    settlements = db.get_settlements()
    summary: dict = {"n_settlements": len(settlements), "isotonic_fitted": False,
                     "bias_stations": 0, "ngr_fitted": False}

    pairs = [(float(s["model_probability_yes"]), int(s["outcome_yes"]))
             for s in settlements if s.get("model_probability_yes") is not None]
    if len(pairs) >= min_pairs:
        probs, outs = zip(*pairs)
        iso = IsotonicCalibrator().fit(list(probs), list(outs))
        db.save_calibration_params("probability", "isotonic", {"x": iso._x, "y": iso._y})
        summary.update(
            isotonic_fitted=True, n_pairs=len(pairs),
            brier_before=round(brier_score(list(probs), list(outs)), 4),
            brier_after=round(brier_score([iso.predict(p) for p in probs], list(outs)), 4),
        )
    else:
        summary["n_pairs"] = len(pairs)
        log.info("Isotonic skipped: %d/%d pairs", len(pairs), min_pairs)

    bias = _train_bias(db, settlements)
    for station, b in bias.items():
        db.save_calibration_params("bias", station, {"bias": b})
    summary["bias_stations"] = len(bias)
    summary["bias_table"] = bias

    ngr = fit_ngr(_ngr_rows(db, settlements), min_rows=ngr_min)
    if ngr:
        db.save_calibration_params("ngr", "global", ngr)
        summary.update(ngr_fitted=True, ngr=ngr)
    else:
        log.info("NGR skipped: need >= %d (mean,std,observed) rows", ngr_min)
    return summary


def _ngr_rows(db, settlements):
    """(ensemble_mean, ensemble_spread, observed_high) for each settled market that has
    both a recorded weather snapshot and a verified observed high."""
    rows = []
    for s in settlements:
        if s.get("observed_high") is None:
            continue
        snap = db.query(
            "SELECT mean, std FROM weather_snapshots WHERE ticker=? AND mean IS NOT NULL "
            "AND std IS NOT NULL ORDER BY id DESC LIMIT 1", (s["ticker"],))
        if snap:
            rows.append((float(snap[0]["mean"]), float(snap[0]["std"]), float(s["observed_high"])))
    return rows


def _train_bias(db, settlements) -> Dict[str, float]:
    """Per-station mean forecast error = forecast_mean - observed_high (deg F)."""
    samples: Dict[str, list] = defaultdict(list)
    for s in settlements:
        if s.get("observed_high") is None or not s.get("station"):
            continue
        snap = db.query(
            "SELECT mean FROM weather_snapshots WHERE ticker=? AND mean IS NOT NULL ORDER BY id DESC LIMIT 1",
            (s["ticker"],))
        if snap and snap[0].get("mean") is not None:
            samples[s["station"]].append(float(snap[0]["mean"]) - float(s["observed_high"]))
    return {st: round(sum(v) / len(v), 3) for st, v in samples.items() if len(v) >= MIN_SAMPLES_FOR_BIAS}


def load_calibrators(db) -> Tuple[Optional[IsotonicCalibrator], Dict[str, float]]:
    """Reconstruct the persisted (calibrator, bias_table) for the probability engine."""
    calibrator = None
    iso = db.load_latest_calibration("probability").get("isotonic")
    if iso and iso.get("x") and iso.get("y"):
        calibrator = IsotonicCalibrator()
        calibrator._x = [float(x) for x in iso["x"]]
        calibrator._y = [float(y) for y in iso["y"]]
        calibrator.fitted = True
    bias_rows = db.load_latest_calibration("bias")
    bias_table = {st: float(p["bias"]) for st, p in bias_rows.items() if "bias" in p}
    return calibrator, bias_table


def load_ngr(db) -> Optional[dict]:
    """Reconstruct the persisted NGR coefficients (or None if never fitted)."""
    params = db.load_latest_calibration("ngr").get("global")
    if params and all(k in params for k in ("a", "b", "c", "d")):
        return {k: float(params[k]) for k in ("a", "b", "c", "d")}
    return None
