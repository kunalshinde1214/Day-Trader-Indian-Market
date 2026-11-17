"""Personal holdings review for Indian equity investors.

Reads a holdings file (normalized holdings_v1 JSON, a Zerodha Console holdings
CSV or a Zerodha tradebook CSV) and an optional tradebook for tax lots, then
reports P&L, weights, concentration, sector exposure and, when lots are known,
an estimate of capital-gains tax for one Indian financial year (FIFO lots).

Research only: no orders, no recommendation to buy, sell or harvest. Prices are
public Yahoo daily closes with their dates; a price that fails stays visible as
unavailable and is never guessed. Tax figures are estimates with labelled
default rates; check current rules with a tax professional.
"""
import argparse
import calendar
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import io
import json
from pathlib import Path
import re
import sys
import time

from core import Block, IST, instant
from public_market import request, symbol as check_symbol

D = Decimal
NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
OTHER_SECTOR = "Other / not in Nifty 500"
MAX_BYTES = 20 * 1024 * 1024
RATE_DATE = date(2024, 7, 23)
GRANDFATHER_DATE = date(2018, 2, 1)
DEFAULT_RATES = {"stcg_rate": 0.20, "ltcg_rate": 0.125, "ltcg_exemption": 125000.0, "cess_rate": 0.04}
RATE_BASIS = ("Default rates — verify current rates: STCG 20% (s.111A) and LTCG 12.5% (s.112A) above an annual "
              "exemption of ₹1,25,000, applicable to transfers on or after 23 July 2024 under the Finance (No. 2) Act 2024; "
              "health and education cess 4% on the tax.")
NOT_MODELLED = [
    "Surcharge on capital-gains tax",
    "Grandfathering of shares bought before 1 Feb 2018 (cost = higher of cost and 31 Jan 2018 FMV)",
    "Bonus, split, merger and demerger cost adjustments",
    "Set-off of capital losses against other income, and carry-forward of losses to later years",
    "Intraday and F&O trades (business income)",
    "Brokerage and exchange charges as transfer expenses (STT is not deductible)",
    "Basic exemption limit, section 87A rebate and non-resident (TDS) treatment",
    "Older rates for transfers before 23 July 2024 (15% STCG, 10% LTCG above ₹1,00,000)",
]
DISCLAIMER = ("For information only; not investment or tax advice and not a recommendation to buy, sell or "
              "harvest gains or losses. Tax figures are estimates; check with a tax professional.")
HOLDINGS_CSV = {"instrument": "Instrument", "qty.": "Qty.", "avg. cost": "Avg. cost"}
TRADEBOOK_CSV = ["symbol", "trade_date", "segment", "trade_type", "quantity", "price"]


# ---------------------------------------------------------------- parsing

def decimal(value, name, allow_zero=True):
    text = str(value).strip().replace(",", "").replace("₹", "") if value is not None else ""
    try:
        if isinstance(value, bool) or text == "":
            raise InvalidOperation
        v = D(text)
    except (InvalidOperation, ValueError):
        raise Block(f"INPUT: {name} must be a number (got {str(value)[:20]!r})") from None
    if not v.is_finite():
        raise Block(f"INPUT: {name} must be finite")
    if v < 0:
        raise Block(f"INPUT: {name} cannot be negative (got {text})")
    if v == 0 and not allow_zero:
        raise Block(f"INPUT: {name} must be positive")
    return v


def stock(raw):
    """Return (display symbol, Yahoo symbol). Accepts RELIANCE, reliance, RELIANCE.NS, RELIANCE.BO."""
    text = str(raw or "").strip().upper()
    if text.startswith(("NSE:", "BSE:")):
        text = text[4:]
    if not re.fullmatch(r"[A-Z0-9&_.-]{1,30}", text):
        raise Block(f"INPUT: unsupported symbol {str(raw)[:30]!r}; use an NSE symbol such as RELIANCE")
    if text.endswith((".NS", ".BO")):
        return text[:-3], check_symbol(text)
    return text, check_symbol(text + ".NS")


def trade_date(value, name="date"):
    text = str(value or "").strip()[:10]
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise Block(f"INPUT: {name} must be a date such as 2025-04-01 (got {text!r})")


def blank(row):
    return all(not str(v or "").strip() for v in row.values())


def holding(raw_symbol, quantity, price, notes):
    q = decimal(quantity, f"quantity for {raw_symbol}")
    if q == 0:
        notes.append(f"{raw_symbol}: zero quantity row skipped")
        return None
    name, yahoo = stock(raw_symbol)
    return {"symbol": name, "yahoo": yahoo, "quantity": q, "average_price": decimal(price, f"average price for {name}")}


def merge(rows, notes):
    merged = {}
    for h in rows:
        if h is None:
            continue
        if h["symbol"] in merged:
            m = merged[h["symbol"]]
            cost = m["quantity"] * m["average_price"] + h["quantity"] * h["average_price"]
            m["quantity"] += h["quantity"]
            m["average_price"] = cost / m["quantity"]
            notes.append(f"{h['symbol']}: duplicate rows merged (quantity-weighted average cost)")
        else:
            merged[h["symbol"]] = dict(h)
    return list(merged.values())


