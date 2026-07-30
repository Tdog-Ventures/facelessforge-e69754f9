from __future__ import annotations

"""CHANGELOG
==========
v2.0.0 — Quality Constitution Compliance Sweep (2025-06-25)
----------------------------------------------------------------
1. [TIMING] INTRO_DURATION_SECONDS 1.5 → 2.5  (≤2.5s per §1)
2. [VISUAL] MAX_SUBCLIP_SECONDS 9.0 → 6.0  (shot ≤6s per §2)
3. [FOOTAGE] Deleted all pad filters → crop-fill 1920×1080 (§4)
4. [TEXT] Removed giant per-scene title labels (§5)
5. [TEXT] Intro now uses hook footage + dark overlay + title (§5)
6. [TEXT] Subtitle style updated to constitution spec (§5)
7. [TEXT] words_per_cue=7 passed to subtitle generator (§5)
8. [AUDIO] Music volume dB → amplitude 0.12 (§6)
9. [AUDIO] Added music fade-out last 2s (§6)
10. [AUDIO] Added loudnorm=I=-14:TP=-1.5:LRA=11 to final mux (§6)
11. [AUDIO] WARNING log on silent fallback (§7)
12. [VERIFICATION] All timing / pacing constraints now enforced by code
"""
"""Real ffmpeg render queue.

Produces a 1920x1080 30fps H.264 + AAC MP4 from:
  • selected thumbnail   (intro frame, up to 2.5s — now uses hook footage)
  • scene visual assets  (multiple clips per scene at scene duration)
  • selected voiceover   (full-script preferred; else concat of per-scene VOs)

Mock-compatible:
  • Mock thumbnails are SVG → fall back to a Pillow-rendered PNG
  • Remote stock URLs that 404 / time out → fall back to a Pillow caption frame
  • Missing voiceover → silent track (with WARNING log)

Security:
  • All ffmpeg args are constructed server-side from validated DB rows.
  • No raw user args ever reach ffmpeg.
  • All paths sanitised to the project's render workdir.
  • One concurrent render per project; explicit cancellation supported.
"""
import asyncio
import json
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
from .subtitles import write_srt, write_srt_from_text, write_srt_from_words
from .transcribe import transcribe_words
from . import stock as stock_service
try:
    from .verify import verify_render as _verify_render_constitution
    VERIFY_AVAILABLE = True
except ImportError:
    try:
        from verify import verify_render as _verify_render_constitution
        VERIFY_AVAILABLE = True
    except ImportError:
        _verify_render_constitution = None
        VERIFY_AVAILABLE = False

logger = logging.getLogger("facelessforge.render")

STATIC_RENDERS = Path(__file__).parent.parent / "static" / "renders"
STATIC_RENDERS.mkdir(parents=True, exist_ok=True)

STATIC_MUSIC_DIR = Path(__file__).parent.parent / "static" / "music"
DEFAULT_MUSIC_BED = STATIC_MUSIC_DIR / "default_bed.mp3"

# Max length of any single sub-clip in seconds. Long scenes are split into
# multiple sub-clips against the same source footage (different seek offsets)
# so viewers see cuts every 3-6 seconds instead of one shot held for 20+.
# CONSTITUTION §2: A single stock clip may hold the screen for at most 6 seconds.
MAX_SUBCLIP_SECONDS = 6.0
MIN_SUBCLIP_SECONDS = 3.0


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


async def _probe_duration_seconds(path: Path) -> Optional[float]:
    """Return media duration in seconds via ffprobe, or None on failure."""
    bin_ = _resolve_ffprobe_bin()
    if not bin_ or not path.exists():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return float(out.decode().strip())
    except Exception:  # noqa: BLE001
        return None


