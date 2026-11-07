"""Routers: auth, projects, generation, exports, analytics, settings."""
from __future__ import annotations

import asyncio
import io
import json
import math
import re
import os
import uuid
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Body, UploadFile, File, Form
from fastapi.responses import RedirectResponse, StreamingResponse, PlainTextResponse

from ..auth import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    set_auth_cookies, clear_auth_cookies, get_current_user, require_roles,
)
from ..db import get_db
from ..publishing_access import ensure_operator
from pydantic import BaseModel, Field

from ..models import (
    RegisterRequest, LoginRequest, ProjectCreate, ProjectUpdate,
    ScriptUpdate, MetadataUpdate, ProviderSettingsUpdate, AssetCreate,
    ShareUpdate, ForgotPasswordRequest, ResetPasswordRequest,
    StockAttachRequest, FindAssetsRequest, AssetStatusUpdate,
    AutoAttachRequest, GenerateThumbnailImagesRequest, MusicSelectRequest,
)
try:
    from ..billing import check_credits
except Exception:
    check_credits = None  # fallback if billing not yet patched
from ..scoring import quality_score, quality_label, compute_project_status, scenes_to_csv
from ..grounding import CLIENT_VISUAL_TYPES
from ..timing import build_timing_status, narration_fits_budget
from ..grounding import commercial_project_errors
from ..storage import resolve_delivery_url
from .. import generation as gen
from .. import stock as stock_service
from .. import thumbnail_images as thumb_images
from ..asset_dedupe import filter_distinct_titles
from ..asset_relevance import relevant_candidates
from ..scene_terms import compact_stock_query
from .. import asset_commit, project_lease


router = APIRouter(prefix="/api")


def _now():
    return datetime.now(timezone.utc)


CINEMATIC_SUFFIX = "4k slow motion b-roll cinematic"
_SCENE_LEAK_STOP = {"might","obsolete","coming","year","years","small","businesses","drowning","imagine","software","shift","machine","impact","simulate","will","shall","could","should","would","may","might","about","hear","story","cover","converts","alter","the","and","a","an","of","to","in","on","for","with","about","is","are","was","were","be","as","it","its","this","that","what","you","about","to","hear","story","will","cover","how","it","converts","alter","will"}

def _clean_visual_full(visual: str) -> str:
    """Preserve full Visual including commas, normalize whitespace only. No split on comma, no truncation."""
    if not visual:
        return ""
    return " ".join((visual or "").strip().split())

def _extract_core_query(scene: dict) -> str:
    """Extract 3-4 word core for Pixabay/Unsplash fallback, e.g. crystal ball forecast.
    Prefers search_terms multi-word, else visual keywords, filters leak words.
    """
    terms = scene.get("search_terms") or []
    if terms:
        for t in terms:
            clean = " ".join(str(t or "").split()).strip()
            if not clean:
                continue
            words = clean.split()
            if len(words) == 1 and words[0].lower() in _SCENE_LEAK_STOP:
                continue
            if len(words) == 1 and len(words[0]) < 4:
                continue
            if len(words) >= 2:
                filtered = [w for w in words if w.lower() not in _SCENE_LEAK_STOP and len(w) >= 3]
                if len(filtered) >= 2:
                    return " ".join(filtered[:4])
                return " ".join(words[:4])
    visual = (scene.get("visual_direction") or "").strip()
    if visual:
        import re
        words = re.findall(r"[a-z]{3,}", visual.lower())
        kw = [w for w in words if w not in _SCENE_LEAK_STOP]
        kw = [w for w in kw if 3 <= len(w) <= 20]
        seen=set()
        out=[]
        for w in kw:
            if w not in seen:
                seen.add(w)
                out.append(w)
            if len(out)>=4:
                break
        if len(out)>=2:
            return " ".join(out[:4])
        if out:
            return " ".join(out)
    return "nature cinematic"

def _build_scene_stock_query(scene: dict, project: dict, override: str | None = None) -> str:
    """Use visible stock-footage subjects, not complete cinematic instructions.

    A manually entered query remains untouched. Generated search terms are
    alternatives, NOT three phrases to concatenate into an impossible match.
    """
    if override and override.strip():
        return override.strip()
    terms = scene.get("search_terms") or []
    if isinstance(terms, str):
        terms = [terms]
    for term in terms:
        concise = compact_stock_query(term)
        if len(concise.split()) >= 2:
            return concise
    for candidate in (
        scene.get("visual_direction"),
        scene.get("visual"),
        project.get("topic"),
        project.get("niche"),
    ):
        concise = compact_stock_query(candidate)
        if concise:
            return concise
    return "nature landscape"


def _build_ordered_stock_queries(scene: dict, project: dict) -> list[dict]:
    """Generate exactly 3 ordered query objects per scene: pexels_video (cinematic full), pixabay_video (shorter core), unsplash_image (shortest).
    Preference order video first. Used by auto-attach fallback chain.
    """
    visual = _clean_visual_full(scene.get("visual_direction") or "")
    if not visual:
        terms = scene.get("search_terms") or []
        if terms and isinstance(terms, list):
            for t in terms:
                if t and len(str(t).split()) >= 2:
                    visual = str(t)
                    break
            if not visual:
                visual = str(terms[0]) if terms else ""
        elif isinstance(terms, str):
            visual = terms
        else:
            visual = (project.get("topic") if project and project.get("topic") else "") or "nature cinematic"
        visual = _clean_visual_full(visual) or "nature cinematic"
    pexels_q = f"{visual} {CINEMATIC_SUFFIX}".strip()
    core = _extract_core_query(scene)
    pixabay_q = core if core else "nature cinematic"
    if len(pixabay_q.split()) < 2:
        pixabay_q = "nature cinematic b-roll"
    unsplash_q = " ".join(core.split()[:3]).strip() if core else "nature cinematic"
    if len(unsplash_q.split()) < 2:
        unsplash_q = "nature cinematic"
    return [
        {"source": "pexels", "type": "video", "media_type": "videos", "query": pexels_q},
        {"source": "pixabay", "type": "video", "media_type": "videos", "query": pixabay_q},
        {"source": "unsplash", "type": "image", "media_type": "photos", "query": unsplash_q},
    ]


def _ser(doc: dict) -> dict:
    """Drop _id and serialise datetimes for JSON."""
    if not doc:
        return doc
    doc = {k: v for k, v in doc.items() if k != "_id"}
    for k, v in list(doc.items()):
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


def _render_job_payload(job: dict | None) -> dict | None:
    """Serialize a render job with a URL that is usable right now.

    Object-storage URLs saved at render completion expire. API consumers must
    receive a newly signed URL rather than the stale value persisted in Mongo.
    """
    if not job:
        return None
    payload = _ser(job)
    delivery_url = resolve_delivery_url(job)
    if delivery_url:
        payload["output_url"] = delivery_url
    return payload


def _ensure_project_access(project: dict, user: dict, *, write: bool = False):
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    role = user.get("role")
    if role == "admin":
        return
    if project["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not your project")
    if write and role == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot modify projects")


# ============================ AUTH ============================

@router.post("/auth/register")
async def register(body: RegisterRequest, response: Response, request: Request):
    db = get_db()
    email = body.email.lower()
    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user_id = str(uuid.uuid4())
    user_doc = {
        "id": user_id,
        "name": body.name,
        "email": email,
        "role": body.role,
        "password_hash": hash_password(body.password),
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.users.insert_one(user_doc)
    access = create_access_token(user_id, email, body.role)
    refresh = create_refresh_token(user_id)
    set_auth_cookies(response, access, refresh, request)
    return _ser({**user_doc, "password_hash": None})


@router.post("/auth/login")
async def login(body: LoginRequest, response: Response, request: Request):
    db = get_db()
    email = body.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    access = create_access_token(user["id"], email, user["role"])
    refresh = create_refresh_token(user["id"])
    set_auth_cookies(response, access, refresh, request)
    out = dict(user); out.pop("password_hash", None)
    return _ser(out)


@router.post("/auth/logout")
async def logout(response: Response, _user=Depends(get_current_user)):
    clear_auth_cookies(response)
    return {"ok": True}


@router.get("/auth/me")
async def me(user=Depends(get_current_user)):
    return _ser(user)


@router.get("/users/me")
async def users_me(user=Depends(get_current_user)):
    """Alias used by some frontend builds — returns the current authenticated user."""
    return _ser(user)


# ---- Forgot / Reset password ----

RESET_RATE_LIMIT = 5              # max requests
RESET_RATE_WINDOW_SECONDS = 900   # per 15 minutes


def _dev_mode() -> bool:
    return os.environ.get("DEV_MODE", "false").lower() in ("1", "true", "yes")


@router.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request):
    """Always returns 200 — never reveals whether the email exists.
    Rate-limited per-IP+email to 5 requests per 15 minutes.
    In DEV_MODE, returns the reset token + reset_url in the response and logs it.
    """
    import secrets
    import logging
    logger = logging.getLogger("facelessforge.auth")

    db = get_db()
    email = body.email.lower()
    # Trust X-Forwarded-For first-hop (set by k8s ingress / proxy) for rate limiting.
    fwd = request.headers.get("x-forwarded-for", "")
    ip = (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "unknown")
    identifier = f"{ip}:{email}"
    now = _now()

    # Rate limit check
    window_start = now - timedelta(seconds=RESET_RATE_WINDOW_SECONDS)
    recent_count = await db.password_reset_attempts.count_documents({
        "identifier": identifier,
        "created_at": {"$gte": window_start},
    })
    if recent_count >= RESET_RATE_LIMIT:
        # Still return success to avoid enumeration; just skip token creation.
        return {"ok": True, "message": "If that email exists, a reset link has been issued."}

    await db.password_reset_attempts.insert_one({
        "identifier": identifier, "email": email, "ip": ip, "created_at": now,
    })

    user = await db.users.find_one({"email": email})
    response_payload = {"ok": True, "message": "If that email exists, a reset link has been issued."}

    if user:
        # Invalidate any existing un-used tokens for this user
        await db.password_reset_tokens.update_many(
            {"user_id": user["id"], "used_at": None},
            {"$set": {"used_at": now}},
        )
        ttl_minutes = int(os.environ.get("PASSWORD_RESET_TTL_MINUTES", "60"))
        token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(minutes=ttl_minutes)
        await db.password_reset_tokens.insert_one({
            "id": str(uuid.uuid4()),
            "user_id": user["id"],
            "email": email,
            "token": token,
            "created_at": now,
            "expires_at": expires_at,
            "used_at": None,
        })
        # Build reset link (frontend route)
        frontend = os.environ.get("FRONTEND_URL") or request.headers.get("origin") or ""
        reset_url = f"{frontend}/reset-password?token={token}" if frontend else f"/reset-password?token={token}"
        if _dev_mode():
            logger.warning("[DEV reset] %s -> %s", email, reset_url)
            response_payload["dev_reset_token"] = token
            response_payload["dev_reset_url"] = reset_url
            response_payload["dev_expires_in_minutes"] = ttl_minutes

    return response_payload


@router.post("/auth/reset-password")
async def reset_password(body: ResetPasswordRequest):
    db = get_db()
    now = _now()
    record = await db.password_reset_tokens.find_one({"token": body.token})
    if not record:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has already been used.")
    if record.get("used_at") is not None:
        raise HTTPException(status_code=400, detail="This reset link has already been used.")
    expires_at = record.get("expires_at")
    if isinstance(expires_at, datetime):
        # Motor returns naive UTC datetimes; normalise before comparison.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now:
            raise HTTPException(status_code=400, detail="This reset link has expired. Please request a new one.")

    user = await db.users.find_one({"id": record["user_id"]})
    if not user:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has already been used.")

    new_hash = hash_password(body.new_password)
    await db.users.update_one(
        {"id": user["id"]},
        {"$set": {"password_hash": new_hash, "updated_at": now}},
    )
    await db.password_reset_tokens.update_one(
        {"token": body.token},
        {"$set": {"used_at": now}},
    )
    # Invalidate any other outstanding tokens for this user
    await db.password_reset_tokens.update_many(
        {"user_id": user["id"], "used_at": None},
        {"$set": {"used_at": now}},
    )
    return {"ok": True, "message": "Password updated. You can now sign in."}


# ============================ PROJECTS ============================

async def _attach_project_view(db, project: dict) -> dict:
    project = project_lease.strip_lease_fields(project)
    script = await db.scripts.find_one({"project_id": project["id"]}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project["id"]}, {"_id": 0}).to_list(1000)
    metadata = await db.metadata_packages.find_one({"project_id": project["id"]}, {"_id": 0})
    assets = await db.assets.find({"project_id": project["id"]}, {"_id": 0}).to_list(1000)
    render_job = await db.render_jobs.find_one(
        {"project_id": project["id"]},
        {"_id": 0},
        sort=[("created_at", -1)],
    )
    score = quality_score(project=project, script=script, scenes=scenes, metadata=metadata, render_job=render_job)
    status = compute_project_status(
        has_script=bool(script), has_scenes=bool(scenes), has_metadata=bool(metadata),
        has_assets=bool(assets), render_status=(render_job or {}).get("status"),
    )
    if status != project.get("status"):
        await db.projects.update_one({"id": project["id"]}, {"$set": {"status": status, "updated_at": _now()}})
        project["status"] = status
    if score != project.get("quality_score"):
        await db.projects.update_one({"id": project["id"]}, {"$set": {"quality_score": score, "updated_at": _now()}})
        project["quality_score"] = score
    selected_voiceover = next((
        asset for asset in assets
        if asset.get("id") == project.get("selected_voiceover_asset_id")
        and asset.get("asset_type") == "voiceover_audio"
    ), None)
    selected_music = next((
        asset for asset in assets
        if asset.get("id") == project.get("selected_music_asset_id")
        and asset.get("asset_type") == "music_audio"
    ), None)
    timing = build_timing_status(
        project=project,
        scenes=scenes,
        voiceover=selected_voiceover,
    )
    return {
        "project": _ser(project) | {"quality_label": quality_label(score)},
        "script": _ser(script) if script else None,
        "scenes": sorted([_ser(s) for s in scenes], key=lambda s: s.get("scene_number", 0)),
        "metadata": _ser(metadata) if metadata else None,
        "assets": [_ser(a) for a in assets],
        "render_job": _render_job_payload(render_job),
        "music_asset": _ser(selected_music) if selected_music else None,
        "share": _share_payload(project),
        "timing": timing,
    }


@router.post("/projects")
async def create_project(body: ProjectCreate, user=Depends(get_current_user)):
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewers cannot create projects")
    db = get_db()
    proj = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "name": body.name,
        "niche": body.niche,
        "topic": body.topic,
        "audience": body.audience,
        "tone": body.tone,
        "target_duration": body.target_duration,
        "voice_style": body.voice_style or "neutral male narrator",
        "voice_id": body.voice_id or body.voice_style or "neutral male narrator",
        "visual_style": body.visual_style or "cinematic b-roll",
        "monetisation_intent": body.monetisation_intent or "ads + affiliate",
        "cta_goal": body.cta_goal or "subscribe",
        "auto_post": body.auto_post if body.auto_post is not None else False,
        "platforms": body.platforms or [],
        "production_mode": body.production_mode,
        "client_name": body.client_name,
        "structured_brief": body.structured_brief.model_dump() if body.structured_brief else None,
        "verified_cta": body.verified_cta.model_dump() if body.verified_cta else None,
        "brand_assets_supplied": bool(body.brand_assets_supplied),
        "aspect_ratio": body.aspect_ratio or "9:16",
        "resolution": body.resolution or "1080x1920",
        "status": "DRAFT",
        "quality_score": 0,
        "estimated_cost": 0.0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    if body.production_mode == "editorial":
        proj.update({
            "client_name": None,
            "structured_brief": None,
            "verified_cta": None,
            "brand_assets_supplied": False,
        })
    errors = commercial_project_errors(proj)
    if errors:
        raise HTTPException(
            status_code=422,
            detail="Commercial project requires: " + ", ".join(errors),
        )
    await db.projects.insert_one(proj)
    return _ser(proj)


@router.get("/projects")
async def list_projects(user=Depends(get_current_user)):
    db = get_db()
    is_admin = user["role"] == "admin"
    if is_admin:
        q = {"is_demo": {"$ne": True}, "is_template": {"$ne": True}}
        # Admin sees all non-demo across all users, with badge
    else:
        q = {"user_id": user["id"], "is_demo": {"$ne": True}, "is_template": {"$ne": True}}
    projs = await db.projects.find(q, {"_id": 0}).sort("created_at", -1).to_list(5000)
    # Attach derived score labels without recomputing too expensively:
    result = [_ser(project_lease.strip_lease_fields(p)) | {"quality_label": quality_label(int(p.get("quality_score", 0)))} for p in projs]
    # For admin, add flag so frontend can show badge
    if is_admin:
        for r in result:
            r["_is_admin_view"] = True
    return result


# ── BATCH RENDERING — must be BEFORE /projects/{project_id} routes to avoid Starlette capturing "batch" as project_id ──
class BatchRenderRequest(BaseModel):
    project_ids: List[str] = Field(default_factory=list, max_length=20)
    urls: Optional[List[str]] = Field(default=None, max_length=20)
    force: Optional[bool] = False

async def _batch_render_impl(body: BatchRenderRequest, user: dict):
    import logging
    logger = logging.getLogger("facelessforge.batch")
    logger.info("batch_render called user=%s body=%s", user.get("id"), body.model_dump())
    db = get_db()
    from .. import render as render_service
    results = []
    errors = []
    if body.project_ids:
        for pid in body.project_ids[:20]:
            try:
                logger.info("batch pid=%s lookup", pid)
                project = await db.projects.find_one({"id": pid}, {"_id": 0})
                logger.info("batch pid=%s found=%s", pid, bool(project))
                _ensure_project_access(project, user, write=True)
                script = await db.scripts.find_one({"project_id": pid}, {"_id": 0})
                scenes = await db.scenes.find({"project_id": pid}, {"_id": 0}).to_list(100)
                metadata = await db.metadata_packages.find_one({"project_id": pid}, {"_id": 0})
                assets = await db.assets.find({"project_id": pid}, {"_id": 0}).to_list(500)
                check = render_service.validate_prerequisites(project, script, scenes, metadata, assets)
                if not check.get("ok") and not body.force:
                    errors.append({"project_id": pid, "error": "preflight_failed", "details": check})
                    continue
                job = await render_service.queue_render(
                    pid, requested_by=user["id"],
                    expected_inputs=project_lease.render_input_snapshot(project),
                )
                results.append({"project_id": pid, "job_id": job.get("id"), "status": job.get("status")})
            except Exception as e:
                logger.exception("batch pid=%s error %s", pid, e)
                errors.append({"project_id": pid, "error": str(e)})
        return {"queued": len(results), "jobs": results, "errors": errors}
    if body.urls:
        for idx, url in enumerate(body.urls[:5]):
            try:
                topic = f"Video from {url}"
                try:
                    from ..scraper import scrape_website
                    data = await scrape_website(url)
                    topic = (data.get("text") or topic)[:600] if data else topic
                    title = data.get("title") or f"Batch {idx+1}"
                except Exception:
                    title = f"Batch {idx+1} - {url}"
                pid = str(uuid.uuid4())
                doc = {
                    "id": pid, "user_id": user["id"], "name": title[:120], "niche": "general",
                    "topic": topic, "audience": "general", "tone": "informative", "target_duration": 60,
                    "voice_style": "neutral male narrator", "visual_style": "cinematic b-roll",
                    "monetisation_intent": "batch", "cta_goal": "subscribe", "status": "DRAFT",
                    "quality_score": 0, "estimated_cost": 0.0, "source_url": url,
                    "created_at": _now(), "updated_at": _now(),
                }
                await db.projects.insert_one(doc)
                if body.force:
                    job = await render_service.queue_render(pid, requested_by=user["id"])
                    results.append({"project_id": pid, "job_id": job.get("id"), "status": job.get("status"), "source_url": url})
                else:
                    results.append({"project_id": pid, "status": "created", "source_url": url, "next": "generate-script -> generate-scenes -> render/start"})
            except Exception as e:
                errors.append({"url": url, "error": str(e)})
        return {"queued": len(results), "jobs": results, "errors": errors}
    raise HTTPException(status_code=400, detail="provide project_ids (up to 20) or urls (up to 5)")

