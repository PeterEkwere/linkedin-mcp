"""Minimal client for LinkedIn's official member APIs (no third-party dependencies)."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional, Tuple

from . import __version__
from .config import Config
from .storage import read_json, write_json

API = "https://api.linkedin.com"
MAX_RESPONSE_BYTES = 262_144


class LinkedInError(RuntimeError):
    def __init__(self, message: str, http_status: Optional[int] = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


class LinkedInClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def token(self) -> dict:
        token = read_json(self.config.token_file)
        if not token or not isinstance(token.get("access_token"), str):
            raise LinkedInError("LinkedIn is not connected. Run `linkedin-mcp login` first.")
        if int(token.get("expires_at", 0)) <= int(time.time()):
            raise LinkedInError("The LinkedIn token has expired. Run `linkedin-mcp login` again.")
        return token

    def request(self, method: str, path: str, payload: Optional[dict] = None, *, versioned: bool = True) -> Tuple[int, dict, Any]:
        headers = {
            "Authorization": "Bearer " + self.token()["access_token"],
            "Accept": "application/json",
            "User-Agent": f"linkedin-mcp/{__version__}",
        }
        if versioned:
            headers["LinkedIn-Version"] = self.config.api_version
            headers["X-Restli-Protocol-Version"] = "2.0.0"
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(API + path, data=body, method=method, headers=headers)
        try:
            with _OPENER.open(req, timeout=20) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
                response_headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                parsed = json.loads(error.read(4096) or b"{}")
                detail = str(parsed.get("message") or parsed.get("code") or "")[:200]
            except ValueError:
                pass
            message = f"LinkedIn rejected the request (HTTP {error.code})" + (f": {detail}" if detail else "") + "."
            raise LinkedInError(message, error.code) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise LinkedInError("LinkedIn response exceeded the size limit.")
        return status, response_headers, (json.loads(raw) if raw.strip() else None)

    def member(self) -> dict:
        """The connected member's URN and name, cached per token."""
        token = self.token()
        cached = read_json(self.config.member_cache)
        if cached and cached.get("token_fingerprint") == fingerprint(token["access_token"]):
            return cached
        _, _, info = self.request("GET", "/v2/userinfo", versioned=False)
        if not isinstance(info, dict) or not info.get("sub"):
            raise LinkedInError("LinkedIn did not return the member identity.")
        cached = {
            "person_urn": f"urn:li:person:{info['sub']}",
            "name": str(info.get("name", ""))[:160],
            "token_fingerprint": fingerprint(token["access_token"]),
        }
        write_json(self.config.member_cache, cached)
        return cached
