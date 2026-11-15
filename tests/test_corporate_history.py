import copy
from decimal import Decimal
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT / 'scripts'))
from core import Block, instant
from corporate_history import reconstruct


class CorporateHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); body = b'Synthetic history and action fixture, not exchange evidence.'
        (self.root / 'source.txt').write_bytes(body)
        artifact = {'path': 'source.txt', 'sha256': hashlib.sha256(body).hexdigest(),
                    'source': 'https://www.bseindia.com/fixture.pdf', 'published_at': '2026-11-01T10:00:00+05:30'}
        self.old, self.new = 'INE000000001', 'INE000000002'
        def bar(day, isin, price, volume):
            return {'session': day, 'isin': isin, 'open': price, 'high': price, 'low': price, 'close': price,
                    'volume': volume, 'available_at': day + 'T16:00:00+05:30'}
        self.spec = {'schema': 'equity_action_history_v1', 'reviewed': True, 'actions_complete': True,
                     'reference_session': '2026-11-13', 'artifacts': {'raw': artifact, 'split': artifact, 'bonus': artifact},
                     'history': {'exchange': 'NSE', 'symbol': 'DEMO', 'initial_isin': self.old,
                                 'price_basis': 'raw_unadjusted', 'evidence_id': 'raw',
                                 'bars': [bar('2026-11-09', self.old, 200, 100), bar('2026-11-11', self.new, 100, 200),
                                          bar('2026-11-13', self.new, 50, 400)]},
                     'actions': [
                         {'action_id': 's', 'kind': 'split', 'ex_date': '2026-11-11', 'evidence_id': 'split',
                          'old_isin': self.old, 'new_isin': self.new, 'numerator': 2, 'denominator': 1},
                         {'action_id': 'b', 'kind': 'bonus', 'ex_date': '2026-11-13', 'evidence_id': 'bonus',
                          'old_isin': self.new, 'new_isin': self.new, 'numerator': 1, 'denominator': 1}]}
        self.now = instant('2026-11-15T10:00:00+05:30')

    def run_report(self): return reconstruct(self.spec, self.root, self.now)

    def test_split_then_bonus_reconstructs_price_and_volume_basis(self):
        report = self.run_report()
        self.assertEqual([b['close'] for b in report['bars']], ['50', '50', '50'])
        self.assertEqual([b['volume'] for b in report['bars']], ['400', '400', '400'])
        self.assertEqual([b['share_factor'] for b in report['bars']], ['4', '2', '1'])
        for original, adjusted in zip(self.spec['history']['bars'], report['bars']):
            self.assertEqual(Decimal(str(original['close'])) * Decimal(str(original['volume'])),
                             Decimal(adjusted['close']) * Decimal(adjusted['volume']))
        self.assertFalse(report['execution_eligible']); self.assertFalse(report['independently_verified'])
        self.assertEqual(report['bars'][0]['source_isin'], self.old); self.assertEqual(report['isin'], self.new)

    def test_reverse_split_uses_new_shares_per_old_share(self):
        self.spec['actions'] = self.spec['actions'][:1]
        self.spec['actions'][0].update(numerator=1, denominator=10)
        report = self.run_report()
        self.assertEqual(report['bars'][0]['close'], '2000'); self.assertEqual(report['bars'][0]['volume'], '10')

    def test_already_adjusted_or_unknown_basis_rejected(self):
        for basis in ('split_adjusted', 'yahoo_public_chart', None):
            self.spec['history']['price_basis'] = basis
            with self.assertRaises(Block): self.run_report()

    def test_unknown_actions_same_date_and_broken_isin_chain_block(self):
        original = copy.deepcopy(self.spec)
        for mutate in (lambda s: s['actions'][0].update(kind='merger'),
                       lambda s: s['actions'][1].update(ex_date='2026-11-11'),
                       lambda s: s['actions'][1].update(old_isin=self.old),
                       lambda s: s['history']['bars'][0].update(isin=self.new),
                       lambda s: s['actions'][1].update(action_id='s')):
            self.spec = copy.deepcopy(original); mutate(self.spec)
            with self.assertRaises(Block): self.run_report()

    def test_publication_and_price_availability_no_lookahead(self):
        original = copy.deepcopy(self.spec)
        for mutate in (lambda s: s['artifacts']['split'].update(published_at='2026-11-16T10:00:00+05:30'),
                       lambda s: s['history']['bars'][0].update(available_at='2026-11-16T10:00:00+05:30'),
                       lambda s: s['history']['bars'][0].update(available_at='2026-11-08T10:00:00+05:30'),
                       lambda s: s.update(reference_session='2026-11-16')):
            self.spec = copy.deepcopy(original); mutate(self.spec)
            with self.assertRaises(Block): self.run_report()

    def test_ratio_volume_and_ohlc_checked(self):
        original = copy.deepcopy(self.spec)
        for mutate in (lambda s: s['actions'][0].update(denominator=0),
                       lambda s: s['actions'][0].update(numerator=1),
                       lambda s: s['actions'][0].update(numerator=True),
                       lambda s: s['history']['bars'][0].update(volume=-1),
                       lambda s: s['history']['bars'][0].update(high=1),
                       lambda s: s['history']['bars'][0].update(close=float('nan'))):
            self.spec = copy.deepcopy(original); mutate(self.spec)
            with self.assertRaises(Block): self.run_report()

    def test_source_hash_complete_window_and_order_required(self):
        original = copy.deepcopy(self.spec)
        for mutate in (lambda s: s.update(actions_complete=False),
                       lambda s: s['artifacts']['raw'].update(sha256='0'*64),
                       lambda s: s['history']['bars'].reverse(),
                       lambda s: s['artifacts']['split'].update(source='https://other.invalid/fixture.pdf'),
                       lambda s: s['actions'][0].update(ex_date='2026-11-09')):
            self.spec = copy.deepcopy(original); mutate(self.spec)
            with self.assertRaises(Block): self.run_report()

    def test_no_actions_does_not_change_raw_values(self):
        self.spec['actions'] = []
        for row in self.spec['history']['bars']: row['isin'] = self.old
        report = self.run_report()
        self.assertEqual([b['close'] for b in report['bars']], ['200', '100', '50'])
        self.assertEqual(report['actions'], [])


if __name__ == '__main__':
    unittest.main()
