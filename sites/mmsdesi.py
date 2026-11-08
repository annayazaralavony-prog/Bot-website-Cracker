import os
import re
import time
import asyncio
import html as htmllib
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup, Tag, NavigableString
from curl_cffi.requests import AsyncSession

# ================= ওয়েবসাইট কনফিগারেশন =================
SITE_NAME = "MMSDesi"
HASHTAG = "#newweb"
BASE_URL = "https://mmsdesi.me/"

# এখানে লিস্ট পেজের HTML পেস্ট করবেন অথবা বট থেকে "Send New HTML" দিয়ে পাঠাবেন
HTML_CONTENT = """
"""

SITE_HOST = (urlparse(BASE_URL).netloc or "").lower()
if SITE_HOST.startswith("www."):
    SITE_HOST = SITE_HOST[4:]

MAX_DETAIL_PAGES = max(1, int(os.environ.get("MAX_DETAIL_PAGES", "8")))        # একটা অ্যালবামের সর্বোচ্চ কয়টা পেজ (pagination)
MAX_ITEMS_PER_ALBUM = max(1, int(os.environ.get("MAX_ITEMS_PER_ALBUM", "300")))  # একটা অ্যালবামে সর্বোচ্চ কয়টা ফাইল
FETCH_RETRIES = 3
DOWNLOAD_RETRIES = 3

# কুকি Railway Variables-এ SITE_COOKIE নামে দিলে সেটাই ব্যবহার হবে (কুকি মেয়াদ শেষ হলে সহজে বদলানো যাবে)
DEFAULT_COOKIE = (
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


def _get_cookie():
    return os.environ.get("SITE_COOKIE", DEFAULT_COOKIE).strip()


# ================= রেজেক্স প্যাটার্ন =================
VIDEO_EXT_RE = re.compile(
    r'(?:https?:)?//[^\s"\'<>\\]+?\.(?:mp4|m4v|webm|mov)(?:\?[^\s"\'<>\\]*)?',
    re.IGNORECASE
)
HLS_RE = re.compile(r'\.m3u8', re.IGNORECASE)
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".m4v", ".webm", ".mov")
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

# অ্যালবামের পেজ লিংক চেনার জন্য - এসব পাথের লিংক বাদ
SKIP_PATH_RE = re.compile(
    r"/(tag|tags|category|categories|author|authors|page|search|label|login|register|contact|about|dmca|privacy|terms)(/|$)"
)
# অ্যালবামের ভেতরের পেজে "রিলেটেড/সাইডবার/বিজ্ঞাপন" অংশ বাদ দেওয়ার জন্য
JUNK_CLASS_RE = re.compile(
    r"related|recommend|suggest|similar|sidebar|comment|(?<![a-z])share(?![a-z])|social|popular|trending|"
    r"you-may|more-video|(?<![a-z])ads?(?![a-z])|advert|sponsor",
    re.IGNORECASE
)
SCOPE_SELECTORS = [
    "article", "main", "[itemprop=articleBody]", ".entry-content", ".post-content",
    ".single-post", "#content", "#primary", "#main",
]
META_VIDEO_KEYS = ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream")
EMBED_HINT_RE = re.compile(r"embed|player|video|stream|play|watch", re.IGNORECASE)


# ================= URL হেল্পার =================
def _normalize_url(url, base=None):
    if not url or not isinstance(url, str):
        return None
    u = htmllib.unescape(url.strip().strip("\"'"))
    u = u.replace("\\/", "/").replace("\\u0026", "&").replace("\\u003d", "=")
    if not u or u.startswith(("data:", "javascript:", "mailto:", "tel:", "#")):
        return None
    if u.startswith("//"):
        u = "https:" + u
    elif not u.startswith("http"):
        u = urljoin(base or BASE_URL, u)
    if not u.startswith("http"):
        return None
    return u


def _same_site(url):
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return bool(SITE_HOST) and (host == SITE_HOST or host.endswith("." + SITE_HOST))


def _is_video_url(url):
    return urlparse(url).path.lower().endswith(VIDEO_EXTS)


