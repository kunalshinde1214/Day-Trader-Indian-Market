"""Fundamental and technical stock screener for Indian indices, plus shared peer and valuation helpers.

Universes come from NSE's published index constituent lists; prices and company financials
come from the same free Yahoo feeds as stock_analysis.py. Research only; no orders.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, datetime, timedelta
import io
import json
import operator
from pathlib import Path
import re
import statistics
import sys
import time

from core import Block, instant
from public_market import request

INDEX_URL = "https://nsearchives.nseindia.com/content/indices/ind_{}list.csv"
UNIVERSES = {
    "nifty50": "nifty50", "niftynext50": "niftynext50", "nifty100": "nifty100", "nifty200": "nifty200", "nifty500": "nifty500",
    "midcap150": "niftymidcap150", "smallcap250": "niftysmallcap250", "bank": "niftybank", "psubank": "niftypsubank", "it": "niftyit",
    "pharma": "niftypharma", "healthcare": "niftyhealthcare", "auto": "niftyauto", "fmcg": "niftyfmcg", "metal": "niftymetal",
    "realty": "niftyrealty", "energy": "niftyenergy", "oilgas": "niftyoilgas", "media": "niftymedia", "consumerdurables": "niftyconsumerdurables",
}
SECTORS = ("bank", "psubank", "it", "pharma", "healthcare", "auto", "fmcg", "metal", "realty", "oilgas", "media", "consumerdurables", "energy")
FIELDS = {
    "pe": "P/E", "pb": "P/B", "roe": "ROE", "net_margin": "net margin", "operating_margin": "operating margin",
    "debt_to_equity": "debt/equity", "revenue_growth": "revenue growth", "eps_growth": "EPS growth",
    "dividend_yield": "dividend yield (12m)", "cash_dividend_yield": "cash dividends / mkt cap",
    "fcf_yield": "FCF yield", "market_cap_cr": "market cap (₹ cr)", "return_1y": "1-year return", "from_high": "distance from 52-week high",
    "rsi14": "RSI(14)", "price": "price", "recurring_yield": "dividend yield ex-specials", "payout_ratio": "payout (cash div ÷ profit)",
    "dividend_years": "years paid in a row", "dps_cagr_5y": "DPS growth 5y (a year)", "fcf_cover": "FCF ÷ dividends",
    "debt_cr": "debt incl. leases (₹ cr)", "borrowings_cr": "borrowings ex leases (₹ cr)",
}
PERCENT = {"roe", "net_margin", "operating_margin", "revenue_growth", "eps_growth", "dividend_yield", "cash_dividend_yield", "fcf_yield",
           "return_1y", "from_high", "recurring_yield", "payout_ratio", "dps_cagr_5y"}
# Fields that come from the fundamentals feed; the rest come from the daily chart.
STATEMENT_FIELDS = {"pe", "pb", "roe", "net_margin", "operating_margin", "debt_to_equity", "revenue_growth", "eps_growth",
                    "cash_dividend_yield", "fcf_yield", "market_cap_cr", "payout_ratio", "fcf_cover", "debt_cr", "borrowings_cr"}
# Fields that need the ten-year dividend history (one more small request per stock).
DIVIDEND_FIELDS = {"dividend_years", "dps_cagr_5y"}
PRESETS = {
    # Steady payers with low debt: a recurring (ex-special) yield of 1.5%+, five or more years of dividends in a row and
    # debt/equity of 0.5 or less. Banks and other financials are left out by the debt filter (not meaningful for them).
    "dividend": {"where": ["recurring_yield>=1.5%", "dividend_years>=5", "debt_to_equity<=0.5"], "sort": "recurring_yield",
                 "columns": ("price", "market_cap_cr", "recurring_yield", "dividend_yield", "payout_ratio", "fcf_cover", "dividend_years",
                             "dps_cagr_5y", "debt_to_equity", "debt_cr", "roe", "return_1y"),
                 # A short list (--limit 10 or fewer) keeps at most two stocks per sector unless --max-per-sector says otherwise:
                 # the Nifty 100 dividend screen of Nov 2026 put six IT companies in its top fourteen.
                 "max_per_sector_short": 2},
}
SHORT_LIST = 10  # a --limit this small is a shortlist, which a preset may diversify by sector
OPS = {"<=": operator.le, ">=": operator.ge, "<": operator.lt, ">": operator.gt, "=": operator.eq}
MAX_SYMBOLS = 500
WORKERS = 4                 # gentle on the public feeds
BIG_WORKERS = 6             # a live screen of more than 100 stocks (measured on the Nifty 500 in Nov 2026; see references/screener.md)
UNIT_SERIES = {"RR", "IV"}  # REIT and InvIT units: listed in some NSE indices but not company shares
DEFAULT_COLUMNS = ("price", "market_cap_cr", "pe", "pb", "roe", "net_margin", "debt_to_equity", "revenue_growth", "return_1y")


def placeholder(row):
    """NSE parks index slots with entries such as DUMMYHEG (ISIN DUM...) during corporate actions; they are not stocks."""
    symbol = (row.get("Symbol") or "").strip().upper()
    isin = (row.get("ISIN Code") or "").strip().upper()
    return symbol.startswith("DUMMY") or isin.startswith("DUM")


def index_members(name, *, fetcher=request):
    if name not in UNIVERSES:
        raise Block(f"SCREENER: unknown universe {name}; choose from {', '.join(sorted(UNIVERSES))}")
    raw = fetcher(INDEX_URL.format(UNIVERSES[name]))
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("Symbol") and not placeholder(r)]
    if not rows:
        raise Block(f"SCREENER: {name} constituent list was empty or changed format")
    return [{"symbol": r["Symbol"].strip(), "name": (r.get("Company Name") or "").strip(), "industry": (r.get("Industry") or "").strip(),
             "series": (r.get("Series") or "").strip()} for r in rows]


def universe_members(names, *, fetcher=request):
    """Members of one or more universes, merged in order and de-duplicated by symbol."""
    names = [names] if isinstance(names, str) else list(dict.fromkeys(names))
    merged = {}
    for name in names:
        for m in index_members(name, fetcher=fetcher):
            merged.setdefault(m["symbol"], m)
    return list(merged.values())


def size_tiers(fetcher):
    """Symbol -> 'large' (Nifty 100), 'mid' (Midcap 150) from NSE's lists; other Nifty 500 names are 'small'. {} if unavailable."""
    tiers = {}
    for name, tier in (("nifty100", "large"), ("midcap150", "mid")):
        members = _safe_members(name, fetcher)
        if members is None:
            return {}
        tiers.update({m["symbol"]: tier for m in members})
    return tiers


