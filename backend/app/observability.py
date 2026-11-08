"""Sentry helpers shared by API routes, background workers and providers.

The reliability dashboard keys off *event tags*, not span data.  Sentry only
exports spans when they belong to a sampled transaction, so the previous
pattern (``start_span`` → leave the span → ``capture_exception``) produced
exception events with **no** generation context.  Every helper here sets tags
on a fresh scope *before* the event is captured, and always stamps the release.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from contextlib import contextmanager
from typing import Any, Mapping

_MAX_TAG_LEN = 200
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_TOKEN_RE = re.compile(
    r"(?i)(xi-api-key|authorization|bearer|api[_-]?key|token)\s*[:=]\s*\S+")

#: Fields required by the "FacelessForge — Generation Reliability" dashboard.
REQUIRED_TAGS = (
    "release", "operation", "stage", "terminal_status", "provider", "model",
    "error_code", "attempt_number", "retry_count", "output_saved",
    "frontend_outcome", "backend_outcome", "duration_ms",
    "requested_duration_seconds", "scene_count", "attached_clip_count",
    "asset_candidate_count", "thumbnail_set_id", "option_index",
    "audio_duration_seconds", "client_disconnected",
    "completed_after_disconnect", "idempotency_result",
)

_RELEASE_CACHE: str | None = None


def release() -> str:
    """Deployment release: explicit env first, then git SHA, then "unknown"."""
    global _RELEASE_CACHE
    if _RELEASE_CACHE:
        return _RELEASE_CACHE
    for key in ("SENTRY_RELEASE", "GIT_SHA", "SOURCE_VERSION", "GITHUB_SHA"):
        value = (os.getenv(key) or "").strip()
        if value:
            _RELEASE_CACHE = value
            return value
    try:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=1.5,
        )
        value = (proc.stdout or "").strip()
        if proc.returncode == 0 and value:
            _RELEASE_CACHE = value
            return value
    except Exception:  # noqa: BLE001 — observability must never break startup
        pass
    _RELEASE_CACHE = "unknown"
    return _RELEASE_CACHE


def environment() -> str:
    return (os.getenv("SENTRY_ENVIRONMENT") or os.getenv("ENVIRONMENT")
            or os.getenv("APP_ENV") or "production").strip()


def init_kwargs(dsn: str) -> dict:
    """Keyword arguments for ``sentry_sdk.init`` (release + environment + PII)."""
    return {
        "dsn": dsn,
        "environment": environment(),
        "release": release(),
        # Anonymous user id + role only — never an email address.
        "send_default_pii": False,
        "traces_sample_rate": 0.1,
    }


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        value = ",".join(str(v) for v in value)
    elif isinstance(value, dict):
        value = json.dumps(value, sort_keys=True, default=str)
    text = str(value)
    text = _EMAIL_RE.sub("[email]", text)
    text = _TOKEN_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    return text[:_MAX_TAG_LEN]


def _apply_tags(scope, tags: Mapping[str, Any] | None) -> None:
    scope.set_tag("release", release())
    for key, value in (tags or {}).items():
        cleaned = _clean(value)
        if cleaned is not None:
            scope.set_tag(str(key), cleaned)


def anonymous_user(user: Mapping[str, Any] | str | None,
                   role: str | None = None) -> dict | None:
    """Return a Sentry user dict with an opaque id only (never email)."""
    if isinstance(user, Mapping):
        uid = user.get("id") or user.get("user_id")
        role = role or user.get("role")
    else:
        uid = user
    if not uid:
        return None
    payload = {"id": str(uid)}
    if role:
        payload["role"] = str(role)
    return payload


def set_user_anonymous(user: Mapping[str, Any] | str | None,
                       role: str | None = None) -> None:
    try:
        import sentry_sdk

        payload = anonymous_user(user, role)
        if payload:
            sentry_sdk.set_user(payload)
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def sentry_scope(tags: Mapping[str, Any] | None = None,
                 user: Mapping[str, Any] | str | None = None,
                 role: str | None = None):
    """Fork the current scope, stamp tags, and yield it.

    Yields ``None`` when Sentry is unavailable so callers never need a guard.
    """
    try:
        import sentry_sdk

        manager = sentry_sdk.new_scope()
    except Exception:  # noqa: BLE001
        yield None
        return
    with manager as scope:
        try:
            _apply_tags(scope, tags)
            payload = anonymous_user(user, role)
            if payload:
                scope.set_user(payload)
        except Exception:  # noqa: BLE001
            pass
        yield scope


def capture_exception(exc: BaseException, *,
                      tags: Mapping[str, Any] | None = None,
                      user: Mapping[str, Any] | str | None = None,
                      role: str | None = None) -> None:
    """Capture ``exc`` with tags already attached to the event scope."""
    try:
        import sentry_sdk

        with sentry_scope(tags, user=user, role=role):
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001
        pass


def capture_message(message: Any, *, level: str = "warning",
                    tags: Mapping[str, Any] | None = None,
                    user: Mapping[str, Any] | str | None = None,
                    role: str | None = None,
                    fingerprint: list[str] | None = None) -> None:
    """Capture a non-exception failure (HTTPException, provider exhaustion)."""
    try:
        import sentry_sdk

        with sentry_scope(tags, user=user, role=role) as scope:
            if scope is not None and fingerprint:
                scope.fingerprint = list(fingerprint)
            sentry_sdk.capture_message(_clean(message) or "unknown", level=level)
    except Exception:  # noqa: BLE001
        pass


def record_terminal(operation: str, *,
                    tags: Mapping[str, Any] | None = None,
                    status: str = "ok",
                    user: Mapping[str, Any] | str | None = None,
                    role: str | None = None) -> None:
    """Emit a sampled transaction carrying the terminal generation fields.

    Errors are always captured separately; this records successful/degraded
    terminal outcomes so the reliability dashboard can count them too.
    """
    try:
        import sentry_sdk

        merged = {"operation": operation, **(tags or {})}
        with sentry_scope(merged, user=user, role=role):
            txn = sentry_sdk.start_transaction(op=operation, name=operation)
            for key, value in merged.items():
                cleaned = _clean(value)
                if cleaned is not None:
                    txn.set_tag(str(key), cleaned)
            txn.set_status(status)
            txn.finish()
    except Exception:  # noqa: BLE001
        pass


def user_from_request(request) -> dict | None:
    """Decode the access token just enough to attach an anonymous Senty user.

    Signature is verified with the app secret; on any failure we simply leave
    the event anonymous.  Email is deliberately discarded.
    """
    try:
        from .auth import get_token_identity

        return get_token_identity(request)
    except Exception:  # noqa: BLE001
        return None
