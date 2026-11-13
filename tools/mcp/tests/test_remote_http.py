import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from http.client import HTTPConnection
from urllib.parse import urlencode

os.environ.update({
    "FACELESSFORGE_REMOTE_MCP_CLIENT_ID": "gemini-test-client",
    "FACELESSFORGE_REMOTE_MCP_CLIENT_SECRET": "test-client-secret",
    "FACELESSFORGE_REMOTE_MCP_OWNER_PASSWORD": "test-owner-password",
    "FACELESSFORGE_REMOTE_MCP_BASE_URL": "https://cinematic.ethinx.solutions",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

remote = importlib.import_module("remote_http")


class RemoteHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = remote.ThreadingHTTPServer(("127.0.0.1", 0), remote.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = body.encode() if isinstance(body, str) else body
        connection.request(method, path, payload, headers or {})
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), json.loads(data) if data else None)
        connection.close()
        return result

    def form(self, path, values, headers=None):
        return self.request("POST", path, urlencode(values), {
            "Content-Type": "application/x-www-form-urlencoded", **(headers or {})
        })

    def issue_access_token(self):
        verifier = "a" * 50
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        record = {"client_id": "gemini-test-client", "redirect_uri":
                  "https://oauth-redirect.googleusercontent.com/r/test", "code_challenge": challenge}
        code = remote.STATE.issue_code(record)
        auth = base64.b64encode(b"gemini-test-client:test-client-secret").decode()
        status, _, payload = self.form(remote.TOKEN_PATH, {
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": record["redirect_uri"], "code_verifier": verifier,
        }, {"Authorization": "Basic " + auth})
        self.assertEqual(status, 200)
        return payload["access_token"]

    def test_metadata_and_unauthorized_challenge(self):
        self.assertEqual(self.request("GET", remote.AUTH_METADATA_PATH)[0], 200)
        self.assertEqual(self.request("GET", remote.PROTECTED_RESOURCE_PATH)[0], 200)
        status, headers, _ = self.request("POST", remote.MCP_PATH, "{}")
        self.assertEqual(status, 401)
        self.assertIn("resource_metadata=", headers["WWW-Authenticate"])

    def test_authorization_rejects_non_google_redirect(self):
        query = urlencode({"client_id": "gemini-test-client", "redirect_uri": "https://evil.example/callback",
                           "response_type": "code", "state": "x", "code_challenge": "x",
                           "code_challenge_method": "S256"})
        self.assertEqual(self.request("GET", remote.AUTHORIZATION_PATH + "?" + query)[0], 400)

    def test_read_only_tool_list_and_write_rejection(self):
        token = self.issue_access_token()
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-11-25"}}
        status, _, payload = self.request("POST", remote.MCP_PATH, json.dumps(initialize), headers)
        self.assertEqual(status, 200)
        self.assertIn("instructions", payload["result"])
        listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        _, _, payload = self.request("POST", remote.MCP_PATH, json.dumps(listing), headers)
        names = {tool["name"] for tool in payload["result"]["tools"]}
        self.assertEqual(names, remote.READ_ONLY_NAMES)
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "render_video", "arguments": {}}}
        _, _, payload = self.request("POST", remote.MCP_PATH, json.dumps(call), headers)
        self.assertTrue(payload["result"]["isError"])
        self.assertIn("disabled", payload["result"]["content"][0]["text"])

    def test_invalid_secret_and_pkce_fail(self):
        status, _, _ = self.form(remote.TOKEN_PATH, {"grant_type": "authorization_code",
                                                     "client_id": "gemini-test-client",
                                                     "client_secret": "wrong"})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
