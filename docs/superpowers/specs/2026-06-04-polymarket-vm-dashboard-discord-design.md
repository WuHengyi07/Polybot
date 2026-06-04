# Design: Polymarket forward-test bot → VM deploy, web dashboard, Discord push

**Date:** 2026-06-04
**Status:** Approved (design), pending implementation plan
**Scope:** Extend the existing `prediction_market_bot` package. No rewrite, no new architecture.

## Goal

Take the existing paper-first weather trading bot and make it run as an unattended
service on an existing Linux VM, with:

1. **Polymarket as the primary exchange** (Kalshi still available via a flag).
2. **Paper trading now, live (on-chain) later** — this run collects the live
   forward-test evidence the edge-proven gate requires. No web3 this session.
3. A **web dashboard** reachable on the VM (Streamlit, bound to localhost).
4. **Discord push** of trade fills, periodic P&L summaries, errors/health, and
   settlement results.

Build locally, verify, then transfer to the VM.

## Non-goals (YAGNI)

- Grafana / Prometheus (Discord was chosen instead).
- Public dashboard exposure or auth (reached via SSH tunnel / Tailscale only).
- Live on-chain trading / web3 / wallet integration (deferred; the edge-proven
  gate remains the guard for later).
- Any new exchange beyond Kalshi + Polymarket.

## Current state (what we extend, not rebuild)

- **`src/notify.py`** — `Notifier` posts to a Discord-compatible webhook
  (`{"content": ...}`) from `ALERT_WEBHOOK_URL`. Has `alert()`, `daily_summary()`,
  `heartbeat()`. **Missing:** per-fill posts and settlement-result posts.
- **`src/service.py`** — crash-recovering scheduler. Calls `heartbeat` every cycle
  and, once per UTC day, settles → calibrates → `daily_summary`. **All
  notification orchestration already lives here**, keeping the trading core
  notification-free. This is where new notification hooks go.
- **`config.py`** — `data_source ∈ {mock, live (Kalshi), polymarket}`;
  `alert_webhook_url`, `loop_interval_seconds`, `polymarket_gamma_base/clob_base`
  already present. Polymarket settlement is wired in
  `service.build_settlement_source` (`PolymarketSettlementSource`).
- **`src/dashboard.py`** — Streamlit, reads the SQLite DB. Run via `streamlit run`.
  **Not yet part of the systemd deploy.**
- **`deploy/`** — systemd unit (`main.py service`), Docker, Windows bat.
  Linux-ready; **no dashboard service unit; no localhost-bind/Tailscale guidance.**

## Workstreams

### A. Discord push (extend `notify.py`, orchestrate from `service.py`)

The "Discord bot" is a Discord **incoming webhook** — no hosted application, phone-friendly.

New `Notifier` methods (pure message-formatting + existing `_post`):

- `trade_fills(new_fills)` — **one batched message per cycle** listing each new
  fill: city, bucket, side, price, size, modeled edge. Batching respects Discord's
  ~30 msg/min webhook rate limit and avoids per-trade spam.
- `settlement_results(resolved)` — posted from `service._settle`: per settled
  position, outcome vs model prediction + P&L delta (batched).
- Enriched `daily_summary` — add win rate, Brier-vs-market, and **progress toward
  the edge-proven gate** (e.g. "112/150 settled paper trades, ROI +x%").
- Errors / kill-switch: already covered by `alert()`.

Config:
- `NOTIFY_EVENTS` — comma list, default `fills,summary,errors,settlement`. Lets a
  category be muted. When no webhook is set, behavior is unchanged (logs only).

**Per-fill detection — chosen approach: service-loop DB diff.** `service` tracks
the last-seen position id (or created-at watermark); after each `run_cycle` it
queries the DB for positions newer than the watermark and passes them to
`trade_fills`. Rationale: keeps notification concerns out of `paper_trader` /
`execution_engine`, matching the existing pattern where `service` owns all
notifications. (Rejected alternative: hooking fills directly in the trading core —
more real-time but couples trading to Discord.)

### B. Polymarket forward-test profile (config only, no new code)

A VM `.env` profile: `DATA_SOURCE=polymarket`, paper mode (live flags unset), the
chosen cities, `LOOP_INTERVAL_SECONDS`, `ALERT_WEBHOOK_URL`, `NOTIFY_EVENTS`. This
runs the live forward-test the edge-proven gate needs. Kalshi remains available by
switching `DATA_SOURCE=live`.

### C. VM deploy + dashboard as a service (extend `deploy/`)

- **Second systemd unit** for Streamlit, bound to `127.0.0.1:8501`
  (`streamlit run src/dashboard.py --server.address 127.0.0.1 --server.port 8501`).
  No public exposure.
- **`deploy/README` access section:** SSH tunnel (`ssh -L 8501:localhost:8501 …`)
  or Tailscale private network.
- **Dashboard hardening:** auto-refresh + a panel showing forward-test progress
  toward the gate and notify/Discord status.
- **Transfer checklist:** rsync the code, create venv + `pip install -r
  requirements.txt`, fill `.env`, enable both systemd units, verify with
  `python main.py healthcheck` and a Discord test post.

### D. Tests

New `notify` methods get unit tests using a fake webhook poster (assert message
content / batching, no network), matching the existing 129-test style. Pure
formatting is separated from `_post` so it is testable offline. Service-loop
fill-diff logic gets a test with a seeded in-memory DB.

## Acceptance criteria

1. Running the service locally with `DATA_SOURCE=polymarket` and a test webhook
   posts: batched fills per cycle, settlement results on settle, an enriched daily
   summary, and error alerts on a forced failure.
2. `NOTIFY_EVENTS` mutes categories; no webhook set ⇒ logs only, no errors.
3. Streamlit dashboard runs bound to localhost and shows live positions, P&L,
   forward-test progress, and notify status.
4. `deploy/` has a dashboard systemd unit + an SSH-tunnel/Tailscale access doc + a
   transfer checklist.
5. Full test suite still green (existing 129 + new tests). No change to default
   behavior or the edge-proven live gate.
