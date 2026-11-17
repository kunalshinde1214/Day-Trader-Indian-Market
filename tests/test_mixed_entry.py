"""Mixed cash-entry controls against simulated complete carry/intraday books."""
import copy
from datetime import timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from core import Block, fingerprint, size
from execution import ExecutionStore, execute
from mixed_entry import prepare, risk_ledger, main
from live_broker import wire_payload
import test_execution as fixtures


class MixedEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixtures.ExecutionTests.setUpClass()

    def setUp(self):
        self.now = fixtures.NOW
        self.bundle, self.profile = copy.deepcopy((fixtures.ExecutionTests.base, fixtures.ExecutionTests.profile))
        selected = self.bundle['instrument']; self.bundle['context']['quote']['token'] = selected['token']
        self.carry = {**selected, 'symbol': 'CARRY', 'token': '123'}
        self.master = [selected, self.carry]
        self.header = {'broker': 'kite', 'account_id': '123456', 'source': 'kite', 'date': self.now.date().isoformat(),
                       'as_of': self.now.isoformat(), 'complete': True}
        ident = {k: self.carry[k] for k in ('exchange', 'symbol', 'token')}
        previous = (self.now - timedelta(days=1)).date().isoformat()
        opening = {**self.header, 'source': 'broker_statement', 'schema': 'portfolio_opening_v1', 'previous_session': previous,
                   'adjustments_reviewed': True, 'inventory_events': [], 'positions': [{**ident, 'product': 'CNC', 'quantity': 10,
                   'average_price': 100, 'previous_close': 100, 'price_basis': 'per_unit_inr', 'cost_source': 'broker_statement',
                   'mark_source': 'previous_close', 'mark_session': previous}]}
        self.position = {**ident, 'product': 'CNC', 'quantity': 10, 'mark_price': 100, 'mark_source': 'kite',
                         'mark_as_of': self.now.isoformat(), 'price_basis': 'per_unit_inr'}
        self.collection = {'schema': 'portfolio_collection_v1', 'opening': opening,
                           'snapshot': {**self.header, 'schema': 'portfolio_snapshot_v1', 'inventory_events': [],
                                        'positions': [self.position], 'orders': []},
                           'execution_orders': [], 'fills': {**self.header, 'fills': []}}
        self.funds = {**self.header, 'margins': {'equity': {'available': {'live_balance': 100000}}}}
        self.reservations = {k: self.header[k] for k in ('broker', 'account_id', 'date')}; self.reservations['entries'] = []
        self.profile.update(mixed_inventory_entries=True, max_portfolio_gross_inr=200000,
                            mixed_entry_acceptance={'broker': 'kite', 'account_id': '123456', 'source': 'offline test',
                                                    'status': 'PASS', 'as_of': self.now.isoformat()})

    def review(self):
        return prepare(self.bundle, self.collection, self.master, self.funds, self.reservations, self.profile, self.now)

    def order(self, product='MIS', quantity=2, filled=2, side='BUY', oid='O1'):
        ident = {k: self.carry[k] for k in ('exchange', 'symbol', 'token')}
        row = {**ident, 'product': product, 'order_id': oid, 'quantity': quantity, 'filled_quantity': filled,
               'side': side, 'status': 'FILLED' if quantity == filled else 'PARTIAL' if filled else 'OPEN',
               'price': 100, 'trigger_price': 0, 'order_type': 'LIMIT', 'variety': 'regular'}
        self.collection['snapshot']['orders'].append(copy.deepcopy(row)); self.collection['execution_orders'].append(row)
        if filled:
            self.collection['fills']['fills'].append({**ident, 'product': product, 'order_id': oid, 'fill_id': 'F' + oid,
                                                      'quantity': filled, 'side': side, 'price': 100, 'source': 'kite',
                                                      'at': (self.now - timedelta(seconds=1)).isoformat(), 'fees_final': True, 'fees': {'contract': 1}})
        return row

    def intraday(self, quantity=2, filled=2):
        self.order(quantity=quantity, filled=filled)
        self.collection['snapshot']['positions'].append({**self.position, 'product': 'MIS', 'quantity': filled})
        self.reservations['entries'] = [{'entry_order_id': 'O1', 'exit_order_ids': [], 'planned_loss_inr': 100}]

    def test_existing_carry_is_preserved_in_gross_exposure(self):
        review, snapshot = self.review()
        self.assertEqual(review['account_risk']['position_values']['NSE:CARRY'], 1000)
        self.assertEqual(review['portfolio_gross_after_inr'], 1000 + review['risk']['metrics']['notional_inr'])
        self.assertEqual(snapshot['positions'][0]['product'], 'CNC')
        self.assertEqual(review['action']['product'], 'MIS')

    def test_five_broker_product_identity_and_fund_contracts(self):
        for broker in ('kite', 'upstox', 'dhan', 'angel', 'fyers'):
            with self.subTest(broker=broker):
                self.setUp()
                tokens = {'upstox': ('NSE_EQ|INE000A01028', 'NSE_EQ|INE000A01010'),
                          'fyers': ('10100000000001', '10100000000002')}.get(broker, (self.master[0]['token'], '123'))
                original = self.master[0]['token']
                def convert(value):
                    if isinstance(value, dict):
                        return {k: (broker if k in ('broker', 'source') and v == 'kite' else
                                    tokens[0] if k == 'token' and v == original else tokens[1] if k == 'token' and v == '123' else convert(v))
                                for k, v in value.items()}
                    if isinstance(value, list): return [convert(v) for v in value]
                    return value
                self.bundle, self.profile, self.collection, self.master, self.reservations = convert(
                    [self.bundle, self.profile, self.collection, self.master, self.reservations])
                self.bundle['charges']['source'] = 'offline_tariff_fixture'
                self.bundle['context']['replay']['plan_hash'] = fingerprint(self.bundle['order']['plan'])
                self.funds = {**self.header, 'source': broker, 'broker': broker, 'margins': {
                    'kite': {'equity': {'available': {'live_balance': 100000}}},
                    'upstox': {'equity': {'available_margin': 100000}}, 'dhan': {'availabelBalance': 100000},
                    'angel': {'availablecash': 100000}, 'fyers': [{'title': 'Available Balance', 'equityAmount': 100000}]}[broker]}
                plan = self.bundle['order']['plan']
                payload = wire_payload({'broker': broker, 'account_id': '123456', 'kind': 'entry', 'product': 'MIS',
                                        'instrument': self.master[0], 'quantity': self.bundle['order']['quantity'], 'price': plan['entry'], 'side': 'BUY'})
                self.bundle['live_margin']['payload_hash'] = fingerprint(payload)
                review, _ = self.review()
                self.assertEqual(review['payload'], payload)
                self.assertEqual(review['action']['broker'], broker)

    def test_intraday_and_carry_reservations_are_combined_without_netting(self):
        self.intraday(quantity=5, filled=2)
        review, _ = self.review()
        self.assertEqual(review['account_risk']['position_values']['NSE:CARRY'], 1500)
        self.assertEqual(review['account_risk']['daily_risk_used'], 100)
        self.assertEqual(review['account_risk']['trades_today'], 1)

    def test_existing_short_mis_and_long_carry_use_gross_not_net_exposure(self):
        self.order(quantity=5, filled=2, side='SELL')
        self.collection['snapshot']['positions'].append({**self.position, 'product': 'MIS', 'quantity': -2})
        self.reservations['entries'] = [{'entry_order_id': 'O1', 'exit_order_ids': [], 'planned_loss_inr': 100}]
        review, _ = self.review()
        self.assertEqual(review['account_risk']['position_values']['NSE:CARRY'], 1500)
        self.assertEqual(review['account_risk']['daily_risk_used'], 100)

    def test_new_short_requires_short_opt_in_and_exact_margin_payload(self):
        plan = copy.deepcopy(self.bundle['order']['plan'])
        plan.update(side='SELL', stop=2*plan['entry']-plan['stop'], target=2*plan['entry']-plan['target'])
        self.bundle['context']['replay']['plan_hash'] = fingerprint(plan)
        self.bundle['config']['allow_short'] = True
        self.bundle['order'] = size(plan, self.bundle['instrument'], self.bundle['config'],
                                    self.bundle['context'], self.bundle['charges'], self.now)
        action = {'broker': 'kite', 'account_id': '123456', 'kind': 'entry', 'product': 'MIS',
                  'instrument': self.bundle['instrument'], 'quantity': self.bundle['order']['quantity'],
                  'price': plan['entry'], 'side': 'SELL'}
        self.bundle['live_margin']['payload_hash'] = fingerprint(wire_payload(action))
        review, _ = self.review()
        self.assertEqual(review['action']['side'], 'SELL')
        self.assertEqual(review['action']['product'], 'MIS')
        self.bundle['config']['allow_short'] = False
        with self.assertRaisesRegex(Block, 'SHORT'): self.review()

    def test_unmapped_orders_or_missing_fills_block(self):
        self.intraday(); self.reservations['entries'] = []
        with self.assertRaises(Block): self.review()
        self.reservations['entries'] = [{'entry_order_id': 'O1', 'exit_order_ids': [], 'planned_loss_inr': 100}]
        self.collection['fills']['fills'] = []
        with self.assertRaises(Block): self.review()

    def test_pending_carry_buy_and_overdrawn_exit_block(self):
        self.order(product='CNC', filled=0)
        with self.assertRaises(Block): self.review()
        self.collection['snapshot']['orders'] = []; self.collection['execution_orders'] = []
        self.order(product='CNC', quantity=11, filled=0, side='SELL')
        with self.assertRaises(Block): self.review()

    def test_carry_reducing_working_order_does_not_free_exposure(self):
        self.order(product='CNC', quantity=10, filled=0, side='SELL')
        review, _ = self.review()
        self.assertEqual(review['account_risk']['position_values']['NSE:CARRY'], 1000)
        self.assertEqual(review['account_risk']['orders_today'], 1)

    def test_carry_losses_consume_daily_budget_and_gains_do_not_replenish(self):
        self.intraday()
        self.collection['opening']['positions'][0]['previous_close'] = 110
        self.assertEqual(self.review()[0]['account_risk']['daily_risk_used'], 200)
        self.collection['opening']['positions'][0]['previous_close'] = 90
        self.assertEqual(self.review()[0]['account_risk']['daily_risk_used'], 100)
        self.collection['opening']['positions'][0]['previous_close'] = 1000
        with self.assertRaises(Block): self.review()

    def test_carry_provisional_fees_are_not_treated_as_zero(self):
        self.order(product='CNC', quantity=1, filled=1, side='SELL'); self.position['quantity'] = 9
        self.collection['fills']['fills'][0]['fees_final'] = False
        with self.assertRaisesRegex(Block, 'carry fill charges'): self.review()
        self.collection['fills']['fills'][0]['fees_final'] = True
        self.assertEqual(self.review()[0]['account_risk']['daily_risk_used'], 1)

    def test_profile_gross_and_live_margin_limits(self):
        review, _ = self.review(); self.profile['max_portfolio_gross_inr'] = review['portfolio_gross_after_inr'] - 1
        with self.assertRaisesRegex(Block, 'gross exposure'): self.review()
        self.profile['max_portfolio_gross_inr'] = 200000; self.bundle['live_margin']['payload_hash'] = 'wrong'
        with self.assertRaisesRegex(Block, 'margin payload'): self.review()

    def test_distinct_mixed_opt_in_and_account_acceptance(self):
        self.profile['mixed_inventory_entries'] = False
        with self.assertRaises(Block): self.review()
        self.profile['mixed_inventory_entries'] = True; self.profile['mixed_entry_acceptance']['account_id'] = 'OTHER'
        with self.assertRaises(Block): self.review()

    def test_freshness_funds_quote_identity_and_demo_evidence(self):
        self.funds['account_id'] = 'OTHER'
        with self.assertRaises(Block): self.review()
        self.funds['account_id'] = '123456'; self.position['mark_as_of'] = (self.now - timedelta(seconds=31)).isoformat()
        with self.assertRaises(Block): self.review()
        self.position['mark_as_of'] = self.now.isoformat(); self.bundle['context']['quote']['token'] = 'wrong'
        with self.assertRaises(Block): self.review()
        self.bundle['context']['quote']['token'] = self.bundle['instrument']['token']; self.collection['opening']['source'] = 'synthetic'
        with self.assertRaises(Block): self.review()

    def test_short_carry_derivatives_and_carry_entries_remain_blocked(self):
        self.bundle['order']['plan']['side'] = 'SELL'
        with self.assertRaises(Block): self.review()
        self.bundle['order']['plan']['side'] = 'BUY'; self.bundle['order']['plan']['product'] = 'CNC'
        with self.assertRaises(Block): self.review()

    def test_refresh_timestamps_do_not_change_unchanged_review(self):
        original, _ = self.review(); self.now += timedelta(seconds=5)
        for item in (self.collection['snapshot'], self.collection['fills'], self.funds, self.bundle['context']['quote'], self.bundle['live_margin']):
            item['as_of'] = self.now.isoformat()
        self.position['mark_as_of'] = self.now.isoformat()
        self.assertEqual(original['review_hash'], self.review()[0]['review_hash'])

    def test_changed_inventory_after_approval_blocks_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExecutionStore(Path(tmp) / 'execution.sqlite')
            try:
                transport, approval = Mock(), Mock()
                def approve(review):
                    self.collection['opening']['positions'][0]['quantity'] = 11; self.position['quantity'] = 11
                    return review['review_hash']
                approval.approve.side_effect = approve
                with self.assertRaises(Block): execute(store, transport, approval, self.review, clock=lambda: self.now)
                transport.send.assert_not_called()
            finally: store.close()

    def test_cli_collects_around_margin_and_revalidates_after_approval(self):
        client = Mock(); client.account.return_value = self.funds; client.margin.return_value = self.bundle['live_margin']
        transport = Mock(); transport.send.return_value = {'order_id': 'new-entry'}
        approval = Mock(); approval.approve.side_effect = lambda review: review['review_hash']
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp)
            for name, data in {'config': self.bundle['config'], 'live-profile': self.profile, 'opening': self.collection['opening'],
                               'master': {'instruments': self.master}}.items(): (w / (name + '.json')).write_text(json.dumps(data))
            args = ['entry', '--workdir', tmp, '--opening', str(w / 'opening.json'), '--master', str(w / 'master.json')]
            with patch('broker.adapter', return_value=client), patch('live.load_bundle', side_effect=lambda w: copy.deepcopy(self.bundle)), \
                 patch('broker_data.normalize_quote', return_value=self.bundle['context']['quote']), \
                 patch('mixed_entry.collect', return_value=self.collection) as collect, patch('mixed_entry.instant', return_value=self.now), \
                 patch('mixed_entry.LiveBroker', return_value=transport), patch('mixed_entry.TerminalApproval', return_value=approval), \
                 patch('mixed_entry.execute', side_effect=lambda *args: execute(*args, clock=lambda: self.now)), patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(main(args), 0)
            self.assertEqual(collect.call_count, 4); self.assertEqual(client.margin.call_count, 2); transport.send.assert_called_once()


if __name__ == '__main__': unittest.main()
