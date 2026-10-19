# FacelessForge RUNBOOK — Production Operations

**Generated:** 2026-09-09 — host `ubuntu-8gb-nbg1-1` — Hetzner

## 1. Architecture at a glance

| Layer | Process | Port / Path | Config |
|-------|---------|-------------|--------|
| **Systemd** | `facelessforge.service` (host-native) | `127.0.0.1:8081` `2 workers` | `/etc/systemd/system/facelessforge.service` `User=www-data` `WorkingDirectory=/opt/facelessforge/deploy/backend` (drop-in `.../releases/ff-youtube-20260908/backend` overrides `WorkingDirectory` + `Environment=AUTO_POST_ENABLED=false`) `EnvironmentFile=/opt/facelessforge/deploy/backend/.env` `ExecStart=.../venv/bin/uvicorn server:app --host 127.0.0.1 --port 8081 --workers 2` |
| **Docker** | `facelessforge-app` | `127.0.0.1:7001 → 8080` (`facelessforge-mongo` → `127.0.0.1:27017`) | `docker-compose.facelessforge.yml` `env_file: /opt/facelessforge/deploy/.env.facelessforge` volumes `facelessforge_renders:/app/backend/static/renders` `facelessforge_audio:/app/backend/static/audio` healthcheck `GET http://127.0.0.1:8080/api/health` |
| **Caddy** | `caddy` | `443` (+ `:80` → TLS) via `acme.sh --dns dns_cf` at `/etc/caddy/Caddyfile:112` | `facelessforge.ethinx.solutions { tls /etc/caddy/certs/ethinx.solutions.crt .key; reverse_proxy 127.0.0.1:7001 }` `content.ethinx.solutions → 7001`, `api.ethinx.solutions → 3100`. Cert renewal hook copies to `/etc/caddy/certs/ethinx.solutions.{crt,key}` then `systemctl reload caddy` |
| **DB** | `mongod --bind_ip_all` (docker `facelessforge-mongo:7`) + `postgres` (optional) | `mongodb://localhost:27017/facelessforge` (`MONGO_URL`) `DB_NAME=facelessforge` | `backend/.env:2` `REDIS_URL=redis://127.0.0.1:6379` (`ethinx-redis`) |
| **LLM** | `ollama serve` (system) | `http://localhost:11434` | `OLLAMA_MODEL=llama3:latest` (also `qwen2.5:7b-instruct`, `ethinx-sovereign:latest`) `OLLAMA_URL=http://localhost:11434/api/generate` `OLLAMA_TIMEOUT_SECONDS=20` |
| **Frontend** | React `craco` SPA | `frontend/build` → served by FastAPI `server.py` at `/` fallback `index.html` + `/static`, `/api/static` | `frontend/.env` `REACT_APP_BACKEND_URL=https://facelessforge.ethinx.solutions` `REACT_APP_DISABLE_WAITLIST=true` |

## 2. Env locations (truth, not docs)

```
# Host-native (systemd loads this before drop-in)
 /opt/facelessforge/deploy/backend/.env              ← primary (640 root:www-data, systemd EnvironmentFile)
/opt/facelessforge/releases/ff-youtube-20260908/backend/.env  ← must stay in sync (cp after every .env edit)

# Docker
 /opt/facelessforge/deploy/.env.facelessforge       ← docker env_file (also .env, .env.example at deploy root)
 /opt/facelessforge/deploy/.env.facelessforge.save / .backup-*

# Frontend (build-time, baked into JS)
 /opt/facelessforge/deploy/frontend/.env
 /opt/facelessforge/deploy/frontend/.env.production

# Secrets vault (mounted, not in repo)
 /opt/ethinx/deployment/secrets-vault/facelessforge-genny-vid-vertex.json  ← GOOGLE_APPLICATION_CREDENTIALS

# Caddy TLS
 /etc/caddy/certs/ethinx.solutions.crt/.key
 /etc/letsencrypt/cloudflare.ini
```

**After any `.env` edit:** `cp /opt/facelessforge/deploy/backend/.env /opt/facelessforge/releases/.../backend/.env && chmod 640 ... && chown root:www-data ... && systemctl daemon-reload && systemctl restart facelessforge && docker cp frontend/build/. facelessforge-app:/app/frontend/build/ && docker restart facelessforge-app` (or at least `systemctl restart caddy` if Caddyfile changed).

## 3. How to restart / health check

