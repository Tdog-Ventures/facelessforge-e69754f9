# Production reconciliation 2026-11-08 (ff-27a483e overlay)

Target: `27a483eee1765b215d5d1eb44521fd83ad0de128`
Preserved runtime overlay: **true** — this branch is the live production lineage (auto_attach_operations /
idempotency_key lifecycle) with the target commit's stock-query, relevance and scene-coverage work ported onto it.
It is not a pristine build of the target commit.

## Deployed image
`facelessforge-app:release-ff-27a483e-genfix-20261108`
`sha256:dc74c26532bcf518c3bb639d39f199ac25cc603de4f9f9628f9cdb25a9cb1dfa`
(built from `release-ff-27a483e-final-20261108` sha256:6e7104e1a4d892d6d4c0f1624566ec87fb7e63da953e1ccbda86d26013f6c657)

## Verification
- 43 focused tests: 32 visual/API (test_live_api_port, test_visual_selection_gate, test_video_qa)
  + 6 attachment behaviour + 5 script continuation envelope — all passing, 1 skip
  (lease-expiry: the live lifecycle has no project_lease/asset_commit fence).
- 29 legacy failures in the four generation/render files are identical to the pre-deploy baseline run
  (node-ID set diffed, IDENTICAL_FAILURE_SET).
- generation.py in both `/app/backend/app` and `/app/deploy/backend/app`:
  sha256 `a35731363d122993d921173299e20d27882c9d469ee826827b463432d01d7d6c`.

## Script-length defect fixed
Generator under-produced (248 words vs 280-308 for 120s) and the continuation correction asked for a fragment
while providers returned full rewrites, which were discarded. The too_short correction now requests
`{"continuation": "..."}` of exactly the shortfall, and a full-rewrite or narration echo is rejected rather
than appended. Bounds, retry limit and the too_long condensation path are unchanged.

## Live confirmation
Project `19ca42ba-8a00-4623-8dd9-b6f0842ae1a4` (120s) went DRAFT -> SCRIPT_GENERATED at 2026-11-08T04:12:25Z
with a 294-word script (floor 280, ceiling 308, within_bounds). Verified by calling the running
`generate_script_endpoint` handler internally with the project owner's existing record — not over HTTP,
and no credentials were created or exposed.

## Rollback
```
docker compose -p ff-continuity -f /opt/facelessforge/backups/create-project-theme-merged-20261107/docker-compose.create-project-theme-merged-20261107.yml up -d --no-deps --pull never app
```
Activation record: `/opt/facelessforge/backups/ff-genfix-activation-20261108T041048Z/activation-receipt.txt`
(previous image sha256:134e19cd0521640dbc0c61db1538432d64b963fb96cf4a46bbb2045217c4290a).
