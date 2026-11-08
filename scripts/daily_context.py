"""Read-only constituent quote and announcement collection for daily briefings."""
import csv
from datetime import datetime
import io

from core import Block, IST, fresh, instant, integer, positive
from data import SOURCES, fetch, members_csv, rss
from daily import session_bounds

MEMBERSHIP_SOURCES = {"nifty": "nifty50", "banknifty": "niftybank"}


def membership(index, now, supplied=None, fetcher=fetch):
    if supplied is not None:
        item = supplied[index]
        if item["index_id"] != index or item["membership_date"] != now.date().isoformat() or not item.get("source"):
            raise Block("MEMBERSHIP: sourced exact index/date required")
        fresh(item["as_of"], now, 86400, "membership")
        if item.get("membership_verified") is not True: raise Block("MEMBERSHIP: explicit reviewed membership required")
        symbols = item["members"]
        source = item["source"]; verified = True
    else:
        if index not in MEMBERSHIP_SOURCES: raise Block("MEMBERSHIP: explicit Sensex membership source required")
        named = MEMBERSHIP_SOURCES[index]
        parsed = members_csv(fetcher(named), SOURCES[named], now)
        symbols = [r["symbol"] for r in parsed["members"]]; source = SOURCES[named]; verified = False
        # The public file has no effective-date field. Fetch time cannot prove it.
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 500 or len(set(symbols)) != len(symbols) or any(not isinstance(s, str) or not s.strip() or ":" in s for s in symbols):
        raise Block("MEMBERSHIP: unique symbol list required")
    return {"members": symbols, "membership_source": source, "membership_date": now.date().isoformat(), "membership_verified": verified,
            "membership_basis": "reviewed current-session list" if verified else "current public list fetched today; effective date unverified"}


def collect_breadth(client, raw_master, index, calendar, now, *, supplied=None, fetcher=fetch, clock=None):
    start, _, phase = session_bounds(calendar, now)
    if phase != "intraday": raise Block("BREADTH: automatic constituent quotes supported during the session only")
    item = membership(index, now, supplied, fetcher)
    exchange = "BSE" if index == "sensex" else "NSE"
    rows = list(csv.DictReader(io.StringIO(raw_master)))
    keys = [exchange + ":" + symbol for symbol in item["members"]]
    quote_bundle = client.quotes(keys)
    observed_at = clock() if clock else now
    if session_bounds(calendar, observed_at)[2] != "intraday": raise Block("BREADTH: collection crossed a session boundary")
    if quote_bundle.get("source") != "kite": raise Block("BREADTH: Kite quote source required")
    observations = []; unavailable = []
    for symbol, key in zip(item["members"], keys):
        try:
            exact = [r for r in rows if r.get("exchange") == exchange and r.get("tradingsymbol") == symbol and r.get("instrument_type") == "EQ" and r.get("segment") == exchange]
            if len(exact) != 1 or key in quote_bundle.get("missing", []): raise Block("BREADTH: missing/ambiguous equity master or quote")
            quote = quote_bundle["data"][key]
            if integer(quote["instrument_token"]) != integer(exact[0]["instrument_token"]): raise Block("BREADTH: wrong token")
            stamp = datetime.fromisoformat(quote["last_trade_time"])
            if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=IST)
            fresh(stamp.isoformat(), observed_at, 60, "constituent trade")
            if stamp < start: raise Block("BREADTH: prior-session trade")
            observations.append({"symbol": symbol, "as_of": stamp.isoformat(), "value": float(positive(quote["last_price"])),
                                 "previous_close": float(positive(quote["ohlc"]["close"])), "previous_session": calendar["previous_session"]})
        except (Block, ValueError, TypeError, KeyError, OverflowError): unavailable.append(symbol)
    return {**item, "source": "kite_constituent_quotes", "as_of": observed_at.isoformat(), "observations": observations,
            "unavailable_symbols": unavailable, "previous_close_basis": "Kite quote OHLC close: last trading day, bound to supplied calendar"}


def collect_news(calendar, now, fetcher=fetch, clock=None):
    raw = fetcher("announcements")
    now = clock() if clock else now
    if now.date().isoformat() != calendar["date"]: raise Block("NEWS: collection crossed a session boundary")
    feed = rss(raw, SOURCES["announcements"], now)
    items = [{"headline": r["headline"], "source": r["url"], "published_at": r["published_at"]}
             for r in feed["records"] if instant(r["published_at"]).date() == now.date()]
    return {"source": SOURCES["announcements"], "as_of": now.isoformat(), "session": calendar["date"], "coverage": "partial", "items": items,
            "unparsed_count": len(feed["unparsed"]), "scope": "NSE announcement feed only; not a complete macro/event calendar"}


def enrich(bundle, client, raw_master, selected, *, supplied=None, clock=instant, fetcher=fetch):
    errors = bundle.setdefault("context_errors", {})
    for key in selected:
        if key not in bundle["indices"]: continue
        try: bundle["indices"][key]["breadth"] = collect_breadth(client, raw_master, key, bundle["session"], clock(), supplied=supplied, fetcher=fetcher, clock=clock)
        except (Block, ValueError, TypeError, KeyError, OSError): errors[key + "_breadth"] = "Constituent collection unavailable; membership/session/quote evidence required"
    if "events" not in bundle:
        try: bundle["events"] = collect_news(bundle["session"], clock(), fetcher, clock)
        except (Block, ValueError, TypeError, KeyError, OSError): errors["announcements"] = "Announcement collection unavailable; no event clearance asserted"
    return bundle
