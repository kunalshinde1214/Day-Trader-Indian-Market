"""Simple position size for an Indian equity or F&O paper trade: shares from risk, stop and charges.

No broker connection or evidence files needed. Answers "how many shares can I buy risking ₹2,000 with a
stop at X?" with charges included at the stop, the exposure, the capital or margin it needs, and the result
at a target. Charges are estimates from a dated rate card; margin for intraday positions depends on the
broker and the stock. Research only: no orders.
"""
import argparse
from datetime import date
import json
import math
from pathlib import Path
import sys

from core import Block, instant

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CARD = ROOT / "assets" / "charges.example.json"
# Delivery (CNC) equity charges. STT 0.1% on buy and sell and stamp duty 0.015% on buy are statutory;
# zero brokerage and the DP charge per sell follow the same discount-broker schedule as the example card.
DELIVERY = {"brokerage_rate": 0.0, "stt_buy": 0.001, "stt_sell": 0.001, "stamp_buy": 0.00015, "dp_per_sell": 15.34}  # per scrip per sell day, GST included (Zerodha schedule)
LATER_CLOSE_STEPS = (0.0025, 0.005)  # a confirming close this far beyond the trigger, for the "if the close is higher" sizes
INTRADAY_MARGIN = 0.20  # typical MIS margin (5x) for liquid stocks; the broker and stock decide the real figure


def load_card(path):
    card = json.loads(Path(path).read_text(encoding="utf-8"))
    seg = card.get("segments", {}).get("NSE:equity")
    if not seg:
        raise Block("SIZE: rate card has no NSE:equity entry")
    return card, seg


def charges(entry, exit_price, qty, side, product, card, seg):
    """Round-trip charges in rupees for qty shares (entry and exit at the given prices)."""
    buy, sell = (entry * qty, exit_price * qty) if side == "BUY" else (exit_price * qty, entry * qty)
    turnover = buy + sell
    exchange = turnover * seg["exchange_rate"]
    sebi = turnover * card["sebi_rate"]
    if product == "MIS":
        rate, cap = card["brokerage_rate"], card["brokerage_cap"]
        brokerage = min(buy * rate, cap) + min(sell * rate, cap)
        stt = sell * seg["stt_sell"]
        stamp = buy * seg["stamp_buy"]
        dp = 0.0
    else:
        brokerage = (buy + sell) * DELIVERY["brokerage_rate"]
        stt = buy * DELIVERY["stt_buy"] + sell * DELIVERY["stt_sell"]
        stamp = buy * DELIVERY["stamp_buy"]
        dp = DELIVERY["dp_per_sell"] if qty else 0.0  # the published DP charge already includes GST
    gst = (brokerage + exchange + sebi) * card["gst_rate"]
    total = brokerage + stt + exchange + sebi + stamp + gst + dp
    return {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi, "stamp": stamp, "gst": gst, "dp": dp, "total": total}


TICK = 0.05
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def last_price(symbol, *, fetcher=None, clock=instant):
    """The latest price for an NSE symbol through holdings_review's Yahoo daily-history reader (the same one the portfolio
    review prices holdings with): the last daily close, or the live price while the session is open. Returns
    {'symbol', 'price', 'date', 'kind': 'close'|'live', 'time', 'label'}; raises Block when no price can be read."""
    import holdings_review as hr
    from public_market import request
    name, yahoo = hr.stock(symbol)
    try:
        q = hr.latest_price(yahoo, fetcher=fetcher or request, clock=clock, years=1)
    except (Block, ValueError, KeyError, TypeError, IndexError, OSError) as error:
        raise Block(f"SIZE: no price for {name} ({str(error).replace('PUBLIC: ', '')}); give --entry PRICE instead") from None
    day = date.fromisoformat(q["price_date"])
    when = f"{WEEKDAYS[day.weekday()]} {day.day} {day:%b %Y}"
    label = (f"{name} live price at {q['price_time'] or 'n/a'} IST, {when} (Yahoo, may be delayed)" if q["price_kind"] == "live"
             else f"{name} last close, {when} (Yahoo daily bar)")
    return {"symbol": name, "price": float(q["price"]), "date": q["price_date"], "kind": q["price_kind"], "time": q["price_time"],
            "label": label, "bars": q.get("bars") or []}


