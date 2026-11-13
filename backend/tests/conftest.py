"""Session-wide test safety: a live API key can never turn a test into a paid run.

Every test runs with paid API calls disabled and all real provider credentials
removed from the environment. Tests that intentionally exercise a *mocked*
provider client can opt in explicitly with FF_TEST_AUTHORISE_PAID_API_CALLS=1
(or by setting FF_ALLOW_PAID_API_CALLS=1 themselves). No test may reach a real
paid endpoint by accident.
"""
from __future__ import annotations

import os

import pytest

_LIVE_CREDENTIALS = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS", "DEEPSEEK_API_KEY", "KIMI_API_KEY",
    "MOONSHOT_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY", "PEXELS_API_KEY", "PIXABAY_API_KEY",
    "UNSPLASH_ACCESS_KEY", "EMERGENT_KEY", "PADDLE_API_KEY", "PADDLE_APIKEY",
)


@pytest.fixture(autouse=True)
def _zero_paid_api_calls(monkeypatch):
    if os.environ.get("FF_TEST_AUTHORISE_PAID_API_CALLS") == "1":
        return
    monkeypatch.setenv("FF_ALLOW_PAID_API_CALLS", "0")
    for name in _LIVE_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
