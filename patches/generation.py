"""LLM-based generation — Anthropic Claude direct (replaces emergentintegrations).

Produces structured JSON for: Script, Scene plan, Metadata, Thumbnail concepts.
Falls back to deterministic generation if ANTHROPIC_API_KEY missing.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import uuid
from typing import Optional

logger = logging.getLogger("facelessforge.generation")


def _script_text(script) -> str:
    if isinstance(script, dict):
        return str(script.get("full_script") or script.get("script") or "")
    return str(script or "")


def _normalise_script(data: dict, project: dict) -> dict:
    full = str(data.get("full_script") or "").strip()
    word_count = len(re.findall(r"\b\w+\b", full))
    estimated = int(word_count / 2.5) if word_count else int(project.get("target_duration", 300))
    return {
        "hook_option_one": str(data.get("hook_option_one") or data.get("selected_hook") or "").strip(),
        "hook_option_two": str(data.get("hook_option_two") or "").strip(),
        "hook_option_three": str(data.get("hook_option_three") or "").strip(),
        "selected_hook": str(data.get("selected_hook") or data.get("hook_option_one") or "").strip(),
        "full_script": full,
        "retention_beats": [str(x) for x in (data.get("retention_beats") or [])][:10],
        "cta_block": str(data.get("cta_block") or "").strip(),
        "word_count": word_count,
        "estimated_duration": estimated,
    }


def _normalise_scene(scene: dict, project: dict, idx: int, target_dur: int) -> dict:
    scene_number = int(scene.get("scene_number") or idx)
    start = float(scene.get("start_time") if scene.get("start_time") is not None else (idx - 1) * max(1, target_dur // 5))
    end = float(scene.get("end_time") if scene.get("end_time") is not None else start + float(scene.get("duration") or 6.0))
    narration = str(scene.get("narration_text") or "").strip()
    visual = str(scene.get("visual_direction") or "Stock b-roll matching the narration").strip()
    search_terms = [str(t) for t in (scene.get("search_terms") or []) if t][:6]
    topic = project.get("topic", project.get("title", "business story"))
    return {
        "id": str(scene.get("id") or uuid.uuid4()),
        "scene_number": scene_number,
        "start_time": round(start, 2),
        "end_time": round(max(end, start + 1.0), 2),
        "duration": round(max(end - start, 1.0), 2),
        "narration_text": narration,
        "visual_direction": visual,
        "asset_type": str(scene.get("asset_type") or "stock_video"),
        "search_terms": search_terms or [str(topic), "business", visual.split(",")[0]],
        "image_prompt": str(scene.get("image_prompt") or visual or narration[:120]),
        "caption_text": str(scene.get("caption_text") or narration[:80] or f"Part {scene_number}").strip(),
        "status": str(scene.get("status") or "planned"),
    }


def _normalise_metadata(data: dict, project: dict, scenes: list[dict]) -> dict:
    topic = project.get("topic", project.get("title", "business story"))
    titles = [str(t) for t in (data.get("title_options") or []) if t]
    while len(titles) < 3:
        titles.append(f"The Untold Story of {topic}")
    chapters = []
    for chapter in data.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        title = chapter.get("title") or chapter.get("label")
        if title:
            chapters.append({
                "timestamp": str(chapter.get("timestamp") or chapter.get("time") or "0:00"),
                "title": str(title),
            })
    if not chapters:
        for scene in scenes[:8]:
            start = int(scene.get("start_time") or 0)
            chapters.append({
                "timestamp": f"{start // 60}:{start % 60:02d}",
                "title": str(scene.get("caption_text") or f"Part {scene.get('scene_number', 1)}")[:50],
            })
    return {
        "title_options": titles[:12],
        "selected_title": str(data.get("selected_title") or titles[0]),
        "description": str(data.get("description") or "").strip(),
        "tags": [str(t).strip().lstrip("#") for t in (data.get("tags") or []) if t][:35],
        "hashtags": [str(h if str(h).startswith("#") else f"#{str(h).lstrip('#')}") for h in (data.get("hashtags") or []) if h][:8],
        "chapters": chapters,
        "pinned_comment": str(data.get("pinned_comment") or "").strip(),
    }


def _normalise_thumbnail(concept: dict, project: dict) -> dict:
    topic = project.get("topic", project.get("title", "business story"))
    headline = str(concept.get("thumbnail_title_text") or concept.get("headline") or topic)[:28]
    visual = str(concept.get("visual_composition") or concept.get("visual_style") or "Cinematic documentary frame")
    colour = str(concept.get("colour_direction") or concept.get("color_scheme") or "Dark teal and gold")
    emotion = str(concept.get("emotion_angle") or concept.get("emotion") or "curiosity")
    return {
        "thumbnail_title_text": headline,
        "visual_composition": visual,
        "emotion_angle": emotion,
        "background_idea": str(concept.get("background_idea") or visual),
        "subject_focal_point": str(concept.get("subject_focal_point") or topic),
        "colour_direction": colour,
        "click_trigger": str(concept.get("click_trigger") or emotion),
        "image_prompt": str(concept.get("image_prompt") or f"{visual}, subject is {topic}, {colour}, 16:9"),
    }


def _llm_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


async def _llm_json(system: str, user: str, session_id: str) -> Optional[dict]:
    """Call Claude and parse JSON from response."""
    if not _llm_available():
        return None
    try:
        import httpx
        api_key = os.environ["ANTHROPIC_API_KEY"]
        model = os.environ.get("LLM_MODEL", "claude-sonnet-4-20250514")

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 2048,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["content"][0]["text"].strip()

        # Strip markdown fence if present
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*", "", text).rstrip("`").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        return json.loads(m.group(0))
    except Exception as e:
        logger.warning("[llm] generation failed, using fallback: %s", e)
        return None


# ------------------------------ SCRIPT ------------------------------

SCRIPT_SYSTEM = (
    "You are a senior YouTube scriptwriter specialised in faceless, high-retention videos. "
    "Always respond with strict JSON, no commentary. All text in English."
)


async def generate_script(project: dict) -> dict:
    target_dur = int(project.get("target_duration", 300))
    target_words = max(120, int(target_dur / 60 * 150))

    user = f"""Generate a faceless YouTube script as JSON with this exact shape:
{{
  "hook_option_one": "...",
  "hook_option_two": "...",
  "hook_option_three": "...",
  "selected_hook": "...",
  "full_script": "...",
  "retention_beats": ["...","...","..."],
  "cta_block": "..."
}}

