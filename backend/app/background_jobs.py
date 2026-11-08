"""Mongo-backed async job runners for voiceover and thumbnail generation.

Why this module exists
----------------------
The API used to hold a synchronous HTTP request open for the whole ElevenLabs
or image-provider call.  When Caddy/proxy hit its response-header timeout the
browser saw a failure even though the provider had already produced (and been
charged for) the artifact.  A retry then paid for the same script twice.

Jobs are persisted *before* any provider call.  A unique idempotency key makes
a duplicate request return the existing job/artifact instead of calling the
provider again.  Workers are asyncio loops owned by the API process (one
uvicorn worker in this deployment), claim queued jobs atomically, and keep
running after the originating request has returned 202.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from pymongo import ReturnDocument

from . import observability as obs
from .db import get_db

logger = logging.getLogger("facelessforge.jobs")

VOICEOVER_JOBS = "voiceover_jobs"
THUMBNAIL_JOBS = "thumbnail_jobs"
THUMBNAIL_SETS = "thumbnail_sets"

POLL_SECONDS = float(os.environ.get("JOB_WORKER_POLL_SECONDS", "1.5"))
VOICE_CONCURRENCY = max(1, int(os.environ.get("VOICEOVER_WORKER_CONCURRENCY", "2")))
THUMB_CONCURRENCY = max(1, int(os.environ.get("THUMBNAIL_WORKER_CONCURRENCY", "2")))
DISCONNECT_GRACE_SECONDS = float(os.environ.get("JOB_DISCONNECT_GRACE_SECONDS", "15"))
VOICE_LEASE_SECONDS = max(30, int(os.environ.get("VOICEOVER_JOB_LEASE_SECONDS", "300")))
VOICE_HEARTBEAT_SECONDS = max(5, int(os.environ.get("VOICEOVER_JOB_HEARTBEAT_SECONDS", "30")))
VOICEOVER_LOCKS = "voiceover_locks"
WORKER_ID = f"{os.uname().nodename}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

_workers: list[asyncio.Task] = []
_stop = asyncio.Event()
_voice_tasks: set[asyncio.Task] = set()
_thumb_tasks: set[asyncio.Task] = set()
_started = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _public_asset(asset: dict | None) -> dict | None:
    """Strip internal paths before persisting a job result for the browser."""
    if not asset:
        return None
    keep = (
        "id", "project_id", "scene_id", "name", "asset_type", "source",
        "preview_url", "url", "duration", "duration_measured", "size_bytes",
        "word_count", "provider", "voice_id", "voice_style", "tone", "status",
        "model", "width", "height", "storage_mode", "thumbnail_set_id",
        "option_index", "attempt_number", "created_at", "updated_at",
    )
    return {k: asset.get(k) for k in keep if k in asset}


def _sanitize_message(value) -> str:
    text = str(value or "")
    for token in ("ELEVENLABS_API_KEY", "xi-api-key", "Authorization", "Bearer "):
        if token.lower() in text.lower():
            return "provider error (details redacted)"
    return text[:500]


def _classify_exception(exc: BaseException) -> tuple[str, str]:
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(token in text for token in (
        "serverselectiontimeout", "autoreconnect", "connectionfailure",
        "networktimeout", "mongo", "connection refused", "connection reset",
    )):
        return "mongo_unreachable", _sanitize_message(exc)
    if "timeout" in text:
        return "provider_timeout", _sanitize_message(exc)
    if "rate" in text and "limit" in text:
        return "provider_rate_limited", _sanitize_message(exc)
    return "voiceover_provider_error", _sanitize_message(exc)


def _http_error(exc) -> tuple[str, str, dict | None]:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        code = str(detail.get("code") or f"http_{getattr(exc, 'status_code', 500)}")
        message = _sanitize_message(detail.get("message") or detail)
        return code, message, detail
    return (
        f"http_{getattr(exc, 'status_code', 500)}",
        _sanitize_message(detail if detail is not None else exc),
        None,
    )


async def ensure_job_indexes() -> None:
    db = get_db()
    try:
        # Legacy job documents predate idempotency_key and carry null.  A plain
        # unique index cannot be built over them (E11000 on null), which would
        # silently leave concurrent duplicate requests unprotected.  Index only
        # real keys; every new job writes a string key before any provider call.
        await db[VOICEOVER_JOBS].create_index(
            "idempotency_key",
            unique=True,
            name="idempotency_key_unique",
            partialFilterExpression={"idempotency_key": {"$type": "string"}},
        )
        await db[VOICEOVER_JOBS].create_index([("project_id", 1), ("created_at", -1)])
        await db[VOICEOVER_JOBS].create_index([("status", 1), ("created_at", 1)])
        await db[VOICEOVER_JOBS].create_index(
            "id",
            unique=True,
            name="voiceover_job_id_unique",
            partialFilterExpression={"id": {"$type": "string"}},
        )
        await db["voiceover_attempts"].create_index("id", unique=True, sparse=True)
        await db["voiceover_attempts"].create_index([("project_id", 1), ("created_at", -1)])
        await db["voiceover_attempts"].create_index([("job_id", 1), ("updated_at", -1)])
        await db[VOICEOVER_LOCKS].create_index("id", unique=True)
        await db[VOICEOVER_LOCKS].create_index("expires_at", expireAfterSeconds=0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("voiceover job index creation skipped: %s", exc)
    try:
        await db[THUMBNAIL_JOBS].create_index([("set_id", 1), ("option_index", 1)], unique=True)
        await db[THUMBNAIL_JOBS].create_index([("status", 1), ("created_at", 1)])
        await db[THUMBNAIL_JOBS].create_index([("project_id", 1), ("created_at", -1)])
        await db[THUMBNAIL_SETS].create_index([("project_id", 1), ("created_at", -1)])
        await db[THUMBNAIL_SETS].create_index("id", unique=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("thumbnail job index creation skipped: %s", exc)


# ============================ VOICEOVER ============================

def voiceover_key(project_id: str, script_hash: str, voice_style: str,
                  voice_id: str, model: str, text_override: str | None) -> str:
    raw = json.dumps({
        "project_id": project_id,
        "script_hash": script_hash,
        "voice_style": voice_style or "",
        "voice_id": voice_id or "",
        "model": model or "",
        "text_override": _hash(text_override or ""),
    }, sort_keys=True)
    return _hash(raw)


def scene_voiceover_key(
    project_id: str, scene_id: str, script_hash: str,
    voice_id: str = "", tone: str | None = None,
) -> str:
    """Idempotency key for a scene take: hash(project + scene + script + voice).

    A scene re-open recovers the existing take; any narration edit, voice or
    tone change creates a distinct job. An explicit ``force`` still bypasses
    the recovery path.
    """
    return _hash(f"{project_id}{scene_id}{script_hash}{voice_id or ''}{tone or ''}")


def _lock_id(project_id: str, kind: str, scene_id: str | None = None) -> str:
    return f"{project_id}:{kind}:{scene_id or 'full'}"


async def acquire_voiceover_lock(
    *, project_id: str, kind: str, scene_id: str | None, job_id: str
) -> bool:
    """Acquire one synthesis slot per project/kind/scene.

    The unique ``id`` is the lock. Expired locks are replaced atomically so a
    dead worker cannot wedge the project forever. The TTL is a backstop; normal
    completion deletes the lock.
    """
    db = get_db()
    lock_id = _lock_id(project_id, kind, scene_id)
    now = _now()
    expires = now + timedelta(seconds=VOICE_LEASE_SECONDS)
    try:
        await db[VOICEOVER_LOCKS].insert_one({
            "_id": lock_id, "id": lock_id, "project_id": project_id, "kind": kind,
            "scene_id": scene_id, "job_id": job_id,
            "created_at": now, "updated_at": now, "expires_at": expires,
        })
        return True
    except Exception as exc:  # duplicate or transient
        if "duplicate" not in str(exc).lower() and getattr(exc, "code", None) != 11000:
            raise
        try:
            updated = await db[VOICEOVER_LOCKS].find_one_and_update(
                {"_id": lock_id, "expires_at": {"$lt": now}},
                {"$set": {"job_id": job_id, "updated_at": now, "expires_at": expires}},
                return_document=ReturnDocument.AFTER,
            )
            return bool(updated)
        except Exception:  # noqa: BLE001
            return False


async def release_voiceover_lock(
    *, project_id: str, kind: str, scene_id: str | None, job_id: str | None = None
) -> None:
    try:
        query = {"_id": _lock_id(project_id, kind, scene_id)}
        if job_id:
            query["job_id"] = job_id
        await get_db()[VOICEOVER_LOCKS].delete_one(query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("voiceover lock release failed: %s", exc)


async def refresh_voiceover_lock(
    *, project_id: str, kind: str, scene_id: str | None, job_id: str
) -> None:
    try:
        await get_db()[VOICEOVER_LOCKS].update_one(
            {"_id": _lock_id(project_id, kind, scene_id), "job_id": job_id},
            {"$set": {"updated_at": _now(),
                      "expires_at": _now() + timedelta(seconds=VOICE_LEASE_SECONDS)}},
        )
    except Exception:  # noqa: BLE001
        pass


async def create_voiceover_job(
    *,
    project_id: str,
    requested_by: str,
    requested_role: str,
    script_id: str | None,
    script_hash: str,
    text_override: str | None,
    voice_style: str,
    voice_id: str,
    provider: str,
    model: str,
    tone: str | None = None,
    force: bool = False,
    scene_id: str | None = None,
    kind: str = "full_script",
    requested_duration_seconds: int | None = None,
    scene_count: int | None = None,
    text_snapshot: str | None = None,
) -> tuple[dict, bool]:
    """Create or reuse a voiceover job.

    Returns ``(job, recovered)``.  ``recovered`` is True only when an existing
    successful job for the same idempotency key was returned without a provider
    call.  An active job is returned as-is so concurrent requests cannot cause a
    second charge.  ``kind`` is ``full_script`` or ``scene``.
    """
    db = get_db()
    if scene_id:
        base_key = scene_voiceover_key(
            project_id, scene_id, script_hash, voice_id=voice_id, tone=tone)
    else:
        base_key = voiceover_key(
            project_id, script_hash, voice_style, voice_id, model, text_override)
    coll = db[VOICEOVER_JOBS]
    now = _now()

    existing = await coll.find_one({"idempotency_key": base_key}, {"_id": 0})
    if existing:
        status = str(existing.get("status") or "").lower()
        if status in ("queued", "running"):
            existing["idempotency_result"] = "active"
            return existing, False
        if status == "succeeded" and not force:
            existing["idempotency_result"] = "recovered"
            return existing, True
        if status == "failed" and not force:
            await release_voiceover_lock(
                project_id=project_id, kind=kind, scene_id=scene_id,
                job_id=existing.get("id"),
            )
            attempt = await db["voiceover_attempts"].find_one(
                {"job_id": existing.get("id")}, {"_id": 0},
                sort=[("updated_at", -1)],
            )
            outcome = (attempt or {}).get("provider_outcome")
            started = bool((attempt or {}).get("provider_call_started"))
            tracked = "provider_call_started" in existing
            if (
                (not attempt and tracked and not existing.get("provider_call_started")
                 and existing.get("provider_outcome") == "none")
                or (attempt and outcome == "none" and not started)
            ):
                # No chargeable call was made (validation/pre-provider failure):
                # requeueing is safe and does not risk a duplicate charge.
                updated = await coll.find_one_and_update(
                    {"id": existing["id"], "status": "failed"},
                    {"$set": {
                        "status": "queued",
                        "terminal_status": "pending",
                        "attempt_number": int(existing.get("attempt_number") or 1) + 1,
                        "error_code": None,
                        "error_message": None,
                        "error_detail": None,
                        "output_saved": False,
                        "started_at": None,
                        "completed_at": None,
                        "completed_after_disconnect": False,
                        "idempotency_result": "retry",
                        "updated_at": now,
                    }},
                    return_document=ReturnDocument.AFTER,
                )
                if updated:
                    updated["idempotency_result"] = "retry"
                    return updated, False
            # Anything that reached a paid provider is NOT retried implicitly.
            # Surface what is knowable so the caller can reconcile the existing
            # file or explicitly force a new attempt after reviewing usage.
            existing["idempotency_result"] = (
                "recoverable_audio" if outcome == "succeeded" and (attempt or {}).get("audio_path")
                else "needs_reconciliation"
            )
            existing["needs_reconciliation"] = True
            existing["attempt"] = attempt
            existing["retry_requires_force"] = True
            return existing, False
        if status == "failed" and force:
            await release_voiceover_lock(
                project_id=project_id, kind=kind, scene_id=scene_id,
                job_id=existing.get("id"),
            )

    key = base_key if not force else f"{base_key}:force:{uuid.uuid4()}"
    job_id = str(uuid.uuid4())
    reuse_asset_id = None
    if existing and force:
        prior_attempt = await db["voiceover_attempts"].find_one(
            {"job_id": existing.get("id")}, {"_id": 0},
            sort=[("updated_at", -1)],
        )
        if (
            prior_attempt
            and prior_attempt.get("provider_outcome") == "succeeded"
            and prior_attempt.get("audio_path")
            and not prior_attempt.get("asset_registered")
        ):
            existing["idempotency_result"] = "recoverable_audio"
            existing["needs_reconciliation"] = True
            existing["retry_requires_force"] = False
            existing["attempt"] = prior_attempt
            return existing, False
        if (
            prior_attempt
            and int(prior_attempt.get("chunks_completed") or 0) > 0
            and not prior_attempt.get("asset_registered")
        ):
            # Explicit retry of a partial take: reuse the same asset id so the
            # durable chunk manifest can reuse every already-paid chunk.
            reuse_asset_id = existing.get("asset_id")
    asset_id = reuse_asset_id or str(uuid.uuid4())
    acquired = await acquire_voiceover_lock(
        project_id=project_id, kind=kind, scene_id=scene_id, job_id=job_id
    )
    if not acquired:
        active = await coll.find_one(
            {"project_id": project_id, "kind": kind,
             "scene_id": scene_id, "status": {"$in": ["queued", "running"]}},
            {"_id": 0}, sort=[("created_at", 1)],
        )
        if active:
            active["idempotency_result"] = "active"
            return active, False
        raise RuntimeError(
            "Another voiceover request is starting for this project. Retry in a moment."
        )
    doc = {
        "id": job_id,
        "project_id": project_id,
        "kind": kind,
        "scene_id": scene_id,
        "requested_by": requested_by,
        "requested_role": requested_role,
        "script_id": script_id,
        "script_hash": script_hash,
        "text_override": text_override,
        "text_snapshot": text_snapshot,
        "voice_style": voice_style,
        "voice_id": voice_id,
        "tone": tone,
        "provider": provider,
        "model": model,
        "idempotency_key": key,
        "base_idempotency_key": base_key,
        "idempotency_result": "created",
        "force": bool(force),
        "status": "queued",
        "terminal_status": "pending",
        "attempt_number": 1,
        "output_saved": False,
        "asset_id": asset_id,
        "asset": None,
        "provider_call_started": False,
        "provider_outcome": "none",
        "needs_reconciliation": False,
        "lease_token": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "error_code": None,
        "error_message": None,
        "error_detail": None,
        "client_disconnected": False,
        "completed_after_disconnect": False,
        "last_polled_at": None,
        "requested_duration_seconds": requested_duration_seconds,
        "scene_count": scene_count,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "completed_at": None,
        "duration_ms": None,
    }
    try:
        await coll.insert_one(dict(doc))
        return doc, False
    except Exception as exc:  # duplicate key = concurrent request won the insert
        await release_voiceover_lock(
            project_id=project_id, kind=kind, scene_id=scene_id, job_id=job_id
        )
        if "duplicate" not in str(exc).lower() and getattr(exc, "code", None) != 11000:
            raise
        existing = await coll.find_one({"idempotency_key": key}, {"_id": 0})
        if existing:
            return existing, str(existing.get("status") or "").lower() == "succeeded"
        raise


async def get_voiceover_job(project_id: str, job_id: str, *, touch: bool = True) -> dict | None:
    db = get_db()
    job = await db[VOICEOVER_JOBS].find_one(
        {"id": job_id, "project_id": project_id}, {"_id": 0})
    if job and touch:
        await db[VOICEOVER_JOBS].update_one(
            {"id": job_id}, {"$set": {"last_polled_at": _now(), "updated_at": _now()}})
        job["last_polled_at"] = _now()
    return job


async def list_voiceover_jobs(project_id: str, limit: int = 20) -> list[dict]:
    db = get_db()
    return await db[VOICEOVER_JOBS].find(
        {"project_id": project_id}, {"_id": 0}
    ).sort("created_at", -1).to_list(max(1, min(int(limit or 20), 100)))


async def run_voiceover_job(job_id: str) -> None:
    db = get_db()
    now = _now()
    lease_token = uuid.uuid4().hex
    lease_expires = now + timedelta(seconds=VOICE_LEASE_SECONDS)
    claim_set = {
        "status": "running", "started_at": now, "updated_at": now,
        "lease_token": lease_token, "lease_owner": WORKER_ID,
        "lease_expires_at": lease_expires, "heartbeat_at": now,
    }
    job = await db[VOICEOVER_JOBS].find_one_and_update(
        {"id": job_id, "status": "queued"},
        {"$set": claim_set},
        return_document=ReturnDocument.AFTER,
    )
    if not job:
        # Reclaim only a running job whose lease expired BEFORE any provider
        # call. A lease that expired after provider_call_started is reconciled,
        # never silently re-synthesized.
        job = await db[VOICEOVER_JOBS].find_one_and_update(
            {
                "id": job_id,
                "status": "running",
                "provider_call_started": {"$ne": True},
                "lease_expires_at": {"$lt": now},
            },
            {"$set": claim_set},
            return_document=ReturnDocument.AFTER,
        )
    if not job:
        return

    kind = job.get("kind") or ("scene" if job.get("scene_id") else "full_script")
    scene_id = job.get("scene_id")
    heartbeat_stop = asyncio.Event()

    async def _heartbeat() -> None:
        while not heartbeat_stop.is_set():
            try:
                await asyncio.wait_for(heartbeat_stop.wait(), timeout=VOICE_HEARTBEAT_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await db[VOICEOVER_JOBS].update_one(
                    {"id": job_id, "lease_token": lease_token},
                    {"$set": {
                        "heartbeat_at": _now(), "updated_at": _now(),
                        "lease_expires_at": _now() + timedelta(seconds=VOICE_LEASE_SECONDS),
                    }},
                )
                await refresh_voiceover_lock(
                    project_id=job["project_id"], kind=kind,
                    scene_id=scene_id, job_id=job_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("voiceover heartbeat failed job=%s: %s", job_id, exc)

    heartbeat = asyncio.create_task(_heartbeat())

    from .routes import api as api_routes
    from .models import GenerateVoiceoverRequest
    from fastapi import HTTPException

    async def output_saved(asset: dict) -> None:
        await db[VOICEOVER_JOBS].update_one(
            {"id": job_id},
            {"$set": {
                "output_saved": True,
                "asset_id": asset.get("id") or job.get("asset_id"),
                "asset": _public_asset(asset),
                "provider_call_started": True,
                "provider_outcome": "succeeded",
                "updated_at": _now(),
            }},
        )

    body = GenerateVoiceoverRequest(
        voice_style=job.get("voice_style"),
        tone=job.get("tone"),
        text_override=job.get("text_override"),
    )
    user = {
        "id": job.get("requested_by"),
        "role": job.get("requested_role") or "creator",
    }
    started = time.monotonic()
    try:
        if kind == "scene":
            result = await api_routes._perform_scene_voiceover(
                job["project_id"], job["scene_id"], body, user,
                job=job, on_output_saved=output_saved)
        else:
            result = await api_routes._perform_full_voiceover(
                job["project_id"], body, user, job=job, on_output_saved=output_saved)
        asset = result.get("asset") or {}
        finished = _now()
        current = await db[VOICEOVER_JOBS].find_one(
            {"id": job_id}, {"_id": 0, "client_disconnected": 1, "last_polled_at": 1}) or job
        disconnect = _completed_after_disconnect(current, finished)
        duration_ms = int((time.monotonic() - started) * 1000)
        frontend_outcome = "disconnected" if disconnect else "connected"
        await db[VOICEOVER_JOBS].update_one(
            {"id": job_id},
            {"$set": {
                "status": "succeeded",
                "terminal_status": "success",
                "output_saved": True,
                "asset_id": asset.get("id") or job.get("asset_id"),
                "asset": _public_asset(asset),
                "provider": asset.get("provider") or job.get("provider"),
                "provider_call_started": True,
                "provider_outcome": "succeeded",
                "needs_reconciliation": False,
                "error_code": None,
                "error_message": None,
                "error_detail": None,
                "completed_at": finished,
                "updated_at": finished,
                "duration_ms": duration_ms,
                "audio_duration_seconds": float(asset.get("duration") or 0) or None,
                "backend_outcome": "success",
                "frontend_outcome": frontend_outcome,
                "completed_after_disconnect": disconnect,
                "lease_token": None,
                "lease_expires_at": None,
            }},
        )
        terminal = {
            **job,
            "provider": asset.get("provider") or job.get("provider"),
            "output_saved": True,
            "duration_ms": duration_ms,
            "audio_duration_seconds": float(asset.get("duration") or 0) or None,
            "completed_after_disconnect": disconnect,
            "client_disconnected": bool(current.get("client_disconnected")),
        }
        obs.record_terminal("voiceover.job", tags=_job_tags(
            "voiceover", terminal, stage="finalize",
            terminal_status="success", output_saved=True,
            frontend_outcome=frontend_outcome,
            backend_outcome="success",
            duration_ms=duration_ms,
            audio_duration_seconds=terminal["audio_duration_seconds"],
            completed_after_disconnect=disconnect,
            client_disconnected=terminal["client_disconnected"],
        ), user=user)
    except HTTPException as exc:
        code, message, detail = _http_error(exc)
        await _fail_voiceover(job, code, message, detail, started)
    except Exception as exc:  # noqa: BLE001
        code, message = _classify_exception(exc)
        await _fail_voiceover(job, code, message, None, started, exc=exc)
    finally:
        heartbeat_stop.set()
        heartbeat.cancel()
        try:
            await heartbeat
        except (asyncio.CancelledError, Exception):
            pass
        await release_voiceover_lock(
            project_id=job["project_id"], kind=kind,
            scene_id=scene_id, job_id=job_id,
        )


def _completed_after_disconnect(job: dict, finished: datetime) -> bool:
    if job.get("client_disconnected"):
        return True
    last = job.get("last_polled_at")
    if not last:
        return False
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (finished - last).total_seconds() > DISCONNECT_GRACE_SECONDS


async def _fail_voiceover(job: dict, code: str, message: str,
                          detail: dict | None, started: float,
                          exc: BaseException | None = None) -> None:
    db = get_db()
    finished = _now()
    current = await db[VOICEOVER_JOBS].find_one(
        {"id": job["id"]}, {"_id": 0, "client_disconnected": 1, "last_polled_at": 1}) or job
    duration_ms = int((time.monotonic() - started) * 1000)
    disconnect = _completed_after_disconnect(current, finished)
    frontend_outcome = "disconnected" if disconnect else "connected"
    pre_provider_codes = {
        "script_length_out_of_bounds",
        "script_changed_before_generation",
        "audio_measurement_unavailable",
        "voiceover_in_progress",
    }
    pre_provider = code in pre_provider_codes and not (detail or {}).get("partial_path")
    needs_reconciliation = False if pre_provider else bool(
        (detail or {}).get("needs_reconciliation")
        or (detail or {}).get("provider_outcome") in {"uncertain", "pending"}
    )
    provider_started = False if pre_provider else (
        True if needs_reconciliation else job.get("provider_call_started", False)
    )
    provider_outcome = "none" if pre_provider else (
        "uncertain" if needs_reconciliation else job.get("provider_outcome", "none")
    )
    await db[VOICEOVER_JOBS].update_one(
        {"id": job["id"]},
        {"$set": {
            "status": "failed",
            "terminal_status": "failed",
            "error_code": code,
            "error_message": message,
            "error_detail": detail,
            "output_saved": bool(
                (detail or {}).get("file_path") or (detail or {}).get("audio_path")
            ),
            "provider_call_started": provider_started,
            "provider_outcome": provider_outcome,
            "needs_reconciliation": needs_reconciliation,
            "completed_at": finished,
            "updated_at": finished,
            "duration_ms": duration_ms,
            "backend_outcome": "failed",
            "frontend_outcome": frontend_outcome,
            "completed_after_disconnect": disconnect,
            "lease_token": None,
            "lease_expires_at": None,
        }},
    )
    tags = _job_tags(
        "voiceover", job, stage="provider_call", terminal_status="failed",
        error_code=code, output_saved=False, duration_ms=duration_ms,
        frontend_outcome=frontend_outcome, backend_outcome="failed",
        client_disconnected=bool(current.get("client_disconnected")),
        completed_after_disconnect=disconnect,
    )
    user = {"id": job.get("requested_by"), "role": job.get("requested_role")}
    if exc is not None:
        obs.capture_exception(exc, tags=tags, user=user)
    else:
        obs.capture_message(message, level="error", tags=tags, user=user,
                            fingerprint=["voiceover", str(code)])


def _job_tags(operation: str, job: dict, *, stage: str,
              terminal_status: str, **extra) -> dict:
    """Build the tag set the reliability dashboard filters on.

    All keys are present in every event (``None`` values are dropped at the
    Sentry boundary) so a missing field is distinguishable from an absent tag
    *only* by the caller's failure to pass it.  ``release`` and ``retry_count``
    are always derived, never caller-supplied.
    """
    attempt = int(job.get("attempt_number") or 1)
    failed = str(terminal_status).lower() in ("failed", "cancelled", "canceled")
    tags = {
        "operation": operation,
        "stage": stage,
        "terminal_status": terminal_status,
        "release": obs.release(),
        "provider": job.get("provider"),
        "model": job.get("model"),
        "error_code": job.get("error_code"),
        "attempt_number": attempt,
        "retry_count": max(0, attempt - 1),
        "output_saved": bool(job.get("output_saved")),
        "frontend_outcome": job.get("frontend_outcome") or (
            "disconnected" if job.get("client_disconnected") else "connected"),
        "backend_outcome": job.get("backend_outcome") or (
            "failed" if failed else "success"),
        "duration_ms": job.get("duration_ms"),
        "requested_duration_seconds": job.get("requested_duration_seconds"),
        "scene_count": job.get("scene_count"),
        "attached_clip_count": job.get("attached_clip_count"),
        "asset_candidate_count": job.get("asset_candidate_count"),
        "thumbnail_set_id": job.get("thumbnail_set_id") or job.get("set_id"),
        "option_index": job.get("option_index"),
        "audio_duration_seconds": job.get("audio_duration_seconds"),
        "client_disconnected": bool(job.get("client_disconnected")),
        "completed_after_disconnect": bool(job.get("completed_after_disconnect")),
        "idempotency_result": job.get("idempotency_result"),
        "job_id": job.get("id"),
        "project_id": job.get("project_id"),
        "scene_id": job.get("scene_id"),
    }
    tags.update(extra)
    return {k: v for k, v in tags.items() if v is not None}


def _sentry(op: str, job: dict, **tags) -> None:
    """Best-effort terminal telemetry; never let observability break the worker."""
    exc = tags.pop("exc", None)
    terminal_status = tags.pop("status", "success")
    known = _job_tags(op.split(".")[0], job, stage=tags.pop("stage", "finalize"),
                      terminal_status=terminal_status, **tags)
    user = {"id": job.get("requested_by"), "role": job.get("requested_role")}
    if exc is not None:
        obs.capture_exception(exc, tags=known, user=user)
    else:
        obs.record_terminal(op, tags=known, user=user,
                            status="ok" if terminal_status == "success" else "internal_error")


# ============================ THUMBNAILS ============================

async def find_active_thumbnail_set(project_id: str, brief_asset_id: str) -> dict | None:
    """Return the queued/running set for this brief, if one exists.

    A double-clicked Generate button must not start two paid image calls for
    the same brief.  Terminal sets are ignored so an explicit retry is always
    possible.
    """
    db = get_db()
    set_doc = await db[THUMBNAIL_SETS].find_one(
        {"project_id": project_id, "brief_asset_id": brief_asset_id,
         "status": {"$in": ["queued", "running"]}},
        {"_id": 0},
        sort=[("created_at", -1)],
    )
    if not set_doc:
        return None
    options = await db[THUMBNAIL_JOBS].find(
        {"set_id": set_doc["id"], "project_id": project_id}, {"_id": 0}
    ).sort("option_index", 1).to_list(10)
    return {"set": set_doc, "options": options}


async def create_thumbnail_set(
    *,
    project_id: str,
    brief_asset_id: str,
    variants: int,
    requested_by: str,
) -> dict:
    db = get_db()
    now = _now()
    set_id = str(uuid.uuid4())
    variants = max(1, min(int(variants or 1), 3))
    jobs = []
    for index in range(variants):
        job = {
            "id": str(uuid.uuid4()),
            "set_id": set_id,
            "project_id": project_id,
            "brief_asset_id": brief_asset_id,
            "option_index": index,
            "attempt_number": 1,
            "status": "queued",
            "terminal_status": "pending",
            "output_saved": False,
            "asset_id": str(uuid.uuid4()),
            "asset": None,
            "provider": None,
            "model": None,
            "error_code": None,
            "error_message": None,
            "duration_ms": None,
            "requested_by": requested_by,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
        }
        jobs.append(job)
    set_doc = {
        "id": set_id,
        "project_id": project_id,
        "brief_asset_id": brief_asset_id,
        "variants": variants,
        "status": "queued",
        "succeeded_count": 0,
        "failed_count": 0,
        "requested_by": requested_by,
        "created_at": now,
        "updated_at": now,
    }
    await db[THUMBNAIL_SETS].insert_one(dict(set_doc))
    await db[THUMBNAIL_JOBS].insert_many([dict(j) for j in jobs])
    return {"set": set_doc, "options": jobs}


async def run_thumbnail_job(job_id: str) -> None:
    db = get_db()
    now = _now()
    job = await db[THUMBNAIL_JOBS].find_one_and_update(
        {"id": job_id, "status": "queued"},
        {"$set": {"status": "running", "started_at": now, "updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not job:
        return

    from . import thumbnail_images
    started = time.monotonic()
    try:
        project = await db.projects.find_one(
            {"id": job["project_id"]}, {"_id": 0}) or {}
        brief_asset = await db.assets.find_one(
            {"id": job["brief_asset_id"], "project_id": job["project_id"],
             "asset_type": "thumbnail_concept"},
            {"_id": 0},
        )
        if not brief_asset:
            raise thumbnail_images.ThumbnailGenerationError(
                "Thumbnail brief not found", code="thumbnail_brief_missing",
                option_index=job.get("option_index", 0),
                attempt_number=job.get("attempt_number", 1))
        brief = brief_asset.get("brief") or {}
        asset = await thumbnail_images.generate_thumbnail_image(
            project, brief,
            project_id=job["project_id"],
            option_index=int(job.get("option_index") or 0),
            attempt_number=int(job.get("attempt_number") or 1),
            asset_id=job.get("asset_id"),
        )
        asset["brief_asset_id"] = job["brief_asset_id"]
        asset["thumbnail_set_id"] = job["set_id"]
        asset["option_index"] = int(job.get("option_index") or 0)
        asset["attempt_number"] = int(job.get("attempt_number") or 1)
        await db.assets.insert_one(dict(asset))
        finished = _now()
        await db[THUMBNAIL_JOBS].update_one(
            {"id": job_id},
            {"$set": {
                "status": "succeeded",
                "terminal_status": "success",
                "output_saved": True,
                "asset_id": asset.get("id"),
                "asset": _public_asset(asset),
                "provider": asset.get("provider"),
                "model": asset.get("model"),
                "error_code": None,
                "error_message": None,
                "completed_at": finished,
                "updated_at": finished,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }},
        )
        await _refresh_thumbnail_set(job["set_id"])
        terminal = {**job, "provider": asset.get("provider") or job.get("provider"),
                    "model": asset.get("model") or job.get("model"),
                    "output_saved": True,
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        obs.record_terminal("thumbnail.option", tags=_job_tags(
            "thumbnail", terminal, stage="finalize",
            terminal_status="success", output_saved=True,
            thumbnail_set_id=job["set_id"],
            option_index=job.get("option_index"),
            attempt_number=job.get("attempt_number"),
            duration_ms=terminal["duration_ms"],
            frontend_outcome="connected", backend_outcome="success",
        ), user={"id": job.get("requested_by"), "role": "creator"})
    except thumbnail_images.ThumbnailGenerationError as exc:
        await _fail_thumbnail(job, exc.code, str(exc), exc.provider, exc.model,
                              int((time.monotonic() - started) * 1000), exc)
    except Exception as exc:  # noqa: BLE001
        code, message = _classify_exception(exc)
        await _fail_thumbnail(job, code, message, None, None,
                              int((time.monotonic() - started) * 1000), exc)


async def _fail_thumbnail(job: dict, code: str, message: str,
                          provider: str | None, model: str | None,
                          duration_ms: int, exc: BaseException | None) -> None:
    db = get_db()
    finished = _now()
    attempt_number = int(getattr(exc, "attempt_number", 0) or job.get("attempt_number") or 1)
    option_index = int(getattr(exc, "option_index", 0) or job.get("option_index") or 0)
    await db[THUMBNAIL_JOBS].update_one(
        {"id": job["id"]},
        {"$set": {
            "status": "failed",
            "terminal_status": "failed",
            "output_saved": False,
            "error_code": code,
            "error_message": _sanitize_message(message),
            "provider": provider,
            "model": model,
            "attempt_number": attempt_number,
            "retry_count": max(0, attempt_number - 1),
            "option_index": option_index,
            "backend_outcome": "failed",
            "frontend_outcome": "connected",
            "completed_at": finished,
            "updated_at": finished,
            "duration_ms": duration_ms,
        }},
    )
    await _refresh_thumbnail_set(job["set_id"])
    tags = _job_tags(
        "thumbnail", {**job, "provider": provider, "model": model,
                      "output_saved": False, "duration_ms": duration_ms},
        stage="provider_call", terminal_status="failed", error_code=code,
        output_saved=False, thumbnail_set_id=job["set_id"],
        option_index=option_index, attempt_number=attempt_number,
        retry_count=max(0, attempt_number - 1), duration_ms=duration_ms,
        frontend_outcome="connected", backend_outcome="failed",
    )
    user = {"id": job.get("requested_by"), "role": "creator"}
    if exc is not None:
        obs.capture_exception(exc, tags=tags, user=user)
    else:
        obs.capture_message(message, level="error", tags=tags, user=user,
                            fingerprint=["thumbnail", str(code)])


async def _refresh_thumbnail_set(set_id: str) -> None:
    db = get_db()
    jobs = await db[THUMBNAIL_JOBS].find(
        {"set_id": set_id}, {"_id": 0, "status": 1}).to_list(20)
    statuses = [str(j.get("status") or "").lower() for j in jobs]
    succeeded = statuses.count("succeeded")
    failed = statuses.count("failed")
    active = any(s in ("queued", "running") for s in statuses)
    if active:
        status = "running" if "running" in statuses else "queued"
    elif failed and succeeded:
        status = "partial"
    elif failed:
        status = "failed"
    else:
        status = "succeeded"
    await db[THUMBNAIL_SETS].update_one(
        {"id": set_id},
        {"$set": {
            "status": status,
            "succeeded_count": succeeded,
            "failed_count": failed,
            "updated_at": _now(),
        }},
    )


async def get_thumbnail_set(project_id: str, set_id: str) -> dict | None:
    db = get_db()
    set_doc = await db[THUMBNAIL_SETS].find_one(
        {"id": set_id, "project_id": project_id}, {"_id": 0})
    if not set_doc:
        return None
    options = await db[THUMBNAIL_JOBS].find(
        {"set_id": set_id, "project_id": project_id}, {"_id": 0}
    ).sort("option_index", 1).to_list(10)
    assets = await db.assets.find(
        {"project_id": project_id, "thumbnail_set_id": set_id,
         "asset_type": "generated_thumbnail"}, {"_id": 0}
    ).to_list(10)
    return {"set": set_doc, "options": options, "assets": assets}


async def latest_thumbnail_sets(project_id: str, limit: int = 5) -> list[dict]:
    db = get_db()
    sets = await db[THUMBNAIL_SETS].find(
        {"project_id": project_id}, {"_id": 0}
    ).sort("created_at", -1).to_list(max(1, min(int(limit or 5), 20)))
    out = []
    for set_doc in sets:
        options = await db[THUMBNAIL_JOBS].find(
            {"set_id": set_doc["id"]}, {"_id": 0}
        ).sort("option_index", 1).to_list(10)
        out.append({**set_doc, "options": options})
    return out


async def thumbnail_sets_by_brief(project_id: str, limit: int = 50) -> list[dict]:
    """Latest thumbnail set for every brief, with its independent options.

    The project view is fetched often, so this deliberately uses two queries
    (sets + ``$in`` options) instead of one query per brief.
    """
    db = get_db()
    sets = await db[THUMBNAIL_SETS].find(
        {"project_id": project_id}, {"_id": 0}
    ).sort("created_at", -1).to_list(500)
    latest: dict[str, dict] = {}
    for set_doc in sets:
        brief_id = set_doc.get("brief_asset_id")
        if brief_id and brief_id not in latest:
            latest[brief_id] = set_doc
    chosen = list(latest.values())[:max(1, min(int(limit or 50), 100))]
    if not chosen:
        return []
    set_ids = [s["id"] for s in chosen]
    options = await db[THUMBNAIL_JOBS].find(
        {"project_id": project_id, "set_id": {"$in": set_ids}}, {"_id": 0}
    ).sort([("set_id", 1), ("option_index", 1)]).to_list(1000)
    by_set: dict[str, list[dict]] = {}
    for option in options:
        by_set.setdefault(option.get("set_id"), []).append(option)
    return [{**set_doc, "options": by_set.get(set_doc["id"], [])}
            for set_doc in chosen]


# ============================ WORKERS ============================

async def _voice_loop() -> None:
    db = get_db()
    last_recovery = 0.0
    while not _stop.is_set():
        try:
            now = time.monotonic()
            if now - last_recovery >= 30.0:
                last_recovery = now
                await recover_orphaned_jobs()
            if len(_voice_tasks) < VOICE_CONCURRENCY:
                slots = VOICE_CONCURRENCY - len(_voice_tasks)
                jobs = await db[VOICEOVER_JOBS].find(
                    {"status": "queued"}, {"_id": 0, "id": 1}
                ).sort("created_at", 1).to_list(slots)
                for job in jobs:
                    task = asyncio.create_task(run_voiceover_job(job["id"]))
                    _voice_tasks.add(task)
                    task.add_done_callback(_voice_tasks.discard)
        except Exception as exc:  # noqa: BLE001
            logger.warning("voiceover worker poll failed: %s", exc)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _thumb_loop() -> None:
    db = get_db()
    while not _stop.is_set():
        try:
            if len(_thumb_tasks) < THUMB_CONCURRENCY:
                slots = THUMB_CONCURRENCY - len(_thumb_tasks)
                jobs = await db[THUMBNAIL_JOBS].find(
                    {"status": "queued"}, {"_id": 0, "id": 1}
                ).sort("created_at", 1).to_list(slots)
                for job in jobs:
                    task = asyncio.create_task(run_thumbnail_job(job["id"]))
                    _thumb_tasks.add(task)
                    task.add_done_callback(_thumb_tasks.discard)
        except Exception as exc:  # noqa: BLE001
            logger.warning("thumbnail worker poll failed: %s", exc)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def recover_orphaned_jobs() -> dict:
    """Reconcile jobs left running by a previous process without re-charging.

    * An asset already registered -> mark the job succeeded.
    * A durable attempt shows provider call started/partial/succeeded but no
      asset -> mark ``needs_reconciliation``. Never silently retry.
    * No provider call started -> requeue; that path cannot have spent money.
    """
    db = get_db()
    recovered = 0
    failed = 0
    requeued = 0
    try:
        running = await db[VOICEOVER_JOBS].find(
            {"status": "running", "$or": [
                {"lease_expires_at": {"$lt": _now()}},
                {"lease_expires_at": {"$exists": False}},
                {"lease_expires_at": None},
            ]}, {"_id": 0}).to_list(200)
        for job in running:
            asset = await db.assets.find_one(
                {"id": job.get("asset_id"), "project_id": job.get("project_id"),
                 "asset_type": "voiceover_audio"}, {"_id": 0})
            if asset:
                await db[VOICEOVER_JOBS].update_one(
                    {"id": job["id"]},
                    {"$set": {
                        "status": "succeeded", "terminal_status": "success",
                        "output_saved": True, "asset": _public_asset(asset),
                        "asset_id": asset.get("id"), "completed_at": _now(),
                        "updated_at": _now(),
                        "recovered_after_restart": True,
                        "lease_token": None, "lease_expires_at": None,
                    }})
                await release_voiceover_lock(
                    project_id=job["project_id"],
                    kind=job.get("kind") or ("scene" if job.get("scene_id") else "full_script"),
                    scene_id=job.get("scene_id"), job_id=job["id"])
                recovered += 1
                continue
            attempt = await db["voiceover_attempts"].find_one(
                {"job_id": job["id"]}, {"_id": 0}, sort=[("updated_at", -1)])
            outcome = (attempt or {}).get("provider_outcome")
            started = bool((attempt or {}).get("provider_call_started"))
            tracked = "provider_call_started" in job
            if (
                (not attempt and tracked and not job.get("provider_call_started"))
                or (attempt and outcome == "none" and not started)
            ):
                await db[VOICEOVER_JOBS].update_one(
                    {"id": job["id"]},
                    {"$set": {
                        "status": "queued", "terminal_status": "pending",
                        "started_at": None, "updated_at": _now(),
                        "lease_token": None, "lease_expires_at": None,
                        "recovered_after_restart": True,
                    }})
                requeued += 1
            else:
                await db[VOICEOVER_JOBS].update_one(
                    {"id": job["id"]},
                    {"$set": {
                        "status": "failed", "terminal_status": "failed",
                        "output_saved": bool((attempt or {}).get("audio_path")),
                        "error_code": "worker_restart_needs_reconciliation",
                        "error_message": (
                            "Worker restarted after a provider call may have started. "
                            "The recording is reconciled before any new charge."
                        ),
                        "error_detail": {
                            "code": "worker_restart_needs_reconciliation",
                            "attempt": attempt,
                            "needs_reconciliation": True,
                        },
                        "provider_call_started": started or bool(attempt),
                        "provider_outcome": outcome or "uncertain",
                        "needs_reconciliation": True,
                        "completed_at": _now(), "updated_at": _now(),
                        "lease_token": None, "lease_expires_at": None,
                    }})
                failed += 1
        running = await db[THUMBNAIL_JOBS].find(
            {"status": "running"}, {"_id": 0}).to_list(200)
        for job in running:
            asset = await db.assets.find_one(
                {"id": job.get("asset_id"), "project_id": job.get("project_id"),
                 "asset_type": "generated_thumbnail"}, {"_id": 0})
            if asset:
                await db[THUMBNAIL_JOBS].update_one(
                    {"id": job["id"]},
                    {"$set": {
                        "status": "succeeded", "terminal_status": "success",
                        "output_saved": True, "asset": _public_asset(asset),
                        "asset_id": asset.get("id"), "completed_at": _now(),
                        "updated_at": _now(), "recovered_after_restart": True,
                    }})
                await _refresh_thumbnail_set(job["set_id"])
                recovered += 1
            else:
                await db[THUMBNAIL_JOBS].update_one(
                    {"id": job["id"]},
                    {"$set": {
                        "status": "failed", "terminal_status": "failed",
                        "output_saved": False, "error_code": "worker_restarted",
                        "error_message": "Worker restarted before completion; retry is safe.",
                        "completed_at": _now(), "updated_at": _now(),
                    }})
                await _refresh_thumbnail_set(job["set_id"])
                failed += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("orphaned job recovery failed: %s", exc)
    return {"recovered": recovered, "failed": failed, "requeued": requeued}


async def start_workers() -> None:
    global _started, _stop
    if _started:
        return
    # Recreate the event per loop; module import happens before uvicorn starts.
    _stop = asyncio.Event()
    await ensure_job_indexes()
    outcome = await recover_orphaned_jobs()
    if outcome.get("recovered") or outcome.get("failed"):
        logger.info("job recovery: %s", outcome)
    _workers.append(asyncio.create_task(_voice_loop()))
    _workers.append(asyncio.create_task(_thumb_loop()))
    _started = True
    logger.info("background job workers started")


async def stop_workers() -> None:
    global _started
    _stop.set()
    tasks = list(_workers) + list(_voice_tasks) + list(_thumb_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _workers.clear()
    _voice_tasks.clear()
    _thumb_tasks.clear()
    _started = False
