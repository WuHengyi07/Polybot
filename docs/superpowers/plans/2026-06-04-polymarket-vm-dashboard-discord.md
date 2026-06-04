# Polymarket VM Deploy + Dashboard + Discord Push — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `prediction_market_bot` so it runs unattended on a Linux VM as a Polymarket-primary paper forward-test, pushing trade fills / settlement results / daily P&L / health alerts to a Discord webhook, with a localhost-bound Streamlit dashboard reachable via SSH tunnel or Tailscale.

**Architecture:** All new behavior extends existing modules — `notify.py` gains pure message formatters + batched event methods; `service.py` (which already owns notification orchestration) detects new fills by diffing the `trades` table on an id watermark, posts settlement results around the daily settle pass, and enriches the daily summary with `performance.compute_performance` + `live_gate.edge_proven`. The dashboard gains a forward-test-progress panel. `deploy/` gains a dashboard systemd unit, a Polymarket `.env` profile, and an SSH-tunnel/Tailscale + transfer doc. No trading-core changes, no new architecture, edge-proven gate untouched.

**Tech Stack:** Python 3.13, SQLite, `requests` (webhook), Streamlit, systemd, pytest.

---

## File Structure

- `config.py` — **Modify.** Add `notify_events` field + parse + `notify_enabled()` helper.
- `src/notify.py` — **Modify.** Pure formatters (`format_fills`, `format_settlements`, `format_daily`) + `Notifier.trade_fills` / `settlement_results` / enriched `daily_summary`; event gating.
- `src/database.py` — **Modify.** Add `trades_since(last_id, action)` and `latest_signal(ticker)` readers.
- `src/service.py` — **Modify.** Fill watermark + post-cycle fill diff; settlement-result posting around `_settle`; enriched daily summary.
- `src/dashboard.py` — **Modify.** Add a forward-test-progress + notify-status panel.
- `deploy/prediction_market_bot-dashboard.service` — **Create.** systemd unit for Streamlit on `127.0.0.1:8501`.
- `deploy/.env.polymarket.example` — **Create.** VM Polymarket paper profile.
- `deploy/README.md` — **Modify.** Dashboard service, SSH-tunnel/Tailscale access, transfer checklist.
- `.env.example` — **Modify.** Document `NOTIFY_EVENTS`.
- `tests/test_config.py` — **Modify.** `notify_events` parsing + `notify_enabled`.
- `tests/test_notify.py` — **Create.** Formatters, event gating, batching, no-webhook-logs-only.
- `tests/test_database.py` — **Create.** `trades_since` + `latest_signal`.
- `tests/test_service.py` — **Modify.** Service posts fills on new trades; no historical replay on startup.

---

## Task 0: Make the project a git repo (one-time, enables commits)

**Files:** none (repo init)

- [ ] **Step 1: Check whether git is already initialized**

Run: `git -C "." rev-parse --is-inside-work-tree 2>NUL`
Expected: prints `true` (skip to Task 1) or errors / prints nothing (continue).

- [ ] **Step 2: Initialize and make the baseline commit**

```bash
git init
git add -A
git commit -m "chore: baseline before VM/dashboard/Discord work"
```

Expected: a commit is created. `.gitignore` already excludes the venv and `prediction_market_bot.db`.

---

## Task 1: Config — `notify_events`

**Files:**
- Modify: `config.py` (field near `alert_webhook_url:173`; parse near `alert_webhook_url=:254`; helper near `emergency_stop_engaged:258`)
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_notify_events_default_and_helper(monkeypatch):
    from config import Config
    for k in ("NOTIFY_EVENTS",):
        monkeypatch.delenv(k, raising=False)
    cfg = Config.from_env(env_path=False) if False else Config.from_env()
    assert cfg.notify_enabled("fills")
    assert cfg.notify_enabled("settlement")
    assert cfg.notify_enabled("summary")
    assert cfg.notify_enabled("errors")


