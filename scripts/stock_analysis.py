"""One-stock research report for Indian equities from free public data.

Combines price/market context, technical condition, financial ratios, recent news
and a statistical outlook. Research only: no orders, no recommendation, no
price target. Every figure carries its source date so stale data stays visible.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import http.client
import json
import math
from pathlib import Path
import re
import statistics
import sys
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
                "annualOrdinarySharesNumber annualOperatingIncome annualEBITDA annualFreeCashFlow annualOperatingCashFlow "
                "annualCurrentAssets annualCurrentLiabilities annualInterestExpense annualCashAndCashEquivalents "
                "annualCashDividendsPaid annualTotalAssets trailingDilutedEPS trailingTotalRevenue trailingNetIncome "
                "quarterlyTotalRevenue quarterlyNetIncome quarterlyDilutedEPS").split()
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


def trim_for_demerger(history, selected, demergers=None):
    """Drop bars before the latest demerger record date of this company (as parent, under its current or former symbol).

    Yahoo does not adjust a demerged parent's past prices, so its series jumps on the record date (seen: TMPV
    ₹660.75 on 13 Oct 2025, ₹395.45 on 14 Oct). Returns a description of the cut, or None. Mutates `history`.
    """
    base = selected.rsplit(".", 1)[0]
    bars = history["bars"]
    first, last = bars[0]["at"][:10], bars[-1]["at"][:10]
    events = [d for d in (load_demergers() if demergers is None else demergers)
              if (d.get("parent") == base or base in (d.get("parent_aliases") or [])) and first < str(d.get("record_date")) <= last]
    if not events:
        return None
    event = max(events, key=lambda d: d["record_date"])
    keep = [b for b in bars if b["at"][:10] >= event["record_date"]]
    before = [b for b in bars if b["at"][:10] < event["record_date"]]
    move = keep[0]["close"] / before[-1]["close"] - 1 if keep and before else None
    cut = {"record_date": event["record_date"], "first_session": keep[0]["at"][:10] if keep else None, "sessions": len(keep),
           "dropped_sessions": len(before), "resulting_symbol": event.get("resulting_symbol"), "resulting_company": event.get("resulting_company"),
           "day_move": move,
           "note": (f"History starts {keep[0]['at'][:10] if keep else event['record_date']}, the first session on or after the "
                    f"{event['record_date']} record date of the demerger of {event.get('resulting_company') or event.get('resulting_symbol')}"
                    + (f"; the price moved {move * 100:+.1f}% that day because Yahoo does not adjust earlier prices" if move is not None else "")
                    + ". Returns, averages, the 52-week range, the outlook and the P/E band use only sessions from then on.")}
    history["bars"] = keep
    history["demerger_cut"] = cut
    return cut


def unexplained_gaps(bars, split_dates=(), threshold=0.35):
    """Single-session closes that fall by more than `threshold` with no split recorded that day: possible unadjusted actions."""
    out = []
    for a, b in zip(bars, bars[1:]):
        day = b["at"][:10]
        if b["close"] < a["close"] * (1 - threshold) and day not in split_dates:
            out.append(f"Close fell {(b['close'] / a['close'] - 1) * 100:.0f}% on {day} with no split recorded; if that was a demerger, "
                       "bonus or other corporate action, figures that span the date are not comparable.")
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


def daily_history(selected, *, fetcher, now, years=2):
    query = urllib.parse.urlencode({"range": f"{years}y", "interval": "1d", "includePrePost": "false", "events": "div,splits"})
    raw = fetcher("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(selected, safe="") + "?" + query)
    result = chart(raw, selected, "1d", now)
    meta = json.loads(raw)["chart"]["result"][0]["meta"]
    result["name"] = meta.get("longName") or meta.get("shortName") or selected
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


def news_request(url):
    """Google News RSS search only; same size and redirect limits as the other public fetchers."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "news.google.com" or not parsed.path.startswith("/rss/search"):
        raise Block("NEWS: unsupported source")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (IndiaDayTrader public research)", "Accept": "application/rss+xml,application/xml"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            raw = response.read(LIMIT + 1)
    except (OSError, ValueError):
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
        if "SharesNumber" not in name:
            currencies |= {r.get("currencyCode") or "INR" for r in rows}
        rows = [{"date": r["asOfDate"], "value": float(r["reportedValue"]["raw"])} for r in rows if math.isfinite(r["reportedValue"]["raw"])]
        if rows:
            series[name] = sorted(rows, key=lambda r: r["date"])
    foreign = currencies - {"INR"}
    if series and foreign:
        # Some companies (e.g. Infosys) report in USD; prices are in rupees, so convert money values at today's rate.
        if len(foreign) > 1 or len(currencies) > 1:
            raise Block("STOCK: financial statements mix currencies; ratios omitted")
        currency = foreign.pop()
        rate = fx_rate(currency, fetcher=fetcher)
        for name, rows in series.items():
            if "SharesNumber" not in name:
                series[name] = [{**r, "value": r["value"] * rate} for r in rows]
        series["_currency"] = {"reported": currency, "rate": rate, "note": f"Financials reported in {currency}; converted at today's rate ₹{rate:,.2f} per {currency}, so past figures are approximate."}
    return series


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
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        change = b - a
        gains += max(change, 0)
        losses += max(-change, 0)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


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
    special_ttm = [s for s in specials if s in ttm]
    flags = []
    if special_ttm:
        flags.append("Possible special dividend in the last 12 months: " + ", ".join(f"₹{a:,.2f} on {d.isoformat()}" for d, a in special_ttm)
                     + f"; without it the trailing yield would be about {(dps - sum(a for _, a in special_ttm)) / price * 100:.1f}%.")
    return {"dividend_yield": dps / price if price else None, "dps_ttm": dps,
            "recurring_yield": (dps - sum(a for _, a in special_ttm)) / price if price else None,
            "payments": [{"date": d.isoformat(), "amount": a} for d, a in ttm],
            "specials": [{"date": d.isoformat(), "amount": a} for d, a in special_ttm], "flags": flags,
            "basis": "dividends per share with an ex-date in the last 12 months ÷ today's price (Yahoo chart dividend events)"
                     + (f"; only dividends from {since} on" if since else "")}


