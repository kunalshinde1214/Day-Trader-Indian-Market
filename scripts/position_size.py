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

from core import Block

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CARD = ROOT / "assets" / "charges.example.json"
# Delivery (CNC) equity charges. STT 0.1% on buy and sell and stamp duty 0.015% on buy are statutory;
# zero brokerage and the DP charge per sell follow the same discount-broker schedule as the example card.
DELIVERY = {"brokerage_rate": 0.0, "stt_buy": 0.001, "stt_sell": 0.001, "stamp_buy": 0.00015, "dp_per_sell": 15.34}
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
        dp = DELIVERY["dp_per_sell"] * 1.18 if qty else 0.0
    gst = (brokerage + exchange + sebi) * card["gst_rate"]
    total = brokerage + stt + exchange + sebi + stamp + gst + dp
    return {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi, "stamp": stamp, "gst": gst, "dp": dp, "total": total}


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
            "risk_budget": risk, "risk_per_share": round(per_share, 2), "quantity": qty, "lot_size": lot, "exposure": round(exposure, 2),
            "capital_needed": round(exposure * (margin if product == "MIS" else 1.0), 2), "capital_limited": capital_limited,
            "loss_at_stop": round(loss, 2), "charges_at_stop": round(at_stop["total"], 2), "gain_at_target": round(gain, 2),
            "charges_at_target": round(at_target["total"], 2), "breakeven_price": round(breakeven, 2),
            "reward_to_risk": round(gain / loss, 2) if loss else None, "charge_breakdown_at_stop": {k: round(v, 2) for k, v in at_stop.items()},
            "rate_card": {"source": card.get("source"), "verified_on": card.get("verified_on"), "status": card.get("status")},
            "notes": notes, "execution_eligible": False, "submitted": False,
            "disclaimer": "Paper sizing from an example rate card; not a recommendation. A stop can fill worse than planned in a gap or fast market."}


def markdown(r):
    unit = "shares" if r["lot_size"] == 1 else f"units ({r['quantity'] // r['lot_size']} lots of {r['lot_size']})"
    lines = [f"# Position size — {r['side']} {r['product']} ({'intraday' if r['product'] == 'MIS' else 'delivery'})", "",
             f"**{r['quantity']:,} {unit}** at ₹{r['entry']:,.2f}, stop ₹{r['stop']:,.2f}, target ₹{r['target']:,.2f}", "",
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


def main(argv=None):
    p = argparse.ArgumentParser(description="How many shares for a given risk, entry and stop, with Indian charges (paper sizing only).")
    p.add_argument("--entry", type=float, required=True)
    p.add_argument("--stop", type=float, required=True)
    p.add_argument("--risk", type=float, required=True, help="Rupees you accept losing if the stop is hit, charges included")
    p.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    p.add_argument("--product", choices=["MIS", "CNC"], default="MIS", help="MIS intraday (default) or CNC delivery")
    p.add_argument("--lot", type=int, default=1, help="Lot size for F&O; 1 for cash equity")
    p.add_argument("--capital", type=float, help="Cash available; caps the quantity")
    p.add_argument("--margin", type=float, default=INTRADAY_MARGIN, help="Intraday margin as a fraction of exposure (default 0.20)")
    p.add_argument("--target", type=float, help="Target price (default: 2R)")
    p.add_argument("--r-multiple", type=float, default=2.0)
    p.add_argument("--charges", default=str(DEFAULT_CARD), help="Rate card JSON (default: the example card)")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    a = p.parse_args(argv)
    try:
        r = size(entry=a.entry, stop=a.stop, risk=a.risk, side=a.side, product=a.product, lot=a.lot, capital=a.capital, margin=a.margin,
                 target=a.target, r_multiple=a.r_multiple, card_path=a.charges)
    except (Block, OSError, ValueError, KeyError) as error:
        print(f"BLOCKED - {error}", file=sys.stderr)
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(r, indent=2, ensure_ascii=False) if a.format == "json" else markdown(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
