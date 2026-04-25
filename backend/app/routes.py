"""Routers: auth, projects, generation, exports, analytics, settings."""
from __future__ import annotations

import io
import json
import os
import uuid
import zipfile
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Body
from fastapi.responses import StreamingResponse, PlainTextResponse

from .auth import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    set_auth_cookies, clear_auth_cookies, get_current_user, require_roles,
)
from .db import get_db
from .models import (
    RegisterRequest, LoginRequest, ProjectCreate, ProjectUpdate,
    ScriptUpdate, MetadataUpdate, ProviderSettingsUpdate, AssetCreate,
    ShareUpdate, ForgotPasswordRequest, ResetPasswordRequest,
    StockAttachRequest, FindAssetsRequest, AssetStatusUpdate,
    AutoAttachRequest, GenerateThumbnailImagesRequest,
)
from .scoring import quality_score, quality_label, compute_project_status, scenes_to_csv
from . import generation as gen
from . import stock as stock_service
from . import thumbnail_images as thumb_images


router = APIRouter(prefix="/api")


def _now():
    return datetime.now(timezone.utc)


def _ser(doc: dict) -> dict:
    """Drop _id and serialise datetimes for JSON."""
    if not doc:
        return doc
    doc = {k: v for k, v in doc.items() if k != "_id"}
    for k, v in list(doc.items()):
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


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
async def register(body: RegisterRequest, response: Response):
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
    set_auth_cookies(response, access, refresh)
    return _ser({**user_doc, "password_hash": None})


@router.post("/auth/login")
async def login(body: LoginRequest, response: Response):
    db = get_db()
    email = body.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    access = create_access_token(user["id"], email, user["role"])
    refresh = create_refresh_token(user["id"])
    set_auth_cookies(response, access, refresh)
    out = dict(user); out.pop("password_hash", None)
    return _ser(out)


@router.post("/auth/logout")
async def logout(response: Response, _user=Depends(get_current_user)):
    clear_auth_cookies(response)
    return {"ok": True}


@router.get("/auth/me")
async def me(user=Depends(get_current_user)):
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
    return {
        "project": _ser(project) | {"quality_label": quality_label(score)},
        "script": _ser(script) if script else None,
        "scenes": sorted([_ser(s) for s in scenes], key=lambda s: s.get("scene_number", 0)),
        "metadata": _ser(metadata) if metadata else None,
        "assets": [_ser(a) for a in assets],
        "render_job": _ser(render_job) if render_job else None,
        "share": _share_payload(project),
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
        "visual_style": body.visual_style or "cinematic b-roll",
        "monetisation_intent": body.monetisation_intent or "ads + affiliate",
        "cta_goal": body.cta_goal or "subscribe",
        "status": "DRAFT",
        "quality_score": 0,
        "estimated_cost": 0.0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.projects.insert_one(proj)
    return _ser(proj)


@router.get("/projects")
async def list_projects(user=Depends(get_current_user)):
    db = get_db()
    q = {} if user["role"] == "admin" else {"user_id": user["id"]}
    projs = await db.projects.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    # Attach derived score labels without recomputing too expensively:
    return [_ser(p) | {"quality_label": quality_label(int(p.get("quality_score", 0)))} for p in projs]


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
    if "target_duration" in patch and not (30 <= int(patch["target_duration"]) <= 3600):
        raise HTTPException(status_code=422, detail="Target duration must be 30..3600s")
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
        "provider": "openai/gpt-5.2",
        "operation": operation,
        "tokens_used": tokens,
        "characters_used": 0,
        "estimated_cost": cost,
        "created_at": _now(),
    })