ATR_SESSIONS = 14
WICK_ATR = 1.0  # a low this many ATRs below the lower of that day's open and close (above the higher, for a high) is a wick


def day_label(d):
    d = date.fromisoformat(str(d)[:10])
    return f"{WEEKDAYS[d.weekday()]} {d.day} {d:%b %Y}"


def atr(bars, n=ATR_SESSIONS):
    """Average true range over the last `n` daily bars (simple mean), or None with too few bars."""
    if len(bars) < n + 1:
        return None
    tr = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])) for p, c in zip(bars[-n - 1:-1], bars[-n:])]
    return sum(tr) / n


def session_extreme(bars, n, side, *, live=False):
    """The stop at the `n`-session low (BUY) or high (SELL) of completed daily bars ({'date', 'open', 'high', 'low', 'close'};
    today's still-forming bar is left out while the session is open), with what can make it a poor stop:

    - wick: the extreme bar's low sits at least WICK_ATR ATRs below the lower of that day's open and close (above the higher
      for a high) — one bar's spike, not where the stock has traded since; the alternative is the extreme of the window without that bar;
    - drops: the extreme is the oldest bar in the window, so next session the n-session level moves to the extreme of the
      newest n−1 bars (unless a new extreme prints).

    Returns {'stop', 'date', 'sessions', 'window': (first, last), 'atr', 'wick', 'wick_atr', 'drops', 'alternatives': [...]}."""
    done = [b for b in bars if b.get("low") is not None and b.get("high") is not None and b.get("close") is not None]
    if live and done:
        done = done[:-1]
    if not 2 <= n <= 250:
        raise Block("SIZE: --stop-low/--stop-high take a session count between 2 and 250, e.g. 20")
    if len(done) < n:
        raise Block(f"SIZE: only {len(done)} completed daily bars; cannot find a {n}-session {'low' if side == 'BUY' else 'high'}")
    window = done[-n:]
    key = "low" if side == "BUY" else "high"
    pick = (min if side == "BUY" else max)(enumerate(window), key=lambda x: (x[1][key], -x[0]) if side == "BUY" else (x[1][key], x[0]))[1]
    extreme = lambda rows: (min if side == "BUY" else max)(rows, key=lambda b: b[key])
    a = atr(done)
    body = None
    if pick.get("open") is not None:
        body = min(pick["open"], pick["close"]) if side == "BUY" else max(pick["open"], pick["close"])
    spike = abs(body - pick[key]) if body is not None else None
    wick = bool(a and spike is not None and spike >= WICK_ATR * a)
    drops = pick is window[0]
    alternatives = []
    if wick:
        rest = extreme([b for b in window if b is not pick])
        alternatives.append({"why": "wick", "stop": round(rest[key], 2), "date": str(rest["date"])})
    if drops:
        nxt = extreme(window[1:])
        alternatives.append({"why": "drops", "stop": round(nxt[key], 2), "date": str(nxt["date"])})
    return {"stop": round(pick[key], 2), "date": str(pick["date"]), "sessions": n, "side": side, "kind": key,
            "window": (str(window[0]["date"]), str(window[-1]["date"])), "atr": a, "wick": wick,
            "wick_atr": spike / a if a and spike is not None else None, "open": pick.get("open"), "close": pick["close"],
            "drops": drops, "alternatives": alternatives}


def stop_label(x):
    """'the 20-session low, ₹1,038.05 on Fri 23 Oct 2026 (Yahoo daily bars, Fri 23 Oct–Fri 20 Nov)'."""
    lo, hi = x["window"]
    return (f"the {x['sessions']}-session {x['kind']}, set on {day_label(x['date'])} (Yahoo daily bars "
            f"{day_label(lo)[:-5]}–{day_label(hi)[:-5]})")


