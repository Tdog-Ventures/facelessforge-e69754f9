"""Seed admin + demo creator + 3 sample projects in different statuses."""
import os
import uuid
from datetime import datetime, timezone, timedelta

from .auth import hash_password
from .db import get_db
from . import generation as gen
from .scoring import quality_score, compute_project_status


def _now():
    return datetime.now(timezone.utc)


SAMPLES = [
    {
        "name": "AI Is Quietly Replacing Stock Traders",
        "niche": "finance",
        "topic": "AI-driven high-frequency trading and what retail investors should know",
        "audience": "retail investors 25-45",
        "tone": "calm-authoritative",
        "target_duration": 420,
        "voice_style": "neutral male narrator",
        "visual_style": "cinematic b-roll",
        "monetisation_intent": "ads + affiliate",
        "cta_goal": "subscribe",
        "depth": "full",        # script + scenes + metadata + thumbnails + render
    },
    {
        "name": "The Hidden Psychology of Dark Mode",
        "niche": "design",
        "topic": "Why dark mode changes how users perceive premium software interfaces",
        "audience": "product designers and founders",
        "tone": "curious-expert",
        "target_duration": 300,
        "voice_style": "neutral female narrator",
        "visual_style": "moody minimal",
        "monetisation_intent": "ads",
        "cta_goal": "join newsletter",
        "depth": "scenes",      # script + scenes only
    },
    {
        "name": "What Ancient Rome Knew About Productivity",
        "niche": "history",
        "topic": "Rituals Romans used to structure their day and why it still outperforms modern productivity apps",
        "audience": "knowledge workers",
        "tone": "cinematic",
        "target_duration": 360,
        "voice_style": "deep male narrator",
        "visual_style": "archival + painterly",
        "monetisation_intent": "ads + affiliate",
        "cta_goal": "subscribe",
        "depth": "script",      # only script
    },
]


async def _create_user_if_missing(db, email: str, password: str, name: str, role: str) -> str:
    existing = await db.users.find_one({"email": email})
    if existing:
        return existing["id"]
    uid = str(uuid.uuid4())
    await db.users.insert_one({
        "id": uid, "name": name, "email": email, "role": role,
        "password_hash": hash_password(password),
        "created_at": _now(), "updated_at": _now(),
    })
    return uid


async def _seed_project(db, *, user_id: str, spec: dict, created_offset_days: int):
    existing = await db.projects.find_one({"user_id": user_id, "name": spec["name"]})
    if existing:
        return
    pid = str(uuid.uuid4())
    created = _now() - timedelta(days=created_offset_days)
    project = {
        "id": pid, "user_id": user_id,
        "name": spec["name"], "niche": spec["niche"], "topic": spec["topic"],
        "audience": spec["audience"], "tone": spec["tone"],
        "target_duration": spec["target_duration"],
        "voice_style": spec["voice_style"], "visual_style": spec["visual_style"],
        "monetisation_intent": spec["monetisation_intent"], "cta_goal": spec["cta_goal"],
        "status": "DRAFT", "quality_score": 0, "estimated_cost": 0.0,
        "created_at": created, "updated_at": created,
    }
    await db.projects.insert_one(project)

    depth = spec["depth"]

    # script
    script_data = await gen.generate_script(project)
    script_doc = {"id": str(uuid.uuid4()), "project_id": pid, **script_data,
                  "created_at": created, "updated_at": created}
    await db.scripts.insert_one(script_doc)

    scenes = []
    metadata = None
    if depth in ("scenes", "full"):
        scenes = await gen.generate_scene_plan(project, script_data)
        for sc in scenes:
            sc["project_id"] = pid
            sc["created_at"] = created
            sc["updated_at"] = created
        if scenes:
            await db.scenes.insert_many([dict(s) for s in scenes])

    if depth == "full":
        meta = await gen.generate_metadata(project, script_data, scenes)
        metadata = {"id": str(uuid.uuid4()), "project_id": pid, **meta,
                    "created_at": created, "updated_at": created}
        await db.metadata_packages.insert_one(metadata)

        # thumbnails -> assets
        thumbs = await gen.generate_thumbnail_concepts(project, script_data)
        for i, c in enumerate(thumbs, start=1):
            await db.assets.insert_one({
                "id": str(uuid.uuid4()), "project_id": pid,
                "name": f"Thumbnail Concept #{i}",
                "asset_type": "thumbnail_concept", "file_path": None,
                "source": "seed", "tags": ["thumbnail", "concept"],
                "status": "ready", "brief": c,
                "created_at": created, "updated_at": created,
            })
        await db.render_jobs.insert_one({
            "id": str(uuid.uuid4()), "project_id": pid,
            "status": "COMPLETED", "progress": 100, "current_step": "completed",
            "output_path": f"/exports/{pid}.package.json", "error_message": None,
            "started_at": created, "completed_at": created,
            "created_at": created, "updated_at": created,
        })

    est_cost = 0.08 + (0.06 if depth in ("scenes", "full") else 0) + (0.07 if depth == "full" else 0)
    render_job = None
    if depth == "full":
        render_job = await db.render_jobs.find_one({"project_id": pid}, {"_id": 0})
    score = quality_score(project=project, script=script_doc, scenes=scenes,
                          metadata=metadata, render_job=render_job)
    status = compute_project_status(
        has_script=True, has_scenes=bool(scenes), has_metadata=bool(metadata),
        has_assets=depth == "full", render_status=(render_job or {}).get("status"),
    )
    await db.projects.update_one({"id": pid}, {"$set": {
        "status": status, "quality_score": score, "estimated_cost": round(est_cost, 2),
        "updated_at": _now(),
    }})


async def run_seed():
    db = get_db()
    admin_email = os.environ["ADMIN_EMAIL"]
    admin_password = os.environ["ADMIN_PASSWORD"]
    creator_email = os.environ.get("DEMO_CREATOR_EMAIL", "creator@facelessforge.io")
    creator_password = os.environ.get("DEMO_CREATOR_PASSWORD", "creator123")

    await _create_user_if_missing(db, admin_email, admin_password, "Admin", "admin")
    creator_id = await _create_user_if_missing(db, creator_email, creator_password, "Demo Creator", "creator")

    for i, spec in enumerate(SAMPLES):
        await _seed_project(db, user_id=creator_id, spec=spec, created_offset_days=(i + 1) * 2)
