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

