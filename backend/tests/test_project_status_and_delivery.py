import io
import json
import zipfile

import pytest

from app import render, storage
from app.routes import api as api_routes
from app.scoring import compute_project_status
from app.workers import forge_loop


class _AsyncCursor:
    def __init__(self, documents=None):
        self.documents = documents or []

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, *_args, **_kwargs):
        return [dict(document) for document in self.documents]


class _FakeCollection:
    def __init__(self, document=None, documents=None):
        self.document = document
        self.documents = documents or []

    async def find_one(self, *_args, **_kwargs):
        return dict(self.document) if self.document is not None else None

    def find(self, *_args, **_kwargs):
        return _AsyncCursor(self.documents)

    async def update_one(self, *_args, **_kwargs):
        return None


def test_completed_render_status_is_case_insensitive():
    status = compute_project_status(
        has_script=True,
        has_scenes=True,
        has_metadata=True,
        has_assets=True,
        render_status="completed",
    )

    assert status == "COMPLETED"


def test_failed_render_status_is_case_insensitive():
    status = compute_project_status(
        has_script=True,
        has_scenes=True,
        has_metadata=True,
        has_assets=True,
        render_status="failed",
    )

    assert status == "FAILED"


def test_delivery_url_is_refreshed_from_storage_key(monkeypatch):
    class FakeObjectStorage:
        mode = "object"

        def get_presigned_url(self, key):
            return f"fresh://{key}"

    monkeypatch.setattr(storage, "_BACKEND", FakeObjectStorage())
    resolver = getattr(storage, "resolve_delivery_url", None)

    assert callable(resolver), "storage must expose resolve_delivery_url"
    assert resolver({
        "output_storage_key": "renders/project/job.mp4",
        "output_url": "expired://signed-url",
    }) == "fresh://renders/project/job.mp4"


def test_delivery_url_keeps_stable_local_preview(monkeypatch):
    class FakeLocalStorage:
        mode = "local"

    monkeypatch.setattr(storage, "_BACKEND", FakeLocalStorage())
    resolver = getattr(storage, "resolve_delivery_url", None)

    assert callable(resolver), "storage must expose resolve_delivery_url"
    assert resolver({
        "output_relative_url": "/api/static/renders/project/job.mp4",
        "output_url": "https://example.invalid/legacy.mp4",
    }) == "/api/static/renders/project/job.mp4"


def test_render_job_payload_replaces_expired_persisted_url(monkeypatch):
    monkeypatch.setattr(
        api_routes,
        "resolve_delivery_url",
        lambda _job: "fresh://signed-url",
    )
    serializer = getattr(api_routes, "_render_job_payload", None)

    assert callable(serializer), "render API responses must refresh delivery URLs"
    original = {
        "id": "job-1",
        "status": "completed",
        "output_storage_key": "renders/project-1/job-1.mp4",
        "output_url": "expired://signed-url",
    }

    payload = serializer(original)

    assert payload["output_url"] == "fresh://signed-url"
    assert original["output_url"] == "expired://signed-url"


@pytest.mark.asyncio
async def test_job_download_endpoint_refreshes_the_delivery_url(monkeypatch):
    class FakeCollection:
        def __init__(self, document):
            self.document = document

        async def find_one(self, *_args, **_kwargs):
            return dict(self.document)

    class FakeDB:
        projects = FakeCollection({"id": "project-1", "user_id": "user-1"})
        render_jobs = FakeCollection({
            "id": "job-1",
            "project_id": "project-1",
            "status": "completed",
            "output_storage_key": "renders/project-1/job-1.mp4",
            "output_url": "expired://signed-url",
        })

    monkeypatch.setattr(api_routes, "get_db", lambda: FakeDB())
    monkeypatch.setattr(api_routes, "resolve_delivery_url", lambda _job: "fresh://signed-url", raising=False)
    endpoint = getattr(api_routes, "download_render_job", None)

    assert callable(endpoint), "API must expose a per-job download endpoint"
    response = await endpoint(
        "project-1",
        "job-1",
        user={"id": "user-1", "role": "creator"},
    )

    assert response.status_code == 302
    assert response.headers["location"] == "fresh://signed-url"


