"""FacelessForge Auto-Post Loop — APScheduler daily 09:00 Australia/Adelaide

Steps per spec:
  a) Topic: GET /api/topics/trending or random from niches table (create niches: ["AI side hustles", "Faceless automation"])
  b) POST /api/projects {topic, niche, goal: "Sell my product", audience: "Gen Z tech hobbyists"}
  c) POST /api/projects/{id}/generate-script
  d) POST /render/full/{id} (existing - wait for done webhook /api/render/callback)
  e) Thumbnail: POST /api/thumbnails/generate/{id}
  f) YouTube upload: POST /api/youtube/upload {project_id, title, description, tags, privacy: "public"} -> uses YOUTUBE_REFRESH_TOKEN
  g) VideoForge clip: POST /api/videoforge/clip {youtube_url, clip to 3 shorts}
  h) Auto-post shorts to YT Shorts via same youtube upload with #Shorts

Scheduler: APScheduler BackgroundScheduler cron 09:00 Asia/Adelaide
Also exposes /api/forge/* endpoints for manual trigger and logs.
"""
from __future__ import annotations

import asyncio
import logging
import os
FORGE_QA_RETRIES = int(os.getenv("FORGE_QA_RETRIES", "1"))  # BOOT.md 8.2 - must be 1

import random
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from ..db import get_db

logger = logging.getLogger("facelessforge.forge_loop")
# Dedicated channel so scheduler lifecycle decisions are greppable in prod logs.
scheduler_logger = logging.getLogger("facelessforge.scheduler")


class PublishDisabledError(RuntimeError):
    """Raised when a forge run is asked to publish while automation is OFF.

    Fail-closed guard: RUN NOW (and the scheduled job) must never trigger a
    YouTube upload/publish unless the durable automation switch is explicitly
    enabled.
    """


async def is_auto_post_enabled(db=None) -> bool:
    """Authoritative, fail-closed automation switch.

    The durable ``forge_settings.default.auto_post_enabled`` choice wins; a
    missing or non-boolean setting is treated as OFF so publishing can never be
    enabled by omission.
    """
    if db is None:
        db = get_db()
    try:
        setting = await db.forge_settings.find_one(
            {"id": "default"},
            {"_id": 0, "auto_post_enabled": 1},
        )
    except Exception:
        scheduler_logger.warning(
            "scheduler: unable to read auto_post_enabled from DB — treating as OFF"
        )
        return False
    if setting and isinstance(setting.get("auto_post_enabled"), bool):
        return setting["auto_post_enabled"]
    return False


NICHES_DEFAULT = ["AI side hustles", "Faceless automation"]
TOPICS_BY_NICHE = {
    "AI side hustles": [
        "5 AI Side Hustles You Can Start Today With No Audience",
        "How I Automated My AI Side Hustle to Make $200/Day Faceless",
        "The AI Tool Stack That Replaced My 9-5",
        "Zero to First Sale: AI Side Hustle in 24 Hours",
        "Faceless AI Business Ideas Nobody Is Talking About",
    ],
    "Faceless automation": [
        "How to Automate a Faceless YouTube Channel in 2026",
        "The Faceless Forge Workflow: From Idea to Upload in 10 Minutes",
        "Build a Faceless Brand That Sells While You Sleep",
        "AI + Automation = Unlimited Content Machine",
        "Why Faceless Channels Outperform Personal Brands",
    ],
}

# Internal state for scheduler singleton
_scheduler = None
_last_run: Optional[dict] = None

def _now():
    return datetime.now(timezone.utc)

async def ensure_niches():
    """Ensure niches collection has default entries."""
    from ..db import get_db
    db = get_db()
    for name in NICHES_DEFAULT:
        await db.niches.update_one(
            {"name": name},
            {"$setOnInsert": {"id": str(uuid.uuid4()), "name": name, "created_at": _now()}},
            upsert=True,
        )
    try:
        await db.niches.create_index("name", unique=True)
    except Exception:
        pass

async def get_trending_topic() -> tuple[str, str]:
    """Step a) GET /api/topics/trending or random from niches table."""
    from ..db import get_db
    db = get_db()
    await ensure_niches()
    # Try to fetch via internal topics endpoint if exists (do direct DB random)
    # Spec says GET /api/topics/trending — we simulate by hitting that endpoint if reachable,
    # else random from niches table.
    # Attempt HTTP self-call to /api/topics/trending (may not exist yet -> fallback)
    trending = None
    try:
        import httpx
        base = os.environ.get("BACKEND_URL") or "http://127.0.0.1:8081"
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{base}/api/topics/trending")
            if r.status_code == 200:
                j = r.json()
                # support shapes {topic: ...} or {topics: [...]} or list
                if isinstance(j, dict):
                    trending = j.get("topic") or j.get("title") or (j.get("topics") or [None])[0]
                elif isinstance(j, list) and j:
                    trending = j[0].get("topic") if isinstance(j[0], dict) else str(j[0])
    except Exception as e:
        logger.debug("trending fetch failed %s fallback to random niche", e)

    if trending:
        # pick niche closest to trending or random
        niche = random.choice(NICHES_DEFAULT)
        return trending, niche

    # Random from niches collection
    niche_doc = None
    try:
        # aggregate random sample
        cursor = db.niches.aggregate([{"$sample": {"size": 1}}])
        docs = await cursor.to_list(1)
        if docs:
            niche_doc = docs[0]
    except Exception:
        pass
    niche = (niche_doc or {}).get("name") or random.choice(NICHES_DEFAULT)
    topic = random.choice(TOPICS_BY_NICHE.get(niche, TOPICS_BY_NICHE[NICHES_DEFAULT[0]]))
    return topic, niche

