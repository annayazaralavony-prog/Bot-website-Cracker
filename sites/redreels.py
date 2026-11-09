import re

SITE_NAME = "RedReels"
HASHTAG = "#reels"

def extract_albums(html_content):
    page_title = "RedReels Feed"
    
    # HTML ফাইল থেকে সব ধরনের ভিডিও লিংক (mp4, webm) খুঁজে বের করার রেজেক্স (Regex)
    # এটি Trending এবং GIFs উভয় সেকশনের HTML থেকেই ভিডিও লিংকগুলো স্বয়ংক্রিয়ভাবে ধরে ফেলবে।
    video_urls = re.findall(r'https?://[^\s"\'<>]+?\.(?:mp4|webm)', html_content, re.IGNORECASE)
    
    unique_urls = []
    for url in video_urls:
        # ডুপ্লিকেট লিংক বাদ দেওয়া এবং কোয়েরি প্যারামিটার রিমুভ করে লিংক ক্লিন করা
        clean_url = url.split('?')[0]
        
        # শুধুমাত্র ইউনিক লিংকগুলো যুক্ত করা হচ্ছে
        if clean_url not in unique_urls:
            unique_urls.append(clean_url)
            
    albums = []
    for idx, url in enumerate(unique_urls):
        # লিংক থেকে সুন্দর একটি টাইটেল তৈরি করা (যেমন: url/cute-cat-mobile.mp4 থেকে Cute Cat)
        title_match = re.search(r'/([^/]+)\.(?:mp4|webm)', url, re.IGNORECASE)
        raw_title = title_match.group(1) if title_match else f"Video_{idx+1}"
        title = raw_title.replace("-mobile", "").replace("-hd", "").replace("-", " ").title()
        
        # এখানে প্রতিটি ভিডিওকে একটি আলাদা 'অ্যালবাম' হিসেবে সেট করা হয়েছে।
        # এর সুবিধা হলো: bot.py একসাথে ১০০টি ভিডিও স্ক্যান করবে এবং ডাটাবেসে সেভ করবে। 
        # পরের বার স্ক্যান দিলে এই ডাটাবেস চেক করে আগের ১০০টি ভিডিও স্কিপ করে তার পরের ১০০টি ভিডিও স্ক্যান করবে।
        albums.append({
            "title": title[:100],  # ক্যাপশনে শুধু টাইটেল থাকবে
            "items": [("video", url)]
        })
        
    return page_title, albums