```bash
# systemd host-native (primary)
systemctl status facelessforge
systemctl daemon-reload; systemctl restart facelessforge
journalctl -u facelessforge -f --since "5 min ago"
curl -s http://127.0.0.1:8081/api/health | jq
curl -s http://127.0.0.1:8081/api/health/detailed | jq  # after §3 patch
curl -s http://127.0.0.1:8081/api/docs | head

# docker
docker ps | grep facelessforge
docker logs -f facelessforge-app --tail 100
docker exec facelessforge-app curl -s http://127.0.0.1:8080/api/health
docker compose -f /opt/facelessforge/deploy/docker-compose.facelessforge.yml ps
docker compose -f /opt/facelessforge/deploy/docker-compose.facelessforge.yml restart facelessforge-app

# Caddy
systemctl status caddy; caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
tail -f /var/log/caddy/facelessforge.ethinx.solutions.log
curl -s https://facelessforge.ethinx.solutions/api/health | jq

# WorkingDirectory confusion: systemd shows WorkingDirectory=/opt/facelessforge/releases/.../backend due to drop-in.
# The file on disk that systemd actually runs is /opt/facelessforge/deploy/backend/venv/bin/uvicorn server:app
# but python resolves Path(__file__) → releases/.../backend/server.py when drop-in is active.
# Keep both trees in sync; the symlink /root/facelessforge-deploy → /opt/facelessforge/deploy helps.
```

## 4. Where logs are

| Source | Command |
|--------|---------|
| **Systemd** | `journalctl -u facelessforge -n 300 --since "30 min ago"` `journalctl -u facelessforge -f` `journalctl -u facelessforge | grep -E "PADDLE|PROVISION|EXTERNAL_RENDER|RENDER|FOOTAGE|KEN_BURNS|VERIFY"` |
| **Docker** | `docker logs facelessforge-app --tail 200` |
| **Caddy** | `/var/log/caddy/facelessforge.ethinx.solutions.log` `/var/log/caddy/content.ethinx.solutions.log` `/var/log/caddy/ethinx.solutions.log` |
| **Render artifacts** | `/opt/facelessforge/deploy/backend/static/renders/{project_id}/_work_{job_id}/` (tmp, cleaned after mux) → final `backend/static/renders/{project_id}/{job}.mp4` + R2 `renders/{project}/{job}.mp4` |
| **QA artifacts** | `backend/static/renders/{project}/qa_runs/{run_id}/probe.json` `deterministic_findings.json` `frames/` `visual_findings.json` |
| **Paddle/Dodo** | `journalctl -u facelessforge | grep -E "\[PADDLE\]|\[PROVISION\]|Dodo webhook"` `redis-cli --raw hgetall "tenant:{id}"` `redis-cli SMEMBERS dodo:webhooks:processed` |
| **Ollama** | `journalctl -u ollama` `curl http://localhost:11434/api/tags` `ollama ps` `ollama logs` |

## 5. Ollama checks

```bash
curl -s http://localhost:11434/api/tags | jq          # should list llama3:latest, qwen2.5:7b-instruct, ethinx-sovereign:latest
curl -s http://localhost:11434/api/generate -d '{"model":"llama3:latest","prompt":"Return JSON {\"a\":1}","stream":false,"format":"json"}' | jq .response
# Expected: {"a":1} in <10s warm (first load 5s). If >20s, we timeout and fallback.
# Generation uses http://localhost:11434/api/generate system+prompt, format=json, options {temperature:0.7,num_ctx:8192}
# Timeout: OLLAMA_TIMEOUT_SECONDS=20 (backend/.env:128). 2 attempts → fallback to DeepSeek (sk-c447...) → Kimi → Claude.
# Symptoms: journal shows "Ollama (llama3:latest) timeout for script-... attempt 1/2" then "DeepSeek ...". That's normal if Ollama cold; if always timeout, check `ollama serve` RAM (8GB) and `df -h` (150G, 76% used).
# Test scene regeneration topic-specificity:
# POST /api/projects/{id}/generate-scenes with "How pets improve mental health" should return search_terms like ["dog brain scan","oxytocin"] not ["cinematic b-roll"].
```

## 6. ElevenLabs timeout symptoms

