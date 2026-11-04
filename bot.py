import os
import re
import sqlite3
import asyncio
import traceback
import aiohttp
import time
import glob
import html as htmllib
from urllib.parse import urlparse
from datetime import datetime, timezone
from threading import Thread
from flask import Flask
from bs4 import BeautifulSoup, Tag, NavigableString

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

# Telethon (optional - ইনস্টল না থাকলেও বট ক্র্যাশ করবে না)
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

MAX_SITES = 5  # মেনুতে সর্বোচ্চ কয়টি ওয়েবসাইট বাটন থাকবে
DB_PATH = "scraped_data.db"
# ডাটাবেস ব্যাকআপ যেখানে জমা থাকবে (না দিলে STORAGE_CHANNEL_ID তেই থাকবে)
BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID") or STORAGE_CHANNEL_ID)
BACKUP_TAG = "#DB_BACKUP"
SITE_PREFIX = "🌐 "

bot = Client("html_scraper_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

telethon_client = None
if TELETHON_AVAILABLE and TELETHON_SESSION_STRING:
    telethon_client = TelegramClient(StringSession(TELETHON_SESSION_STRING), API_ID, API_HASH)

# ================= ডাটাবেস =================
conn = None
cursor = None

def init_db():
    """ডাটাবেস খোলে (আগে খোলা থাকলে বন্ধ করে আবার খোলে) এবং টেবিল তৈরি করে"""
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
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            hashtag TEXT,
            html TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

def db_is_empty():
    cursor.execute("SELECT (SELECT COUNT(*) FROM sites), (SELECT COUNT(*) FROM uploaded_media)")
    sites_n, media_n = cursor.fetchone()
    return sites_n == 0 and media_n == 0

init_db()

# ================= টেলিগ্রাম ব্যাকআপ সিস্টেম =================
backup_event = None        # main() এ তৈরি হয়
backup_dirty = False       # ব্যাকআপ হয়নি এমন পরিবর্তন আছে কিনা
last_backup_msg_id = None  # সর্বশেষ ব্যাকআপ মেসেজের আইডি

def schedule_backup():
    """ডাটাবেসে কিছু বদলালে এটা কল হয়; কয়েক সেকেন্ড পর অটো ব্যাকআপ হবে"""
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
    """ডাটাবেসের কপি টেলিগ্রাম চ্যানেলে আপলোড করে পিন করে। (ok, info) রিটার্ন করে"""
    global backup_dirty, last_backup_msg_id
    snap = None
    backup_dirty = False
    try:
        snap = _make_snapshot()
        size_mb = os.path.getsize(snap) / (1024 * 1024)
        if size_mb > 45:
            backup_dirty = True
            return False, f"Database too large for bot upload ({size_mb:.1f} MB)"

        cursor.execute("SELECT (SELECT COUNT(*) FROM sites), (SELECT COUNT(*) FROM uploaded_media)")
        sites_n, media_n = cursor.fetchone()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        msg = await bot.send_document(
            BACKUP_CHANNEL_ID,
            snap,
            file_name="scraped_data_backup.db",
            caption=f"{BACKUP_TAG}\n🕒 {stamp}\n🌐 Sites: {sites_n}\n🎬 Media records: {media_n}"
        )
        try:
            await bot.pin_chat_message(BACKUP_CHANNEL_ID, msg.id, disable_notification=True)
        except Exception as pin_err:
            print(f"⚠️ Backup pin failed (give bot 'Pin Messages' admin right): {pin_err}")

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
    """পরিবর্তন হলে ১০ সেকেন্ড অপেক্ষা করে একবারে ব্যাকআপ নেয় (বারবার আপলোড এড়াতে)"""
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
        return "sites" in names and "uploaded_media" in names
    except Exception:
        return False

async def restore_db_from_telegram():
    """ডিপ্লয়ের পর ডাটাবেস খালি থাকলে টেলিগ্রামের সর্বশেষ ব্যাকআপ থেকে ফিরিয়ে আনে"""
    global last_backup_msg_id
    if not db_is_empty():
        return "Local database already has data - restore skipped."

    path = None
    msg_id = None

    # ১. পিন করা ব্যাকআপ মেসেজ থেকে (বট দিয়ে)
    try:
        chat = await bot.get_chat(BACKUP_CHANNEL_ID)
        pm = chat.pinned_message
        if pm and pm.document and (pm.caption or "").startswith(BACKUP_TAG):
            path = await bot.download_media(pm, file_name="./restore_tmp.db")
            msg_id = pm.id
    except Exception as e:
        print(f"⚠️ Pinned backup lookup failed: {type(e).__name__}: {e}")

    # ২. না পেলে Telethon দিয়ে চ্যানেলে #DB_BACKUP সার্চ
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
                print(f"⚠️ Telethon backup search failed: {type(e).__name__}: {e}")

    if not path or not os.path.exists(path):
        return "No backup found in Telegram (fresh start)."
    if not _valid_backup_file(path):
        os.remove(path)
        return "Backup file found but invalid - ignored."

    conn.close()
    os.replace(path, DB_PATH)
    init_db()
    last_backup_msg_id = msg_id
    cursor.execute("SELECT (SELECT COUNT(*) FROM sites), (SELECT COUNT(*) FROM uploaded_media)")
    sites_n, media_n = cursor.fetchone()
    return f"Restored from Telegram backup: {sites_n} sites, {media_n} media records."

def is_duplicate(url):
    cursor.execute("SELECT 1 FROM uploaded_media WHERE media_url = ?", (url,))
    return cursor.fetchone() is not None

def mark_as_uploaded(url, hashtag):
    cursor.execute("INSERT OR IGNORE INTO uploaded_media (media_url, hashtag) VALUES (?, ?)", (url, hashtag))
    conn.commit()
    schedule_backup()

def save_site(user_id, name, hashtag, html):
    """একই নামের সাইট থাকলে আপডেট করবে, নাহলে নতুন সেভ করবে। শুধু শেষ ৫টা রাখবে।"""
    cursor.execute("SELECT id FROM sites WHERE user_id = ? AND name = ?", (user_id, name))
    row = cursor.fetchone()
    if row:
        site_id = row[0]
        cursor.execute(
            "UPDATE sites SET html = ?, hashtag = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (html, hashtag, site_id)
        )
    else:
        cursor.execute(
            "INSERT INTO sites (user_id, name, hashtag, html) VALUES (?, ?, ?, ?)",
            (user_id, name, hashtag, html)
        )
        site_id = cursor.lastrowid
    # পুরনো সাইট মুছে শুধু সর্বশেষ MAX_SITES রাখা
    cursor.execute(
        "DELETE FROM sites WHERE user_id = ? AND id NOT IN "
        "(SELECT id FROM sites WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT ?)",
        (user_id, user_id, MAX_SITES)
    )
    conn.commit()
    schedule_backup()
    return site_id

def get_sites(user_id):
    cursor.execute(
        "SELECT id, name, hashtag FROM sites WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT ?",
        (user_id, MAX_SITES)
    )
    return cursor.fetchall()

def get_site(user_id, site_id):
    cursor.execute("SELECT id, name, hashtag, html FROM sites WHERE user_id = ? AND id = ?", (user_id, site_id))
    return cursor.fetchone()

def find_site_by_label(user_id, label):
    name = label[len(SITE_PREFIX):] if label.startswith(SITE_PREFIX) else label
    cursor.execute("SELECT id, name, hashtag, html FROM sites WHERE user_id = ? AND name = ?", (user_id, name))
    return cursor.fetchone()

def delete_site(user_id, site_id):
    cursor.execute("DELETE FROM sites WHERE user_id = ? AND id = ?", (user_id, site_id))
    conn.commit()
    schedule_backup()

# ================= ইউজার সেশন মেমোরি =================
user_sessions = {}

# ================= Inline বাটন =================
def get_home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("add HTML FILE 🗄️", callback_data="menu_add_html")],
        [InlineKeyboardButton("🔄 Sync Storage Channel (Telethon)", callback_data="action_sync_channel")]
    ])

