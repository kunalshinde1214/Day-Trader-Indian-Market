"""Depth sampling and partial-fill margin scenarios; simulated transport only."""
import copy
from datetime import timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Block
from depth_collect import observation, combine, collect
from portfolio_margin import quote
from test_execution import sample, NOW
import test_india_expansion as expansion


class DepthTests(unittest.TestCase):
    def setUp(self):
        self.i,_=sample()
        self.key=self.i['exchange']+':'+self.i['symbol']
        self.row={'instrument_token':self.i['token'],'last_price':100,'timestamp':NOW.isoformat(),
                  'last_trade_time':NOW.isoformat(),'volume':1000,
                  'depth':{'buy':[{'price':99,'quantity':50}], 'sell':[{'price':101,'quantity':70}]}}

    def response(self,row=None):return {'source':'kite','data':{self.key:row or self.row}}

    def test_observed_volume_delta_not_daily_volume(self):
        first=observation(self.response(),self.i,NOW)
        later=NOW+timedelta(seconds=2)
        row={**self.row,'timestamp':later.isoformat(),'last_trade_time':later.isoformat(),'volume':1020}
        result=combine(first,observation(self.response(row),self.i,later))
        self.assertEqual(result['traded_units'],20)
        self.assertNotIn('cumulative_volume',result)

    def test_duplicate_reset_and_cross_session_samples_block(self):
        first=observation(self.response(),self.i,NOW)
        for changed in ({}, {'as_of':(NOW+timedelta(seconds=2)).isoformat(),'cumulative_volume':999},
                        {'as_of':(NOW+timedelta(days=1)).isoformat()}):
            with self.subTest(changed=changed),self.assertRaises(Block):combine(first,{**first,**changed})

    def test_crossed_unsorted_and_wrong_identity_block(self):
        for change in ('crossed','unsorted','token'):
            row=copy.deepcopy(self.row)
            if change=='crossed':row['depth']['buy'][0]['price']=102
            elif change=='unsorted':row['depth']['buy'].append({'price':100,'quantity':10})
            else:row['instrument_token']='other'
            with self.subTest(change=change),self.assertRaises(Block):observation(self.response(row),self.i,NOW)

    def test_sampling_is_two_readonly_requests(self):
        later=NOW+timedelta(seconds=2)
        row={**self.row,'timestamp':later.isoformat(),'last_trade_time':later.isoformat(),'volume':1020}
        client=Mock();client.quotes.side_effect=[self.response(),self.response(row)]
        clock=Mock(side_effect=[NOW,later]);sleep=Mock()
        result=collect(client,self.i,clock=clock,sleep=sleep)
        sleep.assert_called_once_with(2);self.assertEqual(client.quotes.call_count,2)
        self.assertEqual(result['traded_units'],20)


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        fixture=expansion.BasketTests();fixture.setUp()
        self.bundle=fixture.bundle;self.client=fixture.client;self.now=fixture.now

    def test_requested_quantities_sent_and_peak_covers_scenarios(self):
        self.bundle['fill_scenarios']=[[0,5],[5,10]]
        original=self.client.request.side_effect
        def response(method,path,query,body):
            result=original(method,path,query,body)
            if body[0]['quantity']==5:result['initial']['total']=9000
            return result
        self.client.request.side_effect=response
        result=quote(self.client,self.bundle,clock=lambda:self.now)
        self.assertEqual(result['peak_estimate_inr'],9000)
        self.assertEqual(len(result['fill_scenario_estimates']),2)
        self.assertEqual([r['quantity'] for r in self.client.request.call_args_list[2].kwargs['body']],[5])
        self.assertFalse(result['execution_eligible'])

    def test_bad_scenarios_block_before_network(self):
        for scenarios in ([[0,0]],[[11,0]],[[1]],[[5,0],[5,0]],[[True,0]],[[0.5,0]]):
            with self.subTest(scenarios=scenarios),self.assertRaises(Block):
                self.bundle['fill_scenarios']=scenarios
                quote(self.client,self.bundle,clock=lambda:self.now)
        self.client.account.assert_not_called()


if __name__=='__main__':unittest.main()
