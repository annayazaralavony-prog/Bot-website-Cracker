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

# ---- একাধিক অ্যাকাউন্ট ----
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

DOWNLOAD_PARALLEL = max(1, int(os.environ.get("DOWNLOAD_PARALLEL", "5")))
SITES_DIR = "sites"
DB_PATH = "scraped_data.db"
BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID") or -1004448646419)
BACKUP_TAG = "#DB_BACKUP"
SITE_PREFIX = "🌐 "

MAX_ALBUM_BATCH = 10
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_MB", "2000")) * 1024 * 1024
MAX_BATCH_BYTES = int(os.environ.get("MAX_BATCH_MB", "2000")) * 1024 * 1024

# ========== নতুন সেটিংস ==========
MAX_ALBUMS_PER_SCAN = 20          # প্রতি স্ক্যানে সর্বোচ্চ কয়টা অ্যালবাম
DEFAULT_HASHTAG = "#newweb"       # ডিফল্ট হ্যাশট্যাগ
REST_CAPTION_HASHTAG = os.environ.get("REST_CAPTION_HASHTAG", "1") == "1"
ORDERED_ALBUMS = os.environ.get("ORDERED_ALBUMS", "1") == "1"

bot = Client("html_scraper_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

pyro_clients = []
for _i, _sess in enumerate(_collect_pyro_sessions(), start=1):
    pyro_clients.append(Client(
        f"uploader_acc_{_i}", api_id=API_ID, api_hash=API_HASH,
        session_string=_sess, in_memory=True
    ))
pyro_ready = []

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS site_html (
            site_key TEXT PRIMARY KEY,
            html TEXT
        )
    """)
    # নতুন টেবিল: কোন অ্যালবাম প্রসেস হয়েছে ট্র্যাক করার জন্য
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_albums (
            album_key TEXT PRIMARY KEY,
            site_key TEXT,
            title TEXT,
            processed_at TEXT
        )
    """)
    conn.commit()

def db_is_empty():
    cursor.execute("SELECT COUNT(*) FROM uploaded_media")
    (media_n,) = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) FROM site_html")
    (html_n,) = cursor.fetchone()
    return media_n == 0 and html_n == 0

def save_site_html(site_key, html_content):
    if not html_content or not html_content.strip():
        return
    cursor.execute("INSERT OR REPLACE INTO site_html (site_key, html) VALUES (?, ?)", (site_key, html_content))
    conn.commit()
    schedule_backup()