@pytest.mark.asyncio
async def test_public_share_refreshes_video_and_download_urls(monkeypatch):
    project = {
        "id": "project-1",
        "user_id": "user-1",
        "name": "Project One",
        "niche": "testing",
        "status": "COMPLETED",
        "share_enabled": True,
        "share_token": "share-token-1",
    }
    final = {
        "id": "job-1",
        "project_id": "project-1",
        "status": "completed",
        "output_storage_key": "renders/project-1/job-1.mp4",
        "output_url": "expired://signed-url",
    }

    class FakeDB:
        projects = _FakeCollection(project)
        metadata_packages = _FakeCollection()
        assets = _FakeCollection()
        render_jobs = _FakeCollection(final)
        provider_settings = _FakeCollection()

    monkeypatch.setattr(api_routes, "get_db", lambda: FakeDB())
    monkeypatch.setattr(api_routes, "resolve_delivery_url", lambda _job: "fresh://signed-url")

    payload = await api_routes.public_share("share-token-1")

    assert payload["final_video"]["url"] == "fresh://signed-url"
    assert payload["final_video"]["download_url"] == "fresh://signed-url"
    assert payload["download"]["mp4"] == "fresh://signed-url"


@pytest.mark.asyncio
async def test_export_package_refreshes_render_manifest_url(monkeypatch):
    project = {
        "id": "project-1",
        "user_id": "user-1",
        "name": "Project One",
        "niche": "testing",
        "topic": "delivery",
        "target_duration": 60,
    }
    final = {
        "id": "job-1",
        "status": "completed",
        "output_storage_key": "renders/project-1/job-1.mp4",
        "output_url": "expired://signed-url",
    }

    class FakeDB:
        projects = _FakeCollection(project)
        scripts = _FakeCollection()
        scenes = _FakeCollection()
        metadata_packages = _FakeCollection()
        assets = _FakeCollection()
        render_jobs = _FakeCollection(final)

    monkeypatch.setattr(api_routes, "get_db", lambda: FakeDB())
    monkeypatch.setattr(api_routes, "resolve_delivery_url", lambda _job: "fresh://signed-url")

    response = await api_routes.export_package_zip(
        "project-1", user={"id": "user-1", "role": "creator"}
    )
    body = b"".join([chunk async for chunk in response.body_iterator])
    with zipfile.ZipFile(io.BytesIO(body)) as package:
        manifest = json.loads(package.read("render.json"))

    assert manifest["url"] == "fresh://signed-url"


@pytest.mark.asyncio
async def test_email_preview_refreshes_render_url(monkeypatch):
    from app import email as email_module

    project = {"id": "project-1", "user_id": "user-1", "name": "Project One"}
    final = {
        "id": "job-1",
        "status": "completed",
        "output_storage_key": "renders/project-1/job-1.mp4",
        "output_url": "expired://signed-url",
    }

    class FakeDB:
        projects = _FakeCollection(project)
        render_jobs = _FakeCollection(final)

    monkeypatch.setattr(api_routes, "get_db", lambda: FakeDB())
    monkeypatch.setattr(api_routes, "resolve_delivery_url", lambda _job: "fresh://signed-url")
    monkeypatch.setattr(email_module, "get_email_service_status", lambda: {"configured": True})
    monkeypatch.setattr(
        email_module,
        "render_video_ready_email",
        lambda **kwargs: {"text": "ready", "mp4_url": kwargs["mp4_url"]},
    )

    payload = await api_routes.email_preview(
        "project-1", user={"id": "user-1", "role": "creator"}
    )

    assert payload["mp4_url"] == "fresh://signed-url"
    assert payload["email"]["mp4_url"] == "fresh://signed-url"


