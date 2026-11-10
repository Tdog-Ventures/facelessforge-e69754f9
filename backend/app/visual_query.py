"""Visual query helpers for stock footage selection.

Two layers:

1. **Deterministic keyword extractor** (ported from `video_engine.py`) —
   stopword-based, returns the top N visual keywords from a script segment.
   No external dependencies, always available, used as a fallback.

2. **LLM-driven visual tone derivation** — one Emergent-LLM call analyses
   the full project script and returns a short 3-5 word modifier
   (e.g. ``"cinematic moody slow-motion neon-lit"``) that gets appended to
   every per-scene Pexels query so all clips share a consistent visual
   world. Cached on the project row so it only runs once per script.

Constitution fix v2.7: Preserve full Visual field including commas,
append "4k slow motion b-roll cinematic", generate 3 ordered queries
[pexels_video, pixabay_video, unsplash_image] video preferred first.
Stop leaking script words like "might", "obsolete", "coming", "year".
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger("facelessforge.visual_query")

# Same stopword set as video_engine.py, expanded to block leak words
_STOP_WORDS = {
    "the", "and", "a", "an", "of", "to", "is", "in", "we", "you", "your", "our",
    "for", "on", "this", "that", "with", "by", "it", "are", "do", "does", "get",
    "now", "or", "as", "be", "have", "has", "had", "but", "if", "so", "not",
    "from", "at", "was", "were", "they", "them", "their", "what", "who", "how",
    "why", "when", "where", "can", "will", "just", "all", "any", "some", "more",
    "most", "than", "then", "into", "out", "also", "only", "even", "very",
    # Leak blocklist: abstract/modal/temporal/script dregs that must never be standalone queries
    "might", "obsolete", "coming", "year", "years", "will", "shall", "should", "could", "would", "may",
    "about", "hear", "story", "cover", "converts", "alter", "small", "businesses", "drowning",
    "imagine", "software", "shift", "machine", "impact", "simulate", "future", "past", "today", "tomorrow",
    "must", "shall", "may", "might", "could", "would", "should", "will", "been", "being",
}

CINEMATIC_SUFFIX = "4k slow motion b-roll cinematic"


def is_garbage_token(token: str) -> bool:
    """Reject junk tokens that must never reach a stock-video query.

    Catches LLM repeat artifacts like ``STARTUPSTARTUPSTARTU`` (one unit
    concatenated ≥2 times), non-word tokens, and absurd lengths.
    """
    t = (token or "").strip().lower()
    if len(t) < 3 or len(t) > 20:
        return True
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", t):
        return True
    # Repeated-concatenation artifact: the whole token is one unit repeated
    # (possibly truncated at the end), e.g. "startupstartupstartu".
    if len(t) >= 10:
        for k in range(3, len(t) // 2 + 1):
            unit = t[:k]
            if t == (unit * (len(t) // k + 1))[: len(t)]:
                return True
    return False


def truncate_words(text: str, max_len: int = 80) -> str:
    """Truncate at a word boundary so titles/captions never cut mid-word."""
    text = " ".join(str(text or "").split())
    if len(text) <= max_len:
        return text
    cut = text[: max_len + 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" .,;:!?-–—")


def extract_visual_keywords(text: str, *, top_n: int = 3) -> list[str]:
    """Return up to ``top_n`` deduplicated high-signal visual keywords.

    Ported from video_engine.py — robust fallback when LLM unavailable.
    Drops stopwords, words shorter than 4 chars, and garbage tokens
    (``is_garbage_token``); preserves insertion order.
    Filters leak words like might/obsolete/coming/year.
    """
    if not text:
        return []
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if len(w) < 4 or w in _STOP_WORDS or is_garbage_token(w):
            continue
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= top_n:
            break
    return out


def _clean_visual(visual: str) -> str:
    """Preserve full Visual including commas, normalize whitespace only."""
    if not visual:
        return ""
    return " ".join(str(visual or "").strip().split())

def _core_from_visual_or_terms(visual: str, search_terms: list | None, max_words: int = 4) -> str:
    """Extract 3-4 word core for shorter fallback queries.
    Prefers search_terms multi-word phrases, then visual keywords.
    Ensures not single-word leak.
    """
    if search_terms:
        for t in search_terms:
            clean = " ".join(str(t or "").split()).strip()
            if not clean:
                continue
            words = clean.split()
            if len(words) == 1 and words[0].lower() in _STOP_WORDS:
                continue
            if len(words) == 1 and len(words[0]) < 4:
                continue
            if len(words) >= 2:
                filtered = [w for w in words if w.lower() not in _STOP_WORDS and len(w) >= 3 and not is_garbage_token(w)]
                if len(filtered) >= 2:
                    return " ".join(filtered[:max_words])
                return " ".join(words[:max_words])
    # Fallback from visual
    words = re.findall(r"[a-z]{3,}", (visual or "").lower())
    kw = [w for w in words if w not in _STOP_WORDS and not is_garbage_token(w)]
    seen = set()
    out = []
    for w in kw:
        if w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= max_words:
            break
    if len(out) >= 2:
        return " ".join(out[:max_words])
    if out:
        return " ".join(out)
    return "nature cinematic"


def build_scene_query(
    scene: dict,
    *,
    visual_tone: Optional[str] = None,
    fallback_text_fields: tuple[str, ...] = ("visual_direction", "narration_text"),
) -> str:
    """Compose the Pexels query for one scene.

    Priority order (v2.7 fix):
      1. Full ``scene.visual_direction`` preserved verbatim + CINEMATIC_SUFFIX (no truncation, no comma split)
      2. Fallback to search_terms filtered (multi-word)
      3. Top-3 keywords from visual_direction / narration_text

    The project-wide ``visual_tone`` modifier is appended if provided, but
    cinematic suffix is already included for video queries. Returns full visual + cinematic.
    """
    # v2.7: Prefer full Visual field directly, preserve commas
    visual = _clean_visual(scene.get("visual_direction") or "")
    if visual:
        base = f"{visual} {CINEMATIC_SUFFIX}".strip()
        if visual_tone and visual_tone.strip():
            base = f"{base} {visual_tone.strip()}".strip()
        # Ensure no single-word leak edge case
        if len(base.split()) >= 4:
            return base
    # Fallback to search_terms
    search_terms = scene.get("search_terms")
    if isinstance(search_terms, list):
        raw = " ".join(str(x) for x in search_terms if x)
    elif isinstance(search_terms, str):
        raw = search_terms
    else:
        raw = ""
    if raw:
        words = [w for w in re.sub(r"[^\w\s\-]", " ", raw.lower()).split()
                 if w not in _STOP_WORDS and not is_garbage_token(w)]
        deduped = list(dict.fromkeys(words))
        base = " ".join(deduped[:6]).strip()
        if base and len(base.split()) >= 2:
            if visual_tone and visual_tone.strip():
                return f"{base} {CINEMATIC_SUFFIX} {visual_tone.strip()}".strip()
            return f"{base} {CINEMATIC_SUFFIX}".strip()
    # Fallback to keywords from fields — NEVER caption_text: captions like
    # "Stay tuned for the full breakdown" are for TTS/subtitles only and must
    # never reach a stock-video query (they produce 0 candidates + 429 loops).
    base = ""
    for field in fallback_text_fields:
        text = scene.get(field) or ""
        kws = extract_visual_keywords(str(text), top_n=4)
        if kws and len(kws) >= 2:
            base = " ".join(kws)
            break
    if not base:
        base = "abstract motion"
    if visual_tone and visual_tone.strip():
        return f"{base} {CINEMATIC_SUFFIX} {visual_tone.strip()}".strip()
    return f"{base} {CINEMATIC_SUFFIX}".strip()

def build_ordered_queries(scene: dict, project: dict | None = None, visual_tone: str | None = None) -> list[dict]:
    """Generate exactly 3 ordered query objects per scene:
       0: pexels_video (full Visual + cinematic) - preferred
       1: pixabay_video (shorter core 3-4 words)
       2: unsplash_image (shortest 2-3 words)
    Mirrors JS fix: preserves full Visual including comma detail.
    """
    visual = _clean_visual(scene.get("visual_direction") or scene.get("visual") or "")
    search_terms = scene.get("search_terms") or []
    if not visual:
        if search_terms and isinstance(search_terms, list):
            for t in search_terms:
                if t and len(str(t).split()) >= 2:
                    visual = str(t)
                    break
            if not visual:
                visual = str(search_terms[0]) if search_terms else ""
        elif isinstance(search_terms, str):
            visual = search_terms
        else:
            visual = (project.get("topic") if project and project.get("topic") else "") or "nature cinematic"
    cleaned = _clean_visual(visual) or "nature cinematic"
    # Full cinematic for pexels
    pexels_q = f"{cleaned} {CINEMATIC_SUFFIX}".strip()
    if visual_tone and visual_tone.strip():
        pexels_q = f"{pexels_q} {visual_tone.strip()}".strip()
    core = _core_from_visual_or_terms(cleaned, search_terms, max_words=4)
    pixabay_q = core if core else "nature cinematic"
    if len(pixabay_q.split()) < 2:
        pixabay_q = "nature cinematic b-roll"
    unsplash_q = " ".join(core.split()[:3]).strip() if core else "nature cinematic"
    if len(unsplash_q.split()) < 2:
        unsplash_q = "nature cinematic"
    return [
        {"source": "pexels", "type": "video", "media_type": "videos", "query": pexels_q},
        {"source": "pixabay", "type": "video", "media_type": "videos", "query": pixabay_q},
        {"source": "unsplash", "type": "image", "media_type": "photos", "query": unsplash_q},
    ]

# Alias for scene_terms compatibility
def build_scene_queries(scene: dict, project: dict | None = None) -> list[dict]:
    return build_ordered_queries(scene, project)


async def generate_broll_keywords(
    caption: str = "",
    title: str = "",
    narration: str = "",
    top_n: int = 5,
) -> list[str]:
    """Return visual b-roll keywords for stock-footage search.

    Captions and narration are NEVER used literally as a stock query — they are
    only context for the LLM, which returns a JSON array of concrete visual
    nouns (e.g. ``["red dvd envelope", "1990s office", "stack of dvds"]``).

    Provider order mirrors the rest of the stack: local Ollama first, then
    DeepSeek/Kimi, then Claude. On any failure it falls back to the
    deterministic ``extract_visual_keywords`` over the visual_direction /
    narration fields (captions excluded).
    """
    import httpx

    kws: list[str] = []
    if narration or title or caption:
        context_parts = []
        if title:
            context_parts.append(f"Video title: {title}")
        if narration:
            context_parts.append(f"Narration: {narration[:1200]}")
        if caption:
            context_parts.append(f"Caption: {caption[:200]}")
        prompt = (
            "I need stock b-roll search keywords for a faceless YouTube video.\n"
            + "\n".join(context_parts)
            + "\n\n"
            "Respond with ONLY a JSON array of exactly "
            f"{top_n} concrete, visual, stock-searchable noun phrases "
            '(no abstract words like "story", "success", "future"). '
            "Example: [\"red dvd envelope\", \"1990s office\", \"stack of dvds\", "
            '"mail sorting room", "streaming apps on tv"]'
        )

        # 1. Local Ollama only when configured. An empty OLLAMA_MODEL must not
        # spend a long timeout on a default model the host may not be running.
        ollama_model = os.environ.get("OLLAMA_MODEL", "").strip()
        ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate").strip() or "http://localhost:11434/api/generate"
        if not ollama_model:
            logger.info("generate_broll_keywords skipped Ollama; OLLAMA_MODEL is not set")
        else:
            logger.info("generate_broll_keywords Ollama model=%s url=%s connect_timeout=2s", ollama_model, ollama_url)
            try:
                timeout = httpx.Timeout(connect=2.0, read=20.0, write=10.0, pool=2.0)
                async with httpx.AsyncClient(timeout=timeout) as c:
                    resp = await c.post(
                        ollama_url,
                        json={"model": ollama_model, "prompt": prompt, "stream": False, "format": "json"},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        text = data.get("response") or data.get("message", {}).get("content", "")
                        kws = _parse_broll_json(text)
                        if kws:
                            logger.info("generate_broll_keywords ollama=%s url=%s -> %s", ollama_model, ollama_url, kws)
            except Exception as e:  # noqa: BLE001
                hint = ""
                if "127.0.0.1" in ollama_url or "localhost" in ollama_url.lower():
                    hint = " Docker bridge containers cannot use the host's 127.0.0.1; set OLLAMA_URL=http://host.docker.internal:11434/api/generate and --add-host=host.docker.internal:host-gateway."
                logger.warning("generate_broll_keywords ollama failed url=%s: %s%s", ollama_url, e, hint)

        # 2. Cloud backups
        if not kws:
            for provider, url, model in (
                ("DEEPSEEK_API_KEY", "https://api.deepseek.com/chat/completions", "deepseek-chat"),
                ("KIMI_API_KEY", "https://api.moonshot.cn/v1/chat/completions", os.environ.get("KIMI_MODEL", "kimi-k2-0711-preview")),
            ):
                key = os.environ.get(provider, "").strip()
                if not key:
                    continue
                try:
                    async with httpx.AsyncClient(timeout=30.0) as c:
                        resp = await c.post(
                            url,
                            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                            json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": 200},
                        )
                        if resp.status_code == 200:
                            kws = _parse_broll_json(resp.json()["choices"][0]["message"]["content"])
                            if kws:
                                logger.info("generate_broll_keywords %s -> %s", provider, kws)
                                break
                except Exception as e:  # noqa: BLE001
                    logger.warning("generate_broll_keywords %s failed: %s", provider, e)
            if not kws:
                anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
                if anthropic_key:
                    try:
                        model = os.environ.get("LLM_MODEL", "claude-sonnet-4-5-20250929").strip() or "claude-sonnet-4-5-20250929"
                        async with httpx.AsyncClient(timeout=30.0) as c:
                            resp = await c.post(
                                "https://api.anthropic.com/v1/messages",
                                headers={"x-api-key": anthropic_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                                json={"model": model, "system": "You output only JSON arrays of visual stock keywords.", "messages": [{"role": "user", "content": prompt}], "max_tokens": 200, "temperature": 0.3},
                            )
                            if resp.status_code == 200:
                                text = "".join(b.get("text", "") for b in resp.json().get("content", []))
                                kws = _parse_broll_json(text)
                                if kws:
                                    logger.info("generate_broll_keywords anthropic -> %s", kws)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("generate_broll_keywords anthropic failed: %s", e)

    if not kws:
        # Deterministic fallback — narration/directions only, NEVER caption-only
        # text (no "stay tuned full breakdown" style junk reaches a stock query).
        text_src = narration or title or ""
        if text_src:
            kws = extract_visual_keywords(text_src, top_n=top_n)
    return kws[:top_n]


def _parse_broll_json(text: str) -> list[str]:
    """Parse a JSON array of keyword strings from an LLM reply."""
    if not text:
        return []
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        for key in ("keywords", "broll_keywords", "queries", "result"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []
    out: list[str] = []
    _junk = {"story", "success", "future", "idea", "concept", "business", "team"}
    for item in data:
        s = re.sub(r"\s+", " ", str(item or "").strip())
        if not s and s in out:
            continue
        if any(w in _junk for w in s.lower().split()):
            continue
        if s not in out:
            out.append(s)
    return out


async def derive_visual_tone(full_script: str) -> str:
    """Return a 3-5 word visual-tone modifier for the project.

    Uses Emergent LLM key + emergentintegrations. On any failure, returns
    "" (caller treats absent tone as "no modifier" and Pexels gets the
    raw per-scene queries). Designed to be cheap (1 short LLM call, no
    streaming, low temperature).
    """
    text = (full_script or "").strip()
    if len(text) < 50:
        return ""
    api_key = os.environ.get("EMERGENT_LLM_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return ""
    # Trim to ~6k chars — visual-tone signal saturates long before that
    if len(text) > 6000:
        text = text[:3000] + "\n...\n" + text[-3000:]
    prompt = (
        "Read this YouTube voiceover script and respond with EXACTLY 3 to 5 "
        "lowercase words separated by spaces describing the visual aesthetic "
        "that should unify the stock footage for this video. Examples of valid "
        "responses: \"cinematic moody slow-motion\", \"clean corporate bright\", "
        "\"gritty urban handheld neon\". Respond with ONLY the words, no quotes, "
        "no punctuation, no explanation.\n\nScript:\n" + text
    )
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage  # type: ignore
        import uuid
        chat = (
            LlmChat(api_key=api_key, session_id=f"tone-{uuid.uuid4().hex[:8]}",
                    system_message="You output only short visual-aesthetic descriptors.")
            .with_model("openai", "gpt-4o-mini")
        )
        msg = UserMessage(text=prompt)
        result = await chat.send_message(msg)
        out = (result or "").strip().strip('"').strip("'").lower()
        # Sanitise — keep words/spaces only, cap at 5 words.
        out = re.sub(r"[^a-z0-9\-\s]", " ", out)
        words = [w for w in out.split() if 2 <= len(w) <= 20]
        if 2 <= len(words) <= 6:
            return " ".join(words[:5])
        logger.warning("visual tone LLM returned malformed output: %r", result)
    except Exception as e:  # noqa: BLE001
        logger.warning("visual tone derivation failed: %s", e)
    return ""
