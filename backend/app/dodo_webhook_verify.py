"""
Dodo Payments Webhook Verification - Standard Webhooks (Svix compatible)
Path: /opt/facelessforge/deploy/backend/app/dodo_webhook_verify.py
Implements Standard Webhooks verification: https://www.standardwebhooks.com/
Signed message: webhook-id.webhook-timestamp.raw_body
Secret: strip whsec_ prefix, base64-decode, HMAC-SHA256, base64-encode -> v1,<sig>

Primary path: use Dodopayments SDK Webhooks.unwrap() if available.
Fallback: pure-Python Standard Webhooks verification (no SDK required).
"""
import base64
import hashlib
import hmac
import time
import os
import logging
from typing import Dict, Optional

logger = logging.getLogger("dodo_verify")
TOLERANCE = 300  # 5 min replay window


class WebhookVerificationError(Exception):
    pass


class WebhookReplayError(WebhookVerificationError):
    pass


def _get_secret_bytes(secret: str) -> bytes:
    """Decode webhook secret: strip whsec_ prefix, base64 decode."""
    if secret.startswith("whsec_"):
        secret = secret[len("whsec_") :]
    # Dodo may provide raw base64 or already-decoded; try base64 decode
    # If decode fails, treat as raw UTF-8 bytes (legacy path)
    try:
        # add padding if missing
        padded = secret + "=" * (-len(secret) % 4)
        return base64.b64decode(padded)
    except Exception:
        return secret.encode("utf-8")


def _verify_standard_webhooks(payload: bytes, headers: Dict[str, str], secret: str) -> None:
    """Pure-Python Standard Webhooks verification."""
    if not secret:
        raise WebhookVerificationError("DODO_PAYMENTS_WEBHOOK_KEY not set")

    # headers are case-insensitive, normalized to lowercase hyphenated per spec
    def _h(name: str) -> Optional[str]:
        # try exact, lower, and capitalized variants
        for k in (name, name.lower(), name.upper(), name.capitalize(), "Webhook-Id", "Webhook-Timestamp", "Webhook-Signature"):
            if k in headers and headers[k]:
                return str(headers[k])
        # also check lower-case dict
        low = {kk.lower(): vv for kk, vv in headers.items()}
        return low.get(name.lower())

    webhook_id = _h("webhook-id")
    webhook_timestamp = _h("webhook-timestamp")
    webhook_signature = _h("webhook-signature")

    if not all([webhook_id, webhook_timestamp, webhook_signature]):
        raise WebhookVerificationError("Missing webhook headers: webhook-id, webhook-timestamp, webhook-signature required")

    try:
        ts = int(str(webhook_timestamp).strip())
    except ValueError:
        raise WebhookVerificationError(f"Invalid webhook-timestamp: {webhook_timestamp}")

    now = int(time.time())
    if abs(now - ts) > TOLERANCE:
        raise WebhookReplayError(f"Timestamp outside tolerance: ts={ts} now={now} diff={abs(now - ts)} > {TOLERANCE}s")

    secret_bytes = _get_secret_bytes(secret)
    # payload is raw bytes; decode as utf-8 for signing but keep bytes exact
    # Standard Webhooks signs: f"{webhook_id}.{webhook_timestamp}.{payload_str}"
    # payload_str is raw body as utf-8 string
    try:
        payload_str = payload.decode("utf-8")
    except Exception:
        payload_str = payload.decode("utf-8", errors="replace")

    signed_content = f"{webhook_id}.{webhook_timestamp}.{payload_str}".encode("utf-8")
    expected = base64.b64encode(hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()).decode("utf-8")

    # Header can contain multiple signatures: "v1,<sig> v1,<sig2>" or "v1,<sig>, v1,<sig2>"
    sig_header = str(webhook_signature)
    # split on space and comma
    parts = []
    for chunk in sig_header.replace(",", " ").split():
        chunk = chunk.strip()
        if not chunk:
            continue
        parts.append(chunk)

    # Extract signatures with v1 prefix
    passed = False
    for p in parts:
        # format is v1,<base64>
        if "," in p:
            version, sig = p.split(",", 1)
            version = version.strip()
            sig = sig.strip()
        else:
            # no version prefix, treat as raw sig (legacy)
            version, sig = "v1", p
        if version != "v1":
            continue
        # timing-safe compare
        try:
            # hmac.compare_digest requires same length strings; use constant-time if lengths differ -> false
            if len(sig) != len(expected):
                # still do compare_digest on padded to avoid timing leak? but we just continue
                # use hmac.compare_digest with dummy
                hmac.compare_digest(expected, expected)  # warm
                continue
            if hmac.compare_digest(sig, expected):
                passed = True
                break
        except Exception:
            continue

    if not passed:
        raise WebhookVerificationError("Invalid webhook signature")


