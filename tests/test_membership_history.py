import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from core import Block, instant
from membership_history import select, load, main


class MembershipHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        body = b'Synthetic source fixture; not real index membership.'
        (self.root / 'source.txt').write_bytes(body)
        artifact = {'path': 'source.txt', 'sha256': hashlib.sha256(body).hexdigest(),
                    'source': 'https://www.niftyindices.com/Press_Release/fixture.pdf',
                    'published_at': '2026-10-01T10:00:00+05:30'}
        self.a = {'exchange': 'NSE', 'symbol': 'DEMOA', 'isin': 'INE000000001'}
        self.b = {'exchange': 'NSE', 'symbol': 'DEMOB', 'isin': 'INE000000002'}
        self.c = {'exchange': 'NSE', 'symbol': 'DEMOC', 'isin': 'INE000000003'}
        self.spec = {'schema': 'index_membership_history_v1', 'index_id': 'nifty', 'reviewed': True,
                     'artifacts': {'oct': artifact, 'nov': {**artifact, 'published_at': '2026-10-30T18:00:00+05:30'}},
                     'snapshots': [
                         {'snapshot_id': 'oct', 'effective_from': '2026-10-01', 'effective_through': '2026-10-31',
                          'evidence_id': 'oct', 'expected_count': 2, 'members': [self.a, self.b]},
                         {'snapshot_id': 'nov', 'effective_from': '2026-11-01', 'effective_through': '2026-11-30',
                          'evidence_id': 'nov', 'expected_count': 2, 'members': [{**self.a, 'symbol': 'RENAMED'}, self.c]}]}
        self.now = instant('2026-11-15T10:00:00+05:30')

    def result(self, session='2026-11-15', now=None):
        return select(self.spec, self.root, session, now or self.now)['nifty']

    def test_effective_interval_and_isin_changes(self):
        report = self.result()
        self.assertEqual(report['members'], ['DEMOC', 'RENAMED'])
        change = report['observed_changes'][0]
        self.assertEqual(change['added'], [self.c]); self.assertEqual(change['removed'], [self.b])
        self.assertEqual(change['renamed'][0]['before'], 'DEMOA'); self.assertFalse(change['unobserved_gap'])
        self.assertFalse(report['membership_verified']); self.assertFalse(report['execution_eligible'])
        self.assertEqual(self.result('2026-10-31')['members'], ['DEMOA', 'DEMOB'])

    def test_no_current_list_fallback_outside_coverage(self):
        for day in ('2026-09-30', '2026-12-01'):
            with self.assertRaises(Block): self.result(day, instant('2026-12-02T10:00:00+05:30'))

    def test_publication_after_information_cutoff_blocks(self):
        self.spec['artifacts']['nov']['published_at'] = '2026-11-02T18:00:00+05:30'
        with self.assertRaises(Block): self.result('2026-11-01', instant('2026-11-01T10:00:00+05:30'))
        report = self.result('2026-11-01')
        self.assertFalse(report['same_session_information_cutoff'])

    def test_future_session_refused_even_if_announced(self):
        with self.assertRaises(Block): self.result('2026-11-16')

    def test_missing_period_not_filled_and_change_marks_gap(self):
        self.spec['snapshots'][0]['effective_through'] = '2026-10-29'
        with self.assertRaises(Block): self.result('2026-10-30')
        self.assertTrue(self.result()['observed_changes'][0]['unobserved_gap'])

    def test_later_publications_do_not_leak_into_changes(self):
        self.spec['artifacts']['oct']['published_at'] = '2026-11-16T10:00:00+05:30'
        self.assertEqual(self.result()['observed_changes'], [])

    def test_overlapping_unordered_and_duplicate_intervals_fail(self):
        original = copy.deepcopy(self.spec)
        for mutate in (lambda s: s['snapshots'][1].update(effective_from='2026-10-31'),
                       lambda s: s['snapshots'].reverse(),
                       lambda s: s['snapshots'][1].update(snapshot_id='oct'),
                       lambda s: s['snapshots'][1].update(effective_through='2026-10-30')):
            self.spec = copy.deepcopy(original); mutate(self.spec)
            with self.assertRaises(Block): self.result()

    def test_count_identity_exchange_and_review_required(self):
        original = copy.deepcopy(self.spec)
        for mutate in (lambda s: s.update(reviewed=False),
                       lambda s: s['snapshots'][0].update(expected_count=50),
                       lambda s: s['snapshots'][0]['members'][1].update(isin=self.a['isin']),
                       lambda s: s['snapshots'][0]['members'][1].update(symbol=self.a['symbol']),
                       lambda s: s['snapshots'][0]['members'][0].update(exchange='BSE')):
            self.spec = copy.deepcopy(original); mutate(self.spec)
            with self.assertRaises(Block): self.result()

    def test_file_hash_and_relative_path_required(self):
        (self.root / 'source.txt').write_bytes(b'changed')
        with self.assertRaises(Block): self.result()
        self.spec['artifacts']['oct']['path'] = '../source.txt'
        with self.assertRaises(Block): self.result()

    def test_source_host_publication_timezone_and_dates_checked(self):
        original = copy.deepcopy(self.spec)
        for mutate in (lambda s: s['artifacts']['oct'].update(source='https://www.niftyindices.com.evil/'),
                       lambda s: s['artifacts']['oct'].update(source='https://user:secret@www.niftyindices.com/a'),
                       lambda s: s['artifacts']['oct'].update(published_at='2026-10-01T10:00:00'),
                       lambda s: s['snapshots'][0].update(effective_from='20261001'),
                       lambda s: s['snapshots'][0].update(effective_from='2026-13-01'),
                       lambda s: s['snapshots'][0].update(effective_through='2026-02-30'),
                       lambda s: s['snapshots'][0].update(effective_through=None)):
            self.spec = copy.deepcopy(original); mutate(self.spec)
            with self.assertRaises(Block): self.result()

    def test_daily_membership_preserves_partial_status_and_detects_changes(self):
        from daily_context import membership
        supplied = select(self.spec, self.root, '2026-11-15', self.now)
        report = membership('nifty', self.now, supplied)
        self.assertFalse(report['membership_verified'])
        self.assertIn('not independently', report['membership_basis'])
        supplied['nifty']['members'].append('INJECTED')
        with self.assertRaises(Block): membership('nifty', self.now, supplied)

    def test_sensex_identity_kept_on_bse(self):
        self.spec['index_id'] = 'sensex'
        for snapshot in self.spec['snapshots']:
            for member in snapshot['members']: member['exchange'] = 'BSE'
        self.spec['artifacts']['oct']['source'] = self.spec['artifacts']['nov']['source'] = 'https://www.bseindices.com/fixture.pdf'
        report = select(self.spec, self.root, '2026-11-15', self.now)['sensex']
        self.assertEqual({m['exchange'] for m in report['member_records']}, {'BSE'})

    def test_cli_writes_new_file_and_does_not_overwrite(self):
        source = self.root / 'input.json'; out = self.root / 'output.json'
        source.write_text(json.dumps(self.spec), encoding='utf-8')
        args = ['--input', str(source), '--session', '2026-11-01', '--known-at', '2026-11-01T10:00:00+05:30', '--out', str(out)]
        self.assertEqual(main(args), 0); self.assertEqual(main(args), 2)
        self.assertFalse(json.loads(out.read_text())['nifty']['membership_verified'])


if __name__ == '__main__':
    unittest.main()
