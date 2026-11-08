"""Connection verification for the FacelessForge stdio MCP connector.

Runs the real server over stdin/stdout and verifies:
  1. initialize handshake replies with a protocol version
  2. tools/list exposes all nine FacelessForge tools
  3. connection_status executes a live read-only call and reports flags
No render, no mutation, no credentials are printed.

Usage:  python3 tools/mcp/verify_connection.py [--json]
Exit 0 = all checks passed; nonzero otherwise.
"""
import json
import subprocess
import sys
from pathlib import Path

SERVER = str(Path(__file__).resolve().with_name('server.py'))
EXPECTED = {
    'connection_status', 'render_video', 'render_status', 'cancel_render',
    'ops_status', 'ops_logs', 'ops_task_status', 'ops_restart', 'ops_deploy',
}
MESSAGES = [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
     'params': {'name': 'connection_status', 'arguments': {}}},
]


def main(verbose=False):
    result = subprocess.run([sys.executable, SERVER], input=''.join(json.dumps(m) + '\n' for m in MESSAGES),
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        print('FAIL: server exited', result.returncode, file=sys.stderr)
        print(result.stderr[-2000:], file=sys.stderr)
        return 1
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    checks = {}
    handshake = replies[0]['result']
    checks['handshake'] = bool(handshake.get('protocolVersion'))
    discovered = {t['name'] for t in replies[1]['result']['tools']}
    checks['tool_discovery'] = discovered == EXPECTED
    reply = replies[2]['result']
    payload = json.loads(reply['content'][0]['text'])
    checks['connection_status'] = (not reply.get('isError')
                                   and payload.get('render_api', {}).get('reachable') is not None)
    if verbose:
        print(json.dumps(payload, indent=1))
    for name, passed in checks.items():
        print(('PASS' if passed else 'FAIL'), name)
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    sys.exit(main(verbose='--json' in sys.argv))