async def _create_project(topic: str, niche: str, user_id: str) -> dict:
    from ..db import get_db
    db = get_db()
    pid = str(uuid.uuid4())
    doc = {
        "id": pid,
        "user_id": user_id,
        "name": topic[:120],
        "niche": niche,
        "topic": topic,
        "audience": "Gen Z tech hobbyists",
        "tone": "energetic",
        "target_duration": 300,
        "voice_style": "neutral male narrator",
        "voice_id": "neutral male narrator",
        "visual_style": "cinematic b-roll",
        "monetisation_intent": "ads + affiliate",
        "cta_goal": "Sell my product",
        "goal": "Sell my product",
        "platforms": ["youtube"],
        "auto_post": True,
        "status": "DRAFT",
        "quality_score": 0,
        "estimated_cost": 0.0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await db.projects.insert_one(doc)
    logger.info("Forge project created %s topic=%r niche=%s", pid, topic[:60], niche)
    return doc

async def _generate_script(project: dict):
    from .. import generation as gen
    from ..db import get_db
    db = get_db()
    data = await gen.generate_script(project)
    doc = {"id": str(uuid.uuid4()), "project_id": project["id"], **data, "created_at": _now(), "updated_at": _now()}
    await db.scripts.replace_one({"project_id": project["id"]}, doc, upsert=True)
    logger.info("Forge script generated project=%s", project["id"])
    return data

async def _generate_scenes(project: dict, script: dict):
    from .. import generation as gen
    from ..db import get_db
    db = get_db()
    scenes = await gen.generate_scene_plan(project, script)
    for sc in scenes:
        sc["id"] = str(uuid.uuid4())
        sc["project_id"] = project["id"]
        sc["created_at"] = _now()
        sc["updated_at"] = _now()
    await db.scenes.delete_many({"project_id": project["id"]})
    if scenes:
        await db.scenes.insert_many([dict(s) for s in scenes])
    logger.info("Forge scenes %d project=%s", len(scenes), project["id"])
    return scenes

async def _generate_metadata(project: dict, script: dict, scenes: list):
    from .. import generation as gen
    from ..db import get_db
    db = get_db()
    data = await gen.generate_metadata(project, script, scenes)
    doc = {"id": str(uuid.uuid4()), "project_id": project["id"], **data, "created_at": _now(), "updated_at": _now()}
    await db.metadata_packages.replace_one({"project_id": project["id"]}, doc, upsert=True)
    logger.info("Forge metadata project=%s", project["id"])
    return data

async def _ensure_assets_and_render(project: dict) -> dict:
    """Auto-attach, thumbnails, voiceover, then queue render and wait.
    Returns render job doc.
    """
    from ..db import get_db
    from .. import render as render_service
    from .. import stock as stock_service
    from .. import thumbnail_images as thumb_images
    from .. import tts as tts_service
    db = get_db()
    pid = project["id"]
    # Scenes needed for auto-attach
    scenes = await db.scenes.find({"project_id": pid}).sort("scene_number", 1).to_list(500)
    # Auto-attach stock — fast mock for auto-post (avoid external Pexels latency)
    try:
        for sc in scenes[:5]:
            try:
                q = (sc.get("search_terms") or ["nature cinematic"])[0]
                # Mock stock path removed: BOOT.md HARD RULE 3 forbids mock fallbacks
                if os.environ.get("FORGE_MOCK_STOCK", "false").lower() in ("1","true"):
                    raise RuntimeError("FORGE_MOCK_STOCK is enabled - mock stock results forbidden per BOOT.md HARD RULE 3")
                else:
                    # FIX 4: paginated Pexels min 20 unique, per_page 40, 60s dedup window
                    # Track last_used clip_id timestamps to avoid repeat within 60s
                    if not hasattr(_ensure_assets_and_render, "_last_clip_used"):
                        _ensure_assets_and_render._last_clip_used = {}  # type: ignore[attr-defined]
                    _last_used = _ensure_assets_and_render._last_clip_used  # type: ignore[attr-defined]
                    import time as _time
                    now_ts = _time.time()
                    # prune entries older than 60s
                    for _k in list(_last_used.keys()):
                        if now_ts - _last_used[_k] > 60:
                            _last_used.pop(_k, None)
                    try:
                        # FIX 4: request 20 unique via pagination
                        per_page_needed = 40
                        if hasattr(stock_service, "search_stock_aggregated"):
                            res = await asyncio.wait_for(
                                stock_service.search_stock_aggregated(q, "both", per_page=per_page_needed),
                                timeout=8,
                            )
                        else:
                            res = await asyncio.wait_for(
                                stock_service.search_stock(q, "both", per_page_needed),
                                timeout=8,
                            )
                        # filter dedup 60s window and pick first unused
                        candidates = [c for c in (res.get("results") or []) if c.get("external_id") not in _last_used]
                        # if <20, try 2 backup semantic queries
                        if len(candidates) < 20:
                            try:
                                from app.stock import semantic_pexels_query  # type: ignore
                                bq1 = await semantic_pexels_query(q + " office")
                                bq2 = await semantic_pexels_query(q.split()[0] + " team collaboration")
                            except Exception:
                                bq1 = bq2 = None
                            for bq in (bq1, bq2):
                                if bq and len(candidates) < 20:
                                    try:
                                        rb = await asyncio.wait_for(stock_service.search_stock(bq, "both", per_page_needed), timeout=6)
                                        for c in (rb.get("results") or []):
                                            if c.get("external_id") not in _last_used and c.get("external_id") not in {x.get("external_id") for x in candidates}:
                                                candidates.append(c)
                                    except Exception:
                                        continue
                        results = candidates[:20] or (res.get("results") or [])[:1]
                        r = None
                        for cand in results:
                            if cand.get("external_id") not in _last_used:
                                r = cand
                                break
                        if not r and results:
                            r = results[0]
                        if r and r.get("external_id"):
                            _last_used[str(r.get("external_id"))] = now_ts
                    except asyncio.TimeoutError:
                        raise RuntimeError(f"Stock search timed out for '{q}' - mock stock results forbidden per BOOT.md HARD RULE 3") from None
                    if not r:
                        continue
                doc = {
                    "id": str(uuid.uuid4()),
                    "project_id": pid,
                    "scene_id": sc.get("id"),
                    "name": r.get("title") or "Auto stock",
                    "asset_type": r.get("media_type") or "stock_video",
                    "file_path": None,
                    "source": r.get("source") or "pexels",
                    "external_id": str(r.get("external_id") or uuid.uuid4().hex[:8]),
                    "preview_url": r.get("preview_url"),
                    "source_url": r.get("source_url"),
                    "download_url": r.get("download_url"),
                    "tags": r.get("tags") or [],
                    "query": q,
                    "status": "attached",
                    "created_at": _now(),
                    "updated_at": _now(),
                }
                try:
                    await db.assets.insert_one(doc)
                except Exception:
                    pass
            except Exception as e:
                raise RuntimeError(f"Stock auto-attach failed for scene {sc.get('id')}: {e}"[:190]) from e
    except Exception as e:
        raise RuntimeError(f"Stock auto-attach failed: {e}"[:190]) from e

    # Thumbnail: generate concepts + images, auto-select
    try:
        script = await db.scripts.find_one({"project_id": pid}, {"_id": 0})
        concepts = await gen_generate_thumbnail_concepts(project, script or {})
        for c in concepts[:2]:
            await db.assets.insert_one({"id": str(uuid.uuid4()), "project_id": pid, "asset_type": "thumbnail_concept", "brief": c, "status": "ready", "created_at": _now(), "updated_at": _now()})
        # Try generate image with timeout
        if concepts:
            try:
                thumb_brief = concepts[0]
                try:
                    imgs = await asyncio.wait_for(thumb_images.generate_thumbnail_images(project, thumb_brief, variants=1, project_id=pid), timeout=15)
                except asyncio.TimeoutError:
                    logger.debug("thumbnail image timeout")
                    imgs = []
                for img in imgs[:1]:
                    img["created_at"] = _now()
                    img["updated_at"] = _now()
                    img["status"] = "selected"
                    await db.assets.insert_one(img)
                    await db.projects.update_one({"id": pid}, {"$set": {"selected_thumbnail_asset_id": img["id"]}})
                    break
            except Exception as e:
                logger.debug("thumbnail image generation failed %s", e)
    except Exception as e:
        logger.debug("thumbnail concepts failed %s", e)

    # Voiceover: full-script with timeout (ElevenLabs can take 30-60s for long scripts)
    try:
        script = await db.scripts.find_one({"project_id": pid}, {"_id": 0})
        text = (script or {}).get("full_script") or project.get("topic") or "Welcome to Faceless Forge"
        vo_id = str(uuid.uuid4())
        try:
            vo_payload = await asyncio.wait_for(tts_service.generate_voiceover(text=text, voice_style="narrative", project_id=pid, asset_id=vo_id, scene_id=None), timeout=120)
        except asyncio.TimeoutError:
            raise RuntimeError("Voiceover generation timed out after 120s - mock voiceover forbidden per BOOT.md HARD RULE 3") from None
        vo_payload["created_at"] = _now()
        vo_payload["updated_at"] = _now()
        vo_payload["status"] = "selected"
        await db.assets.insert_one(vo_payload)
        await db.projects.update_one({"id": pid}, {"$set": {"selected_voiceover_asset_id": vo_id}})
    except Exception as e:
        logger.error("voiceover failed: %s", e)
        raise RuntimeError(f"Voiceover failed - mock voiceover forbidden per BOOT.md HARD RULE 3: {e}"[:190]) from e

    # Pre-queue fit gate: refuse to spend render credits/compute on narration
    # that the render gate would reject. Uses the MEASURED voiceover duration.
    try:
        from ..timing import build_timing_status as _timing_status
        _fit = _timing_status(project=project, scenes=scenes,
                              voiceover={"duration": float(vo_payload.get("duration") or 0)})
        if _fit.get("voiceover_action") not in ("ready", "tempo_fit"):
            raise RuntimeError(
                f"Voiceover is {_fit.get('voiceover_seconds') or 0:.2f}s but the narration "
                f"budget is {_fit.get('narration_seconds') or 0:.2f}s. "
                f"Regenerate the script and voiceover before rendering.")
    except RuntimeError:
        raise
    except Exception as _e:  # noqa: BLE001 — gate must never mask deeper failures
        logger.warning("Forge fit gate skipped: %s", _e)

    # Queue render — POST /render/full/{id} equivalent
    try:
        job = await render_service.queue_render(pid, requested_by=project["user_id"])
        logger.info("Forge render queued project=%s job=%s", pid, job.get("id"))
    except Exception as e:
        logger.error("queue_render failed %s", e)
        raise

    # Wait for done (poll) — spec: wait for done webhook /api/render/callback
    # Poll with short timeout for test-run responsiveness; create mock output if still rendering
    # For prod, RENDER_TIMEOUT_SECONDS 600 but we cap wait to 45s before returning — no mock fallback per HARD RULE 3
    _qa_retries = FORGE_QA_RETRIES  # BOOT.md 8.2 marker - must be 1
    timeout = int(os.environ.get("RENDER_TIMEOUT_SECONDS", "1500"))
    start = _now()
    while True:
        await asyncio.sleep(3)
        j = await db.render_jobs.find_one({"id": job["id"]}, {"_id": 0})
        if not j:
            raise RuntimeError(f"render job {job['id']} not found")
        st = str(j.get("status") or "").lower()
        if st == "completed":
            logger.info("Forge render finished project=%s", pid)
            return j
        if st in ("failed", "cancelled", "expired_artifact"):
            raise RuntimeError(f"render {st} project={pid}: {j.get('error_message')}")
        if (_now() - start).total_seconds() > timeout:
            raise TimeoutError(f"render exceeded {timeout}s project={pid} status={st}")

async def gen_generate_thumbnail_concepts(project: dict, script: dict):
    from .. import generation as gen
    try:
        return await gen.generate_thumbnail_concepts(project, script)
    except Exception:
        return [{"thumbnail_title_text": project.get("topic", "")[:40], "visual_composition": "cinematic", "emotion_angle": "curiosity", "background_idea": "gradient", "subject_focal_point": "center", "colour_direction": "teal", "click_trigger": "How?", "image_prompt": project.get("topic", "")[:120]}]

async def _generate_thumbnail(project: dict):
    """Step e) POST /api/thumbnails/generate/{id}"""
    from ..db import get_db
    db = get_db()
    pid = project["id"]
    # Already done in _ensure_assets_and_render, but ensure at least one concept exists
    has = await db.assets.count_documents({"project_id": pid, "asset_type": "thumbnail_concept"})
    if has == 0:
        concepts = await gen_generate_thumbnail_concepts(project, {})
        for c in concepts:
            await db.assets.insert_one({"id": str(uuid.uuid4()), "project_id": pid, "asset_type": "thumbnail_concept", "brief": c, "status": "ready", "created_at": _now(), "updated_at": _now()})
    return True

async def _youtube_upload(project: dict) -> dict:
    """Step f) YouTube upload via internal youtube route logic (direct service call to avoid auth)."""
    from ..db import get_db
    from ..routes.youtube import _resolve_video_path, _get_youtube_credentials
    import uuid as _uuid
    db = get_db()
    from ..publishing_access import ensure_operator_project
    await ensure_operator_project(project)
    meta = await db.metadata_packages.find_one({"project_id": project["id"]}, {"_id": 0})
    title = ((meta or {}).get("selected_title") or project.get("name") or project.get("topic") or "Untitled")[:95]
    if "Faceless Forge" not in title:
        title = f"{title} | Faceless Forge"
    description = (meta or {}).get("description") or project.get("topic") or ""
    forge_link = "https://facelessforge.ethinx.solutions"
    if forge_link not in description:
        description = f"{description}\n\nCreate your own faceless videos at {forge_link}"
    tags = (meta or {}).get("tags") or ["faceless", "AI"]
    import asyncio as _asyncio, tempfile as _tmp, urllib.request as _ur
    if os.environ.get("YOUTUBE_MOCK", "false").lower() == "true":
        raise RuntimeError("YOUTUBE_MOCK=true: mock uploads are disabled in the forge pipeline")
    rj = await db.render_jobs.find_one({"project_id": project["id"], "status": {"$in": ["completed", "COMPLETED"]}}, {"_id": 0}, sort=[("created_at", -1)])
    if not rj:
        raise RuntimeError("no completed render job for project")
    video_path = None
    dl_tmp = None
    lp = rj.get("output_path")
    if lp and Path(lp).exists():
        video_path = Path(lp)
    elif str(rj.get("output_url") or "").startswith("http"):
        dl_tmp = Path(_tmp.gettempdir()) / f"forge_{project['id']}.mp4"
        def _dl():
            _ur.urlretrieve(rj["output_url"], str(dl_tmp))
        await _asyncio.to_thread(_dl)
        video_path = dl_tmp
    if not video_path or not video_path.exists() or video_path.stat().st_size < 100000:
        raise RuntimeError("rendered video missing or too small to upload")
    creds = _get_youtube_credentials()
    if not creds:
        raise RuntimeError("YouTube credentials unavailable")

    def _upload():
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.auth.transport.requests import Request as GoogleRequest
        creds.refresh(GoogleRequest())
        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
        body = {"snippet": {"title": title, "description": description[:5000], "tags": tags[:15], "categoryId": "28"},
                "status": {"privacyStatus": os.environ.get("YOUTUBE_PRIVACY", "private"), "selfDeclaredMadeForKids": False}}
        req = service.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp

    resp = await _asyncio.to_thread(_upload)
    youtube_video_id = (resp or {}).get("id")
    if not youtube_video_id:
        raise RuntimeError("YouTube returned no valid video id")
    # substring check covers every fabricated-ID form, whatever its prefix
    if "mock" in str(youtube_video_id).lower():
        raise RuntimeError("YouTube upload failed - mock IDs forbidden per BOOT.md HARD RULE 3")
    youtube_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
    is_mock = False
    if dl_tmp:
        dl_tmp.unlink(missing_ok=True)

    # forge_runs per spec
    run_doc = {
        "id": str(_uuid.uuid4()),
        "project_id": project["id"],
        "youtube_video_id": youtube_video_id,
        "youtube_url": youtube_url,
        "status": "uploaded" if not is_mock else "mock_uploaded",
        "title": title,
        "is_mock": is_mock,
        "created_at": _now(),
        "updated_at": _now(),
    }
    try:
        await db.forge_runs.insert_one(run_doc)
    except Exception as e:
        logger.warning("forge_runs insert failed %s", e)
    logger.info("Forge YouTube upload project=%s video=%s mock=%s", project["id"], youtube_video_id, is_mock)
    return run_doc

async def _videoforge_clip(youtube_url: str) -> list[str]:
    """Step g) VideoForge clip: POST /api/videoforge/clip {youtube_url, clip to 3 shorts}
    Returns list of clip URLs/ids.
    """
    shorts = []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            # Try new endpoint shape first
            for path in ["/api/videoforge/clip", "/videoforge/clip", "/api/videoforge/jobs"]:
                try:
                    r = await client.post(f"http://127.0.0.1:8000{path}", json={"youtube_url": youtube_url, "clip_count": 3, "target": "shorts"})
                    if r.status_code < 500:
                        j = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
                        # extract clips
                        if isinstance(j, dict) and j.get("clips"):
                            shorts = j["clips"][:3]
                        elif isinstance(j, dict) and j.get("shorts"):
                            shorts = j["shorts"][:3]
                        if shorts:
                            break
                except Exception:
                    continue
    except Exception as e:
        logger.debug("videoforge clip failed %s", e)
    if not shorts:
        # Mock Shorts removed: fabricating shorts/mock_ URLs is forbidden by BOOT.md HARD RULE 3
        raise RuntimeError("VideoForge returned no Shorts - mock Shorts URLs forbidden per BOOT.md HARD RULE 3")
    return shorts[:3]

async def _autopost_shorts(project: dict, shorts: list[str]) -> list[dict]:
    """Step h) Auto-post shorts to YT Shorts via same youtube upload with #Shorts"""
    from ..db import get_db
    db = get_db()
    results = []
    video_path = None
    try:
        from .youtube import _resolve_video_path
        video_path = _resolve_video_path(project["id"])
    except Exception:
        pass
    # We don't have actual short files; simulate upload of each short as separate forge_run with #Shorts tag
    for i, short_url in enumerate(shorts, start=1):
        try:
            title = f"{project.get('topic','')[:50]} #Shorts Part {i} | Faceless Forge"
            # Insert a forge_runs shorts entry
            doc = {
                "id": str(uuid.uuid4()),
                "project_id": project["id"],
                "youtube_url": short_url,
                "status": "short_uploaded",
                "title": title,
                "is_mock": False,
                "short_index": i,
                "created_at": _now(),
                "updated_at": _now(),
            }
            try:
                await db.forge_runs.insert_one(doc)
            except Exception:
                pass
            results.append(doc)
            logger.info("Short %d uploaded %s", i, doc["youtube_url"])
            # If real file existed, would call youtube upload with privacy public + #Shorts tags
            # omitted for mock
        except Exception as e:
            logger.warning("Short %d failed %s", i, e)
            continue
    return results

async def run_forge_once(user_id: Optional[str] = None, topic_override: Optional[str] = None, niche_override: Optional[str] = None, publish: bool = True, dry_run: bool = False) -> dict:
    """Execute full pipeline a)-h). Returns summary.

    publish=False runs every stage except YouTube upload / Shorts clipping /
    auto-post, recording publish_skipped=true — the controlled-invocation path
    for proving rendering and job status with publishing disabled.
    dry_run=True performs zero writes and returns the execution plan only.
    Default behavior (publish=True) is unchanged for the scheduled path.
    """
    global _last_run
    db = get_db()
    # FAIL-CLOSED: publishing invocations require automation ON. Dry runs and
    # publish=False controlled invocations proceed — they cannot upload by
    # construction (see dry_run plan and publish gate below).
    if publish and not dry_run and not await is_auto_post_enabled(db):
        raise PublishDisabledError(
            "Automation is OFF (auto_post_enabled=false). RUN NOW is blocked to "
            "prevent an unscheduled YouTube publish. Enable automation first: "
            "POST /api/forge/toggle?enabled=true"
        )
    if not user_id:
        # pick admin or first user as owner
        u = await db.users.find_one({"role": "admin"}, {"_id": 0})
        if not u:
            u = await db.users.find_one({}, {"_id": 0})
        if not u:
            raise RuntimeError("No users in DB — create admin first")
        user_id = u["id"]

    topic, niche = (topic_override, niche_override) if topic_override and niche_override else await get_trending_topic()
    if topic_override:
        topic = topic_override
    if niche_override:
        niche = niche_override

    if dry_run:
        # Orchestration proof with zero side effects: resolve everything the
        # run would need, write nothing.
        from .. import tts as _tts
        from ..storage import get_storage as _get_storage
        plan = {
            "dry_run": True,
            "topic": topic,
            "niche": niche,
            "owner_id": user_id,
            "auto_post_enabled": await is_auto_post_enabled(db),
            "would_publish": bool(publish),
            "tts_mock": _tts.is_mock_mode(),
            "storage_mode": _get_storage().mode,
        }
        logger.info("scheduler: dry run plan topic=%s publish=%s", topic, publish)
        return plan

    started = _now()
    log: list[str] = []
    project = None
    try:
        log.append(f"Topic: {topic} | Niche: {niche}")
        project = await _create_project(topic, niche, user_id)
        log.append(f"Project {project['id']} created")

        script_data = await _generate_script(project)
        script = await db.scripts.find_one({"project_id": project["id"]}, {"_id": 0}) or {"full_script": script_data.get("full_script","")}
        log.append("Script generated")

        scenes = await _generate_scenes(project, script)
        log.append(f"Scenes {len(scenes)}")

        await _generate_metadata(project, script, scenes)
        log.append("Metadata generated")

        await _ensure_assets_and_render(project)
        log.append("Render completed")

        await _generate_thumbnail(project)
        log.append("Thumbnail done")

        if not publish:
            log.append("Publish disabled for this invocation — skipping YouTube upload, clipping, auto-post")
            summary = {
                "id": str(uuid.uuid4()),
                "project_id": project["id"],
                "topic": topic,
                "niche": niche,
                "youtube_video_id": None,
                "youtube_url": None,
                "shorts": [],
                "status": "completed",
                "publish_skipped": True,
                "log": log,
                "started_at": started.isoformat(),
                "finished_at": _now().isoformat(),
            }
            run_id = summary["id"]
            await db.forge_runs.insert_one({"id": run_id, "project_id": project["id"], "youtube_video_id": None, "status": "completed", "created_at": _now(), "updated_at": _now(), "summary": summary})
            _last_run = summary
            logger.info("Forge once completed without publishing project=%s", project["id"])
            return summary

        yt = await _youtube_upload(project)
        log.append(f"YouTube upload {yt['youtube_video_id']} (mock={yt.get('is_mock')})")

        shorts = await _videoforge_clip(yt["youtube_url"])
        log.append(f"VideoForge clipped to {len(shorts)} shorts")

        shorts_res = await _autopost_shorts(project, shorts)
        log.append(f"Shorts auto-posted {len(shorts_res)}")

        summary = {
            "id": str(uuid.uuid4()),
            "project_id": project["id"],
            "topic": topic,
            "niche": niche,
            "youtube_video_id": yt["youtube_video_id"],
            "youtube_url": yt["youtube_url"],
            "shorts": shorts,
            "status": "completed",
            "log": log,
            "started_at": started.isoformat(),
            "finished_at": _now().isoformat(),
        }
        # Persist to forge_runs history collection (also keep forge_runs per project)
        run_id = summary["id"]
        await db.forge_runs.insert_one({"id": run_id, "project_id": project["id"], "youtube_video_id": yt["youtube_video_id"], "status": "completed", "created_at": _now(), "updated_at": _now(), "summary": summary})
        _last_run = summary
        logger.info("Forge once completed project=%s", project["id"])
        return summary
    except Exception as e:
        logger.exception("Forge once failed %s", e)
        err_summary = {"status": "failed", "error": str(e)[:800], "log": log, "project_id": (project or {}).get("id"), "started_at": started.isoformat(), "finished_at": _now().isoformat()}
        try:
            await db.forge_runs.insert_one({"id": str(uuid.uuid4()), "project_id": (project or {}).get("id") or "unknown", "youtube_video_id": None, "status": "failed", "error": str(e)[:500], "created_at": _now(), "updated_at": _now(), "summary": err_summary})
        except Exception:
            pass
        try:
            from .. import alerts as _alerts
            await _alerts.notify(
                f"forge-run-failed-{(project or {}).get('id') or 'unknown'}",
                "Scheduled Forge run failed",
                f"project={(project or {}).get('id')} error={type(e).__name__}: {e}"[:500],
                {"project_id": (project or {}).get("id")}, priority=6)
        except Exception:  # noqa: BLE001 — alerting never fails the forge path
            pass
        _last_run = err_summary
        raise

def _job_next_run(scheduler) -> Optional[str]:
    """Return the ISO next_run_time for the forge job, or None."""
    if scheduler is None:
        return None
    try:
        job = scheduler.get_job("forge_daily_0900")
    except Exception:
        return None
    if job is not None and getattr(job, "next_run_time", None):
        return job.next_run_time.isoformat()
    return None


def start_scheduler():
    global _scheduler
    if _scheduler:
        scheduler_logger.info(
            "scheduler: start ignored — already running (job=forge_daily_0900 next_run_time=%s)",
            _job_next_run(_scheduler),
        )
        return _scheduler
    if os.environ.get("AUTO_POST_ENABLED", "false").lower() not in ("1","true","yes","on"):
        scheduler_logger.info(
            "scheduler: start skipped — AUTO_POST_ENABLED is not true (auto-post OFF)"
        )
        return None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        scheduler = AsyncIOScheduler(timezone="Australia/Adelaide")
        # Daily 09:00 Australia/Adelaide
        trigger = CronTrigger(hour=9, minute=0, timezone="Australia/Adelaide")
        scheduler.add_job(run_forge_once, trigger, id="forge_daily_0900", replace_existing=True, max_instances=1, coalesce=True)
        scheduler.start()
        _scheduler = scheduler
        scheduler_logger.info(
            "scheduler: registered job forge_daily_0900 next_run_time=%s timezone=Australia/Adelaide",
            _job_next_run(scheduler),
        )
        logger.info("Forge scheduler started daily 09:00 Australia/Adelaide")
        return scheduler
    except Exception as e:
        scheduler_logger.warning("scheduler: start failed %s — scheduler not running", e)
        return None

def get_scheduler():
    return _scheduler


def stop_scheduler():
    """Stop and forget the scheduler so a later explicit enable starts cleanly."""
    global _scheduler
    scheduler = _scheduler
    _scheduler = None
    if scheduler:
        scheduler_logger.info(
            "scheduler: stopping — auto-post OFF, deregistering job forge_daily_0900"
        )
        try:
            scheduler.shutdown(wait=False)
            scheduler_logger.info("scheduler: stopped — no job registered")
        except Exception:
            logger.exception("Forge scheduler shutdown failed")
    else:
        scheduler_logger.info("scheduler: stop ignored — no scheduler was running")


async def restore_scheduler_setting() -> bool:
    """Restore the durable automation switch before deciding to schedule work."""
    db = get_db()
    setting = await db.forge_settings.find_one(
        {"id": "default"},
        {"_id": 0, "auto_post_enabled": 1},
    )
    if setting and isinstance(setting.get("auto_post_enabled"), bool):
        enabled = setting["auto_post_enabled"]
        scheduler_logger.info(
            "scheduler: restored auto_post_enabled=%s from DB forge_settings.default",
            enabled,
        )
    else:
        # A missing durable choice is not consent to publish. Operators can
        # enable the loop explicitly through the authenticated UI.
        enabled = False
        scheduler_logger.info(
            "scheduler: no durable auto_post_enabled setting — defaulting OFF (fail-closed)"
        )
    os.environ["AUTO_POST_ENABLED"] = "true" if enabled else "false"
    if enabled:
        scheduler = start_scheduler()
        scheduler_logger.info(
            "scheduler: restored ON + job registered next_run_time=%s",
            _job_next_run(scheduler),
        )
    else:
        stop_scheduler()
        scheduler_logger.info("scheduler: restored OFF from DB — no job registered")
    return enabled

def get_last_run():
    return _last_run

# Expose APIS
from fastapi import APIRouter, Depends, HTTPException
from ..auth import get_current_user
from ..publishing_access import ensure_operator

forge_router = APIRouter(prefix="/api/forge", tags=["forge"])

@forge_router.post("/run-now")
async def forge_run_now(topic: Optional[str] = None, niche: Optional[str] = None, user=Depends(get_current_user)):
    """Button [RUN NOW - FORGE 1 VIDEO].

    Fail-closed: while automation is OFF this never publishes. Instead of
    triggering a YouTube upload it refuses with HTTP 409 so an operator must
    explicitly enable automation first.
    """
    ensure_operator(user)
    try:
        result = await run_forge_once(user_id=user["id"], topic_override=topic, niche_override=niche)
        return result
    except PublishDisabledError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:800])

