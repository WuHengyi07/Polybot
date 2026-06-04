"""Lightweight notifications for unattended operation.

Posts to a webhook (Discord/Slack-compatible `{"content": ...}` or `{"text": ...}`)
if `ALERT_WEBHOOK_URL` is set; otherwise it just logs. Used for alerts on errors
/ kill-switch and a once-a-day summary — never per-cycle spam.
"""
from __future__ import annotations

import os
from typing import Optional

from .utils import get_logger

log = get_logger("notify")


class Notifier:
    def __init__(self, config=None):
        self.webhook = (getattr(config, "alert_webhook_url", "") or os.environ.get("ALERT_WEBHOOK_URL", "")).strip()
        self._requests = None
        if self.webhook:
            try:
                import requests
                self._requests = requests
            except Exception:  # pragma: no cover
                log.warning("requests not installed; notifications will only be logged.")

    def _post(self, text: str) -> None:
        log.info("NOTIFY: %s", text)
        if not (self.webhook and self._requests):
            return
        try:  # pragma: no cover - network dependent
            self._requests.post(self.webhook, json={"content": text, "text": text}, timeout=10)
        except Exception as exc:  # pragma: no cover
            log.warning("Notification post failed: %s", exc)

    def alert(self, message: str) -> None:
        self._post(f"🚨 prediction_market_bot ALERT: {message}")

    def daily_summary(self, result) -> None:
        self._post(
            f"📊 daily: mode={result.mode} equity=${result.equity:.2f} "
            f"realized=${result.realized_pnl:.2f} open={result.open_positions} "
            f"drawdown={result.drawdown*100:.1f}%"
        )

    def heartbeat(self, result) -> None:
        # logged every cycle, but not pushed to the webhook (avoid spam)
        log.info("heartbeat: mode=%s equity=%.2f open=%d", result.mode, result.equity, result.open_positions)
