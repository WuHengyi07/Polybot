"""prediction_market_bot — command-line entry point.

Examples:
  python main.py once                      # one paper cycle on mock data
  python main.py once --data-source live   # one paper cycle on REAL Kalshi+Open-Meteo data
  python main.py run --cycles 5            # loop 5 cycles
  python main.py backtest                  # metrics over recorded/mock snapshots
  python main.py status                    # show mode, safety flags, open positions

Live trading stays OFF unless PAPER_TRADING=false, LIVE_TRADING=true,
AUTO_TRADE=true and Kalshi credentials are set in .env. --mode/--data-source
never arm live trading on their own.
"""
from __future__ import annotations

import argparse
import os
import sys

# Allow running as `python main.py` from the project directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Make stdout UTF-8 where possible so output is consistent across platforms
# (Windows consoles default to cp1252 and choke on non-ASCII).
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from config import Config  # noqa: E402


def _apply_overrides(args) -> None:
    if args.data_source:
        os.environ["DATA_SOURCE"] = args.data_source
    # --mode paper forces paper only for TRADING commands (read-only commands like
    # `status` should reflect the real .env). --mode live is a no-op for safety:
    # arming still requires .env flags (PAPER_TRADING=false, LIVE_TRADING=true,
    # AUTO_TRADE=true, keys) AND the edge-proven gate.
    if args.mode == "paper" and args.command in {"once", "run", "service"}:
        os.environ["PAPER_TRADING"] = "true"
        os.environ["LIVE_TRADING"] = "false"


def _print_summary(res) -> None:
    print("\n" + "=" * 100)
    banner = "HALTED (emergency stop)" if res.halted else "CYCLE COMPLETE"
    print(f"  {banner}  |  trading mode: {res.mode.upper()}  |  data source: {res.data_source}")
    print("=" * 100)
    if res.rows:
        print(f"{'TICKER':<26}{'P(yes)':>7}{'bid/ask':>12}{'edge':>7}  {'SIGNAL':<13}{'ACTION':<16}REASONS")
        print("-" * 100)
        for r in res.rows:
            p = f"{r['model_probability']:.2f}" if r.get("model_probability") is not None else "  -"
            ba = f"{r['yes_bid']:.2f}/{r['yes_ask']:.2f}"
            edge = f"{r['edge_net']:+.2f}" if r.get("edge_net") is not None else "   -"
            sig = r.get("signal") or "-"
            action = r.get("action") or "none"
            reasons = ",".join(r.get("reasons") or [])[:34]
            print(f"{r['ticker']:<26}{p:>7}{ba:>12}{edge:>7}  {sig:<13}{action:<16}{reasons}")
    print("-" * 100)
    print(f"  cash={res.cash:>8.2f}  equity={res.equity:>8.2f}  "
          f"realized={res.realized_pnl:>7.2f}  unrealized={res.unrealized_pnl:>7.2f}  "
          f"drawdown={res.drawdown*100:>4.1f}%  open={res.open_positions}")
    print("=" * 100 + "\n")


def cmd_once(config) -> int:
    from src.execution_engine import ExecutionEngine
    engine = ExecutionEngine(config)
    _print_summary(engine.run_cycle())
    return 0


def cmd_run(config, cycles) -> int:
    from src.execution_engine import ExecutionEngine
    engine = ExecutionEngine(config)
    try:
        engine.run_loop(cycles=cycles)
    except KeyboardInterrupt:
        print("\nInterrupted — exiting cleanly.")
    return 0


def cmd_backtest(config) -> int:
    from src.backtester import run_backtest, format_report
    report = run_backtest(config)
    print(format_report(report))
    return 0


def cmd_histbacktest(config, past_days, walk_forward) -> int:
    from src.historical_backtester import format_report, run_historical_backtest
    print(format_report(run_historical_backtest(config, past_days=past_days or 90,
                                                walk_forward=walk_forward)))
    return 0


def cmd_marketbacktest(config, lookback_days, lead_hours) -> int:
    from src.market_backtester import format_report, run_market_backtest
    print(format_report(run_market_backtest(config, lookback_days=lookback_days or 60,
                                            lead_hours=lead_hours or 24)))
    return 0


def cmd_settle(config) -> int:
    from src.scorer import settle_cli
    resolved, pm = settle_cli(config)
    print(f"\nSettled {len(resolved)} position(s).")
    for ticker, side, outcome in resolved:
        print(f"  {ticker:<28} {side:<4} -> {'YES' if outcome else 'NO'}")
    print(f"  realized PnL now: ${pm.realized_pnl:+.2f}   cash: ${pm.cash:.2f}\n")
    return 0


