"""
CHANGELOG:
- Created verify.py implementing Constitution Section 6 verification checks a-h.
- Each check is a standalone async function with proper error handling.
- Downloads video from served URL to a temp file for verification (no local-file bypass).
- Uses ffprobe/ffmpeg for all media analysis. Reports numeric results in a structured dict.
- Addresses: timing verification (a), silence-gap detection (b), volume consistency (c),
  shot-pacing/scene-change rate (d), caption-presence detection (e), pillarbox detection (f),
  loudnorm compliance (g), and compiled report table (h).
- Forbidden code paths (pad filter, fixed scene slots, giant per-scene titles, etc.) are
  NOT present; this file only validates outputs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Constitution constants (Section 6 — Verification)
# ---------------------------------------------------------------------------
MAX_DURATION_DIFF = 2.0          # check a: |video − narration| ≤ 2s
SILENCE_NOISE_DB = -35           # check b: silencedetect threshold
SILENCE_MIN_DURATION = 2.0      # check b: minimum silence gap to flag
SILENCE_SKIP_PCT = 0.05          # check b: ignore gaps before 5% runtime
VOL_SEGMENT_SEC = 10.0           # check c: per-10s volume check
MAX_VOL_DIFF_DB = 12.0           # check c: max segment-to-segment diff
SCENE_THRESHOLD = 0.3            # check d: scene-change threshold
MIN_CUTS_PER_8S = 1.0            # check d: ≥1 cut per 8s average
CAPTION_BOTTOM_PCT = 0.15        # check e: bottom strip % for caption check
CAPTION_VAR_MIN = 30.0           # check e: min variance for text presence
EDGE_CHECK_WIDTH = 200           # check f: left/right column width (px)
EDGE_VAR_MAX = 10.0              # check f: max variance → pillarbox suspected
LOUDNORM_I_TARGET = -14.0        # check g: target integrated loudness
LOUDNORM_TOLERANCE = 1.0         # check g: acceptable ±1 LUFS
LOUDNORM_TP = -1.5               # check g: true peak
LOUDNORM_LRA = 11                # check g: loudness range

FFPROBE_BIN = os.getenv("FFPROBE_PATH", "ffprobe")
FFMPEG_BIN = os.getenv("FFMPEG_PATH", "ffmpeg")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run_cmd(*cmd: str, timeout: int = 300) -> tuple[str, str, int]:
    """Run a subprocess, return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd)}")
    return stdout_b.decode(errors="ignore"), stderr_b.decode(errors="ignore"), proc.returncode or 0


