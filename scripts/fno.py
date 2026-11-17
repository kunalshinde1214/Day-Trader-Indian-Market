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
import io
import json
import math
from pathlib import Path
import re
import sys
import zipfile

from core import Block, IST, instant
from public_market import chart, request

BHAVCOPY = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d}_F_0000.csv.zip"
INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
DEFAULT_RATE = 0.065
DISCLAIMER = ("Research from public end-of-day exchange data, not investment advice. OI, PCR and max pain describe "
              "positioning; they are not reliable predictions. Options can lose their full premium; short options can lose far more.")


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


def fetch_bhavcopy(selected, *, fetcher=request, today=None, on=None, lookback=8):
    """Most recent available bhavcopy on or before `on` (default today), skipping weekends and holidays."""
    day = date.fromisoformat(on) if on else (today or datetime.now(IST).date())
    tried = []
    for _ in range(lookback):
        if day.weekday() < 5:
            url = BHAVCOPY.format(d=day.strftime("%Y%m%d"))
            tried.append(day.isoformat())
            try:
                return parse_bhavcopy(fetcher(url), selected), url
            except Block as error:
                if "no F&O contracts" in str(error):
                    raise
            except (zipfile.BadZipFile, KeyError, ValueError):
                pass
        day -= timedelta(days=1)
    raise Block("FNO: no NSE F&O bhavcopy found for " + ", ".join(tried) + "; it is published after market close")


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


def analyse_chain(contracts, selected, *, expiry=None, as_of=None, rate=DEFAULT_RATE, width=12, source="NSE F&O bhavcopy"):
    trade_date = contracts[0]["trade_date"]
    as_of = as_of or datetime.combine(date.fromisoformat(trade_date), dtime(15, 30), IST)
    live = contracts[0]["settle"] is None  # --input chains carry one expiry and may be read on expiry day itself
    available = sorted({c["expiry"] for c in contracts if c["kind"] in {"CE", "PE"}}) if live else expiries(contracts, trade_date)
    if not available:
        raise Block("FNO: no live option expiries in the data")
    chosen = expiry or available[0]
    if chosen not in available:
        raise Block(f"FNO: expiry {chosen} not available; choose from {', '.join(available[:8])}")
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

    window = sorted(strikes, key=lambda k: abs(k - atm))[:2 * width + 1]
    return {
        "schema": "fno_chain_report_v1", "underlying": selected, "source": source, "data_date": trade_date, "as_of": as_of.isoformat(),
        "expiry": chosen, "expiries": available[:8], "days_to_expiry": round(years * 365, 2), "spot": spot, "lot_size": lot,
        "forward": forward, "forward_basis": forward_basis, "rate_assumption": rate, "atm_strike": atm,
        "pcr_oi": put_oi / call_oi if call_oi else None, "pcr_volume": put_vol / call_vol if call_vol else None,
        "call_oi": call_oi, "put_oi": put_oi, "call_oi_change": sum(c["oi_change"] for c in calls.values()),
        "put_oi_change": sum(p["oi_change"] for p in puts.values()),
        "max_pain": max_pain(strikes, calls, puts), "resistance_oi": resistance, "support_oi": support,
        "atm_iv": atm_iv, "skew_put_minus_call": skew, "straddle": straddle,
        "expected_move": straddle, "expected_move_iv": spot * atm_iv * math.sqrt(years) if atm_iv else None,
        "range_1sd": one_sd_range(forward, atm_iv, years),
        "call_oi_breakdown": oi_breakdown(calls), "put_oi_breakdown": oi_breakdown(puts), "future": fut,
        "chain": [r for r in rows if r["strike"] in window], "otm_note": "IV uses traded closes only; untraded strikes show no IV.",
        "disclaimer": DISCLAIMER, "execution_eligible": False, "submitted": False,
        "_books": (calls, puts), "_years": years, "_live": live,
    }


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


def profit_probability(value, knots, forward, vol, years, eps=1e-9):
    """P(value(S_T) > 0) for a payoff that is linear between knots (strikes); exact integration segment by segment."""
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
        total += prob_above(lo, forward, vol, years) - (prob_above(hi, forward, vol, years) if hi is not None else 0.0)
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
            "percentile_1y": sum(c <= bar["close"] for c in closes) / len(closes), "low_1y": min(closes), "high_1y": max(closes)}


