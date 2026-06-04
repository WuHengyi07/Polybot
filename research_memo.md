# Research Memo — Weather Prediction-Market Trading

**Date:** 2026-06-02 · **Purpose:** Study weather/prediction-market trading
strategies skeptically, separate real edge from noise/marketing, and define a
safer, testable strategy for an automated bot.

> **Bottom line up front:** There is a plausible, *small* edge in pricing Kalshi
> daily-temperature contracts off weather ensembles **if** your model is better
> calibrated than the market and you respect fees, liquidity, and settlement
> rules. Almost none of the headline Reddit claims can be verified, and they
> exhibit every classic red flag. Build for **survival and calibration first**,
> not for the big numbers in the screenshots.

---

## 0. Source access honesty note

**14 of the 15 provided links are Reddit, and every Reddit thread was
inaccessible from this network** — `www.reddit.com`, `old.reddit.com`, and the
`.json` API all return *"You've been blocked by network security"* (an IP-level
block, confirmed via WebFetch, `curl`, and a real Chromium browser). This is **not
a login wall**, so it was not bypassed (per instructions and site rules). Post
*existence* and *titles* were confirmed via the **Pholder** mirror and
**DuckDuckGo** (e.g. source #1 is real), but full thread bodies — the actual
evidence — are unreadable. The single non-Reddit source (mcinerney.ai) was fully read.

**Consequence:** every Reddit claim below is treated as an **unverified anecdote**.
Where a field is unknown because the thread is blocked, it says so. Load-bearing
facts come from primary technical sources (Kalshi docs, NOAA/NWS, Open-Meteo,
academic literature), cited in §3.

---

## 1. Skepticism framework

Each source is checked for:

- **Survivorship bias** — "best bot out of 500 backtests" = the max of many noisy
  configs; looks great in-sample, decays live.
- **Win-rate illusion** — a 90–98% win rate almost always means selling deep
  favorites (~$0.90+): tiny edge per trade, fat catastrophic tail. **Win rate ≠ profit.**
- **Realized vs unrealized** — screenshots of open positions / paper PnL are not
  withdrawn money.
- **Overbetting** — 11×–49× account "runs" imply massive leverage/variance, not edge.
- **Liquidity/slippage** — can you actually fill size at the shown price on thin
  weather books? Mid-price overstates achievable edge.
- **Marketing** — is the poster selling a course / Discord / "prediction engine" /
  bot subscription / referral? If so, the post is an ad.

---

## 2. Source-by-source analysis

### 2.0 Verification status

| # | Source | Readable? |
|---|--------|-----------|
| 1 | r/PredictionsMarkets `1tko1iw` — "I backtested 500 Weather Kalshi Bots. The best bot was the simplest" | Title only (confirmed REAL via mirror) |
| 2 | r/PredictionsMarkets `1togyks` — "I spent a year figuring out how to predict daily…" | Title only |
| 3 | r/PredictionsMarkets `1ttomwg` — "This synthesis trader made $23,000 solely from…" | Title only |
| 4 | r/PredictionsMarkets `1tn0kbr` — "Over 1000 trades with a 98% win rate" | Title only |
| 5 | r/PredictionsMarkets `1tqeu9y` — "A new wallet turned $500 into $5854…" | Title only |
| 6 | r/passive_income `1sww5nv` — "Trading weather on Kalshi" | Title only |
| 7 | r/PredictionMarkets `1sae7j8` — "I built a weather peak temp analyzer" | Title only |
| 8 | r/PredictionMarkets weather **search** link | Inaccessible (cannot enumerate) |
| 9 | r/PredictionsMarkets `1tp8sko` — "5 beginner mistakes…" | Title + comparable public guides |
| 10 | r/PredictionsMarkets `1qcorty` — "Trader who turned $100 into $4923…" | Title only |
| 11 | r/PredictionsMarkets `1s260mb` — "Kalshi weather edge **prediction engine** w/ up to …%" | Title only (a product) |
| 12 | r/PredictionsMarkets `1rxxte0` — "Kalshi weather contracts: the complete 2026 guide" | Title + Kalshi Help/RotoWire |
| 13 | r/PredictionsMarkets `1s4n4wp` — "What are the best strategies you've seen or used" | Title + comparable guides |
| 14 | r/PredictionsMarkets `1s4k6co` — "$85k in profit from trading weather with a 90% win" | Title only |
| 15 | **mcinerney.ai** — "How I botted $6k prediction markets as I slept" | **Fully read** |

### 2.1 Detailed fields (16 attributes per source)

**#1 — "I backtested 500 Weather Kalshi Bots. The best bot was the simplest." (CONFIRMED REAL)**
- *Strategy:* brute-force search over ~500 bot configs; reports the simplest as best.
- *Market type:* Kalshi weather (temperature). *Claimed PnL / win% / #trades:* unknown (blocked).
- *Data sources / entry / exit / sizing / horizon:* unknown (blocked).
- *Realized?* By definition a **backtest = simulated/unrealized**.
- *Red flags:* textbook **survivorship/overfitting** ("best of 500"); backtest ≠ live fills; possible funnel.
- *Useful:* its own headline lesson — **simplicity beats complexity; beware overfit.** (Our V1 is deliberately simple.)
- *Don't copy:* treating "the winning backtest bot" as robust live.

**#2 — "I spent a year figuring out how to predict daily [temps]"**
- *Strategy:* a year-long temperature-prediction methodology. *All numeric fields:* unknown (blocked).
- *Red flags:* likely educational/funnel; effort-signaling ("a year") ≠ evidence.
- *Useful:* concept that daily temp is predictable enough to model. *Don't copy:* anything unstated/unverified.

**#3 — "This synthesis trader made $23,000 solely from [weather/synthesis]"**
- *Strategy:* "synthesis" = blending multiple models. *PnL:* $23,000 claimed. *win%/#trades:* unknown.
- *Realized?* unknown — third-person promo phrasing ("this trader") suggests a showcase.
- *Red flags:* third-person ad pattern, likely **unrealized/screenshot**, possible signal-service funnel.
- *Useful:* model-ensembling (synthesis) is sound *in principle.* *Don't copy:* the $23k figure.

**#4 — "Over 1000 trades with a 98% win rate"**
- *Strategy:* unknown. *Claimed:* 98% win rate over 1000+ trades.
- *Red flags:* **win-rate illusion** — 98% wins ⇒ selling ~$0.95 favorites; a handful of the 2% losses wipe out many wins. Win rate is the wrong metric; ROI net of fees is what matters.
- *Useful:* a reminder to **report ROI, not win rate.** *Don't copy:* favorite-selling without tail-risk accounting.

**#5 — "A new wallet turned $500 into $5854" / #10 — "$100 into $4923"**
- *Strategy:* unknown (Polymarket on-chain "wallet" framing for #5). *Claimed:* 11.7× and 49×.
- *Red flags:* **survivorship** (one lucky wallet among thousands) + **massive overbetting/variance**; a single hot streak is not a strategy.
- *Useful:* nothing reliable. *Don't copy:* the implied leverage/all-in behavior.

**#6 — "Trading weather on Kalshi" (r/passive_income)**
- *Red flags:* "passive income" framing for active trading; likely intro/funnel. *Useful:* low.

**#7 — "I built a weather peak temp analyzer"**
- *Strategy:* a peak-temperature analyzer tool — conceptually **our probability engine.**
- *Red flags:* possibly a product. *Useful:* validates the "ensemble → peak-temp probability" approach. *Don't copy:* unverified accuracy claims.

**#9 — "5 beginner mistakes new traders make"** (themes recovered from comparable public guides)
- Mistakes: trading on emotion; **ignoring fees (worst in the 40–60¢ band)**; buying cheap 5–20¢ "lottery" contracts; trading **illiquid/wide-spread** markets; leaving cash idle.
- *Useful:* directly drives our **risk manager** (fee model, `MIN_PRICE`/`MAX_PRICE`, `MAX_SPREAD`, `MIN_VOLUME`).

**#11 — "Kalshi weather edge prediction engine w/ up to X%"**
- *Strategy:* a packaged **"prediction engine"** — i.e. **a product.** *Claimed %:* ignore.
- *Red flags:* marketing funnel; "up to X%" is a sales figure. *Useful:* only as confirmation that others build edge engines. *Don't copy:* the claim.

**#12 — "Kalshi weather contracts: the complete 2026 guide"** (mechanics verified vs Kalshi Help/RotoWire)
- *Facts:* settles on **NWS Daily Climate Report**; daily window in **Local Standard Time** (DST-shifted); **bracketed** temperature markets; CFTC-regulated; NYC/Chicago/LA liquid.
- *Useful:* the **factual backbone** (verify against Kalshi docs). *Don't copy:* assume it's also a funnel for something.

**#13 — "What are the best strategies you've seen or used"** (themes from comparable guides)
- Generic: early-entry/early-exit, fade overreactions, correlated pairs, prioritize liquidity. *Useful:* mild; not weather-specific.

**#14 — "$85k in profit from trading weather with a 90% win rate"**
- *Claimed:* $85k, 90% win. *Realized?* unknown. *Red flags:* biggest brag; same favorite-selling + unrealized? + promo flags as #4.
- *Useful:* nothing verifiable. *Don't copy:* the numbers.

**#15 — mcinerney.ai, "How I botted $6k prediction markets as I slept" (FULLY READ — highest value)**
- *Strategy:* systematic **miscalibration detection** on Kalshi **"mention" markets** (predicting words spoken at events) — **NOT weather**.
- *Market type:* Kalshi mention markets. *PnL:* **realized net ≈ $5,515.94.** *#trades:* 15,277 fills / 2,717 settled markets. *Win%:* not stated (high settlement-rate strategy).
- *Data:* custom scraper + Kalshi L1 (top-of-book) data.
- *Entry:* NO-side calibration edge; sweet spot **$0.20–$0.85**; strongest **24–30h before** the event.
- *Exit:* close **before** the event to avoid information leaks (early speech releases, contestant leaks).
- *Sizing:* laddered limit orders, small per-position; **maker (1.75% fee) vs taker (7%)** discipline.
- *Realized?* **Yes**, but net was eaten by **state tax on total volume** (~80% of profit), plus **edge decay** as liquidity improved, plus **regulatory uncertainty**.
- *Useful (a lot):* the realistic framework — **modest realized PnL, fee discipline, exit-before-tail-risk, calibration as the true edge, infra > forecast genius.**
- *Don't copy:* assuming it transfers to weather unchanged, or ignoring tax/regulatory drag.

### 2.2 Cross-source code references found (unverified, but methodologically instructive)
- **`suislanchez/polymarket-kalshi-weather-bot`** — GFS ensemble fraction vs price; fractional Kelly ×0.15 capped at 5%; 8% edge gate; **simulation-only** (places no real trades).
- **`Oalkhadra/prediction-market-trading`** — XGBoost on Kalshi temp markets; claims market-implied uncertainty exceeds realized by ~1.27× (a real, testable mispricing hypothesis); walk-forward; **selectivity = the edge**; live results undisclosed.
- Treat all metrics as unproven; the *methods* (ensemble→probability, selectivity, walk-forward validation, explicit fee modeling) are sound and we adopt them.

---

## 3. Technical backbone (primary sources — trustworthy)

- **Kalshi weather settlement:** official source = **NWS Daily Climate Report** for a
  specific station; daily window in **Local Standard Time** (DST-shifted);
  temperature markets are **bracketed** (interior ~2°F brackets + open-ended edges);
  contracts pay **$1**, priced **1–99¢**; YES/NO complementary (NO ask = 1 − YES bid).
  *(Kalshi Help Center.)*
- **Kalshi fees:** taker ≈ **0.07 × p × (1 − p)** per contract (max ~1.75¢ at 50¢, → 0
  at the extremes); maker ≈ **0.0175 × p × (1 − p)**. *(Kalshi fee schedule.)* Fees
  dominate small edges in the 40–60¢ band → must be modeled explicitly.
- **Kalshi API (verified live 2026-06-02):** base
  `https://api.elections.kalshi.com/trade-api/v2`; `GET /markets` and
  `GET /markets/{ticker}/orderbook` are **public**; auth = **RSA-PSS** over
  `timestamp+METHOD+path` for orders/portfolio; rate limits ~20 reads/s, 10 writes/s;
  demo env at `demo-api.kalshi.co`. Weather series: `KXHIGHNY`, `KXHIGHCHI`, … ;
  market ticker `KXHIGH<CITY>-<YYMMMDD>-T<thr>`; prices as `*_dollars` strings.
- **Polymarket:** CLOB at `clob.polymarket.com`, wallet-derived creds; **weak/scarce
  weather coverage** → secondary at best.
- **Weather data (free, real):** **Open-Meteo Ensemble** (`/v1/ensemble`, **no key**,
  CC-BY-4.0) exposes individual members of **GEFS (≈31 via gfs025)** and **ECMWF ENS
  (51 via ecmwf_ifs025)**; verified: GFS returns `temperature_2m` + `…_member01..30`.
  **NWS api.weather.gov** (gridpoint + station obs), **Iowa State Mesonet** (METAR/ASOS),
  **NOMADS** (raw GRIB), **NBM** (best-calibrated point temps).
- **Calibration:** ensemble → probability via (a) member fraction, (b) Normal(mean,
  inflated std) CDF. Operational ensembles are typically **under-dispersive
  (overconfident)** → apply spread inflation + station/model bias correction +
  optional isotonic/Platt recalibration; score with **Brier**. *(Academic literature.)*
- **Why bots fail:** overfitting/curve-fit, look-ahead bias, **ignoring fees+slippage**
  (mid-price illusion), overbetting, data outages, **duplicate orders / no
  idempotency**, **settlement-rule misunderstanding** (wrong station/DST window),
  regime change, regulatory/tax shocks, **mistaking unrealized paper PnL for edge.**

---

## 4. Synthesis (the 10 questions)

1. **Strategies that recur:** ensemble-forecast-vs-market-price; high-"win-rate"
   favorite-selling; packaged "edge engines"; daily high-temp on Kalshi.
2. **Most credible:** model-probability vs **executable** price with **strict
   selectivity** + explicit fee/slippage accounting (supported by mcinerney's
   calibration edge, Oalkhadra's selectivity, and the confirmed "simplest bot won").
   Plus maker-fee discipline and exit-before-tail-risk.
3. **Suspicious/overfit:** 90–98% win rates, 11×–49× account runs, "$85k/$23k"
   brags, "engine up to X%" products, "best of 500 backtests." All carry
   survivorship/overbetting/unrealized/marketing flags.
4. **Data that matters most:** the **settlement source** (exact NWS station + LST/DST
   window) above all; then ensembles (Open-Meteo GEFS+ECMWF), **NBM** for calibrated
   point temps, **METAR** for live obs, NWS API.
5. **Easiest to model:** **daily HIGH temperature** (and low) for major cities —
   bounded, ensemble-forecastable, objective settlement. Hardest: exact precip/snow
   thresholds and wind.
6. **Biggest beginner mistakes:** ignoring fees (40–60¢ band), illiquid/wide-spread
   markets, cheap-contract lottery tickets, overbetting, the win-rate illusion,
   misreading the settlement station/DST window, trusting unrealized PnL, overfitting.
7. **Biggest automation risks:** overfitting/look-ahead, fee+slippage neglect,
   duplicate orders / weak idempotency, data outages, no kill switch, settlement
   misunderstanding, regime change, regulatory/tax surprises, paper-edge self-deception.
8. **Best V1 for a $100 bankroll:** **paper-trade** Kalshi daily-high-temp on 2–3
   deep-liquidity cities; ensemble→Normal-CDF probability per bracket; trade only
   when **post-fee edge ≥ 10%**, **spread ≤ 8¢**, **volume ≥ 100**, **confidence ≥
   0.6**, price in [0.05, 0.95]; **fixed 2–3% sizing**; exit on edge decay / thesis
   break; skip near settlement; daily-loss + drawdown kill switch.
9. **Weather-only or broader:** **weather-FIRST** (most modelable, objective
   settlement) with a **category-agnostic parser/engine** so other
   objective-settlement markets can plug in later. Avoid political/sentiment markets
   (poor calibration).
10. **Exact first strategy:** the **fair-value-vs-price edge** strategy in
    [`strategy_spec.md`](strategy_spec.md), on **Kalshi daily-high-temperature
    brackets**, ensemble-driven, **paper by default.**

---

## 5. Key sources
- Kalshi Help Center — Weather Markets; Kalshi fee schedule; Kalshi API docs (`docs.kalshi.com`).
- Open-Meteo Ensemble API (`open-meteo.com/en/docs/ensemble-api`); NWS `api.weather.gov`;
  Iowa State Mesonet; NOAA NOMADS; NWS NBM.
- mcinerney.ai — "How I botted $6k prediction markets as I slept" (the one fully-read, honest case study).
- Academic: Brier score decomposition / reliability diagrams; isotonic & Platt calibration; ensemble under-dispersion literature.
- Reddit sources #1–#14 — **existence/titles confirmed, full content unreadable from this network; treated as unverified anecdotes.**
