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
