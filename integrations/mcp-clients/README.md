# FacelessForge MCP — cross-client connection templates

One reusable stdio MCP connector (`<REPO_PATH>/tools/mcp/server.py`, Python 3.12+ standard
library only) serves every compliant MCP client. No per-client backend logic is duplicated:
all four clients run the same server, which calls the existing FacelessForge backend and the
owner-only operations bridge of the deployed platform.

Repository of record: `Tdog-Ventures/facelessforge-e69754f9`.
Production origin: `https://facelessforge.ethinx.solutions`.

## Tools (9)

| Tool | Kind | Effect |
|---|---|---|
| `connection_status` | read | backend reachability, health, credential presence (no values) |
| `render_status` | read | one render job (job_id) |
| `ops_status` | read | container health, deployed revision, guards, active work |
| `ops_logs` | read | redacted container log tail, 20–200 lines |
| `ops_task_status` | read | one ops deploy/restart task by UUID |
| `render_video` | write | submit one render (consumes provider credits; receipt ≠ completed video) |
| `cancel_render` | write | cancel one render job |
| `ops_restart` | write | guarded production restart (queued receipt ≠ completed restart) |
| `ops_deploy` | write | guarded production deploy of one verified 40-char commit SHA |

Default posture is read-only. Writes require either the locally installed credentials or
explicit operator authorisation, and are enforced server-side by the existing backend and
`facelessforge-ops` bridge (idle guard, guarded deployment helpers). Never automatically retry
`render_video` or any dispatch whose result is uncertain.

## Authentication model

| Destination | Auth header | Credential | Source of truth on the FacelessForge VPS |
|---|---|---|---|
| Render backend (default `http://127.0.0.1:7001`, off-box: `https://facelessforge.ethinx.solutions`) | `X-FacelessForge-Key` | `EXTERNAL_RENDER_API_KEY` | `/opt/ethinx/deployment/secrets-vault/facelessforge-deploy.env` |
| Ops bridge (loopback-only `http://127.0.0.1:7012`) | `Authorization: Bearer` | `FACELESSFORGE_OPS_KEY` | `/opt/ethinx/facelessforge-ops/ops.env` |

- Credentials are **never** written into client configuration or Git.
- The connector reads the process environment first, then root-owned `KEY=VALUE`
  files; only the three names above (`EXTERNAL_RENDER_API_KEY`, `FACELESSFORGE_OPS_KEY`,
  `EXTERNAL_RENDER_ENABLED`) are ever parsed, and no other vault value enters the process.
- On the FacelessForge VPS no extra file is needed: the two default paths above are used.
- Elsewhere, create `~/.secrets/facelessforge/mcp.env` (mode 0600) containing only the
  required keys and point `FACELESSFORGE_MCP_ENV_FILE` at it (full Unix paths, `:`-separated
  list allowed).

## Environment variables (all optional)

| Variable | Default | Notes |
|---|---|---|
| `FACELESSFORGE_API_URL` | `http://127.0.0.1:7001` | HTTP only for loopback; remote origins require HTTPS; origin-only (no path/query/credentials); port 8000 always rejected; redirects refused |
| `FACELESSFORGE_OPS_URL` | `http://127.0.0.1:7012` | same URL rules; the ops bridge is loopback-only, so off-box clients can reach it only by running the connector on the host (SSH) |
| `FACELESSFORGE_MCP_ENV_FILE` | VPS defaults above | `:`-separated `KEY=VALUE` files; set-only override |
| `EXTERNAL_RENDER_API_KEY` / `FACELESSFORGE_OPS_KEY` / `EXTERNAL_RENDER_ENABLED` | – | direct values take precedence over files |

Security invariants enforced by the code: stdio transport only (no HTTP listener is
opened), no shell execution anywhere, bounded timeouts (10–20 s) and response caps
(1 MB), credential/body redaction in every returned string, ops writes fail closed when
guarded helpers are missing, and an uncertain mutation is never auto-retried.

## Client templates

Templates in this directory (copy values, keep other settings you already have):

- [`opencode.json`](opencode.json) — merge the `facelessforge` entry into your `mcp.servers`
- [`claude.mcp.json`](claude.mcp.json) — Claude Code `.mcp.json` / `claude mcp add` shape
- [`codex.config.toml`](codex.config.toml) — Codex CLI `~/.codex/config.toml` server block
- [`cursor.mcp.json`](cursor.mcp.json) — Cursor `~/.cursor/mcp.json` entry

### Render-only access from a different machine

Set `FACELESSFORGE_API_URL=https://facelessforge.ethinx.solutions` and provide
`EXTERNAL_RENDER_API_KEY`. HTTPS-only is enforced for remote origins. The ops tools will
not work off-box (loopback-only bridge); run the connector on the VPS instead, or
`ssh -L 7012:127.0.0.1:7012` so the tunnel address is loopback (the bridge key is still
required — never forward the port publicly).

## Connection testing

```sh
cd <REPO_PATH>
python3 tools/mcp/verify_connection.py          # discovery + live connection_status
python3 -m unittest discover -s tools/mcp/tests # 15 offline contract tests
```

OpenCode: `opencode mcp list` → expect `✓ facelessforge  connected`.
Claude Code: `claude mcp list`.
Cursor: MCP indicator in Settings → MCP.
Codex: `/mcp` inside the CLI.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `EXTERNAL_RENDER_API_KEY is required` | credential not in env or file | check file mode 0600 / `FACELESSFORGE_MCP_ENV_FILE` path |
| `ops tools unavailable` error | `FACELESSFORGE_OPS_KEY` missing | add it to the env file; never commit it |
| `FacelessForge API connection failed` | backend down or URL misset | `connection_status` via another route; check `http://127.0.0.1:7001/api/health` on the host (or `https://facelessforge.ethinx.solutions/api/health`) |
| `HTTP 401/403` from render API | stale key | rotate in the secrets vault; restart affected client |
| ops writes fail "Required guarded helper is not installed" | bridge guard missing on host (currently true on the production VPS: deploy helper absent) | reinstall guarded helpers under `/opt/ethinx/facelessforge-ops/`; fail-closed is correct behaviour |
| Tool names not visible | client caches | restart the client or reload config (`/reload` in OpenCode) |

Report status labels accurately: a queued ops task or render receipt is not a completed
operation; verify with `ops_task_status` / `render_status` and production health checks.
