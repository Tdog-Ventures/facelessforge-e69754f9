"""Render artifact retention + cleanup.

Periodically removes:
  • render MP4s + work dirs older than RENDER_RETENTION_DAYS
  • voiceover audio files for projects no longer in DB
  • thumbnail image files for projects no longer in DB
  • orphaned project directories under static/{thumbs,audio,renders}/

Triggerable manually via /api/admin/retention/run.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .db import get_db
from .storage import get_storage

logger = logging.getLogger("facelessforge.retention")

STATIC_ROOT = Path(__file__).parent.parent / "static"
RENDERS = STATIC_ROOT / "renders"
THUMBS = STATIC_ROOT / "thumbs"
AUDIO = STATIC_ROOT / "audio"


def _retention_days() -> int:
    try:
        return max(1, int(os.environ.get("RENDER_RETENTION_DAYS", "7")))
    except Exception:
        return 7


async def run_cleanup_once() -> dict:
    """Returns a small report of what was removed."""
    db = get_db()
    cutoff_ts = time.time() - _retention_days() * 86400
    report = {
        "renders_removed": 0,
        "render_workdirs_removed": 0,
        "orphan_project_dirs_removed": 0,
        "stale_jobs_marked_failed": 0,
        "remote_objects_removed": 0,
        "bytes_freed": 0,
        "retention_days": _retention_days(),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    store = get_storage()

    # 1. Stale render workdirs (always — these are temporary)
    if RENDERS.exists():
        for proj_dir in RENDERS.iterdir():
            if not proj_dir.is_dir():
                continue
            for w in proj_dir.glob("_work_*"):
                try:
                    age = time.time() - w.stat().st_mtime
                    if age > 3600:  # always purge work dirs older than 1h
                        size = sum(f.stat().st_size for f in w.rglob("*") if f.is_file())
                        shutil.rmtree(w, ignore_errors=True)
                        report["render_workdirs_removed"] += 1
                        report["bytes_freed"] += size
                except Exception as e:  # noqa: BLE001
                    logger.warning("workdir cleanup failed for %s: %s", w, e)

    # 2. Old completed render MP4s — delete files older than retention AND mark
    #    job rows accordingly (output_url cleared, status flagged 'expired_artifact').
    project_ids = {p["id"] async for p in db.projects.find({}, {"_id": 0, "id": 1})}

    if RENDERS.exists():
        for proj_dir in RENDERS.iterdir():
            if not proj_dir.is_dir():
                continue
            # Project deleted? Drop entire dir.
            if proj_dir.name not in project_ids:
                try:
                    size = sum(f.stat().st_size for f in proj_dir.rglob("*") if f.is_file())
                    shutil.rmtree(proj_dir, ignore_errors=True)
                    report["orphan_project_dirs_removed"] += 1
                    report["bytes_freed"] += size
                    await db.render_jobs.delete_many({"project_id": proj_dir.name})
                    continue
                except Exception as e:  # noqa: BLE001
                    logger.warning("orphan render dir cleanup failed: %s", e)
            # Per-file retention
            for mp4 in proj_dir.glob("*.mp4"):
                try:
                    if mp4.stat().st_mtime < cutoff_ts:
                        size = mp4.stat().st_size
                        mp4.unlink()
                        report["renders_removed"] += 1
                        report["bytes_freed"] += size
                        # Job row update
                        job_id = mp4.stem
                        await db.render_jobs.update_one(
                            {"id": job_id, "project_id": proj_dir.name},
                            {"$set": {"status": "expired_artifact",
                                      "output_url": None, "output_path": None,
                                      "current_step": "expired_artifact",
                                      "error_message": "Render artifact removed by retention policy"}},
                        )
                except Exception as e:  # noqa: BLE001
                    logger.warning("mp4 cleanup failed for %s: %s", mp4, e)

    # 2b. Object-storage expiry: for renders past retention with a storage_key,
    # ask the storage backend to delete. Safe — never touches project/user data.
    if store.mode == "object":
        cutoff_dt = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc)
        async for job in db.render_jobs.find(
            {"status": {"$in": ["completed", "COMPLETED"]},
             "storage_mode": "object",
             "storage_key": {"$ne": None},
             "completed_at": {"$lt": cutoff_dt}},
            {"_id": 0, "id": 1, "project_id": 1, "storage_key": 1},
        ):
            try:
                if store.delete(key=job["storage_key"]):
                    report["remote_objects_removed"] += 1
                    await db.render_jobs.update_one(
                        {"id": job["id"]},
                        {"$set": {"status": "expired_artifact",
                                  "output_url": None, "output_path": None,
                                  "storage_key": None,
                                  "current_step": "expired_artifact",
                                  "error_message": "Render artifact removed by retention policy"}},
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("remote retention delete failed for %s: %s", job.get("storage_key"), e)

    # 3. Orphan thumbnail / audio dirs (project deleted)
    for root in (THUMBS, AUDIO):
        if not root.exists():
            continue
        for proj_dir in root.iterdir():
            if not proj_dir.is_dir():
                continue
            if proj_dir.name not in project_ids:
                try:
                    size = sum(f.stat().st_size for f in proj_dir.rglob("*") if f.is_file())
                    shutil.rmtree(proj_dir, ignore_errors=True)
                    report["orphan_project_dirs_removed"] += 1
                    report["bytes_freed"] += size
                except Exception as e:  # noqa: BLE001
                    logger.warning("orphan dir cleanup failed: %s", e)

    # 4. Mark stuck render jobs as failed (older than 1h still in active state)
    one_hour_ago = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc)
    res = await db.render_jobs.update_many(
        {
            "status": {"$in": ["queued", "QUEUED", "validating", "VALIDATING", "preparing_assets", "PREPARING_ASSETS", "rendering", "RENDERING", "verifying", "VERIFYING"]},
            "created_at": {"$lt": one_hour_ago},
        },
        {"$set": {"status": "failed", "current_step": "failed",
                  "error_message": "Job orphaned at boot or by retention sweep",
                  "completed_at": datetime.now(timezone.utc)}},
    )
    report["stale_jobs_marked_failed"] = res.modified_count

    logger.info("Retention sweep: %s", report)
    return report


def disk_usage_report() -> dict:
    """Return on-disk storage footprint per category."""
    out = {"retention_days": _retention_days()}
    for label, path in (("renders", RENDERS), ("thumbnails", THUMBS), ("audio", AUDIO)):
        total = 0
        count = 0
        if path.exists():
            for f in path.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                        count += 1
                    except Exception:
                        pass
        out[label] = {"bytes": total, "files": count}
    return out