def pct(value, digits=1, sign=True):
    return "n/a" if value is None else f"{value * 100:{'+' if sign else ''}.{digits}f}%"


def num(value, digits=2):
    return "n/a" if value is None else f"{value:,.{digits}f}"


def crore(value):
    return "n/a" if value is None else f"₹{value / 1e7:,.0f} cr"


def technicals(history, benchmark):
    bars = [b for b in history["bars"]]
    closes = [b["close"] for b in bars]
    if len(closes) < 60:
        raise Block("STOCK: fewer than 60 daily observations; listing too recent or feed incomplete")
    last = closes[-1]
    year = closes[-250:]
    window = bars[-250:]
    hi52, lo52 = max(b["high"] for b in window), min(b["low"] for b in window)
    daily = [math.log(b / a) for a, b in zip(closes[-251:-1], closes[-250:])]
    vol = statistics.pstdev(daily) * math.sqrt(250) if len(daily) > 20 else None
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    volumes = [b["volume"] for b in bars if b["volume"]]
    avg_vol = sum(volumes[-20:]) / len(volumes[-20:]) if len(volumes) >= 20 else None
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
    r = rsi(closes)
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
        "volume_latest": bars[-1]["volume"] or None, "volume_avg20": avg_vol,
        "support": min(b["low"] for b in bars[-20:]), "resistance": max(b["high"] for b in bars[-20:]),
        "support_60d": min(b["low"] for b in bars[-60:]), "resistance_60d": max(b["high"] for b in bars[-60:]),
        "relative": relative, "signals": signals, "trend": trend, "trend_score": score, "closes": closes,
        "close_dates": [b["at"][:10] for b in bars], "history_start": bars[0]["at"][:10], "history_sessions": len(bars),
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