def _extract_from_srcset(srcset_val, base=None):
    if not srcset_val:
        return None
    parts = [p.strip().split(" ")[0] for p in srcset_val.split(",") if p.strip()]
    return _normalize_url(parts[-1], base) if parts else None


def _valid_album_href(href, base=None):
    """অ্যালবামের পেজের লিংক কিনা যাচাই (মিডিয়া/ট্যাগ/ক্যাটাগরি লিংক বাদ)"""
    if not href or not isinstance(href, str):
        return None
    h = href.strip()
    if not h or h.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    u = _normalize_url(h, base)
    if not u or not _same_site(u):
        return None
    path = urlparse(u).path.lower()
    if path in ("", "/"):
        return None
    if path.endswith(IMG_EXTS + VIDEO_EXTS):
        return None
    if SKIP_PATH_RE.search(path):
        return None
    return u.split("#")[0]


# ================= মিডিয়া চেনা =================
def _small_img(tag):
    for a in ("width", "height"):
        v = tag.get(a)
        if v and str(v).isdigit() and int(v) < 80:
            return True
    return False


def classify_node(tag, base=None):
    # ১) যেকোনো অ্যাট্রিবিউটে ভিডিও লিংক
    for attr, val in tag.attrs.items():
        if isinstance(val, str):
            m = VIDEO_EXT_RE.search(val)
            if m:
                u = _normalize_url(m.group(0), base)
                if u:
                    return ("video", u)

    # ২) <video src> / <source type=video/*> (এক্সটেনশন ছাড়া হলেও)
    if tag.name in ("video", "source"):
        ttype = (tag.get("type") or "").lower() if isinstance(tag.get("type"), str) else ""
        if tag.name == "video" or ttype.startswith("video/"):
            cand = _normalize_url(tag.get("src"), base)
            if cand and not HLS_RE.search(cand):
                return ("video", cand)

    # ৩) ছবি
    url = None
    if tag.name == "img":
        if _small_img(tag):
            return None
        parent = tag.parent
        if parent is not None and parent.name == "a":
            href = parent.get("href") or ""
            if VIDEO_EXT_RE.search(href):
                return None
            # ছবির লিংক থাকলে পুরো সাইজের ছবিটা <a> থেকেই নেওয়া হবে
            if urlparse(href).path.lower().endswith(IMG_EXTS):
                return None

        for attr in ("data-src", "data-original", "data-lazy-src", "data-full", "src"):
            cand = _normalize_url(tag.get(attr), base)
            if cand and not cand.startswith("data:"):
                url = cand
                break
        if not url and tag.get("srcset"):
            url = _extract_from_srcset(tag.get("srcset"), base)

    elif tag.name == "a":
        cand = _normalize_url(tag.get("href"), base)
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


def iter_scope_media(scope, base=None):
    for node in scope.descendants:
        if isinstance(node, Tag):
            res = classify_node(node, base)
            if res and res[1]:
                yield res
        elif isinstance(node, NavigableString):
            if node.parent and node.parent.name in ("script", "noscript"):
                text = str(node).replace("\\/", "/")
                for m in VIDEO_EXT_RE.findall(text):
                    u = _normalize_url(m, base)
                    if u:
                        yield ("video", u)


# ================= টাইটেল বের করা =================
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


# ================= অ্যালবামের পেজ লিংক বের করা =================
def _inside_title(a, container):
    p = a.parent
    while p is not None and p is not container:
        if getattr(p, "name", None) in HEADING_TAGS:
            return True
        cls = " ".join(p.get("class") or []) if hasattr(p, "get") else ""
        if cls and TITLE_CLASS_RE.search(cls):
            return True
        p = p.parent
    return False


def _first_valid_link(anchors):
    for a in anchors:
        u = _valid_album_href(a.get("href"))
        if u:
            return u
    return None


