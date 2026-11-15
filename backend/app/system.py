from __future__ import annotations

import shutil


def _which(name: str) -> str | None:
    return shutil.which(name)


def probe_ffmpeg_status() -> dict:
    """Lightweight, side-effect-free ffmpeg/ffprobe probe (no apt install)."""
    sys_ffmpeg = _which("ffmpeg")
    sys_ffprobe = _which("ffprobe")
    return {
        "ffmpeg": sys_ffmpeg,
        "ffprobe": sys_ffprobe,
        "ffmpeg_source": "apt" if sys_ffmpeg else None,
        "ffprobe_source": "apt" if sys_ffprobe else None,
    }
