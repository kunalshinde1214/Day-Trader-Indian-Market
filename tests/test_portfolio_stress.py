import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Block, instant
from portfolio_stress import evaluate

NOW = instant('2026-11-16T10:00:00+05:30')


class StressTests(unittest.TestCase):
    def setUp(self):
        asset = {'id': 'asset', 'exchange': 'NSE', 'token': '123', 'quantity': 10, 'mark_price': 100, 'price_basis': 'per_unit_inr'}
        account = {'id': 'one', 'broker': 'kite', 'account_id': 'test', 'source': 'offline fixture', 'as_of': NOW.isoformat(),
            'cash_balance_inr': 100, 'reserved_cash_inr': 0, 'account_margin_required_inr': 500,
            'cash_component_required_inr': 100, 'positions': [asset], 'collateral': [{**asset, 'haircut': .2}]}
        scenario = {'position_prices': {'asset': 90}, 'collateral_prices': {'asset': 90},
            'collateral_haircuts': {'asset': .5}, 'margin_multiplier': 1.2, 'cash_component_multiplier': 1,
            'cash_debit_inr': 0}
        self.spec = {'schema': 'portfolio_stress_v1', 'accounts': [account], 'scenarios': [{'name': 'shock', 'accounts': {'one': scenario}}]}

    def run_case(self): return evaluate(self.spec, NOW)

    def test_loss_haircut_and_cash_floor_without_double_counting(self):
        row = self.run_case()['scenarios'][0]['accounts'][0]
        self.assertEqual(row['mark_to_market_change_inr'], -100)
        self.assertEqual(row['collateral_after_haircut_inr'], 450)
        self.assertEqual(row['total_shortfall_inr'], 150)
        self.assertEqual(row['cash_component_shortfall_inr'], 100)
        self.assertEqual(row['additional_cash_needed_inr'], 150)

    def test_gains_do_not_become_cash(self):
        self.spec['scenarios'][0]['accounts']['one']['position_prices']['asset'] = 110
        self.assertEqual(self.run_case()['scenarios'][0]['accounts'][0]['stressed_cash_inr'], 100)

    def test_short_positions_lose_when_price_rises(self):
        self.spec['accounts'][0]['positions'][0]['quantity'] = -10
        self.spec['scenarios'][0]['accounts']['one']['position_prices']['asset'] = 110
        self.assertEqual(self.run_case()['scenarios'][0]['accounts'][0]['mark_to_market_change_inr'], -100)

    def test_other_account_surplus_cannot_cover_first(self):
        other = copy.deepcopy(self.spec['accounts'][0]); other.update(id='two', account_id='other', cash_balance_inr=10000)
        self.spec['accounts'].append(other)
        self.spec['scenarios'][0]['accounts']['two'] = copy.deepcopy(self.spec['scenarios'][0]['accounts']['one'])
        self.assertEqual(self.run_case()['scenarios'][0]['additional_cash_needed_inr'], 150)

    def test_missing_prices_bad_haircuts_and_stale_sources_block(self):
        original = copy.deepcopy(self.spec)
        for field, value in [('position_prices', {}), ('collateral_haircuts', {'asset': 1.1}), ('margin_multiplier', 'NaN')]:
            self.spec = copy.deepcopy(original); self.spec['scenarios'][0]['accounts']['one'][field] = value
            with self.assertRaises(Block): self.run_case()
        self.spec = original; self.spec['accounts'][0]['as_of'] = '2026-11-15T10:00:00+05:30'
        with self.assertRaises(Block): self.run_case()

    def test_duplicate_accounts_and_unknown_units_block(self):
        self.spec['accounts'].append(copy.deepcopy(self.spec['accounts'][0]))
        with self.assertRaises(Block): self.run_case()
        self.spec['accounts'].pop(); self.spec['accounts'][0]['positions'][0]['price_basis'] = 'lots'
        with self.assertRaises(Block): self.run_case()


if __name__ == '__main__': unittest.main()
