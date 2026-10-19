"""Deployment visibility limit shared by every YouTube uploader."""
import os


def resolve_youtube_privacy(requested: str | None = None) -> str:
    """Honor stricter requests, but never exceed YOUTUBE_PRIVACY (default private)."""
    visibility = ("private", "unlisted", "public")
    policy = os.environ.get("YOUTUBE_PRIVACY", "private").strip().lower()
    if policy not in visibility:
        policy = "private"
    if requested is None:
        return policy
    requested = requested.strip().lower()
    if requested not in visibility:
        return "private"
    return visibility[min(visibility.index(policy), visibility.index(requested))]
