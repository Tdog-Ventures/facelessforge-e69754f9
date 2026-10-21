"""Manual script entry: create/update without LLM, ownership enforced,
safe invalidation of dependent drafts, master MP4s never touched."""
import pytest

from app.models import ScriptUpdate
from app.routes import api as api_routes
from app import generation as gen_module


def _match(doc, query):
    for key, cond in query.items():
        if isinstance(cond, dict) and "$in" in cond:
            if doc.get(key) not in cond["$in"]:
                return False
        elif doc.get(key) != cond:
            return False
    return True


class FakeResult:
    def __init__(self, n=0):
        self.deleted_count = n


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]
        self.calls = []

    async def find_one(self, query, *_args, **_kwargs):
        self.calls.append(("find_one", dict(query)))
        for doc in self.docs:
            if _match(doc, query):
                return dict(doc)
        return None

    async def update_one(self, query, update, upsert=False):
        self.calls.append(("update_one", dict(query)))
        for doc in self.docs:
            if _match(doc, query):
                doc.update(update.get("$set", {}))
                return FakeResult(1)
        if upsert:
            new_doc = {k: v for k, v in query.items() if not isinstance(v, dict)}
            new_doc.update(update.get("$set", {}))
            for k, v in update.get("$setOnInsert", {}).items():
                new_doc.setdefault(k, v)
            self.docs.append(new_doc)
            return FakeResult(0)
        return FakeResult(0)

    async def insert_one(self, document):
        self.calls.append(("insert_one",))
        self.docs.append(dict(document))
        return FakeResult(1)

    async def delete_many(self, query):
        self.calls.append(("delete_many", dict(query)))
        before = len(self.docs)
        self.docs[:] = [d for d in self.docs if not _match(d, query)]
        return FakeResult(before - len(self.docs))

    async def replace_one(self, query, document, upsert=False):
        self.calls.append(("replace_one", dict(query)))
        for i, doc in enumerate(self.docs):
            if _match(doc, query):
                self.docs[i] = dict(document)
                return FakeResult(1)
        if upsert:
            self.docs.append(dict(document))
            return FakeResult(0)
        return FakeResult(0)


class FakeDB:
    def __init__(self, project, scripts=None, scenes=None, metadata=None, assets=None, jobs=None):
        self.projects = FakeCollection([project])
        self.scripts = FakeCollection(scripts)
        self.scenes = FakeCollection(scenes)
        self.metadata_packages = FakeCollection(metadata)
        self.assets = FakeCollection(assets)
        self.render_jobs = FakeCollection(jobs)
        self.cost_logs = FakeCollection()


def _user(uid="user-1", role="creator"):
    return {"id": uid, "role": role, "email": f"{uid}@example.com"}


def _project(pid="project-1", uid="user-1", **over):
    doc = {"id": pid, "user_id": uid, "target_duration": 30, "status": "DRAFT"}
    doc.update(over)
    return doc


async def _patch(db, pid, payload, user):
    orig_attach = api_routes._attach_project_view

    async def stub_attach(_db, _project):
        script = await _db.scripts.find_one({"project_id": pid}, {"_id": 0})
        return {"project": _project, "script": script}

    api_routes._attach_project_view = stub_attach
    try:
        return await api_routes.update_script(pid, ScriptUpdate(**payload), user=user)
    finally:
        api_routes._attach_project_view = orig_attach


