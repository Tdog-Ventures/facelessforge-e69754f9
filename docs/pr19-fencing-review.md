# PR #19 — asset fencing candidate evidence

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
