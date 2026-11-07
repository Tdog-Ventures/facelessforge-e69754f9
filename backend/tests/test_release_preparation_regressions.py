"""Bounded regressions from independent release-preparation review; no providers."""
import asyncio

import pytest
from fastapi import HTTPException
from pymongo.errors import WriteError

from backend.app import project_lease, render
from backend.app.models import StockAttachRequest
from backend.app.routes import api
from backend.tests.fake_db import FakeCollection, FakeDatabase
from backend.tests.test_auto_attach_assets import (
    DISTINCT_TITLES, PID, _attach, _balance, _db, _patch_stock, _rows, _stock, _user,
)


def bounded(coro):
    return asyncio.run(asyncio.wait_for(coro, 2))


def test_acknowledged_validation_rejection_rolls_back_and_can_retry(monkeypatch):
    original = _stock("original", "old", "Original selection")
    db = _db(assets=[original])
    _patch_stock(monkeypatch, db, DISTINCT_TITLES)

    def reject_second(doc):
        if doc.get("external_id") == "ext1":
            raise WriteError("Document failed validation", code=121)

    db.assets.on_insert = reject_second
    result = bounded(_attach(replace=True, key="release-validation-retry"))
    assert result["failed"] == 1 and result["credits_charged"] == 0
    assert _rows(db.assets) == [original] and _balance(db) == 10
    assert not db.projects.docs[0].get("asset_commit_barrier")
    assert not db.projects.docs[0].get("asset_commit_intent")
    db.assets.on_insert = None
    retried = bounded(_attach(replace=True, key="release-validation-retry"))
    assert retried["failed"] == 0 and retried["attached"] == 4 and _balance(db) == 6


def test_manual_scene_zero_match_compensates_own_asset_and_reports_conflict(monkeypatch):
    other = _stock("other", "other", "Other scene", scene_id="scene-2")
    db = _db(assets=[other])
    monkeypatch.setattr(api, "get_db", lambda: db)
    db.assets.on_insert = lambda doc: db.scenes.docs.clear()
    with pytest.raises(HTTPException) as caught:
        bounded(api.attach_scene_asset(PID, "scene-1", StockAttachRequest(
            title="Manual", media_type="stock_video", source="pexels",
            external_id="manual-zero-match", preview_url="offline-preview",
        ), _user()))
    assert caught.value.status_code == 409
    assert _rows(db.assets) == [other] and db.rights_registry.docs == []
    assert _balance(db) == 10 and not db.projects.docs[0].get("asset_commit_barrier")


def timing_db(monkeypatch):
    db = FakeDatabase(
        projects=FakeCollection("projects", [{"id": PID, "user_id": "user-1", "production_mode": "editorial",
                                             "target_duration": 30, "selected_voiceover_asset_id": "voice"}]),
        assets=FakeCollection("assets", [{"id": "voice", "project_id": PID, "asset_type": "voiceover_audio", "duration": 19.0}]),
        scripts=FakeCollection("scripts", [{"project_id": PID, "full_script": "Manual script"}]),
        scenes=FakeCollection("scenes", [{"id": "scene-1", "project_id": PID, "end_time": 19.0}]),
    )
    monkeypatch.setattr(api, "get_db", lambda: db)
    monkeypatch.setattr(render, "get_db", lambda: db)
    return db


def test_autofit_preflight_uses_post_fit_mode(monkeypatch):
    db = timing_db(monkeypatch)
    real_fit = render.autofit_project_duration

    async def switch_before_fit(project_id):
        db.projects.docs[0]["production_mode"] = "commercial_ad"
        return await real_fit(project_id)

    monkeypatch.setattr(render, "autofit_project_duration", switch_before_fit)
    result = bounded(api.render_autofit_duration(PID, _user()))
    assert db.projects.docs[0]["target_duration"] == 19
    assert result["preflight"]["timing"]["intro_seconds"] == 0
    assert result["preflight"]["timing"]["narration_seconds"] == 19
    assert result["preflight"]["timing"]["voiceover_action"] == "ready"


def test_autofit_refuses_other_render_input_holder(monkeypatch):
    db = timing_db(monkeypatch)

    async def scenario():
        lease = await project_lease.acquire(db, PID, project_lease.RENDER_LEASE, ttl_seconds=90)
        with pytest.raises(HTTPException) as caught:
            await api.render_autofit_duration(PID, _user())
        assert caught.value.status_code == 409
        assert await project_lease.still_held(db, lease)
        assert db.projects.docs[0]["target_duration"] == 30 and db.render_jobs.docs == []
    bounded(scenario())


def test_autofit_refuses_active_verification(monkeypatch):
    db = timing_db(monkeypatch)
    db.render_jobs.docs.append({"id": "active", "project_id": PID, "status": "verifying"})
    with pytest.raises(HTTPException) as caught:
        bounded(api.render_autofit_duration(PID, _user()))
    assert caught.value.status_code == 409 and db.projects.docs[0]["target_duration"] == 30
    assert db.projects.docs[0]["render_lease_holder"] is None


def test_stale_autofit_cas_cannot_overwrite_new_holder_inputs(monkeypatch):
    db = timing_db(monkeypatch)
    takeover = []

    async def transfer_before_target_write(query, update):
        if takeover or update.get("$set", {}).get("target_duration") != 22:
            return
        db.projects.docs[0]["render_lease_expires"] = project_lease._now().replace(year=2001)
        lease = await project_lease.acquire(db, PID, project_lease.RENDER_LEASE, ttl_seconds=90)
        takeover.append(lease)
        await db.projects.update_one({"id": PID, **lease.guard()}, {
            "$set": {"target_duration": 19, "production_mode": "commercial_ad"},
        })

    db.projects.on_update = transfer_before_target_write
    with pytest.raises(HTTPException) as caught:
        bounded(api.render_autofit_duration(PID, _user()))
    assert caught.value.status_code == 409
    assert db.projects.docs[0]["target_duration"] == 19
    assert db.projects.docs[0]["production_mode"] == "commercial_ad"
    assert db.projects.docs[0]["render_lease_holder"] == takeover[0].token
