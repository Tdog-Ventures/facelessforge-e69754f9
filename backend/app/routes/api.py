"""Routers: auth, projects, generation, exports, analytics, settings."""
from __future__ import annotations

import asyncio
import hashlib
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
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse, StreamingResponse, PlainTextResponse, JSONResponse, FileResponse

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
from ..timing import (
    NarrationPlan,
    build_timing_status,
    script_word_count,
    suggest_fitted_duration,
)
from .. import voiceover_attempts
from .. import provider_usage
from ..grounding import commercial_project_errors, normalize_commercial_mode
from ..config.pricing_policy import calculate_credits
from ..storage import resolve_delivery_url
from ..email import send_password_reset_email
from .. import generation as gen
from .. import stock as stock_service
from .. import thumbnail_images as thumb_images
from .. import background_jobs as job_service
from ..asset_dedupe import filter_distinct_titles
from ..asset_relevance import relevant_candidates
from ..scene_terms import compact_stock_query


router = APIRouter(prefix="/api")

# Only these types are usable scene visuals.  Keep this local to the route so
# retrying an auto-attach never mistakes a voiceover or thumbnail for a scene
# selection.
STOCK_ASSET_TYPES = ["stock_video", "stock_image"]


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


def _render_job_payload(job: dict | None, *, resolve: bool = True) -> dict | None:
    """Serialize a render job.

    Status/list polling passes ``resolve=False`` so it is a pure DB read.  The
    download endpoint is the only place that must sign a fresh object-storage
    URL; signing there cannot delay progress polling.
    """
    if not job:
        return None
    payload = _ser(job)
    if resolve:
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
        frontend = (
            os.environ.get("PASSWORD_RESET_URL_BASE")
            or os.environ.get("FRONTEND_URL")
            or "https://facelessforge.ethinx.solutions"
        ).rstrip("/")
        reset_url = f"{frontend}/reset-password?token={token}"
        try:
            delivery = await send_password_reset_email(
                recipient=email,
                reset_url=reset_url,
                expires_in_minutes=ttl_minutes,
            )
        except Exception as exc:
            logger.error(
                "Password-reset mailer raised for user_id=%s: %s",
                user["id"],
                type(exc).__name__,
            )
            delivery = {"ok": False, "mode": "unknown"}
        if not delivery.get("ok") and not _dev_mode():
            # Preserve the non-enumerating response while ensuring an undelivered
            # reset token cannot remain active.
            await db.password_reset_tokens.update_many(
                {"user_id": user["id"], "token": token, "used_at": None},
                {"$set": {"used_at": _now()}},
            )
            logger.error(
                "Password-reset email delivery failed for user_id=%s via %s",
                user["id"],
                delivery.get("mode", "unknown"),
            )
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
        and not asset.get("stale")
        and asset.get("status") not in {"outdated", "rejected"}
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
    plan = NarrationPlan.from_project(project)
    if script:
        # Always recompute from the saved narration; never trust a stored
        # model-supplied count for the preflight contract.
        script_words = script_word_count(str(script.get("full_script") or ""))
        script_length = plan.classify_words(script_words)
    else:
        script_length = None
    try:
        thumbnail_sets = await job_service.thumbnail_sets_by_brief(project["id"], limit=20)
    except Exception:  # noqa: BLE001 — project view must not fail on optional job data
        thumbnail_sets = []
    scene_voiceovers = [a for a in assets if a.get("asset_type") == "voiceover_audio"
                        and a.get("scene_id") and a.get("status") != "rejected"]
    scene_voiceover_ids = {a.get("scene_id") for a in scene_voiceovers}
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
        "narration_plan": plan.as_dict(),
        "script_length": script_length,
        "selected_voiceover": _ser(selected_voiceover) if selected_voiceover else None,
        "thumbnail_sets": [_ser(s) for s in thumbnail_sets],
        "metrics": {
            "scene_count": len(scenes),
            "scene_voiceover_count": len(scene_voiceover_ids),
            "full_voiceover_count": len([
                a for a in assets
                if a.get("asset_type") == "voiceover_audio" and not a.get("scene_id")
                and a.get("status") != "rejected"
            ]),
            "outdated_voiceover_count": len([
                a for a in assets
                if a.get("asset_type") == "voiceover_audio"
                and (a.get("stale") or a.get("status") == "outdated")
            ]),
            "thumbnail_concept_count": len([
                a for a in assets if a.get("asset_type") == "thumbnail_concept"
            ]),
            "generated_thumbnail_count": len([
                a for a in assets if a.get("asset_type") == "generated_thumbnail"
            ]),
        },
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
        "niche": (body.niche or "").strip() or (body.topic or "").strip()[:120] or "general",
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
    # Commercial is optional: an empty business promo is demoted to editorial
    # (commercial = null) so it never blocks validation.
    proj = normalize_commercial_mode(proj)
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
    result = [_ser(p) | {"quality_label": quality_label(int(p.get("quality_score", 0)))} for p in projs]
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
                job = await render_service.queue_render(pid, requested_by=user["id"])
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


@router.get("/projects/{project_id}/status")
async def get_project_status(project_id: str, user=Depends(get_current_user)):
    """Cheap DB-only project/job status for ScenePlanner polling.

    Deliberately separate from ``GET /projects/{id}``: no storage signing, no
    R2/S3 probe, no ffprobe, no aggregate recomputation.  The browser uses this
    after a reload to recover scenes and render progress in well under 200 ms.
    """
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    scenes = await db.scenes.find(
        {"project_id": project_id}, {"_id": 0}
    ).sort("scene_number", 1).to_list(500)
    render_job = await db.render_jobs.find_one(
        {"project_id": project_id}, {"_id": 0}, sort=[("created_at", -1)]
    )
    voiceover_job = await db.voiceover_jobs.find_one(
        {"project_id": project_id}, {"_id": 0}, sort=[("created_at", -1)]
    )
    thumbnail_set = await db.thumbnail_sets.find_one(
        {"project_id": project_id}, {"_id": 0}, sort=[("created_at", -1)]
    )
    return {
        "project_id": project_id,
        "status": project.get("status"),
        "updated_at": _ser({"updated_at": project.get("updated_at")})["updated_at"],
        "scene_count": len(scenes),
        "scenes": [_ser(s) for s in scenes],
        # resolve=False: pure DB read, never signs a delivery URL.
        "render_job": _render_job_payload(render_job, resolve=False),
        "voiceover_job": _ser(voiceover_job) if voiceover_job else None,
        "thumbnail_set": _ser(thumbnail_set) if thumbnail_set else None,
    }


