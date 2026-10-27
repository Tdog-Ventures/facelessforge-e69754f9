# Isolated Dodo verification — 25 October 2026

Starting point: PR #22 at f8ef4103690e185f8b2002616282a106a4208296.
Tests ran in a fresh local source snapshot, with production environment loading
disabled and no production credentials or provider requests.

## Reproduced before correction

- Mongo 7.0.14 rejected both first-account and existing-account concurrent
  grants with error 40: overlapping $setOnInsert/$inc credit fields.
- Three retry tests showed a pending payment could use a different credit
  allowance/product or be presented for another tenant.
- Redis 6.2.14 retained payment markers after failed increments caused by a
  wrong account type, non-integer balance or integer overflow.
- The read-only inventory CLI failed for both a database-qualified URI
  (database truth-value testing) and a URI without a default database.

## Corrections

- Leave incremented credit fields out of $setOnInsert.
- Recover from the stored payment contract and reject tenant reassignment
  before either pending or completed payment handling.
- Remove a newly claimed Redis payment marker if HINCRBY fails, allowing a
  repaired account to retry. This does not establish cross-store transactions.
- Select the inventory database with an explicit fallback; include transaction
  marker history in the report.
- Replace unsafe duplicate-balance summation and optional write pausing in the
  migration instructions with evidence-based reconciliation and drained writes
  through explicit index verification.

## Fresh results

The focused billing suite: **35 passed, zero failed, zero skipped**.
Real Mongo 7.0.14 and real Redis 6.2.14 were used for the opt-in integration tests.
A cross-store test interrupted the second Mongo account update after Redis and
the first Mongo account were credited, then retried with a changed allowance.
Redis, both Mongo accounts and the audit record remained one original grant.

A combined compatibility snapshot with PR #21 at
ca81319d8f33b9861833cb6ddcb939c849a75a3d passed **65 focused checks**:
35 billing checks and 30 input/path/HTTP checks. This is not the whole repository
suite, a real payment test or a production rendering test. Warnings concern
existing deprecated multipart, TestClient/AnyIO and Pydantic configuration APIs.

## Reproduce

Use an isolated virtual environment with the repository's relevant pinned
dependencies (including FastAPI 0.110.1, Starlette 0.37.2, Motor 3.3.1,
PyMongo 4.5.0 and Pydantic 2.12.5), pytest, pytest-asyncio and redis.
Provide standalone test executable paths in DODO_TEST_MONGOD and
DODO_TEST_REDIS_SERVER, then run:

```bash
python scripts/run_dodo_isolated_checks.py
```

The runner starts its own Mongo on loopback with fresh temporary data, and the
Redis fixture starts a separate loopback process per test. Both are stopped
after use. It does not connect to the application's configured stores.

## Remaining release conditions

Historical payment cutover, duplicate-account reconciliation and verified
production indexes remain deployment prerequisites. Redis loss/restart recovery
is not solved by these patches. No real payment, live entitlement, provider
render, merge, deployment, production data change or scheduled publication was
performed. The earlier external preflight report could not be retrieved because
the existing executor connector timed out; its reported 34-test result is not
used as evidence for this correction.
