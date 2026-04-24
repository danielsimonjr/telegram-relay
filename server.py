#!/usr/bin/env python3
"""Telegram Message Relay — Reliable message buffer for Claude Code.

Does NOT poll Telegram (that would conflict with the official plugin's long polling).
Instead, provides tools to:
1. Manually check Telegram for recent messages (one-shot getUpdates with offset=-N)
2. Read/write a persistent queue file that hooks can populate
3. Acknowledge processed messages

The SessionStart hook (telegram-check-pending.py) calls check_telegram_messages
at startup to catch anything missed while offline.
"""

import json
import os
import time
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime

from mcp.server.fastmcp import FastMCP

# ── Configuration ────────────────────────────────────────────────────────────
STATE_DIR = Path.home() / ".claude" / "channels" / "telegram"
RELAY_DIR = Path.home() / ".claude" / "telegram-relay"
QUEUE_FILE = RELAY_DIR / "queue.jsonl"
DELIVERED_FILE = RELAY_DIR / "delivered.json"
ENV_FILE = STATE_DIR / ".env"
ACCESS_FILE = STATE_DIR / "access.json"

RELAY_DIR.mkdir(parents=True, exist_ok=True)

# Load token
TOKEN = ""
try:
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            TOKEN = line.split("=", 1)[1].strip()
except Exception:
    pass

# Load allowed users
ALLOWED_USERS = set()
try:
    access = json.loads(ACCESS_FILE.read_text())
    ALLOWED_USERS = set(access.get("allowFrom", []))
except Exception:
    pass

MAX_MESSAGE_AGE = int(os.environ.get("TELEGRAM_RELAY_MAX_AGE", "86400"))


# ── Queue Operations ────────────────────────────────────────────────────────


def load_delivered_ids() -> set:
    if DELIVERED_FILE.exists():
        try:
            data = json.loads(DELIVERED_FILE.read_text())
            return set(str(i) for i in data.get("ids", []))
        except Exception:
            pass
    return set()


def save_delivered_ids(ids: set):
    recent = sorted(ids)[-1000:]
    DELIVERED_FILE.write_text(
        json.dumps({"ids": recent, "updated": datetime.now().isoformat()})
    )


