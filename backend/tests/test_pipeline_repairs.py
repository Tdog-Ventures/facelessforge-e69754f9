"""Repairs from the render-pipeline audit.

No provider calls. Scene voiceover must not move the selected duration,
script regeneration must drop stale scenes, chapters use the UI field names,
and a failed voiceover concat must not become a silent successful render.
"""
import pytest

from app.billing_dodo import scale_credits_for_checkout
from app.generation import chapters_from_scenes, normalize_chapters
from app.routes import api as api_routes
from app.timing import contiguous_timeline_from_durations
from app import render


def _match(doc, query):
    for key, cond in query.items():
        if key == "$or":
            if not any(_match(doc, sub) for sub in cond):
                return False
            continue
        if isinstance(cond, dict) and "$exists" in cond:
            if bool(cond["$exists"]) != (key in doc):
                return False
            continue
        if isinstance(cond, dict) and "$in" in cond:
            if doc.get(key) not in cond["$in"]:
                return False
            continue
        if isinstance(cond, dict) and "$lt" in cond:
            value = doc.get(key)
            if value is None or not value < cond["$lt"]:
                return False
            continue
        if doc.get(key) != cond and not (cond is None and key not in doc):
            return False
    return True


class FakeResult:
    def __init__(self, n=0):
        self.deleted_count = n
        self.matched_count = n


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    async def find_one(self, query, *_args, **_kwargs):
        for doc in self.docs:
            if _match(doc, query):
                return dict(doc)
        return None

    def find(self, query=None, *_args, **_kwargs):
        matched = [d for d in self.docs if _match(d, query or {})]
        return _Cursor(matched)

    async def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if _match(doc, query):
                doc.update(update.get("$set", {}))
                for key in update.get("$unset", {}):
                    doc.pop(key, None)
                return FakeResult(1)
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


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_a, **_k):
        return self

    async def to_list(self, _n):
        return [dict(d) for d in self.docs]


class FakeDB:
    def __init__(self, project, scripts=None, scenes=None, assets=None):
        self.projects = FakeCollection([project])
        self.scripts = FakeCollection(scripts)
        self.scenes = FakeCollection(scenes)
        self.metadata_packages = FakeCollection()
        self.assets = FakeCollection(assets)
        self.rights_registry = FakeCollection()
        self.render_jobs = FakeCollection()


def test_zero_start_without_duration_uses_the_end_time():
    repaired = contiguous_timeline_from_durations([
        {"id": "s1", "scene_number": 1, "start_time": 0, "end_time": 10, "start_frame": 999, "end_frame": 999, "duration_frames": 999},
        {"id": "s2", "scene_number": 2, "duration": 5, "start_frame": 1, "end_frame": 2, "duration_frames": 1},
    ])
    assert repaired[0]["duration"] == 10
    assert repaired[0]["end_time"] == 10
    assert repaired[0]["start_frame"] == 0
    assert repaired[0]["end_frame"] == 300
    assert repaired[0]["duration_frames"] == 300
    assert repaired[1]["start_time"] == 10
    assert repaired[1]["start_frame"] == 300


def test_overlapped_voiceover_ranges_become_contiguous_without_touching_target():
    scenes = [
        {"id": "s1", "scene_number": 1, "start_time": 0, "end_time": 31, "duration": 31, "narration_text": "boil"},
        {"id": "s2", "scene_number": 2, "start_time": 26, "end_time": 59, "duration": 33, "narration_text": "scramble"},
        {"id": "s3", "scene_number": 3, "start_time": 53, "end_time": 90, "duration": 36, "narration_text": "fry"},
        {"id": "s4", "scene_number": 4, "start_time": 82, "end_time": 118, "duration": 36, "narration_text": "poach"},
    ]
    repaired = contiguous_timeline_from_durations(scenes)
    starts = [row["start_time"] for row in repaired]
    ends = [row["end_time"] for row in repaired]
    assert starts == [0, 31, 64, 100]
    assert ends == [31, 64, 100, 136]
    assert repaired[0]["narration_text"] == "boil"
    assert ends[-1] == sum(row["duration"] for row in repaired)


def test_chapters_from_time_label_match_the_metadata_ui():
    assert normalize_chapters([{"time": "0:00", "label": "Intro"}]) == [
        {"timestamp": "0:00", "title": "Intro"},
    ]
    built = chapters_from_scenes([
        {"start_time": 0, "caption_text": "Boil"},
        {"start_time": 82, "narration_text": "Poach the egg gently now please"},
    ])
    assert built[0] == {"timestamp": "0:00", "title": "Boil"}
    assert built[1]["timestamp"] == "1:22"
    assert built[1]["title"].startswith("Poach the egg")


def test_pending_checkout_credits_are_not_scaled_twice():
    assert scale_credits_for_checkout(pending={"credits": "200"}, quantity=2, unit_credits=100) == 200
    assert scale_credits_for_checkout(pending={}, quantity=2, unit_credits=100) == 200
    assert scale_credits_for_checkout(pending=None, quantity=2, unit_credits=100) == 200


