"""Forge-path pre-queue fit gate: a drifting voiceover must fail fast with
numbers BEFORE render credits/compute are spent. Uses measured VO duration."""
import pytest

from app.workers import forge_loop
from app import render as render_module
from app import tts as tts_module


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_a, **_k):
        return self

    async def to_list(self, _n):
        return [dict(d) for d in self.docs]


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]
        self.calls = []

    async def find_one(self, query, *_a, **_k):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items() if not isinstance(v, dict)):
                return dict(doc)
        return None

    async def update_one(self, query, update, upsert=False):
        self.calls.append(("update_one", dict(query)))
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items() if not isinstance(v, dict)):
                doc.update(update.get("$set", {}))
                return
        if upsert:
            self.docs.append({**{k: v for k, v in query.items() if not isinstance(v, dict)},
                              **update.get("$set", {})})

    async def insert_one(self, document):
        self.docs.append(dict(document))

    def find(self, query=None, *_args, **_kwargs):
        matched = [d for d in self.docs
                   if all(doc.get(k) == v for k, v in (query or {}).items()
                          if not isinstance(v, dict))]
        return _FakeCursor(matched)


class _FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_a, **_k):
        return self

    async def to_list(self, _n):
        return [dict(d) for d in self.docs]


class FakeDB:
    def __init__(self, project, script_text, render_status="completed"):
        self.projects = FakeCollection([project])
        self.scripts = FakeCollection([{"id": "s1", "project_id": project["id"],
                                        "full_script": script_text}])
        self.assets = FakeCollection()
        self.scenes = FakeCollection()
        self.render_jobs = FakeCollection(
            [{"id": "job-1", "status": render_status}] if render_status else [])


def _project(**over):
    doc = {"id": "p1", "user_id": "u1", "target_duration": 30, "status": "DRAFT"}
    doc.update(over)
    return doc


def _wire(monkeypatch, db, vo_duration, queued):
    # _ensure_assets_and_render re-imports get_db from app.db inside its body,
    # so patch the canonical source — never the real database in tests.
    import app.db as _db_module
    monkeypatch.setattr(_db_module, "get_db", lambda: db)
    monkeypatch.setattr(forge_loop, "get_db", lambda: db)

    async def _concepts(*_a, **_k):
        return []

    monkeypatch.setattr(forge_loop, "gen_generate_thumbnail_concepts", _concepts)

    async def fake_tts(**kwargs):
        return {"id": kwargs.get("asset_id"), "duration": vo_duration,
                "mock": False, "provider": "elevenlabs"}

    monkeypatch.setattr(tts_module, "generate_voiceover", fake_tts)

    async def fake_queue(pid, **_k):
        queued.append(pid)
        return {"id": "job-1", "status": "queued"}

    monkeypatch.setattr(render_module, "queue_render", fake_queue)


@pytest.mark.asyncio
async def test_drifted_voiceover_fails_before_queue(monkeypatch):
    db = FakeDB(_project(), "word " * 60)
    queued = []
    _wire(monkeypatch, db, 18.0, queued)
    with pytest.raises(Exception) as exc:
        await forge_loop._ensure_assets_and_render(_project())
    assert "Voiceover is 18.00s" in str(exc.value)
    assert "27.50s" in str(exc.value)
    assert queued == []  # no render credits spent


@pytest.mark.asyncio
async def test_fitting_voiceover_proceeds_to_queue(monkeypatch):
    db = FakeDB(_project(), "word " * 70)
    queued = []
    _wire(monkeypatch, db, 27.2, queued)
    job = await forge_loop._ensure_assets_and_render(_project())
    assert job["status"] == "completed"
    assert queued == ["p1"]
