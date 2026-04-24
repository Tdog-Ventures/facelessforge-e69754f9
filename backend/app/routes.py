"""Routers: auth, projects, generation, exports, analytics, settings."""
from __future__ import annotations

import io
import json
import os
import uuid
import zipfile
from datetime import datetime, timezone
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
)
from .scoring import quality_score, quality_label, compute_project_status, scenes_to_csv
from . import generation as gen


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
