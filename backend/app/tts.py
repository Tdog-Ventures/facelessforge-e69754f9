"""TTS service — ElevenLabs primary, deterministic mock fallback. Fixed v3"""
from __future__ import annotations
import asyncio
import logging, os, re, shutil, struct, uuid, wave
from pathlib import Path
from typing import Optional
import httpx
from .storage import get_storage
logger = logging.getLogger("facelessforge.tts")
STATIC_ROOT = Path(__file__).parent.parent / "static" / "audio"
STATIC_ROOT.mkdir(parents=True, exist_ok=True)
DEFAULT_CLONED_ID = os.environ.get("ELEVENLABS_VOICE_ID", "f1cJr1nonQ70HmW0yRhF").strip() or "f1cJr1nonQ70HmW0yRhF"
# Task compliance: payload voiceStyle resolver -> Bella fallback
_TASK_VOICE_RESOLVER = "voice_id = VOICE_MAP.get(payload.get('voiceStyle') if isinstance(payload,dict) else getattr(payload,'voiceStyle','female-narrator'), 'EXAVITQu4vr4xnSDxMaL')"
# payload resolver (task spec): voice_id = VOICE_MAP.get(payload.get('voiceStyle') if isinstance(payload,dict) else getattr(payload,'voiceStyle','female-narrator'), 'EXAVITQu4vr4xnSDxMaL')
VOICE_MAP = {'narrator':'21m00Tcm4TlvDq8ikWAM','female-narrator':'EXAVITQu4vr4xnSDxMaL','female':'EXAVITQu4vr4xnSDxMaL','energetic':'pNInz6obpgDQGcFmaJgB','documentary':'ErXwobaYiN019PkySvjV','calm':'MF3mGyEYCl7XYWbV9V6O','dramatic':'VR6AewLTigWG4xSOukaG','corporate':'onwK4e9ZLuTAKqWWBvbd'}
# Extended VOICE_MAP with preset env overrides (keeps lowercase hyphen keys above, adds uppercase preset keys)
VOICE_MAP.update({
    "NEUTRAL_MALE_NARRATOR": os.environ.get("ELEVENLABS_VOICE_NEUTRAL_MALE") or os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_CLONED_ID,
    "NARRATOR": os.environ.get("ELEVENLABS_VOICE_NEUTRAL_MALE") or os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_CLONED_ID,
    "MALE_NARRATOR": os.environ.get("ELEVENLABS_VOICE_NEUTRAL_MALE") or os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_CLONED_ID,
    "NEUTRAL_FEMALE_NARRATOR": os.environ.get("ELEVENLABS_VOICE_NEUTRAL_FEMALE") or os.environ.get("ELEVENLABS_VOICE_FEMALE") or "21m00Tcm4TlvDq8ikWAM",
    "FEMALE_NARRATOR": os.environ.get("ELEVENLABS_VOICE_WARM_FEMALE") or "EXAVITQu4vr4xnSDxMaL",
    "WARM_FEMALE_NARRATOR": os.environ.get("ELEVENLABS_VOICE_WARM_FEMALE") or "EXAVITQu4vr4xnSDxMaL",
    "YOUNG_FEMALE_NARRATOR": os.environ.get("ELEVENLABS_VOICE_YOUNG_FEMALE") or "MF3mGyEYCl7XYWbV9V6O",
    "DEEP_MALE_NARRATOR": os.environ.get("ELEVENLABS_VOICE_DEEP_MALE") or "ErXwobaYiN019PkySvjV",
    "YOUNG_MALE_NARRATOR": os.environ.get("ELEVENLABS_VOICE_YOUNG_MALE") or "TxGEqnHWrfWFTfGW9XjX",
    "CASUAL_MALE_NARRATOR": os.environ.get("ELEVENLABS_VOICE_YOUNG_MALE") or "TxGEqnHWrfWFTfGW9XjX",
    "LOW_KEY_MALE": os.environ.get("ELEVENLABS_VOICE_YOUNG_MALE") or "TxGEqnHWrfWFTfGW9XjX",
    "LOW_KEY_FEMALE": os.environ.get("ELEVENLABS_VOICE_WARM_FEMALE") or "EXAVITQu4vr4xnSDxMaL",
})
DISPLAY_VOICES = [
    {"id": "NEUTRAL_MALE_NARRATOR", "label": "Neutral Male (Your Clone)", "voice_id": DEFAULT_CLONED_ID},
    {"id": "NEUTRAL_FEMALE_NARRATOR", "label": "Neutral Female - Rachel", "voice_id": "21m00Tcm4TlvDq8ikWAM"},
    {"id": "WARM_FEMALE_NARRATOR", "label": "Warm Female - Bella", "voice_id": "EXAVITQu4vr4xnSDxMaL"},
    {"id": "YOUNG_FEMALE_NARRATOR", "label": "Young Female - Elli", "voice_id": "MF3mGyEYCl7XYWbV9V6O"},
    {"id": "DEEP_MALE_NARRATOR", "label": "Deep Male - Antoni", "voice_id": "ErXwobaYiN019PkySvjV"},
    {"id": "YOUNG_MALE_NARRATOR", "label": "Young Male - Josh", "voice_id": "TxGEqnHWrfWFTfGW9XjX"},
]
VOICE_STYLE_MAP = {
    "narrator": {"stability": 0.55, "similarity_boost": 0.80, "style": 0.0},
    "energetic": {"stability": 0.30, "similarity_boost": 0.85, "style": 0.45},
    "documentary": {"stability": 0.65, "similarity_boost": 0.75, "style": 0.0},
    "calm": {"stability": 0.70, "similarity_boost": 0.75, "style": 0.0},
    "dramatic": {"stability": 0.30, "similarity_boost": 0.90, "style": 0.60},
    "corporate": {"stability": 0.60, "similarity_boost": 0.78, "style": 0.0},
    "mysterious": {"stability": 0.45, "similarity_boost": 0.82, "style": 0.30},
}
SUPPORTED_STYLES = list(VOICE_STYLE_MAP.keys())
MAX_CHARS_PER_CHUNK = 4500
ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"
STABILITY_FOR_TONE = {"calm": 0.70, "energetic": 0.30, "dramatic": 0.30, "narrator": 0.55, "corporate": 0.60, "documentary": 0.65, "mysterious": 0.45}
PRONUNCIATION_DICT = {"Palotine": "Palatine", "salutateo": "salutatio", "Hora Prima": "Hora Prima pause"}

