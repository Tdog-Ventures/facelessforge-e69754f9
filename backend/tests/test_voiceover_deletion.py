"""Voiceover deletion must only remove the artifact issued for that asset."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app import storage, tts
from app.routes import api


class Collection:
    def __init__(self, docs=()):
        self.docs = [dict(doc) for doc in docs]

    async def find_one(self, query, *_args):
        return next((dict(doc) for doc in self.docs if all(
            doc.get(key) == value for key, value in query.items())), None)

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def delete_one(self, query):
        self.docs[:] = [doc for doc in self.docs if not all(
            doc.get(key) == value for key, value in query.items())]

    async def update_one(self, query, update):
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in query.items()):
                doc.update(update["$set"])


@pytest.fixture
def context(monkeypatch, tmp_path):
    root = tmp_path / "static"
    monkeypatch.setattr(storage, "STATIC_ROOT", root)
    monkeypatch.setattr(tts, "STATIC_ROOT", root / "audio")
    (root / "audio").mkdir(parents=True)
    store = storage.LocalStorage()
    monkeypatch.setattr(storage, "get_storage", lambda: store)
    monkeypatch.setattr(tts, "get_storage", lambda: store)
    db = SimpleNamespace(
        projects=Collection([{"id": "p1", "user_id": "u1"}]),
        assets=Collection(),
    )
    monkeypatch.setattr(api, "get_db", lambda: db)

    async def view(_db, project):
        return {"project": project}

    monkeypatch.setattr(api, "_attach_project_view", view)
    return SimpleNamespace(db=db, root=root, user={"id": "u1", "role": "creator"})


def write_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"keep me")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["outside", "traversal", "other_project", "other_asset"])
async def test_registered_file_path_does_not_authorize_deletion(context, tmp_path, target):
    paths = {
        "outside": tmp_path / "server-config",
        "traversal": context.root / "audio" / ".." / ".." / "server-config",
        "other_project": context.root / "audio" / "p2" / "victim.wav",
        "other_asset": context.root / "audio" / "p1" / "victim.wav",
    }
    victim = write_file(paths[target])
    # PR21 rejects client-supplied server paths at creation, so seed the
    # dangerous record directly to keep this test focused on deletion behavior.
    asset_id = "registered-a1"
    context.db.assets.docs.append({
        "id": asset_id,
        "project_id": "p1",
        "asset_type": "voiceover_audio",
        "file_path": str(victim),
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    })

    await api.delete_voiceover("p1", asset_id, user=context.user)

    assert victim.read_bytes() == b"keep me"
    assert context.db.assets.docs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["file_path", "storage_key"])
@pytest.mark.parametrize("link_at", ["audio", "project", "file"])
async def test_symlink_escape_is_not_deleted(context, tmp_path, field, link_at):
    outside = tmp_path / "outside"
    victim = write_file(outside / "p1" / "v1.wav")
    expected = context.root / "audio" / "p1" / "v1.wav"
    if link_at == "audio":
        (context.root / "audio").rmdir()
        (context.root / "audio").symlink_to(outside, target_is_directory=True)
    elif link_at == "project":
        expected.parent.symlink_to(victim.parent, target_is_directory=True)
    else:
        expected.parent.mkdir(parents=True)
        expected.symlink_to(victim)
    context.db.assets.docs.append({
        "id": "v1", "project_id": "p1", "asset_type": "voiceover_audio",
        field: str(expected) if field == "file_path" else "audio/p1/v1.wav",
    })

    await api.delete_voiceover("p1", "v1", user=context.user)

    assert victim.read_bytes() == b"keep me"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["file_path", "storage_key"])
@pytest.mark.parametrize("ext", [".mp3", ".wav"])
async def test_valid_artifact_is_deleted_and_selection_cleared(context, field, ext):
    path = write_file(context.root / "audio" / "p1" / f"v1{ext}")
    context.db.projects.docs[0]["selected_voiceover_asset_id"] = "v1"
    context.db.assets.docs.append({
        "id": "v1", "project_id": "p1", "asset_type": "voiceover_audio",
        field: str(path) if field == "file_path" else f"audio/p1/v1{ext}",
    })

    await api.delete_voiceover("p1", "v1", user=context.user)

    assert not path.exists()
    assert context.db.assets.docs == []
    assert context.db.projects.docs[0]["selected_voiceover_asset_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [
    "audio/p1/v1.mp3", "audio/p1/v1.wav", "audio/p2/v1.wav",
    "audio/p1/other.wav", "audio/p1/../../victim", "/audio/p1/v1.wav",
    "audio/p1-extra/v1.wav", "renders/p1/v1.mp4",
])
async def test_object_deletion_requires_exact_asset_key(context, monkeypatch, key):
    store = SimpleNamespace(mode="object", delete=Mock())
    monkeypatch.setattr(storage, "get_storage", lambda: store)
    context.db.assets.docs.append({
        "id": "v1", "project_id": "p1", "asset_type": "voiceover_audio", "storage_key": key,
    })

    await api.delete_voiceover("p1", "v1", user=context.user)

    if key in {"audio/p1/v1.mp3", "audio/p1/v1.wav"}:
        store.delete.assert_called_once_with(key=key)
    else:
        store.delete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("user,asset_project,status", [
    ({"id": "intruder", "role": "creator"}, "p1", 403),
    ({"id": "u1", "role": "viewer"}, "p1", 403),
    ({"id": "u1", "role": "creator"}, "p2", 404),
])
async def test_existing_access_controls_preserve_artifacts(context, user, asset_project, status):
    path = write_file(context.root / "audio" / "p1" / "v1.wav")
    context.db.assets.docs.append({
        "id": "v1", "project_id": asset_project, "asset_type": "voiceover_audio",
        "file_path": str(path),
    })

    with pytest.raises(HTTPException) as exc:
        await api.delete_voiceover("p1", "v1", user=user)

    assert exc.value.status_code == status
    assert path.exists()
    assert len(context.db.assets.docs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mock", [True, False])
async def test_generated_voiceover_keeps_storage_reference(context, monkeypatch, mock):
    monkeypatch.setenv("USE_MOCK_TTS", str(mock).lower())
    monkeypatch.setenv("ELEVENLABS_API_KEY", "synthetic-test-key")

    async def audio(*_args, **_kwargs):
        return b"synthetic mp3"

    async def duration(_path):
        return 2.0

    monkeypatch.setattr(tts, "_elevenlabs_tts", audio)
    monkeypatch.setattr(tts, "_measure_audio_duration", duration)
    asset = await tts.generate_voiceover("A short narration", project_id="p1", asset_id="v1")
    ext = ".wav" if mock else ".mp3"
    assert asset["storage_key"] == f"audio/p1/v1{ext}"
    context.db.assets.docs.append(asset)

    await api.delete_voiceover("p1", "v1", user=context.user)

    assert not (context.root / asset["storage_key"]).exists()
