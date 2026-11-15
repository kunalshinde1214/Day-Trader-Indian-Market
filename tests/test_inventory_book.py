"""Inventory event arithmetic, evidence integrity and durable no-overwrite checks."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from core import Block, fingerprint, instant
from inventory_book import lot, reconcile, record, main

NOW = instant('2026-11-13T10:00:00+05:30')


class InventoryBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        artifact = self.root / 'statement.txt'; artifact.write_bytes(b'Fictional statement for offline testing only')
        self.row = lot({'lot_id': 'lot-1', 'exchange': 'NSE', 'symbol': 'DEMO', 'token': '123',
                        'isin': 'INE000A01010', 'bucket': 'T1', 'quantity': 10, 'cost_basis_inr': '1000', 'principal_inr': '0'})
        identity = {'broker': 'kite', 'account_id': 'SYNTHETIC'}
        self.spec = {**identity, 'schema': 'inventory_reconciliation_v1', 'reviewed': True, 'complete': True,
                     'as_of': NOW.isoformat(), 'trading_activity': False,
                     'opening': {**identity, 'as_of': '2026-11-12T16:00:00+05:30', 'evidence_id': 'statement', 'lots': [self.row]},
                     'closing': {**identity, 'as_of': NOW.isoformat(), 'evidence_id': 'statement', 'lots': [copy.deepcopy(self.row)]},
                     'events': [], 'artifacts': {'statement': {'path': artifact.name, 'source': 'synthetic fixture',
                     'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}}}

    def event(self, action, **kwargs):
        return {'event_id': 'e1', 'broker': 'kite', 'account_id': 'SYNTHETIC', 'action': action,
                'effective_at': '2026-11-13T09:00:00+05:30', 'evidence_id': 'statement',
                'lot_id': self.row['lot_id'], 'expected_lot_hash': fingerprint(self.row), **kwargs}

    def run_case(self): return reconcile(self.spec, self.root, NOW)

    def transfer(self, q=10, destination='SETTLED', repayment=0):
        self.spec['events'] = [self.event('transfer', quantity=q, destination_bucket=destination,
                                         new_lot_id='lot-2', principal_repaid_inr=repayment)]

    def test_statement_buckets_do_not_authorize_sale(self):
        for bucket in ('T1', 'SETTLED', 'PLEDGED', 'MTF', 'ENTITLEMENT'):
            self.row.update(bucket=bucket, principal_inr='800' if bucket == 'MTF' else '0')
            self.spec['closing']['lots'] = [copy.deepcopy(self.row)]
            result = self.run_case()
            self.assertEqual(result['buckets'][bucket]['cost_basis_inr'], '1000')
            self.assertIsNone(result['sellable_quantity']); self.assertFalse(result['execution_eligible'])

    def test_partial_settlement_preserves_units_and_cost(self):
        self.transfer(q=4)
        self.spec['closing']['lots'] = [{**self.row, 'quantity': 6, 'cost_basis_inr': '600'},
                                      {**self.row, 'lot_id': 'lot-2', 'bucket': 'SETTLED', 'quantity': 4, 'cost_basis_inr': '400'}]
        result = self.run_case()
        self.assertEqual(result['buckets']['T1']['cost_basis_inr'], '600')
        self.assertEqual(result['buckets']['SETTLED']['cost_basis_inr'], '400')
        self.assertEqual(len(result['audit']), 1)

    def test_pledge_and_unpledge_require_observed_state(self):
        for source, destination in [('SETTLED', 'PLEDGED'), ('PLEDGED', 'SETTLED')]:
            self.row['bucket'] = source; self.transfer(destination=destination)
            with self.assertRaises(Block): self.run_case()
            self.spec['closing']['lots'] = [{**self.row, 'lot_id': 'lot-2', 'bucket': destination}]
            self.assertEqual(self.run_case()['status'], 'RECONCILED')

    def test_funded_transfer_requires_exact_principal_repayment(self):
        self.row.update(bucket='MTF', principal_inr='800'); self.transfer(q=5)
        self.spec['closing']['lots'] = [{**self.row, 'quantity': 5, 'cost_basis_inr': '500', 'principal_inr': '400'},
                                      {**self.row, 'lot_id': 'lot-2', 'bucket': 'SETTLED', 'quantity': 5,
                                       'cost_basis_inr': '500', 'principal_inr': '0'}]
        with self.assertRaises(Block): self.run_case()
        self.spec['events'][0]['principal_repaid_inr'] = '400'
        self.assertEqual(self.run_case()['buckets']['MTF']['principal_inr'], '400')

    def test_split_reconstructs_quantity_and_preserves_cost(self):
        self.spec['events'] = [self.event('split', numerator=2, denominator=1, symbol='DEMO-NEW', token='456', isin='INE000A01028')]
        self.spec['closing']['lots'] = [{**self.row, 'quantity': 20, 'symbol': 'DEMO-NEW', 'token': '456', 'isin': 'INE000A01028'}]
        result = self.run_case(); self.assertEqual(result['lots'][0]['cost_basis_inr'], '1000')
        self.spec['events'][0].update(numerator=1, denominator=3)
        with self.assertRaisesRegex(Block, 'fractional'): self.run_case()

    def test_bonus_requires_later_credit_and_does_not_change_original_cost(self):
        self.row['bucket'] = 'SETTLED'
        self.spec['events'] = [self.event('bonus', numerator=1, denominator=1, new_lot_id='bonus-1')]
        self.spec['closing']['lots'] = [copy.deepcopy(self.row), {**self.row, 'lot_id': 'bonus-1', 'bucket': 'ENTITLEMENT', 'cost_basis_inr': '0'}]
        result = self.run_case(); self.assertEqual(result['buckets']['SETTLED']['cost_basis_inr'], '1000')
        self.assertEqual(result['lots'][0]['bucket'], 'ENTITLEMENT')

    def test_adjustment_keeps_before_and_after_evidence(self):
        replacement = {**self.row, 'lot_id': 'corrected', 'quantity': 8, 'cost_basis_inr': '800'}
        self.spec['events'] = [self.event('adjustment', reason='Sourced settlement correction', replacement_lots=[replacement])]
        self.spec['closing']['lots'] = [replacement]
        report = self.run_case(); self.assertEqual(report['audit'][0]['before']['quantity'], 10)
        self.spec['events'][0]['reason'] = ''
        with self.assertRaises(Block): self.run_case()

    def test_malformed_lots_and_funding_fail(self):
        for changes in ({'quantity': True}, {'quantity': -1}, {'quantity': 1.5}, {'cost_basis_inr': 'NaN'},
                        {'principal_inr': '1'}, {'bucket': 'MTF'}, {'isin': 'UNKNOWN'}, {'exchange': 'NFO'}):
            with self.subTest(changes=changes), self.assertRaises(Block): lot({**self.row, **changes})

    def test_same_id_cannot_be_reused_and_overtransfer_blocks(self):
        self.transfer(q=11)
        with self.assertRaises(Block): self.run_case()
        self.transfer(); self.spec['events'][0]['new_lot_id'] = 'lot-1'
        with self.assertRaises(Block): self.run_case()

    def test_account_clock_unknown_fields_and_duplicate_events_block(self):
        self.transfer(); self.spec['closing']['lots'] = [{**self.row, 'lot_id': 'lot-2', 'bucket': 'SETTLED'}]
        original = copy.deepcopy(self.spec)
        for changes in ({'account_id': 'OTHER'}, {'effective_at': '2026-11-13T11:00:00+05:30'},
                        {'expected_lot_hash': 'wrong'}, {'guess': True}):
            self.spec = copy.deepcopy(original); self.spec['events'][0].update(changes)
            with self.assertRaises(Block): self.run_case()
        self.spec = copy.deepcopy(original); self.spec['events'] *= 2
        with self.assertRaises(Block): self.run_case()

    def test_stale_incomplete_and_trade_activity_block(self):
        original = copy.deepcopy(self.spec)
        for changes in ({'trading_activity': True}, {'reviewed': False}, {'complete': False}, {'as_of': '2026-11-12T10:00:00+05:30'}):
            self.spec = copy.deepcopy(original); self.spec.update(changes)
            with self.assertRaises(Block): self.run_case()

    def test_hash_and_path_tampering_block(self):
        original = copy.deepcopy(self.spec)
        for changes in ({'sha256': '0' * 64}, {'path': '../outside.txt'}, {'path': str(self.root / 'statement.txt')}):
            self.spec = copy.deepcopy(original); self.spec['artifacts']['statement'].update(changes)
            with self.assertRaises(Block): self.run_case()

    def test_numeric_spellings_canonicalize(self):
        self.spec['closing']['lots'][0].update(cost_basis_inr='1000.00', principal_inr='0.00', quantity='10')
        self.assertEqual(self.run_case()['status'], 'RECONCILED')

    def test_durable_duplicate_and_conflict_rollback(self):
        self.transfer(); self.spec['closing']['lots'] = [{**self.row, 'lot_id': 'lot-2', 'bucket': 'SETTLED'}]
        report = self.run_case(); db = sqlite3.connect(':memory:'); self.addCleanup(db.close)
        record(db, self.spec, report); record(db, self.spec, report)
        self.assertEqual(db.execute('SELECT count(*) FROM inventory_event_evidence').fetchone()[0], 1)
        changed = copy.deepcopy(self.spec); changed['events'][0]['quantity'] = 5
        new_event = {**changed['events'][0], 'event_id': 'e2'}
        changed['events'].insert(0, new_event)
        with self.assertRaises(Block): record(db, changed, {**report, 'input_hash': fingerprint(changed)})
        self.assertEqual(db.execute('SELECT count(*) FROM inventory_event_evidence').fetchone()[0], 1)
        self.assertEqual(db.execute('SELECT count(*) FROM inventory_reports').fetchone()[0], 1)

    def test_cli_offline_and_preserves_report(self):
        source, out, db = (self.root / name for name in ('input.json', 'report.json', 'journal.sqlite'))
        source.write_text(json.dumps(self.spec))
        args = ['--input', str(source), '--out', str(out), '--db', str(db), '--now', NOW.isoformat()]
        with patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(args), 0); original = out.read_bytes()
            self.assertEqual(main(args), 2); self.assertEqual(out.read_bytes(), original)
        self.assertFalse(json.loads(original)['execution_eligible'])


if __name__ == '__main__': unittest.main()
