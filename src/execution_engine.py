"""The main loop that ties every module together.

Per cycle, for each market:
  fetch -> snapshot -> parse -> forecast -> probability -> EV -> signal
  -> (entry: risk check -> size -> trade) | (exit: close) -> mark-to-market -> log

Paper by default; live only if config.is_live_trading_armed(). The cycle returns
a structured summary used by the CLI and the dashboard.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import signal_engine as S
from .calibration_trainer import load_calibrators, load_ngr
from .database import Database
from .ev_engine import evaluate_entry
from .market_client import build_market_client
from .market_parser import parse_market
from .paper_trader import PaperTrader
from .position_manager import PositionManager
from .probability_engine import estimate_probability
from .risk_manager import RiskManager
from .signal_engine import generate_entry_signal, generate_exit_signal
from .utils import get_logger, setup_logging, utcnow
from .weather_client import build_weather_provider

log = get_logger("execution_engine")


@dataclass
class CycleResult:
    mode: str
    data_source: str
    cash: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    drawdown: float
    open_positions: int
    halted: bool = False
    rows: List[dict] = field(default_factory=list)


class ExecutionEngine:
    def __init__(self, config):
        self.config = config
        setup_logging(config.log_level)
        self.db = Database(config.db_path)
        # Load any calibration learned from past settled outcomes (safe defaults if none).
        self.calibrator, self.bias_table = load_calibrators(self.db)
        self.ngr = load_ngr(self.db) if getattr(config, "use_ngr", False) else None
        self.market_client = build_market_client(config)
        self.weather = build_weather_provider(config)
        self.pm = PositionManager(config.starting_bankroll)
        self.risk = RiskManager(config)
        self.paper = PaperTrader(config, self.pm, self.db)
        self.live = None
        self.reconciler = None
        self._cached_mode = None
        self._pruned_locations = set()
        # Rebuild paper state from the trade log so positions persist across runs.
        self.pm.replay(self.db.query(
            "SELECT * FROM trades WHERE mode='paper' ORDER BY id", ()))
        if config.data_source == "live" or config.live_trading:
            try:
                from .kalshi_client import KalshiClient
                from .live_trader import LiveTrader
                client = self.market_client if getattr(self.market_client, "name", "") == "kalshi" else KalshiClient(config)
                self.live = LiveTrader(config, client, self.db)
                from .reconciler import Reconciler
                self.reconciler = Reconciler(config, client, self.pm, self.db)
            except Exception as exc:  # pragma: no cover
                log.warning("Live trader unavailable: %s", exc)

    @property
    def trading_mode(self) -> str:
        # Memoized per cycle: live.armed() runs the edge-proven gate (replays
        # the trade log), so we don't want to recompute it for every market.
        if self._cached_mode is None:
            self._cached_mode = "live" if (self.live and self.live.armed()) else "paper"
        return self._cached_mode

    # ------------------------------------------------------------------ #
    def run_cycle(self) -> CycleResult:
        self._cached_mode = None  # recompute trading mode once per cycle
        if self.config.emergency_stop_engaged():
            log.critical("EMERGENCY STOP engaged — skipping cycle, taking no action.")
            self.db.record_error("execution_engine", "emergency stop engaged; cycle skipped")
            return self._summary(halted=True, markets_by_ticker={})

        # Live mode: trust the exchange as source of truth before acting.
        if self.trading_mode == "live" and self.reconciler is not None:
            self.reconciler.sync_positions()

        # Skip cities where the model has historically lost to the market.
        self._pruned_locations = set()
        if self.config.skip_unprofitable_segments:
            try:
                from .performance import unprofitable_segments
                self._pruned_locations = unprofitable_segments(
                    self.db, self.config, min_n=self.config.segment_min_samples)
                if self._pruned_locations:
                    log.info("Pruned (no proven edge) cities: %s", self._pruned_locations)
            except Exception as exc:  # pragma: no cover
                log.warning("Segment pruning failed: %s", exc)

        try:
            markets = self.market_client.list_markets()
        except Exception as exc:
            log.error("Failed to fetch markets: %s", exc)
            self.db.record_error("market_client", str(exc))
            return self._summary(halted=False, markets_by_ticker={})

        markets_by_ticker = {m.ticker: m for m in markets}
        rows: List[dict] = []

        for market in markets:
            try:
                rows.append(self._process_market(market))
            except Exception as exc:  # never let one market kill the cycle
                log.exception("Error processing %s", market.ticker)
                self.db.record_error("process_market", f"{market.ticker}: {exc}")

        return self._summary(halted=False, markets_by_ticker=markets_by_ticker, rows=rows)

    def _process_market(self, market) -> dict:
        cfg = self.config
        self.db.record_market_snapshot(market, self.market_client.name)
        parsed = parse_market(market)

        # Forecast + probability (needed for entry and to value exits).
        prob_est = None
        dist = None
        if parsed.location and parsed.target_date and parsed.threshold is not None:
            dist = self.weather.get_forecast(parsed)
            if dist is not None:
                self.db.record_weather_snapshot(market.ticker, dist)
                prob_est = estimate_probability(parsed, dist, cfg, calibrator=self.calibrator,
                                                bias_table=self.bias_table, ngr=self.ngr)
                if prob_est is not None:
                    self.db.record_probability(market.ticker, prob_est)

        ev = evaluate_entry(market, prob_est.model_probability_yes, cfg) if prob_est else None

        from .position_manager import position_id
        held_sides = [s for s in ("yes", "no") if position_id(market.ticker, s) in self.pm.positions]

        row = {
            "ticker": market.ticker, "title": market.title,
            "yes_bid": market.yes_bid, "yes_ask": market.yes_ask,
            "volume": market.volume, "spread": market.yes_spread,
            "model_probability": prob_est.model_probability_yes if prob_est else None,
            "confidence": prob_est.confidence if prob_est else None,
            "edge_net": ev.best_edge_net if ev else None,
            "signal": None, "side": None, "reasons": [], "action": "none",
            "tradeable": parsed.tradeable, "parse_reason": parsed.reason,
        }

        if held_sides:
            # EXIT logic for each side we hold.
            for side in held_sides:
                pos = self.pm.positions[position_id(market.ticker, side)]
                sig = generate_exit_signal(pos, parsed, market, prob_est, cfg)
                self.db.record_signal(sig, self.trading_mode)
                row.update(signal=sig.signal_type, side=side, reasons=sig.reason_codes,
                           edge_net=sig.edge)
                if sig.signal_type == S.EXIT:
                    self.paper.close(market, pos)
                    row["action"] = "closed"
            return row

        # ENTRY logic.
        sig = generate_entry_signal(parsed, market, prob_est, ev, cfg,
                                    pruned_locations=self._pruned_locations)
        self.db.record_signal(sig, self.trading_mode)
        row.update(signal=sig.signal_type, side=sig.side, reasons=sig.reason_codes)

        if sig.signal_type in (S.BUY_YES, S.BUY_NO):
            _, equity = self.pm.mark_to_market(self._exit_price_fn({market.ticker: market}))
            bankroll = max(equity, self.pm.cash)
            decision = self.risk.evaluate_entry(sig, parsed, market, prob_est, ev, bankroll, self.pm)
            row["reasons"] = sig.reason_codes + decision.reason_codes
            if decision.approved:
                if self.trading_mode == "live" and self.live:
                    self.live.open(market, decision.side, decision.allowed_contracts, decision)
                    row["action"] = f"LIVE order submitted x{decision.allowed_contracts}"
                else:
                    trade = self.paper.open(
                        market, decision.side, decision.allowed_contracts,
                        price=decision.entry_price,
                        fee_rate=(cfg.maker_fee_rate if cfg.use_maker_orders else cfg.fee_rate),
                        maker=cfg.use_maker_orders)
                    row["action"] = (f"opened x{decision.allowed_contracts}" if trade
                                     else "maker order unfilled")
                row["contracts"] = decision.allowed_contracts
            else:
                row["action"] = "blocked"
        return row

    # ------------------------------------------------------------------ #
    def _exit_price_fn(self, markets_by_ticker: Dict[str, object]):
        def exit_price_for(pos):
            m = markets_by_ticker.get(pos.ticker)
            if not m:
                return None
            return m.yes_bid if pos.side == "yes" else m.no_bid
        return exit_price_for

    def _summary(self, *, halted: bool, markets_by_ticker: Dict[str, object],
                 rows: Optional[List[dict]] = None) -> CycleResult:
        unrealized, equity = self.pm.mark_to_market(self._exit_price_fn(markets_by_ticker))
        drawdown = self.pm.drawdown(equity)
        result = CycleResult(
            mode=self.trading_mode, data_source=self.config.data_source,
            cash=round(self.pm.cash, 2), equity=round(equity, 2),
            realized_pnl=round(self.pm.realized_pnl, 2), unrealized_pnl=round(unrealized, 2),
            drawdown=drawdown, open_positions=self.pm.open_count(),
            halted=halted, rows=rows or [],
        )
        self.db.record_pnl({
            "bankroll": result.equity, "realized_pnl": result.realized_pnl,
            "unrealized_pnl": result.unrealized_pnl, "drawdown": result.drawdown,
            "open_positions": result.open_positions, "mode": result.mode,
        })
        return result

    def run_loop(self, cycles: Optional[int] = None) -> None:
        n = 0
        log.info("Starting loop: mode=%s data_source=%s interval=%ss",
                 self.trading_mode, self.config.data_source, self.config.loop_interval_seconds)
        while cycles is None or n < cycles:
            if self.config.emergency_stop_engaged():
                log.critical("EMERGENCY STOP engaged — halting loop.")
                break
            result = self.run_cycle()
            log.info("Cycle %d: equity=%.2f realized=%.2f open=%d",
                     n + 1, result.equity, result.realized_pnl, result.open_positions)
            n += 1
            if cycles is not None and n >= cycles:
                break
            time.sleep(self.config.loop_interval_seconds)
