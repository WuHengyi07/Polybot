"""Calibration feedback loop: fit from settled outcomes, persist, reload."""
import random

from src.calibration_trainer import load_calibrators, load_ngr, train_from_db
from src.database import Database
from src.emos import apply_ngr


def _seed_miscalibrated(db, n=80, seed=0):
    """Model is over-confident: it says p, but true rate is ~0.8*p. Isotonic should help."""
    rng = random.Random(seed)
    for i in range(n):
        p = round((i % 9) / 10.0 + 0.05, 3)          # 0.05 .. 0.85
        outcome = 1 if rng.random() < p * 0.8 else 0
        db.record_settlement({"ticker": f"T{i}", "target_date": "", "station": "",
                              "outcome_yes": outcome, "observed_high": None,
                              "model_probability_yes": p, "market_implied_yes": None,
                              "source": "mock", "mode": "paper"})


def test_isotonic_fits_and_does_not_worsen_training_brier():
    db = Database(":memory:")
    _seed_miscalibrated(db, n=80)
    summary = train_from_db(db, min_pairs=30)
    assert summary["isotonic_fitted"] is True
    # isotonic regression minimizes squared error on the training set
    assert summary["brier_after"] <= summary["brier_before"] + 1e-9


def test_skips_when_too_few_pairs():
    db = Database(":memory:")
    _seed_miscalibrated(db, n=5)
    summary = train_from_db(db, min_pairs=30)
    assert summary["isotonic_fitted"] is False


def test_load_roundtrip():
    db = Database(":memory:")
    _seed_miscalibrated(db, n=80)
    train_from_db(db, min_pairs=30)
    calibrator, bias_table = load_calibrators(db)
    assert calibrator is not None and calibrator.fitted
    assert 0.0 <= calibrator.predict(0.5) <= 1.0
    assert isinstance(bias_table, dict)


def _seed_for_ngr(db, n=120, seed=7):
    """Settlements with observed_high + matching weather snapshots (biased, tight spread)."""
    rng = random.Random(seed)
    for i in range(n):
        truth = rng.gauss(60, 10)
        ens_mean = truth + 2.0 + rng.gauss(0, 4)
        db._insert("weather_snapshots", {
            "ts": "", "ticker": f"NG{i}", "location": "NYC", "target_date": "",
            "variable": "high_temp", "model": "m", "members_json": "[]",
            "mean": ens_mean, "std": 1.0, "n_members": 20})
        db.record_settlement({"ticker": f"NG{i}", "target_date": "", "station": "KNYC",
                              "outcome_yes": 1 if truth > 60 else 0, "observed_high": truth,
                              "model_probability_yes": 0.5, "market_implied_yes": None,
                              "source": "mock", "mode": "paper"})


def test_ngr_fits_and_inflates_tight_spread():
    db = Database(":memory:")
    _seed_for_ngr(db, n=120)
    summary = train_from_db(db, min_pairs=30, ngr_min=60)
    assert summary["ngr_fitted"] is True
    ngr = load_ngr(db)
    assert ngr is not None
    _, sigma = apply_ngr(60.0, 1.0, ngr)
    assert sigma > 1.5            # the too-tight 1F spread is inflated toward real error


def test_ngr_skipped_when_too_few():
    db = Database(":memory:")
    _seed_for_ngr(db, n=20)
    summary = train_from_db(db, ngr_min=60)
    assert summary["ngr_fitted"] is False
    assert load_ngr(db) is None
