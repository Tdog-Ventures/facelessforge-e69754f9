"""Fill a scene's sub-clips by reusing its own on-topic footage.

A scene often has fewer accepted clips than the sub-clip plan has cuts
(auto-attach may commit 1-3 of 9). Searching stock again at render time
skips the relevance judge and can pull off-topic filler. Instead:

1. Each of the scene's own sources may be used up to ``MAX_USES_PER_SOURCE``
   times. Every reuse gets a different trim (video) and a different slow
   zoom/pan, so the boundary is still a visible cut.
2. If that is not enough, adjacent scenes' own sources may be borrowed, at
   most once each (``BORROWED_MAX_USES``), and only when the scene has at
   least one source of its own.
3. Only when even that cannot fill the plan does the caller search stock.

Same-source segments are never placed back to back when another source is
available.
"""
from __future__ import annotations

import math
from typing import Sequence

MAX_USES_PER_SOURCE = 3
BORROWED_MAX_USES = 1

# Motion for a repeated video segment (first use keeps the plain treatment).
VIDEO_REUSE_MOTIONS = ("pan_right", "pan_left", "push_in", "pan_down", "pan_up")


def reuse_capacity(own_count: int, borrowed_count: int = 0) -> int:
    """Sub-clips that ``own_count`` own sources plus borrowed sources can fill."""
    own = max(0, int(own_count))
    borrowed = max(0, int(borrowed_count)) if own else 0
    return own * MAX_USES_PER_SOURCE + borrowed * BORROWED_MAX_USES


def own_sources_needed(slots: int) -> int:
    """Distinct own sources needed to fill ``slots`` by reuse alone."""
    return max(1, math.ceil(max(1, int(slots)) / MAX_USES_PER_SOURCE))


def borrow_order(scene_index: int, sources_by_scene: Sequence[Sequence], ) -> list[tuple[int, int]]:
    """(scene, source) pairs to borrow, nearest neighbours first, alternating
    previous/next scene so no single neighbour dominates."""
    prev_i, next_i = scene_index - 1, scene_index + 1
    prev = list(range(len(sources_by_scene[prev_i]))) if prev_i >= 0 else []
    nxt = list(range(len(sources_by_scene[next_i]))) if next_i < len(sources_by_scene) else []
    order: list[tuple[int, int]] = []
    for k in range(max(len(prev), len(nxt))):
        if k < len(prev):
            order.append((prev_i, prev[-1 - k]))  # end of previous scene first
        if k < len(nxt):
            order.append((next_i, nxt[k]))        # start of next scene first
    return order


def schedule_reuse(own_count: int, borrowed_count: int, slots: int) -> list[tuple[int, int]]:
    """Return one ``(source_index, pass_number)`` per sub-clip.

    Sources ``0..own_count-1`` are the scene's own, then the borrowed ones.
    Own sources are filled first (evenly, up to the cap); borrowed sources only
    cover what remains, once each. If even that is short, own sources are
    reused beyond the cap rather than inventing footage. The order avoids
    the same source in consecutive slots whenever the counts allow it.
    ``pass_number`` counts earlier uses of that source (0 = first use).
    """
    slots = max(0, int(slots))
    own = max(0, int(own_count))
    borrowed = max(0, int(borrowed_count)) if own else 0
    total = own + borrowed
    if slots == 0 or total == 0:
        return []
    quota = [0] * total
    remaining = slots
    # Own sources: round robin up to the cap.
    for _ in range(MAX_USES_PER_SOURCE):
        for s in range(own):
            if remaining and quota[s] < MAX_USES_PER_SOURCE:
                quota[s] += 1
                remaining -= 1
    # Borrowed sources: once each, only for the shortfall.
    for s in range(own, total):
        if remaining and quota[s] < BORROWED_MAX_USES:
            quota[s] += 1
            remaining -= 1
    # Still short: stretch own sources beyond the cap (never off-topic stock).
    s = 0
    while remaining:
        quota[s % max(1, own)] += 1
        remaining -= 1
        s += 1

    order: list[tuple[int, int]] = []
    uses = [0] * total
    left = list(quota)
    prev = None
    for _ in range(slots):
        candidates = [i for i in range(total) if left[i] > 0 and i != prev]
        if not candidates:
            candidates = [i for i in range(total) if left[i] > 0]
        # Most remaining first keeps a valid no-repeat arrangement possible;
        # ties prefer own sources (lower index).
        pick = max(candidates, key=lambda i: (left[i], -i))
        order.append((pick, uses[pick]))
        uses[pick] += 1
        left[pick] -= 1
        prev = pick
    return order


def ken_burns_direction(directions: Sequence[str], scene_index: int, source_index: int,
                        pass_number: int) -> str:
    """Different direction for each use of the same still."""
    return directions[(scene_index + source_index + pass_number) % len(directions)]


def video_reuse_motion(pass_number: int, source_index: int = 0) -> str | None:
    """Slow zoom/pan for a repeated video segment; None for its first use."""
    if pass_number <= 0:
        return None
    return VIDEO_REUSE_MOTIONS[(pass_number - 1 + source_index) % len(VIDEO_REUSE_MOTIONS)]


def reuse_offset(pass_number: int, slot_seconds: float, source_seconds: float | None) -> float:
    """Trim point for a repeated video use: spread passes across the source."""
    if not source_seconds or source_seconds <= slot_seconds:
        return 0.0
    span = source_seconds - slot_seconds
    if pass_number <= 0:
        return 0.0
    # Golden-ratio stepping keeps successive in-points far apart.
    return round((pass_number * 0.618033988749895 * span) % span, 2)


def scene_can_fill_by_reuse(own_count: int, neighbour_counts: Sequence[int], slots: int) -> bool:
    """Pre-render check mirror of the render path: own reuse, then neighbours."""
    if own_count <= 0:
        return False
    return reuse_capacity(own_count, sum(max(0, int(c)) for c in neighbour_counts)) >= slots
