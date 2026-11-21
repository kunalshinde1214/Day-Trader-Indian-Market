"""Free NSE index levels, trading holidays and dated index closes. Read-only public endpoints; research only.

* ``live_indices``   - NSE's public allIndices snapshot (NIFTY 50, NIFTY BANK, INDIA VIX, sectoral indices) with
                       NSE's own minute timestamp. Falls back per index to Yahoo's chart meta.regularMarketPrice.
* ``holiday_calendar`` - NSE's trading-holiday master (CM and F&O lists, with holiday names), cached per run.
* ``index_history``  - official daily closes from NSE's ``ind_close_all_DDMMYYYY.csv`` files, including special
                       sessions (for example the Diwali Muhurat session) that Yahoo's daily history omits.
* ``cached_bhavcopy`` - one day's ``sec_bhavdata_full`` cash bhavcopy, retried with backoff and kept on disk once the
                       day is over (a published past bhavcopy never changes).
* ``index_members``  - an NSE index's constituent list (``ind_<index>list.csv``), cached on disk for a few days.
* ``fpi_month``      - NSDL's current-month daily FPI equity investment (Monthly.aspx), for month-to-date flows.

Every request goes to one exact allowlisted URL (or URL pattern) on www.nseindia.com / nsearchives.nseindia.com,
with no redirects, a size limit and a timeout. NSE's JSON API answers scripts on www.nseindia.in (the .com host
sends them a 301), so the request goes to the exact .in twin of the allowlisted .com URL; reports cite the .com address. Nothing here logs in, retries aggressively or places orders.
"""
from __future__ import annotations

import argparse
import calendar
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, wait
import csv
from datetime import date, datetime, timedelta
import http.client
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from core import Block, IST, instant, number, positive

LIVE_HOST = "www.nseindia.com"
API_HOST = "www.nseindia.in"  # NSE serves its JSON API to scripts here; www.nseindia.com answers them with a 301
ARCHIVE_HOST = "nsearchives.nseindia.com"
ALL_INDICES = "https://www.nseindia.com/api/allIndices"
HOLIDAYS = "https://www.nseindia.com/api/holiday-master?type=trading"
LIVE_PAGE = "https://www.nseindia.com/market-data/live-market-indices"
HOLIDAY_PAGE = "https://www.nseindia.com/resources/exchange-communication-holidays"
INDEX_CLOSE = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{}.csv"
INDEX_CLOSE_RE = re.compile(r"https://nsearchives\.nseindia\.com/content/indices/ind_close_all_\d{8}\.csv")
BHAVCOPY = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{}.csv"
BHAVCOPY_RE = re.compile(r"https://nsearchives\.nseindia\.com/products/content/sec_bhavdata_full_\d{8}\.csv")
NIFTY50_LIST = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
NIFTYBANK_LIST = "https://nsearchives.nseindia.com/content/indices/ind_niftybanklist.csv"
INDEX_LIST = "https://nsearchives.nseindia.com/content/indices/ind_{}list.csv"
INDEX_LIST_RE = re.compile(r"https://nsearchives\.nseindia\.com/content/indices/ind_[a-z0-9_]{2,40}list\.csv")
NSDL_MONTHLY = "https://www.fpi.nsdl.co.in/web/Reports/Monthly.aspx"  # NSDL: daily FPI investment, current month
NSDL_HOST = "www.fpi.nsdl.co.in"
LIMIT = 4 * 1024 * 1024
TIMEOUT = 15
USER_AGENT = "IndiaDayTrader personal research"
YAHOO = {"NIFTY 50": "^NSEI", "NIFTY BANK": "^NSEBANK", "INDIA VIX": "^INDIAVIX", "SENSEX": "^BSESN"}
KEYS = {"nifty": "NIFTY 50", "banknifty": "NIFTY BANK", "vix": "INDIA VIX", "sensex": "SENSEX"}
EXPIRY_RULE = ("NSE index F&O expiries fall on Tuesday (Nifty 50 weekly and monthly; Bank Nifty monthly only), "
               "moved to the previous trading day when Tuesday is a holiday")

_CACHE: dict = {}


class Missing(Block):
    """The exchange has no file for that date (HTTP 404): usually a holiday or a report not yet published."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Block("NSE: redirect refused")


def allowed(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment:
        return False
    if parsed.hostname == LIVE_HOST:
        return url in (ALL_INDICES, HOLIDAYS)
    if parsed.hostname == NSDL_HOST:
        return url == NSDL_MONTHLY
    return parsed.hostname == ARCHIVE_HOST and (url in (NIFTY50_LIST, NIFTYBANK_LIST) or bool(
        INDEX_CLOSE_RE.fullmatch(url) or BHAVCOPY_RE.fullmatch(url) or INDEX_LIST_RE.fullmatch(url)))


CALL_BUDGET = 25.0  # seconds for one main-thread call to a source whose own reads are not bounded in total (Yahoo)


def bounded_call(fn, budget, *args, **kw):
    """fn(*args, **kw) on a worker thread; Block if it has not answered within ``budget`` seconds. The worker is left
    behind (the CLIs exit without joining it), so a stalled source becomes a stated gap instead of a hung report."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn, *args, **kw).result(timeout=budget)
    except FuturesTimeout:
        raise Block("SOURCE: no answer within the time limit") from None
    finally:
        pool.shutdown(wait=False)


