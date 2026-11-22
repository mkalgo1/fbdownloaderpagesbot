import io
import json
import logging
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
import zipfile
from typing import Optional, Tuple, Dict, List, Any
from http.server import BaseHTTPRequestHandler
import requests
import yt_dlp

# -------------------------------------------------------------------
# CONFIGURATION & CONSTANTS
# -------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")
logger = logging.getLogger("VercelBot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    logger.warning("BOT_TOKEN environment variable is missing! Set BOT_TOKEN in project environment.")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
URL_REGEX = re.compile(r"https?://(?:www\.)?\S+", re.IGNORECASE)
USER_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "2140565223"))

DEFAULT_FB_TARGET_PAGES = [
    "https://web.facebook.com/profile.php?id=61556328743295",
    "https://web.facebook.com/profile.php?id=100083193627339",
    "https://web.facebook.com/lavialyon",
]

LAST_FB_CHECK = 0

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as cffi_requests
    HAS_CURL_CFFI = False


def get_target_pages() -> list:
    """Returns the list of monitored Facebook pages (defaults + custom added)."""
    pages = list(DEFAULT_FB_TARGET_PAGES)
    env_pages = os.getenv("CUSTOM_FB_PAGES", "")
    if env_pages:
        for ep in env_pages.split(","):
            ep = ep.strip()
            if ep and ep not in pages:
                pages.append(ep)
    p_file = os.path.join(tempfile.gettempdir(), "monitored_fb_pages.json")
    if os.path.exists(p_file):
        try:
            with open(p_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, list):
                    for p in saved:
                        if p not in pages:
                            pages.append(p)
        except Exception:
            pass
    return pages


def add_target_page(url: str) -> bool:
    """Adds a new page URL to the monitored list and saves it."""
    pages = get_target_pages()
    if url not in pages:
        pages.append(url)
        p_file = os.path.join(tempfile.gettempdir(), "monitored_fb_pages.json")
        try:
            with open(p_file, "w", encoding="utf-8") as f:
                json.dump(pages, f, indent=2)
        except Exception:
            pass
        return True
    return False


def remove_target_page(query: str) -> Optional[str]:
    """Removes a page by 1-based index or matching URL substring."""
    pages = get_target_pages()
    removed = None
    try:
        idx = int(query.strip()) - 1
        if 0 <= idx < len(pages):
            removed = pages.pop(idx)
    except Exception:
        for p in list(pages):
            if query.strip().lower() in p.lower():
                pages.remove(p)
                removed = p
                break
    if removed:
        p_file = os.path.join(tempfile.gettempdir(), "monitored_fb_pages.json")
        try:
            with open(p_file, "w", encoding="utf-8") as f:
                json.dump(pages, f, indent=2)
        except Exception:
            pass
    return removed


def get_user_state(chat_id: int) -> dict:
    """Retrieves conversation state for a user (e.g., awaiting /add input)."""
    state_file = os.path.join(tempfile.gettempdir(), "user_states.json")
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                data = json.load(f)
                return data.get(str(chat_id), {})
        except Exception:
            pass
    return {}