ADJACENT = {"large": ("mid",), "mid": ("large", "small"), "small": ("mid",)}

# NSE's sectoral indices and its Nifty 500 "Industry" field mix business types (Nifty Auto put TMCV's trucks next to
# two-wheeler and car makers: median P/E 47.8 in Nov 2026). For well-known cases the peers are the companies in the
# same NSE industry classification (the fourth level, "basic industry", shown on each company's NSE quote page),
# listed here by hand. Checked on 18 Nov 2026 against Screener.in's industry pages, which use that classification.
SUB_INDUSTRIES = {
    "commercial vehicles": {
        "symbols": ["TMCV", "ASHOKLEY", "SMLMAH"], "also": ["EICHERMOT"],
        "note": "Eicher Motors is classed under 2/3 wheelers (Royal Enfield) but is included for its Volvo Eicher trucks-and-buses joint venture",
        "source": "https://www.screener.in/market/IN07/IN0702/IN070202/IN070202002/",
    },
    "passenger cars & utility vehicles": {
        "symbols": ["MARUTI", "M&M", "HYUNDAI", "TMPV", "FORCEMOT"],
        "source": "https://www.screener.in/market/IN02/IN0201/IN020101/IN020101001/",
    },
    "2/3 wheelers": {
        "symbols": ["BAJAJ-AUTO", "EICHERMOT", "TVSMOTOR", "HEROMOTOCO", "ATHERENERG", "OLAELEC"],
        "source": "https://www.screener.in/market/IN02/IN0201/IN020101/IN020101002/",
    },
}
SUB_INDUSTRY_CHECKED = "2026-11-18"


# NSE's Nifty 500 "Industry" field is a sector: "Financial Services" puts Bajaj Finance next to banks, insurers, an
# exchange and state power lenders (peer P/Es 4.1 to 85.4 in Nov 2026). Within that sector, the company name says which
# business it is in well enough to compare like with like; first match wins, and a company matching none stays "other".
FINANCIAL_KINDS = (
    ("banks", re.compile(r"\bBank\b", re.I)),
    ("insurers", re.compile(r"Insurance|Assurance|\bLife\b", re.I)),
    ("state infrastructure lenders", re.compile(r"Power Finance|Railway Finance|Renewable Energy Development|Housing (?:&|and) Urban|^REC\b", re.I)),
    ("capital-market companies", re.compile(r"Asset Management|\bAMC\b|Securities|Exchange|Depository|Broking|Wealth|\bWAM\b|Computer Age|"
                                            r"Registry|Capital Markets|^BSE\b|Motilal|Angel One|Nuvama|Fintech", re.I)),
    ("lending companies (NBFCs and housing finance)", re.compile(r"Financ|Fincorp|Credit|Cards|Housing|Homes|Leasing|Gold Loan", re.I)),
)
SECTOR_SPLITS = {"Financial Services": FINANCIAL_KINDS}
MIN_KIND_PEERS = 3  # same-kind peers needed before the sector is narrowed to them


def business_kind(name, industry):
    """The kind of business inside a broad NSE sector, from the company name ('banks', 'insurers', ...), or None when the
    sector is not split or nothing matches."""
    for kind, pattern in SECTOR_SPLITS.get(industry or "", ()):
        if pattern.search(name or ""):
            return kind
    return None


def sub_industry(base):
    """(name, entry) of the curated NSE industry group that classifies `base`, else None ("also" members are peers
    added to a group, not members of it)."""
    return next(((n, e) for n, e in SUB_INDUSTRIES.items() if base in e["symbols"]), None)


def peers_for(symbol, *, fetcher=request, limit=40, min_same_tier=5):
    """Peers from the smallest NSE sectoral index containing the stock; falls back to the same Nifty 500 industry.

    In the industry fallback, companies of comparable size (same Nifty 100 / Midcap 150 / smallcap tier) come first,
    then the adjacent tier if the same tier has fewer than `min_same_tier`; the rest of the industry is used only when
    the tier lists are unavailable. REIT/InvIT units and NSE placeholder entries are never peers.
    """
    base = symbol.rsplit(".", 1)[0]
    curated = sub_industry(base)
    if curated:
        name, entry = curated
        peers = [{"symbol": s, "name": "", "industry": name} for s in entry["symbols"] + entry.get("also", []) if s != base]
        return {"basis": f"NSE industry '{name}' (curated list checked {SUB_INDUSTRY_CHECKED}; source {entry['source']})"
                         + (f"; {entry['note']}" if entry.get("note") else ""),
                "kind": "industry", "group_size": len(peers) + 1, "peers": peers, "capped": False}
    groups = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for name, members in zip(SECTORS, pool.map(lambda n: _safe_members(n, fetcher), SECTORS)):
            if members and any(m["symbol"] == base for m in members):
                groups[name] = members
    if groups:
        name, members = min(groups.items(), key=lambda kv: len(kv[1]))
        members = [m for m in members if m.get("series") not in UNIT_SERIES]
        basis = f"Nifty {name} index constituents"
    else:
        everyone = _safe_members("nifty500", fetcher) or []
        own = next((m for m in everyone if m["symbol"] == base), None)
        if not own or not own["industry"]:
            return {"basis": "no sectoral index or Nifty 500 industry found", "peers": []}
        members = [m for m in everyone if m["industry"] == own["industry"] and m.get("series") not in UNIT_SERIES]
        basis = f"Nifty 500 industry: {own['industry']}"
        kind = business_kind(own.get("name"), own["industry"])
        alike = [m for m in members if business_kind(m.get("name"), m["industry"]) == kind] if kind else []
        if kind and len(alike) - 1 >= MIN_KIND_PEERS:
            members = alike
            basis += f", {kind} only (business read from company names)"
        tiers = size_tiers(fetcher)
        if tiers:
            tier_of = lambda m: tiers.get(m["symbol"], "small")
            mine = tier_of(own)
            same = [m for m in members if tier_of(m) == mine]
            near = [m for m in members if tier_of(m) in ADJACENT[mine]]
            chosen = same if len(same) - 1 >= min_same_tier else same + near
            members = chosen
            basis += f", {mine}-cap companies" + ("" if chosen is same else " and the adjacent size tier") + " (tiers from Nifty 100 / Midcap 150 membership)"
    peers = [m for m in members if m["symbol"] != base]
    return {"basis": basis, "group_size": len(peers) + 1, "peers": peers[:limit], "capped": len(peers) > limit}


