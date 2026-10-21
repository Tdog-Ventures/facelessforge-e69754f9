"""Fenced, Mongo-backed leases that serialize project-level writers.

FacelessForge runs several API and worker processes, so an in-process
asyncio.Lock cannot serialize writers that share a project. The lease state
lives on the project document itself: acquisition and protected project writes
are single-document compare-and-set operations. Cross-collection asset writes
use the non-expiring commit barrier in asset_commit.py. No multi-document
transaction is required, and none is assumed.

Fields stored on the project document for each lease prefix:

    <prefix>_holder   token of the current holder, or None when free
    <prefix>_expires  UTC expiry of the current holder's lease
    <prefix>_fence    monotonically increasing fencing counter

Acquisition bumps the fence with a CAS on its previous value. Protected project
writes and asset-barrier entry filter on (holder token, fence). Once a lease has
expired and been taken over, that stale filter matches nothing. Asset takeover
is additionally refused while the current holder has a commit barrier pinned.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("facelessforge.lease")

# Serializes project-mode/commercial updates against render submission and
# render release (see render.queue_render, render._run_render, update_project).
RENDER_LEASE = "render_lease"
# Serializes stock-asset selection writers: auto-attach replace/top-up and
# manual scene attachment.
ASSET_LEASE = "asset_lease"

RENDER_LEASE_TTL_SECONDS = 90
ASSET_LEASE_TTL_SECONDS = 120

# Project fields that the render prerequisite checks and the commercial release
# gates read. Writes to any of them must be serialized with render submission.
RENDER_INPUT_FIELDS = (
    "production_mode",
    "target_duration",
    "client_name",
    "structured_brief",
    "verified_cta",
    "brand_assets_supplied",
)

_LEASE_FIELD_PREFIXES = ("render_lease_", "asset_lease_")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Lease:
    project_id: str
    prefix: str
    token: str
    fence: int

    def guard(self) -> dict:
        """Filter fragment that matches only while this lease is still held."""
        return {f"{self.prefix}_holder": self.token, f"{self.prefix}_fence": self.fence}


async def acquire(db, project_id: str, prefix: str, *, ttl_seconds: int) -> Optional[Lease]:
    """Take the lease if it is free or expired. Returns None when busy or missing."""
    fence_field = f"{prefix}_fence"
    current = await db.projects.find_one({"id": project_id}, {"_id": 0, fence_field: 1})
    if current is None:
        return None
    if fence_field in current:
        previous = int(current.get(fence_field) or 0)
        fence_filter = {fence_field: previous}
    else:
        previous = 0
        fence_filter = {fence_field: {"$exists": False}}
    now = _now()
    token = uuid.uuid4().hex
    query = {
            "id": project_id,
            **fence_filter,
            "$or": [{f"{prefix}_holder": None}, {f"{prefix}_expires": {"$lt": now}}],
        }
    if prefix == ASSET_LEASE:
        # A commit barrier is deliberately NOT a lease. Expiry cannot transfer
        # ownership while cross-document asset/account writes may be in flight.
        query["asset_commit_barrier"] = None
    result = await db.projects.update_one(
        query,
        {"$set": {
            f"{prefix}_holder": token,
            f"{prefix}_expires": now + timedelta(seconds=ttl_seconds),
            fence_field: previous + 1,
        }},
    )
    if getattr(result, "matched_count", 0) != 1:
        return None
    return Lease(project_id=project_id, prefix=prefix, token=token, fence=previous + 1)


async def acquire_with_retry(
    db, project_id: str, prefix: str, *, ttl_seconds: int, attempts: int = 40, delay: float = 0.25,
) -> Optional[Lease]:
    """Bounded wait for a short-lived holder. Returns None if still busy."""
    for attempt in range(max(1, attempts)):
        lease = await acquire(db, project_id, prefix, ttl_seconds=ttl_seconds)
        if lease is not None:
            return lease
        if attempt + 1 < attempts:
            await asyncio.sleep(delay)
    return None


async def renew(db, lease: Lease, *, ttl_seconds: int) -> bool:
    """Extend a held lease. False means it was lost and the caller must abort."""
    result = await db.projects.update_one(
        {"id": lease.project_id, **lease.guard()},
        {"$set": {f"{lease.prefix}_expires": _now() + timedelta(seconds=ttl_seconds)}},
    )
    return getattr(result, "matched_count", 0) == 1


async def still_held(db, lease: Lease) -> bool:
    doc = await db.projects.find_one({"id": lease.project_id, **lease.guard()}, {"_id": 0, "id": 1})
    return doc is not None


async def release(db, lease: Optional[Lease]) -> None:
    """Release only if still held. Never raises: a lost lease is already fenced."""
    if lease is None:
        return
    try:
        query = {"id": lease.project_id, **lease.guard()}
        if lease.prefix == ASSET_LEASE:
            query["asset_commit_barrier"] = None
        await db.projects.update_one(
            query,
            {"$set": {f"{lease.prefix}_holder": None, f"{lease.prefix}_expires": None}},
        )
    except Exception:  # noqa: BLE001 - expiry releases it anyway
        logger.exception("lease release failed project=%s prefix=%s", lease.project_id, lease.prefix)


def strip_lease_fields(doc: Optional[dict]) -> Optional[dict]:
    """Remove internal lease state before a project document leaves the API."""
    if not doc:
        return doc
    return {k: v for k, v in doc.items() if not k.startswith((*_LEASE_FIELD_PREFIXES, "asset_commit_"))}


def render_input_snapshot(project: Optional[dict]) -> dict:
    """Capture the project fields a render submission was validated against."""
    project = project or {}
    return {
        "production_mode": project.get("production_mode") or "editorial",
        "target_duration": project.get("target_duration"),
        "client_name": project.get("client_name"),
        "structured_brief": project.get("structured_brief"),
        "verified_cta": project.get("verified_cta"),
        "brand_assets_supplied": bool(project.get("brand_assets_supplied")),
    }


def render_inputs_match(project: Optional[dict], snapshot: dict) -> bool:
    current = render_input_snapshot(project)
    return all(current.get(key) == value for key, value in snapshot.items())