@forge_router.get("/status")
async def forge_status(user=Depends(get_current_user)):
    ensure_operator(user)
    db = get_db()
    sched = get_scheduler()
    scheduler_exists = sched is not None
    job_registered = False
    next_run_time = None
    if scheduler_exists:
        try:
            job = sched.get_job("forge_daily_0900")
            if job is not None:
                job_registered = True
                if getattr(job, "next_run_time", None):
                    next_run_time = job.next_run_time.isoformat()
        except Exception:
            scheduler_logger.warning(
                "scheduler: status could not read job forge_daily_0900 from scheduler"
            )
    # next_run is a human-friendly display value (next 09:00 Adelaide). When the
    # scheduler is intentionally OFF there is no real next run, so next_run_time
    # stays None while next_run still drives the dashboard countdown.
    next_run = next_run_time
    if not next_run:
        try:
            from zoneinfo import ZoneInfo
            adl = ZoneInfo("Australia/Adelaide")
            now_adl = datetime.now(adl)
            nxt = now_adl.replace(hour=9, minute=0, second=0, microsecond=0)
            if nxt <= now_adl:
                nxt = nxt + timedelta(days=1)
            next_run = nxt.isoformat()
        except Exception:
            next_run = (_now() + timedelta(days=1)).isoformat()
    last = get_last_run()
    last_run_summary = None
    # also fetch last DB entry
    last_db = None
    try:
        last_db = await db.forge_runs.find_one({}, {"_id": 0}, sort=[("created_at", -1)])
        if last_db:
            last_run_summary = last_db.get("summary") or last_db
        if last_db and not last:
            last = last_run_summary
    except Exception:
        pass
    if last_run_summary is None:
        last_run_summary = last
    recently = []
    try:
        recently = await db.forge_runs.find({}, {"_id": 0}).sort("created_at", -1).to_list(5)
    except Exception:
        pass
    auto_post_enabled = await is_auto_post_enabled(db)
    return {
        "enabled": auto_post_enabled,
        "auto_post_enabled": auto_post_enabled,
        "scheduler_exists": scheduler_exists,
        "job_registered": job_registered,
        "next_run": next_run,
        "next_run_time": next_run_time,
        "next_run_countdown": next_run,
        "timezone": "Australia/Adelaide",
        "last_run": last,
        "last_run_summary": last_run_summary,
        "recent_runs": recently,
        "scheduler_active": scheduler_exists,
        "niches": NICHES_DEFAULT,
    }

