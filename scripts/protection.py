"""Stateful protection decisions. Produces reviews, never submits broker orders."""
import copy
from datetime import time
import json
from pathlib import Path
import sqlite3

from core import Block, fingerprint, fresh, integer, number, positive, on_tick, validate_instrument, instant
from execution import prepare, UNRESOLVED
from live_state import consistent, state_hash, TERMINAL
from session_policy import validate as validate_session


def decide(bundle, snapshot, profile, policy, now, state=None, intents=()):
    """Evaluate one fresh observation; caller persists next_state before returning.

    A target/stop/cutoff latch survives cancellation and later price reversals.
    Actions are previews for the existing human-operated execution workflow.
    """
    i, config = bundle['instrument'], bundle['config']
    validate_instrument(i, now)
    identity = ('broker', 'exchange', 'symbol', 'token')
    if (policy.get('schema') != 'protection_policy_v1' or not isinstance(policy.get('policy_id'), str) or not policy['policy_id'].strip()
            or any(policy.get(k) != i[k] for k in identity)
            or policy.get('account_id') != config['account_id'] or policy.get('product') != 'MIS'
            or policy.get('date') != now.date().isoformat() or policy.get('entry_side') not in ('BUY', 'SELL')):
        raise Block('PROTECTION: exact dated intraday policy required')
    if config['broker'] != i['broker'] or any(snapshot.get(k) != config[k] for k in ('broker', 'account_id')):
        raise Block('PROTECTION: snapshot account mismatch')
    if snapshot.get('source') != config['broker'] or snapshot.get('date') != policy['date'] or snapshot.get('state_hash') != state_hash(snapshot):
        raise Block('PROTECTION: invalid snapshot source/date/hash')
    fresh(snapshot['as_of'], now, 30, 'protection snapshot')
    if any(r['product'] != 'MIS' for r in snapshot['positions'] + snapshot['orders']):
        raise Block('PROTECTION: carry inventory requires a separate controller')
    consistent(snapshot)
    if any(r['state'] in UNRESOLVED for r in intents):
        raise Block('PROTECTION: unresolved intent; reconcile without retry')
    cap = integer(policy['max_quantity'])
    if cap % integer(i['lot_size']) or cap > integer(i['freeze_quantity']):
        raise Block('PROTECTION: policy quantity exceeds lot/freeze limits')
    prices = {k: positive(policy[k]) for k in ('stop_trigger', 'stop_limit', 'target', 'exit_limit')}
    if any(not on_tick(v, i['tick_size']) for v in prices.values()):
        raise Block('PROTECTION: prices must be tick aligned')
    long = policy['entry_side'] == 'BUY'
    if not (prices['stop_limit'] <= prices['stop_trigger'] < prices['target'] if long else
            prices['stop_limit'] >= prices['stop_trigger'] > prices['target']):
        raise Block('PROTECTION: invalid stop/target direction')
    if policy['flat_by'] != config['flat_by']:
        raise Block('PROTECTION: policy/config cutoff differs')
    cutoff = time.fromisoformat(policy['flat_by'])
    if cutoff.tzinfo is not None:
        raise Block('PROTECTION: flat_by must be local IST clock time')
    quote = bundle['context']['quote']
    if quote.get('source') != i['broker'] or any(str(quote.get(k)) != str(i[k]) for k in ('exchange', 'symbol', 'token')):
        raise Block('PROTECTION: exact broker quote required')
    fresh(quote['as_of'], now, 30, 'protection quote')
    price = positive(quote['price'])
    policy_hash = fingerprint(policy)
    state = copy.deepcopy(state) if state is not None else {
        'schema': 'protection_state_v1', 'policy_hash': policy_hash, 'phase': 'WATCH', 'reason': None}
    if (state.get('schema') != 'protection_state_v1' or state.get('policy_hash') != policy_hash
            or state.get('phase') not in ('WATCH', 'EXITING', 'COMPLETE')
            or state.get('reason') not in (None, 'FLAT_BY', 'STOP_REACHED', 'TARGET_REACHED')
            or state.get('phase') == 'EXITING' and state.get('reason') is None):
        raise Block('PROTECTION: changed policy or invalid saved state; start a new policy ID')
    if state.get('as_of') and instant(state['as_of']) > now:
        raise Block('PROTECTION: observation predates saved state')
    matching = lambda r: all(r[k] == i[k] for k in ('exchange', 'symbol', 'token'))
    positions = [p for p in snapshot['positions'] if matching(p)]
    if len(positions) > 1:
        raise Block('PROTECTION: duplicate position')
    qty = positions[0]['quantity'] if positions else 0
    if abs(qty) > cap or qty and (qty > 0) != long:
        raise Block('PROTECTION: position exceeds mandate or reversed direction')
    working = [r for r in snapshot['orders'] if matching(r) and r['status'] not in TERMINAL]
    state['as_of'] = now.isoformat()
    review = None
    args = None
    reason = state['reason']
    stop_hit = price <= prices['stop_trigger'] if long else price >= prices['stop_trigger']
    target_hit = price >= prices['target'] if long else price <= prices['target']
    if state['phase'] == 'COMPLETE' and (qty or working):
        raise Block('PROTECTION: exposure reappeared after completion; new policy required')
    if state['phase'] == 'WATCH':
        if now.time() >= cutoff: reason = 'FLAT_BY'
        elif stop_hit: reason = 'STOP_REACHED'
        elif target_hit: reason = 'TARGET_REACHED'
        if reason:
            state.update(phase='EXITING', reason=reason)
    exit_side = 'SELL' if long else 'BUY'
    desired_kind = 'exit' if state['phase'] == 'EXITING' else 'stop'
    desired_price = prices['exit_limit'] if desired_kind == 'exit' else prices['stop_limit']
    exact = (len(working) == 1 and qty and working[0]['side'] == exit_side
             and working[0]['quantity'] - working[0]['filled_quantity'] == abs(qty)
             and number(working[0]['price']) == desired_price
             and working[0]['order_type'] == ('LIMIT' if desired_kind == 'exit' else 'SL')
             and (desired_kind == 'exit' or (working[0]['raw_status'] == 'TRIGGER PENDING'
                  and number(working[0]['trigger_price']) == prices['stop_trigger'])))
    if exact:
        status = 'WAIT_EXIT_FILL' if desired_kind == 'exit' else 'STOP_WAITING'
    elif working:
        # Cancel exposure-increasing orders first. Recompute quantities after terminal cancellation.
        row = sorted(working, key=lambda r: (r['side'] == exit_side, r['order_id']))[0]
        args = {'kind': 'cancel', 'order_id': row['order_id']}
        status = 'CANCEL_THEN_REFRESH'
    elif not qty:
        state.update(phase='COMPLETE')
        status = 'FLAT_OBSERVED'
    else:
        validate_session(bundle['context'].get('instrument_session'), i, config, now)
        args = {'kind': desired_kind, 'quantity': abs(qty), 'price': float(desired_price)}
        if desired_kind == 'stop': args['trigger'] = float(prices['stop_trigger'])
        status = 'REVIEW_EXIT' if desired_kind == 'exit' else 'REVIEW_STOP'
    if args:
        review = prepare(args['kind'], bundle, snapshot, profile, now,
                         **{k: v for k, v in args.items() if k != 'kind'})
        review['protection_policy_hash'] = policy_hash
        review['protection_phase'] = state['phase']
        review['review_hash'] = fingerprint({k: v for k, v in review.items() if k not in ('review_hash', 'as_of')})
    return {'schema': 'protection_decision_v1', 'status': status, 'reason': state['reason'],
            'next_state': state, 'review': review, 'next_action': args,
            'submitted': False, 'unattended_execution': False,
            'warning': 'Preview only. Confirm each action through the human broker tool, then refresh. A limit order may not fill.'}


def preview(database, bundle, snapshot, profile, policy, now, intents=()):
    """Persist the exit latch transactionally; never import a broker transport."""
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=0)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS protection_states (policy_id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        saved = db.execute('SELECT body FROM protection_states WHERE policy_id=?', (policy['policy_id'],)).fetchone()
        result = decide(bundle, snapshot, profile, policy, now, json.loads(saved[0]) if saved else None, intents)
        db.execute('INSERT OR REPLACE INTO protection_states VALUES (?,?)',
                   (policy['policy_id'], json.dumps(result['next_state'], allow_nan=False)))
        db.commit()
        return result
    finally:
        db.close()
