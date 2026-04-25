"""External render API — thin wrapper over the existing render pipeline.

Exposes:
  • POST /api/external/render-video
  • GET  /api/external/render-video-status?job_id=...

Auth: header `X-FacelessForge-Key: <key>` matching env `EXTERNAL_RENDER_API_KEY`.
Toggle: env `EXTERNAL_RENDER_ENABLED=true|false`.

Hard rules (matches user spec):
  • Never touch the render pipeline. We import and call existing services.
  • Never expose internal file paths in responses.
  • Return immediately after queuing — render runs async.
"""
from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query
from pydantic import BaseModel, Field

from . import generation as gen
from . import render as render_service
from . import thumbnail_images as thumb_images
from . import tts as tts_service
from .db import get_db

logger = logging.getLogger("facelessforge.external")

router = APIRouter(prefix="/external", tags=["external"])


# ============================ MODELS ============================

class ExternalSceneInput(BaseModel):
    scene_number: Optional[int] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    duration: Optional[float] = Field(default=None, ge=1, le=600)
    narration_text: Optional[str] = Field(default=None, max_length=2000)
    visual_direction: Optional[str] = Field(default=None, max_length=600)
    caption_text: Optional[str] = Field(default=None, max_length=400)
    search_terms: Optional[list[str]] = None


