#!/usr/bin/env python3
"""
FacelessForge bulk pipeline — script → scenes → metadata → thumbnails → assets → voiceover → render
Mirrors bulk_create_fixed.py auth pattern. CLI for Dashboard bulk generate.

Usage:
  python3 /opt/facelessforge/deploy/bulk_render.py --limit 5            # test 5 oldest DRAFT projects (default, safe)
  python3 /opt/facelessforge/deploy/bulk_render.py --limit 5 --full     # include voiceover + thumbnail image + render queue
  python3 /opt/facelessforge/deploy/bulk_render.py --limit 100 --yes    # full 100 bulk (needs credits, ~4h with Ollama 15s fallback to DeepSeek)
  python3 /opt/facelessforge/deploy/bulk_render.py --limit 5 --skip-render  # script/scenes/metadata only (fastest demo)
  # via UI: Dashboard > Projects > Select All 100 > Generate  (this CLI is the backend batch; UI bulk button calls same /api batch)

Design:
- Ollama is PRIMARY (llama3:latest local, 15s timeout). Falls back to DeepSeek after 2×15s → 6s DeepSeek = ~36s/gen.
- qwen2.5 and llama3 both CPU slow for 750w script; DeepSeek is production backup for bulk.
- Sequential per project to avoid Mongo burst + LLM credit storm (same throttle as bulk_create_fixed 0.15s)
- Each stage validates before next; logs every change plainly per skill rules.

Dashboard equivalent isn't yet a UI button — ProjectsPage shows cards only — so this CLI IS the bulk generate path
until the UI bulk action is wired (see TODO at bottom).
"""
import argparse
import sys
import time
from pathlib import Path

import requests

API_BASE = "http://127.0.0.1:8081"
LOGIN_URL = f"{API_BASE}/api/auth/login"
PROJECTS_URL = f"{API_BASE}/api/projects"

EMAIL = "creator@facelessforge.io"
PASSWORD = "creator123"
USE_COOKIE = False
ACCESS_TOKEN_COOKIE = "paste_access_token_here"

def login_session() -> requests.Session:
    s = requests.Session()
    if USE_COOKIE:
        s.cookies.set("access_token", ACCESS_TOKEN_COOKIE, domain="127.0.0.1", path="/")
        r = s.get(f"{API_BASE}/api/auth/me", timeout=10)
        if r.status_code != 200:
            print(f"Cookie auth failed {r.status_code} {r.text[:200]}")
            sys.exit(1)
        print(f"Auth via cookie OK: {r.json().get('email')}")
        return s
    r = s.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD}, timeout=10)
    if r.status_code != 200:
        print(f"Login failed {r.status_code} {r.text}")
        print("Check creds or register POST /api/auth/register")
        sys.exit(1)
    print(f"Logged in as {r.json().get('email')} (token 24h)")
    return s

def fetch_projects(s: requests.Session):
    r = s.get(PROJECTS_URL, timeout=20)
    if r.status_code != 200:
        print(f"GET /api/projects failed {r.status_code} {r.text[:500]}")
        sys.exit(1)
    data = r.json()
    # API returns list or {projects: []} depending on version
    if isinstance(data, dict) and "projects" in data:
        return data["projects"]
    if isinstance(data, list):
        return data
    # also handle {project: ...} unwrap
    return data if isinstance(data, list) else []

def api_post(s, url, json_body=None, timeout=180):
    try:
        resp = s.post(url, json=json_body or {}, timeout=timeout)
        return resp
    except requests.exceptions.Timeout:
        return None
    except Exception as e:
        print(f"  network error {e}")
        return None