def _safe_members(name, fetcher):
    try:
        return index_members(name, fetcher=fetcher)
    except (Block, ValueError, KeyError, OSError):
        return None


def fetch_failure(error):
    """True for errors that mean the data did not arrive (network, rate limit, malformed reply), not that it is absent."""
    if isinstance(error, Block):
        return "unavailable" in str(error) or "oversized" in str(error)
    return isinstance(error, (ValueError, KeyError, TypeError, OSError))


def snapshot(symbol, *, fetcher=request, now=None, dividend_history=False, nse_fetcher=None, dividend_nse=None):
    """Compact valuation and momentum metrics for one stock; None fields when data is missing.

    Uses two years of daily bars so the 1-year return has the same definition as stock_analysis.py (close vs the
    close 250 sessions earlier), and the same renamed-symbol and demerger handling. With `nse_fetcher`, a company
    Yahoo shows in US dollars (Infosys) takes its annual revenue and EPS growth from its rupee results on NSE, as the
    single-stock report does.
    """
    import stock_analysis as sa
    now = now or instant()
    selected, history, notes = sa.prepare_history(sa.normalize(symbol), fetcher=fetcher, now=now, years=2)
    cut = notes["demerger"]
    closes = [b["close"] for b in history["bars"]]
    price = closes[-1]
    window = history["bars"][-sa.YEAR_SESSIONS:]
    done = closes[:-1] if sa.forming_bar(history["bars"], now) else closes  # RSI on completed sessions only
    out = {"symbol": selected, "name": history["name"], "price": price, "price_date": history["bars"][-1]["at"][:10],
           "price_note": history.get("price_note"), "renamed_note": notes["renamed"],
           # Scaling only changes screen figures when the demerger falls inside the 1-year window.
           "history_note": (f"{selected.replace('.NS', '')}: Yahoo does not adjust prices before the {cut['ex_date']} demerger of "
                            f"{sa.demerged_label(cut)}; they are scaled down so the 1-year figures compare like with like."
                            if cut and cut["mode"] == "scaled" and cut["sessions_since"] <= sa.YEAR_SESSIONS else None),
           "return_1y": sa.one_year_return(closes),
           "from_high": price / max(b["high"] for b in window) - 1, "rsi14": sa.rsi(done)}
    since = cut["record_date"] if cut and cut["mode"] == "provider" and not cut["minor"] else None
    dividends = sa.trailing_dividends(history, price, since=since)
    out["dividend_yield"] = dividends["dividend_yield"] if dividends else None
    out["recurring_yield"] = dividends["recurring_yield"] if dividends else None
    out["dividend_flags"] = dividends["flags"] if dividends else []
    out["dividend_latest"] = dividends.get("latest") if dividends else None
    try:
        series = sa.fundamentals(selected, fetcher=fetcher, now=now)
        # Split and bonus dates let the trailing EPS be checked on today's share basis (TRENT's 2026 bonus).
        f = sa.ratios(series, price, now, sa.share_changes(selected, history))
        sa.check_revenue(f, series)  # no exchange filings here: revenue that more than doubled is withheld, not shown
        # A demerger parent whose trailing EPS still includes the demerged businesses (Vedanta, 2026: P/E 4.3 against
        # about 7 on aggregators) gets the same caution as in the single-stock report.
        flag = sa.demerger_pe_flag(selected.rsplit(".", 1)[0], f.get("eps"), price, series.get("quarterlyDilutedEPS"), today=now.date())
        if flag:
            f["eps_flags"].append(flag)
            out["pe_demerger_note"] = flag
        if nse_fetcher and series.get("_currency") and selected.endswith(".NS"):
            nse = sa.nse_quarter_results(selected[:-3], nse_fetcher, details=False)
            sa.rupee_growth(f, series, lambda: sa.nse_annual_results(nse, nse_fetcher))
        out["demerger_notes"] = sa.demerger_statements(f, series, selected.rsplit(".", 1)[0], cut)
    except (Block, ValueError, KeyError, TypeError, OSError) as error:
        if fetch_failure(error):
            out["fundamentals_error"] = "financial statements failed to load"
            out["fundamentals_failed"] = True
        else:
            out["fundamentals_error"] = f"financial statements unusable ({error})"
        return out
    out.update({k: f.get(k) for k in ("pe", "pb", "roe", "net_margin", "operating_margin", "debt_to_equity", "revenue_growth",
                                      "eps_growth", "cash_dividend_yield", "fcf_yield", "bank_like", "payout_ratio", "fcf_cover")})
    out["eps_flags"] = f.get("eps_flags") or []
    out["revenue_note"] = f.get("revenue_note")
    out["market_cap_cr"] = f["market_cap"] / 1e7 if f.get("market_cap") else None
    # Borrowings in rupees beside debt/equity: "low debt" is judged on both (0.40 of a large equity can be a big sum).
    out["debt_cr"] = f["total_debt"] / 1e7 if f.get("total_debt") is not None else None
    out["borrowings_cr"] = f["borrowings_ex_leases"] / 1e7 if f.get("borrowings_ex_leases") is not None else None
    out["debt_date"] = f.get("debt_date")
    out["equity_cr"] = out["market_cap_cr"] / f["pb"] if out["market_cap_cr"] and f.get("pb") else None  # book value, ₹ cr
    out["eps_date"] = f["eps"]["date"] if f.get("eps") else None
    out["loss_making"] = bool(f.get("eps") and f["eps"]["value"] is not None and f["eps"]["value"] <= 0)
    out["currency_label"] = (series.get("_currency") or {}).get("label")
    out["growth_basis"] = f.get("growth_basis")
    if dividend_history:
        try:
            record = sa.dividend_record(selected, fetcher=fetcher, now=now, fund=f, nse_fetcher=dividend_nse)
            out["dividend_years"] = record["years_paid_in_a_row"]
            out["streak_note"] = record.get("streak_note")
            out["dps_cagr_5y"] = record["dps_cagr_5y"]
        except (Block, ValueError, KeyError, TypeError, IndexError, OSError):
            out["dividend_history_failed"] = True
    return out


