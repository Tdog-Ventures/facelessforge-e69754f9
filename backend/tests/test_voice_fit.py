"""Voiceover/target fit policy: measured audio decides, target stays
authoritative, manual scripts are never rewritten, AI corrections are
bounded (initial + 2), attempts and actions persist, failures are explicit.
"""
import math

import pytest

from app import render
from app.routes import api as api_routes
from app.timing import TimingContract, build_timing_status, suggest_fitted_duration
from backend.tests.fake_db import FakeCollection as ProjectCollection


def _match(doc, query):
    for key, cond in query.items():
        if isinstance(cond, dict) and "$in" in cond:
            if doc.get(key) not in cond["$in"]:
                return False
        elif doc.get(key) != cond:
            return False
    return True


class FakeResult:
    def __init__(self, n=0):
        self.deleted_count = n
        self.modified_count = n


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]
        self.calls = []

    async def find_one(self, query, *_args, **_kwargs):
        for doc in self.docs:
            if _match(doc, query):
                return dict(doc)
        return None

    async def update_one(self, query, update, upsert=False):
        self.calls.append(("update_one", dict(query), dict(update.get("$set", {}))))
        for doc in self.docs:
            if _match(doc, query):
                doc.update(update.get("$set", {}))
                for k, v in update.get("$unset", {}).items():
                    doc.pop(k, None)
                return FakeResult(1)
        if upsert:
            new_doc = {k: v for k, v in query.items() if not isinstance(v, dict)}
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)
        return FakeResult(0)

    async def update_many(self, query, update):
        self.calls.append(("update_many", dict(query)))
        n = 0
        for doc in self.docs:
            if _match(doc, query):
                doc.update(update.get("$set", {}))
                n += 1
        return FakeResult(n)

    async def insert_one(self, document):
        self.docs.append(dict(document))
        return FakeResult(1)

    async def delete_many(self, query):
        before = len(self.docs)
        self.docs[:] = [d for d in self.docs if not _match(d, query)]
        return FakeResult(before - len(self.docs))

    async def replace_one(self, query, document, upsert=False):
        for i, doc in enumerate(self.docs):
            if _match(doc, query):
                self.docs[i] = dict(document)
                return FakeResult(1)
        if upsert:
            self.docs.append(dict(document))
        return FakeResult(0)

    def find(self, query=None, *_args, **_kwargs):
        matched = [d for d in self.docs if _match(d, query or {})]
        return _FakeCursor(matched)


class _FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_a, **_k):
        return self

    async def to_list(self, _n):
        return [dict(d) for d in self.docs]


class FakeDB:
    def __init__(self, project, script, assets=None):
        self.projects = ProjectCollection("projects", [project])
        self.projects.calls = []
        update_project = self.projects.update_one

        async def tracked_update(query, update, **kwargs):
            self.projects.calls.append(("update_one", dict(query), dict(update.get("$set", {}))))
            return await update_project(query, update, **kwargs)

        self.projects.update_one = tracked_update
        self.scripts = FakeCollection([script] if script else [])
        self.assets = FakeCollection(assets)
        self.scenes = FakeCollection([{"id": "sc1", "project_id": project["id"],
                                       "duration": 27.5, "end_time": 27.5}])
        self.metadata_packages = FakeCollection([{"id": "m1", "project_id": project["id"]}])
        self.render_jobs = FakeCollection()


def _project(**over):
    doc = {"id": "p1", "user_id": "u1", "target_duration": 30, "status": "DRAFT"}
    doc.update(over)
    return doc


def _script(text, source="ai_generated", sid="s1"):
    words = len(text.split())
    return {"id": sid, "project_id": "p1", "full_script": text, "word_count": words,
            "estimated_duration": int(words / 2.5), "source": source,
            "updated_at": "2026-10-11T00:00:00"}


def _user():
    return {"id": "u1", "role": "creator", "email": "u1@example.com"}


def _stub_attach(monkeypatch):
    async def stub(_db, _project):
        return {"project": _project, "stub": True}

    monkeypatch.setattr(api_routes, "_attach_project_view", stub)


def _tts_factory(durations, calls):
    async def fake_tts(**kwargs):
        calls.append(dict(kwargs))
        return {"id": kwargs.get("asset_id") or "a-new", "duration": durations[min(len(calls) - 1, len(durations) - 1)],
                "mock": False, "provider": "elevenlabs"}

    return fake_tts


