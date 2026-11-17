"""Dhan multi-margin contracts; offline observations, never orders."""
import copy
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from core import Block
from portfolio_margin import quote
from test_execution import sample, NOW


class DhanBasketTests(unittest.TestCase):
    def setUp(self):
        i, _ = sample('dhan')
        self.bundle = {'broker': 'dhan', 'account_id': '123456', 'legs': [
            {'instrument': {**i, 'token': str(n), 'symbol': 'TEST' + str(n)}, 'account_id': '123456',
             'product': 'CNC', 'side': 'BUY', 'quantity': 10, 'price': 100} for n in (100, 200)]}
        self.client = Mock()
        self.account = {'broker': 'dhan', 'source': 'dhan', 'account_id': '123456', 'as_of': NOW.isoformat(),
                        'positions': [], 'orders': [], 'margins': {'availabelBalance': 10000}}
        self.client.account.return_value = self.account
        self.response = {'currency': 'INR', 'total_margin': '3000', 'span_margin': '0', 'exposure_margin': '0',
                         'equity_margin': '3000', 'fo_margin': '0', 'commodity_margin': '0', 'hedge_benefit': ''}
        self.client.request.return_value = self.response

    def run_case(self): return quote(self.client, self.bundle, clock=lambda: NOW)

    def test_exact_sdk_contract_and_grid(self):
        self.bundle['fill_grid'] = [[0, 5, 10], [0, 10]]
        result = self.run_case()
        self.assertEqual(self.client.request.call_count, 7)
        call = self.client.request.call_args
        self.assertEqual(call.args, ('POST', '/margincalculator/multi'))
        self.assertEqual(call.kwargs['body'], {'dhanClientId': '123456', 'includePosition': True, 'includeOrder': True, 'scripList': [
            {'exchangeSegment': 'NSE_EQ', 'securityId': str(n), 'transactionType': 'BUY', 'quantity': 10,
             'productType': 'CNC', 'price': 100, 'triggerPrice': 0} for n in (100, 200)]})
        self.assertEqual(result['peak_account_total_inr'], 3000)
        self.assertIsNone(result['final_estimate_inr'])
        self.assertFalse(result['execution_eligible'])
        self.assertFalse(result['prefix_estimates'][0]['response_identity_verified'])

    def test_derivative_units_and_product(self):
        leg = self.bundle['legs'][0]
        self.bundle['legs'] = [leg]
        leg.update(product='NRML', side='SELL')
        leg['instrument'].update(kind='future', exchange='NFO', lot_size=5, expiry='2026-11-26', underlying_type='index')
        self.run_case()
        row = self.client.request.call_args.kwargs['body']['scripList'][0]
        self.assertEqual((row['exchangeSegment'], row['productType'], row['quantity']), ('NSE_FNO', 'MARGIN', 10))

    def test_unknown_contract_or_invalid_money_never_falls_back(self):
        for changed in ({'currency': 'USD'}, {'total_margin': -1}, {'total_margin': True},
                        {'total_margin': 'NaN'}, {'span_margin': -1}):
            self.client.request.return_value = {**self.response, **changed}
            with self.subTest(changed=changed), self.assertRaises(Block): self.run_case()
        self.client.request.return_value = {'totalMargin': 3000}
        with self.assertRaises(Block): self.run_case()

    def test_changed_books_abort_and_do_not_submit(self):
        changed = copy.deepcopy(self.account); changed['orders'] = [{'order_id': 'new'}]
        self.client.account.side_effect = [self.account, changed]
        with self.assertRaisesRegex(Block, 'books changed'): self.run_case()
        self.assertTrue(all(c.args[1] == '/margincalculator/multi' for c in self.client.request.call_args_list))

    def test_wrong_account_or_invalid_token_blocks_before_margin(self):
        self.bundle['legs'][0]['instrument']['token'] = 'NSE:100'
        with self.assertRaises(Block): self.run_case()
        self.client.account.assert_not_called()


if __name__ == '__main__': unittest.main()
