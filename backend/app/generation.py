"""LLM-based generation — local Ollama primary, Anthropic Claude backup.

Produces structured JSON for: Script, Scene plan, Metadata, Thumbnail concepts.
Falls back to deterministic generation if no LLM is available.
"""
from __future__ import annotations

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

CRITICAL: full_script must be ONE continuous string of ~{target_words} words, not a list or object."""

    result = await _llm_json(SCRIPT_SYSTEM, user, f"script-{project.get('id', uuid.uuid4())}")
    if result:
        coerced = _coerce_script(result)
        if coerced and coerced.get("full_script", "").strip():
            return coerced

    # Deterministic fallback
    topic = project.get("topic", project.get("title", "an entrepreneur"))
    return {
        "hook_option_one": f"How {topic} built a billion dollar empire from nothing",
        "hook_option_two": f"Nobody believed in {topic}. Then this happened.",
        "hook_option_three": f"The untold story of how {topic} changed everything",
        "selected_hook": f"How {topic} built a billion dollar empire from nothing",
        "full_script": (
            f"What you're about to hear is the story of {topic}. "
            "It started with nothing. No money, no connections, no blueprint. "
            "Just an idea and an obsession with making it work. "
            "In the beginning, every door was closed. Every investor said no. "
            "Every mentor said the market was too competitive. "
            "But that rejection became fuel. "
            "The first year was brutal. Eighteen hour days. "
            "Eating instant noodles. Sleeping in the office. "
            "But something was being built — slowly, brick by brick. "
            "The turning point came when the product found its first hundred customers. "
            "Word spread. Revenue grew. The vision became real. "
            "Today, the empire stands as proof that timing matters less than obsession. "
            "The lesson? Start before you're ready. Build before they believe. "
            "Because the people who change industries are never the ones who waited for permission."
        ),
        "retention_beats": [
            "The moment everything almost collapsed",
            "The decision that changed everything",
            "What nobody tells you about building from zero",
        ],
        "cta_block": "If this story inspired you, subscribe. More stories like this every week.",
    }


# ------------------------------ SCENE PLAN ------------------------------

SCENE_SYSTEM = (
    "You are a video editor planning scene breakdowns for faceless YouTube videos. "
    "Always respond with strict JSON. No commentary."
)


def _coerce_scenes(scenes: list) -> list[dict]:
    """Normalize scene objects to the expected schema."""
    out = []
    for s in scenes:
        if not isinstance(s, dict):
            continue
        out.append({
            "scene_number": int(s.get("scene_number") or 0) or len(out) + 1,
            "start_time": float(s.get("start_time") or 0),
            "end_time": float(s.get("end_time") or 0),
            "duration": float(s.get("duration") or 0),
            "narration_text": str(s.get("narration_text") or s.get("caption_text") or ""),
            "visual_direction": str(s.get("visual_direction") or ""),
            "caption_text": str(s.get("caption_text") or s.get("narration_text") or "")[:120],
            "search_terms": [str(t) for t in (s.get("search_terms") or []) if t],
        })
    return out


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

    # Deterministic fallback — topic-specific visuals derived from the project
    scene_count = 5
    scene_dur = target_dur / scene_count
    sentences = re.split(r'(?<=[.!?])\s+', full_script) if full_script else ["Scene content."] * scene_count
    chunk = max(1, len(sentences) // scene_count)
    topic = project.get("topic", project.get("title", "the topic"))
    niche = project.get("niche", "documentary")
    # Build visuals from topic keywords instead of hard-coded business footage
    topic_words = " ".join(topic.split()[:6])
    visuals = [
        f"wide establishing shot of {topic_words}, cinematic",
        f"close-up detail shot related to {topic_words}",
        f"dynamic action shot illustrating {topic_words}",
        f"contextual environment shot for {topic_words}",
        f"iconic or symbolic image representing {topic_words}",
    ]
    scenes = []
    for i in range(scene_count):
        start = i * scene_dur
        narration = " ".join(sentences[i * chunk:(i + 1) * chunk]) or f"Part {i + 1} of the story."
        visual = visuals[i]
        scenes.append({
            "scene_number": i + 1,
            "start_time": round(start, 1),
            "end_time": round(start + scene_dur, 1),
            "duration": round(scene_dur, 1),
            "narration_text": narration,
            "visual_direction": visual,
            "caption_text": f"Part {i + 1}",
            "search_terms": [topic, niche, visual.split(",")[0]],
        })
    return scenes


# ------------------------------ METADATA ------------------------------

META_SYSTEM = (
    "You are a YouTube SEO expert. Always respond with strict JSON. No commentary."
)


async def generate_metadata(project: dict, script: dict, scenes: list = None) -> dict:
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
Hook: {hook}"""

    result = await _llm_json(META_SYSTEM, user, f"meta-{project.get('id', uuid.uuid4())}")
    if result:
        return result

    return {
        "title_options": [
            f"How {topic} Built an Empire From Nothing",
            f"The Untold Story of {topic}",
            f"{topic}: From Zero to Billions",
        ],
        "selected_title": f"How {topic} Built an Empire From Nothing",
        "description": (
            f"The inspiring story of {topic} and the lessons every entrepreneur needs to hear. "
            "This video breaks down the exact decisions, mindset shifts, and turning points "
            "that separated success from failure. Whether you're building a business or just "
            "getting started, these lessons apply. Subscribe for weekly business empire stories."
        ),
        "tags": [topic, "entrepreneur", "business", "success story", "startup", "motivation",
                 "how to build a business", "business empire", "millionaire mindset"],
        "hashtags": ["#entrepreneur", "#business", "#success", "#motivation", "#startup"],
        "chapters": [
            {"time": "0:00", "label": "The Beginning"},
            {"time": "1:00", "label": "The Struggle"},
            {"time": "2:30", "label": "The Turning Point"},
            {"time": "4:00", "label": "The Breakthrough"},
            {"time": "5:00", "label": "The Lesson"},
        ],
        "pinned_comment": f"What part of {topic}'s story resonated most with you? Comment below 👇",
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

    # Topic-aware deterministic fallback
    short_topic = topic[:40]
    return [
        {
            "thumbnail_title_text": f"{short_topic.upper()} EXPOSED",
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
