import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from linkedin_mcp.actions import ActionLedger, build_request, post_urn
from linkedin_mcp.api import LinkedInClient, LinkedInError
from linkedin_mcp.config import Config
from linkedin_mcp.oauth import LoginError, authorize_url, code_from_callback
from linkedin_mcp.server import Server
from linkedin_mcp.storage import StorageError, read_json, write_json

PERSON = "urn:li:person:abc123"


def make_config(home: Path, approval_mode: str = "auto", cap: int = 40) -> Config:
    return Config(home=home, api_version="202608", daily_write_cap=cap, proposal_ttl_seconds=1800,
                  approval_mode=approval_mode, client_id="cid", client_secret="secret", redirect_port=8765)


class FakeClient(LinkedInClient):
    """Records requests instead of calling LinkedIn."""

    def __init__(self, config, outcome="ok"):
        super().__init__(config)
        self.outcome = outcome
        self.calls = []

    def member(self):
        return {"person_urn": PERSON, "name": "Test Member"}

    def request(self, method, path, payload=None, *, versioned=True):
        self.calls.append((method, path, payload))
        if self.outcome == "5xx":
            raise LinkedInError("server error", 503)
        if self.outcome == "4xx":
            raise LinkedInError("bad request", 422)
        if self.outcome == "network":
            raise OSError("connection reset")
        return 201, {"x-restli-id": "urn:li:share:7000000000001"}, None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.config = make_config(self.home)
        self.client = FakeClient(self.config)
        self.ledger = ActionLedger(self.config, self.client)

    def tearDown(self):
        self.tmp.cleanup()


class ParsingTests(unittest.TestCase):
    def test_post_urn_from_urls_and_urns(self):
        self.assertEqual(post_urn("urn:li:share:1234567"), "urn:li:share:1234567")
        self.assertEqual(post_urn("https://www.linkedin.com/feed/update/urn:li:activity:7123456789/"), "urn:li:activity:7123456789")
        self.assertEqual(post_urn("https://www.linkedin.com/posts/someone_title-activity-7123456789-AbCd"), "urn:li:activity:7123456789")
        with self.assertRaises(LinkedInError):
            post_urn("https://example.com/not-linkedin")

    def test_post_request_and_link_card(self):
        request, preview = build_request("post", {"text": "Hello", "visibility": "CONNECTIONS", "link_url": "https://x.dev", "link_title": "X"}, PERSON)
        self.assertEqual(request["path"], "/rest/posts")
        self.assertEqual(request["body"]["visibility"], "CONNECTIONS")
        self.assertEqual(request["body"]["content"]["article"]["source"], "https://x.dev")
        self.assertIn("CONNECTIONS", preview)

    def test_validation(self):
        with self.assertRaises(LinkedInError):
            build_request("post", {"text": "   "}, PERSON)
        with self.assertRaises(LinkedInError):
            build_request("post", {"text": "x" * 3001}, PERSON)
        with self.assertRaises(LinkedInError):
            build_request("post", {"text": "hi", "link_url": "http://insecure.dev"}, PERSON)
        with self.assertRaises(LinkedInError):
            build_request("react", {"post_url": "urn:li:share:1234567", "reaction": "HATE"}, PERSON)
        with self.assertRaises(LinkedInError):
            build_request("delete_post", {"post_url": "urn:li:activity:1234567"}, PERSON)

    def test_control_characters_are_stripped(self):
        request, _ = build_request("post", {"text": "a\x00b\x07c\nd"}, PERSON)
        self.assertEqual(request["body"]["commentary"], "abc\nd")


