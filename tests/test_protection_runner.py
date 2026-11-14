"""Standing authorization faults; no real broker, terminal confirmations or trades."""
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Block, fingerprint
from execution import ExecutionStore
from protection_runner import scope, validate_mandate, run, arm, MandateApproval
import test_protection_controller as fixture_module
from test_execution import NOW


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.w=Path(self.temp.name)
        self.fixture=fixture_module.ProtectionControllerTests();self.fixture.setUp()
        self.fixture.p.update(unattended_protection=True,automation_acceptance={
            'broker':'kite','account_id':'123456','status':'PASS','source':'offline fixture only',
            'broker_api_automation_permitted':True,'as_of':NOW.isoformat()})
        for name,body in {'config':self.fixture.c,'instrument':self.fixture.i,'live-profile':self.fixture.p,
                          'protection-policy':self.fixture.policy}.items():
            (self.w/(name+'.json')).write_text(json.dumps(body))
        self.digest,self.values=scope(self.w)
        self.m={'schema':'protection_mandate_v1','scope_hash':self.digest,'broker':'kite','account_id':'123456',
                'armed_at':NOW.isoformat(),'expires_at':(NOW+timedelta(hours=1)).isoformat(),'max_actions':2,'policy_id':'offline-1'}
        (self.w/'state').mkdir()
        (self.w/'state/protection-mandate.json').write_text(json.dumps(self.m))

    def validate(self,**kwargs):
        return validate_mandate(self.m,self.digest,self.values,kwargs.pop('records',[]),kwargs.pop('now',NOW),**kwargs)

    def test_expiry_revocation_scope_and_missing_opt_in(self):
        self.assertEqual(self.validate(),fingerprint(self.m))
        with self.assertRaises(Block):self.validate(now=NOW+timedelta(hours=1))
        with self.assertRaises(Block):self.validate(revoked=True)
        self.m['scope_hash']='different'
        with self.assertRaises(Block):self.validate()
        self.m['scope_hash']=self.digest;self.values['live-profile']['unattended_protection']=False
        with self.assertRaises(Block):self.validate()

    def test_action_budget_and_ambiguous_or_rejected_intents(self):
        record={'state':'FILLED','review':{'mandate_hash':fingerprint(self.m)}}
        with self.assertRaises(Block):self.validate(records=[record,record])
        for state in ('UNKNOWN','SUBMITTING','REJECTED'):
            with self.subTest(state=state),self.assertRaises(Block):self.validate(records=[{**record,'state':state}])

    def test_unaccepted_broker_and_excessive_duration_block(self):
        self.values['live-profile']['automation_acceptance']['broker_api_automation_permitted']=False
        with self.assertRaises(Block):self.validate()
        self.values['live-profile']['automation_acceptance']['broker_api_automation_permitted']=True
        self.m['expires_at']=(NOW+timedelta(hours=9)).isoformat()
        with self.assertRaises(Block):self.validate()

    def test_standing_authorization_cannot_open_entries(self):
        approval=MandateApproval(lambda:'hash')
        with self.assertRaises(Block):approval.approve({'mandate_hash':'hash','review_hash':'r','action':{'kind':'entry'}})
        with self.assertRaises(Block):approval.approve({'mandate_hash':'other','review_hash':'r','action':{'kind':'exit'}})

    def observation(self,*args):return self.fixture.run_decision(),self.fixture.s

    def test_bounded_run_sends_one_action_and_records_authorization(self):
        transport=Mock();transport.send.return_value={'order_id':'S1'}
        with patch('protection_runner.observe',side_effect=self.observation),patch('protection_runner.adapter'), \
             patch('protection_runner.LiveBroker',return_value=transport):
            result=run(self.w,cycles=1,clock=lambda:NOW,sleep=Mock())
        self.assertEqual(result['submitted_this_run'],1);transport.send.assert_called_once()
        health=json.loads((self.w/'state/protection-health.json').read_text())
        self.assertEqual(health['state'],'stopped');self.assertEqual(health['reason'],'cycle_limit')
        store=ExecutionStore(self.w/'state/execution.sqlite')
        try:
            records=store.records('kite','123456')
            self.assertEqual(records[0]['state'],'ACKNOWLEDGED')
            self.assertEqual(records[0]['review']['mandate_hash'],fingerprint(self.m))
            event=store.db.execute('SELECT body FROM execution_events ORDER BY sequence LIMIT 1').fetchone()[0]
            self.assertIn('human-armed bounded protection mandate',event)
        finally:store.close()

    def test_timeout_is_durable_and_revokes_without_retry(self):
        transport=Mock();transport.send.side_effect=TimeoutError('private details')
        with patch('protection_runner.observe',side_effect=self.observation),patch('protection_runner.adapter'), \
             patch('protection_runner.LiveBroker',return_value=transport):
            with self.assertRaises(Exception):run(self.w,cycles=3,clock=lambda:NOW,sleep=Mock())
            with self.assertRaises(Block):run(self.w,cycles=3,clock=lambda:NOW,sleep=Mock())
        transport.send.assert_called_once()
        self.assertTrue((self.w/'state/protection-revoked.json').exists())
        self.assertEqual(json.loads((self.w/'state/protection-health.json').read_text())['reason'],'failure_or_interruption')
        store=ExecutionStore(self.w/'state/execution.sqlite')
        try:self.assertEqual(store.records('kite','123456')[0]['state'],'UNKNOWN')
        finally:store.close()

    def test_changed_scope_stops_before_transport(self):
        config=self.fixture.c.copy();config['flat_by']='15:00'
        (self.w/'config.json').write_text(json.dumps(config))
        with patch('protection_runner.LiveBroker') as transport:
            with self.assertRaises(Block):run(self.w,cycles=1,clock=lambda:NOW)
            transport.assert_not_called()

    def test_arm_requires_human_review_and_sends_no_order(self):
        approval=Mock();approval.approve.side_effect=lambda review:review['review_hash']
        with patch('protection_runner.observe',side_effect=self.observation),patch('protection_runner.adapter'), \
             patch('protection_runner.LiveBroker') as transport:
            result=arm(self.w,(NOW+timedelta(hours=1)).isoformat(),2,approval=approval,clock=lambda:NOW)
        approval.approve.assert_called_once();transport.assert_not_called()
        self.assertEqual(result['status'],'ARMED');self.assertFalse(result['submitted'])

    def test_flat_observation_writes_terminal_health_without_submission(self):
        decision={'status':'FLAT_OBSERVED','review':None}
        with patch('protection_runner.observe',return_value=(decision,self.fixture.s)),patch('protection_runner.adapter'), \
             patch('protection_runner.LiveBroker') as transport:
            result=run(self.w,cycles=3,clock=lambda:NOW,sleep=Mock())
        transport.return_value.send.assert_not_called()
        self.assertEqual(result['reason'],'flat_observed')
        health=json.loads((self.w/'state/protection-health.json').read_text())
        self.assertEqual(health['completed_cycles'],1);self.assertEqual(health['last_decision'],'FLAT_OBSERVED')

    def test_revocation_during_sleep_blocks_next_action(self):
        transport=Mock();transport.send.return_value={'order_id':'S1'}
        def revoke(seconds):(self.w/'state/protection-revoked.json').write_text('{}')
        with patch('protection_runner.observe',side_effect=self.observation),patch('protection_runner.adapter'), \
             patch('protection_runner.LiveBroker',return_value=transport):
            with self.assertRaises(Block):run(self.w,cycles=3,clock=lambda:NOW,sleep=revoke)
        transport.send.assert_called_once()


if __name__=='__main__':unittest.main()