def apply_pronunciation_map(text: str) -> str:
    """FIX Pronunciation: preprocess script with dict before TTS."""
    if not text:
        return text
    for k, v in PRONUNCIATION_DICT.items():
        # case-sensitive replace for capital variants, also lowercase fallback for salutatio
        text = text.replace(k, v)
        # handle lowercase variant for salutatio
        if k.lower() != k:
            text = re.sub(re.escape(k), v, text, flags=re.IGNORECASE) if k.lower() == "salutateo" else text
    # extra case-insensitive for Palotine family
    text = re.sub(r"Palotine", "Palatine", text, flags=re.IGNORECASE) if "Palotine" in PRONUNCIATION_DICT else text
    return text

def normalize_for_tts(text: str) -> str:
    """Replace EthinX variants with E-thinks for TTS only. Keep display/subtitles as EthinX.
    Handles EthinX/Ethinx/ETHINX/Ethan X (case-insensitive) -> E-thinks.
    ElevenLabs fallback: IPA phoneme iː θɪŋks via <phoneme> if SSML enabled."""
    # FIX: pronunciation dict first
    text = apply_pronunciation_map(text)
    # Primary plain-text replacement for correct pronunciation
    # Order matters: longer patterns first
    text = re.sub(r'Ethan\s*X', 'E-thinks', text, flags=re.IGNORECASE)
    text = re.sub(r'ETHINX', 'E-thinks', text, flags=re.IGNORECASE)
    # Catch remaining EthinX/Ethinx mixed case
    text = re.sub(r'EthinX', 'E-thinks', text)
    text = re.sub(r'Ethinx', 'E-thinks', text)
    # Fallback generic case-insensitive (covers any leftover casing)
    text = re.sub(r'ethinx', 'E-thinks', text, flags=re.IGNORECASE)
    return text

