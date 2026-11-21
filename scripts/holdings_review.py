"""Personal holdings review for Indian equity investors.

Reads a holdings file (normalized holdings_v1 JSON, a Zerodha Console holdings
CSV or a Zerodha tradebook CSV) and an optional tradebook for tax lots, then
reports P&L, weights, concentration, sector exposure and, when lots are known,
an estimate of capital-gains tax for one Indian tax year (FIFO lots), the sale
costs of selling everything, annualised returns against the Nifty 50 over the
same dates, and the value of demerged shares the file does not list.

Research only: no orders, no recommendation to buy, sell or harvest. Prices are
public Yahoo quotes: the live price with its time while the NSE session is open,
otherwise the latest daily close with its date; a price that fails stays visible
as unavailable and is never guessed. Tax figures are estimates with labelled
default rates; check current rules with a tax professional.
"""
import argparse
import calendar
import csv
from datetime import date, datetime, timedelta
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
# The Income-tax Act 2025 replaced the 1961 Act from 1 April 2026 (checked 2026-11-18 against incometaxindia.gov.in section
# pages 196 and 198 and the CBDT commencement notice as reported by taxguru.in/cleartax.in). Rates carried over unchanged
# by the Finance Act 2026. Transfers before 1 April 2026 stay under the 1961 Act (s.111A/s.112A).
NEW_ACT_DATE = date(2026, 4, 1)
RATE_BASIS_2025 = ("Income-tax Act 2025 (applies to transfers from 1 April 2026; the 'tax year' replaces the financial/assessment "
                   "year): short-term gains on listed equity sold with STT taxed at 20% (s.196, formerly s.111A of the 1961 Act) and "
                   "long-term gains at 12.5% (s.198, formerly s.112A) on the part above ₹1,25,000 a tax year; health and education "
                   "cess 4% on the tax. Rates unchanged by the Finance Act 2026 — verify current rates.")
BENCHMARK = "^NSEI"
NOT_MODELLED = [
    "Surcharge on capital-gains tax",
    "Grandfathering of shares bought before 1 Feb 2018 (cost = higher of cost and 31 Jan 2018 FMV)",
    "Mergers and rights issues (cost apportionment); bonuses and splits are applied from Yahoo's split events, as a bonus when "
    "the event matches the curated list (assets/bonuses.json) or --bonus is given, otherwise as a split",
    "Demergers are flagged from a curated list (assets/demergers.json) with the published cost split spelled out; "
    "cost changes only with --apply-demergers, and demergers missing from the list are not detected",
    "Set-off of capital losses against other income, and carry-forward of losses to later years",
    "Intraday and F&O trades (business income)",
    "Brokerage, exchange and DP charges as transfer expenses deducted from the gain (STT is not deductible); sale costs are shown separately",
    "Basic exemption limit, section 87A rebate and non-resident (TDS) treatment",
    "Older rates for transfers before 23 July 2024 (15% STCG, 10% LTCG above ₹1,00,000)",
]
DISCLAIMER = ("For information only; not investment or tax advice and not a recommendation to buy, sell or "
              "harvest gains or losses. Tax figures are estimates; check with a tax professional.")
DEMERGERS_PATH = Path(__file__).resolve().parents[1] / "assets" / "demergers.json"
BONUSES_PATH = Path(__file__).resolve().parents[1] / "assets" / "bonuses.json"
BONUS_MATCH_DAYS = 4          # a Yahoo split event this close to a listed bonus's ex-date or record date can be that bonus
PRICE_TOLERANCE = D("0.03")   # a buy/sell price this far outside the period's traded low-high range is flagged
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


def purchase_date(value, name):
    """Full date, or a month (2024-03) when the user only knows that; month-only dates are flagged as approximate."""
    text = str(value or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if m and 1 <= int(m.group(2)) <= 12:
        return date(int(m.group(1)), int(m.group(2)), 1), True
    return trade_date(text, name), False


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
            row = holding(h["symbol"], h["quantity"], h["average_price"], notes)
            if row is not None and h.get("bought"):
                row["bought"], row["bought_approx"] = purchase_date(h["bought"], f"bought date for {row['symbol']}")
            rows.append(row)
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


def load_demergers(path=DEMERGERS_PATH):
    """Load and validate the curated demerger list. Returns entries with dates and Decimals; blocks on any malformed entry."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"),
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
    except (OSError, ValueError) as error:
        raise Block(f"DEMERGERS: cannot read {Path(path).name} ({error})") from None
    if not isinstance(doc, dict) or doc.get("schema") != "demergers_v1" or not isinstance(doc.get("demergers"), list):
        raise Block('DEMERGERS: expected {"schema": "demergers_v1", "demergers": [...]}')
    out = []
    for n, e in enumerate(doc["demergers"]):
        try:
            parent = str(e["parent"]).strip().upper()
            aliases = [str(a).strip().upper() for a in (e.get("parent_aliases") or [])]
            new, per = e["ratio"]["new_shares"], e["ratio"]["per_parent_shares"]
            if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (new, per)):
                raise ValueError("ratio must be positive whole numbers")
            split = e.get("cost_split_pct")
            parent_pct = resulting_pct = None
            if split is not None:
                parent_pct, resulting_pct = D(str(split["parent"])), D(str(split["resulting"]))
                if not (0 < parent_pct < 100 and 0 < resulting_pct < 100) or abs(parent_pct + resulting_pct - 100) > D("0.011"):
                    raise ValueError("cost split must be two percentages adding to 100")
            sources = e["sources"]
            if not isinstance(sources, list) or not sources or not all(isinstance(s, dict) and str(s.get("url", "")).startswith("https://") for s in sources):
                raise ValueError("at least one https source URL is required")
            out.append({"parent": parent, "symbols": [parent] + aliases, "parent_name": str(e.get("parent_name") or parent),
                        "resulting_company": str(e["resulting_company"]), "resulting_symbol": (str(e["resulting_symbol"]).upper() if e.get("resulting_symbol") else None),
                        "record_date": date.fromisoformat(e["record_date"]), "new": new, "per": per,
                        "parent_pct": parent_pct, "resulting_pct": resulting_pct, "sources": [s["url"] for s in sources],
                        "checked": date.fromisoformat(e["checked"])})
        except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError) as error:
            raise Block(f"DEMERGERS: entry {n + 1} is invalid ({error})") from None
    return out


def whole_ratio(pair, a, b):
    x, y = pair[a], pair[b]
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (x, y)):
        raise ValueError("ratios must be positive whole numbers")
    return x, y


def load_bonuses(path=BONUSES_PATH):
    """Load and validate the curated bonus list. Each entry gets the share multipliers Yahoo would record:
    bonus_factor = (new + held) / held, split_factor for a same-day split before the bonus, and their product."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"),
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
    except (OSError, ValueError) as error:
        raise Block(f"BONUSES: cannot read {Path(path).name} ({error})") from None
    if not isinstance(doc, dict) or doc.get("schema") != "bonuses_v1" or not isinstance(doc.get("bonuses"), list):
        raise Block('BONUSES: expected {"schema": "bonuses_v1", "bonuses": [...]}')
    out = []
    for n, e in enumerate(doc["bonuses"]):
        try:
            new, held = whole_ratio(e["ratio"], "new_shares", "per_held_shares")
            split = D(1)
            if e.get("split_before_bonus") is not None:
                to, old = whole_ratio(e["split_before_bonus"], "new_shares", "per_old_shares")
                split = D(to) / D(old)
                if split == 1:
                    raise ValueError("split_before_bonus must change the share count")
            sources = e["sources"]
            if not isinstance(sources, list) or not sources or not all(isinstance(s, dict) and str(s.get("url", "")).startswith("https://") for s in sources):
                raise ValueError("at least one https source URL is required")
            record = date.fromisoformat(e["record_date"])
            ex = date.fromisoformat(e["ex_date"]) if e.get("ex_date") else None
            allotted = date.fromisoformat(e["allotment_date"]) if e.get("allotment_date") else None
            if (ex and not (record - ex).days in range(0, 8)) or (allotted and allotted < record):
                raise ValueError("ex-date must be on or up to a week before the record date, allotment on or after it")
            bonus = D(new + held) / D(held)
            out.append({"symbol": str(e["symbol"]).strip().upper(), "company": str(e.get("company") or e["symbol"]),
                        "new": new, "held": held, "bonus_factor": bonus, "split_factor": split, "combined": bonus * split,
                        "record_date": record, "ex_date": ex, "allotment_date": allotted,
                        "sources": [s["url"] for s in sources], "checked": date.fromisoformat(e["checked"])})
        except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError) as error:
            raise Block(f"BONUSES: entry {n + 1} is invalid ({error})") from None
    return out


def classify_actions(symbol, actions, listed, force_bonus=False, force_split=False):
    """Turn one symbol's Yahoo split events into FIFO events and readable descriptions.

    A Yahoo event matches a listed bonus when the dates are within BONUS_MATCH_DAYS of its ex-date or record date and
    the share multiplier equals the bonus (or, for a same-day split plus bonus, the combined multiplier or the split
    alone, which is what Yahoo sometimes records). --split forces split treatment; --bonus forces bonus treatment."""
    events, texts = [], []
    for a in actions:
        base = {"date": a["date"], "label": a["label"], "num": a["num"], "den": a["den"]}
        match, part = None, None
        for e in listed:
            near = min(abs((a["date"] - d).days) for d in (e["record_date"], e["ex_date"]) if d)
            if e["symbol"] != symbol or near > BONUS_MATCH_DAYS:
                continue
            if a["ratio"] == e["combined"]:
                match, part = e, "all"
            elif e["split_factor"] != 1 and a["ratio"] == e["split_factor"]:
                match, part = e, "split only"
            if match:
                break
        indian = f"{a['num'] - a['den']:g}:{a['den']:g}"
        yahoo = f"Yahoo records a {a['label']} split on {a['date'].isoformat()}"
        split_text = f"{a['label']} split on {a['date'].isoformat()}"
        if force_split:
            events.append({**base, "ratio": a["ratio"], "kind": "split", "text": split_text})
            texts.append(f"{split_text} (treated as a split because of --split)")
            continue
        if match is None:
            if force_bonus:
                events.append({**base, "ratio": a["ratio"], "kind": "bonus", "lot_date": a["date"],
                               "text": f"{indian} bonus on {a['date'].isoformat()}"})
                texts.append(f"{indian} bonus on {a['date'].isoformat()} (--bonus; bonus shares dated from that ex-date, as the allotment "
                             "date is not known — the long-term date may be a few days early)")
            else:
                events.append({**base, "ratio": a["ratio"], "kind": "split", "text": split_text})
                texts.append(f"{a['label']} share change on {a['date'].isoformat()} (a {indian} bonus or a {a['label']} split; not in this "
                             f"skill's list of bonus issues, so treated as a split — use --bonus {symbol} if it was a bonus)")
            continue
        lot_date = match["allotment_date"] or match["record_date"]
        dated = (f"bonus shares dated from the allotment date {lot_date.isoformat()}" if match["allotment_date"] else
                 f"bonus shares dated from the record date {lot_date.isoformat()} (allotment date not in the list; the long-term date "
                 "may be a few days early)")
        terms = f"{match['new']}:{match['held']} bonus (record date {match['record_date'].isoformat()})"
        bonus_text = f"{match['new']}:{match['held']} bonus on {a['date'].isoformat()}"
        if match["split_factor"] != 1:
            s = match["split_factor"]
            events.append({**base, "ratio": s, "kind": "split", "text": f"{s:g}-for-1 split on {a['date'].isoformat()}"})
            events.append({**base, "ratio": match["bonus_factor"], "kind": "bonus", "lot_date": lot_date, "text": bonus_text})
            seen = ("Yahoo records only the split; the bonus is added from the list" if part == "split only"
                    else f"{yahoo} for both together")
            texts.append(f"{s:g}-for-1 split and then a {terms} on {a['date'].isoformat()} — {seen} (assets/bonuses.json); {dated}; "
                         f"use --split {symbol} to treat Yahoo's event as a plain split")
        else:
            events.append({**base, "ratio": a["ratio"], "kind": "bonus", "lot_date": lot_date, "text": bonus_text})
            texts.append(f"{terms} — {yahoo}, matched to the listed bonus issue (assets/bonuses.json), so treated as a bonus; {dated}; "
                         f"use --split {symbol} to treat it as a split")
    return events, texts


# ---------------------------------------------------------------- buy/sell price plausibility

def month_end(day):
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def rupees_range(low, high):
    digits = 2 if high < 100 else 0
    lo = D(low).quantize(D(1) / 10 ** digits, rounding="ROUND_FLOOR")
    hi = D(high).quantize(D(1) / 10 ** digits, rounding="ROUND_CEILING")
    return f"{inr(lo, digits)}–{inr(hi, digits)[1:]}"