def test_billing_credits_route_is_mounted():
    paths = {getattr(route, "path", None) for route in api_routes.router.routes}
    assert "/api/billing/credits" in paths


@pytest.mark.asyncio
async def test_billing_credits_returns_operator_unmetered(monkeypatch):
    async def status(_user):
        return {"operator_unmetered": True, "remaining": 0, "monthly": 150, "plan": "free"}

    monkeypatch.setattr("app.billing.get_billing_status_for_user", status)
    payload = await api_routes.billing_credits(user={"id": "admin"})
    assert payload["operator_unmetered"] is True


@pytest.mark.asyncio
async def test_scene_voiceover_does_not_overlap_or_replace_target(monkeypatch):
    scenes = [
        {"id": "s1", "project_id": "p1", "scene_number": 1, "start_time": 0, "end_time": 30, "duration": 30, "narration_text": "one"},
        {"id": "s2", "project_id": "p1", "scene_number": 2, "start_time": 30, "end_time": 60, "duration": 30, "narration_text": "two"},
    ]
    project = {"id": "p1", "user_id": "u1", "target_duration": 90, "status": "SCENES_GENERATED"}
    db = FakeDB(project, scenes=scenes)
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    async def fake_tts(**kwargs):
        return {"id": kwargs["asset_id"], "duration": 40.0, "mock": True, "provider": "elevenlabs", "source": "elevenlabs"}

    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover", fake_tts)

    async def stub(_db, _project):
        return {"project": dict(_project)}

    monkeypatch.setattr(api_routes, "_attach_project_view", stub)
    await api_routes.generate_scene_voiceover("p1", "s1", body=None, user={"id": "u1", "role": "creator"})
    stored = {row["id"]: row for row in db.scenes.docs}
    assert stored["s1"]["start_time"] == 0 and stored["s1"]["end_time"] == 30
    assert stored["s2"]["start_time"] == 30 and stored["s2"]["end_time"] == 60
    assert db.projects.docs[0]["target_duration"] == 90
    assert db.assets.docs[0]["duration"] == 40.0


@pytest.mark.asyncio
async def test_generate_script_clears_stale_scenes(monkeypatch):
    project = {"id": "p1", "user_id": "u1", "target_duration": 90, "estimated_cost": 0}
    old = {"id": "script-1", "project_id": "p1", "full_script": "old narration " * 20, "source": "ai_generated"}
    db = FakeDB(project, scripts=[old], scenes=[{"id": "s1", "project_id": "p1", "narration_text": "old"}],
                assets=[{"id": "v1", "project_id": "p1", "asset_type": "voiceover_audio"}])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(api_routes, "check_credits", None)

    async def fake_gen(_project):
        return {"full_script": "brand new narration " * 20, "word_count": 60}

    async def fake_log(*_a, **_k):
        return None

    monkeypatch.setattr(api_routes.gen, "generate_script", fake_gen)
    monkeypatch.setattr(api_routes, "_log_cost", fake_log)

    async def stub(_db, _project):
        script = await _db.scripts.find_one({"project_id": "p1"})
        return {"project": dict(_project), "script": script}

    monkeypatch.setattr(api_routes, "_attach_project_view", stub)
    view = await api_routes.generate_script_endpoint("p1", user={"id": "u1", "role": "creator"})
    assert db.scenes.docs == []
    assert db.assets.docs == []
    assert view["script_invalidated"]["scenes"] == 1
    assert "regenerate" in view["script_notice"].lower()


@pytest.mark.asyncio
async def test_failed_script_write_does_not_delete_scenes(monkeypatch):
    project = {"id": "p1", "user_id": "u1", "target_duration": 90, "estimated_cost": 0}
    old = {"id": "script-1", "project_id": "p1", "full_script": "old narration " * 20}
    db = FakeDB(project, scripts=[old], scenes=[{"id": "s1", "project_id": "p1"}])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(api_routes, "check_credits", None)

    async def fake_gen(_project):
        return {"full_script": "brand new narration " * 20, "word_count": 60}

    async def boom(*_a, **_k):
        raise RuntimeError("script write failed")

    monkeypatch.setattr(api_routes.gen, "generate_script", fake_gen)
    monkeypatch.setattr(api_routes, "_log_cost", lambda *_a, **_k: None)
    db.scripts.replace_one = boom
    with pytest.raises(RuntimeError, match="script write failed"):
        await api_routes.generate_script_endpoint("p1", user={"id": "u1", "role": "creator"})
    assert db.scenes.docs == [{"id": "s1", "project_id": "p1"}]


@pytest.mark.asyncio
async def test_busy_asset_lease_restores_the_previous_script(monkeypatch):
    project = {"id": "p1", "user_id": "u1", "target_duration": 90, "estimated_cost": 0}
    old = {"id": "script-1", "project_id": "p1", "full_script": "old narration " * 20}
    db = FakeDB(project, scripts=[old], scenes=[{"id": "s1", "project_id": "p1"}])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(api_routes, "check_credits", None)
    monkeypatch.setattr(api_routes.gen, "generate_script", lambda _project: _async_script())
    monkeypatch.setattr(api_routes, "_log_cost", lambda *_a, **_k: None)

    async def busy(*_a, **_k):
        return None

    monkeypatch.setattr("app.project_lease.acquire", busy)
    with pytest.raises(Exception) as exc:
        await api_routes.generate_script_endpoint("p1", user={"id": "u1", "role": "creator"})
    assert exc.value.status_code == 409
    assert db.scenes.docs
    assert db.scripts.docs[0]["full_script"] == old["full_script"]


