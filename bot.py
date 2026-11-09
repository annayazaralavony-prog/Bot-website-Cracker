import io
import os
import re
import sys
import glob
import shutil
import time
import sqlite3
import asyncio
import aiohttp
import traceback
import importlib.util
import html as htmllib
from urllib.parse import urlparse, unquote
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
    try:
        from telethon.errors import AuthKeyDuplicatedError
    except ImportError:
        class AuthKeyDuplicatedError(Exception):
            pass
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False
    TelegramClient = None
    StringSession = None

    class FloodWaitError(Exception):
        pass

    class TelethonRPCError(Exception):
        pass

    class AuthKeyDuplicatedError(Exception):
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
DB_PATH = os.environ.get("DB_PATH", "scraped_data.db")   # Railway Volume থাকলে যেমন /data/scraped_data.db
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)
# BACKUP_CHANNEL_ID না দিলে স্টোরেজ চ্যানেলেই ব্যাকআপ যাবে (বট ওখানে অ্যাডমিন আছে)
BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID") or STORAGE_CHANNEL_ID)
BACKUP_TAG = "#DB_BACKUP"
SITE_PREFIX = "🌐 "

MAX_ALBUM_BATCH = 10
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_MB", "2000")) * 1024 * 1024
MAX_BATCH_BYTES = int(os.environ.get("MAX_BATCH_MB", "2000")) * 1024 * 1024

# ========== নতুন সেটিংস ==========
MAX_ALBUMS_PER_SCAN = 20          # প্রতি স্ক্যানে সর্বোচ্চ কয়টা অ্যালবাম
DEFAULT_HASHTAG = "#newweb"       # ডিফল্ট হ্যাশট্যাগ
# অ্যালবামে শুধু প্রথম ফাইলে ক্যাপশন (টাইটেল + নিচে হ্যাশট্যাগ) থাকবে, যাতে টেলিগ্রাম অ্যালবামের নিচে ক্যাপশন দেখায়
REST_CAPTION_HASHTAG = os.environ.get("REST_CAPTION_HASHTAG", "0") == "1"
# প্রতি অ্যালবাম শেষে চ্যানেলে আলাদা "completed" রিপোর্ট মেসেজ (ডিফল্ট বন্ধ)
ALBUM_REPORT_IN_CHANNEL = os.environ.get("ALBUM_REPORT_IN_CHANNEL", "0") == "1"
ORDERED_ALBUMS = os.environ.get("ORDERED_ALBUMS", "1") == "1"
RESOLVE_PARALLEL = max(1, int(os.environ.get("RESOLVE_PARALLEL", "3")))   # একসাথে কয়টা অ্যালবামের পেজ খুলবে

# ========== Send Link ফিচারের সেটিংস ==========
MAX_LINKS_QUEUE = 10              # একসাথে সর্বোচ্চ কয়টা লিংক কিউতে থাকতে পারবে (অ্যালবামের ফাইল সংখ্যার কোনো লিমিট নেই)
# Send Link-এ একটি অ্যালবামের অংশগুলো (১০ ফাইলের গ্রুপ) একাধিক আইডিতে ভাগ হলে চ্যানেলে ক্রম ঠিক রাখবে কিনা
# 1 = ক্রমানুসারে আপলোড (ডাউনলোড সমান্তরালে হয়), 0 = যে অংশ আগে তৈরি সেটা আগে যাবে (দ্রুততম)
ORDERED_PARTS = os.environ.get("ORDERED_PARTS", "1") == "1"

# ========== নতুন: ভূমিকা-ভিত্তিক আপলোড (Telethon x3) ==========
# 1 = প্রথম আইডি শুধু ছবি ডাউনলোড/আপলোড করবে, বাকি আইডিগুলো শুধু ভিডিও (ভিডিও সমান ভাগে ভাগ হবে)।
#     নিজের কাজ শেষ হলে বসে না থেকে অন্যজনের বাকি অংশ নিয়ে সাহায্য করবে।
# 0 = আগের মতো মিশ্র অংশে ভাগ (যে আইডি আগে ফ্রি সে পরের অংশ নেবে)
ROLE_SPLIT = os.environ.get("ROLE_SPLIT", "1") == "1"
# 0 = Telethon সেশন থাকলে শুধু Telethon দিয়েই আপলোড (Pyrogram ইউজার আইডি আপলোডারে ধরা হবে না)
# 1 = Pyrogram ইউজার আইডিও আপলোডারের তালিকায় যুক্ত হবে
USE_PYRO_UPLOADERS = os.environ.get("USE_PYRO_UPLOADERS", "0") == "1"

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

# ---- Telethon সেশন স্টেট ট্র্যাকিং ----
dead_telethon = {}      # id(client) -> কারণ (AuthKeyDuplicated ইত্যাদি হলে এখানে যাবে)
connect_locks = {}      # id(client) -> asyncio.Lock (একসাথে দুইবার connect ঠেকাতে)
primed_clients = set()  # যেসব ক্লায়েন্টের চ্যানেল entity লোড হয়েছে
backup_primed_pyro = set()      # যেসব Pyrogram ইউজার ক্লায়েন্ট ব্যাকআপ চ্যানেল চিনেছে
backup_primed_telethon = set()  # যেসব Telethon ক্লায়েন্ট ব্যাকআপ চ্যানেল চিনেছে

def mark_telethon_dead(c, reason):
    if id(c) not in dead_telethon:
        dead_telethon[id(c)] = reason
        idx = telethon_clients.index(c) + 1 if c in telethon_clients else "?"
        print(f"🔴 Telethon session #{idx} মারা গেছে: {reason}")

def get_telethon():
    """প্রথম জীবিত Telethon ক্লায়েন্ট ফেরত দেয়"""
    for c in telethon_clients:
        if id(c) not in dead_telethon:
            return c
    return None

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
startup_db_status = "n/a"

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

async def _prime_pyro_backup_channel(pc):
    """Pyrogram ইউজার সেশনকে ব্যাকআপ চ্যানেল চিনিয়ে নেয় (in_memory সেশনে cache থাকে না)"""
    if id(pc) in backup_primed_pyro:
        return True
    try:
        await pc.get_chat(BACKUP_CHANNEL_ID)
        backup_primed_pyro.add(id(pc))
        return True
    except Exception:
        pass
    try:
        async for _d in pc.get_dialogs():
            pass
        await pc.get_chat(BACKUP_CHANNEL_ID)
        backup_primed_pyro.add(id(pc))
        return True
    except Exception as e:
        print(f"⚠️ Pyrogram user ব্যাকআপ চ্যানেল চিনতে পারেনি: {type(e).__name__}: {e}")
        return False

async def _prime_telethon_backup_channel(tc):
    """Telethon সেশনকে ব্যাকআপ চ্যানেল চিনিয়ে নেয়"""
    if id(tc) in backup_primed_telethon:
        return True
    try:
        await tc.get_entity(BACKUP_CHANNEL_ID)
        backup_primed_telethon.add(id(tc))
        return True
    except AuthKeyDuplicatedError:
        raise
    except Exception:
        pass
    try:
        async for _d in tc.iter_dialogs():
            pass
        await tc.get_entity(BACKUP_CHANNEL_ID)
        backup_primed_telethon.add(id(tc))
        return True
    except AuthKeyDuplicatedError:
        raise
    except Exception as e:
        print(f"⚠️ Telethon ব্যাকআপ চ্যানেল চিনতে পারেনি: {type(e).__name__}: {e}")
        return False

