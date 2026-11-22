# FacelessForge operations bridge

This is a separate, authenticated host-side operations service. It binds only to 127.0.0.1:7012 and is routed by the existing Cloudflare Tunnel at https://facelessforge.ethinx.solutions/api/ops. The renderer remains in its existing Docker container on 127.0.0.1:7001. No arbitrary commands, URLs, files or containers can be supplied by a caller.

## Routes

All routes require `Authorization: Bearer <FACELESSFORGE_OPS_KEY>`.

- GET /status: container/image metadata, checkout revision, local health and recorded active work.
- GET /logs?lines=80: redacted bounded tail, 20–200 lines.
- GET /tasks/<UUID>: persisted operation state/result.
- POST /restart with reason: asynchronous guarded restart; requires installed helper.
- POST /deploy with a full lowercase commit SHA: asynchronous activation of an already verified, root-owned prepared release; requires installed helper.

Task submissions are never retried automatically. An unresolved operation blocks later mutations, including after a service restart. Reconcile on the host rather than deleting the lock or resubmitting. There is no queue admission lock: an idle check covers recorded render/background/provider work at the time it runs, not unrecorded incoming requests.

## Prepared releases

`deploy-prepared.py` does not fetch, build or deploy arbitrary repository code. A release must already be staged under `/opt/ethinx/facelessforge-ops/releases/<SHA>/` with protected root ownership. Its manifest must include exact image IDs, expected base image ID, target commit, passing backend/frontend verification, protected compose SHA256 and artifact SHA256 proofs. It refuses changes to environment, mounts, networking and any setting other than the app image. Failed activation verification attempts restoration with the exact prior Compose file. `deploy-revision.sh <SHA> --check` performs preflight without activation. Artifacts/receipts distinguish target code from preserved runtime overlays.

## Verified state on 8 November 2026

The service and idle guard were installed and tested on Hetzner. Public unauthenticated status returns401; authenticated status/logs were verified by the host executor. Main app remained healthy, unchanged. Restart/deployment helpers are source-only and not installed. Candidate27a483ee is not activated: it needs behavioural reconciliation with live runtime fixes and frontend source provenance before tests and activation.

The owner-private ChatGPT Site has the matching MCP implementation and runtime-secret/encrypted-D1 credential support. Its actual connection_status still reports no backend/ops key configured. The executor refused automatic transfer to the private Site, so end-to-end MCP access remains blocked pending a supported secure credential setup. No key or token is in this repository.

## Tests

`python3 -m unittest -v test_service test_deploy_prepared`

Nine tests cover authentication, fixed routes, argument rejection, log redaction, missing helpers, active work refusal, check-only activation, configuration drift refusal and rollback. Tests mock Docker; they do not prove a production deployment. Bash syntax and Python compilation also pass.
