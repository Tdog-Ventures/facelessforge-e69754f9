"""TTS service — ElevenLabs primary, deterministic mock fallback.

Voice ID is controlled by ELEVENLABS_VOICE_ID env var.
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
    "narrator":     {"stability": 0.55, "similarity_boost": 0.80, "style": 0.0},
    "energetic":    {"stability": 0.35, "similarity_boost": 0.85, "style": 0.45},
    "documentary":  {"stability": 0.65, "similarity_boost": 0.75, "style": 0.0},
    "calm":         {"stability": 0.70, "similarity_boost": 0.75, "style": 0.0},
    "dramatic":     {"stability": 0.30, "similarity_boost": 0.90, "style": 0.60},
    "corporate":    {"stability": 0.60, "similarity_boost": 0.78, "style": 0.0},
    "mysterious":   {"stability": 0.45, "similarity_boost": 0.82, "style": 0.30},
}

SUPPORTED_STYLES = list(VOICE_STYLE_MAP.keys())
MAX_CHARS_PER_CHUNK = 4500
ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"


def _elevenlabs_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()

def _voice_id() -> str:
    return os.environ.get("ELEVENLABS_VOICE_ID", "f1cjR1nonQ70hmW0yRhF").strip()

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
        "voices": SUPPORTED_STYLES,
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

def _mock_voiceover(text: str, asset_id: str, project_id: str, name_suffix: str | None = None) -> dict:
    duration = _estimate_duration_seconds(text)
    fname = f"{asset_id}.wav"
    local_path = STATIC_ROOT / fname
    _write_mock_wav(local_path, duration)
    store = get_storage()
    key = f"audio/{project_id}/{fname}"
    info = store.save_file(local_path, key, "audio/wav")
    local_path.unlink(missing_ok=True)
    return {
        "id": asset_id, "project_id": project_id,
        "name": f"Voiceover {name_suffix or ''}".strip(),
        "asset_type": "voiceover_audio",
        "url": info.url,
        "preview_url": info.preview_path or info.url,
        "file_path": str(info.file_path or ""),
        "duration": float(duration), "word_count": len(re.findall(r"\b\w+\b", text or "")),
        "provider": "mock", "voice_id": "mock", "voice_style": "narrator", "source": "mock",
        "mock": True,
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
    vid = _voice_id()
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
        result = _mock_voiceover(text, asset_id, project_id, name_suffix)
        if scene_id:
            result["scene_id"] = scene_id
        return result
    try:
        logger.info("[tts] ElevenLabs: voice=%s style=%s chars=%d", _voice_id(), voice_style, len(text))
        mp3_bytes = await _elevenlabs_tts(text, voice_style)
        fname = f"{asset_id}.mp3"
        local_path = STATIC_ROOT / fname
        local_path.write_bytes(mp3_bytes)
        store = get_storage()
        key = f"audio/{project_id}/{fname}"
        info = store.save_file(local_path, key, "audio/mpeg")
        local_path.unlink(missing_ok=True)
        result = {
            "id": asset_id, "project_id": project_id,
            "name": f"Voiceover {name_suffix or ''}".strip(),
            "asset_type": "voiceover_audio",
            "url": info.url, "preview_url": info.preview_path or info.url,
            "file_path": str(info.file_path or ""),
            "duration": _estimate_mp3_duration(mp3_bytes),
            "word_count": len(re.findall(r"\b\w+\b", text or "")),
            "provider": "elevenlabs", "voice_id": _voice_id(),
            "voice_style": voice_style, "source": "elevenlabs",
            "mock": False,
        }
        if scene_id:
            result["scene_id"] = scene_id
        return result
    except Exception as exc:
        logger.exception("[tts] ElevenLabs failed, falling back to mock: %s", exc)
        result = _mock_voiceover(text, asset_id, project_id, name_suffix)
        if scene_id:
            result["scene_id"] = scene_id
        return result
