# -*- coding: utf-8 -*-
"""
features.py — RedReels (bot.py এর পাশেই রাখুন; bot.py ও mmsdesi.py কিছুই বদলাতে হবে না)

• "Add Html" নেই। "Scan Website" চাপলেই RedGifs API থেকে ট্রেন্ডিং ভিডিও আনে।
• আগে আপলোড হওয়া ভিডিও বাদ দিয়ে প্রতি স্ক্যানে ১০০টি নতুন ভিডিও স্টোরেজ চ্যানেলে যায়
  (১০০ লিমিট bot.py-এর MAX_ALBUMS_PER_SCAN থেকেও মানা হয়)।
• ক্যাপশন = ওয়েবসাইটের টাইটেল/ডেসক্রিপশন (সাইটের #ট্যাগ মুছে ফেলা হয়) + আপনার HASHTAG।
• আপলোড/ডুপ্লিকেট চেক/ক্যানসেল/ব্যাকআপ — সব bot.py এর আগের লজিকই ব্যবহার হয়।

দরকার: pip install curl_cffi aiohttp
"""
import re
import os
import sys
import json
import time
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

# ============================================================
#  কনফিগ
# ============================================================
SITE_NAME = "RedReels"
HASHTAG = "#reels"                  # আপনার নিজস্ব হ্যাশট্যাগ (এখানে বদলান)

RR_KEY = "redreels"
RR_LABEL = "RedReels"

PER_SCAN = 100                      # প্রতি স্ক্যানে কয়টি ভিডিও
SHOW_TYPE_IN_CAPTION = True         # True = ক্যাপশনে ভিডিওর টাইপ (Vertical • FHD • 12s • Audio • ক্যাটাগরি) দেখাবে
UNORDERED_UPLOAD = True             # True = যে আইডি আগে ডাউনলোড শেষ করবে সে আগে আপলোড করবে (ক্রম মানবে না)
SEND_TYPE_REPORT = True             # True = স্ক্যানের আগে টাইপ-ভিত্তিক সারসংক্ষেপ মেসেজ পাঠাবে
PAGE_SIZE = 80                      # API প্রতি রিকোয়েস্টে সর্বোচ্চ
MAX_PAGES = 25                      # নতুন ভিডিও না পেলে সর্বোচ্চ কত পেজ দেখবে
API_BASE = "https://api.redgifs.com/v2"
SEARCH_URL = API_BASE + "/gifs/search"
AUTH_URL = API_BASE + "/auth/temporary"

# ঐচ্ছিক: নিজের টোকেন দিতে চাইলে এনভায়রনমেন্ট ভ্যারিয়েবল REDGIFS_TOKEN সেট করুন।
# না দিলে বট নিজে ফ্রি টেম্পরারি টোকেন নেবে (মেয়াদ শেষ হলে অটো রিনিউ)।
ENV_TOKEN = os.environ.get("REDGIFS_TOKEN", "").strip()

_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Mobile Safari/537.36"
)
_BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://www.redgifs.com",
    "referer": "https://www.redgifs.com/",
    "user-agent": _UA,
}
_DL_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://www.redgifs.com/",
    "Origin": "https://www.redgifs.com",
    "Accept": "*/*",
}


# ============================================================
#  ক্যাপশন/টাইটেল ক্লিন করা
# ============================================================
def _clean_caption(text):
    text = htmllib.unescape(text or "")
    text = re.sub(r"#\w+", "", text)          # ওয়েবসাইটের হ্যাশট্যাগ বাদ
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _make_title(item):
    title = _clean_caption(item.get("description") or "")
    if not title:
        raw_id = item.get("id") or ""
        title = raw_id.replace("-", " ").replace("_", " ").title() if raw_id else ""
    if not title:
        title = "Video"
    return title[:100]


def _pretty(word):
    word = str(word or "").replace("#", "").replace("-", " ").replace("_", " ").strip()
    return word.title()


