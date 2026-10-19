"""OAuth callback HTML must escape attacker-controlled query values (CWE-79)."""
from types import SimpleNamespace

import pytest

from app.routes import youtube_oauth


@pytest.mark.asyncio
async def test_script_payload_in_error_param_is_escaped():
    payload = '<script>alert("xss")</script>'
    resp = await youtube_oauth.youtube_callback(SimpleNamespace(), error=payload)

    assert resp.status_code == 400
    body = resp.body.decode()
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


@pytest.mark.asyncio
async def test_attribute_breakout_in_error_param_is_inert():
    payload = '"><img src=x onerror=alert(1)>'
    resp = await youtube_oauth.youtube_callback(SimpleNamespace(), error=payload)

    body = resp.body.decode()
    assert "<img" not in body
    assert "&lt;img" in body
    assert "&quot;" in body


@pytest.mark.asyncio
async def test_plain_error_text_still_renders():
    resp = await youtube_oauth.youtube_callback(
        SimpleNamespace(), error="access_denied")

    assert resp.status_code == 400
    assert "access_denied" in resp.body.decode()
