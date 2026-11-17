"""Sequential coordination of individually human-armed protection mandates."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import time
from core import Block, fingerprint, instant, integer
from execution import ExecutionStore, store_path
from protection_runner import scope, validate_mandate, run as run_member
from service import ProcessLock
from trader import read, write


def members(settings, root):
    if settings.get('schema') != 'protection_group_v1' or set(settings) != {'schema', 'workdirs', 'max_actions'}:
        raise Block('GROUP: exact group settings required')
    paths = settings['workdirs']; maximum = integer(settings['max_actions'])
    if not isinstance(paths, list) or not 1 <= len(paths) <= 20 or maximum > 100:
        raise Block('GROUP: one to twenty scopes and at most one hundred actions required')
    result = []; scopes = set(); accounts = {}; resolved = set(); broker_accounts = {}
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip(): raise Block('GROUP: explicit member path required')
        w = (Path(root)/raw).resolve()
        if w in resolved: raise Block('GROUP: duplicate member directory')
        resolved.add(w); digest, values = scope(w)
        c, i, p = values['config'], values['instrument'], values['protection-policy']
        account = (c['broker'], c['account_id']); database = store_path(w, c)
        if c['broker'] in broker_accounts and broker_accounts[c['broker']] != c['account_id']:
            raise Block('GROUP: one account per broker per process; credentials are never switched automatically')
        broker_accounts[c['broker']] = c['account_id']
        identity = (*account, i['exchange'], i['token'], p['product'])
        if identity in scopes: raise Block('GROUP: duplicate account/instrument/product mandate')
        scopes.add(identity)
        if account in accounts and accounts[account] != database:
            raise Block('GROUP: same-account instruments must share one configured execution database')
        accounts[account] = database
        result.append((w, digest, values, database))
    return result


def validate_all(rows, now):
    for w, digest, values, database in rows:
        current, latest = scope(w)
        if current != digest: raise Block('GROUP: member configuration changed')
        store = ExecutionStore(database)
        try:
            c = values['config']
            validate_mandate(read(w/'state/protection-mandate.json'), current, latest,
                             store.records(c['broker'], c['account_id']), now,
                             (w/'state/protection-revoked.json').exists())
        finally: store.close()


def run(settings, directory, *, cycles=12, interval=5, worker=run_member, clock=instant, sleep=time.sleep):
    cycles, interval = integer(cycles), integer(interval)
    if cycles > 8640 or not 5 <= interval <= 60: raise Block('GROUP: bounded cycles and 5..60 second interval required')
    directory = Path(directory).resolve(); rows = members(settings, directory)
    maximum = integer(settings['max_actions'])
    digest = fingerprint({'settings': settings, 'scopes': [r[1] for r in rows]})
    sent = 0; completed = 0; status = 'STARTING'; outcomes = []
    with ExitStack() as locks:
        locks.enter_context(ProcessLock(directory/'group.lock'))
        # Lock every member first. A busy scope cannot lead to partial startup.
        for w, _, _, _ in sorted(rows, key=lambda r: str(r[0])):
            locks.enter_context(ProcessLock(w/'state/protection-runner.lock'))
        if (directory/'revoked.json').exists(): raise Block('GROUP: group revocation latch present')
        validate_all(rows, clock())
        try:
            for _ in range(cycles):
                outcomes = []
                for w, _, _, _ in rows:
                    if (directory/'revoked.json').exists(): raise Block('GROUP: group revoked')
                    if sent >= maximum: status = 'ACTION_LIMIT'; break
                    validate_all(rows, clock())
                    # Existing runner recollects full books, revalidates the
                    # mandate and records intent before any broker submission.
                    result = worker(w, cycles=1, interval=interval, clock=clock, sleep=sleep)
                    sent += integer(result['submitted_this_run'], minimum=0)
                    outcomes.append({'workdir': str(w), **result})
                    write(directory/'health.json', {'schema': 'protection_group_health_v1', 'state': 'running',
                          'as_of': clock().isoformat(), 'scope_hash': digest, 'submitted_this_run': sent,
                          'members': outcomes, 'flat_guaranteed': False})
                completed += 1
                if status == 'ACTION_LIMIT': break
                if len(outcomes) == len(rows) and all(r['status'] == 'FLAT_OBSERVED' for r in outcomes):
                    status = 'ALL_SCOPES_FLAT_OBSERVED'; break
                status = 'CYCLE_LIMIT'
                if completed < cycles: sleep(interval)
        except BaseException:
            status = 'FAILED_REVOKED'
            write(directory/'revoked.json', {'as_of': clock().isoformat(), 'reason': 'Member failed or interrupted; reconcile all accounts before explicit rearming'})
            for w, _, _, _ in rows:
                write(w/'state/protection-revoked.json', {'as_of': clock().isoformat(), 'reason': 'Group failed or interrupted'})
            raise
        finally:
            write(directory/'health.json', {'schema': 'protection_group_health_v1', 'state': 'stopped', 'status': status,
                  'as_of': clock().isoformat(), 'scope_hash': digest, 'completed_cycles': completed,
                  'submitted_this_run': sent, 'members': outcomes, 'flat_guaranteed': False})
    return {'status': status, 'completed_cycles': completed, 'submitted_this_run': sent, 'flat_guaranteed': False}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--settings', required=True); p.add_argument('--directory', required=True)
    p.add_argument('--cycles', type=int, default=12); p.add_argument('--interval', type=int, default=5)
    a = p.parse_args(argv)
    try:
        result = run(read(a.settings), a.directory, cycles=a.cycles, interval=a.interval)
        print(json.dumps(result, indent=2)); return 0
    except (Block, KeyError, ValueError, TypeError, OSError, sqlite3.Error, KeyboardInterrupt):
        print('BLOCKED: inspect group/member health and shared execution records before any restart'); return 2


if __name__ == '__main__': raise SystemExit(main())