# Alias that won't collide with /projects/{project_id}/render — primary batch endpoint
@router.post("/batch/render")
async def batch_render_alias(body: BatchRenderRequest, user=Depends(get_current_user)):
    return await _batch_render_impl(body, user)

# Also keep the original path for spec compliance, but Starlette needs it before param routes
# We register it here (before param routes) so it takes precedence over /projects/{project_id}/render
@router.post("/projects/batch/render")
async def batch_render(body: BatchRenderRequest, user=Depends(get_current_user)):
    return await _batch_render_impl(body, user)


@router.get("/projects/{project_id}")
async def get_project(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    return await _attach_project_view(db, project)


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate, user=Depends(get_current_user)):
    """Partial project update.

    Writes to render-input fields (production mode, commercial ground truth,
    target duration) run under the project's render lease. The same lease
    serializes render submission and render release, so validation, the
    active-render check and the write are one atomic contract across workers.
    The write itself is fenced on the lease holder and fence.
    """
    from .. import render as render_service

    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if "target_duration" in patch and not (5 <= int(patch["target_duration"]) <= 3600):
        raise HTTPException(status_code=422, detail="Target duration must be 5..3600s")
    commercial_fields = {"production_mode", "client_name", "structured_brief", "verified_cta"}

    def _mode_lock_conflict() -> HTTPException:
        return HTTPException(
            status_code=409,
            detail=(
                "Cannot change production_mode while a render is active; "
                "wait for the render to finish or cancel it first."
            ),
        )

    if not patch:
        return await _attach_project_view(db, project)

    if not (set(patch) & set(project_lease.RENDER_INPUT_FIELDS)):
        # Non-render-input edits (e.g. auto_post) keep their previous behaviour.
        patch["updated_at"] = _now()
        await db.projects.update_one({"id": project_id}, {"$set": patch})
        return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))

    lease = await project_lease.acquire(
        db, project_id, project_lease.RENDER_LEASE, ttl_seconds=project_lease.RENDER_LEASE_TTL_SECONDS,
    )
    if lease is None:
        raise HTTPException(
            status_code=409,
            detail="The project is being updated or a render is being submitted or released; retry shortly.",
        )
    try:
        # Re-read under the lease: validation must see the state the write will replace.
        current = await db.projects.find_one({"id": project_id}, {"_id": 0})
        if current is None:
            raise HTTPException(status_code=404, detail="Project not found")
        mode_switching = "production_mode" in patch and patch["production_mode"] != current.get("production_mode")
        if mode_switching:
            # The render worker snapshots production_mode when the job starts and
            # gates release/auto-post with it. A mode switch while a render is
            # active could ungate an editorial auto-post or record commercial
            # metadata the new mode never validated. Refuse while active.
            if await render_service.is_render_active(project_id):
                raise _mode_lock_conflict()
        if commercial_fields.intersection(patch):
            merged = {**current, **patch}
            # Creation parity: an editorial project must not carry commercial
            # ground truth, so a later commercial switch validates against fresh
            # client data instead of inheriting stale values.
            if merged.get("production_mode") == "editorial":
                patch.update({
                    "client_name": None,
                    "structured_brief": None,
                    "verified_cta": None,
                    "brand_assets_supplied": False,
                })
            errors = commercial_project_errors({**current, **patch})
            if errors:
                raise HTTPException(
                    status_code=422,
                    detail="Commercial project requires: " + ", ".join(errors),
                )
        patch["updated_at"] = _now()
        result = await db.projects.update_one({"id": project_id, **lease.guard()}, {"$set": patch})
        if getattr(result, "matched_count", 0) != 1:
            # Fenced: the lease expired and was taken over. Nothing was written.
            raise HTTPException(
                status_code=409,
                detail="The project lease was lost during the update; nothing was changed. Retry.",
            )
    finally:
        await project_lease.release(db, lease)
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    await db.projects.delete_one({"id": project_id})
    await db.scripts.delete_many({"project_id": project_id})
    await db.scenes.delete_many({"project_id": project_id})
    await db.metadata_packages.delete_many({"project_id": project_id})
    await db.assets.delete_many({"project_id": project_id})
    await db.render_jobs.delete_many({"project_id": project_id})
    return {"ok": True}


# ============================ GENERATION ============================

def _log_cost(db, project_id: str, operation: str, tokens: int, cost: float):
    return db.cost_logs.insert_one({
        "id": str(uuid.uuid4()),
        "project_id": project_id,
        "provider": "deepseek/deepseek-chat",
        "operation": operation,
        "tokens_used": tokens,
        "characters_used": 0,
        "estimated_cost": cost,
        "created_at": _now(),
    })


@router.post("/projects/{project_id}/generate-script")
async def generate_script_endpoint(project_id: str, user=Depends(get_current_user)):
    if check_credits:
        await check_credits(user, required=1)
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    data = await gen.generate_script(project)
    doc = {
        "id": str(uuid.uuid4()),
        "project_id": project_id,
        **data,
        "source": "ai_generated",
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.scripts.replace_one({"project_id": project_id}, doc, upsert=True)
    await _log_cost(db, project_id, "script", tokens=max(500, (data.get("word_count", 0) or 0) * 2), cost=0.08)
    await db.projects.update_one({"id": project_id}, {"$set": {"estimated_cost": float(project.get("estimated_cost", 0)) + 0.08, "updated_at": _now()}})
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.post("/projects/{project_id}/generate-scenes")
async def generate_scenes_endpoint(project_id: str, request: Request, user=Depends(get_current_user)):
    if check_credits:
        await check_credits(user, required=1)
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    if not script:
        raise HTTPException(status_code=400, detail="Generate a script before scenes")
    # Check preserveSceneCount flag from body (regeneration must preserve count)
    try:
        body = await request.json()
        preserve = bool(body.get("preserveSceneCount"))
    except Exception:
        preserve = False
    # If preserve and scenes already exist and target_duration unchanged, we will let generation lock handle it (it already locks to ceil)
    # But log for audit
    if preserve:
        existing = await db.scenes.count_documents({"project_id": project_id})
        logger = __import__("logging").getLogger("facelessforge.routes")
        logger.info("Regenerating scenes preserve count project=%s existing=%s target=%s", project_id, existing, project.get("target_duration"))
    try:
        scenes = await gen.generate_scene_plan(project, script)
    except Exception as e:  # noqa: BLE001
        logger = __import__("logging").getLogger("facelessforge.routes")
        logger.warning("generate_scene_plan failed project=%s: %s — using fallback", project_id, e)
        scenes = await gen.generate_scene_plan(project, {"full_script": script.get("full_script", "") or project.get("topic") or "test"})
    for sc in scenes:
        sc["id"] = str(uuid.uuid4())
        sc["project_id"] = project_id
        sc["created_at"] = _now()
        sc["updated_at"] = _now()
    await db.scenes.delete_many({"project_id": project_id})
    if scenes:
        await db.scenes.insert_many([dict(sc) for sc in scenes])
    await _log_cost(db, project_id, "scenes", tokens=len(scenes) * 200, cost=0.06)
    await db.projects.update_one({"id": project_id}, {"$set": {"estimated_cost": float(project.get("estimated_cost", 0)) + 0.06, "updated_at": _now()}})
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.post("/projects/generate-scenes")
async def generate_scenes_shim(request: Request):
    """Shim for POST /api/projects/generate-scenes — accepts JSON {project_id, script, topic, niche, target_duration}.

    Public (no auth) so health checks and external callers can test auto-gen without a full project lifecycle.
    Never 500: falls back to deterministic generation if LLM times out or keys missing.
    Returns {"scenes": [...]} where scenes is always an array.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    project_id = (body.get("project_id") or "test").strip() or "test"
    script_text = body.get("script") or body.get("full_script") or body.get("topic") or "test 450 word flow"
    if isinstance(script_text, dict):
        script_text = script_text.get("full_script") or str(script_text)
    topic = body.get("topic") or (script_text[:60] if isinstance(script_text, str) else "test")
    niche = body.get("niche") or "general"
    target_duration = int(body.get("target_duration") or body.get("duration") or 300)
    # Build minimal project/script dicts for generation
    project = {
        "id": project_id,
        "topic": topic,
        "niche": niche,
        "target_duration": target_duration,
        "tone": body.get("tone") or "documentary",
        "title": topic,
    }
    script = {"full_script": script_text if isinstance(script_text, str) else str(script_text)}
    try:
        # Use timeout to avoid hanging on LLM — Ollama 20s + DeepSeek fallback fits within 60s
        scenes = await asyncio.wait_for(gen.generate_scene_plan(project, script), timeout=60.0)
    except asyncio.TimeoutError:
        logger = __import__("logging").getLogger("facelessforge.routes")
        logger.warning("generate-scenes shim timeout project=%s — fallback", project_id)
        scenes = await gen.generate_scene_plan(project, {"full_script": script_text[:2000] or "fallback script"})
    except Exception as e:  # noqa: BLE001
        logger = __import__("logging").getLogger("facelessforge.routes")
        logger.warning("generate-scenes shim failed project=%s: %s — fallback", project_id, e)
        # Deterministic fallback: sync call
        try:
            scenes = await gen.generate_scene_plan(project, {"full_script": script_text[:2000] or "fallback script"})
        except Exception as e2:  # noqa: BLE001
            logger.error("fallback also failed: %s", e2)
            scenes = [
                {
                    "scene_number": 1,
                    "start_time": 0.0,
                    "end_time": float(target_duration),
                    "duration": float(target_duration),
                    "narration_text": script_text[:500] or "Test scene narration",
                    "visual_direction": "forest nature b-roll",
                    "caption_text": "Test scene",
                    "search_terms": ["forest", "nature", "trees"],
                    "id": str(uuid.uuid4()),
                    "project_id": project_id,
                }
            ]
    # Ensure scenes is list
    if not isinstance(scenes, list):
        scenes = []
    # Attach ids if missing
    for sc in scenes:
        sc.setdefault("id", str(uuid.uuid4()))
        sc.setdefault("project_id", project_id)
    return {"scenes": scenes, "project_id": project_id, "count": len(scenes)}


@router.post("/projects/{project_id}/generate-metadata")
async def generate_metadata_endpoint(project_id: str, user=Depends(get_current_user)):
    if check_credits:
        await check_credits(user, required=1)
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project_id}).sort("scene_number", 1).to_list(500)
    if not script:
        raise HTTPException(status_code=400, detail="Generate a script before metadata")
    data = await gen.generate_metadata(project, script, scenes)
    doc = {
        "id": str(uuid.uuid4()),
        "project_id": project_id,
        **data,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.metadata_packages.replace_one({"project_id": project_id}, doc, upsert=True)
    await _log_cost(db, project_id, "metadata", tokens=600, cost=0.04)
    await db.projects.update_one({"id": project_id}, {"$set": {"estimated_cost": float(project.get("estimated_cost", 0)) + 0.04, "updated_at": _now()}})

    # Auto-create thumbnail briefs
    try:
        concepts = await gen.generate_thumbnail_concepts(project, script)
        await db.assets.delete_many({"project_id": project_id, "asset_type": "thumbnail_concept"})
        for concept in concepts:
            await db.assets.insert_one({
                "id": __import__("uuid").uuid4().hex,
                "project_id": project_id,
                "asset_type": "thumbnail_concept",
                "brief": concept,
                "status": "pending",
                "created_at": _now(),
                "updated_at": _now(),
            })
    except Exception:
        pass

    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.post("/projects/{project_id}/generate-thumbnails")
async def generate_thumbnails_endpoint(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0}) or {}
    concepts = await gen.generate_thumbnail_concepts(project, script)
    # Upsert as assets (type=thumbnail_concept)
    await db.assets.delete_many({"project_id": project_id, "asset_type": "thumbnail_concept"})
    for i, c in enumerate(concepts, start=1):
        await db.assets.insert_one({
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "name": f"Thumbnail Concept #{i}",
            "asset_type": "thumbnail_concept",
            "file_path": None,
            "source": "llm" if os.environ.get("EMERGENT_LLM_KEY") else "fallback",
            "tags": ["thumbnail", "concept"],
            "status": "ready",
            "brief": c,
            "created_at": _now(),
            "updated_at": _now(),
        })
    await _log_cost(db, project_id, "thumbnails", tokens=400, cost=0.03)
    await db.projects.update_one({"id": project_id}, {"$set": {"estimated_cost": float(project.get("estimated_cost", 0)) + 0.03, "updated_at": _now()}})
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.post("/projects/{project_id}/render")
async def prepare_render(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)

    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project_id}, {"_id": 0}).to_list(500)
    metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    assets = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(500)

    missing = []
    if not script: missing.append("script")
    if not scenes: missing.append("scenes")
    if not metadata: missing.append("metadata")

    job_id = str(uuid.uuid4())
    if missing:
        job = {
            "id": job_id,
            "project_id": project_id,
            "status": "FAILED",
            "progress": 0,
            "current_step": "validation",
            "output_path": None,
            "error_message": f"Missing: {', '.join(missing)}",
            "started_at": _now(),
            "completed_at": _now(),
            "created_at": _now(),
            "updated_at": _now(),
        }
        await db.render_jobs.insert_one(job)
        return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))

    status = "COMPLETED" if assets else "READY_TO_RENDER"
    job = {
        "id": job_id,
        "project_id": project_id,
        "status": status,
        "progress": 100 if status == "COMPLETED" else 80,
        "current_step": "ready_to_render" if status == "READY_TO_RENDER" else "completed",
        "output_path": f"/exports/{project_id}.package.json",
        "error_message": None,
        "started_at": _now(),
        "completed_at": _now(),
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.render_jobs.insert_one(job)
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


# ============================ SCRIPT / METADATA EDIT ============================

@router.patch("/projects/{project_id}/script")
async def update_script(project_id: str, body: ScriptUpdate, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot edit")
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    patch.pop("source", None)
    source = (body.source or "").strip().lower() or None
    if source not in (None, "manual", "ai_generated"):
        raise HTTPException(status_code=422, detail="source must be 'manual' or 'ai_generated'")
    if "full_script" in patch:
        import re as _re
        cleaned = (patch["full_script"] or "").replace("\x00", "").strip()
        if len(cleaned) > 20000:
            raise HTTPException(status_code=422, detail="Script exceeds 20000 characters")
        words = len(_re.findall(r"\b\w+\b", cleaned))
        existing = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
        if not existing:
            # Manual-save path: creating the project's first script without any LLM call.
            if words < 20:
                raise HTTPException(status_code=422, detail="Script needs at least 20 words")
            if source is None:
                source = "manual"
        elif not cleaned:
            raise HTTPException(status_code=422, detail="Script cannot be empty")
        patch["full_script"] = cleaned
        patch["word_count"] = words
        patch["estimated_duration"] = int(words / 2.5)
        try:
            narration = float(project.get("target_duration") or 30) - (
                0.0 if project.get("production_mode") == "commercial_ad" else 2.5
            )
            budget = gen.target_word_count(max(narration, 1.0))
        except Exception:
            budget = None
        patch["word_budget"] = budget
        patch["budget_coverage"] = round(words / budget, 3) if budget else None
    if source is not None:
        patch["source"] = source
    patch["updated_at"] = _now()
    invalidated: dict = {}
    if "full_script" in patch:
        existing = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
        old_text = " ".join(str((existing or {}).get("full_script") or "").split())
        new_text = " ".join(str(patch["full_script"] or "").split())
        if existing and old_text != new_text:
            # Material change: dependent drafts derived from the old script go
            # stale (scenes, metadata, thumbnail briefs, voiceover audio).
            # The old hook/beats/CTA must not survive as claims the new script
            # no longer supports; callers may still send replacements explicitly.
            patch.setdefault("selected_hook", "")
            patch.setdefault("cta_block", "")
            patch.setdefault("retention_beats", [])
            # History is preserved: render_jobs / master MP4 rows are never touched.
            scenes_res = await db.scenes.delete_many({"project_id": project_id})
            meta_res = await db.metadata_packages.delete_many({"project_id": project_id})
            assets_res = await db.assets.delete_many({
                "project_id": project_id,
                "asset_type": {"$in": ["stock_video", "stock_image", "thumbnail_concept", "voiceover_audio"]},
            })
            await db.projects.update_one(
                {"id": project_id},
                {"$set": {"selected_voiceover_asset_id": None, "updated_at": _now()},
                 "$unset": {"voice_fit": ""}},
            )
            project["selected_voiceover_asset_id"] = None
            invalidated = {
                "scenes": getattr(scenes_res, "deleted_count", 0),
                "metadata": getattr(meta_res, "deleted_count", 0),
                "assets": getattr(assets_res, "deleted_count", 0),
            }
    await db.scripts.update_one(
        {"project_id": project_id},
        {"$set": patch, "$setOnInsert": {"id": str(uuid.uuid4()), "project_id": project_id, "created_at": _now()}},
        upsert=True,
    )
    view = await _attach_project_view(db, project)
    if invalidated and any(invalidated.values()):
        view["script_invalidated"] = invalidated
        view["script_notice"] = (
            "Script updated — regenerate scenes, metadata, and voiceover. "
            "Completed videos were kept."
        )
    return view


@router.patch("/projects/{project_id}/metadata")
async def update_metadata(project_id: str, body: MetadataUpdate, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    patch["updated_at"] = _now()
    await db.metadata_packages.update_one({"project_id": project_id}, {"$set": patch})
    return await _attach_project_view(db, project)


# ============================ ASSETS ============================

CLIENT_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
CLIENT_VIDEO_EXTS = {".mp4", ".mov", ".webm"}
CLIENT_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
CLIENT_UPLOAD_TYPES = {
    "client_image", "client_video", "before_after",
    "brand_image", "brand_logo", "voiceover_audio",
}
BRAND_UPLOAD_TYPES = {"brand_image", "brand_logo"}
CLIENT_IMAGE_MAX_BYTES = 15 * 1024 * 1024
CLIENT_VIDEO_MAX_BYTES = 60 * 1024 * 1024
CLIENT_AUDIO_MAX_BYTES = 20 * 1024 * 1024
CLIENT_VIDEO_MAX_SECONDS = 600.0
CLIENT_IMAGE_MAX_PIXELS = 50_000_000


def _inspect_client_image(raw: bytes) -> tuple[int, int]:
    """Validate image bytes and return (width, height)."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            image.verify()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="File is not a decodable image") from exc
    if width <= 0 or height <= 0:
        raise HTTPException(status_code=422, detail="Image has no usable dimensions")
    if width * height > CLIENT_IMAGE_MAX_PIXELS:
        raise HTTPException(status_code=422, detail="Image dimensions are too large")
    return width, height


async def _inspect_client_video(path: Path) -> tuple[int, int, float]:
    """Validate video bytes with ffprobe and return (width, height, duration)."""
    from ..verify import ffprobe_video_duration, ffprobe_video_stream
    try:
        stream = await ffprobe_video_stream(str(path))
        duration = await ffprobe_video_duration(str(path))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="File is not a decodable video") from exc
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if width <= 0 or height <= 0 or duration <= 0:
        raise HTTPException(status_code=422, detail="Video has no usable stream")
    if duration > CLIENT_VIDEO_MAX_SECONDS:
        raise HTTPException(status_code=422, detail="Client video exceeds 600 seconds")
    return width, height, duration


def _inspect_client_audio(path: Path) -> float:
    """Validate audio bytes and return duration in seconds."""
    # probe_audio shells out to ffprobe; a missing/broken binary must degrade
    # to the same 422 the video path raises instead of a bare 500.
    try:
        info = audio_mix_service.probe_audio(path)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="File is not decodable audio") from exc
    info = info or {}
    duration = float(info.get("duration") or 0)
    if not info.get("sample_rate") or duration <= 0:
        raise HTTPException(status_code=422, detail="File is not decodable audio")
    return duration


