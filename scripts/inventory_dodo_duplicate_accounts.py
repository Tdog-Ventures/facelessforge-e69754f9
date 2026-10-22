#!/usr/bin/env python3
"""Read-only inventory of legacy duplicate Dodo credit accounts.

This script scans ff_credits and subscriptions for duplicate string identities
that would prevent the unique partial indexes in app.billing_dodo from being
created. It never writes, updates, deletes, or creates indexes.

Run with a read-only Mongo connection string, e.g.:

    python3 scripts/inventory_dodo_duplicate_accounts.py \
        --mongo-url 'mongodb://readonly:pwd@host/facelessforge'

Exit codes:
    0 = scan completed (duplicates may have been printed)
    1 = connection or query error
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any


def _serialize(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


async def scan_duplicates(db) -> dict:
    result: dict = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "ff_credits": {"duplicate_tenant_ids": []},
        "subscriptions": {"duplicate_user_ids": []},
    }

    pipeline_ff = [
        {"$match": {"tenant_id": {"$type": "string"}}},
        {"$group": {"_id": "$tenant_id", "count": {"$sum": 1}, "docs": {"$push": "$$ROOT"}}},
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}},
    ]
    async for group in db.ff_credits.aggregate(pipeline_ff):
        docs = []
        for doc in group["docs"]:
            docs.append({
                "_id": str(doc.get("_id")),
                "tenant_id": doc.get("tenant_id"),
                "user_id": doc.get("user_id"),
                "credits": doc.get("credits"),
                "plan": doc.get("plan"),
                "email": doc.get("email"),
                "transaction_id": doc.get("transaction_id"),
                "last_transaction_id": doc.get("last_transaction_id"),
                "created_at": _serialize(doc.get("created_at")),
                "updated_at": _serialize(doc.get("updated_at")),
            })
        result["ff_credits"]["duplicate_tenant_ids"].append({
            "tenant_id": group["_id"],
            "count": group["count"],
            "docs": docs,
        })

    pipeline_sub = [
        {"$match": {"user_id": {"$type": "string"}}},
        {"$group": {"_id": "$user_id", "count": {"$sum": 1}, "docs": {"$push": "$$ROOT"}}},
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}},
    ]
    async for group in db.subscriptions.aggregate(pipeline_sub):
        docs = []
        for doc in group["docs"]:
            docs.append({
                "_id": str(doc.get("_id")),
                "user_id": doc.get("user_id"),
                "credits_remaining": doc.get("credits_remaining"),
                "credits_monthly": doc.get("credits_monthly"),
                "plan": doc.get("plan"),
                "status": doc.get("status"),
                "current_period_end": _serialize(doc.get("current_period_end")),
                "created_at": _serialize(doc.get("created_at")),
                "updated_at": _serialize(doc.get("updated_at")),
            })
        result["subscriptions"]["duplicate_user_ids"].append({
            "user_id": group["_id"],
            "count": group["count"],
            "docs": docs,
        })

    result["summary"] = {
        "ff_credits_duplicate_groups": len(result["ff_credits"]["duplicate_tenant_ids"]),
        "ff_credits_duplicate_docs": sum(g["count"] for g in result["ff_credits"]["duplicate_tenant_ids"]),
        "subscriptions_duplicate_groups": len(result["subscriptions"]["duplicate_user_ids"]),
        "subscriptions_duplicate_docs": sum(g["count"] for g in result["subscriptions"]["duplicate_user_ids"]),
    }
    return result


async def main():
    parser = argparse.ArgumentParser(description="Read-only inventory of duplicate Dodo billing accounts")
    parser.add_argument("--mongo-url", required=True, help="MongoDB connection string (read-only recommended)")
    parser.add_argument("--out", default="-", help="Output file (default stdout)")
    args = parser.parse_args()

    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError as exc:
        print("motor is required: pip install motor", file=sys.stderr)
        sys.exit(1)

    client = AsyncIOMotorClient(args.mongo_url, serverSelectionTimeoutMS=10000)
    try:
        # Use the database name from the URI if present, otherwise facelessforge
        db_name = client.get_default_database().name if client.get_default_database() else "facelessforge"
        db = client[db_name]
        await client.admin.command("ping")
        result = await scan_duplicates(db)
    except Exception as exc:
        print(f"Scan failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()

    text = json.dumps(result, indent=2, default=str)
    if args.out == "-":
        print(text)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote inventory to {args.out}")

    if result["summary"]["ff_credits_duplicate_groups"] or result["summary"]["subscriptions_duplicate_groups"]:
        print(
            "\nWARNING: duplicate accounts were found. "
            "Deployment of the new unique indexes requires reconciliation first.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    asyncio.run(main())
