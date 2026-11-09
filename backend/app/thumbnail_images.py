"""Thumbnail image generation — Gemini Nano Banana with deterministic mock fallback.

Public API:
    await generate_thumbnail_images(project, brief, variants=1) -> list[dict]

Each returned dict is a normalised asset payload with preview_url pointing to a file
served by the backend's `/api/static/thumbs/...` mount.

Mock mode produces deterministic branded SVGs so the UI flow stays testable without
API access. Real mode uses emergentintegrations LlmChat with gemini-3.1-flash-image-preview.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import io
import logging
import os
import uuid
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .storage import get_storage

logger = logging.getLogger("facelessforge.thumbnail_images")

STATIC_ROOT = Path(__file__).parent.parent / "static" / "thumbs"
STATIC_ROOT.mkdir(parents=True, exist_ok=True)


def _use_mock() -> bool:
    flag = os.environ.get("USE_MOCK_THUMBNAIL_IMAGES", "false").strip().lower() in ("1", "true", "yes")
    return flag


def is_mock_mode() -> bool:
    return _use_mock()


# Google's current recommended high-efficiency image model (Nano Banana 2.1).
# https://ai.google.dev/gemini-api/docs/image-generation
DEFAULT_IMAGE_MODEL = "gemini-nano-banana-2.1"


def _emergent_key() -> str:
    return os.environ.get("EMERGENT_LLM_KEY", "").strip()


def _google_api_key() -> str:
    for name in ("GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY", "GEMINI_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _pexels_api_key() -> str:
    return os.environ.get("PEXELS_API_KEY", "").strip() or os.environ.get("PEXELS_KEY", "").strip()


def _image_model() -> str:
    return os.environ.get("THUMBNAIL_IMAGE_MODEL", "").strip() or DEFAULT_IMAGE_MODEL


def _public_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for secret in (_emergent_key(), _google_api_key(), _pexels_api_key()):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:240]


def provider_info() -> dict:
    mock = _use_mock()
    gemini_configured = bool(_emergent_key() or _google_api_key())
    pexels_configured = bool(_pexels_api_key())
    if mock:
        provider = "mock"
        model = "deterministic-svg"
    elif gemini_configured:
        provider = os.environ.get("THUMBNAIL_IMAGE_PROVIDER", "gemini_nano_banana")
        model = _image_model()
    elif pexels_configured:
        provider = "pexels_stock"
        model = "photo-search+local-compositor"
    else:
        provider = os.environ.get("THUMBNAIL_IMAGE_PROVIDER", "gemini_nano_banana")
        model = _image_model()
    return {
        "mock": mock,
        "configured": mock or gemini_configured or pexels_configured,
        "provider": provider,
        "model": model,
        "gemini_via": "emergent" if _emergent_key() else ("google" if _google_api_key() else None),
    }


def _prompt_from_brief(project: dict, brief: dict) -> str:
    """Build a high-quality YouTube thumbnail prompt.

    Deliberately asks the model NOT to bake text in, and requests 16:9 composition,
    strong focal point, specific colour direction, and readable-at-thumbnail style.
    """
    title_text = brief.get("thumbnail_title_text", "")
    parts = [
        "Photorealistic YouTube thumbnail, 16:9 aspect ratio, ultra-sharp, high contrast.",
        f"Niche: {project.get('niche', 'general')}.",
        f"Topic: {project.get('topic', '')}.",
        f"Audience: {project.get('audience', 'general viewers')}.",
        f"Tone: {project.get('tone', 'cinematic')}.",
        f"Subject / focal point: {brief.get('subject_focal_point', 'single dominant subject')}.",
        f"Composition: {brief.get('visual_composition', 'rule of thirds, clear focal hierarchy')}.",
        f"Background: {brief.get('background_idea', 'clean, uncluttered, moody')}.",
        f"Emotion angle: {brief.get('emotion_angle', 'curiosity')}.",
        f"Lighting: dramatic directional light, subtle rim, low-noise, cinematic.",
        f"Colour direction: {brief.get('colour_direction', 'deep blacks, electric cyan accent, signal green pop')}.",
        f"Click trigger: {brief.get('click_trigger', 'contradiction / pattern interrupt')}.",
        "Safe, platform-friendly style. No nudity, no gore, no copyrighted logos or trademarks.",
        "CRITICAL: Do NOT bake any text, captions, watermarks, or lettering into the image — text will be added later in post.",
        "Leave clear negative space on the top-right for a future title overlay.",
    ]
    if title_text:
        parts.append(f"(The future title overlay will read: \"{title_text}\" — compose space for it.)")
    image_prompt = str(brief.get("image_prompt") or "").strip()
    if image_prompt:
        parts.append(f"Concept direction: {image_prompt}")
    return "\n".join(parts)


def thumbnail_search_query(project: dict, brief: dict) -> str:
    """Search words for this concept, not one shared project query."""
    inner = brief.get("brief") if isinstance(brief.get("brief"), dict) else brief
    inner = inner or {}
    parts = [
        inner.get("subject_focal_point"),
        inner.get("background_idea"),
        inner.get("thumbnail_title_text"),
        (project or {}).get("niche"),
    ]
    words = " ".join(str(part).strip() for part in parts if part and str(part).strip())
    return (words or str((project or {}).get("topic") or "cinematic"))[:180]


def choose_unused_photo(photos: list, exclude_ids: set[str]) -> dict | None:
    for photo in photos or []:
        photo_id = str((photo or {}).get("id") or "").strip()
        if not photo_id or photo_id in exclude_ids:
            continue
        src = (photo or {}).get("src") or {}
        if src.get("large2x") or src.get("large") or src.get("original"):
            return photo
    return None


# ---------------- Mock SVG generation ----------------

_MOCK_PALETTES = [
    ("#0A0A0A", "#00E5FF", "#00FF66"),
    ("#0A0A0A", "#7B61FF", "#FFB020"),
    ("#111", "#FF3366", "#FFFFFF"),
    ("#0A0A0A", "#00FF66", "#7B61FF"),
    ("#07080A", "#00E5FF", "#FFB020"),
]


def _mock_svg(seed: str, project: dict, brief: dict) -> bytes:
    bg, accent, secondary = _MOCK_PALETTES[int(hashlib.sha1(seed.encode()).hexdigest()[:8], 16) % len(_MOCK_PALETTES)]
    title = (brief.get("thumbnail_title_text") or project.get("niche") or "Forge").upper()[:28]
    topic = (project.get("topic") or "")[:70]
    title = html.escape(title)
    topic = html.escape(topic)
    # Radial-ish composition using SVG
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
  <defs>
    <radialGradient id="g" cx="30%" cy="40%" r="80%">
      <stop offset="0%" stop-color="{accent}" stop-opacity="0.22"/>
      <stop offset="60%" stop-color="{bg}" stop-opacity="1"/>
    </radialGradient>
    <pattern id="grid" width="48" height="48" patternUnits="userSpaceOnUse">
      <path d="M 48 0 L 0 0 0 48" fill="none" stroke="rgba(255,255,255,0.04)" stroke-width="1"/>
    </pattern>
  </defs>
  <rect width="1280" height="720" fill="{bg}"/>
  <rect width="1280" height="720" fill="url(#g)"/>
  <rect width="1280" height="720" fill="url(#grid)"/>
  <circle cx="380" cy="360" r="220" fill="{accent}" opacity="0.25"/>
  <circle cx="380" cy="360" r="140" fill="{secondary}" opacity="0.35"/>
  <circle cx="380" cy="360" r="70" fill="{accent}"/>
  <rect x="820" y="100" width="380" height="8" fill="{secondary}"/>
  <rect x="820" y="130" width="260" height="8" fill="{accent}" opacity="0.6"/>
  <rect x="820" y="160" width="300" height="4" fill="{accent}" opacity="0.3"/>
  <text x="640" y="300" font-family="Inter, sans-serif" font-weight="800" font-size="56" fill="#FFFFFF" letter-spacing="-2" text-anchor="middle" style="display:flex;justify-content:center;align-items:center;text-align:center">{title}</text>
  <text x="640" y="360" font-family="JetBrains Mono, monospace" font-size="18" fill="#A1A1AA" letter-spacing="2" text-anchor="middle">FACELESSFORGE · CONCEPT</text>
  <text x="640" y="560" font-family="Inter, sans-serif" font-size="22" fill="#A1A1AA" text-anchor="middle">{topic}</text>
  <rect x="0" y="700" width="1280" height="20" fill="{accent}" opacity="0.7"/>
</svg>'''
    return svg.encode("utf-8")


# ---------------- Real generation ----------------

def _decode_image_payload(data) -> bytes | None:
    if not data:
        return None
    if isinstance(data, str):
        return base64.b64decode(data)
    return bytes(data)


async def _gemini_via_emergent(prompt: str, session_id: str, api_key: str, model: str) -> bytes | None:
    from emergentintegrations.llm.chat import LlmChat, UserMessage
    chat = LlmChat(
        api_key=api_key,
        session_id=session_id,
        system_message="You are a cinematic YouTube thumbnail designer.",
    )
    chat.with_model("gemini", model).with_params(modalities=["image", "text"])
    _, images = await chat.send_message_multimodal_response(UserMessage(text=prompt))
    if images:
        return _decode_image_payload(images[0].get("data"))
    return None


async def _gemini_via_google(prompt: str, api_key: str, model: str) -> bytes | None:
    """Official google-genai client. A plain Google API key cannot use the Emergent client."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    try:
        interaction = await asyncio.to_thread(
            lambda: client.interactions.create(model=model, input=prompt)
        )
        image = getattr(interaction, "output_image", None)
        payload = _decode_image_payload(getattr(image, "data", None))
        if payload:
            return payload
    except Exception as exc:  # noqa: BLE001
        logger.info("Gemini interactions API missed (%s); trying generate_content", _public_error(exc))

    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]),
    )
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            payload = _decode_image_payload(getattr(inline, "data", None))
            if payload:
                return payload
    return None


