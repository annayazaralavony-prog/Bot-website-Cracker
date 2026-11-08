import os
import re
import time
import asyncio
import html as htmllib
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup, Tag, NavigableString
from curl_cffi.requests import AsyncSession

# ================= ওয়েবসাইট কনফিগারেশন =================
SITE_NAME = "MMSDesi"
HASHTAG = "#newweb"          # <-- আপনার চাওয়া হ্যাশট্যাগ
BASE_URL = "https://mmsdesi.me/"

# এখানে HTML পেস্ট করবেন অথবা বট থেকে "Send New HTML" দিয়ে পাঠাবেন
HTML_CONTENT = """
"""

# ================= রেজেক্স প্যাটার্ন =================
VIDEO_EXT_RE = re.compile(
    r'https?://[^\s"\'<>\\]+?\.(?:mp4|m4v|webm|mov)(?:\?[^\s"\'<>\\]*)?',
    re.IGNORECASE
)
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
IGNORE_IMG_RE = re.compile(
    r"(?<![a-z])(logo|icon|avatar|banner|ads?|pixel|sprite|emoji|favicon|thumb_play|loading)(?![a-z])",
    re.IGNORECASE
)
ALBUM_CONTAINER_RE = re.compile(
    r"album|gallery|folder|collection|post|entry|card|item-box|video-block|media-group|group relative",
    re.IGNORECASE
)
TITLE_CLASS_RE = re.compile(
    r"title|name|caption|header|heading|post-title|entry-title|video-title|desc",
    re.IGNORECASE
)
IGNORE_TEXT_RE = re.compile(
    r"^(download|watch|click here|play|video|photo|image|mp4|hd|views?|\d+[\s\w]*)$",
    re.IGNORECASE
)
HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "summary", "figcaption"]


def _normalize_url(url):
    if not url or not isinstance(url, str):
        return None
    u = htmllib.unescape(url.strip().strip("\"'"))
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("/"):
        u = urljoin(BASE_URL, u)
    if not u.startswith("http"):
        return None
    return u


def _extract_from_srcset(srcset_val):
    if not srcset_val:
        return None
    parts = [p.strip().split(" ")[0] for p in srcset_val.split(",") if p.strip()]
    return _normalize_url(parts[-1]) if parts else None


def classify_node(tag):
    for attr, val in tag.attrs.items():
        if isinstance(val, str):
            m = VIDEO_EXT_RE.search(val)
            if m:
                return ("video", _normalize_url(m.group(0)))

    url = None
    if tag.name == "img":
        parent = tag.parent
        if parent and parent.name == "a":
            href = parent.get("href") or ""
            if VIDEO_EXT_RE.search(href):
                return None

        for attr in ("data-src", "data-original", "data-lazy-src", "data-full", "src"):
            cand = _normalize_url(tag.get(attr))
            if cand and not cand.startswith("data:"):
                url = cand
                break
        if not url and tag.get("srcset"):
            url = _extract_from_srcset(tag.get("srcset"))

    elif tag.name == "a":
        cand = _normalize_url(tag.get("href"))
        if cand and urlparse(cand).path.lower().endswith(IMG_EXTS):
            url = cand

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
            res = classify_node(node)
            if res and res[1]:
                yield res
        elif isinstance(node, NavigableString):
            if node.parent and node.parent.name in ("script", "noscript"):
                for m in VIDEO_EXT_RE.findall(str(node)):
                    u = _normalize_url(m)
                    if u:
                        yield ("video", u)


def _clean_title(text):
    if not text:
        return None
    t = re.sub(r"#\S+", "", str(text))
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) < 3 or IGNORE_TEXT_RE.match(t):
        return None
    return t[:180]


def _extract_title_from_box(box):
    if not isinstance(box, Tag):
        return None

    for sel in HEADING_TAGS:
        for h in box.find_all(sel):
            t = _clean_title(h.get_text(" ", strip=True))
            if t:
                return t

    for el in box.find_all(class_=TITLE_CLASS_RE):
        t = _clean_title(el.get_text(" ", strip=True))
        if t:
            return t

    for attr in ("data-title", "title", "aria-label", "data-name", "alt"):
        val = box.get(attr)
        if isinstance(val, str):
            t = _clean_title(val)
            if t:
                return t

    for p in box.find_all(["p", "strong", "b", "h3"]):
        t = _clean_title(p.get_text(" ", strip=True))
        if t:
            return t

    for media_tag in box.find_all(["a", "video", "img"]):
        for attr in ("data-title", "title", "alt", "aria-label"):
            val = media_tag.get(attr)
            if isinstance(val, str):
                t = _clean_title(val)
                if t:
                    return t
        if media_tag.name == "a":
            t = _clean_title(media_tag.get_text(" ", strip=True))
            if t:
                return t

    return None


