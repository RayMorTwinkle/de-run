"""CLI/process integration tests using a local fake DevEco server."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, str(ROOT / 'scripts/de-run.py')]
spec = importlib.util.spec_from_file_location('de_run', ROOT / 'scripts/de-run.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CLIIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'deveco'
        fake.write_text('#!' + sys.executable + '\n' + (ROOT / 'tests/fake_deveco.py').read_text())
        fake.chmod(0o755)
        self.home = self.root / 'home'
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.env = dict(os.environ, DE_RUN_HOME=str(self.home), FAKE_DEVECO_HOME=str(self.root / 'fake'),
                        PATH=str(bin_dir) + os.pathsep + os.environ.get('PATH', ''))

    def tearDown(self):
        # Cancel remaining fixture work, then wait for workers to release their lock.
        if self.home.exists():
            for dbfile in self.home.glob('run_*/state.db'):
                with sqlite3.connect(dbfile) as db:
                    tasks = [json.loads(r[0]) for r in db.execute('SELECT data FROM tasks')]
                for t in tasks:
                    if t['status'] not in module.TERMINAL:
                        self.call('cancel', t['task_id'], check=False)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                with unittest.mock.patch.dict(os.environ, self.env):
                    if all(not module.worker_alive(p) for p in self.home.glob('run_*')):
                        break
                time.sleep(.1)
        self.tmp.cleanup()

    def call(self, *args, check=True):
        p = subprocess.run(CLI + list(args) + ['--json'], env=self.env, text=True,
                           capture_output=True, timeout=45)
        try:
            data = json.loads(p.stdout)
        except Exception:
            self.fail('invalid JSON: ' + repr((p.returncode, p.stdout, p.stderr)))
        if check:
            self.assertEqual(p.returncode, 0, data)
        return p.returncode, data

    def start(self, prompt='FAST', **kw):
        args = ['start', '--dir', str(self.workspace), '--prompt', prompt]
        for k, v in kw.items(): args += ['--' + k.replace('_', '-'), str(v)]
        return self.call(*args)[1]

    def running(self, tid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            t = self.call('status', tid)[1]['tasks'][0]
            if t['status'] == 'running' and self.events('prompt'):
                return t
            if t['status'] in module.TERMINAL:
                self.fail(t)
            time.sleep(.1)
        self.fail('never running')

    def events(self, kind):
        path = self.root / 'fake/events.jsonl'
        return [x for x in map(json.loads, path.read_text().splitlines()) if x['kind'] == kind] if path.exists() else []

    def test_detached_start_and_durable_result(self):
        before = time.monotonic()
        start = self.start('SLOW')
        self.assertLess(time.monotonic() - before, 2)
        tid = start['tasks'][0]['task_id']
        self.running(tid)
        code, _ = self.call('wait', tid, '--timeout', '.1', check=False)
        self.assertEqual(code, 124)
        self.assertEqual(self.call('status', tid)[1]['tasks'][0]['status'], 'running')
        self.call('send', tid, 'FAST replacement')
        result = self.call('wait', start['run_id'], '--timeout', '10')[1]
        self.assertEqual(result['tasks'][0]['final_result'], 'report: FAST replacement')
        self.assertEqual(result['tasks'][0]['history'][0]['status'], 'interrupted')
        self.assertTrue(self.events('abort'))
        self.assertEqual(len({e['sid'] for e in self.events('prompt')}), 1)
        self.assertLess(self.events('abort')[0]['time'], self.events('prompt')[1]['time'])
        self.assertEqual(self.call('result', tid)[1]['tasks'][0]['status'], 'ok')

    def test_cancel_running(self):
        tid = self.start('SLOW')['tasks'][0]['task_id']
        self.running(tid)
        self.assertEqual(self.call('cancel', tid)[1]['status'], 'cancelled')
        code, result = self.call('wait', tid, '--timeout', '5', check=False)
        self.assertEqual(code, 1)
        self.assertEqual(result['tasks'][0]['status'], 'cancelled')

    def test_cancel_queued_does_not_dispatch(self):
        tasks = self.root / 'tasks.json'
        tasks.write_text(json.dumps([dict(dir=str(self.workspace), prompt='SLOW first'),
                                    dict(dir=str(self.workspace), prompt='FAST second')]))
        result = self.call('--tasks', str(tasks), '--max-parallel', '1')[1]
        self.running(result['tasks'][0]['task_id'])
        self.call('cancel', result['tasks'][1]['task_id'])
        self.assertEqual(len(self.events('prompt')), 1)
        self.call('cancel', result['tasks'][0]['task_id'])

    def test_queue_and_resume_finished_worker(self):
        tid = self.start()['tasks'][0]['task_id']
        self.call('send', tid, 'FAST queued', '--queue')
        result = self.call('wait', tid, '--timeout', '10')[1]['tasks'][0]
        self.assertEqual(len(result['history']), 2)
        deadline = time.monotonic() + 5
        while self.call('status', tid)[1]['worker_alive'] and time.monotonic() < deadline:
            time.sleep(.1)
        self.call('send', tid, 'FAST third')
        third = self.call('wait', tid, '--timeout', '10')[1]['tasks'][0]
        self.assertEqual(third['session_id'], result['session_id'])
        self.assertEqual(len(third['history']), 3)
        self.assertEqual(third['final_result'], 'report: FAST third')

    def test_provider_failure_not_success(self):
        tid = self.start('FAIL')['tasks'][0]['task_id']
        code, result = self.call('wait', tid, '--timeout', '10', check=False)
        self.assertEqual(code, 1)
        self.assertEqual(result['tasks'][0]['status'], 'failed')
        self.assertEqual(self.call('result', tid, check=False)[0], 1)

    def test_length_is_incomplete(self):
        tid = self.start('LENGTH')['tasks'][0]['task_id']
        code, result = self.call('wait', tid, '--timeout', '10', check=False)
        self.assertEqual(code, 1)
        self.assertEqual(result['tasks'][0]['status'], 'incomplete')

    def test_execution_timeout_aborts(self):
        tid = self.start('SLOW', timeout=1)['tasks'][0]['task_id']
        code, result = self.call('wait', tid, '--timeout', '10', check=False)
        self.assertEqual(code, 1)
        self.assertEqual(result['tasks'][0]['status'], 'timeout')
        self.assertTrue(self.events('abort'))

    def test_failed_abort_never_sends_replacement(self):
        tid = self.start('SLOW ABORT_FAIL')['tasks'][0]['task_id']
        self.running(tid)
        code, result = self.call('send', tid, 'FAST unsafe', check=False)
        self.assertEqual(code, 1)
        self.assertIn('abort', result['error'])
        self.assertEqual(len(self.events('prompt')), 1)

    def test_explicit_model_and_directory_mismatch(self):
        tid = self.start(model='custom/test')['tasks'][0]['task_id']
        result = self.call('wait', tid, '--timeout', '10')[1]['tasks'][0]
        self.assertEqual(self.events('prompt')[0]['body']['model'], {'providerID': 'custom', 'modelID': 'test'})
        other = self.root / 'other'
        other.mkdir()
        start = self.call('--dir', str(other), '--prompt', 'FAST', '--session', result['session_id'])[1]
        code, result = self.call('wait', start['run_id'], '--timeout', '10', check=False)
        self.assertEqual(code, 1)
        self.assertIn('目录不一致', result['tasks'][0]['error'])
        self.assertEqual(len(self.events('prompt')), 1)

    def test_result_before_completion_and_validation(self):
        tid = self.start('SLOW')['tasks'][0]['task_id']
        self.assertEqual(self.call('result', tid, check=False)[0], 2)
        self.assertEqual(self.call('status', '../escape', check=False)[0], 2)
        self.assertEqual(self.call('--dir', str(self.root / 'absent'), '--prompt', 'FAST', check=False)[0], 2)
        self.call('cancel', tid)

    def test_session_lock_rejects_second_batch(self):
        first = self.start('SLOW')['tasks'][0]['task_id']
        task = self.running(first)
        second = self.start('FAST overlapping', session=task['session_id'])['tasks'][0]['task_id']
        code, result = self.call('wait', second, '--timeout', '10', check=False)
        self.assertEqual(code, 1)
        self.assertIn('其他 de-run', result['tasks'][0]['error'])
        self.assertEqual(self.call('status', first)[1]['tasks'][0]['status'], 'running')
        self.call('cancel', first)

    def test_server_crash_does_not_replay_work(self):
        tid = self.start('SLOW')['tasks'][0]['task_id']
        self.running(tid)
        os.kill(self.events('server')[-1]['pid'], signal.SIGTERM)
        code, result = self.call('wait', tid, '--timeout', '10', check=False)
        self.assertEqual(code, 1)
        self.assertEqual(result['tasks'][0]['status'], 'orphaned')
        self.assertEqual(len(self.events('prompt')), 1)
        self.assertEqual(self.call('send', tid, 'FAST retry', check=False)[0], 2)

    def test_worker_sigterm_aborts_before_shutdown(self):
        started = self.start('SLOW')
        tid = started['tasks'][0]['task_id']
        self.running(tid)
        with sqlite3.connect(self.home / started['run_id'] / 'state.db') as db:
            pid = json.loads(db.execute('SELECT data FROM settings').fetchone()[0])['worker_pid']
        os.kill(pid, signal.SIGTERM)
        code, result = self.call('wait', tid, '--timeout', '10', check=False)
        self.assertEqual(code, 1)
        self.assertEqual(result['tasks'][0]['status'], 'cancelled')
        self.assertTrue(self.events('abort'))


class SummaryTest(unittest.TestCase):
    def test_lost_worker_with_unacknowledged_control_is_terminal_failure(self):
        result = dict(worker_alive=False, pending_commands=[{'kind': 'send'}],
                      tasks=[{'status': 'orphaned'}])
        self.assertTrue(module.finished(result))
        result['worker_alive'] = True
        self.assertFalse(module.finished(result))

    def test_multiple_tool_steps_and_old_rounds(self):
        messages = [dict(info=dict(role='assistant', parentID='old', tokens={'total': 999}), parts=[]),
                    dict(info=dict(role='assistant', parentID='new', tokens={'total': 3}, finish='tool-calls'),
                         parts=[dict(type='tool', tool='read')]),
                    dict(info=dict(role='assistant', parentID='new', tokens={'total': 4}, finish='stop', time={'completed': 1}),
                         parts=[dict(type='text', text='done')])]
        result = module.round_result(messages, 'new')
        self.assertEqual(result['tokens'], 7)
        self.assertEqual(result['actions'], {'read': 1})
        self.assertEqual(result['final_result'], 'done')


if __name__ == '__main__':
    import unittest.mock
    unittest.main()
