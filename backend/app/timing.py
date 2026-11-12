"""One authoritative frame/sample timeline for FacelessForge exports.

The duration selected on the project owns the final artifact. Generated text,
scene timestamps, measured voiceover length, and ffmpeg's ``-shortest`` must
never silently replace that selection.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import re
from typing import Any


DEFAULT_INTRO_SECONDS = Decimal("2.5")
DEFAULT_FPS = 30
DEFAULT_SAMPLE_RATE = 48_000

# Auto-fit loop bounds — mirror the product rule enforced on project updates
# (target_duration 5..3600s). The loop may only move the selected duration
# inside these bounds; anything else needs a script/voiceover rebuild.
MIN_AUTOFIT_TARGET_SECONDS = 5
MAX_AUTOFIT_TARGET_SECONDS = 3600
# A trailing fit is only worth applying when it actually moves the selection.
MIN_AUTOFIT_MOVE_SECONDS = 1


def _units(seconds: Decimal, rate: int) -> int:
    return int((seconds * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def target_word_count(narration_seconds: float, *, words_per_minute: int = 150) -> int:
    if narration_seconds <= 0 or words_per_minute <= 0:
        raise ValueError("Narration duration and speaking rate must be positive")
    words = Decimal(str(narration_seconds)) * Decimal(words_per_minute) / Decimal(60)
    return int(words.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class TimingContract:
    target_seconds: float
    intro_seconds: float
    narration_seconds: float
    fps: int
    sample_rate: int
    final_frames: int
    intro_frames: int
    narration_frames: int
    final_samples: int
    intro_samples: int
    narration_samples: int

    @classmethod
    def from_project(cls, project: dict[str, Any]) -> "TimingContract":
        intro = 0.0 if project.get("production_mode") == "commercial_ad" else float(DEFAULT_INTRO_SECONDS)
        return cls.from_target(float(project.get("target_duration") or 300), intro_seconds=intro)

    @classmethod
    def from_target(
        cls,
        target_seconds: float,
        *,
        intro_seconds: float = float(DEFAULT_INTRO_SECONDS),
        fps: int = DEFAULT_FPS,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> "TimingContract":
        target = Decimal(str(target_seconds))
        intro = Decimal(str(intro_seconds))
        if target <= intro:
            raise ValueError("Target duration must be longer than the intro")
        if fps <= 0 or sample_rate <= 0:
            raise ValueError("FPS and sample rate must be positive")

        final_frames = _units(target, fps)
        intro_frames = _units(intro, fps)
        final_samples = _units(target, sample_rate)
        intro_samples = _units(intro, sample_rate)
        narration_frames = final_frames - intro_frames
        narration_samples = final_samples - intro_samples
        return cls(
            target_seconds=final_frames / fps,
            intro_seconds=intro_frames / fps,
            narration_seconds=narration_frames / fps,
            fps=fps,
            sample_rate=sample_rate,
            final_frames=final_frames,
            intro_frames=intro_frames,
            narration_frames=narration_frames,
            final_samples=final_samples,
            intro_samples=intro_samples,
            narration_samples=narration_samples,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_scene_timeline(
    scenes: list[dict[str, Any]], *, narration_frames: int, fps: int = DEFAULT_FPS
) -> list[dict[str, Any]]:
    """Replace model timestamps with a continuous, exact frame allocation."""
    if not scenes:
        return []
    if narration_frames < len(scenes):
        raise ValueError("Narration budget must provide at least one frame per scene")
    if fps <= 0:
        raise ValueError("FPS must be positive")

    weights = [max(1, len(re.findall(r"\b\w+\b", str(scene.get("narration_text") or "")))) for scene in scenes]
    total_weight = sum(weights)
    distributable = narration_frames - len(scenes)
    ideals = [distributable * weight / total_weight for weight in weights]
    durations = [1 + math.floor(value) for value in ideals]
    remaining = narration_frames - sum(durations)
    order = sorted(range(len(scenes)), key=lambda i: (-(ideals[i] - math.floor(ideals[i])), i))
    for index in order[:remaining]:
        durations[index] += 1

    normalized: list[dict[str, Any]] = []
    cursor = 0
    for number, (scene, duration_frames) in enumerate(zip(scenes, durations), start=1):
        end = cursor + duration_frames
        item = dict(scene)
        item.update({
            "scene_number": number,
            "start_frame": cursor,
            "end_frame": end,
            "duration_frames": duration_frames,
            "start_time": round(cursor / fps, 6),
            "end_time": round(end / fps, 6),
            "duration": round(duration_frames / fps, 6),
        })
        normalized.append(item)
        cursor = end
    return normalized


def narration_fits_budget(
    measured_seconds: float, budget_seconds: float, *, max_tempo_delta: float = 0.05,
) -> bool:
    """Inclusive tempo policy, evaluated without float subtraction at the boundary.

    E.g. 2.375 / 2.5 is exactly the allowed -5%, but abs(0.95 - 1.0)
    in binary floating point exceeds 0.05. Compare decimal duration differences
    instead; no epsilon widens the policy to admit a value just outside it.
    """
    try:
        measured, budget, limit = (Decimal(str(value)) for value in (
            measured_seconds, budget_seconds, max_tempo_delta,
        ))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (
        measured.is_finite() and budget.is_finite() and limit.is_finite()
        and measured > 0 and budget > 0 and limit >= 0
        and abs(measured - budget) <= budget * limit
    )


def build_timing_status(
    *,
    project: dict[str, Any],
    scenes: list[dict[str, Any]] | None = None,
    voiceover: dict[str, Any] | None = None,
    max_tempo_delta: float = 0.05,
) -> dict[str, Any]:
    timing = TimingContract.from_project(project)
    scenes = scenes or []
    scene_total = max((float(scene.get("end_time") or 0) for scene in scenes), default=0.0)
    measured_vo = float((voiceover or {}).get("duration") or 0) or None
    frame_tolerance = 1 / timing.fps
    scenes_match = bool(scenes) and abs(scene_total - timing.narration_seconds) <= frame_tolerance

    if measured_vo is None:
        drift = None
        ratio = None
        action = "generate"
    else:
        drift = round(measured_vo - timing.narration_seconds, 6)
        ratio = measured_vo / timing.narration_seconds
        if abs(drift) <= frame_tolerance:
            action = "ready"
        elif narration_fits_budget(measured_vo, timing.narration_seconds, max_tempo_delta=max_tempo_delta):
            action = "tempo_fit"
        else:
            action = "rebuild"

    return timing.as_dict() | {
        "authority": "selected_duration",
        "scene_total_seconds": round(scene_total, 6),
        "scenes_match": scenes_match,
        "voiceover_seconds": measured_vo,
        "voiceover_drift_seconds": drift,
        "voiceover_tempo_ratio": round(ratio, 6) if ratio is not None else None,
        "voiceover_action": action,
        "ready": scenes_match and action in {"ready", "tempo_fit"},
    }


def suggest_fitted_duration(
    *,
    voiceover_seconds: float,
    intro_seconds: float = float(DEFAULT_INTRO_SECONDS),
    minimum: int = MIN_AUTOFIT_TARGET_SECONDS,
    maximum: int = MAX_AUTOFIT_TARGET_SECONDS,
    max_tempo_delta: float = 0.05,
) -> int | None:
    """Suggest a target duration that fits measured narration (VO is master).

    Prefer the rounded voice-plus-intro duration. Check its adjacent integer and
    legal range boundary as well: rounding alone can reject a valid fit (10s VO
    plus 2.5s intro fails at 12s but fits at 13s). Return None when no in-bounds
    whole-second target fits the tempo policy. Invalid audio returns None.
    """
    try:
        measured = float(voiceover_seconds or 0)
        intro = float(intro_seconds or 0)
    except (TypeError, ValueError):
        return None
    total = measured + intro
    if not math.isfinite(total) or measured <= 0 or intro < 0 or minimum > maximum:
        return None
    # For a positive narration budget the tempo ratio is monotonic in the target.
    # If a fit exists, one of the adjacent integers (clamped to the legal range)
    # fits; more distant targets cannot rescue a rejected pair.
    candidates = dict.fromkeys(
        max(minimum, min(maximum, candidate))
        for candidate in (round(total), math.floor(total), math.ceil(total))
    )
    for fitted in candidates:
        if narration_fits_budget(measured, fitted - intro, max_tempo_delta=max_tempo_delta):
            return fitted
    return None


def contiguous_timeline_from_durations(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Abut scene ranges using each scene's stored duration.

    Per-scene voiceover used to extend ``end_time`` from the old start and
    leave the next scene's start in place, so ranges overlapped and the last
    end no longer matched the sum of durations. This rewrite only repairs
    those timestamps. It does not change narration text or the project's
    selected target duration.
    """
    ordered = sorted(
        scenes or [],
        key=lambda scene: (int(scene.get("scene_number") or 0), str(scene.get("id") or "")),
    )
    cursor = 0.0
    repaired: list[dict[str, Any]] = []
    for scene in ordered:
        item = dict(scene)
        duration = _positive_seconds(item.get("duration"))
        if duration is None:
            end = _nonnegative_seconds(item.get("end_time"))
            start = _nonnegative_seconds(item.get("start_time"))
            if end is not None and start is not None and end > start:
                duration = end - start
        if duration is None:
            duration = 30.0
        duration = round(duration, 2)
        item["start_time"] = round(cursor, 2)
        item["end_time"] = round(cursor + duration, 2)
        item["duration"] = duration
        start_frame = int(round(item["start_time"] * DEFAULT_FPS))
        end_frame = int(round(item["end_time"] * DEFAULT_FPS))
        item["start_frame"] = start_frame
        item["end_frame"] = end_frame
        item["duration_frames"] = end_frame - start_frame
        cursor += duration
        repaired.append(item)
    return repaired


