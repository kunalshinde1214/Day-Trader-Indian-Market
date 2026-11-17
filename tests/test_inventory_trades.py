import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Block, fingerprint
from inventory_book import lot
import test_inventory_book as fixtures


class InventoryTradeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.InventoryBookTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.spec.update(schema='inventory_reconciliation_v2', trading_activity=True,
            trade_ledger={'broker': 'kite', 'account_id': 'SYNTHETIC', 'complete': True, 'evidence_id': 'statement', 'fills': []})
        self.spec = self.f.spec

    def sale(self):
        event = self.f.event('sell', fill_id='F1', quantity=4, price=120, charges_inr=2, principal_repaid_inr=0)
        self.spec['events'] = [event]
        self.spec['trade_ledger']['fills'] = [{**{k: self.f.row[k] for k in ('exchange', 'symbol', 'token')},
            'fill_id': 'F1', 'side': 'SELL', 'quantity': 4, 'price': 120, 'at': event['effective_at']}]
        self.spec['closing']['lots'] = [{**self.f.row, 'quantity': 6, 'cost_basis_inr': '600'}]

    def test_sale_and_later_settlement_conserve_cost(self):
        self.sale()
        before = self.spec['closing']['lots'][0]
        transfer = self.f.event('transfer', quantity=6, destination_bucket='SETTLED', new_lot_id='settled', principal_repaid_inr=0)
        transfer.update(event_id='E2', expected_lot_hash=fingerprint(lot(before)), effective_at=fixtures.NOW.isoformat())
        self.spec['events'].append(transfer)
        self.spec['closing']['lots'] = [{**before, 'lot_id': 'settled', 'bucket': 'SETTLED'}]
        result = self.f.run_case()
        self.assertEqual(result['trade_fills_reconciled'], 1)
        self.assertEqual(result['sale_cash_after_charges_and_principal_inr'], '478')
        self.assertFalse(result['execution_eligible'])

    def test_buy_creates_sourced_t1_lot(self):
        new = lot({**self.f.row, 'lot_id': 'purchase', 'quantity': 2, 'cost_basis_inr': '201'})
        event = self.f.event('buy', fill_id='B1', new_lot=new, price=100, capitalized_charges_inr=1)
        event.update(lot_id='purchase', expected_lot_hash=None)
        self.spec['events'] = [event]
        self.spec['trade_ledger']['fills'] = [{**{k:new[k] for k in ('exchange','symbol','token')},
            'fill_id': 'B1', 'side': 'BUY', 'quantity': 2, 'price': 100, 'at': event['effective_at']}]
        self.spec['closing']['lots'].append(new)
        self.assertEqual(self.f.run_case()['trade_fills_reconciled'], 1)
        event['new_lot']['cost_basis_inr'] = '200'
        with self.assertRaises(Block): self.f.run_case()

    def test_missing_duplicate_and_wrong_account_ledger_block(self):
        self.sale(); original = copy.deepcopy(self.spec['trade_ledger'])
        for ledger in ({**original, 'fills': []}, {**original, 'fills': original['fills']*2}, {**original, 'account_id':'OTHER'}, {**original, 'complete':False}):
            self.spec['trade_ledger'] = ledger
            with self.assertRaises(Block): self.f.run_case()

    def test_omitted_fill_blocks_even_if_closing_lots_match(self):
        self.sale(); extra = {**self.spec['trade_ledger']['fills'][0], 'fill_id': 'F2'}
        self.spec['trade_ledger']['fills'].append(extra)
        with self.assertRaisesRegex(Block, 'every observed fill'): self.f.run_case()

    def test_wrong_quantity_price_or_time_blocks(self):
        self.sale(); original = copy.deepcopy(self.spec['events'][0])
        for changes in ({'quantity': 5}, {'price': 121}, {'effective_at': fixtures.NOW.isoformat()}):
            self.spec['events'][0] = {**original, **changes}
            with self.assertRaises(Block): self.f.run_case()


if __name__ == '__main__': unittest.main()
