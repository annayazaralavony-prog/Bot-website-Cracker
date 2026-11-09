# -*- coding: utf-8 -*-
"""
features.py  —  RedReels সেকশন (bot.py এর পাশেই রাখুন, bot.py কিছুই চেঞ্জ করতে হবে না)

কীভাবে কাজ করে:
  • bot.py এর discover_sites() প্রতিটা .py ফাইল লোড করে। এই ফাইলে মডিউল-লেভেলে
    extract_albums / get_albums / HTML_CONTENT নেই, তাই এটা "সাইট" হিসেবে মেনুতে আসে না,
    কিন্তু লোড হওয়ার সময়ই নিচের _install() চলে এবং RedReels ফিচারটা বটে জুড়ে দেয়।
  • mmsdesi বা অন্য কোনো সাইট ফাইলের কোনো কিছু বদলায় না।
  • স্ক্যান/আপলোড/ডুপ্লিকেট চেক/ক্যানসেল — সব bot.py এর আগের লজিকই ব্যবহার হয়।

সাইট কনফিগ (আপনার দেওয়া):
  SITE_NAME = "RedReels", HASHTAG = "#reels"
"""
import re
import sys
import types
import asyncio
import traceback
import html as htmllib

from pyrogram import filters, StopPropagation
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)

SITE_NAME = "RedReels"
HASHTAG = "#reels"

RR_KEY = "redreels"                 # ডাটাবেসে HTML সেভ করার key
RR_LABEL = "RedReels"               # চ্যাট বক্সের নিচের মেনু বাটনের নাম
RR_STATE_WAIT_HTML = "WAITING_RR_HTML"


# ============================================================
#  স্ক্যান লজিক (আপনার দেওয়া নিয়ম অনুযায়ী)
# ============================================================
def _clean_html(html_content):
    text = html_content or ""
    text = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    text = text.replace("\\u0026", "&").replace("\\u003d", "=").replace("\\u003D", "=")
    text = htmllib.unescape(text)
    return text


def rr_extract_albums(html_content):
    page_title = "RedReels Feed"
    text = _clean_html(html_content)

    # https://... এবং //cdn... (প্রোটোকল ছাড়া) দুই ধরনের লিংকই ধরবে
    raw_urls = re.findall(
        r'(?:https?:)?//[^\s"\'<>\\]+?\.(?:mp4|webm)(?![A-Za-z0-9])',
        text, re.IGNORECASE
    )

    unique_urls = []
    seen = set()
    for url in raw_urls:
        if url.startswith("//"):
            url = "https:" + url
        clean_url = url.split("?")[0]
        if clean_url not in seen:
            seen.add(clean_url)
            unique_urls.append(clean_url)

    albums = []
    for idx, url in enumerate(unique_urls):
        title_match = re.search(r'/([^/]+)\.(?:mp4|webm)$', url, re.IGNORECASE)
        raw_title = title_match.group(1) if title_match else f"Video_{idx + 1}"
        title = (raw_title.replace("-mobile", "").replace("-hd", "")
                 .replace("-", " ").replace("_", " ").title())

        # প্রতিটি ভিডিও একটি আলাদা 'অ্যালবাম': প্রতি স্ক্যানে ১০০টি,
        # পরের স্ক্যানে ডাটাবেস দেখে পরের ১০০টি
        albums.append({
            "title": title[:100],
            "items": [("video", url)]
        })

    return page_title, albums


# bot.py এর process_and_upload() যে "সাইট মডিউল" আশা করে তার সমতুল্য অবজেক্ট
_site_module = types.SimpleNamespace(
    SITE_NAME=SITE_NAME,
    HASHTAG=HASHTAG,
    HTML_CONTENT="",
    extract_albums=rr_extract_albums,
)


def _site():
    return {
        "key": RR_KEY,
        "name": SITE_NAME,
        "hashtag": HASHTAG,
        "module": _site_module,
        "path": "features.py",
    }


# ============================================================
#  bot.py মডিউল খুঁজে বের করা
# ============================================================
def _find_main():
    for name in ("__main__", "bot"):
        m = sys.modules.get(name)
        if m is not None and hasattr(m, "discover_sites") and hasattr(m, "bot"):
            return m
    for m in list(sys.modules.values()):
        if m is not None and hasattr(m, "discover_sites") and hasattr(m, "bot") \
                and hasattr(m, "SITE_PREFIX") is not None and getattr(m, "__file__", "").endswith("bot.py"):
            return m
    return None


def M():
    m = _find_main()
    if m is None:
        raise RuntimeError("bot.py module not found")
    return m


# ============================================================
#  UI: কিবোর্ড ও টেক্সট
# ============================================================
def rr_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add Html", callback_data="rr_add_html")],
        [InlineKeyboardButton("Scan Website", callback_data="rr_scan")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu_home")],
    ])