def _nonnegative_seconds(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _positive_seconds(value: Any) -> float | None:
    number = _nonnegative_seconds(value)
    if number is None or number <= 0:
        return None
    return number



class SceneAlignmentError(ValueError):
    """Fitted words do not match the script. Callers must not invent equal slots."""


def _norm_token(text: Any) -> str:
    return re.sub(r"[^\w]+", "", str(text or "").lower())


def _script_tokens(text: Any) -> list[str]:
    return [token for token in (_norm_token(part) for part in str(text or "").split()) if token]


def _word_token(word: Any) -> str:
    if not isinstance(word, dict):
        return ""
    return _norm_token(word.get("word"))


def _word_bounds(word: dict) -> tuple[float, float]:
    try:
        start = float(word["start"])
        end = float(word["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SceneAlignmentError("Spoken word is missing a timestamp") from exc
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
        raise SceneAlignmentError("Spoken word has an invalid timestamp")
    return start, end


def _next_spoken(words: list[dict], index: int) -> int:
    while index < len(words) and not _word_token(words[index]):
        index += 1
    return index


def _consume_scene_words(
    script_tokens: list[str],
    words: list[dict],
    index: int,
    scene_label: Any,
) -> tuple[list[int], int]:
    """Consume the next spoken tokens that spell this scene, in order.

    Adjacent whisper tokens may join into one script token, and adjacent
    script tokens may join into one whisper token, but only when the pieces
    spell the same word. A shared word such as "rain" does not match
    "raindrops", and words are never skipped to find a later hit.
    """
    if not script_tokens:
        raise SceneAlignmentError(f"Scene {scene_label} has no narration to align")
    consumed: list[int] = []
    script_index = 0
    cursor = _next_spoken(words, index)
    while script_index < len(script_tokens):
        cursor = _next_spoken(words, cursor)
        if cursor >= len(words):
            raise SceneAlignmentError(
                f"Scene {scene_label} is missing spoken words for '{script_tokens[script_index]}'"
            )
        spoken = _word_token(words[cursor])
        wanted = script_tokens[script_index]
        if spoken == wanted:
            consumed.append(cursor)
            cursor += 1
            script_index += 1
            continue
        joined = spoken
        group = [cursor]
        scan = cursor
        spelled = False
        while len(joined) < len(wanted):
            scan = _next_spoken(words, scan + 1)
            if scan >= len(words):
                break
            nxt = _word_token(words[scan])
            if not wanted.startswith(joined + nxt):
                break
            joined += nxt
            group.append(scan)
            if joined == wanted:
                consumed.extend(group)
                cursor = scan + 1
                script_index += 1
                spelled = True
                break
        if spelled:
            continue
        joined_script = wanted
        script_end = script_index
        while len(joined_script) < len(spoken):
            if script_end + 1 >= len(script_tokens):
                break
            nxt_script = script_tokens[script_end + 1]
            if not spoken.startswith(joined_script + nxt_script):
                break
            joined_script += nxt_script
            script_end += 1
            if joined_script == spoken:
                consumed.append(cursor)
                cursor += 1
                script_index = script_end + 1
                spelled = True
                break
        if spelled:
            continue
        raise SceneAlignmentError(
            f"Scene {scene_label} does not match the spoken words at '{wanted}'"
        )
    if not consumed:
        raise SceneAlignmentError(f"Scene {scene_label} matched no spoken words")
    previous: float | None = None
    for word_index in consumed:
        start, _end = _word_bounds(words[word_index])
        if previous is not None and start < previous:
            raise SceneAlignmentError(f"Scene {scene_label} spoken timestamps go backwards")
        previous = start
    return consumed, cursor


def align_scene_timeline(
    scenes: list[dict[str, Any]],
    words: list[dict[str, Any]],
    *,
    narration_frames: int,
    fps: int = DEFAULT_FPS,
) -> list[dict[str, Any]]:
    """Replace placeholder scene bounds with fitted-word boundaries.

    Word ``start``/``end`` are seconds on the fitted narration file.
    ``fit_scale`` is 1 and is not applied again. Returned ``start_time`` /
    ``end_time`` stay on the narration clock, the same keys as
    :func:`normalize_scene_timeline`. Intro is added by
    :func:`word_alignment_report`, not stored here.

    The first scene starts at 0 so leading silence stays on that picture.
    Each later scene starts at its first matched word, so the pause after a
    sentence stays on that sentence. The last scene ends on the narration
    budget, including trailing silence, and absorbs frame-rounding remainder.
    """
    if not isinstance(scenes, list) or not scenes:
        raise SceneAlignmentError("No scenes to align")
    if narration_frames < len(scenes) or fps <= 0:
        raise SceneAlignmentError("Narration budget must provide at least one frame per scene")
    if not isinstance(words, list) or not any(_word_token(word) for word in words):
        raise SceneAlignmentError("No spoken words to align")

    consumed_by_scene: list[list[int]] = []
    cursor = 0
    for index, scene in enumerate(scenes):
        label = scene.get("scene_number") or index + 1
        tokens = _script_tokens(scene.get("narration_text"))
        consumed, cursor = _consume_scene_words(tokens, words, cursor, label)
        consumed_by_scene.append(consumed)
    cursor = _next_spoken(words, cursor)
    if cursor < len(words):
        raise SceneAlignmentError("Leftover spoken words do not belong to a script segment")

    previous_word = -1.0
    boundaries = [0]
    for index, consumed in enumerate(consumed_by_scene):
        word_start, _word_end = _word_bounds(words[consumed[0]])
        if index > 0 and word_start <= previous_word:
            raise SceneAlignmentError("Spoken timestamps go backwards")
        previous_word = word_start
        if index == 0:
            continue
        frame = _units(Decimal(str(word_start)), fps)
        if frame <= boundaries[-1] or frame >= narration_frames:
            label = scenes[index].get("scene_number") or index + 1
            raise SceneAlignmentError(
                f"Scene {label} boundary does not fit the narration budget"
            )
        boundaries.append(frame)
    boundaries.append(narration_frames)

    aligned: list[dict[str, Any]] = []
    for index, (scene, consumed) in enumerate(zip(scenes, consumed_by_scene)):
        start = boundaries[index]
        end = boundaries[index + 1]
        item = dict(scene)
        number = scene.get("scene_number") or index + 1
        item.update({
            "scene_number": number,
            "start_frame": start,
            "end_frame": end,
            "duration_frames": end - start,
            "start_time": round(start / fps, 6),
            "end_time": round(end / fps, 6),
            "duration": round((end - start) / fps, 6),
            "aligned_words": [dict(words[word_index]) for word_index in consumed],
        })
        aligned.append(item)
    return aligned


def split_segment_holds(duration_frames: int, *, max_seconds: float, fps: int) -> list[int]:
    """Contiguous visual holds of one spoken segment. Each piece is <= max_seconds.

    The pieces sum to ``duration_frames``. Neighboring segments are not resized.
    A segment that already fits the maximum stays one hold.
    """
    if duration_frames < 1:
        raise ValueError("Segment must contain at least one frame")
    if fps <= 0 or max_seconds <= 0:
        raise ValueError("FPS and maximum hold must be positive")
    max_frames = _units(Decimal(str(max_seconds)), fps)
    if max_frames < 1:
        raise ValueError("Maximum hold must cover at least one frame")
    if duration_frames <= max_frames:
        return [duration_frames]
    count = math.ceil(duration_frames / max_frames)
    base, extra = divmod(duration_frames, count)
    pieces = [base + (1 if index < extra else 0) for index in range(count)]
    if any(piece < 1 or piece > max_frames for piece in pieces) or sum(pieces) != duration_frames:
        raise ValueError("Hold split did not fit the spoken segment")
    return pieces


def _within_frame(deviation_ms: float, fps: int) -> bool:
    return deviation_ms <= (1000.0 / fps) + 1e-6


def _cue_matches_word(cue_word: dict, spoken: dict, intro: float) -> bool:
    if _norm_token(cue_word.get("word")) != _norm_token(spoken.get("word")):
        return False
    try:
        cue_start = float(cue_word["start"])
        spoken_start = float(intro) + float(spoken["start"])
    except (KeyError, TypeError, ValueError):
        return False
    return abs(cue_start - spoken_start) <= 1e-4


def word_alignment_report(
    scenes: list[dict[str, Any]],
    cues: list[dict[str, Any]],
    *,
    fps: int,
    intro_seconds: float,
    asset_ids: list[Any] | None = None,
    visual_spans: list[tuple[float, float]] | None = None,
) -> list[dict[str, Any]]:
    """Picture-versus-speech records on the video clock. No I/O.

    Narration times are the spoken segment (first scene at ``intro_seconds``,
    each later scene at its first word, last scene ending on the narration
    budget). Visual times are that segment's picture window. Pass
    ``visual_spans`` on the narration clock; intro is added once. Subtitle
    times are the first and last cue from :func:`build_cues_from_words` that
    contain the segment's words. Cue starts are not moved.

    ``within_frame`` is false when the picture misses the spoken segment by
    more than one frame (``1000/fps`` milliseconds).
    """
    try:
        intro = float(intro_seconds)
    except (TypeError, ValueError) as exc:
        raise SceneAlignmentError("Intro seconds must be zero or positive") from exc
    if fps <= 0 or not math.isfinite(intro) or intro < 0:
        raise SceneAlignmentError("FPS must be positive and intro must be zero or positive")
    if not scenes:
        return []
    if asset_ids is None:
        asset_ids = [None] * len(scenes)
    if len(asset_ids) != len(scenes):
        raise SceneAlignmentError("Asset ids must match the aligned scenes")
    if visual_spans is not None and len(visual_spans) != len(scenes):
        raise SceneAlignmentError("Visual spans must match the aligned scenes")
    if any(not scene.get("aligned_words") for scene in scenes):
        raise SceneAlignmentError("Scene has no spoken-word alignment")

    budget = scenes[-1]["end_frame"] / fps
    records: list[dict[str, Any]] = []
    for index, scene in enumerate(scenes):
        spoken_words = scene["aligned_words"]
        raw_start = 0.0 if index == 0 else float(spoken_words[0]["start"])
        if index + 1 < len(scenes):
            raw_end = float(scenes[index + 1]["aligned_words"][0]["start"])
        else:
            raw_end = budget
        if visual_spans is None:
            visual_start = scene["start_frame"] / fps
            visual_end = scene["end_frame"] / fps
        else:
            visual_start, visual_end = visual_spans[index]
            visual_start = float(visual_start)
            visual_end = float(visual_end)
        if visual_end < visual_start or raw_end < raw_start:
            raise SceneAlignmentError("Timeline interval goes backwards")
        narration_start = round(intro + raw_start, 6)
        narration_end = round(intro + raw_end, 6)
        picture_start = round(intro + visual_start, 6)
        picture_end = round(intro + visual_end, 6)
        deviation_ms = round(max(
            abs(picture_start - narration_start),
            abs(picture_end - narration_end),
        ) * 1000.0, 6)
        belonging = [
            cue for cue in cues
            if any(
                _cue_matches_word(cue_word, spoken, intro)
                for cue_word in (cue.get("words") or [])
                for spoken in spoken_words
            )
        ]
        try:
            number = int(scene.get("scene_number") or index + 1)
        except (TypeError, ValueError):
            number = index + 1
        records.append({
            "scene_id": scene.get("id"),
            "scene_number": number,
            "narration_start": narration_start,
            "narration_end": narration_end,
            "visual_start": picture_start,
            "visual_end": picture_end,
            "asset_id": asset_ids[index],
            "subtitle_start": belonging[0]["start"] if belonging else None,
            "subtitle_end": belonging[-1]["end"] if belonging else None,
            "deviation_ms": deviation_ms,
            "within_frame": _within_frame(deviation_ms, fps),
        })
    return records


def alignment_within_frame(records: list[dict[str, Any]], *, fps: int) -> bool:
    """False when any picture window misses its spoken segment by more than one frame."""
    if fps <= 0:
        raise SceneAlignmentError("FPS must be positive")
    return all(_within_frame(float(record["deviation_ms"]), fps) for record in records)