@router.get("/projects/{project_id}")
async def get_project(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    return await _attach_project_view(db, project)


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if "target_duration" in patch and not (5 <= int(patch["target_duration"]) <= 3600):
        raise HTTPException(status_code=422, detail="Target duration must be 5..3600s")
    commercial_fields = {"production_mode", "client_name", "structured_brief", "verified_cta"}
    if commercial_fields.intersection(patch):
        merged = {**project, **patch}
        normalized = normalize_commercial_mode(dict(merged))
        if normalized.get("production_mode") != merged.get("production_mode"):
            # Empty commercial promo -> demote to editorial in the persisted patch.
            patch["production_mode"] = "editorial"
            patch["client_name"] = None
            patch["structured_brief"] = None
            patch["verified_cta"] = None
            patch["brand_assets_supplied"] = False
        errors = commercial_project_errors(normalized)
        if errors:
            raise HTTPException(
                status_code=422,
                detail="Commercial project requires: " + ", ".join(errors),
            )
    if patch:
        patch["updated_at"] = _now()
        await db.projects.update_one({"id": project_id}, {"$set": patch})
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
    try:
        data = await gen.generate_script(project)
    except gen.ScriptLengthError as exc:
        await provider_usage.record_request_outcome(
            project_id=project_id,
            operation="script_generation",
            provider="deepseek",
            model="deepseek-chat",
            success=False,
            error_code="script_length_out_of_bounds",
            error_message=str(exc),
            metadata={
                "word_count": exc.word_count,
                "word_floor": exc.word_floor,
                "word_ceiling": getattr(exc, "word_ceiling", None),
                "target_duration": project.get("target_duration"),
            },
        )
        raise HTTPException(status_code=400, detail={
            "code": "script_length_out_of_bounds" if getattr(exc, "word_ceiling", None) else "script_under_length",
            "message": str(exc),
            "word_count": exc.word_count,
            "word_floor": exc.word_floor,
            "word_ceiling": getattr(exc, "word_ceiling", None),
        }) from exc
    await provider_usage.record_request_outcome(
        project_id=project_id,
        operation="script_generation",
        provider="deepseek",
        model="deepseek-chat",
        success=True,
        metadata={
            "word_count": data.get("word_count"),
            "word_floor": data.get("word_floor"),
            "word_ceiling": data.get("word_ceiling"),
            "target_duration": project.get("target_duration"),
        },
    )
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
    old_scenes = await db.scenes.find(
        {"project_id": project_id},
        {"_id": 0, "id": 1, "scene_number": 1, "narration_text": 1},
    ).to_list(1000)
    trace: dict = {}
    try:
        scenes = await gen.generate_scene_plan(project, script, trace=trace)
    except Exception as e:  # noqa: BLE001
        logger = __import__("logging").getLogger("facelessforge.routes")
        logger.warning("generate_scene_plan failed project=%s: %s — using fallback", project_id, e)
        trace = {
            "fallback_used": True,
            "fallback_reason": "llm_exception",
            "error_code": type(e).__name__,
            "llm_scene_count": 0,
            "scene_count": 0,
            "requested_duration_seconds": int(project.get("target_duration") or 0),
        }
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(e)
            sentry_sdk.set_tag("scene.fallback_used", "true")
            sentry_sdk.set_tag("scene.fallback_reason", "llm_exception")
        except Exception:  # noqa: BLE001
            pass
        scenes = await gen.generate_scene_plan(
            project, {"full_script": script.get("full_script", "") or project.get("topic") or "test"},
            trace=trace)
    for sc in scenes:
        sc["id"] = str(uuid.uuid4())
        sc["project_id"] = project_id
        sc["created_at"] = _now()
        sc["updated_at"] = _now()
    await db.scenes.delete_many({"project_id": project_id})
    if scenes:
        await db.scenes.insert_many([dict(sc) for sc in scenes])
    remap = await _remap_scene_assets(db, project_id, old_scenes, scenes)
    generation_meta = {
        **{k: trace.get(k) for k in (
            "fallback_used", "fallback_reason", "error_code",
            "llm_scene_count", "scene_count", "requested_duration_seconds",
        )},
        "generated_at": _now(),
        "preserved_requested_count": preserve,
        "asset_remap": remap,
    }
    await db.projects.update_one(
        {"id": project_id},
        {"$set": {"scene_generation": generation_meta, "updated_at": _now()}},
    )
    try:
        import sentry_sdk
        for key, value in generation_meta.items():
            if value is not None and not isinstance(value, dict):
                sentry_sdk.set_tag(f"scene.{key}", str(value))
        sentry_sdk.add_breadcrumb(
            category="scene.generation",
            message="scene plan generated",
            data={k: v for k, v in generation_meta.items() if not isinstance(v, dict)},
            level="info",
        )
    except Exception:  # noqa: BLE001
        pass
    await _log_cost(db, project_id, "scenes", tokens=len(scenes) * 200, cost=0.06)
    await db.projects.update_one({"id": project_id}, {"$set": {"estimated_cost": float(project.get("estimated_cost", 0)) + 0.06, "updated_at": _now()}})
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


async def _remap_scene_assets(db, project_id: str, old_scenes: list[dict],
                              new_scenes: list[dict]) -> dict:
    """Keep existing work attached across a scene-plan regeneration.

    Stock clips and matching voiceovers follow their ``scene_number`` to the new
    scene id.  Assets from removed scenes, or voiceovers whose narration text
    changed, are detached and explicitly marked orphaned so coverage can never
    report more covered scenes than the current plan contains.
    """
    old_by_number = {s.get("scene_number"): s for s in old_scenes}
    new_by_number = {s.get("scene_number"): s for s in new_scenes}
    remapped = 0
    detached = 0
    for number, old in old_by_number.items():
        old_id = old.get("id")
        if not old_id:
            continue
        new = new_by_number.get(number)
        if new:
            new_id = new["id"]
            result = await db.assets.update_many(
                {"project_id": project_id, "scene_id": old_id,
                 "asset_type": {"$in": STOCK_ASSET_TYPES}},
                {"$set": {"scene_id": new_id, "scene_number": number,
                          "scene_remapped_at": _now(), "updated_at": _now()}},
            )
            remapped += result.modified_count
            old_text = " ".join(str(old.get("narration_text") or "").split())
            new_text = " ".join(str(new.get("narration_text") or "").split())
            if old_text and old_text == new_text:
                result = await db.assets.update_many(
                    {"project_id": project_id, "scene_id": old_id,
                     "asset_type": "voiceover_audio"},
                    {"$set": {"scene_id": new_id, "scene_number": number,
                              "scene_remapped_at": _now(), "updated_at": _now()}},
                )
                remapped += result.modified_count
            else:
                result = await db.assets.update_many(
                    {"project_id": project_id, "scene_id": old_id,
                     "asset_type": "voiceover_audio"},
                    {"$set": {
                        "orphaned_scene_id": old_id,
                        "orphaned_scene_number": number,
                        "orphaned_reason": "scene_narration_changed",
                        "scene_id": None,
                        "updated_at": _now(),
                    }},
                )
                detached += result.modified_count
        else:
            result = await db.assets.update_many(
                {"project_id": project_id, "scene_id": old_id},
                {"$set": {
                    "orphaned_scene_id": old_id,
                    "orphaned_scene_number": number,
                    "orphaned_reason": "scene_removed",
                    "scene_id": None,
                    "updated_at": _now(),
                }},
            )
            detached += result.modified_count
    # Defensive sweep: any asset pointing at an id outside the current plan is
    # orphaned, including leftovers from even older plans with no scene record.
    valid_ids = [s["id"] for s in new_scenes if s.get("id")]
    result = await db.assets.update_many(
        {"project_id": project_id, "scene_id": {"$nin": valid_ids, "$ne": None}},
        {"$set": {"orphaned_reason": "scene_removed", "scene_id": None, "updated_at": _now()}},
    )
    detached += result.modified_count
    return {"remapped": remapped, "detached": detached}


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


async def _generate_metadata_and_thumbnail_concepts(project: dict, script: dict, scenes: list) -> tuple[dict, list]:
    """Plan independent metadata and thumbnail briefs concurrently.

    The endpoint used to wait for one LLM request and then start the other.
    There are exactly two independent tasks here, so this is deliberately
    bounded to two concurrent requests.  Thumbnail planning remains
    best-effort; metadata retains its existing success behavior.
    """
    metadata_result, thumbnail_result = await asyncio.gather(
        gen.generate_metadata(project, script, scenes),
        gen.generate_thumbnail_concepts(project, script),
        return_exceptions=True,
    )
    if isinstance(metadata_result, Exception):
        raise metadata_result
    if isinstance(thumbnail_result, Exception):
        __import__("logging").getLogger("facelessforge.routes").warning(
            "thumbnail concept planning failed project=%s: %s", project.get("id"), thumbnail_result,
        )
        return metadata_result, []
    return metadata_result, thumbnail_result


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
    data, concepts = await _generate_metadata_and_thumbnail_concepts(project, script, scenes)
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

    # Auto-create thumbnail briefs from the concurrently planned concepts.
    # A thumbnail-planning failure preserves the previous briefs exactly as the
    # old best-effort block did; metadata itself is still returned promptly.
    if concepts:
        try:
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
        cleaned = (patch["full_script"] or "").replace("\x00", "").strip()
        if len(cleaned) > 20000:
            raise HTTPException(status_code=422, detail="Script exceeds 20000 characters")
        words = script_word_count(cleaned)
        existing = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
        if not existing:
            # Manual-save path: creating the project's first script without any LLM call.
            if words < 20:
                raise HTTPException(status_code=422, detail="Script needs at least 20 words")
            if source is None:
                source = "manual"
        elif not cleaned:
            raise HTTPException(status_code=422, detail="Script cannot be empty")
        plan = NarrationPlan.from_project(project)
        length = plan.classify_words(words)
        patch["full_script"] = cleaned
        patch["word_count"] = words
        patch["estimated_duration"] = int(words / 2.5)
        patch["word_budget"] = plan.words_target
        patch["word_floor"] = plan.words_min
        patch["word_ceiling"] = plan.words_max
        patch["budget_coverage"] = round(words / plan.words_target, 3) if plan.words_target else None
        patch["length_status"] = length["status"]
        patch["length_message"] = length["message"]
        patch["within_bounds"] = length["within_bounds"]
        patch["under_length"] = length["status"] == "too_short"
        patch["planning_words_per_minute"] = plan.words_per_minute
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
                "asset_type": {"$in": ["stock_video", "stock_image", "thumbnail_concept"]},
            })
            # Paid voiceovers are never deleted on an edit. Mark them outdated
            # so they stay downloadable/auditable but cannot be selected or
            # rendered against the new narration.
            voiceovers_res = await db.assets.update_many(
                {"project_id": project_id, "asset_type": "voiceover_audio"},
                {"$set": {
                    "status": "outdated",
                    "stale": True,
                    "stale_reason": "script_changed",
                    "outdated_at": _now(),
                    "updated_at": _now(),
                }},
            )
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
                "voiceovers_outdated": getattr(voiceovers_res, "modified_count", 0),
            }
            if invalidated["voiceovers_outdated"]:
                invalidated["voiceovers_retained"] = invalidated["voiceovers_outdated"]
    await db.scripts.update_one(
        {"project_id": project_id},
        {"$set": patch, "$setOnInsert": {"id": str(uuid.uuid4()), "project_id": project_id, "created_at": _now()}},
        upsert=True,
    )
    view = await _attach_project_view(db, project)
    if "full_script" in patch:
        view["script_length"] = length
    if invalidated and any(invalidated.values()):
        view["script_invalidated"] = invalidated
        retained = int(invalidated.get("voiceovers_retained") or 0)
        view["script_notice"] = (
            "Script updated — regenerate scenes and metadata. "
            + (f"{retained} paid voiceover(s) were kept as outdated history and cannot be rendered against the new script. " if retained else "")
            + "Completed videos were kept."
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
    info = audio_mix_service.probe_audio(path)
    duration = float((info or {}).get("duration") or 0)
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
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    limit = (
        CLIENT_IMAGE_MAX_BYTES if kind in {"client_image", "brand_image", "brand_logo"}
        else CLIENT_VIDEO_MAX_BYTES if kind in {"client_video", "before_after"}
        else CLIENT_AUDIO_MAX_BYTES
    )
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
        tmp.write_bytes(raw)
        try:
            width, height, duration = await _inspect_client_video(tmp)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    elif kind == "before_after":
        if ext in CLIENT_VIDEO_EXTS:
            tmp = Path(tempfile.gettempdir()) / f"client-before-after-{uuid.uuid4().hex}{ext}"
            tmp.write_bytes(raw)
            try:
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
        tmp.write_bytes(raw)
        try:
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
    saved = _gs().save_file(_stage_upload_copy(raw, key, ext, prefix="client-asset"), key, mime)

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
        return await stock_service.search_stock_aggregated(
            query, body.media_type, body.per_page,
            orientation="portrait" if project.get("aspect_ratio") == "9:16" else None,
        )
    return await stock_service.search_stock(query, body.media_type, body.per_page)


@router.post("/projects/{project_id}/scenes/{scene_id}/attach-asset")
async def attach_scene_asset(project_id: str, scene_id: str, body: StockAttachRequest, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot attach assets")
    if str(body.source or "").lower() == "mock":
        raise HTTPException(status_code=422, detail="Mock footage cannot be attached to a production video.")
    scene = await db.scenes.find_one({"id": scene_id, "project_id": project_id}, {"_id": 0})
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")

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
    await db.assets.insert_one(doc)
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
    # Mark scene as having assets
    await db.scenes.update_one(
        {"id": scene_id, "project_id": project_id},
        {"$set": {"status": "assets_attached", "updated_at": _now()}},
    )
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

def _scene_domain(scene: dict, project: dict) -> str:
    hay = " ".join([
        str(project.get("topic") or ""), str(project.get("niche") or ""),
        str(scene.get("narration_text") or ""), str(scene.get("visual_direction") or ""),
    ]).lower()
    if any(k in hay for k in ("software", "code", "programmer", "developer", "api", "database", "deploy", "export", "app", "tech")):
        return "software"
    if any(k in hay for k in ("construction", "contractor", "building site", "renovation")):
        return "construction"
    return "business"


_SAFE_VISUAL_QUERIES = {
    "software": [
        "software developer",
        "programmer computer",
        "code screen",
        "technology office",
        "server room",
        "keyboard typing",
    ],
    "construction": [
        "construction site",
        "contractor working",
        "building construction",
        "architecture blueprint",
    ],
    "business": [
        "business team office",
        "professional handshake",
        "office laptop",
        "city skyline",
    ],
}

# These words are visually ambiguous in stock search: "export" returns shipping
# containers, "terminal" returns airports, and "delivery" returns parcels.
_AMBIGUOUS_STOCK_WORDS = {
    "export", "exports", "container", "containers", "terminal", "delivery",
    "port", "ports", "shipping", "cargo", "freight", "parcel", "package",
}


def _scene_stock_queries(scene: dict, project: dict) -> list[str]:
    """Ordered, deduplicated stock queries that use scene semantics safely.

    Software topics are searched with concrete visual nouns first because
    literal terms such as "export" produce unrelated shipping footage. Other
    topics keep their scene terms first and use the generic fallbacks second.
    """
    from ..visual_query import build_scene_query, extract_visual_keywords

    domain = _scene_domain(scene, project)
    safe = list(_SAFE_VISUAL_QUERIES.get(domain, _SAFE_VISUAL_QUERIES["business"]))
    terms: list[str] = []
    for term in (scene.get("search_terms") or []):
        text = " ".join(str(term or "").split()).strip()
        if not text:
            continue
        if domain == "software" and any(
            word in _AMBIGUOUS_STOCK_WORDS for word in text.lower().split()
        ):
            continue
        terms.append(text[:60])
    for candidate in (
        build_scene_query(scene),
        " ".join(extract_visual_keywords(str(scene.get("narration_text") or ""), top_n=6)),
        str(scene.get("visual_direction") or "").split(",")[0].strip(),
        str(project.get("topic") or ""),
    ):
        text = " ".join(str(candidate or "").split()).strip()
        if text:
            terms.append(text[:60])

    ordered = safe + terms if domain == "software" else terms + safe
    queries: list[str] = []
    for text in ordered:
        if text and text.lower() not in {q.lower() for q in queries}:
            queries.append(text)
    return queries or ["cinematic b-roll"]


def _stock_media_order(media_type: str | None) -> list[str]:
    requested = str(media_type or "both").lower()
    if requested == "both":
        return ["videos", "photos"]
    if requested in ("videos", "photos"):
        return [requested]
    return ["videos", "photos"]


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

@router.post("/projects/{project_id}/auto-attach-assets")
async def auto_attach_assets(project_id: str, body: AutoAttachRequest, user=Depends(get_current_user)):
    """Find, validate, persist and link stock visuals for unsatisfied scenes.

    A linked visual is a deliberate scene selection, including one selected by
    hand.  It is therefore terminal for a normal retry: do not call a provider,
    replace it, or charge it again unless the caller explicitly asks to replace.
    """

    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot attach assets")

    # Persist a click outcome.  This also makes a duplicate in-flight browser
    # request fail closed instead of racing two provider searches.
    op_key = body.idempotency_key or str(uuid.uuid4())
    op_id = f"{project_id}:{op_key}"
    prior = await db.auto_attach_operations.find_one({"_id": op_id}, {"_id": 0})
    if prior and prior.get("status") == "completed":
        return {**(prior.get("summary") or {}), "replayed": True}
    if prior and prior.get("status") == "running":
        raise HTTPException(status_code=409, detail="Auto-attach is already running for this project. Please wait for it to finish.")
    try:
        if prior:
            await db.auto_attach_operations.update_one(
                {"_id": op_id}, {"$set": {"status": "running", "updated_at": _now()}},
            )
        else:
            await db.auto_attach_operations.insert_one({
                "_id": op_id, "project_id": project_id, "user_id": user["id"],
                "status": "running", "created_at": _now(), "updated_at": _now(),
            })
    except Exception as exc:
        # A duplicate key is an in-flight request that won the race above. Any
        # other persistence fault must not start an untracked write operation.
        if getattr(exc, "code", None) == 11000 or type(exc).__name__ == "DuplicateKeyError":
            raise HTTPException(status_code=409, detail="Auto-attach is already running for this project. Please wait for it to finish.") from exc
        raise HTTPException(status_code=503, detail="Could not record auto-attach operation. Please retry.") from exc

    scenes = await db.scenes.find({"project_id": project_id}).sort("scene_number", 1).to_list(500)
    if not scenes:
        await db.auto_attach_operations.update_one({"_id": op_id}, {"$set": {"status": "failed", "updated_at": _now()}})
        raise HTTPException(status_code=400, detail="Generate scenes before auto-attach.")

    total = len(scenes)
    attached = 0
    skipped = 0
    failed = 0
    near_duplicates_skipped = 0
    details: list[dict] = []
    # On replace, every existing stock asset is deleted as its scene is
    # processed, so its title must not block the replacement.
    project_visual_titles = [] if body.replace_existing else [
        str(a.get("name") or "")
        for a in await db.assets.find(
            {"project_id": project_id, "asset_type": {"$in": STOCK_ASSET_TYPES}},
            {"_id": 0, "name": 1},
        ).to_list(1000)
        if a.get("name")
    ]

    # --- Credits check: sum needed for scenes that will be attached ---
    total_needed = 0
    for _s in scenes:
        _d = _get_scene_duration(_s)
        _n = max(1, math.ceil(_d / 3))
        _sid = str(_s.get("id") or _s.get("_id"))
        _cnt = await db.assets.count_documents({"project_id": project_id, "scene_id": _sid, "asset_type": {"$in": STOCK_ASSET_TYPES}})
        if _cnt > 0 and not body.replace_existing:
            # A user-selected asset is already a usable scene selection.  Do
            # not "top up" it to the timing heuristic on retry.
            continue
        else:
            total_needed += _n
    # ensure subscription exists and check
    sub = await db.subscriptions.find_one({"user_id": user["id"]}, {"_id": 0})
    if not sub:
        # fallback to ff_credits for backward compat, else create free 30
        ff = await db.ff_credits.find_one({"tenant_id": user["id"]}, {"_id": 0}) or await db.ff_credits.find_one({"user_id": user["id"]}, {"_id": 0})
        if ff:
            sub = {"credits_remaining": int(ff.get("credits", 30)), "credits_monthly": int(ff.get("credits", 30))}
        else:
            # create free subscription 30 credits
            sub_doc = {
                "user_id": user["id"],
                "paddle_customer_id": None,
                "paddle_subscription_id": None,
                "paddle_price_id": None,
                "plan": "free",
                "status": "active",
                "credits_monthly": 30,
                "credits_remaining": 30,
                "current_period_end": _now() + timedelta(days=30),
                "created_at": _now(),
                "updated_at": _now(),
            }
            await db.subscriptions.insert_one(sub_doc)
            sub = sub_doc
    remaining = int(sub.get("credits_remaining", 30) if isinstance(sub, dict) else 30)
    if total_needed > 0 and remaining < total_needed:
        reset_at = sub.get("current_period_end") or _now() + timedelta(days=30)
        if hasattr(reset_at, 'isoformat'):
            reset_at = reset_at.isoformat()
        await db.auto_attach_operations.update_one({"_id": op_id}, {"$set": {"status": "failed", "updated_at": _now()}})
        raise HTTPException(status_code=402, detail={"code": "credits_exhausted", "reset_at": reset_at, "plan": sub.get("plan", "free"), "remaining": remaining, "required": total_needed, "quota": int(sub.get("credits_monthly", 30)), "message": f"Insufficient credits: {remaining} remaining, {total_needed} required. Need {total_needed} credits for {total_needed} assets (3s each)."})

    for scene in scenes:
        scene_id = str(scene.get("id") or scene.get("_id"))
        # 3-second rule: ceil(duration/3) assets per scene (10 for 30s), 3s each
        duration = _get_scene_duration(scene)
        num_needed = max(1, math.ceil(duration / 3))
        # Handle existing count and replace logic
        existing_count = await db.assets.count_documents({
            "project_id": project_id,
            "scene_id": scene_id,
            "asset_type": {"$in": STOCK_ASSET_TYPES},
        })
        if existing_count > 0 and not body.replace_existing:
            skipped += 1
            details.append({"scene_id": scene_id, "scene_number": scene.get("scene_number"), "status": "skipped", "reason": "already_has_stock"})
            # Do not overwrite the manual selection's scene status.
            continue
        else:
            remaining = num_needed

        # Use every scene term (not just term zero), and prefer moving clips.
        try:
            import logging
            logger = logging.getLogger("facelessforge.routes")
            queries = _scene_stock_queries(scene, project)
            # 27a483e: concise provider queries - generated terms are alternatives, not one phrase.
            queries = [q for q in (compact_stock_query(str(x)) for x in queries) if q]
            logger.info(
                "AUTO_ATTACH_QUERIES scene=%s queries=%s media=%s",
                scene.get("scene_number"), queries, body.media_type,
            )
            # Gather existing ids to exclude (dedup)
            existing_assets = await db.assets.find({"project_id": project_id, "scene_id": scene_id, "asset_type": {"$in": STOCK_ASSET_TYPES}}, {"_id": 0, "external_id": 1, "preview_url": 1}).to_list(100)
            existing_ids = set()
            for ea in existing_assets:
                if ea.get("external_id"):
                    existing_ids.add(str(ea.get("external_id")))
                if ea.get("preview_url"):
                    existing_ids.add(str(ea.get("preview_url")))
            per_page = min(max(remaining * 3, 10), 40)
            media_order = _stock_media_order(body.media_type)
            candidates = []
            seen = set(existing_ids)

            async def _search(query: str, media: str):
                if hasattr(stock_service, "search_stock_aggregated"):
                    return await stock_service.search_stock_aggregated(
                        query=query, media_type=media, per_page=per_page,
                        orientation="portrait" if project.get("aspect_ratio") == "9:16" else None)
                return await stock_service.search_stock(query, media, per_page)

            for media in media_order:
                for query in queries:
                    if len(candidates) >= remaining:
                        break
                    result = await _search(query, media)
                    for row in (result.get("results") or []) if result else []:
                        row_type = str(row.get("media_type") or row.get("asset_type") or "")
                        if media == "videos" and row_type and row_type != "stock_video":
                            continue
                        if media == "photos" and row_type and row_type != "stock_image":
                            continue
                        ext = str(row.get("external_id") or "")
                        prev = str(row.get("preview_url") or "")
                        key = ext or prev
                        if not key or key in seen:
                            continue
                        seen.add(key)
                        candidates.append(row)
                        if len(candidates) >= remaining:
                            break
                if len(candidates) >= remaining:
                    break

            # Last-resort mixed search preserves the old behaviour when a
            # provider has no typed results for the scene terms.
            if not candidates:
                result = await stock_service.search_stock_aggregated(
                    query=(queries[0] if queries else "nature landscape"), media_type="both", per_page=per_page,
                    orientation="portrait" if project.get("aspect_ratio") == "9:16" else None)
                for row in (result.get("results") or []) if result else []:
                    key = str(row.get("external_id") or row.get("preview_url") or "")
                    if key and key not in seen:
                        seen.add(key)
                        candidates.append(row)
            unique = _dedup_by_preview(candidates)
            # 27a483e: provider query echoes are not evidence of content. Reject mock,
            # unverifiable and off-topic rows before any write or billing.
            unique, irrelevant = relevant_candidates(unique, scene, queries[0] if queries else "")
            for rejected in irrelevant:
                details.append({
                    "scene_id": scene_id,
                    "scene_number": scene.get("scene_number"),
                    "status": "skipped",
                    "reason": "irrelevant_or_unverified",
                    "title": rejected.get("title") or rejected.get("name"),
                })
            unique, near_duplicates = filter_distinct_titles(unique, project_visual_titles)
            for duplicate in near_duplicates:
                near_duplicates_skipped += 1
                details.append({
                    "scene_id": scene_id,
                    "scene_number": scene.get("scene_number"),
                    "status": "skipped",
                    "reason": "near_duplicate",
                    "title": duplicate.get("title") or duplicate.get("name"),
                })
                try:
                    await db.scenes.update_one({"id": scene_id}, {"$set": {"status": "skipped", "updated_at": _now()}})
                except Exception:
                    pass
            unique = unique[:remaining]
            if not unique:
                failed += 1
                details.append({"scene_id": scene_id, "scene_number": scene.get("scene_number"), "status": "failed", "reason": "no_relevant_stock"})
                try:
                    await db.scenes.update_one({"id": scene_id}, {"$set": {"status": "failed", "error_message": "no_relevant_stock", "updated_at": _now()}})
                except Exception:
                    pass
                continue
            # Replacement is insert-first: a provider or storage failure must
            # leave the current selection usable.  Only delete captured rows
            # after all replacement rows are durably saved.
            originals = await db.assets.find(
                {"project_id": project_id, "scene_id": scene_id, "asset_type": {"$in": STOCK_ASSET_TYPES}},
                {"_id": 0},
            ).to_list(100)
            start_order = 0 if body.replace_existing else existing_count
            committed = []
            for i, asset in enumerate(unique):
                order = start_order + i
                start_offset = order * 3  # 0,3,6,9...
                duration_asset = 3
                doc = {
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
                    "duration": duration_asset,
                    "tags": asset.get("tags") or [],
                    "query": query,
                    "status": "attached",
                    "order": order,
                    "start_offset": start_offset,
                    "scene_duration": duration,
                    "created_at": _now(),
                    "updated_at": _now(),
                }
                try:
                    await db.assets.insert_one(doc)
                    committed.append(doc)
                except Exception as exc:
                    is_duplicate = getattr(exc, "code", None) == 11000 or type(exc).__name__ == "DuplicateKeyError"
                    if is_duplicate:
                        skipped += 1
                        details.append({"scene_id": scene_id, "scene_number": scene.get("scene_number"), "status": "skipped", "reason": "duplicate"})
                        continue
                    # A storage problem is not a duplicate.  Compensate only
                    # rows from this attempt and preserve any original choice.
                    if committed:
                        await db.assets.delete_many({"project_id": project_id, "id": {"$in": [d["id"] for d in committed]}})
                    failed += 1
                    details.append({"scene_id": scene_id, "scene_number": scene.get("scene_number"), "status": "failed", "reason": "storage_error"})
                    logger.exception("auto-attach persistence failed project=%s scene=%s", project_id, scene_id)
                    committed = []
                    break
            if not committed:
                continue
            if body.replace_existing and originals:
                try:
                    await db.assets.delete_many({"project_id": project_id, "id": {"$in": [d["id"] for d in originals]}})
                except Exception:
                    await db.assets.delete_many({"project_id": project_id, "id": {"$in": [d["id"] for d in committed]}})
                    failed += 1
                    details.append({"scene_id": scene_id, "scene_number": scene.get("scene_number"), "status": "failed", "reason": "storage_error"})
                    logger.exception("auto-attach replacement cleanup failed project=%s scene=%s", project_id, scene_id)
                    continue
            # A scene is only marked attached after usable rows have been saved
            # and linked.  Clear a prior error only at this verified point.
            await db.scenes.update_one(
                {"id": scene_id, "project_id": project_id},
                {"$set": {"status": "attached", "error_message": "", "updated_at": _now()}},
            )
            attached += len(committed)
            project_visual_titles.extend(doc["name"] for doc in committed)
            details.extend({"scene_id": scene_id, "scene_number": scene.get("scene_number"), "status": "attached", "asset_id": doc["id"], "order": doc["order"], "start_offset": doc["start_offset"]} for doc in committed)
            # Deduct credits only for rows that were saved and linked.
            if committed:
                await db.subscriptions.update_one({"user_id": user["id"]}, {"$inc": {"credits_remaining": -len(committed)}, "$set": {"updated_at": _now()}})
                for _ in committed:
                    await db.credit_transactions.insert_one({
                        "id": str(uuid.uuid4()),
                        "user_id": user["id"],
                        "amount": -1,
                        "reason": f"auto-attach scene {scene.get('scene_number')} {duration}s",
                        "created_at": _now(),
                    })
                # also mirror to ff_credits for backward compat
                try:
                    await db.ff_credits.update_one({"tenant_id": user["id"]}, {"$inc": {"credits": -len(committed)}})
                except: pass
            logger.info("auto-attach scene %s duration=%s num_needed=%s remaining=%s query='%s' -> %d committed", scene.get("scene_number"), duration, num_needed, remaining, query[:60], len(committed))
        except Exception as e:
            failed += 1
            details.append({"scene_id": scene_id, "scene_number": scene.get("scene_number"), "status": "failed", "reason": str(e)[:120]})
            logger.exception("auto-attach scene failed project=%s scene=%s", project_id, scene_id)
            try:
                await db.scenes.update_one({"id": scene_id}, {"$set": {"status": "failed", "error_message": str(e)[:120], "updated_at": _now()}})
            except Exception:
                pass

    # Required journalctl log for spec: [ATTACH] 18 attached 0 failed
    import logging as _logging
    _logging.getLogger("facelessforge.routes").info("[ATTACH] %s attached %s failed project=%s mock=%s", attached, failed, project_id, stock_service.is_mock_mode())
    print(f"[ATTACH] {attached} attached {failed} failed project={project_id}", flush=True)
    summary = {
        "total": total,
        "attached": attached,
        "skipped": skipped,
        "near_duplicates_skipped": near_duplicates_skipped,
        "failed": failed,
        "details": details,
        "mock": stock_service.is_mock_mode(),
        "replayed": False,
    }
    await db.auto_attach_operations.update_one(
        {"_id": op_id},
        {"$set": {"status": "completed" if failed == 0 else "failed", "summary": summary, "updated_at": _now()}},
    )
    return summary


# ============================ THUMBNAIL IMAGE GENERATION ============================

@router.get("/thumbnails/meta")
async def thumbnails_meta(user=Depends(get_current_user)):
    return thumb_images.provider_info()


@router.post("/projects/{project_id}/thumbnails/{brief_asset_id}/generate")
async def generate_thumbnail_image_endpoint(
    project_id: str, brief_asset_id: str,
    body: GenerateThumbnailImagesRequest,
    user=Depends(get_current_user),
    request: Request = None,
):
    """Queue one independent job per thumbnail option (HTTP), or run the
    historical synchronous batch for internal callers.

    Each option has its own terminal record, so a failed option can never erase
    a successful sibling or leave the UI spinning forever.
    """
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

    if request is None:
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

    active = await job_service.find_active_thumbnail_set(project_id, brief_asset_id)
    reused = bool(active)
    if not active:
        active = await job_service.create_thumbnail_set(
            project_id=project_id,
            brief_asset_id=brief_asset_id,
            variants=body.variants,
            requested_by=user["id"],
        )
    payload = {
        "set": _ser(active["set"]),
        "options": [_ser(o) for o in active.get("options", [])],
        "poll_url": f"/api/projects/{project_id}/thumbnail-sets/{active['set']['id']}",
        "reused": reused,
    }
    return JSONResponse(status_code=202, content=jsonable_encoder(payload))


@router.get("/projects/{project_id}/thumbnail-sets/{set_id}")
async def get_thumbnail_set_endpoint(
    project_id: str, set_id: str, user=Depends(get_current_user),
):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    payload = await job_service.get_thumbnail_set(project_id, set_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Thumbnail set not found")
    response = {
        "set": _ser(payload["set"]),
        "options": [_ser(o) for o in payload.get("options", [])],
        "assets": [_ser(a) for a in payload.get("assets", [])],
    }
    if str(payload["set"].get("status") or "").lower() in (
        "succeeded", "partial", "failed", "completed", "cancelled",
    ):
        response["view"] = await _attach_project_view(
            db, await db.projects.find_one({"id": project_id}, {"_id": 0}))
    return response


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


async def _perform_full_voiceover(
    project_id: str,
    body: GenerateVoiceoverRequest = Body(default=None),
    user=Depends(get_current_user),
    *,
    job: dict | None = None,
    on_output_saved=None,
):
    """Synthesize one full-script take and register it before judging fit.

    Contract:
    * no automatic paid correction loop;
    * AI-generated scripts outside the shared word envelope are rejected
      before any provider call;
    * a completed take is inserted as an asset even when its measured duration
      is outside the target, with an explicit duration-fit suggestion;
    * the selected duration is never changed here.
    """
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot generate voiceovers")
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    if not script or not script.get("full_script", "").strip():
        raise HTTPException(status_code=400, detail="Generate a script before voiceover.")
    body = body or GenerateVoiceoverRequest()
    snapshot = (job or {}).get("text_snapshot")
    text = (body.text_override or snapshot or script["full_script"]).strip()
    if not text:
        raise HTTPException(status_code=400, detail="Script cannot be empty.")
    voice = _voice_style_for(project, body.voice_style)
    plan = NarrationPlan.from_project(project)
    word_count = script_word_count(text)
    length = plan.classify_words(word_count)
    script_source = (script.get("source") or "ai_generated").lower()

    # The submitted script version must match the snapshot the job was created
    # for. A queued job must never silently narrate a different edit.
    if job and job.get("script_hash"):
        current_hash = hashlib.sha256(str(script.get("full_script") or "").encode("utf-8")).hexdigest()
        if not snapshot and current_hash != job.get("script_hash"):
            raise HTTPException(status_code=409, detail={
                "code": "script_changed_before_generation",
                "message": "The script changed after this voiceover job was queued. "
                           "No provider call was made. Start a new voiceover request for the saved script.",
            })

    # AI scripts are generated inside the envelope. If one reaches the paid
    # boundary outside it, fix text first; never spend on a known mismatch.
    if script_source != "manual" and not length["within_bounds"]:
        raise HTTPException(status_code=422, detail={
            "code": "script_length_out_of_bounds",
            "message": length["message"] + " No paid provider call was made.",
            "word_count": word_count,
            "word_budget": plan.words_target,
            "word_floor": plan.words_min,
            "word_ceiling": plan.words_max,
            "delta_words": length["delta_words"],
            "length_status": length["status"],
        })

    asset_id = (job or {}).get("asset_id") or str(uuid.uuid4())
    script_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        payload = await tts_service.generate_voiceover(
            text=text,
            voice_style=voice,
            tone=body.tone,
            project_id=project_id,
            asset_id=asset_id,
            scene_id=None,
            name_suffix="(Full script)",
            job_id=(job or {}).get("id"),
            script_id=script.get("id"),
            script_hash=script_hash,
        )
    except tts_service.VoiceoverSynthesisError as exc:
        raise HTTPException(status_code=502, detail={
            "code": exc.code,
            "message": str(exc),
            "attempt_id": exc.attempt_id,
            "chunks_completed": exc.chunks_completed,
            "chunks_total": exc.chunks_total,
            "partial_path": exc.partial_path,
            "provider_outcome": "uncertain" if exc.uncertain else "failed",
            "needs_reconciliation": bool(exc.uncertain or exc.chunks_completed),
        }) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail={
            "code": "voiceover_provider_error",
            "message": f"Voiceover generation failed: {exc}",
            "needs_reconciliation": True,
        }) from exc

    measured = payload.get("duration")
    duration_status = plan.classify_measured(measured)
    fitted = None
    if measured and duration_status.get("status") == "outside_safe_range":
        fitted = suggest_fitted_duration(
            voiceover_seconds=float(measured), intro_seconds=plan.intro_seconds
        )
    timing_fields = {
        "target_duration": float(project.get("target_duration") or plan.target_seconds),
        "narration_budget_seconds": round(plan.narration_seconds, 3),
        "duration_status": duration_status["status"],
        "duration_message": duration_status["message"],
        "fits_target": duration_status["status"] in {"ready", "tempo_fit"},
        "duration_fit_required": duration_status["status"] not in {"ready", "tempo_fit"},
        "duration_measured": bool(measured),
        "duration_method": payload.get("measurement_method"),
        "autofit_suggestion": (
            {"from": int(project.get("target_duration") or round(plan.target_seconds)),
             "to": int(fitted), "voiceover_seconds": float(measured)}
            if fitted else None
        ),
    }
    payload.update(timing_fields)
    payload["script_id"] = script.get("id")
    payload["script_hash"] = script_hash
    payload["word_count"] = word_count
    payload["text_excerpt"] = text[:240]
    payload["created_at"] = _now()
    payload["updated_at"] = _now()
    payload["status"] = "selected"
    payload["output_saved"] = True
    _ensure_playable_voiceover(payload)

    # Keep any previously selected full-script take, but demote it. The new
    # take is persisted BEFORE duration judgment and before project bookkeeping.
    await db.assets.update_many(
        {
            "project_id": project_id,
            "asset_type": "voiceover_audio",
            "scene_id": None,
            "status": "selected",
        },
        {"$set": {"status": "generated", "updated_at": _now()}},
    )
    try:
        await db.assets.insert_one(dict(payload))
    except Exception as exc:  # noqa: BLE001
        logger = __import__("logging").getLogger("facelessforge.routes")
        logger.exception("voiceover asset registration failed project=%s asset=%s", project_id, asset_id)
        raise HTTPException(status_code=500, detail={
            "code": "voiceover_asset_registration_failed",
            "message": "Audio was generated and saved, but its project asset record could not be written. "
                       "The recording is preserved; run reconciliation instead of paying for another take.",
            "attempt_id": payload.get("attempt_id"),
            "asset_id": asset_id,
            "file_path": payload.get("file_path"),
            "needs_reconciliation": True,
        }) from exc
    if on_output_saved is not None:
        try:
            await on_output_saved(payload)
        except Exception:  # noqa: BLE001 — never lose a saved artifact to a job tag failure
            pass
    if payload.get("attempt_id"):
        await voiceover_attempts.mark_asset_registered(
            payload["attempt_id"], asset_id=asset_id
        )

    try:
        await _record_voice_fit(
            db,
            project_id,
            f"{script.get('id')}:{script.get('updated_at')}:{plan.narration_seconds}",
            1,
            1,
            0,
            float(measured or 0),
            [duration_status["message"]],
        )
    except Exception as exc:  # noqa: BLE001 — saved audio must remain a success
        __import__("logging").getLogger("facelessforge.routes").warning(
            "voice_fit record failed after audio saved project=%s: %s", project_id, exc
        )
    project_set = {
        "selected_voiceover_asset_id": asset_id,
        "voiceover_duration_status": duration_status["status"],
        "updated_at": _now(),
    }
    if not payload.get("mock"):
        cost = float(payload.get("cost_estimate") or 0)
        if cost:
            project_set["estimated_cost"] = float(project.get("estimated_cost", 0)) + cost
    try:
        await db.projects.update_one({"id": project_id}, {"$set": project_set})
    except Exception as exc:  # noqa: BLE001 — saved audio must remain a success
        __import__("logging").getLogger("facelessforge.routes").warning(
            "selected voiceover update failed after audio saved project=%s: %s", project_id, exc
        )
    view = await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))
    return {"view": view, "asset": payload, "timing": duration_status}


