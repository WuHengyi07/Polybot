"""Guarded live trading.

NOTHING here places a real order unless config.is_live_trading_armed() is true,
which requires ALL of: PAPER_TRADING=false, LIVE_TRADING=true, AUTO_TRADE=true,
no emergency stop, and Kalshi API credentials present. Even then:
  * limit orders only (unless USE_LIMIT_ORDERS_ONLY=false)
  * optional y/N confirmation per order (CONFIRM_LIVE_TRADES)
  * idempotent client_order_id so a retry cannot duplicate a position
  * every order logged before AND after submission
  * NO aggressive retrying (a failed submit stops; it is not resent)
"""
from __future__ import annotations

import hashlib
from typing import Optional

from .live_gate import edge_proven
from .utils import get_logger, utcnow

log = get_logger("live_trader")


class EmergencyStop(Exception):
    pass


class LiveTrader:
    mode = "live"

    def __init__(self, config, client, db):
        self.config = config
        self.client = client
        self.db = db

    def armed(self) -> bool:
        """Live orders allowed ONLY if the config flags/keys are set AND the
        edge-proven gate passes on the paper track record."""
        if not self.config.is_live_trading_armed():
            return False
        ok, reason = edge_proven(self.db, self.config)
        if not ok:
            log.warning("LIVE blocked by edge-proven gate: %s", reason)
        return ok

    def _client_order_id(self, ticker: str, side: str, bucket: str) -> str:
        """Deterministic id so an accidental retry maps to the SAME order."""
        raw = f"{ticker}|{side}|{bucket}"
        return "pmb-" + hashlib.sha1(raw.encode()).hexdigest()[:24]

    def open(self, market, side: str, contracts: int, decision=None) -> Optional[dict]:
        # 1) Hard gates ---------------------------------------------------
        if not self.armed():
            log.warning("LIVE refused: live trading is not armed (staying paper). %s",
                        self.config.as_public_dict().get("data_source"))
            return None
        if self.config.emergency_stop_engaged():
            log.error("LIVE refused: EMERGENCY STOP engaged.")
            return None
        if contracts < 1:
            return None

        # 2) Build a LIMIT order at the executable price (maker price if provided)
        price = (decision.entry_price if decision is not None and getattr(decision, "entry_price", None)
                 else (market.yes_ask if side == "yes" else market.no_ask))
        price_cents = int(round(price * 100))
        order_type = "limit" if self.config.use_limit_orders_only else "market"
        bucket = utcnow().strftime("%Y%m%d%H%M")  # minute bucket -> idempotency window
        client_order_id = self._client_order_id(market.ticker, side, bucket)
        order = {
            "ticker": market.ticker, "action": "buy", "side": side, "count": int(contracts),
            "type": order_type, "client_order_id": client_order_id, "time_in_force": "ioc",
        }
        if order_type == "limit":
            order["yes_price" if side == "yes" else "no_price"] = price_cents

        # 3) Log BEFORE submit -------------------------------------------
        self.db.record_order({
            "ticker": market.ticker, "side": side, "action": "buy", "price": price,
            "contracts": contracts, "order_type": order_type, "status": "pending",
            "client_order_id": client_order_id, "mode": self.mode, "detail_json": order,
        })
        log.info("LIVE ORDER (pending) %s", order)

        # 4) Optional human confirmation ---------------------------------
        if self.config.confirm_live_trades:
            print("\n=== CONFIRM LIVE ORDER ===")
            print(f"  {order_type.upper()} BUY {side.upper()} {contracts} x {market.ticker} @ {price:.2f}")
            print(f"  stake ≈ ${contracts * price:.2f}  |  client_order_id={client_order_id}")
            try:
                answer = input("  Type 'YES' to submit, anything else to abort: ").strip()
            except EOFError:
                answer = ""
            if answer != "YES":
                self.db.record_order({
                    "ticker": market.ticker, "side": side, "action": "buy", "price": price,
                    "contracts": contracts, "order_type": order_type, "status": "aborted_by_user",
                    "client_order_id": client_order_id, "mode": self.mode, "detail_json": order,
                })
                log.info("LIVE order aborted by user: %s", client_order_id)
                return None

        # 5) Submit ONCE (no aggressive retry) ---------------------------
        try:
            resp = self.client.place_order(order)
        except Exception as exc:  # do NOT retry — a retry could duplicate
            self.db.record_order({
                "ticker": market.ticker, "side": side, "action": "buy", "price": price,
                "contracts": contracts, "order_type": order_type, "status": "submit_failed",
                "client_order_id": client_order_id, "mode": self.mode, "detail_json": {"error": str(exc)},
            })
            log.error("LIVE submit failed (NOT retrying): %s", exc)
            return None

        # 6) Log AFTER submit --------------------------------------------
        self.db.record_order({
            "ticker": market.ticker, "side": side, "action": "buy", "price": price,
            "contracts": contracts, "order_type": order_type, "status": "submitted",
            "client_order_id": client_order_id, "mode": self.mode, "detail_json": resp,
        })
        log.info("LIVE order submitted: %s", resp)
        return resp

    def emergency_stop(self) -> None:
        """Engage the kill switch by creating the STOP file."""
        try:
            with open(self.config.emergency_stop_file, "w", encoding="utf-8") as f:
                f.write(f"engaged {utcnow().isoformat()}\n")
            log.critical("EMERGENCY STOP engaged via file %s", self.config.emergency_stop_file)
        except OSError as exc:  # pragma: no cover
            log.error("Failed to write emergency stop file: %s", exc)
