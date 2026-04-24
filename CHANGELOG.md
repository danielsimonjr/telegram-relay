# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-04-23

### Changed (breaking)
- Server-reported FastMCP name is now `telegram_relay_mcp` (was
  `telegram-relay`) to match the `{service}_mcp` naming convention from the
  MCP Python guide.
- All four tools are now `telegram_`-prefixed to avoid collisions with other
  MCP servers in the same client session:
  - `check_telegram_messages` → `telegram_check_messages`
  - `get_pending_messages` → `telegram_get_pending_messages`
  - `acknowledge_messages` → `telegram_acknowledge_messages`
  - `relay_status` → `telegram_relay_status`

### Migration
- Any hook or script that invokes these tools by name needs to be updated to
  the new prefixed names. The `.mcp.json` client-side key (e.g.,
  `"telegram-relay"`) is unchanged and can still be any alias.

## [0.1.0] - 2026-04-23

Initial public release.

### Added
- FastMCP-based server (`server.py`) exposing 4 tools over stdio:
  `check_telegram_messages` (one-shot Telegram peek + queue append),
  `get_pending_messages` (read local queue), `acknowledge_messages`
  (mark delivered, remove from queue), `relay_status` (diagnostic).
- JSONL queue at `~/.claude/telegram-relay/queue.jsonl` with 24-hour default
  retention; delivered-ID ring buffer (last 1000) at `delivered.json`.
- Bot-token loader reading `~/.claude/channels/telegram/.env`.
- Allow-list loader reading `~/.claude/channels/telegram/access.json`
  (`allowFrom` array of user IDs).
- Stdlib-only HTTP via `urllib` — no third-party HTTP clients.
- `pyproject.toml` with `mcp>=1.0.0` as the only runtime dependency.
- `README.md` documenting installation, configuration, the peek-don't-poll
  strategy for coexisting with long-polling Telegram clients, examples, and
  security notes.
- MIT license.