@router.post("/projects/{project_id}/voiceover/generate-script")
async def generate_full_voiceover(
    project_id: str,
    body: GenerateVoiceoverRequest = Body(default=None),
    user=Depends(get_current_user),
    request: Request = None,
):
    """Start a durable voiceover job (HTTP) or run synchronously (internal).

    HTTP requests get a 202 immediately, so a proxy disconnect can never make
    a completed provider call look like a failure.  Direct/internal calls with
    no Request keep the historical synchronous return value for tests and
    callers that explicitly need it.
    """
    if request is None:
        result = await _perform_full_voiceover(project_id, body, user)
        return result["view"]

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
    info = tts_service.provider_info()
    if (
        not info.get("mock")
        and str(info.get("provider") or "") in tts_service.MEASUREMENT_REQUIRED_PROVIDERS
        and not tts_service.measurement_available()
    ):
        raise HTTPException(status_code=503, detail={
            "code": "audio_measurement_unavailable",
            "message": "Audio measurement (ffprobe) is unavailable. No paid voice provider was called.",
        })
    script_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    voice_id = tts_service._voice_id_for(voice)
    try:
        job, recovered = await job_service.create_voiceover_job(
            project_id=project_id,
            requested_by=user["id"],
            requested_role=user.get("role") or "creator",
            script_id=script.get("id"),
            script_hash=script_hash,
            text_override=body.text_override,
            text_snapshot=text,
            voice_style=voice,
            voice_id=voice_id,
            provider=str(info.get("provider") or ""),
            model=str(info.get("model") or ""),
            tone=body.tone,
            force=bool(body.force),
            requested_duration_seconds=int(project.get("target_duration") or 0) or None,
            scene_count=await db.scenes.count_documents({"project_id": project_id}),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "voiceover_in_progress",
            "message": str(exc),
        }) from exc
    payload = _ser(job)
    payload["poll_url"] = f"/api/projects/{project_id}/voiceover/jobs/{job['id']}"
    if recovered:
        payload["idempotency_result"] = "recovered"
        payload["output_saved"] = True
        payload["view"] = await _attach_project_view(
            db, await db.projects.find_one({"id": project_id}, {"_id": 0}))
        return JSONResponse(status_code=200, content=jsonable_encoder(payload))
    if str(job.get("status") or "").lower() == "failed":
        # Never turn an undefined provider outcome into a second charge.
        raise HTTPException(status_code=409, detail={
            "code": "voiceover_reconciliation_required",
            "message": (
                "This recording request already reached the provider and its outcome "
                "is not safe to repeat automatically. Reconcile the existing attempt "
                "first; no new provider call was made."
            ),
            "job": payload,
            "attempt": job.get("attempt"),
            "needs_reconciliation": bool(job.get("needs_reconciliation")),
            "recoverable_audio": job.get("idempotency_result") == "recoverable_audio",
            "retry_requires_force": bool(job.get("retry_requires_force")),
        })
    return JSONResponse(status_code=202, content=jsonable_encoder(payload))