def find_album_title(container):
    t = _extract_title_from_box(container)
    if t:
        return t

    sib = container.find_previous_sibling()
    for _ in range(3):
        if not sib:
            break
        if sib.name in HEADING_TAGS or sib.name in ("p", "header", "div", "strong", "span"):
            t = _extract_title_from_box(sib) or _clean_title(sib.get_text(" ", strip=True))
            if t:
                return t
        sib = sib.find_previous_sibling()

    parent = container.parent
    for _ in range(3):
        if not parent or parent.name in ("body", "html", "[document]"):
            break
        for sel in HEADING_TAGS + ["header"]:
            h = parent.find(sel, recursive=False)
            if h:
                t = _clean_title(h.get_text(" ", strip=True))
                if t:
                    return t
        el = parent.find(class_=TITLE_CLASS_RE, recursive=False)
        if el:
            t = _clean_title(el.get_text(" ", strip=True))
            if t:
                return t
        psib = parent.find_previous_sibling()
        if psib and psib.name in HEADING_TAGS + ["p", "header", "strong"]:
            t = _clean_title(psib.get_text(" ", strip=True))
            if t:
                return t
        parent = parent.parent

    return None


def _strategy_containers(soup):
    candidates = []
    for t in soup.find_all(["article", "section", "div", "li", "details", "figure"]):
        cid = " ".join(t.get("class") or []) + " " + (t.get("id") or "")
        if t.name in ("article", "figure") or ALBUM_CONTAINER_RE.search(cid):
            candidates.append(t)

    with_media = []
    for t in candidates:
        items = list(dict.fromkeys(iter_scope_media(t)))
        if items:
            with_media.append((t, items))

    item_map = {id(t): items for t, items in with_media}
    all_ids = set(item_map.keys())

    albums = []
    for t, items in with_media:
        child_with_different_media = t.find(
            lambda child: child is not t
            and id(child) in all_ids
            and len(item_map[id(child)]) < len(items)
        )
        if child_with_different_media:
            continue

        parent_same_media = False
        p = t.parent
        while p:
            if id(p) in all_ids and len(item_map[id(p)]) == len(items):
                parent_same_media = True
                break
            p = p.parent
        if parent_same_media:
            continue

        albums.append({
            "title": find_album_title(t),
            "items": items
        })
    return albums


def _strategy_headings(soup):
    albums = []
    current_album = None
    root = soup.body or soup

    has_headings = bool(root.find(HEADING_TAGS))
    caption_tags = set(HEADING_TAGS) if has_headings else set(HEADING_TAGS + ["p", "strong"])

    for node in root.descendants:
        if isinstance(node, Tag):
            if node.name in caption_tags:
                title_text = _clean_title(node.get_text(" ", strip=True))
                if title_text:
                    if current_album is not None and not current_album["items"]:
                        current_album["title"] = title_text
                    else:
                        current_album = {"title": title_text, "items": []}
                        albums.append(current_album)
                continue

            res = classify_node(node)
            if res and res[1]:
                if current_album is None:
                    current_album = {"title": None, "items": []}
                    albums.append(current_album)
                current_album["items"].append(res)

        elif isinstance(node, NavigableString):
            if node.parent and node.parent.name in ("script", "noscript"):
                for m in VIDEO_EXT_RE.findall(str(node)):
                    u = _normalize_url(m)
                    if u:
                        if current_album is None:
                            current_album = {"title": None, "items": []}
                            albums.append(current_album)
                        current_album["items"].append(("video", u))

    return albums


def _finalize_albums(raw_albums, default_title=""):
    final = []
    for a in raw_albums:
        seen = set()
        items = []
        for kind, url in a.get("items", []):
            if not url or url in seen:
                continue
            seen.add(url)
            items.append((kind, url))
        if items:
            raw_title = _clean_title(a.get("title")) or default_title or ""
            clean_tag = HASHTAG.strip()
            final.append({
                "title": f"{raw_title}\n\n{clean_tag}" if raw_title else clean_tag,
                "items": items
            })
    return final