@pytest.mark.asyncio
async def test_email_send_refreshes_render_url(monkeypatch):
    from app import email as email_module

    project = {"id": "project-1", "user_id": "owner-1", "name": "Project One"}
    final = {
        "id": "job-1",
        "status": "completed",
        "output_storage_key": "renders/project-1/job-1.mp4",
        "output_url": "expired://signed-url",
    }

    class FakeDB:
        projects = _FakeCollection(project)
        render_jobs = _FakeCollection(final)
        users = _FakeCollection({"id": "owner-1", "email": "owner@example.test"})

    async def fake_send_video_ready_email(**kwargs):
        return {"sent": True, "mp4_url": kwargs["mp4_url"]}

    monkeypatch.setattr(api_routes, "get_db", lambda: FakeDB())
    monkeypatch.setattr(api_routes, "resolve_delivery_url", lambda _job: "fresh://signed-url")
    monkeypatch.setattr(email_module, "send_video_ready_email", fake_send_video_ready_email)

    payload = await api_routes.email_send(
        "project-1", user={"id": "owner-1", "role": "creator"}
    )

    assert payload["mp4_url"] == "fresh://signed-url"


@pytest.mark.asyncio
async def test_render_failure_persists_failed_project_status(monkeypatch):
    project_updates = []

    async def fail_render(_job_id, _project_id):
        raise RuntimeError("provider unavailable")

    async def capture_job_update(_job_id, **_patch):
        return None

    class FakeProjects:
        async def update_one(self, query, update):
            project_updates.append((query, update))

    class FakeDB:
        projects = FakeProjects()

    monkeypatch.setattr(render, "_run_render", fail_render)
    monkeypatch.setattr(render, "_set_job", capture_job_update)
    monkeypatch.setattr(render, "get_db", lambda: FakeDB())

    await render._run_render_safe("job-1", "project-1")

    assert project_updates == [(
        {"id": "project-1"},
        {"$set": {"status": "FAILED"}, "$currentDate": {"updated_at": True}},
    )]


@pytest.mark.asyncio
async def test_verifying_render_is_still_active(monkeypatch):
    captured_queries = []

    class FakeRenderJobs:
        async def find_one(self, query, _projection):
            captured_queries.append(query)
            return {"id": "job-1"}

    class FakeDB:
        render_jobs = FakeRenderJobs()

    monkeypatch.setattr(render, "get_db", lambda: FakeDB())

    assert await render.is_render_active("project-1") is True
    assert "verifying" in captured_queries[0]["status"]["$in"]


def test_legacy_uppercase_statuses_are_covered_by_queries():
    assert "RENDERING" in render.ACTIVE_RENDER_STATUS_VALUES
    assert "VERIFYING" in render.ACTIVE_RENDER_STATUS_VALUES
    assert "QUEUED" in render.ACTIVE_RENDER_STATUS_VALUES
    assert "COMPLETED" in render.COMPLETED_RENDER_STATUS_VALUES
    assert "completed" in render.COMPLETED_RENDER_STATUS_VALUES
    assert render.is_active_job_status("RENDERING") is True
    assert render.is_active_job_status("Verifying") is True
    assert render.is_active_job_status("FAILED") is False
    assert render.is_active_job_status("completed") is False
    assert render.normalize_job_status("FAILED") == "failed"
    assert render.normalize_job_status("COMPLETED") == "completed"


@pytest.mark.asyncio
async def test_cancel_rejects_terminal_uppercase_status(monkeypatch):
    class FakeRenderJobs:
        async def find_one(self, *_args, **_kwargs):
            return {"id": "job-1", "project_id": "project-1", "status": "FAILED"}

    class FakeDB:
        render_jobs = FakeRenderJobs()

    monkeypatch.setattr(render, "get_db", lambda: FakeDB())

    assert await render.cancel_render("project-1", "job-1") is False


