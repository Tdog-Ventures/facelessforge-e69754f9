"""Regression tests for the deterministic QA freeze check (frame-sampling gate).

ffmpeg's freezedetect false-positived on the full E2E render (job 3bb6a9d9):
freeze_start=0 with no freeze_end while direct frame diffs showed real motion
throughout (MAD vs frame0 reaching 115 after t=2.7s). The QA gate now samples
frames directly and only flags near-zero motion sustained past the budget.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import qa

FIXTURE = Path(__file__).parent / "fixtures" / "e2e_job_3bb6a9d9.mp4"


def _make_video(path: Path, spec: str) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *spec.split(), str(path)],
        check=True,
    )
    return path


def test_e2e_render_3bb6a9d9_passes_updated_gate():
    duration = qa.ffprobe_duration(FIXTURE)
    assert duration > 0
    result = qa.run_checks(FIXTURE, {}, duration)
    assert result["blocked"] == []
    freeze = next(c for c in result["checks"] if c["check"] == "freezedetect")
    assert freeze["status"] == "pass"
    assert freeze["method"] == "frame_sample_mad"


def test_genuinely_frozen_video_still_blocks(tmp_path: Path):
    video = _make_video(
        tmp_path / "static.mp4",
        "-f lavfi -i color=c=gray:s=320x180:r=30:d=8 -pix_fmt yuv420p",
    )
    duration = qa.ffprobe_duration(video)
    result = qa.run_checks(video, {}, duration)
    assert "freezedetect" in result["blocked"]


def test_short_intro_hold_is_not_flagged(tmp_path: Path):
    video = _make_video(
        tmp_path / "intro_hold.mp4",
        "-f lavfi -i color=c=gray:s=320x180:r=30:d=2.5 "
        "-f lavfi -i testsrc2=s=320x180:r=30:d=3.5 "
        "-filter_complex [0:v][1:v]concat=n=2:v=1:a=0[out] -map [out] -pix_fmt yuv420p",
    )
    assert qa.detect_freeze(video) is False


def test_frame_sampler_budget_is_strict():
    frozen = bytes(64 * 64)
    motion = bytes((i * 37) % 256 for i in range(64 * 64))
    # 4 fps sampling: 12 adds = exactly 3.0s (not > budget) -> no flag
    assert qa._freeze_from_frames([frozen] * 13) is False
    # 13 adds = 3.25s > 3.0s budget -> flag
    assert qa._freeze_from_frames([frozen] * 14) is True
    # sustained motion never accumulates
    assert qa._freeze_from_frames([motion, bytes(reversed(motion))] * 60) is False


def test_empty_or_single_frame_is_not_a_freeze():
    assert qa._freeze_from_frames([]) is False
    assert qa._freeze_from_frames([bytes(64 * 64)]) is False


def _clip(path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(path)],
        check=True,
    )
    return path


@pytest.mark.parametrize(
    "cmd_marker,check_name",
    [
        ("blackdetect", "blackdetect"),
        ("scale=64:64", "freezedetect"),
        ("silencedetect", "silencedetect"),
        ("loudnorm", "loudnorm"),
    ],
)
def test_check_blocks_when_underlying_call_raises(tmp_path, monkeypatch, cmd_marker, check_name):
    """A check that could not run must fail-closed: status=fail, in blocked,
    exception text preserved. Never report pass on an exception."""
    video = _clip(tmp_path / "clip.mp4")
    real_check_output = qa.subprocess.check_output

    def raiser(cmd, *args, **kwargs):
        if any(cmd_marker in str(c) for c in cmd):
            raise RuntimeError("forced ffmpeg failure")
        return real_check_output(cmd, *args, **kwargs)

    monkeypatch.setattr(qa.subprocess, "check_output", raiser)
    result = qa.run_checks(video, {}, qa.ffprobe_duration(video))

    check = next(c for c in result["checks"] if c["check"] == check_name)
    assert check["status"] == "fail", f"{check_name} must fail when it cannot run"
    assert "forced ffmpeg failure" in check.get("note", "")
    assert check_name in result["blocked"]


def _gap_clip(path: Path, video_s: float, audio_segments: list[tuple[str, float]]) -> Path:
    """Video with concatenated audio segments: ('tone'|'silence', seconds)."""
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=30:duration={video_s}"]
    labels = []
    for i, (kind, secs) in enumerate(audio_segments):
        src = "sine=frequency=440" if kind == "tone" else "anullsrc=r=44100:cl=stereo"
        cmd += ["-f", "lavfi", "-i", f"{src}:duration={secs}"]
        labels.append(f"[{i + 1}:a]")
    cmd += ["-filter_complex", "".join(labels) + f"concat=n={len(audio_segments)}:v=0:a=1[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path)]
    subprocess.run(cmd, check=True)
    return path


def test_silence_stitched_into_middle_blocks(tmp_path: Path):
    video = _gap_clip(tmp_path / "mid_gap.mp4", 9.0,
                      [("tone", 3.0), ("silence", 3.0), ("tone", 3.0)])
    result = qa.run_checks(video, {}, qa.ffprobe_duration(video))
    check = next(c for c in result["checks"] if c["check"] == "silencedetect")
    assert check["status"] == "fail"
    assert "silencedetect" in result["blocked"]


def test_only_edge_silence_still_passes(tmp_path: Path):
    video = _gap_clip(tmp_path / "edge_gap.mp4", 8.0,
                      [("silence", 2.0), ("tone", 4.0), ("silence", 2.0)])
    result = qa.run_checks(video, {}, qa.ffprobe_duration(video))
    check = next(c for c in result["checks"] if c["check"] == "silencedetect")
    assert check["status"] == "pass"
    assert "silencedetect" not in result["blocked"]
