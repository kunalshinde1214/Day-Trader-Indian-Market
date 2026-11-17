from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Block
from acceptance import extended_checks
import test_portfolio_exit as fixtures
import test_dhan_basket as basket_fixtures


class ExtendedAcceptanceTests(unittest.TestCase):
    def test_carry_probe_uses_observed_collection_and_returns_no_positions(self):
        f = fixtures.MixedExitTests(); f.setUp()
        with patch('portfolio_exit.collect', return_value=f.collection()):
            result = extended_checks(Mock(), 'kite', 'DEMO', f.i, opening=f.opening,
                                     master={'instruments': f.master}, clock=lambda: fixtures.NOW)
        self.assertTrue(result['carry_inventory']['inventory_reconciled'])
        self.assertNotIn('positions', str(result))

    def test_basket_probe_preserves_unverified_response_identity(self):
        f = basket_fixtures.DhanBasketTests(); f.setUp()
        result = extended_checks(f.client, 'dhan', '123456', f.bundle['legs'][0]['instrument'],
                                 basket=f.bundle, clock=lambda: basket_fixtures.NOW)
        self.assertEqual(result['basket_margin']['prefix_count'], 2)
        self.assertFalse(result['basket_margin']['response_identity_verified'])

    def test_wrong_account_optional_probe_blocks_before_requests(self):
        client = Mock()
        with self.assertRaises(Block):
            extended_checks(client, 'kite', 'user', {'broker': 'kite'}, basket={'broker': 'kite', 'account_id': 'other'})
        client.assert_not_called(); client.request.assert_not_called()

    def test_opening_and_master_must_be_paired(self):
        with self.assertRaises(Block): extended_checks(Mock(), 'kite', 'user', {'broker': 'kite'}, opening={})

    def test_no_optional_probes_make_no_requests(self):
        client = Mock()
        self.assertEqual(extended_checks(client, 'kite', 'user', {'broker': 'kite'}), {})
        self.assertEqual(client.mock_calls, [])

    def test_depth_read_does_not_promote_research_timestamp(self):
        with patch('depth_collect.collect', return_value={'liquidity_model_eligible': False}):
            result = extended_checks(Mock(), 'dhan', 'user', {'broker': 'dhan'}, depth=True)
        self.assertFalse(result['cash_depth']['liquidity_model_eligible'])


if __name__ == '__main__': unittest.main()
