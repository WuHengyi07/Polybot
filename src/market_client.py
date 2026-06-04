"""Unified prediction-market data interface + the offline mock implementation.

``Market`` is the normalized shape every adapter (mock / Kalshi / Polymarket)
returns, so the rest of the bot never sees exchange-specific JSON.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .utils import get_logger, parse_iso, to_float

log = get_logger("market_client")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# --------------------------------------------------------------------------- #
# Normalized data structures
# --------------------------------------------------------------------------- #
@dataclass
class OrderBookLevel:
    price: float
    size: int


@dataclass
class OrderBook:
    """Resting depth. Kalshi YES/NO are complementary; we keep both sides if given."""
    yes_bids: List[OrderBookLevel] = field(default_factory=list)
    yes_asks: List[OrderBookLevel] = field(default_factory=list)
    no_bids: List[OrderBookLevel] = field(default_factory=list)
    no_asks: List[OrderBookLevel] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["OrderBook"]:
        if not d:
            return None

        def levels(key):
            return [OrderBookLevel(to_float(p), int(s)) for p, s in d.get(key, []) or []]

        return cls(
            yes_bids=levels("yes_bids"),
            yes_asks=levels("yes_asks"),
            no_bids=levels("no_bids"),
            no_asks=levels("no_asks"),
        )

    def depth_at_or_better(self, side: str, price: float) -> int:
        """Total resting size you could take on ``side`` ('yes'/'no') at <= price (asks)."""
        asks = self.yes_asks if side == "yes" else self.no_asks
        return sum(l.size for l in asks if l.price <= price + 1e-9)


@dataclass
class Market:
    market_id: str
    ticker: str
    title: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    volume: int = 0
    open_interest: int = 0
    close_time: Optional[datetime] = None
    expiration_time: Optional[datetime] = None
    rules: str = ""
    settlement_source: str = ""
    order_book: Optional[OrderBook] = None
    category: str = "weather"
    raw: dict = field(default_factory=dict)

    @property
    def yes_spread(self) -> float:
        return round(self.yes_ask - self.yes_bid, 6)

    @property
    def yes_mid(self) -> float:
        return round((self.yes_bid + self.yes_ask) / 2.0, 6)

    @classmethod
    def from_dict(cls, d: dict) -> "Market":
        yes_bid = to_float(d.get("yes_bid"))
        yes_ask = to_float(d.get("yes_ask"))
        # Derive the NO book from YES if not provided (Kalshi complementarity).
        no_bid = to_float(d.get("no_bid"), round(1.0 - yes_ask, 4))
        no_ask = to_float(d.get("no_ask"), round(1.0 - yes_bid, 4))
        return cls(
            market_id=d.get("market_id") or d.get("ticker", ""),
            ticker=d.get("ticker", ""),
            title=d.get("title", ""),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            volume=int(to_float(d.get("volume"))),
            open_interest=int(to_float(d.get("open_interest"))),
            close_time=parse_iso(d.get("close_time")),
            expiration_time=parse_iso(d.get("expiration_time")),
            rules=d.get("rules", ""),
            settlement_source=d.get("settlement_source", ""),
            order_book=OrderBook.from_dict(d.get("order_book")),
            category=d.get("category", "weather"),
            raw=d,
        )


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #
class MarketClient(ABC):
    """Every data adapter implements this. Read-only; trading lives in live_trader."""

    name: str = "abstract"

    @abstractmethod
    def list_markets(self) -> List[Market]:
        ...

    def get_market(self, ticker: str) -> Optional[Market]:
        for m in self.list_markets():
            if m.ticker == ticker or m.market_id == ticker:
                return m
        return None

    def get_order_book(self, ticker: str) -> Optional[OrderBook]:
        m = self.get_market(ticker)
        return m.order_book if m else None


class MockMarketClient(MarketClient):
    """Deterministic markets loaded from data/mock_markets.json."""

    name = "mock"

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else DATA_DIR / "mock_markets.json"

    def list_markets(self) -> List[Market]:
        with open(self.path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        markets = [Market.from_dict(m) for m in payload.get("markets", [])]
        log.debug("MockMarketClient loaded %d markets", len(markets))
        return markets


def build_market_client(config) -> MarketClient:
    """Factory: returns the right client for config.data_source.

    Live falls back to mock if the live adapter cannot initialize, so the bot
    never crashes a paper run because of a network/library issue.
    """
    if config.data_source == "live":
        try:
            from .kalshi_client import KalshiClient

            return KalshiClient(config)
        except Exception as exc:  # pragma: no cover - depends on environment
            log.warning("Live Kalshi client unavailable (%s); falling back to mock data.", exc)
            return MockMarketClient()
    if config.data_source == "polymarket":
        try:
            from .polymarket_client import PolymarketClient

            return PolymarketClient(config)
        except Exception as exc:  # pragma: no cover - depends on environment
            log.warning("Polymarket client unavailable (%s); falling back to mock data.", exc)
            return MockMarketClient()
    return MockMarketClient()
