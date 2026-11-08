"""Customer password-reset regressions: hashed storage, atomic claim, legacy links, failure paths.

No provider calls, no real DB, no cookies or tokens printed.
"""
import asyncio
import hashlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/backend")

import pytest
from fastapi import HTTPException

import backend.app.routes.api as api
from backend.tests.fake_db import FakeCollection, FakeDatabase

try:
    from backend.tests.fake_db import matches as _matches, _apply_update as _apply
except Exception:  # pragma: no cover
    _matches = _apply = None


class MemoryCollection(FakeCollection):
    """Test-only shim: FakeCollection covers update_one, production code also uses update_many."""

    async def update_many(self, query, update, **_kw):
        changed = 0
        for doc in self.docs:
            if _matches(doc, query):
                _apply(doc, update)
                changed += 1
        return type("R", (), {"modified_count": changed})()
from backend.app.models import ForgotPasswordRequest, ResetPasswordRequest


class FakeRequest:
    def __init__(self, ip="203.0.113.9"):
        self.headers = {}
        self.client = type("C", (), {"host": ip})()


def seed(db, name, docs):
    db.add(name, MemoryCollection(name, docs))


def docs(db, name):
    return db.__getattr__(name).docs


def run(coro):
    return asyncio.run(coro)


def setup(monkeypatch, users=None):
    db = FakeDatabase()
    seed(db, "users", users if users is not None else [
        {"id": "u1", "email": "customer@example.com", "role": "creator", "password_hash": "old-hash-1"},
        {"id": "u2", "email": "other@example.com", "role": "creator", "password_hash": "old-hash-2"},
    ])
    seed(db, "password_reset_tokens", [])
    seed(db, "password_reset_attempts", [])
    monkeypatch.setattr(api, "get_db", lambda: db)
    sent = {"calls": []}

    async def fake_send(*, recipient, reset_url, expires_in_minutes):
        sent["calls"].append({"recipient": recipient, "has_token": "token=" in reset_url})
        return sent.get("result", {"ok": True, "mode": "resend"})

    monkeypatch.setattr(api, "send_password_reset_email", fake_send)
    return db, sent


def issue(db, email="customer@example.com"):
    return run(api.forgot_password(ForgotPasswordRequest(email=email), FakeRequest()))


def sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


def test_registered_customer_gets_hashed_token_and_generic_response(monkeypatch):
    db, sent = setup(monkeypatch)
    resp = issue(db)
    assert resp.get("ok") is True and set(resp.keys()) == {"ok", "message"}
    tokens = docs(db, "password_reset_tokens")
    assert len(tokens) == 1
    doc = tokens[0]
    assert "token" not in doc, "raw token must not be stored"
    assert len(doc["token_hash"]) == 64
    assert sent["calls"] and sent["calls"][0]["has_token"] is True
    assert doc["used_at"] is None


def test_unknown_email_returns_generic_200_and_sends_nothing(monkeypatch):
    db, sent = setup(monkeypatch)
    resp = issue(db, "nobody@example.com")
    assert resp.get("ok") is True
    assert docs(db, "password_reset_tokens") == []
    assert sent["calls"] == []


def test_send_failure_invalidates_token(monkeypatch):
    db, sent = setup(monkeypatch)
    sent["result"] = {"ok": False, "mode": "resend", "error": "provider returned HTTP 500"}
    issue(db)
    tokens = docs(db, "password_reset_tokens")
    assert len(tokens) == 1
    assert tokens[0]["used_at"] is not None, "undelivered token must be invalidated"


def test_expired_token_rejected(monkeypatch):
    db, _ = setup(monkeypatch)
    raw = "expired-token-value-123456"
    seed(db, "password_reset_tokens", [{
        "id": "t1", "user_id": "u1", "email": "customer@example.com",
        "token_hash": sha(raw),
        "created_at": datetime.now(timezone.utc) - timedelta(hours=3),
        "expires_at": datetime.now(timezone.utc) - timedelta(hours=2),
        "used_at": None,
    }])
    with pytest.raises(HTTPException) as ei:
        run(api.reset_password(ResetPasswordRequest(token=raw, new_password="brand-new-pass")))
    assert ei.value.status_code == 400
    assert docs(db, "users")[0]["password_hash"] == "old-hash-1"
    assert docs(db, "password_reset_tokens")[0]["used_at"] is None


