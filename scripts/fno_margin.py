"""Indicative NSE F&O margin (SPAN + exposure) from NSE Clearing's free public SPAN risk-parameter files. Research only.

Source: NSE Clearing publishes the SPAN® risk parameters for every F&O contract as a zipped XML file (CME SPAN "spn"
format 4.00) at nsearchives.nseindia.com/archives/nsccl/span/:

- ``nsccl.YYYYMMDD.i1.zip`` .. ``iN.zip``: the day's begin-of-day and intraday files (i1 is published the evening before);
- ``nsccl.YYYYMMDD.s.zip``: the end-of-day (settlement) file.

Each contract carries a 16-scenario risk array: the loss on one unit held long when the underlying moves by 0, ±1/3,
±2/3 and ±3/3 of the price scan range with volatility up and down, plus two extreme moves (already weighted). Values
are per unit (cvf 1); positions are in units (lots × lot size).

Method (NSE Clearing SPAN, as reproduced to the paisa against a broker calculator on the 18 Sep 2026 file):

    scenario loss_i = Σ units × risk_array_i                 (long units positive, short negative)
    scanning risk   = max(0, max_i scenario loss_i)
    SPAN            = max(0, max(scanning risk, short option minimum) − net option value)
    net option value = Σ units × option price                (long options reduce, short options add)

Exposure (extreme loss) margin, from NSE Clearing's equity-derivatives margins page
(https://www.nseindia.com/static/products-services/equity-derivatives-margins):

- index derivatives 2% of notional; short index options more than 10% out of the money (vs the previous close) 3%;
  short index options with more than 9 months to expiry 5%;
- stock derivatives 3.5%; short stock options more than 30% out of the money 5.25%;
- notional: futures at the contract price, short options at the underlying's last close; long options pay no exposure;
  every short leg pays it on its own notional, whether or not a long wing hedges it;
- expiry day: a further 2% of notional on short index options expiring that day (SEBI circular
  SEBI/HO/MRD/TPD-1/P/CIR/2024/132, 1 Oct 2024, effective 20 Nov 2024), on open short positions at the start of that day
  and on shorts opened during it. It is charged only when the entry day (`trade_day`) is the expiry day: the session in
  progress, or — before the open, after the close, at a weekend or on a holiday — the next session, which the caller
  passes and the text names ("if you enter on Mon 23 Nov (expiry day)"). The result keeps it apart (`expiry_extra`,
  `normal_total`) because broker calculators show only the normal figure on the days before (Zerodha on Sat 21 Nov 2026
  for Mon 23 Nov positions: ₹33,422 and ₹76,022 against ₹77,562 and ₹1,47,201 with the extra included), so the ±10% band
  is claimed for the normal figure only.

Which file (reconciled against Zerodha's basket-margin calculator on 18 Nov 2026, about 12:00 IST, with the intraday
file i2 of 11:03 already published): SPAN matched NSE's begin-of-day parameters — the data date's end-of-day file, the same
risk arrays and prices as today's i1 — to the rupee for a BANKNIFTY 23 Nov iron condor (₹14,235.5; net option value
₹5,299.50 at the 17 Nov closes) and a naked short NIFTY 25800 CE (₹1,26,338), while the 11:03 intraday file gave ₹14,314
and ₹1,15,271. So margin uses the begin-of-day file; the latest intraday file (when newer) is reported alongside as the
exchange's intraday re-computation. Exposure there was ₹66,844.68 and ₹31,196.56 against 2% × the 17 Nov close × units
(₹74,728 and ₹33,004): the broker's base (≈55,704 and ≈23,997 per unit) matches neither the close nor the live index, and
its NIFTY figure was the same ₹31,196.56 per lot on 18 Sep 2026 with NIFTY at 23,346 — a broker convention not in NSE's
published rule, so it is not copied here; the published rule is kept and the result carries a ±10% band.

After the close the broker uses the day's closing intraday file instead (reconciled against Zerodha's SPAN calculator on
19 Nov 2026 at 18:04 IST, with that day's end-of-day file nsccl.20261119.s already published): SPAN matched the 15:32 IST
closing file i5 to within ₹0.45 on four positions — FINNIFTY 28300 short straddle ₹1,53,376 (i5 ₹1,53,375.60; s file
₹1,55,060.40), NIFTY 25300/25500 bull call spread ₹0 (all files ₹0), BANKNIFTY 61500/62000/64000/64500 iron condor ₹13,649.50
(i5 ₹13,649.40; s ₹13,987.20) and a naked short NIFTY 25800 CE ₹1,33,085.75 (i5 ₹1,33,086.20; s ₹1,34,067.05) — with net option
values equal to i5's closing prices. At 22:22 IST, with the next session's begin-of-day file nsccl.20261120.i1 (created 21:15,
identical to the s file) already published, Zerodha still matched i5 on all four, so the closing file holds through the
evening; when the broker moves to the next begin-of-day file before the open was not observed. So the file is chosen by time
of day (`fetch_margin_spans`):

- before the open and during the session: the begin-of-day file (today's i1, i.e. the previous close's parameters);
- after the 15:30 close on a trading day: that day's closing intraday file (stamped 15:30 or later), falling back to the
  begin-of-day file until it is published;
- on a holiday or weekend: the next session's begin-of-day file (``nsccl.<next session>.i1``, published the evening
  before), else the last session's end-of-day file. Reconciled against Zerodha's SPAN calculator on Sat 21 Nov 2026:
  a NIFTY 23 Nov 25600/25800/25800/26000 iron fly showed SPAN ₹13,629 — the 23 Nov i1 file (created 20 Nov 20:55 IST)
  gives ₹13,628.55, while the 20 Nov end-of-day file gives ₹13,038.35 and the 20 Nov closing file i5 ₹12,940.20 (the
  expiry-day i1 re-values every risk array to the new date, so it is not a copy of the s file).
- during the session and before the open: the begin-of-day file is today's i1, then the data date's end-of-day file.

Hedged positions (short and long options together, e.g. spreads, iron condors and iron flies): NSE Clearing gives no exposure-margin
offset for them. Its equity-derivatives margins page says the calendar-spread benefit in extreme loss margin "is not provided
for option contracts as exposure is made applicable only for short positions", and as no extreme loss margin is levied on
long positions "there cannot be any offset" (the only exposure spread benefit is for futures calendar spreads, charged on
one third of the far-month value) — page text as indexed by a web search on 18 Nov 2026 (the page refuses scripted fetches).
So no offset is applied here. But on a hedged spread exposure is most of the margin, so
the broker's lower notional matters more: on the Bank Nifty condor above Zerodha's total was ₹81,080 against ₹88,964 here
(exposure 10.5% lower), on the naked NIFTY call only 1.1% lower in total. Hedged results therefore carry `hedged_note` and
`broker_low` (SPAN + exposure × the ratio seen on that condor) so the answer gives the observed range, not a false precision.

Not modelled (so the result says "unavailable" rather than guess): positions spanning more than one expiry (calendar
spread charge), additional/ad-hoc or surveillance margins on stocks, delivery margins, and broker add-ons.
"""
from datetime import date
import io
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from core import Block
from public_market import NoRedirect