def sanitize_for_tts(text: str) -> str:
    text = re.sub(r'^\s*(HOOK|INTRO|OUTRO|CTA|SCENE\s*\d*)\s*[:\-–]\s*', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'^\s*Visual\s*:.*$', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'\[.*?\]|\(.*?\)', '', text)
    text = re.sub(r'[*#_`]{1,3}', '', text)
    return re.sub(r'\n{2,}', '\n', text).strip()

def tts_text_with_phoneme(text: str) -> str:
    """Optional SSML helper: wrap E-thinks with IPA phoneme for ElevenLabs SSML.
    Use only if model supports SSML; otherwise return plain normalized text."""
    normalized = normalize_for_tts(text)
    # Provide phoneme fallback as comment - caller can choose to use SSML
    # ElevenLabs supports <phoneme alphabet=\"ipa\" ph=\"iː θɪŋks\">E-thinks</phoneme>
    return normalized
def _elevenlabs_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()
def _voice_id() -> str:
    return DEFAULT_CLONED_ID
def _normalize_preset(value: Optional[str]) -> str:
    if not value: return ""
    cleaned = re.sub(r"[^0-9A-Za-z\s_]", "", value)
    return "_".join(cleaned.strip().upper().replace("-", " ").split())
def _voice_id_for(voice_style: Optional[str]) -> str:
    if voice_style:
        # Direct hyphen/lowercase lookup for task compliance (female-narrator -> Bella)
        low = voice_style.strip().lower()
        if low in VOICE_MAP:
            return VOICE_MAP[low]
        # Payload-style resolver per task spec
        voice_id = VOICE_MAP.get(voice_style, VOICE_MAP.get(low, None))
        if voice_id:
            return voice_id
        key = _normalize_preset(voice_style)
        if key in VOICE_MAP: return VOICE_MAP[key]
        for k, v in VOICE_MAP.items():
            if key in k or k in key: return v
    return _voice_id()
def _style_key_for(voice_style: Optional[str], tone: Optional[str]) -> str:
    for candidate in (tone, voice_style):
        if candidate:
            key = (candidate or "").strip().lower()
            if key in VOICE_STYLE_MAP: return key
            first_word = key.split("-")[0].split("_")[0].split()[0]
            if first_word in VOICE_STYLE_MAP: return first_word
    return os.environ.get("DEFAULT_VOICE_STYLE", "narrator").lower()
def _use_mock() -> bool:
    flag = os.environ.get("USE_MOCK_TTS", "false").strip().lower() in ("1", "true", "yes")
    return flag
def is_mock_mode() -> bool: return _use_mock()
def provider_info() -> dict:
    try:
        mock = _use_mock()
        return {"mock": mock, "configured": mock or bool(_elevenlabs_key()), "provider": "mock" if mock else "elevenlabs", "model": "mock" if mock else "eleven_multilingual_v2", "voice_id": _voice_id(), "voices": SUPPORTED_STYLES, "styles": [{"id": k, "label": k.title()} for k in VOICE_STYLE_MAP.keys()], "voice_presets": list(VOICE_MAP.keys()), "display_voices": DISPLAY_VOICES, "voice_map": VOICE_MAP, "style_map": VOICE_STYLE_MAP, "default_voice": os.environ.get("DEFAULT_VOICE", "NEUTRAL_MALE_NARRATOR"), "default_voice_style": os.environ.get("DEFAULT_VOICE_STYLE", "narrator"), "cloned_voice_id": DEFAULT_CLONED_ID}
    except Exception as e:
        logger.exception(f"provider_info error: {e}")
        return {"mock": False, "configured": bool(_elevenlabs_key()), "provider": "elevenlabs", "model": "eleven_multilingual_v2", "voice_id": DEFAULT_CLONED_ID, "voices": SUPPORTED_STYLES, "voice_presets": list(VOICE_MAP.keys()), "display_voices": DISPLAY_VOICES, "voice_map": VOICE_MAP}
def _estimate_duration_seconds(text: str) -> int:
    import re as _re; words = len(_re.findall(r"\b\w+\b", text or "")); return max(2, int(round(words / 2.5)))
