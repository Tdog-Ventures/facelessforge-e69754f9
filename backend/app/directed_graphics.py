"""Render constrained, animated graphics. Model output never becomes code."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import os
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont


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


def render_graphic(shot, output: Path, width: int, height: int, fps: int = 30):
    frames = int(shot["frames"])
    if not 1 <= frames <= fps * 60 or width % 2 or height % 2 or not 160 <= min(width, height) <= 2160:
        raise ValueError("Unsupported graphic dimensions or frame budget")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [os.getenv("FFMPEG_BIN", "ffmpeg"), "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
               "-r", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(output)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        for index in range(frames):
            image = graphic_frame(shot, index / max(1, frames - 1), width, height)
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
