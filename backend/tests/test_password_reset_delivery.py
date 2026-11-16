import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.models import ForgotPasswordRequest, ResetPasswordRequest
from app.routes import api as api_routes


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = list(documents or [])

    async def count_documents(self, _query):
        return 0

    async def find_one(self, query):
        return next(
            (doc for doc in self.documents if all(doc.get(key) == value for key, value in query.items())),
            None,
        )

    async def insert_one(self, document):
        self.documents.append(dict(document))

    async def update_one(self, query, update):
        for document in self.documents:
            if all(
                document.get(key) > value["$gt"] if isinstance(value, dict)
                else document.get(key) == value
                for key, value in query.items()
            ):
                document.update(update["$set"])
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def update_many(self, query, update):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                document.update(update["$set"])


class FakeDB:
    def __init__(self):
        self.users = FakeCollection([
            {"id": "user-1", "name": "Ada Lovelace", "email": "ada@example.com"},
        ])
        self.password_reset_attempts = FakeCollection()
        self.password_reset_tokens = FakeCollection()


@pytest.mark.asyncio
async def test_forgot_password_disables_token_when_email_delivery_fails(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    async def fail_delivery(**_kwargs):
        return {"ok": False, "mode": "resend", "error": "provider unavailable"}

    monkeypatch.setattr(api_routes, "send_password_reset_email", fail_delivery, raising=False)
    monkeypatch.setenv("FRONTEND_URL", "https://facelessforge.example")
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.8"))

    tasks = BackgroundTasks()
    response = await api_routes.forgot_password(
        ForgotPasswordRequest(email="ada@example.com"),
        request,
        tasks,
    )
    assert db.password_reset_tokens.documents[0]["used_at"] is None
    await tasks()

    assert response == {
        "ok": True,
        "message": "If that email exists, a reset link has been issued.",
    }
    assert len(db.password_reset_tokens.documents) == 1
    assert db.password_reset_tokens.documents[0]["used_at"] is not None


@pytest.mark.asyncio
async def test_forgot_password_keeps_delivered_token_active(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    async def deliver(**_kwargs):
        assert _kwargs["recipient"] == "ada@example.com"
        assert _kwargs["reset_url"].startswith(
            "https://facelessforge.example/reset-password?token="
        )
        assert _kwargs["expires_in_minutes"] == 60
        return {"ok": True, "mode": "resend"}

    monkeypatch.setattr(api_routes, "send_password_reset_email", deliver, raising=False)
    monkeypatch.setenv("FRONTEND_URL", "https://facelessforge.example/")
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.8"))

    tasks = BackgroundTasks()
    await api_routes.forgot_password(
        ForgotPasswordRequest(email="ada@example.com"),
        request,
        tasks,
    )
    await tasks()

    assert len(db.password_reset_tokens.documents) == 1
    assert db.password_reset_tokens.documents[0]["used_at"] is None


@pytest.mark.asyncio
async def test_forgot_password_still_hides_account_when_mailer_raises(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    async def raise_from_mailer(**_kwargs):
        raise RuntimeError("mailer crashed")

    monkeypatch.setattr(api_routes, "send_password_reset_email", raise_from_mailer)
    monkeypatch.delenv("DEV_MODE", raising=False)
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.8"))

    tasks = BackgroundTasks()
    response = await api_routes.forgot_password(
        ForgotPasswordRequest(email="ada@example.com"),
        request,
        tasks,
    )
    await tasks()

    assert response["ok"] is True
    assert db.password_reset_tokens.documents[0]["used_at"] is not None


@pytest.mark.asyncio
async def test_concurrent_requests_can_only_use_a_reset_token_once(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(api_routes, "hash_password", lambda password: password)
    db.password_reset_tokens.documents.append({
        "token": "single-use-token", "user_id": "user-1", "used_at": None,
        "expires_at": api_routes._now() + timedelta(minutes=10),
    })
    tokens = db.password_reset_tokens
    read_token = tokens.find_one
    both_read = asyncio.Event()
    reads = 0

    async def simultaneous_reads(query):
        nonlocal reads
        record = dict(await read_token(query))
        reads += 1
        if reads == 2:
            both_read.set()
        await both_read.wait()
        return record

    monkeypatch.setattr(tokens, "find_one", simultaneous_reads)
    outcomes = await asyncio.gather(*(api_routes.reset_password(ResetPasswordRequest(
        token="single-use-token", new_password=password,
    )) for password in ("first-password", "second-password")), return_exceptions=True)

    assert sum(isinstance(result, dict) and result.get("ok") for result in outcomes) == 1
    errors = [result for result in outcomes if isinstance(result, HTTPException)]
    assert [error.status_code for error in errors] == [400]
