"""Forecast calibration helpers.

V1 ships SAFE no-op defaults (identity recalibration, zero bias) so the bot runs
without any historical training data, but every hook is a real, documented place
to add the corrections the research memo calls for:

  * station/model bias correction (subtract recent mean forecast error)
  * ensemble spread inflation (raw ensembles are typically under-dispersive)
  * probability recalibration (Platt / isotonic) once you have outcome history
  * seasonal & forecast-horizon adjustments
  * scoring (Brier) + reliability tables to measure calibration

Nothing here will silently distort probabilities until you provide real data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

from .utils import clamp, normal_cdf


# --------------------------------------------------------------------------- #
# Distribution-level corrections (applied before computing the probability)
# --------------------------------------------------------------------------- #
def bias_correct_mean(mean: float, *, station: str = None, model: str = None,
                      bias_table: Dict[str, float] = None) -> float:
    """Subtract a known mean forecast error for this station/model.

    bias_table maps a key like 'KNYC:open-meteo' -> degrees of systematic bias
    (forecast minus observed). Default: no table -> no change.
    """
    if not bias_table:
        return mean
    key = f"{station}:{model}"
    return mean - bias_table.get(key, bias_table.get(station or "", 0.0))


def inflate_std(std: float, factor: float) -> float:
    """Widen ensemble spread to counter under-dispersion. factor >= 1.0."""
    return std * max(1.0, factor)


def seasonal_adjustment(mean: float, target_month: int = None) -> float:
    """Placeholder hook for a seasonal correction. Identity by default."""
    return mean


def horizon_adjustment_std(std: float, horizon_hours: float = None) -> float:
    """Placeholder: spread should grow with lead time. Identity by default."""
    return std


def recent_observation_nudge(mean: float, recent_obs: float = None, weight: float = 0.0) -> float:
    """Optionally blend the latest observation toward the forecast mean."""
    if recent_obs is None or weight <= 0:
        return mean
    return (1 - weight) * mean + weight * recent_obs


# --------------------------------------------------------------------------- #
# Probability recalibration (applied after computing the raw probability)
# --------------------------------------------------------------------------- #
class PlattScaler:
    """Sigmoid recalibration p' = 1/(1+exp(a*logit(p)+b)). Identity until fit."""

    def __init__(self, a: float = 1.0, b: float = 0.0):
        self.a, self.b = a, b
        self.fitted = False

    def predict(self, p: float) -> float:
        import math
        p = clamp(p, 1e-6, 1 - 1e-6)
        logit = math.log(p / (1 - p))
        z = self.a * logit + self.b
        return clamp(1.0 / (1.0 + math.exp(-z)))


class IsotonicCalibrator:
    """Monotonic recalibration via pool-adjacent-violators. Identity until fit."""

    def __init__(self):
        self._x: List[float] = []
        self._y: List[float] = []
        self.fitted = False

    def fit(self, probs: Sequence[float], outcomes: Sequence[int]) -> "IsotonicCalibrator":
        # Aggregate by unique x first (ties must map to one value), then run a
        # block-based weighted Pool-Adjacent-Violators. This guarantees the fit
        # minimizes squared error subject to monotonicity (so it never worsens
        # the training Brier) and is a well-defined function of x.
        agg: dict = {}
        for p, o in zip(probs, outcomes):
            a = agg.setdefault(round(float(p), 6), [0.0, 0])
            a[0] += float(o)
            a[1] += 1
        blocks = []  # each: [value, weight, x_left, x_right]
        for x in sorted(agg):
            s, c = agg[x]
            blocks.append([s / c, float(c), x, x])
            while len(blocks) >= 2 and blocks[-2][0] > blocks[-1][0] + 1e-12:
                v2, w2, _l2, r2 = blocks.pop()
                v1, w1, l1, _r1 = blocks.pop()
                blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, l1, r2])
        fx, fy = [], []
        for v, _w, l, r in blocks:
            fx.append(l)
            fy.append(v)
            if r != l:
                fx.append(r)
                fy.append(v)
        self._x, self._y = fx, fy
        self.fitted = bool(fx)
        return self

    def predict(self, p: float) -> float:
        if not self.fitted:
            return p
        # piecewise-constant / linear interpolation over fitted points
        if p <= self._x[0]:
            return clamp(self._y[0])
        if p >= self._x[-1]:
            return clamp(self._y[-1])
        for i in range(1, len(self._x)):
            if p <= self._x[i]:
                x0, x1 = self._x[i - 1], self._x[i]
                y0, y1 = self._y[i - 1], self._y[i]
                if x1 == x0:
                    return clamp(y1)
                return clamp(y0 + (y1 - y0) * (p - x0) / (x1 - x0))
        return clamp(p)


