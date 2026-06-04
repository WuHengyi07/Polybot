"""Polymarket adapter — READ-ONLY data for paper trading the international markets.

Polymarket lists daily-temperature markets for global cities (Shanghai, Seoul, Hong
Kong, Tokyo, ...). They are grouped as EVENTS (one per city/day) under the
"Daily Temperature" tag (id 103040); each event holds one binary Yes/No sub-market per
1 deg-C bucket ("22C or below", "23C", ... "35C or higher") — Kalshi brackets in
disguise. Resolution is Weather Underground (intl, Celsius) / Hong Kong Observatory.

This client is READ-ONLY (plain `requests`, no web3/wallet): it lists markets + prices so
the existing pipeline can PAPER-trade them. Live on-chain execution (py-clob-client, USDC,
EIP-712 signed orders) is intentionally NOT here — it stays gated behind the edge-proven gate.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from .market_client import Market, MarketClient, OrderBook, OrderBookLevel
from .market_parser import CITY_REGISTRY
from .utils import get_logger, to_float

log = get_logger("polymarket_client")

GAMMA_BASE = "https://gamma-api.polymarket.com"
DAILY_TEMP_TAG_ID = 103040  # Polymarket "Daily Temperature" tag


def _json_list(value) -> list:
    """gamma returns outcomePrices/clobTokenIds as JSON-encoded strings (or real lists)."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _ymd(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%Y%m%d")
    except ValueError:
        return str(iso)[:10].replace("-", "")


def _city_for_event(event: dict):
    """Match an event to a CITY_REGISTRY city via its title/slug/series ticker."""
    text = ((event.get("title") or "") + " " + (event.get("slug") or "")).lower()
    for s in event.get("series", []) or []:
        text += " " + str(s.get("ticker", "")).lower()
    for city in CITY_REGISTRY.values():
        if any(name in text for name in city.names):
            return city
    return None


def events_to_markets(events: List[dict], cities: Optional[List[str]] = None) -> List[Market]:
    """Pure transform: gamma `/events` JSON -> one normalized Market per °C bucket.

    `cities` (city codes) filters to the ones we forecast; None keeps all known cities.
    """
    allowed = {c.upper() for c in cities} if cities else None
    out: List[Market] = []
    for ev in events:
        city = _city_for_event(ev)
        if city is None or (allowed is not None and city.code not in allowed):
            continue
        ev_title = ev.get("title") or f"Highest temperature in {city.code}"
        end = ev.get("endDate") or ev.get("end_date")
        ymd = _ymd(end)
        ev_source = ev.get("resolutionSource") or ev.get("resolution_source") or ""
        # Hong Kong settles on the HK Observatory (the API often omits resolutionSource);
        # the rest settle on Weather Underground. Supply a clear default when missing.
        default_source = "Hong Kong Observatory" if city.code == "HKG" else "Weather Underground"
        for sm in ev.get("markets", []) or []:
            bucket = sm.get("groupItemTitle") or sm.get("question") or ""
            if not bucket:
                continue
            prices = _json_list(sm.get("outcomePrices"))
            yes_p = to_float(prices[0]) if prices else 0.0
            bid = to_float(sm.get("bestBid"), yes_p)
            ask = to_float(sm.get("bestAsk"), yes_p)
            mid = sm.get("id") or sm.get("conditionId") or bucket
            ticker = f"PM-{city.code}-{ymd}-{mid}"
            # The per-bucket `question` carries variable+city+threshold+date; prefer it.
            title = sm.get("question") or f"{ev_title} - {bucket}"
            out.append(Market.from_dict({
                "market_id": ticker, "ticker": ticker,
                "title": title,
                "yes_bid": bid, "yes_ask": ask,
                "volume": int(to_float(sm.get("volume") or sm.get("volumeClob"))),
                "close_time": end,
                "settlement_source": sm.get("resolutionSource") or ev_source or default_source,
                "category": "weather",
                "event_id": ev.get("id"),
                "clob_token_ids": _json_list(sm.get("clobTokenIds")),
            }))
    return out


def _book_levels(side) -> list:
    """[{price,size}, ...] -> [OrderBookLevel] (dropping zero/blank levels)."""
    out = []
    for lvl in side or []:
        p, s = to_float(lvl.get("price")), int(to_float(lvl.get("size")))
        if p > 0 and s > 0:
            out.append(OrderBookLevel(p, s))
    return out


