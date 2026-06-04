"""SQLite persistence for snapshots, probabilities, signals, orders, trades,
positions, PnL, errors, and research notes.

One small wrapper class; every write is a plain parameterized INSERT so there's
nothing exotic to audit. Used by the execution engine, paper trader, backtester
and dashboard.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from .utils import get_logger, utcnow

log = get_logger("database")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, title TEXT,
    yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
    volume INTEGER, open_interest INTEGER, spread REAL, category TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS weather_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, location TEXT,
    target_date TEXT, variable TEXT, model TEXT, members_json TEXT,
    mean REAL, std REAL, n_members INTEGER);
CREATE TABLE IF NOT EXISTS model_probabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT,
    model_probability_yes REAL, confidence REAL, method TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, signal_type TEXT,
    side TEXT, edge REAL, model_probability REAL, reason_codes TEXT,
    detail_json TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, side TEXT,
    action TEXT, price REAL, contracts INTEGER, order_type TEXT, status TEXT,
    client_order_id TEXT, mode TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT, side TEXT,
    action TEXT, price REAL, contracts INTEGER, fee REAL, cash_flow REAL,
    mode TEXT, position_id TEXT);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT, ticker TEXT, side TEXT,
    contracts INTEGER, avg_price REAL, status TEXT, opened_ts TEXT, closed_ts TEXT,
    realized_pnl REAL, mode TEXT);
CREATE TABLE IF NOT EXISTS pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, bankroll REAL, realized_pnl REAL,
    unrealized_pnl REAL, drawdown REAL, open_positions INTEGER, mode TEXT);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS research_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, topic TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT UNIQUE, target_date TEXT,
    station TEXT, outcome_yes INTEGER, observed_high REAL, model_probability_yes REAL,
    market_implied_yes REAL, source TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS calibration_params (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, key TEXT, params_json TEXT);
"""


class Database:
    def __init__(self, path: str = "prediction_market_bot.db"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- low level -----------------------------------------------------
    def _insert(self, table: str, row: Dict[str, Any]) -> int:
        cols = ", ".join(row.keys())
        ph = ", ".join("?" for _ in row)
        cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", list(row.values()))
        self.conn.commit()
        return cur.lastrowid

    def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    # -- writers -------------------------------------------------------
    def record_market_snapshot(self, m, source: str) -> int:
        return self._insert("market_snapshots", {
            "ts": utcnow().isoformat(), "ticker": m.ticker, "title": m.title,
            "yes_bid": m.yes_bid, "yes_ask": m.yes_ask, "no_bid": m.no_bid, "no_ask": m.no_ask,
            "volume": m.volume, "open_interest": m.open_interest, "spread": m.yes_spread,
            "category": m.category, "source": source,
        })

    def record_weather_snapshot(self, ticker: str, dist) -> int:
        return self._insert("weather_snapshots", {
            "ts": utcnow().isoformat(), "ticker": ticker,
            "location": getattr(dist, "location", None),
            "target_date": str(getattr(dist, "valid_date", "") or ""),
            "variable": dist.variable, "model": dist.model,
            "members_json": json.dumps(list(dist.members)),
            "mean": dist.mean, "std": dist.std, "n_members": len(dist.members),
        })

    def record_probability(self, ticker: str, est) -> int:
        return self._insert("model_probabilities", {
            "ts": utcnow().isoformat(), "ticker": ticker,
            "model_probability_yes": est.model_probability_yes, "confidence": est.confidence,
            "method": est.method, "detail_json": json.dumps(est.components),
        })

    def record_signal(self, sig, mode: str) -> int:
        return self._insert("signals", {
            "ts": utcnow().isoformat(), "ticker": sig.ticker, "signal_type": sig.signal_type,
            "side": sig.side or "", "edge": sig.edge, "model_probability": sig.model_probability,
            "reason_codes": ",".join(sig.reason_codes), "detail_json": json.dumps(sig.detail), "mode": mode,
        })

    def record_order(self, row: Dict[str, Any]) -> int:
        row = {"ts": utcnow().isoformat(), **row}
        row["detail_json"] = json.dumps(row.get("detail_json", {}))
        return self._insert("orders", row)

    def record_trade(self, row: Dict[str, Any]) -> int:
        return self._insert("trades", {"ts": utcnow().isoformat(), **row})

    def upsert_position(self, row: Dict[str, Any]) -> None:
        self.conn.execute("DELETE FROM positions WHERE position_id = ?", (row["position_id"],))
        self._insert("positions", row)

    def record_pnl(self, row: Dict[str, Any]) -> int:
        return self._insert("pnl", {"ts": utcnow().isoformat(), **row})

    def record_error(self, source: str, message: str) -> int:
        return self._insert("errors", {"ts": utcnow().isoformat(), "source": source, "message": message})

    def add_research_note(self, topic: str, note: str) -> int:
        return self._insert("research_notes", {"ts": utcnow().isoformat(), "topic": topic, "note": note})

    def record_settlement(self, row: Dict[str, Any]) -> None:
        """Upsert a settled market outcome (ticker is unique)."""
        row = {"ts": utcnow().isoformat(), **row}
        cols = ", ".join(row.keys())
        ph = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT OR REPLACE INTO settlements ({cols}) VALUES ({ph})", list(row.values()))
        self.conn.commit()

    def get_settlements(self) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM settlements ORDER BY id")

    def settled_tickers(self) -> set:
        return {r["ticker"] for r in self.query("SELECT ticker FROM settlements")}

    def latest_probability(self, ticker: str) -> Optional[Dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM model_probabilities WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,))
        return rows[0] if rows else None

    def latest_market_snapshot(self, ticker: str) -> Optional[Dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM market_snapshots WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,))
        return rows[0] if rows else None

    def save_calibration_params(self, kind: str, key: str, params: Dict[str, Any]) -> int:
        return self._insert("calibration_params", {
            "ts": utcnow().isoformat(), "kind": kind, "key": key, "params_json": json.dumps(params)})

    def load_latest_calibration(self, kind: str) -> Dict[str, Any]:
        """Return {key: params} using the most recent row per key for this kind."""
        rows = self.query("SELECT * FROM calibration_params WHERE kind=? ORDER BY id", (kind,))
        out: Dict[str, Any] = {}
        for r in rows:  # later rows overwrite earlier -> latest wins
            try:
                out[r["key"]] = json.loads(r["params_json"])
            except (ValueError, TypeError):
                continue
        return out

    # -- readers -------------------------------------------------------
    def recent_signals(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))

    def recent_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    def trades_since(self, last_id: int, action: str = "open") -> List[Dict[str, Any]]:
        """Trades with id > last_id for the given action, oldest first (for fill diffs)."""
        return self.query(
            "SELECT * FROM trades WHERE id > ? AND action = ? ORDER BY id",
            (last_id, action))

    def max_trade_id(self) -> int:
        rows = self.query("SELECT MAX(id) AS m FROM trades")
        return int(rows[0]["m"]) if rows and rows[0]["m"] is not None else 0

    def latest_signal(self, ticker: str) -> Optional[Dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM signals WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,))
        return rows[0] if rows else None

    def open_positions(self) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM positions WHERE status = 'open' ORDER BY ticker")

    def all_positions(self) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM positions ORDER BY id DESC")

    def pnl_history(self, limit: int = 500) -> List[Dict[str, Any]]:
        return self.query("SELECT * FROM pnl ORDER BY id DESC LIMIT ?", (limit,))

    def close(self) -> None:
        self.conn.close()