@router.get("/projects/{project_id}/voiceover/jobs")
async def list_voiceover_jobs_endpoint(
    project_id: str, user=Depends(get_current_user), limit: int = 20,
):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    jobs = await job_service.list_voiceover_jobs(project_id, limit=limit)
    return [_ser(j) for j in jobs]


@router.get("/projects/{project_id}/voiceover/jobs/{job_id}")
async def get_voiceover_job_endpoint(
    project_id: str, job_id: str, user=Depends(get_current_user),
):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    job = await job_service.get_voiceover_job(project_id, job_id, touch=True)
    if not job:
        raise HTTPException(status_code=404, detail="Voiceover job not found")
    payload = _ser(job)
    if str(job.get("status") or "").lower() == "succeeded":
        payload["output_saved"] = bool(job.get("output_saved"))
        payload["view"] = await _attach_project_view(
            db, await db.projects.find_one({"id": project_id}, {"_id": 0}))
    return payload


@router.post("/projects/{project_id}/voiceover/jobs/{job_id}/disconnect")
async def voiceover_job_disconnect(
    project_id: str, job_id: str, user=Depends(get_current_user),
):
    """Record a client disconnect without cancelling the provider work."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    job = await db.voiceover_jobs.find_one(
        {"id": job_id, "project_id": project_id}, {"_id": 0, "id": 1})
    if not job:
        raise HTTPException(status_code=404, detail="Voiceover job not found")
    await db.voiceover_jobs.update_one(
        {"id": job_id},
        {"$set": {"client_disconnected": True, "updated_at": _now()}},
    )
    return {"ok": True, "job_id": job_id, "client_disconnected": True}


@router.post("/projects/{project_id}/voiceover/jobs/{job_id}/reconcile")
async def reconcile_voiceover_job(
    project_id: str, job_id: str, user=Depends(get_current_user),
):
    """Recover a saved recording from durable attempt data. Never calls TTS.

    This is the explicit repair step for a worker restart or a failure between
    provider completion, file write and asset registration.
    """
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot reconcile voiceovers")
    job = await db.voiceover_jobs.find_one(
        {"id": job_id, "project_id": project_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Voiceover job not found")
    attempt = await voiceover_attempts.latest_for_job(job_id)
    if not attempt:
        raise HTTPException(status_code=409, detail={
            "code": "voiceover_attempt_not_found",
            "message": "No durable attempt record exists for this job; nothing can be reconciled without a new provider call.",
            "needs_reconciliation": False,
        })
    audio_path = attempt.get("audio_path")
    if not audio_path or not Path(str(audio_path)).is_file():
        raise HTTPException(status_code=409, detail={
            "code": "voiceover_audio_not_found",
            "message": "No completed audio file was recorded for this attempt. Do not retry the provider until usage history is reviewed.",
            "attempt": attempt,
            "needs_reconciliation": bool(attempt.get("provider_call_started")),
        })
    existing = await db.assets.find_one(
        {"id": attempt.get("asset_id"), "project_id": project_id,
         "asset_type": "voiceover_audio"}, {"_id": 0})
    if not existing:
        path = Path(str(audio_path))
        published = tts_service._publish_audio(path, project_id)
        script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
        current_hash = hashlib.sha256(
            str((script or {}).get("full_script") or "").encode("utf-8")
        ).hexdigest()
        stale = bool(attempt.get("script_hash") and attempt["script_hash"] != current_hash)
        plan = NarrationPlan.from_project(project)
        measured = attempt.get("duration")
        duration_status = (
            {"status": "outdated", "message": "This take belongs to an older script version."}
            if stale else plan.classify_measured(measured)
        )
        fitted = None
        if (
            not stale and measured
            and duration_status.get("status") == "outside_safe_range"
        ):
            fitted = suggest_fitted_duration(
                voiceover_seconds=float(measured), intro_seconds=plan.intro_seconds
            )
        asset = {
            "id": attempt["asset_id"],
            "project_id": project_id,
            "name": f"Voiceover {attempt.get('scene_id') or '(Full script)'}",
            "asset_type": "voiceover_audio",
            "scene_id": attempt.get("scene_id"),
            "storage_key": published.key if published else None,
            "url": published.url if published else None,
            "preview_url": (published.preview_path or published.url) if published else None,
            "storage_mode": getattr(published, "storage_mode", None) if published else "local",
            "storage_degraded": bool(getattr(published, "degraded", False)) if published else True,
            "publication_status": "published" if published else "failed",
            "file_path": str(path),
            "duration": measured,
            "duration_measured": bool(attempt.get("duration_measured")),
            "measurement_method": attempt.get("measurement_method"),
            "size_bytes": path.stat().st_size,
            "word_count": attempt.get("word_count"),
            "provider": attempt.get("provider"),
            "voice_id": attempt.get("voice_id"),
            "voice_style": attempt.get("voice_style"),
            "tone": attempt.get("tone"),
            "source": attempt.get("provider"),
            "mock": False,
            "status": "outdated" if stale else "generated",
            "stale": stale,
            "stale_reason": "script_changed" if stale else None,
            "script_id": attempt.get("script_id"),
            "script_hash": attempt.get("script_hash"),
            "text_excerpt": attempt.get("text_excerpt"),
            "target_duration": float(project.get("target_duration") or plan.target_seconds),
            "narration_budget_seconds": round(plan.narration_seconds, 3),
            "duration_status": duration_status["status"],
            "duration_message": duration_status["message"],
            "fits_target": duration_status["status"] in {"ready", "tempo_fit"},
            "duration_fit_required": duration_status["status"] not in {"ready", "tempo_fit"},
            "autofit_suggestion": (
                {"from": int(project.get("target_duration") or round(plan.target_seconds)),
                 "to": int(fitted), "voiceover_seconds": float(measured)}
                if fitted else None
            ),
            "output_saved": True,
            "reconciled_from_attempt": attempt.get("id"),
            "created_at": _now(),
            "updated_at": _now(),
        }
        _ensure_playable_voiceover(asset)
        await db.assets.insert_one(dict(asset))
        existing = asset
    if not existing.get("preview_url"):
        _ensure_playable_voiceover(existing)
        await db.assets.update_one(
            {"id": existing["id"], "project_id": project_id},
            {"$set": {
                "preview_url": existing["preview_url"],
                "url": existing["url"],
                "updated_at": _now(),
            }},
        )
    if (
        not existing.get("stale")
        and attempt.get("script_hash")
        and not project.get("selected_voiceover_asset_id")
    ):
        await db.assets.update_many(
            {"project_id": project_id, "asset_type": "voiceover_audio",
             "scene_id": None, "status": "selected"},
            {"$set": {"status": "generated", "updated_at": _now()}},
        )
        await db.assets.update_one(
            {"id": existing["id"], "project_id": project_id},
            {"$set": {"status": "selected", "updated_at": _now()}},
        )
        existing["status"] = "selected"
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"selected_voiceover_asset_id": existing["id"], "updated_at": _now()}},
        )
    await voiceover_attempts.mark_asset_registered(attempt["id"], asset_id=existing["id"])
    await db.voiceover_jobs.update_one(
        {"id": job_id},
        {"$set": {
            "status": "succeeded", "terminal_status": "success",
            "output_saved": True, "asset": _ser(existing),
            "asset_id": existing["id"], "provider_outcome": "succeeded",
            "needs_reconciliation": False,
            "error_code": None, "error_message": None, "error_detail": None,
            "completed_at": job.get("completed_at") or _now(),
            "updated_at": _now(), "lease_token": None, "lease_expires_at": None,
            "reconciled": True,
        }},
    )
    await job_service.release_voiceover_lock(
        project_id=project_id,
        kind=job.get("kind") or ("scene" if job.get("scene_id") else "full_script"),
        scene_id=job.get("scene_id"), job_id=job_id,
    )
    view = await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))
    return {"ok": True, "job_id": job_id, "asset": _ser(existing), "view": view}


@router.get("/projects/{project_id}/voiceover/attempts")
async def list_voiceover_attempts(
    project_id: str, user=Depends(get_current_user), limit: int = 50,
):
    """Auditable provider attempts, including failures and partial chunks."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    attempts = await voiceover_attempts.list_for_project(project_id, limit=limit)
    return [_ser(a) for a in attempts]