def yahoo_get(url):
    """Yahoo's public request, bounded in total time (see bounded_call)."""
    from public_market import request
    return bounded_call(request, CALL_BUDGET, url)


TOTAL_FACTOR = 3  # a whole download may take at most this many per-read timeouts


def read_within(response, limit, total):
    """The response body read in chunks, giving up after ``total`` seconds: a socket timeout bounds each read, not the
    download, so a server dripping bytes could otherwise hold a report open for minutes (seen: one run past 300 s)."""
    start, parts, size = time.monotonic(), [], 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            return b"".join(parts)
        parts.append(chunk); size += len(chunk)
        if size > limit:
            return b"".join(parts)  # the caller's size check rejects it
        if time.monotonic() - start > total:
            raise Block("SOURCE: download too slow; stopped at the time limit")


def fetch(url, timeout=None):
    """GET one allowlisted NSE URL. HTTP 404 raises Missing; every other failure (including a body cut short,
    http.client.IncompleteRead) raises Block."""
    if not allowed(url):
        raise Block("NSE: unsupported endpoint")
    target = url.replace("://" + LIVE_HOST + "/api/", "://" + API_HOST + "/api/", 1)  # exact twin of an allowlisted URL
    req = urllib.request.Request(target, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/csv,text/html"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout or TIMEOUT) as response:
            body = read_within(response, LIMIT, (timeout or TIMEOUT) * TOTAL_FACTOR)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise Missing("NSE: no file for that date") from None
        raise Block("NSE: source refused the request") from None
    except (OSError, ValueError, http.client.HTTPException):
        raise Block("NSE: source unavailable") from None
    if len(body) > LIMIT:
        raise Block("NSE: oversized response")
    return body


def cached(fetcher, url):
    """One download per URL per run (per fetcher), shared by every report built in this process."""
    key = (fetcher, url)  # the fetcher itself (not id()) so a recycled id can never serve another source's data
    if key not in _CACHE:
        try:
            _CACHE[key] = ("ok", fetcher(url))
        except Missing as exc:
            _CACHE[key] = ("missing", exc)
    kind, value = _CACHE[key]
    if kind == "missing":
        raise value
    return value


def clear_cache():
    _CACHE.clear()


def _json(raw):
    if not isinstance(raw, (bytes, str)) or not raw:
        raise Block("NSE: empty response")
    return json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(Block("NSE: nonfinite JSON")))


def _float(value, minimum="-1e30"):
    return float(number(value, minimum=minimum))


def _nse_time(text):
    text = str(text).strip()
    for pattern in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=IST)
        except ValueError:
            continue
    raise Block("NSE: unrecognised timestamp")


# ---------------------------------------------------------------- live index levels

def parse_all_indices(raw, now):
    data = _json(raw)
    rows = data.get("data")
    if not isinstance(rows, list) or not rows:
        raise Block("NSE: index list missing")
    at = _nse_time(data["timestamp"])
    if (at - now).total_seconds() > 120:
        raise Block("NSE: future index timestamp")
    indices, rejected, stale_dates = {}, [], []
    for row in rows:
        try:
            name = str(row["index"]).strip().upper()
            last, previous = float(positive(row["last"])), float(positive(row["previousClose"]))
            change, pct = _float(row["variation"]), _float(row["percentChange"])
            # NSE computes percentChange from unrounded values; a 2-decimal level (India VIX ~15) can differ by ~0.03 pct.
            if abs((last - previous) - change) > max(0.05, previous * 1e-5) or abs((last / previous - 1) * 100 - pct) > 0.02 + 1.0 / previous:
                raise Block("NSE: index change arithmetic mismatch")
            item = {"name": name, "last": last, "change": round(change, 2), "change_pct": pct, "previous_close": previous,
                    "previous_day": datetime.strptime(row["previousDay"], "%d-%b-%Y").date().isoformat() if row.get("previousDay") else None,
                    "group": str(row.get("key", "")).strip()}
            for key in ("open", "high", "low"):
                value = row.get(key)
                item[key] = float(positive(value)) if value not in (None, "", 0, "0", "-") else None
            if None not in (item["high"], item["low"]) and not item["low"] - 0.01 <= last <= item["high"] + 0.01:
                raise Block("NSE: index level outside its day range")
            for key in ("advances", "declines", "unchanged"):
                value = row.get(key)
                item[key] = int(number(value)) if value not in (None, "", "-") else None
            for key, column in (("year_high", "yearHigh"), ("year_low", "yearLow")):  # 52-week range (NSE)
                try: item[key] = float(positive(row[column])) if row.get(column) not in (None, "", 0, "0", "-") else None
                except (Block, TypeError, ValueError): item[key] = None
            if item["previous_day"] and item["previous_day"] >= at.date().isoformat():
                # In the evening NSE's snapshot relabels previousDay as today while previousClose is still the previous
                # session's close (18 Nov 2026 after ~17:30: previousDay 18-Nov-2026, previousClose = the 17 Nov close).
                # Such a date would point the reports at today's bhavcopy as "previous"; callers use the calendar instead.
                item["previous_day"] = None
                stale_dates.append(name)
            if name not in indices:  # the same index can appear under two groups; keep the first
                indices[name] = item
        except (Block, KeyError, TypeError, ValueError):
            rejected.append(str(row.get("index", "?")) if isinstance(row, dict) else "?")
    if not indices:
        raise Block("NSE: no valid index rows")
    breadth = {k: int(number(data[k])) for k in ("advances", "declines", "unchanged") if data.get(k) not in (None, "")}
    return {"schema": "nse_live_indices_v1", "source": ALL_INDICES, "page": LIVE_PAGE, "provider": "NSE",
            "as_of": at.isoformat(), "retrieved_at": now.isoformat(), "age_seconds": max(0.0, (now - at).total_seconds()),
            "timestamp_basis": "NSE snapshot time (minute resolution, IST)", "indices": indices,
            "rejected_rows": rejected, "market_breadth": breadth or None, "previous_day_relabelled": bool(stale_dates)}


