# FacelessForge Deploy Runbook

## Critical Rules

1. **Always use the correct compose file:**
   ```bash
   docker compose -f /opt/facelessforge/deploy/docker-compose.facelessforge.yml <command>
   ```
   Never run bare `docker compose` in this directory.

2. **Never use `--force-recreate` on mongo.** Use `--no-deps` when recreating the app:
   ```bash
   docker compose -f /opt/facelessforge/deploy/docker-compose.facelessforge.yml up -d --force-recreate --no-deps facelessforge-app
   ```

3. **Env file:** The stack reads `/opt/facelessforge/deploy/.env.facelessforge` (not `.env`). Update secrets there.

4. **Secret rotation:** After changing `.env.facelessforge`, recreate the app with `--no-deps`.

5. **Rollback:** If app is unhealthy after recreate, restore `.env.facelessforge.pre-fix` and recreate app only.

6. **Code rollback (scoped, preserves later work):** never use `git reset --hard`.
   ```bash
   cd /opt/facelessforge/deploy
   git log --oneline -5                       # identify the bad commit
   git revert <bad-commit-sha>                # newest first if several
   docker compose -f docker-compose.facelessforge.yml up -d --build --no-deps facelessforge-app
   docker inspect facelessforge-app --format '{{.State.Health.Status}}'
   curl -s http://127.0.0.1:7001/api/health
   ```
   File-level priors for recent slices live under `/root/ffbackups/<timestamp>-<slice>/`
   (each with `changed-files.txt`, `PRIOR_SHA.txt`, `rollback.txt`). Confirm zero
   active renders in the last 90 minutes before any rebuild (see SWARM_STATE.md).

