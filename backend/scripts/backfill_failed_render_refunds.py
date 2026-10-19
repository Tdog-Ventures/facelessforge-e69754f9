#!/usr/bin/env python3
"""One-off backfill: reverse render credits charged for failed renders.

Window: 2026-10-14T00:00Z .. 2026-10-18T00:00Z (jobs whose terminal failure
update landed in that window and that carry a matching -3 charge ledger row).

Safety:
- DRY-RUN by default: prints the plan, performs ZERO writes.
- --apply performs idempotent reversals (deterministic refund:<job_id> ledger id,
  same key as the live reverse_render_credits() patch).
- Only reverses when a matching charge ledger row is found (reason
  "render <project_id>", amount -3, same user, timestamped around job creation).
  If no charge row exists, nothing is reversed (no money is invented).
- Completed/cancelled jobs are never candidates.
- Known-good accepted jobs are guarded explicitly.

Usage:
  MONGO_URL=... python3 backfill_failed_render_refunds.py            # dry-run
  MONGO_URL=... python3 backfill_failed_render_refunds.py --apply    # live
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

WINDOW_START = datetime(2026, 10, 14, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 10, 18, tzinfo=timezone.utc)
FAILED_STATUSES = ["failed", "FAILED", "error", "ERROR"]
CHARGE_AMOUNT = -3
REFUND_AMOUNT = 3
PROTECTED_JOB_IDS = {
    "17253296-aaaa-bbbb-cccc-000000000001",  # placeholder prefix guard; real ids matched by prefix below
}
PROTECTED_JOB_PREFIXES = ("17253296", "3cd769ec")


def _is_protected(job_id: str) -> bool:
    return str(job_id).startswith(PROTECTED_JOB_PREFIXES)


async def find_candidates(db, since: datetime, until: datetime, limit: int = 2000):
    """Failed jobs in the window, excluding protected known-good ids."""
    cursor = db.render_jobs.find(
        {"status": {"$in": FAILED_STATUSES}, "updated_at": {"$gte": since, "$lt": until}},
        {"_id": 0, "id": 1, "project_id": 1, "requested_by": 1, "created_at": 1,
         "updated_at": 1, "credit_charge": 1, "credit_reversed_at": 1, "status": 1},
    )
    jobs = await cursor.to_list(limit)
    return [j for j in jobs if not _is_protected(j.get("id", ""))]


async def find_matching_charge(db, job):
    """The original -3 ledger row for this job's render, if any."""
    user_id = (job.get("credit_charge") or {}).get("user_id") or job.get("requested_by")
    project_id = job.get("project_id") or ""
    if not user_id or not project_id:
        return None
    created = job.get("created_at")
    query = {
        "user_id": user_id,
        "amount": CHARGE_AMOUNT,
        "reason": f"render {project_id}",
    }
    if isinstance(created, datetime):
        query["created_at"] = {"$gte": created - timedelta(minutes=10)}
    candidates = await db.credit_transactions.find(query, {"_id": 0}).to_list(50)
    for row in candidates:
        if row.get("refunded_by_job_id"):
            continue  # already consumed by a previous backfill pass
        if row.get("job_id") and row["job_id"] != job.get("id"):
            continue  # charge explicitly linked to a different job
        return row
    return None


async def choose_store(db, user_id: str):
    """Mirror the live deduct preference: subscriptions first, then ff_credits."""
    sub = await db.subscriptions.find_one({"user_id": user_id}, {"_id": 0, "credits_remaining": 1})
    if sub is not None:
        return "subscriptions"
    ff = await db.ff_credits.find_one({"tenant_id": user_id}, {"_id": 0, "credits": 1})
    if ff is not None:
        return "ff_credits"
    return None


async def plan_refunds(db, since: datetime, until: datetime, limit: int = 2000):
    plan = []
    for job in await find_candidates(db, since, until, limit):
        job_id = job.get("id")
        if job.get("credit_reversed_at"):
            plan.append({"job_id": job_id, "action": "skip", "why": "already_reversed"})
            continue
        if await db.credit_transactions.find_one({"refund_id": f"refund:{job_id}"}, {"_id": 0, "id": 1}):
            plan.append({"job_id": job_id, "action": "skip", "why": "refund_ledger_exists"})
            continue
        charge = await find_matching_charge(db, job)
        if not charge:
            plan.append({"job_id": job_id, "action": "skip", "why": "no_matching_charge"})
            continue
        user_id = charge.get("user_id") or job.get("requested_by")
        store = await choose_store(db, user_id)
        if not store:
            plan.append({"job_id": job_id, "action": "skip", "why": "unknown_store", "user_id": user_id})
            continue
        plan.append({
            "job_id": job_id,
            "project_id": job.get("project_id"),
            "user_id": user_id,
            "amount": REFUND_AMOUNT,
            "store": store,
            "charge_created_at": charge.get("created_at"),
            "action": "refund",
        })
    return plan


