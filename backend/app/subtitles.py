"""SRT subtitle generation from scene rows.

Used by the render pipeline to burn captions into the video. The frontend
already collects per-scene `caption_text` + start_time/end_time, so we just
serialise those into a standards-compliant SRT and let ffmpeg's `subtitles`
filter render them as a hard-burn.
"""
from __future__ import annotations

from pathlib import Path

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

# CONSTITUTION §5 TEXT ON SCREEN — ffmpeg subtitle filter style string.
# FIX: Captions spec - Arial-Bold 60 white black stroke 3, y=70% centered max-width 1000px
# This is consumed by the render pipeline (render.py) when burning captions.
FFMPEG_SUBTITLE_STYLE = (
    "FontName=Arial,"
    "FontSize=60,"
    "Bold=1,"
    "Alignment=2,"
    "MarginL=460,"
    "MarginR=460,"
    "MarginV=280,"
    "BorderStyle=1,"
    "OutlineColour=&H00000000,"
    "PrimaryColour=&H00FFFFFF,"
    "Outline=3,"
    "Shadow=0"
)

def _ass_layout(play_res_x: int, play_res_y: int) -> tuple[int, int, int, int]:
    """Return font and safe-zone margins for the actual output canvas."""
    if play_res_y > play_res_x:  # 9:16 vertical
        return 64, 96, 96, 300
    if play_res_x == play_res_y:  # 1:1 square
        return 56, 120, 120, 180
    return 60, 460, 460, 280  # 16:9 landscape


def build_ass_header(play_res_x: int = 1920, play_res_y: int = 1080) -> str:
    font_size, margin_l, margin_r, margin_v = _ass_layout(play_res_x, play_res_y)
    style = (
        f"Style: Default,Arial,{font_size},16777215,16777215,0,0,-1,0,0,0,"
        f"100,100,0,0,1,3,0,2,{margin_l},{margin_r},{margin_v},1"
    )
    return f"""[Script Info]
Title: FacelessForge Karaoke
ScriptType: v4.00+
PlayResX: {play_res_x}
PlayResY: {play_res_y}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{style}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


ASS_HEADER = build_ass_header()

# Tighter caption defaults per the Quality Constitution.
DEFAULT_WORDS_PER_CUE = 7        # CONSTITUTION §5: 6-8 word chunks
DEFAULT_MAX_CUE_SECONDS = 3.0    # CONSTITUTION §5: timed proportionally
DEFAULT_MIN_CUE_SECONDS = 0.5    # CONSTITUTION §5: never more than 2 lines
DEFAULT_MAX_CHARS_PER_LINE = 38  # CONSTITUTION §5: text band ≤15% frame height


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


def _clean_caption(text: str, max_chars_per_line: int = DEFAULT_MAX_CHARS_PER_LINE) -> str:
    """Soft-wrap a caption into at most 2 lines for legibility.

    CONSTITUTION §5: Never more than 2 lines; text band ≤15% of frame height.
    """
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
    words_per_cue: int = DEFAULT_WORDS_PER_CUE,    # CHANGED: 5 → 7 (CONSTITUTION §5)
    max_cue_seconds: float = DEFAULT_MAX_CUE_SECONDS,
    min_cue_seconds: float = DEFAULT_MIN_CUE_SECONDS,  # CHANGED: 0.7 → 0.5 (CONSTITUTION §5)
) -> str:
    """Word-synchronised SRT.

    Groups Whisper word records into compact cues that hold for at most
    ``max_cue_seconds`` and at least ``min_cue_seconds``. Each cue contains
    up to ``words_per_cue`` words (auto-breaks on long pauses or sentence
    punctuation for natural reading).

    CONSTITUTION §5: Captions cover FULL video; 6-8 word chunks timed
    proportionally within each scene's narration.
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
        # CHANGED: max_chars_per_line tightened to 38 (CONSTITUTION §5)
        text = _clean_caption(text, max_chars_per_line=DEFAULT_MAX_CHARS_PER_LINE)
        out.append(f"{idx}\n{_format_ts(start)} --> {_format_ts(end)}\n{text}\n")
    return "\n".join(out) + ("\n" if out else "")


def write_srt_from_words(
    words: list[dict],
    out_path: Path,
    *,
    intro_offset_seconds: float = 0.0,
    words_per_cue: int = DEFAULT_WORDS_PER_CUE,
    max_cue_seconds: float = DEFAULT_MAX_CUE_SECONDS,
    min_cue_seconds: float = DEFAULT_MIN_CUE_SECONDS,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        build_srt_from_words(
            words,
            intro_offset_seconds=intro_offset_seconds,
            words_per_cue=words_per_cue,
            max_cue_seconds=max_cue_seconds,
            min_cue_seconds=min_cue_seconds,
        ),
        encoding="utf-8",
    )
    return out_path


