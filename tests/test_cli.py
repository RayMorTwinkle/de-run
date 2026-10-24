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

    def test_provider_error_before_any_message_is_reported_promptly(self):
        tid = self.start('EARLY_PROVIDER')['tasks'][0]['task_id']
        code, result = self.call('wait', tid, '--timeout', '3', check=False)
        self.assertEqual(code, 1, result)
        task = result['tasks'][0]
        self.assertEqual(task['status'], 'failed')
        self.assertIn('ProviderNoProvidersError', json.dumps(task['error']))
        self.assertIn('de-run models', task['error_hint'])
        self.assertEqual(task['history'][0]['error'], task['error'])
        self.assertEqual(self.call('result', tid, check=False)[0], 1)
        # A new turn in the same session must not inherit the prior error event.
        self.call('send', tid, 'FAST recovered')
        recovered = self.call('wait', tid, '--timeout', '5')[1]['tasks'][0]
        self.assertIsNone(recovered['error'])
        self.assertIsNone(recovered['error_hint'])
        self.assertEqual(recovered['session_id'], task['session_id'])

    def test_error_stream_loss_aborts_instead_of_hiding_failures(self):
        tid = self.start('SLOW STREAM_LOST')['tasks'][0]['task_id']
        code, result = self.call('wait', tid, '--timeout', '3', check=False)
        self.assertEqual(code, 1, result)
        self.assertEqual(result['tasks'][0]['status'], 'failed')
        self.assertIn('事件流', result['tasks'][0]['error'])
        self.assertTrue(self.events('abort'))

    def test_models_lists_connected_providers_without_submitting_work(self):
        result = self.call('models', '--dir', str(self.workspace))[1]
        self.assertEqual(result['models'], [dict(model='deveco/GLM-5.1', name='GLM 5.1', provider_default=False),
                                           dict(model='deveco/GLM-5.3', name='GLM 5.3',
                                                provider_default=True)])
        self.assertEqual(result['default_model'], 'deveco/GLM-5.3')
        self.assertEqual(result['default_source'], 'provider_default')
        self.assertEqual(result['dir'], str(self.workspace.resolve()))
        self.assertFalse(self.events('create'))
        self.assertFalse(self.events('prompt'))

    def test_bare_invocation_and_help_show_live_default_without_submitting_work(self):
        for args in ([], ['--help']):
            p = subprocess.run(CLI + args, env=self.env, cwd=self.workspace, text=True,
                               capture_output=True, timeout=10)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn('现有模型列表', p.stdout)
            self.assertIn('deveco/GLM-5.3（默认）', p.stdout)
            self.assertIn('deveco/GLM-5.1', p.stdout)
            self.assertNotIn('disconnected/hidden', p.stdout)
        self.assertFalse(self.events('create'))
        self.assertFalse(self.events('prompt'))

    def test_unspecified_model_is_explicitly_submitted_and_persisted(self):
        tid = self.start()['tasks'][0]['task_id']
        result = self.call('wait', tid, '--timeout', '5')[1]['tasks'][0]
        self.assertEqual(self.events('prompt')[0]['body']['model'], {'providerID': 'deveco', 'modelID': 'GLM-5.3'})
        self.assertEqual(result['model'], 'deveco/GLM-5.3')
        self.assertEqual(result['model_source'], 'provider_default')
        self.assertEqual(result['history'][0]['model'], result['model'])

    def test_project_config_overrides_provider_default_and_session_model_is_reused(self):
        (self.workspace / 'config.fixture.json').write_text(json.dumps({'model': 'deveco/GLM-5.1'}))
        self.assertEqual(self.call('models', '--dir', str(self.workspace))[1]['default_model'], 'deveco/GLM-5.1')
        tid = self.start()['tasks'][0]['task_id']
        first = self.call('wait', tid, '--timeout', '5')[1]['tasks'][0]
        self.assertEqual(first['model'], 'deveco/GLM-5.1')
        self.assertEqual(first['model_source'], 'config')
        # A separate batch resuming a native session must honor the session model.
        (self.workspace / 'config.fixture.json').write_text(json.dumps({'model': 'deveco/GLM-5.3'}))
        deadline = time.monotonic() + 5
        while self.call('status', tid)[1]['worker_alive'] and time.monotonic() < deadline:
            time.sleep(.1)
        resumed = self.start(session=first['session_id'])['tasks'][0]['task_id']
        result = self.call('wait', resumed, '--timeout', '5')[1]['tasks'][0]
        self.assertEqual(result['model'], 'deveco/GLM-5.1')
        self.assertEqual(result['model_source'], 'session')

    def test_no_connected_models_fails_before_submitting_prompt(self):
        (self.workspace / 'provider.fixture.json').write_text(json.dumps({'all': [], 'connected': [], 'default': {}}))
        tid = self.start()['tasks'][0]['task_id']
        code, result = self.call('wait', tid, '--timeout', '5', check=False)
        self.assertEqual(code, 1)
        self.assertIn('没有已连接的可用模型', result['tasks'][0]['error'])
        self.assertFalse(self.events('prompt'))

    def test_model_discovery_tolerates_slow_project_initialization(self):
        (self.workspace / 'provider-delay.fixture.json').write_text('6')
        result = self.call('models', '--dir', str(self.workspace))[1]
        self.assertEqual(result['default_model'], 'deveco/GLM-5.3')
        self.assertFalse(self.events('prompt'))

    def test_event_error_is_scoped_to_the_correct_session(self):
        tasks = self.root / 'tasks.json'
        tasks.write_text(json.dumps([dict(dir=str(self.workspace), prompt='EARLY_PROVIDER'),
                                    dict(dir=str(self.workspace), prompt='FAST sibling')]))
        started = self.call('--tasks', str(tasks))[1]
        code, result = self.call('wait', started['run_id'], '--timeout', '5', check=False)
        self.assertEqual(code, 1)
        failed, ok = result['tasks']
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(ok['status'], 'ok')
        self.assertIsNone(ok['error'])

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


