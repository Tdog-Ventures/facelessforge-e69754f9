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

from backend.app import render, verify

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


def _uniform_tone_video(dest: Path, duration_s: float = 30.07) -> Path:
    """Video with uniform sine audio: every full 10s segment measures the
    same level. Only a sub-second trailing sliver differs (~-91dB)."""
    _run([ffmpeg, "-y",
          "-f", "lavfi", "-i",
          f"testsrc2=size={portrait_W}x{portrait_H}:rate=30:duration={duration_s}",
          "-f", "lavfi", "-i",
          f"sine=frequency=440:duration={duration_s}",
          "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-shortest", str(dest)])
    return dest


def _single_side_dark_top_video(dest: Path) -> Path:
    """Bright pattern with a black band ONLY at the top (dark-sky content).

    Incident 2026-10-11: a real render carried a ~200px near-black top band
    over bright content for ~2.5s (night-sky stock). check_f flagged it as
    letterboxing (OR logic) and failed the whole job. Bars from aspect
    conversion come in pairs — one dark edge + bright opposite edge is
    content and must pass.
    """
    src = dest.parent / "src_bright.mp4"
    _run([ffmpeg, "-y", "-f", "lavfi", "-i",
          f"testsrc2=size={portrait_W}x{portrait_H - 200}:rate=30:duration=11",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)])
    _run([ffmpeg, "-y", "-i", str(src),
          "-vf", f"pad={portrait_W}:{portrait_H}:0:200:black",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)])
    return dest


def _landscape_single_side_dark_video(dest: Path) -> Path:
    """1920x1080 with a black band ONLY on the left — content, must pass."""
    src = dest.parent / "src_land.mp4"
    _run([ffmpeg, "-y", "-f", "lavfi", "-i",
          "testsrc2=size=1720x1080:rate=30:duration=11",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)])
    _run([ffmpeg, "-y", "-i", str(src),
          "-vf", "pad=1920:1080:200:0:black",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)])
    return dest


def _landscape_symmetric_bars_video(dest: Path) -> Path:
    """1920x1080 with black bars left AND right — genuine pillarbox, must fail."""
    src = dest.parent / "src_land2.mp4"
    _run([ffmpeg, "-y", "-f", "lavfi", "-i",
          "testsrc2=size=1520x1080:rate=30:duration=11",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)])
    _run([ffmpeg, "-y", "-i", str(src),
          "-vf", "pad=1920:1080:200:0:black",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)])
    return dest


def test_check_f_passes_single_sided_dark_top_content(tmp_path: Path):
    video = _single_side_dark_top_video(tmp_path / "single_top.mp4")
    result = asyncio.run(verify.check_f_no_pillarbox(str(video), aspect="9:16"))
    assert result["frames_checked"] > 0
    assert result["passed"] is True, f"single-sided dark content flagged as bars: {result}"


def test_check_f_passes_single_sided_dark_left_content(tmp_path: Path):
    video = _landscape_single_side_dark_video(tmp_path / "single_left.mp4")
    result = asyncio.run(verify.check_f_no_pillarbox(str(video), aspect="16:9"))
    assert result["frames_checked"] > 0
    assert result["passed"] is True, f"single-sided dark content flagged as bars: {result}"


def test_check_f_still_rejects_symmetric_pillarbox_bars(tmp_path: Path):
    video = _landscape_symmetric_bars_video(tmp_path / "sym_bars.mp4")
    result = asyncio.run(verify.check_f_no_pillarbox(str(video), aspect="16:9"))
    assert result["frames_checked"] > 0
    assert result["passed"] is False, f"symmetric pillarbox not detected: {result}"