async def do_backup():
    global backup_dirty, last_backup_msg_id
    snap = None
    backup_dirty = False
    used_telethon = False
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
        cap = f"{BACKUP_TAG}\n🕒 {stamp}\n🎬 Media records: {media_n}\n📄 Saved HTML: {html_n}"

        new_id = None
        sender_kind = None
        sender_obj = None
        errs = []

        # ১) আগে বট টোকেন দিয়ে চেষ্টা
        try:
            msg = await bot.send_document(
                BACKUP_CHANNEL_ID,
                snap,
                file_name="scraped_data_backup.db",
                caption=cap
            )
            new_id = msg.id
            sender_kind = "bot"
            sender_obj = bot
        except Exception as bot_err:
            errs.append(f"Bot: {type(bot_err).__name__}: {bot_err}")
            print(f"⚠️ Bot backup failed ({bot_err}) - ইউজার আইডি দিয়ে চেষ্টা করা হচ্ছে...")

        # ২) বট ব্যর্থ হলে Pyrogram ইউজার আইডি দিয়ে
        if new_id is None:
            for _n, pc in pyro_ready:
                try:
                    if not await _prime_pyro_backup_channel(pc):
                        errs.append(f"{_n}: চ্যানেল চেনা যায়নি")
                        continue
                    msg = await pc.send_document(
                        BACKUP_CHANNEL_ID,
                        snap,
                        file_name="scraped_data_backup.db",
                        caption=cap
                    )
                    new_id = msg.id
                    sender_kind = "pyro"
                    sender_obj = pc
                    break
                except Exception as e:
                    errs.append(f"{_n}: {type(e).__name__}: {e}")
                    print(f"⚠️ Pyrogram user backup failed: {e}")

        # ৩) তাতেও না হলে Telethon দিয়ে
        if new_id is None and TELETHON_AVAILABLE:
            tc = None
            try:
                t_ok, t_info = await ensure_telethon_connected()
                tc = get_telethon()
                if t_ok and tc is not None:
                    used_telethon = True
                    if await _prime_telethon_backup_channel(tc):
                        msg = await tc.send_file(
                            BACKUP_CHANNEL_ID, snap, caption=cap, force_document=True
                        )
                        new_id = msg.id
                        sender_kind = "telethon"
                        sender_obj = tc
                    else:
                        errs.append("Telethon: চ্যানেল চেনা যায়নি")
                else:
                    errs.append(f"Telethon: {t_info}")
            except AuthKeyDuplicatedError:
                if tc is not None:
                    mark_telethon_dead(tc, "AuthKeyDuplicatedError during backup")
                errs.append("Telethon: AuthKeyDuplicatedError")
            except Exception as e:
                errs.append(f"Telethon: {type(e).__name__}: {e}")
                print(f"⚠️ Telethon backup failed: {e}")

        if new_id is None:
            raise RuntimeError("কোনো আইডি দিয়েই ব্যাকআপ পাঠানো যায়নি | " + " | ".join(errs)[:350])

        # পিন করা
        try:
            if sender_kind == "bot":
                await bot.pin_chat_message(BACKUP_CHANNEL_ID, new_id, disable_notification=True)
            elif sender_kind == "pyro":
                await sender_obj.pin_chat_message(BACKUP_CHANNEL_ID, new_id, disable_notification=True)
            elif sender_kind == "telethon":
                await sender_obj.pin_message(BACKUP_CHANNEL_ID, new_id, notify=False)
        except Exception as pin_err:
            print(f"⚠️ Backup pin failed: {pin_err}")

        # আগের ব্যাকআপ মেসেজ মুছে ফেলা
        old_id = last_backup_msg_id
        last_backup_msg_id = new_id
        if old_id and old_id != new_id:
            try:
                await sender_obj.delete_messages(BACKUP_CHANNEL_ID, old_id)
            except Exception:
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
        if used_telethon:
            try:
                await release_telethon()
            except Exception:
                pass

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
        tc = get_telethon()
        if t_ok and tc is not None:
            try:
                await _prime_telethon_backup_channel(tc)
                async for m in tc.iter_messages(BACKUP_CHANNEL_ID, search=BACKUP_TAG, limit=50):
                    if m.document:
                        path = await tc.download_media(m, "restore_tmp.db")
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
    shutil.move(path, DB_PATH)
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
    """অ্যালবামকে ইউনিকভাবে চিনতে (v2: আগের ভুল থাম্বনেইল-স্ক্যানের রেকর্ড এড়াতে)"""
    base = f"{site_key}|v2|{(title or '').strip().lower()[:120]}"
    if first_url:
        base += f"|{first_url}"
    return base

def album_key_of(site_key, album):
    url = album.get("url")
    if url:
        return f"{site_key}|v2|{url}"
    items = album.get("items") or []
    first_url = items[0][1] if items else ""
    return make_album_key(site_key, album.get("title"), first_url)

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
        if items or a.get("needs_resolve"):
            entry = dict(a)   # url, needs_resolve ইত্যাদি এক্সট্রা ফিল্ড রেখে দিই
            entry["title"] = a.get("title") or f"Album {idx}"
            entry["items"] = items
            clean_albums.append(entry)
    return page_title or site["name"], clean_albums

discover_sites()

# ================= ইউজার সেশন ও বাটন =================
user_sessions = {}
active_scans = {}
link_jobs = {}          # user_id -> Send Link কিউ জব

def link_job_running(user_id):
    job = link_jobs.get(user_id)
    return bool(job and not job.get("closed"))

def any_link_running():
    return any(link_job_running(u) for u in list(link_jobs))

class ScanCancelled(Exception):
    pass

def check_cancel(state):
    if state and state.get("cancel"):
        raise ScanCancelled()

async def cancellable_sleep(seconds, state):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_cancel(state)
        await asyncio.sleep(max(0.01, min(0.5, end - time.monotonic())))

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
        [InlineKeyboardButton("🔗 Send Link", callback_data=f"site_link_{site_key}")],
        [InlineKeyboardButton("📄 Send New HTML for this Site", callback_data=f"site_html_{site_key}")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu_home")]
    ])

def get_cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛔ Cancel Scan", callback_data="action_cancel_scan")]
    ])

def get_link_cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛔ Cancel Links", callback_data="action_cancel_links")]
    ])

def build_sites_reply_keyboard():
    if not loaded_sites:
        return None
    rows = [[KeyboardButton(f"{SITE_PREFIX}{s['name']}")] for s in loaded_sites.values()]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, placeholder="Select a website module...")

async def safe_answer(cq, text=None):
    try:
        await cq.answer(text) if text else await cq.answer()
    except Exception:
        pass

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

# ================= Telethon কানেকশন ম্যানেজমেন্ট =================
async def prime_entity(c):
    """নতুন StringSession-এ চ্যানেলের entity cache থাকে না, তাই আগে লোড করে নিই"""
    try:
        await c.get_entity(STORAGE_CHANNEL_ID)
        return
    except AuthKeyDuplicatedError:
        raise
    except Exception:
        pass
    try:
        async for _d in c.iter_dialogs():
            pass
        await c.get_entity(STORAGE_CHANNEL_ID)
    except AuthKeyDuplicatedError:
        raise
    except Exception as e:
        print(f"⚠️ Telethon: স্টোরেজ চ্যানেল entity পাওয়া যায়নি: {type(e).__name__}: {e}")

async def ensure_telethon_connected(tclient=None):
    if not TELETHON_AVAILABLE:
        return False, "Telethon not installed."
    target = tclient if tclient is not None else get_telethon()
    if not target:
        if telethon_clients:
            return False, "সব Telethon session অকেজো (AuthKeyDuplicated) - নতুন session দিন।"
        return False, "TELETHON_SESSION_STRING missing."
    if id(target) in dead_telethon:
        return False, dead_telethon[id(target)]

    # ইতিমধ্যে ভেরিফাইড ও কানেক্টেড হলে আর নেটওয়ার্ক কল নয়
    if target.is_connected() and id(target) in primed_clients:
        return True, "Connected"

    lock = connect_locks.setdefault(id(target), asyncio.Lock())
    async with lock:
        try:
            if not target.is_connected():
                await target.connect()
            if not await target.is_user_authorized():
                mark_telethon_dead(target, "Session String invalid/expired.")
                return False, "Telethon Session String invalid/expired."
            if id(target) not in primed_clients:
                await prime_entity(target)
                primed_clients.add(id(target))
            return True, "Connected"
        except AuthKeyDuplicatedError:
            msg = "AuthKeyDuplicatedError: একই session একসাথে দুই IP-তে ব্যবহার হয়েছে। নতুন session বানান এবং অন্য সব জায়গায় বন্ধ করুন।"
            mark_telethon_dead(target, msg)
            try:
                await target.disconnect()
            except Exception:
                pass
            return False, msg
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)}"

async def release_telethon():
    """স্ক্যান/লিংক কিউ না চললে Telethon কানেকশন বন্ধ রাখি (Railway overlap-এ সেশন ক্লাশ কমাতে)"""
    if active_scans or any_link_running():
        return
    for c in telethon_clients:
        try:
            if c.is_connected():
                await c.disconnect()
        except Exception:
            pass

def safe(text):
    return str(text).replace("`", "'")

def album_totals(albums):
    v = sum(1 for a in albums for k, _ in a["items"] if k == "video")
    p = sum(1 for a in albums for k, _ in a["items"] if k == "image")
    return v, p

def describe_albums(albums, limit=10):
    lines = []
    for i, a in enumerate(albums[:limit], start=1):
        short = safe((a["title"] or "").split("\n")[0])[:32]
        if a.get("needs_resolve"):
            lines.append(f"{i}. {short}")
        else:
            v = sum(1 for k, _ in a["items"] if k == "video")
            p = len(a["items"]) - v
            lines.append(f"{i}. {short} - {v}V {p}P")
    if len(albums) > limit:
        lines.append(f"...and {len(albums) - limit} more albums")
    return "\n".join(lines)