def get_tags_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("#rells", callback_data="tag_#rells"),
         InlineKeyboardButton("#movies", callback_data="tag_#movies")],
        [InlineKeyboardButton("#funny", callback_data="tag_#funny"),
         InlineKeyboardButton("#series", callback_data="tag_#series")],
        [InlineKeyboardButton("✏️ Custom Hashtag", callback_data="tag_custom")],
        [InlineKeyboardButton("🔙 Back to Home", callback_data="menu_home")]
    ])

def get_waiting_html_keyboard(parts_count=0):
    buttons = []
    if parts_count > 0:
        buttons.append([InlineKeyboardButton(f"✅ Save Website ({parts_count} Parts Collected)", callback_data="action_process")])
    buttons.append([InlineKeyboardButton("❌ Cancel / Home", callback_data="menu_home")])
    return InlineKeyboardMarkup(buttons)

def get_site_keyboard(site_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Scan Website", callback_data=f"site_scan_{site_id}")],
        [InlineKeyboardButton("🗑️ Delete", callback_data=f"site_del_{site_id}"),
         InlineKeyboardButton("🏠 Home", callback_data="menu_home")]
    ])

# ================= মেসেজ বক্সের পাশের মেনু (Reply Keyboard) =================
def build_sites_reply_keyboard(user_id):
    sites = get_sites(user_id)
    if not sites:
        return None
    rows = [[KeyboardButton(f"{SITE_PREFIX}{name}")] for (_id, name, _tag) in sites]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, placeholder="Select a website...")

async def refresh_menu(client, user_id):
    """সাইট লিস্ট অনুযায়ী মেসেজ বক্সের মেনু আপডেট করে"""
    session = user_sessions.setdefault(user_id, {})
    kb = build_sites_reply_keyboard(user_id)
    old_id = session.get("kb_msg_id")
    try:
        if kb:
            msg = await client.send_message(user_id, "📚 Website menu updated", reply_markup=kb)
        else:
            msg = await client.send_message(user_id, "📚 Website menu cleared", reply_markup=ReplyKeyboardRemove())
        session["kb_msg_id"] = msg.id
        if old_id:
            try:
                await client.delete_messages(user_id, old_id)
            except Exception:
                pass
    except Exception:
        pass

# ================= প্যানেল আপডেট হেল্পার =================
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
    """পুরনো প্যানেল মেসেজ মুছে দেয়, যাতে পরের update_panel নতুন মেসেজ হিসেবে চ্যাটের একদম নিচে আসে"""
    session = user_sessions.setdefault(user_id, {})
    old_id = session.get("panel_msg_id")
    if old_id:
        try:
            await client.delete_messages(user_id, old_id)
        except Exception:
            pass
    session["panel_msg_id"] = None

# ================= Telethon কানেকশন চেক =================
async def ensure_telethon_connected():
    if not TELETHON_AVAILABLE:
        return False, "Telethon library is not installed. Add 'telethon' to requirements.txt."
    if not telethon_client:
        return False, "TELETHON_SESSION_STRING is missing in Environment Variables."
    try:
        if not telethon_client.is_connected():
            await telethon_client.connect()
        if not await telethon_client.is_user_authorized():
            return False, "Telethon Session String is invalid or expired. Please generate a new one."
        return True, "Connected"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)}"

# ================= ওয়েবসাইট সেভ ও সাইট প্যানেল =================
def make_site_name(title):
    name = re.sub(r"\s+", " ", title or "").strip()
    if not name:
        name = "Website"
    return name[:30]