class LedgerTests(Base):
    def test_prepare_then_execute_once(self):
        proposal = self.ledger.prepare({"kind": "post", "text": "Shipping linkedin-mcp"})
        self.assertEqual(self.client.calls, [])  # prepare never touches LinkedIn
        result = self.ledger.execute(proposal["proposal_id"], proposal["digest"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["result"]["post_urn"], "urn:li:share:7000000000001")
        self.assertEqual(len(self.client.calls), 1)
        with self.assertRaises(LinkedInError):
            self.ledger.execute(proposal["proposal_id"], proposal["digest"])
        self.assertEqual(len(self.client.calls), 1)

    def test_wrong_digest_is_refused(self):
        proposal = self.ledger.prepare({"kind": "post", "text": "Hello"})
        with self.assertRaises(LinkedInError):
            self.ledger.execute(proposal["proposal_id"], "0" * 64)
        self.assertEqual(self.client.calls, [])

    def test_tampered_request_is_refused(self):
        proposal = self.ledger.prepare({"kind": "post", "text": "Hello"})
        path = self.config.proposals_dir / f"{proposal['proposal_id']}.json"
        stored = read_json(path)
        stored["request"]["body"]["commentary"] = "Something else"
        write_json(path, stored)
        with self.assertRaises(LinkedInError):
            self.ledger.execute(proposal["proposal_id"], proposal["digest"])
        self.assertEqual(self.client.calls, [])

    def test_expired_proposal_is_refused(self):
        proposal = self.ledger.prepare({"kind": "post", "text": "Hello"})
        path = self.config.proposals_dir / f"{proposal['proposal_id']}.json"
        stored = read_json(path)
        stored["expires_at"] = int(time.time()) - 1
        write_json(path, stored)
        with self.assertRaises(LinkedInError):
            self.ledger.execute(proposal["proposal_id"], proposal["digest"])

    def test_uncertain_result_is_never_retried(self):
        for outcome in ("5xx", "network"):
            with self.subTest(outcome=outcome):
                self.client.outcome = outcome
                proposal = self.ledger.prepare({"kind": "post", "text": f"Hello {outcome}"})
                with self.assertRaises(LinkedInError):
                    self.ledger.execute(proposal["proposal_id"], proposal["digest"])
                self.assertEqual(self.ledger.get(proposal["proposal_id"])["status"], "unknown")
                with self.assertRaises(LinkedInError):
                    self.ledger.prepare({"kind": "post", "text": f"Hello {outcome}"})

    def test_client_error_is_marked_failed(self):
        self.client.outcome = "4xx"
        proposal = self.ledger.prepare({"kind": "comment", "post_url": "urn:li:share:1234567", "text": "Nice"})
        with self.assertRaises(LinkedInError):
            self.ledger.execute(proposal["proposal_id"], proposal["digest"])
        self.assertEqual(self.ledger.get(proposal["proposal_id"])["status"], "failed")

    def test_daily_cap(self):
        ledger = ActionLedger(make_config(self.home, cap=1), self.client)
        first = ledger.prepare({"kind": "post", "text": "one"})
        second = ledger.prepare({"kind": "post", "text": "two"})
        ledger.execute(first["proposal_id"], first["digest"])
        with self.assertRaises(LinkedInError):
            ledger.execute(second["proposal_id"], second["digest"])
        self.assertEqual(len(self.client.calls), 1)

    def test_cancel(self):
        proposal = self.ledger.prepare({"kind": "react", "post_url": "urn:li:share:1234567"})
        self.assertEqual(self.ledger.cancel(proposal["proposal_id"])["status"], "cancelled")
        with self.assertRaises(LinkedInError):
            self.ledger.execute(proposal["proposal_id"], proposal["digest"])
        self.assertEqual(self.ledger.pending(), [])


class StorageTests(unittest.TestCase):
    def test_files_are_private_and_unsafe_files_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "x.json"
            write_json(path, {"a": 1})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            os.chmod(path, 0o644)
            with self.assertRaises(StorageError):
                read_json(path)


class OAuthTests(unittest.TestCase):
    def test_authorize_url_and_state_check(self):
        config = make_config(Path("/tmp/unused"))
        url = authorize_url(config, "state123")
        self.assertIn("client_id=cid", url)
        self.assertIn("w_member_social", url.replace("+", " ").replace("%20", " "))
        self.assertEqual(code_from_callback("http://localhost:8765/callback?code=abc&state=state123", "state123"), "abc")
        with self.assertRaises(LoginError):
            code_from_callback("http://localhost:8765/callback?code=abc&state=other", "state123")


class ServerTests(Base):
    def run_session(self, messages, capabilities=None, approval_mode="auto"):
        config = make_config(self.home, approval_mode=approval_mode)
        ledger = ActionLedger(config, self.client)
        init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": capabilities or {}}}
        stdin = io.StringIO("\n".join(json.dumps(m) for m in [init] + messages) + "\n")
        stdout = io.StringIO()
        Server(config, ledger, stdin, stdout).serve()
        return [json.loads(line) for line in stdout.getvalue().splitlines()]

    @staticmethod
    def call(i, name, arguments):
        return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}}

    @staticmethod
    def payload(frame):
        return json.loads(frame["result"]["content"][0]["text"])

    def test_handshake_and_tools(self):
        frames = self.run_session([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        self.assertEqual(frames[0]["result"]["serverInfo"]["name"], "linkedin-mcp")
        names = [t["name"] for t in frames[1]["result"]["tools"]]
        self.assertEqual(names, ["linkedin_status", "linkedin_prepare_action", "linkedin_execute_action", "linkedin_list_actions", "linkedin_cancel_action"])

    def test_elicitation_approval_and_denial(self):
        proposal = self.ledger.prepare({"kind": "post", "text": "Approved post"})
        denied = self.ledger.prepare({"kind": "post", "text": "Denied post"})
        frames = self.run_session([
            self.call(1, "linkedin_execute_action", {"proposal_id": proposal["proposal_id"], "digest": proposal["digest"]}),
            {"jsonrpc": "2.0", "id": "linkedin-approval-1", "result": {"action": "accept", "content": {"approve": True}}},
            self.call(2, "linkedin_execute_action", {"proposal_id": denied["proposal_id"], "digest": denied["digest"]}),
            {"jsonrpc": "2.0", "id": "linkedin-approval-2", "result": {"action": "decline"}},
        ], capabilities={"elicitation": {}})
        self.assertEqual(frames[1]["method"], "elicitation/create")
        self.assertIn("Approved post", frames[1]["params"]["message"])
        self.assertEqual(self.payload(frames[2])["status"], "done")
        self.assertEqual(frames[3]["method"], "elicitation/create")
        self.assertTrue(frames[4]["result"]["isError"])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.ledger.get(denied["proposal_id"])["status"], "pending")

    def test_client_mode_does_not_elicit(self):
        proposal = self.ledger.prepare({"kind": "post", "text": "Client approved"})
        frames = self.run_session([self.call(1, "linkedin_execute_action", {"proposal_id": proposal["proposal_id"], "digest": proposal["digest"]})])
        self.assertEqual(self.payload(frames[1])["status"], "done")

    def test_strict_elicit_mode_refuses_unsupported_clients(self):
        proposal = self.ledger.prepare({"kind": "post", "text": "Needs elicitation"})
        frames = self.run_session([self.call(1, "linkedin_execute_action", {"proposal_id": proposal["proposal_id"], "digest": proposal["digest"]})], approval_mode="elicit")
        self.assertTrue(frames[1]["result"]["isError"])
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
