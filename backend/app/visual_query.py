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
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger("facelessforge.visual_query")

# Same stopword set as video_engine.py, lightly expanded for our domain.
_STOP_WORDS = {
    "the", "and", "a", "an", "of", "to", "is", "in", "we", "you", "your", "our",
    "for", "on", "this", "that", "with", "by", "it", "are", "do", "does", "get",
    "now", "or", "as", "be", "have", "has", "had", "but", "if", "so", "not",
    "from", "at", "was", "were", "they", "them", "their", "what", "who", "how",
    "why", "when", "where", "can", "will", "just", "all", "any", "some", "more",
    "most", "than", "then", "into", "out", "also", "only", "even", "very",
}


def extract_visual_keywords(text: str, *, top_n: int = 3) -> list[str]:
    """Return up to ``top_n`` deduplicated high-signal visual keywords.

    Ported from video_engine.py — robust fallback when LLM unavailable.
    Drops stopwords + words shorter than 4 chars, preserves insertion order.
    """
    if not text:
        return []
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if len(w) < 4 or w in _STOP_WORDS:
            continue
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= top_n:
            break
    return out


def build_scene_query(
    scene: dict,
    *,
    visual_tone: Optional[str] = None,
    fallback_text_fields: tuple[str, ...] = ("narration_text", "caption_text", "visual_direction"),
) -> str:
    """Compose the Pexels query for one scene.

    Priority order for the base query:
      1. ``scene.search_terms``  (LLM-derived during script generation; best)
      2. Top-3 keywords from narration_text / caption_text / visual_direction

    The project-wide ``visual_tone`` modifier is then appended so every
    scene pulls from the same visual world.
    """
    base = ""
    search_terms = scene.get("search_terms")
    if isinstance(search_terms, list):
        base = " ".join(str(x) for x in search_terms if x).strip()
    elif isinstance(search_terms, str):
        base = search_terms.strip()
    if not base:
        for field in fallback_text_fields:
            text = scene.get(field) or ""
            kws = extract_visual_keywords(str(text))
            if kws:
                base = " ".join(kws)
                break
    if not base:
        base = "abstract motion"  # last-resort generic
    if visual_tone and visual_tone.strip():
        return f"{base} {visual_tone.strip()}".strip()
    return base


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
