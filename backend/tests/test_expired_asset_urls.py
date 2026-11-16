"""Expired R2 presigned URLs must not break renders or previews.

Voiceovers and thumbnails are stored with a storage_key and a presigned
preview_url (X-Amz-Expires, default 86400). After that URL 403s, a render and
the asset responses still have to work from storage_key. The S3 client is
faked: these tests make no network calls.
"""
import io
from pathlib import Path

import pytest
from PIL import Image

from app import render, storage
from app.routes import api


EXPIRED_AUDIO = (
    "https://acct.r2.cloudflarestorage.com/ff-test/audio/p1/v1.mp3"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=86400"
    "&X-Amz-Date=20261113T211200Z&X-Amz-Signature=deadbeef"
)
EXPIRED_THUMB = (
    "https://acct.r2.cloudflarestorage.com/ff-test/thumbs/p1/t1.png"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=86400"
    "&X-Amz-Date=20261113T211300Z&X-Amz-Signature=deadbeef"
)


class FakeS3:
    """Stand-in for the boto3 client. Records calls and serves in-memory bytes."""

    def __init__(self):
        self.objects = {}
        self.calls = []

    def head_object(self, Bucket, Key):
        self.calls.append(("head", Bucket, Key))
        if Key not in self.objects:
            raise FileNotFoundError(Key)
        return {"ContentLength": len(self.objects[Key])}

    def download_file(self, Bucket, Key, Filename):
        self.calls.append(("get", Bucket, Key))
        if Key not in self.objects:
            raise FileNotFoundError(Key)
        path = Path(Filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.objects[Key])

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.calls.append(("sign", Params.get("Key"), ExpiresIn))
        return (
            f"https://signed.example/{Params['Key']}"
            f"?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=fresh"
        )


class _Docs:
    def __init__(self, docs=()):
        self.docs = [dict(doc) for doc in docs]

    async def find_one(self, query=None, *_args, **_kwargs):
        query = query or {}
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items() if not isinstance(value, dict)):
                return dict(doc)
        return None

    def find(self, *_args, **_kwargs):
        return self

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, *_args, **_kwargs):
        return [dict(doc) for doc in self.docs]

    async def update_one(self, *_args, **_kwargs):
        return None