def write_srt_from_cues(cues: list[dict], out_path: Path) -> Path:
    """Write an SRT from pre-timed cues: [{"start": s, "end": e, "text": t}].

    Times are absolute seconds (caller applies any intro offset). Empty
    texts are dropped; non-positive durations are clamped to the minimum
    cue length.
    """
    out: list[str] = []
    for c in cues:
        text = _clean_caption(str(c.get("text") or ""),
                              max_chars_per_line=DEFAULT_MAX_CHARS_PER_LINE)
        if not text:
            continue
        start = float(c.get("start") or 0.0)
        end = float(c.get("end") or 0.0)
        if end <= start:
            end = start + DEFAULT_MIN_CUE_SECONDS
        out.append(f"{len(out) + 1}\n{_format_ts(start)} --> {_format_ts(end)}\n{text}\n")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    return out_path


def build_srt_from_text(
    text: str,
    *,
    total_seconds: float,
    intro_offset_seconds: float = 0.0,
    words_per_cue: int = DEFAULT_WORDS_PER_CUE,
) -> str:
    """Fallback SRT when no STT word timings exist.

    Spreads the narration words uniformly across ``total_seconds`` and
    reuses the word-cue grouping of :func:`build_srt_from_words`, so the
    output still honours the 6-8 word chunk spec (CONSTITUTION §5).
    """
    words = (text or "").split()
    if not words or total_seconds <= 0:
        return ""
    per = total_seconds / len(words)
    recs = [{"word": w, "start": i * per, "end": (i + 1) * per}
            for i, w in enumerate(words)]
    return build_srt_from_words(
        recs,
        intro_offset_seconds=intro_offset_seconds,
        words_per_cue=words_per_cue,
    )


def write_srt_from_text(
    text: str,
    out_path: Path,
    *,
    total_seconds: float,
    intro_offset_seconds: float = 0.0,
    words_per_cue: int = DEFAULT_WORDS_PER_CUE,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        build_srt_from_text(
            text,
            total_seconds=total_seconds,
            intro_offset_seconds=intro_offset_seconds,
            words_per_cue=words_per_cue,
        ),
        encoding="utf-8",
    )
    return out_path


def _ass_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass_karaoke(
    words: list[dict],
    *,
    intro_offset_seconds: float = 0.0,
    words_per_cue: int = DEFAULT_WORDS_PER_CUE,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
) -> str:
    """Generate ASS karaoke subtitles: 62px white bold black stroke, karaoke effect."""
    if not words:
        return build_ass_header(play_res_x, play_res_y)
    cues: list[tuple[float, float, str, list[dict]]] = []
    bucket: list[dict] = []
    bucket_start: float | None = None
    last_end = 0.0

    def _flush(end_ts: float):
        nonlocal bucket, bucket_start
        if not bucket or bucket_start is None:
            return
        cues.append((bucket_start, end_ts, " ".join(w["word"] for w in bucket), list(bucket)))
        bucket = []
        bucket_start = None

    for i, w in enumerate(words):
        wt = w["word"].strip()
        if not wt:
            continue
        if bucket_start is None:
            bucket_start = w["start"]
        bucket.append(w)
        last_end = w["end"]
        long_pause = i + 1 < len(words) and words[i + 1]["start"] - w["end"] > 0.45
        sentence_break = wt.endswith((".", "!", "?"))
        cue_full = len(bucket) >= words_per_cue
        cue_too_long = (w["end"] - bucket_start) >= 3.0
        if cue_full or sentence_break or long_pause or cue_too_long:
            _flush(w["end"])
    _flush(last_end)

    lines = [build_ass_header(play_res_x, play_res_y).strip()]
    for start, end, text, wlist in cues:
        s = start + intro_offset_seconds
        e = max(end, start + 0.5) + intro_offset_seconds
        karaoke = ""
        for w in wlist:
            dur_cs = max(1, int(round((w["end"] - w["start"]) * 100)))
            karaoke += f"{{\\k{dur_cs}}}{w['word']} "
        karaoke = karaoke.strip()
        lines.append(f"Dialogue: 0,{_ass_ts(s)},{_ass_ts(e)},Default,,0,0,0,,{karaoke}")
    return "\n".join(lines) + "\n"


def write_ass_karaoke(
    words: list[dict],
    out_path: Path,
    *,
    intro_offset_seconds: float = 0.0,
    words_per_cue: int = DEFAULT_WORDS_PER_CUE,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        build_ass_karaoke(
            words,
            intro_offset_seconds=intro_offset_seconds,
            words_per_cue=words_per_cue,
            play_res_x=play_res_x,
            play_res_y=play_res_y,
        ),
        encoding="utf-8",
    )
    return out_path


def build_ass_from_cues(
    cues: list[dict],
    *,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
) -> str:
    """Fallback ASS when no word timestamps."""
    lines = [build_ass_header(play_res_x, play_res_y).strip()]
    for c in cues:
        text = _clean_caption(str(c.get("text") or ""))
        if not text:
            continue
        s = float(c.get("start") or 0.0)
        e = float(c.get("end") or 0.0)
        if e <= s:
            e = s + 0.5
        lines.append(f"Dialogue: 0,{_ass_ts(s)},{_ass_ts(e)},Default,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def write_ass_from_cues(
    cues: list[dict],
    out_path: Path,
    *,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        build_ass_from_cues(
            cues,
            play_res_x=play_res_x,
            play_res_y=play_res_y,
        ),
        encoding="utf-8",
    )
    return out_path
