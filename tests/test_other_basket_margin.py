"""Offline basket contracts; no credentials or account traffic."""
import copy
from datetime import timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from core import Block
from portfolio_margin import quote
from test_execution import sample, NOW


def fixture(broker):
    instrument, _ = sample(broker)
    tokens = {'upstox': ['NSE_EQ|INE000000001', 'NSE_EQ|INE000000002'],
              'angel': ['100', '200'], 'fyers': ['10100000999999', '10100000999998']}[broker]
    legs = [{'instrument': {**instrument, 'symbol': f'DEMO{index}-EQ', 'token': token},
             'account_id': '123456', 'product': 'CNC', 'side': 'BUY', 'quantity': 10, 'price': 100}
            for index, token in enumerate(tokens)]
    bundle = {'broker': broker, 'account_id': '123456', 'legs': legs}
    account = {'broker': broker, 'source': broker, 'account_id': '123456', 'as_of': NOW.isoformat(),
               'positions': [], 'orders': [], 'margins': {'cash': 10000}}
    client = Mock(); client.account.return_value = account
    def request(method, path, body):
        key = {'upstox': 'instruments', 'angel': 'positions', 'fyers': 'data'}[broker]
        count = len(body[key])
        amount = 3000 if count == 1 else 2000
        if broker == 'upstox':
            return {'margins': [{'total_margin': amount / count} for _ in body[key]],
                    'required_margin': amount, 'final_margin': amount - 500}
        if broker == 'angel': return {'totalMarginRequired': amount}
        return {'data': {'margin_total': amount, 'margin_new_order': amount + 7000, 'margin_avail': 10000}}
    client.request.side_effect = request
    return bundle, client, account


