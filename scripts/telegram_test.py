"""Send a Telegram test message with the bot token in .env (TELEGRAM_KEY or TELEGRAM_BOT_TOKEN).
If TELEGRAM_OWNER_CHAT_ID is not set yet, it is found from the chat that sent /start to the bot and saved.
Never prints the token.

    .venv\\Scripts\\python.exe scripts\\telegram_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goat import dashboard  # noqa: E402

env = dashboard._read_env()
if not dashboard._telegram_token(env):
    sys.exit("no bot token in .env (TELEGRAM_KEY)")
if not dashboard._chat_id(env):
    found = dashboard.telegram_detect()
    if not found.get("ok"):
        sys.exit(f"chat id not found: {found.get('error')}")
    print(f"chat id found and saved for {found.get('name') or 'your chat'} via @{found.get('bot')}")
sent = dashboard.telegram_test()
print("test message sent: check Telegram" if sent.get("ok") else f"send failed: {sent.get('error')}")
sys.exit(0 if sent.get("ok") else 1)
