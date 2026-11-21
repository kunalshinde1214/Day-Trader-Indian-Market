"""One-stock research report for Indian equities from free public data.

Combines price/market context, technical condition, financial ratios, recent news
and a statistical outlook. Research only: no orders, no recommendation, no
price target. Every figure carries its source date so stale data stays visible.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date, datetime, timedelta
import functools
import hashlib
import http.client
import inspect
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request

from core import Block, IST, instant
from public_market import chart, request, symbol as check_symbol, NoRedirect, LIMIT, WARNING

ASSETS = Path(__file__).resolve().parents[1] / "assets"
ALIASES_PATH = ASSETS / "symbol_aliases.json"
DEMERGERS_PATH = ASSETS / "demergers.json"
STOPPED_DAYS = 14  # an aliased symbol whose last bar is older than this has stopped trading

FUNDAMENTALS = ("annualTotalRevenue annualNetIncome annualDilutedEPS annualStockholdersEquity annualTotalDebt "
                "annualOrdinarySharesNumber annualOperatingIncome annualEBITDA annualEBIT annualFreeCashFlow annualOperatingCashFlow "
                "annualCurrentAssets annualCurrentLiabilities annualInterestExpense annualCashAndCashEquivalents "
                "annualCashDividendsPaid annualTotalAssets trailingDilutedEPS trailingTotalRevenue trailingNetIncome trailingOperatingIncome "
                "quarterlyTotalRevenue quarterlyNetIncome quarterlyDilutedEPS quarterlyStockholdersEquity quarterlyOrdinarySharesNumber "
                "annualDilutedAverageShares quarterlyDilutedAverageShares quarterlyTotalDebt "
                "quarterlyNetIncomeIncludingNoncontrollingInterests annualCapitalLeaseObligations quarterlyCapitalLeaseObligations "
                # Why a ROE is low: acquisition goodwill and intangibles, or a cash pile, inside shareholders' equity.
                "annualGoodwill annualGoodwillAndOtherIntangibleAssets quarterlyGoodwillAndOtherIntangibleAssets "
                "annualCashCashEquivalentsAndShortTermInvestments quarterlyCashCashEquivalentsAndShortTermInvestments").split()
# Balance-sheet items convert at the rate on their date; flows (revenue, profit, EPS, cash flows) at the period's average rate.
BALANCE_ITEMS = ("StockholdersEquity", "TotalDebt", "CurrentAssets", "CurrentLiabilities", "CashAndCashEquivalents", "TotalAssets",
                 "CapitalLeaseObligations", "Goodwill", "ShortTermInvestments")
PERIOD_DAYS = {"annual": 365, "quarterly": 91, "trailing": 365}
BENCHMARK = "^NSEI"
DISCLAIMER = ("Research summary from free public data, not investment advice or a recommendation. "
              "The outlook shows historical ranges and conditional scenarios, not a prediction.")


def normalize(raw):
    """Accept RELIANCE, reliance, RELIANCE.NS or RELIANCE.BO."""
    text = str(raw).strip().upper()
    if not re.fullmatch(r"[A-Z0-9&_.-]{1,30}", text):
        raise Block("STOCK: use an NSE symbol such as RELIANCE or a Yahoo symbol such as RELIANCE.NS")
    if not text.endswith((".NS", ".BO")):
        text += ".NS"
    return check_symbol(text)


# ---------- renamed symbols and demergers ----------

class SymbolChanged(Block):
    """The symbol no longer trades and was replaced by more than one company; the caller must choose."""

    def __init__(self, entry):
        self.old = entry["old"]
        self.successors = [dict(s) for s in entry["successors"]]
        self.entry = entry
        choices = "; ".join(f"{s['symbol']}: {s['what']}" for s in self.successors)
        super().__init__(f"STOCK: {self.old} no longer trades. {entry['reason']} It became {len(self.successors)} companies: {choices}. "
                         f"Analyse one of them (for example {self.successors[0]['symbol']}) or each in turn. Source: {entry['source']}")


def load_aliases(path=None):
    try:
        data = json.loads(Path(path or ALIASES_PATH).read_text(encoding="utf-8"))
        return {e["old"].upper(): e for e in data.get("aliases", []) if e.get("old") and e.get("successors")}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def alias_for(selected, aliases=None):
    """The alias entry for an NSE symbol (RELIANCE.NS form), or None. BSE codes are not mapped."""
    if not selected.endswith(".NS"):
        return None
    return (load_aliases() if aliases is None else aliases).get(selected[:-3])


def renamed_note(entry, successor):
    return (f"{entry['old']} no longer trades: {entry['reason']} Showing {successor['symbol']} ({successor['what']}). "
            f"Source: {entry['source']}")


def resolve_history(selected, *, fetcher, now, years, aliases=None):
    """Daily history for `selected`; if that fails (or has stopped) and the symbol is a known old symbol, follow it.

    Returns (symbol actually used, history, rename note or None). A symbol that became several companies raises
    SymbolChanged so the caller can ask which one, or analyse each.
    """
    try:
        history, failure = daily_history(selected, fetcher=fetcher, now=now, years=years), None
    except (Block, ValueError, KeyError, TypeError) as error:
        history, failure = None, error
    entry = alias_for(selected, aliases)
    stopped = history is not None and history["bars"][-1]["at"][:10] < (now.date() - timedelta(days=STOPPED_DAYS)).isoformat()
    if not entry or (history is not None and not stopped):
        if failure:
            raise failure
        return selected, history, None
    if len(entry["successors"]) > 1:
        raise SymbolChanged(entry)
    successor = entry["successors"][0]
    new = check_symbol(successor["symbol"] + ".NS")
    try:
        history = daily_history(new, fetcher=fetcher, now=now, years=years)
    except (Block, ValueError, KeyError, TypeError):
        raise Block(f"STOCK: {entry['old']} no longer trades ({entry['reason']}) and the history for its successor {successor['symbol']} "
                    "did not load; try again later") from None
    return new, history, renamed_note(entry, successor)


def load_demergers(path=None):
    try:
        return json.loads(Path(path or DEMERGERS_PATH).read_text(encoding="utf-8")).get("demergers", [])
    except (OSError, ValueError, AttributeError):
        return []


MINOR_SHARE = 10.0  # % of cost going to the demerged company below which the parent is essentially the same business


def parent_events(base, demergers=None):
    return [d for d in (load_demergers() if demergers is None else demergers)
            if d.get("parent") == base or base in (d.get("parent_aliases") or [])]


def resulting_events(base, demergers=None):
    return [d for d in (load_demergers() if demergers is None else demergers)
            if d.get("resulting_symbol") == base or base in [r.get("symbol") for r in d.get("resulting_companies") or []]]


def demerged_share(event):
    """Percent of the parent's cost that went to the demerged business(es), or None when not published."""
    split = event.get("cost_split_pct")
    return float(split["resulting"]) if isinstance(split, dict) and isinstance(split.get("resulting"), (int, float)) else None


def trim_for_demerger(history, selected, demergers=None):
    """Make a demerged parent's price history comparable across its latest demerger, keeping the whole history.

    Yahoo adjusts some demerged parents' past prices (Reliance 2023, ITC 2025, Siemens 2025: no jump on the day) and
    not others (TMPV ₹660.75 → ₹395.45 on 14 Oct 2025; ABFRL −67% on 22 May 2025; VEDL −65% on 30 Apr 2026). When the
    day shows the unadjusted jump, prices (and dividends) before it are scaled:
    - a small demerger (under 10% of cost) by the parent's published cost split (HUL 0.9809 for Kwality Wall's);
    - a large one by that day's own price ratio, which is how the market valued the parts. The published cost split
      is a tax apportionment of book value and can be far from it (ABFRL kept 75.68% of cost but 33% of the price).
    Returns a description, or None when no demerger of this company falls inside the history. Mutates `history`.
    """
    base = selected.rsplit(".", 1)[0]
    bars = history["bars"]
    first, last = bars[0]["at"][:10], bars[-1]["at"][:10]
    events = [d for d in parent_events(base, demergers) if first < str(d.get("ex_date") or d.get("record_date")) <= last]
    if not events:
        return None
    event = max(events, key=lambda d: d.get("ex_date") or d["record_date"])
    day = event.get("ex_date") or event["record_date"]
    i = next(k for k, b in enumerate(bars) if b["at"][:10] >= day)
    move = bars[i]["close"] / bars[i - 1]["close"] - 1
    share = demerged_share(event)
    minor = share is not None and share < MINOR_SHARE
    who = event.get("resulting_company") or event.get("resulting_symbol")
    if minor:
        factor = event["cost_split_pct"]["parent"] / 100 if move < -0.03 else 1.0
    else:
        factor = 1 + move if move < -max(0.10, (share or 0) / 200) else 1.0
    if factor != 1.0:
        for b in bars[:i]:
            for k in ("open", "high", "low", "close"):
                if k in b:
                    b[k] *= factor
        cutoff = int(datetime.fromisoformat(bars[i]["at"]).timestamp())
        for d in ((history.get("corporate_actions") or {}).get("dividends") or {}).values():
            if isinstance(d, dict) and isinstance(d.get("amount"), (int, float)) and int(d.get("date", cutoff)) < cutoff:
                d["amount"] *= factor
        how = (f"the {event['cost_split_pct']['parent']}% the parent kept in the published cost split" if minor else
               "that day's own price change, which is how the market valued the demerged part"
               + (f" (the published cost split, {event['cost_split_pct']['parent']}% / {share}%, is a tax apportionment, not a price ratio)" if share else ""))
        note = (f"Yahoo does not adjust prices before the {day} demerger of {who}; they are scaled by {factor:.4f}, {how}, "
                "so returns, averages, the 52-week range, the outlook and the P/E history compare like with like.")
        mode = "scaled"
    else:
        note = (f"Prices before the {day} demerger of {who} already reflect it in Yahoo's data (the price moved {move * 100:+.1f}% that day), "
                "so the full history is used.")
        mode = "provider"
    cut = {"record_date": event["record_date"], "ex_date": day, "first_session": bars[i]["at"][:10], "sessions": len(bars),
           "sessions_since": len(bars) - i, "dropped_sessions": 0, "resulting_symbol": event.get("resulting_symbol"),
           "resulting_company": event.get("resulting_company"), "day_move": move, "factor": factor, "mode": mode, "minor": minor,
           "demerged_share_pct": share, "event": event, "note": note}
    history["demerger_cut"] = cut
    return cut


align_demerger_history = trim_for_demerger


def unexplained_gaps(bars, split_dates=(), threshold=0.35):
    """Single-session closes that fall by more than `threshold` with no split recorded that day: possible unadjusted actions."""
    out = []
    for a, b in zip(bars, bars[1:]):
        day = b["at"][:10]
        if b["close"] < a["close"] * (1 - threshold) and day not in split_dates:
            out.append(f"Close fell {(b['close'] / a['close'] - 1) * 100:.0f}% on {day} with no split recorded; if that was a demerger, "
                       "bonus or other corporate action, figures that span the date are not comparable.")
    return out


NSE_ACTIONS = "https://www.nseindia.in/api/corporates-corporateActions?index=equities&symbol={}"


def nse_actions(base, fetcher):
    """NSE's corporate actions for a symbol: [(ex-date ISO, subject)]. Empty when unavailable."""
    try:
        rows = json.loads(fetcher(NSE_ACTIONS.format(urllib.parse.quote(base, safe=""))))
    except (Block, ValueError, TypeError, KeyError, OSError):
        return None
    out = []
    for r in rows if isinstance(rows, list) else []:
        try:
            out.append((datetime.strptime(str(r.get("exDate")).strip().title(), "%d-%b-%Y").date().isoformat(), str(r.get("subject") or "").strip()))
        except ValueError:
            continue
    return out


def action_gaps(bars, actions, split_dates=(), skip=(), threshold=0.04, years=2):
    """Single-day falls of more than `threshold` on an NSE ex-date: explained when a dividend covers most of the fall,
    otherwise a warning that the action may be unadjusted in Yahoo's prices."""
    start = (date.fromisoformat(bars[-1]["at"][:10]) - timedelta(days=365 * years)).isoformat()
    by_day = {}
    for day, subject in actions or []:
        by_day.setdefault(day, []).append(subject)
    out = []
    for a, b in zip(bars, bars[1:]):
        day = b["at"][:10]
        move = b["close"] / a["close"] - 1
        if day < start or move >= -threshold or day not in by_day or day in split_dates or day in skip:
            continue
        subject = "; ".join(by_day[day])
        dividend_only = all("ividend" in s or "AGM" in s.upper() or "General Meeting" in s for s in by_day[day])
        paid = sum(float(x) for x in re.findall(r"(?:Rs|Re)\.?\s*([0-9]+(?:\.[0-9]+)?)", subject, re.I)) if "ividend" in subject else 0.0
        if dividend_only and not (paid and paid / a["close"] >= 0.5 * -move):
            continue  # prices are never adjusted for ordinary dividends; a bigger fall that day is a market move
        if paid and paid / a["close"] >= 0.5 * -move:
            out.append(f"Close fell {move * 100:.1f}% on {day}, the ex-date of '{subject}' (NSE); the payout is {paid / a['close'] * 100:.1f}% of the "
                       "price, so most of the fall is the dividend, not a market move.")
        else:
            out.append(f"Close fell {move * 100:.1f}% on {day}, the ex-date of '{subject}' (NSE); if Yahoo has not adjusted earlier prices for it, "
                       "returns and averages across that date are not like-for-like.")
    return out


def retrying(fetcher, *, attempts=3, delays=(0.5, 1.5), sleep=time.sleep):
    """Wrap a fetcher so transient failures (network errors, rate limits) are retried a bounded number of times."""
    def fetch(url):
        for attempt in range(attempts):
            try:
                return fetcher(url)
            except (Block, OSError, http.client.HTTPException) as error:
                # http.client errors (e.g. IncompleteRead on a cut-off reply) escape the public fetcher's own handling.
                transient = not isinstance(error, Block) or "unavailable" in str(error)
                if not transient:
                    raise
                if attempt == attempts - 1:
                    if isinstance(error, Block):
                        raise
                    raise Block("PUBLIC: source unavailable (connection dropped)") from None
                sleep(delays[min(attempt, len(delays) - 1)])
    fetch.retrying = True
    return fetch


# Time limits. Every live request gets a per-operation timeout, and the optional sections (news and peers) run
# alongside the core data within one overall budget, so a slow feed becomes a labelled gap instead of minutes of
# waiting (a slow network once took 3-3.5 minutes for one stock with peers; the same run repeated took 8-9 s).
CORE_TIMEOUT = 15        # seconds per network operation for prices, statements and NSE filings
OPTIONAL_TIMEOUT = 8     # seconds per network operation for news and peers
OPTIONAL_BUDGET = 30     # seconds for news and peers together, counted from when they start
GRACE = 2.0              # extra wait for a request already in flight when the budget runs out
try:
    # public_market.request may retry transient failures itself (it then takes a `budget`); never retry on top of it.
    REQUEST_RETRIES = "budget" in inspect.signature(request).parameters
except (TypeError, ValueError):
    REQUEST_RETRIES = False


def live_fetcher(timeout=CORE_TIMEOUT):
    """The live public fetcher with a per-operation timeout and bounded retries."""
    fetch = lambda url: request(url, timeout=timeout)
    return fetch if REQUEST_RETRIES else retrying(fetch)


def live_nse(timeout=CORE_TIMEOUT, attempts=3):
    """The live NSE fetcher (company endpoints and filings); it retries transient failures itself."""
    from shareholding import nse_request
    return functools.partial(nse_request, timeout=timeout, attempts=attempts)


def memo(fetch, keep=lambda url: True):
    """Fetch each kept URL once per run (the Nifty history and USD/INR rates are shared by every stock compared)."""
    cache, lock = {}, threading.Lock()

    def get(url):
        if not keep(url):
            return fetch(url)
        with lock:
            if url in cache:
                return cache[url]
        body = fetch(url)
        with lock:
            cache[url] = body
        return body
    get.retrying = getattr(fetch, "retrying", False)
    return get


TIMED_OUT = object()


class OptionalSections:
    """News and peers run in the background while the core data loads, under one time limit. Requests are not
    started after the deadline, so a slow source ends as a gap the report names ("peers (timed out)")."""

    def __init__(self, budget):
        self.deadline = time.monotonic() + budget if budget else None
        self.pool, self.futures = None, {}

    def start(self, name, fn, *args, **kwargs):
        if self.pool is None:
            self.pool = ThreadPoolExecutor(max_workers=3)
        self.futures[name] = self.pool.submit(fn, *args, **kwargs)

    def result(self, name):
        """The section's result, TIMED_OUT, or the exception it raised."""
        future = self.futures[name]
        wait = None if self.deadline is None else max(0.0, self.deadline - time.monotonic()) + GRACE
        try:
            return future.result(timeout=wait)
        except FuturesTimeout:
            return TIMED_OUT
        except Exception as error:  # an optional section must never break the report
            return error

    def expired(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    def close(self):
        if self.pool:
            self.pool.shutdown(wait=False)

    def guard(self, fn, label, timed=False):
        """`fn` that refuses to start after the deadline; `timed` live fetchers also get a timeout ending by it."""
        if self.deadline is None:
            return fn

        def get(url):
            left = self.deadline - time.monotonic()
            if left <= 0.05:
                raise Block(f"{label}: time limit for optional sections reached")
            return fn(url, timeout=max(1.0, min(OPTIONAL_TIMEOUT, left))) if timed else fn(url)
        return get

    def fetchers(self, *, live, fetcher, news_fetcher, nse_fetcher, live_news):
        """(public, news, NSE) fetchers for the optional sections."""
        if live:
            def public(url, timeout):
                kw = {"timeout": timeout}
                if REQUEST_RETRIES and self.deadline is not None:
                    kw["budget"] = max(0.0, self.deadline - time.monotonic() - timeout)
                return request(url, **kw)
            pub = self.guard(public, "PUBLIC", timed=True) if self.deadline is not None else fetcher
            if self.deadline is not None and not REQUEST_RETRIES:
                pub = retrying(pub, attempts=2)

            def nse(url, timeout):
                from shareholding import nse_request
                return nse_request(url, timeout=timeout, attempts=2)
            nse_f = self.guard(nse, "NSE", timed=True) if self.deadline is not None else nse_fetcher
        else:
            pub, nse_f = self.guard(fetcher, "PUBLIC"), self.guard(nse_fetcher, "NSE")
        if live_news and self.deadline is not None:
            news_f = self.guard(lambda url, timeout: news_request(url, timeout=timeout), "NEWS", timed=True)
        else:
            news_f = self.guard(news_fetcher, "NEWS")
        return pub, news_f, nse_f


def daily_history(selected, *, fetcher, now, years=2):
    query = urllib.parse.urlencode({"range": f"{years}y", "interval": "1d", "includePrePost": "false", "events": "div,splits"})
    raw = fetcher("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(selected, safe="") + "?" + query)
    result = chart(raw, selected, "1d", now)
    meta = json.loads(raw)["chart"]["result"][0]["meta"]
    result["name"] = meta.get("longName") or meta.get("shortName") or selected
    if isinstance(meta.get("firstTradeDate"), int):
        result["first_trade"] = datetime.fromtimestamp(meta["firstTradeDate"], IST).date().isoformat()
    complete_latest_session(result, meta, now)
    return result


def complete_latest_session(result, meta, now):
    """Yahoo sometimes ships the newest daily bar with no close (seen just after midnight IST).

    The chart parser rightly drops that bar, which would silently make yesterday look like the
    latest session. Rebuild it from Yahoo's own last-trade quote when that quote belongs to a newer
    session, and say so; if no usable quote exists, record the gap instead.
    """
    bars = result["bars"]
    last_day = bars[-1]["at"][:10]
    result["price_note"] = None
    stamp, price = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    quote_at = datetime.fromtimestamp(int(stamp), IST) if isinstance(stamp, (int, float)) else None
    newer_missing = [d for d in result.get("missing_bars", []) if d[:10] > last_day]
    if quote_at and quote_at <= now and quote_at.date().isoformat() > last_day and isinstance(price, (int, float)) and price > 0:
        high = max(price, meta.get("regularMarketDayHigh") or price)
        low = min(price, meta.get("regularMarketDayLow") or price)
        open_ = min(max(bars[-1]["close"], low), high)  # Yahoo's quote has no open; previous close, kept inside the day's range
        day = quote_at.date().isoformat()
        bars.append({"at": f"{day}T09:15:00+05:30", "open": float(open_), "high": float(high), "low": float(low), "close": float(price),
                     "volume": float(meta.get("regularMarketVolume") or 0), "from_quote": True})
        result["missing_bars"] = [d for d in result.get("missing_bars", []) if d[:10] != day]
        result["price_note"] = (f"Yahoo's daily bar for {day} had no close; using its last traded price ₹{price:,.2f} "
                                f"at {quote_at.strftime('%H:%M')} IST and that day's high/low.")
    elif newer_missing:
        result["price_note"] = (f"The latest session ({newer_missing[-1][:10]}) has no price in Yahoo's data yet; "
                                f"figures use the {last_day} close. Check a live quote before relying on levels.")
    result["price_date"] = bars[-1]["at"][:10]


def session_open(now):
    """True during the regular NSE session on a weekday (holidays are not checked)."""
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) < (15, 30)


def news_request(url, *, timeout=20):
    """Google News RSS search only; same size and redirect limits as the other public fetchers."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "news.google.com" or not parsed.path.startswith("/rss/search"):
        raise Block("NEWS: unsupported source")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (IndiaDayTrader public research)", "Accept": "application/rss+xml,application/xml"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(LIMIT + 1)
    except (OSError, ValueError, http.client.HTTPException):
        raise Block("NEWS: source unavailable") from None
    if len(raw) > LIMIT:
        raise Block("NEWS: oversized response")
    return raw


def fundamentals(selected, *, fetcher, now):
    query = urllib.parse.urlencode({"type": ",".join(FUNDAMENTALS), "period1": int((now - timedelta(days=6 * 365)).timestamp()),
                                    "period2": int(now.timestamp()) + 86400})
    raw = fetcher("https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/"
                  + urllib.parse.quote(selected, safe="") + "?" + query)
    data = json.loads(raw)
    series, currencies = {}, set()
    for result in data["timeseries"]["result"]:
        name = result["meta"]["type"][0]
        rows = [r for r in result.get(name) or [] if r and isinstance(r.get("reportedValue", {}).get("raw"), (int, float))]
        if "Shares" not in name:  # share counts carry no currency
            currencies |= {r.get("currencyCode") or "INR" for r in rows}
        rows = [{"date": r["asOfDate"], "value": float(r["reportedValue"]["raw"])} for r in rows if math.isfinite(r["reportedValue"]["raw"])]
        if rows:
            series[name] = sorted(rows, key=lambda r: r["date"])
    foreign = currencies - {"INR"}
    if series and foreign:
        # Some companies (e.g. Infosys) report in USD on Yahoo while prices are in rupees. Each period converts at that
        # period's own USD/INR, so growth is rupee growth (converting every year at today's rate would show USD growth,
        # seen: Infosys FY2026 revenue +4.6% in USD against about +9.6% in rupees).
        if len(foreign) > 1 or len(currencies) > 1:
            raise Block("STOCK: financial statements mix currencies; ratios omitted")
        currency = foreign.pop()
        try:
            history = fx_history(currency, fetcher=fetcher)
            series = convert_at_period_rates(series, history)
            series["_currency"] = {"reported": currency, "basis": "period",
                                   "note": f"Financials reported in {currency}; converted to rupees at each period's average {currency}/INR "
                                           "(balance-sheet items at the period-end rate), so growth rates are rupee growth.",
                                   "label": f"converted from {currency} at period rates"}
        except Block:
            rate = fx_rate(currency, fetcher=fetcher)
            for name, rows in series.items():
                if "Shares" not in name:
                    series[name] = [{**r, "value": r["value"] * rate} for r in rows]
            series["_currency"] = {"reported": currency, "basis": "today", "rate": rate,
                                   "note": f"Financials reported in {currency}; converted at today's rate ₹{rate:,.2f} per {currency} because "
                                           f"past rates were unavailable, so growth rates are in {currency} terms and past figures are approximate.",
                                   "label": f"{currency} terms, converted at today's rate"}
    return series


def fx_history(currency, *, fetcher):
    """Daily closes of CURRENCY/INR from Yahoo (about ten years), as sorted (date, rate) pairs."""
    if not re.fullmatch(r"[A-Z]{3}", currency or ""):
        raise Block("STOCK: unknown reporting currency")
    try:
        raw = fetcher(f"https://query1.finance.yahoo.com/v8/finance/chart/{currency}INR=X?range=10y&interval=1d")
        result = json.loads(raw)["chart"]["result"][0]
        if result["meta"].get("currency") != "INR":
            raise ValueError("currency")
        closes = result["indicators"]["quote"][0]["close"]
        rows = sorted((datetime.fromtimestamp(int(s), IST).date(), float(c)) for s, c in zip(result["timestamp"], closes)
                      if isinstance(c, (int, float)) and 0 < c < 100000)
    except (Block, ValueError, KeyError, IndexError, TypeError):
        raise Block(f"STOCK: {currency}/INR history unavailable") from None
    if len(rows) < 200:
        raise Block(f"STOCK: {currency}/INR history unavailable")
    return rows


def period_rate(history, end, days):
    """Average rate over the `days` ending on `end` (None when the history does not reach that far)."""
    start = end - timedelta(days=days - 1)
    if history[0][0] > start + timedelta(days=10):
        return None
    values = [v for d, v in history if start <= d <= end]
    return sum(values) / len(values) if values else None


def spot_rate(history, day):
    values = [v for d, v in history if day - timedelta(days=10) <= d <= day]
    return values[-1] if values else None


def convert_at_period_rates(series, history):
    out = {}
    for name, rows in series.items():
        if "Shares" in name:
            out[name] = rows
            continue
        kind = next((k for k in PERIOD_DAYS if name.startswith(k)), "annual")
        converted = []
        for r in rows:
            day = date.fromisoformat(r["date"])
            rate = spot_rate(history, day) if any(b in name for b in BALANCE_ITEMS) else period_rate(history, day, PERIOD_DAYS[kind])
            if rate:
                converted.append({**r, "value": r["value"] * rate, "fx_rate": rate})
        if converted:
            out[name] = converted
    # USD EPS comes rounded to cents (Infosys: 0.76, 0.80), too coarse for growth; rebuild rupee EPS from rupee profit
    # and diluted shares where both exist for the same period.
    for kind in ("annual", "quarterly"):
        shares = {r["date"]: r["value"] for r in out.get(f"{kind}DilutedAverageShares", []) if r["value"] > 0}
        profit = {r["date"]: r["value"] for r in out.get(f"{kind}NetIncome", [])}
        rebuilt = [{"date": d, "value": profit[d] / shares[d], "from_profit": True} for d in sorted(profit) if d in shares]
        if rebuilt:
            keep = {r["date"]: r for r in out.get(f"{kind}DilutedEPS", [])}
            keep.update({r["date"]: r for r in rebuilt})
            out[f"{kind}DilutedEPS"] = [keep[d] for d in sorted(keep)]
    ttm, quarters = (out.get("trailingNetIncome") or [None])[-1], out.get("quarterlyDilutedAverageShares") or out.get("annualDilutedAverageShares")
    if ttm and quarters and out.get("trailingDilutedEPS"):
        shares = quarters[-1]["value"]
        out["trailingDilutedEPS"] = [r for r in out["trailingDilutedEPS"] if r["date"] != ttm["date"]] + [{"date": ttm["date"], "value": ttm["value"] / shares, "from_profit": True}]
        out["trailingDilutedEPS"].sort(key=lambda r: r["date"])
    return out


def fx_rate(currency, *, fetcher):
    if not re.fullmatch(r"[A-Z]{3}", currency or ""):
        raise Block("STOCK: unknown reporting currency")
    try:
        raw = fetcher(f"https://query1.finance.yahoo.com/v8/finance/chart/{currency}INR=X?range=5d&interval=1d")
        meta = json.loads(raw)["chart"]["result"][0]["meta"]
    except (Block, ValueError, KeyError, IndexError, TypeError):
        raise Block(f"STOCK: {currency}/INR rate unavailable; ratios omitted") from None
    rate = meta.get("regularMarketPrice")
    if meta.get("currency") != "INR" or not isinstance(rate, (int, float)) or not 0 < rate < 100000:
        raise Block(f"STOCK: {currency}/INR rate unavailable; ratios omitted")
    return float(rate)


# ---------- technical helpers ----------

def sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def rsi(closes, n=14):
    """Wilder's RSI (the one Kite, TradingView and most brokers show): seeded with a simple average of the first `n`
    changes, then smoothed as avg = (avg * (n - 1) + change) / n over every later close. Pass a long history (a year
    or more) so the seed no longer matters. A plain 14-day sum (Cutler's RSI) overstated Reliance at 98.1 against
    Wilder's 74.3 on 18 Nov 2026."""
    if len(closes) <= n:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(c, 0) for c in changes[:n]) / n
    loss = sum(max(-c, 0) for c in changes[:n]) / n
    for c in changes[n:]:
        gain = (gain * (n - 1) + max(c, 0)) / n
        loss = (loss * (n - 1) + max(-c, 0)) / n
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)


def forming_bar(bars, now):
    """True when the last daily bar is today's and the NSE session is still open (its close and volume are partial)."""
    return bool(now and bars and session_open(now) and bars[-1]["at"][:10] == now.date().isoformat())


def atr(bars, n=14):
    if len(bars) <= n:
        return None
    ranges = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"])) for a, b in zip(bars[-n - 1:-1], bars[-n:])]
    return sum(ranges) / n


def ret(closes, sessions):
    return closes[-1] / closes[-1 - sessions] - 1 if len(closes) > sessions else None


YEAR_SESSIONS = 250


def one_year_return(closes):
    """The single 1-year return definition shared with the screener: last close vs the close 250 sessions earlier."""
    return ret(closes, YEAR_SESSIONS)


SPECIAL_MULTIPLE = 3  # a payment this many times the median of the other recent ones is flagged as a possible special


def trailing_dividends(history, price, *, since=None):
    """Trailing-12-month dividends per share from Yahoo's chart dividend events, divided by today's price.

    Flags a payment as a possible special when, among the last two years' payments (at least four), it is more than
    three times the median of the others, unless a payment of similar size (at least two-thirds) came about a year
    earlier or later and the company pays fewer than about four times a year. Checked live on the Nifty 100 in Nov 2026:
    twice the median flagged ordinary finals (TCS ₹30 against ₹11 interims); without the seasonal test, Asian Paints'
    and GAIL's regular large final/interim were flagged; TCS's January interim-plus-special and ABB's ₹90 are flagged.
    Only the part of a flagged payment above a regular one counts as special (TCS ₹57 = ₹11 interim + ₹46 special).
    Nothing is flagged when the flagged payments would be over 80% of two years' dividends (MRF's annual final). Yahoo's amounts are already adjusted for later splits and bonuses (checked on
    KOTAKBANK's 2026 split and TRENT's 2026 bonus), so they are on today's share count.
    Returns None when the chart cannot establish it (no payments and less than a year of history).
    """
    bars = history["bars"]
    last, first = date.fromisoformat(bars[-1]["at"][:10]), date.fromisoformat(bars[0]["at"][:10])
    actions = history.get("corporate_actions") or {}
    rows = []
    for key, d in (actions.get("dividends") or {}).items():
        amount, stamp = (d or {}).get("amount"), (d or {}).get("date", key)
        try:
            day = datetime.fromtimestamp(int(stamp), IST).date()
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if isinstance(amount, (int, float)) and math.isfinite(amount) and amount > 0 and day <= last and (not since or day.isoformat() >= since):
            rows.append((day, float(amount)))
    rows.sort()
    ttm = [r for r in rows if r[0] > last - timedelta(days=365)]
    if not ttm and (last - first).days < 360:
        return None
    recent = [r for r in rows if r[0] > last - timedelta(days=730)]
    specials = []
    quarterly = len(recent) >= 7
    for i, (day, amount) in enumerate(recent):
        others = [a for j, (_, a) in enumerate(recent) if j != i]
        if len(others) < 3 or amount <= SPECIAL_MULTIPLE * statistics.median(others):
            continue
        # A large payment that recurs in the same season (a big final or interim each year) is regular, except for
        # quarterly payers, whose regular payments are similar in size (TCS's January interim-plus-special recurs too).
        seasonal = any(300 <= abs((d - day).days) <= 430 and a * 1.5 >= amount for d, a in recent)
        if quarterly or not seasonal:
            specials.append((day, amount))
    if specials and sum(a for _, a in specials) > 0.8 * sum(a for _, a in recent):
        specials = []  # the "regular" stream would be implausibly small (MRF: ₹3 interims, ₹229 final every July)
    dps = sum(a for _, a in ttm)
    flagged = set(specials)
    # A flagged payment is often a regular interim plus a special declared together (TCS January 2026: ₹11 interim
    # + ₹46 special = ₹57). Only the part above a regular payment is treated as special: the regular amount is the
    # unflagged payment in the same season a year earlier (ABB's August interim) or else the median of the others.
    special_ttm = []
    for day, amount in (s for s in specials if s in ttm):
        others = [a for d, a in recent if (d, a) != (day, amount)]
        season = [a for d, a in recent if (d, a) not in flagged and 300 <= (day - d).days <= 430]
        regular = min(season[-1] if season else statistics.median(others), amount)
        special_ttm.append((day, amount, amount - regular, regular))
    special_sum = sum(s for _, _, s, _ in special_ttm)
    flags = []
    if special_ttm:
        flags.append("Possible special dividend in the last 12 months: " + ", ".join(
            f"₹{a:,.2f} on {d.isoformat()}" + (f" (about ₹{s:,.2f} above a regular ₹{r:,.2f} payment)" if r else "") for d, a, s, r in special_ttm)
            + f"; without the special part the trailing yield would be about {(dps - special_sum) / price * 100:.1f}%.")
    latest_paid = ttm[-1] if ttm else (rows[-1] if rows else None)
    return {"dividend_yield": dps / price if price else None, "dps_ttm": dps,
            "recurring_yield": (dps - special_sum) / price if price else None,
            "payments": [{"date": d.isoformat(), "amount": a} for d, a in ttm],
            "specials": [{"date": d.isoformat(), "amount": a, "special_part": s, "regular_part": r} for d, a, s, r in special_ttm], "flags": flags,
            "latest": {"date": latest_paid[0].isoformat(), "amount": latest_paid[1], "days_ago": (last - latest_paid[0]).days} if latest_paid else None,
            "basis": "dividends per share with an ex-date in the last 12 months ÷ today's price (Yahoo chart dividend events)"
                     + (f"; only dividends from {since} on" if since else "")}


def pct(value, digits=1, sign=True):
    if value is not None and abs(value * 100) < 0.5 * 10 ** -digits:
        value = 0.0  # never "-0.0%"
    return "n/a" if value is None else f"{value * 100:{'+' if sign else ''}.{digits}f}%"


def num(value, digits=2):
    return "n/a" if value is None else f"{value:,.{digits}f}"


def indian(number):
    """Whole number with Indian digit grouping: 1837980 -> '18,37,980'."""
    digits = str(abs(int(round(number))))
    if len(digits) > 3:
        head, groups = digits[:-3], [digits[-3:]]
        while head:
            groups.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join(groups)
    return ("-" if number < 0 and round(number) else "") + digits


LAKH_CRORE = 1e12  # rupees in one lakh crore


def crore(value):
    """Rupee amount in crore with Indian grouping; one lakh crore and above in lakh crore (₹18.38 lakh cr)."""
    if value is None:
        return "n/a"
    if abs(value) >= LAKH_CRORE:
        return f"₹{value / LAKH_CRORE:,.2f} lakh cr"
    return f"₹{indian(value / 1e7)} cr"


NEW_LISTING_DAYS = 150  # an NSE history that starts this recently is a new listing, not a broken feed
NEW_LISTING_MIN = 5     # sessions needed to say anything about a new listing


