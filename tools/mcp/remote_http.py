"""Authenticated Streamable HTTP adapter for FacelessForge's existing stdio MCP.

This process exposes only the five read-only tools. It imports the existing
``server.py`` implementation so tool logic, backend authentication, redaction,
and input validation remain single-sourced.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import server as stdio_mcp


PUBLIC_BASE_URL = os.environ.get(
    "FACELESSFORGE_REMOTE_MCP_BASE_URL",
    "https://cinematic.ethinx.solutions",
).rstrip("/")
MCP_PATH = "/facelessforge/mcp/v1"
AUTH_ISSUER_PATH = ""
AUTHORIZATION_PATH = "/facelessforge/mcp/oauth/authorize"
TOKEN_PATH = "/facelessforge/mcp/oauth/token"
REVOCATION_PATH = "/facelessforge/mcp/oauth/revoke"
PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
AUTH_METADATA_PATH = "/.well-known/oauth-authorization-server"
MAX_REQUEST = 100_000
TOKEN_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300
READ_ONLY_NAMES = {
    "connection_status",
    "render_status",
    "ops_status",
    "ops_logs",
    "ops_task_status",
}
READ_ONLY_TOOLS = [tool for tool in stdio_mcp.TOOLS if tool["name"] in READ_ONLY_NAMES]
GOOGLE_REDIRECT_HOSTS = {
    "oauth-redirect.googleusercontent.com",
    "oauth-redirect-sandbox.googleusercontent.com",
    "oauth-redirect-test.googleusercontent.com",
}


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


CLIENT_ID = required_env("FACELESSFORGE_REMOTE_MCP_CLIENT_ID")
CLIENT_SECRET = required_env("FACELESSFORGE_REMOTE_MCP_CLIENT_SECRET")
OWNER_PASSWORD = required_env("FACELESSFORGE_REMOTE_MCP_OWNER_PASSWORD")


class OAuthState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.codes: dict[str, dict] = {}
        self.access_tokens: dict[str, float] = {}
        self.refresh_tokens: dict[str, float] = {}

    def prune(self) -> None:
        now = time.time()
        for store in (self.codes, self.access_tokens, self.refresh_tokens):
            expired = [key for key, value in store.items()
                       if (value["expires"] if isinstance(value, dict) else value) <= now]
            for key in expired:
                store.pop(key, None)

    def issue_code(self, payload: dict) -> str:
        code = secrets.token_urlsafe(32)
        with self.lock:
            self.prune()
            self.codes[code] = payload | {"expires": time.time() + CODE_TTL}
        return code

    def exchange_code(self, code: str) -> dict | None:
        with self.lock:
            self.prune()
            return self.codes.pop(code, None)

    def issue_tokens(self) -> dict:
        access = secrets.token_urlsafe(40)
        refresh = secrets.token_urlsafe(48)
        now = time.time()
        with self.lock:
            self.prune()
            self.access_tokens[access] = now + TOKEN_TTL
            self.refresh_tokens[refresh] = now + REFRESH_TTL
        return {"access_token": access, "token_type": "Bearer", "expires_in": TOKEN_TTL,
                "refresh_token": refresh, "scope": "facelessforge.read"}

    def refresh(self, refresh: str) -> dict | None:
        with self.lock:
            self.prune()
            if refresh not in self.refresh_tokens:
                return None
            self.refresh_tokens.pop(refresh, None)
        return self.issue_tokens()

    def valid_access(self, token: str) -> bool:
        with self.lock:
            self.prune()
            return self.access_tokens.get(token, 0) > time.time()

    def revoke(self, token: str) -> None:
        with self.lock:
            self.access_tokens.pop(token, None)
            self.refresh_tokens.pop(token, None)


STATE = OAuthState()


def valid_redirect(uri: str) -> bool:
    parsed = urlsplit(uri)
    return (parsed.scheme == "https" and parsed.hostname in GOOGLE_REDIRECT_HOSTS
            and not parsed.username and not parsed.password and not parsed.fragment
            and parsed.path.startswith(("/r/", "/a/")))


def b64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def client_credentials(headers, form: dict[str, str]) -> tuple[str, str]:
    auth = headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            raw = base64.b64decode(auth[6:], validate=True).decode("utf-8")
            return tuple(raw.split(":", 1)) if ":" in raw else (raw, "")
        except (ValueError, UnicodeDecodeError):
            return "", ""
    return form.get("client_id", ""), form.get("client_secret", "")


class Handler(BaseHTTPRequestHandler):
    server_version = "FacelessForgeRemoteMCP/1.0"

    def log_message(self, _format, *_args):
        return

    def security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")

    def send_json(self, status: int, payload: dict | list | None, extra: dict[str, str] | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.security_headers()
        if extra:
            for name, value in extra.items():
                self.send_header(name, value)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length < 0 or length > MAX_REQUEST:
            return None
        return self.rfile.read(length)

    def read_form(self) -> dict[str, str] | None:
        body = self.read_body()
        if body is None:
            return None
        try:
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
        except (UnicodeDecodeError, ValueError):
            return None
        return {key: values[-1] for key, values in parsed.items()}

    def oauth_error(self, status: int, error: str) -> None:
        self.send_json(status, {"error": error})

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == PROTECTED_RESOURCE_PATH:
            return self.send_json(200, {
                "resource": PUBLIC_BASE_URL + MCP_PATH,
                "authorization_servers": [PUBLIC_BASE_URL + AUTH_ISSUER_PATH],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["facelessforge.read"],
            })
        if parsed.path == AUTH_METADATA_PATH:
            return self.send_json(200, {
                "issuer": PUBLIC_BASE_URL + AUTH_ISSUER_PATH,
                "authorization_endpoint": PUBLIC_BASE_URL + AUTHORIZATION_PATH,
                "token_endpoint": PUBLIC_BASE_URL + TOKEN_PATH,
                "revocation_endpoint": PUBLIC_BASE_URL + REVOCATION_PATH,
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["facelessforge.read"],
            })
        if parsed.path == AUTHORIZATION_PATH:
            return self.authorization_page(parse_qs(parsed.query, keep_blank_values=True))
        if parsed.path == MCP_PATH:
            return self.send_json(405, {"error": "method_not_allowed"}, {"Allow": "POST"})
        return self.send_json(404, {"error": "not_found"})

    def authorization_page(self, query: dict[str, list[str]]) -> None:
        params = {key: values[-1] for key, values in query.items()}
        required = ("client_id", "redirect_uri", "response_type", "state", "code_challenge", "code_challenge_method")
        if (any(not params.get(name) for name in required)
                or params["client_id"] != CLIENT_ID
                or params["response_type"] != "code"
                or params["code_challenge_method"] != "S256"
                or not valid_redirect(params["redirect_uri"])):
            return self.oauth_error(400, "invalid_request")
        fields = "".join(
            f'<input type="hidden" name="{html.escape(name)}" value="{html.escape(params.get(name, ""), quote=True)}">'
            for name in required + ("scope", "resource")
        )
        page = f"""<!doctype html><html><head><meta charset=utf-8><title>Authorize FacelessForge</title>
