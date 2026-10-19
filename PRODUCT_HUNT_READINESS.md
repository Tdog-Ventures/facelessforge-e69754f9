# Product Hunt Readiness — 2026-09-07

## Launch recommendation: BLOCKED

FacelessForge is not ready for a Product Hunt launch until a real, client-owned
ad has completed the grounded workflow below and been watched in full.

## Ready items

| Item | Evidence |
|---|---|
| Commercial grounding contract implemented | `backend/app/grounding.py` requires client, structured brief, verified CTA, format, provenance, client visuals, scene assignments, script duration, and semantic alignment. |
| Wrong-footage release gate | Medical, finance, hacking, coding, office, fashion, and unrelated-cleaning labels reject commercial scene assignments. No stock top-up or caption-card fallback is allowed for a commercial render. |
| Short-form script guard | Automated Effective Wash-style 15-second brief produced 26 words; unit gate permits 21–42 words and rejects 147 words. |
| 9:16 commercial render default | Renderer defaults to 1080×1920; commercial render has no inherited YouTube intro and rejects an encoded duration more than one second from the requested duration. |
| Asset provenance and review records | Client upload endpoint hashes supplied files and records provenance; render writes an asset manifest and timestamped scene review report. A completed commercial render remains `release_eligible=false` until every scene has a timestamped human approval. |
| Local automated validation | `backend/venv/bin/python -m pytest -q backend/tests/test_grounding.py backend/tests/test_video_qa.py` passed: **26 tests**. Frontend production build completed. |

## Release-blocking evidence

| Blocker | Owner / action | Exact evidence |
|---|---|---|
| Genuine Effective Wash asset package and verified CTA are absent | Client owner: provide before, active-cleaning, after, logo/brand files and verify the destination/phone. | No client package was supplied in this task. A commercial project correctly cannot pass preflight without it. |
| No client-grounded visual end-to-end proof | Product owner: create 15s project, upload/assign real assets, render, watch complete MP4, submit timestamped review. | Latest local render: `backend/static/renders/cff58c78-9086-4ce2-befd-797b29ab9c6f/df6fafca-86f2-494b-ba37-74b4aa77ece5.mp4`; ffprobe: 61.279s, 1920×1080. Visual contact-sheet review found computer/coding, office and finance-chart imagery. |
| New code is not deployed | Release owner: review dirty worktree, deploy a controlled preview, then exercise the real workflow. | Live `facelessforge.service` is active on 127.0.0.1:8081 since 2026-09-07 04:06 UTC; it currently serves `main.8f0ed4bd.js`, not this build (`main.b499d064.js`). No restart/copy was performed. |
| Production configuration/documentation drift | Platform owner: reconcile systemd unit and canonical deployment guide; document rollback. | Live unit uses `/opt/facelessforge/deploy/backend/.env`; AGENTS.md specifies different environment-file ordering. Compose exposes 7001 while systemd serves 8081. |
| Customer journey has not been freshly proven | QA owner: run account/login, project/brief, upload, render, preview/download, billing/waitlist, mobile and safe-error checks against deployed preview. | TestSprite authentication is valid and project `833859c5-d1f8-4034-87af-739b19505cfb` exists, but its existing customer-flow tests predate this code and cannot validate an undeployed build. |
| Legal/public trust pages and monitoring are unproven | Product owner: add/verify Privacy, Terms, Contact, analytics and error monitoring, then capture URLs/screenshots. | Source audit found waitlist and dashboard analytics routes, but no confirmed Privacy/Terms/Contact page or configured error-monitoring provider. |

## Controlled release procedure

1. Preserve the current dirty worktree and record its commit/patch; do not overwrite unrelated changes.
2. Deploy the reviewed patch to a preview, not the active service; retain the current 04:06 UTC service state for rollback.
3. Run the client workflow with the real Effective Wash package only: 15s, 1080×1920, before → cleaning → finished branding/CTA.
4. Inspect the complete MP4 visually and aurally; submit timestamped review for all three scenes. Confirm the provenance report contains only client/brand sources.
5. Run TestSprite against that deployed preview, inspect any failure artifacts, and verify the complete customer workflow.
6. Only after all blockers close, copy frontend assets, restart `facelessforge`, health-check it, and retain the prior static build and service revision for rollback.

No Product Hunt publication, outbound email, affiliate contact, or production-access change was performed.
