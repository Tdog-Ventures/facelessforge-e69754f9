"""Opt-in checks against an isolated Redis executable; never use app Redis."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import subprocess
import tempfile
import time

import pytest
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.billing_dodo import _apply_redis_grant


@pytest.fixture
def isolated_redis():
    executable = os.environ.get("DODO_TEST_REDIS_SERVER")
    if not executable:
        pytest.skip("requires disposable Redis executable")
    with tempfile.TemporaryDirectory(prefix="ff-dodo-redis-") as directory:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        process = subprocess.Popen([
            executable, "--bind", "127.0.0.1", "--port", str(port),
            "--save", "", "--appendonly", "no", "--dir", directory,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        client = redis.Redis(host="127.0.0.1", port=port, decode_responses=True,
                             socket_connect_timeout=0.2, socket_timeout=1,
                             retry=Retry(NoBackoff(), 0))
        try:
            for _ in range(30):
                try:
                    client.ping()
                    break
                except redis.ConnectionError:
                    if process.poll() is not None:
                        raise RuntimeError("Disposable Redis failed to start")
                    time.sleep(0.05)
            else:
                raise RuntimeError("Disposable Redis readiness timed out")
            yield client
        finally:
            client.close()
            process.terminate()
            process.wait(timeout=5)


def test_real_redis_concurrent_replays_and_distinct_payments(isolated_redis):
    client = isolated_redis
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _apply_redis_grant(client, "user", "same", 100), range(16)))
    assert sum(results) == 1
    assert client.hget("tenant:user", "credits") == "100"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: _apply_redis_grant(client, "user", f"distinct-{n}", 7), range(16)))
    assert client.hget("tenant:user", "credits") == "212"


@pytest.mark.parametrize("failure", ["wrong_type", "non_integer", "overflow"])
def test_failed_increment_leaves_payment_retryable(isolated_redis, failure):
    client = isolated_redis
    if failure == "wrong_type":
        client.set("tenant:user", "legacy string account")
    else:
        client.hset("tenant:user", "credits", "bad" if failure == "non_integer" else str(2**63 - 1))
    with pytest.raises(redis.ResponseError):
        _apply_redis_grant(client, "user", "payment", 100)
    assert client.hget("tenant:user:dodo_fulfillments", "payment") is None
    client.delete("tenant:user")
    assert _apply_redis_grant(client, "user", "payment", 100)
    assert client.hget("tenant:user", "credits") == "100"