def rr_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="rr_menu")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu_home")],
    ])


def _get_html(user_id):
    m = M()
    sess = m.user_sessions.get(user_id) or {}
    html = (sess.get("custom_html") or {}).get(RR_KEY)
    if not html:
        html = m.db_get_site_html(RR_KEY)
    return html


def _status_text(user_id):
    html = _get_html(user_id)
    if not html:
        return "📄 **HTML Status:** `Not added yet`"
    try:
        _t, albums = rr_extract_albums(html)
        total = len(albums)
        m = M()
        fresh = sum(1 for a in albums if not m.is_duplicate(a["items"][0][1]))
        return (
            "📄 **HTML Status:** `Saved ✅`\n"
            f"🎬 **Videos detected:** `{total}` | 🆕 **Not uploaded yet:** `{fresh}`"
        )
    except Exception as e:
        return f"📄 **HTML Status:** `Saved` (parse error: {str(e)[:80]})"


def _panel_text(user_id, note=""):
    extra = f"{note}\n\n" if note else ""
    return (
        "🎬 **RedReels**\n\n"
        "Please provide your project's HTML.\n"
        "Send it as a `.html` file, or paste the HTML code right here.\n"
        "The bot will automatically detect the video links (mp4 / webm) inside it.\n"
        "Once the HTML is added, tap **Scan Website** to upload the videos to the storage channel.\n\n"
        f"{extra}"
        f"{_status_text(user_id)}\n\n"
        "Choose an option below 👇"
    )


async def _show_panel_new(client, user_id, note=""):
    """নতুন প্যানেল মেসেজ পাঠায় (পুরনোটা মুছে)"""
    m = M()
    sess = m.user_sessions.setdefault(user_id, {"state": "IDLE", "custom_html": {}})
    sess["state"] = "IDLE"
    await m.reset_panel(client, user_id)
    await m.update_panel(client, user_id, _panel_text(user_id, note), rr_keyboard())


async def _show_panel_edit(client, user_id, note=""):
    """বর্তমান প্যানেল মেসেজ এডিট করে"""
    m = M()
    sess = m.user_sessions.setdefault(user_id, {"state": "IDLE", "custom_html": {}})
    sess["state"] = "IDLE"
    await m.update_panel(client, user_id, _panel_text(user_id, note), rr_keyboard())


# ============================================================
#  ফিল্টার
# ============================================================
_OPEN_RE = re.compile(r"^(?:🎬\s*)?RedReels$", re.IGNORECASE)


async def _f_open(_, __, message):
    return bool(message.text and _OPEN_RE.match(message.text.strip()))


async def _f_wait_html(_, __, message):
    if not message.from_user:
        return False
    try:
        m = M()
    except Exception:
        return False
    sess = m.user_sessions.get(message.from_user.id) or {}
    if sess.get("state") != RR_STATE_WAIT_HTML:
        return False
    if message.text:
        t = message.text.strip()
        if t.startswith("/") or t.startswith(m.SITE_PREFIX) or _OPEN_RE.match(t):
            return False
    return True


open_filter = filters.create(_f_open)
wait_filter = filters.create(_f_wait_html)


# ============================================================
#  হ্যান্ডলার
# ============================================================
async def rr_open(client, message):
    try:
        user_id = message.from_user.id
        try:
            await message.delete()
        except Exception:
            pass
        await _show_panel_new(client, user_id)
    except Exception as e:
        print(f"RedReels open error: {type(e).__name__}: {e}")
        traceback.print_exc()
    raise StopPropagation


async def _save_html(client, message, html_content):
    m = M()
    user_id = message.from_user.id
    sess = m.user_sessions.setdefault(user_id, {"state": "IDLE", "custom_html": {}})

    _t, albums = rr_extract_albums(html_content)
    if not albums:
        await message.reply(
            "⚠️ No video links (.mp4 / .webm) were found in this HTML.\n"
            "Please send the correct HTML file or code."
        )
        return False

    m.save_site_html(RR_KEY, html_content)
    sess.setdefault("custom_html", {})[RR_KEY] = html_content
    sess["state"] = "IDLE"
    try:
        await message.delete()
    except Exception:
        pass
    await _show_panel_new(
        client, user_id,
        note=f"✅ **HTML saved.** Detected `{len(albums)}` video link(s)."
    )
    return True


async def rr_html_text(client, message):
    try:
        text = (message.text or "").strip()
        looks_like_html = bool(re.search(r"<\s*(!doctype|html|body|div|a\s|img|video|source|script)", text, re.I)) \
            or bool(re.search(r"\.(mp4|webm)", text, re.I))
        if not looks_like_html:
            await message.reply(
                "⚠️ This doesn't look like HTML code. Paste the full HTML or send a `.html` file."
            )
        else:
            await _save_html(client, message, text)
    except Exception as e:
        print(f"RedReels html text error: {type(e).__name__}: {e}")
        traceback.print_exc()
    raise StopPropagation


