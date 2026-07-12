"""Real ffmpeg render queue.

Produces a 1920x1080 30fps H.264 + AAC MP4 from:
  • selected thumbnail   (intro frame, 1.5s)
  • scene visual assets  (one clip per scene at scene duration)
  • selected voiceover   (full-script preferred; else concat of per-scene VOs)

Mock-compatible:
  • Mock thumbnails are SVG → fall back to a Pillow-rendered PNG
  • Scene stock URLs that 404 / time out → retry broader stock searches, else the render fails
  • Missing voiceover → silent track

Security:
  • All ffmpeg args are constructed server-side from validated DB rows.
  • No raw user args ever reach ffmpeg.
  • All paths sanitised to the project's render workdir.
  • One concurrent render per project; explicit cancellation supported.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont

from .db import get_db
from .storage import get_storage
from .stock import search_stock
from .adengine import send_to_adengine

logger = logging.getLogger("facelessforge.render")

STATIC_RENDERS = Path(__file__).parent.parent / "static" / "renders"
STATIC_RENDERS.mkdir(parents=True, exist_ok=True)


def _resolve_ffmpeg_bin() -> str:
    """Resolve ffmpeg binary. Prefer system ffmpeg if present (apt), else fall
    back to the static binary shipped by imageio-ffmpeg (pip), so renders survive
    a fresh container without apt packages."""
    sys_bin = shutil.which("ffmpeg")
    if sys_bin:
        return sys_bin
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return "ffmpeg"  # last resort — will surface a clear error in render job


def _resolve_ffprobe_bin() -> Optional[str]:
    """ffprobe is optional (only used for duration probe). System apt ships it;
    imageio-ffmpeg does not. If absent, we silently skip the probe step."""
    return shutil.which("ffprobe")


FFMPEG_BIN = _resolve_ffmpeg_bin()
FFPROBE_BIN = _resolve_ffprobe_bin()

WIDTH = 1920
HEIGHT = 1080
FPS = 30
HARD_TIMEOUT_SECONDS = int(os.environ.get("RENDER_TIMEOUT_SECONDS", "600"))
MAX_VIDEO_DOWNLOAD_BYTES = 60 * 1024 * 1024  # 60MB per asset cap
INTRO_DURATION_SECONDS = 1.5

# Track active asyncio tasks per project for cancellation
_ACTIVE_TASKS: dict[str, asyncio.Task] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_name(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", s or "")[:80]


# ============================ VALIDATION ============================

def validate_prerequisites(project: dict, script: dict | None,
                           scenes: list[dict], metadata: dict | None,
                           assets: list[dict]) -> dict:
    """Returns a checklist + ok flag the UI can render."""
    issues: list[str] = []
    checklist: list[dict] = []

    def _add(key: str, label: str, ok: bool, hint: str = ""):
        checklist.append({"key": key, "label": label, "ok": bool(ok), "hint": hint})
        if not ok:
            issues.append(label)

    _add("script", "Script generated", bool(script and (script.get("full_script") or "").strip()),
         "Generate a script first.")
    _add("scenes", "Scenes generated", bool(scenes),
         "Generate the scene plan.")
    _add("metadata", "Metadata generated", bool(metadata),
         "Generate metadata package.")

    sel_thumb = next((a for a in assets
                      if a.get("asset_type") == "generated_thumbnail"
                      and a.get("id") == project.get("selected_thumbnail_asset_id")), None)
    _add("thumbnail", "Selected thumbnail", bool(sel_thumb),
         "Pick a thumbnail in the Thumbnails tab.")

    full_voice = next((a for a in assets
                       if a.get("asset_type") == "voiceover_audio"
                       and not a.get("scene_id")
                       and a.get("id") == project.get("selected_voiceover_asset_id")), None)
    scene_voices = [a for a in assets if a.get("asset_type") == "voiceover_audio"
                    and a.get("scene_id") and a.get("status") != "rejected"]
    has_voice = bool(full_voice) or len(scene_voices) > 0
    _add("voiceover", "Voiceover ready (full or per-scene)", has_voice,
         "Generate a full-script voiceover, or scene voiceovers.")

    # Scene visual coverage — soft warning only (empty scenes retry a broader
    # stock search at render time; the render fails if nothing is found)
    scene_assets = [a for a in assets if a.get("asset_type") in ("stock_image", "stock_video") and a.get("scene_id")]
    covered_ids = {a["scene_id"] for a in scene_assets}
    coverage = (len(covered_ids) / max(1, len(scenes))) if scenes else 0
    _add("scene_assets", "Scene visuals attached",
         coverage >= 0.5,
         f"{len(covered_ids)}/{len(scenes)} scenes have stock visuals. "
         "Empty scenes retry a broader stock search at render; the render fails if none is found.")

    return {
        "ok": all(c["ok"] for c in checklist if c["key"] != "scene_assets"),
        "issues": issues,
        "checklist": checklist,
        "scene_coverage": round(coverage, 2),
        "selected_thumbnail_asset_id": (sel_thumb or {}).get("id"),
        "selected_voiceover_asset_id": (full_voice or {}).get("id"),
        "scene_voiceover_count": len(scene_voices),
    }


# ============================ ASSET RESOLUTION ============================

def _try_load_font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _pil_caption_frame(out_path: Path, *, title: str, subtitle: str = "",
                       footer: str = "", palette: tuple[str, str] = ("#0A0A0A", "#00E5FF"),
                       size: tuple[int, int] = (WIDTH, HEIGHT)) -> Path:
    """Branded fallback frame — used when an image asset is unusable."""
    bg, accent = palette
    img = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(img)
    # subtle grid
    for x in range(0, size[0], 64):
        draw.line([(x, 0), (x, size[1])], fill=(20, 20, 22), width=1)
    for y in range(0, size[1], 64):
        draw.line([(0, y), (size[0], y)], fill=(20, 20, 22), width=1)
    # accent bar
    draw.rectangle([(0, size[1] - 14), (size[0], size[1])], fill=accent)
    # title
    title_font = _try_load_font(96)
    sub_font = _try_load_font(40)
    foot_font = _try_load_font(28)
    margin = 100
    max_text_width = size[0] - margin * 2

    def _text_width(text: str, font) -> float:
        # crude width check
        try:
            return draw.textlength(text, font=font)
        except Exception:
            return len(text) * 40

    def _wrap(text: str, font) -> list[str]:
        words = (text or "").split()
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if _text_width(test, font) > max_text_width and cur:
                lines.append(cur)
                cur = w
            else:
                cur = test
        if cur:
            lines.append(cur)
        return lines

    # Wrap both title and subtitle, centre the whole block horizontally + vertically
    title_lines = _wrap(title, title_font)[:4]
    sub_lines = _wrap(subtitle[:200], sub_font)[:3] if subtitle else []
    block_h = len(title_lines) * 110 + ((30 + len(sub_lines) * 54) if sub_lines else 0)
    y = max(margin, int((size[1] - block_h) / 2))
    for line in title_lines:
        draw.text((int((size[0] - _text_width(line, title_font)) / 2), y),
                  line, font=title_font, fill="#FFFFFF")
        y += 110
    if sub_lines:
        y += 30
        for line in sub_lines:
            draw.text((int((size[0] - _text_width(line, sub_font)) / 2), y),
                      line, font=sub_font, fill="#A1A1AA")
            y += 54
    if footer:
        draw.text((margin, size[1] - 90), footer[:140], font=foot_font, fill=accent)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    return out_path


async def _download_to(url: str, out_path: Path, *, max_bytes: int) -> bool:
    """Best-effort download. Returns True on success, False on any failure."""
    try:
        timeout = httpx.Timeout(20.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return False
                ct = resp.headers.get("content-type", "")
                # Accept image/video/audio and generic binary streams
                if not (
                    ct.startswith("image/")
                    or ct.startswith("video/")
                    or ct.startswith("audio/")
                    or ct.startswith("application/octet-stream")
                ):
                    logger.warning("_download_to rejected content-type '%s' for %s", ct, url)
                    return False
                total = 0
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            f.close()
                            try:
                                out_path.unlink(missing_ok=True)
                            except Exception:
                                pass
                            return False
                        f.write(chunk)
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception as e:  # noqa: BLE001
        logger.info("Download failed for %s: %s", url, e)
        return False


def _local_path_for_asset(asset: Optional[dict]) -> Optional[Path]:
    """If the asset already has a local file_path that exists, return it."""
    if not asset:
        return None
    fp = asset.get("file_path")
    if fp:
        p = Path(fp)
        if p.exists() and p.is_file():
            return p
    return None


async def _resolve_thumbnail(asset: Optional[dict], project: dict, work_dir: Path) -> Path:
    asset = asset or {}
    out = work_dir / "intro.png"
    local = _local_path_for_asset(asset)
    if local and local.suffix.lower() in (".png", ".jpg", ".jpeg"):
        # Re-encode to consistent size via PIL
        try:
            img = Image.open(local).convert("RGB")
            img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
            img.save(out, format="PNG")
            return out
        except Exception as e:
            logger.warning("Thumbnail PIL load failed (%s) — using caption frame", e)
    elif asset.get("download_url") or asset.get("preview_url"):
        url = asset.get("download_url") or asset.get("preview_url")
        tmp = work_dir / "intro_dl.bin"
        ok = await _download_to(url, tmp, max_bytes=20 * 1024 * 1024)
        if ok:
            try:
                img = Image.open(tmp).convert("RGB")
                img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
                img.save(out, format="PNG")
                tmp.unlink(missing_ok=True)
                return out
            except Exception:
                tmp.unlink(missing_ok=True)
    # Fallback caption frame
    title = (asset.get("brief_snapshot") or {}).get("thumbnail_title_text") or project.get("name") or "FacelessForge"
    return _pil_caption_frame(
        out, title=title.upper(),
        subtitle=project.get("topic", "")[:120],
        footer="FacelessForge · Generated render",
    )


def _broader_scene_queries(scene: dict, project: dict) -> list[str]:
    """Progressively broader stock queries for a scene with no usable footage."""
    queries: list[str] = []
    for t in (scene.get("search_terms") or []):
        t = (t or "").strip()
        if t and t not in queries:
            queries.append(t)
    topic = " ".join((project.get("topic") or "").split()[:6]).strip()
    if topic and topic not in queries:
        queries.append(topic)
    niche = (project.get("niche") or "").strip()
    if niche and niche not in queries:
        queries.append(niche)
    return queries


async def _resolve_scene_visual(scene: dict, attached_assets: list[dict],
                                 project: dict, work_dir: Path, idx: int) -> tuple[Path, str]:
    """Return (local_path, kind) where kind is 'image' or 'video'.

    Tries the attached stock assets first, then retries the stock API with
    progressively broader keywords. A scene that still has no footage raises
    so the render fails loudly instead of shipping a filler frame."""
    # Prefer first attached stock asset
    candidates = [a for a in attached_assets if a.get("scene_id") == scene.get("id")
                  and a.get("asset_type") in ("stock_image", "stock_video")]
    out_dir = work_dir / "scenes"
    out_dir.mkdir(parents=True, exist_ok=True)
    scene_no = scene.get("scene_number", idx + 1)

    for a in candidates:
        local = _local_path_for_asset(a)
        ext = (Path(local).suffix.lower() if local else "")
        # Try local first
        if local and ext in (".png", ".jpg", ".jpeg"):
            logger.info("scene %s: using local image %s", scene_no, local)
            return (local, "image")
        if local and ext in (".mp4", ".mov", ".webm"):
            logger.info("scene %s: using local video %s", scene_no, local)
            return (local, "video")
        is_video = a.get("asset_type") == "stock_video"
        # A video asset needs an actual video file — a poster/preview image
        # frozen for the scene duration is not footage.
        url = (a.get("download_url") if is_video
               else a.get("download_url") or a.get("preview_url") or a.get("source_url"))
        if not url:
            logger.warning("scene %s: attached %s %s has no usable download_url",
                           scene_no, a.get("asset_type"), a.get("external_id"))
            continue
        suffix = ".mp4" if is_video else ".jpg"
        target = out_dir / f"scene_{idx:03d}_src{suffix}"
        ok = await _download_to(url, target, max_bytes=MAX_VIDEO_DOWNLOAD_BYTES)
        logger.info("scene %s: download %s -> %s", scene_no, url[:120], "ok" if ok else "FAILED")
        if ok:
            return (target, "video" if is_video else "image")

    # No usable attached asset — retry with progressively broader keywords.
    for query in _broader_scene_queries(scene, project):
        try:
            result = await search_stock(query, "videos", per_page=8)
        except Exception as e:  # noqa: BLE001
            logger.warning("scene %s: retry stock search '%s' failed: %s", scene_no, query, e)
            continue
        items = result.get("results") or []
        logger.info("scene %s: retry stock search '%s' -> %d results", scene_no, query, len(items))
        for item in items:
            url = item.get("download_url")
            if not url:
                continue
            is_video = item.get("media_type") == "stock_video"
            suffix = ".mp4" if is_video else ".jpg"
            target = out_dir / f"scene_{idx:03d}_src{suffix}"
            ok = await _download_to(url, target, max_bytes=MAX_VIDEO_DOWNLOAD_BYTES)
            logger.info("scene %s: download %s -> %s", scene_no, url[:120], "ok" if ok else "FAILED")
            if ok:
                return (target, "video" if is_video else "image")

    raise RuntimeError(f"scene {scene_no}: no usable stock footage found after keyword retries")


async def _resolve_single_audio(asset: dict, work_dir: Path, prefix: str) -> Optional[Path]:
    """Download a voiceover asset to a local path, normalising to WAV for ffmpeg."""
    local = _local_path_for_asset(asset)
    if local and local.exists() and local.stat().st_size > 0:
        logger.info("_resolve_audio: using local file %s", local)
        return local
    url = asset.get("preview_url") or asset.get("download_url") or asset.get("url")
    logger.info("_resolve_audio: downloading from %s", url[:80] if url else None)
    if not url:
        return None
    ext = ".wav" if asset.get("mock") else ".mp3"
    tmp = work_dir / f"{prefix}_dl{ext}"
    ok = await _download_to(url, tmp, max_bytes=200 * 1024 * 1024)
    if not ok or not tmp.exists() or tmp.stat().st_size == 0:
        logger.warning("_resolve_audio: download failed for %s", url)
        return None
    # Normalise to WAV 48kHz stereo so downstream mixing is reliable
    norm = work_dir / f"{prefix}_norm.wav"
    cmd = [
        FFMPEG_BIN, "-y", "-i", str(tmp),
        "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
        str(norm),
    ]
    ok, err = await _run_ffmpeg(cmd, timeout=120)
    if ok and norm.exists() and norm.stat().st_size > 0:
        return norm
    logger.warning("_resolve_audio: normalisation failed: %s", err[-300:])
    return tmp  # Return raw downloaded file as last resort


async def _concat_audio_files(paths: list[Path], out: Path) -> bool:
    """Concatenate audio files of possibly mixed formats into one WAV."""
    if not paths:
        return False
    if len(paths) == 1:
        shutil.copy2(paths[0], out)
        return True
    # Convert each to a common WAV first (concat demuxer requires identical codec)
    wavs: list[Path] = []
    for i, p in enumerate(paths):
        w = out.parent / f"concat_{i:03d}.wav"
        cmd = [FFMPEG_BIN, "-y", "-i", str(p), "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(w)]
        ok, err = await _run_ffmpeg(cmd, timeout=120)
        if ok and w.exists() and w.stat().st_size > 0:
            wavs.append(w)
        else:
            logger.warning("_concat_audio_files: failed to convert %s: %s", p, err[-200:])
    if not wavs:
        return False
    list_file = out.parent / "audio_concat.txt"
    list_file.write_text("\n".join(f"file '{w.as_posix()}'" for w in wavs) + "\n")
    cmd = [
        FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(out),
    ]
    ok, _ = await _run_ffmpeg(cmd, timeout=180)
    return ok and out.exists() and out.stat().st_size > 0


async def _resolve_audio(project: dict, scenes: list[dict], assets: list[dict],
                         work_dir: Path) -> Optional[Path]:
    """Return local path to the project's narration audio, or None."""
    logger.info("_resolve_audio: project=%s voiceover_assets=%d", project.get("id"),
                len([a for a in assets if a.get("asset_type") == "voiceover_audio"]))

    # Prefer selected full-script voiceover; fall back to selected/newest usable full-script asset.
    full_candidates = [
        a for a in assets
        if a.get("asset_type") == "voiceover_audio"
        and not a.get("scene_id")
        and a.get("status") != "rejected"
    ]
    selected_full_id = project.get("selected_voiceover_asset_id")
    full = next(
        (a for a in full_candidates if selected_full_id and a.get("id") == selected_full_id),
        None,
    )
    if not full and full_candidates:
        full = next((a for a in full_candidates if a.get("status") == "selected"), None) or max(
            full_candidates, key=lambda x: str(x.get("created_at") or "")
        )
    if full:
        local = await _resolve_single_audio(full, work_dir, "audio_full")
        if local:
            logger.info("_resolve_audio: resolved full-script voiceover -> %s", local)
            return local

    def _scene_key(scene: dict, index: int) -> str:
        scene_id = scene.get("id")
        if scene_id not in (None, ""):
            return str(scene_id)
        scene_number = scene.get("scene_number")
        if scene_number not in (None, ""):
            return f"scene-{scene_number}"
        return f"scene-idx-{index}"

    # Concat scene-level voiceovers (pick selected per scene; else newest non-rejected)
    scene_voices_by_id: dict[str, dict] = {}
    for i, s in enumerate(scenes):
        scene_id = s.get("id")
        scene_number = s.get("scene_number")
        acceptable_scene_ids = {
            str(v)
            for v in (
                scene_id,
                f"scene-{scene_number}" if scene_number not in (None, "") else None,
                str(scene_number) if scene_number not in (None, "") else None,
            )
            if v not in (None, "")
        }
        ss = [
            a for a in assets
            if a.get("asset_type") == "voiceover_audio"
            and a.get("scene_id") in acceptable_scene_ids
            and a.get("status") != "rejected"
        ]
        if not ss:
            continue
        sel = next((x for x in ss if x.get("status") == "selected"), None) or max(
            ss, key=lambda x: str(x.get("created_at") or ""))
        scene_voices_by_id[_scene_key(s, i)] = sel

    if scene_voices_by_id:
        ordered: list[Path] = []
        for i, s in enumerate(sorted(scenes, key=lambda x: x.get("scene_number", 0))):
            v = scene_voices_by_id.get(_scene_key(s, i))
            if not v:
                continue
            local = await _resolve_single_audio(v, work_dir, f"audio_scene_{i:03d}")
            if local:
                ordered.append(local)
        if ordered:
            out = work_dir / "audio_full.wav"
            if await _concat_audio_files(ordered, out):
                logger.info("_resolve_audio: resolved concatenated scene voiceovers -> %s", out)
                return out

    logger.warning("_resolve_audio: no usable voiceover audio found")
    return None


