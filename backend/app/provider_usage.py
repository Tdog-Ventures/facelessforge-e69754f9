"""Measured provider usage and explicit cost estimates.

This collection separates *what the provider reported* (tokens/characters)
from *what we estimate it cost* (only when rates are configured). It never
guesses a dollar figure. Failed calls are recorded with the same structure as
successful ones, so failed script corrections are auditable.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .db import get_db

logger = logging.getLogger("facelessforge.provider_usage")

COLLECTION = "provider_attempts"
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def project_id_from_cache_key(cache_key: str | None) -> Optional[str]:
    match = _UUID_RE.search(str(cache_key or ""))
    return match.group(0) if match else None


def operation_from_cache_key(cache_key: str | None) -> str:
    return (str(cache_key or "").split("-", 1)[0] or "llm").strip() or "llm"


def estimated_llm_cost_usd(
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    input_rate_env: str = "DEEPSEEK_INPUT_USD_PER_MILLION",
    output_rate_env: str = "DEEPSEEK_OUTPUT_USD_PER_MILLION",
) -> tuple[Optional[float], Optional[str]]:
    """Return (cost, basis). Both rates must be configured or cost is None."""
    try:
        input_rate = float(os.environ.get(input_rate_env, "") or 0)
        output_rate = float(os.environ.get(output_rate_env, "") or 0)
    except (TypeError, ValueError):
        return None, None
    if input_rate <= 0 or output_rate <= 0:
        return None, None
    prompt = int(prompt_tokens or 0)
    completion = int(completion_tokens or 0)
    cost = prompt / 1_000_000 * input_rate + completion / 1_000_000 * output_rate
    return round(cost, 8), f"USD from {input_rate_env}/{output_rate_env}"


async def start_attempt(
    *,
    operation: str,
    provider: str,
    model: str,
    cache_key: str | None,
    request_kind: str = "llm",
) -> str:
    attempt_id = str(uuid.uuid4())
    try:
        await get_db()[COLLECTION].insert_one({
            "id": attempt_id,
            "project_id": project_id_from_cache_key(cache_key),
            "operation": operation,
            "request_kind": request_kind,
            "provider": provider,
            "model": model,
            "cache_key": cache_key,
            "status": "started",
            "success": None,
            "started_at": _now(),
            "created_at": _now(),
            "updated_at": _now(),
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider attempt start failed: %s", exc)
    return attempt_id


async def finish_attempt(attempt_id: str, **fields: Any) -> None:
    try:
        await get_db()[COLLECTION].update_one(
            {"id": attempt_id},
            {"$set": {
                **fields,
                "updated_at": _now(),
                "completed_at": _now(),
            }},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider attempt finish failed: %s", exc)


async def record_llm_attempt(
    *,
    provider: str,
    model: str,
    cache_key: str | None,
    success: bool,
    attempt_id: str | None = None,
    http_status: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    duration_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    cached_tokens: int | None = None,
    prompt_characters: int | None = None,
    completion_characters: int | None = None,
    request_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    cost, basis = estimated_llm_cost_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    attempt_id = attempt_id or str(uuid.uuid4())
    doc = {
        "id": attempt_id,
        "project_id": project_id_from_cache_key(cache_key),
        "operation": operation_from_cache_key(cache_key),
        "request_kind": "llm",
        "provider": provider,
        "model": model,
        "cache_key": cache_key,
        "status": "succeeded" if success else "failed",
        "success": bool(success),
        "http_status": http_status,
        "error_code": error_code,
        "error_message": (str(error_message or "")[:500] or None),
        "duration_ms": duration_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "prompt_characters": prompt_characters,
        "completion_characters": completion_characters,
        "request_id": request_id,
        "estimated_cost_usd": cost,
        "cost_basis": basis,
        "cost_is_estimate": cost is not None,
        "metadata": metadata or {},
        "updated_at": _now(),
    }
    try:
        await get_db()[COLLECTION].update_one(
            {"id": attempt_id},
            {"$set": doc, "$setOnInsert": {"created_at": _now()}},
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider attempt record failed: %s", exc)


async def record_request_outcome(
    *,
    project_id: str,
    operation: str,
    provider: str,
    model: str,
    success: bool,
    error_code: str | None = None,
    error_message: str | None = None,
    metadata: dict | None = None,
) -> None:
    """One row per user-visible operation, separate from per-call usage rows."""
    try:
        await get_db()[COLLECTION].insert_one({
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "operation": operation,
            "request_kind": "outcome",
            "provider": provider,
            "model": model,
            "cache_key": None,
            "status": "succeeded" if success else "failed",
            "success": bool(success),
            "error_code": error_code,
            "error_message": (str(error_message or "")[:500] or None),
            "metadata": metadata or {},
            "created_at": _now(),
            "updated_at": _now(),
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider outcome record failed: %s", exc)


async def list_for_project(project_id: str, limit: int = 100) -> list[dict]:
    try:
        return await get_db()[COLLECTION].find(
            {"project_id": project_id}, {"_id": 0}
        ).sort("created_at", -1).to_list(max(1, min(int(limit or 100), 500)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider attempt list failed: %s", exc)
        return []