async def _async_script():
    return {"full_script": "brand new narration " * 20, "word_count": 60}


@pytest.mark.asyncio
async def test_metadata_patch_stores_chapter_fields(monkeypatch):
    from app.models import MetadataUpdate
    db = FakeDB({"id": "p1", "user_id": "u1"})
    db.metadata_packages.docs.append({"id": "m", "project_id": "p1", "chapters": []})
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    async def stub(_db, project):
        return {"project": project}

    monkeypatch.setattr(api_routes, "_attach_project_view", stub)
    await api_routes.update_metadata(
        "p1",
        MetadataUpdate(chapters=[{"time": "0:00", "label": "Intro"}]),
        user={"id": "u1", "role": "creator"},
    )
    assert db.metadata_packages.docs[0]["chapters"] == [{"timestamp": "0:00", "title": "Intro"}]


@pytest.mark.asyncio
async def test_voiceover_concat_reencodes_and_failed_concat_raises(tmp_path, monkeypatch):
    left = tmp_path / "a.mp3"
    right = tmp_path / "b.mp3"
    left.write_bytes(b"left")
    right.write_bytes(b"right")
    scenes = [{"id": "s1", "scene_number": 1}, {"id": "s2", "scene_number": 2}]
    assets = [
        {"id": "v1", "asset_type": "voiceover_audio", "scene_id": "s1", "status": "selected", "source": "elevenlabs"},
        {"id": "v2", "asset_type": "voiceover_audio", "scene_id": "s2", "status": "selected", "source": "elevenlabs"},
    ]

    async def fake_local(_asset, _work, name):
        return left if name.endswith("000") else right

    calls = []

    async def ok_ffmpeg(cmd, timeout=600):
        calls.append(cmd)
        out = __import__("pathlib").Path(cmd[-1])
        out.write_bytes(b"RIFF")
        return True, ""

    monkeypatch.setattr(render, "_ensure_audio_local", fake_local)
    monkeypatch.setattr(render, "_run_ffmpeg", ok_ffmpeg)
    produced = await render._resolve_audio({"id": "p"}, scenes, assets, tmp_path)
    assert produced.read_bytes() == b"RIFF"
    assert "pcm_s16le" in calls[0]
    assert "copy" not in calls[0]

    async def missing_full(_asset, _work, _name):
        return None

    monkeypatch.setattr(render, "_ensure_audio_local", missing_full)
    with pytest.raises(RuntimeError, match="full-script"):
        await render._resolve_audio(
            {"id": "p", "selected_voiceover_asset_id": "full"},
            scenes,
            assets + [{"id": "full", "asset_type": "voiceover_audio", "scene_id": None, "source": "elevenlabs"}],
            tmp_path,
        )

    async def present(_asset, _work, name):
        return left if "000" in name or name == "voiceover_full" else right

    monkeypatch.setattr(render, "_ensure_audio_local", present)
    uneven = [
        {"id": "s1", "scene_number": 1, "duration": 30},
        {"id": "s2", "scene_number": 2, "duration": 30},
    ]
    uneven_assets = [
        {"id": "v1", "asset_type": "voiceover_audio", "scene_id": "s1", "status": "selected", "source": "elevenlabs", "duration": 40},
        {"id": "v2", "asset_type": "voiceover_audio", "scene_id": "s2", "status": "selected", "source": "elevenlabs", "duration": 20},
    ]
    with pytest.raises(RuntimeError, match="visual slot"):
        await render._resolve_audio({"id": "p"}, uneven, uneven_assets, tmp_path)

    async def bad_ffmpeg(cmd, timeout=600):
        return False, "Could not find tag for codec mp3"

    monkeypatch.setattr(render, "_run_ffmpeg", bad_ffmpeg)
    with pytest.raises(RuntimeError, match="silent video"):
        await render._resolve_audio({"id": "p"}, scenes, assets, tmp_path)


@pytest.mark.asyncio
async def test_auto_attach_records_rights_with_both_licence_spellings(monkeypatch):
    recorded = {}

    async def insert(document):
        recorded.update(document)

    class Registry:
        async def insert_one(self, document):
            await insert(document)

    class DB:
        rights_registry = Registry()

    await api_routes._record_stock_rights(
        DB(), project_id="p1", scene_id="s1",
        doc={"id": "a1", "source": "pexels", "external_id": "123", "source_url": "https://pexels.example/123"},
        user_id="u1",
    )
    assert recorded["license"] == "pexels"
    assert recorded["licence"] == "pexels"
    assert recorded["asset_id"] == "a1"