@pytest.mark.parametrize("mode,measured,target,intro,narration", [
    ("editorial", 19.0, 22, 2.5, 19.5),
    ("commercial_ad", 19.0, 19, 0.0, 19.0),
    ("editorial", 2.4, 5, 2.5, 2.5),
    ("commercial_ad", 5.0, 5, 0.0, 5.0),
    ("editorial", 10.0, 13, 2.5, 10.5),  # rounded 12s fails; adjacent 13s fits
    ("editorial", 3597.5, 3600, 2.5, 3597.5),
    ("editorial", 3600.0, 3600, 2.5, 3597.5),  # fit at the legal upper bound
])
def test_duration_fit_obeys_product_bounds_and_intro_allocation(mode, measured, target, intro, narration):
    # The product range is 5..3600, not the obsolete 30-second minimum.
    # In particular, 19s of voice fits a 22s editorial target: 19.5s for
    # narration after reserving the intro, with only ~2.56% tempo adjustment.
    project = {"target_duration": 30, "production_mode": mode}
    assert suggest_fitted_duration(voiceover_seconds=measured, intro_seconds=intro) == target
    assert render.suggest_duration_autofit(project, {"duration": measured}) == {
        "from": 30, "to": target, "voiceover_seconds": measured,
    }
    assert project == {"target_duration": 30, "production_mode": mode}, "a suggestion is not a duration edit"

    fitted_project = dict(project, target_duration=target)
    timing = TimingContract.from_project(fitted_project)
    assert timing.intro_seconds == intro
    assert timing.narration_seconds == narration
    assert timing.intro_frames + timing.narration_frames == target * timing.fps
    status = build_timing_status(
        project=fitted_project, scenes=[{"end_time": narration}], voiceover={"duration": measured},
    )
    assert status["ready"] is True
    assert status["voiceover_tempo_ratio"] == pytest.approx(measured / narration, abs=1e-6)
    assert render.suggest_duration_autofit(fitted_project, {"duration": measured}) is None


@pytest.mark.parametrize("measured,accepted", [
    (math.nextafter(2.375, -math.inf), False),
    (2.375, True),  # exactly -5% of the 2.5-second narration budget
    (math.nextafter(2.375, math.inf), True),
    (math.nextafter(2.625, -math.inf), True),
    (2.625, True),  # exactly +5%
    (math.nextafter(2.625, math.inf), False),
])
def test_five_second_tempo_boundary_is_inclusive_without_expanding_tolerance(measured, accepted):
    expected = 5 if accepted else None
    assert suggest_fitted_duration(voiceover_seconds=measured, intro_seconds=2.5) == expected
    status = build_timing_status(
        project={"target_duration": 5}, scenes=[{"end_time": 2.5}], voiceover={"duration": measured},
    )
    assert status["voiceover_action"] == ("tempo_fit" if accepted else "rebuild")
    assert status["ready"] is accepted
    suggestion = render.suggest_duration_autofit({"target_duration": 30}, {"duration": measured})
    assert suggestion == ({"from": 30, "to": 5, "voiceover_seconds": measured} if accepted else None)


@pytest.mark.parametrize("mode,measured,intro", [
    ("editorial", 2.0, 2.5),  # below 95% of even the minimum narration slot
    ("editorial", 3.0, 2.5),  # too long for 5s, too short for 6s: no whole-second fit
    ("commercial_ad", 5.4, 0.0),  # likewise between the 5s and 6s tempo windows
    ("editorial", 4000.0, 2.5),  # exceeds 105% of the maximum narration slot
    ("commercial_ad", 4000.0, 0.0),
])
def test_truly_impossible_fits_return_failure(mode, measured, intro):
    assert suggest_fitted_duration(voiceover_seconds=measured, intro_seconds=intro) is None
    assert render.suggest_duration_autofit(
        {"target_duration": 30, "production_mode": mode}, {"duration": measured},
    ) is None


