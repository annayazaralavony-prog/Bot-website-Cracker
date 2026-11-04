import os
import re
import sys
import glob
import time
import sqlite3
import asyncio
import aiohttp
import traceback
import importlib.util
import html as htmllib
from urllib.parse import urlparse
from datetime import datetime, timezone
from threading import Thread
from flask import Flask

# ================= Python 3.14 Event Loop Fix =================
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, idle, enums
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InputMediaVideo, InputMediaPhoto
)
from pyrogram.errors import FloodWait, RPCError

# Telethon (optional)
try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import FloodWaitError, RPCError as TelethonRPCError
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False
    TelegramClient = None
    StringSession = None

    class FloodWaitError(Exception):
        pass

    class TelethonRPCError(Exception):
        pass

# ================= কনফিগারেশন =================
API_ID = int(os.environ.get("API_ID", "123456"))
API_HASH = os.environ.get("API_HASH", "your_api_hash")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token")
STORAGE_CHANNEL_ID = int(os.environ.get("STORAGE_CHANNEL_ID", "-100123456789"))
TELETHON_SESSION_STRING = os.environ.get("TELETHON_SESSION_STRING", "").strip()

# ---- [NEW] একাধিক অ্যাকাউন্ট (Album-level Concurrency) ----
# অতিরিক্ত অ্যাকাউন্ট দিতে: TELETHON_SESSION_STRING_2, TELETHON_SESSION_STRING_3 ... (৯ পর্যন্ত)
# অথবা কমা দিয়ে আলাদা করে: TELETHON_SESSION_STRINGS="sess1,sess2,sess3"
def _collect_session_strings():
    found = []
    if TELETHON_SESSION_STRING:
        found.append(TELETHON_SESSION_STRING)
    for raw in os.environ.get("TELETHON_SESSION_STRINGS", "").split(","):
        raw = raw.strip()
        if raw:
            found.append(raw)
    for n in range(2, 10):
        raw = os.environ.get(f"TELETHON_SESSION_STRING_{n}", "").strip()
        if raw:
            found.append(raw)
    unique = []
    for s in found:
        if s not in unique:
            unique.append(s)
    return unique

# ---- [NEW] Pyrogram User Session Strings (আপলোডার অ্যাকাউন্ট) ----
# PYROGRAM_SESSION_STRING (অ্যাকাউন্ট ১), PYROGRAM_SESSION_STRING_2 ... _9
# অথবা কমা দিয়ে: PYROGRAM_SESSION_STRINGS="sess1,sess2,sess3"
def _collect_pyro_sessions():
    found = []
    first = os.environ.get("PYROGRAM_SESSION_STRING", "").strip()
    if first:
        found.append(first)
    for raw in os.environ.get("PYROGRAM_SESSION_STRINGS", "").split(","):
        raw = raw.strip()
        if raw:
            found.append(raw)
    for n in range(2, 10):
        raw = os.environ.get(f"PYROGRAM_SESSION_STRING_{n}", "").strip()
        if raw:
            found.append(raw)
    unique = []
    for x in found:
        if x not in unique:
            unique.append(x)
    return unique

# একটি অ্যালবামের ভেতরে একসাথে কয়টি ফাইল প্যারালাল ডাউনলোড হবে
DOWNLOAD_PARALLEL = max(1, int(os.environ.get("DOWNLOAD_PARALLEL", "5")))

SITES_DIR = "sites"  # গিটহাবে এই ফোল্ডারে সাইটের ফাইলগুলো রাখবেন (অথবা রুট ফোল্ডারে)
DB_PATH = "scraped_data.db"
BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID") or STORAGE_CHANNEL_ID)
BACKUP_TAG = "#DB_BACKUP"
SITE_PREFIX = "🌐 "

MAX_ALBUM_BATCH = 10
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_MB", "2000")) * 1024 * 1024
MAX_BATCH_BYTES = int(os.environ.get("MAX_BATCH_MB", "1200")) * 1024 * 1024

