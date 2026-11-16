"""Quantity-dependent replay fees and explicit, uncalibrated cost sensitivity."""
from copy import deepcopy
from core import Block, number, positive, integer, charges, fingerprint, money


def policy(config, instrument):
    quantities = config.get('replay_cost_quantities')
    stress = config.get('replay_cost_stress')
    if quantities is None and stress is None:
        return None
    lot = integer(instrument['lot_size'])
    quantities = [lot] if quantities is None else quantities
    if not isinstance(quantities, list) or not 1 <= len(quantities) <= 10:
        raise Block('REPLAY_COSTS: provide 1..10 distinct quantities')
    ceiling = min(1000000, integer(instrument.get('freeze_quantity', 1000000)))
    if any(type(q) is not int or q <= 0 or q > ceiling or q % lot for q in quantities):
        raise Block('REPLAY_COSTS: quantities must be positive whole lots within the single-order bound')
    if len(set(quantities)) != len(quantities):
        raise Block('REPLAY_COSTS: duplicate quantities')
    if stress is not None:
        if not isinstance(stress, dict) or set(stress) != {'mode', 'latency_bps', 'impact_bps_at_reference', 'reference_quantity'} or stress['mode'] != 'linear_impact_v1':
            raise Block('REPLAY_COSTS: explicit linear impact sensitivity fields required')
        latency = number(stress['latency_bps'])
        impact = number(stress['impact_bps_at_reference'])
        reference = integer(stress['reference_quantity'])
        if reference % lot or any(latency + impact * q / reference >= 10000 for q in quantities):
            raise Block('REPLAY_COSTS: reference must be whole lots and adverse per-leg shift below 10000 bps')
    return deepcopy({'quantities': quantities, 'stress': stress})


def summarize(plan, instrument, card, selected, exits):
    """exits maps each synthetic terminal price to its path count, including open paths."""
    count = sum(exits.values())
    if count <= 0: raise Block('REPLAY_COSTS: terminal observations required')
    entry = positive(plan['entry'])
    day = plan['as_of'][:10]
    long = plan['side'] == 'BUY'
    scenarios = []
    for quantity in selected['quantities']:
        model = selected['stress']
        bps = (number(model['latency_bps']) + number(model['impact_bps_at_reference']) * quantity /
               integer(model['reference_quantity'])) if model else number(0)
        fraction = bps / 10000
        shifted_entry = entry * (1 + fraction if long else 1 - fraction)
        fees = base_fees = drag = gross = number(0)
        for price, weight in exits.items():
            price = positive(price)
            shifted_exit = price * (1 - fraction if long else 1 + fraction)
            base_fees += number(charges(entry, price, quantity, plan['side'], instrument, card, day)['total']) * weight
            fees += number(charges(shifted_entry, shifted_exit, quantity, plan['side'], instrument, card, day)['total']) * weight
            drag += (entry + price) * fraction * quantity * weight
            gross += (price - entry if long else entry - price) * quantity * weight
        scenarios.append({'quantity': quantity, 'adverse_shift_bps_per_leg': float(bps),
            'average_base_fees_inr': money(base_fees / count),
            'average_stressed_fees_inr': money(fees / count),
            'average_adverse_price_cost_inr': money(drag / count),
            'average_gross_mark_to_exit_inr': money(gross / count),
            'average_net_mark_to_exit_inr': money((gross - fees - drag) / count)})
    return {'policy': selected, 'policy_hash': fingerprint(selected), 'rate_card_hash': fingerprint(card),
            'scenarios': scenarios, 'execution_eligible': False,
            'basis': 'Same terminal prices at each requested quantity; fees recomputed, including brokerage caps. '
                     'Open paths are marked at their terminal price with hypothetical exit fees, not realised trades.',
            'limitations': ['Linear impact and latency basis points are user assumptions, not fitted market estimates.',
                            'Cost sensitivity does not change target/stop frequencies or simulate queues, fills or order splitting.',
                            'One entry and one exit per quantity; current supplied tariff, not historical contract-note reconstruction.']}
