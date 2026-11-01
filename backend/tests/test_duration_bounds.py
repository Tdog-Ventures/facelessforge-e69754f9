"""Product duration is 5..3600s end to end, not 30s+ in the timing helpers."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.external_api import ExternalRenderRequest
from app.models import ProjectUpdate
from app.render import suggest_duration_autofit
from app.timing import (
    MIN_AUTOFIT_TARGET_SECONDS,
    TimingContract,
    suggest_fitted_duration,
    target_word_count,
)


def test_five_second_editorial_project_has_two_and_a_half_seconds_of_narration():
    timing = TimingContract.from_project({"target_duration": 5})

    assert timing.intro_seconds == 2.5
    assert timing.narration_seconds == 2.5
    assert timing.final_frames == 150
    assert target_word_count(timing.narration_seconds) == 6


def test_autofit_lower_bound_matches_product_minimum():
    assert MIN_AUTOFIT_TARGET_SECONDS == 5
    assert suggest_fitted_duration(voiceover_seconds=2.4, intro_seconds=2.5) == 5


def test_autofit_suggests_a_short_target_above_the_old_thirty_second_floor():
    suggestion = suggest_duration_autofit(
        {"target_duration": 5},
        {"duration": 7.2},
    )

    assert suggestion == {"from": 5, "to": 10, "voiceover_seconds": 7.2}


def test_external_api_accepts_five_seconds_and_rejects_four():
    request = ExternalRenderRequest(title="Short", script="Ten chars.", target_duration=5)
    assert request.target_duration == 5

    with pytest.raises(ValidationError):
        ExternalRenderRequest(title="Short", script="Ten chars.", target_duration=4)


def test_project_update_accepts_five_seconds_and_rejects_four():
    assert ProjectUpdate(target_duration=5).target_duration == 5

    with pytest.raises(ValidationError):
        ProjectUpdate(target_duration=4)


def test_external_render_short_script_estimates_five_seconds(monkeypatch):
    # Regression for CodeRabbit P6 in PR #18: the 50-word floor turned a
    # short script into a 20s target, then the render-side autofit had to
    # correct it after payment.
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app import external_api

    class FakeCursor:
        def __init__(self, docs):
            self._docs = docs

        def sort(self, *_a, **_k):
            return self

        async def to_list(self, _n):
            return [dict(d) for d in self._docs]

    class FakeCollection:
        def __init__(self):
            self.inserted = []

        async def insert_one(self, doc):
            self.inserted.append(dict(doc))

        async def insert_many(self, docs):
            self.inserted.extend(dict(d) for d in docs)

        async def update_one(self, _query, _update, upsert=False):
            return MagicMock()

        async def find_one(self, query, _projection=None):
            for doc in self.inserted:
                if all(doc.get(k) == v for k, v in query.items()):
                    return dict(doc)
            return None

        def find(self, query=None, _projection=None):
            docs = [
                d for d in self.inserted
                if all(d.get(k) == v for k, v in (query or {}).items())
            ] if query else list(self.inserted)
            return FakeCursor(docs)

    class FakeDB:
        def __init__(self):
            self._cols = {}
            self.projects_records = None

        def __getattr__(self, name):
            return self._cols.setdefault(name, FakeCollection())

    fake_db = FakeDB()
    monkeypatch.setattr(external_api, "_system_creator", AsyncMock(return_value={"id": "sys", "role": "admin"}))
    monkeypatch.setattr(external_api, "get_db", lambda: fake_db)
    monkeypatch.setattr(external_api.gen, "generate_metadata", AsyncMock(return_value={
        "title_options": ["T"], "selected_title": "T", "description": "d",
        "tags": ["x"], "youtube_chapters": [], "social_caption": "T",
    }))
    monkeypatch.setattr(external_api.gen, "generate_thumbnails", AsyncMock(return_value=[]))
    monkeypatch.setattr(external_api.thumb_images, "generate_thumbnail_images", AsyncMock(return_value=[]))
    monkeypatch.setattr(external_api.tts_service, "generate_voiceover", AsyncMock(return_value={"id": "vo-1"}))
    monkeypatch.setattr(external_api.render_service, "validate_prerequisites", lambda *a, **k: {"ok": True, "issues": []})
    monkeypatch.setattr(external_api.render_service, "queue_render", AsyncMock(return_value={"id": "job-1"}))
    monkeypatch.setenv("EXTERNAL_RENDER_ENABLED", "true")
    monkeypatch.setenv("EXTERNAL_RENDER_API_KEY", "test-key")

    body = external_api.ExternalRenderRequest(title="T", script="Short on purpose.")
    asyncio.run(external_api.external_render_video(body, "test-key"))

    project = fake_db.projects.inserted[0]
    assert project["target_duration"] == 5
