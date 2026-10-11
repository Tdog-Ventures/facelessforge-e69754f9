"""Observability + fail-closed tests for the Forge automation scheduler.

Covers:
  * GET /api/forge/status shape while the scheduler is intentionally OFF
  * no duplicate job registration on repeated start_scheduler() calls
  * POST /api/forge/run-now refusing (409) to publish while auto-post is OFF
"""
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.workers import forge_loop


class FakeForgeRuns:
    async def find_one(self, *_args, **_kwargs):
        return None

    def find(self, *_args, **_kwargs):
        return self

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, _limit):
        return []


class FakeForgeSettings:
    def __init__(self, auto_post_enabled):
        self._value = auto_post_enabled

    async def find_one(self, *_args, **_kwargs):
        if self._value is None:
            return None
        return {"id": "default", "auto_post_enabled": self._value}


class FakeDB:
    def __init__(self, auto_post_enabled=False):
        self.forge_runs = FakeForgeRuns()
        self.forge_settings = FakeForgeSettings(auto_post_enabled)


class FakeJob:
    next_run_time = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)


class FakeScheduler:
    def get_job(self, job_id):
        return FakeJob() if job_id == "forge_daily_0900" else None

    def get_jobs(self):
        return [FakeJob()]


@pytest.mark.asyncio
async def test_forge_status_shape_when_scheduler_is_off(monkeypatch):
    monkeypatch.setattr(forge_loop, "get_db", lambda: FakeDB(auto_post_enabled=False))
    monkeypatch.setattr(forge_loop, "get_scheduler", lambda: None)

    status = await forge_loop.forge_status(user={"id": "user-1"})

    assert status["scheduler_exists"] is False
    assert status["job_registered"] is False
    assert status["next_run_time"] is None
    assert status["timezone"] == "Australia/Adelaide"
    assert status["auto_post_enabled"] is False
    assert status["scheduler_active"] is False
    assert "last_run" in status
    assert "last_run_summary" in status


@pytest.mark.asyncio
async def test_forge_status_reports_registered_job(monkeypatch):
    monkeypatch.setattr(forge_loop, "get_db", lambda: FakeDB(auto_post_enabled=True))
    monkeypatch.setattr(forge_loop, "get_scheduler", lambda: FakeScheduler())

    status = await forge_loop.forge_status(user={"id": "user-1"})

    assert status["scheduler_exists"] is True
    assert status["job_registered"] is True
    assert status["next_run_time"] == FakeJob.next_run_time.isoformat()
    assert status["auto_post_enabled"] is True


@pytest.mark.asyncio
async def test_repeated_start_scheduler_registers_single_job(monkeypatch):
    monkeypatch.setenv("AUTO_POST_ENABLED", "true")
    forge_loop.stop_scheduler()
    forge_loop._scheduler = None
    try:
        first = forge_loop.start_scheduler()
        if first is None:
            pytest.skip("APScheduler AsyncIOScheduler unavailable")
        second = forge_loop.start_scheduler()

        assert first is second, "repeated start must reuse the running scheduler"
        assert len(first.get_jobs()) == 1, "exactly one forge job must be registered"
        assert first.get_job("forge_daily_0900") is not None
    finally:
        forge_loop.stop_scheduler()
        forge_loop._scheduler = None


@pytest.mark.asyncio
async def test_run_now_is_blocked_from_publishing_while_off(monkeypatch):
    monkeypatch.setattr(forge_loop, "get_db", lambda: FakeDB(auto_post_enabled=False))

    def _fail_upload(*_args, **_kwargs):
        pytest.fail("YouTube upload must not be attempted while automation is OFF")

    monkeypatch.setattr(forge_loop, "_youtube_upload", _fail_upload)

    with pytest.raises(HTTPException) as excinfo:
        await forge_loop.forge_run_now(user={"id": "user-1"}, topic=None, niche=None)

    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_missing_auto_post_setting_defaults_off(monkeypatch):
    monkeypatch.setattr(forge_loop, "get_db", lambda: FakeDB(auto_post_enabled=None))

    assert await forge_loop.is_auto_post_enabled() is False
