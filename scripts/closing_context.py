"""Public NSE closing reports and separately dated flow/GIFT observations."""
import argparse
import csv
from datetime import date, datetime
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.request

from core import Block, IST, instant, number, positive, fingerprint
from data import MAX_BYTES, NoRedirect

FLOWS = 'https://www.nseindia.in/api/fiidiiTradeReact'
STATUS = 'https://www.nseindia.in/api/marketStatus'
INDEX_BASE = 'https://nsearchives.nseindia.in/content/indices/ind_close_all_'
CASH_BASE = 'https://nsearchives.nseindia.in/products/content/sec_bhavdata_full_'
NAMES = {'nifty': 'NIFTY 50', 'banknifty': 'NIFTY BANK'}


def report_url(base, session):
    return base + session.strftime('%d%m%Y') + '.csv'


def fetch(url):
    allowed = url in (FLOWS, STATUS) or any(re.fullmatch(re.escape(base) + r'\d{8}\.csv', url) for base in (INDEX_BASE, CASH_BASE))
    if not allowed: raise Block('CLOSING: unsupported official endpoint')
    req = urllib.request.Request(url, headers={'User-Agent': 'IndiaDayTrader personal research', 'Accept': 'text/csv,application/json'})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response: body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES: raise Block('CLOSING: oversized report')
    return body


def text(body):
    if not isinstance(body, bytes) or not body or len(body) > MAX_BYTES: raise Block('CLOSING: empty/oversized response')
    return body.decode('utf-8-sig')


def csv_rows(body, required):
    reader = csv.DictReader(io.StringIO(text(body)), skipinitialspace=True, strict=True)
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)) or not required <= set(reader.fieldnames):
        raise Block('CLOSING: missing/duplicate source columns')
    rows = []
    for row in reader:
        if None in row or any(v is None for v in row.values()): raise Block('CLOSING: ragged source row')
        rows.append({k: v.strip() for k, v in row.items()})
        if len(rows) > 30000: raise Block('CLOSING: excessive source rows')
    if not rows: raise Block('CLOSING: no source observations')
    return rows


def provenance(body, url, now):
    return {'source': url, 'sha256': hashlib.sha256(body).hexdigest(), 'retrieved_at': now.isoformat(),
            'published_at': None, 'publication_time_verified': False, 'execution_eligible': False}


def indices(body, session, now):
    columns = {'Index Name', 'Index Date', 'Open Index Value', 'High Index Value', 'Low Index Value', 'Closing Index Value', 'Points Change', 'Change(%)'}
    found = {}
    for row in csv_rows(body, columns):
        if datetime.strptime(row['Index Date'], '%d-%m-%Y').date() != session: raise Block('CLOSING: index report date mismatch')
        name = row['Index Name'].upper()
        if name not in NAMES.values(): continue
        if name in found: raise Block('CLOSING: ambiguous index row')
        values = {key: positive(row[column]) for key, column in [('open', 'Open Index Value'), ('high', 'High Index Value'), ('low', 'Low Index Value'), ('close', 'Closing Index Value')]}
        if not values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
            raise Block('CLOSING: contradictory index OHLC')
        change = number(row['Points Change'], minimum='-1e30'); previous = positive(values['close'] - change)
        percent = number(row['Change(%)'], minimum='-1e30')
        if abs(change / previous * 100 - percent) > number('0.02'): raise Block('CLOSING: index change arithmetic mismatch')
        key = next(k for k, v in NAMES.items() if v == name)
        found[name] = {'index_id': key, 'name': row['Index Name'], 'session': session.isoformat(),
                       **{k: float(v) for k, v in values.items()}, 'points_change': float(change), 'change_pct': float(percent),
                       'previous_close_implied_by_source_change': float(previous), 'price_basis': 'published_spot_index_close'}
    if not found: raise Block('CLOSING: supported index observations absent')
    return {**provenance(body, report_url(INDEX_BASE, session), now), 'indices': {r['index_id']: r for r in found.values()}}


def cash_prices(body, session, now):
    rows = csv_rows(body, {'SYMBOL', 'SERIES', 'DATE1', 'PREV_CLOSE', 'CLOSE_PRICE'})
    prices = {}; seen = set(); rejected = []; excluded = 0
    for row in rows:
        if datetime.strptime(row['DATE1'], '%d-%b-%Y').date() != session: raise Block('CLOSING: cash report date mismatch')
        key = (row['SYMBOL'], row['SERIES'])
        if key in seen: raise Block('CLOSING: duplicate cash instrument/series')
        seen.add(key)
        if row['SERIES'] != 'EQ': excluded += 1; continue
        if not re.fullmatch(r'[A-Z0-9&_.-]{1,40}', row['SYMBOL']): raise Block('CLOSING: invalid security symbol')
        try:
            close, previous = positive(row['CLOSE_PRICE']), positive(row['PREV_CLOSE'])
        except Block:
            rejected.append(row['SYMBOL']); continue
        change = close - previous
        prices[row['SYMBOL']] = {'symbol': row['SYMBOL'], 'close': float(close), 'previous_close': float(previous),
                                 'change_pct': float(change / previous * 100), 'direction': 1 if change > 0 else -1 if change < 0 else 0}
    return {**provenance(body, report_url(CASH_BASE, session), now), 'prices': prices,
            'source_rows': len(rows), 'excluded_non_eq_rows': excluded, 'unavailable_eq_symbols': rejected}