def albums_panel_text(albums):
    v_total, p_total = album_totals(albums)
    text = (
        f"📁 **Albums Detected:** `{len(albums)}`\n"
        f"🎬 **Videos:** `{v_total}` | 🖼️ **Photos:** `{p_total}`\n"
    )
    if albums:
        text += f"\n```text\n{describe_albums(albums)}\n```\n"
    return text

async def register_site(client, user_id, html_content, hashtag):
    """HTML সেভ করে মেনু বাটনে সাইটের নাম যোগ করে (তখনই স্ক্যান করে না)"""
    title, albums = extract_albums(html_content)
    name = make_site_name(title)
    site_id = save_site(user_id, name, hashtag, html_content)

    user_sessions.setdefault(user_id, {})["state"] = "IDLE"
    user_sessions[user_id]["buffer"] = []

    await reset_panel(client, user_id)
    await refresh_menu(client, user_id)
    await update_panel(
        client, user_id,
        f"✅ **Website Saved**\n\n"
        f"🌐 **Name:** `{safe(name)}`\n"
        f"🏷️ **Hashtag:** `{hashtag}`\n"
        f"{albums_panel_text(albums)}\n"
        f"মেসেজ বক্সের পাশের মেনু বাটনে সাইটের নাম যোগ হয়েছে। এখনই স্ক্যান করতে নিচের বাটনে ক্লিক করুন:",
        get_site_keyboard(site_id)
    )

async def show_site_panel(client, user_id, site):
    site_id, name, hashtag, html_content = site
    _t, albums = extract_albums(html_content)
    user_sessions.setdefault(user_id, {})["state"] = "IDLE"
    await reset_panel(client, user_id)
    await update_panel(
        client, user_id,
        f"🌐 **{safe(name)}**\n\n"
        f"🏷️ **Hashtag:** `{hashtag}`\n"
        f"{albums_panel_text(albums)}\n"
        f"অ্যালবাম বাই অ্যালবাম ভিডিও ও ছবি স্ক্যান করে আপলোড করতে নিচের বাটনে ক্লিক করুন:",
        get_site_keyboard(site_id)
    )

# ================= স্টার্ট কমান্ড ও কলব্যাক =================
@bot.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    user_id = message.from_user.id
    try:
        await message.delete()
    except Exception:
        pass

    old = user_sessions.get(user_id, {})
    user_sessions[user_id] = {
        "state": "IDLE",
        "buffer": [],
        "hashtag": "#rells",
        "panel_msg_id": old.get("panel_msg_id"),
        "kb_msg_id": old.get("kb_msg_id")
    }

    t_ok, t_status = await ensure_telethon_connected()
    engine_status = "🟢 Active (Telethon Session)" if t_ok else f"🔴 Inactive ({t_status})"

    welcome_text = (
        "🤖 **HTML Media Scraper Dashboard**\n\n"
        f"⚡ **Telethon Engine:** `{engine_status}`\n"
        f"📦 **Storage Channel:** `{STORAGE_CHANNEL_ID}`\n\n"
        "ওয়েবসাইটের HTML কোড বা ফাইল থেকে ভিডিও ও ছবি স্বয়ংক্রিয়ভাবে স্টোরেজ চ্যানেলে আপলোড করতে নিচের বাটনে ক্লিক করুন:"
    )
    await reset_panel(client, user_id)
    if get_sites(user_id):
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
    if ok:
        text = (
            "✅ **BACKUP COMPLETED**\n\n"
            f"• **Time:** `{info}`\n"
            f"• **Saved In:** `{BACKUP_CHANNEL_ID}` (pinned message)\n"
            "• **Auto Restore:** `Enabled on every deploy/restart`"
        )
    else:
        text = f"❌ **BACKUP FAILED**\n\n**Reason:** `{info}`\n\nCheck that the bot is admin in the channel with Post/Pin/Delete rights."
    await update_panel(client, user_id, text, get_home_keyboard())