def find_album_link(container):
    """কার্ড/কন্টেইনার থেকে অ্যালবামের নিজস্ব পেজের লিংক"""
    if not isinstance(container, Tag):
        return None
    if container.name == "a":
        u = _valid_album_href(container.get("href"))
        if u:
            return u

    anchors = container.find_all("a", href=True)

    # ১) থাম্বনেইল (img/video) ঘেরা লিংক
    u = _first_valid_link([a for a in anchors if a.find(["img", "video"])])
    if u:
        return u
    # ২) টাইটেল/হেডিংয়ের ভেতরের লিংক
    u = _first_valid_link([a for a in anchors if _inside_title(a, container)])
    if u:
        return u
    # ৩) যেকোনো বৈধ লিংক
    u = _first_valid_link(anchors)
    if u:
        return u
    # ৪) কার্ড নিজেই লিংকের ভেতরে থাকলে
    outer = container.find_parent("a", href=True)
    if outer:
        return _valid_album_href(outer.get("href"))
    return None


def _link_of_node(node):
    if not isinstance(node, Tag):
        return None
    a = node.find("a", href=True)
    if a is None:
        a = node.find_parent("a", href=True)
    return _valid_album_href(a.get("href")) if a else None


# ================= লিস্ট পেজ থেকে অ্যালবাম বের করা =================
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
            "items": items,
            "url": find_album_link(t),
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
                    link = _link_of_node(node)
                    if current_album is not None and not current_album["items"]:
                        current_album["title"] = title_text
                    else:
                        current_album = {"title": title_text, "items": [], "url": None}
                        albums.append(current_album)
                    if link and not current_album.get("url"):
                        current_album["url"] = link
                continue

            res = classify_node(node)
            if res and res[1]:
                if current_album is None:
                    current_album = {"title": None, "items": [], "url": None}
                    albums.append(current_album)
                current_album["items"].append(res)
                if not current_album.get("url"):
                    pa = node.find_parent("a", href=True)
                    link = _valid_album_href(pa.get("href")) if pa else None
                    if link:
                        current_album["url"] = link

        elif isinstance(node, NavigableString):
            if node.parent and node.parent.name in ("script", "noscript"):
                for m in VIDEO_EXT_RE.findall(str(node).replace("\\/", "/")):
                    u = _normalize_url(m)
                    if u:
                        if current_album is None:
                            current_album = {"title": None, "items": [], "url": None}
                            albums.append(current_album)
                        current_album["items"].append(("video", u))

    return albums


def _finalize_albums(raw_albums, default_title=""):
    final = []
    by_url = {}
    for a in raw_albums:
        seen = set()
        items = []
        for kind, url in a.get("items", []):
            if not url or url in seen:
                continue
            seen.add(url)
            items.append((kind, url))
        if not items:
            continue

        url = a.get("url")
        if url and url in by_url:
            existing = by_url[url]
            have = {u for _k, u in existing["items"]}
            for it in items:
                if it[1] not in have:
                    existing["items"].append(it)
            continue

        raw_title = _clean_title(a.get("title")) or default_title or ""
        clean_tag = HASHTAG.strip()
        entry = {
            "title": f"{raw_title}\n\n{clean_tag}" if raw_title else clean_tag,
            "items": items,
            "url": url,
            "needs_resolve": bool(url),   # true হলে বট অ্যালবামের পেজ খুলে ভেতরের আসল মিডিয়া আনবে
        }
        final.append(entry)
        if url:
            by_url[url] = entry
    return final


