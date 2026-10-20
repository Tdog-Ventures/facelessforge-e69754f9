"""Local stdio MCP connector for FacelessForge's authenticated external API.

Implements MCP 2025-06-18 and 2024-11-05 lifecycle; no HTTP listener,
model calls, retries, scheduler changes, or YouTube publication.
"""
import ipaddress
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSIONS = ('2025-06-18', '2024-11-05')
MAX_MESSAGE = 100_000
RENDER_FIELDS = {
    'title': {'type': 'string', 'minLength': 1, 'maxLength': 200},
    'script': {'type': 'string', 'minLength': 10, 'maxLength': 20000},
    'target_duration': {'type': 'integer', 'minimum': 15, 'maximum': 3600},
    'voiceover_notes': {'type': 'string', 'maxLength': 600},
    'stock_footage_terms': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 20},
}
JOB_FIELDS = {'job_id': {'type': 'string', 'minLength': 8, 'maxLength': 64,
                         'pattern': '^[A-Za-z0-9_-]+$'}}


def tool(name, description, properties, required, read_only):
    return {'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties,
                            'required': required, 'additionalProperties': False},
            'annotations': {'readOnlyHint': read_only, 'destructiveHint': name == 'cancel_render',
                            'idempotentHint': read_only, 'openWorldHint': True}}


TOOLS = [
    tool('render_video', 'Submit one FacelessForge render using an existing script. May consume provider credits. Returns a job ID; does not upload to YouTube. Never retry an ambiguous submission automatically.', RENDER_FIELDS, ['title', 'script'], False),
    tool('render_status', 'Read an existing FacelessForge render job. A completed status is not proof that its MP4 passes media verification.', JOB_FIELDS, ['job_id'], True),
    tool('cancel_render', 'Cancel the specified FacelessForge render job through its existing cancellation API.', JOB_FIELDS, ['job_id'], False),
]


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
            if 'pattern' in field and not re.fullmatch(r'[A-Za-z0-9_-]+', value):
                raise ValueError('invalid job_id')
        elif kind == 'integer':
            if not field['minimum'] <= value <= field['maximum']:
                raise ValueError(f'invalid range for {key}')
        elif len(value) > field['maxItems'] or any(not isinstance(v, str) or len(v) > 200 for v in value):
            raise ValueError(f'invalid items for {key}')
    return arguments


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api_call(name, arguments):
    base = os.environ.get('FACELESSFORGE_API_URL', 'http://127.0.0.1:7001').rstrip('/')
    parsed = urlsplit(base)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username or parsed.password or parsed.port == 8000 or
            parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('API URL must be an HTTP(S) origin without credentials or port 8000')
    if parsed.scheme == 'http':
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == 'localhost'
        if not loopback:
            raise ValueError('remote API connections require HTTPS')
    key = os.environ.get('EXTERNAL_RENDER_API_KEY', '')
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
                'serverInfo': {'name': 'facelessforge', 'version': '1.0.0'}}, True
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
            result = api_call(name, args)
            return {'content': [{'type': 'text', 'text': json.dumps(result)}], 'isError': False}, initialized
        except (ValueError, TypeError):
            # Validation errors are useful; unexpected API JSON never leaks credentials.
            error = sys.exc_info()[1]
            detail = str(error) if not isinstance(error, json.JSONDecodeError) else 'API returned invalid JSON'
            key = os.environ.get('EXTERNAL_RENDER_API_KEY', '')
            if key:
                detail = detail.replace(key, '[REDACTED]')
            return {'content': [{'type': 'text', 'text': detail}], 'isError': True}, initialized
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
