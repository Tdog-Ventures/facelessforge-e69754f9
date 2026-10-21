"""Generated scenes must not rewrite the approved script or invent tools."""
from __future__ import annotations

import asyncio

from backend.app import generation


def _project(topic: str = "How to plan a weekly video") -> dict:
    return {
        "id": "p1", "topic": topic, "niche": "video production",
        "audience": "small teams", "target_duration": 60,
        "production_mode": "editorial",
    }


def test_exact_splitter_preserves_every_word_when_sentences_outnumber_scenes():
    text = "One. Two. Three. Four. Five. Six."
    chunks = generation._split_script_exact(text, 3)
    assert " ".join(chunks) == text
    assert all(chunks)


def test_exact_splitter_preserves_every_word_when_scenes_outnumber_sentences():
    text = "A single long sentence with enough words to divide across four scenes."
    chunks = generation._split_script_exact(text, 4)
    assert " ".join(chunks) == text
    assert all(chunks)


def test_scene_plan_overrides_model_narration_with_exact_script(monkeypatch):
    script_text = (
        "Start with a clear promise. Show the viewer the first step. "
        "Then prove the result. End with one specific action."
    )
    fake_scenes = {"scenes": [
        {
            "scene_number": 1, "narration_text": "The model rewrote this.",
            "visual_direction": "a planner on a desk", "caption_text": "Plan",
            "search_terms": ["weekly planner", "desk"],
        },
        {
            "scene_number": 2, "narration_text": "And invented that.",
            "visual_direction": "a finished video", "caption_text": "Ship",
            "search_terms": ["video editing", "timeline"],
        },
    ]}

    async def fake_llm(*_args, **_kwargs):
        return fake_scenes

    monkeypatch.setattr(generation, "_llm_json", fake_llm)
    scenes = asyncio.run(generation.generate_scene_plan(
        _project(), {"full_script": script_text},
    ))

    assert len(scenes) == 2
    assert " ".join(scene["narration_text"] for scene in scenes) == script_text
    assert all("rewrote" not in scene["narration_text"] for scene in scenes)
    assert all("invented" not in scene["caption_text"] for scene in scenes)
    assert scenes[0]["visual_direction"] == "a planner on a desk"


def test_scene_plan_pads_missing_scenes_without_losing_script(monkeypatch):
    script_text = "First point. Second point. Third point. Fourth point."
    fake_scenes = {"scenes": [
        {"scene_number": 1, "narration_text": "wrong", "search_terms": ["one"]},
    ]}

    async def fake_llm(*_args, **_kwargs):
        return fake_scenes

    monkeypatch.setattr(generation, "_llm_json", fake_llm)
    project = dict(_project(), target_duration=120)  # 117.5s narration -> 4 scenes
    scenes = asyncio.run(generation.generate_scene_plan(
        project, {"full_script": script_text},
    ))

    assert len(scenes) == 4
    assert " ".join(scene["narration_text"] for scene in scenes) == script_text
    assert all(scene["narration_text"] for scene in scenes)


def test_generated_script_with_invented_tool_falls_back(monkeypatch):
    async def fake_llm(*_args, **_kwargs):
        return {
            "hook_option_one": "Hook", "hook_option_two": "Hook 2",
            "hook_option_three": "Hook 3", "selected_hook": "Hook",
            "full_script": "Use Zapier to connect everything smoothly and save time.",
            "retention_beats": ["Zapier connects everything smoothly"],
            "cta_block": "Book a Zapier demo",
        }

    monkeypatch.setattr(generation, "_llm_json", fake_llm)
    result = asyncio.run(generation.generate_script(_project()))

    assert "Zapier" not in result["full_script"]
    assert all("Zapier" not in beat for beat in result["retention_beats"])


def test_generated_script_may_name_a_tool_the_brief_requests(monkeypatch):
    text = "Zapier connects your apps so a small team can ship a video every week."

    async def fake_llm(*_args, **_kwargs):
        return {
            "hook_option_one": "Hook", "hook_option_two": "Hook 2",
            "hook_option_three": "Hook 3", "selected_hook": "Hook",
            "full_script": text,
            "retention_beats": ["Zapier automation"],
            "cta_block": "Try Zapier",
        }

    monkeypatch.setattr(generation, "_llm_json", fake_llm)
    result = asyncio.run(generation.generate_script(_project("How Zapier connects apps")))

    assert result["full_script"] == text


def test_common_word_notion_is_no_longer_a_tool_claim(monkeypatch):
    # Regression for CodeRabbit P3 in PR #15: "notion" was matched
    # case-insensitively, discarding common-word usages wholesale.
    text = "The notion of a weekly cadence keeps a small team shipping."

    async def fake_llm(*_args, **_kwargs):
        return {
            "hook_option_one": "Hook", "hook_option_two": "Hook 2",
            "hook_option_three": "Hook 3", "selected_hook": "Hook",
            "full_script": text,
            "retention_beats": ["The weekly notion"],
            "cta_block": "Subscribe",
        }

    monkeypatch.setattr(generation, "_llm_json", fake_llm)
    result = asyncio.run(generation.generate_script(_project()))

    assert result["full_script"] == text


