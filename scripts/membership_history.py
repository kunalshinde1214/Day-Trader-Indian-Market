"""Source-backed index membership intervals with explicit information cutoffs."""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from core import Block, instant, integer, fingerprint
from inventory_book import evidence_files
from trader import read

INDICES = {'nifty': 'NSE', 'banknifty': 'NSE', 'nifty500': 'NSE', 'sensex': 'BSE'}
HOSTS = {'niftyindices.com', 'www.niftyindices.com', 'www.nseindia.com', 'www.nseindia.in',
         'nsearchives.nseindia.com', 'nsearchives.nseindia.in', 'archives.nseindia.com',
         'www.bseindia.com', 'www.bseindices.com', 'bseindices.com'}
SNAPSHOT_FIELDS = {'snapshot_id', 'effective_from', 'effective_through', 'evidence_id', 'expected_count', 'members'}


def members(rows, exchange):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2000:
        raise Block('MEMBERSHIP_HISTORY: bounded complete member list required')
    by_isin = {}; symbols = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'exchange', 'symbol', 'isin'}:
            raise Block('MEMBERSHIP_HISTORY: exact member identity fields required')
        if row['exchange'] != exchange or not isinstance(row['symbol'], str) or not re.fullmatch(r'[A-Z0-9&_.-]{1,40}', row['symbol']):
            raise Block('MEMBERSHIP_HISTORY: wrong exchange or symbol')
        if not isinstance(row['isin'], str) or not re.fullmatch(r'IN[A-Z0-9]{10}', row['isin']):
            raise Block('MEMBERSHIP_HISTORY: Indian security ISIN required')
        if row['isin'] in by_isin or row['symbol'] in symbols:
            raise Block('MEMBERSHIP_HISTORY: duplicate ISIN or symbol')
        by_isin[row['isin']] = dict(row); symbols.add(row['symbol'])
    return by_isin


def load(spec, root):
    if spec.get('schema') != 'index_membership_history_v1' or spec.get('reviewed') is not True:
        raise Block('MEMBERSHIP_HISTORY: explicit reviewed source intervals required')
    index = spec['index_id']
    if index not in INDICES:
        raise Block('MEMBERSHIP_HISTORY: unsupported index')
    hashes = evidence_files(spec, root)
    sources = {}
    for key, artifact in spec['artifacts'].items():
        url = urlsplit(artifact['source'])
        if url.scheme != 'https' or url.hostname not in HOSTS or url.username or url.password or url.port not in (None, 443):
            raise Block('MEMBERSHIP_HISTORY: official index/exchange source required')
        published = instant(artifact['published_at'])
        sources[key] = {'source': artifact['source'], 'published_at': published.isoformat(), 'sha256': hashes[key]}
    snapshots = spec['snapshots']
    if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= 5000:
        raise Block('MEMBERSHIP_HISTORY: bounded snapshot intervals required')
    normalized = []; seen = set(); previous = None
    for row in snapshots:
        if not isinstance(row, dict) or set(row) != SNAPSHOT_FIELDS:
            raise Block('MEMBERSHIP_HISTORY: exact interval fields required')
        sid = row['snapshot_id']
        if not isinstance(sid, str) or not sid.strip() or sid in seen:
            raise Block('MEMBERSHIP_HISTORY: unique snapshot ID required')
        seen.add(sid)
        start, end = date.fromisoformat(row['effective_from']), date.fromisoformat(row['effective_through'])
        if row['effective_from'] != start.isoformat() or row['effective_through'] != end.isoformat():
            raise Block('MEMBERSHIP_HISTORY: ISO calendar dates required')
        if start > end or previous is not None and start <= previous:
            raise Block('MEMBERSHIP_HISTORY: ordered non-overlapping intervals required')
        previous = end
        if row['evidence_id'] not in sources:
            raise Block('MEMBERSHIP_HISTORY: missing source evidence')
        selected = members(row['members'], INDICES[index])
        if integer(row['expected_count']) != len(selected):
            raise Block('MEMBERSHIP_HISTORY: incomplete member count')
        normalized.append({**row, 'members': sorted(selected.values(), key=lambda m: m['isin']),
                           **sources[row['evidence_id']]})
    return index, normalized


def differences(previous, current):
    before = {m['isin']: m for m in previous['members']}
    after = {m['isin']: m for m in current['members']}
    return {'from_snapshot': previous['snapshot_id'], 'to_snapshot': current['snapshot_id'],
            'effective_from': current['effective_from'],
            'unobserved_gap': date.fromisoformat(current['effective_from']) != date.fromisoformat(previous['effective_through']) + timedelta(days=1),
            'added': [after[k] for k in sorted(after.keys() - before.keys())],
            'removed': [before[k] for k in sorted(before.keys() - after.keys())],
            'renamed': [{'isin': k, 'before': before[k]['symbol'], 'after': after[k]['symbol']}
                        for k in sorted(before.keys() & after.keys()) if before[k]['symbol'] != after[k]['symbol']]}


def select(spec, root, session, known_at):
    index, snapshots = load(spec, root)
    day = date.fromisoformat(session)
    if session != day.isoformat():
        raise Block('MEMBERSHIP_HISTORY: ISO session date required')
    if day > known_at.date():
        raise Block('MEMBERSHIP_HISTORY: future session lookup refused')
    matches = [s for s in snapshots if date.fromisoformat(s['effective_from']) <= day <= date.fromisoformat(s['effective_through'])]
    if len(matches) != 1:
        raise Block('MEMBERSHIP_HISTORY: no exact source interval; never carry a list through a gap')
    selected = matches[0]
    if instant(selected['published_at']) > known_at:
        raise Block('MEMBERSHIP_HISTORY: membership was not available by the information cutoff')
    visible = [s for s in snapshots if s['effective_from'] <= selected['effective_from'] and instant(s['published_at']) <= known_at]
    changes = [differences(a, b) for a, b in zip(visible, visible[1:])]
    record = {'schema': 'reviewed_index_membership_v1', 'index_id': index, 'membership_date': session,
              'as_of': known_at.isoformat(), 'source': selected['source'],
              'members': sorted(m['symbol'] for m in selected['members']), 'member_records': selected['members'],
              'membership_verified': False, 'membership_reviewed': True,
              'membership_basis': 'operator-reviewed source intervals and publication times; artifact hashes checked, not independently authenticated',
              'snapshot_id': selected['snapshot_id'], 'effective_from': selected['effective_from'],
              'effective_through': selected['effective_through'], 'published_at': selected['published_at'],
              'artifact_sha256': selected['sha256'], 'input_hash': fingerprint(spec),
              'same_session_information_cutoff': known_at.date() == day,
              'independently_verified': False, 'execution_eligible': False, 'submitted': False,
              'observed_changes': changes}
    record['membership_hash'] = fingerprint(record)
    return {index: record}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True); parser.add_argument('--session', required=True)
    parser.add_argument('--known-at', required=True); parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        out = Path(args.out); known_at = instant(args.known_at)
        if out.exists() or known_at > instant():
            raise Block('MEMBERSHIP_HISTORY: new output and nonfuture information cutoff required')
        source = Path(args.input)
        result = select(read(source), source.parent, args.session, known_at)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, indent=2, allow_nan=False)
        print(json.dumps({'status': 'REVIEWED_SOURCE_ONLY', 'out': str(out), 'submitted': False}))
        return 0
    except (Block, ValueError, KeyError, TypeError, OSError):
        print('BLOCKED: incomplete, changed or unavailable membership evidence')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
