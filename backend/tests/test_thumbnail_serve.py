"""Thumbnail serving must only read the canonical artifact for the asset (CWE-73).

Mirrors test_voiceover_deletion.py: a registered file_path/storage_key never
authorizes serving a file outside the asset storage root, across projects,
via traversal, or through symlink escapes.
"""
from types import SimpleNamespace
from pathlib import Path

import pytest

from app import storage
from app.routes import api


class Collection:
    def __init__(self, docs=()):
        self.docs = [dict(doc) for doc in docs]

    async def find_one(self, query, *_args):
        return next((dict(doc) for doc in self.docs if all(
            doc.get(key) == value for key, value in query.items())), None)

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


@pytest.fixture
def context(monkeypatch, tmp_path):
    root = tmp_path / "static"
    (root / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(storage, "STATIC_ROOT", root)
    db = SimpleNamespace(
        projects=Collection([{"id": "p1", "user_id": "u1"}]),
        assets=Collection(),
    )
    import app.db as db_mod
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    monkeypatch.setattr(api, "get_db", lambda: db)
    return SimpleNamespace(db=db, root=root, user={"id": "u1", "role": "creator"})


def write_file(path, payload=b"asset bytes"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def served_path(resp):
    path = getattr(resp, "path", None)
    return Path(path).resolve() if path else None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["file_path", "storage_key"])
async def test_legitimate_thumbnail_is_served(context, field):
    canonical = write_file(context.root / "thumbs" / "p1" / "t1.png")
    context.db.assets.docs.append({
        "id": "t1", "project_id": "p1", "asset_type": "generated_thumbnail",
        field: str(canonical) if field == "file_path" else "thumbs/p1/t1.png",
    })

    resp = await api.get_thumbnail("t1", request=None, user=context.user)

    assert resp.status_code == 200
    assert served_path(resp) == canonical.resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["outside", "traversal", "other_project", "symlink_file_path"])
async def test_registered_file_path_does_not_authorize_serving(context, tmp_path, target):
    victim = write_file(tmp_path / "server-config.png", b"victim bytes")
    paths = {
        "outside": victim,
        "traversal": context.root / "thumbs" / "p1" / ".." / ".." / "server-config.png",
        "other_project": write_file(context.root / "thumbs" / "p2" / "t1.png", b"victim bytes"),
        "symlink_file_path": context.root / "thumbs" / "p1" / "t1.png",
    }
    target_path = paths[target]
    if target == "traversal":
        # lands on static/server-config.png, which sits outside static/thumbs
        write_file(context.root / "server-config.png", b"victim bytes")
        victim = context.root / "server-config.png"
    if target == "symlink_file_path":
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.symlink_to(victim)

    context.db.assets.docs.append({
        "id": "t1", "project_id": "p1", "asset_type": "generated_thumbnail",
        "file_path": str(target_path),
    })

    resp = await api.get_thumbnail("t1", request=None, user=context.user)

    assert served_path(resp) != victim.resolve(), f"{target} must not be served"
    assert victim.read_bytes() == b"victim bytes"


@pytest.mark.asyncio
async def test_storage_key_symlink_escape_is_rejected(context, tmp_path):
    victim = write_file(tmp_path / "server-config.png", b"victim bytes")
    canonical = context.root / "thumbs" / "p1" / "t1.png"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.symlink_to(victim)
    context.db.assets.docs.append({
        "id": "t1", "project_id": "p1", "asset_type": "generated_thumbnail",
        "storage_key": "thumbs/p1/t1.png",
    })

    resp = await api.get_thumbnail("t1", request=None, user=context.user)

    assert served_path(resp) != victim.resolve()
    assert victim.read_bytes() == b"victim bytes"


@pytest.mark.asyncio
async def test_missing_asset_falls_back_without_touching_files(context, tmp_path):
    victim = write_file(tmp_path / "server-config.png", b"victim bytes")
    resp = await api.get_thumbnail("no-such-asset", request=None, user=context.user)

    assert served_path(resp) != victim.resolve()