def test_branded_notion_claim_is_still_discarded(monkeypatch):
    text = "Notion connects your docs and your tasks in one workspace."

    async def fake_llm(*_args, **_kwargs):
        return {
            "hook_option_one": "Notion hook", "hook_option_two": "Hook 2",
            "hook_option_three": "Hook 3", "selected_hook": "Notion hook",
            "full_script": text,
            "retention_beats": ["Notion workspace"],
            "cta_block": "Try Notion",
        }

    monkeypatch.setattr(generation, "_llm_json", fake_llm)
    result = asyncio.run(generation.generate_script(_project()))

    assert "Notion" not in result["full_script"]


def test_notion_brief_request_still_requires_brand_casing(monkeypatch):
    reverse_text = (
        "The pre-fix matcher accepted the lowercased brand name itself, so the "
        "project brief talking about Notion had to survive this check."
    )

    async def fake_llm(*_args, **_kwargs):
        return {
            "hook_option_one": "Hook", "hook_option_two": "Hook 2",
            "hook_option_three": "Hook 3", "selected_hook": "Hook",
            "full_script": reverse_text,
            "retention_beats": ["editorial wiggle"],
            "cta_block": "Subscribe",
        }

    monkeypatch.setattr(generation, "_llm_json", fake_llm)
    # Brief requests the tool; script uses the brand casing -> kept.
    ok = asyncio.run(generation.generate_script(_project("How Notion templates help small teams")))
    assert "Notion" in ok["full_script"]

    # Brand casing in the script but absent from the brief -> still discarded.
    stray = asyncio.run(generation.generate_script(_project()))
    assert "Notion" not in stray["full_script"]


def test_exact_splitter_never_hands_empty_chunks_to_late_scenes():
    # Regression for CodeRabbit P4 in PR #15: with three short sentences and
    # one long closer over four scenes, the greedy splitter consumed every
    # sentence into scene 1 and handed empty strings to scenes 2-4.
    text = (
        "Begin with the exact promise the video will keep. "
        "Show the first concrete step the viewer can copy. "
        "Prove the payoff with a before and after shot. "
        "And close with a genuinely long final sentence that keeps adding one more clause "
        "then another and another so that the character balancing code could never fit it "
        "inside the earlier scene targets no matter how it tried."
    )
    chunks = generation._split_script_exact(text, 4)

    assert all(chunks), chunks
    assert " ".join(chunks) == " ".join(text.split())


def test_exact_splitter_stays_finite_when_words_fewer_than_scenes():
    # Regression found while running PR #18's padding review test: with a
    # short script against 120 scenes the word-splitting clamps went
    # negative and sliced words twice, raising IndexError.
    text = "A short b roll scene."
    chunks = generation._split_script_exact(text, 120)

    assert len(chunks) == 120
    assert " ".join(chunks).split() == text.split()  # no word lost or duplicated
    assert all(chunk is not None for chunk in chunks)


def test_short_educational_budget_fallback_keeps_the_topic(monkeypatch):
    # Regression for CodeRabbit P7 in PR #15: a 5s editorial target leaves
    # ~6 narration words; truncating the long skeleton dropped the topic and
    # kept only "Today we are looking at what".
    async def _no_llm(*_args, **_kwargs):
        return None

    monkeypatch.setattr(generation, "_llm_json", _no_llm)
    project = _project(topic="How much do dogs sleep")
    project["target_duration"] = 5
    result = asyncio.run(generation.generate_script(project))

    assert "how much do dogs sleep" in result["full_script"].lower()
    assert result["word_count"] <= 6 * 2  # short script, not a 30-word skeleton head


def test_short_budget_never_returns_more_words_than_it_can_speak(monkeypatch):
    # Regression for the Codex P2 on PR #19: a 7-9 word topic against a 5s editorial
    # budget (~6 words) was returned whole. Finalization tolerates 50% overrun, so the
    # voiceover overran the slot and failed render preflight.
    async def _no_llm(*_args, **_kwargs):
        return None

    monkeypatch.setattr(generation, "_llm_json", _no_llm)
    # Nine words: finalization's 50% tolerance lets it through untouched.
    project = _project(topic="How much sleep do older senior dogs need nightly")
    project["target_duration"] = 5
    result = asyncio.run(generation.generate_script(project))

    budget = result["word_budget"]
    assert budget is not None and budget <= 12
    spoken = result["full_script"].split()
    assert len(spoken) <= budget, (len(spoken), budget, result["full_script"])
    assert result["full_script"].lower().startswith("how much sleep do older senior")


def test_longer_educational_budget_keeps_the_full_skeleton(monkeypatch):
    async def _no_llm(*_args, **_kwargs):
        return None

    monkeypatch.setattr(generation, "_llm_json", _no_llm)
    project = _project(topic="How much do dogs sleep")
    result = asyncio.run(generation.generate_script(project))

    lowered = result["full_script"].lower()
    assert lowered.startswith("today we are looking at what the research actually says about")
