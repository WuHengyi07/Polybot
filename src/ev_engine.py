"""Expected-value / edge calculations.

Edge is always measured against EXECUTABLE prices — you BUY at the ask and SELL
at the bid, never at the midpoint (the "mid-price illusion" is a top reason
paper edges evaporate live). Fees and a slippage buffer are subtracted so a
signal only fires on edge that survives real costs.

Kalshi taker fee per contract: FEE_RATE * price * (1 - price)  (dollars).
Each contract settles to $1, so for a long position bought at ``price`` the
maximum loss is ``price`` and the maximum gain is ``1 - price``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def fee_per_contract(price: float, fee_rate: float) -> float:
    """Kalshi-style fee: fee_rate * p * (1 - p), clamped to non-negative."""
    return max(0.0, fee_rate * price * (1.0 - price))


def maker_price(bid: float, ask: float, tick: float) -> float:
    """A resting MAKER price on the bid side: one tick better than the best bid if
    the spread allows, else join the bid. Always strictly below the ask, so it
    provides liquidity (lower fee) rather than crossing the spread (taker)."""
    improve = round(bid + tick, 2)
    return improve if improve < ask else round(bid, 2)


@dataclass
class EVResult:
    market_id: str
    ticker: str
    model_probability_yes: float
    yes_ask: float
    no_ask: float
    yes_bid: float
    no_bid: float
    yes_entry: float        # price we'd actually pay for YES (ask=taker, bid+tick=maker)
    no_entry: float
    is_maker: bool
    # gross = before costs; net = after fee + slippage buffer
    edge_yes_gross: float
    edge_no_gross: float
    fee_yes: float
    fee_no: float
    edge_yes_net: float
    edge_no_net: float
    best_side: Optional[str]      # "yes" | "no" | None
    best_edge_net: float
    ev_per_contract: float        # expected $ profit/contract on best_side

    @property
    def entry_price(self) -> Optional[float]:
        if self.best_side == "yes":
            return self.yes_entry
        if self.best_side == "no":
            return self.no_entry
        return None


def evaluate_entry(market, model_probability_yes: float, config) -> EVResult:
    """Edge for entering YES/NO after fees + slippage.

    Taker: pay the ask, full fee, plus a slippage buffer.
    Maker (USE_MAKER_ORDERS): rest on the bid side at the ~4x-cheaper maker fee and
    no slippage buffer (you set your price) — which materially raises net edge.
    """
    p = model_probability_yes
    is_maker = bool(getattr(config, "use_maker_orders", False))
    if is_maker:
        rate, slip = config.maker_fee_rate, 0.0
        yes_entry = maker_price(market.yes_bid, market.yes_ask, config.maker_tick)
        no_entry = maker_price(market.no_bid, market.no_ask, config.maker_tick)
    else:
        rate, slip = config.fee_rate, config.slippage_buffer
        yes_entry, no_entry = market.yes_ask, market.no_ask

    fee_yes = fee_per_contract(yes_entry, rate)
    fee_no = fee_per_contract(no_entry, rate)

    edge_yes_gross = p - yes_entry
    edge_no_gross = (1.0 - p) - no_entry
    edge_yes_net = edge_yes_gross - fee_yes - slip
    edge_no_net = edge_no_gross - fee_no - slip

    if edge_yes_net >= edge_no_net and edge_yes_net > 0:
        best_side, best_edge = "yes", edge_yes_net
    elif edge_no_net > 0:
        best_side, best_edge = "no", edge_no_net
    else:
        best_side, best_edge = None, max(edge_yes_net, edge_no_net)

    return EVResult(
        market_id=market.market_id,
        ticker=market.ticker,
        model_probability_yes=round(p, 4),
        yes_ask=market.yes_ask,
        no_ask=market.no_ask,
        yes_bid=market.yes_bid,
        no_bid=market.no_bid,
        yes_entry=round(yes_entry, 4),
        no_entry=round(no_entry, 4),
        is_maker=is_maker,
        edge_yes_gross=round(edge_yes_gross, 4),
        edge_no_gross=round(edge_no_gross, 4),
        fee_yes=round(fee_yes, 4),
        fee_no=round(fee_no, 4),
        edge_yes_net=round(edge_yes_net, 4),
        edge_no_net=round(edge_no_net, 4),
        best_side=best_side,
        best_edge_net=round(best_edge, 4),
        ev_per_contract=round(best_edge, 4),
    )


def remaining_edge(side: str, market, model_probability_yes: float) -> float:
    """Edge still left in an OPEN position, valued at the exit (bid) price.

    YES: p - yes_bid     NO: (1 - p) - no_bid
    """
    p = model_probability_yes
    if side == "yes":
        return round(p - market.yes_bid, 4)
    if side == "no":
        return round((1.0 - p) - market.no_bid, 4)
    return 0.0