def _llm_guard(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("LLM provider must not be called for manual save")

    monkeypatch.setattr(gen_module, "generate_script", _boom)
    monkeypatch.setattr(api_routes.gen, "generate_script", _boom)


@pytest.mark.asyncio
async def test_manual_script_create_without_llm(monkeypatch):
    _llm_guard(monkeypatch)
    db = FakeDB(_project())
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    text = "word " * 60
    view = await _patch(db, "project-1", {"full_script": text, "source": "manual"}, _user())

    assert view["script"] is not None
    assert view["script"]["source"] == "manual"
    assert view["script"]["word_count"] == 60
    assert view["script"]["estimated_duration"] == 24
    assert view["script"]["id"]
    assert "script_invalidated" not in view  # nothing to invalidate on first save


@pytest.mark.asyncio
async def test_manual_script_update(monkeypatch):
    _llm_guard(monkeypatch)
    old = {"id": "s1", "project_id": "project-1", "full_script": "word " * 60,
           "word_count": 60, "source": "manual"}
    db = FakeDB(_project(), scripts=[old])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    view = await _patch(db, "project-1", {"full_script": "term " * 40}, _user())

    assert view["script"]["word_count"] == 40
    assert view["script"]["source"] == "manual"  # preserved when not specified


@pytest.mark.asyncio
async def test_cross_user_script_write_forbidden(monkeypatch):
    db = FakeDB(_project(uid="owner-1"))
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    with pytest.raises(Exception) as exc:
        await _patch(db, "project-1", {"full_script": "word " * 30}, _user("intruder-9"))
    assert getattr(exc.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_manual_script_validation(monkeypatch):
    db = FakeDB(_project())
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    with pytest.raises(Exception) as exc:
        await _patch(db, "project-1", {"full_script": "too short"}, _user())
    assert getattr(exc.value, "status_code", None) == 422
    with pytest.raises(Exception) as exc:
        await _patch(db, "project-1", {"full_script": "x" * 20001}, _user())
    assert getattr(exc.value, "status_code", None) == 422
    with pytest.raises(Exception) as exc:
        await _patch(db, "project-1", {"full_script": "word " * 30, "source": "bogus"}, _user())
    assert getattr(exc.value, "status_code", None) == 422


@pytest.mark.asyncio
async def test_material_change_invalidates_drafts_not_masters(monkeypatch):
    _llm_guard(monkeypatch)
    old = {"id": "s1", "project_id": "project-1", "full_script": "old text here " * 10,
           "word_count": 30, "source": "ai_generated",
           "selected_hook": "Zapier connects everything",
           "retention_beats": ["Zapier connects everything smoothly"],
           "cta_block": "Book a Zapier demo"}
    scenes = [{"id": "sc1", "project_id": "project-1"}]
    metadata = [{"id": "m1", "project_id": "project-1"}]
    assets = [
        {"id": "v1", "project_id": "project-1", "asset_type": "voiceover_audio"},
        {"id": "st1", "project_id": "project-1", "asset_type": "stock_video", "scene_id": "sc1"},
        {"id": "t1", "project_id": "project-1", "asset_type": "generated_thumbnail"},
    ]
    jobs = [{"id": "j1", "project_id": "project-1", "status": "completed",
             "output_storage_key": "renders/project-1/j1.mp4"}]
    db = FakeDB(
        _project(selected_voiceover_asset_id="v1"),
        scripts=[old], scenes=scenes, metadata=metadata, assets=assets, jobs=jobs,
    )
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    view = await _patch(db, "project-1", {"full_script": "brand new narration " * 20}, _user())

    assert db.scenes.docs == []
    assert db.metadata_packages.docs == []
    remaining_types = sorted(a["asset_type"] for a in db.assets.docs)
    assert remaining_types == ["generated_thumbnail"]
    assert db.render_jobs.docs != []  # master history preserved
    assert db.render_jobs.docs[0]["output_storage_key"] == "renders/project-1/j1.mp4"
    assert all(c[0] != "delete_many" for c in db.render_jobs.calls)
    assert db.projects.docs[0].get("selected_voiceover_asset_id") is None
    assert view["script"]["selected_hook"] == ""
    assert view["script"]["retention_beats"] == []
    assert view["script"]["cta_block"] == ""
    assert view["script_notice"].startswith("Script updated")
    assert view["script_invalidated"] == {"scenes": 1, "metadata": 1, "assets": 2}


@pytest.mark.asyncio
async def test_hook_only_edit_does_not_invalidate(monkeypatch):
    _llm_guard(monkeypatch)
    old = {"id": "s1", "project_id": "project-1", "full_script": "word " * 30,
           "word_count": 30, "source": "manual"}
    db = FakeDB(_project(), scripts=[old], scenes=[{"id": "sc1", "project_id": "project-1"}])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    view = await _patch(db, "project-1", {"selected_hook": "A better hook"}, _user())

    assert db.scenes.docs != []
    assert "script_invalidated" not in view


@pytest.mark.asyncio
async def test_ai_generate_stamps_source(monkeypatch):
    async def fake_generate(_project):
        return {"full_script": "word " * 50, "word_count": 50}

    async def fake_log(*_a, **_k):
        return None

    monkeypatch.setattr(api_routes.gen, "generate_script", fake_generate)
    monkeypatch.setattr(api_routes, "_log_cost", fake_log)
    monkeypatch.setattr(api_routes, "check_credits", None)
    db = FakeDB(_project())
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    orig_attach = api_routes._attach_project_view

    async def stub_attach(_db, _project):
        return {"script": await _db.scripts.find_one({"project_id": "project-1"}, {"_id": 0})}

    api_routes._attach_project_view = stub_attach
    try:
        view = await api_routes.generate_script_endpoint("project-1", user=_user())
    finally:
        api_routes._attach_project_view = orig_attach

    assert view["script"]["source"] == "ai_generated"