def lot(raw_symbol, day, side, quantity, price, at="", seq=0):
    side = str(side or "BUY").strip().upper()
    if side not in {"BUY", "SELL"}:
        raise Block(f"INPUT: side/trade_type must be buy or sell for {raw_symbol} (got {side!r})")
    name, _ = stock(raw_symbol)
    return {"symbol": name, "date": trade_date(day, f"date for {name}"), "side": side,
            "quantity": decimal(quantity, f"lot quantity for {name}", allow_zero=False),
            "price": decimal(price, f"lot price for {name}"), "time": str(at or ""), "seq": seq}


def parse_tradebook(rows, notes):
    lots, ignored = [], 0
    for n, r in enumerate(rows):
        if blank(r):
            continue
        if str(r.get("segment", "")).strip().upper() != "EQ":
            ignored += 1
            continue
        lots.append(lot(r["symbol"], r["trade_date"], r["trade_type"], r["quantity"], r["price"], r.get("order_execution_time", ""), n))
    if ignored:
        notes.append(f"{ignored} tradebook row(s) outside the equity segment (EQ) ignored; F&O and other segments are not modelled")
    if not lots:
        raise Block("INPUT: tradebook has no equity (segment EQ) rows")
    return lots


def parse_text(text, name="input"):
    """Detect and parse one input. Returns {kind, holdings, lots, as_of, source, notes}."""
    notes = []
    if text.startswith("﻿"):
        text = text[1:]
    if text.lstrip().startswith("{"):
        try:
            doc = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(Block("INPUT: non-finite JSON")))
        except json.JSONDecodeError:
            raise Block(f"INPUT: {name} is not valid JSON") from None
        if not isinstance(doc, dict) or doc.get("schema") != "holdings_v1" or not isinstance(doc.get("holdings"), list):
            raise Block('INPUT: JSON must be {"schema":"holdings_v1","holdings":[{"symbol","quantity","average_price"}], "lots":[optional]}')
        rows = []
        for h in doc["holdings"]:
            if not isinstance(h, dict) or not {"symbol", "quantity", "average_price"} <= h.keys():
                raise Block("INPUT: each holding needs symbol, quantity and average_price")
            rows.append(holding(h["symbol"], h["quantity"], h["average_price"], notes))
        lots = []
        for n, x in enumerate(doc.get("lots") or []):
            if not isinstance(x, dict) or not {"symbol", "quantity", "price", "date"} <= x.keys():
                raise Block("INPUT: each lot needs symbol, quantity, price, date and side (BUY/SELL)")
            lots.append(lot(x["symbol"], x["date"], x.get("side", "BUY"), x["quantity"], x["price"], x.get("time", ""), n))
        as_of = doc.get("as_of")
        if as_of is not None:
            try:
                datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
            except ValueError:
                raise Block("INPUT: as_of must be an ISO date or timestamp") from None
        return {"kind": "holdings_v1", "holdings": merge(rows, notes), "lots": lots, "as_of": as_of,
                "source": str(doc.get("source") or "holdings_v1"), "notes": notes}
    reader = csv.DictReader(io.StringIO(text))
    headers = {str(h or "").strip().lower(): h for h in (reader.fieldnames or [])}
    raw_rows = [{k: r.get(v) for k, v in headers.items()} for r in reader]
    if set(HOLDINGS_CSV) <= headers.keys():
        rows = []
        for r in raw_rows:
            if blank(r) or not str(r.get("instrument") or "").strip():
                continue
            rows.append(holding(r["instrument"], r["qty."], r["avg. cost"], notes))
        if not any(rows):
            raise Block("INPUT: holdings CSV has no rows")
        return {"kind": "zerodha_holdings_csv", "holdings": merge(rows, notes), "lots": [], "as_of": None,
                "source": "Zerodha Console holdings CSV", "notes": notes}
    if set(TRADEBOOK_CSV) <= headers.keys():
        return {"kind": "zerodha_tradebook_csv", "holdings": None, "lots": parse_tradebook(raw_rows, notes), "as_of": None,
                "source": "Zerodha tradebook CSV", "notes": notes}
    raise Block(f"INPUT: unrecognised format for {name}. Expected holdings_v1 JSON; a Zerodha holdings CSV with columns "
                f"{', '.join(HOLDINGS_CSV.values())}; or a Zerodha tradebook CSV with columns {', '.join(TRADEBOOK_CSV)}")


