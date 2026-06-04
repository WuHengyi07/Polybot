"""Position + bankroll accounting (state only; no I/O, no price fetching).

Source of truth is the sequence of trades. ``replay`` reconstructs cash,
open positions, and realized PnL from the trades table so state survives across
process runs and is fully auditable.

Cash-flow convention (so cash = starting_bankroll + sum(cash_flow)):
  open  (buy) : cash_flow = -(contracts*price + fee)
  close (sell): cash_flow = +(contracts*price - fee)
  settle      : cash_flow = +(contracts*payout)        # payout is 1.0 or 0.0
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional

from .utils import get_logger

log = get_logger("position_manager")


def position_id(ticker: str, side: str) -> str:
    return f"{ticker}:{side}"


@dataclass
class Position:
    position_id: str
    ticker: str
    side: str            # "yes" | "no"
    contracts: int
    avg_price: float
    category: str = "weather"
    opened_ts: str = ""
    status: str = "open"
    realized_pnl: float = 0.0

    @property
    def cost_basis(self) -> float:
        return round(self.contracts * self.avg_price, 4)


class PositionManager:
    def __init__(self, starting_bankroll: float):
        self.starting_bankroll = starting_bankroll
        self.cash = starting_bankroll
        self.realized_pnl = 0.0
        self.peak_equity = starting_bankroll
        self.positions: Dict[str, Position] = {}
        self.realized_by_date: Dict[str, float] = {}

    # ---- accounting primitives -------------------------------------- #
    def apply_open(self, ticker: str, side: str, contracts: int, price: float,
                   fee: float, category: str, ts: str) -> dict:
        pid = position_id(ticker, side)
        cash_flow = -(contracts * price + fee)
        self.cash += cash_flow
        pos = self.positions.get(pid)
        if pos:  # average in
            total = pos.contracts + contracts
            pos.avg_price = round((pos.cost_basis + contracts * price) / total, 6)
            pos.contracts = total
        else:
            self.positions[pid] = Position(pid, ticker, side, contracts, price, category, ts)
        return self._trade(ticker, side, "open", price, contracts, fee, cash_flow, pid)

    def apply_close(self, ticker: str, side: str, contracts: int, price: float,
                    fee: float, ts: str) -> Optional[dict]:
        pid = position_id(ticker, side)
        pos = self.positions.get(pid)
        if not pos:
            return None
        contracts = min(contracts, pos.contracts)
        proceeds = contracts * price - fee
        cost = contracts * pos.avg_price
        realized = proceeds - cost
        self.cash += proceeds
        self.realized_pnl += realized
        pos.realized_pnl += realized
        self._book_daily(ts, realized)
        pos.contracts -= contracts
        if pos.contracts <= 0:
            pos.status = "closed"
            del self.positions[pid]
        return self._trade(ticker, side, "close", price, contracts, fee, proceeds, pid, realized)

    def apply_settle(self, ticker: str, side: str, outcome_yes: bool, ts: str) -> Optional[dict]:
        pid = position_id(ticker, side)
        pos = self.positions.get(pid)
        if not pos:
            return None
        won = (side == "yes" and outcome_yes) or (side == "no" and not outcome_yes)
        payout = 1.0 if won else 0.0
        proceeds = pos.contracts * payout
        realized = proceeds - pos.cost_basis
        self.cash += proceeds
        self.realized_pnl += realized
        pos.realized_pnl += realized
        self._book_daily(ts, realized)
        trade = self._trade(ticker, side, "settle", payout, pos.contracts, 0.0, proceeds, pid, realized)
        pos.status = "closed"
        del self.positions[pid]
        return trade

    # ---- exposures & equity ----------------------------------------- #
    def open_count(self) -> int:
        return len(self.positions)

    def exposure_total(self) -> float:
        return round(sum(p.cost_basis for p in self.positions.values()), 4)

    def exposure_market(self, ticker: str) -> float:
        return round(sum(p.cost_basis for p in self.positions.values() if p.ticker == ticker), 4)

    def exposure_category(self, category: str) -> float:
        return round(sum(p.cost_basis for p in self.positions.values() if p.category == category), 4)

    def realized_today(self, today: Optional[str] = None) -> float:
        today = today or date.today().isoformat()
        return round(self.realized_by_date.get(today, 0.0), 4)

    def mark_to_market(self, exit_price_for: Callable[[Position], float]):
        """Return (unrealized_pnl, equity) given a function mapping a Position to
        its current exit (bid) price."""
        unreal = 0.0
        holdings_value = 0.0
        for pos in self.positions.values():
            px = exit_price_for(pos)
            if px is None:
                px = pos.avg_price  # fall back to cost if no live price
            holdings_value += pos.contracts * px
            unreal += pos.contracts * (px - pos.avg_price)
        equity = self.cash + holdings_value
        self.peak_equity = max(self.peak_equity, equity)
        return round(unreal, 4), round(equity, 4)

    def drawdown(self, equity: float) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return round(max(0.0, (self.peak_equity - equity) / self.peak_equity), 4)

    # ---- reconstruction --------------------------------------------- #
    def replay(self, trades: List[dict]) -> None:
        """Rebuild state from an ordered list of trade rows (from the DB)."""
        self.cash = self.starting_bankroll
        self.realized_pnl = 0.0
        self.positions = {}
        self.realized_by_date = {}
        for t in trades:
            action = t["action"]
            ticker, side = t["ticker"], t["side"]
            contracts = int(t["contracts"])
            price = float(t["price"])
            fee = float(t.get("fee") or 0.0)
            ts = t.get("ts", "")
            if action == "open":
                self.apply_open(ticker, side, contracts, price, fee, "weather", ts)
            elif action == "close":
                self.apply_close(ticker, side, contracts, price, fee, ts)
            elif action == "settle":
                self.apply_settle(ticker, side, outcome_yes=(price >= 0.5), ts=ts)
        # peak is unknown historically; reset to current cash as a floor
        self.peak_equity = max(self.cash, self.starting_bankroll)

    # ---- helpers ---------------------------------------------------- #
    def _book_daily(self, ts: str, realized: float) -> None:
        day = (ts or date.today().isoformat())[:10]
        self.realized_by_date[day] = self.realized_by_date.get(day, 0.0) + realized

    @staticmethod
    def _trade(ticker, side, action, price, contracts, fee, cash_flow, pid, realized=0.0) -> dict:
        return {
            "ticker": ticker, "side": side, "action": action, "price": round(price, 4),
            "contracts": contracts, "fee": round(fee, 4), "cash_flow": round(cash_flow, 4),
            "position_id": pid, "realized": round(realized, 4),
        }
