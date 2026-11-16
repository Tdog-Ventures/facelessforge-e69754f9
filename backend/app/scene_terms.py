"""Constitution fix v2.7: preserve full Visual, cinematic 3-query ordered sources.

Fixes:
- Preserves full Visual field including comma-separated detail (no split on comma, no [:40] truncation)
- Appends "4k slow motion b-roll cinematic" for pexels_video query (same as JS fix)
- Generates exactly 3 ordered query objects per scene: pexels_video (cinematic full), pixabay_video (shorter core 3-4 words), unsplash_image (shortest)
- Stops leaking script words like "might", "obsolete", "coming", "year", "small", "businesses", "drowning", "imagine", "software", "shift", "machine", "impact", "simulate" as single-word queries
- Video preferred first order
"""
import math
import re

# One judged frame-batch per search term before the next term gets a turn.
PER_TERM_JUDGE_BUDGET = 8
# The scene judge cap is this many frames for every clip the floor requires.
JUDGE_FRAMES_PER_REQUIRED_CLIP = 6

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


def plan_hold_durations(scene_duration: float, count: int) -> list[float]:
    """Split a scene across the clips we actually have.

    Fewer clips than the 3-second ideal get longer holds. The holds sum to
    the scene duration. One clip is held for the whole scene.
    """
    if count <= 0:
        return []
    duration = max(0.1, float(scene_duration or 0))
    if count == 1:
        return [round(duration, 3)]
    step = round(duration / count, 3)
    holds = [step] * (count - 1)
    holds.append(round(duration - sum(holds), 3))
    return holds


def holds_cover(scene_duration: float, clips: list) -> bool:
    if not clips:
        return False
    duration = float(scene_duration or 0)
    if duration <= 0:
        return True
    covered = 0.0
    for clip in clips:
        try:
            covered += float(clip.get("duration") or 0)
        except (TypeError, ValueError):
            continue
    return covered + 0.05 >= duration


def min_distinct_clips(duration: float) -> int:
    """Hard floor of distinct clips for a scene.

    The search still targets about one shot every 3 seconds. This floor is
    one distinct clip per 4 seconds and is never below 3. A 28-second scene
    therefore needs 8 different clips. Reused, borrowed, and Ken Burns
    repeats fill time. They never count toward this number.
    """
    try:
        seconds = float(duration or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if not math.isfinite(seconds) or seconds <= 0:
        return 3
    return max(3, math.ceil(seconds / 4))


def clips_to_find(duration: float) -> int:
    """How many distinct clips to look for: one about every 3 seconds, at least the floor."""
    try:
        seconds = float(duration or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if not math.isfinite(seconds) or seconds <= 0:
        return min_distinct_clips(0)
    return max(min_distinct_clips(seconds), math.ceil(seconds / 3))


def scene_judge_cap(duration: float, *, configured_cap: int = 0) -> int:
    """Judge budget for one scene. It grows with the distinct-clip floor."""
    scaled = min_distinct_clips(duration) * JUDGE_FRAMES_PER_REQUIRED_CLIP
    try:
        configured = int(configured_cap or 0)
    except (TypeError, ValueError):
        configured = 0
    return max(scaled, configured, 12)


def footage_shortfall_message(scene_number, have: int, required: int) -> str:
    """The sentence render preflight and render/start show for a short scene."""
    label = scene_number if scene_number not in (None, "") else "?"
    return (
        f"Scene {label} has {have} of {required} required distinct clips "
        f"- run Find footage for scene {label}."
    )


_FETCHABLE_FIELDS = ("download_url", "preview_url", "source_url", "file_path", "storage_key")
_OWN_VISUAL_TYPES = {"stock_image", "stock_video"}


def clip_is_downloadable(asset: dict) -> bool:
    """A scene's own stock clip that can actually be fetched. Mocks never count."""
    if not isinstance(asset, dict):
        return False
    if asset.get("status") == "rejected" or asset.get("mock"):
        return False
    if str(asset.get("source") or "").lower() in {"mock", "mock_tts"}:
        return False
    kind = str(asset.get("asset_type") or asset.get("media_type") or "")
    if kind not in _OWN_VISUAL_TYPES:
        return False
    return any(str(asset.get(key) or "").strip() for key in _FETCHABLE_FIELDS)


def distinct_downloadable_clips(clips, *, scene_id: str | None = None) -> int:
    """Count distinct own clips. Repeats of the same provider id count once.

    Pass ``scene_id`` to ignore borrowed clips that belong to another scene.
    """
    seen: set[str] = set()
    for clip in clips or []:
        if not isinstance(clip, dict) or not clip_is_downloadable(clip):
            continue
        if scene_id is not None and str(clip.get("scene_id") or "") != str(scene_id):
            continue
        identity = str(
            clip.get("external_id")
            or clip.get("download_url")
            or clip.get("preview_url")
            or clip.get("file_path")
            or clip.get("storage_key")
            or clip.get("id")
            or ""
        ).strip()
        if not identity or identity in seen:
            continue
        seen.add(identity)
    return len(seen)


def stock_query_tiers(scene: dict, project: dict, primary: str = "") -> list[list[str]]:
    """Escalation tiers, each one round-robin'd before the next starts.

    1. The scene's own search terms (and the primary / visual-direction query).
    2. Nouns taken from the narration.
    3. The project topic.
    4. Generic niche b-roll. Cooking topics end on kitchen footage.
    """
    scene_terms: list[str] = []
    narration_terms: list[str] = []
    topic_terms: list[str] = []
    generic_terms: list[str] = []
    seen: set[str] = set()

    def add(bucket: list[str], value, max_words: int = 5) -> None:
        term = compact_stock_query(value, max_words=max_words)
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            bucket.append(term)

    add(scene_terms, primary)
    terms = scene.get("search_terms") or []
    if isinstance(terms, str):
        terms = [terms]
    for term in terms:
        add(scene_terms, term)
    add(scene_terms, scene.get("visual_direction") or "")
    narration = str(scene.get("narration_text") or "")
    add(narration_terms, narration, 4)
    add(narration_terms, narration, 2)
    topic = str((project or {}).get("topic") or "")
    add(topic_terms, topic, 4)
    add(topic_terms, topic, 2)
    niche = str((project or {}).get("niche") or "")
    add(topic_terms, niche, 3)
    blob = f"{topic} {niche} {narration} {scene.get('visual_direction') or ''}".lower()
    if re.search(r"\b(eggs?|cook\w*|kitchen|recipes?|food|fr(?:y|ied|ying)|boil\w*)\b", blob):
        add(generic_terms, "eggs cooking")
        add(generic_terms, "kitchen cooking")
    elif niche:
        add(generic_terms, niche, 2)
    else:
        add(generic_terms, "everyday life")
    return [tier for tier in (scene_terms, narration_terms, topic_terms, generic_terms) if tier]


def broaden_stock_queries(scene: dict, project: dict, primary: str = "") -> list[str]:
    """Specific scene query first, then shorter nouns, the topic, then a generic.

    Cooking topics end on kitchen footage. Other topics end on the niche,
    then a broad everyday query. Repeated phrases are dropped. This is the
    flat form of ``stock_query_tiers``.
    """
    queries: list[str] = []
    for tier in stock_query_tiers(scene, project, primary):
        queries.extend(tier)
    return queries


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