def ratios(series, price, now, splits=None):
    out = {}
    # Use the most recent EPS. A stale trailing figure may predate a split/bonus that the annual series already reflects.
    candidates = [r for r in (latest(series, "trailingDilutedEPS"), latest(series, "annualDilutedEPS")) if r]
    eps = max(candidates, key=lambda r: r["date"]) if candidates else None
    if eps:
        eps = {**eps, "basis": "trailing 12 months" if eps == latest(series, "trailingDilutedEPS") else "last financial year"}
    eps, out["eps_flags"] = check_eps(eps, series.get("quarterlyDilutedEPS", []), price)
    out["split_after_statements"] = [d for d in (splits or []) if eps and d > eps["date"]]
    shares = value(series, "annualOrdinarySharesNumber")
    equity = value(series, "annualStockholdersEquity")
    eq_rows = series.get("annualStockholdersEquity", [])
    net = value(series, "annualNetIncome")
    revenue = value(series, "annualTotalRevenue")
    debt = value(series, "annualTotalDebt")
    market_cap = price * shares if shares else None
    out["market_cap"] = market_cap
    out["eps"] = eps
    out["pe"] = price / eps["value"] if eps and eps["value"] > 0 else None
    out["pb"] = price / (equity / shares) if equity and shares and equity > 0 else None
    avg_equity = (eq_rows[-1]["value"] + eq_rows[-2]["value"]) / 2 if len(eq_rows) >= 2 else equity
    out["roe"] = net / avg_equity if net is not None and avg_equity and avg_equity > 0 else None
    out["net_margin"] = net / revenue if net is not None and revenue else None
    op = value(series, "annualOperatingIncome")
    out["operating_margin"] = op / revenue if op is not None and revenue else None
    out["debt_to_equity"] = debt / equity if debt is not None and equity and equity > 0 else None
    ca, cl = value(series, "annualCurrentAssets"), value(series, "annualCurrentLiabilities")
    out["current_ratio"] = ca / cl if ca and cl else None
    interest = value(series, "annualInterestExpense")
    out["interest_cover"] = op / interest if op is not None and interest and interest > 0 else None
    fcf = value(series, "annualFreeCashFlow")
    out["fcf_yield"] = fcf / market_cap if fcf is not None and market_cap else None
    dividends = value(series, "annualCashDividendsPaid")
    # Last financial year's cash dividends paid ÷ today's market cap. Includes special dividends and can lag a year,
    # so the headline dividend_yield (trailing dividends per share ÷ price) is filled from the chart by the caller.
    out["cash_dividend_yield"] = abs(dividends) / market_cap if dividends is not None and market_cap else None
    out["dividend_yield"] = None
    rev_rows = series.get("annualTotalRevenue", [])
    out["revenue_growth"] = rev_rows[-1]["value"] / rev_rows[-2]["value"] - 1 if len(rev_rows) >= 2 and rev_rows[-2]["value"] > 0 else None
    eps_rows = series.get("annualDilutedEPS", [])
    out["eps_growth"] = eps_rows[-1]["value"] / eps_rows[-2]["value"] - 1 if len(eps_rows) >= 2 and eps_rows[-2]["value"] > 0 else None
    if out["eps_growth"] is not None and (out["eps_growth"] > 2 or out["eps_growth"] < -0.6):
        out["eps_flags"].append(f"Annual EPS changed {out['eps_growth'] * 100:+.0f}% in a year; P/E, ROE and margins may include a one-off gain or loss.")
    if len(rev_rows) >= 4 and rev_rows[-4]["value"] > 0:
        out["revenue_cagr_3y"] = (rev_rows[-1]["value"] / rev_rows[-4]["value"]) ** (1 / 3) - 1
    else:
        out["revenue_cagr_3y"] = None
    q = series.get("quarterlyNetIncome", [])
    out["latest_quarter"] = q[-1] if q else None
    out["quarter_profit_change"] = q[-1]["value"] / q[-2]["value"] - 1 if len(q) >= 2 and q[-2]["value"] > 0 else None
    out["quarter_stale"] = bool(q) and (now.date() - datetime.fromisoformat(q[-1]["date"]).date()).days > 200
    out["financials_as_of"] = latest(series, "annualTotalRevenue")["date"] if latest(series, "annualTotalRevenue") else None
    stale = eps and (now.date() - datetime.fromisoformat(eps["date"]).date()).days > (200 if eps["basis"] == "trailing 12 months" else 430)
    out["eps_stale"] = bool(stale)
    out["bank_like"] = ca is None and cl is None and value(series, "annualEBITDA") is None and revenue is not None
    out["revenue"] = revenue
    out["net_income"] = net
    return out


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
    return {
        "bias": tech["trend"],
        "ranges": ranges,
        "history_years": round(years, 1), "history_sessions": len(closes),
        "bull_case": f"A daily close above ₹{num(tech['resistance'])} (20-day high) would extend the move"
                     + "".join(f"; next reference ₹{num(v)} ({k})" for v, k in up) + ".",
        "bear_case": f"A daily close below ₹{num(tech['support'])} (20-day low) would weaken the picture"
                     + "".join(f"; next reference ₹{num(v)} ({k})" for v, k in down) + ".",
        "bull_levels": [v for v, _ in up], "bear_levels": [v for v, _ in down],
        "momentum_note": None if tech["rsi14"] is None else
            "RSI is above 70: the recent rise is stretched and short pullbacks are common from here." if tech["rsi14"] > 70 else
            "RSI is below 30: the recent fall is stretched and short rebounds are common from here." if tech["rsi14"] < 30 else None,
        "method": f"Ranges are the 10th, 50th and 90th percentiles of this stock's own overlapping past returns over the last {years:.1f} years "
                  f"({len(closes)} sessions from {tech['close_dates'][-len(closes)]}), applied to today's price"
                  + (f"; history starts {cut['first_session']}, after the demerger (record date {cut['record_date']})" if cut else "")
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
    ordered = sorted(unique.values(), key=lambda r: r["published_at"], reverse=True)
    return {"items": ordered[:12], "failures": failures, "query": term,
            "note": "Headlines only; open the story before treating it as fact. Search results can include unrelated or promotional items."}


PEER_FIELDS = ("market_cap_cr", "pe", "pb", "roe", "net_margin", "revenue_growth", "return_1y")


def peer_comparison(selected, own, *, fetcher, now, show=6, chosen=None):
    from screener import peers_for, snapshot
    if chosen:
        base = selected.rsplit(".", 1)[0]
        picked = [s.strip().upper().replace(".NS", "") for s in chosen if s.strip()]
        group = {"basis": "peers chosen for this business", "kind": "chosen",
                 "peers": [{"symbol": s} for s in dict.fromkeys(picked) if s != base][:12]}
        group["group_size"] = len(group["peers"]) + 1
    else:
        group = {**peers_for(selected, fetcher=fetcher), "kind": "index"}
    if not group["peers"]:
        return {"basis": group["basis"], "kind": group["kind"], "rows": [], "medians": {}}

    def one(member):
        try:
            return snapshot(member["symbol"], fetcher=fetcher, now=now)
        except (Block, ValueError, KeyError, TypeError, OSError):
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        loaded = list(pool.map(one, group["peers"]))
    rows = [r for r in loaded if r and r["symbol"] != selected]
    everyone = rows + [own]
    medians = {}
    for field in PEER_FIELDS:
        values = sorted(r[field] for r in everyone if r.get(field) is not None)
        if values:
            medians[field] = statistics.median(values)
    ranked = sorted(rows, key=lambda r: -(r.get("market_cap_cr") or 0))
    caps = [r["market_cap_cr"] for r in rows if r.get("market_cap_cr")]
    mismatch = bool(caps and own.get("market_cap_cr") and own["market_cap_cr"] > 3 * max(caps))
    return {"basis": group["basis"], "kind": group["kind"], "group_size": group.get("group_size"), "evaluated": len(everyone),
            "capped": group.get("capped", False), "truncated": len(ranked) > show, "rows": ranked[:show], "own": own, "medians": medians,
            "size_mismatch": mismatch, "failed": [m["symbol"] for m, r in zip(group["peers"], loaded) if not r],
            "renamed": [r["renamed_note"] for r in rows if r.get("renamed_note")]}


def prepare_history(selected, *, fetcher, now, years):
    """History for analysis: follows a known old symbol, then drops sessions before a demerger of this company.

    Returns (symbol used, history, notes) with notes {"renamed", "demerger", "gaps"}.
    """
    used, history, renamed = resolve_history(selected, fetcher=fetcher, now=now, years=years)
    cut = trim_for_demerger(history, used)
    if cut and cut["sessions"] < 60:
        raise Block(f"STOCK: only {cut['sessions']} sessions since the {cut['record_date']} demerger of "
                    f"{cut.get('resulting_company') or cut.get('resulting_symbol')}; earlier prices include the demerged business, "
                    "so there is too little comparable history for this analysis yet")
    split_days = {datetime.fromtimestamp(int(s.get("date", k)), IST).date().isoformat()
                  for k, s in ((history.get("corporate_actions") or {}).get("splits") or {}).items() if isinstance(s, dict)}
    gaps = unexplained_gaps(history["bars"], split_days)
    return used, history, {"renamed": renamed, "demerger": cut, "gaps": gaps}


def apply_dividends(fund, history, price, since=None):
    """Fill the headline dividend yield from the chart's dividend events (None when they cannot establish it)."""
    info = trailing_dividends(history, price, since=since)
    fund["dividend"] = info
    fund["dividend_yield"] = info["dividend_yield"] if info else None
    return fund


NO_HISTORY = ("STOCK: no price history for {}. Check the NSE symbol (for example RELIANCE, INFY, HDFCBANK), "
              "or the BSE ticker with .BO (for example RELIANCE.BO); the public feed may also be temporarily unavailable")


def analyse(raw_symbol, *, fetcher=request, news_fetcher=news_request, clock=instant, news_hours=168, peers=True, peer_symbols=None):
    requested = normalize(raw_symbol)
    now = clock()
    if fetcher is request:
        fetcher = retrying(request)
    try:
        selected, history, notes = prepare_history(requested, fetcher=fetcher, now=now, years=5)
    except Block as error:
        if isinstance(error, SymbolChanged) or str(error).startswith("STOCK:"):
            raise
        raise Block(NO_HISTORY.format(requested)) from None
    except (ValueError, KeyError, TypeError):
        raise Block(NO_HISTORY.format(requested)) from None
    cut = notes["demerger"]
    errors = {}
    if history.get("price_note"):
        errors["price"] = history["price_note"]
    try:
        benchmark = daily_history(BENCHMARK, fetcher=fetcher, now=now)
    except (Block, ValueError, KeyError, TypeError):
        benchmark = None
        errors["benchmark"] = "Nifty 50 history unavailable; relative strength omitted"
    try:
        tech = technicals(history, benchmark)
    except Block as error:
        if selected.endswith(".BO"):
            raise Block(f"{error}. The public BSE (.BO) history is often short; try the NSE symbol instead") from None
        raise
    try:
        series = fundamentals(selected, fetcher=fetcher, now=now)
        if not series:
            raise Block("STOCK: no financial statements returned")
        splits = sorted(datetime.fromtimestamp(int(s["date"]), IST).date().isoformat() for s in (history.get("corporate_actions") or {}).get("splits", {}).values())
        fund = ratios(series, tech["last_close"], now, splits)
        apply_dividends(fund, history, tech["last_close"], since=cut["record_date"] if cut else None)
        from screener import pe_band
        fund["currency_note"] = series.get("_currency", {}).get("note")
        if cut:
            if fund.get("eps") and fund["eps"]["date"] < cut["record_date"]:
                fund["eps_flags"].append(f"The EPS used for P/E (dated {fund['eps']['date']}) predates the {cut['record_date']} demerger and "
                                         "includes the demerged business, so the P/E is not like-for-like.")
            fund["demerger_note"] = (f"Financial statements before {cut['record_date']} include the demerged business "
                                     f"({cut.get('resulting_company') or cut.get('resulting_symbol')}); year-on-year growth across that date is not like-for-like.")
        # Today's FX rate would distort past P/E, so the history band is shown only for rupee-reported financials.
        fund["pe_band"] = None if "_currency" in series else pe_band(history["bars"], series.get("annualDilutedEPS", []),
                                                                     since=cut["record_date"] if cut else None)
    except (Block, ValueError, KeyError, TypeError):
        fund = None
        errors["fundamentals"] = "Financial statements unavailable from the public feed; ratios omitted"
    comparison = None
    if peers:
        own = {"symbol": selected, "name": history["name"], "price": tech["last_close"], "return_1y": tech["returns"]["1Y"],
               **({"market_cap_cr": fund["market_cap"] / 1e7 if fund.get("market_cap") else None,
                   **{k: fund.get(k) for k in ("pe", "pb", "roe", "net_margin", "revenue_growth")}} if fund else {})}
        try:
            comparison = peer_comparison(selected, own, fetcher=fetcher, now=now, chosen=peer_symbols)
            if not comparison["rows"]:
                errors["peers"] = f"No peer group found ({comparison['basis']})"
        except (Block, ValueError, KeyError, TypeError, OSError):
            errors["peers"] = "Peer comparison unavailable"
    headlines = gather_news(selected, history["name"], now, fetcher=fetcher, news_fetcher=news_fetcher, hours=news_hours)
    return {"schema": "stock_analysis_v1", "symbol": selected, "requested_symbol": requested, "name": history["name"], "as_of": now.isoformat(),
            "renamed_note": notes["renamed"], "history_note": cut["note"] if cut else None, "demerger": cut, "history_warnings": notes["gaps"],
            "price_note": history.get("price_note"), "session_open": session_open(now), "price_source": "Yahoo Finance daily chart (may be delayed ~15 min)",
            "fundamentals_source": "Yahoo Finance fundamentals timeseries (company filings as reported)",
            "technical": {k: v for k, v in tech.items() if k not in ("closes", "close_dates")}, "fundamentals": fund, "peers": comparison, "news": headlines,
            "_bars": history["bars"],
            "outlook": outlook(tech, cut), "errors": errors, "warning": WARNING, "disclaimer": DISCLAIMER,
            "execution_eligible": False, "submitted": False}


def markdown(r):
    t, f, o = r["technical"], r["fundamentals"], r["outlook"]
    name = r["symbol"]
    lines = [f"# {r['name']} ({name}) — stock analysis", "", f"As of {r['as_of'][:16].replace('T', ' ')} IST · prices as of {t['last_close_date']}"
             + (" (today's session is still trading; the latest price will change)" if r.get("session_open") and t["last_close_date"] == r["as_of"][:10] else ""), "",
             *([f"> ⚠ {r['price_note']}", ""] if r.get("price_note") else []),
             *([f"> ⚠ {r['renamed_note']}", ""] if r.get("renamed_note") else []),
             *([f"> ⚠ {r['history_note']}", ""] if r.get("history_note") else []),
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
    lines += ["", "| Indicator | Value |", "| --- | ---: |",
              f"| 20 / 50 / 200-day average | {num(t['sma20'])} / {num(t['sma50'])} / {num(t['sma200'])} |",
              f"| RSI (14) | {num(t['rsi14'], 1)} — {t['rsi_reading'] or 'n/a'} |",
              f"| Average daily range (ATR 14) | ₹{num(t['atr14'])} ({pct(t['atr14'] / t['last_close'] if t['atr14'] else None, sign=False)}) |",
              f"| Annualised volatility | {pct(t['annual_volatility'], 0, sign=False)} |",
              f"| Volume vs 20-day average | {num(t['volume_latest'] / t['volume_avg20'], 2) + '×' if t['volume_latest'] and t['volume_avg20'] else 'n/a'} |",
              f"| Support (20-day / 60-day low) | ₹{num(t['support'])} / ₹{num(t['support_60d'])} |",
              f"| Resistance (20-day / 60-day high) | ₹{num(t['resistance'])} / ₹{num(t['resistance_60d'])} |", ""]
    lines += ["## Financial ratios", ""]
    if f:
        eps = f["eps"]
        lines += [f"Latest annual statements: {f['financials_as_of'] or 'n/a'}" + (f"; EPS ({eps['basis']}) dated {eps['date']}" if eps else "") + ("  ⚠ EPS may be out of date" if f["eps_stale"] else ""), "",
                  "| Ratio | Value |", "| --- | ---: |",
                  f"| Market cap | {crore(f['market_cap'])} |",
                  f"| P/E ({eps['basis'] if eps else 'n/a'}) | {num(f['pe'], 1)} |", f"| P/B | {num(f['pb'], 2)} |",
                  f"| ROE | {pct(f['roe'], sign=False)} |", f"| Net margin | {pct(f['net_margin'], sign=False)} |", f"| Operating margin | {pct(f['operating_margin'], sign=False)} |",
                  f"| Debt / equity | {num(f['debt_to_equity'], 2)} |", f"| Current ratio | {num(f['current_ratio'], 2)} |",
                  f"| Interest cover | {num(f['interest_cover'], 1)}× |" if f["interest_cover"] else "| Interest cover | n/a |",
                  f"| Free-cash-flow yield | {pct(f['fcf_yield'], sign=False)} |", f"| Dividend yield (trailing 12 months per share ÷ price) | {pct(f['dividend_yield'], sign=False)} |",
                  f"| Cash dividends paid (last FY) / market cap | {pct(f.get('cash_dividend_yield'), sign=False)} |",
                  f"| Revenue growth (last year) | {pct(f['revenue_growth'])} |", f"| Revenue CAGR (3 years) | {pct(f['revenue_cagr_3y'])} |",
                  f"| EPS growth (last year) | {pct(f['eps_growth'])} |",
                  f"| Latest quarter profit vs previous quarter | {pct(f['quarter_profit_change'])}" + (f" ({f['latest_quarter']['date']}{' ⚠ stale' if f['quarter_stale'] else ''})" if f["latest_quarter"] else "") + " |", ""]
        for flag in f.get("eps_flags") or []:
            lines += [f"⚠ {flag}", ""]
        for flag in (f.get("dividend") or {}).get("flags") or []:
            lines += [f"⚠ {flag}", ""]
        if f.get("demerger_note"):
            lines += [f"⚠ {f['demerger_note']}", ""]
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
            where = "below its usual range" if band["current"] < band["p25"] else "above its usual range" if band["current"] > band["p75"] else "within its usual range"
            lines += ["### Valuation history", "",
                      f"P/E on latest annual EPS over the last {band['years']} years ({band['sessions']} sessions from {band['start']}): "
                      f"low {num(band['min'], 1)}, typical {num(band['p25'], 1)}–{num(band['p75'], 1)} "
                      f"(median {num(band['median'], 1)}), high {num(band['max'], 1)}. Today {num(band['current'], 1)} on the same basis, "
                      f"higher than {band['percentile'] * 100:.0f}% of past sessions — {where}."
                      + (f" (The P/E of {num(f['pe'], 1)} in the table uses {eps['basis']} EPS, so it is not compared with this range.)"
                         if f.get("pe") and eps and eps["basis"] != "last financial year" else ""), "",
                      f"Basis: {band['basis']}. A low P/E versus history can reflect lower expected growth, not only cheapness.", ""]
    else:
        lines += [r["errors"].get("fundamentals", "Unavailable"), ""]
    c = r.get("peers")
    if c and c["rows"]:
        cols = (("market_cap_cr", "Mkt cap ₹ cr"), ("pe", "P/E"), ("pb", "P/B"), ("roe", "ROE"), ("net_margin", "Net margin"),
                ("revenue_growth", "Revenue growth"), ("return_1y", "1Y return"))
        show = lambda k, v: "n/a" if v is None else f"{v:,.0f}" if k == "market_cap_cr" else f"{v:.1f}" if k in ("pe", "pb") else pct(v, sign=k in ("revenue_growth", "return_1y"))
        title = "## Peers" if c.get("kind") == "chosen" else "## Index peers (same NSE index, not necessarily the same business)"
        lines += [title, "", f"Group: {c['basis']} — {c.get('group_size')} stocks" + ("; the largest by market cap are shown" if c.get("truncated") else "")
                  + f"; medians use all {c.get('evaluated')} that loaded" + (" (group capped at 40)" if c.get("capped") else "") + ".", "",
                  *([f"⚠ {r['name']} is more than three times the size of every peer here, so this comparison says little about its valuation. "
                     "Compare it with companies in the same businesses instead.", ""] if c.get("size_mismatch") else []),
                  *([f"Could not load: {', '.join(c['failed'])}.", ""] if c.get("failed") else []),
                  "| Company | " + " | ".join(h for _, h in cols) + " |", "| --- |" + " ---: |" * len(cols),
                  f"| **{r['name']}** | " + " | ".join(f"**{show(k, c['own'].get(k))}**" for k, _ in cols) + " |"]
        for row in c["rows"]:
            lines.append(f"| {row['name']} | " + " | ".join(show(k, row.get(k)) for k, _ in cols) + " |")
        lines += [f"⚠ {n}" for n in c.get("renamed") or []]
        lines += ["| Group median | " + " | ".join(show(k, c["medians"].get(k)) for k, _ in cols) + " |", ""]
        if c.get("kind") != "chosen":
            lines += ["These peers come from NSE index membership, not business similarity. For a fairer comparison, rerun with `--peers` and companies in the same businesses.", ""]
    lines += ["## News (last 7 days)", ""]
    if r["news"]["items"]:
        for n in r["news"]["items"]:
            when = n["published_at"][:16].replace("T", " ")
            if "news.google.com" in n["url"]:
                lines.append(f"- {when} — {n['headline']}" + (f" · {n['outlet']}" if n.get("outlet") else ""))
            else:
                lines.append(f"- {when} — [{n['headline']}]({n['url']})" + (f" · {n['outlet']}" if n.get("outlet") else ""))
        lines += ["", r["news"]["note"] + " Links to each story are in the `--format json` output."]
    else:
        lines.append("No matching headlines found in the public feeds. That does not mean there was no news.")
    if r["news"]["failures"]:
        lines.append(f"\nUnavailable feeds: {len(r['news']['failures'])}.")
    lines += ["", "## Outlook — ranges and scenarios, not a prediction", "", f"Current bias from the technical signals: **{o['bias']}**.", "",
              "| Horizon | Low (10%) | Typical (median) | High (90%) | Past windows that ended higher |", "| --- | ---: | ---: | ---: | ---: |"]
    for label, g in o["ranges"].items():
        lines.append(f"| {label} | ₹{num(g['low_10'])} | ₹{num(g['median'])} | ₹{num(g['high_90'])} | {g['share_positive'] * 100:.0f}% of {g['samples']} |")
    lines += ["", f"- **Bull case:** {o['bull_case']}", f"- **Bear case:** {o['bear_case']}"]
    if o["momentum_note"]:
        lines.append(f"- **Momentum:** {o['momentum_note']}")
    lines += ["", o["method"], ""]
    if r["errors"]:
        lines += ["## Data gaps", ""] + [f"- {v}" for v in r["errors"].values()] + [""]
    lines.append(f"Sources: {r['price_source']}; {r['fundamentals_source']}; NSE index constituent lists; Google News, Yahoo and NSE news feeds. {r['warning']}")
    return "\n".join(lines) + "\n"


def summary(r):
    """Answer-first digest: the handful of lines most questions need, with dates and warnings."""
    t, f, o, c = r["technical"], r["fundamentals"], r["outlook"], r.get("peers")
    lines = [f"{r['name']} ({r['symbol']}) — prices as of {t['last_close_date']}"]
    if r.get("price_note"):
        lines.append(f"⚠ {r['price_note']}")
    if r.get("renamed_note"):
        lines.append(f"⚠ {r['renamed_note']}")
    if r.get("history_note"):
        lines.append(f"⚠ {r['history_note']}")
    lines.append(f"Price ₹{num(t['last_close'])} ({pct(t['day_change'], 2)} on the day); 1 year {pct(t['returns']['1Y'])} vs Nifty "
                 f"{pct(t['relative']['1Y']['nifty'])}; {pct(t['from_high'])} from the 52-week high.")
    lines.append(f"Trend: {t['trend']}; RSI {num(t['rsi14'], 0)} ({t['rsi_reading'] or 'n/a'}).")
    if f:
        band = f.get("pe_band")
        eps = f.get("eps")
        valuation = f"P/E {num(f['pe'], 1)}" + (f" ({eps['basis']})" if eps else "")
        if band:
            valuation += (f"; on latest annual EPS {num(band['current'], 1)} vs its usual {num(band['p25'], 1)}–{num(band['p75'], 1)} "
                          f"(higher than {band['percentile'] * 100:.0f}% of sessions over the last {band['years']} years)")
        lines.append(f"Valuation: {valuation}; P/B {num(f['pb'], 2)}; ROE {pct(f['roe'], sign=False)}; debt/equity {num(f['debt_to_equity'], 2)}; "
                     f"dividend yield {pct(f.get('dividend_yield'), sign=False)} (trailing 12 months).")
        lines.append(f"Growth: revenue {pct(f['revenue_growth'])} last year, EPS {pct(f['eps_growth'])}; latest quarter profit {pct(f['quarter_profit_change'])} vs previous quarter.")
        lines += [f"⚠ {x}" for x in (f.get("eps_flags") or []) + ((f.get("dividend") or {}).get("flags") or [])]
        if f.get("demerger_note"):
            lines.append(f"⚠ {f['demerger_note']}")
        if f.get("currency_note"):
            lines.append(f"⚠ {f['currency_note']}")
    if c and c.get("rows"):
        lines.append(f"{'Peers' if c.get('kind') == 'chosen' else 'Index peers'}: median P/E {num(c['medians'].get('pe'), 1)}, ROE {pct(c['medians'].get('roe'), sign=False)}"
                     + (" — ⚠ much larger than every peer, weak comparison" if c.get("size_mismatch") else "") + ".")
    month = o["ranges"].get("1 month")
    if month:
        lines.append(f"Past 1-month moves from today's price: ₹{num(month['low_10'])} to ₹{num(month['high_90'])} (10th–90th percentile, not a forecast).")
    lines.append(f"Levels: above ₹{num(t['resistance'])} extends the move; below ₹{num(t['support'])} weakens it.")
    if r["news"]["items"]:
        n = r["news"]["items"][0]
        lines.append(f"Latest headline ({n['published_at'][:10]}): {n['headline']}" + (f" · {n['outlet']}" if n.get("outlet") else ""))
    gaps = [k for k in r["errors"] if k != "price"]
    if gaps:
        lines.append("Data gaps: " + ", ".join(gaps) + ".")
    lines.append("Research only; not investment advice.")
    return "\n".join(lines) + "\n"


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
    p = argparse.ArgumentParser(description="Complete public-data analysis of one Indian stock (research only).")
    p.add_argument("symbol", help="NSE symbol such as RELIANCE (preferred), or a Yahoo symbol such as RELIANCE.NS / RELIANCE.BO")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--news-hours", type=int, default=168)
    p.add_argument("--out", help="Write the report to a new file")
    p.add_argument("--no-peers", action="store_true", help="Skip the peer comparison (faster)")
    p.add_argument("--peers", nargs="+", metavar="SYMBOL", help="Compare with these companies (same businesses) instead of index peers")
    p.add_argument("--summary", action="store_true", help="Print a short answer-first digest instead of the full report")
    p.add_argument("--chart", help="Write a one-year SVG price chart to a new file")
    a = p.parse_args(argv)
    try:
        if a.chart and Path(a.chart).exists():
            raise Block("STOCK: chart file exists; choose a new path")
        report = analyse(a.symbol, news_hours=a.news_hours, peers=not a.no_peers, peer_symbols=a.peers)
        if a.chart:
            Path(a.chart).parent.mkdir(parents=True, exist_ok=True)
            Path(a.chart).write_text(svg_price_chart(report), encoding="utf-8")
            report["chart"] = a.chart
    except SymbolChanged as error:
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
    if a.out:
        out = Path(a.out)
        if out.exists():
            print("BLOCKED - output exists; choose a new path", file=sys.stderr)
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
