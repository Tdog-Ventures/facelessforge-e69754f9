# FacelessForge — First TestSprite/Codex Audit

## Target

Repository: `Tdog-Ventures/facelessforge-e69754f9`

Live application: `https://facelessforge.ethinx.solutions`

Known-good infrastructure state before this audit:

- public `/api/health` returns HTTP 200;
- Docker `facelessforge-app` is healthy;
- host port `7001` maps to container port `8080`;
- Uvicorn/FastAPI listens on `0.0.0.0:8080`;
- Redis health response reports active;
- tenant isolation health response reports JWT `tenant_id` enforcement.

Do not modify Caddy, Cloudflare, Docker port mappings, healthcheck wiring, Redis, Mongo networking, secrets, OAuth client IDs, OAuth client secrets, redirect URIs, or unrelated infrastructure during the first audit.

## Known User-Visible Failures To Reproduce

1. Login/account creation currently produces `405 Method Not Allowed` in the UI.
2. YouTube OAuth reaches Google account selection and consent for an approved tester, then callback/token exchange returns `invalid_client` / invalid client secret.
3. A Google account that is not registered as an OAuth test user is blocked by Google with `403 access_denied` while the OAuth app remains in Testing mode. Treat this as an external OAuth configuration/test-user condition, not an application-route defect.

Do not infer fixes from these notes alone. Reproduce each app-owned failure and collect evidence first.

## First Audit Mission

Use the TestSprite onboarding skill to inspect this codebase, create the TestSprite project, seed approximately 8–15 starter tests, and run the highest-value happy-path flows first.

Then expand coverage so the audit includes the following application surfaces where the repository supports them:

- landing/root page renders;
- static JS/CSS/assets load without 4xx/5xx;
- login form;
- account registration/create-account flow;
- invite flow / invite code handling;
- authenticated dashboard load;
- logout/session expiry behavior;
- project/list/create flows;
- media/asset library access;
- video/render creation entry point;
- render/output retrieval route;
- automation/forge-loop UI surface;
- YouTube OAuth initiation;
- OAuth callback route behavior up to the external-provider boundary;
- API health;
- authenticated API authorization boundaries;
- tenant isolation checks that can be safely exercised with dedicated QA accounts;
- meaningful 404/405/422/500 error handling;
- obvious broken buttons, navigation, CORS failures, reverse-proxy path mismatches, and frontend/backend route mismatches.

## Production Safety

This audit targets a live production hostname. Therefore:

- prefer non-destructive read-only tests;
- use dedicated QA/test accounts where credentials are available via environment variables;
- never commit credentials, cookies, tokens, OAuth secrets, API keys, or session data;
- never print secrets into logs or reports;
- do not delete real projects, videos, tenants, assets, users, or production data;
- if a write test is required, create clearly named disposable QA data and remove only that data when safe;
- do not publish videos or social posts;
- do not complete external OAuth actions that would grant unintended production access without explicit test credentials;
- distinguish application defects from Google/TestSprite/provider policy blocks.

## Failure Handling Contract

For every failure:

1. Reproduce it.
2. Record the exact user flow and failing step.
3. Capture request URL, HTTP method, status code, and relevant response body without secrets.
4. Capture browser/session evidence TestSprite provides.
5. Trace the frontend caller to the corresponding backend route/handler.
6. State root cause with confidence and supporting evidence.
7. Classify severity: blocker / high / medium / low.
8. Patch only when the cause is clear and the change is scoped.
9. Run repository tests relevant to the patch.
10. Rerun the failed TestSprite test.
11. Run regression tests covering adjacent auth/API/navigation behavior.
12. Do not mark fixed until the observable user flow passes.

Do not paper over a `405`, `401`, `403`, `422`, or `500` by changing the test expectation unless the repository contract proves that status is correct.

## Initial Priority Order

### P0 — Auth surface

Reproduce and diagnose the current `405 Method Not Allowed` affecting login/create-account. Determine:

- exact frontend endpoint;
- exact HTTP method sent;
- matching backend route, if any;
- accepted methods;
- whether frontend and backend route prefixes differ;
- whether reverse proxy/base URL configuration contributes;
- whether login and register incorrectly share one route.

### P0 — YouTube OAuth callback

Verify the code path and configuration source without exposing secrets:

- authorization request client ID source;
- callback route;
- token endpoint request;
- client ID source at exchange;
- client secret source at exchange;
- redirect URI source;
- tenant-specific OAuth storage/overrides, if any;
- whether stale persistent configuration can override the runtime environment.

A Google `invalid_client` result is not a TestSprite failure by itself; determine whether FacelessForge is presenting stale/incorrect credentials or whether the external OAuth client configuration is the remaining blocker.

### P1 — Core authenticated product flow

After auth is functional, verify dashboard, projects, assets/media, render creation entry point, render retrieval, and major navigation.

## Completion Report

Return exactly these sections:

## Audit Summary

## Tests Created

## Tests Executed

## Passed

## Failed

## Blockers

## Root Causes

## Code Changes

## Regression Results

## External Configuration Issues

## Remaining Risks

## Final Status

Final status must be one of:

- `PASS - critical user flows verified`
- `PARTIAL - application defects remain`
- `BLOCKED - external/test environment prevents completion`

Do not report PASS unless the tested critical user-visible flows actually passed in TestSprite.
