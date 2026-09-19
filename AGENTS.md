# FacelessForge Agent Guide

This file maps the active skills and conventions for the `facelessforge-deploy` project and its related runtime components.

## Project Scope

`facelessforge-deploy` is the deployable FacelessForge package. It contains:

- `backend/` — FastAPI backend (generation, TTS, storage, render, external API)
- `frontend/` — React dashboard
- `remotion-renderer/` — Remotion-based video renderer
- `patches/` — Hot-fix patches for backend modules
- Related runtime deployment: `/opt/videoforge` (VideoForge Optimizer, Celery pipeline)
- Related backend: `/root/the-studio` (Studio Bridge swarm)

## Skill Map

Load the relevant skill before working on a task. When scopes overlap, prefer the most specific one.

| Task Area | Skill | Location |
|-----------|-------|----------|
| General FacelessForge shipping, debugging, service restart rules | `facelessforge-ship` | `/root/.agents/skills/facelessforge-ship/SKILL.md` |
| ETHINX SOT compliance check (before any task) | `vidup` | `/root/.agents/skills/vidup/SKILL.md` |
| Remotion video creation / editing `FacelessForgeVideo` | `remotion-index` | `.agents/skills/remotion-index/SKILL.md` |
| Deep Remotion topics (animations, audio, captions, etc.) | official Remotion skills | `remotion-renderer/.agents/skills/` |
| Voiceover script writing / TTS prompts for ElevenLabs | `tts-prompter` | `.agents/skills/tts-prompter/SKILL.md` |
| The Studio swarm backend / bridge API | `studio` | `/root/the-studio/.agents/skills/studio/SKILL.md` |
| VideoForge (`/opt/videoforge`) bridge integration / restart | `videoforge-bridge` | `.agents/skills/videoforge-bridge/SKILL.md` |

## Build & Deploy Conventions

- **Frontend changes:** After any JSX change, rebuild and copy static assets:
  ```bash
  cd /root/facelessforge-deploy/frontend
  npm run build
  cp -r build/* /root/facelessforge-deploy/backend/static/frontend/
  ```
- **Backend changes:** Restart the service:
  ```bash
  sudo systemctl restart facelessforge
  ```
- **VideoForge changes:** Follow the restart procedure in `videoforge-bridge` skill.
- **Studio Bridge changes:** Restart with `sudo systemctl restart the-studio`.

## Environment Files

| File | Purpose |
|------|---------|
| `/opt/ethinx/deployment/secrets-vault/facelessforge-deploy.env` | **Live production secrets** loaded by `facelessforge.service` |
| `/opt/facelessforge/deploy/.env.facelessforge` | Repo env template loaded as a fallback/defaults by the service |
| `/root/facelessforge-deploy/.env.facelessforge` | Symlink to the repo env above |
| `/root/the-studio/deploy/env` | Studio Bridge runtime secrets |
| `/opt/videoforge/.env` | VideoForge runtime secrets + bridge integration |

The systemd unit loads env files in this order:
1. `Environment=MONGO_URL=...` fallback
2. `EnvironmentFile=/opt/facelessforge/deploy/.env.facelessforge`
3. `EnvironmentFile=/opt/ethinx/deployment/secrets-vault/facelessforge-deploy.env` (production values override defaults)
4. `Environment=PATH=...`

## Systems of Truth

- FacelessForge backend/frontend: this repo (`/root/facelessforge-deploy`)
- The Studio: `/root/the-studio/SPEC.md`
- VideoForge runtime: `/opt/videoforge/` (deployed from a separate repo; patch carefully)

## Critical Rules

1. Read `vidup` before starting any FacelessForge or ETHINX task.
2. Read `facelessforge-ship` for the full ship workflow and restart rules.
3. Never modify SACRED files in `/root/the-studio` (`SPEC.md`, `studio/types.py`, `studio/config.py`, `studio/agents/base.py`) unless explicitly asked.
4. Keep the VideoForge rollback path intact: `FF_LEGACY_PIPELINE=1` must still run the original stages 4–8.
5. The Studio Bridge smoke path must always work offline: `python3 tests/smoke_test.py`.

<!-- BEGIN TESTSPRITE AGENT SECTION (testsprite agent install codex) -->
<!-- testsprite-skill: testsprite-verify+testsprite-onboard v0.10.0 sha256:2347e3393180 -->
# TestSprite Verification Loop

After finishing a feature or fix in a TestSprite-tested repo, use the `testsprite`
CLI to run the relevant TestSprite tests against the change and inspect any failure
artifacts before reporting the work as done. Use whenever code has changed outside
docs/config and is about to be reported complete.

## When to run

Run after a feature or fix lands. Skip only for: docs-only edits, pure
build/config changes, or when the repo has no TestSprite project linked.

The CLI tests a deployed URL via `--target-url`, or a frontend test on this
machine via `--local <port>` (tunnels `localhost`/`127.0.0.1`/`::1` only; needs
a key with the `run:tunnel` scope — 403 there means mint a new key; a backend
test's target is baked into its code, so it can't use `--local`). If the user
named a tool (the CLI or the MCP), honor that even when both could apply.

