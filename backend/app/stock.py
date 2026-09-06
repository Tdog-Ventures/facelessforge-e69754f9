"""Stock asset service — Pexels with deterministic mock fallback.

CHANGELOG:
- 2025-06-01: Constitution §4.1 (FOOTAGE CURATION) — Enforced landscape-only
  via orientation="landscape" for all video searches and width/height checks.
- 2025-06-01: Constitution §4.1 — Reject stock videos narrower than 1280px.
- 2025-06-01: Constitution §4.1 — Reject stock videos with height >= width.
- 2025-06-01: Constitution §4.1 — Added `score_relevance()` to rank candidates
  by query term overlap in URL/slug, preferring the most relevant footage.
- 2025-06-01: Constitution §4.2 — Search queries now 2-4 concrete nouns from
  search_terms; orientation is locked to landscape for all video searches.

Public API:
    await search_stock(query, media_type="both", per_page=12) -> {
        "source": "mock" | "pexels",
        "results": [normalised item, ...],
    }

Normalised item shape:
    {
        "source":            "pexels" | "mock",
        "external_id":       "str",
        "media_type":        "stock_video" | "stock_image",
        "title":             str,
        "preview_url":       str,            # thumbnail to show in UI
        "source_url":        str,            # link back to source page
        "download_url":      str | None,     # direct file (if available)
        "attribution_name":  str,
        "attribution_url":   str,
        "width":             int,
        "height":            int,
        "duration":          int | None,     # seconds, video only
        "tags":              [str],
        "query":             str,
    }

If PEXELS_API_KEY missing OR USE_MOCK_PEXELS truthy, deterministic mock is used.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Literal, Optional

import httpx
import re
import requests  # requests-compatible via httpx; key used via params={'key': key}

logger = logging.getLogger("facelessforge.stock")

# ── FIX: Visual mismatch blocklist + text-date filter ───────────────────
# ── FIX 3: semantic Pexels queries — block abstract/legal/literal leaks
PEXELS_ABSTRACT_BLOCKLIST = {
    "judgment","judgement","gap","abstract","legal","literal","gavel","boxes",
    "moving","concept","idea","notion","theory","mindset","paradigm",
    "verdict","court","lawyer","lawsuit","contract","agreement",
    "shall","should","could","would","might","may","generic",
}
BLOCKLIST = ["split", "saldi", "slack", "2026", "sale", "umbrella", "tourist"]
# matches 4-9.9.2026 variants like "4.9.2026", "5.9.2026", "4-9.9.2026"
DATE_TEXT_RE = re.compile(r"(?:^|[^0-9])(?:[4-9]\.9\.2026|4-9\.9\.2026)(?:[^0-9]|$)")
SHORTS_RE = re.compile(r"\bshorts\b", re.I)
# project-wide dedupe of last 10 asset IDs (global)
_LAST_ASSET_IDS: set[str] = set()
_LAST_ASSET_IDS_ORDER: list[str] = []


def _is_blocklisted(item: dict) -> bool:
    """Return True if item matches visual-mismatch blocklist.

    Spec: blocklist = ["Split","SALDI","Slack","2026","sale","umbrella","tourist"]
    + shorts (modern person bounding box with shorts) proxy via tags/title
    + image contains text with numbers 4-9.9.2026
    Checks tags, title, source_url, preview_url case-insensitively.
    """
    tags = item.get("tags") or []
    title = str(item.get("title") or "")
    source_url = str(item.get("source_url") or "")
    preview_url = str(item.get("preview_url") or "")
    combined = " ".join([str(t) for t in tags] + [title, source_url]).lower()
    for w in BLOCKLIST:
        if w.lower() in combined:
            logger.info("BLOCKLIST_FILTER reject ext_id=%s reason=blocklist_word=%s title=%r tags=%s", item.get("external_id"), w, title[:60], tags[:2])
            return True
    # shorts proxy
    if SHORTS_RE.search(combined):
        logger.info("BLOCKLIST_FILTER reject ext_id=%s reason=shorts_proxy tags=%s title=%r", item.get("external_id"), tags[:2], title[:60])
        return True
    # date text 4-9.9.2026 etc in title/tags
    raw = " ".join([str(t) for t in tags] + [title])
    if DATE_TEXT_RE.search(raw):
        logger.info("BLOCKLIST_FILTER reject ext_id=%s reason=date_text_4-9.9.2026 raw=%r", item.get("external_id"), raw[:80])
        return True
    return False


def _dedupe_and_filter_items(items: list[dict]) -> list[dict]:
    """Apply blocklist + de-dupe (last 10 asset IDs) + preview_url dedupe.

    Keeps global _LAST_ASSET_IDS of last 10 external_ids; if duplicate -> discard and fetch next.
    Also discards blocklisted items immediately.
    """
    global _LAST_ASSET_IDS, _LAST_ASSET_IDS_ORDER
    out: list[dict] = []
    seen_urls: set[str] = set()
    for it in items:
        ext = str(it.get("external_id") or "").strip()
        if ext and ext in _LAST_ASSET_IDS:
            logger.info("DEDUPE_FILTER reject ext_id=%s reason=duplicate_last10", ext)
            continue
        if _is_blocklisted(it):
            continue
        # also dedupe by preview_url within this batch
        k = it.get("preview_url") or ext
        if k and k in seen_urls:
            continue
        seen_urls.add(k)
        out.append(it)
        # track for future calls (keep last 10)
        if ext:
            if ext not in _LAST_ASSET_IDS:
                _LAST_ASSET_IDS_ORDER.append(ext)
                _LAST_ASSET_IDS.add(ext)
                while len(_LAST_ASSET_IDS_ORDER) > 10:
                    old = _LAST_ASSET_IDS_ORDER.pop(0)
                    _LAST_ASSET_IDS.discard(old)
    return out

MediaType = Literal["both", "videos", "photos"]

# ── Constitution §4.1: Minimum acceptable dimensions ──────────────────────
MIN_VIDEO_WIDTH = 1280  # Reject files narrower than 1280px


def _use_mock() -> bool:
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    flag = os.environ.get("USE_MOCK_PEXELS", "true").strip().lower() in ("1", "true", "yes")
    return flag or not key


def _has_any_stock_key() -> bool:
    """Check if any stock provider key is configured."""
    return bool(
        os.environ.get("PEXELS_API_KEY", "").strip()
        or os.environ.get("PIXABAY_API_KEY", "").strip()
        or os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
    )


def _should_use_mock_aggregated() -> bool:
    """Mock if flag set or no keys at all (all providers missing)."""
    flag = os.environ.get("USE_MOCK_PEXELS", "true").strip().lower() in ("1", "true", "yes")
    if flag:
        return True
    return not _has_any_stock_key()


def is_mock_mode() -> bool:
    """Exposed helper for the API layer so the UI can show a 'mock' badge."""
    return _use_mock()


def is_mock_mode_aggregated() -> bool:
    """Mock mode for aggregated search — true only if all keys missing or flag set."""
    return _should_use_mock_aggregated()


# ── Relevance scoring ─────────────────────────────────────────────────────

def score_relevance(candidate: dict, query_terms: list[str]) -> int:
    """Score a candidate's relevance based on query term overlap in URL/slug.

    Constitution §4.1: Prefer candidates whose URL/slug contains the search
    terms, giving +2 for the first matching term and +1 for each additional
    matching term. This surfaces the most semantically relevant footage first.

    Args:
        candidate: A normalised stock item dict (must have "source_url" key).
        query_terms: List of lower-case search terms (typically from query.split()).

    Returns:
        Integer score >= 0.
    """
    url = (candidate.get("source_url") or "").lower()
    slug = url.replace("-", " ").replace("_", " ").replace("/", " ")
    score = 0
    first_match = True
    for term in query_terms:
        if term in slug or term in url:
            if first_match:
                score += 2
                first_match = False
            else:
                score += 1
    return score


# ── Mock generator ────────────────────────────────────────────────────────

_MOCK_PHOTOGRAPHERS = [
    "Ada Klein", "Bram Voss", "Chen Rowe", "Dara Okafor", "Elio Marsh",
    "Fen Arita", "Guido Kline", "Hana Reed", "Ivo Ström", "Jana Wolfe",
]


def _mock_results(query: str, media_type: MediaType, per_page: int) -> list[dict]:
    """Deterministic mock results derived from the query string."""
    q = (query or "stock").strip()
    base_seed = int(hashlib.sha1(q.encode("utf-8")).hexdigest()[:8], 16)

    types: list[str] = []
    if media_type in ("both", "photos"):
        types.append("stock_image")
    if media_type in ("both", "videos"):
        types.append("stock_video")

    results: list[dict] = []
    n = max(4, min(per_page, 16))
    for i in range(n):
        mt = types[i % len(types)]
        seed = base_seed + i * 7919
        external_id = str(10_000_000 + (seed % 89_000_000))
        width = 1920 if i % 2 == 0 else 1280
        height = 1080 if i % 2 == 0 else 720
        photographer = _MOCK_PHOTOGRAPHERS[seed % len(_MOCK_PHOTOGRAPHERS)]
        preview = f"https://picsum.photos/seed/ff-{external_id}/640/360"
        results.append({
            "source": "mock",
            "external_id": external_id,
            "media_type": mt,
            "title": f"{q.title()} · {mt.replace('stock_', '').title()} #{i + 1}",
            "preview_url": preview,
            "source_url": f"https://www.pexels.com/{'video' if mt == 'stock_video' else 'photo'}/{external_id}/",
            "download_url": preview,  # in mock, the preview is also the 'downloadable' asset
            "attribution_name": photographer,
            "attribution_url": f"https://www.pexels.com/@{photographer.lower().replace(' ', '-')}",
            "width": width,
            "height": height,
            "duration": (5 + (seed % 25)) if mt == "stock_video" else None,
            "tags": [q] + [w for w in q.split()[:3]],
            "query": q,
        })
    return results


# ── Real Pexels adapter ───────────────────────────────────────────────────

def _normalise_pexels_photo(p: dict, query: str) -> dict:
    src = p.get("src") or {}
    return {
        "source": "pexels",
        "external_id": str(p.get("id")),
        "media_type": "stock_image",
        "title": (p.get("alt") or f"Pexels photo {p.get('id')}")[:160],
        "preview_url": src.get("medium") or src.get("small") or src.get("tiny") or p.get("url"),
        "source_url": p.get("url") or "",
        "download_url": src.get("large2x") or src.get("large") or src.get("original"),
        "attribution_name": p.get("photographer") or "Pexels contributor",
        "attribution_url": p.get("photographer_url") or "https://www.pexels.com",
        "width": int(p.get("width") or 0),
        "height": int(p.get("height") or 0),
        "duration": None,
        "tags": [query],
        "query": query,
    }


def _normalise_pexels_video(v: dict, query: str) -> Optional[dict]:
    """Normalise a Pexels video item, returning None if it fails
    Constitution §4.1 dimension/orientation gates.

    Constitution §4.1 (FOOTAGE CURATION):
        - Landscape only: reject height >= width.
        - Reject files narrower than 1280px.
    """
    width = int(v.get("width") or 0)
    height = int(v.get("height") or 0)

    # Constitution §4.1: Reject non-landscape (height >= width)
    if height >= width:
        logger.debug(
            "Rejecting Pexels video %s: height=%d >= width=%d (not landscape)",
            v.get("id"), height, width,
        )
        return None

    # Constitution §4.1: Reject files narrower than 1280px
    if width < MIN_VIDEO_WIDTH:
        logger.debug(
            "Rejecting Pexels video %s: width=%d < %d (too narrow)",
            v.get("id"), width, MIN_VIDEO_WIDTH,
        )
        return None

    # Pick a reasonable thumbnail
    preview = v.get("image")
    # Pick best mp4 file: try uhd → hd → sd → ld → any mp4 → first file
    download = None
    files = list(v.get("video_files") or [])
    mp4_files = [f for f in files if (f.get("file_type") == "video/mp4")]
    quality_order = ["uhd", "hd", "sd", "ld"]
    for q in quality_order:
        match = next((f for f in mp4_files if f.get("quality") == q), None)
        if match and match.get("link"):
            download = match["link"]
            break
    if not download and mp4_files:
        download = next((f.get("link") for f in mp4_files if f.get("link")), None)
    if not download and files:
        # last resort: take ANY file with a link
        download = next((f.get("link") for f in files if f.get("link")), None)
    user = v.get("user") or {}
    return {
        "source": "pexels",
        "external_id": str(v.get("id")),
        "media_type": "stock_video",
        "title": f"Pexels video {v.get('id')}"[:160],
        "preview_url": preview,
        "source_url": v.get("url") or "",
        "download_url": download,
        "attribution_name": user.get("name") or "Pexels contributor",
        "attribution_url": user.get("url") or "https://www.pexels.com",
        "width": width,
        "height": height,
        "duration": int(v.get("duration") or 0),
        "tags": [query],
        "query": query,
    }


def _strip_pexels_abstract(query: str) -> str:
    words=[w for w in re.split(r"\W+", (query or "").lower()) if w]
    kept=[w for w in words if w not in PEXELS_ABSTRACT_BLOCKLIST and len(w)>=3]
    return " ".join(kept[:4]).strip() or "business team office"

async def _llm_visual_query(sentence: str) -> str:
    prompt=('Generate a 3-word Pexels search query for B-roll that visually represents: '
            f'"{sentence[:400]}" in the context of software engineering / business productivity. '
            'Return only visual nouns, no abstract words.')
    for provider in ("DEEPSEEK_API_KEY","KIMI_API_KEY","MOONSHOT_API_KEY","ANTHROPIC_API_KEY"):
        key=os.environ.get(provider,"").strip()
        if not key: continue
        try:
            if provider=="DEEPSEEK_API_KEY":
                async with httpx.AsyncClient(timeout=20) as c:
                    r=await c.post("https://api.deepseek.com/chat/completions", headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"}, json={"model":"deepseek-chat","messages":[{"role":"user","content":prompt}],"temperature":0.3,"max_tokens":30})
                    if r.status_code==200:
                        txt=re.sub(r"[^a-z0-9 ]"," ",r.json()["choices"][0]["message"]["content"].strip().lower())
                        return _strip_pexels_abstract(txt) or _strip_pexels_abstract(sentence)
            elif provider in ("KIMI_API_KEY","MOONSHOT_API_KEY"):
                async with httpx.AsyncClient(timeout=20) as c:
                    r=await c.post("https://api.moonshot.cn/v1/chat/completions", headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"}, json={"model":os.environ.get("KIMI_MODEL","kimi-k2-0711-preview"),"messages":[{"role":"user","content":prompt}],"temperature":0.3,"max_tokens":30})
                    if r.status_code==200:
                        return _strip_pexels_abstract(re.sub(r"[^a-z0-9 ]"," ",r.json()["choices"][0]["message"]["content"].strip().lower()))
            else:
                async with httpx.AsyncClient(timeout=20) as c:
                    r=await c.post("https://api.anthropic.com/v1/messages", headers={"x-api-key":key,"anthropic-version":"2023-06-01","Content-Type":"application/json"}, json={"model":os.environ.get("LLM_MODEL","claude-3-5-sonnet-latest"),"max_tokens":30,"messages":[{"role":"user","content":prompt}]})
                    if r.status_code==200:
                        txt="".join(b.get("text","") for b in r.json().get("content",[])).strip().lower()
                        return _strip_pexels_abstract(re.sub(r"[^a-z0-9 ]"," ",txt))
        except Exception:
            continue
    return _strip_pexels_abstract(sentence)

async def semantic_pexels_query(sentence: str) -> str:
    q=await _llm_visual_query(sentence)
    return q or _strip_pexels_abstract(sentence)

async def _search_pexels(
    query: str,
    media_type: MediaType,
    per_page: int,
    *,
    orientation: Optional[str] = None,
    page: int = 1,
) -> list[dict]:
    """Search Pexels API and return normalised results.

    Constitution §4.1 (FOOTAGE CURATION):
        - Video searches ALWAYS pass orientation="landscape" regardless of caller.
        - Photo searches honour the caller's orientation if provided.
    FIX 3: query is pre-filtered for abstract/legal/literal words.
    FIX 4: supports page param for pagination (per_page=40).
    """
    # FIX 3: semantic filter — strip abstract words before API call
    query = _strip_pexels_abstract(query)
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    base = os.environ.get("PEXELS_API_BASE_URL", "https://api.pexels.com").rstrip("/")
    headers = {"Authorization": key}
    per_page = max(1, min(int(per_page), 40))
    page = max(1, int(page))
    out: list[dict] = []
    timeout = httpx.Timeout(10.0, connect=5.0)

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        # ── Photos ───────────────────────────────────────────────────────
        if media_type in ("both", "photos"):
            params = {"query": query, "per_page": per_page, "page": page}
            if orientation in ("landscape", "portrait", "square"):
                params["orientation"] = orientation
            r = await client.get(f"{base}/v1/search", params=params)
            if r.status_code == 429:
                raise RuntimeError("pexels_rate_limited")
            r.raise_for_status()
            data = r.json()
            for p in (data.get("photos") or []):
                out.append(_normalise_pexels_photo(p, query))

        # ── Videos ───────────────────────────────────────────────────────
        # Constitution §4.1: ALWAYS force landscape for video searches
        if media_type in ("both", "videos"):
            video_params = {"query": query, "per_page": per_page, "page": page}
            min_dur = int(os.environ.get("PEXELS_MIN_VIDEO_DURATION", "10"))
            if min_dur > 0:
                video_params["min_duration"] = min_dur
            # Force landscape — no caller override permitted
            video_params["orientation"] = "landscape"
            r = await client.get(f"{base}/videos/search", params=video_params)
            if r.status_code == 429:
                logger.warning("Pexels videos rate-limited (429) query=%r", query[:60])
                raise RuntimeError("pexels_rate_limited")
            r.raise_for_status()
            data = r.json()
            raw_videos = data.get("videos") or []
            kept = 0
            for v in raw_videos:
                normalised = _normalise_pexels_video(v, query)
                if normalised is not None:
                    out.append(normalised)
                    kept += 1
            logger.info(
                "pexels_videos query=%r per_page=%d page=%d response_count=%d kept=%d",
                query[:60], per_page, page, len(raw_videos), kept,
            )

    # FIX: blocklist + dedupe (last 10) filter
    out = _dedupe_and_filter_items(out)
    logger.info("BLOCKLIST post-filter query=%r remaining=%d", query[:60], len(out))
    return out

async def _search_pexels_paginated(query: str, media_type: MediaType, *, min_results: int = 20, per_page: int = 40) -> list[dict]:
    """FIX 4: Paginate Pexels (page + per_page=40) until min_results unique clips.
    Tries page 1-3 with same query, then 2 backup semantic queries if still short."""
    seen: dict[str, dict] = {}
    for pg in (1, 2, 3):
        try:
            batch = await _search_pexels(query, media_type, per_page, page=pg)
        except Exception as e:
            if "rate_limited" in str(e):
                break
            continue
        for it in batch:
            eid = it.get("external_id")
            if eid not in seen:
                seen[eid] = it
        if len(seen) >= min_results:
            break
    # If still short, try 2 backup queries with rephrased terms
    if len(seen) < min_results:
        # backup: drop last word / add cinematic variant
        base_words = query.split()
        backups = []
        if len(base_words) >= 2:
            backups.append(" ".join(base_words[1:]) + " office")
            backups.append(base_words[0] + " team collaboration")
        for bq in backups[:2]:
            if len(seen) >= min_results:
                break
            bq = _strip_pexels_abstract(bq)
            try:
                batch = await _search_pexels(bq, media_type, per_page, page=1)
            except Exception:
                continue
            for it in batch:
                eid = it.get("external_id")
                if eid not in seen:
                    seen[eid] = it
                if len(seen) >= min_results:
                    break
    return list(seen.values())

# ── Pixabay adapters ─────────────────────────────────────────────────────

def _normalise_pixabay_image(hit: dict, query: str) -> dict:
    return {
        "source": "pixabay",
        "external_id": str(hit.get("id")),
        "media_type": "stock_image",
        "title": (hit.get("tags") or f"Pixabay photo {hit.get('id')}")[:160],
        "preview_url": hit.get("webformatURL") or hit.get("previewURL") or hit.get("largeImageURL"),
        "source_url": hit.get("pageURL") or f"https://pixabay.com/photos/{hit.get('id')}/",
        "download_url": hit.get("largeImageURL") or hit.get("webformatURL") or hit.get("previewURL"),
        "attribution_name": hit.get("user") or "Pixabay contributor",
        "attribution_url": f"https://pixabay.com/users/{hit.get('user')}-" + str(hit.get("user_id") or ""),
        "width": int(hit.get("imageWidth") or hit.get("webformatWidth") or 0),
        "height": int(hit.get("imageHeight") or hit.get("webformatHeight") or 0),
        "duration": None,
        "tags": [query] + (hit.get("tags") or "").split(", ")[:3],
        "query": query,
    }

def _normalise_pixabay_video(hit: dict, query: str) -> Optional[dict]:
    # Pixabay video hit contains videos.{large,medium,small,tiny}
    videos = hit.get("videos") or {}
    # Prefer large -> medium -> small
    best = None
    for size in ["large", "medium", "small", "tiny"]:
        v = videos.get(size)
        if v and v.get("url"):
            best = v
            break
    if not best:
        return None
    width = int(best.get("width") or 0)
    height = int(best.get("height") or 0)
    if height >= width:
        return None
    if width < MIN_VIDEO_WIDTH:
        return None
    return {
        "source": "pixabay",
        "external_id": str(hit.get("id")),
        "media_type": "stock_video",
        "title": (hit.get("tags") or f"Pixabay video {hit.get('id')}")[:160],
        "preview_url": hit.get("userImageURL") or best.get("url"),
        "source_url": hit.get("pageURL") or f"https://pixabay.com/videos/{hit.get('id')}/",
        "download_url": best.get("url"),
        "attribution_name": hit.get("user") or "Pixabay contributor",
        "attribution_url": f"https://pixabay.com/users/{hit.get('user')}-" + str(hit.get("user_id") or ""),
        "width": width,
        "height": height,
        "duration": int(hit.get("duration") or best.get("duration") or 0) if hit.get("duration") or best.get("duration") else None,
        "tags": [query] + (hit.get("tags") or "").split(", ")[:3],
        "query": query,
    }

async def _search_pixabay(query: str, media_type: MediaType, per_page: int) -> list[dict]:
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key:
        logger.info("Pixabay search skipped: no API key")
        raise RuntimeError("pixabay_no_key")
    per_page = max(1, min(int(per_page), 40))
    out: list[dict] = []
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if media_type in ("both", "photos"):
            params = {"key": key, "q": query, "image_type": "photo", "per_page": per_page, "safesearch": "true", "orientation": "horizontal"}
            r = await client.get("https://pixabay.com/api/", params=params)
            if r.status_code == 401:
                logger.warning("Pixabay photos auth failed (401) query=%r — mock fallback", query[:60])
                raise RuntimeError("pixabay_rate_limited")
            if r.status_code == 429:
                logger.warning("Pixabay photos rate-limited (429) query=%r — mock fallback", query[:60])
                raise RuntimeError("pixabay_rate_limited")
            r.raise_for_status()
            data = r.json()
            for hit in (data.get("hits") or [])[:per_page]:
                out.append(_normalise_pixabay_image(hit, query))
        if media_type in ("both", "videos"):
            # Over-fetch to ensure 12 landscape results after filtering (portrait filtered)
            fetch_n = max(per_page * 2, per_page + 10)
            fetch_n = min(fetch_n, 40)
            params = {"key": key, "q": query, "per_page": fetch_n, "safesearch": "true"}
            r = await client.get("https://pixabay.com/api/videos/", params=params)
            if r.status_code == 401:
                logger.warning("Pixabay videos auth failed (401) query=%r — mock fallback", query[:60])
                raise RuntimeError("pixabay_rate_limited")
            if r.status_code == 429:
                logger.warning("Pixabay videos rate-limited (429) query=%r — mock fallback", query[:60])
                raise RuntimeError("pixabay_rate_limited")
            r.raise_for_status()
            data = r.json()
            # Filter portrait etc., then slice to requested per_page to guarantee 12 when possible
            valid = []
            for hit in (data.get("hits") or []):
                norm = _normalise_pixabay_video(hit, query)
                if norm:
                    valid.append(norm)
                if len(valid) >= per_page:
                    break
            out.extend(valid[:per_page])
    out = _dedupe_and_filter_items(out)
    return out

# ── Unsplash adapters ────────────────────────────────────────────────────

def _normalise_unsplash_photo(p: dict, query: str) -> dict:
    urls = p.get("urls") or {}
    user = p.get("user") or {}
    return {
        "source": "unsplash",
        "external_id": str(p.get("id")),
        "media_type": "stock_image",
        "title": (p.get("alt_description") or p.get("description") or f"Unsplash photo {p.get('id')}")[:160],
        "preview_url": urls.get("small") or urls.get("thumb") or urls.get("regular"),
        "source_url": p.get("links", {}).get("html") or f"https://unsplash.com/photos/{p.get('id')}",
        "download_url": urls.get("regular") or urls.get("full") or urls.get("small"),
        "attribution_name": user.get("name") or "Unsplash contributor",
        "attribution_url": user.get("links", {}).get("html") or "https://unsplash.com",
        "width": int(p.get("width") or 0),
        "height": int(p.get("height") or 0),
        "duration": None,
        "tags": [query],
        "query": query,
    }

async def _search_unsplash(query: str, per_page: int) -> list[dict]:
    key = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
    if not key:
        logger.info("Unsplash search skipped: no API key")
        raise RuntimeError("unsplash_no_key")
    per_page = max(1, min(int(per_page), 30))
    headers = {"Authorization": f"Client-ID {key}"}
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        params = {"query": query, "per_page": per_page, "orientation": "landscape"}
        r = await client.get("https://api.unsplash.com/search/photos", params=params)
        if r.status_code == 429:
            raise RuntimeError("unsplash_rate_limited")
        r.raise_for_status()
        data = r.json()
        out = []
        for p in (data.get("results") or [])[:per_page]:
            out.append(_normalise_unsplash_photo(p, query))
        out = _dedupe_and_filter_items(out)
        return out

# ── Unified dispatcher ────────────────────────────────────────────────────

async def search_stock_with_source(query: str, source: str, media_type: MediaType, per_page: int = 12) -> dict:
    """Dispatch to correct provider based on source param.

    source: pexels|pixabay|unsplash (case-insensitive). Default pexels.
    media_type: both|videos|photos derived from type param.
    Falls back to mock if provider key missing or rate-limited.
    """
    query = (query or "").strip()
    if query and len(query.split()) > 4:
        query = " ".join(query.split()[:4])
    source = (source or "pexels").strip().lower()
    if not query:
        return {"source": source if source in ("pexels","pixabay","unsplash") else "mock", "results": [], "mock": True, "query": ""}
    # Normalize media_type
    if media_type not in ("both", "videos", "photos"):
        media_type = "both"
    # Unsplash only supports images
    if source == "unsplash" and media_type == "videos":
        media_type = "photos"
    try:
        if source == "pixabay":
            results = await _search_pixabay(query, media_type, per_page)
            query_terms = [t.lower() for t in query.split() if len(t) > 2]
            if query_terms:
                results.sort(key=lambda c: score_relevance(c, query_terms), reverse=True)
            results = _dedupe_and_filter_items(results)
            return {"source": "pixabay", "results": results, "mock": False, "query": query}
        elif source == "unsplash":
            results = await _search_unsplash(query, per_page)
            query_terms = [t.lower() for t in query.split() if len(t) > 2]
            if query_terms:
                results.sort(key=lambda c: score_relevance(c, query_terms), reverse=True)
            results = _dedupe_and_filter_items(results)
            return {"source": "unsplash", "results": results, "mock": False, "query": query}
        else:  # pexels default
            # Use existing search_stock path which handles mock fallback internally
            res = await search_stock(query, media_type, per_page)
            # Override source to pexels if not mock
            if not res.get("mock"):
                res["source"] = "pexels"
            return res
    except RuntimeError as e:
        msg = str(e)
        if msg in ("pixabay_no_key", "unsplash_no_key"):
            logger.info("%s no key, falling back to mock for query=%r", source, query[:60])
            return {"source": "mock", "results": _mock_results(query, media_type, per_page), "mock": True, "query": query, "warning": f"{source} API key missing — mock results"}
        if "rate_limited" in msg:
            logger.warning("%s rate-limited, mock fallback query=%r", source, query[:60])
            return {"source": "mock", "results": _mock_results(query, media_type, per_page), "mock": True, "query": query, "warning": f"{source} rate limit — mock results"}
        raise
    except httpx.HTTPError as e:
        logger.warning("%s HTTP error %s — mock fallback", source, e)
        return {"source": "mock", "results": _mock_results(query, media_type, per_page), "mock": True, "query": query, "warning": f"{source} unavailable — mock results"}

# ── Aggregated multi-source search ───────────────────────────────────────

async def search_stock_aggregated(
    query: str,
    media_type: MediaType = "both",
    per_page: int = 12,
) -> dict:
    """Aggregated search across Pexels + Pixabay + Unsplash.

    - Tries Pexels if PEXELS_API_KEY set.
    - Also queries Pixabay if PIXABAY_API_KEY set.
    - Also queries Unsplash if UNSPLASH_ACCESS_KEY set (photos only).
    - Merges results round-robin to preserve mix, normalises to same shape.
    - Does NOT truncate query server-side; uses full body.query as-is.
    - Returns {source: "mixed" | "pexels" | "pixabay" | "unsplash" | "mock", results, mock, query, warning?}
    - If all keys missing or USE_MOCK_PEXELS true -> mock mode with warning.
    """
    query = (query or "").strip()
    if query and len(query.split()) > 4:
        query = " ".join(query.split()[:4])
    if not query:
        is_mock = _should_use_mock_aggregated()
        src = "mock" if is_mock else "mixed"
        return {"source": src, "results": [], "mock": is_mock, "query": ""}

    if _should_use_mock_aggregated():
        logger.info("search_stock_aggregated mock mode query=%r", query[:60])
        return {
            "source": "mock",
            "results": _mock_results(query, media_type, per_page),
            "mock": True,
            "query": query,
            "warning": "No stock API keys configured — showing deterministic mock results.",
        }

    # Determine enabled providers
    providers: list[str] = []
    if os.environ.get("PEXELS_API_KEY", "").strip():
        providers.append("pexels")
    if os.environ.get("PIXABAY_API_KEY", "").strip():
        providers.append("pixabay")
    if os.environ.get("UNSPLASH_ACCESS_KEY", "").strip():
        providers.append("unsplash")

    if not providers:
        return {
            "source": "mock",
            "results": _mock_results(query, media_type, per_page),
            "mock": True,
            "query": query,
            "warning": "No stock API keys configured — showing deterministic mock results.",
        }

    # Build tasks per provider; each gets per_page to allow merging then slice
    tasks = []
    task_sources: list[str] = []
    for src in providers:
        # Unsplash only supports photos — skip it when caller wants videos only
        if src == "unsplash" and media_type == "videos":
            continue
        mt = media_type
        if src == "unsplash" and media_type == "both":
            mt = "photos"
        if src == "pexels":
            tasks.append(_search_pexels(query, mt, per_page))
            task_sources.append(src)
        elif src == "pixabay":
            tasks.append(_search_pixabay(query, mt, per_page))
            task_sources.append(src)
        elif src == "unsplash":
            tasks.append(_search_unsplash(query, per_page))
            task_sources.append(src)

    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    results_by_source: list[list[dict]] = []
    successful_sources: list[str] = []
    warnings: list[str] = []
    for src, res in zip(task_sources, gathered):
        if isinstance(res, Exception):
            msg = str(res)
            if "rate_limited" in msg or "no_key" in msg:
                logger.warning("search_stock_aggregated %s failed query=%r: %s", src, query[:60], msg)
                warnings.append(src)
            else:
                logger.warning("search_stock_aggregated %s HTTP error query=%r: %s", src, query[:60], res)
                warnings.append(src)
            continue
        # res is list[dict]
        if isinstance(res, list) and res:
            # Already normalised, ensure source field correct
            results_by_source.append(res)
            successful_sources.append(src)
        elif isinstance(res, list):
            results_by_source.append([])
            successful_sources.append(src)

    if not results_by_source or all(len(lst) == 0 for lst in results_by_source):
        # All providers returned empty — fallback to mock with warning if appropriate
        logger.warning("search_stock_aggregated all providers empty query=%r, falling back to mock", query[:60])
        return {
            "source": "mock",
            "results": _mock_results(query, media_type, per_page),
            "mock": True,
            "query": query,
            "warning": "All stock providers returned no results — showing mock results.",
        }

    # Score relevance per provider already? Ensure scoring across merged? We'll keep interleaving then score overall
    # Interleave round-robin to ensure mix representation up to per_page
    merged: list[dict] = []
    max_len = max(len(lst) for lst in results_by_source) if results_by_source else 0
    for i in range(max_len):
        for lst in results_by_source:
            if i < len(lst) and len(merged) < per_page:
                merged.append(lst[i])
        if len(merged) >= per_page:
            break

    # If still under per_page (e.g., one provider had few results), fill remaining sequentially
    if len(merged) < per_page:
        for lst in results_by_source:
            for item in lst:
                if item not in merged and len(merged) < per_page:
                    merged.append(item)

    # FIX: blocklist + last10 dedupe
    merged = _dedupe_and_filter_items(merged)

    # Optional: sort by relevance overall? Keep interleaved order to preserve mix; but apply relevance as secondary sort within each provider already.
    # Determine source label
    if len(successful_sources) == 1:
        agg_source = successful_sources[0]
    else:
        agg_source = "mixed"

    out: dict = {"source": agg_source, "results": merged[:per_page], "mock": False, "query": query}
    if warnings:
        out["warning"] = f"Some providers unavailable ({', '.join(warnings)}) — showing partial results."
    # If all keys missing case already handled, but add warning if aggregated used mock-like? Not needed.
    return out


# ── Public ──────────────────────────────────────────────────────────────────

async def search_stock_videos(query: str, per_page: int = 30) -> list[dict]:
    """Video-only Pexels search for cinematic b-roll. FIX 4: paginated, min 20 unique.
    FIX 3: query is LLM-semantic (strip abstract + LLM visual nouns)."""
    query = (query or "").strip()
    if not query:
        return []
    if _use_mock():
        logger.info("search_stock_videos query=%r skipped (mock mode)", query[:60])
        return []
    # FIX 3: use LLM semantic query for B-roll when sentence-like input
    if len(query.split()) > 2 or any(w in query.lower() for w in PEXELS_ABSTRACT_BLOCKLIST):
        try:
            semantic = await _llm_visual_query(query)
            if semantic and semantic != _strip_pexels_abstract(query):
                logger.info("semantic_pexels query=%r -> %r", query[:60], semantic)
                query = semantic
        except Exception:
            query = _strip_pexels_abstract(query)
    else:
        query = _strip_pexels_abstract(query)
    try:
        # FIX 4: paginated fetch guarantees >=20 unique when available
        results = await _search_pexels_paginated(query, "videos", min_results=20, per_page=40)
        if len(results) < 20:
            # per_page fallback kept for backward-compat path
            fallback = await _search_pexels(query, "videos", min(per_page, 40))
            for it in fallback:
                if it.get("external_id") not in {r.get("external_id") for r in results}:
                    results.append(it)
    except RuntimeError as e:
        if str(e) == "pexels_rate_limited":
            logger.warning("search_stock_videos rate-limited query=%r — backing off", query[:60])
            await asyncio.sleep(2.0)
            return []
        raise
    except httpx.HTTPError as e:
        logger.warning("search_stock_videos HTTP error query=%r: %s", query[:60], e)
        return []
    videos = [r for r in results
              if r.get("media_type") == "stock_video" and r.get("download_url")]
    videos = _dedupe_and_filter_items(videos)
    query_terms = [t.lower() for t in query.split() if len(t) > 2]
    if query_terms:
        videos.sort(key=lambda c: score_relevance(c, query_terms), reverse=True)
    logger.info("search_stock_videos query=%r usable=%d (paginated min20)", query[:60], len(videos))
    return videos[:max(per_page,20)]


async def search_stock(
    query: str,
    media_type: MediaType = "both",
    per_page: int = 12,
    *,
    visual_tone: Optional[str] = None,
    orientation: Optional[str] = None,
) -> dict:
    """Search Pexels (or mock) for stock footage.

    ``visual_tone`` is appended to the query so every scene in a project
    pulls from the same visual world. ``orientation`` (landscape/portrait/
    square) is honoured by the real Pexels API for photos only; videos are
    ALWAYS locked to landscape per Constitution §4.1.

    Constitution §4.1: Even after visual_tone is appended, the final query
    is still passed with orientation="landscape" for video searches.
    """
    query = (query or "").strip()
    if visual_tone and visual_tone.strip():
        query = f"{query} {visual_tone.strip()}".strip()
    if query and len(query.split()) > 4:
        query = " ".join(query.split()[:4])
    if not query:
        return {"source": "mock" if _use_mock() else "pexels", "results": [], "mock": _use_mock(), "query": ""}

    if _use_mock():
        return {
            "source": "mock",
            "results": _mock_results(query, media_type, per_page),
            "mock": True,
            "query": query,
        }

    # FIX 3: semantic LLM query for sentence-like inputs
    if not _use_mock() and (len(query.split())>2 or any(w in query.lower() for w in PEXELS_ABSTRACT_BLOCKLIST)):
        try:
            sem=await _llm_visual_query(query)
            if sem: query=sem
        except Exception:
            query=_strip_pexels_abstract(query)
    else:
        query=_strip_pexels_abstract(query)
    try:
        # FIX 4: use paginated search when caller wants >=20
        if per_page >= 20:
            results = await _search_pexels_paginated(query, media_type, min_results=20, per_page=40)
            # trim/pad to per_page but keep at least 20
            results = results[:max(per_page,20)]
        else:
            results = await _search_pexels(query, media_type, per_page, orientation=orientation)

        # Constitution §4.1: Score and sort results by relevance so the most
        # semantically matching clips surface first.
        query_terms = [t.lower() for t in query.split() if len(t) > 2]
        if query_terms:
            results.sort(key=lambda c: score_relevance(c, query_terms), reverse=True)

        results = _dedupe_and_filter_items(results)

        return {"source": "pexels", "results": results, "mock": False, "query": query}
    except RuntimeError as e:
        if str(e) == "pexels_rate_limited":
            logger.warning("Pexels rate-limited, returning mock results")
            return {
                "source": "mock",
                "results": _mock_results(query, media_type, per_page),
                "mock": True,
                "query": query,
                "warning": "Pexels rate limit hit — showing deterministic mock results.",
            }
        raise
    except httpx.HTTPError as e:
        logger.warning("Pexels HTTP error %s — falling back to mock", e)
        return {
            "source": "mock",
            "results": _mock_results(query, media_type, per_page),
            "mock": True,
            "query": query,
            "warning": "Pexels is unavailable — showing deterministic mock results.",
        }
