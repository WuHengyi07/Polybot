"""Unattended service: a crash-recovering scheduler around the execution engine.

Cadence:
  * every loop_interval_seconds → one analysis/trade cycle
  * once per (UTC) day → settle matured positions + retrain calibration + post a summary

A crashing cycle is logged, alerted, and recovered from (the loop continues) so
the bot survives transient data/API failures unattended. Pairs with a process
supervisor (Windows Task Scheduler / systemd / Docker `restart: always`) for
restart-on-exit; see the deploy/ files.
"""
from __future__ import annotations

import time
from typing import Optional

from .execution_engine import ExecutionEngine
from .notify import Notifier
from .utils import get_logger, utcnow

log = get_logger("service")


def build_settlement_source(config, market_client, weather):
    """Pick the settlement source by data source. CRITICAL: a real-data source
    (live=Kalshi, polymarket) must use its REAL outcome source; ONLY the offline `mock`
    source may settle against the model's own forecast. Mirrors scorer.settle_cli."""
    from .settlement import (KalshiSettlementSource, MockSettlementSource,
                             PolymarketSettlementSource, mock_outcomes_from_weather)
    if config.data_source == "live":
        return KalshiSettlementSource(config)
    if config.data_source == "polymarket":
        return PolymarketSettlementSource(config)
    return MockSettlementSource(mock_outcomes_from_weather(market_client, weather))


class Service:
    def __init__(self, config):
        self.config = config
        self.engine = ExecutionEngine(config)
        self.notifier = Notifier(config)
        self._last_daily = None
        # Start the fill watermark at the current max trade id so the service never
        # re-posts historical fills when it (re)starts.
        self._last_trade_id = self.engine.db.max_trade_id()

    def run_forever(self, max_cycles: Optional[int] = None) -> None:
        log.info("Service starting: data_source=%s interval=%ss (max_cycles=%s)",
                 self.config.data_source, self.config.loop_interval_seconds, max_cycles)
        n = 0
        while max_cycles is None or n < max_cycles:
            if self.config.emergency_stop_engaged():
                self.notifier.alert("emergency stop engaged; service halting")
                break
            try:
                result = self.engine.run_cycle()
                self.notifier.heartbeat(result)
                self._post_new_fills()
                self._maybe_daily_tasks(result)
            except Exception as exc:  # crash recovery — never let one cycle kill the service
                log.exception("Cycle crashed; recovering")
                try:
                    self.engine.db.record_error("service", str(exc))
                except Exception:  # pragma: no cover
                    pass
                self.notifier.alert(f"cycle error (recovering): {exc}")
            n += 1
            if (max_cycles is None or n < max_cycles) and self.config.loop_interval_seconds > 0:
                time.sleep(self.config.loop_interval_seconds)

    def _maybe_daily_tasks(self, result) -> None:
        today = utcnow().date()
        if self._last_daily == today:
            return
        self._last_daily = today
        try:
            self._settle()
            self._calibrate()
            # Summarize AFTER settling so equity/PnL reflect the post-settle state (the
            # cycle `result` is pre-settle and was reporting stale, inconsistent equity).
            fresh = self.engine._summary(halted=False, markets_by_ticker={})
            from .live_gate import edge_proven
            from .performance import compute_closed_stats
            closed = compute_closed_stats(self.engine.db, self.config)
            gate = edge_proven(self.engine.db, self.config)
            self.notifier.daily_summary(fresh, closed=closed, gate=gate)
        except Exception as exc:  # pragma: no cover
            log.warning("Daily tasks failed: %s", exc)
            self.notifier.alert(f"daily tasks failed: {exc}")

    def _post_new_fills(self) -> None:
        """Post any opens recorded since the last watermark, as one batched message."""
        db = self.engine.db
        new = db.trades_since(self._last_trade_id, action="open")
        if not new:
            return
        fills = []
        for t in new:
            sig = db.latest_signal(t["ticker"])
            edge = float(sig["edge"]) if sig and sig.get("edge") is not None else None
            fills.append((t, edge))
        self.notifier.trade_fills(fills)
        self._last_trade_id = db.max_trade_id()

    def _settle(self) -> None:
        from .performance import compute_performance
        from .scorer import run_settlement_pass
        e = self.engine
        before = compute_performance(e.db, self.config).realized_pnl
        source = build_settlement_source(self.config, e.market_client, e.weather)
        resolved = run_settlement_pass(e.db, e.pm, e.paper, source)
        log.info("Daily settlement: resolved %d position(s)", len(resolved))
        if not resolved:
            return
        after = compute_performance(e.db, self.config).realized_pnl
        by_ticker = {s["ticker"]: s for s in e.db.get_settlements()}
        items = []
        for ticker, side, _outcome in resolved:
            row = dict(by_ticker.get(ticker, {"ticker": ticker}))
            row["side"] = side
            items.append(row)
        self.notifier.settlement_results(items, realized_delta=round(after - before, 2))

    def _calibrate(self) -> None:
        from .calibration_trainer import load_calibrators, train_from_db
        e = self.engine
        summary = train_from_db(e.db)
        e.calibrator, e.bias_table = load_calibrators(e.db)  # hot-reload into the engine
        log.info("Daily calibration: %s", summary)