def set_user_state(chat_id: int, state: str, meta: dict = None):
    """Sets conversation state for a user."""
    state_file = os.path.join(tempfile.gettempdir(), "user_states.json")
    data = {}
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                data = json.load(f)
        except Exception:
            pass
    data[str(chat_id)] = {"state": state, "meta": meta or {}, "time": int(time.time())}
    try:
        with open(state_file, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def clear_user_state(chat_id: int):
    """Clears conversation state for a user."""
    set_user_state(chat_id, "")


def resolve_facebook_page(input_str: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Intelligently extracts Facebook page credentials from:
    1. A video or reel link (detects creator page uploader_id and resolves canonical page URL)
    2. A full Facebook Page URL (e.g. facebook.com/lavialyon or profile.php?id=...)
    3. A Facebook username or handle (e.g. lavialyon or @lavialyon)
    Returns (page_url, page_name, error_message)
    """
    text = input_str.strip()
    if not text:
        return None, None, "Input cannot be empty."

    # 1. Check if input is a video or reel link
    is_video_link = any(x in text.lower() for x in ["/reel/", "/videos/", "watch", "fb.watch", "/video.php"])
    if is_video_link:
        try:
            ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(text, download=False)
                if info and "entries" in info and info["entries"]:
                    info = info["entries"][0]
                uid = info.get("uploader_id")
                uploader_name = info.get("uploader")
                if uid:
                    page_url = f"https://web.facebook.com/profile.php?id={uid}"
                    try:
                        if HAS_CURL_CFFI:
                            r = cffi_requests.get(page_url, impersonate="chrome120", allow_redirects=True, timeout=10)
                        else:
                            r = cffi_requests.get(page_url, allow_redirects=True, timeout=10)
                        if r.status_code == 200 and "facebook.com" in r.url:
                            clean_r_url = r.url.split("?")[0].rstrip("/")
                            if not any(x in clean_r_url for x in ["/login", "/recover", "/help"]):
                                page_url = clean_r_url
                    except Exception:
                        pass
                    return page_url, uploader_name or "Facebook Page", None
                elif uploader_name:
                    slug = re.sub(r"[^a-zA-Z0-9.]", "", uploader_name).lower()
                    cand_url = f"https://web.facebook.com/{slug}"
                    return cand_url, uploader_name, None
        except Exception as e:
            logger.warning(f"Failed to inspect video link: {e}")

    # 2. Check if input is a direct URL
    if text.startswith("http://") or text.startswith("https://") or "facebook.com" in text.lower():
        clean_url = text.split("?")[0]
        if "profile.php" in text:
            m_id = re.search(r"id=(\d+)", text)
            if m_id:
                clean_url = f"https://web.facebook.com/profile.php?id={m_id.group(1)}"
            else:
                clean_url = re.sub(r"https?://(?:www\.|m\.)?facebook\.com", "https://web.facebook.com", clean_url).rstrip("/")
        else:
            clean_url = re.sub(r"https?://(?:www\.|m\.)?facebook\.com", "https://web.facebook.com", clean_url).rstrip("/")
        
        try:
            if HAS_CURL_CFFI:
                r = cffi_requests.get(clean_url, impersonate="chrome120", allow_redirects=True, timeout=10)
            else:
                r = cffi_requests.get(clean_url, allow_redirects=True, timeout=10)
            p_name = "Facebook Page"
            tm = re.search(r"<title>([^<]+)</title>", r.text)
            if tm:
                raw_t = tm.group(1).replace(" | Facebook", "").replace(" - Facebook", "").strip()
                if raw_t and "log in" not in raw_t.lower() and "error" not in raw_t.lower():
                    p_name = raw_t
            return clean_url, p_name, None
        except Exception as e:
            return clean_url, "Facebook Page", None

    # 3. Handle username / slug (e.g. "lavialyon", "@lavialyon")
    slug = text.lstrip("@").strip().strip("/")
    cand_url = f"https://web.facebook.com/{slug}"
    try:
        if HAS_CURL_CFFI:
            r = cffi_requests.get(cand_url, impersonate="chrome120", allow_redirects=True, timeout=10)
        else:
            r = cffi_requests.get(cand_url, allow_redirects=True, timeout=10)
        p_name = slug
        tm = re.search(r"<title>([^<]+)</title>", r.text)
        if tm:
            raw_t = tm.group(1).replace(" | Facebook", "").replace(" - Facebook", "").strip()
            if raw_t and "log in" not in raw_t.lower() and "not found" not in raw_t.lower():
                p_name = raw_t
        return cand_url, p_name, None
    except Exception as e:
        return cand_url, slug, None


def get_subscribers() -> list:
    sub_file = os.path.join(tempfile.gettempdir(), "subscribers.json")
    subs = [USER_CHAT_ID]
    if os.path.exists(sub_file):
        try:
            with open(sub_file, "r") as f:
                loaded = json.load(f)
                if isinstance(loaded, list):
                    for s in loaded:
                        if s not in subs:
                            subs.append(s)
        except Exception:
            pass
    return subs


def add_subscriber(chat_id: int):
    subs = get_subscribers()
    if chat_id not in subs:
        subs.append(chat_id)
        sub_file = os.path.join(tempfile.gettempdir(), "subscribers.json")
        try:
            with open(sub_file, "w") as f:
                json.dump(subs, f)
        except Exception:
            pass


def get_seen_posts() -> dict:
    seen_file = os.path.join(tempfile.gettempdir(), "seen_fb_posts.json")
    if os.path.exists(seen_file):
        try:
            with open(seen_file, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_seen_post(key: str, data: dict):
    seen = get_seen_posts()
    seen[key] = data
    seen_file = os.path.join(tempfile.gettempdir(), "seen_fb_posts.json")
    try:
        with open(seen_file, "w") as f:
            json.dump(seen, f)
    except Exception:
        pass


def check_facebook_pages_sync(target_chat_ids: list = None) -> int:
    """
    Polite Facebook check pass for serverless ping trigger.
    Inspects FB_TARGET_PAGES, extracts new reels/videos/photos,
    and sends them to registered subscriber chats.
    """
    global LAST_FB_CHECK
    import time
    LAST_FB_CHECK = time.time()
    seen = get_seen_posts()
    subscribers = target_chat_ids or get_subscribers()
    new_found = 0

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": '"Not A(Brand";v="99", "Google Chrome";v="120", "Chromium";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    for page_url in get_target_pages():
        try:
            if HAS_CURL_CFFI:
                sess = cffi_requests.Session(impersonate="chrome120")
                resp = sess.get(page_url, headers=headers, timeout=15, allow_redirects=True)
            else:
                resp = cffi_requests.get(page_url, headers=headers, timeout=15, allow_redirects=True)

            if resp.status_code != 200:
                continue

            html = resp.text
            scripts = re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html)
            page_name = "Facebook Page"
            tm = re.search(r'<title>([^<]+)</title>', html)
            if tm:
                raw_t = tm.group(1).replace(" | Facebook", "").replace(" - Facebook", "").strip()
                if raw_t and "log in" not in raw_t.lower() and "error" not in raw_t.lower():
                    page_name = raw_t

            posts = []

            def parse_rec(obj):
                if isinstance(obj, dict):
                    plink = obj.get("permalink_url") or obj.get("url")
                    pid = obj.get("post_id") or obj.get("id")
                    if plink and isinstance(plink, str):
                        clean_l = plink.split("?")[0].rstrip("/")
                        is_reel = "/reel/" in clean_l
                        is_vid = "/videos/" in clean_l or "watch" in clean_l
                        is_ph = "/photo.php" in plink or "/posts/" in clean_l
                        if is_reel or is_vid or is_ph:
                            msg_t = ""
                            m_obj = obj.get("message")
                            if isinstance(m_obj, dict):
                                msg_t = m_obj.get("text", "")
                            elif isinstance(m_obj, str):
                                msg_t = m_obj
                            posts.append({
                                "id": str(pid) if pid else clean_l,
                                "url": plink,
                                "clean_url": clean_l,
                                "type": "video" if (is_reel or is_vid) else "photo",
                                "title": msg_t,
                                "page_name": page_name
                            })
                    for v in obj.values():
                        parse_rec(v)
                elif isinstance(obj, list):
                    for it in obj:
                        parse_rec(it)

            for scr in scripts:
                try:
                    data = json.loads(scr)
                    parse_rec(data)
                except Exception:
                    continue

            # First run: seed cache so we don't spam old history
            first_run = len(seen) == 0

            for p in posts:
                key = p["id"]
                clean_l = p["clean_url"]
                if key in seen or clean_l in seen:
                    continue

                seen_data = {"url": clean_l, "time": int(time.time()), "title": p["title"]}
                save_seen_post(key, seen_data)
                save_seen_post(clean_l, seen_data)

                if first_run:
                    continue

                new_found += 1
                logger.info(f"Vercel FB Checker: New post {clean_l} from {page_name}")

                # Dispatch download and delivery to subscribers
                caption_msg = f"🔔 **New {p['type'].capitalize()} Uploaded!**\n📘 **Page:** {page_name}\n"
                if p["title"]:
                    caption_msg += f"📌 {p['title'][:150]}\n"
                caption_msg += f"\n🔗 [Original Link]({p['url']})"

                for sid in subscribers:
                    try:
                        status_id = send_telegram_message(sid, caption_msg)
                        platform_dict = {"name": "Facebook", "emoji": "📘", "desc": "Facebook Video"}
                        process_url_and_reply(sid, p["url"], platform_dict, status_id)
                    except Exception as se:
                        logger.error(f"Error dispatching to {sid}: {se}")

        except Exception as pe:
            logger.error(f"Error checking page {page_url}: {pe}")

    return new_found

# -------------------------------------------------------------------
# PLATFORM DETECTOR
# -------------------------------------------------------------------
def detect_platform(url: str) -> dict:
    url_l = url.lower()
    if "instagram.com" in url_l:
        return {"name": "Instagram", "emoji": "📸", "desc": "Instagram Reel / Post"}
    elif "tiktok.com" in url_l:
        return {"name": "TikTok", "emoji": "🎵", "desc": "TikTok Video"}
    elif "youtube.com" in url_l or "youtu.be" in url_l:
        return {"name": "YouTube", "emoji": "🔴", "desc": "YouTube Video / Shorts"}
    elif "twitter.com" in url_l or "x.com" in url_l:
        return {"name": "X (Twitter)", "emoji": "🐦", "desc": "X / Twitter Media"}
    elif "reddit.com" in url_l or "redd.it" in url_l:
        return {"name": "Reddit", "emoji": "👽", "desc": "Reddit Video"}
    elif "facebook.com" in url_l or "fb.watch" in url_l:
        return {"name": "Facebook", "emoji": "📘", "desc": "Facebook Video"}
    elif "twitch.tv" in url_l:
        return {"name": "Twitch", "emoji": "🟣", "desc": "Twitch Clip"}
    elif "pinterest.com" in url_l or "pin.it" in url_l:
        return {"name": "Pinterest", "emoji": "📌", "desc": "Pinterest Media / Pin"}
    elif "vimeo.com" in url_l:
        return {"name": "Vimeo", "emoji": "🎬", "desc": "Vimeo Video"}
    elif "dailymotion.com" in url_l or "dai.ly" in url_l:
        return {"name": "Dailymotion", "emoji": "📹", "desc": "Dailymotion Video"}
    elif "snapchat.com" in url_l:
        return {"name": "Snapchat", "emoji": "👻", "desc": "Snapchat Spotlight / Story"}
    else:
        return {"name": "Web", "emoji": "🌐", "desc": "Universal Web Video"}


# -------------------------------------------------------------------
# SPECIALIZED FAST EXTRACTORS (TikTok & Snapchat)
# -------------------------------------------------------------------
def extract_snapchat_direct(url: str):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }

        # Expand shortened links (t.snapchat.com/...)
        if "t.snapchat.com" in url:
            try:
                r_exp = requests.get(url, allow_redirects=True, headers=headers, timeout=8)
                url = r_exp.url
            except Exception:
                pass

        resp = requests.get(url, headers=headers, timeout=12)
        html = resp.text

        # 1. Check for Story / Public Profile (__NEXT_DATA__) -> Completely Watermark-Free!
        m_next = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
        if m_next:
            try:
                data = json.loads(m_next.group(1))
                page_props = data.get("props", {}).get("pageProps", {})

                # Single story
                story = page_props.get("story")
                if story and story.get("snapList"):
                    for snap in story["snapList"]:
                        media_u = snap.get("snapUrls", {}).get("mediaUrl")
                        if media_u:
                            return media_u, "Snapchat Story", int(snap.get("duration", 0))

                # Curated highlights
                highlights = page_props.get("curatedHighlights", [])
                for h in highlights:
                    for snap in h.get("snapList", []):
                        media_u = snap.get("snapUrls", {}).get("mediaUrl")
                        if media_u and snap.get("snapMediaType") != 0:
                            return media_u, h.get("title", "Snapchat Story"), int(snap.get("duration", 0))
            except Exception as e:
                logger.warning(f"Next data parsing error: {e}")

        # 2. Check for exact video ID match in Spotlight
        id_m = re.search(r'spotlight/([A-Za-z0-9_-]+)', url)
        target_id = id_m.group(1) if id_m else None
        if target_id and target_id in html:
            pos = html.find(target_id)
            sub = html[max(0, pos - 500):min(len(html), pos + 2500)]
            m_v = re.findall(r'https://(?:bolt-gcdn|cf-st)\.sc-cdn\.net/[^\s"\'<>\\]+\.27\.[^\s"\'<>\\]+', sub)
            if m_v:
                clean_u = m_v[0].replace('\\u0026', '&').replace('\\"', '').replace('\\', '')
                title_m = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
                title = title_m.group(1).split(" | ")[0].strip() if title_m else "Snapchat Spotlight"
                return clean_u, title, 0

        # 3. Fallback: all matching video URLs
        m_videos = re.findall(r'https://(?:bolt-gcdn|cf-st)\.sc-cdn\.net/[^\s"\'<>]+\.27\.[^\s"\'<>]+', html)
        if not m_videos:
            m_videos = re.findall(r'videoUrl[^:]*:[^"]*"([^"]+)"', html)

        if m_videos:
            clean_u = m_videos[0].replace('\\u0026', '&').replace('\\"', '').replace('\\', '')
            title_m = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
            title = title_m.group(1).split(" | ")[0].strip() if title_m else "Snapchat Video"
            return clean_u, title, 0
    except Exception as e:
        logger.warning(f"Snapchat extraction error: {e}")
    return None, None, None


def extract_mp4_dimensions(file_path: str):
    """
    Extracts the exact native visual width and height of an MP4 file
    by reading track header (tkhd) boxes inside the moov atom.
    Accurately detects display transformation matrix rotation (90/270 deg).
    Works for all platforms (TikTok, Instagram, Snapchat, YouTube, etc.)
    in under 2 milliseconds without FFmpeg or OpenCV.
    """
    width, height = None, None
    try:
        with open(file_path, "rb") as f:
            data = f.read(4 * 1024 * 1024)
            offset = 0
            while True:
                pos = data.find(b"tkhd", offset)
                if pos == -1:
                    break
                if pos >= 4:
                    box_size = struct.unpack(">I", data[pos - 4:pos])[0]
                    if 84 <= box_size <= 256 and (pos - 4 + box_size) <= len(data):
                        w_raw, h_raw = struct.unpack(">II", data[pos - 4 + box_size - 8 : pos - 4 + box_size])
                        w = w_raw >> 16
                        h = h_raw >> 16
                        # Check transformation matrix (36 bytes prior to width/height)
                        if pos - 4 + box_size - 44 >= 0:
                            matrix = struct.unpack(">9i", data[pos - 4 + box_size - 44 : pos - 4 + box_size - 8])
                            # matrix[1] (b) or matrix[3] (c) non-zero indicates 90 or 270 degree rotation
                            if matrix[1] != 0 or matrix[3] != 0:
                                w, h = h, w
                        if w > 0 and h > 0:
                            width, height = w, h
                            break
                offset = pos + 4

            if not width:
                file_size = os.path.getsize(file_path)
                if file_size > 4 * 1024 * 1024:
                    f.seek(max(0, file_size - 2 * 1024 * 1024))
                    data_end = f.read()
                    offset = 0
                    while True:
                        pos = data_end.find(b"tkhd", offset)
                        if pos == -1:
                            break
                        if pos >= 4:
                            box_size = struct.unpack(">I", data_end[pos - 4:pos])[0]
                            if 84 <= box_size <= 256 and (pos - 4 + box_size) <= len(data_end):
                                w_raw, h_raw = struct.unpack(">II", data_end[pos - 4 + box_size - 8 : pos - 4 + box_size])
                                w = w_raw >> 16
                                h = h_raw >> 16
                                if pos - 4 + box_size - 44 >= 0:
                                    matrix = struct.unpack(">9i", data_end[pos - 4 + box_size - 44 : pos - 4 + box_size - 8])
                                    if matrix[1] != 0 or matrix[3] != 0:
                                        w, h = h, w
                                if w > 0 and h > 0:
                                    width, height = w, h
                                    break
                        offset = pos + 4
    except Exception as e:
        logger.warning(f"Could not parse MP4 dimensions: {e}")
    return width, height


def get_video_info_fast(file_path: str):
    """
    Extracts display width, height, and duration with rotation normalization.
    Uses FFprobe/FFmpeg if available; falls back to fast MP4 box inspection.
    """
    try:
        ffmpeg_bin = ensure_ffmpeg()
        ffprobe_bin = shutil.which("ffprobe")
        if not ffprobe_bin and ffmpeg_bin:
            cand = os.path.join(os.path.dirname(ffmpeg_bin), "ffprobe")
            if os.path.exists(cand):
                ffprobe_bin = cand

        if ffprobe_bin:
            cmd = [ffprobe_bin, "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", file_path]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=6)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                for s in data.get("streams", []):
                    if s.get("codec_type") == "video":
                        w = int(s.get("width", 0))
                        h = int(s.get("height", 0))
                        rot = 0
                        if s.get("tags", {}).get("rotate"):
                            try:
                                rot = int(s["tags"]["rotate"])
                            except Exception:
                                pass
                        for sd in s.get("side_data_list", []):
                            if "rotation" in sd:
                                try:
                                    rot = int(sd["rotation"])
                                except Exception:
                                    pass
                        if rot in [90, 270, -90, -270]:
                            w, h = h, w
                        dur = float(s.get("duration") or data.get("format", {}).get("duration") or 0)
                        if w > 0 and h > 0:
                            return w, h, int(dur)
    except Exception as e:
        logger.warning(f"get_video_info_fast warning: {e}")

    # Fallback to pure Python MP4 atom inspection
    w, h = extract_mp4_dimensions(file_path)
    return w, h, 0


def ensure_ffmpeg() -> str:
    """
    Ensures FFmpeg executable is available.
    1. Checks system PATH (e.g. local Windows/Linux).
    2. Checks /tmp/ffmpeg (Vercel serverless environment).
    3. If running on Linux x86_64 on Vercel, automatically downloads and extracts
       a static binary to /tmp/ffmpeg and caches it for future invocations.
    """
    path = shutil.which("ffmpeg")
    if path:
        return path

    tmp_bin = os.path.join(tempfile.gettempdir(), "ffmpeg")
    if os.path.exists(tmp_bin) and os.path.getsize(tmp_bin) > 5000000:
        if not os.access(tmp_bin, os.X_OK):
            try:
                os.chmod(tmp_bin, 0o755)
            except Exception:
                pass
        return tmp_bin

    if sys.platform.startswith("linux"):
        try:
            logger.info("Downloading static FFmpeg for Linux...")
            url = "https://github.com/ffbinaries/ffbinaries-prebuilt/releases/download/v6.1/ffmpeg-6.1-linux-64.zip"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            if r.status_code == 200:
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    z.extract("ffmpeg", tempfile.gettempdir())
                if os.path.exists(tmp_bin):
                    os.chmod(tmp_bin, 0o755)
                    logger.info("FFmpeg static binary extracted successfully to /tmp/ffmpeg")
                    return tmp_bin
        except Exception as e:
            logger.warning(f"Could not setup static ffmpeg: {e}")

    return None


def mix_image_and_audio(image_src: str, audio_src: str, output_path: str = None, headers: dict = None) -> str:
    """
    Combines a static image (photo) and an audio track into a standard H.264 MP4 video.
    Supports either local file paths or remote HTTP URLs for image and audio.
    Automatically handles odd pixel dimensions (scale=trunc(iw/2)*2:trunc(ih/2)*2).
    Outputs a lightweight MP4 with faststart suitable for Telegram and iOS Shortcuts.
    """
    ffmpeg_bin = ensure_ffmpeg()
    if not ffmpeg_bin:
        logger.warning("FFmpeg not available to mix image and audio")
        return None

    h = headers or {"User-Agent": "Mozilla/5.0"}
    local_img = image_src
    local_aud = audio_src
    created_temp_files = []

    try:
        # Download image if remote URL
        if image_src.startswith("http://") or image_src.startswith("https://"):
            ext = ".png" if ".png" in image_src.lower() else ".jpg"
            tmp_img_file = os.path.join(tempfile.gettempdir(), f"mix_img_{uuid.uuid4().hex[:6]}{ext}")
            r_img = requests.get(image_src, headers=h, timeout=12)
            if r_img.status_code != 200 or len(r_img.content) < 100:
                logger.warning(f"Failed to fetch image for mixing: status {r_img.status_code}")
                return None
            with open(tmp_img_file, "wb") as f:
                f.write(r_img.content)
            local_img = tmp_img_file
            created_temp_files.append(tmp_img_file)

        # Download audio if remote URL
        if audio_src.startswith("http://") or audio_src.startswith("https://"):
            ext = ".m4a" if ".m4a" in audio_src.lower() else ".mp3"
            tmp_aud_file = os.path.join(tempfile.gettempdir(), f"mix_aud_{uuid.uuid4().hex[:6]}{ext}")
            r_aud = requests.get(audio_src, headers=h, timeout=15)
            if r_aud.status_code != 200 or len(r_aud.content) < 100:
                logger.warning(f"Failed to fetch audio for mixing: status {r_aud.status_code}")
                return None
            with open(tmp_aud_file, "wb") as f:
                f.write(r_aud.content)
            local_aud = tmp_aud_file
            created_temp_files.append(tmp_aud_file)

        if not output_path:
            output_path = os.path.join(tempfile.gettempdir(), f"mixed_vid_{uuid.uuid4().hex[:6]}.mp4")

        cmd = [
            ffmpeg_bin, "-y",
            "-loop", "1", "-i", local_img,
            "-i", local_aud,
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-shortest",
            "-movflags", "+faststart",
            output_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info(f"Successfully mixed image and audio into MP4 ({os.path.getsize(output_path)} bytes): {output_path}")
            return output_path
        else:
            logger.warning(f"FFmpeg mixing failed (code {res.returncode}): {res.stderr.decode('utf-8', errors='ignore')[-300:]}")
            return None
    except Exception as e:
        logger.warning(f"Error in mix_image_and_audio: {e}")
        return None
    finally:
        for tf in created_temp_files:
            try:
                if os.path.exists(tf):
                    os.remove(tf)
            except Exception:
                pass


def extract_instagram_direct(url: str):
    """
    Extracts Instagram video (including pre-rendered photo with music) or high-res photo.
    Uses vxinstagram gateway and Open Graph metadata fallback.
    Returns: (media_url, media_type, title, thumbnail_url, audio_url)
    """
    try:
        m = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', url)
        if not m:
            return None, None, None, None, None
        shortcode = m.group(1)

        # 1. Query vxinstagram with TelegramBot user-agent
        vx_url = f"https://www.vxinstagram.com/reel/{shortcode}/"
        headers = {
            "User-Agent": "TelegramBot (like TwitterBot)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        r = requests.get(vx_url, headers=headers, timeout=8)
        if r.status_code == 200 and r.text:
            html = r.text
            v_matches = re.findall(r'<meta property="og:video" content="([^"]+)"', html)
            if not v_matches:
                v_matches = re.findall(r'<meta property="twitter:player:stream" content="([^"]+)"', html)

            img_matches = re.findall(r'<meta property="og:image" content="([^"]+)"', html)
            title_m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
            title = title_m.group(1) if title_m else "Instagram Media"

            if v_matches:
                vid_url = v_matches[0]
                thumb = img_matches[0] if img_matches else None
                return vid_url, "video", title, thumb, None
            elif img_matches:
                img_url = img_matches[0]
                return img_url, "image", title, img_url, None

        # 2. Direct Instagram page check
        r_ig = requests.get(f"https://www.instagram.com/p/{shortcode}/", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }, timeout=8)
        if r_ig.status_code == 200:
            og_v = re.findall(r'<meta property="og:video" content="([^"]+)"', r_ig.text)
            og_i = re.findall(r'<meta property="og:image" content="([^"]+)"', r_ig.text)
            if og_v:
                return og_v[0].replace("&amp;", "&"), "video", "Instagram Video", og_i[0] if og_i else None, None
            if og_i:
                return og_i[0].replace("&amp;", "&"), "image", "Instagram Photo", og_i[0], None

    except Exception as e:
        logger.warning(f"extract_instagram_direct failed: {e}")
    return None, None, None, None, None


def extract_tiktok_direct(url: str):
    endpoints = [
        f"https://tikwm.com/api/?url={urllib.parse.quote(url, safe=':/?=&')}",
        f"https://www.tikwm.com/api/?url={urllib.parse.quote(url, safe=':/?=&')}",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    for api_url in endpoints:
        try:
            req = requests.get(api_url, headers=headers, timeout=8)
            if req.status_code == 200:
                data = req.json()
                if data.get("code") == 0 and data.get("data"):
                    d = data["data"]
                    play_url = d.get("play") or d.get("wmplay")
                    hd_url = d.get("hdplay")
                    title = d.get("title") or "TikTok Video"
                    duration = d.get("duration", 0)
                    cover_url = d.get("cover") or d.get("origin_cover")
                    music_url = d.get("music") or (d.get("music_info") or {}).get("play")
                    images = d.get("images") or []
                    image_url = images[0] if images else None
                    if play_url or image_url:
                        return play_url, hd_url, title, duration, cover_url, image_url, music_url
        except Exception as e:
            logger.warning(f"TikWM endpoint {api_url[:30]} failed: {e}")
    return None, None, None, None, None, None, None


def extract_pinterest_direct(url: str):
    """
    Extracts direct Pinterest video (with audio/sound) or high-res photo.
    Resolves pin.it short links, retrieves direct expMp4 720p stream with full sound,
    or extracts full-resolution uncompressed photo.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "X-Pinterest-PWS-Handler": "www/[username].js"
    }

    # 1. Expand short links (pin.it/...) and get page HTML
    expanded_url = url
    page_html = ""
    try:
        r = requests.get(url, allow_redirects=True, headers=headers, timeout=8)
        expanded_url = r.url
        page_html = r.text
    except Exception as e:
        logger.warning(f"Error expanding pin.it: {e}")

    # 2. Check for direct expMp4 video with full sound directly in the web HTML
    if page_html:
        mp4_matches = re.findall(r'https://v1\.pinimg\.com/videos/iht/expMp4/[^"\'\s<>]+\.mp4', page_html)
        if mp4_matches:
            direct_mp4 = mp4_matches[0].replace('\\u0026', '&').replace('\\"', '').replace('\\', '')
            title_m = re.search(r'<title>(.*?)</title>', page_html, re.IGNORECASE)
            title = title_m.group(1).split(" | ")[0].split(" - ")[0].strip() if title_m else "Pinterest Video"
            logger.info(f"Extracted direct Pinterest MP4 with sound from HTML: {direct_mp4[:80]}")
            return direct_mp4, "video", title, 0, None, None, None

    # 3. Extract Pin ID
    pin_id_match = re.search(r"pin/(\d+)", expanded_url)
    if not pin_id_match:
        pin_id_match = re.search(r"pin/(\d+)", url)
    if not pin_id_match:
        return None, None, None, None, None, None, None

    pin_id = pin_id_match.group(1)

    # 4. Query Pinterest PinResource API
    params = {
        "source_url": f"/pin/{pin_id}/",
        "data": json.dumps({
            "options": {"field_set_key": "unauth_react_main_pin", "id": pin_id},
            "context": {}
        })
    }

    try:
        resp = requests.get("https://www.pinterest.com/resource/PinResource/get/", headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            pdata = resp.json().get("resource_response", {}).get("data", {})
            title = pdata.get("grid_title") or pdata.get("title") or pdata.get("closeup_unified_description") or "Pinterest Media"
            thumb = pdata.get("images", {}).get("orig", {}).get("url")

            # Check for direct video in 'videos'
            videos = pdata.get("videos")
            if videos and isinstance(videos, dict):
                v_list = videos.get("video_list", {})
                for q in ["V_720P", "V_EXP7", "V_480P", "V_MOBILE"]:
                    if q in v_list and v_list[q].get("url") and v_list[q]["url"].endswith(".mp4"):
                        return v_list[q]["url"], "video", title, v_list[q].get("duration", 0), v_list[q].get("width"), v_list[q].get("height"), thumb
                if "V_HLSV3_MOBILE" in v_list and v_list["V_HLSV3_MOBILE"].get("url"):
                    vhls = v_list["V_HLSV3_MOBILE"]
                    # Try deriving direct expMp4 with sound
                    cand_mp4 = vhls["url"].replace("/hls/", "/expMp4/").replace(".m3u8", "_720w.mp4")
                    try:
                        if requests.head(cand_mp4, timeout=3).status_code == 200:
                            return cand_mp4, "video", title, vhls.get("duration", 0), vhls.get("width"), vhls.get("height"), thumb
                    except Exception:
                        pass
                    return vhls["url"], "video", title, vhls.get("duration", 0), vhls.get("width"), vhls.get("height"), thumb

            # Check story_pin_data for video
            story = pdata.get("story_pin_data")
            if story and isinstance(story, dict):
                for page in story.get("pages", []):
                    for block in page.get("blocks", []):
                        if "video" in block:
                            v_list = block["video"].get("video_list", {})
                            for q in ["V_720P", "V_EXP7", "V_480P", "V_MOBILE"]:
                                if q in v_list and v_list[q].get("url") and v_list[q]["url"].endswith(".mp4"):
                                    return v_list[q]["url"], "video", title, v_list[q].get("duration", 0), v_list[q].get("width"), v_list[q].get("height"), thumb
                            if "V_HLSV3_MOBILE" in v_list and v_list["V_HLSV3_MOBILE"].get("url"):
                                vhls = v_list["V_HLSV3_MOBILE"]
                                cand_mp4 = vhls["url"].replace("/hls/", "/expMp4/").replace(".m3u8", "_720w.mp4")
                                try:
                                    if requests.head(cand_mp4, timeout=3).status_code == 200:
                                        return cand_mp4, "video", title, vhls.get("duration", 0), vhls.get("width"), vhls.get("height"), thumb
                                except Exception:
                                    pass
                                return vhls["url"], "video", title, vhls.get("duration", 0), vhls.get("width"), vhls.get("height"), thumb

            # If not a video, extract full resolution original photo
            images = pdata.get("images", {})
            if "orig" in images and images["orig"].get("url"):
                orig = images["orig"]
                return orig["url"], "image", title, 0, orig.get("width"), orig.get("height"), None

    except Exception as e:
        logger.warning(f"Pinterest API query error: {e}")

    return None, None, None, None, None, None, None


# -------------------------------------------------------------------
# IOS SHORTCUT BINARY BUILDER (.shortcut)
# -------------------------------------------------------------------
def generate_shortcut_bytes() -> bytes:
    detect_uuid = "2B5480FD-6725-45C6-9EEB-CBE2764B6256"
    download_uuid = "5F89A507-6BEE-4DCE-9FA4-DF8EC323C625"
    save_uuid = "9A1D0E44-9BD9-4F5B-9721-BC467DE7BC55"

    url_prefix = "https://botdownloaderbymk.vercel.app/api/stream?url="
    pos = len(url_prefix)

    shortcut_dict = {
        "WFWorkflowClientVersion": "2600.0.0",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowInputContentItemClasses": [
            "WFURLContentItem",
            "WFStringContentItem",
            "WFSafariWebPageContentItem",
            "WFArticleContentItem"
        ],
        "WFWorkflowIcon": {
            "WFWorkflowIconGlyphNumber": 59781,
            "WFWorkflowIconStartColor": 4282601983
        },
        "WFWorkflowActions": [
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.detect.link",
                "WFWorkflowActionParameters": {
                    "UUID": detect_uuid
                }
            },
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
                "WFWorkflowActionParameters": {
                    "UUID": download_uuid,
                    "WFHTTPMethod": "GET",
                    "WFURL": {
                        "Value": {
                            "string": f"{url_prefix}\ufffc",
                            "attachmentsByRange": {
                                f"{{{pos}, 1}}": {
                                    "Type": "ActionOutput",
                                    "OutputUUID": detect_uuid,
                                    "OutputName": "URLs"
                                }
                            }
                        },
                        "WFSerializationType": "WFTextTokenString"
                    }
                }
            },
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.savetocameraroll",
                "WFWorkflowActionParameters": {
                    "UUID": save_uuid,
                    "WFCameraRollSelectedGroup": "Recents"
                }
            }
        ]
    }
    return plistlib.dumps(shortcut_dict, fmt=plistlib.FMT_BINARY)


