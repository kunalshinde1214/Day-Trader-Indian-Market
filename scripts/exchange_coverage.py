"""Dated NSE surveillance and ban observations, never exchange-wide clearance."""
import argparse
import csv
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.request

from core import Block, instant, fingerprint
from data import MAX_BYTES, NoRedirect

CATALOGUE = 'https://www.nseindia.in/api/daily-reports?key=CM'
BAN = 'https://nsearchives.nseindia.com/content/fo/fo_secban.csv'
REPORT_KEYS = {'CM-SURVEILLANCE-INDICATOR-CSV': 'REG_IND',
               'CM-SURVEILLANCE-INDICATOR-NEW-CSV': 'REG1_IND'}
BASES = ('https://nsearchives.nseindia.in/content/cm/',
         'https://nsearchives.nseindia.com/content/cm/')
STAGES = {
    'GSM': ('gsm', {'100', '0', '1', '2', '3', '4', '6'}),
    'Long_Term_Additional_Surveillance_Measure (Long Term ASM)': ('long_term_asm', {'100', '1', '2', '3', '4'}),
    'Short_Term_Additional_Surveillance_Measure (Short Term ASM)': ('short_term_asm', {'100', '1', '2'}),
    'ESM': ('esm', {'100', '1', '2'}),
    'Derivative contracts in the scrip to be moved out of F and O': ('fo_exit_notice', {'100', '0'}),
}
IDENTITY = {'ScripCode', 'Symbol', 'Nse Exclusive', 'Status', 'Series'}


def allowed_url(url):
    return url in (CATALOGUE, BAN) or any(re.fullmatch(re.escape(base) + r'REG1?_IND\d{6}\.csv', url) for base in BASES)


def fetch(url):
    if not allowed_url(url):
        raise Block('EXCHANGE: unsupported official endpoint')
    req = urllib.request.Request(url, headers={'User-Agent': 'IndiaDayTrader personal research',
                                               'Accept': 'application/json,text/csv'})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise Block('EXCHANGE: oversized response')
    return body


def text(body):
    if not isinstance(body, bytes) or not body or len(body) > MAX_BYTES:
        raise Block('EXCHANGE: empty or oversized report')
    value = body.decode('utf-8-sig')
    if '\x00' in value:
        raise Block('EXCHANGE: invalid text')
    return value


def catalogue(body, now):
    data = json.loads(text(body))
    if not isinstance(data, dict) or not data:
        raise Block('EXCHANGE: unsupported catalogue')
    found = {}
    for group, rows in data.items():
        if group in ('currentDate', 'previousDate', 'futureDate'):
            datetime.strptime(rows, '%d-%b-%Y')
            continue
        if group not in ('CurrentDay', 'PreviousDay', 'FutureDay'):
            raise Block('EXCHANGE: unknown catalogue group')
        if not isinstance(rows, list):
            raise Block('EXCHANGE: unsupported catalogue group')
        for row in rows:
            if not isinstance(row, dict):
                raise Block('EXCHANGE: malformed catalogue row')
            key = row.get('fileKey')
            if key not in REPORT_KEYS:
                continue
            day = datetime.strptime(row['tradingDate'], '%d-%b-%Y').date()
            name = REPORT_KEYS[key] + day.strftime('%d%m%y') + '.csv'
            if row.get('fileActlName') != name or row.get('fileSegment') != 'CM' or row.get('filePath') not in BASES:
                raise Block('EXCHANGE: report identity/path mismatch')
            if day > now.date():
                raise Block('EXCHANGE: future surveillance report')
            identity = (day, key)
            candidate = {'url': row['filePath'] + name, 'report_date': day.isoformat(), 'format': REPORT_KEYS[key]}
            if identity in found and candidate != found[identity]:
                raise Block('EXCHANGE: conflicting report links')
            found[identity] = candidate
    if not found:
        raise Block('EXCHANGE: surveillance report absent from catalogue')
    # Date takes precedence over format. Do not substitute an older richer file.
    chosen = found[max(found, key=lambda k: (k[0], k[1] == 'CM-SURVEILLANCE-INDICATOR-NEW-CSV'))]
    if (now.date() - datetime.fromisoformat(chosen['report_date']).date()).days > 7:
        raise Block('EXCHANGE: surveillance catalogue older than seven calendar days')
    return {**chosen, 'catalogue_source': CATALOGUE, 'catalogue_sha256': hashlib.sha256(body).hexdigest()}