def process_project(s, proj, idx, total, args):
    pid = proj["id"]
    name = proj.get("name","")[:55]
    print(f"\n[{idx}/{total}] {pid} {name}")
    # quick inspect current state via GET /projects/{id}
    # we rely on POST stages returning full project view; no need extra fetch
    ok_stages = []
    fail_stages = []

    # 1. generate-script (750 words, 300s target)
    print("  → POST /generate-script ...", flush=True)
    r = api_post(s, f"{API_BASE}/api/projects/{pid}/generate-script", {}, timeout=180)
    if r is None:
        print("    timeout (180s) — will retry via fallback on next stage")
        fail_stages.append("script timeout")
    elif r.status_code in (200,201):
        j = r.json()
        script = (j.get("script") or j.get("project") or {})
        # API returns {project, script, scenes, ...}
        real_script = j.get("script") if isinstance(j.get("script"), dict) else j
        wc = (real_script.get("word_count") or len((real_script.get("full_script") or "").split())) if isinstance(real_script, dict) else "?"
        print(f"    ✓ script {r.status_code} words={wc} hook={(real_script.get('selected_hook') or '')[:60]}")
        ok_stages.append("script")
    else:
        print(f"    ✗ script {r.status_code} {r.text[:600]}")
        fail_stages.append(f"script {r.status_code}")
        if r.status_code >= 500:
            # don't proceed if script hard failed
            return ok_stages, fail_stages

    time.sleep(0.5)

    # 2. generate-scenes (10 scenes for 300s)
    print("  → POST /generate-scenes ...", flush=True)
    r = api_post(s, f"{API_BASE}/api/projects/{pid}/generate-scenes", {}, timeout=180)
    if r is None:
        print("    timeout")
        fail_stages.append("scenes timeout")
    elif r.status_code in (200,201):
        j = r.json()
        scenes = j.get("scenes") or []
        print(f"    ✓ scenes {r.status_code} count={len(scenes)}")
        ok_stages.append("scenes")
        # verify regeneration changes per topic (skill #1)
        if scenes and len(scenes) >= 1:
            nar = (scenes[0].get("narration_text") or "")[:80]
            if "instant noodles" in nar.lower() or "sleeping in the office" in nar.lower():
                print(f"    ⚠ generic business template detected in scenes — LLM fallback not topic-specific!")
            else:
                print(f"    first scene ok: {nar[:70]}…")
    else:
        print(f"    ✗ scenes {r.status_code} {r.text[:600]}")
        fail_stages.append(f"scenes {r.status_code}")

    time.sleep(0.5)

    # 3. generate-metadata (also auto-creates 3 thumbnail concepts)
    print("  → POST /generate-metadata ...", flush=True)
    r = api_post(s, f"{API_BASE}/api/projects/{pid}/generate-metadata", {}, timeout=180)
    if r is None:
        print("    timeout")
        fail_stages.append("metadata timeout")
    elif r.status_code in (200,201):
        j = r.json()
        meta = j.get("metadata") or {}
        title = (meta.get("selected_title") or "")[:60]
        print(f"    ✓ metadata {r.status_code} title={title}")
        ok_stages.append("metadata")
        # thumbnail concepts are auto-created; fetch to verify no "—"
        assets = j.get("assets") or []
        briefs = [a for a in assets if a.get("asset_type")=="thumbnail_concept"]
        print(f"    concepts {len(briefs)}")
        for b in briefs[:1]:
            br = b.get("brief") or b
            missing = [k for k in ["thumbnail_title_text","visual_composition","emotion_angle","background_idea","subject_focal_point","colour_direction","click_trigger","image_prompt"] if not (br.get(k) or "").strip()]
            if missing:
                print(f"    ⚠ brief missing {missing} — would show '—' in ThumbnailPanel")
            else:
                print(f"    brief ok: {br.get('thumbnail_title_text')[:50]}")
    else:
        print(f"    ✗ metadata {r.status_code} {r.text[:600]}")
        fail_stages.append(f"metadata {r.status_code}")

    time.sleep(0.5)

    # 4. generate-thumbnails endpoint (idempotent, ensures 3 concepts exist even if metadata failed)
    if not args.skip_thumbs:
        # fetch current view to get brief ids
        try:
            cr = s.get(f"{API_BASE}/api/projects/{pid}", timeout=20)
            if cr.status_code == 200:
                view = cr.json()
                assets = (view.get("assets") or view.get("project", {}).get("assets") or [])
                # Also try view direct assets
                if not assets and isinstance(view, dict) and "assets" in view:
                    assets = view["assets"]
                briefs = [a for a in assets if a.get("asset_type")=="thumbnail_concept"]
                if len(briefs) == 0:
                    print("  → POST /generate-thumbnails (no briefs → create) ...", flush=True)
                    r2 = api_post(s, f"{API_BASE}/api/projects/{pid}/generate-thumbnails", {}, timeout=180)
                    if r2 and r2.status_code in (200,201):
                        print(f"    ✓ thumbnails {r2.status_code}")
                        ok_stages.append("thumbnails")
                    elif r2:
                        print(f"    thumbnails {r2.status_code} {r2.text[:300]}")
        except Exception as e:
            print(f"  thumbnails check error {e}")

    # 5. auto-attach-assets (stock, 10 per scene for 300s, needs credits; fallback to caption frame if 402)
    if not args.skip_assets:
        print("  → POST /auto-attach-assets ...", flush=True)
        r = api_post(s, f"{API_BASE}/api/projects/{pid}/auto-attach-assets", {"replace_existing": False}, timeout=90)
        if r is None:
            print("    timeout")
            fail_stages.append("attach timeout")
        elif r.status_code in (200,201):
            j = r.json()
            print(f"    ✓ attach {r.status_code} attached={j.get('attached')} total={j.get('total')}")
            ok_stages.append("attach")
        elif r.status_code == 402:
            j = r.json()
            det = j.get("detail") if isinstance(j.get("detail"), dict) else j
            print(f"    ⚠ attach 402 credits exhausted remaining={det.get('remaining')} required={det.get('required')} — render will fallback to Pillow caption frames")
            # not fatal: render can proceed with caption frames
            ok_stages.append("attach (mock fallback)")
        else:
            print(f"    attach {r.status_code} {r.text[:500]}")
            fail_stages.append(f"attach {r.status_code}")

        time.sleep(0.5)

    # 6. voiceover full script (uses ElevenLabs; mock if key missing)
    if args.full or args.with_voiceover:
        print("  → POST /voiceover/generate-script (ElevenLabs) ...", flush=True)
        r = api_post(s, f"{API_BASE}/api/projects/{pid}/voiceover/generate-script", {}, timeout=90)
        if r is None:
            print("    timeout")
            fail_stages.append("voiceover timeout")
        elif r.status_code in (200,201):
            j = r.json()
            assets = j.get("assets") or []
            vos = [a for a in assets if a.get("asset_type")=="voiceover_audio"]
            print(f"    ✓ voiceover {r.status_code} voiceovers={len(vos)}")
            ok_stages.append("voiceover")
        else:
            print(f"    voiceover {r.status_code} {r.text[:500]}")
            # voiceover missing → render will fallback to silent track with WARNING (skill #2) but we want audible
            fail_stages.append(f"voiceover {r.status_code}")

        time.sleep(0.5)

    # 7. thumbnail image generate + select (needs GEMINI or mock)
    if args.full:
        try:
            cr = s.get(f"{API_BASE}/api/projects/{pid}", timeout=20)
            if cr.status_code == 200:
                view = cr.json()
                assets = view.get("assets") or []
                briefs = [a for a in assets if a.get("asset_type")=="thumbnail_concept"]
                if briefs:
                    brief_id = briefs[0]["id"]
                    print(f"  → POST /thumbnails/{brief_id[:8]}…/generate variants=1 ...", flush=True)
                    r = api_post(s, f"{API_BASE}/api/projects/{pid}/thumbnails/{brief_id}/generate", {"variants": 1}, timeout=60)
                    if r and r.status_code in (200,201):
                        j = r.json()
                        gens = [a for a in (j.get("assets") or []) if a.get("asset_type")=="generated_thumbnail" and a.get("brief_asset_id")==brief_id]
                        print(f"    ✓ thumb image {r.status_code} gens={len(gens)}")
                        if gens:
                            gid = gens[0]["id"]
                            print(f"  → POST /thumbnails/{gid[:8]}…/select ...", flush=True)
                            r2 = api_post(s, f"{API_BASE}/api/projects/{pid}/thumbnails/{gid}/select", {}, timeout=20)
                            if r2 and r2.status_code in (200,201):
                                print(f"    ✓ thumb select {r2.status_code}")
                                ok_stages.append("thumb select")
                            else:
                                print(f"    thumb select {r2.status_code if r2 else 'timeout'} {(r2.text[:300] if r2 else '')}")
                        ok_stages.append("thumb image")
                    elif r:
                        print(f"    thumb image {r.status_code} {r.text[:400]} (mock fallback will use Pillow)")
                    else:
                        print("    thumb image timeout")
        except Exception as e:
            print(f"  thumb image error {e}")

    # 8. render queue (requires thumbnail selected + voiceover)
    if args.full and not args.skip_render:
        print("  → POST /render/start (queue) ...", flush=True)
        # preflight first to get checklist
        try:
            pf = s.get(f"{API_BASE}/api/projects/{pid}/render/preflight", timeout=20)
            if pf.status_code == 200:
                chk = pf.json()
                if not chk.get("ok"):
                    print(f"    preflight issues: {chk.get('issues')}")
                else:
                    print("    preflight ok")
        except Exception:
            pass
        r = api_post(s, f"{API_BASE}/api/projects/{pid}/render/start", {"burn_subtitles": True, "aspect_ratio": "9:16"}, timeout=30)
        if r is None:
            print("    render timeout")
            fail_stages.append("render timeout")
        elif r.status_code in (200,201):
            j = r.json()
            print(f"    ✓ render queued {r.status_code} job={j.get('id') or j.get('job_id')} status={j.get('status')}")
            ok_stages.append("render")
        elif r.status_code == 400:
            # prerequisites missing -> show checklist
            try:
                det = r.json().get("detail") if isinstance(r.json().get("detail"), dict) else {}
                print(f"    render 400 prerequisites: {det.get('issues') or r.text[:500]}")
            except:
                print(f"    render 400 {r.text[:500]}")
            fail_stages.append("render 400")
        elif r.status_code == 429:
            print(f"    render 429 rate limited (5/hour)")
            fail_stages.append("render 429")
        else:
            print(f"    render {r.status_code} {r.text[:500]}")
            fail_stages.append(f"render {r.status_code}")

    print(f"  done stages ok={ok_stages} fail={fail_stages}")
    return ok_stages, fail_stages

