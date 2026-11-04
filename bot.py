import os
import re
import sqlite3
import asyncio
import traceback
import aiohttp
from datetime import datetime, timezone
from threading import Thread
from flask import Flask
from bs4 import BeautifulSoup

# ================= Python 3.14 Event Loop Fix =================
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, idle
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
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

async def register_site(client, user_id, html_content, hashtag):
    """HTML সেভ করে মেনু বাটনে সাইটের নাম যোগ করে (তখনই স্ক্যান করে না)"""
    title, videos, images = extract_media_from_html(html_content)
    name = make_site_name(title)
    site_id = save_site(user_id, name, hashtag, html_content)

    user_sessions.setdefault(user_id, {})["state"] = "IDLE"
    user_sessions[user_id]["buffer"] = []

    await reset_panel(client, user_id)
    await refresh_menu(client, user_id)
    await update_panel(
        client, user_id,
        f"✅ **Website Saved**\n\n"
        f"🌐 **Name:** `{name}`\n"
        f"🏷️ **Hashtag:** `{hashtag}`\n"
        f"🎬 **Videos Found:** `{len(videos)}`\n"
        f"🖼️ **Images Found:** `{len(images)}`\n\n"
        f"মেসেজ বক্সের পাশের মেনু বাটনে সাইটের নাম যোগ হয়েছে। এখনই স্ক্যান করতে নিচের বাটনে ক্লিক করুন:",
        get_site_keyboard(site_id)
    )

