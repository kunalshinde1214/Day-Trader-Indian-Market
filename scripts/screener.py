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

from core import Block, instant
from public_market import request

INDEX_URL = "https://nsearchives.nseindia.com/content/indices/ind_{}list.csv"
UNIVERSES = {
    "nifty50": "nifty50", "niftynext50": "niftynext50", "nifty500": "nifty500", "midcap150": "niftymidcap150",
    "smallcap250": "niftysmallcap250", "bank": "niftybank", "psubank": "niftypsubank", "it": "niftyit", "pharma": "niftypharma",
    "healthcare": "niftyhealthcare", "auto": "niftyauto", "fmcg": "niftyfmcg", "metal": "niftymetal", "realty": "niftyrealty",
    "energy": "niftyenergy", "oilgas": "niftyoilgas", "media": "niftymedia", "consumerdurables": "niftyconsumerdurables",
}
SECTORS = ("bank", "psubank", "it", "pharma", "healthcare", "auto", "fmcg", "metal", "realty", "oilgas", "media", "consumerdurables", "energy")
FIELDS = {
    "pe": "P/E", "pb": "P/B", "roe": "ROE", "net_margin": "net margin", "operating_margin": "operating margin",
    "debt_to_equity": "debt/equity", "revenue_growth": "revenue growth", "eps_growth": "EPS growth", "dividend_yield": "dividend yield",
    "fcf_yield": "FCF yield", "market_cap_cr": "market cap (₹ cr)", "return_1y": "1-year return", "from_high": "distance from 52-week high",
    "rsi14": "RSI(14)", "price": "price",
}
PERCENT = {"roe", "net_margin", "operating_margin", "revenue_growth", "eps_growth", "dividend_yield", "fcf_yield", "return_1y", "from_high"}
OPS = {"<=": operator.le, ">=": operator.ge, "<": operator.lt, ">": operator.gt, "=": operator.eq}
MAX_SYMBOLS = 500


def index_members(name, *, fetcher=request):
    if name not in UNIVERSES:
        raise Block(f"SCREENER: unknown universe {name}; choose from {', '.join(sorted(UNIVERSES))}")
    raw = fetcher(INDEX_URL.format(UNIVERSES[name]))
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("Symbol")]
    if not rows:
        raise Block(f"SCREENER: {name} constituent list was empty or changed format")
    return [{"symbol": r["Symbol"].strip(), "name": (r.get("Company Name") or "").strip(), "industry": (r.get("Industry") or "").strip()} for r in rows]


def peers_for(symbol, *, fetcher=request, limit=40):
    """Peers from the smallest NSE sectoral index containing the stock; falls back to the same Nifty 500 industry."""
    base = symbol.rsplit(".", 1)[0]
    groups = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, members in zip(SECTORS, pool.map(lambda n: _safe_members(n, fetcher), SECTORS)):
            if members and any(m["symbol"] == base for m in members):
                groups[name] = members
    if groups:
        name, members = min(groups.items(), key=lambda kv: len(kv[1]))
        basis = f"Nifty {name} index constituents"
    else:
        everyone = _safe_members("nifty500", fetcher) or []
        own = next((m for m in everyone if m["symbol"] == base), None)
        if not own or not own["industry"]:
            return {"basis": "no sectoral index or Nifty 500 industry found", "peers": []}
        members = [m for m in everyone if m["industry"] == own["industry"]]
        basis = f"Nifty 500 industry: {own['industry']}"
    peers = [m for m in members if m["symbol"] != base]
    return {"basis": basis, "group_size": len(members), "peers": peers[:limit], "capped": len(peers) > limit}


def _safe_members(name, fetcher):
    try:
        return index_members(name, fetcher=fetcher)
    except (Block, ValueError, KeyError, OSError):
        return None


