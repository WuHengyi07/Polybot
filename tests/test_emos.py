"""NGR / EMOS spread calibration: fit a Normal predictive distribution that fixes
ensemble under-dispersion and mean bias, minimizing CRPS over history."""
import random

from src.calibration import crps_gaussian
from src.emos import apply_ngr, fit_ngr


def _rows(n, seed):
    """Ensemble with a +2F mean bias and a far-too-tight spread (1F) vs ~4F real error."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        truth = rng.gauss(60, 10)
        ens_mean = truth + 2.0 + rng.gauss(0, 4)   # biased + noisy
        ens_spread = 1.0                            # under-dispersed
        rows.append((ens_mean, ens_spread, truth))
    return rows


def test_ngr_widens_underdispersed_spread():
    coeffs = fit_ngr(_rows(800, 0))
    _, sigma = apply_ngr(60.0, 1.0, coeffs)
    assert sigma > 1.5            # the raw 1F spread is inflated toward the real error


def test_ngr_corrects_mean_bias():
    coeffs = fit_ngr(_rows(800, 2))
    mu, _ = apply_ngr(62.0, 1.0, coeffs)   # ensemble mean 62 carries +2 bias
    assert mu < 61.5                       # bias pulled out


def test_ngr_lowers_crps_vs_raw():
    rows = _rows(800, 1)
    coeffs = fit_ngr(rows)
    raw = sum(crps_gaussian(m, max(0.5, s), o) for m, s, o in rows) / len(rows)
    cal = 0.0
    for m, s, o in rows:
        mu, sigma = apply_ngr(m, s, coeffs)
        cal += crps_gaussian(mu, sigma, o)
    cal /= len(rows)
    assert cal < raw             # calibrated NGR beats the raw (biased mean, tight spread)


def test_ngr_returns_none_below_min_rows():
    assert fit_ngr([(60, 1, 60)] * 3, min_rows=30) is None