@pytest.mark.parametrize("measured", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_voiceover_cannot_suggest_a_duration(measured):
    assert suggest_fitted_duration(voiceover_seconds=measured) is None
    assert render.suggest_duration_autofit({"target_duration": 30}, {"duration": measured}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("measured,accepted", [(2.375, True), (2.625, True), (2.374, False), (2.626, False)])
async def test_manual_voice_generation_uses_the_same_tempo_boundary(monkeypatch, measured, accepted):
    text = "The manual short narration stays unchanged."
    db = FakeDB(_project(target_duration=5), _script(text, source="manual"))
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover", _tts_factory([measured], calls := []))

    async def no_rewrite(*_args, **_kwargs):
        raise AssertionError("manual narration must never be rewritten")

    monkeypatch.setattr(api_routes.gen, "generate_script", no_rewrite)
    _stub_attach(monkeypatch)
    if accepted:
        result = await api_routes.generate_full_voiceover("p1", body=None, user=_user())
        assert result["stub"] is True
        assert db.assets.docs[0]["duration"] == measured
        assert db.projects.docs[0]["selected_voiceover_asset_id"] == db.assets.docs[0]["id"]
    else:
        with pytest.raises(Exception) as exc:
            await api_routes.generate_full_voiceover("p1", body=None, user=_user())
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "voiceover_length_mismatch"
        assert exc.value.detail["autofit_suggestion"] is None
        assert db.assets.docs == []
    assert len(calls) == 1
    assert db.projects.docs[0]["target_duration"] == 5
    assert db.scripts.docs[0]["full_script"] == text


def test_long_voiceover_suggests_explicit_fit():
    assert render.suggest_duration_autofit(
        {"target_duration": 30}, {"duration": 32.6558125}) == {
        "from": 30, "to": 35, "voiceover_seconds": 32.6558125}


def test_tolerance_needs_no_move():
    assert render.suggest_duration_autofit(
        {"target_duration": 30}, {"duration": 27.9}) is None


@pytest.mark.asyncio
async def test_manual_drift_fails_with_numbers_never_rewritten(monkeypatch):
    text = "manual narration here " * 12  # ~36 words -> short of the 27.5s budget
    db = FakeDB(_project(), _script(text, source="manual"))
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover",
                        _tts_factory([18.0], calls := []))
    gen_calls = []
    monkeypatch.setattr(api_routes.gen, "generate_script",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("LLM must not run")))
    _stub_attach(monkeypatch)

    view = await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    assert view["stub"] is True
    assert len(calls) == 1  # single TTS attempt, no script rewrite
    stored = await db.scripts.find_one({"project_id": "p1"})
    assert stored["full_script"] == text  # manual text untouched
    project = await db.projects.find_one({"id": "p1"})
    assert project["target_duration"] != 30  # voice kept, target moved to fit it
    assert db.assets.docs[0]["duration"] == 18.0
    fit = project["voice_fit"]
    assert fit["attempts"] == 1 and fit["measured_seconds"] == 18.0


@pytest.mark.asyncio
async def test_ai_shortfall_corrected_then_fits(monkeypatch):
    short_text = "short ai narration " * 12
    fixed_text = "corrected full narration preserving the original meaning throughout " * 9
    db = FakeDB(_project(), _script(short_text, source="ai_generated"))
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    tts_calls = []
    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover",
                        _tts_factory([18.0, 27.2], tts_calls))
    llm_calls = []

    async def fake_gen(_proj):
        llm_calls.append(dict(_proj).get("target_duration"))
        return {"full_script": fixed_text, "word_count": len(fixed_text.split())}

    monkeypatch.setattr(api_routes.gen, "generate_script", fake_gen)
    _stub_attach(monkeypatch)

    view = await api_routes.generate_full_voiceover("p1", body=None, user=_user())

    assert view["stub"] is True
    assert len(tts_calls) == 2 and len(llm_calls) == 1  # bounded: 1 correction
    assert tts_calls[1]["text"] == fixed_text.strip()  # full corrected narration spoken
    assert len(tts_calls[1]["text"].split()) >= 0.8 * 69  # not truncated to fit
    fit = (await db.projects.find_one({"id": "p1"}))["voice_fit"]
    assert fit["attempts"] == 2 and fit["llm_calls"] == 1
    assert fit["measured_seconds"] == 27.2
    assert db.projects.docs[0]["target_duration"] == 30
    stored = await db.scripts.find_one({"project_id": "p1"})
    assert stored["full_script"] == fixed_text


@pytest.mark.asyncio
async def test_ai_exhaustion_stops_after_two_corrections(monkeypatch):
    text = "short ai narration " * 12
    db = FakeDB(_project(), _script(text, source="ai_generated"))
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    tts_calls = []
    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover",
                        _tts_factory([18.0, 18.5, 18.2, 99.0], tts_calls))
    llm_calls = []

    async def fake_gen(_proj):
        llm_calls.append(1)
        return {"full_script": text, "word_count": len(text.split())}

    monkeypatch.setattr(api_routes.gen, "generate_script", fake_gen)
    _stub_attach(monkeypatch)

    view = await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    assert view["stub"] is True
    assert len(tts_calls) == 3 and len(llm_calls) == 2  # initial + 2, never more
    project = await db.projects.find_one({"id": "p1"})
    assert project["target_duration"] != 30  # last take kept, target moved
    assert db.assets.docs[0]["duration"] == 18.2
    fit = project["voice_fit"]
    assert fit["attempts"] == 3