def db_get_site_html(site_key):
    try:
        cursor.execute("SELECT html FROM site_html WHERE site_key = ?", (site_key,))
        row = cursor.fetchone()
        return row[0] if row else None
    except Exception:
        return None

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
        cursor.execute("SELECT COUNT(*) FROM site_html")
        (html_n,) = cursor.fetchone()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        msg = await bot.send_document(
            BACKUP_CHANNEL_ID,
            snap,
            file_name="scraped_data_backup.db",
            caption=f"{BACKUP_TAG}\n🕒 {stamp}\n🎬 Media records: {media_n}\n📄 Saved HTML: {html_n}"
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

def make_album_key(site_key, title, first_url=None):
    """অ্যালবামকে ইউনিকভাবে চিনতে"""
    base = f"{site_key}|{(title or '').strip().lower()[:120]}"
    if first_url:
        base += f"|{first_url}"
    return base

def is_album_processed(album_key):
    cursor.execute("SELECT 1 FROM processed_albums WHERE album_key = ?", (album_key,))
    return cursor.fetchone() is not None

def mark_album_processed(album_key, site_key, title):
    cursor.execute(
        "INSERT OR IGNORE INTO processed_albums (album_key, site_key, title, processed_at) VALUES (?, ?, ?, ?)",
        (album_key, site_key, title or "", datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    schedule_backup()

# ================= ডাইনামিক সাইট ফাইল লোডার =================
loaded_sites = {}

def discover_sites():
    global loaded_sites
    loaded_sites = {}
    os.makedirs(SITES_DIR, exist_ok=True)

    candidates = sorted(glob.glob(os.path.join(SITES_DIR, "*.py")))
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

            if not (hasattr(mod, "extract_albums") or hasattr(mod, "get_albums") or hasattr(mod, "HTML_CONTENT")):
                continue

            name = str(getattr(mod, "SITE_NAME", key)).strip()[:30]
            hashtag = str(getattr(mod, "HASHTAG", DEFAULT_HASHTAG)).strip()
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
active_scans = {}

class ScanCancelled(Exception):
    pass

def check_cancel(state):
    if state and state.get("cancel"):
        raise ScanCancelled()

async def cancellable_sleep(seconds, state):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_cancel(state)
        await asyncio.sleep(min(0.5, end - time.monotonic()))

async def run_cancellable(coro, state):
    check_cancel(state)
    return await coro

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

def get_cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛔ Cancel Scan", callback_data="action_cancel_scan")]
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
    if override_html is None:
        override_html = db_get_site_html(site["key"])
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
        f"প্রতি স্ক্যানে সর্বোচ্চ **{MAX_ALBUMS_PER_SCAN}**টি নতুন অ্যালবাম প্রসেস হবে।\n"
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
        f"📦 **Storage Channel:** `{STORAGE_CHANNEL_ID}`\n"
        f"📌 **Albums per Scan:** `{MAX_ALBUMS_PER_SCAN}`\n"
        f"🏷️ **Default Hashtag:** `{DEFAULT_HASHTAG}`\n\n"
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
                f"⚡ **Telethon Engine:** `{engine_status}`\n"
                f"📌 **Albums per Scan:** `{MAX_ALBUMS_PER_SCAN}`\n\n"
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
                    for u in re.findall(r'https?://[^\s"\'<>]+', msg.text or ""):
                        if not is_duplicate(u):
                            mark_as_uploaded(u, DEFAULT_HASHTAG)
                            count += 1
                if msg.media and getattr(msg, "file", None):
                    pass
            await update_panel(client, user_id, f"✅ **Sync Completed**\n• New media marked: `{count}`", get_home_keyboard())

        elif data == "action_cancel_scan":
            state = active_scans.get(user_id)
            if state:
                state["cancel"] = True
                await cq.answer("Cancelling scan...")
            else:
                await cq.answer("No active scan.")

        elif data.startswith("site_scan_"):
            site_key = data.replace("site_scan_", "", 1)
            site = loaded_sites.get(site_key)
            if not site:
                await cq.answer("Site not found!")
                return
            await cq.answer("Starting scan...")
            override = sess.get("custom_html", {}).get(site_key) or db_get_site_html(site_key)
            started = await start_scan(client, user_id, site, override)
            if not started:
                await update_panel(client, user_id, "⚠️ একটি স্ক্যান ইতিমধ্যে চলছে। আগে সেটা শেষ করুন অথবা ক্যানসেল করুন।", get_site_keyboard(site_key))

        elif data.startswith("site_html_"):
            site_key = data.replace("site_html_", "", 1)
            site = loaded_sites.get(site_key)
            if not site:
                await cq.answer("Site not found!")
                return
            sess["state"] = "WAITING_HTML"
            sess["active_site_key"] = site_key
            await cq.answer()
            await update_panel(
                client, user_id,
                f"📄 **{safe(site['name'])}** এর জন্য নতুন HTML পাঠান\n\n"
                f"• `.html` ফাইল আপলোড করুন অথবা\n"
                f"• HTML কোড সরাসরি পেস্ট করুন\n\n"
                f"পাঠানোর পর অটো সেভ হয়ে যাবে।",
                InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu_home")]])
            )

    except Exception as e:
        print(f"Callback error: {e}")
        traceback.print_exc()

@bot.on_message(filters.private & filters.text & \~filters.command(["start", "backup"]))
async def handle_text(client, message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    sess = user_sessions.setdefault(user_id, {"state": "IDLE", "custom_html": {}})

    # Reply keyboard থেকে সাইট সিলেক্ট
    if text.startswith(SITE_PREFIX):
        site = find_site_by_label(text)
        if site:
            await show_site_panel(client, user_id, site)
            return

    # HTML পেস্ট করা হলে
    if sess.get("state") == "WAITING_HTML":
        site_key = sess.get("active_site_key")
        site = loaded_sites.get(site_key)
        if site and text and ("<html" in text.lower() or "<!doctype" in text.lower() or "div" in text.lower()):
            save_site_html(site_key, text)
            sess.setdefault("custom_html", {})[site_key] = text
            sess["state"] = "IDLE"
            try:
                await message.delete()
            except Exception:
                pass
            await show_site_panel(client, user_id, site, override_html=text)
            return

@bot.on_message(filters.private & filters.document)
async def handle_document(client, message):
    user_id = message.from_user.id
    sess = user_sessions.setdefault(user_id, {"state": "IDLE", "custom_html": {}})
    if sess.get("state") != "WAITING_HTML":
        return

    site_key = sess.get("active_site_key")
    site = loaded_sites.get(site_key)
    if not site:
        return

    doc = message.document
    if not (doc.file_name or "").lower().endswith((".html", ".htm", ".txt")):
        await message.reply("শুধু `.html` বা `.txt` ফাইল সাপোর্ট করে।")
        return

    path = await message.download()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            html_content = f.read()
        save_site_html(site_key, html_content)
        sess.setdefault("custom_html", {})[site_key] = html_content
        sess["state"] = "IDLE"
        await show_site_panel(client, user_id, site, override_html=html_content)
    finally:
        if os.path.exists(path):
            os.remove(path)

# ================= ডাউনলোড ও আপলোড লজিক =================
def url_ext(url, kind="video"):
    path = urlparse(url).path.lower()
    for ext in (".mp4", ".m4v", ".webm", ".mov", ".jpg", ".jpeg", ".png", ".webp"):
        if path.endswith(ext):
            return ext
    return ".mp4" if kind == "video" else ".jpg"

async def download_file_with_details(url, dest_path, site_mod, state, progress_cb=None, max_bytes=None):
    # সাইট মডিউলে custom_download থাকলে সেটা ব্যবহার করবে
    if hasattr(site_mod, "custom_download"):
        return await site_mod.custom_download(url, dest_path, state, progress_cb, max_bytes)

    last_err = "Unknown"
    for attempt in range(1, 4):
        try:
            check_cancel(state)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                    if resp.status != 200:
                        return False, f"HTTP {resp.status}"
                    total = int(resp.headers.get("Content-Length") or 0)
                    downloaded = 0
                    last_cb = 0
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
    single_tag = "#" + hashtag.lstrip("#").split()[0] if hashtag else DEFAULT_HASHTAG
    tag_html = htmllib.escape(single_tag)
    clean_title = re.sub(r"#\S+", "", title or "").strip()
    if clean_title:
        first = f"<b>{htmllib.escape(clean_title[:200])}</b>\n{tag_html}"
    else:
        first = tag_html
    rest = tag_html if REST_CAPTION_HASHTAG else ""
    return first, rest, tag_html

async def send_album(files, caption, uploader=None):
    first_cap, rest_cap = caption[0], caption[1]
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
                caption=first_cap or None, parse_mode="html",
                supports_streaming=(files[0]["kind"] == "video")
            )
        else:
            caps = [first_cap] + [rest_cap] * (len(paths) - 1)
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
                    STORAGE_CHANNEL_ID, video=f["path"], caption=first_cap,
                    parse_mode=enums.ParseMode.HTML, supports_streaming=True
                )
            else:
                await pc.send_photo(
                    STORAGE_CHANNEL_ID, photo=f["path"], caption=first_cap,
                    parse_mode=enums.ParseMode.HTML
                )
        else:
            media = []
            for i, f in enumerate(files):
                cap = first_cap if i == 0 else rest_cap
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

        print(f"⚠️ Album send failed ({type(first_err).__name__}: {first_err}) - retrying once...")
        await cancellable_sleep(3, state)
        try:
            await send_with_flood_retry(files, caption, state, uploader)
            return list(files), []
        except ScanCancelled:
            raise
        except Exception as second_err:
            print(f"⚠️ Album retry failed, fallback to single: {type(second_err).__name__}: {second_err}")

        tag_only = caption[2]
        ok_files, failed = [], []
        for f in files:
            try:
                await send_with_flood_retry([f], (tag_only, tag_only, tag_only), state, uploader)
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
    if override_html is None:
        override_html = db_get_site_html(site["key"])
    state = {"cancel": False}
    task = asyncio.create_task(process_and_upload(client, user_id, site, override_html, state))
    state["task"] = task
    active_scans[user_id] = state
    return True

async def build_uploaders():
    uploaders = []
    seen_ids = set()
    for name, c in pyro_ready:
        try:
            uid = (await c.get_me()).id
        except Exception:
            uid = None
        if uid is not None:
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
        uploaders.append({"type": "pyrogram", "client": c, "name": name})
    for i, c in enumerate(telethon_clients, start=1):
        ok, _ = await ensure_telethon_connected(c)
        if not ok:
            continue
        try:
            uid = (await c.get_me()).id
        except Exception:
            uid = None
        if uid is not None:
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
        uploaders.append({"type": "telethon", "client": c, "name": f"Telethon {i}"})
    if not uploaders:
        uploaders.append({"type": "pyrogram", "client": bot, "name": "Bot Token"})
    return uploaders

async def process_and_upload(client, user_id, site, override_html=None, state=None):
    if state is None:
        state = {"cancel": False}
    site_key = site["key"]
    site_mod = site["module"]
    hashtag = DEFAULT_HASHTAG   # জোর করে #newweb ব্যবহার
    end_kb = get_site_keyboard(site_key)
    cancel_kb = get_cancel_keyboard()

    up_v = up_p = skipped = albums_done = 0
    errors = []
    cancelled = False
    fatal = False
    title = site["name"]
    total_albums = 0
    uploader_mode = "Pyrogram Bot Token"
    workers = {}
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
            f"📌 **Batch Limit:** `{MAX_ALBUMS_PER_SCAN}` albums\n"
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

        # ========== শুধুমাত্র নতুন ২০টা অ্যালবাম ফিল্টার ==========
        new_albums = []
        for album in albums:
            items = album.get("items") or []
            if not items:
                continue

            first_url = items[0][1] if items else ""
            akey = make_album_key(site_key, album.get("title"), first_url)

            if is_album_processed(akey):
                continue

            # সব মিডিয়া আগে থেকেই আপলোড আছে কিনা
            if all(is_duplicate(url) for _, url in items):
                mark_album_processed(akey, site_key, album.get("title"))
                continue

            new_albums.append(album)
            if len(new_albums) >= MAX_ALBUMS_PER_SCAN:
                break

        albums = new_albums
        total_albums = len(albums)

        if not albums:
            await update_panel(
                client, user_id,
                f"✅ **কোনো নতুন অ্যালবাম নেই**\n\n"
                f"• সাইট: `{safe(site['name'])}`\n"
                f"• সব অ্যালবাম আগেই স্ক্যান করা হয়েছে অথবা স্টোরেজ চ্যানেলে আছে।\n"
                f"• নতুন HTML দিয়ে আবার চেষ্টা করুন।",
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

        # জব কিউ তৈরি
        seen_run = set()
        queue = asyncio.Queue()
        prev_done = None
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
                # পুরো অ্যালবাম আগেই আপলোড → মার্ক করে দিই
                first_url = all_items[0][1] if all_items else ""
                akey = make_album_key(site_key, album.get("title"), first_url)
                mark_album_processed(akey, site_key, album.get("title"))
                continue

            done_ev = asyncio.Event()
            queue.put_nowait({
                "a_idx": a_idx, "album": album, "items": items, "all_items": all_items,
                "v_found": v_found, "p_found": p_found, "prev_v": prev_v, "prev_p": prev_p,
                "prev_done": prev_done, "done": done_ev
            })
            prev_done = done_ev

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
            waited = False
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

                    groups, cur, cur_bytes = [], [], 0
                    for f in downloaded_files:
                        if cur and cur_bytes + f["size"] > MAX_BATCH_BYTES:
                            groups.append(cur)
                            cur, cur_bytes = [], 0
                        cur.append(f)
                        cur_bytes += f["size"]
                    if cur:
                        groups.append(cur)

                    for g in groups:
                        check_cancel(state)

                        if ORDERED_ALBUMS and not waited and job.get("prev_done") is not None:
                            ws["action"] = "⏳ আগের অ্যালবামের জন্য অপেক্ষা..."
                            await refresh(force=True)
                            await run_cancellable(job["prev_done"].wait(), state)
                            waited = True

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
                        a_up_v += v
                        a_up_p += p
                        up_v += v
                        up_p += p
                        a_fail += len(errs)
                        errors.extend(errs)
                        await cancellable_sleep(1, state)
            finally:
                for fn in glob.glob(f"tmp_{user_id}_{a_idx}_*"):
                    try:
                        os.remove(fn)
                    except Exception:
                        pass

            # অ্যালবাম কমপ্লিট রিপোর্ট
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

            # অ্যালবাম প্রসেসড হিসেবে মার্ক
            first_url = all_items[0][1] if all_items else ""
            akey = make_album_key(site_key, album.get("title"), first_url)
            mark_album_processed(akey, site_key, album.get("title"))

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
                finally:
                    job["done"].set()

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
        f"🏷️ **Hashtag:** `{hashtag}`\n"
        f"📌 **Batch Limit:** `{MAX_ALBUMS_PER_SCAN}` albums\n"
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
    return f"Modular Scraper Bot is Running! Loaded sites: {len(loaded_sites)} | Max albums/scan: {MAX_ALBUMS_PER_SCAN}"

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

    for i, pc in enumerate(pyro_clients, start=1):
        try:
            await pc.start()
            me = await pc.get_me()
            try:
                await pc.get_chat(STORAGE_CHANNEL_ID)
            except Exception:
                async for _d in pc.get_dialogs():
                    pass
                await pc.get_chat(STORAGE_CHANNEL_ID)
            pyro_ready.append((f"Account {i}", pc))
            print(f"👤 Uploader Account {i} ready: {me.first_name} ({me.id})")
        except Exception as e:
            print(f"⚠️ Uploader Account {i} failed to start: {type(e).__name__}: {e}")

    backup_event = asyncio.Event()
    worker = asyncio.create_task(backup_worker())
    print(f"🤖 Bot is running. Loaded {len(loaded_sites)} site modules. "
          f"Pyrogram uploader accounts: {len(pyro_ready)} | Telethon accounts: {len(telethon_clients)}")
    print(f"📌 MAX_ALBUMS_PER_SCAN = {MAX_ALBUMS_PER_SCAN} | DEFAULT_HASHTAG = {DEFAULT_HASHTAG}")

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
