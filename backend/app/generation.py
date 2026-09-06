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
from dotenv import load_dotenv
load_dotenv()

import random
import re
import uuid
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("facelessforge.generation")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# FIX 1: Strip BEAT markers and other structural labels from text paths.
# LLMs sometimes leak section labels like "BEAT 1", "Beat 2: ...", or
# "INTRO/OUTRO/CTA" into narration/caption text — these are internal
# scaffolding and must never reach the renderer (would burn into the video).
_BEAT_MARKER_RE = re.compile(r"BEAT\s*\d+", re.IGNORECASE)
_SECTION_LABEL_RE = re.compile(
    r"^\s*(HOOK|INTRO|OUTRO|CTA|SCENE\s*\d*|BEAT\s*\d*)\s*[:\-–—]\s*",
    re.IGNORECASE | re.MULTILINE,
)


def strip_beat_markers(text: str) -> str:
    """Remove BEAT markers and leading section labels from script text.

    Applied to every narration/caption path before it reaches the renderer.
    Safe to call on empty/None values.
    """
    if not text:
        return ""
    cleaned = _BEAT_MARKER_RE.sub("", text or "")
    cleaned = _SECTION_LABEL_RE.sub("", cleaned)
    # Collapse whitespace introduced by removals
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    return cleaned.strip()


# Backwards-compat: also exported as `strip_beat_artifacts` for any importer
# that uses the old name.
strip_beat_artifacts = strip_beat_markers


def _load_prompt_template(name: str) -> Optional[str]:
    """Load a prompt template from app/prompts/, None if missing/unreadable."""
    try:
        return (PROMPTS_DIR / name).read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Prompt template %s unavailable: %s", name, e)
        return None


def _is_educational_topic(project: dict) -> bool:
    """Route pet / educational topics to the educational script template."""
    topic = (project.get("topic") or project.get("title") or "").lower()
    audience = (project.get("audience") or "").strip().lower()
    return True if any(k in (topic+" "+audience) for k in ["pet","dog","cat","mental","health","how much"]) else False


def _render_educational_prompt(project: dict, template: str, target_words: int) -> str:
    """Fill the educational template placeholders from the project."""
    filled = template
    replacements = {
        "{topic}": project.get("topic") or project.get("title") or "this subject",
        "{audience}": project.get("audience") or "general audience",
        "{tone}": project.get("tone") or "documentary",
        "{voice}": project.get("voice_style") or "neutral male narrator",
        "{visual}": project.get("visual_style") or "cinematic b-roll",
        "{cta}": project.get("cta") or "Subscribe for more videos like this.",
    }
    for key, value in replacements.items():
        filled = filled.replace(key, str(value))
    return filled + f"""

Respond with strict JSON in this exact shape:
{{
  "hook_option_one": "short curiosity hook under 15 words",
  "hook_option_two": "second hook option",
  "hook_option_three": "third hook option",
  "selected_hook": "the best hook from the three",
  "full_script": "THE FULL SCRIPT AS ONE SINGLE STRING. Do NOT use a list or array here.",
  "retention_beats": ["beat 1", "beat 2", "beat 3"],
  "cta_block": "call to action"
}}

CRITICAL: full_script must be ONE continuous string of ~{target_words} words, not a list or object."""


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
    """DeepSeek $15 credit first, then Kimi, then Claude."""
    from dotenv import load_dotenv
    load_dotenv()
    last_error: str = ""
    lower_user = user.lower()
    is_edu = any(k in lower_user for k in ["pet", "dog", "cat", "mental health", "how much", "whether", "anxiety", "depression", "pet ownership", "wellbeing"])

    # 1. DeepSeek - YOUR WORKING KEY
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if deepseek_key:
        try:
            logger.info(f"DeepSeek for {cache_key} edu={is_edu}")
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": f"Bearer {deepseek_key}", "Content-Type": "application/json"},
                    json={"model": "deepseek-chat", "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.7, "max_tokens": 4096},
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"]
                    parsed = _extract_json(text)
                    if parsed:
                        logger.info(f"DeepSeek OK {cache_key}")
                        return parsed
                    else:
                        last_error = f"DeepSeek no JSON: {text[:200]}"
                else:
                    last_error = f"DeepSeek {resp.status_code}: {resp.text[:300]}"
                    logger.warning(last_error)
        except Exception as e:
            last_error = f"DeepSeek {e}"
            logger.warning(last_error)

    # 2. Kimi
    kimi_key = os.environ.get("KIMI_API_KEY", "").strip() or os.environ.get("MOONSHOT_API_KEY", "").strip()
    if kimi_key:
        try:
            kimi_model = os.environ.get("KIMI_MODEL", "kimi-k2-0711-preview").strip()
            logger.info(f"Kimi {kimi_model} for {cache_key}")
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    "https://api.moonshot.cn/v1/chat/completions",
                    headers={"Authorization": f"Bearer {kimi_key}", "Content-Type": "application/json"},
                    json={"model": kimi_model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.7, "max_tokens": 4096},
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"]
                    parsed = _extract_json(text)
                    if parsed:
                        return parsed
                last_error = f"Kimi {resp.status_code}"
        except Exception as e:
            last_error = f"Kimi {e}"

    # 3. Claude - fixed model name
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if anthropic_key:
        try:
            # fixed model - old one you had is gone
            anthropic_model = os.environ.get("LLM_MODEL", "claude-3-5-sonnet-latest").strip()
            if "20241022" in anthropic_model:
                anthropic_model = "claude-3-5-sonnet-latest"
            logger.info(f"Claude {anthropic_model} for {cache_key}")
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": anthropic_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                    json={"model": anthropic_model, "system": system, "messages": [{"role": "user", "content": user}], "max_tokens": 4096, "temperature": 0.7},
                )
                if resp.status_code == 200:
                    text = "".join([b.get("text","") for b in resp.json().get("content",[])])
                    parsed = _extract_json(text)
                    if parsed:
                        return parsed
                else:
                    last_error = f"Claude {resp.status_code}: {resp.text[:300]}"
                    logger.warning(last_error)
        except Exception as e:
            last_error = f"Claude {e}"

    logger.error(f"All LLMs failed {cache_key}: {last_error}")
    return None


