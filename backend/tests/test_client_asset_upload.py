"""The missing /assets/upload route: validation, scene binding, provenance."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi import UploadFile

from app import storage as storage_module
from app.render import _order_scene_candidates
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

    async def count_documents(self, query):
        return sum(
            1 for doc in self.docs
            if all(doc.get(k) == v for k, v in query.items() if not isinstance(v, dict))
        )

    async def insert_one(self, document):
        self.docs.append(dict(document))
        return FakeResult(1)

    async def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items() if not isinstance(v, dict)):
                doc.update(update.get("$set", {}))
                return FakeResult(1)
        if upsert:
            self.docs.append({**query, **update.get("$set", {})})
        return FakeResult(0)


class FakeDB:
    def __init__(self, project, assets=None, scenes=None):
        self.projects = FakeCollection([project])
        self.assets = FakeCollection(assets)
        self.scenes = FakeCollection(scenes)


def _user(uid="u1", role="creator"):
    return {"id": uid, "role": role}


def _project(**over):
    doc = {"id": "p1", "user_id": "u1", "target_duration": 30, "production_mode": "editorial"}
    doc.update(over)
    return doc


def _scene(**over):
    doc = {"id": "s1", "project_id": "p1", "scene_number": 1, "duration": 30.0}
    doc.update(over)
    return doc


def _upload(name: str, data: bytes = b"x" * 200) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data))


class FakeSave:
    key = "assets/p1/a1.png"
    url = "http://store/a1.png"
    preview_path = "/api/static/assets/p1/a1.png"
    file_path = None
    remote = True


class FakeStore:
    def __init__(self):
        self.calls = []

    def save_file(self, local_path, key, content_type):
        self.calls.append({"key": key, "content_type": content_type})
        return FakeSave()


@pytest.fixture
def fake_media(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(storage_module, "get_storage", lambda: store)
    monkeypatch.setattr(api_routes, "_inspect_client_image", lambda _raw: (1080, 1920))
    monkeypatch.setattr(api_routes, "_inspect_client_audio", lambda _path: 12.5)

    async def fake_video(_path):
        return (1080, 1920, 10.0)

    monkeypatch.setattr(api_routes, "_inspect_client_video", fake_video)
    return store


@pytest.mark.asyncio
async def test_client_image_is_scene_bound_with_provenance(monkeypatch, fake_media):
    db = FakeDB(_project(), scenes=[_scene()])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    doc = await api_routes.upload_client_asset(
        "p1", file=_upload("shop.png"), scene_id="s1",
        asset_type="client_image", user=_user(),
    )

    assert doc["asset_type"] == "client_image"
    assert doc["scene_id"] == "s1"
    assert doc["source"] == "client_upload"
    assert doc["provenance"] == "client_upload"
    assert doc["status"] == "attached"
    assert doc["width"] == 1080 and doc["height"] == 1920
    assert doc["sha256"]
    assert fake_media.calls[0]["key"].startswith("assets/p1/")
    assert fake_media.calls[0]["content_type"] == "image/png"
    assert db.assets.docs[0]["id"] == doc["id"]


@pytest.mark.asyncio
async def test_client_video_uses_probed_duration(monkeypatch, fake_media):
    db = FakeDB(_project(), scenes=[_scene()])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    doc = await api_routes.upload_client_asset(
        "p1", file=_upload("clip.mp4"), scene_id="s1",
        asset_type="client_video", user=_user(),
    )

    assert doc["asset_type"] == "client_video"
    assert doc["duration"] == 10.0
    assert doc["status"] == "attached"


@pytest.mark.asyncio
async def test_brand_logo_is_project_level_and_marks_brand_supplied(monkeypatch, fake_media):
    db = FakeDB(_project())
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    doc = await api_routes.upload_client_asset(
        "p1", file=_upload("logo.png"), scene_id="",
        asset_type="brand_logo", user=_user(),
    )

    assert doc["scene_id"] is None
    assert doc["status"] == "ready"
    assert db.projects.docs[0]["brand_assets_supplied"] is True


@pytest.mark.asyncio
async def test_full_narration_upload_becomes_selected_when_none_exists(monkeypatch, fake_media):
    db = FakeDB(_project())
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    doc = await api_routes.upload_client_asset(
        "p1", file=_upload("vo.mp3"), scene_id="",
        asset_type="voiceover_audio", user=_user(),
    )

    assert doc["status"] == "selected"
    assert doc["duration"] == 12.5
    assert db.projects.docs[0]["selected_voiceover_asset_id"] == doc["id"]


@pytest.mark.asyncio
async def test_client_visual_requires_a_scene(monkeypatch, fake_media):
    db = FakeDB(_project())
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    with pytest.raises(Exception) as exc:
        await api_routes.upload_client_asset(
            "p1", file=_upload("shop.png"), scene_id="",
            asset_type="client_image", user=_user(),
        )

    assert getattr(exc.value, "status_code", None) == 422
    assert db.assets.docs == []


@pytest.mark.asyncio
async def test_scene_must_belong_to_project(monkeypatch, fake_media):
    db = FakeDB(_project(), scenes=[_scene(project_id="other")])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    with pytest.raises(Exception) as exc:
        await api_routes.upload_client_asset(
            "p1", file=_upload("shop.png"), scene_id="s1",
            asset_type="client_image", user=_user(),
        )

    assert getattr(exc.value, "status_code", None) == 404
    assert db.assets.docs == []


@pytest.mark.asyncio
async def test_viewer_cannot_upload(monkeypatch, fake_media):
    db = FakeDB(_project())
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    with pytest.raises(Exception) as exc:
        await api_routes.upload_client_asset(
            "p1", file=_upload("logo.png"), scene_id="",
            asset_type="brand_logo", user=_user(role="viewer"),
        )

    assert getattr(exc.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_unsupported_asset_type_is_rejected(monkeypatch, fake_media):
    db = FakeDB(_project())
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    with pytest.raises(Exception) as exc:
        await api_routes.upload_client_asset(
            "p1", file=_upload("logo.svg"), scene_id="",
            asset_type="brand_logo", user=_user(),
        )

    assert getattr(exc.value, "status_code", None) == 422


def test_real_png_bytes_are_accepted_and_invalid_bytes_are_rejected():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (12, 8), "white").save(buf, format="PNG")
    assert api_routes._inspect_client_image(buf.getvalue()) == (12, 8)

    with pytest.raises(Exception) as exc:
        api_routes._inspect_client_image(b"not an image")
    assert getattr(exc.value, "status_code", None) == 422


@pytest.mark.asyncio
async def test_asset_type_is_inferred_from_extension(monkeypatch, fake_media):
    db = FakeDB(_project(), scenes=[_scene()])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    doc = await api_routes.upload_client_asset(
        "p1", file=_upload("photo.jpg"), scene_id="s1",
        asset_type="", user=_user(),
    )

    assert doc["asset_type"] == "client_image"


def test_client_visuals_are_preferred_over_stock_for_rendering():
    ordered = _order_scene_candidates([
        {"id": "stock", "asset_type": "stock_image"},
        {"id": "client", "asset_type": "client_image"},
        {"id": "brand", "asset_type": "brand_logo"},
    ])
    assert [a["id"] for a in ordered] == ["client", "stock", "brand"]


@pytest.mark.asyncio
async def test_oversized_body_rejected_before_read(monkeypatch, fake_media):
    # Regression for the #18 review: the whole body used to be read into
    # memory before the size check, so an oversized upload spiked RAM.
    db = FakeDB(_project())
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    oversized = UploadFile(
        filename="clip.mp4", file=io.BytesIO(b"x"),
        size=api_routes.CLIENT_VIDEO_MAX_BYTES + 1,
    )

    with pytest.raises(Exception) as exc:
        await api_routes.upload_client_asset(
            "p1", file=oversized, scene_id="s1",
            asset_type="client_video", user=_user(),
        )

    assert getattr(exc.value, "status_code", None) == 413
    assert fake_media.calls == [], "oversized body must never reach the storage layer"


@pytest.mark.asyncio
async def test_bare_read_limit_still_enforced(monkeypatch, fake_media):
    # Servers that do not populate UploadFile.size must still hit the
    # post-read guard (image limit is 15 MB, so this stays cheap).
    db = FakeDB(_project(), scenes=[_scene()])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    oversized = UploadFile(
        filename="shop.png", file=io.BytesIO(b"z" * (api_routes.CLIENT_IMAGE_MAX_BYTES + 1)),
        size=None,
    )

    with pytest.raises(Exception) as exc:
        await api_routes.upload_client_asset(
            "p1", file=oversized, scene_id="s1",
            asset_type="client_image", user=_user(),
        )

    assert getattr(exc.value, "status_code", None) == 413
    assert fake_media.calls == []


def test_audio_probe_failure_degrades_to_422(monkeypatch):
    # Regression for the #18 review: a broken ffprobe raised bare RuntimeError
    # out of _inspect_client_audio and surfaced as a 500.
    def boom(_path):
        raise RuntimeError("ffprobe missing")

    monkeypatch.setattr(api_routes.audio_mix_service, "probe_audio", boom)

    with pytest.raises(Exception) as exc:
        api_routes._inspect_client_audio(Path("/tmp/whatever.mp3"))

    assert getattr(exc.value, "status_code", None) == 422


def test_audio_probe_none_report_is_guarded(monkeypatch):
    monkeypatch.setattr(api_routes.audio_mix_service, "probe_audio", lambda _p: None)

    with pytest.raises(Exception) as exc:
        api_routes._inspect_client_audio(Path("/tmp/whatever.mp3"))

    assert getattr(exc.value, "status_code", None) == 422


@pytest.mark.asyncio
async def test_before_after_image_uses_image_budget_not_video_budget(monkeypatch, fake_media):
    # Regression for review finding: before_after uploads matched the
    # client_video branch of the limit table, so a 16 MB image slipped past
    # the pre-check even though images must stay under 15 MB.
    db = FakeDB(_project(), scenes=[_scene()])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    between_limits = UploadFile(
        filename="half.png",
        file=io.BytesIO(b"z" * (api_routes.CLIENT_IMAGE_MAX_BYTES + 1)),
        size=api_routes.CLIENT_IMAGE_MAX_BYTES + 1,  # still far under the 60 MB video limit
    )

    with pytest.raises(Exception) as exc:
        await api_routes.upload_client_asset(
            "p1", file=between_limits, scene_id="s1",
            asset_type="before_after", user=_user(),
        )

    assert getattr(exc.value, "status_code", None) == 413
    assert fake_media.calls == []
    assert db.assets.docs == []


@pytest.mark.asyncio
async def test_staged_temp_copy_is_removed_when_storage_save_fails(monkeypatch, fake_media):
    # Regression for review finding: _stage_upload_copy leaked the staged
    # copy (up to 60 MB) into $TMPDIR whenever save_file raised.
    import tempfile

    store = fake_media

    def boom(path, key, content_type):
        raise RuntimeError("storage outage")

    store.save_file = boom

    db = FakeDB(_project(), scenes=[_scene()])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    tmpdir = Path(tempfile.gettempdir())
    before = set(p.name for p in tmpdir.glob("client-asset-*"))

    with pytest.raises(Exception) as exc:
        await api_routes.upload_client_asset(
            "p1", file=_upload("shop.png"), scene_id="s1",
            asset_type="client_image", user=_user(),
        )

    assert "storage outage" in str(exc.value)
    after = set(p.name for p in tmpdir.glob("client-asset-*"))
    assert after == before, f"orphaned staged files: {after - before}"
