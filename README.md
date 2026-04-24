# telegram-relay

An MCP (Model Context Protocol) server that buffers Telegram bot messages for
Claude Code so none get dropped when no session is running. Built on
[FastMCP](https://github.com/modelcontextprotocol/python-sdk).

**Why this exists.** Telegram's long-polling `getUpdates` API can only have one
consumer at a time. The official Telegram Claude Code plugin holds that consumer
connection while a session is active — which means every message sent while you
*aren't* in a session is delivered to the plugin's next long-poll, and anything
between session end and the next start window can be missed.

`telegram-relay` works around that by deliberately **not** long-polling. It uses
one-shot `getUpdates` calls with `offset=-N` to *peek* at recent updates without
consuming them, then stores new messages in a local JSONL queue. A `SessionStart`
hook calls `telegram_check_messages` on startup so anything missed while offline
gets surfaced on the next session.

---

## Tools

All tool names are prefixed `telegram_` to avoid collisions with other MCP servers.

| Tool | Purpose | Read-only | Calls Telegram API |
|---|---|---|---|
| `telegram_check_messages` | Fetch recent messages one-shot, append new ones to the local queue, return everything pending. Safe to call anytime — does not interfere with the official plugin. | No (writes queue) | Yes |
| `telegram_get_pending_messages` | Return messages already in the local queue. Does not hit Telegram. | Yes | No |
| `telegram_acknowledge_messages` | Mark a list of message IDs as delivered and remove them from the queue. | No | No |
| `telegram_relay_status` | Report queue depth, delivered count, token-configured flag, and allowed-user list. Useful for diagnostics. | Yes | No |

---

## How it stays out of the official plugin's way

Telegram allows exactly one active `getUpdates` consumer per bot token. The
official plugin holds a long-poll (`timeout=30+`) for its full session. If
`telegram-relay` also long-polled, one of them would lose messages.

Instead, `fetch_recent_messages` uses `offset=-1` with `timeout=1`:

- `offset=-1` asks for the *last* update only, which is a peek — it does not mark
  older updates as consumed.
- `timeout=1` makes the call return immediately whether or not new messages are
  ready, so there is no window where two consumers are both blocked on the same
  endpoint.

The result is that both the official plugin and this relay can coexist: the
plugin handles real-time delivery during active sessions, and the relay catches
up on anything that landed outside those windows.

---

## Installation

### Prerequisites

- Python 3.10 or newer
- A Telegram bot ([create one via @BotFather](https://t.me/BotFather))
- The user IDs of anyone allowed to message the bot (`/start` in Telegram, then
  check `from.id` in the bot's update payload)

### Install

```bash
git clone https://github.com/danielsimonjr/telegram-relay.git
cd telegram-relay
pip install -e .
```

Or, for dependencies only:

```bash
pip install -r requirements.txt
```

The only runtime dependency is the `mcp` SDK. All HTTP is handled by stdlib
`urllib`, so there is no `requests`, `httpx`, or `aiohttp` involved.

---

## Configuration

The server reads two files on startup, both under `~/.claude/channels/telegram/`.

### 1. Bot token

Create `~/.claude/channels/telegram/.env` with your bot token:

```ini
TELEGRAM_BOT_TOKEN=123456789:AA...your-token-here...
```

The file is only read for lines starting with `TELEGRAM_BOT_TOKEN=` — you can
put other variables in it without conflict.

### 2. Allowed users (recommended)

Create `~/.claude/channels/telegram/access.json` with the list of Telegram user
IDs allowed to send messages:

```json
{
  "allowFrom": ["123456789", "987654321"]
}
```

> **Security note.** If `access.json` is missing or its `allowFrom` list is
> empty, **every message reaching the bot is accepted**. Always set an
> allow-list in production — a public bot name can receive spam from anyone.

### 3. Optional environment variable

- `TELEGRAM_RELAY_MAX_AGE` — max age in seconds for messages to remain in the
  queue after `telegram_check_messages`. Default: `86400` (24 hours).

### State files created at runtime

On first run, the server creates `~/.claude/telegram-relay/` with:

- `queue.jsonl` — newline-delimited JSON queue of pending messages
- `delivered.json` — last 1000 acknowledged message IDs (deduped so restarts
  don't replay old messages)

---

## Running the server

### Directly (for testing)

```bash
python server.py
```

Communicates over stdio; no interactive output until MCP messages arrive on
stdin.

### With the MCP Inspector

```bash
npx @modelcontextprotocol/inspector python server.py
```

### Registering with Claude Code

Add an entry to your `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "telegram-relay": {
      "command": "python",
      "args": [
        "-X", "utf8",
        "C:\\path\\to\\telegram-relay\\server.py"
      ]
    }
  }
}
```

The `-X utf8` flag is recommended on Windows so that non-ASCII message text does
not trip up the default `cp1252` encoding.

Restart Claude Code for the registration to take effect.

---

## Examples

**Check for missed messages at session start:**

```
Agent: telegram_check_messages(count=50)
Result: {
  "status": "ok",
  "count": 2,
  "new_from_telegram": 2,
  "messages": [
    {
      "message_id": "4231",
      "chat_id": "123456789",
      "user": "alice",
      "text": "remember to run tests before pushing",
      "date": "2026-04-23T14:02:17",
      "age_seconds": 1847
    },
    ...
  ]
}
```

**Process and acknowledge:**

```
Agent: telegram_acknowledge_messages(message_ids=["4231", "4232"])
Result: {"status": "ok", "acknowledged": 2}
```

**Diagnostic:**

```
Agent: telegram_relay_status()
Result: {
  "status": "ok",
  "queue_depth": 0,
  "delivered_count": 127,
  "token_configured": true,
  "allowed_users": ["123456789"],
  "queue_file": "C:\\Users\\you\\.claude\\telegram-relay\\queue.jsonl",
  "max_message_age_seconds": 86400
}
```

---

## Pairing with a SessionStart hook

The intended deployment is a Claude Code `SessionStart` hook that calls
`telegram_check_messages` automatically, so missed messages surface the moment
you open a session. A minimal Python hook script:

```python
#!/usr/bin/env python3
# ~/.claude/hooks/telegram-check-pending.py
# Registered in settings.json under hooks.SessionStart.
# (This script is a suggestion, not shipped with the repo.)
import subprocess, sys
# Call the MCP tool through Claude's tool router, or use the relay's queue
# file directly if you prefer a hook that doesn't need the MCP layer.
```

How you wire this up depends on your preferred hook style; the relay itself
makes no assumptions about it.

---

## Security notes

- `.env` holds the bot token — keep it out of version control (`.gitignore`
  already excludes `.env*`).
- Set `access.json` with explicit user IDs. Empty or missing `access.json`
  means the relay accepts messages from anyone who knows the bot's handle.
- Messages starting with `/` are filtered out (treated as bot commands, not
  user content).
- The queue is stored in plaintext JSONL — on a shared machine, consider
  tightening permissions on `~/.claude/telegram-relay/`.
- Communication with Telegram is HTTPS only (`https://api.telegram.org`).
- The server has no inbound network surface — it's stdio-only for MCP and
  outbound-only for Telegram.
- Logs go to stderr, never stdout (stdout is reserved for MCP protocol frames).

---

## Development

```bash
# Syntax check
python -m py_compile server.py

# Smoke-test via the inspector
npx @modelcontextprotocol/inspector python server.py
```

Update `CHANGELOG.md` in the same commit as tool-surface changes.

---

## License

MIT — see [LICENSE](LICENSE).