def price_check(symbol, side, price, start, end, bars, events, demerger_dates, approx=False):
    """Compare a typed buy/sell price with what traded over the period (a day, or a month when the date is month-only).

    Yahoo's daily bars are in today's share units, so each bar is scaled back by every split/bonus after it to give the
    prices of the time. Yahoo also scales prices before some demergers down (no event is published for that), so when a
    listed demerger falls after the period start only a price below the range is flagged. The user's numbers are never
    changed. Returns a dict with status ok / outside / not_checked."""
    out = {"side": side, "price": f(price), "period_start": start.isoformat(), "period_end": end.isoformat(), "status": "not_checked"}
    if not bars or price <= 0:
        out["reason"] = "no price history"
        return out
    window = [b for b in bars if start <= b["date"] <= end]
    around = False
    if not window and start == end:
        window = [b for b in bars if abs((b["date"] - start).days) <= 3]
        around = bool(window)
    if not window:
        out["reason"] = ("before the 5-year price history" if start < bars[0]["date"] else "no trading in that period in the price history")
        return out
    scale = lambda day: _product(e["ratio"] for e in events if e["date"] > day)
    low = min(D(str(b["low"])) * scale(b["date"]) for b in window)
    high = max(D(str(b["high"])) * scale(b["date"]) for b in window)
    raw_low, raw_high = min(D(str(b["low"])) for b in window), max(D(str(b["high"])) for b in window)
    later = sorted({e["date"] for e in events if e["date"] > start})
    demerged = [d for d in demerger_dates if d > start]
    below, above = price < low * (1 - PRICE_TOLERANCE), price > high * (1 + PRICE_TOLERANCE)
    if demerged:
        above = False
    period = ("in " + start.strftime("%b %Y") if approx else
              f"in the days around {start.isoformat()} (no trading that day)" if around else f"on {start.isoformat()}")
    out.update({"status": "outside" if below or above else "ok", "low": f(low), "high": f(high), "period": period,
                "units": "prices of the time (Yahoo's split-adjusted bars scaled back)" if later else "as traded",
                "upper_bound_checked": not demerged})
    if not (below or above):
        return out
    shown = inr(price, 2 if price != price.to_integral_value() else 0)
    verb = "bought" if side == "BUY" else "sold"
    text = f"{symbol}: {shown} ({verb}) is outside the {rupees_range(low, high)} range traded {period} — check the price or date"
    if later and raw_low * (1 - PRICE_TOLERANCE) <= price <= raw_high * (1 + PRICE_TOLERANCE):
        text += (f". It fits the {rupees_range(raw_low, raw_high)} range in today's share units (after the share change on "
                 f"{', '.join(d.isoformat() for d in later)}): if your quantity and price are already adjusted, the review has "
                 "adjusted them a second time")
    if demerged:
        text += (f". Yahoo scales prices from before the demerger on {demerged[0].isoformat()} down, so the real low was at or above "
                 "this and the range shown understates it")
    out["message"] = text + "."
    return out


def _product(values):
    total = D(1)
    for v in values:
        total *= v
    return total


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


def fifo(lots, events=None):
    """Match sells to the oldest open buys per symbol. Returns matches, open lots, unmatched sells, notes.

    `events` maps symbol -> [{"date", "ratio", "kind"}]. On the ex-date, before that day's trades, a split
    multiplies open quantities and divides their cost (holding period unchanged); a bonus adds a new lot of
    (ratio - 1) x open quantity at nil cost dated on the event's "lot_date" (the allotment date, from which Indian tax
    counts the holding period of bonus shares) or else the ex-date. A demerger
    event ({"kind": "demerger", "factor"}) keeps quantities and multiplies open lots' cost by the parent's published
    share of the cost of acquisition; trades on the record date itself are not entitled, so it applies before them.
    """
    symbols = {x["symbol"] for x in lots}
    marks = [{"symbol": s, "date": e["date"], "time": "", "side": "EVENT", "seq": -1, **e}
             for s, evs in (events or {}).items() if s in symbols for e in evs]
    rank = {"EVENT": 0, "BUY": 1, "SELL": 2}
    order = sorted(list(lots) + marks, key=lambda x: (x["symbol"], x["date"], x["time"], rank[x["side"]], x["seq"]))
    queues, matches, unmatched, notes = {}, [], [], []
    days = {}
    for x in order:
        queue = queues.setdefault(x["symbol"], [])
        if x["side"] == "EVENT":
            held = sum((l["quantity"] for l in queue), D(0))
            if held:
                if x["kind"] == "bonus":
                    queue.append({"symbol": x["symbol"], "date": x.get("lot_date") or x["date"], "quantity": held * (x["ratio"] - 1),
                                  "price": D(0), "bonus": True})
                elif x["kind"] == "demerger":
                    for l in queue:
                        l["price"] *= x["factor"]
                else:
                    for l in queue:
                        l["quantity"] *= x["ratio"]
                        l["price"] /= x["ratio"]
            continue
        days.setdefault((x["symbol"], x["date"]), set()).add(x["side"])
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


def chronology(sym_lots, evs):
    """One readable step per corporate event in date order: shares and total cost of the lots held just before and
    just after it (splits and bonuses change the share count, not the cost; a demerger cost split changes the cost)."""
    ordered = sorted(evs, key=lambda e: e["date"])
    totals = lambda open_lots: (sum((l["quantity"] for l in open_lots), D(0)), sum((l["quantity"] * l["price"] for l in open_lots), D(0)))
    steps = []
    for i, e in enumerate(ordered):
        held = [l for l in sym_lots if l["date"] < e["date"]]
        if not held:
            continue
        sym = held[0]["symbol"]
        qb, cb = totals(fifo(held, {sym: ordered[:i]})[1])
        qa, ca = totals(fifo(held, {sym: ordered[:i + 1]})[1])
        if not qb:
            continue
        text = e.get("text") or f"{e['kind']} on {e['date'].isoformat()}"
        if e["kind"] == "demerger":
            steps.append(f"{text}: cost {inr(cb)} became {inr(ca)} ({qty(qa)} shares, {inr(ca / qa, 2)} a share)")
        else:
            nil = ", the new shares at nil cost" if e["kind"] == "bonus" else ""
            steps.append(f"{text}: {qty(qb)} shares became {qty(qa)} ({inr(ca / qa, 2)} a share{nil}; total cost unchanged at {inr(ca)})")
    return steps


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


def latest_price(yahoo, *, fetcher, clock, years=5):
    """Latest price with its basis. While the NSE session is open and Yahoo's newest daily bar is today's, the price is the
    live (still-forming) one with Yahoo's quote time; otherwise it is the latest daily close. Five years of daily bars by
    default so splits and bonuses behind an old average cost are still detected."""
    from stock_analysis import daily_history, session_open
    raws = []

    def capture(url):
        raw = fetcher(url)
        raws.append(raw)
        return raw

    now = clock()
    history = daily_history(yahoo, fetcher=capture, now=now, years=years)
    bars = history["bars"]
    last = bars[-1]
    previous = bars[-2]["close"] if len(bars) > 1 else None
    actions = []
    for k, s in (history.get("corporate_actions", {}).get("splits") or {}).items():
        num, den = s.get("numerator"), s.get("denominator")
        if isinstance(num, (int, float)) and isinstance(den, (int, float)) and num > 0 and den > 0 and num != den:
            actions.append({"date": datetime.fromtimestamp(int(s.get("date", k)), IST).date(), "ratio": D(str(num)) / D(str(den)),
                            "num": num, "den": den, "label": f"{num:g}-for-{den:g}"})
    actions.sort(key=lambda a: a["date"])
    ranges = [{"date": date.fromisoformat(b["at"][:10]), "open": b.get("open"), "low": b["low"], "high": b["high"], "close": b["close"]}
              for b in bars]
    live = session_open(now) and last["at"][:10] == now.date().isoformat()
    price_time = None
    if live:
        try:
            stamp = json.loads(raws[-1])["chart"]["result"][0]["meta"].get("regularMarketTime")
            at = datetime.fromtimestamp(int(stamp), IST) if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else None
            price_time = at.strftime("%H:%M") if at and at.date() == now.date() and at <= now else None
        except (IndexError, KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError):
            price_time = None
    return {"price": D(str(last["close"])), "previous_close": None if previous is None else D(str(previous)),
            "previous_close_date": bars[-2]["at"][:10] if len(bars) > 1 else None,
            "price_date": last["at"][:10], "price_kind": "live" if live else "close", "price_time": price_time,
            "splits": [a["date"].isoformat() for a in actions], "actions": actions,
            "price_note": history.get("price_note"), "bars": ranges}


def close_on_or_after(bars, day):
    """(close, date) of the first daily bar on or after `day` (a purchase on a holiday fills on the next session); None if outside."""
    if not bars or day < bars[0]["date"] - timedelta(days=7):
        return None
    for b in bars:
        if b["date"] >= day:
            return D(str(b["close"])), b["date"]
    return None


def xirr(flows):
    """Annual rate r with sum(amount / (1+r)^(years since first flow)) = 0 (money in negative). Bisection; None when undefined."""
    flows = [(d, float(a)) for d, a in flows if a]
    if not flows or not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None
    t0 = min(d for d, _ in flows)

    def npv(r):
        return sum(a / (1 + r) ** ((d - t0).days / 365.0) for d, a in flows)

    lo, hi = -0.9999, 1.0
    try:
        while npv(hi) > 0 and hi < 1e6:
            hi *= 2
        if npv(lo) * npv(hi) > 0:
            return None
        for _ in range(200):
            mid = (lo + hi) / 2
            if npv(lo) * npv(mid) <= 0:
                hi = mid
            else:
                lo = mid
    except (OverflowError, ZeroDivisionError):
        return None
    return (lo + hi) / 2


def sale_costs(items):
    """Charges for selling (symbol, quantity, price) items as delivery (CNC) today: STT 0.1% of the sale value and a DP charge
    per scrip (plus GST) from position_size.DELIVERY, with exchange, SEBI and GST from the example rate card. Returns Decimals."""
    from position_size import DEFAULT_CARD, charges, load_card
    card, seg = load_card(DEFAULT_CARD)
    out = {"stt": D(0), "dp": D(0), "other": D(0), "total": D(0), "sale_value": D(0)}
    for _, q, price in items:
        if not q or price is None:
            continue
        c = charges(0.0, float(price), float(q), "BUY", "CNC", card, seg)  # entry 0: only the sell leg is charged
        out["stt"] += D(str(c["stt"]))
        out["dp"] += D(str(c["dp"]))
        out["total"] += D(str(c["total"]))
        out["sale_value"] += D(str(q)) * D(str(price))
    out["other"] = out["total"] - out["stt"] - out["dp"]
    out["source"] = card.get("source")
    return out


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


def holdings_for_prices(source, lots):
    if source["holdings"] is not None:
        return source["holdings"]
    return [{"symbol": s, "yahoo": stock(s)[1]} for s in sorted({l["symbol"] for l in lots})]


def hhi_reading(value):
    if value is None:
        return "n/a"
    if value < 0.10:
        return "diversified"
    if value < 0.18:
        return "moderately concentrated"
    return "highly concentrated"


# ---------------------------------------------------------------- demergers

def plural(n, word):
    return f"{qty(n)} {word} share" + ("" if n == 1 else "s")


def demerger_checks(symbol, entries, sym_lots, prior_events, quantity, average, holding, held, applied):
    """Warnings for one holding whose symbol is a demerger parent. Returns (texts, details).

    Dated (lots or a bought date): only shares held before the record date count; their quantity and cost on that date come
    from FIFO over the earlier lots, with earlier splits and earlier published demerger splits applied. Undated: the whole
    holding, worded conditionally. Nothing is changed here; `applied` says whether --apply-demergers already adjusted the cost.
    """
    texts, details = [], []
    for e in sorted(entries, key=lambda x: x["record_date"]):
        rd = e["record_date"]
        res = e["resulting_symbol"] or e["resulting_company"]
        if sym_lots:
            pre = [l for l in sym_lots if l["date"] < rd]
            if not pre:
                continue
            open_pre = fifo(pre, {symbol: [x for x in prior_events if x["date"] < rd]})[1]
            held_rd = sum((l["quantity"] for l in open_pre), D(0))
            cost_rd = sum((l["quantity"] * l["price"] for l in open_pre), D(0))
            if held_rd == 0:
                continue
            conditional = False
        else:
            held_rd, cost_rd, conditional = quantity, quantity * average, True
        ratio = D(e["new"]) / D(e["per"])
        new_q = held_rd * ratio
        whole = int(new_q)
        terms = f"{plural(e['new'], res)} for every {plural(e['per'], symbol)}"
        if conditional:
            text = (f"Demerger: if you bought {symbol} before {rd.isoformat()} (record date for the demerger of {e['resulting_company']}: "
                    f"{terms}), you should also hold {plural(whole, res)} for these {qty(held_rd)}")
        else:
            text = (f"Demerger: {e['resulting_company']} was demerged from {symbol} with record date {rd.isoformat()} ({terms}). "
                    f"You held {plural(held_rd, symbol)} then, so you should also have received {plural(whole, res)}")
            if holding and holding.get("bought_approx") and (holding["bought"].year, holding["bought"].month) == (rd.year, rd.month):
                text += f" (your purchase date is month-only and taken as the 1st; if you bought on or after {rd.isoformat()} this does not apply)"
        if new_q != whole:
            text += ", plus cash for the fractional entitlement"
        text += "."
        text += (f" {res} is also in this file ({plural(held[res], res)})." if res in held else
                 f" {res} is not in this file; check your demat account for these shares and add them if you still hold them "
                 "(the value and P&L here exclude them).")
        per_share = cost_rd / held_rd if held_rd else D(0)
        info = {"resulting_company": e["resulting_company"], "resulting_symbol": e["resulting_symbol"], "record_date": rd.isoformat(),
                "conditional": conditional, "held_at_record_date": f(held_rd, 4), "entitled_new_shares": f(new_q, 4),
                "resulting_in_holdings": res in held, "resulting_quantity_in_holdings": f(held[res], 4) if res in held else None,
                "cost_split_known": e["parent_pct"] is not None, "applied": bool(applied and e["parent_pct"] is not None and not conditional),
                "sources": e["sources"]}
        if e["parent_pct"] is not None:
            pp, rp = e["parent_pct"] / 100, e["resulting_pct"] / 100
            info.update({"cost_at_record_date": f(cost_rd), "parent_cost_after": f(cost_rd * pp), "parent_cost_per_share_after": f(per_share * pp),
                         "resulting_cost": f(cost_rd * rp), "resulting_cost_per_share": f(per_share * rp / ratio)})
            text += (f" The published cost split is {e['parent_pct']}% {symbol} / {e['resulting_pct']}% {res}: of the {inr(cost_rd)} paid for these shares, "
                     f"{inr(cost_rd * pp)} stays with {symbol} ({inr(per_share * pp, 2)} a share instead of {inr(per_share, 2)}) and "
                     f"{inr(cost_rd * rp)} becomes the cost of the {res} shares ({inr(per_share * rp / ratio, 2)} each).")
            later = sorted({x["date"].isoformat() for x in prior_events if x.get("kind") in ("split", "bonus") and x["date"] >= rd})
            if later and not conditional:
                text += (f" The {symbol} per-share figures are in the units held on {rd.isoformat()}; the later share change on "
                         f"{', '.join(later)} changes them, not the rupee totals.")
            if info["applied"]:
                text += " The cost and P&L above already use this split (--apply-demergers)."
            elif conditional:
                text += " The cost above is not adjusted; if your broker has not already adjusted it, it is overstated and the P&L understated."
            else:
                text += (" The cost and P&L above are not adjusted for it: run with --apply-demergers, or adjust it yourself, "
                         "unless your broker has already done so.")
        else:
            text += (f" The cost split between {symbol} and {res} is not in this skill's reference list; {e['parent_name']} publishes it as an "
                     "'apportionment of cost of acquisition' notice. Until it is applied the cost here may be overstated and the P&L understated.")
        text += " The new shares' holding period counts from your original purchase date."
        texts.append(text)
        details.append(info)
    return texts, details


