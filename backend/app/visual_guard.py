VISUAL_RULES = {
    "finance": {
        "allowed": ["stock chart", "bank vault", "compound interest graph", "wall street"],
        "banned": ["beach", "beach hut", "pink house", "hand bangles", "peeling orange", "food", "pottery"],
    },
    "business": {
        "allowed": ["startup office san francisco", "【entity-airbnb¦canonical_name=airbnb】 founders", "【entity-y combinator¦canonical_name=y combinator】 whiteboard", "valuation chart crash", "empty hotel covid", "【entity-airbnb¦canonical_name=airbnb】 app", "global map pins"],
        "banned": ["beach hut", "bangles", "peeling orange", "pottery painting"],
    }
}

def allocate_scenes(total_seconds):
    if total_seconds <= 60:
        return [
            {"time": "0-12s", "beat": "HOOK", "search": "san francisco apartment air mattress startup 2007"},
            {"time": "12-24s", "beat": "DECISION", "search": "【entity-y combinator¦canonical_name=y combinator】 whiteboard founders meeting"},
            {"time": "24-42s", "beat": "CRASH", "search": "travel industry stock crash chart empty hotel covid"},
            {"time": "42-60s", "beat": "EMPIRE", "search": "【entity-airbnb¦canonical_name=airbnb】 app global map pins worldwide"},
        ]
    else:
        return [
            {"time": "0-45s", "beat": "HOOK", "search": "san francisco apartment air mattress 2007 laptop"},
            {"time": "45-150s", "beat": "ORIGINS", "search": "【entity-airbnb¦canonical_name=airbnb】 founders early days struggle"},
            {"time": "150-270s", "beat": "YC DECISIONS", "search": "【entity-y combinator¦canonical_name=y combinator】 cereal boxes whiteboard"},
            {"time": "270-360s", "beat": "GROWTH", "search": "startup growth chart regulatory battle"},
            {"time": "360-420s", "beat": "CRASH", "search": "travel stock crash empty streets hotel closed 2020"},
            {"time": "420-480s", "beat": "EMPIRE", "search": "【entity-airbnb¦canonical_name=airbnb】 app experiences world map global empire"},
        ]

def get_search_terms(niche, scene_text, scene_beat):
    rules = VISUAL_RULES.get(niche.lower(), VISUAL_RULES["business"])
    return f"{niche} {scene_beat} {scene_text} allowed:{rules['allowed']} banned:{rules['banned']}"

def render_title_safe(text, w, h):
    return {"text": text.upper(), "max_width": w*0.8, "no_pillarbox": True}
