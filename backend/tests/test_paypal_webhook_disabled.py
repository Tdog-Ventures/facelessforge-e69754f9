"""Regression: the legacy PayPal webhook must stay hard-disabled (HTTP 410).

Commit d1caaea closed the unauthenticated mint path in app/billing_secure.py
by raising 410 before any provider/signature logic. The old code failed open
(_verify_paypal_signature returned True with no creds configured), so any
anonymous POST with {"email","plan"} could mint credits via the Paddle/PayPal
provision shortcut. This test pins the disable.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.billing_secure import router


@pytest.fixture
def app():
    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


@pytest.mark.asyncio
async def test_paypal_webhook_returns_410_without_any_auth(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/api/billing/webhook/paypal",
            json={"email": "attacker@evil.example", "plan": "pro",
                  "eventType": "transaction.completed"},
        )
    assert resp.status_code == 410
    assert resp.json()["detail"] == "disabled"


@pytest.mark.asyncio
async def test_paypal_webhook_returns_410_even_with_forged_signature(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/api/billing/webhook/paypal",
            content=b'{"email":"attacker@evil.example","plan":"pro"}',
            headers={
                "content-type": "application/json",
                "paypal-transmission-sig": "Zm9yZ2VkLXNpZw==",
                "paypal-transmission-id": "fake-id",
                "paypal-cert-url": "https://evil.example/cert",
            },
        )
    assert resp.status_code == 410
