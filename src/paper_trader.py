"""Paper trading engine — simulates realistic fills against the order book.

Fills use EXECUTABLE prices:
  buy YES @ yes_ask   sell YES @ yes_bid
  buy NO  @ no_ask    sell NO  @ no_bid
Kalshi-style taker fees are charged on every fill. All accounting flows through
PositionManager; everything is written to the database.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from .ev_engine import fee_per_contract
from .position_manager import Position, PositionManager
from .utils import get_logger, utcnow

log = get_logger("paper_trader")


class PaperTrader:
    mode = "paper"

    def __init__(self, config, pm: PositionManager, db):
        self.config = config
        self.pm = pm
        self.db = db

    def _fee(self, price: float, contracts: int) -> float:
        return round(fee_per_contract(price, self.config.fee_rate) * contracts, 4)

    def _maker_filled(self, ticker: str) -> bool:
        """Deterministic per-ticker fill decision (reproducible) — maker orders
        don't always get hit, so we model a fill probability honestly."""
        h = int(hashlib.md5(ticker.encode()).hexdigest(), 16) % 1000 / 1000.0
        return h < self.config.maker_fill_prob

    def open(self, market, side: str, contracts: int, price: Optional[float] = None,
             fee_rate: Optional[float] = None, maker: bool = False) -> Optional[dict]:
        if price is None:
            price = market.yes_ask if side == "yes" else market.no_ask
        if price <= 0 or contracts < 1:
            return None
        if maker and not self._maker_filled(market.ticker):
            log.info("PAPER maker order NOT filled: %s %s x%d @ %.2f", side.upper(),
                     market.ticker, contracts, price)
            return None
        rate = fee_rate if fee_rate is not None else self.config.fee_rate
        fee = round(fee_per_contract(price, rate) * contracts, 4)
        ts = utcnow().isoformat()
        trade = self.pm.apply_open(market.ticker, side, contracts, price, fee, market.category, ts)
        self._persist_trade(trade)
        self._sync_position(market.ticker, side)
        log.info("PAPER OPEN %s %s x%d @ %.2f (fee %.3f%s)", side.upper(), market.ticker,
                 contracts, price, fee, " maker" if maker else "")
        return trade

    def close(self, market, position: Position) -> Optional[dict]:
        side = position.side
        price = market.yes_bid if side == "yes" else market.no_bid
        contracts = position.contracts
        fee = self._fee(price, contracts)
        ts = utcnow().isoformat()
        trade = self.pm.apply_close(market.ticker, side, contracts, price, fee, ts)
        if trade is None:
            return None
        self._persist_trade(trade)
        self._sync_position(market.ticker, side)
        log.info("PAPER CLOSE %s %s x%d @ %.2f -> realized %.3f",
                 side.upper(), market.ticker, contracts, price, trade["realized"])
        return trade

    def settle(self, ticker: str, side: str, outcome_yes: bool) -> Optional[dict]:
        ts = utcnow().isoformat()
        trade = self.pm.apply_settle(ticker, side, outcome_yes, ts)
        if trade:
            self._persist_trade(trade)
            self._sync_position(ticker, side)
        return trade

    # ---- persistence ------------------------------------------------- #
    def _persist_trade(self, trade: dict) -> None:
        row = {k: trade[k] for k in ("ticker", "side", "action", "price", "contracts", "fee", "cash_flow", "position_id")}
        row["mode"] = self.mode
        self.db.record_trade(row)

    def _sync_position(self, ticker: str, side: str) -> None:
        from .position_manager import position_id
        pid = position_id(ticker, side)
        pos = self.pm.positions.get(pid)
        if pos:
            self.db.upsert_position({
                "position_id": pos.position_id, "ticker": pos.ticker, "side": pos.side,
                "contracts": pos.contracts, "avg_price": pos.avg_price, "status": "open",
                "opened_ts": pos.opened_ts, "closed_ts": "", "realized_pnl": pos.realized_pnl,
                "mode": self.mode,
            })
        else:
            # mark any stored row closed
            self.db.conn.execute(
                "UPDATE positions SET status='closed', closed_ts=? WHERE position_id=? AND mode=?",
                (utcnow().isoformat(), pid, self.mode))
            self.db.conn.commit()
