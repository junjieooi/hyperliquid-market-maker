#!/usr/bin/env python3
"""
Helper script to get your Telegram chat ID.

Usage:
1. Send a message to your bot on Telegram (just say "hello")
2. Run this script: python get_telegram_chat_id.py
3. Copy the chat_id and add it to config.json
"""

import json
import sys

import requests

# Read bot token from config
try:
    with open("config.json", "r") as f:
        config = json.load(f)
    bot_token = config["telegram"]["bot_token"]
except (FileNotFoundError, KeyError):
    print("Error: Could not read bot token from config.json")
    print("Make sure config.json exists and has telegram.bot_token configured")
    sys.exit(1)

# Get updates from Telegram
url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
try:
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    data = response.json()
except requests.RequestException as e:
    print(f"Error fetching updates from Telegram: {e}")
    sys.exit(1)

# Parse chat IDs
if not data.get("ok"):
    print("Error: Telegram API returned an error")
    print(data)
    sys.exit(1)

updates = data.get("result", [])
if not updates:
    print("No messages found!")
    print("\nPlease:")
    print("1. Open Telegram")
    print("2. Search for your bot")
    print("3. Send it a message (e.g., 'hello')")
    print("4. Run this script again")
    sys.exit(0)

# Extract unique chat IDs
chat_ids = set()
for update in updates:
    if "message" in update:
        chat = update["message"].get("chat", {})
        chat_id = chat.get("id")
        username = chat.get("username", "Unknown")
        first_name = chat.get("first_name", "")

        if chat_id:
            chat_ids.add((chat_id, username, first_name))

print(f"\n✅ Found {len(chat_ids)} chat(s):\n")

for chat_id, username, first_name in chat_ids:
    print(f"Chat ID: {chat_id}")
    print(f"  Username: @{username}" if username else "  Username: (not set)")
    print(f"  Name: {first_name}")
    print()

if len(chat_ids) == 1:
    chat_id = list(chat_ids)[0][0]
    print(f"📝 Add this to your config.json:")
    print(f'   "chat_id": "{chat_id}",')
else:
    print("⚠️  Multiple chats found. Use the chat_id that matches your account.")
    print("📝 Add one of the above chat_ids to your config.json:")
    print('   "chat_id": "YOUR_CHAT_ID",')