def snapshot(symbol, *, fetcher=request, now=None):
    """Compact valuation and momentum metrics for one stock; None fields when data is missing."""
    import stock_analysis as sa
    now = now or instant()
    selected = sa.normalize(symbol)
    history = sa.daily_history(selected, fetcher=fetcher, now=now, years=1)
    closes = [b["close"] for b in history["bars"]]
    price = closes[-1]
    window = history["bars"][-250:]
    out = {"symbol": selected, "name": history["name"], "price": price, "price_date": history["bars"][-1]["at"][:10],
           "price_note": history.get("price_note"),
           "return_1y": sa.ret(closes, 250) if len(closes) > 250 else (closes[-1] / closes[0] - 1 if len(closes) > 200 else None),
           "from_high": price / max(b["high"] for b in window) - 1, "rsi14": sa.rsi(closes)}
    try:
        f = sa.ratios(sa.fundamentals(selected, fetcher=fetcher, now=now), price, now)
        out.update({k: f.get(k) for k in ("pe", "pb", "roe", "net_margin", "operating_margin", "debt_to_equity", "revenue_growth",
                                          "eps_growth", "dividend_yield", "fcf_yield", "bank_like")})
        out["eps_flags"] = f.get("eps_flags") or []
        out["market_cap_cr"] = f["market_cap"] / 1e7 if f.get("market_cap") else None
        out["eps_date"] = f["eps"]["date"] if f.get("eps") else None
    except (Block, ValueError, KeyError, TypeError):
        out["fundamentals_error"] = "financial statements unavailable"
    return out


def pe_band(bars, eps_rows, *, lag_days=60, years=5):
    """Historical P/E band using each annual EPS from `lag_days` after its year end (roughly when it is published)."""
    rows = sorted((date.fromisoformat(r["date"]) + timedelta(days=lag_days), r["value"]) for r in eps_rows if r["value"] and r["value"] > 0)
    if not rows:
        return None
    start = bars[-1]["at"][:10]
    start = date.fromisoformat(start) - timedelta(days=365 * years)
    series = []
    for b in bars:
        day = date.fromisoformat(b["at"][:10])
        if day < start:
            continue
        usable = [eps for avail, eps in rows if avail <= day]
        if usable:
            series.append(b["close"] / usable[-1])
    if len(series) < 120:
        return None
    ordered = sorted(series)
    q = lambda p: ordered[min(len(ordered) - 1, int(p * len(ordered)))]
    current = series[-1]
    return {"current": current, "min": ordered[0], "p25": q(0.25), "median": statistics.median(ordered), "p75": q(0.75), "max": ordered[-1],
            "percentile": sum(v <= current for v in ordered) / len(ordered), "sessions": len(series), "years": round(len(series) / 250, 1),
            "basis": f"price ÷ latest annual diluted EPS, each EPS used from {lag_days} days after its year end"}


def parse_filter(text):
    m = re.fullmatch(r"\s*([a-z_0-9]+)\s*(<=|>=|<|>|=)\s*(-?[0-9.]+)\s*(%?)\s*", text.lower())
    if not m or m.group(1) not in FIELDS:
        raise Block(f"SCREENER: filter must look like 'pe<25' or 'roe>15%'; fields: {', '.join(FIELDS)}")
    field, op, value, percent = m.groups()
    value = float(value)
    if field in PERCENT and (percent or abs(value) > 1.5):
        value /= 100  # 'roe>15' and 'roe>15%' both mean 15%
    return field, op, value


