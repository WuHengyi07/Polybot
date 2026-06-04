# Deploying the bot as an unattended service

The service runs one analysis/trade cycle every `LOOP_INTERVAL_SECONDS`, and once
per day it settles matured positions, retrains calibration, and posts a summary.
It is **paper by default**; live trading additionally requires the `.env` flags +
Kalshi keys **and** the programmatic edge-proven gate (a real paper track record).

Set `ALERT_WEBHOOK_URL` in `.env` (Discord/Slack-compatible) to get error/kill-switch
alerts and the daily summary.

## Windows (Task Scheduler)
Create a task → Trigger: *At startup* → Action: run `deploy\run_service.bat`
(it loops and restarts the service if it ever exits). Check liveness with:
```
python main.py healthcheck
```

## Linux (systemd)
```
sudo cp -r . /opt/prediction_market_bot
sudo cp deploy/prediction_market_bot.service /etc/systemd/system/
sudo systemctl enable --now prediction_market_bot
journalctl -u prediction_market_bot -f
```

## Docker
```
docker build -t pmbot -f deploy/Dockerfile .
docker run -d --restart=always --env-file .env -v $PWD/data:/app/data pmbot
```

## Emergency stop
Create a file named `EMERGENCY_STOP` in the working directory (or set
`EMERGENCY_STOP=true`) — the next cycle halts immediately and alerts.

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