SPAN_URL = "https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.{d}.{kind}.zip"
SPAN_PATTERN = re.compile(r"^https://nsearchives\.nseindia\.com/archives/nsccl/span/nsccl\.\d{8}\.(?:s|i[0-9]{1,2})\.zip$")
SPAN_LIMIT = 48 * 1024 * 1024
UNZIPPED_LIMIT = 400 * 1024 * 1024
INTRADAY_FILES = 6
LABEL = "brokers' figures typically within ±10%; your broker's basket margin is final"
UNAVAILABLE = "margin unavailable — check your broker's basket margin"
EXPOSURE_SOURCE = "https://www.nseindia.com/static/products-services/equity-derivatives-margins"
EXPIRY_DAY_CIRCULAR = "SEBI/HO/MRD/TPD-1/P/CIR/2024/132 (1 Oct 2024)"
EXPIRY_DAY_SOURCE = "SEBI circular SEBI/HO/MRD/TPD-1/P/CIR/2024/132"
INDEX_ELM, INDEX_DEEP_OTM_ELM, INDEX_LONG_DATED_ELM, INDEX_EXPIRY_DAY_ADD = 0.02, 0.03, 0.05, 0.02
STOCK_ELM, STOCK_DEEP_OTM_ELM = 0.035, 0.0525
SCENARIOS = 16
# Zerodha's exposure ÷ NSE's published 2% × close × units, lowest seen on the reconciled BANKNIFTY 23 Nov iron condors:
# 18 Nov 2026 ₹66,844.68 ÷ ₹74,727.90 (0.894, close 62,273.25); 19 Nov 2026 ₹66,844.68 ÷ ₹75,675.60 (0.883, close 63,063). The
# broker's exposure per lot did not move with the index (the same ₹66,844.68 and NIFTY ₹31,196.56 both days, and NIFTY's also
# on 18 Sep 2026), so the ratio falls as the index rises; the lowest one seen keeps every observation inside the range. Used
# only to show a range on hedged index positions.
BROKER_EXPOSURE_SEEN = 66844.68 / (0.02 * 63063.00 * 60)
# Zerodha's exposure per short unit has stayed fixed while the index moved: NIFTY ₹31,196.56 per lot of 65 (18 Sep, 18 Nov,
# 19 Nov 2026; ₹62,393 for the two short lots of an iron fly on Sat 21 Nov 2026) and BANKNIFTY ₹66,844.68 per two lots of 30
# (18–19 Nov 2026; ₹33,422 for one short lot of a bear put spread on 21 Nov 2026) — 2% of a fixed per-unit base. With the
# index above that base the broker's exposure is lower than NSE's 2% × close by base ÷ close (0.862 for BANKNIFTY at the
# 20 Nov close 64,635.20), more than the ratio above, so the range's low end uses whichever is lower.
BROKER_EXPOSURE_BASE = {"NIFTY": 31196.56 / 65 / 0.02, "BANKNIFTY": 66844.68 / 60 / 0.02}
BROKER_SEEN_TEXT = "Zerodha, Bank Nifty and Nifty spreads, 18–21 Nov 2026"
WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
HEDGE_SOURCE = "NSE Clearing's equity-derivatives margins page"


