"""Opt-in cash MIS entries alongside reconciled long CNC and signed MIS inventory."""
import argparse
import copy
import json
from pathlib import Path
import sqlite3

from core import Block, check_order, fingerprint, fresh, instant, integer, money, number, on_tick, positive, validate_instrument
from execution import ExecutionStore, TerminalApproval, execute, profile_check, review_data, store_path
from journal import Journal, reconcile as reconcile_intraday
from live_broker import LiveBroker, wire_payload
from live_state import TERMINAL, available_funds
from portfolio import FIELDS
from portfolio_exit import collect, reconcile
from trader import read


def risk_ledger(collection, instruments, funds, reservations, profile, card, now):
    snapshot, report = reconcile(collection, instruments, now)
    broker, account = snapshot['broker'], snapshot['account_id']
    fresh(snapshot['as_of'], now, 30, 'mixed inventory')
    if any(funds.get(k) != snapshot[k] for k in ('broker', 'account_id', 'source')):
        raise Block('MIXED_ENTRY: funds account/source mismatch')
    fresh(funds['as_of'], now, 30, 'mixed funds')
    available = max(0, available_funds(funds))
    master = {tuple(i[k] for k in FIELDS[:3]): i for i in instruments}
    current = collection['snapshot']['positions']
    positions = {tuple(row[k] for k in FIELDS): row for row in current}
    values = {}; gross = number(0)
    for row in current:
        key = tuple(row[k] for k in FIELDS)
        if row['product'] not in ('CNC', 'MIS') or master[key[:3]]['kind'] != 'equity' or (row['product'] == 'CNC' and row['quantity'] < 0):
            raise Block('MIXED_ENTRY: cash equity long CNC and signed MIS only')
        if row['quantity']:
            fresh(row['mark_as_of'], now, 30, 'portfolio mark')
            value = integer(abs(row['quantity'])) * positive(row['mark_price']); gross += value
            name = row['exchange'] + ':' + row['symbol']
            values[name] = values.get(name, number(0)) + value
    carry_reserved = {}
    for order in snapshot['orders']:
        key = tuple(order[k] for k in FIELDS)
        if master[key[:3]]['kind'] != 'equity' or order['product'] not in ('CNC', 'MIS'):
            raise Block('MIXED_ENTRY: unmapped derivative/product risk')
        if order['status'] in TERMINAL: continue
        remaining = integer(order['quantity']) - integer(order['filled_quantity'], minimum=0)
        if order['product'] == 'CNC':
            if order['side'] != 'SELL': raise Block('MIXED_ENTRY: working carry entry needs separate risk mapping')
            carry_reserved[key] = carry_reserved.get(key, 0) + remaining
            if carry_reserved[key] > positions.get(key, {}).get('quantity', 0):
                raise Block('MIXED_ENTRY: carry exits can overdraw current inventory')
        else:
            # Count all outstanding MIS units conservatively, on either side.
            # A pending cover/exit cannot release exposure before it fills.
            value = remaining * positive(order['price']); gross += value
            name = order['exchange'] + ':' + order['symbol']
            values[name] = values.get(name, number(0)) + value
    intraday_fills = [row for row in collection['fills']['fills'] if row['product'] == 'MIS']
    carry_fills = [row for row in collection['fills']['fills'] if row['product'] != 'MIS']
    if any(row.get('fees_final') is not True for row in carry_fills):
        raise Block('MIXED_ENTRY: carry fill charges need final reconciliation before further entries')
    canonical = {**snapshot, 'complete': True, 'opening_positions': [],
                 'cash_available': available, 'margin_available': available,
                 'orders': [row for row in snapshot['orders'] if row['product'] == 'MIS'],
                 'positions': [{**row, 'position_value_inr': float(abs(row['quantity']) * positive(row['mark_price'])) if row['quantity'] else 0}
                               for row in current if row['product'] == 'MIS']}
    journal = Journal(':memory:')
    try:
        journal.import_fills({**collection['fills'], 'fills': intraday_fills}, now)
        account_ledger = reconcile_intraday(journal, canonical, reservations, now, 30,
                                           fee_policy=profile.get('fee_policy'), instruments=instruments, rate_card=card)
    finally: journal.close()
    if account_ledger['reconciled'] is not True:
        raise Block('MIXED_ENTRY: every intraday fill/order/risk reservation must reconcile')
    # The full portfolio state/economic inputs are bound below. This intraday-only
    # hash includes refresh timestamps, which must not invalidate unchanged approval.
    account_ledger.pop('snapshot_hash', None)
    carry_loss = sum((max(number(0), -number(row['daily_gross_inr'], minimum='-1e30'))
                      for row in report['positions'] if row['product'] == 'CNC'), number(0))
    carry_fees = sum((sum((number(v) for v in row['fees'].values()), number(0)) for row in carry_fills), number(0))
    account_ledger.update(source='mixed_portfolio+' + broker, execution_state_hash=snapshot['state_hash'],
                          orders_today=len(snapshot['orders']),
                          trades_today=account_ledger['trades_today'] + len({r['order_id'] for r in carry_fills}),
                          daily_risk_used=money(number(account_ledger['daily_risk_used']) + carry_loss + carry_fees),
                          position_values={k: money(v) for k, v in values.items()})
    return account_ledger, snapshot, gross


