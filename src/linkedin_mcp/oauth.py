"""OAuth 2.0 authorization-code login for your own LinkedIn developer app."""

from __future__ import annotations

import http.server
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Optional

from .config import Config
from .storage import write_json

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
SCOPES = "openid profile w_member_social"


class LoginError(RuntimeError):
    pass


def authorize_url(config: Config, state: str) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "state": state,
        "scope": SCOPES,
    })
    return f"{AUTHORIZE_URL}?{query}"


def code_from_callback(callback_url: str, state: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url.strip()).query)
    if query.get("error"):
        raise LoginError("LinkedIn returned an error: " + query.get("error_description", query["error"])[0])
    if query.get("state", [""])[0] != state:
        raise LoginError("State mismatch: this callback does not belong to this login attempt.")
    code = query.get("code", [""])[0]
    if not code:
        raise LoginError("The callback URL has no authorization code.")
    return code


def exchange_code(config: Config, code: str) -> dict:
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.redirect_uri,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
    }).encode()
    request = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read(65536))
    except urllib.error.HTTPError as error:
        raise LoginError(f"Token exchange failed (HTTP {error.code}). Check the client ID, secret and redirect URL.") from None
    if not payload.get("access_token"):
        raise LoginError("LinkedIn did not return an access token.")
    return {
        "access_token": payload["access_token"],
        "expires_at": int(time.time()) + int(payload.get("expires_in", 0)),
        "scope": payload.get("scope", SCOPES),
    }


def _wait_for_callback(config: Config, timeout: int = 300) -> str:
    captured: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if not self.path.startswith("/callback"):
                self.send_error(404)
                return
            captured["url"] = config.redirect_uri + self.path[len("/callback"):]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h2>LinkedIn connected.</h2><p>You can close this tab and return to the terminal.</p>")

        def log_message(self, *args):  # silence default request logging
            pass

    server = http.server.HTTPServer(("127.0.0.1", config.redirect_port), Handler)
    server.timeout = 1
    deadline = time.time() + timeout
    try:
        while "url" not in captured and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "url" not in captured:
        raise LoginError("Timed out waiting for LinkedIn to redirect back.")
    return captured["url"]


def login(config: Config, paste: bool = False, open_browser: bool = True, callback_url: Optional[str] = None) -> dict:
    if not config.client_id or not config.client_secret:
        raise LoginError("Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET (from your LinkedIn developer app) first.")
    state = secrets.token_urlsafe(24)
    url = authorize_url(config, state)
    print("Open this URL, sign in to LinkedIn and allow access:\n\n" + url + "\n")
    if callback_url is None:
        if paste:
            callback_url = input("Paste the full URL you were redirected to: ")
        else:
            if open_browser:
                webbrowser.open(url)
            print(f"Waiting for the redirect on {config.redirect_uri} ...")
            callback_url = _wait_for_callback(config)
    token = exchange_code(config, code_from_callback(callback_url, state))
    write_json(config.token_file, token)
    return token