Topic: {project.get('topic', project.get('title', 'business success story'))}
Niche: {project.get('niche', 'business')}
Target duration: {target_dur}s (~{target_words} words)
Tone: {project.get('tone', 'documentary')}

Rules: hook must be <15 words and create curiosity. full_script must be ~{target_words} words. No filler."""

    result = await _llm_json(SCRIPT_SYSTEM, user, f"script-{project.get('id', uuid.uuid4())}")
    if result:
        return _normalise_script(result, project)

    # Deterministic fallback
    topic = project.get("topic", project.get("title", "an entrepreneur"))
    return _normalise_script({
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
    }, project)


# ------------------------------ SCENE PLAN ------------------------------

SCENE_SYSTEM = (
    "You are a video editor planning scene breakdowns for faceless YouTube videos. "
    "Always respond with strict JSON. No commentary."
)


async def generate_scene_plan(project: dict, script) -> list[dict]:
    target_dur = int(project.get("target_duration", 300))
    full_script = _script_text(script)

    user = f"""Break this script into scenes for a {target_dur}s faceless YouTube video.
Return JSON: {{"scenes": [list of scene objects]}}

Each scene object:
{{
  "scene_number": 1,
  "start_time": 0.0,
  "end_time": 30.0,
  "duration": 30.0,
  "narration_text": "exact script excerpt for this scene",
  "visual_direction": "what stock footage to show — be specific",
  "caption_text": "key phrase for caption overlay",
  "search_terms": ["term1", "term2", "term3"]
}}