def append_to_queue(message: dict):
    with open(QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")


def read_queue(max_age: int = MAX_MESSAGE_AGE) -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    messages = []
    now = time.time()
    for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
            if now - msg.get("timestamp", 0) < max_age:
                messages.append(msg)
        except json.JSONDecodeError:
            continue
    return messages


def remove_from_queue(message_ids: set):
    if not QUEUE_FILE.exists():
        return
    remaining = []
    for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
            if str(msg.get("message_id", "")) not in message_ids:
                remaining.append(line)
        except json.JSONDecodeError:
            remaining.append(line)
    QUEUE_FILE.write_text(
        "\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8"
    )


# ── Telegram API (one-shot, not polling) ─────────────────────────────────────


def fetch_recent_messages(count: int = 20) -> list[dict]:
    """One-shot fetch of recent updates from Telegram. Does NOT conflict with
    the official plugin because we use offset=-1 to peek without consuming."""
    if not TOKEN:
        return []

    # First, get the latest update_id
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    params = urllib.parse.urlencode({"offset": -1, "limit": 1, "timeout": 1})
    try:
        req = urllib.request.Request(f"{url}?{params}")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    if not data.get("ok") or not data.get("result"):
        return []

    latest_id = data["result"][-1]["update_id"]

    # Now fetch the last N updates (peek only — offset=-count doesn't consume)
    params = urllib.parse.urlencode(
        {"offset": max(1, latest_id - count + 1), "limit": count, "timeout": 1}
    )
    try:
        req = urllib.request.Request(f"{url}?{params}")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    if not data.get("ok"):
        return []

    delivered = load_delivered_ids()
    messages = []

    for update in data.get("result", []):
        message = update.get("message")
        if not message:
            continue

        from_user = message.get("from", {})
        user_id = str(from_user.get("id", ""))
        if ALLOWED_USERS and user_id not in ALLOWED_USERS:
            continue

        msg_id = str(message.get("message_id", ""))
        if msg_id in delivered:
            continue

        text = message.get("text", "")
        if not text or text.startswith("/"):
            continue

        messages.append(
            {
                "message_id": msg_id,
                "chat_id": str(message.get("chat", {}).get("id", "")),
                "user_id": user_id,
                "username": from_user.get("username", ""),
                "first_name": from_user.get("first_name", ""),
                "text": text,
                "timestamp": time.time(),
                "date": datetime.fromtimestamp(message.get("date", 0)).isoformat(),
            }
        )

    return messages


# ── FastMCP Server ───────────────────────────────────────────────────────────

mcp = FastMCP("telegram-relay")


@mcp.tool()
def check_telegram_messages(count: int = 20) -> str:
    """Check Telegram for recent messages that may have been missed. Fetches the last N messages from the Telegram API and returns any that haven't been acknowledged yet. Safe to call anytime — does not interfere with the official Telegram plugin."""
    messages = fetch_recent_messages(count)

    # Also include anything already in the queue
    queued = read_queue(max_age=3600)
    queued_ids = {str(m.get("message_id")) for m in queued}

    # Add new messages to queue
    new_count = 0
    for msg in messages:
        if str(msg["message_id"]) not in queued_ids:
            append_to_queue(msg)
            new_count += 1

    # Return all pending (queue + newly fetched)
    all_pending = read_queue(max_age=3600)

    if not all_pending:
        return json.dumps({"status": "ok", "count": 0, "messages": []})

    formatted = []
    now = time.time()
    for m in all_pending:
        formatted.append(
            {
                "message_id": m["message_id"],
                "chat_id": m["chat_id"],
                "user": m.get("username") or m.get("first_name", "unknown"),
                "text": m["text"],
                "date": m.get("date", ""),
                "age_seconds": int(now - m.get("timestamp", 0)),
            }
        )

    return json.dumps(
        {
            "status": "ok",
            "count": len(formatted),
            "new_from_telegram": new_count,
            "messages": formatted,
        },
        indent=2,
    )


@mcp.tool()
def get_pending_messages(max_age_seconds: int = 3600) -> str:
    """Get messages from the relay queue that haven't been acknowledged yet. Does not call the Telegram API — only reads the local queue file."""
    messages = read_queue(max_age=max_age_seconds)

    if not messages:
        return json.dumps({"status": "ok", "count": 0, "messages": []})

    now = time.time()
    formatted = []
    for m in messages:
        formatted.append(
            {
                "message_id": m["message_id"],
                "chat_id": m["chat_id"],
                "user": m.get("username") or m.get("first_name", "unknown"),
                "text": m["text"],
                "date": m.get("date", ""),
                "age_seconds": int(now - m.get("timestamp", 0)),
            }
        )

    return json.dumps(
        {"status": "ok", "count": len(formatted), "messages": formatted}, indent=2
    )


@mcp.tool()
def acknowledge_messages(message_ids: list[str]) -> str:
    """Mark messages as delivered/processed. Removes them from the queue."""
    id_set = set(str(i) for i in message_ids)
    delivered = load_delivered_ids()
    delivered.update(id_set)
    save_delivered_ids(delivered)
    remove_from_queue(id_set)
    return json.dumps({"status": "ok", "acknowledged": len(id_set)})


@mcp.tool()
def relay_status() -> str:
    """Check the relay server status: queue depth, delivered count, configuration."""
    queue = read_queue()
    delivered = load_delivered_ids()

    return json.dumps(
        {
            "status": "ok",
            "queue_depth": len(queue),
            "delivered_count": len(delivered),
            "token_configured": bool(TOKEN),
            "allowed_users": list(ALLOWED_USERS),
            "queue_file": str(QUEUE_FILE),
            "max_message_age_seconds": MAX_MESSAGE_AGE,
        },
        indent=2,
    )


if __name__ == "__main__":
    mcp.run()
