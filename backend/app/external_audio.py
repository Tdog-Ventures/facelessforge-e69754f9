"""Preserve short external narration and fill only its trailing audio budget."""
from __future__ import annotations
import asyncio
from pathlib import Path
import tempfile

async def _measure(path: Path) -> float:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 30)
    except asyncio.TimeoutError:
        process.kill(); await process.communicate(); raise RuntimeError("External audio measurement timed out") from None
    if process.returncode:
        raise RuntimeError("External audio could not be measured")
    seconds = float(stdout.decode().strip())
    if not 0 < seconds < 3601:
        raise RuntimeError("External audio duration is invalid")
    return seconds

async def pad_audio_file(source: Path, destination: Path, samples: int, sample_rate: int = 48000) -> float:
    """Append silence without changing speech speed; retain exact sample count."""
    if samples <= 0 or sample_rate <= 0:
        raise ValueError("Positive audio sample budget required")
    source_seconds = await _measure(source)
    budget = samples / sample_rate
    if source_seconds >= budget:
        raise ValueError("Padding must not truncate or speed up narration")
    if budget - source_seconds > min(3.0, budget * 0.25):
        raise ValueError("Narration is too short; script review required")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source),
        "-af", f"aresample={sample_rate},apad=whole_len={samples},atrim=end_sample={samples},asetpts=PTS-STARTPTS",
        "-c:a", "pcm_s16le", str(destination),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(process.communicate(), 60)
    except asyncio.TimeoutError:
        process.kill(); await process.communicate(); raise RuntimeError("External audio padding timed out") from None
    if process.returncode:
        raise RuntimeError("External audio padding failed")
    actual = await _measure(destination)
    if abs(actual - budget) > 1 / sample_rate:
        raise RuntimeError("External audio padding did not match the selected budget")
    return source_seconds

async def pad_short_external_voiceover(asset: dict, project: dict) -> dict:
    from .timing import TimingContract
    from .render import _ensure_audio_local
    from .storage import get_storage
    if asset.get("mock") or asset.get("source") == "mock" or asset.get("provider") == "mock":
        raise ValueError("Only real narration can be prepared")
    timing = TimingContract.from_project(project)
    with tempfile.TemporaryDirectory(prefix="ff-external-audio-") as temporary:
        work = Path(temporary)
        source = await _ensure_audio_local(asset, work, "original")
        if source is None:
            raise RuntimeError("External narration audio is unavailable")
        measured = await _measure(source)
        if measured >= timing.narration_seconds:
            return dict(asset, duration=measured, duration_measured=True)
        padded = work / "padded.wav"
        original = await pad_audio_file(source, padded, timing.narration_samples, timing.sample_rate)
        key = f"audio/{project['id']}/{asset['id']}.padded.wav"
        saved = get_storage().save_file(padded, key, "audio/wav")
        return dict(asset, storage_key=saved.key, url=saved.url,
                    preview_url=saved.preview_path or saved.url, file_path=str(saved.file_path or ""),
                    duration=timing.narration_seconds, duration_measured=True,
                    original_duration_seconds=original, original_storage_key=asset.get("storage_key"),
                    original_preview_url=asset.get("preview_url"),
                    padding_silence_seconds=round(timing.narration_seconds-original, 6),
                    timing_preparation="trailing_silence_only")