def breadth(prices, members=None):
    selected = list(prices) if members is None else members
    if not isinstance(selected, list) or len(selected) != len(set(selected)) or any(not isinstance(s, str) for s in selected):
        raise Block('CLOSING: unique membership symbols required')
    rows = [prices[s] for s in selected if s in prices]
    ordered = sorted(rows, key=lambda r: (r['change_pct'], r['symbol']))
    return {'observed': len(rows), 'expected': len(selected), 'missing': sorted(set(selected) - prices.keys()),
            'advancing': sum(r['direction'] > 0 for r in rows), 'declining': sum(r['direction'] < 0 for r in rows),
            'unchanged': sum(r['direction'] == 0 for r in rows), 'strongest_observed': list(reversed(ordered[-5:])),
            'weakest_observed': ordered[:5], 'index_point_contributions': False}


def flows(body, now):
    data = json.loads(text(body))
    if not isinstance(data, list) or len(data) != 2: raise Block('FLOWS: unique FII/FPI and DII rows required')
    values = {}; dates = set()
    for row in data:
        category = row['category']; session = datetime.strptime(row['date'], '%d-%b-%Y').date()
        if category not in ('FII/FPI', 'DII') or category in values or session > now.date(): raise Block('FLOWS: category/date mismatch')
        buy, sell = number(row['buyValue']), number(row['sellValue']); net = number(row['netValue'], minimum='-1e30')
        if abs(buy - sell - net) > number('0.01'): raise Block('FLOWS: source net value mismatch')
        values[category] = {'buy_crore': float(buy), 'sell_crore': float(sell), 'net_crore': float(net), 'net_inr': str(net * 10000000)}
        dates.add(session)
    if len(dates) != 1: raise Block('FLOWS: categories have different trading dates')
    session = dates.pop()
    return {**provenance(body, FLOWS, now), 'trading_date': session.isoformat(), 'age_calendar_days': (now.date() - session).days,
            'values': values, 'finality_verified': False, 'point_in_time_backtest_eligible': False,
            'scope': 'Values from the cited NSE endpoint; exchange aggregation and final release time not inferred'}


def gift(body, now):
    row = json.loads(text(body))['giftnifty']
    if row.get('SYMBOL') != 'NIFTY' or row.get('INSTRUMENTTYPE') != 'FUTIDX' or row.get('OPTIONTYPE') != '-' or number(row['STRIKEPRICE']) != 0:
        raise Block('GIFT: exact NIFTY futures observation required')
    at = datetime.strptime(row['TIMESTMP'], '%d-%b-%Y %H:%M').replace(tzinfo=IST)
    expiry = datetime.strptime(row['EXPIRYDATE'], '%d-%b-%Y').date()
    age = (now - at).total_seconds()
    if age < 0: raise Block('GIFT: future source observation')
    return {**provenance(body, STATUS, now), 'value': float(positive(row['LASTPRICE'])),
            'change_points': float(number(row['DAYCHANGE'], minimum='-1e30')), 'change_pct': float(number(row['PERCHANGE'], minimum='-1e30')),
            'as_of': at.isoformat(), 'source_timestamp_raw': row['TIMESTMP'], 'timezone_basis': 'IST assumed; source omits offset',
            'timezone_verified': False, 'age_seconds': age, 'freshness': 'RECENT_UNVERIFIED_TIMEZONE' if age <= 180 else 'STALE',
            'expiry': expiry.isoformat(), 'expired': expiry < now.date(),
            'price_basis': 'GIFT Nifty futures; not domestic spot-index points or an executable broker contract'}


