"""LLM-based generation — local Ollama primary, Anthropic Claude backup.

Produces structured JSON for: Script, Scene plan, Metadata, Thumbnail concepts.
Falls back to deterministic generation if no LLM is available.
"""
from __future__ import annotations
from app.scene_terms import derive_search_terms, derive_visual_query
from app.visual_query import extract_visual_keywords, is_garbage_token, truncate_words

import json
import logging
import os
import random
import re
import uuid
from typing import Optional

import httpx

logger = logging.getLogger("facelessforge.generation")


def _llm_available() -> bool:
    """Return True if any LLM provider is configured."""
    return bool(os.environ.get("OLLAMA_MODEL", "").strip()) or bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
    )


def _extract_json(text: str) -> Optional[dict]:
    """Extract the first JSON object from a string, stripping markdown fences."""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    # Extract first balanced JSON object
    match = re.search(r"(\{.*\})", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None



async def _llm_json(system: str, user: str, cache_key: str = "") -> Optional[dict]:
    """Call local Ollama first, then DeepSeek, then deterministic fallback.

    Ollama is the primary provider (local, private, no API costs).
    DeepSeek is the cloud backup if Ollama is unavailable.
    """
    last_error: str = ""

    # 1. Local Ollama (primary)
    ollama_model = os.environ.get("OLLAMA_MODEL", "llama3:latest").strip() or "llama3:latest"
    ollama_timeout = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "600.0"))
    for attempt in range(2):
        try:
            logger.info("Ollama (%s) request for %s", ollama_model, cache_key)
            async with httpx.AsyncClient(timeout=ollama_timeout) as client:
                resp = await client.post(
                    "http://localhost:11434/api/generate",
                    json={
                        "model": ollama_model,
                        "system": system,
                        "prompt": user,
                        "stream": False,
                        "format": "json",
                        "options": {"temperature": 0.7, "num_ctx": 8192},
                    },
                )
                if resp.status_code == 200:
                    text = resp.json().get("response", "")
                    parsed = _extract_json(text)
                    if parsed:
                        logger.info("Ollama (%s) returned valid JSON for %s", ollama_model, cache_key)
                        return parsed
                    logger.warning(
                        "Ollama (%s) returned non-JSON response for %s: %s",
                        ollama_model,
                        cache_key,
                        text[:500],
                    )
                    last_error = "Ollama non-JSON response"
                else:
                    logger.warning(
                        "Ollama returned non-200 status: %s %s - %s",
                        resp.status_code,
                        resp.reason_phrase,
                        resp.text[:500],
                    )
                    last_error = f"Ollama HTTP {resp.status_code}"
        except httpx.TimeoutException as e:
            last_error = f"Ollama timeout ({ollama_timeout}s)"
            logger.warning("Ollama (%s) timeout for %s (attempt %d): %s", ollama_model, cache_key, attempt + 1, e)
        except Exception as e:  # noqa: BLE001
            last_error = f"Ollama {type(e).__name__}: {e}"
            logger.warning("Ollama (%s) call failed for %s (attempt %d): %s", ollama_model, cache_key, attempt + 1, e)

    # 2. DeepSeek (OpenAI-compatible cloud backup)
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if deepseek_key:
        try:
            logger.info("Falling back to DeepSeek for %s", cache_key)
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={
                        "Authorization": f"Bearer {deepseek_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek-chat",
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": 0.7,
                        "max_tokens": 4096,
                    },
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"]
                    parsed = _extract_json(text)
                    if parsed:
                        logger.info("DeepSeek returned valid JSON for %s", cache_key)
                        return parsed
                    logger.warning(
                        "DeepSeek returned non-JSON response for %s: %s",
                        cache_key,
                        text[:500],
                    )
                    last_error = "DeepSeek non-JSON response"
                else:
                    logger.warning(
                        "DeepSeek returned non-200 status: %s %s - %s",
                        resp.status_code,
                        resp.reason_phrase,
                        resp.text[:500],
                    )
                    last_error = f"DeepSeek HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            last_error = f"DeepSeek {type(e).__name__}: {e}"
            logger.warning("DeepSeek call failed for %s: %s", cache_key, e)
    else:
        last_error = "DeepSeek API key not configured"
        logger.info("DEEPSEEK_API_KEY not set; skipping DeepSeek")

    logger.error("All LLM providers failed for %s (last error: %s)", cache_key, last_error)
    return None


