# FacelessForge Security Candidate — Final Approval Packet

**Prepared:** 2026-10-22
**Combined candidate branch:** `review/security-combined-20261022`
**Candidate source commit (code + tests):** `34fa7d61bedbd30eecae2d32248cbcb041d96906`
**Branch head including this packet:** current `HEAD` of `review/security-combined-20261022`
**Candidate image:** `facelessforge:candidate-34fa7d6` / `sha256:8bbdf0dd07305e6da880d2c1d4f1e8ae0bb919df55b59a6c92aad7fec7418d1c`
**Status:** Candidate built and verified offline; deployment blocked only by duplicate-account reconciliation.

---

## 1. Source Identity

| PR | Branch | Exact pushed SHA | Role |
|---|---|---|---|
| #19 | `review/combined-20261022` | `0ecca886ceadd12b0d305efb29a79d60f4988ef0` | Release-review base (reconciled shared-render + duration fixes) |
| #20 | `codex/security-external-release-gate` | `cf92afab66e029a7b280250bb1ea315134f06479` | Shared artifact-bound publish gate + CodeRabbit follow-up |
| #21 | `codex/security-manifest-paths` | `ca81319d8f33b9861833cb6ddcb939c849a75a3d` | Manifest read containment + reject client server paths |
| #22 | `codex/security-billing-grant-race` | `f8ef4103690e185f8b2002616282a106a4208296` | Atomic concurrent Dodo Mongo grants + durable payment_id fulfillment |
| **Combined** | `review/security-combined-20261022` | **`34fa7d61bedbd30eecae2d32248cbcb041d96906`** | Isolated integration of #19–#22 plus test alignment + docs |

All SHAs are exact, pushed to `origin`, and verified by `git rev-parse`.

GitHub links:
- PR #19: https://github.com/Tdog-Ventures/facelessforge-e69754f9/pull/19
- PR #20: https://github.com/Tdog-Ventures/facelessforge-e69754f9/pull/20
- PR #21: https://github.com/Tdog-Ventures/facelessforge-e69754f9/pull/21
- PR #22: https://github.com/Tdog-Ventures/facelessforge-e69754f9/pull/22
- Combined branch: https://github.com/Tdog-Ventures/facelessforge-e69754f9/tree/review/security-combined-20261022

---

## 2. Build / Image Status

- **Candidate container image:** built and verified.
  - Docker tag: `facelessforge:candidate-34fa7d6`
  - Image ID: `sha256:8bbdf0dd07305e6da880d2c1d4f1e8ae0bb919df55b59a6c92aad7fec7418d1c`
  - Size: 1,581,743,986 bytes
  - Exported tar.gz: `/root/ff_backups/pr19-prep-inventory-20261022T003201Z/candidate/facelessforge-candidate-34fa7d6.tar.gz`
  - Tar.gz SHA-256: `87f8769c2f399e4215d83fc2456a93aa2c54005c0688b42f88d160f01b26dc31`
  - Bytes: 514 MB
- **Production rollback image** (pre-deployment):
  - Path: `/root/ff_backups/pr19-prep-inventory-20261022T003201Z/runtime-image.tar`
  - SHA-256: `98518cb98ab574040668cabef28cfd1faff15211807b9cbca5491cf48f2ad80a`
- **Rollback source package:**
  - Path: `/root/ff_backups/pr19-prep-inventory-20261022T003201Z/release-config-source.tar.gz`
  - SHA-256: `78acf5311fc83045b4fadaedcf5386362d722f96f8032d7e702498bfeeae85a7`

> Backup directory uses an underscore: `/root/ff_backups/...`

---

## 3. Independent Publishing-Path Review

All four public YouTube upload channels route through `backend/app/publish_gate.py`.

| Channel | Ownership enforced? | QA gate? | Artifact binding? | Notes |
|---|---|---|---|---|---|
| **External auto-upload** (`external_api.py`) | API-key user project | `assert_publishable` + `resolve_approved_artifact` | Job's own `output_path` or `{job_id}_verify.mp4` | No arbitrary scan; no mock-ID fallback. |
| **Operator manual upload** (`routes/youtube.py`) | `ensure_operator` + `user_id` ownership | `find_approved_job` → gate → artifact | Same job-bound file | HTTP 409 on blocked/missing artifact. |
| **Forge pipeline** (`workers/forge_loop.py`) | `ensure_operator_project` | Same shared gate | Same job-bound file | URL download fallback removed. |
| **Autopost adapter** (`adapters/posters/youtube.py`) | Admin-only caller | Same shared gate | Same job-bound file | `PublicationBlocked` re-raised distinctly. |

`assert_publishable` rejects non-completed jobs. `find_approved_job` prefers `rendered_video_asset_id` and falls back to newest completed, with the QA gate failing closed.

---

## 4. CodeRabbit Findings Addressed

### PR #20
- `assert_publishable` rejects non-completed jobs.
- Forge `_youtube_upload` uses `resolve_approved_artifact`; URL fallback removed.
- `test_security_exposure.py` asserts the artifact path passed to `MediaFileUpload`.

### PR #22 billing follow-up
- Webhook acknowledgement now happens only after fulfillment succeeds.
- Durable Mongo fulfillment record keyed by `payment_id` enables crash recovery.
- Redis grant is atomic via Lua `HSETNX` + `HINCRBY` (exactly-once per payment).
- Mongo mirror uses a single atomic upsert+increment; no zero-balance window.
- Only `payment.succeeded` grants credits; `subscription.active` is ignored.

---

## 5. Test Results

