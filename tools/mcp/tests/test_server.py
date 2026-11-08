import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVER = Path(__file__).resolve().parents[1] / 'server.py'
UUID = '1234abcd-12ab-34cd-56ef-1234567890ab'
COMMIT = 'a' * 40

TOOLS_NINE = {
    'connection_status', 'render_video', 'render_status', 'cancel_render',
    'ops_status', 'ops_logs', 'ops_task_status', 'ops_restart', 'ops_deploy',
}


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.ops_requests = []
        requests = self.requests
        ops_requests = self.ops_requests
        secrets = {'X-FacelessForge-Key': 'test-secret', 'Authorization': 'Bearer ops-test-key'}

        def collect(store, auth_header, extra=()):
            def handle(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                store.append((self.command, self.path, self.headers.get(auth_header),
                              self.headers.get('X-FacelessForge-Key'), body))
                for key, value in extra:
                    if key in self.path:
                        self.send_response(value[0])
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        self.wfile.write(json.dumps(value[1]).encode())
                        return
                if 'redirect-job' in self.path:
                    self.send_response(302)
                    self.send_header('Location', 'http://127.0.0.1:8000/forbidden')
                    self.end_headers()
                    return
                payload = {'ok': True}
                if 'echo-secret' in self.path:
                    payload = {'echo': secrets['X-FacelessForge-Key']}
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
            return handle

        class APIHandler(BaseHTTPRequestHandler):
            do_GET = do_POST = collect(requests, 'X-FacelessForge-Key',
                                       extra=(('/api/health', (200, {'status': 'ok'})),))

            def log_message(self, *args):
                pass

        class OPSHandler(BaseHTTPRequestHandler):
            do_GET = do_POST = collect(ops_requests, 'Authorization')

            def log_message(self, *args):
                pass

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), APIHandler)
        self.ops_http = ThreadingHTTPServer(('127.0.0.1', 0), OPSHandler)
        for server in (self.http, self.ops_http):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def exchange(self, messages, **environment):
        env = os.environ | {
            'FACELESSFORGE_API_URL': f'http://127.0.0.1:{self.http.server_port}',
            'FACELESSFORGE_OPS_URL': f'http://127.0.0.1:{self.ops_http.server_port}',
            'EXTERNAL_RENDER_API_KEY': 'test-secret',
            'FACELESSFORGE_OPS_KEY': 'ops-test-key',
            'FACELESSFORGE_MCP_ENV_FILE': '/nonexistent/mcp.env',  # deterministic: never read host defaults
        } | environment
        result = subprocess.run([sys.executable, str(SERVER)],
                                input=''.join(json.dumps(m) + '\n' for m in messages),
                                text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def call(self, name, arguments=None, **env):
        return self.exchange([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
             'params': {'name': name, 'arguments': arguments or {}}},
        ], **env)[-1]

    def test_handshake_and_tool_discovery(self):
        replies = self.exchange([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        ])
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-06-18')
        names = {t['name'] for t in replies[1]['result']['tools']}
        self.assertEqual(names, TOOLS_NINE)

    def test_action_annotations(self):
        replies = self.exchange([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        ])
        tools = {t['name']: t['annotations'] for t in replies[1]['result']['tools']}
        read_only = {'connection_status', 'render_status', 'ops_status', 'ops_logs', 'ops_task_status'}
        destructive = {'cancel_render', 'ops_restart', 'ops_deploy'}
        for name, annotations in tools.items():
            self.assertEqual(annotations['readOnlyHint'], name in read_only, name)
            self.assertEqual(annotations['destructiveHint'], name in destructive, name)

    def test_authenticated_render_matches_backend_contract(self):
        payload = {'title': 'A business story', 'script': 'This is a real narration script.', 'target_duration': 60}
        reply = self.call('render_video', payload)
        self.assertFalse(reply['result'].get('isError', False))
        method, path, _, key, body = self.requests[0]
        self.assertEqual((method, path, key), ('POST', '/api/external/render-video', 'test-secret'))
        self.assertEqual(json.loads(body), payload)

    def test_status_and_cancel_use_exact_routes(self):
        self.call('render_status', {'job_id': 'test-job-123'})
        self.call('cancel_render', {'job_id': 'test-job-123'})
        self.assertEqual(self.requests[0][:2], ('GET', '/api/external/render-video-status?job_id=test-job-123'))
        self.assertEqual(self.requests[1][:2], ('POST', '/api/external/render-video/cancel'))

    def test_connection_status_reports_reachability_and_flags_without_secrets(self):
        reply = self.call('connection_status', {})
        self.assertFalse(reply['result'].get('isError', False))
        text = reply['result']['content'][0]['text']
        self.assertIn('"key_configured": true', text)
        self.assertIn('"reachable": true', text)
        self.assertNotIn('test-secret', text)
        self.assertNotIn('ops-test-key', text)
        self.assertEqual(self.ops_requests, [])

    def test_invalid_arguments_never_reach_backend(self):
        for name, args in [('render_video', {'title': 'x', 'script': 'short'}),
                           ('render_status', {'job_id': '../evil?x=1'}),
                           ('render_status', {'job_id': 'test-job-123', 'url': 'http://evil'}),
                           ('render_video', {'title': 'x', 'script': 'a' * 20, 'target_duration': True}),
                           ('ops_deploy', {'commit': 'A' * 40}),
                           ('ops_deploy', {'commit': 'z' * 40}),
                           ('ops_deploy', {'commit': 'a' * 39}),
                           ('ops_deploy', {'commit': COMMIT, 'reason': 'extra'}),
                           ('ops_restart', {'reason': ''}),
                           ('ops_restart', {'reason': 'x' * 301}),
                           ('ops_restart', {'reason': 'ok', 'commit': COMMIT}),
                           ('ops_task_status', {'task_id': '../../etc/passwd'}),
                           ('ops_task_status', {'task_id': UUID.upper()}),
                           ('ops_logs', {'lines': 10}),
                           ('ops_logs', {'lines': 201}),
                           ('ops_logs', {'lines': '80'})]:
            with self.subTest(name=name, args=args):
                self.assertTrue(self.call(name, args)['result']['isError'])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.ops_requests, [])

    def test_ops_reads_hit_exact_bridge_routes(self):
        self.call('ops_status')
        self.call('ops_logs', {'lines': 40})
        self.call('ops_task_status', {'task_id': UUID})
        self.assertEqual([r[:3] for r in self.ops_requests],
                         [('GET', '/api/ops/status', 'Bearer ops-test-key'),
                          ('GET', '/api/ops/logs?lines=40', 'Bearer ops-test-key'),
                          ('GET', '/api/ops/tasks/' + UUID, 'Bearer ops-test-key')])

    def test_ops_writes_submit_exact_bodies(self):
        reply = self.call('ops_restart', {'reason': 'apply patched module'})
        self.assertFalse(reply['result'].get('isError', False))
        self.call('ops_deploy', {'commit': COMMIT})
        method, path, auth, _, body = self.ops_requests[0]
        self.assertEqual((method, path, auth), ('POST', '/api/ops/restart', 'Bearer ops-test-key'))
        self.assertEqual(json.loads(body), {'reason': 'apply patched module'})
        method, path, _, _, body = self.ops_requests[1]
        self.assertEqual((method, path), ('POST', '/api/ops/deploy'))
        self.assertEqual(json.loads(body), {'commit': COMMIT})

    def test_ops_unavailable_fails_closed_without_network(self):
        reply = self.call('ops_status', {}, FACELESSFORGE_OPS_KEY='')
        self.assertTrue(reply['result']['isError'])
        self.assertEqual(self.ops_requests, [])

    def test_missing_key_fails_without_network(self):
        reply = self.call('render_status', {'job_id': 'test-job-123'}, EXTERNAL_RENDER_API_KEY='')
        self.assertTrue(reply['result']['isError'])
        self.assertEqual(self.requests, [])

    def test_redirect_does_not_forward_credentials(self):
        self.assertTrue(self.call('render_status', {'job_id': 'redirect-job'})['result']['isError'])
        self.assertEqual(len(self.requests), 1)

    def test_port_8000_is_forbidden(self):
        reply = self.call('render_status', {'job_id': 'test-job-123'}, FACELESSFORGE_API_URL='http://127.0.0.1:8000')
        self.assertTrue(reply['result']['isError'])
        self.assertEqual(self.requests, [])
        reply = self.call('ops_status', {}, FACELESSFORGE_OPS_URL='http://127.0.0.1:8000')
        self.assertTrue(reply['result']['isError'])
        self.assertEqual(self.ops_requests, [])

    def test_secrets_are_redacted_from_result_text(self):
        reply = self.call('render_status', {'job_id': 'echo-secret-job'})
        self.assertFalse(reply['result'].get('isError', False))
        text = reply['result']['content'][0]['text']
        self.assertNotIn('test-secret', text)
        self.assertNotIn('ops-test-key', text)
        self.assertIn('[REDACTED]', text)

    def test_call_before_initialize_is_rejected(self):
        reply = self.exchange([{'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}])[0]
        self.assertIn('error', reply)

    def test_enabled_flag_value_never_mangles_output(self):
        reply = self.call('connection_status', {}, EXTERNAL_RENDER_ENABLED='true')
        self.assertFalse(reply['result'].get('isError', False))
        text = reply['result']['content'][0]['text']
        self.assertIn('"reachable": true', text)
        self.assertIn('"key_configured": true', text)
        status = json.loads(text)
        self.assertTrue(status['render_api']['external_render_enabled_in_local_configuration'])


if __name__ == '__main__':
    unittest.main()
