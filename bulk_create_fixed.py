#!/usr/bin/env python3
"""
Fixed bulk create for FacelessForge - correct schema + auth handling
Ref: backend/app/models.py:52 ProjectCreate, backend/app/routes/api.py:402 create_project, backend/app/auth.py:79 _read_token

USAGE:
1. scp business_100_topics.csv root@YOUR_SERVER:/opt/facelessforge/deploy/business_100_topics.csv
2. python3 /opt/facelessforge/deploy/bulk_create_fixed.py
"""
import csv, requests, sys, time, pathlib

# --- CONFIG ---
CSV_CANDIDATES = [
    "/opt/facelessforge/deploy/business_100_topics.csv",
    "/opt/facelessforge/business_100_topics.csv",
    "./business_100_topics.csv",
]
API_BASE = "http://127.0.0.1:8081"
LOGIN_URL = f"{API_BASE}/api/auth/login"
CREATE_URL = f"{API_BASE}/api/projects"

# Verified working admin creds (from DB check 2026-09-11). Change if you create your own creator.
EMAIL = "creator@facelessforge.io"
PASSWORD = "creator123"
# Alternative: if you prefer cookie from browser DevTools > Application > Cookies > access_token
# set USE_COOKIE=True and paste value below
USE_COOKIE = False
ACCESS_TOKEN_COOKIE = "paste_access_token_here"

def find_csv():
    for p in CSV_CANDIDATES:
        if pathlib.Path(p).exists():
            return p
    return None

def map_row(row):
    # CSV headers from user prompt: ID, Title, Hook (first 15s), Tone, Voice Style, Visual Style, Target Duration, Goal, CTA Action, Core Business Lesson
    # API required: name (1-160), niche (3-120), topic (10-1000), audience (1-200), tone (1-80), target_duration (5-3600)  -> models.py:52
    title = (row.get("Title") or row.get("title") or row.get("Topic") or "").strip() or "Untitled Business Lesson"
    hook = (row.get("Hook (first 15s)") or row.get("Hook") or row.get("hook") or "").strip()
    core = (row.get("Core Business Lesson") or row.get("core_lesson") or "").strip()
    tone = (row.get("Tone") or row.get("tone") or "informative").strip() or "informative"
    voice = (row.get("Voice Style") or row.get("voice_style") or "neutral male narrator").strip() or "neutral male narrator"
    visual = (row.get("Visual Style") or row.get("visual_style") or "cinematic b-roll").strip() or "cinematic b-roll"
    goal = (row.get("Goal") or "").strip() or "ads + affiliate"
    cta = (row.get("CTA Action") or row.get("CTA") or "").strip() or "subscribe"

    # target_duration can be "60s", "60", etc.
    raw_dur = str(row.get("Target Duration") or row.get("target_duration") or "60").strip().lower().replace("s","")
    try:
        dur = int(float(raw_dur))
    except:
        dur = 60
    dur = max(5, min(dur, 3600))

    # Build niche from core lesson keywords or default Business Education
    niche = core[:60] if len(core) >=3 else (goal[:40] if len(goal)>=3 else "Business Education")
    if len(niche) < 3:
        niche = "Business Education"
    niche = niche[:120]

    # Topic needs 10..1000 chars, combine hook + title + core for richness (prevents generic template fallback)
    topic = f"{title}: {hook} — {core}" if hook and core else f"{title} — {core}" if core else title
    if len(topic) < 10:
        topic = f"{title} business lesson on {core or goal}"
    topic = topic[:998]

    audience = "aspiring entrepreneurs and small business owners"  # default, audience required 1..200
    # optionally derive audience from row if column exists
    for k in ["Audience","audience","Target Audience"]:
        if row.get(k) and len(row.get(k).strip())>=3:
            audience = row.get(k).strip()[:200]
            break

    return {
        "name": title[:160],
        "niche": niche,
        "topic": topic,
        "audience": audience,
        "tone": tone[:80],
        "target_duration": dur,
        "voice_style": voice,
        "visual_style": visual,
        "cta_goal": cta[:80],
        "monetisation_intent": goal[:120],
    }

def main():
    csv_path = find_csv()
    if not csv_path:
        print(f"CSV not found. Tried: {CSV_CANDIDATES}")
        print("Upload via: scp business_100_topics.csv root@SERVER:/opt/facelessforge/deploy/business_100_topics.csv")
        sys.exit(1)
    print(f"Using CSV: {csv_path}")
    print(f"API: {CREATE_URL} (systemd facelessforge on 127.0.0.1:8081, see /etc/systemd/system/facelessforge.service:8)")

    s = requests.Session()
    if USE_COOKIE:
        s.cookies.set("access_token", ACCESS_TOKEN_COOKIE, domain="127.0.0.1", path="/")
        # verify
        r = s.get(f"{API_BASE}/api/auth/me")
        if r.status_code != 200:
            print(f"Cookie auth failed {r.status_code} {r.text[:200]}")
            print("Tip: copy access_token from DevTools Application > Cookies (httpOnly) or use Network header Authorization: Bearer <token>")
            sys.exit(1)
        print(f"Auth via cookie OK: {r.json().get('email')}")
    else:
        r = s.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD})
        if r.status_code != 200:
            print(f"Login failed {r.status_code} {r.text}")
            print("Check creds or register: POST /api/auth/register {name,email,password,role:creator}")
            sys.exit(1)
        print(f"Logged in as {r.json().get('email')} token expires in 24h (auth.py:11)")

    # quick schema sanity: ensure auth headers fallback supported (auth.py:79 reads access_token cookie OR Bearer)
    # Session now holds both access_token + refresh_token

    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        print(f"Headers: {reader.fieldnames}")
        count=0
        ok=0
        fail=0
        for row in reader:
            count+=1
            payload = map_row(row)
            # optional: log mapping for first row
            if count==1:
                print(f"Example payload: {payload}")
            try:
                resp = s.post(CREATE_URL, json=payload, timeout=20)
            except Exception as e:
                print(f"[{row.get('ID','?')}] {payload['name'][:40]} -> network error {e}")
                fail+=1
                continue
            if resp.status_code in (200,201):
                pid = resp.json().get("id","")
                print(f"[{row.get('ID',count)}] {resp.status_code} {payload['name'][:50]} -> {pid}")
                ok+=1
            else:
                print(f"[{row.get('ID',count)}] {resp.status_code} FAIL {payload['name'][:50]} -> {resp.text[:400]}")
                fail+=1
            # gentle throttle to avoid Mongo burst + LLM credit storm
            time.sleep(0.15)
        print(f"\nDone: total={count} ok={ok} fail={fail}")

if __name__=="__main__":
    main()