def live_indices(*, fetcher=fetch, clock=instant):
    now = clock()
    return parse_all_indices(cached(fetcher, ALL_INDICES), now)


def _day_open(result, at):
    """Today's open from the daily candle in the same Yahoo response (interval=1d), or None. Only a candle dated
    the quote's own day counts, so a stale candle never lends its open to today's range."""
    try:
        stamps, opens = result["timestamp"], result["indicators"]["quote"][0]["open"]
        if not stamps or datetime.fromtimestamp(int(stamps[-1]), IST).date() != at.date() or opens[-1] is None:
            return None
        return float(positive(opens[-1]))
    except (Block, KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None


def _previous_candle(result, at):
    """(date, close) of the newest daily candle dated before the quote's own day, or None. Yahoo's chartPreviousClose is
    not used when daily candles are present: on 18 Nov 2026 its range=1d value for ^BSESN and ^INDIAVIX was the 16 Nov
    close, two sessions back, which turned a 500-point Sensex fall into -73 points."""
    try:
        rows = [(datetime.fromtimestamp(int(t), IST).date(), c) for t, c in zip(result["timestamp"], result["indicators"]["quote"][0]["close"])]
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None
    before = [(d, float(c)) for d, c in rows if d < at.date() and isinstance(c, (int, float)) and c > 0]
    return before[-1] if before else None


def yahoo_quote(name, *, fetcher=None, clock=instant):
    """Fallback: Yahoo chart meta.regularMarketPrice with its own trade time; unofficial and possibly delayed. The previous
    close is the close of the last daily candle before the quote's day (five daily candles are requested); Yahoo's
    chartPreviousClose is used only when the response carries no earlier candle."""
    symbol = YAHOO[name]
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol, safe="") + "?range=5d&interval=1d"
    result = _json((fetcher or yahoo_get)(url))["chart"]["result"][0]
    meta = result["meta"]
    if meta.get("symbol") != symbol or meta.get("currency") != "INR":
        raise Block("YAHOO: wrong instrument")
    now = clock(); at = datetime.fromtimestamp(int(meta["regularMarketTime"]), IST)
    if (at - now).total_seconds() > 120:
        raise Block("YAHOO: future quote time")
    last = float(positive(meta["regularMarketPrice"]))
    candle = _previous_candle(result, at)
    if candle:
        previous, previous_day, basis = float(positive(candle[1])), candle[0].isoformat(), "close of the previous daily candle"
    elif len(result.get("timestamp") or []) <= 1:
        previous, previous_day, basis = float(positive(meta["chartPreviousClose"])), None, "Yahoo chartPreviousClose"
    else:
        raise Block("YAHOO: previous close not identifiable")
    high, low = meta.get("regularMarketDayHigh"), meta.get("regularMarketDayLow")
    return {"name": name, "last": last, "previous_close": previous, "previous_day": previous_day, "previous_basis": basis,
            "change": round(last - previous, 2),
            "change_pct": round((last / previous - 1) * 100, 2), "high": float(positive(high)) if high else None,
            "low": float(positive(low)) if low else None, "open": _day_open(result, at), "as_of": at.isoformat(), "provider": "Yahoo",
            "source": "https://finance.yahoo.com/quote/" + urllib.parse.quote(symbol, safe="") + "/",
            "timestamp_basis": "Yahoo regularMarketTime (last trade time; may be delayed)"}


def quote(name, *, snapshot=None, fetcher=fetch, yahoo_fetcher=None, clock=instant):
    """Latest level for one index: NSE first (labelled), Yahoo meta price if NSE refuses. Never raises for NSE failure.

    ``snapshot``: a parsed allIndices result to reuse, None to fetch one, or False when NSE already failed this run."""
    name = KEYS.get(name, name)
    reason = None
    if name != "SENSEX" and snapshot is not False:
        try:
            snap = snapshot or live_indices(fetcher=fetcher, clock=clock)
            row = snap["indices"].get(name)
            if row is not None:
                return {**row, "as_of": snap["as_of"], "provider": "NSE", "source": snap["page"], "timestamp_basis": snap["timestamp_basis"]}
            reason = "index not in NSE snapshot"
        except (Block, KeyError, TypeError, ValueError, OSError):
            reason = "NSE live index data unavailable"
    result = yahoo_quote(name, fetcher=yahoo_fetcher, clock=clock)
    if reason:
        result["fallback_reason"] = reason
    return result


# ---------------------------------------------------------------- trading holidays and expiries

