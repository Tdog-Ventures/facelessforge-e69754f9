import ast
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import generation, tts
from app.auth import get_current_user
from app.models import ProjectUpdate, ForgotPasswordRequest
from app.routes import api

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_tts_unclosed_brackets_complete_in_bounded_time():
    # The old lazy wildcard retries from every '[' on an unmatched input.
    environment = {**os.environ, 'PYTHONPATH': str(BACKEND_ROOT)}
    subprocess.run([sys.executable, '-c',
                    "from app.tts import sanitize_for_tts; s='['*100000; assert sanitize_for_tts(s)==s"],
                   check=True, timeout=5, capture_output=True, env=environment)


def test_tts_preserves_spoken_words_and_removes_directions():
    assert tts.sanitize_for_tts('HOOK: Hello [pause] world.\nVisual: skyline\nCTA: Join us.') == 'Hello  world.\nJoin us.'


def test_generation_duration_is_already_bounded_at_customer_boundary():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ProjectUpdate(target_duration=10**12)


@pytest.mark.asyncio
async def test_scene_padding_is_finite_at_maximum_supported_duration(monkeypatch):
    monkeypatch.setattr(generation, '_llm_json', AsyncMock(return_value={'scenes': [{'narration_text': 'Hello.'}]}))
    scenes = await generation.generate_scene_plan({'id': 'p', 'target_duration': 3600}, {'full_script': 'Hello.'})
    assert len(scenes) == 120
    assert scenes[-1]['end_time'] <= 3600


@pytest.mark.asyncio
async def test_production_reset_never_returns_token(monkeypatch):
    monkeypatch.delenv('DEV_MODE', raising=False)
    collection = SimpleNamespace(count_documents=AsyncMock(return_value=0), insert_one=AsyncMock(), update_many=AsyncMock())
    db = SimpleNamespace(password_reset_attempts=collection, password_reset_tokens=collection,
                         users=SimpleNamespace(find_one=AsyncMock(return_value={'id': 'customer'})))
    monkeypatch.setattr(api, 'get_db', lambda: db)
    request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [], 'client': ('127.0.0.1', 1)})
    response = await api.forgot_password(ForgotPasswordRequest(email='test@example.com'), request)
    assert set(response) == {'ok', 'message'}


@pytest.mark.asyncio
async def test_youtube_provider_exception_never_leaks_secret(monkeypatch, caplog, tmp_path):
    import app.db
    from app.adapters.posters import youtube as adapter
    from app.routes import youtube
    # The publication gate runs before any provider interaction, so this
    # scenario needs an approved render job and its own artifact.
    import app.publish_gate as publish_gate
    artifact = tmp_path / 'approved.mp4'
    artifact.write_bytes(b'approved artifact')
    monkeypatch.setattr(publish_gate, 'STATIC_RENDERS', tmp_path / 'renders')
    monkeypatch.setattr(app.db, 'get_db', lambda: SimpleNamespace(
        users=SimpleNamespace(find_one=AsyncMock(return_value={'role': 'admin'})),
        projects=SimpleNamespace(find_one=AsyncMock(return_value={'id': 'p'})),
        render_jobs=SimpleNamespace(find_one=AsyncMock(return_value={
            'id': 'job-1', 'project_id': 'p', 'status': 'completed',
            'release_eligible': True, 'deterministic_qa_blocked': [],
            'current_step': 'completed', 'output_path': str(artifact)})),
    ))
    monkeypatch.setattr(adapter, 'rate_limit_check', lambda *a: (True, 5))
    monkeypatch.setenv('YOUTUBE_REFRESH_TOKEN', 'configured')
    reached = []
    def credentials():
        reached.append(True)
        raise RuntimeError('refresh_token=DO_NOT_DISCLOSE')
    monkeypatch.setattr(youtube, '_get_youtube_credentials', credentials)
    with pytest.raises(RuntimeError) as error:
        await adapter.YoutubePoster().post({'id': 'p', 'user_id': 'admin', 'name': 'Video'}, '', {'duration': 60}, 'admin')
    assert reached
    assert 'DO_NOT_DISCLOSE' not in str(error.value)
    assert 'DO_NOT_DISCLOSE' not in caplog.text