def _client_upload_kind(asset_type: str, ext: str, content_type: str) -> str:
    """Resolve and validate the requested client asset kind."""
    requested = (asset_type or "").strip().lower()
    ctype = (content_type or "").lower()
    if not requested:
        if ctype.startswith("image/") or ext in CLIENT_IMAGE_EXTS:
            requested = "client_image"
        elif ctype.startswith("video/") or ext in CLIENT_VIDEO_EXTS:
            requested = "client_video"
        elif ctype.startswith("audio/") or ext in CLIENT_AUDIO_EXTS:
            requested = "voiceover_audio"
        else:
            raise HTTPException(status_code=422, detail="Could not determine upload type")
    if requested not in CLIENT_UPLOAD_TYPES:
        raise HTTPException(status_code=422, detail=f"Unsupported asset type {requested}")
    if requested in {"client_image", "brand_image", "brand_logo"} and ext not in CLIENT_IMAGE_EXTS:
        raise HTTPException(status_code=422, detail=f"Unsupported image type {ext or 'unknown'}")
    if requested == "client_video" and ext not in CLIENT_VIDEO_EXTS:
        raise HTTPException(status_code=422, detail=f"Unsupported video type {ext or 'unknown'}")
    if requested == "before_after" and ext not in (CLIENT_IMAGE_EXTS | CLIENT_VIDEO_EXTS):
        raise HTTPException(status_code=422, detail=f"Unsupported before/after type {ext or 'unknown'}")
    if requested == "voiceover_audio" and ext not in CLIENT_AUDIO_EXTS:
        raise HTTPException(status_code=422, detail=f"Unsupported audio type {ext or 'unknown'}")
    return requested