# ---------------------------------------------------------------- demerged shares, returns, sell-all scenarios

def demerged_shares(rows, parents, lots, events, dm_events, *, bonuses, fetcher, clock, sleep, errors):
    """Demerged shares the file does not list, valued at their latest price. For dated holdings whose published cost split
    is known, also their tax lots: the apportioned cost and the parent's original purchase dates (the law counts the holding
    period from there). Later splits/bonuses of the new company (from Yahoo) change the share count."""
    out, cache = [], {}
    for r in rows:
        sym = r["symbol"]
        for d in r["demergers"]:
            res = d["resulting_symbol"]
            whole = int(D(str(d["entitled_new_shares"] or 0)))
            if d["resulting_in_holdings"] or not res or whole == 0:
                continue
            e = next((x for x in parents.get(sym, []) if x["record_date"].isoformat() == d["record_date"] and x["resulting_symbol"] == res), None)
            if e is None:
                continue
            if res not in cache:
                if cache:
                    sleep(0.3)
                cache[res] = None
                for attempt in range(2):  # a second try after a pause, as for the holdings' own prices
                    if attempt:
                        sleep(1.0)
                    try:
                        cache[res] = latest_price(stock(res)[1], fetcher=fetcher, clock=clock)
                        break
                    except (Block, KeyError, TypeError, ValueError, OSError, OverflowError, IndexError):
                        pass
                if cache[res] is None:
                    errors[res] = f"price unavailable for the demerged {res} shares; their value is not estimated"
            quote, rd = cache[res], e["record_date"]
            later = [x for x in (classify_actions(res, quote["actions"], bonuses)[0] if quote and quote.get("actions") else []) if x["date"] > rd]
            count = D(whole) * _product(x["ratio"] for x in later)
            item = {"parent": sym, "symbol": res, "company": e["resulting_company"], "record_date": rd, "entitled": D(whole),
                    "quantity": count, "conditional": d["conditional"], "price": quote["price"] if quote else None,
                    "price_date": quote["price_date"] if quote else None, "price_kind": quote["price_kind"] if quote else None,
                    "price_time": quote["price_time"] if quote else None, "value": count * quote["price"] if quote else None,
                    "cost": None, "lots": [], "events": later, "later_share_changes": [x["text"] for x in later]}
            if not d["conditional"] and e["parent_pct"] is not None:
                prior = [x for x in events.get(sym, []) + dm_events.get(sym, []) if x["date"] < rd]
                open_pre = fifo([l for l in lots if l["symbol"] == sym and l["date"] < rd], {sym: prior})[1]
                ratio = D(e["new"]) / D(e["per"])
                total = sum((l["quantity"] for l in open_pre), D(0)) * ratio
                scale = D(whole) / total if total else D(0)
                rp = e["resulting_pct"] / 100
                item["lots"] = [{"symbol": res, "date": l["date"], "side": "BUY", "quantity": l["quantity"] * ratio * scale,
                                 "price": l["price"] * rp / ratio, "time": "", "seq": 3_000_000 + k, "approx": l.get("approx", False)}
                                for k, l in enumerate(open_pre) if l["quantity"]]
                item["cost"] = sum((x["quantity"] * x["price"] for x in item["lots"]), D(0))
            out.append(item)
    return out


def flow_returns(flows, end_value, today, bench=None, bench_now=None):
    """Return on money-in/money-out flows [(date, amount)] ending at `end_value` today: simple return, days since the first buy,
    XIRR (only when the first buy is at least a year ago), and the same flows put into the benchmark on the same dates."""
    buys = -sum((a for _, a in flows if a < 0), D(0))
    sells = sum((a for _, a in flows if a > 0), D(0))
    if not buys:
        return None
    first = min(d for d, a in flows if a < 0)
    days = (today - first).days
    out = {"invested": buys, "received": sells, "value": end_value, "first_buy": first, "days": days,
           "return": (end_value + sells) / buys - 1, "annualised": xirr(flows + [(today, end_value)]) if days >= 365 else None}
    if bench and bench_now:
        units, ok = D(0), True
        for d, a in flows:
            hit = close_on_or_after(bench, d)
            if hit is None:
                ok = False
                break
            units -= a / hit[0]
        if ok:
            nifty_value = units * bench_now
            out.update({"benchmark_value": nifty_value, "benchmark_return": (nifty_value + sells) / buys - 1,
                        "benchmark_annualised": xirr(flows + [(today, nifty_value)]) if days >= 365 else None})
    return out


def returns_section(rows, lots, complete, extra_value, today, bench, bench_now):
    """Per-holding and portfolio returns for symbols whose dated lots fully explain the holding."""
    per, all_flows, total_value, total_extra = [], [], D(0), D(0)
    for r in rows:
        sym = r["symbol"]
        if sym not in complete or r["current_value"] is None:
            continue
        sym_lots = [l for l in lots if l["symbol"] == sym]
        flows = [(l["date"], (-1 if l["side"] == "BUY" else 1) * l["quantity"] * l["price"]) for l in sym_lots]
        one = flow_returns(flows, r["current_value"], today, bench, bench_now)
        if one is None:
            continue
        one.update({"symbol": sym, "approx_date": any(l.get("approx") for l in sym_lots)})
        if extra_value.get(sym):
            both = flow_returns(flows, r["current_value"] + extra_value[sym], today)
            one.update({"return_with_demerged": both["return"], "annualised_with_demerged": both["annualised"]})
        per.append(one)
        all_flows += flows
        total_value += r["current_value"]
        total_extra += extra_value.get(sym, D(0))
    if not per:
        return None
    portfolio = flow_returns(all_flows, total_value, today, bench, bench_now)
    portfolio["approx_date"] = any(x["approx_date"] for x in per)
    if total_extra:
        both = flow_returns(all_flows, total_value + total_extra, today)
        portfolio.update({"return_with_demerged": both["return"], "annualised_with_demerged": both["annualised"]})
    portfolio["symbols"] = [x["symbol"] for x in per]
    return {"holdings": per, "portfolio": portfolio}


def sell_all(open_lots, in_fy, price_of, undated_gains, rates, today):
    """Tax if every open lot with a price were sold today on top of this year's realised matches: (best, worst) where undated
    gains are long-term and losses short-term in the best case, the reverse in the worst."""
    unreal = {"short": [], "long": []}
    for l in open_lots:
        price = price_of.get(l["symbol"])
        if price is not None:
            unreal["long" if is_long_term(l["date"], today) else "short"].append((price - l["price"]) * l["quantity"])
    st = [m["gain"] for m in in_fy if m["term"] == "short"] + unreal["short"]
    lt = [m["gain"] for m in in_fy if m["term"] == "long"] + unreal["long"]
    best = tax(st + [g for g in undated_gains if g < 0], lt + [g for g in undated_gains if g > 0], rates)
    worst = tax(st + [g for g in undated_gains if g > 0], lt + [g for g in undated_gains if g < 0], rates)
    return best, worst


def loss_pair(pair):
    """(low, high) net long-term loss left over after set-off, across the (best, worst) cases of sell_all."""
    lo, hi = sorted(x.get("unused_long_term_loss") or 0 for x in pair)
    return lo, hi


def year_label(start):
    """'tax year 2026-27' under the Income-tax Act 2025 (from 1 April 2026), 'FY 2025-26' before it."""
    label = f"{start.year}-{(start.year + 1) % 100:02d}"
    return f"tax year {label}" if start >= NEW_ACT_DATE else f"FY {label}"


# ---------------------------------------------------------------- review