def test_notify_events_mutes_categories(monkeypatch):
    from config import Config
    monkeypatch.setenv("NOTIFY_EVENTS", "errors")
    cfg = Config.from_env()
    assert cfg.notify_enabled("errors")
    assert not cfg.notify_enabled("fills")
    assert not cfg.notify_enabled("summary")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -k notify_events -v`
Expected: FAIL — `Config` has no attribute `notify_enabled` / `notify_events`.

- [ ] **Step 3: Add the field, parsing, and helper**

In `config.py`, after the `alert_webhook_url: str = ""` field (line ~173) add:

```python
    # Comma list of event categories pushed to ALERT_WEBHOOK_URL. Subset of:
    # fills, summary, settlement, errors. Empty/unset = all four.
    notify_events: str = "fills,summary,settlement,errors"
```

In `from_env`, after `alert_webhook_url=g("ALERT_WEBHOOK_URL", "") or "",` (line ~254) add:

```python
            notify_events=g("NOTIFY_EVENTS", "fills,summary,settlement,errors") or "fills,summary,settlement,errors",
```

Add this method next to `emergency_stop_engaged` (line ~258):

```python
    def notify_enabled(self, event: str) -> bool:
        """True if `event` (fills|summary|settlement|errors) should be pushed."""
        events = {e.strip().lower() for e in (self.notify_events or "").split(",") if e.strip()}
        return (not events) or (event.lower() in events)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -k notify_events -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat(config): add NOTIFY_EVENTS category gating"
```

---

## Task 2: Database — `trades_since` + `latest_signal` readers

**Files:**
- Modify: `src/database.py` (readers section near `recent_trades:179`)
- Test: `tests/test_database.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_database.py`:

```python
import os
import tempfile

from src.database import Database


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Database(path), path


def test_trades_since_returns_only_newer_opens():
    db, path = _db()
    try:
        id1 = db.record_trade({"ticker": "T1", "side": "yes", "action": "open",
                               "price": 0.4, "contracts": 3, "fee": 0.01,
                               "cash_flow": -1.2, "mode": "paper", "position_id": "T1:yes"})
        db.record_trade({"ticker": "T1", "side": "yes", "action": "close",
                         "price": 0.5, "contracts": 3, "fee": 0.01,
                         "cash_flow": 1.5, "mode": "paper", "position_id": "T1:yes"})
        id3 = db.record_trade({"ticker": "T2", "side": "no", "action": "open",
                               "price": 0.3, "contracts": 2, "fee": 0.01,
                               "cash_flow": -0.6, "mode": "paper", "position_id": "T2:no"})
        new = db.trades_since(id1)  # only opens AFTER id1
        assert [t["id"] for t in new] == [id3]
        assert new[0]["ticker"] == "T2"
    finally:
        db.close()
        os.remove(path)