# ---------------- day over day ----------------

COMPARE_KEYS = ("spot", "pcr_oi", "max_pain", "atm_iv", "call_oi", "put_oi")


def day_over_day(current, previous, previous_source=None):
    """Same-expiry comparison of two analysed chains (previous trading day vs the report's data date)."""
    metrics = {}
    for key in COMPARE_KEYS:
        before, now = previous[key], current[key]
        metrics[key] = {"previous": before, "current": now, "change": None if before is None or now is None else now - before}
    note = ("Current values come from the supplied chain; previous values are end of day." if current.get("_live") else
            "Both days are NSE end-of-day bhavcopies.")
    return {"status": "ok", "expiry": current["expiry"], "previous_date": previous["data_date"], "current_date": current["data_date"],
            "previous_source": previous_source, "metrics": metrics, "note": note}


def compare_previous(report, selected, *, fetcher=request, on=None):
    """Fetch the previous trading day's bhavcopy (walking back over weekends and holidays) and compare the same expiry."""
    data_day = date.fromisoformat(report["data_date"])
    if on:
        try:
            target = date.fromisoformat(on)
        except ValueError:
            raise Block("FNO: --previous-date must be YYYY-MM-DD") from None
        if target >= data_day:
            raise Block(f"FNO: --previous-date must be before the data date {report['data_date']}")
    else:
        target = data_day - timedelta(days=1)
    try:
        contracts, url = fetch_bhavcopy(selected, fetcher=fetcher, on=target.isoformat())
    except Block as error:
        return {"status": "unavailable", "note": f"Previous trading day's bhavcopy unavailable ({error}); day-over-day comparison skipped."}
    try:
        previous = analyse_chain(contracts, selected, expiry=report["expiry"], rate=report["rate_assumption"])
    except Block:
        return {"status": "unavailable",
                "note": f"Expiry {report['expiry']} was not listed in the {contracts[0]['trade_date']} bhavcopy; day-over-day comparison skipped."}
    return day_over_day(report, previous, "NSE F&O bhavcopy " + url)


# ---------------- strategies ----------------

LEG = re.compile(r"^(BUY|SELL):(CE|PE|FUT):([0-9.]*):([0-9]+)(?::([0-9.]+))?$")


def parse_leg(text):
    m = LEG.match(text.strip().upper())
    if not m:
        raise Block("FNO: leg must look like BUY:CE:25500:1 (side:type:strike:lots[:premium]); futures use BUY:FUT::1")
    side, kind, strike, lots, premium = m.groups()
    if kind != "FUT" and not strike:
        raise Block("FNO: option legs need a strike")
    if int(lots) < 1:
        raise Block("FNO: lots must be at least 1")
    return {"side": side, "type": kind, "strike": float(strike) if strike else None, "lots": int(lots), "premium": float(premium) if premium else None}




PROB_LABEL = "implied by option prices, not a forecast"


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


