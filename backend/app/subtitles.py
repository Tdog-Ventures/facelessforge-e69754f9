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


def subtitle_region(play_res_x: int, play_res_y: int) -> tuple[int, int, int, int]:
    """Pixel box the burned captions occupy, including their bottom margin.

    Alignment 2 places the text bottom ``margin_v`` pixels above the frame.
    Two lines plus the outline sit above that edge. Returned as
    ``(x0, y0, x1, y1)``.
    """
    font, margin_l, margin_r, margin_v = _ass_layout(play_res_x, play_res_y)
    band_h = 2 * font + 16
    y0 = max(0, int(play_res_y) - int(margin_v) - band_h)
    return int(margin_l), y0, max(int(margin_l), int(play_res_x) - int(margin_r)), int(play_res_y)


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
    """Soft-wrap a caption into at most 2 lines without dropping words.

    CONSTITUTION §5: Never more than 2 lines; text band ≤15% of frame height.
    A line that exceeds the character budget keeps its words — an ellipsis
    clamp used to cut the last word off the sentence.
    """
    text = (text or "").strip().replace("\r", "")
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= max_chars_per_line:
        return text
    words = text.split()
    lines = ["", ""]
    index = 0
    for word in words:
        placed = False
        for i in range(index, 2):
            candidate = f"{lines[i]} {word}".strip()
            if not lines[i] or len(candidate) <= max_chars_per_line:
                lines[i] = candidate
                index = i
                placed = True
                break
        if not placed:
            lines[1] = f"{lines[1]} {word}".strip()
            index = 1
    return "\n".join(line for line in lines if line)


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


def build_cues_from_words(
    words: list[dict],
    *,
    narration_start: float = 0.0,
    fit_scale: float = 1.0,
    video_end: float | None = None,
    words_per_cue: int = DEFAULT_WORDS_PER_CUE,
    max_cue_seconds: float = DEFAULT_MAX_CUE_SECONDS,
    min_cue_seconds: float = DEFAULT_MIN_CUE_SECONDS,
) -> list[dict]:
    """Group whisper words onto the video clock.

    Word ``start``/``end`` are seconds from the beginning of the narration
    file. Playback places that file at ``narration_start`` (the same intro
    delay as ``place_narration``). When the words were timed before the fit,
    ``fit_scale`` is ``budget/measured`` — atempo uses ``measured/budget``,
    so cue time is ``narration_start + word_time * (budget/measured)``.
    Times already taken from the fitted file use ``fit_scale=1``.

    ``max_cue_seconds`` does not close a cue. A duration clamp used to flush
    mid-sentence and leave the last words on a stub that ended early.
    """
    if fit_scale <= 0:
        raise ValueError("fit_scale must be positive")
    # Kept on the signature so callers share one clock. It must not split a
    # sentence or drop the words that cross it.
    del max_cue_seconds
    placed: list[dict] = []
    for word in words or []:
        token = str(word.get("word") or "").strip()
        if not token:
            continue
        start = float(narration_start) + float(word["start"]) * float(fit_scale)
        end = float(narration_start) + float(word["end"]) * float(fit_scale)
        if end < start:
            end = start
        placed.append({"word": token, "start": start, "end": end})
    if not placed:
        return []

    cues: list[dict] = []
    bucket: list[dict] = []

    def _close(group: list[dict]) -> dict:
        start = group[0]["start"]
        end = group[-1]["end"]
        if end < start + min_cue_seconds:
            end = start + min_cue_seconds
        if video_end is not None:
            limit = float(video_end)
            if end > limit:
                end = limit
            if end - start < min(min_cue_seconds, max(limit, 0.0)):
                start = end - min(min_cue_seconds, max(limit, 0.0))
            if start < 0:
                start = 0.0
            if end > limit:
                end = limit
            if end < start:
                end = start
        return {
            "start": start,
            "end": end,
            "text": " ".join(item["word"] for item in group),
            "words": list(group),
        }

    def _flush() -> None:
        nonlocal bucket
        if not bucket:
            return
        cues.append(_close(bucket))
        bucket = []

    for index, word in enumerate(placed):
        bucket.append(word)
        nxt = placed[index + 1] if index + 1 < len(placed) else None
        long_pause = nxt is not None and (nxt["start"] - word["end"]) > 0.45
        sentence_break = word["word"].endswith((".", "!", "?"))
        cue_full = len(bucket) >= words_per_cue
        if sentence_break or long_pause or cue_full:
            _flush()
    _flush()

    # A 1-2 word tail is the remainder of the previous sentence, not its own
    # caption. Leaving it separate is what cut "dries." off the line on screen.
    if (
        len(cues) >= 2
        and len(cues[-1]["words"]) <= 2
        and not cues[-2]["text"].rstrip().endswith((".", "!", "?"))
    ):
        merged = cues[-2]["words"] + cues[-1]["words"]
        cues[-2] = _close(merged)
        cues.pop()
    return cues


