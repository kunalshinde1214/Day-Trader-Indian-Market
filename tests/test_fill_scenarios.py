import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Block, charges
from demo import inputs
from fill_scenarios import simulate

ROOT = Path(__file__).resolve().parents[1]


class FillScenarioTests(unittest.TestCase):
    def setUp(self):
        config = json.loads((ROOT/'assets/config.example.json').read_text())
        self.now, instrument, *_ = inputs(config)
        instrument['freeze_quantity'] = 10000
        self.spec = {'schema': 'passive_fill_scenarios_v1', 'instrument': instrument, 'side': 'BUY',
            'limit_price': 100, 'hypothetical_exit_price': 102, 'quantity': 10,
            'charges': json.loads((ROOT/'assets/charges.example.json').read_text()),
            'scenarios': [{'name': 'queue', 'ahead_units_at_arrival': 5, 'latency_ms': 100, 'cancel_after_ms': 1000,
                          'eligible_flow': [{'elapsed_ms': 50, 'units': 100}, {'elapsed_ms': 100, 'units': 7},
                                            {'elapsed_ms': 900, 'units': 3}, {'elapsed_ms': 1000, 'units': 100}]}]}

    def run_case(self): return simulate(self.spec, self.now)

    def test_latency_queue_partial_and_cancel_boundary(self):
        report = self.run_case(); row = report['scenarios'][0]
        self.assertEqual((row['filled_units'], row['unfilled_units']), (5, 5))
        self.assertEqual([r['units'] for r in row['fill_slices']], [2, 3])
        fees = charges(100, 102, 5, 'BUY', self.spec['instrument'], self.spec['charges'], self.now.date().isoformat())
        self.assertEqual(row['hypothetical_round_trip_charges'], fees)
        self.assertAlmostEqual(row['hypothetical_net_inr'], 10 - fees['total'])
        self.assertFalse(report['execution_eligible'])

    def test_zero_fills_have_no_trade_fees(self):
        self.spec['scenarios'][0]['ahead_units_at_arrival'] = 1000
        row = self.run_case()['scenarios'][0]
        self.assertEqual(row['filled_units'], 0); self.assertIsNone(row['hypothetical_round_trip_charges'])
        self.assertEqual(row['hypothetical_net_inr'], 0)

    def test_volume_cannot_fill_more_than_order(self):
        self.spec['scenarios'][0]['eligible_flow'] = [{'elapsed_ms': 100, 'units': 100}]
        self.assertEqual(self.run_case()['scenarios'][0]['filled_units'], 10)

    def test_short_sign_and_nonfinite_invalid_flow(self):
        self.spec['side'] = 'SELL'
        self.assertLess(self.run_case()['scenarios'][0]['hypothetical_net_inr'], -10)
        self.spec['scenarios'][0]['eligible_flow'][0]['units'] = 'NaN'
        with self.assertRaises(Block): self.run_case()

    def test_unordered_time_duplicate_names_derivatives_block(self):
        original = copy.deepcopy(self.spec)
        self.spec['scenarios'][0]['eligible_flow'].reverse()
        with self.assertRaises(Block): self.run_case()
        self.spec = copy.deepcopy(original); self.spec['scenarios'] *= 2
        with self.assertRaises(Block): self.run_case()
        self.spec = original; self.spec['instrument']['kind'] = 'future'
        with self.assertRaises(Block): self.run_case()


if __name__ == '__main__': unittest.main()