@pytest.mark.parametrize('release_fields,allowed', [
    ({'release_eligible': True, 'deterministic_qa_blocked': [], 'current_step': 'completed'}, True),
    ({'release_eligible': True, 'deterministic_qa_blocked': ['freeze']}, False),
    ({'release_eligible': True, 'deterministic_qa_blocked': ['silence']}, False),
    ({'release_eligible': True, 'deterministic_qa_blocked': ['loudnorm']}, False),
    ({'release_eligible': False, 'deterministic_qa_blocked': []}, False),
    ({'release_eligible': True, 'current_step': 'qa_blocked'}, False),
    ({'release_eligible': 'true', 'deterministic_qa_blocked': []}, False),
    ({}, False),
    ({'release_eligible': True, 'output_path': None}, False),
    ({'release_eligible': True, 'output_path': None, '_object_copy': True}, True),
    ({'release_eligible': True, 'output_path': '/missing-approved-artifact.mp4'}, False),
])
def test_external_upload_provider_error_is_not_echoed(monkeypatch, caplog, release_fields, allowed, tmp_path):
    # Drive the real auto-upload task with a provider exception carrying a
    # distinctive secret; it must not surface in logs or the persisted job error.
    import asyncio
    from app import external_api
    from app.routes import youtube as _youtube_mod
    SECRET = 'SECRET_REFRESH_TOKEN_DO_NOT_DISCLOSE'

    captured = {}
    approved_path = tmp_path / 'approved-job.mp4'
    approved_path.write_bytes(b'video test fixture')
    monkeypatch.setattr(external_api.render_service, 'STATIC_RENDERS', tmp_path / 'renders')

    def _fake_create_task(coro):
        captured['coro'] = coro
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr(asyncio, 'create_task', _fake_create_task)

    class FakeCursor:
        def __init__(self, docs):
            self.docs = docs
        def sort(self, *a, **k):
            return self
        async def to_list(self, n):
            return list(self.docs)

    class FakeCollection:
        def __init__(self):
            self.inserted = []
            self.updated = []
        async def insert_one(self, doc):
            self.inserted.append(doc)
        async def insert_many(self, docs):
            self.inserted.extend(docs)
        async def update_one(self, query, update, upsert=False):
            self.updated.append((query, update))
        async def find_one(self, query, projection=None):
            for doc in self.inserted:
                if all(doc.get(k) == v for k, v in query.items()):
                    return dict(doc)
            return None
        def find(self, query=None, projection=None):
            return FakeCursor(list(self.inserted))

    class FakeDB:
        def __init__(self):
            self._cols = {}
        def __getattr__(self, name):
            return self._cols.setdefault(name, FakeCollection())

    fake_db = FakeDB()
    monkeypatch.setattr(external_api, '_system_creator', AsyncMock(return_value={'id': 'sys', 'role': 'admin'}))
    monkeypatch.setattr(external_api, 'get_db', lambda: fake_db)
    # The auto-upload closure re-imports get_db from app.db; patch that source too.
    import app.db as _app_db
    monkeypatch.setattr(_app_db, 'get_db', lambda: fake_db)
    monkeypatch.setattr(external_api.render_service, 'validate_prerequisites', lambda *a: {'ok': True, 'issues': []})
    monkeypatch.setattr(external_api.render_service, 'queue_render', AsyncMock(return_value={'id': 'job-1'}))

    class OKCreds:
        def refresh(self, req):
            reached.append(True)

    class FakeUploadRequest:
        def next_chunk(self):
            raise RuntimeError(f'provider failed with {SECRET}')

    class FakeVideos:
        def insert(self, **kwargs):
            return FakeUploadRequest()

    class FakeService:
        def videos(self):
            return FakeVideos()

    import googleapiclient.discovery as _youtube_discovery
    import googleapiclient.http as _youtube_http
    uploaded_paths = []

    class CapturingMediaFileUpload:
        def __init__(self, filename, **kwargs):
            uploaded_paths.append(filename)

    monkeypatch.setattr(_youtube_http, 'MediaFileUpload', CapturingMediaFileUpload)
    monkeypatch.setattr(_youtube_discovery, 'build', lambda *args, **kwargs: FakeService())

    reached = []
    monkeypatch.setattr(_youtube_mod, '_get_youtube_credentials', lambda: OKCreds())
    monkeypatch.setenv('EXTERNAL_RENDER_ENABLED', 'true')
    monkeypatch.setenv('EXTERNAL_RENDER_API_KEY', 'test-key')

    # Neutralize downstream generators that would hit providers/filesystem.
    monkeypatch.setattr(external_api.gen, 'generate_metadata', AsyncMock(return_value={'title_options': ['T'], 'selected_title': 'T', 'description': 'd', 'tags': ['x'], 'youtube_chapters': [], 'social_caption': 'T'}))
    monkeypatch.setattr(external_api.gen, 'generate_thumbnails', AsyncMock(return_value=[]))
    monkeypatch.setattr(external_api.thumb_images, 'generate_thumbnail_images', AsyncMock(return_value=[]))
    monkeypatch.setattr(external_api.tts_service, 'generate_voiceover',
                        AsyncMock(return_value={'id': 'vo-1', 'status': 'generated'}))

    body = external_api.ExternalRenderRequest(title='T', script='A long enough script body.', auto_upload=True)
    asyncio.run(external_api.external_render_video(body, 'test-key'))
    assert 'coro' in captured, 'auto-upload task was not scheduled'

    async def _short_sleep(_t):
        return None

    async def _completed_job(_q, _p=None):
        if release_fields.get('_object_copy'):
            pid = fake_db.projects.inserted[0]['id']
            copy_path = tmp_path / 'renders' / pid / 'job-1_verify.mp4'
            copy_path.parent.mkdir(parents=True, exist_ok=True)
            copy_path.write_bytes(b'approved object verification copy')
        return {'id': 'job-1', 'status': 'completed', 'output_path': str(approved_path), **release_fields}

    async def _run():
        original_sleep = asyncio.sleep
        asyncio.sleep = _short_sleep
        fake_db.render_jobs.find_one = _completed_job
        try:
            await captured['coro']
        finally:
            asyncio.sleep = original_sleep

    asyncio.run(_run())

    assert bool(reached) is allowed, 'QA-blocked render reached provider'
    if allowed:
        if release_fields.get('output_path') is None and release_fields.get('_object_copy'):
            expected_path = str(tmp_path / 'renders' / fake_db.projects.inserted[0]['id'] / 'job-1_verify.mp4')
        else:
            expected_path = str(approved_path)
        assert uploaded_paths == [expected_path], 'upload used an unbound artifact'
    else:
        assert uploaded_paths == [], 'upload reached provider for a blocked render'
        assert fake_db.forge_runs.inserted == []
        assert 'YouTube upload blocked' in str(fake_db.forge_runs.updated)
    assert SECRET not in caplog.text, 'secret leaked into logs'
    recorded = str(fake_db.forge_runs.updated) + str(fake_db.forge_runs.inserted)
    assert SECRET not in recorded, 'secret leaked into persisted job error'


@pytest.mark.parametrize('role,expected', [(None, 401), ('creator', 403), ('admin', 200)])
def test_debug_file_inventory_requires_operator(tmp_path, role, expected):
    # Execute the actual server route in isolation, without production startup,
    # scheduler, MongoDB or optional provider imports.
    source = Path(__file__).parents[1] / 'server.py'
    node = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                if isinstance(n, ast.FunctionDef) and n.name == 'debug_files_8081')
    app = FastAPI()
    from app.publishing_access import ensure_operator
    namespace = dict(app=app, Depends=Depends, get_current_user=get_current_user, ensure_operator=ensure_operator,
                     _TEASER_TMP=tmp_path, _TEASER_DIR=tmp_path, _r2_client_patch=lambda: None)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    if role:
        app.dependency_overrides[get_current_user] = lambda: {'id': 'u', 'role': role}
    response = TestClient(app).get('/debug/files')
    assert response.status_code == expected
