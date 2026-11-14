"""Voiceover correction must never destroy completed scene work.

Requirements:
- A 48.38s voiceover against a 57.5s narration budget does not delete scenes.
- Existing AI Visual Director plans survive duration correction.
- Attached stock footage and concepts survive.
- A valid adjusted timeline passes preflight.
- An unadjustable timeline fails safely without deleting work.
- Explicit script replacement retains its invalidation behaviour.
- Retrying does not produce duplicate charges or unnecessary voiceovers.
"""
from __future__ import annotations

import pytest

from app import render
from app.routes import api as api_routes


class FakeResult:
    def __init__(self, n=0):
        self.deleted_count = n
        self.modified_count = n
        self.matched_count = n


def _match(doc, query):
    for key, cond in query.items():
        if key == "$or":
            if not any(_match(doc, sub) for sub in cond):
                return False
            continue
        if isinstance(cond, dict) and "$in" in cond:
            if doc.get(key) not in cond["$in"]:
                return False
        elif isinstance(cond, dict):
            for op, arg in cond.items():
                if op == "$exists":
                    if (key in doc) != bool(arg):
                        return False
                elif op == "$lt":
                    if not (doc.get(key) is not None and doc.get(key) < arg):
                        return False
                elif op == "$gte":
                    if not (doc.get(key) is not None and doc.get(key) >= arg):
                        return False
                elif op == "$lte":
                    if not (doc.get(key) is not None and doc.get(key) <= arg):
                        return False
                elif op == "$gt":
                    if not (doc.get(key) is not None and doc.get(key) > arg):
                        return False
                elif op == "$ne":
                    if doc.get(key) == arg:
                        return False
                elif doc.get(key) != arg:
                    return False
        elif doc.get(key) != cond:
            return False
    return True


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    async def find_one(self, query, *_args, **_kwargs):
        for doc in self.docs:
            if _match(doc, query):
                return dict(doc)
        return None

    async def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if _match(doc, query):
                doc.update(update.get("$set", {}))
                for k in update.get("$unset", {}):
                    doc.pop(k, None)
                return FakeResult(1)
        if upsert:
            new_doc = {k: v for k, v in query.items() if not isinstance(v, dict)}
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)
        return FakeResult(0)

    async def update_many(self, query, update):
        n = 0
        for doc in self.docs:
            if _match(doc, query):
                doc.update(update.get("$set", {}))
                n += 1
        return FakeResult(n)

    async def insert_one(self, document):
        self.docs.append(dict(document))
        return FakeResult(1)

    async def delete_many(self, query):
        before = len(self.docs)
        self.docs[:] = [d for d in self.docs if not _match(d, query)]
        return FakeResult(before - len(self.docs))

    async def replace_one(self, query, document, upsert=False):
        for i, doc in enumerate(self.docs):
            if _match(doc, query):
                self.docs[i] = dict(document)
                return FakeResult(1)
        if upsert:
            self.docs.append(dict(document))
        return FakeResult(0)

    def find(self, query=None, *_args, **_kwargs):
        matched = [d for d in self.docs if _match(d, query or {})]
        return _FakeCursor(matched)


class _FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_a, **_k):
        return self

    async def to_list(self, _n):
        return [dict(d) for d in self.docs]


class FakeDB:
    def __init__(self, project, script, assets=None, scenes=None):
        self.projects = FakeCollection([project])
        self.projects.calls = []
        _update = self.projects.update_one

        async def tracked(query, update, **kwargs):
            self.projects.calls.append(("update_one", dict(query), dict(update.get("$set", {}))))
            return await _update(query, update, **kwargs)

        self.projects.update_one = tracked
        self.scripts = FakeCollection([script] if script else [])
        self.assets = FakeCollection(assets)
        self.scenes = FakeCollection(scenes)
        self.metadata_packages = FakeCollection(
            [{"id": "m1", "project_id": project["id"]}])
        self.render_jobs = FakeCollection()


def _project(**over):
    doc = {"id": "p1", "user_id": "u1", "target_duration": 57.5, "status": "DRAFT"}
    doc.update(over)
    return doc


def _script(text, source="ai_generated", sid="s1", rev=1):
    return {"id": sid, "project_id": "p1", "full_script": text, "word_count": len(text.split()),
            "estimated_duration": int(len(text.split()) / 2.5), "source": source,
            "updated_at": "2026-10-11T00:00:00", "content_revision": rev}


def _user():
    return {"id": "u1", "role": "creator", "email": "u1@example.com"}


