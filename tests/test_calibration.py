"""Scoring primitives: CRPS (gaussian + ensemble) and the rank histogram."""
import random

from src.calibration import crps_ensemble, crps_gaussian, rank_histogram


# --- CRPS for a Gaussian predictive distribution --------------------------- #
def test_crps_gaussian_near_zero_for_perfect_sharp_forecast():
    # A near-certain forecast sitting on the observation scores ~0.
    assert crps_gaussian(70.0, 1e-9, 70.0) < 1e-3


def test_crps_gaussian_decreases_as_mean_approaches_obs():
    far = crps_gaussian(60.0, 3.0, 70.0)
    near = crps_gaussian(69.0, 3.0, 70.0)
    assert near < far


def test_crps_gaussian_punishes_confident_and_wrong():
    # Same mean, observation in the tail: a too-narrow sigma is worse than an honest wide one.
    obs, mean = 75.0, 70.0
    overconfident = crps_gaussian(mean, 1.0, obs)
    honest = crps_gaussian(mean, 5.0, obs)
    assert overconfident > honest


# --- CRPS for a raw ensemble ----------------------------------------------- #
def test_crps_ensemble_reduces_to_abs_error_for_one_member():
    assert abs(crps_ensemble([70.0], 73.0) - 3.0) < 1e-9


def test_crps_ensemble_lower_when_centered_on_obs():
    centered = crps_ensemble([68, 69, 70, 71, 72], 70)
    offset = crps_ensemble([60, 61, 62, 63, 64], 70)
    assert centered < offset


# --- Rank histogram (Talagrand) -------------------------------------------- #
def test_rank_histogram_has_m_plus_one_bins():
    hist = rank_histogram([[1, 2, 3]], [2.5])
    assert len(hist) == 4  # 3 members -> 4 ranks


def test_rank_histogram_uniform_for_calibrated_ensemble():
    # Observation and members are exchangeable draws -> rank is ~uniform, so the
    # extremes do NOT hold the majority.
    rng = random.Random(1)
    ensembles, obs = [], []
    for _ in range(3000):
        obs.append(rng.gauss(0, 5))
        ensembles.append([rng.gauss(0, 5) for _ in range(9)])
    hist = rank_histogram(ensembles, obs)
    assert hist[0] + hist[-1] < 0.5 * sum(hist)


def test_rank_histogram_flags_underdispersion():
    # Members clustered far tighter than the real error -> obs lands outside the
    # ensemble range often -> U-shaped histogram (extremes dominate).
    rng = random.Random(0)
    ensembles, obs = [], []
    for _ in range(800):
        truth = rng.gauss(0, 5)
        obs.append(truth)
        center = truth + rng.gauss(0, 5)            # forecast error ~5F
        ensembles.append([center + rng.gauss(0, 1) for _ in range(10)])  # spread only ~1F
    hist = rank_histogram(ensembles, obs)
    assert hist[0] + hist[-1] > 0.5 * sum(hist)
