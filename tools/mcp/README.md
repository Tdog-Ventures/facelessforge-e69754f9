# FacelessForge MCP connector

This connector belongs to `Tdog-Ventures/facelessforge-e69754f9`. It calls the existing
FacelessForge backend and the `facelessforge-ops` operations bridge directly, without
requiring VideoForge, Everygen, a new subscription, or a model provider change. Python 3.12
standard library only, stdio transport, MCP `initialize`/`tools/list`/`tools/call` lifecycle
(protocol revisions 2025-06-18, 2024-11-05 and 2025-11-25 for handshake downgrades).

## Available tools (9)

Read-only:

- `connection_status` — backend reachability + health payload, whether render/ops credentials are configured (booleans only; never values).
- `render_status` — one backend render job by `job_id`. A completed status is not proof that the MP4 passes media verification.
- `ops_status` — production status: container health, deployed revision, backend health, external render flags, mutation guards, active task.
- `ops_logs` — redacted container log tail via the bridge, `lines` 20–200.
- `ops_task_status` — one ops deploy/restart task by UUID. `queued`/`running` is not success; `reconcile_required` needs host inspection.

Controlled writes:

- `render_video` — submit one render from an existing script (title, script, optional target_duration 15–3600 s, voiceover_notes, stock_footage_terms). May consume provider credits. Never retry an ambiguous submission.
- `cancel_render` — cancel the existing cancellation endpoint for one job.
- `ops_restart` — queue one guarded restart with a 1–300 character reason; blocked while a render or deployment is active.
- `ops_deploy` — queue one guarded deployment of a full lowercase 40-character commit SHA. The 202 receipt is not deployment; poll `ops_task_status`, verify revision and health before reporting success.

Routes and headers match `backend/app/external_api.py` (`X-FacelessForge-Key`) and the
owner-only bridge in `service.py` (`Authorization: Bearer`). No automatic retries anywhere.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `FACELESSFORGE_API_URL` | `http://127.0.0.1:7001` | Backend origin. `http` only for loopback hosts; remote origins require HTTPS; origin-only; redirects refused; port 8000 forbidden. |
| `FACELESSFORGE_OPS_URL` | `http://127.0.0.1:7012` | Operations bridge origin; same URL rules. |
| `FACELESSFORGE_MCP_ENV_FILE` | `/opt/ethinx/deployment/secrets-vault/facelessforge-deploy.env` and `/opt/ethinx/facelessforge-ops/ops.env` | `:`-separated `KEY=VALUE` credential files. |
| `EXTERNAL_RENDER_API_KEY`, `FACELESSFORGE_OPS_KEY`, `EXTERNAL_RENDER_ENABLED` | – | Process-environment values override files. Only these three names are ever parsed. |

Credentials are never logged, returned or committed; every returned string is filtered so
credential values become `[REDACTED]`. Off-box clients set
`FACELESSFORGE_API_URL=https://facelessforge.ethinx.solutions` (render tools only — the ops
bridge is loopback-only).

## Install

Per-client templates live in `integrations/mcp-clients/` (OpenCode global registration is
already in place on the VPS at `~/.config/opencode/opencode.json`):

```json
"mcp": { "servers": { "facelessforge": {
  "type": "local",
  "command": ["python3", "<REPO_PATH>/tools/mcp/server.py"],
  "disabled": false } } }
```

OpenCode names the tools `facelessforge_<tool>`, exposed through Code Mode as
`tools.facelessforge.*`. Nothing else in the client configuration changes.

## Verification

```sh
cd <REPO_PATH>
python3 tools/mcp/verify_connection.py            # handshake + 9-tool discovery + live connection_status
python3 -m unittest discover -s tools/mcp/tests   # 15 offline contract tests
```

Test coverage: handshake/discovery, render contract, exact render routes, ops read routes,
ops write bodies, argument rejection (all tools), missing credentials (render and ops),
redirect credential refusal, port-8000 rejection for both origins, lifecycle ordering,
secret redaction, and regression that the `EXTERNAL_RENDER_ENABLED` value can never mangle
output.

Ask the agent to run `connection_status` and `ops_status` first. Then submit one approved
script with `render_video`, record the job ID, and poll with `render_status`. Verify the
MP4 independently (ffprobe plus a full FFmpeg decode) before treating it as accepted. Do
not retry a submission because it timed out — reconcile first.

## Limits

These are connector tests, not a successful production render. Deployments through the ops
bridge currently fail closed on the production host because the guarded deploy helper is
not installed under `/opt/ethinx/facelessforge-ops/` (the source copies live at
`/opt/ethinx/facelessforge-ops-source/tools/ops/`); restoring them is a host operation and
is not part of this connector. No production service is modified by this directory.