def _detect_type(item):
    """API-র মেটাডাটা (সাইজ, সময়, অডিও, niches/tags) থেকে ভিডিওর টাইপ বের করে"""
    try:
        w = int(item.get("width") or 0)
        h = int(item.get("height") or 0)
    except Exception:
        w = h = 0
    try:
        dur = float(item.get("duration") or 0)
    except Exception:
        dur = 0.0

    if w and h:
        if h >= w * 1.15:
            orient = "Vertical"
        elif w >= h * 1.15:
            orient = "Horizontal"
        else:
            orient = "Square"
    else:
        orient = ""

    short_side = min(w, h) if (w and h) else 0
    if short_side >= 2160:
        quality = "4K"
    elif short_side >= 1080:
        quality = "FHD"
    elif short_side >= 720:
        quality = "HD"
    elif short_side > 0:
        quality = "SD"
    else:
        quality = ""

    if dur <= 0:
        length = ""
    elif dur < 60:
        length = f"{int(round(dur))}s"
    else:
        length = f"{int(dur // 60)}m{int(dur % 60):02d}s"

    audio = bool(item.get("hasAudio"))

    cats = []
    for src in (item.get("niches") or [], item.get("tags") or []):
        for c in src:
            c = _pretty(c)
            if c and c not in cats:
                cats.append(c)
        if cats:
            break
    category = " / ".join(cats[:2])

    parts = [orient, quality, length, "Audio" if audio else "", category]
    return {
        "orientation": orient or "Unknown",
        "quality": quality,
        "audio": audio,
        "category": cats[0] if cats else "",
        "label": " • ".join(p for p in parts if p),
    }


# ============================================================
#  RedGifs API
# ============================================================
class _Auth:
    token = ENV_TOKEN


