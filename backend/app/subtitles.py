"""SRT subtitle generation from scene rows.

Used by the render pipeline to burn captions into the video. The frontend
already collects per-scene `caption_text` + start_time/end_time, so we just
serialise those into a standards-compliant SRT and let ffmpeg's `subtitles`
filter render them as a hard-burn.
"""
from __future__ import annotations

from pathlib import Path


def _format_ts(seconds: float) -> str:
    """SRT timestamp: HH:MM:SS,mmm  (note the comma decimal separator)."""
    if seconds is None or seconds < 0:
        seconds = 0
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    s = (total_ms // 1000) % 60
    m = (total_ms // 60000) % 60
    h = total_ms // 3600000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _clean_caption(text: str, max_chars_per_line: int = 42) -> str:
    """Soft-wrap a caption into at most 2 lines for legibility."""
    text = (text or "").strip().replace("\r", "")
    if not text:
        return ""
    # Collapse whitespace
    text = " ".join(text.split())
    if len(text) <= max_chars_per_line:
        return text
    # Greedy two-line wrap
    words = text.split()
    line1, line2 = "", ""
    for w in words:
        if len(line1) + len(w) + 1 <= max_chars_per_line:
            line1 = f"{line1} {w}".strip()
        else:
            line2 = f"{line2} {w}".strip()
            if len(line2) > max_chars_per_line:
                # Hard cut — better than dropping content silently
                line2 = line2[: max_chars_per_line - 1] + "…"
                break
    return f"{line1}\n{line2}".strip()


def build_srt(
    scenes: list[dict],
    *,
    intro_offset_seconds: float = 0.0,
    prefer_caption_first: bool = True,
) -> str:
    """Return an SRT string for the provided scenes.

    The render pipeline prepends a static intro clip (the thumbnail) — pass
    its duration as `intro_offset_seconds` so subtitle timings align with
    the final concatenated video.

    Each scene contributes ONE cue, using `caption_text` (short hook) when
    available, falling back to `narration_text` truncated.
    """
    cues: list[str] = []
    idx = 1
    for s in sorted(scenes, key=lambda x: x.get("scene_number", 0)):
        start = float(s.get("start_time") or 0) + intro_offset_seconds
        end = float(s.get("end_time") or 0) + intro_offset_seconds
        if end <= start:
            # If timings are bad, give the cue a default 4s on screen
            end = start + 4.0
        if prefer_caption_first:
            raw = s.get("caption_text") or s.get("narration_text") or s.get("visual_direction") or ""
        else:
            raw = s.get("narration_text") or s.get("caption_text") or ""
        text = _clean_caption(str(raw))
        if not text:
            continue
        cues.append(
            f"{idx}\n"
            f"{_format_ts(start)} --> {_format_ts(end)}\n"
            f"{text}\n"
        )
        idx += 1
    return "\n".join(cues) + ("\n" if cues else "")


def write_srt(scenes: list[dict], out_path: Path, *, intro_offset_seconds: float = 0.0) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_srt(scenes, intro_offset_seconds=intro_offset_seconds), encoding="utf-8")
    return out_path


def build_srt_from_words(
    words: list[dict],
    *,
    intro_offset_seconds: float = 0.0,
    words_per_cue: int = 5,
    max_cue_seconds: float = 3.0,
    min_cue_seconds: float = 0.7,
) -> str:
    """Word-synchronised SRT.

    Groups Whisper word records into compact cues that hold for at most
    ``max_cue_seconds`` and at least ``min_cue_seconds``. Each cue contains
    up to ``words_per_cue`` words (auto-breaks on long pauses or sentence
    punctuation for natural reading).
    """
    if not words:
        return ""
    cues: list[tuple[float, float, str]] = []
    bucket_words: list[str] = []
    bucket_start: float | None = None
    last_end: float = 0.0

    def _flush(end_ts: float):
        nonlocal bucket_words, bucket_start
        if not bucket_words or bucket_start is None:
            return
        text = " ".join(bucket_words).strip()
        if text:
            start = bucket_start + intro_offset_seconds
            end = max(end_ts, bucket_start + min_cue_seconds) + intro_offset_seconds
            cues.append((start, end, text))
        bucket_words = []
        bucket_start = None

    for i, w in enumerate(words):
        wt = w["word"].strip()
        if not wt:
            continue
        if bucket_start is None:
            bucket_start = w["start"]
        bucket_words.append(wt)
        last_end = w["end"]
        long_pause = (i + 1 < len(words)
                      and words[i + 1]["start"] - w["end"] > 0.45)
        sentence_break = wt.endswith((".", "!", "?"))
        cue_full = len(bucket_words) >= words_per_cue
        cue_too_long = (w["end"] - bucket_start) >= max_cue_seconds
        if cue_full or sentence_break or long_pause or cue_too_long:
            _flush(w["end"])
    _flush(last_end)

    out: list[str] = []
    for idx, (start, end, text) in enumerate(cues, 1):
        # Wrap long single-line cues for readability
        text = _clean_caption(text, max_chars_per_line=42)
        out.append(f"{idx}\n{_format_ts(start)} --> {_format_ts(end)}\n{text}\n")
    return "\n".join(out) + ("\n" if out else "")


def write_srt_from_words(
    words: list[dict],
    out_path: Path,
    *,
    intro_offset_seconds: float = 0.0,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        build_srt_from_words(words, intro_offset_seconds=intro_offset_seconds),
        encoding="utf-8",
    )
    return out_path