def review(source, tradebook=None, *, fy=None, rates=None, prices=True, fetcher=request, clock=instant, sleep=time.sleep, bonus=(),
           splits=(), bonuses=None, demergers=None, apply_demergers=False):
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
    if source["holdings"] is not None:
        dated = {l["symbol"] for l in lots}
        for n, h in enumerate(source["holdings"]):
            if h.get("bought") and h["symbol"] not in dated:
                lots.append({"symbol": h["symbol"], "date": h["bought"], "side": "BUY", "quantity": h["quantity"], "price": h["average_price"],
                             "time": "", "seq": 2_000_000 + n, "approx": bool(h.get("bought_approx"))})
                if h.get("bought_approx"):
                    notes.append(f"{h['symbol']}: purchase month only ({h['bought'].strftime('%Y-%m')}); the 1st of the month is assumed, so the "
                                 "long-term date may be off by up to a month")
    errors, quotes, sector_map = {}, {}, None
    symbols_for_prices = holdings_for_prices(source, lots)
    if prices:
        for n, h in enumerate(symbols_for_prices):
            if n:
                sleep(0.3)
            try:
                quotes[h["symbol"]] = latest_price(h["yahoo"], fetcher=fetcher, clock=clock)
            except (Block, KeyError, TypeError, ValueError, OSError, OverflowError, IndexError):
                errors[h["symbol"]] = "price unavailable (Yahoo daily chart failed or inconsistent); not estimated"
        # One more try for each symbol that failed, after the others (a timeout or rate limit often clears by then).
        for h in [h for h in symbols_for_prices if h["symbol"] in errors]:
            sleep(1.0)
            try:
                quotes[h["symbol"]] = latest_price(h["yahoo"], fetcher=fetcher, clock=clock)
                del errors[h["symbol"]]
            except (Block, KeyError, TypeError, ValueError, OSError, OverflowError, IndexError):
                errors[h["symbol"]] = "price unavailable (Yahoo daily chart failed twice, or inconsistent); not estimated"
        try:
            sector_map = sectors(fetcher=fetcher)
        except (Block, KeyError, TypeError, ValueError, OSError, UnicodeDecodeError):
            errors["sectors"] = "Nifty 500 industry list unavailable; sector exposure not computed"
    else:
        errors["prices"] = "--no-prices: values shown at average cost; no P&L, day change or sector data"


    bonus = {s.strip().upper().replace(".NS", "") for s in bonus}
    splits = {s.strip().upper().replace(".NS", "") for s in splits}
    if bonus & splits:
        raise Block(f"INPUT: {', '.join(sorted(bonus & splits))} given to both --bonus and --split")
    if bonuses is None:
        try:
            bonuses = load_bonuses()
        except Block as error:
            bonuses = []
            errors["bonuses"] = f"bonus reference list unavailable ({error}); Yahoo split events are treated as splits unless --bonus is given"
    events, corporate = {}, {}
    for s, q in quotes.items():
        if q.get("actions"):
            events[s], corporate[s] = classify_actions(s, q["actions"], bonuses, s in bonus, s in splits)
    if demergers is None:
        try:
            demergers = load_demergers()
        except Block as error:
            demergers = []
            errors["demergers"] = f"demerger reference list unavailable ({error}); demergers not checked"
    parents = {}
    for e in demergers:
        for name in e["symbols"]:
            parents.setdefault(name, []).append(e)
    # Published cost splits as FIFO events, for symbols with dated lots. Always used to work out the figures in warnings;
    # applied to the reported cost only with --apply-demergers.
    dm_events = {s: [{"date": e["record_date"], "kind": "demerger", "factor": e["parent_pct"] / 100,
                      "text": f"{e['resulting_symbol'] or e['resulting_company']} demerger (record date {e['record_date'].isoformat()}; "
                              f"published cost split, {e['parent_pct']}% stays with {s}, applied by --apply-demergers)"}
                     for e in parents[s] if e["parent_pct"] is not None]
                 for s in {l["symbol"] for l in lots} if s in parents}
    dm_events = {s: evs for s, evs in dm_events.items() if evs}
    fifo_events = events
    if apply_demergers and dm_events:
        fifo_events = {s: events.get(s, []) + dm_events.get(s, []) for s in set(events) | set(dm_events)}
    matches, open_lots, unmatched, fifo_notes = fifo(lots, fifo_events) if lots else ([], [], [], [])
    notes += fifo_notes
    dm_factor = {}
    if apply_demergers and dm_events:
        base, adjusted = {}, {}
        for target, source_lots in ((base, fifo(lots, events)[1]), (adjusted, open_lots)):
            for l in source_lots:
                target[l["symbol"]] = target.get(l["symbol"], D(0)) + l["quantity"] * l["price"]
        dm_factor = {s: adjusted[s] / base[s] for s in dm_events if base.get(s) and adjusted.get(s) is not None and adjusted[s] != base[s]}
    raw_open = {}
    if lots and events:
        for l in fifo(lots)[1]:
            raw_open[l["symbol"]] = raw_open.get(l["symbol"], D(0)) + l["quantity"]
    if source["holdings"] is None:
        derived = {}
        for l in open_lots:
            d = derived.setdefault(l["symbol"], {"quantity": D(0), "cost": D(0)})
            d["quantity"] += l["quantity"]
            d["cost"] += l["quantity"] * l["price"]
        holdings = [{"symbol": s, "yahoo": stock(s)[1], "quantity": d["quantity"], "average_price": d["cost"] / d["quantity"]}
                    for s, d in sorted(derived.items())]
        notes.append("Holdings derived from open FIFO lots in the tradebook; transfers and pre-tradebook buys are missing; bonuses/splits from Yahoo are applied")
    else:
        holdings = source["holdings"]
    if not holdings:
        raise Block("INPUT: no open holdings to review")

    adjusted_open = {}
    for l in open_lots:
        a = adjusted_open.setdefault(l["symbol"], {"quantity": D(0), "cost": D(0)})
        a["quantity"] += l["quantity"]
        a["cost"] += l["quantity"] * l["price"]
    # Typed buy/sell prices against what traded at the time (never changes the user's numbers).
    checks, doubtful_lots = {}, {}
    for n, l in enumerate(lots):
        quote = quotes.get(l["symbol"])
        if not quote or not quote.get("bars"):
            continue
        approx = bool(l.get("approx"))
        c = price_check(l["symbol"], l["side"], l["price"], l["date"], month_end(l["date"]) if approx else l["date"], quote["bars"],
                        events.get(l["symbol"], []), [e["record_date"] for e in parents.get(l["symbol"], [])], approx)
        checks.setdefault(l["symbol"], []).append(c)
        if c["status"] == "outside":
            doubtful_lots[n] = c
    rows = []
    held_qty = {h["symbol"]: h["quantity"] for h in holdings}
    for h in holdings:
        q, avg = h["quantity"], h["average_price"]
        q0, avg0 = q, avg
        warning, corporate_note, corporate_applied = None, None, False
        if h["symbol"] in corporate:
            fixed = adjusted_open.get(h["symbol"])
            if fixed and h["symbol"] in raw_open and q == raw_open[h["symbol"]] and fixed["quantity"] != q:
                corporate_applied = True
                corporate_note = (f"{h['symbol']}: adjusted for {'; '.join(corporate[h['symbol']])}: {qty(q)} shares at {inr(avg, 2)} "
                                  f"became {qty(fixed['quantity'])} at {inr(q * avg / fixed['quantity'], 2)} (same total cost)")
                q, avg = fixed["quantity"], (q * avg) / fixed["quantity"]
            elif not fixed:
                warning = (f"{'; '.join(corporate[h['symbol']])}. If your quantity and average cost are from before that date, "
                           f"multiply the quantity and divide the cost by the ratio; the P&L shown would otherwise be wrong."
                           + (" Broker holdings exports are normally already adjusted." if source["kind"] != "holdings_v1" else
                              " Give the purchase date (bought) so this can be applied automatically."))
        sym = h["symbol"]
        dm_applied, dm_note = False, None
        if sym in dm_factor:
            lots_q = adjusted_open.get(sym, {}).get("quantity")
            if source["holdings"] is None:
                dm_applied = True
                dm_note = f"{sym}: --apply-demergers: cost of lots bought before the demerger record date reduced by the published cost split"
            elif lots_q == q:
                dm_applied = True
                dm_note = (f"{sym}: --apply-demergers: average cost {inr(avg, 2)} became {inr(avg * dm_factor[sym], 2)} "
                           "(published cost split for lots held on the demerger record date; quantity unchanged)")
                avg = avg * dm_factor[sym]
            else:
                dm_note = f"{sym}: --apply-demergers not applied: open lots total {qty(lots_q)} shares but the holding shows {qty(q)}"
        applied_actions = None
        first_lot = min((l["date"] for l in lots if l["symbol"] == sym), default=None)
        skipped_actions = [{"kind": e["kind"], "date": e["date"].isoformat(), "ratio": (e.get("text") or "").split(" on ")[0]}
                           for e in events.get(sym, []) if first_lot is not None and e["date"] < first_lot]
        if corporate_applied:
            first = min((l["date"] for l in lots if l["symbol"] == sym), default=None)
            done = [e for e in events.get(sym, []) if first is None or e["date"] > first]
            applied_actions = {"from": f(q0, 4), "to": f(q, 4),
                               "kinds": sorted({e["kind"] for e in done}),
                               "events": [{"kind": e["kind"], "date": e["date"].isoformat(),
                                           "ratio": (e.get("text") or "").split(" on ")[0]} for e in done]}
        if corporate_applied and dm_applied and source["holdings"] is not None:
            # Both changed this holding: say what happened in date order, so each step's quantity and cost are right.
            steps = chronology([l for l in lots if l["symbol"] == sym], events.get(sym, []) + dm_events.get(sym, []))
            notes.append(f"{sym}: {qty(q0)} shares at {inr(avg0, 2)} (cost {inr(q0 * avg0)}), adjusted in date order: " + "; then ".join(steps)
                         + f". Result: {qty(q)} shares at {inr(avg, 2)} (cost {inr(q * avg)}).")
        else:
            notes += [n for n in (corporate_note, dm_note) if n]
        price_flags = [c for c in checks.get(sym, []) if c["status"] == "outside"]
        price_warning = None
        if price_flags:
            price_warning = " ".join(c["message"].split(": ", 1)[1] for c in price_flags[:3])
            if len(price_flags) > 3:
                price_warning += f" {len(price_flags) - 3} more price(s) for {sym} are outside their period's range (see JSON)."
        demerger_warning, demerger_details = None, []
        if sym in parents:
            sym_lots = [l for l in lots if l["symbol"] == sym]
            texts, demerger_details = demerger_checks(sym, parents[sym], sym_lots, events.get(sym, []) + dm_events.get(sym, []),
                                                      q, avg, h, held_qty, dm_applied)
            demerger_warning = " ".join(texts) or None
        invested = q * avg
        quote = quotes.get(h["symbol"])
        value = q * quote["price"] if quote else None
        day = q * (quote["price"] - quote["previous_close"]) if quote and quote["previous_close"] is not None else None
        stale = bool(quote) and (today - date.fromisoformat(quote["price_date"])).days > 7
        sector = None if sector_map is None else sector_map.get(h["symbol"], OTHER_SECTOR)
        rows.append({"symbol": h["symbol"], "quantity": q, "average_price": avg, "last_price": quote["price"] if quote else None,
                     "price_date": quote["price_date"] if quote else None, "price_stale": stale,
                     "price_kind": quote["price_kind"] if quote else None, "price_time": quote["price_time"] if quote else None,
                     "previous_close_date": quote["previous_close_date"] if quote else None,
                     "price_status": "ok" if quote else ("not loaded" if not prices else "price unavailable"),
                     "invested": invested, "current_value": value, "pnl": None if value is None else value - invested,
                     "pnl_pct": None if value is None or invested == 0 else (value - invested) / invested,
                     "day_change": day, "previous_value": None if day is None else value - day,
                     "weight_basis": value if value is not None else invested, "valued_at_cost": value is None,
                     "sector": sector, "splits_recent": quote["splits"] if quote else [], "corporate_action_warning": warning,
                     "corporate_action_applied": applied_actions, "corporate_action_before_purchase": skipped_actions or None,
                     "first_purchase": first_lot.isoformat() if first_lot else None,
                     "demerger_warning": demerger_warning, "demergers": demerger_details,
                     "price_warning": price_warning, "price_checks": price_flags,
                     "price_checks_ok": sum(1 for c in checks.get(sym, []) if c["status"] == "ok"),
                     "price_checks_not_checked": sum(1 for c in checks.get(sym, []) if c["status"] == "not_checked")})
        if stale:
            notes.append(f"{h['symbol']}: latest price is from {quote['price_date']} (more than 7 days old)")
        if quote and quote.get("price_note"):
            notes.append(f"{h['symbol']}: {quote['price_note']}")

    shown = {r["symbol"] for r in rows}
    sold_price_flags = {}
    for sym, found in sorted(checks.items()):
        if sym not in shown:
            notes += [c["message"] for c in found if c["status"] == "outside"]
            if any(c["status"] == "outside" for c in found):
                sold_price_flags[sym] = sum(1 for c in found if c["status"] == "outside")
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
              "unpriced_symbols": [r["symbol"] for r in rows if r["current_value"] is None] if prices else [],
              "invested_priced": f(invested_priced), "current_value": f(value_priced) if priced else None,
              "unrealised_pnl": f(value_priced - invested_priced) if priced else None,
              "unrealised_pnl_pct": f((value_priced - invested_priced) / invested_priced, 6) if priced and invested_priced else None,
              "day_change": f(day_total), "day_change_pct": f(day_total / prev_total, 6) if day_total is not None and prev_total else None,
              "price_dates": price_dates,
              "price_kind": "live" if any(r["price_kind"] == "live" for r in priced) else ("close" if priced else None),
              "price_times": sorted({r["price_time"] for r in priced if r["price_kind"] == "live" and r["price_time"]}),
              "last_close_dates": sorted({r["previous_close_date"] for r in priced if r["price_kind"] == "live" and r["previous_close_date"]})}
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

    # Demerged shares the file does not list (valued in case the user still holds them) and returns against the Nifty 50.
    entitled = demerged_shares(rows, parents, lots, events, dm_events, bonuses=bonuses, fetcher=fetcher, clock=clock, sleep=sleep,
                               errors=errors) if prices else []
    valued = [x for x in entitled if x["value"] is not None]
    with_demerged = None
    if valued and priced:
        extra = sum((x["value"] for x in valued), D(0))
        with_demerged = {"extra_value": f(extra), "current_value": f(value_priced + extra), "invested": f(invested_priced),
                         "unrealised_pnl": f(value_priced + extra - invested_priced),
                         "unrealised_pnl_pct": f((value_priced + extra - invested_priced) / invested_priced, 6) if invested_priced else None,
                         "conditional": any(x["conditional"] for x in valued)}
    lots_by_symbol = {}
    for l in open_lots:
        lots_by_symbol[l["symbol"]] = lots_by_symbol.get(l["symbol"], D(0)) + l["quantity"]
    complete = {r["symbol"] for r in rows if lots_by_symbol.get(r["symbol"]) == r["quantity"]}
    returns, bench, extra_by_parent = None, None, {}
    if prices and any(r["current_value"] is not None for r in rows if r["symbol"] in complete):
        sleep(0.3)
        try:
            bench = latest_price(BENCHMARK, fetcher=fetcher, clock=clock)
        except (Block, KeyError, TypeError, ValueError, OSError, OverflowError, IndexError):
            errors["benchmark"] = "Nifty 50 history unavailable; returns are shown without the index comparison"
        for x in valued:
            if not x["conditional"]:
                extra_by_parent[x["parent"]] = extra_by_parent.get(x["parent"], D(0)) + x["value"]
        returns = returns_section(rows, lots, complete, extra_by_parent, today, bench["bars"] if bench else None,
                                  bench["price"] if bench else None)
        if returns and bench:
            returns["benchmark"] = {"symbol": BENCHMARK, "name": "Nifty 50", "level": f(bench["price"]), "price_date": bench["price_date"],
                                    "price_kind": bench["price_kind"], "price_time": bench["price_time"],
                                    "basis": "price index (no dividends), like the stock returns here"}
        undated_syms = [r["symbol"] for r in rows if r["symbol"] not in complete and r["current_value"] is not None]
        if returns and undated_syms:
            returns["excluded"] = undated_syms
        unpriced_dated = [r["symbol"] for r in rows if r["symbol"] in complete and r["current_value"] is None]
        if returns and unpriced_dated:
            returns["unpriced"] = unpriced_dated  # dated holdings left out of the return because their price is missing

    tax_section, mid_idx = None, []
    lot_symbols = {l["symbol"] for l in lots}
    undated = [r for r in rows if r["symbol"] not in lot_symbols and r["pnl"] is not None]
    if lots or undated:
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
        for r in rows:
            held = lots_by_symbol.get(r["symbol"])
            if held is None:
                notes.append(f"{r['symbol']}: no tax lots supplied; not included in the unrealised short/long-term split")
            elif held != r["quantity"]:
                notes.append(f"{r['symbol']}: open lots total {qty(held)} shares but holdings show {qty(r['quantity'])}; lots may be incomplete")
        for u in unmatched:
            notes.append(f"{u['symbol']}: sell of {qty(u['quantity'])} on {u['date'].isoformat()} has no matching earlier buy in the file; its gain is not computed")
        spanned = set()
        for m in in_fy:
            for e in parents.get(m["symbol"], []):
                if m["buy_date"] < e["record_date"] <= m["sell_date"] and not (apply_demergers and e["parent_pct"] is not None):
                    spanned.add((m["symbol"], e["record_date"], e["resulting_symbol"] or e["resulting_company"], e["parent_pct"]))
        for s, rd, res, pp in sorted(spanned, key=lambda x: (x[0], x[1])):
            notes.append(f"{s}: shares bought before the {res} demerger (record date {rd.isoformat()}) were sold this year; the realised gain uses the "
                         "full original cost" + (f", but only {pp}% of it belongs to {s} (use --apply-demergers), so the gain is understated" if pp is not None
                                                 else "; the published cost split is not in the reference list, so the gain is understated"))
        if any(m["sell_date"] < RATE_DATE for m in in_fy):
            notes.append("Some sales in this FY are before 23 July 2024, when older rates applied (15% STCG; 10% LTCG above ₹1,00,000); the estimate uses the rates shown")
        if any(m["buy_date"] < GRANDFATHER_DATE for m in matches) or any(l["date"] < GRANDFATHER_DATE for l in open_lots):
            notes.append("Some lots were bought before 1 Feb 2018; grandfathering (31 Jan 2018 FMV) is not modelled, so long-term gains may be overstated")
        unrealised = {t: {"cost": f(cost[t]), "gain": f(sum(unreal[t], D(0))) if unreal[t] else None,
                          "lots": sum(1 for l in open_out if l["term_today"] == t),
                          "lots_without_price": sum(1 for l in open_out if l["term_today"] == t and l["unrealised_gain"] is None)}
                      for t in ("short", "long")}
        hypothetical = None
        if fy_start <= today <= fy_end and (any(unreal.values()) or undated):
            # Undated holdings could be short- or long-term: best case puts gains long-term and losses short-term; worst case the reverse.
            gains = [r["pnl"] for r in undated]
            best, worst = sell_all(open_lots, in_fy, price_of, gains, rates, today)
            hypothetical = {"description": f"If every holding with a price were sold at its last price, on top of the {year_label(fy_start)} realised gains",
                            "unpriced_symbols": sorted({l["symbol"] for l in open_lots if price_of.get(l["symbol"]) is None}
                                                       | {r["symbol"] for r in rows if r["current_value"] is None}),
                            "estimated_total_tax": worst["total_tax"], "estimated_total_tax_low": best["total_tax"],
                            "estimated_additional_tax": f(worst["total_tax"] - realised["total_tax"]),
                            "undated_symbols": [r["symbol"] for r in undated], "detail": worst, "detail_best_case": best}
            figure = lambda pair: {"estimated_total_tax_low": pair[0]["total_tax"], "estimated_total_tax": pair[1]["total_tax"]}
            same = lambda pair: (pair[0]["total_tax"], pair[1]["total_tax"]) == (best["total_tax"], worst["total_tax"])
            try:
                costs = sale_costs([(r["symbol"], r["quantity"], r["last_price"]) for r in priced])
                hypothetical["sale_costs"] = {k: (f(v) if isinstance(v, Decimal) else v) for k, v in costs.items()}
                hypothetical["cash_after_tax_and_costs"] = f(value_priced - costs["total"] - D(str(worst["total_tax"])))
                hypothetical["cash_after_tax_and_costs_high"] = f(value_priced - costs["total"] - D(str(best["total_tax"])))
            except (Block, OSError, ValueError, KeyError, TypeError) as error:
                errors["sale_costs"] = f"rate card unavailable ({error}); sale costs not estimated"
            all_dm = {s: events.get(s, []) + dm_events.get(s, []) for s in set(events) | set(dm_events)}
            hypothetical["unused_long_term_loss_low"], hypothetical["unused_long_term_loss"] = loss_pair((best, worst))
            if dm_events and not apply_demergers:
                # The law (s.49(2C)/(2D) of the 1961 Act, carried into the Income-tax Act 2025) splits the cost of shares held on
                # a demerger's record date between the two companies; the parent keeps only its published share of the cost.
                pair = sell_all(fifo(lots, all_dm)[1], in_fy, price_of, gains, rates, today)
                lo, hi = loss_pair(pair)
                hypothetical["with_demerger_cost_split"] = {
                    **figure(pair), "changes_figure": not same(pair), "symbols": sorted(dm_events),
                    "unused_long_term_loss_low": lo, "unused_long_term_loss": hi,
                    "changes_loss": (lo, hi) != (hypothetical["unused_long_term_loss_low"], hypothetical["unused_long_term_loss"]),
                    "basis": "published demerger cost splits applied to lots held on the record date (s.49(2C)/(2D) of the Income-tax "
                             "Act 1961, carried into the Income-tax Act 2025); assumes the typed prices are the original purchase prices"}
            taxable = [x for x in valued if x["lots"]]
            if taxable:
                extra_lots = [l for x in taxable for l in x["lots"]]
                ev = {**all_dm, **{x["symbol"]: x["events"] for x in taxable}}
                pair = sell_all(fifo(lots + extra_lots, ev)[1], in_fy, {**price_of, **{x["symbol"]: x["price"] for x in taxable}},
                                gains, rates, today)
                scenario = {**figure(pair), "symbols": [x["symbol"] for x in taxable],
                            "excluded": [x["symbol"] for x in valued if not x["lots"]]}
                try:
                    c2 = sale_costs([(r["symbol"], r["quantity"], r["last_price"]) for r in priced]
                                    + [(x["symbol"], x["quantity"], x["price"]) for x in taxable])
                    scenario.update({"sale_costs": f(c2["total"]), "sale_value": f(c2["sale_value"]),
                                     "cash_after_tax_and_costs": f(c2["sale_value"] - c2["total"] - D(str(pair[1]["total_tax"]))),
                                     "cash_after_tax_and_costs_high": f(c2["sale_value"] - c2["total"] - D(str(pair[0]["total_tax"])))})
                except (Block, OSError, ValueError, KeyError, TypeError):
                    pass
                hypothetical["with_demerged_shares"] = scenario
            if doubtful_lots:
                # Only prices that move the figure on their own are swapped for the middle of what traded in their period.
                swapped, alt = [], list(lots)
                for n, c in sorted(doubtful_lots.items()):
                    mid = (D(str(c["low"])) + D(str(c["high"]))) / 2
                    one = list(lots)
                    one[n] = {**lots[n], "price": mid}
                    if same(sell_all(fifo(one, fifo_events)[1], in_fy, price_of, gains, rates, today)):
                        continue
                    alt[n] = one[n]
                    mid_idx.append(n)
                    swapped.append({"symbol": lots[n]["symbol"], "side": lots[n]["side"], "typed_price": f(lots[n]["price"]), "midpoint": f(mid),
                                    "period": c["period"], "range_understated": not c.get("upper_bound_checked", True)})
                pair = sell_all(fifo(alt, fifo_events)[1], in_fy, price_of, gains, rates, today)
                if not (swapped and not same(pair)):
                    mid_idx.clear()
                if swapped and not same(pair):
                    hypothetical["at_range_midpoint"] = {**figure(pair), "prices": swapped}
                    if hypothetical.get("with_demerger_cost_split"):
                        hypothetical["at_range_midpoint"]["with_demerger_cost_split"] = figure(
                            sell_all(fifo(alt, all_dm)[1], in_fy, price_of, gains, rates, today))
        tax_section = {"financial_year": fy_label, "fy_start": fy_start.isoformat(), "fy_end": fy_end.isoformat(),
                       "year_label": year_label(fy_start), "law": "Income-tax Act 2025" if fy_start >= NEW_ACT_DATE else "Income-tax Act 1961",
                       "method": "FIFO per symbol; long-term when the sale date is more than 12 months after the purchase date",
                       "rates": {**rates, "basis": RATE_BASIS_2025 if fy_start >= NEW_ACT_DATE else RATE_BASIS},
                       "realised_matches": [{k: (v.isoformat() if isinstance(v, date) else f(v, 4) if k == "quantity" else f(v) if isinstance(v, Decimal) else v)
                                                for k, v in m.items()} for m in in_fy],
                       "realised": realised, "ltcg_exemption_remaining": realised["ltcg_exemption_remaining"],
                       "unrealised": unrealised, "open_lots": open_out, "hypothetical_sell_all": hypothetical,
                       "unmatched_sells": [{"symbol": u["symbol"], "date": u["date"].isoformat(), "quantity": f(u["quantity"], 4)} for u in unmatched],
                       "not_modelled": NOT_MODELLED,
                       "note": "For information; check with a tax professional. Not a recommendation to sell or harvest."}

    # The same "what if the ⚠ typed price were the middle of what traded then" for value/P&L and the return per year, with
    # every doubtful lot swapped (round 6: ITC as well as SBIN), so the P&L line shows their combined effect; the tax what-if
    # above keeps only the prices that move the tax. Holdings whose quantity the lots do not fully explain are left out (the
    # typed lot may not be what the average cost is built from).
    if doubtful_lots and priced:
        idx = sorted(doubtful_lots)
        alt = list(lots)
        for n in idx:
            alt[n] = {**lots[n], "price": (D(str(doubtful_lots[n]["low"])) + D(str(doubtful_lots[n]["high"]))) / 2}
        alt_cost = {}
        for l in fifo(alt, fifo_events)[1]:
            alt_cost[l["symbol"]] = alt_cost.get(l["symbol"], D(0)) + l["quantity"] * l["price"]
        by_sym = {r["symbol"]: r for r in priced}
        deltas = {}
        for s in {lots[n]["symbol"] for n in idx}:
            d = alt_cost.get(s, D(0)) - adjusted_open.get(s, {}).get("cost", D(0))
            if d and s in complete and s in by_sym:
                deltas[s] = d
        if deltas:
            inv = invested_priced + sum(deltas.values())
            scenario = {"prices": [{"symbol": lots[n]["symbol"], "side": lots[n]["side"], "typed_price": f(lots[n]["price"]),
                                    "midpoint": f(alt[n]["price"]), "period": doubtful_lots[n]["period"],
                                    "range_understated": not doubtful_lots[n].get("upper_bound_checked", True)}
                                   for n in idx if lots[n]["symbol"] in deltas],
                        "invested": f(inv), "unrealised_pnl": f(value_priced - inv),
                        "unrealised_pnl_pct": f((value_priced - inv) / inv, 6) if inv else None,
                        "holdings": [{"symbol": s, "invested": f(by_sym[s]["invested"] + d), "pnl": f(by_sym[s]["pnl"] - d),
                                      "pnl_pct": f((by_sym[s]["pnl"] - d) / (by_sym[s]["invested"] + d), 6) if by_sym[s]["invested"] + d else None}
                                     for s, d in sorted(deltas.items())],
                        "returns": None,
                        "basis": "each ⚠ typed price swapped for the middle of the Yahoo low-high of its period; current prices unchanged"}
            if returns:
                alt_ret = returns_section(rows, alt, complete, extra_by_parent, today, bench["bars"] if bench else None,
                                          bench["price"] if bench else None)
                if alt_ret:
                    p = alt_ret["portfolio"]
                    scenario["returns"] = {k: (f(p[k], 6) if p.get(k) is not None else None)
                                           for k in ("return", "annualised", "benchmark_return", "benchmark_annualised")}
                    scenario["returns"]["days"] = p["days"]
            totals["at_range_midpoint"] = scenario

    out_rows = []
    for r in rows:
        out_rows.append({k: (f(v, 4) if k == "quantity" else f(v, 6) if k in {"weight", "pnl_pct"} else f(v) if isinstance(v, Decimal) else v)
                         for k, v in r.items() if k not in {"weight_basis", "previous_value"}})
    plain = lambda v: (v.isoformat() if isinstance(v, date) else f(v, 4) if isinstance(v, Decimal) else v)
    demerged_out = [{k: plain(v) for k, v in x.items() if k not in {"lots", "events"}} | {"tax_lots": len(x["lots"])} for x in entitled]
    if returns:
        conv = lambda d: {k: (f(v, 6) if k in {"return", "annualised", "benchmark_return", "benchmark_annualised", "return_with_demerged",
                                                 "annualised_with_demerged"} else f(v) if isinstance(v, Decimal) else plain(v)) for k, v in d.items()}
        returns = {**returns, "holdings": [conv(x) for x in returns["holdings"]], "portfolio": conv(returns["portfolio"]),
                   "method": "Money-weighted annual return (XIRR) on your buy/sell dates and amounts, ending at today's value; shown only when "
                             "the first buy is at least a year old (otherwise the simple return). The Nifty 50 figures put the same rupees "
                             "into the index on the same dates. Neither includes dividends."}
    return {"schema": "holdings_review_v1", "as_of": now.isoformat(), "source": source["source"], "input_format": source["kind"],
            "holdings_as_of": source["as_of"], "price_source": ("Yahoo Finance public chart (SYMBOL.NS): the live price while the NSE session is "
                                                                "open, otherwise the latest daily close; may be delayed") if prices else None,
            "returns": returns, "demerged_shares": demerged_out, "with_demerged_shares": with_demerged,
            "sector_source": NIFTY500_URL if sector_map is not None else None,
            "bonus_reference": {"source": "assets/bonuses.json", "entries": len(bonuses), "forced_bonus": sorted(bonus),
                                "forced_split": sorted(splits), "checked": max(e["checked"] for e in bonuses).isoformat() if bonuses else None},
            "price_check": {"method": "typed buy/sell prices compared with the Yahoo daily low-high of the purchase day (or month, for "
                                      "month-only dates), scaled back to the share units of the time", "tolerance": f(PRICE_TOLERANCE, 4)},
            "demerger_reference": {"source": "assets/demergers.json", "entries": len(demergers), "applied": bool(apply_demergers),
                                   "checked": max(e["checked"] for e in demergers).isoformat() if demergers else None},
            "totals": totals, "holdings": out_rows, "concentration": concentration, "sectors": sector_rows, "tax": tax_section,
            "errors": errors, "notes": notes, "sold_price_flags": sold_price_flags,
            "disclaimer": DISCLAIMER, "execution_eligible": False, "submitted": False}


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


