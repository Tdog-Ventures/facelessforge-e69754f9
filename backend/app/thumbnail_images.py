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
import logging
import os
import uuid
from pathlib import Path

from .storage import get_storage

logger = logging.getLogger("facelessforge.thumbnail_images")

STATIC_ROOT = Path(__file__).parent.parent / "static" / "thumbs"
STATIC_ROOT.mkdir(parents=True, exist_ok=True)


def _use_mock() -> bool:
    key = os.environ.get("EMERGENT_LLM_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
    flag = os.environ.get("USE_MOCK_THUMBNAIL_IMAGES", "true").strip().lower() in ("1", "true", "yes")
    return flag or not key


def is_mock_mode() -> bool:
    return _use_mock()


def provider_info() -> dict:
    return {
        "mock": _use_mock(),
        "provider": os.environ.get("THUMBNAIL_IMAGE_PROVIDER", "gemini_nano_banana"),
        "model": os.environ.get("THUMBNAIL_IMAGE_MODEL", "gemini-3.1-flash-image-preview"),
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
    return "\n".join(parts)


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

async def _gemini_image(prompt: str, session_id: str) -> bytes | None:
    """Call Gemini Nano Banana via emergentintegrations. Returns PNG bytes or None."""
    api_key = os.environ.get("EMERGENT_LLM_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None
    model = os.environ.get("THUMBNAIL_IMAGE_MODEL", "gemini-3.1-flash-image-preview")
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(
            api_key=api_key,
            session_id=session_id,
            system_message="You are a cinematic YouTube thumbnail designer.",
        )
        chat.with_model("gemini", model).with_params(modalities=["image", "text"])
        _, images = await chat.send_message_multimodal_response(UserMessage(text=prompt))
        if images:
            # Use first image
            data = images[0].get("data")
            if data:
                return base64.b64decode(data)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("Gemini image generation failed: %s", e)
        return None


# ---------------- Public ----------------

async def generate_thumbnail_images(
    project: dict,
    brief: dict,
    *,
    variants: int = 1,
    project_id: str,
) -> list[dict]:
    """Generate `variants` thumbnail images for the given brief + project.

    Returns a list of normalised asset payloads ready to be inserted into the assets
    collection. Mock mode writes .svg files; real mode writes .png files.
    Both served from /api/static/thumbs/{project_id}/{asset_id}.{ext}.
    """
    variants = max(1, min(int(variants or 1), 3))
    prompt = _prompt_from_brief(project, brief)
    info = provider_info()
    out: list[dict] = []
    dir_path = STATIC_ROOT / project_id
    dir_path.mkdir(parents=True, exist_ok=True)

    for i in range(variants):
        asset_id = str(uuid.uuid4())
        session_id = f"thumb-{project_id}-{asset_id}"
        ext = "svg"
        image_bytes: bytes | None = None
        source = "mock_thumbnail"

        if _use_mock():
            image_bytes = _mock_svg(asset_id, project, brief)
        else:
            image_bytes = await _gemini_image(prompt, session_id)
            if image_bytes is not None:
                ext = "png"
                source = info["provider"]
            else:
                image_bytes = _mock_svg(asset_id, project, brief)
                logger.info("Gemini failed, falling back to mock SVG for %s variant %s", project_id, i)

        if image_bytes is None:
            # No mock fallback — return real only, frontend shows skeleton/Retry
            logger.warning("Thumbnail generation failed for %s variant %s — no mock, will show Retry", project_id, i)
            continue

        file_path = dir_path / f"{asset_id}.{ext}"
        file_path.write_bytes(image_bytes)

        # Persist via storage backend
        store = get_storage()
        key = f"thumbs/{project_id}/{asset_id}.{ext}"
        content_type = "image/png" if ext == "png" else "image/svg+xml"
        saved = store.save_file(file_path, key, content_type=content_type)

        out.append({
            "id": asset_id,
            "project_id": project_id,
            "scene_id": None,
            "name": f"{brief.get('thumbnail_title_text', 'Thumbnail')} · variant {i + 1}",
            "asset_type": "generated_thumbnail",
            "source": source,
            "external_id": None,
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
            "provider": info["provider"],
            "model": info["model"],
            "mock": _use_mock() or source == "mock_thumbnail",
            "brief_snapshot": {k: brief.get(k) for k in (
                "thumbnail_title_text", "visual_composition", "emotion_angle",
                "background_idea", "subject_focal_point", "colour_direction",
                "click_trigger",
            )},
            "status": "generated",
        })
    return out
