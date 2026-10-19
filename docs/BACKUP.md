# FacelessForge BACKUP — Daily

**Host:** `ubuntu-8gb-nbg1-1` (Hetzner) — `150G` disk, `76%` used — `mongodb://localhost:27017/facelessforge` + `postgres` (optional) + `ethinx-redis` `127.0.0.1:6379` + R2 `facelessforge-prod` via `https://9d1310958fd8...r2.cloudflarestorage.com` → `https://videos.ethinx.solutions`

## 1. What to backup daily (no excuses after 2026-09-06 restore point loss)

| What | Path / Command | Why | Retention |
|------|----------------|-----|-----------|
| **Host env (primary)** | `/opt/facelessforge/deploy/backend/.env` (640 root:www-data, `SECRET_KEY`, `JWT_SECRET`, `ELEVENLABS_API_KEY`, `PEXELS_API_KEY`, `STORAGE_*`, `PADDLE_*`, `DODO_*`, `OLLAMA_*`) | Systemd `EnvironmentFile` — lose it = lose `SECRET_KEY`/`JWT_SECRET` rotate, all JWT invalid, Paddle `LIVE` price IDs lost | 30d |
| **Docker env** | `/opt/facelessforge/deploy/.env.facelessforge` (diff: `MONGO_URL=mongodb://facelessforge-mongo:27017`, `NEXTAUTH_*`, `STORAGE_*` duplicate) | `docker-compose.facelessforge.yml:15 env_file` — Docker mounts renders/audio volumes | 30d |
| **Caddy TLS** | `/etc/caddy/certs/ethinx.solutions.crt/.key` + `/etc/letsencrypt/cloudflare.ini` | DNS-01 `acme.sh --dns dns_cf` — Cloudflare token | 90d |
| **Frontend build** | `/opt/facelessforge/deploy/frontend/build/` + `/opt/facelessforge/releases/.../frontend/build/` + `backend/static/frontend/` | `main.370e5cc9.js` — lose it = `/ph` 404 until next `yarn build` | 7d |
| **Mongo** | `mongodump --uri="mongodb://localhost:27017/facelessforge" --out=/var/backups/mongo/{date}` | `projects`, `scripts`, `scenes`, `assets` (77 thumbnail_concept migrated 2026-09-09), `render_jobs`, `users`, `waitlist`, `subscriptions`, `ff_credits` | 30d daily + 90d weekly |
| **Postgres** (if used) | `pg_dump -Fc -h 127.0.0.1 -U postgres facelessforge > /var/backups/pg/{date}.dump` | Alternative tenant store (currently Mongo primary) | 30d |
| **Redis tenant credits** (finite, not `ff_credits`) | `redis-cli --rdb /var/backups/redis/dump.rdb && cp /var/lib/redis/dump.rdb /var/backups/redis/` + `redis-cli --raw HGETALL "tenant:{id}"` audit list `tenant:*:billing_audit` | `Dodo/PayPal` grant `hincrby tenant:{tenant_id} credits` — lose it = double-spend / lost credits | 7d (RDB) + audit `LPUSH` capped 999, `EXPIRE audit:transaction:{id} 2592000` |
| **R2** | **Not dumped locally** — R2 is source of truth (`STORAGE_BUCKET=facelessforge-prod`). Verify via `aws s3 sync --endpoint-url https://9d131...r2.cloudflarestorage.com s3://facelessforge-prod /var/backups/r2-mirror --dryrun` | `renders/{project}/{job}.mp4` 1.4M + `audio/{project}/*.mp3` + `thumbs/{project}/*.svg` | Versioned, 30d |
| **Vault secrets** | `/opt/ethinx/deployment/secrets-vault/facelessforge-genny-vid-vertex.json` (`GOOGLE_APPLICATION_CREDENTIALS`) | Vertex `genny-vid` QA | 90d |

## 2. Cron script — install once, run daily 03:00

