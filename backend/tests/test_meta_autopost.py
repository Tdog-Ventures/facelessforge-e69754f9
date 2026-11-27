"""Tests for the real Meta autopost adapters (Facebook Reels + Instagram Reels).

No real network, no real tokens: all Graph API traffic goes through
httpx.MockTransport with fake env values. Safe under conftest's
paid-API-call guard.
"""
from __future__ import annotations

import asyncio
import urllib.parse

import httpx
import pytest

from app.adapters.posters.base import PLATFORM_LIMITS, PlatformPoster
from app.adapters.posters.facebook import FacebookPoster
from app.adapters.posters.instagram import InstagramPoster

PROJECT = {"id": "proj-test", "name": "Test video title", "auto_post": True}
META = {"selected_title": "Test video title", "description": "Test caption", "duration": 15}
VIDEO_URL = "https://example.com/video.mp4"
TENANT = "tenant-test"

FB_TOKEN = "EAATestFacebookPageToken"
FB_PAGE_ID = "1084931948040856"
IG_TOKEN = "EAATestInstagramToken"
IG_ACCOUNT_ID = "17841450015692981"


def _run(coro):
    return asyncio.run(coro)


def _set_fb_env(monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", FB_TOKEN)
    monkeypatch.setenv("FACEBOOK_PAGE_ID", FB_PAGE_ID)


def _set_ig_env(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", IG_TOKEN)
    monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", IG_ACCOUNT_ID)


def _client_factory(monkeypatch, transport):
    orig = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: orig(transport=transport, *a, **k)
    )


# --- configuration -----------------------------------------------------------

def test_facebook_not_configured_when_env_empty(monkeypatch):
    monkeypatch.delenv("FACEBOOK_PAGE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    poster = FacebookPoster()
    assert poster.is_configured() is False
    res = _run(poster.post(PROJECT, VIDEO_URL, META, TENANT))
    assert res["platform"] == "facebook"
    assert res["status"] == "not_configured"
    assert "mock" not in res["status"]


def test_facebook_is_configured_requires_both(monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", FB_TOKEN)
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    assert FacebookPoster().is_configured() is False
    _set_fb_env(monkeypatch)
    assert FacebookPoster().is_configured() is True


def test_instagram_not_configured_when_env_empty(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", raising=False)
    poster = InstagramPoster()
    assert poster.is_configured() is False
    res = _run(poster.post(PROJECT, VIDEO_URL, META, TENANT))
    assert res["platform"] == "instagram"
    assert res["status"] == "not_configured"
    assert "mock" not in res["status"]


def test_instagram_is_configured_requires_both(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", IG_TOKEN)
    monkeypatch.delenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", raising=False)
    assert InstagramPoster().is_configured() is False
    _set_ig_env(monkeypatch)
    assert InstagramPoster().is_configured() is True


def test_base_tokens_and_limits(monkeypatch):
    _set_fb_env(monkeypatch)
    _set_ig_env(monkeypatch)
    poster = PlatformPoster()
    assert poster._load_tokens()["facebook_token"] == FB_TOKEN
    assert poster._load_tokens()["facebook_page_id"] == FB_PAGE_ID
    assert poster._load_tokens()["instagram_account_id"] == IG_ACCOUNT_ID
    assert PLATFORM_LIMITS["facebook"]["max_duration"] == 90
    assert PLATFORM_LIMITS["facebook"]["max_size_mb"] == 650


# --- Facebook Reels flow (mocked Graph API) ---------------------------------

def _fb_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if request.method == "GET" and url == VIDEO_URL:
        return httpx.Response(200, content=b"fake-mp4-bytes")
    if url.endswith(f"/{FB_PAGE_ID}/video_reels"):
        body = urllib.parse.parse_qs(request.read().decode())
        phase = body.get("upload_phase", [""])[0]
        if phase == "start":
            return httpx.Response(200, json={
                "video_id": "555555555",
                "upload_url": "https://rupload.facebook.com/video-upload/v26.0/555555555",
            })
        if phase == "finish":
            assert body.get("video_state", [""])[0] == "PUBLISHED"
            assert body.get("video_id", [""])[0] == "555555555"
            return httpx.Response(200, json={"success": True})
    if url.startswith("https://rupload.facebook.com/"):
        assert request.headers.get("Authorization") == f"OAuth {FB_TOKEN}"
        assert request.headers.get("offset") == "0"
        assert request.headers.get("file_size") == str(len(b"fake-mp4-bytes"))
        return httpx.Response(200, json={"success": True})
    if url.startswith("https://graph.facebook.com/v26.0/555555555"):
        return httpx.Response(200, json={"status": {"video_status": "upload_complete"}})
    return httpx.Response(404, json={"error": {"message": f"unexpected {request.method} {url}"}})


def test_facebook_reel_flow(monkeypatch):
    _set_fb_env(monkeypatch)
    _client_factory(monkeypatch, httpx.MockTransport(_fb_handler))
    poster = FacebookPoster()
    res = _run(poster.post(PROJECT, VIDEO_URL, META, TENANT))
    assert res["status"] == "uploaded"
    assert res["video_id"] == "555555555"
    assert res["url"] == "https://www.facebook.com/reel/555555555"


# --- Instagram Reels flow (mocked Graph API) ---------------------------------

def _ig_handler_factory():
    calls = {"status": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(f"/{IG_ACCOUNT_ID}/media"):
            return httpx.Response(200, json={"id": "container-1"})
        if "/container-1" in url:
            calls["status"] += 1
            # First poll still processing, second poll finished
            code = "IN_PROGRESS" if calls["status"] == 1 else "FINISHED"
            return httpx.Response(200, json={"status_code": code})
        if url.endswith(f"/{IG_ACCOUNT_ID}/media_publish"):
            body = urllib.parse.parse_qs(request.read().decode())
            assert body.get("creation_id", [""])[0] == "container-1"
            return httpx.Response(200, json={"id": "18000000000000001"})
        if "/18000000000000001" in url and "permalink" in url:
            return httpx.Response(
                200, json={"permalink": "https://www.instagram.com/reel/ABC123/"}
            )
        return httpx.Response(404, json={"error": {"message": f"unexpected {request.method} {url}"}})

    return handler


def test_instagram_reel_flow(monkeypatch):
    import app.adapters.posters.instagram as ig_mod

    _set_ig_env(monkeypatch)
    monkeypatch.setattr(ig_mod, "_POLL_INTERVAL", 0.01)
    _client_factory(monkeypatch, httpx.MockTransport(_ig_handler_factory()))
    poster = InstagramPoster()
    res = _run(poster.post(PROJECT, VIDEO_URL, META, TENANT))
    assert res["status"] == "uploaded"
    assert res["video_id"] == "18000000000000001"
    assert res["url"] == "https://www.instagram.com/reel/ABC123/"


# --- wiring ------------------------------------------------------------------

def test_autopost_registers_facebook():
    from app.workers.autopost import ADAPTERS, SUPPORTED_PLATFORMS

    assert "facebook" in ADAPTERS
    assert isinstance(ADAPTERS["facebook"], FacebookPoster)
    assert "facebook" in SUPPORTED_PLATFORMS