def _stub_attach(monkeypatch):
    async def stub(_db, _project):
        return {"project": _project, "stub": True}

    monkeypatch.setattr(api_routes, "_attach_project_view", stub)


def _tts_factory(durations, calls):
    async def fake_tts(**kwargs):
        calls.append(dict(kwargs))
        return {"id": kwargs.get("asset_id") or "a-new", "project_id": kwargs.get("project_id"),
                "asset_type": "voiceover_audio", "scene_id": None,
                "duration": durations[min(len(calls) - 1, len(durations) - 1)],
                "mock": False, "provider": "elevenlabs", "voice_style": "narrator",
                "source": "elevenlabs", "name": "Voiceover (Full script)"}

    return fake_tts


def _build_project_with_scenes():
    """57.5s target; two scenes, saved director plan, attached stock + thumbnail."""
    project = _project(target_duration=57.5, content_revision=1)
    script = _script("narration text " * 23)
    assets = [
        {"id": "stock1", "project_id": "p1", "asset_type": "stock_video", "scene_id": "sc1",
         "duration": 27.5, "status": "ready", "mock": False, "external_id": "pexels-1"},
        {"id": "thumb1", "project_id": "p1", "asset_type": "generated_thumbnail",
         "duration": 1.0, "status": "selected", "mock": False},
    ]
    scenes = [
        {"id": "sc1", "project_id": "p1", "scene_number": 1, "duration": 27.5,
         "end_time": 27.5, "narration_text": "narration text " * 23},
        {"id": "sc2", "project_id": "p1", "scene_number": 2, "duration": 30.0,
         "end_time": 57.5, "narration_text": "narration text " * 23},
    ]
    plan = {"enabled": True, "version": 1, "fps": 30, "shots": [
        {"id": "sc1:0", "scene_id": "sc1", "kind": "stock_video",
         "query": "waves rolling on shore", "frames": 825, "narration": "narration text"},
        {"id": "sc2:0", "scene_id": "sc2", "kind": "stock_video",
         "query": "wind in the trees", "frames": 900, "narration": "narration text"},
    ]}
    db = FakeDB(project, script, assets=assets, scenes=scenes)
    for doc in db.projects.docs:
        doc["visual_director"] = plan
    return db


def _plan_saved(db):
    return (db.projects.docs[0].get("visual_director") or {}).get("enabled") is True


@pytest.mark.asyncio
async def test_48s_voiceover_against_57s_budget_keeps_scenes(monkeypatch):
    """Take 1 measures 48.38s vs the 55s narration budget → duration fallback, no wipe."""
    db = _build_project_with_scenes()
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    tts_calls = []
    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover",
                        _tts_factory([48.38], tts_calls))
    gen_calls = []
    monkeypatch.setattr(api_routes.gen, "generate_script",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("LLM correction must not run when scenes exist")))
    _stub_attach(monkeypatch)

    view = await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    assert view["stub"] is True
    # One take generated and kept; no LLM correction, no republish, no wipe.
    assert len(tts_calls) == 1
    assert gen_calls == []
    assert len(db.scenes.docs) == 2, "scenes must not be deleted"
    assert _plan_saved(db), "director plan must survive"
    assert len(db.assets.docs) == 3  # stock + thumbnail + new voiceover, nothing deleted
    project = db.projects.docs[0]
    assert project["target_duration"] == 51  # 48.38s + 2.5s intro -> 51s fits
    fit = project.get("voice_fit") or {}
    assert fit.get("attempts") == 1
    assert fit.get("measured_seconds") == 48.38
    stored = db.scripts.docs[0]
    assert stored["full_script"] == "narration text " * 23


