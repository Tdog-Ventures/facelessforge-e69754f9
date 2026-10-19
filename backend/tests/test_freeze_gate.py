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
