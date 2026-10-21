"""One authoritative frame/sample timeline for FacelessForge exports.

The duration selected on the project owns the final artifact. Generated text,
scene timestamps, measured voiceover length, and ffmpeg's ``-shortest`` must
never silently replace that selection.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
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
        elif abs(ratio - 1.0) <= max_tempo_delta:
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

    Returns the fitted whole-second target, or None when no in-bounds target
    can bring the narration within tempo tolerance (caller must rebuild the
    script/voiceover instead). Never raises on bad input — returns None.
    """
    try:
        measured = float(voiceover_seconds or 0)
        intro = float(intro_seconds or 0)
    except (TypeError, ValueError):
        return None
    if measured <= 0 or intro < 0:
        return None
    fitted = int(round(measured + intro))
    if fitted < minimum or fitted > maximum:
        return None
    narration = fitted - intro
    if narration <= 0:
        return None
    if abs(measured / narration - 1.0) > max_tempo_delta:
        return None
    return fitted
