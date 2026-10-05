"""MCP server over stdio (JSON-RPC 2.0, newline-delimited).

Human approval is enforced in one of two ways, chosen per session:

* **elicit** - before executing, the server itself sends an MCP
  ``elicitation/create`` request showing the exact preview. Only the human's
  answer in the client UI can approve it; the model cannot approve on its own.
* **client** - the server relies on the MCP client's own tool-call confirmation
  (for example Claude Desktop asks before running a tool). Do not add
  ``linkedin_execute_action`` to any auto-approve list in this mode.

``auto`` (default) uses elicit when the client advertises the elicitation
capability, and client mode otherwise.
"""

from __future__ import annotations

import collections
import json
import sys
from typing import Any, Deque, Dict, Optional, TextIO

from . import __version__
from .actions import KINDS, MAX_POST_CHARS, REACTIONS, ActionLedger
from .api import LinkedInClient, LinkedInError
from .config import Config
from .storage import StorageError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def _schema(properties: Dict[str, Any], required: Optional[list] = None) -> dict:
    schema: Dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


TOOLS = [
    {
        "name": "linkedin_status",
        "description": "Show the connected LinkedIn member, token expiry, today's write usage, the approval mode, and what this server can and cannot do. Never writes to LinkedIn.",
        "inputSchema": _schema({}),
        "annotations": {"title": "LinkedIn status", "readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "linkedin_prepare_action",
        "description": (
            "Prepare (not perform) one LinkedIn action and return its exact preview, proposal_id and digest. "
            "kind=post (text, optional visibility PUBLIC|CONNECTIONS, optional link_url/link_title/link_description), "
            "comment (post_url, text), react (post_url, optional reaction), or delete_post (post_url of your own post). "
            "Always show the user the exact preview before calling linkedin_execute_action."
        ),
        "inputSchema": _schema(
            {
                "kind": {"type": "string", "enum": list(KINDS)},
                "text": {"type": "string", "maxLength": MAX_POST_CHARS},
                "visibility": {"type": "string", "enum": ["PUBLIC", "CONNECTIONS"], "default": "PUBLIC"},
                "link_url": {"type": "string", "maxLength": 2000},
                "link_title": {"type": "string", "maxLength": 200},
                "link_description": {"type": "string", "maxLength": 300},
                "post_url": {"type": "string", "maxLength": 2000},
                "reaction": {"type": "string", "enum": list(REACTIONS), "default": "LIKE"},
            },
            ["kind"],
        ),
        "annotations": {"title": "Prepare LinkedIn action", "readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "linkedin_execute_action",
        "description": (
            "Perform one prepared LinkedIn action exactly once, after the user approved its exact preview. "
            "Requires proposal_id and digest from linkedin_prepare_action. The server asks the user to confirm "
            "when the client supports it. Never call it again for a result reported as unknown."
        ),
        "inputSchema": _schema(
            {
                "proposal_id": {"type": "string", "minLength": 32, "maxLength": 32},
                "digest": {"type": "string", "minLength": 64, "maxLength": 64},
            },
            ["proposal_id", "digest"],
        ),
        "annotations": {"title": "Publish to LinkedIn", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "linkedin_list_actions",
        "description": "List pending proposals awaiting approval, or recent actions with their final status and receipts. Never writes to LinkedIn.",
        "inputSchema": _schema({"which": {"type": "string", "enum": ["pending", "recent"], "default": "pending"}}),
        "annotations": {"title": "List LinkedIn actions", "readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "linkedin_cancel_action",
        "description": "Cancel a pending proposal so it can never be executed. Does not undo anything already published.",
        "inputSchema": _schema({"proposal_id": {"type": "string", "minLength": 32, "maxLength": 32}}, ["proposal_id"]),
        "annotations": {"title": "Cancel LinkedIn action", "readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
]


class ApprovalDenied(LinkedInError):
    pass


class Server:
    def __init__(self, config: Config, ledger: Optional[ActionLedger] = None,
                 stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        self.config = config
        self.ledger = ledger or ActionLedger(config, LinkedInClient(config))
        self.stdin = stdin
        self.stdout = stdout
        self.client_can_elicit = False
        self._backlog: Deque[dict] = collections.deque()
        self._next_id = 0

    # -- transport ---------------------------------------------------------

    def _send(self, frame: dict) -> None:
        self.stdout.write(json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stdout.flush()

    def _read(self) -> Optional[dict]:
        if self._backlog:
            return self._backlog.popleft()
        while True:
            line = self.stdin.readline()
            if not line:
                return None
            if line.strip():
                try:
                    return json.loads(line)
                except ValueError:
                    self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})

    def _respond(self, request_id: Any, result: Any = None, error: Optional[dict] = None) -> None:
        frame: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        frame["error" if error is not None else "result"] = error if error is not None else result
        self._send(frame)

    # -- approval ----------------------------------------------------------

    def _approval_route(self) -> str:
        mode = self.config.approval_mode
        if mode == "elicit" or (mode == "auto" and self.client_can_elicit):
            return "elicit"
        return "client"

    def _elicit_approval(self, preview: str) -> None:
        if not self.client_can_elicit:
            raise ApprovalDenied("This server requires in-client approval (elicitation), which this MCP client does not support. Nothing was done.")
        self._next_id += 1
        request_id = f"linkedin-approval-{self._next_id}"
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "elicitation/create",
            "params": {
                "message": "Approve this LinkedIn action?\n\n" + preview,
                "requestedSchema": {
                    "type": "object",
                    "properties": {"approve": {"type": "boolean", "title": "Approve and publish", "description": "Tick to perform exactly this action once."}},
                    "required": ["approve"],
                },
            },
        })
        while True:
            message = self._read()
            if message is None:
                raise ApprovalDenied("The client disconnected before approving. Nothing was done.")
            if message.get("id") == request_id and "method" not in message:
                break
            self._backlog.append(message)  # handled after this call completes
        result = message.get("result") or {}
        if message.get("error") or result.get("action") != "accept" or (result.get("content") or {}).get("approve") is not True:
            raise ApprovalDenied("The user did not approve this action. Nothing was done.")

    # -- tools -------------------------------------------------------------

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        if name == "linkedin_status":
            return self.ledger.status()
        if name == "linkedin_prepare_action":
            proposal = self.ledger.prepare(arguments)
            route = self._approval_route()
            proposal["next_step"] = (
                "Show the user this exact preview. When they want it published, call linkedin_execute_action with "
                "this proposal_id and digest; the user will be asked to confirm in the client."
                if route == "elicit" else
                "Show the user this exact preview and wait for their explicit approval before calling "
                "linkedin_execute_action with this proposal_id and digest."
            )
            return proposal
        if name == "linkedin_execute_action":
            proposal_id, digest = arguments.get("proposal_id"), arguments.get("digest")
            proposal = self.ledger.check_executable(proposal_id, digest)
            if self._approval_route() == "elicit":
                self._elicit_approval(proposal["preview"])
            return self.ledger.execute(proposal_id, digest)
        if name == "linkedin_list_actions":
            return self.ledger.recent() if arguments.get("which") == "recent" else self.ledger.pending()
        if name == "linkedin_cancel_action":
            return self.ledger.cancel(arguments.get("proposal_id"))
        raise LinkedInError(f"Unknown tool: {name}")

    def handle(self, request: dict) -> None:
        request_id = request.get("id")
        method = request.get("method")
        if method is None or request_id is None:
            return  # notifications and stray responses need no reply
        try:
            if method == "initialize":
                params = request.get("params") or {}
                self.client_can_elicit = "elicitation" in (params.get("capabilities") or {})
                requested = params.get("protocolVersion")
                self._respond(request_id, {
                    "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "linkedin-mcp", "version": __version__},
                    "instructions": "LinkedIn writes are two-phase: prepare, show the user the exact preview, then execute once after approval. Never retry an unknown result.",
                })
            elif method == "ping":
                self._respond(request_id, {})
            elif method == "tools/list":
                self._respond(request_id, {"tools": TOOLS})
            elif method == "tools/call":
                params = request.get("params") or {}
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise LinkedInError("Tool arguments must be an object.")
                payload = self.call_tool(str(params.get("name", "")), arguments)
                self._respond(request_id, {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]})
            else:
                self._respond(request_id, error={"code": -32601, "message": f"Method not found: {method}"})
        except (LinkedInError, StorageError) as error:
            self._respond(request_id, {"content": [{"type": "text", "text": str(error)}], "isError": True})
        except (ValueError, TypeError, KeyError):
            self._respond(request_id, {"content": [{"type": "text", "text": "Invalid request arguments."}], "isError": True})

    def serve(self) -> None:
        while True:
            message = self._read()
            if message is None:
                return
            if isinstance(message, dict):
                self.handle(message)