@pytest.mark.asyncio
async def test_job_download_endpoint_handles_uppercase_completed(monkeypatch):
    captured = []

    class FakeProjects:
        async def find_one(self, *_args, **_kwargs):
            return {"id": "project-1", "user_id": "user-1"}

    class FakeRenderJobs:
        async def find_one(self, query, *_args, **_kwargs):
            captured.append(query)
            return {
                "id": "job-1",
                "project_id": "project-1",
                "status": "COMPLETED",
                "output_storage_key": "renders/project-1/job-1.mp4",
                "output_url": "expired://signed-url",
            }

    class FakeDB:
        projects = FakeProjects()
        render_jobs = FakeRenderJobs()

    monkeypatch.setattr(api_routes, "get_db", lambda: FakeDB())
    monkeypatch.setattr(api_routes, "resolve_delivery_url", lambda _job: "fresh://signed-url", raising=False)
    endpoint = getattr(api_routes, "download_render_job", None)

    assert callable(endpoint), "API must expose a per-job download endpoint"
    response = await endpoint(
        "project-1",
        "job-1",
        user={"id": "user-1", "role": "creator"},
    )

    assert response.status_code == 302
    assert response.headers["location"] == "fresh://signed-url"
    # Legacy COMPLETED rows must match the same endpoint query.
    assert captured, "download must query render_jobs"
    allowed = captured[0].get("status", {}).get("$in", [])
    assert "completed" in allowed and "COMPLETED" in allowed


@pytest.mark.asyncio
async def test_billing_failure_blocks_render_queue(monkeypatch):
    inserted_jobs = []

    class FakeRenderJobs:
        async def find_one(self, *_args, **_kwargs):
            return None

        async def count_documents(self, *_args, **_kwargs):
            return 0

        async def insert_one(self, document):
            inserted_jobs.append(document)

    class FailingSubscriptions:
        async def find_one(self, *_args, **_kwargs):
            raise RuntimeError("billing database unavailable")

    class FakeProjects:
        # Render lease acquisition reads and fences the project record.
        async def find_one(self, *_args, **_kwargs):
            return {"id": "project-1", "user_id": "user-1", "production_mode": "editorial"}

        async def update_one(self, *_args, **_kwargs):
            return type("R", (), {"matched_count": 1, "modified_count": 1})()

    class FakeDB:
        render_jobs = FakeRenderJobs()
        subscriptions = FailingSubscriptions()
        projects = FakeProjects()

    monkeypatch.setattr(render, "get_db", lambda: FakeDB())

    with pytest.raises(RuntimeError, match="authorize render credits"):
        await render.queue_render("project-1", requested_by="user-1")

    assert inserted_jobs == []


@pytest.mark.asyncio
async def test_disabled_forge_setting_survives_process_restart(monkeypatch):
    scheduler_starts = []

    class FakeSettings:
        async def find_one(self, *_args, **_kwargs):
            return {"id": "default", "auto_post_enabled": False}

    class FakeDB:
        forge_settings = FakeSettings()

    monkeypatch.setattr(forge_loop, "get_db", lambda: FakeDB(), raising=False)
    monkeypatch.setattr(forge_loop, "start_scheduler", lambda: scheduler_starts.append(True))
    monkeypatch.setenv("AUTO_POST_ENABLED", "true")
    restore = getattr(forge_loop, "restore_scheduler_setting", None)

    assert callable(restore), "startup must restore the persisted automation toggle"
    assert await restore() is False
    assert forge_loop.os.environ["AUTO_POST_ENABLED"] == "false"
    assert scheduler_starts == []


@pytest.mark.asyncio
async def test_missing_forge_setting_defaults_to_disabled(monkeypatch):
    class EmptySettings:
        async def find_one(self, *_args, **_kwargs):
            return None

    class FakeDB:
        forge_settings = EmptySettings()

    monkeypatch.setattr(forge_loop, "get_db", lambda: FakeDB(), raising=False)
    monkeypatch.setattr(forge_loop, "start_scheduler", lambda: pytest.fail("scheduler started"))
    monkeypatch.setenv("AUTO_POST_ENABLED", "true")

    assert await forge_loop.restore_scheduler_setting() is False