def payoff(report, legs, *, span=0.08, points=81, table_rows=18):
    calls, puts = report["_books"]
    lot = report["lot_size"]
    if not lot:
        raise Block("FNO: lot size unavailable")
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
        if years <= 0:
            return value(s)
        scale = s / spot
        total = 0.0
        for leg in resolved:
            if leg["type"] == "FUT":
                now = future_now * scale
            else:
                now = black76(leg["type"], forward * scale, leg["strike"], years, leg["iv"], rate)
            total += leg["units"] * (now - leg["price"])
        return total

    centre = spot
    strikes = [l["strike"] for l in resolved if l["strike"]]
    low = min([centre * (1 - span)] + [k * 0.97 for k in strikes])
    high = max([centre * (1 + span)] + [k * 1.03 for k in strikes])
    grid = sorted(set([low + (high - low) * i / (points - 1) for i in range(points)] + strikes + [centre]))
    curve = [(s, value(s)) for s in grid]
    today_curve = [(s, today(s)) for s in grid] if priced else None
    breakevens = []
    for (a, va), (b, vb) in zip(curve, curve[1:]):
        if va == 0:
            breakevens.append(a)
        elif va * vb < 0:
            breakevens.append(a + (b - a) * va / (va - vb))
    # Upside is unbounded when the payoff still slopes at the top; downside ends where the underlying reaches zero.
    slope_high = value(high + 1) - value(high)
    at_zero = value(0.0)
    best = max([v for _, v in curve] + [at_zero])
    worst = min([v for _, v in curve] + [at_zero])
    net_premium = -sum(l["units"] * l["price"] for l in resolved if l["type"] != "FUT")
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
    if vol and years > 0:
        probabilities = {
            "available": True, "label": PROB_LABEL, "vol": vol, "vol_basis": vol_basis, "forward": forward, "years": years,
            "model": "risk-neutral lognormal on the forward at a single volatility; ignores skew, costs and early exit",
            "profit": profit_probability(value, strikes, forward, vol, years),
            "above_breakevens": [{"breakeven": b, "prob_above": prob_above(b, forward, vol, years)} for b in breakevens],
            "range_1sd": one_sd_range(forward, vol, years)}
    else:
        probabilities = {"available": False, "label": PROB_LABEL,
                         "note": "No implied volatility or no time left to expiry; probabilities not computed."}

    def marks(s):
        found = []
        if any(abs(s - round(k, 2)) < 1e-6 for k in strikes):
            found.append("strike")
        if any(abs(s - round(b, 2)) < 1e-6 for b in breakevens):
            found.append("breakeven")
        if abs(s - round(spot, 2)) < 1e-6:
            found.append("spot")
        return found

    table = [{"price": s, "expiry_pnl": value(s), "today_pnl": today(s) if priced else None, "marks": marks(s)}
             for s in table_prices(low, high, strikes + breakevens + [spot], table_rows)]
    return {"legs": [{k: v for k, v in l.items() if k != "premium"} for l in resolved], "lot_size": lot,
            "net_premium": net_premium, "max_profit": None if slope_high > 1e-9 else best,
            "max_loss": None if slope_high < -1e-9 else worst, "breakevens": breakevens,
            "extreme_at_zero": {"profit": best == at_zero and at_zero > max(v for _, v in curve), "loss": worst == at_zero and at_zero < min(v for _, v in curve)},
            "curve": curve, "today_curve": today_curve, "table": table, "position_greeks": net, "probabilities": probabilities,
            "today_note": ("Today (T+0): mark-to-market P&L if the underlying moved there now; Black-76 per leg at its implied volatility "
                           "(held constant) and the current time to expiry." if today_curve else
                           "Today (T+0) value unavailable: at least one option leg has no implied volatility."),
            "costs_note": "Excludes brokerage, STT, exchange charges, GST, stamp duty and slippage; margin for short legs comes from your broker (basket margin)."}


def svg_chart(report, result):
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
<text x="{left}" y="22" font-size="15" font-weight="600" fill="#111827">{report['underlying']} · expiry {report['expiry']} · payoff at expiry{' and today' if today else ''}</text>
<text x="{left}" y="40" font-size="12" fill="#4b5563">{legs} · lot {result['lot_size']} · ₹, excluding costs · data {report['data_date']}</text>
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
    lines = [f"## Day over day (same expiry {dod['expiry']})", "",
             f"| Measure | {dod['previous_date']} | {dod['current_date']} | Change |", "| --- | ---: | ---: | ---: |"]
    for label, key, show, change in rows:
        lines.append(f"| {label} | {show(m[key]['previous'])} | {show(m[key]['current'])} | {change(m[key]['change'])} |")
    return lines + ["", f"{dod['note']} ATM IV and max pain are recomputed for each day with the same method.", ""]


