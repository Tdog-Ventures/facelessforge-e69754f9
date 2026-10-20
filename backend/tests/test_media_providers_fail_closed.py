from pathlib import Path

import pytest
from fastapi import HTTPException

from app import thumbnail_images, tts
from app.routes import youtube


def test_tts_provider_info_reports_the_real_live_provider(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "configured")
    monkeypatch.setenv("USE_MOCK_TTS", "false")

    info = tts.provider_info()

    assert info["provider"] == "elevenlabs"
    assert info["model"] == "eleven_multilingual_v2"
    assert info["configured"] is True


def test_thumbnail_provider_info_reports_missing_configuration(monkeypatch):
    monkeypatch.delenv("EMERGENT_LLM_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.setenv("USE_MOCK_THUMBNAIL_IMAGES", "false")

    info = thumbnail_images.provider_info()

    assert info["provider"] == "gemini_nano_banana"
    assert info["configured"] is False


def test_thumbnail_provider_info_uses_real_stock_fallback(monkeypatch):
    monkeypatch.delenv("EMERGENT_LLM_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("PEXELS_API_KEY", "configured")
    monkeypatch.setenv("USE_MOCK_THUMBNAIL_IMAGES", "false")

    info = thumbnail_images.provider_info()

    assert info["provider"] == "pexels_stock"
    assert info["model"] == "photo-search+local-compositor"
    assert info["configured"] is True


@pytest.mark.asyncio
async def test_tts_missing_key_does_not_create_mock_audio(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("USE_MOCK_TTS", "false")
    monkeypatch.setattr(
        tts,
        "_mock_voiceover",
        lambda *_args, **_kwargs: pytest.fail("mock voiceover must not be created"),
    )

    with pytest.raises(RuntimeError, match="ElevenLabs.*not configured"):
        await tts.generate_voiceover("Production narration", project_id="project-1")


@pytest.mark.asyncio
async def test_tts_provider_error_does_not_fall_back_to_mock(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "configured-but-invalid")
    monkeypatch.setenv("USE_MOCK_TTS", "false")

    async def provider_failure(*_args, **_kwargs):
        raise RuntimeError("provider rejected request")

    monkeypatch.setattr(tts, "_elevenlabs_tts", provider_failure)
    monkeypatch.setattr(
        tts,
        "_mock_voiceover",
        lambda *_args, **_kwargs: pytest.fail("mock voiceover must not be created"),
    )

    with pytest.raises(RuntimeError, match="ElevenLabs voiceover generation failed"):
        await tts.generate_voiceover("Production narration", project_id="project-1")


@pytest.mark.asyncio
async def test_thumbnail_provider_error_does_not_create_mock_svg(monkeypatch):
    monkeypatch.delenv("EMERGENT_LLM_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.setenv("USE_MOCK_THUMBNAIL_IMAGES", "false")
    monkeypatch.setattr(
        thumbnail_images,
        "_mock_svg",
        lambda *_args, **_kwargs: pytest.fail("mock thumbnail must not be created"),
    )

    result = await thumbnail_images.generate_thumbnail_images(
        {"topic": "Test"},
        {"thumbnail_title_text": "Test"},
        variants=1,
        project_id="project-1",
    )

    assert result == []


@pytest.mark.asyncio
async def test_youtube_upload_without_render_never_mints_mock_success(monkeypatch):
    class FakeCollection:
        def __init__(self, document):
            self.document = document

        async def find_one(self, *_args, **_kwargs):
            return self.document

    class FakeDB:
        projects = FakeCollection({"id": "project-1", "user_id": "user-1", "name": "Test"})
        metadata_packages = FakeCollection({})

    monkeypatch.setattr(youtube, "get_db", lambda: FakeDB())
    monkeypatch.setattr(youtube, "_resolve_video_path", lambda _project_id: None)

    with pytest.raises(HTTPException) as exc:
        await youtube.youtube_upload(
            youtube.YoutubeUploadRequest(project_id="project-1"),
            user={"id": "user-1", "role": "admin"},
        )

    assert exc.value.status_code == 409
    assert "completed MP4" in str(exc.value.detail)


def test_pytest_asyncio_is_pinned_for_fresh_builds():
    requirements = Path(__file__).parents[1] / "requirements.txt"
    assert "pytest-asyncio==1.4.0" in requirements.read_text(encoding="utf-8")