async def _gemini_image(prompt: str, session_id: str) -> tuple[bytes | None, str | None]:
    """Return image bytes, or a short reason when Gemini cannot produce one."""
    model = _image_model()
    emergent = _emergent_key()
    google_key = _google_api_key()
    if not emergent and not google_key:
        return None, "Gemini is not configured."
    try:
        if emergent:
            image = await _gemini_via_emergent(prompt, session_id, emergent, model)
            path = "emergent"
        else:
            image = await _gemini_via_google(prompt, google_key, model)
            path = "google"
        if image:
            return image, None
        reason = f"Gemini ({path}, {model}) returned no image."
        logger.warning(reason)
        return None, reason
    except Exception as exc:  # noqa: BLE001
        reason = f"Gemini image generation failed: {_public_error(exc)}"
        logger.warning(reason)
        return None, reason


async def _pexels_stock_image(
    project: dict,
    brief: dict,
    exclude_ids: set[str],
) -> tuple[bytes | None, dict | None, str | None]:
    """Return a landscape photo that this project has not already used."""
    api_key = _pexels_api_key()
    if not api_key:
        return None, None, "Pexels is not configured."
    query = thumbnail_search_query(project, brief)
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for page in (1, 2):
                search = await client.get(
                    "https://api.pexels.com/v1/search",
                    headers={"Authorization": api_key},
                    params={"query": query, "per_page": 15, "orientation": "landscape", "page": page},
                )
                search.raise_for_status()
                photo = choose_unused_photo(search.json().get("photos") or [], exclude_ids)
                if photo is None:
                    continue
                src = photo.get("src") or {}
                url = src.get("large2x") or src.get("large") or src.get("original")
                response = await client.get(url)
                response.raise_for_status()
                if len(response.content) > 20 * 1024 * 1024:
                    exclude_ids.add(str(photo.get("id")))
                    continue
                return response.content, {
                    "external_id": str(photo.get("id") or ""),
                    "source_url": photo.get("url"),
                    "download_url": url,
                    "photographer": photo.get("photographer"),
                }, None
    except Exception as exc:  # noqa: BLE001
        reason = f"Pexels photo search failed: {_public_error(exc)}"
        logger.warning(reason)
        return None, None, reason
    return None, None, f"No unused Pexels photo matched “{query}”."


