"""Check publication privacy at every YouTube upload boundary."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import db as db_module, external_api
from app.adapters.posters import youtube as poster_module
from app.routes import youtube, youtube_oauth
from app.workers import autopost, forge_loop
from app.youtube_policy import resolve_youtube_privacy


@pytest.mark.parametrize("policy", ["private", "unlisted", "public"])
@pytest.mark.parametrize("requested", [None, "private", "unlisted", "public"])
def test_requests_cannot_exceed_deployment_visibility(monkeypatch, policy, requested):
    monkeypatch.setenv("YOUTUBE_PRIVACY", policy)
    expected = {
        "private": {None: "private", "private": "private", "unlisted": "private", "public": "private"},
        "unlisted": {None: "unlisted", "private": "private", "unlisted": "unlisted", "public": "unlisted"},
        "public": {None: "public", "private": "private", "unlisted": "unlisted", "public": "public"},
    }
    assert resolve_youtube_privacy(requested) == expected[policy][requested]


@pytest.mark.parametrize("requested", ["", "invalid"])
def test_invalid_request_fails_private(monkeypatch, requested):
    monkeypatch.setenv("YOUTUBE_PRIVACY", "public")
    assert resolve_youtube_privacy(requested) == "private"


def test_visibility_values_are_normalized(monkeypatch):
    monkeypatch.setenv("YOUTUBE_PRIVACY", " PUBLIC ")
    assert resolve_youtube_privacy(" Private ") == "private"


@pytest.fixture
def upload_context(monkeypatch, tmp_path):
    from googleapiclient import discovery

    path = tmp_path / "render.mp4"
    path.write_bytes(b"synthetic video" * 10000)
    project = {"id": "project-1", "user_id": "owner-1", "name": "Private video", "auto_post": True}
    job = {"id": "job-1", "status": "completed", "output_path": str(path)}
    database = SimpleNamespace()
    for name, document in (("projects", project), ("metadata_packages", {}),
                           ("render_jobs", job), ("forge_runs", None),
                           ("scripts", {}), ("scenes", None), ("assets", None),
                           ("longform_projects", None)):
        collection = MagicMock()
        collection.find_one = AsyncMock(return_value=document)
        collection.insert_one = AsyncMock()
        collection.insert_many = AsyncMock()
        collection.update_one = AsyncMock()
        collection.create_index = AsyncMock()
        collection.find.return_value = collection
        collection.sort.return_value = collection
        collection.to_list = AsyncMock(return_value=[])
        setattr(database, name, collection)
    monkeypatch.setattr(db_module, "get_db", lambda: database)
    monkeypatch.setattr(youtube, "get_db", lambda: database)
    monkeypatch.setattr(youtube, "_get_youtube_credentials", lambda: MagicMock())
    monkeypatch.setattr(youtube, "_resolve_video_path", lambda _pid: path)
    monkeypatch.setenv("YOUTUBE_MOCK", "false")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    service = MagicMock()
    insert = service.videos.return_value.insert
    insert.return_value.next_chunk.return_value = (None, {"id": "video-1"})
    monkeypatch.setattr(discovery, "build", lambda *_a, **_kw: service)
    return SimpleNamespace(db=database, project=project, path=path, insert=insert)


async def _upload(sink, context, monkeypatch):
    if sink == "poster":
        monkeypatch.setattr(poster_module, "rate_limit_check", lambda *_a: (True, 5))
        monkeypatch.setattr(poster_module, "log_analytics", lambda *_a: None)
        poster = poster_module.YoutubePoster()
        poster.vault_tokens = {"youtube_refresh": "synthetic-refresh"}
        result = await poster.post(context.project, str(context.path), {}, "owner-1")
        assert result["status"] == "uploaded"
    elif sink == "forge":
        result = await forge_loop._youtube_upload(context.project)
        assert result["status"] == "uploaded"
    elif sink == "route":
        result = await youtube.youtube_upload(
            youtube.YoutubeUploadRequest(project_id="project-1", privacy="public"),
            user={"id": "owner-1", "role": "creator"},
        )
        assert result["ok"] is True
        assert result["privacy"] == context.insert.call_args.kwargs["body"]["status"]["privacyStatus"]
    elif sink == "longform":
        from app.routes import longform

        for name in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
            monkeypatch.setattr(longform, name, "synthetic-credential")
        monkeypatch.setattr(longform, "get_db", lambda: context.db)
        monkeypatch.setattr(longform, "_call_openrouter_10min", lambda *_a: {})
        monkeypatch.setattr(longform, "_get_broll_clips_via_teaser", lambda *_a: [])
        monkeypatch.setattr(longform, "_render_longform", lambda *_a, **_kw: context.path)
        result = await longform.longform_generate(
            longform.LongformRequest(companyName="Synthetic Company", privacy="public"),
            request=SimpleNamespace(), user={"id": "owner-1"},
        )
        assert result.youtube["status"] == "uploaded"
        stored = context.db.longform_projects.update_one.call_args.args[1]["$set"]
        assert stored["privacy"] == context.insert.call_args.kwargs["body"]["status"]["privacyStatus"]
    elif sink == "oauth":
        import httpx

        redis = MagicMock()
        redis.hgetall.return_value = {"refresh_token_enc": "synthetic-encrypted-token"}
        fernet = MagicMock()
        fernet.decrypt.return_value = b"synthetic-refresh"
        monkeypatch.setattr(youtube_oauth, "_verify_service_token", lambda _token: True)
        monkeypatch.setattr(youtube_oauth, "_redis_client", lambda: redis)
        monkeypatch.setattr(youtube_oauth, "_fernet", lambda: (fernet, ValueError))
        monkeypatch.setattr(youtube_oauth, "_refresh_access_token", AsyncMock(return_value="synthetic-access"))

        async def chunks(*_a):
            yield b"synthetic video"

        response = MagicMock(status_code=200)
        response.aiter_bytes = chunks
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.stream.return_value.__aenter__ = AsyncMock(return_value=response)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: client)
        result = await youtube_oauth.youtube_auto_publish(youtube_oauth.AutoPublishRequest(
            tenant_id="synthetic-tenant", video_url="https://example.invalid/video.mp4",
            title="Private video", privacy="public",
        ))
        assert result["ok"] is True
    elif sink == "external":
        monkeypatch.setattr(external_api, "get_db", lambda: context.db)
        monkeypatch.setattr(external_api, "_require_external_key", lambda _key: None)
        monkeypatch.setattr(external_api, "_system_creator", AsyncMock(return_value={"id": "owner-1"}))
        monkeypatch.setattr(external_api.gen, "generate_metadata", AsyncMock(return_value={}))
        monkeypatch.setattr(external_api.gen, "generate_thumbnails", AsyncMock(return_value=[]))
        monkeypatch.setattr(external_api.tts_service, "generate_voiceover", AsyncMock(return_value={}))
        monkeypatch.setattr(external_api.render_service, "validate_prerequisites", lambda *_a: {"ok": True})
        monkeypatch.setattr(external_api.render_service, "queue_render", AsyncMock(return_value={"id": "job-1"}))
        tasks = []
        monkeypatch.setattr(asyncio, "create_task", tasks.append)
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        await external_api.external_render_video(external_api.ExternalRenderRequest(
            title="Private video", script="Synthetic private script.", auto_upload=True,
        ), x_facelessforge_key=None)
        assert len(tasks) == 1
        await tasks[0]


@pytest.mark.parametrize("sink", ["poster", "forge", "route", "longform", "oauth", "external"])
@pytest.mark.parametrize("policy,expected", [
    (None, "private"), ("private", "private"), ("unlisted", "unlisted"),
    ("public", "public"), ("", "private"), ("invalid", "private"),
])
@pytest.mark.asyncio
async def test_upload_requests_enforce_policy(monkeypatch, upload_context, sink, policy, expected):
    if policy is None:
        monkeypatch.delenv("YOUTUBE_PRIVACY", raising=False)
    else:
        monkeypatch.setenv("YOUTUBE_PRIVACY", policy)
    await _upload(sink, upload_context, monkeypatch)
    upload_context.insert.assert_called_once()
    payload = upload_context.insert.call_args.kwargs
    assert payload["part"] == "snippet,status"
    assert payload["body"]["status"]["privacyStatus"] == expected


@pytest.mark.asyncio
async def test_forge_projects_skip_render_autopost(monkeypatch, upload_context):
    project = await forge_loop._create_project("Private topic", "Testing", "owner-1")
    stored = upload_context.db.projects.insert_one.call_args.args[0]
    assert stored["auto_post"] is False
    assert project["auto_post"] is False
    result = await autopost.autopost_after_render(project, {"output_path": str(upload_context.path)}, {})
    assert result == {"skipped": "auto_post false"}
    upload_context.insert.assert_not_called()