# ------------------------------ SCRIPT ------------------------------

SCRIPT_SYSTEM = (
    "You are a senior YouTube scriptwriter specialised in faceless, high-retention videos. "
    "Always respond with strict JSON, no commentary. All text in English."
)


def _coerce_script(result: dict) -> dict:
    """Normalize a parsed script JSON into the expected schema."""
    if not isinstance(result, dict):
        return None
    out = {
        "hook_option_one": str(result.get("hook_option_one") or ""),
        "hook_option_two": str(result.get("hook_option_two") or ""),
        "hook_option_three": str(result.get("hook_option_three") or ""),
        "selected_hook": str(result.get("selected_hook") or ""),
        "full_script": "",
        "retention_beats": [],
        "cta_block": str(result.get("cta_block") or ""),
    }
    fs = result.get("full_script")
    if isinstance(fs, str):
        out["full_script"] = fs
    elif isinstance(fs, list):
        # Some models return a list of {time, text} objects or plain strings
        parts = []
        for item in fs:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        out["full_script"] = " ".join(parts)
    beats = result.get("retention_beats")
    if isinstance(beats, list):
        out["retention_beats"] = [str(b) for b in beats if b]
    return out


def _finalize_script(out: dict) -> dict:
    """Attach derived stats so API consumers never see missing fields."""
    words = len(re.findall(r"\b\w+\b", out.get("full_script") or ""))
    out["word_count"] = words
    out["estimated_duration"] = int(words / 2.5)
    return out


async def generate_script(project: dict) -> dict:
    target_dur = int(project.get("target_duration", 300))
    target_words = max(120, int(target_dur / 60 * 150))

    user = f"""Generate a faceless YouTube script as strict JSON with this exact shape:
{{
  "hook_option_one": "short curiosity hook under 15 words",
  "hook_option_two": "second hook option",
  "hook_option_three": "third hook option",
  "selected_hook": "the best hook from the three",
  "full_script": "THE FULL SCRIPT AS ONE SINGLE STRING. Do NOT use a list or array here.",
  "retention_beats": ["beat 1", "beat 2", "beat 3"],
  "cta_block": "call to action"
}}

Topic: {project.get('topic', project.get('title', 'business success story'))}
Niche: {project.get('niche', 'business')}
Target duration: {target_dur}s (~{target_words} words)
Tone: {project.get('tone', 'documentary')}

CRITICAL: full_script must be ONE continuous string of ~{target_words} words, not a list or object.
Write specifically about the Topic above. Do NOT fall back to a generic entrepreneur/startup origin story (instant noodles, sleeping in the office, investors saying no, first 100 customers) unless the Topic explicitly asks for one."""

    result = await _llm_json(SCRIPT_SYSTEM, user, f"script-{project.get('id', uuid.uuid4())}")
    if result:
        coerced = _coerce_script(result)
        if coerced and coerced.get("full_script", "").strip():
            return _finalize_script(coerced)

    # Deterministic fallback — on-topic skeleton, never a canned unrelated story
    topic = (project.get("topic") or project.get("title") or "this subject").strip().rstrip("?")
    return _finalize_script({
        "hook_option_one": f"What nobody tells you about {topic}",
        "hook_option_two": f"The truth about {topic} most people miss",
        "hook_option_three": f"Why {topic} matters more than you think",
        "selected_hook": f"What nobody tells you about {topic}",
        "full_script": (
            f"This video is about {topic}. "
            "In the next few minutes, you will learn what really drives it, "
            "why most people misunderstand it, and what you can do differently starting today. "
            f"Let's begin with the fundamentals. When people first encounter {topic}, "
            "they usually focus on the surface and miss the mechanisms underneath. "
            "That is exactly where the advantage hides. "
            "The first key insight is that small, consistent decisions compound. "
            "What looks like overnight success is almost always the visible tip of a long, deliberate process. "
            "The second insight is that data beats guesswork. "
            "The people who win in this space measure what works, cut what does not, and iterate fast. "
            "The third insight is that distribution matters as much as quality. "
            "Even the best work fails in silence if nobody sees it. "
            "So here is the takeaway: understand the fundamentals, measure everything, and ship consistently. "
            "Do that, and the results stop being luck and start being math."
        ),
        "retention_beats": [
            "The fundamentals most people skip",
            "Why data beats guesswork",
            "The distribution mistake that kills good work",
        ],
        "cta_block": "If this was useful, subscribe. New videos like this every week.",
    })