@bot.on_callback_query()
async def handle_callbacks(client, cq):
    user_id = cq.from_user.id
    data = cq.data

    if user_id not in user_sessions:
        user_sessions[user_id] = {"state": "IDLE", "buffer": [], "hashtag": "#rells", "panel_msg_id": cq.message.id}
    else:
        user_sessions[user_id]["panel_msg_id"] = cq.message.id

    try:
        if data == "menu_home":
            user_sessions[user_id]["state"] = "IDLE"
            user_sessions[user_id]["buffer"] = []
            t_ok, t_status = await ensure_telethon_connected()
            engine_status = "🟢 Active (Telethon Session)" if t_ok else f"🔴 Inactive ({t_status})"
            await update_panel(
                client, user_id,
                f"🤖 **HTML Media Scraper Dashboard**\n\n"
                f"⚡ **Telethon Engine:** `{engine_status}`\n"
                f"নতুন ফাইল বা কোড যোগ করতে নিচের বাটনে ক্লিক করুন:",
                get_home_keyboard()
            )

        elif data == "action_sync_channel":
            await cq.answer("Syncing storage channel via Telethon...")
            t_ok, t_err = await ensure_telethon_connected()
            if not t_ok:
                await update_panel(
                    client, user_id,
                    f"❌ **TELETHON CONNECTION FAILED**\n\n**Reason:** `{t_err}`",
                    get_home_keyboard()
                )
                return

            await update_panel(client, user_id, "🔄 **Scanning Storage Channel via Telethon...**\nChecking existing posts & media...")
            count = 0
            async for msg in telethon_client.iter_messages(STORAGE_CHANNEL_ID, limit=500):
                if msg.text:
                    urls = re.findall(r'https?://[^\s"\'<>]+', msg.text)
                    for u in urls:
                        mark_as_uploaded(u, "#synced")
                count += 1

            await update_panel(
                client, user_id,
                f"✅ **CHANNEL SYNC COMPLETED**\n\n"
                f"• **Scanned Messages:** `{count} recent posts`\n"
                f"• **Status:** `Database synced with Storage Channel via Telethon.`",
                get_home_keyboard()
            )

        elif data == "menu_add_html":
            user_sessions[user_id]["state"] = "WAITING_TAG"
            user_sessions[user_id]["buffer"] = []
            await update_panel(
                client, user_id,
                "🏷️ **Step 1: Select Section Hashtag**\n\nএই ওয়েবসাইটের ভিডিওগুলো চিহ্নিত করার জন্য নিচের যেকোনো একটি হ্যাশট্যাগ সিলেক্ট করুন:",
                get_tags_keyboard()
            )

        elif data.startswith("tag_"):
            selected = data.split("_", 1)[1]
            if selected == "custom":
                user_sessions[user_id]["state"] = "WAITING_CUSTOM_TAG"
                await update_panel(
                    client, user_id,
                    "✏️ **Enter Custom Hashtag**\n\nআপনার পছন্দের হ্যাশট্যাগটি চ্যাটে লিখে পাঠান (যেমন: `#natok` বা `#viral`):",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_add_html")]])
                )
            else:
                user_sessions[user_id]["hashtag"] = selected
                user_sessions[user_id]["state"] = "WAITING_HTML"
                user_sessions[user_id]["buffer"] = []
                await update_panel(
                    client, user_id,
                    f"✅ **Selected Hashtag:** `{selected}`\n\n"
                    f"📄 **Step 2: Send HTML File or Code**\n"
                    f"এখন কোনো এডিট ছাড়াই সরাসরি `.html` ফাইল আপলোড করুন অথবা সম্পূর্ণ কোড কপি করে চ্যাটে পেস্ট করুন।\n"
                    f"*(কোড ১০-১৫ ভাগে ভাগ হয়ে গেলেও বট সব এই এক মেসেজেই জমা করে নেবে)*",
                    get_waiting_html_keyboard(0)
                )

        elif data == "action_process":
            buffer_list = user_sessions[user_id].get("buffer", [])
            if not buffer_list:
                await cq.answer("⚠️ No HTML code received yet!", show_alert=True)
                return
            full_html = "".join(buffer_list)
            hashtag = user_sessions[user_id].get("hashtag", "#rells")
            await register_site(client, user_id, full_html, hashtag)

        elif data.startswith("site_scan_"):
            site_id = int(data.split("_")[2])
            site = get_site(user_id, site_id)
            if not site:
                await cq.answer("⚠️ Website not found. Please add it again.", show_alert=True)
                return
            _id, name, hashtag, html = site
            started = await start_scan(client, user_id, html, hashtag, site_id)
            if not started:
                await cq.answer("⚠️ A scan is already running. Use Cancel Scan first.", show_alert=True)
                return
            user_sessions[user_id]["state"] = "PROCESSING"
            await cq.answer("Scan started")
            return

        elif data == "scan_cancel":
            st = active_scans.get(user_id)
            if st and not st["task"].done():
                st["cancel"] = True
                await cq.answer("Stopping scan... please wait a moment.")
            else:
                await cq.answer("No scan is running.", show_alert=True)
            return

        elif data.startswith("site_del_"):
            site_id = int(data.split("_")[2])
            delete_site(user_id, site_id)
            await update_panel(
                client, user_id,
                "🗑️ **Website deleted from menu.**",
                get_home_keyboard()
            )
            await refresh_menu(client, user_id)

        await cq.answer()

    except Exception as e:
        err_trace = traceback.format_exc()
        await update_panel(
            client, user_id,
            f"❌ **SYSTEM ERROR IN CALLBACK**\n\n"
            f"**Reason:** `{type(e).__name__}: {str(e)}`\n"
            f"**Details:**\n```python\n{err_trace[-1000:]}\n```",
            get_home_keyboard()
        )

# ================= টেক্সট ও ডকুমেন্ট ইনপুট =================
@bot.on_message(filters.text & filters.private)
async def handle_text_input(client, message):
    user_id = message.from_user.id
    text = message.text.strip()

    try:
        await message.delete()
    except Exception:
        pass

    if user_id not in user_sessions:
        user_sessions[user_id] = {"state": "IDLE", "buffer": [], "hashtag": "#rells"}

    # মেনু বাটনে সাইটের নামে ক্লিক করলে
    if text.startswith(SITE_PREFIX):
        site = find_site_by_label(user_id, text)
        if site:
            await show_site_panel(client, user_id, site)
            return

    state = user_sessions[user_id].get("state")

    if state == "WAITING_CUSTOM_TAG":
        tag = text if text.startswith("#") else f"#{text}"
        user_sessions[user_id]["hashtag"] = tag
        user_sessions[user_id]["state"] = "WAITING_HTML"
        user_sessions[user_id]["buffer"] = []
        await update_panel(
            client, user_id,
            f"✅ **Custom Hashtag Set:** `{tag}`\n\n"
            f"📄 **Step 2: Send HTML File or Code**\n"
            f"এখন `.html` ফাইল আপলোড করুন অথবা সম্পূর্ণ কোড পেস্ট করুন।",
            get_waiting_html_keyboard(0)
        )

    elif state == "WAITING_HTML":
        user_sessions[user_id]["buffer"].append(message.text)
        parts = len(user_sessions[user_id]["buffer"])
        total_chars = sum(len(x) for x in user_sessions[user_id]["buffer"])
        hashtag = user_sessions[user_id].get("hashtag", "#rells")

        await update_panel(
            client, user_id,
            f"📥 **Receiving HTML Code Parts...**\n\n"
            f"🏷️ **Hashtag:** `{hashtag}`\n"
            f"🧩 **Parts Collected:** `{parts} part(s)`\n"
            f"📊 **Total Size:** `{total_chars} characters`\n\n"
            f"সবগুলো অংশ পেস্ট করা শেষ হলে নিচের **✅ Save Website** বাটনে ক্লিক করুন:",
            get_waiting_html_keyboard(parts)
        )