def _build_subclip_plan(scenes: list[dict], audio_duration: Optional[float]) -> list[dict]:
    """Return a per-scene plan describing how many sub-clips to render and
    each sub-clip's duration. When ``audio_duration`` is provided, the total
    video time is stretched/contracted to match the voiceover exactly.

    Each scene entry: ``{"scene_index": i, "subclips": [seconds, ...]}``.
    """
    def _scene_dur(s: dict) -> float:
        return max(2.0, float((s.get("end_time") or 0) - (s.get("start_time") or 0)) or 4.0)

    planned = [_scene_dur(s) for s in scenes]
    planned_total = sum(planned)
    if audio_duration and audio_duration > 1.0 and planned_total > 1.0:
        scale = audio_duration / planned_total
    else:
        scale = 1.0
    plan: list[dict] = []
    for i, base in enumerate(planned):
        target = base * scale
        if target <= MAX_SUBCLIP_SECONDS:
            subclips = [target]
        else:
            import math
            n = max(2, math.ceil(target / MAX_SUBCLIP_SECONDS))
            even = target / n
            # Avoid runt clips
            if even < MIN_SUBCLIP_SECONDS:
                n = max(2, int(target // MIN_SUBCLIP_SECONDS) or 2)
                even = target / n
            subclips = [round(even, 3)] * n
        plan.append({"scene_index": i, "subclips": subclips, "target": round(target, 3)})
    return plan


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
# CONSTITUTION §1: Intro duration must be ≤2.5 seconds.
INTRO_DURATION_SECONDS = 2.5

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


def _resolve_font_path() -> str:
    """Return a system font path for ffmpeg drawtext."""
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if os.path.exists(path):
            return path
    return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _wrap_text(text: str, max_chars: int = 30) -> str:
    """Wrap text into lines of at most max_chars characters.

    CONSTITUTION §5: Intro title centered, wrapped, ≥80px side margins.
    At 72pt font, 30 chars ≈ safe width within 1760px usable area.
    """
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > max_chars and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip() if cur else w
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def _pil_caption_frame(out_path: Path, *, title: str, subtitle: str = "",
                       footer: str = "", palette: tuple[str, str] = ("#0A0A0A", "#00E5FF"),
                       size: tuple[int, int] = (WIDTH, HEIGHT)) -> Path:
    """Branded fallback frame — used when an image asset is unusable.

    CONSTITUTION §5: No giant per-scene title labels. When used as a scene
    fallback, title is passed as "" so only the subtitle (narration) appears.
    """
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
    # title (omitted for scene fallbacks per constitution)
    title_font = _try_load_font(96)
    sub_font = _try_load_font(40)
    foot_font = _try_load_font(28)
    margin = 100
    y = margin + 60
    if title:
        words = (title or "").split()
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
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
    """Resolve thumbnail to a static PNG for fallback use.

    CONSTITUTION §5: The intro now uses hook footage + dark overlay by default.
    This static image is only used as a last-resort fallback when no video
    footage is available across any scene.
    """
    out = work_dir / "intro_fallback.png"
    local = _local_path_for_asset(asset)
    if local and local.suffix.lower() in (".png", ".jpg", ".jpeg"):
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


async def _video_has_motion(path: Path) -> bool:
    """Return True iff the file is a real video with multiple frames.

    Some Pexels results — and certain CDN responses — return a still image
    encoded as a single-frame MP4, or a download_url that 200's with an
    image/jpeg payload. Either produces a 'static slideshow' artifact when
    looped through ffmpeg. We probe for: video stream present, duration > 1s,
    and frame count > 1 (or frame_rate × duration > 1).
    """
    bin_ = _resolve_ffprobe_bin()
    if not bin_:
        return True
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=nb_frames,nb_read_frames,r_frame_rate,duration,codec_type",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=0", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="ignore")
        fields: dict[str, str] = {}
        for line in text.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k.strip()] = v.strip()
        if fields.get("codec_type") != "video":
            return False
        nb = fields.get("nb_frames", "")
        if nb and nb != "N/A":
            try:
                if int(nb) <= 1:
                    return False
            except ValueError:
                pass
        rate = fields.get("r_frame_rate", "0/1")
        try:
            num, den = rate.split("/")
            fps = float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        dur_str = fields.get("duration") or ""
        try:
            dur = float(dur_str)
        except ValueError:
            dur = 0.0
        if dur < 1.0:
            return False
        if fps and dur and fps * dur < 2:
            return False
        return True
    except Exception:  # noqa: BLE001
        return True