def collect(session, *, selected=None, membership=None, fetcher=fetch, clock=instant):
    now = clock(); day = date.fromisoformat(session); selected = ['nifty', 'sensex', 'banknifty'] if selected is None else selected
    if day.isoformat() != session or day > now.date() or not selected or len(selected) != len(set(selected)) or any(k not in ('nifty', 'sensex', 'banknifty') for k in selected):
        raise Block('CLOSING: exact nonfuture session and supported indices required')
    result = {'schema': 'india_closing_context_v1', 'session': session, 'as_of': now.isoformat(), 'indices': {}, 'coverage': []}
    for name, url, parser in (
        ('index_report', report_url(INDEX_BASE, day), lambda raw: indices(raw, day, now)),
        ('cash_report', report_url(CASH_BASE, day), lambda raw: cash_prices(raw, day, now)),
        ('institutional_flows', FLOWS, lambda raw: flows(raw, now)), ('gift', STATUS, lambda raw: gift(raw, now)),
    ):
        try:
            result[name] = parser(fetcher(url)); result['coverage'].append({'component': name, 'status': 'COLLECTED'})
        except (Block, ValueError, KeyError, TypeError, OSError, csv.Error):
            result['coverage'].append({'component': name, 'status': 'FAILED', 'reason': 'Unavailable or unsupported source response'})
    cash = result.get('cash_report')
    if cash is not None:
        prices = cash.pop('prices')
        result['nse_eq_breadth'] = {**breadth(prices), 'unavailable_eq_symbols': cash['unavailable_eq_symbols'],
                                   'scope': 'Valid EQ rows in the cited daily report; not complete Indian-market or index membership'}
    for key in selected:
        row = result.get('index_report', {}).get('indices', {}).get(key)
        if row is None:
            result['indices'][key] = {'status': 'UNAVAILABLE', 'reason': 'No matching index in this NSE closing source'}; continue
        result['indices'][key] = {**row, 'status': 'OBSERVED', 'constituent_breadth': None}
        if membership is not None and key in membership and cash is not None:
            try:
                item = membership[key]
                if item.get('schema') != 'reviewed_index_membership_v1' or item.get('index_id') != key or item.get('membership_date') != session or item.get('membership_verified') is not False:
                    raise Block('CLOSING: generated exact-date reviewed membership required')
                if item.get('membership_hash') != fingerprint({k: v for k, v in item.items() if k != 'membership_hash'}) or instant(item['as_of']) > now:
                    raise Block('CLOSING: changed/future membership output')
                result['indices'][key]['constituent_breadth'] = {**breadth(prices, item['members']), 'membership_verified': False,
                                                               'membership_basis': item['membership_basis'], 'membership_hash': item['membership_hash']}
            except (Block, ValueError, KeyError, TypeError):
                result['coverage'].append({'component': key + '_breadth', 'status': 'FAILED', 'reason': 'Invalid membership evidence'})
    if 'institutional_flows' in result:
        result['institutional_flows']['matches_requested_session'] = result['institutional_flows']['trading_date'] == session
    result.update(status='PARTIAL', full_market_coverage=False, execution_eligible=False, submitted=False,
                  missing_coverage=['BSE/Sensex closing provider', 'Independent membership and publication/finality verification',
                                    'Complete macro-event calendar', 'Current tradable quotes and instrument-session clearance'])
    result['context_hash'] = fingerprint(result)
    return result


def markdown(result):
    lines = ['# Indian closing context', '', 'Requested report session: ' + result['session'],
             'Retrieved: ' + result['as_of'], '', 'Published observations with separate dates; research only.', '',
             '| Index | Published close | Change | Status |', '| --- | ---: | ---: | --- |']
    for key, row in result['indices'].items():
        change = str(row['change_pct'])+'%' if 'change_pct' in row else 'unavailable'
        lines.append(f"| {key} | {row.get('close', 'unavailable')} | {change} | {row['status']} |")
    if 'nse_eq_breadth' in result:
        b = result['nse_eq_breadth']; lines += ['', f"Observed NSE EQ breadth: {b['advancing']} advancing, {b['declining']} declining, {b['unchanged']} unchanged.", b['scope'] + '.']
    if 'institutional_flows' in result:
        f = result['institutional_flows']; lines += ['', 'FII/DII observation date: ' + f['trading_date'] + '; release time and finality unverified.']
        lines += [f"- {k}: net ₹{v['net_crore']:,.2f} crore." for k, v in f['values'].items()]
        if not f['matches_requested_session']: lines.append('These flows belong to a different date from the requested closing report.')
    if 'gift' in result:
        g = result['gift']; lines += ['', f"GIFT Nifty futures: {g['value']:,.2f}; {g['freshness']}; source time {g['as_of']}; expiry {g['expiry']}.", g['timezone_basis'] + '.']
    lines += ['', 'Sources:']
    for key in ('index_report', 'cash_report', 'institutional_flows', 'gift'):
        if key in result: lines.append(f"- [{key}]({result[key]['source']})")
    lines += ['', 'Coverage:'] + [f"- {r['component']}: {r['status']}" for r in result['coverage']]
    lines += ['', 'Remaining coverage:'] + ['- ' + gap for gap in result['missing_coverage']]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--session', required=True)
    parser.add_argument('--indices', nargs='+', choices=['nifty', 'sensex', 'banknifty']); parser.add_argument('--membership')
    parser.add_argument('--format', choices=['json', 'markdown'], default='markdown'); parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        from trader import read
        out = Path(args.out)
        if out.exists(): raise Block('OUTPUT: choose a new file')
        result = collect(args.session, selected=args.indices, membership=read(args.membership) if args.membership else None)
        rendered = json.dumps(result, indent=2, allow_nan=False) if args.format == 'json' else markdown(result)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('x', encoding='utf-8') as stream: stream.write(rendered)
        print(json.dumps({'status': result['status'], 'submitted': False}))
        return 0 if any(i['status'] == 'OBSERVED' for i in result['indices'].values()) else 2
    except (Block, ValueError, KeyError, TypeError, OSError):
        print('BLOCKED: invalid closing context request'); return 2


if __name__ == '__main__': raise SystemExit(main())
