"""Signal generation with a reason code on every decision.

The signal engine decides the INTENT (enter / hold / exit / skip) from edge,
market quality, and forecast confidence. The risk_manager is the final gate and
position sizer — it can still veto or shrink any BUY signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .ev_engine import remaining_edge
from .utils import get_logger

log = get_logger("signal_engine")


# Signal types
BUY_YES = "BUY_YES"
BUY_NO = "BUY_NO"
HOLD = "HOLD"
EXIT = "EXIT"
DO_NOT_TRADE = "DO_NOT_TRADE"


# Reason codes (stable strings for logging/auditing)
class R:
    EDGE_YES = "EDGE_YES"
    EDGE_NO = "EDGE_NO"
    FAVORITE_EDGE = "FAVORITE_EDGE"
    EDGE_TO_PRICE_LOW = "EDGE_TO_PRICE_LOW"
    EDGE_BELOW_THRESHOLD = "EDGE_BELOW_THRESHOLD"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    UNCLEAR_SETTLEMENT = "UNCLEAR_SETTLEMENT"
    UNPARSEABLE = "UNPARSEABLE"
    NO_FORECAST = "NO_FORECAST"
    NEAR_SETTLEMENT = "NEAR_SETTLEMENT"
    PRICE_OUT_OF_RANGE = "PRICE_OUT_OF_RANGE"
    SUSPICIOUS_EDGE = "SUSPICIOUS_EDGE"
    NO_EDGE_SEGMENT = "NO_EDGE_SEGMENT"
    EDGE_INTACT = "EDGE_INTACT"
    EDGE_DECAYED = "EDGE_DECAYED"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    LIQUIDITY_DETERIORATED = "LIQUIDITY_DETERIORATED"
    PRICE_AT_FAIR_VALUE = "PRICE_AT_FAIR_VALUE"


@dataclass
class Signal:
    ticker: str
    signal_type: str
    side: Optional[str] = None            # "yes" | "no"
    edge: float = 0.0
    model_probability: float = 0.0
    confidence: float = 0.0
    reason_codes: List[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def _hours_to_close(market, now=None) -> Optional[float]:
    from .utils import hours_until
    return hours_until(market.close_time, now=now)


def generate_entry_signal(parsed, market, prob_est, ev_result, config,
                          pruned_locations=None) -> Signal:
    """Decide whether to open a position in a market we don't yet hold."""
    base = dict(ticker=market.ticker)

    # 1) Must be cleanly parseable with a clear settlement source.
    if not parsed.tradeable:
        code = R.UNCLEAR_SETTLEMENT if "settlement" in parsed.reason else R.UNPARSEABLE
        return Signal(signal_type=DO_NOT_TRADE, reason_codes=[code],
                      detail={"reason": parsed.reason}, **base)

    # 1b) Skip cities where the model has historically LOST to the market.
    if pruned_locations and parsed.location in pruned_locations:
        return Signal(signal_type=DO_NOT_TRADE, reason_codes=[R.NO_EDGE_SEGMENT],
                      detail={"location": parsed.location}, **base)

    # 2) Must have a forecast.
    if prob_est is None:
        return Signal(signal_type=DO_NOT_TRADE, reason_codes=[R.NO_FORECAST], **base)

    p = prob_est.model_probability_yes
    conf = prob_est.confidence
    common = dict(model_probability=p, confidence=conf, **base)

    # 3) Market-quality gates (skip, don't trade).
    if market.yes_spread > config.max_spread:
        return Signal(signal_type=DO_NOT_TRADE, reason_codes=[R.SPREAD_TOO_WIDE],
                      detail={"spread": market.yes_spread, "max": config.max_spread}, **common)
    if market.volume < config.min_volume:
        return Signal(signal_type=DO_NOT_TRADE, reason_codes=[R.LOW_LIQUIDITY],
                      detail={"volume": market.volume, "min": config.min_volume}, **common)
    if conf < config.min_model_confidence:
        return Signal(signal_type=DO_NOT_TRADE, reason_codes=[R.LOW_CONFIDENCE],
                      detail={"confidence": conf, "min": config.min_model_confidence}, **common)

    # Near-settlement is normally skipped — UNLESS a live observation already
    # decides the outcome (no forecast risk left), which is the intraday/METAR edge.
    obs_decided = bool(prob_est.components.get("obs_decided"))
    h = _hours_to_close(market)
    if (h is not None and h < config.min_hours_to_close
            and not config.allow_near_settlement and not obs_decided):
        return Signal(signal_type=DO_NOT_TRADE, reason_codes=[R.NEAR_SETTLEMENT],
                      detail={"hours_to_close": round(h, 2)}, **common)

    # 3b) Suspiciously large edge => assume the MODEL is wrong, not the market.
    if ev_result.best_side and ev_result.best_edge_net > config.max_plausible_edge:
        return Signal(signal_type=DO_NOT_TRADE, side=ev_result.best_side,
                      edge=ev_result.best_edge_net, reason_codes=[R.SUSPICIOUS_EDGE],
                      detail={"edge": ev_result.best_edge_net, "max": config.max_plausible_edge}, **common)

    # 4) Edge decision (against executable ASK prices, after fees+slippage).
    # Favorite mode: if the side we'd buy is a high-confidence favorite the model
    # thinks is underpriced, allow it at a lower edge bar (still a positive net edge).
    # The favorite-longshot bias makes these both higher win rate AND positive EV.
    side_p = p if ev_result.best_side == "yes" else (1.0 - p)
    is_favorite = (getattr(config, "favorite_mode", False) and bool(ev_result.best_side)
                   and side_p >= getattr(config, "favorite_p_floor", 1.0))
    eff_threshold = config.entry_edge_threshold
    if is_favorite:
        eff_threshold = min(eff_threshold, config.entry_edge_threshold_favorite)

    if ev_result.best_side and ev_result.best_edge_net >= eff_threshold:
        # Reject cheap-lottery / near-certain executable prices (the 1c/99c trap).
        entry = ev_result.entry_price or 0.0
        if entry < config.min_price or entry > config.max_price:
            return Signal(signal_type=DO_NOT_TRADE, side=ev_result.best_side,
                          edge=ev_result.best_edge_net, reason_codes=[R.PRICE_OUT_OF_RANGE],
                          detail={"entry_price": entry, "min": config.min_price,
                                  "max": config.max_price}, **common)
        # Edge-to-price ratio guard: a cheap contract must clear a larger RELATIVE
        # net edge, since the flat slippage/fee drag eats small-priced bets.
        ratio_min = getattr(config, "min_edge_to_price_ratio", 0.0)
        if ratio_min > 0 and entry > 0 and (ev_result.best_edge_net / entry) < ratio_min:
            return Signal(signal_type=DO_NOT_TRADE, side=ev_result.best_side,
                          edge=ev_result.best_edge_net, reason_codes=[R.EDGE_TO_PRICE_LOW],
                          detail={"edge_net": ev_result.best_edge_net, "entry_price": entry,
                                  "ratio": round(ev_result.best_edge_net / entry, 4),
                                  "min_ratio": ratio_min}, **common)
        sig_type = BUY_YES if ev_result.best_side == "yes" else BUY_NO
        reasons = [R.EDGE_YES if ev_result.best_side == "yes" else R.EDGE_NO]
        if is_favorite:
            reasons.append(R.FAVORITE_EDGE)
        return Signal(signal_type=sig_type, side=ev_result.best_side,
                      edge=ev_result.best_edge_net, reason_codes=reasons,
                      detail={"entry_price": ev_result.entry_price,
                              "edge_yes_net": ev_result.edge_yes_net,
                              "edge_no_net": ev_result.edge_no_net,
                              "favorite": is_favorite}, **common)

    return Signal(signal_type=HOLD, edge=ev_result.best_edge_net,
                  reason_codes=[R.EDGE_BELOW_THRESHOLD],
                  detail={"best_edge_net": ev_result.best_edge_net,
                          "threshold": eff_threshold}, **common)


