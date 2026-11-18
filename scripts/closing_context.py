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
            result['indices'][key] = {'status': 'UNAVAILABLE', 'reason': SENSEX_REASON if key == 'sensex' else 'No matching index in this NSE closing source'}; continue
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


LABELS = {'nifty': 'Nifty 50', 'sensex': 'Sensex', 'banknifty': 'Bank Nifty'}
SENSEX_ROUTE = 'python scripts/daily.py sensex --collect-public'
SENSEX_REASON = ('Sensex is a BSE index and is not in the NSE closing report used here. '
                 'For Sensex use `' + SENSEX_ROUTE + '` (Yahoo public data; not an official BSE feed).')


def sensex_notice(session, clock=instant):
    """Plain explanation for a Sensex-only closing request; no NSE request is made because it cannot cover Sensex."""
    day = date.fromisoformat(session)
    if day.isoformat() != session: raise Block('CLOSING: exact YYYY-MM-DD session required')
    return {'schema': 'india_closing_context_v1', 'session': session, 'as_of': clock().isoformat(), 'status': 'UNAVAILABLE',
            'indices': {'sensex': {'status': 'UNAVAILABLE', 'reason': SENSEX_REASON}}, 'coverage': [],
            'alternative_command': SENSEX_ROUTE, 'missing_coverage': ['BSE/Sensex closing provider'],
            'full_market_coverage': False, 'execution_eligible': False, 'submitted': False}


def _day(value):
    d = date.fromisoformat(str(value)[:10]); return f"{d.day} {d:%b}"


def _long_day(value):
    d = date.fromisoformat(str(value)[:10]); return f"{d:%a} {d.day} {d:%b} {d.year}"


def _signed(value, suffix=''):
    return f"{value:+,.2f}{suffix}"


def _index_line(key, row):
    name = LABELS.get(key, key)
    if row['status'] != 'OBSERVED':
        return name + ': ' + (SENSEX_REASON if key == 'sensex' else row.get('reason', 'unavailable'))
    return (f"{name} ({_day(row['session'])}): {row['close']:,.2f}, {_signed(row['points_change'])} ({_signed(row['change_pct'], '%')}); "
            f"range {row['low']:,.2f}-{row['high']:,.2f}.")


def _gift_text(g):
    freshness = 'STALE, older than 3 minutes' if g['freshness'] == 'STALE' else 'recent'
    return (f"GIFT Nifty futures ({_day(g['as_of'])} {g['as_of'][11:16]} IST, {freshness}; expiry {g['expiry']}): {g['value']:,.2f}, "
            f"{_signed(g['change_points'])} ({_signed(g['change_pct'], '%')}). Not the domestic spot index.")


def _flow_text(f):
    values = '; '.join(f"{k} net ₹{v['net_crore']:,.2f} crore" for k, v in f['values'].items())
    text = f"FII/DII ({_day(f['trading_date'])}): {values}."
    if not f['matches_requested_session']: text += ' These flows belong to a different date from the requested closing report.'
    return text


