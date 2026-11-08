"""Regressions for the 120s script-length correction path (fragment contract).

No provider calls: generation._llm_json is replaced with a scripted fake.
"""
import asyncio
import sys

sys.path.insert(0, "/app/backend")

import pytest

import backend.app.generation as gen


def words(n: int, stem: str = "insight") -> str:
    return " ".join(f"{stem}{i}" for i in range(n))


def payload(text: str, **extra) -> dict:
    d = {
        "hook_option_one": "a short hook",
        "hook_option_two": "another hook",
        "hook_option_three": "a third hook",
        "selected_hook": "a short hook",
        "full_script": text,
        "retention_beats": ["one", "two", "three"],
        "cta_block": "subscribe for more",
    }
    d.update(extra)
    return d


def project(target_duration=120):
    return {
        "id": "p-length-1",
        "topic": "how to cook an egg every way",
        "niche": "food",
        "tone": "documentary",
        "target_duration": target_duration,
        "aspect_ratio": "9:16",
    }


def plan_for(proj):
    plan = gen.NarrationPlan.from_project(proj)
    return plan.words_min, plan.words_max, plan.words_target


def run_with(monkeypatch, responses):
    """responses: list of dicts returned in call order."""
    calls = {"n": 0, "keys": []}

    async def fake_llm_json(system, user, cache_key="", max_tokens=8192):
        calls["keys"].append(cache_key)
        i = calls["n"]
        calls["n"] += 1
        return responses[i] if i < len(responses) else responses[-1]

    monkeypatch.setattr(gen, "_llm_json", fake_llm_json)
    return calls


def test_bounds_for_120s_are_280_to_308():
    lo, hi, tgt = plan_for(project())
    assert (lo, hi) == (280, 308), (lo, hi, tgt)


def test_compliant_continuation_reaches_valid_envelope(monkeypatch):
    lo, hi, tgt = plan_for(project())
    assert gen.micro_continuation(248, min_words=lo, max_words=hi, target_words=tgt) is None
    first = payload(words(248))
    cont = {"continuation": words(40, stem="closing")}
    run_with(monkeypatch, [first, cont])
    out = asyncio.run(gen.generate_script(project()))
    assert lo <= out["word_count"] <= hi, out["word_count"]
    assert out["length_status"] == "within_bounds"
    assert "closing0" in out["full_script"]


def test_full_rewrite_in_fragment_path_is_not_appended(monkeypatch):
    lo, hi, tgt = plan_for(project())
    original = words(248)
    assert gen.micro_continuation(248, min_words=lo, max_words=hi, target_words=tgt) is None
    first = payload(original)
    rewrite = {"full_script": original + " " + words(12, stem="extra")}
    run_with(monkeypatch, [first, rewrite, rewrite])
    with pytest.raises(gen.ScriptLengthError) as ei:
        asyncio.run(gen.generate_script(project()))
    assert ei.value.word_count <= hi, ei.value.word_count


def test_uncorrectable_output_fails_honestly(monkeypatch):
    lo, hi, tgt = plan_for(project())
    proj = project()
    snapshot = dict(proj)
    first = payload(words(200))
    short_fragment = {"continuation": words(3, stem="tiny")}
    run_with(monkeypatch, [first, short_fragment, short_fragment])
    with pytest.raises(gen.ScriptLengthError) as ei:
        asyncio.run(gen.generate_script(proj))
    assert ei.value.word_count == 200
    assert proj == snapshot, "generation mutated the project on failure"


def test_too_long_correction_still_accepted(monkeypatch):
    lo, hi, tgt = plan_for(project())
    first = payload(words(340, stem="over"))
    condensed = payload(words(290, stem="trim"))
    run_with(monkeypatch, [first, condensed])
    out = asyncio.run(gen.generate_script(project()))
    assert lo <= out["word_count"] <= hi, out["word_count"]
    assert out["length_status"] == "within_bounds"
