"""Fundamental and technical stock screener for Indian indices, plus shared peer and valuation helpers.

Universes come from NSE's published index constituent lists; prices and company financials
come from the same free Yahoo feeds as stock_analysis.py. Research only; no orders.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, timedelta
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
    "rsi14": "RSI(14)", "price": "price",
}
PERCENT = {"roe", "net_margin", "operating_margin", "revenue_growth", "eps_growth", "dividend_yield", "cash_dividend_yield", "fcf_yield",
           "return_1y", "from_high"}
# Fields that come from the fundamentals feed; the rest come from the daily chart.
STATEMENT_FIELDS = {"pe", "pb", "roe", "net_margin", "operating_margin", "debt_to_equity", "revenue_growth", "eps_growth",
                    "cash_dividend_yield", "fcf_yield", "market_cap_cr"}
OPS = {"<=": operator.le, ">=": operator.ge, "<": operator.lt, ">": operator.gt, "=": operator.eq}
MAX_SYMBOLS = 500
WORKERS = 4                 # gentle on the public feeds
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


def peers_for(symbol, *, fetcher=request, limit=40, min_same_tier=5):
    """Peers from the smallest NSE sectoral index containing the stock; falls back to the same Nifty 500 industry.

    In the industry fallback, companies of comparable size (same Nifty 100 / Midcap 150 / smallcap tier) come first,
    then the adjacent tier if the same tier has fewer than `min_same_tier`; the rest of the industry is used only when
    the tier lists are unavailable. REIT/InvIT units and NSE placeholder entries are never peers.
    """
    base = symbol.rsplit(".", 1)[0]
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


def snapshot(symbol, *, fetcher=request, now=None):
    """Compact valuation and momentum metrics for one stock; None fields when data is missing.

    Uses two years of daily bars so the 1-year return has the same definition as stock_analysis.py (close vs the
    close 250 sessions earlier), and the same renamed-symbol and demerger handling.
    """
    import stock_analysis as sa
    now = now or instant()
    selected, history, notes = sa.prepare_history(sa.normalize(symbol), fetcher=fetcher, now=now, years=2)
    cut = notes["demerger"]
    closes = [b["close"] for b in history["bars"]]
    price = closes[-1]
    window = history["bars"][-sa.YEAR_SESSIONS:]
    out = {"symbol": selected, "name": history["name"], "price": price, "price_date": history["bars"][-1]["at"][:10],
           "price_note": history.get("price_note"), "renamed_note": notes["renamed"],
           # The cut only changes screen figures when the demerger falls inside the 1-year window.
           "history_note": cut["note"] if cut and cut["sessions"] <= sa.YEAR_SESSIONS else None,
           "return_1y": sa.one_year_return(closes),
           "from_high": price / max(b["high"] for b in window) - 1, "rsi14": sa.rsi(closes)}
    dividends = sa.trailing_dividends(history, price, since=cut["record_date"] if cut else None)
    out["dividend_yield"] = dividends["dividend_yield"] if dividends else None
    out["dividend_flags"] = dividends["flags"] if dividends else []
    try:
        f = sa.ratios(sa.fundamentals(selected, fetcher=fetcher, now=now), price, now)
    except (Block, ValueError, KeyError, TypeError, OSError) as error:
        if fetch_failure(error):
            out["fundamentals_error"] = "financial statements failed to load"
            out["fundamentals_failed"] = True
        else:
            out["fundamentals_error"] = f"financial statements unusable ({error})"
        return out
    out.update({k: f.get(k) for k in ("pe", "pb", "roe", "net_margin", "operating_margin", "debt_to_equity", "revenue_growth",
                                      "eps_growth", "cash_dividend_yield", "fcf_yield", "bank_like")})
    out["eps_flags"] = f.get("eps_flags") or []
    out["market_cap_cr"] = f["market_cap"] / 1e7 if f.get("market_cap") else None
    out["eps_date"] = f["eps"]["date"] if f.get("eps") else None
    return out


def pe_band(bars, eps_rows, *, lag_days=60, years=5, since=None):
    """Historical P/E band using each annual EPS from `lag_days` after its year end (roughly when it is published).

    Sessions whose latest published EPS is zero or negative have no P/E and are skipped (an older positive EPS is
    never reused). Returns None when today's latest EPS is not positive. `since` (a demerger record date) drops
    annual EPS for years ending before it, because those figures include the demerged business.
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
        usable = [eps for avail, eps in rows if avail <= day]
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