def surveillance(body, descriptor, now):
    day = datetime.fromisoformat(descriptor['report_date']).date()
    name = descriptor['format'] + day.strftime('%d%m%y') + '.csv'
    if descriptor['format'] not in REPORT_KEYS.values() or descriptor['url'] not in [base + name for base in BASES]:
        raise Block('EXCHANGE: report provenance mismatch')
    if not 0 <= (now.date() - day).days <= 7:
        raise Block('EXCHANGE: stale/future surveillance report')
    reader = csv.DictReader(io.StringIO(text(body)), strict=True)
    headers = reader.fieldnames
    if not headers or len(headers) != len(set(headers)) or not (IDENTITY | set(STAGES)) <= set(headers):
        raise Block('EXCHANGE: missing or duplicate surveillance columns')
    rows = {}; symbols = set()
    for row in reader:
        if None in row or any(v is None for v in row.values()):
            raise Block('EXCHANGE: ragged surveillance row')
        row = {k: v.strip() for k, v in row.items()}
        symbol, series = row['Symbol'], row['Series']
        key = symbol + ':' + series
        if not re.fullmatch(r'[A-Z0-9&_.-]{1,40}', symbol) or not re.fullmatch(r'[A-Z0-9]{2}', series) or key in rows:
            raise Block('EXCHANGE: ambiguous security identity')
        if row['Status'] not in ('A', 'S', 'I') or row['Nse Exclusive'] not in ('Y', 'N'):
            raise Block('EXCHANGE: unsupported trading status')
        stages = {}
        for column, (label, supported) in STAGES.items():
            if row[column] not in supported:
                raise Block('EXCHANGE: unsupported surveillance stage')
            stages[label] = None if row[column] == '100' else int(row[column])
        # Keep every additional column, without inventing undocumented semantics.
        extra = {k: v for k, v in row.items() if k not in IDENTITY | set(STAGES)}
        rows[key] = {'symbol': symbol, 'series': series, 'status': row['Status'], 'stages': stages,
                     'trade_for_trade': series in ('BE', 'BZ', 'ST', 'SZ'), 'additional_source_fields': extra}
        symbols.add(symbol)
        if len(rows) > 30000:
            raise Block('EXCHANGE: excessive surveillance rows')
    if not rows:
        raise Block('EXCHANGE: empty surveillance file')
    return {**descriptor, 'sha256': hashlib.sha256(body).hexdigest(), 'retrieved_at': now.isoformat(),
            'records': rows, 'row_count': len(rows), 'symbol_count': len(symbols),
            'effective_session_verified': False, 'execution_eligible': False}


