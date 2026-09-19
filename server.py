#!/usr/bin/env python3
"""ETHINX sovereign revenue loop HTTP service. Python standard library only."""
from __future__ import annotations

import hmac
import json
import os
import re
import sys
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from wsgiref.simple_server import WSGIRequestHandler, make_server

from operations_status import collect_status
from sovereign_db import DomainError, SovereignStore

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load_config() -> dict[str, Any]:
    config_path = Path(os.environ.get("ETHINX_CONFIG", DEFAULT_CONFIG))
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    required = ("business", "bank_transfer", "plans", "content")
    missing = [key for key in required if key not in config]
    if missing:
        raise RuntimeError(f"Missing configuration sections: {', '.join(missing)}")
    return config


def clean_text(value: Any, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())[:max_length]


class RateLimiter:
    """Small in-process limiter; effective because the service runs as a single local process."""

    def __init__(self) -> None:
        self.buckets: dict[str, deque[float]] = defaultdict(deque)

    def allowed(self, key: str, maximum: int, window_seconds: int) -> bool:
        now = time.monotonic()
        events = self.buckets[key]
        while events and events[0] <= now - window_seconds:
            events.popleft()
        if len(events) >= maximum:
            return False
        events.append(now)
        return True


class App:
    def __init__(self, config: dict[str, Any], database_path: Path):
        self.config = config
        self.store = SovereignStore(database_path, config)
        self.web_root = ROOT / "web"
        self.admin_token = os.environ.get("ETHINX_ADMIN_TOKEN", "")
        security = config.get("security", {})
        self.trusted_proxy_ips = set(security.get("trusted_proxy_ips", ["127.0.0.1", "::1"]))
        self.trusted_origins = set(security.get("trusted_origins", []))
        self.require_https = bool(security.get("require_https", False))
        self.public_limit = int(security.get("public_requests_per_window", 5))
        self.admin_limit = int(security.get("admin_requests_per_window", 30))
        self.window_seconds = int(security.get("rate_limit_window_seconds", 900))
        self.limiter = RateLimiter()

    def client_ip(self, environ: dict[str, Any]) -> str:
        remote = environ.get("REMOTE_ADDR", "unknown")
        if remote in self.trusted_proxy_ips:
            forwarded = environ.get("HTTP_X_REAL_IP", "").strip()
            if forwarded and len(forwarded) <= 45 and all(ch in "0123456789abcdefABCDEF:." for ch in forwarded):
                return forwarded
        return remote

    def is_https(self, environ: dict[str, Any]) -> bool:
        remote = environ.get("REMOTE_ADDR", "unknown")
        if remote in self.trusted_proxy_ips and environ.get("HTTP_X_FORWARDED_PROTO", "").lower() == "https":
            return True
        return environ.get("wsgi.url_scheme", "http").lower() == "https"

    def headers(self, content_type: str, payload_length: int, cache_control: str = "no-store") -> list[tuple[str, str]]:
        headers = [
            ("Content-Type", content_type), ("Content-Length", str(payload_length)), ("Cache-Control", cache_control),
            ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "strict-origin-when-cross-origin"), ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
            ("Cross-Origin-Resource-Policy", "same-origin"), ("Cross-Origin-Opener-Policy", "same-origin"),
        ]
        if self.require_https:
            headers.append(("Strict-Transport-Security", "max-age=31536000; includeSubDomains"))
        return headers

    def log_access(self, request_id: str, environ: dict[str, Any], status: HTTPStatus) -> None:
        event = {
            "event": "http_request", "request_id": request_id, "time": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "method": environ.get("REQUEST_METHOD", "GET"), "path": environ.get("PATH_INFO", "/"),
            "status": status.value, "client_ip": self.client_ip(environ),
        }
        print(json.dumps(event, separators=(",", ":")), file=sys.stdout, flush=True)

    def response(self, start_response: Callable, environ: dict[str, Any], request_id: str, status: HTTPStatus,
                 body: dict[str, Any], extra_headers: list[tuple[str, str]] | None = None) -> list[bytes]:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self.headers("application/json; charset=utf-8", len(payload)) + [("X-Request-ID", request_id)]
        if extra_headers:
            headers.extend(extra_headers)
        start_response(f"{status.value} {status.phrase}", headers)
        self.log_access(request_id, environ, status)
        return [payload]

    def html(self, start_response: Callable, environ: dict[str, Any], request_id: str, name: str) -> list[bytes]:
        file_path = self.web_root / name
        if not file_path.is_file():
            return self.response(start_response, environ, request_id, HTTPStatus.NOT_FOUND, {"error": "Not found."})
        payload = file_path.read_bytes()
        headers = self.headers("text/html; charset=utf-8", len(payload), "no-cache") + [
            ("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
            ("X-Request-ID", request_id),
        ]
        start_response("200 OK", headers)
        self.log_access(request_id, environ, HTTPStatus.OK)
        return [payload]

    @staticmethod
    def parse_json(environ: dict[str, Any]) -> dict[str, Any]:
        content_length = environ.get("CONTENT_LENGTH", "0") or "0"
        try:
            length = int(content_length)
        except ValueError as exc:
            raise DomainError("Invalid request length.") from exc
        if length < 1 or length > 16_384:
            raise DomainError("Request body size is invalid.")
        content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            raise DomainError("Content-Type must be application/json.")
        raw = environ["wsgi.input"].read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainError("Request body must be valid JSON.") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON object expected.")
        return value

    def authorised(self, environ: dict[str, Any]) -> bool:
        supplied = environ.get("HTTP_X_ADMIN_TOKEN", "")
        origin = environ.get("HTTP_ORIGIN", "")
        if origin and origin not in self.trusted_origins:
            return False
        return bool(self.admin_token) and hmac.compare_digest(supplied, self.admin_token)

    def require_admin(self, environ: dict[str, Any]) -> None:
        if not self.authorised(environ):
            raise PermissionError("Administrative authorisation required.")

    def allow_request(self, environ: dict[str, Any], scope: str) -> bool:
        client = self.client_ip(environ)
        maximum = self.admin_limit if scope == "admin" else self.public_limit
        return self.limiter.allowed(f"{scope}:{client}", maximum, self.window_seconds)

    def operations_status(self) -> dict[str, Any]:
        data_dir = Path(os.environ.get("ETHINX_DATA_DIR", ROOT / "data"))
        return collect_status(self.config, data_dir, self.store.database_path)

    def health_response(self, start_response: Callable, environ: dict[str, Any], request_id: str,
                        job_name: str | None = None) -> list[bytes]:
        status = self.operations_status()
        if job_name is not None:
            job = status["jobs"].get(job_name)
            if job is None:
                return self.response(start_response, environ, request_id, HTTPStatus.NOT_FOUND, {"ok": False, "error": "Unknown job."})
            healthy = job.get("status") == "ok"
            return self.response(start_response, environ, request_id, HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                                 {"ok": healthy, "job": job_name, "status": job.get("status"), "reason": job.get("reason")})
        healthy = status["overall_status"] in {"ok", "attention"}
        return self.response(start_response, environ, request_id, HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                             {"ok": healthy, "status": status["overall_status"], "service": status["service"]})

    def application_instructions(self, application: dict[str, Any]) -> dict[str, Any]:
        bank = self.config["bank_transfer"]
        amount = application["amount_cents"] / 100
        return {
            "application_id": application["id"], "access_state": application["access_state"], "plan_code": application["plan_code"],
            "payment_reference": application["payment_reference"], "amount": f"{self.config['business']['currency']} {amount:,.2f}",
            "bank_transfer": {
                "account_name": bank["account_name"], "bsb": bank["bsb"], "account_number": bank["account_number"],
                "instruction": "Transfer the exact amount and use the exact reference. For monthly access, configure a recurring transfer at your bank using this amount and reference.",
            },
            "activation": "Access changes only after an authorised local reconciliation confirms an exact reference-and-amount match from a bank transaction export.",
        }

    def __call__(self, environ: dict[str, Any], start_response: Callable) -> list[bytes]:
        request_id = uuid.uuid4().hex
        method, path = environ.get("REQUEST_METHOD", "GET").upper(), environ.get("PATH_INFO", "/")
        try:
            if self.require_https and path != "/health" and not self.is_https(environ):
                return self.response(start_response, environ, request_id, HTTPStatus.UPGRADE_REQUIRED, {"error": "HTTPS is required."})
            if method == "GET" and path in ("/", "/index.html"):
                return self.html(start_response, environ, request_id, "index.html")
            if method == "GET" and path == "/health":
                return self.response(start_response, environ, request_id, HTTPStatus.OK, {"ok": True, "service": "ethinx-sovereign-revenue-loop", "time": datetime.now(UTC).replace(microsecond=0).isoformat()})
            if method == "GET" and path == "/health/ready":
                return self.health_response(start_response, environ, request_id)
            if method == "GET" and path.startswith("/health/jobs/"):
                return self.health_response(start_response, environ, request_id, path.removeprefix("/health/jobs/"))
            if method == "GET" and path == "/api/availability":
                if not self.allow_request(environ, "public"):
                    return self.response(start_response, environ, request_id, HTTPStatus.TOO_MANY_REQUESTS, {"error": "Rate limit exceeded."}, [("Retry-After", str(self.window_seconds))])
                return self.response(start_response, environ, request_id, HTTPStatus.OK, {"plans": self.store.seat_availability()})
            if method == "POST" and path == "/api/applications":
                if not self.allow_request(environ, "public"):
                    return self.response(start_response, environ, request_id, HTTPStatus.TOO_MANY_REQUESTS, {"error": "Rate limit exceeded."}, [("Retry-After", str(self.window_seconds))])
                payload = self.parse_json(environ)
                email = clean_text(payload.get("email"), 254).lower()
                if not EMAIL_PATTERN.match(email):
                    raise DomainError("A valid email address is required.")
                application = self.store.create_application(clean_text(payload.get("full_name"), 120), email, clean_text(payload.get("company_name"), 160), clean_text(payload.get("plan_code"), 30))
                return self.response(start_response, environ, request_id, HTTPStatus.CREATED, self.application_instructions(application))
            if path.startswith("/api/admin/"):
                if not self.allow_request(environ, "admin"):
                    return self.response(start_response, environ, request_id, HTTPStatus.TOO_MANY_REQUESTS, {"error": "Rate limit exceeded."}, [("Retry-After", str(self.window_seconds))])
                self.require_admin(environ)
                if method == "GET" and path == "/api/admin/summary":
                    return self.response(start_response, environ, request_id, HTTPStatus.OK, self.store.export_summary())
                if method == "GET" and path == "/api/admin/operations":
                    return self.response(start_response, environ, request_id, HTTPStatus.OK, self.operations_status())
                if method == "GET" and path == "/api/admin/content":
                    return self.response(start_response, environ, request_id, HTTPStatus.OK, {"posts": self.store.list_content()})
                if method == "GET" and path == "/api/admin/notifications":
                    return self.response(start_response, environ, request_id, HTTPStatus.OK, {"notifications": self.store.list_operator_notifications()})
                if method == "GET" and path == "/api/admin/review-queue":
                    return self.response(start_response, environ, request_id, HTTPStatus.OK, {"queue": self.store.list_review_queue()})
                if method == "GET" and path == "/api/admin/retention-review":
                    return self.response(start_response, environ, request_id, HTTPStatus.OK, self.store.retention_review())
                if method == "POST" and path.startswith("/api/admin/notifications/") and path.endswith("/ack"):
                    segments = path.split("/")
                    if len(segments) != 6 or not segments[4].isdigit():
                        raise DomainError("Invalid notification acknowledgement path.")
                    self.store.acknowledge_notification(int(segments[4]))
                    return self.response(start_response, environ, request_id, HTTPStatus.OK, {"ok": True})
                if method == "POST" and path.startswith("/api/admin/review-queue/") and path.endswith("/resolve"):
                    segments = path.split("/")
                    if len(segments) != 6 or not segments[4].isdigit():
                        raise DomainError("Invalid review-resolution path.")
                    payload = self.parse_json(environ)
                    self.store.resolve_review_item(int(segments[4]), clean_text(payload.get("resolution_note"), 1000))
                    return self.response(start_response, environ, request_id, HTTPStatus.OK, {"ok": True})
                if method == "POST" and path == "/api/admin/evaluate-access":
                    return self.response(start_response, environ, request_id, HTTPStatus.OK, {"changes": self.store.evaluate_access()})
                if method == "POST" and path == "/api/admin/content/mark-published":
                    payload = self.parse_json(environ)
                    self.store.mark_content_published(clean_text(payload.get("content_date"), 10), clean_text(payload.get("external_reference"), 300))
                    return self.response(start_response, environ, request_id, HTTPStatus.OK, {"ok": True})
            return self.response(start_response, environ, request_id, HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except PermissionError as exc:
            return self.response(start_response, environ, request_id, HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
        except DomainError as exc:
            return self.response(start_response, environ, request_id, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            return self.response(start_response, environ, request_id, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error."})


class QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    config = load_config()
    host = os.environ.get("ETHINX_BIND", "127.0.0.1")
    port = int(os.environ.get("ETHINX_PORT", "8094"))
    data_dir = Path(os.environ.get("ETHINX_DATA_DIR", ROOT / "data"))
    db_path = Path(os.environ.get("ETHINX_DB", data_dir / "ethinx.sqlite3"))
    server = make_server(host, port, App(config, db_path), handler_class=QuietHandler)
    print(f"ETHINX sovereign revenue loop listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