def screen(universe, filters, *, sort=None, descending=True, limit=25, symbols=None, fetcher=request, workers=WORKERS, now=None,
           retries=None, sleep=time.sleep):
    """Screen a universe (one name or several, merged) or a custom symbol list.

    Transient fetch failures are retried (`retries` extra attempts with a short backoff; by default only for the live
    feed). A stock is "failed" when data it needs did not load, and "missing data" when the feed answered but has no
    value for a filtered field.
    """
    import stock_analysis as sa
    if sort and sort not in FIELDS:
        raise Block(f"SCREENER: sort field must be one of {', '.join(FIELDS)}")
    parsed = [parse_filter(f) for f in filters]
    retries = (2 if fetcher is request else 0) if retries is None else retries
    if retries:
        fetcher = sa.retrying(fetcher, attempts=retries + 1, sleep=sleep)
    names = [universe] if isinstance(universe, str) else list(dict.fromkeys(universe))
    members = ([{"symbol": s.upper().replace(".NS", ""), "name": "", "industry": ""} for s in dict.fromkeys(symbols)] if symbols
               else universe_members(names, fetcher=fetcher))
    if len(members) > MAX_SYMBOLS:
        raise Block(f"SCREENER: at most {MAX_SYMBOLS} symbols per run")
    now = now or instant()

    def one(member):
        try:
            snap = snapshot(member["symbol"], fetcher=fetcher, now=now)
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
    passed, unknown = [], []
    for r in ok:
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
    failed = [r["symbol"] for r in results if r["symbol"] in failed_detail]
    partial = [r["symbol"] for r in passed if r.get("fundamentals_failed")]
    return {"schema": "screen_v1", "universe": "custom" if symbols else "+".join(names), "as_of": now.isoformat(), "filters": filters,
            "sort": sort, "descending": descending,
            "requested": len(members), "evaluated": len(ok), "matched": len(passed), "missing_data": unknown, "failed": failed,
            "failed_detail": failed_detail, "partial_financials": partial, "retries": retries,
            "renamed": [r["renamed_note"] for r in ok if r.get("renamed_note")],
            "results": passed[:limit], "truncated": len(passed) > limit,
            "note": "Ratios come from company financials as reported on Yahoo Finance and can be stale or restated; a match is not a recommendation.",
            "execution_eligible": False, "submitted": False}


def cell(field, value):
    if value is None:
        return "n/a"
    if field in PERCENT:
        return f"{value * 100:.1f}%"
    if field == "market_cap_cr":
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def display_columns(r, columns=None):
    """The default columns plus every field the screen filtered or sorted on."""
    base = list(columns or DEFAULT_COLUMNS)
    used = [f.split("<")[0].split(">")[0].split("=")[0].strip().lower() for f in r.get("filters") or []]
    used += [r["sort"]] if r.get("sort") else []
    return tuple(base + [f for f in dict.fromkeys(used) if f in FIELDS and f not in base])


