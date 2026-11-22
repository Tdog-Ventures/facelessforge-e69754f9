# FacelessForge agent developer workflow (via MCP)

Reusable instructions for any MCP-capable coding agent (OpenCode, Claude Code, Codex,
Cursor) working on FacelessForge through the `facelessforge` MCP server.

Repository of record: `Tdog-Ventures/facelessforge-e69754f9`
checkout on the VPS: `/root/facelessforge-deploy` (= `/opt/facelessforge/deploy`).
Project scope and build conventions: see the repo `AGENTS.md`.

## Product boundaries (never cross)

- FacelessForge is this repo only. VideoForge lives at `/opt/videoforge`, EthinX Shortform
  at `/root/ethinx-html-deploy`; never port defects or fixes between them.
- Never alter customer billing entitlements to enable testing; use the authorised
  administrative testing paths (e.g. the verified admin credit lockout) instead.
- No publishing automation; no bypassing human approval for production writes.

## Standard diagnostic loop

1. **Orient** — `connection_status` then `ops_status`. Note deployed revision, container
   health, backend health, active work and mutation guards before touching anything.
2. **Frontend trace** — reproduce in the UI, note the failing route; the React dashboard is
   built to `backend/static/frontend/`.
3. **Backend evidence** — `ops_logs` with `lines: 200` (redacted, bounded). Correlate the
   timestamp of the user-visible failure with container log lines.
4. **Localise** — `backend/` (FastAPI: `external_api.py`, `generation.py`, `render.py`,
   `billing*.py`), operations bridge at
   `/opt/ethinx/facelessforge-ops/` (loopback 7012, source copies at
   `/opt/ethinx/facelessforge-ops-source/`).
5. **Scene/asset/voiceover defects** — asset search must originate from the actual scene
   context (short, specific terms); a cooking scene must never search "nature landscapes".
   Verify attachment persistence, scene status transitions, and that the renderer receives
   the intended assets. For voiceover, check provider config, auth, error handling and
   output compatibility (`external_audio.py`, `audio_mix.py`).
6. **Credits** — confirm presentation and backend enforcement (`billing.py`,
   `billing_dodo.py`); write-off only through the authorised admin flow.
7. **Fix minimally** — smallest safe modification; add a regression test next to the
   failing behaviour or under `backend/tests/`.
8. **Build** — frontend: `cd /root/facelessforge-deploy/frontend && npm run build`. The production
   container is rebuilt by the deploy pipeline, not by hand.
9. **Test** — `python3 -m pytest backend/tests -k <area>` and the connector tests
   (`python3 -m unittest discover -s tools/mcp/tests`).
10. **Commit** — never bundle unrelated changes; reference the defect; never commit
    secrets (`.env`, keys) — the vault files are outside Git.

## Rendering protocols

- Renders consume provider resources. `render_video` only with explicit operator
  authorisation, and only after scope confirm (script, duration).
- Job accepted ≠ video delivered. Poll `render_status`; verify the finished MP4 with
  ffprobe (audio stream present, matching duration) before any acceptance claim.
- Never automatically retry an ambiguous submission: reconcile via `render_status` first.
- Cancel stalled jobs with `cancel_render` rather than leaving provider spend running.

## Deployment protocols

- Confirm the target repository (`facelessforge-e69754f9`) and the exact verified commit
  SHA (40 lowercase hex characters; verify with `git rev-parse` in the container checkout).
- Check for active rendering work first — `ops_status` reports `active_work` verified by
  the idle guard.
- `ops_deploy` is unavailable on the production host at the moment: the guarded
  `deploy-revision.sh` helper is not installed in the bridge directory (source copies exist
  under `/opt/ethinx/facelessforge-ops-source/tools/ops/`). Writes fail closed — that is
  correct behaviour; never fake health by hand-editing containers.
- A 202 receipt is not a deployment. Poll `ops_task_status`; a `reconcile_required` status
  means stop and inspect the host; never resubmit automatically.
- Verify the deployed revision via `ops_status` (`checkout.head`, image
  `release-<short-sha>-…`) and the health endpoints (backend `:7001/api/health`,
  production `https://facelessforge.ethinx.solutions/api/health`).

## Status labels

Report `IMPLEMENTED`, `TESTED`, `DEPLOYED`, `VERIFIED`, `BLOCKED` only with evidence.
If a criterion cannot be demonstrated, label it incomplete and report the blocker.

## Bugs to cover with regression tests (historical defects)

A. Asset search relevance (scene-content-derived terms only).
B. Asset attachment persistence through to the renderer.
C. Voiceover provider compatibility.
D. Credit balance presentation + enforcement.
E. Short test render (authorised, reusable assets).
F. Deployment pipeline tests (build → health → deployed revision).