def parse_holidays(raw, now):
    data = _json(raw)
    segments = {}
    for key in ("CM", "FO"):
        rows = data.get(key)
        if not isinstance(rows, list) or not rows:
            raise Block("HOLIDAYS: CM and FO lists required")
        listed = {}
        for row in rows:
            day = datetime.strptime(row["tradingDate"], "%d-%b-%Y").date()
            text = str(row.get("description") or "").strip()
            if not text or day in listed:
                raise Block("HOLIDAYS: duplicate or unnamed holiday")
            listed[day.isoformat()] = text
        segments[key] = listed
    years = sorted({int(d[:4]) for seg in segments.values() for d in seg})
    special = sorted(d for d, text in segments["CM"].items() if "*" in text or "muhurat" in text.lower())
    return {"schema": "nse_holidays_v1", "source": HOLIDAYS, "page": HOLIDAY_PAGE, "retrieved_at": now.isoformat(),
            "years": years, "segments": segments, "special_sessions": special,
            "special_session_basis": "Holiday names marked * are special (e.g. Muhurat) trading sessions; timings come from NSE circulars",
            "coverage": "NSE CM and F&O trading holidays for the listed year(s); ad-hoc closures are not included"}


def holiday_calendar(*, fetcher=fetch, clock=instant):
    now = clock()
    return parse_holidays(cached(fetcher, HOLIDAYS), now)


def covered(cal, day):
    return day.year in cal["years"]


def holiday_name(cal, day, segment="CM"):
    text = cal["segments"][segment].get(day.isoformat())
    return None if text is None or day.isoformat() in cal["special_sessions"] else text


def is_trading_day(cal, day, segment="CM"):
    """Regular weekday session not listed as a holiday. Special (Muhurat) sessions are reported separately."""
    if not covered(cal, day):
        raise Block("HOLIDAYS: date outside the published holiday list")
    return day.weekday() < 5 and day.isoformat() not in cal["segments"][segment]


def previous_session(day, *, fetcher=fetch, clock=instant):
    """The trading day before ``day`` from NSE's holiday list; without the list, the previous weekday (labelled by the caller
    as unverified only when that matters)."""
    try:
        return previous_trading_day(holiday_calendar(fetcher=fetcher, clock=clock), day)
    except (Block, KeyError, TypeError, ValueError, OSError):
        probe = day - timedelta(days=1)
        while probe.weekday() >= 5:
            probe -= timedelta(days=1)
        return probe


def next_trading_day(cal, day, segment="CM"):
    probe = day + timedelta(days=1)
    for _ in range(15):
        if is_trading_day(cal, probe, segment):
            return probe
        probe += timedelta(days=1)
    raise Block("HOLIDAYS: no trading day found")


def previous_trading_day(cal, day, segment="CM"):
    probe = day - timedelta(days=1)
    for _ in range(15):
        if is_trading_day(cal, probe, segment):
            return probe
        probe -= timedelta(days=1)
    raise Block("HOLIDAYS: no trading day found")


def _expiry(cal, nominal):
    """Shift a nominal Tuesday expiry back over F&O holidays and weekends."""
    actual = nominal
    while not is_trading_day(cal, actual, "FO"):
        actual -= timedelta(days=1)
    return actual


def last_weekday(year, month, weekday):
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def upcoming_expiries(cal, today, count=2):
    """Next Nifty weekly expiry and the current/next monthly expiry, flagged when a holiday moves them."""
    rows, nominal = [], today + timedelta(days=(1 - today.weekday()) % 7)
    while len([r for r in rows if r["kind"] == "weekly"]) < count:
        if covered(cal, nominal):
            actual = _expiry(cal, nominal)
            if actual >= today:
                monthly = nominal == last_weekday(nominal.year, nominal.month, 1)
                rows.append({"kind": "monthly" if monthly else "weekly", "contracts": "Nifty 50 and Bank Nifty monthly" if monthly else "Nifty 50 weekly",
                             "nominal": nominal.isoformat(), "date": actual.isoformat(), "moved": actual != nominal,
                             "reason": cal["segments"]["FO"].get(nominal.isoformat())})
        else:
            break
        nominal += timedelta(days=7)
    month = last_weekday(today.year, today.month, 1)
    if month < today or not covered(cal, month):
        nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        month = last_weekday(nxt.year, nxt.month, 1)
    if covered(cal, month) and not any(r["nominal"] == month.isoformat() for r in rows):
        actual = _expiry(cal, month)
        rows.append({"kind": "monthly", "contracts": "Nifty 50 and Bank Nifty monthly", "nominal": month.isoformat(), "date": actual.isoformat(),
                     "moved": actual != month, "reason": cal["segments"]["FO"].get(month.isoformat())})
    return sorted(rows, key=lambda r: r["date"])


def upcoming_holidays(cal, today, days=21):
    end = today + timedelta(days=days)
    rows = []
    for text_day, name in sorted(cal["segments"]["CM"].items()):
        day = date.fromisoformat(text_day)
        if today <= day <= end:
            rows.append({"date": text_day, "name": name, "weekday": f"{day:%A}", "special_session": text_day in cal["special_sessions"],
                         "on_weekend": day.weekday() >= 5})
    return rows


