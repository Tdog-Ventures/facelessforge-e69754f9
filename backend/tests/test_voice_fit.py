"""Voiceover/target fit policy: measured audio decides, target stays
authoritative, manual scripts are never rewritten, AI corrections are
bounded (initial + 2), attempts and actions persist, failures are explicit.
"""
import pytest

from app import render
from app.routes import api as api_routes
from app.timing import suggest_fitted_duration


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
        self.projects = FakeCollection([project])
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


def test_short_voiceover_has_no_inbounds_fit():
    # 19s narration against a 30s target cannot fit inside 30..3600 bounds.
    assert suggest_fitted_duration(voiceover_seconds=19.0, intro_seconds=2.5) is None
    assert render.suggest_duration_autofit(
        {"target_duration": 30}, {"duration": 19.0}) is None


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

    with pytest.raises(Exception) as exc:
        await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    detail = exc.value.detail
    assert exc.value.status_code == 400
    assert detail["code"] == "voiceover_length_mismatch"
    assert detail["measured_seconds"] == 18.0
    assert len(calls) == 1  # single TTS attempt, no correction
    stored = await db.scripts.find_one({"project_id": "p1"})
    assert stored["full_script"] == text  # manual text untouched
    fit = (await db.projects.find_one({"id": "p1"}))["voice_fit"]
    assert fit["attempts"] == 1 and fit["measured_seconds"] == 18.0
    assert db.projects.docs[0]["target_duration"] == 30  # target preserved


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

    with pytest.raises(Exception) as exc:
        await api_routes.generate_full_voiceover("p1", body=None, user=_user())
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "voiceover_fit_exhausted"
    assert len(tts_calls) == 3 and len(llm_calls) == 2  # initial + 2, never more
    fit = (await db.projects.find_one({"id": "p1"}))["voice_fit"]
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
async def test_explicit_autofit_is_idempotent(monkeypatch):
    voice = {"id": "v1", "project_id": "p1", "asset_type": "voiceover_audio",
             "duration": 32.6558125}
    project = _project(target_duration=30, selected_voiceover_asset_id="v1")
    db = FakeDB(project, _script("word " * 80), assets=[voice])
    monkeypatch.setattr(api_routes, "get_db", lambda: db)
    monkeypatch.setattr(render, "get_db", lambda: db)

    first = await api_routes.render_autofit_duration("p1", user=_user())
    assert first["autofit"] == {"from": 30, "to": 35, "voiceover_seconds": 32.6558125}
    writes = [c for c in db.projects.calls if c[2].get("target_duration") == 35]
    assert len(writes) == 1

    with pytest.raises(Exception) as exc:
        await api_routes.render_autofit_duration("p1", user=_user())
    assert exc.value.status_code == 400  # already fitting: no second write
    writes = [c for c in db.projects.calls if c[2].get("target_duration") == 35]
    assert len(writes) == 1


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
