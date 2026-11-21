"""F&O (futures and options) analytics for NSE underlyings. Research only; never places orders.

Data: NSE's free end-of-day F&O bhavcopy (all contracts, OI, prices, lot sizes), or a
normalized option chain the user supplies with --input (for example read from their
broker connector during market hours).

Analytics: option chain around the money, put-call ratio, max pain, signed OI build-up,
OI-based support/resistance, implied volatility and Greeks (Black-76 on a
put-call-parity forward; NSE options are European), ATM straddle expected move,
futures basis, India VIX on the chain's date, a same-expiry day-over-day comparison,
and strategy payoff at expiry and today (T+0) with breakevens, risk-neutral lognormal
probabilities (implied by option prices, not a forecast) and an SVG chart.
"""
import argparse
import csv
from datetime import date, datetime, time as dtime, timedelta
import http.client
import io
import json
import math
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request
import zipfile

from core import Block, IST, instant
import fno_margin
from public_market import NoRedirect, chart, request

BHAVCOPY = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d}_F_0000.csv.zip"
PARTICIPANT_OI = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{d}.csv"
NSE_HOLIDAYS = "https://www.nseindia.in/api/holiday-master?type=trading"
YAHOO_QUOTE = "https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=1d&interval=1m&includePrePost=false"
YAHOO_SYMBOLS = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "NIFTY_FIN_SERVICE.NS", "MIDCPNIFTY": "NIFTY_MID_SELECT.NS",
                 "NIFTYNXT50": "^NSMIDCP"}
INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
DEFAULT_RATE = 0.065
# After the close and before the evening bhavcopy: NSE's public option chain (today's OI and closing prices) and its index
# snapshot (today's closing level). Exact endpoints, served to scripts from NSE's www.nseindia.in API twin (as nse_live does).
OPTION_CHAIN_V3 = "https://www.nseindia.com/api/option-chain-v3?type={kind}&symbol={symbol}&expiry={expiry}"
OPTION_CHAIN_V3_RE = re.compile(r"^https://www\.nseindia\.com/api/option-chain-v3\?type=(?:Indices|Equity)&symbol=(?:[A-Z0-9-]|%26){1,24}"
                                r"&expiry=\d{2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{4}$")
OPTION_CHAIN_LIMIT = 8 * 1024 * 1024
NSE_ALL_INDICES = "https://www.nseindia.com/api/allIndices"
NSE_INDEX_NAMES = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FINANCIAL SERVICES",
                   "MIDCPNIFTY": "NIFTY MIDCAP SELECT", "NIFTYNXT50": "NIFTY NEXT 50"}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
BHAVCOPY_HOURS = "usually ~18:00–19:00 IST"
# STT on an in-the-money option exercised at expiry, on its intrinsic value (paid by the holder): 0.15% from 1 Apr 2026
# (Finance Act 2026; 0.125% before). The rate card's own `stt_exercise` wins; `rates_on` applies a card's `stt_before` rates
# to a data date before their end.
STT_EXERCISE = 0.0015
STT_CHANGE = "2026-04-01"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CARD = ROOT / "assets" / "charges.example.json"
SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)
SESSION_HOURS = 6.25
DISCLAIMER = ("Research from public end-of-day exchange data, not investment advice. OI, PCR and max pain describe "
              "positioning; they are not reliable predictions. Options can lose their full premium; short options can lose far more.")
MARGIN_NOTE = ("Margin is not estimated here: short options need exchange SPAN + exposure margin that only your broker's basket "
               "margin calculator gives exactly (scripts/portfolio_margin.py with your connector, or the broker's own basket tool).")