def price_basis(t):
    """How the prices were taken, for headers: live with its time during the session, otherwise the close with its date."""
    if t.get("price_kind") == "live":
        times = t.get("price_times") or []
        times = (times[0] if len(times) == 1 else f"{times[0]}–{times[-1]}") if times else "time not given by Yahoo"
        closes = ", ".join(t.get("last_close_dates") or []) or "n/a"
        return f"live price {times} IST (session in progress; Yahoo, may lag a minute or more); last close {closes}"
    if t.get("price_dates"):
        return f"closing price {', '.join(t['price_dates'])} (Yahoo)"
    return "no prices loaded"


def price_cell(h):
    if h["last_price"] is None:
        return h["price_status"]
    when = f"live {h['price_time']}" if h.get("price_kind") == "live" and h.get("price_time") else (
        "live" if h.get("price_kind") == "live" else h["price_date"])
    return f"{inr(h['last_price'], 2)} ({when}{' ⚠ stale' if h['price_stale'] else ''})"


def tax_range(low, high):
    return inr(high) if low == high else f"{inr(low)} to {inr(high)}"


def join_names(names):
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def verb(names, singular, plural_form):
    return singular if len(list(names)) == 1 else plural_form


def held_for(days):
    return f"{days / 365.25:.1f} yrs" if days >= 365 else f"{days} days"