def span_request(url):
    """Download one NSE Clearing SPAN zip; nothing else is fetched here (exact URL pattern, no redirects, size cap)."""
    if not SPAN_PATTERN.match(url):
        raise Block("PUBLIC: unsupported source")
    req = urllib.request.Request(url, headers={"User-Agent": "IndiaDayTrader personal research", "Accept": "application/zip"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=60) as response:
            raw = response.read(SPAN_LIMIT + 1)
    except (OSError, ValueError):
        raise Block("PUBLIC: SPAN file unavailable") from None
    if len(raw) > SPAN_LIMIT:
        raise Block("PUBLIC: oversized response")
    return raw


def candidates(data_date, today, *, market_day=True):
    """SPAN files to try, best first. On a session after the data date: today's latest intraday file, then the data date's
    end-of-day file; on the data date itself (evening, or an --input chain): the end-of-day file first, then today's
    intraday files. Returns [(url, label)]."""
    day = date.fromisoformat(data_date)
    intraday = [(SPAN_URL.format(d=today.strftime("%Y%m%d"), kind=f"i{n}"), f"{today.isoformat()} intraday file i{n}")
                for n in range(INTRADAY_FILES, 0, -1)] if market_day and today >= day else []
    final = [(SPAN_URL.format(d=day.strftime("%Y%m%d"), kind="s"), f"{data_date} end-of-day file")]
    return intraday + final if today > day else final + intraday


def fetch_span(symbol, data_date, today, *, fetcher, market_day=True):
    """The best available SPAN file parsed for `symbol`; a dict with status 'ok' or 'unavailable' (never raises)."""
    tried = []
    for url, label in candidates(data_date, today, market_day=market_day):
        try:
            raw = fetcher(url)
        except Block:
            tried.append(label)
            continue
        try:
            span = parse_span(raw, symbol)
        except (Block, ValueError, KeyError, TypeError, AttributeError, ET.ParseError, zipfile.BadZipFile, UnicodeDecodeError) as error:
            return {"status": "unavailable", "note": f"{UNAVAILABLE} (the NSE SPAN file {label} could not be read for {symbol}: {error})"}
        span.update(status="ok", url=url, file_label=label)
        return span
    days = ", ".join(dict.fromkeys(label.split(" ")[0] for label in tried))
    return {"status": "unavailable", "note": f"{UNAVAILABLE} (NSE Clearing SPAN files for {days} not reachable)"}


def begin_of_day_candidates(data_date, today, *, market_day=True):
    """The begin-of-day SPAN file for margin, best first: (on a later session) today's i1, then the data date's end-of-day
    file. NSE Clearing builds i1 from that close but re-values the risk arrays to the new date, which matters after a weekend
    and on expiry day (23 Nov 2026 i1: NIFTY iron fly SPAN ₹13,628.55 against ₹13,038.35 on the 20 Nov s file)."""
    day = date.fromisoformat(data_date)
    out = [(SPAN_URL.format(d=day.strftime("%Y%m%d"), kind="s"), f"{data_date} end-of-day file")]
    if market_day and today > day:
        out.insert(0, (SPAN_URL.format(d=today.strftime("%Y%m%d"), kind="i1"), f"{today.isoformat()} begin-of-day file i1"))
    return out


def _first(symbol, items, fetcher):
    """Like fetch_span over an explicit [(url, label)] list."""
    tried = []
    for url, label in items:
        try:
            raw = fetcher(url)
        except Block:
            tried.append(label)
            continue
        try:
            span = parse_span(raw, symbol)
        except (Block, ValueError, KeyError, TypeError, AttributeError, ET.ParseError, zipfile.BadZipFile, UnicodeDecodeError) as error:
            return {"status": "unavailable", "note": f"{UNAVAILABLE} (the NSE SPAN file {label} could not be read for {symbol}: {error})"}
        span.update(status="ok", url=url, file_label=label)
        return span
    days = ", ".join(dict.fromkeys(label.split(" ")[0] for label in tried))
    return {"status": "unavailable", "note": f"{UNAVAILABLE} (NSE Clearing SPAN files for {days} not reachable)"}


def is_intraday(span):
    """True for today's i2, i3, … (an intraday re-computation), False for an end-of-day or begin-of-day (i1) file."""
    label = (span or {}).get("file_label") or ""
    return "intraday file i" in label and not label.endswith("intraday file i1")


def fetch_spans(symbol, data_date, today, *, fetcher, market_day=True):
    """(latest, for_margin): the newest SPAN file (today's intraday prices, for an 'if placed now' estimate) and the
    begin-of-day file that margin is computed on (see the module notes). The two downloads run side by side; when the
    latest file is already the begin-of-day one it serves both. The margin file carries the latest one as 'latest' so the
    intraday re-computation can be shown alongside."""
    from concurrent.futures import ThreadPoolExecutor
    day = date.fromisoformat(data_date)
    if not (market_day and today > day):
        span = fetch_span(symbol, data_date, today, fetcher=fetcher, market_day=market_day)
        return span, span
    with ThreadPoolExecutor(max_workers=2) as pool:
        latest_job = pool.submit(fetch_span, symbol, data_date, today, fetcher=fetcher, market_day=market_day)
        bod_job = pool.submit(_first, symbol, begin_of_day_candidates(data_date, today, market_day=market_day), fetcher)
        latest, bod = latest_job.result(), bod_job.result()
    if not is_intraday(latest):
        return latest, (bod if bod.get("status") == "ok" else latest)
    if bod.get("status") != "ok":
        fallback = dict(latest, bod_note="the begin-of-day SPAN file was unavailable, so margin uses the intraday file "
                                         "(brokers' calculators used the begin-of-day file on 18 Nov 2026)")
        return latest, fallback
    return latest, dict(bod, latest=latest)


AFTER_CLOSE_NOTE = "the ones Zerodha's calculator used after the close (matched to the rupee on 19 Nov 2026)"


def closing_file(span, day):
    """True when `span` is `day`'s closing intraday file: an intraday SPAN file NSE Clearing created on `day` at or after
    15:30 IST (i5 at 15:32 on 18 and 19 Nov 2026), which carries the closing prices and margin parameters."""
    if not span or span.get("status") != "ok" or "intraday file i" not in (span.get("file_label") or ""):
        return False
    created = span.get("created") or ""
    return len(created) >= 12 and created[:8] == day.strftime("%Y%m%d") and created[8:12] >= "1530"


def fetch_margin_spans(symbol, data_date, today, *, fetcher, market_day=True, after_close=False, next_session=None):
    """(latest, for_margin) chosen by time of day (see the module notes): after the close on a trading day, today's closing
    intraday file serves both the 'if placed now' prices and the margin; at a weekend or on a holiday the next session's
    begin-of-day file (`next_session`, ISO date) when it is out, else the last end-of-day file; otherwise `fetch_spans`."""
    if not market_day and next_session:
        nxt = date.fromisoformat(next_session)
        items = [(SPAN_URL.format(d=nxt.strftime("%Y%m%d"), kind="i1"), f"{next_session} begin-of-day file i1"),
                 (SPAN_URL.format(d=date.fromisoformat(data_date).strftime("%Y%m%d"), kind="s"), f"{data_date} end-of-day file")]
        span = _first(symbol, items, fetcher)
        if span.get("status") == "ok" and span["file_label"].startswith(next_session):
            span = dict(span, margin_basis="next_session", next_session=next_session)
        return span, span
    if after_close and market_day:
        intraday = [(SPAN_URL.format(d=today.strftime("%Y%m%d"), kind=f"i{n}"), f"{today.isoformat()} intraday file i{n}")
                    for n in range(INTRADAY_FILES, 0, -1)]
        latest = _first(symbol, intraday, fetcher)
        if closing_file(latest, today):
            return latest, dict(latest, margin_basis="closing")
    return fetch_spans(symbol, data_date, today, fetcher=fetcher, market_day=market_day)


def _blocks(text, tag, code):
    """Each <tag>…</tag> element (phyPf/futPf/oopPf/ccDef) whose pfCode (or cc) is `code`, parsed on its own."""
    start, out = 0, []
    open_tag, close_tag = f"<{tag}>", f"</{tag}>"
    key = (f"<pfCode>{code}</pfCode>", f"<cc>{code}</cc>")
    while True:
        i = text.find(open_tag, start)
        if i < 0:
            return out
        j = text.find(close_tag, i)
        if j < 0:
            raise Block("SPAN: truncated file")
        head = text[i:i + 600]
        if any(k in head for k in key):
            out.append(ET.fromstring(text[i:j + len(close_tag)]))
        start = j + len(close_tag)


def _num(el, path):
    node = el.find(path)
    if node is None or node.text is None or not node.text.strip():
        raise Block(f"SPAN: missing {path}")
    return float(node.text)


def _risk_array(el):
    ra = el.find("ra")
    values = [float(a.text) for a in ra.findall("a")] if ra is not None else []
    if len(values) != SCENARIOS:
        raise Block("SPAN: risk array must have 16 scenarios")
    return values


def _iso(yyyymmdd):
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def parse_span(raw, symbol):
    """Risk parameters for one underlying from an NSE Clearing SPAN zip (or the bare .spn XML bytes)."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".spn")]
            if len(names) != 1:
                raise Block("SPAN: unexpected archive")
            info = archive.getinfo(names[0])
            if info.file_size > UNZIPPED_LIMIT:
                raise Block("SPAN: oversized file")
            name, data = names[0], archive.read(names[0])
    else:
        name, data = "spn", raw
    text = data.decode("utf-8", "replace")
    if "<spanFile>" not in text[:2000]:
        raise Block("SPAN: not a SPAN XML file")
    when = re.search(r"<pointInTime>\s*<date>(\d{8})</date>", text)
    created = re.search(r"<created>(\d{8,12})</created>", text[:4000])
    qualifier = re.search(r"<setlQualifier>([^<]*)</setlQualifier>", text[:20000])
    if not when:
        raise Block("SPAN: no business date")
    phys = _blocks(text, "phyPf", symbol)
    if not phys:
        raise Block(f"SPAN: {symbol} not in the file")
    phy = phys[0].find("phy")
    out = {"symbol": symbol, "file": name, "business_date": _iso(when.group(1)), "created": created.group(1) if created else None,
           "qualifier": (qualifier.group(1).strip() if qualifier else None), "underlying_close": _num(phy, "p"),
           "price_scan": None, "vol_scan": None, "futures": {}, "options": {}, "som_rate": 0.0}
    for pf in _blocks(text, "futPf", symbol):
        for fut in pf.findall("fut"):
            out["futures"][_iso(fut.find("pe").text.strip())] = {"price": _num(fut, "p"), "ra": _risk_array(fut)}
    for pf in _blocks(text, "oopPf", symbol):
        for series in pf.findall("series"):
            expiry = _iso(series.find("pe").text.strip())
            scan = series.find("scanRate")
            if scan is not None and out["price_scan"] is None:
                out["price_scan"], out["vol_scan"] = _num(scan, "priceScan"), _num(scan, "volScan")
            for opt in series.findall("opt"):
                kind = {"C": "CE", "P": "PE"}[opt.find("o").text.strip()]
                out["options"][(expiry, kind, _num(opt, "k"))] = {"price": _num(opt, "p"), "ra": _risk_array(opt)}
    for cc in _blocks(text, "ccDef", symbol):
        rates = [float(v.text) for v in cc.findall("somTiers/tier/rate/val") if v.text and v.text.strip()]
        if rates:
            out["som_rate"] = max(rates)
    if not out["options"] and not out["futures"]:
        raise Block(f"SPAN: no {symbol} derivatives in the file")
    return out


def exposure_rate(leg, *, index, close, expiry, trade_day):
    """Exposure (extreme loss) margin rate for one leg, with the rule that set it; None for long options (no exposure)."""
    if leg["type"] == "FUT":
        return (INDEX_ELM if index else STOCK_ELM), "futures"
    if leg["units"] > 0:
        return None, "long option: no exposure margin"
    otm = (leg["strike"] / close - 1) if leg["type"] == "CE" else (1 - leg["strike"] / close)
    days = (date.fromisoformat(expiry) - date.fromisoformat(trade_day)).days
    if index:
        if days > 273:
            rate, rule = INDEX_LONG_DATED_ELM, "short index option, more than 9 months to expiry"
        elif otm > 0.10:
            rate, rule = INDEX_DEEP_OTM_ELM, "short index option more than 10% out of the money"
        else:
            rate, rule = INDEX_ELM, "short index option"
        if days == 0:
            rate, rule = rate + INDEX_EXPIRY_DAY_ADD, rule + " + 2% on expiry day"
        return rate, rule
    if otm > 0.30:
        return STOCK_DEEP_OTM_ELM, "short stock option more than 30% out of the money"
    return STOCK_ELM, "short stock option"


def broker_ratio(symbol, close):
    """The lowest broker exposure ÷ NSE's 2% × close seen for `symbol` at this close (see BROKER_EXPOSURE_BASE)."""
    ratio = BROKER_EXPOSURE_SEEN
    base = BROKER_EXPOSURE_BASE.get(symbol)
    if base and close:
        ratio = min(ratio, base / close)
    return ratio


def day_label(iso):
    """'Mon 23 Nov' for '2026-11-23'."""
    d = date.fromisoformat(str(iso))
    return f"{WEEKDAY_NAMES[d.weekday()]} {d.day} {d:%b}"


def estimate(span, legs, *, expiry, trade_day, index, close=None, today=None):
    """SPAN + exposure for `legs` (dicts with type CE/PE/FUT, strike, units signed: long positive; optional 'expiry');
    `close` is the underlying's last closing price for the exposure notional (default: the price in the SPAN file);
    `trade_day` is the entry day the margin is for (ISO) and `today` the calendar day now (so the text can tell "today"
    from a later session).

    Returns {'available': True, 'span', 'scan_risk', 'net_option_value', 'short_option_minimum', 'exposure', 'total',
    'expiry_extra', 'normal_total', 'worst_scenario', 'exposure_rows', 'file', 'label', …} or {'available': False, 'note'}
    — never a guess. `total` includes the expiry-day extra when `trade_day` is the expiry day; `normal_total` never does."""
    if not span or span.get("status") != "ok":
        return {"available": False, "note": (span or {}).get("note") or UNAVAILABLE}
    expiries = {l.get("expiry") or expiry for l in legs}
    if len(expiries) > 1:
        return {"available": False, "note": f"{UNAVAILABLE} (legs span more than one expiry; the calendar spread charge is not modelled)"}
    losses, nov, short_units, rows = [0.0] * SCENARIOS, 0.0, 0, []
    # Exposure notional for short options: the underlying's last close (NSE Clearing's rule). Intraday SPAN files carry an
    # intraday underlying price, so callers pass the exchange close they hold; the file's own price is the fallback.
    close = close or span["underlying_close"]
    for leg in legs:
        e = leg.get("expiry") or expiry
        if leg["type"] == "FUT":
            contract = span["futures"].get(e)
        else:
            contract = span["options"].get((e, leg["type"], float(leg["strike"])))
        if not contract:
            name = "future" if leg["type"] == "FUT" else f"{leg['strike']:g} {leg['type']}"
            return {"available": False, "note": f"{UNAVAILABLE} ({span['symbol']} {e} {name} is not in the NSE SPAN file)"}
        units = leg["units"]
        losses = [x + units * r for x, r in zip(losses, contract["ra"])]
        if leg["type"] != "FUT":
            nov += units * contract["price"]
            short_units += max(0, -units)
        rate, rule = exposure_rate(leg, index=index, close=close, expiry=e, trade_day=trade_day)
        notional = abs(units) * (contract["price"] if leg["type"] == "FUT" else close)
        extra = notional * INDEX_EXPIRY_DAY_ADD if rate and rule.endswith("on expiry day") else 0.0
        later = (notional * INDEX_EXPIRY_DAY_ADD if index and rate and not extra and leg["type"] != "FUT" and units < 0
                 and e == expiry else 0.0)
        rows.append({"leg": leg, "rate": rate, "rule": rule, "notional": notional if rate else 0.0,
                     "exposure": notional * rate if rate else 0.0, "expiry_extra": extra, "extra_if_held": later})
    scan = max(0.0, max(losses))
    som = span.get("som_rate", 0.0) * short_units
    span_margin = max(0.0, max(scan, som) - nov)
    exposure = sum(r["exposure"] for r in rows)
    extra = sum(r["expiry_extra"] for r in rows)
    normal_exposure = exposure - extra
    worst = max(range(SCENARIOS), key=lambda i: losses[i]) + 1
    short_legs = any(l["units"] < 0 for l in legs)
    hedged = short_legs and any(l["type"] != "FUT" and l["units"] < 0 for l in legs) and any(l["units"] > 0 for l in legs)
    hedged_note = broker_low = broker_low_normal = None
    if hedged and exposure > 0:
        ratio = broker_ratio(span.get("symbol"), close) if index else None
        broker_low = span_margin + exposure * ratio if index else None
        broker_low_normal = span_margin + normal_exposure * ratio if index else None
        share = exposure / (span_margin + exposure)
        hedged_note = (f"Hedged position: exposure is {share:.0%} of this margin and NSE gives hedged option positions no exposure "
                       f"offset (each short option pays it on its own notional; {HEDGE_SOURCE}), but broker calculators have charged "
                       "less exposure: Zerodha's was 11.7% lower on a Bank Nifty iron condor on 19 Nov 2026 (total ₹80,494 against "
                       "₹89,325 by this method; 10.5% on 18 Nov) and 13.8% lower on a Bank Nifty bear put spread on 21 Nov 2026"
                       + (f", so expect roughly ₹{inr(broker_low)}–₹{inr(span_margin + exposure)}" if broker_low is not None else "")
                       + "; your broker's basket margin is final")
    intraday = None
    if span.get("latest") and span["latest"].get("status") == "ok":
        later = estimate({k: v for k, v in span["latest"].items() if k != "latest"}, legs, expiry=expiry, trade_day=trade_day,
                         index=index, close=close, today=today)
        if later.get("available"):
            intraday = {"span": later["span"], "total": later["total"], "file": later["file"], "file_label": later["file_label"],
                        "created": span["latest"].get("created"), "underlying": span["latest"].get("underlying_close")}
    return {"available": True, "span": span_margin, "intraday": intraday, "bod_note": span.get("bod_note"), "scan_risk": scan, "net_option_value": nov, "short_option_minimum": som,
            "exposure": exposure, "total": span_margin + exposure, "worst_scenario": worst, "exposure_rows": rows,
            "expiry_extra": extra, "normal_exposure": normal_exposure, "normal_total": span_margin + normal_exposure,
            "extra_if_held": sum(r["extra_if_held"] for r in rows), "entry_day": trade_day, "expiry": expiry,
            "entry_is_today": bool(today) and str(today) == str(trade_day),
            "hedged": hedged, "hedged_note": hedged_note, "broker_low": broker_low, "broker_low_normal": broker_low_normal,
            "price_scan": span.get("price_scan"), "underlying_close": close, "file": span.get("file"), "url": span.get("url"),
            "file_label": span.get("file_label"), "business_date": span.get("business_date"), "label": LABEL,
            "margin_basis": span.get("margin_basis"), "created": span.get("created"), "next_session": span.get("next_session"),
            "basis": ("SPAN = max(scanning risk over NSE's 16 scenarios, short-option minimum) − net option value; exposure "
                      f"{'2% (index)' if index else '3.5% (stock)'} of notional on futures and short options (NSE Clearing ELM table"
                      f"{'; +2% on expiry day for short index options, SEBI circular ' + EXPIRY_DAY_CIRCULAR if index else ''})")}


HEDGED_LABEL = "brokers have charged hedged positions less, see the range; your broker's basket margin is final"


def entry_text(m):
    """'if you enter on Mon 23 Nov (expiry day; the next session, which this margin assumes)' or 'to enter today (expiry day)'."""
    if m.get("entry_is_today"):
        return "to enter today (expiry day)"
    return f"if you enter on {day_label(m['entry_day'])} (expiry day; the next session, assumed as the entry day)"


def margin_text(m, *, short=False):
    """'indicative ₹1,56,003 (SPAN ₹1,23,000 + exposure ₹33,003; brokers' figures typically within ±10%; your broker's
    basket margin is final)' — `short` leaves out the band — or the unavailable note. When the entry day is the expiry day
    the normal figure and the expiry-day extra are given apart (the ±10% band is for the normal figure only), and hedged
    index positions point to the observed broker range (`hedged_text`) instead of the ±10% band."""
    if not m or not m.get("available"):
        return (m or {}).get("note") or UNAVAILABLE
    if m["total"] <= 0.5:
        return "≈ ₹0 blocked beyond the premium you pay (long options only)"
    label = HEDGED_LABEL if m.get("broker_low") is not None else LABEL
    extra = m.get("expiry_extra") or 0.0
    if extra > 0.5:
        exposure = m.get("normal_exposure", m["exposure"] - extra)
        normal = m.get("normal_total", m["total"] - extra)
        band = "" if short or m.get("broker_low") is not None else "; brokers' figures typically within ±10% of this part"
        before = ("brokers add the extra today too" if m.get("entry_is_today") else
                  f"broker calculators show the lower ₹{inr(normal)} before that day")
        return (f"indicative ≈₹{inr(normal)} normal (SPAN ₹{inr(m['span'])} + exposure ₹{inr(exposure)}{band}) + ₹{inr(extra)} "
                f"expiry-day extra 2% on short index options ({EXPIRY_DAY_SOURCE}) = ₹{inr(m['total'])} "
                f"{entry_text(m)}; {before}; your broker's basket margin is final")
    parts = f"SPAN ₹{inr(m['span'])} + exposure ₹{inr(m['exposure'])}"
    return f"indicative ₹{inr(m['total'])} ({parts})" if short else f"indicative ₹{inr(m['total'])} ({parts}; {label})"


def held_extra_text(m):
    """'Held into expiry day (Mon 23 Nov), the short index legs add a further 2% exposure that day, ≈₹67,082 (SEBI circular …)'
    when the entry is before expiry day and the position has short index options of that expiry; None otherwise."""
    if not m or not m.get("available") or (m.get("expiry_extra") or 0) > 0.5 or (m.get("extra_if_held") or 0) <= 0.5:
        return None
    return (f"Held into expiry day ({day_label(m['expiry'])}), the short index legs add a further 2% exposure that day, "
            f"≈₹{inr(m['extra_if_held'])} ({EXPIRY_DAY_SOURCE})")


def hedged_text(m):
    """'Hedged: NSE gives no exposure offset for hedged options, but broker calculators have charged less exposure (≈₹81,081
    here at the ratio seen on 18 Nov 2026)' for a hedged index position, the longer note for others, or None."""
    if not m or not m.get("available") or not m.get("hedged_note"):
        return None
    if m.get("broker_low") is None:
        return m["hedged_note"]
    head = (f"Hedged position: NSE gives no exposure offset for hedged options ({HEDGE_SOURCE}), but broker calculators have charged "
            f"less exposure — at the largest gap seen ({BROKER_SEEN_TEXT}) ")
    if (m.get("expiry_extra") or 0) > 0.5 and m.get("broker_low_normal") is not None:
        return (head + f"the normal figure would be ≈₹{inr(m['broker_low_normal'])}, so expect roughly "
                f"₹{inr(m['broker_low_normal'])}–₹{inr(m['normal_total'])} before expiry day and ₹{inr(m['broker_low'])}–"
                f"₹{inr(m['total'])} with the expiry-day extra")
    return head + f"this would be ≈₹{inr(m['broker_low'])}, so expect roughly ₹{inr(m['broker_low'])}–₹{inr(m['total'])}"


def intraday_text(m):
    """'NSE's 11:03 IST intraday file (i2) re-computes SPAN at ₹14,314 (total ₹89,042) at BANKNIFTY 62,044.35' or None."""
    x = (m or {}).get("intraday")
    if not x:
        return None
    created = x.get("created") or ""
    when = f"{created[8:10]}:{created[10:12]} IST " if len(created) >= 12 else ""
    return (f"NSE's {when}intraday file ({x['file']}) re-computes SPAN at ₹{inr(x['span'])} (total ₹{inr(x['total'])})"
            + (f", with the index at {x['underlying']:,.2f}" if x.get("underlying") else "")
            + "; brokers' calculators matched the begin-of-day figure when checked")


def inr(v):
    """Indian digit grouping without decimals: 156003 -> '1,56,003'."""
    n = int(round(abs(v)))
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        s = ",".join(([head] if head else []) + groups) + "," + tail
    return ("-" if v < 0 and n else "") + s


def source_text(m):
    if not m or not m.get("available"):
        return None
    return f"NSE Clearing SPAN file {m['file']} ({m['file_label']})"
