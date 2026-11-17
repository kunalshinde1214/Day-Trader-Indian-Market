from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Block, instant
from history_service import run


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.clock = lambda: instant('2026-11-16T10:00:00+05:30')
        self.collector = Mock(side_effect=self.collect)
        self.archiver = Mock(return_value={'deferred': 0})

    def collect(self, start, end, clock):
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        return {'schema': 'india_news_v1', 'records': [], 'sources': [
            {'requested_session': (first+timedelta(days=i)).isoformat(), 'status': 'COLLECTED', 'rejected': 0}
            for i in range((last-first).days+1)]}

    def run_case(self, **kwargs):
        return run(self.root, '2026-11-01', '2026-11-10', collector=self.collector,
                   archiver=self.archiver, clock=self.clock, **kwargs)

    def test_resume_without_refetching_completed_windows(self):
        self.assertEqual(self.run_case()['next_date'], '2026-11-04')
        result = self.run_case(windows=3)
        self.assertTrue(result['requested_window_collected'])
        self.assertFalse(result['full_filing_coverage'])
        self.assertEqual(self.collector.call_args_list[1].args, ('2026-11-04', '2026-11-06'))
        self.run_case(); self.assertEqual(self.collector.call_count, 4)

    def test_failed_or_rejected_date_does_not_advance(self):
        for status, rejected in [('FAILED', 0), ('PARTIAL', 1), ('COLLECTED', 2)]:
            report = self.collect('2026-11-01', '2026-11-03', self.clock)
            report['sources'][1].update(status=status, rejected=rejected)
            self.collector.side_effect = None; self.collector.return_value = report
            result = self.run_case()
            self.assertEqual(result['next_date'], '2026-11-01')
            self.assertFalse((self.root/'checkpoint.json').exists())

    def test_partial_window_does_not_erase_completed_history(self):
        self.run_case()
        self.collector.side_effect = OSError('network')
        with self.assertRaises(OSError): self.run_case()
        self.assertEqual(json.loads((self.root/'checkpoint.json').read_text())['next_date'], '2026-11-04')

    def test_archiver_failure_does_not_advance(self):
        self.archiver.side_effect = OSError('disk')
        with self.assertRaises(OSError): self.run_case()
        self.assertFalse((self.root/'checkpoint.json').exists())

    def test_changed_window_and_stop_latch(self):
        self.run_case()
        with self.assertRaises(Block):
            run(self.root, '2026-11-02', '2026-11-10', collector=self.collector, archiver=self.archiver, clock=self.clock)
        (self.root/'stop.json').write_text('{}')
        self.assertEqual(self.run_case()['reason'], 'requested_stop')


if __name__ == '__main__': unittest.main()