async def _refresh_token(session):
    r = await session.get(AUTH_URL, headers=_BASE_HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Token API error {r.status_code}")
    tok = (r.json() or {}).get("token")
    if not tok:
        raise RuntimeError("Token not found in auth response")
    _Auth.token = tok
    return tok


async def _get_page(session, page):
    params = {
        "type": "g", "order": "trending", "count": PAGE_SIZE,
        "verified": "y", "page": page,
    }
    for attempt in range(2):
        if not _Auth.token:
            await _refresh_token(session)
        headers = dict(_BASE_HEADERS)
        headers["authorization"] = f"Bearer {_Auth.token}"
        r = await session.get(SEARCH_URL, params=params, headers=headers, timeout=30)
        if r.status_code in (401, 403) and attempt == 0:
            _Auth.token = ""          # মেয়াদ শেষ -> নতুন টোকেন নিয়ে আবার চেষ্টা
            continue
        if r.status_code != 200:
            raise RuntimeError(f"API error {r.status_code}")
        return r.json()
    raise RuntimeError("API auth failed")


async def fetch_fresh_videos(is_duplicate, want=PER_SCAN, max_pages=MAX_PAGES):
    """আগে আপলোড না হওয়া `want` টি ভিডিও খুঁজে বের করে। রিটার্ন: [(title, url), ...]"""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        raise RuntimeError("curl_cffi ইনস্টল নেই। চালান: pip install curl_cffi")

    found, seen = [], set()
    async with AsyncSession(impersonate="chrome") as session:
        page = 1
        total_pages = max_pages
        while len(found) < want and page <= min(max_pages, total_pages):
            data = await _get_page(session, page)
            total_pages = int(data.get("pages") or 1)
            gifs = data.get("gifs") or []
            if not gifs:
                break
            for item in gifs:
                urls = item.get("urls") or {}
                url = urls.get("hd") or urls.get("sd")
                if not url:
                    continue
                url = url.split("?")[0]
                if not re.search(r"\.(mp4|webm)$", url, re.IGNORECASE):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                try:
                    if is_duplicate(url):
                        continue
                except Exception:
                    pass
                found.append((_make_title(item), url, _detect_type(item)))
                if len(found) >= want:
                    break
            page += 1
            await asyncio.sleep(0.3)
    return found


# ============================================================
#  bot.py এর "সাইট মডিউল" ফাংশনগুলো
# ============================================================
def rr_extract_albums(html_content):
    """scan এর সময় fetch করা JSON এখানে আসে; প্রতিটি ভিডিও = একটি অ্যালবাম"""
    page_title = "RedReels Feed"
    albums = []
    try:
        data = json.loads(html_content or "{}")
    except Exception:
        return page_title, albums
    for entry in data.get("videos", []):
        title, url = entry[0], entry[1]
        info = entry[2] if len(entry) > 2 and isinstance(entry[2], dict) else {}
        label = info.get("label") or ""
        if SHOW_TYPE_IN_CAPTION and label:
            title = f"{title} — {label}"
        albums.append({"title": title[:190], "items": [("video", url)]})
    return page_title, albums


def _rm(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


async def rr_custom_download(url, dest_path, state, progress_cb=None, max_bytes=None):
    """bot.py এর download_file_with_details এই ফাংশন ব্যবহার করবে (Referer সহ ডাউনলোড)"""
    import aiohttp
    last_err = "Unknown"
    for attempt in range(1, 4):
        try:
            if state and state.get("cancel"):
                return False, "__CANCELLED__"
            async with aiohttp.ClientSession(headers=_DL_HEADERS) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    total = int(resp.headers.get("Content-Length") or 0)
                    downloaded = 0
                    last_cb = 0.0
                    cancelled = False
                    too_big = False
                    with open(dest_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            if state and state.get("cancel"):
                                cancelled = True
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if max_bytes and downloaded > max_bytes:
                                too_big = True
                                break
                            if progress_cb and time.monotonic() - last_cb >= 4:
                                last_cb = time.monotonic()
                                try:
                                    await progress_cb(downloaded, total)
                                except Exception:
                                    pass
                    if cancelled:
                        _rm(dest_path)
                        return False, "__CANCELLED__"
                    if too_big:
                        _rm(dest_path)
                        return False, "File exceeds size limit."
            if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
                raise RuntimeError("Downloaded file is 0 bytes.")
            return True, "OK"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)}"
        _rm(dest_path)
        for _ in range(3 * attempt * 2):
            if state and state.get("cancel"):
                return False, "__CANCELLED__"
            await asyncio.sleep(0.5)
    return False, f"{last_err} (failed after 3 attempts)"


_site_module = types.SimpleNamespace(
    SITE_NAME=SITE_NAME,
    HASHTAG=HASHTAG,
    HTML_CONTENT="",
    extract_albums=rr_extract_albums,
    custom_download=rr_custom_download,
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
                and getattr(m, "__file__", "").endswith("bot.py"):
            return m
    return None


def M():
    m = _find_main()
    if m is None:
        raise RuntimeError("bot.py module not found")
    return m


# ============================================================
#  UI
# ============================================================
def rr_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Scan Website", callback_data="rr_scan")],
        [InlineKeyboardButton("🏠 Home", callback_data="menu_home")],
    ])


def _panel_text(note=""):
    extra = f"{note}\n\n" if note else ""
    return (
        "🎬 **RedReels**\n\n"
        f"Tap **Scan Website** — the bot will fetch trending videos and upload "
        f"`{PER_SCAN}` new videos to the storage channel (already-uploaded ones are skipped).\n\n"
        f"{extra}"
        "Choose an option below 👇"
    )


async def _show_panel_new(client, user_id, note=""):
    m = M()
    sess = m.user_sessions.setdefault(user_id, {"state": "IDLE", "custom_html": {}})
    sess["state"] = "IDLE"
    await m.reset_panel(client, user_id)
    await m.update_panel(client, user_id, _panel_text(note), rr_keyboard())


async def _show_panel_edit(client, user_id, note=""):
    m = M()
    sess = m.user_sessions.setdefault(user_id, {"state": "IDLE", "custom_html": {}})
    sess["state"] = "IDLE"
    await m.update_panel(client, user_id, _panel_text(note), rr_keyboard())


# ============================================================
#  ফিল্টার ও হ্যান্ডলার
# ============================================================
_OPEN_RE = re.compile(r"^(?:🎬\s*)?RedReels$", re.IGNORECASE)


async def _f_open(_, __, message):
    return bool(message.text and _OPEN_RE.match(message.text.strip()))


open_filter = filters.create(_f_open)


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


