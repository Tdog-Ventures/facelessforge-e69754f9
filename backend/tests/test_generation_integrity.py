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