def albums_panel_text(albums):
    if any(a.get("needs_resolve") for a in albums):
        text = (
            f"📁 **Albums/Sections:** `{len(albums)}`\n"
            f"🔗 ভেতরের ফটো/ভিডিও স্ক্যানের সময় প্রতিটা অ্যালবামের পেজ খুলে আনা হবে।\n"
        )
        if albums:
            text += f"\n```text\n{describe_albums(albums)}\n```\n"
        return text
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
    auto_html = False
    fetch_err_txt = ""
    if override_html is None:
        fetch_fn = getattr(site["module"], "fetch_html_content", None)
        if fetch_fn:
            try:
                fetched = await fetch_fn()
                if fetched and fetched.strip():
                    override_html = fetched
                    auto_html = True
            except Exception as fe:
                fetch_err_txt = f"\n⚠️ **Auto-fetch Error:** `{safe(fe)[:150]}`\n"
        if override_html is None:
            override_html = db_get_site_html(site["key"])
    try:
        _title, albums = get_site_albums(site, override_html)
        err_txt = fetch_err_txt
    except Exception as e:
        albums = []
        err_txt = fetch_err_txt + f"\n⚠️ **Parser Error:** `{safe(e)}`\n"

    sess = user_sessions.setdefault(user_id, {})
    sess["state"] = "IDLE"
    sess["active_site_key"] = site["key"]
    if override_html is not None and not auto_html:
        sess.setdefault("custom_html", {})[site["key"]] = override_html

    has_html = override_html is not None or bool(getattr(site["module"], "HTML_CONTENT", "").strip())
    if auto_html:
        source_note = "📄 **HTML Source:** `Auto-fetch from website`"
    elif has_html:
        source_note = "📄 **HTML Source:** `Saved / Ready in file`"
    else:
        source_note = "⚠️ **HTML Source:** `Empty (Set SITE_LIST_URLS or send .html file)`"

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
        f"স্ক্যান শুরু করতে, অ্যালবামের লিংক পাঠাতে বা নতুন HTML ফাইল দিতে নিচের বাটনে ক্লিক করুন:",
        get_site_keyboard(site["key"])
    )

