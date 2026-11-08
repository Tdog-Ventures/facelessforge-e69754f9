"""Local stdio MCP connector for FacelessForge's authenticated external API.

Implements MCP 2025-06-18 lifecycle (plus 2024-11-05 / 2025-11-25 for older clients);
no HTTP listener, model calls, retries, scheduler changes, or YouTube publication.

Tools:
  Read-only:        connection_status, render_status, ops_status, ops_logs, ops_task_status
  Controlled write: render_video, cancel_render, ops_restart, ops_deploy

Render tools call the FacelessForge backend (FACELESSFORGE_API_URL, default
http://127.0.0.1:7001) with the X-FacelessForge-Key header. Ops tools call the
owner-only operations bridge (FACELESSFORGE_OPS_URL, default http://127.0.0.1:7012)
with a Bearer FACELESSFORGE_OPS_KEY. HTTP is only ever allowed to loopback origins;
remote connections must use HTTPS. Redirects are refused so credentials are never
forwarded. Port 8000 is forbidden for both destinations.

Credentials come from the process environment, or from root-owned key=value files.
Only EXTERNAL_RENDER_API_KEY, FACELESSFORGE_OPS_KEY and EXTERNAL_RENDER_ENABLED are
ever parsed; no other file value is read into this process and no value is logged.
Set FACELESSFORGE_MCP_ENV_FILE (colon-separated) to override the default file list.
"""
import ipaddress
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSIONS = ('2025-06-18', '2024-11-05', '2025-11-25')
MAX_MESSAGE = 100_000
SECRET_NAMES = ('EXTERNAL_RENDER_API_KEY', 'FACELESSFORGE_OPS_KEY', 'EXTERNAL_RENDER_ENABLED')
DEFAULT_ENV_FILES = ('/opt/ethinx/deployment/secrets-vault/facelessforge-deploy.env',
                     '/opt/ethinx/facelessforge-ops/ops.env')
UUID_PATTERN = r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
COMMIT_PATTERN = r'^[a-f0-9]{40}$'
RENDER_FIELDS = {
    'title': {'type': 'string', 'minLength': 1, 'maxLength': 200},
    'script': {'type': 'string', 'minLength': 10, 'maxLength': 20000},
    'target_duration': {'type': 'integer', 'minimum': 15, 'maximum': 3600},
    'voiceover_notes': {'type': 'string', 'maxLength': 600},
    'stock_footage_terms': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 20},
}
JOB_FIELDS = {'job_id': {'type': 'string', 'minLength': 8, 'maxLength': 64,
                         'pattern': '^[A-Za-z0-9_-]+$'}}
LINES_FIELDS = {'lines': {'type': 'integer', 'minimum': 20, 'maximum': 200}}
TASK_FIELDS = {'task_id': {'type': 'string', 'minLength': 36, 'maxLength': 36,
                           'pattern': UUID_PATTERN}}
COMMIT_FIELDS = {'commit': {'type': 'string', 'minLength': 40, 'maxLength': 40,
                            'pattern': COMMIT_PATTERN}}
REASON_FIELDS = {'reason': {'type': 'string', 'minLength': 1, 'maxLength': 300}}


DESTRUCTIVE = ('cancel_render', 'ops_restart', 'ops_deploy')


def tool(name, description, properties, required, read_only):
    return {'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties,
                            'required': required, 'additionalProperties': False},
            'annotations': {'readOnlyHint': read_only,
                            'destructiveHint': name in DESTRUCTIVE,
                            'idempotentHint': read_only, 'openWorldHint': True}}


