"""TTS service — ElevenLabs primary, deterministic mock fallback.

Voice IDs are mapped per style. Override any single voice by setting
ELEVENLABS_VOICE_ID in the environment (applies to all styles).
Troy's voice: f1cjR1nonQ70hmW0yRhF
"""
from __future__ import annotations

import logging
import os
import re
import struct
import uuid
import wave
from pathlib import Path

import httpx

from .storage import get_storage

logger = logging.getLogger("facelessforge.tts")

STATIC_ROOT = Path(__file__).parent.parent / "static" / "audio"
STATIC_ROOT.mkdir(parents=True, exist_ok=True)

VOICE_STYLE_MAP = {
    # Each entry maps a style name to ElevenLabs voice settings + a curated voice ID.
    # voice_id is the ElevenLabs premade voice that best fits the style.
    # Override all styles with a single voice by setting ELEVENLABS_VOICE_ID env var.
    "narrator":    {"voice_id": "JBFqnCBsd6RMkjVDRZzb", "stability": 0.55, "similarity_boost": 0.80, "style": 0.0},   # George — Warm Storyteller (British)
    "energetic":   {"voice_id": "TX3LPaxmHKxFdv7VOQHJ", "stability": 0.35, "similarity_boost": 0.85, "style": 0.45},  # Liam — Energetic Creator (American)
    "documentary": {"voice_id": "onwK4e9ZLuTAKqWW03F9", "stability": 0.65, "similarity_boost": 0.75, "style": 0.0},   # Daniel — Steady Broadcaster (British)
    "calm":        {"voice_id": "SAz9YHcvj6GT2YYXdXww", "stability": 0.70, "similarity_boost": 0.75, "style": 0.0},   # River — Relaxed, Neutral (American)
    "dramatic":    {"voice_id": "SOYHLrjzK2X1ezoPC6cr", "stability": 0.30, "similarity_boost": 0.90, "style": 0.60},  # Harry — Fierce Warrior (American)
    "corporate":   {"voice_id": "cjVigY5qzO86Huf0OWal", "stability": 0.60, "similarity_boost": 0.78, "style": 0.0},   # Eric — Smooth, Trustworthy (American)
    "mysterious":  {"voice_id": "N2lVS1w4EtoT3dr4eOWO", "stability": 0.45, "similarity_boost": 0.82, "style": 0.30},  # Callum — Husky Trickster (American)
}

SUPPORTED_STYLES = list(VOICE_STYLE_MAP.keys())
MAX_CHARS_PER_CHUNK = 4500
ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"


def _elevenlabs_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()

def _voice_id(voice_style: str | None = None) -> str:
    # Env var override takes priority (e.g. ELEVENLABS_VOICE_ID=f1cjR1nonQ70hmW0yRhF for Troy)
    override = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    if override:
        return override
    # Look up the curated voice for this style
    if voice_style and voice_style in VOICE_STYLE_MAP:
        return VOICE_STYLE_MAP[voice_style]["voice_id"]
    return VOICE_STYLE_MAP["narrator"]["voice_id"]

def _use_mock() -> bool:
    flag = os.environ.get("USE_MOCK_TTS", "false").strip().lower() in ("1", "true", "yes")
    return flag or not _elevenlabs_key()

def is_mock_mode() -> bool:
    return _use_mock()

def provider_info() -> dict:
    return {
        "mock": _use_mock(),
        "provider": "elevenlabs",
        "voice_id": _voice_id(),
        "voices": {style: VOICE_STYLE_MAP[style]["voice_id"] for style in VOICE_STYLE_MAP},
        "default_voice_style": os.environ.get("DEFAULT_VOICE_STYLE", "narrator"),
    }

def _estimate_duration_seconds(text: str) -> int:
    words = len(re.findall(r"\b\w+\b", text or ""))
    return max(2, int(round(words / 2.5)))

def _write_mock_wav(path: Path, duration_s: int) -> None:
    sample_rate = 22050
    n_samples = int(sample_rate * max(1, duration_s))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack("<" + "h" * n_samples, *([0] * n_samples)))