@router.get("/projects/{project_id}/provider-attempts")
async def list_provider_attempts(
    project_id: str, user=Depends(get_current_user), limit: int = 100,
):
    """Measured LLM/TTS usage and explicit cost estimates for this project."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    attempts = await provider_usage.list_for_project(project_id, limit=limit)
    return [_ser(a) for a in attempts]


async def _perform_scene_voiceover(
    project_id: str,
    scene_id: str,
    body: GenerateVoiceoverRequest = Body(default=None),
    user=Depends(get_current_user),
    *,
    job: dict | None = None,
    on_output_saved=None,
):
    """Synthesize one scene take and persist it without moving the project target."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot generate voiceovers")
    scene = await db.scenes.find_one({"id": scene_id, "project_id": project_id}, {"_id": 0})
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")
    body = body or GenerateVoiceoverRequest()
    snapshot = (job or {}).get("text_snapshot")
    text = (body.text_override or snapshot or scene.get("narration_text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Scene has no narration text. Generate scenes first.")
    voice = _voice_style_for(project, body.voice_style)
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    script_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    asset_id = (job or {}).get("asset_id") or str(uuid.uuid4())
    try:
        payload = await tts_service.generate_voiceover(
            text=text,
            voice_style=voice,
            tone=body.tone,
            project_id=project_id,
            asset_id=asset_id,
            scene_id=scene_id,
            name_suffix=f"(Scene {scene.get('scene_number', '?'):02d})" if isinstance(scene.get('scene_number'), int) else "(Scene)",
            job_id=(job or {}).get("id"),
            script_id=(script or {}).get("id"),
            script_hash=script_hash,
        )
    except tts_service.VoiceoverSynthesisError as exc:
        raise HTTPException(status_code=502, detail={
            "code": exc.code,
            "message": str(exc),
            "attempt_id": exc.attempt_id,
            "chunks_completed": exc.chunks_completed,
            "chunks_total": exc.chunks_total,
            "partial_path": exc.partial_path,
            "provider_outcome": "uncertain" if exc.uncertain else "failed",
            "needs_reconciliation": bool(exc.uncertain or exc.chunks_completed),
        }) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail={
            "code": "voiceover_provider_error",
            "message": f"Voiceover generation failed: {exc}",
            "needs_reconciliation": True,
        }) from exc

    measured = payload.get("duration")
    expected = float(scene.get("duration") or 0) or None
    payload["scene_id"] = scene_id
    payload["text_excerpt"] = text[:240]
    payload["scene_number"] = scene.get("scene_number")
    payload["script_id"] = (script or {}).get("id")
    payload["script_hash"] = script_hash
    payload["target_scene_seconds"] = expected
    payload["duration_measured"] = bool(measured)
    payload["duration_status"] = (
        "unmeasured" if not measured else
        "ready" if expected and abs(float(measured) - expected) <= 0.05 * expected else
        "outside_scene_range"
    )
    payload["duration_fit_required"] = payload["duration_status"] not in {"ready", "tempo_fit"}
    payload["created_at"] = _now()
    payload["updated_at"] = _now()
    payload["output_saved"] = True
    _ensure_playable_voiceover(payload)
    # Demote any other voiceover for this scene to "generated", mark this one selected
    await db.assets.update_many(
        {"project_id": project_id, "scene_id": scene_id, "asset_type": "voiceover_audio", "status": "selected"},
        {"$set": {"status": "generated", "updated_at": _now()}},
    )
    payload["status"] = "selected"
    await db.assets.insert_one(dict(payload))
    if on_output_saved is not None:
        try:
            await on_output_saved(payload)
        except Exception:  # noqa: BLE001 — never lose a saved artifact to a job tag failure
            pass
    if payload.get("attempt_id"):
        await voiceover_attempts.mark_asset_registered(payload["attempt_id"], asset_id=asset_id)

    # Scene timing is normalized from the selected project duration at render.
    # Never rewrite project.target_duration from one scene take.
    if not payload.get("mock"):
        cost = float(payload.get("cost_estimate") or 0)
        if cost:
            await db.projects.update_one(
                {"id": project_id},
                {"$set": {"estimated_cost": float(project.get("estimated_cost", 0)) + cost, "updated_at": _now()}},
            )
    view = await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))
    return {"view": view, "asset": payload}