def stop_warnings(x, alt_sizes):
    """⚠ lines for a wick or a level that leaves the window next session, each with the alternative stop and its sizes
    (`alt_sizes`: {stop: 'CNC 23 shares · MIS 24 shares'})."""
    out, word = [], x["kind"]
    alts = x["alternatives"]
    if len(alts) == 2 and alts[0]["stop"] == alts[1]["stop"]:
        alt, sizes = alts[0], alt_sizes.get(alts[0]["stop"])
        return [f"⚠ The {word} ₹{x['stop']:,.2f} is a one-bar wick: {x['wick_atr']:.1f} ATR "
                f"{'below the lower' if word == 'low' else 'above the higher'} of that day's open ₹{x['open']:,.2f} and close "
                f"₹{x['close']:,.2f} (14-session ATR ₹{x['atr']:,.2f}) — a single intraday spike, not where the stock has traded since — and {day_label(x['date'])} is the oldest "
                f"bar in the window, so it drops out next session. Without it the {x['sessions']}-session {word} is ₹{alt['stop']:,.2f} "
                f"({day_label(alt['date'])})" + (f"; at that stop: {sizes}." if sizes else ".")]
    for alt in alts:
        sizes = alt_sizes.get(alt["stop"])
        tail = f"; at that stop: {sizes}" if sizes else ""
        if alt["why"] == "wick":
            out.append(f"⚠ The {word} ₹{x['stop']:,.2f} is a one-bar wick: {x['wick_atr']:.1f} ATR "
                       f"{'below the lower' if word == 'low' else 'above the higher'} of that day's open ₹{x['open']:,.2f} and close "
                       f"₹{x['close']:,.2f} (14-session ATR ₹{x['atr']:,.2f}) — a single intraday spike, not where the stock has traded since. Without that bar the "
                       f"{x['sessions']}-session {word} is ₹{alt['stop']:,.2f} ({day_label(alt['date'])}){tail}.")
        else:
            out.append(f"⚠ The {word} drops out of the {x['sessions']}-session window next session ({day_label(x['date'])} is the oldest "
                       f"bar): from then the {x['sessions']}-session {word} is ₹{alt['stop']:,.2f} ({day_label(alt['date'])}) unless a new "
                       f"{word} prints{tail}.")
    return out


def stop_from_pct(entry, pct, side):
    """Stop `pct` percent below the entry for a buy (above it for a sell), on NSE's ₹0.05 tick."""
    if not 0 < pct < 50:
        raise Block("SIZE: --stop-pct must be between 0 and 50, e.g. 2 for a stop 2% from the entry")
    raw = entry * (1 - pct / 100) if side == "BUY" else entry * (1 + pct / 100)
    return round(round(raw / TICK) * TICK, 2)


