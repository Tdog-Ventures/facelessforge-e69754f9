"""Commercial-ad grounding and release gates.

This module is deliberately deterministic.  It does not try to infer a client
identity from a prompt or trust a provider label: the project record, assets,
and scene assignments are the source of truth for a releasable client ad.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


# A generic office clip is not harmless when it is used to advertise a local
# pressure-washing business.  These terms are release blockers for commercial
# cleaning ads unless the client explicitly opts into them in a future policy.
FORBIDDEN_COMMERCIAL_VISUAL_CATEGORIES = {
    "medical": ("medical", "hospital", "doctor", "blood pressure", "stethoscope", "patient", "surgery"),
    "finance": ("finance", "financial", "stock market", "trading", "chart", "crypto", "banking"),
    "hacking": ("hacker", "hacking", "cyber", "cybersecurity", "terminal", "code screen"),
    "coding": ("coding", "programming", "developer", "software engineer", "laptop code"),
    "office": ("office", "meeting room", "corporate", "coworking", "business team"),
    "fashion": ("fashion", "runway", "model posing", "clothing store", "catwalk"),
    "unrelated_cleaning": ("car wash", "laundry", "dishwashing", "housekeeping", "janitor office"),
}

CLIENT_VISUAL_TYPES = {"client_image", "client_video", "before_after", "brand_image"}
BRAND_TYPES = {"brand_logo", "brand_image", "brand_font", "brand_colour"}
VISUAL_TYPES = CLIENT_VISUAL_TYPES | {"stock_image", "stock_video"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


def script_duration_check(script: dict | None, requested_duration: int | float | None) -> dict[str, Any]:
    """Return a conservative narration-duration contract.

    Ads are paced at up to 2.8 spoken words/second.  The small tolerance is
    intentional: a 15-second request accepts at most 42 narration words, not
    a 147-word long-form script.
    """
    target = int(requested_duration or 0)
    words = word_count((script or {}).get("full_script") or "")
    maximum = max(1, int(target * 2.8))
    minimum = max(1, int(target * 1.4))
    return {
        "requested_duration_seconds": target,
        "word_count": words,
        "minimum_words": minimum,
        "maximum_words": maximum,
        "ok": bool(target and minimum <= words <= maximum),
        "reason": None if target and minimum <= words <= maximum else (
            f"Script has {words} words; a {target}s commercial ad requires {minimum}–{maximum} words."
        ),
    }


def semantic_rejections(asset: dict, scene: dict | None = None) -> list[str]:
    """Find known unrelated-footage categories from immutable asset metadata.

    This is a baseline guard, not an image-classifier claim.  Uploaded client
    footage is allowed because its provenance is explicit; stock candidates
    must not carry any of the known wrong-domain labels in their provider
    metadata, URL, title, query, tags, or assigned visual direction.
    """
    if asset.get("asset_type") in CLIENT_VISUAL_TYPES:
        return []
    values = [
        asset.get("name"), asset.get("title"), asset.get("query"),
        asset.get("source_url"), asset.get("download_url"), asset.get("preview_url"),
        " ".join(str(x) for x in (asset.get("tags") or [])),
        (scene or {}).get("visual_direction"),
        " ".join(str(x) for x in ((scene or {}).get("search_terms") or [])),
    ]
    haystack = " ".join(str(v or "") for v in values).lower()
    return [category for category, terms in FORBIDDEN_COMMERCIAL_VISUAL_CATEGORIES.items()
            if any(term in haystack for term in terms)]


def asset_provenance(asset: dict) -> dict[str, Any]:
    """A serialisable provenance entry required in a release report."""
    return {
        "asset_id": asset.get("id"),
        "name": asset.get("name"),
        "asset_type": asset.get("asset_type"),
        "scene_id": asset.get("scene_id"),
        "provenance": asset.get("provenance") or asset.get("source"),
        "source_url": asset.get("source_url"),
        "sha256": asset.get("sha256"),
        "uploaded_at": asset.get("uploaded_at"),
        "rejections": semantic_rejections(asset),
    }


def build_asset_manifest(assets: list[dict]) -> list[dict]:
    return [asset_provenance(asset) for asset in assets if asset.get("status") != "rejected"]


def commercial_project_errors(project: dict | None) -> list[str]:
    """Return missing required fields for a commercial project.

    Editorial projects are not client ads and must not be blocked by client
    brief fields.  The frontend exposes this as a commercial/non-commercial
    choice; the API enforces the same rule so a crafted request cannot create
    an ungrounded ad.
    """
    project = project or {}
    if project.get("production_mode") != "commercial_ad":
        return []
    errors: list[str] = []
    if not str(project.get("client_name") or "").strip():
        errors.append("Client business name")
    brief = project.get("structured_brief") or {}
    for key, label in (
        ("service", "Advertised service"),
        ("offer", "Offer"),
        ("audience", "Client audience"),
        ("location", "Location"),
    ):
        if not str(brief.get(key) or "").strip():
            errors.append(label)
    cta = project.get("verified_cta") or {}
    if not (cta.get("verified") and str(cta.get("text") or "").strip()
            and str(cta.get("destination") or "").strip()):
        errors.append("Verified CTA")
    return errors


def validate_commercial_grounding(
    project: dict | None,
    script: dict | None,
    scenes: list[dict],
    assets: list[dict],
) -> dict[str, Any]:
    """Fail-closed release validation for a commercial ad project."""
    project = project or {}
    active_assets = [a for a in assets if a.get("status") not in {"rejected", "deleted"}]
    client_visuals = [a for a in active_assets if a.get("asset_type") in CLIENT_VISUAL_TYPES]
    brand_assets = [a for a in active_assets if a.get("asset_type") in BRAND_TYPES]
    supplied_brand = bool(project.get("brand_assets_supplied"))
    cta = project.get("verified_cta") or {}
    structured = project.get("structured_brief") or {}
    target = project.get("target_duration")
    aspect = project.get("aspect_ratio")
    resolution = project.get("resolution")
    duration = script_duration_check(script, target)
    assigned: dict[str, list[dict]] = {
        str(scene.get("id")): [a for a in active_assets if a.get("scene_id") == scene.get("id") and a.get("asset_type") in VISUAL_TYPES]
        for scene in scenes
    }
    unassigned = [scene.get("scene_number") for scene in scenes if not assigned.get(str(scene.get("id")))]
    non_client_assignments = [
        scene.get("scene_number") for scene in scenes
        if assigned.get(str(scene.get("id"))) and not any(a.get("asset_type") in CLIENT_VISUAL_TYPES for a in assigned[str(scene.get("id"))])
    ]
    semantic = []
    for scene in scenes:
        for asset in assigned.get(str(scene.get("id")), []):
            for category in semantic_rejections(asset, scene):
                semantic.append({"scene_number": scene.get("scene_number"), "asset_id": asset.get("id"), "category": category})
    requirements = [
        ("client_name", "Named client", bool(str(project.get("client_name") or "").strip())),
        ("structured_brief", "Structured client brief", all(bool(str(structured.get(k) or "").strip()) for k in ("service", "offer", "audience", "location"))),
        ("verified_cta", "Verified CTA details", bool(cta.get("verified") and cta.get("text") and cta.get("destination"))),
        ("requested_duration", "Requested duration", bool(target and int(target) > 0)),
        ("format", "Aspect ratio and resolution", bool(aspect and resolution)),
        ("asset_manifest", "Asset manifest with provenance", bool(active_assets) and all(bool(a.get("provenance") or a.get("source")) for a in active_assets)),
        ("client_assets", "Genuine client visual assets", bool(client_visuals)),
        ("brand_assets", "Brand assets supplied are attached", bool(brand_assets) if supplied_brand else True),
        ("scene_assignments", "Every scene has an assigned visual", bool(scenes) and not unassigned),
        ("client_scene_assignments", "Every scene uses a client visual", bool(scenes) and not non_client_assignments),
        ("script_duration", "Script fits requested duration", duration["ok"]),
        ("semantic_alignment", "No known unrelated footage categories", not semantic),
    ]
    checklist = [{"key": key, "label": label, "ok": ok} for key, label, ok in requirements]
    issues = [label for key, label, ok in requirements if not ok]
    if unassigned:
        issues.append(f"Unassigned scenes: {', '.join(str(x) for x in unassigned)}")
    if non_client_assignments:
        issues.append(f"Scenes without client visuals: {', '.join(str(x) for x in non_client_assignments)}")
    if semantic:
        issues.append("Rejected semantic categories: " + ", ".join(sorted({x["category"] for x in semantic})))
    return {
        "ok": not issues,
        "checklist": checklist,
        "issues": issues,
        "script_duration": duration,
        "asset_manifest": build_asset_manifest(active_assets),
        "scene_assignments": [{"scene_id": s.get("id"), "scene_number": s.get("scene_number"), "asset_ids": [a.get("id") for a in assigned.get(str(s.get("id")), [])]} for s in scenes],
        "semantic_rejections": semantic,
    }
