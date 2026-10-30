import pytest
from unittest.mock import ANY

from app import email as email_service


def test_password_reset_email_contains_the_link_and_expiry_without_exposing_it_in_subject():
    message = email_service.render_password_reset_email(
        recipient="ada@example.com",
        reset_url="https://facelessforge.example/reset-password?token=secret-token",
        expires_in_minutes=60,
    )

    assert message["subject"] == "Reset your FacelessForge password"
    assert "secret-token" not in message["subject"]
    assert 'href="https://facelessforge.example/reset-password?token=secret-token"' in message["html"]
    assert "expires in 60 minutes" in message["text"]


@pytest.mark.asyncio
async def test_password_reset_email_uses_resend_when_configured(monkeypatch):
    requests = []

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return FakeResponse()

    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("MAIL_FROM", "FacelessForge <support@example.com>")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setattr(email_service.httpx, "AsyncClient", FakeClient, raising=False)

    result = await email_service.send_password_reset_email(
        recipient="ada@example.com",
        reset_url="https://facelessforge.example/reset-password?token=secret-token",
        expires_in_minutes=60,
    )

    assert result == {"ok": True, "mode": "resend"}
    assert requests == [
        (
            "https://api.resend.com/emails",
            {
                "headers": {
                    "Authorization": "Bearer test-key",
                    "Content-Type": "application/json",
                },
                "json": {
                    "from": "FacelessForge <support@example.com>",
                    "to": ["ada@example.com"],
                    "subject": "Reset your FacelessForge password",
                    "html": ANY,
                    "text": ANY,
                },
            },
        )
    ]

