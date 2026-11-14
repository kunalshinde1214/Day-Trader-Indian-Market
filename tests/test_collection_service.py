import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Block,instant
from collection_service import run


class CollectionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.settings={'schema':'public_collection_settings_v1','symbols':['RELIANCE.NS']}
        self.clock=lambda:instant('2026-11-14T10:00:00+05:30')
        self.collector=Mock(return_value={'status':'PARTIAL','coverage':[],'submitted':False,'full_market_coverage':False})

    def test_cycle_reports_are_unique_and_filing_backlog_reused(self):
        result=run(self.root,self.settings,cycles=2,interval=60,collector=self.collector,clock=self.clock,sleep=Mock())
        self.assertEqual(len(set(result['reports'])),2)
        for call in self.collector.call_args_list:self.assertEqual(call.kwargs['archive'],self.root/'filings.sqlite')
        self.assertEqual(json.loads((self.root/'latest.json').read_text())['report'],result['reports'][-1])
        self.assertFalse(json.loads((self.root/'latest.json').read_text())['full_market_coverage'])

    def test_failure_is_saved_and_does_not_erase_previous_report(self):
        self.collector.side_effect=[self.collector.return_value,OSError('private')]
        result=run(self.root,self.settings,cycles=2,collector=self.collector,clock=self.clock,sleep=Mock())
        self.assertEqual(result['failed_cycles'],1)
        for name in result['reports']:self.assertTrue((self.root/name).exists())
        self.assertNotIn('private', (self.root/result['reports'][-1]).read_text().lower().replace('private response content omitted',''))

    def test_stop_latch_and_invalid_settings_make_no_requests(self):
        (self.root/'stop.json').write_text('{}')
        result=run(self.root,self.settings,collector=self.collector,clock=self.clock)
        self.assertEqual(result['reason'],'requested_stop');self.collector.assert_not_called()
        with self.assertRaises(Block):run(self.root,{**self.settings,'token':'not-a-setting'},collector=self.collector)
        with self.assertRaises(Block):run(self.root,self.settings,interval=1,collector=self.collector)

    def test_interrupt_records_stopped_health(self):
        self.collector.side_effect=KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):run(self.root,self.settings,collector=self.collector,clock=self.clock)
        health=json.loads((self.root/'health.json').read_text())
        self.assertEqual(health['state'],'stopped');self.assertEqual(health['reason'],'interrupted_or_failed')


if __name__=='__main__':unittest.main()