### Final combined offline suite — commit `34fa7d6`
```
pytest backend/tests/ --ignore=backend/tests/backend_test.py
513 passed, 4 skipped, 0 failed
```
Same result reproduced inside the candidate container.

### Skip reasons (verified)
- `RealMongoDodoGrants` ×3 in `test_dodo_grant_atomicity.py`: require `DODO_TEST_MONGO_URL` (opt-in disposable Mongo).
- `test_configured_object_storage_endpoint_resolves` in `test_render_verification.py`: requires configured object-storage endpoint.

No failures concealed; no tests weakened.

### Billing behavioral regressions
`test_dodo_fulfillment.py` (12 tests) covers:
- normal grant once,
- `subscription.active` ignored,
- duplicate and different webhook IDs for the same payment,
- separate payments both grant,
- crashes after pending record / Redis grant / Mongo mirror,
- concurrent duplicate delivery,
- Redis/Mongo failure not acknowledged,
- no zero-balance subscription after initial grant.

### Independent review
A second reviewer verified billing correctness and recovery behavior:
- **Result:** 22 passed, 3 skipped, 0 failed.
- **Verdict:** approved for deployment contingent on duplicate-account reconciliation.
- **Minor notes:** stale comments fixed; low residual risks documented (Redis durability vs. flush, email tenant fallback edge case, refunds remain manual review).

---

## 6. Billing Residual Assessment

The demonstrated billing gaps are now closed in code:

| Gap | Fix |
|---|---|
| Crash after marking webhook processed loses grant | Acknowledgement + webhook-id marker set only after fulfillment; durable `ff_dodo_fulfillments` record keyed by `payment_id` enables retry-safe recovery. |
| Interrupted Mongo mirror leaves zero-balance subscription | Single atomic upsert (`$setOnInsert` defaults + `$inc` credits) removes the zero-balance window. |
| Different webhook IDs / late replays grant twice | Stable `payment_id` grant identity + atomic Redis HSETNX + Mongo transaction marker; `subscription.active` no longer grants. |

### Remaining deployment prerequisite
Legacy duplicate Mongo accounts must be reconciled **before** the new unique partial indexes are created. This is a supervised data migration, not a code change.

- Read-only inventory script: `scripts/inventory_dodo_duplicate_accounts.py`
- Reconciliation plan: `docs/dodo-duplicate-account-reconciliation.md`

No production indexes, accounts, balances, or payment records were modified.

---

## 7. Remaining Risks / Follow-ups

| Risk | Severity | Owner | Mitigation |
|---|---|---|---|---|---|
| Duplicate Mongo accounts block index creation | Blocking (deploy prereq) | Operator/DBA | Run inventory script, approve reconciliation plan, execute during maintenance window. |
| Redis flush/eviction loses fulfillment hash while Mongo pending | Low | Platform | Startup reconciliation from `ff_dodo_fulfillments` (future enhancement). |
| Email-based tenant fallback can split Redis/Mongo view | Low | Billing | Ensure checkout sets `custom_data.tenant_id` for all Dodo sessions. |
| Refunds/disputes require manual review | Low | Billing ops | Existing `ff_billing_reviews` workflow preserved. |
| Legacy thumbnail reads before manifest validation | Medium | Renderer/security | Apply containment to earlier thumbnail resolution paths in a follow-up. |
| Auto-upload success not atomic with provider upload | Medium | Publishing | Add idempotent success recording or duplicate-upload guard in a follow-up. |
| Customer acceptance test | Not executed | QA/Owner | Execute non-production test plan before full rollout. |

---

## 8. Protected-Code Preservation

No changes to:
- `security/entrypoint/requirements`
- `backend/app/qa.py`
- `backend/app/routes/youtube_oauth.py`
- `backend/app/render.py` credit/render-cost constants

All changes are confined to publishing boundary, manifest hardening, billing Dodo fulfillment, and tests/docs.

---

## 9. Deployment / Rollback / Customer-Test Plan

### Rollback
1. Stop candidate service.
2. Restore image from `/root/ff_backups/pr19-prep-inventory-20261022T003201Z/runtime-image.tar` (sha256 `98518cb...`).
3. Restore source config from `/root/ff_backups/pr19-prep-inventory-20261022T003201Z/release-config-source.tar.gz` (sha256 `78acf5...`).
4. Restart prior service.

### Pre-deployment steps
1. Run `scripts/inventory_dodo_duplicate_accounts.py` against production (read-only).
2. Execute the reconciliation plan in `docs/dodo-duplicate-account-reconciliation.md` if duplicates exist.
3. Confirm no duplicate `tenant_id` in `ff_credits` and no duplicate `user_id` in `subscriptions`.

### Customer test plan (unexecuted)
- Create a non-production project.
- Render and QA-block it; verify all four upload channels refuse publication.
- Mark release-eligible; verify all four channels can publish the same approved artifact.
- Attempt to publish an older completed job while a newer approved job exists; verify the newer artifact is used.
- Complete a Dodo test purchase; verify exactly one credit grant, idempotent replays, and `check_credits` reflects the balance.
- Assign a staging tenant before execution; no production account assumed.

---

## 10. Approval Recommendation

**Approve for deployment after duplicate-account reconciliation.**

The combined source at `34fa7d61bedbd30eecae2d32248cbcb041d96906` closes the sibling publishing bypasses and the demonstrated Dodo billing gaps. The offline regression suite passes (513/4) both on the host and inside the built candidate image. The only remaining release blocker is the supervised Mongo duplicate-account reconciliation documented in `docs/dodo-duplicate-account-reconciliation.md`.
