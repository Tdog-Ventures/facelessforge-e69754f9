"""The retention preview counts deletions and must not perform them."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from app import retention


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def __aiter__(self):
        async def walk():
            for doc in self.docs:
                yield doc
        return walk()


def _match(doc, query):
    for key, cond in (query or {}).items():
        if isinstance(cond, dict):
            if "$in" in cond and doc.get(key) not in cond["$in"]:
                return False
            if "$lt" in cond and (doc.get(key) is None or not doc.get(key) < cond["$lt"]):
                return False
            if "$ne" in cond and (key not in doc or doc.get(key) == cond["$ne"]):
                return False
        elif doc.get(key) != cond:
            return False
    return True


class _Collection:
    def __init__(self, docs):
        self.docs = [dict(doc) for doc in docs]

    def find(self, query, _projection=None):
        return _Cursor([doc for doc in self.docs if _match(doc, query)])

    async def update_one(self, *_args, **_kwargs):
        raise AssertionError("preview updated a document")

    async def update_many(self, *_args, **_kwargs):
        raise AssertionError("preview updated documents")

    async def delete_many(self, *_args, **_kwargs):
        raise AssertionError("preview deleted documents")


class _DB:
    def __init__(self, projects, jobs):
        self.projects = _Collection(projects)
        self.render_jobs = _Collection(jobs)


class _Store:
    mode = "object"

    def delete(self, *_args, **_kwargs):
        raise AssertionError("preview deleted storage")


def _touch(path, age_seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video-bytes")
    stamp = datetime.now(timezone.utc).timestamp() - age_seconds
    os.utime(path, (stamp, stamp))
    if path.parent.name.startswith("_work_"):
        os.utime(path.parent, (stamp, stamp))


@pytest.mark.asyncio
async def test_preview_counts_old_artifacts_without_changing_them(tmp_path, monkeypatch):
    renders = tmp_path / "renders"
    thumbs = tmp_path / "thumbs"
    audio = tmp_path / "audio"
    monkeypatch.setattr(retention, "RENDERS", renders)
    monkeypatch.setattr(retention, "THUMBS", thumbs)
    monkeypatch.setattr(retention, "AUDIO", audio)
    _touch(renders / "live" / "_work_old" / "frame.mp4", 7200)
    _touch(renders / "live" / "_work_new" / "frame.mp4", 60)
    _touch(renders / "live" / "old-job.mp4", 10 * 86400)
    _touch(renders / "live" / "new-job.mp4", 3600)
    _touch(renders / "gone" / "orphan.mp4", 10)
    _touch(thumbs / "gone" / "thumb.jpg", 10)
    _touch(audio / "live" / "voice.mp3", 10)
    _touch(audio / "gone" / "voice.mp3", 10)
    now = datetime.now(timezone.utc)
    jobs = [
        {"id": "stuck", "status": "rendering", "created_at": now - timedelta(hours=2)},
        {"id": "fresh", "status": "queued", "created_at": now},
        {
            "id": "remote",
            "status": "completed",
            "storage_mode": "object",
            "storage_key": "renders/remote.mp4",
            "completed_at": now - timedelta(days=10),
        },
    ]
    db = _DB([{"id": "live"}], jobs)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    jobs_before = [dict(job) for job in jobs]

    monkeypatch.setattr(retention, "get_db", lambda: db)
    monkeypatch.setattr(retention, "get_storage", lambda: _Store())
    report = await retention.preview_cleanup()

    assert report["dry_run"] is True
    assert report["retention_days"] >= 1
    assert report["workdir_max_age_seconds"] == 3600
    assert report["stale_job_max_age_seconds"] == 3600
    assert report["render_workdirs_removed"] == 1
    assert report["renders_removed"] == 1
    assert report["orphan_project_dirs_removed"] == 3
    assert report["stale_jobs_marked_failed"] == 1
    assert report["remote_objects_removed"] == 1
    assert {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert [dict(job) for job in db.render_jobs.docs] == jobs_before