def prepare(bundle, collection, instruments, funds, reservations, profile, now):
    bundle = copy.deepcopy(bundle)
    i, c, ctx, card = [bundle[k] for k in ('instrument', 'config', 'context', 'charges')]
    broker, account = c['broker'], c['account_id']
    profile_check(profile, broker, account, now, entry=True)
    acceptance = profile.get('mixed_entry_acceptance', {})
    if (profile.get('mixed_inventory_entries') is not True or acceptance.get('broker') != broker or
            acceptance.get('account_id') != account or acceptance.get('status') != 'PASS' or not acceptance.get('source')):
        raise Block('MIXED_ENTRY: separate opt-in and account-specific mixed-entry acceptance required')
    fresh(acceptance.get('as_of'), now, 86400, 'mixed entry acceptance')
    validate_instrument(i, now)
    order = bundle['order']; plan = order['plan']
    if i not in instruments or i['broker'] != broker or i['kind'] != 'equity' or plan['side'] not in ('BUY', 'SELL') or plan['product'] != 'MIS':
        raise Block('MIXED_ENTRY: exact selected master and intraday cash-equity plan required')
    sources = [card.get('source', ''), collection['opening'].get('source', '')]
    sources += [ctx[name].get('source', '') for name in ('session', 'restrictions', 'event', 'quote')]
    sources += [row.get(k, '') for row in collection['opening']['positions'] for k in ('cost_source', 'mark_source')]
    if account == 'PAPER-DEMO' or any(any(word in str(source).lower() for word in ('synthetic', 'fictional', 'demo')) for source in sources):
        raise Block('MIXED_ENTRY: demo evidence cannot authorize an entry')
    from session_policy import validate
    validate(ctx.get('instrument_session'), i, c, now)
    ledger, snapshot, gross = risk_ledger(collection, instruments, funds, reservations, profile, card, now)
    if (snapshot['broker'], snapshot['account_id']) != (broker, account): raise Block('MIXED_ENTRY: account mismatch')
    ctx['account'] = ledger
    quote = ctx.get('quotes', {}).get(i['exchange'] + ':' + i['symbol']) or ctx['quote']
    if quote.get('source') != broker or any(str(quote.get(k)) != str(i[k]) for k in ('exchange', 'symbol', 'token')):
        raise Block('MIXED_ENTRY: exact broker quote required')
    fresh(quote['as_of'], now, 30, 'mixed entry quote')
    risk = check_order(order, i, c, ctx, card, now)
    action = {'kind': 'entry', 'broker': broker, 'account_id': account, 'instrument': copy.deepcopy(i), 'product': 'MIS',
              'side': plan['side'], 'quantity': integer(order['quantity']), 'price': float(positive(plan['entry'])),
              'planned_stop': plan['stop'], 'planned_target': plan['target'], 'flat_by': plan['flat_by'],
              'order_hash': fingerprint(order), 'planned_loss_inr': risk['metrics']['loss_at_stop_inr']}
    if action['quantity'] > integer(i['freeze_quantity']) or not on_tick(action['price'], i['tick_size']):
        raise Block('MIXED_ENTRY: freeze/tick limit')
    notional = action['quantity'] * positive(action['price'])
    if notional > positive(profile['max_entry_notional_inr']) or number(action['planned_loss_inr']) > number(profile['max_entry_loss_inr']):
        raise Block('MIXED_ENTRY: per-entry profile cap exceeded')
    if gross + notional > positive(profile['max_portfolio_gross_inr']):
        raise Block('MIXED_ENTRY: complete gross exposure plus outstanding buys exceeds profile cap')
    margin = bundle['live_margin']; wire = wire_payload(action)
    fresh(margin['as_of'], now, 30, 'mixed entry margin')
    if margin.get('source') != broker or margin.get('account_id') != account or margin.get('payload_hash') != fingerprint(wire):
        raise Block('MIXED_ENTRY: exact broker margin payload/account mismatch')
    if positive(margin['required_inr']) + number(risk['metrics']['charges_at_stop']['total']) > number(ledger['margin_available']):
        raise Block('MIXED_ENTRY: insufficient current broker availability')
    review = {'action': action, 'payload': wire, 'state_hash': snapshot['state_hash'],
              'baseline_order_ids': [r['order_id'] for r in snapshot['orders']], 'profile_hash': fingerprint(profile),
              'bundle_hash': fingerprint(review_data({'bundle': bundle, 'opening': collection['opening'], 'fills': collection['fills'],
                                                      'positions': [{k:v for k,v in r.items() if k != 'mark_as_of'} for r in collection['snapshot']['positions']],
                                                      'reservations': reservations, 'available_inr': available_funds(funds)}))}
    return {**review, 'review_hash': fingerprint(review), 'as_of': now.isoformat(), 'risk': risk,
            'portfolio_gross_after_inr': money(gross + notional), 'account_risk': ledger,
            'warning': 'Human-operated MIS entry alongside carry inventory. Gross exposure is not a maximum-loss guarantee. No automatic protection is installed.'}, snapshot


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('command', choices=('preview', 'entry'))
    for name in ('workdir', 'opening', 'master'): p.add_argument('--' + name, required=True)
    a = p.parse_args(argv); store = None
    try:
        from broker import adapter
        from broker_data import normalize_quote, quote_key
        from live import load_bundle
        w = Path(a.workdir); initial = read(w / 'config.json'); client = adapter(initial['broker'])
        store = ExecutionStore(store_path(w,initial))
        def refresh():
            bundle = load_bundle(w); config = bundle['config']; selected = bundle['instrument']
            if any(config[k] != initial[k] for k in ('broker', 'account_id')): raise Block('MIXED_ENTRY: selected account changed')
            profile = read(w / 'live-profile.json'); instruments = read(a.master)['instruments']; opening = read(a.opening)
            first = collect(client, config['broker'], config['account_id'], opening, instruments)
            before, _ = reconcile(first, instruments, instant())
            funds = client.account()
            quote = normalize_quote(client.quotes([quote_key(selected)]), selected, instant(), 30)
            bundle['context']['quote'] = quote
            bundle['context'].setdefault('quotes', {})[selected['exchange'] + ':' + selected['symbol']] = quote
            plan = bundle['order']['plan']
            payload = wire_payload({'kind': 'entry', 'broker': config['broker'], 'account_id': config['account_id'],
                                    'instrument': selected, 'product': 'MIS', 'side': plan['side'],
                                    'quantity': bundle['order']['quantity'], 'price': plan['entry']})
            bundle['live_margin'] = client.margin(payload)
            second = collect(client, config['broker'], config['account_id'], opening, instruments)
            after, _ = reconcile(second, instruments, instant())
            if before['state_hash'] != after['state_hash']: raise Block('MIXED_ENTRY: account changed around funds/margin reads')
            store.observe(after, instant())
            reservation_path = w / 'reservations.json'
            reservations = read(reservation_path) if reservation_path.exists() else store.reservations(after, intraday_partition=True)
            return prepare(bundle, second, instruments, funds, reservations, profile, instant())
        if a.command == 'preview': result, _ = refresh(); result['submitted'] = False
        else: result = execute(store, LiveBroker(initial['broker'], client), TerminalApproval(), refresh)
        print(json.dumps(result, indent=2, allow_nan=False)); return 0
    except (Block, ValueError, KeyError, TypeError, OSError, sqlite3.Error, KeyboardInterrupt):
        print(json.dumps({'status': 'BLOCK_OR_UNKNOWN', 'reason': 'Mixed entry validation failed; inspect shared execution state before any retry'})); return 2
    finally:
        if store: store.close()


if __name__ == '__main__': raise SystemExit(main())