class OtherBasketTests(unittest.TestCase):
    def test_prefix_and_grid_peak_preserved_for_each_broker(self):
        for broker in ('upstox', 'angel', 'fyers'):
            with self.subTest(broker=broker):
                bundle, client, _ = fixture(broker)
                bundle['fill_grid'] = [[0, 5, 10], [0, 5, 10]]
                result = quote(client, bundle, clock=lambda: NOW)
                self.assertEqual(client.request.call_count, 10)
                self.assertEqual(len(result['fill_scenario_estimates']), 8)
                self.assertEqual(result['peak_estimate_inr'], 3000)
                self.assertEqual(result['final_required_inr'], 2000)
                self.assertFalse(result['execution_eligible'])
                self.assertTrue(all(not r['response_identity_verified'] for r in result['prefix_estimates']))
                self.assertEqual(result['final_estimate_inr'], 1500 if broker == 'upstox' else None)

    def test_upstox_wire_keys_quantities_products_and_prices(self):
        bundle, client, _ = fixture('upstox')
        bundle['legs'][1]['product'] = 'MIS'; bundle['fill_scenarios'] = [[0, 5]]
        quote(client, bundle, clock=lambda: NOW)
        call = client.request.call_args
        self.assertEqual(call.args, ('POST', '/v2/charges/margin'))
        self.assertEqual(call.kwargs['body'], {'instruments': [{'instrument_key': 'NSE_EQ|INE000000002',
                         'quantity': 5, 'transaction_type': 'BUY', 'product': 'I', 'price': 100}]})

    def test_angel_delivery_wire_and_partial_quantities(self):
        bundle, client, _ = fixture('angel'); bundle['fill_scenarios'] = [[0, 5]]
        quote(client, bundle, clock=lambda: NOW)
        call = client.request.call_args
        self.assertEqual(call.args[:2], ('POST', '/rest/secure/angelbroking/margin/v1/batch'))
        self.assertEqual(call.args[2], {'positions': [{'exchange': 'NSE', 'qty': 5, 'price': 100,
                         'productType': 'DELIVERY', 'token': '200', 'tradeType': 'BUY', 'orderType': 'LIMIT'}]})

    def test_fyers_account_total_kept_separate_from_proposed_requirement(self):
        bundle, client, _ = fixture('fyers')
        result = quote(client, bundle, clock=lambda: NOW)
        self.assertEqual(result['peak_estimate_inr'], 3000)
        self.assertEqual(result['peak_account_total_inr'], 10000)
        call = client.request.call_args
        self.assertEqual(call.args, ('POST', '/api/v3/multiorder/margin'))
        self.assertEqual(call.kwargs['body']['data'][1]['symbol'], 'NSE:DEMO1-EQ')
        self.assertEqual(call.kwargs['body']['data'][1]['productType'], 'CNC')

    def test_derivative_carry_products_and_units(self):
        for broker, product in (('upstox', 'D'), ('angel', 'CARRYFORWARD'), ('fyers', 'MARGIN')):
            bundle, client, _ = fixture(broker)
            bundle['legs'] = bundle['legs'][:1]
            leg = bundle['legs'][0]; leg.update(product='NRML', side='SELL', quantity=10)
            leg['instrument'].update(kind='future', exchange='NFO', lot_size=5, expiry='2026-11-26', underlying_type='index')
            leg['instrument']['token'] = {'upstox': 'NSE_FO|100', 'angel': '100', 'fyers': '10110000999999'}[broker]
            quote(client, bundle, clock=lambda: NOW)
            call = client.request.call_args
            body = call.args[2] if broker == 'angel' else call.kwargs['body']
            row = body[{'upstox': 'instruments', 'angel': 'positions', 'fyers': 'data'}[broker]][0]
            self.assertEqual(row['product' if broker == 'upstox' else 'productType'], product)
            self.assertEqual(row['quantity' if broker == 'upstox' else 'qty'], 10)

    def test_upstox_repeated_token_across_products_block_before_reads(self):
        bundle, client, _ = fixture('upstox')
        bundle['legs'][1]['instrument'] = copy.deepcopy(bundle['legs'][0]['instrument'])
        bundle['legs'][1]['product'] = 'MIS'
        with self.assertRaisesRegex(Block, 'repeated instrument'): quote(client, bundle, clock=lambda: NOW)
        client.account.assert_not_called()

    def test_dhan_not_approximated_by_summing_single_order_margin(self):
        bundle, client, _ = fixture('angel'); bundle['broker'] = 'dhan'
        with self.assertRaises(Block): quote(client, bundle, clock=lambda: NOW)
        client.account.assert_not_called()

    def test_wrong_provider_or_stale_account_blocks_before_margin(self):
        for changes in ({'source': 'other'}, {'broker': 'other'}, {'account_id': 'other'},
                        {'as_of': (NOW - timedelta(seconds=61)).isoformat()}):
            bundle, client, account = fixture('angel'); account.update(changes)
            with self.assertRaises(Block): quote(client, bundle, clock=lambda: NOW)
            client.request.assert_not_called()

    def test_account_change_invalidates_estimates(self):
        for broker in ('upstox', 'angel', 'fyers'):
            bundle, client, account = fixture(broker)
            client.account.side_effect = [account, {**account, 'orders': [{'changed': True}]}]
            with self.assertRaisesRegex(Block, 'books changed'): quote(client, bundle, clock=lambda: NOW)

    def test_upstox_mismatched_response_count_blocks(self):
        bundle, client, _ = fixture('upstox')
        client.request.side_effect = None
        client.request.return_value = {'margins': [], 'required_margin': 0, 'final_margin': 0}
        with self.assertRaisesRegex(Block, 'row count'): quote(client, bundle, clock=lambda: NOW)

    def test_negative_or_nonfinite_margin_blocks(self):
        for value in (-1, float('nan'), float('inf'), True):
            bundle, client, _ = fixture('angel'); client.request.side_effect = None
            client.request.return_value = {'totalMarginRequired': value}
            with self.assertRaises(Block): quote(client, bundle, clock=lambda: NOW)

    def test_zero_margin_is_recorded_without_inventing_a_requirement(self):
        bundle, client, _ = fixture('angel'); client.request.side_effect = None
        client.request.return_value = {'totalMarginRequired': 0}
        result = quote(client, bundle, clock=lambda: NOW)
        self.assertEqual(result['peak_estimate_inr'], 0)
        self.assertIsNone(result['final_estimate_inr'])

    def test_identity_exchange_conflict_blocks_before_network(self):
        for broker, token in (('upstox', 'BSE_EQ|INE000000001'), ('fyers', '12100000999999')):
            bundle, client, _ = fixture(broker); bundle['legs'][0]['instrument']['token'] = token
            with self.assertRaises(Block): quote(client, bundle, clock=lambda: NOW)
            client.account.assert_not_called()

    def test_cross_account_legs_and_bad_grids_block_before_reads(self):
        bundle, client, _ = fixture('angel'); bundle['legs'][1]['account_id'] = 'other'
        with self.assertRaises(Block): quote(client, bundle, clock=lambda: NOW)
        client.account.assert_not_called()
        bundle, client, _ = fixture('fyers'); bundle['fill_grid'] = [[0, 11], [0, 5]]
        with self.assertRaises(Block): quote(client, bundle, clock=lambda: NOW)
        client.account.assert_not_called()


if __name__ == '__main__': unittest.main()