# ------------------------------ SCENE PLAN ------------------------------

SCENE_SYSTEM = (
    "You are a video editor planning scene breakdowns for faceless YouTube videos. "
    "Always respond with strict JSON. No commentary."
)


def _clean_search_terms(terms: list, narration: str, *, min_terms: int = 4) -> list[str]:
    """Filter LLM search_terms down to real stock-video query terms and
    top up from the narration until we hold at least ``min_terms``.

    Drops garbage tokens like "STARTUPSTARTUPSTARTU" (repeat artifacts) via
    ``is_garbage_token`` — a term is kept only if every word in it is clean.
    """
    out: list[str] = []
    for t in terms:
        t = " ".join(str(t or "").split()).strip()
        if not t:
            continue
        words = t.lower().split()
        if any(is_garbage_token(w) for w in words):
            continue
        if t.lower() in (x.lower() for x in out):
            continue
        out.append(t)
    if len(out) < min_terms:
        for kw in extract_visual_keywords(narration, top_n=min_terms):
            if len(out) >= min_terms:
                break
            if not any(kw in x.lower() for x in out):
                out.append(kw)
    return out[:6]


def _coerce_scenes(scenes: list) -> list[dict]:
    """Normalize scene objects to the expected schema."""
    out = []
    for s in scenes:
        if not isinstance(s, dict):
            continue
        narration = str(s.get("narration_text") or s.get("caption_text") or "")
        search_terms = _clean_search_terms(s.get("search_terms") or [], narration)
        out.append({
            "scene_number": int(s.get("scene_number") or 0) or len(out) + 1,
            "start_time": float(s.get("start_time") or 0),
            "end_time": float(s.get("end_time") or 0),
            "duration": float(s.get("duration") or 0),
            "narration_text": narration,
            "visual_direction": str(s.get("visual_direction") or ""),
            # Word-boundary cut at 80 chars — never mid-word, never a
            # hard [:120] slice that mangles the phrase.
            "caption_text": truncate_words(s.get("caption_text") or s.get("narration_text") or "", 80),
            "search_terms": search_terms,
        })
    return out


async def generate_scenes(project: dict, full_script: str) -> list[dict]:
    """Back-compat wrapper used by routes.generate_scenes_endpoint — adapts
    the (project, full_script: str) call signature to ``generate_scene_plan``.
    """
    return await generate_scene_plan(project, {"full_script": full_script or ""})


