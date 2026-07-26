"""TTS service — ElevenLabs primary, OpenAI fallback, mock-safe.

Provider chain (each step falls through to the next on failure):
    1. ElevenLabs   (requires ELEVENLABS_API_KEY, TTS_PROVIDER=elevenlabs)
    2. OpenAI TTS   (Emergent LLM key)
    3. Mock WAV     (deterministic silence)

Public API:
    await generate_voiceover(
        text,
        voice_style,
        project_id,
        asset_id=None,
        scene_id=None,
    ) -> dict
"""
from __future__ import annotations

import logging
import os
import re
import uuid
import wave
from pathlib import Path

from .storage import get_storage

logger = logging.getLogger("facelessforge.tts")

STATIC_ROOT = Path(__file__).parent.parent / "static" / "audio"
STATIC_ROOT.mkdir(parents=True, exist_ok=True)

# Voice style → per-provider voice. Keys are stable contract for the frontend.
VOICE_STYLE_MAP = {
    "narrator":     {"openai_voice": "onyx",    "eleven_env": "ELEVENLABS_VOICE_NARRATOR",    "eleven_default": "f1cjR1nonQ70hmW0yRhF"},
    "energetic":    {"openai_voice": "nova",    "eleven_env": "ELEVENLABS_VOICE_ENERGETIC",   "eleven_default": "MF3mGyEYCl7XYWbV9V6O"},
    "documentary":  {"openai_voice": "sage",    "eleven_env": "ELEVENLABS_VOICE_DOCUMENTARY", "eleven_default": "ErXwobaYiN019PkySvjV"},
    "calm":         {"openai_voice": "alloy",   "eleven_env": "ELEVENLABS_VOICE_CALM",        "eleven_default": "EXAVITQu4vr4xnSDxMaL"},
    "dramatic":     {"openai_voice": "fable",   "eleven_env": "ELEVENLABS_VOICE_DRAMATIC",    "eleven_default": "VR6AewLTigWG4xSOukaG"},
    "corporate":    {"openai_voice": "echo",    "eleven_env": "ELEVENLABS_VOICE_CORPORATE",   "eleven_default": "TxGEqnHWrfWFTfGW9XjX"},
    "mysterious":   {"openai_voice": "shimmer", "eleven_env": "ELEVENLABS_VOICE_MYSTERIOUS",  "eleven_default": "AZnzlk1v7XfdyXxAFdtl"},
}

SUPPORTED_STYLES = list(VOICE_STYLE_MAP.keys())

# ElevenLabs' eleven_multilingual_v2 caps a single request at 5000 characters.
# Longer scripts are split on sentence boundaries and concatenated.
MAX_TEXT_CHARS = 100000  # absolute upper bound for sanity
MAX_ELEVENLABS_CHARS_PER_REQUEST = 4500


# ---------------- Provider detection ----------------

def _force_mock_flag() -> bool:
    return os.environ.get("USE_MOCK_TTS", "true").strip().lower() in ("1", "true", "yes")


def _has_elevenlabs() -> bool:
    return bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())


