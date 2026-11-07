"""Mode switches and render submission/release share one fenced project lease.

Blocker 2 contract: a production-mode or commercial-input change can never race a
render into bypassing commercial validation. The tests below force the exact
interleavings deterministically by running the second operation inside the first
one's critical section (through the same awaited hooks the real code uses).

Each interleaving test is written to fail on the previous implementation, where
the active-render check and the write were separate, unserialized steps.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app import project_lease
from backend.app import render as render_service
from backend.app.models import ProjectUpdate
from backend.app.routes import api as api_routes
from backend.tests.fake_db import FakeCollection, FakeDatabase

PID = "p1"
USER = {"id": "u1", "role": "user", "email": "u1@example.com"}


def _commercial_project(**over):
    doc = {
        "id": PID, "user_id": "u1", "production_mode": "commercial_ad", "target_duration": 30,
        "client_name": "Effective Wash",
        "structured_brief": {"service": "pressure washing", "offer": "free quote",
                             "audience": "homeowners", "location": "Adelaide"},
        "verified_cta": {"text": "Call for a free quote", "destination": "tel:+61812345678", "verified": True},
        "brand_assets_supplied": True,
        "subscription_ref": None,
    }
    doc.update(over)
    return doc


def _editorial_project():
    return {"id": PID, "user_id": "u1", "production_mode": "editorial", "target_duration": 30}


def _db(project, *, credits=50):
    return FakeDatabase(
        projects=FakeCollection("projects", [project]),
        render_jobs=FakeCollection("render_jobs"),
        subscriptions=FakeCollection("subscriptions", [{"user_id": "u1", "credits_remaining": credits}]),
        ff_credits=FakeCollection("ff_credits"),
        credit_transactions=FakeCollection("credit_transactions"),
    )


@pytest.fixture
def render_env(monkeypatch):
    """Route both the API and the render module at one fake database; no real worker."""
    holder = {}

    def install(project, credits=50):
        db = _db(project, credits=credits)
        holder["db"] = db
        monkeypatch.setattr(api_routes, "get_db", lambda: db)
        monkeypatch.setattr(render_service, "get_db", lambda: db)

        async def no_worker(_job_id, _project_id):
            return None

        monkeypatch.setattr(render_service, "_run_render_safe", no_worker)
        monkeypatch.setattr(api_routes, "_attach_project_view", _passthrough_view)
        return db

    return install


async def _passthrough_view(_db, project):
    return project


def _active_jobs(db):
    return [j for j in db.render_jobs.docs if render_service.is_active_job_status(j.get("status"))]


def _mode_of(db):
    return db.projects.docs[0]["production_mode"]


# ------------------------------------------------------------------ interleavings

def test_mode_switch_inside_render_submission_is_refused_and_render_keeps_its_mode(monkeypatch, render_env):
    """Race A: a PATCH to editorial lands immediately before the submitted job is inserted."""
    db = render_env(_commercial_project())
    outcomes = {}
    real_insert = db.render_jobs.insert_one
    injected = False

    async def insert_after_patch_mode(doc):
        nonlocal injected
        if not injected:
            injected = True
            try:
                await api_routes.update_project(PID, ProjectUpdate(production_mode="editorial"), USER)
                outcomes["patch"] = "applied"
            except HTTPException as exc:
                outcomes["patch"] = exc.status_code
        return await real_insert(doc)

    monkeypatch.setattr(db.render_jobs, "insert_one", insert_after_patch_mode)

    job = asyncio.run(asyncio.wait_for(render_service.queue_render(PID, requested_by="u1"), timeout=2))

    assert outcomes["patch"] == 409, outcomes
    assert _mode_of(db) == "commercial_ad"
    stored = db.render_jobs.docs[0]
    assert stored["id"] == job["id"]
    assert stored["render_mode"] == "commercial_ad"
    assert db.projects.docs[0]["render_lease_holder"] is None


def test_render_submission_inside_mode_switch_is_refused(monkeypatch, render_env):
    """Race B: a render is submitted after the PATCH's final active check but before its write."""
    db = render_env(_commercial_project(production_mode="editorial", client_name=None,
                                        structured_brief=None, verified_cta=None, brand_assets_supplied=False))
    db.projects.docs[0]["production_mode"] = "editorial"
    seen = {}
    real_update = db.projects.update_one
    injected = False

    async def update_after_submit_render(query, update, **kwargs):
        nonlocal injected
        if "production_mode" in update.get("$set", {}) and not injected:
            injected = True
            try:
                seen["render"] = await render_service.queue_render(PID, requested_by="u1")
            except render_service.RenderSubmissionBusy as exc:
                seen["render"] = "busy"
                seen["detail"] = str(exc)
        return await real_update(query, update, **kwargs)

    monkeypatch.setattr(db.projects, "update_one", update_after_submit_render)

    # Switch to commercial with valid ground truth so the PATCH itself passes validation.
    asyncio.run(asyncio.wait_for(api_routes.update_project(PID, ProjectUpdate(
        production_mode="commercial_ad", client_name="Effective Wash",
        structured_brief={"service": "pressure washing", "offer": "free quote",
                          "audience": "homeowners", "location": "Adelaide"},
        verified_cta={"text": "Call", "destination": "https://example.com", "verified": True},
        brand_assets_supplied=True,
    ), USER), timeout=2))

    assert seen.get("render") == "busy", seen
    assert _active_jobs(db) == [], "no job may exist for a mode that is changing underneath it"
    assert _mode_of(db) == "commercial_ad"


