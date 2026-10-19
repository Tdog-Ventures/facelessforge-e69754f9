# FacelessForge — Phase 0 Audit

**Audited:** 2026-07-22 UTC  
**Auditor:** KimiCode (facelessforge-ship agent)  
**Deploy path:** `/opt/facelessforge/deploy` (working tree exposed via symlink `/root/facelessforge-deploy`)  
**Canonical repo:** `git@github.com:ethinxsolutionsau-prog/facelessforge.git`  
**SOT backups:** `/root/backups/sot/20260722-0425/` (pre-change), `/root/backups/sot/20260722-0522/` (post-E2E)  

---

## 1. Execution context

- **Host:** Hetzner VPS `91.99.162.243`, 4 vCPU / 16 GB RAM / Ubuntu, FFmpeg 6.1.1.
- **FFmpeg filter audit:** `vidstabdetect`, `lut3d`, `loudnorm`, `xfade`, `deflicker`, `sidechaincompress`, `blackdetect`, `freezedetect` all present.
- **Service:** `facelessforge.service` active and running on port 8081.
- **LLM:** Ollama is installed and reachable on `localhost:11434`. Models present: `llama3:latest`, `ethinx-sovereign:latest`.
- **Runtime env:** systemd unit loads `/opt/ethinx/deployment/secrets-vault/facelessforge-deploy.env`. This is the live env, *not* `.env.facelessforge` in the repo.
- **Git state:** uncommitted changes exist in `Dockerfile`, `backend/app/generation.py`, `backend/app/render.py`, `backend/app/routes.py`, `backend/app/tts.py`, plus untracked `backend/static/frontend/` artifacts and render workdirs.

---

## 2. Architecture (text diagram)

```
User / React frontend ( served from backend/static/frontend/ )
        |
        v
FastAPI backend (port 8081) — uvicorn, 1 worker
        |
        +-- auth/users (MongoDB)
        +-- projects CRUD + status/scoring
        +-- generation (script, scenes, metadata, thumbnail concepts)
        +-- TTS (ElevenLabs primary, mock fallback)
        +-- stock footage (Pexels primary, mock fallback)
        +-- thumbnail images (Gemini Nano Banana via emergentintegrations, SVG mock fallback)
        +-- render queue (asyncio background task per project)
        +-- storage abstraction (local or Cloudflare R2 object mode)
        +-- retention + admin diagnostics
        |
        v
MongoDB ( mongodb://172.17.0.4:27017/facelessforge )
R2 object storage ( facelessforge-prod bucket via STORAGE_MODE=object )
Local static dirs (backend/static/renders, backend/static/thumbs, backend/static/music)
```

---

## 3. What works

- **Service boots and serves API + frontend.**
- **Auth, project CRUD, and share links** are functional.
- **Generation module** (`backend/app/generation.py`) has been refactored to use Ollama as primary LLM, DeepSeek as cloud backup, then deterministic fallback. Scene fallbacks are topic-derived, not purely generic business.
- **TTS** is wired to ElevenLabs with real voice ID `f1cjR1nonQ70hmW0yRhF` and falls back to mock WAV silence.
- **Stock footage** uses Pexels API with retry/broader keyword logic.
- **Render pipeline** (`backend/app/render.py`) implements the full chain: intro thumbnail → scene visual downloads → audio resolve → background music → subtitle SRT → FFmpeg concat + mux with `amix` and `subtitles` filter.
- **Storage abstraction** supports local and R2 object modes.
- **Admin diagnostics** endpoint exposes provider modes, disk usage, and binary paths.

---

## 4. What is stubbed / partially implemented

- **Auto-post to platforms:** `send_to_adengine` is called when `auto_post=true`, but the AdEngine adapter is a placeholder that does not actually post to YouTube or any platform.
- **Scheduler:** no scheduled rendering/publishing process exists.
- **Quality gates / post-production battery:** no artifact scanning, segment scoring, or automated re-render policy exists beyond basic render validation.
- **Continuity enforcement:** no memory store, style tokens, entity ledger, or segment diffing exists.
- **Model registry / routing:** LLM calls are hard-coded to Ollama → DeepSeek → fallback; no external video-generation models (Kimi Video, Kling, etc.) are wired.
- **Multi-platform posting:** only `auto_post` flag + platforms list exist in schema; no adapters.

