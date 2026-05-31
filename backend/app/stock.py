"""Stock asset service — Pexels with deterministic mock fallback.

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

import hashlib
import logging
import os
from typing import Literal, Optional

import httpx

logger = logging.getLogger("facelessforge.stock")

MediaType = Literal["both", "videos", "photos"]


def _use_mock() -> bool:
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    flag = os.environ.get("USE_MOCK_PEXELS", "true").strip().lower() in ("1", "true", "yes")
    return flag or not key


def is_mock_mode() -> bool:
    """Exposed helper for the API layer so the UI can show a 'mock' badge."""
    return _use_mock()


# ---------------- Mock generator ----------------

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


# ---------------- Real Pexels adapter ----------------

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


def _normalise_pexels_video(v: dict, query: str) -> dict:
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
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "duration": int(v.get("duration") or 0),
        "tags": [query],
        "query": query,
    }


async def _search_pexels(query: str, media_type: MediaType, per_page: int,
                          *, orientation: Optional[str] = None) -> list[dict]:
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    base = os.environ.get("PEXELS_API_BASE_URL", "https://api.pexels.com").rstrip("/")
    headers = {"Authorization": key}
    per_page = max(1, min(int(per_page), 40))
    out: list[dict] = []
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        if media_type in ("both", "photos"):
            params = {"query": query, "per_page": per_page}
            if orientation in ("landscape", "portrait", "square"):
                params["orientation"] = orientation
            r = await client.get(f"{base}/v1/search", params=params)
            if r.status_code == 429:
                raise RuntimeError("pexels_rate_limited")
            r.raise_for_status()
            data = r.json()
            for p in (data.get("photos") or []):
                out.append(_normalise_pexels_photo(p, query))
        if media_type in ("both", "videos"):
            video_params = {"query": query, "per_page": per_page}
            min_dur = int(os.environ.get("PEXELS_MIN_VIDEO_DURATION", "10"))
            if min_dur > 0:
                video_params["min_duration"] = min_dur
            if orientation in ("landscape", "portrait", "square"):
                video_params["orientation"] = orientation
            r = await client.get(f"{base}/videos/search", params=video_params)
            if r.status_code == 429:
                raise RuntimeError("pexels_rate_limited")
            r.raise_for_status()
            data = r.json()
            for v in (data.get("videos") or []):
                out.append(_normalise_pexels_video(v, query))
    return out


# ---------------- Public ----------------

async def search_stock(query: str, media_type: MediaType = "both", per_page: int = 12,
                       *, visual_tone: Optional[str] = None,
                       orientation: Optional[str] = None) -> dict:
    """Search Pexels (or mock) for stock footage.

    ``visual_tone`` is appended to the query so every scene in a project
    pulls from the same visual world. ``orientation`` (landscape/portrait/
    square) is honoured by the real Pexels API for tighter visual control.
    """
    query = (query or "").strip()
    if visual_tone and visual_tone.strip():
        query = f"{query} {visual_tone.strip()}".strip()
    if not query:
        return {"source": "mock" if _use_mock() else "pexels", "results": [], "mock": _use_mock(), "query": ""}

    if _use_mock():
        return {
            "source": "mock",
            "results": _mock_results(query, media_type, per_page),
            "mock": True,
            "query": query,
        }
    try:
        results = await _search_pexels(query, media_type, per_page, orientation=orientation)
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
