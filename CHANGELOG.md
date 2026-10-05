# Changelog

## 0.1.0 — 2026-10-05

First standalone release, extracted from the LinkedIn layer of the Grace assistant.

- MCP stdio server with five tools: status, prepare, execute, list and cancel.
- Official LinkedIn API support: text and link posts, comments, reactions, post deletion.
- Two-phase, digest-bound, single-use, expiring proposals; no retry on uncertain results; daily write cap.
- Server-side human approval through MCP elicitation, with `auto`, `elicit` and `client` modes.
- `linkedin-mcp login` OAuth flow (local callback or `--paste`), `status`, `logout`.
- Zero third-party dependencies; Python 3.9+; 18 unit tests; GitHub Actions CI.
