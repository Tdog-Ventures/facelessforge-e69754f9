import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVER = Path(__file__).resolve().parents[1] / 'server.py'


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests
        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                requests.append((self.command, self.path, self.headers.get('X-FacelessForge-Key'), body))
                if 'redirect-job' in self.path:
                    self.send_response(302)
                    self.send_header('Location', 'http://127.0.0.1:8000/forbidden')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'job_id': 'test-job-123', 'status': 'queued'}).encode())
            do_GET = handle_request
            do_POST = handle_request
            def log_message(self, *args):
                pass
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def exchange(self, messages, **environment):
        env = os.environ | {'FACELESSFORGE_API_URL': f'http://127.0.0.1:{self.http.server_port}',
                               'EXTERNAL_RENDER_API_KEY': 'test-secret'} | environment
        result = subprocess.run([sys.executable, str(SERVER)], input=''.join(json.dumps(m)+'\n' for m in messages),
                                text=True, capture_output=True, env=env, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def call(self, name, arguments, **env):
        return self.exchange([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}},
        ], **env)[-1]

    def test_handshake_and_tool_discovery(self):
        replies = self.exchange([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        ])
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-06-18')
        self.assertEqual({t['name'] for t in replies[1]['result']['tools']}, {'render_video', 'render_status', 'cancel_render'})

    def test_authenticated_render_matches_backend_contract(self):
        payload = {'title': 'A business story', 'script': 'This is a real narration script.', 'target_duration': 60}
        reply = self.call('render_video', payload)
        self.assertFalse(reply['result'].get('isError', False))
        method, path, key, body = self.requests[0]
        self.assertEqual((method, path, key), ('POST', '/api/external/render-video', 'test-secret'))
        self.assertEqual(json.loads(body), payload)

    def test_status_and_cancel_use_exact_routes(self):
        self.call('render_status', {'job_id': 'test-job-123'})
        self.call('cancel_render', {'job_id': 'test-job-123'})
        self.assertEqual(self.requests[0][:2], ('GET', '/api/external/render-video-status?job_id=test-job-123'))
        self.assertEqual(self.requests[1][:2], ('POST', '/api/external/render-video/cancel'))

    def test_invalid_arguments_never_reach_backend(self):
        for name, args in [('render_video', {'title': 'x', 'script': 'short'}),
                           ('render_status', {'job_id': '../evil?x=1'}),
                           ('render_status', {'job_id': 'test-job-123', 'url': 'http://evil'}),
                           ('render_video', {'title': 'x', 'script': 'a'*20, 'target_duration': True})]:
            with self.subTest(name=name, args=args):
                self.assertTrue(self.call(name, args)['result']['isError'])
        self.assertEqual(self.requests, [])

    def test_redirect_does_not_forward_credentials(self):
        self.assertTrue(self.call('render_status', {'job_id': 'redirect-job'})['result']['isError'])
        self.assertEqual(len(self.requests), 1)

    def test_missing_key_fails_without_network(self):
        reply = self.call('render_status', {'job_id': 'test-job-123'}, EXTERNAL_RENDER_API_KEY='')
        self.assertTrue(reply['result']['isError'])
        self.assertEqual(self.requests, [])

    def test_port_8000_is_forbidden(self):
        reply = self.call('render_status', {'job_id': 'test-job-123'}, FACELESSFORGE_API_URL='http://127.0.0.1:8000')
        self.assertTrue(reply['result']['isError'])
        self.assertEqual(self.requests, [])

    def test_call_before_initialize_is_rejected(self):
        reply = self.exchange([{'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}])[0]
        self.assertIn('error', reply)


if __name__ == '__main__':
    unittest.main()
