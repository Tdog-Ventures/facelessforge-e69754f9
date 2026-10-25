"""Run focused Dodo tests with a disposable Mongo process and isolated Redis.

Requires DODO_TEST_MONGOD and DODO_TEST_REDIS_SERVER executable paths.
Uses only fresh temporary storage and loopback listeners; does not load .env.
"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TESTS = [
    "backend/tests/test_dodo_fulfillment.py",
    "backend/tests/test_dodo_grant_atomicity.py",
    "backend/tests/test_dodo_refund_review.py",
    "backend/tests/test_dodo_pending_identity.py",
    "backend/tests/test_dodo_redis_integration.py",
    "backend/tests/test_dodo_inventory_cli.py",
    "backend/tests/test_dodo_store_recovery.py",
]


def main():
    binary = os.environ.get("DODO_TEST_MONGOD")
    redis_binary = os.environ.get("DODO_TEST_REDIS_SERVER")
    if not binary or not redis_binary:
        raise SystemExit("Set DODO_TEST_MONGOD and DODO_TEST_REDIS_SERVER to disposable test executables")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"mongodb://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="ff-dodo-mongo-") as directory:
        log = Path(directory) / "mongo.log"
        process = subprocess.Popen([
            binary, "--bind_ip", "127.0.0.1", "--port", str(port),
            "--nounixsocket", "--dbpath", directory, "--logpath", str(log),
            "--wiredTigerCacheSizeGB", "0.25",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        client = MongoClient(url, serverSelectionTimeoutMS=500)
        try:
            for _ in range(20):
                try:
                    client.admin.command("ping")
                    break
                except Exception:
                    if process.poll() is not None:
                        raise RuntimeError("Disposable Mongo startup failed: " + log.read_text()[-2000:])
                    time.sleep(0.1)
            else:
                raise RuntimeError("Disposable Mongo readiness timed out")
            print("Disposable Mongo version:", client.server_info()["version"], flush=True)
            env = dict(os.environ, DODO_TEST_MONGO_URL=url,
                       PYTHON_DOTENV_DISABLED="1", PYTHONPATH=str(ROOT / "backend"))
            arguments = sys.argv[1:] or [*DEFAULT_TESTS, "-q", "--tb=short"]
            result = subprocess.run([sys.executable, "-m", "pytest", *arguments], cwd=ROOT, env=env)
            return result.returncode
        finally:
            client.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    sys.exit(main())