async def _resolve_scene_visual(scene: dict, attached_assets: list[dict],
                                 project: dict, work_dir: Path, idx: int) -> tuple[Path, str]:
    """Return (local_path, kind) where kind is 'image' or 'video'.
    Always succeeds — falls back to caption frame on any error.

    For ``stock_video`` candidates, downloads are probed with ffprobe; any
    single-frame / sub-1s clip is rejected and the next candidate is tried.
    When all attached candidates fail the motion check, Pexels is re-queried
    with the project's visual_tone modifier appended for a coherent fallback.
    """
    from .visual_query import build_scene_query
    visual_tone = (project or {}).get("visual_tone") or ""
    candidates = [a for a in attached_assets if a.get("scene_id") == scene.get("id")
                  and a.get("asset_type") in ("stock_image", "stock_video")]
    out_dir = work_dir / "scenes"
    out_dir.mkdir(parents=True, exist_ok=True)
    fallback_path = out_dir / f"scene_{idx:03d}_fallback.png"

    for a in candidates:
        url = a.get("download_url") or a.get("preview_url") or a.get("source_url")
        local = _local_path_for_asset(a)
        ext = (Path(local).suffix.lower() if local else "")
        ext_id = a.get("external_id") or a.get("id", "")[:8]
        if local and ext in (".png", ".jpg", ".jpeg"):
            logger.info("scene=%02d FOOTAGE_SELECT type=local_image ext_id=%s path=%s",
                        idx + 1, ext_id, local)
            return (local, "image")
        if local and ext in (".mp4", ".mov", ".webm"):
            if await _video_has_motion(local):
                logger.info("scene=%02d FOOTAGE_SELECT type=local_video ext_id=%s path=%s",
                            idx + 1, ext_id, local)
                return (local, "video")
            logger.warning("scene=%02d FOOTAGE_REJECT reason=local_static_video ext_id=%s path=%s",
                           idx + 1, ext_id, local)
            continue
        if not url:
            logger.warning("scene=%02d FOOTAGE_SKIP reason=no_url ext_id=%s", idx + 1, ext_id)
            continue
        is_video = a.get("asset_type") == "stock_video" or any(url.lower().endswith(ext)
            for ext in (".mp4", ".mov", ".webm"))
        suffix = ".mp4" if is_video else ".jpg"
        target = out_dir / f"scene_{idx:03d}_src_{ext_id}{suffix}"
        ok = await _download_to(url, target, max_bytes=MAX_VIDEO_DOWNLOAD_BYTES)
        if not ok:
            logger.warning("scene=%02d FOOTAGE_REJECT reason=download_failed ext_id=%s url=%s",
                           idx + 1, ext_id, url[:100])
            continue
        size = target.stat().st_size if target.exists() else 0
        if is_video:
            motion = await _video_has_motion(target)
            probe = await _probe_duration_seconds(target)
            if not motion:
                logger.warning("scene=%02d FOOTAGE_REJECT reason=no_motion ext_id=%s size=%d duration=%ss url=%s",
                               idx + 1, ext_id, size, probe, url[:100])
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            logger.info("scene=%02d FOOTAGE_SELECT type=pexels_video ext_id=%s size=%d duration=%ss url=%s",
                        idx + 1, ext_id, size, probe, url[:100])
            return (target, "video")
        logger.info("scene=%02d FOOTAGE_SELECT type=pexels_image ext_id=%s size=%d url=%s",
                    idx + 1, ext_id, size, url[:100])
        return (target, "image")

    # ---- Pexels retry: query for fresh results when attached candidates fail ----
    queries: list[str] = []
    primary = build_scene_query(scene, visual_tone=visual_tone or None)
    if primary:
        queries.append(primary)
    narration_only = build_scene_query(scene)
    if narration_only and narration_only not in queries:
        queries.append(narration_only)
    tried_ext_ids = {str(a.get("external_id")) for a in candidates if a.get("external_id")}
    retry_results: list[dict] = []
    for q in queries[:2]:
        try:
            res = await stock_service.search_stock(
                q, media_type="videos", per_page=15,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("scene=%02d FOOTAGE_RETRY_ERROR query=%r err=%s",
                           idx + 1, q[:60], e)
            continue
        for r in (res.get("results") or []):
            if r.get("media_type") != "stock_video":
                continue
            if str(r.get("external_id")) in tried_ext_ids:
                continue
            retry_results.append(r)
            tried_ext_ids.add(str(r.get("external_id")))
        if retry_results:
            logger.info("scene=%02d FOOTAGE_RETRY query=%r tone=%r got=%d candidates",
                        idx + 1, q[:60], visual_tone, len(retry_results))
            break

    for r in retry_results[:6]:
        url = r.get("download_url")
        if not url:
            continue
        ext_id = r.get("external_id") or ""
        target = out_dir / f"scene_{idx:03d}_retry_{ext_id}.mp4"
        ok = await _download_to(url, target, max_bytes=MAX_VIDEO_DOWNLOAD_BYTES)
        if not ok:
            logger.warning("scene=%02d FOOTAGE_RETRY_REJECT reason=download_failed ext_id=%s",
                           idx + 1, ext_id)
            continue
        if not await _video_has_motion(target):
            size = target.stat().st_size if target.exists() else 0
            probe = await _probe_duration_seconds(target)
            logger.warning("scene=%02d FOOTAGE_RETRY_REJECT reason=no_motion ext_id=%s size=%d duration=%ss",
                           idx + 1, ext_id, size, probe)
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        size = target.stat().st_size if target.exists() else 0
        probe = await _probe_duration_seconds(target)
        logger.info("scene=%02d FOOTAGE_SELECT type=pexels_retry ext_id=%s size=%d duration=%ss url=%s",
                    idx + 1, ext_id, size, probe, url[:100])
        return (target, "video")

    # Fallback caption — CONSTITUTION §5: no giant per-scene title labels.
    logger.warning("scene=%02d FOOTAGE_FALLBACK reason=all_candidates_rejected candidates=%d",
                   idx + 1, len(candidates))
    caption = scene.get("caption_text") or scene.get("narration_text") or scene.get("visual_direction") or ""
    _pil_caption_frame(
        fallback_path,
        title="",  # Giant titles deleted per constitution
        subtitle=(caption or "")[:160],
        footer="",
        palette=("#0F0F12", "#7B61FF"),
    )
    return (fallback_path, "image")


async def _resolve_scene_visuals(scene: dict, attached_assets: list[dict],
                                 project: dict, work_dir: Path, idx: int,
                                 max_visuals: int = 4) -> list[tuple[Path, str]]:
    """Resolve up to ``max_visuals`` distinct visuals for one scene.

    Verify check d needs a hard cut every ~8s; seek-offset jump cuts within
    a single source clip rarely reach the scene-score threshold, so
    successive sub-clips must come from different sources. Iterates the
    scene's attached candidates one at a time via ``_resolve_scene_visual``
    (which still applies motion checks, Pexels retry, and caption fallback).
    Stops when the same output path repeats (caption fallback) or when
    ``max_visuals`` distinct sources are collected.
    """
    visuals: list[tuple[Path, str]] = []
    seen: set[str] = set()
    candidates = [a for a in attached_assets if a.get("scene_id") == scene.get("id")
                  and a.get("asset_type") in ("stock_image", "stock_video")]
    for a in candidates[:max_visuals]:
        path, kind = await _resolve_scene_visual(scene, [a], project, work_dir, idx)
        if str(path) in seen:
            break
        seen.add(str(path))
        visuals.append((path, kind))
    if not visuals:
        path, kind = await _resolve_scene_visual(scene, attached_assets, project, work_dir, idx)
        visuals.append((path, kind))
    return visuals


async def _resolve_audio(project: dict, scenes: list[dict], assets: list[dict],
                         work_dir: Path) -> Optional[Path]:
    """Return local audio path or None.

    Skips mock (silent) voiceover assets — callers fall through to the
    music-bed-only mux branch so the final MP4 actually has audible audio.
    """
    def _is_real(a: dict) -> bool:
        return bool(a) and not a.get("mock") and a.get("source") != "mock_tts"

    full = next((a for a in assets if a.get("asset_type") == "voiceover_audio"
                 and not a.get("scene_id")
                 and a.get("id") == project.get("selected_voiceover_asset_id")), None)
    if _is_real(full):
        local = await _ensure_audio_local(full, work_dir, "voiceover_full")
        if local:
            return local

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


async def _loudnorm_two_pass(final: Path, work_dir: Path) -> None:
    """Measure the muxed file and re-apply loudnorm in linear mode.

    Single-pass dynamic loudnorm (used in the mux filtergraph) can land
    several dB off the -14 LUFS target; a measured linear pass converges.
    Best-effort: on any failure the single-pass output is kept.
    """
    try:
        ok, tail = await _run_ffmpeg([
            FFMPEG_BIN, "-hide_banner", "-i", str(final),
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ])
        m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", tail, re.DOTALL)
        if not m:
            logger.warning("loudnorm measure pass produced no stats — keeping single-pass audio")
            return
        stats = json.loads(m.group(0))
        input_i = float(stats["input_i"])
        if abs(input_i - (-14.0)) <= 1.0:
            return  # already within verify tolerance
        normed = work_dir / f"{final.stem}_loudnorm2.mp4"
        af = (
            "loudnorm=I=-14:TP=-1.5:LRA=11"
            f":measured_I={stats['input_i']}"
            f":measured_TP={stats['input_tp']}"
            f":measured_LRA={stats['input_lra']}"
            f":measured_thresh={stats['input_thresh']}"
            f":offset={stats['target_offset']}"
            ":linear=true"
        )
        ok, err = await _run_ffmpeg([
            FFMPEG_BIN, "-y", "-i", str(final),
            "-map", "0:v", "-map", "0:a",
            "-c:v", "copy",
            "-af", af,
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(normed),
        ])
        if ok and normed.exists() and normed.stat().st_size > 0:
            shutil.move(str(normed), str(final))
            logger.info("loudnorm two-pass applied (measured_I=%s)", stats["input_i"])
        else:
            logger.warning("loudnorm linear pass failed (%s) — keeping single-pass audio", err[-300:])
    except Exception as e:  # noqa: BLE001
        logger.warning("loudnorm two-pass skipped: %s", e)


# CONSTITUTION §4: Normalization: scale=1920:1080:force_original_aspect_ratio=increase,
# crop=1920:1080,fps=30. CROP-FILL. The pad filter is DELETED.
def _ffmpeg_normalise_image(src: Path, duration: float, out: Path) -> list[str]:
    return [
        FFMPEG_BIN, "-y",
        "-loop", "1", "-t", f"{duration:.2f}",
        "-i", str(src),
        "-vf", (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},setsar=1,format=yuv420p"
        ),
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-an",
        str(out),
    ]