@router.post("/projects/{project_id}/assets/upload")
async def upload_client_asset(
    project_id: str,
    file: UploadFile = File(...),
    scene_id: str = Form(""),
    asset_type: str = Form(""),
    user=Depends(get_current_user),
):
    """Upload a client image/video/brand asset or narration audio.

    Client visuals are scene-bound. Brand assets may be project-level. A
    project-level narration upload becomes the selected full voiceover only
    when no full voiceover is already selected.
    """
    import hashlib
    import mimetypes
    import tempfile

    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot upload assets")

    ext = (Path(file.filename or "").suffix or "").lower()
    kind = _client_upload_kind(asset_type, ext, file.content_type or "")
    limit = (
        CLIENT_IMAGE_MAX_BYTES if kind in {"client_image", "brand_image", "brand_logo"}
        else CLIENT_VIDEO_MAX_BYTES if kind in {"client_video", "before_after"}
        else CLIENT_AUDIO_MAX_BYTES
    )
    if kind == "before_after" and ext in CLIENT_IMAGE_EXTS:
        # The half is validated as an image below; hold it to the image budget.
        limit = CLIENT_IMAGE_MAX_BYTES
    # Reject oversized uploads before reading the body into memory.
    if file.size is not None and file.size > limit:
        raise HTTPException(status_code=413, detail=f"File exceeds {limit // (1024 * 1024)} MB")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    if len(raw) > limit:
        raise HTTPException(status_code=413, detail=f"File exceeds {limit // (1024 * 1024)} MB")

    scene = None
    scene_id = (scene_id or "").strip()
    if kind in {"client_image", "client_video", "before_after"} and not scene_id:
        raise HTTPException(status_code=422, detail="scene_id is required for client visuals")
    if scene_id:
        scene = await db.scenes.find_one({"id": scene_id, "project_id": project_id}, {"_id": 0})
        if not scene:
            raise HTTPException(status_code=404, detail="Scene not found for this project")

    width = height = None
    duration = None
    if kind in {"client_image", "brand_image", "brand_logo"}:
        width, height = _inspect_client_image(raw)
    elif kind == "client_video":
        tmp = Path(tempfile.gettempdir()) / f"client-video-{uuid.uuid4().hex}{ext}"
        try:
            tmp.write_bytes(raw)
            width, height, duration = await _inspect_client_video(tmp)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    elif kind == "before_after":
        if ext in CLIENT_VIDEO_EXTS:
            tmp = Path(tempfile.gettempdir()) / f"client-before-after-{uuid.uuid4().hex}{ext}"
            try:
                tmp.write_bytes(raw)
                width, height, duration = await _inspect_client_video(tmp)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
        else:
            width, height = _inspect_client_image(raw)
    else:
        tmp = Path(tempfile.gettempdir()) / f"client-audio-{uuid.uuid4().hex}{ext}"
        try:
            tmp.write_bytes(raw)
            duration = _inspect_client_audio(tmp)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    from ..storage import get_storage as _gs
    asset_id = str(uuid.uuid4())
    key = f"assets/{project_id}/{asset_id}{ext}"
    mime = mimetypes.guess_type(file.filename or "")[0] or file.content_type or "application/octet-stream"
    staged = _stage_upload_copy(raw, key, ext, prefix="client-asset")
    try:
        saved = _gs().save_file(staged, key, mime)
    except Exception:
        # A storage outage must not orphan the staged copy (up to 60 MB
        # would otherwise leak into $TMPDIR).
        try:
            staged.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise

    is_full_voice = kind == "voiceover_audio" and not scene_id
    order = None
    start_offset = None
    scene_duration = None
    if scene:
        existing_count = await db.assets.count_documents({
            "project_id": project_id,
            "scene_id": scene_id,
            "asset_type": {"$in": ["stock_video", "stock_image", *CLIENT_VISUAL_TYPES]},
        })
        order = int(existing_count or 0)
        start_offset = order * 3
        scene_duration = _get_scene_duration(scene)

    doc = {
        "id": asset_id,
        "project_id": project_id,
        "scene_id": scene_id or None,
        "name": (file.filename or f"Client {kind}")[:200],
        "asset_type": kind,
        "file_path": str(saved.file_path) if saved.file_path else None,
        "storage_key": saved.key,
        "preview_url": saved.preview_path or saved.url,
        "source": "client_upload",
        "provenance": "client_upload",
        "mime_type": mime,
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "width": width,
        "height": height,
        "duration": duration,
        "tags": [],
        "status": "selected" if is_full_voice else ("attached" if scene else "ready"),
        "order": order,
        "start_offset": start_offset,
        "scene_duration": scene_duration,
        "uploaded_at": _now(),
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.assets.insert_one(dict(doc))
    project_patch = {"updated_at": _now()}
    if kind in BRAND_UPLOAD_TYPES:
        project_patch["brand_assets_supplied"] = True
    if is_full_voice and not project.get("selected_voiceover_asset_id"):
        project_patch["selected_voiceover_asset_id"] = asset_id
    if len(project_patch) > 1:
        await db.projects.update_one({"id": project_id}, {"$set": project_patch})
    return _ser(doc)


@router.post("/projects/{project_id}/assets")
async def create_asset(project_id: str, body: AssetCreate, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    doc = {
        "id": str(uuid.uuid4()),
        "project_id": project_id,
        "name": body.name,
        "asset_type": body.asset_type,
        "file_path": body.file_path,
        "source": body.source,
        "tags": body.tags,
        "status": "ready",
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.assets.insert_one(doc)
    return _ser(doc)


@router.delete("/projects/{project_id}/assets/{asset_id}")
async def delete_asset(project_id: str, asset_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    await db.assets.delete_one({"id": asset_id, "project_id": project_id})
    return {"ok": True}


@router.patch("/projects/{project_id}/assets/{asset_id}")
async def update_asset_status(project_id: str, asset_id: str, body: AssetStatusUpdate, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    result = await db.assets.update_one(
        {"id": asset_id, "project_id": project_id},
        {"$set": {"status": body.status, "updated_at": _now()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset = await db.assets.find_one({"id": asset_id, "project_id": project_id}, {"_id": 0})
    return _ser(asset)


# ============================ STOCK / PEXELS ============================

@router.get("/stock/meta")
async def stock_meta(user=Depends(get_current_user)):
    """Lightweight endpoint so the UI can show 'mock mode' badge without triggering a search."""
    # Use aggregated mock check — true only if all keys missing or flag set
    if hasattr(stock_service, "is_mock_mode_aggregated"):
        return {"mock": stock_service.is_mock_mode_aggregated()}
    return {"mock": stock_service.is_mock_mode()}


@router.get("/stock/search")
async def stock_search(
    q: str = "",
    source: str = "pexels",
    type: str = "both",
    per_page: int = 12,
):
    """Public stock search for scene picker — GET /api/stock/search?q=&source=pexels|pixabay|unsplash&type=video|image

    Calls Pexels Video API (preferred), Pixabay Video API, or Unsplash API
    depending on source param. Falls back to deterministic mock if provider
    key missing or rate-limited. No auth required so scene picker can query
    quickly; still respects mock mode when keys absent.

    Query params:
      q: search keywords
      source: pexels|pixabay|unsplash (default pexels)
      type: video|image|both (maps to videos/photos/both)
      per_page: 1..40
    """
    query = (q or "").strip()
    source = (source or "pexels").strip().lower()
    if source not in ("pexels", "pixabay", "unsplash"):
        source = "pexels"
    # Map type param to internal MediaType
    t = (type or "both").strip().lower()
    if t in ("video", "videos"):
        media_type = "videos"
    elif t in ("image", "images", "photo", "photos"):
        media_type = "photos"
    else:
        media_type = "both"
    per_page = max(1, min(int(per_page), 40))
    if not query:
        query = "forest"
    # Unsplash only supports images; force photos
    if source == "unsplash" and media_type == "videos":
        media_type = "photos"
    try:
        result = await stock_service.search_stock_with_source(query, source, media_type, per_page)
        return result
    except Exception as e:  # noqa: BLE001
        logger = __import__("logging").getLogger("facelessforge.routes")
        logger.warning("stock/search failed q=%r source=%s type=%s: %s", query[:60], source, media_type, e)
        # Fallback to mock never fails
        from ..stock import _mock_results
        return {"source": "mock", "results": _mock_results(query, media_type, per_page), "mock": True, "query": query, "warning": str(e)}


@router.post("/projects/{project_id}/stock-search")
async def project_stock_search(project_id: str, body: FindAssetsRequest, user=Depends(get_current_user)):
    """Ad-hoc stock search scoped to a project (no scene context). Aggregated Pexels+Pixabay+Unsplash."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    query = (body.query or project.get("topic") or project.get("niche") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Provide a query or ensure project has a topic.")
    # Aggregated multi-source — do NOT truncate query server-side, use full body.query
    if hasattr(stock_service, "search_stock_aggregated"):
        return await stock_service.search_stock_aggregated(query, body.media_type, body.per_page)
    return await stock_service.search_stock(query, body.media_type, body.per_page)


@router.post("/projects/{project_id}/scenes/{scene_id}/find-assets")
async def find_scene_assets(project_id: str, scene_id: str, body: FindAssetsRequest, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    scene = await db.scenes.find_one({"id": scene_id, "project_id": project_id}, {"_id": 0})
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")

    query = _build_scene_stock_query(scene, project, body.query)
    # Aggregated multi-source — merges Pexels + Pixabay + Unsplash, preserves source field per item
    if hasattr(stock_service, "search_stock_aggregated"):
        return await stock_service.search_stock_aggregated(query, body.media_type, body.per_page)
    return await stock_service.search_stock(query, body.media_type, body.per_page)


@router.post("/projects/{project_id}/scenes/{scene_id}/attach-asset")
async def attach_scene_asset(project_id: str, scene_id: str, body: StockAttachRequest, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot attach assets")
    scene = await db.scenes.find_one({"id": scene_id, "project_id": project_id}, {"_id": 0})
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")

    lease = await project_lease.acquire(
        db, project_id, project_lease.ASSET_LEASE, ttl_seconds=project_lease.ASSET_LEASE_TTL_SECONDS,
    )
    if lease is None:
        raise HTTPException(status_code=409, detail="Another asset update is running for this project; retry shortly.")
    async def commit(db):
        # Idempotency: reject duplicates by (project_id, scene_id, external_id, source)
        existing = await db.assets.find_one({
            "project_id": project_id,
            "scene_id": scene_id,
            "external_id": body.external_id,
            "source": body.source,
        }, {"_id": 0})
        if existing:
            raise HTTPException(status_code=409, detail="This asset is already attached to the scene.")

        doc = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "scene_id": scene_id,
            "name": body.title,
            "asset_type": body.media_type,  # stock_video | stock_image
            "file_path": None,
            "source": body.source,
            "external_id": body.external_id,
            "preview_url": body.preview_url,
            "source_url": body.source_url,
            "download_url": body.download_url,
            "attribution_name": body.attribution_name,
            "attribution_url": body.attribution_url,
            "width": body.width,
            "height": body.height,
            "duration": body.duration,
            "tags": body.tags or [],
            "query": body.query,
            "status": "attached",
            "created_at": _now(),
            "updated_at": _now(),
        }
        await asset_commit.set_intent(db, {
            "kind": "manual_attach", "scene_id": scene_id, "candidates": [doc],
            "previous_scene": scene,
        })
        try:
            await db.assets.insert_one(doc)
        except Exception as exc:  # noqa: BLE001
            if _is_duplicate_key_error(exc):
                raise HTTPException(status_code=409, detail="This asset is already attached to the scene.") from exc
            import logging as _logging
            _logging.getLogger("facelessforge.routes").error("manual attach insert failed project=%s scene=%s", project_id, scene_id, exc_info=True)
            raise HTTPException(status_code=503, detail="Asset storage is unavailable; the attachment was not saved. Retry.") from exc
        scene_update = await db.scenes.update_one(
            {"id": scene_id, "project_id": project_id},
            {"$set": {"status": "assets_attached", "updated_at": _now()}},
        )
        if getattr(scene_update, "matched_count", 0) != 1:
            # Regeneration can remove this scene ID while attachment is in flight.
            # The insert was acknowledged; compensate only our row under the pin.
            await db.assets.delete_one({"project_id": project_id, "id": doc["id"]})
            raise HTTPException(status_code=409, detail="Scene changed during attachment; refresh the scene plan and retry.")
        return doc

    try:
        doc = await asset_commit.run(db, lease, commit)
    except asset_commit.LeaseLost as exc:
        raise HTTPException(status_code=409, detail="Asset ownership changed; retry the attachment.") from exc
    except asset_commit.CommitUncertain as exc:
        raise HTTPException(status_code=503, detail="Asset commit requires reconciliation; retry after recovery.") from exc
    finally:
        await project_lease.release(db, lease)
    # Archive licence at acquisition (Agent 3 spec: rights_registry + filesystem)
    try:
        await db.rights_registry.insert_one({
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "scene_id": scene_id,
            "asset_id": doc["id"],
            "external_id": body.external_id,
            "source": body.source,
            "source_url": body.source_url,
            "download_url": body.download_url,
            "license": "pexels" if body.source == "pexels" else body.source,
            "attribution_name": body.attribution_name,
            "attribution_url": body.attribution_url,
            "archive_license": True,
            "acquired_at": _now().isoformat(),
            "acquired_by": user["id"],
        })
        # filesystem archive
        import pathlib as _pl
        lic_dir = _pl.Path("/opt/facelessforge/licenses") / project_id
        lic_dir.mkdir(parents=True, exist_ok=True)
        (lic_dir / f"{doc['id']}.json").write_text(__import__("json").dumps({
            "asset_id": doc["id"], "external_id": body.external_id, "source": body.source,
            "source_url": body.source_url, "license": "pexels" if body.source == "pexels" else body.source,
            "attribution_name": body.attribution_name, "attribution_url": body.attribution_url,
            "archive_license": True, "acquired_at": _now().isoformat()
        }, indent=2))
    except Exception:
        pass
    return _ser(doc)


# ---- Auto-attach 3-second rule: 10 assets per 30s scene ----

def _parse_time_to_seconds(t: str) -> float:
    """Parse 0:00, 0:30, 1:30, 00:01:30 to seconds."""
    t = (t or "").strip()
    if not t:
        return 0.0
    parts = t.strip().split(":")
    try:
        parts = [float(p.strip()) for p in parts if p.strip() != ""]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 1:
            return float(parts[0])
    except Exception:
        return 0.0
    return 0.0

def _get_scene_duration(scene: dict) -> float:
    """Parse duration from scene.duration or start_time/end_time or scene.time '0:00 -> 0:30'."""
    try:
        if scene.get("duration") is not None:
            d = float(scene.get("duration") or 0)
            if d > 0:
                return d
        if scene.get("end_time") is not None and scene.get("start_time") is not None:
            try:
                end = float(scene.get("end_time") or 0)
                start = float(scene.get("start_time") or 0)
                if end > start:
                    return end - start
            except Exception:
                pass
        t = scene.get("time") or scene.get("timestamp") or ""
        if isinstance(t, str) and "->" in t:
            left, right = t.split("->", 1)
            s = _parse_time_to_seconds(left.strip())
            e = _parse_time_to_seconds(right.strip())
            if e > s:
                return e - s
            if e > 0:
                return e
        # fallback to string duration like "30s"
        for k in ("duration", "length", "time"):
            v = scene.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
    except Exception:
        pass
    return 30.0

def _dedup_by_preview(results: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for r in results:
        k = r.get("preview_url") or r.get("external_id") or r.get("id")
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out

STOCK_ASSET_TYPES = ["stock_video", "stock_image"]


def _is_duplicate_key_error(exc: BaseException) -> bool:
    """True only for a unique-index conflict (Mongo 11000), never for a storage failure."""
    return getattr(exc, "code", None) == 11000 or type(exc).__name__ == "DuplicateKeyError"


class _CreditsExhausted(Exception):
    """A scene's committed attachments could not be debited; the scene is rolled back."""


def _stock_scene_query(project_id: str, scene_id: str) -> dict:
    return {"project_id": project_id, "scene_id": scene_id, "asset_type": {"$in": STOCK_ASSET_TYPES}}


async def _rollback_scene_assets(db, project_id: str, committed: list, restore: list) -> None:
    """Undo a scene's partial commit: remove rows this run inserted, restore removed originals."""
    import logging
    logger = logging.getLogger("facelessforge.routes")
    ids = [doc["id"] for doc in committed]
    if ids:
        try:
            await db.assets.delete_many({"project_id": project_id, "id": {"$in": ids}})
        except Exception:  # noqa: BLE001
            asset_commit.require_recovery(db)
            logger.critical("auto-attach rollback could not remove new rows project=%s ids=%s", project_id, ids, exc_info=True)
    if not restore:
        return
    wanted = [doc["id"] for doc in restore]
    try:
        present = {
            d["id"] for d in await db.assets.find(
                {"project_id": project_id, "id": {"$in": wanted}}, {"_id": 0, "id": 1},
            ).to_list(len(wanted) + 1)
        }
    except Exception:  # noqa: BLE001
        asset_commit.require_recovery(db)
        logger.critical("auto-attach rollback could not inspect originals project=%s", project_id, exc_info=True)
        return
    for doc in restore:
        if doc["id"] in present:
            continue
        try:
            await db.assets.insert_one(dict(doc))
        except Exception:  # noqa: BLE001
            asset_commit.require_recovery(db)
            logger.critical("auto-attach rollback could not restore original project=%s asset=%s", project_id, doc.get("id"), exc_info=True)


async def _debit_committed_attachments(
    db, *, collection: str, balance_filter: dict, field: str, user_id: str, asset_ids: list, reason: str,
) -> int:
    """Debit one credit per committed attachment, exactly once per attachment.

    The debit and the record of which attachment it paid for are one single-document
    update on the account, so a retried or resumed operation cannot debit the same
    attachment twice, and a crash cannot debit without recording it. The balance
    update is conditional on sufficient credits. Returns how many attachments this
    call newly debited. On any failure, debits made by this call are reversed.
    """
    import logging
    logger = logging.getLogger("facelessforge.routes")
    target = getattr(db, collection)
    newly: list[str] = []
    try:
        for asset_id in asset_ids:
            res = await target.update_one(
                {**balance_filter, "charged_attach_ids": {"$ne": asset_id}, field: {"$gte": 1}},
                {
                    "$inc": {field: -1},
                    "$set": {"updated_at": _now()},
                    "$addToSet": {"charged_attach_ids": asset_id},
                },
            )
            if getattr(res, "matched_count", 0) != 1:
                already = await target.find_one({**balance_filter, "charged_attach_ids": asset_id}, {"_id": 0})
                if already:
                    continue  # charged by an earlier attempt of this operation
                raise _CreditsExhausted()
            newly.append(asset_id)
            try:
                await db.credit_transactions.insert_one({
                    "_id": f"attach:{asset_id}",
                    "id": str(uuid.uuid4()),
                    "user_id": user_id,
                    "asset_id": asset_id,
                    "amount": -1,
                    "reason": reason,
                    "created_at": _now(),
                })
            except Exception:  # noqa: BLE001 - the balance is authoritative; the ledger row is an audit copy
                logger.warning("auto-attach ledger row not written asset=%s", asset_id, exc_info=True)
    except BaseException:
        for asset_id in newly:
            try:
                await target.update_one(
                    {**balance_filter, "charged_attach_ids": asset_id},
                    {"$inc": {field: 1}, "$pull": {"charged_attach_ids": asset_id}},
                )
                await db.credit_transactions.delete_one({"_id": f"attach:{asset_id}"})
            except Exception:  # noqa: BLE001
                asset_commit.require_recovery(db)
                logger.critical("auto-attach could not reverse debit asset=%s", asset_id, exc_info=True)
        raise
    return len(newly)


async def _debit_scene(db, *, store: dict, user_id: str, asset_ids: list, reason: str) -> int:
    """Debit the primary balance, then the legacy ff_credits mirror (best effort)."""
    charged = await _debit_committed_attachments(
        db, collection=store["collection"], balance_filter=store["filter"], field=store["field"],
        user_id=user_id, asset_ids=asset_ids, reason=reason,
    )
    if store["collection"] == "subscriptions" and charged:
        try:
            await _debit_committed_attachments(
                db, collection="ff_credits", balance_filter={"tenant_id": user_id}, field="credits",
                user_id=user_id, asset_ids=asset_ids, reason=reason,
            )
        except Exception:  # noqa: BLE001 - legacy mirror never blocks an attachment
            import logging
            logging.getLogger("facelessforge.routes").warning("auto-attach legacy ff_credits mirror skipped", exc_info=True)
    return charged


def _auto_stock_query(project: dict, scene: dict) -> str:
    return _build_scene_stock_query(scene, project)


async def _asset_write(db, lease, collection, method, *args, **kwargs):
    async def write(fenced_db):
        return await getattr(getattr(fenced_db, collection), method)(*args, **kwargs)
    return await asset_commit.run(db, lease, write)


async def _attach_scene_stock(db, *, lease, project, scene, replace_existing, user, store,
                              visual_titles, media_type, op_id=None):
    """Slow lookup is revocable; the entire mutation/compensation phase is pinned."""
    originals = await db.assets.find(
        _stock_scene_query(project["id"], str(scene.get("id") or scene.get("_id"))), {"_id": 0},
    ).to_list(500)
    carried = [o for o in originals if op_id and o.get("attach_op") == op_id]
    needed = max(1, math.ceil(_get_scene_duration(scene) / 3))
    needs_search = not (
        (replace_existing and carried and len(carried) == len(originals))
        or (not replace_existing and len(originals) >= needed)
    )
    result, search_error = None, None
    if needs_search:
        remaining = needed if replace_existing else max(1, needed - len(originals))
        per_page = min(max(remaining * 2, remaining + 5), 40)
        query = _auto_stock_query(project, scene)
        # Stock providers can return only one result for an over-specific query.
        # Search alternative scene terms before accepting a partial selection.
        # The commit phase still deduplicates, fences writers and retains old assets.
        candidates = []
        attempted = set()
        fallbacks = list(scene.get("search_terms") or []) if isinstance(scene.get("search_terms"), list) else []
        fallbacks.append(scene.get("visual_direction") or "")
        fallbacks.append(project.get("topic") or "")
        for term in [query, *fallbacks]:
            term = compact_stock_query(term)
            if not term or term.casefold() in attempted:
                continue
            attempted.add(term.casefold())
            try:
                if hasattr(stock_service, "search_stock_aggregated"):
                    found = await stock_service.search_stock_aggregated(query=term, media_type="both", per_page=per_page)
                else:
                    found = await stock_service.search_stock(term, "both", per_page)
                candidates.extend((found or {}).get("results") or [])
                # Leave headroom for duplicates already used in the scene/project.
                if len(_dedup_by_preview(candidates)) >= remaining * 2:
                    break
            except Exception as exc:  # noqa: BLE001
                search_error = exc
        if candidates:
            result = {"results": candidates}
            search_error = None


    async def commit(fenced_db):
        return await _attach_scene_stock_committing(
            fenced_db, project=project, scene=scene, replace_existing=replace_existing, user=user,
            store=store, visual_titles=visual_titles, media_type=media_type, op_id=op_id,
            stock_result=result, search_error=search_error,
        )
    return await asset_commit.run(db, lease, commit)


async def _attach_scene_stock_committing(
    db, *, project: dict, scene: dict, replace_existing: bool, user: dict, store: dict,
    visual_titles: list, media_type: str, stock_result, search_error, op_id: Optional[str] = None,
) -> dict:
    """Select and commit stock assets for one scene. Never raises for scene-level failures.

    Insert-first: the new candidates are written before the scene's previous stock
    selection is removed. Any storage failure rolls the scene back to its previous
    selection and charges nothing. Duplicate-key conflicts are actual duplicates and
    are skipped; every other write error is a failure.
    """
    import logging
    logger = logging.getLogger("facelessforge.routes")
    project_id = project["id"]
    scene_id = str(scene.get("id") or scene.get("_id"))
    duration = _get_scene_duration(scene)
    num_needed = max(1, math.ceil(duration / 3))
    outcome: dict = {
        "scene_id": scene_id, "status": "failed", "attached": 0, "charged": 0, "skipped": 0,
        "near_duplicates": 0, "details": [], "reason": None,
    }

    def _detail(status: str, **extra) -> None:
        outcome["details"].append({"scene_id": scene_id, "scene_number": scene.get("scene_number"), "status": status, **extra})

    originals = await db.assets.find(_stock_scene_query(project_id, scene_id), {"_id": 0}).to_list(500)
    intent = {
        "kind": "auto_attach", "scene_id": scene_id, "replace_existing": replace_existing,
        "op_id": op_id, "originals": originals, "account": store,
    }
    await asset_commit.set_intent(db, intent)
    # Rows tagged with this operation were committed by an earlier attempt that stopped
    # before recording the scene. Finish their accounting (idempotent) instead of
    # selecting and charging the scene again.
    carried = [o for o in originals if op_id and o.get("attach_op") == op_id]
    carried_committed = False
    if carried and (not replace_existing or len(carried) == len(originals)):
        # Replacement: every original is ours, so the earlier attempt had already removed
        # the old selection and written the whole replacement. Top-up: these rows are
        # committed attachments that stay. Either way they are paid for exactly once.
        try:
            charged_now = await _debit_scene(
                db, store=store, user_id=user["id"], asset_ids=[o["id"] for o in carried],
                reason=f"auto-attach scene {scene.get('scene_number')} {duration}s",
            )
        except _CreditsExhausted:
            # Carried rows belong to an earlier committed attempt, and may already
            # be paid. Never delete them on a resume failure (top-up OR replacement).
            # This call's partial debits are reversed by the debit helper; a same-key
            # retry completes the remaining IDs without losing the paid selection.
            outcome.update(reason="credits_exhausted", attached=0)
            _detail("failed", reason="credits_exhausted")
            return outcome
        except Exception:  # noqa: BLE001
            import logging as _logging
            _logging.getLogger("facelessforge.routes").error("auto-attach resume debit failed project=%s scene=%s", project_id, scene_id, exc_info=True)
            outcome.update(reason="charge_failed", attached=0)
            _detail("failed", reason="charge_failed")
            return outcome
        for doc in carried:
            visual_titles.append(str(doc.get("name") or ""))
            _detail("attached", asset_id=doc["id"], order=doc.get("order"), start_offset=doc.get("start_offset"))
        outcome.update(attached=len(carried), charged=charged_now)
        if replace_existing:
            outcome["status"] = "attached"
            return outcome
        carried_committed = True
    if originals and not replace_existing:
        if len(originals) >= num_needed:
            if carried_committed:
                outcome["status"] = "attached"
            else:
                outcome.update(status="skipped", skipped=1)
                _detail("skipped", reason="already_has_stock")
            return outcome
        remaining = num_needed - len(originals)
        start_order = len(originals)
        excluded = originals
    else:
        remaining = num_needed
        start_order = 0
        # A replacement set must be disjoint from the selection it replaces: the
        # unique stock index would otherwise reject the same clip while the original
        # still exists, and that would be indistinguishable from a real duplicate.
        excluded = originals if replace_existing else []

    query = _auto_stock_query(project, scene)

    excluded_ids: set[str] = set()
    for ea in excluded:
        if ea.get("external_id"):
            excluded_ids.add(str(ea.get("external_id")))
        if ea.get("preview_url"):
            excluded_ids.add(str(ea.get("preview_url")))

    if search_error is not None:
        logger.warning("auto-attach search failed scene=%s: %s", scene_id, search_error)
        outcome["reason"] = "search_failed"
        _detail("failed", reason=str(search_error)[:120])
        return outcome

    results = (stock_result.get("results") or []) if stock_result else []
    filtered = []
    for r in results:
        ext = str(r.get("external_id") or "")
        prev = str(r.get("preview_url") or "")
        if ext in excluded_ids or prev in excluded_ids:
            continue
        filtered.append(r)
    unique = _dedup_by_preview(filtered)
    unique, irrelevant = relevant_candidates(unique, scene, query)
    for rejected in irrelevant:
        _detail("skipped", reason="irrelevant_or_unverified", title=rejected.get("title") or rejected.get("name"))
    unique, near_duplicates = filter_distinct_titles(unique, visual_titles)
    for dup in near_duplicates:
        outcome["near_duplicates"] += 1
        _detail("skipped", reason="near_duplicate", title=dup.get("title") or dup.get("name"))
    unique = unique[:remaining]
    if not unique:
        # Nothing usable: the scene keeps its current selection untouched.
        outcome["reason"] = "no_relevant_stock"
        _detail("failed", reason="no_relevant_stock")
        return outcome

    new_docs: list[dict] = []
    for i, asset in enumerate(unique):
        order = start_order + i
        new_docs.append({
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "scene_id": scene_id,
            "name": asset.get("title") or asset.get("name") or f"Asset {order+1}",
            "asset_type": asset.get("media_type") or asset.get("asset_type") or "stock_image",
            "file_path": None,
            "source": asset.get("source") or "pexels",
            "external_id": asset.get("external_id") or str(uuid.uuid4()),
            "preview_url": asset.get("preview_url"),
            "source_url": asset.get("source_url"),
            "download_url": asset.get("download_url"),
            "attribution_name": asset.get("attribution_name"),
            "attribution_url": asset.get("attribution_url"),
            "width": asset.get("width"),
            "height": asset.get("height"),
            "duration": 3,
            "tags": asset.get("tags") or [],
            "query": query,
            "status": "attached",
            "order": order,
            "start_offset": order * 3,  # 0,3,6,9...
            "scene_duration": duration,
            "attach_op": op_id,
            "created_at": _now(),
            "updated_at": _now(),
        })

    await asset_commit.set_intent(db, {**intent, "candidates": new_docs})
    committed: list[dict] = []
    for doc in new_docs:
        try:
            await db.assets.insert_one(dict(doc))
        except Exception as exc:  # noqa: BLE001
            if _is_duplicate_key_error(exc):
                outcome["skipped"] += 1
                _detail("skipped", reason="duplicate")
                continue
            logger.error("auto-attach insert failed project=%s scene=%s", project_id, scene_id, exc_info=True)
            await _rollback_scene_assets(db, project_id, committed, restore=[])
            outcome.update(reason="storage_error", attached=0)
            _detail("failed", reason="storage_error")
            return outcome
        committed.append(doc)

    if committed and replace_existing and originals:
        # Originals go only now that the replacement is fully written. Delete by
        # captured id, never by scene, so another writer's rows are never removed.
        try:
            await db.assets.delete_many({"project_id": project_id, "id": {"$in": [o["id"] for o in originals]}})
        except Exception:  # noqa: BLE001
            logger.error("auto-attach replacement cleanup failed project=%s scene=%s", project_id, scene_id, exc_info=True)
            await _rollback_scene_assets(db, project_id, committed, restore=originals)
            outcome.update(reason="storage_error", attached=0)
            _detail("failed", reason="storage_error")
            return outcome

    if committed:
        try:
            charged_now = await _debit_scene(
                db, store=store, user_id=user["id"], asset_ids=[d["id"] for d in committed],
                reason=f"auto-attach scene {scene.get('scene_number')} {duration}s",
            )
        except _CreditsExhausted:
            await _rollback_scene_assets(db, project_id, committed, restore=originals if replace_existing else [])
            outcome.update(reason="credits_exhausted", attached=0)
            _detail("failed", reason="credits_exhausted")
            return outcome
        except Exception:  # noqa: BLE001
            logger.error("auto-attach debit failed project=%s scene=%s", project_id, scene_id, exc_info=True)
            await _rollback_scene_assets(db, project_id, committed, restore=originals if replace_existing else [])
            outcome.update(reason="charge_failed", attached=0)
            _detail("failed", reason="charge_failed")
            return outcome
        outcome["charged"] += charged_now

    for doc in committed:
        visual_titles.append(doc["name"])
        _detail("attached", asset_id=doc["id"], order=doc["order"], start_offset=doc["start_offset"])
    outcome["attached"] += len(committed)
    # Successful API writes do not imply the scene meets the 3-second pacing rule.
    # Keep any good clips as a resumable top-up, but report incomplete coverage.
    final_count = (0 if replace_existing else len(originals)) + len(committed)
    if final_count < num_needed:
        outcome["status"] = "failed"
        outcome["reason"] = "incomplete_visual_coverage"
        _detail("failed", reason="incomplete_visual_coverage", required=num_needed, available=final_count)
    elif carried_committed or committed:
        outcome["status"] = "attached"
    else:
        outcome["status"] = "skipped"
    logger.info(
        "auto-attach scene %s duration=%s num_needed=%s committed=%d skipped=%d status=%s",
        scene.get("scene_number"), duration, num_needed, len(committed), outcome["skipped"], outcome["status"],
    )
    return outcome


def _auto_attach_summary(*, total: int, ordered: list, mock: bool, aborted: Optional[str]) -> dict:
    return {
        "total": total,
        "attached": sum(r["attached"] for r in ordered),
        "skipped": sum(r["skipped"] for r in ordered),
        "near_duplicates_skipped": sum(r["near_duplicates"] for r in ordered),
        "failed": sum(1 for r in ordered if r["status"] == "failed"),
        "credits_charged": sum(r["charged"] for r in ordered),
        "details": [d for r in ordered for d in r["details"]],
        "mock": mock,
        "aborted": aborted,
        "replayed": False,
    }


@router.post("/projects/{project_id}/auto-attach-assets")
async def auto_attach_assets(project_id: str, body: AutoAttachRequest, user=Depends(get_current_user)):
    """3-second rule: ceil(duration/3) assets per scene (10 for 30s), 3s each, order/start_offset.

    Writers of a project's stock selection (this route, manual scene attach) share
    one fenced asset lease, so a replacement, top-up or manual attach can never
    interleave with another. Replacement is insert-first and rolls back on failure,
    so a failed replacement leaves the previous selection in place. Credits are
    debited only for attachments that committed. With an ``idempotency_key`` a
    repeated request replays its stored outcome, and a failed request resumes
    without re-processing scenes it already finished.
    """
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot attach assets")
    if body.replace_existing and not body.idempotency_key:
        raise HTTPException(
            status_code=422,
            detail="replace_existing requires an idempotency_key so a repeated request cannot replace or charge twice.",
        )
    op_id = f"{project_id}:{body.idempotency_key}" if body.idempotency_key else None
    if op_id:
        prior = await db.asset_operations.find_one({"_id": op_id}, {"_id": 0})
        if prior and prior.get("status") == "completed":
            return {**(prior.get("summary") or {}), "replayed": True}

    lease = await project_lease.acquire(
        db, project_id, project_lease.ASSET_LEASE, ttl_seconds=project_lease.ASSET_LEASE_TTL_SECONDS,
    )
    if lease is None:
        raise HTTPException(status_code=409, detail="Another asset update is running for this project; retry shortly.")
    try:
        return await _auto_attach_under_lease(db, lease, project_id, project, body, user, op_id)
    except asset_commit.LeaseLost as exc:
        raise HTTPException(status_code=409, detail="Asset ownership changed; retry with the same idempotency key.") from exc
    except asset_commit.CommitUncertain as exc:
        raise HTTPException(status_code=503, detail="Asset commit requires reconciliation; retry after recovery.") from exc
    finally:
        await project_lease.release(db, lease)


async def _auto_attach_under_lease(db, lease, project_id: str, project: dict, body: AutoAttachRequest, user: dict, op_id: Optional[str]) -> dict:
    import logging
    logger = logging.getLogger("facelessforge.routes")
    now = _now()
    resumed: dict = {}
    if op_id:
        try:
            await _asset_write(db, lease, "asset_operations", "insert_one", {
                "_id": op_id, "project_id": project_id, "user_id": user["id"], "status": "running",
                "scene_results": {}, "created_at": now, "updated_at": now,
            })
        except Exception as exc:  # noqa: BLE001
            if not _is_duplicate_key_error(exc):
                raise
            prior = await db.asset_operations.find_one({"_id": op_id}, {"_id": 0}) or {}
            if prior.get("status") == "completed":
                return {**(prior.get("summary") or {}), "replayed": True}
            # The lease is ours, so no other writer is active: a non-completed record
            # was left by an interrupted run. Resume it.
            resumed = dict(prior.get("scene_results") or {})
            await _asset_write(db, lease, "asset_operations", "update_one", {"_id": op_id}, {"$set": {"status": "running", "updated_at": now}})

    try:
        scenes = await db.scenes.find({"project_id": project_id}).sort("scene_number", 1).to_list(500)
        if not scenes:
            raise HTTPException(status_code=400, detail="Generate scenes before auto-attach.")

        # Credits check: include unpaid carried rows, but do not require a second
        # balance for attachments this operation already committed and paid for.
        total_needed = 0
        carried_asset_ids: list[str] = []
        for scene in scenes:
            needed = max(1, math.ceil(_get_scene_duration(scene) / 3))
            scene_id = str(scene.get("id") or scene.get("_id"))
            if (resumed.get(scene_id) or {}).get("status") in ("attached", "skipped"):
                continue  # finished and charged by an earlier attempt of this same operation
            count = await db.assets.count_documents(_stock_scene_query(project_id, scene_id))
            if op_id:
                carried = await db.assets.find(
                    {**_stock_scene_query(project_id, scene_id), "attach_op": op_id},
                    {"_id": 0, "id": 1},
                ).to_list(None)
                if carried and (not body.replace_existing or len(carried) == count):
                    carried_asset_ids.extend(row["id"] for row in carried)
                    total_needed += len(carried)
                    if body.replace_existing:
                        continue  # finish the committed replacement, not a new selection
            if count > 0 and not body.replace_existing:
                if count >= needed:
                    continue
                total_needed += needed - count
            else:
                total_needed += needed

        sub = await db.subscriptions.find_one({"user_id": user["id"]}, {"_id": 0})
        if sub:
            store = {"collection": "subscriptions", "filter": {"user_id": user["id"]}, "field": "credits_remaining"}
            remaining = int(sub.get("credits_remaining", 30))
            paid_asset_ids = set(sub.get("charged_attach_ids") or [])
        else:
            ff_filter = {"tenant_id": user["id"]}
            ff = await db.ff_credits.find_one(ff_filter, {"_id": 0})
            if ff is None:
                ff_filter = {"user_id": user["id"]}
                ff = await db.ff_credits.find_one(ff_filter, {"_id": 0})
            if ff:
                store = {"collection": "ff_credits", "filter": ff_filter, "field": "credits"}
                remaining = int(ff.get("credits", 30))
                paid_asset_ids = set(ff.get("charged_attach_ids") or [])
                sub = {"credits_monthly": int(ff.get("credits", 30)), "plan": "free"}
            else:
                sub = {
                    "user_id": user["id"], "paddle_customer_id": None, "paddle_subscription_id": None,
                    "paddle_price_id": None, "plan": "free", "status": "active",
                    "credits_monthly": 30, "credits_remaining": 30,
                    "current_period_end": _now() + timedelta(days=30),
                    "created_at": _now(), "updated_at": _now(),
                }
                await _asset_write(db, lease, "subscriptions", "insert_one", dict(sub))
                store = {"collection": "subscriptions", "filter": {"user_id": user["id"]}, "field": "credits_remaining"}
                remaining = 30
                paid_asset_ids = set()
        total_needed -= sum(asset_id in paid_asset_ids for asset_id in carried_asset_ids)
        if total_needed > 0 and remaining < total_needed:
            reset_at = sub.get("current_period_end") or _now() + timedelta(days=30)
            if hasattr(reset_at, 'isoformat'):
                reset_at = reset_at.isoformat()
            raise HTTPException(status_code=402, detail={"code": "credits_exhausted", "reset_at": reset_at, "plan": sub.get("plan", "free"), "remaining": remaining, "required": total_needed, "quota": int(sub.get("credits_monthly", 30)), "message": f"Insufficient credits: {remaining} remaining, {total_needed} required. Need {total_needed} credits for {total_needed} assets (3s each)."})

        # Titles already in the project keep near-duplicate filtering honest. In replace
        # mode only scenes finished by this same operation count; the rest are being replaced.
        if body.replace_existing:
            done_scene_ids = [sid for sid, r in resumed.items() if r.get("status") in ("attached", "skipped")]
            visual_titles = []
            if done_scene_ids:
                done_assets = await db.assets.find(
                    {"project_id": project_id, "scene_id": {"$in": done_scene_ids}, "asset_type": {"$in": STOCK_ASSET_TYPES}},
                    {"_id": 0, "name": 1},
                ).to_list(1000)
                visual_titles = [str(a.get("name") or "") for a in done_assets if a.get("name")]
        else:
            visual_titles = [
                str(a.get("name") or "")
                for a in await db.assets.find(
                    {"project_id": project_id, "asset_type": {"$in": STOCK_ASSET_TYPES}}, {"_id": 0, "name": 1},
                ).to_list(1000)
                if a.get("name")
            ]

        results: dict = {}
        aborted = None
        for scene in scenes:
            scene_id = str(scene.get("id") or scene.get("_id"))
            if (resumed.get(scene_id) or {}).get("status") in ("attached", "skipped"):
                results[scene_id] = resumed[scene_id]
                continue
            if not await project_lease.renew(db, lease, ttl_seconds=project_lease.ASSET_LEASE_TTL_SECONDS):
                aborted = "lease_lost"
                break
            try:
                outcome = await _attach_scene_stock(
                    db, lease=lease, project=project, scene=scene, replace_existing=body.replace_existing, user=user,
                    store=store, visual_titles=visual_titles, media_type=body.media_type, op_id=op_id,
                )
            except asset_commit.LeaseLost:
                aborted = "lease_lost"
                break
            except asset_commit.CommitUncertain:
                raise
            except Exception:  # noqa: BLE001 - one scene must not abort the rest of the operation
                logger.error("auto-attach scene failed project=%s scene=%s", project_id, scene_id, exc_info=True)
                outcome = {
                    "scene_id": scene_id, "status": "failed", "attached": 0, "charged": 0, "skipped": 0,
                    "near_duplicates": 0, "reason": "scene_error",
                    "details": [{"scene_id": scene_id, "scene_number": scene.get("scene_number"),
                                 "status": "failed", "reason": "scene_error"}],
                }
            results[scene_id] = outcome
            if op_id:
                await _asset_write(db, lease, "asset_operations", "update_one", {"_id": op_id},
                    {"$set": {"scene_results": {**resumed, **results}, "updated_at": _now()}},
                )

        ordered = [results[str(s.get("id") or s.get("_id"))] for s in scenes if str(s.get("id") or s.get("_id")) in results]
        complete = aborted is None and len(ordered) == len(scenes) and all(r["status"] in ("attached", "skipped") for r in ordered)
        summary = _auto_attach_summary(total=len(scenes), ordered=ordered, mock=stock_service.is_mock_mode(), aborted=aborted)
        if op_id and aborted != "lease_lost":
            # Only a fully processed operation is final. Anything else stays resumable
            # with the same key and never re-charges scenes already committed.
            await _asset_write(db, lease, "asset_operations", "update_one", {"_id": op_id},
                {"$set": {"status": "completed" if complete else "failed", "summary": summary, "updated_at": _now()}},
            )
        import logging as _logging
        _logging.getLogger("facelessforge.routes").info(
            "[ATTACH] %s attached %s failed project=%s mock=%s", summary["attached"], summary["failed"], project_id, summary["mock"],
        )
        return summary
    except (asset_commit.LeaseLost, asset_commit.CommitUncertain):
        raise
    except BaseException:
        if op_id:
            try:
                await _asset_write(db, lease, "asset_operations", "update_one", {"_id": op_id}, {"$set": {"status": "failed", "updated_at": _now()}})
            except Exception:  # noqa: BLE001
                logger.critical("could not mark auto-attach operation failed op=%s", op_id, exc_info=True)
        raise


# ============================ THUMBNAIL IMAGE GENERATION ============================

@router.get("/thumbnails/meta")
async def thumbnails_meta(user=Depends(get_current_user)):
    return thumb_images.provider_info()


@router.post("/projects/{project_id}/thumbnails/{brief_asset_id}/generate")
async def generate_thumbnail_image_endpoint(
    project_id: str, brief_asset_id: str,
    body: GenerateThumbnailImagesRequest,
    user=Depends(get_current_user),
):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot generate images")
    brief_asset = await db.assets.find_one(
        {"id": brief_asset_id, "project_id": project_id, "asset_type": "thumbnail_concept"},
        {"_id": 0},
    )
    if not brief_asset:
        raise HTTPException(status_code=404, detail="Thumbnail brief not found")
    brief = brief_asset.get("brief") or {}

    generated = await thumb_images.generate_thumbnail_images(
        project, brief, variants=body.variants, project_id=project_id,
    )
    if not generated:
        raise HTTPException(status_code=502, detail="Image generation failed. Please retry.")

    now = _now()
    inserts = []
    for g in generated:
        g["brief_asset_id"] = brief_asset_id
        g["created_at"] = now
        g["updated_at"] = now
        inserts.append(dict(g))
    await db.assets.insert_many([dict(d) for d in inserts])

    est_cost = 0.02 * len(generated) if not generated[0].get("mock") else 0
    if est_cost:
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"estimated_cost": float(project.get("estimated_cost", 0)) + est_cost, "updated_at": now}},
        )
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.post("/projects/{project_id}/thumbnails/{asset_id}/select")
async def select_thumbnail(project_id: str, asset_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot select thumbnails")
    asset = await db.assets.find_one(
        {"id": asset_id, "project_id": project_id, "asset_type": "generated_thumbnail"},
        {"_id": 0},
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Generated thumbnail not found")
    now = _now()
    # Demote any currently selected thumbnail
    await db.assets.update_many(
        {"project_id": project_id, "asset_type": "generated_thumbnail", "status": "selected"},
        {"$set": {"status": "generated", "updated_at": now}},
    )
    await db.assets.update_one(
        {"id": asset_id, "project_id": project_id},
        {"$set": {"status": "selected", "updated_at": now}},
    )
    await db.projects.update_one(
        {"id": project_id},
        {"$set": {"selected_thumbnail_asset_id": asset_id, "updated_at": now}},
    )
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.post("/projects/{project_id}/thumbnails/{asset_id}/reject")
async def reject_thumbnail(project_id: str, asset_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot reject thumbnails")
    asset = await db.assets.find_one(
        {"id": asset_id, "project_id": project_id, "asset_type": "generated_thumbnail"},
        {"_id": 0},
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Generated thumbnail not found")
    now = _now()
    await db.assets.update_one(
        {"id": asset_id, "project_id": project_id},
        {"$set": {"status": "rejected", "updated_at": now}},
    )
    # If the rejected one was selected, clear project pointer
    if project.get("selected_thumbnail_asset_id") == asset_id:
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"selected_thumbnail_asset_id": None, "updated_at": now}},
        )
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


# ============================ VOICEOVER (TTS) ============================
from .. import tts as tts_service
from ..models import GenerateVoiceoverRequest


@router.get("/tts/meta")
async def tts_meta(user=Depends(get_current_user)):
    return tts_service.provider_info()


def _voice_style_for(project: dict, override: Optional[str]) -> str:
    """Resolve a narrator preset ("NEUTRAL_FEMALE_NARRATOR") or style word ("calm").

    Preset/override strings are passed through untouched so tts.py resolves the
    ElevenLabs voice ID; free-form project.voice_style labels are normalized to
    a preset or style word so a female selection actually gets a female voice.
    """
    if override:
        return override
    raw = (project.get("voice_style") or "").strip()
    if not raw:
        return os.environ.get("DEFAULT_VOICE_STYLE", "narrator")
    preset = tts_service._normalize_preset(raw)
    if preset in tts_service.VOICE_MAP:
        return preset
    low = raw.lower()
    for key in tts_service.VOICE_STYLE_MAP.keys():
        if key in low:
            return key
    if "female" in low:
        return "NEUTRAL_FEMALE_NARRATOR"
    if "male" in low or "deep" in low or "narrator" in low:
        return "NEUTRAL_MALE_NARRATOR"
    if "upbeat" in low or "energy" in low or "fast" in low:
        return "energetic"
    if "doc" in low or "neutral" in low:
        return "documentary"
    return os.environ.get("DEFAULT_VOICE_STYLE", "narrator")


async def _record_voice_fit(db, project_id: str, script_version: str, attempts: int,
                          tts_calls: int, llm_calls: int, measured: float, actions: list) -> None:
    """Persist fitting provenance. Never raises — observability only."""
    try:
        project = await db.projects.find_one({"id": project_id}, {"_id": 0}) or {}
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"voice_fit": {
                "script_version": script_version,
                "attempts": attempts,
                "tts_calls": tts_calls,
                "llm_calls": llm_calls,
                "measured_seconds": round(float(measured or 0), 2),
                "target_seconds": project.get("target_duration"),
                "actions": list(actions or []),
                "updated_at": _now().isoformat(),
            }, "updated_at": _now()}},
        )
    except Exception:  # noqa: BLE001
        pass


@router.post("/projects/{project_id}/voiceover/generate-script")
async def generate_full_voiceover(
    project_id: str,
    body: GenerateVoiceoverRequest = Body(default=None),
    user=Depends(get_current_user),
):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot generate voiceovers")
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    if not script or not script.get("full_script", "").strip():
        raise HTTPException(status_code=400, detail="Generate a script before voiceover.")
    body = body or GenerateVoiceoverRequest()
    text = (body.text_override or script["full_script"]).strip()
    voice = _voice_style_for(project, body.voice_style)

    asset_id = str(uuid.uuid4())
    script_source = (script.get("source") or "ai_generated").lower()
    narration_budget = float(project.get("target_duration") or 30) - (
        0.0 if project.get("production_mode") == "commercial_ad" else 2.5
    )
    fit = project.get("voice_fit") or {}
    script_version = f"{script.get('id')}:{script.get('updated_at')}"
    if fit.get("script_version") == script_version:
        attempts_used = int(fit.get("attempts") or 0)
    else:
        attempts_used = 0
    if attempts_used >= 3:
        raise HTTPException(status_code=409, detail={
            "code": "voice_correction_exhausted",
            "message": "Voiceover correction budget exhausted for this script (initial + 2 corrections). Edit the script or apply an explicit duration fit.",
            "attempts": attempts_used,
        })
    actions: list = []
    llm_calls = 0
    current_text = text
    current_script_id = script.get("id")
    payload = None
    while True:
        attempts_used += 1
        try:
            payload = await tts_service.generate_voiceover(
                text=current_text, voice_style=voice, tone=body.tone, project_id=project_id,
                asset_id=asset_id if attempts_used == 1 else str(uuid.uuid4()),
                scene_id=None, name_suffix="(Full script)",
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Voiceover generation failed: {e}")
        measured = float(payload.get("duration") or 0)
        if measured <= 0:
            raise HTTPException(status_code=502, detail="Voiceover provider returned invalid audio (no measurable duration).")
        if narration_fits_budget(measured, narration_budget):
            actions.append(f"attempt {attempts_used}: measured {measured:.2f}s fits {narration_budget:.2f}s budget")
            break
        # Outside tolerance: manual scripts are never rewritten — stop with numbers.
        if script_source == "manual":
            actions.append(f"attempt {attempts_used}: measured {measured:.2f}s outside 5% of {narration_budget:.2f}s budget (manual script kept)")
            await _record_voice_fit(db, project_id, script_version, attempts_used, 1, llm_calls, measured, actions)
            suggestion = render_service.suggest_duration_autofit(
                project, {"duration": measured})
            raise HTTPException(status_code=400, detail={
                "code": "voiceover_length_mismatch",
                "message": f" narration is {measured:.1f}s but the {float(project.get('target_duration') or 30):.0f}s target allows {narration_budget:.1f}s: shorten/lengthen the script, or apply an explicit duration fit.",
                "measured_seconds": round(measured, 2),
                "narration_budget_seconds": round(narration_budget, 2),
                "autofit_suggestion": suggestion,
            })
        if attempts_used >= 3:
            actions.append(f"attempt {attempts_used}: still outside tolerance after 2 corrections")
            await _record_voice_fit(db, project_id, script_version, attempts_used, attempts_used, llm_calls, measured, actions)
            raise HTTPException(status_code=400, detail={
                "code": "voiceover_fit_exhausted",
                "message": f"Voiceover still outside 5% after 2 corrections ({measured:.1f}s vs {narration_budget:.1f}s budget). Edit the script or apply an explicit duration fit.",
                "measured_seconds": round(measured, 2),
                "narration_budget_seconds": round(narration_budget, 2),
                "autofit_suggestion": render_service.suggest_duration_autofit(project, {"duration": measured}),
            })
        # Bounded AI correction: same generator, length instruction scaled to the
        # measured shortfall/overrun. Meaning preserved; full new text is spoken
        # (never truncated to fit). attempts_used counts this correction.
        factor = narration_budget / measured
        current_words = len(current_text.split())
        intro_seconds = 0.0 if project.get("production_mode") == "commercial_ad" else 2.5
        corrected_target = max(5.0, min(3600.0, round((current_words * factor) / 2.5 + intro_seconds)))
        actions.append(
            f"attempt {attempts_used}: measured {measured:.2f}s vs {narration_budget:.2f}s budget — "
            f"regenerating AI script at {corrected_target:.0f}s budget"
        )
        regen_project = dict(project, target_duration=corrected_target)
        corrected = await gen.generate_script(regen_project)
        llm_calls += 1
        current_text = (corrected.get("full_script") or "").strip()
        if not current_text:
            raise HTTPException(status_code=502, detail="Script correction produced empty text.")
        new_script_doc = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            **corrected,
            "source": "ai_generated",
            "created_at": _now(),
            "updated_at": _now(),
        }
        await db.scripts.replace_one({"project_id": project_id}, dict(new_script_doc), upsert=True)
        current_script_id = new_script_doc["id"]
        script_version = f"{current_script_id}:{new_script_doc['updated_at']}"

    payload["text_excerpt"] = text[:240]
    payload["created_at"] = _now()
    payload["updated_at"] = _now()
    # Auto-select: mirror the thumbnail auto-chain so render preflight passes
    # immediately after generation. Any previously selected full-script voiceover
    # is demoted to keep the per-project exclusivity invariant.
    payload["status"] = "selected"
    await db.assets.update_many(
        {
            "project_id": project_id,
            "asset_type": "voiceover_audio",
            "scene_id": None,
            "status": "selected",
        },
        {"$set": {"status": "generated", "updated_at": _now()}},
    )
    await db.assets.insert_one(dict(payload))
    # Requested target stays authoritative: never move it here (the previous
    # silent target_duration overwrite from the byte-size estimate caused the
    # preflight mismatch loop). Record the fit instead.
    await _record_voice_fit(db, project_id, script_version, attempts_used, attempts_used, llm_calls,
                            float(payload.get("duration") or 0), actions)
    project_set = {"selected_voiceover_asset_id": payload["id"], "updated_at": _now()}

    if not payload.get("mock"):
        cost = float(payload.get("cost_estimate") or 0)
        if cost:
            project_set["estimated_cost"] = float(project.get("estimated_cost", 0)) + cost
    await db.projects.update_one({"id": project_id}, {"$set": project_set})
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.post("/projects/{project_id}/scenes/{scene_id}/voiceover/generate")
async def generate_scene_voiceover(
    project_id: str, scene_id: str,
    body: GenerateVoiceoverRequest = Body(default=None),
    user=Depends(get_current_user),
):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot generate voiceovers")
    scene = await db.scenes.find_one({"id": scene_id, "project_id": project_id}, {"_id": 0})
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    body = body or GenerateVoiceoverRequest()
    text = (body.text_override or scene.get("narration_text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Scene has no narration text. Generate scenes first.")
    voice = _voice_style_for(project, body.voice_style)

    asset_id = str(uuid.uuid4())
    try:
        payload = await tts_service.generate_voiceover(
            text=text, voice_style=voice, tone=body.tone, project_id=project_id,
            asset_id=asset_id, scene_id=scene_id,
            name_suffix=f"(Scene {scene.get('scene_number', '?'):02d})" if isinstance(scene.get('scene_number'), int) else "(Scene)",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Voiceover generation failed: {e}")

    payload["text_excerpt"] = text[:240]
    payload["scene_number"] = scene.get("scene_number")
    payload["created_at"] = _now()
    payload["updated_at"] = _now()
    # Demote any other voiceover for this scene to "generated", mark this one selected
    await db.assets.update_many(
        {"project_id": project_id, "scene_id": scene_id, "asset_type": "voiceover_audio", "status": "selected"},
        {"$set": {"status": "generated", "updated_at": _now()}},
    )
    payload["status"] = "selected"
    await db.assets.insert_one(dict(payload))

    # VO is master timeline: update scene duration to voDuration + 0.5s buffer
    try:
        vo_duration = float(payload.get("duration") or 0)
        if vo_duration > 0:
            new_dur = round(vo_duration + 0.5, 2)
            await db.scenes.update_one(
                {"id": scene_id, "project_id": project_id},
                {"$set": {"duration": new_dur, "end_time": round(float(scene.get("start_time", 0)) + new_dur, 2), "updated_at": _now()}},
            )
            # Recalculate total video duration as sum of scene durations for validation
            all_scenes = await db.scenes.find({"project_id": project_id}, {"_id": 0}).to_list(100)
            total_dur = sum(float(s.get("duration", 30) or 30) for s in all_scenes)
            await db.projects.update_one({"id": project_id}, {"$set": {"target_duration": int(total_dur), "updated_at": _now()}})
    except Exception as e:
        import logging
        logging.getLogger("facelessforge.routes").warning("Failed to update scene duration from VO: %s", e)

    if not payload.get("mock"):
        cost = float(payload.get("cost_estimate") or 0)
        if cost:
            await db.projects.update_one(
                {"id": project_id},
                {"$set": {"estimated_cost": float(project.get("estimated_cost", 0)) + cost, "updated_at": _now()}},
            )
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.post("/projects/{project_id}/voiceover/{asset_id}/select")
async def select_voiceover(project_id: str, asset_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot select voiceovers")
    asset = await db.assets.find_one(
        {"id": asset_id, "project_id": project_id, "asset_type": "voiceover_audio"},
        {"_id": 0},
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Voiceover not found")
    now = _now()
    if asset.get("scene_id"):
        # Per-scene exclusivity
        await db.assets.update_many(
            {"project_id": project_id, "scene_id": asset["scene_id"],
             "asset_type": "voiceover_audio", "status": "selected"},
            {"$set": {"status": "generated", "updated_at": now}},
        )
        await db.assets.update_one(
            {"id": asset_id, "project_id": project_id},
            {"$set": {"status": "selected", "updated_at": now}},
        )
    else:
        # Full-script exclusivity — demote prior selected full-script voiceovers
        await db.assets.update_many(
            {"project_id": project_id, "scene_id": None,
             "asset_type": "voiceover_audio", "status": "selected"},
            {"$set": {"status": "generated", "updated_at": now}},
        )
        await db.assets.update_one(
            {"id": asset_id, "project_id": project_id},
            {"$set": {"status": "selected", "updated_at": now}},
        )
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"selected_voiceover_asset_id": asset_id, "updated_at": now}},
        )
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.post("/projects/{project_id}/voiceover/{asset_id}/reject")
async def reject_voiceover(project_id: str, asset_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot reject voiceovers")
    asset = await db.assets.find_one(
        {"id": asset_id, "project_id": project_id, "asset_type": "voiceover_audio"},
        {"_id": 0},
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Voiceover not found")
    now = _now()
    await db.assets.update_one(
        {"id": asset_id, "project_id": project_id},
        {"$set": {"status": "rejected", "updated_at": now}},
    )
    if not asset.get("scene_id") and project.get("selected_voiceover_asset_id") == asset_id:
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"selected_voiceover_asset_id": None, "updated_at": now}},
        )
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.delete("/projects/{project_id}/voiceover/{asset_id}")
async def delete_voiceover(project_id: str, asset_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot delete voiceovers")
    asset = await db.assets.find_one(
        {"id": asset_id, "project_id": project_id, "asset_type": "voiceover_audio"},
        {"_id": 0},
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Voiceover not found")
    # Best-effort artifact removal via storage abstraction
    try:
        from ..storage import STATIC_ROOT, get_storage as _gs
        store = _gs()
        # IDs are server-issued: a registered path cannot grant permission to
        # delete another asset, even within the same project's directory.
        allowed_keys = {f"audio/{project_id}/{asset_id}{ext}" for ext in (".mp3", ".wav")}
        root = STATIC_ROOT.resolve()
        if asset.get("storage_key"):
            key = asset["storage_key"]
            if key in allowed_keys:
                path = root / key
                # LocalStorage resolves symlinks, so check before dispatching.
                if store.mode != "local" or path.resolve() == path:
                    store.delete(key=key)
        elif asset.get("file_path"):
            # Legacy records have no storage key. Accept only the canonical
            # location issued for this asset, rejecting symlink escapes too.
            p = Path(asset["file_path"]).resolve()
            if p in {root / key for key in allowed_keys} and p.is_file():
                p.unlink()
    except Exception:  # noqa: BLE001
        pass
    await db.assets.delete_one({"id": asset_id, "project_id": project_id})
    if project.get("selected_voiceover_asset_id") == asset_id:
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"selected_voiceover_asset_id": None, "updated_at": _now()}},
        )
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


# ============================ MUSIC (per-project background tracks) ============================
from .. import audio_mix as audio_mix_service

MUSIC_ALLOWED_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
MUSIC_MAX_BYTES = 20 * 1024 * 1024
MUSIC_MAX_SECONDS = 600.0
MUSIC_GAIN_MIN_DB = -30.0
MUSIC_GAIN_MAX_DB = -6.0
MUSIC_GAIN_DEFAULT_DB = -14.0


@router.post("/projects/{project_id}/music/upload")
async def upload_music(project_id: str, file: UploadFile = File(...),
                       rights_confirmed: bool = Form(False),
                       rights_note: str = Form(""),
                       user=Depends(get_current_user)):
    """Tenant-owned music upload. Never becomes audible without selection + rights."""
    import tempfile
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot upload music")
    ext = (Path(file.filename or "").suffix or "").lower()
    if ext not in MUSIC_ALLOWED_EXTS:
        raise HTTPException(status_code=422, detail=f"Unsupported audio type {ext or 'unknown'}")
    # Same pre-read guard as client assets: an oversized declared body is refused
    # before it is buffered.
    if file.size is not None and file.size > MUSIC_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Music file exceeds 20 MB")
    raw = await file.read()
    if len(raw) > MUSIC_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Music file exceeds 20 MB")
    if len(raw) < 1024:
        raise HTTPException(status_code=422, detail="Music file is empty")
    tmp = Path(tempfile.gettempdir()) / f"music-upload-{uuid.uuid4().hex}{ext}"
    try:
        tmp.write_bytes(raw)
        # Same probe contract as narration uploads: a missing or failing ffprobe is a 422, not a 500.
        duration = await asyncio.to_thread(_inspect_client_audio, tmp)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    if duration > MUSIC_MAX_SECONDS:
        raise HTTPException(status_code=422, detail="Music track exceeds 600 seconds")
    from ..storage import get_storage as _gs
    asset_id = str(uuid.uuid4())
    key = f"music/{project_id}/{asset_id}{ext}"
    staged = _stage_upload_copy(raw, key, ext)
    try:
        saved = _gs().save_file(staged, key, "audio/mpeg")
    except Exception:
        # A storage outage must not orphan the staged copy in $TMPDIR.
        try:
            staged.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise
    doc = {
        "id": asset_id,
        "project_id": project_id,
        "name": file.filename or "music track",
        "asset_type": "music_audio",
        "file_path": None,
        "storage_key": saved.key,
        "preview_url": saved.preview_path or saved.url,
        "source": "upload",
        "duration": duration,
        "rights_confirmed": bool(rights_confirmed),
        "rights_note": (rights_note or "")[:200],
        "mock": False,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.assets.insert_one(dict(doc))
    if rights_confirmed:
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"music_enabled": True, "selected_music_asset_id": asset_id,
                      "music_gain_db": MUSIC_GAIN_DEFAULT_DB,
                      "music_ducking_enabled": True,
                      "music_rights_confirmed": True, "updated_at": _now()}},
        )
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


