# FacelessForge Deploy Runbook

Deploy is `docker-compose.facelessforge.yml`: host `127.0.0.1:7001` to container `8080`, entrypoint `uvicorn main:app` (`Dockerfile`). Compose `env_file` is `/opt/facelessforge/deploy/.env.facelessforge`.

Run the commands below from the deploy checkout (the directory that contains `docker-compose.facelessforge.yml`).

## Critical Rules

1. **Always pass the compose file:**
   ```bash
   docker compose -f docker-compose.facelessforge.yml <command>
   ```

2. **Never use `--force-recreate` on `facelessforge-mongo`.** Recreate the app with `--no-deps` only when no render is running (`BOOT.md` hard rule 5):
   ```bash
   docker compose -f docker-compose.facelessforge.yml up -d --force-recreate --no-deps facelessforge-app
   ```

3. **Env file:** the stack reads the compose `env_file`, `/opt/facelessforge/deploy/.env.facelessforge`.

4. **Secret rotation:** after changing that env file, recreate the app with `--no-deps`.

5. **Code rollback:** do not use `git reset --hard`. Rebuild only when no render is running (`BOOT.md` hard rule 5).
   ```bash
   git log --oneline -5
   git revert <bad-commit-sha>
   docker compose -f docker-compose.facelessforge.yml up -d --build --no-deps facelessforge-app
   docker inspect facelessforge-app --format '{{.State.Health.Status}}'
   curl -s http://127.0.0.1:7001/api/health
   ```
