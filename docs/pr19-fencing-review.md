# PR #19 — asset fencing candidate evidence

## Latest follow-up — 5–3600-second timing contract resolved

Continues `c82560ec1130fbb04895ba69f1db6698e9a02172`. The previous sole gate
failure is resolved. The obsolete
`test_short_voiceover_has_no_inbounds_fit` was replaced with behavioral coverage,
not skipped/xfail'd or reduced to an existence check:

- 19s narration fits an editorial 22s target: reserve 2.5s intro, leaving a 19.5s
  narration slot (about 2.56% tempo adjustment). Commercial projects reserve no
  intro; the same narration fits 19s. Tests also cover the 5s and 3600s endpoints.
- Exactly ±5% is inclusive. The new tests reproduced binary-float rejection of
  both endpoints; a shared decimal-duration comparison now applies the same policy
  to suggestions, preflight, voice generation and actual audio fitting. Immediate
  representable values outside the boundary are still rejected; no epsilon widens
  the accepted range. Offline tone tests verify exact fitted sample budgets.
- Integer rounding alone missed valid fits: 10s voice plus editorial intro fails
  at the rounded 12s target but fits 13s; 3600s voice can fit the 3600s cap. The helper
  now checks adjacent whole-second targets clamped to the agreed legal bounds.
- Truly impossible short/long fits, gaps between legal whole-second tempo windows,
  and non-finite measurements fail explicitly. Impossible explicit API fits write
  neither a new target nor a render job.
- Caller tests also exposed stale response timing: explicit auto-fit persisted the
  new target but returned preflight for the old one. The response now validates
  the fitted selection. Short-target retry remains idempotent, and manual scripts
  are not rewritten or their selected duration silently changed.

### Verification of this follow-up

Affected timing/voice-fit tests **and preserved fencing/accounting regressions**:

```bash
PYTHONPATH=backend STORAGE_DIR=/tmp/opencode/ff-review-candidate PYTHONDONTWRITEBYTECODE=1 timeout --kill-after=5s 90s /root/ff-review/venv/bin/python -m pytest backend/tests/test_voice_fit.py backend/tests/test_duration_autofit.py backend/tests/test_duration_bounds.py backend/tests/test_authoritative_timing.py backend/tests/test_caption_music_acceptance.py backend/tests/test_forge_fit_gate.py backend/tests/test_asset_commit_fencing.py backend/tests/test_auto_attach_assets.py backend/tests/test_render_mode_lease.py -q --tb=short -p no:cacheprovider
```

**131 passed**, 5 warnings, **7.36s**. The first six modules account for 74 timing /
voice-fit / audio / caller tests; the three preserved asset/render-lease modules
account for 57 passing regressions.

The required combined gate was run **after** the affected suite passed:

```bash
PYTHONPATH=backend STORAGE_DIR=/tmp/opencode/ff-review-candidate PYTHONDONTWRITEBYTECODE=1 timeout --kill-after=5s 240s /root/ff-review/venv/bin/python -m pytest backend/tests/test_*.py -q -p no:cacheprovider -o faulthandler_timeout=30
```

**406 passed, 1 skipped**, 9 warnings, **187.24s**. No failures. The existing skip
is retained; this follow-up adds no skip or xfail. Local logs:
`/root/ff-review/logs/candidate-timing-affected.log` and
`/root/ff-review/logs/candidate-timing-combined-gate.log`.

The pinned-commit [operator runbook](asset-commit-fencing.md) now explains how to
establish quiescence: exclude producers/retries, close old writer connections,
and drain server-side operations with full-privilege `$currentOp`/connection-log
evidence. It includes retained-pin recovery ownership/fence CAS, asset/account/ID
reconciliation and exact-pin release. If attribution or drainage cannot be proven,
the project stays pinned. No production recovery was performed. Automatic recovery
and transactional reader snapshots remain unimplemented.

Fencing/accounting mutation paths are preserved. This follow-up changes timing,
two timing-related API paths, audio fitting, tests and documentation. The independent
review below pertains to the earlier fencing candidate; no new independent review
is claimed for this timing follow-up. No merge, deployment, production restart,
paid generation/render or changes to VideoForge/Revenue Swarm were performed.
The live Notion fetch still returns a JavaScript shell; no SOT update/export is
claimed. The prior TestSprite linkage/credential limitation remains.

---

## Historical evidence — fencing candidate at c82560e

## Result and scope

Confirmed stale-holder defect fixed for auto-attach/top-up/replacement and manual
scene attachment. All previously uncommitted candidate changes are retained,
including paid replacement retries at zero balance, durable attachment charge IDs,
resumed top-up totals, render-mode leases/snapshots and upload/generation fixes.
This is a review candidate, not a merge/deployment approval; the combined gate is
still red for the named baseline failure below.

Mongo read-only inspection: **7.0.34**, topology **Single**, no replica set name,
`maxWireVersion: 21`. The design uses no multi-document transaction. A project
document CAS installs a non-expiring commit barrier under the current holder/fence;
acquisition on that same document refuses the barrier. Lookup remains revocable.
Stale callbacks cannot enter the mutation boundary. Mutation, debit, reversal,
rollback and operation-record writes are fenced. Cancellation drains outstanding
protected work. Unknown writes retain the pin and durable scene recovery evidence.
See [asset-commit-fencing.md](asset-commit-fencing.md) for protocol and recovery.

## Independent review

