# Telegram Bot Setup Guide

## Step 1: Create your bot

1. Open Telegram and search for **@BotFather**
2. Send: `/newbots`
3. Create your bot
4. Click **API Token**
5. Copy the new token
6. Update `config.json` with the new token

## Step 2: Get Your Chat ID

1. **Start a chat with your bot on Telegram**
   - Search for your bot name in Telegram
   - Click "Start" or send any message (e.g., "hello")

2. **Run the helper script**
   ```bash
   python get_telegram_chat_id.py
   ```

3. **Copy the chat_id** to your `config.json`:
   ```json
   "telegram": {
       "enabled": true,
       "bot_token": "YOUR_NEW_TOKEN_HERE",
       "chat_id": "YOUR_CHAT_ID_HERE",
       "update_interval_seconds": 900
   }
   ```

## Step 3: Test It

Start the bot and you should receive updates every 15 minutes (900 seconds).

To test immediately, you can temporarily change `update_interval_seconds` to `60` (1 minute).

## What You'll Receive

Every 15 minutes, you'll get a message like:

```
🤖 Market Maker Update - xyz:XYZ100
⏰ 2025-12-21 08:45:00 UTC

📊 Position
  Direction: 🟢 LONG
  Size: 0.5000
  Entry: $25,400.00
  Value: $12,700.00
  Leverage: 2x
  Unrealized P&L: $45.00
  Local Inventory: 0.5000

📝 Open Orders (2)
  🟢 BUY: 1 order(s)
    • 0.2500 @ $25,370.00
  🔴 SELL: 1 order(s)
    • 0.2500 @ $25,430.00

💰 Stats
  Realized P&L: $120.50
  Mark Price: $25,415.00
  Max Inventory: 1.50
```

## Troubleshooting

**"Telegram enabled but chat_id not configured"**
- You forgot to add your chat_id to config.json
- Run `python get_telegram_chat_id.py` to get it

**"No messages found"**
- Make sure you sent a message to your bot first
- The bot can't send you messages until you initiate contact

**"Telegram send failed: 401"**
- Your bot token is invalid
- Make sure you copied the new token correctly after revoking the old one

**"Telegram send failed: 403 Forbidden"**
- You haven't started a chat with your bot yet
- Open Telegram, search for your bot, and click "Start"
