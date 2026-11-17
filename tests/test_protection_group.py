import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Block
from execution import store_path
from protection_group import members, run
from protection_runner import scope
from service import ProcessLock
import test_protection_runner as fixtures


class ProtectionGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.children = []
        for n in range(2):
            child = fixtures.RunnerTests(); child.setUp(); self.addCleanup(child.doCleanups)
            for name in ('config', 'instrument', 'protection-policy'):
                path = child.w/(name+'.json'); value = json.loads(path.read_text())
                if name == 'config': value['execution_database'] = str(self.root/'account.sqlite')
                else: value.update(token=str(100+n), symbol='TEST'+str(n))
                path.write_text(json.dumps(value))
            child.m['scope_hash'] = scope(child.w)[0]
            (child.w/'state/protection-mandate.json').write_text(json.dumps(child.m))
            self.children.append(child)
        self.settings = {'schema': 'protection_group_v1', 'workdirs': [str(c.w) for c in self.children], 'max_actions': 4}
        self.worker = Mock(return_value={'status': 'STOP_WAITING', 'submitted_this_run': 0})

    def run_case(self, **kwargs):
        return run(self.settings, self.root, worker=self.worker, clock=lambda: fixtures.NOW, sleep=Mock(), **kwargs)

    def test_same_account_scopes_share_database_and_run_sequentially(self):
        rows = members(self.settings, self.root)
        self.assertEqual(rows[0][3], rows[1][3])
        result = self.run_case(cycles=2)
        self.assertEqual(self.worker.call_count, 4)
        self.assertEqual(result['submitted_this_run'], 0)
        self.assertFalse(result['flat_guaranteed'])

    def test_same_account_separate_journals_block_before_work(self):
        path = self.children[1].w/'config.json'; value = json.loads(path.read_text()); value.pop('execution_database')
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(Block, 'share'): self.run_case(cycles=1)
        self.worker.assert_not_called()

    def test_duplicate_scope_and_busy_member_block_before_work(self):
        self.settings['workdirs'][1] = self.settings['workdirs'][0]
        with self.assertRaisesRegex(Block, 'duplicate'): self.run_case(cycles=1)
        self.settings['workdirs'][1] = str(self.children[1].w)
        with ProcessLock(self.children[1].w/'state/protection-runner.lock'):
            with self.assertRaises(Block): self.run_case(cycles=1)
        self.worker.assert_not_called()

    def test_one_failure_revokes_all_and_prevents_restart(self):
        self.worker.side_effect = TimeoutError('private response')
        with self.assertRaises(TimeoutError): self.run_case(cycles=2)
        self.assertEqual(self.worker.call_count, 1)
        self.assertTrue(all((c.w/'state/protection-revoked.json').exists() for c in self.children))
        with self.assertRaises(Block): self.run_case(cycles=2)
        self.assertEqual(self.worker.call_count, 1)
        health = (self.root/'health.json').read_text()
        self.assertNotIn('private response', health)
        self.assertEqual(json.loads(health)['status'], 'FAILED_REVOKED')

    def test_action_budget_and_flat_observations(self):
        self.settings['max_actions'] = 1
        self.worker.return_value = {'status': 'REVIEW_STOP', 'submitted_this_run': 1}
        self.assertEqual(self.run_case(cycles=2)['status'], 'ACTION_LIMIT')
        self.assertEqual(self.worker.call_count, 1)
        self.worker.reset_mock(); self.worker.return_value = {'status': 'FLAT_OBSERVED', 'submitted_this_run': 0}
        self.assertEqual(self.run_case(cycles=2)['status'], 'ALL_SCOPES_FLAT_OBSERVED')
        self.assertEqual(self.worker.call_count, 2)

    def test_scope_change_between_members_stops_all(self):
        def work(*args, **kwargs):
            path = self.children[1].w/'config.json'; value = json.loads(path.read_text()); value['flat_by'] = '15:01'
            path.write_text(json.dumps(value))
            return {'status': 'STOP_WAITING', 'submitted_this_run': 0}
        self.worker.side_effect = work
        with self.assertRaises(Block): self.run_case(cycles=1)
        self.assertEqual(self.worker.call_count, 1)

    def test_existing_local_history_cannot_be_abandoned(self):
        child = self.children[0]; (child.w/'state/execution.sqlite').write_bytes(b'preserve this history')
        with self.assertRaisesRegex(Block, 'local execution history'):
            store_path(child.w, json.loads((child.w/'config.json').read_text()))


if __name__ == '__main__': unittest.main()