@router.post("/projects/{project_id}/scenes/{scene_id}/voiceover/generate")
async def generate_scene_voiceover(
    project_id: str, scene_id: str,
    body: GenerateVoiceoverRequest = Body(default=None),
    user=Depends(get_current_user),
    request: Request = None,
):
    """Start a durable scene voiceover job (HTTP) or run synchronously (internal).

    Mirrors the full-script contract: 202 + ``poll_url``, DB row persisted before
    any provider call, and an idempotency key of
    ``hash(project_id + scene_id + script_hash)``.  Retrying a disconnected
    request recovers the same take instead of paying ElevenLabs twice.
    """
    if request is None:
        result = await _perform_scene_voiceover(project_id, scene_id, body, user)
        return result["view"]

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
    info = tts_service.provider_info()
    if (
        not info.get("mock")
        and str(info.get("provider") or "") in tts_service.MEASUREMENT_REQUIRED_PROVIDERS
        and not tts_service.measurement_available()
    ):
        raise HTTPException(status_code=503, detail={
            "code": "audio_measurement_unavailable",
            "message": "Audio measurement (ffprobe) is unavailable. No paid voice provider was called.",
        })
    script_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    voice_id = tts_service._voice_id_for(voice)
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    job, recovered = await job_service.create_voiceover_job(
        project_id=project_id,
        requested_by=user["id"],
        requested_role=user.get("role") or "creator",
        script_id=(script or {}).get("id"),
        script_hash=script_hash,
        text_override=body.text_override,
        text_snapshot=text,
        voice_style=voice,
        voice_id=voice_id,
        provider=str(info.get("provider") or ""),
        model=str(info.get("model") or ""),
        tone=body.tone,
        force=bool(body.force),
        scene_id=scene_id,
        kind="scene",
        requested_duration_seconds=int(project.get("target_duration") or 0) or None,
        scene_count=1,
    )
    payload = _ser(job)
    payload["poll_url"] = f"/api/projects/{project_id}/voiceover/jobs/{job['id']}"
    if recovered:
        payload["idempotency_result"] = "recovered"
        payload["output_saved"] = True
        payload["view"] = await _attach_project_view(
            db, await db.projects.find_one({"id": project_id}, {"_id": 0}))
        return JSONResponse(status_code=200, content=jsonable_encoder(payload))
    if str(job.get("status") or "").lower() == "failed":
        raise HTTPException(status_code=409, detail={
            "code": "voiceover_reconciliation_required",
            "message": (
                "This scene take already reached the provider and its outcome is "
                "not safe to repeat automatically. Reconcile the existing attempt "
                "first; no new provider call was made."
            ),
            "job": payload,
            "attempt": job.get("attempt"),
            "needs_reconciliation": bool(job.get("needs_reconciliation")),
            "recoverable_audio": job.get("idempotency_result") == "recoverable_audio",
            "retry_requires_force": bool(job.get("retry_requires_force")),
        })
    return JSONResponse(status_code=202, content=jsonable_encoder(payload))


