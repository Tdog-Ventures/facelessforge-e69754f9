"""Configurable per-operation API request and spending limits.

All limits are environment-driven so an operator can tighten them without a
code change.  The test suite (``backend/tests/conftest.py``) sets
``FF_ALLOW_PAID_API_CALLS=0`` for the whole session: a live key that happens
to be present in the environment can never turn a test run into a paid API
run by accident.  Explicit authorisation is opt-in via
``FF_TEST_AUTHORISE_PAID_API_CALLS=1`` (for fully mocked provider tests) or
``FF_ALLOW_PAID_API_CALLS=1`` in a deployment.

Environment variables
---------------------
FF_MAX_API_REQUESTS_PER_OPERATION   int   default 12  (one scene-attach operation)
FF_MAX_STOCK_PAGES_PER_QUERY        int   default 3
FF_MAX_JUDGED_CANDIDATES_PER_SCENE  int   default 48
FF_MAX_PAID_CALLS_PER_OPERATION     int   default 300 (whole auto-attach request)
FF_MAX_API_SPEND_USD_PER_OPERATION  float default 3.00 (whole auto-attach request)
FF_ESTIMATED_USD_PER_PAID_CALL      float default 0.01
FF_ALLOW_PAID_API_CALLS             bool  default true (production)
"""
from __future__ import annotations

import contextvars
import os
from dataclasses import dataclass, field

_DEFAULTS = {
    "FF_MAX_API_REQUESTS_PER_OPERATION": 12,
    "FF_MAX_STOCK_PAGES_PER_QUERY": 3,
    "FF_MAX_JUDGED_CANDIDATES_PER_SCENE": 48,
    "FF_MAX_PAID_CALLS_PER_OPERATION": 300,
    "FF_MAX_API_SPEND_USD_PER_OPERATION": 3.00,
    "FF_ESTIMATED_USD_PER_PAID_CALL": 0.01,
}


def _env_int(name: str, *, low: int = 0, high: int = 1_000_000) -> int:
    try:
        value = int(os.getenv(name, str(_DEFAULTS[name])))
    except (TypeError, ValueError):
        value = int(_DEFAULTS[name])
    return max(low, min(high, value))


def _env_float(name: str, *, low: float = 0.0, high: float = 1e6) -> float:
    try:
        value = float(os.getenv(name, str(_DEFAULTS[name])))
    except (TypeError, ValueError):
        value = float(_DEFAULTS[name])
    return max(low, min(high, value))


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ApiLimits:
    max_requests_per_operation: int
    max_pages_per_query: int
    max_judged_candidates: int
    max_paid_calls_per_operation: int
    max_spend_usd_per_operation: float
    estimated_usd_per_paid_call: float
    allow_paid_calls: bool


def current_limits() -> ApiLimits:
    """Read limits fresh from the environment (so tests can monkeypatch them)."""
    return ApiLimits(
        max_requests_per_operation=_env_int("FF_MAX_API_REQUESTS_PER_OPERATION", low=1),
        max_pages_per_query=_env_int("FF_MAX_STOCK_PAGES_PER_QUERY", low=1, high=100),
        max_judged_candidates=_env_int("FF_MAX_JUDGED_CANDIDATES_PER_SCENE", low=1),
        max_paid_calls_per_operation=_env_int("FF_MAX_PAID_CALLS_PER_OPERATION"),
        max_spend_usd_per_operation=_env_float("FF_MAX_API_SPEND_USD_PER_OPERATION"),
        estimated_usd_per_paid_call=_env_float("FF_ESTIMATED_USD_PER_PAID_CALL"),
        allow_paid_calls=_env_flag("FF_ALLOW_PAID_API_CALLS", True),
    )


def paid_calls_enabled(limits: ApiLimits | None = None) -> bool:
    """Master switch. When false, no paid API call may leave the process."""
    cfg = limits or current_limits()
    return bool(
        cfg.allow_paid_calls
        and cfg.max_paid_calls_per_operation > 0
        and cfg.max_spend_usd_per_operation > 0
    )


@dataclass
class OperationBudget:
    """Counters and hard caps for one attach/plan operation."""

    limits: ApiLimits = field(default_factory=current_limits)
    requests: int = 0
    paid_calls: int = 0
    spend_usd: float = 0.0
    exhausted_reason: str | None = None

    def take_request(self) -> bool:
        if self.requests >= self.limits.max_requests_per_operation:
            self.exhausted_reason = "api_request_limit"
            return False
        self.requests += 1
        return True

    def can_paid(self, count: int = 1) -> bool:
        if not paid_calls_enabled(self.limits):
            self.exhausted_reason = "paid_api_not_authorised"
            return False
        if self.paid_calls + count > self.limits.max_paid_calls_per_operation:
            self.exhausted_reason = "paid_call_limit"
            return False
        projected = self.spend_usd + count * self.limits.estimated_usd_per_paid_call
        if projected > self.limits.max_spend_usd_per_operation + 1e-9:
            self.exhausted_reason = "api_spend_limit"
            return False
        return True

    def record_paid(self, count: int = 1) -> None:
        self.paid_calls += count
        self.spend_usd += count * self.limits.estimated_usd_per_paid_call

    def snapshot(self) -> dict:
        return {
            "requests": self.requests,
            "paid_calls": self.paid_calls,
            "spend_usd": round(self.spend_usd, 6),
            "exhausted_reason": self.exhausted_reason,
            "limits": {
                "max_requests": self.limits.max_requests_per_operation,
                "max_pages": self.limits.max_pages_per_query,
                "max_judged": self.limits.max_judged_candidates,
                "max_paid_calls": self.limits.max_paid_calls_per_operation,
                "max_spend_usd": self.limits.max_spend_usd_per_operation,
                "allow_paid_calls": self.limits.allow_paid_calls,
            },
        }


# The budget bound for the current async task. Using a ContextVar keeps the
# per-operation limits available at the actual network call sites without
# changing the signatures of the whole selection pipeline.
_current_budget: contextvars.ContextVar[OperationBudget | None] = contextvars.ContextVar(
    "ff_api_operation_budget", default=None
)


def bind_budget(budget: OperationBudget):
    return _current_budget.set(budget)


def unbind_budget(token) -> None:
    _current_budget.reset(token)


def current_budget() -> OperationBudget | None:
    return _current_budget.get()


def paid_call_permitted(count: int = 1) -> bool:
    """Gate every paid outbound call. Returns False with zero paid calls."""
    budget = current_budget()
    if budget is not None:
        return budget.can_paid(count)
    return paid_calls_enabled()


def record_paid_call(count: int = 1) -> None:
    budget = current_budget()
    if budget is not None:
        budget.record_paid(count)
