"""Auto-post engine - triggered after render completed if project.auto_post=true.
No manual prep: system generates and posts to platform of choice.
Handles rate limits (redis), compliance (banned phrases, duration), vault tokens, analytics, daily cap.
"""
from __future__ import annotations
import os, logging, asyncio, uuid
from datetime import datetime, timezone
from typing import List, Dict, Any
logger = logging.getLogger("autopost.engine")

# Import adapters
try:
    from ..adapters.posters.youtube import YoutubePoster
    from ..adapters.posters.tiktok import TiktokPoster
    from ..adapters.posters.instagram import InstagramPoster
    from ..adapters.posters.facebook import FacebookPoster
    from ..adapters.posters.x_twitter import XPoster
except ImportError:
    from app.adapters.posters.youtube import YoutubePoster  # type: ignore
    from app.adapters.posters.tiktok import TiktokPoster
    from app.adapters.posters.instagram import InstagramPoster
    from app.adapters.posters.facebook import FacebookPoster
    from app.adapters.posters.x_twitter import XPoster

ADAPTERS = {
    "youtube": YoutubePoster(),
    "tiktok": TiktokPoster(),
    "instagram": InstagramPoster(),
    "facebook": FacebookPoster(),
    "x": XPoster(),
    "twitter": XPoster(),
}

SUPPORTED_PLATFORMS = ["youtube","tiktok","instagram","facebook","x","linkedin","reddit","product_hunt"]
# LinkedIn/Reddit/Product Hunt use generic mock via same pattern
def _now():
    return datetime.now(timezone.utc)

async def autopost_after_render(project: dict, render_job: dict, metadata: dict):
    """Entry point called from render.py after status=completed.
    project: dict with auto_post, platforms, user_id, id
    render_job: dict with output_url, duration
    metadata: dict with selected_title, description, tags
    """
    if not project.get("auto_post"):
        logger.info(f"autopost skipped project={project.get('id')} auto_post=false")
        return {"skipped":"auto_post false"}
    platforms = project.get("platforms") or ["youtube"]
    # Normalize platforms list
    norm = []
    for p in platforms:
        pp = str(p).lower().strip()
        if pp in ("yt","youtube"): norm.append("youtube")
        elif pp in ("ig","instagram"): norm.append("instagram")
        elif pp in ("fb","facebook"): norm.append("facebook")
        elif pp in ("tt","tiktok"): norm.append("tiktok")
        elif pp in ("x","twitter"): norm.append("x")
        else: norm.append(pp)
    if not norm:
        norm = ["youtube"]
    # Also add youtube if empty? spec says customer choice, but ensure at least one
    tenant_id = str(project.get("user_id") or project.get("tenant_id") or "default")
    video_url = render_job.get("output_url") or render_job.get("output_path") or ""
    results: List[Dict[str,Any]] = []
    for plat in norm:
        if plat not in ADAPTERS:
            # No real adapter for this platform — never record a fake "posted" result.
            results.append({"platform":plat,"status":"not_supported","note":"no autopost adapter for this platform yet"})
            continue
        adapter = ADAPTERS[plat]
        try:
            res = await adapter.post(project, video_url, metadata, tenant_id)
            results.append(res)
            logger.info(f"autopost {plat} project={project.get('id')} status={res.get('status')}")
        except Exception as e:
            logger.warning(f"autopost {plat} failed {e}")
            results.append({"platform":plat,"status":"error","error":str(e)[:300]})
        # Small delay to respect rate limits across platforms
        await asyncio.sleep(0.5)
    # Persist to db.autopost_logs for analytics
    try:
        from ..db import get_db
        db = get_db()
        doc = {"id": str(uuid.uuid4()), "project_id": project.get("id"), "tenant_id": tenant_id, "platforms": norm, "results": results, "video_url": video_url, "created_at": _now(), "updated_at": _now()}
        await db.autopost_logs.insert_one(doc)
        await db.projects.update_one({"id": project.get("id")}, {"$set": {"autopost_results": results, "autopost_at": _now()}})
    except Exception as e:
        logger.warning(f"autopost log failed {e}")
    return {"platforms": norm, "results": results}

# For manual trigger / testing
async def trigger_autopost_for_project(project_id: str) -> dict:
    from ..db import get_db
    db = get_db()
    project = await db.projects.find_one({"id": project_id}, {"_id":0})
    if not project:
        raise RuntimeError(f"project {project_id} not found")
    job = await db.render_jobs.find_one({"project_id": project_id}, {"_id":0}, sort=[("created_at",-1)])
    if not job or job.get("status")!="completed":
        raise RuntimeError(f"no completed render for {project_id}")
    meta = await db.metadata_packages.find_one({"project_id": project_id}, {"_id":0}) or {}
    # merge duration
    meta["duration"] = job.get("duration")
    return await autopost_after_render(project, job, meta)

# APIRouter for manual trigger - mounted in server.py
from fastapi import APIRouter, Depends, HTTPException
try:
    from ...app.auth import get_current_user
except ImportError:
    from app.auth import get_current_user
autopost_router = APIRouter(prefix="/api/projects", tags=["autopost"])
@autopost_router.post("/{project_id}/autopost")
async def autopost_trigger(project_id: str, user=Depends(get_current_user)):
    from ..db import get_db
    db = get_db()
    proj = await db.projects.find_one({"id": project_id}, {"_id":0})
    if not proj:
        raise HTTPException(404, "Project not found")
    # tenant isolation
    if proj.get("user_id") != user.get("id") and user.get("role") != "admin":
        raise HTTPException(403, "Not your project")
    try:
        res = await trigger_autopost_for_project(project_id)
        return res
    except Exception as e:
        raise HTTPException(500, str(e)[:800])
@autopost_router.get("/{project_id}/autopost/status")
async def autopost_status(project_id: str, user=Depends(get_current_user)):
    from ..db import get_db
    db = get_db()
    proj = await db.projects.find_one({"id": project_id}, {"_id":0})
    if not proj:
        raise HTTPException(404, "Project not found")
    if proj.get("user_id") != user.get("id") and user.get("role") != "admin":
        raise HTTPException(403, "Not your project")
    logs = await db.autopost_logs.find({"project_id": project_id}, {"_id":0}).sort("created_at",-1).to_list(5)
    return {"project_id": project_id, "auto_post": proj.get("auto_post"), "platforms": proj.get("platforms"), "results": proj.get("autopost_results"), "logs": logs}
