# prediction_market_bot

An automated, **paper-first** trading bot for prediction-market **weather** contracts
(Kalshi daily high-temperature markets). It estimates a model probability from
**weather-forecast ensembles**, compares it to **executable market prices**, and
only trades when the post-fee edge is large enough — with strict risk limits,
logging, and a hard-gated live-trading path that is **off by default**.

> ⚠️ **Reddit PnL claims are not proof of edge.** This project was built from a
> skeptical research pass (see [`research_memo.md`](research_memo.md)). Of the 15
> "weather trading" sources reviewed, 14 were unverifiable Reddit posts whose
> headline claims (90–98% win rates, $85k profits, $100→$4,923 runs) carry every
> classic red flag: survivorship bias, the win-rate illusion of selling deep
> favorites, unrealized/screenshot "proof", overbetting, and product funnels. The
> bot is a **research scaffold**, not a money printer. Treat any "edge" it shows
> as a hypothesis to be validated on **real settled outcomes**, not a guarantee.

---

## What it does (plain English)

For each weather market like *"Will the high temp in NYC be >83° on Jun 3, 2026?"*:

1. **Parse** the question → location, station, date, variable, threshold, settlement source.
2. **Forecast** the daily high as a *distribution* using a weather ensemble
   (e.g. 31 GFS members from Open-Meteo), not a single number.
3. **Probability**: convert that distribution into `model_probability_yes`
   (fraction of members over the threshold, blended with a Normal-CDF estimate).
4. **Edge**: compare to the **executable** price — buy at the ask, sell at the
   bid — after subtracting Kalshi fees and a slippage buffer.
5. **Signal**: `BUY_YES` / `BUY_NO` / `HOLD` / `EXIT` / `DO_NOT_TRADE`, each with
   reason codes.
6. **Risk**: a risk manager sizes the trade (default 3% of bankroll) and enforces
   every limit (exposure caps, daily-loss limit, max positions, liquidity, spread,
   confidence, settlement clarity, price sanity, drawdown kill switch).
7. **Trade**: paper engine simulates a realistic fill (or, only if fully armed, a
   guarded live order). Everything is logged to SQLite.

The core formula:

```
edge_yes = model_probability_yes - yes_ask        (entry uses the ASK)
edge_no  = (1 - model_probability_yes) - no_ask
remaining_edge_yes = model_probability_yes - yes_bid   (exit uses the BID)
```
A trade only fires when `edge_net = edge − fee − slippage ≥ ENTRY_EDGE_THRESHOLD`.

---

## Quick start

```bash
cd prediction_market_bot
pip install -r requirements.txt          # core/tests run on stdlib; this adds live data + dashboard + tests

python main.py once                      # ONE paper cycle on MOCK data (fully offline)
python main.py run --cycles 5            # loop 5 paper cycles
python main.py settle                    # resolve open positions vs REAL settled outcomes
python main.py report                    # live performance: win rate, ROI, Brier-vs-market
python main.py calibrate                 # train calibration from settled outcomes
python main.py service                   # unattended scheduler (cycle + daily settle/calibrate)
python main.py healthcheck               # liveness / error / emergency-stop status
python main.py backtest                  # Monte-Carlo metrics on the mock market set
python main.py status                    # show mode, safety flags, open positions
python -m pytest tests/ -q               # run the test suite (53 tests)
streamlit run src/dashboard.py           # web dashboard
```

### Mock mode (default)
`DATA_SOURCE=mock` reads `data/mock_markets.json` + `data/mock_weather.json`.
Deterministic, offline, and used by the tests/backtester. Start here.

### Paper trading
Paper trading is the default and is always on unless you deliberately arm live
trading. Fills are simulated at executable prices (buy@ask, sell@bid) with Kalshi
fees; realized/unrealized PnL, fees, bankroll, and drawdown are tracked and
persisted. State is rebuilt from the trade log on each run, so positions survive
restarts and the bot won't double-buy.

### Live read-only data (still paper fills)
```bash
python main.py once --data-source live
```
Pulls **real** Kalshi public markets (`KXHIGHNY`, `KXHIGHCHI`, …) and **real**
Open-Meteo ensemble forecasts — but still **paper-trades**. No API key needed for
this; it's read-only. (Verified live: base `https://api.elections.kalshi.com/trade-api/v2`.)