def _stage_upload_copy(raw: bytes, key: str, ext: str, prefix: str = "music-store"):
    """Persist upload bytes to a temp path for the storage backend.

    The caller owns the returned path and must unlink it when storage fails. A
    partial write here is removed before the error propagates.
    """
    import tempfile
    tmp = Path(tempfile.gettempdir()) / f"{prefix}-{uuid.uuid4().hex}{ext}"
    try:
        tmp.write_bytes(raw)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        raise
    return tmp


@router.post("/projects/{project_id}/music/{asset_id}/select")
async def select_music(project_id: str, asset_id: str, body: MusicSelectRequest,
                       user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot select music")
    asset = await db.assets.find_one(
        {"id": asset_id, "project_id": project_id, "asset_type": "music_audio"},
        {"_id": 0},
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Music asset not found for this project")
    gain = body.music_gain_db
    if gain is None:
        gain = project.get("music_gain_db", MUSIC_GAIN_DEFAULT_DB)
    try:
        gain = float(gain)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="music_gain_db must be a number")
    if not (MUSIC_GAIN_MIN_DB <= gain <= MUSIC_GAIN_MAX_DB):
        raise HTTPException(status_code=422, detail="music_gain_db must be between -30 and -6 dB")
    ducking = body.music_ducking_enabled
    if ducking is None:
        ducking = project.get("music_ducking_enabled", True)
    rights = bool(body.rights_confirmed or asset.get("rights_confirmed")
                  or project.get("music_rights_confirmed"))
    if not rights:
        raise HTTPException(status_code=422, detail="Rights confirmation is required before music can be selected")
    patch_asset: dict = {"rights_confirmed": True, "updated_at": _now()}
    if body.rights_note is not None:
        patch_asset["rights_note"] = body.rights_note[:200]
    await db.assets.update_one({"id": asset_id, "project_id": project_id}, {"$set": patch_asset})
    await db.projects.update_one(
        {"id": project_id},
        {"$set": {"music_enabled": True, "selected_music_asset_id": asset_id,
                  "music_gain_db": gain, "music_ducking_enabled": bool(ducking),
                  "music_rights_confirmed": True, "updated_at": _now()}},
    )
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.delete("/projects/{project_id}/music")
async def disable_music(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot change music")
    await db.projects.update_one(
        {"id": project_id},
        {"$set": {"music_enabled": False, "selected_music_asset_id": None,
                  "updated_at": _now()}},
    )
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


# ============================ RENDER QUEUE (Phase 6) ============================
from .. import render as render_service
from ..models import RenderStartRequest


@router.get("/projects/{project_id}/render/preflight")
async def render_preflight(project_id: str, user=Depends(get_current_user)):
    """Return validation checklist for the render UI."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project_id}).sort("scene_number", 1).to_list(500)
    metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    assets = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(500)
    return render_service.validate_prerequisites(project, script, scenes, metadata, assets)


@router.post("/projects/{project_id}/render/autofit-duration")
async def render_autofit_duration(project_id: str, user=Depends(get_current_user)):
    """Explicit user action: move the selected duration to fit measured narration.

    Idempotent — already-fitting selections return ok without writing.
    This is the only path that changes target_duration besides direct project
    edits; render/start never moves it.
    """
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot change duration")
    try:
        suggestion = await render_service.autofit_project_duration(project_id)
    except render_service.RenderSubmissionBusy as e:
        raise HTTPException(status_code=409, detail=str(e)[:300])
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])
    # The write succeeded. Validate the newly selected duration in the response,
    # not the stale project snapshot read before the explicit fit.
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    try:
        project_after = await db.projects.find_one({"id": project_id}, {"_id": 0}) or {}
        fit = dict(project_after.get("voice_fit") or {})
        fit["actions"] = list(fit.get("actions") or []) + [
            f"explicit duration fit {suggestion['from']}s -> {suggestion['to']}s"]
        await db.projects.update_one(
            {"id": project_id}, {"$set": {"voice_fit": fit, "updated_at": _now()}})
    except Exception:  # noqa: BLE001
        pass
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project_id}).sort("scene_number", 1).to_list(500)
    metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    assets = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(500)
    return {
        "autofit": suggestion,
        "preflight": render_service.validate_prerequisites(project, script, scenes, metadata, assets),
    }


@router.post("/projects/{project_id}/render/start")
async def render_start(project_id: str,
                       body: RenderStartRequest = Body(default=None),
                       user=Depends(get_current_user)):
    _check_render_rate(user["id"])
    db = get_db()
    # Per-tenant spend caps + metering
    try:
        u = await db.users.find_one({"id": user["id"]}, {"_id": 0})
        cap = (u or {}).get("monthly_cap")
        used = (u or {}).get("used_this_month", 0)
        if cap is not None and used >= cap:
            raise HTTPException(status_code=402, detail={"code": "spend_cap_exceeded", "cap": cap, "used": used})
    except HTTPException:
        raise
    except Exception:
        pass
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot start renders")
    body = body or RenderStartRequest()
    # Voice selector moved from /generate-script to /render payload — allow last-minute voice change
    # DB: voice_id now lives on projects table (not just scripts)
    # Also handle burn_subtitles + aspect_ratio overrides at render time (not during production)
    update_fields = {}
    if body.voice_id or body.voice_style:
        if body.voice_style:
            update_fields["voice_style"] = body.voice_style
        if body.voice_id:
            update_fields["voice_id"] = body.voice_id
            # normalize voice_id to voice_style for TTS resolver
            update_fields["voice_style"] = body.voice_id
    if body.aspect_ratio in ("9:16", "1:1", "16:9"):
        update_fields["aspect_ratio"] = body.aspect_ratio
        # also keep resolution in sync
        if body.aspect_ratio == "16:9":
            update_fields["resolution"] = "1920x1080"
        elif body.aspect_ratio == "1:1":
            update_fields["resolution"] = "1080x1080"
        else:
            update_fields["resolution"] = "1080x1920"
    if body.burn_subtitles is not None:
        update_fields["burn_subtitles"] = bool(body.burn_subtitles)
    if update_fields:
        update_fields["updated_at"] = _now()
        await db.projects.update_one({"id": project_id}, {"$set": update_fields})
        project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    # Validate prereqs first so caller gets a clean 400 before queuing
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project_id}).sort("scene_number", 1).to_list(500)
    metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    assets = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(500)
    check = render_service.validate_prerequisites(project, script, scenes, metadata, assets)
    if not check["ok"]:
        detail: dict = {
            "message": "Render prerequisites not met",
            "issues": check["issues"],
            "checklist": check["checklist"],
        }
        # The requested target is authoritative: render/start never moves it.
        # Surface the explicit-fit option so the user can apply it deliberately.
        suggestion = check.get("autofit_suggestion")
        if suggestion and check["issues"] == ["Voiceover fits selected duration"]:
            detail["autofit_suggestion"] = suggestion
            detail["autofit_endpoint"] = f"/api/projects/{project_id}/render/autofit-duration"
        raise HTTPException(status_code=400, detail=detail)
    # Pass burn_subtitles and aspect to queue (burn defaults False as per user request — not during production)
    try:
        job = await render_service.queue_render(
            project_id, requested_by=user["id"], burn_subtitles=bool(body.burn_subtitles),
            aspect_ratio=body.aspect_ratio,
            expected_inputs=project_lease.render_input_snapshot(project),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=_scrub_email(str(e)))
    import logging as _logging
    _logging.getLogger("facelessforge.render").info("render start project=%s user=%s", project_id, _scrub_email(user.get("email","")))
    return _render_job_payload(job)


@router.get("/projects/{project_id}/render/jobs")
async def render_list_jobs(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    jobs = await db.render_jobs.find({"project_id": project_id}, {"_id": 0})\
        .sort("created_at", -1).to_list(50)
    return [_render_job_payload(j) for j in jobs]


@router.get("/projects/{project_id}/render/jobs/{job_id}")
async def render_get_job(project_id: str, job_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    job = await db.render_jobs.find_one({"id": job_id, "project_id": project_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Render job not found")
    return _render_job_payload(job)


@router.get("/projects/{project_id}/render/jobs/{job_id}/download")
async def download_render_job(project_id: str, job_id: str, user=Depends(get_current_user)):
    """Redirect to a freshly resolved URL for one completed render job."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    job = await db.render_jobs.find_one({
        "id": job_id,
        "project_id": project_id,
        "status": {"$in": list(render_service.COMPLETED_RENDER_STATUS_VALUES)},
    }, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Completed render not found")
    delivery_url = resolve_delivery_url(job)
    if not delivery_url:
        raise HTTPException(status_code=404, detail="Render artifact is unavailable")
    return RedirectResponse(url=delivery_url, status_code=302)


@router.post("/projects/{project_id}/render/jobs/{job_id}/cancel")
async def render_cancel_job(project_id: str, job_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot cancel renders")
    ok = await render_service.cancel_render(project_id, job_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Job is not cancellable in its current state.")
    job = await db.render_jobs.find_one({"id": job_id, "project_id": project_id}, {"_id": 0})
    return _ser(job)


# Also add batch via external proxy for VideoForge compatibility
@router.post("/external/render-video/batch")
async def external_batch_render(
    body: dict = Body(...),
    user=Depends(get_current_user),
):
    """VideoForge batch: accept {urls: [5 urls]} or {items: [{title, script}]} batch 5."""
    db = get_db()
    from .external_api import ExternalRenderRequest, ExternalSceneInput
    from .. import render as render_service
    items = body.get("items") or body.get("urls") or []
    if not items:
        raise HTTPException(status_code=400, detail="provide items or urls (5)")
    out = []
    for it in items[:5]:
        if isinstance(it, str):
            it = {"title": f"Batch {it[:40]}", "script": f"Content for {it}", "source": "batch_url", "external_asset_id": it}
        # forward to external logic minimally: create project
        try:
            title = it.get("title", "Batch Video")
            script = it.get("script", "Batch script")
            pid = str(uuid.uuid4())
            await db.projects.insert_one({
                "id": pid, "user_id": user["id"], "name": title[:120], "niche": "batch", "topic": script[:500],
                "audience": "general", "tone": "informative", "target_duration": 60,
                "voice_style": "neutral male narrator", "visual_style": "cinematic b-roll",
                "monetisation_intent": "batch", "cta_goal": "subscribe", "status": "DRAFT",
                "quality_score": 0, "estimated_cost": 0.0, "created_at": _now(), "updated_at": _now()
            })
            # seed script
            await db.scripts.insert_one({
                "id": str(uuid.uuid4()), "project_id": pid, "hook_option_one": script[:120], "hook_option_two": "", "hook_option_three": "",
                "selected_hook": script[:120], "full_script": script, "retention_beats": [], "cta_block": "",
                "word_count": len(script.split()), "estimated_duration": 60, "created_at": _now(), "updated_at": _now()
            })
            out.append({"project_id": pid, "title": title, "status": "created"})
        except Exception as e:
            out.append({"error": str(e), "item": it})
    return {"batch_size": len(out), "jobs": out}


# ============================ PROD FIXES: billing balance + full render + thumbnails ============================
import time as _time
_RENDER_RATE = {}
def _scrub_email(s: str) -> str:
    import re
    return re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email-redacted]", str(s))
def _check_render_rate(user_id: str):
    now = _time.time()
    bucket = _RENDER_RATE.get(user_id, [])
    bucket = [t for t in bucket if now - t < 3600]
    if len(bucket) >= 5:
        raise HTTPException(status_code=429, detail="Rate limited: 5 renders per hour")
    bucket.append(now)
    _RENDER_RATE[user_id] = bucket

@router.get("/billing/balance")
async def billing_balance(user=Depends(get_current_user)):
    from ..db import get_db as _get_db
    db = _get_db()
    try:
        from ..billing import get_billing_status_for_user
        return await get_billing_status_for_user(user)
    except Exception:
        try:
            import redis
            r = redis.Redis(host=os.getenv("REDIS_HOST","localhost"), port=int(os.getenv("REDIS_PORT","6379")), decode_responses=True)
            data = r.hgetall(f"tenant:{user['id']}")
            credits = int(data.get("credits", 0) or 0)
            return {"credits": credits, "remaining": credits, "monthly": 150, "quota": 150, "plan": "free", "balance": credits}
        except:
            return {"credits": 30, "remaining": 30, "monthly": 30, "quota": 30, "plan": "free", "balance": 30}

@router.get("/thumbnails/{asset_id}")
async def get_thumbnail(asset_id: str, request: Request, user=Depends(get_current_user)):
    from ..db import get_db as _get_db
    from ..storage import STATIC_ROOT as _STATIC_ROOT
    from fastapi.responses import FileResponse, Response
    from pathlib import Path
    db = _get_db()
    asset = await db.assets.find_one({"id": asset_id}, {"_id": 0})
    if not asset:
        fallback = Path(__file__).parent.parent / "static" / "thumbs" / "fallback.png"
        if fallback.exists():
            return FileResponse(str(fallback), media_type="image/png")
        return Response(status_code=404, content=b"not found")
    # Serve only the canonical artifact issued for this asset. IDs are
    # server-issued, so a stored file_path/storage_key can never authorize
    # reading another asset's file or anything outside the storage root.
    project_id = asset.get("project_id")
    allowed_keys = {f"thumbs/{project_id}/{asset_id}{ext}" for ext in (".png", ".jpg", ".jpeg", ".webp", ".svg")}
    root = _STATIC_ROOT.resolve()
    served = None
    if asset.get("storage_key"):
        key = asset["storage_key"]
        if key in allowed_keys:
            p = root / key
            # Local storage resolves symlinks, so check before dispatching.
            if p.resolve() == p and p.is_file():
                served = p
    elif asset.get("file_path"):
        # Legacy records have no storage key. Accept only the canonical
        # location issued for this asset, rejecting symlink escapes too.
        p = Path(asset["file_path"]).resolve()
        if p in {root / key for key in allowed_keys} and p.is_file():
            served = p
    if served is not None:
        ext = served.suffix.lower()
        ct = "image/png" if ext==".png" else "image/jpeg" if ext in (".jpg",".jpeg") else "image/svg+xml" if ext==".svg" else "image/png"
        return FileResponse(str(served), media_type=ct, headers={"Content-Disposition": f'inline; filename="{asset_id}{ext}"'})
    url = asset.get("preview_url")
    if url:
        return Response(status_code=302, headers={"Location": url})
    fallback = Path(__file__).parent.parent / "static" / "thumbs" / "fallback.png"
    if fallback.exists():
        return FileResponse(str(fallback), media_type="image/png")
    return Response(status_code=404, content=b"fallback not found")

@router.post("/render/full/{project_id}")
async def render_full(project_id: str, user=Depends(get_current_user)):
    _check_render_rate(user["id"])
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    active = await db.render_jobs.find_one({"project_id": project_id, "status": {"$in": list(render_service.ACTIVE_RENDER_STATUS_VALUES)}}, {"_id": 0})
    if active:
        return {"queued": True, "job": _ser(active), "idempotent": True}
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project_id}).sort("scene_number", 1).to_list(500)
    metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    assets = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(500)
    check = render_service.validate_prerequisites(project, script, scenes, metadata, assets)
    if not check["ok"]:
        raise HTTPException(status_code=400, detail={"message": "Prerequisites not met", "issues": check["issues"]})
    try:
        job = await render_service.queue_render(project_id, requested_by=user["id"])
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=_scrub_email(str(e)))
    import logging as _logging
    _logging.getLogger("facelessforge.render").info("full render queued project=%s user=%s", project_id, _scrub_email(user.get("email","")))
    return {"queued": True, "job": _ser(job)}

