"""Project creation must honour the commercial/editorial choice.

Regression: the frontend exposed only commercial fields, while the API
silently dropped production_mode and all ground-truth fields. Every project
therefore fell back to editorial rendering, and the client-ad path was
ungrounded.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.models import ProjectCreate, ProjectUpdate, StructuredBrief, VerifiedCTA
from backend.app import project_lease
from backend.app.routes import api as api_routes
from backend.tests.fake_db import FakeCollection, FakeDatabase


class _Projects:
    def __init__(self, existing=None):
        self.existing = existing
        self.inserted = []
        self.last_update = None

    async def insert_one(self, doc):
        self.inserted.append(doc)

    async def find_one(self, *_args, **_kwargs):
        return dict(self.existing) if self.existing is not None else None

    async def update_one(self, _query, update):
        self.last_update = update
        if self.existing is not None:
            self.existing.update(update.get("$set", {}))


class _DB:
    def __init__(self, existing=None):
        self.projects = _Projects(existing)


class _RenderJobs:
    def __init__(self, active=None):
        self.active = active

    async def find_one(self, *_args, **_kwargs):
        return dict(self.active) if self.active else None


def _patch_active_render(monkeypatch, job):
    from backend.app import render as render_service

    monkeypatch.setattr(
        render_service, "get_db",
        lambda: SimpleNamespace(render_jobs=_RenderJobs(job)),
    )


def _stub_attach(monkeypatch):
    async def _attach(_db, project):
        return project

    monkeypatch.setattr(api_routes, "_attach_project_view", _attach)


def _body(**overrides):
    data = {
        "name": "Launch video",
        "niche": "design",
        "topic": "How dark mode changes reading",
        "audience": "designers",
        "tone": "calm-authoritative",
        "target_duration": 30,
    }
    data.update(overrides)
    return ProjectCreate(**data)


def test_editorial_project_create_persists_mode_and_skips_commercial_validation(monkeypatch):
    db = _DB()
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    user = {"id": "u1", "role": "user"}

    created = asyncio.run(api_routes.create_project(_body(production_mode="editorial"), user))

    assert created["production_mode"] == "editorial"
    assert db.projects.inserted[0]["production_mode"] == "editorial"
    assert db.projects.inserted[0]["client_name"] is None


def test_commercial_project_create_rejects_missing_ground_truth(monkeypatch):
    db = _DB()
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    user = {"id": "u1", "role": "user"}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_routes.create_project(_body(production_mode="commercial_ad"), user))

    assert exc.value.status_code == 422
    assert "Client business name" in exc.value.detail
    assert db.projects.inserted == []


def test_commercial_project_create_persists_all_ground_truth(monkeypatch):
    db = _DB()
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    user = {"id": "u1", "role": "user"}
    body = _body(
        production_mode="commercial_ad",
        client_name="Effective Wash",
        structured_brief=StructuredBrief(
            service="pressure washing", offer="free quote",
            audience="homeowners", location="Adelaide",
        ),
        verified_cta=VerifiedCTA(
            text="Call for a free quote", destination="tel:+61812345678", verified=True,
        ),
        brand_assets_supplied=True,
        aspect_ratio="9:16",
        resolution="1080x1920",
    )

    created = asyncio.run(api_routes.create_project(body, user))
    stored = db.projects.inserted[0]

    assert created["production_mode"] == "commercial_ad"
    assert stored["client_name"] == "Effective Wash"
    assert stored["structured_brief"]["service"] == "pressure washing"
    assert stored["verified_cta"]["verified"] is True
    assert stored["aspect_ratio"] == "9:16"


def _update_db(project, render_job=None):
    """Fake database for PATCH tests: honours filters and fenced compare-and-set."""
    return FakeDatabase(
        projects=FakeCollection("projects", [project]),
        render_jobs=FakeCollection("render_jobs", [render_job] if render_job else []),
    )


def _patch_db(monkeypatch, db):
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    from backend.app import render as render_service

    monkeypatch.setattr(render_service, "get_db", lambda: db)


def test_project_update_allows_short_duration_and_validates_mode_change(monkeypatch):
    existing = {
        "id": "p1", "user_id": "u1", "production_mode": "editorial",
        "target_duration": 30, "client_name": None,
        "structured_brief": None, "verified_cta": None,
    }
    db = _update_db(existing)
    _patch_db(monkeypatch, db)
    _stub_attach(monkeypatch)
    user = {"id": "u1", "role": "user"}

    updated = asyncio.run(api_routes.update_project(
        "p1", ProjectUpdate(target_duration=5), user,
    ))
    assert updated["target_duration"] == 5
    raw = db.projects.docs[0]
    assert raw["render_lease_holder"] is None, "lease must be released"
    assert "render_lease_fence" in raw
    assert not [k for k in project_lease.strip_lease_fields(raw) if k.startswith("render_lease_")]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_routes.update_project(
            "p1", ProjectUpdate(production_mode="commercial_ad"), user,
        ))
    assert exc.value.status_code == 422
    assert db.projects.docs[0]["production_mode"] == "editorial"


def _commercial_project():
    return {
        "id": "p1", "user_id": "u1", "production_mode": "commercial_ad",
        "client_name": "Effective Wash",
        "structured_brief": {
            "service": "pressure washing", "offer": "free quote",
            "audience": "homeowners", "location": "Adelaide",
        },
        "verified_cta": {
            "text": "Call for a free quote", "destination": "tel:+61812345678",
            "verified": True,
        },
        "brand_assets_supplied": True,
    }


def test_update_commercial_to_editorial_clears_commercial_ground_truth(monkeypatch):
    # Regression for CodeRabbit P1 in PR #16: the $set-only patch kept stale
    # client_name/brief/CTA behind, so the next commercial switch passed
    # validation against dead ground truth.
    _stub_attach(monkeypatch)
    db = _update_db(_commercial_project())
    _patch_db(monkeypatch, db)
    user = {"id": "u1", "role": "user"}

    updated = asyncio.run(api_routes.update_project(
        "p1", ProjectUpdate(production_mode="editorial"), user,
    ))

    assert updated["production_mode"] == "editorial"
    assert updated["client_name"] is None
    assert updated["structured_brief"] is None
    assert updated["verified_cta"] is None
    assert updated["brand_assets_supplied"] is False


def test_update_back_to_commercial_requires_fresh_ground_truth(monkeypatch):
    _stub_attach(monkeypatch)
    db = _update_db(_commercial_project())
    _patch_db(monkeypatch, db)
    user = {"id": "u1", "role": "user"}

    asyncio.run(api_routes.update_project(
        "p1", ProjectUpdate(production_mode="editorial"), user,
    ))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_routes.update_project(
            "p1", ProjectUpdate(production_mode="commercial_ad"), user,
        ))
    assert exc.value.status_code == 422

    refreshed = asyncio.run(api_routes.update_project(
        "p1",
        ProjectUpdate(
            production_mode="commercial_ad",
            client_name="Fresh Client",
            structured_brief=StructuredBrief(
                service="tinting", offer="free quote",
                audience="homeowners", location="Adelaide",
            ),
            verified_cta=VerifiedCTA(
                text="Book now", destination="https://fresh.example.com", verified=True,
            ),
        ),
        user,
    ))
    assert refreshed["client_name"] == "Fresh Client"
    assert refreshed["updated_at"]
    assert db.projects.docs[0]["render_lease_holder"] is None, "lease must be released"


def test_update_mode_change_blocked_while_render_active(monkeypatch):
    # Regression for CodeRabbit P8 in PR #16: the render worker snapshots the
    # production mode at start and gates release/auto-post with it, so a
    # mid-render mode switch must be blocked.
    _stub_attach(monkeypatch)
    db = _update_db(_commercial_project(), render_job={"id": "job-1", "project_id": "p1", "status": "rendering"})
    _patch_db(monkeypatch, db)
    user = {"id": "u1", "role": "user"}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api_routes.update_project(
            "p1", ProjectUpdate(production_mode="editorial"), user,
        ))
    assert exc.value.status_code == 409
    assert db.projects.docs[0]["production_mode"] == "commercial_ad"
    assert db.projects.docs[0]["render_lease_holder"] is None, "a refused update must release the lease"

    # Non-render-input fields stay editable mid-render.
    updated = asyncio.run(api_routes.update_project(
        "p1", ProjectUpdate(auto_post=True), user,
    ))
    assert updated["auto_post"] is True


def test_update_mode_change_allowed_when_no_render_active(monkeypatch):
    _stub_attach(monkeypatch)
    db = _update_db(_commercial_project())
    _patch_db(monkeypatch, db)
    user = {"id": "u1", "role": "user"}

    updated = asyncio.run(api_routes.update_project(
        "p1", ProjectUpdate(production_mode="editorial"), user,
    ))
    assert updated["production_mode"] == "editorial"