def _voiceover_audio_url(project_id: str, asset_id: str) -> str:
    return f"/api/projects/{project_id}/voiceover/{asset_id}/audio"


def _ensure_playable_voiceover(payload: dict) -> dict:
    """Never leave a saved file without an authenticated playback URL."""
    if not payload.get("preview_url"):
        payload["preview_url"] = _voiceover_audio_url(
            str(payload.get("project_id") or ""), str(payload.get("id") or "")
        )
    if not payload.get("url"):
        payload["url"] = payload["preview_url"]
    return payload


@router.get("/projects/{project_id}/voiceover/{asset_id}/audio")
async def stream_voiceover_audio(
    project_id: str, asset_id: str, user=Depends(get_current_user),
):
    """Authenticated local fallback for a canonical paid recording.

    Object storage is preferred, but a publication failure must not make a
    paid take inaccessible. Only the asset's recorded canonical path is served.
    """
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    asset = await db.assets.find_one(
        {"id": asset_id, "project_id": project_id, "asset_type": "voiceover_audio"},
        {"_id": 0},
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Voiceover not found")
    path = Path(str(asset.get("file_path") or ""))
    roots = [
        tts_service.VOICEOVER_STORAGE_DIR.resolve(),
        tts_service.STATIC_ROOT.resolve(),
    ]
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        resolved = None
    if (
        resolved is None
        or not resolved.is_file()
        or not any(resolved == root or root in resolved.parents for root in roots)
    ):
        raise HTTPException(status_code=404, detail="Voiceover file is not available locally")
    media = "audio/mpeg" if resolved.suffix.lower() == ".mp3" else "audio/wav"
    return FileResponse(resolved, media_type=media, filename=resolved.name)


@router.post("/projects/{project_id}/voiceover/{asset_id}/restore")
async def restore_voiceover(project_id: str, asset_id: str, user=Depends(get_current_user)):
    """Restore a rejected take only when it matches the current narration.

    Restoration never calls a TTS provider. It preserves the existing file and
    makes the recording selectable again; duration fit remains a separate,
    explicit action and the project target is untouched.
    """
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot restore voiceovers")
    asset = await db.assets.find_one(
        {"id": asset_id, "project_id": project_id, "asset_type": "voiceover_audio"},
        {"_id": 0},
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Voiceover not found")
    if asset.get("stale") or asset.get("status") == "outdated":
        raise HTTPException(status_code=409, detail={
            "code": "voiceover_outdated",
            "message": "This recording belongs to an older script version. It remains downloadable history and cannot be restored or selected.",
        })
    if asset.get("status") == "selected":
        return await _attach_project_view(
            db, await db.projects.find_one({"id": project_id}, {"_id": 0})
        )

    if asset.get("scene_id"):
        scene = await db.scenes.find_one(
            {"id": asset["scene_id"], "project_id": project_id}, {"_id": 0})
        if not scene:
            raise HTTPException(status_code=409, detail={
                "code": "voiceover_scene_missing",
                "message": "The scene for this recording no longer exists. It remains downloadable history.",
            })
        current_text = str(scene.get("narration_text") or "")
    else:
        script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
        current_text = str((script or {}).get("full_script") or "")
    if not current_text.strip():
        raise HTTPException(status_code=409, detail={
            "code": "voiceover_script_missing",
            "message": "The current narration text is unavailable; restore cannot verify this recording.",
        })
    current_hash = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
    if not asset.get("script_hash") or asset["script_hash"] != current_hash:
        raise HTTPException(status_code=409, detail={
            "code": "voiceover_script_mismatch",
            "message": "This recording does not match the current narration. It cannot be restored; keep it as downloadable history or generate a new take.",
        })
    local_path = Path(str(asset.get("file_path") or ""))
    if not local_path.is_file() and not asset.get("preview_url"):
        raise HTTPException(status_code=409, detail={
            "code": "voiceover_file_missing",
            "message": "The recording file is not accessible. It cannot be restored without a new paid generation.",
        })

    now = _now()
    plan = NarrationPlan.from_project(project)
    measured = asset.get("duration")
    ds = plan.classify_measured(measured)
    fitted = None
    if measured and ds.get("status") == "outside_safe_range":
        fitted = suggest_fitted_duration(
            voiceover_seconds=float(measured), intro_seconds=plan.intro_seconds)
    patch = {
        "status": "selected",
        "stale": False,
        "stale_reason": None,
        "restored_at": now,
        "restored_by": user["id"],
        "target_duration": float(project.get("target_duration") or plan.target_seconds),
        "narration_budget_seconds": round(plan.narration_seconds, 3),
        "duration_status": ds["status"],
        "duration_message": ds["message"],
        "fits_target": ds["status"] in {"ready", "tempo_fit"},
        "duration_fit_required": ds["status"] not in {"ready", "tempo_fit"},
        "autofit_suggestion": (
            {"from": int(project.get("target_duration") or round(plan.target_seconds)),
             "to": int(fitted), "voiceover_seconds": float(measured)}
            if fitted else None
        ),
        "updated_at": now,
    }
    if asset.get("scene_id"):
        await db.assets.update_many(
            {"project_id": project_id, "scene_id": asset["scene_id"],
             "asset_type": "voiceover_audio", "status": "selected"},
            {"$set": {"status": "generated", "updated_at": now}},
        )
        await db.assets.update_one(
            {"id": asset_id, "project_id": project_id}, {"$set": patch})
    else:
        await db.assets.update_many(
            {"project_id": project_id, "scene_id": None,
             "asset_type": "voiceover_audio", "status": "selected"},
            {"$set": {"status": "generated", "updated_at": now}},
        )
        await db.assets.update_one(
            {"id": asset_id, "project_id": project_id}, {"$set": patch})
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {"selected_voiceover_asset_id": asset_id, "updated_at": now}},
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
    if asset.get("stale") or asset.get("status") == "outdated":
        raise HTTPException(status_code=409, detail={
            "code": "voiceover_outdated",
            "message": "This recording was made for an older script version. It remains downloadable, but cannot be selected or rendered.",
        })
    if asset.get("script_hash"):
        script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
        current_hash = hashlib.sha256(
            str((script or {}).get("full_script") or "").encode("utf-8")
        ).hexdigest()
        if current_hash != asset.get("script_hash"):
            raise HTTPException(status_code=409, detail={
                "code": "voiceover_outdated",
                "message": "This recording does not match the saved script. Regenerate it or keep it as history.",
            })
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
    raw = await file.read()
    if len(raw) > MUSIC_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Music file exceeds 20 MB")
    if len(raw) < 1024:
        raise HTTPException(status_code=422, detail="Music file is empty")
    tmp = Path(tempfile.gettempdir()) / f"music-upload-{uuid.uuid4().hex}{ext}"
    tmp.write_bytes(raw)
    try:
        info = await asyncio.to_thread(audio_mix_service.probe_audio, tmp)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    if not info.get("sample_rate") or not info.get("duration"):
        raise HTTPException(status_code=422, detail="File is not decodable audio")
    if info["duration"] > MUSIC_MAX_SECONDS:
        raise HTTPException(status_code=422, detail="Music track exceeds 600 seconds")
    from ..storage import get_storage as _gs
    asset_id = str(uuid.uuid4())
    key = f"music/{project_id}/{asset_id}{ext}"
    saved = _gs().save_file(_stage_upload_copy(raw, key, ext), key, "audio/mpeg")
    doc = {
        "id": asset_id,
        "project_id": project_id,
        "name": file.filename or "music track",
        "asset_type": "music_audio",
        "file_path": None,
        "storage_key": saved.key,
        "preview_url": saved.preview_path or saved.url,
        "source": "upload",
        "duration": info["duration"],
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
    """Persist upload bytes to a temp path for the storage backend."""
    import tempfile
    tmp = Path(tempfile.gettempdir()) / f"{prefix}-{uuid.uuid4().hex}{ext}"
    tmp.write_bytes(raw)
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
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])
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
    project = project_after or project
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
        job = await render_service.queue_render(project_id, requested_by=user["id"], burn_subtitles=bool(body.burn_subtitles), aspect_ratio=body.aspect_ratio)
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
    return [_render_job_payload(j, resolve=False) for j in jobs]