def calendar_summary(cal, today):
    """Plain dated calendar facts for a report: today's status, next trading day, holidays and expiries."""
    out = {"source": cal["page"], "retrieved_at": cal["retrieved_at"], "today": today.isoformat(), "notes": []}
    if not covered(cal, today):
        out["notes"].append(f"NSE holiday list covers {', '.join(map(str, cal['years']))} only.")
        return out
    out["today_holiday"] = holiday_name(cal, today)
    out["today_is_trading_day"] = is_trading_day(cal, today)
    try:
        out["next_trading_day"] = next_trading_day(cal, today).isoformat()
        out["previous_trading_day"] = previous_trading_day(cal, today).isoformat()
    except Block:
        out["notes"].append("Next trading day falls outside the published holiday list.")
    out["holidays"] = [h for h in upcoming_holidays(cal, today) if not h["on_weekend"] or h["special_session"]]
    try:
        out["expiries"] = upcoming_expiries(cal, today)
        out["expiry_rule"] = EXPIRY_RULE
    except Block:
        out["expiries"] = []
    return out


def _short(day):
    d = date.fromisoformat(day); return f"{d:%a} {d.day} {d:%b}"


def calendar_lines(summary, weekly=True):
    """Short sentences for reports: holidays and moved expiries only (no boilerplate when nothing is unusual).

    ``weekly=False`` (a Bank Nifty-only report) drops Nifty 50 weekly-expiry lines: Bank Nifty has monthly expiries only."""
    lines = []
    for h in summary.get("holidays", []):
        if h["special_session"]:
            lines.append(f"{_short(h['date'])}: special trading session ({h['name'].rstrip('*').strip()}); timings per NSE circular.")
        else:
            lines.append(f"{_short(h['date'])}: NSE closed ({h['name']}).")
    expiries = [e for e in summary.get("expiries", []) if weekly or e["kind"] == "monthly"]
    for e in expiries:
        if e["moved"]:
            lines.append(f"{e['contracts'][0].upper() + e['contracts'][1:]} expiry moves from {_short(e['nominal'])} ({e['reason'] or 'holiday'}) to {_short(e['date'])}.")
    nxt = [e for e in expiries if not e["moved"]][:1]
    if nxt:
        lines.append(f"Next {nxt[0]['kind']} expiry: {_short(nxt[0]['date'])} ({nxt[0]['contracts']}).")
    return lines


# ---------------------------------------------------------------- dated index closes

def parse_index_closes(raw, day):
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else str(raw)
    reader = csv.DictReader(io.StringIO(text), skipinitialspace=True)
    need = {"Index Name", "Index Date", "Open Index Value", "High Index Value", "Low Index Value", "Closing Index Value", "Points Change", "Change(%)"}
    if not reader.fieldnames or not need <= set(reader.fieldnames):
        raise Block("NSE: unexpected index-close columns")
    out = {}
    for row in reader:
        try:
            if datetime.strptime(row["Index Date"].strip(), "%d-%m-%Y").date() != day:
                raise Block("NSE: index report date mismatch")
            name = row["Index Name"].strip().upper()
            close = float(positive(row["Closing Index Value"]))
            values = {}
            for key, column in (("open", "Open Index Value"), ("high", "High Index Value"), ("low", "Low Index Value")):
                try: values[key] = float(positive(row[column]))
                except Block: values[key] = None
            change = _float(row["Points Change"]); pct = _float(row["Change(%)"])
            for key, column in (("pe", "P/E"), ("pb", "P/B"), ("div_yield", "Div Yield")):  # valuation, when NSE prints it
                try:
                    v = float(str(row.get(column) or "").strip())
                    if 0 < v < 1000: values[key] = v
                except ValueError:
                    pass
        except Block as exc:
            if "date mismatch" in str(exc): raise
            continue
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if name not in out:
            out[name] = {"session": day.isoformat(), "close": close, "change": change, "change_pct": pct, **values}
    if not out:
        raise Block("NSE: empty index-close report")
    return out


def index_closes(day, *, fetcher=fetch):
    return parse_index_closes(cached(fetcher, INDEX_CLOSE.format(day.strftime("%d%m%Y"))), day)


def candidate_sessions(cal, end, needed):
    """Likely session dates ending at ``end`` (newest first): weekdays not listed as CM holidays, plus special sessions."""
    days, probe = [], end
    while len(days) < needed and (end - probe).days < 120:
        text = probe.isoformat()
        listed = cal is not None and text in cal["segments"]["CM"]
        if text in (cal or {}).get("special_sessions", []) or (probe.weekday() < 5 and not listed):
            days.append(probe)
        probe -= timedelta(days=1)
    return days


HISTORY_TIMEOUT = 8     # seconds per daily file inside index_history
HISTORY_BUDGET = 30.0   # seconds for the whole 20-day history before it gives up on slow files
CACHE_ENV = "INDIA_DAY_TRADER_CACHE"


_LIVE_FETCH = fetch  # the real network fetch; a patched or injected fetcher never touches the disk cache


def fetch_history(url):
    """The live NSE fetch with the short per-file timeout used for the daily index-close history."""
    return _LIVE_FETCH(url, timeout=HISTORY_TIMEOUT)


