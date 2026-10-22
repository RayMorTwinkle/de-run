"""List persisted tasks without starting DevEco or consuming model calls."""
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

CLI = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/de-run.py')]


class ListCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = dict(os.environ, DE_RUN_HOME=str(self.home), PATH='/unavailable')
        self.locks = []

    def tearDown(self):
        for lock in self.locks:
            lock.close()
        self.tmp.cleanup()

    def seed(self, suffix, statuses, alive=False):
        rid = 'run_' + suffix
        p = self.home / rid
        p.mkdir()
        now = time.time()
        with sqlite3.connect(p / 'state.db') as db:
            db.executescript('CREATE TABLE settings(data TEXT);'
                             'CREATE TABLE tasks(id TEXT, ordinal INTEGER, data TEXT);'
                             'CREATE TABLE commands(id TEXT, task_id TEXT, kind TEXT, state TEXT, response TEXT);')
            db.execute('INSERT INTO settings VALUES (?)',
                       (json.dumps(dict(max_parallel=6, created_at=now - 60)),))
            for i, status in enumerate(statuses, 1):
                tid = 'task_' + suffix + '_' + str(i)
                task = dict(task_id=tid, status=status, title='task ' + str(i),
                            session_id='ses_PRIVATE', updated_at=now - i,
                            dir=str(self.home), final_result='REPORT_PRIVATE', error=None)
                db.execute('INSERT INTO tasks VALUES (?, ?, ?)', (tid, i, json.dumps(task)))
        if alive:
            lock = open(p / 'worker.lock', 'a')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.locks.append(lock)
        return rid

    def call(self, *args, json_output=True, code=0):
        p = subprocess.run(CLI + list(args) + (['--json'] if json_output else []),
                           env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(p.returncode, code, (p.stdout, p.stderr))
        return json.loads(p.stdout) if json_output else p

    def test_all_active_and_orphaned_plus_ten_recent(self):
        rid = self.seed('a' * 12, ['queued'] * 12 + ['running', 'interrupting', 'orphaned'] + ['ok'] * 13,
                        alive=True)
        result = self.call('list')
        self.assertEqual(len(result['active']), 14)
        self.assertEqual(result['active'][0]['status'], 'running')
        self.assertEqual(len(result['attention']), 1)
        self.assertEqual(len(result['recent']), 10)
        self.assertEqual(result['summary']['total_recent'], 13)
        self.assertEqual(result['recent'][0]['task_id'], 'task_' + rid[4:] + '_16')
        self.assertEqual(result['active'][0]['session_id'], 'ses_PRIVATE')
        self.assertEqual(len(self.call('list', '--recent', '30')['recent']), 13)
        active = self.call('list', '--active')
        self.assertEqual(len(active['active']), 14)
        self.assertFalse(active['attention'])
        self.assertFalse(active['recent'])

    def test_missing_worker_becomes_attention_and_run_filter(self):
        first = self.seed('a' * 12, ['running', 'queued', 'ok'])
        self.seed('b' * 12, ['ok'])
        result = self.call('list', '--run', first, '--recent', '0')
        self.assertFalse(result['active'])
        self.assertFalse(result['recent'])
        self.assertEqual(len(result['attention']), 2)
        self.assertTrue(all(t['status'] == 'orphaned' for t in result['attention']))
        self.assertTrue(all(t['run_id'] == first for t in result['attention']))

    def test_human_list_and_status_hide_sessions_until_verbose(self):
        rid = self.seed('a' * 12, ['ok'])
        tid = 'task_' + rid[4:] + '_1'
        for args in [('list',), ('status', tid), ('result', tid)]:
            plain = self.call(*args, json_output=False).stdout
            self.assertIn(tid, plain)
            self.assertNotIn('ses_PRIVATE', plain)
            detailed = self.call(*args, '--verbose', json_output=False).stdout
            self.assertIn('ses_PRIVATE', detailed)
        self.assertNotIn('REPORT_PRIVATE', self.call('list', json_output=False).stdout)
        self.assertIn('ses_PRIVATE', self.call('status', tid)['tasks'][0]['session_id'])

    def test_empty_and_invalid_filters(self):
        result = self.call('list')
        self.assertFalse(result['active'] or result['attention'] or result['recent'])
        self.assertIn('没有符合条件', self.call('list', json_output=False).stdout)
        self.call('list', '--recent', '-1', code=2)
        self.call('list', '--run', 'task_' + 'a' * 12 + '_1', code=2)
        self.call('list', '--run', 'run_' + 'a' * 12, code=2)

    def test_corrupted_batch_is_reported_without_hiding_healthy_tasks(self):
        self.seed('a' * 12, ['ok'])
        broken = self.home / ('run_' + 'b' * 12)
        broken.mkdir()
        (broken / 'state.db').write_text('not sqlite')
        result = self.call('list', code=1)
        self.assertEqual(len(result['recent']), 1)
        self.assertEqual(result['errors'][0]['run_id'], broken.name)

    def test_control_receipt_hides_session_but_json_keeps_it(self):
        # Rendering uses the same path for send/cancel receipts.
        import importlib.util
        import contextlib
        import io
        spec = importlib.util.spec_from_file_location('de_run', CLI[1])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        value = dict(task_id='task_example', session_id='ses_PRIVATE', status='queued')
        for verbose in (False, True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                module.emit(value, verbose=verbose)
            self.assertEqual('ses_PRIVATE' in out.getvalue(), verbose)
        self.assertEqual(value['session_id'], 'ses_PRIVATE')


if __name__ == '__main__':
    unittest.main()
