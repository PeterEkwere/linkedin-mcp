"""Two-phase LinkedIn actions: prepare an exact proposal, then execute it once.

Safety properties:

* A proposal stores the exact HTTP request that will be sent. Its SHA-256
  digest binds the request to the connected member, and execution recomputes it,
  so nothing can be changed between approval and execution.
* Proposals expire and are single-use: status moves pending -> sending -> done,
  and "sending" is persisted *before* the network call.
* An uncertain result (network error, timeout, HTTP 5xx) is recorded as
  ``unknown`` and is never retried automatically, so an agent cannot
  double-post. An identical action cannot be re-prepared for 24 hours after a
  completed or uncertain attempt.
* A local daily write cap stays below LinkedIn's member limits.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .api import LinkedInClient, LinkedInError
from .config import Config
from .storage import private_dir, read_json, write_json

MAX_POST_CHARS = 3_000
MAX_COMMENT_CHARS = 1_250
REACTIONS = ("LIKE", "PRAISE", "EMPATHY", "INTEREST", "APPRECIATION", "ENTERTAINMENT")
KINDS = ("post", "comment", "react", "delete_post")
_URN_RE = re.compile(r"urn:li:(activity|share|ugcPost):(\d{6,25})")
_ACTIVITY_SLUG_RE = re.compile(r"-activity-(\d{6,25})-")
_ID_RE = re.compile(r"[0-9a-f]{32}")


def post_urn(value: str) -> str:
    """Extract an activity/share/ugcPost URN from a LinkedIn post URL or URN."""
    text = urllib.parse.unquote(str(value or "").strip())
    match = _URN_RE.search(text)
    if match:
        return f"urn:li:{match.group(1)}:{match.group(2)}"
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme == "https" and (parsed.hostname or "").endswith("linkedin.com"):
        slug = _ACTIVITY_SLUG_RE.search(parsed.path + "-")
        if slug:
            return f"urn:li:activity:{slug.group(1)}"
    raise LinkedInError("That is not a recognisable LinkedIn post link. Use the post's share link or URN.")


def clean_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    text = "".join(c for c in text if c in "\n\t" or not (ord(c) < 32 or 0x7F <= ord(c) < 0xA0))
    if not text:
        raise LinkedInError("Text is required.")
    if len(text) > limit:
        raise LinkedInError(f"Text is longer than LinkedIn's {limit}-character limit.")
    return text


def build_request(kind: str, arguments: Dict[str, Any], person: str) -> Tuple[dict, str]:
    """Return the exact LinkedIn HTTP request and a human-readable preview."""
    if kind == "post":
        text = clean_text(arguments.get("text"), MAX_POST_CHARS)
        visibility = arguments.get("visibility") or "PUBLIC"
        if visibility not in ("PUBLIC", "CONNECTIONS"):
            raise LinkedInError("Visibility must be PUBLIC or CONNECTIONS.")
        body: Dict[str, Any] = {
            "author": person,
            "commentary": text,
            "visibility": visibility,
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        preview = f"POST to LinkedIn ({visibility}):\n{text}"
        link = str(arguments.get("link_url") or "").strip()
        if link:
            parsed = urllib.parse.urlparse(link)
            if parsed.scheme != "https" or not parsed.hostname:
                raise LinkedInError("Links must be https URLs.")
            title = clean_text(arguments.get("link_title") or parsed.hostname, 200)
            article: Dict[str, Any] = {"source": link, "title": title}
            if arguments.get("link_description"):
                article["description"] = clean_text(arguments["link_description"], 300)
            body["content"] = {"article": article}
            preview += f"\n[link card] {title} — {link}"
        return {"method": "POST", "path": "/rest/posts", "body": body}, preview
    if kind == "comment":
        target = post_urn(arguments.get("post_url", ""))
        text = clean_text(arguments.get("text"), MAX_COMMENT_CHARS)
        path = f"/rest/socialActions/{urllib.parse.quote(target, safe='')}/comments"
        body = {"actor": person, "object": target, "message": {"text": text}}
        return {"method": "POST", "path": path, "body": body}, f"COMMENT on {target}:\n{text}"
    if kind == "react":
        target = post_urn(arguments.get("post_url", ""))
        reaction = arguments.get("reaction") or "LIKE"
        if reaction not in REACTIONS:
            raise LinkedInError("Unsupported reaction.")
        path = f"/rest/reactions?actor={urllib.parse.quote(person, safe='')}"
        return {"method": "POST", "path": path, "body": {"root": target, "reactionType": reaction}}, f"REACT {reaction} on {target}"
    if kind == "delete_post":
        target = post_urn(arguments.get("post_url", ""))
        if target.startswith("urn:li:activity:"):
            raise LinkedInError("Deleting needs the post's share or ugcPost URN, as returned when it was published.")
        return {"method": "DELETE", "path": f"/rest/posts/{urllib.parse.quote(target, safe='')}", "body": None}, f"DELETE your post {target}"
    raise LinkedInError(f"Unknown action kind. Use one of: {', '.join(KINDS)}.")


def digest_of(request: dict, person: str) -> str:
    canonical = json.dumps({"person": person, "request": request}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def public_view(proposal: dict) -> dict:
    keys = ("proposal_id", "kind", "preview", "digest", "status", "expires_at", "result", "error")
    return {k: proposal[k] for k in keys if k in proposal}


class ActionLedger:
    """Persists proposals and enforces the prepare -> approve -> execute-once lifecycle."""

    def __init__(self, config: Config, client: LinkedInClient) -> None:
        self.config = config
        self.client = client

    # -- storage -----------------------------------------------------------

    def _path(self, proposal_id: str):
        if not isinstance(proposal_id, str) or not _ID_RE.fullmatch(proposal_id):
            raise LinkedInError("Invalid proposal id.")
        return private_dir(self.config.proposals_dir) / f"{proposal_id}.json"

    def get(self, proposal_id: str) -> dict:
        proposal = read_json(self._path(proposal_id))
        if not proposal:
            raise LinkedInError("No such proposal.")
        return proposal

    def all(self) -> List[dict]:
        out = []
        for path in private_dir(self.config.proposals_dir).glob("*.json"):
            value = read_json(path)
            if value:
                out.append(value)
        return sorted(out, key=lambda p: p.get("created_at", 0), reverse=True)

    def _usage(self) -> dict:
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        usage = read_json(self.config.usage_file) or {}
        return usage if usage.get("day") == today else {"day": today, "writes": 0}

    # -- lifecycle ---------------------------------------------------------

    def prepare(self, arguments: Dict[str, Any]) -> dict:
        kind = str(arguments.get("kind", ""))
        if kind not in KINDS:
            raise LinkedInError(f"Unknown action kind. Use one of: {', '.join(KINDS)}.")
        person = self.client.member()["person_urn"]
        request, preview = build_request(kind, arguments, person)
        digest = digest_of(request, person)
        now = time.time()
        for old in self.all():
            recent = old.get("finished_at", old.get("created_at", 0)) > now - 86_400
            if old.get("digest") == digest and recent and old.get("status") in ("sending", "unknown", "done"):
                raise LinkedInError("This exact action was already performed or has an uncertain result. Check it on LinkedIn before trying again.")
        proposal = {
            "proposal_id": uuid.uuid4().hex,
            "kind": kind,
            "person": person,
            "request": request,
            "preview": preview,
            "digest": digest,
            "status": "pending",
            "created_at": now,
            "expires_at": int(now) + self.config.proposal_ttl_seconds,
        }
        write_json(self._path(proposal["proposal_id"]), proposal)
        return public_view(proposal)

    def check_executable(self, proposal_id: str, digest: str) -> dict:
        proposal = self.get(proposal_id)
        invalid = "That proposal is invalid, already used, or expired. Nothing was done."
        if proposal.get("status") != "pending" or int(proposal.get("expires_at", 0)) < int(time.time()):
            raise LinkedInError(invalid)
        if proposal.get("digest") != digest or digest_of(proposal["request"], proposal["person"]) != proposal["digest"]:
            raise LinkedInError(invalid)
        return proposal

    def cancel(self, proposal_id: str) -> dict:
        proposal = self.get(proposal_id)
        if proposal.get("status") != "pending":
            raise LinkedInError("Only a pending proposal can be cancelled.")
        proposal.update(status="cancelled", finished_at=int(time.time()))
        write_json(self._path(proposal_id), proposal)
        return public_view(proposal)

    def execute(self, proposal_id: str, digest: str) -> dict:
        proposal = self.check_executable(proposal_id, digest)
        path = self._path(proposal_id)
        if self.client.member()["person_urn"] != proposal["person"]:
            raise LinkedInError("The connected LinkedIn account changed. Nothing was done.")
        usage = self._usage()
        if usage["writes"] >= self.config.daily_write_cap:
            raise LinkedInError(f"The daily write cap ({self.config.daily_write_cap}) is reached. Nothing was done.")

        # Consume the proposal before touching the network.
        proposal["status"] = "sending"
        write_json(path, proposal)
        usage["writes"] += 1
        write_json(self.config.usage_file, usage)

        uncertain = "LinkedIn did not confirm the result. Do not retry; check LinkedIn first."
        request = proposal["request"]
        try:
            status, headers, body = self.client.request(request["method"], request["path"], request["body"])
        except LinkedInError as error:
            if error.http_status is not None and 400 <= error.http_status < 500:
                proposal.update(status="failed", error=str(error), finished_at=int(time.time()))
                write_json(path, proposal)
                raise
            proposal.update(status="unknown", error=uncertain, finished_at=int(time.time()))
            write_json(path, proposal)
            raise LinkedInError(uncertain) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            proposal.update(status="unknown", error=uncertain, finished_at=int(time.time()))
            write_json(path, proposal)
            raise LinkedInError(uncertain) from None

        result: Dict[str, Any] = {"kind": proposal["kind"], "http_status": status}
        created = headers.get("x-restli-id") or (body.get("id") if isinstance(body, dict) else None)
        if proposal["kind"] == "post" and created:
            result["post_urn"] = created
            result["post_url"] = f"https://www.linkedin.com/feed/update/{created}/"
        proposal.update(status="done", result=result, finished_at=int(time.time()))
        write_json(path, proposal)
        return public_view(proposal)

    def status(self) -> dict:
        token = read_json(self.config.token_file)
        usage = self._usage()
        result: Dict[str, Any] = {
            "connected": False,
            "writes_today": usage["writes"],
            "daily_write_cap": self.config.daily_write_cap,
            "approval_mode": self.config.approval_mode,
            "can_do": ["publish text or link posts", "comment on a post", "react to a post", "delete a post you published"],
            "cannot_do": ["read the feed, inbox or connections", "send messages or invitations", "search people (not available in LinkedIn's open API)"],
        }
        if token:
            remaining = int(token.get("expires_at", 0)) - int(time.time())
            result["connected"] = remaining > 0
            result["token_days_left"] = max(0, remaining // 86_400)
            if remaining > 0:
                result["member"] = self.client.member()["name"]
        return result

    def pending(self) -> List[dict]:
        now = time.time()
        return [public_view(p) for p in self.all() if p.get("status") == "pending" and p.get("expires_at", 0) > now]

    def recent(self, limit: int = 10) -> List[dict]:
        return [public_view(p) for p in self.all()[:limit]]