@router.post("/projects/{project_id}/generate-script")
async def generate_script_endpoint(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    data = await gen.generate_script(project)
    doc = {
        "id": str(uuid.uuid4()),
        "project_id": project_id,
        **data,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.scripts.replace_one({"project_id": project_id}, doc, upsert=True)
    await _log_cost(db, project_id, "script", tokens=max(500, data["word_count"] * 2), cost=0.08)
    await db.projects.update_one({"id": project_id}, {"$set": {"estimated_cost": float(project.get("estimated_cost", 0)) + 0.08, "updated_at": _now()}})
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.post("/projects/{project_id}/generate-scenes")
async def generate_scenes_endpoint(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    if not script:
        raise HTTPException(status_code=400, detail="Generate a script before scenes")
    scenes = await gen.generate_scenes(project, script["full_script"])
    for sc in scenes:
        sc["project_id"] = project_id
        sc["created_at"] = _now()
        sc["updated_at"] = _now()
    await db.scenes.delete_many({"project_id": project_id})
    if scenes:
        await db.scenes.insert_many([dict(sc) for sc in scenes])
    await _log_cost(db, project_id, "scenes", tokens=len(scenes) * 200, cost=0.06)
    await db.projects.update_one({"id": project_id}, {"$set": {"estimated_cost": float(project.get("estimated_cost", 0)) + 0.06, "updated_at": _now()}})
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.post("/projects/{project_id}/generate-metadata")
async def generate_metadata_endpoint(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project_id}, {"_id": 0}).sort("scene_number", 1).to_list(500)
    if not script:
        raise HTTPException(status_code=400, detail="Generate a script before metadata")
    data = await gen.generate_metadata(project, script["full_script"], scenes)
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
    return await _attach_project_view(db, await db.projects.find_one({"id": project_id}, {"_id": 0}))


@router.post("/projects/{project_id}/generate-thumbnails")
async def generate_thumbnails_endpoint(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    concepts = await gen.generate_thumbnails(project)
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
    if "full_script" in patch:
        import re as _re
        patch["word_count"] = len(_re.findall(r"\b\w+\b", patch["full_script"]))
        patch["estimated_duration"] = int(patch["word_count"] / 2.5)
    patch["updated_at"] = _now()
    await db.scripts.update_one({"project_id": project_id}, {"$set": patch})
    return await _attach_project_view(db, project)


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
    return {"mock": stock_service.is_mock_mode()}


@router.post("/projects/{project_id}/stock-search")
async def project_stock_search(project_id: str, body: FindAssetsRequest, user=Depends(get_current_user)):
    """Ad-hoc stock search scoped to a project (no scene context)."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    query = (body.query or project.get("topic") or project.get("niche") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Provide a query or ensure project has a topic.")
    return await stock_service.search_stock(query, body.media_type, body.per_page)


@router.post("/projects/{project_id}/scenes/{scene_id}/find-assets")
async def find_scene_assets(project_id: str, scene_id: str, body: FindAssetsRequest, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    scene = await db.scenes.find_one({"id": scene_id, "project_id": project_id}, {"_id": 0})
    if not scene:
        raise HTTPException(status_code=404, detail="Scene not found")

    # Build the query: explicit body.query > scene.search_terms > visual_direction > project.topic
    query_parts: list[str] = []
    if body.query and body.query.strip():
        query_parts.append(body.query.strip())
    elif scene.get("search_terms"):
        # Use first 2-3 terms as a single query
        query_parts.append(" ".join(scene["search_terms"][:3]))
    elif scene.get("visual_direction"):
        query_parts.append(scene["visual_direction"][:80])
    else:
        query_parts.append(project.get("topic") or project.get("niche") or "stock")
    query = " ".join(q for q in query_parts if q).strip()

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
    # Mark scene as having assets
    await db.scenes.update_one(
        {"id": scene_id, "project_id": project_id},
        {"$set": {"status": "assets_attached", "updated_at": _now()}},
    )
    return _ser(doc)


# ---- Auto-attach top result per scene ----

@router.post("/projects/{project_id}/auto-attach-assets")
async def auto_attach_assets(project_id: str, body: AutoAttachRequest, user=Depends(get_current_user)):
    """Iterate scenes and attach the top stock result per scene.
    Skips scenes that already have stock assets unless replace_existing=true.
    """
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot attach assets")

    scenes = await db.scenes.find({"project_id": project_id}, {"_id": 0}).sort("scene_number", 1).to_list(500)
    if not scenes:
        raise HTTPException(status_code=400, detail="Generate scenes before auto-attach.")

    total = len(scenes)
    attached = 0
    skipped = 0
    failed = 0
    details: list[dict] = []

    for scene in scenes:
        scene_id = scene["id"]
        existing = await db.assets.find_one({
            "project_id": project_id,
            "scene_id": scene_id,
            "asset_type": {"$in": ["stock_video", "stock_image"]},
        }, {"_id": 0})

        if existing and not body.replace_existing:
            skipped += 1
            details.append({"scene_id": scene_id, "scene_number": scene["scene_number"], "status": "skipped", "reason": "already_has_stock"})
            continue

        # Build query
        query_parts = []
        if scene.get("search_terms"):
            query_parts.append(" ".join(scene["search_terms"][:3]))
        elif scene.get("visual_direction"):
            query_parts.append(scene["visual_direction"][:80])
        else:
            query_parts.append(project.get("topic") or project.get("niche") or "stock")
        query = " ".join(q for q in query_parts if q).strip() or "stock"

        try:
            result = await stock_service.search_stock(query, body.media_type, per_page=4)
            top = (result.get("results") or [None])[0]
            if not top:
                failed += 1
                details.append({"scene_id": scene_id, "scene_number": scene["scene_number"], "status": "failed", "reason": "no_results"})
                continue

            # If replacing, remove prior stock assets for this scene first
            if existing and body.replace_existing:
                await db.assets.delete_many({
                    "project_id": project_id,
                    "scene_id": scene_id,
                    "asset_type": {"$in": ["stock_video", "stock_image"]},
                })

            doc = {
                "id": str(uuid.uuid4()),
                "project_id": project_id,
                "scene_id": scene_id,
                "name": top["title"],
                "asset_type": top["media_type"],
                "file_path": None,
                "source": top["source"],
                "external_id": top["external_id"],
                "preview_url": top.get("preview_url"),
                "source_url": top.get("source_url"),
                "download_url": top.get("download_url"),
                "attribution_name": top.get("attribution_name"),
                "attribution_url": top.get("attribution_url"),
                "width": top.get("width"),
                "height": top.get("height"),
                "duration": top.get("duration"),
                "tags": top.get("tags") or [],
                "query": query,
                "status": "attached",
                "created_at": _now(),
                "updated_at": _now(),
            }
            try:
                await db.assets.insert_one(doc)
                attached += 1
                details.append({"scene_id": scene_id, "scene_number": scene["scene_number"], "status": "attached", "asset_id": doc["id"]})
            except Exception as insert_err:  # DuplicateKeyError from compound unique index
                # Treat as skipped — another actor already attached the same item
                skipped += 1
                details.append({"scene_id": scene_id, "scene_number": scene["scene_number"], "status": "skipped", "reason": "duplicate"})
        except Exception as e:  # noqa: BLE001
            failed += 1
            details.append({"scene_id": scene_id, "scene_number": scene["scene_number"], "status": "failed", "reason": str(e)[:120]})

    return {
        "total": total,
        "attached": attached,
        "skipped": skipped,
        "failed": failed,
        "details": details,
        "mock": stock_service.is_mock_mode(),
    }


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
from . import tts as tts_service
from .models import GenerateVoiceoverRequest


@router.get("/tts/meta")
async def tts_meta(user=Depends(get_current_user)):
    return tts_service.provider_info()


def _voice_style_for(project: dict, override: Optional[str]) -> str:
    """Map a free-form project.voice_style ("neutral male narrator") to a tts key."""
    if override and override in tts_service.VOICE_STYLE_MAP:
        return override
    raw = (project.get("voice_style") or "").lower()
    for key in tts_service.VOICE_STYLE_MAP.keys():
        if key in raw:
            return key
    if "male" in raw or "narrator" in raw or "deep" in raw:
        return "narrator"
    if "upbeat" in raw or "energy" in raw or "fast" in raw:
        return "energetic"
    if "doc" in raw or "neutral" in raw:
        return "documentary"
    return os.environ.get("DEFAULT_VOICE_STYLE", "narrator")


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
    try:
        payload = await tts_service.generate_voiceover(
            text=text, voice_style=voice, project_id=project_id,
            asset_id=asset_id, scene_id=None, name_suffix="(Full script)",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Voiceover generation failed: {e}")

    payload["text_excerpt"] = text[:240]
    payload["created_at"] = _now()
    payload["updated_at"] = _now()
    await db.assets.insert_one(dict(payload))

    if not payload.get("mock"):
        cost = float(payload.get("cost_estimate") or 0)
        if cost:
            await db.projects.update_one(
                {"id": project_id},
                {"$set": {"estimated_cost": float(project.get("estimated_cost", 0)) + cost, "updated_at": _now()}},
            )
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
            text=text, voice_style=voice, project_id=project_id,
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
    # Best-effort file removal
    fp = asset.get("file_path")
    if fp:
        try:
            from pathlib import Path as _P
            p = _P(fp)
            if p.exists() and p.is_file():
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


# ============================ RENDER QUEUE (Phase 6) ============================
from . import render as render_service
from .models import RenderStartRequest


@router.get("/projects/{project_id}/render/preflight")
async def render_preflight(project_id: str, user=Depends(get_current_user)):
    """Return validation checklist for the render UI."""
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project_id}, {"_id": 0}).sort("scene_number", 1).to_list(500)
    metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    assets = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(500)
    return render_service.validate_prerequisites(project, script, scenes, metadata, assets)


@router.post("/projects/{project_id}/render/start")
async def render_start(project_id: str,
                       body: RenderStartRequest = Body(default=None),
                       user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user, write=True)
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot start renders")
    # Validate prereqs first so caller gets a clean 400 before queuing
    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project_id}, {"_id": 0}).sort("scene_number", 1).to_list(500)
    metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    assets = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(500)
    check = render_service.validate_prerequisites(project, script, scenes, metadata, assets)
    if not check["ok"]:
        raise HTTPException(status_code=400, detail={
            "message": "Render prerequisites not met",
            "issues": check["issues"],
            "checklist": check["checklist"],
        })
    try:
        job = await render_service.queue_render(project_id, requested_by=user["id"])
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _ser(job)


@router.get("/projects/{project_id}/render/jobs")
async def render_list_jobs(project_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    jobs = await db.render_jobs.find({"project_id": project_id}, {"_id": 0})\
        .sort("created_at", -1).to_list(50)
    return [_ser(j) for j in jobs]


@router.get("/projects/{project_id}/render/jobs/{job_id}")
async def render_get_job(project_id: str, job_id: str, user=Depends(get_current_user)):
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    _ensure_project_access(project, user)
    job = await db.render_jobs.find_one({"id": job_id, "project_id": project_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail="Render job not found")
    return _ser(job)


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
    scenes = await db.scenes.find({"project_id": project_id}, {"_id": 0}).sort("scene_number", 1).to_list(500)
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
    scenes = await db.scenes.find({"project_id": project_id}, {"_id": 0}).sort("scene_number", 1).to_list(500)
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
            {"project_id": project_id, "status": "completed", "output_url": {"$ne": None}},
            {"_id": 0},
            sort=[("completed_at", -1)],
        )
        if latest:
            zf.writestr("render.json", json.dumps({
                "job_id": latest.get("id"),
                "status": latest.get("status"),
                "duration": latest.get("duration"),
                "file_size": latest.get("file_size"),
                "url": latest.get("output_url"),
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
    q = {} if user["role"] == "admin" else {"user_id": user["id"]}
    projects = await db.projects.find(q, {"_id": 0}).to_list(1000)
    total = len(projects)
    completed = sum(1 for p in projects if p.get("status") == "COMPLETED")
    in_progress = sum(1 for p in projects if p.get("status") not in ("COMPLETED", "FAILED", "DRAFT"))
    avg_q = (sum(int(p.get("quality_score", 0)) for p in projects) / total) if total else 0
    total_cost = sum(float(p.get("estimated_cost", 0)) for p in projects)

    status_counts = {}
    niche_counts = {}
    for p in projects:
        status_counts[p.get("status", "DRAFT")] = status_counts.get(p.get("status", "DRAFT"), 0) + 1
        niche_counts[p.get("niche", "other")] = niche_counts.get(p.get("niche", "other"), 0) + 1

    # projects over time (last 14 days)
    from collections import Counter
    by_day = Counter()
    for p in projects:
        created = p.get("created_at")
        if isinstance(created, datetime):
            by_day[created.date().isoformat()] += 1
    # monthly content output projection
    monthly_output_projection = round(completed * 4.2 + in_progress * 1.5, 1)

    return {
        "total_projects": total,
        "completed": completed,
        "in_progress": in_progress,
        "average_quality_score": round(avg_q, 1),
        "total_estimated_cost": round(total_cost, 2),
        "monthly_output_projection": monthly_output_projection,
        "status_counts": status_counts,
        "niche_counts": niche_counts,
        "projects_over_time": sorted([{"date": d, "count": c} for d, c in by_day.items()], key=lambda x: x["date"]),
    }


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
            "preferred_provider": "openai/gpt-5.2",
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
        {"project_id": project["id"], "status": "completed", "output_url": {"$ne": None}},
        {"_id": 0},
        sort=[("completed_at", -1)],
    )

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
            "url": final_render.get("output_url"),
            "duration": final_render.get("duration"),
            "width": 1920,
            "height": 1080,
        } if final_render else None),
        "shared_at": project.get("updated_at").isoformat()
            if isinstance(project.get("updated_at"), datetime) else project.get("updated_at"),
    }