def pe_band(bars, eps_rows, *, lag_days=60, years=5, since=None, max_age_days=None):
    """Historical P/E band using each annual EPS from `lag_days` after its year end (roughly when it is published).

    Sessions whose latest published EPS is zero or negative have no P/E and are skipped (an older positive EPS is
    never reused). Returns None when today's latest EPS is not positive. `since` (a demerger record date) drops
    annual EPS for years ending before it, because those figures include the demerged business. `max_age_days` stops
    using an EPS that many days after it became usable, so a year missing from the data leaves a gap instead of
    stretching the year before over it.
    """
    rows = sorted((date.fromisoformat(r["date"]) + timedelta(days=lag_days), r["value"]) for r in eps_rows
                  if r.get("value") is not None and (not since or r["date"] > since))
    if not rows:
        return None
    start = date.fromisoformat(bars[-1]["at"][:10]) - timedelta(days=365 * years)
    series, first = [], None
    for b in bars:
        day = date.fromisoformat(b["at"][:10])
        if day < start or (since and day.isoformat() < since):
            continue
        usable = [(avail, eps) for avail, eps in rows if avail <= day]
        if max_age_days and usable and (day - usable[-1][0]).days > max_age_days:
            continue
        usable = [eps for _, eps in usable]
        if usable and usable[-1] > 0:
            series.append(b["close"] / usable[-1])
            first = first or day.isoformat()
    today = [eps for avail, eps in rows if avail <= date.fromisoformat(bars[-1]["at"][:10])]
    if len(series) < 120 or not today or today[-1] <= 0:
        return None
    ordered = sorted(series)
    q = lambda p: ordered[min(len(ordered) - 1, int(p * len(ordered)))]
    current = series[-1]
    return {"current": current, "min": ordered[0], "p25": q(0.25), "median": statistics.median(ordered), "p75": q(0.75), "max": ordered[-1],
            "percentile": sum(v <= current for v in ordered) / len(ordered), "sessions": len(series), "years": round(len(series) / 250, 1),
            "start": first, "eps_used": today[-1],
            "basis": f"price ÷ latest annual diluted EPS, each EPS used from {lag_days} days after its year end"
                     + (f"; only financial years ending after the {since} demerger" if since else "")}


def parse_filter(text):
    m = re.fullmatch(r"\s*([a-z_0-9]+)\s*(<=|>=|<|>|=)\s*(-?[0-9.]+)\s*(%?)\s*", text.lower())
    if not m or m.group(1) not in FIELDS:
        raise Block(f"SCREENER: filter must look like 'pe<25' or 'roe>15%'; fields: {', '.join(FIELDS)}")
    field, op, value, percent = m.groups()
    value = float(value)
    if field in PERCENT and (percent or abs(value) > 1.5):
        value /= 100  # 'roe>15' and 'roe>15%' both mean 15%
    return field, op, value


def sector_matches(sector, excluded):
    """True when a stock's NSE sector is one of `excluded` (case-insensitive, '&' and 'and' alike)."""
    norm = lambda t: " ".join(str(t or "").casefold().replace("&", " and ").split())
    return bool(sector) and norm(sector) in {norm(x) for x in excluded or ()}


# ---------- snapshot cache: completed sessions only ----------

CACHE_VERSION = 1
SETTLED = (16, 30)       # IST: a session's close and the day's statements are treated as final after this
SESSION_START = (9, 0)   # IST: from here until SETTLED on a weekday, nothing is read from or written to the cache
CACHE_MAX_HOURS = 20     # a saved snapshot is never used after this long, whatever the calendar says


def last_settled(now):
    """The most recent weekday SETTLED time at or before `now` (an exchange holiday only makes the cache miss)."""
    day = now.date()
    while True:
        moment = now.replace(year=day.year, month=day.month, day=day.day, hour=SETTLED[0], minute=SETTLED[1], second=0, microsecond=0)
        if day.weekday() < 5 and moment <= now:
            return moment
        day -= timedelta(days=1)


def in_session(now):
    return now.weekday() < 5 and SESSION_START <= (now.hour, now.minute) < SETTLED


def snapshot_store():
    """Folder for saved screen snapshots (the plugin's per-user cache; None when INDIA_DAY_TRADER_CACHE=off)."""
    import stock_analysis as sa
    base = sa.filing_store()
    return Path(base).parent / "screen_snapshots" if base else None


def _cache_path(store, symbol, variant):
    safe = re.sub(r"[^A-Z0-9&_.-]", "_", symbol.upper())
    return Path(store) / f"v{CACHE_VERSION}-{variant}-{safe}.json"