def generate_exit_signal(position, parsed, market, prob_est, config) -> Signal:
    """Decide whether to close a position we already hold."""
    base = dict(ticker=market.ticker, side=position.side)

    if prob_est is None:
        # Can't re-evaluate the thesis; hold rather than panic-sell on missing data.
        return Signal(signal_type=HOLD, reason_codes=[R.NO_FORECAST], **base)

    p = prob_est.model_probability_yes
    rem = remaining_edge(position.side, market, p)
    common = dict(model_probability=p, confidence=prob_est.confidence, edge=rem, **base)

    # Liquidity / spread deteriorated — exit while we still can.
    if market.yes_spread > config.max_spread or market.volume < config.min_volume:
        return Signal(signal_type=EXIT, reason_codes=[R.LIQUIDITY_DETERIORATED],
                      detail={"spread": market.yes_spread, "volume": market.volume}, **common)

    # Too close to settlement and not allowed to hold through it.
    h = _hours_to_close(market)
    if h is not None and h < config.min_hours_to_close and not config.allow_near_settlement:
        return Signal(signal_type=EXIT, reason_codes=[R.NEAR_SETTLEMENT],
                      detail={"hours_to_close": round(h, 2)}, **common)

    # Edge gone / moved against us.
    if rem < 0:
        return Signal(signal_type=EXIT, reason_codes=[R.THESIS_INVALIDATED],
                      detail={"remaining_edge": rem}, **common)
    if rem <= config.exit_edge_threshold:
        return Signal(signal_type=EXIT, reason_codes=[R.EDGE_DECAYED, R.PRICE_AT_FAIR_VALUE],
                      detail={"remaining_edge": rem, "exit_threshold": config.exit_edge_threshold}, **common)

    return Signal(signal_type=HOLD, reason_codes=[R.EDGE_INTACT],
                  detail={"remaining_edge": rem}, **common)