Reviewer ran in a fresh OpenCode process with explicit model/agent configuration,
avoiding the stale running Task-tool model. Before delegation, resolved `general`
configuration was verified as provider **openai**, model **gpt-6.1-sol**, variant
**medium**. The reviewer session's actual assistant records confirm exactly that
provider/model/variant; no reviewer subdelegation or alternate model was used.

Session: `ses_e9f24eadcffeJ312dOKlcaehfZ`. Input was a focused asset API diff plus
the helper, lease, relevant tests and recovery policy, not the historical handoff.

- Initial verdict: ownership-transfer invariant holds for the intended reviewed
  manual/auto-attach writers. Reviewer independently ran **45 passing tests**.
- One **P2** accounting finding: a failed resumed top-up could delete already-paid
  carried assets without reversing their earlier charge. The reviewer reproduced
  both a transient paid-ID read error at zero balance and mixed paid/unpaid carried
  rows after another project spends the account balance.
- Correction: both carried-accounting failure branches preserve all previously
  committed carried rows. This call's partial debit reversal is still idempotent;
  same-key retry can finish outstanding IDs without losing the paid selection.
- Re-review: **finding resolved; no new correctness or recovery defect found in
  the focused correction**. Reviewer independently ran the two new regressions
  plus the lost-ack/recovery test: **3 passed**. Its original ownership-transfer
  verdict is unchanged. Review was scoped to asset fencing/accounting, not a new
  review of every preserved render/generation/frontend change.

## Verification

### Affected asset/render regressions

```bash
PYTHONPATH=backend STORAGE_DIR=/tmp/opencode/ff-review-candidate PYTHONDONTWRITEBYTECODE=1 timeout --kill-after=5s 30s /root/ff-review/venv/bin/python -m pytest backend/tests/test_asset_commit_fencing.py backend/tests/test_auto_attach_assets.py backend/tests/test_render_mode_lease.py -q --tb=short -p no:cacheprovider
```

**57 passed**, 3 warnings, 1.58s. New fencing module includes **21 tests** covering
lookup and commit-CAS takeover, current-holder completion, same/different keys,
asset/account/operation preservation, replay, manual attach, protected deletion /
debit / reversal / rollback, cancellation, lost acknowledgement and quiesced recovery.

### Actual standalone Mongo probe

```bash
PYTHONPATH=.:backend STORAGE_DIR=/tmp/opencode/ff-review-candidate PYTHONDONTWRITEBYTECODE=1 timeout --kill-after=5s 25s /root/ff-review/venv/bin/python scripts/verify_asset_fencing_mongo.py
```

**PASS**: stale callback fenced, current selection and balance preserved, current
commit/retry completed, and takeover denied during a real Mongo mutation barrier.
Only its randomly named disposable `ff_fence_probe_*` database was written, then
removed. No application database, HTTP endpoint or paid provider was exercised.

### Required combined gate

```bash
PYTHONPATH=backend STORAGE_DIR=/tmp/opencode/ff-review-candidate PYTHONDONTWRITEBYTECODE=1 timeout --kill-after=5s 240s /root/ff-review/venv/bin/python -m pytest backend/tests/test_*.py -q -p no:cacheprovider -o faulthandler_timeout=30
```

Final result: **372 passed, 1 skipped, 1 failed**, 9 warnings, **190.65s**.
Earlier checkpoint: 351 passed, 1 skipped, 1 failed. There are 21 new passing tests.

Failure: `backend/tests/test_voice_fit.py::test_short_voiceover_has_no_inbounds_fit`.
It asserts `suggest_fitted_duration(voiceover_seconds=19.0, intro_seconds=2.5) is None`
under its old **30..3600** bound assumption. `timing.py` now has
`MIN_AUTOFIT_TARGET_SECONDS = 5`, explicitly required by `test_duration_bounds.py`.
The function rounds 21.5 to **22**, within the 5-second minimum and 5% tempo policy.
The unchanged baseline at `6ad6014` reproduces the same assertion failure in a
bounded individual test (**1 failed**, 2.00s). This is a duration-policy/test-contract
conflict, not a fencing failure. It remains unresolved; the gate is **not green**.
No assertion was weakened or marked xfail to conceal it.

### Other checks

- `git diff --check`: passes.
- Candidate frontend `CI=false npm run build`: succeeds; `main.1aadf1d1.js`,
  311.81 kB gzip. Existing hook dependency warnings remain.
- `backend/tests/backend_test.py`: not run; it writes/deletes through live HTTP.
- TestSprite CLI 0.10.0 is installed, but auth reports an invalid/revoked key;
  no `.testsprite/config.json` is linked. No TestSprite verification is claimed.
- Notion SOT URL fetch returned only a JavaScript shell. Live SOT read/update,
  changelog and export could not be completed; this document is local evidence,
  not a claimed SOT export.

## Remaining blockers / limitations

1. Combined gate remains red for the specifically reproduced voice-fit contract
   mismatch above.
2. Standalone-Mongo safety trades crash availability for consistency: a killed
   process or uncertain write can pin a project indefinitely. Command quiescence,
   operator reconciliation using the stored intent, and exact-barrier CAS are
   required. There is no automated recovery or atomic in-progress reader snapshot.
3. TestSprite credentials and live Notion SOT access are unavailable.

No merge, deployment, production restart/static copy, paid generation/render or
changes to VideoForge/Revenue Swarm were performed.