async def apply_refund(db, entry: dict) -> dict:
    """Idempotent reversal for one backfill entry. Safe to re-run."""
    job_id = entry["job_id"]
    refund_id = f"refund:{job_id}"
    if await db.credit_transactions.find_one({"refund_id": refund_id}, {"_id": 0, "id": 1}):
        return {"job_id": job_id, "reversed": False, "why": "already_reversed"}
    now = datetime.now(timezone.utc)
    try:
        await db.credit_transactions.insert_one({
            "id": str(uuid.uuid4()),
            "refund_id": refund_id,
            "job_id": job_id,
            "user_id": entry["user_id"],
            "amount": entry["amount"],
            "reason": f"refund failed render {entry.get('project_id')}: backfill 2026-10-14..17"[:200],
            "created_at": now,
        })
    except Exception as e:  # noqa: BLE001 — duplicate key = already reversed concurrently
        if "duplicate" in str(e).lower() or getattr(e, "code", None) == 11000:
            return {"job_id": job_id, "reversed": False, "why": "already_reversed"}
        raise
    if entry["store"] == "subscriptions":
        await db.subscriptions.update_one(
            {"user_id": entry["user_id"]},
            {"$inc": {"credits_remaining": entry["amount"]}, "$set": {"updated_at": now}},
        )
    else:
        await db.ff_credits.update_one(
            {"tenant_id": entry["user_id"]},
            {"$inc": {"credits": entry["amount"]}, "$set": {"updated_at": now}},
        )
    await db.render_jobs.update_one(
        {"id": job_id},
        {"$set": {"credit_reversed_at": now, "credit_reversed_refund_id": refund_id}},
    )
    # Consume the matched charge row so re-runs cannot match it again.
    await db.credit_transactions.update_one(
        {"user_id": entry["user_id"], "amount": CHARGE_AMOUNT,
         "reason": f"render {entry.get('project_id')}", "refunded_by_job_id": None},
        {"$set": {"refunded_by_job_id": job_id}},
    )
    return {"job_id": job_id, "reversed": True, "store": entry["store"], "amount": entry["amount"]}


async def run(mongo_uri: str, db_name: str, since: datetime, until: datetime,
              apply: bool, limit: int) -> int:
    import motor.motor_asyncio

    client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
    db = client[db_name]
    plan = await plan_refunds(db, since, until, limit)

    refunds = [p for p in plan if p["action"] == "refund"]
    skips = [p for p in plan if p["action"] == "skip"]
    print(f"BACKFILL PLAN window={since.isoformat()}..{until.isoformat()} apply={apply}")
    print(f"candidates: {len(plan)}  refund: {len(refunds)}  skip: {len(skips)}")
    for entry in refunds:
        print(f"  REFUND job={entry['job_id']} project={entry.get('project_id')} "
              f"user={entry['user_id']} amount=+{entry['amount']} store={entry['store']}")
    from collections import Counter
    for why, n in Counter(s["why"] for s in skips).items():
        print(f"  SKIP({why}): {n}")

    if not apply:
        print("DRY-RUN: no writes performed. Re-run with --apply to execute.")
        return 0

    ok = failed = 0
    for entry in refunds:
        try:
            result = await apply_refund(db, entry)
            print(f"  APPLIED job={entry['job_id']} -> {result}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR job={entry['job_id']}: {e}", file=sys.stderr)
            failed += 1
    print(f"APPLIED: {ok}  ERRORS: {failed}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo", default=os.environ.get("MONGO_URL"),
                        help="MongoDB URI (default: $MONGO_URL)")
    parser.add_argument("--db", default=os.environ.get("DB_NAME", "facelessforge"))
    parser.add_argument("--since", default=WINDOW_START.isoformat())
    parser.add_argument("--until", default=WINDOW_END.isoformat())
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--apply", action="store_true",
                        help="Actually write reversals (default is dry-run)")
    args = parser.parse_args()
    if not args.mongo:
        print("error: provide --mongo or set MONGO_URL", file=sys.stderr)
        return 2
    import asyncio
    since = datetime.fromisoformat(args.since)
    until = datetime.fromisoformat(args.until)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return asyncio.run(run(args.mongo, args.db, since, until, args.apply, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