def main():
    ap = argparse.ArgumentParser(description="FacelessForge bulk generate — script→scenes→metadata→assets→voiceover→render")
    ap.add_argument("--limit", type=int, default=5, help="max projects to process (default 5)")
    ap.add_argument("--yes", action="store_true", help="skip confirmation for large limits")
    ap.add_argument("--full", action="store_true", help="include voiceover + thumbnail image + render (full E2E). Default with --limit 5 is script/scenes/metadata/attach only; use --full for E2E")
    ap.add_argument("--skip-render", action="store_true", help="skip render queue even with --full")
    ap.add_argument("--skip-assets", action="store_true", help="skip auto-attach")
    ap.add_argument("--skip-thumbs", action="store_true", help="skip thumbnail brief check")
    ap.add_argument("--with-voiceover", action="store_true", help="generate full-script voiceover even without --full")
    ap.add_argument("--order", choices=["oldest","newest"], default="oldest", help="which DRAFT projects first")
    ap.add_argument("--dry-run", action="store_true", help="list what would be processed, no API calls")
    ap.add_argument("--project-id", type=str, default=None, help="process single project id")
    args = ap.parse_args()

    if args.limit > 5 and not args.yes and not args.dry_run:
        print(f"Limit {args.limit} >5 needs --yes to confirm (estimated ~{args.limit*2.4/60:.1f}h with --full, ~{args.limit*0.6/60:.1f}h without). Use --dry-run to preview.")
        sys.exit(1)

    s = login_session()
    projects = fetch_projects(s)

    if args.project_id:
        target = [p for p in projects if p["id"]==args.project_id]
        if not target:
            print(f"Project {args.project_id} not found among {len(projects)}")
            sys.exit(1)
        projects = target
    else:
        # filter to those needing work: status DRAFT or missing script would be DRAFT
        # For demo, take oldest DRAFT first (bulk 100 are oldest DRAFT from 2026-09-11)
        # If order newest, reverse
        # Sort by created_at
        def get_created(p):
            return p.get("created_at") or p.get("updated_at") or ""
        projects_sorted = sorted(projects, key=get_created)
        if args.order == "newest":
            projects_sorted = list(reversed(projects_sorted))
        # Prefer DRAFT / projects without script; but for bulk we want the 100 DRAFT bulk
        drafts = [p for p in projects_sorted if p.get("status")=="DRAFT"]
        # fallback to all if no drafts
        pool = drafts if drafts else projects_sorted
        projects = pool

    total_in_pool = len(projects)
    # also show counts
    drafts_cnt = len([p for p in fetch_projects(s) if p.get("status")=="DRAFT"]) if not args.dry_run else total_in_pool
    print(f"Pool: {total_in_pool} projects (DRAFT prioritized), limit {args.limit}, order {args.order}, full={args.full}")
    if args.dry_run:
        for i, p in enumerate(projects[:args.limit]):
            print(f" [{i+1}] {p['id']} {p.get('name','')[:60]} status={p.get('status')} created={p.get('created_at')}")
        print("dry-run done")
        return

    to_process = projects[:args.limit]
    print(f"Processing {len(to_process)} projects sequentially (throttled, ~36s/gen fallback to DeepSeek, ~2.4min/proj without render)")
    print(f"API: {API_BASE}  throttle 0.5s between stages, 0.15s between projects (see bulk_create_fixed)")

    ok_total = 0
    fail_total = 0
    for idx, proj in enumerate(to_process, start=1):
        ok, fail = process_project(s, proj, idx, len(to_process), args)
        if not fail:
            ok_total += 1
        else:
            fail_total += 1
        # gentle throttle between projects
        if idx < len(to_process):
            time.sleep(0.15)

    print(f"\nDone: total={len(to_process)} ok={ok_total} fail={fail_total}")
    if args.limit == 5 and not args.full:
        print("Tip: rerun with --full to also generate voiceover + thumbnail image + render queue (needs thumb select + VO).")
        print("For 100 bulk: python3 /opt/facelessforge/deploy/bulk_render.py --limit 100 --yes --full  (or Dashboard > Projects > Select All 100 > Generate once UI bulk is wired)")

    # Dashboard TODO note
    # To wire UI bulk: add checkbox + Select All + Bulk Generate button in ProjectsPage.jsx that POSTs to new
    # /api/projects/batch/generate {project_ids, stages: ["script","scenes","metadata"]} sequentially.

if __name__ == "__main__":
    main()