---

## Live trading is OFF by default

Real orders are **impossible** unless you flip *all* of these in `.env`:

```
PAPER_TRADING=false
LIVE_TRADING=true
AUTO_TRADE=true
KALSHI_API_KEY_ID=...           # your Kalshi API key id
KALSHI_PRIVATE_KEY_PATH=...     # path to your RSA private key .pem
```

Even then, more guards apply:

| Setting | Default | Effect |
|---|---|---|
| `PAPER_TRADING` | `true` | Master paper switch. **Must be false to go live.** |
| `LIVE_TRADING` | `false` | Master live switch. |
| `AUTO_TRADE` | `false` | Allow fully automated execution. |
| `CONFIRM_LIVE_TRADES` | `true` | Print each order and require typing `YES`. |
| `USE_LIMIT_ORDERS_ONLY` | `true` | Never send market orders. |
| `EMERGENCY_STOP` | `false` | Kill switch (env flag **or** create an `EMERGENCY_STOP` file). |
| `MAX_DAILY_LOSS` / `MAX_DRAWDOWN` | `0.05` / `0.20` | Halt on loss/drawdown. |

`config.is_live_trading_armed()` is the single source of truth; the dashboard and
`status` command show whether live is armed. Live orders use **idempotent**
client order IDs and are **never aggressively retried** (a failed submit stops, so
a retry can't duplicate a position). Every order is logged before and after submit.

`--mode live` on the CLI does **not** arm live trading on its own — only the `.env`
flags do.

---

## Safety settings (all configurable in `.env`)

- **Edge gates**: `ENTRY_EDGE_THRESHOLD` (0.10), `EXIT_EDGE_THRESHOLD` (0.03),
  `MAX_PLAUSIBLE_EDGE` (0.35 — edges bigger than this are treated as *model error*,
  not opportunity).
- **Sizing**: `MAX_RISK_PER_TRADE` (0.03 of bankroll), never all-in;
  optional fractional Kelly (`USE_KELLY_SIZING`, off by default, still capped).
- **Exposure caps**: per market / per category / total / max open positions.
- **Market quality**: `MAX_SPREAD` (0.08), `MIN_VOLUME` (100),
  `MIN_MODEL_CONFIDENCE` (0.60), `MIN_PRICE`/`MAX_PRICE` (0.05/0.95 — avoids the
  1¢/99¢ lottery-ticket trap), near-settlement skip.
- **Kill switches**: `MAX_DAILY_LOSS`, `MAX_DRAWDOWN`, `EMERGENCY_STOP`.

---

## Plugging in real data

### Real Kalshi / Polymarket markets
- **Kalshi (primary, already wired):** `src/kalshi_client.py`. Read-only data needs
  no auth. For live trading, set `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH`;
  the RSA-PSS request signing is implemented in `_signed_headers`. A demo
  environment exists at `https://demo-api.kalshi.co` — point `KALSHI_API_BASE` there to test.
- **Polymarket (international cities, paper):** `src/polymarket_client.py` is wired for
  **read-only data + paper trading**. Polymarket runs daily-temperature markets for global
  cities (Shanghai, Seoul, Hong Kong, Tokyo, …) as **events** under the "Daily Temperature"
  tag (id `103040`), each holding one binary Yes/No sub-market per **1°C bucket**. Run it with:
  ```bash
  DATA_SOURCE=polymarket WEATHER_CITIES=SHA,HKG,SEL python main.py once
  ```
  The client (plain `requests`, no wallet) lists the buckets; the parser converts the **°C**
  thresholds to the bot's internal °F; the rest of the pipeline (edge → signal → paper →
  `settle`/`report`) is unchanged, so it scores toward the same edge-proven gate.
  **Realism:** fills are capped to the **live CLOB order-book depth** (`/books`), and a quote
  with no resting size behind it becomes untradeable instead of "filling" at a stale price —
  so paper sizes reflect what could actually transact, not the full quoted book.
  **Live on-chain execution is intentionally NOT built** — it needs a funded Polygon wallet,
  USDC, and `web3`/`py-clob-client` signed orders, and is deferred behind the edge-proven gate
  (prove the paper edge first). Caveat: forecasts are an Open-Meteo grid point near each
  resolution station (Weather Underground / HK Observatory), not the exact station.

