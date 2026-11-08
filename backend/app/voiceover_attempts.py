"""Durable provenance for voiceover attempts.

An attempt row is written *before* a chargeable provider call and updated at
every state boundary. It is deliberately separate from the project asset row:
if the process dies between provider completion, file write and Mongo asset
registration, the file path and chunk state survive in this collection and can
be reconciled without paying again.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .db import get_db

logger = logging.getLogger("facelessforge.voiceover_attempts")

COLLECTION = "voiceover_attempts"

# Attempts in these states must never trigger another provider call until a
# human/worker has reconciled whether audio actually exists.
UNCERTAIN_STATES = {"provider_call_started", "provider_partial", "reconciling"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def attempt_id_for(project_id: str, asset_id: str, job_id: str | None = None) -> str:
    """Stable attempt id for one logical job/artifact request.

    The job id keeps failed attempts as separate provenance rows; the asset id
    remains the key for the durable chunk manifest so an explicit retry can
    reuse already-paid chunks.
    """
    base = f"voa_{project_id}_{asset_id}"
    return f"{base}_{job_id}" if job_id else base


async def start_attempt(
    *,
    project_id: str,
    asset_id: str,
    job_id: str | None = None,
    scene_id: str | None = None,
    provider: str,
    model: str,
    voice_id: str,
    voice_style: str,
    tone: str | None,
    text: str,
    script_id: str | None = None,
    script_hash: str | None = None,
    chunks_total: int | None = None,
    characters_total: int | None = None,
) -> dict[str, Any]:
    """Create or refresh the provenance row. Never raises."""
    doc = {
        "id": attempt_id_for(project_id, asset_id, job_id),
        "project_id": project_id,
        "asset_id": asset_id,
        "job_id": job_id,
        "scene_id": scene_id,
        "provider": provider,
        "model": model,
        "voice_id": voice_id,
        "voice_style": voice_style,
        "tone": tone,
        "script_id": script_id,
        "script_hash": script_hash,
        "text_hash": text_hash(text),
        "text_excerpt": (text or "")[:240],
        "word_count": len((text or "").split()),
        "characters": len(text or ""),
        "characters_requested": characters_total if characters_total is not None else len(text or ""),
        "chunks_total": chunks_total,
        "chunks_completed": 0,
        "status": "created",
        "provider_outcome": "none",
        "provider_call_started": False,
        "output_saved": False,
        "audio_path": None,
        "duration": None,
        "duration_measured": False,
        "measurement_method": None,
        "asset_registered": False,
        "error_code": None,
        "error_message": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    try:
        db = get_db()
        # Configuration fields may be refreshed, but a retry must never erase
        # prior provider evidence. ``provider_call_started``/``outcome`` and the
        # audio path are only written on insert; the synthesis path updates
        # them explicitly at each state boundary.
        config_fields = {
            "project_id": project_id,
            "asset_id": asset_id,
            "job_id": job_id,
            "scene_id": scene_id,
            "provider": provider,
            "model": model,
            "voice_id": voice_id,
            "voice_style": voice_style,
            "tone": tone,
            "script_id": script_id,
            "script_hash": script_hash,
            "text_hash": doc["text_hash"],
            "text_excerpt": doc["text_excerpt"],
            "word_count": doc["word_count"],
            "characters": doc["characters"],
            "characters_requested": doc["characters_requested"],
            "chunks_total": chunks_total,
            "updated_at": _now(),
        }
        insert_fields = {
            "status": "created",
            "provider_outcome": "none",
            "provider_call_started": False,
            "output_saved": False,
            "audio_path": None,
            "duration": None,
            "duration_measured": False,
            "measurement_method": None,
            "asset_registered": False,
            "error_code": None,
            "error_message": None,
            "chunks_completed": 0,
            "created_at": doc["created_at"],
        }
        await db[COLLECTION].update_one(
            {"id": doc["id"]},
            {"$set": config_fields, "$setOnInsert": insert_fields},
            upsert=True,
        )
        # Preserve the original creation time if the row already existed.
        existing = await db[COLLECTION].find_one({"id": doc["id"]}, {"_id": 0})
        return existing or doc
    except Exception as exc:  # noqa: BLE001
        logger.warning("voiceover attempt start failed: %s", exc)
        return doc


async def update_attempt(attempt_id: str, **fields: Any) -> None:
    """Best-effort provenance update. Never blocks the provider path."""
    try:
        await get_db()[COLLECTION].update_one(
            {"id": attempt_id},
            {"$set": {**fields, "updated_at": _now()}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("voiceover attempt update failed id=%s: %s", attempt_id, exc)


async def mark_provider_started(attempt_id: str, *, chunks_total: int | None = None) -> None:
    await update_attempt(
        attempt_id,
        status="provider_call_started",
        provider_outcome="pending",
        provider_call_started=True,
        provider_started_at=_now(),
        chunks_total=chunks_total,
    )


async def mark_chunk_completed(
    attempt_id: str, *, chunk_index: int, characters: int, path: str | None = None
) -> None:
    await update_attempt(
        attempt_id,
        status="provider_partial",
        provider_outcome="pending",
        last_chunk_index=chunk_index,
        last_chunk_characters=characters,
        last_chunk_path=path,
    )


async def mark_provider_succeeded(
    attempt_id: str,
    *,
    path: str,
    chunks_completed: int,
    characters_completed: int,
    size_bytes: int | None = None,
) -> None:
    await update_attempt(
        attempt_id,
        status="provider_succeeded",
        provider_outcome="succeeded",
        output_saved=True,
        audio_path=path,
        chunks_completed=chunks_completed,
        characters_completed=characters_completed,
        size_bytes=size_bytes,
        provider_completed_at=_now(),
    )


async def mark_provider_failed(
    attempt_id: str,
    *,
    error_code: str,
    message: str,
    chunks_completed: int = 0,
    characters_completed: int = 0,
    partial_path: str | None = None,
    uncertain: bool = False,
) -> None:
    await update_attempt(
        attempt_id,
        status="provider_partial" if chunks_completed and uncertain else "provider_failed",
        provider_outcome="uncertain" if uncertain else "failed",
        error_code=error_code,
        error_message=str(message or "")[:500],
        chunks_completed=chunks_completed,
        characters_completed=characters_completed,
        partial_path=partial_path,
        output_saved=bool(partial_path),
        provider_completed_at=_now(),
    )


async def mark_measured(
    attempt_id: str,
    *,
    duration: float | None,
    method: str | None,
) -> None:
    await update_attempt(
        attempt_id,
        duration=duration,
        duration_measured=bool(duration and method),
        measurement_method=method,
        status="measured" if duration else "measurement_failed",
    )


async def mark_asset_registered(attempt_id: str, *, asset_id: str) -> None:
    await update_attempt(
        attempt_id,
        asset_registered=True,
        registered_asset_id=asset_id,
        status="succeeded",
    )


async def get_attempt(attempt_id: str) -> dict | None:
    try:
        return await get_db()[COLLECTION].find_one({"id": attempt_id}, {"_id": 0})
    except Exception as exc:  # noqa: BLE001
        logger.warning("voiceover attempt lookup failed: %s", exc)
        return None


async def latest_for_job(job_id: str) -> dict | None:
    try:
        return await get_db()[COLLECTION].find_one(
            {"job_id": job_id}, {"_id": 0}, sort=[("updated_at", -1)]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("voiceover attempt job lookup failed: %s", exc)
        return None


async def list_for_project(project_id: str, limit: int = 50) -> list[dict]:
    try:
        return await get_db()[COLLECTION].find(
            {"project_id": project_id}, {"_id": 0}
        ).sort("updated_at", -1).to_list(max(1, min(int(limit or 50), 200)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("voiceover attempt list failed: %s", exc)
        return []


async def record_cost(
    *,
    project_id: str,
    attempt: dict,
    characters_used: int,
    operation: str = "voiceover",
    success: bool = True,
) -> None:
    """Record what was sent to the provider. Never guesses a dollar price.

    ElevenLabs bills by characters/credits; the exact plan rate is account
    specific. ``characters_used`` is the auditable figure. ``estimated_cost``
    is left at zero rather than inventing a rate.
    """
    try:
        await get_db().cost_logs.insert_one({
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "provider": f"{attempt.get('provider') or 'tts'}/{attempt.get('model') or ''}".rstrip("/"),
            "operation": operation,
            "tokens_used": 0,
            "characters_used": int(max(0, characters_used)),
            "estimated_cost": 0.0,
            "success": bool(success),
            "attempt_id": attempt.get("id"),
            "job_id": attempt.get("job_id"),
            "asset_id": attempt.get("asset_id"),
            "chunks_completed": int(attempt.get("chunks_completed") or 0),
            "chunks_total": attempt.get("chunks_total"),
            "cost_note": "provider character usage recorded; account-specific rate not estimated",
            "created_at": _now(),
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("voiceover cost record failed: %s", exc)