TOOLS = [
    tool('connection_status', 'Read FacelessForge connection state: backend reachability, health payload, '
         'and whether render/ops credentials are configured (never their values). Read-only.', {}, [], True),
    tool('render_video', 'Submit one FacelessForge render using an existing script. May consume provider credits. '
         'Returns a job ID; does not upload to YouTube. Never retry an ambiguous submission automatically.',
         RENDER_FIELDS, ['title', 'script'], False),
    tool('render_status', 'Read an existing FacelessForge render job. A completed status is not proof that its '
         'MP4 passes media verification.', JOB_FIELDS, ['job_id'], True),
    tool('cancel_render', 'Cancel the specified FacelessForge render job through its existing cancellation API.',
         JOB_FIELDS, ['job_id'], False),
    tool('ops_status', 'Read FacelessForge production status: container health, deployed revision, backend health, '
         'external render flags, mutation guards and active work. Read-only.', {}, [], True),
    tool('ops_logs', 'Read a redacted tail of the FacelessForge production container logs via the operations '
         'bridge. Bounded to 20-200 lines; output is credential-redacted server-side. Read-only.',
         LINES_FIELDS, [], True),
    tool('ops_task_status', 'Read one operations-bridge task (deploy or restart) by UUID. A queued/running status '
         'is not evidence of success; reconcile_required requires host inspection.', TASK_FIELDS, ['task_id'], True),
    tool('ops_restart', 'Queue one guarded FacelessForge production restart through the operations bridge. '
         'Blocking: fails if a render or deployment is active. Supply a short reason.', REASON_FIELDS, ['reason'], False),
    tool('ops_deploy', 'Queue one guarded FacelessForge production deployment through the operations bridge. '
         'Requires the full lowercase 40-character commit SHA from the authorised repository. The 202 receipt is '
         'not proof of deployment; poll ops_task_status and verify revision plus health before reporting success.',
         COMMIT_FIELDS, ['commit'], False),
]
TOOL_NAMES = {t['name'] for t in TOOLS}


def validate_arguments(name, arguments):
    definition = next((t for t in TOOLS if t['name'] == name), None)
    if definition is None:
        raise ValueError('unknown tool')
    schema = definition['inputSchema']
    if not isinstance(arguments, dict) or set(arguments) - set(schema['properties']):
        raise ValueError('arguments must contain only documented fields')
    if any(k not in arguments for k in schema['required']):
        raise ValueError('required argument missing')
    for key, value in arguments.items():
        field = schema['properties'][key]
        kind = field['type']
        valid = (isinstance(value, str) if kind == 'string' else
                 type(value) is int if kind == 'integer' else isinstance(value, list))
        if not valid:
            raise ValueError(f'invalid type for {key}')
        if kind == 'string':
            if not field.get('minLength', 0) <= len(value) <= field.get('maxLength', 2000):
                raise ValueError(f'invalid length for {key}')
            if 'pattern' in field:
                if key == 'job_id' and not re.fullmatch(r'[A-Za-z0-9_-]+', value):
                    raise ValueError('invalid job_id')
                if key == 'task_id' and not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
                                                         r'[0-9a-f]{4}-[0-9a-f]{12}', value):
                    raise ValueError('invalid task_id')
                if key == 'commit' and not re.fullmatch(r'[a-f0-9]{40}', value):
                    raise ValueError('commit must be a full lowercase 40-character SHA')
        elif kind == 'integer':
            if not field['minimum'] <= value <= field['maximum']:
                raise ValueError(f'invalid range for {key}')
        elif len(value) > field['maxItems'] or any(not isinstance(v, str) or len(v) > 200 for v in value):
            raise ValueError(f'invalid items for {key}')
    return arguments


def secret_files():
    override = os.environ.get('FACELESSFORGE_MCP_ENV_FILE', '')
    if override.strip():
        return [path for path in override.split(':') if path.strip()]
    return list(DEFAULT_ENV_FILES)


def parse_secret_file(path):
    # Strict allowlist: values other than SECRET_NAMES are never read into the process.
    resolved = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return resolved
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        line = line[len('export '):] if line.startswith('export ') else line
        name, _, value = line.partition('=')
        name = name.strip()
        if name in SECRET_NAMES and _:
            resolved[name] = value.strip().strip('"').strip("'")
    return resolved