def apply_books(markets, books_by_token: dict):
    """Attach the REAL CLOB order book (keyed by token id) to each market and refresh its
    top-of-book quotes from it. This is the realism fix: with the resting book present, the
    risk manager caps fills to actual depth, and a side with no asks becomes untradeable
    (ask -> 0) instead of being 'filled' at a stale quote. Markets whose tokens aren't in
    ``books_by_token`` are left untouched (gamma fallback)."""
    for m in markets:
        ids = m.raw.get("clob_token_ids") or []
        yes_id = str(ids[0]) if len(ids) > 0 else None
        no_id = str(ids[1]) if len(ids) > 1 else None
        if (yes_id not in books_by_token) and (no_id not in books_by_token):
            continue
        yb = books_by_token.get(yes_id) or {}
        nb = books_by_token.get(no_id) or {}
        ob = OrderBook(
            yes_bids=_book_levels(yb.get("bids")), yes_asks=_book_levels(yb.get("asks")),
            no_bids=_book_levels(nb.get("bids")), no_asks=_book_levels(nb.get("asks")))
        m.order_book = ob
        # Fresh top-of-book; 0.0 when a side is empty (-> untradeable, correctly).
        m.yes_bid = max((l.price for l in ob.yes_bids), default=0.0)
        m.yes_ask = min((l.price for l in ob.yes_asks), default=0.0)
        m.no_bid = max((l.price for l in ob.no_bids), default=0.0)
        m.no_ask = min((l.price for l in ob.no_asks), default=0.0)
    return markets


class PolymarketClient(MarketClient):
    name = "polymarket"

    def __init__(self, config):
        self.config = config
        self.gamma_base = getattr(config, "polymarket_gamma_base", GAMMA_BASE).rstrip("/")
        try:
            import requests
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("pip install requests to use the Polymarket client") from exc
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json",
                                      "User-Agent": "prediction_market_bot/1.0"})

    def _fetch_events(self, max_pages: int = 8, page: int = 500) -> List[dict]:
        from .utils import utcnow
        cutoff = utcnow().date().isoformat()
        events: List[dict] = []
        for i in range(max_pages):
            try:
                resp = self._session.get(f"{self.gamma_base}/events", params={
                    "tag_id": DAILY_TEMP_TAG_ID, "closed": "false", "limit": page,
                    "offset": i * page, "order": "endDate", "ascending": "true",
                    "end_date_min": f"{cutoff}T00:00:00Z"}, timeout=30)
                resp.raise_for_status()
                batch = resp.json()
            except Exception as exc:  # pragma: no cover - network dependent
                log.warning("Polymarket events fetch failed (page %d): %s", i, exc)
                break
            if not batch:
                break
            events.extend(batch)
            if len(batch) < page:
                break
        return events

    def _fetch_books(self, token_ids: List[str]) -> dict:
        """POST clob/books (batched) -> {asset_id: {bids, asks}}. Best-effort."""
        clob = getattr(self.config, "polymarket_clob_base", "https://clob.polymarket.com").rstrip("/")
        books: dict = {}
        ids = [t for t in token_ids if t]
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                resp = self._session.post(f"{clob}/books",
                                          json=[{"token_id": t} for t in chunk], timeout=30)
                resp.raise_for_status()
                for b in resp.json() or []:
                    aid = b.get("asset_id") or b.get("token_id")
                    if aid is not None:
                        books[str(aid)] = b
            except Exception as exc:  # pragma: no cover - network dependent
                log.warning("Polymarket books fetch failed: %s", exc)
        return books

    def list_markets(self) -> List[Market]:
        cities = getattr(self.config, "weather_cities", None)
        markets = events_to_markets(self._fetch_events(), cities)
        # Realism: replace stale gamma quotes with the live order book and cap fills to depth.
        token_ids = [t for m in markets for t in (m.raw.get("clob_token_ids") or [])]
        if token_ids:
            apply_books(markets, self._fetch_books(token_ids))
        log.info("PolymarketClient: %d bucket markets across %s",
                 len(markets), ",".join(cities or []) or "all cities")
        return markets
