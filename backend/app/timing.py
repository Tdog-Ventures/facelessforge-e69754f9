"""
Authoritative video timing - single source of truth.
Requested duration is the ONLY authority. No VO scaling, no 30s rounding, no -shortest.
"""
from dataclasses import dataclass

FPS = 30
SAMPLE_RATE = 48000

@dataclass(frozen=True)
class AuthoritativeTiming:
    requested_duration_seconds: int
    total_frames: int
    total_samples: int
    fps: int = FPS
    sample_rate: int = SAMPLE_RATE

    @property
    def duration_seconds(self) -> int:
        return self.requested_duration_seconds

def get_authoritative_timing(requested_duration_seconds: int) -> AuthoritativeTiming:
    req = int(requested_duration_seconds)
    if req <= 0:
        raise ValueError("requested_duration_seconds must be > 0")
    return AuthoritativeTiming(
        requested_duration_seconds=req,
        total_frames=req * FPS,
        total_samples=req * SAMPLE_RATE,
    )

def distribute_scene_durations(requested_duration_seconds: int, num_scenes: int) -> list[float]:
    """Equal split, no 30s default, sum == requested."""
    if num_scenes <= 0:
        return []
    base = requested_duration_seconds / num_scenes
    durations = [base] * num_scenes
    # fix float drift so sum == requested
    drift = requested_duration_seconds - sum(durations)
    durations[-1] += drift
    return durations