@pytest.mark.asyncio
async def test_duration_fallback_reanchors_timeline_and_passes_preflight(monkeypatch):
    """The fitted target re-anchors scene frames; preflight timing passes."""
    db = _build_project_with_scenes()
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    tts_calls = []
    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover",
                        _tts_factory([48.38], tts_calls))
    gen_calls = []
    monkeypatch.setattr(api_routes.gen, "generate_script",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("LLM correction must not run when scenes exist")))
    _stub_attach(monkeypatch)

    view = await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    assert view["stub"] is True
    assert len(tts_calls) == 1 and gen_calls == []
    project = db.projects.docs[0]
    assert project["target_duration"] == 51

    # Frames re-anchored to the 48.5s narration budget; scene count intact.
    assert len(db.scenes.docs) == 2
    total_frames = sum(int(s.get("duration_frames") or 0) for s in db.scenes.docs)
    assert total_frames == int(round(48.5 * 30)), db.scenes.docs
    for scene in db.scenes.docs:
        assert scene["end_frame"] - scene["start_frame"] == int(scene.get("duration_frames") or 0)

    # Preflight on the fitted project passes timing.
    check = render.validate_prerequisites(
        project, _script("narration text " * 23), db.scenes.docs,
        db.metadata_packages.docs[0], db.assets.docs)
    timing_ok = [c for c in check["checklist"] if c["key"] == "timing"][0]
    assert timing_ok["ok"], timing_ok
    assert render.suggest_duration_autofit(
        project, {"duration": project.get("voice_fit", {}).get("measured_seconds", 48.38)}
    ) is None, "already fitting -> no further suggestion"


@pytest.mark.asyncio
async def test_unadjustable_timeline_fails_safely_without_deleting_work(monkeypatch):
    """4000s VO cannot fit any legal duration -> explicit error, work kept."""
    db = _build_project_with_scenes()
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    tts_calls = []
    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover",
                        _tts_factory([4000.0], tts_calls))
    gen_calls = []
    monkeypatch.setattr(api_routes.gen, "generate_script",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("LLM correction must not run when scenes exist")))
    _stub_attach(monkeypatch)

    with pytest.raises(Exception) as exc:
        await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    assert exc.value.status_code == 400
    assert "voiceover_duration_unfit" in str(exc.value.detail)
    # Work kept: scenes, stock, plan, script all untouched, target unchanged.
    assert len(db.scenes.docs) == 2
    assert _plan_saved(db)
    assert len(db.assets.docs) == 2  # no voiceover asset persisted on the hard failure
    project = db.projects.docs[0]
    assert project["target_duration"] == 57.5
    assert db.scripts.docs[0]["full_script"] == "narration text " * 23


@pytest.mark.asyncio
async def test_explicit_script_replacement_still_invalidates_derivatives(monkeypatch):
    """Explicit full-script replacement triggers publish path (invalidates derived work)."""
    from app.models import ScriptUpdate
    db = _build_project_with_scenes()
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    _stub_attach(monkeypatch)

    view = await api_routes.update_script(
        "p1", body=ScriptUpdate(full_script="completely different new script narration " * 20),
        user=_user())
    assert view["stub"] is True
    # Explicit replacement invalidates derived scenes (kept behaviour).
    assert len(db.scenes.docs) == 0
    project = db.projects.docs[0]
    assert project["target_duration"] == 57.5
    assert db.scripts.docs[0]["full_script"].startswith("completely different")


@pytest.mark.asyncio
async def test_retry_reuses_selected_take_no_duplicate_charge(monkeypatch):
    """A valid selected take is reused; retry makes no provider calls."""
    project = _project(target_duration=57.5, selected_voiceover_asset_id="v1")
    script = _script("narration text " * 23)
    assets = [
        {"id": "v1", "project_id": "p1", "asset_type": "voiceover_audio", "scene_id": None,
         "duration": 55.0, "mock": False, "provider": "elevenlabs",
         "voice_style": "narrator", "tone": None, "status": "selected"},
    ]
    db = FakeDB(project, script, assets=assets)
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    tts_calls = []
    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover",
                        _tts_factory([55.0], tts_calls))
    gen_calls = []
    monkeypatch.setattr(api_routes.gen, "generate_script",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("no generation expected on reuse")))
    _stub_attach(monkeypatch)

    first = await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    assert first["stub"] is True
    assert tts_calls == []  # reused, no provider call
    second = await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    assert second["stub"] is True
    assert tts_calls == []  # retry still reuses; never duplicated
    project = db.projects.docs[0]
    assert project["selected_voiceover_asset_id"] == "v1"


@pytest.mark.asyncio
async def test_voice_fit_recorded_after_duration_fallback(monkeypatch):
    """voice_fit persists attempts and measured duration after a fitted fallback."""
    db = _build_project_with_scenes()
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    tts_calls = []
    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover",
                        _tts_factory([48.38], tts_calls))
    gen_calls = []
    monkeypatch.setattr(api_routes.gen, "generate_script",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            AssertionError("no correction when scenes exist")))
    _stub_attach(monkeypatch)

    await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    project = db.projects.docs[0]
    fit = project.get("voice_fit") or {}
    assert fit.get("attempts") == 1
    assert fit.get("measured_seconds") == 48.38