def concentration_text(c, holdings):
    eq = f"like {c['effective_holdings']:.1f} equal-sized holdings" if c.get("effective_holdings") else ""
    if holdings <= 1:
        return "a single holding"
    return f"{c['reading']}" + (f" — {eq}" if eq else "")


def scenario_lines(h, x, rows):
    """Plain-language follow-ups to the sell-everything figure: sale costs, demerger splits, demerged shares, typed-price doubts."""
    out = []
    costs = h.get("sale_costs")
    if costs:
        cash = tax_range(h["cash_after_tax_and_costs"], h["cash_after_tax_and_costs_high"])
        out.append(f"Sale costs ≈ {inr(costs['total'])} (STT 0.1% {inr(costs['stt'])}, DP charges {inr(costs['dp'])}, exchange/SEBI/GST "
                   f"{inr(costs['other'])}; delivery rates from position_size.py's example card) → cash after tax and costs ≈ {cash}.")
    split = h.get("with_demerger_cost_split")
    loss = ""
    if split and split.get("changes_loss"):
        loss = (f" Net long-term loss left over after set-off: {tax_range(split['unused_long_term_loss_low'], split['unused_long_term_loss'])} "
                f"with the split, {tax_range(h.get('unused_long_term_loss_low', 0), h.get('unused_long_term_loss', 0))} on the full typed cost "
                "(s.49(2C)/(2D) of the 1961 Act, carried into the Income-tax Act 2025, require the split).")
    if split and split["changes_figure"]:
        out.append(f"With the published demerger cost splits applied to {join_names(split['symbols'])} (the cost the law uses if your typed price "
                   f"is the original purchase price; run with --apply-demergers): {tax_range(split['estimated_total_tax_low'], split['estimated_total_tax'])}.{loss}")
    elif split:
        out.append(f"Applying the published demerger cost splits to {join_names(split['symbols'])} does not change this figure.{loss}")
    dm = h.get("with_demerged_shares")
    if dm:
        text = (f"Including the demerged {join_names(dm['symbols'])} shares (assumed still held; apportioned cost, original purchase dates): "
                f"{tax_range(dm['estimated_total_tax_low'], dm['estimated_total_tax'])}")
        if dm.get("sale_costs") is not None:
            text += f", sale costs ≈ {inr(dm['sale_costs'])}, cash after tax and costs ≈ {tax_range(dm['cash_after_tax_and_costs'], dm['cash_after_tax_and_costs_high'])}"
        out.append(text + ".")
    mid = h.get("at_range_midpoint")
    if mid:
        parts = []
        for m in mid["prices"]:
            text = f"{m['symbol']} at {inr(m['midpoint'], 2)} (middle of the range traded {m['period']}) instead of {inr(m['typed_price'], 2)}"
            if m["range_understated"]:
                text += " — Yahoo's range before a demerger is scaled down, so the real midpoint is higher"
            parts.append(text)
        out.append(f"The ⚠ typed price drives this figure. With {'; '.join(parts)}: {tax_range(mid['estimated_total_tax_low'], mid['estimated_total_tax'])}.")
    return out