def resolve_secrets():
    resolved = {name: os.environ.get(name, '') for name in SECRET_NAMES}
    for path in secret_files():
        for name, value in parse_secret_file(path).items():
            if not resolved.get(name):
                resolved[name] = value
    return resolved


def redact(text, secrets):
    # Only actual credential values are redacted. Short generic values like the
    # EXTERNAL_RENDER_ENABLED flag "true" must never enter the replacement set.
    result = str(text)
    values = [secrets.get('EXTERNAL_RENDER_API_KEY', ''), secrets.get('FACELESSFORGE_OPS_KEY', '')]
    for value in sorted({v for v in values if len(v) >= 8}, key=len, reverse=True):
        result = result.replace(value, '[REDACTED]')
    return result


def checked_origin(url, what):
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username or parsed.password or parsed.port == 8000 or
            parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError(f'{what} URL must be an HTTP(S) origin without credentials or port 8000')
    if parsed.scheme == 'http':
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == 'localhost'
        if not loopback:
            raise ValueError('remote API connections require HTTPS')
    return url


def base_url():
    return os.environ.get('FACELESSFORGE_API_URL', 'http://127.0.0.1:7001').rstrip('/')


def ops_url():
    return os.environ.get('FACELESSFORGE_OPS_URL', 'http://127.0.0.1:7012').rstrip('/')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api_call(name, arguments):
    base = checked_origin(base_url(), 'API')
    key = resolve_secrets()['EXTERNAL_RENDER_API_KEY']
    if not key:
        raise ValueError('EXTERNAL_RENDER_API_KEY is required')
    routes = {'render_video': '/api/external/render-video',
              'render_status': '/api/external/render-video-status',
              'cancel_render': '/api/external/render-video/cancel'}
    url = base + routes[name]
    data = None
    if name == 'render_status':
        url += '?' + urlencode(arguments)
    else:
        data = json.dumps(arguments).encode()
    request = Request(url, data=data, headers={'X-FacelessForge-Key': key,
                                              'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError('API response too large')
            return json.loads(raw)
    except HTTPError as error:
        raise ValueError(f'FacelessForge API returned HTTP {error.code}; redirects are refused') from None
    except (URLError, TimeoutError, OSError) as error:
        # Never return request headers, URLs, provider errors, or credential values.
        raise ValueError('FacelessForge API connection failed; reconcile any submitted job before retrying') from None


def ops_call(method, path, body=None, timeout=12):
    origin = checked_origin(ops_url(), 'OPS')
    key = resolve_secrets()['FACELESSFORGE_OPS_KEY']
    if not key:
        raise ValueError('FACELESSFORGE_OPS_KEY is not configured; ops tools are unavailable read-only')
    headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    request = Request(origin + path, data=data, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError('OPS response too large')
            return response.status, json.loads(raw) if raw else {}
        # Bridge error bodies are scrubbed server-side and safe to relay.
    except HTTPError as error:
        try:
            payload = json.loads(error.read(100_001))
        except Exception:
            payload = {}
        raise ValueError(f'FacelessForge OPS bridge returned HTTP {error.code}: '
                         f'{payload.get("error", "operation unavailable")}') from None
    except (URLError, TimeoutError, OSError):
        raise ValueError('FacelessForge OPS bridge connection failed; verify the service on the host '
                         'before retrying; never repeat a mutation on an uncertain outcome') from None


def probe_health(base):
    request = Request(base.rstrip('/') + '/api/health', headers={'Accept': 'application/json'})
    try:
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(100_001)
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            return response.status, body, False
    except HTTPError as error:
        refused = error.code in (301, 302, 303, 307, 308)
        return error.code, None, refused
    except (URLError, TimeoutError, OSError):
        return None, None, False


def utc_now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def connection_status(arguments):
    base = checked_origin(base_url(), 'API')
    checked_origin(ops_url(), 'OPS')
    secrets = resolve_secrets()
    code, body, refused = probe_health(base)
    enabled = secrets.get('EXTERNAL_RENDER_ENABLED', '')
    return {'product': 'FacelessForge', 'observed_at': utc_now(),
            'render_api': {'base_url': base, 'scheme': urlsplit(base).scheme,
                           'reachable': code is not None, 'http_status': code,
                           'redirect_refused': refused, 'health': body,
                           'key_configured': bool(secrets.get('EXTERNAL_RENDER_API_KEY')),
                           'external_render_enabled_in_local_configuration':
                               enabled.lower() in ('1', 'true', 'yes') if enabled else None},
            'ops_bridge': {'base_url': ops_url(), 'scheme': urlsplit(ops_url()).scheme,
                           'key_configured': bool(secrets.get('FACELESSFORGE_OPS_KEY'))},
            'notes': ['Render submissions consume provider credits; a returned job ID is not a completed video.',
                      'OPS mutations require the guarded helpers installed on the host.']}


def ops_log_path(arguments):
    return '/api/ops/logs' + ('?' + urlencode({'lines': arguments['lines']}) if arguments.get('lines') is not None else '')


def dispatch(message, initialized):
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
        raise ValueError('invalid JSON-RPC request')
    method = message['method']
    params = message.get('params', {})
    if not isinstance(params, dict):
        raise ValueError('params must be an object')
    if method == 'initialize':
        version = params.get('protocolVersion')
        return {'protocolVersion': version if version in VERSIONS else VERSIONS[0],
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': 'facelessforge', 'version': '1.1.0'}}, True
    if method.startswith('notifications/'):
        return None, initialized
    if not initialized:
        raise ValueError('initialize before invoking tools')
    if method == 'ping':
        return {}, initialized
    if method == 'tools/list':
        return {'tools': TOOLS}, initialized
    if method == 'tools/call':
        try:
            name = params.get('name')
            args = validate_arguments(name, params.get('arguments', {}))
            if name == 'connection_status':
                result = connection_status(args)
            elif name.startswith('ops_'):
                if name == 'ops_status':
                    result = ops_call('GET', '/api/ops/status')[1]
                elif name == 'ops_logs':
                    result = ops_call('GET', ops_log_path(args))[1]
                elif name == 'ops_task_status':
                    result = ops_call('GET', '/api/ops/tasks/' + args['task_id'])[1]
                elif name == 'ops_restart':
                    result = ops_call('POST', '/api/ops/restart', {'reason': args['reason']})[1]
                else:  # ops_deploy
                    result = ops_call('POST', '/api/ops/deploy', {'commit': args['commit']})[1]
            else:
                result = api_call(name, args)
            secrets = resolve_secrets()
            return {'content': [{'type': 'text', 'text': redact(json.dumps(result), secrets)}],
                    'isError': False}, initialized
        except (ValueError, TypeError) as error:
            # Validation errors are useful; unexpected API JSON never leaks credentials.
            detail = str(error) if not isinstance(error, json.JSONDecodeError) else 'API returned invalid JSON'
            secrets = resolve_secrets()
            return {'content': [{'type': 'text', 'text': redact(detail, secrets)}], 'isError': True}, initialized
    return {'_unknown_method': True}, initialized


def main():
    initialized = False
    for line in sys.stdin:
        message = None
        try:
            if len(line) > MAX_MESSAGE:
                raise ValueError('message too large')
            message = json.loads(line)
            result, initialized = dispatch(message, initialized)
            if 'id' not in message:
                continue
            if result == {'_unknown_method': True}:
                reply = {'error': {'code': -32601, 'message': 'method not found'}}
            else:
                reply = {'result': result}
        except (ValueError, TypeError):
            reply = {'error': {'code': -32700 if message is None else -32600, 'message': 'invalid request or lifecycle'}}
            if isinstance(message, dict) and 'id' not in message:
                continue
        reply.update(jsonrpc='2.0', id=message.get('id') if isinstance(message, dict) else None)
        print(json.dumps(reply), flush=True)


if __name__ == '__main__':
    main()
