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
