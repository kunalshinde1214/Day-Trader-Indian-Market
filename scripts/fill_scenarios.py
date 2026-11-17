"""Cash-equity passive-limit queue, latency and partial-fill sensitivity. Never trades."""
import argparse
import json
from pathlib import Path
from core import Block, charges, fingerprint, instant, integer, number, on_tick, positive, validate_instrument


def simulate(spec, now):
    if spec.get('schema') != 'passive_fill_scenarios_v1': raise Block('FILL_MODEL: explicit schema required')
    i = spec['instrument']; validate_instrument(i, now)
    if i['kind'] != 'equity' or integer(i['lot_size']) != 1:
        raise Block('FILL_MODEL: cash equity in individual units only')
    side = spec['side']; limit = positive(spec['limit_price']); exit_price = positive(spec['hypothetical_exit_price'])
    if side not in ('BUY', 'SELL') or not on_tick(limit, i['tick_size']) or not on_tick(exit_price, i['tick_size']):
        raise Block('FILL_MODEL: side and on-tick prices required')
    q = integer(spec['quantity'])
    if q > integer(i['freeze_quantity']): raise Block('FILL_MODEL: single-order freeze limit exceeded')
    scenarios = spec['scenarios']
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 40:
        raise Block('FILL_MODEL: one to forty explicit scenarios required')
    names = set(); output = []
    for scenario in scenarios:
        if set(scenario) != {'name', 'ahead_units_at_arrival', 'latency_ms', 'cancel_after_ms', 'eligible_flow'}:
            raise Block('FILL_MODEL: exact scenario fields required')
        name = scenario['name']
        if not isinstance(name, str) or not name or name in names: raise Block('FILL_MODEL: unique scenario name required')
        names.add(name)
        queue = integer(scenario['ahead_units_at_arrival'], minimum=0)
        latency = integer(scenario['latency_ms'], minimum=0); cancel = integer(scenario['cancel_after_ms'])
        if not latency < cancel <= 22500000: raise Block('FILL_MODEL: latency must precede cancellation within session length')
        events = scenario['eligible_flow']
        if not isinstance(events, list) or len(events) > 10000: raise Block('FILL_MODEL: bounded explicit eligible volume required')
        previous = -1; filled = 0; slices = []
        for event in events:
            if set(event) != {'elapsed_ms', 'units'}: raise Block('FILL_MODEL: exact flow fields required')
            elapsed = integer(event['elapsed_ms'], minimum=0); units = integer(event['units'], minimum=0)
            if not previous < elapsed <= 22500000: raise Block('FILL_MODEL: distinct ordered session timestamps required')
            previous = elapsed
            if elapsed < latency or elapsed >= cancel or filled == q: continue
            consumed = min(queue, units); queue -= consumed
            used = min(q - filled, units - consumed)
            if used:
                filled += used; slices.append({'elapsed_ms': elapsed, 'units': used, 'price': float(limit)})
        fees = charges(limit, exit_price, filled, side, i, spec['charges'], now.date().isoformat()) if filled else None
        gross = (exit_price - limit) * filled * (1 if side == 'BUY' else -1)
        output.append({'name': name, 'filled_units': filled, 'unfilled_units': q-filled, 'fill_slices': slices,
                       'remaining_queue_ahead_units': queue, 'hypothetical_round_trip_charges': fees,
                       'hypothetical_net_inr': float(gross - number(fees['total'])) if fees else 0})
    return {'schema': 'passive_fill_report_v1', 'input_hash': fingerprint(spec), 'as_of': now.isoformat(),
        'scenarios': output, 'execution_eligible': False, 'submitted': False,
        'limitations': ['FIFO queue and eligible contra-side volume are explicit scenario assumptions, not measured queue position.',
            'Use only volume eligible to trade against this passive limit; OHLCV cannot establish eligible volume.',
            'No cancellation ahead, hidden liquidity, price improvement, market-order sweep or calibrated fill probability.',
            'Partial fills belong to one order; charges use actual simulated filled units and one hypothetical exit. Unfilled units incur no trade fees.',
            'Cancellation is assumed effective at cancel_after_ms; live cancel races and real fills remain unknown.']}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', required=True); p.add_argument('--out', required=True); p.add_argument('--now')
    a = p.parse_args(argv)
    try:
        from trader import read
        out = Path(a.out)
        if out.exists(): raise Block('OUTPUT: choose a new file')
        report = simulate(read(a.input), instant(a.now)); out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('x', encoding='utf-8') as f: json.dump(report, f, indent=2, allow_nan=False)
        print(json.dumps({'status': 'SAVED', 'execution_eligible': False})); return 0
    except (Block, ValueError, KeyError, TypeError, OSError, ArithmeticError):
        print('BLOCKED: invalid cash instrument, scenario, rate card or output'); return 2


if __name__ == '__main__': raise SystemExit(main())
