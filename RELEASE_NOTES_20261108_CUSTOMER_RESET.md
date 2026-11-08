# Customer password reset — 2026-11-08

Image: `facelessforge-app:release-ff-27a483e-resetfix-20261108`
`sha256:4070da2670e993aeb96bd40881e5428dc461c4383af3d4dea88a5d5aac028206`
Built FROM `release-ff-27a483e-genfix-20261108` (sha256:dc74c265…), preserving the script-length fix,
prior footage backend hashes and runtime-only modules.

## Changes
- `frontend/src/App.js`: `/reset-password` is no longer wrapped in `GuestOnly`. Login and register keep
  their guest guards. A customer holding a live session can now open an emailed reset link; previously they
  were redirected to /app and could never set a new password.
- `backend/app/routes/api.py`: reset tokens are stored as SHA-256 digests (`_hash_reset_token`); the raw
  token exists only in the email. Lookup accepts `token_hash` or a legacy raw `token`, so links issued before
  this change stay valid until their normal expiry. Redemption is an atomic claim
  (`find_one_and_update` on `{_id, used_at: null}`); a losing concurrent request gets 400 instead of a double
  write. If the password write fails the claim is released and the caller gets 503, never a silent partial
  update. Failed sends still invalidate the token. Generic 200 response, rate cap, 60-minute TTL, single-use
  and expiry semantics are unchanged.

## Verification
Candidate image, network none, mocked provider/DB: 10 reset regressions passed (registered issuance with
generic response, unknown email sends nothing, send failure invalidates, expired rejected, valid token
updates only the intended user, reuse rejected, concurrent single winner, short password rejected by schema,
legacy raw token accepted, password-write failure releases the claim); 32 visual/API, 6 attachment (+1 skip),
5 script-length tests passed.

Live after activation: health/db/public 200, served bundle `main.ffbfcb78.js`, api.py
`c10a3a28fd36…` in both backend paths, generation.py `a3573136…` and render.py `63986bb7…` unchanged,
mail mode resend/configured true, creator account id and password hash unchanged, still 23 projects owned,
one legacy reset token left open and unexpired.

## Rollback
```
docker compose -p ff-continuity -f /opt/facelessforge/backups/ff-genfix-activation-20261108T041048Z/docker-compose.genfix-mail.yml up -d --no-deps --pull never app
```