# ------------------------------ SCRIPT ------------------------------

SCRIPT_SYSTEM = (
    "You are a senior YouTube scriptwriter specialised in faceless, high-retention videos. "
    "Always respond with strict JSON, no commentary. All text in English."
)


def _coerce_script(result: dict) -> dict:
    """Normalize a parsed script JSON into the expected schema.
    All text fields are scrubbed of BEAT markers and structural labels."""
    if not isinstance(result, dict):
        return None
    raw_fs = result.get("full_script")
    fs_text = ""
    if isinstance(raw_fs, str):
        fs_text = strip_beat_markers(raw_fs)
    elif isinstance(raw_fs, list):
        # Some models return a list of {time, text} objects or plain strings
        parts = []
        for item in raw_fs:
            if isinstance(item, dict):
                parts.append(strip_beat_markers(str(item.get("text") or "")))
            elif isinstance(item, str):
                parts.append(strip_beat_markers(item))
        fs_text = " ".join(parts)
    out = {
        "hook_option_one": strip_beat_markers(str(result.get("hook_option_one") or "")),
        "hook_option_two": strip_beat_markers(str(result.get("hook_option_two") or "")),
        "hook_option_three": strip_beat_markers(str(result.get("hook_option_three") or "")),
        "selected_hook": strip_beat_markers(str(result.get("selected_hook") or "")),
        "full_script": fs_text,
        "retention_beats": [],
        "cta_block": strip_beat_markers(str(result.get("cta_block") or "")),
    }
    beats = result.get("retention_beats")
    if isinstance(beats, list):
        out["retention_beats"] = [strip_beat_markers(str(b)) for b in beats if b]
    return out


def _finalize_script(out: dict) -> dict:
    """Attach derived stats so API consumers never see missing fields."""
    words = len(re.findall(r"\b\w+\b", out.get("full_script") or ""))
    out["word_count"] = words
    out["estimated_duration"] = int(words / 2.5)
    return out


