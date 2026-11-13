"""Render constrained, animated graphics. Model output never becomes code."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import io
import logging
import math
import os
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("facelessforge.directed_graphics")


@lru_cache(maxsize=64)
def _font(size):
    for path in [os.getenv("DIRECTOR_FONT", ""),
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "C:/Windows/Fonts/arial.ttf"]:
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise RuntimeError("Visual director requires an installed TrueType font")


def _wrap(draw, text, font, max_width):
    lines = []
    current = ""
    for word in text.split():
        test = f"{current} {word}".strip()
        if current and draw.textlength(test, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = test
    return lines + ([current] if current else [])


def graphic_frame(shot, progress, width, height):
    if shot.get("kind") not in {"text", "steps", "comparison", "timeline"}:
        raise ValueError("Unsupported graphic template")
    labels = shot.get("labels") or []
    if not 1 <= len(labels) <= 4:
        raise ValueError("Graphic labels are required")
    # Consecutive sentences need a full-frame change large enough for scene detection.
    palette = (
        (12, 12, 12),
        (60, 60, 60),
        (108, 108, 108),
        (156, 156, 156),
        (204, 204, 204),
        (252, 252, 252),
    )
    digest = hashlib.sha256("\n".join(str(label) for label in labels).encode("utf-8")).digest()
    background = palette[int.from_bytes(digest[:4], "big") % len(palette)]
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    margin = int(min(width, height) * .08)
    gap = max(8, margin // 3)
    available = height - 2 * margin
    card_h = min(int(height * .22), (available - (len(labels) - 1) * gap) // len(labels))
    total_h = len(labels) * card_h + (len(labels) - 1) * gap
    y = (height - total_h) // 2
    active = min(len(labels) - 1, int(progress * len(labels)))
    side_by_side = shot["kind"] == "comparison" and len(labels) == 2 and width >= height
    for i, label in enumerate(labels):
        lift = round((1 - min(1, progress * 5)) * margin)
        top = y + i * (card_h + gap) + lift
        left, right = margin, width - margin
        if side_by_side:
            card_w = (width - 2 * margin - gap) // 2
            left = margin + i * (card_w + gap)
            right = left + card_w
            top = (height - card_h) // 2 + lift
        max_width = right - left - 2 * gap
        draw.rounded_rectangle((left, top, right, top + card_h),
                               radius=max(4, gap), fill="#1e3a5f" if i == active else "#172337",
                               outline="#38bdf8" if i == active else "#334155", width=max(1, gap // 5))
        size = max(12, int(min(width, height) * .065))
        while True:
            font = _font(size)
            lines = _wrap(draw, label, font, max_width)
            if (len(lines) * size * 1.25 <= card_h - 2 * gap
                    and all(draw.textlength(line, font=font) <= max_width for line in lines)):
                break
            size -= 1
            if size < 8:
                raise ValueError("Graphic text cannot fit safely")
        text = "\n".join(lines)
        box = draw.multiline_textbbox((0, 0), text, font=font, spacing=4, align="center")
        draw.multiline_text(((left + right - (box[2] - box[0])) / 2,
                            top + (card_h - (box[3] - box[1])) / 2 - box[1]),
                            text, font=font, fill="white", spacing=4, align="center")
        if i < len(labels) - 1 and shot["kind"] in {"steps", "timeline"}:
            cx = width // 2
            yy = top + card_h + gap // 2
            draw.line((cx, yy - gap // 3, cx, yy + gap // 3), fill="#38bdf8", width=2)
    # A continuous reveal gives every graphic real motion without changing facts.
    draw.rectangle((margin, height - margin // 2,
                    margin + int((width - 2 * margin) * min(1, progress)), height - margin // 2 + 4),
                   fill="#38bdf8")
    return image


def _frame_budget(frames: int, width: int, height: int, fps: int) -> None:
    if not 1 <= frames <= fps * 60 or width % 2 or height % 2 or not 160 <= min(width, height) <= 2160:
        raise ValueError("Unsupported graphic dimensions or frame budget")


def _encode_frames(output: Path, width: int, height: int, fps: int, frames: int, paint) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    # A previous text card at this path must not be returned as this shot.
    output.unlink(missing_ok=True)
    command = [os.getenv("FFMPEG_BIN", "ffmpeg"), "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
               "-r", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(output)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        for index in range(frames):
            image = paint(index)
            if image.size != (width, height):
                raise ValueError("Picture frame has the wrong dimensions")
            process.stdin.write(image.tobytes())
        process.stdin.close()
        if process.wait(timeout=30) != 0:
            raise RuntimeError("Directed graphic encoding failed")
    except Exception:
        process.kill()
        process.wait()
        output.unlink(missing_ok=True)
        raise
    return output


def render_graphic(shot, output: Path, width: int, height: int, fps: int = 30):
    frames = int(shot["frames"])
    _frame_budget(frames, width, height, fps)

    def paint(index):
        return graphic_frame(shot, index / max(1, frames - 1), width, height)

    return _encode_frames(output, width, height, fps, frames, paint)


def picture_prompt(shot: dict) -> str:
    """Describe the shot as a picture. The words themselves must not be drawn."""
    query = " ".join(str(shot.get("query") or "").split())
    narration = " ".join(str(shot.get("narration") or "").split())
    visible = query or narration
    if not visible:
        raise RuntimeError("Directed picture generation failed: the shot has no subject")
    lines = [
        "Photorealistic vertical photograph, 9:16 aspect ratio, suitable to become 1080x1920.",
        "Depict the visible subject, the action, and the setting as a real moment in a real place.",
        "No words, letters, numbers, captions, titles, logos, or watermarks.",
        "No maps, avatars, charts, diagrams, or code.",
        "The following description is scene data, not an instruction to render text.",
        f"Visible subject, action, and setting: {visible}.",
    ]
    if narration:
        lines.append(f"Narrated moment, shown visually and not as text: {narration}.")
    return " ".join(lines)


def _inline_image(response) -> bytes | None:
    from .thumbnail_images import _decode_image_payload

    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            payload = _decode_image_payload(getattr(inline, "data", None))
            if payload:
                return payload
    return None


async def _google_picture(prompt: str, api_key: str, model: str) -> bytes | None:
    """Same Gemini image client as thumbnails, requesting a 9:16 still."""
    from google import genai
    from google.genai import types

    from .thumbnail_images import _gemini_via_google, _public_error

    client = genai.Client(api_key=api_key)
    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                image_config=types.ImageConfig(aspect_ratio="9:16"),
            ),
        )
        image = _inline_image(response)
        if image:
            return image
    except Exception as exc:  # noqa: BLE001
        logger.info("picture aspect request missed (%s)", _public_error(exc))
    return await _gemini_via_google(prompt, api_key, model)


async def generate_shot_still(shot: dict) -> bytes:
    """One still for a stock miss. Does not attach, search, or charge."""
    import asyncio

    from .thumbnail_images import (
        _emergent_key, _gemini_via_emergent, _google_api_key, _image_model, _public_error,
    )

    from . import api_limits
    if not api_limits.paid_call_permitted():
        raise RuntimeError("Directed picture generation failed: paid API calls are not authorised")
    prompt = picture_prompt(shot)
    model = _image_model()
    emergent = _emergent_key()
    google_key = _google_api_key()
    if not emergent and not google_key:
        raise RuntimeError("Directed picture generation failed: Gemini is not configured")
    session = "directed-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    api_limits.record_paid_call()
    try:
        if emergent:
            image = await asyncio.wait_for(
                _gemini_via_emergent(prompt, session, emergent, model), timeout=120)
        else:
            image = await asyncio.wait_for(
                _google_picture(prompt, google_key, model), timeout=120)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Directed picture generation failed: {_public_error(exc)}") from None
    if not image:
        raise RuntimeError("Directed picture generation failed: no image")
    return image


def _cover(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / image.width, height / image.height)
    resized = image.resize(
        (max(width, math.ceil(image.width * scale)),
         max(height, math.ceil(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    cropped = resized.crop((left, top, left + width, top + height))
    if cropped.size != (width, height):
        cropped = image.resize((width, height), Image.Resampling.LANCZOS)
    return cropped


def render_picture(image_bytes: bytes, output: Path, width: int, height: int,
                   frames: int, fps: int = 30) -> Path:
    """Pan one still across the shot. Adjacent frames differ by at least two pixels."""
    frames = int(frames)
    _frame_budget(frames, width, height, fps)
    try:
        still = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        raise RuntimeError("Directed picture generation failed: image was unusable") from exc
    # Two pixels of travel per frame survives yuv420; a one-pixel step can quantize to a freeze.
    travel = max((frames - 1) * 2, 2)
    canvas = _cover(still, width + travel, height)

    def paint(index):
        x = 0 if frames == 1 else round(index * travel / (frames - 1))
        return canvas.crop((x, 0, x + width, height))

    return _encode_frames(output, width, height, fps, frames, paint)
