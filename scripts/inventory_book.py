"""Reconcile sourced equity inventory lots and non-trade events; never authorize orders."""
import argparse
import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from core import Block, fresh, instant, integer, number, fingerprint
from journal import identity
from portfolio import array
from trader import read

BUCKETS = {'SETTLED', 'T1', 'PLEDGED', 'MTF', 'ENTITLEMENT'}
MOVES = {('T1', 'SETTLED'), ('SETTLED', 'PLEDGED'), ('PLEDGED', 'SETTLED'), ('MTF', 'SETTLED'), ('ENTITLEMENT', 'SETTLED')}
LOT_FIELDS = {'lot_id', 'exchange', 'symbol', 'token', 'isin', 'bucket', 'quantity', 'cost_basis_inr', 'principal_inr'}
COMMON_EVENT = {'event_id', 'broker', 'account_id', 'evidence_id', 'effective_at', 'lot_id', 'expected_lot_hash', 'action'}
EVENT_FIELDS = {
    'transfer': {'quantity', 'destination_bucket', 'new_lot_id', 'principal_repaid_inr'},
    'split': {'numerator', 'denominator', 'symbol', 'token', 'isin'},
    'bonus': {'numerator', 'denominator', 'new_lot_id'},
    'adjustment': {'reason', 'replacement_lots'},
    'buy': {'fill_id', 'new_lot', 'price', 'capitalized_charges_inr'},
    'sell': {'fill_id', 'quantity', 'price', 'charges_inr', 'principal_repaid_inr'},
}


def decimal_text(value):
    # Canonical numeric spelling makes a lot's evidence hash reproducible.
    value = number(value)
    if value == 0: return '0'
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def lot(row):
    if not isinstance(row, dict) or set(row) != LOT_FIELDS:
        raise Block('INVENTORY: exact lot fields required')
    for field in ('lot_id', 'symbol', 'token'):
        if not isinstance(row[field], str) or not row[field].strip():
            raise Block('INVENTORY: lot/instrument identity missing')
    if row['exchange'] not in {'NSE', 'BSE'} or not isinstance(row['isin'], str) or not re.fullmatch(r'IN[A-Z0-9]{10}', row['isin']):
        raise Block('INVENTORY: Indian equity identity required')
    if row['bucket'] not in BUCKETS:
        raise Block('INVENTORY: unsupported inventory bucket')
    result = copy.deepcopy(row)
    result['quantity'] = integer(row['quantity'])
    result['cost_basis_inr'] = decimal_text(row['cost_basis_inr'])
    principal = number(row['principal_inr'])
    if (row['bucket'] == 'MTF') != (principal > 0):
        raise Block('INVENTORY: positive funding principal belongs to MTF lots only')
    result['principal_inr'] = decimal_text(principal)
    return result


def book(rows):
    result = {}
    for row in array(rows):
        normalized = lot(row)
        if normalized['lot_id'] in result:
            raise Block('INVENTORY: duplicate lot ID')
        result[normalized['lot_id']] = normalized
    return result


def evidence_files(spec, root):
    root = Path(root).resolve()
    artifacts = spec['artifacts']
    if not isinstance(artifacts, dict) or not artifacts:
        raise Block('INVENTORY: local source artifacts required')
    hashes = {}
    for key, item in artifacts.items():
        if not isinstance(key, str) or not key or not isinstance(item, dict) or not isinstance(item.get('source'), str) or not item['source'].strip():
            raise Block('INVENTORY: artifact identity/provenance required')
        relative = Path(item['path']); path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            raise Block('INVENTORY: bounded local evidence file required')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item['sha256']:
            raise Block('INVENTORY: evidence hash mismatch')
        hashes[key] = digest
    return hashes


