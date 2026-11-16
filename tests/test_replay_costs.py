"""Quantity/cost sensitivity uses fictional data and explicit assumptions."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from core import Block, charges, entry_context
from replay_costs import policy, summarize
from replay import replay
import test_core


class ReplayCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_core.CoreTests.setUpClass()

    def setUp(self):
        self.config = copy.deepcopy(test_core.CoreTests.config)
        self.now, self.instrument, self.context, self.history, _, _ = copy.deepcopy(test_core.CoreTests.base)
        self.plan = copy.deepcopy(test_core.CoreTests.plan)
        self.card = copy.deepcopy(test_core.CoreTests.card)
        self.config['replay_paths'] = 20

    def test_default_keeps_original_cost_report(self):
        self.assertIsNone(policy(self.config, self.instrument))
        result = replay(self.plan, self.instrument, self.history, self.config, self.card)
        self.assertNotIn('cost_sensitivity', result)
        self.assertTrue(result['costs']['reprice_after_sizing'])

    def test_caps_are_recomputed_not_multiplied_from_one_unit(self):
        self.config['replay_cost_quantities'] = [1, 10000]
        selected = policy(self.config, self.instrument)
        result = summarize(self.plan, self.instrument, self.card, selected, {110: 2, 98: 1})
        for row in result['scenarios']:
            q = row['quantity']
            expected = (2 * charges(self.plan['entry'], 110, q, 'BUY', self.instrument, self.card, self.plan['as_of'][:10])['total'] +
                        charges(self.plan['entry'], 98, q, 'BUY', self.instrument, self.card, self.plan['as_of'][:10])['total']) / 3
            self.assertAlmostEqual(row['average_base_fees_inr'], round(expected, 2), places=2)
        small, large = result['scenarios']
        self.assertNotEqual(round(small['average_base_fees_inr'] * 10000, 2), large['average_base_fees_inr'])

    def test_costs_are_computed_inside_replay_without_changing_price_paths(self):
        plain = replay(self.plan, self.instrument, self.history, self.config, self.card)
        self.config['replay_cost_quantities'] = [1, 20, 100]
        stressed = replay(self.plan, self.instrument, self.history, self.config, self.card)
        self.assertEqual([r['quantity'] for r in stressed['cost_sensitivity']['scenarios']], [1, 20, 100])
        for field in ('target_first_pct', 'stop_first_pct', 'still_open_pct', 'costs'):
            self.assertEqual(stressed[field], plain[field])
        self.assertFalse(stressed['cost_sensitivity']['execution_eligible'])

    def test_adverse_cost_increases_with_quantity_for_both_sides(self):
        self.config.update(replay_cost_quantities=[10, 100], replay_cost_stress={
            'mode': 'linear_impact_v1', 'latency_bps': 2, 'impact_bps_at_reference': 5, 'reference_quantity': 100})
        selected = policy(self.config, self.instrument)
        for side in ('BUY', 'SELL'):
            rows = summarize({**self.plan, 'side': side}, self.instrument, self.card, selected, {100: 1})['scenarios']
            self.assertLess(rows[0]['adverse_shift_bps_per_leg'], rows[1]['adverse_shift_bps_per_leg'])
            for row in rows:
                self.assertGreater(row['average_adverse_price_cost_inr'], 0)
                self.assertLess(row['average_net_mark_to_exit_inr'], row['average_gross_mark_to_exit_inr'])

    def test_invalid_grids_block(self):
        for quantities in ([], [0], [-1], [True], [1.5], ['1'], [1, 1], list(range(1, 12)), [1000001]):
            with self.subTest(quantities=quantities), self.assertRaises(Block):
                policy({**self.config, 'replay_cost_quantities': quantities}, self.instrument)

    def test_lots_and_freeze_quantity_are_enforced(self):
        i = {**self.instrument, 'lot_size': 5, 'freeze_quantity': 20}
        for quantities in ([1], [25]):
            with self.assertRaises(Block): policy({'replay_cost_quantities': quantities}, i)
        self.assertEqual(policy({'replay_cost_quantities': [5, 20]}, i)['quantities'], [5, 20])

    def test_unsupported_and_extreme_models_block(self):
        model = {'mode': 'linear_impact_v1', 'latency_bps': 2, 'impact_bps_at_reference': 5, 'reference_quantity': 100}
        for changes in ({'mode': 'guaranteed'}, {'latency_bps': -1}, {'latency_bps': float('nan')},
                        {'reference_quantity': 0}, {'impact_bps_at_reference': 1000000}, {'unknown': 1}):
            with self.subTest(changes=changes), self.assertRaises(Block):
                policy({'replay_cost_quantities': [100], 'replay_cost_stress': {**model, **changes}}, self.instrument)

    def test_changed_cost_policy_requires_regenerated_replay(self):
        self.config['replay_cost_quantities'] = [1, 10]
        self.context['replay'] = replay(self.plan, self.instrument, self.history, self.config, self.card)
        entry_context(self.plan, self.instrument, self.config, self.context, self.now)
        self.config['replay_cost_quantities'] = [1, 20]
        with self.assertRaisesRegex(Block, 'cost sensitivity policy changed'):
            entry_context(self.plan, self.instrument, self.config, self.context, self.now)

    def test_removed_cost_policy_requires_regenerated_replay(self):
        self.config['replay_cost_quantities'] = [1, 10]
        self.context['replay'] = replay(self.plan, self.instrument, self.history, self.config, self.card)
        del self.config['replay_cost_quantities']
        with self.assertRaisesRegex(Block, 'cost sensitivity policy changed'):
            entry_context(self.plan, self.instrument, self.config, self.context, self.now)

    def test_report_does_not_alias_mutable_configuration(self):
        self.config['replay_cost_quantities'] = [1, 10]
        self.context['replay'] = replay(self.plan, self.instrument, self.history, self.config, self.card)
        self.config['replay_cost_quantities'].append(20)
        with self.assertRaisesRegex(Block, 'cost sensitivity policy changed'):
            entry_context(self.plan, self.instrument, self.config, self.context, self.now)


if __name__ == '__main__': unittest.main()
