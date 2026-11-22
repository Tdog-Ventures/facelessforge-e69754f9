import concurrent.futures
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import service


class OpsTest(unittest.TestCase):
    def setUp(self):
        self.server = service.ThreadingHTTPServer(('127.0.0.1', 0), service.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.key = patch.object(service, 'KEY', 'test-key-' + 'a' * 48)
        self.key.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.key.stop()

    def request(self, method, path, body=None, auth=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        headers = {'Authorization': 'Bearer ' + service.KEY} if auth else {}
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        result = conn.getresponse()
        data = json.loads(result.read())
        conn.close()
        return result.status, data

    def test_auth_and_route_boundaries(self):
        with patch.object(service, 'status', return_value={'running': True}) as status:
            self.assertEqual(self.request('GET', '/api/ops/status', auth=False)[0], 401)
            status.assert_not_called()
            self.assertEqual(self.request('GET', '/api/ops/status'), (200, {'running': True}))
            self.assertEqual(self.request('GET', '/api/ops/status?command=whoami')[0], 404)
            self.assertEqual(self.request('POST', '/api/ops/exec', {'command': 'whoami'})[0], 404)

    def test_strict_arguments_and_missing_helpers(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, 'ROOT', Path(tmp)):
            self.assertEqual(self.request('POST', '/api/ops/deploy', {'commit': 'main;whoami'})[0], 400)
            self.assertEqual(self.request('POST', '/api/ops/deploy', {'commit': 'a' * 40, 'command': 'whoami'})[0], 400)
            self.assertEqual(self.request('POST', '/api/ops/deploy', {'commit': 'a' * 40})[0], 503)
            self.assertEqual(self.request('POST', '/api/ops/restart', {'reason': ''})[0], 400)
            self.assertEqual(self.request('GET', '/api/ops/logs?lines=10000')[0], 400)
            self.assertEqual(self.request('GET', '/api/ops/logs?lines=80&path=/etc/passwd')[0], 400)

    def test_redaction_and_fail_closed_logs(self):
        info = {'Config': {'Env': ['PROVIDER_KEY=provider-super-secret', 'MONGODB_URI=mongodb://user:privatepass@mongo/db']}}
        with patch.object(service, 'container_info', return_value=info):
            clean = service.scrub(service.KEY + ' provider-super-secret mongodb://user:privatepass@mongo/db Authorization=abc-secret Bearer other-secret')
            self.assertNotIn('provider-super-secret', clean)
            self.assertNotIn('privatepass', clean)
            self.assertNotIn(service.KEY, clean)
            self.assertNotIn('other-secret', clean)
        with patch.object(service, 'container_info', side_effect=RuntimeError()):
            self.assertNotIn('some-private-log', service.scrub('some-private-log'))

    def test_failed_idle_guard_prevents_mutation(self):
        task = {'task_id': 'a', 'action': 'restart'}
        with patch.object(service, 'save_task'), patch.object(service, 'idle_guard', side_effect=RuntimeError('Busy')), patch.object(service, 'scrub', side_effect=lambda x: x), patch.object(service.subprocess, 'Popen') as popen:
            service.work(task)
            popen.assert_not_called()
            self.assertEqual(task['status'], 'failed')

    def test_mutation_lock_prevents_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, 'ROOT', Path(tmp)), patch.object(service, 'ACTIVE', 'existing-task'):
            Path(tmp, 'guard-idle.py').touch()
            Path(tmp, 'deploy-revision.sh').touch()
            with self.assertRaises(BlockingIOError):
                service.submit('deploy', {'commit': 'a' * 40})


if __name__ == '__main__':
    unittest.main()