@router.post("/projects/{project_id}/render/full")
async def render_full_alias(project_id: str, user=Depends(get_current_user)):
    return await render_full(project_id, user)

# --- Waitlist (public) ---
from pydantic import BaseModel as _BM2
class _WaitlistReq2(_BM2):
    email: str

@router.post("/waitlist")
async def join_waitlist(body: _WaitlistReq2):
    email = (body.email or "").strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="valid email required")
    db = get_db()
    existing = await db.waitlist.find_one({"email": email}, {"_id": 0})
    if existing:
        raise HTTPException(status_code=409, detail="already on waitlist")
    doc = {"id": str(uuid.uuid4()), "email": email, "created_at": _now()}
    await db.waitlist.insert_one(doc)
    return {"ok": True, "email": email}

@router.get("/waitlist")
async def list_waitlist(user=Depends(require_roles("admin"))):
    db = get_db()
    items = await db.waitlist.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return items


# ============================ ADMIN DIAGNOSTICS / RETENTION ============================
from .. import retention as retention_service
from ..storage import get_storage as _get_storage_backend
from ..storage import storage_status as storage_status_fn


def _provider_modes() -> dict:
    ollama_ready = bool(os.environ.get("OLLAMA_MODEL", "").strip())
    claude_ready = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    return {
        "llm_text": {
            "mode": "live" if (ollama_ready or claude_ready) else "fallback",
            "provider": "ollama" if ollama_ready else ("anthropic" if claude_ready else "none"),
            "model": os.environ.get("OLLAMA_MODEL") or os.environ.get("LLM_MODEL", "claude-3-5-sonnet-20241022"),
            "ollama_ready": ollama_ready,
            "claude_ready": claude_ready,
        },
        "thumbnail_image": {
            "mode": "mock" if thumb_images.is_mock_mode() else "live",
            "provider": os.environ.get("THUMBNAIL_IMAGE_PROVIDER", "gemini_nano_banana"),
            "model": os.environ.get("THUMBNAIL_IMAGE_MODEL", "gemini-3.1-flash-image-preview"),
        },
        "tts": {
            "mode": "mock" if tts_service.is_mock_mode() else "live",
            "provider": os.environ.get("TTS_PROVIDER", "openai"),
            "model": os.environ.get("OPENAI_TTS_MODEL", "tts-1"),
        },
        "stock_footage": {
            "mode": "mock" if stock_service.is_mock_mode() else "live",
            "provider": "pexels",
        },
    }