# CONSTITUTION §4: Normalization: scale=1920:1080:force_original_aspect_ratio=increase,
# crop=1920:1080,fps=30. CROP-FILL. The pad filter is DELETED.
def _ffmpeg_normalise_video(src: Path, duration: float, out: Path,
                            *, start_offset: float = 0.0) -> list[str]:
    cmd = [FFMPEG_BIN, "-y"]
    if start_offset > 0:
        cmd += ["-ss", f"{start_offset:.2f}"]
    cmd += [
        "-stream_loop", "-1",
        "-i", str(src),
        "-t", f"{duration:.2f}",
        "-vf", (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},setsar=1,format=yuv420p,fps={FPS}"
        ),
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-an",
        str(out),
    ]
    return cmd


# CONSTITUTION §5: Intro card (≤2.5s): title centered, wrapped, ≥80px side
# margins, over hook footage with dark overlay 30%.
def _ffmpeg_intro_from_video(src: Path, duration: float, out: Path, title: str, work_dir: Path) -> list[str]:
    """Create intro clip from video hook footage with dark overlay and title."""
    wrapped = _wrap_text(title, max_chars=30)
    text_file = work_dir / "intro_title.txt"
    text_file.write_text(wrapped, encoding="utf-8")
    font_path = _resolve_font_path()
    return [
        FFMPEG_BIN, "-y",
        "-i", str(src),
        "-t", f"{duration:.2f}",
        "-vf", (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},setsar=1,format=yuv420p,fps={FPS},"
            f"drawbox=y=0:color=black@0.3:w=iw:h=ih:t=fill,"
            f"drawtext=fontfile='{font_path}':"
            f"textfile='{text_file.as_posix()}':fontcolor=white:fontsize=72:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=8"
        ),
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-an",
        str(out),
    ]