def cache_dir():
    """Per-user cache folder outside the plugin: $INDIA_DAY_TRADER_CACHE ('off' disables), else
    %LOCALAPPDATA%/india-day-trader/cache on Windows, else $XDG_CACHE_HOME or ~/.cache /india-day-trader."""
    env = os.environ.get(CACHE_ENV, "").strip()
    if env:
        return None if env.lower() in ("off", "0", "none", "false") else Path(env)
    try:
        base = (os.environ.get("LOCALAPPDATA") if os.name == "nt" else None) or os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    except (OSError, RuntimeError):
        return None
    return Path(base) / "india-day-trader" / "cache"


def _cache_path(folder, day):
    return Path(folder) / "nse_ind_close_all" / f"ind_close_all_{day.strftime('%d%m%Y')}.csv"


def cached_index_closes(day, *, fetcher=fetch, folder=None, today=None):
    """``index_closes`` backed by an on-disk copy. A published ind_close_all file never changes, so a file for a day
    before ``today`` that parsed cleanly is kept and reused; a damaged cached copy is ignored and fetched again."""
    path = _cache_path(folder, day) if folder else None
    if path is not None:
        try:
            return parse_index_closes(path.read_bytes(), day)
        except (OSError, Block, ValueError, UnicodeError, csv.Error):
            pass
    raw = cached(fetcher, INDEX_CLOSE.format(day.strftime("%d%m%Y")))
    rows = parse_index_closes(raw, day)
    if path is not None and day < (today or datetime.now(IST).date()):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp%d" % os.getpid())
            tmp.write_bytes(raw if isinstance(raw, bytes) else str(raw).encode("utf-8"))
            os.replace(tmp, path)
        except OSError:
            pass  # a read-only or full disk only costs a re-download next run
    return rows


RETRY_DELAYS = (1.0, 2.5)   # seconds before the 2nd and 3rd try of a refused or dropped archive download
BHAVCOPY_TIMEOUT = 20      # the full cash bhavcopy is ~0.5 MB


def is_live(fetcher):
    """True for the real NSE network fetch (or None, meaning it); False for an injected or patched fetcher."""
    return fetcher is None or fetcher is _LIVE_FETCH or fetcher is _bhav_fetch or fetcher is fetch_history


def retry_delays(fetcher):
    """Backoff only against the real network: an injected fetcher (a test fake, a replay) is retried at once, so a
    refusal costs no wall-clock time there."""
    return RETRY_DELAYS if is_live(fetcher) else tuple(0.0 for _ in RETRY_DELAYS)


def fetch_retry(fetcher, url, *, delays=None, sleep=time.sleep):
    """``fetcher(url)`` retried after each delay when the source refuses or drops the request (Block); a 404 (Missing)
    is final at once. NSE's archive host intermittently refuses a request that succeeds a second later. A delay of 0
    retries without sleeping."""
    delays = RETRY_DELAYS if delays is None else delays
    for wait_s in (*delays, None):
        try:
            return fetcher(url)
        except Missing:
            raise
        except Block:
            if wait_s is None:
                raise
            if wait_s > 0:
                sleep(wait_s)


def _bhavcopy_path(folder, day):
    return Path(folder) / "nse_bhavcopy" / f"sec_bhavdata_full_{day.strftime('%d%m%Y')}.csv"


def _bhavcopy_ok(raw, day):
    """A cash bhavcopy for exactly ``day``: the expected header and that date on its first data rows."""
    try:
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else str(raw)
    except UnicodeError:
        return False
    lines = text.splitlines()
    head = [c.strip() for c in lines[0].split(",")] if lines else []
    if not {"SYMBOL", "SERIES", "DATE1", "PREV_CLOSE", "CLOSE_PRICE"} <= set(head) or len(lines) < 2:
        return False
    at = head.index("DATE1"); stamp = day.strftime("%d-%b-%Y")
    rows = [ln.split(",") for ln in lines[1:6] if ln.strip()]
    return bool(rows) and all(len(r) > at and r[at].strip() == stamp for r in rows)


def _bhav_fetch(url):
    return _LIVE_FETCH(url, timeout=BHAVCOPY_TIMEOUT)


def cached_bhavcopy(day, *, fetcher=None, folder=None, today=None, sleep=time.sleep, delays=None):
    """Raw ``sec_bhavdata_full`` cash bhavcopy for ``day``: from this run's memory, then the on-disk copy (only for
    the live NSE fetch, or an explicit ``folder``), then the network with retries (``fetch_retry``). A bhavcopy for a
    day before ``today`` never changes, so a clean copy is kept on disk and later runs need no download at all.
    HTTP 404 raises Missing (not published yet, or a holiday); repeated refusals raise Block."""
    live = is_live(fetcher)
    get = _bhav_fetch if live else fetcher
    if folder is None and live:
        folder = cache_dir()
    if delays is None:
        delays = retry_delays(get)
    url = BHAVCOPY.format(day.strftime("%d%m%Y"))
    key = (fetcher or _LIVE_FETCH, url)
    if key in _CACHE:
        kind, value = _CACHE[key]
        if kind == "missing": raise value
        return value
    path = _bhavcopy_path(folder, day) if folder else None
    if path is not None:
        try:
            raw = path.read_bytes()
            if _bhavcopy_ok(raw, day):
                _CACHE[key] = ("ok", raw)
                return raw
        except OSError:
            pass
    try:
        raw = fetch_retry(get, url, delays=delays, sleep=sleep)
    except Missing as exc:
        _CACHE[key] = ("missing", exc)
        raise
    if not _bhavcopy_ok(raw, day):
        raise Block("NSE: unexpected bhavcopy content")
    _CACHE[key] = ("ok", raw)
    if path is not None and day < (today or datetime.now(IST).date()):
        _write(path, raw)
    return raw