def extract_albums(html_content):
    """
    HTML থেকে অ্যালবাম বের করে।
    রিটার্ন: (page_title, albums_list)
    """
    html_text = html_content if html_content and html_content.strip() else HTML_CONTENT
    if not html_text or not html_text.strip():
        return SITE_NAME, []

    soup = BeautifulSoup(html_text, "html.parser")

    page_title = None
    og_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
    if og_title and og_title.get("content"):
        page_title = _clean_title(og_title["content"])
    if not page_title and soup.find("h1"):
        page_title = _clean_title(soup.find("h1").get_text(" ", strip=True))
    if not page_title and soup.title and soup.title.string:
        page_title = _clean_title(soup.title.string)
    page_title = page_title or ""

    by_containers = _finalize_albums(_strategy_containers(soup), default_title=page_title)
    by_headings = _finalize_albums(_strategy_headings(soup), default_title=page_title)

    def _titled_count(alb_list):
        return sum(1 for x in alb_list if x["title"])

    if len(by_containers) >= 2 and _titled_count(by_containers) >= _titled_count(by_headings):
        return page_title or SITE_NAME, by_containers
    if len(by_headings) >= 2:
        return page_title or SITE_NAME, by_headings
    if by_containers:
        return page_title or SITE_NAME, by_containers
    if by_headings:
        return page_title or SITE_NAME, by_headings

    all_items = list(iter_scope_media(soup))
    return page_title or SITE_NAME, _finalize_albums([{"title": page_title, "items": all_items}], default_title=page_title)


# ================= curl_cffi দিয়ে Cloudflare ও 403 বাইপাস ডাউনলোডার =================
async def custom_download(url, dest_path, state=None, progress_cb=None, max_bytes=None):
    user_cookie = (
        "_ga=GA1.1.2033937358.1793779443; "
        "dom3ic8zudi28v8lr6fgphwffqoz0j6c=01a1a596-297f-73bb-aeee-f1c73ed0e533; "
        "vrk4n8fqhwc3jzy7pbsmgt6dx5lha2u9=01a1a596-297f-73bb-aeee-f1c73ed0e533_2; "
        "__suvt=8-941b13b1ea7495daf71fbc63d20ee1e8; "
        "imprCounter_df944f9c7362c951eeb70e2971549a1e_expiry=Invalid Date; "
        "__nuvt=8-bd41f605fc8e6ad4aac8296ad99bf260; "
        "imprCounter_1ce633f4a79ad1403e907714d91f9b70_expiry=Invalid Date; "
        "imprCounter_5ca152d2a83beebf9923e476b98919cb_expiry=Invalid Date; "
        "imprCounter_df944f9c7362c951eeb70e2971549a1e=4; "
        "imprCounter_1ce633f4a79ad1403e907714d91f9b70=5; "
        "imprCounter_5ca152d2a83beebf9923e476b98919cb=2; "
        "_ga_PQR73V6M37=GS2.1.s1793779443$o1$g1$t1793779621$j60$l0$h0; "
        "sb_main_91b7e623af01664a963f1516a146e8b0=1; "
        "sb_count_91b7e623af01664a963f1516a146e8b0=1"
    )

    headers = {
        "Referer": "https://mmsdesi.me/",
        "Origin": "https://mmsdesi.me",
        "Cookie": user_cookie,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    last_err = "Unknown error"

    try:
        if state and state.get("cancel"):
            raise asyncio.CancelledError("Scan cancelled by user")

        async with AsyncSession(impersonate="chrome") as session:
            response = await session.get(url, headers=headers, allow_redirects=True, timeout=300)

            if response.status_code != 200:
                return False, f"HTTP Error {response.status_code} - Server blocked."

            content = response.content
            if not content:
                return False, "Downloaded file is 0 bytes."

            total = len(content)
            if max_bytes and total > max_bytes:
                return False, "File exceeds size limit."

            with open(dest_path, "wb") as f:
                f.write(content)

            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                if progress_cb:
                    try:
                        await progress_cb(total, total)
                    except Exception:
                        pass
                return True, "OK"

            last_err = "Failed to save file."

    except asyncio.CancelledError:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise
    except Exception as e:
        last_err = f"{type(e).__name__}: {str(e)}"

    if os.path.exists(dest_path):
        os.remove(dest_path)
    return False, last_err
