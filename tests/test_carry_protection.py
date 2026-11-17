"""Portfolio-aware protection decisions with synthetic reconciled inventory."""
import copy
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from core import Block
from protection import decide
from portfolio_exit import reconcile
import test_portfolio_exit as fixtures
NOW = fixtures.NOW


class CarryProtectionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MixedExitTests(); self.f.setUp()
        self.f.c.update(flat_by='15:05', snapshot_max_age_seconds=30)
        i = self.f.i
        self.policy = {**{k: i[k] for k in ('broker', 'exchange', 'symbol', 'token')},
            'schema': 'portfolio_protection_policy_v1', 'policy_id': 'carry-test', 'account_id': 'DEMO',
            'product': 'CNC', 'date': NOW.date().isoformat(), 'entry_side': 'BUY', 'max_quantity': 10,
            'stop_trigger': 110, 'stop_limit': 109, 'target': 140, 'exit_limit': 100, 'flat_by': '15:05'}
        quote = {**{k: i[k] for k in ('exchange', 'symbol', 'token')}, 'source': 'kite', 'price': 130, 'as_of': NOW.isoformat()}
        session = {**{k: i[k] for k in ('broker', 'exchange', 'symbol', 'token')}, 'account_id': 'DEMO',
            'date': NOW.date().isoformat(), 'as_of': NOW.isoformat(), 'is_open_day': True, 'halted': False,
            'source': 'https://www.nseindia.com/static/market-data/market-timings', 'auction_included': False,
            'continuous_open': '09:15', 'continuous_close': '15:30', 'broker_squareoff': '15:20'}
        self.bundle = {'instrument': i, 'config': self.f.c, 'context': {'quote': quote, 'instrument_session': session}}
        self.collection = self.f.collection()
        self.portfolio = {'collection': self.collection, 'instruments': self.f.master, 'delivery': self.f.delivery}

    def run_case(self, state=None, intents=()):
        snap, _ = reconcile(self.collection, self.f.master, NOW)
        return decide(self.bundle, snap, self.f.profile, self.policy, NOW, state, intents, self.portfolio)

    def test_stop_preserves_delivery_product_and_quantity(self):
        result = self.run_case()
        self.assertEqual(result['status'], 'REVIEW_STOP')
        self.assertEqual(result['review']['payload']['product'], 'CNC')
        self.assertEqual(result['review']['action']['quantity'], 10)
        self.assertFalse(result['submitted'])

    def test_hit_target_latches_exit_after_price_reversal(self):
        self.bundle['context']['quote']['price'] = 145
        result = self.run_case(); self.assertEqual(result['status'], 'REVIEW_EXIT')
        self.bundle['context']['quote']['price'] = 130
        self.assertEqual(self.run_case(result['next_state'])['status'], 'REVIEW_EXIT')

    def test_cancel_then_recompute_instead_of_double_sell(self):
        self.f.working(self.collection)
        result = self.run_case()
        self.assertEqual(result['status'], 'CANCEL_THEN_REFRESH')
        self.assertEqual(result['review']['action']['product'], 'CNC')
        self.assertEqual(result['review']['action']['order_id'], 'OPEN1')

    def test_intraday_working_order_does_not_cancel_carry_protection(self):
        self.f.working(self.collection, product='MIS')
        self.collection['snapshot']['orders'][0]['side'] = 'BUY'
        self.collection['execution_orders'][0]['side'] = 'BUY'
        result = self.run_case()
        self.assertEqual(result['status'], 'REVIEW_STOP')
        self.assertEqual(result['review']['action']['product'], 'CNC')

    def test_delivery_session_and_unresolved_intents_block(self):
        for changes in ({'authorized_quantity': 9}, {'status': 'UNKNOWN'}):
            saved = copy.deepcopy(self.portfolio['delivery']); self.portfolio['delivery'].update(changes)
            with self.assertRaises(Block): self.run_case()
            self.portfolio['delivery'] = saved
        with self.assertRaises(Block): self.run_case(intents=[{'state': 'UNKNOWN'}])
        self.bundle['context']['instrument_session']['halted'] = True
        with self.assertRaises(Block): self.run_case()

    def test_intraday_schema_cannot_silently_enable_carry(self):
        self.policy['schema'] = 'protection_policy_v1'
        with self.assertRaises(Block): self.run_case()

    def test_snapshot_must_equal_reconciled_collection(self):
        snap, _ = reconcile(self.collection, self.f.master, NOW)
        snap['positions'][0]['quantity'] = 99
        with self.assertRaises(Block):
            decide(self.bundle, snap, self.f.profile, self.policy, NOW, portfolio=self.portfolio)


if __name__ == '__main__': unittest.main()
