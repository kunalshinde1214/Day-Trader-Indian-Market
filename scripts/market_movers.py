"""Why did the Indian market move? Direction first, then sectors, Nifty 50 point contributors, gainers/losers,
breadth, India VIX and FII/DII. Free public data (NSE + Yahoo); research only, no prediction or advice.

Live mode (default while NSE shows today's snapshot): NSE allIndices for index/sector/VIX levels and Nifty 50
advance/decline, Yahoo meta.regularMarketPrice for each Nifty 50 constituent, NSE bhavcopy for their official
previous closes. Session mode (``--session YYYY-MM-DD``): NSE's dated index-close file and bhavcopy for that day.

Index-point contributions = estimated weight x stock % change x previous index close. NSE no longer serves
constituent free-float market caps to scripts, so weights are ESTIMATED, anchored to official data: the NSE Indices
monthly factsheet's top-10 constituent weights and sector weights (assets/index_weights.json, dated and sourced),
rolled forward to the previous close by relative price change; within each sector the remaining weight is split by a
non-negative least-squares fit of Nifty 50 five-minute returns on its constituents' returns over the last month
(Yahoo 5-minute charts). Without a usable factsheet file the pure fit is used. The report prints how much of the
actual index move the estimate explains.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, wait
import csv
from datetime import date, datetime, timedelta
import http.client
import io
import json
from pathlib import Path
import re
import sys
import urllib.parse

from core import Block, IST, instant, positive

SECTORS = ("NIFTY AUTO", "NIFTY BANK", "NIFTY FINANCIAL SERVICES", "NIFTY FMCG", "NIFTY IT", "NIFTY MEDIA", "NIFTY METAL",
           "NIFTY PHARMA", "NIFTY PSU BANK", "NIFTY PRIVATE BANK", "NIFTY REALTY", "NIFTY HEALTHCARE INDEX",
           "NIFTY CONSUMER DURABLES", "NIFTY OIL & GAS", "NIFTY ENERGY", "NIFTY CEMENT", "NIFTY CHEMICALS")
INDEX = "NIFTY 50"
BANK = "NIFTY BANK"
SENSEX = "SENSEX"
CHART = {INDEX: "^NSEI", BANK: "^NSEBANK", SENSEX: "^BSESN"}
SENSEX_SIZE = 30
SENSEX_METHOD = ("Sensex weights estimated, not published: BSE does not serve its constituent list or weights to scripts, so Sensex "
                 "5-minute returns (Yahoo ^BSESN, about one month) are fitted on the Nifty 50 stocks' 5-minute returns by "
                 "non-negative least squares; the 30 stocks with the largest fitted weights are taken as the Sensex members and "
                 "refitted alone. A Sensex member outside Nifty 50 would be missed; the check against Sensex's own move shows "
                 "how much the estimate explains.")
ERRORS = (Block, KeyError, TypeError, ValueError, OSError, ArithmeticError, IndexError, csv.Error, http.client.HTTPException)
METHOD = ("Weights estimated by non-negative least squares of Nifty 50 5-minute returns on constituent 5-minute returns "
          "over about one month of Yahoo charts, rolled forward to the previous close; NSE does not serve free-float "
          "market caps to scripts. Typical weight error about 0.5 percentage point; the gap between estimated and actual "
          "index change is shown.")
ANCHOR_METHOD = ("Weights estimated, anchored to official data: the top-10 constituent weights and {sectors} from the NSE Indices "
                 "{factsheet} factsheet of {when} ({source}), rolled to {prev} by each stock's price change relative to the index; "
                 "inside each sector the remaining weight is split among the other stocks by a non-negative least-squares fit of "
                 "about one month of Yahoo 5-minute returns. Not NSE's live weights (NSE does not serve them to scripts); the gap "
                 "between estimated and actual index change is shown.")
SENSEX_ANCHOR = ("Sensex weights: the factsheet-anchored Nifty 50 weights of the 30 identified Sensex members, rescaled to 100% "
                 "(both indices weight by free-float market cap); estimated, not BSE's published weights.")
NEWS_RE = re.compile(r"\b(sensex|nifty|dalal street|d-street|fiis?|fpis?|rupee|crude|fed|rbi|inflation|gdp|sell-?off|rally|"
                     r"stock market|markets? (?:today|fall|falls|rise|rises|decline|gain|gains|close|closes|open|opens|slip|slips|tumble|crash))\b", re.I)
NEWS_NOISE_RE = re.compile(r"among \d+ stocks|share price live|stocks? to (?:buy|watch)|price live updates|technical", re.I)


def label(name):
    """'NIFTY OIL & GAS' -> 'Nifty Oil & Gas'; keeps IT/FMCG/PSU in capitals."""
    keep = {"IT", "FMCG", "PSU", "VIX", "&", "50"}
    return " ".join(w if w in keep else w.capitalize() for w in name.split()).replace("Healthcare Index", "Healthcare")


def word(change):
    return "up" if change > 0 else "down" if change < 0 else "flat"


# ---------------------------------------------------------------- inputs

def constituents(*, fetcher, index=INDEX):
    import nse_live
    url, low, high = {INDEX: (nse_live.NIFTY50_LIST, 45, 55), BANK: (nse_live.NIFTYBANK_LIST, 8, 20)}[index]
    raw = nse_live.fetch_retry(lambda u: nse_live.cached(fetcher, u), url, delays=nse_live.retry_delays(fetcher))  # a refusal is not cached
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw), skipinitialspace=True))
    members = [{"symbol": r["Symbol"].strip(), "name": r["Company Name"].strip(), "industry": r.get("Industry", "").strip()}
               for r in rows if r.get("Series", "EQ").strip() == "EQ" and r.get("Symbol")]
    if not low <= len(members) <= high or len({m["symbol"] for m in members}) != len(members):
        raise Block("MOVERS: unexpected " + label(index) + " constituent list")
    return members


def bhavcopy(day, *, fetcher):
    """One day's NSE cash bhavcopy (official close and previous close per stock): retried with backoff when NSE refuses
    the download, and kept on disk once the day is over (see nse_live.cached_bhavcopy)."""
    import closing_context, nse_live
    raw = nse_live.cached_bhavcopy(day, fetcher=fetcher)  # a patched or injected fetcher is used as given, never the network
    return closing_context.cash_prices(raw, day, instant())


CHART_TIMEOUT = 8     # seconds per Yahoo chart request
CHART_RETRY_BUDGET = 12  # seconds per chart including one quick retry (the default request budget allowed ~41 s)
CHART_BUDGET = 20.0   # seconds for all constituent charts; slower ones count as missing (was 30: runs took 16-43 s)


CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range={}&interval=5m&includePrePost=false"


def _bar_cache_file(folder, sym):
    return Path(folder) / "yahoo_5m" / (re.sub(r"[^A-Za-z0-9._-]", "_", sym) + ".json")


def _cached_bars(folder, sym, today):
    """Completed past days of 5-minute closes saved earlier TODAY: {"through": last day, "t": [...], "c": [...]}, else None.
    Past sessions never change, so later runs the same day fetch only today's bars."""
    try:
        data = json.loads(_bar_cache_file(folder, sym).read_text(encoding="utf-8"))
        if data.get("fetched_on") != today.isoformat() or data.get("symbol") != sym: return None
        t, c = [int(x) for x in data["t"]], [float(x) for x in data["c"]]
        if len(t) < 100 or len(c) != len(t) or data["through"] >= today.isoformat(): return None
        return {"through": data["through"], "t": t, "c": c}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_bars(folder, sym, chart, today):
    """Save the completed past days (never today's bars, which are still forming) of a one-month chart."""
    try:
        q = chart["indicators"]["quote"][0]["close"]
        rows = [(int(t), float(c)) for t, c in zip(chart["timestamp"], q)
                if c is not None and datetime.fromtimestamp(int(t), IST).date() < today]
        if len(rows) < 100: return
        path = _bar_cache_file(folder, sym); path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp%d" % id(rows))
        tmp.write_text(json.dumps({"symbol": sym, "fetched_on": today.isoformat(),
                                   "through": datetime.fromtimestamp(rows[-1][0], IST).date().isoformat(),
                                   "t": [r[0] for r in rows], "c": [r[1] for r in rows]}), encoding="utf-8")
        import os
        os.replace(tmp, path)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        pass  # a read-only or full disk only costs the full download next run


FINAL_AFTER = (16, 0)  # IST: by then Yahoo's last price is the day's close, so today's chart is final and can be kept


def _final_file(folder, sym):
    return Path(folder) / "yahoo_5m" / (re.sub(r"[^A-Za-z0-9._-]", "_", sym) + ".final.json")


def _session_over(now):
    return now is not None and (now.hour, now.minute) >= FINAL_AFTER


def _cached_final(folder, sym, today):
    """Today's whole chart saved after the session ended (meta included), else None: after the close it cannot change,
    so repeat post-close runs need no download at all."""
    try:
        data = json.loads(_final_file(folder, sym).read_text(encoding="utf-8"))
        if data.get("day") != today.isoformat() or data.get("symbol") != sym: return None
        chart = data["chart"]
        if chart["meta"]["symbol"] != sym or not chart["timestamp"]: return None
        return chart
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_final(folder, sym, chart, today):
    """Keep today's chart once the session is over (after FINAL_AFTER) and Yahoo's last trade is from today; a stock
    with no trade today (suspended, or a stale feed) is never kept."""
    try:
        at = datetime.fromtimestamp(int(chart["meta"]["regularMarketTime"]), IST)
        if at.date() != today: return
        path = _final_file(folder, sym); path.parent.mkdir(parents=True, exist_ok=True)
        keep = {"meta": chart["meta"], "timestamp": chart["timestamp"], "indicators": {"quote": [{"close": chart["indicators"]["quote"][0]["close"]}]}}
        tmp = path.with_suffix(".tmp%d" % id(keep))
        tmp.write_text(json.dumps({"symbol": sym, "day": today.isoformat(), "chart": keep}), encoding="utf-8")
        import os
        os.replace(tmp, path)
    except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError):
        pass


def yahoo_charts(symbols, *, fetcher=None, workers=4, budget=None, cache=None, today=None, now=None):
    """Five-minute charts (about one month) for each symbol; per-symbol failures (including a connection cut short,
    http.client.IncompleteRead, or a chart still loading when the time budget runs out) are None, never raised.

    ``cache``: a folder for completed past days of bars (default: the user cache folder for the live Yahoo fetch, none
    for an injected fetcher). The first run of a day downloads the month and saves the finished days; later runs that
    day download only today's bars (range=1d) and join them on, which keeps run times short and steady. After 16:00 IST
    (``now``) today's chart is final: it is kept whole, and later runs that day read it without any download."""
    from public_market import request
    import nse_live
    live = fetcher is None
    get = fetcher or (lambda url: request(url, timeout=CHART_TIMEOUT, budget=CHART_RETRY_BUDGET))
    folder = (nse_live.cache_dir() if live else None) if cache is None else (cache or None)
    if now is None and live: now = datetime.now(IST)
    today = today or (now.date() if now else datetime.now(IST).date())
    final = _session_over(now) and now.date() == today  # after the close: today's charts are final and kept

    # Today's bars on top of cached days: one attempt, no retry - a failure falls back to the full month at once.
    get_day = fetcher or (lambda url: request(url, timeout=CHART_TIMEOUT, budget=CHART_TIMEOUT))

    def load(sym, span):
        data = json.loads((get_day if span == "1d" else get)(CHART_URL.format(urllib.parse.quote(sym, safe=""), span)))["chart"]["result"][0]
        if data["meta"]["symbol"] != sym: raise Block("YAHOO: wrong symbol")
        return data

    def one(sym):
        try:
            if folder and final:
                kept = _cached_final(folder, sym, today)
                if kept: return kept
            past = _cached_bars(folder, sym, today) if folder else None
            if past:
                try:
                    day = load(sym, "1d")
                    t = [int(x) for x in day["timestamp"]]; c = day["indicators"]["quote"][0]["close"]
                    keep = [(a, b) for a, b in zip(t, c) if datetime.fromtimestamp(a, IST).date().isoformat() > past["through"]]
                    joined = {"meta": day["meta"], "timestamp": past["t"] + [a for a, _ in keep],
                              "indicators": {"quote": [{"close": past["c"] + [b for _, b in keep]}]}, "cached_through": past["through"]}
                    if final: _save_final(folder, sym, joined, today)
                    return joined
                except Exception:  # noqa: BLE001 - fall back to the full month
                    pass
            data = load(sym, "1mo")
            if folder:
                _save_bars(folder, sym, data, today)
                if final: _save_final(folder, sym, data, today)
            return data
        except Exception:  # noqa: BLE001 - one bad chart must never sink the report; the symbol is simply missing
            return None
    out = {s: None for s in symbols}
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, 6)))
    try:
        futures = {pool.submit(one, s): s for s in symbols}
        done, _ = wait(futures, timeout=CHART_BUDGET if budget is None else budget)
        for f in done:
            out[futures[f]] = f.result()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def series(chart):
    stamps, closes = chart["timestamp"], chart["indicators"]["quote"][0]["close"]
    return {int(t): float(c) for t, c in zip(stamps, closes) if c is not None and int(t) % 300 == 0 and c > 0}


# ---------------------------------------------------------------- weight estimate

def nnls(A, b, sweeps=4000, tol=1e-11):
    """Non-negative least squares on normal equations (A = X'X, b = X'y) by cyclic coordinate descent."""
    n = len(b); w = [0.0] * n; g = [-v for v in b]
    for _ in range(sweeps):
        biggest = 0.0
        for i in range(n):
            if A[i][i] <= 0: continue
            new = max(0.0, w[i] - g[i] / A[i][i]); delta = new - w[i]
            if delta:
                w[i] = new; col = A[i]
                for j in range(n): g[j] += delta * col[j]
                biggest = max(biggest, abs(delta))
        if biggest < tol: break
    return w


def _returns(charts, index_symbol, minimum=0.9):
    """Aligned five-minute returns: (index series, member series, common stamps, X rows, y, symbols)."""
    index = series(charts[index_symbol])
    members = {s: series(c) for s, c in charts.items() if s != index_symbol and c is not None}
    if len(members) < minimum * (len(charts) - 1): raise Block("MOVERS: too few constituent charts for a weight estimate")
    common = sorted(t for t in index if all(t in m for m in members.values()))
    pairs = [(a, b) for a, b in zip(common, common[1:]) if b - a == 300]
    if len(pairs) < 200: raise Block("MOVERS: not enough overlapping 5-minute bars")
    syms = sorted(members)
    X = [[members[s][b] / members[s][a] - 1 for s in syms] for a, b in pairs]
    y = [index[b] / index[a] - 1 for a, b in pairs]
    return index, members, common, X, y, syms


def _nnls_fit(X, y, columns):
    """Non-negative least-squares weights for the given column positions of X."""
    n = len(columns)
    A = [[0.0] * n for _ in range(n)]; bv = [0.0] * n
    for row, target in zip(X, y):
        sub = [row[c] for c in columns]
        for i in range(n):
            ri = sub[i]
            if ri == 0: continue
            bv[i] += ri * target; Ai = A[i]
            for j in range(i, n): Ai[j] += ri * sub[j]
    for i in range(n):
        for j in range(i): A[i][j] = A[j][i]
    return nnls(A, bv)


def _r_squared(X, y, w):
    fitted = [sum(wi * xi for wi, xi in zip(w, row)) for row in X]
    mean = sum(y) / len(y); sst = sum((v - mean) ** 2 for v in y); sse = sum((v - f) ** 2 for v, f in zip(y, fitted))
    return round(1 - sse / sst, 4) if sst else None


def _window(common):
    return [datetime.fromtimestamp(common[0], IST).date().isoformat(), datetime.fromtimestamp(common[-1], IST).date().isoformat()]