@bot.on_message(filters.document & filters.private)
async def handle_document_input(client, message):
    user_id = message.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {"state": "IDLE", "buffer": [], "hashtag": "#rells"}

    hashtag = user_sessions[user_id].get("hashtag", "#rells")
    file_name = message.document.file_name or "unknown_file"

    if not file_name.lower().endswith((".html", ".htm", ".txt")):
        try:
            await message.delete()
        except Exception:
            pass
        await update_panel(
            client, user_id,
            f"❌ **INVALID FILE FORMAT**\n\n"
            f"**File:** `{file_name}`\n"
            f"**Reason:** `Only .html, .htm, or .txt files are supported.`",
            get_waiting_html_keyboard(0)
        )
        return

    await update_panel(client, user_id, f"📥 **Downloading File:** `{file_name}`...\nPlease wait...")

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

        await register_site(client, user_id, html_content, hashtag)

    except Exception as e:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        err_trace = traceback.format_exc()
        await update_panel(
            client, user_id,
            f"❌ **FILE PROCESSING FAILED**\n\n"
            f"**File Name:** `{file_name}`\n"
            f"**Error Type:** `{type(e).__name__}`\n"
            f"**Reason:** `{str(e)}`\n\n"
            f"**Traceback Details:**\n```python\n{err_trace[-1000:]}\n```",
            get_home_keyboard()
        )

# ================= অ্যালবাম স্ক্যান, ডাউনলোড ও আপলোড লজিক =================
MP4_RE = re.compile(r'https?://[^\s"\'<>]+?\.mp4(?:\?[^\s"\'<>]*)?', re.IGNORECASE)
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
IGNORE_IMG_RE = re.compile(r"(?<![a-z])(logo|icon|avatar|banner|ads?|pixel|sprite|emoji|favicon)(?![a-z])", re.IGNORECASE)
ALBUM_CLASS_RE = re.compile(r"album|gallery|folder|collection", re.IGNORECASE)
HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "summary"]
MAX_ALBUM_BATCH = 10  # টেলিগ্রাম একটি অ্যালবামে সর্বোচ্চ ১০টি ফাইল নেয়
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_MB", "2000")) * 1024 * 1024    # টেলিগ্রামের ২GB সীমা
MAX_BATCH_BYTES = int(os.environ.get("MAX_BATCH_MB", "1200")) * 1024 * 1024  # একসাথে ডিস্কে জমা রাখার সীমা

class ScanCancelled(BaseException):
    """ইউজার Cancel চাপলে এটা রেইজ হয় (BaseException, যাতে সাধারণ except এ আটকে না যায়)"""
    pass

active_scans = {}  # user_id -> {"cancel": bool, "task": Task}

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Cancel Scan", callback_data="scan_cancel")]])

def safe(text):
    return str(text).replace("`", "'")

def check_cancel(state):
    if state and state.get("cancel"):
        raise ScanCancelled()

async def cancellable_sleep(seconds, state):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_cancel(state)
        await asyncio.sleep(min(1, max(0, end - time.monotonic())))

async def run_cancellable(coro, state):
    """কোনো কাজ চলার সময় Cancel চাপলে সেটা মাঝপথে বন্ধ করে দেয়"""
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

# ---------- HTML থেকে অ্যালবাম বের করার লজিক ----------
def _first_http(tag, attrs):
    for a in attrs:
        v = tag.get(a)
        if isinstance(v, str) and v.strip().startswith("http"):
            return v.strip()
    return None

def classify_tag(tag):
    """একটি ট্যাগ ভিডিও/ছবি হলে ("video"/"image", url) রিটার্ন করে"""
    for val in tag.attrs.values():
        if isinstance(val, str) and ".mp4" in val.lower():
            m = MP4_RE.search(val)
            if m:
                return ("video", htmllib.unescape(m.group(0)))

    if tag.name == "img":
        parent = tag.parent
        # ভিডিওর থাম্বনেইল (mp4 লিংকের ভিতরের ছবি) বাদ
        if parent is not None and parent.name == "a" and ".mp4" in (parent.get("href") or "").lower():
            return None
        url = _first_http(tag, ("src", "data-src", "data-original", "data-lazy-src"))
    elif tag.name == "a":
        url = _first_http(tag, ("href",))
    else:
        return None

    if not url:
        return None
    path = urlparse(url).path.lower()
    if not path.endswith(IMG_EXTS):
        return None
    if IGNORE_IMG_RE.search(url.lower()):
        return None
    return ("image", url)

def iter_scope_media(scope):
    for node in scope.descendants:
        if isinstance(node, Tag):
            r = classify_tag(node)
            if r:
                yield r
        elif isinstance(node, NavigableString):
            if node.parent is not None and node.parent.name in ("script", "noscript"):
                for u in MP4_RE.findall(str(node)):
                    yield ("video", htmllib.unescape(u))

def _clean_title(text):
    t = re.sub(r"\s+", " ", text or "").strip()
    return t[:150] if t else None

def find_album_title(container):
    h = container.find(HEADING_TAGS)
    if h is not None:
        t = _clean_title(h.get_text(" ", strip=True))
        if t:
            return t
    el = container.find(class_=re.compile(r"title|name|caption", re.IGNORECASE))
    if el is not None:
        t = _clean_title(el.get_text(" ", strip=True))
        if t:
            return t
    for attr in ("data-title", "title", "aria-label", "data-name"):
        v = container.get(attr)
        if isinstance(v, str) and _clean_title(v):
            return _clean_title(v)
    sib = container.find_previous_sibling()
    for _ in range(2):
        if sib is None:
            break
        if sib.name in HEADING_TAGS:
            t = _clean_title(sib.get_text(" ", strip=True))
            if t:
                return t
        sib = sib.find_previous_sibling()
    return None