def reconcile(spec, root, now):
    broker, account = identity(spec)
    trades = spec.get('schema') == 'inventory_reconciliation_v2'
    if spec.get('schema') not in ('inventory_reconciliation_v1', 'inventory_reconciliation_v2') or spec.get('reviewed') is not True or spec.get('complete') is not True:
        raise Block('INVENTORY: complete reviewed reconciliation required')
    fresh(spec['as_of'], now, 60, 'inventory statement')
    hashes = evidence_files(spec, root)
    opening, closing = spec['opening'], spec['closing']
    start, end = instant(opening['as_of']), instant(closing['as_of'])
    if not start <= end == instant(spec['as_of']) or (end - start).total_seconds() > 31 * 86400:
        raise Block('INVENTORY: ordered statement window of at most 31 days required')
    if not trades and spec.get('trading_activity') is not False:
        raise Block('INVENTORY: reconcile trade activity separately; never omit fills')
    fills = {}
    if trades:
        ledger = spec.get('trade_ledger', {})
        if ledger.get('complete') is not True or ledger.get('evidence_id') not in hashes or any(ledger.get(k) != spec[k] for k in ('broker', 'account_id')):
            raise Block('INVENTORY: complete sourced account trade ledger required')
        for row in array(ledger['fills']):
            if set(row) != {'fill_id', 'exchange', 'symbol', 'token', 'side', 'quantity', 'price', 'at'}:
                raise Block('INVENTORY: exact observed fill fields required')
            fid = row['fill_id']
            if not isinstance(fid, str) or not fid or fid in fills or row['side'] not in ('BUY', 'SELL'):
                raise Block('INVENTORY: unique trade identity and side required')
            if not start <= instant(row['at']) <= end:
                raise Block('INVENTORY: trade outside statement interval')
            integer(row['quantity']); number(row['price'], minimum='0.00000001')
            fills[fid] = row
        if type(spec.get('trading_activity')) is not bool or spec['trading_activity'] != bool(fills):
            raise Block('INVENTORY: trading activity disagrees with complete ledger')
    for statement in (opening, closing):
        if any(statement.get(k) != spec[k] for k in ('broker', 'account_id')) or statement.get('evidence_id') not in hashes:
            raise Block('INVENTORY: sourced same-account statements required')
    expected, observed = book(opening['lots']), book(closing['lots'])
    seen_lots, seen_events, audit = set(expected), set(), []
    previous = start; seen_fills = set(); sale_cash = Decimal(0)
    for event in array(spec['events']):
        action = event.get('action')
        if action not in EVENT_FIELDS or set(event) != COMMON_EVENT | EVENT_FIELDS[action]:
            raise Block('INVENTORY: exact supported event fields required')
        event_id = event['event_id']
        if not isinstance(event_id, str) or not event_id or event_id in seen_events:
            raise Block('INVENTORY: unique event ID required')
        seen_events.add(event_id)
        if any(event[k] != spec[k] for k in ('broker', 'account_id')) or event['evidence_id'] not in hashes:
            raise Block('INVENTORY: event identity/evidence mismatch')
        at = instant(event['effective_at'])
        if not previous <= at <= end:
            raise Block('INVENTORY: events must be supplied in effective order inside statement window')
        previous = at
        key = event['lot_id']
        buying = action == 'buy'
        if buying and (not trades or key in seen_lots or event['expected_lot_hash'] is not None):
            raise Block('INVENTORY: a buy needs a new lot identity and v2 trade ledger')
        if not buying and (key not in expected or event['expected_lot_hash'] != fingerprint(expected[key])):
            raise Block('INVENTORY: exact pre-event lot hash required')
        before = None if buying else copy.deepcopy(expected[key])

        def add(row):
            value = lot(row)
            if value['lot_id'] in seen_lots:
                raise Block('INVENTORY: replacement/new lot ID was already used')
            seen_lots.add(value['lot_id']); expected[value['lot_id']] = value

        if action in ('buy', 'sell'):
            fid = event['fill_id']; fill = fills.get(fid)
            row = lot(event['new_lot']) if buying else before
            q = row['quantity'] if buying else integer(event['quantity'])
            if (not trades or fid in seen_fills or fill is None or
                    any(fill[k] != row[k] for k in ('exchange', 'symbol', 'token')) or
                    fill['side'] != action.upper() or integer(fill['quantity']) != q or
                    number(fill['price']) != number(event['price']) or instant(fill['at']) != at):
                raise Block('INVENTORY: event must exactly match one unconsumed observed fill')
            seen_fills.add(fid)
            if buying:
                if row['lot_id'] != key or row['bucket'] not in ('T1', 'MTF') or number(row['cost_basis_inr']) != q * number(fill['price']) + number(event['capitalized_charges_inr']):
                    raise Block('INVENTORY: sourced new T1/MTF purchase cost required')
                add(row)
            else:
                if q > before['quantity']:
                    raise Block('INVENTORY: sale exceeds identified lot')
                principal = number(before['principal_inr']) * q / before['quantity']
                if number(event['principal_repaid_inr']) != principal:
                    raise Block('INVENTORY: sale funding repayment differs from allocated principal')
                sale_cash += q * number(fill['price']) - number(event['charges_inr']) - principal
                if q == before['quantity']: del expected[key]
                else:
                    expected[key] = lot({**before, 'quantity': before['quantity'] - q,
                        'cost_basis_inr': decimal_text(number(before['cost_basis_inr']) * (before['quantity'] - q) / before['quantity']),
                        'principal_inr': decimal_text(number(before['principal_inr']) - principal)})
        elif action == 'transfer':
            q = integer(event['quantity']); destination = event['destination_bucket']
            if (before['bucket'], destination) not in MOVES or q > before['quantity']:
                raise Block('INVENTORY: invalid bucket transfer or excess quantity')
            cost = number(before['cost_basis_inr']) * q / before['quantity']
            principal = number(before['principal_inr']) * q / before['quantity']
            if number(event['principal_repaid_inr']) != principal:
                raise Block('INVENTORY: funded transfer requires evidenced principal repayment')
            add({**before, 'lot_id': event['new_lot_id'], 'quantity': q, 'bucket': destination,
                 'cost_basis_inr': str(cost), 'principal_inr': '0'})
            if q == before['quantity']:
                del expected[key]
            else:
                expected[key] = lot({**before, 'quantity': before['quantity'] - q,
                                     'cost_basis_inr': str(number(before['cost_basis_inr']) - cost),
                                     'principal_inr': str(number(before['principal_inr']) - principal)})
        elif action == 'split':
            numerator, denominator = integer(event['numerator']), integer(event['denominator'])
            product = before['quantity'] * numerator
            if product % denominator:
                raise Block('INVENTORY: fractional entitlement needs an explicit sourced adjustment')
            expected[key] = lot({**before, 'quantity': product // denominator,
                                 **{k: event[k] for k in ('symbol', 'token', 'isin')}})
        elif action == 'bonus':
            if before['bucket'] == 'MTF':
                raise Block('INVENTORY: funded bonus allocation needs an explicit sourced adjustment')
            numerator, denominator = integer(event['numerator']), integer(event['denominator'])
            product = before['quantity'] * numerator
            if product % denominator:
                raise Block('INVENTORY: fractional bonus requires sourced adjustment')
            add({**before, 'lot_id': event['new_lot_id'], 'quantity': product // denominator,
                 'bucket': 'ENTITLEMENT', 'cost_basis_inr': '0', 'principal_inr': '0'})
        elif action == 'adjustment':
            if not isinstance(event['reason'], str) or not event['reason'].strip():
                raise Block('INVENTORY: explicit correction reason required')
            # Exact statement correction, not an inferred corporate-action formula.
            for row in array(event['replacement_lots']): add(row)
            del expected[key]
        audit.append({'event_id': event_id, 'event_hash': fingerprint(event), 'before': before,
                      'after_book_hash': fingerprint(expected), 'evidence_sha256': hashes[event['evidence_id']]})
    if seen_fills != set(fills):
        raise Block('INVENTORY: every observed fill must be consumed exactly once')
    if expected != observed:
        raise Block('INVENTORY: projected lots differ from closing broker/depository statement')
    totals = {bucket: {'cost_basis_inr': Decimal(0), 'principal_inr': Decimal(0), 'lots': 0} for bucket in sorted(BUCKETS)}
    for row in expected.values():
        total = totals[row['bucket']]; total['lots'] += 1
        for field in ('cost_basis_inr', 'principal_inr'): total[field] += number(row[field])
    for total in totals.values():
        for field in ('cost_basis_inr', 'principal_inr'): total[field] = decimal_text(total[field])
    return {'schema': 'inventory_reconciliation_report_v1', 'broker': broker, 'account_id': account,
            'as_of': end.isoformat(), 'status': 'RECONCILED', 'input_hash': fingerprint(spec),
            'lots': sorted(expected.values(), key=lambda r: r['lot_id']), 'buckets': totals,
            'lot_hashes': {key: fingerprint(value) for key, value in sorted(expected.items())},
            'audit': audit, 'evidence_hashes': hashes, 'submitted': False, 'execution_eligible': False,
            'sellable_quantity': None,
            'trade_fills_reconciled': len(seen_fills), 'sale_cash_after_charges_and_principal_inr': str(sale_cash),
            'limitations': ['Local hashes and arithmetic do not authenticate statements or verify operator declarations.',
                            'No automatic settlement-date, broker-field, tax/P&L, funding-interest or sellability inference.',
                            'Corporate-action ratios and entitlement eligibility require reviewed source evidence.']}