def test_check_c_ignores_trailing_subsecond_sliver(tmp_path: Path):
    """Incident 2026-10-11: a 30.07s render with healthy uniform audio
    failed check_c with vol_diff=72.4dB because the trailing 66ms sliver
    measures ~-91dB. Slivers under MIN_VOL_SEGMENT_SEC must be skipped."""
    video = _uniform_tone_video(tmp_path / "uniform_sliver.mp4")
    result = asyncio.run(verify.check_c_volume_consistency(str(video)))
    assert result["segments_checked"] == 3, f"sliver not skipped: {result}"
    assert result["passed"] is True, f"healthy mix failed: {result}"
    assert result["diff_db"] <= 12.0


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


class _ReachedScriptLookup(RuntimeError):
    """Sentinel proving render startup progressed past environment handling."""


class _FindOneCollection:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def find_one(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return self.value


class _StartupDB:
    render_jobs = _FindOneCollection({"aspect_ratio": "16:9", "burn_subtitles": True})
    projects = _FindOneCollection({"aspect_ratio": "16:9", "burn_subtitles": True})
    scripts = _FindOneCollection(error=_ReachedScriptLookup("script lookup reached"))


def test_run_render_can_read_environment_before_script_lookup(monkeypatch):
    """An inner ``import os`` must not shadow the module import in _run_render.

    The production incident failed on RENDER_BURN_SUBTITLES before the first
    script lookup. Reaching the sentinel demonstrates startup passed that line.
    """

    async def _no_op_set_job(*_args, **_kwargs):
        return None

    monkeypatch.setattr(render, "get_db", lambda: _StartupDB())
    monkeypatch.setattr(render, "_set_job", _no_op_set_job)
    monkeypatch.setenv("RENDER_BURN_SUBTITLES", "true")

    with pytest.raises(_ReachedScriptLookup, match="script lookup reached"):
        asyncio.run(render._run_render("job-id", "project-id"))


def _silence_tone_video(dest: Path, segments: list[tuple[str, float]]) -> Path:
    """Video with concatenated ('tone'|'silence', seconds) audio segments."""
    total = sum(seconds for _, seconds in segments)
    inputs: list[str] = []
    filters: list[str] = []
    for index, (kind, seconds) in enumerate(segments, start=1):
        if kind == "tone":
            inputs += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
        else:
            inputs += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={seconds}"]
        filters.append(f"[{index}:a]")
    graph = "".join(filters) + f"concat=n={len(segments)}:v=0:a=1[a]"
    _run([ffmpeg, "-y",
          "-f", "lavfi", "-i",
          f"testsrc2=size={portrait_W}x{portrait_H}:rate=30:duration={total}",
          *inputs,
          "-filter_complex", graph,
          "-map", "0:v", "-map", "[a]",
          "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-shortest", str(dest)])
    return dest


def test_check_b_allows_silence_within_intentional_intro(tmp_path: Path):
    """Incident 2026-10-20: a correct 30.07s render failed because the 2.5s
    silent title intro exceeded the fixed 5% skip (1.5s). The intro contract
    must allow that exact leading window without weakening interior checks."""
    video = _silence_tone_video(tmp_path / "intro_silence.mp4",
                                [("silence", 2.5), ("tone", 27.57)])
    result = asyncio.run(verify.check_b_silence_gaps(str(video), intro_seconds=2.5))
    assert result["gaps_total"] == 1
    assert result["gaps_offending"] == 0, result
    assert result["passed"] is True


def test_check_b_still_rejects_leading_silence_without_intro_contract(tmp_path: Path):
    video = _silence_tone_video(tmp_path / "no_intro_contract.mp4",
                                [("silence", 2.5), ("tone", 27.57)])
    result = asyncio.run(verify.check_b_silence_gaps(str(video)))
    assert result["gaps_offending"] == 1
    assert result["passed"] is False


def test_check_b_rejects_silence_that_runs_past_the_intro(tmp_path: Path):
    video = _silence_tone_video(tmp_path / "long_intro_silence.mp4",
                                [("silence", 4.0), ("tone", 26.07)])
    result = asyncio.run(verify.check_b_silence_gaps(str(video), intro_seconds=2.5))
    assert result["gaps_offending"] == 1
    assert result["passed"] is False


def test_check_b_rejects_interior_silence_even_with_intro(tmp_path: Path):
    video = _silence_tone_video(tmp_path / "interior_silence.mp4",
                                [("silence", 2.5), ("tone", 5.0),
                                 ("silence", 3.0), ("tone", 5.0)])
    result = asyncio.run(verify.check_b_silence_gaps(str(video), intro_seconds=2.5))
    assert result["gaps_offending"] == 1
    assert result["passed"] is False


def _fast_video_with_audio_segments(dest: Path, segments: list[tuple[str, float]],
                                    duration_s: float) -> Path:
    """Low-resolution/fast variant of _silence_tone_video for long renders.

    check_b only analyses audio, so a 320x240/10fps video track keeps the
    240-second render cheap to synthesise while exercising real silence
    detection timing.
    """
    inputs: list[str] = []
    filters: list[str] = []
    for index, (kind, seconds) in enumerate(segments, start=1):
        if kind == "tone":
            inputs += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
        else:
            inputs += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={seconds}"]
        filters.append(f"[{index}:a]")
    graph = "".join(filters) + f"concat=n={len(segments)}:v=0:a=1[a]"
    _run([ffmpeg, "-y",
          "-f", "lavfi", "-i",
          f"color=c=black:size=320x240:rate=10:duration={duration_s}",
          *inputs,
          "-filter_complex", graph,
          "-map", "0:v", "-map", "[a]",
          "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-shortest", str(dest)])
    return dest


def test_check_b_intro_contract_supersedes_legacy_5pct_window(tmp_path: Path):
    """CodeRabbit PR13 R1: the legacy 5% skip must not be combined with the
    intro contract. On a 240s render with a 2.5s intro, keeping max() makes
    the allowance 12s, so an interior 6-9s silence gap would wrongly pass.
    The intro boundary alone must apply when intro_seconds is supplied."""
    video = _fast_video_with_audio_segments(
        tmp_path / "long_interior_gap.mp4",
        [("tone", 6.0), ("silence", 3.0), ("tone", 231.0)],
        duration_s=240.0,
    )
    result = asyncio.run(verify.check_b_silence_gaps(str(video), intro_seconds=2.5))
    assert result["gaps_offending"] == 1, result
    assert result["passed"] is False


def test_check_b_leading_allowance_requires_leading_start(tmp_path: Path):
    """CodeRabbit PR13 R2: only silence that starts at the beginning of the
    render is leading silence. Brief audible audio (0-0.3s) followed by an
    in-window gap (0.3-2.4s) must not receive the intro allowance."""
    video = _fast_video_with_audio_segments(
        tmp_path / "post_audio_gap.mp4",
        [("tone", 0.3), ("silence", 2.1), ("tone", 27.6)],
        duration_s=30.0,
    )
    result = asyncio.run(verify.check_b_silence_gaps(str(video), intro_seconds=2.5))
    assert result["gaps_offending"] == 1, result
    assert result["passed"] is False


def test_verify_render_prefers_existing_local_artifact(tmp_path: Path, monkeypatch):
    """A slow object-storage edge must not stall verification: the local copy
    of the already-uploaded artifact is verified first."""
    video = _uniform_tone_video(tmp_path / "local_artifact.mp4", duration_s=6.0)

    async def _must_not_download(*_args, **_kwargs):
        raise AssertionError("remote download attempted despite local artifact")

    monkeypatch.setattr(verify, "_download_video", _must_not_download)
    result = asyncio.run(verify.verify_render(
        url="https://example.invalid/render.mp4",
        local_fallback_path=str(video),
        intro_seconds=2.5,
    ))
    assert "Failed to download" not in str(result.get("error", ""))
    assert result["checks"], result
    assert next(c for c in result["checks"] if c["check"] == "b")["passed"] is True
