# features.py
import os
import aiohttp
from pyrogram import filters

_menu_done = False

def _webapp_url():
    url = os.environ.get("WEBAPP_URL", "").strip()
    if not url:
        dom = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if dom:
            url = f"https://{dom}"
    return url

async def _set_redreels_menu(token):
    url = _webapp_url()
    if not url:
        print("⚠️ RedReels: WEBAPP_URL বা RAILWAY_PUBLIC_DOMAIN সেট নেই")
        return False
    payload = {
        "menu_button": {
            "type": "web_app",
            "text": "RedReels",
            "web_app": {"url": url},
        }
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"https://api.telegram.org/bot{token}/setChatMenuButton",
            json=payload,
        ) as r:
            data = await r.json()
    print(f"✅ RedReels menu button: {data.get('ok')}")
    return bool(data.get("ok"))

def setup(ns):
    bot = ns["bot"]
    token = ns["BOT_TOKEN"]

    # প্রথম প্রাইভেট মেসেজে (যেমন /start) একবার মেনু বাটন সেট হবে (Telegram সার্ভারে সেভ থাকে)
    @bot.on_message(filters.private, group=-1)
    async def _ensure_menu(client, message):
        global _menu_done
        if _menu_done:
            return
        _menu_done = True
        try:
            ok = await _set_redreels_menu(token)
            if not ok:
                _menu_done = False
        except Exception as e:
            _menu_done = False
            print(f"⚠️ RedReels menu error: {e}")

    print("✅ features.py loaded (RedReels menu)")