def nse_calendar_request(url):
    """NSE's public holiday master (the same endpoint india_collect.py uses); nothing else is fetched here."""
    if url != NSE_HOLIDAYS:
        raise Block("PUBLIC: unsupported source")
    req = urllib.request.Request(url, headers={"User-Agent": "IndiaDayTrader personal research", "Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            body = response.read(2 * 1024 * 1024 + 1)
    except (OSError, ValueError):
        raise Block("PUBLIC: NSE holiday list unavailable") from None
    if len(body) > 2 * 1024 * 1024:
        raise Block("PUBLIC: oversized response")
    return body


def public_fetch(url):
    """Default fetcher: the NSE holiday master and NSE Clearing SPAN files through their own allow-listed requests,
    everything else through public_market."""
    if url == NSE_HOLIDAYS:
        return nse_calendar_request(url)
    if fno_margin.SPAN_PATTERN.match(url):
        return fno_margin.span_request(url)
    if OPTION_CHAIN_V3_RE.match(url):
        return nse_chain_request(url)
    if url == NSE_ALL_INDICES:
        import nse_live  # its own allow-listed request for www.nseindia.com/api/allIndices
        return nse_live.fetch(url)
    return request(url)


def nse_chain_request(url):
    """NSE's public option chain (option-chain-v3) for one symbol and expiry; nothing else is fetched here (exact URL pattern,
    the www.nseindia.in API twin NSE serves scripts, no redirects, size cap). NSE's robots.txt allows /api/."""
    if not OPTION_CHAIN_V3_RE.match(url):
        raise Block("PUBLIC: unsupported source")
    target = url.replace("https://www.nseindia.com/api/", "https://www.nseindia.in/api/", 1)
    req = urllib.request.Request(target, headers={"User-Agent": "IndiaDayTrader personal research", "Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            body = response.read(OPTION_CHAIN_LIMIT + 1)
    except (OSError, ValueError, http.client.HTTPException):
        raise Block("PUBLIC: NSE option chain unavailable") from None
    if len(body) > OPTION_CHAIN_LIMIT:
        raise Block("PUBLIC: oversized response")
    return body


# ---------------- calendar, session and live level ----------------

def parse_holidays(raw):
    """F&O segment trading holidays from NSE's holiday master: {date: description}."""
    data = json.loads(raw)
    rows = data.get("FO")
    if not isinstance(rows, list) or not rows:
        raise Block("CALENDAR: no FO holiday list")
    out = {}
    for r in rows:
        day = datetime.strptime(r["tradingDate"], "%d-%b-%Y").date()
        out[day] = str(r.get("description") or "exchange holiday").strip()
    return out


def fetch_holidays(fetcher):
    try:
        holidays = parse_holidays(fetcher(NSE_HOLIDAYS))
        return {"days": holidays, "years": sorted({d.year for d in holidays}), "source": NSE_HOLIDAYS}
    except (Block, ValueError, KeyError, TypeError, AttributeError):
        pass
    if fetcher is not public_fetch:
        return None
    try:  # the same NSE holiday master through nse_live's own allow-listed request (www.nseindia.com)
        import nse_live
        cal = nse_live.holiday_calendar()
        days = {date.fromisoformat(d): text for d, text in cal["segments"]["FO"].items() if d not in cal.get("special_sessions", [])}
        return {"days": days, "years": cal["years"], "source": nse_live.HOLIDAYS} if days else None
    except (Block, ValueError, KeyError, TypeError, AttributeError, ImportError, OSError):
        return None


def holiday_name(holidays, day):
    if not holidays:
        return None
    return holidays["days"].get(date.fromisoformat(day) if isinstance(day, str) else day)


def is_trading_day(day, holidays):
    return day.weekday() < 5 and not (holidays and day in holidays["days"])


def next_trading_day(day, holidays):
    day += timedelta(days=1)
    while not is_trading_day(day, holidays):
        day += timedelta(days=1)
    return day


def sessions_between(start, end, holidays):
    """Trading sessions in (start, end]."""
    count, day = 0, start
    while day < end:
        day += timedelta(days=1)
        count += is_trading_day(day, holidays)
    return count


def remaining_fraction(at, holidays):
    """Share of `at`'s regular session still to trade (0 outside the session)."""
    if not is_trading_day(at.date(), holidays) or not SESSION_OPEN <= at.time() < SESSION_CLOSE:
        return 0.0
    end = datetime.combine(at.date(), SESSION_CLOSE, IST)
    return (end - at).total_seconds() / (SESSION_HOURS * 3600)


def day_text(day):
    day = date.fromisoformat(day) if isinstance(day, str) else day
    return f"{WEEKDAYS[day.weekday()][:3]} {day.day} {day:%b}"


def session_context(data_date, now, holidays, *, live_chain=False):
    """Where 'now' sits against the data: the market phase, the session after the data date and the next session not yet started."""
    data_day = date.fromisoformat(data_date)
    today = now.date()
    trading_today = is_trading_day(today, holidays)
    t = now.time()
    phase = ("holiday or weekend" if not trading_today else "pre-open" if t < SESSION_OPEN else "open" if t < SESSION_CLOSE else "closed")
    target = today if trading_today and t < SESSION_OPEN else next_trading_day(today, holidays)
    close = datetime.combine(data_day, SESSION_CLOSE, IST)
    return {"now": now.isoformat(), "today": today.isoformat(), "phase": phase, "market_open": phase == "open",
            "today_holiday": holiday_name(holidays, today), "data_date": data_date, "live_chain": live_chain,
            "next_session": next_trading_day(data_day, holidays).isoformat(), "next_unstarted_session": target.isoformat(),
            "data_age_hours": max(0.0, (now - close).total_seconds() / 3600),
            "holidays_checked": bool(holidays and today.year in holidays["years"]),
            "holiday_note": None if holidays else "NSE holiday list unavailable; only weekends are skipped when counting sessions."}


def session_status(day, ctx):
    today, phase = ctx["today"], ctx["phase"]
    if day < today or (day == today and phase == "closed"):
        return "closed"
    if day == today and phase == "open":
        return "in progress"
    if day == today:
        return "today, opens 09:15"
    return "tomorrow" if (date.fromisoformat(day) - date.fromisoformat(today)).days == 1 else "next session"


def data_age_line(ctx):
    """First line when the market is open and the report runs on end-of-day data."""
    if not ctx or ctx["live_chain"] or not ctx["market_open"]:
        return None
    return (f"**END-OF-DAY data from {day_text(ctx['data_date'])} close, {ctx['data_age_hours']:.0f} hours old; "
            f"the live chain needs your broker connector (--input).** NSE is open: today's ({day_text(ctx['today'])}) session is in progress.")


def live_level(selected, *, fetcher, now):
    """Latest index (or stock) level from Yahoo's public chart meta; None unless it is from today's session."""
    sym = YAHOO_SYMBOLS.get(selected, selected + ".NS")
    try:
        data = json.loads(fetcher(YAHOO_QUOTE.format(s=urllib.parse.quote(sym, safe=""))))
        meta = data["chart"]["result"][0]["meta"]
        price, stamp = number(meta.get("regularMarketPrice")), meta.get("regularMarketTime")
        if meta.get("symbol") != sym or meta.get("currency") != "INR" or meta.get("exchangeTimezoneName") not in {"Asia/Kolkata", "Asia/Calcutta"}:
            return None
        if not price or price <= 0 or not isinstance(stamp, (int, float)):
            return None
        at = datetime.fromtimestamp(stamp, IST)
    except (Block, ValueError, KeyError, TypeError, IndexError, OSError):
        return None
    if at.date() != now.date() or at > now + timedelta(minutes=5):
        return None
    return {"level": price, "at": at.isoformat(), "symbol": sym, "previous_close": number(meta.get("chartPreviousClose")),
            "source": f"Yahoo public quote {sym} (unofficial; may be delayed)"}


def nse_close_level(selected, *, fetcher, now):
    """Today's closing index level from NSE's allIndices snapshot once the session is over (stamped 15:30 IST or later
    today); None for a stock, before the close, or when NSE fails."""
    name = NSE_INDEX_NAMES.get(selected)
    if not name:
        return None
    try:
        import nse_live
        snap = nse_live.parse_all_indices(fetcher(NSE_ALL_INDICES), now)
        row, at = snap["indices"][name], instant(snap["as_of"])
        level = number(row["last"])
    except (Block, ValueError, KeyError, TypeError, AttributeError, OSError, ImportError):
        return None
    if at.date() != now.date() or at.time() < SESSION_CLOSE or not level or level <= 0:
        return None
    return {"level": level, "at": datetime.combine(now.date(), SESSION_CLOSE, IST).isoformat(), "close": True, "provider": "NSE",
            "previous_close": number(row.get("previous_close")), "source": f"NSE closing level ({name}, NSE allIndices)"}


def nse_vix_close(vix, *, fetcher, now):
    """After the session: today's India VIX close from NSE's allIndices in place of Yahoo's daily bar (seen stale on 18 Nov
    2026: 15.52, yesterday's close, against NSE's 15.56). `vix` unchanged when NSE has no closing value for today."""
    try:
        import nse_live
        snap = nse_live.parse_all_indices(fetcher(NSE_ALL_INDICES), now)
        level, at = number(snap["indices"]["INDIA VIX"]["last"]), instant(snap["as_of"])
    except (Block, ValueError, KeyError, TypeError, AttributeError, OSError, ImportError):
        return vix
    if not level or level <= 0 or at.date() != now.date() or at.time() < SESSION_CLOSE:
        return vix
    if vix.get("date") == now.date().isoformat() and abs(vix["vix"] - level) < 0.005:
        return vix
    note = f"NSE's close; the percentile and range use Yahoo's daily history, whose last bar still shows {fmt(vix['vix'])}"
    out = dict(vix, vix=level, date=now.date().isoformat(), exact_date=True, source="NSE allIndices", note=note)
    closes = vix.get("_closes")
    if closes:
        # Rank NSE's close, not Yahoo's stale bar, in the year's history (today's Yahoo bar, if any, is replaced by it): on
        # 18 Nov 2026 a fixture's stale 18.00 ranked 100% while NSE's 15.56 sat mid-range.
        history = list(closes[:-1] if vix.get("date") == now.date().isoformat() else closes) + [level]
        out.update(percentile_1y=sum(c <= level for c in history) / len(history), low_1y=min(history), high_1y=max(history),
                   note=f"NSE's close, ranked in Yahoo's 1-year daily history (whose last bar still shows {fmt(vix['vix'])})")
    return out


def closing_level(selected, *, fetcher, now):
    """After the session: today's close — NSE's official index close, else Yahoo's last price (stocks, or NSE failing)."""
    level = nse_close_level(selected, fetcher=fetcher, now=now)
    if level:
        return level
    y = live_level(selected, fetcher=fetcher, now=now)
    return dict(y, close=True, provider="Yahoo") if y else None


def chain_v3_url(selected, expiry):
    day = date.fromisoformat(expiry)
    return OPTION_CHAIN_V3.format(kind="Indices" if selected in INDICES else "Equity", symbol=urllib.parse.quote(selected, safe=""),
                                  expiry=f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}")


def chain_entry_stub(c):
    """True for NSE's empty placeholder entries: no underlying, no expiry and no strike of their own (seen on 19 Nov 2026 as
    the PE side of BANKNIFTY strikes 67,400 and above: strikePrice 0, underlying and expiryDate null, every figure 0)."""
    return not c.get("underlying") and not c.get("expiryDate") and not number(c.get("strikePrice"))


def parse_chain_v3(raw, selected, expiry, lot, now):
    """Contracts (bhavcopy shape, OI in shares) for one expiry from NSE's option chain, which must be stamped after today's
    15:30 close; its OI is in contracts, so it is multiplied by the lot size. Returns (contracts, stamp, underlying level).

    Tolerant of benign shape variations: empty placeholder entries (no underlying, expiry or strike) are skipped; missing
    figures count as zero (no price, no OI); an entry without its own strike takes its row's. A row for another underlying or
    expiry, or whose strike disagrees with its row, rejects the chain with that reason named."""
    data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(Block("FNO: nonfinite JSON")))
    rec = data.get("records") if isinstance(data, dict) else None
    if not isinstance(rec, dict) or not isinstance(rec.get("data"), list):
        raise Block("FNO: the NSE option chain has no records (empty or changed response)")
    try:
        at = datetime.strptime(str(rec.get("timestamp")).strip(), "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
    except ValueError:
        raise Block(f"FNO: the NSE option chain has no readable timestamp ({rec.get('timestamp')!r})") from None
    if at.date() != now.date() or at.time() < SESSION_CLOSE or at > now + timedelta(minutes=5):
        raise Block(f"FNO: the NSE option chain is not from today's close (stamped {rec.get('timestamp')})")
    spot = number(rec.get("underlyingValue"))
    if not spot or spot <= 0 or not lot:
        raise Block("FNO: the NSE option chain has no underlying level")
    want, contracts = date.fromisoformat(expiry).strftime("%d-%m-%Y"), []
    for row in rec["data"]:
        if not isinstance(row, dict):
            continue
        k = number(row.get("strikePrice"))
        for side in ("CE", "PE"):
            c = row.get(side)
            if not isinstance(c, dict) or not c or chain_entry_stub(c):
                continue
            own = number(c.get("strikePrice"))
            und, exp = c.get("underlying"), c.get("expiryDate")
            if (und and und != selected) or (exp and exp != want):
                raise Block(f"FNO: unexpected row in the NSE option chain ({und or 'no underlying'} {exp or 'no expiry'} {side} at "
                            f"strike {fmt(own or k or 0, 0)}; wanted {selected} {want})")
            strike = own or k
            if not strike or strike <= 0 or (own and k and own != k):
                raise Block(f"FNO: unexpected row in the NSE option chain ({side} strike {own or 0:g} in the row for strike {k or 0:g})")
            last, change = number(c.get("lastPrice")), number(c.get("change"))
            oi, oi_change = number(c.get("openInterest")) or 0.0, number(c.get("changeinOpenInterest")) or 0.0
            volume = number(c.get("totalTradedVolume")) or 0.0
            price = last if last and last > 0 else None
            contracts.append({"kind": side, "expiry": expiry, "strike": strike, "close": price,
                              "prev_close": last - change if price and change is not None and last - change > 0 else None,
                              "settle": last or 0.0, "oi": oi * lot, "oi_change": oi_change * lot, "volume": volume, "lot_size": lot,
                              "spot": spot, "trade_date": now.date().isoformat()})
    traded = sum(1 for c in contracts if c["close"] and c["volume"] > 0)
    if traded < 4:
        raise Block(f"FNO: too few traded strikes in the NSE option chain ({traded})")
    if not any(c["oi"] > 0 for c in contracts):
        raise Block("FNO: the NSE option chain shows no open interest")
    return contracts, at, spot


CALENDAR_KEYS = ("expiry_type", "expiry_weekday", "expiry_moved", "expiry_usual_date", "expiry_holiday", "expiry_note",
                 "usual_expiry_weekday", "has_weeklies", "selection_note", "expiry_calendar", "expiries")


def nse_chain_report(report, selected, *, fetcher, now, holidays=None, rate=DEFAULT_RATE, width=12):
    """Between the 15:30 close and the evening bhavcopy: the same expiry analysed from NSE's option chain at today's close
    (today's OI, PCR, max pain and closing prices), with the expiry calendar of the last bhavcopy.

    Returns (report, None), or (None, reason) when the chain is unavailable or rejected — the caller prints the reason, so a
    fall-back to the last bhavcopy is never silent."""
    url = chain_v3_url(selected, report["expiry"])
    try:
        raw = fetcher(url)
    except Block as error:
        return None, f"{str(error).replace('PUBLIC: ', '')}"
    except OSError:
        return None, "NSE option chain unavailable"
    try:
        contracts, at, _ = parse_chain_v3(raw, selected, report["expiry"], report["lot_size"], now)
        label = f"NSE option chain, {day_text(now.date())[4:]} 15:30 close"
        fresh = analyse_chain(contracts, selected, expiry=report["expiry"], as_of=datetime.combine(now.date(), SESSION_CLOSE, IST),
                              rate=rate, width=width, source=f"{label} ({url})", holidays=holidays)
    except Block as error:
        return None, str(error).replace("FNO: ", "")
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, StopIteration) as error:
        return None, f"the NSE option chain could not be read ({type(error).__name__}: {error})"
    if not fresh["atm_iv"]:
        return None, "no implied volatility at the money in the NSE option chain"
    for key in CALENDAR_KEYS:
        fresh[key] = report.get(key)
    fresh["nse_chain"] = {"url": url, "at": at.isoformat(), "label": label, "bhavcopy_date": report["data_date"],
                          "note": (f"Today's figures from NSE's option chain at the close (it carries today's OI); the official "
                                   f"{day_text(now.date())} bhavcopy ({BHAVCOPY_HOURS}) will confirm them; option prices here are last "
                                   "trades, which can differ from the bhavcopy's closing prices.")}
    return fresh, None


def live_vix(*, fetcher, now):
    """India VIX now and its previous close from Yahoo's public chart meta; None unless the quote is from today."""
    sym = "^INDIAVIX"
    try:
        meta = json.loads(fetcher(YAHOO_QUOTE.format(s=urllib.parse.quote(sym, safe=""))))["chart"]["result"][0]["meta"]
        level, previous, stamp = number(meta.get("regularMarketPrice")), number(meta.get("chartPreviousClose")), meta.get("regularMarketTime")
        if meta.get("symbol") != sym or not level or not previous or level <= 0 or previous <= 0 or not isinstance(stamp, (int, float)):
            return None
        at = datetime.fromtimestamp(stamp, IST)
    except (Block, ValueError, KeyError, TypeError, IndexError, OSError):
        return None
    if at.date() != now.date() or at > now + timedelta(minutes=5):
        return None
    return {"level": level, "previous_close": previous, "at": at.isoformat(), "ratio": level / previous,
            "source": "Yahoo public quote ^INDIAVIX (unofficial; may be delayed)"}


def range_item(centre, iv, sessions, *, session, status, centre_label, rest_of_today=0.0):
    if not iv or not centre or sessions <= 0:
        return None
    move = centre * iv * math.sqrt(sessions / TRADING_DAYS)
    return {"session": session, "status": status, "centre": centre, "centre_label": centre_label, "sessions": sessions,
            "rest_of_today": rest_of_today,
            "points": move, "pct": move / centre, "low": centre - move, "high": centre + move, "iv": iv}


def session_ranges(report, ctx, live, holidays):
    """Dated one-sigma ranges: the session after the data date (from that close) and, when it has already started,
    the next session not yet started (from the live level, or more sessions from the close)."""
    if not ctx:
        return []
    iv, spot, out = report["atm_iv"], report["spot"], []
    first = None
    if not ctx["live_chain"]:
        first = ctx["next_session"]
        item = range_item(spot, iv, 1, session=first, status=session_status(first, ctx), centre_label=f"the {day_text(ctx['data_date'])} close")
        if item and item["status"] != "closed":  # a range for a session that has already ended answers nothing
            out.append(item)
    target = ctx["next_unstarted_session"]
    if target != first:
        if live:
            at = instant(live["at"])
            rest = remaining_fraction(at, holidays)
            sessions = rest + sessions_between(at.date(), date.fromisoformat(target), holidays)
            label = (f"the NSE close {fmt(live['level'])} ({day_text(at.date())})" if live.get("close") and live.get("provider") == "NSE"
                     else f"today's close {fmt(live['level'])} ({live.get('provider', 'Yahoo')}, unofficial)" if live.get("close")
                     else f"{'the chain' if ctx['live_chain'] else 'live'} {fmt(live['level'])} at {at:%H:%M} IST")
            centre = live["level"]
        else:
            sessions = sessions_between(date.fromisoformat(ctx["data_date"]), date.fromisoformat(target), holidays)
            label, centre, rest = f"the {day_text(ctx['data_date'])} close (no live level)", spot, 0.0
        item = range_item(centre, iv, sessions, session=target, status=session_status(target, ctx), centre_label=label, rest_of_today=rest)
        if item:
            out.append(item)
    return out


def ranges_at_now_iv(ranges, n):
    """Ranges centred on the live level re-sized with the 'now' ATM IV (the one the summary quotes), not the close's."""
    if not (n or {}).get("available") or not n.get("atm_iv"):
        return ranges
    out = []
    for m in ranges or []:
        if m["centre_label"].startswith(("live ", "the NSE close ", "today's close ")):
            fresh = range_item(m["centre"], n["atm_iv"], m["sessions"], session=m["session"], status=m["status"],
                               centre_label=m["centre_label"], rest_of_today=m.get("rest_of_today", 0.0))
            if fresh:
                m = {**fresh, "iv_source": "now", "iv_close": m["iv"]}
        out.append(m)
    return out


def sessions_text(m):
    """Plain words for the time a range covers: 'covers the rest of today, about 5h 35m of trading, plus tomorrow';
    empty for a single session (the label already names it)."""
    full = round(m["sessions"] - m.get("rest_of_today", 0.0))
    if abs(m["sessions"] - 1) < 1e-9:
        return ""
    if m.get("rest_of_today"):
        minutes = round(m["rest_of_today"] * SESSION_HOURS * 60)
        left = f"about {minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"about {minutes}m"
        then = ("tomorrow" if full == 1 and m.get("status") == "tomorrow" else f"the {day_text(m['session'])} session" if full == 1
                else f"{full} more sessions to {day_text(m['session'])}")
        return f"covers the rest of today, {left} of trading, plus {then}"
    return f"covers {m['sessions']:.0f} trading sessions"


def range_text(m):
    d = 0 if m["high"] >= 1000 else 2
    sessions = sessions_text(m)
    when = (f"for the {day_text(m['session'])} session ({m['status']})" if abs(m["sessions"] - 1) < 1e-9
            else f"to the {day_text(m['session'])} close ({m['status']})")
    return (f"1-sigma {when}, from {m['centre_label']}: ±{fmt(m['points'], 0)} ({pct(m['pct'], 2)}): "
            f"{fmt(m['low'], d)}–{fmt(m['high'], d)}" + (f" ({sessions})" if sessions else ""))


# ---------------- data ----------------

def underlying(raw):
    text = str(raw).strip().upper().replace(".NS", "")
    text = {"BANK NIFTY": "BANKNIFTY", "NIFTY50": "NIFTY", "NIFTY 50": "NIFTY", "FIN NIFTY": "FINNIFTY"}.get(text, text)
    if not re.fullmatch(r"[A-Z0-9&_-]{1,20}", text):
        raise Block("FNO: use an NSE F&O symbol such as NIFTY, BANKNIFTY or RELIANCE")
    return text


def number(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_bhavcopy(raw, selected):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise Block("FNO: unexpected bhavcopy archive")
        reader = csv.DictReader(io.TextIOWrapper(archive.open(names[0]), encoding="utf-8"))
        rows = [r for r in reader if r.get("TckrSymb") == selected]
    if not rows:
        raise Block(f"FNO: {selected} has no F&O contracts in this file; check the symbol (F&O stocks and NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY)")
    contracts = []
    for r in rows:
        kind = r["FinInstrmTp"]
        if kind not in {"IDO", "STO", "IDF", "STF"}:
            continue
        contracts.append({
            "kind": "FUT" if kind.endswith("F") else r["OptnTp"],
            "expiry": r["XpryDt"], "strike": number(r["StrkPric"]) if kind.endswith("O") else None,
            "close": number(r["ClsPric"]), "prev_close": number(r["PrvsClsgPric"]), "settle": number(r["SttlmPric"]),
            "oi": number(r["OpnIntrst"]) or 0.0, "oi_change": number(r["ChngInOpnIntrst"]) or 0.0,
            "volume": number(r["TtlTradgVol"]) or 0.0, "lot_size": int(float(r["NewBrdLotQty"] or 0)) or None,
            "spot": number(r["UndrlygPric"]), "trade_date": r["TradDt"],
        })
    return contracts


def fetch_bhavcopy_raw(*, fetcher=request, today=None, on=None, lookback=8):
    """Raw zip of the most recent available bhavcopy on or before `on` (default today), skipping weekends and holidays."""
    day = date.fromisoformat(on) if on else (today or datetime.now(IST).date())
    tried = []
    for _ in range(lookback):
        if day.weekday() < 5:
            url = BHAVCOPY.format(d=day.strftime("%Y%m%d"))
            tried.append(day.isoformat())
            try:
                raw = fetcher(url)
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    if len([n for n in archive.namelist() if n.lower().endswith(".csv")]) == 1:
                        return raw, url
            except (Block, zipfile.BadZipFile, KeyError, ValueError, TypeError):
                pass
        day -= timedelta(days=1)
    raise Block("FNO: no NSE F&O bhavcopy found for " + ", ".join(tried) + "; it is published after market close")


def fetch_bhavcopy(selected, *, fetcher=request, today=None, on=None, lookback=8):
    """Most recent available bhavcopy on or before `on` (default today), skipping weekends and holidays."""
    raw, url = fetch_bhavcopy_raw(fetcher=fetcher, today=today, on=on, lookback=lookback)
    return parse_bhavcopy(raw, selected), url


def fetch_bhavcopy_on(day, *, fetcher, attempts=2):
    """(raw, url) of the bhavcopy for exactly `day`, tried `attempts` times (a retry for a transient failure); None if absent."""
    url = BHAVCOPY.format(d=day.strftime("%Y%m%d"))
    for _ in range(attempts):
        try:
            raw = fetcher(url)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if len([n for n in archive.namelist() if n.lower().endswith(".csv")]) == 1:
                    return raw, url
        except (Block, zipfile.BadZipFile, KeyError, ValueError, TypeError, OSError):
            pass
    return None


def index_option_expiries(raw, start, end):
    """{symbol: [expiry dates]} for NSE index options in a raw bhavcopy that expire between `start` and `end` (ISO dates,
    inclusive): contracts that close or roll inside a comparison window."""
    out = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
            text = archive.read(name).decode("utf-8-sig")
    except (zipfile.BadZipFile, StopIteration, KeyError, ValueError, TypeError, UnicodeDecodeError):
        return out
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("FinInstrmTp") == "IDO" and start <= (r.get("XpryDt") or "") <= end and (number(r.get("OpnIntrst")) or 0) > 0:
            out.setdefault(r["TckrSymb"], set()).add(r["XpryDt"])  # only expiries with open interest left to close or roll
    return {k: sorted(v) for k, v in sorted(out.items())}


def merge_expiries(*maps):
    out = {}
    for m in maps:
        for k, v in (m or {}).items():
            out.setdefault(k, set()).update(v)
    return {k: sorted(v) for k, v in sorted(out.items())}


def expiry_caveat(expiries, start, end, symbols=None):
    """'NIFTY options expired on Tue 17 Nov, inside this comparison window: …' or None. `expiries` maps symbol to dates."""
    hits = {}
    for sym, days in (expiries or {}).items():
        if symbols is not None and sym not in symbols:
            continue
        for d in days:
            if start <= d <= end:
                hits.setdefault(d, []).append(sym)
    if not hits:
        return None
    parts = [f"{', '.join(sorted(syms))} options expired on {day_text(d)}" for d, syms in sorted(hits.items())]
    return ("; ".join(parts) + f", inside the {day_text(start)}–{day_text(end)} comparison window: much of the option OI change is "
            "expiring contracts closing or rolling, not fresh positioning")


def sessions_label(sessions, holidays):
    """'2 sessions', or 'up to 2 sessions' when the NSE holiday list did not load (a skipped weekday may be a holiday)."""
    return f"{'' if holidays else 'up to '}{sessions} session{'s' if sessions != 1 else ''}"


def gap_note(kind, expected, used, sessions, holidays):
    """'Mon 16 Nov bhavcopy unavailable (tried twice); compared with Fri 13 Nov instead (2 sessions)'."""
    if not holidays:
        return (f"no {day_text(expected)} {kind} (a holiday, or the file failed twice — the NSE holiday list did not load); "
                f"compared with {day_text(used)} ({sessions_label(sessions, holidays)})")
    return (f"{day_text(expected)} {kind} unavailable (tried twice); compared with {day_text(used)} instead "
            f"({sessions_label(sessions, holidays)})")


def period_text(sessions, previous, gap=None, holidays=True):
    """'on the day' for consecutive sessions, otherwise 'since Fri 13 Nov (2 sessions)'."""
    if sessions == 1 and not gap:
        return "on the day"
    return f"since {day_text(previous)} ({sessions_label(sessions, holidays)})"


STRIKE_SPEC = re.compile(r"^\s*([0-9]{2,6}(?:\.[0-9]+)?)\s*(CE|PE|CALL|PUT|C|P)?\s*$", re.I)


def strike_spec(text):
    """'25800 CE', '25800CE', '25800 call' or '25800' -> (25800.0, 'CE'|'PE'|None); None if it is not a bare strike."""
    m = STRIKE_SPEC.match(str(text or ""))
    if not m:
        return None
    kind = (m.group(2) or "").upper()
    return float(m.group(1)), ("CE" if kind in {"CE", "CALL", "C"} else "PE" if kind in {"PE", "PUT", "P"} else None)


def infer_underlying(raw, strikes):
    """Pick the underlying for strikes given without one: the index (else stock) that lists every strike and whose
    close is nearest by moneyness (largest |ln(strike/spot)| smallest). Returns (symbol, assumption text, candidates)."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        seen = {}
        for r in csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8")):
            if r.get("FinInstrmTp") not in {"IDO", "STO"} or r.get("XpryDt", "") <= r.get("TradDt", ""):
                continue
            item = seen.setdefault(r["TckrSymb"], {"index": r["FinInstrmTp"] == "IDO", "spot": number(r["UndrlygPric"]), "strikes": set(),
                                                   "date": r["TradDt"]})
            k = number(r["StrkPric"])
            if k:
                item["strikes"].add(k)
    candidates = []
    for sym, item in seen.items():
        if item["spot"] and all(k in item["strikes"] for k in strikes):
            distance = max(abs(math.log(k / item["spot"])) for k in strikes)
            candidates.append({"symbol": sym, "spot": item["spot"], "distance": distance, "index": item["index"], "date": item["date"]})
    indices = sorted((c for c in candidates if c["index"]), key=lambda c: c["distance"])
    stocks = sorted((c for c in candidates if not c["index"]), key=lambda c: c["distance"])
    pool = [c for c in indices if c["distance"] <= 0.15] or [c for c in stocks if c["distance"] <= 0.15]
    shown = ", ".join(f"{k:,.0f}" for k in strikes)
    if not pool:
        raise Block(f"FNO: strike {shown} has no underlying within 15% of its close in the bhavcopy; name the underlying, e.g. NIFTY")
    best, others = pool[0], pool[1:4]
    text = (f"Underlying assumed: {best['symbol']} — strike {shown} is {best['distance'] * 100:.1f}% from {best['symbol']}'s "
            f"{day_text(best['date'])} close {fmt(best['spot'])}, the nearest by moneyness")
    if others:
        text += "; also listed on " + ", ".join(f"{c['symbol']} ({fmt(c['spot'], 0)}, {c['distance'] * 100:.1f}% away)" for c in others)
    return best["symbol"], text + ". Name the underlying if you meant another.", pool


def load_input(path, selected):
    """Normalized chain, e.g. built from a broker connector's option chain."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != "fno_chain_v1" or underlying(data.get("underlying", "")) != selected:
        raise Block("FNO: --input must be an fno_chain_v1 file for the requested underlying")
    spot, lot, expiry = number(data.get("spot")), data.get("lot_size"), data.get("expiry")
    if not spot or spot <= 0 or not isinstance(lot, int) or lot <= 0 or not isinstance(expiry, str):
        raise Block("FNO: input needs positive spot, integer lot_size and expiry")
    as_of = instant(data["as_of"])
    contracts = []
    for r in data.get("rows", []):
        if r.get("type") not in {"CE", "PE", "FUT"}:
            raise Block("FNO: row type must be CE, PE or FUT")
        contracts.append({"kind": r["type"], "expiry": expiry, "strike": number(r.get("strike")), "close": number(r.get("last")),
                          "prev_close": number(r.get("prev_close")), "settle": None, "oi": number(r.get("oi")) or 0.0,
                          "oi_change": number(r.get("oi_change")) or 0.0, "volume": number(r.get("volume")) or 0.0,
                          "lot_size": lot, "spot": spot, "trade_date": as_of.date().isoformat()})
    if not any(c["kind"] in {"CE", "PE"} for c in contracts):
        raise Block("FNO: input has no option rows")
    return contracts, data.get("source", "user-supplied chain"), as_of


# ---------------- maths ----------------

def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def black76(kind, forward, strike, years, vol, rate):
    disc = math.exp(-rate * years)
    if vol <= 0 or years <= 0:
        return disc * max(0.0, (forward - strike) if kind == "CE" else (strike - forward))
    sd = vol * math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if kind == "CE":
        return disc * (forward * ncdf(d1) - strike * ncdf(d2))
    return disc * (strike * ncdf(-d2) - forward * ncdf(-d1))


def implied_vol(kind, price, forward, strike, years, rate):
    if price is None or price <= 0 or years <= 0:
        return None
    floor = black76(kind, forward, strike, years, 0, rate)
    if price <= floor + 1e-6 or price >= (forward if kind == "CE" else strike):
        return None
    low, high = 1e-4, 5.0
    for _ in range(100):
        mid = (low + high) / 2
        if black76(kind, forward, strike, years, mid, rate) > price:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def greeks(kind, forward, strike, years, vol, rate):
    """Spot Greeks with spot = forward * exp(-rT). Theta per calendar day, vega per 1 vol point."""
    if not vol or years <= 0:
        return None
    spot = forward * math.exp(-rate * years)
    sd = vol * math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    disc = math.exp(-rate * years)
    gamma = npdf(d1) / (spot * sd)
    vega = spot * npdf(d1) * math.sqrt(years) / 100
    decay = -spot * npdf(d1) * vol / (2 * math.sqrt(years))
    if kind == "CE":
        return {"delta": ncdf(d1), "gamma": gamma, "vega": vega, "theta": (decay - rate * strike * disc * ncdf(d2)) / 365}
    return {"delta": ncdf(d1) - 1, "gamma": gamma, "vega": vega, "theta": (decay + rate * strike * disc * ncdf(-d2)) / 365}


def years_to_expiry(expiry, as_of):
    end = datetime.combine(date.fromisoformat(expiry), dtime(15, 30), IST)
    return max(0.0, (end - as_of).total_seconds() / (365 * 86400))


def price_of(c):
    """Traded close only; untraded contracts carry a stale previous close."""
    return c["close"] if c["volume"] > 0 and c["close"] and c["close"] > 0 else None


# ---------------- analytics ----------------

def expiries(contracts, trade_date):
    return sorted({c["expiry"] for c in contracts if c["kind"] in {"CE", "PE"} and c["expiry"] > trade_date})


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TRADING_DAYS = 252
MOVED_NOTE = "probably for a holiday — check the exchange circular"


def moved_reason(holiday):
    """The NSE holiday that moved an expiry when the holiday list names it; otherwise the cautious fallback."""
    return f"NSE trading holiday ({holiday})" if holiday else MOVED_NOTE


def last_weekday(month_key, weekday):
    """Last date in the month 'YYYY-MM' falling on `weekday` (0 = Monday)."""
    year, month = int(month_key[:4]), int(month_key[5:7])
    day = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1))
    return day - timedelta(days=(day.weekday() - weekday) % 7)


# Every NSE equity-derivative expiry (index and stock, weekly and monthly) has been on a Tuesday since 1 Sep 2025 (NSE's
# expiry-day change under SEBI's rule of one expiry day per exchange). Used only to break a tie when the listed dates fit
# two weekdays equally well (e.g. three monthlies: a holiday-moved Monday, a Tuesday and a last-Monday-that-is-moved).
NSE_EXPIRY_WEEKDAY = 1


def holiday_stand_in(day, weekday, holidays):
    """The regular expiry date that `day` stands in for when `day` is the last trading day before an NSE holiday falling on
    `weekday` (every date between them a non-trading day): e.g. Mon 23 Nov 2026 for Tue 24 Nov (Guru Nanak Jayanti). None
    without the holiday list, or when `day` is not such a stand-in."""
    if not holidays:
        return None
    d = day + timedelta(days=1)
    while (d - day).days <= 7:
        if d.weekday() == weekday:
            return d if d in holidays["days"] else None
        if is_trading_day(d, holidays):
            return None
        d += timedelta(days=1)
    return None


def expiry_calendar(contracts, trade_date, holidays=None):
    """Label each listed option expiry weekly or monthly from the data itself, and flag moved ones.

    Monthly: the expiry that a futures contract shares in that calendar month, else the last option expiry of the month.
    Every other expiry in the month is weekly; an underlying with one expiry per month has no weeklies. The usual expiry
    weekday is the one most expiries agree with (weeklies on that weekday, monthlies on its last occurrence in the month).
    With NSE's holiday list, an expiry on the last trading day before a holiday that falls on that weekday counts for it and
    is marked moved from that holiday; without the list, an expiry off the pattern is marked moved with a cautious reason.
    A tie between weekdays goes to NSE's regular expiry weekday (Tuesday since Sep 2025)."""
    listed = expiries(contracts, trade_date)
    futures = {c["expiry"] for c in contracts if c["kind"] == "FUT" and c["expiry"] > trade_date}
    by_month = {}
    for e in listed:
        by_month.setdefault(e[:7], []).append(e)
    monthly = set()
    for month, items in by_month.items():
        shared = sorted(f for f in futures if f in items)
        monthly.add(shared[-1] if shared else items[-1])
    has_weeklies = any(len(items) > 1 for items in by_month.values())

    def regular(e, w):
        """The regular date on weekday `w` for listed expiry `e`, if `e` is on it: the date itself or the holiday it stands in for."""
        day = date.fromisoformat(e)
        target = last_weekday(e[:7], w) if e in monthly else None
        if (day == target) if e in monthly else day.weekday() == w:
            return day
        stand_in = holiday_stand_in(day, w, holidays)
        if stand_in and (e not in monthly or stand_in == target):
            return stand_in
        return None

    score = lambda w: sum(regular(e, w) is not None for e in listed)
    usual = max(range(5), key=lambda w: (score(w), w == NSE_EXPIRY_WEEKDAY, -w)) if listed else None
    if usual is not None and score(usual) == 0:
        usual = None  # nothing follows a pattern (e.g. a single, moved expiry without the holiday list): do not guess
    rows = []
    for e in listed:
        day = date.fromisoformat(e)
        kind = "monthly" if e in monthly else "weekly"
        normal = regular(e, usual) if usual is not None else None
        moved = usual is not None and normal != day
        if moved and normal is None:
            normal = last_weekday(e[:7], usual) if kind == "monthly" else day + timedelta(days=(usual - day.weekday()) % 7)
            if kind == "weekly" and normal - day > timedelta(days=3):
                normal = day - timedelta(days=(day.weekday() - usual) % 7)
        rows.append({"expiry": e, "type": kind, "weekday": WEEKDAYS[day.weekday()], "moved": moved,
                     "usual_date": normal.isoformat() if moved else None})
    return {"expiries": rows, "has_weeklies": has_weeklies, "usual_weekday": WEEKDAYS[usual] if usual is not None else None}


def expiry_text(item, usual_weekday):
    """'monthly, Monday — moved from Tuesday 2026-11-24, probably for a holiday — check the exchange circular'."""
    if not item:
        return "type unknown"
    text = f"{item['type']}, {item['weekday']}"
    if item["moved"]:
        text += f" — moved from {usual_weekday} {item['usual_date']}, {moved_reason(item.get('holiday'))}"
    return text


def one_day_move(spot, iv):
    """One-sigma move over the next trading session from the ATM IV: spot × IV × √(1/252).

    The IV is annualised on calendar time; dividing its variance over 252 trading days spreads weekend and holiday
    variance across sessions, the usual convention for a next-session range."""
    if not iv or not spot:
        return None
    move = spot * iv * math.sqrt(1 / TRADING_DAYS)
    return {"points": move, "pct": move / spot, "low": spot - move, "high": spot + move, "iv": iv,
            "basis": f"ATM IV × √(1/{TRADING_DAYS}), one trading session; about 68% of outcomes if the IV is right — {PROB_LABEL}"}


def max_pain(strikes, calls, puts):
    best = None
    for s in strikes:
        pay = sum(c["oi"] * max(0.0, s - k) for k, c in calls.items()) + sum(p["oi"] * max(0.0, k - s) for k, p in puts.items())
        if best is None or pay < best[1]:
            best = (s, pay)
    return best[0] if best else None


def buildup(c):
    if c["prev_close"] is None or c["close"] is None or c["volume"] <= 0 or not c["oi_change"]:
        return None
    up = c["close"] > c["prev_close"]
    if c["oi_change"] > 0:
        return "long build-up" if up else "short build-up"
    return "short covering" if up else "long unwinding"


def implied_forward(calls, puts, spot, years, rate):
    """Put-call parity at the most-traded strikes near spot: F = K + e^{rT}(C - P)."""
    pairs = []
    for k in sorted(set(calls) & set(puts), key=lambda k: abs(k - spot))[:6]:
        c, p = price_of(calls[k]), price_of(puts[k])
        if c and p:
            pairs.append(k + math.exp(rate * years) * (c - p))
    if not pairs:
        return spot * math.exp(rate * years), "spot carried at the assumed rate (no traded call/put pair near the money)"
    pairs.sort()
    return pairs[len(pairs) // 2], "put-call parity at traded strikes near the money"


def analyse_chain(contracts, selected, *, expiry=None, kind=None, as_of=None, rate=DEFAULT_RATE, width=12, source="NSE F&O bhavcopy",
                  holidays=None):
    trade_date = contracts[0]["trade_date"]
    as_of = as_of or datetime.combine(date.fromisoformat(trade_date), dtime(15, 30), IST)
    live = contracts[0]["settle"] is None  # --input chains carry one expiry and may be read on expiry day itself
    available = sorted({c["expiry"] for c in contracts if c["kind"] in {"CE", "PE"}}) if live else expiries(contracts, trade_date)
    if not available:
        raise Block("FNO: no live option expiries in the data")
    calendar = None if live else expiry_calendar(contracts, trade_date, holidays)
    if calendar and holidays:
        for x in calendar["expiries"]:
            if x["moved"] and x["usual_date"]:
                x["holiday"] = holiday_name(holidays, x["usual_date"])
    selection_note = None
    if kind:
        if calendar is None:
            raise Block("FNO: --weekly/--monthly need the exchange bhavcopy; an --input chain carries one expiry")
        typed = [x["expiry"] for x in calendar["expiries"] if x["type"] == kind]
        if not typed:
            monthly = next(x["expiry"] for x in calendar["expiries"] if x["type"] == "monthly")
            selection_note = (f"{INDEX_NAMES[selected]} has had no weekly options since Nov 2024; using the {day_text(monthly)[4:]} monthly "
                              f"({monthly}) — NSE lists one expiry a month for it." if selected in WEEKLIES_ENDED else
                              f"{selected} has no weekly expiries — NSE lists one expiry a month for it (weeklies on indices other than "
                              f"NIFTY stopped in Nov 2024) — so this uses the nearest monthly, {monthly}, instead.")
            typed = [monthly]
        if expiry and expiry != typed[0]:
            raise Block("FNO: use --expiry or --weekly/--monthly, not both")
        expiry = typed[0]
    chosen = expiry or available[0]
    if chosen not in available:
        raise Block(f"FNO: expiry {chosen} not available; choose from {', '.join(available[:8])}")
    info = next((x for x in calendar["expiries"] if x["expiry"] == chosen), None) if calendar else None
    if info:
        expiry_note = f"Expiry {chosen} is a {expiry_text(info, calendar['usual_weekday'])}."
        if not calendar["has_weeklies"]:
            expiry_note += f" {selected} has no weekly expiries in this data (one expiry per month)."
    else:
        expiry_note = "Single-expiry input chain: weekly or monthly cannot be told from the data."
    opts = [c for c in contracts if c["expiry"] == chosen and c["kind"] in {"CE", "PE"} and c["strike"]]
    calls = {c["strike"]: c for c in opts if c["kind"] == "CE"}
    puts = {c["strike"]: c for c in opts if c["kind"] == "PE"}
    spot = next(c["spot"] for c in contracts if c["spot"])
    lot = next((c["lot_size"] for c in opts if c["lot_size"]), None)
    years = years_to_expiry(chosen, as_of)
    forward, forward_basis = implied_forward(calls, puts, spot, years, rate)
    strikes = sorted(set(calls) | set(puts))
    atm = min(strikes, key=lambda k: abs(k - forward))

    rows = []
    for k in strikes:
        row = {"strike": k}
        for side, book in (("ce", calls), ("pe", puts)):
            c = book.get(k)
            if not c:
                continue
            p = price_of(c)
            iv = implied_vol(c["kind"], p, forward, k, years, rate) if p else None
            row[side] = {"close": p, "stale_close": None if p else c["close"], "oi": c["oi"], "oi_change": c["oi_change"],
                         "volume": c["volume"], "iv": iv, "buildup": buildup(c),
                         "greeks": greeks(c["kind"], forward, k, years, iv, rate) if iv else None}
        rows.append(row)

    call_oi, put_oi = sum(c["oi"] for c in calls.values()), sum(p["oi"] for p in puts.values())
    call_vol, put_vol = sum(c["volume"] for c in calls.values()), sum(p["volume"] for p in puts.values())
    above = [k for k in calls if k >= spot]
    below = [k for k in puts if k <= spot]
    resistance = sorted(above, key=lambda k: calls[k]["oi"], reverse=True)[:3]
    support = sorted(below, key=lambda k: puts[k]["oi"], reverse=True)[:3]
    atm_call, atm_put = price_of(calls.get(atm, {"close": None, "volume": 0})), price_of(puts.get(atm, {"close": None, "volume": 0}))
    straddle = atm_call + atm_put if atm_call and atm_put else None
    # ATM IV from the out-of-the-money side at the ATM strike (average when both exist).
    atm_ivs = [r[s]["iv"] for r in rows if r["strike"] == atm for s in ("ce", "pe") if s in r and r[s]["iv"]]
    atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else None
    otm = lambda k, side: (side == "ce" and k > forward) or (side == "pe" and k < forward)
    skew = None
    if atm_iv:
        put_wing = [r["pe"]["iv"] for r in rows if "pe" in r and r["pe"]["iv"] and 0.93 * forward <= r["strike"] <= 0.97 * forward]
        call_wing = [r["ce"]["iv"] for r in rows if "ce" in r and r["ce"]["iv"] and 1.03 * forward <= r["strike"] <= 1.07 * forward]
        if put_wing and call_wing:
            skew = sum(put_wing) / len(put_wing) - sum(call_wing) / len(call_wing)

    futures = [c for c in contracts if c["kind"] == "FUT"]
    future = next((f for f in futures if f["expiry"] == chosen), None) or (min(futures, key=lambda f: f["expiry"]) if futures else None)
    fut = None
    if future and future["close"]:
        fut = {"expiry": future["expiry"], "price": future["close"], "basis": future["close"] - spot,
               "basis_pct": future["close"] / spot - 1, "oi": future["oi"], "oi_change": future["oi_change"], "buildup": buildup(future)}

    # Walls near spot: the largest OI inside the to-expiry 1-SD range (±3% of spot when there is no IV, or a side of the range
    # holds no strike) — the global maxima are often far out of the money and useless as next-session levels.
    near = near_walls(calls, puts, spot, one_sd_range(forward, atm_iv, years))
    pain = max_pain(strikes, calls, puts)
    window = sorted(strikes, key=lambda k: abs(k - atm))[:2 * width + 1]
    return {
        "schema": "fno_chain_report_v1", "underlying": selected, "source": source, "data_date": trade_date, "as_of": as_of.isoformat(),
        "expiry": chosen, "expiries": available[:8], "days_to_expiry": round(years * 365, 2), "spot": spot, "lot_size": lot,
        "expiry_type": info["type"] if info else None, "expiry_weekday": WEEKDAYS[date.fromisoformat(chosen).weekday()],
        "expiry_moved": bool(info and info["moved"]), "expiry_usual_date": info["usual_date"] if info else None,
        "expiry_holiday": info.get("holiday") if info else None, "selection_note": selection_note,
        "expiry_note": expiry_note, "has_weeklies": calendar["has_weeklies"] if calendar else None,
        "usual_expiry_weekday": calendar["usual_weekday"] if calendar else None,
        "expiry_calendar": calendar["expiries"][:8] if calendar else None,
        "forward": forward, "forward_basis": forward_basis, "rate_assumption": rate, "atm_strike": atm,
        "pcr_oi": put_oi / call_oi if call_oi else None, "pcr_volume": put_vol / call_vol if call_vol else None,
        "call_oi": call_oi, "put_oi": put_oi, "call_oi_change": sum(c["oi_change"] for c in calls.values()),
        "put_oi_change": sum(p["oi_change"] for p in puts.values()),
        "max_pain": pain, "max_pain_vs_spot": pain / spot - 1 if pain and spot else None,
        "resistance_oi": resistance, "support_oi": support, "oi_walls_near": near,
        "atm_iv": atm_iv, "skew_put_minus_call": skew, "straddle": straddle,
        "expected_move": straddle, "expected_move_iv": spot * atm_iv * math.sqrt(years) if atm_iv else None,
        "range_1sd": one_sd_range(forward, atm_iv, years), "one_day_move": one_day_move(spot, atm_iv),
        "call_oi_breakdown": oi_breakdown(calls), "put_oi_breakdown": oi_breakdown(puts), "future": fut,
        "chain": [r for r in rows if r["strike"] in window], "otm_note": "IV uses traded closes only; untraded strikes show no IV.",
        "disclaimer": DISCLAIMER, "execution_eligible": False, "submitted": False,
        "_books": (calls, puts), "_years": years, "_live": live, "_rows": rows,
        "_smile": fit_smile(rows, forward, years, atm_iv),
    }


def near_walls(calls, puts, centre, rng, basis="1-SD range to expiry"):
    """The two largest-OI call strikes above `centre` and put strikes below it inside `rng` (±3% of `centre` when there is
    no range, or a side of it holds no strike)."""
    lo, hi = rng if rng else (centre * 0.97, centre * 1.03)
    above = [k for k in calls if k >= centre]
    below = [k for k in puts if k <= centre]
    near_calls = [k for k in above if k <= hi] or [k for k in above if k <= centre * 1.03]
    near_puts = [k for k in below if k >= lo] or [k for k in below if k >= centre * 0.97]
    return {"calls": sorted(near_calls, key=lambda k: calls[k]["oi"], reverse=True)[:2],
            "puts": sorted(near_puts, key=lambda k: puts[k]["oi"], reverse=True)[:2],
            "window": [lo, hi], "centre": centre, "basis": basis if rng else "±3% of spot"}


def oi_breakdown(book):
    """Signed OI change by build-up type, in units; added + reduced + unclassified = net = the side's total OI change."""
    parts = {name: 0.0 for name in ("long build-up", "short build-up", "short covering", "long unwinding")}
    unclassified = 0.0
    for c in book.values():
        kind = buildup(c)
        if kind:
            parts[kind] += c["oi_change"]
        else:
            unclassified += c["oi_change"]
    added = {k: parts[k] for k in ("long build-up", "short build-up")}
    reduced = {k: parts[k] for k in ("short covering", "long unwinding")}
    return {"unit": "shares (divide by lot_size for contracts)", "added": added, "added_total": sum(added.values()),
            "reduced": reduced, "reduced_total": sum(reduced.values()),
            "unclassified": unclassified, "unclassified_note": "OI change on contracts that did not trade or had no previous close",
            "net": sum(added.values()) + sum(reduced.values()) + unclassified}


# ---------------- risk-neutral distribution ----------------

def prob_above(level, forward, vol, years):
    """P(S_T > level) for a lognormal on the forward: ln S_T ~ N(ln F - s^2/2, s^2), s = vol * sqrt(T)."""
    if level <= 0:
        return 1.0
    sd = vol * math.sqrt(years)
    return ncdf((math.log(forward / level) - 0.5 * sd * sd) / sd)


def one_sd_range(forward, vol, years):
    """Plus/minus one standard deviation of log price around the lognormal median; holds about 68.3% of outcomes."""
    if not vol or years <= 0:
        return None
    sd = vol * math.sqrt(years)
    median = forward * math.exp(-0.5 * sd * sd)
    return [median * math.exp(-sd), median * math.exp(sd)]


def fit_smile(rows, forward, years, atm_iv, max_sd=4.0):
    """Least-squares quadratic smile σ(x) = a + b·x + c·x², x = ln(K/F), through the out-of-the-money traded IVs
    (puts below the forward, calls above) within ±max_sd ATM standard deviations. Flat beyond the fitted strikes.
    None when there are too few strikes on either side, in which case probabilities use the flat ATM IV."""
    if not atm_iv or years <= 0:
        return None
    limit = max_sd * atm_iv * math.sqrt(years)
    pts = []
    for r in rows:
        k = r["strike"]
        first, second = ("pe", "ce") if k < forward else ("ce", "pe")
        iv = (r.get(first) or {}).get("iv") or (r.get(second) or {}).get("iv")
        x = math.log(k / forward)
        if iv and abs(x) <= limit:
            pts.append((x, iv))
    if len(pts) < 4 or not any(x < 0 for x, _ in pts) or not any(x > 0 for x, _ in pts):
        return None
    # Normal equations for [a, b, c].
    s = [sum(x ** n for x, _ in pts) for n in range(5)]
    t = [sum(v * x ** n for x, v in pts) for n in range(3)]
    m = [[s[0], s[1], s[2], t[0]], [s[1], s[2], s[3], t[1]], [s[2], s[3], s[4], t[2]]]
    for i in range(3):
        pivot = max(range(i, 3), key=lambda j: abs(m[j][i]))
        m[i], m[pivot] = m[pivot], m[i]
        if abs(m[i][i]) < 1e-18:
            return None
        for j in range(3):
            if j != i:
                f = m[j][i] / m[i][i]
                m[j] = [a - f * b for a, b in zip(m[j], m[i])]
    a, b, c = (m[i][3] / m[i][i] for i in range(3))
    xmin, xmax = min(x for x, _ in pts), max(x for x, _ in pts)
    if min(a + b * x + c * x * x for x in (xmin, xmax, min(max(-b / (2 * c), xmin), xmax) if c else xmin)) <= 0.01:
        return None
    rms = math.sqrt(sum((a + b * x + c * x * x - v) ** 2 for x, v in pts) / len(pts))
    return {"a": a, "b": b, "c": c, "xmin": xmin, "xmax": xmax, "forward": forward, "points": len(pts), "rms": rms,
            "basis": "quadratic in log-moneyness through each traded out-of-the-money strike's own IV"}


def smile_vol(smile, level):
    x = min(max(math.log(level / smile["forward"]), smile["xmin"]), smile["xmax"])
    return smile["a"] + smile["b"] * x + smile["c"] * x * x


def smile_slope(smile, level):
    """dσ/dK at `level`; zero on the flat extrapolation beyond the fitted strikes."""
    x = math.log(level / smile["forward"])
    if not smile["xmin"] <= x <= smile["xmax"]:
        return 0.0
    return (smile["b"] + 2 * smile["c"] * x) / level


def prob_above_smile(level, forward, years, smile):
    """Risk-neutral P(S_T > K) consistent with the smile: minus the strike derivative of the undiscounted Black-76 call
    priced at σ(K), i.e. N(d2) − F·φ(d1)·√T·σ'(K). With a flat smile it is N(d2), the lognormal figure."""
    if level <= 0:
        return 1.0
    vol = smile_vol(smile, level)
    sd = vol * math.sqrt(years)
    d1 = (math.log(forward / level) + 0.5 * sd * sd) / sd
    p = ncdf(d1 - sd) - forward * npdf(d1) * math.sqrt(years) * smile_slope(smile, level)
    return min(1.0, max(0.0, p))


def profit_probability(value, knots, forward, vol, years, eps=1e-9, above=None):
    """P(value(S_T) > 0) for a payoff that is linear between knots (strikes); exact integration segment by segment.
    `above(level)` gives P(S_T > level); the default is the single-volatility lognormal."""
    above = above or (lambda level: prob_above(level, forward, vol, years))
    points = sorted({0.0} | {k for k in knots if k and k > 0})
    total = 0.0
    for a, b in zip(points, points[1:] + [None]):
        va = value(a)
        if b is None:  # beyond the last strike the payoff is a straight line
            slope = value(a + 1.0) - va
            if va > eps:
                lo, hi = a, (a - va / slope if slope < -eps else None)
            elif slope > eps:
                lo, hi = a - va / slope, None
            else:
                continue
        else:
            vb = value(b)
            if va > eps and vb > eps:
                lo, hi = a, b
            elif va > eps:
                lo, hi = a, a + (b - a) * va / (va - vb)
            elif vb > eps:
                lo, hi = a + (b - a) * va / (va - vb), b
            else:
                continue
        total += above(lo) - (above(hi) if hi is not None else 0.0)
    return min(1.0, max(0.0, total))


def vix_context(*, fetcher=request, now, on=None):
    """India VIX close on the chain's trading date `on` (default: latest bar), from the public 1-year daily history.

    If that exact date is missing, the latest close before it is used and the result says so. The percentile and
    range cover the history up to and including the bar used."""
    query = "range=1y&interval=1d&includePrePost=false"
    raw = fetcher("https://query1.finance.yahoo.com/v8/finance/chart/%5EINDIAVIX?" + query)
    bars = chart(raw, "^INDIAVIX", "1d", now)["bars"]
    if on:
        usable = [b for b in bars if b["at"][:10] <= on]
        if not usable:
            raise Block(f"FNO: no India VIX close on or before {on} in the 1-year history")
    else:
        usable = bars
    bar = usable[-1]
    closes = [b["close"] for b in usable]
    exact = on is None or bar["at"][:10] == on
    note = None if exact else f"India VIX close for {on} is missing from the history; showing the previous close ({bar['at'][:10]})."
    return {"vix": bar["close"], "date": bar["at"][:10], "requested_date": on, "exact_date": exact, "note": note,
            "percentile_1y": sum(c <= bar["close"] for c in closes) / len(closes), "low_1y": min(closes), "high_1y": max(closes),
            "_closes": closes}


# ---------------- participant-wise OI ----------------

PARTICIPANTS = ("FII", "DII", "Pro", "Client")
PARTICIPANT_COLUMNS = {"fut_long": "Future Index Long", "fut_short": "Future Index Short", "call_long": "Option Index Call Long",
                       "call_short": "Option Index Call Short", "put_long": "Option Index Put Long", "put_short": "Option Index Put Short"}


def parse_participant_oi(raw, expected=None):
    """NSE's daily participant-wise OI (contracts) — index futures and index options long/short for Client, DII, FII, Pro."""
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    lines = text.splitlines()
    stamp = re.search(r"as on ([A-Z][a-z]{2} \d{1,2}, \d{4})", lines[0] if lines else "")
    as_on = datetime.strptime(stamp.group(1), "%b %d, %Y").date().isoformat() if stamp else None
    if expected and as_on != expected:
        raise Block(f"FNO: participant OI file is for {as_on}, not {expected}")
    start = next((i for i, l in enumerate(lines) if l.strip().lower().startswith("client type")), None)
    if start is None:
        raise Block("FNO: unexpected participant OI file")
    rows = {}
    for r in csv.DictReader(lines[start:]):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        who = r.get("Client Type")
        if who in PARTICIPANTS or who == "TOTAL":
            rows[who] = {key: int(float(r[col])) for key, col in PARTICIPANT_COLUMNS.items()}
    if set(PARTICIPANTS) - set(rows) or "TOTAL" not in rows:
        raise Block("FNO: participant OI file lacks a participant row")
    total = rows["TOTAL"]
    # NSE's own rows are occasionally off by a contract or two (seen: 16 Nov 2026 index call shorts sum to TOTAL - 1).
    close = lambda x, y: abs(x - y) <= max(10, 1e-4 * max(abs(x), abs(y)))
    if (not close(total["fut_long"], total["fut_short"]) or not close(total["call_long"], total["call_short"])
            or not close(total["put_long"], total["put_short"])
            or not all(close(sum(rows[w][k] for w in PARTICIPANTS), total[k]) for k in PARTICIPANT_COLUMNS)):
        raise Block("FNO: participant OI totals do not balance")
    for row in rows.values():
        row["fut_net"] = row["fut_long"] - row["fut_short"]
        row["fut_long_pct"] = row["fut_long"] / (row["fut_long"] + row["fut_short"]) if row["fut_long"] + row["fut_short"] else None
        row["call_net"] = row["call_long"] - row["call_short"]
        row["put_net"] = row["put_long"] - row["put_short"]
    return {"date": as_on, "rows": rows}


def fetch_participant_oi(data_date, *, fetcher, holidays=None, previous=None, lookback=6):
    """Participant OI on the data date and the previous trading day (from the NSE holiday calendar; `previous` overrides),
    with the change. A missing previous file is retried once; failing that the comparison walks back to the last file
    found and says how many sessions it spans (never 'on the day' for more than one)."""
    def one(day):
        return parse_participant_oi(fetcher(PARTICIPANT_OI.format(d=day.strftime("%d%m%Y"))), day.isoformat())

    def attempt(day, tries):
        for _ in range(tries):
            try:
                return one(day)
            except (Block, ValueError, KeyError, TypeError, IndexError, UnicodeDecodeError, OSError):
                pass
        return None
    day = date.fromisoformat(data_date)
    current = attempt(day, 2)
    if current is None:
        return {"status": "unavailable", "note": f"NSE participant-wise OI for {data_date} unavailable."}
    expected = date.fromisoformat(previous) if previous else previous_trading_day(day, holidays)
    prior = attempt(expected, 2)
    probe = expected - timedelta(days=1)
    for _ in range(0 if previous or prior else lookback):
        if is_trading_day(probe, holidays):
            prior = attempt(probe, 1)
            if prior:
                break
        probe -= timedelta(days=1)
    for who, row in current["rows"].items():
        before = prior["rows"].get(who) if prior else None
        row["change"] = {k: row[k] - before[k] for k in ("fut_long", "fut_short", "fut_net", "call_net", "put_net")} if before else None
    used = date.fromisoformat(prior["date"]) if prior else None
    sessions = sessions_between(used, day, holidays) if used else None
    gap = gap_note("participant OI file", expected, used, sessions, holidays) if used and used != expected else None
    if prior is None:
        gap = f"{day_text(expected)} participant OI file unavailable (tried twice); no change shown"
    return {"status": "ok", "date": current["date"], "previous_date": prior["date"] if prior else None, "rows": current["rows"],
            "expected_previous_date": expected.isoformat(), "sessions": sessions, "sessions_certain": bool(holidays), "gap_note": gap,
            "source": PARTICIPANT_OI.format(d=day.strftime("%d%m%Y")), "unit": "contracts",
            "scope": "all NSE index futures and index options (every index and expiry combined), not this underlying alone",
            "note": ("Index options are counted in contracts, not delta-weighted. FIIs often hold index futures short as a hedge "
                     "against cash-market holdings, so a net short is not by itself a bearish bet.")}


def signed_int(v):
    return "n/a" if v is None else f"{v:+,.0f}"


def net_text(net, change, noun="", period="on the day", before_text="the day before"):
    """'net short 116,830 calls (down 69,819 on the day)': the side, the size and how it moved, in plain words.
    `period`/`before_text` name a longer comparison ('since Fri 13 Nov (2 sessions)', 'on Fri 13 Nov')."""
    side = lambda v: "net long" if v > 0 else "net short" if v < 0 else "flat"
    text = f"{side(net)} {abs(net):,}{noun}" if net else f"no net position{' in' + noun if noun else ''}"
    if change is None:
        return text
    before = net - change
    if change == 0:
        return text + f" (unchanged {period})"
    if not net or not before or (net > 0) != (before > 0):
        return text + f" (was {side(before)} {abs(before):,} {before_text})" if before else text + f" (was flat {before_text})"
    return text + f" ({'up' if abs(net) > abs(before) else 'down'} {abs(change):,} {period})"


def participant_period(po):
    """('on the day', 'the day before') or ('since Fri 13 Nov (2 sessions)', 'on Fri 13 Nov') for a participant OI result."""
    prev = po.get("previous_date")
    if not prev:
        return "on the day", "the day before"
    sessions = po.get("sessions") or 1
    if sessions == 1 and not po.get("gap_note"):
        return "on the day", "the day before"
    return period_text(sessions, prev, po.get("gap_note"), po.get("sessions_certain", True)), f"on {day_text(prev)}"


def participant_summary_line(po, shown=()):
    if not po or po.get("status") != "ok":
        return f"Participant OI: {po['note']}" if po else None
    rows, prev = po["rows"], po["previous_date"]
    ch = lambda who, key: rows[who]["change"][key] if rows[who].get("change") else None
    period, before = participant_period(po)
    nt = lambda net, change, noun="": net_text(net, change, noun, period, before)
    return (f"Who holds index F&O (NSE participant OI, all indices and expiries, {day_text(po['date'])}"
            f"{' vs ' + day_text(prev) if prev else ''}; contracts): "
            f"FIIs are {pct(rows['FII']['fut_long_pct'], 0)} long in index futures, {nt(rows['FII']['fut_net'], ch('FII', 'fut_net'))} · "
            f"clients (retail and other non-institutional) {nt(rows['Client']['fut_net'], ch('Client', 'fut_net'))} in index futures · "
            f"FIIs in index options: {nt(rows['FII']['call_net'], ch('FII', 'call_net'), ' calls')}, "
            f"{nt(rows['FII']['put_net'], ch('FII', 'put_net'), ' puts')}"
            + ("" if not po.get("expiry_caveat") else " (⚠ the same expiry caveat as above applies)" if po["expiry_caveat"] in shown
               else f" (⚠ {po['expiry_caveat']})") + (f"; {po['pending_note']}" if po.get("pending_note") else ""))


def participant_section(po):
    if not po:
        return []
    if po.get("status") != "ok":
        return ["## Participant-wise OI", "", po["note"], ""]
    prev = po["previous_date"]
    head = f"Δ vs {prev}" if prev else "Δ (previous day unavailable)"
    if prev and ((po.get("sessions") or 1) > 1 or po.get("gap_note")):
        head += f", {sessions_label(po['sessions'], po.get('sessions_certain', True))}"
    notes = [f"⚠ {x}." for x in (po.get("gap_note"), po.get("expiry_caveat")) if x]
    lines = ["## Participant-wise OI (NSE, all index futures and options)", "",
             f"Contracts on {po['date']}; {head}. Scope: {po['scope']}.", "", *[n + "\n" for n in notes],
             f"| Participant | Index fut long | Index fut short | Long % | Fut net ({head}) | Index calls net ({head}) | Index puts net ({head}) |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for who in PARTICIPANTS:
        r = po["rows"][who]
        c = r.get("change") or {}
        both = lambda key: f"{signed_int(r[key])} ({signed_int(c.get(key))})" if c else signed_int(r[key])
        lines.append(f"| {who} | {r['fut_long']:,} | {r['fut_short']:,} | {pct(r['fut_long_pct'], 0)} | {both('fut_net')} | "
                     f"{both('call_net')} | {both('put_net')} |")
    return lines + ["", po["note"], ""]


# ---------------- day over day ----------------

COMPARE_KEYS = ("spot", "pcr_oi", "max_pain", "atm_iv", "call_oi", "put_oi")


def day_over_day(current, previous, previous_source=None):
    """Same-expiry comparison of two analysed chains (previous trading day vs the report's data date)."""
    metrics = {}
    for key in COMPARE_KEYS:
        before, now = previous[key], current[key]
        metrics[key] = {"previous": before, "current": now, "change": None if before is None or now is None else now - before}
    note = ("Current values come from the supplied chain; previous values are end of day." if current.get("_live") else
            f"Current values from {current['nse_chain']['label']}; previous values from the NSE end-of-day bhavcopy." if current.get("nse_chain")
            else "Both days are NSE end-of-day bhavcopies.")
    return {"status": "ok", "expiry": current["expiry"], "previous_date": previous["data_date"], "current_date": current["data_date"],
            "previous_source": previous_source, "metrics": metrics, "note": note}


def compare_previous(report, selected, *, fetcher=request, on=None, holidays=None, preloaded=None):
    """Compare the same expiry with the previous trading day's bhavcopy. The previous day comes from the NSE holiday
    calendar (weekends only when it did not load); its file is tried twice. If it is still missing, the comparison uses
    the last earlier file and says so — the session count and the missing date go into the result (`sessions`, `gap_note`)."""
    data_day = date.fromisoformat(report["data_date"])
    gap = None
    if on:
        try:
            target = date.fromisoformat(on)
        except ValueError:
            raise Block("FNO: --previous-date must be YYYY-MM-DD") from None
        if target >= data_day:
            raise Block(f"FNO: --previous-date must be before the data date {report['data_date']}")
        try:
            raw, url = fetch_bhavcopy_raw(fetcher=fetcher, on=target.isoformat())
        except Block as error:
            return {"status": "unavailable", "note": f"Bhavcopy on or before {on} unavailable ({error}); day-over-day comparison skipped."}
        expected = target
    else:
        expected = previous_trading_day(data_day, holidays)
        got = (preloaded[0], preloaded[1]) if preloaded and preloaded[2] == expected.isoformat() else fetch_bhavcopy_on(expected, fetcher=fetcher)
        if got is None:
            try:
                got = fetch_bhavcopy_raw(fetcher=fetcher, on=(expected - timedelta(days=1)).isoformat(), lookback=7)
            except Block:
                return {"status": "unavailable", "expected_previous_date": expected.isoformat(),
                        "note": f"The {day_text(expected)} bhavcopy (previous trading day) is unavailable after a retry, and no earlier "
                                "file was found; day-over-day comparison skipped."}
        raw, url = got
    contracts = parse_bhavcopy(raw, selected)
    used = date.fromisoformat(contracts[0]["trade_date"]) if contracts else expected
    sessions = sessions_between(used, data_day, holidays)
    if not on and used != expected:
        gap = gap_note("bhavcopy", expected, used, sessions, holidays)
    window = index_option_expiries(raw, used.isoformat(), report["data_date"])
    try:
        previous = analyse_chain(contracts, selected, expiry=report["expiry"], rate=report["rate_assumption"])
    except (Block, IndexError):
        return {"status": "unavailable", "expected_previous_date": expected.isoformat(), "gap_note": gap,
                "note": f"Expiry {report['expiry']} was not listed in the {used.isoformat()} bhavcopy; day-over-day comparison skipped."}
    out = day_over_day(report, previous, "NSE F&O bhavcopy " + url)
    out.update(expected_previous_date=expected.isoformat(), sessions=sessions, sessions_certain=bool(holidays), gap_note=gap,
               window_expiries=window)
    return out


# ---------------- strategies ----------------

LEG = re.compile(r"^(BUY|SELL):(CE|PE|FUT):([0-9.]*|ATM(?:[+-][0-9.]+(?:PTS|PT|P)?){0,2}):([0-9]+)(?::([0-9.]+))?$")
RELATIVE = re.compile(r"[+-][0-9.]+(?:PTS|PT|P)?")


def parse_leg(text):
    m = LEG.match(text.strip().upper().replace(" ", ""))
    if not m:
        raise Block("FNO: leg must look like BUY:CE:25500:1 (side:type:strike:lots[:premium]); the strike can be relative to the "
                    "at-the-money strike: ATM, ATM-1 (strike steps), ATM-1-500 (one step below, then 500 points) or ATM+200PTS; "
                    "futures use BUY:FUT::1")
    side, kind, strike, lots, premium = m.groups()
    if kind != "FUT" and not strike:
        raise Block("FNO: option legs need a strike")
    if kind == "FUT" and strike:
        raise Block("FNO: futures legs take no strike (BUY:FUT::1)")
    if int(lots) < 1:
        raise Block("FNO: lots must be at least 1")
    relative = strike if strike.startswith("ATM") else None
    if relative:
        try:
            relative_offset(relative, 1.0)
        except ValueError:
            raise Block(f"FNO: could not read the relative strike {relative}; use ATM, ATM-1, ATM-1-500 or ATM+200PTS") from None
    return {"side": side, "type": kind, "strike": float(strike) if strike and not relative else None, "lots": int(lots),
            "premium": float(premium) if premium else None, **({"relative": relative} if relative else {})}


def relative_offset(spec, step):
    """Points from the ATM strike for 'ATM', 'ATM-1' (strike steps), 'ATM-1-500' (a step count, then points) or 'ATM+200PTS'
    (points): the first unsuffixed number counts strike steps, a later one or any number ending in P/PT/PTS counts points."""
    total, terms = 0.0, RELATIVE.findall(spec[3:])
    if "".join(terms) != spec[3:]:
        raise ValueError(spec)
    for i, term in enumerate(terms):
        points = term.endswith("P") or term.endswith("PT") or term.endswith("PTS") or i > 0
        value = float(term.rstrip("PTS"))
        total += value if points else value * step
    return total


def atm_reference(report):
    """(ATM strike, level, label): the listed strike nearest the level a trader sees — the live or closing level the report
    quotes now, else the chain's spot (the last close) — among strikes listed on both sides."""
    calls, puts = report["_books"]
    strikes = sorted(set(calls) & set(puts)) or sorted(set(calls) | set(puts))
    live = report.get("live") or {}
    if live.get("level") and not report.get("_live") and str(live.get("at", ""))[:10] > report["data_date"]:
        level = live["level"]
        label = (f"the close {fmt(level)} ({day_text(live['at'][:10])})" if live.get("close")
                 else f"live {fmt(level)} at {live['at'][11:16]} IST")
    else:
        level = report["spot"]
        label = f"spot {fmt(level)}" + ("" if report.get("_live") else f" (the {day_text(report['data_date'])} close)")
    return min(strikes, key=lambda k: (abs(k - level), k)), level, label


def resolve_relative(report, legs):
    """Fill in the strike of each ATM-relative leg from the chain's ATM strike and strike step; returns (legs, note) where
    the note lists what each relative strike resolved to (None when no leg is relative)."""
    if not any(l.get("relative") for l in legs):
        return legs, None
    atm, level, label = atm_reference(report)
    step = strike_step(report)
    if not step:
        raise Block("FNO: could not work out the strike step for ATM-relative strikes; give the strikes as numbers")
    calls, puts = report["_books"]
    out, done = [], []
    for l in legs:
        if not l.get("relative"):
            out.append(l)
            continue
        target = atm + relative_offset(l["relative"], step)
        book = calls if l["type"] == "CE" else puts
        if target not in book:
            near = sorted(book, key=lambda k: abs(k - target))[:3]
            raise Block(f"FNO: {l['relative']} resolves to {fmt(target, 0)} {l['type']} (ATM {fmt(atm, 0)}, step {fmt(step, 0)}), which "
                        f"is not listed for {report['expiry']}; nearest listed: {', '.join(fmt(k, 0) for k in sorted(near))}. A plain "
                        "number after ATM counts strike steps; write points as ATM-500PTS or ATM-1-500")
        out.append({**l, "strike": target})
        done.append(f"{l['relative']} → {fmt(target, 0)} {l['type']}")
    note = (f"ATM {fmt(atm, 0)} = the strike nearest {label}; strike step {fmt(step, 0 if step == int(step) else 2)}: "
            + ", ".join(done))
    return out, note




PROB_LABEL = "implied by option prices, not a forecast"
SD_LEVELS = "the lognormal median (forward × e^(−σ²T/2)) × e^(±k·σ√T)"


def nice_tick(x):
    """Largest 1, 2 or 5 x 10^n not above x; used to round table prices."""
    if x <= 0:
        return 0.01
    base = 10 ** math.floor(math.log10(x))
    return max(m * base for m in (1, 2, 5) if m * base <= x)


def table_prices(low, high, keys, rows=18):
    """About `rows` prices: evenly spaced across [low, high] (rounded) plus every key price, sorted and deduplicated."""
    keys = sorted({round(k, 2) for k in keys})
    n = max(8, rows - len(keys))
    step = (high - low) / (n - 1)
    tick = nice_tick(step / 4)
    even = {round(round((low + step * i) / tick) * tick, 2) for i in range(n)}
    even = {p for p in even if all(abs(p - k) > step * 0.25 for k in keys)}
    return sorted(even | set(keys))


def leg_iv(report, leg, years):
    """IV of the leg's contract from its traded close; falls back to the IV implied by the leg's own premium."""
    calls, puts = report["_books"]
    book = calls if leg["type"] == "CE" else puts
    for price in (price_of(book[leg["strike"]]), leg["price"]):
        iv = implied_vol(leg["type"], price, report["forward"], leg["strike"], years, report["rate_assumption"]) if price else None
        if iv:
            return iv
    return None


def load_charge_card(path=DEFAULT_CARD):
    """The dated rate card shared with position_size.py; None if missing or without NFO segments."""
    try:
        card = json.loads(Path(path).read_text(encoding="utf-8"))
        if {"NFO:option", "NFO:future"} <= set(card.get("segments", {})) and "option_brokerage" in card:
            return card
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def rates_on(card, day):
    """The rate card as it applied on `day` (ISO): before a card's `stt_before.effective_until` (31 Mar 2026 on the example
    card) the older STT rates replace the current ones; otherwise the card unchanged."""
    before = (card or {}).get("stt_before")
    if not card or not before or not day or str(day) > before.get("effective_until", ""):
        return card
    segments = {k: dict(v, stt_sell=before.get(k, v["stt_sell"])) for k, v in card["segments"].items()}
    return {**card, "segments": segments, "stt_exercise": before.get("exercise", card.get("stt_exercise", STT_EXERCISE)),
            "stt_applied": f"STT rates before {STT_CHANGE} (the data date {day} is before the Finance Act 2026 change)"}


def trade_charges(card, kind, side, price, qty):
    """Charges in ₹ for one order: kind CE/PE/FUT, side BUY/SELL, price per unit, qty units (positive).
    Options: flat brokerage per order, STT on sell premium, exchange + SEBI on premium, stamp on buy, GST on
    brokerage + exchange + SEBI. Futures: percentage brokerage with a cap, same pattern on contract value."""
    seg = card["segments"]["NFO:future" if kind == "FUT" else "NFO:option"]
    value = max(0.0, price) * qty
    brokerage = min(value * card["brokerage_rate"], card["brokerage_cap"]) if kind == "FUT" else card["option_brokerage"]
    stt = value * seg["stt_sell"] if side == "SELL" else 0.0
    stamp = value * seg["stamp_buy"] if side == "BUY" else 0.0
    exchange, sebi = value * seg["exchange_rate"], value * card["sebi_rate"]
    gst = (brokerage + exchange + sebi) * card["gst_rate"]
    return {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi, "stamp": stamp, "gst": gst,
            "total": brokerage + stt + exchange + sebi + stamp + gst}


def exit_side(leg):
    return "SELL" if leg["units"] > 0 else "BUY"


def intrinsic(leg, s):
    if leg["type"] == "FUT":
        return s
    return max(0.0, s - leg["strike"]) if leg["type"] == "CE" else max(0.0, leg["strike"] - s)


def payoff(report, legs, *, span=0.08, points=81, table_rows=18, card="default"):
    calls, puts = report["_books"]
    lot = report["lot_size"]
    if not lot:
        raise Block("FNO: lot size unavailable")
    card = load_charge_card() if card == "default" else card
    years = report.get("_years", report["days_to_expiry"] / 365)
    forward, rate, spot = report["forward"], report["rate_assumption"], report["spot"]
    resolved = []
    for leg in legs:
        if leg["type"] == "FUT":
            price = leg["premium"] or (report["future"]["price"] if report["future"] else forward)
        else:
            book = calls if leg["type"] == "CE" else puts
            if leg["strike"] not in book:
                raise Block(f"FNO: strike {leg['strike']:g} {leg['type']} is not listed for {report['expiry']}")
            price = leg["premium"] or price_of(book[leg["strike"]])
            if price is None:
                raise Block(f"FNO: {leg['strike']:g} {leg['type']} did not trade; pass a premium like BUY:{leg['type']}:{leg['strike']:g}:{leg['lots']}:PRICE")
        units = leg["lots"] * lot * (1 if leg["side"] == "BUY" else -1)
        item = {**leg, "price": price, "units": units, "iv": None}
        if leg["type"] != "FUT":
            item["iv"] = leg_iv(report, item, years)
        resolved.append(item)

    def value(s):
        total = 0.0
        for leg in resolved:
            if leg["type"] == "FUT":
                intrinsic = s
            elif leg["type"] == "CE":
                intrinsic = max(0.0, s - leg["strike"])
            else:
                intrinsic = max(0.0, leg["strike"] - s)
            total += leg["units"] * (intrinsic - leg["price"])
        return total

    future_now = report["future"]["price"] if report["future"] else forward
    priced = years <= 0 or all(l["iv"] for l in resolved if l["type"] != "FUT")

    def today(s):
        """Mark-to-market P&L now if the underlying were at s: Black-76 per leg at its own IV and the current time to
        expiry, with the forward (and any future) moved in proportion to the underlying."""
        return mark_value(report, resolved, s, years)

    # Costs: entry at the prices used; exit by squaring off every leg at its expiry value.
    if card:
        entry = [{"leg": leg_name(l), **trade_charges(card, l["type"], l["side"], l["price"], abs(l["units"]))} for l in resolved]
        entry_total = sum(c["total"] for c in entry)

        def leg_exit(l, s):
            # Squaring off at the expiry value, or — for an in-the-money long option left to exercise — the exercise STT on its
            # intrinsic value, whichever costs more (the worst case at expiry).
            off = trade_charges(card, l["type"], exit_side(l), intrinsic(l, s), abs(l["units"]))["total"]
            if l["type"] != "FUT" and l["units"] > 0 and intrinsic(l, s) > 0:
                return max(off, intrinsic(l, s) * abs(l["units"]) * card.get("stt_exercise", STT_EXERCISE))
            return off

        def exit_cost(s):
            return sum(leg_exit(l, s) for l in resolved)

        def net_value(s):
            return value(s) - entry_total - exit_cost(s)
    else:
        entry, entry_total, exit_cost, net_value = [], 0.0, None, None

    centre = spot
    strikes = [l["strike"] for l in resolved if l["strike"]]
    low = min([centre * (1 - span)] + [k * 0.97 for k in strikes])
    high = max([centre * (1 + span)] + [k * 1.03 for k in strikes])
    grid = sorted(set([low + (high - low) * i / (points - 1) for i in range(points)] + strikes + [centre]))
    curve = [(s, value(s)) for s in grid]
    today_curve = [(s, today(s)) for s in grid] if priced else None
    breakevens = crossings(curve)
    net_curve = [(s, net_value(s)) for s in grid] if net_value else None
    net_breakevens = crossings(net_curve) if net_curve else []
    # Upside is unbounded when the payoff still slopes at the top; downside ends where the underlying reaches zero.
    slope_high = value(high + 1) - value(high)
    at_zero = value(0.0)
    best = max([v for _, v in curve] + [at_zero])
    worst = min([v for _, v in curve] + [at_zero])
    tol = 1e-6 * max(1.0, abs(worst), abs(best))
    max_profit = None if slope_high > 1e-9 else best
    max_loss = None if slope_high < -1e-9 else worst
    net_premium = -sum(l["units"] * l["price"] for l in resolved if l["type"] != "FUT")
    after = None
    if net_value:
        zone = lambda target: [s for s, v in curve + [(0.0, at_zero)] if abs(v - target) <= tol]
        after = {"net_premium": net_premium - entry_total,
                 "max_profit": None if max_profit is None else max(net_value(s) for s in zone(best)),
                 "max_loss": None if max_loss is None else max(net_value(s) for s in zone(worst)),
                 "breakevens": net_breakevens}
    net = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in resolved:
        if leg["type"] == "FUT":
            net["delta"] += leg["units"]
            continue
        g = greeks(leg["type"], forward, leg["strike"], years, leg["iv"], rate) if leg["iv"] else None
        if g:
            for k in net:
                net[k] += g[k] * leg["units"]
        else:
            net = None
            break

    vol, vol_basis = report["atm_iv"], "ATM IV"
    if not vol:
        leg_ivs = [l["iv"] for l in resolved if l["iv"]]
        vol, vol_basis = (sum(leg_ivs) / len(leg_ivs), "average of the legs' IVs (ATM IV unavailable)") if leg_ivs else (None, None)
    smile = report.get("_smile")
    if vol and years > 0:
        flat = lambda level: prob_above(level, forward, vol, years)
        skewed = (lambda level: prob_above_smile(level, forward, years, smile)) if smile else flat
        floor = lambda above: (None if max_loss is None else
                               1 - profit_probability(lambda s: value(s) - worst - tol, strikes, forward, vol, years, above=above))
        probabilities = {
            "available": True, "label": PROB_LABEL, "vol": vol, "vol_basis": vol_basis, "forward": forward, "years": years,
            "model": "risk-neutral lognormal on the forward at a single volatility; ignores skew, costs and early exit",
            "profit": profit_probability(value, strikes, forward, vol, years),
            "profit_skew": profit_probability(value, strikes, forward, vol, years, above=skewed),
            "profit_after_costs": profit_probability(net_value, strikes, forward, vol, years, above=skewed) if net_value else None,
            "profit_after_costs_flat": profit_probability(net_value, strikes, forward, vol, years) if net_value else None,
            "max_loss_chance": floor(skewed), "max_loss_chance_flat": floor(flat),
            "skew_used": bool(smile),
            "skew_basis": (f"each strike's own IV: {smile['basis']} ({smile['points']} strikes, fit error {smile['rms'] * 100:.2f} vol pts), "
                           "including how IV changes across strikes" if smile else
                           "flat ATM IV (too few traded strikes either side to fit the smile)"),
            "above_breakevens": [{"breakeven": b, "prob_above": flat(b), "prob_above_skew": skewed(b)} for b in breakevens],
            "above_breakevens_after_costs": [{"breakeven": b, "prob_above": skewed(b), "prob_above_flat": flat(b)} for b in net_breakevens],
            "range_1sd": one_sd_range(forward, vol, years)}
    else:
        probabilities = {"available": False, "label": PROB_LABEL,
                         "note": "No implied volatility or no time left to expiry; probabilities not computed."}

    expected = expected_value(report, resolved, years, card)
    if expected and net_value:
        expected["after_costs"] = expected["before_costs"] - entry_total - expected["exit_costs"]
    scenarios = sd_scenarios(report, resolved, value, net_value, vol, years, priced, entry_total)
    if after and after["max_loss"] is not None:
        # Exit charges (mainly STT on an in-the-money long leg) grow as the move deepens past the long strike, so the maximum
        # loss after costs is the worst after-cost value at every expiry level shown — the payoff range and the ±3 SD rows —
        # never smaller than any of those rows.
        shown = [(s, net_value(s)) for s, _ in curve] + [(x["level"], x["pnl_after_costs"]) for x in (scenarios or {}).get("expiry", [])]
        level, worst_net = min(shown, key=lambda t: t[1])
        after["max_loss_first"] = after["max_loss"]
        if worst_net < after["max_loss"]:
            after["max_loss"], after["max_loss_level"] = worst_net, level

    def marks(s):
        found = []
        if any(abs(s - round(k, 2)) < 1e-6 for k in strikes):
            found.append("strike")
        if any(abs(s - round(b, 2)) < 1e-6 for b in breakevens):
            found.append("breakeven")
        if any(abs(s - round(b, 2)) < 1e-6 for b in net_breakevens):
            found.append("breakeven after costs")
        if abs(s - round(spot, 2)) < 1e-6:
            found.append("spot")
        return found

    table = [{"price": s, "expiry_pnl": value(s), "expiry_pnl_after_costs": net_value(s) if net_value else None,
              "today_pnl": today(s) if priced else None, "marks": marks(s)}
             for s in table_prices(low, high, strikes + breakevens + net_breakevens + [spot], table_rows)]
    costs = ({"available": True, "entry": entry, "entry_total": entry_total, "exit_at_spot": exit_cost(spot),
              "round_trip_at_spot": entry_total + exit_cost(spot),
              "card": {k: card.get(k) for k in ("broker", "source", "verified_on", "status")},
              "basis": (f"NFO option charges from the rate card (flat brokerage per order, STT {card['segments']['NFO:option']['stt_sell'] * 100:g}% "
                        "on sell premium, exchange and SEBI fees, stamp duty on buys, GST); entry at the prices used, exit by squaring off "
                        "every leg at its expiry value. A leg left to expire worthless saves its exit brokerage; an in-the-money option "
                        f"left to exercise pays exercise STT ({card.get('stt_exercise', STT_EXERCISE) * 100:g}% of intrinsic value"
                        f"{'' if card.get('stt_applied') else ', rates from 1 Apr 2026 under the Finance Act 2026'}) "
                        "instead (the dearer of the two is used). Maximum loss after costs is the worst after-cost value at any expiry "
                        "level shown (the payoff range and ±3 SD), so it includes the STT on in-the-money legs; an even deeper move "
                        "adds a little more. Slippage is not included.")} if card else
             {"available": False, "note": "Rate card unavailable; figures exclude charges."})
    return {"legs": [{k: v for k, v in l.items() if k != "premium"} for l in resolved], "lot_size": lot,
            "net_premium": net_premium, "max_profit": max_profit,
            "max_loss": max_loss, "breakevens": breakevens, "after_costs": after, "costs": costs,
            "expected_value": expected, "scenarios": scenarios, "margin_note": MARGIN_NOTE,
            "extreme_at_zero": {"profit": at_zero > max(v for _, v in curve) + tol, "loss": at_zero < min(v for _, v in curve) - tol},
            "curve": curve, "today_curve": today_curve, "table": table, "position_greeks": net, "probabilities": probabilities,
            "today_note": ("Today (T+0): mark-to-market P&L if the underlying moved there now; Black-76 per leg at its implied volatility "
                           "(held constant) and the current time to expiry." if today_curve else
                           "Today (T+0) value unavailable: at least one option leg has no implied volatility."),
            "costs_note": ("Gross columns exclude charges; 'after costs' columns include the rate-card charges described above. "
                           "Slippage excluded. " + MARGIN_NOTE) if card else
                          "Excludes brokerage, STT, exchange charges, GST, stamp duty and slippage; margin for short legs comes from your broker (basket margin)."}


def crossings(curve):
    out = []
    for (a, va), (b, vb) in zip(curve, curve[1:]):
        if va == 0:
            out.append(a)
        elif va * vb < 0:
            out.append(a + (b - a) * va / (va - vb))
    return out


def mark_value(report, legs, s, years, bump=0.0, spot=None):
    """Mark-to-market P&L of resolved legs if the underlying were at s with `years` left and IVs shifted by `bump`."""
    if years <= 0:
        return sum(l["units"] * (intrinsic(l, s) - l["price"]) for l in legs)
    forward, rate = report["forward"], report["rate_assumption"]
    spot = spot or report["spot"]
    future_now = report["future"]["price"] if report["future"] else forward
    scale = s / spot
    total = 0.0
    for leg in legs:
        if leg["type"] == "FUT":
            now = future_now * scale
        else:
            now = black76(leg["type"], forward * scale, leg["strike"], years, max(0.01, leg["iv"] + bump), rate)
        total += leg["units"] * (now - leg["price"])
    return total


def expected_value(report, legs, years, card):
    """Risk-neutral expected P&L at expiry if held: each option's expected payoff is its Black-76 value at its own IV
    (that is, its market price) grown at the rate, so before costs it is only the interest on the premium."""
    if years <= 0 or any(l["type"] != "FUT" and not l["iv"] for l in legs):
        return None
    forward, rate = report["forward"], report["rate_assumption"]
    disc = math.exp(-rate * years)
    gross, exits = 0.0, 0.0
    for l in legs:
        mean = forward if l["type"] == "FUT" else black76(l["type"], forward, l["strike"], years, l["iv"], rate) / disc
        gross += l["units"] * (mean - l["price"])
        if card:
            exits += trade_charges(card, l["type"], exit_side(l), mean, abs(l["units"]))["total"]
    return {"before_costs": gross, "exit_costs": exits, "after_costs": None,
            "basis": "risk-neutral expectation at implied prices: option prices are fair under their own IVs, so the expected "
                     "value is about zero before costs and negative after them; any edge has to come from a view that differs from the market"}


def sd_scenarios(report, legs, value, net_value, vol, years, priced, entry_total):
    """P&L at expiry at the forward ±1/2/3 SD (ATM IV) and an overnight gap of ±3 one-session SD marked next morning."""
    if not vol or years <= 0:
        return None
    forward, spot = report["forward"], report["spot"]
    sd = vol * math.sqrt(years)
    median = forward * math.exp(-0.5 * sd * sd)  # the same centre as one_sd_range, so ±1 SD here is the 68% range's ends
    level = lambda k: median * math.exp(k * sd)
    expiry = [{"sd": k, "level": level(k), "pnl": value(level(k)),
               "pnl_after_costs": net_value(level(k)) if net_value else None} for k in (-3, -2, -1, 1, 2, 3)]
    gaps = []
    if priced:
        g = vol / math.sqrt(TRADING_DAYS)
        later = max(0.0, years - 1 / 365)
        for label, k, bump in (("gap up 3 one-session SD, IVs unchanged", 3, 0.0), ("gap down 3 one-session SD, IVs unchanged", -3, 0.0),
                               ("gap down 3 one-session SD, IVs +5 vol pts", -3, 0.05)):
            level = spot * math.exp(k * g)
            gaps.append({"label": label, "level": level, "move_pct": level / spot - 1, "iv_bump": bump,
                         "pnl": mark_value(report, legs, level, later, bump) - entry_total})
    return {"expiry": expiry, "gaps": gaps, "sd_points": forward * sd,
            "basis": f"expiry levels = {SD_LEVELS} at {pct(vol)}, so ±1 SD are the ends of the 68% range; gaps marked at the next "
                     "morning (one day less to expiry), "
                     "after entry costs, before exit costs"}


# ---------------- if placed now (model re-price at the live level) ----------------

TICK = 0.05


def reprice_report(report, live, *, vix=None, holidays=None, close_report=None, iv_from=None):
    """The chain re-priced for a trade placed now: every traded option of the report's expiry valued with Black-76 at the
    live underlying level, its own IV from the end-of-day close (scaled by India VIX's move today when `vix` is given)
    and the time left now; futures move with the underlying. Re-analysed with the same method, so payoff() on the result
    gives premiums, breakevens, probabilities and costs for a trade placed now. A model estimate, not live quotes.
    None when there is no time left or nothing to re-price."""
    at = instant(live["at"])
    years0, years1 = report["_years"], years_to_expiry(report["expiry"], at)
    s0, s1, rate = report["spot"], live["level"], report["rate_assumption"]
    if years0 <= 0 or years1 <= 0 or not s0 or not s1:
        return None
    scale = vix["ratio"] if vix else 1.0
    f1 = s1 * (report["forward"] / s0) ** (years1 / years0)
    ivs = {(r["strike"], side.upper()): r[side]["iv"] for r in report["_rows"] for side in ("ce", "pe") if r.get(side) and r[side]["iv"]}
    contracts = []
    for book in report["_books"]:
        for k, c in book.items():
            iv = ivs.get((k, c["kind"]))
            new = dict(c, spot=s1, prev_close=c["close"])
            if iv:
                value = black76(c["kind"], f1, k, years1, iv * scale, rate)
                new.update(close=max(TICK, round(round(value / TICK) * TICK, 2)), volume=max(c["volume"], 1.0))
            else:
                new.update(close=None, volume=0.0)
            contracts.append(new)
    if not any(c["close"] for c in contracts):
        return None
    if report["future"]:
        f = report["future"]
        contracts.append({"kind": "FUT", "expiry": f["expiry"], "strike": None, "close": f["price"] * s1 / s0, "prev_close": f["price"],
                          "settle": f["price"], "oi": f["oi"], "oi_change": 0.0, "volume": 1.0, "lot_size": report["lot_size"],
                          "spot": s1, "trade_date": report["data_date"]})
    try:
        now = analyse_chain(contracts, report["underlying"], expiry=report["expiry"], as_of=at, rate=rate, source=report["source"],
                            holidays=holidays)
    except Block:
        return None
    meta = close_report or report
    now["_close"] = meta.get("_close", meta["spot"])  # exposure margin stays on the last exchange close
    for key in ("expiry_type", "expiry_weekday", "expiry_moved", "expiry_usual_date", "expiry_holiday", "expiry_note",
                "usual_expiry_weekday", "has_weeklies", "selection_note", "data_date"):
        now[key] = meta.get(key)
    vix_text = (f" (scaled by India VIX's move today, {fmt(vix['previous_close'])} → {fmt(vix['level'])}, {(scale - 1) * 100:+.1f}%)" if vix
                else "" if iv_from else " (unchanged)")
    snap = report.get("_snapshot")
    if snap and snap.get("closing"):
        moved = abs(s1 - snap["level"]) > 1e-9
        basis = (f"option prices from NSE Clearing's {snap['at'][11:16]} IST closing snapshot (SPAN risk-parameter file {snap['file']}: "
                 f"{report['underlying']} at its close {fmt(snap['level'])} and each option's price as NSE Clearing set it at the close; "
                 "on 17 Nov 2026 its end-of-day prices equalled the bhavcopy's closing prices)"
                 + (f", each moved to {now_where(live)} with Black-76 at its own IV" if moved else "")
                 + f", {years1 * 365:.1f} days to expiry; costs, breakevens and probabilities recomputed on those prices. Today's "
                 "closing prices, not live quotes: the next session opens at its own prices, so check your broker's option chain "
                 "before trading.")
    elif snap:
        implied = snap.get("implied_level", snap["level"])
        then = (f"{report['underlying']} then {fmt(snap['level'])}" if abs(implied - snap["level"]) < 1e-9 else
                f"{report['underlying']} {fmt(snap['level'])} in the file, but its option and futures prices imply ≈{fmt(implied)} "
                "at the close's futures basis, so moves are measured from there")
        basis = (f"option prices from NSE Clearing's {snap['at'][11:16]} IST intraday snapshot (the prices in its SPAN risk-parameter "
                 f"file {snap['file']}; {then})"
                 + (f", each moved to {now_where(live)}{now_when(live)} with Black-76 at its own snapshot IV"
                    if abs(s1 - implied) > 1e-9 or live["at"] != snap["at"] else "")
                 + f", {years1 * 365:.1f} days to expiry; costs, breakevens and probabilities recomputed on those prices. An hourly "
                 "exchange snapshot, not live quotes: check your broker's option chain before trading.")
    else:
        basis = (f"each option re-priced with Black-76 at {now_where(live, report['underlying'])}{now_when(live)}, its own IV "
                 f"from {iv_from or 'the ' + day_text(meta['data_date']) + ' close'}{vix_text}, and {years1 * 365:.1f} days to expiry; costs, "
                 "breakevens and probabilities recomputed on those prices. A model estimate, not live option quotes: check your broker's "
                 "option chain before trading.")
    now["_placed_now"] = {"level": s1, "at": live["at"], "years": years1, "forward": f1, "iv_scale": scale, "vix": vix,
                          "source": "nse_intraday_snapshot" if snap else "model_from_close", "snapshot": snap, "basis": basis,
                          "close": bool(live.get("close")), "provider": live.get("provider")}
    return now


# NSE Clearing's closing file vs the evening bhavcopy's close (18 Nov 2026, traded strikes within 3% of spot on the 23 Nov
# expiry): NIFTY median -0.9% (-4.5% to +1.5%), BANKNIFTY -0.04%, FINNIFTY -0.15%, MIDCPNIFTY -0.4%, in both directions and
# often equal; 19 Nov 2026: NIFTY median -0.6%, BANKNIFTY -0.9%, FINNIFTY and MIDCPNIFTY 0.0%, again both ways (NIFTY 25300 CE
# 275.30 vs 280.95) — no consistent bias worth correcting, so the label says so (NIFTY 25800 CE on 18 Nov: 21.00 here, 21.10).
CLOSING_PRICE_NOTE = "NSE Clearing's closing prices; the bhavcopy close can differ slightly"
SNAPSHOT_LATEST = dtime(16, 0)  # NSE Clearing's closing intraday file is stamped a few minutes after 15:30


def snapshot_report(span, report, *, now, holidays=None, max_age_hours=3.0):
    """The chosen expiry rebuilt from the option prices in today's NSE Clearing intraday SPAN file (an hourly exchange
    snapshot: i2, i3, …; i1 carries the previous close). None unless the file is from today's session, not in the future
    and at most `max_age_hours` old, and its prices give a usable chain."""
    if not span or span.get("status") != "ok" or "intraday" not in (span.get("file_label") or ""):
        return None
    created = span.get("created") or ""
    try:
        at = datetime.strptime(created[:12], "%Y%m%d%H%M").replace(tzinfo=IST)
    except ValueError:
        return None
    # After the close its age counts to 15:30, not to now: the 15:32 file (i5 on 18 Nov 2026) carries the closing prices.
    ref = min(now, datetime.combine(at.date(), SESSION_CLOSE, IST))
    if at.date() != now.date() or at > now + timedelta(minutes=5) or (ref - at).total_seconds() > max_age_hours * 3600:
        return None
    if not is_trading_day(at.date(), holidays) or not SESSION_OPEN <= at.time() <= SNAPSHOT_LATEST:
        return None
    level = span["underlying_close"]
    contracts = []
    for book in report["_books"]:
        for k, c in book.items():
            snap = span["options"].get((report["expiry"], c["kind"], float(k)))
            price = snap["price"] if snap and snap["price"] > 0 else None
            contracts.append(dict(c, close=price, volume=1.0 if price else 0.0, prev_close=c["close"], spot=level))
    fut = report.get("future")
    if fut and span["futures"].get(fut["expiry"]):
        contracts.append({"kind": "FUT", "expiry": fut["expiry"], "strike": None, "close": span["futures"][fut["expiry"]]["price"],
                          "prev_close": fut["price"], "settle": fut["price"], "oi": fut["oi"], "oi_change": 0.0, "volume": 1.0,
                          "lot_size": report["lot_size"], "spot": level, "trade_date": report["data_date"]})
    if sum(1 for c in contracts if c["close"]) < 4:
        return None
    analyse = lambda rows: analyse_chain(rows, report["underlying"], expiry=report["expiry"], as_of=at, rate=report["rate_assumption"],
                                         source=report["source"], holidays=holidays)
    try:
        snap_report = analyse(contracts)
    except Block:
        return None
    if not snap_report["atm_iv"]:
        return None
    # The file's index level and its option/futures prices are not taken at the same instant (seen on 18 Nov 2026: BANKNIFTY
    # 62,044.35 in nsccl.20261118.i02 while its futures and put-call parity priced ≈61,970 at the close's basis). The level
    # the options were priced at is their parity forward less the close's carry; moves to the live level start from there.
    implied, years = level, snap_report["_years"]
    # The closing file (stamped after 15:30; i5 at 15:32 on 18 Nov 2026) holds the closing level and NSE Clearing's closing option
    # prices — its end-of-day file's prices equal the bhavcopy's ClsPric (17 Nov 2026: NIFTY 25800 CE 51.45 in both, last trade
    # 45.30) — so it is used as it is, without the parity re-basing.
    closing = at.time() >= SESSION_CLOSE
    if (not closing and snap_report["forward_basis"].startswith("put-call parity") and report.get("forward") and report.get("spot")
            and report["_years"] > 0):
        implied = snap_report["forward"] / (report["forward"] / report["spot"]) ** (years / report["_years"])
        if abs(implied / level - 1) > 1e-4:
            try:
                snap_report = analyse([dict(c, spot=implied) for c in contracts])
            except Block:
                return None
            if not snap_report["atm_iv"]:
                return None
        else:
            implied = level
    snap_report["_snapshot"] = {"at": at.isoformat(), "file": span.get("file"), "level": level, "implied_level": implied, "closing": closing}
    snap_report["_close"] = report.get("_close", report["spot"])
    for key in ("expiry_type", "expiry_weekday", "expiry_moved", "expiry_usual_date", "expiry_holiday", "expiry_note",
                "usual_expiry_weekday", "has_weeklies", "selection_note", "data_date"):
        snap_report[key] = report.get(key)
    return snap_report


def placed_now_report(report, span, live, *, now, holidays=None, fetcher=None):
    """The chain re-priced for a trade placed now: NSE Clearing's intraday snapshot prices when fresh (moved to the live
    level when that is newer), otherwise the close's IVs at the live level (NIFTY: scaled by India VIX's move today).
    None when neither is available."""
    snap = snapshot_report(span, report, now=now, holidays=holidays)
    if snap:
        at = snap["_snapshot"]["at"]
        later = live and (live.get("close") or instant(live["at"]) > instant(at))  # after the session: always to the close
        target = live if later else {"level": snap["spot"], "at": at}
        if later and live.get("close") and snap["_snapshot"].get("closing"):
            target = dict(live, at=at)  # the closing file is already at the close: same clock, so its prices stay exact
        return reprice_report(snap, target, holidays=holidays, close_report=report)
    if live:
        vix = None
        if report["underlying"] == "NIFTY" and fetcher and report["data_date"] == previous_trading_day(now.date(), holidays).isoformat():
            vix = live_vix(fetcher=fetcher, now=now)
        return reprice_report(report, live, vix=vix, holidays=holidays)
    return None


def now_where(x, underlying=None):
    """'live NIFTY 25,227.95' during the session; 'the NSE close NIFTY 25,227.95' after it (`x`: a live or placed-now dict)."""
    name = f"{underlying} " if underlying else ""
    if x.get("close"):
        return (f"the NSE close {name}{fmt(x['level'])}" if x.get("provider") == "NSE" else
                f"today's close {name}{fmt(x['level'])} ({x.get('provider', 'Yahoo')}, unofficial)")
    return f"live {name}{fmt(x['level'])}"


def now_when(x):
    """' (13:43 IST)' during the session; ' (Wed 18 Nov)' after it."""
    return f" ({day_text(x['at'][:10])})" if x.get("close") else f" ({x['at'][11:16]} IST)"


def now_estimate(now_report):
    """Chain 'Now (estimate)' figures from a re-priced report: ATM strike, IV, straddle and its two legs, with the basis."""
    if not now_report:
        return {"available": False, "note": "no intraday NSE snapshot or live index level yet; positioning below is end of day"}
    pn = now_report["_placed_now"]
    snap = pn.get("snapshot")
    calls, puts = now_report["_books"]
    atm = now_report["atm_strike"]
    ce, pe = price_of(calls[atm]) if atm in calls else None, price_of(puts[atm]) if atm in puts else None
    at = pn["at"][11:16]
    if snap:
        moved = abs(pn["level"] - snap["level"]) > 1e-9 or pn["at"] != snap["at"]
        short = (f"NSE Clearing's {snap['at'][11:16]} IST intraday snapshot ({snap['file']})"
                 + (f" moved to {now_where(pn, now_report['underlying'])}{now_when(pn)}" if moved else "")
                 + " with Black-76 at each strike's snapshot IV")
        plain = (f"from NSE's {snap['at'][11:16]} IST {'closing' if snap['at'][11:16] >= '15:30' else 'intraday'} option prices"
                 + (f", moved to {now_where(pn, now_report['underlying'])}{now_when(pn)}" if moved else "")
                 + (f" ({CLOSING_PRICE_NOTE})" if snap['at'][11:16] >= '15:30' else ""))
    else:
        short = (f"model at {now_where(pn, now_report['underlying'])}{now_when(pn)} with each strike's IV from the "
                 f"{day_text(now_report['data_date'])} close" + (" scaled by India VIX's move" if pn.get("vix") else ""))
        plain = (f"modelled at {now_where(pn, now_report['underlying'])}{now_when(pn)} from the "
                 f"{day_text(now_report['data_date'])} close's option prices" + (", adjusted for India VIX's move" if pn.get("vix") else ""))
    return {"available": True, "label": "estimate, not live quotes", "at": pn["at"], "level": pn["level"], "source": pn["source"],
            "close": pn.get("close"), "provider": pn.get("provider"),
            "snapshot_at": snap["at"] if snap else None, "file": snap["file"] if snap else None, "atm_strike": atm,
            "atm_iv": now_report["atm_iv"], "straddle": ce + pe if ce and pe else None, "ce": ce, "pe": pe,
            "days_to_expiry": pn["years"] * 365, "forward": pn["forward"], "basis_short": short, "basis_plain": plain, "basis": pn["basis"],
            # The to-expiry move centred on the live level, priced on the same snapshot (or model) IVs as the straddle above.
            "range_1sd": one_sd_range(pn["forward"], now_report["atm_iv"], pn["years"]),
            "iv_source": "snapshot IV" if snap else f"IVs from the {day_text(now_report['data_date'])} close"}


def outcome(p):
    """After-cost headline figures of a payoff result (before-cost figures when no rate card)."""
    a, pr = p["after_costs"], p["probabilities"]
    above = {round(b["breakeven"], 6): b["prob_above"] for b in pr.get("above_breakevens_after_costs" if a else "above_breakevens") or []}
    if not a:
        above = {round(b["breakeven"], 6): b["prob_above_skew"] for b in pr.get("above_breakevens") or []}
    sd = {x["sd"]: (x["level"], x["pnl_after_costs"] if x["pnl_after_costs"] is not None else x["pnl"])
          for x in (p.get("scenarios") or {}).get("expiry", [])}
    return {"net_premium": a["net_premium"] if a else p["net_premium"], "max_profit": a["max_profit"] if a else p["max_profit"],
            "max_loss": a["max_loss"] if a else p["max_loss"], "breakevens": a["breakevens"] if a else p["breakevens"],
            "prob_above": above, "pop": pr.get("profit_after_costs") if a else pr.get("profit_skew"),
            "pop_flat": pr.get("profit_after_costs_flat") if a else pr.get("profit"), "max_loss_chance": pr.get("max_loss_chance"),
            "sd_pnl": sd, "after_costs": bool(a)}


def margin_legs(report, legs):
    fut = report["future"]["expiry"] if report.get("future") else report["expiry"]
    return [{"type": l["type"], "strike": l["strike"], "units": l["units"], "expiry": fut if l["type"] == "FUT" else report["expiry"]}
            for l in legs]


def margin_for(report, legs, span, trade_day):
    """Indicative SPAN + exposure for resolved payoff legs (scripts/fno_margin.py), or the unavailable note."""
    if span is None:
        return None
    close = None if report.get("_live") else report.get("_close", report["spot"])  # the bhavcopy's underlying close
    live = report.get("live") or {}
    if close and live.get("close") and live.get("level") and str(live.get("at", ""))[:10] > report["data_date"]:
        # After today's close (bhavcopy not yet out) the next session's exposure is on today's close, not the bhavcopy's.
        close = live["level"]
    return fno_margin.estimate(span, margin_legs(report, legs), expiry=report["expiry"], trade_day=trade_day,
                               index=report["underlying"] in INDICES, close=close, today=(report.get("session") or {}).get("today"))


def return_on_margin(max_profit, margin):
    if max_profit is None or not margin or not margin.get("available") or margin["total"] <= 0.5:
        return None
    return max_profit / margin["total"]


def svg_chart(report, result, placed_now=False):
    w, h, left, right, top, bottom = 760, 400, 72, 24, 72, 52
    xs = [s for s, _ in result["curve"]]
    ys = [v for _, v in result["curve"]] + [v for _, v in result.get("today_curve") or []]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys + [0]), max(ys + [0])
    if y1 == y0:
        y1 = y0 + 1
    X = lambda s: left + (s - x0) / (x1 - x0) * (w - left - right)
    Y = lambda v: h - bottom - (v - y0) / (y1 - y0) * (h - top - bottom)
    zero = Y(0)
    path = lambda curve: " ".join(f"{'M' if i == 0 else 'L'}{X(s):.1f},{Y(v):.1f}" for i, (s, v) in enumerate(curve))
    line = path(result["curve"])
    area = f"{line} L{X(x1):.1f},{zero:.1f} L{X(x0):.1f},{zero:.1f} Z"
    placed = report.get("_placed_now") if placed_now else None
    basis = (f"if placed now: model estimate at live {report['underlying']} {report['spot']:,.2f} ({placed['at'][11:16]} IST), not live quotes"
             if placed else f"data {report['data_date']}")
    legs = " · ".join(f"{l['side']} {l['lots']}× {l['type']}{'' if l['type'] == 'FUT' else ' ' + format(l['strike'], 'g')}" for l in result["legs"])
    marks = "".join(f'<line x1="{X(b):.1f}" y1="{top}" x2="{X(b):.1f}" y2="{h - bottom}" stroke="#6b7280" stroke-dasharray="4 4"/>'
                    f'<text x="{X(b):.1f}" y="{top - 6}" text-anchor="middle" fill="#374151">BE {b:,.0f}</text>' for b in result["breakevens"])
    lx = w - right - 250
    legend = (f'<line x1="{lx}" y1="50" x2="{lx + 24}" y2="50" stroke="#111827" stroke-width="2"/>'
              f'<text x="{lx + 30}" y="54" fill="#374151">At expiry</text>')
    today = ""
    if result.get("today_curve"):
        today = f'<path d="{path(result["today_curve"])}" fill="none" stroke="#7c3aed" stroke-width="1.75" stroke-dasharray="7 5"/>'
        legend += (f'<line x1="{lx + 100}" y1="50" x2="{lx + 124}" y2="50" stroke="#7c3aed" stroke-width="1.75" stroke-dasharray="7 5"/>'
                   f'<text x="{lx + 130}" y="54" fill="#374151">Today (T+0)</text>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img" aria-label="Payoff at expiry{' and today (T+0)' if today else ''} for {report['underlying']} {report['expiry']}: {legs}">
<defs>
<clipPath id="gain"><rect x="0" y="0" width="{w}" height="{zero:.1f}"/></clipPath>
<clipPath id="loss"><rect x="0" y="{zero:.1f}" width="{w}" height="{h - zero:.1f}"/></clipPath>
</defs>
<g font-family="system-ui, -apple-system, 'Segoe UI', sans-serif" font-size="11">
<rect width="{w}" height="{h}" fill="#ffffff"/>
<text x="{left}" y="22" font-size="15" font-weight="600" fill="#111827">{report['underlying']} · expiry {report['expiry']}{' (' + report['expiry_type'] + (', moved' if report.get('expiry_moved') else '') + ')' if report.get('expiry_type') else ''} · payoff at expiry{' and today' if today else ''}</text>
<text x="{left}" y="40" font-size="12" fill="#4b5563">{legs} · lot {result['lot_size']} · ₹, excluding costs · {basis}</text>
{legend}
<path d="{area}" fill="#16a34a" fill-opacity="0.14" clip-path="url(#gain)"/>
<path d="{area}" fill="#dc2626" fill-opacity="0.12" clip-path="url(#loss)"/>
<line x1="{left}" y1="{zero:.1f}" x2="{w - right}" y2="{zero:.1f}" stroke="#9ca3af"/>
<text x="{left - 8}" y="{zero + 4:.1f}" text-anchor="end" fill="#6b7280">0</text>
<line x1="{X(report['spot']):.1f}" y1="{top}" x2="{X(report['spot']):.1f}" y2="{h - bottom}" stroke="#2563eb"/>
<text x="{X(report['spot']):.1f}" y="{h - bottom + 32}" text-anchor="middle" fill="#2563eb">spot {report['spot']:,.0f}</text>
{marks}
{today}
<path d="{line}" fill="none" stroke="#111827" stroke-width="2"/>
<text x="{left}" y="{h - bottom + 16}" fill="#6b7280">{x0:,.0f}</text>
<text x="{w - right}" y="{h - bottom + 16}" text-anchor="end" fill="#6b7280">{x1:,.0f}</text>
<text x="{left - 8}" y="{Y(y1) + 4:.1f}" text-anchor="end" fill="#15803d">{y1:,.0f}</text>
<text x="{left - 8}" y="{Y(y0) + 4:.1f}" text-anchor="end" fill="#b91c1c">{y0:,.0f}</text>
</g>
</svg>
"""


# ---------------- output ----------------

def fmt(v, d=2):
    if v is None:
        return "n/a"
    return f"{0.0 if abs(v) < 0.5 * 10 ** -d else v:,.{d}f}"


def signed(v, d=2):
    if v is None:
        return "n/a"
    return "0" if abs(v) < 0.5 * 10 ** -d else f"{v:+,.{d}f}"


def pct(v, d=1):
    return "n/a" if v is None else f"{v * 100:.{d}f}%"


def contracts_text(units, lot):
    return "n/a" if not lot else f"{units / lot:,.0f}"


def signed_contracts(units, lot):
    return "n/a" if not lot or units is None else signed(units / lot, 0)


def vix_line(vix):
    return (f"{fmt(vix['vix'])} (close {vix['date']}) — {pct(vix['percentile_1y'], 0)} 1-year percentile "
            f"(range {fmt(vix['low_1y'])}–{fmt(vix['high_1y'])})")


def oi_breakdown_table(r):
    lot = r["lot_size"]
    c, p = r["call_oi_breakdown"], r["put_oi_breakdown"]
    row = lambda label, f: f"| {label} | {signed_contracts(f(c), lot)} | {signed_contracts(f(p), lot)} |"
    return ["| OI change (contracts) | Calls | Puts |", "| --- | ---: | ---: |",
            row("Added: long build-up (price up, OI up)", lambda b: b["added"]["long build-up"]),
            row("Added: short build-up (price down, OI up)", lambda b: b["added"]["short build-up"]),
            row("**OI added**", lambda b: b["added_total"]),
            row("Reduced: short covering (price up, OI down)", lambda b: b["reduced"]["short covering"]),
            row("Reduced: long unwinding (price down, OI down)", lambda b: b["reduced"]["long unwinding"]),
            row("**OI reduced**", lambda b: b["reduced_total"]),
            row("Not classified (no trade or no previous close)", lambda b: b["unclassified"]),
            row("**Net OI change** (= summary change)", lambda b: b["net"])]


def dod_section(r):
    dod = r.get("day_over_day")
    if not dod:
        return []
    if dod["status"] != "ok":
        return ["## Day over day", "", dod["note"], ""]
    m, lot = dod["metrics"], r["lot_size"]
    rows = [("Spot", "spot", lambda v: fmt(v), lambda v: signed(v)),
            ("PCR (open interest)", "pcr_oi", lambda v: fmt(v), lambda v: signed(v)),
            ("Max pain", "max_pain", lambda v: fmt(v, 0), lambda v: signed(v, 0)),
            ("ATM IV", "atm_iv", pct, lambda v: "n/a" if v is None else signed(v * 100, 1) + " pts"),
            ("Call OI (contracts)", "call_oi", lambda v: contracts_text(v, lot), lambda v: signed_contracts(v, lot)),
            ("Put OI (contracts)", "put_oi", lambda v: contracts_text(v, lot), lambda v: signed_contracts(v, lot))]
    span = f" ({sessions_label(dod['sessions'], dod.get('sessions_certain', True))})" if (dod.get("sessions") or 1) > 1 else ""
    lines = [f"## Day over day (same expiry {dod['expiry']})", ""]
    lines += [f"⚠ {x}." for x in (dod.get("gap_note"), dod.get("expiry_caveat")) if x] + ([""] if dod.get("gap_note") or dod.get("expiry_caveat") else [])
    lines += [f"| Measure | {dod['previous_date']} | {dod['current_date']} | Change{span} |", "| --- | ---: | ---: | ---: |"]
    for label, key, show, change in rows:
        lines.append(f"| {label} | {show(m[key]['previous'])} | {show(m[key]['current'])} | {change(m[key]['change'])} |")
    return lines + ["", f"{dod['note']} ATM IV and max pain are recomputed for each day with the same method.", ""]


def expiry_tag(r):
    """Short type label for headers: 'monthly, Monday — moved …' or '' when the type is unknown (input chains)."""
    if not r.get("expiry_type"):
        return ""
    text = f"{r['expiry_type']}, {r['expiry_weekday']}"
    if r.get("expiry_moved"):
        text += f" — moved from {r['usual_expiry_weekday']} {r['expiry_usual_date']}, {moved_reason(r.get('expiry_holiday'))}"
    return f" ({text})"


def expiry_list_text(r):
    if not r.get("expiry_calendar"):
        return ", ".join(r["expiries"])
    return ", ".join(f"{x['expiry']} ({x['type']}{', moved from ' + x['usual_date'] if x['moved'] else ''})" for x in r["expiry_calendar"])


def one_day_text(m):
    if not m:
        return "n/a"
    d = 0 if m["high"] >= 1000 else 2
    return f"±{fmt(m['points'])} ({pct(m['pct'], 2)}): {fmt(m['low'], d)} – {fmt(m['high'], d)}"


def chain_markdown(r, vix=None):
    lot = r["lot_size"]
    rng = r.get("range_1sd")
    lines = report_preamble(r)
    lines += [f"# {r['underlying']} options — expiry {r['expiry']}{expiry_tag(r)}", "",
              f"Data: {r['source']} for {r['data_date']} · {r['days_to_expiry']:.1f} days to expiry · lot size {lot}{live_text(r)}", ""]
    if r.get("expiry_moved") or r.get("has_weeklies") is False or r.get("expiry_type") is None:
        lines += [f"{r['expiry_note']}", ""]
    lines += [f"> {r['disclaimer']}", "", "## Summary", "",
             "| Measure | Value |", "| --- | ---: |",
             f"| Spot | {fmt(r['spot'])} |",
             *([f"| Now (estimate, not live quotes) | {now_estimate_text(r['now_estimate']).split(': ', 1)[1]} |"]
               if r.get("now_estimate") else []),
             f"| Futures-implied level at expiry (forward; {r['forward_basis']}) | {fmt(r['forward'])} |",
             f"| PCR (open interest) | {fmt(r['pcr_oi'])} |",
             f"| PCR (volume) | {fmt(r['pcr_volume'])} |",
             f"| Max pain | {fmt(r['max_pain'], 0)} |",
             f"| ATM strike / ATM IV | {fmt(r['atm_strike'], 0)} / {pct(r['atm_iv'])} |",
             f"| ATM straddle (expected move to expiry) | ±{fmt(r['straddle'])} ({pct(r['straddle'] / r['spot'] if r['straddle'] else None)}) |",
             f"| IV-implied 1-sigma move to expiry | ±{fmt(r['expected_move_iv'])} |",
             *([f"| {range_label(m)} | {range_value(m)} |" for m in r["session_ranges"]] if r.get("session_ranges") else
               [f"| Next-session 1-sigma move and range (ATM IV × √(1/{TRADING_DAYS}); {PROB_LABEL}) | {one_day_text(r.get('one_day_move'))} |"]),
             f"| 68% implied range at expiry (±1 SD, lognormal; {PROB_LABEL}){'' if r.get('_live') else ', from the ' + day_text(r['data_date']) + ' close'} | {fmt(rng[0], 0) + ' – ' + fmt(rng[1], 0) if rng else 'n/a'} |",
             *([f"| 68% implied range at expiry from {now_where(r['now_estimate'])}"
                f"{now_when(r['now_estimate']) if r['now_estimate'].get('close') else ' at ' + r['now_estimate']['at'][11:16] + ' IST'} "
                f"({r['now_estimate'].get('iv_source', 'snapshot IV')}) | {fmt(r['now_estimate']['range_1sd'][0], 0)} – "
                f"{fmt(r['now_estimate']['range_1sd'][1], 0)} |"] if (r.get("now_estimate") or {}).get("range_1sd") else []),
             f"| Put-minus-call skew (5% wings) | {pct(r['skew_put_minus_call'])} |",
             f"| Call OI / change (contracts) | {contracts_text(r['call_oi'], lot)} / {signed_contracts(r['call_oi_change'], lot)} |",
             f"| Put OI / change (contracts) | {contracts_text(r['put_oi'], lot)} / {signed_contracts(r['put_oi_change'], lot)} |"]
    if r["future"]:
        f = r["future"]
        lines.append(f"| Future {f['expiry']} / basis | {fmt(f['price'])} / {fmt(f['basis'])} ({pct(f['basis_pct'], 2)}){' · ' + f['buildup'] if f['buildup'] else ''} |")
    if vix:
        lines.append(f"| India VIX | {vix_line(vix)} |")
    if vix and vix.get("note"):
        lines += ["", f"Note: {vix['note']}"]
    if r.get("strike_focus"):
        lines.append(f"| {r['strike_focus']['label']} | {r['strike_focus']['text']} |")
    lines += [""] + dod_section(r) + participant_section(r.get("participants"))
    walls = oi_walls(r)
    lines += ["## Positioning", "",
              f"- **Highest call OI above {walls[0]} (resistance by OI):** {', '.join(fmt(k, 0) for k in walls[1]) or 'n/a'}",
              f"- **Highest put OI below {walls[0]} (support by OI):** {', '.join(fmt(k, 0) for k in walls[2]) or 'n/a'}",
              *([f"- **{near_walls_text(r['oi_walls_near'])}**"] if near_walls_text(r.get("oi_walls_near")) else []),
              f"- **Max pain** {fmt(r['max_pain'], 0)}{pain_distance(r)}", ""]
    lines += oi_breakdown_table(r)
    lines += ["", "Added rows are positive and reduced rows negative, so each column sums to the net change shown in the summary. "
              "Build-up uses each contract's own close against its previous close.", "",
              "Reading guide: heavy call OI above spot and put OI below spot are levels option sellers are defending, not guaranteed barriers. "
              "PCR far above 1 means many puts are open (often read as support or hedging); far below 1 means many calls. Max pain is where option buyers collectively lose most at expiry; prices often do not settle there.", "",
              "## Chain around the money", "",
              "| CE OI (ctr) | CE ΔOI | CE IV | CE close | CE Δ | **Strike** | PE Δ | PE close | PE IV | PE ΔOI | PE OI (ctr) |",
              "| ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in r["chain"]:
        c, p = row.get("ce", {}), row.get("pe", {})
        cell = lambda s, key: (fmt(s.get("greeks", {}).get("delta"), 2) if key == "delta" and s.get("greeks") else
                               pct(s.get("iv")) if key == "iv" else
                               fmt(s.get("close") if s.get("close") else None) if key == "close" else
                               contracts_text(s.get(key, 0), lot) if s else "")
        strike = f"**{fmt(row['strike'], 0)}**" if row["strike"] == r["atm_strike"] else fmt(row["strike"], 0)
        lines.append(f"| {cell(c, 'oi')} | {cell(c, 'oi_change')} | {cell(c, 'iv') if c else ''} | {cell(c, 'close') if c else ''} | {cell(c, 'delta') if c else ''} | {strike} | "
                     f"{cell(p, 'delta') if p else ''} | {cell(p, 'close') if p else ''} | {cell(p, 'iv') if p else ''} | {cell(p, 'oi_change')} | {cell(p, 'oi')} |")
    lines += ["", f"Expiries: {expiry_list_text(r)}" + (f" · usual expiry day {r['usual_expiry_weekday']}" if r.get("usual_expiry_weekday") else ""), "",
              f"Method: Black-76 on the forward, rate assumption {pct(r['rate_assumption'])}. {r['otm_note']} The 68% range is a risk-neutral "
              f"lognormal at the ATM IV ({PROB_LABEL}). The next-session move is spot × ATM IV × √(1/{TRADING_DAYS}) (one trading "
              "day; the IV is annualised on calendar time, so weekend variance is spread over sessions). Weekly/monthly comes from the "
              "listed expiries: the one sharing the futures expiry (or the last of the month) is the monthly. "
              "End-of-day data; use a broker connector chain (--input) for live prices.", ""]
    if r.get("session_ranges"):
        lines += [f"Session ranges: centre × ATM IV × √(sessions/{TRADING_DAYS}); a range from a live level counts the rest of today's "
                  "session as a fraction (6¼ hours = 1 session). " + (r["session"].get("holiday_note") or "Sessions skip NSE trading holidays."), ""]
    return "\n".join(lines)


def sessions_left(expiry, ctx, holidays):
    """Trading sessions from the next one to trade — today's while it is in progress, else the next not yet started — to
    `expiry`, both included (1 = the next session is the expiry session); None without a session context."""
    if not ctx:
        return None
    start = date.fromisoformat(ctx["today"] if ctx["phase"] == "open" else ctx["next_unstarted_session"])
    end = date.fromisoformat(expiry)
    if end < start:
        return 0
    return int(is_trading_day(start, holidays)) + sessions_between(start, end, holidays)


def next_expiries(r):
    """[(date, type)] for the first weekly and the first monthly expiry after the selected one (from the expiry calendar)."""
    out = []
    for kind in ("weekly", "monthly"):
        x = next((x for x in r.get("expiry_calendar") or [] if x["expiry"] > r["expiry"] and x["type"] == kind), None)
        if x:
            out.append((x["expiry"], kind))
    return sorted(out)


def expiry_lead(r):
    """'**Expires Mon 23 Nov — 1 session left** (moved from Tue 24 Nov, NSE trading holiday (…)); next expiries: Tue 1 Dec
    (weekly), Tue 29 Dec (monthly).' when the expiry is at most one session away, else None."""
    n, ctx = r.get("sessions_left"), r.get("session")
    if n is None or n > 1 or not ctx:
        return None
    if n == 0:
        return f"**Expiry {day_text(r['expiry'])} has passed** — choose a later expiry with --expiry."
    if r["expiry"] == ctx["today"] and ctx["phase"] == "open":
        head = f"**Expires today, {day_text(r['expiry'])} — the final session is in progress**"
    elif r["expiry"] == ctx["today"]:
        head = f"**Expires today, {day_text(r['expiry'])} — 1 session left (today's)**"
    else:
        head = f"**Expires {day_text(r['expiry'])} — 1 session left**"
    if r.get("expiry_moved") and r.get("expiry_usual_date"):
        head += f" (moved from {day_text(r['expiry_usual_date'])}, {moved_reason(r.get('expiry_holiday'))})"
    nxt = next_expiries(r)
    if nxt:
        head += f"; next expir{'ies' if len(nxt) > 1 else 'y'}: " + ", ".join(f"{day_text(d)} ({k})" for d, k in nxt)
    return head + "."


def report_preamble(r):
    """Lines that must come first: the end-of-day warning during market hours, a changed expiry selection, an assumed underlying,
    and the one-session-to-expiry line."""
    out = [x for x in (data_age_line(r.get("session")), r.get("post_close_note"), expiry_lead(r)) if x]
    for key in ("selection_note", "underlying_assumption"):
        if r.get(key):
            out.append(f"**Note:** {r[key]}")
    if r.get("strike_resolution"):
        out.append(f"**Strikes:** {r['strike_resolution']}")
    return out + ([""] if out else [])


def live_text(r):
    live = r.get("live")
    if not live or r.get("_live"):
        return ""
    at = instant(live["at"])
    change = live["level"] / r["spot"] - 1
    if live.get("close"):
        return f" · {now_where(live, r['underlying'])}{now_when(live)} ({change * 100:+.2f}% vs that close; {live['source']})"
    return f" · live {r['underlying']} {fmt(live['level'])} at {at:%H:%M} IST ({change * 100:+.2f}% vs that close; {live['source']})"


def range_label(m):
    if abs(m["sessions"] - 1) < 1e-9:
        return f"1-sigma range for the {day_text(m['session'])} session ({m['status']}), from {m['centre_label']}"
    return f"1-sigma range to the {day_text(m['session'])} close ({m['status']}), from {m['centre_label']}" + (f" ({sessions_text(m)})" if sessions_text(m) else "")


def range_value(m):
    d = 0 if m["high"] >= 1000 else 2
    return f"±{fmt(m['points'])} ({pct(m['pct'], 2)}): {fmt(m['low'], d)} – {fmt(m['high'], d)}"


def strike_focus(r, strike, kind):
    """One-line view of a strike the user named (e.g. '25800 CE')."""
    row = next((x for x in r["_rows"] if x["strike"] == strike), None)
    sides = [kind] if kind else ["CE", "PE"]
    parts = []
    for k in sides:
        c = (row or {}).get(k.lower())
        if not c:
            parts.append(f"{strike:,.0f} {k}: not listed for {r['expiry']}")
            continue
        g = c.get("greeks") or {}
        parts.append(f"{strike:,.0f} {k}: close {fmt(c['close'] or c['stale_close'])}{'' if c['close'] else ' (stale, untraded)'}, IV {pct(c['iv'])}, "
                     f"delta {fmt(g.get('delta'), 2)}, OI {contracts_text(c['oi'], r['lot_size'])} contracts "
                     f"({signed_contracts(c['oi_change'], r['lot_size'])}){', ' + c['buildup'] if c.get('buildup') else ''}")
    return {"label": f"Strike {strike:,.0f}" + (f" {kind}" if kind else ""), "text": "; ".join(parts)}


def leg_name(l):
    return f"{l['side']} {l['lots']} lot{'s' if l['lots'] > 1 else ''} {l['type']}" + ("" if l["type"] == "FUT" else f" {l['strike']:g}")


def extreme_text(p, key):
    if p[key] is None:
        return "unlimited"
    return fmt(p[key]) + (" (only if the price fell to zero)" if p["extreme_at_zero"]["profit" if key == "max_profit" else "loss"] else "")


def rupees(v, d=0, sign=False):
    """'₹3,344' / '₹-1,200' / '+₹…'; 'unlimited' for None (never '₹unlimited')."""
    if v is None:
        return "unlimited"
    text = fmt(abs(v), d)
    return ("-" if v < 0 and text.strip("0.,") else "+" if sign and v > 0 else "") + "₹" + text


def close_label(r):
    return f"the {day_text(r['data_date'])} close" if not r.get("_live") else f"the chain's spot ({r['as_of'][11:16]} IST)"


def attach_extras(report, result, legs, *, now=None, card=None, span=None, trade_day=None):
    """Add the indicative margin, return on margin and — when a re-priced report `now` is given — the 'if placed now'
    figures to a payoff result. Returns the placed-now payoff result (or None) for charting."""
    eod = outcome(result)
    margin = margin_for(report, result["legs"], span, trade_day or report["data_date"])
    result.update(eod_outcome=eod, margin=margin, return_on_margin=return_on_margin(eod["max_profit"], margin))
    if not now:
        return None
    try:
        later = payoff(now, legs, card=card)
    except Block as error:
        result["placed_now"] = {"available": False, "note": f"If-placed-now estimate unavailable: {str(error).replace('FNO: ', '')}."}
        return None
    o = outcome(later)
    pr = later["probabilities"]
    result["placed_now"] = {
        "available": True, **now["_placed_now"], "atm_iv": now["atm_iv"], "outcome": o,
        "return_on_margin": return_on_margin(o["max_profit"], margin), "scenarios": later["scenarios"],
        "expected_value": later["expected_value"], "range_1sd": pr.get("range_1sd"), "costs": later["costs"],
        "legs": [{"name": leg_name(a), "type": a["type"], "close": a["price"], "now": b["price"], "iv_close": a["iv"], "iv_now": b["iv"]}
                 for a, b in zip(result["legs"], later["legs"])],
        "label": "model estimate, not live option quotes"}
    snap = now["_placed_now"].get("snapshot")
    if snap:
        # 'Already in this trade from the close?' on the same prices as 'If placed now' (the intraday snapshot moved to live),
        # so the two lines cannot disagree; without a snapshot the close-IV model mark set by the caller stays, labelled.
        pn = now["_placed_now"]
        result["live_mark"] = {"level": pn["level"], "at": pn["at"], "source": "nse_intraday_snapshot", "close": pn.get("close"),
                               "provider": pn.get("provider"),
                               "iv_from": f"NSE Clearing's {snap['at'][11:16]} IST intraday snapshot",
                               "pnl": sum(a["units"] * (b["price"] - a["price"]) for a, b in zip(result["legs"], later["legs"]))}
    return later


def now_title(pn, underlying):
    """Heading for the 'if placed now' block, naming where the prices come from."""
    snap = pn.get("snapshot")
    if pn.get("close"):
        src = (f"NSE's {snap['at'][11:16]} IST {'closing' if snap['at'][11:16] >= '15:30' else 'intraday'} option prices" if snap
               else "a model from the last bhavcopy's implied volatilities")
        return (f"If placed now — estimate at {now_where(pn, underlying)}{now_when(pn)} from {src}; market closed, so an order "
                "now fills at the next session's prices, not these")
    if snap:
        return (f"If placed now — estimate at live {underlying} {fmt(pn['level'])} ({pn['at'][11:16]} IST) from NSE's "
                f"{snap['at'][11:16]} IST intraday option prices, not live quotes")
    return f"If placed now — model estimate at live {underlying} {fmt(pn['level'])} ({pn['at'][11:16]} IST), not live option quotes"


def premium_word(v):
    return "received" if v > 0 else "paid"


def be_cell(o):
    def one(b):
        p = o["prob_above"].get(round(b, 6))
        return fmt(b, 0) + (f" (P above {pct(p, 0)})" if p is not None else "")
    return ", ".join(one(b) for b in o["breakevens"]) or "none in range"


def comparison_lines(r, p):
    """'If placed now' vs 'at the close' table for the lots shown, after costs."""
    pn, e = p["placed_now"], p["eod_outcome"]
    o, m = pn["outcome"], p.get("margin")
    cost = "after costs" if o["after_costs"] else "before costs"
    iv = lambda v: f" (IV {pct(v)})" if v else ""
    rows = [f"| For the lots shown, {cost} | If placed now (estimate) | At the {day_text(r['data_date'])} close |", "| --- | ---: | ---: |"]
    for leg in pn["legs"]:
        rows.append(f"| {leg['name']} premium | ≈ {fmt(leg['now'])}{iv(leg['iv_now'])} | {fmt(leg['close'])}{iv(leg['iv_close'])} |")
    rows += [f"| Net premium | ≈ {rupees(abs(o['net_premium']))} {premium_word(o['net_premium'])} | "
             f"{rupees(abs(e['net_premium']))} {premium_word(e['net_premium'])} |",
             f"| Maximum profit at expiry | {'≈ ' if o['max_profit'] is not None else ''}{rupees(o['max_profit'])} | {rupees(e['max_profit'])} |",
             f"| Maximum loss at expiry | {'≈ ' + rupees(-o['max_loss']) if o['max_loss'] is not None else 'unlimited (grows with the move)'} | "
             f"{rupees(-e['max_loss']) if e['max_loss'] is not None else 'unlimited'} |",
             f"| Breakeven(s) at expiry | {be_cell(o)} | {be_cell(e)} |",
             f"| Probability of profit at expiry ({PROB_LABEL}) | {pct(o['pop'])} | {pct(e['pop'])} |"]
    if o["max_loss_chance"] is not None:
        rows.append(f"| Chance of the maximum loss | {pct(o['max_loss_chance'])} | {pct(e['max_loss_chance'])} |")
    if m and m.get("available"):
        band = (f"hedged: brokers have shown ≈₹{fno_margin.inr(m['broker_low'])}" if m.get("broker_low") is not None
                else "brokers typically within ±10%")
        if (m.get("expiry_extra") or 0) > 0.5:
            band = (f"₹{fno_margin.inr(m['normal_total'])} normal + ₹{fno_margin.inr(m['expiry_extra'])} expiry-day extra 2%; "
                    + ("hedged: brokers have shown ≈₹" + fno_margin.inr(m['broker_low_normal']) + " normal"
                       if m.get("broker_low_normal") is not None else "brokers typically within ±10% of the normal part"))
        rows += [f"| Margin blocked (SPAN + exposure) | indicative ₹{fno_margin.inr(m['total'])} ({band}) | "
                 "same strikes, same margin |",
                 f"| Return on margin if held to expiry (max profit ÷ margin) | {pct(pn['return_on_margin'])} | {pct(p.get('return_on_margin'))} |"]
        now_cash, eod_cash = cash_needed(m, o["net_premium"]), cash_needed(m, e["net_premium"])
        if now_cash or eod_cash:
            cell = lambda c: "≈ " + cash_text(c).split(" = ", 1)[1] + f" (₹{fno_margin.inr(c[2])} premium)" if c else "n/a (net credit)"
            rows.append(f"| Cash needed (margin + premium paid) | {cell(now_cash)} | {cell(eod_cash)} |")
    else:
        rows.append(f"| Margin | {fno_margin.UNAVAILABLE} | |")
    return rows


def cash_needed(m, net_premium):
    """(low, high) cash to open a net-debit position: margin blocked plus the premium paid (after entry charges); low uses the
    broker figure seen on hedged positions when there is one. None for a net credit or when the margin is unavailable."""
    if net_premium is None or net_premium >= 0 or not m or not m.get("available"):
        return None
    paid = -net_premium
    high = (m["total"] if m["total"] > 0.5 else 0.0) + paid
    low = (m["broker_low"] + paid) if m.get("broker_low") is not None else high
    return low, high, paid


def cash_text(c):
    low, high, paid = c
    total = f"₹{fno_margin.inr(high)}" if abs(high - low) < 0.5 else f"₹{fno_margin.inr(low)}–₹{fno_margin.inr(high)}"
    return f"cash needed ≈ margin + premium paid ₹{fno_margin.inr(paid)} = {total}"


def margin_line(p, short=False):
    m = p.get("margin")
    if m is None:
        return p["margin_note"]
    if not m.get("available"):
        return m["note"][0].upper() + m["note"][1:] + "."
    pn = p.get("placed_now") or {}
    rom = pn.get("return_on_margin") if pn.get("available") else p.get("return_on_margin")
    tail = f" Return on margin if held to expiry {pct(rom)} (max profit after costs ÷ margin)." if rom is not None else ""
    if not pn.get("available"):
        cash = cash_needed(m, (p.get("eod_outcome") or {}).get("net_premium"))
        tail += f" {cash_text(cash)[0].upper() + cash_text(cash)[1:]}." if cash else ""
    which = margin_file_text(m, plain=short)
    hedged = fno_margin.hedged_text(m)
    if short:
        return f"Margin {fno_margin.margin_text(m)} — {which}.{tail}" + (f" {hedged}." if hedged else "")
    extra = " ".join(x[0].upper() + x[1:] + "." for x in (hedged, fno_margin.held_extra_text(m), fno_margin.intraday_text(m),
                                                         m.get("bod_note")) if x)
    return (f"Margin {fno_margin.margin_text(m)} — {which}; {m['basis']}.{tail} " + (extra + " " if extra else "") +
            "Premium for bought legs is paid on top; premium received on short legs is not counted towards margin here.")


def margin_file_text(m, plain=False):
    """Which SPAN file the margin is on, in words; `plain` leaves out the file name (summaries)."""
    label = m.get("file_label") or ""
    if m.get("margin_basis") == "next_session":
        created = m.get("created") or ""
        made = (f", published {day_text(created[:4] + '-' + created[4:6] + '-' + created[6:8])} {created[8:10]}:{created[10:12]} IST"
                if len(created) >= 12 else "")
        name = "" if plain else f" ({m['file']})"
        return (f"NSE Clearing's start-of-day margin parameters for {day_text(m['next_session'])}{name}{made}, the file Zerodha's "
                "calculator matched at the weekend (21 Nov 2026)")
    if m.get("margin_basis") == "closing":
        created = m.get("created") or ""
        when = f"{created[8:10]}:{created[10:12]} IST " if len(created) >= 12 else ""
        if plain:
            return f"NSE Clearing's closing margin parameters ({when}today), {fno_margin.AFTER_CLOSE_NOTE}"
        return (f"NSE Clearing's closing SPAN file ({m['file']}, {when}{label}), {fno_margin.AFTER_CLOSE_NOTE}; exposure on "
                f"the close {fmt(m['underlying_close'])}")
    if plain:
        if label.endswith("end-of-day file") or label.endswith("file i1"):
            when = (f"the {day_text(m['business_date'])} close" if label.endswith("end-of-day file") and m.get("business_date")
                    else "the previous close")
            return f"NSE Clearing's start-of-day margin parameters from {when}, the ones broker calculators matched"
        return "NSE Clearing's intraday margin parameters, priced at that file's levels"
    if label.endswith("end-of-day file"):
        return (f"NSE Clearing's begin-of-day SPAN parameters ({m['file']}, from the {day_text(m['business_date'])} close), "
                "the file broker calculators matched")
    if label.endswith("file i1"):
        return (f"NSE Clearing's begin-of-day SPAN parameters ({m['file']}, built from the previous close), "
                "the file broker calculators matched")
    return f"NSE Clearing SPAN file {m['file']} ({label}), priced at that file's levels"


def now_scenario_lines(pn):
    sc, out = pn.get("scenarios"), []
    if sc:
        out.append("- If placed now, P&L at expiry after costs: " + " · ".join(
            f"{x['sd']:+d} SD {fmt(x['level'], 0)} {rupees(x['pnl_after_costs'] if x['pnl_after_costs'] is not None else x['pnl'], 0, True)}"
            for x in sc["expiry"]))
        if sc["gaps"]:
            up, down, shock = sc["gaps"]
            out.append(f"- Overnight gap of 3 one-session SD ({up['move_pct'] * 100:+.1f}% / {down['move_pct'] * 100:+.1f}%), marked next "
                       f"morning: up {rupees(up['pnl'], 0, True)}, down {rupees(down['pnl'], 0, True)}, down with IV +5 pts "
                       f"{rupees(shock['pnl'], 0, True)}")
    rng = pn.get("range_1sd")
    if rng:
        out.append(f"- 68% implied range at expiry, from now: {fmt(rng[0], 0)}–{fmt(rng[1], 0)} ({PROB_LABEL})")
    return out


def payoff_markdown(r, p):
    lines = report_preamble(r)
    lines += [f"# {r['underlying']} strategy payoff — expiry {r['expiry']}{expiry_tag(r)}", "",
              f"Data: {r['source']} for {r['data_date']} · spot {fmt(r['spot'])} ({close_label(r)}) · lot size {p['lot_size']}{live_text(r)}", "",
              f"> {r['disclaimer']}", ""]
    pn = p.get("placed_now") or {}
    if pn.get("available"):
        lines += [f"## {now_title(pn, r['underlying'])}", ""]
        lines += comparison_lines(r, p) + [""] + now_scenario_lines(pn)
        lines += ["", f"How 'now' is estimated: {pn['basis']}", "", margin_line(p), "",
                  f"## At the {day_text(r['data_date'])} close (end-of-day data, for reference)", ""]
    elif pn:
        lines += [pn["note"], ""]
    lines += ["| Leg | Price used | IV | Units | Entry charges ₹ |", "| --- | ---: | ---: | ---: | ---: |"]
    costs = p["costs"]
    for i, l in enumerate(p["legs"]):
        charge = fmt(costs["entry"][i]["total"]) if costs["available"] else "n/a"
        lines.append(f"| {leg_name(l)} | {fmt(l['price'])} | {pct(l['iv']) if l['type'] != 'FUT' else ''} | {l['units']:+,} | {charge} |")
    g, a = p["position_greeks"], p["after_costs"]
    before = lambda key: rupees(p[key], 2) + (" (only if the price fell to zero)" if p[key] is not None and
                                              p["extreme_at_zero"]["profit" if key == "max_profit" else "loss"] else "")
    after = lambda key: (rupees(a[key], 2) if key != "breakevens" else (", ".join(fmt(b) for b in a["breakevens"]) or "none in range")) if a else "n/a"
    lines += ["", "| Result at expiry | Before costs | After costs |", "| --- | ---: | ---: |",
              f"| Net premium ({'received' if p['net_premium'] > 0 else 'paid'}) | ₹{fmt(abs(p['net_premium']))} | "
              f"{'₹' + fmt(abs(a['net_premium'])) + ' (after entry charges)' if a else 'n/a'} |",
              f"| Maximum profit | {before('max_profit')} | {after('max_profit')} |",
              f"| Maximum loss | {before('max_loss')} | {after('max_loss')} |",
              f"| Breakeven(s) | {', '.join(fmt(b) for b in p['breakevens']) or 'none in range'} | {after('breakevens')} |"]
    if costs["available"]:
        card = costs["card"]
        lines += ["", f"Charges: entry ₹{fmt(costs['entry_total'])}; exit at today's spot ₹{fmt(costs['exit_at_spot'])} "
                      f"(round trip ₹{fmt(costs['round_trip_at_spot'])}). Rate card {card.get('broker')} ({card.get('source')}, checked "
                      f"{card.get('verified_on')}, {card.get('status')}). {costs['basis']}"]
    if g:
        lines += ["", f"Position Greeks now: delta {fmt(g['delta'], 1)} units, gamma {fmt(g['gamma'], 3)}, theta ₹{fmt(g['theta'])}/day, vega ₹{fmt(g['vega'])} per vol point."]
    pr = p["probabilities"]
    lines += ["", f"## Probabilities at expiry ({PROB_LABEL})", ""]
    if pr["available"]:
        rng = pr["range_1sd"]
        both = lambda skew, flat: f"| {pct(skew)} | {pct(flat)} |"
        lines += [f"Risk-neutral, on the forward {fmt(pr['forward'])}, {fmt(pr['years'] * 365, 1)} days to expiry. Skew column: "
                  f"{pr['skew_basis']}. Flat column: one volatility, {pct(pr['vol'])} ({pr['vol_basis']}). Both ignore closing early.", "",
                  "| Measure | Each strike's own IV (skew) | Flat ATM IV |", "| --- | ---: | ---: |"]
        if pr.get("profit_after_costs") is not None:
            lines.append("| Probability of profit at expiry, after costs " + both(pr["profit_after_costs"], pr["profit_after_costs_flat"]))
        lines.append("| Probability of profit at expiry, before costs " + both(pr["profit_skew"], pr["profit"]))
        lines.append("| Chance of the maximum loss " + (both(pr["max_loss_chance"], pr["max_loss_chance_flat"]) if pr["max_loss_chance"] is not None
                                                     else "| no cap: the loss grows with the move | |"))
        for b in pr.get("above_breakevens_after_costs") or []:
            lines.append(f"| Finishing above breakeven after costs {fmt(b['breakeven'])} " + both(b["prob_above"], b["prob_above_flat"]))
        for b in pr["above_breakevens"]:
            lines.append(f"| Probability of finishing above breakeven {fmt(b['breakeven'])} (before costs) " + both(b["prob_above_skew"], b["prob_above"]))
        lines.append(f"| 68% range at expiry (±1 SD, flat ATM IV) | {fmt(rng[0], 0)} – {fmt(rng[1], 0)} | |")
        ev = p["expected_value"]
        if ev:
            lines += ["", f"Expected value if held to expiry, at implied prices: {rupees(ev['before_costs'], 0, True)} before costs, "
                          f"{rupees(ev['after_costs'], 0, True) if ev['after_costs'] is not None else 'n/a'} after costs. {ev['basis'][0].upper() + ev['basis'][1:]}"]
    else:
        lines.append(pr["note"])
    sc = p["scenarios"]
    if sc:
        lines += ["", "## Stress: ±1/2/3 SD at expiry and overnight gaps", "", "| Scenario | Underlying | P&L ₹ (after costs) |", "| --- | ---: | ---: |"]
        for x in sc["expiry"]:
            lines.append(f"| At expiry, {x['sd']:+d} SD | {fmt(x['level'], 0)} | {fmt(x['pnl_after_costs'] if x['pnl_after_costs'] is not None else x['pnl'], 0)} |")
        for x in sc["gaps"]:
            lines.append(f"| Next morning, {x['label']} ({x['move_pct'] * 100:+.1f}%) | {fmt(x['level'], 0)} | {fmt(x['pnl'], 0)} |")
        lines += ["", f"Basis: {sc['basis']}."]
    lines += ["", "## P&L by underlying price", "", "| Underlying | At expiry ₹ | At expiry after costs ₹ | Today (T+0) ₹ | Note |",
              "| ---: | ---: | ---: | ---: | --- |"]
    for t in p["table"]:
        s = t["price"]
        lines.append(f"| {fmt(s, 2 if s != round(s) else 0)} | {fmt(t['expiry_pnl'], 0)} | {fmt(t['expiry_pnl_after_costs'], 0)} | "
                     f"{fmt(t['today_pnl'], 0)} | {', '.join(t['marks'])} |")
    lines += ["", p["today_note"] + f" The spot row is {close_label(r)}, where entry is priced, so its T+0 P&L is about zero."]
    if p.get("live_mark"):
        lines += ["", live_mark_text(p["live_mark"])]
    if not pn.get("available"):
        lines += ["", margin_line(p)]
    lines += ["", p["costs_note"].replace(" " + MARGIN_NOTE, "") if p.get("margin") is not None else p["costs_note"], ""]
    return "\n".join(lines)


def live_mark_text(m):
    if m.get("close") and m.get("pnl") is not None:
        how = ("at the 'If placed now' prices" if m.get("source") == "nse_intraday_snapshot" else f"with IVs held at {m['iv_from']}")
        return (f"T+0 at {now_where(m)}{now_when(m)}: {rupees(m['pnl'], 0, True)} before costs — an estimate {how}; tomorrow's "
                "prices will differ, so check your broker's quotes.")
    if m.get("pnl") is None:
        return f"Live level {fmt(m['level'])} at {m['at'][11:16]} IST; T+0 not computed (a leg has no IV)."
    if m.get("source") == "nse_intraday_snapshot":
        return (f"T+0 at the live level {fmt(m['level'])} ({m['at'][11:16]} IST): {rupees(m['pnl'], 0, True)} before costs — an estimate "
                f"at the 'If placed now' prices ({m['iv_from']}, each option moved to the live level at its own snapshot IV); live "
                "option prices will differ, so check your broker's quotes.")
    return (f"T+0 at the live level {fmt(m['level'])} ({m['at'][11:16]} IST): {rupees(m['pnl'], 0, True)} before costs — an estimate "
            f"with IVs held at {m['iv_from']}; live option prices will differ, so check your broker's quotes.")


def chain_summary(r, vix=None):
    dod = r.get("day_over_day") or {"status": "skipped", "note": "Day-over-day comparison not run."}
    ok = dod["status"] == "ok"
    change = lambda key: dod["metrics"][key]["change"] if ok else None
    return {"schema": "fno_chain_summary_v1", "underlying": r["underlying"], "expiry": r["expiry"], "days_to_expiry": r["days_to_expiry"],
            **{k: r.get(k) for k in ("expiry_type", "expiry_weekday", "expiry_moved", "expiry_usual_date", "expiry_holiday", "usual_expiry_weekday",
                                     "has_weeklies", "expiry_note", "one_day_move", "selection_note", "underlying_assumption", "session",
                                     "session_ranges", "live", "participants", "strike_focus")},
            "data_age_line": data_age_line(r.get("session")), "expiry_lead": expiry_lead(r), "sessions_left": r.get("sessions_left"),
            "data_date": r["data_date"], "source": r["source"], "spot": r["spot"], "forward": r["forward"], "lot_size": r["lot_size"],
            "pcr_oi": r["pcr_oi"], "pcr_oi_change": change("pcr_oi"), "max_pain": r["max_pain"], "max_pain_change": change("max_pain"),
            "atm_iv": r["atm_iv"], "atm_iv_change": change("atm_iv"), "previous_date": dod.get("previous_date"),
            "previous_sessions": dod.get("sessions") if ok else None, "previous_sessions_certain": dod.get("sessions_certain", True),
            "now_estimate": r.get("now_estimate"), "post_close_note": r.get("post_close_note"), "nse_chain": r.get("nse_chain"),
            # A chain-level expiry warning only when the selected symbol itself expired inside the comparison window; the
            # participant files cover every index, so their expiry caveat rides on the participant line.
            "warnings": [x for x in (dod.get("gap_note"), (r.get("participants") or {}).get("gap_note"), dod.get("expiry_caveat")) if x],
            "day_over_day_note": None if ok else dod["note"], "expected_move": r["straddle"], "expected_move_iv": r["expected_move_iv"],
            "range_1sd": r.get("range_1sd"), "vix": vix, "resistance_oi": r["resistance_oi"], "support_oi": r["support_oi"],
            "oi_walls": oi_walls(r),
            "oi_walls_near": r.get("oi_walls_near"), "max_pain_vs_spot": r.get("max_pain_vs_spot"),
            "call_oi_change": r["call_oi_change"], "put_oi_change": r["put_oi_change"], "_live": r.get("_live"),
            "disclaimer": DISCLAIMER, "execution_eligible": False, "submitted": False}


def short_source(source):
    if source.startswith("NSE option chain"):
        return source.split(" (https://", 1)[0]
    return "NSE F&O bhavcopy, end of day" if source.startswith("NSE F&O bhavcopy") else source


def summary_preamble(s):
    out = [x for x in (s.get("data_age_line"), s.get("post_close_note"), s.get("expiry_lead")) if x]
    for key in ("selection_note", "underlying_assumption"):
        if s.get(key):
            out.append(f"**Note:** {s[key]}")
    if s.get("strike_resolution"):
        out.append(f"**Strikes:** {s['strike_resolution']}")
    return out


def now_estimate_text(n, plain=False):
    """'Now (estimate, not live quotes): ATM 25,250 straddle 290.10 (CE 140.25 + PE 149.85) · ATM IV 14.6% — …'; `plain`
    names the source in everyday words (the summary), otherwise the clearing-house file and pricing model (full report)."""
    if not n:
        return None
    if not n.get("available"):
        return f"Now (estimate): {n['note']}"
    legs = (f" (CE {fmt(n['ce'])} + PE {fmt(n['pe'])})" if n.get("ce") and n.get("pe") else "")
    straddle = f"straddle {fmt(n['straddle'])}{legs}" if n.get("straddle") else "straddle n/a"
    basis = n.get("basis_plain") if plain and n.get("basis_plain") else n["basis_short"]
    return (f"Now (estimate, not live quotes): ATM {fmt(n['atm_strike'], 0)} {straddle} · ATM IV {pct(n['atm_iv'])} — {basis}")


def live_point(s):
    """(level, 'live 25,158 (13:43 IST)' or 'the NSE close 62,039 (Wed 18 Nov)') of the level the report quotes now — the
    'now' estimate's, else the live or closing level — or None."""
    n = s.get("now_estimate") or {}
    x = n if n.get("available") and n.get("level") else (s.get("live") if not s.get("_live") else None)
    if not x or not x.get("level"):
        return None
    if x.get("close"):
        name = "the NSE close" if x.get("provider") == "NSE" else "today's close"
        return x["level"], f"{name} {fmt(x['level'], 0)} ({day_text(x['at'][:10])})"
    return x["level"], f"live {fmt(x['level'], 0)} ({x['at'][11:16]} IST)"


def pain_distance(s):
    """' — 3.7% below spot (the Tue 17 Nov close), 2.9% below live 25,158 (13:43 IST)' for the max-pain line."""
    d = s.get("max_pain_vs_spot")
    if d is None:
        return ""
    where = "spot" if s.get("_live") else f"spot (the {day_text(s['data_date'])} close)"
    text = f" — at {where}" if abs(d) < 0.0005 else f" — {abs(d) * 100:.1f}% {'below' if d < 0 else 'above'} {where}"
    point = live_point(s)
    if point:
        e = s["max_pain"] / point[0] - 1
        text += (f", at {point[1]}" if abs(e) < 0.0005 else f", {abs(e) * 100:.1f}% {'below' if e < 0 else 'above'} {point[1]}")
    return text


def oi_walls(r):
    """(where, call walls, put walls): the three largest-OI call strikes at or above the level the report quotes now and put
    strikes at or below it. 'where' is 'spot' when that level is the chain's own spot, else the live or closing level named
    ('the NSE close 63,063 (Thu 19 Nov)'), so a wall the market has already passed is never called 'above spot'."""
    point = live_point(r)
    books = r.get("_books")
    if not point or not books or abs(point[0] / r["spot"] - 1) < 1e-4:
        return "spot", r["resistance_oi"], r["support_oi"]
    calls, puts = books
    level = point[0]
    above = sorted((k for k in calls if k >= level), key=lambda k: calls[k]["oi"], reverse=True)[:3]
    below = sorted((k for k in puts if k <= level), key=lambda k: puts[k]["oi"], reverse=True)[:3]
    return point[1], above, below


def straddle_sd(straddle, rng):
    """The ATM straddle as a multiple of the 1-SD move to expiry (half the 68% range): ≈0.8 for fairly priced options."""
    if not straddle or not rng or rng[1] <= rng[0]:
        return None
    return straddle / ((rng[1] - rng[0]) / 2)


def near_walls_text(near):
    """'OI walls near spot (largest OI within the 1-SD range to expiry, 61,000–63,600): calls 62,500, 63,000 · puts 62,000,
    61,000 — the levels to quote for the next session' or None."""
    if not near or not (near.get("calls") or near.get("puts")):
        return None
    lo, hi = near["window"]
    return (f"OI walls near spot (largest OI within the {near['basis']}, {fmt(lo, 0)}–{fmt(hi, 0)}): calls "
            f"{', '.join(fmt(k, 0) for k in near['calls']) or 'n/a'} · puts {', '.join(fmt(k, 0) for k in near['puts']) or 'n/a'} — "
            "the nearer levels for the next session")


def expected_move_line(s, move, rng):
    """'- Expected move to expiry ±342.80 (1.4%) by the ATM straddle; 68% implied range 24,850–25,530 — from live 25,189.25 at
    12:55 IST, snapshot IV' during market hours (the close-based figures are centred on a level the market has left), else the
    close-based figures labelled with their close."""
    n = s.get("now_estimate") or {}
    if n.get("available") and (n.get("straddle") or n.get("range_1sd")):
        parts = []
        if n.get("straddle"):
            ratio = straddle_sd(n["straddle"], n.get("range_1sd"))
            parts.append(f"±{fmt(n['straddle'])} ({pct(n['straddle'] / n['level'])}) by the now ATM straddle"
                         + (f" (≈ {ratio:.1f} SD)" if ratio else ""))
        if n.get("range_1sd"):
            parts.append(f"68% implied range (1 SD) {fmt(n['range_1sd'][0], 0)}–{fmt(n['range_1sd'][1], 0)}"
                         + (f" around the forward {fmt(n['forward'], 0)}" if n.get("forward") else ""))
        if n.get("range_1sd") and (s.get("sessions_left") or 2) <= 1:
            parts = parts[-1:]
        return (f"- Expected move to expiry {'; '.join(parts)} — from {now_where(n)}"
                f"{now_when(n) if n.get('close') else ' at ' + n['at'][11:16] + ' IST'}, "
                f"{n.get('iv_source', 'snapshot IV')}")
    where = "" if s.get("_live") else f" — from the {day_text(s['data_date'])} close {fmt(s['spot'])}"
    around = f" around the forward {fmt(s['forward'], 0)}" if s.get("forward") else ""
    if rng and (s.get("sessions_left") or 2) <= 1:
        half = (rng[1] - rng[0]) / 2
        return (f"- Expected move for the {day_text(s['expiry'])} expiry session: 1 SD ±{fmt(half, 0)} ({pct(half / s['spot'], 2)}) — "
                f"68% implied range {fmt(rng[0], 0)}–{fmt(rng[1], 0)}{around}{where}; {PROB_LABEL}")
    ratio = straddle_sd(s.get("expected_move"), rng)
    return (f"- Expected move to expiry {move}" + (f" (≈ {ratio:.1f} SD)" if ratio else "")
            + (f"; 68% implied range (1 SD) {fmt(rng[0], 0)}–{fmt(rng[1], 0)}{around}" if rng else "") + where)


def chain_summary_markdown(s):
    sessions = s.get("previous_sessions") or 1
    since = (f"{s['previous_date']}, {sessions_label(sessions, s.get('previous_sessions_certain', True))}" if sessions > 1
             else s.get("previous_date"))
    vs = lambda c, text: "" if c is None else f" ({text} vs {since})"
    lot = s["lot_size"]
    move = (f"±{fmt(s['expected_move'])} ({pct(s['expected_move'] / s['spot'])}) by ATM straddle" if s["expected_move"] else
            f"±{fmt(s['expected_move_iv'])} (IV-implied)" if s["expected_move_iv"] else "n/a")
    rng = s["range_1sd"]
    kind = f"{s['expiry_type']}, " if s.get("expiry_type") else ""
    closing = "" if s.get("_live") else " close"
    lines = summary_preamble(s)
    lines.append(f"**{s['underlying']} options, expiry {s['expiry']} ({kind}{s['days_to_expiry']:.1f} days)** — data {s['data_date']}{closing} "
                 f"({short_source(s['source'])})")
    if (s.get("expiry_moved") or s.get("has_weeklies") is False) and not s.get("expiry_lead"):
        lines.append(f"- {s['expiry_note']}")
    live = s.get("live") if not s.get("_live") else None
    spot = f"- Spot {fmt(s['spot'])}" + (f" ({day_text(s['data_date'])} close{', NSE' if s.get('nse_chain') else ''})" if not s.get("_live") else "")
    if live and live.get("close"):
        spot += f" · {now_where(live)}{now_when(live)} ({(live['level'] / s['spot'] - 1) * 100:+.2f}%)"
    elif live:
        spot += f" · live {fmt(live['level'])} at {live['at'][11:16]} IST ({(live['level'] / s['spot'] - 1) * 100:+.2f}%, Yahoo, unofficial)"
    lines.append(spot)
    if s.get("nse_chain"):
        lines.append(f"- {s['nse_chain']['note']}")
    now_line = now_estimate_text(s.get("now_estimate"), plain=True)
    if now_line:
        lines.append(f"- {now_line}")
    # The next session gets its own block at the end: its 1-SD range (today's range from the last close is stale during
    # the session) and the OI walls near the live level.
    ranges = [m for m in (s.get("session_ranges") or []) if m.get("status") != "in progress"]
    if (s.get("sessions_left") or 2) <= 1:
        ranges = []  # the expiry session's single 1-SD figure is on the expected-move line
    nxt = []
    if ranges:
        nxt += [f"- {range_text(m)}" for m in ranges]
        iv = ranges[-1]
        nxt[-1] += (f" — at the now ATM IV {pct(iv['iv'])}" if iv.get("iv_source") == "now" else
                    f" — at the {day_text(s['data_date'])} close's ATM IV {pct(iv['iv'])}") + f"; {PROB_LABEL}"
    elif not s.get("session_ranges") and (s.get("sessions_left") or 2) > 1:
        nxt.append(f"- 1-sigma move {one_day_text(s.get('one_day_move'))} (ATM IV × √(1/{TRADING_DAYS}); {PROB_LABEL})")
    near = near_walls_text(s.get("oi_walls_near"))
    if near:
        nxt.append(f"- {near}")
    if s.get("warnings"):
        lines.append("- ⚠ " + " · ".join(w[0].upper() + w[1:] for w in s["warnings"]))
    lines += [f"- PCR (OI) {fmt(s['pcr_oi'])}{vs(s['pcr_oi_change'], signed(s['pcr_oi_change']))}",
              f"- Max pain {fmt(s['max_pain'], 0)}{vs(s['max_pain_change'], signed(s['max_pain_change'], 0))}{pain_distance(s)}",
              f"- ATM IV {pct(s['atm_iv'])}{vs(s['atm_iv_change'], signed(s['atm_iv_change'] * 100 if s['atm_iv_change'] is not None else None, 1) + ' pts')}"
              + (f" at the close · now ≈ {pct(s['now_estimate']['atm_iv'])}" if (s.get("now_estimate") or {}).get("available") else ""),
              expected_move_line(s, move, rng)]
    if s.get("strike_focus"):
        lines.append(f"- {s['strike_focus']['text']}")
    if s["vix"]:
        lines.append(f"- {vix_summary_text(s['vix'])}")
    where, res, sup = s.get("oi_walls") or ("spot", s["resistance_oi"], s["support_oi"])
    lines += [f"- Call OI walls above {where}: {', '.join(fmt(k, 0) for k in res) or 'n/a'} · put OI walls below {where}: "
              f"{', '.join(fmt(k, 0) for k in sup) or 'n/a'}" + (" (biggest overall)" if near else ""),
              f"- Net OI change on the day: calls {signed_contracts(s['call_oi_change'], lot)}, puts {signed_contracts(s['put_oi_change'], lot)} contracts"]
    po = participant_summary_line(s.get("participants"), shown=s.get("warnings") or ()) if participants_notable(s.get("participants")) else None
    if po:
        lines.append(f"- {po}")
    if s["day_over_day_note"]:
        lines.append(f"- Day over day: {s['day_over_day_note']}")
    if nxt:
        target = ranges[-1] if ranges else None
        title = (f"**Next session — {day_text(target['session'])} ({target['status']})**" if target else
                 f"**Next session — {day_text(s['expiry'])}, the expiry session**" if (s.get("sessions_left") or 2) <= 1
                 else "**Next session**")
        lines += ["", title] + nxt
    lines += ["", "Positioning from public exchange data; not a prediction and not investment advice. Run without --summary for the "
                  "full report (with the futures level, participant-wise OI, how 'now' is priced and the data files).", ""]
    return "\n".join(lines)


VIX_EXTREME = 0.10          # the summary quotes India VIX's 1-year percentile only in its top or bottom 10%
FII_LONG_EXTREME = 0.25     # … and the participant-OI line only when FIIs are ≤25% or ≥75% long in index futures


def vix_summary_text(vix):
    """'India VIX 15.80 (close 2026-11-19)' — with its 1-year percentile and range only when that is extreme (top or bottom
    10%), since a middling percentile does not change the read; the full report always has it."""
    p = vix.get("percentile_1y")
    text = f"India VIX {fmt(vix['vix'])} (close {vix['date']})"
    if p is not None and (p <= VIX_EXTREME or p >= 1 - VIX_EXTREME):
        text = f"India VIX {vix_line(vix)}" + (f" — {vix['note']}" if vix.get("note") else "")
    return text


def participants_notable(po):
    """True when NSE's participant OI is worth a summary line: FIIs one-sided in index futures (≤25% or ≥75% long)."""
    if not po or po.get("status") != "ok":
        return False
    share = ((po.get("rows") or {}).get("FII") or {}).get("fut_long_pct")
    return share is not None and (share <= FII_LONG_EXTREME or share >= 1 - FII_LONG_EXTREME)


def payoff_summary(r, p):
    pr = p["probabilities"]
    return {"schema": "fno_payoff_summary_v1", "underlying": r["underlying"], "expiry": r["expiry"], "data_date": r["data_date"],
            "expiry_type": r.get("expiry_type"), "expiry_moved": r.get("expiry_moved"), "expiry_note": r.get("expiry_note"),
            "expiry_usual_date": r.get("expiry_usual_date"), "expiry_holiday": r.get("expiry_holiday"),
            "selection_note": r.get("selection_note"), "underlying_assumption": r.get("underlying_assumption"),
            "data_age_line": data_age_line(r.get("session")), "session": r.get("session"), "live": r.get("live"), "_live": r.get("_live"),
            "expiry_lead": expiry_lead(r), "sessions_left": r.get("sessions_left"), "forward": r["forward"],
            "strike_resolution": r.get("strike_resolution"),
            "post_close_note": r.get("post_close_note"),
            "source": r["source"], "spot": r["spot"], "atm_iv": r["atm_iv"], "expected_move": r["straddle"],
            "legs": [{k: l[k] for k in ("side", "type", "strike", "lots", "price", "units", "iv")} for l in p["legs"]], "lot_size": p["lot_size"],
            "net_premium": p["net_premium"], "max_profit": p["max_profit"], "max_loss": p["max_loss"], "extreme_at_zero": p["extreme_at_zero"],
            "breakevens": p["breakevens"], "probability_of_profit": pr.get("profit"), "above_breakevens": pr.get("above_breakevens", []),
            "probability_of_profit_after_costs": pr.get("profit_after_costs"), "probability_of_profit_after_costs_flat": pr.get("profit_after_costs_flat"),
            "max_loss_chance": pr.get("max_loss_chance"), "max_loss_chance_flat": pr.get("max_loss_chance_flat"), "skew_basis": pr.get("skew_basis"),
            "above_breakevens_after_costs": pr.get("above_breakevens_after_costs", []), "after_costs": p["after_costs"], "costs": p["costs"],
            "expected_value": p["expected_value"], "scenarios": p["scenarios"], "margin_note": p["margin_note"],
            "range_1sd": pr.get("range_1sd"), "probability_label": PROB_LABEL, "probability_note": pr.get("note"),
            "today_pnl_at_spot": next((t["today_pnl"] for t in p["table"] if "spot" in t["marks"]), None), "live_mark": p.get("live_mark"),
            "costs_note": p["costs_note"], "margin": p.get("margin"), "return_on_margin": p.get("return_on_margin"),
            "eod_outcome": p.get("eod_outcome"), "placed_now": p.get("placed_now"), "held": bool(p.get("held")),
            "disclaimer": DISCLAIMER, "execution_eligible": False, "submitted": False}


def plain_now_basis(pn, underlying):
    """How the 'if placed now' prices are made, without file names or model names (those stay in the full report)."""
    snap = pn.get("snapshot")
    if snap and pn.get("close"):
        return (f"option prices from NSE's {snap['at'][11:16]} IST {'closing' if snap['at'][11:16] >= '15:30' else 'intraday'} snapshot "
                f"(NSE Clearing), each moved to {now_where(pn, underlying)} at its own implied volatility. Today's closing prices, not "
                f"live quotes ({CLOSING_PRICE_NOTE}): tomorrow's open can differ, so check your broker's option chain before trading.")
    if snap:
        return (f"option prices from NSE's {snap['at'][11:16]} IST intraday snapshot, each moved to live {underlying} {fmt(pn['level'])} "
                f"({pn['at'][11:16]} IST) at its own implied volatility. An hourly exchange snapshot, not live quotes: check your "
                "broker's option chain before trading.")
    return (f"each option re-priced at {now_where(pn, underlying)}{now_when(pn)} with its implied volatility from "
            "the last close" + (", adjusted for India VIX's move today" if pn.get("vix") else "")
            + ". A model, not live quotes: check your broker's option chain before trading.")


def single_leg_lead(s):
    """For one option leg whose 'now' price differs materially (≥ 20%) from the last close: 'Selling now would get about
    ₹19.05 (≈ ₹1,238 for 1 lot of 65; estimate) — not the ₹51.45 of the Tue 17 Nov close.' None otherwise."""
    pn = s.get("placed_now") or {}
    legs = [l for l in s.get("legs") or [] if l["type"] != "FUT"]
    if len(legs) != 1 or len(s["legs"]) != 1 or not pn.get("legs"):
        return None
    now, close = pn["legs"][0]["now"], pn["legs"][0]["close"]
    if not now or not close or abs(now / close - 1) < 0.2:
        return None
    leg, units = legs[0], abs(legs[0]["units"])
    verb = "Selling now would get" if leg["side"] == "SELL" else "Buying now would cost"
    lots = f"{leg['lots']} lot{'s' if leg['lots'] != 1 else ''} of {s['lot_size']}"
    return (f"**{verb} about ₹{fmt(now)} a unit (≈ {rupees(now * units)} for {lots}; estimate) — not the ₹{fmt(close)} of the "
            f"{day_text(s['data_date'])} close**, which the right-hand column keeps for reference.")


def payoff_now_summary_markdown(s):
    """Answer-first summary when the market is open: the 'if placed now' estimate leads, the close's figures alongside."""
    pn = s["placed_now"]
    legs = " · ".join(leg_name(l) for l in s["legs"])
    kind = (f" ({s['expiry_type']}" + (f", moved from {s.get('expiry_usual_date')} — " + moved_reason(s.get("expiry_holiday"))
                                      if s.get("expiry_moved") and not s.get("expiry_lead") else "") + ")") if s.get("expiry_type") else ""
    lines = summary_preamble(s)
    lines += [f"**{s['underlying']} payoff, expiry {s['expiry']}{kind}** — {legs} (lot {s['lot_size']})",
              f"**{now_title(pn, s['underlying'])} · {pn['years'] * 365:.1f} days left**", ""]
    lead = single_leg_lead(s)
    if lead:
        lines += [lead, ""]
    lines += comparison_lines({"data_date": s["data_date"]}, s) + [""]
    lines += now_scenario_lines(pn)
    ev = pn.get("expected_value")
    if ev and ev.get("after_costs") is not None:
        lines.append(f"- Expected value if held to expiry at implied prices {rupees(ev['after_costs'], 0, True)} after costs "
                     "(option prices are fair by construction; any edge needs a view that differs from the market)")
    lines.append(f"- How 'now' is estimated: {plain_now_basis(pn, s['underlying'])}")
    lines.append(f"- {margin_line(s, short=True)}")
    if s.get("held") and s.get("live_mark") and s["live_mark"].get("pnl") is not None:
        m = s["live_mark"]
        how = ("at the 'If placed now' prices above, same intraday snapshot" if m.get("source") == "nse_intraday_snapshot"
               else "IVs held at the close, not the 'If placed now' prices")
        lines.append(f"- Already in this trade from the {day_text(s['data_date'])} close? Model P&L now {rupees(m['pnl'], 0, True)} "
                     f"before costs ({how}).")
    lines += ["", f"Right-hand column: {short_source(s['source'])} for {s['data_date']}. Charges from the example NFO rate card; "
                  "slippage excluded. Not investment advice. Run without --summary for the full report.", ""]
    return "\n".join(lines)


def payoff_summary_markdown(s):
    if (s.get("placed_now") or {}).get("available"):
        return payoff_now_summary_markdown(s)
    legs = " · ".join(f"{leg_name(l)} @ {fmt(l['price'])}" + (f" (IV {pct(l['iv'])})" if l.get("iv") else "") for l in s["legs"])
    a = s["after_costs"]
    extreme = lambda key: "unlimited" if s[key] is None else rupees(s[key], 0) + (" (only at zero)" if s["extreme_at_zero"]["profit" if key == "max_profit" else "loss"] else "")
    rng = s["range_1sd"]
    kind = (f" ({s['expiry_type']}" + (f", moved from {s.get('expiry_usual_date')} — " + moved_reason(s.get("expiry_holiday"))
                                      if s.get("expiry_moved") and not s.get("expiry_lead") else "") + ")") if s.get("expiry_type") else ""
    live = s.get("live") if not s.get("_live") else None
    lines = summary_preamble(s)
    lines += [f"**{s['underlying']} payoff, expiry {s['expiry']}{kind}** — data {s['data_date']}{'' if s.get('_live') else ' close'} "
              f"({short_source(s['source'])})",
              f"- Legs: {legs} (lot {s['lot_size']})",
              f"- Spot {fmt(s['spot'])}" + ("" if s.get("_live") else f" ({day_text(s['data_date'])} close)") +
              (f" · live {fmt(live['level'])} at {live['at'][11:16]} IST" if live else "") +
              f" · ATM IV {pct(s['atm_iv'])} · ATM straddle ±{fmt(s['expected_move'])}"]
    if a:
        costs = s["costs"]
        lines += [f"- Net premium {'received' if s['net_premium'] > 0 else 'paid'} ₹{fmt(abs(s['net_premium']))}; "
                  f"₹{fmt(abs(a['net_premium']))} after entry charges ₹{fmt(costs['entry_total'])} (round trip ≈ ₹{fmt(costs['round_trip_at_spot'])})",
                  f"- Max profit ₹{fmt(a['max_profit'], 0) if a['max_profit'] is not None else 'unlimited'} after costs "
                  f"(gross {extreme('max_profit')}) · max loss {rupees(-a['max_loss'], 0) if a['max_loss'] is not None else 'unlimited'}"
                  + ((" after costs" + (f" (at {fmt(a['max_loss_level'], 0)}, the worst expiry level shown: exit STT on in-the-money "
                                        "legs grows with the move)" if a.get("max_loss_level") is not None else ""))
                     if a["max_loss"] is not None else " (no cap: it grows with the move)")]
        bes = ", ".join(f"{fmt(b['breakeven'])} (P above {pct(b['prob_above'])})" for b in s["above_breakevens_after_costs"]) or \
            (", ".join(fmt(b) for b in a["breakevens"]) or "none in range")
        lines.append(f"- Breakeven(s): {bes} after costs")
    else:
        bes = ", ".join(f"{fmt(b['breakeven'])} (P above {pct(b['prob_above'])})" for b in s["above_breakevens"]) or \
            (", ".join(fmt(b) for b in s["breakevens"]) or "none in range")
        lines += [f"- Net premium {'received' if s['net_premium'] > 0 else 'paid'} ₹{fmt(abs(s['net_premium']))}",
                  f"- Max profit {extreme('max_profit')} · max loss {extreme('max_loss')}", f"- Breakeven(s): {bes}"]
    if s["probability_of_profit"] is not None:
        if s.get("probability_of_profit_after_costs") is not None:
            pop, flat = s["probability_of_profit_after_costs"], s["probability_of_profit_after_costs_flat"]
            alongside = f"; {pct(flat)} at a flat ATM IV" if abs(pop - flat) >= 0.0005 else "; same at a flat ATM IV"
            lines.append(f"- Probability of profit at expiry {pct(pop)} after costs (each strike's own IV{alongside}; {PROB_LABEL})")
        else:
            lines.append(f"- Probability of profit at expiry {pct(s['probability_of_profit'])} ({PROB_LABEL})")
        ml = s.get("max_loss_chance")
        ev = s.get("expected_value")
        bits = [f"chance of the max loss {pct(ml)}" + (f" ({pct(s['max_loss_chance_flat'])} flat)" if abs(ml - s['max_loss_chance_flat']) >= 0.0005 else "")
                if ml is not None else "no maximum loss: it grows with the move"]
        if ev and ev.get("after_costs") is not None:
            bits.append(f"expected value if held to expiry at implied prices {rupees(ev['after_costs'], 0, True)} after costs "
                        f"({rupees(ev['before_costs'], 0, True)} before: option prices are fair by construction)")
        lines.append("- " + "; ".join(bits)[0].upper() + "; ".join(bits)[1:])
        sc = s.get("scenarios")
        if sc:
            lines.append("- At expiry (after costs): " + " · ".join(
                f"{x['sd']:+d} SD {fmt(x['level'], 0)} {rupees(x['pnl_after_costs'] if x['pnl_after_costs'] is not None else x['pnl'], 0, True)}"
                for x in sc["expiry"]))
            if sc["gaps"]:
                up, down, shock = sc["gaps"]
                lines.append(f"- Overnight gap of 3 one-session SD ({up['move_pct'] * 100:+.1f}% / {down['move_pct'] * 100:+.1f}%), marked next "
                             f"morning: up {rupees(up['pnl'], 0, True)}, down {rupees(down['pnl'], 0, True)}, down with IV +5 pts "
                             f"{rupees(shock['pnl'], 0, True)}")
        lines.append(f"- 68% implied range at expiry {fmt(rng[0], 0)}–{fmt(rng[1], 0)}"
                     + (f" (around the forward {fmt(s['forward'], 0)})" if s.get("forward") else ""))
    else:
        lines.append(f"- Probabilities: {s['probability_note']}")
    if s.get("live_mark"):
        lines.append(f"- {live_mark_text(s['live_mark'])}")
    elif s["today_pnl_at_spot"] is not None:
        where = f"the chain's spot {fmt(s['spot'])}" if s.get("_live") else f"the {day_text(s['data_date'])} close {fmt(s['spot'])}"
        lines.append(f"- Today (T+0) P&L at {where}: {rupees(s['today_pnl_at_spot'], 0)} (entry is priced at that close)")
    if (s.get("placed_now") or {}).get("note"):
        lines.append(f"- {s['placed_now']['note']}")
    lines.append(f"- {margin_line(s, short=True)}")
    tail = ("Charges from the example NFO rate card; slippage excluded." if a else "Excludes costs.")
    lines += ["", f"{tail} Not investment advice. Run without --summary for the full report.", ""]
    return "\n".join(lines)


# ---------------- strike grid ----------------

GRID_DELTAS = (0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.05)


def otm_traded(report, kind):
    """Traded out-of-the-money strikes with Greeks for one side: [(strike, |delta|)]."""
    side, f = kind.lower(), report["forward"]
    out = []
    for r in report["_rows"]:
        c = r.get(side)
        if c and c.get("close") and c.get("greeks") and ((kind == "CE" and r["strike"] > f) or (kind == "PE" and r["strike"] < f)):
            out.append((r["strike"], abs(c["greeks"]["delta"])))
    return out


def wing_strike(report, kind, short, width):
    """Traded strike nearest to short ± width, further out of the money than the short."""
    book = report["_books"][0 if kind == "CE" else 1]
    target = short + width if kind == "CE" else short - width
    pool = [k for k, c in book.items() if price_of(c) and ((kind == "CE" and k > short) or (kind == "PE" and k < short))]
    return min(pool, key=lambda k: (abs(k - target), k)) if pool else None


def row_figures(report, legs, card, span=None, trade_day=None):
    """Payoff figures of one grid row (legs like 'SELL:CE:25800:1') on `report`: credit, max profit/loss, breakevens and
    probabilities after costs, EV, ±SD P&L at expiry, margin and return on margin."""
    result = payoff(report, [parse_leg(x) for x in legs], card=card)
    pr, a, ev = result["probabilities"], result["after_costs"], result["expected_value"]
    margin = margin_for(report, result["legs"], span, trade_day or report["data_date"])
    best = a["max_profit"] if a else result["max_profit"]
    return {"credit": result["net_premium"], "credit_after_costs": a["net_premium"] if a else None,
            "max_profit_after_costs": best, "max_loss_after_costs": a["max_loss"] if a else result["max_loss"],
            "breakevens_after_costs": a["breakevens"] if a else result["breakevens"],
            "pop_after_costs": pr.get("profit_after_costs") if a else pr.get("profit_skew"),
            "pop_flat": pr.get("profit_after_costs_flat") if a else pr.get("profit"), "max_loss_chance": pr.get("max_loss_chance"),
            "ev_after_costs": ev.get("after_costs") if ev else None, "skew_basis": pr.get("skew_basis"),
            "sd_pnl": outcome(result)["sd_pnl"], "margin": margin, "return_on_margin": return_on_margin(best, margin)}


def reprice_grid(g, now, card, span=None, trade_day=None):
    """Re-price every grid row (strikes chosen at the close, so --row N is stable) for a trade placed now; the close's
    figures move to row['eod']. Rows whose legs cannot be priced now keep the close's figures and say so."""
    deltas = {(r["strike"], side.upper()): r[side]["greeks"]["delta"] for r in now["_rows"] for side in ("ce", "pe")
              if r.get(side) and r[side].get("greeks")}
    sd = now["atm_iv"] * math.sqrt(now["_years"]) if now["atm_iv"] else None
    for x in g["rows"]:
        keys = ("credit", "credit_after_costs", "max_profit_after_costs", "max_loss_after_costs", "breakevens_after_costs",
                "pop_after_costs", "pop_flat", "max_loss_chance", "ev_after_costs", "sd_pnl", "return_on_margin")
        eod = {k: x[k] for k in keys}
        try:
            fresh = row_figures(now, x["legs"], card, span, trade_day)
        except Block:
            x["now_note"] = "not re-priced (a leg has no IV); close figures shown"
            continue
        x.update(fresh, eod=eod)
        for s_ in x["shorts"]:
            s_["delta_close"] = s_["delta"]
            s_["delta"] = abs(deltas.get((s_["strike"], s_["type"]), s_["delta"]))
            s_["sd_away"] = math.log(s_["strike"] / now["forward"]) / sd if sd else None
    g["placed_now"] = now["_placed_now"]
    return g


FLY_WIDTHS = {"NIFTY": (100, 150, 200, 300), "BANKNIFTY": (300, 500, 700, 1000), "FINNIFTY": (100, 150, 200, 300),
              "MIDCPNIFTY": (50, 75, 100, 150)}
FLY_WIDTH_STEPS = (2, 3, 4, 6)  # other underlyings: wings this many strike steps from the ATM strike


def fly_widths(report, width=None):
    """(widths, reason) for iron-fly rows: the one --width given, else the defaults for the index (or strike steps)."""
    if width:
        return [float(width)], None
    if report["underlying"] in FLY_WIDTHS:
        return [float(w) for w in FLY_WIDTHS[report["underlying"]]], f"default for {report['underlying']}"
    step = strike_step(report)
    if not step:
        raise Block("FNO: could not work out the strike step; pass --width (points from the ATM strike to each wing)")
    return [step * n for n in FLY_WIDTH_STEPS], f"default: {', '.join(map(str, FLY_WIDTH_STEPS))} strike steps of {fmt(step, 0 if step == int(step) else 2)}"


def fly_rows(report, width=None):
    """Iron-fly legs per wing width: sell the ATM call and put (the strike nearest the level a trader sees), buy a call and a
    put `width` points out (the nearest traded strikes). Returns (rows, atm, reason)."""
    atm, _, _ = atm_reference(report)
    calls, puts = report["_books"]
    if not (price_of(calls.get(atm, {"close": None, "volume": 0})) and price_of(puts.get(atm, {"close": None, "volume": 0}))):
        raise Block(f"FNO: the ATM strike {fmt(atm, 0)} has no traded call and put for an iron fly")
    widths, reason = fly_widths(report, width)
    sd = report["atm_iv"] * math.sqrt(report["_years"]) if report["atm_iv"] else None
    row = next((r for r in report["_rows"] if r["strike"] == atm), {})
    delta = lambda side: abs(((row.get(side) or {}).get("greeks") or {}).get("delta") or 0.5)
    out, seen = [], set()
    for w in widths:
        wc, wp = wing_strike(report, "CE", atm, w), wing_strike(report, "PE", atm, w)
        if wc is None or wp is None:
            continue
        legs = [f"SELL:CE:{atm:g}:1", f"SELL:PE:{atm:g}:1", f"BUY:CE:{wc:g}:1", f"BUY:PE:{wp:g}:1"]
        if tuple(legs) in seen:
            continue
        seen.add(tuple(legs))
        actual = wc - atm if wc - atm == atm - wp else w  # the listed wings decide the width shown
        out.append({"side": "both", "target_delta": delta("ce"), "legs": legs, "width_target": actual,
                    "shorts": [{"type": k, "strike": atm, "delta": delta(k.lower()),
                                "sd_away": math.log(atm / report["forward"]) / sd if sd else None} for k in ("PE", "CE")],
                    "wings": [{"type": "CE", "strike": wc, "width": wc - atm}, {"type": "PE", "strike": wp, "width": atm - wp}]})
    return out, atm, reason


def grid(report, structure, width=None, *, side="both", deltas=GRID_DELTAS, card="default", span=None, trade_day=None):
    if structure == "fly":
        card = load_charge_card() if card == "default" else card
        rows, atm, reason = fly_rows(report, width)
        if not rows:
            raise Block("FNO: no traded wings for an iron fly at these widths; pass --width")
        for row in rows:
            row.update(row_figures(report, row["legs"], card, span, trade_day))
        widths = sorted({x["width_target"] for x in rows})
        return {"schema": "fno_grid_v1", "underlying": report["underlying"], "expiry": report["expiry"], "structure": structure,
                "width": widths[0] if len(widths) == 1 else None, "widths": widths, "width_reason": reason, "atm": atm,
                "lot_size": report["lot_size"], "spot": report["spot"], "forward": report["forward"], "atm_iv": report["atm_iv"],
                "data_date": report["data_date"], "rows": rows, "costs_available": bool(card),
                "margin_note": MARGIN_NOTE if span is None else f"Margin: SPAN + exposure per row, {fno_margin.LABEL}.",
                "note": ("Each row is a different trade-off, not a recommendation: wider wings collect more credit and raise the chance "
                         "of profit but enlarge the maximum loss; at implied prices every row's expected value is about minus its costs."),
                "execution_eligible": False, "submitted": False}
    if structure in ("condor", "spread") and not (width and width > 0):
        raise Block("FNO: --width (points between short and long strikes) is required for condor and spread")
    card = load_charge_card() if card == "default" else card
    calls, puts = otm_traded(report, "CE"), otm_traded(report, "PE")
    if not calls or not puts:
        raise Block("FNO: not enough traded out-of-the-money strikes for a grid")
    sides = {"strangle": ["both"], "condor": ["both"], "spread": ["call", "put"] if side == "both" else [side]}[structure]
    sd = report["atm_iv"] * math.sqrt(report["_years"]) if report["atm_iv"] else None
    rows, seen = [], set()
    for which in sides:
        for d in deltas:
            c = min(calls, key=lambda x: abs(x[1] - d)) if which in ("both", "call") else None
            p = min(puts, key=lambda x: abs(x[1] - d)) if which in ("both", "put") else None
            legs = []
            if c:
                legs.append(f"SELL:CE:{c[0]:g}:1")
            if p:
                legs.append(f"SELL:PE:{p[0]:g}:1")
            wings = {}
            if structure in ("condor", "spread"):
                for kind, short in (("CE", c), ("PE", p)):
                    if short:
                        w = wing_strike(report, kind, short[0], width)
                        if w is None:
                            legs = None
                            break
                        wings[kind] = w
                        legs.append(f"BUY:{kind}:{w:g}:1")
            if not legs:
                continue
            key = (which, tuple(sorted(legs)))
            if key in seen:
                continue
            seen.add(key)
            shorts = [(k, x) for k, x in (("PE", p), ("CE", c)) if x]
            row = {"side": which, "target_delta": d, "legs": legs,
                   "shorts": [{"type": k, "strike": x[0], "delta": x[1],
                               "sd_away": math.log(x[0] / report["forward"]) / sd if sd else None} for k, x in shorts],
                   "wings": [{"type": k, "strike": w, "width": abs(w - dict(shorts)[k][0])} for k, w in sorted(wings.items(), reverse=True)]}
            row.update(row_figures(report, legs, card, span, trade_day))
            rows.append(row)
    return {"schema": "fno_grid_v1", "underlying": report["underlying"], "expiry": report["expiry"], "structure": structure,
            "width": width, "lot_size": report["lot_size"], "spot": report["spot"], "forward": report["forward"], "atm_iv": report["atm_iv"],
            "data_date": report["data_date"], "rows": rows, "costs_available": bool(card),
            "margin_note": MARGIN_NOTE if span is None else f"Margin: SPAN + exposure per row, {fno_margin.LABEL}.",
            "note": ("Each row is a different trade-off, not a recommendation: shorts further out (lower delta) raise the chance of profit "
                     "but shrink the credit and worsen the reward-to-risk; at implied prices every row's expected value is about minus its costs."),
            "execution_eligible": False, "submitted": False}


STRUCTURE_NAMES = {"condor": "iron condor", "strangle": "short strangle", "spread": "credit spread", "fly": "iron fly"}


def sd_cells(sd):
    """'+3,950 / +3,950 / -1,230 / -9,800' for −2, −1, +1, +2 SD at expiry (after costs)."""
    return " / ".join(signed(sd[k][1], 0) if k in sd else "n/a" for k in (-2, -1, 1, 2))


def grid_row_cells(x, g):
    shorts = ", ".join(f"{s['strike']:,.0f} {s['type']} ({s['delta']:.2f}, {s['sd_away']:+.1f} SD)" for s in x["shorts"])
    wings = ", ".join(f"{w['strike']:,.0f} {w['type']}" + (f" ({w['width']:,.0f} wide)" if (g["width"] and abs(w["width"] - g["width"]) > 1e-9)
                                                           or (g["structure"] == "fly" and not g["width"]) else "")
                      for w in x["wings"]) or "—"
    ml = x["max_loss_after_costs"]
    margin = margin_cell(x.get("margin"))
    return [shorts, wings, fmt(x["credit_after_costs"] if x["credit_after_costs"] is not None else x["credit"], 0),
            fmt(x["max_profit_after_costs"], 0) if x["max_profit_after_costs"] is not None else "unlimited",
            fmt(-ml, 0) if ml is not None else "unlimited", ", ".join(fmt(b, 0) for b in x["breakevens_after_costs"]) or "n/a",
            f"{pct(x['pop_after_costs'])} ({pct(x['pop_flat'])} flat)",
            pct(x["max_loss_chance"]) if x["max_loss_chance"] is not None else "no cap", margin,
            pct(x.get("return_on_margin")), sd_cells(x.get("sd_pnl") or {})]


def margin_cell(m):
    """'80,120' or, with the expiry-day extra, '80,120 + 67,082 expiry day' for a grid's margin column."""
    if not m or not m.get("available"):
        return "n/a"
    if (m.get("expiry_extra") or 0) > 0.5:
        return f"{fno_margin.inr(m['normal_total'])} + {fno_margin.inr(m['expiry_extra'])} expiry day"
    return fno_margin.inr(m["total"])


def expiry_day_margin_note(margin):
    """The grid's note on the expiry-day extra, or None."""
    if not margin or not margin.get("available") or (margin.get("expiry_extra") or 0) <= 0.5:
        return None
    when = ("today" if margin.get("entry_is_today") else
            f"on {day_text(margin['entry_day'])}, the next session (which this grid assumes)")
    return (f"Margin column: the normal figure + the expiry-day extra 2% on short index options "
            f"({fno_margin.EXPIRY_DAY_SOURCE}), charged because entry is {when} and that is expiry day; return on margin uses "
            "the sum. " + ("Brokers add it today too." if margin.get("entry_is_today") else
                           "Broker calculators show only the normal figure before that day"
                           + ("." if margin.get("broker_low") is not None else "; the ±10% band is for the normal figure.")))


def width_text(g):
    """'500 points' or '500 points (default for BANKNIFTY; change with --width)'; an iron fly's several widths
    '100, 150, 200 and 300 points from the ATM 25,800 (default for NIFTY; one width with --width)'."""
    if g["structure"] == "fly":
        ws = [fmt(w, 0 if w == int(w) else 2) for w in g.get("widths") or [g["width"]]]
        listed = ws[0] if len(ws) == 1 else ", ".join(ws[:-1]) + " and " + ws[-1]
        reason = f" ({g['width_reason']}; one width with --width)" if g.get("width_reason") else ""
        return f"{listed} points from the ATM {fmt(g['atm'], 0)}{reason}"
    text = f"{fmt(g['width'], 0 if g['width'] == int(g['width']) else 2)} points"
    return text + (f" ({g['width_default']}; change with --width)" if g.get("width_default") else "")


NEAR_MONEY = 0.005  # a short strike this close to the live level is flagged in the grid


def short_now_cell(s_, level):
    """'Δ 0.47 ⚠' for a short leg re-priced now: its delta, ⚠ when its strike is within NEAR_MONEY of the live level."""
    near = level and abs(s_["strike"] / level - 1) <= NEAR_MONEY
    return f"Δ {s_['delta']:.2f}{' ⚠' if near else ''}"


def near_money_lines(g, level, underlying, where=None, close_day=None):
    """One line per row whose short strike is now within NEAR_MONEY of the live (or, after the session, closing) level."""
    out = []
    for i, x in enumerate(g["rows"], 1):
        for s_ in x["shorts"]:
            if level and abs(s_["strike"] / level - 1) <= NEAR_MONEY:
                word = "put" if s_["type"] == "PE" else "call"
                src = where or f"live {underlying} {fmt(level)}"
                when = f"the {day_text(close_day)} close" if close_day else "the close"
                out.append(f"⚠ Row {i}: short {word} {fmt(s_['strike'], 0)} is now near the money — {abs(s_['strike'] / level - 1):.2%} from "
                           f"{src} (Δ {s_['delta']:.2f} now, {s_.get('delta_close', s_['delta']):.2f} at {when}).")
    return out


def grid_markdown(r, g, now=None):
    """The grid; with `now` (a re-priced report) the first table is 'if placed now' and the same strikes at the close follow."""
    lines = report_preamble(r)
    width = f", wings {width_text(g)}" if g["width"] or g["structure"] == "fly" else ""
    lines += [f"# {r['underlying']} {STRUCTURE_NAMES[g['structure']]} grid — expiry {r['expiry']}{expiry_tag(r)}{width}", "",
              f"Data: {short_source(r['source'])} for {r['data_date']} · spot {fmt(r['spot'])} ({close_label(r)}) · ATM IV {pct(r['atm_iv'])} · "
              f"{r['days_to_expiry']:.1f} days · 1 lot = {g['lot_size']}{live_text(r)}", ""]
    if monthly_only_note(r) and not r.get("selection_note"):
        lines += [monthly_only_note(r), ""]
    lines += [f"> {g['note']} {PROB_LABEL[0].upper() + PROB_LABEL[1:]}.", ""]
    cost = "after costs" if g["costs_available"] else "before costs"
    placed = now.get("_placed_now") if now else None
    if placed:
        lines += [f"## {now_title(placed, r['underlying'])}", "",
                  f"Short strikes chosen by delta at the {day_text(r['data_date'])} close (so `--row N` always picks the same legs); "
                  f"Δ and SD shown are re-computed now · ATM IV now {pct(now['atm_iv'])} · {placed['years'] * 365:.1f} days to expiry.", ""]
    head = (f"| # | Target Δ | Short strikes (Δ, SD away) | Long strikes | Credit {cost} ₹ | Max profit ₹ | Max loss ₹ | "
            f"Breakevens {cost} | Profit chance | Max-loss chance | Margin ₹ (indicative) | Return on margin | P&L at expiry at −2 / −1 / +1 / +2 SD ₹ |")
    number = {id(x): i + 1 for i, x in enumerate(g["rows"])}
    for which in dict.fromkeys(x["side"] for x in g["rows"]):
        if which != "both":
            lines += [f"### {'Bear call' if which == 'call' else 'Bull put'} spreads", ""]
        lines += [head, "| ---: | ---: | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |"]
        for x in (x for x in g["rows"] if x["side"] == which):
            lines.append(f"| {number[id(x)]} | {x['target_delta']:.2f} | " + " | ".join(grid_row_cells(x, g)) + " |")
        lines.append("")
    if placed:
        near = near_money_lines(g, placed["level"], r["underlying"], now_where(placed, r["underlying"]) if placed.get("close") else None,
                                r["data_date"] if placed.get("close") else None)
        if near:
            lines += near + [""]
        lines += [f"How 'now' is estimated: {placed['basis']}", ""]
        if any(x.get("eod") for x in g["rows"]):
            lines += [f"## Same strikes at the {day_text(r['data_date'])} close (end-of-day data, for reference)", "",
                      f"| # | Credit {cost} ₹ | Max profit ₹ | Max loss ₹ | Breakevens {cost} | Profit chance | Return on margin | "
                      "P&L at expiry at −2 / −1 / +1 / +2 SD ₹ |",
                      "| ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |"]
            for x in g["rows"]:
                e = x.get("eod")
                if not e:
                    continue
                credit = e["credit_after_costs"] if e["credit_after_costs"] is not None else e["credit"]
                lines.append(f"| {number[id(x)]} | {fmt(credit, 0)} | "
                             f"{fmt(e['max_profit_after_costs'], 0) if e['max_profit_after_costs'] is not None else 'unlimited'} | "
                             f"{fmt(-e['max_loss_after_costs'], 0) if e['max_loss_after_costs'] is not None else 'unlimited'} | "
                             f"{', '.join(fmt(b, 0) for b in e['breakevens_after_costs']) or 'n/a'} | {pct(e['pop_after_costs'])} | "
                             f"{pct(e.get('return_on_margin'))} | {sd_cells(e.get('sd_pnl') or {})} |")
            lines.append("")
    basis = g["rows"][0]["skew_basis"] if g["rows"] else ""
    margin = next((x["margin"] for x in g["rows"] if x.get("margin")), None)
    if margin and margin.get("available"):
        hedged = fno_margin.hedged_text(margin)
        label = fno_margin.HEDGED_LABEL if margin.get("broker_low") is not None else fno_margin.LABEL
        extra_note = expiry_day_margin_note(margin)
        held = fno_margin.held_extra_text(margin)
        margin_line = (f"Margin: SPAN + exposure for 1 lot of each leg, indicative ({label}) — {margin_file_text(margin)}; "
                       f"{margin['basis']}. Return on margin = max profit after costs ÷ that margin, if held to expiry."
                       + (f" {hedged} (row 1's range; the gap is a similar share of every row's exposure)." if hedged else "")
                       + (f" {extra_note}" if extra_note else "") + (f" {held} (row 1)." if held else ""))
    elif margin:
        margin_line = margin["note"][0].upper() + margin["note"][1:] + "."
    else:
        margin_line = g["margin_note"]
    lines += [f"Per 1 lot of each leg; # numbers the rows. Profit chance and max-loss chance at expiry use {basis}; the bracketed figure "
              "uses one flat ATM IV. Credit is net premium after entry charges; max profit/loss and breakevens include exit charges "
              f"(squaring off every leg). SD columns: P&L at expiry after costs with the underlying at {SD_LEVELS}. "
              "At implied prices every row's expected value is about minus its costs (JSON `ev_after_costs`). "
              f"Full payoff, stress table and chart for one row: add `--row N --chart FILE.svg` to this command.", "",
              margin_line, "", f"> {r['disclaimer']}", ""]
    return "\n".join(lines)


INDEX_NAMES = {"NIFTY": "Nifty", "BANKNIFTY": "Bank Nifty", "FINNIFTY": "Fin Nifty", "MIDCPNIFTY": "Nifty Midcap Select",
               "NIFTYNXT50": "Nifty Next 50"}
WEEKLIES_ENDED = {"BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}  # SEBI's one-weekly-index-per-exchange rule, from 20 Nov 2024


def monthly_only_note(r):
    """'Bank Nifty has had no weekly options since Nov 2024; using the 23 Nov monthly.' when the underlying lists monthlies only."""
    if r.get("has_weeklies") is not False:
        return None
    name, when = INDEX_NAMES.get(r["underlying"], r["underlying"]), day_text(r["expiry"])[4:]
    if r["underlying"] in WEEKLIES_ENDED:
        return f"{name} has had no weekly options since Nov 2024; using the {when} monthly."
    return f"{name} has no weekly options in this data (one expiry a month); using the {when} monthly."


def grid_summary_markdown(r, g, now=None):
    """Compact grid: #, strikes, credit, max loss, profit chance, margin — the 'if placed now' figures when the market is open."""
    lines = report_preamble(r)
    placed = now.get("_placed_now") if now else None
    width = f", wings {width_text(g)}" if g["width"] or g["structure"] == "fly" else ""
    head = (f"**{r['underlying']} {STRUCTURE_NAMES[g['structure']]} grid — expiry {r['expiry']}{width}, 1 lot = {g['lot_size']}**")
    lines.append(head)
    note = monthly_only_note(r)
    if note and not r.get("selection_note"):
        lines.append(note)
    if placed:
        lines.append(f"{now_title(placed, r['underlying'])} · ATM IV now {pct(now['atm_iv'])} · {placed['years'] * 365:.1f} days left")
    else:
        lines.append(f"At the {day_text(r['data_date'])} close (end of day) · ATM IV {pct(r['atm_iv'])} · {r['days_to_expiry']:.1f} days left")
    cost = "after costs" if g["costs_available"] else "before costs"
    lines += ["", "| # | Short / long strikes | Credit ₹ | Max loss ₹ | Breakevens | Profit chance | P&L at −1 / +1 SD ₹ | Margin ₹ |",
              "| ---: | --- | ---: | ---: | --- | ---: | ---: | ---: |"]
    for i, x in enumerate(g["rows"], 1):
        legs = []
        for s_ in x["shorts"]:
            wing = next((w for w in x["wings"] if w["type"] == s_["type"]), None)
            delta = f" ({short_now_cell(s_, placed['level'])})" if placed and "delta_close" in s_ else ""
            legs.append(f"{s_['strike']:,.0f}{'/' + format(wing['strike'], ',.0f') if wing else ''} {s_['type']}{delta}")
        ml = x["max_loss_after_costs"]
        m = x.get("margin")
        sd = x.get("sd_pnl") or {}
        lines.append(f"| {i} | {' · '.join(legs)} | {fmt(x['credit_after_costs'] if x['credit_after_costs'] is not None else x['credit'], 0)} | "
                     f"{fmt(-ml, 0) if ml is not None else 'unlimited'} | {' / '.join(fmt(b, 0) for b in x['breakevens_after_costs']) or 'n/a'} | "
                     f"{pct(x['pop_after_costs'])} | {' / '.join(signed(sd[k][1], 0) if k in sd else 'n/a' for k in (-1, 1))} | "
                     f"{margin_cell(m)} |")
    if placed and any("delta_close" in s_ for x in g["rows"] for s_ in x["shorts"]):
        lines += ["", f"Short strikes were picked by delta at the {day_text(r['data_date'])} close (so `--row N` stays the same); Δ is each "
                      f"short leg's delta now, at {now_where(placed, r['underlying'])}; ⚠ = short strike within "
                      f"{NEAR_MONEY:.1%} of that level."]
        lines += near_money_lines(g, placed["level"], r["underlying"], now_where(placed, r["underlying"]) if placed.get("close") else None,
                                  r["data_date"] if placed.get("close") else None)
    margin = next((x["margin"] for x in g["rows"] if x.get("margin")), None)
    sd = next((x["sd_pnl"] for x in g["rows"] if x.get("sd_pnl") and -1 in x["sd_pnl"] and 1 in x["sd_pnl"]), None)
    levels = (f" −1 / +1 SD: {r['underlying']} at {fmt(sd[-1][0], 0)} / {fmt(sd[1][0], 0)} at expiry, the ends of the 68% implied range."
              if sd else "")
    lows = [1 - x["margin"]["broker_low"] / x["margin"]["total"] for x in g["rows"]
            if (x.get("margin") or {}).get("broker_low") is not None and x["margin"]["total"] > 0]
    lows = [1 - x["margin"]["broker_low_normal"] / x["margin"]["normal_total"] for x in g["rows"]
            if (x.get("margin") or {}).get("broker_low_normal") is not None and x["margin"]["normal_total"] > 0] or lows
    hedged = (f"Hedged spreads: NSE gives hedged options no exposure offset ({fno_margin.HEDGE_SOURCE}), but broker calculators have "
              f"charged less exposure ({fno_margin.BROKER_SEEN_TEXT}), so expect each row's "
              f"{'normal ' if (margin or {}).get('expiry_extra') else ''}margin up to about "
              f"{max(lows):.0%} lower — your broker's basket margin is final" if lows else fno_margin.hedged_text(margin))
    extra_note = expiry_day_margin_note(margin)
    if extra_note:
        hedged = (hedged + ". " if hedged else "") + extra_note.rstrip(".")
    lines += ["", f"Per 1 lot of each leg, {cost} (credit after entry charges; max loss, breakevens and P&L including exit charges). "
                  f"Profit chance at expiry with each strike's own IV ({PROB_LABEL}).{levels} "
                  + ("Wings widen down the rows: more credit and a higher profit chance, but a bigger maximum loss"
                     if g["structure"] == "fly" else
                     "Shorts move further out down the rows: higher profit chance, smaller credit, bigger loss when wrong")
                  + "; at implied prices each row's expected value is about minus its costs."
              + (f" Margin indicative ({fno_margin.HEDGED_LABEL if margin.get('broker_low') is not None else fno_margin.LABEL})."
                 if margin and margin.get("available") else
                 f" {margin['note'][0].upper() + margin['note'][1:]}." if margin else "")
              + (f" {hedged}." if hedged else ""),
              "Payoff of one row: add `--row N --summary`; its chart: `--row N --chart FILE.svg`. Drop --summary for the full grid. "
              "Not investment advice.", ""]
    return "\n".join(lines)


def annotate_holidays(calendar, holidays):
    if calendar and holidays:
        for x in calendar["expiries"]:
            if x["moved"] and x["usual_date"]:
                x["holiday"] = holiday_name(holidays, x["usual_date"])
    return calendar


def expiries_markdown(result):
    lines = [f"{result['underlying']} option expiries after {result['data_date']}:"]
    details = result.get("expiry_details")
    if not details:
        return lines[0] + " " + ", ".join(result["expiries"]) + "\n"
    lines += [f"- {x['expiry']} — {expiry_text(x, result['usual_expiry_weekday'])}" for x in details]
    usual = (f"usual expiry day {result['usual_expiry_weekday']}" if result["usual_expiry_weekday"] else
             "usual expiry day not clear from these dates")
    if result["has_weeklies"]:
        lines.append(f"{usual[0].upper() + usual[1:]}. Monthly = the expiry shared with the futures contract "
                     "(or the last of the month); the others are weeklies.")
    else:
        lines.append(f"No weekly expiries: the data lists one expiry per month for {result['underlying']}, so each is a monthly ({usual}).")
    return "\n".join(lines) + "\n"


def public(report):
    return {k: v for k, v in report.items() if not k.startswith("_")}


def utf8_streams():
    """Windows consoles default to cp1252; switch stdout/stderr to UTF-8 so ₹, —, σ and √ print."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def previous_trading_day(day, holidays):
    day -= timedelta(days=1)
    while not is_trading_day(day, holidays):
        day -= timedelta(days=1)
    return day


DEFAULT_WIDTHS = {"NIFTY": 100, "BANKNIFTY": 500, "FINNIFTY": 100, "MIDCPNIFTY": 50}
STOCK_WIDTH_STEPS = 2  # stocks and other indices: two strike steps


def strike_step(report):
    """The usual gap between listed strikes near the money (the most common one among the 12 nearest)."""
    strikes = sorted(r["strike"] for r in report["_rows"])
    near = sorted(strikes, key=lambda k: abs(k - report["forward"]))[:12]
    near.sort()
    gaps = [round(b - a, 4) for a, b in zip(near, near[1:]) if b > a]
    return max(set(gaps), key=lambda g: (gaps.count(g), -g)) if gaps else None


def default_width(selected, report):
    """(points, reason) for condor/spread wings when --width is not given."""
    if selected in DEFAULT_WIDTHS:
        return float(DEFAULT_WIDTHS[selected]), f"default for {selected}"
    step = strike_step(report)
    if not step:
        raise Block("FNO: could not work out the strike step; pass --width (points between short and long strikes)")
    return step * STOCK_WIDTH_STEPS, f"default: {STOCK_WIDTH_STEPS} strike steps of {fmt(step, 0 if step == int(step) else 2)}"


def check_arguments(a):
    """Everything that can be checked without the network, so a typo fails at once instead of after the downloads."""
    if a.out and Path(a.out).exists():
        raise Block("output exists; choose a new path")
    for name in ("date", "expiry", "previous_date"):
        value = getattr(a, name, None)
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise Block(f"FNO: --{name.replace('_', '-')} must be a date like 2026-11-24") from None
    if a.input and not Path(a.input).is_file():
        raise Block(f"FNO: --input file not found: {a.input}")
    if a.command == "chain" and a.strikes < 1:
        raise Block("FNO: --strikes must be at least 1")
    if a.command == "payoff" and not a.range > 0:
        raise Block("FNO: --range must be a positive percent, e.g. 8")
    if a.command == "grid":
        try:
            deltas = tuple(float(x) for x in a.deltas.split(",")) if a.deltas else GRID_DELTAS
        except ValueError:
            raise Block("FNO: --deltas must be numbers between 0 and 0.5, e.g. 0.30,0.20,0.10") from None
        if any(not 0 < d < 0.5 for d in deltas):
            raise Block("FNO: --deltas must be between 0 and 0.5, e.g. 0.30,0.20,0.10")
        if a.width is not None and not a.width > 0:
            raise Block("FNO: --width must be a positive number of points, e.g. 500")
        if a.row is not None and a.row < 1:
            raise Block("FNO: --row must be 1 or more (rows as numbered in the grid)")
        if a.chart and a.row is None:
            raise Block("FNO: --chart needs --row N")
    if getattr(a, "chart", None) and Path(a.chart).exists():
        raise Block("FNO: chart file exists; choose a new path")


def main(argv=None, *, fetcher=public_fetch, clock=instant):
    utf8_streams()
    p = argparse.ArgumentParser(description="NSE F&O analytics: option chain, OI, PCR, max pain, IV/Greeks, strategy payoff and "
                                            "strike grids (research only).")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("chain", "expiries", "payoff", "grid"):
        s = sub.add_parser(name)
        if name in ("chain", "payoff"):
            s.add_argument("symbol", nargs="?", help="NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY or an F&O stock such as RELIANCE; or a bare "
                                                     "strike such as 25800CE, in which case the underlying is inferred by moneyness and stated")
        else:
            s.add_argument("symbol", help="NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY or an F&O stock such as RELIANCE")
        if name == "expiries":
            s.add_argument("--expiry", help=argparse.SUPPRESS)
        else:
            pick = s.add_mutually_exclusive_group()
            pick.add_argument("--expiry", help="YYYY-MM-DD; default nearest")
            pick.add_argument("--weekly", dest="kind", action="store_const", const="weekly",
                              help="Nearest weekly expiry (falls back to the nearest monthly, with a note, when none is listed)")
            pick.add_argument("--monthly", dest="kind", action="store_const", const="monthly", help="Nearest monthly expiry")
        s.add_argument("--date", help="Bhavcopy trading date YYYY-MM-DD; default latest available")
        s.add_argument("--input", help="fno_chain_v1 JSON, e.g. from a broker connector")
        s.add_argument("--rate", type=float, default=DEFAULT_RATE, help="Annual risk-free rate assumption (default 0.065)")
        s.add_argument("--format", choices=["markdown", "json"], default="markdown")
        s.add_argument("--out", help="Write to a new file")
        s.add_argument("--no-live", action="store_true", help="Skip the NSE holiday list and the live index level (offline labels)")
        if name in ("chain", "payoff", "grid"):
            s.add_argument("--summary", action="store_true", help="Short answer-first block instead of the full report "
                                                                  "(grid: a compact table with breakevens and ±1 SD P&L, or with --row that row's payoff)")
        if name in ("payoff", "grid"):
            s.add_argument("--charges", default=str(DEFAULT_CARD), help="Rate card JSON with NFO:option/NFO:future (default: the example card)")
            s.add_argument("--no-margin", action="store_true", help="Skip the NSE Clearing SPAN file (~10 MB) and the indicative margin")
            s.add_argument("--held", action="store_true", help="You already hold these legs from the last close: the summary adds "
                                                               "their model P&L now (always in the full report)")
        if name == "chain":
            s.add_argument("--strikes", type=int, default=12, help="Strikes each side of ATM")
            s.add_argument("--no-vix", action="store_true")
            s.add_argument("--no-compare", action="store_true", help="Skip the previous trading day's bhavcopy (one less ~1.5 MB download)")
            s.add_argument("--no-participants", action="store_true", help="Skip NSE's participant-wise OI files")
            s.add_argument("--previous-date", help="Compare with the bhavcopy on or before this YYYY-MM-DD (default: the trading day before the data date; needed with --input)")
            s.add_argument("--no-snapshot", action="store_true", help="During market hours, skip NSE Clearing's intraday SPAN file (~10 MB) "
                                                                      "behind the 'Now (estimate)' line")
        if name == "payoff":
            s.add_argument("--leg", action="append", required=True, help="SIDE:TYPE:STRIKE:LOTS[:PREMIUM], e.g. SELL:CE:25800:1; futures BUY:FUT::1")
            s.add_argument("--range", type=float, default=8, help="Percent either side of spot")
            s.add_argument("--chart", help="Write an SVG payoff chart to a new file")
        if name == "grid":
            s.add_argument("--structure", choices=["condor", "strangle", "spread", "fly"], required=True,
                           help="fly = iron fly: sell the ATM call and put, buy a wing on each side (one row per wing width)")
            s.add_argument("--width", type=float, help="Points between each short strike and its long wing (condor, spread; "
                                                       "fly: one width instead of the default set)")
            s.add_argument("--side", choices=["call", "put", "both"], default="both", help="Spread side (default both)")
            s.add_argument("--deltas", help="Comma-separated short-strike deltas, default 0.35,0.30,0.25,0.20,0.15,0.10,0.05")
            s.add_argument("--row", type=int, help="Print the full payoff (or --summary) for grid row N, as numbered in the grid")
            s.add_argument("--chart", help="With --row: write that row's SVG payoff chart to a new file")
    a = p.parse_args(argv)
    now = clock()
    try:
        check_arguments(a)
        legs = [parse_leg(l) for l in a.leg] if a.command == "payoff" else []
        if a.command == "grid" and a.symbol:
            underlying(a.symbol)
        spec = strike_spec(a.symbol) if a.symbol else None
        assumption = None
        if a.input:
            named = json.loads(Path(a.input).read_text(encoding="utf-8")).get("underlying", "") if (spec or not a.symbol) else a.symbol
            selected = underlying(named)
            contracts, source, as_of = load_input(a.input, selected)
        else:
            raw, url = fetch_bhavcopy_raw(fetcher=fetcher, today=now.date(), on=a.date)
            if a.symbol and not spec:
                selected = underlying(a.symbol)
            else:
                if any(l.get("relative") for l in legs):
                    raise Block("FNO: name the underlying (e.g. NIFTY) for ATM-relative strikes")
                wanted = sorted({spec[0]} if spec else {l["strike"] for l in legs if l["type"] != "FUT"})
                if not wanted:
                    raise Block("FNO: name the underlying (e.g. NIFTY) or give a strike such as 25800CE")
                selected, assumption, _ = infer_underlying(raw, wanted)
            contracts = parse_bhavcopy(raw, selected)
            as_of = None
            source = "NSE F&O bhavcopy " + url
        holidays = None if a.no_live else fetch_holidays(fetcher)
        if a.command == "expiries":
            trade = contracts[0]["trade_date"]
            cal = annotate_holidays(expiry_calendar(contracts, trade, holidays), holidays) if not a.input else None
            result = {"underlying": selected, "data_date": trade, "expiries": expiries(contracts, trade),
                      "expiry_details": cal["expiries"] if cal else None, "has_weeklies": cal["has_weeklies"] if cal else None,
                      "usual_expiry_weekday": cal["usual_weekday"] if cal else None}
            text = json.dumps(result, indent=2) if a.format == "json" else expiries_markdown(result)
        else:
            report = analyse_chain(contracts, selected, expiry=a.expiry, kind=a.kind, as_of=as_of, rate=a.rate,
                                   width=getattr(a, "strikes", 12), source=source, holidays=holidays)
            ctx = session_context(report["data_date"], now, holidays, live_chain=bool(a.input))
            # Between the 15:30 close and the evening bhavcopy the last bhavcopy is a day old: the chain comes from NSE's option
            # chain at today's close when it answers, and 'now' is priced at today's close (NSE's closing level and NSE
            # Clearing's last intraday snapshot) instead of silently using yesterday's prices.
            post_close = (not a.input and not a.date and not a.no_live and ctx["phase"] == "closed" and report["data_date"] < ctx["today"])
            bhavcopy = None if a.input else (raw, url, report["data_date"])
            rejected = None
            if post_close and a.command == "chain":
                fresh, rejected = nse_chain_report(report, selected, fetcher=fetcher, now=now, holidays=holidays, rate=a.rate,
                                                   width=a.strikes)
                if fresh:
                    report = fresh
                    ctx = session_context(report["data_date"], now, holidays)
            if post_close and report["data_date"] < ctx["today"]:
                report["post_close_note"] = (
                    f"**Today's ({day_text(ctx['today'])}) F&O bhavcopy is not yet published ({BHAVCOPY_HOURS}); "
                    + (f"PCR, OI and max pain below are the {day_text(report['data_date'])} close — re-run after that for today's PCR, "
                       "OI and max pain.**" if a.command == "chain" else
                       f"'If placed now' is estimated at today's close; the {day_text(report['data_date'])} close is kept for reference — "
                       "re-run after that for today's official closing prices.**"))
                if rejected:
                    # Never fall back silently: say why today's closing chain was not used.
                    report["post_close_note"] += (f"\n**NSE's option chain at today's close was not used: {rejected}** — so the figures "
                                                  f"below are the last bhavcopy's ({day_text(report['data_date'])}).")
                    report["chain_rejected"] = rejected
            live = None
            if a.input:
                live = {"level": report["spot"], "at": report["as_of"], "source": source}
            elif post_close and report["data_date"] < ctx["today"]:
                live = closing_level(selected, fetcher=fetcher, now=now)
            elif not a.no_live and ctx["phase"] in ("open", "closed") and report["data_date"] < ctx["today"]:
                live = live_level(selected, fetcher=fetcher, now=now)
            estimating = not a.input and report["data_date"] < ctx["today"] and (ctx["phase"] == "open" or post_close)
            report.update(session=ctx, live=live, underlying_assumption=assumption,
                          session_ranges=session_ranges(report, ctx, live, holidays),
                          sessions_left=sessions_left(report["expiry"], ctx, holidays))
            if a.command == "payoff":
                legs, report["strike_resolution"] = resolve_relative(report, legs)
            if a.command == "chain":
                if spec:
                    report["strike_focus"] = strike_focus(report, spec[0], spec[1])
                vix = None
                if selected in INDICES and not a.no_vix:
                    try:
                        vix = vix_context(fetcher=fetcher, now=now, on=report["data_date"])
                    except (Block, ValueError, KeyError, TypeError):
                        vix = None
                    if vix and not a.input and not a.no_live and ctx["phase"] == "closed" and report["data_date"] == ctx["today"] or (
                            vix and post_close):
                        # After today's close NSE's official VIX close replaces Yahoo's daily bar, which has differed (19 Nov 2026:
                        # Yahoo 16.11, NSE 15.80), whether or not the evening bhavcopy is out yet.
                        vix = nse_vix_close(vix, fetcher=fetcher, now=now)
                    if vix:
                        vix = {k: v for k, v in vix.items() if not k.startswith("_")}
                report["vix"] = vix
                if a.no_compare:
                    report["day_over_day"] = {"status": "skipped", "note": "Day-over-day comparison skipped (--no-compare)."}
                elif a.input and not a.previous_date:
                    report["day_over_day"] = {"status": "skipped", "note": "Input chain: pass --previous-date YYYY-MM-DD to compare with that day's NSE bhavcopy."}
                else:
                    report["day_over_day"] = compare_previous(report, selected, fetcher=fetcher, on=a.previous_date, holidays=holidays,
                                                              preloaded=bhavcopy if report.get("nse_chain") else None)
                dod = report["day_over_day"]
                # Option expiries inside the comparison window (on the data date itself, or between the two files).
                window = merge_expiries(None if a.input else index_option_expiries(raw, report["data_date"], report["data_date"]),
                                        dod.get("window_expiries"))
                if dod["status"] == "ok":
                    dod["expiry_caveat"] = expiry_caveat(window, dod["previous_date"], report["data_date"], {selected})
                if selected in INDICES and not a.no_participants:
                    on = (previous_trading_day(date.fromisoformat(report["data_date"]), holidays).isoformat() if a.input
                          else report["data_date"])
                    po = fetch_participant_oi(on, fetcher=fetcher, holidays=holidays,
                                              previous=a.previous_date if a.previous_date and not a.input else None)
                    if po.get("status") != "ok" and report.get("nse_chain"):
                        # Today's participant file comes out with the evening reports: show the last one, dated.
                        po = fetch_participant_oi(report["nse_chain"]["bhavcopy_date"], fetcher=fetcher, holidays=holidays)
                        po["pending_note"] = f"today's ({day_text(report['data_date'])}) file is not yet published"
                    if po.get("status") == "ok" and po.get("previous_date") and not a.input:
                        po["expiry_caveat"] = expiry_caveat(window, po["previous_date"], po["date"], INDICES)
                    report["participants"] = po
                if not a.no_snapshot and estimating:
                    span = fno_margin.fetch_span(selected, report["data_date"], now.date(), fetcher=fetcher,
                                                 market_day=is_trading_day(now.date(), holidays))
                    report["now_estimate"] = now_estimate(placed_now_report(report, span, live, now=now, holidays=holidays,
                                                                            fetcher=fetcher))
                    n = report["now_estimate"]
                    report["session_ranges"] = ranges_at_now_iv(report["session_ranges"], n)
                    if n.get("available") and n.get("range_1sd"):
                        # Walls near the live level, inside the same live-centred range the expected-move line shows.
                        report["oi_walls_near"] = near_walls(*report["_books"], n["level"], n["range_1sd"],
                                                             f"1-SD range to expiry from live {fmt(n['level'])} at {n['at'][11:16]} IST")
                if a.summary:
                    summary = chain_summary(report, vix)
                    text = json.dumps(public(summary), indent=2, default=str) if a.format == "json" else chain_summary_markdown(summary)
                else:
                    text = json.dumps(public(report), indent=2, default=str) if a.format == "json" else chain_markdown(report, vix)
            else:
                card = rates_on(load_charge_card(a.charges), report["data_date"])
                trade_day = ctx["today"] if ctx["phase"] == "open" else ctx["next_unstarted_session"]
                span = margin_span = None
                if not a.no_margin:
                    span, margin_span = fno_margin.fetch_margin_spans(selected, report["data_date"], now.date(), fetcher=fetcher,
                                                                      market_day=is_trading_day(now.date(), holidays),
                                                                      after_close=ctx["phase"] == "closed",
                                                                      next_session=trade_day)
                now_report = None
                if estimating:
                    now_report = placed_now_report(report, span, live, now=now, holidays=holidays, fetcher=fetcher)
                span = margin_span
                if a.command == "grid":
                    deltas = tuple(float(x) for x in a.deltas.split(",")) if a.deltas else GRID_DELTAS
                    width, width_basis = a.width, None
                    if width is None and a.structure in ("condor", "spread"):
                        width, width_basis = default_width(selected, report)
                    g = grid(report, a.structure, width, side=a.side, deltas=deltas, card=card, span=span, trade_day=trade_day)
                    g["width_default"] = width_basis
                    if now_report:
                        reprice_grid(g, now_report, card, span, trade_day)
                    if a.row is not None:
                        if not 1 <= a.row <= len(g["rows"]):
                            raise Block(f"FNO: --row must be between 1 and {len(g['rows'])}")
                        legs = [parse_leg(l) for l in g["rows"][a.row - 1]["legs"]]
                    else:
                        if a.chart:
                            raise Block("FNO: --chart needs --row N")
                        text = (json.dumps({"report": public(report), "grid": g, "placed_now": (now_report or {}).get("_placed_now"),
                                            "monthly_only_note": monthly_only_note(report)}, indent=2, default=str)
                                if a.format == "json" else grid_summary_markdown(report, g, now_report) if a.summary
                                else grid_markdown(report, g, now_report))
                if a.command == "payoff" or a.row is not None:
                    result = payoff(report, legs, span=getattr(a, "range", 8) / 100, card=card)
                    if live and not a.input:
                        at = instant(live["at"])
                        priced = all(l["iv"] for l in result["legs"] if l["type"] != "FUT")
                        result["live_mark"] = {"level": live["level"], "at": live["at"], "iv_from": f"the {day_text(report['data_date'])} close",
                                               "close": live.get("close"), "provider": live.get("provider"),
                                               "pnl": mark_value(report, result["legs"], live["level"], years_to_expiry(report["expiry"], at))
                                               if priced else None}
                    later = attach_extras(report, result, legs, now=now_report, card=card, span=span, trade_day=trade_day)
                    if a.command == "grid":
                        result["grid_row"] = {"row": a.row, "structure": a.structure, "legs": g["rows"][a.row - 1]["legs"]}
                    if a.chart:
                        chart_path = Path(a.chart)
                        if chart_path.exists():
                            raise Block("FNO: chart file exists; choose a new path")
                        chart_path.parent.mkdir(parents=True, exist_ok=True)
                        chart_path.write_text(svg_chart(now_report if later else report, later or result,
                                                        placed_now=bool(later)), encoding="utf-8")
                    result["chart"] = a.chart
                    result["held"] = a.held
                    if a.summary:
                        summary = payoff_summary(report, result)
                        text = json.dumps(public(summary), indent=2, default=str) if a.format == "json" else payoff_summary_markdown(summary)
                    else:
                        text = json.dumps({"report": public(report), "payoff": result}, indent=2, default=str) if a.format == "json" else payoff_markdown(report, result)
                    if a.command == "grid" and a.format != "json":
                        row = g["rows"][a.row - 1]
                        note = monthly_only_note(report) if not report.get("selection_note") else None
                        shape = (f"wings {fmt(row['width_target'], 0)} points from the ATM {fmt(g['atm'], 0)}" if a.structure == "fly"
                                 else f"target delta {row['target_delta']:.2f}")
                        text = (f"Grid row {a.row} ({STRUCTURE_NAMES[a.structure]}, {shape}): "
                                f"{' '.join('--leg ' + l for l in row['legs'])}\n" + (f"{note}\n" if note else "") + "\n" + text)
    except (Block, OSError, json.JSONDecodeError) as error:
        print(f"BLOCKED - {error}", file=sys.stderr)
        return 2
    if a.out:
        out = Path(a.out)
        if out.exists():
            print("BLOCKED - output exists; choose a new path", file=sys.stderr)
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