def _seconds_to_srt_time(seconds: float) -> str:
    """Convert seconds to SRT time format HH:MM:SS,mmm."""
    seconds = max(0, seconds)
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds % 1) * 1000))
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def _generate_srt(scene_timeline: list[tuple[dict, float, float]], work_dir: Path) -> Path:
    """Generate an SRT subtitle file from scene narration/caption text.

    scene_timeline holds (scene, start, end) using the actual encoded clip
    times, so captions stay in sync with the final video across all scenes.
    No intro entry — the title card already shows the title once."""
    srt_path = work_dir / "subtitles.srt"
    entries: list[str] = []
    idx = 1
    for scene, start, end in scene_timeline:
        text = (scene.get("caption_text") or scene.get("narration_text") or "").strip()
        if not text:
            continue
        # Truncate long narration to a readable caption line
        text = text[:120]
        entries.append(f"{idx}\n{_seconds_to_srt_time(start)} --> {_seconds_to_srt_time(end)}\n{text}\n")
        idx += 1
    srt_path.write_text("\n".join(entries), encoding="utf-8")
    return srt_path


async def _generate_background_music(duration: float, work_dir: Path) -> Path:
    """Generate a low-volume ambient music bed that loops for the full video duration."""
    out = work_dir / "music_bed.wav"
    # Generative ambient chord (A minor 7: 220Hz, 262Hz, 329Hz, 440Hz) with slow attack/release
    chord = "220|262|329|440"
    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "lavfi", "-i", f"sine=frequency={chord.split('|')[0]}:duration={duration:.2f}",
        "-f", "lavfi", "-i", f"sine=frequency={chord.split('|')[1]}:duration={duration:.2f}",
        "-f", "lavfi", "-i", f"sine=frequency={chord.split('|')[2]}:duration={duration:.2f}",
        "-f", "lavfi", "-i", f"sine=frequency={chord.split('|')[3]}:duration={duration:.2f}",
        "-filter_complex",
        "[0:a][1:a][2:a][3:a]amix=inputs=4:duration=longest,volume=0.12,"
        "afade=t=in:ss=0:d=2,afade=t=out:st=" + f"{max(0, duration - 2):.2f}" + ":d=2",
        "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
        str(out),
    ]
    ok, err = await _run_ffmpeg(cmd, timeout=120)
    if not ok or not out.exists() or out.stat().st_size == 0:
        logger.warning("Background music generation failed: %s", err[-300:])
        # Fallback: silent bed
        cmd = [
            FFMPEG_BIN, "-y",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t", f"{duration:.2f}",
            "-c:a", "pcm_s16le", str(out),
        ]
        await _run_ffmpeg(cmd, timeout=60)
    return out