def size(*, entry, stop, risk, side="BUY", product="MIS", lot=1, capital=None, margin=INTRADAY_MARGIN, target=None, r_multiple=2.0, card_path=DEFAULT_CARD):
    if entry <= 0 or stop <= 0 or risk <= 0:
        raise Block("SIZE: entry, stop and risk must be positive")
    if side not in ("BUY", "SELL") or product not in ("MIS", "CNC"):
        raise Block("SIZE: side BUY/SELL and product MIS (intraday) or CNC (delivery)")
    if product == "CNC" and side == "SELL":
        raise Block("SIZE: a delivery (CNC) short sale is not possible in Indian cash equity; use MIS for an intraday short")
    if (side == "BUY" and stop >= entry) or (side == "SELL" and stop <= entry):
        raise Block("SIZE: the stop must be below the entry for a buy and above it for a sell")
    if lot < 1:
        raise Block("SIZE: lot size must be at least 1")
    card, seg = load_card(card_path)
    per_share = abs(entry - stop)
    qty = int(risk // per_share) // lot * lot
    notes = []
    while qty > 0 and qty * per_share + charges(entry, stop, qty, side, product, card, seg)["total"] > risk:
        qty -= lot
    capital_limited = False
    if capital:
        need_per_share = entry * (margin if product == "MIS" else 1.0)
        affordable = int(capital // need_per_share) // lot * lot
        if affordable < qty:
            qty, capital_limited = affordable, True
            notes.append(f"Capped by capital: ₹{capital:,.0f} covers {affordable:,} shares at "
                         f"{'an assumed ' + format(margin * 100, '.0f') + '% intraday margin' if product == 'MIS' else 'full delivery value'}.")
    if qty <= 0:
        raise Block(f"SIZE: ₹{risk:,.0f} risk is not enough for even {lot} share(s) with a ₹{per_share:,.2f} stop after charges")
    target_given = bool(target)
    target = target or (entry + r_multiple * per_share if side == "BUY" else entry - r_multiple * per_share)
    at_stop = charges(entry, stop, qty, side, product, card, seg)
    at_target = charges(entry, target, qty, side, product, card, seg)
    direction = 1 if side == "BUY" else -1
    loss = qty * per_share + at_stop["total"]
    gain = qty * (target - entry) * direction - at_target["total"]
    exposure = qty * entry
    breakeven = entry + direction * at_target["total"] / qty
    if at_stop["total"] > 0.1 * risk:
        notes.append(f"Charges are {at_stop['total'] / risk * 100:.0f}% of the risk budget; a wider stop or larger budget makes them matter less.")
    if product == "MIS":
        notes.append(f"Intraday margin assumed at {margin * 100:.0f}% of exposure (₹{exposure * margin:,.0f}); the real figure comes from your broker and differs by stock.")
    return {"schema": "position_size_v1", "side": side, "product": product, "entry": entry, "stop": stop, "target": round(target, 2),
            "target_basis": "given" if target_given else f"{r_multiple:g}R default (risk per share × {r_multiple:g})",
            "risk_budget": risk, "risk_per_share": round(per_share, 2), "quantity": qty, "lot_size": lot, "exposure": round(exposure, 2),
            "capital_needed": round(exposure * (margin if product == "MIS" else 1.0), 2), "capital_limited": capital_limited,
            "loss_at_stop": round(loss, 2), "charges_at_stop": round(at_stop["total"], 2), "gain_at_target": round(gain, 2),
            "charges_at_target": round(at_target["total"], 2), "breakeven_price": round(breakeven, 2),
            "reward_to_risk": round(gain / loss, 2) if loss else None, "charge_breakdown_at_stop": {k: round(v, 2) for k, v in at_stop.items()},
            "rate_card": {"source": card.get("source"), "verified_on": card.get("verified_on"), "status": card.get("status")},
            "notes": notes, "execution_eligible": False, "submitted": False,
            "disclaimer": "Paper sizing from an example rate card; not a recommendation. A stop can fill worse than planned in a gap or fast market."}


def worst_entry(*, qty, stop, risk, side="BUY", product="MIS", card_path=DEFAULT_CARD):
    """The highest entry (lowest for a SELL) at which `qty` shares still lose no more than `risk`, charges included, at `stop`."""
    card, seg = load_card(card_path)
    direction = 1 if side == "BUY" else -1
    loss = lambda e: qty * (e - stop) * direction + charges(e, stop, qty, side, product, card, seg)["total"]
    near, far = stop, stop + direction * risk / qty
    if loss(stop + direction * 0.01) > risk:
        return None
    for _ in range(80):
        mid = (near + far) / 2
        near, far = (mid, far) if loss(mid) <= risk else (near, mid)
    return math.floor(near * 100) / 100 if side == "BUY" else math.ceil(near * 100) / 100


def range_sizes(*, range_high, range_low, risk, side="BUY", entry=None, product="MIS", card_path=DEFAULT_CARD):
    """Size an opening-range breakout from the range itself: stops at the range midpoint and at the far side of the range
    (range low for a BUY, high for a SELL). `entry` defaults to the trigger level (range high for a BUY, low for a SELL), the
    best price a confirming close can have; each size also gives the worst entry that still fits the risk at that share count."""
    if not range_low < range_high:
        raise Block("SIZE: range high must be above range low")
    level = range_high if side == "BUY" else range_low
    entry = entry or level
    out = []
    for name, stop in (("range midpoint", round((range_high + range_low) / 2, 2)),
                       ("range low" if side == "BUY" else "range high", range_low if side == "BUY" else range_high)):
        row = {"stop_rule": name, "stop": stop}
        try:
            r = size(entry=entry, stop=stop, risk=risk, side=side, product=product, card_path=card_path)
            row.update({"quantity": r["quantity"], "entry": entry, "risk_per_share": r["risk_per_share"], "loss_at_stop": r["loss_at_stop"],
                        "charges_at_stop": r["charges_at_stop"], "exposure": r["exposure"], "capital_needed": r["capital_needed"],
                        "target_2r": r["target"], "gain_at_target": r["gain_at_target"],
                        "worst_entry": worst_entry(qty=r["quantity"], stop=stop, risk=risk, side=side, product=product, card_path=card_path)})
            row["risk_added_per_rupee"] = r["quantity"]
        except Block as error:
            row["blocked"] = str(error).replace("SIZE: ", "")
        row["later_closes"] = []
        for step in LATER_CLOSE_STEPS:
            later = round(entry * (1 + step if side == "BUY" else 1 - step), 2)
            try:
                q = size(entry=later, stop=stop, risk=risk, side=side, product=product, card_path=card_path)
                row["later_closes"].append({"entry": later, "move_pct": step * 100, "quantity": q["quantity"], "loss_at_stop": q["loss_at_stop"]})
            except Block:
                row["later_closes"].append({"entry": later, "move_pct": step * 100, "quantity": 0, "loss_at_stop": None})
        out.append(row)
    return {"schema": "range_sizes_v1", "side": side, "product": product, "risk_budget": risk, "entry_assumed": entry,
            "range_high": range_high, "range_low": range_low, "sizes": out,
            "note": ("Entry assumed at the trigger level; the confirming close will be higher" if side == "BUY" else
                     "Entry assumed at the trigger level; the confirming close will be lower") + ", which raises the risk per share — "
                    "above the worst entry shown, the same share count risks more than the budget, so size again with the actual close.",
            "execution_eligible": False, "submitted": False}


def inr(value, digits=0):
    """Rupees with Indian digit grouping: ₹4,03,456."""
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.{digits}f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return f"{sign}₹" + ",".join(groups + [tail]) + (f".{frac}" if frac else "")


def range_markdown(r):
    more = "higher" if r["side"] == "BUY" else "lower"
    steps = r["sizes"][0]["later_closes"] if r["sizes"] else []
    head = " | ".join(f"Close {inr(x['entry'], 2)} ({'+' if r['side'] == 'BUY' else '-'}{x['move_pct']:g}%)" for x in steps)
    lines = [f"Risk {inr(r['risk_budget'])}, {r['product']} {'intraday' if r['product'] == 'MIS' else 'delivery'}; charges included. "
             f"Shares by stop and by the price of the confirming close:", "",
             f"| Stop | Close at the trigger {inr(r['entry_assumed'], 2)} | {head} |", "| --- | ---: |" + " ---: |" * len(steps)]
    for s in r["sizes"]:
        first = f"**{s['quantity']:,}** ({inr(s['risk_per_share'], 2)} a share, loss {inr(s['loss_at_stop'])})" if "quantity" in s else s["blocked"]
        later = " | ".join(f"{x['quantity']:,}" if x["quantity"] else "too far" for x in s["later_closes"])
        lines.append(f"| {s['stop_rule']} {inr(s['stop'], 2)} | {first} | {later} |")
    lines.append("")
    for s in r["sizes"]:
        if "quantity" in s:
            worst = f"{inr(s['worst_entry'], 2)}" if s["worst_entry"] is not None else "n/a"
            lines.append(f"- Stop at the {s['stop_rule']}: {s['quantity']:,} shares (exposure {inr(s['exposure'])}, est. intraday margin "
                         f"{inr(s['capital_needed'])}) fits the budget only up to an entry of {worst}; each ₹1 {more} adds "
                         f"{inr(s['risk_added_per_rupee'])} to the loss at the stop. 2R target {inr(s['target_2r'], 2)}.")
    lines += ["", f"⚠ {r['note']}", ""]
    return "\n".join(lines)


def target_label(r):
    """'target ₹2,436.40' when the user gave it, else 'target ₹2,436.40 (2R default; pass --target)'."""
    basis = r.get("target_basis", "given")
    return f"target ₹{r['target']:,.2f}" + ("" if basis == "given" else f" ({basis.split(' (')[0]}; pass --target)")


def inputs_line(r):
    """'Entry ₹2,342.70 — TCS last close, Thu 19 Nov 2026 (Yahoo daily bar) · stop ₹2,295.85 (2% below entry) · risk ₹5,000
    (1% of ₹5,00,000)' when any input was derived; None when the user typed them all."""
    i = r.get("inputs") or {}
    if not (i.get("price_label") or i.get("stop_pct") or i.get("risk_pct") or i.get("stop_label")):
        return None
    below = "below" if r["side"] == "BUY" else "above"
    return " · ".join([f"Entry ₹{r['entry']:,.2f}" + (f" — {i['price_label']}" if i.get("price_label") else ""),
                       f"stop ₹{r['stop']:,.2f}" + (f" ({i['stop_pct']:g}% {below} entry)" if i.get("stop_pct") else
                                                    f" — {i['stop_label']}" if i.get("stop_label") else ""),
                       f"risk {inr(r['risk_budget'])}" + (f" ({i['risk_pct']:g}% of {inr(i['capital'])})" if i.get("risk_pct") else "")])


def markdown(r):
    unit = "shares" if r["lot_size"] == 1 else f"units ({r['quantity'] // r['lot_size']} lots of {r['lot_size']})"
    head = inputs_line(r)
    lines = [f"# Position size — {r['side']} {r['product']} ({'intraday' if r['product'] == 'MIS' else 'delivery'})", ""]
    lines += [head, ""] if head else []
    warn = (r.get("inputs") or {}).get("stop_warnings") or []
    lines += [f"- {w}" for w in warn] + ([""] if warn else [])
    lines += [f"**{r['quantity']:,} {unit}** at ₹{r['entry']:,.2f}, stop ₹{r['stop']:,.2f}, {target_label(r)}", "",
             "| | ₹ |", "| --- | ---: |",
             f"| Risk budget | {r['risk_budget']:,.0f} |",
             f"| Loss at stop, including charges | {r['loss_at_stop']:,.0f} |",
             f"| Gain at target, after charges | {r['gain_at_target']:,.0f} |",
             f"| Reward ÷ risk | {r['reward_to_risk']} |",
             f"| Exposure (quantity × entry) | {r['exposure']:,.0f} |",
             f"| Capital or margin needed | {r['capital_needed']:,.0f} |",
             f"| Charges if stopped out | {r['charges_at_stop']:,.2f} |",
             f"| Price that covers all charges | {r['breakeven_price']:,.2f} |", ""]
    lines += [f"- {n}" for n in r["notes"]]
    lines += ["", f"Charges: estimate from {r['rate_card']['source']} (card checked {r['rate_card']['verified_on']}); your broker's rates may differ. {r['disclaimer']}", ""]
    return "\n".join(lines)


def both_markdown(b):
    """Delivery and intraday sizes side by side for the same entry, stop and risk."""
    rs, first = b["sizes"], b["sizes"][0]
    title = {"CNC": "Delivery (CNC)", "MIS": "Intraday (MIS)"}
    head = inputs_line(first) or f"Entry ₹{first['entry']:,.2f} · stop ₹{first['stop']:,.2f} · risk {inr(first['risk_budget'])}"
    warn = (first.get("inputs") or {}).get("stop_warnings") or []
    lines = [f"# Position size — {first['side']}{' ' + b['symbol'] if b.get('symbol') else ''}, delivery and intraday", "", head, ""]
    lines += [f"- {w}" for w in warn] + ([""] if warn else [])
    lines += [
             " · ".join(f"**{title[r['product']]}: {r['quantity']:,} shares**" for r in rs), "",
             "| | " + " | ".join(title[r["product"]] for r in rs) + " |", "| --- |" + " ---: |" * len(rs)]

    def row(label, fn):
        lines.append(f"| {label} | " + " | ".join(fn(r) for r in rs) + " |")
    row("Loss at stop, including charges ₹", lambda r: f"{r['loss_at_stop']:,.0f}")
    row("Charges if stopped out ₹", lambda r: f"{r['charges_at_stop']:,.2f}")
    row("Exposure (quantity × entry) ₹", lambda r: f"{r['exposure']:,.0f}")
    row("Capital or margin needed ₹", lambda r: f"{r['capital_needed']:,.0f}")
    row("Price that covers all charges ₹", lambda r: f"{r['breakeven_price']:,.2f}")
    row(f"Gain at the {target_label(first)}, after charges ₹", lambda r: f"{r['gain_at_target']:,.0f}")
    row("Reward ÷ risk", lambda r: f"{r['reward_to_risk']}")
    shared = set.intersection(*(set(r["notes"]) for r in rs))
    word = lambda r: title[r["product"]].split(" (")[0]
    notes = list(dict.fromkeys(n if n in shared or n.startswith(word(r)) else f"{word(r)}: {n}" for r in rs for n in r["notes"]))
    notes.append("Delivery pays STT on both buy and sell plus a DP charge on the sell, so it fits fewer shares in the same risk; an "
                 "intraday (MIS) position is squared off the same day.")
    lines += [""] + [f"- {n}" for n in notes]
    lines += ["", f"Charges: estimate from {first['rate_card']['source']} (card checked {first['rate_card']['verified_on']}); your broker's "
                  f"rates may differ. {first['disclaimer']}", ""]
    return "\n".join(lines)


def main(argv=None, *, fetcher=None, clock=instant):
    p = argparse.ArgumentParser(description="How many shares for a given risk, entry and stop, with Indian charges (paper sizing only).")
    p.add_argument("--entry", type=float, help="Entry price (with --from-entry: overrides the trigger level, e.g. the actual confirming close)")
    p.add_argument("--stop", type=float, help="Stop price (not needed with --from-entry, which uses the opening range)")
    p.add_argument("--symbol", help="NSE symbol (TCS, RELIANCE.NS): the entry defaults to its last close (or the live price in market "
                                    "hours), read from Yahoo and labelled with its date")
    p.add_argument("--stop-pct", type=float, help="Stop this many percent below the entry for a BUY (above for a SELL), e.g. 2")
    p.add_argument("--stop-low", type=int, metavar="N", help="With --symbol, BUY: stop at the N-session low of daily bars (e.g. 20), "
                                                            "dated, with a warning for a one-bar wick or a low that leaves the window next session")
    p.add_argument("--stop-high", type=int, metavar="N", help="With --symbol, SELL: stop at the N-session high of daily bars")
    p.add_argument("--risk", type=float, help="Rupees you accept losing if the stop is hit, charges included")
    p.add_argument("--risk-pct", type=float, help="Risk as a percent of --capital, e.g. 1 (instead of --risk)")
    p.add_argument("--from-entry", metavar="FILE", help="public_market.py entry JSON (--out file): size from its opening range, with stops "
                                                       "at the range midpoint and the far side of the range")
    p.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    p.add_argument("--product", choices=["MIS", "CNC", "BOTH"], default="BOTH",
                   help="MIS intraday, CNC delivery, or BOTH side by side (default; a SELL or an F&O lot is intraday only)")
    p.add_argument("--lot", type=int, default=1, help="Lot size for F&O; 1 for cash equity")
    p.add_argument("--capital", type=float, help="Cash available; caps the quantity")
    p.add_argument("--margin", type=float, default=INTRADAY_MARGIN, help="Intraday margin as a fraction of exposure (default 0.20)")
    p.add_argument("--target", type=float, help="Target price (default: 2R, labelled as the default)")
    p.add_argument("--r-multiple", type=float, default=2.0)
    p.add_argument("--charges", default=str(DEFAULT_CARD), help="Rate card JSON (default: the example card)")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    a = p.parse_args(argv)
    try:
        if a.risk is None and a.risk_pct is None:
            raise Block("SIZE: give --risk RUPEES, or --risk-pct N with --capital RUPEES")
        if a.risk is not None and a.risk_pct is not None:
            raise Block("SIZE: give --risk or --risk-pct, not both")
        if a.risk_pct is not None:
            if not a.capital:
                raise Block("SIZE: --risk-pct needs --capital RUPEES (the account the percent is of)")
            if not 0 < a.risk_pct <= 100:
                raise Block("SIZE: --risk-pct must be between 0 and 100, e.g. 1")
            a.risk = round(a.capital * a.risk_pct / 100, 2)
        if sum(x is not None for x in (a.stop, a.stop_pct, a.stop_low, a.stop_high)) > 1:
            raise Block("SIZE: give one stop rule: --stop or --stop-pct, --stop-low or --stop-high, not several")
        if a.stop_low is not None and a.side != "BUY":
            raise Block("SIZE: --stop-low is for a BUY (stop below the entry); a SELL uses --stop-high N")
        if a.stop_high is not None and a.side != "SELL":
            raise Block("SIZE: --stop-high is for a SELL (stop above the entry); add --side SELL, or use --stop-low N for a BUY")
        if (a.stop_low is not None or a.stop_high is not None) and not a.symbol:
            raise Block("SIZE: --stop-low/--stop-high need --symbol SYMBOL (the daily bars come from its price history)")
        if a.from_entry:
            doc = json.loads(Path(a.from_entry).read_text(encoding="utf-8"))
            rng = doc.get("opening_range") or {}
            if not rng.get("complete"):
                raise Block("SIZE: the entry file has no completed opening range (run public_market.py entry after 09:30 IST)")
            side = doc.get("side", a.side)
            entry = a.entry or (doc.get("entry") if doc.get("state") == "triggered" else None)
            r = range_sizes(range_high=rng["high"], range_low=rng["low"], risk=a.risk, side=side, entry=entry,
                            product="MIS" if a.product == "BOTH" else a.product, card_path=a.charges)
            render = range_markdown
        else:
            quote = None
            sessions = a.stop_low if a.stop_low is not None else a.stop_high
            if (a.entry is None or sessions is not None) and a.symbol:
                quote = last_price(a.symbol, fetcher=fetcher, clock=clock)
                if a.entry is None:
                    a.entry = quote["price"]
                else:
                    quote = dict(quote, label=None)
            extreme = None
            if sessions is not None:
                extreme = session_extreme(quote["bars"], sessions, a.side, live=quote["kind"] == "live")
                a.stop = extreme["stop"]
            if a.entry is None:
                raise Block("SIZE: give --entry PRICE or --symbol SYMBOL, or --from-entry FILE")
            if a.stop is None and a.stop_pct is not None:
                a.stop = stop_from_pct(a.entry, a.stop_pct, a.side)
            if a.stop is None:
                raise Block("SIZE: give --stop PRICE or --stop-pct N")
            products = (["CNC", "MIS"] if a.side == "BUY" and a.lot == 1 else ["MIS"]) if a.product == "BOTH" else [a.product]
            inputs = {"price_label": quote["label"] if quote else None,
                      "price_quote": {k: v for k, v in quote.items() if k != "bars"} if quote else None, "stop_pct": a.stop_pct,
                      "risk_pct": a.risk_pct, "capital": a.capital}
            if extreme:
                alt_sizes = {}
                for alt in extreme["alternatives"]:
                    parts = []
                    for prod in products:
                        try:
                            q = size(entry=a.entry, stop=alt["stop"], risk=a.risk, side=a.side, product=prod, lot=a.lot,
                                     capital=a.capital, margin=a.margin, card_path=a.charges)
                            parts.append(f"{'delivery' if prod == 'CNC' else 'intraday'} {q['quantity']:,} shares")
                        except Block as error:
                            parts.append(f"{'delivery' if prod == 'CNC' else 'intraday'}: {str(error).replace('SIZE: ', '')}")
                    alt_sizes[alt["stop"]] = " · ".join(parts)
                inputs.update(stop_label=stop_label(extreme), stop_rule=extreme, stop_warnings=stop_warnings(extreme, alt_sizes))
            sizes = [dict(size(entry=a.entry, stop=a.stop, risk=a.risk, side=a.side, product=prod, lot=a.lot, capital=a.capital,
                               margin=a.margin, target=a.target, r_multiple=a.r_multiple, card_path=a.charges), inputs=inputs)
                     for prod in products]
            if len(sizes) == 1:
                r, render = sizes[0], markdown
            else:
                r = {"schema": "position_sizes_v1", "symbol": quote["symbol"] if quote else None, "inputs": inputs, "sizes": sizes,
                     "execution_eligible": False, "submitted": False, "disclaimer": sizes[0]["disclaimer"]}
                render = both_markdown
    except (Block, OSError, ValueError, KeyError, TypeError) as error:
        print(f"BLOCKED - {error}", file=sys.stderr)
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(r, indent=2, ensure_ascii=False) if a.format == "json" else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
