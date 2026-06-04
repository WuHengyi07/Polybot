# Strategy Specification — Fair Value vs. Market Price

A selective, fee-aware, capped, **paper-first** strategy for Kalshi daily
high-temperature markets. Designed as a **beginner quant project, not a gambling
bot** (see [`research_memo.md`](research_memo.md) for why the "big number" Reddit
strategies are not copied).

---

## 1. Core idea

Estimate a **model probability** that the event resolves YES from a weather
forecast **ensemble**, and trade only when it diverges enough from the
**executable** market price to overcome fees and slippage.

Prices are in dollars (0–1). Each contract settles to **$1** (win) or **$0** (lose).
`p` = `model_probability_yes`.

```
fee(price) = FEE_RATE * price * (1 - price)         # Kalshi taker, FEE_RATE ≈ 0.07

# ENTRY edge uses the ASK (what you actually pay to buy):
edge_yes_gross = p           - yes_ask
edge_no_gross  = (1 - p)     - no_ask
edge_yes_net   = edge_yes_gross - fee(yes_ask) - SLIPPAGE_BUFFER
edge_no_net    = edge_no_gross  - fee(no_ask)  - SLIPPAGE_BUFFER
EV_per_contract = edge_*_net                         # expected $ profit per $1 contract

# EXIT remaining edge uses the BID (what you actually get to sell):
remaining_edge_yes = p - yes_bid
remaining_edge_no  = (1 - p) - no_bid
```

> **Why ask/bid and not mid?** The "mid-price illusion" is a top reason paper
> edges evaporate live. You buy at the ask and sell at the bid; the strategy is
> always evaluated against those.

---

## 2. Model probability

From a forecast distribution (ensemble of per-member daily highs):

1. **Empirical:** fraction of members satisfying the event
   (`> threshold`, `< threshold`, or `in [lo, hi]`).
2. **Parametric:** fit `Normal(mean, std)` to the ensemble and integrate over the
   threshold/bracket via the CDF. `std` is inflated by `SPREAD_INFLATION` (default
   1.15) because raw ensembles are typically **under-dispersive (overconfident)**.
3. **Blend:** `p = 0.5 * empirical + 0.5 * parametric`, optionally recalibrated
   (Platt/isotonic) once real settled outcomes exist.

**Confidence (0–1):** a heuristic from ensemble size, forecast horizon, and spread
sanity. Trades require `confidence ≥ MIN_MODEL_CONFIDENCE`. (Documented placeholder;
replace with a data-driven score after collecting verification history.)

**Calibration hooks (default no-op):** station/model bias correction, seasonal &
horizon adjustments, recent-observation nudge, Platt/isotonic recalibration, Brier
scoring. They will not distort probabilities until you supply data.

---

## 3. Entry rules

Enter `BUY_YES` if `edge_yes_net ≥ ENTRY_EDGE_THRESHOLD`; `BUY_NO` if
`edge_no_net ≥ ENTRY_EDGE_THRESHOLD`. **Do not trade** (emit `DO_NOT_TRADE`/`HOLD`)
when **any** of these hold:

- Spread `> MAX_SPREAD` (illiquid/wide).
- `volume < MIN_VOLUME` (low liquidity).
- Settlement rules unclear/non-NWS or the question can't be parsed (**fail closed**).
- `confidence < MIN_MODEL_CONFIDENCE`.
- Within `MIN_HOURS_TO_CLOSE` of settlement and not `ALLOW_NEAR_SETTLEMENT`.
- Executable price outside `[MIN_PRICE, MAX_PRICE]` (avoids the 1¢/99¢ lottery trap).
- **Edge `> MAX_PLAUSIBLE_EDGE`** → treated as **model error**, not opportunity
  (a huge gap in a liquid market means the model is wrong or a settlement nuance is
  unmodeled).
- Daily-loss limit hit, max open positions reached, exposure cap reached, or the
  emergency stop / kill switch is engaged.

---

## 4. Exit rules

For an open position, exit (`EXIT`) when:

- `remaining_edge ≤ EXIT_EDGE_THRESHOLD` (price has converged to fair value), **or**
- `remaining_edge < 0` (model has moved against the position — thesis invalidated), **or**
- new weather data changes the probability against the position, **or**
- spread/liquidity deteriorates (get out while you can), **or**
- within `MIN_HOURS_TO_CLOSE` of settlement and not allowed to hold through it
  (reduce holding risk), **or**
- a stop-loss / max-drawdown rule triggers.

Otherwise `HOLD` (edge intact). Exits sell at the **bid**.

---

## 5. Position sizing