def load(path):
    p = Path(path)
    if not p.is_file():
        raise Block(f"INPUT: file not found: {path}")
    raw = p.read_bytes()
    if len(raw) > MAX_BYTES:
        raise Block("INPUT: file larger than 20 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Block(f"INPUT: {p.name} is not UTF-8 text; export CSV or JSON") from None
    return parse_text(text, p.name)


# ---------------------------------------------------------------- dates and FIFO

def add_months(day, months):
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def long_term_from(bought):
    """First sale date that counts as long-term: the day after the 12-month anniversary."""
    return date.fromordinal(add_months(bought, 12).toordinal() + 1)


def is_long_term(bought, sold):
    """Listed equity is long-term when the sale date is MORE than 12 calendar months after the purchase date.

    Bought 2025-01-10: sold 2026-01-10 (exactly 12 months, 365 days) is short-term; sold 2026-01-11 is long-term.
    Across a leap day 366 days can still be exactly 12 months (2023-06-01 to 2024-06-01), which stays short-term.
    """
    return sold > add_months(bought, 12)


def financial_year(text=None, today=None):
    if text is None:
        today = today or instant().date()
        start = today.year if today.month >= 4 else today.year - 1
        return f"{start}-{(start + 1) % 100:02d}", date(start, 4, 1), date(start + 1, 3, 31)
    m = re.fullmatch(r"(\d{4})-(\d{2})", str(text).strip())
    if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        raise Block("FY: use the Indian financial year form 2026-27 (April to March)")
    start = int(m.group(1))
    return m.group(0), date(start, 4, 1), date(start + 1, 3, 31)


def fifo(lots):
    """Match sells to the oldest open buys per symbol. Returns matches, open lots, unmatched sells, notes."""
    order = sorted(lots, key=lambda x: (x["symbol"], x["date"], x["time"], 0 if x["side"] == "BUY" else 1, x["seq"]))
    queues, matches, unmatched, notes = {}, [], [], []
    days = {}
    for x in order:
        days.setdefault((x["symbol"], x["date"]), set()).add(x["side"])
        queue = queues.setdefault(x["symbol"], [])
        if x["side"] == "BUY":
            queue.append({"symbol": x["symbol"], "date": x["date"], "quantity": x["quantity"], "price": x["price"]})
            continue
        left = x["quantity"]
        while left > 0 and queue:
            head = queue[0]
            take = min(left, head["quantity"])
            matches.append({"symbol": x["symbol"], "buy_date": head["date"], "sell_date": x["date"], "quantity": take,
                            "buy_price": head["price"], "sell_price": x["price"], "gain": (x["price"] - head["price"]) * take,
                            "days_held": (x["date"] - head["date"]).days,
                            "term": "long" if is_long_term(head["date"], x["date"]) else "short"})
            head["quantity"] -= take
            left -= take
            if head["quantity"] == 0:
                queue.pop(0)
        if left > 0:
            unmatched.append({"symbol": x["symbol"], "date": x["date"], "quantity": left})
    for (sym, day), sides in sorted(days.items()):
        if sides == {"BUY", "SELL"}:
            notes.append(f"{sym}: buy and sell on {day.isoformat()}; if intraday this is business income (not modelled) — FIFO treated it as delivery")
    open_lots = [l for q in queues.values() for l in q if l["quantity"] > 0]
    return matches, open_lots, unmatched, notes


def tax(st_items, lt_items, rates):
    """Set-off within this report: short-term losses offset short- then long-term gains; long-term losses offset only long-term gains."""
    st_gain = sum((g for g in st_items if g > 0), D(0))
    st_loss = -sum((g for g in st_items if g < 0), D(0))
    lt_gain = sum((g for g in lt_items if g > 0), D(0))
    lt_loss = -sum((g for g in lt_items if g < 0), D(0))
    st_net, lt_net = st_gain - st_loss, lt_gain - lt_loss
    unused_lt = max(-lt_net, D(0))
    lt_net = max(lt_net, D(0))
    st_against_lt = D(0)
    if st_net < 0:
        st_against_lt = min(-st_net, lt_net)
        lt_net -= st_against_lt
    unused_st = max(-st_net, D(0)) - st_against_lt
    st_net = max(st_net, D(0))
    exemption = D(str(rates["ltcg_exemption"]))
    used = min(lt_net, exemption)
    taxable_lt = lt_net - used
    st_tax = st_net * D(str(rates["stcg_rate"]))
    lt_tax = taxable_lt * D(str(rates["ltcg_rate"]))
    cess = (st_tax + lt_tax) * D(str(rates["cess_rate"]))
    out = {"short_term_gains": st_gain, "short_term_losses": st_loss, "long_term_gains": lt_gain, "long_term_losses": lt_loss,
           "short_term_loss_set_off_against_long_term": st_against_lt,
           "short_term_after_set_off": st_net, "long_term_after_set_off": lt_net,
           "ltcg_exemption_used": used, "ltcg_exemption_remaining": exemption - used,
           "taxable_short_term": st_net, "taxable_long_term": taxable_lt,
           "stcg_tax": st_tax, "ltcg_tax": lt_tax, "cess": cess, "total_tax": st_tax + lt_tax + cess,
           "unused_short_term_loss": unused_st, "unused_long_term_loss": unused_lt}
    return {k: f(v) for k, v in out.items()}


# ---------------------------------------------------------------- market data

def f(value, digits=2):
    return None if value is None else round(float(value), digits)


def latest_price(yahoo, *, fetcher, clock):
    # Five years of daily bars so splits and bonuses behind an old average cost are still detected.
    from stock_analysis import daily_history
    history = daily_history(yahoo, fetcher=fetcher, now=clock(), years=5)
    bars = history["bars"]
    last = bars[-1]
    previous = bars[-2]["close"] if len(bars) > 1 else None
    splits = sorted({datetime.fromtimestamp(int(s.get("date", k)), IST).date().isoformat()
                     for k, s in (history.get("corporate_actions", {}).get("splits") or {}).items()})
    return {"price": D(str(last["close"])), "previous_close": None if previous is None else D(str(previous)),
            "price_date": last["at"][:10], "splits": splits}


def sectors(*, fetcher):
    raw = fetcher(NIFTY500_URL)
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    if not rows:
        raise Block("SECTOR: empty Nifty 500 list")
    out = {}
    for r in rows:
        r = {str(k or "").strip(): str(v or "").strip() for k, v in r.items()}
        if not {"Symbol", "Industry"} <= r.keys():
            raise Block("SECTOR: unexpected Nifty 500 CSV columns")
        if r["Symbol"]:
            out[r["Symbol"].upper()] = r["Industry"] or OTHER_SECTOR
    return out


def hhi_reading(value):
    if value is None:
        return "n/a"
    if value < 0.10:
        return "diversified"
    if value < 0.18:
        return "moderately concentrated"
    return "highly concentrated"


# ---------------------------------------------------------------- review

def review(source, tradebook=None, *, fy=None, rates=None, prices=True, fetcher=request, clock=instant, sleep=time.sleep):
    now = clock()
    today = now.date()
    rates = {**DEFAULT_RATES, **(rates or {})}
    for k, v in rates.items():
        if v is None or v < 0 or (k != "ltcg_exemption" and v > 1):
            raise Block(f"RATES: {k} must be between 0 and 1 (exemption: a non-negative rupee amount)")
    fy_label, fy_start, fy_end = financial_year(fy, today)
    notes = list(source["notes"])
    lots = list(source["lots"])
    if tradebook is not None:
        notes += tradebook["notes"]
        lots += [{**x, "seq": x["seq"] + 1_000_000} for x in tradebook["lots"]]
    matches, open_lots, unmatched, fifo_notes = fifo(lots) if lots else ([], [], [], [])
    notes += fifo_notes
    if source["holdings"] is None:
        derived = {}
        for l in open_lots:
            d = derived.setdefault(l["symbol"], {"quantity": D(0), "cost": D(0)})
            d["quantity"] += l["quantity"]
            d["cost"] += l["quantity"] * l["price"]
        holdings = [{"symbol": s, "yahoo": stock(s)[1], "quantity": d["quantity"], "average_price": d["cost"] / d["quantity"]}
                    for s, d in sorted(derived.items())]
        notes.append("Holdings derived from open FIFO lots in the tradebook; transfers, bonus/split and pre-tradebook buys are missing")
    else:
        holdings = source["holdings"]
    if not holdings:
        raise Block("INPUT: no open holdings to review")

    errors, quotes, sector_map = {}, {}, None
    if prices:
        for n, h in enumerate(holdings):
            if n:
                sleep(0.3)
            try:
                quotes[h["symbol"]] = latest_price(h["yahoo"], fetcher=fetcher, clock=clock)
            except (Block, KeyError, TypeError, ValueError, OSError, OverflowError, IndexError):
                errors[h["symbol"]] = "price unavailable (Yahoo daily chart failed or inconsistent); not estimated"
        try:
            sector_map = sectors(fetcher=fetcher)
        except (Block, KeyError, TypeError, ValueError, OSError, UnicodeDecodeError):
            errors["sectors"] = "Nifty 500 industry list unavailable; sector exposure not computed"
    else:
        errors["prices"] = "--no-prices: values shown at average cost; no P&L, day change or sector data"

    rows = []
    for h in holdings:
        q, avg = h["quantity"], h["average_price"]
        invested = q * avg
        quote = quotes.get(h["symbol"])
        value = q * quote["price"] if quote else None
        day = q * (quote["price"] - quote["previous_close"]) if quote and quote["previous_close"] is not None else None
        stale = bool(quote) and (today - date.fromisoformat(quote["price_date"])).days > 7
        sector = None if sector_map is None else sector_map.get(h["symbol"], OTHER_SECTOR)
        rows.append({"symbol": h["symbol"], "quantity": q, "average_price": avg, "last_price": quote["price"] if quote else None,
                     "price_date": quote["price_date"] if quote else None, "price_stale": stale,
                     "price_status": "ok" if quote else ("not loaded" if not prices else "price unavailable"),
                     "invested": invested, "current_value": value, "pnl": None if value is None else value - invested,
                     "pnl_pct": None if value is None or invested == 0 else (value - invested) / invested,
                     "day_change": day, "previous_value": None if day is None else value - day,
                     "weight_basis": value if value is not None else invested, "valued_at_cost": value is None,
                     "sector": sector, "splits_recent": quote["splits"] if quote else []})
        if stale:
            notes.append(f"{h['symbol']}: latest price is from {quote['price_date']} (more than 7 days old)")
        if quote and quote["splits"]:
            notes.append(f"{h['symbol']}: split/bonus recorded on {', '.join(quote['splits'])}; check that quantity and average cost are adjusted")
    total_basis = sum((r["weight_basis"] for r in rows), D(0))
    for r in rows:
        r["weight"] = r["weight_basis"] / total_basis if total_basis else None
    rows.sort(key=lambda r: r["weight_basis"], reverse=True)
    if any(r["valued_at_cost"] for r in rows) and prices:
        notes.append("Weights value holdings without a price at average cost; their P&L is not shown")

    priced = [r for r in rows if r["current_value"] is not None]
    invested_priced = sum((r["invested"] for r in priced), D(0))
    value_priced = sum((r["current_value"] for r in priced), D(0))
    day_rows = [r for r in priced if r["day_change"] is not None]
    day_total = sum((r["day_change"] for r in day_rows), D(0)) if day_rows else None
    prev_total = sum((r["previous_value"] for r in day_rows), D(0)) if day_rows else None
    weights = [float(r["weight"]) for r in rows if r["weight"] is not None]
    hhi = sum(w * w for w in weights) if weights else None
    price_dates = sorted({r["price_date"] for r in priced})
    totals = {"holdings": len(rows), "priced_holdings": len(priced), "invested": f(sum((r["invested"] for r in rows), D(0))),
              "invested_priced": f(invested_priced), "current_value": f(value_priced) if priced else None,
              "unrealised_pnl": f(value_priced - invested_priced) if priced else None,
              "unrealised_pnl_pct": f((value_priced - invested_priced) / invested_priced, 6) if priced and invested_priced else None,
              "day_change": f(day_total), "day_change_pct": f(day_total / prev_total, 6) if day_total is not None and prev_total else None,
              "price_dates": price_dates}
    concentration = {"top_holding": rows[0]["symbol"] if rows else None, "top_holding_weight": f(rows[0]["weight"], 6) if rows else None,
                     "top5_weight": f(sum(weights[:5]), 6) if weights else None, "herfindahl": f(hhi, 6),
                     "effective_holdings": f(1 / hhi, 2) if hhi else None, "reading": hhi_reading(hhi),
                     "basis": "current value; holdings without a price at average cost" if prices else "average cost"}
    sector_rows = None
    if sector_map is not None:
        grouped = {}
        for r in rows:
            g = grouped.setdefault(r["sector"], {"sector": r["sector"], "holdings": [], "value": D(0)})
            g["holdings"].append(r["symbol"])
            g["value"] += r["weight_basis"]
        sector_rows = sorted(({"sector": g["sector"], "holdings": g["holdings"], "value": f(g["value"]),
                               "weight": f(g["value"] / total_basis, 6) if total_basis else None} for g in grouped.values()),
                             key=lambda g: g["value"], reverse=True)

    tax_section = None
    if lots:
        price_of = {r["symbol"]: r["last_price"] for r in rows}
        in_fy = [m for m in matches if fy_start <= m["sell_date"] <= fy_end]
        realised = tax([m["gain"] for m in in_fy if m["term"] == "short"], [m["gain"] for m in in_fy if m["term"] == "long"], rates)
        open_out, unreal = [], {"short": [], "long": []}
        cost = {"short": D(0), "long": D(0)}
        for l in sorted(open_lots, key=lambda l: (l["symbol"], l["date"])):
            term = "long" if is_long_term(l["date"], today) else "short"
            price = price_of.get(l["symbol"])
            gain = None if price is None else (price - l["price"]) * l["quantity"]
            cost[term] += l["quantity"] * l["price"]
            if gain is not None:
                unreal[term].append(gain)
            open_out.append({"symbol": l["symbol"], "buy_date": l["date"].isoformat(), "quantity": f(l["quantity"], 4), "buy_price": f(l["price"]),
                             "term_today": term, "long_term_from": long_term_from(l["date"]).isoformat(),
                             "last_price": f(price), "unrealised_gain": f(gain)})
        lots_by_symbol = {}
        for l in open_lots:
            lots_by_symbol[l["symbol"]] = lots_by_symbol.get(l["symbol"], D(0)) + l["quantity"]
        for r in rows:
            held = lots_by_symbol.get(r["symbol"])
            if held is None:
                notes.append(f"{r['symbol']}: no tax lots supplied; not included in the unrealised short/long-term split")
            elif held != r["quantity"]:
                notes.append(f"{r['symbol']}: open lots total {qty(held)} shares but holdings show {qty(r['quantity'])}; lots may be incomplete")
        for u in unmatched:
            notes.append(f"{u['symbol']}: sell of {qty(u['quantity'])} on {u['date'].isoformat()} has no matching earlier buy in the file; its gain is not computed")
        if any(m["sell_date"] < RATE_DATE for m in in_fy):
            notes.append("Some sales in this FY are before 23 July 2024, when older rates applied (15% STCG; 10% LTCG above ₹1,00,000); the estimate uses the rates shown")
        if any(m["buy_date"] < GRANDFATHER_DATE for m in matches) or any(l["date"] < GRANDFATHER_DATE for l in open_lots):
            notes.append("Some lots were bought before 1 Feb 2018; grandfathering (31 Jan 2018 FMV) is not modelled, so long-term gains may be overstated")
        unrealised = {t: {"cost": f(cost[t]), "gain": f(sum(unreal[t], D(0))) if unreal[t] else None,
                          "lots": sum(1 for l in open_out if l["term_today"] == t),
                          "lots_without_price": sum(1 for l in open_out if l["term_today"] == t and l["unrealised_gain"] is None)}
                      for t in ("short", "long")}
        hypothetical = None
        if fy_start <= today <= fy_end and any(unreal.values()):
            combined = tax([m["gain"] for m in in_fy if m["term"] == "short"] + unreal["short"],
                           [m["gain"] for m in in_fy if m["term"] == "long"] + unreal["long"], rates)
            hypothetical = {"description": f"If every open lot with a price were sold at its last price, on top of the FY {fy_label} realised gains",
                            "estimated_total_tax": combined["total_tax"], "estimated_additional_tax": f(combined["total_tax"] - realised["total_tax"]),
                            "detail": combined}
        tax_section = {"financial_year": fy_label, "fy_start": fy_start.isoformat(), "fy_end": fy_end.isoformat(),
                       "method": "FIFO per symbol; long-term when the sale date is more than 12 months after the purchase date",
                       "rates": {**rates, "basis": RATE_BASIS},
                       "realised_matches": [{k: (v.isoformat() if isinstance(v, date) else f(v, 4) if k == "quantity" else f(v) if isinstance(v, Decimal) else v)
                                                for k, v in m.items()} for m in in_fy],
                       "realised": realised, "ltcg_exemption_remaining": realised["ltcg_exemption_remaining"],
                       "unrealised": unrealised, "open_lots": open_out, "hypothetical_sell_all": hypothetical,
                       "unmatched_sells": [{"symbol": u["symbol"], "date": u["date"].isoformat(), "quantity": f(u["quantity"], 4)} for u in unmatched],
                       "not_modelled": NOT_MODELLED,
                       "note": "For information; check with a tax professional. Not a recommendation to sell or harvest."}

    out_rows = []
    for r in rows:
        out_rows.append({k: (f(v, 4) if k == "quantity" else f(v, 6) if k in {"weight", "pnl_pct"} else f(v) if isinstance(v, Decimal) else v)
                         for k, v in r.items() if k not in {"weight_basis", "previous_value"}})
    return {"schema": "holdings_review_v1", "as_of": now.isoformat(), "source": source["source"], "input_format": source["kind"],
            "holdings_as_of": source["as_of"], "price_source": "Yahoo Finance public daily chart (SYMBOL.NS); latest daily close, may be delayed" if prices else None,
            "sector_source": NIFTY500_URL if sector_map is not None else None,
            "totals": totals, "holdings": out_rows, "concentration": concentration, "sectors": sector_rows, "tax": tax_section,
            "errors": errors, "notes": notes, "disclaimer": DISCLAIMER, "execution_eligible": False, "submitted": False}


# ---------------------------------------------------------------- markdown

def inr(value, digits=0):
    """Rupees with Indian digit grouping: 1,25,000."""
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.{digits}f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    text = ",".join(groups + [tail])
    return f"{sign}₹{text}" + (f".{frac}" if frac else "")


def pct(value, digits=1, sign=True):
    return "n/a" if value is None else f"{value * 100:{'+' if sign else ''}.{digits}f}%"


def num(value, digits=2):
    return "n/a" if value is None else f"{value:,.{digits}f}"


def qty(value):
    if value is None:
        return "n/a"
    value = float(value)
    return f"{int(value):,}" if value == int(value) else f"{value:,.4f}".rstrip("0")


def markdown(r):
    t, c = r["totals"], r["concentration"]
    priced = r["price_source"] is not None
    head = f"As of {r['as_of'][:16].replace('T', ' ')} IST · source: {r['source']}"
    if r["holdings_as_of"]:
        head += f" (holdings as of {r['holdings_as_of']})"
    if t["price_dates"]:
        head += f" · prices: Yahoo daily close dated {', '.join(t['price_dates'])} (during market hours the latest bar is still forming)"
    lines = ["# Portfolio review", "", head, "", f"> {r['disclaimer']}", "", "## Summary", "", "| Measure | Value |", "| --- | ---: |",
             f"| Holdings | {t['holdings']}" + (f" ({t['priced_holdings']} priced)" if priced else "") + " |",
             f"| Invested (at average cost) | {inr(t['invested'])} |"]
    if priced:
        partial = " (priced holdings only)" if t["priced_holdings"] < t["holdings"] else ""
        lines += [f"| Current value{partial} | {inr(t['current_value'])} |",
                  f"| Unrealised P&L{partial} | {inr(t['unrealised_pnl'])} ({pct(t['unrealised_pnl_pct'])}) |",
                  f"| Day change (last daily bar vs previous close) | {inr(t['day_change'])} ({pct(t['day_change_pct'], 2)}) |"]
    lines += [f"| Largest holding | {c['top_holding']} {pct(c['top_holding_weight'], sign=False)} |",
              f"| Top 5 holdings | {pct(c['top5_weight'], sign=False)} |",
              f"| Herfindahl index | {num(c['herfindahl'], 3)} — {c['reading']}"
              + (f", like {c['effective_holdings']:.1f} equal-sized holdings" if c["effective_holdings"] else "") + " |", ""]
    lines += ["## Holdings", "", "| Symbol | Qty | Avg cost | Last price (date) | Invested | Current value | P&L | P&L % | Weight | Sector |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for h in r["holdings"]:
        last = f"{inr(h['last_price'], 2)} ({h['price_date']}{' ⚠ stale' if h['price_stale'] else ''})" if h["last_price"] is not None else h["price_status"]
        lines.append(f"| {h['symbol']} | {qty(h['quantity'])} | {inr(h['average_price'], 2)} | {last} | {inr(h['invested'])} | {inr(h['current_value'])} | "
                     f"{inr(h['pnl'])} | {pct(h['pnl_pct'])} | {pct(h['weight'], sign=False)}{' *' if h['valued_at_cost'] and priced else ''} | {h['sector'] or 'n/a'} |")
    if any(h["valued_at_cost"] for h in r["holdings"]) and priced:
        lines.append("")
        lines.append("\\* price unavailable; weighted at average cost, P&L not shown.")
    lines += ["", "## Concentration", "",
              f"Weights use {c['basis']}. Herfindahl index (sum of squared weights) is {num(c['herfindahl'], 3)}: {c['reading']}. "
              "Readings: below 0.10 diversified, 0.10–0.18 moderately concentrated, above 0.18 highly concentrated.", ""]
    if r["sectors"] is not None:
        lines += ["## Sector exposure", "", f"Industry from the NSE Nifty 500 list ({r['sector_source']}).", "",
                  "| Sector | Holdings | Value | Weight |", "| --- | --- | ---: | ---: |"]
        lines += [f"| {s['sector']} | {', '.join(s['holdings'])} | {inr(s['value'])} | {pct(s['weight'], sign=False)} |" for s in r["sectors"]]
        lines.append("")
    x = r["tax"]
    if x:
        rt, rl = x["rates"], x["realised"]
        lines += [f"## Capital-gains tax estimate — FY {x['financial_year']} ({x['fy_start']} to {x['fy_end']})", "",
                  f"Method: {x['method']}. {rt['basis']}", "",
                  f"Rates used: STCG {pct(rt['stcg_rate'], sign=False)}, LTCG {pct(rt['ltcg_rate'], sign=False)} above {inr(rt['ltcg_exemption'])}, cess {pct(rt['cess_rate'], sign=False)}.", "",
                  "### Realised this financial year", ""]
        if x["realised_matches"]:
            lines += ["| Symbol | Bought | Sold | Qty | Buy | Sell | Days held | Term | Gain |", "| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: |"]
            for m in x["realised_matches"][:60]:
                lines.append(f"| {m['symbol']} | {m['buy_date']} | {m['sell_date']} | {qty(m['quantity'])} | {inr(m['buy_price'], 2)} | {inr(m['sell_price'], 2)} | "
                             f"{m['days_held']} | {m['term']} | {inr(m['gain'])} |")
            if len(x["realised_matches"]) > 60:
                lines.append(f"| … {len(x['realised_matches']) - 60} more in JSON output | | | | | | | | |")
        else:
            lines.append("No sales matched in this financial year.")
        lines += ["", "| Line | Short-term | Long-term |", "| --- | ---: | ---: |",
                  f"| Gains | {inr(rl['short_term_gains'])} | {inr(rl['long_term_gains'])} |",
                  f"| Losses | {inr(-rl['short_term_losses'])} | {inr(-rl['long_term_losses'])} |",
                  f"| Short-term loss set off against long-term | | {inr(-rl['short_term_loss_set_off_against_long_term'])} |",
                  f"| After set-off | {inr(rl['short_term_after_set_off'])} | {inr(rl['long_term_after_set_off'])} |",
                  f"| LTCG exemption used | | {inr(-rl['ltcg_exemption_used'])} |",
                  f"| Taxable | {inr(rl['taxable_short_term'])} | {inr(rl['taxable_long_term'])} |",
                  f"| Tax at rate | {inr(rl['stcg_tax'])} | {inr(rl['ltcg_tax'])} |",
                  f"| Cess ({pct(rt['cess_rate'], 0, sign=False)} on tax) | {inr(rl['cess'])} | |",
                  f"| **Estimated tax on realised gains** | **{inr(rl['total_tax'])}** | |", "",
                  f"LTCG exemption remaining this FY (information): {inr(x['ltcg_exemption_remaining'])}. The exemption is shared with other s.112A gains outside this file, such as equity mutual funds.", ""]
        if rl["unused_short_term_loss"] or rl["unused_long_term_loss"]:
            lines += [f"Losses not absorbed in this report: short-term {inr(rl['unused_short_term_loss'])}, long-term {inr(rl['unused_long_term_loss'])} (carry-forward not modelled).", ""]
        u = x["unrealised"]
        lines += [f"### Unrealised on open lots (term as of {r['as_of'][:10]})", "", "| Term | Lots | Cost | Unrealised gain |", "| --- | ---: | ---: | ---: |"]
        for term in ("short", "long"):
            missing = f" ({u[term]['lots_without_price']} lot(s) without price excluded)" if u[term]["lots_without_price"] else ""
            lines.append(f"| {term}-term | {u[term]['lots']} | {inr(u[term]['cost'])} | {inr(u[term]['gain'])}{missing} |")
        if x["open_lots"]:
            lines += ["", "| Symbol | Bought | Qty | Buy price | Term today | Long-term from | Unrealised gain |", "| --- | --- | ---: | ---: | --- | --- | ---: |"]
            for l in x["open_lots"][:60]:
                lines.append(f"| {l['symbol']} | {l['buy_date']} | {qty(l['quantity'])} | {inr(l['buy_price'], 2)} | {l['term_today']} | {l['long_term_from']} | {inr(l['unrealised_gain'])} |")
            if len(x["open_lots"]) > 60:
                lines.append(f"| … {len(x['open_lots']) - 60} more in JSON output | | | | | | |")
        h = x["hypothetical_sell_all"]
        if h:
            lines += ["", f"Hypothetical, for information only: {h['description'][0].lower() + h['description'][1:]}, estimated FY tax would be {inr(h['estimated_total_tax'])} "
                      f"({inr(h['estimated_additional_tax'])} more than on realised gains alone). This is arithmetic, not a suggestion to sell."]
        lines += ["", "### Not modelled", ""] + [f"- {item}" for item in x["not_modelled"]]
        lines += ["", x["note"], ""]
    gaps = [f"- {k}: {v}" for k, v in r["errors"].items()] + [f"- {n}" for n in r["notes"]]
    if gaps:
        lines += ["## Data gaps and notes", ""] + gaps + [""]
    lines += ["Research only. execution_eligible: false · submitted: false. Nothing here places an order."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="Review personal Indian equity holdings: P&L, concentration, sectors and an FIFO capital-gains estimate (research only).")
    p.add_argument("--input", required=True, help="holdings_v1 JSON, Zerodha Console holdings CSV, or Zerodha tradebook CSV")
    p.add_argument("--tradebook", help="Zerodha tradebook CSV for tax lots (equity delivery rows, segment EQ)")
    p.add_argument("--fy", help="Indian financial year such as 2026-27 (default: the FY containing today in IST)")
    p.add_argument("--stcg-rate", type=float, default=DEFAULT_RATES["stcg_rate"], help="default 0.20 (s.111A) — verify current rates")
    p.add_argument("--ltcg-rate", type=float, default=DEFAULT_RATES["ltcg_rate"], help="default 0.125 (s.112A) — verify current rates")
    p.add_argument("--ltcg-exemption", type=float, default=DEFAULT_RATES["ltcg_exemption"], help="default 125000 rupees per FY")
    p.add_argument("--cess-rate", type=float, default=DEFAULT_RATES["cess_rate"], help="default 0.04 health and education cess")
    p.add_argument("--no-prices", action="store_true", help="offline: no network; values at average cost")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--out", help="Write the report to a new file (refuses to overwrite)")
    a = p.parse_args(argv)
    try:
        if a.out and Path(a.out).exists():
            raise Block("output exists; choose a new path")
        source = load(a.input)
        tradebook = load(a.tradebook) if a.tradebook else None
        if tradebook is not None and source["kind"] == "zerodha_tradebook_csv":
            raise Block("INPUT: the input is already a tradebook; pass it once")
        if tradebook is not None and tradebook["kind"] != "zerodha_tradebook_csv":
            raise Block("INPUT: --tradebook must be a Zerodha tradebook CSV")
        rates = {"stcg_rate": a.stcg_rate, "ltcg_rate": a.ltcg_rate, "ltcg_exemption": a.ltcg_exemption, "cess_rate": a.cess_rate}
        report = review(source, tradebook, fy=a.fy, rates=rates, prices=not a.no_prices)
    except Block as error:
        print(f"BLOCKED - {error}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, ensure_ascii=False) if a.format == "json" else markdown(report)
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            with out.open("x", encoding="utf-8") as stream:
                stream.write(text)
        except FileExistsError:
            print("BLOCKED - output exists; choose a new path", file=sys.stderr)
            return 2
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
