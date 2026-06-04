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


class Service:
    def __init__(self, config):
        self.config = config
        self.engine = ExecutionEngine(config)
        self.notifier = Notifier(config)
        self._last_daily = None

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
            self.notifier.daily_summary(result)
        except Exception as exc:  # pragma: no cover
            log.warning("Daily tasks failed: %s", exc)
            self.notifier.alert(f"daily tasks failed: {exc}")

    def _settle(self) -> None:
        from .scorer import run_settlement_pass
        from .settlement import (KalshiSettlementSource, MockSettlementSource,
                                 mock_outcomes_from_weather)
        e = self.engine
        if self.config.data_source == "live":
            source = KalshiSettlementSource(self.config)
        else:
            source = MockSettlementSource(mock_outcomes_from_weather(e.market_client, e.weather))
        resolved = run_settlement_pass(e.db, e.pm, e.paper, source)
        log.info("Daily settlement: resolved %d position(s)", len(resolved))

    def _calibrate(self) -> None:
        from .calibration_trainer import load_calibrators, train_from_db
        e = self.engine
        summary = train_from_db(e.db)
        e.calibrator, e.bias_table = load_calibrators(e.db)  # hot-reload into the engine
        log.info("Daily calibration: %s", summary)