async def show_site_panel(client, user_id, site):
    site_id, name, hashtag, html = site
    _t, videos, images = extract_media_from_html(html)
    user_sessions.setdefault(user_id, {})["state"] = "IDLE"
    await reset_panel(client, user_id)
    await update_panel(
        client, user_id,
        f"🌐 **{name}**\n\n"
        f"🏷️ **Hashtag:** `{hashtag}`\n"
        f"🎬 **Videos Found:** `{len(videos)}`\n"
        f"🖼️ **Images Found:** `{len(images)}`\n\n"
        f"ওয়েবসাইট থেকে ভিডিও ও ছবি স্ক্যান করে আপলোড করতে নিচের বাটনে ক্লিক করুন:",
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
            user_sessions[user_id]["state"] = "PROCESSING"
            await cq.answer("Scanning website...")
            await process_and_upload(client, user_id, html, hashtag)
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

# ================= স্ক্যান, ডাউনলোড ও আপলোড লজিক =================
def extract_media_from_html(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    page_title = soup.title.string.strip() if soup.title and soup.title.string else "Website Media"

    videos = set()
    images = set()

    raw_mp4s = re.findall(r'https?://[^\s"\'<>]+?\.mp4(?:\?[^\s"\'<>]*)?', html_content, re.IGNORECASE)
    for v in raw_mp4s:
        videos.add(v)

    for tag in soup.find_all(["video", "source", "a", "img"]):
        url = tag.get("src") or tag.get("data-src") or tag.get("href")
        if not url or not url.startswith("http"):
            continue
        low_url = url.lower()
        if ".mp4" in low_url:
            videos.add(url)
        elif any(ext in low_url for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            if not any(ignore in low_url for ignore in ["logo", "icon", "avatar", "banner", "ads", "pixel"]):
                images.add(url)

    return page_title, list(videos), list(images)

async def download_file_with_details(url, dest_path):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        timeout = aiohttp.ClientTimeout(total=1200)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False, f"HTTP Error {resp.status} ({resp.reason}) - Server blocked or file removed."

                with open(dest_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        f.write(chunk)

                if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
                    return False, "Downloaded file is 0 bytes (Empty response)."

                return True, "OK"

    except asyncio.TimeoutError:
        return False, "Connection Timeout: Server took too long to respond (>20 mins)."
    except aiohttp.ClientConnectorError as e:
        return False, f"DNS/Connection Error: Cannot connect to host ({str(e)})."
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)}"

async def upload_to_channel(file_path, caption, is_video=True):
    """Telethon Session থাকলে সেটি দিয়ে আপলোড করবে, না থাকলে বট দিয়ে আপলোড করবে"""
    t_ok, t_err = await ensure_telethon_connected()
    if t_ok:
        await telethon_client.send_file(
            STORAGE_CHANNEL_ID,
            file_path,
            caption=caption,
            supports_streaming=is_video
        )
    else:
        if is_video:
            await bot.send_video(
                chat_id=STORAGE_CHANNEL_ID,
                video=file_path,
                caption=caption,
                supports_streaming=True
            )
        else:
            await bot.send_photo(
                chat_id=STORAGE_CHANNEL_ID,
                photo=file_path,
                caption=caption
            )

async def process_and_upload(client, user_id, html_content, hashtag):
    try:
        await update_panel(client, user_id, "🔍 **Scanning Website...**\nExtracting video and image links...")
        title, videos, images = extract_media_from_html(html_content)

        if not videos and not images:
            await update_panel(
                client, user_id,
                "⚠️ **NO MEDIA FOUND IN HTML**\n\n"
                "**Diagnostic Report (English):**\n"
                f"• `Total HTML Length:` `{len(html_content)} chars`\n"
                "• `Direct .mp4 Links:` `0 found`\n"
                "• `Valid Image Links:` `0 found`\n"
                "• **Possible Cause:** The website might be loading videos dynamically via encrypted JavaScript (m3u8/blob) or requires login cookies.",
                get_home_keyboard()
            )
            user_sessions.setdefault(user_id, {})["state"] = "IDLE"
            return

        new_videos = [v for v in videos if not is_duplicate(v)]
        new_images = [img for img in images if not is_duplicate(img)]

        if not new_videos and not new_images:
            await update_panel(
                client, user_id,
                "ℹ️ **ALL MEDIA ALREADY UPLOADED (DUPLICATES)**\n\n"
                f"• **Found Videos:** `{len(videos)}` (All skipped as duplicates)\n"
                f"• **Found Images:** `{len(images)}` (All skipped as duplicates)\n"
                f"• **Database Status:** Already exists in `uploaded_media` table.",
                get_home_keyboard()
            )
            user_sessions.setdefault(user_id, {})["state"] = "IDLE"
            return

        t_ok, _ = await ensure_telethon_connected()
        uploader_mode = "Telethon User Session" if t_ok else "Pyrogram Bot Token"

        uploaded_vids = 0
        uploaded_imgs = 0
        error_logs = []

        # ১. ভিডিও আপলোড লুপ
        for idx, v_url in enumerate(new_videos, start=1):
            temp_vid = f"temp_vid_{user_id}_{idx}.mp4"
            await update_panel(
                client, user_id,
                f"⏳ **Processing Media (Single Panel View)**\n\n"
                f"⚡ **Uploader Engine:** `{uploader_mode}`\n"
                f"📌 **Title:** `{title[:40]}`\n"
                f"🏷️ **Hashtag:** `{hashtag}`\n"
                f"⬇️ **Action:** `Downloading Video ({idx}/{len(new_videos)})`\n"
                f"🔗 **URL:** `{v_url[:50]}...`\n"
                f"✅ **Uploaded:** `{uploaded_vids} Videos` | ❌ **Failed:** `{len(error_logs)}`"
            )

            ok, err_reason = await download_file_with_details(v_url, temp_vid)
            if not ok:
                error_logs.append(f"• [Video #{idx} Download Failed]\n  URL: {v_url[:45]}...\n  Reason: {err_reason}")
                if os.path.exists(temp_vid):
                    os.remove(temp_vid)
                continue

            try:
                await update_panel(
                    client, user_id,
                    f"⏳ **Processing Media (Single Panel View)**\n\n"
                    f"⚡ **Uploader Engine:** `{uploader_mode}`\n"
                    f"⬆️ **Action:** `Uploading Video to Storage Channel ({idx}/{len(new_videos)})`\n"
                    f"📦 **File Size:** `{os.path.getsize(temp_vid) // (1024*1024)} MB`\n"
                    f"✅ **Uploaded:** `{uploaded_vids} Videos` | ❌ **Failed:** `{len(error_logs)}`"
                )
                caption = f"🎬 **{title}**\n\n{hashtag} #website"
                await upload_to_channel(temp_vid, caption, is_video=True)
                mark_as_uploaded(v_url, hashtag)
                uploaded_vids += 1
                await asyncio.sleep(3)

            except (FloodWait, FloodWaitError) as fw:
                wait_sec = getattr(fw, "value", getattr(fw, "seconds", 10))
                error_logs.append(f"• [Video #{idx} FloodWait]\n  Waited {wait_sec}s, file skipped (scan again to retry).")
                await asyncio.sleep(wait_sec)
            except (RPCError, TelethonRPCError) as rpc_err:
                error_logs.append(
                    f"• [Video #{idx} Telegram API Error]\n"
                    f"  Type: {type(rpc_err).__name__}\n"
                    f"  Reason: {str(rpc_err)} (Check STORAGE_CHANNEL_ID & Permissions)"
                )
            except Exception as up_err:
                error_logs.append(f"• [Video #{idx} Upload Error]\n  Reason: {type(up_err).__name__} - {str(up_err)}")
            finally:
                if os.path.exists(temp_vid):
                    os.remove(temp_vid)

        # ২. ছবি আপলোড লুপ
        for idx, img_url in enumerate(new_images, start=1):
            temp_img = f"temp_img_{user_id}_{idx}.jpg"
            await update_panel(
                client, user_id,
                f"⏳ **Processing Images via {uploader_mode}...**\n\n"
                f"🖼️ **Progress:** `{idx}/{len(new_images)}`\n"
                f"✅ **Videos Done:** `{uploaded_vids}` | **Images Done:** `{uploaded_imgs}`\n"
                f"❌ **Errors Logged:** `{len(error_logs)}`"
            )

            ok, err_reason = await download_file_with_details(img_url, temp_img)
            if not ok:
                error_logs.append(f"• [Image #{idx} Download Failed]\n  URL: {img_url[:45]}...\n  Reason: {err_reason}")
                if os.path.exists(temp_img):
                    os.remove(temp_img)
                continue

            try:
                caption = f"🖼️ **{title}**\n\n{hashtag} #website"
                await upload_to_channel(temp_img, caption, is_video=False)
                mark_as_uploaded(img_url, hashtag)
                uploaded_imgs += 1
                await asyncio.sleep(2)
            except Exception as up_err:
                error_logs.append(f"• [Image #{idx} Upload Error]\n  Reason: {type(up_err).__name__} - {str(up_err)}")
            finally:
                if os.path.exists(temp_img):
                    os.remove(temp_img)

        # ৩. চূড়ান্ত রিপোর্ট
        summary_text = (
            f"🏁 **TASK COMPLETED REPORT**\n\n"
            f"⚡ **Engine Used:** `{uploader_mode}`\n"
            f"📌 **Page Title:** `{title[:45]}`\n"
            f"🏷️ **Section Tag:** `{hashtag}`\n"
            f"🎬 **Videos Uploaded:** `{uploaded_vids}/{len(new_videos)}`\n"
            f"🖼️ **Images Uploaded:** `{uploaded_imgs}/{len(new_images)}`\n"
            f"❌ **Total Failed:** `{len(error_logs)}`\n"
        )

        if error_logs:
            joined_errors = "\n".join(error_logs[:8])
            if len(error_logs) > 8:
                joined_errors += f"\n...and {len(error_logs) - 8} more errors."
            summary_text += f"\n⚠️ **DETAILED ERROR LOG (ENGLISH):**\n```text\n{joined_errors}\n```"
        else:
            summary_text += "\n✅ **Status:** `All media processed smoothly with 0 errors!`"

        user_sessions.setdefault(user_id, {})["state"] = "IDLE"
        await update_panel(client, user_id, summary_text, get_home_keyboard())

    except Exception as fatal_e:
        err_trace = traceback.format_exc()
        user_sessions.setdefault(user_id, {})["state"] = "IDLE"
        await update_panel(
            client, user_id,
            f"🚨 **CRITICAL BOT FAILURE (UNHANDLED EXCEPTION)**\n\n"
            f"**Exception Type:** `{type(fatal_e).__name__}`\n"
            f"**Error Message:** `{str(fatal_e)}`\n\n"
            f"**Full Traceback (Where it failed):**\n```python\n{err_trace[-1200:]}\n```",
            get_home_keyboard()
        )

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