def _strategy_containers(soup):
    """class/id তে album, gallery, folder, collection থাকা ব্লক = এক একটি অ্যালবাম"""
    cands = []
    for t in soup.find_all(["div", "section", "article", "li", "details", "figure", "ul"]):
        cid = " ".join(t.get("class") or []) + " " + (t.get("id") or "")
        if ALBUM_CLASS_RE.search(cid):
            cands.append(t)
    with_media = []
    for t in cands:
        items = list(iter_scope_media(t))
        if items:
            with_media.append((t, items))
    ids = {id(t) for t, _ in with_media}
    albums = []
    for t, items in with_media:
        if t.find(lambda x: id(x) in ids):
            continue  # ভিতরে আরও ছোট অ্যালবাম ব্লক আছে, তাই এটা র‍্যাপার
        albums.append({"title": find_album_title(t), "items": items})
    return albums

def _strategy_headings(soup):
    """হেডিং (h1-h6) থেকে পরের হেডিং পর্যন্ত সব মিডিয়া = এক একটি অ্যালবাম"""
    albums = []
    cur = None
    root = soup.body or soup
    for node in root.descendants:
        if isinstance(node, Tag):
            if node.name in HEADING_TAGS:
                text = _clean_title(node.get_text(" ", strip=True))
                if text:
                    cur = {"title": text, "items": []}
                    albums.append(cur)
                continue
            r = classify_tag(node)
            if r and cur is not None:
                cur["items"].append(r)
        elif isinstance(node, NavigableString):
            if cur is not None and node.parent is not None and node.parent.name in ("script", "noscript"):
                for u in MP4_RE.findall(str(node)):
                    cur["items"].append(("video", htmllib.unescape(u)))
    return albums

def _finalize(albums):
    out = []
    for a in albums:
        seen = set()
        items = []
        for kind, url in a["items"]:
            if url in seen:
                continue
            seen.add(url)
            items.append((kind, url))
        if items:
            out.append({"title": a.get("title") or f"Album {len(out) + 1}", "items": items})
    return out

def extract_albums(html_content):
    """রিটার্ন: (page_title, [ {"title": str, "items": [(kind, url), ...]}, ... ])"""
    soup = BeautifulSoup(html_content, "html.parser")
    page_title = soup.title.string.strip() if soup.title and soup.title.string else "Website Media"

    a_albums = _finalize(_strategy_containers(soup))
    if len(a_albums) >= 2:
        return page_title, a_albums

    b_albums = _finalize(_strategy_headings(soup))
    if len(b_albums) >= 2:
        return page_title, b_albums
    if a_albums:
        return page_title, a_albums
    if b_albums:
        return page_title, b_albums

    whole = _finalize([{"title": page_title, "items": list(iter_scope_media(soup))}])
    return page_title, whole

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

# ---------- ডাউনলোড ----------
def url_ext(url, kind):
    path = urlparse(url).path.lower()
    for e in (".mp4", ".jpg", ".jpeg", ".png", ".webp"):
        if path.endswith(e):
            return e
    return ".mp4" if kind == "video" else ".jpg"

async def download_file_with_details(url, dest_path, state=None, progress_cb=None, max_bytes=None):
    parsed = urlparse(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"{parsed.scheme}://{parsed.netloc}/"
    }
    last_err = "Unknown error"
    for attempt in range(1, 4):
        try:
            # মোট সময়ের সীমা নেই (৩০-৬০ মিনিটের ভিডিও চলবে); ৫ মিনিট কোনো ডাটা না এলে টাইমআউট
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return False, f"HTTP Error {resp.status} ({resp.reason}) - Server blocked or file removed."

                    total = resp.content_length or 0
                    if max_bytes and total and total > max_bytes:
                        return False, f"File too large ({total // (1024 * 1024)} MB, limit {max_bytes // (1024 * 1024)} MB)."

                    downloaded = 0
                    last_cb = time.monotonic()
                    with open(dest_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            if state and state.get("cancel"):
                                raise ScanCancelled()
                            f.write(chunk)
                            downloaded += len(chunk)
                            if max_bytes and downloaded > max_bytes:
                                return False, "File exceeds the Telegram size limit."
                            if progress_cb and time.monotonic() - last_cb >= 4:
                                last_cb = time.monotonic()
                                try:
                                    await progress_cb(downloaded, total)
                                except Exception:
                                    pass

            if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
                return False, "Downloaded file is 0 bytes (Empty response)."
            return True, "OK"

        except asyncio.TimeoutError:
            last_err = "Connection Timeout: no data received for 5 minutes."
        except aiohttp.ClientConnectorError as e:
            last_err = f"DNS/Connection Error: Cannot connect to host ({str(e)})."
        except aiohttp.ClientError as e:
            last_err = f"{type(e).__name__}: {str(e)}"
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)}"

        if os.path.exists(dest_path):
            os.remove(dest_path)
        await cancellable_sleep(3 * attempt, state)

    return False, f"{last_err} (failed after 3 attempts)"

# ---------- আপলোড ----------
def build_caption(title, a_idx, total_albums, part, multi_part, hashtag):
    cap = f"<b>{htmllib.escape(title[:200])}</b>\n📁 Album {a_idx}/{total_albums}"
    if multi_part:
        cap += f" • Part {part}"
    cap += f"\n{htmllib.escape(hashtag)}"
    return cap

