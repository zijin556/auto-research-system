"""Mock-only tests: these never contact a model or read real credentials."""
import base64
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from notebook import semantic_jobs as jobs
from notebook.semantic import prepare_semantic_input
from notebook.store import ConflictError, Store


class SemanticJobTests(unittest.TestCase):
    def setUp(self):
        runtime = Path(__file__).resolve().parents[1] / '.runtime'
        runtime.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='semantic-job-test-', dir=runtime)
        self.store = Store(Path(self.temp.name))
        self.manager = jobs.SemanticJobs(self.store)
        self.payload = {'text': '用户：讨论两个误差问题，完整原文应保留。',
                        'files': [{'filename': 'sample.txt', 'base64': base64.b64encode(b'test result\n').decode()}],
                        'context': {}}
        self.request = {**self.payload, 'request_id': 'test-semantic-job-001'}
        self.provider = {'available': True, 'provider': 'Codex', 'auth': 'chatgpt', 'model': 'gpt-test', 'message': 'mock account'}
        self.provider_patch = patch.object(jobs, 'provider_status', return_value=self.provider)
        self.status_mock = self.provider_patch.start()
        self.thread_patch = patch.object(jobs.threading, 'Thread')
        self.thread_mock = self.thread_patch.start()

    def tearDown(self):
        self.thread_patch.stop()
        self.provider_patch.stop()
        self.temp.cleanup()

    def test_same_request_id_starts_once_conflicting_payload_rejected(self):
        first = self.manager.start(self.request)
        self.assertEqual(first['status'], 'queued')
        self.assertEqual(self.manager.start(self.request)['job_id'], first['job_id'])
        self.assertEqual(self.status_mock.call_count, 1)
        self.assertEqual(self.thread_mock.return_value.start.call_count, 1)
        with self.assertRaises(ConflictError):
            self.manager.start({**self.request, 'text': 'different input'})
        with self.assertRaises(ConflictError):
            self.manager.start({**self.request, 'request_id': 'test-semantic-job-002'})
        self.assertEqual(self.thread_mock.return_value.start.call_count, 1)

    def test_cancel_terminates_registered_process_and_preserves_input(self):
        item = self.manager.start(self.request)
        process = MagicMock()
        process.poll.return_value = None
        self.manager.jobs[item['job_id']]['status'] = 'running'
        self.manager.processes[item['job_id']] = process
        result = self.manager.cancel(item['job_id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['input_text'], self.payload['text'])
        self.assertEqual(self.manager._input(item['job_id'])['files'], self.payload['files'])
        process.terminate.assert_called_once()
        self.manager.cancel(item['job_id'])
        process.terminate.assert_called_once()
        self.assertEqual(self.manager.start(self.request)['status'], 'cancelled')
        self.assertEqual(self.thread_mock.return_value.start.call_count, 1)

    def test_restart_marks_interrupted_job_failed_without_rerunning(self):
        self.manager.start(self.request)
        recovered = jobs.SemanticJobs(self.store)
        item = recovered.snapshot(self.request['request_id'])
        self.assertEqual(item['status'], 'failed')
        self.assertIn('重新启动', item['error'])
        self.assertEqual(item['input_text'], self.payload['text'])
        self.assertEqual(recovered.start(self.request)['status'], 'failed')
        self.assertEqual(self.thread_mock.return_value.start.call_count, 1)
        retry = recovered.start({'request_id': 'test-semantic-retry-001', 'resume_job_id': self.request['request_id']})
        self.assertEqual(retry['status'], 'queued')
        self.assertEqual(retry['input_text'], self.payload['text'])
        self.assertEqual(recovered._input(retry['job_id'])['files'], self.payload['files'])
        self.assertEqual(self.thread_mock.return_value.start.call_count, 2)

    def test_model_failure_has_no_rule_fallback_no_automatic_retry_no_research_write(self):
        self.manager.start(self.request)
        process = MagicMock()
        process.stdin = io.BytesIO()
        process.returncode = 1
        process.poll.return_value = 1
        process.communicate.return_value = (None, None)
        def failing_process(*args, **kwargs):
            kwargs['stderr'].write(b'429 quota exhausted')
            return process
        prepared = prepare_semantic_input(self.store, self.payload)
        with patch.object(jobs, 'command_for', return_value=['fake-model']), \
             patch.object(jobs.subprocess, 'Popen', side_effect=failing_process) as invoke:
            self.manager._run(self.request['request_id'], prepared, self.payload)
            self.assertEqual(invoke.call_count, 1)
            result = self.manager.snapshot(self.request['request_id'])
            self.assertEqual(result['status'], 'failed')
            self.assertIn('额度', result['error'])
            self.assertEqual(result['input_text'], self.payload['text'])
            self.assertEqual(self.store.state()['questions'], [])
            started_threads = self.thread_mock.return_value.start.call_count
            self.assertEqual(self.manager.start(self.request)['status'], 'failed')
            self.assertEqual(invoke.call_count, 1)
            self.assertEqual(self.thread_mock.return_value.start.call_count, started_threads)

    def test_unavailable_chatgpt_provider_never_starts_api_fallback(self):
        self.status_mock.return_value = {'available': False, 'message': 'Use ChatGPT login; API fallback forbidden'}
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'unit-test-placeholder', 'CODEX_API_KEY': 'unit-test-placeholder'}), \
             patch.object(jobs.subprocess, 'Popen') as invoke:
            with self.assertRaises(ValueError):
                self.manager.start(self.request)
        invoke.assert_not_called()
        self.thread_mock.return_value.start.assert_not_called()
        self.assertEqual(self.manager.jobs, {})

    def test_timeout_terminates_child_without_archiving_or_retry(self):
        self.manager.start(self.request)
        process = MagicMock()
        process.stdin = io.BytesIO()
        process.poll.side_effect = [None, 1]
        prepared = prepare_semantic_input(self.store, self.payload)
        with patch.object(jobs, 'command_for', return_value=['fake-model']), \
             patch.object(jobs.subprocess, 'Popen', return_value=process) as invoke, \
             patch.object(jobs.time, 'monotonic', side_effect=[0, 301]):
            self.manager._run(self.request['request_id'], prepared, self.payload)
        self.assertEqual(invoke.call_count, 1)
        process.terminate.assert_called_once()
        result = self.manager.snapshot(self.request['request_id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('五分钟', result['error'])
        self.assertEqual(result['input_text'], self.payload['text'])
        self.assertEqual(self.store.state()['questions'], [])


class SemanticProviderBoundaryTests(unittest.TestCase):
    def test_api_credentials_removed_and_chatgpt_forced_without_config_side_effect(self):
        environment = {'OPENAI_API_KEY': 'unit-test-placeholder', 'CODEX_API_KEY': 'unit-test-placeholder',
                       'OPENAI_BASE_URL': 'https://invalid.example', 'CODEX_THREAD_ID': 'unrelated-thread', 'PATH': 'keep'}
        with patch.dict(os.environ, environment, clear=True):
            cleaned = jobs.cli_environment()
        self.assertEqual(cleaned['PATH'], 'keep')
        for key in environment:
            if key != 'PATH': self.assertNotIn(key, cleaned)
        with patch.object(jobs, 'cli_path', return_value=Path('fake-codex.exe')):
            command = jobs.command_for(Path('job'), Path('schema.json'), 'gpt-test')
        self.assertIn('--ignore-user-config', command)
        self.assertIn('--ephemeral', command)
        self.assertIn('read-only', command)
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertIn('model_provider="openai"', command)
        self.assertIn('features.shell_tool=false', command)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)

    def test_api_key_login_does_not_count_as_chatgpt_authentication(self):
        with patch.object(jobs, 'cli_path', return_value=Path('fake-codex.exe')), \
             patch.object(jobs, 'model_name', return_value='gpt-test'), \
             patch.object(jobs.subprocess, 'run', return_value=subprocess.CompletedProcess(['fake'], 0, 'Logged in using API key', '')) as check:
            result = jobs.provider_status()
        self.assertFalse(result['available'])
        self.assertEqual(result['auth'], '')
        self.assertEqual(check.call_args.args[0][-2:], ['login', 'status'])


if __name__ == '__main__':
    unittest.main()