@pytest.mark.asyncio
async def test_exhausted_budget_rejects_before_provider(monkeypatch):
    script = _script("short ai narration " * 12)
    project = _project(voice_fit={"script_version": f"{script['id']}:{script['updated_at']}",
                                  "attempts": 3, "actions": []})
    db = FakeDB(project, script)
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    tts_calls = []
    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover",
                        _tts_factory([27.0], tts_calls))
    _stub_attach(monkeypatch)

    with pytest.raises(Exception) as exc:
        await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    assert exc.value.status_code == 409
    assert tts_calls == []  # no provider call consumed


@pytest.mark.asyncio
async def test_provider_failure_is_explicit_and_queues_nothing(monkeypatch):
    db = FakeDB(_project(), _script("word " * 70))
    monkeypatch.setattr(api_routes, "get_db", lambda: db)

    async def boom(**_k):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover", boom)
    _stub_attach(monkeypatch)

    with pytest.raises(Exception) as exc:
        await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    assert exc.value.status_code == 502
    assert db.assets.docs == [] and db.render_jobs.docs == []


@pytest.mark.asyncio
async def test_invalid_audio_rejected(monkeypatch):
    db = FakeDB(_project(), _script("word " * 70))
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(api_routes.tts_service, "generate_voiceover",
                        _tts_factory([0], []))
    _stub_attach(monkeypatch)

    with pytest.raises(Exception) as exc:
        await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    assert exc.value.status_code == 502
    assert db.assets.docs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,measured,fitted", [
    ("editorial", 32.6558125, 35),
    ("editorial", 19.0, 22),
    ("editorial", 2.4, 5),
    ("commercial_ad", 5.0, 5),
])
async def test_explicit_autofit_is_idempotent(monkeypatch, mode, measured, fitted):
    voice = {"id": "v1", "project_id": "p1", "asset_type": "voiceover_audio",
             "duration": measured}
    project = _project(target_duration=30, production_mode=mode, selected_voiceover_asset_id="v1")
    db = FakeDB(project, _script("word " * 80), assets=[voice])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(render, "get_db", lambda: db)

    first = await api_routes.render_autofit_duration("p1", user=_user())
    assert first["autofit"] == {"from": 30, "to": fitted, "voiceover_seconds": measured}
    assert first["preflight"]["timing"]["narration_seconds"] == fitted - (2.5 if mode == "editorial" else 0)
    assert first["preflight"]["timing"]["voiceover_action"] in {"ready", "tempo_fit"}
    writes = [c for c in db.projects.calls if c[2].get("target_duration") == fitted]
    assert len(writes) == 1

    with pytest.raises(Exception) as exc:
        await api_routes.render_autofit_duration("p1", user=_user())
    assert exc.value.status_code == 400  # already fitting: no second write
    writes = [c for c in db.projects.calls if c[2].get("target_duration") == fitted]
    assert len(writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("measured", [2.0, 4000.0])
async def test_explicit_impossible_fit_fails_without_duration_or_job_writes(monkeypatch, measured):
    voice = {"id": "v1", "project_id": "p1", "asset_type": "voiceover_audio", "duration": measured}
    db = FakeDB(_project(selected_voiceover_asset_id="v1"), _script("Manual narration", source="manual"), [voice])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(render, "get_db", lambda: db)

    with pytest.raises(Exception) as exc:
        await api_routes.render_autofit_duration("p1", user=_user())
    assert exc.value.status_code == 400
    assert "No in-bounds duration" in exc.value.detail
    assert db.projects.docs[0]["target_duration"] == 30
    assert not [call for call in db.projects.calls if "target_duration" in call[2]]
    assert db.render_jobs.docs == []
    assert db.assets.docs == [voice]


@pytest.mark.asyncio
async def test_tts_measures_audio_not_bytes(monkeypatch, tmp_path):
    import wave as _wave
    from app import tts as tts_module

    wav = tmp_path / "probe.wav"
    with _wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 22050 * 3)
    measured = await tts_module._measure_audio_duration(wav)
    assert measured == pytest.approx(3.0, abs=0.1)
