#!/usr/bin/env python3
"""One-shot assembly: first verified MP4 out of an existing project.

Runs on the host (Hetzner 91.99.162.243) next to the deployable backend and
re-uses the app's own code — no rebuilds, no new repos:

  1. loads env in the same order as facelessforge.service: backend-adjacent default
     env files, then the production secrets vault (vault wins)
  2. thumbnail asset  -> assets + projects.selected_thumbnail_asset_id
     full voiceover   -> assets + projects.selected_voiceover_asset_id
  3. Pexels auto-attach per scene (best effort, never blocks)
  4. render via app.render.validate_prerequisites + app.render.queue_render —
     the exact functions behind POST /api/projects/{id}/render/start
  5. tails facelessforge.service journal and runs app.verify until it passes
  6. if the-studio.service is failed, logs its journal tail (non-blocking)

Usage (backend dir, project venv):
  cd /opt/facelessforge/deploy/backend
  ./venv/bin/python3 ../assemble_first_mp4.py 1d8e5af3-0e41-4888-b245-d972acbd43d2

Exit codes: 0 verified, 2 prereq/inputs, 3 render failed, 4 verify not passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SECRETS = "/opt/ethinx/deployment/secrets-vault/facelessforge-deploy.env"
LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[assemble] {msg}"
    print(line, flush=True)
    LOG.append(line)


# --------------------------------------------------------------------------- env

def load_env_file(path: Path) -> int:
    """Load KEY=VALUE pairs without printing any value. Later files win."""
    if not path.exists():
        log(f"secrets file not found: {path}")
        return 0
    loaded = 0
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        else:  # strip trailing inline comment on unquoted values
            hash_at = val.find(" #")
            if hash_at > 0:
                val = val[:hash_at].rstrip()
        os.environ[key] = val
        loaded += 1
    log(f"loaded {loaded} keys from {path}")
    return loaded


def load_service_env(backend: Path) -> None:
    """Mirror the systemd EnvironmentFile order: defaults first, vault last."""
    defaults = [
        backend.parent / ".env.facelessforge",
        backend / ".env.facelessforge",
        backend / ".env",
    ]
    for cand in defaults:
        if cand.is_file():
            load_env_file(cand)
    load_env_file(Path(os.environ.get("FF_SECRETS_FILE", DEFAULT_SECRETS)))


def find_backend() -> Path:
    """Backend dir that can be imported as `app`."""
    here = Path(__file__).resolve()
    candidates = [
        Path(os.environ.get("FF_BACKEND_DIR", "")),
        here.parent / "backend",
        here.parent,
        Path("/opt/facelessforge/deploy/backend"),
    ]
    for cand in candidates:
        if str(cand) and (cand / "app" / "db.py").is_file():
            return cand.resolve()
    sys.exit("[assemble] FATAL: could not locate the backend/app package "
             "(set FF_BACKEND_DIR)")


def now() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------- misc helpers

def journal_tail(unit: str, since: str | None = None, lines: int = 40) -> list[str]:
    cmd = ["journalctl", "-u", unit, "--no-pager", "-o", "short-iso"]
    if since:
        cmd.append(f"--since={since}")
    else:
        cmd += ["-n", str(lines)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return [ln for ln in proc.stdout.splitlines() if ln.strip()]
    except Exception as exc:  # noqa: BLE001 — logs must never break the run
        return [f"(journal tail failed: {exc})"]


def unit_failed(unit: str) -> bool:
    try:
        proc = subprocess.run(["systemctl", "is-failed", unit],
                              capture_output=True, text=True, timeout=20)
        failed = proc.stdout.strip() == "failed"
        if failed:
            log(f"{unit} is failed — attaching journal tail (non-blocking)")
            for ln in journal_tail(unit, lines=60):
                log(f"  {unit}| {ln}")
        else:
            log(f"{unit}: {proc.stdout.strip() or 'unknown (not loaded?)'}")
        return failed
    except Exception as exc:  # noqa: BLE001
        log(f"{unit}: status check skipped ({exc})")
        return False


class JournalFollower:
    """Incremental journalctl tail so render logs stream while we wait."""

    def __init__(self, unit: str):
        self.unit = unit
        self.since = now().strftime("%Y-%m-%d %H:%M:%S")
        self.seen = 0

    def pump(self) -> None:
        lines = journal_tail(self.unit, since=self.since)
        new = lines[self.seen:]
        self.seen = len(lines)
        for ln in new:
            if "Traceback" in ln or "ERROR" in ln or "error" in ln or "failed" in ln.lower():
                log(f"  {self.unit}| {ln}")


async def download(url: str, dest: Path) -> Path:
    def _get() -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "facelessforge-assemble/1"})
        with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)

    await asyncio.to_thread(_get)
    return dest


# ----------------------------------------------------------------- main pipeline

async def run(project_id: str, *, force_regen: bool, render_timeout: int) -> int:
    backend = find_backend()
    log(f"backend dir: {backend}")

    load_service_env(backend)
    if not os.environ.get("MONGO_URL") or not os.environ.get("DB_NAME"):
        log("MONGO_URL or DB_NAME missing after loading service env — cannot continue")
        return 2

    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))

    from app import db as dbmod
    from app import generation as gen
    from app import render as render_service
    from app import tts as tts_service

    # Optional helpers: older/stale release trees may lack them — degrade, never crash.
    try:
        from app.models import AutoAttachRequest
    except Exception as exc:  # noqa: BLE001
        AutoAttachRequest = None
        log(f"auto-attach disabled (AutoAttachRequest unavailable: {type(exc).__name__})")
    try:
        from app.routes.api import auto_attach_assets
    except Exception as exc:  # noqa: BLE001
        auto_attach_assets = None
        log(f"auto-attach disabled (routes.api import failed: {type(exc).__name__}: {exc})"[:300])
    try:
        from app.routes.api import _voice_style_for
    except Exception:  # noqa: BLE001
        _voice_style_for = None
    try:
        from app.thumbnail_images import generate_thumbnail_images
        from app.thumbnail_images import provider_info as thumb_provider
    except Exception as exc:  # noqa: BLE001
        generate_thumbnail_images = None
        thumb_provider = None
        log(f"thumbnail image generation unavailable ({type(exc).__name__}) — will reuse existing")

    log(f"mongo target host={_host_of(os.environ['MONGO_URL'])} db={os.environ['DB_NAME']}")
    db = dbmod.get_db()

    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    if not project:
        log(f"project {project_id} not found in {os.environ['DB_NAME']}.projects")
        return 2
    owner_id = project.get("user_id")
    owner = await db.users.find_one({"id": owner_id}, {"_id": 0}) or {}
    log(f"project '{project.get('name')}' status={project.get('status')} "
        f"target={project.get('target_duration')}s mode={project.get('production_mode') or 'standard'} "
        f"owner_role={owner.get('role')}")

    script = await db.scripts.find_one({"project_id": project_id}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project_id}).sort("scene_number", 1).to_list(500)
    metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    if not script or not (script.get("full_script") or "").strip():
        log("no script with full_script — a script must exist before asset assembly")
        return 2
    if not scenes:
        log("no scenes — a scene plan must exist before asset assembly")
        return 2
    if not metadata and project.get("production_mode") != "commercial_ad":
        log("WARNING: no metadata package; prereq check will flag it")

    summary: dict = {"project_id": project_id, "steps": {}}

    # ------------------------------------------------------------ 2a. thumbnail
    assets = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(1000)
    existing_thumb = next(
        (a for a in assets
         if a.get("asset_type") == "generated_thumbnail"
         and a.get("id") == project.get("selected_thumbnail_asset_id")), None)
    if existing_thumb and not force_regen:
        log(f"thumbnail already selected: {existing_thumb['id']} (reusing)")
        thumb_asset = existing_thumb
    else:
        concepts = [a for a in assets if a.get("asset_type") == "thumbnail_concept" and a.get("brief")]
        if not concepts:
            log("generating thumbnail concepts")
            briefs = await gen.generate_thumbnail_concepts(project, script)
            for i, brief in enumerate(briefs, start=1):
                doc = {
                    "id": str(uuid.uuid4()), "project_id": project_id,
                    "name": f"Thumbnail Concept #{i}", "asset_type": "thumbnail_concept",
                    "file_path": None, "tags": ["thumbnail", "concept"],
                    "status": "ready", "brief": brief,
                    "created_at": now(), "updated_at": now(),
                }
                await db.assets.insert_one(dict(doc))
                concepts.append(doc)
        brief = concepts[0]["brief"]
        payloads: list = []
        if generate_thumbnail_images is not None:
            log(f"rendering thumbnail image (provider={_safe(thumb_provider)})")
            payloads = await generate_thumbnail_images(
                project, brief, variants=1, project_id=project_id)
        if not payloads:
            reuse = next((a for a in assets if a.get("asset_type") == "generated_thumbnail"), None)
            if not reuse:
                log("thumbnail generation returned no image (providers failed, mock disabled)")
                return 2
            log(f"reusing existing generated thumbnail {reuse['id']} instead")
            thumb_asset = reuse
        else:
            thumb_asset = payloads[0]
            thumb_asset.update({"status": "selected", "created_at": now(), "updated_at": now()})
            await db.assets.update_many(
                {"project_id": project_id, "asset_type": "generated_thumbnail",
                 "status": "selected"},
                {"$set": {"status": "generated", "updated_at": now()}})
            await db.assets.insert_one(dict(thumb_asset))
            log(f"inserted thumbnail asset {thumb_asset['id']} source={thumb_asset.get('source')}")
    await db.projects.update_one(
        {"id": project_id},
        {"$set": {"selected_thumbnail_asset_id": thumb_asset["id"], "updated_at": now()}})
    summary["steps"]["thumbnail_asset_id"] = thumb_asset["id"]
    log(f"selected_thumbnail_asset_id = {thumb_asset['id']}")

    # ------------------------------------------------------------ 2b. voiceover
    assets = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(1000)
    full_voice = next(
        (a for a in assets
         if a.get("asset_type") == "voiceover_audio" and not a.get("scene_id")
         and a.get("id") == project.get("selected_voiceover_asset_id")), None)
    if full_voice and not force_regen and float(full_voice.get("duration") or 0) > 0:
        log(f"full voiceover already selected: {full_voice['id']} "
            f"({float(full_voice.get('duration') or 0):.2f}s, provider={full_voice.get('provider')})")
        voice_asset = full_voice
    else:
        text = (script.get("full_script") or "").strip()
        voice = (_voice_style_for(project, None) if _voice_style_for
                 else (project.get("voice_style") or "neutral male narrator"))
        asset_id = str(uuid.uuid4())
        log(f"generating full voiceover: {len(text.split())} words, voice_style={voice}")
        payload = await tts_service.generate_voiceover(
            text=text, voice_style=voice, tone=None, project_id=project_id,
            asset_id=asset_id, scene_id=None, name_suffix="(Full script)")
        dur = float(payload.get("duration") or 0)
        if dur <= 0:
            log("voiceover provider returned no measurable duration — aborting before render")
            return 2
        payload["text_excerpt"] = text[:240]
        payload["status"] = "selected"
        payload["created_at"] = now()
        payload["updated_at"] = now()
        await db.assets.update_many(
            {"project_id": project_id, "asset_type": "voiceover_audio",
             "scene_id": None, "status": "selected"},
            {"$set": {"status": "generated", "updated_at": now()}})
        await db.assets.insert_one(dict(payload))
        voice_asset = payload
        log(f"inserted voiceover asset {asset_id} duration={dur:.2f}s "
            f"provider={payload.get('provider')} mock={payload.get('mock')}")
    await db.projects.update_one(
        {"id": project_id},
        {"$set": {"selected_voiceover_asset_id": voice_asset["id"], "updated_at": now()}})
    summary["steps"]["voiceover_asset_id"] = voice_asset["id"]
    summary["steps"]["voiceover_seconds"] = round(float(voice_asset.get("duration") or 0), 2)
    log(f"selected_voiceover_asset_id = {voice_asset['id']}")

    # ------------------------------------------------- 3. Pexels (best effort)
    if auto_attach_assets is None or AutoAttachRequest is None:
        log("auto-attach unavailable on this tree — skipping (not blocking the render)")
    else:
        try:
            attached = await asyncio.wait_for(
                auto_attach_assets(project_id, AutoAttachRequest(), user=owner), timeout=600)
            n_stock = await db.assets.count_documents(
                {"project_id": project_id, "asset_type": {"$in": ["stock_video", "stock_image"]}})
            summary["steps"]["stock_assets"] = n_stock
            log(f"auto-attach finished: stock assets on project = {n_stock} "
                f"(detail keys: {sorted(k for k in (attached or {}) if 'attach' in k or 'fail' in k)})")
        except Exception as exc:  # noqa: BLE001 — never block the render on stock
            log(f"auto-attach skipped ({type(exc).__name__}: {str(exc)[:200]}) — continuing")

    # ------------------------------------------------------------ 4. render
    project = await db.projects.find_one({"id": project_id}, {"_id": 0})
    scenes = await db.scenes.find({"project_id": project_id}).sort("scene_number", 1).to_list(500)
    metadata = await db.metadata_packages.find_one({"project_id": project_id}, {"_id": 0})
    assets = await db.assets.find({"project_id": project_id}, {"_id": 0}).to_list(2000)
    check = render_service.validate_prerequisites(project, script, scenes, metadata, assets)
    for item in check.get("checklist", []):
        log(f"  prereq {'OK  ' if item.get('ok') else 'MISS'} {item.get('label')}")
    if not check.get("ok"):
        log(f"render prerequisites not met: {check.get('issues')}")
        if check.get("autofit_suggestion"):
            log(f"autofit suggestion: {json.dumps(check['autofit_suggestion'], default=str)[:400]}")
        summary["steps"]["prereq_issues"] = check.get("issues")
        print(json.dumps(summary, indent=2, default=str))
        return 2

    follower = JournalFollower("facelessforge.service")
    aspect = project.get("aspect_ratio")
    if aspect not in ("9:16", "1:1", "16:9"):
        aspect = None
    log(f"starting render via render_service.queue_render (same as POST /render/start) "
        f"aspect={aspect or 'project default'}")
    try:
        job = await render_service.queue_render(
            project_id, requested_by=owner_id, burn_subtitles=False, aspect_ratio=aspect)
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "detail", None) or str(exc)
        log(f"queue_render refused: {json.dumps(detail, default=str)[:600] if isinstance(detail, (dict, list)) else detail}")
        print(json.dumps(summary, indent=2, default=str))
        return 3

    job_id = job["id"]
    summary["steps"]["render_job_id"] = job_id
    log(f"render job {job_id} status={job.get('status')}")

    task = render_service._ACTIVE_TASKS.get(project_id)
    completed_values = set(getattr(render_service, "COMPLETED_RENDER_STATUS_VALUES", ()))
    completed_values |= {"completed", "COMPLETED"}
    failed_values = {"failed", "FAILED", "cancelled", "CANCELLED", "canceled"}

    started = time.time()
    while True:
        follower.pump()
        doc = await db.render_jobs.find_one({"id": job_id}, {"_id": 0}) or {}
        status = str(doc.get("status") or "")
        log(f"job {status} step={doc.get('current_step')} progress={doc.get('progress')}")
        if status in completed_values or status in failed_values:
            break
        if time.time() - started > render_timeout:
            log(f"render timeout after {render_timeout}s — leaving job {status} in place")
            break
        await asyncio.sleep(10)

    if task and not task.done():
        try:
            await task
        except Exception as exc:  # noqa: BLE001
            log(f"render task surfaced {type(exc).__name__}: {str(exc)[:300]}")
    follower.pump()

    job = await db.render_jobs.find_one({"id": job_id}, {"_id": 0}) or {}
    status = str(job.get("status") or "")
    summary["steps"]["render_status"] = status
    if status not in completed_values:
        log(f"render did not complete (status={status}) error={job.get('error_message')}")
        for ln in journal_tail("facelessforge.service", lines=60)[-20:]:
            log(f"  facelessforge| {ln}")
        unit_failed("the-studio.service")
        print(json.dumps(summary, indent=2, default=str))
        return 3

    url = job.get("output_url")
    if not url:
        from app.storage import resolve_delivery_url as _resolve
        url = _resolve(job)
    if not url:
        log("render completed but no output_url / delivery url on the job")
        print(json.dumps(summary, indent=2, default=str))
        return 3
    summary["steps"]["output_url"] = url
    log(f"render completed: {url}")

    # ------------------------------------------------------------ 5. verify
    narration = await _narration_path(voice_asset, download)
    if narration is None:
        log("could not obtain a local narration file for verify.py --narration")
        print(json.dumps(summary, indent=2, default=str))
        return 4

    passed = False
    for attempt in range(1, 7):
        result = await _run_verify(backend, url, narration, len(scenes))
        passed = _verify_passed(result)
        log(f"verify attempt {attempt}: passed={passed}")
        if passed:
            summary["steps"]["verify"] = result
            break
        fails = [c.get("check") for c in (result or {}).get("checks", []) if not c.get("passed")]
        log(f"  verify failing checks: {fails}")
        if attempt < 6:
            await asyncio.sleep(30)

    unit_failed("the-studio.service")
    summary["verification_passed"] = passed
    print(json.dumps(summary, indent=2, default=str))
    log("VERIFIED MP4" if passed else "verify did not pass yet")
    return 0 if passed else 4


def _host_of(mongo_url: str) -> str:
    try:
        after = mongo_url.split("://", 1)[1]
        after = after.split("@")[-1]
        return after.split("/")[0].split("?")[0]
    except Exception:  # noqa: BLE001
        return "?"


def _safe(fn) -> str:
    if fn is None:
        return "unavailable"
    try:
        info = fn()
        return str(info.get("provider") if isinstance(info, dict) else info)
    except Exception:  # noqa: BLE001
        return "unknown"


async def _narration_path(voice_asset: dict, dl) -> Path | None:
    candidate = voice_asset.get("file_path") or ""
    if candidate and Path(candidate).is_file():
        log(f"narration file (local): {candidate}")
        return Path(candidate)
    url = voice_asset.get("url") or voice_asset.get("preview_url")
    if not url:
        return None
    suffix = Path(url.split("?")[0]).suffix or ".mp3"
    dest = Path(tempfile.gettempdir()) / f"narration-{voice_asset.get('id')}{suffix}"
    try:
        await dl(url, dest)
        log(f"narration downloaded: {dest} ({dest.stat().st_size} bytes)")
        return dest
    except Exception as exc:  # noqa: BLE001
        log(f"narration download failed: {type(exc).__name__}: {str(exc)[:200]}")
        return None


async def _run_verify(backend: Path, url: str, narration: Path, scene_count: int) -> dict:
    cmd = [sys.executable, "-m", "app.verify", "--url", url,
           "--narration", str(narration), "--scene-count", str(scene_count), "--json"]
    log(f"verify: {' '.join(cmd[:5])} ... --json")

    def _run() -> tuple[int, str, str]:
        proc = subprocess.run(cmd, cwd=str(backend), capture_output=True, text=True, timeout=3600)
        return proc.returncode, proc.stdout, proc.stderr

    rc, out, err = await asyncio.to_thread(_run)
    data: dict = {}
    start = out.find("{")
    if start >= 0:
        try:
            data = json.loads(out[start:])
        except json.JSONDecodeError:
            data = {}
    if not data:
        log(f"  verify produced no JSON (rc={rc}) stderr={err.strip().splitlines()[-1:] }")
    return data


def _verify_passed(result: dict | None) -> bool:
    if not result:
        return False
    for key in ("verification_passed", "overall_passed", "passed"):
        if isinstance(result.get(key), bool):
            return result[key]
    checks = result.get("checks") or []
    return bool(checks) and all(c.get("passed") for c in checks)


def main() -> None:
    ap = argparse.ArgumentParser(description="Assemble assets and produce the first verified MP4")
    ap.add_argument("project_id", help="projects.id (UUID) to assemble")
    ap.add_argument("--force-regen", action="store_true",
                    help="regenerate thumbnail + voiceover even if already selected")
    ap.add_argument("--render-timeout", type=int, default=3600,
                    help="seconds to wait for the render job (default 3600)")
    args = ap.parse_args()
    code = asyncio.run(run(args.project_id, force_regen=args.force_regen,
                           render_timeout=args.render_timeout))
    sys.exit(code)


if __name__ == "__main__":
    main()
