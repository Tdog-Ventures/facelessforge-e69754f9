"""Integration regressions for the origin/master (PR #33) merge into the
Visual Director branch, plus the new API-spending / credit safeguards.

Proves that both feature sets survive together:
  * PR #33: duration-aware footage coverage, broadened queries, looped holds,
    Ollama connect-timeout + reachability fixes;
  * Visual Director: one visual change every 3 seconds, paged provider search,
    carried rejections, scene_queries, /visual-direction routes;
  * preflight accepts EITHER coverage rule;
  * configurable per-operation API limits, zero paid calls by default;
  * no fabricated credit balances, admin/unmetered status preserved.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import api_limits, asset_relevance, generation, render, scene_terms
from app.routes import api as api_routes


# ---------------------------------------------------------------- PR #33 survives
def test_pr33_files_and_helpers_survive_merge():
    backend = Path(__file__).resolve().parents[1]
    assert (backend / "app" / "scene_terms.py").is_file()
    assert (backend / "app" / "visual_query.py").is_file()
    assert (Path(__file__).resolve().parent / "test_auto_attach_coverage.py").is_file()
    for name in ("holds_cover", "plan_hold_durations", "broaden_stock_queries"):
        assert hasattr(scene_terms, name), name
    assert hasattr(asset_relevance, "fallback_is_usable")


def test_pr33_duration_aware_coverage_survives():
    holds = scene_terms.plan_hold_durations(28.833333, 5)
    assert len(holds) == 5
    assert sum(holds) == pytest.approx(28.833333, abs=0.01)
    assert min(holds) > 3
    assert scene_terms.holds_cover(10, [{"duration": 5}, {"duration": 5}]) is True
    assert scene_terms.holds_cover(10, [{"duration": 3}]) is False
    queries = scene_terms.broaden_stock_queries(
        {"narration_text": "Crack the eggs into the hot pan.",
         "search_terms": ["eggs scrambling frying pan"]},
        {"topic": "Every way to cook eggs", "niche": "cooking"},
        "eggs scrambling frying pan",
    )
    assert "kitchen cooking" in queries
    assert len(queries) == len({q.casefold() for q in queries})
    assert asset_relevance.fallback_is_usable({"source": "mock"}, "kitchen cooking") is False
    assert asset_relevance.fallback_is_usable(
        {"source": "pexels", "download_url": "u", "external_id": "x"}, "kitchen cooking") is False


def test_pr33_looped_holds_survive():
    video = render._ffmpeg_normalise_video(Path("short.mp4"), 14.4, Path("out.mp4"))
    assert video[video.index("-stream_loop") + 1] == "-1"
    still = render._ffmpeg_normalise_image(Path("still.jpg"), 14.4, Path("out.mp4"))
    assert still[still.index("-loop") + 1] == "1"


def test_pr33_ollama_timeout_and_reachability_fixes_survive():
    gen_src = inspect.getsource(generation._llm_json)
    assert "connect=2.0" in gen_src
    assert "_ollama_reachability_hint" in gen_src
    assert "host.docker.internal" in inspect.getsource(generation._ollama_reachability_hint)
    from app import visual_query
    vq_src = inspect.getsource(visual_query.generate_broll_keywords)
    assert "connect=2.0" in vq_src
    assert "OLLAMA_MODEL is not set" in vq_src


def test_generation_auto_merge_keeps_both_feature_sets():
    src = inspect.getsource(generation)
    assert "_ollama_reachability_hint" in src       # PR #33
    assert "_rewrite_to_word_budget" in src         # Visual Director
    assert "word_budget" in src
    assert "connect=2.0" in src


# ---------------------------------------------------------------- Visual Director survives
def test_visual_director_three_second_rule_and_paging_survive():
    assert render.MIN_SUBCLIP_SECONDS == 3.0
    assert render.MAX_SUBCLIP_SECONDS == 5.0
    src = inspect.getsource(api_routes._attach_scene_stock)
    assert "math.ceil(duration / 3)" in src       # 3-second target
    assert "page=page" in src                      # paged provider search
    assert "carried_rejections" in src             # resume without re-judging
    assert "broaden_stock_queries" in src          # PR #33 broadened queries
    assert "holds_cover" in src                    # PR #33 duration coverage
    assert "api_limits.current_limits" in src      # new safeguards

    sig = inspect.signature(api_routes._attach_scene_stock)
    assert "query_override" in sig.parameters      # Visual Director
    assert "used_external_ids" in sig.parameters   # PR #33 project-wide dedupe


def test_visual_director_routes_and_model_survive():
    from app import visual_director
    from app.models import AutoAttachRequest
    assert hasattr(visual_director, "resolve_directed_scene")
    assert hasattr(api_routes, "generate_visual_direction")
    assert hasattr(api_routes, "_run_directed_attach")
    assert "scene_queries" in AutoAttachRequest.model_fields
    paths = {getattr(route, "path", None) for route in api_routes.router.routes}
    assert "/api/projects/{project_id}/visual-direction" in paths


def test_coverage_status_and_hold_retiling_survive():
    assert api_routes._coverage_status(0, 10) == "failed"
    assert api_routes._coverage_status(2, 10) == "partial"
    assert api_routes._coverage_status(10, 10) == "attached"
    assert hasattr(api_routes, "_retile_scene_holds")


# ---------------------------------------------------------------- both rules in preflight
def _preflight_base():
    return (
        {"id": "p", "target_duration": 30, "selected_voiceover_asset_id": "v1",
         "selected_thumbnail_asset_id": "t1"},
        {"full_script": "eggs " * 40},
        [{"id": "s1", "scene_number": 1, "duration": 30, "start_time": 0, "end_time": 30}],
        [{"id": "t1", "asset_type": "generated_thumbnail"},
         {"id": "v1", "asset_type": "voiceover_audio", "duration": 30, "status": "selected"}],
    )


def _pacing(check):
    return next(c for c in check["checklist"] if c["key"] == "scene_assets")


def test_preflight_accepts_pr33_holds_and_visual_director_clip_count():
    project, script, scenes, base = _preflight_base()
    # PR #33: two 15s holds cover a 30s scene with fewer than 10 cuts.
    holds = [
        {"id": "a", "asset_type": "stock_video", "scene_id": "s1", "source": "pexels",
         "duration": 15, "status": "attached"},
        {"id": "b", "asset_type": "stock_video", "scene_id": "s1", "source": "pexels",
         "duration": 15, "status": "attached"},
    ]
    assert _pacing(render.validate_prerequisites(dict(project), script, scenes, {"id": "m"}, base + holds))["ok"] is True
    # Visual Director: ceil(30/3) = 10 distinct pictures, even with tiny stored durations.
    ten = [
        {"id": f"a{i}", "asset_type": "stock_video", "scene_id": "s1", "source": "pexels",
         "duration": 1, "status": "attached"}
        for i in range(10)
    ]
    assert _pacing(render.validate_prerequisites(dict(project), script, scenes, {"id": "m"}, base + ten))["ok"] is True
    # Neither rule met -> blocked.
    assert _pacing(render.validate_prerequisites(dict(project), script, scenes, {"id": "m"}, base))["ok"] is False


def test_legacy_scenes_without_ids_fail_closed():
    # Scenes without an id cannot be coverage-checked; preflight must not
    # grant render-readiness on unverifiable footage.
    project = {"target_duration": 30, "selected_voiceover_asset_id": "v1",
               "selected_thumbnail_asset_id": "t1"}
    script = {"full_script": "w " * 30}
    legacy = [{"scene_number": 1, "duration": 27.5, "end_time": 27.5}]
    assets = [{"id": "v1", "asset_type": "voiceover_audio", "duration": 27.2,
               "status": "selected"},
              {"id": "t1", "asset_type": "generated_thumbnail"}]
    check = render.validate_prerequisites(dict(project), script, legacy, {"id": "m"}, assets)
    assert _pacing(check)["ok"] is False


# ---------------------------------------------------------------- API spending safeguards
def test_api_limits_are_configurable_and_paid_gate_defaults_to_zero_in_tests():
    cfg = api_limits.current_limits()
    assert cfg.max_pages_per_query == 3
    assert cfg.max_judged_candidates == 48
    assert cfg.allow_paid_calls is False           # conftest forces zero paid calls
    assert api_limits.paid_calls_enabled() is False
    budget = api_limits.OperationBudget()
    assert budget.can_paid() is False
    assert budget.exhausted_reason == "paid_api_not_authorised"
    assert budget.paid_calls == 0 and budget.spend_usd == 0.0


def test_operation_budget_enforces_request_paid_and_spend_caps(monkeypatch):
    monkeypatch.setenv("FF_ALLOW_PAID_API_CALLS", "1")
    monkeypatch.setenv("FF_MAX_API_REQUESTS_PER_OPERATION", "2")
    monkeypatch.setenv("FF_MAX_PAID_CALLS_PER_OPERATION", "1")
    monkeypatch.setenv("FF_MAX_API_SPEND_USD_PER_OPERATION", "0.01")
    monkeypatch.setenv("FF_ESTIMATED_USD_PER_PAID_CALL", "0.01")
    budget = api_limits.OperationBudget()
    assert budget.take_request() is True
    assert budget.take_request() is True
    assert budget.take_request() is False
    assert budget.exhausted_reason == "api_request_limit"
    paid = api_limits.OperationBudget()
    assert paid.can_paid() is True
    paid.record_paid()
    assert paid.can_paid() is False
    assert paid.exhausted_reason in ("paid_call_limit", "api_spend_limit")
    assert paid.snapshot()["paid_calls"] == 1


def test_live_key_without_authorisation_makes_zero_paid_calls(monkeypatch):
    monkeypatch.setenv("FF_ALLOW_PAID_API_CALLS", "0")
    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "live-looking-key")
    constructed = []

    class PaidClient:
        def __init__(self, *args, **kwargs):
            constructed.append("genai.Client")
            raise AssertionError("paid client must not be constructed")

    monkeypatch.setattr("google.genai.Client", PaidClient, raising=False)
    verdict = asyncio.run(asset_relevance.judge_frame(
        b"\xff\xd8fake", "image/jpeg", "narration", "coffee cherries branch"))
    assert constructed == []
    assert verdict["accepted"] is False
    assert verdict["reason"] == "paid_api_not_authorised"


def test_bound_operation_budget_blocks_paid_calls_inside_auto_attach(monkeypatch):
    monkeypatch.setenv("FF_ALLOW_PAID_API_CALLS", "0")
    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "live-looking-key")
    constructed = []

    class PaidClient:
        def __init__(self, *args, **kwargs):
            constructed.append("genai.Client")
            raise AssertionError("paid client must not be constructed")

    monkeypatch.setattr("google.genai.Client", PaidClient, raising=False)
    budget = api_limits.OperationBudget()
    token = api_limits.bind_budget(budget)
    try:
        verdict = asyncio.run(asset_relevance.judge_frame(
            b"\xff\xd8fake", "image/jpeg", "narration", "coffee cherries branch"))
    finally:
        api_limits.unbind_budget(token)
    assert constructed == []
    assert verdict["reason"] == "paid_api_not_authorised"
    assert budget.paid_calls == 0 and budget.spend_usd == 0.0


def test_paid_gate_covers_planning_and_visual_qa(monkeypatch):
    from app import video_qa, visual_director

    monkeypatch.setenv("FF_ALLOW_PAID_API_CALLS", "0")
    with pytest.raises(visual_director.DirectorError):
        asyncio.run(visual_director.deepseek_choices([]))
    with pytest.raises(visual_director.DirectorError):
        asyncio.run(visual_director.gemini_choices([]))
    monkeypatch.setenv("VIDEO_QA_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "live-looking-key")
    evaluator = video_qa.configured_vision_evaluator()
    assert evaluator is not None
    with pytest.raises(video_qa.VisualContractError):
        evaluator("vid", [{"frame_ref": "f", "timestamp": 0.0, "path": "unused"}])


# ---------------------------------------------------------------- credits: no fabrication
@pytest.mark.asyncio
async def test_admin_unmetered_balance_is_preserved(monkeypatch):
    async def status(_user):
        return {"operator_unmetered": True, "credits": 0, "remaining": 0,
                "monthly": 150, "plan": "free", "can_generate": True}

    monkeypatch.setattr("app.billing.get_billing_status_for_user", status)
    payload = await api_routes.billing_credits(user={"id": "admin"})
    assert payload["operator_unmetered"] is True
    assert payload["remaining"] == 0


@pytest.mark.asyncio
async def test_billing_failure_never_fabricates_a_balance(monkeypatch):
    async def broken(_user):
        raise RuntimeError("primary billing source unavailable")

    monkeypatch.setattr("app.billing.get_billing_status_for_user", broken)
    fake = types.ModuleType("redis")

    class BrokenRedis:
        def __init__(self, *args, **kwargs):
            raise OSError("redis mirror unavailable")

    fake.Redis = BrokenRedis
    monkeypatch.setitem(sys.modules, "redis", fake)
    with pytest.raises(HTTPException) as exc:
        await api_routes.billing_balance(user={"id": "u1"})
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "billing_unavailable"
    assert "credits" not in exc.value.detail
    assert "remaining" not in exc.value.detail


@pytest.mark.asyncio
async def test_redis_mirror_never_claims_unmetered(monkeypatch):
    async def broken(_user):
        raise RuntimeError("primary billing source unavailable")

    monkeypatch.setattr("app.billing.get_billing_status_for_user", broken)
    fake = types.ModuleType("redis")

    class MirrorRedis:
        def __init__(self, *args, **kwargs):
            pass

        def hgetall(self, key):
            return {"credits": "7"}

    fake.Redis = MirrorRedis
    monkeypatch.setitem(sys.modules, "redis", fake)
    payload = await api_routes.billing_balance(user={"id": "u1"})
    assert payload["credits"] == 7
    assert payload["remaining"] == 7
    assert payload["operator_unmetered"] is False
    assert payload["balance_source"] == "redis_tenant_mirror"