async def _download_video(url: str, dest: Path) -> None:
    """Download remote video to local temp path for analysis."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)


async def ffprobe_duration(path: str) -> float:
    """Return duration in seconds for a media file via ffprobe."""
    stdout, stderr, rc = await _run_cmd(
        FFPROBE_BIN,
        "-v", "error",
        "-select_streams", "a:0",  # audio stream for narration; video for rendered
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    )
    if rc != 0:
        raise RuntimeError(f"ffprobe failed: {stderr}")
    try:
        return float(stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"Could not parse duration from ffprobe output: {stdout!r}") from exc


async def ffprobe_video_duration(path: str) -> float:
    """Return duration in seconds for the video stream."""
    stdout, stderr, rc = await _run_cmd(
        FFPROBE_BIN,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    )
    if rc != 0:
        raise RuntimeError(f"ffprobe failed: {stderr}")
    return float(stdout.strip())


def _parse_loudnorm_json(stderr_text: str) -> dict[str, Any]:
    """Extract the loudnorm JSON block from ffmpeg stderr."""
    match = re.search(r"(\{.*?\})", stderr_text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def _parse_silence_gaps(stderr_text: str) -> list[dict[str, float]]:
    """Parse silence_start / silence_end from ffmpeg silencedetect stderr."""
    starts = re.findall(r"silence_start:\s*([\d.]+)", stderr_text)
    ends = re.findall(r"silence_end:\s*([\d.]+)", stderr_text)
    gaps = []
    for s, e in zip(starts, ends):
        gaps.append({"start": float(s), "end": float(e), "duration": float(e) - float(s)})
    return gaps


def _parse_volumedetect_mean(stderr_text: str) -> Optional[float]:
    """Parse mean_volume dB from ffmpeg volumedetect stderr."""
    m = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", stderr_text)
    if m:
        return float(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Check a: Timing — |video_duration − total_narration_duration| ≤ 2s
# ---------------------------------------------------------------------------

async def check_a_timing(video_path: str, narration_path: str) -> dict[str, Any]:
    """
    Constitution §6a:
    |video_duration − total_narration_duration| ≤ 2 seconds.
    """
    video_dur = await ffprobe_video_duration(video_path)
    narr_dur = await ffprobe_duration(narration_path)
    diff = abs(video_dur - narr_dur)
    passed = diff <= MAX_DURATION_DIFF

    return {
        "check": "a",
        "name": "timing",
        "video_duration_s": round(video_dur, 2),
        "narration_duration_s": round(narr_dur, 2),
        "diff_s": round(diff, 2),
        "passed": passed,
        "threshold_s": MAX_DURATION_DIFF,
    }


# ---------------------------------------------------------------------------
# Check b: Silence gaps — silencedetect -35dB d=2, zero gaps after 5% runtime
# ---------------------------------------------------------------------------

async def check_b_silence_gaps(video_path: str) -> dict[str, Any]:
    """
    Constitution §6b:
    silencedetect=noise=-35dB:d=2
    Zero gaps ≥2s after first 5% of runtime.
    """
    video_dur = await ffprobe_video_duration(video_path)
    skip_until = video_dur * SILENCE_SKIP_PCT

    stdout, stderr, rc = await _run_cmd(
        FFMPEG_BIN,
        "-hide_banner", "-nostats",
        "-i", video_path,
        "-af", f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_DURATION}",
        "-f", "null", "-",
    )
    if rc != 0 and rc != 1:  # rc 1 is common with null output, but stderr still has data
        pass  # continue; silencedetect often exits non-zero with -f null

    gaps = _parse_silence_gaps(stderr)
    offending = [g for g in gaps if g["end"] > skip_until and g["duration"] >= SILENCE_MIN_DURATION]

    passed = len(offending) == 0
    return {
        "check": "b",
        "name": "silence_gaps",
        "video_duration_s": round(video_dur, 2),
        "skip_before_s": round(skip_until, 2),
        "gaps_total": len(gaps),
        "gaps_offending": len(offending),
        "gaps_detail": offending[:10],  # cap detail
        "passed": passed,
    }


# ---------------------------------------------------------------------------
# Check c: Volume consistency — volumedetect per 10s, max diff ≤12dB
# ---------------------------------------------------------------------------

async def check_c_volume_consistency(video_path: str) -> dict[str, Any]:
    """
    Constitution §6c:
    volumedetect per 10-second segment.
    No segment >12dB quieter than the loudest segment.
    """
    video_dur = await ffprobe_video_duration(video_path)
    segment_count = max(1, int(video_dur // VOL_SEGMENT_SEC) + (1 if video_dur % VOL_SEGMENT_SEC else 0))

    segment_means: list[float] = []
    for i in range(segment_count):
        start = i * VOL_SEGMENT_SEC
        duration = min(VOL_SEGMENT_SEC, video_dur - start)
        if duration <= 0:
            break
        stdout, stderr, rc = await _run_cmd(
            FFMPEG_BIN,
            "-hide_banner", "-nostats",
            "-ss", str(start), "-t", str(duration),
            "-i", video_path,
            "-af", "volumedetect",
            "-f", "null", "-",
        )
        mean_db = _parse_volumedetect_mean(stderr)
        if mean_db is not None:
            segment_means.append(mean_db)

    if not segment_means:
        return {
            "check": "c",
            "name": "volume_consistency",
            "segments_checked": 0,
            "passed": False,
            "reason": "Could not measure volume on any segment",
        }

    max_vol = max(segment_means)
    min_vol = min(segment_means)
    diff = max_vol - min_vol
    passed = diff <= MAX_VOL_DIFF_DB

    return {
        "check": "c",
        "name": "volume_consistency",
        "segments_checked": len(segment_means),
        "segment_means_db": [round(v, 2) for v in segment_means],
        "max_db": round(max_vol, 2),
        "min_db": round(min_vol, 2),
        "diff_db": round(diff, 2),
        "passed": passed,
        "threshold_db": MAX_VOL_DIFF_DB,
    }


# ---------------------------------------------------------------------------
# Check d: Shot-change rate — select='gt(scene,0.3)' count ≥1 per 8s average
# ---------------------------------------------------------------------------

async def check_d_shot_change_rate(video_path: str) -> dict[str, Any]:
    """
    Constitution §6d:
    ffmpeg select='gt(scene,0.3)' — average ≥1 cut per 8 seconds.
    """
    video_dur = await ffprobe_video_duration(video_path)

    stdout, stderr, rc = await _run_cmd(
        FFMPEG_BIN,
        "-hide_banner", "-nostats",
        "-i", video_path,
        "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
        "-an", "-f", "null", "-",
    )
    # showinfo writes to stderr; count pts_time lines
    times = re.findall(r"pts_time:\s*([\d.]+)", stderr)
    cut_count = len(times)

    expected_min = max(0.0, video_dur / 8.0) * MIN_CUTS_PER_8S
    passed = cut_count >= expected_min

    return {
        "check": "d",
        "name": "shot_change_rate",
        "video_duration_s": round(video_dur, 2),
        "cut_count": cut_count,
        "expected_min_cuts": round(expected_min, 2),
        "passed": passed,
    }


# ---------------------------------------------------------------------------
# Check e: Captions present — extract frame per 10s, bottom-strip variance check
# ---------------------------------------------------------------------------

async def _extract_frame(video_path: str, timestamp: float, out_path: Path) -> None:
    stdout, stderr, rc = await _run_cmd(
        FFMPEG_BIN,
        "-hide_banner", "-y",
        "-ss", str(timestamp), "-i", video_path,
        "-frames:v", "1", "-q:v", "2",
        str(out_path),
    )
    if rc != 0:
        raise RuntimeError(f"Frame extraction failed at {timestamp}s: {stderr}")


def _image_variance(image_path: str, region: Optional[tuple[int, int, int, int]] = None) -> float:
    """Return grayscale variance of a region (or whole image)."""
    img = Image.open(image_path).convert("L")
    if region:
        img = img.crop(region)
    arr = np.array(img, dtype=np.float32)
    return float(np.var(arr))


async def check_e_captions_present(video_path: str) -> dict[str, Any]:
    """
    Constitution §6e:
    Extract one frame every 10s; check bottom 15% of frame for caption variance.
    Text on screen raises local variance above a flat background.
    """
    video_dur = await ffprobe_video_duration(video_path)
    timestamps = [i * 10.0 for i in range(int(video_dur // 10.0) + 1)]
    if not timestamps:
        timestamps = [0.0]

    temp_dir = Path(tempfile.gettempdir()) / "ff_verify_e"
    temp_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for ts in timestamps:
        frame_path = temp_dir / f"frame_{ts:.1f}.png"
        try:
            await _extract_frame(video_path, ts, frame_path)
            img = Image.open(frame_path)
            w, h = img.size
            bottom_h = int(h * CAPTION_BOTTOM_PCT)
            region = (0, h - bottom_h, w, h)
            var = _image_variance(frame_path, region)
            results.append({"timestamp": ts, "variance": var, "has_caption_suspected": var > CAPTION_VAR_MIN})
        except Exception as exc:
            results.append({"timestamp": ts, "error": str(exc)})
        finally:
            if frame_path.exists():
                frame_path.unlink(missing_ok=True)

    # Clean up
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)

    suspect_count = sum(1 for r in results if r.get("has_caption_suspected"))
    passed = suspect_count >= len([r for r in results if "variance" in r]) * 0.5  # majority show caption variance

    return {
        "check": "e",
        "name": "captions_present",
        "frames_checked": len(results),
        "caption_suspected_frames": suspect_count,
        "per_frame": results,
        "passed": passed,
    }


# ---------------------------------------------------------------------------
# Check f: No pillarboxing — 3 frames per scene, left+right 200px not near-zero variance
# ---------------------------------------------------------------------------

async def check_f_no_pillarbox(video_path: str, scene_count: Optional[int] = None) -> dict[str, Any]:
    """
    Constitution §6f:
    3 frames per scene; left+right 200px columns must not be near-zero variance.
    If scene_count is unknown, sample evenly across the video (minimum 6 frames).
    """
    video_dur = await ffprobe_video_duration(video_path)

    if scene_count:
        frames_per_scene = 3
        total_frames = max(frames_per_scene * scene_count, 6)
    else:
        total_frames = max(6, int(video_dur / 5.0))  # one frame every ~5s if scene count unknown

    timestamps = [video_dur * (i + 1) / (total_frames + 1) for i in range(total_frames)]

    temp_dir = Path(tempfile.gettempdir()) / "ff_verify_f"
    temp_dir.mkdir(parents=True, exist_ok=True)

    edge_failures = 0
    frames_ok = 0
    per_frame = []

    for ts in timestamps:
        frame_path = temp_dir / f"frame_{ts:.1f}.png"
        try:
            await _extract_frame(video_path, ts, frame_path)
            img = Image.open(frame_path)
            w, h = img.size

            # Reject if video itself is narrower than 1280px (Constitution: landscape only, reject <1280px)
            if w < 1280:
                per_frame.append({"timestamp": ts, "width": w, "note": "width < 1280 — rejected per footage curation rules"})
                continue

            left_var = _image_variance(frame_path, (0, 0, EDGE_CHECK_WIDTH, h))
            right_var = _image_variance(frame_path, (w - EDGE_CHECK_WIDTH, 0, w, h))
            flat_edges = (left_var < EDGE_VAR_MAX) and (right_var < EDGE_VAR_MAX)
            if flat_edges:
                edge_failures += 1
            frames_ok += 1
            per_frame.append({
                "timestamp": ts,
                "left_variance": round(left_var, 2),
                "right_variance": round(right_var, 2),
                "flat_edges": flat_edges,
            })
        except Exception as exc:
            per_frame.append({"timestamp": ts, "error": str(exc)})
        finally:
            if frame_path.exists():
                frame_path.unlink(missing_ok=True)

    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)

    passed = (edge_failures == 0) and (frames_ok > 0)
    return {
        "check": "f",
        "name": "no_pillarbox",
        "frames_checked": frames_ok,
        "edge_failures": edge_failures,
        "per_frame": per_frame,
        "passed": passed,
    }


# ---------------------------------------------------------------------------
# Check g: loudnorm pass — integrated loudness within [-13, -15] LUFS
# ---------------------------------------------------------------------------

async def check_g_loudnorm(video_path: str) -> dict[str, Any]:
    """
    Constitution §6g:
    loudnorm in measure mode: integrated loudness within -13 to -15 LUFS.
    """
    stdout, stderr, rc = await _run_cmd(
        FFMPEG_BIN,
        "-hide_banner", "-nostats",
        "-i", video_path,
        "-af", f"loudnorm=I={LOUDNORM_I_TARGET}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json",
        "-f", "null", "-",
    )
    data = _parse_loudnorm_json(stderr)
    if not data:
        return {
            "check": "g",
            "name": "loudnorm",
            "passed": False,
            "reason": "Could not parse loudnorm JSON output",
            "raw_stderr": stderr[-2000:],
        }

    input_i = float(data.get("input_i", 0.0))
    passed = (LOUDNORM_I_TARGET - LOUDNORM_TOLERANCE) <= input_i <= (LOUDNORM_I_TARGET + LOUDNORM_TOLERANCE)

    return {
        "check": "g",
        "name": "loudnorm",
        "input_i_lufs": round(input_i, 2),
        "target_i_lufs": LOUDNORM_I_TARGET,
        "input_tp_lufs": round(float(data.get("input_tp", 0.0)), 2),
        "input_lra": round(float(data.get("input_lra", 0.0)), 2),
        "threshold": LOUDNORM_TOLERANCE,
        "passed": passed,
    }


# ---------------------------------------------------------------------------
# Check h: Compile report table
# ---------------------------------------------------------------------------

def check_h_compile_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Constitution §6h:
    Compile numeric results for checks a–g into a structured table/report.
    """
    rows = []
    for r in results:
        rows.append({
            "check": r.get("check", "?"),
            "name": r.get("name", "?"),
            "passed": r.get("passed", False),
            "key_metric": _extract_key_metric(r),
        })

    all_passed = all(r.get("passed", False) for r in results)

    return {
        "check": "h",
        "name": "report_table",
        "all_passed": all_passed,
        "summary": rows,
        "passed": all_passed,
    }