async def generate_scene_plan(project: dict, script: dict) -> list[dict]:
    target_dur = int(project.get("target_duration", 300))
    full_script = script.get("full_script", "")
    if not isinstance(full_script, str):
        full_script = ""

    user = f"""Break this script into scenes for a {target_dur}s faceless YouTube video.
Return strict JSON with this exact shape:
{{"scenes": [
  {{
    "scene_number": 1,
    "start_time": 0.0,
    "end_time": 30.0,
    "duration": 30.0,
    "narration_text": "exact script excerpt for this scene",
    "visual_direction": "what stock footage to show — be specific to the topic",
    "caption_text": "key phrase for caption overlay",
    "search_terms": ["term1", "term2", "term3"]
  }}
]}}

The visual_direction and search_terms MUST be specific to the topic, not generic business footage.

Script: {full_script[:3000]}
Topic: {project.get('topic', project.get('title', ''))}
Niche: {project.get('niche', 'business')}"""

    result = await _llm_json(SCENE_SYSTEM, user, f"scenes-{project.get('id', uuid.uuid4())}")
    if result and isinstance(result.get("scenes"), list):
        coerced = _coerce_scenes(result["scenes"])
        if coerced:
            return coerced

    # Deterministic fallback — visuals derived from each scene's narration,
    # never from the raw project topic/description text
    scene_count = 5
    scene_dur = target_dur / scene_count
    sentences = re.split(r'(?<=[.!?])\s+', full_script) if full_script else ["Scene content."] * scene_count
    chunk = max(1, len(sentences) // scene_count)
    scenes = []
    for i in range(scene_count):
        start = i * scene_dur
        narration = " ".join(sentences[i * chunk:(i + 1) * chunk]) or f"Part {i + 1} of the video."
        terms = derive_search_terms(narration, i, scene_count)
        scenes.append({
            "scene_number": i + 1,
            "start_time": round(start, 1),
            "end_time": round(start + scene_dur, 1),
            "duration": round(scene_dur, 1),
            "narration_text": narration,
            "visual_direction": f"stock b-roll illustrating: {terms[0]}",
            "caption_text": f"Part {i + 1}",
            "search_terms": terms,
        })
    return scenes


# ------------------------------ METADATA ------------------------------

META_SYSTEM = (
    "You are a YouTube SEO expert. Always respond with strict JSON. No commentary."
)


async def generate_metadata(project: dict, script: dict, scenes: list = None) -> dict:
    if isinstance(script, str):
        script = {"full_script": script}
    topic = project.get("topic", project.get("title", "business story"))
    hook = script.get("selected_hook", topic)

    user = f"""Generate YouTube metadata JSON:
{{
  "title_options": ["title1", "title2", "title3"],
  "selected_title": "best title",
  "description": "150-200 word description with keywords",
  "tags": ["tag1", "tag2", ...],
  "hashtags": ["#hashtag1", "#hashtag2", ...],
  "chapters": [{{"time": "0:00", "label": "Intro"}}, ...],
  "pinned_comment": "engaging pinned comment to drive discussion"
}}

Topic: {topic}
Hook: {hook}

Write specifically about this Topic. Do NOT use a generic entrepreneur/startup origin-story framing (billion dollar empire, instant noodles, investors said no) unless the Topic explicitly asks for it."""

    result = await _llm_json(META_SYSTEM, user, f"meta-{project.get('id', uuid.uuid4())}")
    if result:
        return result

    short_topic = truncate_words(topic, 60)
    return {
        "title_options": [
            f"{short_topic} — Explained",
            f"The Truth About {short_topic}",
            f"{short_topic}: What You Need to Know",
        ],
        "selected_title": f"{short_topic} — Explained",
        "description": (
            f"This video breaks down {short_topic}. "
            "We cover what really drives it, the mistakes most people make, "
            "and the practical takeaways you can apply right away. "
            "Subscribe for new videos every week."
        ),
        "tags": [short_topic, project.get("niche", "education"), "explained", "guide", "how it works"],
        "hashtags": ["#explained", "#education", "#howto"],
        "chapters": [
            {"time": "0:00", "label": "Introduction"},
            {"time": "1:00", "label": "The Fundamentals"},
            {"time": "2:30", "label": "Key Insights"},
            {"time": "4:00", "label": "Practical Takeaways"},
            {"time": "5:00", "label": "Wrap-Up"},
        ],
        "pinned_comment": f"What surprised you most about {short_topic}? Comment below 👇",
    }


# ------------------------------ THUMBNAIL CONCEPTS ------------------------------

THUMB_SYSTEM = (
    "You are a YouTube thumbnail designer. Always respond with strict JSON, no commentary. "
    "Each concept must include: thumbnail_title_text, visual_composition, emotion_angle, "
    "background_idea, subject_focal_point, colour_direction, click_trigger, image_prompt."
)


def _coerce_thumbnail_concepts(concepts: list) -> list[dict]:
    """Normalise thumbnail concepts to the schema expected by models + frontend."""
    keys = [
        "thumbnail_title_text", "visual_composition", "emotion_angle",
        "background_idea", "subject_focal_point", "colour_direction",
        "click_trigger", "image_prompt",
    ]
    out = []
    for c in concepts:
        if not isinstance(c, dict):
            continue
        out.append({k: str(c.get(k) or "").strip() for k in keys})
    return out


async def generate_thumbnail_concepts(project: dict, script: dict) -> list[dict]:
    topic = project.get("topic", project.get("title", "business story"))
    niche = project.get("niche", "business")

    user = f"""Generate 3 YouTube thumbnail concepts as JSON:
{{"concepts": [
  {{
    "thumbnail_title_text": "bold 3-5 word title",
    "visual_composition": "description of layout and framing",
    "emotion_angle": "curiosity|shock|inspiration|fear",
    "background_idea": "description of background / setting",
    "subject_focal_point": "the single dominant subject",
    "colour_direction": "primary and accent colors",
    "click_trigger": "why a viewer would click",
    "image_prompt": "detailed image-generation prompt, no text baked in, 16:9"
  }}
]}}

Topic: {topic}
Niche: {niche}"""

    result = await _llm_json(THUMB_SYSTEM, user, f"thumb-{project.get('id', uuid.uuid4())}")
    if result and isinstance(result.get("concepts"), list):
        coerced = _coerce_thumbnail_concepts(result["concepts"])
        if coerced:
            return coerced

    # Topic-aware deterministic fallback — word-boundary cut, no clickbait
    # auto-suffix (the old "… EXPOSED" append truncated titles mid-word).
    short_topic = truncate_words(topic, 40)
    return [
        {
            "thumbnail_title_text": short_topic.upper(),
            "visual_composition": "Dark background with bold centered text and single focal graphic",
            "emotion_angle": "curiosity",
            "background_idea": "Textured black background with subtle gradient",
            "subject_focal_point": f"Symbol or imagery representing {short_topic}",
            "colour_direction": "Black background, yellow text, green accents",
            "click_trigger": "Contradiction — implies common belief is wrong",
            "image_prompt": (
                f"YouTube thumbnail, 16:9, ultra-sharp, high contrast, dark textured background, "
                f"large bold yellow headline text, green accent graphic related to {short_topic}, "
                "cinematic lighting, no baked text"
            ),
        },
        {
            "thumbnail_title_text": f"THE {short_topic.upper()} TRUTH",
            "visual_composition": "Split screen: before vs after / problem vs solution",
            "emotion_angle": "shock",
            "background_idea": "Red and black high-contrast split background",
            "subject_focal_point": f"Two contrasting visuals of {short_topic}",
            "colour_direction": "Red and black, high contrast",
            "click_trigger": "Transformation and revelation",
            "image_prompt": (
                f"YouTube thumbnail, 16:9, dramatic split-screen composition, dark red and black, "
                f"contrasting visuals representing {short_topic}, cinematic lighting, no baked text"
            ),
        },
        {
            "thumbnail_title_text": f"{short_topic.upper()} SECRETS",
            "visual_composition": "Documentary style, serious tone, single focal subject",
            "emotion_angle": "intrigue",
            "background_idea": "Dark teal textured background with subtle film grain",
            "subject_focal_point": f"Mysterious central subject related to {short_topic}",
            "colour_direction": "Dark teal and gold",
            "click_trigger": "Hidden story promise",
            "image_prompt": (
                "YouTube thumbnail, 16:9, documentary style, dark teal background, gold accents, "
                "mysterious central subject, film grain, cinematic lighting, no baked text"
            ),
        },
    ]

# Alias for backwards compat - routes.py expects generate_thumbnails
async def generate_thumbnails(project: dict, script: dict = None):
    if script is None:
        script = project.get("full_script", {}) if isinstance(project, dict) else {}
        if isinstance(script, str):
            script = {"full_script": script}
    return await generate_thumbnail_concepts(project, script)