---

## 5. What is broken / risky

1. **_llm_json silent failure / non-JSON responses from Ollama.**
   - The function logs warnings and returns `None`, but the caller receives no diagnostic signal. If Ollama returns malformed JSON (common with `llama3`), generation silently falls through to deterministic templates even though the model is "reachable".
   - *Impact:* scene visuals may still end up generic if the LLM response is unparsable.

2. **Rendered MP4 may have no usable audio / music / subtitles in practice.**
   - Code exists, but:
     - `_resolve_audio` downloads voiceover via `preview_url`/`download_url`/`url`. If `store.save_file()` returns object URLs, the download may 404 if the URL is a local preview path rather than a public URL.
     - The `subtitles` filter path escaping is brittle and can break on paths containing special characters.
     - Background music depends on `backend/static/music/default_bed.mp3` existing; if absent, a silent bed is generated.
   - *Impact:* output MP4 may be silent or subtitle-free.

3. **ThumbnailPanel.jsx shows "—" for fields.**
   - The panel reads `b?.visual_composition`, `b?.emotion_angle`, etc. The generation endpoint stores concepts as `thumbnail_concept` assets with `brief` field, but the brief shape returned by `generate_thumbnail_concepts` uses keys `thumbnail_title_text`, `visual_composition`, `emotion_angle`, etc. If the stored `brief` is empty or the wrong shape, rows show "—".
   - Root cause appears to be a mismatch between `routes.py` `generate-thumbnails` endpoint and `generation.py` concept schema: the endpoint calls `gen.generate_thumbnail_concepts(project, script)` (new Ollama version) and inserts `brief: c`. The shape should match. Need to verify live data.

4. **Environment loading in systemd is not the repo `.env.facelessforge`.**
   - `AGENTS.md` states `.env` is the runtime env, but the service uses `EnvironmentFile=/opt/ethinx/deployment/secrets-vault/facelessforge-deploy.env`. The repo `.env.facelessforge` is a template, not the live source.
   - *Impact:* edits to `.env.facelessforge` do not take effect until the secrets-vault file is updated.

5. **Uncommitted code and untracked render artifacts.**
   - `backend/app/generation.py`, `render.py`, `routes.py`, `tts.py` have uncommitted changes.
   - `backend/static/renders/<project>/` directories are untracked in git but contain production render outputs.
   - *Impact:* risk of losing work; builds may include stale frontend bundles.

6. **No end-to-end automated test for the full pipeline.**
   - `backend/tests/backend_test.py` exists but scope is unknown without running it.

---

## 6. Gap list to Milestone A (one unattended episode)

The blueprint assumes five packaged `.skill` files are uploaded to the box. They were **not found** anywhere on `/root` after searching for `*.skill` and related zip names.

| Gap | Status | Blocker |
|-----|--------|---------|
| 8 skill scripts lifted into `tools/` | Not started | `.skill` files missing |
| `config/model_registry.json` with real endpoints | Not started | TDog auth gate #1; also skill files missing |
| Continuity memory store initialized | Not started | skill files + style tokens missing |
| End-to-end render with enforced gates | Partial | render code exists but lacks artifact battery / retry discipline from skills |
| Delivery manifest + probe reports | Not started | skill scripts missing |

Because the skill packages are not present, **Milestone A cannot be completed as specified in the blueprint**. The practical next step is either:

- **Option A:** TDog uploads the five `.skill` files, then proceed with Phase 1 integration.
- **Option B:** Ship the existing FacelessForge deploy first by fixing the known critical issues listed in §5 and the `facelessforge-ship` skill, then revisit the full skill integration once files arrive.

---

## 7. Auth gates status

