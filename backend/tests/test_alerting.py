"""Failure alerting: disabled-by-default Gotify dispatch, cooldowns,
fail-silent sends, and pollable rule evaluation (Gatus-compatible)."""
import pytest

from app import alerts


class FakeRenderJobs:
    def __init__(self, recent_failed=0):
        self.recent_failed = recent_failed

    async def count_documents(self, *_a, **_k):
        return self.recent_failed


class FakeSettings:
    def __init__(self, enabled=False):
        self.enabled = enabled

    async def find_one(self, *_a, **_k):
        return {"id": "default", "auto_post_enabled": self.enabled}


class FakeRuns:
    def __init__(self, last=None):
        self.last = last

    async def find_one(self, *_a, **_k):
        return self.last


class FakeDB:
    def __init__(self, recent_failed=0, auto_post=False, last_run=None):
        self.render_jobs = FakeRenderJobs(recent_failed)
        self.forge_settings = FakeSettings(auto_post)
        self.forge_runs = FakeRuns(last_run)


@pytest.mark.asyncio
async def test_notify_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)
    alerts._LAST_SENT.clear()
    report = await alerts.notify("k1", "t", "m")
    assert report == {"sent": False, "reason": "disabled", "key": "k1"}


@pytest.mark.asyncio
async def test_notify_unconfigured_without_url(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.delenv("GOTIFY_URL", raising=False)
    monkeypatch.delenv("GOTIFY_TOKEN", raising=False)
    alerts._LAST_SENT.clear()
    report = await alerts.notify("k2", "t", "m")
    assert report["sent"] is False and report["reason"] == "unconfigured"


@pytest.mark.asyncio
async def test_notify_dispatch_success(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.local")
    monkeypatch.setenv("GOTIFY_TOKEN", "token-value")
    alerts._LAST_SENT.clear()
    seen = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode()
        return FakeResp()

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)
    report = await alerts.notify("k3", "Render job failed", "job=abc", {"job_id": "abc"})
    assert report["sent"] is True
    assert "token-value" in seen["url"]  # Gotify token auth travels in query string
    assert "Render job failed" in seen["body"]
    assert "abc" in seen["body"]


@pytest.mark.asyncio
async def test_notify_failure_is_fail_silent_and_cooldowned(monkeypatch):
    import urllib.error
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.local")
    monkeypatch.setenv("GOTIFY_TOKEN", "t")
    alerts._LAST_SENT.clear()

    def boom(_req, timeout=None):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(alerts.urllib.request, "urlopen", boom)
    first = await alerts.notify("k4", "t", "m")
    assert first["sent"] is False and first["reason"] == "dispatch_failed"
    # A later success for the same key within cooldown stays suppressed.
    assert alerts._cooldown_ok("k4") is True  # failure does not consume cooldown
    alerts._mark_sent("k4")
    second = await alerts.notify("k4", "t", "m")
    assert second["reason"] == "cooldown"


@pytest.mark.asyncio
async def test_evaluate_shape_off_state_no_false_scheduler_alarm(monkeypatch):
    monkeypatch.setattr(alerts, "disk_usage_pct", lambda _p="/": 42.0)
    db = FakeDB(recent_failed=0, auto_post=False, last_run={"status": "failed"})
    report = await alerts.evaluate(db)
    assert set(report) >= {"ok", "failing", "rules", "alerts_enabled", "checked_at"}
    sched = next(r for r in report["rules"] if r["rule"] == "scheduler")
    assert sched["ok"] is True and "OFF" in sched["detail"]
    assert report["ok"] is True and report["failing"] == []


@pytest.mark.asyncio
async def test_evaluate_flags_recent_failures_and_enabled_scheduler(monkeypatch):
    monkeypatch.setattr(alerts, "disk_usage_pct", lambda _p="/": 91.5)
    db = FakeDB(recent_failed=3, auto_post=True,
                last_run={"status": "failed", "created_at": alerts._now()})
    report = await alerts.evaluate(db)
    assert report["ok"] is False
    assert "failed_jobs_recent" in report["failing"]
    assert "scheduler" in report["failing"]
    assert "disk_usage" in report["failing"]
