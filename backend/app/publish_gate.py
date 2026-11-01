"""Shared, artifact-bound publication enforcement boundary.

Every public YouTube upload channel -- external auto-upload, operator manual
upload, the forge pipeline and the autopost adapter -- clears the same render QA
gate and publishes the exact artifact bound to the approved render job.

A completed render is not permission to publish. A render can finish while its
deterministic QA gates are blocked, so publication requires an explicitly
release-eligible job, no deterministic QA blocks, and a non-blocked step. The
published file must be the approved job's own artifact (or the renderer-retained
verification copy of that same job), never an arbitrary project-wide file found
by scanning a directory.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# Mirrors render.STATIC_RENDERS without importing render (avoids import cycles).
STATIC_RENDERS = Path(__file__).parent.parent / "static" / "renders"


class PublicationBlocked(RuntimeError):
    """Publication refused: the render is not approved for public release."""


def assert_publishable(job: Optional[dict]) -> None:
    """Refuse publication unless the render job cleared every release gate.

    Same semantics as the external auto-upload gate, so no channel can be
    weaker than another. A non-completed or expired job is never publishable.
    """
    if not job:
        raise PublicationBlocked("YouTube upload blocked: no approved render job")
    if job.get("status") not in ("completed", "COMPLETED"):
        raise PublicationBlocked("YouTube upload blocked: render job is not completed")
    if (job.get("release_eligible") is not True
            or job.get("deterministic_qa_blocked")
            or job.get("current_step") == "qa_blocked"):
        raise PublicationBlocked("YouTube upload blocked by render QA")


def resolve_approved_artifact(*, job: dict, project_id: str,
                              static_renders: Optional[Path] = None) -> Path:
    """Return the approved job's own artifact, never a project-wide file scan."""
    root = Path(static_renders) if static_renders is not None else STATIC_RENDERS
    output_path = job.get("output_path")
    if output_path:
        candidate = Path(output_path)
        if candidate.is_file():
            return candidate
    job_id = job.get("id")
    if job_id:
        # Object storage removes the upload source; the renderer retains this
        # exact job's verification copy.
        retained = root / project_id / f"{job_id}_verify.mp4"
        if retained.is_file():
            return retained
    raise PublicationBlocked("YouTube upload blocked: approved artifact unavailable")


async def find_approved_job(*, db, project_id: str) -> Optional[dict]:
    """Find the render job bound to this project.

    The renderer records the authoritative job on the project document, so
    prefer that binding. Fall back to the newest completed job, which still
    fails closed on the QA gate of whichever job is found.
    """
    project = await db.projects.find_one(
        {"id": project_id}, {"_id": 0, "rendered_video_asset_id": 1})
    job_id = (project or {}).get("rendered_video_asset_id")
    jobs = getattr(db, "render_jobs", None)
    if jobs is None:
        return None
    if job_id:
        job = await jobs.find_one({"id": job_id, "project_id": project_id}, {"_id": 0})
        if job:
            return job
    return await jobs.find_one(
        {"project_id": project_id, "status": {"$in": ["completed", "COMPLETED"]}},
        {"_id": 0}, sort=[("created_at", -1)])
