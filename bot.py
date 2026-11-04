import os
import re
import sqlite3
import asyncio
import traceback
import aiohttp
from threading import Thread
from flask import Flask
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, RPCError

# ================= কনফিগারেশন (Environment Variables) =================
API_ID = int(os.environ.get("API_ID", "123456"))
API_HASH = os.environ.get("API_HASH", "your_api_hash")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token")
STORAGE_CHANNEL_ID = int(os.environ.get("STORAGE_CHANNEL_ID", "-100123456789"))

app = Client("html_scraper_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ================= ডাটাবেস সেটআপ (ডুপ্লিকেট রোধ করার জন্য) =================
conn = sqlite3.connect("scraped_data.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS uploaded_media (
        media_url TEXT PRIMARY KEY,
        hashtag TEXT
    )
""")
conn.commit()

def is_duplicate(url):
    cursor.execute("SELECT 1 FROM uploaded_media WHERE media_url = ?", (url,))
    return cursor.fetchone() is not None

def mark_as_uploaded(url, hashtag):
    cursor.execute("INSERT OR IGNORE INTO uploaded_media (media_url, hashtag) VALUES (?, ?)", (url, hashtag))
    conn.commit()

# ================= ইউজার সেশন মেমোরি (Single Message Tracker) =================
user_sessions = {}

# ================= কিবোর্ড বাটনসমূহ (Inline Buttons) =================
def get_home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("add HTML FILE 🗄️", callback_data="menu_add_html")]
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
        buttons.append([InlineKeyboardButton(f"✅ Process Now ({parts_count} Parts Collected)", callback_data="action_process")])
    buttons.append([InlineKeyboardButton("❌ Cancel / Home", callback_data="menu_home")])
    return InlineKeyboardMarkup(buttons)

# ================= মেসেজ আপডেট হেল্পার (একই মেসেজে এডিট করার ফাংশন) =================
async def update_panel(client, user_id, text, reply_markup=None):
    session = user_sessions.get(user_id, {})
    msg_id = session.get("panel_msg_id")
    try:
        if msg_id:
            await client.edit_message_text(
                chat_id=user_id,
                message_id=msg_id,
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
        else:
            msg = await client.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            user_sessions.setdefault(user_id, {})["panel_msg_id"] = msg.id
    except RPCError as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            msg = await client.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            user_sessions.setdefault(user_id, {})["panel_msg_id"] = msg.id

# ================= স্টার্ট কমান্ড ও বাটন কলব্যাক হ্যান্ডলার =================
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    user_id = message.from_user.id
    try:
        await message.delete() # চ্যাট পরিষ্কার রাখতে ইউজারের /start মেসেজটি ডিলিট করবে
    except Exception:
        pass

    user_sessions[user_id] = {
        "state": "IDLE",
        "buffer": [],
        "hashtag": "#rells",
        "panel_msg_id": user_sessions.get(user_id, {}).get("panel_msg_id")
    }

    welcome_text = (
        "🤖 **HTML Media Scraper Dashboard**\n\n"
        "ওয়েবসাইটের HTML কোড বা ফাইল থেকে ভিডিও ও ছবি স্বয়ংক্রিয়ভাবে স্টোরেজ চ্যানেলে আপলোড করতে নিচের বাটনে ক্লিক করুন:"
    )
    await update_panel(client, user_id, welcome_text, get_home_keyboard())

@app.on_callback_query()
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
            await update_panel(
                client, user_id,
                "🤖 **HTML Media Scraper Dashboard**\n\nনতুন ফাইল বা কোড স্ক্যান করতে নিচের বাটনে ক্লিক করুন:",
                get_home_keyboard()
            )

        elif data == "menu_add_html":
            user_sessions[user_id]["state"] = "WAITING_TAG"
            user_sessions[user_id]["buffer"] = []
            await update_panel(
                client, user_id,
                "🏷️ **Step 1: Select Section Hashtag**\n\nএই ওয়েবসাইটের ভিডিওগুলো চিহ্নিত করার জন্য নিচের যেকোনো একটি হ্যাশট্যাগ সিলেক্ট করুন:",
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
                    f"এখন কোনো এডিট ছাড়াই সরাসরি `.html` ফাইল আপলোড করুন অথবা সম্পূর্ণ কোড কপি করে চ্যাটে পেস্ট করুন।\n"
                    f"*(কোড ১০-১৫ ভাগে ভাগ হয়ে গেলেও বট সব এই এক মেসেজেই জমা করে নেবে)*",
                    get_waiting_html_keyboard(0)
                )

        elif data == "action_process":
            buffer_list = user_sessions[user_id].get("buffer", [])
            if not buffer_list:
                await cq.answer("⚠️ No HTML code received yet!", show_alert=True)
                return
            full_html = "".join(buffer_list)
            hashtag = user_sessions[user_id].get("hashtag", "#rells")
            user_sessions[user_id]["buffer"] = []
            user_sessions[user_id]["state"] = "PROCESSING"
            await process_and_upload(client, user_id, full_html, hashtag)

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

# ================= টেক্সট ও ডকুমেন্ট ইনপুট হ্যান্ডলার =================
@app.on_message(filters.text & filters.private)
async def handle_text_input(client, message):
    user_id = message.from_user.id
    text = message.text.strip()

    # চ্যাট পরিষ্কার রাখতে ইউজারের পাঠানো মেসেজ ডিলিট করে শুধু মেইন প্যানেলে আপডেট দেখানো
    try:
        await message.delete()
    except Exception:
        pass

    if user_id not in user_sessions:
        user_sessions[user_id] = {"state": "IDLE", "buffer": [], "hashtag": "#rells"}

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
            f"সবগুলো অংশ পেস্ট করা শেষ হলে নিচের **✅ Process Now** বাটনে ক্লিক করুন:",
            get_waiting_html_keyboard(parts)
        )

@app.on_message(filters.document & filters.private)
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
            f"**Reason:** `Only .html, .htm, or .txt files are supported. Please upload a valid file.`",
            get_waiting_html_keyboard(0)
        )
        return

    await update_panel(client, user_id, f"📥 **Downloading File:** `{file_name}`...\nPlease wait...")

    file_path = None
    try:
        file_path = await message.download()
        try:
            await message.delete() # ডাউনলোড শেষে ইউজারের ফাইল মেসেজটি ডিলিট করা
        except Exception:
            pass

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            html_content = f.read()

        if os.path.exists(file_path):
            os.remove(file_path)

        await process_and_upload(client, user_id, html_content, hashtag)

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

# ================= স্ক্যান, ডাউনলোড ও ডিটেইলড এরর ট্র্যাকিং =================
def extract_media_from_html(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    page_title = soup.title.string.strip() if soup.title and soup.title.string else "Website Media"
    
    videos = set()
    images = set()

    # সরাসরি যেকোনো .mp4 লিংক খোঁজা
    raw_mp4s = re.findall(r'https?://[^\s"\'<>]+?\.mp4(?:\?[^\s"\'<>]*)?', html_content, re.IGNORECASE)
    for v in raw_mp4s:
        videos.add(v)

    # HTML ট্যাগ থেকে ভিডিও ও ছবি খোঁজা
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
    """Returns (Success_Boolean, Error_Reason_In_English)"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        timeout = aiohttp.ClientTimeout(total=900) # ১৫ মিনিট টাইমআউট বড় ভিডিওর জন্য
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False, f"HTTP Error {resp.status} ({resp.reason}) - Server blocked or file removed."
                
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > 2000 * 1024 * 1024:
                    return False, "File size exceeds Telegram 2GB limit."

                with open(dest_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        f.write(chunk)

                if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
                    return False, "Downloaded file is 0 bytes (Empty response from server)."

                return True, "OK"

    except asyncio.TimeoutError:
        return False, "Connection Timeout: Server took too long to respond (>15 mins)."
    except aiohttp.ClientConnectorError as e:
        return False, f"DNS/Connection Error: Cannot connect to host ({str(e)})."
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)}"

