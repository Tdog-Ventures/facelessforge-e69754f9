"""Inventory CLI probes run only against an explicitly disposable Mongo."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(not os.environ.get("DODO_TEST_MONGO_URL"), reason="requires disposable Mongo URL")
@pytest.mark.parametrize("database", ["/inventory_cli_probe", ""])
def test_readonly_inventory_cli_accepts_named_and_default_database(database):
    url = os.environ["DODO_TEST_MONGO_URL"].rstrip("/") + database
    script = Path(__file__).resolve().parents[2] / "scripts" / "inventory_dodo_duplicate_accounts.py"
    result = subprocess.run([sys.executable, str(script), "--mongo-url", url],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["summary"]["ff_credits_duplicate_groups"] == 0
    assert report["summary"]["subscriptions_duplicate_groups"] == 0
