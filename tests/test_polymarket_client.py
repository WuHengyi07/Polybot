"""Polymarket event JSON -> normalized Market list (pure transform, no network)."""
from src.market_parser import Direction, parse_market
from src.polymarket_client import apply_books, events_to_markets

# Shape mirrors gamma-api /events: outcomePrices / clobTokenIds arrive as JSON strings.
SHANGHAI_EVENT = {
    "id": "553911",
    "title": "Highest temperature in Shanghai on June 5, 2026",
    "slug": "highest-temperature-in-shanghai-on-june-5-2026",
    "endDate": "2026-06-05T12:00:00Z",
    "active": True, "closed": False,
    "series": [{"id": 10741, "ticker": "shanghai-daily-weather", "recurrence": "daily"}],
    "resolutionSource": "https://www.wunderground.com/history/daily/cn/shanghai/ZSPD",
    "markets": [
        {"id": "2424602", "groupItemTitle": "22°C or below",
         "outcomePrices": "[\"0.0055\", \"0.9945\"]", "clobTokenIds": "[\"161\", \"410\"]",
         "bestBid": 0.004, "bestAsk": 0.01, "volume": "120"},
        {"id": "2424603", "groupItemTitle": "23°C",
         "outcomePrices": "[\"0.30\", \"0.70\"]", "clobTokenIds": "[\"1\", \"2\"]",
         "bestBid": 0.29, "bestAsk": 0.31, "volume": "250"},
        {"id": "2424604", "groupItemTitle": "35°C or higher",
         "outcomePrices": "[\"0.01\", \"0.99\"]", "clobTokenIds": "[\"3\", \"4\"]",
         "bestBid": 0.005, "bestAsk": 0.02, "volume": "60"},
    ],
}

TOKYO_EVENT = {  # off-list city -> must be skipped when cities filter excludes it
    "id": "999", "title": "Highest temperature in Tokyo on June 5, 2026",
    "endDate": "2026-06-05T12:00:00Z", "series": [{"ticker": "tokyo-daily-weather"}],
    "resolutionSource": "https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
    "markets": [{"id": "1", "groupItemTitle": "28°C", "outcomePrices": "[\"0.5\",\"0.5\"]",
                 "clobTokenIds": "[\"9\",\"10\"]", "bestBid": 0.49, "bestAsk": 0.51, "volume": "10"}],
}


def test_event_expands_to_one_market_per_bucket():
    mkts = events_to_markets([SHANGHAI_EVENT], ["SHA"])
    assert len(mkts) == 3
    m = {x.ticker: x for x in mkts}
    mid = [x for x in mkts if "23" in x.title][0]
    assert mid.ticker.startswith("PM-SHA-20260605-")
    assert mid.yes_bid == 0.29 and mid.yes_ask == 0.31
    assert "shanghai" in mid.title.lower() and "23°c" in mid.title.lower()
    assert "wunderground" in mid.settlement_source.lower()
    assert mid.raw.get("event_id") == "553911"
    assert mid.raw.get("clob_token_ids") == ["1", "2"]


def test_city_filter_skips_off_list_events():
    mkts = events_to_markets([SHANGHAI_EVENT, TOKYO_EVENT], ["SHA"])
    assert all(x.ticker.startswith("PM-SHA-") for x in mkts) and len(mkts) == 3


# Mirrors the LIVE gamma shape: per-bucket `question`, NO year in the date, and Hong Kong
# returns resolutionSource = null.
REAL_SHA = {
    "id": "1", "title": "Lowest temperature in Shanghai on June 4?",
    "slug": "lowest-temperature-in-shanghai", "endDate": "2026-06-04T12:00:00Z",
    "series": [{"ticker": "shanghai-daily-weather"}],
    "resolutionSource": "https://www.wunderground.com/history/daily/cn/shanghai/ZSPD",
    "markets": [{"id": "9", "groupItemTitle": "18°C",
                 "question": "Will the lowest temperature in Shanghai be 18°C on June 4?",
                 "outcomePrices": "[\"0.3\",\"0.7\"]", "clobTokenIds": "[\"1\",\"2\"]",
                 "bestBid": 0.29, "bestAsk": 0.31, "volume": "5"}],
}
REAL_HKG = {
    "id": "2", "title": "Highest temperature in Hong Kong on June 4?",
    "series": [{"ticker": "hong-kong-daily-weather"}], "endDate": "2026-06-04T12:00:00Z",
    "resolutionSource": None,
    "markets": [{"id": "3", "groupItemTitle": "30°C",
                 "question": "Will the highest temperature in Hong Kong be 30°C on June 4?",
                 "outcomePrices": "[\"0.2\",\"0.8\"]", "clobTokenIds": "[\"5\",\"6\"]",
                 "bestBid": 0.19, "bestAsk": 0.21, "volume": "7"}],
}


def test_real_shape_parses_tradeable_without_year_in_title():
    mkts = events_to_markets([REAL_SHA], ["SHA"])
    assert len(mkts) == 1
    p = parse_market(mkts[0])
    assert p.tradeable is True and p.location == "SHA" and p.target_date is not None


def test_missing_resolution_source_defaults_to_clear():
    mkts = events_to_markets([REAL_HKG], ["HKG"])
    assert mkts[0].settlement_source                      # non-empty default supplied
    assert parse_market(mkts[0]).tradeable is True


def test_produced_markets_parse_and_are_tradeable():
    # The normalized markets must flow through the existing parser as Celsius buckets.
    mkts = events_to_markets([SHANGHAI_EVENT], ["SHA"])
    parsed = [parse_market(m) for m in mkts]
    assert all(p.tradeable for p in parsed)
    assert all(p.location == "SHA" and p.threshold_unit == "C" for p in parsed)
    kinds = {p.direction for p in parsed}
    assert Direction.BELOW in kinds and Direction.ABOVE in kinds and Direction.BETWEEN in kinds


def test_apply_books_attaches_real_depth_and_refreshes_quotes():
    # The 23C bucket has clobTokenIds ["1","2"]; give it a real (thin) book.
    mkts = events_to_markets([SHANGHAI_EVENT], ["SHA"])
    books = {"1": {"bids": [{"price": "0.28", "size": "40"}],
                   "asks": [{"price": "0.30", "size": "5"}, {"price": "0.33", "size": "100"}]},
             "2": {"bids": [{"price": "0.70", "size": "10"}], "asks": [{"price": "0.72", "size": "8"}]}}
    apply_books(mkts, books)
    m = [x for x in mkts if x.raw.get("clob_token_ids") == ["1", "2"]][0]
    assert m.yes_bid == 0.28 and m.yes_ask == 0.30           # fresh top-of-book from the CLOB
    assert m.order_book is not None
    assert m.order_book.depth_at_or_better("yes", 0.30) == 5    # only the best ask is fillable here
    assert m.order_book.depth_at_or_better("yes", 0.33) == 105  # deeper if you cross up


def test_apply_books_marks_no_ask_bucket_untradeable():
    # A bucket whose YES book has no asks can't be bought -> ask 0 -> parser/risk reject it.
    mkts = events_to_markets([SHANGHAI_EVENT], ["SHA"])
    books = {"1": {"bids": [{"price": "0.20", "size": "10"}], "asks": []}}
    apply_books(mkts, books)
    m = [x for x in mkts if x.raw.get("clob_token_ids") == ["1", "2"]][0]
    assert m.yes_ask == 0.0 and m.order_book.depth_at_or_better("yes", 0.99) == 0