def cached_snapshot(store, symbol, variant, now):
    """A snapshot saved after the last completed session, outside market hours; else None."""
    if not store or in_session(now):
        return None
    try:
        doc = json.loads(_cache_path(store, symbol, variant).read_text(encoding="utf-8"))
        saved = datetime.fromisoformat(doc["saved"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if saved < last_settled(now) or saved > now or (now - saved).total_seconds() > CACHE_MAX_HOURS * 3600:
        return None
    return doc.get("snapshot")


def save_snapshot(store, symbol, variant, now, snap):
    """Saved only outside market hours and only when everything loaded (a failure is never replayed)."""
    if not store or in_session(now) or not snap or snap.get("fundamentals_failed") or snap.get("dividend_history_failed"):
        return
    try:
        path = _cache_path(store, symbol, variant)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved": now.isoformat(), "snapshot": snap}), encoding="utf-8")
        tmp.replace(path)
    except (OSError, TypeError, ValueError):
        pass


def screen(universe, filters, *, sort=None, descending=True, limit=25, symbols=None, fetcher=request, workers=None, now=None,
           retries=None, sleep=time.sleep, columns=None, max_per_sector=None, exclude_sectors=None, cache=None):
    """Screen a universe (one name or several, merged) or a custom symbol list.

    Transient fetch failures are retried (`retries` extra attempts with a short backoff; by default only for the live
    feed). A stock is "failed" when data it needs did not load, and "missing data" when the feed answered but has no
    value for a filtered field.
    """
    import stock_analysis as sa
    if sort and sort not in FIELDS:
        raise Block(f"SCREENER: sort field must be one of {', '.join(FIELDS)}")
    bad = [c for c in columns or () if c not in FIELDS]
    if bad:
        raise Block(f"SCREENER: unknown column {', '.join(bad)}; fields: {', '.join(FIELDS)}")
    started = time.monotonic()
    parsed = [parse_filter(f) for f in filters]
    wanted = {f for f, _, _ in parsed} | {sort} | set(columns or ())
    need_dividends = bool(wanted & DIVIDEND_FIELDS)
    # Debt/equity means nothing for a bank or NBFC (deposits and borrowings are their raw material), so a debt filter
    # leaves financials out and says so rather than silently failing them.
    debt_filter = any(f == "debt_to_equity" for f, _, _ in parsed)
    live = fetcher is request
    retries = (2 if live else 0) if retries is None else retries
    if live and sa.REQUEST_RETRIES:
        fetcher = sa.live_fetcher()  # the public fetcher already retries transient errors (twice, with backoff)
    elif retries:
        fetcher = sa.retrying(fetcher, attempts=retries + 1, sleep=sleep)
    # Rupee growth for dollar reporters needs their NSE filings; fetched only when a growth column is shown.
    shown = set(display_columns({"filters": filters, "sort": sort, "columns": list(columns) if columns else None}))
    nse_fetcher = ((sa.live_nse() if live else fetcher) if shown & GROWTH_FIELDS else None)
    # A gap in Yahoo's dividend history is checked against NSE's corporate actions (one request, only when there is one).
    dividend_nse = (nse_fetcher or (sa.live_nse() if live else fetcher)) if need_dividends else None
    names = [universe] if isinstance(universe, str) else list(dict.fromkeys(universe))
    members = ([{"symbol": s.upper().replace(".NS", ""), "name": "", "industry": ""} for s in dict.fromkeys(symbols)] if symbols
               else universe_members(names, fetcher=fetcher))
    if symbols:
        # Sectors for a custom list come from NSE's Nifty 500 list (one request); stocks outside it have none.
        sectors = {m["symbol"]: m["industry"] for m in _safe_members("nifty500", fetcher) or []}
        for m in members:
            m["industry"] = sectors.get(m["symbol"], "")
    if len(members) > MAX_SYMBOLS:
        raise Block(f"SCREENER: at most {MAX_SYMBOLS} symbols per run")
    # Excluded sectors are dropped before any data is fetched (a Nifty 500 screen without Financial Services skips about
    # a fifth of the work).
    excluded = [m["symbol"] for m in members if sector_matches(m.get("industry"), exclude_sectors)]
    unknown_sectors = [x for x in exclude_sectors or () if not any(sector_matches(m.get("industry"), [x]) for m in members)]
    members = [m for m in members if m["symbol"] not in set(excluded)]
    now = now or instant()
    # Live runs keep each stock's snapshot after a completed session, so a second screen the same evening or weekend
    # (other filters, another sort) reads them from disk instead of fetching every stock again.
    store = (snapshot_store() if live else None) if cache is None else (cache or None)
    variant = f"d{int(need_dividends)}g{int(nse_fetcher is not None)}"
    cached_count = [0]
    workers = workers or (BIG_WORKERS if len(members) > 100 and live else WORKERS)

    def one(member):
        try:
            snap = cached_snapshot(store, member["symbol"], variant, now)
            if snap is not None:
                cached_count[0] += 1
            else:
                snap = snapshot(member["symbol"], fetcher=fetcher, now=now, dividend_history=need_dividends, nse_fetcher=nse_fetcher,
                                dividend_nse=dividend_nse)
                save_snapshot(store, member["symbol"], variant, now, snap)
            snap = dict(snap)
            snap["industry"] = member["industry"]
            return snap
        except sa.SymbolChanged as error:
            return {"symbol": member["symbol"] + ".NS", "error": "symbol changed",
                    "reason": f"no longer trades; became {' and '.join(s['symbol'] for s in error.successors)}"}
        except (Block, ValueError, KeyError, TypeError, OSError) as error:
            return {"symbol": member["symbol"] + ".NS", "error": "price history unavailable",
                    "reason": "price history failed to load" if fetch_failure(error) else "no usable price history"}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, members))
    failed_detail = {r["symbol"]: r["reason"] for r in results if r.get("error")}
    ok = [r for r in results if not r.get("error")]
    passed, unknown, financials = [], [], []
    for r in ok:
        if debt_filter and (r.get("bank_like") or r.get("industry") == "Financial Services"):
            financials.append(r["symbol"])
            continue
        absent = [f for f, _, _ in parsed if r.get(f) is None]
        if absent:
            if r.get("fundamentals_failed") and any(f in STATEMENT_FIELDS for f in absent):
                failed_detail[r["symbol"]] = "financial statements failed to load"
            else:
                unknown.append(r["symbol"])
            continue
        if all(OPS[op](r[f], v) for f, op, v in parsed):
            passed.append(r)
    if sort:
        # Ties broken by symbol so identical inputs always give the same order.
        passed.sort(key=lambda r: (r.get(sort) is None, -(r.get(sort) or 0) if descending else (r.get(sort) or 0), r["symbol"]))
    capped, matched = [], len(passed)
    if max_per_sector:
        kept, count = [], {}
        for r in passed:
            sector = r.get("industry") or ""
            if sector and count.get(sector, 0) >= max_per_sector:
                if len(kept) < limit:  # only those that would have made the list shown are named
                    capped.append({"symbol": r["symbol"], "sector": sector})
                continue
            count[sector] = count.get(sector, 0) + 1
            kept.append(r)
        passed = kept
    failed = [r["symbol"] for r in results if r["symbol"] in failed_detail]
    partial = [r["symbol"] for r in passed if r.get("fundamentals_failed")]
    return {"schema": "screen_v1", "universe": "custom" if symbols else "+".join(names), "as_of": now.isoformat(), "filters": filters,
            "sort": sort, "descending": descending, "max_per_sector": max_per_sector, "sector_capped": capped,
            "requested": len(members), "evaluated": len(ok), "matched": matched, "missing_data": unknown, "failed": failed,
            "failed_detail": failed_detail, "partial_financials": partial, "retries": retries,
            "renamed": [r["renamed_note"] for r in ok if r.get("renamed_note")], "excluded_financials": financials,
            "exclude_sectors": list(exclude_sectors or []), "excluded_by_sector": excluded, "unknown_exclude_sectors": unknown_sectors,
            "from_cache": cached_count[0], "elapsed_s": round(time.monotonic() - started, 1),
            "columns": list(columns) if columns else None,
            "results": passed[:limit], "truncated": len(passed) > limit,
            "note": "Ratios come from company financials as reported on Yahoo Finance and can be stale or restated; a match is not a recommendation.",
            "execution_eligible": False, "submitted": False}