def _write(path, raw):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp%d" % os.getpid())
        tmp.write_bytes(raw if isinstance(raw, bytes) else str(raw).encode("utf-8"))
        os.replace(tmp, path)
    except OSError:
        pass  # a read-only or full disk only costs a re-download next run


LIST_MAX_AGE_DAYS = 7  # NSE rebalances indices twice a year (with notice); a week-old constituent list is current enough


def parse_members(raw, low=2, high=600):
    """[{symbol, name, industry}] from an NSE ``ind_<index>list.csv`` (EQ series only, no DUMMY placeholders)."""
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else str(raw)
    rows = list(csv.DictReader(io.StringIO(text), skipinitialspace=True))
    members = [{"symbol": r["Symbol"].strip(), "name": (r.get("Company Name") or "").strip(), "industry": (r.get("Industry") or "").strip()}
               for r in rows if r.get("Symbol") and (r.get("Series") or "EQ").strip() == "EQ"
               and not r["Symbol"].strip().upper().startswith("DUMMY") and not (r.get("ISIN Code") or "").strip().upper().startswith("DUM")]
    if not low <= len(members) <= high or len({m["symbol"] for m in members}) != len(members):
        raise Block("NSE: unexpected index constituent list")
    return members


def index_members(slug, *, fetcher=None, folder=None, today=None, low=2, high=600):
    """Constituents of one NSE index from ``ind_<slug>list.csv`` (the files the screener uses), with the list's date:
    a disk copy younger than LIST_MAX_AGE_DAYS is reused; otherwise it is downloaded (with retries) and saved; if the
    download fails an older disk copy is used and labelled. Returns {"members", "source", "as_of", "basis"}."""
    url = INDEX_LIST.format(slug)
    if not INDEX_LIST_RE.fullmatch(url):
        raise Block("NSE: unsupported index list")
    live = is_live(fetcher)
    get = _LIVE_FETCH if live else fetcher
    if folder is None and live:
        folder = cache_dir()
    today = today or datetime.now(IST).date()
    path = Path(folder) / "nse_index_lists" / f"ind_{slug}list.csv" if folder else None
    stale = None
    if path is not None:
        try:
            saved = date.fromtimestamp(path.stat().st_mtime)
            members = parse_members(path.read_bytes(), low, high)
            if (today - saved).days <= LIST_MAX_AGE_DAYS:
                return {"members": members, "source": url, "as_of": saved.isoformat(), "basis": "NSE constituent list (cached copy)"}
            stale = (members, saved)
        except (OSError, Block, KeyError, ValueError, UnicodeError, csv.Error):
            pass
    try:
        key = (get, url)
        if key not in _CACHE:
            _CACHE[key] = ("ok", fetch_retry(get, url, delays=retry_delays(get)))
        raw = _CACHE[key][1]
        members = parse_members(raw, low, high)
    except Missing:
        raise
    except (Block, KeyError, ValueError, UnicodeError, csv.Error):
        if stale:
            return {"members": stale[0], "source": url, "as_of": stale[1].isoformat(),
                    "basis": f"NSE constituent list (cached copy of {stale[1]:%d %b %Y}; today's download failed)"}
        raise
    if path is not None:
        _write(path, raw)
    return {"members": members, "source": url, "as_of": today.isoformat(), "basis": "NSE constituent list"}


# ---------------------------------------------------------------- FPI flows (NSDL)

_CELL = r"\s*<td[^>]*>\s*([()\d.,-]+)\s*</td>"
NSDL_ROW = re.compile(r"<td[^>]*>\s*(\d{2}-[A-Za-z]{3}-\d{4})\s*</td>\s*<td[^>]*>\s*Equity\s*</td>\s*<td[^>]*>\s*Stock Exchange\s*</td>"
                      + _CELL * 3 + r"(?:(?!<td[^>]*>\s*\d{2}-[A-Za-z]{3}-\d{4}).)*?<td[^>]*>\s*Sub-total\s*</td>" + _CELL * 3, re.S | re.I)


def _nsdl_number(text):
    text = text.strip().replace(",", "")
    neg = text.startswith("(") and text.endswith(")")
    value = float(text.strip("()"))
    return -value if neg else value


