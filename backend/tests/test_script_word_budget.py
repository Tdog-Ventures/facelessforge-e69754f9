"""A short model draft is sent back for a length rewrite before it is saved."""
import asyncio

from backend.app import generation


def _egg_project():
    return {
        "id": "p-egg",
        "topic": "How to cook an egg",
        "niche": "Cooking Techniques",
        "target_duration": 136,
        "production_mode": "editorial",
    }


def _payload(text):
    return {
        "hook_option_one": "Hook",
        "hook_option_two": "Hook 2",
        "hook_option_three": "Hook 3",
        "selected_hook": "Hook",
        "full_script": text,
        "retention_beats": ["times"],
        "cta_block": "Subscribe",
    }


def test_short_script_is_rewritten_into_the_word_window(monkeypatch):
    calls = []
    short = " ".join(["eggs"] * 40)
    budget = generation.target_word_count(133.5)
    long = " ".join(["egg"] * budget)

    async def fake_llm(_system, _user, cache_key=""):
        calls.append(cache_key)
        text = long if "words-" in cache_key else short
        return _payload(text)

    monkeypatch.setattr(generation, "_llm_json", fake_llm)
    result = asyncio.run(generation.generate_script(_egg_project()))
    low, high = generation._word_window(result["word_budget"])
    assert low <= result["word_count"] <= high
    assert any("words-" in key for key in calls)


def test_fixed_short_script_is_kept_when_the_model_will_not_expand(monkeypatch):
    text = "Zapier connects your apps so a small team can ship a video every week."

    async def fake_llm(*_args, **_kwargs):
        return _payload(text)

    monkeypatch.setattr(generation, "_llm_json", fake_llm)
    result = asyncio.run(generation.generate_script({
        "id": "p1",
        "topic": "How Zapier connects apps",
        "niche": "video",
        "target_duration": 60,
        "production_mode": "editorial",
    }))
    assert result["full_script"] == text


def test_long_script_is_cut_to_the_word_budget():
    narration = " ".join(["egg"] * 358)
    out = generation._finalize_script({
        "full_script": narration,
        "hook_option_one": "Hook",
        "hook_option_two": "Hook 2",
        "hook_option_three": "Hook 3",
        "selected_hook": "Hook",
        "retention_beats": [],
        "cta_block": "Subscribe",
    }, 133.5)
    assert out["word_budget"] == 334
    assert 317 <= out["word_count"] <= 351
    assert out["estimated_duration"] <= 140
