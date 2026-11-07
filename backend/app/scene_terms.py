"""Constitution fix v2.7: preserve full Visual, cinematic 3-query ordered sources.

Fixes:
- Preserves full Visual field including comma-separated detail (no split on comma, no [:40] truncation)
- Appends "4k slow motion b-roll cinematic" for pexels_video query (same as JS fix)
- Generates exactly 3 ordered query objects per scene: pexels_video (cinematic full), pixabay_video (shorter core 3-4 words), unsplash_image (shortest)
- Stops leaking script words like "might", "obsolete", "coming", "year", "small", "businesses", "drowning", "imagine", "software", "shift", "machine", "impact", "simulate" as single-word queries
- Video preferred first order
"""
import re

# Expanded stopwords to block abstract/modal/temporal leaks and single-word script dregs
_STOP = set(
    "the and a an of to in on for with about is are was were be as it its this that what you about to hear story will cover how it converts alter will might could should would may can will shall must has have had do does did been being was were are is be as it its this that what when where why how which who whom whose will would could should may might must shall can cannot about hear story cover how converts alter future past today tomorrow coming year years small businesses drowning imagine software shift machine impact simulate about hear story cover converts alter".split()
)
# Ensure leak words explicitly present
_STOP.update({"might", "obsolete", "coming", "year", "years", "small", "businesses", "drowning", "imagine", "software", "shift", "machine", "impact", "simulate", "obsolete", "2026", "might", "could", "would", "should", "will", "may", "shall"})

CINEMATIC_SUFFIX = "4k slow motion b-roll cinematic"

# These describe how to film a shot, not what a stock library should find.
_STOCK_SEARCH_NOISE = set("""
a an the of in on at into with for from and to as is are was were being
cinematic cinematic-style b-roll footage stock video videos photo photos image
images shot shots scene scenes camera angle zoom pan panning dolly tracking
closeup close-up wide ultra high resolution 4k 8k hd slow motion dramatic
beautiful stunning epic detailed realistic photorealistic professional showing
featuring capturing depicts depicting visual visuals illustration
""".split())