def _compose_stock_thumbnail(image_bytes: bytes, title: str) -> bytes:
    """Crop a stock photo to 16:9 and add a restrained, readable title treatment."""
    source = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    canvas = ImageOps.fit(source, (1280, 720), method=Image.Resampling.LANCZOS)
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle((0, 430, 1280, 720), fill=(5, 10, 18, 205))
    draw.rectangle((0, 430, 18, 720), fill=(0, 229, 255, 255))
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    try:
        font = ImageFont.truetype(font_path, 64)
    except OSError:
        font = ImageFont.load_default()
    words = (title or "Your next move").upper().split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        width = draw.textbbox((0, 0), candidate, font=font)[2]
        if current and width > 1120:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    lines = lines[:2]
    y = 470
    for line in lines:
        draw.text((70, y), line, font=font, fill=(255, 255, 255, 255))
        y += 78
    composed = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    output = io.BytesIO()
    composed.save(output, format="PNG", optimize=True)
    return output.getvalue()


class ThumbnailBatch:
    """Images ready to insert, plus a user-facing reason when none were made."""

    def __init__(self, images: list[dict] | None = None, error: str | None = None):
        self.images = images or []
        self.error = error


def generated_thumbnail_document(
    *,
    asset_id: str,
    project_id: str,
    brief: dict,
    variant_number: int,
    source: str,
    saved,
    store,
    prompt: str,
    info: dict,
    stock_external_id: str | None = None,
    source_url: str | None = None,
) -> dict:
    """A generated thumbnail must not occupy the stock unique key.

    ``assets_stock_unique`` is (project_id, scene_id, external_id, source) for
    any string external_id. Stock clips use the Pexels photo id there. A
    generated thumbnail stores that photo id separately and leaves
    ``external_id`` unset so a repeated photo cannot raise E11000.
    """
    title = brief.get("thumbnail_title_text") or brief.get("brief", {}).get("thumbnail_title_text") or "Thumbnail"
    doc = {
        "id": asset_id,
        "project_id": project_id,
        "scene_id": None,
        "name": f"{title} · variant {variant_number}",
        "asset_type": "generated_thumbnail",
        "source": source,
        "source_url": source_url,
        "preview_url": saved.url,
        "preview_path": saved.preview_path,
        "file_path": str(saved.file_path) if saved.file_path else None,
        "storage_mode": store.mode,
        "storage_key": saved.key,
        "width": 1280,
        "height": 720,
        "duration": None,
        "tags": ["thumbnail", "generated"],
        "prompt": prompt,
        "provider": source,
        "model": "photo-search+local-compositor" if source == "thumbnail_pexels" else info["model"],
        "mock": _use_mock() or source == "mock_thumbnail",
        "brief_snapshot": {k: brief.get(k) for k in (
            "thumbnail_title_text", "visual_composition", "emotion_angle",
            "background_idea", "subject_focal_point", "colour_direction",
            "click_trigger", "image_prompt",
        )},
        "status": "generated",
    }
    if stock_external_id:
        doc["stock_external_id"] = stock_external_id
    return doc


