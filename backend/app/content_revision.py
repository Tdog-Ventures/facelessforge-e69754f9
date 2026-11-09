"""One published revision for a script and the drafts derived from it.

Script text, scenes, and generated assets live in different collections.
A publish therefore cannot be one database transaction. While a change is in
progress the project is marked inconsistent, and render plus asset attachment
refuse to read the pair. The mark is cleared only after the new script is
stored and the old drafts are gone.
"""
from __future__ import annotations

from typing import Optional


class ContentRevisionError(RuntimeError):
    """Script text and derived drafts are not one published revision."""


def revisions_match(project: Optional[dict], script: Optional[dict]) -> bool:
    if not project or project.get("content_consistent") is False:
        return False
    revision = int(project.get("content_revision") or 0)
    derivatives = project.get("derivatives_revision")
    derivative_revision = revision if derivatives is None else int(derivatives)
    if script is None or "content_revision" not in script:
        script_revision = 0 if revision == 0 else -1
    else:
        script_revision = int(script.get("content_revision") or 0)
    return script_revision == revision and derivative_revision == revision


def require_consistent_content(project: Optional[dict], script: Optional[dict]) -> None:
    if not revisions_match(project, script):
        raise ContentRevisionError(
            "Script and derived scenes are not one revision. "
            "Retry after the script change finishes."
        )


def scenes_match_revision(project: Optional[dict], scenes: Optional[list]) -> bool:
    """Scenes stamped by a planner must belong to the published script revision.

    Older scenes have no stamp. Those stay valid until a script change deletes them.
    """
    if not project:
        return False
    revision = int(project.get("content_revision") or 0)
    for scene in scenes or []:
        if "content_revision" in scene and int(scene.get("content_revision") or 0) != revision:
            return False
    return True