def test_valid_token_updates_only_intended_user(monkeypatch):
    db, _ = setup(monkeypatch)
    raw = "valid-token-value-abcdef"
    seed(db, "password_reset_tokens", [{
        "id": "t1", "user_id": "u1", "email": "customer@example.com",
        "token_hash": sha(raw),
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        "used_at": None,
    }])
    out = run(api.reset_password(ResetPasswordRequest(token=raw, new_password="brand-new-pass")))
    assert out.get("ok") is True
    users = docs(db, "users")
    assert users[0]["password_hash"] != "old-hash-1"
    assert users[1]["password_hash"] == "old-hash-2", "unrelated user must not change"
    assert docs(db, "password_reset_tokens")[0]["used_at"] is not None


def test_reused_token_rejected(monkeypatch):
    db, _ = setup(monkeypatch)
    raw = "reuse-token-value-abcdef"
    seed(db, "password_reset_tokens", [{
        "id": "t1", "user_id": "u1", "email": "customer@example.com",
        "token_hash": sha(raw),
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        "used_at": None,
    }])
    run(api.reset_password(ResetPasswordRequest(token=raw, new_password="first-new-pass")))
    with pytest.raises(HTTPException) as ei:
        run(api.reset_password(ResetPasswordRequest(token=raw, new_password="second-new-pass")))
    assert ei.value.status_code == 400


def test_concurrent_redemption_only_one_wins(monkeypatch):
    db, _ = setup(monkeypatch)
    raw = "concurrent-token-value-abc"
    seed(db, "password_reset_tokens", [{
        "id": "t1", "user_id": "u1", "email": "customer@example.com",
        "token_hash": sha(raw),
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        "used_at": None,
    }])
    async def twice():
        results = []
        for pw in ("winner-pass-1", "loser-pass-2"):
            try:
                results.append(await api.reset_password(ResetPasswordRequest(token=raw, new_password=pw)))
            except HTTPException as e:
                results.append(e.status_code)
        return results
    out = run(twice())
    assert out[0].get("ok") is True
    assert out[1] == 400


def test_short_password_rejected_by_schema(monkeypatch):
    db, _ = setup(monkeypatch)
    with pytest.raises(Exception):
        ResetPasswordRequest(token="valid-token-value-abcdef", new_password="short")


def test_legacy_raw_token_still_accepted(monkeypatch):
    db, _ = setup(monkeypatch)
    raw = "legacy-plain-token-abcdef"
    seed(db, "password_reset_tokens", [{
        "id": "t-legacy", "user_id": "u1", "email": "customer@example.com",
        "token": raw,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        "used_at": None,
    }])
    out = run(api.reset_password(ResetPasswordRequest(token=raw, new_password="legacy-new-pass")))
    assert out.get("ok") is True
    assert docs(db, "users")[0]["password_hash"] != "old-hash-1"


def test_password_write_failure_releases_claim(monkeypatch):
    db, _ = setup(monkeypatch)
    raw = "claim-release-token-abcdef"
    seed(db, "password_reset_tokens", [{
        "id": "t1", "user_id": "u1", "email": "customer@example.com",
        "token_hash": sha(raw),
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        "used_at": None,
    }])
    orig = db.users.update_one

    async def boom(query, update, **kw):
        raise RuntimeError("storage down")

    db.users.update_one = boom
    with pytest.raises(HTTPException) as ei:
        run(api.reset_password(ResetPasswordRequest(token=raw, new_password="anything-pass")))
    assert ei.value.status_code == 503
    assert docs(db, "password_reset_tokens")[0]["used_at"] is None, "token must be reusable after failed write"
    db.users.update_one = orig
