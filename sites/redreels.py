import re

SITE_NAME = "RedReels"
HASHTAG = "#reels"


def _clean_html(html_content):
    text = html_content or ""
    text = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u0026", "&")
    text = text.replace("&amp;", "&")
    return text


def extract_albums(html_content):
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
        title = raw_title.replace("-mobile", "").replace("-hd", "").replace("-", " ").replace("_", " ").title()

        # প্রতিটি ভিডিও একটি আলাদা 'অ্যালবাম': প্রতি স্ক্যানে ১০০টি, পরের স্ক্যানে ডাটাবেস দেখে পরের ১০০টি
        albums.append({
            "title": title[:100],
            "items": [("video", url)]
        })

    return page_title, albums