bot = Client("html_scraper_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Pyrogram User Session ক্লায়েন্টগুলো (আপলোডার অ্যাকাউন্ট)
pyro_clients = []
for _i, _sess in enumerate(_collect_pyro_sessions(), start=1):
    pyro_clients.append(Client(
        f"uploader_acc_{_i}", api_id=API_ID, api_hash=API_HASH,
        session_string=_sess, in_memory=True
    ))
pyro_ready = []  # সফলভাবে স্টার্ট হওয়া ক্লায়েন্ট: [(নাম, client)]

# প্রথম ক্লায়েন্ট (telethon_client) আগের মতোই ব্যাকআপ রিস্টোর/চ্যানেল সিঙ্কে ব্যবহার হয়
telethon_clients = []
if TELETHON_AVAILABLE:
    for _sess in _collect_session_strings():
        telethon_clients.append(TelegramClient(StringSession(_sess), API_ID, API_HASH))
telethon_client = telethon_clients[0] if telethon_clients else None

# ================= ডাটাবেস =================
conn = None
cursor = None

def init_db():
    global conn, cursor
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS uploaded_media (
            media_url TEXT PRIMARY KEY,
            hashtag TEXT
        )
    """)
    conn.commit()

def db_is_empty():
    cursor.execute("SELECT COUNT(*) FROM uploaded_media")
    (media_n,) = cursor.fetchone()
    return media_n == 0

init_db()

# ================= টেলিগ্রাম ব্যাকআপ সিস্টেম =================
backup_event = None
backup_dirty = False
last_backup_msg_id = None

def schedule_backup():
    global backup_dirty
    backup_dirty = True
    if backup_event is not None:
        backup_event.set()

def _make_snapshot():
    snap = "scraped_data_snapshot.db"
    if os.path.exists(snap):
        os.remove(snap)
    dst = sqlite3.connect(snap)
    conn.backup(dst)
    dst.close()
    return snap

async def do_backup():
    global backup_dirty, last_backup_msg_id
    snap = None
    backup_dirty = False
    try:
        snap = _make_snapshot()
        size_mb = os.path.getsize(snap) / (1024 * 1024)
        if size_mb > 45:
            backup_dirty = True
            return False, f"Database too large ({size_mb:.1f} MB)"

        cursor.execute("SELECT COUNT(*) FROM uploaded_media")
        (media_n,) = cursor.fetchone()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        msg = await bot.send_document(
            BACKUP_CHANNEL_ID,
            snap,
            file_name="scraped_data_backup.db",
            caption=f"{BACKUP_TAG}\n🕒 {stamp}\n🎬 Media records: {media_n}"
        )
        try:
            await bot.pin_chat_message(BACKUP_CHANNEL_ID, msg.id, disable_notification=True)
        except Exception as pin_err:
            print(f"⚠️ Backup pin failed: {pin_err}")

        old_id = last_backup_msg_id
        last_backup_msg_id = msg.id
        if old_id and old_id != msg.id:
            try:
                await bot.delete_messages(BACKUP_CHANNEL_ID, old_id)
            except Exception:
                pass
        return True, stamp
    except Exception as e:
        backup_dirty = True
        return False, f"{type(e).__name__}: {str(e)}"
    finally:
        if snap and os.path.exists(snap):
            os.remove(snap)

async def backup_worker():
    while True:
        await backup_event.wait()
        await asyncio.sleep(10)
        backup_event.clear()
        if backup_dirty:
            ok, info = await do_backup()
            if not ok:
                print(f"⚠️ Auto backup failed: {info}")
                await asyncio.sleep(60)
                backup_event.set()
        await asyncio.sleep(20)

def _valid_backup_file(path):
    try:
        c = sqlite3.connect(path)
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        c.close()
        return "uploaded_media" in names
    except Exception:
        return False

async def restore_db_from_telegram():
    global last_backup_msg_id
    if not db_is_empty():
        return "Local database already has data - restore skipped."

    path = None
    msg_id = None

    try:
        chat = await bot.get_chat(BACKUP_CHANNEL_ID)
        pm = chat.pinned_message
        if pm and pm.document and (pm.caption or "").startswith(BACKUP_TAG):
            path = await bot.download_media(pm, file_name="./restore_tmp.db")
            msg_id = pm.id
    except Exception as e:
        print(f"⚠️ Pinned backup lookup failed: {e}")

    if not path:
        t_ok, _ = await ensure_telethon_connected()
        if t_ok:
            try:
                async for m in telethon_client.iter_messages(BACKUP_CHANNEL_ID, search=BACKUP_TAG, limit=20):
                    if m.document:
                        path = await telethon_client.download_media(m, "restore_tmp.db")
                        msg_id = m.id
                        break
            except Exception as e:
                print(f"⚠️ Telethon backup search failed: {e}")

    if not path or not os.path.exists(path):
        return "No backup found in Telegram (fresh start)."
    if not _valid_backup_file(path):
        os.remove(path)
        return "Backup file invalid - ignored."

    conn.close()
    os.replace(path, DB_PATH)
    init_db()
    last_backup_msg_id = msg_id
    cursor.execute("SELECT COUNT(*) FROM uploaded_media")
    (media_n,) = cursor.fetchone()
    return f"Restored from Telegram backup: {media_n} media records."

def is_duplicate(url):
    cursor.execute("SELECT 1 FROM uploaded_media WHERE media_url = ?", (url,))
    return cursor.fetchone() is not None

def mark_as_uploaded(url, hashtag):
    cursor.execute("INSERT OR IGNORE INTO uploaded_media (media_url, hashtag) VALUES (?, ?)", (url, hashtag))
    conn.commit()
    schedule_backup()

# ================= ডাইনামিক সাইট ফাইল লোডার =================
loaded_sites = {}  # key -> {"key", "name", "hashtag", "module", "path"}

def discover_sites():
    """গিটহাব রিপোজিটরির sites/ ফোল্ডার বা রুট ফোল্ডার থেকে সব ওয়েবসাইট স্ক্রিপ্ট লোড করে"""
    global loaded_sites
    loaded_sites = {}
    os.makedirs(SITES_DIR, exist_ok=True)

    candidates = sorted(glob.glob(os.path.join(SITES_DIR, "*.py")))
    # রুট ফোল্ডারেও যদি কোনো সাইট ফাইল থাকে (bot.py/app.py বাদে) সেটাও চেক করবে
    for f in sorted(glob.glob("*.py")):
        if os.path.basename(f).lower() not in ("bot.py", "app.py", "main.py"):
            candidates.append(f)

    for path in candidates:
        base = os.path.basename(path)
        if base.startswith("_"):
            continue
        key = os.path.splitext(base)[0]
        try:
            spec = importlib.util.spec_from_file_location(f"site_mod_{key}_{int(time.time()*1000)}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # ফাইলটিতে extract_albums বা get_albums বা HTML_CONTENT থাকতে হবে
            if not (hasattr(mod, "extract_albums") or hasattr(mod, "get_albums") or hasattr(mod, "HTML_CONTENT")):
                continue

            name = str(getattr(mod, "SITE_NAME", key)).strip()[:30]
            hashtag = str(getattr(mod, "HASHTAG", "#rells")).strip()
            if not hashtag.startswith("#"):
                hashtag = f"#{hashtag}"

            loaded_sites[key] = {
                "key": key,
                "name": name,
                "hashtag": hashtag,
                "module": mod,
                "path": path
            }
        except Exception as e:
            print(f"⚠️ Failed to load site file '{path}': {e}")

    return loaded_sites

def find_site_by_label(label):
    name = label[len(SITE_PREFIX):].strip() if label.startswith(SITE_PREFIX) else label.strip()
    for site in loaded_sites.values():
        if site["name"] == name or site["key"] == name:
            return site
    return None

def get_site_albums(site, override_html=None):
    """নির্দিষ্ট ওয়েবসাইটের ফাইল থেকে অ্যালবাম ও ভিডিও লিস্ট বের করে আনে"""
    mod = site["module"]
    html_content = override_html if override_html is not None else getattr(mod, "HTML_CONTENT", "")

    if hasattr(mod, "get_albums"):
        res = mod.get_albums()
    elif hasattr(mod, "extract_albums"):
        res = mod.extract_albums(html_content)
    else:
        raise RuntimeError(f"'{site['path']}' ফাইলে extract_albums(html) ফাংশন নেই।")

    if isinstance(res, tuple) and len(res) == 2:
        page_title, albums = res
    else:
        page_title, albums = site["name"], res

    # ডুপ্লিকেট লিংক পরিষ্কার করা
    clean_albums = []
    for idx, a in enumerate(albums or [], start=1):
        seen = set()
        items = []
        for kind, url in a.get("items", []):
            if not url or url in seen:
                continue
            seen.add(url)
            items.append((kind, url))
        if items:
            clean_albums.append({
                "title": a.get("title") or f"Album {idx}",
                "items": items
            })
    return page_title or site["name"], clean_albums

discover_sites()

# ================= ইউজার সেশন ও বাটন =================
user_sessions = {}

def get_home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Reload Site Files", callback_data="action_reload_sites")],
        [InlineKeyboardButton("🔄 Sync Storage Channel (Telethon)", callback_data="action_sync_channel")]
    ])

def get_site_keyboard(site_key):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start Scan & Upload", callback_data=f"site_scan_{site_key}")],
        [InlineKeyboardButton("📄 Send New HTML for this Site", callback_data=f"site_html_{site_key}")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu_home")]
    ])

def build_sites_reply_keyboard():
    if not loaded_sites:
        return None
    rows = [[KeyboardButton(f"{SITE_PREFIX}{s['name']}")] for s in loaded_sites.values()]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, placeholder="Select a website module...")

async def refresh_menu(client, user_id):
    session = user_sessions.setdefault(user_id, {})
    kb = build_sites_reply_keyboard()
    old_id = session.get("kb_msg_id")
    try:
        if kb:
            msg = await client.send_message(user_id, f"📚 Loaded `{len(loaded_sites)}` website file(s) in menu.", reply_markup=kb)
        else:
            msg = await client.send_message(user_id, "📚 No site files found in `sites/` folder.", reply_markup=ReplyKeyboardRemove())
        session["kb_msg_id"] = msg.id
        if old_id:
            try:
                await client.delete_messages(user_id, old_id)
            except Exception:
                pass
    except Exception:
        pass

async def update_panel(client, user_id, text, reply_markup=None):
    session = user_sessions.get(user_id, {})
    msg_id = session.get("panel_msg_id")
    try:
        if msg_id:
            await client.edit_message_text(
                chat_id=user_id, message_id=msg_id, text=text,
                reply_markup=reply_markup, disable_web_page_preview=True
            )
        else:
            msg = await client.send_message(
                chat_id=user_id, text=text,
                reply_markup=reply_markup, disable_web_page_preview=True
            )
            user_sessions.setdefault(user_id, {})["panel_msg_id"] = msg.id
    except RPCError as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            msg = await client.send_message(
                chat_id=user_id, text=text,
                reply_markup=reply_markup, disable_web_page_preview=True
            )
            user_sessions.setdefault(user_id, {})["panel_msg_id"] = msg.id

async def reset_panel(client, user_id):
    session = user_sessions.setdefault(user_id, {})
    old_id = session.get("panel_msg_id")
    if old_id:
        try:
            await client.delete_messages(user_id, old_id)
        except Exception:
            pass
    session["panel_msg_id"] = None

async def ensure_telethon_connected(tclient=None):
    """tclient না দিলে প্রথম Telethon ক্লায়েন্ট চেক করবে (আগের মতোই)"""
    if not TELETHON_AVAILABLE:
        return False, "Telethon not installed."
    target = tclient if tclient is not None else telethon_client
    if not target:
        return False, "TELETHON_SESSION_STRING missing."
    try:
        if not target.is_connected():
            await target.connect()
        if not await target.is_user_authorized():
            return False, "Telethon Session String invalid/expired."
        return True, "Connected"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)}"

# ================= সাইট প্যানেল ডিসপ্লে =================
def safe(text):
    return str(text).replace("`", "'")

def album_totals(albums):
    v = sum(1 for a in albums for k, _ in a["items"] if k == "video")
    p = sum(1 for a in albums for k, _ in a["items"] if k == "image")
    return v, p

def describe_albums(albums, limit=10):
    lines = []
    for i, a in enumerate(albums[:limit], start=1):
        v = sum(1 for k, _ in a["items"] if k == "video")
        p = len(a["items"]) - v
        lines.append(f"{i}. {safe(a['title'])[:32]} - {v}V {p}P")
    if len(albums) > limit:
        lines.append(f"...and {len(albums) - limit} more albums")
    return "\n".join(lines)

def albums_panel_text(albums):
    v_total, p_total = album_totals(albums)
    text = (
        f"📁 **Albums/Sections:** `{len(albums)}`\n"
        f"🎬 **Videos:** `{v_total}` | 🖼️ **Photos:** `{p_total}`\n"
    )
    if albums:
        text += f"\n```text\n{describe_albums(albums)}\n```\n"
    return text

async def show_site_panel(client, user_id, site, override_html=None):
    discover_sites()
    site = loaded_sites.get(site["key"], site)
    try:
        _title, albums = get_site_albums(site, override_html)
        err_txt = ""
    except Exception as e:
        albums = []
        err_txt = f"\n⚠️ **Parser Error:** `{safe(e)}`\n"

    sess = user_sessions.setdefault(user_id, {})
    sess["state"] = "IDLE"
    sess["active_site_key"] = site["key"]
    if override_html is not None:
        sess.setdefault("custom_html", {})[site["key"]] = override_html

    has_html = override_html is not None or bool(getattr(site["module"], "HTML_CONTENT", "").strip())
    source_note = "📄 **HTML Source:** `Ready in file`" if has_html else "⚠️ **HTML Source:** `Empty (Send .html file or paste code)`"

    await reset_panel(client, user_id)
    await update_panel(
        client, user_id,
        f"🌐 **{safe(site['name'])}**\n"
        f"📂 **File:** `{site['path']}`\n"
        f"🏷️ **Hashtag:** `{site['hashtag']}`\n"
        f"{source_note}\n"
        f"{albums_panel_text(albums)}"
        f"{err_txt}\n"
        f"স্ক্যান শুরু করতে বা নতুন HTML ফাইল দিতে নিচের বাটনে ক্লিক করুন:",
        get_site_keyboard(site["key"])
    )

# ================= কমান্ড ও কলব্যাক হ্যান্ডলার =================
@bot.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    user_id = message.from_user.id
    try:
        await message.delete()
    except Exception:
        pass

    discover_sites()
    old = user_sessions.get(user_id, {})
    user_sessions[user_id] = {
        "state": "IDLE",
        "panel_msg_id": old.get("panel_msg_id"),
        "kb_msg_id": old.get("kb_msg_id"),
        "custom_html": old.get("custom_html", {})
    }

    t_ok, t_status = await ensure_telethon_connected()
    engine_status = "🟢 Active (Telethon Session)" if t_ok else f"🔴 Inactive ({t_status})"
    if t_ok and len(telethon_clients) > 1:
        engine_status = f"🟢 Active (Telethon x{len(telethon_clients)} Accounts)"
    sites_list = "\n".join(f"• `{s['name']}` (`{s['path']}`)" for s in loaded_sites.values()) or "• `No site files found in sites/ folder`"

    welcome_text = (
        "🤖 **Modular Website Scraper Dashboard**\n\n"
        f"⚡ **Telethon Engine:** `{engine_status}`\n"
        f"📦 **Storage Channel:** `{STORAGE_CHANNEL_ID}`\n\n"
        f"📂 **Loaded Website Modules ({len(loaded_sites)}):**\n{sites_list}\n\n"
        "নিচের মেনু কিবোর্ড থেকে যেকোনো ওয়েবসাইট সিলেক্ট করে স্ক্যান শুরু করুন:"
    )
    await reset_panel(client, user_id)
    await refresh_menu(client, user_id)
    await update_panel(client, user_id, welcome_text, get_home_keyboard())

@bot.on_message(filters.command("backup") & filters.private)
async def backup_cmd(client, message):
    user_id = message.from_user.id
    try:
        await message.delete()
    except Exception:
        pass
    await reset_panel(client, user_id)
    await update_panel(client, user_id, "💾 **Creating database backup in Telegram...**")
    ok, info = await do_backup()
    text = f"✅ **BACKUP COMPLETED**\n• **Time:** `{info}`" if ok else f"❌ **BACKUP FAILED**\n• **Reason:** `{info}`"
    await update_panel(client, user_id, text, get_home_keyboard())

@bot.on_callback_query()
async def handle_callbacks(client, cq):
    user_id = cq.from_user.id
    data = cq.data
    sess = user_sessions.setdefault(user_id, {"state": "IDLE", "custom_html": {}})
    sess["panel_msg_id"] = cq.message.id

    try:
        if data in ("menu_home", "action_reload_sites"):
            discover_sites()
            sess["state"] = "IDLE"
            if data == "action_reload_sites":
                await refresh_menu(client, user_id)
                await cq.answer(f"Reloaded {len(loaded_sites)} site files!")

            t_ok, t_status = await ensure_telethon_connected()
            engine_status = "🟢 Active (Telethon Session)" if t_ok else f"🔴 Inactive ({t_status})"
            if t_ok and len(telethon_clients) > 1:
                engine_status = f"🟢 Active (Telethon x{len(telethon_clients)} Accounts)"
            sites_list = "\n".join(f"• `{s['name']}` (`{s['path']}`)" for s in loaded_sites.values()) or "• `No site files found in sites/ folder`"
            await update_panel(
                client, user_id,
                f"🤖 **Modular Website Scraper Dashboard**\n\n"
                f"⚡ **Telethon Engine:** `{engine_status}`\n\n"
                f"📂 **Loaded Website Modules ({len(loaded_sites)}):**\n{sites_list}",
                get_home_keyboard()
            )

        elif data == "action_sync_channel":
            await cq.answer("Syncing storage channel via Telethon...")
            t_ok, t_err = await ensure_telethon_connected()
            if not t_ok:
                await update_panel(client, user_id, f"❌ **TELETHON CONNECTION FAILED**\n\n`{t_err}`", get_home_keyboard())
                return

            await update_panel(client, user_id, "🔄 **Scanning Storage Channel via Telethon...**")
            count = 0
            async for msg in telethon_client.iter_messages(STORAGE_CHANNEL_ID, limit=500):
                if msg.text:
                    for u in re.findall(r'https?://[^\s"\'<>]+', msg.text):
                        mark_as_uploaded(u, "#synced")
                count += 1

            await update_panel(
                client, user_id,
                f"✅ **CHANNEL SYNC COMPLETED**\n\n• **Scanned Messages:** `{count} recent posts`",
                get_home_keyboard()
            )

        elif data.startswith("site_html_"):
            site_key = data[len("site_html_"):]
            site = loaded_sites.get(site_key)
            if not site:
                await cq.answer("⚠️ Site file not found!", show_alert=True)
                return
            sess["state"] = "WAITING_SITE_HTML"
            sess["active_site_key"] = site_key
            await update_panel(
                client, user_id,
                f"📄 **Send HTML for `{safe(site['name'])}`**\n\n"
                f"এখন এই সাইটের `.html` ফাইল আপলোড করুন অথবা কোড পেস্ট করুন।\n"
                f"*(বট `{site['path']}` ফাইলের নিয়ম অনুযায়ী ভিডিও ও অ্যালবাম খুঁজে বের করবে)*",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"site_back_{site_key}")]])
            )

        elif data.startswith("site_back_"):
            site_key = data[len("site_back_"):]
            site = loaded_sites.get(site_key)
            if site:
                override = sess.get("custom_html", {}).get(site_key)
                await show_site_panel(client, user_id, site, override)

        elif data.startswith("site_scan_"):
            site_key = data[len("site_scan_"):]
            discover_sites()
            site = loaded_sites.get(site_key)
            if not site:
                await cq.answer("⚠️ Site file not found!", show_alert=True)
                return
            override = sess.get("custom_html", {}).get(site_key)
            started = await start_scan(client, user_id, site, override)
            if not started:
                await cq.answer("⚠️ A scan is already running!", show_alert=True)
                return
            await cq.answer("Scan started!")
            return

        elif data == "scan_cancel":
            st = active_scans.get(user_id)
            if st and not st["task"].done():
                st["cancel"] = True
                await cq.answer("Stopping scan...")
            else:
                await cq.answer("No active scan.", show_alert=True)
            return

        await cq.answer()
    except Exception as e:
        err_trace = traceback.format_exc()
        await update_panel(
            client, user_id,
            f"❌ **CALLBACK ERROR**\n`{type(e).__name__}: {str(e)}`\n```python\n{err_trace[-900:]}\n```",
            get_home_keyboard()
        )

# ================= টেক্সট ও ফাইল ইনপুট =================
@bot.on_message(filters.text & filters.private)
async def handle_text_input(client, message):
    user_id = message.from_user.id
    text = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    sess = user_sessions.setdefault(user_id, {"state": "IDLE", "custom_html": {}})

    if text.startswith(SITE_PREFIX):
        discover_sites()
        site = find_site_by_label(text)
        if site:
            override = sess.get("custom_html", {}).get(site["key"])
            await show_site_panel(client, user_id, site, override)
            return

    if sess.get("state") == "WAITING_SITE_HTML":
        site_key = sess.get("active_site_key")
        site = loaded_sites.get(site_key)
        if site:
            await show_site_panel(client, user_id, site, override_html=message.text)

@bot.on_message(filters.document & filters.private)
async def handle_document_input(client, message):
    user_id = message.from_user.id
    sess = user_sessions.setdefault(user_id, {"state": "IDLE", "custom_html": {}})
    site_key = sess.get("active_site_key")
    site = loaded_sites.get(site_key) if site_key else None

    if not site and len(loaded_sites) == 1:
        site = next(iter(loaded_sites.values()))

    if not site:
        try:
            await message.delete()
        except Exception:
            pass
        await update_panel(client, user_id, "⚠️ আগে নিচের মেনু থেকে একটি ওয়েবসাইট বাটন সিলেক্ট করুন, তারপর HTML ফাইল দিন।", get_home_keyboard())
        return

    file_path = None
    try:
        file_path = await message.download()
        try:
            await message.delete()
        except Exception:
            pass
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            html_content = f.read()
        if os.path.exists(file_path):
            os.remove(file_path)
        await show_site_panel(client, user_id, site, override_html=html_content)
    except Exception as e:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        await update_panel(client, user_id, f"❌ **File Read Error:** `{e}`", get_home_keyboard())

# ================= ডাউনলোড ও আপলোড ইঞ্জিন =================
class ScanCancelled(BaseException):
    pass

active_scans = {}

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancel Scan", callback_data="scan_cancel")]])

def check_cancel(state):
    if state and state.get("cancel"):
        raise ScanCancelled()

async def cancellable_sleep(seconds, state):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_cancel(state)
        await asyncio.sleep(min(1, max(0, end - time.monotonic())))

async def run_cancellable(coro, state):
    task = asyncio.ensure_future(coro)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=1)
            if state.get("cancel") and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                raise ScanCancelled()
        return task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise

def url_ext(url, kind):
    path = urlparse(url).path.lower()
    for e in (".mp4", ".jpg", ".jpeg", ".png", ".webp"):
        if path.endswith(e):
            return e
    return ".mp4" if kind == "video" else ".jpg"

def build_request_headers(url, site_mod):
    """ওয়েবসাইটের ফাইলে কাস্টম হেডার বা রেফারার থাকলে সেটি ব্যবহার করবে"""
    parsed = urlparse(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": getattr(site_mod, "REFERER", f"{parsed.scheme}://{parsed.netloc}/")
    }
    if hasattr(site_mod, "HEADERS") and isinstance(site_mod.HEADERS, dict):
        headers.update(site_mod.HEADERS)
    if hasattr(site_mod, "get_headers"):
        custom = site_mod.get_headers(url)
        if isinstance(custom, dict):
            headers.update(custom)
    return headers

async def download_file_with_details(url, dest_path, site_mod, state=None, progress_cb=None, max_bytes=None):
    # সাইট ফাইলে যদি নিজস্ব download_file ফাংশন থাকে তবে সেটি কল হবে
    if hasattr(site_mod, "custom_download"):
        return await site_mod.custom_download(url, dest_path, state, progress_cb, max_bytes)

    headers = build_request_headers(url, site_mod)
    last_err = "Unknown error"
    for attempt in range(1, 4):
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return False, f"HTTP Error {resp.status} ({resp.reason}) - Server blocked or file removed."

                    total = resp.content_length or 0
                    if max_bytes and total and total > max_bytes:
                        return False, f"File too large ({total // (1024 * 1024)} MB)."

                    downloaded = 0
                    last_cb = time.monotonic()
                    with open(dest_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            if state and state.get("cancel"):
                                raise ScanCancelled()
                            f.write(chunk)
                            downloaded += len(chunk)
                            if max_bytes and downloaded > max_bytes:
                                return False, "File exceeds size limit."
                            if progress_cb and time.monotonic() - last_cb >= 4:
                                last_cb = time.monotonic()
                                try:
                                    await progress_cb(downloaded, total)
                                except Exception:
                                    pass

            if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
                return False, "Downloaded file is 0 bytes."
            return True, "OK"

        except ScanCancelled:
            raise
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)}"

        if os.path.exists(dest_path):
            os.remove(dest_path)
        await cancellable_sleep(3 * attempt, state)

    return False, f"{last_err} (failed after 3 attempts)"

def build_caption(title, a_idx, total_albums, part, multi_part, hashtag):
    cap = f"<b>{htmllib.escape(title[:200])}</b>\n📁 Album {a_idx}/{total_albums}"
    if multi_part:
        cap += f" • Part {part}"
    cap += f"\n{htmllib.escape(hashtag)}"
    return cap

# ---- [CHANGED] uploader = {"type": "telethon"/"pyrogram", "client": ..., "name": ...}
# প্রতিটি অ্যালবাম একটিমাত্র uploader (অ্যাকাউন্ট) দিয়েই সম্পূর্ণ আপলোড হবে ----
async def send_album(files, caption, uploader=None):
    tclient = None
    if uploader and uploader.get("type") == "telethon":
        t_ok, _ = await ensure_telethon_connected(uploader["client"])
        if t_ok:
            tclient = uploader["client"]
    elif uploader is None:
        t_ok, _ = await ensure_telethon_connected()
        if t_ok:
            tclient = telethon_client
    paths = [f["path"] for f in files]
    pc = uploader["client"] if (uploader and uploader.get("type") == "pyrogram") else bot

    if tclient is not None:
        if len(paths) == 1:
            await tclient.send_file(
                STORAGE_CHANNEL_ID, paths[0],
                caption=caption or None, parse_mode="html",
                supports_streaming=(files[0]["kind"] == "video")
            )
        else:
            caps = [caption] + [""] * (len(paths) - 1)
            await tclient.send_file(
                STORAGE_CHANNEL_ID, paths,
                caption=caps, parse_mode="html",
                supports_streaming=True
            )
    else:
        if len(files) == 1:
            f = files[0]
            if f["kind"] == "video":
                await pc.send_video(
                    STORAGE_CHANNEL_ID, video=f["path"], caption=caption,
                    parse_mode=enums.ParseMode.HTML, supports_streaming=True
                )
            else:
                await pc.send_photo(
                    STORAGE_CHANNEL_ID, photo=f["path"], caption=caption,
                    parse_mode=enums.ParseMode.HTML
                )
        else:
            media = []
            for i, f in enumerate(files):
                cap = caption if i == 0 else ""
                if f["kind"] == "video":
                    media.append(InputMediaVideo(f["path"], caption=cap, parse_mode=enums.ParseMode.HTML, supports_streaming=True))
                else:
                    media.append(InputMediaPhoto(f["path"], caption=cap, parse_mode=enums.ParseMode.HTML))
            await pc.send_media_group(STORAGE_CHANNEL_ID, media)

async def send_with_flood_retry(files, caption, state, uploader=None):
    for _ in range(3):
        try:
            await run_cancellable(send_album(files, caption, uploader), state)
            return
        except (FloodWait, FloodWaitError) as fw:
            wait_sec = int(getattr(fw, "value", getattr(fw, "seconds", 10)) or 10)
            await cancellable_sleep(min(wait_sec + 1, 600), state)
    raise RuntimeError("Too many FloodWait retries")

async def send_batch(files, caption, state, uploader=None):
    try:
        await send_with_flood_retry(files, caption, state, uploader)
        return list(files), []
    except ScanCancelled:
        raise
    except Exception as first_err:
        if len(files) == 1:
            return [], [(files[0]["url"], f"{type(first_err).__name__}: {first_err}")]
        # ফলব্যাক: একই uploader দিয়েই ফাইলগুলো সিরিয়াল অনুযায়ী একটা একটা করে পাঠানো হবে
        ok_files, failed = [], []
        captioned = False
        for f in files:
            try:
                await send_with_flood_retry([f], "" if captioned else caption, state, uploader)
                captioned = True
                ok_files.append(f)
            except ScanCancelled:
                raise
            except Exception as e:
                failed.append((f["url"], f"{type(e).__name__}: {e}"))
        return ok_files, failed

async def upload_album_batch(batch, title, a_idx, total_albums, part, multi_part, hashtag, state, uploader=None):
    caption = build_caption(title, a_idx, total_albums, part, multi_part, hashtag)
    try:
        ok_files, failed = await send_batch(batch, caption, state, uploader)
    finally:
        for f in batch:
            if os.path.exists(f["path"]):
                os.remove(f["path"])
    for f in ok_files:
        mark_as_uploaded(f["url"], hashtag)
    return ok_files, failed

def tally(ok_files, failed, a_idx):
    v = sum(1 for f in ok_files if f["kind"] == "video")
    p = len(ok_files) - v
    errs = [f"• [Album {a_idx} Upload Failed]\n  URL: {u[:45]}...\n  Reason: {e}" for u, e in failed]
    return v, p, errs

async def send_channel_text(text_html):
    try:
        t_ok, _ = await ensure_telethon_connected()
        if t_ok:
            await telethon_client.send_message(STORAGE_CHANNEL_ID, text_html, parse_mode="html")
        else:
            await bot.send_message(STORAGE_CHANNEL_ID, text_html, parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        print(f"⚠️ Could not send album report: {e}")

async def start_scan(client, user_id, site, override_html=None):
    old = active_scans.get(user_id)
    if old and not old["task"].done():
        return False
    state = {"cancel": False}
    task = asyncio.create_task(process_and_upload(client, user_id, site, override_html, state))
    state["task"] = task
    active_scans[user_id] = state
    return True

async def build_uploaders():
    """প্রতিটি রেডি অ্যাকাউন্ট (Pyrogram User / Telethon) একটি করে worker। কিছু না থাকলে Pyrogram বট।"""
    uploaders = []
    for name, c in pyro_ready:
        uploaders.append({"type": "pyrogram", "client": c, "name": name})
    for i, c in enumerate(telethon_clients, start=1):
        ok, _ = await ensure_telethon_connected(c)
        if ok:
            uploaders.append({"type": "telethon", "client": c, "name": f"Telethon {i}"})
    if not uploaders:
        uploaders.append({"type": "pyrogram", "client": bot, "name": "Bot Token"})
    return uploaders

async def process_and_upload(client, user_id, site, override_html=None, state=None):
    if state is None:
        state = {"cancel": False}
    site_key = site["key"]
    site_mod = site["module"]
    hashtag = site["hashtag"]
    end_kb = get_site_keyboard(site_key)
    cancel_kb = get_cancel_keyboard()

    up_v = up_p = skipped = albums_done = 0
    errors = []
    cancelled = False
    fatal = False
    title = site["name"]
    total_albums = 0
    uploader_mode = "Pyrogram Bot Token"
    workers = {}  # wid -> {"name", "a_idx", "title", "action", "bytes"}
    last_panel = [0.0]

    def progress_text(header_action="", extra=""):
        lines = []
        for wid in sorted(workers):
            ws = workers[wid]
            if not ws.get("a_idx"):
                continue
            mb = sum(ws.get("bytes", {}).values()) // (1024 * 1024)
            mb_txt = f" | 📦 `{mb} MB`" if mb else ""
            lines.append(
                f"👤 `{ws['name']}` → 📁 `{ws['a_idx']}/{total_albums}` `{safe(ws['title'])[:28]}`\n"
                f"   {ws['action']}{mb_txt}"
            )
        body = "\n".join(lines) if lines else header_action
        return (
            f"⏳ **Scanning `{safe(site['name'])}`**\n\n"
            f"⚡ **Engine:** `{uploader_mode}`\n"
            f"{body}\n"
            f"{extra}"
            f"✅ **Uploaded:** `{up_v} videos, {up_p} photos` | ❌ **Failed:** `{len(errors)}` | 📁 **Albums Done:** `{albums_done}/{total_albums}`"
        )

    async def refresh(force=False):
        now = time.monotonic()
        if not force and now - last_panel[0] < 3:
            return
        last_panel[0] = now
        try:
            await update_panel(client, user_id, progress_text(), cancel_kb)
        except Exception:
            pass

    try:
        await update_panel(client, user_id, f"🔍 **Running parser `{site['path']}`...**", cancel_kb)
        title, albums = get_site_albums(site, override_html)
        total_albums = len(albums)

        if not albums:
            await update_panel(
                client, user_id,
                f"⚠️ **NO MEDIA FOUND FOR `{safe(site['name'])}`**\n\n"
                f"• **File:** `{site['path']}`\n"
                f"• `HTML_CONTENT` খালি থাকলে চ্যাটে `.html` ফাইল আপলোড করুন অথবা সাইট ফাইলের ভেতরে পেস্ট করুন।",
                end_kb
            )
            return

        uploaders = await build_uploaders()
        if uploaders[0]["name"] == "Bot Token":
            uploader_mode = "Pyrogram Bot Token"
        elif len(uploaders) == 1:
            uploader_mode = f"{uploaders[0]['name']} (User Session)"
        else:
            uploader_mode = f"{len(uploaders)} User Accounts (Album-level parallel)"

        # ---------- ধাপ ১: সব অ্যালবামের জব তৈরি (ডুপ্লিকেট ফিল্টার, সিরিয়াল ঠিক রেখে) ----------
        seen_run = set()
        queue = asyncio.Queue()
        for a_idx, album in enumerate(albums, start=1):
            all_items = album["items"]
            v_found = sum(1 for k, _ in all_items if k == "video")
            p_found = len(all_items) - v_found

            items = []
            prev_v = prev_p = 0
            for kind, url in all_items:
                if is_duplicate(url):
                    skipped += 1
                    if kind == "video":
                        prev_v += 1
                    else:
                        prev_p += 1
                    continue
                if url in seen_run:
                    continue
                seen_run.add(url)
                items.append((kind, url))

            if not items:
                continue
            queue.put_nowait({
                "a_idx": a_idx, "album": album, "items": items, "all_items": all_items,
                "v_found": v_found, "p_found": p_found, "prev_v": prev_v, "prev_p": prev_p
            })

        # ---------- ধাপ ২: একটি অ্যালবাম = একটি অ্যাকাউন্ট, সম্পূর্ণ সিরিয়ালি ----------
        async def process_album(job, uploader, wid):
            nonlocal up_v, up_p, albums_done
            a_idx = job["a_idx"]
            album = job["album"]
            items = job["items"]
            all_items = job["all_items"]
            ws = workers[wid]
            ws.update({"a_idx": a_idx, "title": album["title"], "action": "⏳ Starting...", "bytes": {}})

            a_up_v = a_up_p = a_fail = 0
            part = 0
            sem = asyncio.Semaphore(DOWNLOAD_PARALLEL)

            async def dl_one(item_idx, kind, url):
                label = "Video" if kind == "video" else "Photo"
                temp = f"tmp_{user_id}_{a_idx}_{item_idx}{url_ext(url, kind)}"

                async def progress(downloaded, total):
                    ws["bytes"][item_idx] = downloaded
                    await refresh()

                async with sem:
                    try:
                        check_cancel(state)
                        ok, err = await download_file_with_details(url, temp, site_mod, state, progress, MAX_FILE_BYTES)
                    except ScanCancelled:
                        return {"cancelled": True, "ok": False}
                ws["bytes"].pop(item_idx, None)
                if not ok:
                    if os.path.exists(temp):
                        os.remove(temp)
                    return {
                        "ok": False,
                        "err": f"• [Album {a_idx} {label} #{item_idx} Failed]\n  URL: {url[:45]}...\n  Reason: {err}"
                    }
                size = os.path.getsize(temp)
                return {"ok": True, "file": {"path": temp, "kind": kind, "url": url, "size": size}}

            try:
                for start in range(0, len(items), MAX_ALBUM_BATCH):
                    check_cancel(state)
                    chunk = items[start:start + MAX_ALBUM_BATCH]
                    ws["action"] = f"⬇️ Downloading `{start + 1}-{start + len(chunk)}/{len(items)}` (parallel x{min(DOWNLOAD_PARALLEL, len(chunk))})"
                    await refresh(force=True)

                    # অ্যালবামের ভেতরে প্যারালাল ডাউনলোড - gather রেজাল্ট ইনপুট সিরিয়ালেই ফেরত দেয়
                    results = await asyncio.gather(*[
                        dl_one(start + off + 1, kind, url)
                        for off, (kind, url) in enumerate(chunk)
                    ])
                    if any(r.get("cancelled") for r in results):
                        raise ScanCancelled()
                    check_cancel(state)

                    downloaded_files = []
                    for r in results:
                        if r["ok"]:
                            downloaded_files.append(r["file"])
                        else:
                            errors.append(r["err"])
                            a_fail += 1

                    # সাইজ লিমিট অনুযায়ী ভাগ (সিরিয়াল অপরিবর্তিত)
                    groups, cur, cur_bytes = [], [], 0
                    for f in downloaded_files:
                        if cur and cur_bytes + f["size"] > MAX_BATCH_BYTES:
                            groups.append(cur)
                            cur, cur_bytes = [], 0
                        cur.append(f)
                        cur_bytes += f["size"]
                    if cur:
                        groups.append(cur)

                    # আপলোড: একই অ্যাকাউন্টে, একটার পর একটা পার্ট
                    for g in groups:
                        check_cancel(state)
                        part += 1
                        mb = sum(f["size"] for f in g) // (1024 * 1024)
                        ws["action"] = f"⬆️ Uploading Part `{part}`: `{len(g)} files, {mb} MB`"
                        ws["bytes"] = {}
                        await refresh(force=True)
                        multi = len(items) > MAX_ALBUM_BATCH or part > 1
                        ok_files, failed = await upload_album_batch(
                            g, album["title"], a_idx, total_albums, part, multi, hashtag, state, uploader
                        )
                        v, p, errs = tally(ok_files, failed, a_idx)
                        a_up_v += v; a_up_p += p; up_v += v; up_p += p
                        a_fail += len(errs); errors.extend(errs)
                        await cancellable_sleep(1, state)
            finally:
                for fn in glob.glob(f"tmp_{user_id}_{a_idx}_*"):
                    try:
                        os.remove(fn)
                    except Exception:
                        pass

            report = (
                f"✅ <b>Album {a_idx}/{total_albums} completed</b>\n"
                f"📁 {htmllib.escape(album['title'][:200])}\n"
                f"🎬 Videos: {a_up_v + job['prev_v']}/{job['v_found']}\n"
                f"🖼️ Photos: {a_up_p + job['prev_p']}/{job['p_found']}\n"
                f"📦 Total files in album: {len(all_items)}"
            )
            if a_fail:
                report += f"\n⚠️ Failed: {a_fail}"
            await send_channel_text(report)
            albums_done += 1
            ws.update({"a_idx": 0, "action": "✅ Idle", "bytes": {}})
            await refresh(force=True)
            await cancellable_sleep(1, state)

        async def worker(wid, uploader):
            workers[wid] = {"name": uploader["name"], "a_idx": 0, "title": "", "action": "", "bytes": {}}
            while True:
                check_cancel(state)
                try:
                    job = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await process_album(job, uploader, wid)
                except ScanCancelled:
                    raise
                except Exception as album_err:
                    errors.append(
                        f"• [Album {job['a_idx']} Crashed]\n  Reason: {type(album_err).__name__}: {album_err}"
                    )
                    workers[wid].update({"a_idx": 0, "bytes": {}})

        tasks = [asyncio.create_task(worker(i, u)) for i, u in enumerate(uploaders)]
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            raise
        for out in outcomes:
            if isinstance(out, ScanCancelled):
                cancelled = True
            elif isinstance(out, BaseException) and not isinstance(out, asyncio.CancelledError):
                raise out
        if state.get("cancel"):
            cancelled = True

    except ScanCancelled:
        cancelled = True
    except Exception as fatal_e:
        fatal = True
        err_trace = traceback.format_exc()
        await update_panel(
            client, user_id,
            f"🚨 **CRITICAL BOT FAILURE**\n`{type(fatal_e).__name__}: {safe(fatal_e)}`\n```python\n{err_trace[-1000:]}\n```",
            end_kb
        )
    finally:
        for fn in glob.glob(f"tmp_{user_id}_*"):
            try:
                os.remove(fn)
            except Exception:
                pass
        if active_scans.get(user_id) is state:
            active_scans.pop(user_id, None)

    if fatal:
        return

    head = "⛔ **SCAN STOPPED**" if cancelled else "🏁 **SCAN COMPLETED**"
    summary_text = (
        f"{head}\n\n"
        f"⚡ **Engine Used:** `{uploader_mode}`\n"
        f"🌐 **Website:** `{safe(site['name'])}`\n"
        f"🏷️ **Section Tag:** `{hashtag}`\n"
        f"📁 **Albums Completed:** `{albums_done}/{total_albums}`\n"
        f"🎬 **Videos Uploaded:** `{up_v}`\n"
        f"🖼️ **Photos Uploaded:** `{up_p}`\n"
        f"⏭️ **Skipped (duplicate):** `{skipped}`\n"
        f"❌ **Total Failed:** `{len(errors)}`\n"
    )
    if errors:
        joined_errors = "\n".join(errors[:8])
        if len(errors) > 8:
            joined_errors += f"\n...and {len(errors) - 8} more errors."
        summary_text += f"\n\n⚠️ **DETAILED ERROR LOG:**\n```text\n{joined_errors}\n```"

    await update_panel(client, user_id, summary_text, end_kb)

# ================= Render Web Service Port Binding =================
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return f"Modular Scraper Bot is Running! Loaded sites: {len(loaded_sites)}"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

async def main():
    global backup_event
    await bot.start()
    try:
        status = await restore_db_from_telegram()
    except Exception as e:
        status = f"Restore error: {e}"
    print(f"💾 {status}")

    # Pyrogram User Session আপলোডার অ্যাকাউন্ট স্টার্ট
    for i, pc in enumerate(pyro_clients, start=1):
        try:
            await pc.start()
            me = await pc.get_me()
            try:
                await pc.get_chat(STORAGE_CHANNEL_ID)
            except Exception:
                # in_memory সেশনে চ্যানেল পিয়ার ক্যাশ করতে ডায়ালগ লোড করা হয়
                async for _d in pc.get_dialogs():
                    pass
                await pc.get_chat(STORAGE_CHANNEL_ID)
            pyro_ready.append((f"Account {i}", pc))
            print(f"👤 Uploader Account {i} ready: {me.first_name} ({me.id})")
        except Exception as e:
            print(f"⚠️ Uploader Account {i} failed to start: {type(e).__name__}: {e}")

    backup_event = asyncio.Event()
    worker = asyncio.create_task(backup_worker())
    print(f"🤖 Bot is running. Loaded {len(loaded_sites)} site modules. Pyrogram uploader accounts: {len(pyro_ready)} | Telethon accounts: {len(telethon_clients)}")

    await idle()

    worker.cancel()
    if backup_dirty:
        await do_backup()
    for _n, pc in pyro_ready:
        try:
            await pc.stop()
        except Exception:
            pass
    await bot.stop()

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    asyncio.get_event_loop().run_until_complete(main())
