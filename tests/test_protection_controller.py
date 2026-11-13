"""Fault cases for protection decisions; all broker observations are simulated."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from core import Block
from live_state import normalize, state_hash
from protection import decide, preview
from execution import execute
from test_execution import sample, NOW


class ProtectionControllerTests(unittest.TestCase):
    def setUp(self):
        self.i, raw = sample(filled=10, status='COMPLETE')
        self.s = normalize(raw, [self.i], NOW)
        self.c = dict(broker='kite', account_id='123456', flat_by='15:05', snapshot_max_age_seconds=30)
        self.p = dict(self.c, enabled=True, approval_mode='human_terminal_two_stage')
        ident = {k: self.i[k] for k in ('broker', 'exchange', 'symbol', 'token')}
        session = dict(ident, account_id='123456', date=NOW.date().isoformat(), as_of=NOW.isoformat(),
                       source='https://www.nseindia.com/static/market-data/market-timings', is_open_day=True,
                       halted=False, continuous_open='09:15', continuous_close='15:30', broker_squareoff='15:20', auction_included=False)
        quote = dict(ident, source='kite', price=105, as_of=NOW.isoformat())
        self.b = dict(config=self.c, instrument=self.i, context=dict(quote=quote, instrument_session=session), charges={})
        self.policy = dict(ident, schema='protection_policy_v1', policy_id='offline-1', product='MIS',
                           account_id='123456', date=NOW.date().isoformat(), entry_side='BUY', max_quantity=10,
                           stop_trigger=100, stop_limit=99, target=110, exit_limit=99, flat_by='15:05')

    def run_decision(self, state=None, intents=()):
        self.s['state_hash'] = state_hash(self.s)
        return decide(self.b, self.s, self.p, self.policy, NOW, state, intents)

    def stop(self, **updates):
        row = dict(self.s['orders'][0], order_id='S1', side='SELL', status='OPEN', raw_status='TRIGGER PENDING',
                   filled_quantity=0, order_type='SL', trigger_price=100, price=99)
        row.update(updates)
        self.s['orders'].append(row)
        return row

    def test_unprotected_position_proposes_validated_stop(self):
        result = self.run_decision()
        self.assertEqual(result['status'], 'REVIEW_STOP')
        self.assertEqual(result['review']['payload']['order_type'], 'SL')
        self.assertEqual(result['next_action']['quantity'], 10)
        self.assertFalse(result['submitted'])

    def test_exact_waiting_stop_is_kept_without_duplicate(self):
        self.stop()
        result = self.run_decision()
        self.assertEqual(result['status'], 'STOP_WAITING')
        self.assertIsNone(result['review'])

    def test_target_cancels_then_exits_even_after_price_reverses(self):
        row = self.stop()
        self.b['context']['quote']['price'] = 111
        first = self.run_decision()
        self.assertEqual(first['next_action'], dict(kind='cancel', order_id='S1'))
        row['status'] = 'CANCELLED'
        self.b['context']['quote']['price'] = 105
        second = self.run_decision(first['next_state'])
        self.assertEqual(second['status'], 'REVIEW_EXIT')
        self.assertEqual(second['reason'], 'TARGET_REACHED')

    def test_partial_fill_during_cancel_uses_remaining_position(self):
        row = self.stop()
        self.b['context']['quote']['price'] = 111
        first = self.run_decision()
        row.update(status='CANCELLED', filled_quantity=3)
        self.s['positions'][0]['quantity'] = 7
        second = self.run_decision(first['next_state'])
        self.assertEqual(second['next_action']['quantity'], 7)

    def test_cancel_pending_and_ambiguous_intent_block(self):
        row = self.stop(status='UNKNOWN')
        with self.assertRaises(Block): self.run_decision()
        row['status'] = 'OPEN'
        for status in ('UNKNOWN', 'SUBMITTING', 'ACKNOWLEDGED'):
            with self.subTest(status=status), self.assertRaisesRegex(Block, 'unresolved intent'):
                self.run_decision(intents=[{'state': status}])

    def test_partial_entry_is_cancelled_before_stop(self):
        self.s['orders'][0].update(status='PARTIAL', filled_quantity=4)
        self.s['positions'][0]['quantity'] = 4
        result = self.run_decision()
        self.assertEqual(result['next_action'], dict(kind='cancel', order_id='O1'))

    def test_stop_reached_proposes_limit_exit(self):
        self.b['context']['quote']['price'] = 98
        result = self.run_decision()
        self.assertEqual(result['reason'], 'STOP_REACHED')
        self.assertEqual(result['review']['payload']['order_type'], 'LIMIT')
        self.assertEqual(result['next_action']['price'], 99)

    def test_cutoff_is_inclusive_and_requires_session_evidence(self):
        self.policy['flat_by'] = self.c['flat_by'] = '09:35'
        result = self.run_decision()
        self.assertEqual(result['reason'], 'FLAT_BY')
        self.b['context']['instrument_session']['broker_squareoff'] = '09:35'
        with self.assertRaises(Block): self.run_decision()

    def test_pending_limit_exit_is_not_repeated(self):
        self.b['context']['quote']['price'] = 98
        first = self.run_decision()
        self.stop(order_type='LIMIT', raw_status='OPEN', trigger_price=0)
        result = self.run_decision(first['next_state'])
        self.assertEqual(result['status'], 'WAIT_EXIT_FILL')
        self.assertIsNone(result['next_action'])

    def test_completed_policy_cannot_manage_new_exposure(self):
        row = self.stop(status='FILLED', filled_quantity=10)
        self.s['positions'][0]['quantity'] = 0
        first = self.run_decision()
        self.assertEqual(first['status'], 'FLAT_OBSERVED')
        row.update(status='CANCELLED', filled_quantity=0)
        self.s['positions'][0]['quantity'] = 10
        with self.assertRaisesRegex(Block, 'reappeared'): self.run_decision(first['next_state'])

    def test_stale_quote_account_and_product_block(self):
        original = copy.deepcopy(self.b)
        self.b['context']['quote']['as_of'] = '2026-10-30T09:00:00+05:30'
        with self.assertRaises(Block): self.run_decision()
        self.b = original
        self.policy['account_id'] = 'other'
        with self.assertRaises(Block): self.run_decision()
        self.policy['account_id'] = '123456'
        self.s['positions'][0]['product'] = 'CNC'
        with self.assertRaises(Block): self.run_decision()

    def test_policy_limits_and_direction_block(self):
        original = copy.deepcopy(self.policy)
        for change in ({'max_quantity': 5}, {'entry_side': 'SELL'}, {'stop_limit': 101}, {'target': 99}, {'stop_limit': 99.001}):
            self.policy = {**original, **change}
            with self.subTest(change=change), self.assertRaises(Block): self.run_decision()

    def test_short_position_protects_with_buy_and_exits_above_stop(self):
        self.s['orders'][0]['side'] = 'SELL'
        self.s['positions'][0]['quantity'] = -10
        self.policy.update(entry_side='SELL', stop_trigger=110, stop_limit=111, target=100, exit_limit=112)
        self.assertEqual(self.run_decision()['review']['action']['side'], 'BUY')
        self.b['context']['quote']['price'] = 111
        self.assertEqual(self.run_decision()['reason'], 'STOP_REACHED')

    def test_latch_survives_database_reopen_and_policy_edits_block(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'protection.sqlite'
            self.b['context']['quote']['price'] = 111
            first = preview(path, self.b, self.s, self.p, self.policy, NOW)
            self.b['context']['quote']['price'] = 105
            second = preview(path, self.b, self.s, self.p, self.policy, NOW)
            self.assertEqual(second['reason'], first['reason'])
            self.assertEqual(second['status'], 'REVIEW_EXIT')
            self.policy['target'] = 112
            with self.assertRaisesRegex(Block, 'changed policy'):
                preview(path, self.b, self.s, self.p, self.policy, NOW)

    def test_live_preview_fetches_and_never_constructs_transport(self):
        from live import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'live-profile.json').write_text(json.dumps(self.p))
            (root / 'protection-policy.json').write_text(json.dumps(self.policy))
            with patch('live.load_bundle', return_value=self.b), patch('live.adapter', return_value=Mock()), \
                 patch('live.normalize_quote', return_value=self.b['context']['quote']), \
                 patch('live.snapshot', return_value=self.s), patch('live.instant', return_value=NOW), \
                 patch('live.LiveBroker') as transport, patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(main(['protection-preview', '--workdir', directory]), 0)
                transport.assert_not_called()
                self.assertFalse(json.loads(output.getvalue())['submitted'])

    def test_human_step_refreshes_and_sends_one_fake_action(self):
        from live import main
        transport = Mock()
        transport.send.return_value = {'order_id': 'S1'}
        approval = Mock()
        approval.approve.side_effect = lambda review: review['review_hash']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'live-profile.json').write_text(json.dumps(self.p))
            (root / 'protection-policy.json').write_text(json.dumps(self.policy))
            with patch('live.load_bundle', return_value=self.b), patch('live.adapter', return_value=Mock()), \
                 patch('live.normalize_quote', return_value=self.b['context']['quote']), \
                 patch('live.snapshot', return_value=self.s) as snapshots, patch('live.instant', return_value=NOW), \
                 patch('live.LiveBroker', return_value=transport), patch('live.TerminalApproval', return_value=approval), \
                 patch('live.execute', side_effect=lambda *args: execute(*args, clock=lambda: NOW)), \
                 patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(main(['protection-step', '--workdir', directory]), 0)
                self.assertEqual(snapshots.call_count, 3)
                approval.approve.assert_called_once()
                transport.send.assert_called_once()
                self.assertTrue(json.loads(output.getvalue())['submitted'])

    def test_all_five_broker_stop_payloads(self):
        statuses = {'kite': 'COMPLETE', 'upstox': 'complete', 'dhan': 'TRADED', 'angel': 'complete', 'fyers': 2}
        for broker, status in statuses.items():
            with self.subTest(broker=broker):
                self.setUp()
                self.i, raw = sample(broker, filled=10, status=status)
                self.s = normalize(raw, [self.i], NOW)
                self.b['instrument'] = self.i
                self.c['broker'] = self.p['broker'] = broker
                for item in (self.policy, self.b['context']['quote'], self.b['context']['instrument_session']):
                    item.update({k: self.i[k] for k in ('broker', 'exchange', 'symbol', 'token')})
                self.b['context']['quote']['source'] = broker
                result = self.run_decision()
                self.assertEqual(result['status'], 'REVIEW_STOP')
                self.assertEqual(result['review']['action']['broker'], broker)


if __name__ == '__main__':
    unittest.main()