def verify_signature(payload: bytes, headers: Dict[str, str], secret: Optional[str] = None):
    """
    Verify Dodo webhook signature using Standard Webhooks.

    Priority:
      1. Try Dodopayments SDK client.webhooks.unwrap() if dodopayments installed and secret set.
      2. Fallback to pure-Python Standard Webhooks verification.

    Args:
        payload: raw request body bytes (from await request.body())
        headers: dict with webhook-id, webhook-timestamp, webhook-signature (case insensitive)
        secret: DODO_PAYMENTS_WEBHOOK_KEY (whsec_...); if None, reads env DODO_PAYMENTS_WEBHOOK_KEY or DODO_WEBHOOK_SECRET

    Raises:
        WebhookVerificationError / WebhookReplayError on failure
    """
    if secret is None:
        secret = os.getenv("DODO_PAYMENTS_WEBHOOK_KEY") or os.getenv("DODO_WEBHOOK_SECRET") or os.getenv("DODO_PAYMENTS_WEBHOOK_SECRET") or ""

    # Normalize headers to lowercase for SDK
    norm_headers = {k.lower(): v for k, v in (headers or {}).items() if v is not None}
    # Ensure required keys exist in normalized form
    for k in ("webhook-id", "webhook-timestamp", "webhook-signature"):
        if k not in norm_headers:
            # try case-insensitive lookup
            for orig_k, v in (headers or {}).items():
                if orig_k.lower() == k:
                    norm_headers[k] = v
                    break

    # Try SDK path first if available - most accurate and future-proof
    try:
        from dodopayments import DodoPayments  # type: ignore

        api_key = os.getenv("DODO_PAYMENTS_API_KEY") or "dummy_for_verify"
        env_raw = os.getenv("DODO_PAYMENTS_ENVIRONMENT", "test_mode")
        environment = "live_mode" if env_raw == "live_mode" else "test_mode"
        # SDK requires webhookKey at client construction for unwrap
        client = DodoPayments(bearer_token=api_key, environment=environment, webhook_key=secret)  # type: ignore
        # SDK unwrap expects payload as str or bytes and headers dict
        # It throws on invalid signature - we let it propagate as verification error
        try:
            # Try bytes payload first (newer SDK), fallback to str
            try:
                client.webhooks.unwrap(payload, headers=norm_headers)  # type: ignore
            except TypeError:
                client.webhooks.unwrap(payload.decode("utf-8") if isinstance(payload, bytes) else payload, headers=norm_headers)  # type: ignore
            return  # success via SDK
        except Exception as e:
            # If SDK says invalid signature, surface as verification error
            # But don't swallow - fall through to manual check for better error message
            # Only re-raise if secret is set and we know SDK should have succeeded
            # For debugging, try manual verification to give clearer error
            logger.debug(f"SDK unwrap failed, trying manual verify: {e}")
            _verify_standard_webhooks(payload, norm_headers, secret)
            return
    except ImportError:
        # SDK not installed - use manual verification
        _verify_standard_webhooks(payload, norm_headers, secret)
        return
    except WebhookVerificationError:
        raise
    except Exception as e:
        # SDK construction or other error - fallback to manual
        if "Unknown environment" in str(e) or "webhook" in str(e).lower():
            _verify_standard_webhooks(payload, norm_headers, secret)
            return
        raise WebhookVerificationError(str(e))


def check_and_mark_webhook(redis_client, webhook_id: str, ttl_seconds: int = 86400) -> bool:
    """
    Idempotency helper using Redis SET NX.
    Returns True if webhook_id was newly marked (should process), False if already seen (skip).
    Uses Redis key dodo:webhooks:processed:<id> with TTL.
    Falls back to SISMEMBER if redis_client doesn't support SET NX.
    """
    if not webhook_id:
        return True
    try:
        # Try SET NX with TTL (atomic)
        # redis-py: set(name, value, nx=True, ex=ttl)
        result = redis_client.set(f"dodo:webhooks:processed:{webhook_id}", "1", nx=True, ex=ttl_seconds)
        if result is True or result == True:  # type: ignore
            # Also maintain legacy set for backward compat
            try:
                redis_client.sadd("dodo:webhooks:processed", webhook_id)
            except Exception:
                pass
            return True
        else:
            return False
    except Exception:
        # Fallback to SISMEMBER / SADD pattern
        try:
            if redis_client.sismember("dodo:webhooks:processed", webhook_id):
                return False
            redis_client.sadd("dodo:webhooks:processed", webhook_id)
            return True
        except Exception:
            return True