<style>body{{font:16px system-ui;max-width:34rem;margin:4rem auto;padding:0 1rem}}input,button{{font:inherit;padding:.7rem;width:100%;box-sizing:border-box;margin:.4rem 0}}small{{color:#555}}</style></head>
<body><h1>Authorize read-only FacelessForge access</h1><p>This grants Gemini access only to status, bounded redacted logs, and existing job/task status. Render, cancel, restart, and deploy actions are disabled.</p>
<form method="post" action="{AUTHORIZATION_PATH}">{fields}<label>Owner authorization password<input type="password" name="owner_password" required autocomplete="current-password"></label><button type="submit">Authorize</button></form>
<small>Tokens expire automatically and can be revoked by disconnecting the app.</small></body></html>""".encode()
        self.send_response(200)
        self.security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == MCP_PATH:
            return self.handle_mcp()
        if path == AUTHORIZATION_PATH:
            return self.handle_authorize()
        if path == TOKEN_PATH:
            return self.handle_token()
        if path == REVOCATION_PATH:
            return self.handle_revoke()
        return self.send_json(404, {"error": "not_found"})

    def handle_authorize(self) -> None:
        form = self.read_form()
        required = ("client_id", "redirect_uri", "response_type", "state", "code_challenge", "code_challenge_method")
        if (form is None or any(not form.get(name) for name in required)
                or form["client_id"] != CLIENT_ID
                or form["response_type"] != "code"
                or form["code_challenge_method"] != "S256"
                or not valid_redirect(form["redirect_uri"])
                or not hmac.compare_digest(form.get("owner_password", ""), OWNER_PASSWORD)):
            return self.oauth_error(403, "access_denied")
        code = STATE.issue_code({
            "client_id": form["client_id"],
            "redirect_uri": form["redirect_uri"],
            "code_challenge": form["code_challenge"],
        })
        location = form["redirect_uri"] + ("&" if "?" in form["redirect_uri"] else "?") + urlencode({
            "code": code, "state": form["state"]
        })
        self.send_response(302)
        self.security_headers()
        self.send_header("Location", location)
        self.end_headers()

    def handle_token(self) -> None:
        form = self.read_form()
        if form is None:
            return self.oauth_error(400, "invalid_request")
        client_id, client_secret = client_credentials(self.headers, form)
        if not (hmac.compare_digest(client_id, CLIENT_ID)
                and hmac.compare_digest(client_secret, CLIENT_SECRET)):
            return self.oauth_error(401, "invalid_client")
        grant = form.get("grant_type")
        if grant == "authorization_code":
            record = STATE.exchange_code(form.get("code", ""))
            if (not record or record["client_id"] != client_id
                    or record["redirect_uri"] != form.get("redirect_uri")
                    or not form.get("code_verifier")
                    or not hmac.compare_digest(record["code_challenge"], b64url_sha256(form["code_verifier"]))):
                return self.oauth_error(400, "invalid_grant")
            return self.send_json(200, STATE.issue_tokens())
        if grant == "refresh_token":
            tokens = STATE.refresh(form.get("refresh_token", ""))
            return self.send_json(200, tokens) if tokens else self.oauth_error(400, "invalid_grant")
        return self.oauth_error(400, "unsupported_grant_type")

    def handle_revoke(self) -> None:
        form = self.read_form()
        if form is None:
            return self.oauth_error(400, "invalid_request")
        client_id, client_secret = client_credentials(self.headers, form)
        if not (hmac.compare_digest(client_id, CLIENT_ID)
                and hmac.compare_digest(client_secret, CLIENT_SECRET)):
            return self.oauth_error(401, "invalid_client")
        STATE.revoke(form.get("token", ""))
        return self.send_json(200, {})

    def bearer_token(self) -> str:
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def handle_mcp(self) -> None:
        if not STATE.valid_access(self.bearer_token()):
            metadata = PUBLIC_BASE_URL + PROTECTED_RESOURCE_PATH
            return self.send_json(401, {"error": "unauthorized"}, {
                "WWW-Authenticate": f'Bearer resource_metadata="{metadata}", scope="facelessforge.read"'
            })
        body = self.read_body()
        try:
            message = json.loads(body) if body is not None else None
        except (ValueError, TypeError):
            message = None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self.send_json(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "parse error"}})
        method = message.get("method")
        if method == "initialize":
            result, _ = stdio_mcp.dispatch(message, False)
            result["instructions"] = (
                "Read-only FacelessForge operational visibility. Render submission, cancellation, "
                "restart, and deployment are disabled on this endpoint. Results may contain "
                "production metadata and bounded redacted logs; do not infer media quality from job status."
            )
        elif method == "tools/list":
            result = {"tools": READ_ONLY_TOOLS}
        elif method == "tools/call":
            name = (message.get("params") or {}).get("name")
            if name not in READ_ONLY_NAMES:
                result = {"content": [{"type": "text", "text": "tool is disabled on this read-only endpoint"}],
                          "isError": True}
            else:
                result, _ = stdio_mcp.dispatch(message, True)
        elif isinstance(method, str) and method.startswith("notifications/"):
            return self.send_json(202, None)
        elif method == "ping":
            result = {}
        else:
            return self.send_json(200, {"jsonrpc": "2.0", "id": message.get("id"),
                                        "error": {"code": -32601, "message": "method not found"}})
        return self.send_json(200, {"jsonrpc": "2.0", "id": message.get("id"), "result": result})


def main() -> None:
    host = os.environ.get("FACELESSFORGE_REMOTE_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("FACELESSFORGE_REMOTE_MCP_PORT", "7013"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