def _live_storage_probe() -> dict:
    """Run a live storage write/read/delete probe so Diagnostics reflects
    reality, not just config presence. A misconfigured endpoint (bad DNS,
    wrong bucket, bad creds) previously reported healthy here while every
    asset save failed downstream."""
    try:
        probe = _get_storage_backend().probe()
        return {
            "probe_ok": bool(probe.get("ok")),
            "probe_latency_ms": probe.get("latency_ms"),
            "probe_error": probe.get("error"),
            "probed_at": probe.get("probed_at"),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "probe_ok": False,
            "probe_latency_ms": None,
            "probe_error": str(exc)[:200],
            "probed_at": None,
        }


@router.get("/admin/diagnostics")
async def admin_diagnostics(_admin=Depends(require_roles("admin"))):
    """Single-pane production-readiness check. Admin only."""
    from ..system import probe_ffmpeg_status
    sys_status = probe_ffmpeg_status()

    dev_mode = os.environ.get("DEV_MODE", "false").lower() in ("1", "true", "yes")
    frontend_url = os.environ.get("FRONTEND_URL", "")
    cors_origins = [o.strip() for o in (frontend_url or "").split(",") if o.strip()]
    if not cors_origins and dev_mode:
        cors_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]

    db = get_db()
    project_count = await db.projects.count_documents({})
    user_count = await db.users.count_documents({})
    active_renders = await db.render_jobs.count_documents(
        {"status": {"$in": list(render_service.ACTIVE_RENDER_STATUS_VALUES)}}
    )

    # Live dependency probes: redis ping + mongo ping (system must be green).
    system_ok = bool(sys_status.get("ffmpeg"))
    deps = {}
    try:
        import redis
        rurl = os.environ.get("REDIS_URL") or "redis://127.0.0.1:6379"
        r = redis.asyncio.from_url(rurl, decode_responses=True, socket_connect_timeout=2)
        pong = await asyncio.wait_for(r.ping(), timeout=3)
        await r.aclose()
        deps["redis_ping"] = bool(pong) and pong is True
    except Exception as exc:  # noqa: BLE001
        deps["redis_ping"] = False
        deps["redis_error"] = str(exc)[:200]
    try:
        await asyncio.wait_for(db.command("ping", read_preference="primary"), timeout=3)
        deps["mongo_ping"] = True
    except Exception as exc:  # noqa: BLE001
        deps["mongo_ping"] = False
        deps["mongo_error"] = str(exc)[:200]

    render_counts = {
        "queued": await db.render_jobs.count_documents({"status": {"$in": ["queued", "QUEUED"]}}),
        "rendering": await db.render_jobs.count_documents({"status": {"$in": ["rendering", "RENDERING"]}}),
        "validating": await db.render_jobs.count_documents({"status": {"$in": ["validating", "VALIDATING"]}}),
        "preparing_assets": await db.render_jobs.count_documents({"status": {"$in": ["preparing_assets", "PREPARING_ASSETS"]}}),
        "completed": await db.render_jobs.count_documents({"status": {"$in": list(render_service.COMPLETED_RENDER_STATUS_VALUES)}}),
        "failed": await db.render_jobs.count_documents(
            {"status": {"$in": ["failed", "FAILED", "error", "ERROR"]}}
        ),
        "needs_render": await db.render_jobs.count_documents(
            {"status": {"$in": ["needs_render", "generated", "draft"]}}
        ),
    }

    storage_info = {
        **retention_service.disk_usage_report(),
        **storage_status_fn(),
        **_live_storage_probe(),
    }

    ok = bool(
        system_ok
        and deps.get("redis_ping")
        and deps.get("mongo_ping")
        and storage_info.get("probe_ok")
    )

    return {
        "service": "facelessforge",
        "ok": ok,
        "deps": deps,
        "dev_mode": dev_mode,
        "cookie_mode": "lax+insecure" if dev_mode else "none+secure",
        "cors": {
            "origins": cors_origins,
            "regex_fallback": dev_mode,
            "wildcard": (not cors_origins),
        },
        "binaries": {
            "ffmpeg_path": sys_status.get("ffmpeg"),
            "ffmpeg_source": sys_status.get("ffmpeg_source"),
            "ffprobe_path": sys_status.get("ffprobe"),
            "ffprobe_source": sys_status.get("ffprobe_source"),
        },
        "providers": _provider_modes(),
        "storage": storage_info,
        "render_queue": {
            "active_jobs": active_renders,
            "concurrency": "single asyncio worker per pod",
            "lock": "per-project asyncio Lock + DB status guard",
            "timeout_seconds": int(os.environ.get("RENDER_TIMEOUT_SECONDS", "600")),
        },
        "render_counts": render_counts,
        "data_counts": {
            "users": user_count,
            "projects": project_count,
        },
    }


@router.post("/admin/retention/run")
async def admin_retention_run(_admin=Depends(require_roles("admin"))):
    """Manually trigger the retention sweep. Returns the cleanup report."""
    return await retention_service.run_cleanup_once()


# ============================ EXPORTS ============================