def _has_openai() -> bool:
    return bool(
        os.environ.get("EMERGENT_LLM_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )


def _preferred_provider() -> str:
    """Resolve the requested primary provider from env. Falls back through the
    chain at runtime if the preferred one fails."""
    raw = os.environ.get("TTS_PROVIDER", "openai").strip().lower()
    if raw in ("elevenlabs", "eleven", "11labs", "11"):
        return "elevenlabs"
    return "openai"


def _use_mock() -> bool:
    """True when neither real provider is usable, or USE_MOCK_TTS=true."""
    if _force_mock_flag():
        return True
    return not (_has_elevenlabs() or _has_openai())


def is_mock_mode() -> bool:
    return _use_mock()


def provider_info() -> dict:
    preferred = _preferred_provider()
    return {
        "mock": _use_mock(),
        "provider": preferred,
        "preferred": preferred,
        "elevenlabs_available": _has_elevenlabs(),
        "openai_available": _has_openai(),
        "model": (
            os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
            if preferred == "elevenlabs"
            else os.environ.get("OPENAI_TTS_MODEL", "tts-1")
        ),
        "voices": SUPPORTED_STYLES,
        "default_voice_style": os.environ.get("DEFAULT_VOICE_STYLE", "narrator"),
        "fallback_chain": ["elevenlabs", "openai", "mock"],
    }


# ---------------- Mock audio ----------------

def _estimate_duration_seconds(text: str) -> int:
    words = len(re.findall(r"\b\w+\b", text or ""))
    return max(2, int(round(words / 2.5)))  # ~150 wpm narration


def _write_mock_wav(path: Path, duration_s: int) -> None:
    """Write a valid PCM WAV of silence with the requested duration."""
    sample_rate = 22050
    n_samples = int(sample_rate * max(1, duration_s))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        chunk = b"\x00\x00" * 4096
        remaining = n_samples
        while remaining > 0:
            take = min(4096, remaining)
            w.writeframes(chunk[:take * 2])
            remaining -= take


# ---------------- ElevenLabs ----------------

def _elevenlabs_voice_for(style: str) -> str:
    cfg = VOICE_STYLE_MAP.get(style, VOICE_STYLE_MAP["narrator"])
    return os.environ.get(cfg["eleven_env"], cfg["eleven_default"])


def _elevenlabs_voice_settings():
    """Build a VoiceSettings dict honouring env overrides."""
    def _f(key: str, default: float) -> float:
        try:
            return float(os.environ.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "stability": _f("ELEVENLABS_STABILITY", 0.5),
        "similarity_boost": _f("ELEVENLABS_SIMILARITY_BOOST", 0.75),
        "style": _f("ELEVENLABS_STYLE", 0.0),
        "use_speaker_boost": os.environ.get("ELEVENLABS_USE_SPEAKER_BOOST", "true").lower() in ("1", "true", "yes"),
    }


async def _elevenlabs_tts(text: str, style: str, out_path: Path) -> tuple[bool, int]:
    """Generate audio via ElevenLabs. Returns (ok, duration_seconds_estimate).

    Text longer than ``MAX_ELEVENLABS_CHARS_PER_REQUEST`` is split on sentence
    boundaries; the per-chunk MP3s are concatenated with ffmpeg.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        return False, _estimate_duration_seconds(text)
    model_id = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
    output_format = os.environ.get("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")
    voice_id = _elevenlabs_voice_for(style)
    settings = _elevenlabs_voice_settings()
    try:
        from elevenlabs import ElevenLabs, VoiceSettings
        client = ElevenLabs(api_key=api_key)
        chunks = _split_for_tts(text, MAX_ELEVENLABS_CHARS_PER_REQUEST)
        if len(chunks) == 1:
            audio_stream = client.text_to_speech.convert(
                voice_id=voice_id,
                model_id=model_id,
                text=chunks[0],
                output_format=output_format,
                voice_settings=VoiceSettings(**settings),
            )
            with open(out_path, "wb") as f:
                for chunk in audio_stream:
                    if chunk:
                        f.write(chunk)
        else:
            # Generate each chunk, then ffmpeg-concat the MP3s.
            tmp_dir = out_path.parent / f"_tts_chunks_{out_path.stem}"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            chunk_paths: list[Path] = []
            for i, ctext in enumerate(chunks):
                cp = tmp_dir / f"part_{i:03d}.mp3"
                audio_stream = client.text_to_speech.convert(
                    voice_id=voice_id,
                    model_id=model_id,
                    text=ctext,
                    output_format=output_format,
                    voice_settings=VoiceSettings(**settings),
                )
                with open(cp, "wb") as f:
                    for chunk in audio_stream:
                        if chunk:
                            f.write(chunk)
                if cp.stat().st_size == 0:
                    logger.warning("ElevenLabs returned empty audio for chunk %d", i)
                    return False, _estimate_duration_seconds(text)
                chunk_paths.append(cp)
            # ffmpeg concat
            concat_list = tmp_dir / "concat.txt"
            concat_list.write_text("\n".join(f"file '{p.as_posix()}'" for p in chunk_paths) + "\n")
            import subprocess
            try:
                from imageio_ffmpeg import get_ffmpeg_exe
                ffmpeg_bin = get_ffmpeg_exe()
            except Exception:  # noqa: BLE001
                ffmpeg_bin = "/bin/ffmpeg"
            proc = subprocess.run(
                [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", str(concat_list),
                 "-c", "copy", str(out_path)],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                logger.warning("ElevenLabs chunk concat failed: %s", proc.stderr[-300:])
                return False, _estimate_duration_seconds(text)
            # Best-effort cleanup of chunk files
            for cp in chunk_paths:
                cp.unlink(missing_ok=True)
            concat_list.unlink(missing_ok=True)
            try:
                tmp_dir.rmdir()
            except OSError:
                pass
        if out_path.stat().st_size == 0:
            logger.warning("ElevenLabs returned empty audio; falling back.")
            return False, _estimate_duration_seconds(text)
        return True, _estimate_duration_seconds(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("ElevenLabs TTS failed (%s). Falling back.", e)
        try:
            if out_path.exists() and out_path.stat().st_size == 0:
                out_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False, _estimate_duration_seconds(text)


def _split_for_tts(text: str, max_chars: int) -> list[str]:
    """Split on sentence boundaries; pack greedy into chunks ≤ max_chars.

    Falls back to soft-wrap by clause when a single sentence exceeds the
    limit (rare but possible for run-on copy).
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    # Split on sentence terminators while keeping the punctuation
    import re
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        if len(s) > max_chars:
            # Hard-split very long sentence on commas / spaces
            for piece in re.split(r"(?<=[,;:])\s+", s):
                if len(buf) + 1 + len(piece) > max_chars and buf:
                    chunks.append(buf.strip())
                    buf = piece
                else:
                    buf = (buf + " " + piece).strip()
        elif len(buf) + 1 + len(s) > max_chars and buf:
            chunks.append(buf.strip())
            buf = s
        else:
            buf = (buf + " " + s).strip()
    if buf:
        chunks.append(buf.strip())
    return [c for c in chunks if c]


# ---------------- OpenAI ----------------

def _openai_voice_for(style: str) -> str:
    return VOICE_STYLE_MAP.get(style, VOICE_STYLE_MAP["narrator"])["openai_voice"]


async def _openai_tts(text: str, style: str, out_path: Path) -> tuple[bool, int]:
    """Try OpenAI TTS via Emergent-friendly OpenAI SDK. Returns (ok, dur)."""
    api_key = os.environ.get("EMERGENT_LLM_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return False, _estimate_duration_seconds(text)
    model = os.environ.get("OPENAI_TTS_MODEL", "tts-1")
    voice = _openai_voice_for(style)
    try:
        from openai import OpenAI
        base_url = os.environ.get("OPENAI_BASE_URL") or None
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = OpenAI(**kwargs)
        response = client.audio.speech.create(model=model, voice=voice, input=text)
        response.stream_to_file(str(out_path))
        return True, _estimate_duration_seconds(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("OpenAI TTS failed (%s). Falling back.", e)
        return False, _estimate_duration_seconds(text)


# ---------------- Public ----------------

async def generate_voiceover(
    *,
    text: str,
    voice_style: str,
    project_id: str,
    asset_id: str | None = None,
    scene_id: str | None = None,
    name_suffix: str = "",
) -> dict:
    if not text or not text.strip():
        raise ValueError("empty_text")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    if voice_style not in VOICE_STYLE_MAP:
        voice_style = os.environ.get("DEFAULT_VOICE_STYLE", "narrator")

    aid = asset_id or str(uuid.uuid4())
    dir_path = STATIC_ROOT / project_id
    dir_path.mkdir(parents=True, exist_ok=True)

    ext = "wav"
    source = "mock_tts"
    duration = _estimate_duration_seconds(text)
    model_used: str | None = None

    forced_mock = _force_mock_flag()
    real_ok = False

    if not forced_mock:
        preferred = _preferred_provider()
        order = ["elevenlabs", "openai"] if preferred == "elevenlabs" else ["openai", "elevenlabs"]

        for provider in order:
            if real_ok:
                break
            if provider == "elevenlabs" and _has_elevenlabs():
                out_path = dir_path / f"{aid}.mp3"
                real_ok, duration = await _elevenlabs_tts(text, voice_style, out_path)
                if real_ok:
                    ext = "mp3"
                    source = "elevenlabs_tts"
                    model_used = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
            elif provider == "openai" and _has_openai():
                out_path = dir_path / f"{aid}.mp3"
                real_ok, duration = await _openai_tts(text, voice_style, out_path)
                if real_ok:
                    ext = "mp3"
                    source = "openai_tts"
                    model_used = os.environ.get("OPENAI_TTS_MODEL", "tts-1")

    if not real_ok:
        out_path = dir_path / f"{aid}.wav"
        _write_mock_wav(out_path, duration)

    # Persist via storage backend (local: no-op; object: upload + remove local)
    store = get_storage()
    key = f"audio/{project_id}/{aid}.{ext}"
    content_type = "audio/mpeg" if ext == "mp3" else "audio/wav"
    saved = store.save_file(out_path, key, content_type=content_type)

    # Cost estimate (per provider, approximate)
    cost = 0.0
    if source == "elevenlabs_tts":
        # ElevenLabs charges ~$0.30 per 1k characters on Creator tier
        cost = round(len(text) / 1000 * 0.30, 4)
    elif source == "openai_tts":
        cost = round(len(text) / 1000 * 0.015, 4)

    provider_name = {
        "elevenlabs_tts": "elevenlabs",
        "openai_tts": "openai",
        "mock_tts": "mock_tts",
    }[source]

    return {
        "id": aid,
        "project_id": project_id,
        "scene_id": scene_id,
        "asset_type": "voiceover_audio",
        "source": source,
        "name": (f"Voiceover {name_suffix}" if name_suffix else "Voiceover") + (" (mock)" if source == "mock_tts" else ""),
        "text_source": "scene_narration" if scene_id else "full_script",
        "voice_style": voice_style,
        "provider": provider_name,
        "model": model_used,
        "duration": duration,
        "file_path": str(saved.file_path) if saved.file_path else None,
        "preview_url": saved.url,
        "preview_path": saved.preview_path,
        "storage_mode": store.mode,
        "storage_key": saved.key,
        "width": None,
        "height": None,
        "tags": ["voiceover", voice_style],
        "mock": source == "mock_tts",
        "status": "generated",
        "cost_estimate": cost,
    }
