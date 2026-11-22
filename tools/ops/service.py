"""Owner-only FacelessForge operations bridge. Loopback HTTP, fixed operations.

No arbitrary command, URL, container, file, repository or environment input.
Mutations require the installed idle guard and deploy helper; errors fail closed.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

CONTAINER = "facelessforge-app"
ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
TASKS = STATE / "tasks"
CHECKOUT = Path("/opt/facelessforge/deploy")
KEY = os.environ.get("FACELESSFORGE_OPS_KEY", "")
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)
LOCK = threading.Lock()
ACTIVE: str | None = None
MAX_RESULT = 24000


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def command(args, timeout=15, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"Operation exited {result.returncode}; inspect the host logs")
    return result


def container_info():
    return json.loads(command(["docker", "inspect", CONTAINER]).stdout)[0]


def scrub(value):
    text = str(value)
    secrets = [KEY] if KEY else []
    try:
        for item in container_info().get("Config", {}).get("Env", []):
            name, _, secret = item.partition("=")
            if len(secret) >= 6 and re.search(r"key|token|secret|password|mongo.*(?:url|uri)|database.*(?:url|uri)", name, re.I):
                secrets.append(secret)
    except Exception:
        # If secret inventory is unavailable, do not return raw logs at all.
        return "Log output withheld: credential redaction inventory unavailable."
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[REDACTED]", text)
    return text[-MAX_RESULT:]


def git(*args):
    return command(["git", "-C", str(CHECKOUT), *args], check=False)


def status():
    info = container_info()
    state, config = info.get("State", {}), info.get("Config", {})
    labels = config.get("Labels") or {}
    names = dict(item.split("=", 1) for item in config.get("Env", []) if "=" in item)
    head = git("rev-parse", "HEAD")
    dirty = git("status", "--short", "--untracked-files=no")
    health = {"reachable": False}
    path = os.environ.get("FACELESSFORGE_HEALTH_PATH", "/api/health")
    if path not in ("/api/health", "/health", "/healthz"):
        path = "/api/health"
    try:
        with urlopen("http://127.0.0.1:7001" + path, timeout=5) as response:
            health = {"reachable": True, "http_status": response.status, "path": path}
    except Exception:
        pass
    with LOCK:
        active = ACTIVE
    active_work = {"verified": False}
    if (ROOT / "guard-idle.py").is_file():
        try:
            check = command(["python3", str(ROOT / "guard-idle.py"), "--report"], timeout=20)
            active_work = {"verified": True, **json.loads(check.stdout)}
        except Exception:
            pass
    return {
        "product": "FacelessForge", "observed_at": now(),
        "container": {"name": CONTAINER, "running": bool(state.get("Running")), "health": state.get("Health", {}).get("Status"), "started_at": state.get("StartedAt"), "image": config.get("Image"), "image_id": info.get("Image"), "revision_label": labels.get("org.opencontainers.image.revision"), "revision_label_is_not_source_verification": True},
        "checkout": {"head": head.stdout.strip() if head.returncode == 0 else None, "modified_tracked_files": len(dirty.stdout.splitlines()) if dirty.returncode == 0 else None},
        "backend_health": health,
        "external_api": {"key_configured": bool(names.get("EXTERNAL_RENDER_API_KEY")), "enabled": names.get("EXTERNAL_RENDER_ENABLED", "").lower() in ("1", "true", "yes")},
        "mutation_guards": {"idle_guard_installed": (ROOT / "guard-idle.py").is_file(), "deploy_helper_installed": (ROOT / "deploy-revision.sh").is_file()},
        "active_task_id": active,
        "active_work": active_work,
    }


def save_task(task):
    target = TASKS / (task["task_id"] + ".json")
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(task))
    tmp.chmod(0o600)
    tmp.replace(target)


def read_task(task_id):
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", task_id):
        raise ValueError("Invalid task ID")
    path = TASKS / (task_id + ".json")
    if not path.is_file():
        raise FileNotFoundError("Task not found")
    return json.loads(path.read_text())


def idle_guard():
    guard = ROOT / "guard-idle.py"
    if not guard.is_file():
        raise RuntimeError("Idle guard is missing; no mutation performed.")
    command(["python3", str(guard)], timeout=30)


def work(task):
    global ACTIVE
    try:
        task.update(status="running", started_at=now())
        save_task(task)
        # Guard holds no lock in the renderer. The deploy helper must also
        # quiesce admission and check immediately before replacing the container.
        idle_guard()
        if task["action"] == "restart":
            restart = ROOT / "restart-app.sh"
            if not restart.is_file():
                raise RuntimeError("Guarded restart helper missing; no mutation performed.")
            args = ["bash", str(restart)]
        else:
            deploy = ROOT / "deploy-revision.sh"
            if not deploy.is_file():
                raise RuntimeError("Deploy helper missing; no mutation performed.")
            args = ["bash", str(deploy), task["commit"]]
        # Full output stays in a private file, never in request logs or chat.
        output_path = TASKS / (task["task_id"] + ".log")
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            proc = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            task["process_id"] = proc.pid
            save_task(task)
            rc = proc.wait(timeout=1800)
        task.update(status="succeeded" if rc == 0 else "failed", exit_code=rc, output=scrub(output_path.read_text(errors="replace")[-100000:]))
    except subprocess.TimeoutExpired:
        # Never kill a deployment mid-switch or automatically repeat it.
        task.update(status="reconcile_required", error="Helper exceeded 30 minutes and may still be running. Inspect the host before another mutation.")
    except Exception as exc:
        task.update(status="failed", error=scrub(str(exc)))
    finally:
        task["finished_at"] = now()
        save_task(task)
        with LOCK:
            # A timed-out child may still be mutating; retain the global block.
            if task["status"] != "reconcile_required":
                ACTIVE = None


def submit(action, body):
    global ACTIVE
    if action == "deploy":
        if set(body) != {"commit"} or not isinstance(body["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", body["commit"]):
            raise ValueError("Supply exactly one full lowercase 40-character commit SHA.")
    elif set(body) != {"reason"} or not isinstance(body["reason"], str) or not 1 <= len(body["reason"].strip()) <= 300:
        raise ValueError("Supply a restart reason from 1 to 300 characters.")
    if not (ROOT / "guard-idle.py").is_file() or not (ROOT / ("deploy-revision.sh" if action == "deploy" else "restart-app.sh")).is_file():
        raise RuntimeError("Required guarded helper is not installed; no mutation performed.")
    with LOCK:
        if ACTIVE:
            raise BlockingIOError("An operation is already running or requires reconciliation.")
        task_id = str(uuid.uuid4())
        task = {"task_id": task_id, "action": action, "status": "queued", "created_at": now(), **body}
        save_task(task)
        ACTIVE = task_id
        POOL.submit(work, task)
    return {"task_id": task_id, "status": "queued", "action": action}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def send_json(self, code, value):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        supplied = self.headers.get("Authorization", "")
        return bool(KEY) and hmac.compare_digest(supplied, "Bearer " + KEY)

    def do_GET(self):
        if not self.authorized():
            return self.send_json(401, {"error": "Unauthorized"})
        url = urlsplit(self.path)
        try:
            if url.path == "/api/ops/status" and not url.query:
                return self.send_json(200, status())
            if url.path == "/api/ops/logs":
                query = parse_qs(url.query, keep_blank_values=True)
                if set(query) - {"lines"} or len(query.get("lines", ["80"])) != 1:
                    raise ValueError("Supply lines only")
                lines = int(query.get("lines", ["80"])[0])
                if not 20 <= lines <= 200:
                    raise ValueError("lines must be between 20 and 200")
                result = command(["docker", "logs", "--timestamps", "--tail", str(lines), CONTAINER], check=False)
                if result.returncode:
                    raise RuntimeError("Container logs unavailable")
                return self.send_json(200, {"container": CONTAINER, "observed_at": now(), "logs": scrub(result.stdout + result.stderr), "redacted": True, "max_characters": MAX_RESULT})
            prefix = "/api/ops/tasks/"
            if url.path.startswith(prefix) and not url.query:
                return self.send_json(200, read_task(url.path[len(prefix):]))
            return self.send_json(404, {"error": "Not found"})
        except ValueError:
            return self.send_json(400, {"error": "Invalid arguments"})
        except FileNotFoundError:
            return self.send_json(404, {"error": "Task not found"})
        except Exception:
            return self.send_json(503, {"error": "Operation unavailable; inspect host service"})

    def do_POST(self):
        self.close_connection = True
        if not self.authorized():
            return self.send_json(401, {"error": "Unauthorized"})
        if self.headers.get("Transfer-Encoding"):
            return self.send_json(400, {"error": "Transfer encoding unsupported"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 4096:
                raise ValueError()
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
            if self.path not in ("/api/ops/deploy", "/api/ops/restart"):
                return self.send_json(404, {"error": "Not found"})
            return self.send_json(202, submit(self.path.rsplit("/", 1)[1], body))
        except (ValueError, UnicodeError):
            return self.send_json(400, {"error": "Invalid arguments"})
        except BlockingIOError:
            return self.send_json(409, {"error": "An operation is running or requires reconciliation"})
        except RuntimeError:
            return self.send_json(503, {"error": "Guarded helper unavailable; no mutation performed"})


def main():
    global ACTIVE
    if len(KEY) < 40:
        raise SystemExit("FACELESSFORGE_OPS_KEY must contain at least 40 characters")
    STATE.mkdir(mode=0o700, exist_ok=True)
    TASKS.mkdir(mode=0o700, exist_ok=True)
    # An interrupted operation is never blindly repeated after service restart.
    for path in TASKS.glob("*.json"):
        task = json.loads(path.read_text())
        if task.get("status") in ("queued", "running", "reconcile_required"):
            task.update(status="reconcile_required", error="Service restarted with unresolved work; operator must reconcile on the host.")
            save_task(task)
            ACTIVE = task["task_id"]
    server = ThreadingHTTPServer(("127.0.0.1", 7012), Handler)
    server.daemon_threads = True
    server.timeout = 10
    server.serve_forever()


if __name__ == "__main__":
    main()