def markdown(r, columns=None):
    columns = display_columns(r, columns)
    dates = sorted({x.get("price_date") for x in r["results"] if x.get("price_date")})
    quoted = sum(1 for x in r["results"] if x.get("price_note"))
    failed_prices = [s for s in r["failed"] if "financial statements" not in (r.get("failed_detail") or {}).get(s, "")]
    failed_statements = [s for s in r["failed"] if s not in failed_prices]
    lines = [f"# Screen — {r['universe']}", "",
             f"Run {r['as_of'][:16].replace('T', ' ')} IST · prices as of {' to '.join([dates[0], dates[-1]] if len(dates) > 1 else dates) or 'n/a'}"
             + (f" ({quoted} from Yahoo's last-trade quote because the daily close was missing)" if quoted else "")
             + f" · filters: {', '.join(r['filters']) or 'none'}" + (f" · sorted by {FIELDS.get(r['sort'], r['sort'])}" if r.get("sort") else ""), "",
             f"Evaluated {r['evaluated']} of {r['requested']}; matched {r['matched']}" + (f" (showing {len(r['results'])})" if r["truncated"] else "")
             + f"; missing data for a filter: {len(r['missing_data'])}; failed to load: {len(r['failed'])}"
             + (f" (transient errors retried up to {r['retries']} times)" if r.get("retries") else "") + ".", "",
             "| Stock | " + " | ".join(FIELDS[c] for c in columns) + " |", "| --- |" + " ---: |" * len(columns)]
    for row in r["results"]:
        mark = " ⚠" if row.get("eps_flags") or row.get("dividend_flags") else ""
        lines.append(f"| {row['name'] or row['symbol']} ({row['symbol'].replace('.NS', '')}){mark} | " + " | ".join(cell(c, row.get(c)) for c in columns) + " |")
    if not r["results"]:
        lines.append("| No stock matched | " + " | ".join("" for _ in columns) + " |")
    banks = [row["symbol"].replace(".NS", "") for row in r["results"] if row.get("bank_like")]
    if banks and any(f.split("<")[0].split(">")[0].split("=")[0].strip() in ("roe", "pb") for f in r["filters"]):
        lines += ["", f"⚠ Banks and finance companies ({', '.join(banks)}): ROE and P/B use Yahoo's book equity, which can differ a lot from the "
                      "net worth in the bank's own results. Check reported return on net worth before relying on these filters."]
    flagged = [row for row in r["results"] if row.get("eps_flags") or row.get("dividend_flags")]
    if flagged:
        lines += ["", "⚠ Earnings and dividend checks:"] + [f"- {row['symbol'].replace('.NS', '')}: {' '.join((row.get('eps_flags') or []) + (row.get('dividend_flags') or []))}"
                                                           for row in flagged]
    if "dividend_yield" in columns:
        lines += ["", "Dividend yield (12m) = dividends per share with an ex-date in the last 12 months ÷ price, from Yahoo's chart; "
                      "cash dividends / mkt cap is the last financial year's cash dividends paid ÷ today's market cap."]
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
    lines += ["", r["note"], ""]
    return "\n".join(lines)


BIG = {"nifty500", "smallcap250", "nifty200", "midcap150"}


def main(argv=None):
    p = argparse.ArgumentParser(description="Screen Indian stocks on fundamentals and momentum (research only).")
    p.add_argument("--universe", action="extend", nargs="+", choices=sorted(UNIVERSES),
                   help="One or more NSE indices, merged and de-duplicated (default nifty50); repeat or list several")
    p.add_argument("--symbols", nargs="+", help="Custom list instead of an index, e.g. RELIANCE TCS INFY")
    p.add_argument("--where", action="append", default=[], help="Filter like 'pe<25', 'roe>15', 'debt_to_equity<0.5'; repeat for AND")
    p.add_argument("--sort", help="Field to sort by, e.g. roe")
    p.add_argument("--ascending", action="store_true")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--out")
    a = p.parse_args(argv)
    universes = a.universe or ["nifty50"]
    try:
        if BIG & set(universes) and not a.symbols:
            print("Screening a few hundred stocks makes about two requests per stock and takes a few minutes.", file=sys.stderr)
        report = screen(universes, a.where, sort=a.sort, descending=not a.ascending, limit=a.limit, symbols=a.symbols)
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