def recalibrate_probability(p: float, calibrator=None) -> float:
    """Apply a fitted calibrator if provided, else identity."""
    if calibrator is None:
        return clamp(p)
    return clamp(calibrator.predict(p))


# --------------------------------------------------------------------------- #
# Scoring / diagnostics
# --------------------------------------------------------------------------- #
def brier_score(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error between forecast probability and 0/1 outcome."""
    if not probs:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def crps_gaussian(mean: float, std: float, obs: float) -> float:
    """Closed-form CRPS of a Normal(mean, std) forecast against a scalar observation.

    Lower is better; 0 for a perfect sharp forecast. Unlike a hit/miss score it
    rewards honest uncertainty: a too-narrow spread that turns out wrong is
    penalized more than a wider, well-hedged one. Degrees, not probabilities.
    """
    if std <= 1e-12:
        return abs(obs - mean)
    z = (obs - mean) / std
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    cdf = normal_cdf(z)  # standard normal CDF
    return std * (z * (2.0 * cdf - 1.0) + 2.0 * pdf - 1.0 / math.sqrt(math.pi))


def crps_ensemble(members: Sequence[float], obs: float) -> float:
    """CRPS of a raw ensemble vs a scalar obs:
    mean|x_i - y| - (1/(2 m^2)) * sum_ij |x_i - x_j|. Reduces to |x - y| for one member."""
    m = len(members)
    if m == 0:
        return 0.0
    term1 = sum(abs(x - obs) for x in members) / m
    term2 = sum(abs(a - b) for a in members for b in members) / (2.0 * m * m)
    return term1 - term2


def rank_histogram(ensembles: Sequence[Sequence[float]],
                   observations: Sequence[float]) -> List[int]:
    """Talagrand rank histogram. For each (ensemble, obs) the rank = #members < obs
    (0..m), tallied into m+1 bins. Uniform => calibrated spread; U-shaped (extremes
    dominate) => under-dispersed (the obs falls outside the ensemble too often);
    dome-shaped => over-dispersed."""
    pairs = [(list(e), o) for e, o in zip(ensembles, observations) if e]
    if not pairs:
        return []
    m = len(pairs[0][0])
    hist = [0] * (m + 1)
    for members, o in pairs:
        rank = sum(1 for x in members if x < o)
        hist[min(rank, m)] += 1
    return hist


@dataclass
class ReliabilityBin:
    lo: float
    hi: float
    count: int = 0
    sum_prob: float = 0.0
    sum_outcome: float = 0.0

    @property
    def avg_prob(self) -> float:
        return self.sum_prob / self.count if self.count else 0.0

    @property
    def observed_freq(self) -> float:
        return self.sum_outcome / self.count if self.count else 0.0


def reliability_table(probs: Sequence[float], outcomes: Sequence[int], bins: int = 10) -> List[ReliabilityBin]:
    table = [ReliabilityBin(i / bins, (i + 1) / bins) for i in range(bins)]
    for p, o in zip(probs, outcomes):
        idx = min(bins - 1, int(p * bins))
        b = table[idx]
        b.count += 1
        b.sum_prob += p
        b.sum_outcome += o
    return table


def calibration_error(probs: Sequence[float], outcomes: Sequence[int], bins: int = 10) -> float:
    """Expected Calibration Error: avg |avg_prob - observed_freq| weighted by bin count."""
    table = reliability_table(probs, outcomes, bins)
    n = sum(b.count for b in table)
    if not n:
        return 0.0
    return sum(b.count * abs(b.avg_prob - b.observed_freq) for b in table) / n
