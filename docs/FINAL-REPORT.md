# FacelessForge Security Candidate — Final Approval Packet

**Prepared:** 2026-10-22
**Combined candidate branch:** `review/security-combined-20261022`
**Candidate source commit (code + tests):** `a58f237a21daf278dc20477453bbf4eca050a83c`
**Branch head including this packet:** current `HEAD` of `review/security-combined-20261022`
**Status:** Source approved for build; candidate container image pending.

---

## 1. Source Identity

| PR | Branch | Exact pushed SHA | Role |
|---|---|---|---|
| #19 | `review/combined-20261022` | `0ecca886ceadd12b0d305efb29a79d60f4988ef0` | Release-review base (reconciled shared-render + duration fixes) |
| #20 | `codex/security-external-release-gate` | `cf92afab66e029a7b280250bb1ea315134f06479` | Shared artifact-bound publish gate + CodeRabbit follow-up |
| #21 | `codex/security-manifest-paths` | `ca81319d8f33b9861833cb6ddcb939c849a75a3d` | Manifest read containment + reject client server paths |
| #22 | `codex/security-billing-grant-race` | `df7496741e460c6d928c2edda3e8e725dbebbdaf` | Atomic concurrent Dodo Mongo credit grants |
| **Combined** | `review/security-combined-20261022` | **`a58f237a21daf278dc20477453bbf4eca050a83c`** | Isolated integration of #19–#22 plus one test-alignment commit |

All SHAs are exact, pushed to `origin`, and verified by `git rev-parse`.

---

## 2. Build / Image Status

- **Candidate container image:** **pending**. No new candidate image has been built or verified from `a58f237`.
- **Production image identity:** the existing production image is identified by the deployed runtime, not by this packet. The backup tar of the pre-deployment runtime image is:
  - Path: `/root/ff_backups/pr19-prep-inventory-20261022T003201Z/runtime-image.tar`
  - SHA-256: `98518cb98ab574040668cabef28cfd1faff15211807b9cbca5491cf48f2ad80a`
  - Bytes: 1,619,773,952
- **Rollback source package:**
  - Path: `/root/ff_backups/pr19-prep-inventory-20261022T003201Z/release-config-source.tar.gz`
  - SHA-256: `78acf5311fc83045b4fadaedcf5386362d722f96f8032d7e702498bfeeae85a7`
  - Bytes: 17,871,832

> Correction from earlier checkpoint: the backup directory uses an underscore (`/root/ff_backups/...`), not a hyphen (`/root/ff-backups/...`).

---

## 3. Independent Publishing-Path Review

All four public YouTube upload channels now pass through one shared boundary in `backend/app/publish_gate.py`.

| Channel | Ownership enforced? | QA gate? | Artifact binding? | Notes |
|---|---|---|---|---|
| **External auto-upload** (`external_api.py` `_auto_upload_task`) | Project created for the API-key user; uses queued job ID | `assert_publishable(j)` then `resolve_approved_artifact(job=j, project_id=pid)` | Job's `output_path` or renderer-retained `{job_id}_verify.mp4` | No arbitrary directory scan; no mock-ID fallback. |
| **Operator manual upload** (`routes/youtube.py` `youtube_upload`) | `ensure_operator(user)` + project ownership check (`user_id`) | `find_approved_job` → `assert_publishable` → `resolve_approved_artifact` | Same job-bound artifact | Returns HTTP 409 on blocked/missing artifact. |
| **Forge pipeline upload** (`workers/forge_loop.py` `_youtube_upload`) | `ensure_operator_project(project)` | `find_approved_job` → `assert_publishable` → `resolve_approved_artifact` | Same job-bound artifact | URL download fallback removed per CodeRabbit; fails closed if local artifact missing. |
| **Autopost adapter** (`adapters/posters/youtube.py` `YoutubePoster.post`) | Caller (`trigger_autopost_for_project`) is admin-only; adapter re-binds to project | `find_approved_job` → `assert_publishable` → `resolve_approved_artifact` | Same job-bound artifact | `PublicationBlocked` re-raised distinctly from provider failure. |

`assert_publishable` now also rejects jobs whose `status` is not `completed`/`COMPLETED`, closing the “expired job” gap.

`find_approved_job` prefers the renderer-recorded `rendered_video_asset_id` and falls back to the newest completed job; the QA gate fails closed on whichever job is found.

---

## 4. CodeRabbit Findings Addressed

### PR #20 (publish gate)

| Finding | Resolution |
|---|---|
| `assert_publishable` should reject non-completed jobs | Added `status` check; tests added in `test_publish_gate.py`. |
| Forge `_youtube_upload` should use `resolve_approved_artifact` and remove `output_url` download fallback | Rewrote artifact resolution; removed URL fallback and temp-download code. |
| `test_security_exposure.py` should assert the artifact path passed to `MediaFileUpload` | Updated test to mock `build`/`MediaFileUpload`, reach the upload constructor, and assert the approved path (or verification-copy fallback) for allowed cases and no path for blocked cases. |

### PR #21 / #22
No actionable CodeRabbit comments. Architecture observations about legacy thumbnail reads and billing atomicity are retained as residual risks (see §7).

---

## 5. Test Results

### Final combined offline suite — commit `a58f237`
```
pytest backend/tests/ --ignore=backend/tests/backend_test.py
501 passed, 4 skipped, 0 failed
```
- The 4 skipped tests are pre-existing (optional Mongo integration / dependency cases).
- `backend_test.py` is an integration suite that requires a running server and `MONGO_URL`; it was not run offline.

