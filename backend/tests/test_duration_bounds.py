"""Product duration is 5..3600s end to end, not 30s+ in the timing helpers."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.external_api import ExternalRenderRequest
from app.models import ProjectUpdate
from app.render import suggest_duration_autofit
from app.timing import (
    MIN_AUTOFIT_TARGET_SECONDS,
    TimingContract,
    suggest_fitted_duration,
    target_word_count,
)


def test_five_second_editorial_project_has_two_and_a_half_seconds_of_narration():
    timing = TimingContract.from_project({"target_duration": 5})

    assert timing.intro_seconds == 2.5
    assert timing.narration_seconds == 2.5
    assert timing.final_frames == 150
    assert target_word_count(timing.narration_seconds) == 6


def test_autofit_lower_bound_matches_product_minimum():
    assert MIN_AUTOFIT_TARGET_SECONDS == 5
    assert suggest_fitted_duration(voiceover_seconds=2.4, intro_seconds=2.5) == 5


def test_autofit_suggests_a_short_target_above_the_old_thirty_second_floor():
    suggestion = suggest_duration_autofit(
        {"target_duration": 5},
        {"duration": 7.2},
    )

    assert suggestion == {"from": 5, "to": 10, "voiceover_seconds": 7.2}


def test_external_api_accepts_five_seconds_and_rejects_four():
    request = ExternalRenderRequest(title="Short", script="Ten chars.", target_duration=5)
    assert request.target_duration == 5

    with pytest.raises(ValidationError):
        ExternalRenderRequest(title="Short", script="Ten chars.", target_duration=4)


def test_project_update_accepts_five_seconds_and_rejects_four():
    assert ProjectUpdate(target_duration=5).target_duration == 5

    with pytest.raises(ValidationError):
        ProjectUpdate(target_duration=4)