def build_srt_from_words(
    words: list[dict],
    *,
    intro_offset_seconds: float = 0.0,
    words_per_cue: int = DEFAULT_WORDS_PER_CUE,
    max_cue_seconds: float = DEFAULT_MAX_CUE_SECONDS,
    min_cue_seconds: float = DEFAULT_MIN_CUE_SECONDS,
    fit_scale: float = 1.0,
    video_end: float | None = None,
) -> str:
    """Word-synchronised SRT on the video clock.

    ``intro_offset_seconds`` is the narration placement delay, not a fudge
    factor and not a scene boundary. See :func:`build_cues_from_words`.
    """
    cues = build_cues_from_words(
        words,
        narration_start=intro_offset_seconds,
        fit_scale=fit_scale,
        video_end=video_end,
        words_per_cue=words_per_cue,
        max_cue_seconds=max_cue_seconds,
        min_cue_seconds=min_cue_seconds,
    )
    out: list[str] = []
    for idx, cue in enumerate(cues, 1):
        text = _clean_caption(cue["text"], max_chars_per_line=DEFAULT_MAX_CHARS_PER_LINE)
        if not text:
            continue
        out.append(f"{idx}\n{_format_ts(cue['start'])} --> {_format_ts(cue['end'])}\n{text}\n")
    return "\n".join(out) + ("\n" if out else "")


def write_srt_from_words(
    words: list[dict],
    out_path: Path,
    *,
    intro_offset_seconds: float = 0.0,
    words_per_cue: int = DEFAULT_WORDS_PER_CUE,
    max_cue_seconds: float = DEFAULT_MAX_CUE_SECONDS,
    min_cue_seconds: float = DEFAULT_MIN_CUE_SECONDS,
    fit_scale: float = 1.0,
    video_end: float | None = None,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        build_srt_from_words(
            words,
            intro_offset_seconds=intro_offset_seconds,
            words_per_cue=words_per_cue,
            max_cue_seconds=max_cue_seconds,
            min_cue_seconds=min_cue_seconds,
            fit_scale=fit_scale,
            video_end=video_end,
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
    if cs >= 100:
        cs = 0
        s += 1
        if s >= 60:
            s = 0
            m += 1
            if m >= 60:
                m = 0
                h += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass_karaoke(
    words: list[dict],
    *,
    intro_offset_seconds: float = 0.0,
    words_per_cue: int = DEFAULT_WORDS_PER_CUE,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
    fit_scale: float = 1.0,
    video_end: float | None = None,
    max_cue_seconds: float = DEFAULT_MAX_CUE_SECONDS,
    min_cue_seconds: float = DEFAULT_MIN_CUE_SECONDS,
) -> str:
    """Generate ASS karaoke subtitles on the narration placement clock."""
    header = build_ass_header(play_res_x, play_res_y)
    cues = build_cues_from_words(
        words,
        narration_start=intro_offset_seconds,
        fit_scale=fit_scale,
        video_end=video_end,
        words_per_cue=words_per_cue,
        max_cue_seconds=max_cue_seconds,
        min_cue_seconds=min_cue_seconds,
    )
    if not cues:
        return header
    lines = [header.strip()]
    for cue in cues:
        karaoke = ""
        for word in cue["words"]:
            dur_cs = max(1, int(round((word["end"] - word["start"]) * 100)))
            karaoke += f"{{\\k{dur_cs}}}{word['word']} "
        lines.append(
            f"Dialogue: 0,{_ass_ts(cue['start'])},{_ass_ts(cue['end'])},Default,,0,0,0,,{karaoke.strip()}"
        )
    return "\n".join(lines) + "\n"


def write_ass_karaoke(
    words: list[dict],
    out_path: Path,
    *,
    intro_offset_seconds: float = 0.0,
    words_per_cue: int = DEFAULT_WORDS_PER_CUE,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
    fit_scale: float = 1.0,
    video_end: float | None = None,
    max_cue_seconds: float = DEFAULT_MAX_CUE_SECONDS,
    min_cue_seconds: float = DEFAULT_MIN_CUE_SECONDS,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        build_ass_karaoke(
            words,
            intro_offset_seconds=intro_offset_seconds,
            words_per_cue=words_per_cue,
            play_res_x=play_res_x,
            play_res_y=play_res_y,
            fit_scale=fit_scale,
            video_end=video_end,
            max_cue_seconds=max_cue_seconds,
            min_cue_seconds=min_cue_seconds,
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
