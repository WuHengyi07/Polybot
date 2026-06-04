"""Market backtest — does the model beat the KALSHI market on real history?

Unlike histbacktest (model vs climatology) and backtest (synthetic outcomes), this
lines up, for each PAST SETTLED weather market: the model's probability, the real
market PRICE (from Kalshi candlesticks), and the real OUTCOME — then asks the only
question that matters: was the model better-calibrated than the market price
(Brier model < Brier market)? It also simulates the strategy's PnL on those real
prices.

HONESTY CAVEAT (printed in the report): the archived forecast may reflect info not
available at the decision time, while the market price IS taken at the decision time
— so this FLATTERS the model. A LOSS here is decisive (no edge even with a head
start); a WIN here is promising but NOT proof. Only the live forward-test (no
look-ahead) confirms edge. Also: historical prices can be illiquid/noisy, the market
set is selected (only settled markets), and any backtest can overfit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional

from .calibration import brier_score
from .ev_engine import fee_per_contract
from .market_parser import Direction
from .utils import clamp, get_logger, normal_cdf, parse_iso, safe_mean, safe_std, utcnow

log = get_logger("market_backtester")


# --------------------------------------------------------------------------- #
# Pure scoring core (no network — unit-testable)
# --------------------------------------------------------------------------- #
@dataclass
class MarketBTResult:
    city: str
    n_rows: int = 0
    n_trades: int = 0
    brier_model: Optional[float] = None
    brier_market: Optional[float] = None
    beats_market: bool = False
    realized: float = 0.0
    roi: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    model_probs: List[float] = field(default_factory=list)
    market_probs: List[float] = field(default_factory=list)
    outcomes: List[int] = field(default_factory=list)


def score_markets(rows: List[dict], entry_threshold: float, fee_rate: float,
                  city: str = "?") -> MarketBTResult:
    """rows: [{model_p, market_p, outcome}]. Compares Brier(model) vs Brier(market)
    and simulates the strategy PnL (buy the mispriced side at the market price, hold
    to settlement, pay the fee)."""
    res = MarketBTResult(city=city, n_rows=len(rows))
    if not rows:
        return res
    pnls: List[float] = []
    stake = 0.0
    for r in rows:
        mp, kp, o = r["model_p"], r["market_p"], int(r["outcome"])
        res.model_probs.append(mp)
        res.market_probs.append(kp)
        res.outcomes.append(o)
        edge_yes = mp - kp
        if edge_yes >= entry_threshold:                 # buy YES at the market price
            a = kp
            pnls.append((o - a) - fee_per_contract(a, fee_rate))
            stake += a
        elif -edge_yes >= entry_threshold:              # buy NO at (1 - market price)
            b = 1.0 - kp
            pnls.append(((1 - o) - b) - fee_per_contract(b, fee_rate))
            stake += b

    res.brier_model = round(brier_score(res.model_probs, res.outcomes), 4)
    res.brier_market = round(brier_score(res.market_probs, res.outcomes), 4)
    res.beats_market = res.brier_model < res.brier_market
    res.n_trades = len(pnls)
    if pnls:
        res.realized = round(sum(pnls), 2)
        res.roi = round(sum(pnls) / stake, 4) if stake > 1e-9 else 0.0
        res.win_rate = round(sum(1 for p in pnls if p > 0) / len(pnls), 4)
        gw = sum(p for p in pnls if p > 0)
        gl = -sum(p for p in pnls if p < 0)
        res.profit_factor = round(gw / gl, 3) if gl > 1e-9 else float("inf")
    return res


def model_prob_directional(mean: float, sigma: float, parsed) -> float:
    """Model P(outcome=YES) for an above/below/between market via Normal(mean, sigma)."""
    if parsed.direction == Direction.ABOVE:
        return clamp(1.0 - normal_cdf(parsed.threshold, mean, sigma), 0.001, 0.999)
    if parsed.direction == Direction.BELOW:
        return clamp(normal_cdf(parsed.threshold, mean, sigma), 0.001, 0.999)
    if parsed.direction == Direction.BETWEEN and parsed.threshold_high is not None:
        return clamp(normal_cdf(parsed.threshold_high, mean, sigma)
                     - normal_cdf(parsed.threshold, mean, sigma), 0.001, 0.999)
    return 0.5


# --------------------------------------------------------------------------- #
# Network fetch
# --------------------------------------------------------------------------- #
def fetch_settled_markets(client, series: str, lookback_days: int, max_markets: int = 250) -> List[dict]:
    cutoff = utcnow() - timedelta(days=lookback_days)
    out: List[dict] = []
    cursor = None
    for _ in range(12):  # page cap
        params = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = client._get("/markets", params)
        for raw in data.get("markets", []):
            ct = parse_iso(raw.get("close_time"))
            if ct and ct < cutoff:
                continue
            if str(raw.get("result", "")).lower() in ("yes", "no"):
                out.append(raw)
        cursor = data.get("cursor")
        if not cursor or len(out) >= max_markets:
            break
    return out[:max_markets]


def market_implied_at(client, series: str, ticker: str, decision_ts: int,
                      window_hours: int = 18, interval: int = 60):
    """Market-implied YES probability (price in $) nearest the decision time, + volume."""
    try:
        data = client._get(f"/series/{series}/markets/{ticker}/candlesticks",
                           {"start_ts": decision_ts - window_hours * 3600,
                            "end_ts": decision_ts + 3600, "period_interval": interval})
    except Exception:  # pragma: no cover - network dependent
        return None, 0
    best_price, best_dt, vol = None, None, 0
    for c in data.get("candlesticks", []):
        ts = c.get("end_period_ts")
        price = c.get("price") or {}
        pv = price.get("mean_dollars") or price.get("close_dollars")
        if ts is None or pv in (None, ""):
            continue
        d = abs(ts - decision_ts)
        if best_dt is None or d < best_dt:
            best_dt, best_price = d, float(pv)
            vol = int(float(c.get("volume", c.get("open_interest_fp", 0)) or 0))
    return best_price, vol


def run_market_backtest(config, cities: Optional[List[str]] = None, lookback_days: int = 60,
                        lead_hours: int = 24, entry_threshold: Optional[float] = None,
                        min_volume: int = 0, max_markets_per_city: int = 250) -> dict:
    from .historical_backtester import fetch_city_history
    from .kalshi_client import KalshiClient
    from .market_client import Market  # noqa: F401  (kept for clarity)
    from .market_parser import CITY_REGISTRY, parse_market, series_ticker_for_city

    client = KalshiClient(config)
    et = entry_threshold if entry_threshold is not None else config.entry_edge_threshold
    cities = cities or config.weather_cities
    per_city: Dict[str, MarketBTResult] = {}

    for code in cities:
        city = CITY_REGISTRY.get(code.upper())
        series = series_ticker_for_city(code)
        if not city or not series:
            continue
        try:
            forecasts, actuals = fetch_city_history(city, past_days=lookback_days)
        except Exception as exc:  # pragma: no cover
            log.warning("Forecast history failed for %s: %s", code, exc)
            continue
        common = [d for d in forecasts if d in actuals and forecasts[d]]
        if not common:
            continue
        points = {d: safe_mean(forecasts[d]) for d in common}
        resid = [points[d] - actuals[d] for d in common]
        bias = safe_mean(resid)
        sigma = max(1.5, safe_std(resid))
        corrected = {d: points[d] - bias for d in common}

        try:
            raws = fetch_settled_markets(client, series, lookback_days, max_markets_per_city)
        except Exception as exc:  # pragma: no cover
            log.warning("Settled markets fetch failed for %s: %s", code, exc)
            continue

        rows: List[dict] = []
        for raw in raws:
            parsed = parse_market(KalshiClient._market_from_kalshi(raw))
            if parsed.target_date is None or parsed.threshold is None:
                continue
            cf = corrected.get(parsed.target_date)
            if cf is None:
                continue
            ct = parse_iso(raw.get("close_time"))
            if not ct:
                continue
            mp, vol = market_implied_at(client, series, raw.get("ticker", ""),
                                        int(ct.timestamp()) - lead_hours * 3600)
            if mp is None or vol < min_volume:
                continue
            rows.append({"model_p": model_prob_directional(cf, sigma, parsed),
                         "market_p": clamp(mp, 0.001, 0.999),
                         "outcome": 1 if str(raw.get("result")).lower() == "yes" else 0})
        per_city[code] = score_markets(rows, et, config.fee_rate, city=code)
        log.info("%s: scored %d markets (%d trades)", code, per_city[code].n_rows, per_city[code].n_trades)

    overall = score_markets(
        [{"model_p": p, "market_p": k, "outcome": o}
         for r in per_city.values() for p, k, o in zip(r.model_probs, r.market_probs, r.outcomes)],
        et, config.fee_rate, city="OVERALL")
    return {"per_city": per_city, "overall": overall, "lookback_days": lookback_days, "lead_hours": lead_hours}


def format_report(result: dict) -> str:
    lines = ["", "=" * 80,
             f"  MARKET BACKTEST vs Kalshi (last {result['lookback_days']}d, "
             f"decision {result['lead_hours']}h before close)", "=" * 80]
    lines.append(f"  {'city':<8}{'mkts':>6}{'trades':>8}{'mBrier':>9}{'mktBrier':>10}{'ROI':>9}{'win':>7}  verdict")
    lines.append("  " + "-" * 76)

    def row(r: MarketBTResult):
        if r.brier_model is None or r.n_rows == 0:
            lines.append(f"  {r.city:<8}{'no data':>6}")
            return
        verdict = "BEATS market" if r.beats_market else "LOSES to market"
        lines.append(f"  {r.city:<8}{r.n_rows:>6}{r.n_trades:>8}{r.brier_model:>9.3f}"
                     f"{r.brier_market:>10.3f}{r.roi*100:>8.1f}%{r.win_rate*100:>6.0f}%  {verdict}")

    for code in sorted(result["per_city"]):
        row(result["per_city"][code])
    lines.append("  " + "-" * 76)
    row(result["overall"])
    ov = result["overall"]
    lines.append("")
    if ov.brier_model is not None and ov.n_rows:
        if ov.beats_market:
            lines.append("  Model beats the market's Brier here — PROMISING, but NOT proof (see caveat).")
        else:
            lines.append("  Model LOSES to the market's Brier — strong evidence of NO edge.")
    lines.append("  !! This FLATTERS the model: the archived forecast may use info not available")
    lines.append("     at decision time, while the market price is taken at decision time. A loss is")
    lines.append("     decisive; a win must still be confirmed by the live forward-test (no look-ahead).")
    lines.append("=" * 80)
    return "\n".join(lines)
