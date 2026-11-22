from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app import auth


def request(method="POST", origin=None, referer=None, cookies="access_token=valid", bearer=False):
    headers = [(b"host", b"api.example.test")]
    for name, value in [("origin", origin), ("referer", referer), ("cookie", cookies), ("authorization", "Bearer valid" if bearer else None)]:
        if value is not None:
            headers.append((name.encode(), value.encode()))
    return Request({"type": "http", "method": method, "scheme": "https", "path": "/api/auth/logout", "server": ("api.example.test", 443), "headers": headers})


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["https://evil.test", "null", "https://facelessforge.ethinx.solutions.evil.test", "https://user@facelessforge.ethinx.solutions", "https://facelessforge.ethinx.solutions:444", "https://evil.test:bad", ""])
async def test_cross_site_cookie_mutation_rejected_before_token_or_db(monkeypatch, origin):
    monkeypatch.setattr(auth.jwt, "decode", lambda *a, **k: pytest.fail("CSRF reached token lookup"))
    with pytest.raises(HTTPException) as error:
        await auth.get_current_user(request(origin=origin))
    assert error.value.status_code == 403


@pytest.mark.parametrize("origin", ["https://api.example.test", "https://facelessforge.ethinx.solutions", "https://ethinx.solutions", "https://www.ethinx.solutions"])
def test_trusted_cookie_mutation_allowed(origin):
    auth._check_cookie_origin(request(origin=origin))


def test_configured_frontend_and_same_origin_referer(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://frontend.example.test/path")
    auth._check_cookie_origin(request(origin="https://frontend.example.test"))
    auth._check_cookie_origin(request(referer="https://api.example.test/app"))


def test_cookie_mutation_without_origin_is_not_implicitly_trusted():
    with pytest.raises(HTTPException):
        auth._check_cookie_origin(request())


def test_hostile_origin_cannot_be_overridden_by_good_referer():
    with pytest.raises(HTTPException):
        auth._check_cookie_origin(request(origin="https://evil.test", referer="https://api.example.test/app"))


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_cookie_requests_remain_compatible(method):
    auth._check_cookie_origin(request(method=method, origin="https://evil.test"))


def test_bearer_only_integrations_do_not_use_ambient_cookies():
    auth._check_cookie_origin(request(origin="https://evil.test", cookies=None, bearer=True))


def test_cookie_takes_precedence_over_bearer_and_requires_csrf_check():
    with pytest.raises(HTTPException):
        auth._check_cookie_origin(request(origin="https://evil.test", bearer=True))