Script: {full_script[:3000]}
Topic: {project.get('topic', project.get('title', ''))}"""

    result = await _llm_json(SCENE_SYSTEM, user, f"scenes-{project.get('id', uuid.uuid4())}")
    if result and isinstance(result.get("scenes"), list):
        return [_normalise_scene(s, project, i, target_dur) for i, s in enumerate(result["scenes"], start=1)]

    # Deterministic fallback — 5 equal scenes
    scene_dur = target_dur / 5
    sentences = re.split(r'(?<=[.!?])\s+', full_script) if full_script else ["Scene content."] * 5
    chunk = max(1, len(sentences) // 5)
    topic = project.get("topic", project.get("title", "entrepreneurship"))
    visuals = [
        f"aerial city skyline at dawn, business district",
        f"entrepreneur working at desk, laptop, coffee, focused",
        f"team meeting, whiteboard, brainstorming session",
        f"graph showing exponential growth, data visualization",
        f"successful businessperson, modern office, confident",
    ]
    scenes = []
    for i in range(5):
        start = i * scene_dur
        narration = " ".join(sentences[i * chunk:(i + 1) * chunk]) or f"Part {i + 1} of the story."
        scenes.append(_normalise_scene({
            "scene_number": i + 1,
            "start_time": round(start, 1),
            "end_time": round(start + scene_dur, 1),
            "duration": round(scene_dur, 1),
            "narration_text": narration,
            "visual_direction": visuals[i],
            "caption_text": f"Part {i + 1}",
            "search_terms": [topic, "business", visuals[i].split(",")[0]],
        }, project, i + 1, target_dur))
    return scenes


async def generate_scenes(project: dict, script_text: str) -> list[dict]:
    return await generate_scene_plan(project, {"full_script": script_text})


# ------------------------------ METADATA ------------------------------

META_SYSTEM = (
    "You are a YouTube SEO expert. Always respond with strict JSON. No commentary."
)


async def generate_metadata(project: dict, script, scenes: list[dict] | None = None) -> dict:
    scenes = scenes or []
    topic = project.get("topic", project.get("title", "business story"))
    hook = script.get("selected_hook", topic) if isinstance(script, dict) else topic

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
        return _normalise_metadata(result, project, scenes)

    return _normalise_metadata({
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
            {"timestamp": "0:00", "title": "The Beginning"},
            {"timestamp": "1:00", "title": "The Struggle"},
            {"timestamp": "2:30", "title": "The Turning Point"},
            {"timestamp": "4:00", "title": "The Breakthrough"},
            {"timestamp": "5:00", "title": "The Lesson"},
        ],
        "pinned_comment": f"What part of {topic}'s story resonated most with you? Comment below 👇",
    }, project, scenes)


# ------------------------------ THUMBNAIL CONCEPTS ------------------------------

THUMB_SYSTEM = (
    "You are a YouTube thumbnail designer. Always respond with strict JSON. No commentary."
)


async def generate_thumbnail_concepts(project: dict, script=None) -> list[dict]:
    topic = project.get("topic", project.get("title", "business story"))

    user = f"""Generate 3 YouTube thumbnail concepts as JSON:
{{"concepts": [
  {{
    "concept_id": "A",
    "headline": "bold 3-5 word headline",
    "subheadline": "supporting text",
    "visual_style": "description of visual",
    "color_scheme": "primary and accent colors",
    "emotion": "curiosity|shock|inspiration|fear"
  }}
]}}

Topic: {topic}"""

    result = await _llm_json(THUMB_SYSTEM, user, f"thumb-{project.get('id', uuid.uuid4())}")
    if result and isinstance(result.get("concepts"), list):
        return [_normalise_thumbnail(c, project) for c in result["concepts"][:3]]

    return [_normalise_thumbnail(c, project) for c in [
        {
            "concept_id": "A",
            "headline": "FROM $0 TO BILLIONS",
            "subheadline": topic,
            "visual_style": "Dark background, bold yellow text, upward arrow graphic",
            "color_scheme": "Black background, yellow text, green accents",
            "emotion": "inspiration",
        },
        {
            "concept_id": "B",
            "headline": "NOBODY BELIEVED HIM",
            "subheadline": f"Then {topic} changed everything",
            "visual_style": "Split screen: struggling vs successful",
            "color_scheme": "Red and black, high contrast",
            "emotion": "curiosity",
        },
        {
            "concept_id": "C",
            "headline": "THE UNTOLD STORY",
            "subheadline": topic,
            "visual_style": "Documentary style, serious tone",
            "color_scheme": "Dark teal and gold",
            "emotion": "intrigue",
        },
    ]]


async def generate_thumbnails(project: dict, script=None) -> list[dict]:
    return await generate_thumbnail_concepts(project, script)