| Gate | Status | Notes |
|------|--------|-------|
| Model API keys (Grok/Kimi/Kling) | Not provided | Only Ollama + Anthropic + DeepSeek keys exist in env |
| YouTube OAuth | Not started | No `credentials.json` or `token.json` found |
| R2 keys | Present | In secrets-vault env; STORAGE_MODE=object active |
| GitHub push access from box | Unknown | `origin` is SSH; need to test `git push` |

---

## 8. Recommendations

1. **Commit or reset the current working tree** before making further changes so the deploy state is reproducible.
2. **Decide on Option A vs Option B** (skill integration vs. fix-and-ship existing deploy).
3. If proceeding with existing deploy fixes first:
   - Harden `_llm_json` to surface Ollama parse errors and retry with a fallback format.
   - Fix `_resolve_audio` URL resolution for object-storage voiceovers.
   - Verify and fix thumbnail concept schema round-trip.
   - Add a deterministic default music bed if missing.
   - Run the E2E smoke path: create project → generate script → regenerate scenes → generate voiceover → generate thumbnails → attach assets → render → verify MP4 has audio + subtitles.

---

## 9. Backup log

- `/root/backups/sot/20260722-0425/facelessforge-deploy_code.tar.gz` — code snapshot before changes.
- `/root/backups/sot/20260722-0425/facelessforge-deploy_secrets.tar.gz` — env snapshot.
- `/root/backups/sot/20260722-0425/CHANGELOG` — one-line change note.
- `/root/backups/sot/20260722-0522/facelessforge-deploy_code.tar.gz` — code snapshot after fixes + E2E pass.
- `/root/backups/sot/20260722-0522/facelessforge-deploy_secrets.tar.gz` — env snapshot.
- `/root/backups/sot/20260722-0522/CHANGELOG` — one-line change note.

---

## 10. E2E test result

**Test command:** `/tmp/e2e_facelessforge.py`  
**Completed:** 2026-07-22 05:21:49 UTC  
**Result:** PASSED  
**Project:** `4cc655a4-0ea9-43d5-8527-df587cfa68a1`  
**Render job:** `28254186-a5da-483f-a8be-8a4064d72422`  
**Output:** `/tmp/e2e_render_4cc655a4-0ea9-43d5-8527-df587cfa68a1.mp4`  

```json
{
  "duration": 46.07,
  "streams": {"video": "h264", "audio": "aac"},
  "size_bytes": 16167664
}
```

**Pipeline proved:**
- Project creation
- Script generation via Ollama (114 words)
- Scene generation via Ollama (2 topic-specific scenes: Colosseum/Pantheon, Roman coins/legions)
- Metadata + thumbnail brief generation via Ollama
- Mock full-script voiceover generation
- Thumbnail image generation (mock SVG)
- Stock asset auto-attach (2 Pexels videos)
- Render job completion with intro + scenes + concat + audio mux + subtitle burn-in
- Final MP4 has both video and audio tracks

---

## 11. Remaining production issues (post-E2E)

1. **Claude API returns 404 for all model names tested** (`claude-3-5-sonnet-20241022`, `-latest`, `-20240620`, `claude-3-haiku-20240307`). The `ANTHROPIC_API_KEY` in the vault is present but the account/key appears invalid or not provisioned for API access. Ollama is now the sole working LLM; it is slow (~1–2 min per generation) but functional.
2. **R2 public URLs return 403.** `https://videos.ethinx.solutions/<key>` is not publicly accessible. The render pipeline now falls back to boto3 download for voiceover assets using `storage_key`, and the E2E test falls back to boto3 for the final MP4. Real users will hit 403 when trying to play/download renders unless the bucket/Cloudflare config is fixed or the service switches to presigned URLs.
3. **TTS still falls back to mock.** The E2E voiceover was generated in mock mode despite `USE_MOCK_TTS=false` and a valid-looking `ELEVENLABS_API_KEY`. The ElevenLabs call is failing and silently falling back to mock WAV silence. This is not a mechanical audio issue (the final MP4 has audio) but a quality/voice issue.
4. **Blueprint Phases 1–4 remain blocked** until TDog uploads the five `.skill` files and provides working model API keys + YouTube OAuth credentials.