def compact_stock_query(value: str, max_words: int = 5) -> str:
    """Short footage-search keywords; keep detailed visual_direction separately.

    Search APIs match visible subjects/actions, not a cinematic prompt.
    Preserve word order and named entities (e.g. One Day International cricket).
    """
    raw = str(value or "").split(".")[0].split(";")[0]
    words = re.findall(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*", raw)
    meaningful = []
    for word in words:
        if word.lower() in _STOCK_SEARCH_NOISE or word.lower() in _STOP:
            continue
        if word.lower() in {"matching", "narration", "lens", "lighting", "composition"}:
            continue
        meaningful.append(word)
        if len(meaningful) >= max_words:
            break
    return " ".join(meaningful)


def _clean_visual(visual: str) -> str:
    """Preserve full Visual including commas, normalize whitespace only. No truncation."""
    if not visual:
        return ""
    # Preserve commas, just collapse whitespace
    return " ".join((visual or "").strip().split())

def _extract_core_keywords(visual: str, search_terms: list | None = None, max_words: int = 4) -> str:
    """Extract 3-4 high-signal core words for shorter fallback queries.
    Priority: meaningful nouns from visual_direction + search_terms, filtered for leaks.
    Returns e.g. 'crystal ball forecast' or 'robotic hand typing keyboard'
    """
    # Prefer search_terms that already contain multi-word phrases (LLM-derived, e.g. "crystal ball future business")
    if search_terms:
        # Find first term that contains 2+ words and not a leak single word
        for t in search_terms:
            clean = " ".join(str(t or "").split()).strip()
            # Skip single-word leaks
            if not clean:
                continue
            words = clean.split()
            if len(words) == 1 and words[0].lower() in _STOP:
                continue
            if len(words) == 1 and len(words[0]) < 4:
                continue
            # If term is multi-word, use its first 3-4 words as core
            if len(words) >= 2:
                # Filter leak words inside
                filtered = [w for w in words if w.lower() not in _STOP and len(w) >= 3]
                if len(filtered) >= 2:
                    return " ".join(filtered[:max_words])
                # Fallback still use original multi-word
                return " ".join(words[:max_words])
    # Fallback: extract from visual field directly
    t = (visual or "").lower()
    # Replace punctuation but keep words, split
    words = re.findall(r"[a-z]{3,}", t)
    # Filter stops and leaks
    kw = [w for w in words if w not in _STOP]
    # Also filter garbage length >20 etc.
    kw = [w for w in kw if 3 <= len(w) <= 20]
    # Deduplicate preserving order
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
    # Last resort
    if out:
        return " ".join(out)
    return "nature cinematic"

def derive_search_terms(scene_text: str, idx: int, total: int) -> list[str]:
    """Generate 3 query strings per scene for backward compat, preserving full Visual.
    Returns 3 terms: [full_cinematic, core_shorter, core_shortest] – never single-word leaks.
    The caller (generation.py fallback) expects list[str]; we honour ordered 3.
    """
    cleaned = _clean_visual(scene_text)
    if not cleaned:
        base_core = "nature cinematic"
        return [
            f"{base_core} {CINEMATIC_SUFFIX}",
            "nature cinematic",
            "nature",
        ]
    # Detect if scene_text looks like a Visual sentence (contains spaces, commas) vs already a core term
    # Preserve full visual + cinematic for first term
    full_cinematic = f"{cleaned} {CINEMATIC_SUFFIX}".strip()
    # Extract core for shorter queries
    core = _extract_core_keywords(cleaned, None, max_words=4)
    # Ensure core is not truncated to single word
    core_words = core.split()
    if len(core_words) < 2:
        core = cleaned.split()[:4]
        core = " ".join([w for w in core if w.lower() not in _STOP][:3]) or "nature cinematic"
    # Build shorter pixabay (core + maybe b-roll) and unsplash (core)
    # Keep 3 distinct
    second = f"{core}" if core else "nature cinematic"
    # Ensure second not single word
    if len(second.split()) < 2:
        second = f"{second} b-roll".strip()
    third = " ".join(core.split()[:3]).strip() or "nature"
    if len(third.split()) < 2:
        third = "nature cinematic"
    # Special topic overrides still respected for legacy brain etc. but use full visual base
    t = (scene_text or "").lower()
    if any(k in t for k in ["mushroom","psilocybin","psilocin","mycelium","fungi"]):
        return [f"{core} forest floor {CINEMATIC_SUFFIX}".strip(), "mushrooms moss macro", "fungi forest floor"]
    if any(k in t for k in ["brain","neuron","consciousness","perception","synapse"]):
        return ["neurons brain firing 4k slow motion b-roll cinematic", "neural network abstract", "brain synapse"]
    if any(k in t for k in ["therapy","setting","psychological","mindfulness"]):
        return ["calm therapy nature 4k slow motion b-roll cinematic", "mindfulness forest", "person journaling"]
    if any(k in t for k in ["toxic","identification","safety","lab","poison"]):
        return ["scientist lab research 4k slow motion b-roll cinematic", "microscope laboratory", "mushroom field guide"]
    return [full_cinematic, second, third]

def derive_visual_query(scene_text: str) -> str:
    """Return full Visual + cinematic suffix, preserving commas and detail. No truncation."""
    cleaned = _clean_visual(scene_text)
    if not cleaned:
        return f"nature cinematic {CINEMATIC_SUFFIX}"
    # Ensure we don't leak single-word script dregs: if cleaned is single word leak, fallback
    if cleaned.lower() in _STOP or len(cleaned.split()) == 1:
        return f"nature cinematic {CINEMATIC_SUFFIX}"
    return f"{cleaned} {CINEMATIC_SUFFIX}".strip()

def build_scene_queries(scene: dict, project: dict | None = None) -> list[dict]:
    """Generate exactly 3 ordered query objects per scene:
       0: pexels_video (full Visual + cinematic suffix) - preferred first
       1: pixabay_video (shorter core 3-4 words)
       2: unsplash_image (shortest core 2-3 words)
    Preference order video first. Preserves full Visual including commas.
    """
    visual = (scene.get("visual_direction") or scene.get("visual") or "").strip()
    search_terms = scene.get("search_terms") or []
    if not visual:
        # Use search_terms or project topic as visual fallback
        if search_terms and isinstance(search_terms, list):
            # Join first multi-word term if exists
            for t in search_terms:
                if t and len(str(t).split()) >= 2:
                    visual = str(t)
                    break
            if not visual:
                visual = str(search_terms[0])
        elif isinstance(search_terms, str):
            visual = search_terms
        else:
            visual = (project.get("topic") if project and project.get("topic") else "") or "nature cinematic"
    cleaned = _clean_visual(visual)
    if not cleaned:
        cleaned = "nature cinematic"
    # Full cinematic for pexels
    pexels_q = f"{cleaned} {CINEMATIC_SUFFIX}".strip()
    # Validate no truncation: ensure robotic hand example preserved fully
    # Core for shorter queries
    core = _extract_core_keywords(cleaned, search_terms, max_words=4)
    # Pixabay shorter: core (3-4 words)
    pixabay_q = core if core else "nature cinematic"
    # Ensure pixabay not single word
    if len(pixabay_q.split()) < 2:
        pixabay_q = "nature cinematic b-roll"
    # Unsplash shortest: first 3 words of core
    unsplash_q = " ".join(core.split()[:3]).strip() if core else "nature cinematic"
    if len(unsplash_q.split()) < 2:
        unsplash_q = "nature cinematic"
    # Ensure no single-word leaks like "small", "imagine", "might", etc.
    for q in [pexels_q, pixabay_q, unsplash_q]:
        # If any query is a single leak word, replace with fallback
        if q.strip().lower() in _STOP or len(q.strip().split()) == 1:
            # fallback
            if q == pixabay_q:
                pixabay_q = "nature cinematic b-roll"
            elif q == unsplash_q:
                unsplash_q = "nature cinematic"
    return [
        {"source": "pexels", "type": "video", "media_type": "videos", "query": pexels_q},
        {"source": "pixabay", "type": "video", "media_type": "videos", "query": pixabay_q},
        {"source": "unsplash", "type": "image", "media_type": "photos", "query": unsplash_q},
    ]
