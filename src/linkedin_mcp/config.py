"""Runtime configuration, read from environment variables.

Everything the server stores (OAuth token, proposals, usage counters) lives in
one private directory, by default ``~/.linkedin-mcp``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APPROVAL_MODES = ("auto", "elicit", "client")


@dataclass(frozen=True)
class Config:
    home: Path
    api_version: str
    daily_write_cap: int
    proposal_ttl_seconds: int
    approval_mode: str
    client_id: str
    client_secret: str
    redirect_port: int

    @property
    def token_file(self) -> Path:
        return self.home / "token.json"

    @property
    def proposals_dir(self) -> Path:
        return self.home / "proposals"

    @property
    def usage_file(self) -> Path:
        return self.home / "usage.json"

    @property
    def member_cache(self) -> Path:
        return self.home / "member.json"

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.redirect_port}/callback"


def load() -> Config:
    mode = os.environ.get("LINKEDIN_MCP_APPROVAL", "auto").strip().lower()
    if mode not in APPROVAL_MODES:
        raise SystemExit(f"LINKEDIN_MCP_APPROVAL must be one of: {', '.join(APPROVAL_MODES)}")
    return Config(
        home=Path(os.environ.get("LINKEDIN_MCP_HOME", "~/.linkedin-mcp")).expanduser(),
        api_version=os.environ.get("LINKEDIN_API_VERSION", "202608"),
        daily_write_cap=int(os.environ.get("LINKEDIN_MCP_DAILY_WRITE_CAP", "40")),
        proposal_ttl_seconds=int(os.environ.get("LINKEDIN_MCP_PROPOSAL_TTL", "1800")),
        approval_mode=mode,
        client_id=os.environ.get("LINKEDIN_CLIENT_ID", ""),
        client_secret=os.environ.get("LINKEDIN_CLIENT_SECRET", ""),
        redirect_port=int(os.environ.get("LINKEDIN_MCP_REDIRECT_PORT", "8765")),
    )
