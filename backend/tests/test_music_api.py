"""Per-project music: upload validation, ownership, rights gating, selection
state, and the guarantee that nothing implicitly selects the bundled bed."""
import io

import pytest

from app.models import MusicSelectRequest
from app.routes import api as api_routes


class FakeResult:
    def __init__(self, n=0):
        self.deleted_count = n


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    async def find_one(self, query, *_a, **_k):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items() if not isinstance(v, dict)):
                return dict(doc)
        return None

    async def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items() if not isinstance(v, dict)):
                doc.update(update.get("$set", {}))
                return FakeResult(1)
        if upsert:
            self.docs.append({**query, **update.get("$set", {})})
        return FakeResult(0)

    async def insert_one(self, document):
        self.docs.append(dict(document))
        return FakeResult(1)

    async def delete_one(self, query):
        self.docs[:] = [d for d in self.docs if not all(
            d.get(k) == v for k, v in query.items() if not isinstance(v, dict))]
        return FakeResult(1)


class FakeDB:
    def __init__(self, project, assets=None):
        self.projects = FakeCollection([project])
        self.assets = FakeCollection(assets)
        self.scripts = FakeCollection([{"project_id": project["id"], "full_script": "w " * 30}])
        self.scenes = FakeCollection()
        self.metadata_packages = FakeCollection()
        self.render_jobs = FakeCollection()


def _user(uid="u1", role="creator"):
    return {"id": uid, "role": role}


def _project(**over):
    doc = {"id": "p1", "user_id": "u1", "target_duration": 30}
    doc.update(over)
    return doc


def _music_asset(**over):
    doc = {"id": "m1", "project_id": "p1", "asset_type": "music_audio",
           "name": "track.mp3", "duration": 30.0, "rights_confirmed": True,
           "rights_note": "licensed", "preview_url": "http://x/m.mp3"}
    doc.update(over)
    return doc


def _stub_view(monkeypatch):
    async def stub(_db, _project):
        return {"project": _project, "music": True}

    monkeypatch.setattr(api_routes, "_attach_project_view", stub)


@pytest.mark.asyncio
async def test_select_valid_music_persists_state(monkeypatch):
    db = FakeDB(_project(), assets=[_music_asset(rights_confirmed=False)])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    _stub_view(monkeypatch)
    await api_routes.select_music(
        "p1", "m1",
        MusicSelectRequest(music_gain_db=-10, music_ducking_enabled=True,
                           rights_confirmed=True, rights_note="licensed"),
        user=_user())
    proj = db.projects.docs[0]
    assert proj["music_enabled"] is True
    assert proj["selected_music_asset_id"] == "m1"
    assert proj["music_gain_db"] == -10
    assert proj["music_ducking_enabled"] is True
    assert db.assets.docs[0]["rights_confirmed"] is True


@pytest.mark.asyncio
async def test_select_requires_rights(monkeypatch):
    db = FakeDB(_project(), assets=[_music_asset(rights_confirmed=False)])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    with pytest.raises(Exception) as exc:
        await api_routes.select_music("p1", "m1", MusicSelectRequest(), user=_user())
    assert getattr(exc.value, "status_code", None) == 422
    assert db.projects.docs[0].get("music_enabled") is not True


