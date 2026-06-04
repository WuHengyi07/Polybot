"""Central configuration for prediction_market_bot.

Loads settings from environment variables (optionally via a .env file) and
exposes a single immutable ``Config`` object. Every default is SAFE: the bot
paper-trades on mock data and cannot place a real order unless several flags
are deliberately flipped (see ``is_live_trading_armed``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import List

# python-dotenv is optional: env vars still work without it.
try:  # pragma: no cover - trivial import guard
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False


def _strip_inline_comment(value):
    """Drop an accidental inline ' #...' comment (and surrounding whitespace) from an env
    value. python-dotenv versions differ on stripping these, and copying .env.example with
    trailing '# comment' on a value line otherwise leaks the comment into the value (it broke
    the Open-Meteo model list + webhook URL live). Splits on ' #' (space-hash) only, so a
    value legitimately containing '#' with no leading space is untouched."""
    if value is None:
        return None
    return value.split(" #", 1)[0].strip()


def _as_bool(value: str | bool, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Config:
    """All tunable settings. Construct with :meth:`from_env`."""

    # --- mode ---
    data_source: str = "mock"  # "mock" | "live" (Kalshi) | "polymarket" (read-only, paper)

    # --- bankroll & strategy thresholds ---
    starting_bankroll: float = 100.0
    entry_edge_threshold: float = 0.10
    exit_edge_threshold: float = 0.03
    # An "edge" larger than this is treated as a red flag (model miscalibration or
    # an unmodeled settlement nuance), NOT free money. Real edges in liquid
    # markets are small; a huge gap usually means the model is wrong.
    max_plausible_edge: float = 0.35
    fee_rate: float = 0.07
    slippage_buffer: float = 0.01
    spread_inflation: float = 1.15
    # NGR/EMOS spread calibration learned from settled outcomes (data-driven replacement
    # for the fixed spread_inflation). OFF until fitted on >= ngr_min_pairs settlements.
    use_ngr: bool = False
    ngr_min_pairs: int = 80
    # The NWS "official daily high" is measured over a local-STANDARD-time 24h window.
    # "civil" groups the forecast by local clock time (default, unchanged); "lst" uses
    # the standard-time window. Verify per city against settled outcomes before flipping.
    settlement_window: str = "civil"
    # Maker orders rest on the bid side at the maker fee (~4x cheaper than taker),
    # raising net edge per trade — at the cost of not always getting filled.
    use_maker_orders: bool = False
    maker_fee_rate: float = 0.0175
    maker_tick: float = 0.01
    maker_fill_prob: float = 0.5

    # --- position sizing & risk ---
    max_risk_per_trade: float = 0.03
    max_daily_loss: float = 0.05
    max_drawdown: float = 0.20
    max_open_positions: int = 5
    max_market_exposure: float = 0.10
    max_category_exposure: float = 0.50
    max_total_exposure: float = 0.60

    # --- trade-quality gates ---
    max_spread: float = 0.08
    min_volume: int = 100
    min_model_confidence: float = 0.60
    allow_near_settlement: bool = False
    min_hours_to_close: float = 2.0
    # Avoid cheap lottery tickets / near-certain contracts: only BUY when the
    # executable price is within [min_price, max_price]. Blocks the 1¢/99¢ trap.
    min_price: float = 0.05
    max_price: float = 0.95
    use_intraday: bool = False  # condition same-day highs on live METAR observations
    # "floor" (default) clamps members up to the observed high; "bayesian" also decays the
    # forecast upside after the daily peak (collapses to near-certainty late in the day).
    intraday_mode: str = "floor"
    intraday_peak_hour: int = 15            # local hour of the typical daily high
    intraday_peak_decay_hours: float = 4.0  # hours after the peak for upside to vanish
    skip_unprofitable_segments: bool = False  # don't trade cities where model loses to market
    segment_min_samples: int = 20             # min settled trades before pruning a segment

    # --- favorite-longshot selection (raise win rate + EV the legitimate way) ---
    # Take more high-confidence favorites the model thinks are UNDERPRICED, at a
    # slightly lower edge bar — without lowering the bar for coin-flips. OFF by default.
    favorite_mode: bool = False
    favorite_p_floor: float = 0.65            # a "favorite" is a side the model gives >= this
    entry_edge_threshold_favorite: float = 0.08  # lower edge bar for underpriced favorites
    # A cheap contract must clear this net-edge-to-price RATIO (flat costs eat small bets).
    # 0.0 disables the guard (default).
    min_edge_to_price_ratio: float = 0.0

    # --- fractional Kelly (research only) ---
    use_kelly_sizing: bool = False
    kelly_fraction: float = 0.25
    kelly_fraction_cap: float = 0.5          # hard cap on the Kelly fraction (anti-blowup)
    kelly_confidence_scaled: bool = True     # scale the bet down by forecast confidence

    # --- live trading safety ---
    paper_trading: bool = True
    live_trading: bool = False
    auto_trade: bool = False
    confirm_live_trades: bool = True
    use_limit_orders_only: bool = True
    emergency_stop: bool = False

    # --- Polymarket API (read-only data for paper trading; no wallet/keys) ---
    polymarket_gamma_base: str = "https://gamma-api.polymarket.com"
    polymarket_clob_base: str = "https://clob.polymarket.com"

    # --- Kalshi API ---
    kalshi_api_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""

    # --- Edge-proven gate (programmatic precondition for ANY live order) ---
    edge_proven_min_trades: int = 150        # settled paper trades required
    edge_proven_require_brier_beat: bool = True  # model Brier must beat market Brier

    # --- markets ---
    weather_cities: List[str] = field(default_factory=lambda: ["NYC", "CHI"])
    # Weather models to pool (Open-Meteo). GFS + ECMWF = ~82 members; ECMWF is the
    # strongest global model, so pooling improves forecast skill vs GFS alone.
    openmeteo_models: str = "gfs025,ecmwf_ifs025"
    # Deterministic model for histbacktest's archived forecast. MUST be a real forecast
    # model (e.g. gfs_seamless), NOT the default best_match, which returns ERA5 reanalysis
    # for past dates and would make the forecast == the ERA5 "actual" (circular).
    histbacktest_forecast_model: str = "gfs_seamless"
    # Blend the NWS National Blend of Models point Tmax (api.weather.gov, free) into the
    # ensemble mean. OFF by default; only validated by the live forward-test (no history).
    use_nbm_blend: bool = False
    nbm_blend_weight: float = 0.5

    # --- engine ---
    loop_interval_seconds: int = 300
    db_path: str = "prediction_market_bot.db"
    log_level: str = "INFO"
    emergency_stop_file: str = "EMERGENCY_STOP"
    alert_webhook_url: str = ""  # Discord/Slack webhook for unattended alerts

    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(cls, env_path: str | os.PathLike | None = None) -> "Config":
        """Build a Config from the process environment (loading .env first)."""
        if env_path is not None:
            load_dotenv(env_path)
        else:
            # Load a .env sitting next to this file, if present.
            default_env = Path(__file__).with_name(".env")
            load_dotenv(default_env if default_env.exists() else None)

        # Read env vars with any accidental inline '# comment' stripped (see helper above).
        g = lambda key, default=None: _strip_inline_comment(os.environ.get(key, default))  # noqa: E731
        cities = [c.strip().upper() for c in g("WEATHER_CITIES", "NYC,CHI").split(",") if c.strip()]
        return cls(
            data_source=g("DATA_SOURCE", "mock").strip().lower(),
            starting_bankroll=_as_float(g("STARTING_BANKROLL"), 100.0),
            entry_edge_threshold=_as_float(g("ENTRY_EDGE_THRESHOLD"), 0.10),
            exit_edge_threshold=_as_float(g("EXIT_EDGE_THRESHOLD"), 0.03),
            max_plausible_edge=_as_float(g("MAX_PLAUSIBLE_EDGE"), 0.35),
            fee_rate=_as_float(g("FEE_RATE"), 0.07),
            slippage_buffer=_as_float(g("SLIPPAGE_BUFFER"), 0.01),
            spread_inflation=_as_float(g("SPREAD_INFLATION"), 1.15),
            use_ngr=_as_bool(g("USE_NGR"), False),
            ngr_min_pairs=_as_int(g("NGR_MIN_PAIRS"), 80),
            settlement_window=g("SETTLEMENT_WINDOW", "civil").strip().lower(),
            use_maker_orders=_as_bool(g("USE_MAKER_ORDERS"), False),
            maker_fee_rate=_as_float(g("MAKER_FEE_RATE"), 0.0175),
            maker_tick=_as_float(g("MAKER_TICK"), 0.01),
            maker_fill_prob=_as_float(g("MAKER_FILL_PROB"), 0.5),
            max_risk_per_trade=_as_float(g("MAX_RISK_PER_TRADE"), 0.03),
            max_daily_loss=_as_float(g("MAX_DAILY_LOSS"), 0.05),
            max_drawdown=_as_float(g("MAX_DRAWDOWN"), 0.20),
            max_open_positions=_as_int(g("MAX_OPEN_POSITIONS"), 5),
            max_market_exposure=_as_float(g("MAX_MARKET_EXPOSURE"), 0.10),
            max_category_exposure=_as_float(g("MAX_CATEGORY_EXPOSURE"), 0.50),
            max_total_exposure=_as_float(g("MAX_TOTAL_EXPOSURE"), 0.60),
            max_spread=_as_float(g("MAX_SPREAD"), 0.08),
            min_volume=_as_int(g("MIN_VOLUME"), 100),
            min_model_confidence=_as_float(g("MIN_MODEL_CONFIDENCE"), 0.60),
            allow_near_settlement=_as_bool(g("ALLOW_NEAR_SETTLEMENT"), False),
            min_hours_to_close=_as_float(g("MIN_HOURS_TO_CLOSE"), 2.0),
            min_price=_as_float(g("MIN_PRICE"), 0.05),
            max_price=_as_float(g("MAX_PRICE"), 0.95),
            use_intraday=_as_bool(g("USE_INTRADAY"), False),
            intraday_mode=g("INTRADAY_MODE", "floor").strip().lower(),
            intraday_peak_hour=_as_int(g("INTRADAY_PEAK_HOUR"), 15),
            intraday_peak_decay_hours=_as_float(g("INTRADAY_PEAK_DECAY_HOURS"), 4.0),
            skip_unprofitable_segments=_as_bool(g("SKIP_UNPROFITABLE_SEGMENTS"), False),
            segment_min_samples=_as_int(g("SEGMENT_MIN_SAMPLES"), 20),
            favorite_mode=_as_bool(g("FAVORITE_MODE"), False),
            favorite_p_floor=_as_float(g("FAVORITE_P_FLOOR"), 0.65),
            entry_edge_threshold_favorite=_as_float(g("ENTRY_EDGE_THRESHOLD_FAVORITE"), 0.08),
            min_edge_to_price_ratio=_as_float(g("MIN_EDGE_TO_PRICE_RATIO"), 0.0),
            use_kelly_sizing=_as_bool(g("USE_KELLY_SIZING"), False),
            kelly_fraction=_as_float(g("KELLY_FRACTION"), 0.25),
            kelly_fraction_cap=_as_float(g("KELLY_FRACTION_CAP"), 0.5),
            kelly_confidence_scaled=_as_bool(g("KELLY_CONFIDENCE_SCALED"), True),
            paper_trading=_as_bool(g("PAPER_TRADING"), True),
            live_trading=_as_bool(g("LIVE_TRADING"), False),
            auto_trade=_as_bool(g("AUTO_TRADE"), False),
            confirm_live_trades=_as_bool(g("CONFIRM_LIVE_TRADES"), True),
            use_limit_orders_only=_as_bool(g("USE_LIMIT_ORDERS_ONLY"), True),
            emergency_stop=_as_bool(g("EMERGENCY_STOP"), False),
            polymarket_gamma_base=g("POLYMARKET_GAMMA_BASE", "https://gamma-api.polymarket.com").rstrip("/"),
            polymarket_clob_base=g("POLYMARKET_CLOB_BASE", "https://clob.polymarket.com").rstrip("/"),
            kalshi_api_base=g("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/"),
            kalshi_api_key_id=g("KALSHI_API_KEY_ID", "") or "",
            kalshi_private_key_path=g("KALSHI_PRIVATE_KEY_PATH", "") or "",
            edge_proven_min_trades=_as_int(g("EDGE_PROVEN_MIN_TRADES"), 150),
            edge_proven_require_brier_beat=_as_bool(g("EDGE_PROVEN_REQUIRE_BRIER_BEAT"), True),
            weather_cities=cities or ["NYC", "CHI"],
            openmeteo_models=g("OPENMETEO_MODELS", "gfs025,ecmwf_ifs025") or "gfs025,ecmwf_ifs025",
            histbacktest_forecast_model=g("HISTBACKTEST_FORECAST_MODEL", "gfs_seamless") or "gfs_seamless",
            use_nbm_blend=_as_bool(g("USE_NBM_BLEND"), False),
            nbm_blend_weight=_as_float(g("NBM_BLEND_WEIGHT"), 0.5),
            loop_interval_seconds=_as_int(g("LOOP_INTERVAL_SECONDS"), 300),
            db_path=g("DB_PATH", "prediction_market_bot.db"),
            log_level=g("LOG_LEVEL", "INFO").upper(),
            alert_webhook_url=g("ALERT_WEBHOOK_URL", "") or "",
        )

    # ------------------------------------------------------------------ #
    def emergency_stop_engaged(self) -> bool:
        """True if the kill switch is set via env flag OR the STOP file exists."""
        if self.emergency_stop:
            return True
        try:
            return Path(self.emergency_stop_file).exists()
        except OSError:
            return False

    def is_live_trading_armed(self) -> bool:
        """Real orders are permitted ONLY when every gate is deliberately open.

        This is the single source of truth used by live_trader. If any one of
        these is wrong, the bot stays in paper mode. Note that PAPER_TRADING=true
        is an absolute override: you must explicitly set it false to go live.
        """
        return (
            not self.paper_trading
            and self.live_trading
            and self.auto_trade
            and not self.emergency_stop_engaged()
            and bool(self.kalshi_api_key_id)
            and bool(self.kalshi_private_key_path)
        )

    def as_public_dict(self) -> dict:
        """Config for display/logging with secrets redacted."""
        out = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if "key" in f.name or "private" in f.name:
                val = "***set***" if val else ""
            out[f.name] = val
        return out
