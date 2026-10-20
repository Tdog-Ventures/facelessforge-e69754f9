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


def test_tts_unclosed_brackets_complete_in_bounded_time():
    # The old lazy wildcard retries from every '[' on an unmatched input.
    subprocess.run([sys.executable, '-c',
                    "from app.tts import sanitize_for_tts; s='['*100000; assert sanitize_for_tts(s)==s"],
                   check=True, timeout=5, capture_output=True)


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
async def test_youtube_provider_exception_never_leaks_secret(monkeypatch, caplog):
    import app.db
    from app.adapters.posters import youtube as adapter
    from app.routes import youtube
    monkeypatch.setattr(app.db, 'get_db', lambda: SimpleNamespace(users=SimpleNamespace(find_one=AsyncMock(return_value={'role': 'admin'}))))
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
