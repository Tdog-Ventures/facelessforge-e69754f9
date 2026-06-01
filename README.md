# FacelessForge 🎬

**Automated faceless YouTube video generator. Topic in. Professional video out. Posted daily. Zero involvement.**

Built by [ETHINX Solutions](https://ethinx.solutions) — sovereign infrastructure for those who refuse to be managed.

---

## What It Does

FacelessForge takes a topic, generates a complete professional video, and uploads it to YouTube automatically. Every day. While you sleep.

**Input:** A topic title (e.g. *"How a $40 air mattress became a $100B+ travel empire"*)

**Output:** A fully produced YouTube-ready MP4 with:
- AI-generated script (business empire case study format)
- Professional voiceover via ElevenLabs
- Cinematic stock footage from Pexels — semantic keyword matching per scene
- Word-sync burned-in subtitles via Whisper STT
- Background music bed at broadcast levels (-18 dB)
- Auto-uploaded to YouTube via YouTube Data API v3

**Niche:** Business empire case studies — *"How X turned $500 into a billion dollar company"*

**Target RPM:** $8–15 USD (business/finance niche)

---

## Proof of Concept

The [Wealth Mode Mentality](https://youtube.com) channel grew from 300 to 66,000 subscribers in 60 days using the same format. FacelessForge automates this entire pipeline.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Script Generation | DeepSeek (deepseek-chat) |
| Voiceover | ElevenLabs — eleven_multilingual_v2 |
| Footage | Pexels API — semantic scene-aware search |
| Subtitles | Whisper STT — word-level timestamp sync |
| Assembly | ffmpeg — 1920×1080 H.264 + AAC |
| Storage | Cloudflare R2 — videos.ethinx.solutions CDN |
| Upload | YouTube Data API v3 |
| Scheduler | Hetzner VPS cron — 10AM UTC daily |
| Hosting | Google Cloud Run (australia-southeast1) |

---

## Pipeline Flow

```
Topic Queue (/root/topics.txt)
      │
      ▼
Script Generation (DeepSeek)
      │
      ▼
Voiceover Generation (ElevenLabs)
      │
      ▼
Scene Planning + Keyword Extraction
      │
      ▼
Footage Search (Pexels — semantic + visual tone aware)
      │
      ├── Motion check (ffprobe)
      ├── Quality fallthrough (uhd → hd → sd → ld)
      └── Auto-retry on rejection
      │
      ▼
Subtitle Generation (Whisper STT — word-level sync)
      │
      ▼
Assembly (ffmpeg — video + voiceover + music bed + subtitles)
      │
      ▼
Upload to R2 (videos.ethinx.solutions)
      │
      ▼
YouTube Upload (@Tdog-u8l)
      │
      ▼
Topic removed from queue → next topic queued
```

---

## Infrastructure

- **GCP Project:** ethinx-prime (australia-southeast1)
- **Hetzner VPS:** 91.99.162.243 — render engine, cron scheduler
- **GCS Bucket:** facelessforge-videos
- **CDN:** videos.ethinx.solutions (Cloudflare R2)
- **YouTube Channel:** @Tdog-u8l

---

## Topic Queue

Topics live at `/root/topics.txt` on the Hetzner server. One topic per line. The scheduler picks the next topic each day and removes it after successful upload.

Current queue capacity: 55+ topics (approx 55 days of daily uploads).

Example topics:
```
The $40 Air Mattress Side Hustle That Became a $100B+ Travel Empire
The Broken Payments Problem That Created Stripe's $100B+ Infrastructure
How a $12 Annoyance Turned Into Uber's $80B+ Global Logistics Network
```

---

## Daily Scheduler

```bash
# Cron — runs at 10AM UTC daily
0 10 * * * /root/run_pipeline.sh >> /root/ethinx_pipeline.log 2>&1
```

**Flow:** Pick topic → render → poll status → download MP4 → upload to YouTube → remove topic → log result

---

## Video Quality Standards

Every render must pass:
- ✅ 16/16 real Pexels video clips (zero static image fallbacks)
- ✅ Real ElevenLabs voiceover (not mock TTS)
- ✅ Word-sync subtitles matching narration
- ✅ Background music bed at -18 dB
- ✅ Video duration matches audio duration exactly
- ✅ Public CDN URL on videos.ethinx.solutions

---

## Related Products

| Product | Purpose |
|---|---|
| **VideoForge** | Done-for-you video production for local businesses — $500/month |
| **FacelessForge** | This — automated YouTube passive income |
| **ETHINX OS** | Central command platform |
| **PromptForge** | Autonomous agent operating system |

---

## Brand

**ETHINX Solutions** — *Imagine Ethinx*

Built by Troy (Tdog) — solo bootstrapped founder, Adelaide, South Australia.
Unfairly dismissed. Moved into a camper trailer. Ran off a generator. Built an AI company.

[ethinx.solutions](https://ethinx.solutions)

---

*Confidential — ETHINX Solutions | Updated: June 2026*# Here are your Instructions