class ModelSelectionTest(unittest.TestCase):
    def setUp(self):
        self.models = [dict(model='custom/other', provider_default=True),
                       dict(model='deveco/GLM-5.1', provider_default=False),
                       dict(model='deveco/GLM-5.3', provider_default=True)]

    def test_session_model_precedes_agent_and_project_model(self):
        config = {'model': 'custom/other', 'default_agent': 'plan'}
        agents = [dict(name='plan', model={'providerID': 'deveco', 'modelID': 'GLM-5.3'})]
        result = module.choose_model(self.models, config, agents, {'providerID': 'deveco', 'id': 'GLM-5.1'})
        self.assertEqual(result, ('deveco/GLM-5.1', 'session'))
        self.assertEqual(module.choose_model(self.models, config, agents), ('deveco/GLM-5.3', 'agent'))

    def test_unavailable_preferences_fall_back_to_connected_provider_default(self):
        self.assertEqual(module.choose_model(self.models, {'model': 'disconnected/hidden'}, [], 'gone/model'),
                         ('deveco/GLM-5.3', 'provider_default'))

    def test_fallback_is_deterministic_without_a_provider_default(self):
        for row in self.models:
            row['provider_default'] = False
        self.assertEqual(module.choose_model(list(reversed(self.models)), {}, []),
                         ('deveco/GLM-5.1', 'first_available'))

    def test_native_session_recovers_model_from_messages_when_metadata_is_absent(self):
        runner = module.Runner(None, None, None, {})
        runner.catalogs['/workspace'] = (self.models, {}, [])
        runner.api = unittest.mock.Mock(return_value=[
            dict(info=dict(role='user', model={'providerID': 'deveco', 'modelID': 'GLM-5.1'})),
            dict(info=dict(role='assistant', id='msg_reply'))])
        self.assertEqual(runner.resolve_model({'dir': '/workspace', 'session_id': 'ses_test', 'model': 'gone/model'}, {}),
                         ('deveco/GLM-5.1', 'session'))


class PollingTest(unittest.TestCase):
    def setUp(self):
        self.runner = module.Runner(None, None, None, {'timeout': 900})
        self.runner.persist = unittest.mock.Mock()
        self.runner.release_session = unittest.mock.Mock()
        self.runner.abort = unittest.mock.Mock()
        self.events = unittest.mock.Mock()
        self.events.get_error.return_value = None
        self.runner.events['task_test'] = self.events
        self.task = dict(task_id='task_test', session_id='ses_test', message_id='msg_test',
                         started_at=990, idle_since=None, round=1, history=[], pending=[], status='running')
        self.state = {}
        self.messages = []
        self.runner.api = unittest.mock.Mock(side_effect=lambda t, method, route:
            self.state if route == '/session/status' else self.messages)

    def poll(self):
        with unittest.mock.patch.object(module.time, 'time', return_value=1000):
            self.runner.poll(self.task)

    def test_startup_idle_has_grace_and_busy_resets_it(self):
        self.poll()
        self.assertEqual(self.task['status'], 'running')
        self.assertEqual(self.task['idle_since'], 1000)
        self.runner.abort.assert_not_called()
        self.state['ses_test'] = {'type': 'busy'}
        self.poll()
        self.assertIsNone(self.task['idle_since'])

    def test_idle_without_messages_or_error_cannot_run_forever(self):
        self.task['idle_since'] = 1000 - module.IDLE_GRACE - 1
        self.poll()
        self.runner.abort.assert_called_once()
        self.assertEqual(self.task['status'], 'incomplete')
        self.assertIn('没有完整回复', self.task['error'])
        self.assertEqual(self.task['history'][0]['status'], 'incomplete')

    def test_busy_error_preserves_cause_across_confirmed_abort_and_holds_queue(self):
        self.state['ses_test'] = {'type': 'busy'}
        error = {'name': 'UnknownError', 'data': {'message': 'ProviderNoProvidersError'}}
        self.events.get_error.return_value = error
        self.task['pending'] = ['must not auto-run']
        self.runner.abort.side_effect = lambda t: t.update(error='abort result')
        self.poll()
        self.runner.abort.assert_called_once()
        self.assertEqual(self.task['status'], 'failed')
        self.assertEqual(self.task['error'], error)
        self.assertEqual(self.task['pending'], ['must not auto-run'])

    def test_message_error_is_not_masked_by_a_disconnected_stream(self):
        error = {'name': 'APIError', 'data': {'message': 'provider failed'}}
        self.messages.append(dict(info=dict(role='assistant', parentID='msg_test', error=error), parts=[]))
        self.events.get_error.side_effect = RuntimeError('stream disconnected')
        self.poll()
        self.assertEqual(self.task['status'], 'failed')
        self.assertEqual(self.task['error'], error)
        self.events.get_error.assert_not_called()


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

    def test_token_fallback_includes_cache_but_never_prior_turns(self):
        messages = [dict(info=dict(role='assistant', parentID='old', tokens={'total': 999}), parts=[]),
                    dict(info=dict(role='assistant', parentID='new', tokens={
                        'input': 10, 'output': 2, 'reasoning': 3, 'cache': {'read': 100, 'write': 20}}), parts=[])]
        self.assertEqual(module.round_result(messages, 'new')['tokens'], 135)


if __name__ == '__main__':
    import unittest.mock
    unittest.main()