def chain_markdown(r, vix=None):
    lot = r["lot_size"]
    rng = r.get("range_1sd")
    lines = [f"# {r['underlying']} options — expiry {r['expiry']}", "",
             f"Data: {r['source']} for {r['data_date']} · {r['days_to_expiry']:.1f} days to expiry · lot size {lot}", "",
             f"> {r['disclaimer']}", "", "## Summary", "",
             "| Measure | Value |", "| --- | ---: |",
             f"| Spot | {fmt(r['spot'])} |",
             f"| Forward ({r['forward_basis']}) | {fmt(r['forward'])} |",
             f"| PCR (open interest) | {fmt(r['pcr_oi'])} |",
             f"| PCR (volume) | {fmt(r['pcr_volume'])} |",
             f"| Max pain | {fmt(r['max_pain'], 0)} |",
             f"| ATM strike / ATM IV | {fmt(r['atm_strike'], 0)} / {pct(r['atm_iv'])} |",
             f"| ATM straddle (expected move to expiry) | ±{fmt(r['straddle'])} ({pct(r['straddle'] / r['spot'] if r['straddle'] else None)}) |",
             f"| IV-implied 1-sigma move to expiry | ±{fmt(r['expected_move_iv'])} |",
             f"| 68% implied range at expiry (±1 SD, lognormal; {PROB_LABEL}) | {fmt(rng[0], 0) + ' – ' + fmt(rng[1], 0) if rng else 'n/a'} |",
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
    lines += [""] + dod_section(r)
    lines += ["## Positioning", "",
              f"- **Highest call OI above spot (resistance by OI):** {', '.join(fmt(k, 0) for k in r['resistance_oi']) or 'n/a'}",
              f"- **Highest put OI below spot (support by OI):** {', '.join(fmt(k, 0) for k in r['support_oi']) or 'n/a'}", ""]
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
    lines += ["", f"Other expiries: {', '.join(r['expiries'])}", "",
              f"Method: Black-76 on the forward, rate assumption {pct(r['rate_assumption'])}. {r['otm_note']} The 68% range is a risk-neutral "
              f"lognormal at the ATM IV ({PROB_LABEL}). End-of-day data; use a broker connector chain (--input) for live prices.", ""]
    return "\n".join(lines)


def leg_name(l):
    return f"{l['side']} {l['lots']} lot{'s' if l['lots'] > 1 else ''} {l['type']}" + ("" if l["type"] == "FUT" else f" {l['strike']:g}")


def extreme_text(p, key):
    if p[key] is None:
        return "unlimited"
    return fmt(p[key]) + (" (only if the price fell to zero)" if p["extreme_at_zero"]["profit" if key == "max_profit" else "loss"] else "")


def payoff_markdown(r, p):
    lines = [f"# {r['underlying']} strategy payoff — expiry {r['expiry']}", "",
             f"Data: {r['source']} for {r['data_date']} · spot {fmt(r['spot'])} · lot size {p['lot_size']}", "",
             f"> {r['disclaimer']}", "", "| Leg | Price used | IV | Units |", "| --- | ---: | ---: | ---: |"]
    for l in p["legs"]:
        lines.append(f"| {leg_name(l)} | {fmt(l['price'])} | {pct(l['iv']) if l['type'] != 'FUT' else ''} | {l['units']:+,} |")
    g = p["position_greeks"]
    lines += ["", "| Result at expiry | ₹ |", "| --- | ---: |",
              f"| Net premium ({'received' if p['net_premium'] > 0 else 'paid'}) | {fmt(abs(p['net_premium']))} |",
              f"| Maximum profit | {extreme_text(p, 'max_profit')} |",
              f"| Maximum loss | {extreme_text(p, 'max_loss')} |",
              f"| Breakeven(s) | {', '.join(fmt(b) for b in p['breakevens']) or 'none in range'} |"]
    if g:
        lines += ["", f"Position Greeks now: delta {fmt(g['delta'], 1)} units, gamma {fmt(g['gamma'], 3)}, theta ₹{fmt(g['theta'])}/day, vega ₹{fmt(g['vega'])} per vol point."]
    pr = p["probabilities"]
    lines += ["", f"## Probabilities at expiry ({PROB_LABEL})", ""]
    if pr["available"]:
        rng = pr["range_1sd"]
        lines += [f"Risk-neutral lognormal on the forward {fmt(pr['forward'])} at {pct(pr['vol'])} ({pr['vol_basis']}), "
                  f"{fmt(pr['years'] * 365, 1)} days to expiry. It ignores skew, costs and closing early.", "",
                  "| Measure | Value |", "| --- | ---: |",
                  f"| Probability of profit at expiry | {pct(pr['profit'])} |"]
        lines += [f"| Probability of finishing above breakeven {fmt(b['breakeven'])} | {pct(b['prob_above'])} |" for b in pr["above_breakevens"]]
        lines += [f"| 68% range at expiry (±1 SD) | {fmt(rng[0], 0)} – {fmt(rng[1], 0)} |"]
    else:
        lines.append(pr["note"])
    lines += ["", "## P&L by underlying price", "", "| Underlying | At expiry ₹ | Today (T+0) ₹ | Note |", "| ---: | ---: | ---: | --- |"]
    for t in p["table"]:
        s = t["price"]
        lines.append(f"| {fmt(s, 2 if s != round(s) else 0)} | {fmt(t['expiry_pnl'], 0)} | {fmt(t['today_pnl'], 0)} | {', '.join(t['marks'])} |")
    lines += ["", p["today_note"], "", p["costs_note"], ""]
    return "\n".join(lines)


def chain_summary(r, vix=None):
    dod = r.get("day_over_day") or {"status": "skipped", "note": "Day-over-day comparison not run."}
    ok = dod["status"] == "ok"
    change = lambda key: dod["metrics"][key]["change"] if ok else None
    return {"schema": "fno_chain_summary_v1", "underlying": r["underlying"], "expiry": r["expiry"], "days_to_expiry": r["days_to_expiry"],
            "data_date": r["data_date"], "source": r["source"], "spot": r["spot"], "forward": r["forward"], "lot_size": r["lot_size"],
            "pcr_oi": r["pcr_oi"], "pcr_oi_change": change("pcr_oi"), "max_pain": r["max_pain"], "max_pain_change": change("max_pain"),
            "atm_iv": r["atm_iv"], "atm_iv_change": change("atm_iv"), "previous_date": dod.get("previous_date"),
            "day_over_day_note": None if ok else dod["note"], "expected_move": r["straddle"], "expected_move_iv": r["expected_move_iv"],
            "range_1sd": r.get("range_1sd"), "vix": vix, "resistance_oi": r["resistance_oi"], "support_oi": r["support_oi"],
            "call_oi_change": r["call_oi_change"], "put_oi_change": r["put_oi_change"],
            "disclaimer": DISCLAIMER, "execution_eligible": False, "submitted": False}


def short_source(source):
    return "NSE F&O bhavcopy, end of day" if source.startswith("NSE F&O bhavcopy") else source


def chain_summary_markdown(s):
    vs = lambda c, text: "" if c is None else f" ({text} vs {s['previous_date']})"
    lot = s["lot_size"]
    move = (f"±{fmt(s['expected_move'])} ({pct(s['expected_move'] / s['spot'])}) by ATM straddle" if s["expected_move"] else
            f"±{fmt(s['expected_move_iv'])} (IV-implied)" if s["expected_move_iv"] else "n/a")
    rng = s["range_1sd"]
    lines = [f"**{s['underlying']} options, expiry {s['expiry']} ({s['days_to_expiry']:.1f} days)** — data {s['data_date']} ({short_source(s['source'])})",
             f"- Spot {fmt(s['spot'])} · forward {fmt(s['forward'])}",
             f"- PCR (OI) {fmt(s['pcr_oi'])}{vs(s['pcr_oi_change'], signed(s['pcr_oi_change']))}",
             f"- Max pain {fmt(s['max_pain'], 0)}{vs(s['max_pain_change'], signed(s['max_pain_change'], 0))}",
             f"- ATM IV {pct(s['atm_iv'])}{vs(s['atm_iv_change'], signed(s['atm_iv_change'] * 100 if s['atm_iv_change'] is not None else None, 1) + ' pts')}",
             f"- Expected move to expiry {move}" + (f"; 68% implied range {fmt(rng[0], 0)}–{fmt(rng[1], 0)}" if rng else "")]
    if s["vix"]:
        lines.append(f"- India VIX {vix_line(s['vix'])}" + (f" — {s['vix']['note']}" if s["vix"].get("note") else ""))
    lines += [f"- Call OI walls above spot: {', '.join(fmt(k, 0) for k in s['resistance_oi']) or 'n/a'} · put OI walls below spot: "
              f"{', '.join(fmt(k, 0) for k in s['support_oi']) or 'n/a'}",
              f"- Net OI change on the day: calls {signed_contracts(s['call_oi_change'], lot)}, puts {signed_contracts(s['put_oi_change'], lot)} contracts"]
    if s["day_over_day_note"]:
        lines.append(f"- Day over day: {s['day_over_day_note']}")
    lines += ["", "Positioning from public exchange data; not a prediction and not investment advice. Run without --summary for the full report.", ""]
    return "\n".join(lines)


def payoff_summary(r, p):
    pr = p["probabilities"]
    return {"schema": "fno_payoff_summary_v1", "underlying": r["underlying"], "expiry": r["expiry"], "data_date": r["data_date"],
            "source": r["source"], "spot": r["spot"], "atm_iv": r["atm_iv"], "expected_move": r["straddle"],
            "legs": [{k: l[k] for k in ("side", "type", "strike", "lots", "price", "units")} for l in p["legs"]], "lot_size": p["lot_size"],
            "net_premium": p["net_premium"], "max_profit": p["max_profit"], "max_loss": p["max_loss"], "extreme_at_zero": p["extreme_at_zero"],
            "breakevens": p["breakevens"], "probability_of_profit": pr.get("profit"), "above_breakevens": pr.get("above_breakevens", []),
            "range_1sd": pr.get("range_1sd"), "probability_label": PROB_LABEL, "probability_note": pr.get("note"),
            "today_pnl_at_spot": next((t["today_pnl"] for t in p["table"] if "spot" in t["marks"]), None),
            "costs_note": p["costs_note"], "disclaimer": DISCLAIMER, "execution_eligible": False, "submitted": False}


def payoff_summary_markdown(s):
    legs = " · ".join(f"{leg_name(l)} @ {fmt(l['price'])}" for l in s["legs"])
    bes = ", ".join(f"{fmt(b['breakeven'])} (P above {pct(b['prob_above'])})" for b in s["above_breakevens"]) or \
        (", ".join(fmt(b) for b in s["breakevens"]) or "none in range")
    extreme = lambda key: "unlimited" if s[key] is None else fmt(s[key]) + (" (only at zero)" if s["extreme_at_zero"]["profit" if key == "max_profit" else "loss"] else "")
    rng = s["range_1sd"]
    lines = [f"**{s['underlying']} payoff, expiry {s['expiry']}** — data {s['data_date']} ({short_source(s['source'])})",
             f"- Legs: {legs} (lot {s['lot_size']})",
             f"- Spot {fmt(s['spot'])} · ATM IV {pct(s['atm_iv'])} · ATM straddle ±{fmt(s['expected_move'])}",
             f"- Net premium {'received' if s['net_premium'] > 0 else 'paid'} ₹{fmt(abs(s['net_premium']))}",
             f"- Max profit ₹{extreme('max_profit')} · max loss ₹{extreme('max_loss')}",
             f"- Breakeven(s): {bes}"]
    if s["probability_of_profit"] is not None:
        lines += [f"- Probability of profit at expiry {pct(s['probability_of_profit'])} ({PROB_LABEL})",
                  f"- 68% implied range at expiry {fmt(rng[0], 0)}–{fmt(rng[1], 0)}"]
    else:
        lines.append(f"- Probabilities: {s['probability_note']}")
    if s["today_pnl_at_spot"] is not None:
        lines.append(f"- Today (T+0) P&L at current spot ₹{fmt(s['today_pnl_at_spot'], 0)}")
    lines += ["", "Excludes costs; margin for short legs comes from your broker. Not investment advice. Run without --summary for the full report.", ""]
    return "\n".join(lines)


def public(report):
    return {k: v for k, v in report.items() if not k.startswith("_")}


def main(argv=None, *, fetcher=request, clock=instant):
    p = argparse.ArgumentParser(description="NSE F&O analytics: option chain, OI, PCR, max pain, IV/Greeks and strategy payoff (research only).")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("chain", "expiries", "payoff"):
        s = sub.add_parser(name)
        s.add_argument("symbol", help="NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY or an F&O stock such as RELIANCE")
        s.add_argument("--expiry", help="YYYY-MM-DD; default nearest")
        s.add_argument("--date", help="Bhavcopy trading date YYYY-MM-DD; default latest available")
        s.add_argument("--input", help="fno_chain_v1 JSON, e.g. from a broker connector")
        s.add_argument("--rate", type=float, default=DEFAULT_RATE, help="Annual risk-free rate assumption (default 0.065)")
        s.add_argument("--format", choices=["markdown", "json"], default="markdown")
        s.add_argument("--out", help="Write to a new file")
        if name in ("chain", "payoff"):
            s.add_argument("--summary", action="store_true", help="Short answer-first block instead of the full report")
        if name == "chain":
            s.add_argument("--strikes", type=int, default=12, help="Strikes each side of ATM")
            s.add_argument("--no-vix", action="store_true")
            s.add_argument("--no-compare", action="store_true", help="Skip the previous trading day's bhavcopy (one less ~1.5 MB download)")
            s.add_argument("--previous-date", help="Compare with the bhavcopy on or before this YYYY-MM-DD (default: the trading day before the data date; needed with --input)")
        if name == "payoff":
            s.add_argument("--leg", action="append", required=True, help="SIDE:TYPE:STRIKE:LOTS[:PREMIUM], e.g. SELL:CE:25800:1; futures BUY:FUT::1")
            s.add_argument("--range", type=float, default=8, help="Percent either side of spot")
            s.add_argument("--chart", help="Write an SVG payoff chart to a new file")
    a = p.parse_args(argv)
    try:
        selected = underlying(a.symbol)
        if a.input:
            contracts, source, as_of = load_input(a.input, selected)
        else:
            contracts, source = fetch_bhavcopy(selected, fetcher=fetcher, today=clock().date(), on=a.date)
            as_of = None
            source = "NSE F&O bhavcopy " + source
        if a.command == "expiries":
            trade = contracts[0]["trade_date"]
            result = {"underlying": selected, "data_date": trade, "expiries": expiries(contracts, trade)}
            text = json.dumps(result, indent=2) if a.format == "json" else f"{selected} option expiries after {trade}: " + ", ".join(result["expiries"]) + "\n"
        else:
            report = analyse_chain(contracts, selected, expiry=a.expiry, as_of=as_of, rate=a.rate,
                                   width=getattr(a, "strikes", 12), source=source)
            if a.command == "chain":
                vix = None
                if selected in INDICES and not a.no_vix:
                    try:
                        vix = vix_context(fetcher=fetcher, now=clock(), on=report["data_date"])
                    except (Block, ValueError, KeyError, TypeError):
                        vix = None
                report["vix"] = vix
                if a.no_compare:
                    report["day_over_day"] = {"status": "skipped", "note": "Day-over-day comparison skipped (--no-compare)."}
                elif a.input and not a.previous_date:
                    report["day_over_day"] = {"status": "skipped", "note": "Input chain: pass --previous-date YYYY-MM-DD to compare with that day's NSE bhavcopy."}
                else:
                    report["day_over_day"] = compare_previous(report, selected, fetcher=fetcher, on=a.previous_date)
                if a.summary:
                    summary = chain_summary(report, vix)
                    text = json.dumps(summary, indent=2, default=str) if a.format == "json" else chain_summary_markdown(summary)
                else:
                    text = json.dumps(public(report), indent=2, default=str) if a.format == "json" else chain_markdown(report, vix)
            else:
                result = payoff(report, [parse_leg(l) for l in a.leg], span=a.range / 100)
                if a.chart:
                    chart_path = Path(a.chart)
                    if chart_path.exists():
                        raise Block("FNO: chart file exists; choose a new path")
                    chart_path.parent.mkdir(parents=True, exist_ok=True)
                    chart_path.write_text(svg_chart(report, result), encoding="utf-8")
                result["chart"] = a.chart
                if a.summary:
                    summary = payoff_summary(report, result)
                    text = json.dumps(summary, indent=2, default=str) if a.format == "json" else payoff_summary_markdown(summary)
                else:
                    text = json.dumps({"report": public(report), "payoff": result}, indent=2, default=str) if a.format == "json" else payoff_markdown(report, result)
    except Block as error:
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
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