### Real weather data
`src/weather_client.py` defines a `WeatherProvider` interface returning forecast
*distributions*. `OpenMeteoProvider` is fully implemented (free, no key, GFS/GEFS/
ECMWF). Documented stubs are provided for `GEFS`, `ECMWF ENS`, `HRRR`, `NBM`,
`GFS`, `NWS`, and `METAR` — each notes exactly where to get the data (NOMADS,
api.weather.gov, Iowa State Mesonet). For settlement-grade accuracy, feed the
**NWS Daily Climate Report** station and (for same-day markets) live **METAR**
observations.

### Better calibration
`src/calibration.py` has real hooks (bias correction, spread inflation, Platt/
isotonic recalibration, Brier/reliability scoring) that default to **safe no-ops**
until you supply historical settled outcomes. Train these before trusting probabilities.

---

## Backtest before risking money

```bash
python main.py backtest
```

The V1 backtester runs a Monte-Carlo over the mock market set under **two truth
models**:

- **truth = MODEL** — outcomes drawn from the model's own probability. Tests the
  *mechanics* (sizing, fees, thresholds). NOT proof of edge.
- **truth = MARKET** — outcomes drawn from the market's implied probability, i.e.
  you have *no real edge*. Shows the fee/slippage bleed — usually a loss.

The gap between those columns is the point: **a real edge must come from a model
that beats the market on REAL settled outcomes.** To build a genuine historical
backtest, run the bot live read-only for a while to record market + weather +
settlement snapshots (stored in SQLite), then replay them.

---

## Test the forecast in seconds (`histbacktest`)

Instead of waiting a day per bet, score the model against **real past weather**:

```bash
python main.py histbacktest --past-days 90
```

It pulls the last ~90 days of a genuine archived **GFS forecast** (`gfs_seamless` via the
Open-Meteo Historical Forecast API) and compares it to independent **ERA5** actual highs —
two different systems, so it's a real skill test, not a source compared to itself. (Rows
where the forecast == actual, MAE < 0.1°F, are auto-flagged `ARTIFACT` and must not be
trusted.) It prints, per city: **model Brier vs climatology Brier**,
**CRPS** (model vs climatology — scores the whole predictive distribution, not just
threshold hits), mean forecast error (°F), calibration error, and a **PASS/FAIL**
verdict — in seconds.