```bash
cat >/usr/local/bin/facelessforge-backup.sh <<'EOS'
#!/bin/bash
set -euo pipefail
DATE=$(date +%Y%m%d-%H%M)
ROOT=/var/backups/facelessforge
mkdir -p $ROOT/{env,mongo,pg,redis}
# 1. envs (600 → 640 after restore)
cp -a /opt/facelessforge/deploy/backend/.env $ROOT/env/backend.env.$DATE
cp -a /opt/facelessforge/deploy/.env.facelessforge $ROOT/env/docker.env.$DATE
cp -a /etc/caddy/certs/ethinx.solutions.crt $ROOT/env/caddy.crt.$DATE 2>/dev/null || true
cp -a /etc/caddy/certs/ethinx.solutions.key $ROOT/env/caddy.key.$DATE 2>/dev/null || true
chmod 600 $ROOT/env/*.DATE
# 2. mongo
mongodump --uri="mongodb://localhost:27017/facelessforge" --out=$ROOT/mongo/$DATE --quiet || mongodump --host 127.0.0.1:27017 --db facelessforge --out=$ROOT/mongo/$DATE
tar -czf $ROOT/mongo-$DATE.tgz -C $ROOT/mongo $DATE && rm -rf $ROOT/mongo/$DATE
# 3. postgres (if any)
if pg_isready -h 127.0.0.1 -q 2>/dev/null; then pg_dump -Fc -h 127.0.0.1 -U postgres facelessforge > $ROOT/pg/pg-$DATE.dump 2>/dev/null || true; fi
# 4. redis RDB + hash dump (finite credits)
redis-cli --rdb $ROOT/redis/dump-$DATE.rdb >/dev/null 2>&1 || redis-cli SAVE >/dev/null 2>&1; cp /var/lib/redis/dump.rdb $ROOT/redis/dump-$DATE.rdb 2>/dev/null || true
redis-cli --raw KEYS "tenant:*" | head -n 1000 > $ROOT/redis/keys-$DATE.txt 2>/dev/null || true
# 5. prune (>30d env/mongo/pg, >7d redis)
find $ROOT/env -name "*.DATE" -o -name "*.tgz" | xargs -r find $ROOT -mtime +30 -delete 2>/dev/null || true
find $ROOT/redis -mtime +7 -delete 2>/dev/null || true
# 6. r2 verify (dry-run, log only)
if command -v aws >/dev/null 2>&1; then aws s3 ls s3://facelessforge-prod --endpoint-url https://9d1310958fd8b19bb31d13f6ebe66581.r2.cloudflarestorage.com --recursive | head -n 20 > $ROOT/r2-ls-$DATE.txt 2>/dev/null || true; fi
echo "[BACKUP] $DATE ok env+mongo+redis r2-ls"
EOS
chmod +x /usr/local/bin/facelessforge-backup.sh
# crob
(crontab -l 2>/dev/null | grep -v facelessforge-backup; echo "0 3 * * * /usr/local/bin/facelessforge-backup.sh >>/var/log/facelessforge-backup.log 2>&1") | crontab -
# test
/usr/local/bin/facelessforge-backup.sh && ls -lh /var/backups/facelessforge/env/ | tail -n 5 && ls -lh /var/backups/facelessforge/mongo-*.tgz | tail -n 5
```

## 3. Restore drills (do not skip after 2026-09-06 loss)

* **Env loss:** `cp /var/backups/facelessforge/env/backend.env.{latest} /opt/facelessforge/deploy/backend/.env && cp /var/backups/facelessforge/env/docker.env.{latest} /opt/facelessforge/deploy/.env.facelessforge && chmod 640 ... && chown root:www-data ... && cp /opt/facelessforge/deploy/backend/.env /opt/facelessforge/releases/.../backend/.env && systemctl daemon-reload && systemctl restart facelessforge && docker compose -f /opt/facelessforge/deploy/docker-compose.facelessforge.yml restart`.
* **Mongo loss:** `mongorestore --uri="mongodb://localhost:27017/facelessforge" /var/backups/facelessforge/mongo/{date}/facelessforge --drop`.
* **Redis loss (credits):** `redis-cli --pipe < /var/backups/facelessforge/redis/dump-{date}.rdb` or `redis-cli FLUSHDB && for h in tenant:*; do HSET...` from `keys-*.txt` audit `tenant:*:billing_audit` LPUSH.
* **R2 loss:** R2 is versioned; if bucket deleted, `aws s3 sync /var/backups/r2-mirror s3://facelessforge-prod --endpoint-url ...`.
* **Frontend build loss:** `cd /opt/facelessforge/deploy/frontend && yarn run build && cp -r build/* backend/static/frontend/ && cp -r build/* releases/.../frontend/build/ && docker cp build/. facelessforge-app:/app/frontend/build/ && systemctl restart facelessforge`.

## 4. What is NOT backed up (and why)

* `/opt/facelessforge/deploy/backend/static/renders/*` (449M per project like `a0d4a4fc...` scenes) — ephemeral, source is R2; local is cache.
* `frontend/node_modules` — reinstall via `yarn`.
* Overlay `150G` docker layers — rebuild via `docker compose build`.

## 5. Verification (add to RUNBOOK §8)

```bash
ls -lh /var/backups/facelessforge/env/ | tail
ls -lh /var/backups/facelessforge/mongo-*.tgz | tail
redis-cli INFO persistence | grep rdb
aws s3 ls s3://facelessforge-prod --endpoint-url https://9d1310958fd8b19bb31d13f6ebe66581.r2.cloudflarestorage.com | head
```

Store offsite: `rclone sync /var/backups/facelessforge r2:facelessforge-backups --config /etc/rclone/rclone.conf` (add after first local week).