def record(db, spec, report):
    """Archive evidence atomically; conflicting event IDs cannot overwrite prior records."""
    if report.get('input_hash') != fingerprint(spec) or report.get('status') != 'RECONCILED' or any(report.get(k) != spec[k] for k in ('broker', 'account_id')):
        raise Block('INVENTORY: report does not belong to this reconciliation input')
    db.execute('CREATE TABLE IF NOT EXISTS inventory_event_evidence (broker TEXT, account TEXT, id TEXT, hash TEXT, body TEXT, PRIMARY KEY(broker,account,id))')
    db.execute('CREATE TABLE IF NOT EXISTS inventory_reports (hash TEXT PRIMARY KEY, body TEXT)')
    with db:
        for event in spec['events']:
            key = (spec['broker'], spec['account_id'], event['event_id']); digest = fingerprint(event)
            previous = db.execute('SELECT hash FROM inventory_event_evidence WHERE broker=? AND account=? AND id=?', key).fetchone()
            if previous and previous[0] != digest:
                raise Block('INVENTORY: conflicting event ID; prior evidence preserved')
            db.execute('INSERT OR IGNORE INTO inventory_event_evidence VALUES (?,?,?,?,?)', (*key, digest, json.dumps(event, allow_nan=False)))
        db.execute('INSERT OR IGNORE INTO inventory_reports VALUES (?,?)', (report['input_hash'], json.dumps(report, allow_nan=False)))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'out', 'db'): p.add_argument('--' + name, required=True)
    p.add_argument('--now', help='Explicit timestamp for replaying saved evidence only')
    a = p.parse_args(argv); db = None
    try:
        source, out = Path(a.input), Path(a.out)
        if out.exists(): raise Block('INVENTORY: choose a new report path')
        spec = read(source); report = reconcile(spec, source.parent, instant(a.now))
        Path(a.db).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(a.db, timeout=10); db.execute('PRAGMA synchronous=FULL')
        record(db, spec, report)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('x', encoding='utf-8') as stream: json.dump(report, stream, indent=2, allow_nan=False)
        print(json.dumps({'status': 'RECONCILED', 'execution_eligible': False})); return 0
    except (Block, ValueError, KeyError, TypeError, OSError, ArithmeticError, sqlite3.Error):
        print(json.dumps({'status': 'BLOCKED', 'reason': 'Inventory identity, evidence, event or statement reconciliation failed', 'submitted': False})); return 2
    finally:
        if db: db.close()


if __name__ == '__main__': raise SystemExit(main())