def test_latest_signal_returns_most_recent():
    db, path = _db()
    try:
        class Sig:
            ticker = "T1"; signal_type = "BUY_YES"; side = "yes"; edge = 0.12
            model_probability = 0.6; reason_codes = ["EDGE_OK"]; detail = {}
        db.record_signal(Sig(), mode="paper")
        Sig.edge = 0.21
        db.record_signal(Sig(), mode="paper")
        row = db.latest_signal("T1")
        assert row is not None
        assert abs(row["edge"] - 0.21) < 1e-9
        assert db.latest_signal("NOPE") is None
    finally:
        db.close()
        os.remove(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_database.py -v`
Expected: FAIL — `Database` has no attribute `trades_since` / `latest_signal`.

- [ ] **Step 3: Add the readers**

In `src/database.py`, in the `# -- readers --` section (after `recent_trades`, line ~180) add:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_database.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/database.py tests/test_database.py
git commit -m "feat(db): add trades_since/max_trade_id/latest_signal readers"
```

---

## Task 3: Notify — pure formatters + batched event methods + gating

**Files:**
- Modify: `src/notify.py` (whole module)
- Test: `tests/test_notify.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify.py`:

```python
from types import SimpleNamespace

from src import notify
from src.notify import Notifier, format_fills, format_settlements, format_daily


def _cfg(events="fills,summary,settlement,errors", webhook=""):
    return SimpleNamespace(alert_webhook_url=webhook, notify_events=events,
                           notify_enabled=lambda e: (not events) or e in events,
                           edge_proven_min_trades=150)


def test_format_fills_batches_lines():
    fills = [({"ticker": "PM-SHA-20260604-1", "side": "yes", "contracts": 5, "price": 0.42}, 0.18),
             ({"ticker": "PM-HKG-20260604-2", "side": "no", "contracts": 3, "price": 0.31}, None)]
    msg = format_fills(fills)
    assert "2 new fill" in msg
    assert "PM-SHA-20260604-1" in msg and "YES x5 @ 0.42" in msg
    assert "edge +0.180" in msg
    assert "PM-HKG-20260604-2" in msg and "NO x3 @ 0.31" in msg


def test_format_fills_empty_is_blank():
    assert format_fills([]) == ""


def test_format_settlements_shows_outcome_and_model():
    items = [{"ticker": "PM-SHA-20260604-1", "side": "yes", "outcome_yes": 1,
              "observed_high": 31.0, "model_probability_yes": 0.7}]
    msg = format_settlements(items, realized_delta=2.35)
    assert "PM-SHA-20260604-1" in msg
    assert "YES" in msg and "obs 31.0" in msg and "model 0.70" in msg
    assert "+2.35" in msg


def test_format_daily_includes_gate_progress():
    perf = SimpleNamespace(n_settled=112, win_rate=0.74, realized_pnl=8.4, roi=0.06,
                           brier_model=0.09, brier_market=0.10, edge_vs_market=0.01)
    msg = format_daily(SimpleNamespace(mode="paper", equity=108.4, realized_pnl=8.4,
                                       open_positions=3, drawdown=0.02),
                       perf=perf, gate=(False, "insufficient sample: 112/150 settled trades"),
                       min_trades=150)
    assert "112/150" in msg
    assert "win 74.0%" in msg
    assert "edge vs market +0.010" in msg
    assert "GATE: not yet" in msg


def test_trade_fills_respects_event_gate_and_no_webhook(caplog):
    posted = []
    n = Notifier(_cfg(events="summary"))  # fills muted, no webhook
    n._post = lambda text: posted.append(text)
    n.trade_fills([({"ticker": "T", "side": "yes", "contracts": 1, "price": 0.5}, 0.1)])
    assert posted == []  # muted


def test_trade_fills_posts_when_enabled():
    posted = []
    n = Notifier(_cfg(events="fills"))
    n._post = lambda text: posted.append(text)
    n.trade_fills([({"ticker": "T", "side": "yes", "contracts": 1, "price": 0.5}, 0.1)])
    assert len(posted) == 1 and "1 new fill" in posted[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notify.py -v`
Expected: FAIL — `format_fills` / `format_settlements` / `format_daily` / `trade_fills` not defined.

- [ ] **Step 3: Rewrite `src/notify.py`**

Replace the whole file with:

```python
"""Lightweight notifications for unattended operation.

Posts to a webhook (Discord/Slack-compatible `{"content": ...}` or `{"text": ...}`)
if `ALERT_WEBHOOK_URL` is set; otherwise it just logs. Categories (fills, summary,
settlement, errors) are gated by `config.notify_enabled(...)`. Per-event messages
are BATCHED (one message per cycle / settle pass) to respect the webhook rate limit
and avoid spam.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from .utils import get_logger

log = get_logger("notify")


# --------------------------------------------------------------------------- #
# Pure formatters (no I/O — unit-testable)
# --------------------------------------------------------------------------- #
def _fmt_fill(trade: dict, edge: Optional[float]) -> str:
    e = f"  edge {edge:+.3f}" if edge is not None else ""
    return (f"{trade['ticker']} {str(trade['side']).upper()} "
            f"x{int(trade['contracts'])} @ {float(trade['price']):.2f}{e}")


def format_fills(fills: List[Tuple[dict, Optional[float]]]) -> str:
    if not fills:
        return ""
    lines = [f"🟢 {len(fills)} new fill(s):"]
    lines += [f"• {_fmt_fill(t, e)}" for t, e in fills]
    return "\n".join(lines)


def format_settlements(items: List[dict], realized_delta: Optional[float] = None) -> str:
    if not items:
        return ""
    lines = [f"🏁 {len(items)} settlement(s):"]
    for s in items:
        out = "YES" if int(s.get("outcome_yes", 0)) else "NO"
        obs = s.get("observed_high")
        mp = s.get("model_probability_yes")
        obs_s = f"  obs {float(obs):.1f}" if obs is not None else ""
        mp_s = f"  model {float(mp):.2f}" if mp is not None else ""
        lines.append(f"• {s['ticker']} {str(s.get('side','')).upper()} -> {out}{obs_s}{mp_s}")
    if realized_delta is not None:
        lines.append(f"realized this pass: ${realized_delta:+.2f}")
    return "\n".join(lines)


def format_daily(result, perf=None, gate: Optional[Tuple[bool, str]] = None,
                 min_trades: int = 150) -> str:
    lines = [f"📊 daily: mode={result.mode} equity=${result.equity:.2f} "
             f"realized=${result.realized_pnl:.2f} open={result.open_positions} "
             f"drawdown={result.drawdown*100:.1f}%"]
    if perf is not None:
        lines.append(f"settled {perf.n_settled}/{min_trades}  win {perf.win_rate*100:.1f}%  "
                     f"roi {perf.roi*100:+.1f}%")
        if perf.edge_vs_market is not None:
            lines.append(f"edge vs market {perf.edge_vs_market:+.3f} "
                         f"(model {perf.brier_model} / market {perf.brier_market})")
    if gate is not None:
        allowed, reason = gate
        lines.append(f"GATE: {'PROVEN' if allowed else 'not yet'} — {reason}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
class Notifier:
    def __init__(self, config=None):
        self.config = config
        self.webhook = (getattr(config, "alert_webhook_url", "") or
                        os.environ.get("ALERT_WEBHOOK_URL", "")).strip()
        self._requests = None
        if self.webhook:
            try:
                import requests
                self._requests = requests
            except Exception:  # pragma: no cover
                log.warning("requests not installed; notifications will only be logged.")

    def _enabled(self, event: str) -> bool:
        fn = getattr(self.config, "notify_enabled", None)
        return fn(event) if callable(fn) else True

    def _post(self, text: str) -> None:
        log.info("NOTIFY: %s", text)
        if not (self.webhook and self._requests):
            return
        try:  # pragma: no cover - network dependent
            self._requests.post(self.webhook, json={"content": text, "text": text}, timeout=10)
        except Exception as exc:  # pragma: no cover
            log.warning("Notification post failed: %s", exc)

    # -- events -------------------------------------------------------- #
    def alert(self, message: str) -> None:
        if not self._enabled("errors"):
            return
        self._post(f"🚨 prediction_market_bot ALERT: {message}")

    def trade_fills(self, fills: List[Tuple[dict, Optional[float]]]) -> None:
        if not fills or not self._enabled("fills"):
            return
        self._post(format_fills(fills))

    def settlement_results(self, items: List[dict],
                           realized_delta: Optional[float] = None) -> None:
        if not items or not self._enabled("settlement"):
            return
        self._post(format_settlements(items, realized_delta))

    def daily_summary(self, result, perf=None, gate=None) -> None:
        if not self._enabled("summary"):
            return
        min_trades = getattr(self.config, "edge_proven_min_trades", 150)
        self._post(format_daily(result, perf=perf, gate=gate, min_trades=min_trades))

    def heartbeat(self, result) -> None:
        # logged every cycle, but not pushed to the webhook (avoid spam)
        log.info("heartbeat: mode=%s equity=%.2f open=%d",
                 result.mode, result.equity, result.open_positions)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_notify.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/notify.py tests/test_notify.py
git commit -m "feat(notify): batched fills/settlement posts + enriched daily summary + gating"
```

---

## Task 4: Service — wire fills, settlement results, enriched summary

**Files:**
- Modify: `src/service.py` (`__init__:38`, `run_forever` loop body near `:54`, `_maybe_daily_tasks:67`, `_settle:83`)
- Test: `tests/test_service.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_service.py` (mock mode opens paper trades, so new fills should be posted):

```python
def test_service_posts_new_fills(monkeypatch, tmp_path):
    from config import Config
    from src.service import Service

    cfg = Config.from_env()
    cfg.data_source = "mock"
    cfg.db_path = str(tmp_path / "svc.db")
    cfg.loop_interval_seconds = 0
    cfg.notify_events = "fills,summary,settlement,errors"

    svc = Service(cfg)
    posted = []
    svc.notifier._post = lambda text: posted.append(text)

    svc.run_forever(max_cycles=1)

    # If the mock cycle opened any positions, a batched fill message was posted.
    trades = svc.engine.db.trades_since(0)
    if trades:
        assert any("new fill" in p for p in posted)
    # Watermark advanced so a second pass would not re-post the same fills.
    assert svc._last_trade_id == svc.engine.db.max_trade_id()


def test_service_does_not_post_historical_fills_on_startup(tmp_path):
    from config import Config
    from src.service import Service

    cfg = Config.from_env()
    cfg.data_source = "mock"
    cfg.db_path = str(tmp_path / "svc2.db")

    # Pre-seed a trade BEFORE the service starts.
    from src.database import Database
    pre = Database(cfg.db_path)
    pre.record_trade({"ticker": "OLD", "side": "yes", "action": "open", "price": 0.4,
                      "contracts": 1, "fee": 0.0, "cash_flow": -0.4, "mode": "paper",
                      "position_id": "OLD:yes"})
    pre.close()

    svc = Service(cfg)
    # Watermark must start at the pre-existing max id, so OLD is never posted.
    assert svc._last_trade_id >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_service.py -k "new_fills or historical_fills" -v`
Expected: FAIL — `Service` has no `_last_trade_id`.

- [ ] **Step 3: Wire the service**

In `src/service.py`, set the watermark in `__init__` (after `self._last_daily = None`):

```python
        self._last_daily = None
        # Start the fill watermark at the current max trade id so the service never
        # re-posts historical fills when it (re)starts.
        self._last_trade_id = self.engine.db.max_trade_id()
```

In `run_forever`, post new fills right after a successful cycle. Replace the
`result = self.engine.run_cycle()` / `self.notifier.heartbeat(result)` lines:

```python
            try:
                result = self.engine.run_cycle()
                self.notifier.heartbeat(result)
                self._post_new_fills()
                self._maybe_daily_tasks(result)
```

Add this helper method to `Service`:

```python
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
```

Replace `_settle` to capture resolved + realized delta and post results:

```python
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
```

Replace the summary call in `_maybe_daily_tasks` (the `self.notifier.daily_summary(fresh)` line):

```python
            fresh = self.engine._summary(halted=False, markets_by_ticker={})
            from .live_gate import edge_proven
            from .performance import compute_performance
            perf = compute_performance(self.engine.db, self.config)
            gate = edge_proven(self.engine.db, self.config)
            self.notifier.daily_summary(fresh, perf=perf, gate=gate)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_service.py -v`
Expected: PASS (existing service tests + the 2 new ones).

- [ ] **Step 5: Commit**

```bash
git add src/service.py tests/test_service.py
git commit -m "feat(service): push batched fills, settlement results, gate-aware daily summary"
```

---

## Task 5: Dashboard — forward-test progress + notify status panel

**Files:**
- Modify: `src/dashboard.py` (insert after the Portfolio section, line ~78)
- Test: none (Streamlit UI — manual verification in Task 8)

- [ ] **Step 1: Add the panel**

In `src/dashboard.py`, immediately after the Portfolio `else:` block (the
`st.info("No cycles recorded yet...")` near line 78), insert:

```python
# --------------------------------------------------------------------------- #
# Forward-test progress toward the edge-proven gate + notify status
# --------------------------------------------------------------------------- #
st.subheader("Forward-test progress (toward live)")
from src.performance import compute_performance  # noqa: E402
from src.live_gate import edge_proven  # noqa: E402

perf = compute_performance(db, config)
allowed, reason = edge_proven(db, config)
g1, g2, g3, g4 = st.columns(4)
g1.metric("Settled trades", f"{perf.n_settled}/{config.edge_proven_min_trades}")
g2.metric("Win rate", f"{perf.win_rate*100:.1f}%")
g3.metric("ROI", f"{perf.roi*100:+.1f}%")
g4.metric("Edge vs market",
          f"{perf.edge_vs_market:+.3f}" if perf.edge_vs_market is not None else "n/a")
st.progress(min(1.0, perf.n_settled / max(1, config.edge_proven_min_trades)))
(st.success if allowed else st.warning)(
    f"Edge-proven gate: {'PROVEN' if allowed else 'NOT YET'} — {reason}")

webhook_set = bool((config.alert_webhook_url or "").strip())
st.caption(f"Discord push: {'configured' if webhook_set else 'not configured'} · "
           f"events = {config.notify_events}")
```

- [ ] **Step 2: Syntax-check the dashboard module**

Run: `python -c "import ast; ast.parse(open('src/dashboard.py', encoding='utf-8').read()); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add src/dashboard.py
git commit -m "feat(dashboard): forward-test progress + notify status panel"
```

---

## Task 6: Deploy — dashboard service, Polymarket profile, access + transfer doc

**Files:**
- Create: `deploy/prediction_market_bot-dashboard.service`
- Create: `deploy/.env.polymarket.example`
- Modify: `deploy/README.md`
- Modify: `.env.example`

- [ ] **Step 1: Create the dashboard systemd unit**

Create `deploy/prediction_market_bot-dashboard.service`:

```ini
[Unit]
Description=prediction_market_bot Streamlit dashboard (localhost only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/prediction_market_bot
EnvironmentFile=/opt/prediction_market_bot/.env
# Bind to localhost ONLY — reach it via SSH tunnel or Tailscale, never public.
ExecStart=/usr/bin/python3 -m streamlit run src/dashboard.py \
  --server.address 127.0.0.1 --server.port 8501 \
  --server.headless true --browser.gatherUsageStats false
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create the Polymarket VM profile**

Create `deploy/.env.polymarket.example`:

```bash
# --- Polymarket paper forward-test profile (VM) ---
DATA_SOURCE=polymarket
PAPER_TRADING=true
LIVE_TRADING=false
AUTO_TRADE=false

# International Polymarket daily-temperature cities (edit as desired)
WEATHER_CITIES=SHA,HKG,SEL,TPE,PAR,LON,TYO

# One cycle every 10 minutes
LOOP_INTERVAL_SECONDS=600
STARTING_BANKROLL=100

# Discord incoming webhook + which categories to push
ALERT_WEBHOOK_URL=
NOTIFY_EVENTS=fills,summary,settlement,errors

# Switch to Kalshi instead with: DATA_SOURCE=live (US cities NYC,CHI)
```

- [ ] **Step 3: Document `NOTIFY_EVENTS` in `.env.example`**

Add near the `ALERT_WEBHOOK_URL` line in `.env.example`:

```bash
# Which notification categories to push to ALERT_WEBHOOK_URL.
# Subset of: fills,summary,settlement,errors  (unset = all four)
NOTIFY_EVENTS=fills,summary,settlement,errors
```

- [ ] **Step 4: Extend `deploy/README.md`**

Append to `deploy/README.md`:

````markdown
## Web dashboard on the VM (localhost-bound)

The dashboard binds to `127.0.0.1:8501` and is **never** exposed publicly.

```
sudo cp deploy/prediction_market_bot-dashboard.service /etc/systemd/system/
sudo systemctl enable --now prediction_market_bot-dashboard
```

Reach it from your laptop with an SSH tunnel:

```
ssh -L 8501:localhost:8501 user@your-vm
# then open http://localhost:8501 in your browser
```

…or, if the VM is on your Tailscale network, browse to
`http://<tailscale-name>:8501` after binding Streamlit to the Tailscale IP
(replace `127.0.0.1` in the unit with the `tailscale0` address). SSH tunnel is
the simplest and is recommended.

## Polymarket forward-test profile

```
cp deploy/.env.polymarket.example /opt/prediction_market_bot/.env
# edit .env: set ALERT_WEBHOOK_URL (Discord incoming webhook), pick WEATHER_CITIES
```

This runs Polymarket as the primary exchange in **paper** mode — the live
forward-test that the edge-proven gate needs. Live on-chain trading is out of
scope here and stays disabled by the gate regardless of flags.

## Transfer checklist (laptop → VM)

1. `rsync -av --exclude .venv --exclude '*.db' ./ user@vm:/opt/prediction_market_bot/`
2. On the VM: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
3. `cp deploy/.env.polymarket.example .env` and fill in `ALERT_WEBHOOK_URL`.
4. `python main.py once` — confirm it parses markets and opens paper positions.
5. Confirm a Discord test post arrives (a fill or the daily summary).
6. `sudo cp deploy/prediction_market_bot.service /etc/systemd/system/`
   `sudo cp deploy/prediction_market_bot-dashboard.service /etc/systemd/system/`
   `sudo systemctl enable --now prediction_market_bot prediction_market_bot-dashboard`
7. `python main.py healthcheck` and `journalctl -u prediction_market_bot -f`.
8. SSH-tunnel to `http://localhost:8501` and verify the dashboard + progress panel.
````

- [ ] **Step 5: Commit**

```bash
git add deploy/prediction_market_bot-dashboard.service deploy/.env.polymarket.example deploy/README.md .env.example
git commit -m "feat(deploy): dashboard service, Polymarket profile, SSH-tunnel + transfer doc"
```

---

## Task 7: Full suite green + final verification

**Files:** none (verification)

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -q`
Expected: all tests pass (existing ~129 + new config/db/notify/service tests). 0 failures.

- [ ] **Step 2: Smoke-test the service end-to-end in mock mode with a capturing webhook**

Run:
```bash
python -c "from config import Config; from src.service import Service; \
c=Config.from_env(); c.data_source='mock'; c.loop_interval_seconds=0; \
c.db_path='smoke.db'; s=Service(c); s.notifier._post=lambda t: print('POST>',t); \
s.run_forever(max_cycles=2)"
```
Expected: prints `POST> 🟢 ... new fill(s):` if the mock cycle opened positions; no tracebacks. Then `del smoke.db` / `Remove-Item smoke.db`.

- [ ] **Step 3: Manually verify the dashboard (Windows local)**

Run: `streamlit run src/dashboard.py`
Expected: the "Forward-test progress (toward live)" panel renders with the gate
status and the "Discord push: …" caption. Stop with Ctrl+C.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "test: full suite green; VM/dashboard/Discord workstream complete"
```

---

## Self-Review notes (filled during planning)

- **Spec coverage:** A (Discord fills/settlement/summary/errors) → Tasks 1,3,4. B (Polymarket paper profile) → Task 6 `.env.polymarket.example`. C (dashboard + localhost + SSH/Tailscale + transfer) → Tasks 5,6. D (tests) → Tasks 1–4,7. Acceptance criteria 1–5 map to Tasks 4/7 (events), 3 (gating/no-webhook), 5 (dashboard), 6 (deploy assets), 7 (suite green).
- **P&L delta decision:** per-settlement exact PnL isn't a single stored column; the settlement message reports a **batch** `realized_delta` (post- minus pre-settle `compute_performance().realized_pnl`) — accurate and not misleading. Per-position detail lives in the daily summary + dashboard.
- **Fill edge source:** `trades` has no edge column, so `_post_new_fills` looks up `db.latest_signal(ticker).edge`; `None` when absent (handled in `format_fills`).
- **No-historical-replay:** `_last_trade_id` is seeded from `max_trade_id()` in `__init__`, so restarts never re-post old fills.
- **Type consistency:** `trades_since`, `max_trade_id`, `latest_signal`, `trade_fills`, `settlement_results`, `daily_summary(result, perf, gate)`, `notify_enabled`, `format_fills/settlements/daily` names are used identically across Tasks 1–5.