# ============================ ffmpeg ============================

async def _run_ffmpeg(cmd: list[str], *, timeout: int = HARD_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Run ffmpeg with the supplied (server-built) args. Returns (ok, stderr_tail)."""
    logger.info("ffmpeg: %s", " ".join(cmd[:6]) + " …")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, "ffmpeg timed out"
    tail = (stderr or b"").decode("utf-8", errors="ignore")[-1500:]
    return (proc.returncode == 0), tail


def _ffmpeg_normalise_image(src: Path, duration: float, out: Path) -> list[str]:
    return [
        FFMPEG_BIN, "-y",
        "-loop", "1", "-t", f"{duration:.2f}",
        "-i", str(src),
        "-vf", (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p"
        ),
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-an",
        str(out),
    ]


def _ffmpeg_normalise_video(src: Path, duration: float, out: Path) -> list[str]:
    return [
        FFMPEG_BIN, "-y",
        # Loop sources shorter than the scene slot so the clip always covers
        # the full scene duration (-t caps the output).
        "-stream_loop", "-1",
        "-i", str(src),
        "-t", f"{duration:.2f}",
        "-vf", (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p,fps={FPS}"
        ),
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-an",
        str(out),
    ]


# ============================ MAIN PIPELINE ============================

def _project_lock(project_id: str) -> asyncio.Lock:
    if project_id not in _LOCKS:
        _LOCKS[project_id] = asyncio.Lock()
    return _LOCKS[project_id]


async def _set_job(job_id: str, **patch):
    db = get_db()
    patch.setdefault("updated_at", _now())
    await db.render_jobs.update_one({"id": job_id}, {"$set": patch})


async def is_render_active(project_id: str) -> bool:
    db = get_db()
    job = await db.render_jobs.find_one(
        {"project_id": project_id, "status": {"$in": ["queued", "validating", "preparing_assets", "rendering"]}},
        {"_id": 0, "id": 1},
    )
    return bool(job)


async def queue_render(project_id: str, *, requested_by: str) -> dict:
    """Create a job in 'queued' state and start the background worker."""
    db = get_db()
    if await is_render_active(project_id):
        raise RuntimeError("A render is already in progress for this project.")
    job_id = str(uuid.uuid4())
    now = _now()
    job = {
        "id": job_id,
        "project_id": project_id,
        "status": "queued",
        "progress": 0,
        "current_step": "queued",
        "output_path": None,
        "output_url": None,
        "duration": None,
        "error_message": None,
        "requested_by": requested_by,
        "started_at": None,
        "completed_at": None,
        "created_at": now,
        "updated_at": now,
    }
    await db.render_jobs.insert_one(dict(job))

    # Start background task
    task = asyncio.create_task(_run_render_safe(job_id, project_id))
    _ACTIVE_TASKS[project_id] = task
    job.pop("_id", None)
    return job


async def cancel_render(project_id: str, job_id: str) -> bool:
    db = get_db()
    job = await db.render_jobs.find_one({"id": job_id, "project_id": project_id}, {"_id": 0})
    if not job:
        return False
    if job["status"] not in ("queued", "validating", "preparing_assets", "rendering"):
        return False
    task = _ACTIVE_TASKS.get(project_id)
    if task and not task.done():
        task.cancel()
    await _set_job(job_id, status="cancelled", current_step="cancelled",
                   error_message="Cancelled by user", completed_at=_now())
    return True


async def _run_render_safe(job_id: str, project_id: str):
    try:
        await _run_render(job_id, project_id)
    except asyncio.CancelledError:
        await _set_job(job_id, status="cancelled", current_step="cancelled",
                       error_message="Cancelled by user", completed_at=_now())
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Render job failed")
        await _set_job(job_id, status="failed", current_step="failed",
                       error_message=f"{type(e).__name__}: {e}"[:240],
                       completed_at=_now())
    finally:
        _ACTIVE_TASKS.pop(project_id, None)


async def _run_render(job_id: str, project_id: str):
    db = get_db()
    lock = _project_lock(project_id)
    async with lock:
        await _set_job(job_id, status="validating", current_step="validating",
                       progress=5, started_at=_now())

        project = await db.projects.find_one({"id": project_id}, {"_id": 0})
        script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
        scenes = await db.scenes.find({"project_id": project_id}, {"_id": 0}).sort("scene_number", 1).to_list(500)
        metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
        assets = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(500)

        check = validate_prerequisites(project, script, scenes, metadata, assets)
        if not check["ok"]:
            raise RuntimeError("Missing requirements: " + ", ".join(check["issues"][:5]))

        selected_thumb_id = project.get("selected_thumbnail_asset_id")
        thumb_candidates = [
            a for a in assets
            if a.get("asset_type") == "generated_thumbnail" and a.get("status") != "rejected"
        ]
        sel_thumb = next(
            (a for a in thumb_candidates if selected_thumb_id and a.get("id") == selected_thumb_id),
            None,
        )
        if not sel_thumb and thumb_candidates:
            sel_thumb = next((a for a in thumb_candidates if a.get("status") == "selected"), None) or max(
                thumb_candidates, key=lambda x: str(x.get("created_at") or "")
            )

        # Workdir per job
        work_dir = STATIC_RENDERS / project_id / f"_work_{job_id}"
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        # ---- preparing_assets ----
        await _set_job(job_id, status="preparing_assets", current_step="preparing_thumbnail", progress=15)
        intro_img = await _resolve_thumbnail(sel_thumb, project, work_dir)

        await _set_job(job_id, current_step="preparing_audio", progress=25)
        audio_path = await _resolve_audio(project, scenes, assets, work_dir)

        await _set_job(job_id, current_step="preparing_scenes", progress=35)
        scene_visuals: list[tuple[Path, str, dict]] = []
        for i, scene in enumerate(scenes):
            path, kind = await _resolve_scene_visual(scene, assets, project, work_dir, i)
            scene_visuals.append((path, kind, scene))

        # ---- rendering ----
        await _set_job(job_id, status="rendering", current_step="encoding_intro", progress=45)
        clips: list[Path] = []
        # Intro clip
        intro_out = work_dir / "clip_000_intro.mp4"
        ok, err = await _run_ffmpeg(_ffmpeg_normalise_image(intro_img, INTRO_DURATION_SECONDS, intro_out))
        if not ok:
            raise RuntimeError(f"intro encode failed: {err[-300:]}")
        clips.append(intro_out)

        # Scene clips — track the actual timeline so subtitles stay in sync
        scene_timeline: list[tuple[dict, float, float]] = []
        cursor = INTRO_DURATION_SECONDS
        for i, (path, kind, scene) in enumerate(scene_visuals):
            duration = max(2.0, float(scene.get("end_time", 0) - scene.get("start_time", 0)) or 4.0)
            await _set_job(job_id, current_step=f"encoding_scene_{i+1:02d}",
                           progress=min(85, 45 + int(35 * (i + 1) / max(1, len(scene_visuals)))))
            out = work_dir / f"clip_{i+1:03d}.mp4"
            cmd = (_ffmpeg_normalise_video(path, duration, out) if kind == "video"
                   else _ffmpeg_normalise_image(path, duration, out))
            ok, err = await _run_ffmpeg(cmd)
            if not ok:
                raise RuntimeError(f"scene {i+1} encode failed: {err[-300:]}")
            clips.append(out)
            scene_timeline.append((scene, cursor, cursor + duration))
            cursor += duration

        # Concat
        await _set_job(job_id, current_step="concatenating", progress=88)
        concat_list = work_dir / "concat.txt"
        concat_list.write_text("\n".join(f"file '{c.as_posix()}'" for c in clips) + "\n")
        silent_out = work_dir / "video_silent.mp4"
        ok, err = await _run_ffmpeg([
            FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c:v", "libx264", "-crf", "23", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-an",
            str(silent_out),
        ])
        if not ok:
            raise RuntimeError(f"concat failed: {err[-300:]}")

        # Mux audio + background music + subtitle burn-in
        await _set_job(job_id, current_step="muxing_audio", progress=92)
        srt_path = _generate_srt(scene_timeline, work_dir)

        # The music bed spans the actual encoded timeline
        music_path = await _generate_background_music(cursor, work_dir)

        await _set_job(job_id, current_step="muxing_audio", progress=94)
        out_dir = STATIC_RENDERS / project_id
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / f"{job_id}.mp4"

        has_narration = bool(audio_path and audio_path.exists())
        # Escape ffmpeg filter path characters that break the subtitles filter
        srt_escaped = srt_path.as_posix().replace(":", r"\:").replace("'", r"\'")
        if has_narration:
            inputs = [
                FFMPEG_BIN, "-y",
                "-i", str(silent_out),
                "-i", str(audio_path),
                "-i", str(music_path),
            ]
            filter_complex = (
                f"[0:v]subtitles='{srt_escaped}':force_style='"
                f"FontName=DejaVu Sans,FontSize=28,PrimaryColour=&H00FFFFFF,"
                f"OutlineColour=&HFF000000,Outline=3,Shadow=0,MarginV=60'[v];"
                f"[1:a][2:a]amix=inputs=2:duration=longest:dropout_transition=3,"
                f"volume=2.0,alimiter=limit=0.95[a]"
            )
            maps = ["-map", "[v]", "-map", "[a]"]
        else:
            # No narration: still add music + subtitles
            inputs = [
                FFMPEG_BIN, "-y",
                "-i", str(silent_out),
                "-i", str(music_path),
            ]
            filter_complex = (
                f"[0:v]subtitles='{srt_escaped}':force_style='"
                f"FontName=DejaVu Sans,FontSize=28,PrimaryColour=&H00FFFFFF,"
                f"OutlineColour=&HFF000000,Outline=3,Shadow=0,MarginV=60'[v];"
                f"[1:a]volume=0.8[a]"
            )
            maps = ["-map", "[v]", "-map", "[a]"]

        cmd = inputs + [
            "-filter_complex", filter_complex,
        ] + maps + [
            "-c:v", "libx264", "-crf", "23", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-shortest",
            "-movflags", "+faststart",
            str(final),
        ]
        ok, err = await _run_ffmpeg(cmd)
        if not ok:
            logger.error("mux failed: %s", err[-800:])
            raise RuntimeError(f"mux failed: {err[-300:]}")

        # Keep the subtitle file next to the final render for inspection
        try:
            shutil.copy2(srt_path, out_dir / f"{job_id}.srt")
        except Exception:  # noqa: BLE001
            pass

        # Probe duration via ffprobe (cheap; optional — depends on apt ffprobe)
        duration = None
        if FFPROBE_BIN:
            try:
                proc = await asyncio.create_subprocess_exec(
                    FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", str(final),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await proc.communicate()
                duration = float(out.decode().strip())
            except Exception:
                pass
        if duration is None:
            # Fallback: the encoded timeline length (intro + scene clips)
            duration = round(cursor, 2)

        # Cleanup workdir, keep final
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass
        # Best-effort cleanup for any leaked temp artifacts in container /tmp
        # naming convention from prior render implementations.
        try:
            for tmp_path in glob.glob("/tmp/render_*"):
                try:
                    if os.path.isdir(tmp_path):
                        shutil.rmtree(tmp_path, ignore_errors=True)
                    else:
                        os.remove(tmp_path)
                except Exception:
                    pass
        except Exception:
            pass

        # Persist to storage backend (local: no-op; object: upload + remove local)
        store = get_storage()
        key = f"renders/{project_id}/{final.name}"
        try:
            saved = store.save_file(final, key, content_type="video/mp4")
        except Exception as e:  # noqa: BLE001
            if getattr(store, "mode", "") == "object":
                logger.exception("Object storage upload failed; falling back to local storage")
                from .storage import LocalStorage
                fallback = LocalStorage()
                try:
                    saved = fallback.save_file(final, key, content_type="video/mp4")
                except Exception as local_err:  # noqa: BLE001
                    raise RuntimeError(f"storage upload failed: {e}; local fallback failed: {local_err}")
            else:
                raise RuntimeError(f"storage upload failed: {e}")

        await _set_job(
            job_id,
            status="completed",
            current_step="completed",
            progress=100,
            output_path=str(saved.file_path) if saved.file_path else None,
            output_url=saved.url,
            output_relative_url=saved.preview_path,
            output_storage_mode=("object" if saved.remote else "local"),
            output_storage_key=saved.key,
            file_size=(saved.file_path.stat().st_size if saved.file_path and saved.file_path.exists() else final.stat().st_size if final.exists() else None),
            duration=duration,
            completed_at=_now(),
            error_message=None,
        )
        # Update project status pointer
        await db.projects.update_one(
            {"id": project_id},
            {"$set": {
                "status": "COMPLETED",
                "rendered_video_asset_id": job_id,
                "updated_at": _now(),
            }},
        )

        # Notify AdEngine for auto-post projects
        try:
            if project.get("auto_post") and saved.url:
                caption = (
                    (metadata.get("selected_title") if metadata else None)
                    or (script.get("selected_hook") if script else None)
                    or project.get("topic", "")
                )
                platforms = project.get("platforms") or []
                if platforms:
                    await send_to_adengine(
                        video_url=saved.url,
                        caption=caption,
                        platforms=platforms,
                        project_id=project_id,
                    )
        except Exception as ae:  # noqa: BLE001
            logger.exception("AdEngine notification failed for project %s: %s", project_id, ae)
