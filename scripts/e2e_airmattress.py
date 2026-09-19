"""E2E driver: air-mattress topic, 60s render, full pipeline via local API.

Not part of the shipped codebase — verification helper for the
fix/visuals-v2-force-video branch.
"""
import json
import sys
import time

import httpx

BASE = "http://localhost:8081/api"
EMAIL = "e2e-visuals-v2@example.com"
PW = "testpass123"

c = httpx.Client(base_url=BASE, timeout=httpx.Timeout(600.0, connect=10.0))


def step(name, r):
    print(f"[{name}] HTTP {r.status_code}", flush=True)
    if r.status_code >= 400:
        print(r.text[:800], flush=True)
        sys.exit(1)
    return r.json()


# ---- auth ----
r = c.post("/auth/register", json={"name": "E2E Visuals", "email": EMAIL,
                                   "password": PW, "role": "creator"})
if r.status_code == 400:
    r = c.post("/auth/login", json={"email": EMAIL, "password": PW})
if r.status_code >= 400:
    print(r.text[:500])
    sys.exit(1)
token = r.cookies.get("access_token")
if not token:
    print("ERROR: no access_token cookie in auth response")
    sys.exit(1)
c.headers["Authorization"] = f"Bearer {token}"
print("[auth] HTTP 200", flush=True)

# ---- project ----
view = step("create_project", c.post("/projects", json={
    "name": "Air Mattress E2E visuals-v2",
    "niche": "business stories",
    "topic": "How 3 guys turned renting a $40 air mattress in their San Francisco "
             "apartment into Airbnb, a 75 billion dollar company",
    "audience": "aspiring entrepreneurs",
    "tone": "documentary",
    "target_duration": 60,
    "visual_style": "cinematic b-roll",
}))
pid = view.get("project", view)["id"]
print("project_id:", pid, flush=True)

# ---- generation ----
# llama3 often under-shoots the requested length — retry until the script
# is long enough for a ~60s voiceover (≥120 words), keep the longest try.
best_words = 0
for attempt in range(4):
    view = step("generate_script", c.post(f"/projects/{pid}/generate-script"))
    words = len(((view.get("script") or {}).get("full_script") or "").split())
    print(f"  script attempt {attempt + 1}: {words} words", flush=True)
    best_words = max(best_words, words)
    if words >= 120:
        break
print(f"script words: {best_words}", flush=True)
view = step("generate_scenes", c.post(f"/projects/{pid}/generate-scenes"))
scenes = view.get("scenes") or []
print(f"scenes: {len(scenes)}", flush=True)
for s in scenes:
    print(f"  scene {s.get('scene_number')}: terms={s.get('search_terms')} "
          f"caption={s.get('caption_text')!r}", flush=True)
step("generate_metadata", c.post(f"/projects/{pid}/generate-metadata"))
view = step("generate_thumbnails", c.post(f"/projects/{pid}/generate-thumbnails"))

# ---- thumbnail image: generate from first concept, then select ----
concepts = [a for a in (view.get("assets") or [])
            if a.get("asset_type") == "thumbnail_concept"]
if concepts:
    view = step("thumbnail_image", c.post(
        f"/projects/{pid}/thumbnails/{concepts[0]['id']}/generate", json={"variants": 1}))
    thumbs = [a for a in (view.get("assets") or [])
              if a.get("asset_type") == "generated_thumbnail"]
    if thumbs:
        step("thumbnail_select", c.post(
            f"/projects/{pid}/thumbnails/{thumbs[0]['id']}/select"))
else:
    print("WARNING: no thumbnail concepts", flush=True)

# ---- voiceover (full script) + select ----
view = step("voiceover", c.post(f"/projects/{pid}/voiceover/generate-script", json={}))
voices = [a for a in (view.get("assets") or [])
          if a.get("asset_type") == "voiceover_audio" and not a.get("scene_id")]
if not voices:
    print("ERROR: no full voiceover asset", flush=True)
    sys.exit(1)
step("voiceover_select", c.post(f"/projects/{pid}/voiceover/{voices[0]['id']}/select"))

# ---- stock attach (videos only — 'have=1' per scene; top-up must fill) ----
res = step("auto_attach", c.post(f"/projects/{pid}/auto-attach-assets",
                                 json={"media_type": "videos", "replace_existing": True}))
print(f"auto_attach: attached={res.get('attached')} skipped={res.get('skipped')} "
      f"failed={res.get('failed')} mock={res.get('mock')}", flush=True)

# ---- preflight + render ----
pre = step("preflight", c.get(f"/projects/{pid}/render/preflight"))
print("preflight:", json.dumps(pre)[:400], flush=True)
job = step("render_start", c.post(f"/projects/{pid}/render/start", json={}))
job_id = job["id"]
print("job_id:", job_id, flush=True)

deadline = time.time() + 1500
while time.time() < deadline:
    time.sleep(10)
    j = c.get(f"/projects/{pid}/render/jobs/{job_id}").json()
    st = j.get("status")
    print(f"  job {st} {j.get('current_step')} {j.get('progress')}%", flush=True)
    if st in ("done", "failed", "failed_verification", "cancelled"):
        print(json.dumps(j, indent=1)[:4000], flush=True)
        break
else:
    print("TIMEOUT waiting for render", flush=True)
    sys.exit(2)