## Core loop

### 1. Preflight

```bash
testsprite --version          # CLI installed?
testsprite auth status        # credentials valid?
```

If `--version` fails, tell the user to install the CLI and stop.
If `auth status` fails, tell the user to run `testsprite setup` and stop.

### 2. Find the project

In order: `$TESTSPRITE_PROJECT_ID` → `.testsprite/config.json` → `testsprite project list --output json`.

### 3. Run

```bash
# New frontend test from plan (most common)
testsprite test create --plan-from plan.json --run --wait \
  --target-url https://staging.example.com --timeout 600 --output json

# Existing test
testsprite test run <test-id> --target-url https://staging.example.com \
  --wait --timeout 600 --output json

# New backend test from Python assertion file
testsprite test create --type backend --name "Login rejects empty password" \
  --project <id> --code-file /tmp/test.py --run --wait --timeout 600

# Replay (cheaper than a fresh run — reuses saved test code)
testsprite test rerun <test-id> --wait --output json

# Backend tests sharing state: declare the dependency graph at create time;
# the wave engine orders runs (producers → consumers → teardown last)
testsprite test create --type backend --project <id> --code-file /tmp/login.py \
  --name "login issues an auth token" --produces auth_token
testsprite test create --type backend --project <id> --code-file /tmp/profile.py \
  --name "profile update accepts the token" --needs auth_token
testsprite test create --type backend --project <id> --code-file /tmp/cleanup.py \
  --name "fixture user is deleted" --category teardown

# Wave-ordered batch fresh run (BE tests, all or filtered)
testsprite test run --all --project <id> [--filter <substr>] \
  --wait --max-concurrency 4 --output json
```

**Key behaviors:**

- `--target-url` must be publicly reachable (no localhost / RFC1918) and must
  already have the change deployed (e.g. a CI preview deploy) — running earlier
  verifies the previous build, not your change. For a local-only change use
  `--local <port>` instead (frontend tests only).
- Backend `--code-file`: the runner executes the file top-to-bottom (not `pytest`), so **call your `test_*` function(s) at the end of the file** — a defined-but-uncalled test silently passes.
- Backend sandbox has only stdlib + `requests` + `pytest` + `numpy` + `scipy`. Test the API over HTTP with `requests`; do **not** `import` the project's own source modules or other packages (e.g. `torch`) — they aren't installed and the test won't run.
- `--wait` long-polls until terminal; don't wrap it in a retry loop. A
  `--local` timeout has nothing to resume — re-run with `--local`.
- Exit `0` = passed; `1` = failed/blocked; `7` = timeout (resume with `test wait <run-id>`).
- BE dependency flags (`--produces`/`--needs`/`--category`) are backend-only,
  repeatable, and **editable** — `test update` amends them, so never delete and
  recreate a test to change the graph. Don't hand-sequence `test run` calls to
  fake ordering; use `test run --all` so the engine passes captured variables
  between waves.
- A BE `test rerun` dispatches the whole producer/teardown closure, side effects
  included; `--skip-dependencies` reruns only the named test. If a producer failed
  in the same closure, the consumer's failure is starvation (missing token/fixture)
  — triage the producer first; it does not implicate your change.
- `create` and `--wait` output a `dashboardUrl` — point the user there to
  inspect a test or run.

### 4. On failure — download the artifact

```bash
testsprite test artifact get <run-id> --out ./.testsprite/runs/<run-id>/
```

Inspect the bundle (failing step, screenshots, root-cause hypothesis) before
deciding whether your change caused the failure.

### 5. Dry-run for learning

Every command works without credentials under `--dry-run`:

```bash
testsprite test run <test-id> --dry-run --output json
testsprite test create --plan-from plan.json --dry-run --output json
```

## Exit-code quick reference

| Code | Meaning                                           |
| ---- | ------------------------------------------------- |
| 0    | Success (passed)                                  |
| 1    | Failed / blocked / cancelled                      |
| 3    | Auth error                                        |
| 4    | Not found                                         |
| 5    | Validation error                                  |
| 6    | Conflict (already running)                        |
| 7    | Timeout — resume: `testsprite test wait <run-id>` |
| 11   | Rate limited (retriable)                          |
| 12   | Insufficient credits                              |

## Bootstrap (first-time setup)

```bash
npm install -g @testsprite/testsprite-cli
testsprite setup         # configure + verify + install agent skill in one shot
```

Verify your setup anytime: `testsprite auth status`.

Project with **no tests at all**? Seed a suite first — that's setup, not verification.
`testsprite test plan generate --project <id>` proposes cases and `test plan accept`
promotes them; or author plans by hand.

**First-time setup:** if this repo has no TestSprite tests yet, seed a *broad* first suite across its main user flows — not just one test — each with a concrete, observable assertion, before reporting setup as done.
<!-- END TESTSPRITE AGENT SECTION -->
