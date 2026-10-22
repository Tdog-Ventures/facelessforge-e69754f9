"""Ground external packaging in caller-supplied content; never call an LLM."""
from __future__ import annotations

def prepare_packaging(title: str, script: str, scenes: list[dict], stock_terms: list[str], niche: str | None = None) -> tuple[dict, list[dict]]:
    title = title.strip()
    script = script.strip()
    if not title or len(script) < 10:
        raise ValueError("External packaging requires a title and script")
    terms = list(dict.fromkeys(t.strip() for t in stock_terms if isinstance(t, str) and t.strip()))[:12]
    if not terms and niche and niche.strip():
        terms = [niche.strip()]
    visual = next((str(s.get("visual_direction") or "").strip() for s in scenes if s.get("visual_direction")), "")
    if not visual:
        visual = ", ".join(terms) or title
    metadata = {
        "title_options": [title], "selected_title": title,
        "description": script, "tags": terms, "hashtags": [],
        "chapters": [], "youtube_chapters": [], "pinned_comment": "",
        "social_caption": title, "provenance": "caller_supplied",
    }
    brief = {
        "thumbnail_title_text": title, "visual_composition": visual,
        "emotion_angle": "", "background_idea": visual,
        "subject_focal_point": ", ".join(terms) or visual,
        "colour_direction": "", "click_trigger": "",
        "image_prompt": visual, "provenance": "caller_supplied",
    }
    return metadata, [brief]