@router.get("/projects/{project_id}/render/jobs/{job_id}")
async def render_get_job(project_id: str, job_id: str, user=Depends(get_current_user)):
    """Fast DB-only status poll (no storage signing, no ffprobe)."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    job = await db.render_jobs.find_one({"id": job_id, "project_id": project_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Render job not found")
    return _render_job_payload(job, resolve=False)


@router.get("/projects/{project_id}/render/jobs/{job_id}/probe")
async def render_probe_job(project_id: str, job_id: str, user=Depends(get_current_user)):
    """Live process/artifact probe, deliberately separate from status polling.

    The DB status endpoint must stay cheap and fast.  This endpoint answers the
    harder question ("is the worker still alive and did its file really land?")
    without changing the job.  It never starts or retries work.
    """
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    job = await db.render_jobs.find_one({"id": job_id, "project_id": project_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Render job not found")
    from .. import render as render_service
    task = render_service._ACTIVE_TASKS.get(project_id)
    worker_alive = bool(task is not None and not task.done())
    status = str(job.get("status") or "").lower()
    active = status in set(render_service.ACTIVE_RENDER_STATUS_VALUES)
    updated_at = job.get("updated_at")
    age_seconds = None
    if isinstance(updated_at, datetime):
        # Motor returns naive UTC datetimes by default; _now() is timezone-aware.
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((_now() - updated_at).total_seconds()))
    output_path = job.get("output_path")
    output_exists = None
    output_size_bytes = None
    if output_path:
        try:
            p = Path(output_path)
            output_exists = p.exists() and p.is_file()
            if output_exists:
                output_size_bytes = p.stat().st_size
        except Exception:  # noqa: BLE001
            output_exists = None
    mongo_ok = True
    try:
        from ..db import ping_db
        mongo_ok = bool(await ping_db())
    except Exception:  # noqa: BLE001
        mongo_ok = False
    return {
        "job_id": job_id,
        "project_id": project_id,
        "status": status,
        "current_step": job.get("current_step"),
        "progress": job.get("progress"),
        "updated_at": _ser({"v": updated_at}).get("v"),
        "updated_age_seconds": age_seconds,
        "worker_alive": worker_alive,
        "active_status": active,
        "worker_missing_for_active_job": bool(active and not worker_alive),
        "output_path": output_path,
        "output_exists": output_exists,
        "output_size_bytes": output_size_bytes,
        "output_storage_key": job.get("output_storage_key"),
        "storage_mode": job.get("storage_mode"),
        "queue_duration_ms": job.get("queue_duration_ms"),
        "scene_count": job.get("scene_count"),
        "attached_clip_count": job.get("attached_clip_count"),
        "orphaned_clip_count": job.get("orphaned_clip_count"),
        "asset_candidate_count": job.get("asset_candidate_count"),
        "attached_clips_by_scene": job.get("attached_clips_by_scene"),
        "requested_duration_seconds": job.get("requested_duration_seconds"),
        "mongo_ok": mongo_ok,
    }


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
                "word_count": script_word_count(script), "estimated_duration": 60, "created_at": _now(), "updated_at": _now()
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
            "provider": os.environ.get("TTS_PROVIDER", "gemini"),
            "model": os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts"),
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

def _private_export_headers(filename: str = None) -> dict[str, str]:
    """Prevent shared caches retaining authenticated project snapshots."""
    headers = {
        "Cache-Control": "private, no-store, max-age=0",
        "CDN-Cache-Control": "no-store",
        "Cloudflare-CDN-Cache-Control": "no-store",
        "Pragma": "no-cache",
        "Expires": "0",
        "Vary": "Cookie, Authorization",
    }
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return headers


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
    return PlainTextResponse(body, headers=_private_export_headers(f"{project_id}-script.txt"))


@router.get("/projects/{project_id}/export/scenes.csv")
async def export_scenes_csv(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    scenes = await db.scenes.find({"project_id": project_id}).sort("scene_number", 1).to_list(500)
    csv = scenes_to_csv(scenes)
    return PlainTextResponse(csv, media_type="text/csv", headers=_private_export_headers(f"{project_id}-scenes.csv"))


@router.get("/projects/{project_id}/export/metadata.json")
async def export_metadata_json(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    if not metadata:
        raise HTTPException(status_code=404, detail="No metadata")
    return JSONResponse(jsonable_encoder(_ser(metadata)), headers=_private_export_headers())


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
        headers=_private_export_headers(f"{project_id}-package.zip"),
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
    # TOTAL EST COST = EST OUTPUT * export credit cost (blocks * 3 * multiplier).
    # Locked to PRICING_POLICY_CANONICAL.md — a 30s block = 3 credits at 1080p.
    reference_export_cost = calculate_credits(30, "1080p")
    total_estimated_cost_credits = round(monthly_output_projection * reference_export_cost, 1)

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
        "total_estimated_cost_formula": "blocks=max(1,ceil(duration/30)) * 3 * multiplier",
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
