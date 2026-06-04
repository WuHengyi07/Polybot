"""NGR / EMOS — Non-homogeneous Gaussian Regression for ensemble calibration.

Raw ensembles are typically under-dispersed (overconfident) and biased. NGR turns
the ensemble (mean, spread) into a well-calibrated predictive Normal:

    mu'    = a + b * ensemble_mean          (bias + regression-to-mean correction)
    sigma'^2 = c + d * ensemble_spread^2     (spread calibration; c,d >= 0)

We fit a, b by ordinary least squares (the squared-error-optimal mean), then fit
c, d by minimizing mean CRPS via a tiny stdlib coordinate (pattern) search — no
numpy/scipy. This replaces the fixed `spread_inflation` fudge with a data-driven,
verifiable correction. Honest scope: fit on enough history and check it OUT OF
SAMPLE (walk-forward) before trusting it — see historical_backtester.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from .calibration import crps_gaussian


def fit_ngr(rows: Sequence[Tuple[float, float, float]], *, min_rows: int = 30,
            iters: int = 60) -> Optional[Dict[str, float]]:
    """Fit NGR coefficients from (ensemble_mean, ensemble_spread, observed) rows.

    Returns {a, b, c, d, crps, n} or None if there is too little data.
    """
    data = [(float(m), float(s), float(o)) for m, s, o in rows]
    n = len(data)
    if n < min_rows:
        return None

    # --- mean model by least squares: obs ~ a + b * ensemble_mean ---
    mbar = sum(m for m, _, _ in data) / n
    obar = sum(o for _, _, o in data) / n
    var_m = sum((m - mbar) ** 2 for m, _, _ in data) / n
    cov = sum((m - mbar) * (o - obar) for m, _, o in data) / n
    b = cov / var_m if var_m > 1e-9 else 1.0
    a = obar - b * mbar

    # --- variance model by CRPS minimization: sigma^2 = c + d * spread^2 ---
    resid = [o - (a + b * m) for m, _, o in data]
    c = max(0.25, sum(r * r for r in resid) / n)   # start at the residual variance
    d = 0.0

    def mean_crps(c_: float, d_: float) -> float:
        total = 0.0
        for m, s, o in data:
            var = c_ + d_ * s * s
            sigma = math.sqrt(var) if var > 1e-9 else 1e-3
            total += crps_gaussian(a + b * m, sigma, o)
        return total / n

    best = mean_crps(c, d)
    steps = [max(1.0, c * 0.5), 1.0]   # (c-step, d-step)
    for _ in range(iters):
        improved = False
        for axis, (dc, dd) in enumerate(((1, 0), (0, 1))):
            for sign in (1, -1):
                nc = c + sign * steps[0] * dc
                nd = d + sign * steps[1] * dd
                if nc <= 1e-3 or nd < 0:
                    continue
                val = mean_crps(nc, nd)
                if val < best - 1e-12:
                    best, c, d = val, nc, nd
                    improved = True
        if not improved:
            steps = [s / 2.0 for s in steps]
            if max(steps) < 1e-3:
                break
    return {"a": round(a, 5), "b": round(b, 5), "c": round(c, 5), "d": round(d, 5),
            "crps": round(best, 4), "n": n}


def apply_ngr(mean: float, spread: float, coeffs: Dict[str, float]) -> Tuple[float, float]:
    """Map a raw (ensemble mean, ensemble spread) to a calibrated (mu, sigma)."""
    a, b = coeffs["a"], coeffs["b"]
    c, d = coeffs["c"], coeffs["d"]
    mu = a + b * mean
    var = max(1e-6, c + d * spread * spread)
    return mu, math.sqrt(var)