def cell(field, value, limits=()):
    """One table cell. `limits` are the thresholds filtered on for this field: a value that would print the same as one
    (TCS debt/equity 0.0996 as "0.10" under a "<0.1" filter) gets more decimals, so a match never looks like a miss."""
    if value is None:
        return "n/a"
    if field in PERCENT:
        text, scale, unit = f"{value * 100:.1f}%", 100, "%"
        for digits in (2, 3):
            if any(text == f"{t * scale:.{digits - 1}f}{unit}" and value != t for t in limits):
                text = f"{value * scale:.{digits}f}{unit}"
        return text
    if field in ("market_cap_cr", "debt_cr", "borrowings_cr"):
        import stock_analysis as sa
        return sa.indian(value)  # Indian digit grouping: 2,39,114
    if field == "dividend_years":
        return f"{value:.0f}"
    if field == "fcf_cover":
        return f"{value:,.1f}×"
    text = f"{value:,.2f}"
    for digits in (3, 4):
        if any(text == f"{t:,.{digits - 1}f}" and value != t for t in limits):
            text = f"{value:,.{digits}f}"
    return text


def display_columns(r, columns=None):
    """The chosen (or default) columns plus every field the screen filtered or sorted on."""
    base = list(columns or r.get("columns") or DEFAULT_COLUMNS)
    used = [f.split("<")[0].split(">")[0].split("=")[0].strip().lower() for f in r.get("filters") or []]
    used += [r["sort"]] if r.get("sort") else []
    out = base + [f for f in dict.fromkeys(used) if f in FIELDS and f not in base]
    if "debt_cr" in out and "borrowings_cr" not in out:
        out.insert(out.index("debt_cr") + 1, "borrowings_cr")  # debt in rupees is shown with and without lease liabilities
    return tuple(out)


def filter_limits(r):
    """{field: [thresholds]} from the screen's filters, for cell precision."""
    out = {}
    for text in r.get("filters") or []:
        try:
            field, _, value = parse_filter(text)
        except Block:
            continue
        out.setdefault(field, []).append(value)
    return out


def sector_lines(r):
    """The per-sector cap and what it left out, or a concentration warning when one sector fills the list."""
    out = []
    capped = r.get("sector_capped") or []
    if r.get("max_per_sector"):
        out += ["", f"At most {r['max_per_sector']} per sector, for a diversified list"
                + (": left out " + ", ".join(f"{c['symbol'].replace('.NS', '')} ({c['sector']})" for c in capped) if capped else " (nothing left out)")
                + ". Rerun with --max-per-sector 0 to see every match."]
        return out
    shown = r.get("results") or []
    counts = {}
    for row in shown:
        if row.get("industry"):
            counts[row["industry"]] = counts.get(row["industry"], 0) + 1
    heavy = [(s, n) for s, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n >= 3 and n > len(shown) / 3]
    if heavy:
        out += ["", "⚠ Concentrated: " + "; ".join(f"{n} of {len(shown)} are {s}" for s, n in heavy)
                + ". For a diversified shortlist, rerun with --max-per-sector 2 (or pick across sectors)."]
    return out


