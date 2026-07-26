"""Speech-to-Text transcription wrapper used by the render pipeline.

Calls OpenAI Whisper (via Emergent LLM key + emergentintegrations) with
word-level timestamp granularity and returns a normalised list of
``{word, start, end}`` dicts. Used to drive word-synchronised subtitle
burn-in instead of static caption_text labels.

Files >24 MB are not supported here — for now the render audio sits well
under that. Long audio could be chunked in the future.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("facelessforge.transcribe")

MAX_WHISPER_BYTES = 24 * 1024 * 1024


async def transcribe_words(audio_path: Path, *, language: str = "en") -> list[dict]:
    """Return a list of ``{"word": str, "start": float, "end": float}``.

    Returns an empty list if the API key is missing or transcription fails.
    The caller falls back to its own time-proportional cueing in that case.
    """
    if not audio_path.exists():
        return []
    if audio_path.stat().st_size > MAX_WHISPER_BYTES:
        logger.warning("audio too large for Whisper (%d bytes); skipping STT", audio_path.stat().st_size)
        return []
    api_key = os.environ.get("EMERGENT_LLM_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return []
    try:
        from emergentintegrations.llm.openai import OpenAISpeechToText  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.warning("emergentintegrations import failed: %s", e)
        return []
    try:
        stt = OpenAISpeechToText(api_key=api_key)
        with open(audio_path, "rb") as f:
            resp = await stt.transcribe(
                file=f,
                model="whisper-1",
                response_format="verbose_json",
                language=language,
                temperature=0.0,
                timestamp_granularities=["word"],
            )
        data = resp.model_dump() if hasattr(resp, "model_dump") else resp.dict()
        words = data.get("words") or []
        out: list[dict] = []
        for w in words:
            try:
                out.append({
                    "word": str(w.get("word") or "").strip(),
                    "start": float(w.get("start") or 0.0),
                    "end": float(w.get("end") or 0.0),
                })
            except (TypeError, ValueError):
                continue
        return [w for w in out if w["word"]]
    except Exception as e:  # noqa: BLE001
        logger.warning("Whisper transcription failed: %s", e)
        return []
