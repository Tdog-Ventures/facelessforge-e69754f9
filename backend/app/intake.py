"""
Intake orchestrator — chains the full pipeline behind one call.
Reuses the real endpoint functions from routes.py so behavior stays identical
to clicking through the UI manually; only the sequencing is new.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException

from .auth import get_current_user
from .db import get_db
from .models import ProjectCreate, AutoAttachRequest, GenerateThumbnailImagesRequest, GenerateVoiceoverRequest
from .render import queue_render
from .routes import (
    create_project,
    generate_script_endpoint,
    generate_scenes_endpoint,
    generate_metadata_endpoint,
    generate_thumbnails_endpoint,
    generate_thumbnail_image_endpoint,
    select_thumbnail,
    generate_full_voiceover,
    select_voiceover,
    auto_attach_assets,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/intake")
async def intake(body: ProjectCreate, user=Depends(get_current_user)):
    project = await create_project(body, user=user)
    project_id = project["id"]

    try:
        await generate_script_endpoint(project_id, user=user)
        await generate_scenes_endpoint(project_id, user=user)
        await generate_metadata_endpoint(project_id, user=user)
        await auto_attach_assets(project_id, AutoAttachRequest(), user=user)

        # Thumbnails: generate concepts, render one image, select it.
        await generate_thumbnails_endpoint(project_id, user=user)
        db = get_db()
        concept = await db.assets.find_one(
            {"project_id": project_id, "asset_type": "thumbnail_concept"},
            {"_id": 0},
            sort=[("created_at", -1)],
        )
        if concept:
            await generate_thumbnail_image_endpoint(
                project_id, concept["id"], GenerateThumbnailImagesRequest(variants=1), user=user
            )
            generated_thumb = await db.assets.find_one(
                {"project_id": project_id, "asset_type": "generated_thumbnail"},
                {"_id": 0},
                sort=[("created_at", -1)],
            )
            if generated_thumb:
                await select_thumbnail(project_id, generated_thumb["id"], user=user)

        # Voiceover: generate full-script, select it.
        await generate_full_voiceover(project_id, GenerateVoiceoverRequest(), user=user)
        voiceover = await db.assets.find_one(
            {"project_id": project_id, "asset_type": "voiceover_audio", "scene_id": None},
            {"_id": 0},
            sort=[("created_at", -1)],
        )
        if voiceover:
            await select_voiceover(project_id, voiceover["id"], user=user)

        job = await queue_render(project_id, requested_by=user["id"])
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Intake pipeline failed for project %s", project_id)
        db = get_db()
        await db.projects.update_one(
            {"id": project_id}, {"$set": {"status": "FAILED", "error": str(e)}}
        )
        raise HTTPException(status_code=500, detail=f"Pipeline failed at project {project_id}: {e}")

    return {"project_id": project_id, "job_id": job["id"], "status": "queued"}


@router.get("/my-videos")
async def my_videos(user=Depends(get_current_user)):
    db = get_db()
    projects = (
        await db.projects.find({"user_id": user["id"]}, {"_id": 0})
        .sort("created_at", -1)
        .to_list(200)
    )

    results = []
    for p in projects:
        jobs = (
            await db.render_jobs.find({"project_id": p["id"]}, {"_id": 0})
            .sort("created_at", -1)
            .to_list(1)
        )
        latest_job = jobs[0] if jobs else None
        results.append(
            {
                "project_id": p["id"],
                "name": p.get("name"),
                "topic": p.get("topic"),
                "status": latest_job["status"] if latest_job else p.get("status"),
                "progress": latest_job.get("progress") if latest_job else None,
                "output_url": latest_job.get("output_url") if latest_job else None,
                "error_message": latest_job.get("error_message") if latest_job else p.get("error"),
                "created_at": p.get("created_at"),
            }
        )
    return results
