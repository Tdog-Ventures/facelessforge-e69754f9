"""Shared publication-gate tests.

Covers the single enforcement boundary used by every public YouTube upload
channel: the external auto-upload, the operator manual upload, the forge
pipeline upload and the autopost adapter. A completed render is not permission
to publish, and no channel may substitute an arbitrary project-wide file for
the approved job's own artifact.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app import publish_gate
from app.publish_gate import PublicationBlocked


# --------------------------------------------------------------------------
# Gate semantics
# --------------------------------------------------------------------------

APPROVED_JOB = {
    "id": "job-1",
    "project_id": "p",
    "status": "completed",
    "release_eligible": True,
    "deterministic_qa_blocked": [],
    "current_step": "completed",
}


@pytest.mark.parametrize("job", [
    APPROVED_JOB,
    {**APPROVED_JOB, "deterministic_qa_blocked": []},
])
def test_approved_job_passes_the_gate(job):
    publish_gate.assert_publishable(job)


@pytest.mark.parametrize("job,reason", [
    ({**APPROVED_JOB, "release_eligible": False}, "qa"),
    ({**APPROVED_JOB, "release_eligible": "true"}, "qa"),
    ({**APPROVED_JOB, "release_eligible": None}, "qa"),
    ({**APPROVED_JOB, "deterministic_qa_blocked": ["freeze"]}, "qa"),
    ({**APPROVED_JOB, "deterministic_qa_blocked": ["silence"]}, "qa"),
    ({**APPROVED_JOB, "current_step": "qa_blocked"}, "qa"),
    ({**APPROVED_JOB, "status": "failed"}, "status"),
    ({**APPROVED_JOB, "status": "processing"}, "status"),
    ({**APPROVED_JOB, "status": "cancelled"}, "status"),
    ({}, "qa"),
    (None, "job"),
])
def test_unapproved_job_is_refused(job, reason):
    with pytest.raises(PublicationBlocked) as error:
        publish_gate.assert_publishable(job)
    assert "YouTube upload blocked" in str(error.value)
    if reason == "job":
        assert "no approved render job" in str(error.value)
    elif reason == "status":
        assert "not completed" in str(error.value)


# --------------------------------------------------------------------------
# Artifact binding
# --------------------------------------------------------------------------

def test_approved_job_artifact_is_the_job_output(tmp_path):
    artifact = tmp_path / "approved.mp4"
    artifact.write_bytes(b"approved artifact")
    resolved = publish_gate.resolve_approved_artifact(
        job={**APPROVED_JOB, "output_path": str(artifact)},
        project_id="p", static_renders=tmp_path / "renders")
    assert resolved == artifact


def test_object_storage_copy_falls_back_to_the_job_verification_copy(tmp_path):
    renders = tmp_path / "renders"
    retained = renders / "p" / "job-1_verify.mp4"
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b"retained verification copy")
    resolved = publish_gate.resolve_approved_artifact(
        job={**APPROVED_JOB, "output_path": None},
        project_id="p", static_renders=renders)
    assert resolved == retained


def test_artifact_resolution_refuses_to_substitute_another_file(tmp_path):
    renders = tmp_path / "renders"
    renders.mkdir(parents=True)
    # A different job's verification copy must not be published for job-1.
    other = renders / "p" / "job-OTHER_verify.mp4"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"another job's copy")
    with pytest.raises(PublicationBlocked):
        publish_gate.resolve_approved_artifact(
            job={**APPROVED_JOB, "output_path": None},
            project_id="p", static_renders=renders)


def test_artifact_resolution_refuses_a_missing_artifact(tmp_path):
    with pytest.raises(PublicationBlocked) as error:
        publish_gate.resolve_approved_artifact(
            job={**APPROVED_JOB, "output_path": str(tmp_path / "gone.mp4")},
            project_id="p", static_renders=tmp_path / "renders")
    assert "approved artifact unavailable" in str(error.value)


# --------------------------------------------------------------------------
# Job binding
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_job_binding_prefers_the_renderers_recorded_job():
    bound = {**APPROVED_JOB, "id": "job-bound"}
    newer = {**APPROVED_JOB, "id": "job-newer"}

    def _find_job(query, *_args, **_kwargs):
        return bound if query.get("id") == "job-bound" else newer

    db = SimpleNamespace(
        projects=SimpleNamespace(find_one=AsyncMock(return_value={"id": "p", "rendered_video_asset_id": "job-bound"})),
        render_jobs=SimpleNamespace(find_one=AsyncMock(side_effect=_find_job)),
    )
    job = await publish_gate.find_approved_job(db=db, project_id="p")
    assert job["id"] == "job-bound"


@pytest.mark.asyncio
async def test_job_binding_falls_back_to_the_newest_completed_job():
    db = SimpleNamespace(
        projects=SimpleNamespace(find_one=AsyncMock(return_value={"id": "p"})),
        render_jobs=SimpleNamespace(find_one=AsyncMock(return_value=APPROVED_JOB)),
    )
    job = await publish_gate.find_approved_job(db=db, project_id="p")
    assert job["id"] == "job-1"


# --------------------------------------------------------------------------
# Sibling channels: operator manual upload
# --------------------------------------------------------------------------

def _manual_db(monkeypatch, job, project=None):
    from app.routes import youtube
    project = project or {"id": "p", "user_id": "admin", "name": "Video"}
    db = SimpleNamespace(
        projects=SimpleNamespace(find_one=AsyncMock(return_value=project)),
        metadata_packages=SimpleNamespace(find_one=AsyncMock(return_value={})),
        render_jobs=SimpleNamespace(find_one=AsyncMock(return_value=job)),
        forge_runs=SimpleNamespace(insert_one=AsyncMock()),
    )
    monkeypatch.setattr(youtube, "get_db", lambda: db)
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("job", [
    {**APPROVED_JOB, "release_eligible": False},
    {**APPROVED_JOB, "deterministic_qa_blocked": ["freeze"]},
    {**APPROVED_JOB, "current_step": "qa_blocked"},
])
async def test_manual_upload_refuses_a_qa_blocked_render(monkeypatch, job):
    from app.routes import youtube
    _manual_db(monkeypatch, job)
    provider_calls = []
    monkeypatch.setattr(youtube, "_get_youtube_credentials",
                        lambda: provider_calls.append(True))
    with pytest.raises(HTTPException) as error:
        await youtube.youtube_upload(
            youtube.YoutubeUploadRequest(project_id="p"),
            user={"id": "admin", "role": "admin"})
    assert error.value.status_code == 409
    assert "YouTube upload blocked" in str(error.value.detail)
    assert provider_calls == [], "provider was contacted for a blocked render"


@pytest.mark.asyncio
async def test_manual_upload_publishes_the_approved_job_artifact(monkeypatch, tmp_path):
    from app.routes import youtube
    artifact = tmp_path / "approved.mp4"
    artifact.write_bytes(b"approved artifact")
    monkeypatch.setattr(publish_gate, "STATIC_RENDERS", tmp_path / "renders")
    _manual_db(monkeypatch, {**APPROVED_JOB, "output_path": str(artifact)})

    uploaded = {}

    class FakeMedia:
        def __init__(self, path, **kwargs):
            uploaded["path"] = path

    class FakeRequest:
        def __init__(self):
            self.done = False

        def next_chunk(self):
            self.done = True
            return None, {"id": "vid-1"}

    class FakeVideoInsertion:
        def insert(self, **kwargs):
            uploaded["insert"] = kwargs
            return FakeRequest()

    class FakeService:
        def videos(self):
            return FakeVideoInsertion()

    class FakeCreds:
        def refresh(self, request):
            return None

    monkeypatch.setattr(youtube, "_get_youtube_credentials", lambda: FakeCreds())
    import googleapiclient.discovery as discovery
    monkeypatch.setattr(discovery, "build", lambda *a, **k: FakeService())
    import googleapiclient.http as ghttp
    monkeypatch.setattr(ghttp, "MediaFileUpload", FakeMedia)

    result = await youtube.youtube_upload(
        youtube.YoutubeUploadRequest(project_id="p"),
        user={"id": "admin", "role": "admin"})
    assert result["youtube_video_id"] == "vid-1"
    assert uploaded["path"] == str(artifact)


@pytest.mark.asyncio
async def test_manual_upload_never_substitutes_an_arbitrary_project_file(monkeypatch, tmp_path):
    from app.routes import youtube
    stray = tmp_path / "stray.mp4"
    stray.write_bytes(b"stray project file")
    _manual_db(monkeypatch, {**APPROVED_JOB, "output_path": str(tmp_path / "gone.mp4")})
    provider_calls = []
    monkeypatch.setattr(youtube, "_get_youtube_credentials",
                        lambda: provider_calls.append(True))
    with pytest.raises(HTTPException) as error:
        await youtube.youtube_upload(
            youtube.YoutubeUploadRequest(project_id="p"),
            user={"id": "admin", "role": "admin"})
    assert error.value.status_code == 409
    assert provider_calls == []


# --------------------------------------------------------------------------
# Sibling channels: autopost adapter
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("job", [
    {**APPROVED_JOB, "release_eligible": False},
    {**APPROVED_JOB, "deterministic_qa_blocked": ["silence"]},
    {**APPROVED_JOB, "current_step": "qa_blocked"},
])
async def test_autopost_adapter_refuses_a_qa_blocked_render(monkeypatch, job):
    import app.db
    from app.adapters.posters import youtube as adapter
    monkeypatch.setattr(adapter, "rate_limit_check", lambda *a: (True, 5))
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "configured")
    db = SimpleNamespace(
        users=SimpleNamespace(find_one=AsyncMock(return_value={"role": "admin"})),
        projects=SimpleNamespace(find_one=AsyncMock(return_value={"id": "p"})),
        render_jobs=SimpleNamespace(find_one=AsyncMock(return_value=job)),
    )
    monkeypatch.setattr(app.db, "get_db", lambda: db)
    provider_calls = []
    from app.routes import youtube as route_module
    monkeypatch.setattr(route_module, "_get_youtube_credentials",
                        lambda: provider_calls.append(True))
    with pytest.raises(PublicationBlocked):
        await adapter.YoutubePoster().post(
            {"id": "p", "user_id": "admin", "name": "Video"}, "", {"duration": 60}, "admin")
    assert provider_calls == [], "provider was contacted for a blocked render"


# --------------------------------------------------------------------------
# Sibling channels: forge pipeline upload
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("job", [
    {**APPROVED_JOB, "release_eligible": False},
    {**APPROVED_JOB, "deterministic_qa_blocked": ["freeze"]},
    {**APPROVED_JOB, "current_step": "qa_blocked"},
])
async def test_forge_pipeline_refuses_a_qa_blocked_render(monkeypatch, job):
    import app.db
    from app.workers import forge_loop
    db = SimpleNamespace(
        users=SimpleNamespace(find_one=AsyncMock(return_value={"role": "admin"})),
        metadata_packages=SimpleNamespace(find_one=AsyncMock(return_value={})),
        projects=SimpleNamespace(find_one=AsyncMock(return_value={"id": "p"})),
        render_jobs=SimpleNamespace(find_one=AsyncMock(return_value=job)),
    )
    monkeypatch.setattr(app.db, "get_db", lambda: db)
    provider_calls = []
    from app.routes import youtube as route_module
    monkeypatch.setattr(route_module, "_get_youtube_credentials",
                        lambda: provider_calls.append(True))
    with pytest.raises(PublicationBlocked):
        await forge_loop._youtube_upload({"id": "p", "user_id": "admin", "name": "Video"})
    assert provider_calls == [], "provider was contacted for a blocked render"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "processing", "cancelled"])
async def test_forge_pipeline_refuses_an_incomplete_render(monkeypatch, status):
    import app.db
    from app.workers import forge_loop
    job = {**APPROVED_JOB, "status": status}
    db = SimpleNamespace(
        users=SimpleNamespace(find_one=AsyncMock(return_value={"role": "admin"})),
        metadata_packages=SimpleNamespace(find_one=AsyncMock(return_value={})),
        projects=SimpleNamespace(find_one=AsyncMock(return_value={"id": "p"})),
        render_jobs=SimpleNamespace(find_one=AsyncMock(return_value=job)),
    )
    monkeypatch.setattr(app.db, "get_db", lambda: db)
    provider_calls = []
    from app.routes import youtube as route_module
    monkeypatch.setattr(route_module, "_get_youtube_credentials",
                        lambda: provider_calls.append(True))
    with pytest.raises(PublicationBlocked) as error:
        await forge_loop._youtube_upload({"id": "p", "user_id": "admin", "name": "Video"})
    assert "not completed" in str(error.value)
    assert provider_calls == [], "provider was contacted for an incomplete render"


# --------------------------------------------------------------------------
# Every channel shares one boundary
# --------------------------------------------------------------------------

def test_all_publishing_channels_use_the_shared_boundary():
    """A new upload channel must not be able to skip the shared gate."""
    import app.external_api as external_api
    import app.routes.youtube as route_module
    import app.adapters.posters.youtube as adapter_module
    import app.workers.forge_loop as forge_loop
    for module in (external_api, route_module, adapter_module, forge_loop):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "publish_gate" in source or "assert_publishable" in source, (
            f"{module.__name__} does not use the shared publication gate")