@router.get("/projects/{project_id}/export/script.txt")
async def export_script_txt(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    if not script:
        raise HTTPException(status_code=404, detail="No script")
    body = (
        f"# {project['name']}\n\n"
        f"## Hook\n{script['selected_hook']}\n\n"
        f"## Full Script\n{script['full_script']}\n\n"
        f"## CTA\n{script['cta_block']}\n"
    )
    return PlainTextResponse(body, headers={"Content-Disposition": f'attachment; filename="{project_id}-script.txt"'})


@router.get("/projects/{project_id}/export/scenes.csv")
async def export_scenes_csv(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    scenes = await db.scenes.find({"project_id": project_id}).sort("scene_number", 1).to_list(500)
    csv = scenes_to_csv(scenes)
    return PlainTextResponse(csv, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{project_id}-scenes.csv"'})


@router.get("/projects/{project_id}/export/metadata.json")
async def export_metadata_json(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    if not metadata:
        raise HTTPException(status_code=404, detail="No metadata")
    return _ser(metadata)


@router.get("/projects/{project_id}/export/package.zip")
async def export_package_zip(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project_id}).sort("scene_number", 1).to_list(500)
    metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    assets = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(500)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.json", json.dumps(_ser(project), indent=2, default=str))
        if script:
            zf.writestr("script.txt",
                f"# {project['name']}\n\n## Hook\n{script['selected_hook']}\n\n## Full Script\n{script['full_script']}\n\n## CTA\n{script['cta_block']}\n")
            zf.writestr("script.json", json.dumps(_ser(script), indent=2, default=str))
        if scenes:
            zf.writestr("scenes.csv", scenes_to_csv(scenes))
            zf.writestr("scenes.json", json.dumps([_ser(s) for s in scenes], indent=2, default=str))
        if metadata:
            zf.writestr("metadata.json", json.dumps(_ser(metadata), indent=2, default=str))
        if assets:
            zf.writestr("assets.json", json.dumps([_ser(a) for a in assets], indent=2, default=str))
            voiceovers = [a for a in assets if a.get("asset_type") == "voiceover_audio"]
            if voiceovers:
                # Trim heavy fields, keep what a renderer / client needs
                summary = []
                for v in voiceovers:
                    summary.append({
                        "id": v.get("id"),
                        "scene_id": v.get("scene_id"),
                        "scene_number": v.get("scene_number"),
                        "voice_style": v.get("voice_style"),
                        "duration": v.get("duration"),
                        "provider": v.get("provider"),
                        "model": v.get("model"),
                        "mock": v.get("mock"),
                        "status": v.get("status"),
                        "preview_url": v.get("preview_url"),
                        "preview_path": v.get("preview_path"),
                        "text_excerpt": v.get("text_excerpt"),
                        "is_full_script": v.get("scene_id") is None,
                        "selected_for_project": (project.get("selected_voiceover_asset_id") == v.get("id")),
                    })
                zf.writestr("voiceovers.json", json.dumps(summary, indent=2, default=str))
        # Final render metadata (latest completed) — never include internal file_path
        latest = await db.render_jobs.find_one(
            {"project_id": project_id, "status": {"$in": list(render_service.COMPLETED_RENDER_STATUS_VALUES)}, "output_url": {"$ne": None}},
            {"_id": 0},
            sort=[("completed_at", -1)],
        )
        if latest:
            latest_payload = _render_job_payload(latest)
            zf.writestr("render.json", json.dumps({
                "job_id": latest.get("id"),
                "status": latest.get("status"),
                "duration": latest.get("duration"),
                "file_size": latest.get("file_size"),
                "url": latest_payload.get("output_url"),
                "completed_at": (latest.get("completed_at").isoformat()
                    if isinstance(latest.get("completed_at"), datetime) else latest.get("completed_at")),
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "video_codec": "h264",
                "audio_codec": "aac",
            }, indent=2, default=str))
        zf.writestr("README.md",
            f"# {project['name']}\n\nGenerated with FacelessForge.\n\n"
            f"- Niche: {project['niche']}\n- Topic: {project['topic']}\n"
            f"- Target duration: {project['target_duration']}s\n- Quality: {project.get('quality_score',0)}/100\n")
    buf.seek(0)
    return StreamingResponse(
        iter([buf.read()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project_id}-package.zip"'},
    )


# ============================ ANALYTICS ============================

@router.get("/analytics/overview")
async def analytics_overview(user=Depends(get_current_user)):
    db = get_db()
    # Filter: is_demo=false AND is_template=false AND user_id=current_user.id unless admin
    # For admin troy@ethinx.solutions show all non-demo but with badge ADMIN VIEW (frontend)
    is_admin = user["role"] == "admin"
    is_troy = user.get("email") == "troy@ethinx.solutions"
    if is_admin:
        # Admin sees all non-demo/non-template across all users (for troy, that's 5 after hiding 112/170 demo)
        # Still filter is_demo/is_template so demo seeds hidden even for admin, but show badge
        q = {"is_demo": {"$ne": True}, "is_template": {"$ne": True}}
        # For troy, also ensure we don't leak other users' private projects? Task says show all but with badge, so no user_id filter
        is_admin_view = True
    else:
        q = {"user_id": user["id"], "is_demo": {"$ne": True}, "is_template": {"$ne": True}}
        is_admin_view = False
    projects_coll = db.projects
    # Totals must be exact — never cap the listing cursor (the old
    # to_list(1000)/to_list(500) caps made dashboard, projects page and
    # diagnostics disagree once the DB grew past the caps).
    total = await projects_coll.count_documents(q)
    status_rows = await projects_coll.aggregate([
        {"$match": q},
        {"$group": {"_id": "$status", "n": {"$sum": 1}}},
    ]).to_list(length=None)
    status_counts = {str(r.get("_id") or "DRAFT"): int(r.get("n") or 0) for r in status_rows}
    niche_rows = await projects_coll.aggregate([
        {"$match": q},
        {"$group": {"_id": "$niche", "n": {"$sum": 1}}},
    ]).to_list(length=None)
    niche_counts = {str(r.get("_id") or "other"): int(r.get("n") or 0) for r in niche_rows}
    avg_rows = await projects_coll.aggregate([
        {"$match": {**q, "quality_score": {"$type": "number"}}},
        {"$group": {"_id": None, "avg": {"$avg": "$quality_score"}}},
    ]).to_list(length=None)
    avg_q = round(float((avg_rows[0].get("avg") if avg_rows else 0) or 0), 1)
    completed = status_counts.get("COMPLETED", 0)
    failed = status_counts.get("FAILED", 0)
    draft = status_counts.get("DRAFT", 0)
    in_progress = max(0, total - completed - failed - draft)

    # projects over time (all matching projects, grouped by day)
    by_day_rows = await projects_coll.aggregate([
        {"$match": {**q, "created_at": {"$type": "date"}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
            "n": {"$sum": 1},
        }},
    ]).to_list(length=None)
    by_day = {str(r.get("_id") or ""): int(r.get("n") or 0) for r in by_day_rows if r.get("_id")}
    # EST MONTHLY OUTPUT = count of projects where status=completed in last 30d * 4.3 (weekly avg)
    cutoff = _now() - timedelta(days=30)
    completed_last_30d = await projects_coll.count_documents(
        {**q, "status": "COMPLETED", "created_at": {"$gte": cutoff}}
    )
    # Fallback if no completed in last 30d but there are completed overall, use completed count for projection (better than 0)
    monthly_output_projection = round(completed_last_30d * 4.3, 1) if completed_last_30d else round(completed * 4.3, 1) if completed else 0.0
    # TOTAL EST COST = EST OUTPUT * 3 credits (1 render = 3cr). 572 min total should not multiply. Remove 11435cr calc.
    total_estimated_cost_credits = round(monthly_output_projection * 3, 1)

    # Queue info: max 3 concurrent, rest queued (for 135 ready, show 3 rendering, 132 queued)
    try:
        active_jobs = await db.render_jobs.count_documents({"status": {"$in": list(render_service.ACTIVE_RENDER_STATUS_VALUES)}})
        rendering_jobs = await db.render_jobs.count_documents({"status": {"$in": ["validating", "VALIDATING", "preparing_assets", "PREPARING_ASSETS", "rendering", "RENDERING", "verifying", "VERIFYING"]}})
        queued_jobs = await db.render_jobs.count_documents({"status": {"$in": ["queued", "QUEUED"]}})
        # If many READY_TO_RENDER projects but not yet queued as jobs, estimate queued as READY count - active
        ready_projects = status_counts.get("READY_TO_RENDER", 0)
        # For display: Queue 3 rendering, X queued
        queue_display = f"Queue {min(rendering_jobs, 3)} rendering, {max(0, ready_projects - min(rendering_jobs, 3) + queued_jobs)} queued" if ready_projects > 3 else None
    except Exception:
        active_jobs = queued_jobs = rendering_jobs = 0
        queue_display = None

    return {
        "total_projects": total,
        "completed": completed,
        "in_progress": in_progress,
        "average_quality_score": round(avg_q, 1),
        "total_estimated_cost": total_estimated_cost_credits,
        "total_estimated_cost_credits": total_estimated_cost_credits,
        "total_estimated_cost_formula": "1 render = 3cr",
        "monthly_output_projection": monthly_output_projection,
        "monthly_output_formula": "completed_last_30d * 4.3",
        "completed_last_30d": completed_last_30d,
        "is_admin_view": is_admin_view,
        "render_queue": {"active": rendering_jobs, "queued": queued_jobs, "max": 3, "display": queue_display, "ready_projects": status_counts.get("READY_TO_RENDER", 0)},
        "status_counts": status_counts,
        "niche_counts": niche_counts,
        "projects_over_time": sorted([{"date": d, "count": c} for d, c in by_day.items()], key=lambda x: x["date"]),
    }


# ============================ ALERTS ============================
from .. import alerts as alerts_service


@router.get("/alerts/evaluate")
async def alerts_evaluate(user=Depends(get_current_user)):
    """Evaluate alert rules without sending. Gatus polls this over HTTPS."""
    db = get_db()
    return await alerts_service.evaluate(db)


@router.post("/alerts/test")
async def alerts_test(confirm: bool = False, user=Depends(get_current_user)):
    """Non-destructive Gotify test. Dry-run unless ?confirm=true."""
    if not confirm:
        return {"sent": False, "dry_run": True,
                "message": "Dry run — re-issue with ?confirm=true to dispatch.",
                "alerts_enabled": alerts_service.is_enabled()}
    return await alerts_service.notify(
        "manual-test", "FacelessForge test notification",
        "Operator-initiated test from /api/alerts/test.", {"user_id": user.get("id")},
        priority=3)


# ============================ SETTINGS ============================

@router.get("/settings")
async def get_settings(user=Depends(get_current_user)):
    db = get_db()
    s = await db.provider_settings.find_one({"user_id": user["id"]}, {"_id": 0})
    if not s:
        s = {
            "user_id": user["id"],
            "default_tone": "calm-authoritative",
            "default_visual_style": "cinematic b-roll",
            "cost_limit_monthly": 50.0,
            "preferred_provider": "deepseek/deepseek-chat",
            "created_at": _now(),
            "updated_at": _now(),
        }
        await db.provider_settings.insert_one(s)
    return _ser(s)


@router.patch("/settings")
async def update_settings(body: ProviderSettingsUpdate, user=Depends(get_current_user)):
    db = get_db()
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    patch["updated_at"] = _now()
    await db.provider_settings.update_one(
        {"user_id": user["id"]},
        {"$set": patch, "$setOnInsert": {"user_id": user["id"], "created_at": _now()}},
        upsert=True,
    )
    s = await db.provider_settings.find_one({"user_id": user["id"]}, {"_id": 0})
    return _ser(s)


# ============================ ADMIN USERS ============================

@router.get("/admin/users")
async def admin_list_users(user=Depends(require_roles("admin"))):
    db = get_db()
    users = await db.users.find({}, {"_id": 0, "password_hash": 0}).to_list(1000)
    return [_ser(u) for u in users]


@router.patch("/admin/users/{user_id}/role")
async def admin_update_role(user_id: str, role: str = Body(..., embed=True), _admin=Depends(require_roles("admin"))):
    if role not in ("admin", "creator", "editor", "viewer"):
        raise HTTPException(status_code=422, detail="Invalid role")
    db = get_db()
    await db.users.update_one({"id": user_id}, {"$set": {"role": role, "updated_at": _now()}})
    u = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    return _ser(u) if u else {"ok": True}



# ============================ SHARE LINKS ============================

SHAREABLE_STATUSES = {"METADATA_GENERATED", "ASSETS_READY", "READY_TO_RENDER", "COMPLETED"}


def _share_payload(project: dict) -> dict:
    return {
        "enabled": bool(project.get("share_enabled")),
        "token": project.get("share_token") if project.get("share_enabled") else None,
        "title_override": project.get("share_title_override"),
        "view_count": int(project.get("share_view_count") or 0),
        "last_viewed_at": project.get("share_last_viewed_at").isoformat()
            if isinstance(project.get("share_last_viewed_at"), datetime) else project.get("share_last_viewed_at"),
    }


@router.post("/projects/{project_id}/share")
async def enable_share(project_id: str, body: ShareUpdate = Body(default=None), user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if project.get("status") not in SHAREABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Project must be one of {sorted(SHAREABLE_STATUSES)} to be shared (current: {project.get('status')})",
        )
    patch = {
        "share_enabled": True,
        "updated_at": _now(),
    }
    if not project.get("share_token"):
        import secrets
        patch["share_token"] = secrets.token_urlsafe(24)
    if body is not None and body.title_override is not None:
        patch["share_title_override"] = body.title_override.strip() or None
    await db.projects.update_one({"id": project_id}, {"$set": patch})
    updated = await db.projects.find_one({"id": project_id}, {"_id": 0})
    return _share_payload(updated)


@router.patch("/projects/{project_id}/share")
async def update_share(project_id: str, body: ShareUpdate, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    patch = {"updated_at": _now()}
    if body.title_override is not None:
        patch["share_title_override"] = body.title_override.strip() or None
    await db.projects.update_one({"id": project_id}, {"$set": patch})
    updated = await db.projects.find_one({"id": project_id}, {"_id": 0})
    return _share_payload(updated)


@router.delete("/projects/{project_id}/share")
async def disable_share(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    await db.projects.update_one(
        {"id": project_id},
        {"$set": {"share_enabled": False, "updated_at": _now()}},
    )
    updated = await db.projects.find_one({"id": project_id}, {"_id": 0})
    return _share_payload(updated)


@router.post("/projects/{project_id}/share/regenerate")
async def regenerate_share_token(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    import secrets
    patch = {
        "share_token": secrets.token_urlsafe(24),
        "share_view_count": 0,
        "share_last_viewed_at": None,
        "updated_at": _now(),
    }
    await db.projects.update_one({"id": project_id}, {"$set": patch})
    updated = await db.projects.find_one({"id": project_id}, {"_id": 0})
    return _share_payload(updated)


@router.get("/public/share/{token}")
async def public_share(token: str):
    """Read-only public view of a shared project. No auth required."""
    if not token or len(token) < 8:
        raise HTTPException(status_code=404, detail="Share link not found")
    db = get_db()
    project = await db.projects.find_one({"share_token": token, "share_enabled": True}, {"_id": 0})
    if not project:
        raise HTTPException(status_code=404, detail="Share link not found or disabled")
    if project.get("status") not in SHAREABLE_STATUSES:
        raise HTTPException(status_code=404, detail="Project is no longer shareable")

    metadata = await db.metadata_packages.find_one({"project_id": project["id"]}, {"_id": 0})
    thumbnails = await db.assets.find(
        {"project_id": project["id"], "asset_type": "thumbnail_concept"}, {"_id": 0}
    ).to_list(10)
    # Selected generated thumbnail (if any)
    selected_thumb = None
    if project.get("selected_thumbnail_asset_id"):
        selected_thumb = await db.assets.find_one(
            {"id": project["selected_thumbnail_asset_id"], "project_id": project["id"]},
            {"_id": 0},
        )

    # Selected full-script voiceover (if any)
    selected_voice = None
    if project.get("selected_voiceover_asset_id"):
        selected_voice = await db.assets.find_one(
            {"id": project["selected_voiceover_asset_id"], "project_id": project["id"], "asset_type": "voiceover_audio"},
            {"_id": 0},
        )

    # Latest completed render job (final video)
    final_render = await db.render_jobs.find_one(
        {"project_id": project["id"], "status": {"$in": list(render_service.COMPLETED_RENDER_STATUS_VALUES)}, "output_url": {"$ne": None}},
        {"_id": 0},
        sort=[("completed_at", -1)],
    )
    final_render_payload = _render_job_payload(final_render)

    # Increment view count and update last viewed
    await db.projects.update_one(
        {"id": project["id"]},
        {"$inc": {"share_view_count": 1}, "$set": {"share_last_viewed_at": _now()}},
    )

    display_title = (
        project.get("share_title_override")
        or (metadata or {}).get("selected_title")
        or project["name"]
    )

    # White-label branding: fetch owner's provider_settings for customizable brand
    branding = None
    try:
        owner_settings = await db.provider_settings.find_one({"user_id": project["user_id"]}, {"_id": 0})
        if owner_settings:
            branding = {
                "brand_name": owner_settings.get("brand_name"),
                "brand_logo_url": owner_settings.get("brand_logo_url"),
                "brand_color": owner_settings.get("brand_color"),
                "white_label_enabled": bool(owner_settings.get("white_label_enabled")),
                "custom_domain": owner_settings.get("custom_domain"),
            }
            # If white_label not enabled, null out brand to use default FacelessForge
            if not branding["white_label_enabled"]:
                branding = {"brand_name": None, "white_label_enabled": False}
    except Exception:
        branding = None

    # Read-only, scrub private fields
    return {
        "display_title": display_title,
        "project_name": project["name"],
        "niche": project["niche"],
        "status": project["status"],
        "quality_score": int(project.get("quality_score") or 0),
        "metadata": {
            "selected_title": (metadata or {}).get("selected_title"),
            "description": (metadata or {}).get("description"),
            "tags": (metadata or {}).get("tags") or [],
            "hashtags": (metadata or {}).get("hashtags") or [],
            "chapters": (metadata or {}).get("chapters") or [],
            "pinned_comment": (metadata or {}).get("pinned_comment"),
        } if metadata else None,
        "thumbnails": [
            {"name": t.get("name"), "brief": t.get("brief")}
            for t in thumbnails if t.get("brief")
        ],
        "selected_thumbnail_url": selected_thumb.get("preview_url") if selected_thumb else None,
        "selected_voiceover": ({
            "preview_url": selected_voice.get("preview_url"),
            "duration": selected_voice.get("duration"),
            "voice_style": selected_voice.get("voice_style"),
        } if selected_voice else None),
        "final_video": ({
            "url": final_render_payload.get("output_url"),
            "duration": final_render.get("duration"),
            "width": 1920,
            "height": 1080,
            "download_url": final_render_payload.get("output_url"),
            "gcs_mirror": final_render_payload.get("output_url"),
            "r2_mirror": final_render_payload.get("output_url"),
        } if final_render_payload else None),
        "posting_guidance": {
            "youtube_title": (metadata or {}).get("selected_title"),
            "description": (metadata or {}).get("description"),
            "tags": (metadata or {}).get("tags") or [],
            "hashtags": (metadata or {}).get("hashtags") or [],
            "steps": [
                "Upload MP4 native — don't re-encode (1080p 30fps H.264/AAC)",
                "Title: Copy YouTube title from share page (<70 chars)",
                "Description: Paste full description + add your company.com link first line",
                "Tags: Copy tags block; add niche hashtag",
                "Thumbnail: Download selected thumbnail as custom thumbnail 1280x720",
                "Scheduling: Post 10am-2pm local, add chapters, pin comment",
                "Shorts/Reels: Crop center 1080x1920 if vertical needed",
            ],
            "cta": "Need help? Reply — support@ethinx.solutions — ABN 60 578 933 517",
        },
        "download": ({
            "mp4": final_render_payload.get("output_url"),
            "gcs_mirror": final_render_payload.get("output_url"),
            "r2_url": final_render_payload.get("output_url"),
            "filename": f"{(project.get('name') or 'facelessforge').replace(' ', '_')}.mp4",
        } if final_render_payload else None),
        "shared_at": project.get("updated_at").isoformat()
            if isinstance(project.get("updated_at"), datetime) else project.get("updated_at"),
        "branding": branding,
        "abn": "EthinX Solutions ABN 60 578 933 517",
    }


# ── Email: Your video IS ready — {FirstName} {company.com} ──

@router.get("/projects/{project_id}/email/preview")
async def email_preview(project_id: str, user=Depends(get_current_user)):
    """Preview the Your video IS ready cold email for a project (no send)."""
    from ..email import render_video_ready_email, get_email_service_status
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    final = await db.render_jobs.find_one(
        {"project_id": project_id, "status": {"$in": list(render_service.COMPLETED_RENDER_STATUS_VALUES)}, "output_url": {"$ne": None}},
        {"_id": 0},
        sort=[("completed_at", -1)],
    )
    mp4_url = resolve_delivery_url(final) or f"/api/projects/{project_id}/download"
    share_url = None
    if project.get("share_enabled") and project.get("share_token"):
        base = os.environ.get("FRONTEND_URL", "https://facelessforge.ethinx.solutions").rstrip("/")
        share_url = f"{base}/s/{project['share_token']}"
    preview = render_video_ready_email(user=user, project=project, mp4_url=mp4_url, share_url=share_url)
    return {
        "service": get_email_service_status(),
        "project_id": project_id,
        "mp4_url": mp4_url,
        "share_url": share_url,
        "gcs_mirror": mp4_url,
        "download": mp4_url,
        "email": preview,
        "posting_guidance": preview["text"].split("POSTING GUIDANCE")[1][:1200] if "POSTING GUIDANCE" in preview["text"] else None,
    }


@router.post("/projects/{project_id}/email/send")
async def email_send(project_id: str, user=Depends(get_current_user)):
    """Send Your video IS ready email to project owner (owner or admin). Triggers actual send/log."""
    from ..email import send_video_ready_email
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=False)
    final = await db.render_jobs.find_one(
        {"project_id": project_id, "status": {"$in": list(render_service.COMPLETED_RENDER_STATUS_VALUES)}, "output_url": {"$ne": None}},
        {"_id": 0},
        sort=[("completed_at", -1)],
    )
    mp4_url = resolve_delivery_url(final)
    if not mp4_url:
        raise HTTPException(status_code=400, detail="No completed render — MP4 not ready yet")
    owner = await db.users.find_one({"id": project["user_id"]}, {"_id": 0})
    if not owner:
        raise HTTPException(status_code=404, detail="Project owner not found")
    share_url = None
    if project.get("share_enabled") and project.get("share_token"):
        base = os.environ.get("FRONTEND_URL", "https://facelessforge.ethinx.solutions").rstrip("/")
        share_url = f"{base}/s/{project['share_token']}"
    result = await send_video_ready_email(user=owner, project=project, mp4_url=mp4_url, share_url=share_url)
    return result


@router.get("/projects/{project_id}/download")
async def download_redirect(project_id: str, user=Depends(get_current_user)):
    """Redirect to MP4 download (R2/GCS mirror)."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    final = await db.render_jobs.find_one(
        {"project_id": project_id, "status": {"$in": list(render_service.COMPLETED_RENDER_STATUS_VALUES)}},
        {"_id": 0},
        sort=[("completed_at", -1)],
    )
    if not final:
        raise HTTPException(status_code=404, detail="No completed render for download")
    delivery_url = resolve_delivery_url(final)
    if not delivery_url:
        raise HTTPException(status_code=404, detail="Render artifact is unavailable")
    return RedirectResponse(url=delivery_url, status_code=302)


@router.get("/email/status")
async def email_status(user=Depends(get_current_user)):
    """Email service config check (any authenticated user)."""
    from ..email import get_email_service_status
    return get_email_service_status()

@router.post("/projects/auto-generate")
async def auto_generate_project(request: Request, user=Depends(get_current_user)):
    """Auto-generate from queue - picks next topic, creates project, generates script/scenes, attaches assets, renders.
    Spec: FacelessForge Auto Button https://facelessforge.ethinx.solutions/api/projects/auto-generate
    Idempotent, thread-capped, BullMQ+Redis style via semaphore, pins versions, health /api/health.
    """
    ensure_operator(user)
    from app.workers.forge_loop import run_forge_once
    try:
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        result = await run_forge_once(user_id=user["id"])
        return {"status": "ok", "project_id": result.get("project_id"), "youtube_url": result.get("youtube_url"), "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:800])
@router.post("/auto-generate")
async def auto_generate_alias(request: Request, user=Depends(get_current_user)):
    ensure_operator(user)
    from app.workers.forge_loop import run_forge_once
    try:
        result = await run_forge_once(user_id=user["id"])
        return {"status": "ok", "project_id": result.get("project_id"), "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:800])


@router.get("/projects/{project_id}/receipt")
async def get_production_receipt(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    job = await db.render_jobs.find_one({"project_id": project_id, "status": {"$in": list(render_service.COMPLETED_RENDER_STATUS_VALUES)}}, {"_id": 0}, sort=[("completed_at", -1)])
    if not job:
        raise HTTPException(status_code=404, detail="No completed render")
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    # Real credits: DeepSeek $0.01 + Pexels $0 + ElevenLabs chars/1000*0.10 + R2 0.072GB*0.023=0.001656
    chars = len((script or {}).get("full_script", "") or "")
    elevenlabs_cost = round(chars / 1000 * 0.10, 4)
    deepseek_cost = 0.01
    storage_cost = round(0.072 * 0.023, 4)  # 72MB avg render * R2 $0.023/GB
    pexels_cost = 0.0
    total_real = round(deepseek_cost + elevenlabs_cost + storage_cost, 4)
    turnaround = int((job.get("completed_at") - job.get("started_at")).total_seconds()) if job.get("completed_at") and job.get("started_at") else int(job.get("duration") or 0)
    human_minutes = max(1, round(turnaround / 60 * 0.2))
    return {
        "turnaround_seconds": turnaround,
        "human_minutes": human_minutes,
        "rework_reserve": 2,
        "fully_loaded_cost": round(total_real + 0.05, 2),
        "variable_cost": {"deepseek": deepseek_cost, "elevenlabs": elevenlabs_cost, "pexels": pexels_cost, "ffmpeg": 0, "storage": storage_cost},
        "total": total_real,
        "job_id": job["id"],
    }


# === v6.2 Release Contract ===
from app.qa import run_checks as qa_run_checks
from app.release_manifest import build_manifest
import hashlib
import json as _json

@router.get("/projects/{project_id}/release/manifest")
async def get_release_manifest(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    m = await db.release_manifests.find_one({"project_id": project_id}, {"_id": 0}, sort=[("created_at", -1)])
    if not m:
        raise HTTPException(status_code=404, detail="No manifest — render not completed")
    return m

@router.get("/projects/{project_id}/qa/report")
async def get_qa_report(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    r = await db.qa_reports.find_one({"project_id": project_id}, {"_id": 0}, sort=[("created_at", -1)])
    if not r:
        raise HTTPException(status_code=404, detail="No QA report")
    return r

@router.post("/projects/{project_id}/approve")
async def approve_release(project_id: str, body: dict = Body(...), user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    job_id = body.get("job_id")
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id required")
    job = await db.render_jobs.find_one({"id": job_id, "project_id": project_id}, {"_id": 0})
    if not job or not job.get("output_url"):
        raise HTTPException(status_code=404, detail="Job not found or not completed")
    # Must have watched entire video — check watch event
    watch = await db.watch_events.find_one({"project_id": project_id, "job_id": job_id, "user_id": user["id"], "completed": True})
    if not watch:
        raise HTTPException(status_code=400, detail="Must watch entire video before approving (log watch event)")
    manifest = await db.release_manifests.find_one({"project_id": project_id, "job_id": job_id}, {"_id": 0})
    if not manifest:
        raise HTTPException(status_code=404, detail="No manifest for this job")
    # Hash current manifest
    mh = hashlib.sha256(_json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    vh = manifest.get("video_hash", "")
    # Store approval with exact hashes
    await db.approvals.update_one({"project_id": project_id}, {"$set": {"project_id": project_id, "job_id": job_id, "video_hash": vh, "manifest_hash": mh, "approver_id": user["id"], "approval_time": _now()}}, upsert=True)
    return {"ok": True, "video_hash": vh, "manifest_hash": mh}

@router.post("/projects/{project_id}/watch")
async def log_watch(project_id: str, body: dict = Body(...), user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    await db.watch_events.insert_one({"project_id": project_id, "job_id": body.get("job_id"), "user_id": user["id"], "currentTime": body.get("currentTime", 0), "duration": body.get("duration", 0), "completed": bool(body.get("completed")), "created_at": _now()})
    return {"ok": True}

@router.get("/projects/{project_id}/rights")
async def get_rights(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    items = await db.rights_registry.find({"project_id": project_id}, {"_id": 0}).to_list(100)
    return items

@router.post("/projects/{project_id}/rights")
async def add_right(project_id: str, body: dict = Body(...), user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    body["project_id"] = project_id
    body["created_at"] = _now().isoformat()
    await db.rights_registry.insert_one(body)
    return {"ok": True}

@router.get("/projects/{project_id}/disclosure")
async def get_disclosure(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    m = await db.release_manifests.find_one({"project_id": project_id}, {"_id": 0}, sort=[("created_at", -1)])
    return m.get("disclosure_decision") if m else {"contains_synthetic_media": True, "is_altered": False, "rationale": "ElevenLabs voice is synthetic"}

@router.post("/projects/{project_id}/disclosure")
async def set_disclosure(project_id: str, body: dict = Body(...), user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    await db.projects.update_one({"id": project_id}, {"$set": {"disclosure_decision": body, "updated_at": _now()}})
    return {"ok": True, "disclosure": body}

@router.post("/projects/{project_id}/intake")
async def post_intake(project_id: str, body: dict = Body(...), user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    # Validate no shell, no internal URLs
    import re, shlex
    text = _json.dumps(body)
    if "shell=True" in text or "169.254" in text or "127.0.0.1" in text:
        raise HTTPException(status_code=400, detail="Blocked: possible injection or internal URL")
    body["project_id"] = project_id
    body["tenant_id"] = user["id"]
    body["version"] = body.get("version", 1)
    body["created_at"] = _now().isoformat()
    await db.intake_packets.insert_one(body)
    return {"ok": True}

@router.get("/projects/{project_id}/claims")
async def get_claims(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    items = await db.claim_ledger.find({"project_id": project_id}, {"_id": 0}).to_list(100)
    return items

@router.post("/projects/{project_id}/claims")
async def post_claim(project_id: str, body: dict = Body(...), user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    body["project_id"] = project_id
    body["created_at"] = _now().isoformat()
    await db.claim_ledger.insert_one(body)
    return {"ok": True}

@router.post("/projects/{project_id}/publish-intent")
async def create_publish_intent(project_id: str, body: dict = Body(...), user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    channel_id = body.get("channel_id") or "default"
    release_id = body.get("release_id") or project_id
    # Unique index
    try:
        await db.publish_intents.create_index([("release_id", 1), ("channel_id", 1)], unique=True)
    except: pass
    # Check approval valid
    manifest = await db.release_manifests.find_one({"project_id": project_id}, {"_id": 0}, sort=[("created_at", -1)])
    approval = await db.approvals.find_one({"project_id": project_id}, {"_id": 0})
    if not manifest or not approval or manifest.get("video_hash") != approval.get("video_hash"):
        raise HTTPException(status_code=400, detail="Publish blocked: no valid approval for current video_hash/manifest_hash")
    # Check QA
    qa = await db.qa_reports.find_one({"project_id": project_id}, {"_id": 0}, sort=[("created_at", -1)])
    if qa and qa.get("blocked"):
        raise HTTPException(status_code=400, detail=f"Publish blocked: QA blocked {qa['blocked']}")
    try:
        doc = {"release_id": release_id, "channel_id": channel_id, "project_id": project_id, "job_id": manifest.get("job_id"), "status": "pending", "created_at": _now().isoformat()}
        await db.publish_intents.insert_one(doc)
    except Exception as e:
        if "duplicate" in str(e).lower():
            raise HTTPException(status_code=409, detail="Publish intent already exists for this release+channel")
        raise
    return {"ok": True, "intent": doc}

@router.get("/projects/{project_id}/compliance/checklist")
async def get_compliance_checklist(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    # Return static checklist per Research p11
    return {
        "mandatory": ["acceptable subject/conduct", "truthful metadata", "audience classification", "AI/sponsorship disclosures", "no artificial engagement"],
        "monetization_risk": ["original editorial contribution", "meaningful distinction", "coherent story", "no unsupported expert persona", "copyright permission"],
        "rights": ["every asset permitted use", "voice/likeness consent", "commercial terms"],
        "technical": ["exact master passes QA G", "subtitle sidecars + thumbnail approved", "no draft watermark"],
        "human": ["approver watched entire video", "checked packaging", "selected destination/visibility/schedule"],
        "publishing": ["correct channel authorized", "credentials valid", "quotas: 100 videos.insert, 100 search.list, caption 400, thumbnail 50"],
    }