def extract_albums(html_content):
    """
    লিস্ট পেজের HTML থেকে অ্যালবাম বের করে (নেটওয়ার্ক কল নেই, দ্রুত)।
    প্রতিটা অ্যালবামে 'url' থাকলে ভেতরের আসল ফটো/ভিডিও resolve_album() দিয়ে আনা হবে।
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

    def _linked_count(alb_list):
        return sum(1 for x in alb_list if x.get("url"))

    # যে স্ট্র্যাটেজিতে বেশি অ্যালবামের পেজ লিংক মিলেছে সেটাই ভালো
    if len(by_containers) >= 2 and (
        _linked_count(by_containers) > _linked_count(by_headings)
        or (_linked_count(by_containers) == _linked_count(by_headings)
            and _titled_count(by_containers) >= _titled_count(by_headings))
    ):
        return page_title or SITE_NAME, by_containers
    if len(by_headings) >= 2:
        return page_title or SITE_NAME, by_headings
    if by_containers:
        return page_title or SITE_NAME, by_containers
    if by_headings:
        return page_title or SITE_NAME, by_headings

    all_items = list(iter_scope_media(soup))
    return page_title or SITE_NAME, _finalize_albums([{"title": page_title, "items": all_items, "url": None}], default_title=page_title)


# ================= অ্যালবামের ভেতরের পেজ থেকে আসল মিডিয়া বের করা =================
def _count_media(el, base):
    return sum(1 for _ in iter_scope_media(el, base))


def _pick_scope(soup, base):
    """মূল কনটেন্ট এলাকা বেছে নেয় (সাইডবার/রিলেটেড বাদ দিতে)"""
    for sel in SCOPE_SELECTORS:
        try:
            els = soup.select(sel)
        except Exception:
            continue
        best, best_n = None, 0
        for el in els:
            n = _count_media(el, base)
            if n > best_n:
                best, best_n = el, n
        if best is not None:
            return best
    return soup.body or soup


def _prune_junk(scope):
    ancestors = {id(p) for p in scope.parents}
    for t in list(scope.find_all(True)):
        try:
            if getattr(t, "decomposed", False):
                continue
            if t is scope or id(t) in ancestors:
                continue
            cid = " ".join(t.get("class") or []) + " " + (t.get("id") or "")
            if t.name in ("nav", "aside", "footer", "form") or JUNK_CLASS_RE.search(cid):
                t.decompose()
        except AttributeError:
            continue


def _collect_iframes(scope, base):
    out = []
    for f in scope.find_all("iframe"):
        for attr in ("src", "data-src", "data-lazy-src"):
            u = _normalize_url(f.get(attr), base)
            if u and EMBED_HINT_RE.search(u) and u not in out:
                out.append(u)
                break
    return out[:3]


def _meta_videos(soup, base):
    found = []
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key in META_VIDEO_KEYS:
            u = _normalize_url(meta.get("content"), base)
            if u and _is_video_url(u) and u not in found:
                found.append(u)
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        txt = (s.string or s.get_text() or "").replace("\\/", "/")
        for m in re.finditer(r'"contentUrl"\s*:\s*"([^"]+)"', txt):
            u = _normalize_url(m.group(1), base)
            if u and _is_video_url(u) and u not in found:
                found.append(u)
    return found


def _wide_videos(html_text, base):
    text = html_text.replace("\\/", "/").replace("\\u0026", "&")
    found = []
    for m in VIDEO_EXT_RE.findall(text):
        u = _normalize_url(m, base)
        if u and u not in found:
            found.append(u)
    return found


def extract_detail(html_text, page_url):
    """
    একটা অ্যালবাম পেজের HTML থেকে (items, next_page_urls, iframe_urls, notes) ফেরত দেয়।
    """
    notes = {"hls": bool(HLS_RE.search(html_text)), "scope": ""}
    soup = BeautifulSoup(html_text, "html.parser")

    # পরের পেজের লিংক (pruning-এর আগে)
    nxt = []
    try:
        for el in soup.select('a[rel~="next"], link[rel~="next"], a.next, a.next-page, a.page-numbers.next'):
            u = _normalize_url(el.get("href"), page_url)
            if u and _same_site(u) and u != page_url and u not in nxt:
                nxt.append(u)
    except Exception:
        pass

    scope = _pick_scope(soup, page_url)
    notes["scope"] = getattr(scope, "name", "") or "page"
    iframes = _collect_iframes(scope, page_url)
    _prune_junk(scope)

    items = list(dict.fromkeys(iter_scope_media(scope, page_url)))

    if not any(k == "video" for k, _u in items):
        for u in _meta_videos(soup, page_url):
            items.append(("video", u))

    # ভিডিও না পেলে এবং প্রায় কিছুই না পেলে পুরো পেজে (স্ক্রিপ্টসহ) খুঁজি
    if not any(k == "video" for k, _u in items) and len(items) <= 1:
        for u in _wide_videos(html_text, page_url):
            items.append(("video", u))

    items = list(dict.fromkeys(items))
    return items, nxt, iframes, notes


def _looks_like_challenge(text):
    head = text[:30000]
    return len(text) < 30000 and (
        "Just a moment" in head or "cf-browser-verification" in head or "challenge-platform" in head
    )


def _page_headers(referer=None):
    return {
        "Referer": referer or BASE_URL,
        "Cookie": _get_cookie(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


async def _fetch_html(session, url, referer=None):
    last = "unknown"
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            r = await session.get(url, headers=_page_headers(referer), allow_redirects=True, timeout=60)
            if r.status_code == 200:
                text = r.text or ""
                if text and not _looks_like_challenge(text):
                    return text
                last = "Cloudflare challenge পেজ এসেছে (কুকি আপডেট করুন)"
            elif r.status_code in (403, 503):
                last = f"HTTP {r.status_code} - ব্লক করা হয়েছে (কুকি/Cloudflare)"
            else:
                last = f"HTTP {r.status_code}"
                if r.status_code in (404, 410):
                    break
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        await asyncio.sleep(2 * attempt)
    raise RuntimeError(f"অ্যালবামের পেজ খোলা যায়নি ({last})")


async def resolve_album(album, state=None):
    """
    বট স্ক্যানের সময় প্রতিটা অ্যালবামের জন্য এটা কল করে।
    অ্যালবামের নিজস্ব পেজ খুলে ভেতরের সব ফটো/ভিডিওর [(kind, url), ...] ফেরত দেয়।
    ব্যর্থ হলে RuntimeError তোলে (album["debug_html"] সেট করে, যাতে বট ডিবাগ ফাইল পাঠাতে পারে)।
    """
    url = album.get("url")
    if not url:
        raise RuntimeError("অ্যালবামের পেজ লিংক পাওয়া যায়নি")

    exclude = {u for _k, u in (album.get("items") or [])}   # লিস্ট পেজের থাম্বনেইল
    collected = []
    notes = {"pages": 0, "bytes": 0, "hls": False, "scope": ""}
    visited = set()
    queue = [url]
    first_html = None
    first_iframes = []

    async with AsyncSession(impersonate="chrome") as session:
        while queue and notes["pages"] < MAX_DETAIL_PAGES:
            if state and state.get("cancel"):
                break
            page_url = queue.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)
            try:
                page_html = await _fetch_html(session, page_url, BASE_URL if page_url == url else url)
            except Exception:
                if notes["pages"] == 0:
                    raise
                break

            if first_html is None:
                first_html = page_html
            notes["pages"] += 1
            notes["bytes"] += len(page_html)

            items, nxt, iframes, pn = extract_detail(page_html, page_url)
            notes["hls"] = notes["hls"] or pn["hls"]
            notes["scope"] = notes["scope"] or pn["scope"]
            collected.extend(items)
            if notes["pages"] == 1:
                first_iframes = iframes
            for n in nxt:
                if n not in visited and n not in queue:
                    queue.append(n)
            await asyncio.sleep(0.4)

        # ভিডিও না পেলে প্লেয়ার iframe খুলে দেখি
        if not any(k == "video" for k, _u in collected) and first_iframes:
            for fu in first_iframes:
                if state and state.get("cancel"):
                    break
                try:
                    emb_html = await _fetch_html(session, fu, url)
                except Exception:
                    continue
                emb_items, _n, _i, pn = extract_detail(emb_html, fu)
                notes["hls"] = notes["hls"] or pn["hls"]
                collected.extend(it for it in emb_items if it[0] == "video")
                await asyncio.sleep(0.4)

    merged, seen = [], set()
    for kind, u in collected:
        if u in seen:
            continue
        seen.add(u)
        merged.append((kind, u))

    filtered = [it for it in merged if it[1] not in exclude]
    if not filtered:
        if first_html:
            album["debug_html"] = first_html[:900000]
        kb = notes["bytes"] // 1024
        extra = " | HLS(m3u8) স্ট্রিম আছে - সাপোর্ট নেই" if notes["hls"] else ""
        only_thumb = " | শুধু থাম্বনেইল মিলেছে" if merged else ""
        raise RuntimeError(
            f"ভেতরে কোনো ফটো/ভিডিও পাওয়া যায়নি (পেজ {notes['pages']}টি, {kb}KB, scope=<{notes['scope']}>{only_thumb}{extra})"
        )

    return filtered[:MAX_ITEMS_PER_ALBUM]


# ================= curl_cffi দিয়ে Cloudflare ও 403 বাইপাস ডাউনলোডার =================
def _media_headers():
    return {
        "Referer": BASE_URL,
        "Origin": BASE_URL.rstrip("/"),
        "Cookie": _get_cookie(),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _safe_remove(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


async def _stream_download(url, dest_path, state, progress_cb, max_bytes):
    """স্ট্রিম করে ডিস্কে লেখে (পুরো ফাইল RAM-এ আসে না)। রিটার্ন: (ok, err, retry)"""
    async with AsyncSession(impersonate="chrome") as session:
        async with session.stream("GET", url, headers=_media_headers(), allow_redirects=True, timeout=600) as r:
            if r.status_code != 200:
                retry = r.status_code in (408, 429) or r.status_code >= 500
                return False, f"HTTP Error {r.status_code} - Server blocked.", retry

            try:
                total = int(r.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            encoded = bool(r.headers.get("Content-Encoding"))

            downloaded = 0
            last_cb = 0.0
            with open(dest_path, "wb") as f:
                async for chunk in r.aiter_content(chunk_size=1024 * 1024):
                    if state and state.get("cancel"):
                        return False, "__CANCELLED__", False
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if max_bytes and downloaded > max_bytes:
                        return False, "File exceeds size limit.", False
                    if progress_cb and time.monotonic() - last_cb >= 4:
                        last_cb = time.monotonic()
                        try:
                            await progress_cb(downloaded, total)
                        except Exception:
                            pass

            if total and not encoded and downloaded < total:
                return False, f"Incomplete download ({downloaded}/{total} bytes)", True
            if downloaded == 0:
                return False, "Downloaded file is 0 bytes.", True
            if progress_cb:
                try:
                    await progress_cb(downloaded, total or downloaded)
                except Exception:
                    pass
            return True, "OK", False


async def _whole_download(url, dest_path, state, progress_cb, max_bytes):
    """পুরনো curl_cffi (stream সাপোর্ট নেই) হলে ফallback"""
    async with AsyncSession(impersonate="chrome") as session:
        response = await session.get(url, headers=_media_headers(), allow_redirects=True, timeout=600)
        if response.status_code != 200:
            retry = response.status_code in (408, 429) or response.status_code >= 500
            return False, f"HTTP Error {response.status_code} - Server blocked.", retry
        content = response.content
        if not content:
            return False, "Downloaded file is 0 bytes.", True
        total = len(content)
        if max_bytes and total > max_bytes:
            return False, "File exceeds size limit.", False
        with open(dest_path, "wb") as f:
            f.write(content)
        if progress_cb:
            try:
                await progress_cb(total, total)
            except Exception:
                pass
        return True, "OK", False


async def custom_download(url, dest_path, state=None, progress_cb=None, max_bytes=None):
    """
    রিটার্ন: (True, "OK") অথবা (False, কারণ)।
    ইউজার ক্যানসেল করলে (False, "__CANCELLED__") - বট এটা চিনে স্ক্যান থামিয়ে দেয়।
    """
    last_err = "Unknown error"
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        if state and state.get("cancel"):
            _safe_remove(dest_path)
            return False, "__CANCELLED__"
        try:
            try:
                ok, err, retry = await _stream_download(url, dest_path, state, progress_cb, max_bytes)
            except AttributeError:
                ok, err, retry = await _whole_download(url, dest_path, state, progress_cb, max_bytes)
        except asyncio.CancelledError:
            _safe_remove(dest_path)
            raise
        except Exception as e:
            ok, err, retry = False, f"{type(e).__name__}: {str(e)}", True

        if ok:
            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                return True, "OK"
            ok, err, retry = False, "Failed to save file.", True

        if err == "__CANCELLED__":
            _safe_remove(dest_path)
            return False, err

        last_err = err
        _safe_remove(dest_path)
        if not retry:
            return False, err
        await asyncio.sleep(3 * attempt)

    return False, f"{last_err} (failed after {DOWNLOAD_RETRIES} attempts)"