* **Config:** `ELEVENLABS_API_KEY=sk_d71...` `ELEVENLABS_VOICE_ID=f1cjR1...` `TTS_PROVIDER=elevenlabs` `DEFAULT_VOICE_STYLE=narrator` `GROQ_API_KEY` as fallback. `tts.py` splits at 4500 chars, posts to `https://api.elevenlabs.io/v1/text-to-speech/{voice_id}` with `voice_settings {stability, similarity_boost, style}` + `use_speaker_boost`.
* **Forge loop timeout:** `backend/app/workers/forge_loop.py:321` `wait_for(tts.generate_voiceover(...), timeout=120)` — was 15s, now 120s. If you see `voiceover timeout — using mock` → mock wav (silent zeros) inserted with `source=mock_tts, mock=True, preview_url=/api/static/audio/{pid}_mock.mp3`. Render then skips it (`render.py:856 _is_real = not mock and source != "mock_tts"`) and falls back to `music-bed-only` mux (still audible via `default_bed.mp3`).
* **Check:** `curl -s http://127.0.0.1:8081/api/tts/meta | jq` → `{"mock":false,"provider":"elevenlabs"}` if key present. Test: `POST /api/projects/{id}/voiceover/generate-script` should return `mock:false, duration~20s` not `mock:true`.
* **Fix:** If ElevenLabs 429/401, check `journalctl -u facelessforge | grep "\[tts\]"`, verify `ELEVENLABS_API_KEY` not expired, increase `VOICEOVER_TIMEOUT` only via env, not code.

## 7. Pexels 429 / stock handling

* **Keys:** `PEXELS_API_KEY=sBQps...` `PIXABAY_API_KEY=572...` `UNSPLASH_ACCESS_KEY=MLY...` (`backend/.env:49-50`). `USE_MOCK_PEXELS=false`.
* **Search:** `backend/app/stock.py:371 _search_pexels` → `GET https://api.pexels.com/v1/search?query=...&per_page=40` with `Authorization: PEXELS_API_KEY`. On `429` raises `RuntimeError("pexels_rate_limited")` (`stock.py:405,423`). `render.py:559-734` then retries via `stock_service.search_stock` with `build_scene_query(scene, visual_tone)` + `extract_visual_keywords` + `page=2..` and dedup by `external_id`/`url` hash. If all fail, Pillow fallback `_pil_caption_frame` (dark `0A0A0A` grid, accent bar, caption text truncated `80` via `truncate_words`).
* **Symptoms:** `journal: FOOTAGE_REJECT reason=no_motion` or `download_failed` or `FOOTAGE_FALLBACK reason=all_candidates_rejected` → editorial projects still render (0.5 coverage ok), `commercial_ad` requires `validate_commercial_grounding` → `422`.
* **Handling:** 429 is logged `FOOTAGE_RETRY_ERROR` and automatically falls back to Pixabay/Unsplash mixed search (`stock.py:724 providers=["pexels","pixabay","unsplash"]`) then to caption frame. No manual action; if sustained, check `curl -I https://api.pexels.com/v1/search?query=test -H "Authorization: $PEXELS_API_KEY"` for `X-Ratelimit-Remaining`. Increase `MAX_VIDEO_DOWNLOAD_BYTES=60MB`, `per_page=20` → `40` pagination already.

## 8. Disk / Renders / Redis

```bash
df -h                    # 150G, 76% used — watch overlay 150G
du -sh /opt/facelessforge/deploy/backend/static/renders/* | sort -h | tail
redis-cli ping           # PONG
redis-cli --raw hgetall "tenant:{user_id}"   # Dodo/PayPal finite credits
redis-cli SMEMBERS "dodo:webhooks:processed" | head
```

## 9. Quick triage playbook

| Symptom | Check | Fix |
|---------|-------|-----|
| `502` on `https://facelessforge.ethinx.solutions` | `systemctl status facelessforge` + `caddy validate` | `systemctl restart facelessforge && systemctl reload caddy` |
| `/ph` 404 | `curl http://127.0.0.1:8081/ph` vs `7001/ph` | `cp -r frontend/build/* frontend/build` to both trees (see §2) + `docker cp` + restart |
| Thumbnails `—` | `db.assets.find({thumbnail_concept})` missing `brief` | migrated 77 flat docs to `brief` in 2026-09-09; frontend now `a.brief || a` |
| Video has no audio | `ffprobe -show_streams renders/.../job.mp4` lacks `audio` | Check `_resolve_audio` mock filter + `default_bed.mp3` exists + ElevenLabs key, see §6 |
| QA `qa_stage_error` → render `failed` | `VIDEO_QA_PROVIDER=none` now deterministic-only PASS (was fail-closed) | `video_qa.py:439` patched; keep `VIDEO_QA_PROVIDER=none` in prod unless Vertex creds valid |

## 10. Contacts & env ownership

* `ADMIN_EMAIL=tdogga14@gmail.com` `ADMIN_PASSWORD=EthinxForge2026!` (`backend/.env:9`) — rotate after audit.
* `FRONTEND_URL=https://facelessforge.ethinx.solutions` `STORAGE_PUBLIC_BASE_URL=https://videos.ethinx.solutions` (R2 `facelessforge-prod` via `9d1310958fd8...r2.cloudflarestorage.com`).
* Keep `backend/.env` 640 root:www-data, never commit.