def _is_duplicate_key(exc: BaseException) -> bool:
    return getattr(exc, "code", None) == 11000 or type(exc).__name__ == "DuplicateKeyError"


async def insert_generated_thumbnails(db, docs: list[dict]) -> tuple[list[dict], str | None]:
    """Insert thumbnails one at a time. A unique-index clash is skipped, not a 500."""
    saved: list[dict] = []
    for doc in docs:
        try:
            await db.assets.insert_one(dict(doc))
            saved.append(doc)
        except Exception as exc:  # noqa: BLE001
            if _is_duplicate_key(exc):
                logger.warning(
                    "thumbnail insert skipped duplicate project=%s stock=%s",
                    doc.get("project_id"), doc.get("stock_external_id"),
                )
                continue
            raise
    if docs and not saved:
        return [], "That photo is already saved on this project. Generate again for a different image."
    return saved, None


# ---------------- Public ----------------

async def generate_thumbnail_images(
    project: dict,
    brief: dict,
    *,
    variants: int = 1,
    project_id: str,
    exclude_photo_ids: set[str] | None = None,
) -> ThumbnailBatch:
    """Generate `variants` thumbnail images for the given brief + project.

    Mock mode writes .svg files; real mode writes .png files.
    Both served from /api/static/thumbs/{project_id}/{asset_id}.{ext}.
    """
    variants = max(1, min(int(variants or 1), 3))
    inner = brief.get("brief") if isinstance(brief.get("brief"), dict) else brief
    prompt = _prompt_from_brief(project, inner or {})
    info = provider_info()
    out: list[dict] = []
    reasons: list[str] = []
    used_photos = set(exclude_photo_ids or [])
    dir_path = STATIC_ROOT / project_id
    dir_path.mkdir(parents=True, exist_ok=True)

    for i in range(variants):
        asset_id = str(uuid.uuid4())
        session_id = f"thumb-{project_id}-{asset_id}"
        ext = "svg"
        image_bytes: bytes | None = None
        source = "mock_thumbnail"
        source_item: dict | None = None

        if _use_mock():
            image_bytes = _mock_svg(asset_id, project, inner or {})
        else:
            image_bytes, gemini_error = await _gemini_image(prompt, session_id)
            if image_bytes is not None:
                ext = "png"
                source = info["provider"]
            else:
                if gemini_error:
                    reasons.append(gemini_error)
                stock_bytes, source_item, pexels_error = await _pexels_stock_image(
                    project, inner or {}, used_photos,
                )
                if stock_bytes is not None:
                    title = (inner or {}).get("thumbnail_title_text", "")
                    try:
                        image_bytes = _compose_stock_thumbnail(stock_bytes, title)
                    except Exception as exc:  # noqa: BLE001
                        image_bytes = None
                        reasons.append(f"Could not compose the stock photo: {_public_error(exc)}")
                    else:
                        ext = "png"
                        source = "thumbnail_pexels"
                        photo_id = str((source_item or {}).get("external_id") or "")
                        if photo_id:
                            used_photos.add(photo_id)
                elif pexels_error:
                    reasons.append(pexels_error)

        if image_bytes is None:
            logger.warning(
                "Thumbnail generation failed for %s variant %s: %s",
                project_id, i, reasons[-1] if reasons else "no image",
            )
            continue

        file_path = dir_path / f"{asset_id}.{ext}"
        file_path.write_bytes(image_bytes)

        store = get_storage()
        key = f"thumbs/{project_id}/{asset_id}.{ext}"
        content_type = "image/png" if ext == "png" else "image/svg+xml"
        saved = store.save_file(file_path, key, content_type=content_type)
        out.append(generated_thumbnail_document(
            asset_id=asset_id,
            project_id=project_id,
            brief=inner or {},
            variant_number=i + 1,
            source=source,
            saved=saved,
            store=store,
            prompt=prompt,
            info=info,
            stock_external_id=(source_item or {}).get("external_id"),
            source_url=(source_item or {}).get("source_url"),
        ))
    error = None
    if not out:
        seen: list[str] = []
        for reason in reasons:
            if reason not in seen:
                seen.append(reason)
        error = " ".join(seen) or "Image generation failed. No image provider returned a picture."
    return ThumbnailBatch(out, error)