def parse_fpi_month(raw, now):
    """Daily FPI equity investment by NSDL reporting date from Monthly.aspx, oldest first: [{reporting_date,
    exchange_net_crore (stock-exchange route), equity_net_crore (with primary market)}]. A reporting date carries the
    FPIs' trades of the previous trading day(s)."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    rows, seen = [], set()
    for m in NSDL_ROW.finditer(text):
        day = datetime.strptime(m.group(1), "%d-%b-%Y").date()
        if day > now.date() or day in seen:
            raise Block("NSDL: future or repeated reporting date")
        buy, sell, net = (_nsdl_number(m.group(i)) for i in (2, 3, 4))
        if abs(buy - sell - net) > 0.05:
            raise Block("NSDL: net does not equal purchases minus sales")
        seen.add(day)
        rows.append({"reporting_date": day.isoformat(), "exchange_buy_crore": buy, "exchange_sell_crore": sell, "exchange_net_crore": net,
                     "equity_net_crore": _nsdl_number(m.group(7))})
    if not rows:
        raise Block("NSDL: no daily equity rows")
    rows.sort(key=lambda r: r["reporting_date"])
    if len({r["reporting_date"][:7] for r in rows}) != 1:
        raise Block("NSDL: rows from more than one month")
    return {"source": NSDL_MONTHLY, "retrieved_at": now.isoformat(), "rows": rows,
            "basis": ("NSDL daily trends in FPI investment (equity); each reporting date covers FPI trades of the previous "
                      "trading day(s), so it lags NSE's provisional same-day figure by a day and differs from it")}


def fpi_month(*, fetcher=fetch, clock=instant):
    return parse_fpi_month(cached(fetcher, NSDL_MONTHLY), clock())


def index_history(names, end, count=21, *, cal=None, fetcher=fetch, workers=4, cache=None, budget=None, clock=time.monotonic):
    """Official NSE daily closes for ``names`` (upper-case NSE names) up to and including ``end``, oldest first.

    Fetches about ``count`` files in parallel (at most ``workers`` at a time). Dates with no file (unlisted
    holidays, not-yet-published days) are skipped and the window walks further back. A file that fails or times out
    is retried once, then skipped (listed in ``dates_failed``) instead of aborting the whole history; the run stops
    waiting after ``budget`` seconds (default ``HISTORY_BUDGET``). ``cache``: a folder for on-disk copies (None: the
    user cache folder when ``fetcher`` is the live NSE fetch, nothing for injected fetchers; False: no disk cache)."""
    names = [n.upper() for n in names]
    live = fetcher is _LIVE_FETCH
    get = fetch_history if live else fetcher
    folder = (cache_dir() if live else None) if cache is None else (cache or None)
    deadline = clock() + (HISTORY_BUDGET if budget is None else budget)
    found, tried, missing, failed = {}, set(), [], {}

    def load(day):
        try:
            return day, "ok", cached_index_closes(day, fetcher=get, folder=folder)
        except Missing:
            return day, "missing", None
        except (Block, OSError, ValueError, KeyError, TypeError, UnicodeError, csv.Error, http.client.HTTPException):
            return day, "failed", None

    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, 6)))
    try:
        probe_end, retried = end, False
        for _ in range(8):
            if clock() >= deadline:
                break
            need = count - len(found)
            batch = [d for d in candidate_sessions(cal, probe_end, need + 2) if d not in tried] if need > 0 else []
            if batch:
                probe_end = min(batch) - timedelta(days=1)
            elif failed and not retried:
                retried = True  # one more try for the files that failed or timed out
                batch = sorted(failed, reverse=True); failed.clear()
            else:
                break
            tried.update(batch)
            futures = [pool.submit(load, d) for d in batch]
            done, pending = wait(futures, timeout=max(0.0, deadline - clock()))
            for f in done:
                day, kind, rows = f.result()
                if kind == "ok": found[day] = rows
                elif kind == "missing": missing.append(day.isoformat())
                else: failed[day] = "failed"
            for f in pending:
                f.cancel()
            for day, f in zip(batch, futures):
                if f in pending: failed[day] = "timed out"
            if pending:
                break  # out of time
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    days = sorted(found)[-count:]
    if not days:
        raise Block("NSE: no index-close files found")
    series = {}
    for name in names:
        series[name] = [{**found[d][name]} for d in days if name in found[d]]
    return {"schema": "nse_index_history_v1", "basis": "NSE ind_close_all daily index files", "end": end.isoformat(),
            "sessions": [d.isoformat() for d in days], "series": series, "dates_without_file": sorted(missing),
            "dates_failed": sorted(d.isoformat() for d in failed if d >= days[0]),
            "special_sessions_included": [d.isoformat() for d in days if cal and d.isoformat() in cal["special_sessions"]],
            "source_pattern": INDEX_CLOSE.format("DDMMYYYY")}


# ---------------------------------------------------------------- command line

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("command", choices=["live", "holidays", "history"])
    p.add_argument("--index", nargs="+", default=["NIFTY 50", "NIFTY BANK", "INDIA VIX"])
    p.add_argument("--count", type=int, default=21)
    a = p.parse_args(argv)
    try:
        now = instant()
        if a.command == "live":
            snap = live_indices()
            result = {k: v for k, v in snap.items() if k != "indices"}
            result["indices"] = {n: snap["indices"].get(n.upper()) for n in a.index}
        elif a.command == "holidays":
            cal = holiday_calendar()
            result = calendar_summary(cal, now.date())
        else:
            if not 1 <= a.count <= 60: raise Block("HISTORY: count 1..60")
            cal = holiday_calendar()
            end = now.date() if now.hour >= 18 else now.date() - timedelta(days=1)
            result = index_history(a.index, end, a.count, cal=cal)
    except (Block, KeyError, TypeError, ValueError, OSError) as exc:
        print(json.dumps({"status": "UNAVAILABLE", "reason": "NSE public source unavailable or changed", "detail": type(exc).__name__}))
        return 2
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