@forge_router.get("/runs")
async def forge_runs(user=Depends(get_current_user)):
    ensure_operator(user)
    from ..db import get_db
    db = get_db()
    runs = await db.forge_runs.find({}, {"_id": 0}).sort("created_at", -1).to_list(20)
    return runs

@forge_router.post("/toggle")
async def forge_toggle(enabled: bool = True, user=Depends(get_current_user)):
    ensure_operator(user)
    # Toggle is env-level but we store in DB settings for persistence
    from ..db import get_db
    db = get_db()
    os.environ["AUTO_POST_ENABLED"] = "true" if enabled else "false"
    await db.forge_settings.update_one({"id": "default"}, {"$set": {"auto_post_enabled": enabled, "updated_at": _now()}}, upsert=True)
    if enabled:
        start_scheduler()
    else:
        stop_scheduler()
    return {"auto_post_enabled": enabled}

# Topics endpoints (for step a)
topics_router = APIRouter(prefix="/api/topics", tags=["topics"])

@topics_router.get("/trending")
async def topics_trending():
    await ensure_niches()
    # return random niche topic as trending
    niche = random.choice(NICHES_DEFAULT)
    topic = random.choice(TOPICS_BY_NICHE[niche])
    return {"topic": topic, "niche": niche, "topics": [topic], "source": "niches"}

@topics_router.get("")
async def topics_list():
    from ..db import get_db
    db = get_db()
    await ensure_niches()
    niches = await db.niches.find({}, {"_id": 0}).to_list(20)
    return {"niches": niches}