def render_shortcut_page() -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Downloader Mk — iOS Shortcut</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "SF Pro", "Segoe UI", Roboto, sans-serif; }}
    body {{ background: #0b0f19; color: #f3f4f6; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 24px 16px; }}
    .card {{ background: #131b2e; border: 1px solid #1f2d47; border-radius: 24px; padding: 32px 24px; max-width: 520px; width: 100%; box-shadow: 0 20px 40px rgba(0,0,0,0.5); text-align: center; }}
    .icon {{ font-size: 54px; margin-bottom: 14px; display: inline-block; }}
    h1 {{ font-size: 24px; font-weight: 700; color: #fff; margin-bottom: 8px; }}
    p.subtitle {{ font-size: 14px; color: #94a3b8; margin-bottom: 20px; line-height: 1.5; }}
    .btn-download {{ display: block; width: 100%; background: linear-gradient(135deg, #10b981, #059669); color: #fff; text-decoration: none; font-size: 16px; font-weight: 700; padding: 14px; border-radius: 14px; margin-bottom: 20px; box-shadow: 0 8px 20px rgba(16, 185, 129, 0.35); text-align: center; }}
    .copy-box {{ background: #0b1120; border: 1px dashed #3b82f6; border-radius: 14px; padding: 14px; margin-bottom: 20px; text-align: left; }}
    .copy-label {{ font-size: 11px; text-transform: uppercase; color: #60a5fa; font-weight: 700; letter-spacing: 0.5px; margin-bottom: 6px; }}
    .copy-url {{ font-family: ui-monospace, monospace; font-size: 12px; color: #38bdf8; word-break: break-all; margin-bottom: 10px; display: block; }}
    .btn {{ display: block; width: 100%; background: linear-gradient(135deg, #2563eb, #1d4ed8); color: #fff; text-decoration: none; font-size: 15px; font-weight: 600; padding: 12px; border-radius: 12px; border: none; cursor: pointer; transition: transform 0.15s, opacity 0.15s; box-shadow: 0 8px 20px rgba(37, 99, 235, 0.35); text-align: center; }}
    .btn:active {{ transform: scale(0.98); opacity: 0.9; }}
    .steps {{ background: #0f1626; border-radius: 16px; padding: 18px 16px; text-align: left; margin-top: 20px; border: 1px solid #1a253b; }}
    .step {{ display: flex; align-items: flex-start; gap: 12px; margin-bottom: 14px; font-size: 13.5px; color: #cbd5e1; line-height: 1.45; }}
    .step:last-child {{ margin-bottom: 0; }}
    .badge {{ background: #3b82f6; color: #fff; font-weight: 700; width: 22px; height: 22px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 11px; flex-shrink: 0; }}
    code {{ background: #1e293b; padding: 2px 6px; border-radius: 6px; font-size: 12px; color: #38bdf8; font-family: ui-monospace, monospace; word-break: break-all; }}
    .toast {{ display: none; margin-top: 8px; font-size: 12px; color: #4ade80; font-weight: 600; text-align: center; }}
    .callout {{ background: #1a2333; border-left: 3px solid #f59e0b; padding: 10px 12px; border-radius: 8px; margin-top: 14px; font-size: 12px; color: #fbbf24; text-align: left; line-height: 1.4; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">⚡</div>
    <h1>Downloader Mk Shortcut</h1>
    <p class="subtitle">Save Pinterest, TikTok, Instagram & more straight into your iPhone Photos app!</p>

    <a href="/downloader_mk.shortcut" class="btn-download">📲 Download Ready Shortcut (.shortcut)</a>

    <div class="copy-box">
      <div class="copy-label">Direct Download Stream URL</div>
      <span class="copy-url" id="streamUrl">https://botdownloaderbymk.vercel.app/api/stream?url=</span>
      <button class="btn" onclick="copyUrl()">📋 Copy Stream URL</button>
      <div class="toast" id="toast">✅ Copied to clipboard!</div>
    </div>
    
    <div class="steps">
      <div class="step">
        <span class="badge">1</span>
        <div><b>Option A (Download File):</b> Tap the green button above to download <code>Downloader_Mk.shortcut</code>. In Safari Downloads or Files, tap the file to add it directly to Shortcuts!</div>
      </div>
      <div class="step">
        <span class="badge">2</span>
        <div><b>Fix Untrusted / Security Issue:</b> In iPhone <b>Settings &gt; Shortcuts &gt; Advanced</b>, turn ON <b>&quot;Allow Running Scripts&quot;</b> and <b>&quot;Private Sharing&quot;</b>.</div>
      </div>
      <div class="step">
        <span class="badge">3</span>
        <div><b>Option B (Update Existing Shortcut):</b> In your Shortcuts app, open your shortcut and ensure action #2 is <b><code>Get URLs from Shortcut Input</code></b>, followed by <b><code>Get Contents of URL</code></b> with the stream URL!</div>
      </div>
    </div>

    <div class="callout">
      💡 <b>Tip:</b> When sharing from Pinterest or TikTok, select <b>Downloader Mk</b> from your Share Sheet to save videos and photos with full sound straight to your Camera Roll.
    </div>
  </div>

  <script>
    function copyUrl() {{
      const text = document.getElementById('streamUrl').innerText;
      navigator.clipboard.writeText(text).then(() => {{
        const t = document.getElementById('toast');
        t.style.display = 'block';
        setTimeout(() => {{ t.style.display = 'none'; }}, 2500);
      }});
    }}
  </script>
</body>
</html>"""


# -------------------------------------------------------------------
# TELEGRAM API HELPERS
# -------------------------------------------------------------------
def send_telegram_message(chat_id: int, text: str) -> int:
    try:
        res = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        data = res.json()
        if not data.get("ok"):
            res = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10
            )
            data = res.json()
        if data.get("ok"):
            return data["result"]["message_id"]
    except Exception as e:
        logger.error(f"sendMessage error: {e}")
    return 0


def edit_telegram_message(chat_id: int, message_id: int, text: str):
    if not message_id:
        return
    try:
        res = requests.post(
            f"{TELEGRAM_API}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        if not res.json().get("ok"):
            requests.post(
                f"{TELEGRAM_API}/editMessageText",
                json={"chat_id": chat_id, "message_id": message_id, "text": text},
                timeout=10
            )
    except Exception as e:
        logger.error(f"editMessageText error: {e}")


def delete_telegram_message(chat_id: int, message_id: int):
    if not message_id:
        return
    try:
        requests.post(
            f"{TELEGRAM_API}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=10
        )
    except Exception as e:
        logger.error(f"deleteMessage error: {e}")


# -------------------------------------------------------------------
# EXTRACTION & UPLOAD PIPELINE
# -------------------------------------------------------------------
def process_url_and_reply(chat_id: int, url: str, platform: dict, status_msg_id: int):
    logger.info(f"Processing URL: {url} for {platform['name']} (chat_id: {chat_id})")

    direct_url = None
    media_type = "video"
    title = f"{platform['name']} Media"
    duration = None
    vid_width = None
    vid_height = None
    thumbnail_url = None
    image_to_mix = None
    audio_to_mix = None
    stream_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    try:
        # 1. Specialized fast extractors for TikTok, Instagram, Snapchat & Pinterest
        if platform.get("name") == "TikTok":
            tk_play, tk_hd, tk_title, tk_dur, tk_cov, tk_img, tk_music = extract_tiktok_direct(url)
            if tk_play:
                direct_url = tk_hd or tk_play
                if tk_title:
                    title = tk_title
                if tk_dur:
                    duration = int(tk_dur)
                thumbnail_url = tk_cov
                media_type = "video"
                logger.info(f"TikWM extraction succeeded for TikTok: {direct_url[:80]}")
            elif tk_img and tk_music:
                image_to_mix = tk_img
                audio_to_mix = tk_music
                media_type = "image_with_audio"
                title = tk_title or "TikTok Photo"
                thumbnail_url = tk_cov or tk_img
                logger.info(f"TikTok photo with music detected: {image_to_mix[:60]} + {audio_to_mix[:60]}")
            elif tk_img:
                direct_url = tk_img
                media_type = "image"
                title = tk_title or "TikTok Photo"
                thumbnail_url = tk_img
        elif platform.get("name") == "Instagram":
            ig_u, ig_mtype, ig_title, ig_thumb, ig_audio = extract_instagram_direct(url)
            if ig_u:
                direct_url = ig_u
                media_type = ig_mtype
                title = ig_title or title
                thumbnail_url = ig_thumb
                if ig_audio:
                    image_to_mix = ig_u
                    audio_to_mix = ig_audio
                    media_type = "image_with_audio"
                logger.info(f"Instagram direct extraction succeeded ({media_type}): {direct_url[:80]}")
        elif platform.get("name") == "Snapchat":
            snap_u, snap_t, snap_d = extract_snapchat_direct(url)
            if snap_u:
                direct_url = snap_u
                if snap_t:
                    title = snap_t
                media_type = "video"
                logger.info(f"Snapchat direct extraction succeeded: {direct_url[:80]}")
        elif platform.get("name") == "Pinterest":
            pin_u, pin_mtype, pin_t, pin_d, pin_w, pin_h, pin_th = extract_pinterest_direct(url)
            if pin_u:
                direct_url = pin_u
                media_type = pin_mtype or "video"
                if pin_t:
                    title = pin_t
                if pin_d:
                    duration = int(pin_d)
                if pin_w:
                    vid_width = pin_w
                if pin_h:
                    vid_height = pin_h
                if pin_th:
                    thumbnail_url = pin_th
                logger.info(f"Pinterest direct extraction succeeded ({media_type}): {direct_url[:80]}")

        # 2. Universal yt-dlp extractor fallback (handles all videos AND photos)
        if not direct_url and not (image_to_mix and audio_to_mix):
            try:
                ydl_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "ignore_no_formats_error": True,
                    "format": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=h264]+bestaudio/best[vcodec^=avc1]/best[vcodec^=h264]/hd/sd/best[ext=mp4]/best",
                    "http_headers": {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-us,en;q=0.5",
                    }
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info and "entries" in info and info["entries"]:
                        info = info["entries"][0]

                if info:
                    if info.get("title"):
                        title = str(info["title"]).strip()
                    if info.get("duration"):
                        duration = int(info["duration"])
                    if info.get("thumbnail"):
                        thumbnail_url = info.get("thumbnail")
                    if info.get("width"):
                        vid_width = info.get("width")
                    if info.get("height"):
                        vid_height = info.get("height")

                    chosen_format = None
                    audio_fmt = None
                    if "formats" in info and info["formats"]:
                        # 1. Prioritize native H.264/AVC1 streams or Facebook HD/SD progressive streams
                        for f in reversed(info["formats"]):
                            fid = str(f.get("format_id", "")).lower()
                            vc = str(f.get("vcodec", "")).lower()
                            if f.get("url") and (fid in ["hd", "sd"] or vc.startswith(("avc1", "h264"))):
                                chosen_format = f
                                direct_url = f["url"]
                                media_type = "video"
                                break
                        # 2. Next, any MP4 stream with both video and audio
                        if not direct_url:
                            for f in reversed(info["formats"]):
                                if f.get("url") and f.get("ext") == "mp4" and f.get("vcodec") != "none" and f.get("acodec") != "none":
                                    chosen_format = f
                                    direct_url = f["url"]
                                    media_type = "video"
                                    break
                        if not direct_url:
                            for f in reversed(info["formats"]):
                                if f.get("url") and (f.get("ext") == "mp4" or f.get("vcodec") != "none"):
                                    chosen_format = f
                                    direct_url = f["url"]
                                    media_type = "video"
                                    break
                        for f in reversed(info["formats"]):
                            if f.get("url") and f.get("acodec") != "none":
                                audio_fmt = f
                                break

                    # If no video format found, inspect thumbnails for photo / image posts
                    if not direct_url:
                        img_cand = None
                        if info.get("thumbnails"):
                            img_cand = info["thumbnails"][-1].get("url")
                        elif info.get("url"):
                            img_cand = info.get("url")

                        if img_cand and audio_fmt:
                            image_to_mix = img_cand
                            audio_to_mix = audio_fmt["url"]
                            media_type = "image_with_audio"
                            logger.info(f"yt-dlp extracted image + audio track for mixing: {image_to_mix[:60]}")
                        elif img_cand:
                            direct_url = img_cand
                            media_type = "image"

                    if chosen_format and chosen_format.get("http_headers"):
                        stream_headers.update(chosen_format["http_headers"])
                    elif info.get("http_headers"):
                        stream_headers.update(info["http_headers"])
                    elif "instagram.com" in url.lower():
                        stream_headers["Referer"] = "https://www.instagram.com/"
                    elif "tiktok.com" in url.lower():
                        stream_headers["Referer"] = "https://www.tiktok.com/"
            except Exception as ydl_err:
                logger.warning(f"Universal yt-dlp extractor notice: {ydl_err}")
                if "instagram.com" in url.lower() and not direct_url and not (image_to_mix and audio_to_mix):
                    ig_u, ig_mtype, ig_title, ig_thumb, ig_audio = extract_instagram_direct(url)
                    if ig_u:
                        direct_url = ig_u
                        media_type = ig_mtype
                        title = ig_title or title
                        thumbnail_url = ig_thumb
                        if ig_audio:
                            image_to_mix = ig_u
                            audio_to_mix = ig_audio
                            media_type = "image_with_audio"

        # Double check media_type from URL if still marked video
        if direct_url and any(ext in direct_url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            media_type = "image"

        if not direct_url and not (image_to_mix and audio_to_mix):
            edit_telegram_message(
                chat_id,
                status_msg_id,
                f"❌ Could not extract media from {platform['name']}. Post may be private or protected."
            )
            return

        tmp_file = None

        # 3. Handle image with audio: Mix photo and audio track into an MP4 video!
        if media_type == "image_with_audio" or (image_to_mix and audio_to_mix):
            edit_telegram_message(
                chat_id,
                status_msg_id,
                f"🎛️ **Detected photo with sound! Mixing photo & audio track...**"
            )
            mixed_mp4 = mix_image_and_audio(image_to_mix, audio_to_mix, headers=stream_headers)
            if mixed_mp4 and os.path.exists(mixed_mp4):
                tmp_file = mixed_mp4
                media_type = "video"
                thumbnail_url = image_to_mix
            else:
                # Fallback to image if mixing failed
                direct_url = image_to_mix
                media_type = "image"

        # If not mixed, download the direct media stream
        if not tmp_file:
            edit_telegram_message(
                chat_id,
                status_msg_id,
                f"📥 **Downloading {platform['emoji']} {platform['name']} {media_type}...**"
            )

            is_hls = ".m3u8" in direct_url
            if is_hls and media_type == "video":
                tmp_file = os.path.join(tempfile.gettempdir(), f"vid_{uuid.uuid4().hex[:6]}.mp4")
                ydl_hls_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "format": "bestvideo/bv/best",
                    "outtmpl": tmp_file.replace(".mp4", "") + ".%(ext)s"
                }
                with yt_dlp.YoutubeDL(ydl_hls_opts) as ydl_hls:
                    ydl_hls.download([direct_url])
                if not os.path.exists(tmp_file):
                    base = tmp_file.replace(".mp4", "")
                    for ext in [".mp4", ".mkv", ".ts"]:
                        if os.path.exists(base + ext):
                            tmp_file = base + ext
                            break
            else:
                file_ext = ".jpg" if media_type == "image" else ".mp4"
                tmp_file = os.path.join(tempfile.gettempdir(), f"media_{uuid.uuid4().hex[:6]}{file_ext}")
                is_protected_tiktok = platform.get("name") == "TikTok" and direct_url and ("v19-webapp" in direct_url or "tiktok.com/video/" in direct_url)
                
                downloaded = False
                if not is_protected_tiktok:
                    try:
                        download_resp = requests.get(direct_url, headers=stream_headers, stream=True, timeout=30)
                        download_resp.raise_for_status()

                        total_bytes = 0
                        with open(tmp_file, "wb") as f:
                            for chunk in download_resp.iter_content(chunk_size=1024 * 256):
                                if chunk:
                                    f.write(chunk)
                                    total_bytes += len(chunk)
                                    if total_bytes > 48 * 1024 * 1024:
                                        break

                        if total_bytes > 48 * 1024 * 1024:
                            if os.path.exists(tmp_file):
                                os.remove(tmp_file)
                            edit_telegram_message(
                                chat_id,
                                status_msg_id,
                                f"⚠️ Media size ({total_bytes // (1024 * 1024)} MB) exceeds Telegram's 50 MB upload limit."
                            )
                            return
                        if os.path.exists(tmp_file) and os.path.getsize(tmp_file) > 100:
                            downloaded = True
                    except Exception as dl_err:
                        logger.warning(f"Direct stream download failed ({dl_err}); falling back to yt-dlp native download")

                # Fallback to yt-dlp native download if requests failed or protected CDN
                if not downloaded and media_type == "video":
                    try:
                        ydl_down_opts = {
                            "quiet": True,
                            "no_warnings": True,
                            "noplaylist": True,
                            "format": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=h264]+bestaudio/best[vcodec^=avc1]/best[vcodec^=h264]/hd/sd/best[ext=mp4]/best",
                            "outtmpl": tmp_file.replace(".mp4", "") + ".%(ext)s"
                        }
                        with yt_dlp.YoutubeDL(ydl_down_opts) as ydl_down:
                            ydl_down.download([url])
                        if not os.path.exists(tmp_file):
                            base = tmp_file.replace(".mp4", "")
                            for ext in [".mp4", ".mkv", ".webm", ".ts"]:
                                if os.path.exists(base + ext):
                                    tmp_file = base + ext
                                    break
                    except Exception as ydl_down_err:
                        logger.warning(f"yt-dlp fallback download failed: {ydl_down_err}")

        if not os.path.exists(tmp_file) or os.path.getsize(tmp_file) == 0:
            edit_telegram_message(
                chat_id,
                status_msg_id,
                f"❌ Downloaded stream was empty or blocked by {platform['name']}."
            )
            return

        # 4. Verify actual file binary magic bytes to determine photo vs video
        with open(tmp_file, "rb") as check_f:
            header_sample = check_f.read(16)

        if header_sample.startswith(b"\xff\xd8\xff") or header_sample.startswith(b"\x89PNG") or b"WEBP" in header_sample:
            media_type = "image"
        elif len(header_sample) >= 8 and header_sample[4:8] == b"ftyp":
            media_type = "video"

        # 5. Faststart conversion & video metadata inspection
        final_video_file = tmp_file
        thumb_bytes = None
        created_temp_files = [tmp_file]

        if media_type == "video":
            ffmpeg_bin = ensure_ffmpeg()
            if ffmpeg_bin and os.path.exists(tmp_file) and os.path.getsize(tmp_file) > 0:
                # Apply faststart: moves moov atom to front for instant streaming without download prompts
                fast_out = os.path.join(tempfile.gettempdir(), f"fast_{uuid.uuid4().hex[:6]}.mp4")
                try:
                    cmd_fast = [ffmpeg_bin, "-y", "-i", tmp_file, "-c", "copy", "-movflags", "+faststart", fast_out]
                    res_fast = subprocess.run(cmd_fast, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                    if res_fast.returncode == 0 and os.path.exists(fast_out) and os.path.getsize(fast_out) > 0:
                        final_video_file = fast_out
                        created_temp_files.append(fast_out)
                except Exception as fe:
                    logger.warning(f"Faststart remux warning: {fe}")

                # Extract 1 frame directly from the actual video to guarantee 100% matching aspect ratio & orientation
                th_out = os.path.join(tempfile.gettempdir(), f"th_{uuid.uuid4().hex[:6]}.jpg")
                try:
                    cmd_th = [ffmpeg_bin, "-y", "-ss", "00:00:00.200", "-i", final_video_file, "-vframes", "1", "-q:v", "2", th_out]
                    res_th = subprocess.run(cmd_th, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                    if res_th.returncode == 0 and os.path.exists(th_out) and os.path.getsize(th_out) > 100:
                        with open(th_out, "rb") as tf:
                            thumb_bytes = tf.read()
                        created_temp_files.append(th_out)
                except Exception as te:
                    logger.warning(f"Frame thumbnail extraction warning: {te}")

            # Inspect display dimensions & duration
            vid_w, vid_h, vid_d = get_video_info_fast(final_video_file)
            if vid_w and vid_h:
                vid_width, vid_height = vid_w, vid_h
            if vid_d:
                duration = vid_d

            # Ensure width & height are never None to prevent Telegram 320x320 square crop fallback
            if not vid_width or not vid_height:
                w_cand, h_cand = extract_mp4_dimensions(final_video_file)
                if w_cand and h_cand:
                    vid_width, vid_height = w_cand, h_cand

        # 6. Upload to Telegram (Photo vs Video)
        if media_type == "image":
            edit_telegram_message(
                chat_id,
                status_msg_id,
                f"🚀 **Uploading {platform['name']} photo to chat...**"
            )
            caption_text = f"🖼️ {platform['emoji']} {platform['name']}\n\n📌 {title[:80]}"
            with open(tmp_file, "rb") as pf:
                upload_res = requests.post(
                    f"{TELEGRAM_API}/sendPhoto",
                    data={"chat_id": chat_id, "caption": caption_text},
                    files={"photo": (os.path.basename(tmp_file), pf, "image/jpeg")},
                    timeout=40
                )
        else:
            edit_telegram_message(
                chat_id,
                status_msg_id,
                f"🚀 **Uploading {platform['name']} video to chat...**"
            )
            caption_text = f"🎬 {platform['emoji']} {platform['name']}\n\n📌 {title[:80]}"
            send_data = {
                "chat_id": chat_id,
                "caption": caption_text,
                "supports_streaming": True,
            }
            if duration:
                send_data["duration"] = int(duration)
            if vid_width and vid_height:
                send_data["width"] = int(vid_width)
                send_data["height"] = int(vid_height)

            files = {"video": (os.path.basename(final_video_file), None, "video/mp4")}
            with open(final_video_file, "rb") as vf:
                files["video"] = (os.path.basename(final_video_file), vf, "video/mp4")
                if thumb_bytes:
                    files["thumbnail"] = ("thumb.jpg", thumb_bytes, "image/jpeg")

                upload_res = requests.post(
                    f"{TELEGRAM_API}/sendVideo",
                    data=send_data,
                    files=files,
                    timeout=50
                )

        for cf in created_temp_files:
            try:
                if os.path.exists(cf):
                    os.remove(cf)
            except Exception:
                pass

        upload_data = upload_res.json()
        logger.info(f"Telegram upload result: {upload_data.get('ok')} - desc: {upload_data.get('description')}")

        if upload_data.get("ok"):
            delete_telegram_message(chat_id, status_msg_id)
        else:
            err_desc = upload_data.get("description", "Unknown Telegram error")
            edit_telegram_message(chat_id, status_msg_id, f"❌ Telegram upload error: {err_desc}")

    except yt_dlp.utils.DownloadError as de:
        clean_err = str(de).split(";")[0].replace("ERROR:", "").strip()
        edit_telegram_message(chat_id, status_msg_id, f"❌ Extraction error: {clean_err[:120]}")
    except Exception as e:
        logger.error(f"Error handling media: {e}", exc_info=True)
        edit_telegram_message(
            chat_id,
            status_msg_id,
            f"❌ Error downloading media: {str(e)[:100]}"
        )


# -------------------------------------------------------------------
# VERCEL SERVERLESS HTTP HANDLER
# -------------------------------------------------------------------
class handler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            self._handle_get()
        except Exception as e:
            logger.error(f"Unhandled GET exception: {e}", exc_info=True)
            self._send_json(500, {"error": "Server internal error", "detail": str(e)})

    def _handle_get(self):
        parsed_url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed_url.query)
        action = query.get("action", [""])[0].lower()
        full_path = self.path.lower()

        # Direct shortcut file download
        if action == "shortcut" or "downloader_mk.shortcut" in full_path or full_path.endswith(".shortcut"):
            shortcut_data = generate_shortcut_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-apple-shortcut")
            self.send_header("Content-Disposition", 'attachment; filename="Downloader_Mk.shortcut"')
            self.send_header("Content-Length", str(len(shortcut_data)))
            self.end_headers()
            self.wfile.write(shortcut_data)
            return

        # Interactive Shortcut Webpage
        if action == "page" or "shortcut" in full_path or "ios" in full_path:
            html = render_shortcut_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        # Direct Video/Media Stream for iOS Shortcut (Saves straight to iPhone Camera Roll without Telegram)
        if action == "stream" or "stream" in full_path or action == "download" or "download" in full_path:
            raw_url = (
                query.get("url", [""])[0]
                or query.get("link", [""])[0]
                or query.get("u", [""])[0]
                or query.get("text", [""])[0]
                or query.get("q", [""])[0]
                or query.get("input", [""])[0]
            )

            # If parameter was missing or empty, search full decoded path and query string
            if not raw_url:
                full_decoded = urllib.parse.unquote(self.path)
                m_all = URL_REGEX.search(full_decoded)
                if m_all:
                    raw_url = m_all.group(0)

            quality_raw = query.get("quality", ["best"])[0].lower()
            m_q = re.search(r"\d+", quality_raw)
            q_height = int(m_q.group(0)) if m_q else None

            m = URL_REGEX.search(raw_url)
            target_url = m.group(0) if m else raw_url.strip()

            if not target_url:
                self._send_json(400, {
                    "error": "Missing 'url' query parameter",
                    "tip": "In your iOS Shortcut, add 'Get URLs from Shortcut Input' before 'Get Contents of URL'."
                })
                return

            platform = detect_platform(target_url)
            direct_url = None
            media_type = "video"
            image_to_mix = None
            audio_to_mix = None
            stream_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }

            if platform.get("name") == "TikTok":
                tk_play, tk_hd, _, _, _, tk_img, tk_music = extract_tiktok_direct(target_url)
                if q_height and q_height >= 1080 and tk_hd:
                    direct_url = tk_hd
                elif tk_play:
                    direct_url = tk_play
                elif tk_img and tk_music:
                    image_to_mix = tk_img
                    audio_to_mix = tk_music
                    media_type = "image_with_audio"
                elif tk_img:
                    direct_url = tk_img
                    media_type = "image"
            elif platform.get("name") == "Instagram":
                ig_u, ig_mtype, _, _, ig_audio = extract_instagram_direct(target_url)
                if ig_u:
                    direct_url = ig_u
                    media_type = ig_mtype
                    if ig_audio:
                        image_to_mix = ig_u
                        audio_to_mix = ig_audio
                        media_type = "image_with_audio"
            elif platform.get("name") == "Snapchat":
                snap_u, _, _ = extract_snapchat_direct(target_url)
                if snap_u:
                    direct_url = snap_u
            elif platform.get("name") == "Pinterest":
                pin_u, pin_mtype, _, _, _, _, _ = extract_pinterest_direct(target_url)
                if pin_u:
                    direct_url = pin_u
                    media_type = pin_mtype or "video"

            if not direct_url and not (image_to_mix and audio_to_mix):
                try:
                    fmt_str = (
                        f"best[height<={q_height}][ext=mp4][acodec!=none]/best[height<={q_height}][ext=mp4]/best[height<={q_height}]/best/bestvideo/bv/b"
                        if q_height
                        else "best[ext=mp4][acodec!=none]/best[ext=mp4]/best/bestvideo/bv/b"
                    )
                    ydl_opts = {
                        "quiet": True,
                        "no_warnings": True,
                        "noplaylist": True,
                        "ignore_no_formats_error": True,
                        "format": fmt_str,
                        "http_headers": {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        }
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(target_url, download=False)
                        if info and "entries" in info and info["entries"]:
                            info = info["entries"][0]

                    chosen_format = None
                    audio_fmt = None
                    if info:
                        if "formats" in info and info["formats"]:
                            if q_height:
                                for f in reversed(info["formats"]):
                                    if f.get("url") and f.get("ext") == "mp4" and f.get("vcodec") != "none" and f.get("acodec") != "none" and f.get("height") and f.get("height") <= q_height:
                                        chosen_format = f
                                        direct_url = f["url"]
                                        break
                            if not direct_url:
                                for f in reversed(info["formats"]):
                                    if f.get("url") and f.get("ext") == "mp4" and f.get("vcodec") != "none" and f.get("acodec") != "none":
                                        chosen_format = f
                                        direct_url = f["url"]
                                        break
                            if not direct_url:
                                for f in reversed(info["formats"]):
                                    if f.get("url") and (f.get("ext") == "mp4" or f.get("vcodec") != "none"):
                                        chosen_format = f
                                        direct_url = f["url"]
                                        break
                            for f in reversed(info["formats"]):
                                if f.get("url") and f.get("acodec") != "none":
                                    audio_fmt = f
                                    break

                        # If no video stream, check for image/photo thumbnails (Instagram, Pinterest, X)
                        if not direct_url:
                            img_cand = None
                            if info.get("thumbnails"):
                                img_cand = info["thumbnails"][-1].get("url")
                            elif info.get("url"):
                                img_cand = info.get("url")

                            if img_cand and audio_fmt:
                                image_to_mix = img_cand
                                audio_to_mix = audio_fmt["url"]
                                media_type = "image_with_audio"
                            elif img_cand:
                                direct_url = img_cand
                                media_type = "image"

                        if chosen_format and chosen_format.get("http_headers"):
                            stream_headers.update(chosen_format["http_headers"])
                        elif info.get("http_headers"):
                            stream_headers.update(info["http_headers"])
                        elif "instagram.com" in target_url.lower():
                            stream_headers["Referer"] = "https://www.instagram.com/"
                        elif "tiktok.com" in target_url.lower():
                            stream_headers["Referer"] = "https://www.tiktok.com/"
                except Exception as ydl_err:
                    logger.warning(f"yt-dlp extraction failed: {ydl_err}")
                    if "instagram.com" in target_url.lower() and not direct_url and not (image_to_mix and audio_to_mix):
                        ig_u, ig_mtype, _, _, ig_audio = extract_instagram_direct(target_url)
                        if ig_u:
                            direct_url = ig_u
                            media_type = ig_mtype
                            if ig_audio:
                                image_to_mix = ig_u
                                audio_to_mix = ig_audio
                                media_type = "image_with_audio"

            # If image with audio: Mix photo and audio track into an MP4 and stream directly to iOS!
            if (media_type == "image_with_audio" or (image_to_mix and audio_to_mix)) and not direct_url:
                mixed_file = mix_image_and_audio(image_to_mix, audio_to_mix, headers=stream_headers)
                if mixed_file and os.path.exists(mixed_file):
                    with open(mixed_file, "rb") as vf:
                        v_data = vf.read()
                    try:
                        os.remove(mixed_file)
                    except Exception:
                        pass
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Disposition", 'attachment; filename="audio_photo.mp4"')
                    self.send_header("Content-Length", str(len(v_data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(v_data)
                    return
                elif image_to_mix:
                    direct_url = image_to_mix
                    media_type = "image"

            if not direct_url:
                self._send_json(404, {"error": "Could not extract media stream from URL"})
                return

            # If client requested JSON format
            if query.get("format", [""])[0].lower() == "json":
                self._send_json(200, {
                    "status": "success",
                    "url": direct_url,
                    "platform": platform["name"],
                    "media_type": media_type
                })
                return

            # Special handling for HLS (.m3u8) streams or protected TikTok streams:
            # TikTok internal CDN links (v19-webapp-prime) require cookies and return 403 to iOS Shortcuts.
            # Apple Shortcuts 'Get Contents of URL' downloads .m3u8 as a text file which Photos cannot save.
            # In these cases, download or convert to MP4 directly on the server and stream directly to iOS if <= 4.5 MB!
            is_protected_tiktok = platform.get("name") == "TikTok" and direct_url and ("v19-webapp" in direct_url or "tiktok.com/video/" in direct_url)
            is_hls = direct_url and ".m3u8" in direct_url

            if (is_hls or is_protected_tiktok) and media_type == "video":
                try:
                    tmp_stream = os.path.join(tempfile.gettempdir(), f"stream_{uuid.uuid4().hex[:6]}.mp4")
                    ydl_stream_opts = {
                        "quiet": True,
                        "no_warnings": True,
                        "format": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=h264]+bestaudio/best[vcodec^=avc1]/best[vcodec^=h264]/hd/sd/best[ext=mp4]/best",
                        "outtmpl": tmp_stream.replace(".mp4", "") + ".%(ext)s"
                    }
                    with yt_dlp.YoutubeDL(ydl_stream_opts) as ydl_s:
                        ydl_s.download([target_url if is_protected_tiktok else direct_url])

                    real_mp4 = tmp_stream
                    if not os.path.exists(real_mp4):
                        base = tmp_stream.replace(".mp4", "")
                        for ext in [".mp4", ".mkv", ".ts", ".webm"]:
                            if os.path.exists(base + ext):
                                real_mp4 = base + ext
                                break

                    if os.path.exists(real_mp4):
                        fsize = os.path.getsize(real_mp4)
                        if 0 < fsize <= 4400000:
                            with open(real_mp4, "rb") as vf:
                                v_data = vf.read()
                            try:
                                os.remove(real_mp4)
                            except Exception:
                                pass
                            self.send_response(200)
                            self.send_header("Content-Type", "video/mp4")
                            self.send_header("Content-Disposition", 'attachment; filename="media.mp4"')
                            self.send_header("Content-Length", str(len(v_data)))
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.end_headers()
                            self.wfile.write(v_data)
                            return
                except Exception as s_err:
                    logger.warning(f"Failed to stream converted/downloaded video: {s_err}")

            # Direct 302 Redirect to high-speed CDN URL (Photos, Videos)
            # Apple Shortcuts 'Get Contents of URL' follows 302 redirects automatically and saves directly to Photos!
            self.send_response(302)
            self.send_header("Location", direct_url)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            return

        # Direct iOS Shortcut Share Trigger (GET method: /api/share?url=...&chat_id=...)
        if action == "share" or "share" in full_path:
            raw_url = query.get("url", [""])[0]
            chat_id = int(query.get("chat_id", [str(USER_CHAT_ID)])[0])

            m = URL_REGEX.search(raw_url)
            target_url = m.group(0) if m else raw_url.strip()

            if not target_url:
                self._send_json(400, {"error": "Missing 'url' query parameter"})
                return

            platform = detect_platform(target_url)
            status_id = send_telegram_message(
                chat_id,
                f"📲 **Received via iOS Shortcut**\n\n🔎 **Platform:** {platform['emoji']} *{platform['desc']}*\n⏳ *Extracting media stream...*"
            )
            process_url_and_reply(chat_id, target_url, platform, status_id)
            self._send_json(200, {
                "status": "success",
                "message": "Video sent to your Telegram!",
                "platform": platform["name"]
            })
            return

        if action == "ping" or "ping" in full_path:
            import time
            global LAST_FB_CHECK
            new_posts = 0
            if time.time() - LAST_FB_CHECK > 180:
                try:
                    new_posts = check_facebook_pages_sync()
                except Exception as fbe:
                    logger.warning(f"FB check on ping error: {fbe}")

            self._send_json(200, {
                "status": "pong",
                "message": "Bot is awake and running 24/7 on Vercel",
                "service": "Universal Media Downloader & Facebook Page Auto-Checker Bot",
                "bot": "@fbdownloaderpagesvidsbot",
                "ping_url": "https://fbdownloaderpagesbot.vercel.app/ping",
                "monitored_pages": get_target_pages(),
                "subscribers_count": len(get_subscribers()),
                "new_posts_found": new_posts,
                "last_check_timestamp": LAST_FB_CHECK
            })
            return

        if action == "health" or "health" in full_path:
            self._send_json(200, {"status": "healthy"})
            return

        if action == "setup" or "setup" in full_path:
            webhook_url = "https://fbdownloaderpagesbot.vercel.app/api/webhook"
            res = requests.post(f"{TELEGRAM_API}/setWebhook", json={"url": webhook_url}, timeout=10)
            self._send_json(200, {
                "action": "setWebhook",
                "target_url": webhook_url,
                "telegram_response": res.json()
            })
            return

        self._send_json(200, {
            "status": "online",
            "service": "Facebook Auto-Tracker & Downloader Bot",
            "bot_username": "@fbdownloaderpagesvidsbot",
            "endpoints": {
                "ping": "https://fbdownloaderpagesbot.vercel.app/ping",
                "setup_webhook": "https://fbdownloaderpagesbot.vercel.app/setup"
            }
        })

    def do_POST(self):
        try:
            self._handle_post()
        except Exception as e:
            logger.error(f"Unhandled POST exception: {e}", exc_info=True)
            self._send_json(500, {"error": "Server internal error", "detail": str(e)})

    def _handle_post(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to parse POST JSON: {e}")
            self._send_json(400, {"error": "Invalid JSON"})
            return

        raw_path = self.headers.get("x-matched-path") or self.path
        path = raw_path.split("?")[0].rstrip("/").lower()

        # Handle direct share from iOS Shortcut (POST method)
        if "share" in path or ("url" in payload and "message" not in payload):
            raw_url = payload.get("url", "")
            chat_id = int(payload.get("chat_id", USER_CHAT_ID))

            m = URL_REGEX.search(raw_url)
            target_url = m.group(0) if m else raw_url.strip()

            if not target_url:
                self._send_json(400, {"error": "Missing 'url' field"})
                return

            platform = detect_platform(target_url)
            status_id = send_telegram_message(
                chat_id,
                f"📲 **Received via iOS Shortcut**\n\n🔎 **Platform:** {platform['emoji']} *{platform['desc']}*\n⏳ *Extracting media stream...*"
            )
            process_url_and_reply(chat_id, target_url, platform, status_id)
            self._send_json(200, {
                "status": "success",
                "message": "Video sent to your Telegram!",
                "platform": platform["name"]
            })
            return

        # Otherwise handle standard Telegram Webhook update
        update = payload
        message = update.get("message") or update.get("channel_post")
        if not message:
            self._send_json(200, {"status": "ignored_no_message"})
            return

        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "")

        if not chat_id or not text:
            self._send_json(200, {"status": "ignored_no_text"})
            return

        # 0. Check for cancel command or active conversational state
        user_st = get_user_state(chat_id)
        current_state = user_st.get("state")

        if text.strip().lower() in ["/cancel", "cancel"]:
            if current_state:
                clear_user_state(chat_id)
                send_telegram_message(chat_id, "❌ Operation cancelled.")
                self._send_json(200, {"status": "cancel_handled"})
                return

        # 1. Handle state: awaiting_add_page (user previously called /add and is replying with link/name/video)
        if current_state == "awaiting_add_page":
            clear_user_state(chat_id)
            status_id = send_telegram_message(chat_id, "🔍 **Detecting Facebook page credentials from your input...**")
            page_url, page_name, err = resolve_facebook_page(text)
            if page_url:
                add_target_page(page_url)
                all_pages = get_target_pages()
                pages_list = "\n".join([f"{idx+1}. `{p}`" for idx, p in enumerate(all_pages)])
                edit_telegram_message(
                    chat_id,
                    status_id,
                    f"✅ **Successfully Added to Auto-Grab Monitor!**\n\n"
                    f"📘 **Page Name:** *{page_name}*\n"
                    f"🔗 **Page URL:** `{page_url}`\n\n"
                    f"The bot is now auto-tracking this page! Whenever a new video, reel, or photo is uploaded, "
                    f"it will automatically download and deliver it directly to this chat in smooth HD H.264.\n\n"
                    f"📋 **Current Monitored Pages ({len(all_pages)}):**\n"
                    f"{pages_list}"
                )
            else:
                edit_telegram_message(
                    chat_id,
                    status_id,
                    f"❌ **Could not detect a Facebook page from your input.**\n\n"
                    f"Detail: {err or 'Invalid page link, username, or inaccessible video.'}\n\n"
                    f"💡 *Tip: You can send a direct page URL (`https://facebook.com/...`), page username (`lavialyon`), or any video/reel link from that page!*"
                )
            self._send_json(200, {"status": "add_state_handled"})
            return

        # 2. Handle /add command
        if text.strip().startswith(("/add", "/addpage")):
            parts = text.strip().split(maxsplit=1)
            if len(parts) > 1:
                # User provided the argument inline: /add <url or name or video>
                raw_input = parts[1].strip()
                status_id = send_telegram_message(chat_id, "🔍 **Detecting Facebook page credentials...**")
                page_url, page_name, err = resolve_facebook_page(raw_input)
                if page_url:
                    add_target_page(page_url)
                    all_pages = get_target_pages()
                    pages_list = "\n".join([f"{idx+1}. `{p}`" for idx, p in enumerate(all_pages)])
                    edit_telegram_message(
                        chat_id,
                        status_id,
                        f"✅ **Successfully Added to Auto-Grab Monitor!**\n\n"
                        f"📘 **Page Name:** *{page_name}*\n"
                        f"🔗 **Page URL:** `{page_url}`\n\n"
                        f"The bot is now auto-tracking this page! Whenever a new video, reel, or photo is uploaded, "
                        f"it will automatically download and deliver it directly to this chat in smooth HD H.264.\n\n"
                        f"📋 **Current Monitored Pages ({len(all_pages)}):**\n"
                        f"{pages_list}"
                    )
                else:
                    edit_telegram_message(
                        chat_id,
                        status_id,
                        f"❌ **Could not detect Facebook page credentials.**\nDetail: {err or 'Invalid link or username'}"
                    )
            else:
                # User sent /add alone -> prompt user for the page
                set_user_state(chat_id, "awaiting_add_page")
                send_telegram_message(
                    chat_id,
                    "➕ **Add Facebook Page to Auto-Grab Monitor**\n\n"
                    "What page would you like to add to the auto-grab list? You can reply with any of these:\n\n"
                    "1️⃣ **Page Link:** e.g. `https://facebook.com/pagename` or `https://facebook.com/profile.php?id=...`\n"
                    "2️⃣ **Page Username / Slug:** e.g. `lavialyon` or `@lavialyon`\n"
                    "3️⃣ **Any Video / Reel Link from that page:** e.g. `https://facebook.com/reel/123456...`\n"
                    "    *(The bot will automatically detect the creator page credentials!)*\n\n"
                    "👉 *Send your page or video link now, or send `/cancel` to abort.*"
                )
            self._send_json(200, {"status": "add_command_handled"})
            return

        # 3. Handle /del or /delpage command
        if text.strip().startswith(("/del", "/delpage", "/remove")):
            parts = text.strip().split(maxsplit=1)
            all_pages = get_target_pages()
            if len(parts) > 1:
                target = parts[1].strip()
                removed = remove_target_page(target)
                if removed:
                    all_pages = get_target_pages()
                    pages_list = "\n".join([f"{idx+1}. `{p}`" for idx, p in enumerate(all_pages)])
                    send_telegram_message(
                        chat_id,
                        f"🗑 **Removed from Auto-Grab Monitor:**\n`{removed}`\n\n"
                        f"📋 **Remaining Monitored Pages ({len(all_pages)}):**\n{pages_list}"
                    )
                else:
                    send_telegram_message(chat_id, f"❌ Page matching `{target}` not found in monitored list.")
            else:
                pages_list = "\n".join([f"{idx+1}. `{p}`" for idx, p in enumerate(all_pages)])
                send_telegram_message(
                    chat_id,
                    f"🗑 **Remove a Monitored Page**\n\n"
                    f"Send `/del <number>` to remove a page (e.g. `/del 1`):\n\n"
                    f"{pages_list}"
                )
            self._send_json(200, {"status": "del_command_handled"})
            return

        # 4. Handle /start command
        if text.strip().startswith("/start"):
            add_subscriber(chat_id)
            all_pages = get_target_pages()
            pages_list = "\n".join([f"• `{p}`" for p in all_pages])
            send_telegram_message(
                chat_id,
                "🤖 **Facebook Auto-Checker & Media Downloader Bot**\n\n"
                "✅ **You are now registered!**\n"
                "Whenever any monitored Facebook page uploads a new video, reel, or photo, "
                "it will be automatically downloaded and delivered straight here in smooth HD!\n\n"
                "📋 **Currently Monitored Pages:**\n"
                f"{pages_list}\n\n"
                "🛠 **Commands:**\n"
                "• `/add` - Add a Facebook page, username, or video to auto-track\n"
                "• `/del` - Remove a page from auto-tracking\n"
                "• `/pages` - List all monitored pages\n"
                "• `/checknow` - Run an immediate check of all Facebook pages\n"
                "• `/status` - View monitor status and seen posts\n\n"
                "💡 *For on-demand downloads from Instagram, TikTok, YouTube, Reddit, Pinterest, use @vidsmkdownloaderbot !*"
            )
            self._send_json(200, {"status": "start_command_handled"})
            return

        # 5. Handle /checknow command
        if text.strip().startswith("/checknow"):
            status_id = send_telegram_message(chat_id, "🔎 Checking Facebook pages for new uploads now...")
            count = check_facebook_pages_sync(target_chat_ids=[chat_id])
            all_pages = get_target_pages()
            edit_telegram_message(
                chat_id,
                status_id,
                f"✅ Check pass completed!\n• Pages inspected: {len(all_pages)}\n• New uploads delivered: {count}"
            )
            self._send_json(200, {"status": "checknow_handled", "found": count})
            return

        # 6. Handle /status command
        if text.strip().startswith("/status"):
            seen = get_seen_posts()
            subs = get_subscribers()
            all_pages = get_target_pages()
            send_telegram_message(
                chat_id,
                "📊 **Bot & Monitor Status**\n\n"
                f"• **Platform:** 🟢 24/7 Serverless on Vercel\n"
                f"• **Bot:** `@fbdownloaderpagesvidsbot`\n"
                f"• **Monitored Pages:** {len(all_pages)}\n"
                f"• **Subscribed Chats:** {len(subs)}\n"
                f"• **Seen Posts in DB:** {len(seen)}\n"
                f"• **Ping URL (Keep-Alive):** `https://fbdownloaderpagesbot.vercel.app/ping`"
            )
            self._send_json(200, {"status": "status_handled"})
            return

        # 7. Handle /pages command
        if text.strip().startswith("/pages"):
            all_pages = get_target_pages()
            pages_list = "\n".join([f"{idx+1}. `{p}`" for idx, p in enumerate(all_pages)])
            send_telegram_message(
                chat_id,
                f"📋 **Monitored Facebook Pages ({len(all_pages)}):**\n\n"
                f"{pages_list}\n\n"
                f"➕ To add a new page: send `/add`\n"
                f"🗑 To remove a page: send `/del <number>`"
            )
            self._send_json(200, {"status": "pages_handled"})
            return

        # Fallback message for arbitrary text or non-command links
        match = URL_REGEX.search(text)
        if match:
            url_match = match.group(0)
            url_l = url_match.lower()
            if "facebook.com" in url_l or "fb.watch" in url_l:
                send_telegram_message(
                    chat_id,
                    "📘 **Facebook Link Detected!**\n\n"
                    "• To add this page or video creator to the **Auto-Grab Monitor**, send:\n"
                    f"`/add {url_match}`\n\n"
                    "• The bot will automatically track the page and deliver every new video upload here in smooth HD!"
                )
            else:
                send_telegram_message(
                    chat_id,
                    "ℹ️ **Dedicated Facebook Auto-Tracker Bot**\n\n"
                    "This bot (@fbdownloaderpagesvidsbot) is dedicated exclusively to auto-tracking Facebook pages.\n\n"
                    "💡 For on-demand downloads from **TikTok, Instagram, Pinterest, YouTube, X, Reddit**, please use your main downloader bot: **@vidsmkdownloaderbot**!"
                )
            self._send_json(200, {"status": "info_replied"})
            return

        send_telegram_message(
            chat_id,
            "ℹ️ **Facebook Auto-Tracker Commands:**\n\n"
            "• `/add` - Add a Facebook page, username, or video to auto-track\n"
            "• `/pages` - View all tracked pages\n"
            "• `/checknow` - Check for new video uploads right now\n"
            "• `/del <number>` - Remove a tracked page\n"
            "• `/status` - View bot & monitor status"
        )
        self._send_json(200, {"status": "help_replied"})
        return