async def process_and_upload(client, user_id, html_content, hashtag):
    try:
        await update_panel(client, user_id, "🔍 **Scanning HTML Code...**\nExtracting video and image links...")
        title, videos, images = extract_media_from_html(html_content)

        if not videos and not images:
            await update_panel(
                client, user_id,
                "⚠️ **NO MEDIA FOUND IN HTML**\n\n"
                "**Diagnostic Report (English):**\n"
                "• `Total HTML Length:` " + f"`{len(html_content)} chars`\n"
                "• `Direct .mp4 Links:` `0 found`\n"
                "• `Valid Image Links:` `0 found`\n"
                "• **Possible Cause:** The website might be loading videos dynamically via encrypted JavaScript (m3u8/blob) or requires login cookies.",
                get_home_keyboard()
            )
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
            return

        uploaded_vids = 0
        uploaded_imgs = 0
        error_logs = [] # সব ইংরেজি এরর এখানে জমা হবে

        # ১. ভিডিও আপলোড লুপ
        for idx, v_url in enumerate(new_videos, start=1):
            temp_vid = f"temp_vid_{user_id}_{idx}.mp4"
            await update_panel(
                client, user_id,
                f"⏳ **Processing Media (Single Panel View)**\n\n"
                f"📌 **Title:** `{title[:40]}`\n"
                f"🏷️ **Hashtag:** `{hashtag}`\n"
                f"⬇️ **Action:** `Downloading Video ({idx}/{len(new_videos)})`\n"
                f"🔗 **URL:** `{v_url[:50]}...`\n"
                f"✅ **Uploaded So Far:** `{uploaded_vids} Videos` | ❌ **Failed:** `{len(error_logs)}`"
            )

            ok, err_reason = await download_file_with_details(v_url, temp_vid)
            if not ok:
                error_logs.append(f"• [Video #{idx} Download Failed]\n  URL: {v_url[:45]}...\n  Reason: {err_reason}")
                continue

            try:
                await update_panel(
                    client, user_id,
                    f"⏳ **Processing Media (Single Panel View)**\n\n"
                    f"⬆️ **Action:** `Uploading Video to Telegram ({idx}/{len(new_videos)})`\n"
                    f"📦 **File Size:** `{os.path.getsize(temp_vid) // (1024*1024)} MB`\n"
                    f"✅ **Uploaded So Far:** `{uploaded_vids} Videos` | ❌ **Failed:** `{len(error_logs)}`"
                )
                caption = f"🎬 **{title}**\n\n{hashtag} #website"
                await client.send_video(
                    chat_id=STORAGE_CHANNEL_ID,
                    video=temp_vid,
                    caption=caption,
                    supports_streaming=True
                )
                mark_as_uploaded(v_url, hashtag)
                uploaded_vids += 1
                await asyncio.sleep(3)

            except FloodWait as fw:
                await asyncio.sleep(fw.value)
            except RPCError as rpc_err:
                error_logs.append(
                    f"• [Video #{idx} Telegram API Error]\n"
                    f"  Type: {type(rpc_err).__name__}\n"
                    f"  Reason: {str(rpc_err)} (Check STORAGE_CHANNEL_ID & Bot Admin Rights)"
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
                f"⏳ **Processing Images...**\n\n"
                f"🖼️ **Progress:** `{idx}/{len(new_images)}`\n"
                f"✅ **Videos Done:** `{uploaded_vids}` | **Images Done:** `{uploaded_imgs}`\n"
                f"❌ **Errors Logged:** `{len(error_logs)}`"
            )

            ok, err_reason = await download_file_with_details(img_url, temp_img)
            if not ok:
                error_logs.append(f"• [Image #{idx} Download Failed]\n  URL: {img_url[:45]}...\n  Reason: {err_reason}")
                continue

            try:
                caption = f"🖼️ **{title}**\n\n{hashtag} #website"
                await client.send_photo(
                    chat_id=STORAGE_CHANNEL_ID,
                    photo=temp_img,
                    caption=caption
                )
                mark_as_uploaded(img_url, hashtag)
                uploaded_imgs += 1
                await asyncio.sleep(2)
            except RPCError as rpc_err:
                error_logs.append(f"• [Image #{idx} Telegram API Error]\n  Reason: {type(rpc_err).__name__} - {str(rpc_err)}")
            except Exception as up_err:
                error_logs.append(f"• [Image #{idx} Upload Error]\n  Reason: {type(up_err).__name__} - {str(up_err)}")
            finally:
                if os.path.exists(temp_img):
                    os.remove(temp_img)

        # ৩. চূড়ান্ত রিপোর্ট ও ইংরেজি এরর সামারি একই মেসেজে দেখানো
        summary_text = (
            f"🏁 **TASK COMPLETED REPORT**\n\n"
            f"📌 **Page Title:** `{title[:45]}`\n"
            f"🏷️ **Section Tag:** `{hashtag}`\n"
            f"🎬 **Videos Uploaded:** `{uploaded_vids}/{len(new_videos)}`\n"
            f"🖼️ **Images Uploaded:** `{uploaded_imgs}/{len(new_images)}`\n"
            f"❌ **Total Failed:** `{len(error_logs)}`\n"
        )

        if error_logs:
            # মেসেজ লিমিট ঠিক রাখতে সর্বোচ্চ ৮টি বিস্তারিত এরর দেখানো হবে
            joined_errors = "\n".join(error_logs[:8])
            if len(error_logs) > 8:
                joined_errors += f"\n...and {len(error_logs) - 8} more errors."
            summary_text += f"\n⚠️ **DETAILED ERROR LOG (ENGLISH):**\n```text\n{joined_errors}\n```"
        else:
            summary_text += "\n✅ **Status:** `All media processed smoothly with 0 errors!`"

        user_sessions[user_id]["state"] = "IDLE"
        await update_panel(client, user_id, summary_text, get_home_keyboard())

    except Exception as fatal_e:
        err_trace = traceback.format_exc()
        user_sessions[user_id]["state"] = "IDLE"
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
    return "Single-Message Scraper Bot is Running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    print("🤖 Single-Panel Bot is starting...")
    app.run()