def _png(color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


async def _reject_http(*_args, **_kwargs):
    raise AssertionError("stored asset was fetched by its expired URL")


@pytest.fixture
def object_store(monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "object")
    monkeypatch.setenv("STORAGE_BUCKET", "ff-test")
    monkeypatch.setenv("STORAGE_REGION", "auto")
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", "https://example.r2.cloudflarestorage.com")
    monkeypatch.setenv("STORAGE_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("STORAGE_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("STORAGE_PUBLIC_BASE_URL", "https://videos.example.invalid")
    storage.reset_for_tests()
    store = storage.get_storage()
    store._client = FakeS3()
    try:
        yield store
    finally:
        storage.reset_for_tests()


@pytest.mark.asyncio
async def test_expired_voiceover_url_is_read_by_storage_key(object_store, monkeypatch, tmp_path):
    object_store._client.objects["audio/p1/v1.mp3"] = b"ID3fresh-voiceover"
    monkeypatch.setattr(render, "_download_to", _reject_http)
    asset = {
        "asset_type": "voiceover_audio",
        "file_path": "",
        "storage_key": "audio/p1/v1.mp3",
        "preview_url": EXPIRED_AUDIO,
        "download_url": EXPIRED_AUDIO,
    }
    work = tmp_path / "work"
    work.mkdir()

    path = await render._ensure_audio_local(asset, work, "voiceover_full")

    assert path is not None
    assert path.read_bytes() == b"ID3fresh-voiceover"
    assert asset["preview_url"] == EXPIRED_AUDIO
    assert ("get", "ff-test", "audio/p1/v1.mp3") in object_store._client.calls


@pytest.mark.asyncio
async def test_expired_thumbnail_url_is_read_by_storage_key(object_store, monkeypatch, tmp_path):
    object_store._client.objects["thumbs/p1/t1.png"] = _png()
    monkeypatch.setattr(render, "_download_to", _reject_http)
    work = tmp_path / "work"
    work.mkdir()

    out = await render._resolve_thumbnail(
        {
            "asset_type": "generated_thumbnail",
            "file_path": None,
            "storage_key": "thumbs/p1/t1.png",
            "preview_url": EXPIRED_THUMB,
        },
        {"name": "Eggs", "topic": "how to cook an egg"},
        work,
    )

    img = Image.open(out)
    assert img.size == (render.WIDTH, render.HEIGHT)
    assert img.getpixel((10, 10)) == (255, 0, 0)
    assert ("get", "ff-test", "thumbs/p1/t1.png") in object_store._client.calls


@pytest.mark.asyncio
async def test_local_audio_file_is_used_without_calling_storage(object_store, monkeypatch, tmp_path):
    root = tmp_path / "static"
    audio = root / "audio" / "p1" / "v1.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"local-mp3")
    monkeypatch.setattr(render, "STATIC_ROOT", root)

    def boom(*_args, **_kwargs):
        raise AssertionError("storage client should not be called")

    object_store._client.head_object = boom
    object_store._client.download_file = boom
    path = await render._ensure_audio_local(
        {
            "file_path": str(audio),
            "storage_key": "audio/p1/v1.mp3",
            "preview_url": EXPIRED_AUDIO,
        },
        tmp_path / "work",
        "voiceover_full",
    )

    assert path == audio
    assert path.read_bytes() == b"local-mp3"


@pytest.mark.asyncio
async def test_asset_without_storage_key_still_uses_its_url(object_store, monkeypatch, tmp_path):
    seen = []

    async def grab(url, out, **_kwargs):
        seen.append(url)
        Path(out).write_bytes(b"from-url")
        return True

    monkeypatch.setattr(render, "_download_to", grab)
    work = tmp_path / "work"
    work.mkdir()
    path = await render._ensure_audio_local(
        {"preview_url": "https://cdn.example/track.mp3", "file_path": None},
        work,
        "music_selected",
    )

    assert path.read_bytes() == b"from-url"
    assert seen == ["https://cdn.example/track.mp3"]
    assert object_store._client.calls == []


@pytest.mark.asyncio
async def test_storage_miss_falls_back_to_a_still_valid_url(object_store, monkeypatch, tmp_path):
    async def grab(url, out, **_kwargs):
        Path(out).write_bytes(b"url-bytes")
        return True

    monkeypatch.setattr(render, "_download_to", grab)
    work = tmp_path / "work"
    work.mkdir()
    path = await render._ensure_audio_local(
        {
            "storage_key": "audio/p1/missing.mp3",
            "preview_url": "https://cdn.example/still-valid.mp3",
            "file_path": None,
        },
        work,
        "voiceover_full",
    )

    assert path.read_bytes() == b"url-bytes"
    assert ("head", "ff-test", "audio/p1/missing.mp3") in object_store._client.calls


@pytest.mark.asyncio
async def test_expired_client_image_url_is_read_by_storage_key(object_store, monkeypatch, tmp_path):
    object_store._client.objects["assets/p1/clip.png"] = _png((0, 255, 0))
    monkeypatch.setattr(render, "_download_to", _reject_http)
    scene = {"id": "scene-1"}
    asset = {
        "id": "clip-1",
        "scene_id": "scene-1",
        "asset_type": "client_image",
        "file_path": None,
        "storage_key": "assets/p1/clip.png",
        "preview_url": EXPIRED_THUMB,
    }

    path, kind, _ext = await render._resolve_scene_visual(
        scene, [asset], {"id": "p1"}, tmp_path, 0, stock_retry=False,
    )

    assert kind == "image"
    assert path.read_bytes() == object_store._client.objects["assets/p1/clip.png"]
    assert ("get", "ff-test", "assets/p1/clip.png") in object_store._client.calls


def test_preview_url_is_resigned_without_changing_the_stored_asset(object_store):
    asset = {
        "asset_type": "voiceover_audio",
        "storage_key": "audio/p1/v1.mp3",
        "preview_url": EXPIRED_AUDIO,
        "url": EXPIRED_AUDIO,
    }

    fresh = storage.resolve_asset_url(asset)
    payload = api._asset_payload(asset)

    assert fresh.startswith("https://signed.example/audio/p1/v1.mp3?")
    assert "X-Amz-Signature=fresh" in fresh
    assert "X-Amz-Expires=" in fresh
    assert payload["preview_url"] == fresh
    assert payload["url"] == fresh
    assert asset["preview_url"] == EXPIRED_AUDIO
    assert asset["url"] == EXPIRED_AUDIO


def test_external_preview_url_is_not_resigned(object_store):
    asset = {
        "asset_type": "stock_image",
        "preview_url": "https://images.pexels.com/photos/1.jpeg",
    }

    assert storage.resolve_asset_url(asset) == asset["preview_url"]
    assert object_store._client.calls == []


def test_local_mode_keeps_the_static_preview(monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "local")
    storage.reset_for_tests()
    try:
        asset = {
            "storage_key": "audio/p1/v1.mp3",
            "preview_url": "/api/static/audio/p1/v1.mp3",
        }
        assert storage.resolve_asset_url(asset) == "/api/static/audio/p1/v1.mp3"
    finally:
        storage.reset_for_tests()


def test_object_download_rejects_traversal_and_oversized_objects(object_store, tmp_path):
    object_store._client.objects["audio/p1/big.mp3"] = b"0123456789abcdef"
    dest = tmp_path / "big.mp3"

    assert object_store.download_object("../secret.mp3", tmp_path / "escape.mp3") is False
    assert object_store._client.calls == []
    # A leading slash is a bucket key, not a host path.
    assert object_store.download_object("/etc/passwd", tmp_path / "passwd") is False
    assert not (tmp_path / "passwd").exists()
    assert ("get", "ff-test", "etc/passwd") in object_store._client.calls
    assert object_store.download_object("audio/p1/big.mp3", dest, max_bytes=4) is False
    assert not dest.exists()
    assert ("get", "ff-test", "audio/p1/big.mp3") not in object_store._client.calls


def test_local_download_reads_a_real_file_and_rejects_a_symlink(tmp_path, monkeypatch):
    root = tmp_path / "static"
    folder = root / "audio" / "p1"
    folder.mkdir(parents=True)
    real = folder / "ok.mp3"
    real.write_bytes(b"ok-audio")
    victim = tmp_path / "secret.mp3"
    victim.write_bytes(b"secret")
    link = folder / "v1.mp3"
    link.symlink_to(victim)
    monkeypatch.setattr(storage, "STATIC_ROOT", root)
    store = storage.LocalStorage()
    dest = tmp_path / "out.mp3"

    assert store.download_object("audio/p1/ok.mp3", dest) is True
    assert dest.read_bytes() == b"ok-audio"
    escaped = tmp_path / "escaped.mp3"
    assert store.download_object("audio/p1/v1.mp3", escaped) is False
    assert store.download_object("../../secret.mp3", escaped) is False
    assert store.download_object("/etc/passwd", escaped) is False
    assert not escaped.exists()
    assert victim.read_bytes() == b"secret"


@pytest.mark.asyncio
async def test_thumbnail_endpoint_redirects_to_a_fresh_url(object_store, monkeypatch):
    import app.db as db_mod

    db = type("DB", (), {})()
    db.assets = _Docs([{
        "id": "t1",
        "project_id": "p1",
        "asset_type": "generated_thumbnail",
        "storage_key": "thumbs/p1/t1.png",
        "preview_url": EXPIRED_THUMB,
        "file_path": None,
    }])
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    monkeypatch.setattr(storage, "STATIC_ROOT", object_store.root if hasattr(object_store, "root") else Path("/tmp/ff-no-such-static"))

    response = await api.get_thumbnail("t1", request=None, user={"id": "u1", "role": "creator"})

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://signed.example/thumbs/p1/t1.png?")
    assert "deadbeef" not in location


@pytest.mark.asyncio
async def test_thumbnail_endpoint_does_not_sign_a_foreign_key(object_store, monkeypatch):
    import app.db as db_mod

    db = type("DB", (), {})()
    db.assets = _Docs([{
        "id": "t1",
        "project_id": "p1",
        "asset_type": "generated_thumbnail",
        "storage_key": "audio/p1/secret.mp3",
        "preview_url": "https://cdn.example/stale-thumb.png",
    }])
    monkeypatch.setattr(db_mod, "get_db", lambda: db)

    response = await api.get_thumbnail("t1", request=None, user={"id": "u1", "role": "creator"})

    assert response.status_code == 302
    assert response.headers["location"] == "https://cdn.example/stale-thumb.png"
    assert object_store._client.calls == []


@pytest.mark.asyncio
async def test_thumbnail_endpoint_still_serves_a_local_file(object_store, monkeypatch, tmp_path):
    import app.db as db_mod

    root = tmp_path / "static"
    local = root / "thumbs" / "p1" / "t1.png"
    local.parent.mkdir(parents=True)
    local.write_bytes(_png())
    monkeypatch.setattr(storage, "STATIC_ROOT", root)
    db = type("DB", (), {})()
    db.assets = _Docs([{
        "id": "t1",
        "project_id": "p1",
        "asset_type": "generated_thumbnail",
        "storage_key": "thumbs/p1/t1.png",
        "preview_url": EXPIRED_THUMB,
    }])
    monkeypatch.setattr(db_mod, "get_db", lambda: db)

    response = await api.get_thumbnail("t1", request=None, user={"id": "u1", "role": "creator"})

    assert response.status_code == 200
    assert Path(response.path).resolve() == local.resolve()
    assert object_store._client.calls == []


@pytest.mark.asyncio
async def test_public_share_refreshes_thumbnail_and_voiceover(object_store, monkeypatch):
    project = {
        "id": "p1",
        "user_id": "u1",
        "name": "Eggs",
        "niche": "food",
        "status": "COMPLETED",
        "share_enabled": True,
        "share_token": "share-token-1",
        "selected_thumbnail_asset_id": "t1",
        "selected_voiceover_asset_id": "v1",
    }
    thumb = {
        "id": "t1",
        "project_id": "p1",
        "asset_type": "generated_thumbnail",
        "storage_key": "thumbs/p1/t1.png",
        "preview_url": EXPIRED_THUMB,
    }
    voice = {
        "id": "v1",
        "project_id": "p1",
        "asset_type": "voiceover_audio",
        "storage_key": "audio/p1/v1.mp3",
        "preview_url": EXPIRED_AUDIO,
        "duration": 132.6,
        "voice_style": "narrator",
    }
    db = type("DB", (), {})()
    db.projects = _Docs([project])
    db.metadata_packages = _Docs()
    db.assets = _Docs([thumb, voice])
    db.render_jobs = _Docs()
    db.provider_settings = _Docs()
    monkeypatch.setattr(api, "get_db", lambda: db)

    payload = await api.public_share("share-token-1")

    assert payload["selected_thumbnail_url"].startswith("https://signed.example/thumbs/p1/t1.png?")
    assert payload["selected_voiceover"]["preview_url"].startswith("https://signed.example/audio/p1/v1.mp3?")
    assert "deadbeef" not in payload["selected_thumbnail_url"]
    assert "deadbeef" not in payload["selected_voiceover"]["preview_url"]
    assert thumb["preview_url"] == EXPIRED_THUMB
    assert voice["preview_url"] == EXPIRED_AUDIO


@pytest.mark.asyncio
async def test_project_view_refreshes_asset_previews(object_store, monkeypatch):
    project = {
        "id": "p1",
        "user_id": "u1",
        "name": "Eggs",
        "status": "ASSETS_READY",
        "target_duration": 60,
        "selected_voiceover_asset_id": "v1",
        "selected_music_asset_id": "m1",
    }
    voice = {
        "id": "v1",
        "project_id": "p1",
        "asset_type": "voiceover_audio",
        "storage_key": "audio/p1/v1.mp3",
        "preview_url": EXPIRED_AUDIO,
        "url": EXPIRED_AUDIO,
        "duration": 50,
    }
    music = {
        "id": "m1",
        "project_id": "p1",
        "asset_type": "music_audio",
        "storage_key": "music/p1/m1.mp3",
        "preview_url": EXPIRED_AUDIO,
    }
    stock = {
        "id": "s1",
        "project_id": "p1",
        "asset_type": "stock_image",
        "preview_url": "https://images.pexels.com/photos/1.jpeg",
    }
    db = type("DB", (), {})()
    db.projects = _Docs([project])
    db.scripts = _Docs()
    db.scenes = _Docs()
    db.metadata_packages = _Docs()
    db.assets = _Docs([voice, music, stock])
    db.render_jobs = _Docs()
    monkeypatch.setattr(api, "get_db", lambda: db)

    payload = await api._attach_project_view(db, dict(project))

    by_id = {item["id"]: item for item in payload["assets"]}
    assert by_id["v1"]["preview_url"].startswith("https://signed.example/audio/p1/v1.mp3?")
    assert by_id["v1"]["url"].startswith("https://signed.example/audio/p1/v1.mp3?")
    assert by_id["m1"]["preview_url"].startswith("https://signed.example/music/p1/m1.mp3?")
    assert by_id["s1"]["preview_url"] == "https://images.pexels.com/photos/1.jpeg"
    assert payload["music_asset"]["preview_url"] == by_id["m1"]["preview_url"]
    assert voice["preview_url"] == EXPIRED_AUDIO