async def _do_scan(client, cq, m, user_id, sess):
    if sess.get("rr_scanning"):
        try:
            await cq.answer("Scan already in progress, please wait.", show_alert=True)
        except Exception:
            pass
        return
    sess["rr_scanning"] = True
    try:
        await m.safe_answer(cq, "Fetching videos...")
        sess["state"] = "IDLE"
        await m.update_panel(
            client, user_id,
            "🔎 **RedReels**\n\nFetching new videos from the website, please wait...",
            None
        )

        videos = await fetch_fresh_videos(m.is_duplicate)

        if not videos:
            await m.update_panel(
                client, user_id,
                "⚠️ No new videos found (everything fetched was already uploaded). Try again later.",
                rr_keyboard()
            )
            return

        if SEND_TYPE_REPORT:
            try:
                from collections import Counter
                orient = Counter(v[2].get("orientation", "Unknown") for v in videos)
                cats = Counter(v[2].get("category") for v in videos if v[2].get("category"))
                audio_n = sum(1 for v in videos if v[2].get("audio"))
                lines = [f"🔎 **Detected `{len(videos)}` new videos**", ""]
                lines.append("📐 " + " | ".join(f"{k}: `{n}`" for k, n in orient.most_common()))
                lines.append(f"🔊 With audio: `{audio_n}`")
                if cats:
                    lines.append("🗂 Top categories: " + ", ".join(f"{k} (`{n}`)" for k, n in cats.most_common(5)))
                await client.send_message(user_id, "\n".join(lines))
            except Exception as rep_err:
                print(f"RedReels type report error: {rep_err}")

        payload = json.dumps({"videos": videos}, ensure_ascii=False)

        # শুধু RedReels স্ক্যানের সময় ORDERED_ALBUMS বন্ধ রাখা হয় (যে আগে ডাউনলোড শেষ করবে সে আগে আপলোড করবে)।
        # স্ক্যান শেষ হলেই আগের মান ফিরে আসে, তাই mmsdesi-র ক্রম অপরিবর্তিত থাকে।
        orig_ordered = getattr(m, "ORDERED_ALBUMS", True)
        if UNORDERED_UPLOAD:
            m.ORDERED_ALBUMS = False
        started = await m.start_scan(client, user_id, _site(), payload)
        if started and UNORDERED_UPLOAD:
            try:
                task = (m.active_scans.get(user_id) or {}).get("task")
                if task is not None:
                    task.add_done_callback(lambda _t, _o=orig_ordered: setattr(m, "ORDERED_ALBUMS", _o))
                else:
                    m.ORDERED_ALBUMS = orig_ordered
            except Exception:
                m.ORDERED_ALBUMS = orig_ordered
        elif UNORDERED_UPLOAD:
            m.ORDERED_ALBUMS = orig_ordered
        if not started:
            await m.update_panel(
                client, user_id,
                "⚠️ A scan or link queue is already running. Finish or cancel it first.",
                rr_keyboard()
            )
    finally:
        sess["rr_scanning"] = False


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
        elif data == "rr_scan":
            await _do_scan(client, cq, m, user_id, sess)
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
#  bot.py এর সাথে জুড়ে দেওয়া (bot.py ফাইল অপরিবর্তিত থাকে)
# ============================================================
def _apply(m):
    try:
        def patched_build_sites_reply_keyboard():
            rows = [[KeyboardButton(f"{m.SITE_PREFIX}{s['name']}")] for s in m.loaded_sites.values()]
            rows.append([KeyboardButton(RR_LABEL)])
            return ReplyKeyboardMarkup(rows, resize_keyboard=True, placeholder="Select a website module...")

        m.build_sites_reply_keyboard = patched_build_sites_reply_keyboard

        orig_get_site_keyboard = m.get_site_keyboard

        def patched_get_site_keyboard(site_key):
            if site_key == RR_KEY:
                return rr_keyboard()
            return orig_get_site_keyboard(site_key)

        m.get_site_keyboard = patched_get_site_keyboard

        m.bot.add_handler(MessageHandler(rr_open, filters.private & filters.text & open_filter), group=-1)
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

    loop = getattr(m, "loop", None)
    if loop is None:
        loop = asyncio.get_event_loop()
    loop.call_soon(_apply, m)


_install()
