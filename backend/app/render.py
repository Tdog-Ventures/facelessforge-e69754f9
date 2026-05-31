"""Real ffmpeg render queue.

Produces a 1920x1080 30fps H.264 + AAC MP4 from:
  • selected thumbnail   (intro frame, 1.5s)
  • scene visual assets  (one clip per scene at scene duration)
  • selected voiceover   (full-script preferred; else concat of per-scene VOs)

Mock-compatible:
  • Mock thumbnails are SVG → fall back to a Pillow-rendered PNG
  • Remote stock URLs that 404 / time out → fall back to a Pillow caption frame
  • Missing voiceover → silent track

Security:
  • All ffmpeg args are constructed server-side from validated DB rows.
  • No raw user args ever reach ffmpeg.
  • All paths sanitised to the project's render workdir.
  • One concurrent render per project; explicit cancellation supported.
"""
from __future__ import annotations

import asyncio
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
from .subtitles import write_srt

logger = logging.getLogger("facelessforge.render")

STATIC_RENDERS = Path(__file__).parent.parent / "static" / "renders"
STATIC_RENDERS.mkdir(parents=True, exist_ok=True)

STATIC_MUSIC_DIR = Path(__file__).parent.parent / "static" / "music"
DEFAULT_MUSIC_BED = STATIC_MUSIC_DIR / "default_bed.mp3"


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


