#!/usr/bin/env python3
"""Opt-in: make Radarr ping you when a movie finishes importing to the NAS.

Adds a Radarr notification connection (Telegram OR Discord) that fires on
import (and optionally on grab). Idempotent — re-running updates the existing
connection instead of duplicating it. Nothing here is enabled automatically;
run it yourself when you want the pings.

Telegram:
    export RADARR_URL="http://<nas-ip>:7878"
    export RADARR_API_KEY="<api key>"
    export TELEGRAM_BOT_TOKEN="<bot token>"
    export TELEGRAM_CHAT_ID="<your chat id>"
    python3 radarr-notify.py telegram

Discord:
    export RADARR_URL="http://<nas-ip>:7878"
    export RADARR_API_KEY="<api key>"
    export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
    python3 radarr-notify.py discord
"""
import json, os, sys, urllib.request, urllib.error

RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
API_KEY = os.environ.get("RADARR_API_KEY", "")
kind = sys.argv[1] if len(sys.argv) > 1 else ""
if not RADARR_URL or not API_KEY or kind not in ("telegram", "discord"):
    sys.exit(__doc__)
BASE = f"{RADARR_URL}/api/v3"


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
        headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} on {method} {path}: {e.read().decode()[:400]}")


COMMON = {  # fire on import + upgrade; grab optional
    "onGrab": os.environ.get("NOTIFY_ON_GRAB", "false").lower() == "true",
    "onDownload": True, "onUpgrade": True, "onMovieDelete": False,
    "onHealthIssue": False, "onApplicationUpdate": False,
    "includeHealthWarnings": False,
}

if kind == "telegram":
    bot, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot or not chat:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
    conn = {**COMMON, "name": "Telegram (import)", "implementation": "Telegram",
            "implementationName": "Telegram", "configContract": "TelegramSettings",
            "fields": [{"name": "botToken", "value": bot}, {"name": "chatId", "value": chat}]}
else:
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not hook:
        sys.exit("Set DISCORD_WEBHOOK_URL.")
    conn = {**COMMON, "name": "Discord (import)", "implementation": "Discord",
            "implementationName": "Discord", "configContract": "DiscordSettings",
            "fields": [{"name": "webHookUrl", "value": hook}]}

existing = {n["name"]: n for n in api("GET", "/notification")}
if conn["name"] in existing:
    conn["id"] = existing[conn["name"]]["id"]
    api("PUT", f"/notification/{conn['id']}", conn)
    print(f"Updated notification: {conn['name']}")
else:
    created = api("POST", "/notification", conn)
    print(f"Created notification: {conn['name']} (id {created['id']})")
print("Radarr will now ping you when a movie imports to the NAS.")
