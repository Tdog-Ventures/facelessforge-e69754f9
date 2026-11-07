"""Conservative stock selection for unattended FacelessForge attachment.

Provider query/tags are *not* evidence of footage content: several providers echo
the search string even when the returned clip is completely unrelated.
A missing descriptive title/slug is uncertain, and must require manual review.
"""
from __future__ import annotations
import re
from urllib.parse import urlparse

FILLER = frozenset("""
a an and are as at be by close closeup clip clips drone footage for from
hd high image images in into is it of on or photo photos pexels pixabay
professional scene shot stock the to ultra video videos view with showing
cinematic b roll slow motion wide 4k 8k visual camera angle footage
""".split())


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(w) >= 3 and w not in FILLER}


def subject_terms(scene: dict, query: str) -> set[str]:
    """Primary scene-specific search terms, never broad project topic alone."""
    terms = scene.get("search_terms") or []
    if isinstance(terms, str):
        terms = [terms]
    candidates = [str(t) for t in terms if t] + [scene.get("visual_direction") or "", query]
    for candidate in candidates:
        words = _tokens(candidate)
        if words:
            return words
    return set()


def verified_metadata(candidate: dict) -> str:
    """Only independent provider descriptions and descriptive URL slugs count."""
    title = str(candidate.get("title") or candidate.get("name") or "").strip()
    if re.fullmatch(r"(?:pexels|pixabay|unsplash)\s+(?:video|photo|image)\s+\d+", title, re.I):
        title = ""
    url = str(candidate.get("source_url") or "")
    path = urlparse(url).path
    slug = " ".join(p for p in path.split("/") if p and not p.isdigit())
    # Do not use 'tags', 'query' or the image preview: usually echoed from request.
    return f"{title} {slug}"


def candidate_is_relevant(candidate: dict, scene: dict, query: str) -> bool:
    source = str(candidate.get("source") or "").strip().lower()
    if source == "mock" or bool(candidate.get("mock")):
        return False
    if not candidate.get("download_url") or not candidate.get("external_id"):
        return False
    subjects = subject_terms(scene, query)
    evidence = _tokens(verified_metadata(candidate))
    if not subjects or not evidence:
        return False
    # Strong subject evidence is required, not generic 'office' alternatives.
    return bool(subjects & evidence)


def relevant_candidates(candidates: list[dict], scene: dict, query: str) -> tuple[list[dict], list[dict]]:
    allowed, rejected = [], []
    for candidate in candidates:
        (allowed if candidate_is_relevant(candidate, scene, query) else rejected).append(candidate)
    return allowed, rejected