def markdown(r, columns=None):
    columns = display_columns(r, columns)
    limits = filter_limits(r)
    with_sector = any(row.get("industry") for row in r["results"])
    dates = sorted({x.get("price_date") for x in r["results"] if x.get("price_date")})
    quoted = sum(1 for x in r["results"] if x.get("price_note"))
    failed_prices = [s for s in r["failed"] if "financial statements" not in (r.get("failed_detail") or {}).get(s, "")]
    failed_statements = [s for s in r["failed"] if s not in failed_prices]
    lines = [f"# Screen — {r['universe']}" + (f" (preset: {r['preset']})" if r.get("preset") else ""), "",
             f"Run {r['as_of'][:16].replace('T', ' ')} IST · prices as of {' to '.join([dates[0], dates[-1]] if len(dates) > 1 else dates) or 'n/a'}"
             + (f" ({quoted} from Yahoo's last-trade quote because the daily close was missing)" if quoted else "")
             + f" · filters: {', '.join(r['filters']) or 'none'}" + (f" · sorted by {FIELDS.get(r['sort'], r['sort'])}" if r.get("sort") else ""), "",
             f"Evaluated {r['evaluated']} of {r['requested']}; matched {r['matched']}" + (f" (showing {len(r['results'])})" if r["truncated"] else "")
             + f"; missing data for a filter: {len(r['missing_data'])}; failed to load: {len(r['failed'])}"
             + (f" (transient errors retried up to {r['retries']} times)" if r.get("retries") else "") + ".", "",
             "| Stock | " + ("sector | " if with_sector else "") + " | ".join(FIELDS[c] for c in columns) + " |",
             "| --- |" + (" --- |" if with_sector else "") + " ---: |" * len(columns)]
    for row in r["results"]:
        mark = " ⚠" if row.get("eps_flags") or row.get("dividend_flags") else ""
        if set(columns) & YIELD_FIELDS and ((row.get("dividend_latest") or {}).get("days_ago") or RECENT_EX_DAYS + 1) <= RECENT_EX_DAYS:
            mark += " †"
        if "dividend_years" in columns and row.get("streak_note"):
            mark += " ‡"
        lines.append(f"| {row['name'] or row['symbol']} ({row['symbol'].replace('.NS', '')}){mark} | "
                     + (f"{row.get('industry') or 'n/a'} | " if with_sector else "") + " | ".join(cell(c, row.get(c), limits.get(c, ())) for c in columns) + " |")
    if not r["results"]:
        lines.append("| No stock matched | " + ("| " if with_sector else "") + " | ".join("" for _ in columns) + " |")
    lines += sector_lines(r)
    if r.get("exclude_sectors"):
        lines += ["", f"Sectors left out: {', '.join(r['exclude_sectors'])} ({len(r.get('excluded_by_sector') or [])} stocks, not fetched)"
                  + (f"; no stock in this universe has the sector {', '.join(repr(x) for x in r['unknown_exclude_sectors'])}: check the "
                     "spelling against the sector column (NSE's industry names, e.g. 'Financial Services', 'Information Technology')"
                     if r.get("unknown_exclude_sectors") else "") + "."]
    if r.get("excluded_financials"):
        lines += ["", f"Banks/financials excluded: debt/equity not meaningful ({', '.join(s.replace('.NS', '') for s in r['excluded_financials'])})."]
    converted = [row for row in r["results"] if row.get("currency_label")]
    if converted:
        growth_shown = bool(set(columns) & GROWTH_FIELDS)
        plain = [row["symbol"].replace(".NS", "") for row in converted if not (growth_shown and row.get("growth_basis"))]
        from_nse = [row["symbol"].replace(".NS", "") for row in converted if growth_shown and row.get("growth_basis")]
        if plain:
            lines += ["", f"{', '.join(plain)}: financials reported in USD and converted to rupees at each period's average rate"
                          + (" (growth is rupee growth)." if growth_shown else ".")]
        if from_nse:
            lines += ["", f"{', '.join(from_nse)}: financials reported in USD and converted to rupees at each period's average rate; "
                          "revenue and EPS growth come from the company's rupee results filed on NSE."]
    # A demerger note only matters when the table shows a figure it withholds.
    withheld = [row for row in r["results"] if any(set(columns) & demerger_fields(n) for n in row.get("demerger_notes") or [])]
    if withheld:
        lines += [""] + [f"⚠ {row['symbol'].replace('.NS', '')}: {next(n for n in row['demerger_notes'] if set(columns) & demerger_fields(n))}"
                         for row in withheld]
    banks = [row["symbol"].replace(".NS", "") for row in r["results"] if row.get("bank_like")]
    if banks and any(f.split("<")[0].split(">")[0].split("=")[0].strip() in ("roe", "pb") for f in r["filters"]):
        lines += ["", f"⚠ Banks and finance companies ({', '.join(banks)}): ROE and P/B use Yahoo's book equity, which can differ a lot from the "
                      "net worth in the bank's own results. Check reported return on net worth before relying on these filters."]
    flagged = [row for row in r["results"] if row.get("eps_flags") or row.get("dividend_flags")]
    if flagged:
        lines += ["", "⚠ Earnings and dividend checks:"] + [f"- {row['symbol'].replace('.NS', '')}: {' '.join((row.get('eps_flags') or []) + (row.get('dividend_flags') or []))}"
                                                           for row in flagged]
    defs = [text for c, text in DEFINITIONS.items() if c in columns]
    if defs:
        lines += ["", " ".join(defs)]
    if set(columns) & YIELD_FIELDS:
        recent = [row for row in r["results"] if (row.get("dividend_latest") or {}).get("days_ago") is not None
                  and row["dividend_latest"]["days_ago"] <= RECENT_EX_DAYS]
        if recent:
            lines += ["", f"† Went ex-dividend in the last {RECENT_EX_DAYS} days, so a buyer today does not receive that payment (it still counts in the "
                          "12-month yield): " + "; ".join(f"{row['symbol'].replace('.NS', '')} ₹{row['dividend_latest']['amount']:,.2f} on "
                                                          f"{date.fromisoformat(row['dividend_latest']['date']).strftime('%d %b %Y')}" for row in recent) + "."]
    if "dividend_years" in columns:
        gaps = [row for row in r["results"] if row.get("streak_note")]
        if gaps:
            lines += ["", "‡ Years paid in a row: " + "; ".join(f"{row['symbol'].replace('.NS', '')}: {row['streak_note']}" for row in gaps) + "."]
    notes = [row["history_note"] for row in r["results"] if row.get("history_note")] + list(r.get("renamed") or [])
    if notes:
        lines += [""] + [f"⚠ {n}" for n in dict.fromkeys(notes)]
    if r["missing_data"]:
        lines += ["", "Missing data for a filter (the feed has no value; not counted as a match or a fail): "
                  + ", ".join(s.replace(".NS", "") for s in r["missing_data"][:30])]
    if failed_prices:
        lines += ["", "Failed to load prices: " + ", ".join(
            s.replace(".NS", "") + (f" ({r['failed_detail'][s]})" if (r.get("failed_detail") or {}).get(s, "").startswith(("no longer", "no usable")) else "")
            for s in failed_prices[:30])]
    if failed_statements:
        lines += ["", "Failed to load financial statements (needed for a filter): " + ", ".join(s.replace(".NS", "") for s in failed_statements[:30])]
    if r["failed"]:
        lines += ["Stocks that failed to load are not screened; rerun later or screen them with --symbols."]
    if r.get("partial_financials"):
        lines += ["", "Shown without financial statements (they failed to load): " + ", ".join(s.replace(".NS", "") for s in r["partial_financials"])]
    if r.get("elapsed_s") is not None:
        lines += ["", f"Run time {r['elapsed_s']:.0f} s" + (f"; {r['from_cache']} of {r['evaluated'] + len(r['failed'])} stocks read from snapshots saved "
                                                            "after the last completed session" if r.get("from_cache") else "") + "."]
    if r["note"] not in lines:
        lines += ["", r["note"]]
    lines.append("")
    return "\n".join(lines)