async def generate_script(project: dict) -> dict:
    # EDUCATIONAL routing - use educational_script.txt for pet/health topics.
    # These topics MUST never run through the generic business origin-story prompt.
    _topic_l = (project.get("topic") or project.get("title") or "").lower()
    _aud_l = (project.get("audience") or "").lower()
    _haystack = f"{_topic_l} {_aud_l}"
    _edu_keys = ["pet", "dog", "cat", "mental", "health", "how much", "whether",
                 "anxiety", "depression", "wellbeing", "oxytocin", "pet ownership"]
    _is_edu = any(k in _haystack for k in _edu_keys)

    target_dur = int(project.get("target_duration", 300))
    target_words = max(120, int(target_dur / 60 * 150))

    if _is_edu:
        # Never let a local Ollama model hallucinate the empire/noodles story.
        os.environ["OLLAMA_MODEL"] = ""
        logger.info(f"EDUCATIONAL topic detected: {project.get('topic')} -> using educational_script.txt")
        tmpl = _load_prompt_template("educational_script.txt")
        if tmpl:
            user = _render_educational_prompt(project, tmpl, target_words)
            result = await _llm_json(SCRIPT_SYSTEM, user, f"script-{project.get('id','edu')}")
            if result:
                coerced = _coerce_script(result)
                if coerced and coerced.get("full_script"):
                    return _finalize_script(coerced)
            logger.warning("Educational template LLM failed - using on-topic deterministic fallback")
        # Educational topics never fall through to the generic business prompt:
        # straight to the on-topic deterministic fallback below.
    else:
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
Write specifically about the Topic above. Do NOT fall back to a generic entrepreneur/startup origin story (instant noodles, sleeping in the office, investors saying no, first 100 customers) unless the Topic explicitly asks for one.
Do not include BEAT markers, section headers, or structural labels (such as "BEAT 1", "INTRO:", "HOOK:", "CTA:", "SCENE 1:", etc.) in the output. All text must read as continuous voiceover."""

        result = await _llm_json(SCRIPT_SYSTEM, user, f"script-{project.get('id', uuid.uuid4())}")
        if result:
            coerced = _coerce_script(result)
            if coerced and coerced.get("full_script", "").strip():
                return _finalize_script(coerced)

    # Deterministic fallback - on-topic skeleton, never a canned unrelated story.
    topic = (project.get("topic") or project.get("title") or "this subject").strip().rstrip("?")
    if _is_edu:
        return _finalize_script({
            "hook_option_one": f"What the research really says about {topic}",
            "hook_option_two": f"The science behind {topic} most people miss",
            "hook_option_three": f"How {topic} actually affects us - the evidence",
            "selected_hook": f"What the research really says about {topic}",
            "full_script": (
                f"Today we are looking at what the research actually says about {topic}. "
                "This is not a story about a startup or a founder - it is about evidence. "
                "Studies consistently show that the way we live and the creatures we share "
                "our lives with shape our mental and physical health in measurable ways. "
                "Different pet types - dogs, cats, fish, birds - have different effects, "
                "and the mechanisms behind them, from oxytocin release to routine and "
                "social connection, are now well documented. "
                "The key insight is that small daily changes compound over time. "
                "Data beats guesswork, and the research gives us a clear picture of what "
                "actually helps. "
                "So here is the takeaway: understand the science, apply the evidence, "
                "and build habits that genuinely support your wellbeing."
            ),
            "retention_beats": [
                "What the studies show",
                "Dog vs cat vs fish - the differences",
                "The oxytocin and mental-health mechanism",
                "Practical takeaways you can use today",
            ],
            "cta_block": "If this was useful, subscribe. New evidence-based videos every week.",
        })

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
    """Normalize scene objects to the expected schema. All text fields are
    scrubbed of BEAT markers and structural labels before reaching the renderer."""
    out = []
    for s in scenes:
        if not isinstance(s, dict):
            continue
        raw_narration = s.get("narration_text") or s.get("caption_text") or ""
        raw_caption = s.get("caption_text") or s.get("narration_text") or ""
        narration = strip_beat_markers(str(raw_narration))
        caption = strip_beat_markers(str(raw_caption))
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
            "caption_text": truncate_words(caption, 80),
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

Do not include BEAT markers, section headers, or structural labels (such as "BEAT 1", "INTRO:", "HOOK:", "SCENE 1:", etc.) in narration_text or caption_text. Text must read as continuous voiceover.

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

Write specifically about this Topic. Do NOT use a generic entrepreneur/startup origin-story framing (billion dollar empire, instant noodles, investors said no) unless the Topic explicitly asks for it.
Do not include BEAT markers, section headers, or structural labels in any output text."""

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