def estimate_weights(charts, index_symbol, previous_closes, index_previous):
    """{symbol: weight at the previous close} plus fit quality. Needs most constituents' charts."""
    index, members, common, X, y, syms = _returns(charts, index_symbol)
    w = _nnls_fit(X, y, list(range(len(syms))))
    # Roll each weight from the middle of the window to the previous close: w_i grows with its price relative to the index.
    mid = common[len(common) // 2]
    rolled = {}
    for s, wi in zip(syms, w):
        prev = previous_closes.get(s)
        rolled[s] = wi * (prev / members[s][mid]) / (index_previous / index[mid]) if prev else wi
    total = sum(rolled.values())
    if total <= 0: raise Block("MOVERS: weight estimate failed")
    return {"weights": {s: v / total for s, v in rolled.items()}, "raw_sum": round(sum(w), 4), "observations": len(X),
            "r_squared": _r_squared(X, y, w), "window": _window(common)}


# ---------------------------------------------------------------- official anchors (NSE Indices factsheet)

WEIGHTS_FILE = Path(__file__).resolve().parents[1] / "assets" / "index_weights.json"
FACTSHEET = {INDEX: "Nifty 50", BANK: "Nifty Bank"}  # the factsheet each index's anchor comes from
PRIOR_MAX_AGE_DAYS = 100
DRIFT_BAND = (0.6, 1.6)   # price change relative to the index since the factsheet date; outside it (a split or bonus) the anchor is dropped


def _sector_key(name):
    """'Oil, Gas & Consumable Fuels' and 'Oil Gas & Consumable Fuels' are the same sector."""
    return " ".join(re.sub(r"[^a-z&]+", " ", str(name).lower()).split())


def load_anchor(index=INDEX, *, today=None, path=None):
    """The factsheet weights for ``index`` from assets/index_weights.json, or None when missing, malformed or more than
    PRIOR_MAX_AGE_DAYS old (the weights then fall back to the pure 5-minute fit)."""
    try:
        data = json.loads(Path(path or WEIGHTS_FILE).read_text(encoding="utf-8"))[index]
        as_of = date.fromisoformat(data["as_of"])
        top = {str(k): float(v) for k, v in data["top_constituents_pct"].items()}
        sectors = {_sector_key(k): float(v) for k, v in data["sectors_pct"].items()}
        closes = {str(k): float(v) for k, v in data["reference_closes"].items()}
        index_close = float(data["index_close"]); members = {str(m) for m in data["members"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if (not top or not 0 < sum(top.values()) < 100 or min(top.values()) <= 0 or abs(sum(sectors.values()) - 100) > 0.5
            or min(sectors.values(), default=0) <= 0 or index_close <= 0 or min(closes.values(), default=0) <= 0):
        return None
    if today is not None and not 0 <= (today - as_of).days <= PRIOR_MAX_AGE_DAYS:
        return None
    return {"as_of": as_of.isoformat(), "source": str(data.get("source", "NSE Indices factsheet")), "top": top, "sectors": sectors,
            "closes": closes, "index_close": index_close, "members": members, "index": index}


def anchored_weights(charts, index_symbol, previous_closes, index_previous, anchor, industries):
    """Weights anchored to official data: the factsheet's top-constituent weights (fixed) and sector weights, each rolled
    from the factsheet date to the previous close by price change relative to the index. Only the split of each sector's
    remaining weight among its other stocks is fitted (non-negative least squares on the index move left after the fixed
    stocks). ``industries``: {symbol: NSE industry} for the current constituent list."""
    index, members, common, X, y, syms = _returns(charts, index_symbol)
    ref_index = anchor["index_close"]

    def drift(s):
        sym, prev = s.removesuffix(".NS"), previous_closes.get(s)
        ref = anchor["closes"].get(sym)
        if not prev or not ref: return None
        g = (prev / ref) / (index_previous / ref_index)
        return g if DRIFT_BAND[0] <= g <= DRIFT_BAND[1] else None
    g = {s: drift(s) for s in syms}
    fixed = {s: anchor["top"][s.removesuffix(".NS")] / 100 * g[s] for s in syms if s.removesuffix(".NS") in anchor["top"] and g[s]}
    mid = common[len(common) // 2]

    def to_mid(s, w):  # a previous-close weight expressed at the middle of the fit window
        return w * (members[s][mid] / previous_closes[s]) / (index[mid] / index_previous)
    pos = {s: i for i, s in enumerate(syms)}
    fixed_mid = {pos[s]: to_mid(s, w) for s, w in fixed.items()}
    y_res = [t - sum(w * row[i] for i, w in fixed_mid.items()) for row, t in zip(X, y)]
    rest = [s for s in syms if s not in fixed]
    w_rest = _nnls_fit(X, y_res, [pos[s] for s in rest]) if rest else []
    fitted = {}
    for s, wi in zip(rest, w_rest):
        prev = previous_closes.get(s)
        fitted[s] = wi * (prev / members[s][mid]) / (index_previous / index[mid]) if prev else wi
    # Sector budgets: what the factsheet leaves for each sector's non-top stocks, split by the fitted proportions.
    same_members = set(industries) == anchor["members"]
    weights, anchored_sectors = dict(fixed), []
    if same_members:
        by_sector = {}
        for sym, ind in industries.items(): by_sector.setdefault(_sector_key(ind), []).append(sym)
        for key, pct in anchor["sectors"].items():
            names = by_sector.get(key) or []
            others = [n + ".NS" for n in names if n + ".NS" not in fixed]
            if not names or any(o not in fitted or not g.get(o) for o in others): continue
            if not others:  # every stock in the sector is a fixed top constituent: already official
                anchored_sectors.append(key); continue
            budget = max(0.0, pct - sum(anchor["top"].get(n, 0) for n in names if n + ".NS" in fixed)) / 100
            ref_w = {o: fitted[o] / g[o] for o in others}
            total = sum(ref_w.values())
            for o in others:
                weights[o] = budget * (ref_w[o] / total if total > 0 else 1 / len(others)) * g[o]
            anchored_sectors.append(key)
    for s, w in fitted.items(): weights.setdefault(s, w)
    total = sum(weights.values())
    if total <= 0: raise Block("MOVERS: weight estimate failed")
    # The rolled-forward factsheet weights stay exactly as computed (factsheet x price ratio: the same past close gives the
    # same figure every run); only the fitted remainder is scaled to fill what they leave, so the fit window cannot move them.
    fixed_total, rest_total = sum(fixed.values()), sum(v for s, v in weights.items() if s not in fixed)
    if 0 < fixed_total < 1 and rest_total > 0:
        scale = (1 - fixed_total) / rest_total
        weights = {s: v if s in fixed else v * scale for s, v in weights.items()}
    else:
        weights = {s: v / total for s, v in weights.items()}
    w_mid = [to_mid(s, weights[s]) if previous_closes.get(s) else weights[s] for s in syms]
    return {"weights": weights, "raw_sum": round(total, 4), "observations": len(X), "r_squared": _r_squared(X, y, w_mid),
            "window": _window(common), "anchor": {"source": anchor["source"], "as_of": anchor["as_of"], "index": anchor.get("index", INDEX),
                                                  "fixed": sorted(s.removesuffix(".NS") for s in fixed),
                                                  "factsheet_pct": {s.removesuffix(".NS"): anchor["top"][s.removesuffix(".NS")] for s in fixed},
                                                  "sectors_anchored": len(anchored_sectors), "sectors": len(anchor["sectors"]),
                                                  "membership_matches": same_members}}


def index_weights(charts, index_symbol, previous_closes, index_previous, *, industries=None, today=None, index=INDEX):
    """Anchored weights when the factsheet file applies to this index and constituent list, else the pure fit."""
    anchor = load_anchor(index, today=today) if industries else None
    if anchor and any(s.removesuffix(".NS") in anchor["top"] for s in charts):
        try:
            return anchored_weights(charts, index_symbol, previous_closes, index_previous, anchor, industries)
        except ERRORS:
            pass
    return estimate_weights(charts, index_symbol, previous_closes, index_previous)


# ---------------------------------------------------------------- report

def contributions(changes, weights, index_previous):
    rows = []
    for s, c in changes.items():
        w = weights.get(s + ".NS")
        if w is None: continue
        rows.append({**c, "weight_pct": round(w * 100, 2), "points": round(w * c["change_pct"] / 100 * index_previous, 2)})
    return sorted(rows, key=lambda r: r["points"])


def live_changes(members, prev, charts):
    """{symbol: % change} from Yahoo's latest price against NSE's official previous close (bhavcopy)."""
    changes = {}
    for m in members:
        c = charts.get(m["symbol"] + ".NS"); p = prev.get(m["symbol"])
        if not c or not p: continue
        meta = c["meta"]; last = float(positive(meta["regularMarketPrice"]))
        changes[m["symbol"]] = {"symbol": m["symbol"], "name": m["name"], "industry": m["industry"], "price": last,
                                "previous_close": p["close"], "change_pct": round((last / p["close"] - 1) * 100, 2),
                                "at": datetime.fromtimestamp(int(meta["regularMarketTime"]), IST).isoformat()}
    return changes


def method_text(fit, index_previous_day=None):
    """How the weights were made: anchored to the factsheet (with its date) or the pure fit."""
    a = fit.get("anchor")
    if not a: return METHOD
    when = f"{date.fromisoformat(a['as_of']):%d %b %Y}"
    sectors = f"{a['sectors_anchored']} of {a['sectors']} sector totals" if a.get("membership_matches") else "no sector totals (constituent list changed since)"
    return ANCHOR_METHOD.format(when=when, source=a["source"], sectors=sectors, factsheet=FACTSHEET.get(a.get("index"), "Nifty 50"),
                                prev=f"the {date.fromisoformat(index_previous_day):%d %b} close" if index_previous_day else "the previous close")


def anchor_line(fit, index_previous_day=None):
    """'HDFCBANK 9.95% -> 10.64%' for each fixed factsheet weight: the official figure and the estimate it became."""
    a = fit.get("anchor")
    if not a or not a.get("factsheet_pct"): return None
    weights = fit["weights"] if "weights" in fit else {}
    pairs = []
    for sym, pct in sorted(a["factsheet_pct"].items(), key=lambda kv: -kv[1]):
        now = weights.get(sym + ".NS")
        if now is not None: pairs.append(f"{sym} {pct:.2f}% -> {now * 100:.2f}%")
    if not pairs: return None
    prev = f"{date.fromisoformat(index_previous_day):%d %b} close" if index_previous_day else "previous close"
    head = "Factsheet" if a.get("index", INDEX) == INDEX else FACTSHEET.get(a["index"], a["index"]) + " factsheet"
    return f"{head} weights ({date.fromisoformat(a['as_of']):%d %b %Y}) -> estimated at the {prev}, after price changes: " + ", ".join(pairs) + "."


def contributor_block(charts, index_symbol, changes, prev_closes, index_previous, *, live, actual=None, industries=None, today=None,
                      previous_day=None):
    """Estimated index-point contributions plus the check against the index. Live, the check uses the index level from
    the SAME Yahoo snapshot as the stock prices (with its time); for a finished session, the official session change.
    Nifty 50 weights are anchored to the NSE Indices factsheet when ``industries`` (the constituent list) is given."""
    name = next((n for n, sym in CHART.items() if sym == index_symbol), None)
    if name in (INDEX, BANK) and industries:  # factsheet-anchored when assets/index_weights.json has this index
        fit = index_weights(charts, index_symbol, prev_closes, index_previous, industries=industries, today=today, index=name)
    else:
        fit = estimate_weights(charts, index_symbol, prev_closes, index_previous)
    contrib = contributions(changes, fit["weights"], index_previous)
    explained = round(sum(r["points"] for r in contrib), 2)
    actual_basis, at = "NSE", None
    idx = charts.get(index_symbol)
    if live and idx:
        actual = round(float(positive(idx["meta"]["regularMarketPrice"])) - index_previous, 2)
        at = datetime.fromtimestamp(int(idx["meta"]["regularMarketTime"]), IST).isoformat()
        actual_basis = "Yahoo " + index_symbol + " at " + at[11:16] + " IST, same moment as the stock prices"
    return {"positive": [r for r in reversed(contrib) if r["points"] > 0][:5], "negative": [r for r in contrib if r["points"] < 0][:5],
            "estimated_total_points": explained, "actual_points": actual, "actual_basis": actual_basis, "actual_at": at,
            "method": method_text(fit, previous_day), "anchor_line": anchor_line(fit, previous_day),
            "fit": {k: v for k, v in fit.items() if k != "weights"},
            "heavyweights": sorted(contrib, key=lambda r: -r["weight_pct"])[:5],
            "weights_pct": {s.removesuffix(".NS"): round(v * 100, 2) for s, v in sorted(fit["weights"].items(), key=lambda kv: -kv[1])}}


def bar_check(charts, index_symbol, weights, prev_closes, index_previous):
    """Estimated vs actual index points at the SAME completed 5-minute bar of the latest session in the index chart
    (for a delayed index feed such as Yahoo's Sensex, whose latest quote lags the stock prices). None without one."""
    idx = series(charts[index_symbol])
    if not idx: return None
    today = datetime.fromtimestamp(max(idx), IST).date()
    stamps = sorted(t for t in idx if datetime.fromtimestamp(t, IST).date() == today)[:-1]  # the last bar may still be forming
    members = {s: series(charts[s]) for s in weights if charts.get(s) and prev_closes.get(s)}
    for t in reversed(stamps):
        if all(t in m for m in members.values()):
            est = sum(weights[s] * (m[t] / prev_closes[s] - 1) for s, m in members.items()) * index_previous
            at = datetime.fromtimestamp(t + 300, IST).isoformat()
            return {"estimated_points": round(est, 2), "actual_points": round(idx[t] - index_previous, 2), "at": at}
    return None


def sensex_block(charts, changes, prev_closes, sensex_previous, nifty_fit=None):
    """Estimated Sensex point contributions: identify the 30 Sensex members among Nifty 50 stocks from Sensex's own
    5-minute moves; weight them by the factsheet-anchored Nifty 50 weights rescaled to 100% (``nifty_fit`` from
    index_weights), else by a refit on them alone; check the estimate against Sensex at the same completed bar."""
    symbol = CHART[SENSEX]
    first = estimate_weights(charts, symbol, prev_closes, sensex_previous)
    picked = sorted(first["weights"], key=lambda s: -first["weights"][s])[:SENSEX_SIZE]
    subset = {s: charts[s] for s in picked} | {symbol: charts[symbol]}
    fit = estimate_weights(subset, symbol, prev_closes, sensex_previous)
    method = SENSEX_METHOD
    nw = (nifty_fit or {}).get("weights") or {}
    if (nifty_fit or {}).get("anchor") and all(nw.get(s) for s in picked):
        total = sum(nw[s] for s in picked)
        weights = {s: nw[s] / total for s in picked}
        index, members, common, X, y, syms = _returns(subset, symbol)
        mid = common[len(common) // 2]
        w_mid = [weights[s] * (members[s][mid] / prev_closes[s]) / (sensex_previous / index[mid]) if prev_closes.get(s) else weights[s] for s in syms]
        fit = {"weights": weights, "raw_sum": 1.0, "observations": len(X), "r_squared": _r_squared(X, y, w_mid), "window": _window(common),
               "anchor": {k: v for k, v in nifty_fit["anchor"].items() if k != "factsheet_pct"}}
        method = SENSEX_METHOD.replace(" and refitted alone.", ".") + " " + SENSEX_ANCHOR
    member_changes = {k: v for k, v in changes.items() if k + ".NS" in fit["weights"]}
    contrib = contributions(member_changes, fit["weights"], sensex_previous)
    check = bar_check(subset, symbol, fit["weights"], prev_closes, sensex_previous)
    return {"positive": [r for r in reversed(contrib) if r["points"] > 0][:5], "negative": [r for r in contrib if r["points"] < 0][:5],
            "estimated_total_points": round(sum(r["points"] for r in contrib), 2),
            "actual_points": check["actual_points"] if check else None, "actual_at": check["at"] if check else None,
            "check_estimated_points": check["estimated_points"] if check else None,
            "actual_basis": "Yahoo ^BSESN 5-minute bar vs the members' bars ending at the same time (Sensex's free feed is delayed)",
            "method": method, "fit": {k: v for k, v in fit.items() if k != "weights"}, "members": sorted(s.removesuffix(".NS") for s in picked),
            "heavyweights": sorted(contrib, key=lambda r: -r["weight_pct"])[:5],
            "weights_pct": {s.removesuffix(".NS"): round(v * 100, 2) for s, v in sorted(fit["weights"].items(), key=lambda kv: -kv[1])}}


def sector_extremes(sectors, n=3):
    """(strongest rising, weakest falling) sectors from a list sorted best first."""
    return [x for x in sectors if x["change_pct"] > 0][:n], [x for x in reversed(sectors) if x["change_pct"] < 0][:n]


def live_drivers(indices, snap, *, nse_fetch=None, yahoo_fetch=None, sensex_previous=None, previous_session=None):
    """What is moving each index now, for the daily briefings: strongest/weakest NSE sectors from the allIndices
    snapshot and, per index (NIFTY 50 / NIFTY BANK), estimated point lifts, drags and heavyweights from one Yahoo price
    snapshot against NSE's official previous closes. Raises on missing core inputs; callers treat it as optional."""
    import nse_live
    nse_fetch = nse_fetch or nse_live.fetch
    rows = snap["indices"]
    levels = [{"name": label(n), "change_pct": rows[n]["change_pct"]} for n in SECTORS if n in rows]
    up, down = sector_extremes(sorted(levels, key=lambda r: -r["change_pct"]))
    out = {"at": snap["as_of"], "source": snap["page"], "sectors_up": up, "sectors_down": down, "indices": {},
           "sector_levels": {x["name"]: x["change_pct"] for x in levels}}
    try:
        prev_day = next((rows[n]["previous_day"] for n in (INDEX, BANK) if n in rows and rows[n].get("previous_day")), None) or previous_session
        if not prev_day:  # NSE's evening snapshot can carry no usable previousDay: the holiday calendar gives it
            prev_day = nse_live.previous_session(instant(snap["as_of"]).date(), fetcher=nse_fetch).isoformat()
        prev = bhavcopy(date.fromisoformat(prev_day), fetcher=nse_fetch)["prices"]
    except ERRORS + (StopIteration,):
        out["error"] = "NSE previous-day bhavcopy unavailable after retries"
        return out  # sectors still answer "what is moving"
    members = {}
    for n in indices:
        if n == SENSEX and not sensex_previous: continue
        try: members[n] = constituents(fetcher=nse_fetch, index=INDEX if n == SENSEX else n)  # Sensex: candidates = Nifty 50
        except ERRORS: continue
    symbols = sorted({m["symbol"] for ms in members.values() for m in ms})
    wanted = [CHART[n] for n in members] + ([CHART[INDEX]] if SENSEX in members and INDEX not in members and INDEX in rows else [])
    charts = yahoo_charts([s + ".NS" for s in symbols] + wanted, fetcher=yahoo_fetch) if members else {}
    today = instant(snap["as_of"]).date()
    for n, ms in members.items():
        try:
            changes = live_changes(ms, prev, charts)
            subset = {m["symbol"] + ".NS": charts.get(m["symbol"] + ".NS") for m in ms} | {CHART[n]: charts[CHART[n]]}
            closes = {s + ".NS": prev[s]["close"] for s in changes}
            industries = {m["symbol"]: m["industry"] for m in ms}
            if n == SENSEX:
                nifty_fit = None
                if charts.get(CHART[INDEX]) and INDEX in rows:  # Sensex weights from the factsheet-anchored Nifty 50 weights
                    try:
                        nifty_subset = {k: v for k, v in subset.items() if k != CHART[SENSEX]} | {CHART[INDEX]: charts[CHART[INDEX]]}
                        nifty_fit = index_weights(nifty_subset, CHART[INDEX], closes, rows[INDEX]["previous_close"], industries=industries, today=today)
                    except ERRORS:
                        nifty_fit = None
                block = sensex_block(subset, changes, closes, float(sensex_previous), nifty_fit=nifty_fit)
                out["indices"][n] = {**block, "observed": len([s for s in changes if s + ".NS" in block["weights_pct"] or s in block["weights_pct"]]),
                                     "expected": SENSEX_SIZE}
                continue
            block = contributor_block(subset, CHART[n], changes, closes, rows[n]["previous_close"], live=True, industries=industries,
                                      today=today, previous_day=rows[n].get("previous_day") or prev_day)
            out["indices"][n] = {**block, "observed": len(changes), "expected": len(ms)}
        except ERRORS:
            continue
    if not out["indices"] and "error" not in out:  # name the stage that failed, as the movers report does
        got = sum(1 for v in charts.values() if v) if members else 0
        out["error"] = ("NSE's constituent list could not be downloaded" if not members else
                        f"Yahoo returned {got} of {len(charts)} constituent price charts within the time limit, too few to estimate weights")
    return out


def session_drivers(indices, session, *, nse_fetch=None, yahoo_fetch=None, sensex_previous=None, previous_session=None):
    """``live_drivers`` for a finished session (the weekend/holiday answer): strongest/weakest NSE sectors from that
    session's index-close file and, per index (NIFTY 50 / NIFTY BANK / SENSEX), estimated point lifts and drags from its
    bhavcopy (official close vs previous close). The one-month 5-minute charts for the weights are keyed to the session,
    so the charts saved by any run after that session's close are read back instead of downloaded again."""
    import nse_live
    nse_fetch = nse_fetch or nse_live.fetch
    day = date.fromisoformat(str(session)[:10])
    closes = nse_live.index_closes(day, fetcher=nse_fetch)
    levels = [{"name": label(n), "change_pct": closes[n]["change_pct"]} for n in SECTORS if n in closes]
    up, down = sector_extremes(sorted(levels, key=lambda r: -r["change_pct"]))
    out = {"at": session_close(day).isoformat(), "source": nse_live.INDEX_CLOSE.format(day.strftime("%d%m%Y")), "session": day.isoformat(),
           "sectors_up": up, "sectors_down": down, "indices": {}, "sector_levels": {x["name"]: x["change_pct"] for x in levels},
           "basis": "NSE bhavcopy and daily index report of the session (official closes)"}
    try:
        prices = bhavcopy(day, fetcher=nse_fetch)["prices"]
    except ERRORS:
        out["error"] = "NSE's bhavcopy for that session could not be downloaded after retries"
        return out
    members = {}
    for n in indices:
        if n == SENSEX and not sensex_previous: continue
        try: members[n] = constituents(fetcher=nse_fetch, index=INDEX if n == SENSEX else n)
        except ERRORS: continue
    symbols = sorted({m["symbol"] for ms in members.values() for m in ms})
    wanted = [CHART[n] for n in members] + ([CHART[INDEX]] if SENSEX in members and INDEX not in members else [])
    chart_now = datetime(day.year, day.month, day.day, 23, 59, tzinfo=IST)
    charts = yahoo_charts([s + ".NS" for s in symbols] + wanted, fetcher=yahoo_fetch, today=day, now=chart_now) if members else {}
    for n, ms in members.items():
        try:
            changes = {m["symbol"]: {"symbol": m["symbol"], "name": m["name"], "industry": m["industry"], "price": prices[m["symbol"]]["close"],
                                     "previous_close": prices[m["symbol"]]["previous_close"], "change_pct": round(prices[m["symbol"]]["change_pct"], 2)}
                       for m in ms if m["symbol"] in prices}
            subset = {m["symbol"] + ".NS": charts.get(m["symbol"] + ".NS") for m in ms} | {CHART[n]: charts[CHART[n]]}
            prev = {s + ".NS": c["previous_close"] for s, c in changes.items()}
            industries = {m["symbol"]: m["industry"] for m in ms}
            if n == SENSEX:
                nifty_fit = None
                if charts.get(CHART[INDEX]) and INDEX in closes:
                    try:
                        nifty_subset = {k: v for k, v in subset.items() if k != CHART[SENSEX]} | {CHART[INDEX]: charts[CHART[INDEX]]}
                        nifty_fit = index_weights(nifty_subset, CHART[INDEX], prev, round(closes[INDEX]["close"] - closes[INDEX]["change"], 2),
                                                  industries=industries, today=day)
                    except ERRORS:
                        nifty_fit = None
                block = sensex_block(subset, changes, prev, float(sensex_previous), nifty_fit=nifty_fit)
                out["indices"][n] = {**block, "observed": len([s for s in changes if s in block["weights_pct"]]), "expected": SENSEX_SIZE}
                continue
            row = closes[n]
            block = contributor_block(subset, CHART[n], changes, prev, round(row["close"] - row["change"], 2), live=False, actual=row["change"],
                                      industries=industries, today=day, previous_day=previous_session)
            out["indices"][n] = {**block, "observed": len(changes), "expected": len(ms)}
        except ERRORS:
            continue
    if not out["indices"] and "error" not in out:
        got = sum(1 for v in charts.values() if v) if members else 0
        out["error"] = ("NSE's constituent list could not be downloaded" if not members else
                        f"Yahoo returned {got} of {len(charts)} constituent price charts within the time limit, too few to estimate weights")
    return out


def previous_direction(previous_session, *, nse_fetch):
    """The last session's Nifty 50 close and change from NSE's dated index file ('if you meant yesterday')."""
    import nse_live
    row = nse_live.index_closes(date.fromisoformat(previous_session), fetcher=nse_fetch)[INDEX]
    return {"session": previous_session, "close": row["close"], "change": row["change"], "change_pct": row["change_pct"], "word": word(row["change"])}


MARKET_RE = re.compile(r"\b(sensex|nifty|dalal street|d-street|stock markets?|markets? (?:today|fall|falls|rise|rises|decline|declines|gain|gains|"
                       r"slip|slips|tumble|tumbles|crash|close|closes|open|opens|end|ends))\b", re.I)
OUTLOOK_RE = re.compile(r"prediction|outlook|stocks? to (?:buy|watch)|top picks|share price live|price live updates|among \d+ stocks|technical|"
                        r"shares today|\btill\b|so far this year|year[- ]to[- ]date|\bytd\b|will momentum|what next|explained|check targets?|"
                        r"higher upside|which .{0,60}\b(?:stock|share)s?\b (?:offers?|to buy)|"
                        # forecasts and trading tips are not causes: 'copper will hit $15,500/t in 2027', 'Buy X: SL 800 Tgt 828'
                        r"\bwill (?:hit|reach|touch|cross|rise|fall|climb|drop|rally|slide)\b|\bforecasts?\b|\bstock of the day\b|"
                        r"\b(?:sl|stop ?loss)\b.{0,25}\b(?:tgt|targets?)\b", re.I)
# A Google News market headline must report a move or session event, not just mention the indices.
MOVE_RE = re.compile(r"\b(fall|falls|fell|drop|drops|dropped|slip|slips|slid|slide|slides|decline|declines|declined|tumble|tumbles|crash|crashes|"
                     r"sink|sinks|slump|slumps|plunge|plunges|lower|red|rise|rises|rose|gain|gains|gained|rally|rallies|jump|jumps|surge|surges|"
                     r"climb|climbs|higher|green|recover|recovers|rebound|rebounds|snap|snaps|extend|extends|end|ends|close|closes|open|opens|"
                     r"trade|trades|selloff|sell-off|profit booking|weak|strong)\b", re.I)
# A per-stock headline must carry news about the stock (a move, results, orders, ratings, deals), not evergreen screens.
STOCK_NEWS_RE = re.compile(r"\b(fall|falls|fell|drop|drops|slip|slips|slide|slides|decline|declines|tumble|tumbles|sink|sinks|slump|slumps|"
                           r"plunge|plunges|crash|crashes|tank|tanks|rise|rises|gain|gains|jump|jumps|surge|surges|rally|rallies|soar|soars|"
                           r"correction|results?|q[1-4]|profit|loss|revenue|order|orders|deal|stake|block|rating|downgrade|upgrade|target|"
                           r"guidance|merger|acquisition|ban|probe|sebi|rbi|gold|crude|tariff)\b", re.I)
# Wording that gives a cause ("falls as gold crashes", "slips after downgrade"): such headlines rank before newer ones
# that only report the move. Connectives and cause nouns score one point each.
CAUSE_LINK_RE = re.compile(r"\b(as|after|on|amid|due to|following|on the back of|despite|led by|dragged by|weighs?|weighed|drags?|"
                           r"dragged|hits?|hurts?|triggers?|sparks?|spooks?)\b", re.I)
CAUSE_NOUN_RE = re.compile(r"\b(gold|silver|crude|oil prices?|brent|results?|q[1-4]|earnings|profit|margins?|guidance|downgrades?|upgrades?|"
                           r"rating|target price|orders?|contract|block deals?|bulk deals?|stake sale|stake|fii|fiis|fpi|fpis|foreign (?:investors?|funds?)|"
                           r"rupee|dollar|fed|rbi|sebi|tariffs?|probe|penalty|ban|profit[- ]booking|selloff|sell-off|bond yields?|inflation|"
                           r"rate cut|rate hike|nvidia|ai stocks|wall street|global cues|asian markets)\b", re.I)
# "Why is the market falling today?", "key reasons behind": explanations, kept even though OUTLOOK_RE drops "explained".
EXPLAIN_RE = re.compile(r"\bwhy\b.{0,80}\b(?:today|fall\w*|ris\w*|drop\w*|slump\w*|crash\w*|down|up)\b|\b(?:key )?(?:reasons?|factors?)\b.{0,12}"
                        r"\b(?:behind|for|why|that)\b|\bwhat(?:'s| is) (?:dragging|driving|behind)\b", re.I)
# Corporate housekeeping that does not move a price: ESOP/option grants, AGM/EGM notices, board-meeting intimations,
# trading-window closures, record dates.
ADMIN_RE = re.compile(r"\b(esops?|esos|esps|stock options?|options? (?:granted|grant)|grants? (?:of )?(?:stock )?options|employee stock|agm|egm|"
                      r"annual general meeting|extraordinary general meeting|postal ballot|board meeting|intimation|trading window|"
                      r"record date|book closure|newspaper (?:advertisement|publication)|investor meet schedule|analyst meet)\b", re.I)
UP_WORD_RE = re.compile(r"\b(rise|rises|rose|rising|gain|gains|gained|rally|rallies|rallied|jump|jumps|jumped|surge|surges|surged|climb|climbs|"
                        r"climbed|soar|soars|soared|higher|advance|advances|rebound|rebounds|recover|recovers|record high|fresh high|"
                        r"extends gains|up (?:nearly |almost |over |about |around |more than |up to |as much as )?\d)", re.I)
DOWN_WORD_RE = re.compile(r"\b(fall|falls|fell|falling|drop|drops|dropped|slip|slips|slipped|slide|slides|slid|decline|declines|declined|"
                          r"tumble|tumbles|tumbled|crash|crashes|crashed|sink|sinks|sank|slump|slumps|slumped|plunge|plunges|plunged|tank|"
                          r"tanks|lower|sheds?|loses|lost|crack|cracks|bleed|bleeds|extends losses|down (?:nearly |almost |over |about |around |more than |up to |as much as )?\d)", re.I)
# A story about several days or weeks, not today ("Nifty rallies 13% as FIIs pour Rs 88,300 cr", "best week since June").
RETRO_RE = re.compile(r"\b(in (?:a|one|two|three|four|five|\d+) (?:days?|weeks?|months?|years?|sessions?)|this (?:week|month|year)|so far this|"
                      r"past (?:week|month|year|\d+ (?:days|weeks|months|sessions))|over (?:the )?(?:past|last)\b|"
                      r"\d+(?:st|nd|rd|th)? (?:straight|consecutive) (?:sessions?|days?|weeks?)|(?:weekly|monthly|yearly|annual) (?:gain|loss|rise|fall|rally)|"
                      r"since (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|since \d{4}|year[- ]to[- ]date|ytd)\b", re.I)


def causal_score(headline):
    """0-4: a cause connective, a cause noun, and two for an explicit explanation ('why ... today', 'key reasons behind')."""
    return int(bool(CAUSE_LINK_RE.search(headline))) + int(bool(CAUSE_NOUN_RE.search(headline))) + 2 * int(bool(EXPLAIN_RE.search(headline)))


def headline_direction(headline):
    """'up' / 'down' from the first direction word in the headline (its main verb), else None."""
    u, d = UP_WORD_RE.search(headline), DOWN_WORD_RE.search(headline)
    if not u and not d: return None
    return "up" if u and (not d or u.start() < d.start()) else "down"


def retrospective(headline, index_story=False):
    """A multi-day/multi-week story: dated by its own wording, or (for an index) a move of 4% or more, far beyond a day."""
    if RETRO_RE.search(headline): return True
    return index_story and any(float(x) >= 4 for x in re.findall(r"(\d+(?:\.\d+)?)\s?%", headline))


def opposite_bigger_move(headline, change_pct):
    """A stock headline whose direction is against the stock's move today and whose quoted move is larger than today's
    whole move ('yet down nearly 19%' on a +1.5% day): another period's story, not today's cause."""
    if not change_pct: return False
    d = headline_direction(headline)
    if d is None or d == ("up" if change_pct > 0 else "down"): return False
    sizes = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s?%", headline)]
    return any(x > abs(change_pct) + 2 for x in sizes)


def contradicts(headline, change, index_story=False):
    """A retrospective story whose direction is the opposite of today's move ('rallies 13%' on a falling day)."""
    if not change: return False
    d = headline_direction(headline)
    return d is not None and d != ("up" if change > 0 else "down") and retrospective(headline, index_story)


# Macro and flow terms that can explain a market-wide move (a headline needs one of these, or a moving stock/sector).
MACRO_RE = re.compile(r"\b(fiis?|fpis?|foreign (?:investors?|funds?|inflows?|outflows?|selling|buying)|dii|fed|federal reserve|powell|"
                      r"crude|oil prices?|brent|rupee|dollar|dow|wall street|nasdaq|s&p 500|us (?:markets?|stocks|yields?|inflation|jobs|data)|"
                      r"global cues|asian markets?|gold|rbi|repo rate|rate cuts?|rate hikes?|interest rates?|inflation|cpi|wpi|gdp|"
                      r"bond yields?|treasury|profit[- ]booking|tariffs?|trade deal|geopolitic\w*|war|ceasefire|election results?|budget|gst|"
                      r"monsoon|q[1-4] results|earnings season|sebi)\b", re.I)
# NSE sector names -> words a headline uses for them. "IT" is matched case-sensitively so the pronoun "it" never counts.
SECTOR_WORDS = {"Nifty Auto": r"auto(?:mobile)?s?", "Nifty Bank": r"bank(?:s|ing)?", "Nifty Financial Services": r"financials?|nbfcs?",
                "Nifty FMCG": r"fmcg", "Nifty IT": r"(?-i:IT)|tech(?:nology)? stocks", "Nifty Media": r"media", "Nifty Metal": r"metals?|steel",
                "Nifty Pharma": r"pharma(?:ceuticals?)?", "Nifty PSU Bank": r"psu banks?|public sector banks?", "Nifty Private Bank": r"private banks?",
                "Nifty Realty": r"realty|real estate", "Nifty Healthcare": r"healthcare|hospitals?", "Nifty Consumer Durables": r"consumer durables?|jewell?ery",
                "Nifty Oil & Gas": r"oil (?:and|&) gas|omcs?", "Nifty Energy": r"energy|power stocks", "Nifty Cement": r"cement",
                "Nifty Chemicals": r"chemicals?"}
# A dated mention such as "11 Nov", "Nov 11" or "November 11th" inside a headline.
DATE_RE = re.compile(r"\b(?:(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*|"
                     r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?)\b", re.I)
MONTHS = {m: n for n, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


# How headlines name the big index stocks (symbol -> names); other stocks use the first word(s) of the company name.
ALIASES = {"SBIN": ("SBI", "State Bank"), "LT": ("L&T", "Larsen"), "M&M": ("M&M", "Mahindra"), "BAJFINANCE": ("Bajaj Finance",),
           "BAJAJFINSV": ("Bajaj Finserv",), "BAJAJ-AUTO": ("Bajaj Auto",), "HINDUNILVR": ("HUL", "Hindustan Unilever"),
           "BHARTIARTL": ("Airtel",), "ICICIBANK": ("ICICI Bank",), "HDFCBANK": ("HDFC Bank",), "HDFCLIFE": ("HDFC Life",),
           "KOTAKBANK": ("Kotak",), "AXISBANK": ("Axis Bank",), "TMPV": ("Tata Motors",), "RELIANCE": ("Reliance", "RIL"),
           "ETERNAL": ("Eternal", "Zomato"), "INFY": ("Infosys",), "TATASTEEL": ("Tata Steel",), "TATACONSUM": ("Tata Consumer",),
           "POWERGRID": ("Power Grid",), "ULTRACEMCO": ("UltraTech",), "ADANIPORTS": ("Adani Ports",), "ADANIENT": ("Adani Enterprises",),
           "SUNPHARMA": ("Sun Pharma",), "HCLTECH": ("HCL Tech", "HCLTech"), "TECHM": ("Tech Mahindra",), "INDIGO": ("IndiGo", "InterGlobe"),
           "DRREDDY": ("Dr Reddy", "Dr. Reddy"), "COALINDIA": ("Coal India",), "JIOFIN": ("Jio Financial",), "SHRIRAMFIN": ("Shriram Finance",),
           "SBILIFE": ("SBI Life",), "BEL": ("BEL", "Bharat Electronics"), "EICHERMOT": ("Eicher",), "APOLLOHOSP": ("Apollo Hospitals",),
           "MAXHEALTH": ("Max Healthcare",), "ASIANPAINT": ("Asian Paints",), "NESTLEIND": ("Nestle",)}
GENERIC_FIRST = {"tata", "adani", "bajaj", "hdfc", "icici", "sbi", "state", "bharat", "hindustan", "sun", "power", "coal", "max", "jio", "asian"}


def mover_terms(result):
    """Regex fragments for what is actually moving: the top point contributors and %-movers (symbol and the first word(s)
    of the company name) and the strongest/weakest sectors."""
    stocks = {}
    c = result.get("contributors") or {}
    for r in (c.get("negative") or [])[:5] + (c.get("positive") or [])[:5] + (result.get("gainers") or [])[:5] + (result.get("losers") or [])[:5]:
        stocks.setdefault(r["symbol"], r.get("name") or r["symbol"])
    terms = name_terms(stocks)
    sectors = result.get("sectors") or []
    moving = [x for x in sectors if x["change_pct"] > 0][:3] + [x for x in reversed(sectors) if x["change_pct"] < 0][:3]
    for x in moving:
        if x["name"] in SECTOR_WORDS: terms.append(SECTOR_WORDS[x["name"]])
    return terms


def name_terms(stocks):
    """Regex fragments for {symbol: company name}: the symbol, known aliases, and the first word(s) of the name."""
    terms = []
    for sym, name in stocks.items():
        terms.append(re.escape(sym))
        if sym in ALIASES:
            terms += [re.escape(a) for a in ALIASES[sym]]; continue
        words = [w for w in str(name).split() if w.lower().strip(".") not in {"ltd", "limited", "the"}]
        if not words: continue
        key = " ".join(words[:2]) if words[0].lower() in GENERIC_FIRST and len(words) > 1 else words[0]
        if len(key) >= 3: terms.append(re.escape(key))
    return terms


def relevant(headline, terms):
    """A headline that can explain the move: it names a moving stock or sector, or a macro/flow driver, or says it explains
    the market's move ('why is the market falling today?')."""
    if MACRO_RE.search(headline) or (EXPLAIN_RE.search(headline) and MARKET_RE.search(headline)): return True
    return any(re.search(r"\b(?:" + t + r")\b", headline, re.I) for t in terms)


def dated_before(headline, session):
    """True when the headline itself dates its news to a day before the session (e.g. 'QIP launched on Nov 11')."""
    day = date.fromisoformat(str(session)[:10])
    for m in DATE_RE.finditer(headline):
        d, mon = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        try:
            when = date(day.year, MONTHS[mon.lower()[:3]], int(d))
        except (KeyError, ValueError):
            continue
        if when > day + timedelta(days=200): when = when.replace(year=day.year - 1)  # "28 Dec" read in January
        if day - timedelta(days=200) <= when < day: return True  # a later date (an upcoming record date) is not old news
    return False


WRAP_RE = re.compile(r"\b(?:market wrap|closing bell|closes?|closed|ends?|ended|settles?|settled)\b|\d(?:\.\d+)?\s?%|\b\d[\d,]*\s?(?:points|pts)\b", re.I)


GLOBAL_CUE_RE = re.compile(r"\b(?:gift nifty|wall street|dow|nasdaq|s&p 500|us (?:stocks?|futures|markets?)|asian markets?|nikkei|"
                           r"hang seng|kospi|copper|alumini?um|crude|brent|gold|silver|lme)\b", re.I)


def earlier_session_report(h, session):
    """A headline published before ``session`` opened that reports a move ('Nifty Auto surges 4.4% as Maruti jumps',
    'Sensex ends 990 points higher'): it describes the session before (an evening wrap-up), not ``session``'s move.
    Overnight news without a reported Indian move (results, orders, a policy) and overnight global or commodity moves
    (GIFT Nifty, Wall Street, crude, copper) are kept: they can explain the next open."""
    if not session: return False
    t = h["headline"]
    if instant(h["published_at"]) >= session_open(session): return False
    indian = bool(MARKET_RE.search(t) or re.search(r"\b(?:stocks?|shares?|sectors?|index|scrips?|counters?)\b", t, re.I))
    if GLOBAL_CUE_RE.search(t) and not indian: return False  # 'Copper jumps 3% on China stimulus': an overnight driver
    moved = bool(UP_WORD_RE.search(t) or DOWN_WORD_RE.search(t) or re.search(r"\b(?:gainers|losers|leads? gains|drags?)\b", t, re.I))
    return moved and (indian or bool(WRAP_RE.search(t)))


def _words(headline):
    text = re.sub(r"\b2\b", "two", headline.lower())
    return set(re.findall(r"[a-z0-9]+", text)) - {"the", "a", "an", "of", "in", "on", "as", "after", "to", "and", "for"}


def _same_story(a, b):
    """Near-duplicate syndicated headlines ('two-day rally' vs '2-day rally'): word overlap of 70% or more."""
    x, y = _words(a), _words(b)
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= 0.7


def _outlook(headline):
    """A preview, screen or evergreen explainer - unless it explains today's move ('why is the market falling today?')."""
    return bool(OUTLOOK_RE.search(headline)) and not EXPLAIN_RE.search(headline)


def market_headlines(now, *, news_fetch=None, limit=3, terms=None, since=None, session=None, change=None, window=None):
    """Dated market-wide headlines: ET Markets RSS merged with Google News RSS ('Sensex Nifty when:1d', India edition;
    the fallback when ET is down or stale), Indian-market headlines first, those giving a cause before newer ones that
    only report the move, then newest first. Source claims, never causes.

    ``terms`` (from ``mover_terms``): when given, keep only headlines naming a moving stock/sector or a macro driver.
    ``since``: drop headlines published before it (the previous session's close); ``session``: drop headlines that
    date their own news to an earlier day; ``change`` (today's index change): drop multi-day stories that run against
    it ('Nifty rallies 13% as FIIs pour in' on a falling day)."""
    import india_news
    kw = {"fetcher": news_fetch} if news_fetch else {}
    when, hours = window or ("when:1d", 18)
    picks, failed = [], []
    try:
        feed = india_news.collect(["et-markets"], hours, clock=lambda: now, **kw)
        if feed["status"] == "BLOCKED": failed.append("et-markets")
        picks = [{"headline": r["headline"], "publisher": "ET Markets", "url": r["url"], "published_at": r["published_at"], "source": "ET Markets RSS"}
                 for r in feed["records"] if NEWS_RE.search(r["headline"]) and not NEWS_NOISE_RE.search(r["headline"])]
    except ERRORS:
        failed.append("et-markets")
    try:
        seen = {re.sub(r"\W+", " ", p["headline"].lower()).strip() for p in picks}
        for r in india_news.search("Sensex Nifty " + when, hours, clock=lambda: now, **kw):
            key = re.sub(r"\W+", " ", r["headline"].lower()).strip()
            if (MARKET_RE.search(r["headline"]) and MOVE_RE.search(r["headline"]) and not _outlook(r["headline"]) and key not in seen
                    and not any(_same_story(r["headline"], p["headline"]) for p in picks)):
                picks.append({**r, "source": "Google News RSS"}); seen.add(key)
    except ERRORS:
        failed.append("google-news")
    if terms is not None:
        picks = [p for p in picks if relevant(p["headline"], terms)]
    if since is not None:
        picks = [p for p in picks if instant(p["published_at"]) >= since]
    if session is not None:
        picks = [p for p in picks if not dated_before(p["headline"], session) and not (window and earlier_session_report(p, session))]
    if change is not None:
        picks = [p for p in picks if not contradicts(p["headline"], change, index_story=bool(MARKET_RE.search(p["headline"])))]
        if window and change:  # a finished session: an intraday move the other way ('falls 300 pts' at 09:22 on an up day) is not the cause
            side = "up" if change > 0 else "down"
            picks = [p for p in picks if not (MARKET_RE.search(p["headline"]) and headline_direction(p["headline"]) not in (None, side))]
    # Indian-market headlines first, those giving a cause first, then newest first; other market news (global cues) after.
    picks.sort(key=lambda r: (not MARKET_RE.search(r["headline"]), -causal_score(r["headline"]), -instant(r["published_at"]).timestamp()))
    return picks[:5], failed


SECTOR_QUERY = {"Nifty IT": "IT stocks", "Nifty FMCG": "FMCG stocks", "Nifty PSU Bank": "PSU bank stocks", "Nifty Oil & Gas": "oil and gas stocks",
                "Nifty Financial Services": "financial stocks", "Nifty Bank": "bank stocks", "Nifty Chemicals": "chemical stocks"}


def sector_query(name):
    return SECTOR_QUERY.get(name) or name.removeprefix("Nifty ").lower() + " stocks"


def mentions_sector(headline, name):
    pattern = SECTOR_WORDS.get(name)
    return bool(pattern and re.search(r"\b(?:" + pattern + r")\b", headline, re.I))


def sector_headline(sector, now, *, news_fetch=None, since=None, session=None, window=None):
    """One dated headline giving a cause for a sector's move ('Why did metal stocks fall ... today?'), or None: it must
    name the sector, report a move or give a reason on the side of the sector's move, be published after ``since`` and
    not date itself to an earlier day. Headlines that only report the move are not kept."""
    import india_news
    kw = {"fetcher": news_fetch} if news_fetch else {}
    change = sector.get("change_pct") or 0
    side = "up" if change > 0 else "down"
    found = []
    when, hours = window or ("when:1d", 24)
    for h in india_news.search(sector_query(sector["name"]) + " " + when, hours, clock=lambda: now, **kw):
        t = h["headline"]
        if (mentions_sector(t, sector["name"]) and (STOCK_NEWS_RE.search(t) or EXPLAIN_RE.search(t)) and causal_score(t) > 0
                and not _outlook(t) and not ADMIN_RE.search(t) and headline_direction(t) in (None, side)
                and (since is None or instant(h["published_at"]) >= since) and (session is None or not dated_before(t, session))
                and not (window and earlier_session_report(h, session))
                and not contradicts(t, change)):
            found.append(h)
    if not found: return None
    best = max(found, key=lambda h: (causal_score(h["headline"]), instant(h["published_at"]).timestamp()))
    return {**best, "sector": sector["name"], "source": "Google News RSS"}


def sector_headlines(result, shown, now, *, news_fetch=None, since=None, session=None, count=2, window=None):
    """For the ``count`` sectors leading the move (weakest on a falling day), one cause headline each unless a headline
    already shown names that sector."""
    change = result["direction"]["change"]
    sectors = result.get("sectors") or []
    leading = [x for x in reversed(sectors) if x["change_pct"] < 0][:count] if change < 0 else [x for x in sectors if x["change_pct"] > 0][:count]
    out = []
    for sec in leading:
        if any(mentions_sector(h["headline"], sec["name"]) for h in shown + out): continue
        try:
            h = sector_headline(sec, now, news_fetch=news_fetch, since=since, session=session, window=window)
        except Exception:  # noqa: BLE001 - one failed search only costs that sector's headline
            continue
        if h and not any(_same_story(h["headline"], s["headline"]) for s in shown + out): out.append(h)
    return out


def stock_headlines(rows, now, *, news_fetch=None, per_stock=2, since=None, session=None, window=None):
    """Google News headlines that name each stock (company name or symbol in the title), those giving a cause ('falls as
    gold crashes') before newer ones that only report the move; a second headline per stock only when it also gives a
    cause and is a different story. Dropped: corporate housekeeping (ESOP grants, AGM/EGM, board-meeting intimations,
    trading-window closures), stock news published before ``since`` (the previous session's close) or dated in its own
    text to an earlier day than ``session`` (a QIP launched a week ago), and multi-day stories against the stock's
    move today (``change_pct`` in the row)."""
    import india_news
    kw = {"fetcher": news_fetch} if news_fetch else {}
    when, hours = window or ("when:1d", 24)
    out = []
    for r in rows:
        name = re.sub(r"\s+(?:Ltd\.?|Limited)$", "", str(r.get("name") or r["symbol"])).strip()
        key = name.split()[0].lower()
        try:
            found = [h for h in india_news.search(f'"{name}" share {when}', hours, clock=lambda: now, **kw)
                     if (key in h["headline"].lower() or r["symbol"].lower() in h["headline"].lower())
                     and STOCK_NEWS_RE.search(h["headline"]) and not _outlook(h["headline"]) and not ADMIN_RE.search(h["headline"])
                     and (since is None or instant(h["published_at"]) >= since)
                     and (session is None or not dated_before(h["headline"], session)) and not (window and earlier_session_report(h, session))
                     and not contradicts(h["headline"], r.get("change_pct")) and not opposite_bigger_move(h["headline"], r.get("change_pct"))]
        except ERRORS:
            continue
        found.sort(key=lambda h: (-causal_score(h["headline"]), -instant(h["published_at"]).timestamp()))
        kept = []
        for h in found:
            if len(kept) >= per_stock: break
            if kept and (not causal_score(h["headline"]) or any(_same_story(h["headline"], k["headline"]) for k in kept)): continue
            kept.append(h)
        out += [{**h, "symbol": r["symbol"], "source": "Google News RSS"} for h in kept]
    return out


# ---------------------------------------------------------------- same-day "why" for the daily briefings

NEWS_TIMEOUT = 6     # seconds per news request inside news_context
NEWS_BUDGET = 9.0    # seconds for all reason and cue searches together; slower ones become a stated gap
# Overnight and live cues: (label, Google News queries, what the headline must name). Newest matching headline each.
CUES = (("GIFT Nifty", ("GIFT Nifty when:1d",), r"\bgift nifty\b"),
        ("US markets", ("US stock futures when:1d", "Wall Street when:1d"),
         r"\b(?:wall street|dow(?: jones)?|nasdaq|s&p 500|u\.?s\.? (?:stocks?|stock futures|futures|markets?))\b"),
        ("Gold", ("gold price MCX when:1d", "gold price when:1d"), r"\bgold\b"),
        ("Crude", ("Brent crude oil price when:1d", "crude oil price when:1d"), r"\b(?:crude|brent|wti|oil prices?)\b"),
        ("Rupee", ("rupee dollar when:1d", "Indian rupee when:1d"), r"\b(?:rupee|inr)\b"))
# Cues are about India's market: the Indian rupee (not the Pakistani or Sri Lankan rupee), COMEX/MCX/Indian gold (not Thai
# baht or Gulf retail prices), Brent/WTI crude (not Oman or Dubai crude). A headline or publisher naming another country's
# currency or market is dropped; a rupee rate far above USD/INR's range (e.g. 'Rupee closes at 276.55') is another currency.
FOREIGN_CUE_RE = re.compile(r"\b(pakistan\w*|pkr|sri lanka\w*|lankan|lkr|nepal\w*|npr|bangladesh\w*|taka|thai\w*|baht|thb|malaysia\w*|ringgit|"
                            r"indonesia\w*|rupiah|philippine\w*|peso|vietnam\w*|singapore\w*|uae|dubai|dirham|aed|saudi|riyal|qatar\w*|oman\w*|"
                            r"omani|kuwait\w*|bahrain\w*|egypt\w*|kenya\w*|nigeria\w*|naira|ghana\w*|murban|urals|basrah|turk\w*|lira|"
                            r"mauritius|maldiv\w*|seychelles)\b", re.I)
# Publishers based in neighbouring markets, whose "rupee" is not the Indian rupee.
FOREIGN_PUBLISHER_RE = re.compile(r"business recorder|\bdawn\b|express tribune|the news international|\bgeo\b|\bary\b|urdupoint|propakistani|"
                                  r"daily mirror|economynext|daily ft|newsfirst|ada derana|kathmandu|himalayan|rising nepal|daily star|"
                                  r"dhaka|tbs news|gulf news|khaleej|arab news|zawya|bernama", re.I)
INDIA_CUE_RE = re.compile(r"\b(india\w*|mcx|comex|spot gold|rs\.?|inr|sensex|nifty|dalal street|brent|wti|10 grams?|domestic)\b|₹", re.I)
# Indian business media and the major wires; a headline from one of these is preferred over a newer one from elsewhere.
PREFERRED_PUBLISHERS = re.compile(r"economic ?times|\bet ?now\b|moneycontrol|business standard|\bmint\b|livemint|business ?line|financial express|"
                                  r"ndtv|cnbc|business today|zee business|india today|hindustan times|times of india|the hindu|\bpti\b|"
                                  r"press trust|reuters|bloomberg|bq prime|news18|indian express|firstpost|outlook|deccan herald|hdfc sky|"
                                  r"upstox|groww|goodreturns|investing\.com|kitco|oilprice|wall street journal|financial times|marketwatch|"
                                  r"\bfe\b|devdiscourse|the print|scanx|angel one|5paisa|icici|kotak|motilal", re.I)


def foreign_cue(headline, publisher=None):
    """A cue headline about another country's currency, gold or crude market (or from a publisher named after one)."""
    who = str(publisher or "")
    if FOREIGN_CUE_RE.search(headline) or FOREIGN_CUE_RE.search(who) or FOREIGN_PUBLISHER_RE.search(who):
        return True
    if who and any(ord(ch) > 0x2FF for ch in who):  # a publisher name in a non-Latin script (e.g. an Arabic news agency)
        return True
    if re.search(r"\b(?:rupee|inr)\b", headline, re.I):
        rates = [float(x) for x in re.findall(r"\b(\d{2,4}\.\d{1,4})\b", headline)]
        if any(130 < r < 2000 for r in rates): return True  # USD/INR trades far below this; PKR/LKR rates sit near 280-300
    return False


def cue_rank(h):
    """Preference among matching cue headlines: an Indian/major publisher, then an India marker (MCX, ₹, Indian rupee), then
    the newest."""
    return (bool(PREFERRED_PUBLISHERS.search(str(h.get("publisher") or ""))), bool(INDIA_CUE_RE.search(h["headline"])),
            instant(h["published_at"]).timestamp())
CUE_MOVE_RE = re.compile(r"\b(fall|falls|fell|drop|drops|dropped|slip|slips|slide|slides|slid|decline|declines|tumble|tumbles|crash|crashes|"
                         r"sink|sinks|slump|slumps|plunge|plunges|lower|lows?|rise|rises|rose|gain|gains|jump|jumps|surge|surges|rally|rallies|"
                         r"climb|climbs|higher|highs?|rebound|rebounds|extends?|eases?|firms?|steady|flat|mixed|strengthens?|weakens?|"
                         r"signals?|indicates?|points? to|gap-(?:up|down)|opens?|ends?|closes?|sinks?|sheds?)\b", re.I)
CUE_NOISE_RE = re.compile(r"\b(sri lanka\w*|pakistan\w*|nepal\w*|bangladesh\w*|rate today in|rates today in|prices? in (?:delhi|mumbai|chennai|"
                          r"kolkata|hyderabad|bengaluru|coimbatore)|check latest|price target|preview|bullish or bearish|share price live)\b", re.I)


def possible_reasons(now, *, change, movers, sectors, session, previous_session, news_fetch=None, limit=3, stocks=2, window=None):
    """Up to ``limit`` dated headlines that may explain today's move, for the daily briefings: market-wide headlines and
    the top ``stocks`` movers' own headlines, through the same filters as the movers report (since the last close, no old
    or housekeeping news, nothing running against today's move), headlines giving a cause first. ``movers``: the
    contribution rows on the side of the move (symbol, name, change_pct); ``sectors``: [{name, change_pct}] best first.
    Returns (items, failed sources)."""
    since = session_close(previous_session)
    result = {"contributors": {"negative" if (change or 0) < 0 else "positive": movers}, "sectors": sectors}
    market, failed = market_headlines(now, news_fetch=news_fetch, terms=mover_terms(result), since=since, session=session, change=change,
                                      window=window)
    stock = stock_headlines(movers[:stocks], now, news_fetch=news_fetch, since=since, session=session, window=window)
    # Each top mover's best causal headline first (the biggest drags/lifts explained), then market explanations, then the rest.
    pool = [(causal_score(h["headline"]) + (1 if h.get("symbol") and causal_score(h["headline"]) else 0), h) for h in stock + market]
    pool.sort(key=lambda x: (-x[0], -instant(x[1]["published_at"]).timestamp()))
    out = []
    for _, h in pool:
        if len(out) >= limit: break
        if any(_same_story(h["headline"], o["headline"]) for o in out): continue
        if h.get("symbol") and sum(o.get("symbol") == h["symbol"] for o in out) >= 1 and len(pool) > limit: continue
        out.append(h)
    return out, failed


def market_cues(now, *, since=None, news_fetch=None, since_by=None):
    """The newest dated Google News headline for each cue in CUES (GIFT Nifty, US markets, gold, crude, rupee) published
    since ``since`` (the previous session's close; ``since_by`` overrides it per cue label): {label: headline or None}.
    The cues are searched in parallel. Headlines only, unverified."""
    import india_news
    kw = {"fetcher": news_fetch} if news_fetch else {}

    def one(label, queries, pattern):
        cut = (since_by or {}).get(label, since)
        found, errors = [], []
        for q in queries:
            try:
                rows = india_news.search(q, 24, clock=lambda: now, **kw)
            except Exception as exc:  # noqa: BLE001 - one failed query; the other may still answer
                errors.append(exc); continue
            for h in rows:
                t = h["headline"]
                if (re.search(pattern, t, re.I) and CUE_MOVE_RE.search(t) and not CUE_NOISE_RE.search(t) and not _outlook(t)
                        and not foreign_cue(t, h.get("publisher")) and (cut is None or instant(h["published_at"]) >= cut)):
                    same = [f for f in found if _same_story(t, f["headline"])]
                    if not same: found.append(h)
                    elif cue_rank(h) > cue_rank(same[0]): found[found.index(same[0])] = h  # the same story: keep the better source
            if found:
                break  # the next query is a fallback only (Google News rate-limits bursts of searches)
        if not found and errors and len(errors) == len(queries): raise errors[0]
        best = max(found, key=cue_rank) if found else None
        return best and {**best, "source": "Google News RSS", "cue": label}
    with ThreadPoolExecutor(max_workers=len(CUES)) as pool:
        found = [pool.submit(one, *cue) for cue in CUES]
        wait(found)
    failed = [f.exception() for f in found if f.exception() is not None]
    if len(failed) == len(found): raise failed[0]  # every search failed: the caller states the gap
    return {cue[0]: (None if f.exception() is not None else f.result()) for cue, f in zip(CUES, found)}


def session_open(day):
    """09:15 IST on ``day``. None when the date is unknown."""
    if not day: return None
    d = date.fromisoformat(str(day)[:10])
    return datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)


def cue_window(phase, previous_session, session):
    """(since, since_by) for the cue headlines. During the session: everything since the previous close. After the close
    the cues look ahead to the next session: GIFT Nifty only since today's 15:30 close (its morning headlines were about
    today's open), the rest (US markets, gold, crude, rupee) since today's open."""
    if phase == "close" and session:
        return session_open(session), {"GIFT Nifty": session_close(session)}
    return session_close(previous_session), None


def news_getter(news_fetch=None):
    import india_news
    return news_fetch or (lambda url: india_news.fetch(url, timeout=NEWS_TIMEOUT))


def news_context(now, requests, *, previous_session, session, news_fetch=None, budget=None, phase="intraday", cues=None, window=None):
    """Possible reasons for several indices plus the cues, fetched in parallel within ``budget`` seconds (default
    NEWS_BUDGET). ``requests``: {key: dict(change=, movers=, sectors=)}. Whatever has not answered in time, or failed,
    is listed in ``gaps`` instead of holding up the report: {"reasons": {key: [...]}, "cues": {...}, "gaps": [...]}.
    ``phase``: "close" makes the cues look ahead (see ``cue_window``); ``cues``: a cue search already running (a
    Future started alongside the price work), awaited within the same budget instead of starting another."""
    get = news_getter(news_fetch)
    since = session_close(previous_session)
    cue_since, cue_by = cue_window(phase, previous_session, session)
    pool = ThreadPoolExecutor(max_workers=min(6, len(requests) + 1))
    jobs = {pool.submit(possible_reasons, now, session=session, previous_session=previous_session, news_fetch=get, window=window, **req): ("reasons", key)
            for key, req in requests.items()}
    jobs[cues or pool.submit(market_cues, now, since=cue_since, since_by=cue_by, news_fetch=get)] = ("cues", None)
    out = {"reasons": {}, "cues": None, "gaps": [], "since": since.isoformat() if since else None, "phase": phase,
           "cues_since": cue_since.isoformat() if cue_since else None,
           "cues_since_by": {k: v.isoformat() for k, v in (cue_by or {}).items()}}
    try:
        done, pending = wait(jobs, timeout=NEWS_BUDGET if budget is None else budget)
        for f in done:
            kind, key = jobs[f]
            try:
                value = f.result()
            except Exception:  # noqa: BLE001 - one failed search is a stated gap, never a crash
                out["gaps"].append("cues" if kind == "cues" else f"reasons:{key}"); continue
            if kind == "cues": out["cues"] = value
            else:
                out["reasons"][key] = value[0]
                if value[1] and not value[0]: out["gaps"].append(f"reasons:{key}")
        for f in pending:
            kind, key = jobs[f]
            out["gaps"].append("cues" if kind == "cues" else f"reasons:{key}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def session_close(day):
    """15:30 IST on ``day``: the cut-off for 'news since the last close'. None when the date is unknown."""
    if not day: return None
    d = date.fromisoformat(str(day)[:10])
    return datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST)


MAX_NEWS_DAYS = 7  # Google News search reaches back this far at most (a long holiday weekend fits)


def news_window(now, since):
    """(Google News 'when:Nd' operator, hours to keep) covering every headline published since ``since``: one day when
    ``since`` is unknown or recent; on a weekend or holiday the search reaches back to the cut-off (at most a week).
    The headline searches take it as ``window`` only on such a closed day, and then also drop the evening wrap-ups of
    the session before (``earlier_session_report``), which the wider window would otherwise bring in."""
    if since is None: return "when:1d", 24
    hours = max(24, int(-(-(now - since).total_seconds() // 3600)) + 1)
    hours = min(hours, 24 * MAX_NEWS_DAYS)
    return f"when:{-(-hours // 24)}d", hours


def next_session_after(day, now, *, nse_fetch=None):
    """The first NSE trading day after ``day`` (NSE holiday list; without it, the next weekday)."""
    import nse_live
    try:
        nxt = nse_live.next_trading_day(nse_live.holiday_calendar(fetcher=nse_fetch or nse_live.fetch, clock=lambda: now), day)
        if nxt: return nxt
    except ERRORS:
        pass
    probe = day + timedelta(days=1)
    while probe.weekday() >= 5: probe += timedelta(days=1)
    return probe


def closed_banner_info(now, last_session, *, nse_fetch=None):
    """{date, reason, next_trading_day, last_session} when NSE is closed today (a weekend or a listed holiday), else
    None. Without NSE's holiday list only a weekend is recognised."""
    import nse_live
    try:
        s = nse_live.calendar_summary(nse_live.holiday_calendar(fetcher=nse_fetch or nse_live.fetch, clock=lambda: now), now.date())
        if s.get("today_is_trading_day") is not False: return None
        reason, nxt = s.get("today_holiday") or ("weekend" if now.weekday() >= 5 else "not a listed trading day"), s.get("next_trading_day")
    except ERRORS:
        if now.weekday() < 5: return None
        reason, nxt = "weekend", None
    return {"date": now.date().isoformat(), "reason": reason, "next_trading_day": nxt, "last_session": str(last_session)[:10] if last_session else None}


def closed_banner(info):
    """'**Market closed today (Sat 21 Nov 2026: weekend) — figures are from the last session, Fri 20 Nov 2026.** Next
    trading day: Mon 23 Nov 2026.' (daily.py's closed-market lead, shared by every market report)."""
    if not info: return None
    text = f"**Market closed today ({_day(info['date'])}: {info['reason']})"
    if info.get("last_session"): text += f" — figures are from the last session, {_day(info['last_session'])}"
    text += ".**"
    if info.get("next_trading_day"): text += f" Next trading day: {_day(info['next_trading_day'])}."
    return text


def quiet_since(day, now, *, nse_fetch=None):
    """True when no NSE session has opened since ``day`` closed (a weekend, a holiday, or the night before the next
    open): headlines since the session before ``day`` still explain ``day``'s move."""
    if day >= now.date(): return False
    nxt = next_session_after(day, now, nse_fetch=nse_fetch)
    return nxt > now.date() or (nxt == now.date() and now < session_open(nxt))


def collect(session=None, *, nse_fetch=None, flow_fetch=None, yahoo_fetch=None, clock=instant, news=True, news_fetch=None):
    import closing_context, nse_live
    nse_fetch = nse_fetch or nse_live.fetch
    now = clock(); notes, coverage = [], []
    result = {"schema": "market_movers_v1", "as_of": now.isoformat(), "submitted": False, "research_only": True}
    snap = None
    if session is None:
        try:
            snap = nse_live.live_indices(fetcher=nse_fetch, clock=lambda: now)
        except ERRORS:
            coverage.append({"component": "nse_live", "status": "FAILED"})
    target = date.fromisoformat(session) if session else None
    live = snap is not None and instant(snap["as_of"]).date() == now.date() and target is None
    if target is None and not live:
        if snap is None: raise Block("MOVERS: NSE live data unavailable; pass --session YYYY-MM-DD for a finished session")
        target = instant(snap["as_of"]).date()  # before the open NSE's snapshot still carries the last session's close
    if target is not None and target > now.date(): raise Block("MOVERS: future session")
    result["mode"] = "live" if live else "session"
    # ---- index direction, sectors and VIX
    if live:
        rows = snap["indices"]; nifty = rows[INDEX]
        result["session"] = now.date().isoformat()
        result["previous_session"] = nifty["previous_day"] or nse_live.previous_session(now.date(), fetcher=nse_fetch, clock=lambda: now).isoformat()
        result["at"] = snap["as_of"]; result["source"] = snap["page"]
        levels = {n: {"name": label(n), "value": r["last"], "change": r["change"], "change_pct": r["change_pct"], "previous_close": r["previous_close"],
                      "high": r["high"], "low": r["low"]} for n, r in rows.items()}
        if nifty.get("advances") is not None:
            result["nifty_breadth"] = {"advances": nifty["advances"], "declines": nifty["declines"], "unchanged": nifty["unchanged"], "source": "NSE live"}
        if snap.get("market_breadth"):  # NSE's top-level totals add up every index's advances/declines: not a count of securities
            result["index_breadth_totals"] = {**snap["market_breadth"], "scope": "sum of advances/declines across NSE's index list (not a count of securities)"}
        # After 15:30 NSE's snapshot holds the day's closing values: the report says "closed", not "is down ... live".
        result["closed"] = instant(snap["as_of"]) >= session_close(result["session"]) and now >= session_close(result["session"])
        import daily
        broad = daily.broader_market(snap)
        if broad: result["broader_market"] = broad
    else:
        closes = nse_live.index_closes(target, fetcher=nse_fetch)
        result["session"] = target.isoformat(); result["at"] = None
        result["source"] = nse_live.INDEX_CLOSE.format(target.strftime("%d%m%Y"))
        levels = {n: {"name": label(n), "value": r["close"], "change": r["change"], "change_pct": r["change_pct"],
                      "previous_close": round(r["close"] - r["change"], 2), "high": r["high"], "low": r["low"]} for n, r in closes.items()}
        import daily
        broad = {n: {"last": closes[n]["close"], "change": closes[n]["change"], "change_pct": closes[n]["change_pct"]} for n in daily.BROADER if n in closes}
        if snap is not None and instant(snap["as_of"]).date() == target and daily.broader_market(snap):
            result["broader_market"] = daily.broader_market(snap)  # NSE's own closing snapshot, with advances/declines
        elif broad: result["broader_market"] = {"as_of": target.isoformat(), "source": result["source"], "indices": broad}
    if INDEX not in levels: raise Block("MOVERS: Nifty 50 level unavailable")
    n50 = levels[INDEX]
    result["direction"] = {**n50, "word": word(n50["change"])}
    result["indices"] = [levels[n] for n in ("NIFTY BANK", "NIFTY MIDCAP 100", "NIFTY SMALLCAP 100") if n in levels]
    fpi_job = _background(lambda: nse_live.fpi_month(fetcher=nse_fetch, clock=lambda: now))
    # Mood context for every summary: Nifty 50 valuation against its past year and the 1-month move (NSE daily files),
    # India VIX against its 52-week range; started now so they download alongside the constituent work.
    value_job = _background(lambda: valuation_context(target or now.date(), nse_fetch=nse_fetch, now=now))
    vix_job = _background(lambda: vix_year_range(snap, yahoo_fetch=yahoo_fetch, live=nse_live.is_live(nse_fetch)))
    # A weekend or holiday after the latest session: the report says the market is closed today and searches the news
    # since the session before it, as on the evening of that session.
    quiet = not live and quiet_since(target, now, nse_fetch=nse_fetch)
    if quiet:
        result["previous_session"] = nse_live.previous_session(target, fetcher=nse_fetch, clock=lambda: now).isoformat()
        result["market_closed"] = closed_banner_info(now, target, nse_fetch=nse_fetch)
    try:
        q = nse_live.yahoo_quote("SENSEX", fetcher=yahoo_fetch, clock=lambda: now)
        want = result.get("previous_session")  # Yahoo's change must be against the same previous session as NSE's
        if q["as_of"][:10] == result["session"] and (not want or not q.get("previous_day") or q["previous_day"] == want):
            result["indices"].insert(0, {"name": "Sensex (Yahoo, unofficial)", "value": q["last"], "change": q["change"], "change_pct": q["change_pct"],
                                         "at": q["as_of"], "previous_day": q.get("previous_day")})
        elif q["as_of"][:10] == result["session"]:
            notes.append(f"Sensex row left out: Yahoo's previous close was dated {q.get('previous_day')}, not the {want} session.")
    except ERRORS:
        coverage.append({"component": "sensex", "status": "FAILED"})
    result["sectors"] = sorted((levels[n] for n in SECTORS if n in levels), key=lambda r: -r["change_pct"])
    if "INDIA VIX" in levels: result["vix"] = levels["INDIA VIX"]
    if live and result.get("closed"):
        # India VIX's official close (NSE daily index report, published after the close) can differ from the 15:30 snapshot.
        try:
            v = nse_live.index_closes(now.date(), fetcher=nse_fetch).get("INDIA VIX")
            if v:
                result["vix"] = {"name": "India VIX", "value": v["close"], "change": v["change"], "change_pct": v["change_pct"],
                                 "previous_close": round(v["close"] - v["change"], 4), "high": v["high"], "low": v["low"],
                                 "basis": "NSE daily index report (official close)"}
        except ERRORS:
            pass  # not published yet: the 15:30 snapshot value stands
    # ---- constituents: official previous closes (bhavcopy), current prices, estimated weights
    previous_day = date.fromisoformat(result["previous_session"]) if live else None
    stage = "constituent list"
    try:
        members = constituents(fetcher=nse_fetch)
        symbols = [m["symbol"] for m in members]
        if live:
            stage = "previous-day bhavcopy"
            prev = bhavcopy(previous_day, fetcher=nse_fetch)["prices"]
            stage = "stock prices"
            charts = yahoo_charts([s + ".NS" for s in symbols] + ["^NSEI"], fetcher=yahoo_fetch)
            changes = live_changes(members, prev, charts)
            prev_closes = {s + ".NS": prev[s]["close"] for s in symbols if s in prev}
            price_basis = "Yahoo last price vs NSE official previous close"
        else:
            stage = "session bhavcopy"
            day_prices = bhavcopy(target, fetcher=nse_fetch)["prices"]
            stage = "stock prices"
            changes = {m["symbol"]: {"symbol": m["symbol"], "name": m["name"], "industry": m["industry"], "price": day_prices[m["symbol"]]["close"],
                                     "previous_close": day_prices[m["symbol"]]["previous_close"], "change_pct": round(day_prices[m["symbol"]]["change_pct"], 2)}
                       for m in members if m["symbol"] in day_prices}
            # Keyed to the session, not the calendar day: on a weekend or holiday the charts kept after that session's
            # close are read back instead of 51 fresh one-month downloads racing the time budget (Sat 21 Nov 2026
            # lost the contributions that way).
            chart_now = now if now.date() == target else datetime(target.year, target.month, target.day, 23, 59, tzinfo=IST)
            charts = yahoo_charts([s + ".NS" for s in symbols] + ["^NSEI"], fetcher=yahoo_fetch, today=target, now=chart_now)
            prev_closes = {s + ".NS": changes[s]["previous_close"] for s in changes}
            price_basis = "NSE bhavcopy close vs previous close"
        missing = sorted(set(symbols) - set(changes))
        ranked = sorted(changes.values(), key=lambda r: -r["change_pct"])
        result["gainers"] = [r for r in ranked if r["change_pct"] > 0][:5]
        result["losers"] = [r for r in reversed(ranked) if r["change_pct"] < 0][:5]
        if "nifty_breadth" not in result:
            result["nifty_breadth"] = {"advances": sum(r["change_pct"] > 0 for r in ranked), "declines": sum(r["change_pct"] < 0 for r in ranked),
                                       "unchanged": sum(r["change_pct"] == 0 for r in ranked), "source": "NSE bhavcopy"}
        result["constituents"] = {"observed": len(changes), "expected": len(symbols), "missing": missing, "price_basis": price_basis,
                                  "membership": "Current NSE Nifty 50 list (ind_nifty50list.csv); membership on older dates not verified"}
        try:
            # Check the estimate against the index at the same moment as the stock prices (NSE's snapshot can be minutes older).
            result["contributors"] = contributor_block(charts, "^NSEI", changes, prev_closes, n50["previous_close"], live=live, actual=n50["change"],
                                                       industries={m["symbol"]: m["industry"] for m in members},
                                                       today=date.fromisoformat(result["session"]), previous_day=result.get("previous_session"))
        except ERRORS:
            coverage.append({"component": "contributors", "status": "FAILED"})
            notes.append("Point contributions unavailable this run (constituent charts incomplete); gainers/losers are by % change only.")
    except ERRORS:
        coverage.append({"component": stage, "status": "FAILED"})
    if "contributors" not in result:
        failed = [x["component"] for x in coverage if x["status"] == "FAILED" and x["component"] in
                  ("constituent list", "previous-day bhavcopy", "session bhavcopy", "stock prices", "contributors")]
        result["drivers_missing"] = {"stage": failed[0] if failed else "contributors",
                                     "reason": DRIVER_GAP_REASON.get(failed[0] if failed else "contributors")}
    # ---- institutional flows: NSE serves only the latest day, so a past session gets them only when the dates match
    try:
        raw_flows = flow_fetch(closing_context.FLOWS) if flow_fetch else nse_live.bounded_call(closing_context.fetch, nse_live.CALL_BUDGET, closing_context.FLOWS)
        f = closing_context.flows(raw_flows, now)
        flows = {"date": f["trading_date"], "fii_net_crore": f["values"]["FII/FPI"]["net_crore"], "dii_net_crore": f["values"]["DII"]["net_crore"],
                 "source": "https://www.nseindia.com/reports/fii-dii", "provisional": True}
        if live or flows["date"] == result["session"]:
            result["flows"] = flows
        else:
            result["flows_unavailable"] = {"session": result["session"], "latest": flows["date"]}
    except ERRORS:
        coverage.append({"component": "flows", "status": "FAILED"})
    # The time budget guards against a slow NSDL site; an injected source (tests, replays) is waited for, so the
    # result never depends on how busy the machine is.
    fpi = _joined(fpi_job, FPI_BUDGET if nse_live.is_live(nse_fetch) else None)
    if fpi is not None:
        result["fpi"] = fpi_summary(fpi, None if live else result["session"])
    # All NSE shares from the session's bhavcopy (a finished session, or tonight's once NSE has published it).
    breadth_day = target if not live else (now.date() if result.get("closed") else None)
    if breadth_day is not None:
        try:
            import closing_context
            prices = bhavcopy(breadth_day, fetcher=nse_fetch)["prices"]
            b = closing_context.breadth(prices)
            result["all_breadth"] = {"session": breadth_day.isoformat(), **{k: b[k] for k in ("advancing", "declining", "unchanged")},
                                     "source": nse_live.BHAVCOPY.format(breadth_day.strftime("%d%m%Y"))}
            n500_row = ((result.get("broader_market") or {}).get("indices") or {}).get("NIFTY 500") or {}
            if not live and n500_row.get("advances") is None:  # Nifty 500 advances/declines: its members in the same bhavcopy
                n500 = nse_live.index_members("nifty500", fetcher=nse_fetch, today=now.date(), low=450, high=600)["members"]
                moves = [prices[m["symbol"]]["direction"] for m in n500 if m["symbol"] in prices]
                if len(moves) >= 450:
                    broad = result.setdefault("broader_market", {"as_of": target.isoformat(), "source": result["source"], "indices": {}})
                    broad["indices"].setdefault("NIFTY 500", {}).update(advances=moves.count(1), declines=moves.count(-1), unchanged=moves.count(0))
        except ERRORS:
            pass  # not published yet, or NSE refused: the live Nifty 500 count (or the gap) stands
    live_fetch = nse_live.is_live(nse_fetch)
    value = _joined(value_job, MOOD_BUDGET + 2 if live_fetch else None)
    if value: result["valuation"] = value
    else: result["mood_missing"] = "NSE's daily index files were unavailable or slow"
    vix_year = _joined(vix_job, 6.0 if live_fetch else None)
    if vix_year: result["vix_year"] = vix_year
    if live:
        try:
            result["previous_direction"] = previous_direction(result["previous_session"], nse_fetch=nse_fetch)
        except ERRORS:
            coverage.append({"component": "previous_session", "status": "FAILED"})
    if news:
        since = session_close(result.get("previous_session")) if live or quiet else None  # a past session: no reliable cut-off
        window = news_window(now, since) if quiet else None
        picks, failed = market_headlines(now, news_fetch=news_fetch, terms=mover_terms(result), since=since, session=result["session"],
                                         change=result["direction"]["change"], window=window)
        result["headlines"] = picks
        if failed and not picks: coverage.append({"component": "headlines", "status": "FAILED"})
        c = result.get("contributors")
        if c:
            main = c["negative"] if result["direction"]["change"] < 0 else c["positive"]
            result["stock_headlines"] = stock_headlines(main[:2], now, news_fetch=news_fetch, since=since, session=result["session"], window=window)
        # The sectors leading the move get one cause headline each when none of the headlines shown names them.
        shown = (result["headlines"] or [])[:3] + (result.get("stock_headlines") or [])
        result["sector_headlines"] = sector_headlines(result, shown, now, news_fetch=news_fetch, since=since, session=result["session"], window=window)
        if failed and not picks and not result.get("stock_headlines") and not result["sector_headlines"]:
            result["headlines_missing"] = "the news search failed this run; rerun, or search the web with the date"
        result["headline_filter"] = ("Kept only headlines naming a moving stock or sector, or a macro/flow driver, published after "
                                     + (f"the {since:%d %b} close" if since else "the previous close") + "; older stock news, corporate "
                                     "housekeeping (ESOP grants, AGM/EGM, board-meeting notices) and multi-day stories against today's move "
                                     "dropped; headlines that give a cause are listed first.")
    else:
        result["headlines_missing"] = "headline search skipped (--no-news)"
    result["coverage"] = coverage; result["notes"] = notes
    return result


# ---------------------------------------------------------------- rendering

def _day(text):
    d = date.fromisoformat(str(text)[:10]); return f"{d:%a} {d.day} {d:%b} {d.year}"


def _pts(v): return f"{v:,.2f}"


def _cr(v): return f"{'+' if v >= 0 else '-'}₹{abs(v):,.2f}"


def _side(v): return "net sellers" if v < 0 else "net buyers"


def _chg(r): return f"{r['change']:+,.2f} ({r['change_pct']:+.2f}%)"


def _moved(change, pct):
    """After 'up'/'down': '199.45 points (-0.79%)' - the word carries the direction, so no second sign on the points."""
    return f"{abs(change):,.2f} points ({pct:+.2f}%)" if change else f"({pct:+.2f}%)"


def lead(result, premise=None):
    d = result["direction"]
    when = f"at {result['at'][11:16]} IST (NSE live)" if result["mode"] == "live" else f"at the close on {_day(result['session'])} (NSE)"
    sentence = f"Nifty 50 is **{d['word']}** {_moved(d['change'], d['change_pct'])} at {_pts(d['value'])} {when}."
    if result["mode"] == "session":
        sentence = f"Nifty 50 closed **{d['word']}** {_moved(d['change'], d['change_pct'])} at {_pts(d['value'])} on {_day(result['session'])} (NSE)."
    elif result.get("closed"):
        sentence = (f"Nifty 50 closed **{d['word']}** {_moved(d['change'], d['change_pct'])} at {_pts(d['value'])} today, "
                    f"{_day(result['session'])} (NSE closing level, {result['at'][11:16]} IST).")
    if premise and premise != d["word"]:
        verb = "is actually" if result["mode"] == "live" and not result.get("closed") else "actually closed"
        sentence = f"Correction: the market {verb} **{d['word']}**, not {premise}. " + sentence
    return sentence


def _contrib_list(rows):
    return ", ".join(f"{r['symbol']} {r['points']:+.1f} pts ({r['change_pct']:+.2f}%)" for r in rows)


def _stamp(t):
    return f"{int(t[8:10])} {date.fromisoformat(t[:10]):%b} {t[11:16]} IST"


def _link(h):
    title = h["headline"].replace("[", "(").replace("]", ")")
    return f"[{title}]({h['url']})"


def previous_line(result, premise=None):
    """'If you meant the last session' when the user's premise is wrong today."""
    p = result.get("previous_direction")
    if not premise or premise == result["direction"]["word"] or not p: return None
    return f"If you meant the last session: Nifty 50 closed **{p['word']}** {_moved(p['change'], p['change_pct'])} at {_pts(p['close'])} on {_day(p['session'])}."


def weights_basis(c):
    """'weights are estimated' plus, when anchored, the factsheet date they are anchored to."""
    a = (c.get("fit") or {}).get("anchor")
    return (f"weights are estimated, anchored to the NSE Indices factsheet of {date.fromisoformat(a['as_of']):%d %b %Y}" if a
            else "weights are estimated")


def check_line(result, c):
    """The contribution check, with the time and source of the index figure it is compared against."""
    if c.get("actual_at"):
        ch = result["direction"]["change"]
        nse = (f" (the {abs(ch):,.2f}-point {'fall' if ch < 0 else 'rise'} above is NSE at {result['at'][11:16]} IST)"
               if result.get("at") and ch else "")
        return (f"Check: these estimates add up to {c['estimated_total_points']:+.1f} points vs Nifty's {c['actual_points']:+.1f} "
                f"on Yahoo at {c['actual_at'][11:16]} IST, the time of the stock prices{nse}; {weights_basis(c)}.")
    return f"Check: these estimates add up to {c['estimated_total_points']:+.1f} points vs the official {c['actual_points']:+.1f} ({weights_basis(c)})."


def headline_lines(result, limit=3, links=True):
    """Dated headlines (market-wide, then per top mover), labelled as unverified possible causes."""
    text = _link if links else (lambda h: '"' + h["headline"] + '"')
    out = []
    for h in (result.get("headlines") or [])[:limit]:
        out.append(f"{text(h)} ({h.get('publisher') or h.get('source', 'news')}, {_stamp(h['published_at'])})")
    for h in result.get("stock_headlines") or []:
        out.append(f"{h['symbol']}: {text(h)} ({h.get('publisher') or 'Google News'}, {_stamp(h['published_at'])})")
    for h in result.get("sector_headlines") or []:
        out.append(f"{h['sector']}: {text(h)} ({h.get('publisher') or 'Google News'}, {_stamp(h['published_at'])})")
    return out


def broader_text(result):
    """'Broader market (NSE, ...): Nifty Midcap 150 ..., Nifty Smallcap 250 ...; Nifty 500 stocks N up, M down', or None."""
    if not result.get("broader_market"): return None
    import daily
    live = result["mode"] == "live" and not result.get("closed")
    return daily.broader_line({"broader_market": result["broader_market"], "phase": "intraday" if live else "close"})


def flows_note(result):
    if result.get("mode") == "session": return "NSE's figure for that session"
    return "today's are published this evening" if result.get("closed") else "today's flows come after the close"


DRIVER_GAP_REASON = {"constituent list": "NSE's Nifty 50 constituent list could not be downloaded",
                     "previous-day bhavcopy": "NSE's previous-day bhavcopy (official previous closes) could not be downloaded after retries",
                     "session bhavcopy": "NSE's bhavcopy for that session could not be downloaded",
                     "stock prices": "constituent prices could not be collected",
                     "contributors": "the constituent charts were incomplete, so weights could not be estimated"}


def driver_gap_line(result):
    """'⚠ stock contributions not collected — rerun' whenever the stock-level drivers are missing from this run."""
    gap = result.get("drivers_missing")
    if not gap: return None
    return f"⚠ stock contributions not collected — rerun ({gap.get('reason') or gap.get('stage')}; sectors and breadth above are complete)."


FPI_BUDGET = 8.0  # seconds to wait for NSDL's monthly FPI page


def _background(job):
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool, pool.submit(job)
    finally:
        pool.shutdown(wait=False)


def _joined(handle, budget):
    """The background job's result within ``budget`` seconds, or None on failure or timeout (never raises)."""
    _, future = handle
    try:
        return future.result(timeout=budget)
    except Exception:  # noqa: BLE001 - optional context: a failure or timeout is a stated gap
        return None


def fpi_summary(data, session=None):
    """Month-to-date and last-five-report FPI equity flows from NSDL's monthly page; for a past ``session``, the NSDL
    report that carries that session's trades (the first reporting date after it)."""
    rows = data["rows"]
    last5 = rows[-5:]
    out = {"source": data["source"], "basis": data["basis"], "month": rows[0]["reporting_date"][:7], "first": rows[0]["reporting_date"],
           "last": rows[-1]["reporting_date"], "reports": len(rows),
           "mtd_exchange_net_crore": round(sum(r["exchange_net_crore"] for r in rows), 2),
           "mtd_equity_net_crore": round(sum(r["equity_net_crore"] for r in rows), 2),
           "last5_exchange_net_crore": round(sum(r["exchange_net_crore"] for r in last5), 2),
           "last5": [{"reporting_date": r["reporting_date"], "exchange_net_crore": r["exchange_net_crore"]} for r in last5],
           "buying_reports": sum(r["exchange_net_crore"] > 0 for r in rows), "selling_reports": sum(r["exchange_net_crore"] < 0 for r in rows)}
    if session:
        after = [r for r in rows if r["reporting_date"] > session]
        if after: out["session_report"] = {**after[0], "trades_of": session}
    return out


def _lakh(v):
    """Indian digit grouping: 100365.32 -> '1,00,365.32'."""
    sign = "-" if v < 0 else "+"
    whole, frac = f"{abs(v):.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:]); head = head[:-2]
    if head: groups.insert(0, head)
    return f"{sign}₹{','.join(groups + [tail]) if groups else tail}.{frac}"


def fpi_line(result):
    p = result.get("fpi")
    if not p: return None
    first, last = date.fromisoformat(p["first"]), date.fromisoformat(p["last"])
    head = (f"FPI equity via the exchanges, month to date (NSDL, {p['reports']} reports {first.day}-{last.day} {last:%b}; each covers the "
            f"previous trading day): net {_lakh(p['mtd_exchange_net_crore'])} cr; last {len(p['last5'])} reports {_lakh(p['last5_exchange_net_crore'])} cr, "
            f"net buying in {sum(r['exchange_net_crore'] > 0 for r in p['last5'])} of {len(p['last5'])}")
    s = p.get("session_report")
    if s and result.get("mode") == "session":
        head += f"; the {date.fromisoformat(s['reporting_date']):%d %b} report (trades of {date.fromisoformat(s['trades_of']):%d %b}): {_lakh(s['exchange_net_crore'])} cr"
    return head + "."


def fpi_gap_line(result):
    """The NSDL month-to-date line, or why it is missing: it is part of every summary (market mood questions)."""
    return fpi_line(result) or "FPI equity month to date (NSDL): not collected this run (NSDL's page failed or was slow) - a gap."


# ---------------------------------------------------------------- mood context: valuation, volatility, 1-month move, breadth

MOOD_BUDGET = 10.0  # seconds for the valuation files (cached on disk after the first run)


def _month_back(day, months):
    import calendar
    y, m = day.year, day.month - months
    while m <= 0: m += 12; y -= 1
    return date(y, m, min(day.day, calendar.monthrange(y, m)[1]))


def _month_end(day):
    import calendar
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def valuation_context(end_day, *, nse_fetch, now, budget=None):
    """Nifty 50 P/E, P/B and dividend yield from NSE's ind_close_all file of the last session on or before ``end_day``,
    against their range over the past year (the file of each month's last session, kept on disk after the first run),
    plus the Nifty 50 close one month earlier. Files that are missing or late are skipped; raises Block when the end
    file itself is unavailable."""
    import nse_live
    try: cal = nse_live.holiday_calendar(fetcher=nse_fetch, clock=lambda: now)
    except ERRORS: cal = None
    targets = {"end": end_day, "month": _month_back(end_day, 1)}
    targets.update({f"m{k}": _month_end(_month_back(end_day, k)) for k in range(1, 13)})
    pool = ThreadPoolExecutor(max_workers=6)
    got = {}
    try:
        jobs = {pool.submit(closes_on_or_before, d, fetcher=nse_fetch, cal=cal, today=now.date(), back=7): k for k, d in targets.items()}
        done, _ = wait(jobs, timeout=MOOD_BUDGET if budget is None else budget)
        for f in done:
            if f.exception() is None: got[jobs[f]] = f.result()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if "end" not in got or INDEX not in got["end"][1]: raise Block("MOVERS: NSE index-close file for the valuation unavailable")
    end_session, rows = got["end"]
    n = rows[INDEX]
    out = {"session": end_session.isoformat(), "close": n["close"], "source": nse_live.INDEX_CLOSE.format(end_session.strftime("%d%m%Y"))}
    for key in ("pe", "pb", "div_yield"):
        if n.get(key) is not None: out[key] = n[key]
    points = {d: r[INDEX] for d, r in got.values() if INDEX in r and d <= end_session}
    if len(points) >= 6:
        rng = {}
        for key in ("pe", "pb"):
            vals = [(v[key], d) for d, v in points.items() if v.get(key) is not None]
            if len(vals) >= 6:
                lo, hi = min(vals), max(vals)
                rng[key] = {"low": lo[0], "low_session": lo[1].isoformat(), "high": hi[0], "high_session": hi[1].isoformat()}
        if rng:
            out["range"] = rng
            out["range_basis"] = (f"NSE ind_close_all files of each month's last session over the past year plus the latest ({len(points)} files, "
                                  f"{min(points):%b %Y} to {max(points):%d %b %Y})")
    if "month" in got and INDEX in got["month"][1] and got["month"][0] < end_session:
        out["month_ago"] = {"session": got["month"][0].isoformat(), "close": got["month"][1][INDEX]["close"]}
    return out


VIX_YEAR_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EINDIAVIX?range=1y&interval=1d"


def vix_year_range(snap, *, yahoo_fetch=None, live=True):
    """India VIX 52-week high/low: NSE's own figures from the allIndices snapshot, else Yahoo's one-year daily closes
    (unofficial; only for the real network fetch or an injected Yahoo fetcher). None when neither answers."""
    import nse_live
    row = ((snap or {}).get("indices") or {}).get("INDIA VIX") or {}
    if row.get("year_high") and row.get("year_low"):
        return {"high": row["year_high"], "low": row["year_low"], "basis": "NSE 52-week high/low"}
    if yahoo_fetch is None and not live: return None
    data = json.loads((yahoo_fetch or nse_live.yahoo_get)(VIX_YEAR_URL))["chart"]["result"][0]
    if data["meta"].get("symbol") != "^INDIAVIX": raise Block("YAHOO: wrong instrument")
    closes = [float(c) for c in data["indicators"]["quote"][0]["close"] if c]
    if len(closes) < 150: raise Block("YAHOO: India VIX history too short")
    return {"high": max(closes), "low": min(closes), "basis": "Yahoo daily closes over one year (unofficial)"}


def mood_parts(result):
    """The context lines for a market-mood or 'overheated' question: valuation against its own past year, the 1-month
    move, India VIX against its 52-week range. Context, not a forecast."""
    out = []
    v = result.get("valuation")
    d = result.get("direction") or {}
    if v and v.get("pe") is not None:
        rng = v.get("range") or {}
        def with_range(key, name):
            r = rng.get(key)
            text = f"{name} {v[key]:.2f}"
            if r:
                where = ", the top of" if v[key] >= r["high"] else ", the bottom of" if v[key] <= r["low"] else " vs"
                text += (f"{where} its past-year range {r['low']:.2f} ({date.fromisoformat(r['low_session']):%b %Y}) to "
                         f"{r['high']:.2f} ({date.fromisoformat(r['high_session']):%b %Y})")
            return text
        parts = [with_range("pe", "P/E")] + ([with_range("pb", "P/B")] if v.get("pb") is not None else [])
        if v.get("div_yield") is not None: parts.append(f"dividend yield {v['div_yield']:.2f}%")
        out.append(f"Nifty 50 valuation (NSE, {_day(v['session'])[:-5]} close): " + "; ".join(parts)
                   + (" (ranges from NSE's month-end files)" if rng else "; past-year range not collected this run") + ".")
    if v and v.get("month_ago") and d.get("value"):
        m = v["month_ago"]
        pct = (d["value"] / m["close"] - 1) * 100
        now_text = (f"{_pts(d['value'])} now" if result.get("mode") == "live" and not result.get("closed") else f"{_pts(d['value'])} at the {_day(result['session'])} close")
        out.append(f"Nifty 50 over one month: {pct:+.2f}% ({_pts(m['close'])} at the {_day(m['session'])} close to {now_text}).")
    elif result.get("mood_missing"):
        out.append(f"Valuation and 1-month change not collected this run ({result['mood_missing']}) - a gap.")
    vx, vr = result.get("vix"), result.get("vix_year")
    if vx and vr:
        span = vr["high"] - vr["low"]
        pos = (vx["value"] - vr["low"]) / span * 100 if span > 0 else None
        out.append(f"India VIX {_pts(vx['value'])} vs its 52-week range {_pts(vr['low'])}-{_pts(vr['high'])} ({vr['basis']})"
                   + (f": {pos:.0f}% of the way up the range." if pos is not None else "."))
    elif vx:
        out.append(f"India VIX {_pts(vx['value'])}; its 52-week range was not collected this run - a gap.")
    return out


def all_breadth_line(result):
    """All NSE equity shares from the bhavcopy (advances/declines), or when they come."""
    b = result.get("all_breadth")
    if b:
        return (f"All NSE shares ({_day(b['session'])[:-5]} bhavcopy, EQ series): {b['advancing']:,} up, {b['declining']:,} down, "
                f"{b['unchanged']:,} unchanged.")
    if result.get("mode") == "live":
        return "All NSE shares: the full advance/decline count comes with NSE's evening bhavcopy; the Nifty 500 count above is NSE's live figure."
    return None


def flow_premise_line(result, premise):
    """Checks a user's 'FIIs are selling' / 'FIIs are buying' premise against the latest NSE day and NSDL's month to date."""
    if not premise: return None
    want = {"selling": "sellers", "buying": "buyers"}[premise]
    f, p = result.get("flows"), result.get("fpi")
    if not f and not p:
        return f"Premise check (FIIs net {want}?): FII flow data could not be collected this run - unverified, not confirmed."
    sides = []
    if f: sides.append(("sellers" if f["fii_net_crore"] < 0 else "buyers", f"FII/FPI net {_cr(f['fii_net_crore'])} cr on {date.fromisoformat(f['date']):%d %b} (NSE provisional)"))
    if p: sides.append(("sellers" if p["mtd_exchange_net_crore"] < 0 else "buyers",
                        f"FPI equity {_lakh(p['mtd_exchange_net_crore'])} cr month to date (NSDL, to the {date.fromisoformat(p['last']):%d %b} report)"))
    evidence = "; ".join(t for _, t in sides)
    found = {s for s, _ in sides}
    if found == {want}: return f"Premise check: yes, FIIs were net {want} - {evidence}."
    if want not in found:
        other = found.pop()
        head = f"Correction: FIIs were net **{other}**, not {want} - {evidence}."
        if other != "buyers": return head
        nifty = result.get("direction")
        if nifty and (nifty.get("change") or 0) > 0:
            return head + " The market rose with them; the weak spots are in the sectors and stocks below."
        dii = f.get("dii_net_crore") if f else None
        if dii is not None and dii < 0:
            return head + f" Domestic institutions were the net sellers (DII net {_cr(dii)} cr); the sectors and stocks below show where the selling hit."
        return head + " The selling came from elsewhere - see the sectors and stocks below."
    return f"Premise check: mixed - {evidence}."


def summary(result, premise=None, flow_premise=None):
    lines = banner_lines(result) + [lead(result, premise)]
    if flow_premise_line(result, flow_premise): lines.append("- " + flow_premise_line(result, flow_premise))
    if previous_line(result, premise): lines.append("- " + previous_line(result, premise))
    b = result.get("nifty_breadth")
    if b: lines.append(f"- Breadth: {b['advances']} of 50 Nifty stocks up, {b['declines']} down.")
    if broader_text(result): lines.append("- " + broader_text(result))
    if result.get("all_breadth"): lines.append("- " + all_breadth_line(result))
    s = result.get("sectors") or []
    if s:
        up = [x for x in s if x["change_pct"] > 0][:3]; down = [x for x in reversed(s) if x["change_pct"] < 0][:3]
        parts = []
        if up: parts.append("strongest " + ", ".join(f"{x['name']} {x['change_pct']:+.2f}%" for x in up))
        if down: parts.append("weakest " + ", ".join(f"{x['name']} {x['change_pct']:+.2f}%" for x in down))
        lines.append("- Sectors: " + "; ".join(parts) + ".")
    c = result.get("contributors")
    if c:
        main = c["negative"] if result["direction"]["change"] < 0 else c["positive"]
        other = c["positive"] if result["direction"]["change"] < 0 else c["negative"]
        lines.append(f"- Biggest {'drags' if result['direction']['change'] < 0 else 'lifts'} (est. index points): {_contrib_list(main[:4])}.")
        if other: lines.append(f"- Against the move: {_contrib_list(other[:3])}.")
        lines.append("- " + check_line(result, c))
    elif driver_gap_line(result): lines.append("- " + driver_gap_line(result))
    v = result.get("vix")
    if v: lines.append(f"- India VIX {_pts(v['value'])}, {_chg(v)}" + (" (NSE official close)." if v.get("basis") else "."))
    f = result.get("flows")
    if f: lines.append(f"- FII/FPI net {_cr(f['fii_net_crore'])} cr ({_side(f['fii_net_crore'])}), DII net {_cr(f['dii_net_crore'])} cr ({_side(f['dii_net_crore'])}) on {_day(f['date'])} (provisional NSE data; {flows_note(result)}).")
    elif result.get("flows_unavailable"):
        u = result["flows_unavailable"]
        lines.append(f"- FII/DII for {_day(u['session'])}: not available - NSE's endpoint serves only the latest day ({_day(u['latest'])}).")
    lines.append("- " + fpi_gap_line(result))
    mood = mood_parts(result)
    if mood: lines.append("- Mood context (not a forecast): " + " ".join(mood))
    news = headline_lines(result, links=False)
    if news:
        sources = sorted({h.get("source", "") for h in (result.get("headlines") or []) + (result.get("stock_headlines") or [])
                          + (result.get("sector_headlines") or [])} - {""})
        lines.append(f"- Headlines ({', '.join(sources)}; unverified possible causes, not confirmed reasons):")
        lines += ["  - " + x for x in news]
    elif headlines_gap_line(result):
        lines.append("- " + headlines_gap_line(result))
    elif "headlines" in result:
        lines.append(f"- Headlines: none since {news_cutoff_text(result)} names a moving stock or sector or a macro driver - a gap, not an all-clear.")
    lines.append("- Research only: this explains the move from price data; it does not predict the next one. No buy/sell advice.")
    return "\n".join(lines) + "\n"


def news_cutoff_text(result):
    p = result.get("previous_session")
    return f"the {date.fromisoformat(p):%d %b} close" if p else "the previous close"


def markdown(result, premise=None, flow_premise=None):
    when = f"{_day(result['session'])}, {result['at'][11:16]} IST" if result["mode"] == "live" and not result.get("closed") else f"{_day(result['session'])} close"
    lines = [f"# Market movers - {when}", ""] + banner_lines(result) + [lead(result, premise)] + ([previous_line(result, premise)] if previous_line(result, premise) else [])
    if flow_premise_line(result, flow_premise): lines.append(flow_premise_line(result, flow_premise))
    if driver_gap_line(result): lines += ["", driver_gap_line(result)]
    lines.append("")
    others = result.get("indices") or []
    if others:
        lines += ["| Index | Level | Change |", "| --- | ---: | ---: |", f"| Nifty 50 | {_pts(result['direction']['value'])} | {_chg(result['direction'])} |"]
        lines += [f"| {r['name']} | {_pts(r['value'])} | {_chg(r)} |" for r in others]
        v = result.get("vix")
        if v: lines.append(f"| India VIX{' (official close)' if v.get('basis') else ''} | {_pts(v['value'])} | {_chg(v)} |")
        lines.append("")
    b = result.get("nifty_breadth")
    if b:
        lines.append(f"**Breadth:** {b['advances']} Nifty 50 stocks up, {b['declines']} down, {b['unchanged']} unchanged ({b['source']}).")
        if broader_text(result): lines.append(broader_text(result))
        lines.append("")
    c = result.get("contributors")
    if c:
        lines += ["## Index-point contributors (Nifty 50, estimated)", "", "| Stock | Change | Est. weight | Est. points |", "| --- | ---: | ---: | ---: |"]
        for r in c["negative"] + c["positive"] if result["direction"]["change"] < 0 else c["positive"] + c["negative"]:
            lines.append(f"| {r['symbol']} | {r['change_pct']:+.2f}% | {r['weight_pct']:.1f}% | {r['points']:+.1f} |")
        lines += ["", check_line(result, c), ""]
    for key, title in (("gainers", "Top Nifty 50 gainers"), ("losers", "Top Nifty 50 losers")):
        if result.get(key):
            lines.append(f"**{title}:** " + ", ".join(f"{r['symbol']} {r['change_pct']:+.2f}%" for r in result[key]) + ".")
    if result.get("gainers") or result.get("losers"): lines.append("")
    s = result.get("sectors")
    if s:
        lines += ["## Sectors (NSE indices)", "", "| Sector | Change % |", "| --- | ---: |"] + [f"| {x['name']} | {x['change_pct']:+.2f}% |" for x in s] + [""]
    f = result.get("flows")
    if f:
        lines += [f"**FII/DII ({_day(f['date'])}, provisional):** FII/FPI net {_cr(f['fii_net_crore'])} crore ({_side(f['fii_net_crore'])}); DII net {_cr(f['dii_net_crore'])} crore ({_side(f['dii_net_crore'])}). "
                  + ("" if result.get("mode") == "session" else "Flows for today are published this evening." if result.get("closed") else "Flows for today are published after the close."), ""]
    elif result.get("flows_unavailable"):
        u = result["flows_unavailable"]
        lines += [f"**FII/DII ({_day(u['session'])}):** not available - NSE's endpoint serves only the latest day ({_day(u['latest'])}).", ""]
    lines += [fpi_gap_line(result), ""]
    if all_breadth_line(result): lines += [all_breadth_line(result), ""]
    mood = mood_parts(result)
    if mood: lines += ["## Mood context (not a forecast)", ""] + ["- " + x for x in mood] + [""]
    news = headline_lines(result, limit=5)
    if news:
        lines += ["## Headlines to check (ET Markets / Google News RSS; unverified possible causes, not verified reasons)", ""]
        lines += ["- " + x for x in news] + [""]
    elif headlines_gap_line(result):
        lines += [headlines_gap_line(result), ""]
    lines += ["## Data notes", ""]
    lines.append(f"- Levels: {'NSE live index data (https://www.nseindia.com/market-data/live-market-indices)' if result['mode'] == 'live' else 'NSE daily index report ' + result['source']}.")
    cons = result.get("constituents")
    if cons:
        lines.append(f"- Stocks: {cons['observed']}/{cons['expected']} Nifty 50 constituents; {cons['price_basis']}. {cons['membership']}.")
    if c: lines.append(f"- Method: {c['method']} Fit window {c['fit']['window'][0]} to {c['fit']['window'][1]}, {c['fit']['observations']} bars, R² {c['fit']['r_squared']}.")
    if c and c.get("anchor_line"): lines.append("- " + c["anchor_line"])
    if result.get("headline_filter") and (result.get("headlines") or result.get("stock_headlines")): lines.append("- Headlines: " + result["headline_filter"])
    lines += ["- " + n for n in result.get("notes", [])]
    if result.get("fpi"): lines.append(f"- FPI month to date: {result['fpi']['basis']} ({result['fpi']['source']}).")
    failed = [x["component"] for x in result.get("coverage", []) if x["status"] == "FAILED"]
    if failed: lines.append("- Not collected this run: " + ", ".join(failed) + ".")
    lines.append("- Research only. Explains price moves after the fact; no prediction, no buy/sell call.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- sector drill-down ("Nifty IT aaj kaisa raha?", "why did metal stocks fall?")

# (NSE index name, constituent-list slug as in ind_<slug>list.csv, what people call it, news queries)
SECTOR_SPECS = (
    ("NIFTY IT", "niftyit", r"it|information technology|tech|technology|software|infotech|it services", ("IT stocks", "Nifty IT")),
    ("NIFTY BANK", "niftybank", r"banks?|banking|bank ?nifty|nifty ?bank", ("bank stocks", "Bank Nifty")),
    ("NIFTY PSU BANK", "niftypsubank", r"psu banks?|psu banking|psubank|public sector banks?|psbs?|government banks?|state[- ]run banks?",
     ("PSU bank stocks", "Nifty PSU Bank")),
    ("NIFTY PRIVATE BANK", "nifty_privatebank", r"private banks?|pvt banks?|private sector banks?|privatebank", ("private bank stocks", "Nifty Private Bank")),
    ("NIFTY FINANCIAL SERVICES", "niftyfinance", r"financials?|financial services|fin services|finance|finserv|nbfcs?",
     ("financial stocks", "Nifty Financial Services")),
    ("NIFTY AUTO", "niftyauto", r"autos?|automobiles?|automotive|auto ?mobiles?|car ?makers?|auto ?makers?", ("auto stocks", "Nifty Auto")),
    ("NIFTY PHARMA", "niftypharma", r"pharma|pharmaceuticals?|drug ?makers?", ("pharma stocks", "Nifty Pharma")),
    ("NIFTY HEALTHCARE INDEX", "niftyhealthcare", r"health ?care|hospitals?", ("healthcare stocks", "Nifty Healthcare")),
    ("NIFTY FMCG", "niftyfmcg", r"fmcg|consumer staples|consumer goods", ("FMCG stocks", "Nifty FMCG")),
    ("NIFTY METAL", "niftymetal", r"metals?|metal (?:and|&) mining|mining|steel", ("metal stocks", "Nifty Metal")),
    ("NIFTY REALTY", "niftyrealty", r"realty|real[ -]estate|property|realtors?", ("realty stocks", "Nifty Realty")),
    ("NIFTY ENERGY", "niftyenergy", r"energy|power", ("energy stocks", "Nifty Energy")),
    ("NIFTY OIL & GAS", "niftyoilgas", r"oil (?:and|&) gas|oil ?gas|oil|gas|omcs?", ("oil and gas stocks", "Nifty Oil & Gas")),
    ("NIFTY MEDIA", "niftymedia", r"media|entertainment", ("media stocks", "Nifty Media")),
    ("NIFTY CONSUMER DURABLES", "niftyconsumerdurables", r"consumer durables?|durables|jewell?ery|white goods",
     ("consumer durables stocks", "Nifty Consumer Durables")),
)
_BANK_DRIVERS = r"rbi|repo|deposits?|nims?|net interest margins?|asset quality|npas?|bad loans?|loan growth|credit growth|bond yields?|liquidity"
# What moves each sector besides its own name: a headline naming one of these can explain the sector's move.
SECTOR_DRIVERS = {
    "NIFTY METAL": r"copper|alumini?um|zinc|nickel|iron ore|steel prices?|lme|base metals?|china|commodit\w+|metal prices?|safeguard duty",
    "NIFTY IT": r"rupee|accenture|nasdaq|h-?1b|visa fees?|tech spending|artificial intelligence|\bai\b|us (?:economy|recession|jobs)|deal wins?",
    "NIFTY PHARMA": r"usfda|fda|tariffs?|drug pricing|generics?|warning letter|import alert",
    "NIFTY HEALTHCARE INDEX": r"usfda|fda|tariffs?|drug pricing|generics?|warning letter",
    "NIFTY AUTO": r"auto sales|monthly sales|vehicle sales|\bevs?\b|electric vehicles?|tariffs?|gst",
    "NIFTY BANK": _BANK_DRIVERS, "NIFTY PSU BANK": _BANK_DRIVERS, "NIFTY PRIVATE BANK": _BANK_DRIVERS,
    "NIFTY FINANCIAL SERVICES": _BANK_DRIVERS + r"|nbfcs?|sebi|insurance",
    "NIFTY FMCG": r"rural demand|demand|inflation|monsoon|gst|volume growth|palm oil",
    "NIFTY REALTY": r"home loans?|housing|rate cuts?|repo|property sales|bookings|pre-?sales",
    "NIFTY ENERGY": r"crude|brent|oil prices?|gas prices?|opec|power demand|coal",
    "NIFTY OIL & GAS": r"crude|brent|oil prices?|gas prices?|opec|marketing margins?|refining margins?",
    "NIFTY MEDIA": r"ad revenue|advertising|subscribers?|\bott\b",
    "NIFTY CONSUMER DURABLES": r"gold|silver|festive demand|demand|jewell?ery",
}
# A third search for the commodity behind a sector (Google News rate-limits bursts, so only where it often explains the day).
DRIVER_QUERY = {"NIFTY METAL": "copper price", "NIFTY OIL & GAS": "crude oil price", "NIFTY ENERGY": "crude oil price"}
QUOTE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=1d&interval=1d"
QUOTE_BUDGET = 15.0
SECTOR_NEWS_BUDGET = 12.0
SECTOR_NAMES = "IT, BANK, PSU BANK, PRIVATE BANK, FINANCIAL SERVICES, AUTO, PHARMA, HEALTHCARE, FMCG, METAL, REALTY, ENERGY, OIL & GAS, MEDIA, CONSUMER DURABLES"


def resolve_sector(text):
    """'Nifty IT', 'IT stocks', 'metal stocks', 'PSU banks', 'real estate' -> the NSE sector index and its constituent list."""
    t = re.sub(r"[^a-z0-9& -]+", " ", str(text).lower())
    t = re.sub(r"\b(?:nifty|the|stocks?|shares?|sectors?|index|indices|space|counters?|pack|names|cos|companies)\b", " ", t)
    t = " ".join(t.split())
    for name, slug, words, queries in SECTOR_SPECS:
        if t and re.fullmatch(words, t):
            return {"index": name, "slug": slug, "label": label(name), "queries": queries}
    raise Block(f"MOVERS: unknown sector '{str(text)[:40]}'; choose from {SECTOR_NAMES}")


def stock_quotes(symbols, *, fetcher=None, workers=6, budget=None):
    """{symbol: {price, at}} from Yahoo's chart meta (regularMarketPrice and its trade time) for NSE stocks, fetched in
    parallel within ``budget`` seconds; a stock that fails or is late is left out, never raised."""
    from public_market import request
    get = fetcher or (lambda url: request(url, timeout=CHART_TIMEOUT, budget=CHART_RETRY_BUDGET))

    def one(sym):
        meta = json.loads(get(QUOTE_URL.format(urllib.parse.quote(sym + ".NS", safe=""))))["chart"]["result"][0]["meta"]
        if meta.get("symbol") != sym + ".NS": raise Block("YAHOO: wrong symbol")
        return {"price": float(positive(meta["regularMarketPrice"])), "at": datetime.fromtimestamp(int(meta["regularMarketTime"]), IST).isoformat()}
    out = {}
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, 8)))
    try:
        futures = {pool.submit(one, s): s for s in symbols}
        done, _ = wait(futures, timeout=QUOTE_BUDGET if budget is None else budget)
        for f in done:
            try: out[futures[f]] = f.result()
            except Exception:  # noqa: BLE001 - one bad quote is one missing stock
                pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def _ordinal(n):
    return "" if n == 1 else f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')} "


def rank_text(rank):
    """'the weakest of 17 NSE sector indices', '3rd strongest of 17 ...'."""
    pos, of, change = rank["position"], rank["of"], rank["change_pct"]
    if change < 0: return f"the {_ordinal(of - pos + 1)}weakest of {of} NSE sector indices"
    return f"the {_ordinal(pos)}strongest of {of} NSE sector indices"


def _stock_terms(rows):
    """Regex fragments naming the sector's stocks: symbol, known aliases, first word(s) of the company name."""
    return name_terms({r["symbol"]: r.get("name") or r["symbol"] for r in rows})


SECTOR_NOUNS = r"stocks?|shares?|sectors?|index|indices|pack|counters?|names|majors|space|companies|firms|scrips|cos"
FOREIGN_TICKER_RE = re.compile(r"\((?:NYSE|NASDAQ|AMEX|LSE|TSX|TSXV|ASX|HKEX|SGX|JSE|BMV|OTC\w*)\s*:", re.I)
RESULTS_RE = re.compile(r"\b(?:profits?|earnings|quarterly|results?|revenues?|q[1-4]|beats?|misses|guidance|dividends?|margins?|"
                        r"volumes?|target price|ratings?|upgrades?|downgrades?)\b", re.I)


def names_sector(headline, label_name):
    """True when a headline is about the sector's stocks ('metal stocks', 'Nifty Metal', 'IT shares', 'PSU banks'), not a
    bare commodity word ('metal prices', 'red metal') that only names what moves them."""
    pat = SECTOR_WORDS.get(label_name)
    if not pat: return False
    if re.search(r"\bnifty\s+(?:" + pat + r")\b|\b(?:" + pat + r")\s+(?:" + SECTOR_NOUNS + r")\b", headline, re.I):
        return True
    bare = re.search(r"\b(?:" + pat + r")\b", headline, re.I)
    if bare and re.search(r"stocks?|shares?", bare.group(0), re.I):  # the pattern itself names stocks ('tech stocks')
        return True
    return "bank" in label_name.lower() and bool(
        re.search(r"\b(?:psu|public sector|state[- ]run|private|private sector)\s+banks\b|\bbank nifty\b|^banks\b", headline, re.I))


def sector_news(spec, rows, now, *, change, since, session, news_fetch=None, limit=5, budget=None, window=None):
    """Dated headlines that may explain a sector's move: searches for the sector ('metal stocks', 'Nifty Metal'), the
    commodity behind it where one exists (copper/aluminium/steel for metals), and the two biggest movers on the side of
    the move. Kept: published since ``since`` (the previous close), naming the sector, one of its stocks or a sector
    driver, on the side of the move, not old/housekeeping/outlook news; those giving a cause first. Returns (items, failed)."""
    import india_news
    get = news_getter(news_fetch)
    kw = {"fetcher": get}
    side = "up" if change > 0 else "down"
    stock_pat = "|".join(_stock_terms(rows)) or r"(?!x)x"
    driver_pat = SECTOR_DRIVERS.get(spec["index"], r"(?!x)x")
    when, hours = window or ("when:1d", 24)
    queries = [q + " " + when for q in spec["queries"]] + ([DRIVER_QUERY[spec["index"]] + " " + when] if spec["index"] in DRIVER_QUERY else [])

    def keep(h):
        t = h["headline"]
        member = bool(re.search(r"\b(?:" + stock_pat + r")\b", t, re.I))
        # 'Why is Russel Metals stock sliding today?': one company's stock (singular), not one of the sector's members
        if not member and re.search(r"\b(?:stock|share price|scrip)\b", t, re.I) and not re.search(r"\b(?:stocks|shares)\b", t, re.I):
            return False
        if FOREIGN_TICKER_RE.search(t): return False  # 'AA (NYSE:AA) ... lift Alcoa': a foreign company's own story
        names = member or names_sector(t, spec["label"])
        driver = bool(re.search(r"\b(?:" + driver_pat + r")\b", t, re.I))
        if not names and driver and (RESULTS_RE.search(t) or re.search(r"\b(?:stocks|shares)\b", t, re.I)):
            # a commodity headline that is really another company's results ('KGHM beats profit estimates on high metal
            # prices') or about another group of stocks ('AC stocks jump as copper prices hit 7-month low')
            return False
        return ((names or driver) and (STOCK_NEWS_RE.search(t) or EXPLAIN_RE.search(t) or CUE_MOVE_RE.search(t)) and not _outlook(t)
                and not ADMIN_RE.search(t) and headline_direction(t) in (None, side) and not foreign_cue(t, h.get("publisher"))
                and (since is None or instant(h["published_at"]) >= since) and (session is None or not dated_before(t, session))
                and not (window and earlier_session_report(h, session))
                and not contradicts(t, change))

    movers = (sorted(rows, key=lambda r: r["change_pct"]) if change < 0 else sorted(rows, key=lambda r: -r["change_pct"]))[:2]
    pool = ThreadPoolExecutor(max_workers=4)
    failed = []
    try:
        jobs = {pool.submit(india_news.search, q, hours, clock=lambda: now, **kw): q for q in queries}
        stock_job = pool.submit(stock_headlines, movers, now, news_fetch=get, since=since, session=session, window=window)
        done, pending = wait(list(jobs) + [stock_job], timeout=SECTOR_NEWS_BUDGET if budget is None else budget)
        found = []
        for f, q in jobs.items():
            if f not in done or f.exception() is not None:
                failed.append(q); continue
            for h in f.result():
                if keep(h) and not any(_same_story(h["headline"], x["headline"]) for x in found):
                    names = names_sector(h["headline"], spec["label"]) or bool(re.search(r"\b(?:" + stock_pat + r")\b", h["headline"], re.I))
                    found.append({**h, "source": "Google News RSS", "query": q, "driver_only": not names})
        stocks = stock_job.result() if stock_job in done and stock_job.exception() is None else []
        if stock_job not in done or stock_job.exception() is not None: failed.append("stock headlines")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    # An explanation of the sector's move first, then the best headline on the commodity or driver behind it, then the
    # top movers' own causes, then the rest; within each, causes before plain reports, Indian/major publishers, newest.
    found.sort(key=lambda h: (-int(bool(EXPLAIN_RE.search(h["headline"]))), -causal_score(h["headline"]),
                              -int(bool(PREFERRED_PUBLISHERS.search(str(h.get("publisher") or "")))), -instant(h["published_at"]).timestamp()))
    named = [h for h in found if not h["driver_only"]]
    driver = [h for h in found if h["driver_only"] and causal_score(h["headline"])][:1]
    out = []
    for h in named[:1] + driver + [s for s in stocks if causal_score(s["headline"])] + named[1:] + [s for s in stocks if not causal_score(s["headline"])]:
        if len(out) >= limit: break
        if not any(_same_story(h["headline"], o["headline"]) for o in out): out.append(h)
    return out, failed


def sector_report(text, *, session=None, nse_fetch=None, quote_fetch=None, clock=instant, news=True, news_fetch=None):
    """Every constituent of one NSE sector index with its % change, advances/declines, the sector's % change and rank among
    NSE's sector indices, and dated cause headlines. Prices: NSE's bhavcopy for a finished session (official closes);
    during the session and after the close until the evening bhavcopy, Yahoo's last price against NSE's previous-day
    bhavcopy close (the same basis as the market movers report)."""
    import nse_live
    spec = resolve_sector(text)
    nse_fetch = nse_fetch or nse_live.fetch
    now = clock(); coverage, notes = [], []
    out = {"schema": "market_sector_v1", "as_of": now.isoformat(), "sector": spec["label"], "index": spec["index"], "submitted": False,
           "research_only": True, "coverage": coverage, "notes": notes}
    snap = None
    if session is None:
        try:
            snap = nse_live.live_indices(fetcher=nse_fetch, clock=lambda: now)
        except ERRORS:
            coverage.append({"component": "nse_live", "status": "FAILED"})
    day = date.fromisoformat(session) if session else (instant(snap["as_of"]).date() if snap else None)
    if day is None: raise Block("MOVERS: NSE live data unavailable; pass --session YYYY-MM-DD for a finished session")
    if day > now.date(): raise Block("MOVERS: future session")
    out["session"] = day.isoformat()
    out["closed"] = not (day == now.date() and now < session_close(day))
    previous_session = None
    if snap is not None and instant(snap["as_of"]).date() == day:
        rows = snap["indices"]
        if spec["index"] not in rows: raise Block(f"MOVERS: {spec['label']} missing from NSE's index list")
        row = rows[spec["index"]]
        previous_session = row.get("previous_day") or nse_live.previous_session(day, fetcher=nse_fetch, clock=lambda: now).isoformat()
        out["at"] = snap["as_of"]; out["level_source"] = snap["page"]
        out["level"] = {"value": row["last"], "change": row["change"], "change_pct": row["change_pct"], "previous_close": row["previous_close"],
                        "advances": row.get("advances"), "declines": row.get("declines"), "unchanged": row.get("unchanged"),
                        "basis": "NSE live index data" + (" (closing level)" if out["closed"] else "")}
        levels = {n: rows[n]["change_pct"] for n in SECTORS if n in rows}
    else:
        closes = nse_live.index_closes(day, fetcher=nse_fetch)
        if spec["index"] not in closes: raise Block(f"MOVERS: {spec['label']} missing from NSE's index report for {day}")
        row = closes[spec["index"]]
        out["at"] = None; out["level_source"] = nse_live.INDEX_CLOSE.format(day.strftime("%d%m%Y"))
        out["level"] = {"value": row["close"], "change": row["change"], "change_pct": row["change_pct"],
                        "previous_close": round(row["close"] - row["change"], 2), "basis": "NSE daily index report (official close)"}
        levels = {n: closes[n]["change_pct"] for n in SECTORS if n in closes}
    ranked = sorted(levels.items(), key=lambda kv: -kv[1])
    out["rank"] = {"position": [n for n, _ in ranked].index(spec["index"]) + 1 if spec["index"] in levels else None, "of": len(ranked),
                   "change_pct": out["level"]["change_pct"],
                   "strongest": {"name": label(ranked[0][0]), "change_pct": ranked[0][1]} if ranked else None,
                   "weakest": {"name": label(ranked[-1][0]), "change_pct": ranked[-1][1]} if ranked else None}
    # ---- constituents and their moves
    listing = nse_live.index_members(spec["slug"], fetcher=nse_fetch, today=now.date())
    members = listing["members"]
    out["constituents_source"] = {"url": listing["source"], "as_of": listing["as_of"], "basis": listing["basis"]}
    official = None
    if out["closed"]:
        try:
            official = bhavcopy(day, fetcher=nse_fetch)["prices"]
        except ERRORS:
            official = None  # today's bhavcopy is published in the evening
    stocks = []
    if official:
        for m in members:
            p = official.get(m["symbol"])
            if p: stocks.append({**m, "price": p["close"], "previous_close": p["previous_close"], "change_pct": round(p["change_pct"], 2)})
        out["price_basis"] = f"NSE bhavcopy of {day:%d %b %Y}: official close vs previous close"
    else:
        if previous_session is None:
            try:
                cal = nse_live.holiday_calendar(fetcher=nse_fetch, clock=lambda: now)
                previous_session = nse_live.previous_trading_day(cal, day).isoformat()
            except ERRORS:
                previous_session = None
        prev = None
        if previous_session:
            try:
                prev = bhavcopy(date.fromisoformat(previous_session), fetcher=nse_fetch)["prices"]
            except ERRORS:
                coverage.append({"component": "previous-day bhavcopy", "status": "FAILED"})
        if prev:
            quotes = stock_quotes([m["symbol"] for m in members], fetcher=quote_fetch)
            for m in members:
                q, p = quotes.get(m["symbol"]), prev.get(m["symbol"])
                if not q or not p or q["at"][:10] != day.isoformat(): continue
                stocks.append({**m, "price": q["price"], "previous_close": p["close"], "change_pct": round((q["price"] / p["close"] - 1) * 100, 2), "at": q["at"]})
            out["price_basis"] = (f"Yahoo last price (unofficial) vs NSE's official {date.fromisoformat(previous_session):%d %b} close (bhavcopy)"
                                  + ("; NSE's official closes for today come with this evening's bhavcopy" if out["closed"] and day == now.date() else ""))
    stocks.sort(key=lambda r: -r["change_pct"])
    out["stocks"] = stocks
    out["missing"] = sorted({m["symbol"] for m in members} - {s["symbol"] for s in stocks})
    out["members"] = len(members)
    out["breadth"] = {"advances": sum(s["change_pct"] > 0 for s in stocks), "declines": sum(s["change_pct"] < 0 for s in stocks),
                      "unchanged": sum(s["change_pct"] == 0 for s in stocks)}
    if not stocks:
        out["stocks_missing"] = ("the previous-day bhavcopy could not be downloaded after retries" if any(c["component"] == "previous-day bhavcopy" for c in coverage)
                                 else "no constituent prices were collected")
    # ---- why: dated headlines. Today's session, or the latest one while no session has opened since (a weekend or
    # holiday): the news since the session before it still explains the move, so the search reaches back to that close.
    quiet = day < now.date() and quiet_since(day, now, nse_fetch=nse_fetch)
    if (day == now.date() or quiet) and previous_session is None:
        previous_session = nse_live.previous_session(day, fetcher=nse_fetch, clock=lambda: now).isoformat()
    out["previous_session"] = previous_session
    out["market_closed"] = closed_banner_info(now, day, nse_fetch=nse_fetch) if quiet else None
    if news and (day == now.date() or quiet):
        since = session_close(previous_session) if previous_session else None
        try:
            items, failed = sector_news(spec, stocks, now, change=out["level"]["change"], since=since, session=day.isoformat(), news_fetch=news_fetch,
                                        window=news_window(now, since) if quiet else None)
        except ERRORS:
            items, failed = [], ["news"]
        out["headlines"] = items
        out["news_since"] = since.isoformat() if since else None
        if failed and not items:
            coverage.append({"component": "headlines", "status": "FAILED"})
            out["headlines_missing"] = "the news search failed or ran out of time this run; rerun, or search the web with the date"
    elif news:
        out["headlines_missing"] = (f"{_day(day.isoformat())} is a past session and NSE has traded since; Google News search covers "
                                    "only the days since the latest close - search the web with that date")
        notes.append("Headlines are searched for the latest session only (today's, or the last one on a weekend or holiday).")
    else:
        out["headlines_missing"] = "headline search skipped (--no-news)"
    return out


def _signed_pct(v): return f"{v:+.2f}%"


def sector_lead(r):
    lv = r["level"]
    when = (f"today, {_day(r['session'])} (NSE, {r['at'][11:16]} IST closing level)" if r["closed"] and r.get("at") and r["session"] == r["as_of"][:10]
            else f"on {_day(r['session'])} (NSE official close)" if r["closed"] else f"at {r['at'][11:16]} IST (NSE live)")
    verb = ("closed " if r["closed"] else "is ") + f"**{word(lv['change'])}** {abs(lv['change_pct']):.2f}% ({lv['change']:+,.2f} points)"
    head = f"{r['sector']} {verb} at {_pts(lv['value'])} {when}"
    if r["rank"].get("position"): head += f" - {rank_text(r['rank'])}"
    return head + "."


def sector_breadth_text(r):
    b, lv = r["breadth"], r["level"]
    text = f"{b['advances']} of {len(r['stocks'])} stocks up, {b['declines']} down" + (f", {b['unchanged']} unchanged" if b["unchanged"] else "")
    if lv.get("advances") is not None and lv.get("declines") is not None:
        text += f" (NSE's own count: {lv['advances']} up, {lv['declines']} down)"
    return text


def headlines_gap_line(r):
    """'Headlines not collected (reason)' whenever a report has no headline search result to show."""
    return f"Headlines not collected ({r['headlines_missing']})." if r.get("headlines_missing") else None


def banner_lines(r):
    b = closed_banner(r.get("market_closed"))
    return [b, ""] if b else []


def sector_summary(r):
    lines = banner_lines(r) + [sector_lead(r)]
    if r["stocks"]:
        falling = r["level"]["change"] < 0
        order = sorted(r["stocks"], key=lambda s: s["change_pct"]) if falling else r["stocks"]
        lines.append(f"- Breadth: {sector_breadth_text(r)}.")
        lines.append(f"- {'Fell most' if falling else 'Rose most'} first, every constituent: " + ", ".join(f"{s['symbol']} {_signed_pct(s['change_pct'])}" for s in order) + ".")
        lines.append(f"- Prices: {r['price_basis']}." + (f" Missing: {', '.join(r['missing'])}." if r["missing"] else ""))
    else:
        lines.append(f"- ⚠ stock moves not collected — rerun ({r.get('stocks_missing', 'no prices')}); the sector level above is complete.")
    rk = r["rank"]
    if rk.get("strongest"): lines.append(f"- Other sectors: strongest {rk['strongest']['name']} {_signed_pct(rk['strongest']['change_pct'])}, weakest {rk['weakest']['name']} {_signed_pct(rk['weakest']['change_pct'])}.")
    if r.get("headlines"):
        since = (f"the {instant(r['news_since']):%d %b} close, the session before {_day(r['session'])[:-5]}" if r.get("market_closed") and r.get("news_since")
                 else "the last close")
        lines.append(f"- Possible causes (Google News headlines since {since}; unverified, not confirmed reasons):")
        lines += ["  - " + (f"{h['symbol']}: " if h.get("symbol") else "") + f"\"{h['headline']}\" ({h.get('publisher') or 'Google News'}, {_stamp(h['published_at'])})" for h in r["headlines"]]
    elif "headlines" in r and not r.get("headlines_missing"):
        lines.append("- Possible causes: no dated headline since the last close explains this sector's move - a gap, not an all-clear.")
    if headlines_gap_line(r): lines.append("- " + headlines_gap_line(r))
    lines.append("- Research only: explains the move from prices; no prediction, no buy/sell advice.")
    return "\n".join(lines) + "\n"


def sector_markdown(r):
    lines = [f"# {r['sector']} - {_day(r['session'])}", ""] + banner_lines(r) + [sector_lead(r), ""]
    if r["stocks"]:
        lines += [f"**Breadth:** {sector_breadth_text(r)}.", "", "| Stock | Company | Price | Prev close | Change |", "| --- | --- | ---: | ---: | ---: |"]
        lines += [f"| {s['symbol']} | {s['name']} | {_pts(s['price'])} | {_pts(s['previous_close'])} | {_signed_pct(s['change_pct'])} |" for s in r["stocks"]]
        lines.append("")
    else:
        lines += [f"⚠ stock moves not collected — rerun ({r.get('stocks_missing', 'no prices')}); the sector level above is complete.", ""]
    if r.get("headlines"):
        lines += ["## Possible causes (headlines, unverified)", ""]
        lines += ["- " + (f"{h['symbol']}: " if h.get("symbol") else "") + f"{_link(h)} ({h.get('publisher') or 'Google News'}, {_stamp(h['published_at'])})" for h in r["headlines"]]
        lines.append("")
    elif headlines_gap_line(r):
        lines += [headlines_gap_line(r), ""]
    lines += ["## Data notes", "", f"- Sector level: {r['level']['basis']} ({public_source(r['level_source'])}).",
              f"- Constituents: {r['constituents_source']['basis']}, {r['constituents_source']['url']} ({r['members']} stocks)."]
    if r.get("price_basis"): lines.append(f"- Prices: {r['price_basis']}." + (f" Missing: {', '.join(r['missing'])}." if r["missing"] else ""))
    if r.get("headlines"): lines.append("- Headlines: Google News RSS since the previous close, kept when they name the sector, one of its stocks or a sector driver and run with the move; publishers' claims, not verified causes.")
    lines += ["- " + n for n in r.get("notes", [])]
    failed = [x["component"] for x in r.get("coverage", []) if x["status"] == "FAILED"]
    if failed: lines.append("- Not collected this run: " + ", ".join(failed) + ".")
    lines.append("- Research only; no orders.")
    return "\n".join(lines) + "\n"


def public_source(url):
    return str(url).replace("://www.nseindia.in/", "://www.nseindia.com/")


# ---------------------------------------------------------------- period comparison ("midcaps vs Nifty this week")

PERIOD_INDICES = ("NIFTY 50", "NIFTY BANK", "NIFTY NEXT 50", "NIFTY MIDCAP 150", "NIFTY SMALLCAP 250", "NIFTY 500", "NIFTY MIDCAP 100", "NIFTY SMALLCAP 100")
PERIOD_WORDS = {"week": "This week", "month": "This month", "ytd": "This year (YTD)", "since": "Since"}


def period_cut(kind, end_day, since=None):
    """The first calendar day of the period; the base is the last session before it. 'week': Monday of ``end_day``'s
    week (so the base is the previous Friday's close, or the session before it after a holiday); 'month': the 1st;
    'ytd': 1 January; 'since': the day after ``since`` (the base is ``since``'s own close)."""
    if kind == "week": return end_day - timedelta(days=end_day.weekday())
    if kind == "month": return end_day.replace(day=1)
    if kind == "ytd": return date(end_day.year, 1, 1)
    if kind == "since" and since: return since + timedelta(days=1)
    raise Block("MOVERS: period must be week, month, ytd or --since YYYY-MM-DD")


def closes_on_or_before(day, *, fetcher, cal=None, today=None, back=12):
    """(session, rows) from NSE's ind_close_all file of the last session on or before ``day`` (cached on disk for the live
    fetch), skipping weekends, listed holidays and dates with no file."""
    import nse_live
    folder = nse_live.cache_dir() if nse_live.is_live(fetcher) else None  # a fake never reads or fills the user's cache
    probe = day
    for _ in range(back):
        text = probe.isoformat()
        special = cal is not None and text in cal.get("special_sessions", [])
        holiday = cal is not None and text in cal["segments"]["CM"] and not special
        if (probe.weekday() < 5 or special) and not holiday:
            try:
                return probe, nse_live.fetch_retry(lambda _u: nse_live.cached_index_closes(probe, fetcher=fetcher, folder=folder, today=today),
                                                   nse_live.INDEX_CLOSE.format(probe.strftime("%d%m%Y")), delays=nse_live.retry_delays(fetcher))
            except nse_live.Missing:
                pass
        probe -= timedelta(days=1)
    raise Block(f"MOVERS: no NSE index-close file found on or before {day}")


def sensex_closes(fetcher=None):
    """{date: close} of Sensex from Yahoo's two-year daily chart (unofficial)."""
    import nse_live
    data = json.loads((fetcher or nse_live.yahoo_get)("https://query1.finance.yahoo.com/v8/finance/chart/%5EBSESN?range=2y&interval=1d"))["chart"]["result"][0]
    if data["meta"].get("symbol") != "^BSESN": raise Block("YAHOO: wrong instrument")
    return {datetime.fromtimestamp(int(t), IST).date(): float(c) for t, c in zip(data["timestamp"], data["indicators"]["quote"][0]["close"]) if c}


PERIOD_ALIASES = (  # what people call the broad indices -> NSE names (sectors resolve through SECTOR_SPECS)
    (r"(?:nifty )?50|nifty|the nifty|large ?caps?|largecaps?", "NIFTY 50"), (r"bank ?nifty|nifty ?bank|banks?|banking", "NIFTY BANK"),
    (r"next ?50|nifty next ?50|junior(?: nifty)?", "NIFTY NEXT 50"), (r"(?:nifty )?500|broad market", "NIFTY 500"),
    (r"mid ?caps? ?100|midcap ?100", "NIFTY MIDCAP 100"), (r"small ?caps? ?100|smallcap ?100", "NIFTY SMALLCAP 100"),
    (r"mid ?caps?(?: ?150)?|mid ?cap index", "NIFTY MIDCAP 150"), (r"small ?caps?(?: ?250)?", "NIFTY SMALLCAP 250"),
    (r"sensex|bse sensex", "SENSEX"))


def resolve_period_index(text):
    """'midcaps', 'smallcap 250', 'bank nifty', 'IT', 'sensex' -> the NSE index name (or SENSEX); Block when unknown."""
    t = " ".join(re.sub(r"[^a-z0-9& ]+", " ", str(text).lower()).replace("indices", " ").replace("index", " ").replace("stocks", " ").split())
    t = re.sub(r"^nifty (?=mid|small|next|500|bank|50$)", "", t)
    for pattern, name in PERIOD_ALIASES:
        if t and re.fullmatch(pattern, t): return name
    return resolve_sector(text)["index"]


def parse_period_indices(words):
    """The indices a user named, in their order: greedy longest match over the words ('midcap 150 smallcap 250', 'midcaps,
    smallcaps', 'IT bank'). Block on a word that names no index."""
    tokens = " ".join(words).replace(",", " , ").replace(" and ", " , ").replace(" vs ", " , ").split()
    out, i = [], 0
    while i < len(tokens):
        if tokens[i] == ",":
            i += 1; continue
        for j in range(min(len(tokens), i + 4), i, -1):
            chunk = tokens[i:j]
            if "," in chunk: continue
            try:
                name = resolve_period_index(" ".join(chunk))
            except Block:
                continue
            if name not in out: out.append(name)
            i = j
            break
        else:
            raise Block(f"MOVERS: unknown index '{tokens[i][:30]}'; e.g. midcap 150, smallcap 250, next 50, nifty 500, bank, IT, sensex")
    return out


def period_report(kind, *, since=None, nse_fetch=None, yahoo_fetch=None, clock=instant, indices=None):
    """Index and sector % changes over a period from NSE's official daily index closes (ind_close_all files, cached), the
    end being today's live or closing NSE level when today is a session. ``indices``: the NSE names the user asked
    about (``parse_period_indices``), which lead the answer."""
    import nse_live
    nse_fetch = nse_fetch or nse_live.fetch
    now = clock()
    if kind == "since" and not since: raise Block("MOVERS: --since needs a date")
    snap = cal = None
    try:
        snap = nse_live.live_indices(fetcher=nse_fetch, clock=lambda: now)
    except ERRORS:
        pass
    try:
        cal = nse_live.holiday_calendar(fetcher=nse_fetch, clock=lambda: now)
    except ERRORS:
        pass
    end_day = instant(snap["as_of"]).date() if snap else now.date()
    end, end_basis, end_at = None, None, None
    try:
        got_day, end = closes_on_or_before(end_day, fetcher=nse_fetch, cal=cal, today=now.date(), back=1 if snap else 12)
        end_day, end_basis = got_day, "NSE daily index report (official close)"
    except (Block,) + ERRORS:
        end = None
    if end is None:
        if not snap: raise Block("MOVERS: NSE index data unavailable")
        end = {n: {"close": r["last"]} for n, r in snap["indices"].items()}
        closed = instant(snap["as_of"]) >= session_close(end_day)
        end_basis = "NSE closing level (15:30 snapshot)" if closed else "NSE live level"
        end_at = snap["as_of"]
    cut = period_cut(kind, end_day, since)
    if cut > end_day: raise Block("MOVERS: the period starts after the latest session")
    base_day, base = closes_on_or_before(cut - timedelta(days=1), fetcher=nse_fetch, cal=cal, today=now.date())
    rows = []
    for n in dict.fromkeys(PERIOD_INDICES + SECTORS):
        if n in base and n in end:
            b, e = base[n]["close"], end[n]["close"]
            rows.append({"index": n, "name": label(n), "base": b, "end": e, "change_pct": round((e / b - 1) * 100, 2), "sector": n in SECTORS})
    by = {r["index"]: r for r in rows}
    if "NIFTY 50" not in by: raise Block("MOVERS: Nifty 50 missing from NSE's index files")
    for r in rows: r["vs_nifty_pp"] = round(r["change_pct"] - by["NIFTY 50"]["change_pct"], 2)
    out = {"schema": "market_period_v1", "as_of": now.isoformat(), "period": kind, "since": since.isoformat() if since else None,
           "base_session": base_day.isoformat(), "end_session": end_day.isoformat(), "end_basis": end_basis, "end_at": end_at,
           "rows": rows, "submitted": False, "research_only": True, "named": list(indices or []),
           "market_closed": closed_banner_info(now, end_day, nse_fetch=nse_fetch) if end_day < now.date() else None,
           "source": nse_live.INDEX_CLOSE.format("DDMMYYYY"),
           "definition": {"week": "since the previous week's last close (normally Friday's)", "month": "since the last close of the previous month",
                          "ytd": "since the last close of the previous year", "since": f"since the {base_day:%d %b %Y} close"}[kind]}
    try:
        sx = sensex_closes(yahoo_fetch)
        if base_day in sx and end_day in sx:
            out["sensex"] = {"base": sx[base_day], "end": sx[end_day], "change_pct": round((sx[end_day] / sx[base_day] - 1) * 100, 2), "basis": "Yahoo daily closes (unofficial)"}
    except ERRORS:
        pass
    return out


def _pp(x):
    return f"{x:+.2f} pp vs Nifty 50"


def period_lead(r):
    """The period and dates, then the indices the user named (each with its gap to Nifty 50), or Nifty 50 first."""
    by = {x["index"]: x for x in r["rows"]}
    n = by["NIFTY 50"]
    base, end = date.fromisoformat(r["base_session"]), date.fromisoformat(r["end_session"])
    head = PERIOD_WORDS[r["period"]] + ("" if r["period"] != "since" else f" {base:%d %b %Y}")
    end_text = f"{end:%a} {end.day} {end:%b}" + (f" {r['end_at'][11:16]} IST" if r.get("end_at") and "live" in r["end_basis"] else " close")
    nifty = f"Nifty 50 {_signed_pct(n['change_pct'])} ({_pts(n['base'])} to {_pts(n['end'])})"
    named = []
    for name in r.get("named") or []:
        if name == "NIFTY 50": continue
        if name == "SENSEX":
            sx = r.get("sensex")
            named.append(f"Sensex {_signed_pct(sx['change_pct'])} (Yahoo daily closes, unofficial; {_pp(sx['change_pct'] - n['change_pct'])})" if sx
                         else "Sensex: not available for these dates (Yahoo)")
        elif name in by:
            x = by[name]
            named.append(f"{x['name']} {_signed_pct(x['change_pct'])} ({_pp(x['vs_nifty_pp'])})")
        else:
            named.append(f"{label(name)}: not in NSE's files for both dates")
    if named:
        return f"**{head}** ({base:%a} {base.day} {base:%b} close to {end_text}): " + "; ".join(named) + f". For comparison, {nifty}."
    return f"**{head}** ({base:%a} {base.day} {base:%b} close to {end_text}): {nifty}."


def _vs(x):
    if abs(x["vs_nifty_pp"]) < 0.05: return "in line with Nifty 50"
    return f"{abs(x['vs_nifty_pp']):.2f} percentage points {'better' if x['vs_nifty_pp'] > 0 else 'worse'} than Nifty 50"


def period_summary(r):
    by = {x["index"]: x for x in r["rows"]}
    lines = banner_lines(r) + [period_lead(r)]
    broad = [by[n] for n in ("NIFTY MIDCAP 150", "NIFTY SMALLCAP 250") if n in by]
    if broad:
        verdict = ("both lagged" if all(x["vs_nifty_pp"] < 0 for x in broad) else "both beat" if all(x["vs_nifty_pp"] > 0 for x in broad) else "mixed against")
        if all(x["index"] in (r.get("named") or []) for x in broad):  # already in the lead: the verdict only
            if len(broad) == 2: lines.append(f"- Midcaps and smallcaps: {verdict} the Nifty.")
        else:
            lines.append("- Midcaps and smallcaps: " + "; ".join(f"{x['name']} {_signed_pct(x['change_pct'])} ({_vs(x)})" for x in broad)
                         + (f" - {verdict} the Nifty." if len(broad) == 2 else "."))
    named = r.get("named") or []
    other = [by[n] for n in ("NIFTY 500", "NIFTY BANK", "NIFTY NEXT 50", "NIFTY MIDCAP 100", "NIFTY SMALLCAP 100") if n in by and n not in named]
    if other: lines.append("- Also: " + ", ".join(f"{x['name']} {_signed_pct(x['change_pct'])} ({x['vs_nifty_pp']:+.2f} pp)" for x in other) + ".")
    if r.get("sensex") and "SENSEX" not in named:
        gap = r["sensex"]["change_pct"] - by["NIFTY 50"]["change_pct"]
        lines.append(f"- Sensex (Yahoo daily closes, unofficial): {_signed_pct(r['sensex']['change_pct'])}. Gap to Nifty 50: {gap:+.2f} pp.")
    sectors = sorted((x for x in r["rows"] if x["sector"]), key=lambda x: -x["change_pct"])
    if sectors:
        best = ", ".join(f"{x['name']} {_signed_pct(x['change_pct'])} ({x['vs_nifty_pp']:+.2f} pp)" for x in sectors[:3])
        worst = ", ".join(f"{x['name']} {_signed_pct(x['change_pct'])} ({x['vs_nifty_pp']:+.2f} pp)" for x in sectors[::-1][:3])
        lines.append(f"- Sectors over the period: strongest {best}; weakest {worst}.")
    base, end = date.fromisoformat(r["base_session"]), date.fromisoformat(r["end_session"])
    if "official" in r["end_basis"]:
        src = f"NSE ind_close_all daily index files (official closes) dated {base:%d %b} and {end:%d %b %Y}"
    else:
        src = f"NSE ind_close_all daily index file (official close) of {base:%d %b %Y} for the base; the end is the {r['end_basis']} of {end:%d %b}"
    lines.append(f"- Source: {src}; '{PERIOD_WORDS[r['period']].lower()}' = {r['definition']}; pp = percentage points of difference from Nifty 50's change.")
    lines.append("- Research only: past moves, no prediction or advice.")
    return "\n".join(lines) + "\n"


def period_markdown(r):
    lines = [f"# Period comparison - {PERIOD_WORDS[r['period']]}", ""] + banner_lines(r) + [period_lead(r), "", "| Index | Base close | End | Change | vs Nifty 50 (pp) |", "| --- | ---: | ---: | ---: | ---: |"]
    for x in [x for x in r["rows"] if not x["sector"]]:
        lines.append(f"| {x['name']} | {_pts(x['base'])} | {_pts(x['end'])} | {_signed_pct(x['change_pct'])} | {x['vs_nifty_pp']:+.2f} |")
    if r.get("sensex"):
        gap = r["sensex"]["change_pct"] - next(x["change_pct"] for x in r["rows"] if x["index"] == "NIFTY 50")
        lines.append(f"| Sensex (Yahoo, unofficial) | {_pts(r['sensex']['base'])} | {_pts(r['sensex']['end'])} | {_signed_pct(r['sensex']['change_pct'])} | {gap:+.2f} |")
    sectors = sorted((x for x in r["rows"] if x["sector"]), key=lambda x: -x["change_pct"])
    if sectors:
        lines += ["", "## Sectors over the period", "", "| Sector | Change | vs Nifty 50 (pp) |", "| --- | ---: | ---: |"]
        lines += [f"| {x['name']} | {_signed_pct(x['change_pct'])} | {x['vs_nifty_pp']:+.2f} |" for x in sectors]
    lines += ["", "## Data notes", "", period_summary(r).strip().splitlines()[-2], "- Research only; no orders."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--session", help="Finished session YYYY-MM-DD (default: today live, else the latest NSE session)")
    p.add_argument("--summary", action="store_true", help="Short answer-first version")
    p.add_argument("--premise", choices=["up", "down"], help="The user's assumed direction; the report corrects it when wrong")
    p.add_argument("--flow-premise", choices=["selling", "buying"], help="The user's claim about FIIs ('FIIs are selling'); checked against NSE and NSDL flows")
    p.add_argument("--sector", nargs="+", help="Drill into one NSE sector: IT, BANK, PSU BANK, METAL, AUTO, PHARMA, FMCG, REALTY, ENERGY, ... (natural names work: 'IT stocks', 'metal stocks')")
    p.add_argument("--period", choices=["week", "month", "ytd"], help="Index and sector change over this week / month / year to date (NSE official closes)")
    p.add_argument("--since", help="Index and sector change since this YYYY-MM-DD close")
    p.add_argument("--indices", nargs="+", help="With --period/--since: the indices the user asked about, which lead the answer "
                   "(midcap 150, smallcap 250, next 50, nifty 500, bank, IT, sensex, ...); each with its gap to Nifty 50")
    p.add_argument("--no-news", action="store_true", help="Skip the headline searches")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown"); p.add_argument("--out")
    a = p.parse_args(argv)
    try:
        if a.out and Path(a.out).exists(): raise Block("OUTPUT: choose a new file")
        if a.session: date.fromisoformat(a.session)
        if sum(map(bool, (a.sector, a.period or a.since))) > 1: raise Block("MOVERS: choose --sector or a period, not both")
        if a.period and a.since: raise Block("MOVERS: choose --period or --since, not both")
        if a.indices and not (a.period or a.since): raise Block("MOVERS: --indices goes with --period or --since")
        if a.sector:
            result = sector_report(" ".join(a.sector), session=a.session, news=not a.no_news)
            render = sector_summary if a.summary else sector_markdown
        elif a.period or a.since:
            since = date.fromisoformat(a.since) if a.since else None
            result = period_report(a.period or "since", since=since, indices=parse_period_indices(a.indices) if a.indices else None)
            render = period_summary if a.summary else period_markdown
        else:
            result = collect(a.session, news=not a.no_news)
            render = None
    except ERRORS as exc:
        msg = str(exc) if isinstance(exc, Block) and str(exc).startswith(("MOVERS", "OUTPUT")) else "NSE data for that session is unavailable (holiday, not yet published, or source down)"
        print(json.dumps({"status": "UNAVAILABLE", "reason": msg, "submitted": False})); return 2
    if render is not None:
        body = json.dumps(result, indent=2, allow_nan=False) if a.format == "json" else render(result)
    else:
        body = (json.dumps(result, indent=2, allow_nan=False) if a.format == "json" else summary(result, a.premise, a.flow_premise) if a.summary
                else markdown(result, a.premise, a.flow_premise))
    if a.out:
        path = Path(a.out); path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream: stream.write(body)
    else:
        from daily import emit
        emit(body)
    return 0


if __name__ == "__main__":
    from daily import exit_now
    exit_now(main())  # abandoned chart or news downloads past their time budget must not hold the process open
