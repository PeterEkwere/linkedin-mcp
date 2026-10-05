"""Command line: ``linkedin-mcp [serve|login|status|logout]``."""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__, config as config_module
from .actions import ActionLedger
from .api import LinkedInClient, LinkedInError
from .oauth import LoginError, login
from .server import Server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="linkedin-mcp", description="Approval-gated LinkedIn tools for MCP-compatible AI agents.")
    parser.add_argument("--version", action="version", version=f"linkedin-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="Run the MCP server on stdio (default). Your MCP client starts this for you.")
    login_parser = sub.add_parser("login", help="Connect your LinkedIn account with OAuth.")
    login_parser.add_argument("--paste", action="store_true", help="Paste the redirect URL instead of running a local callback server (for headless machines).")
    login_parser.add_argument("--no-browser", action="store_true", help="Print the login URL without opening a browser.")
    sub.add_parser("status", help="Show connection status and today's usage.")
    sub.add_parser("logout", help="Delete the stored LinkedIn token.")
    args = parser.parse_args(argv)
    config = config_module.load()

    if args.command in (None, "serve"):
        Server(config).serve()
        return 0
    if args.command == "login":
        try:
            token = login(config, paste=args.paste, open_browser=not args.no_browser)
        except LoginError as error:
            print(f"Login failed: {error}", file=sys.stderr)
            return 1
        days = max(0, (token["expires_at"] - time.time()) // 86_400)
        print(f"Connected. Token saved to {config.token_file} (valid for about {int(days)} days).")
        return 0
    if args.command == "status":
        try:
            print(json.dumps(ActionLedger(config, LinkedInClient(config)).status(), indent=2))
        except LinkedInError as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    if args.command == "logout":
        config.token_file.unlink(missing_ok=True)
        config.member_cache.unlink(missing_ok=True)
        print("LinkedIn token removed.")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
