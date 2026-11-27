"""Regression checks for Dodo Standard Webhooks fallback verification."""
import base64
import hashlib
import hmac
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.dodo_webhook_verify import (
    WebhookVerificationError,
    _verify_standard_webhooks,
)


class DodoWebhookHeaderTests(unittest.TestCase):
    def setUp(self):
        self.payload = b'{"type":"payment.succeeded"}'
        self.secret_bytes = b"local-test-secret"
        self.secret = "whsec_" + base64.b64encode(self.secret_bytes).decode()
        self.webhook_id = "msg_test"
        self.timestamp = str(int(time.time()))
        signed = (
            f"{self.webhook_id}.{self.timestamp}.".encode() + self.payload
        )
        signature = base64.b64encode(
            hmac.new(self.secret_bytes, signed, hashlib.sha256).digest()
        ).decode()
        self.signature = f"v1,{signature}"

    def test_valid_signature_with_mixed_case_headers(self):
        headers = {
            "Webhook-Signature": self.signature,
            "WEBHOOK-TIMESTAMP": self.timestamp,
            "webhook-ID": self.webhook_id,
        }
        _verify_standard_webhooks(self.payload, headers, self.secret)

    def test_valid_signature_independent_of_header_order(self):
        headers = {
            "webhook-id": self.webhook_id,
            "webhook-signature": self.signature,
            "webhook-timestamp": self.timestamp,
        }
        _verify_standard_webhooks(self.payload, headers, self.secret)

    def test_invalid_signature_rejected(self):
        headers = {
            "webhook-id": self.webhook_id,
            "webhook-timestamp": self.timestamp,
            "webhook-signature": "v1,invalid",
        }
        with self.assertRaises(WebhookVerificationError):
            _verify_standard_webhooks(self.payload, headers, self.secret)


if __name__ == "__main__":
    unittest.main()