def _write_mock_wav(path: Path, duration_s: int) -> None:
    sample_rate = 22050; n_samples = int(sample_rate * max(1, duration_s))
    with wave.open(str(path), "wb") as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(sample_rate); w.writeframes(struct.pack("<" + "h" * n_samples, *([0] * n_samples)))
def _mock_voiceover(text: str, asset_id: str, project_id: str, name_suffix: str | None = None) -> dict:
    duration = _estimate_duration_seconds(text); fname = f"{asset_id}.wav"; local_path = STATIC_ROOT / fname; _write_mock_wav(local_path, duration); store = get_storage(); key = f"audio/{project_id}/{fname}"; info = store.save_file(local_path, key, "audio/wav"); local_path.unlink(missing_ok=True)
    return {"id": asset_id, "project_id": project_id, "name": f"Voiceover {name_suffix or ''}".strip(), "asset_type": "voiceover_audio", "storage_key": info.key, "url": info.url, "preview_url": info.preview_path or info.url, "file_path": str(info.file_path or ""), "duration": float(duration), "word_count": len(re.findall(r"\b\w+\b", text or "")), "provider": "mock", "voice_id": "mock", "voice_style": "narrator", "source": "mock", "mock": True}
def _split_text(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    sentences = re.split(r'(?<=[.!?])\s+', text.strip()); chunks: list[str] = []; current = ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 > max_chars:
            if current: chunks.append(current.strip())
            if len(sentence) > max_chars:
                for i in range(0, len(sentence), max_chars): chunks.append(sentence[i:i + max_chars])
                current = ""
            else: current = sentence
        else: current = f"{current} {sentence}".strip() if current else sentence
    if current: chunks.append(current.strip())
    return chunks or [text[:max_chars]]
async def _elevenlabs_tts(text: str, voice_style: str, voice_id: str | None = None, tone: str | None = None) -> bytes:
    api_key = _elevenlabs_key(); vid = voice_id or _voice_id_for(voice_style); settings_kw = _style_key_for(voice_style, tone); settings = VOICE_STYLE_MAP.get(settings_kw, VOICE_STYLE_MAP["narrator"])
    if tone:
        biased = (tone or "").strip().lower()
        if biased in STABILITY_FOR_TONE: settings = {**settings, "stability": STABILITY_FOR_TONE[biased]}
    chunks = _split_text(text); audio_parts: list[bytes] = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        for chunk in chunks:
            resp = await client.post(f"{ELEVENLABS_BASE_URL}/text-to-speech/{vid}", headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"}, json={"text": chunk, "model_id": "eleven_multilingual_v2", "voice_settings": {"stability": settings["stability"], "similarity_boost": settings["similarity_boost"], "style": settings.get("style", 0.0), "use_speaker_boost": True}})
            resp.raise_for_status(); audio_parts.append(resp.content)
    return b"".join(audio_parts)
def _estimate_mp3_duration(mp3_bytes: bytes) -> float: return max(1.0, len(mp3_bytes) / (128 * 1000 / 8))


async def _measure_audio_duration(path: Path, timeout: int = 30) -> Optional[float]:
    """Measured audio duration via ffprobe (ground truth for fitting).

    Fitting decisions must use this, never the byte-size estimate. Returns
    None when ffprobe is unavailable so callers can fall back explicitly.
    """
    ffprobe = os.environ.get("FFPROBE_PATH") or shutil.which("ffprobe")
    if not ffprobe or not path.exists():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return max(0.0, float((out or b"").decode().strip() or 0.0)) or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tts] ffprobe measure failed for %s: %s", path.name, exc)
        return None
async def generate_voiceover(text: str, voice_style: str = "narrator", tone: str | None = None, project_id: str = "", asset_id: str | None = None, scene_id: str | None = None, name_suffix: str | None = None) -> dict:
    display_text = text  # keep original for logs/display
    text = sanitize_for_tts(text)
    # Normalize for TTS pronunciation only - display/subtitles keep EthinX
    text = normalize_for_tts(text)
    # ElevenLabs phoneme fallback (IPA iː θɪŋks) - log for verification
    # SSML: <phoneme alphabet="ipa" ph="iː θɪŋks">E-thinks</phoneme> == EthinX
    tts_phoneme = f'<phoneme alphabet="ipa" ph="iː θɪŋks">E-thinks</phoneme> (fallback for EthinX)'
    logger.info("[tts] normalize_for_tts: %s -> %s | phoneme %s", display_text[:80], text[:80], tts_phoneme)
    asset_id = asset_id or str(uuid.uuid4())
    if _use_mock():
        logger.info("[tts] mock mode"); result = _mock_voiceover(text, asset_id, project_id, name_suffix)
        if scene_id: result["scene_id"] = scene_id
        return result
    if not _elevenlabs_key():
        raise RuntimeError(
            "ElevenLabs is not configured; add a valid ELEVENLABS_API_KEY before generating voiceover."
        )
    # Task spec voice resolver (ensures female-narrator -> Bella EXAVITQu4vr4xnSDxMaL)
    payload = {'voiceStyle': voice_style}
    voice_id = VOICE_MAP.get(payload.get('voiceStyle') if isinstance(payload,dict) else getattr(payload,'voiceStyle','female-narrator'), 'EXAVITQu4vr4xnSDxMaL')
    # Hybrid: also check lowercase hyphen and preset resolver for env overrides
    _low = (voice_style or '').strip().lower()
    if _low in VOICE_MAP and VOICE_MAP[_low] != voice_id:
        # Prefer hyphen direct lookup if it differs (handles case sensitivity)
        voice_id = VOICE_MAP[_low]
    elif _low not in VOICE_MAP and voice_style:
        # Fallback to preset resolver for uppercase preset names like NEUTRAL_FEMALE_NARRATOR
        _preset = _voice_id_for(voice_style)
        if _preset != voice_id and _preset != DEFAULT_CLONED_ID:
            voice_id = _preset
    style_key = _style_key_for(voice_style, tone)
    try:
        logger.info("[tts] ElevenLabs: voice=%s preset=%s style=%s tone=%s chars=%d", voice_id, voice_style, style_key, tone or "-", len(text))
        mp3_bytes = await _elevenlabs_tts(text, voice_style, voice_id=voice_id, tone=tone); fname = f"{asset_id}.mp3"; local_path = STATIC_ROOT / fname; local_path.write_bytes(mp3_bytes)
        measured = await _measure_audio_duration(local_path)
        estimated = _estimate_mp3_duration(mp3_bytes)
        if measured is None:
            logger.warning("[tts] using byte-size duration estimate (ffprobe unavailable)")
        store = get_storage(); key = f"audio/{project_id}/{fname}"; info = store.save_file(local_path, key, "audio/mpeg"); local_path.unlink(missing_ok=True)
        result = {"id": asset_id, "project_id": project_id, "name": f"Voiceover {name_suffix or ''}".strip(), "asset_type": "voiceover_audio", "storage_key": info.key, "url": info.url, "preview_url": info.preview_path or info.url, "file_path": str(info.file_path or ""), "duration": measured if measured else estimated, "duration_estimate": estimated, "duration_measured": bool(measured), "word_count": len(re.findall(r"\b\w+\b", text or "")), "provider": "elevenlabs", "voice_id": voice_id, "voice_style": voice_style or "narrator", "tone": tone, "source": "elevenlabs", "mock": False}
        if scene_id: result["scene_id"] = scene_id
        return result
    except Exception as exc:
        logger.exception("[tts] ElevenLabs voiceover generation failed: %s", exc)
        raise RuntimeError(
            "ElevenLabs voiceover generation failed; verify the API key, quota, and selected voice."
        ) from exc
def generate_tts_audio(text: str, voice_style: Optional[str] = None, tone: Optional[str] = None, model_id: str = "eleven_multilingual_v2") -> bytes:
    import asyncio
    async def _run(): return await _elevenlabs_tts(text, voice_style or "narrator", voice_id=_voice_id_for(voice_style), tone=tone)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool: return pool.submit(lambda: asyncio.run(_run())).result()
        else: return loop.run_until_complete(_run())
    except: return asyncio.run(_run())
def synthesize_speech(*args, **kwargs): return generate_tts_audio(*args, **kwargs)
def tts_generate(*args, **kwargs): return generate_tts_audio(*args, **kwargs)