def _ffmpeg_intro_from_image(src: Path, duration: float, out: Path, title: str, work_dir: Path) -> list[str]:
    """Create intro clip from static image with dark overlay and title.
    Used only when no video hook footage is available."""
    wrapped = _wrap_text(title, max_chars=30)
    text_file = work_dir / "intro_title.txt"
    text_file.write_text(wrapped, encoding="utf-8")
    font_path = _resolve_font_path()
    return [
        FFMPEG_BIN, "-y",
        "-loop", "1", "-t", f"{duration:.2f}",
        "-i", str(src),
        "-vf", (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},setsar=1,format=yuv420p,"
            f"drawbox=y=0:color=black@0.3:w=iw:h=ih:t=fill,"
            f"drawtext=fontfile='{font_path}':"
            f"textfile='{text_file.as_posix()}':fontcolor=white:fontsize=72:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=8"
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
        intro_fallback_img = await _resolve_thumbnail(sel_thumb, project, work_dir)

        await _set_job(job_id, current_step="preparing_audio", progress=25)
        audio_path = await _resolve_audio(project, scenes, assets, work_dir)

        # Probe true voiceover duration so the video matches audio length —
        # required by the sub-clip plan below.
        await _set_job(job_id, current_step="probing_audio", progress=30)
        audio_duration: Optional[float] = None
        if audio_path and audio_path.exists():
            audio_duration = await _probe_duration_seconds(audio_path)

        # Build per-scene sub-clip plan first (cuts every ≤6s, total = audio
        # length) so each scene resolves one distinct visual per sub-clip.
        ordered_scenes = sorted(scenes, key=lambda x: x.get("scene_number", 0))
        plan = _build_subclip_plan(ordered_scenes, audio_duration)
        plan_by_idx = {p["scene_index"]: p for p in plan}

        await _set_job(job_id, current_step="preparing_scenes", progress=35)
        scene_visuals: list[tuple[list[tuple[Path, str]], dict]] = []
        for i, scene in enumerate(scenes):
            n_clips = len(plan_by_idx.get(i, {"subclips": [4.0]})["subclips"])
            visuals = await _resolve_scene_visuals(scene, assets, project, work_dir, i,
                                                   max_visuals=n_clips)
            scene_visuals.append((visuals, scene))

        # CONSTITUTION §5: Select hook footage — first video clip for intro background.
        hook_path: Optional[Path] = None
        for visuals, scene in scene_visuals:
            for path, kind in visuals:
                if kind == "video":
                    hook_path = path
                    break
            if hook_path:
                break
        if hook_path:
            logger.info("HOOK_FOOTAGE_SELECTED path=%s", hook_path)
        else:
            logger.warning("HOOK_FOOTAGE_FALLBACK: No video footage found; using static image for intro.")

        # ---- rendering ----
        await _set_job(job_id, status="rendering", current_step="encoding_intro", progress=45)
        clips: list[Path] = []

        # Intro clip — CONSTITUTION §5: uses hook footage + dark overlay + title
        intro_out = work_dir / "clip_000_intro.mp4"
        # Audience-facing title from metadata — never the internal project
        # name (e.g. "TEST VIRAL INGREDIENTS - e2e 04:30 UTC" must not burn in)
        raw_title = ((metadata or {}).get("selected_title")
                     or project.get("topic") or project.get("name") or "FacelessForge")
        intro_title = str(raw_title).upper()[:48]
        if hook_path:
            ok, err = await _run_ffmpeg(_ffmpeg_intro_from_video(hook_path, INTRO_DURATION_SECONDS, intro_out, intro_title, work_dir))
        else:
            ok, err = await _run_ffmpeg(_ffmpeg_intro_from_image(intro_fallback_img, INTRO_DURATION_SECONDS, intro_out, intro_title, work_dir))
        if not ok:
            raise RuntimeError(f"intro encode failed: {err[-300:]}")
        clips.append(intro_out)

        # Scene clips — multiple sub-clips per scene, cycling through the
        # scene's distinct sources so sub-clip boundaries are real cuts.
        total_subclips = sum(len(p["subclips"]) for p in plan)
        emitted = 0
        for i, (visuals, scene) in enumerate(scene_visuals):
            sub_plan = plan_by_idx.get(i, {"subclips": [4.0], "target": 4.0})
            subclips = sub_plan["subclips"]
            dur_cache: dict[str, Optional[float]] = {}
            use_count: dict[int, int] = {}
            last_used: dict[int, float] = {}
            t_cursor = 0.0
            for j, dur in enumerate(subclips):
                emitted += 1
                await _set_job(
                    job_id,
                    current_step=f"encoding_scene_{i+1:02d}_clip_{j+1:02d}",
                    progress=min(85, 45 + int(35 * emitted / max(1, total_subclips))),
                )
                out = work_dir / f"clip_{i+1:03d}_{j:02d}.mp4"
                # No source repeats within 20s when avoidable — pick the
                # first visual not used in the last 20s, else the least
                # recently used one.
                chosen = next(
                    (vi for vi in range(len(visuals))
                     if last_used.get(vi) is None or (t_cursor - last_used[vi]) >= 20.0),
                    None,
                )
                if chosen is None:
                    chosen = min(range(len(visuals)), key=lambda vi: last_used.get(vi, -1e9))
                path, kind = visuals[chosen]
                # Seek pass advances each time the same source repeats
                pass_num = use_count.get(chosen, 0)
                use_count[chosen] = pass_num + 1
                last_used[chosen] = t_cursor
                t_cursor += dur
                if kind == "video":
                    if str(path) not in dur_cache:
                        dur_cache[str(path)] = await _probe_duration_seconds(path)
                    src_dur = dur_cache[str(path)]
                    if src_dur and src_dur > dur:
                        offset = (pass_num * dur) % max(0.1, src_dur - dur)
                    else:
                        offset = 0.0
                    cmd = _ffmpeg_normalise_video(path, dur, out, start_offset=offset)
                else:
                    cmd = _ffmpeg_normalise_image(path, dur, out)
                ok, err = await _run_ffmpeg(cmd)
                if not ok:
                    raise RuntimeError(f"scene {i+1} clip {j+1} encode failed: {err[-300:]}")
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

        # ---- subtitle burn-in: word-synchronised from Whisper STT ----
        burned_out = silent_out
        burn_enabled = os.environ.get("RENDER_BURN_SUBTITLES", "true").lower() in ("1", "true", "yes")
        if burn_enabled and audio_path and audio_path.exists():
            await _set_job(job_id, current_step="transcribing_audio", progress=89)
            words = await transcribe_words(audio_path, language="en")
            srt_path = work_dir / "captions.srt"
            try:
                if words:
                    # CONSTITUTION §5: 6-8 word chunks (default 7)
                    write_srt_from_words(
                        words, srt_path,
                        intro_offset_seconds=INTRO_DURATION_SECONDS,
                        words_per_cue=7,
                    )
                else:
                    # No STT — chunk the narration text itself into word cues
                    # (never per-scene caption titles)
                    narration_text = (script or {}).get("full_script") or ""
                    if narration_text and audio_duration:
                        write_srt_from_text(
                            narration_text, srt_path,
                            total_seconds=audio_duration,
                            intro_offset_seconds=INTRO_DURATION_SECONDS,
                            words_per_cue=7,
                        )
                    else:
                        write_srt(scenes, srt_path,
                                  intro_offset_seconds=INTRO_DURATION_SECONDS)
            except Exception as e:  # noqa: BLE001
                logger.warning("SRT generation failed (%s) — skipping burn-in", e)
                srt_path = None
            if srt_path and srt_path.exists() and srt_path.stat().st_size > 0:
                await _set_job(job_id, current_step="burning_subtitles", progress=91)
                burned_out = work_dir / "video_subbed.mp4"
                srt_escaped = srt_path.as_posix().replace(":", r"\:").replace("'", r"\'")
                # CONSTITUTION §5: Exact subtitle style spec
                sub_style = (
                    "FontName=DejaVu Sans,FontSize=15,Bold=0,Alignment=2,MarginV=45,"
                    "BorderStyle=3,OutlineColour=&H90000000,PrimaryColour=&H00FFFFFF"
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

        # Mux audio (voiceover + optional music bed + loudnorm)
        await _set_job(job_id, current_step="muxing_audio", progress=94)
        out_dir = STATIC_RENDERS / project_id
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / f"{job_id}.mp4"

        music_path = _resolve_music_bed()
        use_music = bool(music_path and music_path.exists()
                         and os.environ.get("RENDER_MUSIC_BED", "true").lower() in ("1", "true", "yes"))

        # CONSTITUTION §6: Music mixed UNDER narration at volume 0.12,
        # fade out last 2s, loudnorm final mix.
        if audio_path and audio_path.exists() and use_music:
            vo_dur = audio_duration or (await _probe_duration_seconds(audio_path)) or 0.0
            fade_start = max(0.0, vo_dur - 2.0)
            cmd = [
                FFMPEG_BIN, "-y",
                "-i", str(burned_out),
                "-i", str(audio_path),
                "-stream_loop", "-1", "-i", str(music_path),
                "-filter_complex",
                # Music bed at 0.12 amplitude, fade out last 2s
                f"[2:a]volume=0.12,afade=t=out:st={fade_start:.2f}:d=2[bed];"
                # Voiceover + music mix, duration=first (voiceover length)
                f"[1:a][bed]amix=inputs=2:duration=first:normalize=0[aout];"
                # Normalize final mix to -14 LUFS
                f"[aout]loudnorm=I=-14:TP=-1.5:LRA=11[aout_norm]",
                "-map", "0:v", "-map", "[aout_norm]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                str(final),
            ]
        elif audio_path and audio_path.exists():
            # Voiceover only — still apply loudnorm
            cmd = [
                FFMPEG_BIN, "-y",
                "-i", str(burned_out),
                "-i", str(audio_path),
                "-filter_complex",
                f"[1:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout_norm]",
                "-map", "0:v", "-map", "[aout_norm]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                str(final),
            ]
        elif use_music:
            # Music only — still apply loudnorm
            music_dur = await _probe_duration_seconds(music_path) or 0.0
            fade_start = max(0.0, music_dur - 2.0) if music_dur > 0 else 0.0
            cmd = [
                FFMPEG_BIN, "-y",
                "-i", str(burned_out),
                "-stream_loop", "-1", "-i", str(music_path),
                "-filter_complex",
                f"[1:a]volume=0.12,afade=t=out:st={fade_start:.2f}:d=2,loudnorm=I=-14:TP=-1.5:LRA=11[aout_norm]",
                "-map", "0:v", "-map", "[aout_norm]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                str(final),
            ]
        else:
            # CONSTITUTION §7: Silent fallbacks must log WARNING.
            logger.warning(
                "RENDER_SILENT_FALLBACK: No voiceover or music available. "
                "Video will have silent audio track. "
                "Consider adding voiceover or music to meet §6 silence-gap requirements."
            )
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

        # CONSTITUTION §6: single-pass dynamic loudnorm can miss the -14 LUFS
        # target (verify check g). Measure the muxed file and re-apply a
        # linear second pass when the result is off-target.
        if audio_path and audio_path.exists() or use_music:
            await _loudnorm_two_pass(final, work_dir)

        # Probe duration via ffprobe
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
            est = INTRO_DURATION_SECONDS
            for s in scenes:
                est += max(2.0, float((s.get("end_time") or 0) - (s.get("start_time") or 0)) or 4.0)
            duration = round(est, 2)

        # ---- PRESERVE narration for verification before workdir cleanup ----
        narration_for_verify: Optional[Path] = None
        if audio_path and Path(audio_path).exists():
            narration_for_verify = out_dir / f"{job_id}_narration{Path(audio_path).suffix}"
            try:
                shutil.copy2(audio_path, narration_for_verify)
            except Exception:
                narration_for_verify = audio_path

        # Persist to storage backend BEFORE cleanup — final must exist
        store = get_storage()
        key = f"renders/{project_id}/{final.name}"
        try:
            saved = store.save_file(final, key, content_type="video/mp4")
        except Exception as e:
            raise RuntimeError(f"storage upload failed: {e}")

        # === CONSTITUTION VERIFICATION GATE — Critical Gap Fix ===
        verification_passed = True
        verification_result = None
        
        if VERIFY_AVAILABLE and _verify_render_constitution:
            try:
                await _set_job(job_id, status="verifying", current_step="verifying_constitution_a-h", progress=96)
                logger.info(f"[VERIFY] Starting constitution verification for {saved.url}")
                verification_result = await _verify_render_constitution(
                    url=saved.url,
                    narration_path=str(narration_for_verify) if narration_for_verify else None,
                    scene_count=len(scenes),
                    cleanup=True,
                    local_fallback_path=str(final),
                )
                verification_passed = bool(verification_result.get("overall_passed"))
                if not verification_passed:
                    logger.error(f"[VERIFY] FAILED — {verification_result}")
                    await _set_job(job_id, status="failed_verification", current_step="failed_verification", progress=100,
                        output_path=str(saved.file_path) if saved.file_path else None, output_url=saved.url,
                        output_relative_url=saved.preview_path, output_storage_mode=store.mode, output_storage_key=saved.key,
                        file_size=(saved.file_path.stat().st_size if saved.file_path and saved.file_path.exists() else final.stat().st_size if final.exists() else None),
                        duration=duration, completed_at=_now(),
                        error_message=f"Constitution verification failed: {verification_result.get('report', {})}",
                        verification=verification_result)
                    await db.projects.update_one({"id": project_id}, {"$set": {"status": "FAILED_VERIFICATION", "updated_at": _now()}})
                    try:
                        shutil.rmtree(work_dir, ignore_errors=True)
                    except Exception:
                        pass
                    if narration_for_verify and narration_for_verify.exists() and narration_for_verify.parent == out_dir:
                        try:
                            narration_for_verify.unlink(missing_ok=True)
                        except Exception:
                            pass
                    return
                else:
                    logger.info(f"[VERIFY] PASSED all checks a-h")
            except Exception as ve:
                logger.exception(f"[VERIFY] Verification crashed: {ve}")
                await _set_job(job_id, status="failed_verification", current_step="failed_verification", progress=100,
                    output_path=str(saved.file_path) if saved.file_path else None, output_url=saved.url,
                    output_relative_url=saved.preview_path, output_storage_mode=store.mode, output_storage_key=saved.key,
                    file_size=(saved.file_path.stat().st_size if saved.file_path and saved.file_path.exists() else final.stat().st_size if final.exists() else None),
                    duration=duration, completed_at=_now(),
                    error_message=f"Verification exception: {ve}", verification={"error": str(ve)})
                await db.projects.update_one({"id": project_id}, {"$set": {"status": "FAILED_VERIFICATION", "updated_at": _now()}})
                try:
                    shutil.rmtree(work_dir, ignore_errors=True)
                except Exception:
                    pass
                return
        else:
            logger.warning("[VERIFY] verify.py not available — skipping constitution check (DEV ONLY)")

        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass
        if narration_for_verify and narration_for_verify.exists() and narration_for_verify.parent == out_dir:
            try:
                narration_for_verify.unlink(missing_ok=True)
            except Exception:
                pass

        await _set_job(job_id, status="completed", current_step="completed", progress=100,
            output_path=str(saved.file_path) if saved.file_path else None, output_url=saved.url,
            output_relative_url=saved.preview_path, output_storage_mode=store.mode, output_storage_key=saved.key,
            file_size=(saved.file_path.stat().st_size if saved.file_path and saved.file_path.exists() else final.stat().st_size if final.exists() else None),
            duration=duration, completed_at=_now(), error_message=None, verification=verification_result)
        await db.projects.update_one({"id": project_id}, {"$set": {"status": "COMPLETED", "rendered_video_asset_id": job_id, "updated_at": _now()}})