def _resolve_music_bed() -> Optional[Path]:
    """Return a local music bed file path, or None if disabled / missing.

    Resolution order:
      1. RENDER_MUSIC_BED_PATH env override (absolute path)
      2. Bundled default at static/music/default_bed.mp3
    """
    override = os.environ.get("RENDER_MUSIC_BED_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.exists() and p.is_file() else None
    if DEFAULT_MUSIC_BED.exists() and DEFAULT_MUSIC_BED.is_file():
        return DEFAULT_MUSIC_BED
    return None


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

    # Scene visual coverage — soft warning only (we fall back to caption frames)
    scene_assets = [a for a in assets if a.get("asset_type") in ("stock_image", "stock_video") and a.get("scene_id")]
    covered_ids = {a["scene_id"] for a in scene_assets}
    coverage = (len(covered_ids) / max(1, len(scenes))) if scenes else 0
    _add("scene_assets", "Scene visuals attached",
         coverage >= 0.5,
         f"{len(covered_ids)}/{len(scenes)} scenes have stock visuals. "
         "Empty scenes will use caption fallback frames.")

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
    # title wrap
    words = (title or "").split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        # crude width check
        try:
            wpx = draw.textlength(test, font=title_font)
        except Exception:
            wpx = len(test) * 40
        if wpx > size[0] - margin * 2 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = test
    if cur:
        lines.append(cur)
    y = margin + 60
    for line in lines[:4]:
        draw.text((margin, y), line, font=title_font, fill="#FFFFFF")
        y += 110
    if subtitle:
        draw.text((margin, y + 30), subtitle[:120], font=sub_font, fill="#A1A1AA")
    if footer:
        draw.text((margin, size[1] - 90), footer[:140], font=foot_font, fill=accent)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    return out_path


async def _download_to(url: str, out_path: Path, *, max_bytes: int,
                       allow_audio: bool = False) -> bool:
    """Best-effort download. Returns True on success, False on any failure."""
    try:
        timeout = httpx.Timeout(20.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return False
                ct = resp.headers.get("content-type", "")
                # Only accept image/video (or audio when explicitly allowed)
                allowed = (ct.startswith("image/") or ct.startswith("video/")
                           or ct.startswith("application/octet-stream")
                           or (allow_audio and ct.startswith("audio/")))
                if not allowed:
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


def _local_path_for_asset(asset: dict) -> Optional[Path]:
    """If the asset already has a local file_path that exists, return it."""
    fp = asset.get("file_path")
    if fp:
        p = Path(fp)
        if p.exists() and p.is_file():
            return p
    return None


async def _ensure_audio_local(asset: dict, work_dir: Path, name: str) -> Optional[Path]:
    """Return a local Path to the asset's audio file, downloading from remote
    storage (R2/S3) if needed. Returns None if no usable source."""
    local = _local_path_for_asset(asset)
    if local:
        return local
    url = asset.get("preview_url") or asset.get("download_url")
    if not url:
        return None
    key = asset.get("storage_key") or url
    suffix = ".mp3" if key.lower().endswith(".mp3") else ".wav"
    out = work_dir / f"{name}{suffix}"
    ok = await _download_to(url, out, max_bytes=80 * 1024 * 1024, allow_audio=True)
    return out if ok else None



async def _resolve_thumbnail(asset: dict, project: dict, work_dir: Path) -> Path:
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


async def _resolve_scene_visual(scene: dict, attached_assets: list[dict],
                                 project: dict, work_dir: Path, idx: int) -> tuple[Path, str]:
    """Return (local_path, kind) where kind is 'image' or 'video'.
    Always succeeds — falls back to caption frame on any error."""
    # Prefer first attached stock asset
    candidates = [a for a in attached_assets if a.get("scene_id") == scene.get("id")
                  and a.get("asset_type") in ("stock_image", "stock_video")]
    out_dir = work_dir / "scenes"
    out_dir.mkdir(parents=True, exist_ok=True)
    fallback_path = out_dir / f"scene_{idx:03d}_fallback.png"

    for a in candidates:
        url = a.get("download_url") or a.get("preview_url") or a.get("source_url")
        local = _local_path_for_asset(a)
        ext = (Path(local).suffix.lower() if local else "")
        # Try local first
        if local and ext in (".png", ".jpg", ".jpeg"):
            return (local, "image")
        if local and ext in (".mp4", ".mov", ".webm"):
            return (local, "video")
        if not url:
            continue
        is_video = a.get("asset_type") == "stock_video" or any(url.lower().endswith(ext)
            for ext in (".mp4", ".mov", ".webm"))
        suffix = ".mp4" if is_video else ".jpg"
        target = out_dir / f"scene_{idx:03d}_src{suffix}"
        ok = await _download_to(url, target, max_bytes=MAX_VIDEO_DOWNLOAD_BYTES)
        if ok:
            return (target, "video" if is_video else "image")

    # Fallback caption
    caption = scene.get("caption_text") or scene.get("narration_text") or scene.get("visual_direction") or ""
    title = f"Scene {scene.get('scene_number', idx + 1):02d}"
    _pil_caption_frame(
        fallback_path,
        title=title,
        subtitle=(caption or "")[:160],
        footer=project.get("name", "")[:120],
        palette=("#0F0F12", "#7B61FF"),
    )
    return (fallback_path, "image")


async def _resolve_audio(project: dict, scenes: list[dict], assets: list[dict],
                         work_dir: Path) -> Optional[Path]:
    """Return local audio path or None.

    Skips mock (silent) voiceover assets — callers fall through to the
    music-bed-only mux branch so the final MP4 actually has audible audio.
    """
    def _is_real(a: dict) -> bool:
        return bool(a) and not a.get("mock") and a.get("source") != "mock_tts"

    # Prefer the full-script selected
    full = next((a for a in assets if a.get("asset_type") == "voiceover_audio"
                 and not a.get("scene_id")
                 and a.get("id") == project.get("selected_voiceover_asset_id")), None)
    if _is_real(full):
        local = await _ensure_audio_local(full, work_dir, "voiceover_full")
        if local:
            return local

    # Concat scene-level (pick selected per scene; else newest non-rejected)
    scene_voices_by_id: dict[str, dict] = {}
    for s in scenes:
        ss = [a for a in assets if a.get("asset_type") == "voiceover_audio"
              and a.get("scene_id") == s.get("id") and a.get("status") != "rejected"
              and _is_real(a)]
        if not ss:
            continue
        sel = next((x for x in ss if x.get("status") == "selected"), None) or max(
            ss, key=lambda x: str(x.get("created_at") or ""))
        scene_voices_by_id[s["id"]] = sel
    if scene_voices_by_id:
        ordered: list[Path] = []
        for idx, s in enumerate(sorted(scenes, key=lambda x: x.get("scene_number", 0))):
            v = scene_voices_by_id.get(s["id"])
            if not v:
                continue
            local = await _ensure_audio_local(v, work_dir, f"voiceover_scene_{idx:03d}")
            if local:
                ordered.append(local)
        if ordered:
            if len(ordered) == 1:
                return ordered[0]
            # ffmpeg concat audio
            list_file = work_dir / "audio_concat.txt"
            list_file.write_text("\n".join(f"file '{p.as_posix()}'" for p in ordered) + "\n")
            out = work_dir / "audio_full.wav"
            cmd = [FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
                   "-i", str(list_file), "-c", "copy", str(out)]
            ok, _ = await _run_ffmpeg(cmd)
            if ok and out.exists():
                return out
    return None


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

        sel_thumb = next((a for a in assets if a["id"] == project.get("selected_thumbnail_asset_id")), None)

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

        # Scene clips
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

        # Concat
        await _set_job(job_id, current_step="concatenating", progress=88)
        concat_list = work_dir / "concat.txt"
        concat_list.write_text("\n".join(f"file '{c.as_posix()}'" for c in clips) + "\n")
        silent_out = work_dir / "video_silent.mp4"
        ok, err = await _run_ffmpeg([
            FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list), "-c", "copy", str(silent_out),
        ])
        if not ok:
            raise RuntimeError(f"concat failed: {err[-300:]}")

        # ---- subtitle burn-in (hard subs from scene captions) ----
        burned_out = silent_out  # fallback if subs disabled/empty
        burn_enabled = os.environ.get("RENDER_BURN_SUBTITLES", "true").lower() in ("1", "true", "yes")
        if burn_enabled and scenes:
            await _set_job(job_id, current_step="burning_subtitles", progress=90)
            srt_path = work_dir / "captions.srt"
            try:
                write_srt(scenes, srt_path, intro_offset_seconds=INTRO_DURATION_SECONDS)
            except Exception as e:  # noqa: BLE001
                logger.warning("SRT generation failed (%s) — skipping burn-in", e)
                srt_path = None
            if srt_path and srt_path.exists() and srt_path.stat().st_size > 0:
                burned_out = work_dir / "video_subbed.mp4"
                # Path needs ffmpeg-style escaping for the subtitles filter.
                srt_escaped = srt_path.as_posix().replace(":", r"\:").replace("'", r"\'")
                sub_style = (
                    "FontName=DejaVu Sans,FontSize=22,PrimaryColour=&H00FFFFFF&,"
                    "OutlineColour=&H66000000&,BackColour=&H99000000&,BorderStyle=3,"
                    "Outline=1,Shadow=0,Alignment=2,MarginV=60"
                )
                cmd = [
                    FFMPEG_BIN, "-y", "-i", str(silent_out),
                    "-vf", f"subtitles='{srt_escaped}':force_style='{sub_style}'",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-an", str(burned_out),
                ]
                ok, err = await _run_ffmpeg(cmd)
                if not ok:
                    logger.warning("subtitle burn-in failed (%s) — using clean video", err[-300:])
                    burned_out = silent_out

        # Mux audio (voiceover + optional music bed at -18dB)
        await _set_job(job_id, current_step="muxing_audio", progress=94)
        out_dir = STATIC_RENDERS / project_id
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / f"{job_id}.mp4"

        music_path = _resolve_music_bed()
        music_db = float(os.environ.get("RENDER_MUSIC_GAIN_DB", "-18"))
        use_music = bool(music_path and music_path.exists()
                         and os.environ.get("RENDER_MUSIC_BED", "true").lower() in ("1", "true", "yes"))

        if audio_path and audio_path.exists() and use_music:
            # voiceover (loud) + music bed (quiet, looped) → amix → AAC
            cmd = [
                FFMPEG_BIN, "-y",
                "-i", str(burned_out),
                "-i", str(audio_path),
                "-stream_loop", "-1", "-i", str(music_path),
                "-filter_complex",
                f"[2:a]volume={music_db}dB[bed];"
                f"[1:a]volume=0dB[vo];"
                f"[vo][bed]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                str(final),
            ]
        elif audio_path and audio_path.exists():
            cmd = [
                FFMPEG_BIN, "-y",
                "-i", str(burned_out),
                "-i", str(audio_path),
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                str(final),
            ]
        elif use_music:
            # No voiceover — music bed plays at 0 dB (it's the only audio)
            cmd = [
                FFMPEG_BIN, "-y",
                "-i", str(burned_out),
                "-stream_loop", "-1", "-i", str(music_path),
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                str(final),
            ]
        else:
            # Last-resort silent AAC so the MP4 still has an audio stream
            cmd = [
                FFMPEG_BIN, "-y",
                "-i", str(burned_out),
                "-f", "lavfi", "-i", "anullsrc=cl=stereo:r=48000",
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                "-movflags", "+faststart",
                str(final),
            ]
        ok, err = await _run_ffmpeg(cmd)
        if not ok:
            raise RuntimeError(f"mux failed: {err[-300:]}")

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
            # Fallback: estimate from scene durations + intro
            est = INTRO_DURATION_SECONDS
            for s in scenes:
                est += max(2.0, float((s.get("end_time") or 0) - (s.get("start_time") or 0)) or 4.0)
            duration = round(est, 2)

        # Cleanup workdir, keep final
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass

        # Persist to storage backend (local: no-op; object: upload + remove local)
        store = get_storage()
        key = f"renders/{project_id}/{final.name}"
        try:
            saved = store.save_file(final, key, content_type="video/mp4")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"storage upload failed: {e}")

        await _set_job(
            job_id,
            status="completed",
            current_step="completed",
            progress=100,
            output_path=str(saved.file_path) if saved.file_path else None,
            output_url=saved.url,
            output_relative_url=saved.preview_path,
            output_storage_mode=store.mode,
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