def screen(universe, filters, *, sort=None, descending=True, limit=25, symbols=None, fetcher=request, workers=6, now=None):
    members = [{"symbol": s.upper().replace(".NS", ""), "name": "", "industry": ""} for s in symbols] if symbols else index_members(universe, fetcher=fetcher)
    if len(members) > MAX_SYMBOLS:
        raise Block(f"SCREENER: at most {MAX_SYMBOLS} symbols per run")
    parsed = [parse_filter(f) for f in filters]
    now = now or instant()

    def one(member):
        try:
            snap = snapshot(member["symbol"], fetcher=fetcher, now=now)
            snap["industry"] = member["industry"]
            return snap
        except (Block, ValueError, KeyError, TypeError, OSError):
            return {"symbol": member["symbol"] + ".NS", "error": "price history unavailable"}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, members))
    failed = [r["symbol"] for r in results if r.get("error")]
    ok = [r for r in results if not r.get("error")]
    passed, unknown = [], []
    for r in ok:
        if any(r.get(f) is None for f, _, _ in parsed):
            unknown.append(r["symbol"])
            continue
        if all(OPS[op](r[f], v) for f, op, v in parsed):
            passed.append(r)
    if sort:
        if sort not in FIELDS:
            raise Block(f"SCREENER: sort field must be one of {', '.join(FIELDS)}")
        passed.sort(key=lambda r: (r.get(sort) is None, -(r.get(sort) or 0) if descending else (r.get(sort) or 0)))
    return {"schema": "screen_v1", "universe": "custom" if symbols else universe, "as_of": now.isoformat(), "filters": filters,
            "requested": len(members), "evaluated": len(ok), "matched": len(passed), "missing_data": unknown, "failed": failed,
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
    return f"{value:,.2f}" if field in ("pe", "pb", "debt_to_equity", "rsi14") else f"{value:,.2f}"


def markdown(r, columns=("price", "market_cap_cr", "pe", "pb", "roe", "net_margin", "debt_to_equity", "revenue_growth", "return_1y")):
    dates = sorted({x.get("price_date") for x in r["results"] if x.get("price_date")})
    quoted = sum(1 for x in r["results"] if x.get("price_note"))
    lines = [f"# Screen — {r['universe']}", "",
             f"Run {r['as_of'][:16].replace('T', ' ')} IST · prices as of {' to '.join([dates[0], dates[-1]] if len(dates) > 1 else dates) or 'n/a'}"
             + (f" ({quoted} from Yahoo's last-trade quote because the daily close was missing)" if quoted else "")
             + f" · filters: {', '.join(r['filters']) or 'none'}", "",
             f"Evaluated {r['evaluated']} of {r['requested']}; matched {r['matched']}" + (f" (showing {len(r['results'])})" if r["truncated"] else "")
             + f"; missing data for a filter: {len(r['missing_data'])}; failed to load: {len(r['failed'])}.", "",
             "| Stock | " + " | ".join(FIELDS[c] for c in columns) + " |", "| --- |" + " ---: |" * len(columns)]
    for row in r["results"]:
        mark = " ⚠" if row.get("eps_flags") else ""
        lines.append(f"| {row['name'] or row['symbol']} ({row['symbol'].replace('.NS', '')}){mark} | " + " | ".join(cell(c, row.get(c)) for c in columns) + " |")
    if not r["results"]:
        lines.append("| No stock matched | " + " | ".join("" for _ in columns) + " |")
    banks = [row["symbol"].replace(".NS", "") for row in r["results"] if row.get("bank_like")]
    if banks and any(f.split("<")[0].split(">")[0].split("=")[0].strip() in ("roe", "pb") for f in r["filters"]):
        lines += ["", f"⚠ Banks and finance companies ({', '.join(banks)}): ROE and P/B use Yahoo's book equity, which can differ a lot from the "
                      "net worth in the bank's own results. Check reported return on net worth before relying on these filters."]
    flagged = [row for row in r["results"] if row.get("eps_flags")]
    if flagged:
        lines += ["", "⚠ Earnings checks:"] + [f"- {row['symbol'].replace('.NS', '')}: {' '.join(row['eps_flags'])}" for row in flagged]
    if r["missing_data"]:
        lines += ["", "Missing data for a filter (not counted as a match or a fail): " + ", ".join(s.replace(".NS", "") for s in r["missing_data"][:30])]
    if r["failed"]:
        lines += ["", "Failed to load: " + ", ".join(s.replace(".NS", "") for s in r["failed"][:30])]
    lines += ["", r["note"], ""]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="Screen Indian stocks on fundamentals and momentum (research only).")
    p.add_argument("--universe", default="nifty50", choices=sorted(UNIVERSES))
    p.add_argument("--symbols", nargs="+", help="Custom list instead of an index, e.g. RELIANCE TCS INFY")
    p.add_argument("--where", action="append", default=[], help="Filter like 'pe<25', 'roe>15', 'debt_to_equity<0.5'; repeat for AND")
    p.add_argument("--sort", help="Field to sort by, e.g. roe")
    p.add_argument("--ascending", action="store_true")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--out")
    a = p.parse_args(argv)
    try:
        if a.universe == "nifty500" and not a.symbols:
            print("Screening 500 stocks makes about 1,000 requests and takes a few minutes.", file=sys.stderr)
        report = screen(a.universe, a.where, sort=a.sort, descending=not a.ascending, limit=a.limit, symbols=a.symbols)
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