GROWTH_FIELDS = {"revenue_growth", "eps_growth"}


def demerger_fields(note):
    """Table fields a demerger note is about: annual growth always; ROE, margins and P/E too for a company in its
    first partial year after receiving a demerged business."""
    fields = set(GROWTH_FIELDS)
    if "annual ROE" in note or "P/E history" in note:
        fields |= {"roe", "net_margin", "operating_margin", "pe"}
    return fields


# Definitions printed under a screen, only for the columns it shows.
DEFINITIONS = {
    "dividend_yield": "Dividend yield (12m) = dividends per share with an ex-date in the last 12 months ÷ price, from Yahoo's chart.",
    "recurring_yield": "Ex-specials leaves out the special part of payments flagged as possible specials (only the amount above a regular payment).",
    "payout_ratio": "Payout = last financial year's cash dividends paid ÷ its net profit.",
    "fcf_cover": "FCF ÷ dividends = that year's free cash flow ÷ dividends paid (below 1× means dividends exceeded free cash flow).",
    "dividend_years": "Years paid in a row count financial years of the ex-dates (Yahoo keeps ten years, so 10 means at least 10).",
    "dps_cagr_5y": "5-year DPS growth compares two-year averages of dividends per share by financial year of the ex-date.",
    "cash_dividend_yield": "Cash dividends / mkt cap = last financial year's cash dividends paid ÷ today's market cap.",
    "debt_cr": "Debt incl. leases (₹ cr) = total debt on the same balance sheet as debt/equity, lease liabilities included (Yahoo's figure; "
               "from an NSE filing, borrowings only), so low debt can be judged both relative to equity and in rupees.",
    "borrowings_cr": "Borrowings ex leases (₹ cr) = that debt without lease liabilities (office and store leases), where the balance sheet "
                     "gives them separately; n/a where it does not.",
}
YIELD_FIELDS = {"dividend_yield", "recurring_yield", "cash_dividend_yield"}
RECENT_EX_DAYS = 30


BIG = {"nifty500", "smallcap250", "nifty200", "midcap150"}


def main(argv=None):
    p = argparse.ArgumentParser(description="Screen Indian stocks on fundamentals and momentum (research only).")
    p.add_argument("--universe", action="extend", nargs="+", choices=sorted(UNIVERSES),
                   help="One or more NSE indices, merged and de-duplicated (default nifty50); repeat or list several")
    p.add_argument("--symbols", nargs="+", help="Custom list instead of an index, e.g. RELIANCE TCS INFY")
    p.add_argument("--where", action="append", default=[], help="Filter like 'pe<25', 'roe>15', 'debt_to_equity<0.5'; repeat for AND")
    p.add_argument("--preset", choices=sorted(PRESETS), help="Ready-made screen: 'dividend' = steady payers with low debt "
                   "(ex-special yield 1.5%%+, 5+ years paid in a row, debt/equity 0.5 or less), with payout, FCF cover and DPS growth shown")
    p.add_argument("--columns", nargs="+", metavar="FIELD", help="Show these fields (no filter needed), e.g. --columns pe roe payout_ratio")
    p.add_argument("--sort", help="Field to sort by, e.g. roe")
    p.add_argument("--ascending", action="store_true")
    p.add_argument("--limit", type=int, help="Show at most this many matches (default 25)")
    p.add_argument("--max-per-sector", type=int, metavar="K", help="Keep at most K matches per sector (NSE industry); 0 = no cap. "
                   "The dividend preset uses 2 when --limit is 10 or fewer")
    p.add_argument("--exclude-sector", action="append", default=[], metavar="SECTOR",
                   help="Leave out stocks whose NSE sector (the sector column) is this, e.g. \"Financial Services\"; case-insensitive, repeatable")
    p.add_argument("--no-cache", action="store_true", help="Fetch every stock afresh instead of reusing snapshots saved after the last completed session")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--out")
    a = p.parse_args(argv)
    universes = a.universe or ["nifty50"]
    preset = PRESETS.get(a.preset) or {}
    where = list(preset.get("where", [])) + a.where
    sort = a.sort or preset.get("sort")
    columns = [c.strip().lower() for part in a.columns or [] for c in part.split(",") if c.strip()] or preset.get("columns")
    try:
        if BIG & set(universes) and not a.symbols:
            print("Screening a few hundred stocks makes about two requests per stock and takes two to four minutes the first time; "
                  "a rerun after the close reuses the saved snapshots.", file=sys.stderr)
        cap = a.max_per_sector
        if cap is None and a.limit is not None and a.limit <= SHORT_LIST:
            cap = preset.get("max_per_sector_short")
        report = screen(universes, where, sort=sort, descending=not a.ascending, limit=a.limit if a.limit is not None else 25,
                        symbols=a.symbols, columns=columns, max_per_sector=cap or None, exclude_sectors=a.exclude_sector,
                        cache=False if a.no_cache else None)
        report["preset"] = a.preset
    except Block as error:
        print(f"BLOCKED - {error}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, ensure_ascii=False) if a.format == "json" else markdown(report)
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
