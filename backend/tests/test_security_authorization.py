from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import external_api
from app.routes import api, longform, youtube
from app.workers import forge_loop


@pytest.mark.parametrize('job_id', ['../outside', '..\\outside', '/tmp/out', 'C:\\out', 'a/b'])
def test_longform_rejects_traversal_job_ids(job_id):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        longform.LongformRequest(companyName='Acme', jobId=job_id)


def request():
    return Request({'type': 'http', 'method': 'GET', 'scheme': 'https',
                    'path': '/', 'headers': [], 'server': ('test', 443)})


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['toggle', 'run', 'longform', 'youtube_auth', 'alias', 'auto'])
async def test_customer_cannot_control_shared_channel(monkeypatch, action):
    user = {'id': 'customer', 'role': 'creator'}
    # Denial must precede database, provider, filesystem, or scheduler effects.
    def forbidden(*args, **kwargs):
        pytest.fail('unauthorized action reached an effect')
    for module in (forge_loop, longform, youtube, api):
        monkeypatch.setattr(module, 'get_db', forbidden)
    import app.db
    monkeypatch.setattr(app.db, 'get_db', forbidden)
    monkeypatch.setattr(forge_loop, 'run_forge_once', forbidden)
    monkeypatch.setattr(longform, '_call_openrouter_10min', forbidden)
    with pytest.raises(HTTPException) as error:
        if action == 'toggle':
            await forge_loop.forge_toggle(True, user=user)
        elif action == 'run':
            await forge_loop.forge_run_now(user=user)
        elif action == 'longform':
            await longform.longform_generate(longform.LongformRequest(companyName='Acme'), request(), user=user)
        elif action == 'youtube_auth':
            await youtube.youtube_auth(request(), user=user)
        elif action == 'alias':
            await api.auto_generate_alias(request(), user=user)
        else:
            await api.auto_generate_project(request(), user=user)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_toggle_automation(monkeypatch):
    settings = SimpleNamespace(update_one=AsyncMock())
    import app.db
    monkeypatch.setattr(app.db, 'get_db', lambda: SimpleNamespace(forge_settings=settings))
    stopped = []
    monkeypatch.setattr(forge_loop, 'stop_scheduler', lambda: stopped.append(True))
    result = await forge_loop.forge_toggle(False, user={'id': 'operator', 'role': 'admin'})
    assert result == {'auto_post_enabled': False}
    assert stopped == [True]


@pytest.mark.asyncio
async def test_legacy_callback_rejects_missing_state_before_exchange(monkeypatch):
    monkeypatch.setenv('YOUTUBE_CLIENT_ID', 'client')
    monkeypatch.setenv('YOUTUBE_CLIENT_SECRET', 'secret')
    with pytest.raises(HTTPException) as error:
        await youtube.youtube_callback(request(), code='attacker-code', user={'id': 'operator', 'role': 'admin'})
    assert error.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['status', 'cancel'])
async def test_external_key_cannot_access_customer_job(monkeypatch, action):
    monkeypatch.setenv('EXTERNAL_RENDER_ENABLED', 'true')
    monkeypatch.setenv('EXTERNAL_RENDER_API_KEY', 'test-key')
    db = SimpleNamespace(
        users=SimpleNamespace(find_one=AsyncMock(return_value={'id': 'system', 'is_external_system': True})),
        render_jobs=SimpleNamespace(find_one=AsyncMock(return_value={'id': 'job-other', 'project_id': 'other', 'status': 'completed'})),
        projects=SimpleNamespace(find_one=AsyncMock(return_value={'id': 'other', 'user_id': 'customer'})),
    )
    monkeypatch.setattr(external_api, 'get_db', lambda: db)
    with pytest.raises(HTTPException) as error:
        if action == 'status':
            await external_api.external_render_status('job-other', 'test-key')
        else:
            await external_api.external_render_cancel(external_api.ExternalCancelRequest(job_id='job-other'), 'test-key')
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_external_key_reads_own_job(monkeypatch):
    monkeypatch.setenv('EXTERNAL_RENDER_ENABLED', 'true')
    monkeypatch.setenv('EXTERNAL_RENDER_API_KEY', 'test-key')
    db = SimpleNamespace(
        users=SimpleNamespace(find_one=AsyncMock(return_value={'id': 'system', 'is_external_system': True})),
        render_jobs=SimpleNamespace(find_one=AsyncMock(return_value={'id': 'job-system', 'project_id': 'p', 'status': 'completed', 'output_url': 'https://cdn.example/video.mp4'})),
        projects=SimpleNamespace(find_one=AsyncMock(return_value={'id': 'p', 'user_id': 'system', 'external_source': 'external'})),
    )
    monkeypatch.setattr(external_api, 'get_db', lambda: db)
    result = await external_api.external_render_status('job-system', 'test-key')
    assert result.video_url == 'https://cdn.example/video.mp4'


