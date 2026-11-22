"""Release manifest generation — Research G"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ARTIFACT_ROOT = Path(__file__).resolve().parent.parent / "static"


def file_hash(path: Path) -> str:
    # Manifest inputs can include persisted client asset metadata. Never hash
    # arbitrary host paths, directories, or symlink escapes from artifact storage.
    candidate = Path(path).resolve(strict=True)
    root = ARTIFACT_ROOT.resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ValueError("Manifest input is not a stored artifact")
    h = hashlib.sha256()
    with candidate.open("rb") as f:
        for chunk in iter(lambda: f.read(1<<20), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"

def build_manifest(project: dict, job: dict, video_path: Path, thumb_path: Path = None, metadata: dict = None, disclosure: dict = None) -> dict:
    tenant_id = project.get("user_id") or project.get("tenant_id") or "unknown"
    channel_id = project.get("channel_id") or os.getenv("YOUTUBE_CHANNEL_ID", "")
    duration = float(job.get("duration") or project.get("target_duration") or 0)
    # hashes
    vh = file_hash(video_path) if video_path and video_path.exists() else "sha256:missing"
    th = file_hash(thumb_path) if thumb_path and thumb_path.exists() else None
    mh = hashlib.sha256(json.dumps(metadata or {}, sort_keys=True, default=str).encode()).hexdigest()
    mh = f"sha256:{mh}" if metadata else None
    disc = disclosure or project.get("disclosure_decision") or {"contains_synthetic_media": True, "is_altered": False, "rationale": "ElevenLabs voice is synthetic, script/captions are assistance"}
    # Normalize
    if "is_altered" not in disc and "is_realistic_alteration" in disc:
        disc["is_altered"] = disc["is_realistic_alteration"]
    return {
        "tenant_id": tenant_id,
        "channel_id": channel_id,
        "project_id": project.get("id"),
        "job_id": job.get("id"),
        "video_hash": vh,
        "thumbnail_hash": th,
        "metadata_hash": mh,
        "duration": duration,
        "disclosure_decision": disc,
        "qa_version": "v1",
        "policy_version": "2026-09-10",
        "approver": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
