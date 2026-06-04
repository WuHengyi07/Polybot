"""Kalshi adapter.

Read-only market data (list markets, order books) needs NO authentication and is
used whenever DATA_SOURCE=live. The authenticated, order-placing methods are a
scaffold used ONLY by live_trader, and only after config.is_live_trading_armed().

Verified live on 2026-06-02: base https://api.elections.kalshi.com/trade-api/v2 ,
GET /markets and GET /markets/{ticker}/orderbook are public; weather series are
KXHIGHNY, KXHIGHCHI, ...; prices come as *_dollars strings.
"""
from __future__ import annotations

import base64
import time
from typing import List, Optional
from urllib.parse import urlsplit

from .market_client import Market, MarketClient, OrderBook, OrderBookLevel
from .market_parser import series_ticker_for_city
from .utils import get_logger, parse_iso, to_float

log = get_logger("kalshi_client")


class KalshiClient(MarketClient):
    name = "kalshi"

    def __init__(self, config):
        self.config = config
        self.base = config.kalshi_api_base.rstrip("/")
        try:
            import requests  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "The 'requests' package is required for live Kalshi data. "
                "pip install requests"
            ) from exc
        import requests

        self._requests = requests
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json", "User-Agent": "prediction_market_bot/1.0"})

    # ------------------------------------------------------------------ #
    # Public read-only endpoints (no auth)
    # ------------------------------------------------------------------ #
    def _get(self, path: str, params: Optional[dict] = None, *, auth: bool = False) -> dict:
        url = self.base + path
        headers = self._signed_headers("GET", url) if auth else {}
        for attempt in range(3):
            resp = self._session.get(url, params=params, headers=headers, timeout=20)
            if resp.status_code == 429:  # rate limited — back off, do not hammer
                time.sleep(0.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Kalshi GET {path} rate-limited after retries")

    def list_markets(self) -> List[Market]:
        markets: List[Market] = []
        for code in self.config.weather_cities:
            series = series_ticker_for_city(code)
            if not series:
                log.warning("No Kalshi series mapping for city %s; skipping.", code)
                continue
            cursor = None
            for _ in range(20):  # hard page cap as a safety stop
                params = {"series_ticker": series, "status": "open", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                try:
                    data = self._get("/markets", params)
                except Exception as exc:  # pragma: no cover - network dependent
                    log.warning("Failed to fetch %s markets: %s", series, exc)
                    break
                for raw in data.get("markets", []):
                    markets.append(self._market_from_kalshi(raw))
                cursor = data.get("cursor")
                if not cursor:
                    break
        log.info("Kalshi returned %d open weather markets for %s", len(markets), self.config.weather_cities)
        return markets

    def get_order_book(self, ticker: str, depth: int = 10) -> Optional[OrderBook]:
        try:
            data = self._get(f"/markets/{ticker}/orderbook", {"depth": depth})
        except Exception as exc:  # pragma: no cover
            log.warning("Order book fetch failed for %s: %s", ticker, exc)
            return None
        book = data.get("orderbook") or data.get("orderbook_fp") or {}
        # Kalshi gives bid ladders for YES and NO in cents; asks are the
        # complement of the opposite side's bids (a YES ask == 100 - NO bid).
        yes_bids = _levels_cents(book.get("yes"))
        no_bids = _levels_cents(book.get("no"))
        yes_asks = [OrderBookLevel(round(1.0 - l.price, 2), l.size) for l in no_bids]
        no_asks = [OrderBookLevel(round(1.0 - l.price, 2), l.size) for l in yes_bids]
        return OrderBook(yes_bids=yes_bids, yes_asks=yes_asks, no_bids=no_bids, no_asks=no_asks)

    @staticmethod
    def _market_from_kalshi(d: dict) -> Market:
        def price(key_dollars: str, key_cents: str) -> float:
            if d.get(key_dollars) not in (None, ""):
                return to_float(d.get(key_dollars))
            return round(to_float(d.get(key_cents)) / 100.0, 4)

        yes_bid = price("yes_bid_dollars", "yes_bid")
        yes_ask = price("yes_ask_dollars", "yes_ask")
        no_bid = price("no_bid_dollars", "no_bid") or round(1.0 - yes_ask, 4)
        no_ask = price("no_ask_dollars", "no_ask") or round(1.0 - yes_bid, 4)
        volume = int(to_float(d.get("volume", d.get("volume_fp", 0))))
        oi = int(to_float(d.get("open_interest", d.get("open_interest_fp", 0))))
        return Market(
            market_id=d.get("ticker", ""),
            ticker=d.get("ticker", ""),
            title=d.get("title") or d.get("yes_sub_title") or d.get("subtitle", ""),
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            volume=volume,
            open_interest=oi,
            close_time=parse_iso(d.get("close_time")),
            expiration_time=parse_iso(d.get("expiration_time") or d.get("expected_expiration_time")),
            rules=(d.get("rules_primary") or d.get("settlement_sources_text") or "")[:1000],
            settlement_source=_settlement_text(d),
            order_book=None,
            category="weather",
            raw=d,
        )

    # ------------------------------------------------------------------ #
    # Authenticated endpoints — used ONLY by live_trader, only when armed
    # ------------------------------------------------------------------ #
    def _signed_headers(self, method: str, url: str) -> dict:
        """RSA-PSS request signing per Kalshi docs: sign(timestamp_ms + METHOD + path)."""
        if not (self.config.kalshi_api_key_id and self.config.kalshi_private_key_path):
            raise RuntimeError("Kalshi API key id + private key path are required for authenticated calls.")
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("pip install cryptography to sign Kalshi requests") from exc

        ts = str(int(time.time() * 1000))
        path = urlsplit(url).path
        message = (ts + method.upper() + path).encode("utf-8")
        with open(self.config.kalshi_private_key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
        signature = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.config.kalshi_api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        }

    def get_balance(self) -> dict:  # pragma: no cover - requires live creds
        return self._get("/portfolio/balance", auth=True)

    def get_positions(self) -> dict:  # pragma: no cover
        return self._get("/portfolio/positions", auth=True)

    def get_fills(self, ticker: Optional[str] = None, limit: int = 200) -> dict:  # pragma: no cover
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self._get("/portfolio/fills", params, auth=True)

    def get_order(self, client_order_id: str) -> dict:  # pragma: no cover
        """Look up an order by client_order_id — used to confirm a submission
        landed after a network timeout, so a retry never double-fires."""
        return self._get("/portfolio/orders", {"client_order_id": client_order_id}, auth=True)

    def place_order(self, order: dict) -> dict:  # pragma: no cover - never called in paper mode
        """POST an order. Caller (live_trader) is responsible for arming checks.

        ``order`` must include a unique ``client_order_id`` for idempotency so a
        retried request cannot create a duplicate position.
        """
        url = self.base + "/portfolio/orders"
        headers = self._signed_headers("POST", url)
        headers["Content-Type"] = "application/json"
        resp = self._session.post(url, json=order, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def cancel_order(self, order_id: str) -> dict:  # pragma: no cover
        url = self.base + f"/portfolio/orders/{order_id}"
        headers = self._signed_headers("DELETE", url)
        resp = self._session.delete(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json()


def _levels_cents(rows) -> List[OrderBookLevel]:
    out: List[OrderBookLevel] = []
    for row in rows or []:
        try:
            price_cents, size = row[0], row[1]
            out.append(OrderBookLevel(round(to_float(price_cents) / 100.0, 4), int(to_float(size))))
        except (IndexError, TypeError, ValueError):
            continue
    return out


def _settlement_text(d: dict) -> str:
    for key in ("settlement_sources_text", "rules_primary", "subtitle"):
        val = d.get(key)
        if val:
            # Weather markets settle on the NWS Daily Climate Report; surface that if present.
            return str(val)[:300]
    # Default for KXHIGH* weather series.
    if str(d.get("ticker", "")).upper().startswith("KXHIGH"):
        return "NWS Daily Climate Report (daily high temperature)"
    return ""
