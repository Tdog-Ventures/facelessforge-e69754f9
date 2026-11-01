"""Preserve the deployed shared-render admission/resume/packaging contract offline."""
import asyncio
import wave
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app import external_api
from app.external_audio import pad_audio_file
from app.external_preparation import prepare_packaging
from backend.tests.fake_db import FakeCollection, FakeDatabase


def request(**values):
    return external_api.ExternalRenderRequest(
        title="Approved shared content", script="The caller approved this exact narration.",
        target_duration=30, external_asset_id="sample-identity", resume_project_id="draft-0001", **values,
    )


@pytest.mark.asyncio
async def test_external_busy_rejects_before_preparation_or_writes(monkeypatch):
    monkeypatch.setattr(external_api, "_require_external_key", lambda key: None)
    monkeypatch.setattr(external_api, "_external_preparation_slots", asyncio.Semaphore(0))
    preparation = AsyncMock(side_effect=AssertionError("busy requests must not prepare"))
    monkeypatch.setattr(external_api, "_prepare_external_render", preparation)
    with pytest.raises(HTTPException) as caught:
        await asyncio.wait_for(external_api.external_render_video(request(), "offline-key"), 2)
    assert caught.value.status_code == 429
    assert caught.value.headers["Retry-After"] == "15"
    preparation.assert_not_called()


def draft_db():
    return FakeDatabase(projects=FakeCollection("projects", [{
        "id": "draft-0001", "user_id": "owner", "external_source": "external",
        "external_asset_id": "sample-identity", "name": "Approved shared content",
        "status": "DRAFT", "target_duration": 30,
    }]))


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["owner", "identity", "duration", "assets", "jobs", "claim_race"])
async def test_resume_rejects_conflicts_without_replacing_seed_documents(conflict):
    db = draft_db()
    if conflict == "owner":
        db.projects.docs[0]["user_id"] = "another-owner"
    elif conflict == "identity":
        db.projects.docs[0]["external_asset_id"] = "another-sample"
    elif conflict == "duration":
        db.projects.docs[0]["target_duration"] = 31
    elif conflict == "assets":
        db.assets.docs.append({"id": "existing", "project_id": "draft-0001"})
    elif conflict == "jobs":
        db.render_jobs.docs.append({"id": "existing", "project_id": "draft-0001"})
    else:
        db.projects.on_update = lambda q, u: db.projects.docs[0].update(status="PREPARING")
    with pytest.raises(HTTPException) as caught:
        await external_api._find_resumable_external_draft(db, request(), {"id": "owner"})
    assert caught.value.status_code == (404 if conflict == "owner" else 409)
    assert db.scripts.delete_calls == db.scenes.delete_calls == db.metadata_packages.delete_calls == []


@pytest.mark.asyncio
async def test_resume_claim_preserves_approved_project_fields():
    db = draft_db()
    db.projects.docs[0]["approved_marker"] = "unchanged"
    original = await external_api._find_resumable_external_draft(db, request(), {"id": "owner"})
    assert original["status"] == "DRAFT"
    assert db.projects.docs[0]["status"] == "PREPARING"
    assert db.projects.docs[0]["approved_marker"] == "unchanged"


def test_packaging_preserves_approved_content_and_deduplicates_terms():
    script = "The caller approved this exact narration, including its final call to action."
    metadata, briefs = prepare_packaging("Approved title", script, [], ["morning walk", "morning walk", "daylight"])
    assert metadata["description"] == script
    assert metadata["selected_title"] == "Approved title"
    assert metadata["tags"] == ["morning walk", "daylight"]
    assert metadata["provenance"] == briefs[0]["provenance"] == "caller_supplied"


@pytest.mark.asyncio
async def test_external_audio_padding_preserves_speech_and_exact_samples(tmp_path):
    source, padded = tmp_path / "source.wav", tmp_path / "padded.wav"
    original = b"\xe8\x03" * 96_000
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        audio.writeframes(original)
    assert await pad_audio_file(source, padded, 120_000) == 2.0
    with wave.open(str(padded), "rb") as audio:
        assert audio.getnframes() == 120_000
        assert audio.readframes(96_000) == original
        assert audio.readframes(24_000) == b"\x00\x00" * 24_000
    with pytest.raises(ValueError, match="too short"):
        await pad_audio_file(source, tmp_path / "too-long.wav", 240_000)
    assert not (tmp_path / "too-long.wav").exists()