def _mock_voiceover(
    text: str,
    asset_id: str,
    project_id: str,
    scene_id: str | None = None,
    name_suffix: str | None = None,
) -> dict:
    duration = _estimate_duration_seconds(text)
    fname = f"{asset_id}.wav"
    local_path = STATIC_ROOT / fname
    _write_mock_wav(local_path, duration)
    store = get_storage()
    key = f"audio/{project_id}/{fname}"
    saved = store.save_file(local_path, key, "audio/wav")
    local_path.unlink(missing_ok=True)
    return {
        "id": asset_id, "project_id": project_id, "scene_id": scene_id,
        "asset_type": "voiceover_audio",
        "name": (f"Voiceover {name_suffix}" if name_suffix else "Voiceover") + " (mock)",
        "url": saved.url, "preview_url": saved.url, "download_url": saved.url,
        "preview_path": saved.preview_path,
        "file_path": str(saved.file_path) if saved.file_path else None,
        "storage_mode": store.mode, "storage_key": saved.key,
        "duration": float(duration), "word_count": len(re.findall(r"\b\w+\b", text or "")),
        "provider": "mock", "voice_id": "mock", "voice_style": "narrator", "source": "mock_tts",
        "mock": True, "status": "generated",
    }

def _split_text(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 > max_chars:
            if current:
                chunks.append(current.strip())
            if len(sentence) > max_chars:
                for i in range(0, len(sentence), max_chars):
                    chunks.append(sentence[i:i + max_chars])
                current = ""
            else:
                current = sentence
        else:
            current = f"{current} {sentence}".strip() if current else sentence
    if current:
        chunks.append(current.strip())
    return chunks or [text[:max_chars]]

async def _elevenlabs_tts(text: str, voice_style: str) -> bytes:
    api_key = _elevenlabs_key()
    vid = _voice_id(voice_style)
    settings = VOICE_STYLE_MAP.get(voice_style, VOICE_STYLE_MAP["narrator"])
    chunks = _split_text(text)
    audio_parts: list[bytes] = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        for chunk in chunks:
            resp = await client.post(
                f"{ELEVENLABS_BASE_URL}/text-to-speech/{vid}",
                headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
                json={
                    "text": chunk,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {
                        "stability": settings["stability"],
                        "similarity_boost": settings["similarity_boost"],
                        "style": settings.get("style", 0.0),
                        "use_speaker_boost": True,
                    },
                },
            )
            resp.raise_for_status()
            audio_parts.append(resp.content)
    return b"".join(audio_parts)

def _estimate_mp3_duration(mp3_bytes: bytes) -> float:
    return max(1.0, len(mp3_bytes) / (128 * 1000 / 8))

async def generate_voiceover(
    text: str,
    voice_style: str = "narrator",
    project_id: str = "",
    asset_id: str | None = None,
    scene_id: str | None = None,
    name_suffix: str | None = None,
) -> dict:
    asset_id = asset_id or str(uuid.uuid4())
    voice_style = voice_style if voice_style in VOICE_STYLE_MAP else "narrator"
    if _use_mock():
        logger.info("[tts] mock mode")
        return _mock_voiceover(text, asset_id, project_id, scene_id, name_suffix)
    try:
        logger.info("[tts] ElevenLabs: voice=%s style=%s chars=%d", _voice_id(), voice_style, len(text))
        mp3_bytes = await _elevenlabs_tts(text, voice_style)
        fname = f"{asset_id}.mp3"
        local_path = STATIC_ROOT / fname
        local_path.write_bytes(mp3_bytes)
        store = get_storage()
        key = f"audio/{project_id}/{fname}"
        saved = store.save_file(local_path, key, "audio/mpeg")
        local_path.unlink(missing_ok=True)
        result = {
            "id": asset_id, "project_id": project_id, "scene_id": scene_id,
            "asset_type": "voiceover_audio",
            "name": f"Voiceover {name_suffix}" if name_suffix else "Voiceover",
            "url": saved.url, "preview_url": saved.url, "download_url": saved.url,
            "preview_path": saved.preview_path,
            "file_path": str(saved.file_path) if saved.file_path else None,
            "storage_mode": store.mode, "storage_key": saved.key,
            "duration": _estimate_mp3_duration(mp3_bytes),
            "word_count": len(re.findall(r"\b\w+\b", text or "")),
            "provider": "elevenlabs", "voice_id": _voice_id(voice_style),
            "voice_style": voice_style, "source": "elevenlabs",
            "mock": False, "status": "generated",
        }
        return result
    except Exception as exc:
        logger.exception("[tts] ElevenLabs failed, falling back to mock: %s", exc)
        return _mock_voiceover(text, asset_id, project_id, scene_id, name_suffix)
