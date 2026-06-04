"""Reconcile local state against the exchange — required for live trading.

The paper engine assumes instant, full fills. Live fills are asynchronous and
partial, so before/after acting live we must trust the EXCHANGE as the source of
truth for positions and cash. This reconciler pulls Kalshi's portfolio and
rewrites the PositionManager to match, and records real fills.

Live-only and best-effort: it never fabricates state, and on any error it logs
and leaves local state untouched (the risk manager will then size conservatively).
"""
from __future__ import annotations

from typing import Optional

from .position_manager import Position, position_id
from .utils import get_logger, to_float, utcnow

log = get_logger("reconciler")


class Reconciler:
    def __init__(self, config, kalshi_client, pm, db):
        self.config = config
        self.client = kalshi_client
        self.pm = pm
        self.db = db

    def sync_balance(self) -> Optional[float]:  # pragma: no cover - needs live creds
        """Return the real account cash balance (dollars), or None on failure."""
        try:
            data = self.client.get_balance()
            cents = to_float(data.get("balance", data.get("balance_cents")))
            return round(cents / 100.0, 2) if cents else None
        except Exception as exc:
            log.warning("Balance sync failed: %s", exc)
            return None

    def sync_positions(self) -> bool:  # pragma: no cover - needs live creds
        """Rewrite PositionManager.positions from the exchange. True if synced."""
        try:
            data = self.client.get_positions()
        except Exception as exc:
            log.warning("Position sync failed (leaving local state): %s", exc)
            return False

        rows = data.get("market_positions", data.get("positions", []))
        new_positions = {}
        for r in rows:
            ticker = r.get("ticker", "")
            net = int(to_float(r.get("position")))  # +ve = long YES, -ve = long NO
            if net == 0 or not ticker:
                continue
            side = "yes" if net > 0 else "no"
            contracts = abs(net)
            # average price from cost basis if available, else mark unknown (0)
            exposure = to_float(r.get("market_exposure", r.get("total_traded")))
            avg_price = round((exposure / 100.0) / contracts, 4) if exposure and contracts else 0.0
            pid = position_id(ticker, side)
            new_positions[pid] = Position(pid, ticker, side, contracts, avg_price, "weather",
                                          utcnow().isoformat())
        self.pm.positions = new_positions
        balance = self.sync_balance()
        if balance is not None:
            self.pm.cash = balance
        log.info("Reconciled %d live position(s) from exchange.", len(new_positions))
        return True

    def record_recent_fills(self) -> int:  # pragma: no cover - needs live creds
        """Persist real fills to the DB for audit. Returns count recorded."""
        try:
            data = self.client.get_fills()
        except Exception as exc:
            log.warning("Fill sync failed: %s", exc)
            return 0
        fills = data.get("fills", [])
        for f in fills:
            self.db.record_trade({
                "ticker": f.get("ticker", ""), "side": f.get("side", ""),
                "action": "fill", "price": round(to_float(f.get("yes_price", f.get("price"))) / 100.0, 4),
                "contracts": int(to_float(f.get("count"))), "fee": 0.0,
                "cash_flow": 0.0, "mode": "live", "position_id": position_id(f.get("ticker", ""), f.get("side", "")),
            })
        return len(fills)
