"""Turn a forecast distribution into P(YES) for a parsed market.

Two estimators are combined:
  * empirical  — fraction of ensemble members satisfying the event
  * parametric — Normal(mean, inflated std) CDF over the threshold/bracket
The parametric estimate handles the tails better when only a few members are
near the threshold; the blend is robust to both. A heuristic confidence score
(0-1) gates trading via MIN_MODEL_CONFIDENCE.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from . import calibration
from .market_parser import Direction, ParsedMarket
from .utils import clamp, get_logger, normal_cdf

log = get_logger("probability_engine")


@dataclass
class ProbabilityEstimate:
    model_probability_yes: float
    confidence: float
    method: str
    components: Dict[str, float] = field(default_factory=dict)


def _event_probability(direction: Direction, threshold, threshold_high,
                       dist, mean: float, std: float):
    """Return (empirical_p, parametric_p) for P(event is YES)."""
    if direction == Direction.ABOVE:
        emp = dist.fraction_at_least(threshold)
        par = 1.0 - normal_cdf(threshold, mean, std)
    elif direction == Direction.BELOW:
        emp = dist.fraction_at_most(threshold)
        par = normal_cdf(threshold, mean, std)
    elif direction == Direction.BETWEEN:
        emp = dist.fraction_between(threshold, threshold_high)
        par = normal_cdf(threshold_high, mean, std) - normal_cdf(threshold, mean, std)
    else:
        return None, None
    return clamp(emp), clamp(par)


def _confidence(n_members: int, std: float, mean: float, horizon_hours: Optional[float]) -> float:
    """Heuristic 0-1 reliability score (NOT the edge).

    Higher when: more ensemble members, shorter lead time, sane (not absurd)
    spread. Documented placeholder — replace with a data-driven score once you
    have verification history.
    """
    sample_factor = clamp(n_members / 30.0)
    h = 24.0 if horizon_hours is None else horizon_hours
    horizon_factor = clamp(1.0 - max(0.0, (h - 24.0)) / 240.0, 0.3, 1.0)
    rel_spread = std / max(abs(mean), 1.0)
    dispersion_factor = clamp(1.0 - rel_spread * 3.0, 0.3, 1.0)
    return round(clamp(0.4 * sample_factor + 0.4 * horizon_factor + 0.2 * dispersion_factor), 4)


def estimate_probability(parsed: ParsedMarket, dist, config,
                         *, calibrator=None, bias_table=None, ngr=None) -> Optional[ProbabilityEstimate]:
    """Compute P(YES) + confidence for one market given its ensemble forecast."""
    if dist is None or dist.n == 0 or parsed.threshold is None:
        return None

    # --- distribution corrections (safe identities by default) ---
    if ngr is not None and getattr(config, "use_ngr", False):
        # NGR/EMOS does its own bias + spread calibration (mu'=a+b*mean,
        # sigma'^2=c+d*spread^2), so it replaces bias_correct + inflate_std.
        from .emos import apply_ngr
        mean, std = apply_ngr(dist.mean, dist.std, ngr)
    else:
        mean = dist.mean
        mean = calibration.bias_correct_mean(mean, station=parsed.station,
                                             model=dist.model, bias_table=bias_table)
        mean = calibration.seasonal_adjustment(
            mean, parsed.target_date.month if parsed.target_date else None)
        std = calibration.inflate_std(dist.std, getattr(config, "spread_inflation", 1.0))
        std = calibration.horizon_adjustment_std(std, dist.horizon_hours)
    if std <= 0:
        std = 0.5  # avoid a degenerate point mass when all members agree exactly

    emp, par = _event_probability(parsed.direction, parsed.threshold,
                                  parsed.threshold_high, dist, mean, std)
    if emp is None:
        return None

    raw_p = 0.5 * emp + 0.5 * par
    p = calibration.recalibrate_probability(raw_p, calibrator)
    p = clamp(p, 0.001, 0.999)

    conf = _confidence(dist.n, std, mean, dist.horizon_hours)

    # If a live observation already determines the outcome, mark it "decided"
    # (lets the signal engine bypass the near-settlement skip with no forecast risk).
    obs_hsf = getattr(dist, "obs_high_so_far", None)
    obs_decided = False
    if obs_hsf is not None:
        if parsed.direction == Direction.ABOVE:
            obs_decided = obs_hsf > parsed.threshold
        elif parsed.direction == Direction.BELOW:
            obs_decided = obs_hsf >= parsed.threshold
        elif parsed.direction == Direction.BETWEEN and parsed.threshold_high is not None:
            obs_decided = obs_hsf > parsed.threshold_high
        if obs_decided:
            conf = max(conf, 0.99)

    return ProbabilityEstimate(
        model_probability_yes=round(p, 4),
        confidence=conf,
        method="blend(empirical,normal_cdf)",
        components={
            "empirical_p": round(emp, 4),
            "parametric_p": round(par, 4),
            "raw_blend_p": round(raw_p, 4),
            "mean_used": round(mean, 3),
            "std_used": round(std, 3),
            "n_members": dist.n,
            "threshold": parsed.threshold,
            "threshold_high": parsed.threshold_high,
            "direction": parsed.direction.value,
            "horizon_hours": dist.horizon_hours,
            "obs_high_so_far": obs_hsf,
            "obs_decided": obs_decided,
        },
    )
