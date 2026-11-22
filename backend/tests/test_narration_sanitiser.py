"""Spoken narration must not contain phones, URLs or digit tokens.

The sanitiser was hot-patched into image
release-93a3b3d-narrationfix-20261122T1348Z on 22 Nov 2026 after manual
generation failed at render (34%): Whisper heard "08 7073 5044" as
merged digit groups that could never match the script tokens.
Contact details belong in cta_block, not in the spoken script.
"""
from __future__ import annotations

import re

from backend.app import generation

_DIGITS = re.compile(r"\d")


def test_phone_number_stripped_and_contact_becomes_speakable_cta():
    cleaned = generation._sanitize_narration(
        "Come down to Texas Diner today. Call 08 7073 5044."
    )
    assert "Texas Diner" in cleaned
    assert not _DIGITS.search(cleaned)
    assert "08" not in cleaned
    assert "7073" not in cleaned
    assert "5044" not in cleaned
    assert cleaned == "Come down to Texas Diner today. Call us today."


def test_urls_stripped_from_narration():
    cleaned = generation._sanitize_narration(
        "Find us at https://texasdiner.com.au and at www.example.org/menu today."
    )
    assert "https" not in cleaned.lower()
    assert "www." not in cleaned.lower()
    assert "texasdiner.com.au" not in cleaned.lower()
    assert "example.org" not in cleaned.lower()
    assert not _DIGITS.search(cleaned)


def test_digit_tokens_stripped_from_narration():
    cleaned = generation._sanitize_narration(
        "Open from 9am until 5pm since 2019, and the special is number 808-70073-5044."
    )
    assert not _DIGITS.search(cleaned)
    assert "808" not in cleaned
    assert "2019" not in cleaned
    assert "9am" not in cleaned
    assert cleaned.startswith("Open from")


def test_dangling_call_or_visit_clause_becomes_speakable_cta():
    assert generation._sanitize_narration("Call or visit today.") == (
        "Call us today or visit our website."
    )


def test_clean_narration_is_left_unchanged():
    text = "Come down to Texas Diner today. Call us today or visit our website."
    assert generation._sanitize_narration(text) == text
    assert generation._sanitize_narration("") == ""
    assert generation._sanitize_narration(None) is None


def test_finalize_script_sanitises_narration_and_leaves_contact_in_cta_block():
    result = generation._finalize_script(
        {
            "full_script": (
                "Visit Texas Diner. Call 08 7073 5044 or visit texasdiner.com.au today."
            ),
            "cta_block": "Call 08 7073 5044 or visit https://texasdiner.com.au",
        }
    )
    assert result["full_script"] == (
        "Visit Texas Diner. Call us today or visit our website."
    )
    assert not _DIGITS.search(result["full_script"])
    # Contact is kept for on-screen / metadata use; the sanitiser does not
    # copy it into cta_block, but it must not wipe a CTA that already has it.
    assert result["cta_block"] == "Call 08 7073 5044 or visit https://texasdiner.com.au"
    assert result["word_count"] == 10


def test_educational_prompt_forbids_digits_and_keeps_contact_in_cta():
    prompt = (generation.PROMPTS_DIR / "educational_script.txt").read_text()
    assert "phone number" in prompt.lower()
    assert "CTA" in prompt or "cta" in prompt.lower()
    assert "digits" in prompt.lower()