def returns_lines(r):
    ret = r.get("returns")
    if not ret:
        return []
    bench = ret.get("benchmark")
    lines = ["## Returns vs Nifty 50", "",
             "| Holding | First buy | Held | Your return | Per year | Nifty 50, same dates | Nifty per year |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for x in ret["holdings"] + [dict(ret["portfolio"], symbol="**All dated holdings**")]:
        first = x["first_buy"][:7] + " (month only)" if x.get("approx_date") and x is not None and x["symbol"] != "**All dated holdings**" else x["first_buy"]
        per_year = pct(x["annualised"]) if x["annualised"] is not None else "n/a (under 1 yr)"
        nifty = pct(x.get("benchmark_return")) if x.get("benchmark_return") is not None else "n/a"
        nifty_year = pct(x.get("benchmark_annualised")) if x.get("benchmark_annualised") is not None else "n/a"
        lines.append(f"| {x['symbol']} | {first} | {held_for(x['days'])} | {pct(x['return'])} | {per_year} | {nifty} | {nifty_year} |")
    lines.append("")
    for x in ret["holdings"]:
        if x.get("return_with_demerged") is not None:
            yr = f", {pct(x['annualised_with_demerged'])} a year" if x.get("annualised_with_demerged") is not None else ""
            lines.append(f"- {x['symbol']} including the shares received in its demerger (if you still hold them): {pct(x['return_with_demerged'])}{yr}.")
    p = ret["portfolio"]
    if p.get("return_with_demerged") is not None:
        yr = f", {pct(p['annualised_with_demerged'])} a year" if p.get("annualised_with_demerged") is not None else ""
        lines.append(f"- All dated holdings including demerged shares (if held): {pct(p['return_with_demerged'])}{yr}.")
    if ret.get("excluded"):
        lines.append(f"- Not included: {join_names(ret['excluded'])} ({verb(ret['excluded'], 'no purchase date', 'no purchase dates')}).")
    if any(x.get("approx_date") for x in ret["holdings"]):
        lines.append("- Month-only purchase dates are taken as the 1st, so per-year figures are approximate.")
    basis = f"Nifty 50 at {num(bench['level'])}" + (f" (live {bench['price_time']} IST)" if bench and bench.get("price_kind") == "live" and bench.get("price_time")
                                                       else f" ({bench['price_date']})") if bench else "Nifty 50 unavailable"
    lines += ["", f"{ret['method']} {basis}. Uses your typed prices, including any marked ⚠. Past returns say nothing about future returns.", ""]
    return lines


def demerged_lines(r):
    items = r.get("demerged_shares") or []
    if not items:
        return []
    lines = ["## Demerged shares not in your list", "",
             "| From | Company | Shares | Price | Value | Cost (apportioned) |", "| --- | --- | ---: | ---: | ---: | ---: |"]
    for x in items:
        when = "" if x["price"] is None else f" ({'live ' + x['price_time'] if x['price_kind'] == 'live' and x['price_time'] else x['price_date']})"
        cond = f" if bought before {x['record_date']}" if x["conditional"] else ""
        lines.append(f"| {x['parent']} | {re.sub(r' [(]formerly [^)]*[)]', '', x['company'])} ({x['symbol']}) | {qty(x['quantity'])}{cond} | {inr(x['price'], 2)}{when} | {inr(x['value'])} | "
                     f"{inr(x['cost']) if x['cost'] is not None else 'n/a'} |")
        if x["later_share_changes"]:
            lines.append(f"| | {x['symbol']}: {'; '.join(x['later_share_changes'])} (share count adjusted) | | | | |")
    w = r.get("with_demerged_shares")
    lines.append("")
    if w:
        lines.append(f"**If you still hold them:** +{inr(w['extra_value'])} → total {inr(w['current_value'])} on {inr(w['invested'])} invested, "
                     f"P&L {inr(w['unrealised_pnl'])} ({pct(w['unrealised_pnl_pct'])}). These shares were credited to your demat account automatically "
                     "on the demerger; check whether you still hold them.")
        if w["conditional"]:
            lines.append("Shares marked 'if bought before' apply only if the parent was bought before that record date.")
    lines.append("")
    return lines


def markdown(r):
    t, c = r["totals"], r["concentration"]
    priced = r["price_source"] is not None
    head = f"As of {r['as_of'][:16].replace('T', ' ')} IST · source: {r['source']}"
    if r["holdings_as_of"]:
        head += f" (holdings as of {r['holdings_as_of']})"
    if t["price_dates"]:
        head += f" · prices: {price_basis(t)}"
    lines = ["# Portfolio review", "", head, "", f"> {r['disclaimer']}", "", "## Summary", "", "| Measure | Value |", "| --- | ---: |",
             f"| Holdings | {t['holdings']}" + (f" ({t['priced_holdings']} priced)" if priced else "") + " |",
             f"| Invested (at average cost) | {inr(t['invested'])} |"]
    if priced:
        partial = " (priced holdings only)" if t["priced_holdings"] < t["holdings"] else ""
        day_label = (f"Day change (live vs {', '.join(t['last_close_dates'])} close)" if t.get("price_kind") == "live" and t.get("last_close_dates")
                     else "Day change (latest close vs previous close)")
        lines += [f"| Current value{partial} | {inr(t['current_value'])} |",
                  f"| Unrealised P&L{partial} | {inr(t['unrealised_pnl'])} ({pct(t['unrealised_pnl_pct'])}) |",
                  f"| {day_label} | {inr(t['day_change'])} ({pct(t['day_change_pct'], 2)}) |"]
        m = t.get("at_range_midpoint")
        if m:
            at = ", ".join(f"{x['symbol']} ~{inr(x['midpoint'])}" for x in m["prices"])
            ret = m.get("returns") or {}
            yr = f", return per year {pct(ret['annualised'])}" if ret.get("annualised") is not None else ""
            lines.append(f"| ⚠ If the typed price{'s' if len(m['prices']) > 1 else ''} outside what traded then were mid-range ({at}) | "
                         f"P&L {inr(m['unrealised_pnl'])} ({pct(m['unrealised_pnl_pct'])}){yr} |")
        w = r.get("with_demerged_shares")
        if w:
            lines.append(f"| Including demerged shares, if still held | {inr(w['current_value'])}, P&L {inr(w['unrealised_pnl'])} ({pct(w['unrealised_pnl_pct'])}) |")
        ret = r.get("returns")
        if ret and ret["portfolio"].get("annualised") is not None:
            p = ret["portfolio"]
            nifty = f" (Nifty 50 same dates {pct(p['benchmark_annualised'])})" if p.get("benchmark_annualised") is not None else ""
            gap = f" — excludes {join_names(ret['unpriced'])} (price unavailable), not a full figure" if ret.get("unpriced") else ""
            lines.append(f"| Return per year, dated holdings (XIRR) | {pct(p['annualised'])}{nifty}{gap} |")
    lines += [f"| Largest holding | {c['top_holding']} {pct(c['top_holding_weight'], sign=False)} |"]
    if t["holdings"] > 5:
        lines.append(f"| Top 5 holdings | {pct(c['top5_weight'], sign=False)} |")
    lines += [f"| Concentration | {concentration_text(c, t['holdings'])} |", ""]
    lines += ["## Holdings", "", "| Symbol | Qty | Avg cost | Last price | Invested | Current value | P&L | P&L % | Weight | Sector |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for h in r["holdings"]:
        flag = " ⚠" if h.get("corporate_action_warning") or h.get("demerger_warning") or h.get("price_warning") else ""
        lines.append(f"| {h['symbol']}{flag} | {qty(h['quantity'])} | {inr(h['average_price'], 2)} | {price_cell(h)} | {inr(h['invested'])} | {inr(h['current_value'])} | "
                     f"{inr(h['pnl'])} | {pct(h['pnl_pct'])} | {pct(h['weight'], sign=False)}{' *' if h['valued_at_cost'] and priced else ''} | {h['sector'] or 'n/a'} |")
    if any(h["valued_at_cost"] for h in r["holdings"]) and priced:
        lines.append("")
        lines.append("\\* price unavailable; weighted at average cost, P&L not shown.")
    flagged = [(h["symbol"], text) for h in r["holdings"]
               for text in (h.get("price_warning"), h.get("corporate_action_warning"), h.get("demerger_warning")) if text]
    if flagged:
        lines += ["", "**⚠ Check before trusting these numbers**", ""]
        lines += [f"- **{s}**: {text}" for s, text in flagged]
    lines.append("")
    lines += demerged_lines(r)
    lines += returns_lines(r)
    lines += ["## Concentration", "",
              f"Weights use {c['basis']}. The holdings are {concentration_text(c, t['holdings'])} (Herfindahl index, the sum of squared weights: "
              f"{num(c['herfindahl'], 3)}; below 0.10 is diversified, 0.10–0.18 moderately concentrated, above 0.18 highly concentrated).", ""]
    if r["sectors"] is not None:
        lines += ["## Sector exposure", "", f"Industry from the NSE Nifty 500 list ({r['sector_source']}).", "",
                  "| Sector | Holdings | Value | Weight |", "| --- | --- | ---: | ---: |"]
        lines += [f"| {s['sector']} | {', '.join(s['holdings'])} | {inr(s['value'])} | {pct(s['weight'], sign=False)} |" for s in r["sectors"]]
        lines.append("")
    x = r["tax"]
    if x:
        rt, rl = x["rates"], x["realised"]
        yl = x.get("year_label") or f"FY {x['financial_year']}"
        word = "tax year" if yl.startswith("tax year") else "FY"
        lt_section = "s.198 (formerly s.112A)" if word == "tax year" else "s.112A"
        has_lots = bool(x["realised_matches"] or x["open_lots"])
        lines += [f"## Capital-gains tax estimate — {yl} ({x['fy_start']} to {x['fy_end']})", "",
                  f"Method: {x['method']}. {rt['basis']}", "",
                  f"Rates used: STCG {pct(rt['stcg_rate'], sign=False)}, LTCG {pct(rt['ltcg_rate'], sign=False)} above {inr(rt['ltcg_exemption'])}, cess {pct(rt['cess_rate'], sign=False)}.", ""]
        if has_lots:
            lines += [f"### Realised this {word}", ""]
        if not has_lots:
            pass
        elif x["realised_matches"]:
            lines += ["| Symbol | Bought | Sold | Qty | Buy | Sell | Days held | Term | Gain |", "| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: |"]
            for m in x["realised_matches"][:60]:
                lines.append(f"| {m['symbol']} | {m['buy_date']} | {m['sell_date']} | {qty(m['quantity'])} | {inr(m['buy_price'], 2)} | {inr(m['sell_price'], 2)} | "
                             f"{m['days_held']} | {m['term']} | {inr(m['gain'])} |")
            if len(x["realised_matches"]) > 60:
                lines.append(f"| … {len(x['realised_matches']) - 60} more in JSON output | | | | | | | | |")
        else:
            lines.append(f"No sales matched in this {word}.")
        realised_lines = ["", "| Line | Short-term | Long-term |", "| --- | ---: | ---: |",
                  f"| Gains | {inr(rl['short_term_gains'])} | {inr(rl['long_term_gains'])} |",
                  f"| Losses | {inr(-rl['short_term_losses'])} | {inr(-rl['long_term_losses'])} |",
                  f"| Short-term loss set off against long-term | | {inr(-rl['short_term_loss_set_off_against_long_term'])} |",
                  f"| After set-off | {inr(rl['short_term_after_set_off'])} | {inr(rl['long_term_after_set_off'])} |",
                  f"| LTCG exemption used | | {inr(-rl['ltcg_exemption_used'])} |",
                  f"| Taxable | {inr(rl['taxable_short_term'])} | {inr(rl['taxable_long_term'])} |",
                  f"| Tax at rate | {inr(rl['stcg_tax'])} | {inr(rl['ltcg_tax'])} |",
                  f"| Cess ({pct(rt['cess_rate'], 0, sign=False)} on tax) | {inr(rl['cess'])} | |",
                  f"| **Estimated tax on realised gains** | **{inr(rl['total_tax'])}** | |", "",
                  f"LTCG exemption remaining this {word} (information): {inr(x['ltcg_exemption_remaining'])}. The exemption is shared with other {lt_section} gains outside this file, such as equity mutual funds.", ""]
        if has_lots:
            lines += realised_lines
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
            low, high = h.get("estimated_total_tax_low", h["estimated_total_tax"]), h["estimated_total_tax"]
            amount = inr(high) if low == high else f"between {inr(low)} and {inr(high)}"
            lines += ["", f"**If you sold everything today** (hypothetical, for information only): {h['description'][0].lower() + h['description'][1:]}, "
                      f"estimated tax for the {yl} would be **{amount}**. This is arithmetic, not a suggestion to sell."]
            if h.get("unpriced_symbols"):
                lines.append(f"⚠ Excludes {join_names(h['unpriced_symbols'])} (price unavailable) — not a full figure; the tax with "
                             f"{verb(h['unpriced_symbols'], 'it', 'them')} sold could be higher or lower.")
            undated = h.get("undated_symbols") or []
            if undated and low != high:
                lines.append(f"The range is because the purchase {verb(undated, 'date', 'dates')} of {join_names(undated)} {verb(undated, 'is', 'are')} unknown: the low end assumes "
                             "the gains are long-term and losses short-term, the high end the reverse. Give purchase dates (or a tradebook) to get one figure.")
            elif undated:
                lines.append(f"The purchase {verb(undated, 'date', 'dates')} of {join_names(undated)} {verb(undated, 'is', 'are')} unknown, but that does not change this figure.")
            extra = scenario_lines(h, x, r["holdings"])
            if extra:
                lines += [""] + [f"- {e}" for e in extra]
        lines += ["", "### Not modelled", ""] + [f"- {item}" for item in x["not_modelled"]]
        lines += ["", x["note"], ""]
    gaps = [f"- {k}: {v}" for k, v in r["errors"].items()] + [f"- {n}" for n in r["notes"]]
    if gaps:
        lines += ["## Data gaps and notes", ""] + gaps + [""]
    lines += ["Research only. execution_eligible: false · submitted: false. Nothing here places an order."]
    return "\n".join(lines) + "\n"


def flags(r):
    """Holdings needing a check, grouped: [(label, [symbols])] in a fixed order, empty groups dropped. A symbol with more than
    one typed price outside what traded then appears as 'SBIN ×2', and a symbol already sold in full whose typed price was
    flagged is listed too (its ⚠ sits in the notes), so the count matches every ⚠ the full report raises."""
    rows = r["holdings"]
    priced = [(h["symbol"], max(1, len(h.get("price_checks") or []))) for h in rows if h.get("price_warning")]
    priced += sorted((r.get("sold_price_flags") or {}).items())
    groups = [("buy/sell price", [f"{sym} ×{n}" if n > 1 else sym for sym, n in priced]),
              ("bonus/split", [h["symbol"] for h in rows if h.get("corporate_action_warning")]),
              ("demerger", [h["symbol"] for h in rows if h.get("demerger_warning")]),
              ("stale price", [h["symbol"] for h in rows if h.get("price_stale")]),
              ("missing price", [h["symbol"] for h in rows if h.get("price_status") == "price unavailable"])]
    return [(label, syms) for label, syms in groups if syms]


def flag_count(item):
    """1 for 'SBIN', 2 for 'SBIN ×2'."""
    head, _, n = item.rpartition(" ×")
    return int(n) if head and n.isdigit() else 1


def demerged_line(r):
    """Summary line saying whether each demerged company's shares are in the holdings, with their value when they are not;
    None when no demerger applies."""
    rows = r["holdings"] if isinstance(r, dict) else r
    valued = {(x["parent"], x["symbol"]): x for x in (r.get("demerged_shares") or [])} if isinstance(r, dict) else {}
    missing, present = [], []
    for h in rows:
        for d in h.get("demergers", []):
            res = d["resulting_symbol"] or d["resulting_company"]
            expected = int(d["entitled_new_shares"] or 0)
            v = valued.get((h["symbol"], res))
            worth = f", worth {inr(v['value'])}" if v and v["value"] is not None else ""
            if d.get("resulting_in_holdings"):
                present.append(f"{res} ({qty(d['resulting_quantity_in_holdings'])} held, from {h['symbol']})")
            elif expected == 0:
                continue  # only a fractional entitlement, paid in cash
            elif d["conditional"]:
                missing.append(f"{res} ({qty(v['quantity'] if v else expected)} expected if your {h['symbol']} was bought before {d['record_date']}{worth})")
            else:
                missing.append(f"{res} ({qty(v['quantity'] if v else expected)} expected from {h['symbol']}{worth})")
    if not (missing or present):
        return None
    parts = []
    if missing:
        text = (f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not in your holdings — check your demat account; "
                "the value and P&L above exclude them")
        w = r.get("with_demerged_shares") if isinstance(r, dict) else None
        if w:
            text += (f". If you still hold them: +{inr(w['extra_value'])} → total {inr(w['current_value'])}, "
                     f"P&L {inr(w['unrealised_pnl'])} ({pct(w['unrealised_pnl_pct'])})")
        h = ((r.get("tax") or {}).get("hypothetical_sell_all") or {}) if isinstance(r, dict) else {}
        dm = h.get("with_demerged_shares")
        if dm:
            # The demerged-shares scenario uses the published cost split, so it is compared with the split figure when there is one.
            base = h.get("with_demerger_cost_split") or h
            same = (dm["estimated_total_tax_low"], dm["estimated_total_tax"]) == (base.get("estimated_total_tax_low", base["estimated_total_tax"]),
                                                                              base["estimated_total_tax"])
            text += (f"; selling {join_names(dm['symbols'])} too: tax "
                     + ("unchanged" if same else tax_range(dm["estimated_total_tax_low"], dm["estimated_total_tax"]))
                     + (f", sale costs ≈ {inr(dm['sale_costs'])}" if dm.get("sale_costs") is not None else ""))
        parts.append(text)
    if present:
        parts.append(f"{', '.join(present)} {'is' if len(present) == 1 else 'are'} in your holdings")
    return "- **Demerged shares**: " + " · ".join(parts)


def mid_price_text(m):
    """'ITC ~₹420 in Jun 2023 (Yahoo understates it before a demerger)' — one ⚠ price at the middle of what traded then."""
    period = m["period"]
    when = period if re.fullmatch(r"in [A-Z][a-z]{2} \d{4}", period) else f"(traded {period})"
    side = "" if m.get("side", "BUY") == "BUY" else " sell"
    return (f"{m['symbol']}{side} ~{inr(m['midpoint'])} {when}"
            + (" (Yahoo understates it before a demerger)" if m.get("range_understated") else ""))


def midpoint_text(m):
    """'if SBIN's real buy price was ~₹960 (middle of its Oct 2026 range)'."""
    period = m["period"]
    where = (f"its {period[3:]} range" if re.fullmatch(r"in [A-Z][a-z]{2} \d{4}", period) else f"the range it traded {period}")
    side = "buy" if m.get("side", "BUY") == "BUY" else "sell"
    return (f"{m['symbol']}'s real {side} price was ~{inr(m['midpoint'])} (middle of {where}"
            + (", which Yahoo understates before a demerger" if m.get("range_understated") else "") + ")")


def sell_all_lines(r, h):
    """The 'if you sold everything today' part of the summary: the figure (with sale costs) on one line, then short
    sub-bullets for what qualifies it — a typed price that drives it, demerger cost splits, the long-term loss left over.
    Prices that are missing make it 'not a full figure'."""
    typed_low, typed_high = h.get("estimated_total_tax_low", h["estimated_total_tax"]), h["estimated_total_tax"]
    split = h.get("with_demerger_cost_split")
    # When a published demerger cost split changes a figure, the split basis (the cost the law uses) leads and the typed-cost
    # figure follows, labelled; tax, the typed-price midpoint and the loss left over are all on that one basis.
    legal = split if split and (split["changes_figure"] or split.get("changes_loss")) else None
    low, high = (split["estimated_total_tax_low"], split["estimated_total_tax"]) if legal else (typed_low, typed_high)
    yl = r["tax"].get("year_label") or f"FY {r['tax']['financial_year']}"
    unpriced = h.get("unpriced_symbols") or []
    rows = r["holdings"]
    text = f"- **If you sold everything today**: estimated {yl} tax " + (inr(high) if low == high else f"{inr(low)} to {inr(high)}")
    why = zero_tax_reason(r, legal if legal and legal.get("detail") else h) if high == 0 and low == 0 else None
    if why:
        text += f" ({why})"
    undated = h.get("undated_symbols") or []
    if undated:
        text += (f" (a range because {join_names(undated)} {verb(undated, 'has', 'have')} no purchase date)" if low != high
                 else f" ({join_names(undated)} undated; it does not change the figure)")
    if h.get("sale_costs"):
        c = h["sale_costs"]
        text += f", plus sale costs ≈ {inr(c['total'])} (STT {inr(c['stt'])}, DP {inr(c['dp'])}, other {inr(c['other'])})"
    if unpriced:
        text += f" — excludes {join_names(unpriced)} (price unavailable), not a full figure"
    text += term_text(r, h)
    out, mid_line = [text], []
    mid = h.get("at_range_midpoint")
    if mid:
        # The tax what-if swaps only the ⚠ prices that change the tax on their own; it says so, since the P&L what-if above swaps all.
        names = list(dict.fromkeys(m["symbol"] for m in mid["prices"]))
        doubtful = [x["symbol"] for x in rows if x.get("price_warning")]
        fig = mid.get("with_demerger_cost_split") if legal and mid.get("with_demerger_cost_split") else mid
        only = (f" (the only ⚠ {verb(names, 'price', 'prices')} that {verb(names, 'changes', 'change')} the tax)"
                if set(doubtful) - set(names) else "")
        mid_line.append(f"  - ⚠ At {' and '.join(mid_price_text(m) for m in mid['prices'])}{only}: tax "
                        f"{tax_range(fig['estimated_total_tax_low'], fig['estimated_total_tax'])}")
    if legal and split["changes_figure"]:
        out.append(f"  - Uses the published demerger cost split for {join_names(split['symbols'])}, the cost the law uses (assumes your typed "
                   f"{verb(split['symbols'], 'price is', 'prices are')} what you paid before the demerger); on your full typed cost, which "
                   f"overstates the {join_names(split['symbols'])} cost: {tax_range(typed_low, typed_high)}")
    out += mid_line  # after the basis it is computed on, before the loss left over
    typed_loss = (h.get("unused_long_term_loss_low"), h.get("unused_long_term_loss"))
    if typed_loss[1] is None:  # reports from before these keys existed
        typed_loss = tuple(sorted((h.get(k) or {}).get("unused_long_term_loss") or 0 for k in ("detail_best_case", "detail")))
    lo, hi = (split["unused_long_term_loss_low"], split["unused_long_term_loss"]) if legal else typed_loss
    amount = lambda a, b: inr(b) if a == b else f"{inr(a)} to {inr(b)}"
    if legal and split.get("changes_loss") and (hi > 0 or typed_loss[1] > 0):
        out.append(f"  - Net long-term loss left over: {amount(lo, hi)} on the demerger cost split the law applies to "
                   f"{join_names(split['symbols'])} ({amount(*typed_loss)} on the full typed cost, which the law does not allow)"
                   + (" — can be carried forward 8 years against long-term gains if you file on time (not modelled)" if hi > 0 else ""))
    elif hi > 0:
        out.append(f"  - Net long-term loss left over: {amount(lo, hi)} — it cannot reduce short-term gains, but can be carried forward "
                   "against long-term gains for up to 8 years if you file your return on time (information; not modelled)")
    return out


def lakh(v):
    """'₹1.25 lakh' for 125000; plain rupees otherwise."""
    return f"₹{v / 100000:g} lakh" if v >= 100000 and v % 1000 == 0 else inr(v)


def zero_tax_reason(r, h):
    """Why a sell-everything estimate is ₹0, from its set-off detail: '₹17,144 short-term loss set off against long-term gains;
    ₹93,356 net long-term within the ₹1.25 lakh exemption', or 'no net gain to tax: losses only'; None without the detail."""
    d = h.get("detail") or {}
    if not d:
        return None
    parts = []
    st_off = d.get("short_term_loss_set_off_against_long_term") or 0
    st_loss, st_gain = d.get("short_term_losses") or 0, d.get("short_term_gains") or 0
    lt_loss = d.get("long_term_losses") or 0
    if st_gain and st_loss >= st_gain:
        parts.append(f"{inr(st_gain)} short-term gain offset by short-term losses")
    if st_off:
        parts.append(f"{inr(st_off)} short-term loss set off against long-term gains")
    lt_left = d.get("long_term_after_set_off") or 0
    exemption = ((r.get("tax") or {}).get("rates") or {}).get("ltcg_exemption")
    if lt_left > 0 and not d.get("taxable_long_term") and exemption:
        parts.append(f"{inr(lt_left)}{' net' if lt_loss or st_off else ''} long-term within the {lakh(exemption)} exemption")
    if not parts and not d.get("taxable_short_term") and not d.get("taxable_long_term"):
        parts.append("no net gain to tax: losses only" if not (st_gain or d.get("long_term_gains")) else "no taxable gain after set-off")
    return "; ".join(parts) or None


def term_text(r, h):
    """' · all ₹22,404 long-term' or ' · ₹12,000 long-term, ₹3,000 short-term' for the sell-everything line: the unrealised
    gain on open lots by holding period today (more than 12 months = long-term), with undated holdings named. '' when the
    lots carry no priced gain."""
    u = (r.get("tax") or {}).get("unrealised") or {}
    lt, st = (u.get("long") or {}).get("gain"), (u.get("short") or {}).get("gain")
    if lt is None and st is None:
        return ""
    undated = h.get("undated_symbols") or []
    tail = f"; {join_names(undated)} undated, term unknown" if undated else ""
    if st is None:
        return f" · {'all ' if not undated else ''}{inr(lt)} long-term{tail}"
    if lt is None:
        return f" · {'all ' if not undated else ''}{inr(st)} short-term{tail}"
    return f" · {inr(lt)} long-term, {inr(st)} short-term{tail}"


def applied_actions_line(r):
    """'- **Bonus applied** (…): HDFCBANK 100 → 200 (1:1, Aug 2025)' for every holding whose quantity and cost were adjusted
    for a bonus or split after its purchase date; None when nothing was applied. Informational, not a ⚠."""
    applied = [h for h in r["holdings"] if (h.get("corporate_action_applied") or {}).get("events")]
    before = before_purchase_text(r)
    if not applied:
        return f"- **Bonus/split not applied**: {before}" if before else None
    kinds = {k for h in applied for k in h["corporate_action_applied"]["kinds"]}
    one = len(kinds) == 1
    label = {"bonus": "Bonus", "split": "Split"}.get(next(iter(kinds)), "Bonus/split") if one else "Bonus/split"
    ratio = lambda e: e["ratio"][:-len(e["kind"]) - 1] if one and e["ratio"].endswith(" " + e["kind"]) else e["ratio"]
    items = []
    for h in applied:
        a = h["corporate_action_applied"]
        what = "; ".join(f"{ratio(e)}, {date.fromisoformat(e['date']):%b %Y}" for e in a["events"])
        items.append(f"{h['symbol']} {qty(a['from'])} → {qty(a['to'])} ({what})")
    return (f"- **{label} applied** (shares and average cost adjusted, total cost unchanged): " + " · ".join(items)
            + (f" · not applied: {before}" if before else ""))


def before_purchase_text(r):
    """'WIPRO 1:1 bonus Dec 2024 predates purchase (Dec 2025)' for known bonuses/splits dated before a holding's
    first purchase (the typed quantity and price are already after them); None when there are none."""
    items = []
    for h in r["holdings"]:
        events = h.get("corporate_action_before_purchase") or []
        if not events:
            continue
        what = "; ".join(f"{e['ratio']} {date.fromisoformat(e['date']):%b %Y}" for e in events)
        bought = f" ({date.fromisoformat(h['first_purchase']):%b %Y})" if h.get("first_purchase") else ""
        items.append(f"{h['symbol']} {what} predate{'s' if len(events) == 1 else ''} purchase{bought}")
    return " · ".join(items) or None


def legal_split(h):
    """The demerger cost split when it changes a sell-everything figure (then it is the basis the summary leads with)."""
    split = (h or {}).get("with_demerger_cost_split")
    return split if split and (split["changes_figure"] or split.get("changes_loss")) else None


def what_if_line(r):
    """One sub-bullet under the value line for typed prices outside what traded then (⚠): the combined P&L and return per
    year with every ⚠ price at the middle of its period's range. The tax what-if (only the prices that move the tax) sits
    under the sell-everything line. None when nothing is flagged."""
    m = r["totals"].get("at_range_midpoint")
    h = (r.get("tax") or {}).get("hypothetical_sell_all") or {}
    doubtful = [x["symbol"] for x in r["holdings"] if x.get("price_warning")]
    if not (m or doubtful):
        return None
    prices = (m or {}).get("prices") or (h.get("at_range_midpoint") or {}).get("prices")
    if not prices:
        return (f"  - ⚠ {join_names(doubtful)} typed {verb(doubtful, 'price is', 'prices are')} outside what traded then, which does "
                "not change the P&L" + (" or the tax figure" if h else ""))
    names = list(dict.fromkeys(x["symbol"] for x in prices))
    text = (f"  - ⚠ {join_names(names)} typed {verb(names, 'price is', 'prices are')} outside what traded then; at "
            + " and ".join(mid_price_text(x) for x in prices) + ": ")
    if not m:
        return text + "P&L not recomputed (the quantity is not fully explained by the typed lot)"
    text += f"P&L {inr(m['unrealised_pnl'])} ({pct(m['unrealised_pnl_pct'])})"
    ret = m.get("returns")
    if ret and ret.get("annualised") is not None:
        text += f", return per year {pct(ret['annualised'])}"
    elif ret and ret.get("return") is not None:
        text += f", return {pct(ret['return'])} (under a year)"
    if h and not h.get("at_range_midpoint"):
        text += " · tax figure unchanged"
    return text


def summary(r):
    """Short answer-first block: value, P&L, day change, returns vs Nifty, best/worst, concentration, top sector, tax, flags."""
    t, c, rows = r["totals"], r["concentration"], r["holdings"]
    lines = [f"# Portfolio summary — {r['as_of'][:10]}", ""]
    missing = t.get("unpriced_symbols") or []
    if t["current_value"] is not None:
        partial = (f" ({t['priced_holdings']} of {t['holdings']} holdings priced)" if t["priced_holdings"] < t["holdings"] else "")
        if missing:
            partial += f" — excludes {join_names(missing)} (price unavailable), not a full figure"
        # Best and worst share the value line (a typed price outside what traded then is marked ⚠), and the ⚠ what-if follows it.
        ranked = sorted((h for h in rows if h["pnl_pct"] is not None), key=lambda h: h["pnl_pct"])
        mark = lambda h: f"{h['symbol']}{' ⚠' if h.get('price_warning') else ''} {pct(h['pnl_pct'])} ({inr(h['pnl'])})"
        ranking = ""
        if len(ranked) > 1:
            ranking = f" · **Best** {mark(ranked[-1])} · **worst** {mark(ranked[0])}"
        elif ranked:
            ranking = f" · **Only priced holding** {mark(ranked[0])}"
        lines.append(f"- **Value** {inr(t['current_value'])} on {inr(t['invested_priced'])} invested: "
                     f"P&L **{inr(t['unrealised_pnl'])} ({pct(t['unrealised_pnl_pct'])})**{partial}{ranking}")
        what_if = what_if_line(r)
        if what_if:
            lines.append(what_if)
        since = f" since the {', '.join(t['last_close_dates'])} close" if t.get("price_kind") == "live" and t.get("last_close_dates") else ""
        lines.append(f"- **Day change** {inr(t['day_change'])} ({pct(t['day_change_pct'], 2)}){since}")
        ret = r.get("returns")
        if ret:
            p = ret["portfolio"]
            if p.get("annualised") is not None:
                text = f"- **Return per year** {pct(p['annualised'])}"
                text += (f" vs Nifty 50 {pct(p['benchmark_annualised'])} on the same dates" if p.get("benchmark_annualised") is not None
                         else " (Nifty 50 comparison unavailable)")
                notes = [f"XIRR on {join_names(p['symbols'])}"]
            else:
                text = f"- **Return** {pct(p['return'])} on {join_names(p['symbols'])}"
                text += (f" vs Nifty 50 {pct(p['benchmark_return'])} on the same dates" if p.get("benchmark_return") is not None
                         else " (Nifty 50 comparison unavailable)")
                text += f" over {held_for(p['days'])} (under a year, so not annualised)"
                notes = []
            if p.get("approx_date"):
                notes.append("month-only dates, so approximate")
            if ret.get("excluded"):
                notes.append(f"{join_names(ret['excluded'])} {verb(ret['excluded'], 'has', 'have')} no purchase date")
            if notes:
                text += f" ({'; '.join(notes)})"
            if p.get("annualised_with_demerged") is not None:
                text += f" · {pct(p['annualised_with_demerged'])} a year counting the demerged shares, if still held"
            if ret.get("unpriced"):
                text += f" — excludes {join_names(ret['unpriced'])} (price unavailable), not a full figure"
            lines.append(text)
    else:
        lines.append(f"- **Invested** {inr(t['invested'])} at average cost; no prices loaded, so no P&L or day change")
    largest = f"- **Largest holding** {c['top_holding']} {pct(c['top_holding_weight'], sign=False)}"
    if t["holdings"] > 5:
        largest += f" · top 5 {pct(c['top5_weight'], sign=False)}"
    largest += f" ({concentration_text(c, t['holdings'])})"
    if r["sectors"]:
        s = r["sectors"][0]
        largest += f" · **top sector** {s['sector']} {pct(s['weight'], sign=False)}" + (
            f" ({', '.join(s['holdings'])})" if s["holdings"] != [c["top_holding"]] else "")
    lines.append(largest)
    h = (r["tax"] or {}).get("hypothetical_sell_all")
    if h:
        lines += sell_all_lines(r, h)
    else:
        lines.append("- **If you sold everything today**: tax not estimated (needs prices, and the current tax year)")
    demerged = demerged_line(r)
    if demerged:
        lines.append(demerged)
    applied = applied_actions_line(r)
    if applied:
        lines.append(applied)
    groups = flags(r)
    if groups:
        count = sum(flag_count(x) for _, s in groups for x in s)
        lines.append(f"- **⚠ {count} item{'s' if count != 1 else ''} to check**: " + "; ".join(f"{label} {', '.join(s)}" for label, s in groups))
    else:
        lines.append("- **⚠ Nothing flagged** (buy/sell price, bonus/split, demerger, stale or missing price)")
    dates = f"prices: {price_basis(t)}" if t["price_dates"] else "no prices"
    lines += ["", f"_{dates}. Research only, not advice or a suggestion to sell. Run without --summary for the full report and the ⚠ details._"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="Review personal Indian equity holdings: P&L, concentration, sectors and an FIFO capital-gains estimate (research only).")
    p.add_argument("--input", required=True, help="holdings_v1 JSON, Zerodha Console holdings CSV, or Zerodha tradebook CSV")
    p.add_argument("--tradebook", help="Zerodha tradebook CSV for tax lots (equity delivery rows, segment EQ)")
    p.add_argument("--fy", help="Indian tax year (financial year before April 2026) such as 2026-27 (default: the one containing today in IST)")
    p.add_argument("--stcg-rate", type=float, default=DEFAULT_RATES["stcg_rate"], help="default 0.20 (s.111A) — verify current rates")
    p.add_argument("--ltcg-rate", type=float, default=DEFAULT_RATES["ltcg_rate"], help="default 0.125 (s.112A) — verify current rates")
    p.add_argument("--ltcg-exemption", type=float, default=DEFAULT_RATES["ltcg_exemption"], help="default 125000 rupees per FY")
    p.add_argument("--cess-rate", type=float, default=DEFAULT_RATES["cess_rate"], help="default 0.04 health and education cess")
    p.add_argument("--no-prices", action="store_true", help="offline: no network; values at average cost")
    p.add_argument("--bonus", action="append", default=[], metavar="SYMBOL",
                   help="Treat this symbol's Yahoo split events as bonus issues (nil-cost new shares dated from the allotment date when "
                        "listed in assets/bonuses.json, else the ex-date) even if they are not in the list; repeatable")
    p.add_argument("--split", action="append", default=[], metavar="SYMBOL",
                   help="Treat this symbol's Yahoo split events as splits even when they match a listed bonus issue; repeatable")
    p.add_argument("--apply-demergers", action="store_true",
                   help="Apply published demerger cost splits (assets/demergers.json) to dated lots/holdings bought before the record date; "
                        "default is to flag them only")
    p.add_argument("--summary", action="store_true", help="Short answer-first summary instead of the full report")
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
        report = review(source, tradebook, fy=a.fy, rates=rates, prices=not a.no_prices, bonus=a.bonus, splits=a.split,
                        apply_demergers=a.apply_demergers)
    except Block as error:
        print(f"BLOCKED - {error}", file=sys.stderr)
        return 2
    if a.summary:
        brief = summary(report)
        text = (json.dumps({"schema": "holdings_summary_v1", "as_of": report["as_of"], "summary": brief, "flags": dict(flags(report)),
                            "execution_eligible": False, "submitted": False}, indent=2, ensure_ascii=False)
                if a.format == "json" else brief)
    else:
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
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
