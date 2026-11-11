"""Stock acceptance for unattended attachment.

Provider titles are not footage. A candidate is inserted only after a downloaded
file probes as a real video and a frame judgment says it is the same subject.
One shared token, or a shared generic word, is not acceptance.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
import re
from urllib.parse import urlparse

FILLER = frozenset("""
a an and are as at be by close closeup clip clips drone footage for from
hd high image images in into is it of on or photo photos pexels pixabay
professional scene shot stock the to ultra video videos view with showing
cinematic b roll slow motion wide 4k 8k visual camera angle footage
""".split())

# Shared scenery and color words from the failed kitchen acceptance. They never
# count as the subject of a clip.
INCIDENTAL = frozenset("""
kitchen counter counters countertop cup cups water food orange oranges
rain rainy raindrop raindrops window windows glass
""".split())

MIN_RELEVANCE_SCORE = 70
INSPECT_CAP = 6
MIN_VIDEO_BYTES = 8192
MIN_EDGE_PX = 64
_JUDGE_MODEL = "gemini-3.8-flash"


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(w) >= 3 and w not in FILLER}


def subject_terms(scene: dict, query: str) -> set[str]:
    """Primary scene-specific search terms, never broad project topic alone."""
    terms = scene.get("search_terms") or []
    if isinstance(terms, str):
        terms = [terms]
    candidates = [str(t) for t in terms if t] + [scene.get("visual_direction") or "", query]
    for candidate in candidates:
        words = _tokens(candidate)
        if words:
            return words
    return set()


def verified_metadata(candidate: dict) -> str:
    """Only independent provider descriptions and descriptive URL slugs count."""
    title = str(candidate.get("title") or candidate.get("name") or "").strip()
    if re.fullmatch(r"(?:pexels|pixabay|unsplash)\s+(?:video|photo|image)\s+\d+", title, re.I):
        title = ""
    url = str(candidate.get("source_url") or "")
    path = urlparse(url).path
    slug = " ".join(p for p in path.split("/") if p and not p.isdigit())
    # Do not use 'tags', 'query' or the image preview: usually echoed from request.
    return f"{title} {slug}"


def specific_overlap(candidate: dict, scene: dict, query: str) -> set[str]:
    """Subject words shared with the title, ignoring generic scenery and color."""
    subjects = set()
    terms = scene.get("search_terms") or []
    if isinstance(terms, str):
        terms = [terms]
    for part in [query, scene.get("narration_text") or "", scene.get("visual_direction") or "", *terms]:
        subjects |= _tokens(part)
    return {word for word in (subjects & _tokens(verified_metadata(candidate))) if word not in INCIDENTAL}


def _judgment_accepts(candidate: dict) -> bool:
    judgment = candidate.get("visual_relevance")
    if not isinstance(judgment, dict) or judgment.get("accepted") is not True:
        return False
    score = judgment.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return False
    if not math.isfinite(float(score)) or float(score) < MIN_RELEVANCE_SCORE:
        return False
    return True


def candidate_is_relevant(candidate: dict, scene: dict, query: str) -> bool:
    source = str(candidate.get("source") or "").strip().lower()
    if source == "mock" or bool(candidate.get("mock")):
        return False
    if not candidate.get("download_url") or not candidate.get("external_id"):
        return False
    # Token overlap is never enough. Acceptance is the frame judgment only.
    return _judgment_accepts(candidate)


def relevant_candidates(candidates: list[dict], scene: dict, query: str) -> tuple[list[dict], list[dict]]:
    allowed, rejected = [], []
    for candidate in candidates:
        (allowed if candidate_is_relevant(candidate, scene, query) else rejected).append(candidate)
    return allowed, rejected


def _structurally_unusable(candidate: dict) -> bool:
    source = str(candidate.get("source") or "").strip().lower()
    if source == "mock" or bool(candidate.get("mock")):
        return True
    return not candidate.get("download_url") or not candidate.get("external_id")


async def download_candidate(url: str, dest: Path) -> bool:
    from .render import MAX_VIDEO_DOWNLOAD_BYTES, _download_to
    return await _download_to(url, dest, max_bytes=MAX_VIDEO_DOWNLOAD_BYTES)


async def probe_stock_video(path: Path) -> dict | None:
    """Decodable video, finite duration of at least one second, and a real frame size."""
    from .verify import FFPROBE_BIN, _run_cmd
    try:
        size = path.stat().st_size if path.exists() else 0
    except OSError:
        return None
    if size < MIN_VIDEO_BYTES:
        return None
    try:
        stdout, _stderr, rc = await _run_cmd(
            FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,duration:format=duration",
            "-of", "json", str(path),
            timeout=30,
        )
    except Exception:
        return None
    if rc != 0 or not stdout.strip():
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    stream = (data.get("streams") or [None])[0]
    if not isinstance(stream, dict) or not stream.get("codec_name"):
        return None
    try:
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if width < MIN_EDGE_PX or height < MIN_EDGE_PX:
        return None
    raw_duration = stream.get("duration") or (data.get("format") or {}).get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration < 1.0:
        return None
    return {"width": width, "height": height, "duration": duration, "file_size": size}


def _image_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG"):
        return "image/png"
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw.startswith(b"RIFF") and b"WEBP" in raw[:16]:
        return "image/webp"
    return "image/jpeg"


async def _representative_image(video: Path, candidate: dict, work: Path) -> tuple[bytes, str] | None:
    from .verify import FFMPEG_BIN, _run_cmd
    frame = work / "frame.jpg"
    try:
        _stdout, _stderr, rc = await _run_cmd(
            FFMPEG_BIN, "-y", "-v", "error", "-ss", "0.4", "-i", str(video),
            "-frames:v", "1", "-q:v", "3", str(frame),
            timeout=30,
        )
    except Exception:
        rc = 1
    if rc == 0 and frame.is_file() and frame.stat().st_size > 0:
        raw = frame.read_bytes()
        return raw, _image_mime(raw)
    preview = str(candidate.get("preview_url") or "")
    if not preview:
        return None
    thumb = work / "thumb"
    if not await download_candidate(preview, thumb):
        return None
    try:
        raw = thumb.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return raw, _image_mime(raw)


async def judge_frame(image: bytes, mime: str, narration: str, query: str) -> dict:
    """Same-subject judgment. Any failed call is a rejection, never a pass."""
    key = (os.getenv("GOOGLE_GENAI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not key or not image:
        return {"accepted": False, "score": None, "reason": "vision_unconfigured"}
    model = (os.getenv("DIRECTOR_GEMINI_MODEL") or _JUDGE_MODEL).strip() or _JUDGE_MODEL
    prompt = (
        "Does this video frame show the queried action happening in the queried setting? "
        "Both the action and the setting named in the footage query must be visible. "
        "One shared object word is not a match. "
        "An outdoor kettle, or a kettle on a cafe or park table, does not match steam over a kitchen counter. "
        "Generic shared words are not a match: kitchen, counter, counters, countertop, cup, cups, "
        "water, food, orange, oranges, rain, rainy, raindrop, raindrops, window, windows, or glass. "
        "A scoreboard, gull, laptop, egg, ink pour, or ocean does not match a kettle, "
        "an orange fruit, a cup, or rain on a window. "
        "Return JSON {\"same_subject\": true or false, \"score\": 0-100, \"reason\": \"short phrase\"}. "
        f"Narration: {narration[:400]}\nFootage query: {query[:200]}"
    )
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        response = await client.aio.models.generate_content(
            model=model,
            contents=[types.Part.from_bytes(data=image, mime_type=mime), prompt],
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
        )
        raw = (getattr(response, "text", None) or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            fence = raw.rfind("```")
            if fence >= 0:
                raw = raw[:fence]
        data = json.loads(raw)
    except Exception:
        return {"accepted": False, "score": None, "reason": "vision_failed"}
    if not isinstance(data, dict):
        return {"accepted": False, "score": None, "reason": "vision_failed"}
    score = data.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
        return {"accepted": False, "score": None, "reason": "vision_failed"}
    score = max(0, min(100, int(score)))
    reason = " ".join(str(data.get("reason") or "").split())[:120]
    same = data.get("same_subject") is True
    if not same or score < MIN_RELEVANCE_SCORE:
        return {"accepted": False, "score": score, "reason": reason or "not_same_subject"}
    return {"accepted": True, "score": score, "reason": reason or "same subject"}


def _reject(candidate: dict, reason: str) -> dict:
    stamped = dict(candidate)
    stamped["reject_reason"] = reason
    stamped["visual_relevance"] = {"accepted": False, "score": None, "reason": reason}
    return stamped


async def _inspect_one(candidate: dict, scene: dict, query: str) -> dict | None:
    query_text = str(candidate.get("matched_query") or query or "")
    narration = str(scene.get("narration_text") or "")
    with tempfile.TemporaryDirectory(prefix="ff-stock-") as tmp:
        work = Path(tmp)
        video = work / "clip.mp4"
        if not await download_candidate(str(candidate.get("download_url") or ""), video):
            return _reject(candidate, "download_failed")
        probe = await probe_stock_video(video)
        if not probe:
            return _reject(candidate, "probe_failed")
        image = await _representative_image(video, candidate, work)
        if image is None:
            return _reject(candidate, "frame_unavailable")
        try:
            judgment = await judge_frame(image[0], image[1], narration, query_text)
        except Exception:
            judgment = {"accepted": False, "score": None, "reason": "vision_failed"}
    accepted = dict(candidate)
    accepted["visual_relevance"] = judgment
    accepted["probed_duration"] = probe["duration"]
    accepted["file_size"] = probe["file_size"]
    accepted["width"] = probe["width"]
    accepted["height"] = probe["height"]
    if judgment.get("accepted") is True:
        accepted["relevance_score"] = judgment.get("score")
        accepted["relevance_reason"] = judgment.get("reason")
        return accepted
    reason = "vision_failed" if judgment.get("reason") == "vision_failed" else "vision_rejected"
    rejected = _reject(accepted, reason)
    rejected["relevance_score"] = judgment.get("score")
    rejected["relevance_reason"] = judgment.get("reason")
    return rejected


async def select_footage_candidates(candidates, scene, query, *, needed: int, renew=None):
    """Probe and judge a capped set. Failures are rejections and are not returned as accepted."""
    accepted, rejected = [], []
    pool = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict) or _structurally_unusable(candidate):
            if isinstance(candidate, dict):
                rejected.append(_reject(candidate, "unusable"))
            continue
        pool.append(candidate)
    pool.sort(key=lambda item: len(specific_overlap(item, scene, query)), reverse=True)
    limit = max(1, int(needed or 1))
    inspected = 0
    for candidate in pool:
        if len(accepted) >= limit or inspected >= INSPECT_CAP:
            break
        if renew is not None and not await renew():
            break
        inspected += 1
        try:
            verdict = await _inspect_one(candidate, scene, query)
        except Exception:
            rejected.append(_reject(candidate, "download_failed"))
            continue
        if not isinstance(verdict, dict) or verdict.get("reject_reason"):
            rejected.append(verdict if isinstance(verdict, dict) else _reject(candidate, "download_failed"))
            continue
        if candidate_is_relevant(verdict, scene, query):
            accepted.append(verdict)
        else:
            rejected.append(_reject(verdict, "vision_rejected"))
    return accepted, rejected