async def rr_html_document(client, message):
    import os
    path = None
    try:
        doc = message.document
        name = (doc.file_name or "").lower()
        if not name.endswith((".html", ".htm", ".txt")):
            await message.reply("Only `.html`, `.htm` or `.txt` files are supported.")
        else:
            path = await message.download()
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                html_content = f.read()
            await _save_html(client, message, html_content)
    except Exception as e:
        print(f"RedReels html doc error: {type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            await message.reply(f"❌ Error: `{type(e).__name__}: {str(e)[:150]}`")
        except Exception:
            pass
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    raise StopPropagation


async def _alert(cq, text):
    try:
        await cq.answer(text, show_alert=True)
    except Exception:
        pass


async def rr_callbacks(client, cq):
    try:
        m = M()
        user_id = cq.from_user.id
        data = cq.data
        sess = m.user_sessions.setdefault(user_id, {"state": "IDLE", "custom_html": {}})
        sess["panel_msg_id"] = cq.message.id

        if data == "rr_menu":
            await m.safe_answer(cq)
            await _show_panel_edit(client, user_id)

        elif data == "rr_add_html":
            sess["state"] = RR_STATE_WAIT_HTML
            await m.safe_answer(cq)
            await m.update_panel(
                client, user_id,
                "📄 **RedReels — Add Html**\n\n"
                "Send your project's HTML now:\n"
                "• Upload a `.html` file, or\n"
                "• Paste the HTML code directly.\n\n"
                "It will be saved automatically and the video links will be detected for you.\n"
                "Sending new HTML replaces the previously saved one.",
                rr_back_keyboard()
            )

        elif data == "rr_scan":
            html = _get_html(user_id)
            if not html:
                await _alert(cq, "Please add your HTML first.")
                return
            await m.safe_answer(cq, "Starting scan...")
            sess["state"] = "IDLE"
            started = await m.start_scan(client, user_id, _site(), html)
            if not started:
                await m.update_panel(
                    client, user_id,
                    "⚠️ A scan or link queue is already running. Finish or cancel it first.",
                    rr_keyboard()
                )
        else:
            await m.safe_answer(cq)

    except Exception as e:
        print(f"RedReels callback error: {type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            await M().update_panel(
                client, cq.from_user.id,
                f"❌ **Error:** `{type(e).__name__}: {str(e)[:200]}`", rr_keyboard()
            )
        except Exception:
            pass
    raise StopPropagation


# ============================================================
#  bot.py এর সাথে জুড়ে দেওয়া (monkey-patch + handlers)
# ============================================================
def _apply(m):
    try:
        # ১) চ্যাট বক্সের নিচের মেনুতে RedReels বাটন
        def patched_build_sites_reply_keyboard():
            rows = [[KeyboardButton(f"{m.SITE_PREFIX}{s['name']}")] for s in m.loaded_sites.values()]
            rows.append([KeyboardButton(RR_LABEL)])
            return ReplyKeyboardMarkup(rows, resize_keyboard=True, placeholder="Select a website module...")

        m.build_sites_reply_keyboard = patched_build_sites_reply_keyboard

        # ২) স্ক্যান শেষে RedReels এর নিজের বাটন দেখানো (অন্য সাইটের আগের মতোই থাকবে)
        orig_get_site_keyboard = m.get_site_keyboard

        def patched_get_site_keyboard(site_key):
            if site_key == RR_KEY:
                return rr_keyboard()
            return orig_get_site_keyboard(site_key)

        m.get_site_keyboard = patched_get_site_keyboard

        # ৩) হ্যান্ডলার (group -1 = bot.py এর হ্যান্ডলারের আগে চলবে)
        m.bot.add_handler(MessageHandler(rr_open, filters.private & filters.text & open_filter), group=-1)
        m.bot.add_handler(MessageHandler(rr_html_text, filters.private & filters.text & wait_filter), group=-1)
        m.bot.add_handler(MessageHandler(rr_html_document, filters.private & filters.document & wait_filter), group=-1)
        m.bot.add_handler(CallbackQueryHandler(rr_callbacks, filters.regex(r"^rr_")), group=-1)

        print("🎬 RedReels feature loaded (features.py)")
    except Exception as e:
        print(f"⚠️ RedReels install failed: {type(e).__name__}: {e}")
        traceback.print_exc()


def _install():
    m = _find_main()
    if m is None:
        return
    if getattr(m, "_redreels_installed", False):
        return
    m._redreels_installed = True

    # bot.py এর সব ফাংশন ডিফাইন হওয়ার পর (event loop চালু হলে) প্যাচ করা হবে
    loop = getattr(m, "loop", None)
    if loop is None:
        loop = asyncio.get_event_loop()
    loop.call_soon(_apply, m)


_install()
