# Content Safety Audit — FacelessForge

**Date:** 2026-09-09 — **Scope:** `backend/app/generation.py`, `backend/app/stock.py`, `backend/app/visual_query.py`, `backend/app/adapters/posters/base.py`, `backend/app/render.py`

## 1. NSFW check on `generate-script`

**Current:** **No dedicated NSFW filter.** `generation.py:294` system prompt is `You are a senior YouTube scriptwriter ... Always respond with strict JSON` with `strip_beat_markers` and `is_garbage_token` (repeat artifacts `STARTUPSTARTUP`). `adapters/posters/base.py:41` has `Basic profanity / spam check placeholder` comment but no enforcement. `visual_query.py:37` blocklist is only `abstract/modal/temporal` words.

**Risk:** User can prompt `generate-script` with `topic: "explicit NSFW how-to"` (e.g., porn, gore, hate) → Ollama (local) will comply (no policy), DeepSeek/Claude may refuse but Ollama primary will not. Script could contain disallowed content, then `tts` (ElevenLabs) will synthesize it, `render` will produce MP4, and auto-publish (`YOUTUBE_MOCK=false`, `AUTO_POST_ENABLED=true` in `backend/.env:112`) would upload to YouTube → **community guidelines strike, demonetization, channel termination**.

**Evidence:**
- `grep -rn "nsfw|profanity" backend/app/generation.py` → 0 hits
- `generation.py:369` prompt `Write specifically about the Topic above. Do NOT fall back to generic...` — no `Do not generate disallowed content` instruction.

**Recommend:**
- Add pre-LLM blocklist: `NSFW_KEYWORDS = ["porn","nude","sex toy","erotic","gore","cutting","suicide","hate"]` case-insensitive on `topic` + `full_script` before `gen.generate_script` → `if any(k in topic.lower() for k in NSFW): raise 400`. Log `NSFW_BLOCK`.
- Add system prompt line: `You must not generate sexual, violent, hateful, or self-harm content. If the topic requests disallowed content, return JSON with "full_script":"I can't help with that."`
- Hook ElevenLabs `tts.py:85 sanitize_for_tts` already strips `[.*?]` but not profanity — add same blocklist there.
- For YouTube, enable manual review gate: keep `AUTO_POST_ENABLED=false` in `zz-clean-release.conf:2` drop-in (already) and require human `POST /api/youtube/upload` with `require_roles("creator")` — do not enable auto-publish on Product Hunt launch.

## 2. Copyright check on stock search

**Current:** **No copyright ownership verification; only license attribution.** `stock.py:15-324` normalises Pexels/Pixabay/Unsplash to `{source, attribution_name, attribution_url, source_url}` and `render.py:85` mirrors blocklist `["Split","SALDI","Slack","2026","sale","umbrella","tourist"]` (visual mismatch, not copyright). No `copyrighted text` scan, no `YouTube Content ID` pre-check, no `Shutterstock` watermark detection.

**What *is* done:**
- Every result keeps `attribution_*` + `source_url` for itch (`stock.py:235` `https://www.pexels.com/video/{id}/`). License disclosure is correct per `/license` (Pexels License: free on YouTube, no attribution required but kept). Rate limit `429` is handled via `pexels_rate_limited` → fallback Pixabay/Unsplash → mock caption frame.

**Risk for YouTube:**
- User provides `topic: "Play entire Taylor Swift music video"` → `generate_script` will produce lyrics verbatim (copyrighted text) → TTS will read it → video with popular song lyrics → **Content ID claim**.
- Stock query `semantic_pexels_query` uses `visual_query: derive_search_terms` from narration without filtering trademarked brands. Searching `iPhone 16 Pro` returns generic phone B-roll but not licensed Apple footage; using it with `iPhone` voiceover could imply endorsement → trademark.
- No `Content ID` or `Audible Magic` pre-flight. Auto-publish would upload and get struck.

**Recommend:**
- Add `copyright` hook: if `topic` or `full_script` contains >20 consecutive words matching known lyrics dataset (use local `copyright` corpus or simple n-gram against `full_script` length), return `400 copyright_check_failed`.
- In `stock.py:71 _is_blocklisted`, extend to `trademark_blocklist = ["apple","iphone","nike","disney"]` when `topic` is not about that brand (already have `SPLIT` etc — extend).
- In `/license` UI, remind user to add Pexels attribution in YouTube description (currently stored but not burned into MP4).
- Keep `commercial_ad` grounding (`backend/app/grounding.py`) which requires `validate_commercial_grounding` for ads — editorial currently skips, but YouTube `commercial` vs `editorial` should both get copyright pre-check.

## 3. Immediate mitigations in place

- `render.py:88 _is_blocklisted_asset` discards `2026` sale/umbrella tourist mismatches immediately (good).
- `visual_query.py:47 is_garbage_token` drops repeat artifacts.
- `AUTO_POST_ENABLED=false` in systemd drop-in prevents silent YouTube push (good — verify `tr '\0' '\n' < /proc/$(pgrep -f 8081)/environ | grep AUTO_POST` shows `false` on host, `true` in `backend/.env` but drop-in wins on host-native).

**Action:** Keep auto-publish off for Product Hunt (`/ph` draft only). Add NSFW blocklist before `POST /api/projects/{id}/generate-script` and copyright n-gram before `POST /api/projects/{id}/render/start` — 1-day patch, no extra deps.
