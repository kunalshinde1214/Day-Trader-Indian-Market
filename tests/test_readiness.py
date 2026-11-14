import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Block, instant
from readiness import assess, REQUIRED


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.now=instant('2026-11-14T10:00:00+05:30')
        self.spec={'schema':'acceptance_inventory_v1','broker':'kite','account_id':'OFFLINE','evidence':{}}

    def add(self,category,**overrides):
        body={'broker':'kite','account_id':'OFFLINE','as_of':self.now.isoformat(),'status':'PASS','category':category,
              'source':'offline test','observed':True,'checks':{'observed_test':True}}
        if category=='broker_readonly':body.update(mode='authenticated_readonly',submitted=False,checks={'account_identity_matches':True})
        body.update(overrides);path=self.root/(category+'.json');path.write_text(json.dumps(body))
        self.spec['evidence'][category]={'path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

    def test_missing_evidence_never_passes(self):
        result=assess(self.spec,self.root,self.now)
        self.assertEqual(result['status'],'INCOMPLETE');self.assertFalse(result['execution_authorized'])

    def test_complete_inventory_is_not_authorization(self):
        for category in REQUIRED:self.add(category)
        result=assess(self.spec,self.root,self.now)
        self.assertEqual(result['status'],'EVIDENCE_RECORDED');self.assertFalse(result['execution_authorized'])

    def test_wrong_account_and_public_probe_do_not_count(self):
        self.add('broker_readonly',mode='public')
        self.assertEqual(assess(self.spec,self.root,self.now)['evidence'][0]['status'],'FAILED')
        self.add('broker_readonly',account_id='OTHER')
        self.assertEqual(assess(self.spec,self.root,self.now)['evidence'][0]['status'],'FAILED')

    def test_modified_evidence_blocks(self):
        self.add('broker_readonly');(self.root/'broker_readonly.json').write_text('{}')
        with self.assertRaises(Block):assess(self.spec,self.root,self.now)

    def test_stale_and_unobserved_evidence(self):
        self.add('installed_host',observed=False)
        self.assertEqual(assess(self.spec,self.root,self.now)['evidence'][1]['status'],'FAILED')
        self.add('installed_host',as_of='2026-11-12T10:00:00+05:30')
        with self.assertRaises(Block):assess(self.spec,self.root,self.now)


if __name__=='__main__':unittest.main()