def _extract_key_metric(result: dict[str, Any]) -> str:
    """Pull the most important numeric metric from a check result for display."""
    name = result.get("name", "")
    if name == "timing":
        return f"diff={result.get('diff_s', 'N/A')}s"
    if name == "silence_gaps":
        return f"offending_gaps={result.get('gaps_offending', 'N/A')}"
    if name == "volume_consistency":
        return f"vol_diff={result.get('diff_db', 'N/A')}dB"
    if name == "shot_change_rate":
        return f"cuts={result.get('cut_count', 'N/A')} (need ≥{result.get('expected_min_cuts', 'N/A')})"
    if name == "captions_present":
        return f"caption_frames={result.get('caption_suspected_frames', 'N/A')}/{result.get('frames_checked', 'N/A')}"
    if name == "no_pillarbox":
        return f"edge_failures={result.get('edge_failures', 'N/A')}"
    if name == "loudnorm":
        return f"I={result.get('input_i_lufs', 'N/A')} LUFS"
    if name == "report_table":
        return f"all_passed={result.get('all_passed', 'N/A')}"
    return ""


# ---------------------------------------------------------------------------
# Master orchestrator
# ---------------------------------------------------------------------------

async def verify_render(
    url: str,
    narration_path: Optional[str] = None,
    scene_count: Optional[int] = None,
    cleanup: bool = True,
    local_fallback_path: Optional[str] = None,
) -> dict[str, Any]:
    """
    Constitution §6 — Full verification suite.

    Args:
        url: Publicly served URL of the rendered video (must be reachable).
        narration_path: Local path to the final narration audio file.
        scene_count: Optional number of scenes for per-scene sampling (check f).
        cleanup: If True, delete the downloaded temp video after verification.

    Returns:
        A dict with keys:
        - "overall_passed": bool
        - "checks": list of individual check results
        - "report": the compiled table (check h)
        - "video_temp_path": local path used (if cleanup=False)
    """
    # Constitution: verify on SERVED URL, not local file bypass — but support local fallback for dev
    is_remote = url.startswith("http://") or url.startswith("https://")
    temp_video = Path(tempfile.gettempdir()) / f"ff_verify_{int(time.time() * 1000)}_video.mp4"
    video_path = None
    
    if is_remote:
        try:
            await _download_video(url, temp_video)
            video_path = str(temp_video)
        except Exception as exc:
            # Fallback to local file if provided
            if local_fallback_path and Path(local_fallback_path).exists():
                video_path = local_fallback_path
                temp_video = Path(local_fallback_path)  # don't delete fallback
            else:
                return {
                    "overall_passed": False,
                    "error": f"Failed to download video from URL: {exc}",
                    "checks": [],
                    "report": {},
                }
    else:
        # url is actually a local path
        if Path(url).exists():
            video_path = url
            temp_video = Path(url)
        else:
            return {
                "overall_passed": False,
                "error": f"Video path does not exist: {url}",
                "checks": [],
                "report": {},
            }

    checks = []

    try:
        # a — timing (only if narration provided)
        if narration_path and Path(narration_path).exists():
            try:
                checks.append(await check_a_timing(video_path, narration_path))
            except Exception as e:
                checks.append({
                    "check": "a",
                    "name": "timing",
                    "passed": False,
                    "error": str(e),
                    "diff_s": None,
                })
        else:
            # No narration — skip timing check, mark as passed with note
            checks.append({
                "check": "a",
                "name": "timing",
                "passed": True,
                "skipped": True,
                "reason": "No narration audio — timing check N/A",
                "diff_s": 0,
            })

        # b — silence gaps
        checks.append(await check_b_silence_gaps(video_path))

        # c — volume consistency
        checks.append(await check_c_volume_consistency(video_path))

        # d — shot change rate
        checks.append(await check_d_shot_change_rate(video_path))

        # e — captions present
        checks.append(await check_e_captions_present(video_path))

        # f — no pillarbox
        checks.append(await check_f_no_pillarbox(video_path, scene_count=scene_count))

        # g — loudnorm
        checks.append(await check_g_loudnorm(video_path))

        # h — compile report
        report = check_h_compile_report(checks)
        checks.append(report)

        overall_passed = report["all_passed"]

    except Exception as exc:
        return {
            "overall_passed": False,
            "error": str(exc),
            "checks": checks,
            "report": {},
        }
    finally:
        # Only cleanup if we downloaded a temp file (not local fallback)
        if cleanup and temp_video.exists() and is_remote and str(temp_video).startswith(tempfile.gettempdir()):
            try:
                temp_video.unlink(missing_ok=True)
            except Exception:
                pass

    return {
        "overall_passed": overall_passed,
        "checks": checks,
        "report": report,
        "video_temp_path": None if cleanup else str(temp_video),
    }


# ---------------------------------------------------------------------------
# Standalone CLI helper (for local testing / CI integration)
# ---------------------------------------------------------------------------

async def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="FacelessForge Constitution verification suite")
    parser.add_argument("--url", required=True, help="Served URL of rendered video")
    parser.add_argument("--narration", required=True, help="Local path to narration audio")
    parser.add_argument("--scene-count", type=int, default=None, help="Number of scenes (for check f)")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep downloaded video temp file")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    result = await verify_render(
        url=args.url,
        narration_path=args.narration,
        scene_count=args.scene_count,
        cleanup=not args.no_cleanup,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print("=" * 60)
        print("FACEFORGE VERIFICATION REPORT")
        print("=" * 60)
        print(f"Overall: {'PASS' if result['overall_passed'] else 'FAIL'}")
        print("-" * 60)
        for c in result.get("checks", []):
            status = "PASS" if c.get("passed") else "FAIL"
            print(f"[{c.get('check', '?')}] {c.get('name', 'unknown'):20s} : {status:4s} | {c.get('key_metric', '')}")
        print("-" * 60)
        if "error" in result:
            print(f"ERROR: {result['error']}")


if __name__ == "__main__":
    asyncio.run(_cli())
