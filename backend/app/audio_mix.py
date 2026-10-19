"""Fitted-narration artifact + genuine sidechain-ducking mixer.

Pipeline order enforced by render.py:
  source VO -> normalize -> MEASURE -> fit (atempo once) -> materialise exact
  sample budget -> transcribe THIS file -> SRT/ASS -> intro delay ->
  mux WITHOUT a second atempo.

Ducking topology: fitted narration is split into audible mix + sidechain
control; music gets gain + high-pass, then sidechaincompress, then amix.
Fixed attenuation is never labelled ducking.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import shutil
import wave
from pathlib import Path
from typing import Optional

logger = logging.getLogger("facelessforge.audio_mix")

FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE_BIN = shutil.which("ffprobe") or "ffprobe"

MAX_TEMPO_DELTA = 0.05
MUSIC_GAIN_DB_DEFAULT = -14.0
MUSIC_GAIN_DB_MIN = -30.0
MUSIC_GAIN_DB_MAX = -6.0
MUSIC_HIGHPASS_HZ = 120.0
# sidechaincompress tuning: fast attack so the first word is never buried,
# slow release so recovery has no pumping; deep ratio for >=8 dB dips.
SC_THRESHOLD = 0.02
SC_RATIO = 10.0
SC_ATTACK_MS = 300.0
SC_RELEASE_MS = 900.0


async def _run_cmd(*cmd: str, timeout: int = 300) -> tuple[str, str, int]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"ffmpeg command timed out: {cmd[0]}")
    return out.decode(errors="ignore"), err.decode(errors="ignore"), proc.returncode or 0


async def probe_duration(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    _out, err, rc = await _run_cmd(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path))
    del err
    try:
        return float(_out.strip()) if rc == 0 else None
    except ValueError:
        return None


def probe_audio(path: Path) -> dict:
    """Synchronous stream/duration/peak report for a finished file."""
    import json
    import subprocess
    streams = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60)
    info: dict = {"sample_rate": 0, "channels": 0, "duration": 0.0, "peak_db": 0.0}
    try:
        for st in json.loads(streams.stdout or "{}").get("streams", []):
            if st.get("codec_type") == "audio":
                info["sample_rate"] = int(st.get("sample_rate") or 0)
                info["channels"] = int(st.get("channels") or 0)
    except Exception:  # noqa: BLE001
        pass
    fmt = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=60)
    try:
        info["duration"] = float((fmt.stdout or "").strip())
    except ValueError:
        pass
    peak = subprocess.run(
        [FFMPEG_BIN, "-hide_banner", "-nostats", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120)
    m = re.search(r"max_volume:\s*([-\d.]+)\s*dB", peak.stderr or "")
    try:
        info["peak_db"] = float(m.group(1)) if m else 0.0
    except ValueError:
        pass
    return info


def segment_loudness(path: Path, start: float, end: float) -> float:
    """Mean volume dB of one time window (volumedetect)."""
    import subprocess
    dur = max(0.1, end - start)
    proc = subprocess.run(
        [FFMPEG_BIN, "-hide_banner", "-nostats", "-ss", str(start), "-t", str(dur),
         "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120)
    m = re.search(r"mean_volume:\s*([-\d.]+|n/a)\s*dB", proc.stderr or "")
    if not m or m.group(1) == "n/a":
        return -91.0
    return float(m.group(1))


async def fit_narration_to_budget(src: Path, *, budget_seconds: float,
                                  sample_rate: int, work_dir: Path) -> Path:
    """Materialise narration at EXACTLY budget_seconds.

    Single atempo when drift is within tolerance; resample; trim/pad only
    rounding residue to the exact sample count. Drift above tolerance raises
    (caller requests regeneration) — never stretches speech unnaturally.
    """
    if budget_seconds <= 0:
        raise RuntimeError(" narration budget must be positive")
    measured = await probe_duration(src)
    if measured is None or measured <= 0:
        raise RuntimeError(f"cannot measure narration duration: {src.name}")
    ratio = measured / budget_seconds
    if abs(ratio - 1.0) > MAX_TEMPO_DELTA:
        raise RuntimeError(
            f"narration drift {ratio:.4f} exceeds tolerance "
            f"(measured={measured:.3f}s budget={budget_seconds:.3f}s)")
    target_frames = int(round(budget_seconds * sample_rate))
    fitted = work_dir / f"{src.stem}_fitted.wav"
    _out, err, rc = await _run_cmd(
        FFMPEG_BIN, "-y", "-i", str(src),
        "-af", f"atempo={ratio:.8f},aresample={sample_rate}:async=1",
        "-ar", str(sample_rate), "-ac", "2", "-c:a", "pcm_s16le",
        str(fitted))
    if rc != 0 or not fitted.exists():
        raise RuntimeError(f"narration fit failed: {err[-300:]}")
    # Trim/pad rounding residue to the exact sample budget.
    with wave.open(str(fitted), "rb") as w:
        params, frames, data = w.getparams(), w.getnframes(), w.readframes(w.getnframes())
    if frames != target_frames:
        width, nch = params.sampwidth, params.nchannels
        frame_bytes = width * nch
        if frames > target_frames:
            data = data[:target_frames * frame_bytes]
        else:
            data = data + b"\x00" * ((target_frames - frames) * frame_bytes)
        with wave.open(str(fitted), "wb") as w:
            w.setparams(params)
            w.writeframes(data)
    verify = await probe_duration(fitted)
    if verify is None or abs(verify - budget_seconds) > 0.02:
        raise RuntimeError(f"fitted narration missed budget: {verify}")
    logger.info("fitted narration %s -> %.3fs (%d samples)", src.name, budget_seconds, target_frames)
    return fitted


def narration_mux_filter(*, fitted: bool, tempo_ratio: float) -> str:
    """Narration leg of the final mux. Fitted audio passes through untouched."""
    if fitted:
        return "anull"
    return f"atempo={tempo_ratio:.8f}"


async def duck_music_stem(*, music: Path, sidechain: Path, speech_offset: float,
                          music_gain_db: float = MUSIC_GAIN_DB_DEFAULT,
                          work_dir: Path) -> Path:
    """Compress the music stem wherever speech is present. Returns the stem."""
    gain = min(MUSIC_GAIN_DB_MAX, max(MUSIC_GAIN_DB_MIN, float(music_gain_db)))
    out = work_dir / "music_ducked.wav"
    delay_ms = int(round(max(0.0, speech_offset) * 1000))
    _out, err, rc = await _run_cmd(
        FFMPEG_BIN, "-y", "-i", str(music), "-i", str(sidechain),
        "-filter_complex",
        f"[0:a]volume={gain}dB,highpass=f={MUSIC_HIGHPASS_HZ:.0f}[m];"
        f"[1:a]adelay={delay_ms}:all=1,apad[sc];"
        f"[m][sc]sidechaincompress=threshold={SC_THRESHOLD}:ratio={SC_RATIO}:"
        f"attack={SC_ATTACK_MS}:release={SC_RELEASE_MS}:makeup=1[d]",
        "-map", "[d]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
        str(out))
    if rc != 0 or not out.exists():
        raise RuntimeError(f"ducking failed: {err[-300:]}")
    return out


async def place_narration(narration: Path, *, intro_seconds: float,
                          final_seconds: float, work_dir: Path) -> Path:
    """Place fitted narration on the timeline: intro delay + out-fade.

    No tempo operations — the input must already match its budget (see
    fit_narration_to_budget). Trims/pads only rounding residue.
    """
    intro_ms = int(round(max(0.0, intro_seconds) * 1000))
    out = work_dir / "narration_placed.wav"
    _out, err, rc = await _run_cmd(
        FFMPEG_BIN, "-y", "-i", str(narration),
        "-af",
        f"aresample=48000,adelay={intro_ms}:all=1,"
        f"afade=t=out:st={max(0.0, final_seconds - 2.0):.3f}:d=2,"
        f"apad,atrim=duration={final_seconds:.6f},"
        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
        str(out))
    if rc != 0 or not out.exists():
        raise RuntimeError(f"narration placement failed: {err[-300:]}")
    return out


async def loop_music_only(music: Path, *, final_seconds: float,
                          music_gain_db: float = MUSIC_GAIN_DB_DEFAULT,
                          work_dir: Path) -> Path:
    """Loop a selected track across the timeline (no narration to duck under)."""
    out = work_dir / "music_only.wav"
    gain = min(MUSIC_GAIN_DB_MAX, max(MUSIC_GAIN_DB_MIN, float(music_gain_db)))
    _out, err, rc = await _run_cmd(
        FFMPEG_BIN, "-y",
        "-stream_loop", "-1", "-i", str(music),
        "-af",
        f"atrim=duration={final_seconds:.6f},"
        f"volume={gain}dB,highpass=f={MUSIC_HIGHPASS_HZ:.0f},"
        f"afade=t=out:st={max(0.0, final_seconds - 2.0):.3f}:d=2,"
        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
        str(out))
    if rc != 0 or not out.exists():
        raise RuntimeError(f"music loop failed: {err[-300:]}")
    return out


async def mix_without_ducking(*, narration: Path, music: Path, intro_seconds: float,
                              final_seconds: float,
                              music_gain_db: float = MUSIC_GAIN_DB_DEFAULT,
                              work_dir: Path) -> Path:
    """Fixed-level mix for tenants that explicitly disable ducking."""
    intro_ms = int(round(max(0.0, intro_seconds) * 1000))
    out = work_dir / "final_mix_flat.wav"
    gain = min(MUSIC_GAIN_DB_MAX, max(MUSIC_GAIN_DB_MIN, float(music_gain_db)))
    _out, err, rc = await _run_cmd(
        FFMPEG_BIN, "-y", "-i", str(narration),
        "-stream_loop", "-1", "-i", str(music),
        "-filter_complex",
        f"[0:a]aresample=48000,adelay={intro_ms}:all=1,apad,atrim=duration={final_seconds:.6f}[vox];"
        f"[1:a]atrim=duration={final_seconds:.6f},"
        f"volume={gain}dB,highpass=f={MUSIC_HIGHPASS_HZ:.0f},"
        f"afade=t=out:st={max(0.0, final_seconds - 2.0):.3f}:d=2[m];"
        f"[vox][m]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
        f"atrim=duration={final_seconds:.6f},aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[out]",
        "-map", "[out]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
        str(out))
    if rc != 0 or not out.exists():
        raise RuntimeError(f"flat mix failed: {err[-300:]}")
    return out


async def mix_with_ducking(*, narration: Path, music: Path, intro_seconds: float,
                           final_seconds: float,
                           music_gain_db: float = MUSIC_GAIN_DB_DEFAULT,
                           work_dir: Path) -> Path:
    """Full mix: intro-delayed narration + ducked music, exact duration."""
    intro_ms = int(round(max(0.0, intro_seconds) * 1000))
    out = work_dir / "final_mix.wav"
    gain = min(MUSIC_GAIN_DB_MAX, max(MUSIC_GAIN_DB_MIN, float(music_gain_db)))
    _out, err, rc = await _run_cmd(
        FFMPEG_BIN, "-y", "-i", str(narration),
        "-stream_loop", "-1", "-i", str(music),
        "-filter_complex",
        f"[0:a]aresample=48000,adelay={intro_ms}:all=1,apad,atrim=duration={final_seconds:.6f},"
        f"asplit=2[vox][sc];"
        f"[1:a]atrim=duration={final_seconds:.6f},"
        f"volume={gain}dB,highpass=f={MUSIC_HIGHPASS_HZ:.0f},"
        f"afade=t=out:st={max(0.0, final_seconds - 2.0):.3f}:d=2[m];"
        f"[m][sc]sidechaincompress=threshold={SC_THRESHOLD}:ratio={SC_RATIO}:"
        f"attack={SC_ATTACK_MS}:release={SC_RELEASE_MS}:makeup=1[dm];"
        f"[vox][dm]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
        f"atrim=duration={final_seconds:.6f},aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[out]",
        "-map", "[out]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
        str(out))
    if rc != 0 or not out.exists():
        raise RuntimeError(f"mix failed: {err[-300:]}")
    return out