- Bankroll starts at **$100 (paper)**. For a long binary, **max loss = the stake**
  (`contracts × entry_price`).
- Default: **fixed fractional**. `risk_$ = MAX_RISK_PER_TRADE × bankroll`
  (default 3%; band 2–5%); `contracts = floor(risk_$ / entry_price)`.
- **Never all-in.** Cap deployed capital by **market** (`MAX_MARKET_EXPOSURE`),
  **category** (`MAX_CATEGORY_EXPOSURE`), and **total** (`MAX_TOTAL_EXPOSURE`).
- **Liquidity realism:** never take more than ~10% of stated volume, nor more than
  the resting ask depth in the order book.
- **Fractional Kelly is opt-in research only** (`USE_KELLY_SIZING`):
  `f* = (p − price)/(1 − price)`, scaled by `KELLY_FRACTION`, **still capped** by
  `MAX_RISK_PER_TRADE`. Never the default sizer.
- **Kill switches:** `MAX_DAILY_LOSS`, `MAX_DRAWDOWN`, `EMERGENCY_STOP`.

---

## 6. Signals & reason codes

Every decision emits one of `BUY_YES | BUY_NO | HOLD | EXIT | DO_NOT_TRADE` with
reason codes, e.g. `EDGE_YES`, `EDGE_NO`, `EDGE_BELOW_THRESHOLD`, `SPREAD_TOO_WIDE`,
`LOW_LIQUIDITY`, `LOW_CONFIDENCE`, `UNCLEAR_SETTLEMENT`, `UNPARSEABLE`,
`NO_FORECAST`, `NEAR_SETTLEMENT`, `PRICE_OUT_OF_RANGE`, `SUSPICIOUS_EDGE`,
`EDGE_INTACT`, `EDGE_DECAYED`, `THESIS_INVALIDATED`, `LIQUIDITY_DETERIORATED`. The
risk manager adds: `APPROVED`, `KILL_SWITCH`, `DAILY_LOSS_LIMIT`, `MAX_DRAWDOWN`,
`MAX_OPEN_POSITIONS`, `MARKET/CATEGORY/TOTAL_EXPOSURE_CAP`, `MIN_LIQUIDITY`,
`MAX_SPREAD`, `MIN_CONFIDENCE`, `SETTLEMENT_RISK`, `INSUFFICIENT_SIZE`.

---

## 7. Config defaults

| Setting | Default | | Setting | Default |
|---|---|---|---|---|
| `STARTING_BANKROLL` | 100 | | `MAX_OPEN_POSITIONS` | 5 |
| `ENTRY_EDGE_THRESHOLD` | 0.10 | | `MAX_MARKET_EXPOSURE` | 0.10 |
| `EXIT_EDGE_THRESHOLD` | 0.03 | | `MAX_CATEGORY_EXPOSURE` | 0.50 |
| `MAX_PLAUSIBLE_EDGE` | 0.35 | | `MAX_TOTAL_EXPOSURE` | 0.60 |
| `FEE_RATE` | 0.07 | | `MAX_SPREAD` | 0.08 |
| `SLIPPAGE_BUFFER` | 0.01 | | `MIN_VOLUME` | 100 |
| `SPREAD_INFLATION` | 1.15 | | `MIN_MODEL_CONFIDENCE` | 0.60 |
| `MAX_RISK_PER_TRADE` | 0.03 | | `MIN_PRICE` / `MAX_PRICE` | 0.05 / 0.95 |
| `MAX_DAILY_LOSS` | 0.05 | | `ALLOW_NEAR_SETTLEMENT` | false |
| `MAX_DRAWDOWN` | 0.20 | | `MIN_HOURS_TO_CLOSE` | 2 |

**Safety:** `PAPER_TRADING=true`, `LIVE_TRADING=false`, `AUTO_TRADE=false`,
`CONFIRM_LIVE_TRADES=true`, `USE_LIMIT_ORDERS_ONLY=true`, `EMERGENCY_STOP=false`.

---

## 8. What makes this *not* a gambling bot

- **Selective:** most markets get `DO_NOT_TRADE` / `HOLD`. Selectivity is itself the edge.
- **Fee-/slippage-aware:** edge is measured net of costs, against executable prices.
- **Capped:** per-trade, per-market, per-category, total, daily-loss, drawdown.
- **Humble about edge:** absurd edges are flagged as model error, not free money.
  The backtester explicitly shows the fee bleed you suffer when you have *no* real edge.
- **Paper-first & reversible:** live trading requires several deliberate flags plus
  keys, a confirmation gate, idempotent orders, no aggressive retries, and a kill switch.
