"""Resume bounded NSE announcement history into the personal filing backlog."""
import argparse
from datetime import date, timedelta
import json
import sqlite3
from pathlib import Path
import uuid
from announcement_history import collect
from core import Block, fingerprint, instant, integer
from filing_archive import run as archive_run
from service import ProcessLock
from trader import read, write


def run(directory, start, end, *, windows=1, window_days=3, filing_limit=5,
        collector=collect, archiver=archive_run, clock=instant):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    windows, window_days, filing_limit = integer(windows), integer(window_days), integer(filing_limit)
    if (first.isoformat() != start or last.isoformat() != end or not first <= last <= clock().date() or
            windows > 10 or window_days > 7 or filing_limit > 20):
        raise Block('HISTORY: ordered nonfuture dates, at most ten windows, seven days/window and twenty filings required')
    directory = Path(directory); settings = {'from_date': start, 'through_date': end, 'source': 'nse-equities-announcements'}
    identity = fingerprint(settings); completed = 0; reason = 'window_limit'
    with ProcessLock(directory/'history.lock'):
        checkpoint = directory/'checkpoint.json'
        saved = read(checkpoint) if checkpoint.exists() else {'schema': 'history_checkpoint_v1', 'settings_hash': identity, 'next_date': start}
        if saved.get('schema') != 'history_checkpoint_v1' or saved.get('settings_hash') != identity:
            raise Block('HISTORY: existing checkpoint belongs to a different requested window')
        cursor = date.fromisoformat(saved['next_date'])
        if not first <= cursor <= last + timedelta(days=1): raise Block('HISTORY: invalid checkpoint cursor')
        for _ in range(windows):
            if cursor > last: reason = 'requested_window_collected'; break
            if (directory/'stop.json').exists(): reason = 'requested_stop'; break
            through = min(last, cursor + timedelta(days=window_days - 1))
            try:
                result = collector(cursor.isoformat(), through.isoformat(), clock=clock)
            except (Block, ValueError, KeyError, TypeError, OSError):
                write(directory/'health.json', {'state': 'stopped', 'reason': 'collection_failed_cursor_preserved',
                      'next_date': cursor.isoformat(), 'as_of': clock().isoformat(), 'full_filing_coverage': False})
                raise
            expected = {(cursor + timedelta(days=n)).isoformat() for n in range((through-cursor).days+1)}
            coverage = result.get('sources', [])
            observed = [row.get('requested_session') for row in coverage]
            valid = (result.get('schema') == 'india_news_v1' and set(observed) == expected and len(observed) == len(expected)
                     and all(row.get('status') == 'COLLECTED' and row.get('rejected', 0) == 0 for row in coverage))
            name = cursor.isoformat() + '-' + uuid.uuid4().hex + '.json'
            directory.mkdir(parents=True, exist_ok=True)
            with (directory/name).open('x', encoding='utf-8') as f: json.dump(result, f, indent=2, allow_nan=False)
            # Archive valid discoveries even if another requested date failed.
            try:
                backlog = archiver(result, directory/'filings.sqlite', limit=filing_limit, clock=clock)
            except (Block, ValueError, KeyError, TypeError, OSError, sqlite3.Error):
                write(directory/'health.json', {'state': 'stopped', 'reason': 'archive_failed_cursor_preserved',
                      'next_date': cursor.isoformat(), 'as_of': clock().isoformat(), 'full_filing_coverage': False})
                raise
            if not valid:
                reason = 'source_gap_cursor_preserved'; break
            cursor = through + timedelta(days=1); completed += 1
            write(checkpoint, {'schema': 'history_checkpoint_v1', 'settings_hash': identity,
                  'next_date': cursor.isoformat(), 'report': name, 'report_hash': fingerprint(result),
                  'as_of': clock().isoformat(), 'filings_deferred': backlog['deferred'], 'full_filing_coverage': False})
        if cursor > last: reason = 'requested_window_collected'
        result = {'schema': 'history_run_v1', 'as_of': clock().isoformat(), 'reason': reason,
                  'windows_completed_this_run': completed, 'next_date': cursor.isoformat(),
                  'requested_window_collected': cursor > last, 'full_filing_coverage': False,
                  'execution_eligible': False, 'submitted': False}
        write(directory/'health.json', result)
        return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('directory', 'from-date', 'through-date'): p.add_argument('--'+name, required=True)
    p.add_argument('--windows', type=int, default=1); p.add_argument('--window-days', type=int, default=3)
    p.add_argument('--filing-limit', type=int, default=5); a = p.parse_args(argv)
    try:
        result = run(a.directory, a.from_date, a.through_date, windows=a.windows, window_days=a.window_days, filing_limit=a.filing_limit)
        print(json.dumps(result, indent=2)); return 2 if result['reason'] == 'source_gap_cursor_preserved' else 0
    except (Block, ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        print('BLOCKED: history source/storage validation failed; checkpoint not advanced for the failed window'); return 2


if __name__ == '__main__': raise SystemExit(main())