@pytest.mark.asyncio
async def test_select_rejects_bad_gain_missing_and_cross_tenant(monkeypatch):
    db = FakeDB(_project(), assets=[_music_asset()])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    with pytest.raises(Exception) as exc:
        await api_routes.select_music(
            "p1", "m1", MusicSelectRequest(music_gain_db=0, rights_confirmed=True),
            user=_user())
    assert getattr(exc.value, "status_code", None) == 422
    with pytest.raises(Exception) as exc:
        await api_routes.select_music(
            "p1", "nope", MusicSelectRequest(rights_confirmed=True), user=_user())
    assert getattr(exc.value, "status_code", None) == 404
    other = _music_asset(**{"id": "mx", "project_id": "other"})
    db2 = FakeDB(_project(), assets=[other])
    monkeypatch.setattr(api_routes, "get_db", lambda: db2)
    with pytest.raises(Exception) as exc:
        await api_routes.select_music(
            "p1", "mx", MusicSelectRequest(rights_confirmed=True), user=_user())
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_cross_user_music_change_forbidden(monkeypatch):
    db = FakeDB(_project(), assets=[_music_asset()])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    with pytest.raises(Exception) as exc:
        await api_routes.select_music(
            "p1", "m1", MusicSelectRequest(rights_confirmed=True),
            user=_user("intruder"))
    assert getattr(exc.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_disable_music_keeps_assets(monkeypatch):
    db = FakeDB(_project(music_enabled=True, selected_music_asset_id="m1"),
                assets=[_music_asset()])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    _stub_view(monkeypatch)
    await api_routes.disable_music("p1", user=_user())
    assert db.projects.docs[0]["music_enabled"] is False
    assert db.projects.docs[0]["selected_music_asset_id"] is None
    assert len(db.assets.docs) == 1


@pytest.mark.asyncio
async def test_upload_rejects_bad_type(monkeypatch):
    from fastapi import UploadFile

    db = FakeDB(_project())
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    bad = UploadFile(filename="evil.exe", file=io.BytesIO(b"mz" * 500))
    with pytest.raises(Exception) as exc:
        await api_routes.upload_music("p1", file=bad, rights_confirmed=True,
                                      rights_note="", user=_user())
    assert getattr(exc.value, "status_code", None) == 422
    assert db.assets.docs == []


@pytest.mark.asyncio
async def test_upload_accepts_decodable_audio(monkeypatch):
    from fastapi import UploadFile
    from app import storage as storage_module

    db = FakeDB(_project())
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    def fake_probe(_path):
        return {"sample_rate": 48000, "channels": 2, "duration": 30.0, "peak_db": -3.0}

    monkeypatch.setattr(api_routes.audio_mix_service, "probe_audio", fake_probe)

    class FakeSave:
        key = "music/p1/m1.mp3"
        url = "http://store/m1.mp3"
        preview_path = "/api/static/m1.mp3"
        file_path = None
        remote = True

    class FakeStore:
        def save_file(self, *_a, **_k):
            return FakeSave()

    monkeypatch.setattr(storage_module, "get_storage", lambda: FakeStore())
    good = UploadFile(filename="bed.mp3", file=io.BytesIO(b"\xff\xfb" + b"\x00" * 5000))
    _stub_view(monkeypatch)
    await api_routes.upload_music("p1", file=good, rights_confirmed=True,
                                  rights_note="licensed", user=_user())
    assert len(db.assets.docs) == 1
    doc = db.assets.docs[0]
    assert doc["asset_type"] == "music_audio" and doc["rights_confirmed"] is True
    assert db.projects.docs[0]["selected_music_asset_id"] == doc["id"]


def test_preflight_music_states():
    from app import render
    base = {"target_duration": 30, "selected_voiceover_asset_id": "v1",
            "selected_thumbnail_asset_id": "t1"}
    script = {"full_script": "w " * 30}
    # A render-ready fixture needs an identifiable scene and footage that
    # actually covers it (two 13.75s holds for a 27.5s scene). Without a
    # scene id the coverage gate correctly fails closed.
    scenes = [{"id": "scene-1", "scene_number": 1, "duration": 27.5,
               "end_time": 27.5}]
    assets = [{"id": "v1", "asset_type": "voiceover_audio", "duration": 27.2,
               "status": "selected"},
              {"id": "t1", "asset_type": "generated_thumbnail"},
              {"id": "s1", "asset_type": "stock_video", "scene_id": "scene-1",
               "source": "pexels", "duration": 13.75, "status": "attached"},
              {"id": "s2", "asset_type": "stock_video", "scene_id": "scene-1",
               "source": "pexels", "duration": 13.75, "status": "attached"}]
    off = render.validate_prerequisites(dict(base), script, scenes, {"id": "m"}, assets)
    assert off["music_state"]["enabled"] is False
    assert off["ok"] is True
    enabled_no_asset = render.validate_prerequisites(
        dict(base, music_enabled=True), script, scenes, {"id": "m"}, assets)
    assert enabled_no_asset["ok"] is False
    assert "Background music" in enabled_no_asset["issues"]
    assert enabled_no_asset["music_state"]["enabled"] is True
    ok_sel = render.validate_prerequisites(
        dict(base, music_enabled=True, selected_music_asset_id="m1",
             music_rights_confirmed=True),
        script, scenes, {"id": "m"},
        assets + [{"id": "m1", "asset_type": "music_audio", "project_id": None,
                   "rights_confirmed": True}])
    assert ok_sel["music_state"]["asset_id"] == "m1"
