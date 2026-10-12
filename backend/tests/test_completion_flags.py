"""Completed-job flags must derive from verified pipeline evidence, not
from workdir paths checked after cleanup (which always read False)."""
import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.app import render

ffmpeg = shutil.which("ffmpeg") or "ffmpeg"


def _run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _clip_with_audio(dest: Path) -> Path:
    _run([ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2",
          "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
          "-shortest", str(dest)])
    return dest


def _clip_silent(dest: Path) -> Path:
    _run([ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)])
    return dest


def test_verification_check_reader():
    result = {"checks": [{"name": "captions_present", "passed": True},
                         {"name": "timing", "passed": False}]}
    assert render._verification_check_passed(result, "captions_present") is True
    assert render._verification_check_passed(result, "timing") is False
    assert render._verification_check_passed(result, "missing") is None
    assert render._verification_check_passed(None, "timing") is None
    assert render._verification_check_passed({}, "timing") is None


def test_audio_probe_detects_stream(tmp_path: Path):
    with_audio = asyncio.run(render._final_has_audio_stream(_clip_with_audio(tmp_path / "a.mp4")))
    silent = asyncio.run(render._final_has_audio_stream(_clip_silent(tmp_path / "s.mp4")))
    assert with_audio is True
    assert silent is False


def test_audio_probe_missing_file_returns_none(tmp_path: Path):
    assert asyncio.run(render._final_has_audio_stream(tmp_path / "nope.mp4")) is None
    assert asyncio.run(render._final_has_audio_stream(None)) is None


def test_completion_write_uses_derived_flags():
    """The completed-write call site must reference the derived variables,
    not post-cleanup path existence checks."""
    import inspect
    src = inspect.getsource(render._run_render)
    assert "final_has_audio if final_has_audio is not None else bool(audio_duration)" in src
    assert "captions_present if captions_present is not None else" in src
    assert "flags_derived_from" in src
    # No post-cleanup exists() check may remain on the completed path.
    tail = src.split('status="completed"')[1].split("Auto-post engine")[0]
    assert "audio_path and audio_path.exists()" not in tail