def engine_status_text(t_ok, t_status):
    status = "🟢 Active (Telethon Session)" if t_ok else f"🔴 Inactive ({t_status})"
    if t_ok:
        alive = sum(1 for c in telethon_clients if id(c) not in dead_telethon)
        if len(telethon_clients) > 1:
            status = f"🟢 Active (Telethon x{alive}/{len(telethon_clients)} Accounts)"
    return status

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
    engine_status = engine_status_text(t_ok, t_status)
    await release_telethon()
    sites_list = "\n".join(f"• `{s['name']}` (`{s['path']}`)" for s in loaded_sites.values()) or "• `No site files found in sites/ folder`"

    welcome_text = (
        "🤖 **Modular Website Scraper Dashboard**\n\n"
        f"⚡ **Telethon Engine:** `{engine_status}`\n"
        f"📦 **Storage Channel:** `{STORAGE_CHANNEL_ID}`\n"
        f"📌 **Albums per Scan:** `{MAX_ALBUMS_PER_SCAN}`\n"
        f"🏷️ **Default Hashtag:** `{DEFAULT_HASHTAG}`\n"
        f"💾 **Database:** `{safe(startup_db_status)[:120]}`\n"
        f"🗄️ **DB Path:** `{DB_PATH}`\n\n"
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
    text = f"✅ **BACKUP COMPLETED**\n• **Time:** `{info}`" if ok else f"❌ **BACKUP FAILED**\n• **Reason:** `{safe(info)[:600]}`"
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
                await safe_answer(cq, f"Reloaded {len(loaded_sites)} site files!")
            else:
                await safe_answer(cq)

            t_ok, t_status = await ensure_telethon_connected()
            engine_status = engine_status_text(t_ok, t_status)
            await release_telethon()
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
            await safe_answer(cq, "Syncing storage channel via Telethon...")
            t_ok, t_err = await ensure_telethon_connected()
            if not t_ok:
                await update_panel(client, user_id, f"❌ **TELETHON CONNECTION FAILED**\n\n`{t_err}`", get_home_keyboard())
                return

            await update_panel(client, user_id, "🔄 **Scanning Storage Channel via Telethon...**")
            count = 0
            try:
                tc = get_telethon()
                async for msg in tc.iter_messages(STORAGE_CHANNEL_ID, limit=500):
                    if msg.text:
                        for u in re.findall(r'https?://[^\s"\'<>]+', msg.text or ""):
                            if not is_duplicate(u):
                                mark_as_uploaded(u, DEFAULT_HASHTAG)
                                count += 1
            except AuthKeyDuplicatedError:
                mark_telethon_dead(get_telethon(), "AuthKeyDuplicatedError during sync")
                await update_panel(client, user_id, "❌ **Telethon session AuthKeyDuplicated**\nনতুন session বানিয়ে দিন।", get_home_keyboard())
                return
            finally:
                await release_telethon()
            await update_panel(client, user_id, f"✅ **Sync Completed**\n• New media marked: `{count}`", get_home_keyboard())

        elif data == "action_cancel_scan":
            state = active_scans.get(user_id)
            if state:
                state["cancel"] = True
                await safe_answer(cq, "Cancelling scan...")
            else:
                await safe_answer(cq, "No active scan.")

        elif data == "action_cancel_links":
            job = link_jobs.get(user_id)
            if job and not job.get("closed"):
                job["state"]["cancel"] = True
                await safe_answer(cq, "Cancelling link queue...")
            else:
                await safe_answer(cq, "No active link queue.")

        elif data.startswith("site_scan_"):
            site_key = data.replace("site_scan_", "", 1)
            site = loaded_sites.get(site_key)
            if not site:
                await safe_answer(cq, "Site not found!")
                return
            await safe_answer(cq, "Starting scan...")
            override = sess.get("custom_html", {}).get(site_key) or db_get_site_html(site_key)
            started = await start_scan(client, user_id, site, override)
            if not started:
                await update_panel(client, user_id, "⚠️ একটি স্ক্যান বা লিংক কিউ ইতিমধ্যে চলছে। আগে সেটা শেষ করুন অথবা ক্যানসেল করুন।", get_site_keyboard(site_key))

        elif data.startswith("site_link_"):
            site_key = data.replace("site_link_", "", 1)
            site = loaded_sites.get(site_key)
            if not site:
                await safe_answer(cq, "Site not found!")
                return
            if not hasattr(site["module"], "resolve_album"):
                await safe_answer(cq, "এই সাইট ফাইলে resolve_album() নেই!")
                return
            old_scan = active_scans.get(user_id)
            if old_scan and not old_scan["task"].done():
                await safe_answer(cq, "স্ক্যান চলছে, আগে শেষ করুন বা ক্যানসেল করুন।")
                return

            sess["state"] = "WAITING_LINK"
            sess["active_site_key"] = site_key
            await safe_answer(cq)

            if link_job_running(user_id):
                await refresh_link_panel(client, user_id, link_jobs[user_id], force=True)
                return

            await update_panel(
                client, user_id,
                f"🔗 **{safe(site['name'])}** — Send Link\n\n"
                f"• অ্যালবামের লিংক পাঠান (একসাথে একাধিক লিংকও পাঠানো যায়)\n"
                f"• সর্বোচ্চ **{MAX_LINKS_QUEUE}**টা লিংক একসাথে কিউতে থাকতে পারবে\n"
                f"• একটা শেষ হলে বট পরেরটা ধরবে\n"
                f"• অ্যালবামের সব ফটো/ভিডিও ক্যাপশন + `{DEFAULT_HASHTAG}` সহ স্টোরেজ চ্যানেলে যাবে\n"
                f"• অ্যালবাম শেষ হলে চ্যানেলে কমপ্লিট মেসেজ যাবে\n\n"
                f"এখন লিংক পাঠান 👇",
                InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu_home")]])
            )

        elif data.startswith("site_html_"):
            site_key = data.replace("site_html_", "", 1)
            site = loaded_sites.get(site_key)
            if not site:
                await safe_answer(cq, "Site not found!")
                return
            sess["state"] = "WAITING_HTML"
            sess["active_site_key"] = site_key
            await safe_answer(cq)
            await update_panel(
                client, user_id,
                f"📄 **{safe(site['name'])}** এর জন্য নতুন HTML পাঠান\n\n"
                f"• `.html` ফাইল আপলোড করুন অথবা\n"
                f"• HTML কোড সরাসরি পেস্ট করুন\n\n"
                f"পাঠানোর পর অটো সেভ হয়ে যাবে।",
                InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu_home")]])
            )
        else:
            await safe_answer(cq)

    except Exception as e:
        print(f"Callback error: {e}")
        traceback.print_exc()
        try:
            await update_panel(client, user_id, f"❌ **Error:** `{type(e).__name__}: {safe(e)}`", get_home_keyboard())
        except Exception:
            pass

@bot.on_message(filters.private & filters.text & ~filters.command(["start", "backup"]))
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

    # লিংক পাঠানো হলে (Send Link ফিচার)
    if sess.get("state") == "WAITING_LINK":
        site_key = sess.get("active_site_key")
        site = loaded_sites.get(site_key)
        links = extract_links(text)
        if not site:
            await message.reply("⚠️ সাইট পাওয়া যায়নি। /start দিয়ে আবার শুরু করুন।")
            return
        if not links:
            await message.reply("⚠️ কোনো বৈধ লিংক পাওয়া যায়নি। অ্যালবামের লিংক (https://...) পাঠান।")
            return

        result = await enqueue_links(client, user_id, site, links)
        status = result["status"]
        if status == "scan_running":
            await message.reply("⚠️ একটি স্ক্যান চলছে। আগে সেটা শেষ করুন অথবা ক্যানসেল করুন।")
            return
        if status == "cancelling":
            await message.reply("⚠️ লিংক কিউ বন্ধ হচ্ছে, একটু পরে আবার লিংক পাঠান।")
            return

        try:
            await message.delete()
        except Exception:
            pass

        job = result["job"]
        if not result["new_job"]:
            # চলমান জবে লিংক যোগ হয়েছে (বা বাদ পড়েছে) - প্যানেল রিফ্রেশ
            await refresh_link_panel(client, user_id, job, force=True)
        return

    # HTML পেস্ট করা হলে
    if sess.get("state") == "WAITING_HTML":
        site_key = sess.get("active_site_key")
        site = loaded_sites.get(site_key)
        looks_like_html = bool(re.search(r"<\s*(!doctype|html|body|div|a\s|img|video|source)", text, re.I))
        if site and text and looks_like_html:
            save_site_html(site_key, text)
            sess.setdefault("custom_html", {})[site_key] = text
            sess["state"] = "IDLE"
            try:
                await message.delete()
            except Exception:
                pass
            await show_site_panel(client, user_id, site, override_html=text)
            return
        else:
            await message.reply("⚠️ এটা HTML কোড মনে হচ্ছে না। সম্পূর্ণ HTML পেস্ট করুন বা `.html` ফাইল পাঠান।")

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

def looks_like_bad_download(path):
    """ডাউনলোড হওয়া ফাইল আসলে HTML/JSON এরর পেজ কিনা (MEDIA_EMPTY এড়াতে)"""
    try:
        with open(path, "rb") as f:
            head = f.read(64).lstrip().lower()
        return (
            head.startswith(b"<!doctype") or head.startswith(b"<html")
            or head.startswith(b"<?xml") or head.startswith(b'{"')
        )
    except Exception:
        return False

async def download_file_with_details(url, dest_path, site_mod, state, progress_cb=None, max_bytes=None):
    # সাইট মডিউলে custom_download থাকলে সেটা ব্যবহার করবে
    if hasattr(site_mod, "custom_download"):
        ok, err = await site_mod.custom_download(url, dest_path, state, progress_cb, max_bytes)
        if not ok and err == "__CANCELLED__":
            raise ScanCancelled()
        return ok, err

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
        first = f"<b>{htmllib.escape(clean_title[:200])}</b>\n\n{tag_html}"
    else:
        first = tag_html
    rest = tag_html if REST_CAPTION_HASHTAG else ""
    return first, rest, tag_html

async def _send_via_telethon(tclient, files, paths, first_cap, rest_cap):
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

async def _send_via_pyrogram(pc, files, first_cap, rest_cap):
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
            tclient = get_telethon()
    paths = [f["path"] for f in files]
    pc = uploader["client"] if (uploader and uploader.get("type") == "pyrogram") else bot

    if tclient is not None:
        try:
            await _send_via_telethon(tclient, files, paths, first_cap, rest_cap)
            return
        except AuthKeyDuplicatedError:
            # সেশন স্থায়ীভাবে অকেজো - মার্ক করে বট টোকেন দিয়ে পাঠাই
            mark_telethon_dead(tclient, "AuthKeyDuplicatedError: একই session দুই IP-তে ব্যবহার হয়েছে।")
            try:
                await tclient.disconnect()
            except Exception:
                pass
            print("↩️ Telethon অকেজো, Bot Token দিয়ে আপলোড করা হচ্ছে...")
            pc = bot

    await _send_via_pyrogram(pc, files, first_cap, rest_cap)

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
        for fi, f in enumerate(files):
            try:
                single_cap = caption if fi == 0 else (tag_only, tag_only, tag_only)
                await send_with_flood_retry([f], single_cap, state, uploader)
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
        tc = get_telethon()
        if t_ok and tc is not None:
            try:
                await tc.send_message(STORAGE_CHANNEL_ID, text_html, parse_mode="html")
                return
            except AuthKeyDuplicatedError:
                mark_telethon_dead(tc, "AuthKeyDuplicatedError")
        await bot.send_message(STORAGE_CHANNEL_ID, text_html, parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        print(f"⚠️ Could not send album report: {e}")

async def start_scan(client, user_id, site, override_html=None):
    old = active_scans.get(user_id)
    if old and not old["task"].done():
        return False
    if link_job_running(user_id):
        return False
    if override_html is None:
        override_html = db_get_site_html(site["key"])
    state = {"cancel": False}
    task = asyncio.create_task(process_and_upload(client, user_id, site, override_html, state))
    state["task"] = task
    active_scans[user_id] = state
    return True

# ================= আপলোডার তালিকা ও ভূমিকা (Role) =================
def _uploader_label(name, first_name):
    fn = (first_name or "").strip()
    return f"{name} ({fn[:14]})" if fn else name

async def build_uploaders():
    # ১) Telethon আপলোডার (সবগুলো জীবিত সেশন)
    tele = []
    for i, c in enumerate(telethon_clients, start=1):
        if id(c) in dead_telethon:
            continue
        ok, _ = await ensure_telethon_connected(c)
        if not ok:
            continue
        uid = None
        fn = ""
        try:
            me = await c.get_me()
            uid = me.id
            fn = me.first_name or ""
        except AuthKeyDuplicatedError:
            mark_telethon_dead(c, "AuthKeyDuplicatedError")
            continue
        except Exception:
            pass
        name = f"Telethon {i}"
        tele.append({
            "type": "telethon", "client": c, "name": name,
            "label": _uploader_label(name, fn), "uid": uid
        })

    # ২) Pyrogram ইউজার আপলোডার (শুধু USE_PYRO_UPLOADERS=1 হলে, অথবা Telethon একদম না থাকলে)
    pyro = []
    if USE_PYRO_UPLOADERS or not tele:
        for name, c in pyro_ready:
            uid = None
            fn = ""
            try:
                me = await c.get_me()
                uid = me.id
                fn = me.first_name or ""
            except Exception:
                pass
            pyro.append({
                "type": "pyrogram", "client": c, "name": name,
                "label": _uploader_label(name, fn), "uid": uid
            })

    uploaders = []
    seen_ids = set()
    for u in pyro + tele:
        uid = u.get("uid")
        if uid is not None:
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
        uploaders.append(u)

    if not uploaders:
        uploaders.append({"type": "pyrogram", "client": bot, "name": "Bot Token", "label": "Bot Token", "uid": None})
    return uploaders

def assign_roles(uploaders):
    """প্রথম আইডি = ছবি, বাকিরা = ভিডিও। একটাই আইডি থাকলে/ROLE_SPLIT বন্ধ থাকলে সবাই 'both'"""
    n = len(uploaders)
    for i, u in enumerate(uploaders):
        if not ROLE_SPLIT or n == 1:
            u["role"] = "both"
        elif i == 0:
            u["role"] = "photo"
        else:
            u["role"] = "video"

def role_icon(role):
    if role == "photo":
        return "🖼️"
    if role == "video":
        return "🎬"
    return "🎬🖼️"

def engine_text(uploaders):
    if uploaders[0]["name"] == "Bot Token":
        return "Bot Token"
    if len(uploaders) == 1:
        return f"{uploaders[0].get('label') or uploaders[0]['name']} (User Session)"
    kind = "Telethon" if all(u["type"] == "telethon" for u in uploaders) else "User"
    p = sum(1 for u in uploaders if u.get("role") == "photo")
    v = sum(1 for u in uploaders if u.get("role") == "video")
    if p and v:
        return f"{len(uploaders)} {kind} Accounts (🖼️ {p} ছবি + 🎬 {v} ভিডিও)"
    return f"{len(uploaders)} {kind} Accounts (Parallel parts)"

def new_worker_state(u):
    return {
        "label": u.get("label") or u["name"],
        "icon": role_icon(u.get("role", "both")),
        "role": u.get("role", "both"),
        "action": "💤 Idle",
        "busy": False,
        "v": 0,
        "p": 0,
        "bytes": {},
    }

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
        if workers:
            busy_n = sum(1 for w_ in workers.values() if w_.get("a_idx"))
            lines.append(f"👥 **Uploaders:** `{busy_n}/{len(workers)}` কাজ করছে")
        for wid in sorted(workers):
            ws = workers[wid]
            if not ws.get("a_idx"):
                lines.append(f"⚪ `{safe(ws['name'])}` → 💤 Idle")
                continue
            mb = sum(ws.get("bytes", {}).values()) // (1024 * 1024)
            mb_txt = f" | 📦 `{mb} MB`" if mb else ""
            lines.append(
                f"🟢 `{safe(ws['name'])}` → 📁 `{ws['a_idx']}/{total_albums}` `{safe(ws['title'])[:28]}`\n"
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
        # ========== লিস্ট পেজ নিজে থেকে সাইট থেকে আনা (SITE_LIST_URLS সেট থাকলে) ==========
        fetch_fn = getattr(site_mod, "fetch_html_content", None)
        if fetch_fn:
            await update_panel(client, user_id, "🌐 **সাইট থেকে লিস্ট পেজ আনা হচ্ছে...**", cancel_kb)
            fetch_err = None
            try:
                fetched = await fetch_fn()
                if fetched and fetched.strip():
                    override_html = fetched
            except Exception as fe:
                fetch_err = f"{type(fe).__name__}: {fe}"
                print(f"⚠️ Auto-fetch failed: {fetch_err}")
            have_fallback = bool((override_html or "").strip()) or bool(str(getattr(site_mod, "HTML_CONTENT", "")).strip())
            if fetch_err and not have_fallback:
                await update_panel(
                    client, user_id,
                    f"❌ **লিস্ট পেজ আনা যায়নি**\n\n`{safe(fetch_err)[:300]}`\n\n"
                    f"কুকি/লিংক চেক করুন অথবা HTML ফাইল পাঠান।",
                    end_kb
                )
                return

        await update_panel(client, user_id, f"🔍 **Running parser `{site['path']}`...**", cancel_kb)
        title, albums = get_site_albums(site, override_html)

        # ========== শুধুমাত্র নতুন অ্যালবাম ফিল্টার ==========
        new_albums = []
        for album in albums:
            items = album.get("items") or []
            if not items and not album.get("needs_resolve"):
                continue

            akey = album_key_of(site_key, album)
            album["_akey"] = akey

            if is_album_processed(akey):
                continue

            # সব মিডিয়া আগে থেকেই আপলোড আছে কিনা
            # (needs_resolve অ্যালবামে এখন শুধু থাম্বনেইল আছে, তাই আসল মিডিয়া আনার পরে চেক হবে)
            if not album.get("needs_resolve") and all(is_duplicate(url) for _, url in items):
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
                f"• সব অ্যালবাম আগেই স্ক্যান করা হয়েছে অথবা স্টোরেজ চ্যানেলে আছে।\n"
                f"• নতুন HTML দিয়ে আবার চেষ্টা করুন।",
                end_kb
            )
            return

        # ========== অ্যালবামের ভেতরের আসল ফটো/ভিডিও বের করা ==========
        resolve_fn = getattr(site_mod, "resolve_album", None)
        if resolve_fn:
            to_resolve = [a for a in albums if a.get("needs_resolve")]
            if to_resolve:
                r_sem = asyncio.Semaphore(RESOLVE_PARALLEL)
                r_done = [0]
                r_last = [0.0]
                debug_pages = []

                async def resolve_one(alb):
                    async with r_sem:
                        check_cancel(state)
                        try:
                            res_items = await resolve_fn(alb, state)
                            res_items = list(res_items or [])
                            if not res_items:
                                raise RuntimeError("কোনো ফটো/ভিডিও পাওয়া যায়নি")
                            alb["items"] = res_items
                        except ScanCancelled:
                            raise
                        except Exception as re_err:
                            alb["_skip"] = True
                            short_title = (alb.get("title") or "").split("\n")[0][:40]
                            errors.append(
                                f"• [Album Open Failed] {short_title}\n"
                                f"  URL: {str(alb.get('url'))[:45]}...\n  Reason: {re_err}"
                            )
                            if alb.get("debug_html") and not debug_pages:
                                debug_pages.append(alb)
                        r_done[0] += 1
                        now = time.monotonic()
                        if now - r_last[0] >= 2 or r_done[0] == len(to_resolve):
                            r_last[0] = now
                            try:
                                await update_panel(
                                    client, user_id,
                                    f"🔎 **অ্যালবামের ভেতরের ফটো/ভিডিও খোঁজা হচ্ছে...**\n"
                                    f"📁 `{r_done[0]}/{len(to_resolve)}` | ❌ Failed: `{len(errors)}`",
                                    cancel_kb
                                )
                            except Exception:
                                pass

                await update_panel(
                    client, user_id,
                    f"🔎 **অ্যালবামের ভেতরের ফটো/ভিডিও খোঁজা হচ্ছে...**\n📁 `0/{len(to_resolve)}`",
                    cancel_kb
                )
                await asyncio.gather(*[resolve_one(a) for a in to_resolve])

                albums = [a for a in albums if not a.get("_skip")]
                total_albums = len(albums)

                if debug_pages:
                    try:
                        dbg = debug_pages[0]
                        bio = io.BytesIO(dbg["debug_html"].encode("utf-8", "ignore"))
                        bio.name = "album_page_debug.html"
                        await client.send_document(
                            user_id, bio,
                            caption=f"🐞 Debug: এই পেজ থেকে ফটো/ভিডিও পাওয়া যায়নি\n{str(dbg.get('url'))[:200]}"
                        )
                    except Exception as dbg_err:
                        print(f"⚠️ Debug HTML send failed: {dbg_err}")

                if not albums:
                    joined = "\n".join(errors[:6])
                    await update_panel(
                        client, user_id,
                        f"❌ **কোনো অ্যালবামের ভেতরের কনটেন্ট পাওয়া যায়নি**\n\n```text\n{joined}\n```\n"
                        f"উপরে পাঠানো `album_page_debug.html` ফাইলটা আমাকে দিন।",
                        end_kb
                    )
                    return

        uploaders = await build_uploaders()
        if uploaders[0]["name"] == "Bot Token":
            uploader_mode = "Pyrogram Bot Token"
        elif len(uploaders) == 1:
            uploader_mode = f"{uploaders[0].get('label') or uploaders[0]['name']} (User Session)"
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
                akey = album.get("_akey") or album_key_of(site_key, album)
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
                if looks_like_bad_download(temp):
                    if os.path.exists(temp):
                        os.remove(temp)
                    return {
                        "ok": False,
                        "err": f"• [Album {a_idx} {label} #{item_idx} Failed]\n  URL: {url[:45]}...\n  Reason: Downloaded file is not valid media (HTML/JSON page)"
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
                report += f"\n⚠️ Failed: {a_fail}\n🔁 ব্যর্থ ফাইল পরের স্ক্যানে আবার চেষ্টা হবে"
            if ALBUM_REPORT_IN_CHANNEL:
                await send_channel_text(report)

            # শুধু সম্পূর্ণ সফল হলে অ্যালবাম "প্রসেসড" মার্ক হবে
            # (ফেইল হলে পরের স্ক্যানে আবার চেষ্টা হবে, আপলোড হওয়া ফাইল ডুপ্লিকেট হিসেবে স্কিপ হবে)
            if a_fail == 0:
                akey = album.get("_akey") or album_key_of(site_key, album)
                mark_album_processed(akey, site_key, album.get("title"))

            albums_done += 1
            ws.update({"a_idx": 0, "action": "✅ Idle", "bytes": {}})
            await refresh(force=True)
            await cancellable_sleep(1, state)

        async def worker(wid, uploader):
            workers[wid] = {
                "name": uploader.get("label") or uploader["name"],
                "a_idx": 0, "title": "", "action": "", "bytes": {}
            }
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
        # স্ক্যান শেষ: Telethon কানেকশন বন্ধ
        try:
            await release_telethon()
        except Exception:
            pass

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
    dead_note = [reason for reason in dead_telethon.values()]
    if dead_note:
        summary_text += f"\n🔴 **Telethon session অকেজো:** `{safe(dead_note[0])[:150]}`\n"
    if errors:
        joined_errors = "\n".join(errors[:8])
        if len(errors) > 8:
            joined_errors += f"\n...and {len(errors) - 8} more errors."
        summary_text += f"\n\n⚠️ **DETAILED ERROR LOG:**\n```text\n{joined_errors}\n```"

    await update_panel(client, user_id, summary_text[:4000], end_kb)

# ================= 🔗 SEND LINK ফিচার =================
# ইউজার অ্যালবামের লিংক পাঠায় → বট সেই অ্যালবামের সব ফটো/ভিডিও স্টোরেজ চ্যানেলে আপলোড করে
# (ক্যাপশন + নিচে #newweb) → শেষে চ্যানেলে "Album completed" মেসেজ।
# একসাথে সর্বোচ্চ MAX_LINKS_QUEUE টা লিংক কিউতে থাকে; একটা শেষ হলে পরেরটা শুরু হয়।
# অ্যালবামের ফাইল সংখ্যার কোনো লিমিট নেই।
# ভূমিকা-ভিত্তিক ভাগ (ROLE_SPLIT=1): প্রথম আইডি শুধু ছবি, বাকি আইডিগুলো শুধু ভিডিও।
# কাজ শেষ হলে যে আইডি ফ্রি হয় সে অন্য লেনের বাকি অংশ নিয়ে সাহায্য করে (🤝)।

def extract_links(text):
    found = []
    for m in re.findall(r'https?://[^\s<>"\']+', text or ""):
        u = m.rstrip(".,;:!?)]}>»")
        if u and u not in found:
            found.append(u)
    return found

def title_from_url(url):
    path = urlparse(url).path.strip("/")
    seg = path.split("/")[-1] if path else ""
    seg = re.sub(r"\.\w{2,4}$", "", seg)
    t = re.sub(r"[-_]+", " ", unquote(seg)).strip()
    return t[:120] or "Album"

async def fetch_link_title(site_mod, url):
    """অ্যালবামের পেজ থেকে টাইটেল আনে (সাইট মডিউলে fetch_album_title(url) থাকলে সেটাই ব্যবহার হবে)"""
    fn = getattr(site_mod, "fetch_album_title", None)
    if fn:
        try:
            t = await fn(url)
            if t:
                return str(t).strip()[:180]
        except Exception:
            pass

    fetch_html = getattr(site_mod, "_fetch_html", None)
    if fetch_html is None:
        return None
    try:
        from curl_cffi.requests import AsyncSession
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    try:
        async with AsyncSession(impersonate="chrome") as s:
            html_text = await fetch_html(s, url, getattr(site_mod, "BASE_URL", None))
        soup = BeautifulSoup(html_text, "html.parser")
        cands = []
        og = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
        if og and og.get("content"):
            cands.append(og["content"])
        h1 = soup.find("h1")
        if h1:
            cands.append(h1.get_text(" ", strip=True))
        if soup.title and soup.title.string:
            cands.append(soup.title.string)
        for c in cands:
            t = re.sub(r"#\S+", "", htmllib.unescape(str(c)))
            t = re.sub(r"\s+", " ", t).strip()
            if len(t) >= 3:
                return t[:180]
    except Exception:
        return None
    return None

def link_panel_text(job):
    cur = job.get("current")
    pending = job["queue"]
    used = len(pending) + (1 if cur else 0)
    free_slots = max(0, MAX_LINKS_QUEUE - used)
    lines = [
        f"🔗 **Link Queue** — `{safe(job['site_name'])}`",
        f"⚡ **Engine:** `{safe(job.get('engine') or '...')}`",
        f"🏷️ **Hashtag:** `{DEFAULT_HASHTAG}`",
        "",
    ]
    if cur:
        mb = job.get("mb", 0)
        mb_txt = f" | 📦 `{mb} MB`" if mb else ""
        shown = safe(cur.get("title") or cur["url"])[:40]
        lines.append(f"▶️ **চলছে:** `{cur['seq']}/{job['total']}` `{shown}`")
        lines.append(f"   {job.get('action', '')}{mb_txt}")

    # ===== প্রতিটা আইডি এখন কী করছে =====
    ws = job.get("workers") or {}
    if ws:
        busy_n = sum(1 for w in ws.values() if w.get("busy"))
        lines.append("")
        lines.append(f"👥 **Uploaders:** `{busy_n}/{len(ws)}` কাজ করছে")
        for w in ws.values():
            wmb = sum(w["bytes"].values()) // (1024 * 1024)
            wmb_txt = f" | 📦 `{wmb} MB`" if wmb else ""
            lines.append(f"{w['icon']} `{safe(w['label'])}` — ✅ 🎬`{w['v']}` 🖼️`{w['p']}`")
            lines.append(f"   {'🟢' if w['busy'] else '⚪'} {w['action']}{wmb_txt}")

    if pending:
        lines.append("")
        lines.append(f"⏳ **অপেক্ষায়:** `{len(pending)}`টি লিংক")
        for i, u in enumerate(pending[:5], start=1):
            lines.append(f"   {i}. `{safe(u)[:45]}`")
        if len(pending) > 5:
            lines.append(f"   ...আরও {len(pending) - 5}টি")
    lines.append("")
    lines.append(
        f"✅ **Done:** `{job['ok']}` | ❌ **Failed:** `{job['failed']}` | "
        f"🎬 `{job['up_v']}` | 🖼️ `{job['up_p']}`"
    )
    if job.get("note"):
        lines.append(job["note"])
    lines.append(f"\n➕ আরও `{free_slots}`টি লিংক পাঠাতে পারবেন।")
    return "\n".join(lines)

async def refresh_link_panel(client, user_id, job, force=False):
    now = time.monotonic()
    if not force and now - job.get("last_panel", 0.0) < 3:
        return
    job["last_panel"] = now
    try:
        await update_panel(client, user_id, link_panel_text(job), get_link_cancel_keyboard())
    except Exception:
        pass

async def enqueue_links(client, user_id, site, links):
    """লিংকগুলো কিউতে যোগ করে; জব না চললে নতুন জব শুরু করে"""
    old_scan = active_scans.get(user_id)
    if old_scan and not old_scan["task"].done():
        return {"status": "scan_running"}

    job = link_jobs.get(user_id)
    new_job = False
    if job and not job.get("closed"):
        if job["state"].get("cancel"):
            return {"status": "cancelling"}
    else:
        job = {
            "queue": [], "state": {"cancel": False}, "site_key": site["key"], "site_name": site["name"],
            "current": None, "done": 0, "total": 0, "ok": 0, "failed": 0, "up_v": 0, "up_p": 0,
            "results": [], "note": "", "closed": False, "engine": "", "action": "",
            "mb": 0, "last_panel": 0.0, "task": None, "site": site, "workers": {},
        }
        link_jobs[user_id] = job
        new_job = True

    added = dup = rejected = 0
    for l in links:
        used = len(job["queue"]) + (1 if job["current"] else 0)
        if l in job["queue"] or (job["current"] and job["current"]["url"] == l):
            dup += 1
            continue
        if used >= MAX_LINKS_QUEUE:
            rejected += 1
            continue
        job["queue"].append(l)
        job["total"] += 1
        added += 1

    notes = []
    if rejected:
        notes.append(f"⚠️ কিউ পূর্ণ (সর্বোচ্চ {MAX_LINKS_QUEUE}টি): `{rejected}`টি লিংক বাদ পড়েছে")
    if dup:
        notes.append(f"ℹ️ `{dup}`টি লিংক আগে থেকেই কিউতে আছে")
    job["note"] = "\n".join(notes)

    if new_job:
        job["task"] = asyncio.create_task(process_link_queue(client, user_id, job))

    return {"status": "ok", "job": job, "new_job": new_job, "added": added, "dup": dup, "rejected": rejected}

async def process_one_link(client, user_id, job, uploaders, url, seq):
    state = job["state"]
    site = job["site"]
    site_key = site["key"]
    site_mod = site["module"]
    hashtag = DEFAULT_HASHTAG

    resolve_fn = getattr(site_mod, "resolve_album", None)
    if resolve_fn is None:
        raise RuntimeError("এই সাইট ফাইলে resolve_album() নেই")

    job["action"] = "🔎 অ্যালবামের পেজ খোলা হচ্ছে..."
    job["mb"] = 0
    await refresh_link_panel(client, user_id, job, force=True)

    title = await fetch_link_title(site_mod, url) or title_from_url(url)
    check_cancel(state)
    job["current"]["title"] = title

    album = {"title": title, "items": [], "url": url, "needs_resolve": True}
    try:
        res_items = await resolve_fn(album, state)
    except Exception:
        if album.get("debug_html"):
            try:
                bio = io.BytesIO(album["debug_html"].encode("utf-8", "ignore"))
                bio.name = "album_page_debug.html"
                await client.send_document(
                    user_id, bio,
                    caption=f"🐞 Debug: এই পেজ থেকে ফটো/ভিডিও পাওয়া যায়নি\n{url[:200]}"
                )
            except Exception as dbg_err:
                print(f"⚠️ Debug HTML send failed: {dbg_err}")
        raise
    check_cancel(state)

    all_items = list(res_items or [])
    if not all_items:
        raise RuntimeError("অ্যালবামে কোনো ফটো/ভিডিও পাওয়া যায়নি")

    akey = album_key_of(site_key, album)
    v_found = sum(1 for k, _ in all_items if k == "video")
    p_found = len(all_items) - v_found

    items = []
    prev_v = prev_p = 0
    seen = set()
    for kind, u in all_items:
        if u in seen:
            continue
        seen.add(u)
        if is_duplicate(u):
            if kind == "video":
                prev_v += 1
            else:
                prev_p += 1
            continue
        items.append((kind, u))

    if not items:
        mark_album_processed(akey, site_key, title)
        return {"ok": True, "title": title, "v": 0, "p": 0, "fail": 0, "note": "আগেই আপলোড করা আছে"}

    a_up_v = a_up_p = a_fail = 0
    n_workers = max(1, len(uploaders))
    sem = asyncio.Semaphore(DOWNLOAD_PARALLEL * n_workers)

    # প্রতিটা আইডির লাইভ স্ট্যাটাস (প্যানেলে দেখানোর জন্য)
    workers = job.get("workers")
    if workers is None:
        workers = job["workers"] = {}
    for u_ in uploaders:
        if u_["name"] not in workers:
            workers[u_["name"]] = new_worker_state(u_)

    job["action"] = "⚙️ ডাউনলোড ও আপলোড চলছে"

    def set_action(wname, text, busy=None):
        w_ = workers.get(wname)
        if w_ is None:
            return
        w_["action"] = text
        if busy is not None:
            w_["busy"] = busy

    # ===== অংশ (part) তৈরি: লেন অনুযায়ী =====
    # split_mode: 'photo' লেন (ছবি) + 'video' লেন (ভিডিও); নাহলে একটাই 'mixed' লেন (আগের মতো)
    indexed = [(i + 1, kind, uu) for i, (kind, uu) in enumerate(items)]

    def _chunks(lst, size):
        return [lst[i:i + size] for i in range(0, len(lst), size)]

    split_mode = (
        any(x.get("role") == "photo" for x in uploaders)
        and any(x.get("role") == "video" for x in uploaders)
    )
    lane_lists = {}
    if split_mode:
        photos = [e for e in indexed if e[1] != "video"]
        videos = [e for e in indexed if e[1] == "video"]
        n_vw = max(1, sum(1 for x in uploaders if x.get("role") == "video"))
        if photos:
            lane_lists["photo"] = _chunks(photos, MAX_ALBUM_BATCH)
        if videos:
            # ভিডিওগুলো ভিডিও-আইডিদের মধ্যে সমান ভাগে ভাগ (সর্বোচ্চ ১০টা করে অংশ)
            vsize = max(1, min(MAX_ALBUM_BATCH, -(-len(videos) // n_vw)))
            lane_lists["video"] = _chunks(videos, vsize)
    else:
        lane_lists["mixed"] = _chunks(indexed, MAX_ALBUM_BATCH)

    lane_q = {}
    lane_done = {}
    lane_total = {}
    for lane, parts in lane_lists.items():
        q = asyncio.Queue()
        lane_done[lane] = []
        for ci, ch in enumerate(parts):
            q.put_nowait((ci, ch))
            lane_done[lane].append(asyncio.Event())
        lane_q[lane] = q
        lane_total[lane] = len(parts)

    lane_icons = {"photo": "🖼️", "video": "🎬", "mixed": "📦"}

    def lane_pref(uploader):
        """আইডি আগে নিজের লেন থেকে কাজ নেবে, খালি হলে অন্য লেন থেকে সাহায্য করবে"""
        if "mixed" in lane_q:
            return ["mixed"]
        order = ["photo", "video"] if uploader.get("role") == "photo" else ["video", "photo"]
        return [l for l in order if l in lane_q]

    async def dl_one(item_idx, kind, uu, wname):
        label = "Video" if kind == "video" else "Photo"
        temp = f"tmplink_{user_id}_{seq}_{item_idx}{url_ext(uu, kind)}"

        async def progress(downloaded, total):
            w_ = workers.get(wname)
            if w_ is not None:
                w_["bytes"][item_idx] = downloaded
            job["mb"] = sum(sum(x["bytes"].values()) for x in workers.values()) // (1024 * 1024)
            await refresh_link_panel(client, user_id, job)

        async with sem:
            try:
                check_cancel(state)
                ok, err = await download_file_with_details(uu, temp, site_mod, state, progress, MAX_FILE_BYTES)
            except ScanCancelled:
                return {"cancelled": True, "ok": False}
        w_ = workers.get(wname)
        if w_ is not None:
            w_["bytes"].pop(item_idx, None)
        if not ok:
            if os.path.exists(temp):
                os.remove(temp)
            return {
                "ok": False,
                "err": f"• [Link {seq} {label} #{item_idx} Failed]\n  URL: {uu[:45]}...\n  Reason: {err}"
            }
        if looks_like_bad_download(temp):
            if os.path.exists(temp):
                os.remove(temp)
            return {
                "ok": False,
                "err": f"• [Link {seq} {label} #{item_idx} Failed]\n  URL: {uu[:45]}...\n  Reason: Downloaded file is not valid media (HTML/JSON page)"
            }
        size = os.path.getsize(temp)
        return {"ok": True, "file": {"path": temp, "kind": kind, "url": uu, "size": size}}

    async def process_chunk(lane, ci, chunk, uploader):
        nonlocal a_up_v, a_up_p, a_fail
        wname = uploader["name"]
        w = workers[wname]
        icon = lane_icons[lane]
        total_parts = lane_total[lane]
        role = uploader.get("role", "both")
        own_lane = "photo" if role == "photo" else ("video" if role == "video" else lane)
        help_mark = " 🤝" if lane != own_lane else ""
        part_txt = f"{icon} Part {ci + 1}/{total_parts}"

        check_cancel(state)
        set_action(wname, f"⬇️ Downloading {part_txt}: `{len(chunk)} files`{help_mark}", busy=True)
        await refresh_link_panel(client, user_id, job, force=True)

        results = await asyncio.gather(*[
            dl_one(idx, kind, uu, wname)
            for idx, kind, uu in chunk
        ])
        if any(r.get("cancelled") for r in results):
            raise ScanCancelled()
        check_cancel(state)

        downloaded_files = []
        for r in results:
            if r["ok"]:
                downloaded_files.append(r["file"])
            else:
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

        waited = False
        for g in groups:
            check_cancel(state)

            # ক্রম ঠিক রাখতে একই লেনের আগের অংশ আপলোড শেষ না হওয়া পর্যন্ত অপেক্ষা
            if ORDERED_PARTS and ci > 0 and not waited:
                set_action(wname, f"⏳ আগের অংশের আপলোডের জন্য অপেক্ষা... ({part_txt}){help_mark}", busy=True)
                await refresh_link_panel(client, user_id, job, force=True)
                await run_cancellable(lane_done[lane][ci - 1].wait(), state)
                waited = True

            mb = sum(f["size"] for f in g) // (1024 * 1024)
            set_action(wname, f"⬆️ Uploading {part_txt}: `{len(g)} files, {mb} MB`{help_mark}", busy=True)
            w["bytes"].clear()
            await refresh_link_panel(client, user_id, job, force=True)
            multi = total_parts > 1
            ok_files, failed = await upload_album_batch(
                g, title, seq, job["total"], ci + 1, multi, hashtag, state, uploader
            )
            v, p, errs = tally(ok_files, failed, seq)
            a_up_v += v
            a_up_p += p
            job["up_v"] += v
            job["up_p"] += p
            w["v"] += v
            w["p"] += p
            a_fail += len(errs)
            await refresh_link_panel(client, user_id, job, force=True)
            await cancellable_sleep(1, state)

        set_action(wname, f"✅ {part_txt} শেষ", busy=False)

    async def link_worker(uploader):
        nonlocal a_fail
        wname = uploader["name"]
        pref = lane_pref(uploader)
        while True:
            check_cancel(state)
            got = None
            for lane in pref:
                try:
                    ci, ch = lane_q[lane].get_nowait()
                    got = (lane, ci, ch)
                    break
                except asyncio.QueueEmpty:
                    continue
            if got is None:
                set_action(wname, "✅ Idle (কাজ শেষ)", busy=False)
                await refresh_link_panel(client, user_id, job, force=True)
                return
            lane, ci, ch = got
            try:
                await process_chunk(lane, ci, ch, uploader)
            except ScanCancelled:
                raise
            except Exception as ce:
                a_fail += len(ch)
                set_action(wname, f"❌ Part {ci + 1} ব্যর্থ: {type(ce).__name__}", busy=False)
                print(f"⚠️ Link {seq} {lane} part {ci + 1} crashed ({uploader['name']}): {type(ce).__name__}: {ce}")
            finally:
                lane_done[lane][ci].set()

    try:
        await refresh_link_panel(client, user_id, job, force=True)
        outcomes = await asyncio.gather(
            *[link_worker(u_) for u_ in uploaders],
            return_exceptions=True
        )
        for o in outcomes:
            if isinstance(o, ScanCancelled):
                raise ScanCancelled()
        for o in outcomes:
            if isinstance(o, BaseException) and not isinstance(o, asyncio.CancelledError):
                raise o
    finally:
        for w_ in workers.values():
            w_["busy"] = False
            w_["bytes"].clear()
            w_["action"] = "💤 Idle"
        for fn in glob.glob(f"tmplink_{user_id}_{seq}_*"):
            try:
                os.remove(fn)
            except Exception:
                pass

    # ===== অ্যালবাম কমপ্লিট মেসেজ (স্টোরেজ চ্যানেলে) =====
    report = (
        f"✅ <b>Album completed</b>\n"
        f"📁 {htmllib.escape(title[:200])}\n"
        f"🎬 Videos: {a_up_v + prev_v}/{v_found}\n"
        f"🖼️ Photos: {a_up_p + prev_p}/{p_found}\n"
        f"📦 Total files in album: {len(all_items)}"
    )
    if a_fail:
        report += f"\n⚠️ Failed: {a_fail}\n🔁 ব্যর্থ ফাইল একই লিংক আবার পাঠালে আবার চেষ্টা হবে"
    await send_channel_text(report)

    if a_fail == 0:
        mark_album_processed(akey, site_key, title)

    return {"ok": a_fail == 0, "title": title, "v": a_up_v, "p": a_up_p, "fail": a_fail, "note": ""}

async def process_link_queue(client, user_id, job):
    state = job["state"]
    site = job["site"]
    site_key = site["key"]
    site_mod = site["module"]
    end_kb = get_site_keyboard(site_key)
    cancelled = False
    fatal_text = None
    dropped = 0

    # অ্যালবামের ফাইল/পেজ সংখ্যার লিমিট তুলে দেওয়া (কাজ শেষে আগের মান ফিরে আসবে)
    saved_limits = {}
    for name, val in (("MAX_ITEMS_PER_ALBUM", 1_000_000), ("MAX_DETAIL_PAGES", 1000)):
        if hasattr(site_mod, name):
            saved_limits[name] = getattr(site_mod, name)
            setattr(site_mod, name, val)

    try:
        job["action"] = "⏳ আপলোডার প্রস্তুত হচ্ছে..."
        uploaders = await build_uploaders()
        assign_roles(uploaders)
        job["engine"] = engine_text(uploaders)
        job["workers"] = {u_["name"]: new_worker_state(u_) for u_ in uploaders}

        seq = 0
        while True:
            if state.get("cancel"):
                cancelled = True
                dropped = len(job["queue"])
                job["queue"].clear()
                job["closed"] = True
                break
            if not job["queue"]:
                job["closed"] = True   # await ছাড়াই বন্ধ মার্ক - যাতে নতুন লিংক হারিয়ে না যায়
                break

            url = job["queue"].pop(0)
            seq += 1
            job["current"] = {"url": url, "title": "", "seq": seq}
            job["action"] = "⏳ শুরু হচ্ছে..."
            job["mb"] = 0
            await refresh_link_panel(client, user_id, job, force=True)

            try:
                res = await process_one_link(client, user_id, job, uploaders, url, seq)
                short = safe(res["title"])[:30]
                if res["ok"]:
                    job["ok"] += 1
                    if res["note"]:
                        job["results"].append(f"✅ {short} — {res['note']}")
                    else:
                        job["results"].append(f"✅ {short} — 🎬{res['v']} 🖼️{res['p']}")
                else:
                    job["failed"] += 1
                    job["results"].append(f"⚠️ {short} — 🎬{res['v']} 🖼️{res['p']} (❌ {res['fail']} failed)")
            except ScanCancelled:
                cancelled = True
                job["current"] = None
                dropped = len(job["queue"])
                job["queue"].clear()
                job["closed"] = True
                break
            except Exception as e:
                job["failed"] += 1
                job["results"].append(f"❌ {safe(url)[:40]} — {type(e).__name__}: {safe(e)[:120]}")

            job["done"] += 1
            job["current"] = None
            await refresh_link_panel(client, user_id, job, force=True)

    except Exception as fatal_e:
        job["closed"] = True
        job["queue"].clear()
        fatal_text = f"{type(fatal_e).__name__}: {safe(fatal_e)}\n{traceback.format_exc()[-800:]}"
    finally:
        job["closed"] = True
        job["current"] = None
        for name, val in saved_limits.items():
            try:
                setattr(site_mod, name, val)
            except Exception:
                pass
        for fn in glob.glob(f"tmplink_{user_id}_*"):
            try:
                os.remove(fn)
            except Exception:
                pass
        try:
            await release_telethon()
        except Exception:
            pass

    sess = user_sessions.get(user_id)
    if sess and sess.get("state") == "WAITING_LINK":
        sess["state"] = "IDLE"

    if fatal_text:
        await update_panel(
            client, user_id,
            f"🚨 **LINK QUEUE FAILURE**\n```python\n{fatal_text[:1500]}\n```",
            end_kb
        )
        return

    head = "⛔ **LINK QUEUE STOPPED**" if cancelled else "🏁 **LINK QUEUE COMPLETED**"
    summary = (
        f"{head}\n\n"
        f"🌐 **Website:** `{safe(job['site_name'])}`\n"
        f"🏷️ **Hashtag:** `{DEFAULT_HASHTAG}`\n"
        f"🔗 **Links Processed:** `{job['done']}/{job['total']}`\n"
        f"✅ **Successful:** `{job['ok']}` | ❌ **Failed:** `{job['failed']}`\n"
        f"🎬 **Videos Uploaded:** `{job['up_v']}`\n"
        f"🖼️ **Photos Uploaded:** `{job['up_p']}`\n"
    )
    if dropped:
        summary += f"🗑️ **বাতিল হওয়া লিংক:** `{dropped}`\n"
    wr = job.get("workers") or {}
    if wr:
        rep = "\n".join(
            f"{w['icon']} {safe(w['label'])}: 🎬{w['v']} 🖼️{w['p']}" for w in wr.values()
        )
        summary += f"\n👥 **Uploader Report:**\n```text\n{rep}\n```"
    if job["results"]:
        shown = "\n".join(job["results"][-8:])
        if len(job["results"]) > 8:
            shown = f"...আগের {len(job['results']) - 8}টি বাদ\n" + shown
        summary += f"\n```text\n{shown}\n```"
    await update_panel(client, user_id, summary[:4000], end_kb)

# ================= Web Service Port Binding (Railway/Render) =================
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return f"Modular Scraper Bot is Running! Loaded sites: {len(loaded_sites)} | Max albums/scan: {MAX_ALBUMS_PER_SCAN}"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

async def main():
    global backup_event, startup_db_status
    await bot.start()
    try:
        status = await restore_db_from_telegram()
    except Exception as e:
        status = f"Restore error: {e}"
    startup_db_status = status
    print(f"💾 {status}")
    # রিস্টোরের জন্য Telethon খুললে সেটা বন্ধ করে দিই (শুধু দরকারে কানেক্ট হবে)
    await release_telethon()

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
    print(f"📌 MAX_ALBUMS_PER_SCAN = {MAX_ALBUMS_PER_SCAN} | DEFAULT_HASHTAG = {DEFAULT_HASHTAG} | MAX_LINKS_QUEUE = {MAX_LINKS_QUEUE} | ORDERED_PARTS = {ORDERED_PARTS}")
    print(f"🎛️ ROLE_SPLIT = {ROLE_SPLIT} | USE_PYRO_UPLOADERS = {USE_PYRO_UPLOADERS}")

    await idle()

    worker.cancel()
    if backup_dirty:
        await do_backup()
    for c in telethon_clients:
        try:
            if c.is_connected():
                await c.disconnect()
        except Exception:
            pass
    for _n, pc in pyro_ready:
        try:
            await pc.stop()
        except Exception:
            pass
    await bot.stop()

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    asyncio.get_event_loop().run_until_complete(main())
