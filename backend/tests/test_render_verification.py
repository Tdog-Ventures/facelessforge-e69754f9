"""Regression tests for the render constitution verification gate (check f).

Incident: a real 1080x1920 portrait render (video+audio present, captions
burned, loudnorm in spec) was marked FAILED by check_f `no_pillarbox`
because one sampled frame landed inside a fade-through-black transition:
the whole frame was near-black (full-frame luminance ~8/255), which the
check misread as a letterbox bar.

Rule locked in by these tests: a flat dark edge strip is only a bar when
the frame centre is actually bright. Full-frame fades / black leaders must
pass; genuine letterbox bars (bright centre, black top/bottom) must fail.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

from backend.app import verify

portrait_W, portrait_H = 1080, 1920
ffmpeg = shutil.which("ffmpeg") or "ffmpeg"


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _fade_tail_video(dest: Path) -> Path:
    """4s bright test pattern + 3s pure black tail (models a fade-to-black)."""
    bright = dest.parent / "bright.mp4"
    black = dest.parent / "black.mp4"
    _run([ffmpeg, "-y", "-f", "lavfi", "-i",
          f"testsrc2=size={portrait_W}x{portrait_H}:rate=30:duration=4",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(bright)])
    _run([ffmpeg, "-y", "-f", "lavfi", "-i",
          f"color=black:size={portrait_W}x{portrait_H}:rate=30:duration=3",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(black)])
    _run([ffmpeg, "-y", "-i", str(bright), "-i", str(black),
          "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)])
    return dest


def _letterboxed_video(dest: Path) -> Path:
    """Bright pattern with genuine 200px black bars top and bottom."""
    _run([ffmpeg, "-y", "-f", "lavfi", "-i",
          f"testsrc2=size={portrait_W}x{portrait_H - 400}:rate=30:duration=7",
          "-vf", f"pad={portrait_W}:{portrait_H}:0:200:black",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)])
    return dest


def test_check_f_ignores_full_frame_fade_to_black(tmp_path: Path):
    video = _fade_tail_video(tmp_path / "fade_tail.mp4")
    result = asyncio.run(verify.check_f_no_pillarbox(str(video), aspect="9:16"))
    assert result["frames_checked"] > 0
    assert result["passed"] is True, f"fade-to-black flagged as bars: {result}"


def test_check_f_still_rejects_genuine_letterbox_bars(tmp_path: Path):
    video = _letterboxed_video(tmp_path / "letterbox.mp4")
    result = asyncio.run(verify.check_f_no_pillarbox(str(video), aspect="9:16"))
    assert result["frames_checked"] > 0
    assert result["passed"] is False, f"genuine bars not detected: {result}"
    assert result["edge_failures"] >= 1


def test_configured_object_storage_endpoint_resolves():
    """Guards the Sep-30 incident root cause: STORAGE pointed at an
    unresolvable host (`minio`), failing every asset save. The configured
    object-storage endpoint hostname must resolve via DNS."""
    if (os.environ.get("STORAGE_MODE", "local") or "local").strip().lower() != "object":
        pytest.skip("STORAGE_MODE is not object")
    endpoint = (os.environ.get("STORAGE_ENDPOINT_URL")
                or os.environ.get("STORAGE_ENDPOINT") or "").strip()
    assert endpoint, "STORAGE endpoint not configured"
    host = urlparse(endpoint).hostname or ""
    assert host, f"no hostname in STORAGE endpoint {endpoint!r}"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        pytest.fail(f"object storage endpoint host does not resolve: {host} ({exc})")
    assert infos, f"no addresses for {host}"
