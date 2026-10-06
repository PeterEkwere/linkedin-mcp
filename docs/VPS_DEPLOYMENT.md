# Running linkedin-mcp on a VPS (browser module + VNC login)

This guide covers the optional browser module. It runs a real Chrome session,
logged in as you, on a headless server, so an agent can read your own LinkedIn
inbox. You log in once through a VNC screen that is only reachable over an SSH
tunnel, and the session is saved in a local Chrome profile after that.

The official-API server (posting, comments, reactions) does not need any of
this. This guide is only for the browser module.

> ## Read this first
> Automating the LinkedIn website is against LinkedIn's User Agreement and can
> get your account restricted or banned. Only run this on your own account, at
> low volume, for your own use. You are taking that risk yourself. This project
> does not try to defeat LinkedIn's bot detection, and you should not use it for
> scraping other people at scale or for any kind of bulk outreach.

## What you need

- A Linux VPS you control (Ubuntu 22.04 or 24.04 is assumed below).
- SSH access to it.
- About 1.5 to 2 GB of RAM free for Chrome.
- Your own LinkedIn account.

## 1. Install the system packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip \
    xvfb x11vnc novnc websockify \
    fonts-liberation libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2
```

`xvfb` gives Chrome a virtual screen, `x11vnc` shares that screen, and
`websockify` / `novnc` let you open it in your browser.

## 2. Install linkedin-mcp with the browser extra

```bash
python3 -m venv ~/linkedin-mcp-venv
~/linkedin-mcp-venv/bin/pip install "linkedin-mcp[browser] @ git+https://github.com/PeterEkwere/linkedin-mcp"
```

SeleniumBase will download a matching Chrome the first time it runs. If you want
to pin a specific Chrome, install it yourself and set `LINKEDIN_MCP_CHROME` to
its path.

## 3. Run the worker

The worker owns Chrome and listens on a Unix socket. Run it as a normal,
unprivileged user, not root.

```bash
~/linkedin-mcp-venv/bin/linkedin-mcp-browser worker
```

Leave it running (a systemd service is set up in step 6). On first start it
creates its state under `~/.linkedin-mcp/browser/` (the Chrome profile, the
socket and the X authority file), all private to your user.

Check it from a second shell:

```bash
~/linkedin-mcp-venv/bin/linkedin-mcp-browser status
# {"ok": true, "result": {"available": true, "connected": false, "login_required": true, ...}}
```

`connected: false` is expected until you log in.

## 4. Log in once, over an SSH tunnel

The login screen is never exposed to the internet. It only listens on
`127.0.0.1`, and you reach it by forwarding the port over SSH.

On the server, open the login window:

```bash
~/linkedin-mcp-venv/bin/linkedin-mcp-browser login
# {"ok": true, "result": {"login_window": "ready", "novnc_port": 6087,
#   "url": "http://127.0.0.1:6087/vnc.html", "expires_in_seconds": 1200, ...}}
```

On your own computer, forward that port over SSH:

```bash
ssh -N -L 6087:127.0.0.1:6087 youruser@your-vps
```

Then open <http://127.0.0.1:6087/vnc.html> in your local browser, click
**Connect**, and you will see the Chrome window with the LinkedIn login page.
Sign in normally, including any verification or 2FA. When the feed loads, you
are done.

Close the login screen (it also closes itself after 20 minutes):

```bash
~/linkedin-mcp-venv/bin/linkedin-mcp-browser login-close
~/linkedin-mcp-venv/bin/linkedin-mcp-browser status   # connected: true
```

Tear down the SSH tunnel (Ctrl-C). The session persists in the Chrome profile,
so you only repeat this when LinkedIn signs you out.

## 5. Point your agent at it

Run the browser MCP front end, which talks to the worker over the socket:

```bash
~/linkedin-mcp-venv/bin/linkedin-mcp-browser mcp
```

Add it to your MCP client the same way as the main server. For a client running
on the VPS:

```json
{
  "mcpServers": {
    "linkedin-browser": {
      "command": "/home/youruser/linkedin-mcp-venv/bin/linkedin-mcp-browser",
      "args": ["mcp"]
    }
  }
}
```

Tools: `linkedin_browser_status`, `linkedin_list_conversations`,
`linkedin_read_conversation`. All read-only.

## 6. Keep the worker running with systemd

`~/.config/systemd/user/linkedin-mcp-browser.service`:

```ini
[Unit]
Description=linkedin-mcp browser worker
After=network-online.target

[Service]
ExecStart=%h/linkedin-mcp-venv/bin/linkedin-mcp-browser worker
Restart=on-failure
RestartSec=10
MemoryMax=2G
UMask=0077

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now linkedin-mcp-browser
loginctl enable-linger "$USER"   # keep it running after you log out
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LINKEDIN_MCP_HOME` | `~/.linkedin-mcp` | Base state directory |
| `LINKEDIN_MCP_BROWSER_STATE` | `<home>/browser` | Chrome profile, socket, X auth |
| `LINKEDIN_MCP_DISPLAY` | `:87` | Xvfb display number |
| `LINKEDIN_MCP_VNC_PORT` | `5907` | x11vnc port (localhost only) |
| `LINKEDIN_MCP_NOVNC_PORT` | `6087` | noVNC/websockify port (localhost only) |
| `LINKEDIN_MCP_NOVNC_WEB` | `/usr/share/novnc` | noVNC web root |
| `LINKEDIN_MCP_CHROME` | auto | Explicit Chrome binary path |
| `LINKEDIN_MCP_LOGIN_SECONDS` | `1200` | How long the login window stays open |

## Security notes

- The worker runs as your unprivileged user. Do not run it as root.
- VNC and noVNC bind to `127.0.0.1` only. The login screen has no password
  because the only way in is your authenticated SSH tunnel. Never bind these
  ports to a public interface or put them behind a public reverse proxy.
- The Chrome profile holds your live LinkedIn session. Keep
  `~/.linkedin-mcp/` locked down (it is created `0700`), and treat a server with
  it on the same as being logged into your LinkedIn.
- Close the login window when you are done; it is the only interactive surface.
- Everything the tools return from LinkedIn is untrusted data. Do not let an
  agent follow instructions found inside a message or profile.