async def send_album(files, caption):
    """files: [{"path","kind","url"}] — ২ বা তার বেশি হলে অ্যালবাম আকারে, ১টি হলে সিঙ্গেল পোস্ট"""
    t_ok, _ = await ensure_telethon_connected()
    paths = [f["path"] for f in files]

    if t_ok:
        if len(paths) == 1:
            await telethon_client.send_file(
                STORAGE_CHANNEL_ID, paths[0],
                caption=caption or None, parse_mode="html",
                supports_streaming=(files[0]["kind"] == "video")
            )
        else:
            caps = [caption] + [""] * (len(paths) - 1)
            await telethon_client.send_file(
                STORAGE_CHANNEL_ID, paths,
                caption=caps, parse_mode="html",
                supports_streaming=True
            )
    else:
        if len(files) == 1:
            f = files[0]
            if f["kind"] == "video":
                await bot.send_video(
                    STORAGE_CHANNEL_ID, video=f["path"], caption=caption,
                    parse_mode=enums.ParseMode.HTML, supports_streaming=True
                )
            else:
                await bot.send_photo(
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
            await bot.send_media_group(STORAGE_CHANNEL_ID, media)

async def send_with_flood_retry(files, caption, state):
    for _ in range(3):
        try:
            await run_cancellable(send_album(files, caption), state)
            return
        except (FloodWait, FloodWaitError) as fw:
            wait_sec = int(getattr(fw, "value", getattr(fw, "seconds", 10)) or 10)
            await cancellable_sleep(min(wait_sec + 1, 600), state)
    raise RuntimeError("Too many FloodWait retries")

async def send_batch(files, caption, state):
    """অ্যালবাম সেন্ড করে। ব্যর্থ হলে ফাইলগুলো একটি একটি করে পাঠানোর চেষ্টা করে। রিটার্ন: (ok_files, [(url, error)])"""
    try:
        await send_with_flood_retry(files, caption, state)
        return list(files), []
    except ScanCancelled:
        raise
    except Exception as first_err:
        if len(files) == 1:
            return [], [(files[0]["url"], f"{type(first_err).__name__}: {first_err}")]
        ok_files, failed = [], []
        captioned = False
        for f in files:
            try:
                await send_with_flood_retry([f], "" if captioned else caption, state)
                captioned = True
                ok_files.append(f)
            except ScanCancelled:
                raise
            except Exception as e:
                failed.append((f["url"], f"{type(e).__name__}: {e}"))
        return ok_files, failed

async def upload_album_batch(batch, title, a_idx, total_albums, part, multi_part, hashtag, state):
    caption = build_caption(title, a_idx, total_albums, part, multi_part, hashtag)
    try:
        ok_files, failed = await send_batch(batch, caption, state)
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
        print(f"⚠️ Could not send album report to channel: {type(e).__name__}: {e}")

# ---------- স্ক্যান শুরু (ব্যাকগ্রাউন্ড টাস্ক) ----------
async def start_scan(client, user_id, html_content, hashtag, site_id=None):
    old = active_scans.get(user_id)
    if old and not old["task"].done():
        return False
    state = {"cancel": False}
    task = asyncio.create_task(process_and_upload(client, user_id, html_content, hashtag, state, site_id))
    state["task"] = task
    active_scans[user_id] = state
    return True

async def process_and_upload(client, user_id, html_content, hashtag, state=None, site_id=None):
    if state is None:
        state = {"cancel": False}
    end_kb = get_site_keyboard(site_id) if site_id else get_home_keyboard()
    cancel_kb = get_cancel_keyboard()

    up_v = up_p = skipped = albums_done = albums_skipped = 0
    errors = []
    cancelled = False
    fatal = False
    title = "Website"
    total_albums = 0
    uploader_mode = "Pyrogram Bot Token"
    ctx = {"a_idx": 0, "title": ""}

    def progress_text(action, extra=""):
        return (
            f"⏳ **Scanning Albums**\n\n"
            f"⚡ **Engine:** `{uploader_mode}`\n"
            f"📁 **Album:** `{ctx['a_idx']}/{total_albums}` - `{safe(ctx['title'])[:40]}`\n"
            f"{action}\n"
            f"{extra}"
            f"✅ **Uploaded:** `{up_v} videos, {up_p} photos` | ❌ **Failed:** `{len(errors)}`"
        )

    try:
        await update_panel(client, user_id, "🔍 **Scanning Website...**\nDetecting albums, photos and videos...", cancel_kb)
        title, albums = extract_albums(html_content)
        total_albums = len(albums)

        if not albums:
            await update_panel(
                client, user_id,
                "⚠️ **NO MEDIA FOUND IN HTML**\n\n"
                "**Diagnostic Report (English):**\n"
                f"• `Total HTML Length:` `{len(html_content)} chars`\n"
                "• `Albums Detected:` `0`\n"
                "• **Possible Cause:** The website might be loading videos dynamically via encrypted JavaScript (m3u8/blob) or requires login cookies.",
                end_kb
            )
            return

        t_ok, _ = await ensure_telethon_connected()
        uploader_mode = "Telethon User Session" if t_ok else "Pyrogram Bot Token"
        seen_run = set()

        for a_idx, album in enumerate(albums, start=1):
            check_cancel(state)
            ctx["a_idx"] = a_idx
            ctx["title"] = album["title"]

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
                albums_skipped += 1
                continue

            batch = []
            batch_bytes = 0
            part = 0
            a_up_v = a_up_p = a_fail = 0

            async def flush(batch_list, part_no):
                mb = sum(f["size"] for f in batch_list) // (1024 * 1024)
                await update_panel(
                    client, user_id,
                    progress_text(f"⬆️ **Uploading Album Part {part_no}:** `{len(batch_list)} files, {mb} MB`"),
                    cancel_kb
                )
                ok_files, failed = await upload_album_batch(
                    batch_list, album["title"], a_idx, total_albums, part_no,
                    len(items) > MAX_ALBUM_BATCH or part_no > 1, hashtag, state
                )
                return tally(ok_files, failed, a_idx)

            try:
                for item_idx, (kind, url) in enumerate(items, start=1):
                    check_cancel(state)
                    label = "Video" if kind == "video" else "Photo"
                    temp = f"tmp_{user_id}_{a_idx}_{item_idx}{url_ext(url, kind)}"

                    await update_panel(
                        client, user_id,
                        progress_text(
                            f"⬇️ **Downloading {label}:** `{item_idx}/{len(items)}`",
                            f"🔗 `{safe(url[:50])}...`\n"
                        ),
                        cancel_kb
                    )

                    async def progress(downloaded, total):
                        pct = f" ({downloaded * 100 // total}%)" if total else ""
                        size_txt = f"{downloaded // (1024 * 1024)} MB" + (f" / {total // (1024 * 1024)} MB" if total else "")
                        await update_panel(
                            client, user_id,
                            progress_text(
                                f"⬇️ **Downloading {label}:** `{item_idx}/{len(items)}`",
                                f"📦 `{size_txt}{pct}`\n"
                            ),
                            cancel_kb
                        )

                    ok, err = await download_file_with_details(url, temp, state, progress, MAX_FILE_BYTES)
                    if not ok:
                        errors.append(f"• [Album {a_idx} {label} #{item_idx} Download Failed]\n  URL: {url[:45]}...\n  Reason: {err}")
                        a_fail += 1
                        if os.path.exists(temp):
                            os.remove(temp)
                        continue

                    size = os.path.getsize(temp)
                    batch.append({"path": temp, "kind": kind, "url": url, "size": size})
                    batch_bytes += size

                    if len(batch) >= MAX_ALBUM_BATCH or batch_bytes >= MAX_BATCH_BYTES:
                        part += 1
                        to_send = batch
                        batch = []
                        batch_bytes = 0
                        v, p, errs = await flush(to_send, part)
                        a_up_v += v; a_up_p += p; up_v += v; up_p += p
                        a_fail += len(errs); errors.extend(errs)
                        await cancellable_sleep(2, state)

                if batch:
                    part += 1
                    to_send = batch
                    batch = []
                    v, p, errs = await flush(to_send, part)
                    a_up_v += v; a_up_p += p; up_v += v; up_p += p
                    a_fail += len(errs); errors.extend(errs)
            finally:
                for f in batch:
                    if os.path.exists(f["path"]):
                        os.remove(f["path"])

            # অ্যালবাম সম্পূর্ণ হলে চ্যানেলে ইংরেজিতে রিপোর্ট
            report = (
                f"✅ <b>Album {a_idx}/{total_albums} completed</b>\n"
                f"📁 {htmllib.escape(album['title'][:200])}\n"
                f"🎬 Videos: {a_up_v + prev_v}/{v_found}\n"
                f"🖼️ Photos: {a_up_p + prev_p}/{p_found}\n"
                f"📦 Total files in album: {len(all_items)}"
            )
            if a_fail:
                report += f"\n⚠️ Failed: {a_fail}"
            await send_channel_text(report)
            albums_done += 1
            await cancellable_sleep(2, state)

    except ScanCancelled:
        cancelled = True
    except Exception as fatal_e:
        fatal = True
        err_trace = traceback.format_exc()
        await update_panel(
            client, user_id,
            f"🚨 **CRITICAL BOT FAILURE (UNHANDLED EXCEPTION)**\n\n"
            f"**Exception Type:** `{type(fatal_e).__name__}`\n"
            f"**Error Message:** `{safe(str(fatal_e))[:300]}`\n\n"
            f"**Full Traceback (Where it failed):**\n```python\n{err_trace[-1200:]}\n```",
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
        user_sessions.setdefault(user_id, {})["state"] = "IDLE"

    if fatal:
        return

    head = "⛔ **SCAN STOPPED**" if cancelled else "🏁 **SCAN COMPLETED**"
    summary_text = (
        f"{head}\n\n"
        f"⚡ **Engine Used:** `{uploader_mode}`\n"
        f"📌 **Page Title:** `{safe(title)[:45]}`\n"
        f"🏷️ **Section Tag:** `{hashtag}`\n"
        f"📁 **Albums Completed:** `{albums_done}/{total_albums}`\n"
        f"🎬 **Videos Uploaded:** `{up_v}`\n"
        f"🖼️ **Photos Uploaded:** `{up_p}`\n"
        f"⏭️ **Skipped (already uploaded):** `{skipped}`\n"
        f"❌ **Total Failed:** `{len(errors)}`\n"
    )
    if cancelled:
        summary_text += "\n💾 Progress is saved. Tap **Scan Website** again to resume - uploaded files are skipped automatically."
    if errors:
        joined_errors = "\n".join(errors[:8])
        if len(errors) > 8:
            joined_errors += f"\n...and {len(errors) - 8} more errors."
        summary_text += f"\n\n⚠️ **DETAILED ERROR LOG (ENGLISH):**\n```text\n{joined_errors}\n```"
    elif not cancelled:
        summary_text += "\n✅ **Status:** `All albums processed with 0 errors!`"

    await update_panel(client, user_id, summary_text, end_kb)

# ================= Render Web Service Port Binding =================
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Telethon + Pyrogram Single-Message Scraper Bot is Running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

async def main():
    global backup_event
    await bot.start()
    try:
        status = await restore_db_from_telegram()
    except Exception as e:
        status = f"Restore error: {type(e).__name__}: {e}"
    print(f"💾 {status}")

    backup_event = asyncio.Event()
    worker = asyncio.create_task(backup_worker())
    print("🤖 Bot is running with Telegram DB backup enabled.")

    await idle()  # SIGTERM (Render deploy/restart) এলে এখান থেকে বের হবে

    worker.cancel()
    if backup_dirty:
        print("💾 Final backup before shutdown...")
        await do_backup()
    await bot.stop()

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    print(f"🤖 Starting Hybrid Scraper Bot... (Telethon available: {TELETHON_AVAILABLE})")
    asyncio.get_event_loop().run_until_complete(main())