@pytest.mark.asyncio
async def test_adapter_cannot_publish_customer_project(monkeypatch):
    import app.db
    from app.adapters.posters.youtube import YoutubePoster
    monkeypatch.setattr(app.db, 'get_db', lambda: SimpleNamespace(users=SimpleNamespace(find_one=AsyncMock(return_value={'role': 'creator'}))))
    with pytest.raises(HTTPException) as error:
        await YoutubePoster().post({'id': 'p', 'user_id': 'customer'}, '', {}, 'operator')
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_callback_rejects_unknown_or_replayed_state(monkeypatch):
    states = SimpleNamespace(find_one_and_delete=AsyncMock(return_value=None))
    monkeypatch.setattr(youtube, 'get_db', lambda: SimpleNamespace(youtube_oauth_states=states))
    with pytest.raises(HTTPException) as error:
        await youtube.youtube_callback(request(), code='code', state='stolen-or-replayed', user={'id': 'operator', 'role': 'admin'})
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_external_system_email_cannot_be_preclaimed():
    users = SimpleNamespace(find_one=AsyncMock(return_value={'id': 'attacker', 'email': 'external-renderer@facelessforge.io'}))
    with pytest.raises(HTTPException) as error:
        await external_api._system_creator(SimpleNamespace(users=users))
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_callback_accepts_one_time_admin_state(monkeypatch):
    import httpx
    from datetime import datetime, timezone
    pending = {'state': 'one-time', 'user_id': 'operator', 'expires_at': datetime(2099, 1, 1, tzinfo=timezone.utc)}
    class States:
        async def find_one_and_delete(self, query):
            nonlocal pending
            if pending and query['state'] == pending['state'] and query['user_id'] == pending['user_id'] and pending['expires_at'] > query['expires_at']['$gt']:
                result, pending = pending, None
                return result
    tokens = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(youtube, 'get_db', lambda: SimpleNamespace(youtube_oauth_states=States(), youtube_tokens=tokens))
    monkeypatch.setenv('YOUTUBE_CLIENT_ID', 'test-client')
    monkeypatch.setenv('YOUTUBE_CLIENT_SECRET', 'test-secret')
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={'refresh_token': 'private-token', 'access_token': 'access'}))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    result = await youtube.youtube_callback(request(), code='code', state='one-time', user={'id': 'operator', 'role': 'admin'})
    assert result.status_code == 200
    assert b'private-token' not in result.body
    with pytest.raises(HTTPException) as error:
        await youtube.youtube_callback(request(), code='code', state='one-time', user={'id': 'operator', 'role': 'admin'})
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_auth_supersedes_stale_oauth_states(monkeypatch):
    # An abandoned consent must not leave a permanent row behind: requesting a
    # fresh auth URL clears the caller's prior states before inserting one.
    class States:
        def __init__(self):
            self.deleted = []
            self.inserted = []
        async def delete_many(self, query):
            self.deleted.append(query)
        async def insert_one(self, doc):
            self.inserted.append(doc)
    states = States()
    tokens = SimpleNamespace(find_one=AsyncMock(return_value=None))
    monkeypatch.setattr(youtube, 'get_db', lambda: SimpleNamespace(youtube_oauth_states=states, youtube_tokens=tokens))
    monkeypatch.setenv('YOUTUBE_CLIENT_ID', 'test-client')
    result = await youtube.youtube_auth(request(), user={'id': 'operator', 'role': 'admin'})
    assert states.deleted == [{'user_id': 'operator'}]
    assert len(states.inserted) == 1
    assert states.inserted[0]['user_id'] == 'operator'
    assert 'state=' in result['auth_url']
    assert states.inserted[0]['state'] in result['auth_url']


@pytest.mark.asyncio
async def test_auth_tolerates_backend_without_delete_many(monkeypatch):
    # Older driver stubs (or restricted collections) lacking delete_many must not
    # break the happy path; the state is still inserted.
    class States:
        def __init__(self):
            self.inserted = []
        async def insert_one(self, doc):
            self.inserted.append(doc)
    states = States()
    tokens = SimpleNamespace(find_one=AsyncMock(return_value=None))
    monkeypatch.setattr(youtube, 'get_db', lambda: SimpleNamespace(youtube_oauth_states=states, youtube_tokens=tokens))
    monkeypatch.setenv('YOUTUBE_CLIENT_ID', 'test-client')
    result = await youtube.youtube_auth(request(), user={'id': 'operator', 'role': 'admin'})
    assert len(states.inserted) == 1
    assert 'auth_url' in result
