"""Thumbnail provider selection and Pexels fallback must not hit the stock unique index."""
import pytest

from app import thumbnail_images


class DuplicateKeyError(Exception):
    code = 11000


class _Assets:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        if doc.get("external_id") and any(
            existing.get("project_id") == doc.get("project_id")
            and existing.get("scene_id") == doc.get("scene_id")
            and existing.get("external_id") == doc.get("external_id")
            and existing.get("source") == doc.get("source")
            for existing in self.docs
        ):
            raise DuplicateKeyError("E11000 duplicate key")
        self.docs.append(dict(doc))


class _DB:
    def __init__(self):
        self.assets = _Assets()


class _Saved:
    def __init__(self, key):
        self.url = f"/api/static/{key}"
        self.preview_path = self.url
        self.file_path = None
        self.key = key


class _Store:
    mode = "local"

    def save_file(self, _path, key, content_type="image/png"):
        return _Saved(key)


def _silence_providers(monkeypatch):
    for name in (
        "EMERGENT_LLM_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "GOOGLE_GENAI_API_KEY", "PEXELS_API_KEY", "PEXELS_KEY",
        "THUMBNAIL_IMAGE_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("USE_MOCK_THUMBNAIL_IMAGES", "false")


def test_google_api_key_enables_gemini(monkeypatch):
    _silence_providers(monkeypatch)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-test-key")
    info = thumbnail_images.provider_info()
    assert info["configured"] is True
    assert info["gemini_via"] == "google"
    assert info["provider"] == "gemini_nano_banana"
    assert info["model"] == "gemini-nano-banana-2.1"
    assert "google-test-key" not in str(info)


def test_google_genai_key_enables_gemini_when_it_is_the_only_key(monkeypatch):
    _silence_providers(monkeypatch)
    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "genai-test-key")
    info = thumbnail_images.provider_info()
    assert info["gemini_via"] == "google"
    assert info["configured"] is True


def test_search_query_uses_the_concept_not_a_shared_topic():
    first = thumbnail_images.thumbnail_search_query(
        {"niche": "cooking", "topic": "Every way to cook an egg"},
        {"subject_focal_point": "poached egg", "background_idea": "white bowl", "thumbnail_title_text": "Poach it"},
    )
    second = thumbnail_images.thumbnail_search_query(
        {"niche": "cooking", "topic": "Every way to cook an egg"},
        {"subject_focal_point": "fried egg", "background_idea": "cast iron pan", "thumbnail_title_text": "Fry it"},
    )
    assert "poached egg" in first
    assert "fried egg" in second
    assert first != second


def test_choose_unused_photo_skips_ids_already_saved():
    photos = [
        {"id": 36874496, "src": {"large": "https://images.example/mince.jpg"}},
        {"id": 42, "src": {"large": "https://images.example/egg.jpg"}},
    ]
    chosen = thumbnail_images.choose_unused_photo(photos, {"36874496"})
    assert chosen["id"] == 42


def test_generated_thumbnail_does_not_use_the_stock_unique_key():
    doc = thumbnail_images.generated_thumbnail_document(
        asset_id="thumb-1",
        project_id="p1",
        brief={"thumbnail_title_text": "Poach it"},
        variant_number=1,
        source="thumbnail_pexels",
        saved=_Saved("thumbs/p1/thumb-1.png"),
        store=_Store(),
        prompt="prompt",
        info={"model": "photo"},
        stock_external_id="36874496",
    )
    assert "external_id" not in doc
    assert doc["stock_external_id"] == "36874496"
    assert doc["asset_type"] == "generated_thumbnail"


@pytest.mark.asyncio
async def test_duplicate_pexels_photo_does_not_raise(monkeypatch, tmp_path):
    _silence_providers(monkeypatch)
    monkeypatch.setattr(thumbnail_images, "STATIC_ROOT", tmp_path)
    monkeypatch.setattr(thumbnail_images, "get_storage", lambda: _Store())

    async def no_gemini(*_args, **_kwargs):
        return None, "Gemini (google, gemini-nano-banana-2.1) returned no image."

    async def same_then_next(_project, _brief, exclude_ids):
        photo_id = "222" if "111" in exclude_ids else "111"
        return b"png-bytes", {"external_id": photo_id, "source_url": f"https://pexels.example/{photo_id}"}, None

    monkeypatch.setattr(thumbnail_images, "_gemini_image", no_gemini)
    monkeypatch.setattr(thumbnail_images, "_pexels_stock_image", same_then_next)
    monkeypatch.setattr(thumbnail_images, "_compose_stock_thumbnail", lambda *_args, **_kwargs: b"\x89PNG composed")
    project = {"niche": "cooking", "topic": "Eggs"}
    first_brief = {"subject_focal_point": "poached egg", "thumbnail_title_text": "Poach it"}
    second_brief = {"subject_focal_point": "fried egg", "thumbnail_title_text": "Fry it"}

    first = await thumbnail_images.generate_thumbnail_images(
        project, first_brief, variants=1, project_id="p1", exclude_photo_ids=set(),
    )
    used = {first.images[0]["stock_external_id"]}
    second = await thumbnail_images.generate_thumbnail_images(
        project, second_brief, variants=1, project_id="p1", exclude_photo_ids=used,
    )
    assert first.images[0]["stock_external_id"] == "111"
    assert second.images[0]["stock_external_id"] == "222"
    db = _DB()
    saved, message = await thumbnail_images.insert_generated_thumbnails(db, first.images + second.images)
    assert message is None
    assert len(saved) == 2
    assert len(db.assets.docs) == 2


@pytest.mark.asyncio
async def test_provider_failure_is_returned_instead_of_an_empty_success(monkeypatch, tmp_path):
    _silence_providers(monkeypatch)
    monkeypatch.setattr(thumbnail_images, "STATIC_ROOT", tmp_path)

    async def no_gemini(*_args, **_kwargs):
        return None, "Gemini image generation failed: TimeoutError: deadline"

    async def no_pexels(*_args, **_kwargs):
        return None, None, "No unused Pexels photo matched “poached egg”."

    monkeypatch.setattr(thumbnail_images, "_gemini_image", no_gemini)
    monkeypatch.setattr(thumbnail_images, "_pexels_stock_image", no_pexels)
    batch = await thumbnail_images.generate_thumbnail_images(
        {"niche": "cooking"},
        {"subject_focal_point": "poached egg", "thumbnail_title_text": "Poach it"},
        variants=1,
        project_id="p1",
    )
    assert batch.images == []
    assert "Gemini image generation failed" in batch.error
    assert "No unused Pexels photo" in batch.error


@pytest.mark.asyncio
async def test_duplicate_key_on_insert_is_a_message_not_an_exception():
    db = _DB()
    colliding = {
        "id": "a",
        "project_id": "p1",
        "scene_id": None,
        "external_id": "36874496",
        "source": "pexels_stock",
        "asset_type": "generated_thumbnail",
    }
    saved, message = await thumbnail_images.insert_generated_thumbnails(db, [colliding, dict(colliding, id="b")])
    assert len(saved) == 1
    assert message is None
    none_saved, blocked = await thumbnail_images.insert_generated_thumbnails(db, [dict(colliding, id="c")])
    assert none_saved == []
    assert "already saved" in blocked