def test_stale_holder_cannot_commit_a_mode_change(monkeypatch, render_env):
    """Fencing: the lease expires mid-update and is taken over; the stale update must not commit."""
    db = render_env(_editorial_project())
    # The commercial switch needs ground truth; the PATCH is otherwise valid.
    db.projects.docs[0].update({
        "production_mode": "editorial",
    })
    real_update = db.projects.update_one
    stolen = {}

    async def expire_and_steal_before_write(query, update, upsert=False, **kw):
        set_fields = (update or {}).get("$set", {})
        if "production_mode" in set_fields and "lease" not in stolen:
            stolen["lease"] = True
            db.projects.docs[0]["render_lease_expires"] = project_lease._now().replace(year=2001)
            stolen["new_holder"] = await project_lease.acquire(
                db, PID, project_lease.RENDER_LEASE, ttl_seconds=60,
            )
        return await real_update(query, update, upsert=upsert, **kw)

    db.projects.update_one = expire_and_steal_before_write

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_routes.update_project(PID, ProjectUpdate(
            production_mode="commercial_ad", client_name="Effective Wash",
            structured_brief={"service": "pressure washing", "offer": "free quote",
                              "audience": "homeowners", "location": "Adelaide"},
            verified_cta={"text": "Call", "destination": "https://example.com", "verified": True},
            brand_assets_supplied=True,
        ), USER))

    assert exc.value.status_code == 409
    assert _mode_of(db) == "editorial", "the stale writer must not commit"
    new_holder = stolen["new_holder"]
    assert new_holder is not None
    assert db.projects.docs[0]["render_lease_holder"] == new_holder.token, \
        "a stale writer must not release the new holder's lease"


def test_queue_refuses_when_expected_inputs_changed_before_the_lease(render_env):
    db = render_env(_commercial_project())
    snapshot = project_lease.render_input_snapshot(_editorial_project())  # validated against an older state

    with pytest.raises(render_service.RenderSubmissionBusy):
        asyncio.run(render_service.queue_render(PID, requested_by="u1", expected_inputs=snapshot))

    assert db.render_jobs.docs == []
    assert db.subscriptions.docs[0]["credits_remaining"] == 50, "a refused submission charges nothing"


def test_refused_submission_charges_nothing_and_releases_lease(render_env):
    db = render_env(_commercial_project())
    db.subscriptions.docs[0]["credits_remaining"] = 0

    with pytest.raises(HTTPException) as exc:
        asyncio.run(render_service.queue_render(PID, requested_by="u1"))

    assert exc.value.status_code == 402
    assert db.projects.docs[0]["render_lease_holder"] is None
    assert db.render_jobs.docs == []


# ------------------------------------------------------------------ release consistency

def test_submitted_job_records_the_validated_mode(render_env):
    db = render_env(_commercial_project())
    job = asyncio.run(render_service.queue_render(PID, requested_by="u1"))
    assert job["render_mode"] == "commercial_ad"
    assert db.render_jobs.docs[0]["render_mode"] == "commercial_ad"
    assert db.subscriptions.docs[0]["credits_remaining"] == 47


def test_release_skips_auto_post_when_mode_changed_after_validation(monkeypatch, render_env):
    db = render_env(_commercial_project(production_mode="editorial", auto_post=True,
                                        client_name=None, structured_brief=None, verified_cta=None,
                                        brand_assets_supplied=False))
    db.projects.docs[0]["production_mode"] = "commercial_ad"  # switched after this render was validated
    posted = []

    async def record_post(*_args, **_kwargs):
        posted.append(True)

    from backend.app.workers import autopost
    monkeypatch.setattr(autopost, "autopost_after_render", record_post, raising=False)
    saved = SimpleNamespace(url="https://cdn.example/out.mp4", file_path=None)

    asyncio.run(render_service._release_side_effects(
        db, PID, "job-1", release_eligible=True, commercial=False, validated_mode="editorial",
        duration=12.0, saved=saved, final=None,
    ))

    assert posted == [], "an editorial render must never auto-post after the project became commercial"


def test_run_render_refuses_to_start_when_mode_differs_from_submission(render_env):
    db = render_env(_commercial_project(production_mode="commercial_ad"))
    db.render_jobs.docs.append({
        "id": "job-9", "project_id": PID, "status": "queued", "render_mode": "editorial",
        "credit_charge": {"amount": 0, "store": "none"},
    })

    with pytest.raises(RuntimeError, match="production mode changed"):
        asyncio.run(render_service._run_render("job-9", PID))


# ------------------------------------------------------------------ lease primitive

def test_lease_is_free_again_after_release(render_env):
    db = render_env(_editorial_project())

    async def scenario():
        first = await project_lease.acquire(db, PID, project_lease.RENDER_LEASE, ttl_seconds=60)
        assert first is not None
        assert await project_lease.acquire(db, PID, project_lease.RENDER_LEASE, ttl_seconds=60) is None
        await project_lease.release(db, first)
        second = await project_lease.acquire(db, PID, project_lease.RENDER_LEASE, ttl_seconds=60)
        assert second is not None and second.fence == first.fence + 1

    asyncio.run(scenario())


def test_strip_lease_fields_hides_internal_state():
    raw = {"id": "p1", "production_mode": "editorial", "render_lease_holder": "t", "render_lease_fence": 3,
           "asset_lease_expires": None}
    assert project_lease.strip_lease_fields(raw) == {"id": "p1", "production_mode": "editorial"}
