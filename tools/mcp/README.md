# FacelessForge MCP connector for OpenCode

This connector belongs to `Tdog-Ventures/facelessforge-e69754f9`. It calls the existing FacelessForge backend directly, without requiring VideoForge, Everygen, a new subscription, or a model provider change. Python 3.12 standard library only.

## Available tools

- `render_video`: submit an existing script and optional duration, voice notes and footage search terms.
- `render_status`: retrieve the real backend job status and download URL when available.
- `cancel_render`: cancel the specified render through the existing cancellation endpoint.

The routes and `X-FacelessForge-Key` header match `backend/app/external_api.py`. No automatic retries: a timed-out submission may already have created a chargeable job. Reconcile before resubmitting. This connector does not modify billing, publish to YouTube, enable scheduled delivery, deploy code, or assert that completed videos passed quality checks.

## Install in the actual FacelessForge checkout

Merge the `mcp.facelessforge` entry from `opencode.example.json` into the project's existing OpenCode configuration. Preserve all existing model/provider and MCP settings. Run OpenCode from that checkout, or use an absolute server path in `command` when configuring it globally. Do not replace an existing configuration with the example.

Start OpenCode with the existing `EXTERNAL_RENDER_API_KEY` environment variable available to its process. The connector reads it at runtime; do not put the key in a committed configuration. The backend must already have `EXTERNAL_RENDER_ENABLED=true` and the matching key. This change does not enable that backend feature itself.

`FACELESSFORGE_API_URL` defaults to `http://127.0.0.1:7001`, matching the recorded host port. Override it with the confirmed backend origin if different. Remote connections require HTTPS. The URL must be an origin with no path, credentials, query or fragment. All redirects are rejected, so credentials cannot be forwarded. Port 8000 is forbidden.

Check registration with `opencode mcp list`. Ask OpenCode to use `facelessforge` to inspect an existing job first. Then submit one approved script, record the returned job ID, and poll using `render_status`. Verify the returned MP4 independently with ffprobe and a complete FFmpeg decode before treating it as production acceptance. Do not retry a submission just because it timed out.

## Verification and limits

Run `python3 -m unittest discover -s tools/mcp/tests -v`. Eight local integration tests exercise the actual stdio subprocess and a controlled HTTP contract fixture: initialization/discovery, authenticated render, exact status/cancel routes, argument rejection, missing credentials, redirect rejection, port 8000 rejection, and lifecycle ordering.

These are connector tests, not a successful production render. OpenCode model execution, the deployed server version, credits, live jobs, downloadable production media and deployment remain unverified. The current repository references runtime modules absent from its tree, so it must not be assumed identical to the deployed server. No production service was changed during development.

Protocol compatibility: MCP 2025-06-18 and 2024-11-05 initialization/stdio lifecycle. Newer protocol revisions are not claimed. OpenCode v1-style local MCP configuration follows https://docs.opencode.ai/docs/mcp-servers/; check the installed major version before applying the example.
