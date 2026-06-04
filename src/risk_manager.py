"""Risk manager — the final authority before any order (paper or live).

It re-checks market quality (defense in depth), enforces all portfolio limits,
and sizes the position. Every rejection carries a reason code. If anything is
uncertain, it fails closed (rejects).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

from .signal_engine import BUY_NO, BUY_YES
from .utils import clamp, get_logger

log = get_logger("risk_manager")


def kelly_position_size(edge_net: float, entry_price: float, bankroll: float,
                        kelly_fraction: float, confidence: float, *,
                        confidence_scaled: bool = True, fraction_cap: float = 0.5) -> int:
    """Fractional-Kelly contract count for a binary bought at ``entry_price``.

    Full Kelly for a 0/1 contract is edge / (1 - price). We bet a FRACTION of that
    (``kelly_fraction``, e.g. 0.25), optionally scaled down by forecast ``confidence``
    so marginal calls bet less, and clamp the fraction at ``fraction_cap`` so a
    mis-set kelly_fraction can't blow up the bet. Returns 0 when there is no edge.
    """
    if edge_net <= 0 or entry_price <= 0:
        return 0
    kelly_f = clamp(edge_net / max(1e-6, 1.0 - entry_price))  # full-Kelly fraction (0..1)
    frac = kelly_fraction * (clamp(confidence) if confidence_scaled else 1.0)
    frac = min(frac, fraction_cap)
    return math.floor(frac * kelly_f * bankroll / entry_price)


class RR:
    KILL_SWITCH = "KILL_SWITCH"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    MARKET_EXPOSURE_CAP = "MARKET_EXPOSURE_CAP"
    CATEGORY_EXPOSURE_CAP = "CATEGORY_EXPOSURE_CAP"
    TOTAL_EXPOSURE_CAP = "TOTAL_EXPOSURE_CAP"
    MIN_LIQUIDITY = "MIN_LIQUIDITY"
    MAX_SPREAD = "MAX_SPREAD"
    MIN_CONFIDENCE = "MIN_CONFIDENCE"
    SETTLEMENT_RISK = "SETTLEMENT_RISK"
    NEAR_SETTLEMENT = "NEAR_SETTLEMENT"
    PRICE_OUT_OF_RANGE = "PRICE_OUT_OF_RANGE"
    INSUFFICIENT_SIZE = "INSUFFICIENT_SIZE"
    APPROVED = "APPROVED"
    NOT_A_BUY = "NOT_A_BUY"


@dataclass
class RiskDecision:
    approved: bool
    allowed_contracts: int = 0
    side: Optional[str] = None
    entry_price: Optional[float] = None
    reason_codes: List[str] = field(default_factory=list)
    binding_constraint: str = ""
    detail: dict = field(default_factory=dict)


class RiskManager:
    def __init__(self, config):
        self.config = config

    def evaluate_entry(self, signal, parsed, market, prob_est, ev_result,
                       bankroll: float, pm) -> RiskDecision:
        c = self.config
        if signal.signal_type not in (BUY_YES, BUY_NO):
            return RiskDecision(False, reason_codes=[RR.NOT_A_BUY])

        side = ev_result.best_side
        entry_price = ev_result.entry_price
        if not side or not entry_price or entry_price <= 0:
            return RiskDecision(False, reason_codes=[RR.INSUFFICIENT_SIZE],
                                detail={"reason": "no executable entry price"})
        # Reject cheap-lottery / near-certain prices (defense in depth).
        if entry_price < c.min_price or entry_price > c.max_price:
            return RiskDecision(False, side=side, entry_price=entry_price,
                                reason_codes=[RR.PRICE_OUT_OF_RANGE],
                                detail={"entry_price": entry_price,
                                        "min": c.min_price, "max": c.max_price})

        # --- hard stops -------------------------------------------------
        if c.emergency_stop_engaged():
            return RiskDecision(False, side=side, reason_codes=[RR.KILL_SWITCH])

        base = c.starting_bankroll
        if pm.realized_today() <= -abs(c.max_daily_loss) * base:
            return RiskDecision(False, side=side, reason_codes=[RR.DAILY_LOSS_LIMIT],
                                detail={"realized_today": pm.realized_today(),
                                        "limit": -abs(c.max_daily_loss) * base})

        # drawdown uses the equity passed in as `bankroll`
        dd = pm.drawdown(bankroll)
        if dd >= c.max_drawdown:
            return RiskDecision(False, side=side, reason_codes=[RR.MAX_DRAWDOWN],
                                detail={"drawdown": dd, "max": c.max_drawdown})

        # --- market-quality re-checks (defense in depth) ---------------
        if not parsed.tradeable:
            return RiskDecision(False, side=side, reason_codes=[RR.SETTLEMENT_RISK],
                                detail={"reason": parsed.reason})
        if market.yes_spread > c.max_spread:
            return RiskDecision(False, side=side, reason_codes=[RR.MAX_SPREAD],
                                detail={"spread": market.yes_spread})
        if market.volume < c.min_volume:
            return RiskDecision(False, side=side, reason_codes=[RR.MIN_LIQUIDITY],
                                detail={"volume": market.volume})
        if prob_est.confidence < c.min_model_confidence:
            return RiskDecision(False, side=side, reason_codes=[RR.MIN_CONFIDENCE],
                                detail={"confidence": prob_est.confidence})

        # --- position count --------------------------------------------
        from .position_manager import position_id
        pid = position_id(market.ticker, side)
        already_open = pid in pm.positions
        if not already_open and pm.open_count() >= c.max_open_positions:
            return RiskDecision(False, side=side, reason_codes=[RR.MAX_OPEN_POSITIONS],
                                detail={"open": pm.open_count(), "max": c.max_open_positions})

        # --- sizing -----------------------------------------------------
        risk_dollars = c.max_risk_per_trade * bankroll
        contracts = math.floor(risk_dollars / entry_price)
        binding = "max_risk_per_trade"

        if c.use_kelly_sizing:
            # fractional Kelly (confidence-scaled) for a binary at `entry_price`;
            # only ever SHRINKS vs the max_risk_per_trade ceiling above.
            kelly_contracts = kelly_position_size(
                ev_result.best_edge_net, entry_price, bankroll, c.kelly_fraction,
                prob_est.confidence,
                confidence_scaled=getattr(c, "kelly_confidence_scaled", True),
                fraction_cap=getattr(c, "kelly_fraction_cap", 0.5))
            if kelly_contracts < contracts:
                contracts, binding = kelly_contracts, "fractional_kelly"

        caps = {
            "max_market_exposure": (c.max_market_exposure * bankroll - pm.exposure_market(market.ticker)),
            "max_category_exposure": (c.max_category_exposure * bankroll - pm.exposure_category(market.category)),
            "max_total_exposure": (c.max_total_exposure * bankroll - pm.exposure_total()),
        }
        cap_codes = {
            "max_market_exposure": RR.MARKET_EXPOSURE_CAP,
            "max_category_exposure": RR.CATEGORY_EXPOSURE_CAP,
            "max_total_exposure": RR.TOTAL_EXPOSURE_CAP,
        }
        for name, dollars in caps.items():
            allowed = math.floor(max(0.0, dollars) / entry_price)
            if allowed < contracts:
                contracts, binding = allowed, name

        # Liquidity realism: never take more than 10% of stated volume, and never
        # more than the resting ask depth if we have an order book.
        vol_cap = max(1, int(0.10 * market.volume))
        if vol_cap < contracts:
            contracts, binding = vol_cap, "liquidity_volume_cap"
        if market.order_book is not None:
            # Real resting depth caps the fill — including to 0 (a quote with no size behind
            # it can't be traded), which then trips the INSUFFICIENT_SIZE guard below.
            depth = market.order_book.depth_at_or_better(side, entry_price)
            if depth < contracts:
                contracts, binding = depth, "order_book_depth"

        if contracts < 1:
            return RiskDecision(False, side=side, entry_price=entry_price,
                                reason_codes=[RR.INSUFFICIENT_SIZE], binding_constraint=binding,
                                detail={"risk_dollars": round(risk_dollars, 2), "binding": binding})

        return RiskDecision(
            approved=True, allowed_contracts=int(contracts), side=side, entry_price=entry_price,
            reason_codes=[RR.APPROVED], binding_constraint=binding,
            detail={
                "risk_dollars": round(risk_dollars, 2),
                "stake": round(contracts * entry_price, 2),
                "binding_constraint": binding,
                "exposure_after": round(pm.exposure_total() + contracts * entry_price, 2),
            },
        )