def cmd_report(config) -> int:
    from src.database import Database
    from src.performance import (compute_performance, format_report, format_segments,
                                 performance_by_segment)
    db = Database(config.db_path)
    print(format_report(compute_performance(db, config, mode="paper")))
    print(format_segments(performance_by_segment(db, config, mode="paper")))
    return 0


def cmd_calibrate(config) -> int:
    from src.calibration_trainer import train_from_db
    from src.database import Database
    summary = train_from_db(Database(config.db_path))
    print("\n--- CALIBRATION TRAINING (from settled outcomes) ---")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print()
    return 0


def cmd_service(config, cycles) -> int:
    from src.service import Service
    Service(config).run_forever(max_cycles=cycles)
    return 0


def cmd_healthcheck(config) -> int:
    from src.database import Database
    db = Database(config.db_path)
    n_err = db.query("SELECT COUNT(*) AS c FROM errors")[0]["c"]
    pnl = db.pnl_history(limit=1)
    stopped = config.emergency_stop_engaged()
    print(f"  emergency_stop : {stopped}")
    print(f"  errors logged  : {n_err}")
    if pnl:
        print(f"  last cycle     : {pnl[0]['ts']}  equity=${pnl[0]['bankroll']:.2f}")
    else:
        print("  last cycle     : (none yet)")
    return 1 if stopped else 0


def cmd_status(config) -> int:
    from src.database import Database
    db = Database(config.db_path)
    print("\n--- CONFIG (secrets redacted) ---")
    for k, v in config.as_public_dict().items():
        print(f"  {k:24} = {v}")
    armed = config.is_live_trading_armed()
    print(f"\n  LIVE config armed : {armed}  (emergency stop: {config.emergency_stop_engaged()})")
    from src.live_gate import edge_proven
    ok, reason = edge_proven(db, config)
    print(f"  Edge-proven gate  : {'PASS' if ok else 'BLOCK'} - {reason}")
    print(f"  => REAL TRADING   : {'ENABLED' if (armed and ok) else 'DISABLED (paper)'}")
    print("\n--- OPEN POSITIONS ---")
    rows = db.open_positions()
    if not rows:
        print("  (none)")
    for r in rows:
        print(f"  {r['ticker']:<26} {r['side']:<4} x{r['contracts']} @ {r['avg_price']:.2f} [{r['mode']}]")
    pnl = db.pnl_history(limit=1)
    if pnl:
        p = pnl[0]
        print(f"\n  last equity={p['bankroll']:.2f} realized={p['realized_pnl']:.2f} "
              f"unrealized={p['unrealized_pnl']:.2f} drawdown={p['drawdown']*100:.1f}%")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prediction_market_bot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command",
                   choices=["once", "run", "backtest", "histbacktest", "marketbacktest", "status",
                            "settle", "report", "calibrate", "service", "healthcheck"],
                   help="once=cycle, run=loop, backtest=mock metrics, histbacktest=forecast skill vs "
                        "past weather, marketbacktest=model vs real Kalshi prices, status=state, "
                        "settle=resolve vs real outcomes, report=live performance, calibrate=train, "
                        "service=scheduler, healthcheck=liveness")
    p.add_argument("--past-days", type=int, default=90, help="history window for hist/marketbacktest")
    p.add_argument("--lead-hours", type=int, default=24, help="decision time before close (marketbacktest)")
    p.add_argument("--walk-forward", action="store_true",
                   help="histbacktest: fit on early days, score held-out later days (out-of-sample)")
    p.add_argument("--mode", choices=["paper", "live"], default="paper",
                   help="paper (default). 'live' does NOT arm real trading by itself.")
    p.add_argument("--data-source", choices=["mock", "live", "polymarket"], default=None,
                   help="override DATA_SOURCE (mock=offline, live=real Kalshi+Open-Meteo, "
                        "polymarket=read-only Polymarket intl markets; all paper)")
    p.add_argument("--cycles", type=int, default=None, help="number of cycles for `run`")
    p.add_argument("--env", default=None, help="path to a .env file")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _apply_overrides(args)
    config = Config.from_env(args.env)
    if args.command == "once":
        return cmd_once(config)
    if args.command == "run":
        return cmd_run(config, args.cycles)
    if args.command == "backtest":
        return cmd_backtest(config)
    if args.command == "histbacktest":
        return cmd_histbacktest(config, args.past_days, args.walk_forward)
    if args.command == "marketbacktest":
        return cmd_marketbacktest(config, args.past_days, args.lead_hours)
    if args.command == "status":
        return cmd_status(config)
    if args.command == "settle":
        return cmd_settle(config)
    if args.command == "report":
        return cmd_report(config)
    if args.command == "calibrate":
        return cmd_calibrate(config)
    if args.command == "service":
        return cmd_service(config, args.cycles)
    if args.command == "healthcheck":
        return cmd_healthcheck(config)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