def bans(body, now):
    lines = [line.strip() for line in text(body).splitlines() if line.strip()]
    if not lines:
        raise Block('EXCHANGE: empty ban report')
    match = re.fullmatch(r'Securities in Ban For Trade Date (\d{2}-[A-Za-z]{3}-\d{4})\s*:\s*(NIL)?', lines[0])
    if not match:
        raise Block('EXCHANGE: dated ban heading required')
    day = datetime.strptime(match[1], '%d-%b-%Y').date()
    if not -7 <= (day - now.date()).days <= 7:
        raise Block('EXCHANGE: stale/far-future ban report')
    symbols = []
    if match[2]:
        if len(lines) != 1:
            raise Block('EXCHANGE: NIL ban report has unexpected rows')
    else:
        reader = csv.reader(lines[1:], strict=True)
        header = next(reader, None)
        if header not in (['Symbol'], ['Sr.No.', 'Symbol']):
            raise Block('EXCHANGE: unsupported ban columns')
        for index, row in enumerate(reader, 1):
            if len(row) != len(header) or (len(row) == 2 and row[0].strip() != str(index)):
                raise Block('EXCHANGE: malformed ban row')
            value = row[-1].strip()
            if not re.fullmatch(r'[A-Z0-9&_.-]{1,40}', value) or value in symbols or value == 'NIL':
                raise Block('EXCHANGE: invalid/duplicate ban symbol')
            symbols.append(value)
        if not symbols:
            raise Block('EXCHANGE: empty ban rows without explicit NIL')
    return {'source': BAN, 'sha256': hashlib.sha256(body).hexdigest(), 'retrieved_at': now.isoformat(),
            'trade_date': day.isoformat(), 'symbols': symbols, 'explicit_nil': bool(match[2]),
            'applies_to_retrieval_date': day == now.date(), 'execution_eligible': False}


def collect(symbols, *, fetcher=fetch, clock=instant):
    now = clock()
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 20 or len(set(symbols)) != len(symbols):
        raise Block('EXCHANGE: one to twenty distinct symbols required')
    if any(not isinstance(s, str) or not re.fullmatch(r'[A-Z0-9&_.-]{1,40}\.(NS|BO)', s) for s in symbols):
        raise Block('EXCHANGE: exact Indian public symbols required')
    result = {'schema': 'india_exchange_observations_v1', 'as_of': now.isoformat(), 'coverage': [], 'symbols': {}}
    for component, run in (
        ('nse_surveillance', lambda: surveillance_body(fetcher, now)),
        ('nse_fo_ban', lambda: bans(fetcher(BAN), now)),
    ):
        try:
            result[component] = run()
            result['coverage'].append({'component': component, 'status': 'COLLECTED'})
        except (Block, ValueError, KeyError, TypeError, OSError, csv.Error):
            result['coverage'].append({'component': component, 'status': 'FAILED',
                                       'reason': 'Unavailable, stale, ambiguous or unsupported official report'})
    records = result.get('nse_surveillance', {}).get('records', {})
    ban = result.get('nse_fo_ban')
    for selected in symbols:
        bare, exchange = selected.rsplit('.', 1)
        matches = [r for r in records.values() if r['symbol'] == bare] if exchange == 'NS' else []
        result['symbols'][selected] = {
            'surveillance_status': 'OBSERVED' if matches else 'UNKNOWN', 'surveillance_records': matches,
            'ban_status': ('LISTED' if bare in ban['symbols'] else 'NOT_LISTED') if exchange == 'NS' and ban and ban['applies_to_retrieval_date'] else 'UNKNOWN',
            'execution_eligible': False,
        }
    result.update(status='PARTIAL', full_market_coverage=False, execution_eligible=False, submitted=False,
                  missing_coverage=['BSE surveillance and bans', 'Intraday emergency changes and verified effective sessions',
                                    'Instrument auctions and special sessions', 'Complete F&O eligibility and membership',
                                    'Interpretation of additional surveillance columns'])
    result['observation_hash'] = fingerprint(result)
    return result


def surveillance_body(fetcher, now):
    descriptor = catalogue(fetcher(CATALOGUE), now)
    return surveillance(fetcher(descriptor['url']), descriptor, now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbols', nargs='+', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        out = Path(args.out)
        if out.exists():
            raise Block('OUTPUT: choose a new file')
        result = collect(args.symbols)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
        print(json.dumps({'status': result['status'], 'coverage': result['coverage'], 'submitted': False}))
        return 0 if any(c['status'] == 'COLLECTED' for c in result['coverage']) else 2
    except (Block, ValueError, KeyError, TypeError, OSError):
        print('BLOCKED: invalid exchange collection request; no trading clearance inferred')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
