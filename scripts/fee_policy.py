"""Explicit conservative intraday fee reservations; never final contract-note fees."""
import copy
from core import Block, charges, fingerprint, fresh, number, money


def reserve_fees(fills, policy, instruments, card, broker, account, now):
    if (policy.get("mode") != "per_fill_roundtrip_reserve" or policy.get("broker") != broker or
            policy.get("account_id") != account or not policy.get("source")):
        raise Block("FEE_POLICY: sourced account-specific reserve policy required")
    fresh(policy.get("as_of"), now, 86400, "fee reserve policy")
    multiplier = number(policy["multiplier"], minimum=1)
    if multiplier > 10: raise Block("FEE_POLICY: multiplier outside supported bounds")
    buffer = number(policy["per_fill_buffer_inr"])
    result = copy.deepcopy(fills)
    count = 0
    for fill in result:
        if fill["fees_final"]: continue
        matching = [i for i in instruments if i["broker"] == broker and all(i[k] == fill[k] for k in ("exchange", "symbol", "token"))]
        if len(matching) != 1: raise Block("FEE_POLICY: exact instrument for every provisional fill required")
        # Reserve an entire round trip for EACH fill, including brokerage caps each
        # time. This intentionally over-reserves split executions under this card.
        estimated = charges(fill["price"], fill["price"], fill["quantity"], fill["side"], matching[0], card, now.date().isoformat())
        amount = number(estimated["total"]) * multiplier + buffer
        existing = sum((number(v) for v in fill["fees"].values()), number(0))
        fill["fees"] = {"provisional_reserve": money(max(amount, existing))}
        count += 1
    return result, {"provisional_fill_count": count, "fee_policy_hash": fingerprint(policy), "rate_card_hash": fingerprint(card),
                    "fee_basis": "conservative estimated reserve; actual fees may differ; contract-note reconciliation remains required"}