class ExternalRenderRequest(BaseModel):
    source: str = Field(default="external", max_length=80)
    external_asset_id: Optional[str] = Field(default=None, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    script: str = Field(min_length=10, max_length=20000)
    scene_breakdown: list[ExternalSceneInput] = Field(default_factory=list)
    stock_footage_terms: Optional[list[str]] = None
    captions: Optional[dict[str, Any]] = None
    voiceover_notes: Optional[str] = Field(default=None, max_length=600)
    # Optional metadata so callers can override defaults if they want
    niche: Optional[str] = Field(default=None, max_length=120)
    audience: Optional[str] = Field(default=None, max_length=200)
    tone: Optional[str] = Field(default=None, max_length=80)
    target_duration: Optional[int] = Field(default=None, ge=15, le=3600)


class ExternalRenderResponse(BaseModel):
    job_id: str
    project_id: str
    status: str
    status_url: str


class ExternalStatusResponse(BaseModel):
    job_id: str
    project_id: str
    status: str
    progress: int
    current_step: Optional[str]
    video_url: Optional[str]
    duration: Optional[float]
    width: int = 1920
    height: int = 1080
    error: Optional[str]


class ExternalCancelRequest(BaseModel):
    job_id: str = Field(min_length=8, max_length=64)


class ExternalCancelResponse(BaseModel):
    job_id: str
    project_id: str
    status: str  # "cancelled" | "completed" | "failed"
    already_terminal: bool = False


# ============================ AUTH ============================

def _enabled() -> bool:
    return os.environ.get("EXTERNAL_RENDER_ENABLED", "false").lower() in ("1", "true", "yes")


def _require_external_key(x_facelessforge_key: Optional[str]) -> None:
    if not _enabled():
        # Hide the surface entirely if disabled
        raise HTTPException(status_code=404, detail="Not Found")
    expected = os.environ.get("EXTERNAL_RENDER_API_KEY") or ""
    if not expected:
        # Misconfigured — refuse rather than allow anonymous access
        raise HTTPException(status_code=503, detail="External API key not configured")
    if not x_facelessforge_key or not secrets.compare_digest(x_facelessforge_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-FacelessForge-Key")


# ============================ HELPERS ============================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _voice_style_from_notes(notes: Optional[str]) -> str:
    notes = (notes or "").lower()
    table = [
        ("dramatic", ("dramatic", "theatrical", "epic", "bold")),
        ("energetic", ("upbeat", "energetic", "fast", "pumped", "hype")),
        ("documentary", ("documentary", "neutral", "explainer")),
        ("calm", ("calm", "soft", "warm", "slow")),
        ("corporate", ("corporate", "professional", "formal")),
        ("mysterious", ("mysterious", "intimate", "breathy", "whisper")),
    ]
    for key, words in table:
        if any(w in notes for w in words):
            return key
    return os.environ.get("DEFAULT_VOICE_STYLE", "narrator")


async def _system_creator(db) -> dict:
    """Idempotently fetches/creates the system user that owns external projects."""
    email = os.environ.get("EXTERNAL_SYSTEM_USER_EMAIL", "external-renderer@facelessforge.io")
    user = await db.users.find_one({"email": email}, {"_id": 0, "password_hash": 0})
    if user:
        return user
    from .auth import hash_password
    new_user = {
        "id": str(uuid.uuid4()),
        "email": email,
        "name": "External Renderer (system)",
        "role": "creator",
        "password_hash": hash_password(secrets.token_urlsafe(48)),
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.users.insert_one(dict(new_user))
    new_user.pop("password_hash", None)
    return new_user


def _normalise_scenes(payload_scenes: list[ExternalSceneInput],
                      script_text: str,
                      target_duration: int,
                      project_id: str,
                      stock_terms_default: list[str]) -> list[dict]:
    """Map ETHINX scene payload into FacelessForge's internal scene shape.
    Falls back to a single scene per ~30s of script if no breakdown given."""
    out: list[dict] = []
    if payload_scenes:
        cursor = 0.0
        for i, s in enumerate(payload_scenes):
            num = int(s.scene_number) if s.scene_number else (i + 1)
            if s.start_time is not None and s.end_time is not None and s.end_time > s.start_time:
                start = float(s.start_time)
                end = float(s.end_time)
            else:
                dur = float(s.duration or 6.0)
                start = cursor
                end = cursor + dur
            cursor = end
            narration = (s.narration_text or "").strip()
            visual = (s.visual_direction or "").strip()
            search_terms = s.search_terms or stock_terms_default[:4] or [visual or narration[:60]]
            out.append({
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "scene_number": num,
                "start_time": round(start, 2),
                "end_time": round(end, 2),
                "narration_text": narration,
                "visual_direction": visual or "Stock b-roll matching the narration",
                "asset_type": "stock_video",
                "search_terms": [t for t in search_terms if t][:6],
                "image_prompt": visual or narration[:120],
                "caption_text": (s.caption_text or narration[:80] or "").strip(),
                "status": "PLANNED",
                "created_at": _now(),
                "updated_at": _now(),
            })
        return out

    # Fallback — chunk script into ~6s per ~15-word block
    sentences = [s.strip() for s in script_text.replace("\n", " ").split(". ") if s.strip()]
    if not sentences:
        sentences = [script_text.strip()]
    cursor = 0.0
    base_terms = stock_terms_default[:4] or ["cinematic b-roll"]
    for i, sent in enumerate(sentences):
        words = max(8, len(sent.split()))
        dur = max(3.0, min(12.0, words / 2.5))
        start = cursor
        end = cursor + dur
        cursor = end
        out.append({
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "scene_number": i + 1,
            "start_time": round(start, 2),
            "end_time": round(end, 2),
            "narration_text": sent if sent.endswith(".") else sent + ".",
            "visual_direction": "Stock b-roll matching the narration",
            "asset_type": "stock_video",
            "search_terms": base_terms,
            "image_prompt": sent[:120],
            "caption_text": sent[:80],
            "status": "PLANNED",
            "created_at": _now(),
            "updated_at": _now(),
        })
        if cursor >= float(target_duration):
            break
    return out


# ============================ ENDPOINTS ============================

@router.post("/render-video", response_model=ExternalRenderResponse)
async def external_render_video(
    body: ExternalRenderRequest = Body(...),
    x_facelessforge_key: Optional[str] = Header(default=None, alias="X-FacelessForge-Key"),
):
    _require_external_key(x_facelessforge_key)
    db = get_db()
    user = await _system_creator(db)

    # Estimate target duration from scenes or script length
    if body.target_duration:
        target_duration = int(body.target_duration)
    elif body.scene_breakdown:
        last_end = max(
            (float(s.end_time or ((s.start_time or 0) + (s.duration or 6))) for s in body.scene_breakdown),
            default=180.0,
        )
        target_duration = int(max(30, min(3600, round(last_end))))
    else:
        words = max(50, len(body.script.split()))
        target_duration = int(max(30, min(3600, round(words / 2.5))))

    project_id = str(uuid.uuid4())
    voice_style_label = body.voiceover_notes or "neutral male narrator"
    project_doc = {
        "id": project_id,
        "user_id": user["id"],
        "name": body.title,
        "niche": body.niche or "external",
        "topic": (body.script[:280] or body.title),
        "audience": body.audience or "general",
        "tone": body.tone or "informative",
        "target_duration": target_duration,
        "voice_style": voice_style_label,
        "visual_style": "cinematic b-roll",
        "monetisation_intent": "external",
        "cta_goal": "subscribe",
        "status": "DRAFT",
        "quality_score": 0,
        "estimated_cost": 0.0,
        "external_source": body.source,
        "external_asset_id": body.external_asset_id,
        "external_captions": body.captions,
        "selected_thumbnail_asset_id": None,
        "selected_voiceover_asset_id": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.projects.insert_one(dict(project_doc))

    # Seed script
    script_doc = {
        "id": str(uuid.uuid4()),
        "project_id": project_id,
        "hook_option_one": body.script.split(".")[0][:160] if "." in body.script else body.script[:160],
        "hook_option_two": "",
        "hook_option_three": "",
        "selected_hook": body.script.split(".")[0][:160] if "." in body.script else body.script[:160],
        "full_script": body.script,
        "retention_beats": [],
        "cta_block": "",
        "word_count": len(body.script.split()),
        "estimated_duration": target_duration,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.scripts.insert_one(dict(script_doc))

    # Seed scenes
    scenes = _normalise_scenes(
        body.scene_breakdown, body.script, target_duration, project_id,
        list(body.stock_footage_terms or []),
    )
    if scenes:
        await db.scenes.insert_many([dict(s) for s in scenes])

    # Generate metadata via existing service (deterministic fallback works without LLM)
    try:
        meta = await gen.generate_metadata(project_doc, body.script, scenes)
    except Exception as e:  # noqa: BLE001
        logger.warning("metadata generation failed: %s", e)
        meta = {
            "title_options": [body.title],
            "selected_title": body.title,
            "description": body.script[:400],
            "tags": [body.niche or "video"],
            "youtube_chapters": [],
            "social_caption": body.title,
        }
    meta_doc = {"id": str(uuid.uuid4()), "project_id": project_id,
                **meta, "created_at": _now(), "updated_at": _now()}
    await db.metadata_packages.insert_one(dict(meta_doc))

    # Generate thumbnail brief + image, auto-select first
    try:
        briefs = await gen.generate_thumbnails(project_doc)
    except Exception:  # noqa: BLE001
        briefs = []
    brief_assets: list[dict] = []
    for c in (briefs or [])[:1]:
        ba = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "scene_id": None,
            "asset_type": "thumbnail_concept",
            "source": "ai_generated",
            "name": c.get("thumbnail_title_text", "Thumbnail concept"),
            **c,
            "status": "generated",
            "created_at": _now(),
            "updated_at": _now(),
        }
        brief_assets.append(ba)
        await db.assets.insert_one(dict(ba))

    if brief_assets:
        try:
            imgs = await thumb_images.generate_thumbnail_images(
                project_doc, brief_assets[0], variants=1, project_id=project_id,
            )
            for img in imgs:
                img["created_at"] = _now()
                img["updated_at"] = _now()
                img["brief_asset_id"] = brief_assets[0]["id"]
                img["brief_snapshot"] = {k: brief_assets[0].get(k) for k in (
                    "thumbnail_title_text", "visual_composition", "emotion_angle",
                    "background_idea", "subject_focal_point", "colour_direction",
                    "click_trigger",
                )}
                img["status"] = "selected"
                await db.assets.insert_one(dict(img))
                await db.projects.update_one(
                    {"id": project_id},
                    {"$set": {"selected_thumbnail_asset_id": img["id"], "updated_at": _now()}},
                )
                break
        except Exception as e:  # noqa: BLE001
            logger.warning("thumbnail image generation failed: %s", e)

    # Generate voiceover (full script), auto-select
    try:
        voice_style = _voice_style_from_notes(body.voiceover_notes)
        vo_id = str(uuid.uuid4())
        vo_payload = await tts_service.generate_voiceover(
            text=body.script, voice_style=voice_style, project_id=project_id,
            asset_id=vo_id, scene_id=None, name_suffix="(External)",
        )
        vo_payload["text_excerpt"] = body.script[:240]
        vo_payload["created_at"] = _now()
        vo_payload["updated_at"] = _now()
        vo_payload["status"] = "selected"
        await db.assets.insert_one(dict(vo_payload))
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"selected_voiceover_asset_id": vo_id, "updated_at": _now()}},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("voiceover generation failed: %s", e)

    # Refresh project after auto-selects
    project_doc = await db.projects.find_one({"id": project_id}, {"_id": 0})
    project_doc["status"] = "READY_TO_RENDER"
    await db.projects.update_one({"id": project_id}, {"$set": {"status": "READY_TO_RENDER"}})

    # Validate prereqs (server-side preflight against the same checklist the UI uses)
    assets_now = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(500)
    scenes_now = await db.scenes.find({"project_id": project_id}, {"_id": 0}).sort("scene_number", 1).to_list(500)
    script_now = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    meta_now = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    project_now = await db.projects.find_one({"id": project_id}, {"_id": 0})
    check = render_service.validate_prerequisites(project_now, script_now, scenes_now, meta_now, assets_now)
    if not check["ok"]:
        raise HTTPException(status_code=422, detail={
            "message": "Render prerequisites not met after seeding",
            "issues": check["issues"],
        })

    # Queue render via the same service the project UI uses — pipeline untouched
    try:
        job = await render_service.queue_render(project_id, requested_by=user["id"])
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return ExternalRenderResponse(
        job_id=job["id"],
        project_id=project_id,
        status="queued",
        status_url=f"/api/external/render-video-status?job_id={job['id']}",
    )


_PUBLIC_STATUS_MAP = {
    "queued": "queued",
    "validating": "running",
    "preparing_assets": "running",
    "rendering": "running",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "expired_artifact": "expired",
}


@router.get("/render-video-status", response_model=ExternalStatusResponse)
async def external_render_status(
    job_id: str = Query(..., min_length=8, max_length=64),
    x_facelessforge_key: Optional[str] = Header(default=None, alias="X-FacelessForge-Key"),
):
    _require_external_key(x_facelessforge_key)
    db = get_db()
    job = await db.render_jobs.find_one({"id": job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    status = _PUBLIC_STATUS_MAP.get(job.get("status", ""), job.get("status", "unknown"))
    completed = status == "completed"
    return ExternalStatusResponse(
        job_id=job["id"],
        project_id=job["project_id"],
        status=status,
        progress=int(job.get("progress") or 0),
        current_step=job.get("current_step"),
        video_url=(job.get("output_url") if completed else None),
        duration=job.get("duration"),
        width=1920,
        height=1080,
        error=job.get("error_message"),
    )


@router.post("/render-video/cancel", response_model=ExternalCancelResponse)
async def external_render_cancel(
    body: ExternalCancelRequest = Body(...),
    x_facelessforge_key: Optional[str] = Header(default=None, alias="X-FacelessForge-Key"),
):
    """Cancel an active render job. Idempotent — already-terminal jobs return
    their current state with `already_terminal=true` (no error)."""
    _require_external_key(x_facelessforge_key)
    db = get_db()
    job = await db.render_jobs.find_one({"id": body.job_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    internal_status = job.get("status", "")
    public = _PUBLIC_STATUS_MAP.get(internal_status, internal_status)

    # Idempotent fast-path for already-terminal states
    if internal_status in ("cancelled", "completed", "failed", "expired_artifact"):
        return ExternalCancelResponse(
            job_id=job["id"],
            project_id=job["project_id"],
            status=public,
            already_terminal=True,
        )

    ok = await render_service.cancel_render(job["project_id"], job["id"])
    # Re-read job to surface the post-cancel state authoritatively
    job = await db.render_jobs.find_one({"id": body.job_id}, {"_id": 0}) or job
    if not ok:
        # Race: job may have flipped to terminal between fetch and cancel.
        public = _PUBLIC_STATUS_MAP.get(job.get("status", ""), job.get("status", ""))
        if public in ("completed", "failed", "cancelled", "expired"):
            return ExternalCancelResponse(
                job_id=job["id"], project_id=job["project_id"],
                status=public, already_terminal=True,
            )
        raise HTTPException(status_code=409, detail="Job is not cancellable in its current state.")
    return ExternalCancelResponse(
        job_id=job["id"],
        project_id=job["project_id"],
        status=_PUBLIC_STATUS_MAP.get(job.get("status", ""), "cancelled"),
        already_terminal=False,
    )