Add `--walk-forward` to fit the bias/spread/climatology on the **first 60%** of the
days and score only the held-out remainder — an **out-of-sample** check that exposes
overfitting (a model that merely memorizes history won't beat climatology here):

```bash
python main.py histbacktest --past-days 120 --walk-forward
```

**International cities (forecast-only):** because Open-Meteo is global, you can score
forecast skill for the cities that trade on **Polymarket** (Shanghai, Seoul, Hong Kong,
Taipei, Tokyo, London, Paris, … — 38 in `CITY_REGISTRY`) by listing their codes:

```bash
WEATHER_CITIES=SHA,SEL,HKG,TPE,PAR python main.py histbacktest --past-days 90 --walk-forward
```

These are **not live-tradeable** here — Kalshi is US-only, and the Polymarket trading path
(client, non-NWS settlement, per-city observations) isn't built. This just answers "does the
model forecast these cities well?" — the prerequisite before any Polymarket build.

**What PASS means (and doesn't):** PASS = the model can forecast (beats climatology),
a necessary first filter; FAIL = the strategy is dead, and you saved weeks. It does
**NOT** prove edge vs the *market* — the market sees the same forecasts, and there's
no free archive of historical Kalshi prices, so beating the market still requires the
live forward-test below. (The archived forecast is short-range, so your real day-of-
trade error will be somewhat larger than the backtest's.)

## Does it beat the market? (`marketbacktest`)

The strongest fast test — uses **real Kalshi price history** (free, unauthenticated):

```bash
python main.py marketbacktest --past-days 45 --lead-hours 24
```

For each past *settled* weather market it lines up three real things — your **model's
probability**, the **market's price** (from Kalshi candlesticks at the decision time),
and the **actual outcome** — then reports **model Brier vs market Brier** and the
strategy's simulated **ROI on real prices**.

How to read it honestly:
- **Brier (calibration) is the reliable signal; ROI on a small, correlated sample is
  noisy.** If a city *loses* on Brier but shows positive ROI, that ROI is luck, not edge.
- It **flatters the model** (the archived forecast can know things the decision-time
  market price didn't), so a **LOSS is decisive** (no edge even with a head start) and
  a **WIN is not proof**.
- Expect results near the line and mixed across cities — that's what a near-efficient
  market looks like. Confirm any apparent edge with the live forward-test (no look-ahead).

## Autonomy, scoring & the live-trading gate

The bot can run unattended, measure itself against reality, and learn — with real
money locked behind a proven edge.

**The forward-test loop (how you find out if it actually works):**
1. `python main.py run --data-source live` (or `service`) records real markets +
   forecasts and opens paper positions.
2. `python main.py settle` resolves matured positions against the **actual** outcome
   (Kalshi's settlement result, with NWS/Mesonet station obs for calibration).
3. `python main.py report` shows realized win rate, ROI, and — the real test —
   **model Brier vs. market Brier**. Beating the market's Brier over many days is
   the bar; a few lucky settlements is not.
4. `python main.py calibrate` trains an isotonic recalibrator + per-station bias
   from settled outcomes; the next cycle's probabilities are corrected by what
   actually happened.

**Run it unattended:** `python main.py service` loops the cycle and, once a day,
settles + retrains + posts a summary. It recovers from crashing cycles and alerts
via `ALERT_WEBHOOK_URL`. See [`deploy/`](deploy/README.md) for Windows Task
Scheduler / systemd / Docker (all restart-on-exit) and the `EMERGENCY_STOP` file.

**METAR / intraday edge:** set `USE_INTRADAY=true` (live) to condition same-day highs
on live airport observations ("high-so-far"), which lets the bot trade same-day markets
it would otherwise skip. `INTRADAY_MODE=bayesian` goes further: once the afternoon peak
has passed it decays the forecast's remaining upside, collapsing the probability toward
certainty while a slower market still lags the observations (the suspected biggest live
edge). A strict decision-time cutoff prevents look-ahead.

### The edge-proven gate (why real money stays off)
Building the live machinery does **not** enable it. `config.is_live_trading_armed()`
is ANDed with a programmatic `edge_proven` check that reads your **paper** track
record. Real orders are refused until:
- ≥ `EDGE_PROVEN_MIN_TRADES` (default **150**) settled paper trades, **and**
- positive after-cost expectancy, **and**
- model Brier **beats** market Brier (`EDGE_PROVEN_REQUIRE_BRIER_BEAT`).

So even with `PAPER_TRADING=false`, `LIVE_TRADING=true`, `AUTO_TRADE=true` and keys
all set, the bot **stays in paper mode** until the numbers earn the right to trade.
This encodes the research conclusion: *measure → prove → only then risk money.*

## Tuning for profit (not win rate)

A 90%+ win rate is easy to fake (buy heavy favorites) and usually **loses money**.
The goal is profit: **after-cost ROI** and **beating the market's forecast (Brier)**.
Win rate is an *output* of skill + price selection, not a target. The levers, all
**off/neutral by default** (flip them deliberately and verify with the backtests):

**Make the forecast genuinely sharper** (the only real edge source):
- **Multi-model ensemble** — `OPENMETEO_MODELS=gfs025,ecmwf_ifs025` pools **GFS + ECMWF**
  (~82 members; ECMWF is the strongest global model). On by default.
- **NGR/EMOS spread calibration** — `USE_NGR=true` replaces the fixed `SPREAD_INFLATION`
  fudge with a distribution learned from settled outcomes (`mu'=a+b·mean`,
  `sigma²=c+d·spread²`, fit by minimizing CRPS). Fixes ensemble over-confidence; trains
  via `calibrate` once you have `NGR_MIN_PAIRS` settlements.
- **NBM blend** — `USE_NBM_BLEND=true` (live) folds the **NWS National Blend of Models**
  point Tmax (`api.weather.gov`, free) into the ensemble mean. *Caveat:* that endpoint
  has no history, so it's validated only by the live forward-test.
- **Settlement window** — the NWS "official high" uses **local standard time**;
  `SETTLEMENT_WINDOW=lst` matches it (verify per city first — default `civil` is unchanged).

**Convert sharper forecasts into wins + profit:**
- **Favorite-longshot mode** — `FAVORITE_MODE=true` takes more high-confidence favorites
  the model thinks are **underpriced** (at the lower `ENTRY_EDGE_THRESHOLD_FAVORITE` bar)
  without lowering the bar for coin-flips. This is the *legitimate* way to raise win rate
  **and** EV together. `MIN_EDGE_TO_PRICE_RATIO` keeps cheap contracts honest.
- **Confidence-scaled Kelly** — `USE_KELLY_SIZING=true` sizes by fractional Kelly, scaled
  by forecast confidence and capped (`KELLY_FRACTION`, `KELLY_FRACTION_CAP`); still inside
  `MAX_RISK_PER_TRADE`.
- **Keep more edge** — `USE_MAKER_ORDERS=true` rests on the bid at the **maker fee
  (~1.75% vs ~7% taker)**, ~4× cheaper per trade.
- **Trade only where you win** — `report` breaks ROI + Brier-vs-market down by city and
  price bucket; `SKIP_UNPROFITABLE_SEGMENTS=true` prunes cities the model loses on.

**Measure honestly:** `histbacktest` now reports **CRPS** and has a `--walk-forward`
(out-of-sample) mode to catch overfitting before you trust a fitted component.

**The optimization loop:** `run` (collect) → `settle` (resolve) → `report` (see which
segments have edge) → `calibrate` (learn bias + NGR + recalibration) → prune the losers
→ repeat. The target is **model Brier < market Brier**, not the win-rate number.

## Project layout

```
prediction_market_bot/
  main.py                  CLI: once|run|settle|report|calibrate|service|healthcheck|backtest|status
  config.py                all settings + safety gates (is_live_trading_armed + edge gate)
  research_memo.md          the skeptical research write-up
  strategy_spec.md          the strategy in full
  data/                     mock_markets.json, mock_weather.json
  deploy/                   service auto-restart: Task Scheduler / systemd / Docker
  src/
    market_client.py        Market/OrderBook + mock client + factory
    kalshi_client.py        live read-only data + auth methods (orders/fills/positions)
    polymarket_client.py    thin documented stub
    weather_client.py       ForecastDistribution + mock + live Open-Meteo + stubs
    intraday.py             METAR observations + same-day high-so-far flooring
    market_parser.py        weather-question parser (fails closed)
    probability_engine.py   distribution -> P(yes) + confidence
    calibration.py          isotonic/Platt/bias/Brier (no-ops until trained)
    calibration_trainer.py  fit + persist + load calibrators from settled outcomes
    ev_engine.py            edge after fees + slippage (ask/bid aware)
    signal_engine.py        BUY/HOLD/EXIT/DO_NOT_TRADE + reason codes
    risk_manager.py         sizing + all limits (final authority)
    position_manager.py     cash/positions/PnL accounting (replayable)
    paper_trader.py         realistic simulated fills
    live_trader.py          guarded live orders (off by default)
    live_gate.py            edge-proven gate (real money locked until proven)
    reconciler.py           sync positions/balance from the exchange (live)
    settlement.py           resolve real outcomes (Kalshi result + station obs)
    scorer.py               settle open positions + record (model_p, market_p, outcome)
    performance.py          win rate / ROI / Brier-vs-market
    execution_engine.py     the main loop
    service.py              unattended crash-recovering scheduler
    notify.py               webhook alerts + daily summary
    database.py             SQLite persistence
    backtester.py           Monte-Carlo metrics
    dashboard.py            Streamlit UI
    utils.py                logging/time/math helpers
  tests/                    pytest suite (53 tests)
```

## Disclaimer
Educational software. Prediction-market trading involves real financial risk and
is regulated; you are responsible for compliance and for any losses. Nothing here
is financial advice, and nothing here guarantees profit.