def markdown(result, brief=False):
    if 'alternative_command' in result:
        return '\n'.join([f"# Sensex close - {_long_day(result['session'])}", '', SENSEX_REASON, '',
                          'The NSE closing route covers Nifty 50 and Bank Nifty, plus NSE breadth, FII/DII and GIFT Nifty context: '
                          f"`python scripts/daily.py india-daily --collect-close --session {result['session']}`."]) + '\n'
    flows, gift = result.get('institutional_flows'), result.get('gift')
    if brief:
        lines = [f"**Market close - {_long_day(result['session'])}** (NSE published data; research only)"]
        lines += ['- ' + _index_line(k, r) for k, r in result['indices'].items()]
        if 'nse_eq_breadth' in result:
            b = result['nse_eq_breadth']; lines.append(f"- NSE shares (all EQ): {b['advancing']:,} up, {b['declining']:,} down, {b['unchanged']:,} unchanged.")
        if flows: lines.append('- ' + _flow_text(flows))
        if gift: lines.append('- ' + _gift_text(gift))
        lines.append('- Notes: release time and finality unverified. Run without --brief for sources and coverage.')
        return '\n'.join(lines) + '\n'
    lines = [f"# Market close - {_long_day(result['session'])}", '',
             f"Session {result['session']}; retrieved {_day(result['as_of'])} {result['as_of'][11:16]} IST. "
             'Published NSE observations, each with its own date; research only.', '',
             '| Index | Close | Change | Change % | Day range |', '| --- | ---: | ---: | ---: | --- |']
    for key, row in result['indices'].items():
        if row['status'] == 'OBSERVED':
            lines.append(f"| {LABELS.get(key, key)} | {row['close']:,.2f} | {_signed(row['points_change'])} | {_signed(row['change_pct'], '%')} | "
                         f"{row['low']:,.2f}-{row['high']:,.2f} |")
        else:
            lines.append(f"| {LABELS.get(key, key)} | {'not in NSE report' if key == 'sensex' else 'not collected'} | | | |")
    if 'sensex' in result['indices'] and result['indices']['sensex']['status'] != 'OBSERVED': lines += ['', SENSEX_REASON]
    if 'nse_eq_breadth' in result:
        b = result['nse_eq_breadth']
        lines += ['', f"NSE breadth (all EQ shares in the {_day(result['session'])} bhavcopy): {b['advancing']:,} advancing, "
                      f"{b['declining']:,} declining, {b['unchanged']:,} unchanged. Not index membership."]
    for key, row in result['indices'].items():
        cb = row.get('constituent_breadth')
        if cb: lines.append(f"{LABELS.get(key, key)} members: {cb['advancing']} advancing, {cb['declining']} declining, {cb['unchanged']} unchanged "
                            f"({cb['observed']}/{cb['expected']} observed; membership unverified).")
    if flows: lines += ['', _flow_text(flows)]
    if gift: lines += ['', _gift_text(gift)]
    lines += ['', '## Data notes', '']
    sources = [f"[{name}]({result[key]['source']})" for key, name in (('index_report', 'NSE index closes'), ('cash_report', 'NSE bhavcopy'),
               ('institutional_flows', 'NSE FII/DII'), ('gift', 'NSE market status')) if key in result]
    if sources: lines.append('- Sources: ' + ', '.join(sources) + '.')
    failed = [r['component'].replace('_', ' ') for r in result['coverage'] if r['status'] != 'COLLECTED']
    if failed: lines.append('- Not collected this run: ' + ', '.join(failed) + '.')
    if flows: lines.append('- FII/DII observation date ' + flows['trading_date'] + '; release time and finality unverified.')
    if gift: lines.append('- GIFT Nifty time: ' + gift['timezone_basis'] + '.')
    lines.append('- Not covered: ' + '; '.join(result['missing_coverage']) + '.')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--session', required=True)
    parser.add_argument('--indices', nargs='+', choices=['nifty', 'sensex', 'banknifty']); parser.add_argument('--membership')
    parser.add_argument('--format', choices=['json', 'markdown'], default='markdown'); parser.add_argument('--out', required=True)
    parser.add_argument('--brief', action='store_true', help='Short answer-first markdown')
    args = parser.parse_args(argv)
    # Only collection and input validation become BLOCKED; rendering errors are not disguised as data gaps.
    try:
        from trader import read
        out = Path(args.out)
        if out.exists(): raise Block('OUTPUT: choose a new file')
        if args.indices == ['sensex']:
            result = sensex_notice(args.session)
        else:
            result = collect(args.session, selected=args.indices, membership=read(args.membership) if args.membership else None)
    except Block as exc:
        print('BLOCKED: ' + str(exc)); return 2
    except OSError:
        print('BLOCKED: membership file unavailable'); return 2
    except UnicodeError:
        raise
    except (ValueError, KeyError, TypeError):
        print('BLOCKED: invalid closing context request'); return 2
    rendered = json.dumps(result, indent=2, allow_nan=False) if args.format == 'json' else markdown(result, brief=args.brief)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('x', encoding='utf-8') as stream: stream.write(rendered)
    except OSError:
        print('BLOCKED: could not create the output file'); return 2
    print(json.dumps({'status': result['status'], 'submitted': False}))
    if 'alternative_command' in result: print('Sensex is not in the NSE closing report; use: ' + SENSEX_ROUTE)
    return 0 if any(i['status'] == 'OBSERVED' for i in result['indices'].values()) else 2


if __name__ == '__main__': raise SystemExit(main())
