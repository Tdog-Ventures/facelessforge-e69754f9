"""Failure alerting for FacelessForge (Gotify/Gatus-compatible).

Topology (verified): this app cannot reach Gotify directly — Gotify/Gatus
live on another docker network and the app socket is loopback-bound. Two paths:
  1. Gatus polls GET /api/alerts/evaluate over the public URL and pages on
     rule failures (no app-to-Gotify network needed).
  2. Optional direct dispatch when ALERTS_ENABLED=true plus GOTIFY_URL/TOKEN
     are configured (operator-owned network route).

Alerting never breaks the request path: every send is short-timeout,
fail-silent, per-key cooldowned, and disabled by default. Secrets are never
logged — only hostnames and rule names.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("facelessforge.alerting")

DEFAULT_COOLDOWN_SECONDS = int(os.environ.get("ALERT_COOLDOWN_SECONDS", "3600"))
DISK_THRESHOLD_PCT = float(os.environ.get("ALERT_DISK_PCT", "80"))
FAILED_WINDOW_MINUTES = int(os.environ.get("ALERT_FAILED_WINDOW_MIN", "60"))
SCHEDULER_MAX_AGE_HOURS = float(os.environ.get("ALERT_SCHEDULER_MAX_AGE_H", "26"))

_LAST_SENT: dict[str, float] = {}


def _now():
    return datetime.now(timezone.utc)


def is_enabled() -> bool:
    return (os.environ.get("ALERTS_ENABLED", "false") or "").strip().lower() in ("1", "true", "yes", "on")


def _config() -> tuple[Optional[str], bool]:
    url = (os.environ.get("GOTIFY_URL") or "").strip().rstrip("/")
    token_present = bool(os.environ.get("GOTIFY_TOKEN"))
    if not url or not token_present:
        return None, False
    return url, True


def _cooldown_ok(key: str) -> bool:
    last = _LAST_SENT.get(key, 0)
    return (time.time() - last) >= DEFAULT_COOLDOWN_SECONDS


def _mark_sent(key: str) -> None:
    _LAST_SENT[key] = time.time()


async def notify(key: str, title: str, message: str,
                 fields: Optional[dict] = None, priority: int = 5) -> dict:
    """Send one alert. Never raises; returns a delivery report dict."""
    safe_fields = {k: v for k, v in (fields or {}).items()
                   if "token" not in k.lower() and "secret" not in k.lower() and "key" not in k.lower()}
    if not is_enabled():
        logger.info("facelessforge.alerting suppressed (disabled) key=%s title=%s", key, title)
        return {"sent": False, "reason": "disabled", "key": key}
    if not _cooldown_ok(key):
        logger.info("facelessforge.alerting suppressed (cooldown) key=%s", key)
        return {"sent": False, "reason": "cooldown", "key": key}
    url, configured = _config()
    if not configured:
        logger.warning("facelessforge.alerting unconfigured key=%s (need GOTIFY_URL + GOTIFY_TOKEN)", key)
        return {"sent": False, "reason": "unconfigured", "key": key}
    try:
        token = os.environ.get("GOTIFY_TOKEN", "")
        body = json.dumps({
            "title": f"[FacelessForge] {title}",
            "message": message[:2000],
            "priority": priority,
            "extras": {"client::display": {"contentType": "text/plain"}},
        }).encode()
        req = urllib.request.Request(
            f"{url}/message?token={token}", data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            _mark_sent(key)
            logger.info("facelessforge.alerting dispatched key=%s status=%s fields=%s",
                        key, resp.status, sorted(safe_fields))
            return {"sent": True, "key": key, "status": resp.status}
    except Exception as exc:  # noqa: BLE001 — alerting must never break callers
        logger.warning("facelessforge.alerting dispatch failed key=%s: %s: %s",
                       key, type(exc).__name__, str(exc)[:200])
        return {"sent": False, "reason": "dispatch_failed", "key": key,
                "error": f"{type(exc).__name__}"}


def disk_usage_pct(path: str = "/") -> float:
    usage = shutil.disk_usage(path)
    return round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0


async def evaluate(db) -> dict:
    """Evaluate alert rules without sending. Safe to poll (Gatus-compatible)."""
    rules: list[dict[str, Any]] = []
    try:
        pct = disk_usage_pct()
        rules.append({"rule": "disk_usage", "ok": pct < DISK_THRESHOLD_PCT,
                      "detail": f"{pct}% used (threshold {DISK_THRESHOLD_PCT}%)"})
    except Exception as exc:  # noqa: BLE001
        rules.append({"rule": "disk_usage", "ok": False, "detail": f"probe failed: {exc}"[:200]})
    try:
        cutoff = _now().timestamp() - FAILED_WINDOW_MINUTES * 60
        recent_failed = 0
        if hasattr(db.render_jobs, "count_documents"):
            from datetime import datetime as _dt
            recent_failed = await db.render_jobs.count_documents({
                "status": {"$in": ["failed", "FAILED"]},
                "updated_at": {"$gte": _dt.fromtimestamp(cutoff, tz=timezone.utc)},
            })
        rules.append({"rule": "failed_jobs_recent", "ok": recent_failed == 0,
                      "detail": f"{recent_failed} failed in last {FAILED_WINDOW_MINUTES}min"})
    except Exception as exc:  # noqa: BLE001
        rules.append({"rule": "failed_jobs_recent", "ok": False, "detail": f"probe failed: {exc}"[:200]})
    try:
        setting = await db.forge_settings.find_one({"id": "default"}, {"_id": 0}) or {}
        enabled = bool(setting.get("auto_post_enabled"))
        last = await db.forge_runs.find_one({}, {"_id": 0}, sort=[("created_at", -1)])
        if not enabled:
            rules.append({"rule": "scheduler", "ok": True,
                          "detail": "automation OFF — missed-run detection disabled by design"})
        elif not last:
            rules.append({"rule": "scheduler", "ok": False, "detail": "automation ON but no runs recorded"})
        else:
            created = last.get("created_at")
            age_h = ((_now() - created).total_seconds() / 3600) if created else 999.0
            failed = str(last.get("status") or "").lower() == "failed"
            ok = (not failed) and age_h <= SCHEDULER_MAX_AGE_HOURS
            rules.append({"rule": "scheduler", "ok": ok,
                          "detail": f"last run {last.get('status')} {age_h:.1f}h ago"})
    except Exception as exc:  # noqa: BLE001
        rules.append({"rule": "scheduler", "ok": False, "detail": f"probe failed: {exc}"[:200]})
    try:
        from .storage import get_storage
        probe = get_storage().probe()
        rules.append({"rule": "object_storage", "ok": bool(probe.get("ok")),
                      "detail": f"mode={probe.get('mode')} latency={probe.get('latency_ms')}ms err={probe.get('error')}"})
    except Exception as exc:  # noqa: BLE001
        rules.append({"rule": "object_storage", "ok": False, "detail": f"probe failed: {exc}"[:200]})
    failing = [r["rule"] for r in rules if not r["ok"]]
    return {"ok": not failing, "failing": failing, "rules": rules,
            "alerts_enabled": is_enabled(), "checked_at": _now().isoformat()}
