"""Scheduler invocation modes: default stays publish-gated while OFF,
dry_run writes nothing, publish=False renders without ever uploading."""
import pytest

from app.workers import forge_loop


class FakeSettings:
    async def find_one(self, *_a, **_k):
        return {"id": "default", "auto_post_enabled": False}


class FakeUsers:
    async def find_one(self, *_a, **_k):
        return {"id": "admin-1", "role": "admin"}


class FakeScripts:
    async def find_one(self, *_a, **_k):
        return {"full_script": "word " * 40}


class FakeRuns:
    def __init__(self):
        self.rows = []

    async def insert_one(self, doc):
        self.rows.append(dict(doc))


class FakeDB:
    def __init__(self):
        self.forge_settings = FakeSettings()
        self.users = FakeUsers()
        self.scripts = FakeScripts()
        self.forge_runs = FakeRuns()


@pytest.mark.asyncio
async def test_default_invocation_blocked_while_off(monkeypatch):
    monkeypatch.setattr(forge_loop, "get_db", lambda: FakeDB())
    with pytest.raises(Exception) as exc:
        await forge_loop.run_forge_once(
            user_id="admin-1", topic_override="t", niche_override="n")
    assert "OFF" in str(exc.value)


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(forge_loop, "get_db", lambda: db)
    plan = await forge_loop.run_forge_once(
        user_id="admin-1", topic_override="Housing", niche_override="Finance", dry_run=True)
    assert plan["dry_run"] is True
    assert plan["would_publish"] is True
    assert plan["topic"] == "Housing" and plan["owner_id"] == "admin-1"
    assert db.forge_runs.rows == []
    assert "storage_mode" in plan and "tts_mock" in plan


def _stage(name):
    async def _fn(*_a, **_k):
        _fn.calls.append(name)
        if name == "_create_project":
            return {"id": "proj-1"}
        if name == "_generate_script":
            return {"full_script": "word " * 40}
        if name == "_generate_scenes":
            return [{"scene_number": 1}]
        if name == "_ensure_assets_and_render":
            return {"id": "job-1", "status": "completed"}
        return {}
    _fn.calls = []
    return _fn


@pytest.mark.asyncio
async def test_publish_false_renders_without_upload(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(forge_loop, "get_db", lambda: db)
    stages = {}
    for name in ("_create_project", "_generate_script", "_generate_scenes",
                 "_generate_metadata", "_ensure_assets_and_render", "_generate_thumbnail"):
        stages[name] = _stage(name)
        monkeypatch.setattr(forge_loop, name, stages[name])

    async def _boom(*_a, **_k):
        raise AssertionError("publish path must not run")

    for name in ("_youtube_upload", "_videoforge_clip", "_autopost_shorts"):
        monkeypatch.setattr(forge_loop, name, _boom)

    summary = await forge_loop.run_forge_once(
        user_id="admin-1", topic_override="Housing", niche_override="Finance", publish=False)

    ran = [c for s in stages.values() for c in s.calls]
    assert summary["status"] == "completed"
    assert summary["publish_skipped"] is True
    assert summary["youtube_video_id"] is None
    assert len(db.forge_runs.rows) == 1
    assert db.forge_runs.rows[0]["summary"]["publish_skipped"] is True
    assert "_youtube_upload" not in ran
