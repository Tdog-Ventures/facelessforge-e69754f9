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
            end = _positive_seconds(item.get("end_time"))
            start = _positive_seconds(item.get("start_time"))
            if end is not None and start is not None and end > start:
                duration = end - start
        if duration is None:
            duration = 30.0
        duration = round(duration, 2)
        item["start_time"] = round(cursor, 2)
        item["end_time"] = round(cursor + duration, 2)
        item["duration"] = duration
        cursor += duration
        repaired.append(item)
    return repaired


def _positive_seconds(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number
