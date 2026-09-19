#!/usr/bin/env python3
"""Validate the completed production configuration before an ETHINX host installation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from bank_adapter import adapter_from_config
from sovereign_db import DomainError


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: validate_config.py /path/to/config.json", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    config = json.loads(path.read_text(encoding="utf-8"))
    required = ("business", "bank_transfer", "bank_import", "billing", "privacy", "security", "plans", "content")
    missing = [key for key in required if key not in config]
    if missing:
        raise DomainError(f"Missing required configuration sections: {', '.join(missing)}.")
    bank = config["bank_transfer"]
    for field in ("account_name", "bsb", "account_number", "payment_reference_prefix"):
        if not isinstance(bank.get(field), str) or not bank[field].strip() or "000" in bank[field] and field in {"bsb", "account_number"}:
            raise DomainError(f"bank_transfer.{field} must be a completed value.")
    privacy = config["privacy"]
    for field in ("application_retention_days", "bank_record_retention_days"):
        value = privacy.get(field)
        if not isinstance(value, int) or value < 1:
            raise DomainError(f"privacy.{field} must be an approved positive integer; do not deploy a placeholder.")
    security = config["security"]
    if security.get("require_https") is not True:
        raise DomainError("security.require_https must be true for a production installation.")
    origins = security.get("trusted_origins")
    if not isinstance(origins, list) or not origins or any(not isinstance(item, str) or not item.startswith("https://") for item in origins):
        raise DomainError("security.trusted_origins must contain one or more HTTPS origins.")
    adapter_from_config(config)
    for code, plan in config["plans"].items():
        if not isinstance(plan.get("amount_cents"), int) or plan["amount_cents"] <= 0:
            raise DomainError(f"plans.{code}.amount_cents must be a positive integer.")
        if not isinstance(plan.get("seats"), int) or plan["seats"] < 1:
            raise DomainError(f"plans.{code}.seats must be a positive integer.")
    print(json.dumps({"status": "valid", "config": str(path)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, json.JSONDecodeError, DomainError) as exc:
        print(json.dumps({"status": "invalid", "reason": str(exc)}))
        raise SystemExit(2)
