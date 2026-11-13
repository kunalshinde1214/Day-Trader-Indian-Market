"""Protection diagnostics use simulated normalized books; no broker calls."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from live import protection


class ProtectionStatusTests(unittest.TestCase):
    def setUp(self):
        self.position = dict(exchange='NSE', symbol='TEST', token='1', product='MIS', quantity=10)
        self.stop = dict(self.position, order_id='S1', side='SELL', filled_quantity=0,
                         status='OPEN', raw_status='TRIGGER PENDING', order_type='SL')

    def report(self, *orders, position=None):
        return protection({'positions': [position or self.position], 'orders': list(orders)})[0]

    def test_exact_product_coverage_only(self):
        other = dict(self.stop, product='CNC')
        self.assertEqual(self.report(other)['uncovered_quantity'], 10)
        self.assertEqual(self.report(self.stop, other)['state'], 'STOP_WAITING')

    def test_independent_target_and_stop_can_oversell(self):
        target = dict(self.stop, order_id='T1', order_type='LIMIT', raw_status='OPEN')
        report = self.report(self.stop, target)
        self.assertEqual(report['state'], 'OVER_EXIT_RISK')
        self.assertEqual(report['excess_exit_quantity'], 10)

    def test_partial_fill_counts_only_remaining_units(self):
        stop = dict(self.stop, status='PARTIAL', filled_quantity=4)
        report = self.report(stop, position=dict(self.position, quantity=6))
        self.assertEqual(report['state'], 'STOP_WAITING')
        self.assertEqual(report['working_exit_quantity'], 6)
        self.assertEqual(report['uncovered_quantity'], 0)

    def test_pending_entry_invalidates_stable_coverage(self):
        entry = dict(self.stop, order_id='E1', side='BUY', quantity=7, filled_quantity=2,
                     order_type='LIMIT', status='PARTIAL', raw_status='OPEN')
        report = self.report(self.stop, entry)
        self.assertEqual(report['state'], 'POSITION_CAN_INCREASE')
        self.assertEqual(report['working_entry_quantity'], 5)

    def test_unknown_or_triggered_order_is_not_waiting_protection(self):
        self.assertEqual(self.report(dict(self.stop, status='UNKNOWN'))['state'], 'ORDER_STATE_UNVERIFIED')
        report = self.report(dict(self.stop, raw_status='OPEN'))
        self.assertEqual(report['state'], 'UNPROTECTED_OR_UNVERIFIED')
        self.assertEqual(report['waiting_stop_quantity_verified'], 0)

    def test_short_and_terminal_orders(self):
        short = dict(self.position, quantity=-10)
        buy = dict(self.stop, side='BUY')
        cancelled = dict(buy, order_id='OLD', status='CANCELLED')
        report = self.report(buy, cancelled, position=short)
        self.assertEqual(report['state'], 'STOP_WAITING')
        self.assertEqual(report['working_exit_quantity'], 10)

    def test_same_token_other_exchange_or_symbol_is_not_coverage(self):
        for field, value in [('exchange', 'BSE'), ('symbol', 'OTHER')]:
            with self.subTest(field=field):
                self.assertEqual(self.report(dict(self.stop, **{field: value}))['uncovered_quantity'], 10)

    def test_undercoverage_and_input_immutability(self):
        before = copy.deepcopy(self.stop)
        report = self.report(dict(self.stop, quantity=3))
        self.assertEqual(report['uncovered_quantity'], 7)
        self.assertEqual(report['state'], 'UNPROTECTED_OR_UNVERIFIED')
        self.assertEqual(self.stop, before)


if __name__ == '__main__':
    unittest.main()
