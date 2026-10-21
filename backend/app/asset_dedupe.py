"""Semantic duplicate guard for stock assets selected by auto-attach.

Provider ids are unique, but a single query can return several near-identical
frames (for example three "video editing timeline" photos). Exact-URL dedupe
misses those. This module compares provider titles with a small stop-word list
so one scene does not get filled with the same visual idea repeated.
"""
from __future__ import annotations

import re

STOPWORDS = frozenset(
    "a an and are as at be by close close-up detailed displayed for from "
    "highlighting in into is it of on or showing the to up used view with".split()
)
MIN_TITLE_TOKENS = 3
MIN_SHARED_TOKENS = 3
MIN_JACCARD = 0.25


def title_tokens(title: str) -> set[str]:
    """Return meaningful lowercase title tokens, dropping stock-photo filler."""
    words = re.findall(r"[a-z0-9]+", str(title or "").lower())
    return {word for word in words if len(word) > 2 and word not in STOPWORDS}


def titles_are_near_duplicates(first: str, second: str) -> bool:
    """True when two provider titles describe the same visual idea.

    Thresholds are intentionally conservative: three shared subject words and
    a quarter of the combined vocabulary. The real incident was three
    "video editing software / timeline / interface" images in one 30s scene.
    """
    left = title_tokens(first)
    right = title_tokens(second)
    if len(left) < MIN_TITLE_TOKENS or len(right) < MIN_TITLE_TOKENS:
        return False
    shared = left & right
    if len(shared) < MIN_SHARED_TOKENS:
        return False
    return len(shared) / len(left | right) >= MIN_JACCARD


def filter_distinct_titles(
    candidates: list[dict],
    existing_titles: list[str],
) -> tuple[list[dict], list[dict]]:
    """Split candidates into (kept, near-duplicates) using provider titles.

    Kept candidates also become fingerprints for the rest of this batch, so a
    single search cannot attach two near-identical results.
    """
    kept: list[dict] = []
    rejected: list[dict] = []
    fingerprints = [str(title or "") for title in existing_titles if str(title or "").strip()]
    for candidate in candidates:
        title = str(candidate.get("title") or candidate.get("name") or "").strip()
        if title and any(titles_are_near_duplicates(title, prior) for prior in fingerprints):
            rejected.append(candidate)
            continue
        kept.append(candidate)
        if title:
            fingerprints.append(title)
    return kept, rejected
