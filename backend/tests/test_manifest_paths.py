import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from app import release_manifest
from app.models import AssetCreate
from app.routes import api


@pytest.fixture
def roots(tmp_path, monkeypatch):
    root=tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setattr(release_manifest,"ARTIFACT_ROOT",root,raising=False)
    return root,tmp_path


def test_owned_artifact_can_be_hashed(roots):
    root,_=roots
    path=root/"thumb.png";path.write_bytes(b"safe image fixture")
    assert release_manifest.file_hash(path)=="sha256:"+hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("variant",["absolute","parent","symlink","directory"])
def test_hash_rejects_outside_or_non_file_input(roots,variant):
    root,base=roots
    outside=base/"private.txt";outside.write_bytes(b"private sentinel")
    path=outside if variant=="absolute" else root/".."/"private.txt"
    if variant=="symlink":
        path=root/"thumb.png";path.symlink_to(outside)
    elif variant=="directory":path=root
    with pytest.raises(ValueError):release_manifest.file_hash(path)


def test_manifest_rejects_legacy_injected_thumbnail_path(roots):
    root,base=roots
    video=root/"video.mp4";video.write_bytes(b"video fixture")
    outside=base/"private.txt";outside.write_bytes(b"private sentinel")
    with pytest.raises(ValueError):
        release_manifest.build_manifest({"id":"p","user_id":"u"},{"id":"j"},video,outside)


@pytest.mark.asyncio
@pytest.mark.parametrize("path",["/tmp/private.txt","../../private.txt","/app/backend/static/other-project/thumbnail.png"])
async def test_metadata_cannot_register_server_file_path(monkeypatch,path):
    monkeypatch.setattr(api,"get_db",lambda:pytest.fail("client path reached database"))
    with pytest.raises(HTTPException) as error:
        await api.create_asset("p",AssetCreate(name="x",asset_type="generated_thumbnail",file_path=path),user={"id":"u"})
    assert error.value.status_code==422


@pytest.mark.asyncio
async def test_metadata_only_assets_remain_supported(monkeypatch):
    insert=AsyncMock()
    db=SimpleNamespace(projects=SimpleNamespace(find_one=AsyncMock(return_value={"id":"p","user_id":"u"})),assets=SimpleNamespace(insert_one=insert))
    monkeypatch.setattr(api,"get_db",lambda:db)
    await api.create_asset("p",AssetCreate(name="brief",asset_type="generated_brief"),user={"id":"u"})
    assert insert.await_count==1