def technicals(history, benchmark, now=None, new_listing=False):
    """Trend, momentum and levels. Needs 60 sessions, or NEW_LISTING_MIN for a stock listed in the last
    NEW_LISTING_DAYS (`new_listing`), whose averages, RSI and 1-year figures then stay n/a."""
    bars = [b for b in history["bars"]]
    closes = [b["close"] for b in bars]
    if len(closes) < (NEW_LISTING_MIN if new_listing else 60):
        raise Block("STOCK: fewer than 60 daily observations; listing too recent or feed incomplete")
    last = closes[-1]
    window = bars[-250:]
    hi52, lo52 = max(b["high"] for b in window), min(b["low"] for b in window)
    daily = [math.log(b / a) for a, b in zip(closes[-251:-1], closes[-250:])]
    vol = statistics.pstdev(daily) * math.sqrt(250) if len(daily) > 20 else None
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    # During market hours the last bar is still forming: its volume is a fraction of a day (seen: 0.08× at 09:27) and its
    # close moves, so RSI and the volume ratio use completed sessions only.
    forming = forming_bar(bars, now)
    done = bars[:-1] if forming else bars
    volumes = [b["volume"] for b in done if b["volume"]]
    avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None
    bench = [b["close"] for b in benchmark["bars"]] if benchmark else []
    relative = {}
    for label, n in (("1M", 21), ("3M", 63), ("1Y", 250)):
        s, m = ret(closes, n), ret(bench, n) if bench else None
        relative[label] = {"stock": s, "nifty": m, "difference": None if s is None or m is None else s - m}
    signals = []
    if s50 and s200:
        signals.append(("bull" if s50 > s200 else "bear", f"50-day average {'above' if s50 > s200 else 'below'} 200-day average"))
    if s200:
        signals.append(("bull" if last > s200 else "bear", f"price {'above' if last > s200 else 'below'} 200-day average"))
    if s50:
        signals.append(("bull" if last > s50 else "bear", f"price {'above' if last > s50 else 'below'} 50-day average"))
    if s20:
        signals.append(("bull" if last > s20 else "bear", f"price {'above' if last > s20 else 'below'} 20-day average"))
    r3 = relative["3M"]["difference"]
    if r3 is not None:
        signals.append(("bull" if r3 > 0 else "bear", f"{'outperformed' if r3 > 0 else 'underperformed'} Nifty 50 over 3 months by {abs(r3) * 100:.1f} pts"))
    r = rsi([b["close"] for b in done])
    score = sum(1 if k == "bull" else -1 for k, _ in signals)
    trend = "uptrend" if score >= 3 else "downtrend" if score <= -3 else "mixed / sideways"
    return {
        "last_close": last, "last_close_date": bars[-1]["at"][:10], "previous_close": closes[-2],
        "day_change": last / closes[-2] - 1,
        "returns": {**{k: ret(closes, n) for k, n in (("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126))}, "1Y": one_year_return(closes)},
        "high_52w": hi52, "low_52w": lo52, "from_high": last / hi52 - 1, "from_low": last / lo52 - 1,
        "sma20": s20, "sma50": s50, "sma200": s200, "rsi14": r,
        "rsi_reading": None if r is None else "overbought (>70)" if r > 70 else "oversold (<30)" if r < 30 else "neutral",
        "atr14": atr(bars), "annual_volatility": vol,
        "volume_latest": done[-1]["volume"] or None, "volume_avg20": avg_vol, "forming_bar": forming,
        "indicator_date": done[-1]["at"][:10],
        "support": min(b["low"] for b in bars[-20:]), "resistance": max(b["high"] for b in bars[-20:]),
        "support_60d": min(b["low"] for b in bars[-60:]), "resistance_60d": max(b["high"] for b in bars[-60:]),
        "relative": relative, "signals": signals, "trend": trend, "trend_score": score, "closes": closes,
        "close_dates": [b["at"][:10] for b in bars], "history_start": bars[0]["at"][:10], "history_sessions": len(bars),
        "new_listing": bool(new_listing and len(bars) < 60), "since_first": last / closes[0] - 1 if closes[0] else None,
    }


def latest(series, name):
    rows = series.get(name)
    return rows[-1] if rows else None


def value(series, name):
    row = latest(series, name)
    return row["value"] if row else None


def last_four_quarters(rows):
    """The latest four quarterly values if they are consecutive quarters (about 91 days apart), else None."""
    if len(rows) < 4:
        return None
    four = rows[-4:]
    days = [datetime.fromisoformat(r["date"]).date() for r in four]
    if any(not 75 <= (b - a).days <= 105 for a, b in zip(days, days[1:])):
        return None
    return four


def check_eps(eps, quarterly, price):
    """Cross-check the EPS used for P/E against the last four reported quarters.

    Yahoo's trailing EPS is occasionally wrong (seen: Adani Enterprises -3,381.91 when its quarters sum to about +46),
    and a single unusual quarter can make an accurate trailing figure misleading (one large loss inflating P/E).
    Returns the EPS to use and plain-language flags.
    """
    flags = []
    four = last_four_quarters(quarterly)
    if not eps or not four:
        return eps, flags
    total = sum(r["value"] for r in four)
    if eps["basis"] == "trailing 12 months" and eps["date"] == four[-1]["date"] and abs(eps["value"] - total) > 0.25 * max(abs(total), 0.01):
        flags.append(f"Yahoo's trailing EPS ({eps['value']:,.2f}) does not match the sum of the last four quarters ({total:,.2f}); the quarterly sum is used.")
        eps = {**eps, "value": total, "basis": "sum of last four quarters", "replaced": eps["value"]}
    values = sorted(r["value"] for r in four)
    typical = 4 * (values[1] + values[2]) / 2
    if eps["date"] == four[-1]["date"] and typical > 0 and abs(eps["value"] - typical) > 0.5 * typical:
        odd = max(four, key=lambda r: abs(r["value"] - typical / 4))
        flags.append(f"One unusual quarter ({odd['date']}, EPS {odd['value']:,.2f}) distorts trailing earnings; "
                     f"on four typical quarters (EPS about {typical:,.2f}) the P/E would be about {price / typical:,.1f}.")
    elif eps["date"] == four[-1]["date"]:
        # Smaller distortion: one clearly depressed quarter while the other three agree with each other.
        # Seasonal businesses (hydro power, air conditioners) have a weak quarter every year, which a trailing figure
        # rightly includes, so the same quarter a year earlier must be in the data and must not have been weak.
        weak = min(four, key=lambda r: r["value"])
        rest = [r["value"] for r in four if r is not weak]
        weak_day = datetime.fromisoformat(weak["date"]).date()
        year_ago = [r["value"] for r in quarterly if 345 <= (weak_day - datetime.fromisoformat(r["date"]).date()).days <= 385]
        if (min(rest) > 0 and max(rest) <= 1.6 * min(rest) and weak["value"] < 0.4 * statistics.median(rest)
                and year_ago and year_ago[-1] >= 0.6 * statistics.median(rest)):
            annual = sum(rest) * 4 / 3
            flags.append(f"Trailing EPS includes a weak quarter ({weak['date']}, EPS {weak['value']:,.2f} against about "
                         f"{statistics.median(rest):,.2f} in each of the other three); on those three annualised (EPS about {annual:,.2f}) "
                         f"the P/E would be about {price / annual:,.1f}.")
    return eps, flags


def latest_of(series, *names):
    """The newest row across several series (e.g. annual and quarterly balance sheets)."""
    rows = [r for r in (latest(series, n) for n in names) if r]
    return max(rows, key=lambda r: r["date"]) if rows else None


def change(current, previous):
    """Growth as a fraction when both values are positive; otherwise None (a percentage across a sign change is meaningless)."""
    if current is None or previous is None or previous <= 0 or current < 0:
        return None
    return current / previous - 1


def swing(current, previous, show):
    """Plain words for a change that a percentage cannot describe: a swing between profit and loss, or a changing loss."""
    if current is None or previous is None or (previous > 0 and current >= 0):
        return None
    if previous > 0 > current:
        return f"swung from {show(previous)} profit to {show(-current)} loss"
    if previous < 0 < current:
        return f"swung from {show(-previous)} loss to {show(current)} profit"
    if previous < 0 and current < 0:
        return f"loss {'narrowed' if current > previous else 'widened'} from {show(-previous)} to {show(-current)}"
    if previous == 0:
        return f"from nil to {show(current)} profit" if current >= 0 else f"from nil to {show(-current)} loss"
    return None


def year_ago(rows, row):
    """The row about a year before `row` (same quarter last year), or None."""
    day = date.fromisoformat(row["date"])
    found = [r for r in rows if 345 <= (day - date.fromisoformat(r["date"])).days <= 385]
    return found[-1] if found else None


def trailing_sum(series, name, quarterly):
    """Trailing-12-month value: Yahoo's trailing figure when it is as new as the latest quarter, else the sum of four
    consecutive quarters. Returns (value, date) or (None, None)."""
    q = series.get(quarterly) or []
    t = latest(series, name)
    if t and (not q or t["date"] >= q[-1]["date"]):
        return t["value"], t["date"]
    four = last_four_quarters(q)
    if four:
        return sum(r["value"] for r in four), four[-1]["date"]
    return None, None


def eps_words(value):
    return f"₹{value:,.2f}" if value >= 0 else f"−₹{-value:,.2f}"


def crore_words(value):
    return f"₹{indian(value / 1e7)} cr"


def signed_crore(value):
    """'₹29 cr' or '−₹29 cr'."""
    return crore_words(value) if value >= 0 else "−" + crore_words(-value)


def balance_sheet(series):
    """(date, debt, equity) from the newest balance sheet that reports both: Indian companies publish one every half-year
    (31 March and 30 September), and Yahoo keeps the September one only in its quarterly series. The March figure
    alone understated TMPV's debt/equity in November 2026 (0.73 at 31 Mar 2026 against 0.84 at 30 Sep 2026)."""
    debt = {r["date"]: r["value"] for n in ("annualTotalDebt", "quarterlyTotalDebt") for r in series.get(n, [])}
    equity = {r["date"]: r["value"] for n in ("annualStockholdersEquity", "quarterlyStockholdersEquity") for r in series.get(n, [])}
    common = sorted(set(debt) & set(equity))
    return (common[-1], debt[common[-1]], equity[common[-1]]) if common else (None, None, None)


def day_month_year(day):
    """'30 Sep 2026' from an ISO date."""
    return date.fromisoformat(day[:10]).strftime("%d %b %Y") if day else "n/a"


def one_year_apart(newer, older, low=300, high=430):
    """True when two statement rows ({'date': ISO}) are consecutive financial years (300-430 days apart)."""
    try:
        return low <= (date.fromisoformat(newer["date"]) - date.fromisoformat(older["date"])).days <= high
    except (KeyError, TypeError, ValueError):
        return False


def ratios(series, price, now, splits=None):
    out = {}
    share_row = latest_of(series, "annualOrdinarySharesNumber", "quarterlyOrdinarySharesNumber")
    shares = share_row["value"] if share_row else None
    out["shares_date"] = share_row["date"] if share_row else None
    out["annual_eps_restated"] = []
    if series.get("annualDilutedEPS"):
        restated, out["annual_eps_restated"] = restate_annual_eps(series["annualDilutedEPS"], series.get("annualNetIncome"),
                                                                  series.get("annualOrdinarySharesNumber"), shares, splits)
        if out["annual_eps_restated"]:
            series["annualDilutedEPS"] = restated
    # Use the most recent EPS. A stale trailing figure may predate a split/bonus that the annual series already reflects.
    candidates = [r for r in (latest(series, "trailingDilutedEPS"), latest(series, "annualDilutedEPS")) if r]
    eps = max(candidates, key=lambda r: r["date"]) if candidates else None
    if eps:
        eps = {**eps, "basis": "trailing 12 months" if eps == latest(series, "trailingDilutedEPS") else "last financial year"}
    # Quarters reported before a split or bonus are put on today's share basis before any check uses them.
    quarters, out["quarters_split_adjusted"] = adjust_quarter_eps(series.get("quarterlyDilutedEPS", []), series.get("quarterlyNetIncome", []),
                                                                  shares, splits)
    eps, out["eps_flags"] = check_eps(eps, quarters, price)
    ttm_net, ttm_date = trailing_sum(series, "trailingNetIncome", "quarterlyNetIncome")
    eps, more = cross_check_net_income(eps, ttm_net, ttm_date, shares, quarters, splits, price)
    out["eps_flags"] += more
    out["split_after_statements"] = [d for d, _ in _split_pairs(splits) if eps and d > eps["date"]]
    equity = value(series, "annualStockholdersEquity")
    eq_rows = series.get("annualStockholdersEquity", [])
    # Book value from the newest balance sheet (half-yearly in India). Yahoo's equity is shareholders' equity excluding
    # minority interest, as Screener.in uses; the March figure alone overstated P/B by about 10% for TCS (8.07 vs 7.3)
    # and 20% for TMCV (12.8 vs 10.7) in November 2026.
    book = latest_of(series, "annualStockholdersEquity", "quarterlyStockholdersEquity")
    net = value(series, "annualNetIncome")
    revenue = value(series, "annualTotalRevenue")
    debt_date, debt, debt_equity = balance_sheet(series)
    market_cap = price * shares if shares else None
    out["market_cap"] = market_cap
    out["eps"] = eps
    out["pe"] = price / eps["value"] if eps and eps["value"] > 0 else None
    out["pb"] = price / (book["value"] / shares) if book and shares and book["value"] > 0 else None
    out["book_date"] = book["date"] if book else None
    out["book_basis"] = (f"shareholders' equity excluding minority interest, balance sheet of {book['date']}" if book else None)
    # Average equity only across a year: Yahoo can skip years (Nestlé India: Dec 2022, then Mar 2025 after its 15-month
    # FY2024), and averaging across the gap put ROE at 99% against Screener.in's 74%.
    avg_equity = (eq_rows[-1]["value"] + eq_rows[-2]["value"]) / 2 if len(eq_rows) >= 2 and one_year_apart(eq_rows[-1], eq_rows[-2]) else equity
    out["roe"] = net / avg_equity if net is not None and avg_equity and avg_equity > 0 else None
    out["roe_basis"] = "last financial year's profit ÷ average equity"
    out["net_margin"] = net / revenue if net is not None and revenue else None
    op = value(series, "annualOperatingIncome")
    ebitda = value(series, "annualEBITDA")
    if op is not None and ebitda is not None and op > ebitda * 1.02:
        # Operating profit above EBITDA is impossible: Yahoo built it as gross profit minus a partial cost line
        # (HCLTech FY2026: ₹40,819 cr, a 31% margin, against EBITDA of ₹27,326 cr). Use EBIT instead.
        op = value(series, "annualEBIT")
    out["operating_margin"] = op / revenue if op is not None and revenue else None
    # EBITDA ÷ revenue is the closest free match to Screener.in's OPM (operating profit before depreciation); it also
    # includes other income, so it reads up to a point or two higher (Nov 2026: TCS 27.1% vs 27%, HCLTech 21.0% vs 21%,
    # Infosys 25.3% vs 24%).
    out["ebitda_margin"] = ebitda / revenue if ebitda is not None and revenue else None
    out["margin_basis"] = "last financial year"
    out["debt_to_equity"] = debt / debt_equity if debt is not None and debt_equity and debt_equity > 0 else None
    out["total_debt"] = debt if out["debt_to_equity"] is not None else None  # rupees, same balance sheet as debt/equity
    out["debt_date"] = debt_date if out["debt_to_equity"] is not None else None
    out["debt_basis"] = (f"total debt ÷ shareholders' equity at {debt_date}, Yahoo" if out["debt_date"] else None)
    # Yahoo's total debt includes lease liabilities (all of TCS's ₹11,808 cr at 30 Sep 2026 is leases): borrowings
    # without them when Yahoo gives the lease figure for the same balance sheet, else None ("incl. leases").
    leases = {r["date"]: r["value"] for n in ("annualCapitalLeaseObligations", "quarterlyCapitalLeaseObligations") for r in series.get(n, [])}
    out["lease_liabilities"] = leases.get(debt_date) if out["debt_date"] else None
    out["borrowings_ex_leases"] = (max(debt - out["lease_liabilities"], 0.0) if out["lease_liabilities"] is not None and debt is not None
                                   and out["debt_date"] else None)
    ca, cl = value(series, "annualCurrentAssets"), value(series, "annualCurrentLiabilities")
    out["current_ratio"] = ca / cl if ca and cl else None
    interest = value(series, "annualInterestExpense")
    out["interest_cover"] = op / interest if op is not None and interest and interest > 0 else None
    fcf = value(series, "annualFreeCashFlow")
    out["fcf"] = fcf
    out["fcf_yield"] = fcf / market_cap if fcf is not None and market_cap else None
    dividends = value(series, "annualCashDividendsPaid")
    out["cash_dividends_paid"] = abs(dividends) if dividends is not None else None
    # Last financial year's cash dividends paid ÷ today's market cap. Includes special dividends and can lag a year,
    # so the headline dividend_yield (trailing dividends per share ÷ price) is filled from the chart by the caller.
    out["cash_dividend_yield"] = abs(dividends) / market_cap if dividends is not None and market_cap else None
    out["payout_ratio"] = abs(dividends) / net if dividends is not None and net and net > 0 else None
    out["fcf_cover"] = fcf / abs(dividends) if fcf is not None and dividends else None
    out["dividend_yield"] = None
    # Annual growth only between consecutive financial years: across a gap in Yahoo's years (Nestlé India, Dec 2022 to
    # Mar 2025) it is two-plus years of growth labelled as one (+19.6% against about +9%).
    rev_rows = series.get("annualTotalRevenue", [])
    rev_pair = len(rev_rows) >= 2 and one_year_apart(rev_rows[-1], rev_rows[-2])
    out["revenue_growth"] = change(rev_rows[-1]["value"], rev_rows[-2]["value"]) if rev_pair else None
    eps_rows = series.get("annualDilutedEPS", [])
    eps_pair = len(eps_rows) >= 2 and one_year_apart(eps_rows[-1], eps_rows[-2])
    out["eps_growth"] = change(eps_rows[-1]["value"], eps_rows[-2]["value"]) if eps_pair else None
    out["eps_change_text"] = (swing(eps_rows[-1]["value"], eps_rows[-2]["value"], lambda v: f"₹{v:,.2f} a share")
                              if eps_pair else None)
    out["growth_period"] = (f"FY{eps_rows[-1]['date'][:4]} vs FY{eps_rows[-2]['date'][:4]}" if eps_pair else
                            f"FY{rev_rows[-1]['date'][:4]} vs FY{rev_rows[-2]['date'][:4]}" if rev_pair else None)
    gap_rows = rev_rows if len(rev_rows) >= 2 and not rev_pair else eps_rows if len(eps_rows) >= 2 and not eps_pair else None
    out["year_gap"] = ({"latest": gap_rows[-1]["date"], "before": gap_rows[-2]["date"]} if gap_rows else None)
    distorted = False
    if out["eps_growth"] is not None and (out["eps_growth"] > 2 or out["eps_growth"] < -0.6):
        out["eps_flags"].append(f"Annual EPS changed {out['eps_growth'] * 100:+.0f}% in a year; P/E, ROE and margins may include a one-off gain or loss.")
        distorted = True
    elif out["eps_change_text"]:
        out["eps_flags"].append(f"Annual EPS {out['eps_change_text']} ({out['growth_period']}); a growth percentage across a loss is not meaningful.")
        distorted = True
    # Profit that does not match EPS × shares includes discontinued operations or a one-off (seen: TMPV FY2026 profit
    # ₹82,390 cr, about ₹224 a share, against continuing EPS of −₹0.61 after the commercial-vehicle demerger).
    eps_row, ni_row = latest(series, "annualDilutedEPS"), latest(series, "annualNetIncome")
    # Diluted average shares of the same year, so a later bonus or split cannot fake a mismatch.
    year_shares = next((r["value"] for r in reversed(series.get("annualDilutedAverageShares", [])) if eps_row and r["date"] == eps_row["date"]), None)
    out["one_off"] = None
    if eps_row and ni_row and eps_row["date"] == ni_row["date"] and year_shares and not eps_row.get("from_profit"):
        implied = ni_row["value"] / year_shares
        if abs(implied - eps_row["value"]) > max(1.0, 0.35 * max(abs(implied), abs(eps_row["value"]))):
            out["one_off"] = {"date": ni_row["date"], "net_income": ni_row["value"], "implied_eps": implied, "eps": eps_row["value"]}
            out["eps_flags"].append(f"FY{ni_row['date'][:4]} net profit {crore_words(ni_row['value'])} is about {eps_words(implied)} a share, but diluted EPS "
                                    f"(continuing operations) is {eps_words(eps_row['value'])}: the profit includes discontinued operations or a one-off "
                                    "gain, so it is not a guide to recurring earnings.")
            distorted = True
    if len(rev_rows) >= 4 and rev_rows[-4]["value"] > 0:
        out["revenue_cagr_3y"] = (rev_rows[-1]["value"] / rev_rows[-4]["value"]) ** (1 / 3) - 1
    else:
        out["revenue_cagr_3y"] = None
    q = series.get("quarterlyNetIncome", [])
    qr = series.get("quarterlyTotalRevenue", [])
    out["latest_quarter"] = q[-1] if q else None
    out["quarter_profit_change"] = change(q[-1]["value"], q[-2]["value"]) if len(q) >= 2 else None
    out["quarter_profit_text"] = swing(q[-1]["value"], q[-2]["value"], crore_words) if len(q) >= 2 else None
    ago = year_ago(q, q[-1]) if q else None
    out["quarter_profit_yoy"] = change(q[-1]["value"], ago["value"]) if ago else None
    out["quarter_profit_yoy_text"] = swing(q[-1]["value"], ago["value"], crore_words) if ago else None
    out["quarter_year_ago"] = ago["date"] if ago else None
    out["quarter_profits"] = [r["value"] for r in q]
    out["quarter_profit_values"] = (q[-1]["value"], ago["value"]) if ago else None
    rago = year_ago(qr, qr[-1]) if qr else None
    out["quarter_revenue_yoy"] = change(qr[-1]["value"], rago["value"]) if rago and q and qr[-1]["date"] == q[-1]["date"] else None
    total = {r["date"]: r["value"] for r in series.get("quarterlyNetIncomeIncludingNoncontrollingInterests", [])}
    if ago and q[-1]["date"] in total and ago["date"] in total:
        out["quarter_total_yoy"] = change(total[q[-1]["date"]], total[ago["date"]])
        out["quarter_total_yoy_text"] = swing(total[q[-1]["date"]], total[ago["date"]], crore_words)
        out["quarter_total_values"] = (total[q[-1]["date"]], total[ago["date"]])
    out["quarter_stale"] = bool(q) and (now.date() - datetime.fromisoformat(q[-1]["date"]).date()).days > 200
    out["financials_as_of"] = latest(series, "annualTotalRevenue")["date"] if latest(series, "annualTotalRevenue") else None
    stale = eps and (now.date() - datetime.fromisoformat(eps["date"]).date()).days > (200 if eps["basis"].startswith("trailing 12 months") else 430)
    out["eps_stale"] = bool(stale)
    out["bank_like"] = ca is None and cl is None and value(series, "annualEBITDA") is None and revenue is not None
    out["revenue"] = revenue
    out["net_income"] = net
    ttm_rev, _ = trailing_sum(series, "trailingTotalRevenue", "quarterlyTotalRevenue")
    out["ttm"] = {"net_income": ttm_net, "revenue": ttm_rev, "date": ttm_date}
    if distorted:
        use_ttm(out, series)
    return out


def use_ttm(out, series, reason="the latest annual figures include one-off items"):
    """Replace annual ROE and net margin with trailing-12-month ones (kept as they were when TTM figures are missing)."""
    t = out.get("ttm") or {}
    book = latest_of(series, "annualStockholdersEquity", "quarterlyStockholdersEquity")
    if t.get("net_income") is None or not book or book["value"] <= 0:
        return False
    books = sorted({r["date"]: r["value"] for n in ("annualStockholdersEquity", "quarterlyStockholdersEquity") for r in series.get(n, [])}.items())
    earlier = [v for d, v in books if 300 <= (date.fromisoformat(book["date"]) - date.fromisoformat(d)).days <= 430]
    base = (book["value"] + earlier[-1]) / 2 if earlier and earlier[-1] > 0 else book["value"]
    out.setdefault("roe_annual", out.get("roe"))
    out["roe"] = t["net_income"] / base
    out["roe_basis"] = f"trailing 12 months to {t['date']} ÷ equity, because {reason}"
    if t.get("revenue"):
        out.setdefault("net_margin_annual", out.get("net_margin"))
        out["net_margin"] = t["net_income"] / t["revenue"]
        out["margin_basis"] = f"net margin: trailing 12 months to {t['date']}"
    return True


def levels(start, candidates, *, above):
    """Reference levels beyond `start`, nearest first (ascending above the price, descending below), near-duplicates merged."""
    keep = sorted(((v, k) for k, v in candidates if v and (v > start * 1.005 if above else v < start * 0.995)),
                  key=lambda vk: vk[0] if above else -vk[0])
    merged = []
    for v, k in keep:
        if merged and abs(v / merged[-1][0] - 1) <= 0.005:
            merged[-1] = (merged[-1][0], merged[-1][1] + " and " + k)
        else:
            merged.append((v, k))
    return merged


NEAR_LEVEL_ATR = 0.5  # price within this many average daily ranges (ATR) of a level is "testing" it


def level_terms(tech):
    """What a close beyond the 20-day high or low would mean, by trend, and whether price is testing either level now.

    In a downtrend a close above the 20-day high breaks the downtrend and one below the low extends the decline (TMPV
    at ₹252.50 in November 2026, a few paise above its ₹252.30 low, was 'testing' it rather than having it as a distant
    floor); in an uptrend the high extends the move and the low weakens it."""
    trend = tech.get("trend")
    price, width = tech.get("last_close"), tech.get("atr14")
    near = lambda level: bool(price and width and level and abs(price - level) <= NEAR_LEVEL_ATR * width)
    if trend == "downtrend":
        up, down = "would break the downtrend", "would extend the decline"
    elif trend == "uptrend":
        up, down = "would extend the move", "would weaken the picture"
    else:
        up, down = "would break the range upward", "would break it downward"
    return {"up": up, "down": down, "testing_high": near(tech.get("resistance")), "testing_low": near(tech.get("support"))}


def levels_words(t):
    """The summary's level phrase: 'above ₹X extends the move, below ₹Y weakens it' in an uptrend, trend-aware otherwise,
    and 'testing ₹Y now' when price is within half an ATR of a level."""
    w = level_terms(t)
    hi, lo = f"₹{num(t['resistance'])}", f"₹{num(t['support'])}"
    if t.get("trend") == "uptrend" and not (w["testing_high"] or w["testing_low"]):
        return f"above {hi} extends the move, below {lo} weakens it"
    up = f"price is testing {hi} now, and a close above it {w['up']}" if w["testing_high"] else f"a close above {hi} {w['up']}"
    down = f"price is testing {lo} now, and a close below it {w['down']}" if w["testing_low"] else f"below {lo} {w['down']}"
    return f"{up}; {down}"


def outlook(tech, cut=None):
    """Empirical forward-return ranges from this stock's own history, plus conditional levels."""
    closes = tech["closes"][-505:]
    price = closes[-1]
    ranges = {}
    for label, n in (("1 week", 5), ("1 month", 21), ("3 months", 63)):
        moves = sorted(closes[i + n] / closes[i] - 1 for i in range(len(closes) - n))
        if len(moves) < 50:
            continue
        def q(p):
            return moves[min(len(moves) - 1, int(p * len(moves)))]
        ranges[label] = {"low_10": price * (1 + q(0.10)), "median": price * (1 + q(0.5)), "high_90": price * (1 + q(0.90)),
                         "share_positive": sum(m > 0 for m in moves) / len(moves), "samples": len(moves)}
    up = levels(tech["resistance"], (("60-day high", tech["resistance_60d"]), ("52-week high", tech["high_52w"])), above=True)
    down = levels(tech["support"], (("60-day low", tech["support_60d"]), ("200-day average", tech["sma200"])), above=False)
    years = len(closes) / YEAR_SESSIONS
    w = level_terms(tech)
    testing = lambda flag, which, level: f"Price is testing the 20-day {which} of ₹{num(level)} now; " if flag else ""
    bull = (testing(w["testing_high"], "high", tech["resistance"]) + ("a" if w["testing_high"] else "A")
            + f" daily close above ₹{num(tech['resistance'])} (20-day high) {w['up']}")
    bear = (testing(w["testing_low"], "low", tech["support"]) + ("a" if w["testing_low"] else "A")
            + f" daily close below ₹{num(tech['support'])} (20-day low) {w['down']}")
    return {
        "bias": tech["trend"],
        "ranges": ranges,
        "history_years": round(years, 1), "history_sessions": len(closes),
        "bull_case": bull + "".join(f"; next reference ₹{num(v)} ({k})" for v, k in up) + ".",
        "bear_case": bear + "".join(f"; next reference ₹{num(v)} ({k})" for v, k in down) + ".",
        "bull_levels": [v for v, _ in up], "bear_levels": [v for v, _ in down],
        "momentum_note": None if tech["rsi14"] is None else
            "RSI is above 70: the recent rise is stretched and short pullbacks are common from here." if tech["rsi14"] > 70 else
            "RSI is below 30: the recent fall is stretched and short rebounds are common from here." if tech["rsi14"] < 30 else None,
        "method": f"Ranges are the 10th, 50th and 90th percentiles of this stock's own overlapping past returns over the last {years:.1f} years "
                  f"({len(closes)} sessions from {tech['close_dates'][-len(closes)]}), applied to today's price"
                  + (f"; prices before the {cut['ex_date']} demerger are scaled by {cut['factor']:.4f} to be comparable" if cut and cut["mode"] == "scaled" else "")
                  + ". Past behaviour can change; earnings, news and market shocks can break any range.",
    }


def company_terms(name):
    """'Reliance Industries Limited' -> 'Reliance Industries' for search and headline matching."""
    skip = {"limited", "ltd", "the", "inc", "corporation", "corp", "co", "company"}
    words = [w for w in re.sub(r"[^A-Za-z0-9& ]", " ", name).split() if w.lower() not in skip]
    return " ".join(words[:3]) or name


def gather_news(selected, name, now, *, fetcher, news_fetcher, hours):
    from data import rss
    items, failures = [], []
    base = selected.rsplit(".", 1)[0]
    term = company_terms(name)
    days = max(1, round(hours / 24))
    query = f'"{term}" (share OR stock OR results OR NSE) when:{days}d'
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
    try:
        for r in rss(news_fetcher(url), url, now)["records"]:
            if instant(r["published_at"]) >= now - timedelta(hours=hours):
                title, _, outlet = r["headline"].rpartition(" - ")
                items.append({"headline": title or r["headline"], "outlet": outlet if title else None, "url": r["url"],
                              "published_at": r["published_at"], "source": "Google News search"})
    except Exception:  # news must never break the stock report
        failures.append({"source": "Google News", "reason": "Unavailable or invalid feed"})
    try:
        from research import news
        report = news([selected], now, fetcher=fetcher, hours=hours)
        failures += report.get("failed_sources", [])
        words = term.upper().split()[:2]
        pattern = r"(?<![A-Z0-9])" + re.escape(base) + r"(?![A-Z0-9])"
        for r in report.get("records", []):
            head = r["headline"].upper()
            if selected in r.get("symbols", []) or re.search(pattern, head) or (words and all(w in head for w in words)):
                items.append({"headline": r["headline"], "outlet": "NSE" if "nseindia" in r["source"] else "Yahoo", "url": r["url"],
                              "published_at": r["published_at"], "source": r["source"]})
    except Exception:
        failures.append({"source": "Yahoo/NSE", "reason": "Unavailable or invalid feed"})
    unique = {}
    for item in items:
        unique.setdefault(item["headline"].lower(), item)
    kept, dropped = [], {"tips": 0, "unrelated": 0}
    match = headline_matcher(base, name)
    for item in unique.values():
        verdict = headline_verdict(item["headline"], match)
        if verdict == "keep":
            kept.append(item)
        else:
            dropped[verdict] += 1
    # Newest first; ties broken by headline so two runs a minute apart order (and pick) the same way.
    ordered = sorted(kept, key=lambda r: (r["published_at"], r["headline"]), reverse=True)
    left_out = (f"{dropped['tips'] + dropped['unrelated']} headline(s) left out: "
                + ", ".join(t for t, n in (("stock-tip lists, price targets and quote pages", dropped["tips"]), ("headlines that do not clearly name the company", dropped["unrelated"])) if n)
                + ".") if dropped["tips"] or dropped["unrelated"] else None
    # The summary's headline: the newest story about the company itself (its name or symbol leads the headline, or an
    # exchange filing) before one where it is mentioned in a market round-up ("Sensex ... Tata Motors tops gains").
    # Within those, routine items (a broker conference "update", an analyst meet, a newspaper advertisement filing) come
    # after real news: Reliance's summary on 18 Nov 2026 led with "... JM Financial Conference Update · Kalkine India".
    lead = sorted(ordered, key=lambda r: (headline_leads(r, match) and not ROUTINE_HEADLINE.search(r["headline"]), headline_leads(r, match),
                                          r["published_at"], r["headline"]), reverse=True)
    return {"items": ordered[:12], "failures": failures, "query": term, "dropped": dropped, "left_out": left_out,
            "headline": lead[0] if lead else None,
            "note": "Headlines only; open the story before treating it as fact. Search results can include unrelated or promotional items."}


# Tip lists, price targets and many-stock round-ups say nothing specific about the company (seen as the "latest
# headline" for Reliance on 18 Nov 2026: "Six Stocks To Buy Today ..."; for TMCV: a TMPV price-target story).
TIP_HEADLINE = re.compile(
    r"stocks? to (?:buy|sell|watch|track|add)|top (?:\w+ )?(?:picks|stocks|gainers|losers)|buy or sell|buy,? sell or hold|should you buy|"
    r"price target|target price|\btarget (?:of|at|to|price)\b|\btgt\b|multibagger|stock picks|stocks? recommendations?|trading (?:ideas|calls)|"
    r"intraday (?:picks|calls|stocks)|stocks? in (?:focus|news)|technical picks|brokerages? (?:see|bullish|bearish|recommend)|"
    r"\b(?:puts?|calls?) (?:draw|see|attract)|contracts on|open interest|options? (?:activity|buildup|data)|stock ?iq|investor analysis|"
    r"time to buy|what (?:brokerages|analysts|experts) (?:say|suggest)|(?:share |stock )?price prediction|key levels (?:for|to watch)|ipo review|recommendations? by|(?:share|stock) price (?:today|live)|live (?:nse|bse)|"
    r"(?:share|stock) price(?: (?:&|and) chart)?\s*$|\((?:nsei?|bse):[a-z0-9&-]+\)|"
    # Quote pages, not news: "Reliance Industries Limited (RELIANCE.NS) stock price, news, quote and history".
    r"stock price, news, quote|quote (?:and|&) history|\([a-z0-9&-]+\.(?:ns|bo)\)|"
    r"\b(?:\d+|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)\s+(?:\S+\s+){0,2}(?:stocks|heavyweights|picks)\b",
    re.I)
# Routine items that say little about the business: kept in the list, but not picked as the summary's headline.
ROUTINE_HEADLINE = re.compile(
    r"\bconference (?:update|call)\b|\b(?:analysts?|investors?)(?: ?/ ?institutional investors?)? meet(?:ing)?s?\b|\bcon\. ?call\b|"
    r"earnings call transcript|investor presentation|\bupdate\s*$|newspaper (?:publication|advertisement)|trading window|"
    r"compliance certificate|(?:loss|duplicate|issue) of (?:duplicate )?share certificates?|record date|book closure|"
    r"\b(?:schedule of|intimation of) (?:analyst|investor)", re.I)
# Words after a symbol-only match that name a different company (Reliance Power, Reliance Infrastructure, ...).
OTHER_COMPANY_WORDS = ("POWER", "INFRA", "INFRASTRUCTURE", "CAPITAL", "COMMUNICATIONS", "NAVAL", "HOME FINANCE")


def headline_matcher(base, name, aliases=None, demergers=None):
    """Words that tie a headline to this company, and words that tie it to a sister company from the same split."""
    words = company_terms(name).split()
    own = [base]
    siblings = []
    shared = False
    for entry in (load_aliases() if aliases is None else aliases).values():
        succ = entry.get("successors") or []
        if any(s.get("symbol") == base for s in succ):
            for s in succ:
                (own if s.get("symbol") == base else siblings).extend([s["symbol"], *(s.get("news_terms") or [])])
                # Successors that still share the old name (both are "Tata Motors" in headlines) need their own words.
                shared = shared or (s.get("symbol") == base and bool(s.get("news_terms")))
    for d in (load_demergers() if demergers is None else demergers):
        pair = [d.get("parent"), d.get("resulting_symbol")]
        if base in pair:
            siblings += [x for x in pair if x and x != base and x not in siblings]
    return {"name": " ".join(words[:2]).upper() if len(words) >= 2 else (words[0].upper() if words else base),
            "own": [w.upper() for w in own if w], "siblings": [w.upper() for w in siblings if w], "shared_name": shared}


MARKET_ROUNDUP = re.compile(r"\b(?:sensex|nifty|stock market|markets? (?:today|live|wrap|close)|closing bell|opening bell)\b", re.I)


def headline_leads(item, match):
    """True when the headline is about the company: an NSE filing, or its name/symbol opens the headline and it is
    not a market round-up."""
    head = " ".join(item["headline"].upper().split())
    if item.get("outlet") == "NSE":
        return True
    opens = any(head.startswith(w) for w in [match["name"], *match["own"]])
    return opens and not MARKET_ROUNDUP.search(head)


def _has(text, word):
    return re.search(r"(?<![A-Z0-9])" + re.escape(word) + r"(?![A-Z0-9])", text) is not None


def headline_verdict(headline, match):
    """'keep', 'tips' (tip list / price target / round-up) or 'unrelated' (does not name this company, or names only
    its sister company from the same demerger)."""
    if TIP_HEADLINE.search(headline):
        return "tips"
    head = " ".join(headline.upper().split())
    own = any(_has(head, w) for w in match["own"]
              if not any(head.find(w + " " + x) >= 0 for x in OTHER_COMPANY_WORDS))
    named = _has(head, match["name"]) or own
    if not named:
        return "unrelated"
    if not own and (match.get("shared_name") or any(_has(head, w) for w in match["siblings"])):
        return "unrelated"  # names only the sister company, or only the name both companies share
    return "keep"


BUSINESS_PEERS_PATH = ASSETS / "business_peers.json"


def load_business_peers(path=None):
    """{'checked': date, 'companies': {SYMBOL: {'peers': {SYM: business}, 'holdings': [SYM], 'rest': words}}} or {}."""
    try:
        data = json.loads(Path(path or BUSINESS_PEERS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("companies"), dict) else {}


def business_entry(base, table=None):
    table = load_business_peers() if table is None else table
    return ((table or {}).get("companies") or {}).get(base) or {}, (table or {}).get("checked")


def business_group(base, table=None):
    """The curated business peers of a company whose index peers are different businesses (Reliance: refiners, telecom
    and retail rather than every Nifty Oil & Gas stock), or None."""
    entry, checked = business_entry(base, table)
    peers = entry.get("peers") or {}
    if not peers:
        return None
    by_business = {}
    for sym, what in peers.items():
        by_business.setdefault(what, []).append(sym)
    why = "; ".join(f"{what}: {', '.join(syms)}" for what, syms in by_business.items())
    return {"basis": f"companies in the same businesses ({why}; curated list checked {checked})", "kind": "business",
            "peers": [{"symbol": s} for s in peers if s != base][:12], "group_size": len([s for s in peers if s != base]) + 1}


def listed_holding(parent, symbol, *, fetcher, nse_fetcher, now):
    """The parent's stake in one listed company (promoter entities in that company's latest NSE shareholding pattern, the
    pattern filed on listing for a new listing) and that company's market value and P/E."""
    from shareholding import api_url, filings, parse_xbrl, same_entity
    from screener import snapshot
    raw = nse_fetcher(api_url("/api/corporate-share-holdings-master", symbol))
    listed = filings(raw)
    quarterly = bool(listed)
    listed = listed or filings(raw, quarterly=False)[:1]
    if not listed or not listed[0].get("xbrl"):
        raise Block(f"STOCK: no shareholding pattern on NSE for {symbol}")
    pattern = parse_xbrl(nse_fetcher(listed[0]["xbrl"]))
    held = [h for h in pattern.get("promoter_holders") or [] if same_entity(h["name"], parent)]
    snap = snapshot(symbol, fetcher=fetcher, now=now)
    return {"symbol": symbol, "name": snap.get("name") or pattern.get("company") or symbol, "stake_pct": sum(h["pct"] for h in held) if held else None,
            "holder": held[0]["name"] if held else None, "pattern_date": listed[0]["quarter"], "filed": listed[0].get("filed"),
            "quarterly": quarterly, "market_cap": snap["market_cap_cr"] * 1e7 if snap.get("market_cap_cr") else None, "pe": snap.get("pe"),
            "price_date": snap.get("price_date"),
            # A consolidated lender's borrowings and book, for the parent's debt/equity without its lending book.
            "bank_like": snap.get("bank_like"), "debt_cr": snap.get("debt_cr"), "debt_date": snap.get("debt_date"), "equity_cr": snap.get("equity_cr")}


def load_holdings(base, parent, *, fetcher, nse_fetcher, now, table=None):
    """Each listed holding of a curated company (Reliance: Jio Platforms), loaded in parallel; failures are kept as
    {'symbol', 'error'}."""
    entry, _ = business_entry(base, table)
    symbols = list(entry.get("holdings") or [])
    if not symbols:
        return None

    def one(sym):
        try:
            return listed_holding(parent, sym, fetcher=fetcher, nse_fetcher=nse_fetcher, now=now)
        except (Block, ValueError, KeyError, TypeError, OSError, SyntaxError) as error:
            return {"symbol": sym, "error": str(error).replace("STOCK: ", "") or "unavailable"}
    with ThreadPoolExecutor(max_workers=min(3, len(symbols))) as pool:
        rows = list(pool.map(one, symbols))
    return {"rows": rows, "rest": entry.get("rest")}


def sum_of_parts(own_cap, own_pe, holdings):
    """Listed stakes at market value against the parent's market cap, and what the market implicitly pays for the rest:
    its value ÷ the parent's trailing earnings less the holdings' share of theirs (an estimate; no holding-company
    discount). Returns None when no stake could be valued."""
    if not holdings or not own_cap:
        return None
    rows = [r for r in holdings["rows"] if not r.get("error") and r.get("stake_pct") and r.get("market_cap")]
    if not rows:
        return None
    for r in rows:
        r["value"] = r["stake_pct"] / 100 * r["market_cap"]
    total = sum(r["value"] for r in rows)
    out = {"rows": rows, "total": total, "share_of_cap": total / own_cap, "rest_value": own_cap - total, "rest": holdings.get("rest"),
           "missing": [r for r in holdings["rows"] if r not in rows]}
    if own_pe and own_pe > 0 and all(r.get("pe") and r["pe"] > 0 for r in rows):
        rest_earnings = own_cap / own_pe - sum(r["stake_pct"] / 100 * r["market_cap"] / r["pe"] for r in rows)
        if rest_earnings > 0 and out["rest_value"] > 0:
            out["rest_pe"] = out["rest_value"] / rest_earnings
    return out


def sotp_line(name, s):
    """'Sum of parts (listed holdings at market value, no holding-company discount): …' for the summary."""
    parts = []
    for r in s["rows"]:
        when = (f"NSE shareholding pattern for the quarter to {day_month_year(r['pattern_date'])}" if r["quarterly"]
                else f"NSE shareholding pattern filed on listing, dated {day_month_year(r['pattern_date'])}")
        parts.append(f"{r['stake_pct']:.2f}% of {r['name']} ({r['symbol']}; {when}) × its market cap {crore(r['market_cap'])} = {crore(r['value'])}")
    text = f"Sum of parts (listed holdings at market value, before any holding-company discount): " + "; ".join(parts)
    if s["rest_value"] <= 0:
        # Grasim, Nov 2026: its UltraTech and Aditya Birla Capital stakes alone came to 128% of its market cap.
        text += (f" — {pct(s['share_of_cap'], 0, sign=False)} of {name}'s own market cap: the listed stakes alone are worth more than the "
                 f"whole company, so the market values {s.get('rest') or 'the rest of the business'} at nothing and applies about a "
                 f"{pct(1 - 1 / s['share_of_cap'], 0, sign=False)} discount to the stakes")
        return text + "."
    text += (f" — {pct(s['share_of_cap'], 0, sign=False)} of {name}'s own market cap, leaving {crore(s['rest_value'])} for "
             + (s.get("rest") or "the rest of the business"))
    if s.get("rest_pe"):
        text += f", about {s['rest_pe']:.1f}× that part's share of trailing earnings (estimate)"
    if s.get("missing"):
        text += "; not valued: " + ", ".join(f"{r['symbol']} ({r.get('error') or 'stake not found among promoter entities'})" for r in s["missing"])
    return text + "."


LENDER_CONTROL = 50.0     # % held above which a listed subsidiary is consolidated line by line
LENDER_DEBT_SHARE = 0.25  # a consolidated lender holding this share of the group's borrowings gets a debt/equity note


def lender_debt(fund, holdings):
    """A parent that consolidates a listed lender (Larsen & Toubro and L&T Finance) reports group borrowings that are
    mostly the lender's lending book. Returns {'subsidiaries', 'group_debt', 'share', 'ex_de', ...} when the lenders'
    borrowings are at least LENDER_DEBT_SHARE of the group's, else None. Debt/equity without them takes their whole
    borrowings out and the parent's share of their equity out of equity (an estimate: other consolidation entries are
    ignored)."""
    if not fund or fund.get("bank_like") or not fund.get("total_debt") or not fund.get("debt_to_equity") or not holdings:
        return None
    subs = [r for r in holdings.get("rows") or [] if not r.get("error") and (r.get("stake_pct") or 0) > LENDER_CONTROL
            and r.get("bank_like") and r.get("debt_cr")]
    if not subs:
        return None
    group = fund["total_debt"]
    equity = group / fund["debt_to_equity"]
    debt = sum(r["debt_cr"] * 1e7 for r in subs)
    share = debt / group
    if share < LENDER_DEBT_SHARE:
        return None
    out = {"subsidiaries": [{"symbol": r["symbol"], "name": r.get("name") or r["symbol"], "stake_pct": r["stake_pct"], "debt": r["debt_cr"] * 1e7,
                             "debt_date": r.get("debt_date")} for r in subs],
           "group_debt": group, "group_date": fund.get("debt_date"), "share": share, "ex_de": None}
    if share <= 1.02 and all(r.get("equity_cr") for r in subs):
        ex_equity = equity - sum(r["stake_pct"] / 100 * r["equity_cr"] * 1e7 for r in subs)
        if ex_equity > 0:
            out["ex_de"] = max(group - debt, 0.0) / ex_equity
    return out


def lender_debt_words(f):
    """The ⚠ line for lender_debt()."""
    x = (f or {}).get("lender_debt")
    if not x:
        return None
    names = " and ".join(f"{s['name']} ({s['symbol']}, {s['stake_pct']:.0f}% owned; borrowings {crore(s['debt'])}"
                         + (f" at {day_month_year(s['debt_date'])}" if s.get("debt_date") else "") + ")" for s in x["subsidiaries"])
    text = (f"Debt/equity of {num(f['debt_to_equity'], 2)} is consolidated and mostly a lending book: {names}, a lender, "
            f"{'is' if len(x['subsidiaries']) == 1 else 'are'} about {min(x['share'], 1.0) * 100:.0f}% of the group's borrowings of "
            f"{crore(x['group_debt'])}" + (f" at {day_month_year(x['group_date'])}" if x.get("group_date") else "") + ".")
    if x.get("ex_de") is not None:
        text += (" Without the lender (its borrowings out, and the parent's share of its equity out of equity) debt/equity would be about "
                 f"{num(x['ex_de'], 2)} (estimate).")
    return text


MULTI_YEARS = 3
JUMP_GROWTH = 0.4   # one year's revenue growth of at least this ...
JUMP_SHARE = 0.7    # ... carrying at least this share of the whole window's growth (in simple terms) is a step change


def multi_year_growth(fund, series, annual=None, backfill=None, base=None, years=MULTI_YEARS, demergers=None):
    """Revenue and profit CAGR over `years` financial years from NSE's annual results filings (the newest years from
    the March-quarter integrated filings, older ones from the annual filings read for the P/E history), else from
    Yahoo's annual statements. One year can hide a slow trend: Asian Paints FY2026 revenue +5.0% against about +1% a
    year since FY2023. Returns {'revenue_cagr', 'profit_cagr', 'start', 'end', 'source', ...} or None."""
    if (fund or {}).get("growth_withheld"):
        return None
    points = {}
    for f in (backfill or {}).get("filings") or []:
        if f.get("revenue"):
            points[f["date"]] = {"revenue": f["revenue"], "profit": f.get("profit"), "basis": (f.get("basis") or "").lower()}
    for day, y in (annual or {}).items():
        if y.get("revenue"):
            points[day] = {"revenue": y["revenue"], "profit": y.get("profit"), "basis": (y.get("basis") or "").lower()}
    out = None
    if points:
        end = max(points)
        start = date(int(end[:4]) - years, int(end[5:7]), int(end[8:10])).isoformat()
        a, b = points.get(start), points[end]
        if a and a["basis"] == b["basis"] and a["revenue"] > 0:
            out = {"start": start, "end": end, "revenue": (a["revenue"], b["revenue"]), "profit": (a.get("profit"), b.get("profit")),
                   "source": f"NSE {b['basis'] or 'annual'} results filings",
                   "yearly": {d: p["revenue"] for d, p in points.items() if start <= d <= end and p["basis"] == b["basis"]}}
    if out is None:
        rev = {r["date"]: r["value"] for r in series.get("annualTotalRevenue") or []}
        net = {r["date"]: r["value"] for r in series.get("annualNetIncome") or []}
        if rev:
            end = max(rev)
            start = next((d for d in sorted(rev) if 365 * years - 20 <= (date.fromisoformat(end) - date.fromisoformat(d)).days <= 365 * years + 20), None)
            if start and rev[start] > 0:
                out = {"start": start, "end": end, "revenue": (rev[start], rev[end]), "profit": (net.get(start), net.get(end)),
                       "source": "Yahoo annual statements", "yearly": {d: v for d, v in rev.items() if start <= d <= end}}
    if out is None:
        return None
    out["years"] = years
    # A large demerger after the first year compares different businesses: inside the window, or after it, when the
    # latest filings already show the demerged businesses as discontinued (Vedanta FY2026: revenue -19% a year).
    for ev in parent_events(base, demergers) if base else []:
        share = demerged_share(ev)
        if out["start"] < (ev.get("record_date") or "") and (share is None or share >= MINOR_SHARE):
            return {**out, "withheld": f"the {ev['record_date']} demerger came after FY{out['start'][:4]}, so the years compare different businesses"}
    cagr = lambda pair: (pair[1] / pair[0]) ** (1 / years) - 1 if pair[0] and pair[1] and pair[0] > 0 and pair[1] > 0 else None
    out["revenue_cagr"], out["profit_cagr"] = cagr(out["revenue"]), cagr(out["profit"])
    p0, p1 = out["profit"]
    out["profit_note"] = (None if out["profit_cagr"] is not None or p0 is None or p1 is None else
                          f"loss in FY{out['start'][:4]}" if p0 <= 0 else f"loss in FY{out['end'][:4]}")
    # One year carrying most of the growth (HDFC Bank's merger with HDFC in FY2024) makes the average misleading.
    yearly = out.pop("yearly", {})
    days = sorted(yearly)
    steps = [(b, yearly[b] / yearly[a] - 1) for a, b in zip(days, days[1:])
             if yearly[a] > 0 and 300 <= (date.fromisoformat(b) - date.fromisoformat(a)).days <= 430]
    big = max(steps, key=lambda x: x[1]) if steps else None
    rate = out["revenue_cagr"]
    out["jump"] = ({"year": big[0], "growth": big[1]} if big and big[1] >= JUMP_GROWTH and (rate is None or rate <= 0 or big[1] >= JUMP_SHARE * rate * years)
                   else None)
    return out


def cagr_words(g):
    """'3-year CAGR FY2023–FY2026: revenue +1.0% a year, profit +1.7% a year (NSE consolidated results filings)'."""
    if not g:
        return None
    if g.get("withheld"):
        return f"{g['years']}-year growth not comparable ({g['withheld']})"
    profit = (pct(g["profit_cagr"]) + " a year" if g.get("profit_cagr") is not None else
              f"n/m ({g['profit_note']})" if g.get("profit_note") else "n/a")
    jump = g.get("jump")
    return (f"{g['years']}-year CAGR FY{g['start'][:4]}–FY{g['end'][:4]}: revenue {pct(g['revenue_cagr'])} a year, "
            f"profit attributable to shareholders {profit} ({g['source']})"
            + (f" — most of it in one year (FY{jump['year'][:4]} revenue {pct(jump['growth'], 0)}), so the average hides a step change "
               "such as a merger or acquisition" if jump else ""))


LOW_ROE_GAP = 0.08     # ROE at least this far below the peer median ...
LOW_ROE_RATIO = 0.6    # ... and under this share of it is "well below peers"
GOODWILL_SHARE = 0.3   # goodwill and intangibles at least this share of equity explain a low ROE
CASH_SHARE = 0.35      # cash and short-term investments at least this share of equity


def low_roe_reason(fund, series, peer_roe):
    """One line on why ROE is well below peers', from the balance sheet: acquisition goodwill and intangibles (Tata
    Consumer: Capital Foods and Organic India), or a cash pile; else that neither explains it. None when ROE is not
    well below peers' or the balance sheet gives neither figure."""
    roe = (fund or {}).get("roe")
    if roe is None or peer_roe is None or fund.get("bank_like") or not (roe < peer_roe - LOW_ROE_GAP and roe < LOW_ROE_RATIO * peer_roe):
        return None
    equity = latest_of(series, "quarterlyStockholdersEquity", "annualStockholdersEquity")
    if not equity or equity["value"] <= 0:
        return None
    by_date = lambda *names: {r["date"]: r["value"] for n in names for r in series.get(n) or []}
    goodwill = by_date("annualGoodwillAndOtherIntangibleAssets", "quarterlyGoodwillAndOtherIntangibleAssets")
    cash = by_date("annualCashCashEquivalentsAndShortTermInvestments", "quarterlyCashCashEquivalentsAndShortTermInvestments")
    if not goodwill and not cash:
        return None
    reasons = []
    gw = goodwill.get(equity["date"])
    if gw and gw / equity["value"] >= GOODWILL_SHARE:
        text = (f"goodwill and other intangibles of {crore(gw)} are {gw / equity['value'] * 100:.0f}% of shareholders' equity "
                f"({day_month_year(equity['date'])})")
        first = min(goodwill)
        if (date.fromisoformat(equity["date"]) - date.fromisoformat(first)).days >= 700 and goodwill[first] > 0 and gw / goodwill[first] >= 1.3:
            text += f", up from {crore(goodwill[first])} at {day_month_year(first)}, which points to acquisitions"
        net = fund.get("net_income")
        tangible = equity["value"] - gw
        if net and net > 0 and tangible > 0:
            text += f"; on the {crore(tangible)} of equity without them, the same profit would be a return of about {net / tangible * 100:.0f}%"
        reasons.append(text)
    c = cash.get(equity["date"])
    if c and c / equity["value"] >= CASH_SHARE:
        reasons.append(f"cash and short-term investments of {crore(c)} are {c / equity['value'] * 100:.0f}% of equity and usually earn less "
                       "than the business")
    if not reasons:
        reasons.append("neither goodwill nor cash is a large part of equity, so the gap comes from the business's own margins and asset use"
                       + (f" (net margin {pct(fund['net_margin'], sign=False)})" if fund.get("net_margin") is not None else ""))
    return (f"ROE {pct(roe, sign=False)} is well below the peer median of {pct(peer_roe, sign=False)}; likely reason from the balance sheet: "
            + "; ".join(reasons) + ".")


def demerger_pe_flag(base, eps, price, quarters=None, demergers=None, today=None):
    """A caution when the EPS behind the P/E still includes businesses a demerger has since taken away: trailing EPS for
    the four quarters to 30 Sep 2026 includes Vedanta's aluminium, power, oil-and-gas and iron-ore businesses for the
    seven months before their 1 May 2026 demerger (P/E 4.3 against about 7 on aggregators). Uses
    assets/demergers.json; None when no large demerger falls inside the EPS period."""
    if not eps or not eps.get("date"):
        return None
    end = date.fromisoformat(eps["date"][:10])
    start = end - timedelta(days=365)
    for ev in parent_events(base, demergers):
        share = demerged_share(ev)
        if share is not None and share < MINOR_SHARE:
            continue
        cut = ev.get("appointed_date") or ev.get("effective_date") or ev.get("record_date")
        if not cut or (today and cut > today.isoformat()):
            continue
        day = date.fromisoformat(cut)
        if day <= start:
            continue
        months = round(min((day - start).days, 365) / 30.4)
        who = re.sub(r"\s*\([^)]*\)", "", ev.get("resulting_company") or "the demerged business").strip()
        text = (f"P/E caution: the EPS behind it ({eps.get('basis', 'EPS')}, to {day_month_year(eps['date'][:10])}) includes about {months} months "
                f"of {who} before the {day_month_year(cut)} demerger, so the P/E is lower than on the continuing business alone"
                + (f" ({share:.0f}% of the pre-demerger cost went to the demerged business)" if share else ""))
        after = [q for q in quarters or [] if q.get("value") is not None and date.fromisoformat(q["date"][:10]) - timedelta(days=88) >= day]
        if after and price and after[-1]["value"] > 0:
            q = after[-1]
            text += (f"; on the {day_month_year(q['date'][:10])} quarter alone, the first wholly after it, annualised (×4; rough: one quarter, "
                     f"seasonality ignored) the P/E would be about {price / (4 * q['value']):.1f}")
        return text + ". Check the company's post-demerger results before comparing it with peers or its own history."
    return None


PEER_FIELDS = ("market_cap_cr", "pe", "pb", "roe", "net_margin", "revenue_growth", "return_1y")
PE_SPREAD = 5  # no peer median P/E when the highest is more than this multiple of the lowest


def load_peers(selected, *, fetcher, now, chosen=None, nse_fetcher=None):
    """The peer group and each peer's snapshot (None where one failed): the slow, network part of the comparison."""
    from screener import peers_for, snapshot
    if chosen:
        base = selected.rsplit(".", 1)[0]
        picked = [s.strip().upper().replace(".NS", "") for s in chosen if s.strip()]
        group = {"basis": "peers chosen for this business", "kind": "chosen",
                 "peers": [{"symbol": s} for s in dict.fromkeys(picked) if s != base][:12]}
        group["group_size"] = len(group["peers"]) + 1
    else:
        # A curated business list first (Reliance: refiners, telecom and retail), else the NSE index or industry group.
        group = business_group(selected.rsplit(".", 1)[0]) or peers_for(selected, fetcher=fetcher)
        group.setdefault("kind", "index")
    if not group["peers"]:
        return group, []

    def one(member):
        try:
            return snapshot(member["symbol"], fetcher=fetcher, now=now, nse_fetcher=nse_fetcher)
        except (Block, ValueError, KeyError, TypeError, OSError):
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        loaded = list(pool.map(one, group["peers"]))
    return group, loaded


def peer_comparison(selected, own, *, fetcher=None, now=None, show=6, chosen=None, loaded=None, nse_fetcher=None):
    """Peer rows and medians; `loaded` is load_peers()'s result when it already ran in the background."""
    group, loaded = loaded if loaded is not None else load_peers(selected, fetcher=fetcher, now=now, chosen=chosen, nse_fetcher=nse_fetcher)
    if not group["peers"]:
        return {"basis": group["basis"], "kind": group["kind"], "rows": [], "medians": {}}
    rows = [r for r in loaded if r and r["symbol"] != selected]
    everyone = rows + [own]
    medians = {}
    for field in PEER_FIELDS:
        values = sorted(r[field] for r in everyone if r.get(field) is not None)
        if values:
            medians[field] = statistics.median(values)
    # A median of P/Es that span more than 5x (an oil refiner at 9 next to a city-gas distributor at 50) describes no one.
    pes = [r["pe"] for r in everyone if r.get("pe") and r["pe"] > 0]
    spread = max(pes) / min(pes) if len(pes) >= 2 else None
    pe_wide = bool(spread and spread > PE_SPREAD)
    if pe_wide:
        medians.pop("pe", None)
    ranked = sorted(rows, key=lambda r: -(r.get("market_cap_cr") or 0))
    caps = [r["market_cap_cr"] for r in rows if r.get("market_cap_cr")]
    mismatch = bool(caps and own.get("market_cap_cr") and own["market_cap_cr"] > 3 * max(caps))
    return {"pe_spread": spread, "pe_spread_wide": pe_wide, "pe_list": sorted(([r["symbol"].replace(".NS", ""), r["pe"]] for r in rows if r.get("pe")), key=lambda x: x[1]),
            "basis": group["basis"], "kind": group["kind"], "group_size": group.get("group_size"), "evaluated": len(everyone),
            "capped": group.get("capped", False), "truncated": len(ranked) > show, "rows": ranked[:show], "own": own, "medians": medians,
            "loss_making": [r["symbol"].replace(".NS", "") for r in ranked if r.get("loss_making") and not r.get("pe")],
            "size_mismatch": mismatch, "failed": [m["symbol"] for m, r in zip(group["peers"], loaded) if not r],
            "renamed": [r["renamed_note"] for r in rows if r.get("renamed_note")],
            "notes": [f"{r['symbol'].replace('.NS', '')}: {r['revenue_note']}" for r in ranked[:show] if r.get("revenue_note")]}


def prepare_history(selected, *, fetcher, now, years, actions_fetcher=None):
    """History for analysis: follows a known old symbol, then makes prices comparable across a demerger of this company.

    With `actions_fetcher`, single-day falls over 4% are checked against NSE's corporate actions for the symbol.
    Returns (symbol used, history, notes) with notes {"renamed", "demerger", "gaps"}.
    """
    used, history, renamed = resolve_history(selected, fetcher=fetcher, now=now, years=years)
    cut = trim_for_demerger(history, used)
    split_days = {datetime.fromtimestamp(int(s.get("date", k)), IST).date().isoformat()
                  for k, s in ((history.get("corporate_actions") or {}).get("splits") or {}).items() if isinstance(s, dict)}
    demerger_days = {e.get("ex_date") or e["record_date"] for e in parent_events(used.rsplit(".", 1)[0])}
    gaps = unexplained_gaps(history["bars"], split_days | demerger_days)
    bars = history["bars"]
    if actions_fetcher and used.endswith(".NS") and any(b["close"] < a["close"] * 0.96 for a, b in list(zip(bars, bars[1:]))[-520:]):
        actions = nse_actions(used[:-3], actions_fetcher)
        matched = action_gaps(bars, actions, split_days, demerger_days)
        days = {g.split(" on ")[1][:10] for g in matched}
        gaps = [g for g in gaps if g.split(" on ")[1][:10] not in days] + matched
    return used, history, {"renamed": renamed, "demerger": cut, "gaps": gaps}


def apply_dividends(fund, history, price, since=None):
    """Fill the headline dividend yield from the chart's dividend events (None when they cannot establish it)."""
    info = trailing_dividends(history, price, since=since)
    fund["dividend"] = info
    fund["dividend_yield"] = info["dividend_yield"] if info else None
    return fund


def financial_year(day):
    """Indian financial year label: FY2026 = April 2025 to March 2026."""
    return day.year + 1 if day.month >= 4 else day.year


def fy_label(year):
    """'FY2019-20' for the financial year ending March 2020."""
    return f"FY{year - 1}-{str(year)[2:]}"


def dividend_record(selected, *, fetcher, now, fund=None, nse_fetcher=None):
    """Ten years of dividends per share by financial year (Yahoo monthly chart events, split/bonus adjusted), with
    consecutive years paid, the 5-year DPS growth rate, and payout and free-cash-flow cover from the statements."""
    raw = fetcher("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(selected, safe="")
                  + "?" + urllib.parse.urlencode({"range": "10y", "interval": "1mo", "events": "div,splits"}))
    result = json.loads(raw)["chart"]["result"][0]
    meta = result["meta"]
    if meta.get("symbol") != selected or meta.get("currency") != "INR":
        raise Block("STOCK: dividend history did not match the symbol")
    stamps = result.get("timestamp") or []
    first = datetime.fromtimestamp(int(stamps[0]), IST).date() if stamps else None
    by_year = {}
    for d in ((result.get("events") or {}).get("dividends") or {}).values():
        amount = (d or {}).get("amount")
        if isinstance(amount, (int, float)) and math.isfinite(amount) and amount > 0:
            day = datetime.fromtimestamp(int(d["date"]), IST).date()
            if day <= now.date():
                by_year[financial_year(day)] = by_year.get(financial_year(day), 0.0) + float(amount)
    current = financial_year(now.date())
    complete = current - 1
    oldest = financial_year(first) + 1 if first else None  # the first year may be partial
    streak = 0
    year = current if by_year.get(current) else complete
    while by_year.get(year) and (oldest is None or year >= oldest):
        streak += 1
        year -= 1
    capped = oldest is not None and year < oldest
    # A single empty year between paid years is often a feed gap (Yahoo has no Hindustan Zinc dividend in FY2019-20),
    # so NSE's corporate actions are checked before the streak is allowed to end there.
    gap_note, nse_years = None, set()
    if streak and not capped and by_year.get(year - 1) and (oldest is None or year - 1 >= oldest):
        gap = year
        actions = nse_actions(selected.rsplit(".", 1)[0], nse_fetcher) if nse_fetcher and selected.endswith(".NS") else None
        nse_years = {financial_year(date.fromisoformat(d)) for d, subject in actions or [] if "ividend" in subject}
        if gap in nse_years:
            while (by_year.get(year) or year in nse_years) and (oldest is None or year >= oldest):
                streak += 1
                year -= 1
            capped = oldest is not None and year < oldest
            gap_note = f"{fy_label(gap)} dividend missing from Yahoo's history; NSE's corporate actions show one, so the streak continues through it"
        else:
            gap_note = (f"streak may be understated (data gap {fy_label(gap)}: no dividend ex-date that year on Yahoo"
                        + (" or NSE" if actions and min(d for d, _ in actions) <= f"{gap - 1}-04-01" else "") + ", though the years either side paid)")
    # Five-year growth between two-year averages (FY n-1..n against FY n-6..n-5): a final dividend paid in July one
    # year and March the next moves between financial years of the ex-date, which made single-year ends swing
    # (ONGC read +50% a year from one year to the next instead of the underlying trend).
    cagr = None
    ends = [by_year.get(y, 0.0) for y in (complete, complete - 1)]
    starts = [by_year.get(y, 0.0) for y in (complete - 5, complete - 6)]
    if all(ends) and all(starts) and (oldest is None or complete - 6 >= oldest):
        cagr = (sum(ends) / sum(starts)) ** (1 / 5) - 1
    f = fund or {}
    return {"by_year": {f"FY{y}": round(v, 4) for y, v in sorted(by_year.items())}, "years_paid_in_a_row": streak, "streak_capped": capped,
            "streak_note": gap_note, "streak_resolved_from_nse": bool(gap_note and "NSE's corporate actions show" in gap_note),
            "dps_cagr_5y": cagr, "cagr_years": f"FY{complete - 6}-{str(complete - 5)[2:]} to FY{complete - 1}-{str(complete)[2:]} averages",
            "payout_ratio": f.get("payout_ratio"), "fcf_cover": f.get("fcf_cover"),
            "basis": ("dividends per share by financial year of the ex-date (Yahoo, adjusted for splits and bonuses; includes specials); "
                      "payout = cash dividends paid in the last financial year ÷ that year's net profit; "
                      "FCF cover = that year's free cash flow ÷ cash dividends paid")}


def demerger_statements(fund, series, base, cut, demergers=None):
    """Keep annual comparisons honest around a demerger. Returns plain-language notes and mutates `fund`.

    - Parent: when the year compared with straddles the scheme's appointed/effective date, annual growth compares
      different businesses, so it is withheld (TMPV FY2026 vs FY2025).
    - Resulting company: until its first full financial year, its annual statements cover only part of a year of the
      business it received (TMCV FY2026 revenue +44% against its FY2025 shell), so annual growth, the P/E band and
      annual ROE are withheld; trailing-12-month ROE and margins are used when those quarters all follow the
      appointed date.
    The latest quarter against the same quarter a year earlier is kept when both quarters follow the appointed date.
    """
    notes = []
    rev = series.get("annualTotalRevenue") or series.get("annualDilutedEPS") or []
    if len(rev) < 1:
        return notes
    latest_end = date.fromisoformat(rev[-1]["date"])

    def withhold(reason):
        for k in ("revenue_growth", "eps_growth", "revenue_cagr_3y", "eps_change_text"):
            fund[k] = None
        fund["growth_withheld"] = reason
        fund["eps_flags"] = [x for x in fund.get("eps_flags") or [] if not x.startswith("Annual EPS")]

    def quarter_ok(start_date):
        ago = fund.get("quarter_year_ago")
        if ago and date.fromisoformat(ago) - timedelta(days=88) < start_date - timedelta(days=5):
            fund["quarter_profit_yoy"] = fund["quarter_revenue_yoy"] = fund["quarter_profit_yoy_text"] = None
            fund["quarter_yoy_withheld"] = f"the quarter a year earlier ({ago}) predates the {start_date.isoformat()} demerger"

    for e in parent_events(base, demergers):
        share = demerged_share(e)
        if share is not None and share < MINOR_SHARE:
            continue
        start = date.fromisoformat(e.get("appointed_date") or e.get("effective_date") or e["record_date"])
        who = e.get("resulting_company") or e.get("resulting_symbol")
        if len(rev) >= 2:
            base_start = date.fromisoformat(rev[-2]["date"]) - timedelta(days=364)
            if base_start < start <= latest_end:
                reason = (f"FY{latest_end.year} vs FY{latest_end.year - 1} straddles the demerger of {who} (from {start.isoformat()}), "
                          "so annual growth compares different businesses and is not shown; the latest quarter against a year earlier is used instead")
                withhold(reason)
                notes.append(reason[0].upper() + reason[1:] + ".")
        quarter_ok(start)
    for e in resulting_events(base, demergers):
        start = date.fromisoformat(e.get("appointed_date") or e.get("effective_date") or e["record_date"])
        full = e.get("resulting_first_full_fy_end")
        parent = e.get("parent")
        if full and latest_end < date.fromisoformat(full):
            reason = (f"{base} received its business in the demerger from {parent} (from {start.isoformat()}, record date {e['record_date']}); "
                      f"its FY{latest_end.year} statements cover only part of a year of it and earlier years are the pre-demerger company, "
                      f"so annual growth, annual ROE and the P/E history are not meaningful until the FY{date.fromisoformat(full).year} results")
            withhold(reason)
            fund["pe_band_withheld"] = True
            notes.append(reason[0].upper() + reason[1:] + ".")
            t = fund.get("ttm") or {}
            if t.get("date") and date.fromisoformat(t["date"]) - timedelta(days=364) >= start - timedelta(days=5):
                use_ttm(fund, series, reason="the annual figures cover only part of a year")
            else:
                fund["roe"] = fund["net_margin"] = fund["operating_margin"] = None
                fund["roe_basis"] = "not shown: fewer than four quarters since the demerger"
        quarter_ok(start)
    return notes


NSE_RESULTS = "https://www.nseindia.in/api/integrated-filing-results?index=equities&symbol={}"
OLDEST_QUARTER_DAYS = 650  # quarters read back from the latest: seven earlier ones, eight in all
REVENUE_TAGS = ("RevenueFromOperations", "TotalRevenueFromOperations", "Income", "TotalIncome")
# Banks file in their own format: profit attributable to shareholders is "after taxes, minority interest and share of
# associates" (Kotak FY2025: ₹22,126 cr against ₹21,946 cr for the period before its associates' share), and EPS is
# "after extraordinary items". Without these tags no bank filing gave an EPS, so HDFC Bank's NSE back-fill read nothing.
PROFIT_TAGS = ("ProfitOrLossAttributableToOwnersOfParent", "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
               "ProfitLossForPeriod", "ProfitLossForThePeriod")
EPS_TAGS = ("DilutedEarningsLossPerShare", "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "DilutedEarningsLossPerShareFromContinuingOperations", "DilutedEarningsPerShareAfterExtraordinaryItems",
            "DilutedEarningsPerShareBeforeExtraordinaryItems")
# Borrowings by balance-sheet format: Ind AS companies split them by maturity; NBFCs list debt securities, borrowings
# and subordinated liabilities; banks show one "Borrowings" line (deposits are not debt for any of them).
BORROWING_TAGS = (("BorrowingsNoncurrent", "BorrowingsCurrent"), ("DebtSecurities", "Borrowings", "SubordinatedLiabilities"))


def results_xbrl(body, span=(80, 100)):
    """Quarter revenue, profit (attributable to shareholders where given) and diluted EPS from an NSE results XBRL.

    `span` is the period length in days: a quarter by default; (350, 380) reads the full year that a March-quarter
    filing also reports."""
    if not isinstance(body, bytes) or len(body) > LIMIT or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise Block("STOCK: results XBRL refused")
    import xml.etree.ElementTree as ET
    root = ET.fromstring(body)
    tag = lambda e: e.tag.rsplit("}", 1)[-1]
    quarter, instants = {}, {}
    for ctx in root.iter():
        if tag(ctx) != "context" or any(tag(e) in ("explicitMember", "typedMember") for e in ctx.iter()):
            continue
        dates = {tag(e): (e.text or "").strip() for e in ctx.iter() if tag(e) in ("startDate", "endDate", "instant")}
        try:
            if "instant" in dates:
                instants[ctx.attrib["id"]] = date.fromisoformat(dates["instant"]).isoformat()
            elif len(dates) == 2 and span[0] <= (date.fromisoformat(dates["endDate"]) - date.fromisoformat(dates["startDate"])).days <= span[1]:
                quarter[ctx.attrib["id"]] = dates["endDate"]
        except (KeyError, ValueError):
            continue
    facts, held = {}, {}
    for e in root.iter():
        ref = e.attrib.get("contextRef")
        if (ref in quarter or ref in instants) and (e.text or "").strip():
            try:
                number = float(e.text.strip())
            except ValueError:
                continue
            if ref in quarter:
                facts.setdefault(tag(e), []).append((quarter[ref], number))
            else:
                held.setdefault(instants[ref], {}).setdefault(tag(e), number)
    pick = lambda names: next(((facts[n][0][0], facts[n][0][1]) for n in names if facts.get(n)), (None, None))
    end, revenue = pick(REVENUE_TAGS)
    pend, profit = pick(PROFIT_TAGS)
    # Integrated filings (2025 onwards) tag EPS by operations: continuing and discontinued together, else continuing.
    eps = next(([v for _, v in facts[n] if v] for n in EPS_TAGS if any(v for _, v in facts.get(n, []))), [])
    if not (end or pend):
        raise Block("STOCK: results XBRL has no quarter")
    first = lambda names: next((facts[n][0][1] for n in names if facts.get(n)), None)
    # Items that mark a one-off quarter: exceptional items, other income (negative after a fair-value loss) and
    # tax against pre-tax profit.
    # The balance sheet (half-yearly and annual filings only): borrowings and equity attributable to shareholders on
    # the period's last day.
    sheet = held.get(end or pend) or {}
    balance = balance_from(sheet, end or pend)
    total, pbt, tax = first(("ProfitLossForPeriod", "ProfitLossForThePeriod")), first(("ProfitBeforeTax", "ProfitLossBeforeTax")), first(("TaxExpense",))
    before = first(("ProfitBeforeExceptionalItemsAndTax", "ProfitLossBeforeExceptionalItemsAndTax"))
    share = share_after_tax(total, first(("ProfitLossForPeriodFromContinuingOperations",)),
                            first(("ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod",)),
                            first(("ProfitLossFromDiscontinuedOperationsAfterTax",)))
    if profit == 0 and total:
        # An attributable-profit line filled with 0 while the period made a profit or loss is an empty field, not nil
        # (Nestlé India, which has no minority interest, files 0 there): the total profit is the shareholders' profit.
        profit = total
    return {"quarter": end or pend, "revenue": revenue, "profit": profit, "eps": max(set(eps), key=eps.count) if eps else None,
            "profit_total": total, "pbt": pbt, "tax": tax,
            # As the company states it: with the share of joint ventures and associates even where the XBRL adds that
            # share after tax (TMPV, September 2026 quarter: ₹374 cr tagged + ₹29 cr share = ₹403 cr, its "PBT (bei)").
            "pbt_before_exceptional": before + share if before is not None and share else before,
            "pbt_before_exceptional_tagged": before, "associates_share_after_tax": share,
            "exceptional": first(("ExceptionalItemsBeforeTax", "ExceptionalItems")), "other_income": first(("OtherIncome",)),
            # After tax: a demerged or sold business, often with the gain on its distribution (Hindustan Unilever, December 2025
            # quarter: ₹4,485 cr from the Kwality Wall's ice-cream demerger, inside ₹6,607 cr of profit).
            "discontinued": first(("ProfitLossFromDiscontinuedOperationsAfterTax",)),
            "balance": balance}


def balance_from(sheet, day):
    """{'date', 'borrowings', 'equity', 'format'} from one filing's balance-sheet facts, or None without an equity figure.

    Equity is the shareholders' share (excluding minority interest): Ind AS 'EquityAttributableToOwnersOfParent'
    (else 'Equity'), or for banks capital plus reserves and surplus (HDFC Bank, 31 Mar 2026: ₹1,539 cr + ₹5,79,975 cr).
    Borrowings are None when the filing tags none of the usual lines."""
    owners = sheet.get("EquityAttributableToOwnersOfParent", sheet.get("Equity"))
    kind = "ind_as"
    if owners is None and "Capital" in sheet and "ReservesAndSurplus" in sheet:
        owners, kind = sheet["Capital"] + sheet["ReservesAndSurplus"], "bank"
    if owners is None:
        return None
    borrowings = None
    for tags in BORROWING_TAGS:
        found = [sheet[k] for k in tags if k in sheet]
        if found:
            borrowings = sum(found)
            if tags[0] == "DebtSecurities":
                kind = "nbfc" if kind != "bank" else kind
            break
    return {"date": day, "borrowings": borrowings, "equity": owners, "format": kind}


def share_after_tax(total, continuing, share, discontinued):
    """The share of profit of joint ventures and associates when the filing adds it after tax (profit for the period =
    continuing operations + that share + discontinued operations), else None (already inside pre-tax profit, or not
    reported). TMPV, September 2026 quarter: −₹5,480 cr continuing + ₹29 cr share = −₹5,451 cr for the period."""
    if not share or total is None or continuing is None:
        return None
    tolerance = max(1e7, 0.002 * abs(total))  # ₹1 cr, or 0.2% of the profit
    disc = discontinued or 0.0
    if abs(continuing + share + disc - total) <= tolerance and abs(continuing + disc - total) > tolerance:
        return share
    return None


def results_page(base, fetcher, page=None):
    """One page of NSE's integrated-filing list: ({quarter end: (rank, row)}, whether more pages exist). The list
    is paged 20 rows at a time (governance and financial filings mixed), so the March 2025 results of a company
    such as Infosys sit on page 2."""
    raw = json.loads(fetcher(NSE_RESULTS.format(urllib.parse.quote(base, safe="")) + (f"&page={page}" if page else "")))
    rows = raw.get("data") if isinstance(raw, dict) else raw
    more = isinstance(raw, dict) and isinstance(raw.get("totalCount"), int) and raw["totalCount"] > len(rows or []) * (page or 1)
    chosen = {}
    for r in rows or []:
        if not isinstance(r, dict) or "Financials" not in str(r.get("type")) or not str(r.get("xbrl") or "").endswith(".xml"):
            continue
        try:
            qe = datetime.strptime(str(r["qe_Date"]).title(), "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        rank = (r.get("consolidated") == "Consolidated", str(r.get("broadcast_Date") or ""))
        if qe not in chosen or rank > chosen[qe][0]:
            chosen[qe] = (rank, r)
    return chosen, more


def filing_list(chosen):
    return [{"quarter": q.isoformat(), "url": r["xbrl"], "basis": "consolidated" if r.get("consolidated") == "Consolidated" else "standalone"}
            for q, (_, r) in sorted(chosen.items(), reverse=True)]


def nse_quarter_results(base, fetcher, details=True):
    """Latest quarter and the same quarter a year earlier from NSE's integrated filings (consolidated when filed).

    Yahoo's quarterly series skip quarters (in Nov 2026 it had no Sep 2025 quarter for Reliance, TCS or HCLTech), so
    year-on-year comes from the exchange filings themselves, in rupees even for companies Yahoo shows in dollars.
    details=False reads only the list (enough for nse_annual_results), not the two quarter XBRL files.
    """
    try:
        chosen, more = results_page(base, fetcher)
        if not chosen and more:
            # Governance filings can fill the whole first page (seen: Coal India, Bosch and CG Power in Nov 2026).
            chosen, more = results_page(base, fetcher, page=2)
    except (Block, ValueError, TypeError, OSError):
        return None
    if not chosen:
        return None
    latest_q = max(chosen)
    ago = next((q for q in chosen if 360 <= (latest_q - q).days <= 370), None)
    out = {"source": "NSE integrated filings (financial results XBRL)", "symbol": base, "more_pages": more,
           "filings": filing_list(chosen)}
    if not details:
        r = chosen[latest_q][1]
        out["latest"] = {"quarter": latest_q.isoformat(), "basis": "consolidated" if r.get("consolidated") == "Consolidated" else "standalone",
                         "url": r["xbrl"]}
        return out
    # The three quarters before the latest complete the trailing twelve months, whose one-offs (exceptional items,
    # negative other income) move the trailing P/E: TCS's December 2025 quarter carried ₹3,391 cr of exceptional charges.
    between = [q for q in chosen if 80 <= (latest_q - q).days <= 290]
    # Older quarters on the same page (up to eight in all) give each quarter's usual other income, against which a
    # one-off gain stands out (Reliance, June 2025 quarter: ₹15,119 cr including the Asian Paints stake sale, against
    # about ₹4,900 cr in the other quarters).
    older = [q for q in chosen if 290 < (latest_q - q).days <= OLDEST_QUARTER_DAYS and q != ago]

    def load(q):
        r = chosen[q][1]
        return {**results_xbrl(fetcher(r["xbrl"])), "basis": "consolidated" if r.get("consolidated") == "Consolidated" else "standalone",
                "url": r["xbrl"]}
    try:
        for label, q in (("latest", latest_q), ("year_ago", ago)):
            if q is not None:
                out[label] = load(q)
    except (Block, ValueError, KeyError, TypeError, OSError):
        return None
    if "year_ago" in out and out["year_ago"]["basis"] != out["latest"]["basis"]:
        del out["year_ago"]  # never compare consolidated with standalone
    recent, extra = [out["latest"]], []
    if between or older:
        wanted = sorted(between, reverse=True) + sorted(older, reverse=True)
        with ThreadPoolExecutor(max_workers=3) as pool:
            loaded = list(pool.map(lambda q: _quietly(load, q), wanted))
        same = lambda x: x and x["basis"] == out["latest"]["basis"]
        recent += [x for x in loaded[:len(between)] if same(x)]
        extra = [x for x in loaded[len(between):] if same(x)]
    out["recent"] = recent
    # Every quarter read on the latest quarter's basis, newest first (at most eight).
    out["quarters"] = sorted(recent + ([out["year_ago"]] if "year_ago" in out else []) + extra, key=lambda x: x["quarter"], reverse=True)[:8]
    return out


def _quietly(load, item):
    try:
        return load(item)
    except (Block, ValueError, KeyError, TypeError, OSError, SyntaxError):
        return None


def nse_annual_results(nse, fetcher, years=2):
    """Full-year revenue and profit from the last `years` March-quarter filings ({'2026-03-31': {...}}), same basis as
    the latest quarter. A March-quarter integrated filing also reports the whole financial year."""
    if not nse or "latest" not in nse:
        return {}
    basis = nse["latest"]["basis"]
    out = {}
    filings = list(nse.get("filings") or [])
    marches = lambda: [f for f in filings if f["quarter"].endswith("-03-31") and f["basis"] == basis]
    more, page = nse.get("more_pages"), 2
    while len(marches()) < years and more and nse.get("symbol") and page <= 3:
        # The year before last is usually on the list's second page. A page keeps one filing per quarter, so the same
        # March can sit on page 1 as standalone and on page 2 as consolidated (Eternal, March 2025): match on both
        # quarter and basis, or the consolidated year before is dropped and growth goes missing.
        try:
            older, more = results_page(nse["symbol"], fetcher, page=page)
            known = {(f["quarter"], f["basis"]) for f in filings}
            filings += [f for f in filing_list(older) if (f["quarter"], f["basis"]) not in known]
        except (Block, ValueError, TypeError, OSError):
            break
        page += 1
    for f in filings:
        if len(out) >= years or not f["quarter"].endswith("-03-31") or f["basis"] != basis:
            continue
        try:
            year = results_xbrl(fetcher(f["url"]), span=(350, 380))
        except (Block, ValueError, KeyError, TypeError, OSError, SyntaxError):
            continue
        if year.get("quarter") == f["quarter"] and year.get("revenue"):
            out[f["quarter"]] = {**year, "basis": basis, "url": f["url"]}
    return out


# ---------- deeper P/E history: older annual EPS from NSE filings ----------

NSE_ANNUAL_LIST = "https://www.nseindia.in/api/corporates-financial-results?index=equities&symbol={}&period=Annual"
BACKFILL_YEARS = 10      # annual results filings to read at most
BACKFILL_BUDGET = 15.0   # seconds for the whole back-fill (filing list, filings, ten-year prices), run alongside the core data
BACKFILL_TOLERANCE = 0.15  # a filing's split-adjusted EPS this far from Yahoo's for the same year stops the back-fill
MAX_EPS_AGE = 430        # days an annual EPS stays in use after it is published; older (a missing year) leaves a gap
_FILED, _FILED_LOCK = {}, threading.Lock()  # parsed filings by URL: a published filing never changes, so runs share them


def annual_xbrl(body):
    """Full-year diluted EPS, profit and revenue from an NSE annual-results XBRL of FY2019-FY2024 (the 'INDAS_' files).

    Their context dates cannot be trusted (Infosys FY2024's 'FourD' context says January-March 2024 but carries the full
    year: revenue ₹1,53,670 cr, EPS ₹63.29) and FY2019-FY2022 files leave the main contexts out altogether, so the year
    is read from the cumulative 'FourD' column by name and checked against the quarter column 'OneD'."""
    if not isinstance(body, bytes) or len(body) > LIMIT or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise Block("STOCK: results XBRL refused")
    import xml.etree.ElementTree as ET
    root = ET.fromstring(body)
    cols = {"OneD": {}, "FourD": {}}
    for e in root.iter():
        ref = e.attrib.get("contextRef")
        if ref in cols and (e.text or "").strip():
            try:
                cols[ref].setdefault(e.tag.rsplit("}", 1)[-1], float(e.text.strip()))
            except ValueError:
                continue
    year, quarter = cols["FourD"], cols["OneD"]
    first = lambda facts, names: next((facts[n] for n in names if facts.get(n) is not None), None)
    eps, revenue, q_revenue = first(year, EPS_TAGS), first(year, REVENUE_TAGS), first(quarter, REVENUE_TAGS)
    if not eps:
        raise Block("STOCK: annual results XBRL has no EPS")
    # A discontinued-operations EPS filled with the continuing figure doubles the combined one (Maruti Suzuki FY2024:
    # ₹429.01 continuing, ₹429.01 "discontinued", ₹858.02 combined, with no discontinued profit): use the continuing EPS.
    cont = year.get("DilutedEarningsLossPerShareFromContinuingOperations")
    disc = year.get("DilutedEarningsLossPerShareFromDiscontinuedOperations")
    if cont and disc == cont and abs(eps - 2 * cont) <= 0.01 * abs(eps) and not year.get("ProfitLossFromDiscontinuedOperationsAfterTax"):
        eps = cont
    if revenue and q_revenue and revenue < 2 * q_revenue:
        raise Block("STOCK: annual results XBRL year column is not a full year")
    return {"eps": eps, "revenue": revenue, "profit": first(year, PROFIT_TAGS)}


def _nse_day(text):
    return datetime.strptime(str(text).strip()[:11].title(), "%d-%b-%Y").date()


def annual_filings(base, fetcher):
    """NSE's annual results filings with XBRL (about FY2019 onwards; earlier years are PDFs/HTML): one per financial year,
    consolidated unless fewer than two consolidated years were filed, newest first, at most BACKFILL_YEARS.
    Each: {'date': year end, 'filed': broadcast date, 'url', 'basis'}."""
    rows = json.loads(fetcher(NSE_ANNUAL_LIST.format(urllib.parse.quote(base, safe=""))))
    found = {"Consolidated": {}, "Non-Consolidated": {}}
    for r in rows if isinstance(rows, list) else []:
        url = str((r or {}).get("xbrl") or "") if isinstance(r, dict) else ""
        if r.get("consolidated") not in found or not url.endswith(".xml") or r.get("period") not in (None, "Annual"):
            continue
        try:
            end, filed = _nse_day(r["toDate"]), _nse_day(r.get("broadCastDate") or r.get("filingDate"))
        except (KeyError, ValueError, TypeError):
            continue
        if (end.month, end.day) != (3, 31):
            continue  # Indian financial years only
        keep = found[r["consolidated"]]
        if end not in keep or filed.isoformat() > keep[end]["filed"]:  # a revised filing replaces the first
            keep[end] = {"date": end.isoformat(), "filed": filed.isoformat(), "url": url,
                         "basis": "consolidated" if r["consolidated"] == "Consolidated" else "standalone"}
    # Consolidated whenever there are a couple of years of it: Reliance filed FY2022 standalone only, and standalone EPS
    # (₹62 for FY2024) is a different business from the consolidated figure Yahoo and the price reflect (₹103).
    consolidated, standalone = found["Consolidated"], found["Non-Consolidated"]
    pick = consolidated if len(consolidated) >= min(2, len(standalone)) and consolidated else standalone
    return [pick[d] for d in sorted(pick, reverse=True)][:BACKFILL_YEARS]


CACHE_ENV = "INDIA_DAY_TRADER_CACHE"  # the same per-user cache folder the daily commands use; "off" disables it
LIST_CACHE_DAYS = 120                 # a saved annual-results list stands in for NSE's when the exchange does not answer
FILING_PARSE_VERSION = 2              # bump when annual_xbrl() changes, so filings parsed by an older version are read again


def filing_store():
    """Per-user folder (outside the plugin) for parsed NSE annual filings, used by live runs only: $INDIA_DAY_TRADER_CACHE
    ('off' disables), else %LOCALAPPDATA%/india-day-trader/cache on Windows, else $XDG_CACHE_HOME or ~/.cache. A
    published filing never changes, so a back-fill that once read it never waits on NSE for it again."""
    env = os.environ.get(CACHE_ENV, "").strip()
    if env:
        return None if env.lower() in ("off", "0", "none", "false") else Path(env) / "nse_annual_filings"
    try:
        base = (os.environ.get("LOCALAPPDATA") if os.name == "nt" else None) or os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    except (OSError, RuntimeError):
        return None
    return Path(base) / "india-day-trader" / "cache" / "nse_annual_filings"


def _stored(store, key):
    return Path(store) / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:24] + ".json") if store else None


def _save(path, doc):
    """Write JSON atomically; a cache that cannot be written is skipped silently."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _read_filing(fetcher, filing, store=None):
    """A parsed annual filing: from this run's memory, else the on-disk store (live runs), else NSE."""
    with _FILED_LOCK:
        if filing["url"] in _FILED:
            return _FILED[filing["url"]]
    path = _stored(store, f"annual-xbrl-v{FILING_PARSE_VERSION}:" + filing["url"])
    if path:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict) and parsed.get("eps") is not None:
                with _FILED_LOCK:
                    _FILED[filing["url"]] = parsed
                return parsed
        except (OSError, ValueError):
            pass
    parsed = annual_xbrl(fetcher(filing["url"]))
    with _FILED_LOCK:
        _FILED[filing["url"]] = parsed
    if path and parsed.get("eps") is not None:
        _save(path, parsed)
    return parsed


def stored_annual_filings(base, fetcher, store=None, today=None):
    """annual_filings() saved to the store on success; when NSE does not answer, the saved list if it is under
    LIST_CACHE_DAYS old (older years never change; only the newest year can be missing from it). Raises like
    annual_filings() when there is neither."""
    path = _stored(store, "annual-list:" + base.upper())
    try:
        rows = annual_filings(base, fetcher)
    except (Block, ValueError, TypeError, KeyError, OSError):
        if path:
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                age = ((today or date.today()) - date.fromisoformat(doc["saved"])).days
                if 0 <= age <= LIST_CACHE_DAYS and isinstance(doc.get("filings"), list):
                    return doc["filings"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        raise
    if path and rows:
        _save(path, {"saved": (today or date.today()).isoformat(), "filings": rows})
    return rows


def split_events(result):
    """[(ex-date ISO, shares after ÷ shares before)] from a Yahoo chart's split events (bonuses appear as splits)."""
    out = []
    for s in ((result.get("corporate_actions") or {}).get("splits") or {}).values():
        try:
            ratio = float(s["numerator"]) / float(s["denominator"])
            day = datetime.fromtimestamp(int(s["date"]), IST).date().isoformat()
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if ratio > 0 and ratio != 1:
            out.append((day, ratio))
    return sorted(out)


BONUSES_PATH = ASSETS / "bonuses.json"


@functools.lru_cache(maxsize=4)
def listed_bonuses(path=BONUSES_PATH):
    """{symbol: [(ex-date ISO, shares after ÷ shares before)]} from the curated bonus list (a same-day split included);
    {} when the file is missing or unreadable, since Yahoo's split events are the main source."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for e in doc.get("bonuses") or [] if isinstance(doc, dict) else []:
        try:
            r = e["ratio"]
            factor = (r["new_shares"] + r["per_held_shares"]) / r["per_held_shares"]
            s = e.get("split_before_bonus")
            if s:
                factor *= s["new_shares"] / s["per_old_shares"]
            day = e.get("ex_date") or e["record_date"]
            date.fromisoformat(day)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if factor > 0 and factor != 1:
            out.setdefault(str(e.get("symbol", "")).upper(), []).append((day, factor))
    return out


def share_changes(selected, history):
    """[(ex-date ISO, shares after ÷ shares before)] for splits and bonuses: Yahoo's chart events, plus any bonus in
    assets/bonuses.json that Yahoo did not record (none within a week of a Yahoo event)."""
    try:
        events = split_events(history)
    except (AttributeError, TypeError):
        events = []
    return with_listed_bonuses(selected.rsplit(".", 1)[0].upper(), events)


def with_listed_bonuses(base, events):
    """Yahoo's split events completed from assets/bonuses.json: a listed bonus with no Yahoo event within a week is added,
    and a Yahoo event within a week whose ratio differs takes the listed one. Yahoo records a same-day split and bonus as
    one event and can keep only the split: Bajaj Finance's 1:2 split with a 4:1 bonus on 16 Jun 2025 (×10 in all) shows
    as a 2-for-1 split, while its annual EPS is restated ×10, so the NSE back-fill's FY2023-FY2024 EPS failed to match."""
    events = list(events or [])
    for day, ratio in listed_bonuses().get(base, []):
        near = [i for i, (d, _) in enumerate(events) if abs((date.fromisoformat(day) - date.fromisoformat(d)).days) <= 7]
        if not near:
            events.append((day, ratio))
        elif all(abs(events[i][1] - ratio) > 1e-6 for i in near):
            events = [e for i, e in enumerate(events) if i not in near] + [(events[near[0]][0], ratio)]
    return sorted(events)


def _split_pairs(splits):
    """Splits as (ex-date ISO, ratio or None); accepts bare ISO dates (ratio unknown) or (date, ratio) pairs."""
    out = []
    for s in splits or []:
        if isinstance(s, str):
            out.append((s, None))
        elif isinstance(s, (tuple, list)) and len(s) == 2 and isinstance(s[0], str):
            out.append((s[0], float(s[1]) if isinstance(s[1], (int, float)) and s[1] > 0 else None))
    return sorted(out)


def adjust_quarter_eps(quarterly, net_income, shares, splits):
    """Quarterly EPS on today's share basis. A quarter that ended before a split or bonus is divided by the share ratio
    only when its own figures show it was not restated: EPS × today's shares ≈ ratio × that quarter's net profit
    (Yahoo restates most quarters but not always; TRENT's Sep 2025 quarter stayed at ₹10.60 after the 3-for-2 bonus of
    June 2026, against ₹7.07 on the new basis). Returns (rows, dates adjusted)."""
    pairs = [(d, r) for d, r in _split_pairs(splits) if r]
    profit = {r["date"]: r["value"] for r in net_income or []}
    if not pairs or not shares or shares <= 0:
        return list(quarterly or []), []
    rows, fixed = [], []
    for row in quarterly or []:
        factor = math.prod(r for d, r in pairs if d > row["date"])
        ni = profit.get(row["date"])
        if factor != 1 and ni and row["value"]:
            now_basis = ni / shares
            if (abs(row["value"] / factor - now_basis) <= 0.12 * abs(now_basis)
                    and abs(row["value"] - now_basis) > 0.12 * abs(now_basis)):
                rows.append({**row, "value": row["value"] / factor, "split_adjusted": factor})
                fixed.append(row["date"])
                continue
        rows.append(row)
    return rows, fixed


RESTATE_TOLERANCE = 0.15  # an annual EPS within this of profit ÷ that year's (restated) shares only after dividing by the split ratio


def restate_annual_eps(eps_rows, net_income, share_rows, shares, splits):
    """Annual EPS on today's share basis. Yahoo restates most years for a later split or bonus but not always the
    oldest: HDFC Bank's FY2023 EPS stayed at ₹88.68 after the 1:1 bonus of August 2025 while FY2024-FY2026 were halved
    (₹44.16-45.75), which put the P/E history's low at 7.8 and its 25th percentile at 9.4 against a median of 18.3.
    A year is divided by the ratio of the later splits only when its own figures show it was not restated: net profit
    ÷ that year-end's share count (which Yahoo does restate; today's count when missing) matches EPS ÷ ratio and not
    EPS itself. Returns (rows, [ISO dates adjusted])."""
    pairs = [(d, r) for d, r in _split_pairs(splits) if r]
    if not pairs or not eps_rows:
        return list(eps_rows or []), []
    profit = {r["date"]: r["value"] for r in net_income or []}
    counts = {r["date"]: r["value"] for r in share_rows or [] if r["value"] > 0}
    rows, fixed = [], []
    for row in eps_rows:
        factor = math.prod(r for d, r in pairs if d > row["date"])
        ni, count = profit.get(row["date"]), counts.get(row["date"]) or shares
        if factor != 1 and ni and count and row["value"] and ni > 0:
            implied = ni / count
            if (abs(row["value"] / factor - implied) <= RESTATE_TOLERANCE * abs(implied)
                    and abs(row["value"] - implied) > RESTATE_TOLERANCE * abs(implied)):
                rows.append({**row, "value": row["value"] / factor, "split_adjusted": factor})
                fixed.append(row["date"])
                continue
        rows.append(row)
    return rows, fixed


EPS_PROFIT_BREAK = (0.55, 1.8)  # annual EPS moving this differently from net profit across a year: a share-basis break
BAND_BREAK = 0.55               # a P/E history whose 25th percentile is under this share of its median has a break in it
                                # (HDFC Bank's unadjusted band of Nov 2026: 9.4 against 18.3, 0.51)
LOW_MARGIN = 0.02               # net margin below this, or ...
HIGH_PE = 150                   # ... a P/E above this: earnings too thin for a P/E history to say anything


def band_problem(band, eps_rows, net_income, fund=None):
    """Why a P/E history should not be shown (None when it is fine): earnings too thin to mean anything (Eternal,
    November 2026: P/E 704 on a 0.7% net margin), a year whose EPS moves very differently from its profit (a split or
    bonus Yahoo left unadjusted), or a 25th percentile under half the median (the same break, seen from the band)."""
    if not band:
        return None
    f = fund or {}
    margin = f.get("net_margin")
    pe = f.get("pe") or band.get("current")
    if (margin is not None and margin < LOW_MARGIN) or (pe and pe > HIGH_PE):
        bits = ([f"net margin {pct(margin, sign=False)}"] if margin is not None and margin < LOW_MARGIN else []) + (
            [f"P/E {num(pe, 0)}"] if pe and pe > HIGH_PE else [])
        return (f"earnings too thin for a P/E history ({', '.join(bits)}): small changes in profit swing the P/E by hundreds; judge the "
                "stock on revenue, margins and the path to profit instead")
    profit = {r["date"]: r["value"] for r in net_income or []}
    used = [r for r in eps_rows or [] if r.get("value") and r["date"] in profit and profit[r["date"]]]
    for a, b in zip(used, used[1:]):
        if min(a["value"], b["value"], profit[a["date"]], profit[b["date"]]) <= 0:
            continue
        moved = (b["value"] / a["value"]) / (profit[b["date"]] / profit[a["date"]])
        if not EPS_PROFIT_BREAK[0] <= moved <= EPS_PROFIT_BREAK[1]:
            return (f"a break in the EPS history (annual EPS ×{b['value'] / a['value']:.2f} from FY{a['date'][:4]} to FY{b['date'][:4]} "
                    f"while net profit moved ×{profit[b['date']] / profit[a['date']]:.2f}): one year is probably on a different share basis "
                    "(an unadjusted split or bonus), so a percentile across it would be wrong")
    if band.get("median") and band.get("p25") is not None and band["p25"] < BAND_BREAK * band["median"]:
        return (f"a break in the P/E history (25th percentile {num(band['p25'], 1)} against a median of {num(band['median'], 1)}): a split or "
                "bonus left unadjusted in one year's EPS, or earnings that swung with one-offs, so a percentile across it would mislead")
    return None


def ratio_words(ratio):
    """'3-for-2 (a 1:2 bonus or split)' style wording for a share multiplier."""
    for den in range(1, 11):
        num_ = ratio * den
        if abs(num_ - round(num_)) < 1e-6:
            return f"{round(num_)}-for-{den}"
    return f"{ratio:g}×"


EPS_NET_TOLERANCE = 0.10  # trailing EPS this far from trailing net profit ÷ today's shares is checked


def cross_check_net_income(eps, ttm_net, ttm_date, shares, quarters, splits, price):
    """Independent check of the trailing EPS: trailing net profit ÷ today's share count (market cap ÷ price).

    Yahoo's trailing EPS can mix share bases across a split or bonus inside the trailing year (TRENT, November 2026:
    ₹27.99 against ₹1,895.9 cr ÷ 53.3 cr shares = ₹35.56 after the 1:2 bonus of June 2026, so P/E 114 instead of 90).
    The net-profit figure replaces it when a split or bonus falls inside the trailing year, or when the last four
    quarters (split-adjusted) agree with it and not with Yahoo's figure. Otherwise the gap usually means profit from
    discontinued operations, a one-off below the EPS line, or new shares issued during the year, so the EPS is kept and
    the gap is flagged. Returns (eps, flags)."""
    if (not eps or not eps["basis"].startswith(("trailing 12 months", "sum of last four quarters")) or ttm_net is None
            or ttm_date != eps["date"] or not shares or shares <= 0 or not eps["value"]):
        return eps, []
    implied = ttm_net / shares
    if implied == 0 or (implied > 0) != (eps["value"] > 0):
        return eps, []
    gap = abs(eps["value"] - implied) / max(abs(eps["value"]), abs(implied))
    if gap <= EPS_NET_TOLERANCE:
        return eps, []
    start = (date.fromisoformat(eps["date"]) - timedelta(days=365)).isoformat()
    inside = [(d, r) for d, r in _split_pairs(splits) if d > start]
    four = last_four_quarters(quarters or [])
    q_sum = sum(r["value"] for r in four) if four and four[-1]["date"] == eps["date"] else None
    agrees = q_sum is not None and abs(q_sum - implied) <= EPS_NET_TOLERANCE * abs(implied)
    shown = f"{crore_words(ttm_net)} ÷ {shares / 1e7:,.2f} cr shares = {eps_words(implied)}"
    pe_now = f"P/E {price / eps['value']:,.1f}" if eps["value"] > 0 else None
    pe_new = f"P/E {price / implied:,.1f}" if implied > 0 else None
    if inside or agrees:
        why = (f"a {', '.join(f'{ratio_words(r)} split/bonus on {day_month_year(d)}' if r else f'split/bonus on {day_month_year(d)}' for d, r in inside)} "
               "falls inside the trailing year, so the reported figure likely mixes pre- and post-issue share counts"
               if inside else f"the last four quarters sum to {eps_words(q_sum)}, so the reported figure looks wrong")
        flag = (f"Trailing EPS {eps_words(eps['value'])} does not match trailing net profit ÷ today's share count ({shown}); {why}. "
                "The net-profit figure is used" + (f" ({pe_new} instead of {pe_now.split(' ', 1)[1]})" if pe_now and pe_new else "") + ".")
        return {**eps, "value": implied, "basis": "trailing 12 months, net profit ÷ current shares", "replaced": eps["value"]}, [flag]
    flag = (f"Trailing net profit ÷ today's share count ({shown}) is {gap * 100:.0f}% away from the trailing EPS of {eps_words(eps['value'])}"
            + (f"; on it the {pe_new}" if pe_new else "") + ". The gap usually means discontinued operations, a one-off below the EPS line or "
            "new shares issued during the year; the reported EPS is kept. Check the results filing.")
    return eps, [flag]


def long_prices(selected, bars, *, fetcher, now):
    """Ten years of daily closes for the P/E history: Yahoo's 10-year chart before the analysed history, scaled at the join
    so a demerger adjustment made to the analysed history carries back. Returns (bars, splits)."""
    query = urllib.parse.urlencode({"range": "10y", "interval": "1d", "includePrePost": "false", "events": "div,splits"})
    result = chart(fetcher("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(selected, safe="") + "?" + query),
                   selected, "1d", now)
    join = bars[0]["at"][:10]
    match = next((b for b in result["bars"] if b["at"][:10] == join), None)
    if not match or not match["close"]:
        return bars, split_events(result)
    k = bars[0]["close"] / match["close"]
    older = [{**b, "close": b["close"] * k} for b in result["bars"] if b["at"][:10] < join]
    return older + list(bars), split_events(result)


def backfill_fetchers(*, live, nse_fetcher, fetcher, deadline):
    """(NSE, public) fetchers for the back-fill that refuse to start after `deadline`; live ones also time out by it."""
    if deadline is None:
        return nse_fetcher, fetcher

    def guard(fetch, timed):
        def get(url):
            left = deadline - time.monotonic()
            if left <= 0.05:
                raise Block("STOCK: time limit for the P/E history reached")
            return fetch(url, timeout=max(1.0, min(CORE_TIMEOUT, left))) if timed else fetch(url)
        return get
    if live:
        from shareholding import nse_request
        return (guard(lambda url, timeout: nse_request(url, timeout=timeout, attempts=2), True),
                guard(lambda url, timeout: request(url, timeout=timeout), True))
    return guard(nse_fetcher, False), guard(fetcher, False)


def load_backfill(base, selected, bars, *, nse_fetcher, fetcher, now, deadline=None, store=None):
    """Older annual EPS from NSE's annual results filings plus ten years of prices, fetched in parallel within
    `deadline` (time.monotonic()). `store` (live runs) keeps parsed filings on disk, so a later run does not wait on NSE
    for them. Returns {'filings': [...], 'bars', 'splits'} or {'error': reason}."""
    def left():
        return None if deadline is None else deadline - time.monotonic()
    try:
        filings = stored_annual_filings(base, nse_fetcher, store, now.date() if hasattr(now, "date") else None)
    except (Block, ValueError, TypeError, KeyError, OSError):
        return {"error": "NSE's annual results list was unavailable"}
    if not filings:
        return {"error": "NSE lists no annual results filings with data for it"}
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        jobs = {pool.submit(_read_filing, nse_fetcher, f, store): f for f in filings}
        prices = pool.submit(long_prices, selected, bars, fetcher=fetcher, now=now)
        read, timed_out = [], False
        for job, f in jobs.items():
            try:
                wait = left()
                read.append({**f, **job.result(timeout=None if wait is None else max(0.0, wait))})
            except FuturesTimeout:
                timed_out = True
                break
            except (Block, ValueError, TypeError, KeyError, OSError, SyntaxError):
                continue
        if not read:
            # Say which: a slow exchange (worth retrying) or filings with no usable EPS (retrying will not help).
            return {"error": "NSE's annual results filings could not be read in time" if timed_out
                    else "NSE's annual results filings gave no usable EPS"}
        try:
            wait = left()
            long_bars, splits = prices.result(timeout=None if wait is None else max(0.0, wait))
        except FuturesTimeout:
            return {"error": "the ten-year price history timed out"}
        except (Block, ValueError, TypeError, KeyError, IndexError, OSError):
            return {"error": "the ten-year price history was unavailable"}
    finally:
        pool.shutdown(wait=False)
    return {"filings": sorted(read, key=lambda f: f["date"]), "bars": long_bars, "splits": splits, "base": base.upper()}


def merge_annual_eps(yahoo_rows, filings, splits):
    """Yahoo's annual EPS with older years from NSE filings put on today's share basis: a filing's EPS is divided by every
    split or bonus whose ex-date came after the filing (Reliance's 1:1 bonus of October 2024 halves FY2019-FY2024).
    Years Yahoo also has must agree within BACKFILL_TOLERANCE, which catches an unrecorded split or a different basis.
    Returns (rows, note) or (None, reason)."""
    if not yahoo_rows:
        return None, "Yahoo has no annual EPS to check the filings against"
    adjusted = {}
    for f in filings:
        factor = math.prod(ratio for day, ratio in splits if day > f["filed"])
        adjusted[f["date"]] = f["eps"] / factor
    yahoo = {r["date"]: r["value"] for r in yahoo_rows}
    overlap = sorted(d for d in adjusted if d in yahoo)
    if not overlap:
        return None, "no year in both the NSE filings and Yahoo to check them against"
    bad = [d for d in overlap if abs(adjusted[d] - yahoo[d]) > BACKFILL_TOLERANCE * max(abs(yahoo[d]), 0.5)]
    if bad:
        return None, ("NSE's annual EPS does not match Yahoo's for " + ", ".join(f"FY{d[:4]}" for d in bad)
                      + " after split and bonus adjustment")
    older = {d: v for d, v in adjusted.items() if d < min(yahoo)}
    if not older:
        return None, "the NSE filings add no year before Yahoo's"
    rows = [{"date": d, "value": v, "source": "NSE annual results"} for d, v in sorted(older.items())] + list(yahoo_rows)
    used = [f for f in filings if f["date"] in older]
    have = {int(r["date"][:4]) for r in rows}
    missing = [y for y in range(min(have), max(have)) if y not in have]
    note = (f"annual EPS for FY{min(older)[:4]}-FY{max(older)[:4]} from NSE {used[0]['basis']} annual results filings"
            + (", adjusted for later splits and bonuses" if any(day > f["filed"] for f in used for day, _ in splits) else "")
            + f" (checked against Yahoo for {', '.join('FY' + d[:4] for d in overlap)}); later years from Yahoo"
            + (f"; no {used[0]['basis']} figure for {', '.join(f'FY{y}' for y in missing)}, so the sessions it would cover are left out"
               if missing else ""))
    return rows, note


def deep_pe_band(fund, backfill, yahoo_rows, since=None):
    """Five-year P/E band and ten-year (or longest available) median from Yahoo's annual EPS plus NSE back-fill. Replaces
    fund['pe_band'] when the back-fill reaches further back; otherwise records why in fund['pe_history_note']."""
    from screener import pe_band
    if not backfill or backfill.get("error"):
        fund["pe_history_note"] = (backfill or {}).get("error") or "the NSE back-fill did not run"
        return None
    rows, note = merge_annual_eps(yahoo_rows, backfill["filings"], with_listed_bonuses(backfill.get("base", ""), backfill["splits"]))
    if not rows:
        fund["pe_history_note"] = note
        return None
    five = pe_band(backfill["bars"], rows, since=since, years=5, max_age_days=MAX_EPS_AGE)
    ten = pe_band(backfill["bars"], rows, since=since, years=10, max_age_days=MAX_EPS_AGE)
    old = fund.get("pe_band")
    if not five or (old and five["start"] >= old["start"]):
        fund["pe_history_note"] = "the NSE filings did not extend the P/E history"
        return None
    end = date.fromisoformat(backfill["bars"][-1]["at"][:10])
    span = lambda band: round((end - date.fromisoformat(band["start"])).days / 365.25, 1)
    five["basis"] += "; " + note
    five["calendar_years"] = span(five)
    five["median_5y"] = five["median"] if five["calendar_years"] >= 4.5 else None
    if ten and ten["start"] < five["start"]:
        five["long"] = {"median": ten["median"], "start": ten["start"], "years": span(ten), "sessions": ten["sessions"],
                        "p25": ten["p25"], "p75": ten["p75"]}
    fund["pe_band"] = five
    fund["pe_history_note"] = None
    fund["_band_inputs"] = (backfill["bars"], rows, {"since": since, "years": 5, "max_age_days": MAX_EPS_AGE})
    return five


REVENUE_MISMATCH = 0.10  # Yahoo's quarter revenue this far from the exchange filing's means a different revenue definition
REVENUE_CONFIRM = 0.02   # Yahoo's latest annual revenue this close to the filing's: a big jump in it is real growth


def check_revenue(fund, series, nse=None, annual=None):
    """Catch Yahoo revenue that does not match the company's filings (finance companies: Yahoo often reports revenue
    net of interest cost or leaves out part of income; seen JIOFIN FY2026 +136% and a 49% net margin against
    about +69% and 44% in its filings). Triggers when annual revenue more than doubled or Yahoo's latest-quarter
    revenue is more than 10% away from the NSE filing's. Then revenue growth and net margin come from NSE's annual
    results when two years load (`annual()` returns them), else they are withheld. Mutates `fund`; returns the note."""
    rows = series.get("annualTotalRevenue") or []
    reasons = []
    growth = fund.get("revenue_growth")
    if growth is not None and growth > 1.0:
        reasons.append(f"revenue {growth * 100:+.0f}% in {fund.get('growth_period') or 'the last year'}")
    q = {r["date"]: r["value"] for r in series.get("quarterlyTotalRevenue") or []}
    latest = (nse or {}).get("latest") or {}
    if latest.get("revenue") and q.get(latest.get("quarter")) and not series.get("_currency"):
        gap = q[latest["quarter"]] / latest["revenue"] - 1
        if abs(gap) > REVENUE_MISMATCH:
            reasons.append(f"its {latest['quarter']} quarter revenue {crore_words(q[latest['quarter']])} against {crore_words(latest['revenue'])} "
                           f"in the NSE filing")
    if not reasons or not rows:
        return None
    years = annual() if callable(annual) else (annual or {})
    newest, before = rows[-1]["date"], (rows[-2]["date"] if len(rows) >= 2 else None)
    a, b = (years or {}).get(newest), (years or {}).get(before) if before else None
    fund["revenue_check"] = {"reasons": reasons, "nse_annual": years or {}}
    only_growth = len(reasons) == 1 and growth is not None and growth > 1.0
    if only_growth and a and a.get("revenue") and abs(rows[-1]["value"] / a["revenue"] - 1) <= REVENUE_CONFIRM:
        # The filings agree with Yahoo's latest year, so the jump is real growth (Eternal FY2026: ₹20,243 cr to ₹54,364 cr,
        # +169%, as quick commerce moved to owning inventory), not a definition mismatch: shown, not withheld.
        fund["revenue_check"]["confirmed"] = True
        if b and b.get("revenue"):
            fund["revenue_growth"] = change(a["revenue"], b["revenue"])
            fund["revenue_basis"] = "NSE annual results (March-quarter filings)"
            note = (f"Revenue {pct(fund['revenue_growth'])} in FY{newest[:4]} vs FY{before[:4]} is confirmed by NSE's {a['basis']} annual results "
                    f"({crore_words(b['revenue'])} to {crore_words(a['revenue'])}): real growth, not a data error.")
        else:
            note = (f"Revenue {pct(growth)} in {fund.get('growth_period') or 'the last year'}: Yahoo's latest year matches NSE's {a['basis']} annual "
                    f"results ({crore_words(a['revenue'])}); NSE's filing for the year before could not be read to confirm the base.")
        fund["revenue_note"] = note
        fund["revenue_confirmed_note"] = note
        return note
    if a:
        fund["revenue"] = a["revenue"]
        fund["revenue_growth"] = change(a["revenue"], b["revenue"]) if b else None
        fund["revenue_cagr_3y"] = None
        fund["operating_margin"] = fund["ebitda_margin"] = None
        fund["net_margin"] = a["profit"] / a["revenue"] if a.get("profit") is not None else None
        fund["margin_basis"] = f"FY{newest[:4]}, NSE {a['basis']} results"
        fund["revenue_basis"] = "NSE annual results (March-quarter filings)"
        note = (f"Yahoo's revenue does not match the company's filings ({'; '.join(reasons)}), so "
                + ("revenue growth and net margin use" if b else "net margin uses") + f" NSE's {a['basis']} annual results: FY{newest[:4]} revenue {crore_words(a['revenue'])}"
                + (f" (net margin {a['profit'] / a['revenue'] * 100:.1f}%)" if a.get("profit") is not None else "")
                + (f" against {crore_words(b['revenue'])} in FY{before[:4]}" if b else "")
                + ("." if b else "; NSE's filing for the year before could not be read, so growth is not shown."))
    else:
        for k in ("revenue_growth", "revenue_cagr_3y", "net_margin", "operating_margin", "ebitda_margin"):
            fund[k] = None
        fund["revenue_withheld"] = True
        note = (f"Yahoo's revenue looks inconsistent ({'; '.join(reasons)}); revenue growth and margins are not shown. "
                "Check the company's own results.")
    fund.setdefault("eps_flags", []).append(note)
    fund["revenue_note"] = note
    return note


def flag_unusual_base(fund):
    """A year-ago quarter several times the size of the others (a one-off gain) makes the year-on-year change mostly about it."""
    pair = fund.get("quarter_profit_values")
    vals = [abs(v) for v in fund.get("quarter_profits") or [] if v is not None]
    if not pair or pair[1] is None:
        return
    others = [v for v in vals if v != abs(pair[1])]
    if len(others) >= 3 and abs(pair[1]) > 3 * statistics.median(others):
        fund["quarter_note"] = (f"The year-ago quarter ({fund.get('quarter_year_ago')}) had an unusually large profit ({crore_words(pair[1])}, more than three "
                                "times a typical recent quarter), probably a one-off, so the year-on-year change mostly reflects that.")


def flag_distorted_base(fund, nse):
    """A year-ago quarter of the opposite sign that one-offs pushed the same way (exceptional items, negative other
    income, tax out of line with pre-tax profit) makes the profit/loss swing look bigger than the business change.
    Seen: TMCV's September 2025 quarter lost ₹867 cr after ₹306 cr of tax on a ₹549 cr pre-tax loss; Screener.in
    shows other income of −₹1,865 cr (a fair-value loss) for it. Sets fund['quarter_note']; returns it."""
    if fund.get("quarter_note") or fund.get("quarter_yoy_withheld") or not nse:
        return None
    pair, ago = fund.get("quarter_profit_values"), nse.get("year_ago") or {}
    if not pair or None in pair or 0 in pair or (pair[0] > 0) == (pair[1] > 0) or ago.get("quarter") != fund.get("quarter_year_ago"):
        return None
    turnaround = pair[0] > 0  # a loss a year ago, a profit now
    size = abs(ago.get("profit") or pair[1])
    exc, other, pbt, tax = (ago.get(k) for k in ("exceptional", "other_income", "pbt", "tax"))
    reasons = []
    if exc and (exc < 0) == turnaround and abs(exc) >= 0.25 * size:
        reasons.append(f"exceptional {'charges' if exc < 0 else 'gains'} of {crore_words(abs(exc))}")
    if turnaround and other is not None and other < 0 and abs(other) >= 0.1 * size:
        reasons.append(f"negative other income of {crore_words(abs(other))}")
    if pbt and tax is not None:
        rate, excess = tax / pbt, tax - 0.25 * pbt  # excess > 0: more tax than a normal ~25% rate
        if (rate < 0 or rate > 0.45) and (excess > 0) == turnaround and abs(excess) >= 0.2 * size:
            reasons.append((f"tax of {crore_words(tax)}" if tax >= 0 else f"a tax credit of {crore_words(-tax)}")
                           + f" on a pre-tax {'loss' if pbt < 0 else 'profit'} of {crore_words(abs(pbt))}"
                           + (f", a {rate * 100:.0f}% rate" if pbt > 0 else ""))
    if not reasons:
        return None
    fund["quarter_note"] = (f"The year-ago quarter ({ago['quarter']}) was distorted by one-offs ({'; '.join(reasons)}); the swing overstates the "
                            + ("turnaround." if turnaround else "decline."))
    return fund["quarter_note"]


def rupee_growth(fund, series, annual):
    """Companies Yahoo shows in US dollars (Infosys): annual revenue and EPS growth from their rupee results filed on
    NSE, not dollar figures converted at average rates (Infosys FY2026 revenue ₹178,650 cr against ₹162,990 cr,
    +9.6%, where the conversion gave +9.3%). `annual` returns nse_annual_results(); mutates `fund`; returns the note."""
    if not (series.get("_currency") or {}).get("reported") or fund.get("revenue_check") or fund.get("growth_withheld"):
        return None
    rows = series.get("annualTotalRevenue") or []
    if len(rows) < 2:
        return None
    years = annual() if callable(annual) else (annual or {})
    newest, before = rows[-1]["date"], rows[-2]["date"]
    a, b = (years or {}).get(newest), (years or {}).get(before)
    if not a or not b or not a.get("revenue") or not b.get("revenue"):
        return None
    fund["revenue_growth"] = change(a["revenue"], b["revenue"])
    used = ["revenue"]
    if not (a.get("eps") and b.get("eps")):
        # No EPS tagged: rupee profit ÷ that year's diluted average shares.
        shares = {r["date"]: r["value"] for r in series.get("annualDilutedAverageShares") or [] if r["value"] > 0}
        if a.get("profit") is not None and b.get("profit") is not None and newest in shares and before in shares:
            a, b = {**a, "eps": a["profit"] / shares[newest]}, {**b, "eps": b["profit"] / shares[before]}
    if a.get("eps") and b.get("eps"):
        fund["eps_growth"] = change(a["eps"], b["eps"])
        fund["eps_change_text"] = swing(a["eps"], b["eps"], lambda v: f"₹{v:,.2f} a share")
        used.append("EPS")
    fund["growth_basis"] = f"NSE {a['basis']} annual results in rupees ({' and '.join(used)})"
    fund["revenue_basis"] = "NSE annual results (March-quarter filings)"
    fund["nse_annual"] = {k: {x: v.get(x) for x in ("revenue", "profit", "eps", "basis", "url")} for k, v in years.items()}
    note = (f"Annual {' and '.join(used)} growth use the company's rupee results filed on NSE ({a['basis']}): FY{newest[:4]} revenue "
            f"{crore_words(a['revenue'])} against {crore_words(b['revenue'])} in FY{before[:4]}"
            + (f", diluted EPS ₹{a['eps']:,.2f} against ₹{b['eps']:,.2f}" if "EPS" in used else "") + ".")
    fund["growth_note"] = note
    if fund.get("currency_note"):
        fund["currency_note"] = fund["currency_note"].replace(", so growth rates are rupee growth.", ".") + " " + note
    return note


def _filed_profit(y):
    """Profit attributable to shareholders from an NSE annual result, falling back to total profit when the attributable
    line is not tagged (read as 0; Nestlé India FY2026, which has no minority interest)."""
    p = y.get("profit")
    return p if p not in (None, 0, 0.0) else y.get("profit_total")


def newer_nse_year(fund, series, annual, price=None, splits=None):
    """When NSE's annual results reach a newer financial year than Yahoo's statements, ROE, net margin and annual
    revenue and EPS growth come from the filings. Seen: Nestlé India, November 2026. Yahoo's years stop at FY2025 (with
    Dec 2021 and Dec 2022 before it, its 15-month FY2024 missing), while NSE has FY2026: revenue ₹23,155 cr against
    ₹20,202 cr, profit ₹3,499 cr. The year before comes from NSE too, or from Yahoo when it has that year. The year
    before's EPS is put on today's share basis (Nestlé's 1:1 bonus of August 2025) by choosing the reading whose growth
    tracks profit growth. Mutates `fund` and appends the newer year's EPS to Yahoo's annual EPS for the P/E history;
    returns the note, or None when Yahoo is current or the filings are missing."""
    if (series.get("_currency") or {}).get("reported") or fund.get("growth_withheld"):
        return None
    try:
        years = (annual() if callable(annual) else annual) or {}
    except Exception:  # a newer year is a bonus, never a reason to fail
        years = {}
    if not years:
        return None
    newest = max(years)
    mine = [r["date"] for n in ("annualNetIncome", "annualTotalRevenue") for r in (series.get(n) or [])[-1:]]
    if not mine or max(mine) >= newest:
        return None
    a = years[newest]
    profit = _filed_profit(a)
    if not a.get("revenue") or profit is None:
        return None
    yahoo = {n: {r["date"]: r["value"] for r in series.get(n) or []} for n in ("annualTotalRevenue", "annualNetIncome", "annualDilutedEPS")}
    before = [d for d in set(years) | set(yahoo["annualTotalRevenue"]) if d < newest and one_year_apart({"date": newest}, {"date": d})]
    b_date = max(before) if before else None
    b = years.get(b_date) or {}
    b_rev = b.get("revenue") or yahoo["annualTotalRevenue"].get(b_date)
    b_profit = _filed_profit(b) if b else yahoo["annualNetIncome"].get(b_date)
    basis = a.get("basis", "consolidated")
    old = max(mine)
    fund["net_margin"] = profit / a["revenue"]
    fund["margin_basis"] = f"FY{newest[:4]}, NSE {basis} annual results"
    fund["net_income"] = profit
    equity = {d: v for d, v in nse_equity(None, years).items()}
    for r in series.get("annualStockholdersEquity") or []:
        equity.setdefault(r["date"], r["value"])
    if equity.get(newest) and equity[newest] > 0:
        prev = equity.get(b_date) if b_date and (equity.get(b_date) or 0) > 0 else None
        fund["roe"] = profit / ((equity[newest] + prev) / 2 if prev else equity[newest])
        fund["roe_basis"] = f"FY{newest[:4]} profit ÷ {'average ' if prev else ''}equity, NSE {basis} annual results"
    bits = [f"FY{newest[:4]} revenue {crore_words(a['revenue'])}"]
    if b_date and b_rev:
        fund["revenue_growth"] = change(a["revenue"], b_rev)
        fund["growth_period"] = f"FY{newest[:4]} vs FY{b_date[:4]}"
        fund["revenue_basis"] = "NSE annual results (March-quarter filings)"
        bits[0] += f" against {crore_words(b_rev)} in FY{b_date[:4]} ({pct(fund['revenue_growth'])})"
        b_eps = b.get("eps") or yahoo["annualDilutedEPS"].get(b_date)
        if a.get("eps") and b_eps:
            factor = math.prod(r for d, r in _split_pairs(splits) if r and d > b_date)
            options = [b_eps] + ([b_eps / factor] if factor != 1 else [])
            grew = profit / b_profit if profit and b_profit and profit > 0 and b_profit > 0 else None
            b_eps = min(options, key=lambda v: abs((a["eps"] / v) / grew - 1) if grew and v > 0 else 0) if grew else options[0]
            fund["eps_growth"] = change(a["eps"], b_eps)
            fund["eps_change_text"] = swing(a["eps"], b_eps, lambda v: f"₹{v:,.2f} a share")
    bits.append(f"profit attributable to shareholders {crore_words(profit)} (net margin {pct(fund['net_margin'], sign=False)})")
    if "NSE" in (fund.get("roe_basis") or "") and f"FY{newest[:4]}" in fund["roe_basis"]:
        bits.append(f"ROE {pct(fund['roe'], sign=False)}")
    # The newer year's EPS joins the annual series for the P/E history when it matches profit ÷ today's shares.
    rows = series.get("annualDilutedEPS") or []
    shares = fund["market_cap"] / price if fund.get("market_cap") and price else None
    if a.get("eps") and a["eps"] > 0 and rows and newest > rows[-1]["date"] and shares and profit and profit > 0:
        implied = profit / shares
        later = math.prod(r for d, r in _split_pairs(splits) if r and d > newest)
        eps_now = next((v for v in (a["eps"], a["eps"] / later) if abs(v - implied) <= 0.15 * implied), None)
        if eps_now:
            series["annualDilutedEPS"] = list(rows) + [{"date": newest, "value": eps_now, "source": "NSE annual results"}]
    fund["year_gap"] = None
    note = (f"Yahoo's statements stop at FY{old[:4]}, but the company has filed FY{newest[:4]} results on NSE, so ROE, net margin and annual "
            f"growth use them ({basis}): " + "; ".join(bits) + ".")
    fund["newer_year_note"] = note
    fund.setdefault("eps_flags", []).append(note)
    return note


def apply_quarter_results(fund, nse):
    """Prefer the exchange's own year-on-year quarter figures over Yahoo's when both quarters were filed."""
    if not nse or "latest" not in nse:
        return
    latest_q, ago = nse["latest"], nse.get("year_ago")
    fund["quarter_nse"] = {k: v for k, v in nse.items() if k not in ("filings", "quarters")}
    # Quarter on quarter: the quarter just before the latest (80-100 days earlier) on the same basis.
    before = next((q for q in nse.get("recent") or [] if 80 <= (date.fromisoformat(latest_q["quarter"]) - date.fromisoformat(q["quarter"])).days <= 100), None)
    if before and before.get("revenue") and latest_q.get("revenue"):
        fund["quarter_revenue_qoq"] = change(latest_q["revenue"], before["revenue"])
        fund["quarter_revenue_qoq_basis"] = f"quarter to {latest_q['quarter']} vs {before['quarter']}"
    if not ago or latest_q.get("profit") is None or ago.get("profit") is None:
        return
    if fund.get("latest_quarter") and fund["latest_quarter"]["date"] > latest_q["quarter"]:
        return  # Yahoo already has a newer quarter than NSE's filings list
    fund["latest_quarter"] = {"date": latest_q["quarter"], "value": latest_q["profit"]}
    fund["quarter_year_ago"] = ago["quarter"]
    fund["quarter_profit_yoy"] = change(latest_q["profit"], ago["profit"])
    fund["quarter_profit_yoy_text"] = swing(latest_q["profit"], ago["profit"], crore_words)
    fund["quarter_revenue_yoy"] = change(latest_q["revenue"], ago["revenue"])
    fund["quarter_basis"] = f"NSE {latest_q['basis']} results, quarter to {latest_q['quarter']} vs {ago['quarter']} (profit attributable to shareholders)"
    fund["quarter_profit_values"] = (latest_q["profit"], ago["profit"])
    for key in ("quarter_total_yoy", "quarter_total_yoy_text", "quarter_total_values"):
        fund.pop(key, None)
    if latest_q.get("profit_total") is not None and ago.get("profit_total") is not None:
        # Press reports usually quote total profit after tax, minority interests included (Reliance's September 2026
        # quarter), which can differ from the shareholders' share by several points.
        fund["quarter_total_yoy"] = change(latest_q["profit_total"], ago["profit_total"])
        fund["quarter_total_yoy_text"] = swing(latest_q["profit_total"], ago["profit_total"], crore_words)
        fund["quarter_total_values"] = (latest_q["profit_total"], ago["profit_total"])


def total_profit_words(f):
    """'; total profit incl. minority interests +33.8%' when it differs from the shareholders' share by half a point or more."""
    own, total, text = f.get("quarter_profit_yoy"), f.get("quarter_total_yoy"), f.get("quarter_total_yoy_text")
    if total is not None and own is not None and abs(total - own) >= 0.005:
        return f"; total profit incl. minority interests {pct(total)}"
    own_pair, total_pair = f.get("quarter_profit_values"), f.get("quarter_total_values")
    if total is None and text and own_pair and total_pair and None not in own_pair + total_pair:
        # A swing reads the same unless minority interests change which side of zero a quarter lands on.
        if tuple(v >= 0 for v in own_pair) != tuple(v >= 0 for v in total_pair):
            return f"; total profit incl. minority interests {text}"
    return ""


def apply_balance_sheet(fund, nse):
    """Use the balance sheet in the latest NSE results filing when it is newer than Yahoo's: borrowings ÷ equity
    attributable to shareholders for debt/equity, and that equity for P/B. Returns the date used, or None."""
    latest_q = (nse or {}).get("latest") or {}
    sheet = latest_q.get("balance")
    if (not sheet or not sheet.get("equity") or sheet["equity"] <= 0 or sheet.get("borrowings") is None or fund.get("bank_like")
            or sheet.get("format", "ind_as") != "ind_as"):
        return None
    if fund.get("debt_date") and fund["debt_date"] >= sheet["date"]:
        return None
    fund["debt_to_equity"] = sheet["borrowings"] / sheet["equity"]
    fund["total_debt"] = sheet["borrowings"]
    fund["borrowings_ex_leases"], fund["lease_liabilities"] = sheet["borrowings"], None  # the filing's borrowings leave leases out
    fund["debt_date"] = sheet["date"]
    fund["debt_basis"] = (f"borrowings ÷ equity attributable to shareholders, balance sheet of {sheet['date']} in the NSE "
                          f"{latest_q.get('basis', 'consolidated')} results filing (newer than Yahoo's; lease liabilities not included)")
    if fund.get("market_cap") and (not fund.get("book_date") or fund["book_date"] < sheet["date"]):
        fund["pb"] = fund["market_cap"] / sheet["equity"]
        fund["book_date"] = sheet["date"]
        fund["book_basis"] = f"equity attributable to shareholders, balance sheet of {sheet['date']} (NSE results filing)"
    return sheet["date"]


PE_ONE_OFF_GAP = 0.10  # trailing P/E without one-offs this far from the reported one is shown beside it in the summary
SHARE_COUNT_GAP = 0.01  # Yahoo's share count this far from the latest NSE shareholding pattern's: market cap uses NSE's
BENIGN_HISTORY_NOTE = re.compile(r"did not extend|add no year")  # back-fill notes that are not a data gap


ALLOTTED = re.compile(r"allot(?:ment|ted)\s+(?:of\s+)?([\d,]{3,})\s+(?:fully\s+paid[- ]up\s+)?(?:equity\s+shares|shares|securities)", re.I)
# A "preferential issue" of unnamed "securities" can be warrants (shares only when converted), so it counts only when the
# text says equity shares, and is named as not counted otherwise.
EQUITY_ISSUE = re.compile(r"qualified\s+institution|\bQIP\b|rights\s+issue|equity\s+shares|\bES[OP]S\b|\bESOP|employee\s+stock|"
                          r"stock\s+option|conversion\s+of\s+(?:warrants|ccps|fccb)", re.I)
UNCLEAR_ISSUE = re.compile(r"preferential", re.I)
NOT_EQUITY = re.compile(r"debenture|\bNCDs?\b|commercial\s+paper|\bbonds?\b|non[- ]convertible", re.I)
MEETING_DAY = re.compile(r"held\s+on\s+([A-Z][a-z]+\s+\d{1,2},\s*\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]+,?\s*\d{4})")


def _allotment_kind(text):
    for pattern, kind in ((r"qualified\s+institution|\bQIP\b", "QIP"), (r"preferential", "preferential issue"), (r"rights", "rights issue"),
                          (r"ES[OP]S|ESOP|employee|stock\s+option", "employee stock options"), (r"conversion", "conversion")):
        if re.search(pattern, text, re.I):
            return kind
    return "allotment"


def _meeting_day(text):
    m = MEETING_DAY.search(text or "")
    if not m:
        return None
    raw = re.sub(r"(\d)(st|nd|rd|th)", r"\1", m.group(1)).replace(",", "")
    for fmt in ("%B %d %Y", "%d %B %Y", "%b %d %Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def equity_allotments(rows, after):
    """Equity shares allotted after `after` (ISO date) from NSE's corporate announcements: [{'date', 'shares', 'kind'}].
    Only announcements that give a share count and name an equity issue (QIP, rights issue, equity shares, ESOPs,
    conversion) count; debentures, NCDs, bonds and commercial paper never do. A preferential issue of unnamed
    "securities" (possibly warrants) is returned with counted=False. The allotment date is the board or committee meeting
    the text names, else the announcement date. A repeated announcement (same date and count) counts once."""
    from shareholding import nse_day
    out, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        text = " ".join(str(r.get(k) or "") for k in ("desc", "attchmntText"))
        m = ALLOTTED.search(text)
        counted = bool(EQUITY_ISSUE.search(text))
        if not m or NOT_EQUITY.search(text) or not (counted or UNCLEAR_ISSUE.search(text)):
            continue
        day = _meeting_day(r.get("attchmntText")) or nse_day(str(r.get("an_dt") or r.get("sort_date") or "")[:11])
        try:
            shares = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if not day or shares <= 0 or day.isoformat() <= after or (day.isoformat(), shares) in seen:
            continue
        seen.add((day.isoformat(), shares))
        out.append({"date": day.isoformat(), "shares": shares, "kind": _allotment_kind(text), "counted": counted})
    return sorted(out, key=lambda a: a["date"])


def nse_share_count(base, nse_fetcher):
    """{'shares', 'date', 'filed', 'pattern_shares', 'allotments'} from the latest quarter-end NSE shareholding pattern
    (total shares outstanding) plus equity allotments NSE announced after that quarter end (a QIP, preferential issue or
    ESOPs: Bajaj Finance's 11.41 cr QIP shares of 17 Nov 2026), or None. Announcements that fail to load add nothing."""
    from shareholding import api_url, filings, parse_xbrl
    listed = filings(nse_fetcher(api_url("/api/corporate-share-holdings-master", base)))
    if not listed or not listed[0].get("xbrl"):
        return None
    total = parse_xbrl(nse_fetcher(listed[0]["xbrl"])).get("total_shares")
    if not total or total <= 0:
        return None
    try:
        added = equity_allotments(json.loads(nse_fetcher(api_url("/api/corporate-announcements", base))), listed[0]["quarter"])
        read = True
    except (Block, ValueError, TypeError, KeyError, OSError):
        added, read = [], False
    return {"shares": total + sum(a["shares"] for a in added if a.get("counted", True)), "date": listed[0]["quarter"], "filed": listed[0].get("filed"),
            "pattern_shares": total, "allotments": added, "announcements_read": read}


def apply_share_count(fund, count, price):
    """Market cap on the share count of the latest NSE shareholding pattern, plus equity allotted since, when Yahoo's
    differs by more than 1% and the pattern is not older than Yahoo's count (Eternal, November 2026: Yahoo's 920.8 cr
    shares against 965 cr in the September 2026 pattern after ESOP allotments and the QIP, so ₹3.37 lakh cr instead of
    ₹3.53 lakh cr; Bajaj Finance: its 17 Nov 2026 QIP of 11.41 cr shares, ₹6.80 lakh cr → ₹6.92 lakh cr). Yields are
    rescaled with the whole count; P/B only with the pattern's count, because shares allotted after the balance sheet
    came with new capital the book does not show yet. Returns the note."""
    if not isinstance(count, dict) or not count.get("shares") or not fund.get("market_cap") or not price:
        return None
    yahoo = fund["market_cap"] / price
    if abs(count["shares"] / yahoo - 1) <= SHARE_COUNT_GAP or (fund.get("shares_date") and count["date"] < fund["shares_date"]):
        return None
    pattern = count.get("pattern_shares") or count["shares"]
    added = [a for a in count.get("allotments") or [] if a.get("shares") and a.get("counted", True)]
    unclear = [a for a in count.get("allotments") or [] if a.get("shares") and not a.get("counted", True)]
    k, k_book = count["shares"] / yahoo, pattern / yahoo
    fund["market_cap_yahoo"] = fund["market_cap"]
    fund["market_cap"] = price * count["shares"]
    for key in ("pb", "fcf_yield", "cash_dividend_yield"):
        if fund.get(key) is not None:
            fund[key] = fund[key] * k_book if key == "pb" else fund[key] / k
    fund["shares_source"] = {"shares": count["shares"], "date": count["date"], "yahoo_shares": yahoo, "pattern_shares": pattern,
                             "allotments": added}
    if added:
        parts = "; ".join(f"{a['shares'] / 1e7:,.2f} cr" + (f" ({a['kind']}, {day_month_year(a['date'])})") for a in added[-3:])
        more = f" and {len(added) - 3} earlier" if len(added) > 3 else ""
        source = (f"the {count['shares'] / 1e7:,.2f} cr shares from NSE's shareholding pattern for {day_month_year(count['date'])} "
                  f"({pattern / 1e7:,.2f} cr) plus equity allotted since in NSE announcements ({parts}{more})")
        tail = "P/B stays on the pattern's count until the balance sheet shows the new capital."
    else:
        source = f"the {count['shares'] / 1e7:,.2f} cr shares in NSE's shareholding pattern for {day_month_year(count['date'])}"
        tail = (("" if unclear else "NSE's announcements show no equity allotted since.") if count.get("announcements_read")
                else "Shares issued after that date (a QIP or allotment since) are not counted yet.")
    if unclear:
        tail = (tail + " " if tail else "") + ("Not counted: " + "; ".join(f"{a['shares'] / 1e7:,.2f} cr securities in a {a['kind']} ({day_month_year(a['date'])})"
                                             for a in unclear) + ", since the announcement does not say whether they are shares or warrants.")
    note = (f"Market cap uses {source} ({crore(fund['market_cap'])}); Yahoo's {yahoo / 1e7:,.2f} cr shares ({(k - 1) * 100:+.1f}%) "
            f"gave {crore(fund['market_cap_yahoo'])}. {tail}")
    fund["shares_note"] = note
    return note


PB_LAG_DAYS = 45  # a half-year balance sheet is published with the results, about six weeks after the period ends


def book_points(series, nse, years=None, trust_yahoo=True):
    """{balance-sheet date: book value per share} from shareholders' equity (NSE filings first, Yahoo's for older dates
    unless Yahoo's equity failed the cross-check) ÷ the share count nearest that date (Yahoo restates it for splits)."""
    equity = {} if not trust_yahoo else {r["date"]: r["value"] for n in ("annualStockholdersEquity", "quarterlyStockholdersEquity")
                                         for r in series.get(n, []) if r["value"] > 0}
    equity.update(nse_equity(nse, years))
    counts = sorted((r["date"], r["value"]) for n in ("annualOrdinarySharesNumber", "quarterlyOrdinarySharesNumber")
                    for r in series.get(n, []) if r["value"] > 0)
    if not counts:
        return {}
    nearest = lambda d: min(counts, key=lambda c: abs((date.fromisoformat(c[0]) - date.fromisoformat(d)).days))[1]
    return {d: e / nearest(d) for d, e in sorted(equity.items())}


def pb_band(bars, points, *, lag_days=PB_LAG_DAYS, years=5, today_book=None):
    """Price ÷ book value per share on every session over `years`, each balance sheet used from `lag_days` after its date
    (banks and finance companies are usually valued on book). `today_book` (book per share behind today's P/B) is used
    for the latest sessions so the band ends at today's P/B. Returns {'current', 'p25', 'median', 'p75', 'percentile',
    'start', 'sessions'} or None (fewer than 120 sessions)."""
    rows = sorted((date.fromisoformat(d) + timedelta(days=lag_days), v) for d, v in points.items() if v and v > 0)
    if not rows or not bars:
        return None
    last_day = date.fromisoformat(bars[-1]["at"][:10])
    start = last_day - timedelta(days=365 * years)
    series, first = [], None
    for b in bars:
        day = date.fromisoformat(b["at"][:10])
        usable = [v for avail, v in rows if avail <= day]
        if day < start or not usable:
            continue
        series.append(b["close"] / usable[-1])
        first = first or day.isoformat()
    if today_book and series:
        series[-1] = bars[-1]["close"] / today_book
    if len(series) < 120:
        return None
    ordered = sorted(series)
    q = lambda x: ordered[min(len(ordered) - 1, int(x * len(ordered)))]
    current = series[-1]
    return {"current": current, "p25": q(0.25), "median": statistics.median(ordered), "p75": q(0.75), "min": ordered[0], "max": ordered[-1],
            "percentile": sum(v <= current for v in ordered) / len(ordered), "start": first, "sessions": len(series),
            "basis": f"price ÷ book value per share, each half-year balance sheet used from {lag_days} days after its date"}


STATEMENT_GAP = 0.10  # Yahoo's net profit or shareholders' equity this far from the NSE results filing's: the filing's are used
STATEMENT_NUDGE = 0.03  # ... and once one is, a profit gap above this is replaced too


def nse_equity(nse, years=None):
    """{balance-sheet date: equity attributable to shareholders} from the NSE results filings read (half-year and annual
    filings carry a balance sheet), all on the latest quarter's basis."""
    out = {}
    for q in list((nse or {}).get("quarters") or []) + list((years or {}).values()):
        b = (q or {}).get("balance")
        if b and b.get("equity") and b["equity"] > 0:
            out.setdefault(b["date"], b["equity"])
    return out


def nse_trailing(nse):
    """{'profit', 'eps', 'end'} summed over the four latest quarters filed on NSE (consecutive, same basis), or None.
    'eps' is None when the quarters' own profit / EPS shows a change of share basis (a split) inside the year."""
    qs = sorted((nse or {}).get("recent") or [], key=lambda q: q["quarter"], reverse=True)[:4]
    if len(qs) < 4 or any(q.get("profit") is None for q in qs):
        return None
    if not 260 <= (date.fromisoformat(qs[0]["quarter"]) - date.fromisoformat(qs[3]["quarter"])).days <= 285:
        return None
    implied = [q["profit"] / q["eps"] for q in qs if q.get("eps") and q["profit"] and (q["profit"] > 0) == (q["eps"] > 0)]
    same = len(implied) == 4 and max(implied) <= 1.1 * min(implied)
    return {"profit": sum(q["profit"] for q in qs), "eps": sum(q["eps"] for q in qs) if same else None, "end": qs[0]["quarter"]}


def _year_before(points, day, low=300, high=430):
    """The value of the newest point 300-430 days before `day` ({ISO date: value}), or None."""
    end = date.fromisoformat(day)
    found = [(d, v) for d, v in points.items() if low <= (end - date.fromisoformat(d)).days <= high]
    return max(found)[1] if found else None


def _same_basis_eps(value, yahoo, factor):
    """A filing's EPS put on Yahoo's (restated) share basis: divided by the later splits' ratio when that lands closer to
    Yahoo's figure; None when neither is within 15% (a different basis altogether)."""
    options = [value, value / factor] if factor != 1 else [value]
    best = min(options, key=lambda v: abs(v - yahoo))
    return best if yahoo and abs(best - yahoo) <= 0.15 * abs(yahoo) else None


def cross_check_statements(fund, series, nse, annual, price, splits=None):
    """Yahoo's net profit and shareholders' equity against the company's own NSE results filings (consolidated when it
    files them). When either is more than 10% away, ROE, P/B, P/E and net margin are recomputed from the filings.

    Seen: HDFC Bank, November 2026. Yahoo's FY2026 net profit of ₹70,479 cr is not the consolidated figure filed on NSE
    (₹76,026 cr; Yahoo's FY2025 ₹67,351 cr is the bank's standalone profit), and Yahoo's equity of ₹8,16,740 cr at 31 Mar
    2026 is far above the ₹5,81,514 cr of capital and reserves in the filing (Yahoo's equity jumped about ₹4 lakh cr at the
    July 2023 HDFC merger, more than the merger added). So ROE read 8.9% against about 14%, and P/B 1.52 against about 2.1.
    Returns the note (also put first among the cautions), or None when the figures agree."""
    if not nse or "latest" not in nse or (series.get("_currency") or {}).get("basis") == "today":
        return None
    try:
        years = (annual() if callable(annual) else annual) or {}
    except Exception:  # a cross-check must never break the report
        years = {}
    ni_row = latest(series, "annualNetIncome")
    fy = ni_row["date"] if ni_row else None
    a = (years.get(fy) or {}) if fy else {}
    checks = []
    if ni_row and a.get("profit") and a["profit"] > 0 and abs(ni_row["value"] / a["profit"] - 1) > STATEMENT_GAP:
        checks.append({"item": "profit", "date": fy, "yahoo": ni_row["value"], "nse": a["profit"]})
    filed = nse_equity(nse, years)
    yahoo_eq = {r["date"]: r["value"] for n in ("annualStockholdersEquity", "quarterlyStockholdersEquity") for r in series.get(n, [])}
    common = sorted(d for d in set(filed) & set(yahoo_eq) if yahoo_eq[d] > 0)
    if common and abs(yahoo_eq[common[-1]] / filed[common[-1]] - 1) > STATEMENT_GAP:
        d = common[-1]
        checks.append({"item": "equity", "date": d, "yahoo": yahoo_eq[d], "nse": filed[d]})
    if not checks:
        return None
    if "profit" not in {c["item"] for c in checks} and ni_row and a.get("profit") and a["profit"] > 0 and abs(ni_row["value"] / a["profit"] - 1) > STATEMENT_NUDGE:
        # Once one statement is off, a smaller profit gap is not trusted either (HDFC Bank FY2026: ₹70,479 cr against ₹76,026 cr filed, 7%).
        checks.append({"item": "profit", "date": fy, "yahoo": ni_row["value"], "nse": a["profit"]})
    kinds = {c["item"] for c in checks}
    basis = nse["latest"].get("basis", "consolidated")
    before = {k: fund.get(k) for k in ("roe", "pb", "pe")}
    net = a["profit"] if "profit" in kinds else (ni_row["value"] if ni_row else None)
    equity = filed or yahoo_eq
    # ROE: the year's profit / average equity at its two year ends (trailing when the annual figure is distorted).
    ttm = nse_trailing(nse)
    if "trailing" in (fund.get("roe_basis") or "") and ttm and equity:
        last = max(equity)
        prev = _year_before(equity, last)
        base = (equity[last] + prev) / 2 if prev else equity[last]
        fund["roe"] = ttm["profit"] / base
        fund["roe_basis"] = f"trailing 12 months to {ttm['end']} ÷ equity, NSE {basis} results"
    elif net is not None and fy and equity.get(fy):
        prev = _year_before(equity, fy)
        base = (equity[fy] + prev) / 2 if prev else equity[fy]
        fund["roe"] = net / base
        fund["roe_basis"] = f"FY{fy[:4]} profit ÷ {'average ' if prev else ''}equity, NSE {basis} results"
    if equity and fund.get("market_cap"):
        last = max(equity)
        fund["pb"] = fund["market_cap"] / equity[last]
        fund["book_date"] = last
        fund["book_basis"] = f"equity attributable to shareholders, balance sheet of {last} (NSE {basis} results filing)"
    if "profit" in kinds:
        if ttm and ttm.get("eps") and ttm["eps"] > 0:
            fund["eps"] = {"date": ttm["end"], "value": ttm["eps"], "basis": "trailing 12 months, NSE results"}
            fund["pe"] = price / ttm["eps"]
        elif a.get("eps") and a["eps"] > 0:
            fund["eps"] = {"date": fy, "value": a["eps"], "basis": "last financial year, NSE results"}
            fund["pe"] = price / a["eps"]
        if a.get("revenue"):
            fund["net_margin"] = a["profit"] / a["revenue"]
            fund["margin_basis"] = f"FY{fy[:4]}, NSE {basis} results"
        fund["net_income"] = a["profit"]
        # Annual EPS on the filings' basis for the growth figure and the P/E history (put on Yahoo's share basis).
        rows = series.get("annualDilutedEPS") or []
        pairs = [(d, r) for d, r in _split_pairs(splits) if r]
        patched = {}
        for row in rows:
            y = years.get(row["date"]) or {}
            if y.get("eps") and y["eps"] > 0 and row.get("value"):
                v = _same_basis_eps(y["eps"], row["value"], math.prod(r for d, r in pairs if d > row["date"]))
                if v:
                    patched[row["date"]] = v
        if patched:
            series["annualDilutedEPS"] = [{**r, "value": patched.get(r["date"], r["value"]), **({"source": "NSE results"} if r["date"] in patched else {})}
                                          for r in rows]
            last2 = [r["date"] for r in rows[-2:]]
            if len(last2) == 2 and all(d in patched for d in last2) and fund.get("eps_growth") is not None:
                fund["eps_growth"] = change(patched[last2[1]], patched[last2[0]])
                fund["eps_change_text"] = None
    said = "; ".join(f"FY{c['date'][:4]} net profit attributable to shareholders {crore_words(c['yahoo'])} on Yahoo against {crore_words(c['nse'])} filed"
                     if c["item"] == "profit" else
                     f"shareholders' equity at {day_month_year(c['date'])} {crore_words(c['yahoo'])} on Yahoo against {crore_words(c['nse'])} filed"
                     for c in checks)
    changed = lambda k: fund.get(k) is not None and before[k] is not None and abs(fund[k] - before[k]) > 1e-9
    now_words = ", ".join(x for x in (
        f"ROE {pct(fund['roe'], sign=False)}" if changed("roe") else None,
        f"P/B {num(fund['pb'], 2)} (book at {day_month_year(fund['book_date'])})" if changed("pb") else None,
        f"P/E {num(fund['pe'], 1)} ({fund['eps']['basis']})" if changed("pe") else None) if x)
    was_words = ", ".join(x for x in (
        f"ROE {pct(before['roe'], sign=False)}" if changed("roe") else None,
        f"P/B {num(before['pb'], 2)}" if changed("pb") else None,
        f"P/E {num(before['pe'], 1)}" if changed("pe") else None) if x)
    note = (f"Yahoo's figures differ from the company's NSE {basis} results by more than {STATEMENT_GAP * 100:.0f}% ({said}), so the ratios use "
            "the filings" + (f": {now_words}" if now_words else "") + (f" (Yahoo's figures gave {was_words})" if was_words else "") + ".")
    fund["statement_check"] = {"checks": checks, "basis": basis, "before": before, "note": note}
    fund.setdefault("eps_flags", []).insert(0, note)
    return note


def flag_latest_exceptional(fund, nse):
    """The latest quarter's profit or loss carried by exceptional items (TMPV, September 2026 quarter: a ₹5,531 cr loss
    after ₹6,738 cr of exceptional charges, with ₹374 cr of pre-tax profit before them). Returns the note."""
    q = (nse or {}).get("latest") or {}
    exc, profit = q.get("exceptional"), q.get("profit")
    if not exc or profit is None or abs(exc) < 0.2 * max(abs(profit), 1.0):
        return None
    kind = "charges" if exc < 0 else "gains"
    result = "loss" if profit < 0 else "profit"
    before = q.get("pbt_before_exceptional")
    share = q.get("associates_share_after_tax")
    rest = []
    if before is not None:
        rest.append((f"profit before exceptional items and tax was {crore_words(before)}" if before >= 0
                     else f"the loss before exceptional items and tax was {crore_words(-before)}")
                    + (f" (including the {signed_crore(share)} share of joint ventures and associates, which the filing adds after tax; "
                       f"{signed_crore(q['pbt_before_exceptional_tagged'])} before it)"
                       if share and q.get("pbt_before_exceptional_tagged") is not None else ""))
    adjusted = profit - exc
    rest.append(f"adding the {kind} back with no tax effect, the {'profit' if adjusted >= 0 else 'loss'} would be about "
                f"{crore_words(abs(adjusted))}")
    note = (f"The latest quarter's {result} attributable to shareholders ({crore_words(abs(profit))}, quarter to {q['quarter']}) "
            f"includes {crore_words(abs(exc))} of exceptional {kind}; " + "; ".join(rest) + ".")
    fund["latest_exceptional"] = {"quarter": q["quarter"], "exceptional": exc, "profit": profit, "pbt_before_exceptional": before,
                                  "note": note}
    fund.setdefault("eps_flags", []).insert(0, note)
    return note


ONE_OFF_SHARE = 0.1  # exceptional items or negative other income this share of a quarter's profit count as a one-off


def disc_words(amount):
    """'profit from discontinued operations of ₹4,485 cr (a demerged or sold business, after tax)'."""
    return (f"{'profit' if amount > 0 else 'loss'} from discontinued operations of {crore_words(abs(amount))} "
            "(a demerged or sold business, after tax)")


def trailing_one_offs(fund, nse, price):
    """Quarters in the trailing twelve months with large exceptional items, negative other income or other income far
    above its usual level (TCS, December 2025 quarter: ₹3,391 cr of exceptional charges; Screener.in folds them into
    other income, −₹2,273 cr). Spikes come from fund['other_income_spikes'] (other_income_spikes()). Sets
    fund['one_offs'] and fund['one_off_note']; returns the note (None when there are none)."""
    found = []
    spikes = {s["quarter"]: s for s in fund.get("other_income_spikes") or []}
    for q in (nse or {}).get("recent") or []:
        profit, exc, other = q.get("profit"), q.get("exceptional") or 0.0, q.get("other_income")
        if profit is None:
            continue
        size = max(abs(profit), 1.0)
        big_exc = abs(exc) >= ONE_OFF_SHARE * size
        negative_other = other is not None and other < 0 and abs(other) >= ONE_OFF_SHARE * size
        spike = spikes.get(q["quarter"])
        disc = q.get("discontinued") or 0.0
        big_disc = abs(disc) >= ONE_OFF_SHARE * size
        if big_exc or negative_other or spike or big_disc:
            found.append({"quarter": q["quarter"], "exceptional": exc if big_exc else 0.0, "other_income": other,
                          "negative_other_income": negative_other, "profit": profit,
                          "other_income_excess": spike["excess"] if spike else 0.0, "other_income_usual": spike["usual"] if spike else None,
                          "discontinued": disc if big_disc else 0.0})
    fund["one_offs"] = found
    fund["one_off_note"] = None
    # The latest quarter's exceptional items already have their own note (flag_latest_exceptional).
    named = {(fund.get("latest_exceptional") or {}).get("quarter")}
    listed = [x for x in found if x["quarter"] not in named or x["other_income_excess"]]
    if not listed:
        return None
    parts = []
    for x in sorted(listed, key=lambda r: r["quarter"]):
        bits = []
        if x["exceptional"] and x["quarter"] not in named:
            bits.append(f"exceptional {'charges' if x['exceptional'] < 0 else 'gains'} of {crore_words(abs(x['exceptional']))}")
            folded = (x["other_income"] or 0.0) + x["exceptional"]
            if not x["negative_other_income"] and x["other_income"] is not None and folded < 0:
                bits.append(f"other income net of them −{crore_words(-folded)}")
        if x["negative_other_income"] and x["quarter"] not in named:
            bits.append(f"other income of −{crore_words(-x['other_income'])}")
        if x["other_income_excess"]:
            bits.append(f"other income of {crore_words(x['other_income'])}, about {crore_words(x['other_income_excess'])} above its usual "
                        f"{crore_words(x['other_income_usual'])} (often a one-off gain)")
        if x.get("discontinued"):
            bits.append(disc_words(x["discontinued"]))
        if bits:
            parts.append(f"{month_year(x['quarter'])} quarter: " + ", ".join(bits))
    if not parts:
        return None
    note = "Trailing earnings include one-offs (" + "; ".join(parts) + ")"
    eps = fund.get("eps") or {}
    removed = sum(-x["exceptional"] - (x["other_income"] if x["negative_other_income"] else 0.0) - x["other_income_excess"]
                  - x.get("discontinued", 0.0) for x in found)
    shares = fund["market_cap"] / price if fund.get("market_cap") and price else None
    if shares and eps.get("value") and not str(eps.get("basis")).startswith("last financial year") and fund.get("pe"):
        adjusted = eps["value"] + removed / shares
        if adjusted > 0:
            fund["pe_ex_one_offs"] = price / adjusted
            note += (f"; without them, before any tax effect, the trailing P/E would be about {price / adjusted:,.1f} "
                     f"instead of {fund['pe']:,.1f}")
    fund["one_off_note"] = note + "."
    return fund["one_off_note"]


# ---------- one-off gains in other income, and one-offs inside the latest annual EPS ----------

OTHER_INCOME_SPIKE = 1.5     # other income at least this multiple of its usual level (median of the other quarters read) ...
OTHER_INCOME_MATERIAL = 0.1  # ... with the excess at least this share of the quarter's pre-tax profit is flagged as a one-off gain
SPIKE_MIN_QUARTERS = 4       # other quarters needed before a usual level is trusted
LUMPY_SHARE = 0.7            # another quarter at this share of the spike or more: lumpy or stepped-up income, not a one-off
SEASONAL_SHARE = 0.6         # the same quarter a year apart at this share of the spike or more: a seasonal pattern (annual dividends)
ANNUAL_ONE_OFF_SHARE = 0.02  # one-offs (after tax) this share of the year's profit get an EPS growth and P/E estimate without them
GROWTH_MOVE = 0.01           # ... shown when growth without them moves at least a point,
BAND_MOVE = 0.05             # ... or the P/E-history position at least five percentile points,
EPS_MOVE = 0.05              # ... or (with no growth to compare) the year's EPS at least 5%


def other_income_spikes(quarters):
    """Quarters whose other income is far above the median of the other quarters read: a stake or investment sale booked
    in other income rather than as an exceptional item (Reliance, June 2025 quarter: ₹15,119 cr against a usual
    ₹4,900 cr or so, the ₹8,924 cr Asian Paints stake-sale gain inside it). Income that is lumpy or stepped up (another
    quarter nearly as high: Adani Ports read ₹837 cr, ₹752 cr and ₹853 cr against ₹234-296 cr in Nov 2026) or that
    recurs in the same quarter each year (annual dividends from subsidiaries) is not flagged. Returns [{'quarter', 'other_income', 'usual', 'excess', 'ratio', 'quarters_compared', 'share_of_pbt'}],
    oldest first."""
    rows = {}
    for q in quarters or []:
        if q.get("quarter") and q.get("other_income") is not None:
            rows.setdefault(q["quarter"], q)
    out = []
    for day, q in sorted(rows.items()):
        others = [r["other_income"] for d, r in rows.items() if d != day]
        if len(others) < SPIKE_MIN_QUARTERS:
            continue
        usual, income = statistics.median(others), q["other_income"]
        if usual <= 0 or income < OTHER_INCOME_SPIKE * usual:
            continue
        excess = income - usual
        scale = max(abs(q.get("pbt") or 0.0), abs(q.get("profit_total") or 0.0), abs(q.get("profit") or 0.0))
        if not scale or excess < OTHER_INCOME_MATERIAL * scale:
            continue
        when = date.fromisoformat(day)
        gap = lambda d: abs((date.fromisoformat(d) - when).days)
        if any(LUMPY_SHARE * income <= r["other_income"] for d, r in rows.items() if d != day):
            continue
        if any(SEASONAL_SHARE * income <= r["other_income"] for d, r in rows.items() if 355 <= gap(d) <= 375):
            continue
        out.append({"quarter": day, "other_income": income, "usual": usual, "excess": excess, "ratio": income / usual,
                    "quarters_compared": len(others), "share_of_pbt": excess / scale})
    return out


def spike_words(s):
    """'other income in the Jun 2025 quarter was ₹15,119 cr, about ₹10,214 cr above its usual ₹4,905 cr (median of the
    other 6 quarters filed on NSE)'."""
    return (f"other income in the {month_year(s['quarter'])} quarter was {crore_words(s['other_income'])}, about {crore_words(s['excess'])} "
            f"above its usual {crore_words(s['usual'])} (median of the other {s['quarters_compared']} quarters filed on NSE)")


def flag_quarter_spike(fund):
    """A spike in the latest quarter or the year-ago quarter moves the year-on-year profit change; adds a caution with the
    change without it (the pre-tax excess taken off, so an estimate). Returns the note."""
    spikes = {s["quarter"]: s for s in fund.get("other_income_spikes") or []}
    pair, latest = fund.get("quarter_profit_values"), (fund.get("latest_quarter") or {}).get("date")
    if not spikes or not pair or None in pair:
        return None
    now_s, ago_s = spikes.get(latest), spikes.get(fund.get("quarter_year_ago"))
    if not now_s and not ago_s:
        return None
    now = pair[0] - (now_s["excess"] if now_s else 0.0)
    ago = pair[1] - (ago_s["excess"] if ago_s else 0.0)
    note = f"{'The latest' if now_s else 'The year-ago'} quarter's profit includes unusually high other income, often a one-off gain: {spike_words(now_s or ago_s)}"
    if ago > 0:
        note += (f"; without it (estimate, before tax), profit attributable to shareholders would have changed about {pct(change(now, ago))} "
                 f"vs a year earlier instead of {growth_cell(fund.get('quarter_profit_yoy'), fund.get('quarter_profit_yoy_text'))}")
    fund["quarter_spike_note"] = note + ". Check that quarter's results release for the item."
    fund.setdefault("eps_flags", []).append(fund["quarter_spike_note"])
    return fund["quarter_spike_note"]


TAX_DEFAULT = 0.25  # India's corporate rate with surcharge, near enough, when no quarter gives a usable rate


def usual_tax_rate(quarters):
    """Median effective tax rate (tax ÷ pre-tax profit) of the quarters read, kept within 0-40%; TAX_DEFAULT without one."""
    rates = [q["tax"] / q["pbt"] for q in quarters if q.get("pbt") and q["pbt"] > 0 and q.get("tax") is not None and 0 <= q["tax"] / q["pbt"] <= 0.4]
    return statistics.median(rates) if rates else TAX_DEFAULT


def _shares_from(quarters):
    """Diluted shares implied by quarters' own profit ÷ EPS, on the newest quarter's share basis (the median of the
    quarters within 10% of the newest), or None. A split inside the year must not blend two bases: Kotak's 5-for-1
    split of late 2025 left 199 cr shares implied by the June and September 2025 quarters against 995 cr since."""
    implied = [(q["quarter"], q["profit"] / q["eps"]) for q in quarters
               if q.get("profit") and q.get("eps") and q["profit"] > 0 and q["eps"] > 0]
    if not implied:
        return None
    newest = max(implied)[1]
    return statistics.median(v for _, v in implied if abs(v / newest - 1) <= 0.1)


def year_items(quarters, spikes, end):
    """One-offs in the four quarters of the financial year ending `end`: exceptional items (charges negative) and other
    income above its usual level. Returns (items, the quarters of that year that were read)."""
    year = [q for q in quarters if 0 <= (end - date.fromisoformat(q["quarter"])).days < 360]
    items = []
    for q in sorted(year, key=lambda x: x["quarter"]):
        if q.get("exceptional"):
            items.append({"quarter": q["quarter"], "kind": "exceptional", "amount": q["exceptional"]})
        if q["quarter"] in spikes:
            items.append({"quarter": q["quarter"], "kind": "other income", "amount": spikes[q["quarter"]]["excess"], "spike": spikes[q["quarter"]]})
        if q.get("discontinued") and q.get("profit") and abs(q["discontinued"]) >= ONE_OFF_SHARE * abs(q["profit"]):
            items.append({"quarter": q["quarter"], "kind": "discontinued", "amount": q["discontinued"], "after_tax": True})
    return items, year


def item_words(x):
    if x["kind"] == "other income":
        return spike_words(x["spike"])
    if x["kind"] == "discontinued":
        return disc_words(x["amount"]) + f" in the {month_year(x['quarter'])} quarter"
    when = f"FY{x['quarter'][:4]} (full-year figure)" if x.get("full_year") else f"the {month_year(x['quarter'])} quarter"
    return f"exceptional {'charges' if x['amount'] < 0 else 'gains'} of {crore_words(abs(x['amount']))} in {when}"


def annual_one_offs(fund, nse, series, annual=None):
    """One-offs inside the latest annual EPS or the year before it (the two years behind 'EPS growth' and the P/E
    history): exceptional items and other-income spikes in the latest year's quarters, and the year before's exceptional
    items from the full-year figures in its March filing. Gives EPS growth and the P/E-history position without them,
    as estimates (amounts taxed at the company's usual rate, per diluted share); says nothing when neither moves.
    Reliance FY2026: EPS +16% includes the June 2025 stake-sale gain; TCS, Infosys and HCLTech FY2026: EPS growth held
    down by the December 2025 labour-code charges; Kotak Mahindra Bank FY2026: EPS -12.9% against a FY2025 that
    included ₹3,803 cr of exceptional gains (the June 2024 sale of 70% of its general-insurance arm). Banks are
    checked for exceptional items only (their other income is treasury and fee income, lumpy by nature).
    Sets fund['annual_one_off'] and appends its note to eps_flags; returns the note."""
    inputs = fund.pop("_band_inputs", None)
    rows = series.get("annualDilutedEPS") or []
    quarters = (nse or {}).get("quarters") or []
    if not rows or not quarters or fund.get("growth_withheld"):
        return None
    spikes = {} if fund.get("bank_like") else {s["quarter"]: s for s in fund.get("other_income_spikes") or []}
    end = date.fromisoformat(rows[-1]["date"])
    items, year = year_items(quarters, spikes, end)
    shares = _shares_from(year) or _shares_from(quarters)
    if not shares:
        return None
    # After tax at the company's usual effective rate (median over the quarters read): Reliance's ₹10,210 cr pre-tax
    # excess is about ₹7,800 cr after tax at its usual 23-24%, close to the ₹8,924 cr gain taxed as a capital gain.
    rate = usual_tax_rate(quarters)
    try:
        years = (annual() if callable(annual) else annual) or {}
    except Exception:  # the estimate must never break the report
        years = {}
    full_now = years.get(rows[-1]["date"]) or {}
    in_quarters = sum(x["amount"] for x in items if x["kind"] == "exceptional")
    after_tax = lambda xs: sum(x["amount"] if x.get("after_tax") else x["amount"] * (1 - rate) for x in xs)
    if full_now.get("exceptional") is not None and abs(full_now["exceptional"] - in_quarters) > 0.01 * abs(full_now.get("profit") or 1.0):
        # The full-year figure in the March filing, when the quarter filings read do not add up to it (HDFC Bank FY2026:
        # ₹3,193 cr of exceptional charges for the year, ₹1,476 cr in the quarter filings), so both years are measured alike.
        items = [x for x in items if x["kind"] != "exceptional"] + (
            [{"quarter": rows[-1]["date"], "kind": "exceptional", "amount": full_now["exceptional"], "full_year": True}] if full_now["exceptional"] else [])
    total = after_tax(items)
    profit = (sum(q["profit"] for q in year) if len(year) == 4 and all(q.get("profit") is not None for q in year)
              else rows[-1]["value"] * shares)
    if not profit or abs(total) < ANNUAL_ONE_OFF_SHARE * abs(profit):
        items, total = [], 0.0  # nothing material in the latest year; the year before may still have some
    fy = f"FY{end.year}"
    nse_years = fund.get("nse_annual") or {}
    eps_now = (nse_years.get(rows[-1]["date"]) or {}).get("eps") or rows[-1]["value"]
    adj_now = eps_now - total / shares
    out = {"year": rows[-1]["date"], "items": [{k: v for k, v in x.items() if k != "spike"} for x in items], "total": total,
           "tax_rate": rate, "per_share": total / shares, "eps": eps_now, "eps_adjusted": adj_now}
    # The year before: its own exceptional items (full-year figures in its March filing) and any spike in quarters read.
    prev_items, prev_row, adj_prev, eps_prev = [], rows[-2] if len(rows) >= 2 else None, None, None
    if prev_row and fund.get("eps_growth") is not None:
        prev_items = [x for x in year_items(quarters, spikes, date.fromisoformat(prev_row["date"]))[0] if x["kind"] == "other income"]
        full = (years or {}).get(prev_row["date"]) or {}
        if full.get("exceptional"):
            prev_items.append({"quarter": prev_row["date"], "kind": "exceptional", "amount": full["exceptional"], "full_year": True})
        eps_prev = (nse_years.get(prev_row["date"]) or {}).get("eps") or prev_row["value"]
        prev_shares = (full["profit"] / full["eps"] if full.get("profit") and full.get("eps") and full["profit"] > 0 and full["eps"] > 0
                       else shares)
        if not 0.7 <= prev_shares / shares <= 1.3:
            prev_shares = shares  # a profit tagged on another basis (Zydus's March 2025 filing: ₹147 cr against EPS ₹44.97), or a split since
        prev_total = after_tax(prev_items)
        if not items and prev_items and abs(prev_total) < ANNUAL_ONE_OFF_SHARE * abs(eps_prev * prev_shares):
            prev_items, prev_total = [], 0.0  # alone, the year before's one-offs must be material to be worth a note
        adj_prev = eps_prev - prev_total / prev_shares
        if adj_now > 0 and adj_prev > 0:
            out["growth"], out["growth_adjusted"] = fund["eps_growth"], change(adj_now, adj_prev)
    if not items and not prev_items:
        return None
    out["previous_items"] = [{k: v for k, v in x.items() if k != "spike"} for x in prev_items]
    # The P/E history on the corrected EPS (that year's row, and the year before's when it had one-offs too).
    band = fund.get("pe_band")
    if inputs and band and adj_now > 0 and abs((band.get("eps_used") or 0) - rows[-1]["value"]) <= 0.02 * max(abs(rows[-1]["value"]), 0.01):
        from screener import pe_band
        bars, eps_rows, kwargs = inputs
        fixed = {rows[-1]["date"]: rows[-1]["value"] - total / shares}
        if prev_items and adj_prev is not None and adj_prev > 0:
            fixed[prev_row["date"]] = prev_row["value"] - (eps_prev - adj_prev)
        redone = pe_band(bars, [{**r, "value": fixed.get(r["date"], r["value"])} for r in eps_rows], **kwargs)
        if redone:
            out["band"] = {k: redone[k] for k in ("current", "percentile", "start", "p25", "p75", "median")}
    growth_moved = out.get("growth_adjusted") is not None and abs(out["growth_adjusted"] - out["growth"]) >= GROWTH_MOVE
    band_moved = bool(out.get("band")) and abs(out["band"]["percentile"] - band["percentile"]) >= BAND_MOVE
    if not growth_moved and not band_moved and abs(adj_now / eps_now - 1) < EPS_MOVE:
        return None  # one-offs on both sides, or too small to change the picture (Grasim FY2026: +31.8% either way)
    if not band_moved:
        out.pop("band", None)
    if not growth_moved:
        out.pop("growth_adjusted", None)
    if items:
        spike = any(x["kind"] == "other income" for x in items)
        note = (f"{fy} earnings include one-offs: " + "; ".join(item_words(x) for x in items)
                + (". A jump like that is usually a one-off gain such as an investment or stake sale; check that quarter's results release"
                   if spike else "")
                + f". Without them (estimate: taxed at the usual {rate * 100:.0f}% rate, per diluted share), {fy} EPS would be about "
                  f"₹{adj_now:,.2f} instead of ₹{eps_now:,.2f}")
        if out.get("growth_adjusted") is not None:
            note += (f", and EPS growth about {pct(out['growth_adjusted'])} instead of {pct(out['growth'])}"
                     + (" (the year before's exceptional items taken out too)" if any(x["kind"] == "exceptional" for x in prev_items) else ""))
        if fund.get("roe") and str(fund.get("roe_basis") or "").startswith(("last financial year", f"FY{end.year}")) and eps_now > 0:
            # The same year's profit scaled by the EPS without them: Adani Enterprises FY2026 ROE 14.2% reported.
            out["roe_adjusted"] = fund["roe"] * adj_now / eps_now
            note += f"; ROE on that profit about {pct(out['roe_adjusted'], sign=False)} instead of {pct(fund['roe'], sign=False)}"
    else:
        if out.get("growth_adjusted") is None:
            return None
        prev_fy = f"FY{prev_row['date'][:4]}"
        note = (f"{fy} EPS growth is measured against a {prev_fy} that included one-offs: " + "; ".join(item_words(x) for x in prev_items)
                + f". Without them (estimate: taxed at the usual {rate * 100:.0f}% rate, per diluted share), {prev_fy} EPS would be about "
                  f"₹{adj_prev:,.2f} instead of ₹{eps_prev:,.2f}, and {fy} EPS growth about {pct(out['growth_adjusted'])} instead of "
                  f"{pct(out['growth'])}; check that year's results release for the item")
    if out.get("band"):
        b = out["band"]
        note += (f"; on that EPS today's P/E would be {num(b['current'], 1)}, {band_words(b)}, against {num(band['current'], 1)} "
                 f"and {band_words(band).split(' since ')[0]} on the reported EPS")
    out["note"] = note + "."
    fund["annual_one_off"] = out
    fund.setdefault("eps_flags", []).append(out["note"])
    return out["note"]


NO_HISTORY = ("STOCK: no price history for {}. Check the NSE symbol (for example RELIANCE, INFY, HDFCBANK), "
              "or the BSE ticker with .BO (for example RELIANCE.BO); the public feed may also be temporarily unavailable")


def analyse(raw_symbol, *, fetcher=request, news_fetcher=news_request, clock=instant, news_hours=168, peers=True, peer_symbols=None,
            news=True, nse_fetcher=None, dividends=True, optional_budget=None, pe_history=True, backfill_budget=None, holdings=True):
    """Full single-stock analysis. News and peers load in the background alongside the core data, within
    `optional_budget` seconds (OPTIONAL_BUDGET for live runs; no limit for test feeds unless given)."""
    requested = normalize(raw_symbol)
    now = clock()
    live, live_news = fetcher is request, news_fetcher is news_request
    if live_news and not live:
        news_fetcher, live_news = fetcher, False  # a test feed answers every URL itself, news included
    if nse_fetcher is None:
        # Live runs fetch NSE's company endpoints through their own allowlist; test feeds answer every URL themselves.
        nse_fetcher = live_nse() if live else fetcher
    if live:
        fetcher = live_fetcher()
    nse_fetcher = memo(nse_fetcher, keep=lambda url: url.endswith(".xml"))
    try:
        selected, history, notes = prepare_history(requested, fetcher=fetcher, now=now, years=5, actions_fetcher=nse_fetcher)
    except Block as error:
        if isinstance(error, SymbolChanged) or str(error).startswith("STOCK:"):
            raise
        raise Block(NO_HISTORY.format(requested)) from None
    except (ValueError, KeyError, TypeError):
        raise Block(NO_HISTORY.format(requested)) from None
    cut = notes["demerger"]
    base = selected.rsplit(".", 1)[0]
    errors = {}
    optional = OptionalSections(optional_budget if optional_budget is not None else (OPTIONAL_BUDGET if live else None))
    opt_public, opt_news, opt_nse = optional.fetchers(live=live, fetcher=fetcher, news_fetcher=news_fetcher, nse_fetcher=nse_fetcher, live_news=live_news)
    if peers:
        optional.start("peers", load_peers, selected, fetcher=opt_public, now=now, chosen=peer_symbols, nse_fetcher=opt_nse)
    if news:
        optional.start("news", gather_news, selected, history["name"], now, fetcher=opt_public, news_fetcher=opt_news, hours=news_hours)
    # Listed companies it holds a stake in (Reliance: Jio Platforms), for a sum-of-parts check.
    holdings_wanted = holdings and selected.endswith(".NS") and bool(business_entry(selected[:-3])[0].get("holdings"))
    if holdings_wanted:
        optional.start("holdings", load_holdings, selected[:-3], history["name"], fetcher=opt_public, nse_fetcher=opt_nse, now=now)
    # Shares outstanding from the latest NSE shareholding pattern: newer than Yahoo's count after a QIP, ESOPs or a merger.
    if selected.endswith(".NS"):
        optional.start("shares", nse_share_count, base, opt_nse)
    # Older annual EPS from NSE filings for a deeper P/E history, loaded alongside the core data under its own limit.
    backfill_job, backfill_pool, backfill_deadline = None, None, None
    if pe_history and selected.endswith(".NS"):
        budget = backfill_budget if backfill_budget is not None else (BACKFILL_BUDGET if live else None)
        backfill_deadline = time.monotonic() + budget if budget else None
        bf_nse, bf_public = backfill_fetchers(live=live, nse_fetcher=nse_fetcher, fetcher=fetcher, deadline=backfill_deadline)
        backfill_pool = ThreadPoolExecutor(max_workers=1)
        backfill_job = backfill_pool.submit(load_backfill, base, selected, history["bars"], nse_fetcher=bf_nse, fetcher=bf_public, now=now,
                                            deadline=backfill_deadline, store=filing_store() if live else None)
    if history.get("price_note"):
        errors["price"] = history["price_note"]
    try:
        benchmark = daily_history(BENCHMARK, fetcher=fetcher, now=now)
    except (Block, ValueError, KeyError, TypeError):
        benchmark = None
        errors["benchmark"] = "Nifty 50 history unavailable; relative strength omitted"
    # A short history is a new listing only when Yahoo's own first-trade date is recent; otherwise the feed is incomplete.
    first = history.get("first_trade")
    recent = bool(selected.endswith(".NS") and first and (now.date() - date.fromisoformat(first)).days <= NEW_LISTING_DAYS)
    try:
        tech = technicals(history, benchmark, now, new_listing=recent)
    except Block as error:
        if selected.endswith(".BO"):
            raise Block(f"{error}. The public BSE (.BO) history is often short; try the NSE symbol instead") from None
        raise
    # A large demerger changes the business: EPS from before it does not belong with today's price.
    major = bool(cut and not cut["minor"])
    since = cut["record_date"] if major else None
    try:
        series = fundamentals(selected, fetcher=fetcher, now=now)
        if not series:
            raise Block("STOCK: no financial statements returned")
        fund = ratios(series, tech["last_close"], now, share_changes(selected, history))
        apply_dividends(fund, history, tech["last_close"], since=since if cut and cut["mode"] == "provider" else None)
        from screener import pe_band
        currency = series.get("_currency") or {}
        fund["currency_note"] = currency.get("note")
        fund["currency_label"] = currency.get("label")
        flag = demerger_pe_flag(base, fund.get("eps"), tech["last_close"], series.get("quarterlyDilutedEPS"), today=now.date())
        if flag:
            fund["demerger_pe_note"] = flag
            fund["eps_flags"].append(flag)
        elif major and fund.get("eps") and fund["eps"]["date"] < cut["record_date"]:
            fund["eps_flags"].append(f"The EPS used for P/E (dated {fund['eps']['date']}) predates the {cut['record_date']} demerger and "
                                     "includes the demerged business, so the P/E is not like-for-like.")
        nse = nse_quarter_results(base, nse_fetcher) if selected.endswith(".NS") else None
        apply_quarter_results(fund, nse)
        # Other income far above its usual level (a stake sale booked there): not checked for banks and finance companies.
        fund["other_income_spikes"] = [] if fund.get("bank_like") else other_income_spikes((nse or {}).get("quarters"))
        annual = functools.lru_cache(maxsize=1)(lambda: nse_annual_results(nse, nse_fetcher))
        check_revenue(fund, series, nse, annual)
        rupee_growth(fund, series, annual)
        fund["demerger_notes"] = demerger_statements(fund, series, base, cut)
        flag_unusual_base(fund)
        flag_distorted_base(fund, nse)
        if fund.get("quarter_note"):
            fund["eps_flags"].append(fund["quarter_note"])
        flag_quarter_spike(fund)
        apply_balance_sheet(fund, nse)
        if apply_share_count(fund, optional.result("shares") if selected.endswith(".NS") else None, tech["last_close"]):
            fund["eps_flags"].append(fund["shares_note"])
        cross_check_statements(fund, series, nse, annual, tech["last_close"], share_changes(selected, history))
        newer_nse_year(fund, series, annual, tech["last_close"], share_changes(selected, history))
        if fund.get("bank_like") and fund.get("pb"):
            # Lenders are valued on book: P/B against its own history matters more than the P/E band for them.
            trusted = not any(c["item"] == "equity" for c in (fund.get("statement_check") or {}).get("checks", []))
            try:
                years = annual() or {}
            except Exception:
                years = {}
            points = book_points(series, nse, years, trust_yahoo=trusted)
            fund["pb_band"] = pb_band(history["bars"], points, today_book=tech["last_close"] / fund["pb"])
        flag_latest_exceptional(fund, nse)
        if trailing_one_offs(fund, nse, tech["last_close"]):
            fund["eps_flags"].append(fund["one_off_note"])
        fund["demerger_note"] = " ".join(fund["demerger_notes"]) or None
        # Today's FX rate would distort past P/E, so the band needs period-rate conversion for foreign-currency reporters.
        fund["pe_band"] = None if currency.get("basis") == "today" or fund.get("pe_band_withheld") else pe_band(
            history["bars"], series.get("annualDilutedEPS", []), since=since)
        fund["_band_inputs"] = (history["bars"], series.get("annualDilutedEPS", []), {"since": since})
        backfill_loaded = None
        if backfill_job is not None and currency.get("basis") != "today" and not fund.get("pe_band_withheld"):
            try:
                wait = None if backfill_deadline is None else max(0.0, backfill_deadline - time.monotonic()) + GRACE
                loaded = backfill_job.result(timeout=wait)
            except FuturesTimeout:
                loaded = {"error": "the NSE back-fill timed out"}
            except Exception:  # the deeper history must never break the report
                loaded = {"error": "the NSE back-fill failed"}
            deep_pe_band(fund, loaded, series.get("annualDilutedEPS", []), since=since)
            backfill_loaded = loaded
        if fund["pe_band"] and cut and not major:
            fund["pe_band"]["basis"] += f"; years before the {cut['record_date']} demerger include the small demerged business"
        problem = band_problem(fund.get("pe_band"), series.get("annualDilutedEPS"), series.get("annualNetIncome"), fund)
        if problem:
            fund["pe_band"], fund["pe_band_dropped"] = None, problem
            fund.pop("_band_inputs", None)
            errors["pe_history"] = f"P/E against its own history not shown: {problem}."
        elif fund.get("pe_history_note") and fund.get("pe_band") and not BENIGN_HISTORY_NOTE.search(fund["pe_history_note"]):
            errors["pe_history"] = (f"P/E history starts {month_year(fund['pe_band']['start'])} (Yahoo's years only): older annual EPS from "
                                    f"NSE could not be added ({fund['pe_history_note']}).")
        annual_one_offs(fund, nse, series, annual)
        fund.pop("_band_inputs", None)
        try:
            years_read = annual() or {}
        except Exception:  # the multi-year view must never break the report
            years_read = {}
        fund["growth_3y"] = multi_year_growth(fund, series, years_read, backfill_loaded, base)
        if dividends:
            try:
                fund["dividend_quality"] = dividend_record(selected, fetcher=fetcher, now=now, fund=fund, nse_fetcher=nse_fetcher)
            except (Block, ValueError, KeyError, TypeError, IndexError, OSError):
                fund["dividend_quality"] = None
                errors["dividend_history"] = "Ten-year dividend history unavailable; dividend record omitted"
    except (Block, ValueError, KeyError, TypeError):
        fund = None
        errors["fundamentals"] = "Financial statements unavailable from the public feed; ratios omitted"
    comparison = None
    if peers:
        own = {"symbol": selected, "name": history["name"], "price": tech["last_close"], "return_1y": tech["returns"]["1Y"],
               **({"market_cap_cr": fund["market_cap"] / 1e7 if fund.get("market_cap") else None,
                   **{k: fund.get(k) for k in ("pe", "pb", "roe", "net_margin", "revenue_growth")}} if fund else {})}
        loaded = optional.result("peers")
        if loaded is TIMED_OUT:
            errors["peers"] = f"Peer comparison timed out (the public feeds took longer than {OPTIONAL_BUDGET} s); peers omitted"
        elif isinstance(loaded, BaseException):
            errors["peers"] = "Peer comparison unavailable"
        else:
            try:
                comparison = peer_comparison(selected, own, now=now, loaded=loaded)
                if not comparison["rows"]:
                    errors["peers"] = f"No peer group found ({comparison['basis']})"
            except (Block, ValueError, KeyError, TypeError, OSError):
                errors["peers"] = "Peer comparison unavailable"
    if fund and comparison and comparison.get("rows"):
        fund["low_roe_note"] = low_roe_reason(fund, series, (comparison.get("medians") or {}).get("roe"))
    sotp = None
    if holdings_wanted:
        loaded = optional.result("holdings")
        if loaded is TIMED_OUT:
            errors["holdings"] = f"Sum of parts timed out (the public feeds took longer than {OPTIONAL_BUDGET} s); listed holdings not valued"
        elif isinstance(loaded, (BaseException, type(None))):
            errors["holdings"] = "Sum of parts unavailable (listed holdings could not be loaded)"
        else:
            sotp = sum_of_parts((fund or {}).get("market_cap"), (fund or {}).get("pe"), loaded)
            if fund:
                fund["lender_debt"] = lender_debt(fund, loaded)
            if not sotp:
                errors["holdings"] = "Sum of parts unavailable: " + "; ".join(f"{r['symbol']} ({r.get('error') or 'stake not found among promoter entities'})"
                                                                               for r in loaded["rows"])
    if news:
        headlines = optional.result("news")
        timed_out = headlines is TIMED_OUT or (optional.expired() and isinstance(headlines, dict) and headlines.get("failures"))
        if not isinstance(headlines, dict):
            headlines = {"items": [], "failures": [{"source": "news", "reason": "timed out" if headlines is TIMED_OUT else "unavailable"}],
                         "query": None, "headline": None,
                         "note": "Headlines only; open the story before treating it as fact. Search results can include unrelated or promotional items."}
        if headlines["failures"] and not headlines["items"]:
            headlines["timed_out"] = bool(timed_out)
            errors["news"] = ("News feeds " + ("timed out" if timed_out else "unavailable")
                              + "; no headlines shown. That does not mean there was no news.")
    else:
        headlines = {"items": [], "failures": [], "query": None, "skipped": True, "note": "News not fetched for this run."}
    optional.close()
    if backfill_pool:
        backfill_pool.shutdown(wait=False)
    age = (now.date() - date.fromisoformat(cut["ex_date"])).days if cut else None
    return {"schema": "stock_analysis_v1", "symbol": selected, "requested_symbol": requested, "name": history["name"], "as_of": now.isoformat(),
            "renamed_note": notes["renamed"], "history_note": cut["note"] if cut else None, "history_note_old": bool(age and age > 365),
            "demerger": {k: v for k, v in cut.items() if k != "event"} if cut else None, "history_warnings": notes["gaps"],
            "price_note": history.get("price_note"), "session_open": session_open(now), "price_source": "Yahoo Finance daily chart (may be delayed ~15 min)",
            "fundamentals_source": "Yahoo Finance fundamentals timeseries (company filings as reported)",
            "technical": {k: v for k, v in tech.items() if k not in ("closes", "close_dates")}, "fundamentals": fund, "peers": comparison,
            "sum_of_parts": sotp, "news": headlines,
            "_bars": history["bars"],
            "outlook": outlook(tech, cut), "errors": errors, "warning": WARNING, "disclaimer": DISCLAIMER,
            "execution_eligible": False, "submitted": False}


def month_year(day):
    return date.fromisoformat(day).strftime("%b %Y") if day else "n/a"


def band_words(band):
    """'cheaper than on 80% of trading days since May 2024' (or 'more expensive than on ...')."""
    cheaper = 1 - band["percentile"]
    if cheaper >= 0.5:
        return f"cheaper than on {cheaper * 100:.0f}% of trading days since {month_year(band['start'])}"
    return f"more expensive than on {band['percentile'] * 100:.0f}% of trading days since {month_year(band['start'])}"


def band_position(band):
    return ("below its usual range" if band["current"] < band["p25"] else "above its usual range" if band["current"] > band["p75"]
            else "within its usual range")


def growth_cell(value, text=None):
    return text if value is None and text else pct(value)


def recurring_yield(f):
    d = (f or {}).get("dividend") or {}
    return d.get("recurring_yield") if d.get("recurring_yield") is not None else (f or {}).get("dividend_yield")


def markdown(r):
    t, f, o = r["technical"], r["fundamentals"], r["outlook"]
    name = r["symbol"]
    old_note = r.get("history_note") if r.get("history_note_old") else None
    lines = [f"# {r['name']} ({name}) — stock analysis", "", f"As of {r['as_of'][:16].replace('T', ' ')} IST · prices as of {t['last_close_date']}"
             + (" (today's session is still trading; the latest price will change)" if r.get("session_open") and t["last_close_date"] == r["as_of"][:10] else ""), "",
             *([f"> ⚠ {r['price_note']}", ""] if r.get("price_note") else []),
             *([f"> ⚠ {r['renamed_note']}", ""] if r.get("renamed_note") else []),
             *([f"> ⚠ {r['history_note']}", ""] if r.get("history_note") and not old_note else []),
             *[x for w in r.get("history_warnings") or [] for x in (f"> ⚠ {w}", "")],
             f"> {r['disclaimer']}", "", "## Snapshot", "",
             f"**₹{num(t['last_close'])}** ({pct(t['day_change'], 2)} vs previous close ₹{num(t['previous_close'])})", "",
             "| Period | Stock | Nifty 50 | Difference |", "| --- | ---: | ---: | ---: |"]
    for label, row in t["relative"].items():
        lines.append(f"| {label} | {pct(row['stock'])} | {pct(row['nifty'])} | {pct(row['difference'])} |")
    span = "52-week range" if t.get("history_sessions", 250) >= 250 else f"Range since {t['history_start']}"
    lines += ["", f"{span} ₹{num(t['low_52w'])} – ₹{num(t['high_52w'])}; price is {pct(t['from_high'])} from the high and {pct(t['from_low'])} from the low.", "",
              "## Technical condition", "", f"**Trend: {t['trend']}** (score {t['trend_score']:+d})", ""]
    lines += [f"- {'▲' if k == 'bull' else '▼'} {text}" for k, text in t["signals"]]
    day = t.get("indicator_date") or t["last_close_date"]
    lines += ["", "| Indicator | Value |", "| --- | ---: |",
              f"| 20 / 50 / 200-day average | {num(t['sma20'])} / {num(t['sma50'])} / {num(t['sma200'])} |",
              f"| RSI (14, Wilder; close of {day}) | {num(t['rsi14'], 1)} — {t['rsi_reading'] or 'n/a'} |",
              f"| Average daily range (ATR 14) | ₹{num(t['atr14'])} ({pct(t['atr14'] / t['last_close'] if t['atr14'] else None, sign=False)}) |",
              f"| Annualised volatility | {pct(t['annual_volatility'], 0, sign=False)} |",
              f"| Volume on {day} vs the 20 sessions before | {num(t['volume_latest'] / t['volume_avg20'], 2) + '×' if t['volume_latest'] and t['volume_avg20'] else 'n/a'} |",
              f"| Support (20-day / 60-day low) | ₹{num(t['support'])} / ₹{num(t['support_60d'])} |",
              f"| Resistance (20-day / 60-day high) | ₹{num(t['resistance'])} / ₹{num(t['resistance_60d'])} |", ""]
    if t.get("forming_bar"):
        lines += [f"Today's bar is still forming, so RSI and the volume ratio use completed sessions up to {day}.", ""]
    lines += ["## Financial ratios", ""]
    if f:
        eps = f["eps"]
        cur = f" ({f['currency_label']})" if f.get("currency_label") else ""
        rev_cur = " (rupee results filed on NSE)" if f.get("growth_basis") else cur
        eps_cur = rev_cur if "EPS" in (f.get("growth_basis") or "") else cur
        lines += [f"Latest annual statements: {f['financials_as_of'] or 'n/a'}" + (f"; EPS ({eps['basis']}) dated {eps['date']}" if eps else "") + ("  ⚠ EPS may be out of date" if f["eps_stale"] else ""), "",
                  "| Ratio | Value |", "| --- | ---: |",
                  f"| Market cap | {crore(f['market_cap'])} |",
                  f"| P/E ({eps['basis'] if eps else 'n/a'}) | {num(f['pe'], 1)} |",
                  f"| P/B (book value at {f.get('book_date') or 'n/a'}) | {num(f['pb'], 2)} |",
                  f"| ROE ({f.get('roe_basis') or 'last financial year'}) | {pct(f['roe'], sign=False)} |",
                  f"| Net margin ({f.get('margin_basis') or 'last financial year'}) | {pct(f['net_margin'], sign=False)} |", f"| Operating margin (after depreciation) | {pct(f['operating_margin'], sign=False)} |",
                  f"| EBITDA margin (close to Screener.in's OPM) | {pct(f.get('ebitda_margin'), sign=False)} |",
                  f"| Debt / equity{' (' + f['debt_basis'] + ')' if f.get('debt_basis') and not f.get('bank_like') else ''} | {'not meaningful (bank/financial)' if f.get('bank_like') else num(f['debt_to_equity'], 2)} |",
                  *([f"| Debt / equity without the listed lender it consolidates (estimate, see ⚠) | {num(f['lender_debt']['ex_de'], 2)} |"]
                    if (f.get("lender_debt") or {}).get("ex_de") is not None else []),
                  f"| Current ratio | {num(f['current_ratio'], 2)} |",
                  f"| Interest cover | {num(f['interest_cover'], 1)}× |" if f["interest_cover"] else "| Interest cover | n/a |",
                  f"| Free-cash-flow yield | {pct(f['fcf_yield'], sign=False)} |", f"| Dividend yield (trailing 12 months per share ÷ price) | {pct(f['dividend_yield'], sign=False)} |",
                  f"| Cash dividends paid (last FY) / market cap | {pct(f.get('cash_dividend_yield'), sign=False)} |",
                  f"| Revenue growth ({f.get('growth_period') or 'last year'}){rev_cur} | {'not comparable' if f.get('growth_withheld') else pct(f['revenue_growth'])} |",
                  *(cagr_rows(f, cur)),
                  f"| EPS growth ({f.get('growth_period') or 'last year'}){eps_cur} | {'not comparable' if f.get('growth_withheld') else growth_cell(f['eps_growth'], f.get('eps_change_text'))} |",
                  f"| Latest quarter profit attributable to shareholders vs same quarter a year earlier | {growth_cell(f.get('quarter_profit_yoy'), f.get('quarter_profit_yoy_text'))}"
                  + (f" ({f['latest_quarter']['date']} vs {f['quarter_year_ago']})" if f.get("quarter_year_ago") else "") + " |",
                  *([f"| Latest quarter total profit (incl. minority interests) vs a year earlier | {growth_cell(f.get('quarter_total_yoy'), f.get('quarter_total_yoy_text'))} |"]
                    if total_profit_words(f) else []),
                  f"| Latest quarter revenue vs same quarter a year earlier | {pct(f.get('quarter_revenue_yoy'))} |",
                  f"| Latest quarter profit vs previous quarter | {growth_cell(f['quarter_profit_change'], f.get('quarter_profit_text'))}" + (f" ({f['latest_quarter']['date']}{' ⚠ stale' if f['quarter_stale'] else ''})" if f["latest_quarter"] else "") + " |"]
        q = f.get("dividend_quality")
        if q:
            lines += [f"| Dividend payout (cash paid ÷ net profit, last FY) | {pct(q.get('payout_ratio'), 0, sign=False)} |",
                      f"| Financial years with a dividend, in a row | {q['years_paid_in_a_row']}{'+' if q.get('streak_capped') else ''}"
                      + (f" ({q['streak_note']})" if q.get("streak_note") else "") + " |",
                      f"| Dividend per share growth ({q['cagr_years']}, a year) | {pct(q.get('dps_cagr_5y'))} |",
                      f"| Free cash flow ÷ dividends paid (last FY) | {num(q.get('fcf_cover'), 1) + '×' if q.get('fcf_cover') is not None else 'n/a'} |"]
        lines.append("")
        if f.get("quarter_basis"):
            lines += [f"Quarter against a year earlier: {f['quarter_basis']}.", ""]
        if f.get("low_roe_note"):
            lines += [f["low_roe_note"], ""]
        if lender_debt_words(f):
            lines += [f"⚠ {lender_debt_words(f)}", ""]
        for flag in f.get("eps_flags") or []:
            lines += [f"⚠ {flag}", ""]
        for flag in (f.get("dividend") or {}).get("flags") or []:
            lines += [f"⚠ {flag}", ""]
        for note in f.get("demerger_notes") or []:
            lines += [f"⚠ {note}", ""]
        if f.get("quarter_yoy_withheld"):
            lines += [f"⚠ Latest quarter against a year earlier is not shown: {f['quarter_yoy_withheld']}.", ""]
        if f.get("currency_note"):
            lines += [f"⚠ {f['currency_note']}", ""]
        if f["split_after_statements"]:
            lines += [f"⚠ Split/bonus on {', '.join(f['split_after_statements'])} came after the EPS date; per-share ratios (P/E, P/B) may not reflect it. Verify on the exchange filing.", ""]
        if f["bank_like"]:
            lines += ["Looks like a bank or financial company: debt/equity, current ratio and operating margin are not meaningful; compare P/B, ROE and asset quality instead. "
                      "⚠ ROE and P/B here use Yahoo's book equity, which for some banks differs a lot from the net worth in the bank's own results "
                      "(seen: HDFC Bank, Yahoo about ₹8.2 lakh crore against about ₹5.5 lakh crore reported). Check the bank's reported return on net worth before relying on either.", ""]
        band = f.get("pe_band")
        if band:
            lines += ["### Valuation history", "",
                      f"Today's P/E on latest annual EPS is {num(band['current'], 1)}: {band_words(band)}, {band_position(band)} "
                      f"(typical {num(band['p25'], 1)}–{num(band['p75'], 1)}, median {num(band['median'], 1)}, low {num(band['min'], 1)}, high {num(band['max'], 1)}; "
                      f"{band['sessions']} sessions from {band['start']})."
                      + (f" (The P/E of {num(f['pe'], 1)} in the table uses {eps['basis']} EPS, so it is not compared with this range.)"
                         if f.get("pe") and eps and not eps["basis"].startswith("last financial year") else ""), "",
                      *([f"Longer history: {median_words(band)[2:]}"
                         + (f" (typical {num(band['long']['p25'], 1)}–{num(band['long']['p75'], 1)} since {band['long']['start']})" if band.get("long") else "")
                         + ".", ""] if median_words(band) else []),
                      *([f"Without the one-offs in FY{f['annual_one_off']['year'][:4]} EPS (estimate, see ⚠ above): P/E "
                         f"{num(f['annual_one_off']['band']['current'], 1)}, {band_words(f['annual_one_off']['band'])}.", ""]
                        if (f.get("annual_one_off") or {}).get("band") else []),
                      *([f"P/E history starts {month_year(band['start'])}: older annual EPS could not be added ({f['pe_history_note']}).", ""]
                        if f.get("pe_history_note") else []),
                      f"Basis: {band['basis']}. A low P/E versus history can reflect lower expected growth, not only cheapness.", ""]
        elif f.get("pe_band_withheld"):
            lines += ["### Valuation history", "", "Not shown: the annual EPS covers only part of a year of the current business (see above).", ""]
    else:
        lines += [r["errors"].get("fundamentals", "Unavailable"), ""]
    c = r.get("peers")
    if c and c["rows"]:
        cols = (("market_cap_cr", "Mkt cap ₹ cr"), ("pe", "P/E"), ("pb", "P/B"), ("roe", "ROE"), ("net_margin", "Net margin"),
                ("revenue_growth", "Revenue growth"), ("return_1y", "1Y return"))
        show = lambda k, v: "n/a" if v is None else indian(v) if k == "market_cap_cr" else f"{v:.1f}" if k in ("pe", "pb") else pct(v, sign=k in ("revenue_growth", "return_1y"))
        title = {"chosen": "## Peers", "industry": "## Industry peers (same NSE industry classification)",
                 "business": "## Business peers (companies in the same businesses)"}.get(
            c.get("kind"), "## Index peers (same NSE index, not necessarily the same business)")
        lines += [title, "", f"Group: {c['basis']} — {c.get('group_size')} stocks" + ("; the largest by market cap are shown" if c.get("truncated") else "")
                  + f"; medians use all {c.get('evaluated')} that loaded" + (" (group capped at 40)" if c.get("capped") else "") + ".", "",
                  *([f"⚠ {r['name']} is more than three times the size of every peer here, so this comparison says little about its valuation. "
                     "Compare it with companies in the same businesses instead.", ""] if c.get("size_mismatch") else []),
                  *([f"Could not load: {', '.join(c['failed'])}.", ""] if c.get("failed") else []),
                  "| Company | " + " | ".join(h for _, h in cols) + " |", "| --- |" + " ---: |" * len(cols),
                  f"| **{r['name']}** | " + " | ".join(f"**{show(k, c['own'].get(k))}**" for k, _ in cols) + " |"]
        for row in c["rows"]:
            lines.append(f"| {row['name']} | " + " | ".join(show(k, row.get(k)) for k, _ in cols) + " |")
        lines += [f"⚠ {n}" for n in (c.get("renamed") or []) + (c.get("notes") or [])]
        lines += ["| Group median | " + " | ".join("too spread" if k == "pe" and c.get("pe_spread_wide") else show(k, c["medians"].get(k)) for k, _ in cols) + " |", ""]
        if c.get("pe_spread_wide"):
            lines += [f"Peer P/Es span {c['pe_spread']:.0f}× (highest ÷ lowest), so a median P/E would describe none of them; compare the companies one by one.", ""]
        if c.get("kind") == "index":
            lines += ["These peers come from NSE index membership, not business similarity. For a fairer comparison, rerun with `--peers` and companies in the same businesses.", ""]
    if r.get("sum_of_parts"):
        lines += ["## Listed holdings (sum of parts)", "", sotp_line(r["name"], r["sum_of_parts"]), "",
                  "Stakes are the promoter entities matching the company's name in each holding's latest NSE shareholding pattern; values use "
                  "today's market caps. Holding companies usually trade at a discount to such a sum, so read it as a cross-check, not a fair value.", ""]
    if not r["news"].get("skipped"):
        lines += ["## News (last 7 days)", ""]
        key = r["news"].get("headline")
        if key:
            lines += [f"Latest headline about the company ({key['published_at'][:10]}): {key['headline']}" + (f" · {key['outlet']}" if key.get("outlet") else ""), ""]
        if r["news"]["items"]:
            for n in r["news"]["items"]:
                when = n["published_at"][:16].replace("T", " ")
                if "news.google.com" in n["url"]:
                    lines.append(f"- {when} — {n['headline']}" + (f" · {n['outlet']}" if n.get("outlet") else ""))
                else:
                    lines.append(f"- {when} — [{n['headline']}]({n['url']})" + (f" · {n['outlet']}" if n.get("outlet") else ""))
            lines += ["", r["news"]["note"] + " Links to each story are in the `--format json` output."]
            if r["news"].get("left_out"):
                lines.append(r["news"]["left_out"])
        else:
            lines.append("No matching headlines found in the public feeds. That does not mean there was no news."
                         + (f" {r['news']['left_out']}" if r["news"].get("left_out") else ""))
        if r["news"]["failures"]:
            lines.append(f"\nUnavailable feeds: {len(r['news']['failures'])}.")
        lines.append("")
    lines += ["## Outlook — ranges and scenarios, not a prediction", "", f"Current bias from the technical signals: **{o['bias']}**.", "",
              "| Horizon | Low (10%) | Typical (median) | High (90%) | Past windows that ended higher |", "| --- | ---: | ---: | ---: | ---: |"]
    for label, g in o["ranges"].items():
        lines.append(f"| {label} | ₹{num(g['low_10'])} | ₹{num(g['median'])} | ₹{num(g['high_90'])} | {g['share_positive'] * 100:.0f}% of {g['samples']} |")
    lines += ["", f"- **Bull case:** {o['bull_case']}", f"- **Bear case:** {o['bear_case']}"]
    if o["momentum_note"]:
        lines.append(f"- **Momentum:** {o['momentum_note']}")
    lines += ["", o["method"], ""]
    if old_note:
        lines += ["## Notes", "", f"- {old_note}", ""]
    if r["errors"]:
        lines += ["## Data gaps", ""] + [f"- {v}" for v in r["errors"].values()] + [""]
    lines.append(f"Sources: {r['price_source']}; {r['fundamentals_source']}; NSE index constituent lists and corporate actions; Google News, Yahoo and NSE news feeds. {r['warning']}")
    return "\n".join(lines) + "\n"


def median_words(band):
    """'; 5-year median 26.1, median since Jun 2019 24.8' when the NSE back-fill gave a deeper history ('' otherwise)."""
    parts = []
    if band.get("median_5y") is not None:
        parts.append(f"5-year median {num(band['median_5y'], 1)}")
    long = band.get("long")
    if long:
        label = "10-year median" if long["years"] >= 9.5 else f"median since {month_year(long['start'])}"
        parts.append(f"{label} {num(long['median'], 1)}")
    return "; " + ", ".join(parts) if parts else ""


def cagr_rows(f, cur=""):
    """Ratio-table rows for multi-year growth: from NSE's filings when they cover the years, else Yahoo's 3-year revenue CAGR."""
    g = f.get("growth_3y")
    if f.get("growth_withheld") or (g and g.get("withheld")):
        return [f"| Revenue CAGR (3 years) | not comparable (demerger) |"]
    if not g:
        return [f"| Revenue CAGR (3 years){cur} | {pct(f.get('revenue_cagr_3y'))} |"]
    span = f"FY{g['start'][:4]}–FY{g['end'][:4]}, {g['source']}"
    profit = pct(g["profit_cagr"]) if g.get("profit_cagr") is not None else f"n/m ({g['profit_note']})" if g.get("profit_note") else "n/a"
    return [f"| Revenue CAGR ({g['years']} years, {span}) | {pct(g['revenue_cagr'])} |",
            f"| Profit attributable to shareholders, CAGR ({g['years']} years, {span}) | {profit} |"]


def debt_words(f):
    """'0.84 at 30 Sep 2026', 'n/m (financial)' or 'n/a'."""
    if f.get("bank_like"):
        return "n/m (financial)"
    if f.get("debt_to_equity") is None:
        return "n/a"
    return num(f["debt_to_equity"], 2) + (f" at {day_month_year(f['debt_date'])}" if f.get("debt_date") else "")


def peer_line(c):
    label = {"chosen": "Peers", "industry": "Industry peers", "business": "Business peers"}.get(c.get("kind"), "Index peers")
    if c.get("pe_spread_wide"):
        pes = c.get("pe_list") or []
        show = lambda rows: ", ".join(f"{s} {p:.1f}" for s, p in rows)
        listed = show(pes) if len(pes) <= 6 else show(pes[:3]) + " … " + show(pes[-3:])
        text = (f"{label}: P/Es run from {pes[0][1]:.1f} to {pes[-1][1]:.1f} ({c['pe_spread']:.0f}× apart), too wide for a meaningful median "
                f"({listed}); median ROE {pct(c['medians'].get('roe'), sign=False)}")
    else:
        text = f"{label}: median P/E {num(c['medians'].get('pe'), 1)}, ROE {pct(c['medians'].get('roe'), sign=False)}"
    if c.get("kind") in ("industry", "business"):
        text = text.replace(label, f"{label} ({', '.join(r['symbol'].replace('.NS', '') for r in c['rows'])})", 1)
    if c.get("loss_making"):
        # A loss-making peer has no P/E, but leaving it out silently would hide the closest comparison (Swiggy for Eternal).
        text += f"; loss-making, P/E n/m: {', '.join(c['loss_making'])}"
    return text + ("; ⚠ the company is much larger than every peer, so the comparison is weak" if c.get("size_mismatch") else "") + "."


def demerged_label(cut):
    """'Kwality Wall's Limited (KWIL)' from the demerger record: company without bracketed asides, then the symbol."""
    company = re.sub(r"\s*\([^)]*\)", "", cut.get("resulting_company") or "").strip()
    symbol = cut.get("resulting_symbol")
    return f"{company} ({symbol})" if company and symbol else company or symbol or "the demerged business"


def short_history_note(r):
    """One plain sentence about a demerger in the price history, for the summary; None when it no longer matters there.

    A demerger Yahoo already allowed for and that is more than a year old changes nothing in the summary (Reliance's
    2023 Jio Financial demerger), so it is left out. Scaled prices stay mentioned while the demerger is inside the
    two years behind the outlook ranges and P/E history (TMPV, October 2025)."""
    cut = r.get("demerger")
    if not cut or not r.get("history_note"):
        return None
    who = demerged_label(cut)
    if cut.get("mode") == "scaled":
        if cut.get("sessions_since", 0) >= (r.get("outlook") or {}).get("history_sessions", 505):
            return None
        return f"Yahoo does not adjust prices before the {cut['ex_date']} demerger of {who}; they are scaled down so past and present compare like with like."
    if r.get("history_note_old"):
        return None
    return f"Yahoo's prices already allow for the {cut['ex_date']} demerger of {who}."


def gap_label(key, r):
    """'news (source unavailable)', 'peers (timed out)' or the plain section name, for the summary's gap line."""
    if key == "news":
        return "news (timed out)" if (r.get("news") or {}).get("timed_out") else "news (source unavailable)"
    if key == "pe_history":
        reason = (r.get("fundamentals") or {}).get("pe_band_dropped") or (r.get("fundamentals") or {}).get("pe_history_note") or ""
        return f"P/E history ({brief_caution(reason, 130)})" if reason else "P/E history"
    if "timed out" in str(r["errors"].get(key, "")):
        return f"{key} (timed out)"
    return key


def brief_caution(text, limit=110):
    """The opening clause of a caution, cut at a word boundary: 'FY2026 net profit ₹82,390 cr is about ₹223.64 a share…'."""
    head = re.split(r"[.;:](?=\s)", text, maxsplit=1)[0].strip()
    if len(head) > limit:
        head = head[:limit].rsplit(" ", 1)[0] + "…"
    return head


def summary(r):
    """Answer-first digest: valuation and quality first, then price and momentum, then warnings; old notes last."""
    t, f, o, c = r["technical"], r["fundamentals"], r["outlook"], r.get("peers")
    lines = [f"{r['name']} ({r['symbol']}) — prices as of {t['last_close_date']}" + (" (session still trading)" if t.get("forming_bar") else "")]
    for key in ("price_note", "renamed_note"):
        if r.get(key):
            lines.append(f"⚠ {r[key]}")
    short_note = short_history_note(r)
    if short_note and not r.get("history_note_old"):
        lines.append(f"⚠ {short_note}")
    if t.get("new_listing"):
        lines.append(f"⚠ Listed recently: only {t['history_sessions']} sessions of prices (since {t['history_start']}), so moving averages, RSI, "
                     "ranges and 1-year figures are not available yet; statements may predate the listing.")
    if f:
        band = f.get("pe_band")
        eps = f.get("eps")
        valuation = (f"P/E {num(f['pe'], 1)}" + (f" ({eps['basis']})" if eps else "") if f.get("pe") else
                     f"P/E not meaningful (EPS {eps_words(eps['value'])}, {eps['basis']})" if eps else "P/E n/a")
        # Large one-offs in the trailing year (a stake-sale gain, a demerged business's profit) move the headline P/E far
        # from what aggregators quoting recurring profit show (Hindustan Unilever, Nov 2026: 32.4 with ₹4,485 cr from the
        # demerged ice-cream business, about 46 without; Adani Enterprises 58 against about 110): say both up front.
        ex = f.get("pe_ex_one_offs")
        if ex and f.get("pe") and abs(ex / f["pe"] - 1) > PE_ONE_OFF_GAP:
            valuation += f", about {num(ex, 1)} without one-offs (before tax; see ⚠)"
        if band:
            valuation += (f"; on latest annual EPS {num(band['current'], 1)}, {band_words(band)} "
                          f"(usual range {num(band['p25'], 1)}–{num(band['p75'], 1)}{median_words(band)})")
            fixed = (f.get("annual_one_off") or {}).get("band")
            if fixed:
                valuation += (f" — but that EPS includes one-offs: without them about {num(fixed['current'], 1)}, "
                              f"{band_words(fixed).replace(' trading days', ' days')} (estimate, see ⚠ below)")
        elif f.get("pe_band_dropped"):
            valuation += f"; P/E against its own history not shown ({brief_caution(f['pe_band_dropped'], 100)})"
        book = f" (book value at {day_month_year(f['book_date'])})" if f.get("pb") and f.get("book_date") else ""
        if f.get("pb_band"):
            book += f", {band_words(f['pb_band'])} (usual range {num(f['pb_band']['p25'], 2)}–{num(f['pb_band']['p75'], 2)})"
        lines.append(f"Valuation: {valuation}; P/B {num(f['pb'], 2)}{book}; dividend yield {pct(recurring_yield(f), sign=False)} (12 months, excluding specials)"
                     + (f"; market cap {crore(f['market_cap'])}" if f.get("market_cap") else "") + ".")
        cur = f" ({f['currency_label']})" if f.get("currency_label") else ""
        if f.get("growth_withheld"):
            growth = "annual growth not comparable (demerger, see below)"
        elif f.get("growth_basis"):
            # Dollar reporters: both growth figures come from the rupee results filed on NSE.
            growth = (f"revenue {pct(f['revenue_growth'])}, EPS {growth_cell(f['eps_growth'], f.get('eps_change_text'))} "
                      f"({f.get('growth_period') or 'last year'}; rupee results filed on NSE" + ("" if "EPS" in f["growth_basis"] else f", EPS {f.get('currency_label')}") + ")")
        else:
            revenue = ("not shown (Yahoo's figure does not match the filings, see below)" if f.get("revenue_check") and f.get("revenue_growth") is None
                       else pct(f["revenue_growth"]) + (" (confirmed in NSE filings)" if (f.get("revenue_check") or {}).get("confirmed")
                                                        else " (NSE filings)" if f.get("revenue_basis") else ""))
            growth = f"revenue {revenue}, EPS {growth_cell(f['eps_growth'], f.get('eps_change_text'))} ({f.get('growth_period') or 'last year'}){cur}"
        fixed = (f.get("annual_one_off") or {}).get("growth_adjusted")
        if fixed is not None and not f.get("growth_withheld"):
            # Into the EPS growth's period bracket (the last one), not a "(NSE filings)" bracket after the revenue figure.
            cut = growth.rfind(")")
            growth = growth[:cut] + f"; EPS about {pct(fixed)} without one-offs, estimate" + growth[cut:] if cut >= 0 else growth
        quarter = growth_cell(f.get("quarter_profit_yoy"), f.get("quarter_profit_yoy_text"))
        several = cagr_words(f.get("growth_3y"))
        revenue_q = (f"revenue {pct(f['quarter_revenue_yoy'])} and " if f.get("quarter_revenue_yoy") is not None else "")
        lender = (f.get("lender_debt") or {})
        who = lender["subsidiaries"][0]["name"] + "'s" if len(lender.get("subsidiaries") or []) == 1 else "its listed lenders'"
        ex_lender = (f" (about {num(lender['ex_de'], 2)} without {who} borrowings, estimate; see ⚠)" if lender.get("ex_de") is not None
                     else " (mostly a listed lender's borrowings; see ⚠)" if lender else "")
        lines.append(f"Quality and growth: ROE {pct(f['roe'], sign=False)}" + (" (trailing 12 months)" if "trailing" in (f.get("roe_basis") or "") else "")
                     + f"; debt/equity {debt_words(f)}{ex_lender}; {growth}" + (f"; {several}" if several else "") + "; "
                     f"latest quarter {revenue_q}profit attributable to shareholders {quarter} vs a year earlier"
                     + (f" ({f['latest_quarter']['date']})" if f.get("latest_quarter") else "") + total_profit_words(f) + ".")
    if c and c.get("rows"):
        lines.append(peer_line(c))
    if f and f.get("low_roe_note"):
        lines.append(f["low_roe_note"])
    if r.get("sum_of_parts"):
        lines.append(sotp_line(r["name"], r["sum_of_parts"]))
    if t.get("new_listing"):
        lines.append(f"Price ₹{num(t['last_close'])} ({pct(t['day_change'], 2)} on the day); {pct(t.get('since_first'))} since the first close on {t['history_start']}; "
                     f"{pct(t['from_high'])} from its high since listing.")
        lines.append(f"Trend: too little history to call ({t['history_sessions']} sessions).")
    else:
        lines.append(f"Price ₹{num(t['last_close'])} ({pct(t['day_change'], 2)} on the day); 1 year {pct(t['returns']['1Y'])} vs Nifty "
                     f"{pct(t['relative']['1Y']['nifty'])}; {pct(t['from_high'])} from the 52-week high.")
        lines.append(f"Trend: {t['trend']}; RSI {num(t['rsi14'], 0)} ({t['rsi_reading'] or 'n/a'}; Wilder, close of {t.get('indicator_date') or t['last_close_date']}).")
    month = o["ranges"].get("1 month")
    if month:
        lines.append(f"Levels: {levels_words(t)}; past 1-month moves from today's price "
                     f"ran ₹{num(month['low_10'])} to ₹{num(month['high_90'])} (10th–90th percentile, not a forecast).")
    else:
        lines.append(f"Levels: {levels_words(t)}.")
    n = r["news"].get("headline") or (r["news"]["items"][0] if r["news"]["items"] else None)
    if n:
        lines.append(f"Latest headline ({n['published_at'][:10]}): {n['headline']}" + (f" · {n['outlet']}" if n.get("outlet") else ""))
    warnings = []
    if f:
        # One-offs in earlier quarters matter less than the demerger notes the growth line points to ("see below").
        warnings += ([x for x in f.get("eps_flags") or [] if x != f.get("one_off_note")] + ((f.get("dividend") or {}).get("flags") or [])
                     + (f.get("demerger_notes") or []))
        if f.get("currency_note"):
            warnings.append(f["currency_note"])
    warnings += r.get("history_warnings") or []
    if f and lender_debt_words(f):
        warnings.insert(0, lender_debt_words(f))
    if f and f.get("one_off_note"):
        warnings.append(f["one_off_note"])
    # Four cautions in full; one or two more are printed too, and a longer tail is named briefly rather than hidden.
    shown = warnings if len(warnings) <= 6 else warnings[:4]
    lines += [f"⚠ {x}" for x in shown]
    if len(warnings) > len(shown):
        lines.append("⚠ Also: " + "; ".join(brief_caution(x) for x in warnings[len(shown):]) + " (details in the full report).")
    # A P/E history withheld on purpose is explained on the valuation line already.
    gaps = [gap_label(k, r) for k in r["errors"] if k != "price" and not (k == "pe_history" and f and f.get("pe_band_dropped"))]
    if gaps:
        lines.append("Data gaps: " + ", ".join(gaps) + ".")
    if short_note and r.get("history_note_old"):
        lines.append(f"Note: {short_note}")
    lines.append("Research only; not investment advice.")
    return "\n".join(lines) + "\n"


# ---------- side-by-side comparison ----------

def compare(symbols, *, fetcher=request, clock=instant, nse_fetcher=None, workers=3):
    """Analyse several stocks (no news, no peer groups) and line up the figures that decide 'which looks best'."""
    picked = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
    if not 2 <= len(picked) <= 8:
        raise Block("COMPARE: give 2 to 8 symbols, e.g. --compare TCS INFY HCLTECH")
    budget = None
    if fetcher is request:
        nse_fetcher = nse_fetcher or live_nse()
        budget = BACKFILL_BUDGET
        # The Nifty history and USD/INR rates are the same for every stock: fetch them once.
        fetcher = memo(live_fetcher(), keep=lambda url: "%5ENSEI" in url or "INR=X" in url)

    def one(sym):
        try:
            return analyse(sym, fetcher=fetcher, clock=clock, peers=False, news=False, nse_fetcher=nse_fetcher or fetcher,
                           backfill_budget=budget)
        except SymbolChanged as error:
            return {"symbol": sym, "error": str(error)}
        except Block as error:
            return {"symbol": sym, "error": str(error)}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        reports = list(pool.map(one, picked))
    return compare_report(reports, picked, clock())


def compare_report(reports, symbols, now):
    """One row per analysed stock (analyse() reports, or {'symbol', 'error'}), with the trade-off lines."""
    rows = []
    for rep in reports:
        if rep.get("error"):
            rows.append({"symbol": rep["symbol"], "error": rep["error"]})
            continue
        f, t = rep["fundamentals"] or {}, rep["technical"]
        band = f.get("pe_band")
        rows.append({"symbol": rep["symbol"].replace(".NS", ""), "name": rep["name"], "price": t["last_close"], "price_date": t["last_close_date"],
                     "market_cap_cr": f["market_cap"] / 1e7 if f.get("market_cap") else None,
                     "roe": f.get("roe"), "roe_basis": f.get("roe_basis"), "debt_to_equity": None if f.get("bank_like") else f.get("debt_to_equity"),
                     "operating_margin": None if f.get("bank_like") else f.get("operating_margin"),
                     "ebitda_margin": None if f.get("bank_like") else f.get("ebitda_margin"), "net_margin": f.get("net_margin"),
                     "margin_basis": f.get("margin_basis"),
                     "bank_like": f.get("bank_like"), "pe": f.get("pe"), "pe_basis": (f.get("eps") or {}).get("basis"),
                     "pb": f.get("pb"), "pb_band": f.get("pb_band"), "book_date": f.get("book_date"),
                     "pe_band": band, "pe_band_words": band_words(band) if band else None, "pe_band_position": band_position(band) if band else None,
                     "revenue_growth": f.get("revenue_growth"), "eps_growth": f.get("eps_growth"), "eps_change_text": f.get("eps_change_text"),
                     "growth_period": f.get("growth_period"), "growth_withheld": f.get("growth_withheld"), "currency_label": f.get("currency_label"),
                     "growth_basis": f.get("growth_basis"),
                     "quarter_profit_yoy": f.get("quarter_profit_yoy"), "quarter_profit_yoy_text": f.get("quarter_profit_yoy_text"),
                     "quarter_revenue_yoy": f.get("quarter_revenue_yoy"), "latest_quarter": (f.get("latest_quarter") or {}).get("date"),
                     "quarter_revenue_qoq": f.get("quarter_revenue_qoq"),
                     "eps_growth_adjusted": (f.get("annual_one_off") or {}).get("growth_adjusted"),
                     "annual_one_off": {k: v for k, v in (f.get("annual_one_off") or {}).items() if k != "band"} or None,
                     "return_1y": t["returns"]["1Y"], "dividend_yield_ex_specials": recurring_yield(f), "dividend_yield": f.get("dividend_yield"),
                     "debt_date": None if f.get("bank_like") else f.get("debt_date"), "one_offs": f.get("one_offs") or [],
                     "latest_exceptional": f.get("latest_exceptional"), "one_off_note": f.get("one_off_note"),
                     "quarter_total_yoy": f.get("quarter_total_yoy"),
                     **multi_year_fields(f.get("growth_3y")),
                     "debt_to_equity_ex_lender": None if f.get("bank_like") else (f.get("lender_debt") or {}).get("ex_de"),
                     "flags": ([lender_debt_words(f)] if lender_debt_words(f) else [])
                              + ([cagr_words(f["growth_3y"]) + "."] if (f.get("growth_3y") or {}).get("jump") else []) + (f.get("eps_flags") or []) + ((f.get("dividend") or {}).get("flags") or []) + (f.get("demerger_notes") or [])
                              + ([f["currency_note"]] if f.get("currency_note") else []) + (rep.get("history_warnings") or []),
                     "errors": rep.get("errors") or {}})
    return {"schema": "stock_compare_v1", "as_of": now.isoformat(), "symbols": list(symbols), "rows": rows, "tradeoffs": tradeoffs(rows),
            "note": "Each figure uses the same definitions as the single-stock report. Research only; not a recommendation.",
            "execution_eligible": False, "submitted": False}


def span_words(rows):
    """' (FY2023–FY2026)' when every compared stock's multi-year growth covers the same years, else ''."""
    spans = {r.get("cagr_span") for r in rows if r.get("revenue_cagr_3y") is not None}
    return f" ({next(iter(spans))})" if len(spans) == 1 else ""


def multi_year_fields(g):
    """Compare-row fields for multi-year growth (None when withheld or missing)."""
    ok = bool(g) and not g.get("withheld")
    return {"revenue_cagr_3y": g.get("revenue_cagr") if ok else None, "profit_cagr_3y": g.get("profit_cagr") if ok else None,
            "cagr_span": f"FY{g['start'][:4]}–FY{g['end'][:4]}" if g else None, "cagr_note": (g or {}).get("withheld") or (g or {}).get("profit_note"),
            "cagr_source": (g or {}).get("source")}


DEBT_FREE = 0.15  # debt/equity below this for every stock compared: "all effectively debt-free", not a ranking


def tradeoffs(rows):
    """One line per dimension (quality, value, growth, momentum, income, balance sheet), each measure ranked best first,
    then one-offs that distort the figures. No tally of 'wins': measures inside a dimension move together (a cheap P/E
    today is usually also cheap against its own history), so counting them would nudge the reader towards one stock."""
    ok = [r for r in rows if not r.get("error")]
    lines, firsts = [], []

    def ranked(key, label, fmt, low=False, keep=lambda r: True, extra=lambda r: ""):
        vals = [r for r in ok if r.get(key) is not None and keep(r)]
        if len(vals) < 2:
            return None
        vals.sort(key=lambda r: (r[key] if low else -r[key], r["symbol"]))
        firsts.append(vals[0]["symbol"])
        return f"{label} " + ", ".join(f"{r['symbol']} {fmt(r[key])}{extra(r)}" for r in vals)

    def ttm(basis):
        return lambda r: " (TTM)" if "trailing" in (r.get(basis) or "") else ""

    def line(dimension, *parts, note="", period=""):
        parts = [p for p in parts if p]
        if parts:
            lines.append(f"{dimension} (best first{period}) — " + "; ".join(parts) + note + ".")
    share = lambda v: pct(v, sign=False)
    line("Quality", ranked("roe", "ROE:", share, extra=ttm("roe_basis")), ranked("net_margin", "net margin:", share, extra=ttm("margin_basis")))
    banded = [r for r in ok if r.get("pe_band")]
    history = None
    if len(banded) >= 2:
        ordered = sorted(banded, key=lambda r: (r["pe_band"]["percentile"], r["symbol"]))
        firsts.append(ordered[0]["symbol"])
        history = "P/E against its own history: " + ", ".join(
            f"{r['symbol']} {r['pe_band_words']}" + (f" (5-year median {num(r['pe_band']['median_5y'], 1)})" if r["pe_band"].get("median_5y") else "")
            for r in ordered)
    lenders = [r for r in ok if r.get("bank_like")]
    pb_today = pb_history = None
    if len(lenders) >= 2:
        # Banks and finance companies are valued on book as much as on earnings.
        pb_today = ranked("pb", "P/B today:", lambda v: f"{v:.2f}", low=True, keep=lambda r: bool(r.get("bank_like")))
        with_band = sorted((r for r in lenders if r.get("pb_band")), key=lambda r: (r["pb_band"]["percentile"], r["symbol"]))
        if len(with_band) >= 2:
            pb_history = "P/B against its own history: " + ", ".join(f"{r['symbol']} {band_words(r['pb_band'])}" for r in with_band)
    line("Value", ranked("pe", "P/E today:", lambda v: f"{v:.1f}", low=True, extra=lambda r: " (FY)" if str(r.get("pe_basis")).startswith("last financial year") else ""),
         history, pb_today, pb_history)
    comparable = lambda r: not r.get("growth_withheld")
    withheld = [r["symbol"] for r in ok if r.get("growth_withheld")]
    periods = sorted({r["growth_period"] for r in ok if r.get("growth_period") and comparable(r)})
    fixed = lambda r: f" ({pct(r['eps_growth_adjusted'])} without one-offs, estimate)" if r.get("eps_growth_adjusted") is not None else ""
    line("Growth",
         ranked("revenue_growth", "revenue:", pct, keep=comparable), ranked("eps_growth", "EPS:", pct, keep=comparable, extra=fixed),
         # One year can hide a slow trend (Asian Paints FY2026 +5.1% against about +1% a year since FY2023).
         ranked("revenue_cagr_3y", "revenue a year over three years" + span_words(ok) + ":", pct,
                extra=lambda r: "" if len({x.get("cagr_span") for x in ok if x.get("revenue_cagr_3y") is not None}) <= 1 else f" ({r['cagr_span']})"),
         ranked("profit_cagr_3y", "profit attributable to shareholders a year over three years:", pct),
         ranked("quarter_revenue_yoy", "latest quarter revenue vs a year earlier:", pct,
                extra=lambda r: f" ({pct(r['quarter_revenue_qoq'])} on the quarter before)" if r.get("quarter_revenue_qoq") is not None else ""),
         ranked("quarter_profit_yoy", "latest quarter profit attributable to shareholders vs a year earlier:", pct),
         note=f" ({', '.join(withheld)}: annual growth not comparable, demerger)" if withheld else "",
         period=f"; annual figures {', '.join(periods)}, in rupees" if periods else "")
    line("Momentum", ranked("return_1y", "1-year price return:", pct))
    line("Income", ranked("dividend_yield_ex_specials", "dividend yield excluding specials:", share))
    banks = [r["symbol"] for r in ok if r.get("bank_like")]
    sheets = {r.get("debt_date") for r in ok if r.get("debt_to_equity") is not None and not r.get("bank_like")}
    same = len(sheets) == 1 and None not in sheets
    levered = [r for r in ok if r.get("debt_to_equity") is not None and not r.get("bank_like")]
    if len(levered) >= 2 and all(r["debt_to_equity"] < DEBT_FREE for r in levered):
        # Ranking 0.05 against 0.08 against 0.10 would read as a difference that does not matter.
        lines.append("Balance sheet — all effectively debt-free (debt/equity"
                     + (f" at {day_month_year(next(iter(sheets)))}" if same else "") + ": "
                     + ", ".join(f"{r['symbol']} {r['debt_to_equity']:.2f}" + (f" at {day_month_year(r['debt_date'])}" if r.get("debt_date") and not same else "")
                                 for r in sorted(levered, key=lambda r: (r["debt_to_equity"], r["symbol"])))
                     + ")" + (f" ({', '.join(banks)}: not meaningful for banks and financials)" if banks else "") + ".")
        levered = None
    if levered is not None:
        line("Balance sheet", ranked("debt_to_equity", f"debt/equity at {day_month_year(next(iter(sheets)))}:" if same else "debt/equity:",
                                     lambda v: f"{v:.2f}", low=True, keep=lambda r: not r.get("bank_like"),
                                     extra=lambda r: f" at {day_month_year(r['debt_date'])}" if r.get("debt_date") and not same else ""),
             note=f" ({', '.join(banks)}: not meaningful for banks and financials)" if banks else "")
    one_offs = []
    for r in ok:
        items = []
        if r.get("latest_exceptional"):
            x = r["latest_exceptional"]
            items.append(f"{month_year(x['quarter'])} quarter {'loss' if x['profit'] < 0 else 'profit'} includes "
                         f"{crore_words(abs(x['exceptional']))} of exceptional {'charges' if x['exceptional'] < 0 else 'gains'}")
        skip = (r.get("latest_exceptional") or {}).get("quarter")
        for x in sorted(r.get("one_offs") or [], key=lambda q: q["quarter"]):
            if x["quarter"] == skip:
                continue
            bits = []
            if x.get("exceptional"):
                bits.append(f"exceptional {'charges' if x['exceptional'] < 0 else 'gains'} of {crore_words(abs(x['exceptional']))}")
                folded = (x.get("other_income") or 0.0) + x["exceptional"]
                if not x.get("negative_other_income") and x.get("other_income") is not None and folded < 0:
                    bits.append(f"other income net of them −{crore_words(-folded)}")
            if x.get("negative_other_income"):
                bits.append(f"other income −{crore_words(-x['other_income'])}")
            if x.get("other_income_excess"):
                bits.append(f"other income about {crore_words(x['other_income_excess'])} above its usual level (often a one-off gain)")
            if x.get("discontinued"):
                bits.append(disc_words(x["discontinued"]))
            if bits:
                items.append(f"{month_year(x['quarter'])} quarter " + ", ".join(bits))
        if items:
            one_offs.append(f"{r['symbol']}: " + "; ".join(items))
    if one_offs:
        lines.append("One-offs inside trailing earnings (they move the trailing P/E and the quarter comparison) — " + " | ".join(one_offs) + ".")
    annual = [r for r in ok if r.get("eps_growth_adjusted") is not None]
    if annual:
        lines.append("One-offs inside annual EPS growth (estimates without them, taxed at each company's usual rate) — " + " | ".join(
            f"{r['symbol']}: {pct(r['eps_growth'])} reported, about {pct(r['eps_growth_adjusted'])} without "
            + "; ".join((f"exceptional {'charges' if x['amount'] < 0 else 'gains'} of {crore_words(abs(x['amount']))}" if x["kind"] == "exceptional"
                         else disc_words(x["amount"]) if x["kind"] == "discontinued"
                         else f"other income about {crore_words(x['amount'])} above usual")
                        + (f" (FY{x['quarter'][:4]}, the year before)" if x.get("full_year") else f" ({month_year(x['quarter'])} quarter)")
                        for x in r["annual_one_off"]["items"] + r["annual_one_off"].get("previous_items", []))
            for r in sorted(annual, key=lambda r: r["symbol"])) + ".")
    leaders = set(firsts)
    if len(leaders) == 1 and len(ok) > 1 and firsts:
        lines.append(f"{firsts[0]} ranks first on every measure shown here; check the cautions and one-offs before reading that as a verdict.")
    elif firsts:
        lines.append("No single stock wins on every measure: weigh valuation against growth and quality for your own horizon.")
    return lines


def compare_table(report):
    books = any(r.get("bank_like") for r in report["rows"] if not r.get("error"))
    cols = ("Stock", "Price", "ROE", "Op. margin (EBITDA)", "Net margin", "Debt/eq", "P/E", "P/E vs own history",
            *(("P/B", "P/B vs own history") if books else ()), "Revenue growth", "EPS growth",
            "3-yr CAGR revenue / profit", "Latest qtr revenue YoY / QoQ", "Latest qtr profit YoY", "1Y return", "Div. yield ex-specials")
    lines = ["| " + " | ".join(cols) + " |", "| --- |" + " ---: |" * (len(cols) - 1)]
    position = lambda b: f"{'cheaper' if b['percentile'] < 0.5 else 'dearer'} than {max(b['percentile'], 1 - b['percentile']) * 100:.0f}% of days since {month_year(b['start'])}"
    for r in report["rows"]:
        if r.get("error"):
            lines.append(f"| {r['symbol']} | " + " | ".join(["n/a"] * (len(cols) - 1)) + " |")
            continue
        band = r.get("pe_band")
        lines.append("| " + " | ".join([
            f"{r['symbol']}", f"₹{num(r['price'])}", pct(r["roe"], sign=False) + (" (TTM)" if "trailing" in (r.get("roe_basis") or "") else ""),
            "n/m" if r.get("bank_like") else pct(r.get("ebitda_margin"), sign=False),
            pct(r.get("net_margin"), sign=False) + (" (TTM)" if "trailing" in (r.get("margin_basis") or "") else ""),
            "n/m" if r.get("bank_like") else num(r["debt_to_equity"], 2)
            + (f" ({num(r['debt_to_equity_ex_lender'], 2)} ex lender)" if r.get("debt_to_equity_ex_lender") is not None else ""),
            (f"{num(r['pe'], 1)}" if r.get("pe") else "loss" if r.get("pe_basis") else "n/a")
            + (" (TTM)" if str(r.get("pe_basis")).startswith(("trailing 12 months", "sum of last four quarters")) else " (FY)" if r.get("pe_basis") else ""),
            (f"{num(band['current'], 1)}: {position(band)}" if band else "n/a"),
            *((num(r.get("pb"), 2), f"{num(r['pb_band']['current'], 2)}: {position(r['pb_band'])}" if r.get("pb_band") else "n/a") if books else ()),
            "n/c" if r.get("growth_withheld") else pct(r["revenue_growth"]),
            "n/c" if r.get("growth_withheld") else growth_cell(r["eps_growth"], r.get("eps_change_text"))
            + (f" (≈{pct(r['eps_growth_adjusted'])} ex one-offs)" if r.get("eps_growth_adjusted") is not None else ""),
            ("n/c" if r.get("growth_withheld") else f"{pct(r.get('revenue_cagr_3y'))} / "
             + (pct(r.get("profit_cagr_3y")) if r.get("profit_cagr_3y") is not None else "n/m" if r.get("cagr_note") else "n/a")),
            f"{pct(r.get('quarter_revenue_yoy'))} / {pct(r.get('quarter_revenue_qoq'))}",
            growth_cell(r["quarter_profit_yoy"], r.get("quarter_profit_yoy_text")), pct(r["return_1y"]), pct(r["dividend_yield_ex_specials"], sign=False)]) + " |")
    return lines


def pe_window_note(rows):
    """The window each P/E history really covers: up to five years when older annual EPS came from NSE's filings,
    otherwise as far back as Yahoo's annual EPS goes (about four figures, so rarely five years)."""
    starts = {}
    for r in rows:
        if r.get("pe_band"):
            starts.setdefault(month_year(r["pe_band"]["start"]), []).append(r["symbol"])
    if not starts:
        return "P/E vs own history: not available (too little annual EPS history). "
    deep = [r["symbol"] for r in rows if (r.get("pe_band") or {}).get("median_5y") is not None or "NSE" in ((r.get("pe_band") or {}).get("basis") or "")]
    source = ("older annual EPS from NSE's annual results filings, adjusted for splits and bonuses" if len(deep) == len([r for r in rows if r.get("pe_band")])
              else "older annual EPS from NSE filings for " + ", ".join(deep) + "; otherwise as far back as Yahoo's annual EPS goes" if deep
              else "as far back as Yahoo's annual EPS goes")
    if len(starts) == 1:
        return f"P/E vs own history compares today's P/E on latest annual EPS with every trading day since {next(iter(starts))} ({source}). "
    return ("P/E vs own history compares today's P/E on latest annual EPS with every trading day since "
            + "; ".join(f"{m} ({', '.join(s)})" for m, s in starts.items()) + f" ({source}). ")


def compare_markdown(report, short=False, cautions=True):
    ok = [r for r in report["rows"] if not r.get("error")]
    dates = sorted({r["price_date"] for r in ok})
    periods = sorted({r["growth_period"] for r in ok if r.get("growth_period")})
    lines = [f"# Comparison — {', '.join(report['symbols'])}", "",
             f"Prices as of {' to '.join([dates[0], dates[-1]] if len(dates) > 1 else dates) or 'n/a'} · run {report['as_of'][:16].replace('T', ' ')} IST"
             + (f" · annual growth {', '.join(periods)}" if periods else ""), ""]
    lines += ["**Trade-offs**", ""] + [f"- {x}" for x in report["tradeoffs"]] + [""]
    lines += compare_table(report) + [""]
    lines.append(pe_window_note(ok) + "ROE and margins are the last financial year unless marked TTM; op. margin = EBITDA ÷ revenue, Screener.in's OPM "
                 "plus other income, so it can read a point or two higher; "
                 "growth is in rupees; 3-yr CAGR is a year, over the three financial years to the latest, from NSE's annual results "
                 "filings where they cover them (profit attributable to shareholders; n/m across a loss); "
                 "n/c = not comparable (demerger); n/m = not meaningful for banks.")
    converted = [r for r in ok if r.get("currency_label") and not r.get("growth_basis")]
    if converted:
        lines.append(f"{', '.join(r['symbol'] for r in converted)}: financials reported in USD, {converted[0]['currency_label']} "
                     "(each period at its own average USD/INR), so growth is rupee growth.")
    from_nse = [r["symbol"] for r in ok if r.get("currency_label") and r.get("growth_basis")]
    if from_nse:
        lines.append(f"{', '.join(from_nse)}: financials reported in USD on Yahoo and converted at each period's average USD/INR; "
                     "revenue and EPS growth come from the company's rupee results filed on NSE.")
    for r in report["rows"]:
        if r.get("error"):
            lines.append(f"⚠ {r['symbol']}: {r['error']}")
    flagged = [r for r in ok if r["flags"]] if cautions else []
    if flagged:
        lines += ["", "Cautions:"]
        for r in flagged:
            items = [x for x in r["flags"] if not x.startswith("Financials reported in")]
            if items:
                lines += [f"- {r['symbol']}: " + " ".join(items[:2 if short else 6])]
    lines += ["", report["note"], ""]
    return "\n".join(lines)


def svg_price_chart(r):
    """One-year closing price with 50/200-day averages, 52-week range and 20-day support/resistance."""
    bars = r["_bars"]
    closes = [b["close"] for b in bars]
    def avg(n):
        return [None if i + 1 < n else sum(closes[i + 1 - n:i + 1]) / n for i in range(len(closes))]
    s50, s200 = avg(50), avg(200)
    keep = min(250, len(closes))
    xs = list(range(keep))
    cl, a50, a200 = closes[-keep:], s50[-keep:], s200[-keep:]
    t = r["technical"]
    values = [v for v in cl + a50 + a200 if v is not None] + [t["support"], t["resistance"]]
    lo, hi = min(values) * 0.98, max(values) * 1.02
    w, h, left, right, top, bottom = 760, 380, 72, 110, 64, 40
    X = lambda i: left + i / max(1, keep - 1) * (w - left - right)
    Y = lambda v: h - bottom - (v - lo) / (hi - lo) * (h - top - bottom)
    def path(series):
        pts = [(X(i), Y(v)) for i, v in enumerate(series) if v is not None]
        return " ".join(f"{'M' if k == 0 else 'L'}{x:.1f},{y:.1f}" for k, (x, y) in enumerate(pts))
    def level(v, label, colour):
        return (f'<line x1="{left}" y1="{Y(v):.1f}" x2="{w - right}" y2="{Y(v):.1f}" stroke="{colour}" stroke-dasharray="5 4"/>'
                f'<text x="{w - right + 6}" y="{Y(v) + 4:.1f}" fill="{colour}">{label} {v:,.0f}</text>')
    first, last = bars[-keep]["at"][:10], bars[-1]["at"][:10]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img" aria-label="{r['name']} one-year price chart">
<g font-family="system-ui, -apple-system, 'Segoe UI', sans-serif" font-size="11">
<rect width="{w}" height="{h}" fill="#ffffff"/>
<text x="{left}" y="22" font-size="15" font-weight="600" fill="#111827">{r['name']} ({r['symbol']}) · last close ₹{t['last_close']:,.2f}</text>
<text x="{left}" y="40" font-size="12" fill="#4b5563"><tspan fill="#111827">━ close</tspan>   <tspan fill="#2563eb">━ 50-day avg</tspan>   <tspan fill="#d97706">━ 200-day avg</tspan>   · {first} to {last} · trend: {t['trend']}</text>
{level(t['resistance'], '20d high', '#15803d')}
{level(t['support'], '20d low', '#b91c1c')}
<path d="{path(a200)}" fill="none" stroke="#d97706" stroke-width="1.5"/>
<path d="{path(a50)}" fill="none" stroke="#2563eb" stroke-width="1.5"/>
<path d="{path(cl)}" fill="none" stroke="#111827" stroke-width="1.8"/>
<text x="{left - 8}" y="{Y(hi) + 10:.1f}" text-anchor="end" fill="#6b7280">{hi:,.0f}</text>
<text x="{left - 8}" y="{Y(lo):.1f}" text-anchor="end" fill="#6b7280">{lo:,.0f}</text>
<text x="{left}" y="{h - bottom + 16}" fill="#6b7280">{first}</text>
<text x="{w - right}" y="{h - bottom + 16}" text-anchor="end" fill="#6b7280">{last}</text>
</g>
</svg>
"""


def main(argv=None):
    p = argparse.ArgumentParser(description="Complete public-data analysis of one Indian stock, or a side-by-side comparison (research only).")
    p.add_argument("symbol", nargs="?", help="NSE symbol such as RELIANCE (preferred), or a Yahoo symbol such as RELIANCE.NS / RELIANCE.BO")
    p.add_argument("--compare", nargs="+", metavar="SYMBOL", help="Compare 2-8 stocks in one table, e.g. --compare TCS INFY HCLTECH")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--news-hours", type=int, default=168)
    p.add_argument("--out", help="Write the report to a new file")
    p.add_argument("--no-peers", action="store_true", help="Skip the peer comparison (faster)")
    p.add_argument("--peers", nargs="+", metavar="SYMBOL", help="Compare with these companies (same businesses) instead of index peers")
    p.add_argument("--summary", action="store_true", help="Print a short answer-first digest instead of the full report")
    p.add_argument("--chart", help="Write a one-year SVG price chart to a new file")
    a = p.parse_args(argv)
    if bool(a.symbol) == bool(a.compare):
        p.error("give one SYMBOL, or --compare with several symbols")
    try:
        if a.compare:
            report = compare(([a.symbol] if a.symbol else []) + a.compare)
            text = json.dumps(report, indent=2, ensure_ascii=False) if a.format == "json" else compare_markdown(report, short=a.summary)
            return emit(text, a.out)
        if a.chart and Path(a.chart).exists():
            raise Block("STOCK: chart file exists; choose a new path")
        report = analyse(a.symbol, news_hours=a.news_hours, peers=not a.no_peers, peer_symbols=a.peers)
        if a.chart:
            Path(a.chart).parent.mkdir(parents=True, exist_ok=True)
            Path(a.chart).write_text(svg_price_chart(report), encoding="utf-8")
            report["chart"] = a.chart
    except SymbolChanged as error:
        if a.format != "json" and a.summary:
            # A symbol that split into several companies: each successor's own digest, the explanation said once.
            text = successor_summaries(error, news_hours=a.news_hours, peers=not a.no_peers)
            if text:
                return emit("> ⚠ " + str(error).replace("STOCK: ", "", 1) + "\n\n" + text, a.out)
        elif a.format != "json":
            # Full mode: show them side by side instead of stopping (one run, not three).
            try:
                both = compare([s["symbol"] for s in error.successors])
            except Block:
                both = None
            if both and any(not r.get("error") for r in both["rows"]):
                text = ("> ⚠ " + str(error).replace("STOCK: ", "", 1) + "\n\n" + compare_markdown(both, short=a.summary)
                        + f"\nFor the full report on one of them: `stock_analysis.py {error.successors[0]['symbol']}`.\n")
                return emit(text, a.out)
        print(f"BLOCKED - {error}", file=sys.stderr)
        if a.format == "json":
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            print(json.dumps({"schema": "stock_analysis_v1", "status": "SYMBOL_CHANGED", "symbol": error.old, "kind": error.entry.get("kind"),
                              "date": error.entry.get("date"), "reason": error.entry["reason"], "successors": error.successors,
                              "source": error.entry["source"], "execution_eligible": False, "submitted": False}, indent=2, ensure_ascii=False))
        return 2
    except Block as error:
        print(f"BLOCKED - {error}", file=sys.stderr)
        return 2
    public = {k: v for k, v in report.items() if not k.startswith("_")}
    text = json.dumps(public, indent=2, ensure_ascii=False) if a.format == "json" else summary(report) if a.summary else markdown(report)
    return emit(text, a.out)


def successor_summaries(error, **kwargs):
    """The normal single-stock summary of each company an old symbol became (analysed in parallel), one after
    another, then a compact side-by-side table built from the same reports; the disclaimer once, at the end.
    None when none of them could be analysed."""
    symbols = [s["symbol"] for s in error.successors]
    if not symbols:
        return None

    def one(sym):
        try:
            return analyse(sym, **kwargs)
        except Block as failure:
            return failure
    with ThreadPoolExecutor(max_workers=min(4, len(symbols))) as pool:
        results = list(pool.map(one, symbols))
    if all(isinstance(x, Block) for x in results):
        return None
    tail = "Research only; not investment advice.\n"
    parts = [f"{sym}: not available ({str(x).replace('STOCK: ', '', 1)}).\n" if isinstance(x, Block) else summary(x).removesuffix(tail)
             for sym, x in zip(symbols, results)]
    reports = [{"symbol": sym, "error": str(x)} if isinstance(x, Block) else x for sym, x in zip(symbols, results)]
    side = compare_report(reports, symbols, instant(next(x["as_of"] for x in results if not isinstance(x, Block))))
    table = compare_markdown(side, short=True, cautions=False).replace(side["note"], "Cautions for each company are in its digest above.")
    return "\n".join(parts) + "\n" + table + "\n" + tail


def emit(text, out):
    if out:
        out = Path(out)
        if out.exists():
            print("BLOCKED - output exists; choose a new path", file=sys.stderr)
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