### Affected regression subset
```
pytest backend/tests/test_publish_gate.py backend/tests/test_security_exposure.py backend/tests/test_media_providers_fail_closed.py backend/tests/test_forge_scheduler.py
67 passed, 0 failed
```

### Integration test alignment
`test_voiceover_deletion.py` was updated to seed asset documents directly because PR #21 now rejects client-supplied server paths at `create_asset`. The security property under test (deletion must not follow non-canonical paths) is preserved.

---

## 6. Billing Residual Assessment

PR #22 fixes the demonstrated Mongo concurrent-grant race (lost updates, duplicate first-account creation, repeated replays). The remaining gaps are architectural and documented in `docs/security-dodo-grant-race.md`.

### Can purchased credits be lost?
**Yes, in three ways:**
1. The Redis processed marker (`dodo:webhooks:processed:{webhook_id}`) is set **before** the Redis credit grant. A crash after the marker but before/during the pipeline leaves the webhook marked “processed” and suppresses a retry, so credits are never granted in Redis.
2. The Mongo mirror is best-effort and runs **after** the Redis grant. If the mirror fails or is interrupted after inserting a `subscriptions` doc with `credits_remaining: 0` but before the increment, `check_credits` (which reads `subscriptions` first and does not fall back) treats the account as exhausted. Redis may hold the credits, but generation is denied until manual repair.
3. The Redis marker expires after 24 hours. A Dodo replay after expiry would be re-processed.

### Can credits be granted repeatedly?
**Yes, in two ways:**
1. Different Dodo webhook IDs describing the same logical purchase are treated as distinct grant identities. If Dodo emits multiple IDs (e.g., `payment.succeeded` + `subscription.active`, or a retry with a fresh ID), each will grant credits.
2. Redis and Mongo are not transactional. A partial failure can leave Redis granted without a Mongo transaction marker; a later reconciliation or repair could re-apply the same grant to Mongo.

### Unresolved release blocker
These crash/replay gaps are **blocking for a bulletproof billing release**. They do not block the source-merge of the security fixes, but they do block confident deployment of Dodo billing unless the following preconditions are met:
1. Reconcile legacy duplicate Mongo accounts before applying the new unique partial indexes (already acknowledged in `security-dodo-grant-race.md`).
2. Confirm Dodo webhook semantics: one unique webhook ID per purchase and retries within the 24-hour window.
3. Have an operational reconciliation runbook (or a durable exactly-once workflow) to repair Redis/Mongo divergence and zero-balance subscription docs.

No production indexes or data were modified during this review.

---

## 7. Remaining Risks / Follow-ups

| Risk | Severity | Owner | Mitigation |
|---|---|---|---|
| Billing crash/replay gaps (lost or double grants) | High | Billing/platform | Reconcile duplicates; confirm Dodo semantics; implement durable exactly-once workflow or reconciliation runbook. |
| Legacy thumbnail reads before manifest validation (PR #21 architecture note) | Medium | Renderer/security | Apply containment/ownership checks to earlier legacy thumbnail resolution paths. |
| Auto-upload success persistence is not atomic with provider upload (CodeRabbit architecture note) | Medium | Publishing | Add idempotent success recording or duplicate-upload guard in a follow-up. |
| Candidate image build & verification | Blocking | Build/CI | Build image from `a58f237` and run integration tests before deployment. |
| Customer acceptance test | Not executed | QA/Owner | Execute the customer-test plan (see §9) against the built candidate on a non-production tenant. |

---

## 8. Protected-Code Preservation

No changes were made to:
- `security/entrypoint/requirements`
- `backend/app/qa.py`
- `backend/app/routes/youtube_oauth.py`
- `backend/app/render.py` credit/render-cost constants (`RENDER_CREDIT_COST`, `reverse_render_credits`, `STATIC_RENDERS`)

All changes are confined to the publishing boundary, manifest-path hardening, billing Dodo mirror, and their tests.

---

## 9. Deployment / Rollback / Customer-Test Plan

### Rollback
1. Stop candidate service.
2. Restore runtime image from `/root/ff_backups/pr19-prep-inventory-20261022T003201Z/runtime-image.tar` (sha256 `98518cb98ab574040668cabef28cfd1faff15211807b9cbca5491cf48f2ad80a`).
3. Restore source config from `/root/ff_backups/pr19-prep-inventory-20261022T003201Z/release-config-source.tar.gz` (sha256 `78acf5311fc83045b4fadaedcf5386362d722f96f8032d7e702498bfeeae85a7`).
4. Restart prior service.

### Customer test plan (unexecuted)
- Create a non-production project.
- Render a video and mark it QA-blocked (`deterministic_qa_blocked` / `current_step=qa_blocked`); verify all four upload channels refuse publication.
- Mark the same render release-eligible; verify all four channels can upload the same approved artifact.
- Attempt to publish from a different completed job while a newer approved job exists; verify the newer approved artifact is used.
- No specific customer account is designated in this packet; assign a staging tenant before execution.

---

## 10. Approval Recommendation

**Approve for build, not for production deployment.**

The combined source at `a58f237a21daf278dc20477453bbf4eca050a83c` closes the sibling publishing bypasses, passes the offline regression suite (501/0), and addresses the actionable CodeRabbit findings. It should be built into a candidate image and subjected to integration / customer tests. Billing crash/replay gaps remain an unresolved release blocker for the Dodo payment flow.
