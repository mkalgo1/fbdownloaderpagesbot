import asyncio
import html as html_lib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Dict, List, Optional, Set, Tuple

import yt_dlp
from aiogram import Bot
from aiogram.types import FSInputFile

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as cffi_requests
    HAS_CURL_CFFI = False

logger = logging.getLogger("FacebookMonitor")

DEFAULT_PAGES = [
    "https://web.facebook.com/people/Paris-France-Tour/61556328743295/",
    "https://web.facebook.com/people/FX-France-Express/100083193627339/",
    "https://web.facebook.com/profile.php?id=1269304149599939",
]

MAX_TELEGRAM_BYTES = 48 * 1024 * 1024       # 48 MB safe cap
TARGET_COMPRESSED_BYTES = 44 * 1024 * 1024  # 44 MB compression target

IMPERSONATE_PROFILES = ["chrome120", "chrome119", "safari17_0", "chrome110"]


def normalize_page_url(u: str) -> str:
    clean = u.strip().rstrip("/")
    clean = clean.replace("http://", "https://").replace("https://www.", "https://web.")
    return clean


def resolve_facebook_slug_to_id(slug_or_url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Given any Facebook page slug, username, handle, or URL:
    Resolves the canonical numeric page ID and clean page title.
    Returns (numeric_id, canonical_url, page_title)
    """
    text = slug_or_url.strip()
    if not text:
        return None, None, None

    # 1. Direct match for profile.php?id=\d+
    id_m = re.search(r"id=(\d+)", text)
    if id_m:
        uid = id_m.group(1)
        return uid, f"https://web.facebook.com/profile.php?id={uid}", "Facebook Page"

    # 2. Match /people/.../(\d+)
    people_m = re.search(r"/people/([^/]+)/(\d+)", text)
    if people_m:
        name = people_m.group(1).replace("-", " ")
        uid = people_m.group(2)
        return uid, f"https://web.facebook.com/profile.php?id={uid}", name

    # 3. Extract slug from URL or username
    clean = text.split("?")[0].rstrip("/")
    if "facebook.com" in clean.lower():
        parts = [p for p in clean.split("/") if p and "facebook.com" not in p and p not in ["pages", "p", "pg", "people"]]
        slug = parts[-1] if parts else clean
    else:
        slug = clean.lstrip("@").strip()

    if not slug or any(x in slug.lower() for x in ["login", "recover", "help", "checkpoint", "settings"]):
        return None, None, None

    # Known vanity slug overrides
    if slug.lower() == "lavialyon":
        return "1269304149599939", "https://web.facebook.com/profile.php?id=1269304149599939", "La vie à Lyon France"

    # Dynamic fetch from mobile Facebook to extract numeric ID and title
    m_url = f"https://m.facebook.com/{slug}"
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        }
        if HAS_CURL_CFFI:
            r = cffi_requests.get(m_url, headers=headers, impersonate="chrome120", allow_redirects=True, timeout=12)
        else:
            r = requests.get(m_url, headers=headers, allow_redirects=True, timeout=12)

        resp_text = r.text
        uid_match = (
            re.search(r'"delegate_page":\{"id":"(\d+)"\}', resp_text) or
            re.search(r'al:android:url[^>]*content="fb://profile/(\d+)"', resp_text) or
            re.search(r'fb://profile/(\d+)', resp_text) or
            re.search(r'fb://page/(?:id=|\?id=)?(\d+)', resp_text) or
            re.search(r'entity_id=(\d+)', resp_text) or
            re.search(r'"userID":"(\d+)"', resp_text) or
            re.search(r'"pageID":"(\d+)"', resp_text) or
            re.search(r'/profile\.php\?id=(\d+)', resp_text)
        )

        title_m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', resp_text) or re.search(r'<title>(.*?)</title>', resp_text)
        raw_title = title_m.group(1).replace(" | Facebook", "").strip() if title_m else slug
        import html as html_lib_local
        clean_title = html_lib_local.unescape(raw_title)

        if uid_match:
            uid = uid_match.group(1)
            return uid, f"https://web.facebook.com/profile.php?id={uid}", clean_title
    except Exception as e:
        logger.warning(f"Error dynamically resolving slug '{slug}': {e}")

    return None, None, None


def resolve_facebook_page(input_str: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Intelligently extracts Facebook page credentials from:
    1. A video or reel link (detects creator page uploader_id and resolves canonical page URL)
    2. A full Facebook Page URL (e.g. facebook.com/lefigaro or profile.php?id=...)
    3. A Facebook username or handle (e.g. lavialyon or @lavialyon)
    Returns (canonical_page_url, page_name, error_message)
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
                    canonical_url = f"https://web.facebook.com/profile.php?id={uid}"
                    return canonical_url, uploader_name or "Facebook Creator", None
        except Exception as e:
            logger.warning(f"Could not extract uploader from video link: {e}")

    # 2. Universal dynamic numeric resolution for ANY page URL, handle, or slug
    uid, canonical_url, page_title = resolve_facebook_slug_to_id(text)
    if canonical_url and uid:
        return canonical_url, page_title or "Facebook Page", None

    # 3. Fallback for vanity URLs if network resolution failed
    if "facebook.com" in text.lower():
        clean_url = text.split("?")[0].rstrip("/")
        if not clean_url.startswith("http"):
            clean_url = "https://" + clean_url
        name_guess = clean_url.rstrip("/").split("/")[-1]
        return clean_url, name_guess, None

    clean_handle = text.lstrip("@").strip()
    if re.match(r"^[a-zA-Z0-9._-]+$", clean_handle):
        return f"https://web.facebook.com/{clean_handle}", clean_handle, None

    return None, None, f"Unrecognized Facebook page format: '{text}'"


class FacebookMonitor:
    def __init__(
        self,
        bot: Bot,
        data_dir: str = ".",
        target_chat_ids: Optional[List[int]] = None,
        check_interval: int = 180,
        pages: Optional[List[str]] = None,
    ):
        self.bot = bot
        self.data_dir = data_dir
        self.check_interval = max(60, check_interval)
        self.target_chat_ids = set(target_chat_ids or [])
        self.start_time: float = time.time()
        
        self.seen_file = os.path.join(self.data_dir, "seen_posts.json")
        self.subscribers_file = os.path.join(self.data_dir, "subscribers.json")
        self.pages_file = os.path.join(self.data_dir, "monitored_pages.json")
        self.initialized_pages_file = os.path.join(self.data_dir, "initialized_pages.json")
        self.last_checked_file = os.path.join(self.data_dir, "last_checked.json")
        self.page_id_map_file = os.path.join(self.data_dir, "page_id_map.json")
        
        self.seen_posts: Dict[str, dict] = self._load_json(self.seen_file, default={})
        self.subscribers: Set[int] = set(self._load_json(self.subscribers_file, default=[]))
        self.initialized_pages: Set[str] = set(self._load_json(self.initialized_pages_file, default=[]))
        self.page_last_checked: Dict[str, float] = self._load_json(self.last_checked_file, default={})
        self.page_id_map: Dict[str, str] = self._load_json(self.page_id_map_file, default={
            "lavialyon": "1269304149599939",
            "61556328743295": "61556328743295",
            "100083193627339": "100083193627339",
            "1269304149599939": "1269304149599939",
        })

        if self.target_chat_ids:
            self.subscribers.update(self.target_chat_ids)
            
        raw_saved_pages = self._load_json(self.pages_file, default=None)
        candidate_pages = list(pages or DEFAULT_PAGES)
        if raw_saved_pages and isinstance(raw_saved_pages, list):
            for sp in raw_saved_pages:
                if sp and sp not in candidate_pages:
                    candidate_pages.append(sp)

        # Sanitize pages: remove any corrupted /login, /checkpoint URLs and deduplicate
        seen_canon = set()
        cleaned_pages = []
        for p in candidate_pages:
            if not isinstance(p, str) or not p.strip():
                continue
            if any(x in p.lower() for x in ["/login", "/checkpoint", "/recover", "/help", "facebook.com/login"]):
                continue
            norm = normalize_page_url(p)
            if norm not in seen_canon:
                seen_canon.add(norm)
                cleaned_pages.append(p.strip())

        # Guarantee DEFAULT_PAGES are always present
        for dp in DEFAULT_PAGES:
            norm_dp = normalize_page_url(dp)
            if norm_dp not in seen_canon:
                seen_canon.add(norm_dp)
                cleaned_pages.append(dp)

        self.pages = cleaned_pages
        self._save_json(self.pages_file, self.pages)

        self.last_check_time: Optional[float] = None
        self.is_checking: bool = False
        self.consecutive_errors: int = 0
        self.running: bool = False

    def _load_json(self, path: str, default):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading {path}: {e}")
        return default

    def _save_json(self, path: str, data):
        temp_path = f"{path}.tmp.{uuid.uuid4().hex[:6]}"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, path)
        except Exception as e:
            logger.error(f"Error saving {path}: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    def add_subscriber(self, chat_id: int):
        if chat_id not in self.subscribers:
            self.subscribers.add(chat_id)
            self._save_json(self.subscribers_file, list(self.subscribers))
            logger.info(f"Added subscriber chat_id: {chat_id}")

    def remove_subscriber(self, chat_id: int):
        if chat_id in self.subscribers:
            self.subscribers.remove(chat_id)
            self._save_json(self.subscribers_file, list(self.subscribers))
            logger.info(f"Removed subscriber chat_id: {chat_id}")

    def add_page(self, page_url: str) -> bool:
        clean_url = page_url.strip()
        
        # Check if numeric ID is present or dynamically resolve it
        id_m = re.search(r"id=(\d+)", clean_url) or re.search(r"/people/[^/]+/(\d+)", clean_url)
        if id_m:
            uid = id_m.group(1)
            clean_url = f"https://web.facebook.com/profile.php?id={uid}"
            self.page_id_map[uid] = uid
            self._save_json(self.page_id_map_file, self.page_id_map)
        else:
            uid, canonical_url, _ = resolve_facebook_slug_to_id(clean_url)
            if uid and canonical_url:
                slug = clean_url.split("?")[0].rstrip("/").split("/")[-1].lstrip("@").lower()
                clean_url = canonical_url
                self.page_id_map[slug] = uid
                self.page_id_map[uid] = uid
                self._save_json(self.page_id_map_file, self.page_id_map)

        norm_new = normalize_page_url(clean_url)
        for p in self.pages:
            if normalize_page_url(p) == norm_new:
                return False
        self.pages.append(clean_url)
        self._save_json(self.pages_file, self.pages)
        logger.info(f"Added new monitored page: {clean_url}")
        return True

    def remove_page(self, page_url: str) -> bool:
        clean_url = page_url.strip()
        matched = [p for p in self.pages if clean_url.lower() in p.lower()]
        if matched:
            for p in matched:
                self.pages.remove(p)
                p_key = normalize_page_url(p).lower()
                if p_key in self.initialized_pages:
                    self.initialized_pages.remove(p_key)
            self._save_json(self.pages_file, self.pages)
            self._save_json(self.initialized_pages_file, list(self.initialized_pages))
            logger.info(f"Removed monitored page: {clean_url}")
            return True
        return False

    def mark_seen(self, post_key: str, metadata: dict):
        self.seen_posts[post_key] = {
            "seen_at": int(time.time()),
            "title": metadata.get("title", ""),
            "type": metadata.get("type", "unknown"),
            "url": metadata.get("url", post_key)
        }
        self._save_json(self.seen_file, self.seen_posts)

    def is_seen(self, post_key: str) -> bool:
        return post_key in self.seen_posts

    def get_target_endpoints(self, page_url: str) -> List[str]:
        """
        Returns both the main page feed and the reels tab for any page URL.
        Always resolves to canonical profile.php?id={uid} to bypass datacenter login barriers.
        """
        clean = page_url.strip().rstrip("/")
        
        # 1. Direct regex for numeric ID in URL
        id_m = re.search(r"id=(\d+)", clean) or re.search(r"/people/[^/]+/(\d+)", clean)
        if id_m:
            uid = id_m.group(1)
            return [
                f"https://web.facebook.com/profile.php?id={uid}",
                f"https://web.facebook.com/profile.php?id={uid}&sk=reels_tab",
            ]
            
        # 2. Check cached page_id_map
        slug = clean.split("?")[0].rstrip("/").split("/")[-1].lstrip("@").lower()
        if slug in self.page_id_map:
            uid = self.page_id_map[slug]
            return [
                f"https://web.facebook.com/profile.php?id={uid}",
                f"https://web.facebook.com/profile.php?id={uid}&sk=reels_tab",
            ]
            
        # 3. Dynamic resolution on the fly if not yet mapped
        uid, _, _ = resolve_facebook_slug_to_id(slug)
        if uid:
            self.page_id_map[slug] = uid
            self._save_json(self.page_id_map_file, self.page_id_map)
            return [
                f"https://web.facebook.com/profile.php?id={uid}",
                f"https://web.facebook.com/profile.php?id={uid}&sk=reels_tab",
            ]

        # 4. Fallback if resolution failed
        return [
            f"{clean}/",
            f"{clean}/reels_tab/",
        ]

    # -------------------------------------------------------------------
    # ANTI-BLOCK REQUEST DISPATCHER (TLS Impersonation + Safe Redirects)
    # -------------------------------------------------------------------
    def _fetch_page_html(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Fetches the Facebook page HTML using curl_cffi with browser impersonation.
        Intelligently follows safe client-side meta-refresh and JS redirects while
        strictly rejecting auth/login redirects to protect the page catalog.
        Returns (html_text, final_resolved_url).
        """
        impersonate_choice = random.choice(IMPERSONATE_PROFILES)
        cookies_file = os.getenv("FB_COOKIES_FILE", os.path.join(self.data_dir, "cookies.txt"))
        
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }

        cookies = {}
        if os.path.exists(cookies_file):
            try:
                with open(cookies_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("#") or not line.strip():
                            continue
                        parts = line.strip().split("\t")
                        if len(parts) >= 7:
                            cookies[parts[5]] = parts[6]
            except Exception as ce:
                logger.warning(f"Could not parse cookies file: {ce}")

        try:
            if HAS_CURL_CFFI:
                session = cffi_requests.Session(impersonate=impersonate_choice)
                resp = session.get(url, headers=headers, cookies=cookies, timeout=25, allow_redirects=True)
            else:
                resp = cffi_requests.get(url, headers=headers, cookies=cookies, timeout=25, allow_redirects=True)

            if resp.status_code == 200:
                self.consecutive_errors = 0
                final_url = str(resp.url)

                if any(x in final_url.lower() for x in ["/login", "/checkpoint", "/recover", "/help", "facebook.com/login"]):
                    # Fallback to mobile Facebook endpoint if desktop was redirected to login
                    if "m.facebook.com" not in url:
                        m_url = url.replace("web.facebook.com", "m.facebook.com").replace("www.facebook.com", "m.facebook.com")
                        logger.info(f"Retrying with mobile Facebook endpoint: {m_url}")
                        try:
                            m_resp = session.get(m_url, headers=headers, cookies=cookies, timeout=25, allow_redirects=True)
                            m_final = str(m_resp.url)
                            if m_resp.status_code == 200 and not any(x in m_final.lower() for x in ["/login", "/checkpoint", "/recover"]):
                                return m_resp.text, m_final
                        except Exception as me:
                            logger.warning(f"Mobile fallback error for {m_url}: {me}")

                    logger.warning(f"Facebook HTTP redirect landed on auth/login page: {final_url}. Discarding.")
                    return None, None

                # If the fetched body already contains rich page data (Relay/GraphQL scripts or reels), use it directly
                if len(resp.text) > 200000 and ("application/json" in resp.text or "/reel/" in resp.text or "creation_time" in resp.text):
                    return resp.text, final_url

                # Check if Facebook returned a client-side meta refresh or JS location replace
                redirect_target = None
                meta_match = re.search(r'<meta\s+http-equiv=["\']refresh["\']\s+content=["\'][0-9;\s]+url=([^"\'>]+)["\']', resp.text, re.IGNORECASE)
                if meta_match:
                    redirect_target = meta_match.group(1).replace("&amp;", "&").strip()
                else:
                    js_match = re.search(r'window\.location\.replace\(["\']([^"\']+)["\']\)', resp.text)
                    if js_match:
                        redirect_target = js_match.group(1).replace(r"\/", "/").strip()

                if redirect_target and redirect_target != url:
                    if redirect_target.startswith("/"):
                        redirect_target = "https://web.facebook.com" + redirect_target
                    elif redirect_target.startswith("https://www.facebook.com"):
                        redirect_target = redirect_target.replace("https://www.facebook.com", "https://web.facebook.com")

                    # Reject auth/login redirects - but keep the valid HTML payload returned by Facebook
                    if any(x in redirect_target.lower() for x in ["/login", "/checkpoint", "/recover", "/help", "facebook.com/login"]):
                        logger.info(f"Ignoring client login redirect target for {url}. Using fetched page body ({len(resp.text)} bytes).")
                        return resp.text, final_url

                    logger.info(f"Following Facebook client redirect: {url} -> {redirect_target}")
                    try:
                        if HAS_CURL_CFFI:
                            resp2 = session.get(redirect_target, headers=headers, cookies=cookies, timeout=25, allow_redirects=True)
                        else:
                            resp2 = cffi_requests.get(redirect_target, headers=headers, cookies=cookies, timeout=25, allow_redirects=True)
                        if resp2.status_code == 200:
                            final_redirect_url = str(resp2.url) if resp2.url else redirect_target
                            if not any(x in final_redirect_url.lower() for x in ["/login", "/checkpoint", "/recover", "/help"]):
                                return resp2.text, final_redirect_url
                    except Exception as re_err:
                        logger.warning(f"Error following redirect target {redirect_target}: {re_err}")

                return resp.text, final_url

            elif resp.status_code in (429, 400):
                logger.warning(f"Facebook returned rate limit / error code {resp.status_code} for {url}")
                self.consecutive_errors += 1
                return None, None
            else:
                logger.warning(f"Unexpected HTTP status {resp.status_code} for {url}")
                return None, None
        except Exception as e:
            logger.error(f"Error fetching Facebook page {url}: {e}")
            self.consecutive_errors += 1
            return None, None

    # -------------------------------------------------------------------
    # EXTRACTION OF POSTS / REELS / PHOTOS FROM PAGE PAYLOAD
    # -------------------------------------------------------------------
    def _extract_posts_from_html(self, html: str, page_url: str) -> List[dict]:
        """
        Scans Relay / GraphQL <script type="application/json"> blocks and raw
        HTML inside Facebook's payload to locate posts, reels, videos, and photos.
        Merges fragmented node properties and extracts canonical keys.
        """
        discovered: Dict[str, dict] = {}

        # Derive page name if available
        page_name = "Facebook Page"
        title_match = re.search(r'<title>([^<]+)</title>', html)
        if title_match:
            raw_title = title_match.group(1).replace(" | Facebook", "").replace(" - Facebook", "").strip()
            if raw_title and "log in" not in raw_title.lower() and "error" not in raw_title.lower():
                page_name = raw_title

        def find_image_uri(d):
            if not isinstance(d, dict):
                return None
            for k in ["photo_image", "large_share_image", "image"]:
                sub = d.get(k)
                if isinstance(sub, dict) and sub.get("uri"):
                    return sub["uri"]
            for v in d.values():
                if isinstance(v, dict):
                    res = find_image_uri(v)
                    if res:
                        return res
                elif isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict):
                            res = find_image_uri(it)
                            if res:
                                return res
            return None

        def parse_recursive(obj, current_creation_time=None):
            if isinstance(obj, dict):
                permalink = obj.get("permalink_url") or obj.get("url")
                post_id = obj.get("post_id") or obj.get("id")
                creation_time = obj.get("creation_time") or current_creation_time
                
                # Check for message / caption across GraphQL fields
                text = ""
                for msg_key in ["message", "video_caption", "story_message", "caption", "text"]:
                    if msg_key in obj:
                        val = obj[msg_key]
                        if isinstance(val, dict):
                            text = val.get("text", "") or val.get("caption", "")
                        elif isinstance(val, str):
                            text = val
                        if text:
                            break

                if permalink and isinstance(permalink, str):
                    clean_link = permalink.split("?")[0].rstrip("/")
                    is_reel = "/reel/" in clean_link
                    is_video = "/videos/" in clean_link or "watch" in clean_link or "?v=" in permalink
                    is_photo = ("/photo.php" in permalink and ("fbid=" in permalink or post_id)) or "/posts/" in clean_link or "/photos/" in clean_link

                    if is_reel or is_video or is_photo:
                        reel_m = re.search(r"/reel/(\d+)", clean_link)
                        vid_m = re.search(r"/videos/(\d+)", clean_link) or re.search(r"[?&]v=(\d+)", permalink)
                        post_m = re.search(r"/posts/([a-zA-Z0-9_]+)", clean_link)
                        fbid_m = re.search(r"[?&]fbid=(\d+)", permalink)

                        if reel_m:
                            canon_key = f"reel_{reel_m.group(1)}"
                            clean_link = f"https://web.facebook.com/reel/{reel_m.group(1)}"
                            post_type = "video"
                        elif vid_m:
                            canon_key = f"video_{vid_m.group(1)}"
                            clean_link = f"https://web.facebook.com/watch/?v={vid_m.group(1)}"
                            post_type = "video"
                        elif post_m:
                            canon_key = f"post_{post_m.group(1)}"
                            post_type = "photo"
                        elif fbid_m:
                            canon_key = f"photo_{fbid_m.group(1)}"
                            clean_link = f"https://web.facebook.com/photo.php?fbid={fbid_m.group(1)}"
                            post_type = "photo"
                        elif post_id and str(post_id).isdigit():
                            canon_key = f"id_{post_id}"
                            post_type = "video" if (is_reel or is_video) else "photo"
                        else:
                            canon_key = None

                        if canon_key and not clean_link.endswith("/photo.php"):
                            photo_url = find_image_uri(obj)

                            if canon_key not in discovered:
                                discovered[canon_key] = {
                                    "id": canon_key,
                                    "url": permalink,
                                    "clean_url": clean_link,
                                    "type": post_type,
                                    "title": text,
                                    "creation_time": creation_time,
                                    "page_name": page_name,
                                    "page_url": page_url,
                                    "photo_url": photo_url,
                                }
                            else:
                                existing = discovered[canon_key]
                                if not existing.get("title") and text:
                                    existing["title"] = text
                                if not existing.get("creation_time") and creation_time:
                                    existing["creation_time"] = creation_time
                                if not existing.get("photo_url") and photo_url:
                                    existing["photo_url"] = photo_url
                                if existing.get("page_name") in ("Facebook Page", "") and page_name not in ("Facebook Page", ""):
                                    existing["page_name"] = page_name

                for v in obj.values():
                    parse_recursive(v, creation_time)
            elif isinstance(obj, list):
                for item in obj:
                    parse_recursive(item, current_creation_time)

        scripts = re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html)
        for scr in scripts:
            try:
                data = json.loads(scr)
                parse_recursive(data)
            except Exception:
                continue

        # Regex scan raw HTML for reels and posts
        for reel_match in re.finditer(r'href=["\'](?:https?://(?:www\.|web\.)?facebook\.com)?/reel/(\d+)[^"\']*["\']', html):
            rid = reel_match.group(1)
            c_key = f"reel_{rid}"
            if c_key not in discovered:
                discovered[c_key] = {
                    "id": c_key,
                    "url": f"https://web.facebook.com/reel/{rid}/",
                    "clean_url": f"https://web.facebook.com/reel/{rid}",
                    "type": "video",
                    "title": "",
                    "creation_time": None,
                    "page_name": page_name,
                    "page_url": page_url,
                    "photo_url": None,
                }

        for post_match in re.finditer(r'href=["\'](https?://(?:www\.|web\.)?facebook\.com/[^/]+/posts/(pfbid[a-zA-Z0-9]+))[^"\']*["\']', html):
            p_full = post_match.group(1)
            p_id = post_match.group(2)
            c_key = f"post_{p_id}"
            if c_key not in discovered:
                discovered[c_key] = {
                    "id": c_key,
                    "url": p_full,
                    "clean_url": p_full,
                    "type": "photo",
                    "title": "",
                    "creation_time": None,
                    "page_name": page_name,
                    "page_url": page_url,
                    "photo_url": None,
                }

        result = list(discovered.values())
        result.sort(key=lambda x: int(x.get("creation_time") or 0), reverse=True)
        return result

    # -------------------------------------------------------------------
    # MEDIA PROCESSING & UPLOAD PIPELINE
    # -------------------------------------------------------------------
    def _download_and_process_video(self, url: str, temp_dir: str) -> Tuple[Optional[str], Optional[str], dict]:
        """
        Uses yt-dlp to download the video, generates +faststart and 1st frame thumbnail.
        """
        out_tmpl = os.path.join(temp_dir, "raw_stream.%(ext)s")
        cookies_file = os.getenv("FB_COOKIES_FILE", os.path.join(self.data_dir, "cookies.txt"))
        
        ydl_opts = {
            "outtmpl": out_tmpl,
            "format": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=h264]+bestaudio/best[vcodec^=avc1]/best[vcodec^=h264]/hd/sd/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 5,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        }
        if os.path.exists(cookies_file):
            ydl_opts["cookiefile"] = cookies_file

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

        files = [
            os.path.join(temp_dir, f) for f in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, f)) and not f.endswith((".temp", ".part"))
        ]
        if not files:
            return None, None, info

        raw_video = [f for f in files if f.lower().endswith(".mp4")]
        video_path = raw_video[0] if raw_video else files[0]

        # Faststart remux
        faststart_path = os.path.join(temp_dir, f"fast_{uuid.uuid4().hex[:6]}.mp4")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", video_path, "-c", "copy", "-movflags", "+faststart", faststart_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )
            if os.path.exists(faststart_path):
                video_path = faststart_path
        except Exception:
            pass

        # Thumbnail extraction at 0.2s
        thumb_path = os.path.join(temp_dir, "thumb.jpg")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "00:00:00.200", "-i", video_path, "-vframes", "1", "-q:v", "2", thumb_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )
        except Exception:
            thumb_path = None

        return video_path, thumb_path, info

    def get_exact_video_dimensions(self, file_path: str) -> Tuple[int, int, int]:
        """
        Extracts exact display width, height, and duration with rotation normalization.
        Guarantees Telegram receives native aspect ratio (e.g. 1080x1920 for 9:16 reels).
        """
        w, h, dur = 0, 0, 0
        try:
            cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", file_path]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
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
                        d = float(s.get("duration") or data.get("format", {}).get("duration") or 0)
                        dur = int(d)
                        break
        except Exception as e:
            logger.warning(f"ffprobe dimension check warning: {e}")
        return w, h, dur

    async def _download_and_broadcast(self, post: dict) -> bool:
        """
        Downloads the media and sends it to all subscribers.
        Returns True if delivered successfully to at least one subscriber, False otherwise.
        """
        if not self.subscribers:
            logger.warning("No subscriber chat IDs registered to receive media. Run /start in the bot chat.")
            return False

        post_url = post["url"]
        post_type = post["type"]
        page_name = post["page_name"]
        raw_caption = post.get("title") or ""

        delivered = False
        temp_dir = tempfile.mkdtemp(prefix="fb_download_")

        try:
            if post_type == "video":
                video_path, thumb_path, info = await asyncio.to_thread(
                    self._download_and_process_video, post_url, temp_dir
                )
                if not video_path or not os.path.exists(video_path):
                    logger.error(f"Failed to download video from {post_url}")
                    return False

                # Extract rich title from yt-dlp if raw_caption was missing
                if not raw_caption or len(raw_caption) < 5:
                    yt_title = info.get("title") or info.get("description") or ""
                    if "|" in yt_title:
                        parts = [p.strip() for p in yt_title.split("|")]
                        valid_parts = [p for p in parts if not any(k in p.lower() for k in ["views", "reactions", "shares", "followers"])]
                        raw_caption = valid_parts[0] if valid_parts else parts[0]
                    elif yt_title:
                        raw_caption = yt_title

                safe_page = html_lib.escape(page_name)
                safe_caption = html_lib.escape(raw_caption[:280]) if raw_caption else ""
                caption_lines = [
                    f"🔔 <b>New {post_type.capitalize()} Uploaded!</b>",
                    f"📘 <b>Page:</b> {safe_page}",
                ]
                if safe_caption:
                    caption_lines.append(f"📌 {safe_caption}")
                caption_lines.append(f'\n🔗 <a href="{post_url}">Original Link</a>')
                caption_text = "\n".join(caption_lines)

                size = os.path.getsize(video_path)
                if size > (50 * 1024 * 1024):
                    logger.warning(f"Video {post_url} size ({size // 1048576} MB) exceeds 50MB limit.")
                    for chat_id in list(self.subscribers):
                        try:
                            await self.bot.send_message(
                                chat_id,
                                f"⚠️ New video on <b>{safe_page}</b> exceeds Telegram's 50 MB limit.\n\n🔗 Watch here: {post_url}",
                                parse_mode="HTML"
                            )
                            delivered = True
                        except Exception as ce:
                            logger.error(f"Send warning error to {chat_id}: {ce}")
                    return delivered

                thumb_input = FSInputFile(thumb_path) if thumb_path and os.path.exists(thumb_path) else None
                duration = int(info.get("duration", 0)) if info.get("duration") else None
                width = int(info.get("width")) if info.get("width") else None
                height = int(info.get("height")) if info.get("height") else None

                if not width or not height or not duration:
                    exact_w, exact_h, exact_dur = self.get_exact_video_dimensions(video_path)
                    width = width or exact_w or 1080
                    height = height or exact_h or 1920
                    duration = duration or exact_dur

                for chat_id in list(self.subscribers):
                    try:
                        video_input = FSInputFile(video_path, filename=f"fb_video_{uuid.uuid4().hex[:6]}.mp4")
                        await self.bot.send_video(
                            chat_id=chat_id,
                            video=video_input,
                            caption=caption_text,
                            parse_mode="HTML",
                            thumbnail=thumb_input,
                            duration=duration,
                            width=width,
                            height=height,
                            supports_streaming=True
                        )
                        logger.info(f"Delivered video to chat {chat_id} from {page_name}")
                        delivered = True
                    except Exception as e:
                        logger.error(f"Error sending video to {chat_id}: {e}")

            elif post_type == "photo":
                photo_cdn_url = post.get("photo_url")
                if not photo_cdn_url:
                    try:
                        p_html, _ = await asyncio.to_thread(self._fetch_page_html, post_url)
                        if p_html:
                            og_m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', p_html)
                            if og_m:
                                photo_cdn_url = og_m.group(1).replace("&amp;", "&")
                            if not raw_caption:
                                desc_m = re.search(r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']', p_html)
                                if desc_m:
                                    raw_caption = desc_m.group(1).replace("&amp;", "&")
                    except Exception as pe:
                        logger.warning(f"Error extracting photo og:image for {post_url}: {pe}")

                safe_page = html_lib.escape(page_name)
                safe_caption = html_lib.escape(raw_caption[:280]) if raw_caption else ""
                caption_lines = [
                    f"🔔 <b>New Photo Uploaded!</b>",
                    f"📘 <b>Page:</b> {safe_page}",
                ]
                if safe_caption:
                    caption_lines.append(f"📌 {safe_caption}")
                caption_lines.append(f'\n🔗 <a href="{post_url}">Original Link</a>')
                caption_text = "\n".join(caption_lines)

                img_path = os.path.join(temp_dir, f"photo_{uuid.uuid4().hex[:6]}.jpg")
                photo_ok = False

                if photo_cdn_url:
                    try:
                        import requests
                        r = await asyncio.to_thread(
                            requests.get,
                            photo_cdn_url,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"},
                            timeout=20
                        )
                        if r.status_code == 200 and len(r.content) > 1000:
                            with open(img_path, "wb") as pf:
                                pf.write(r.content)
                            photo_ok = True
                    except Exception as de:
                        logger.warning(f"Direct photo download error: {de}")

                if not photo_ok:
                    try:
                        ydl_opts = {"outtmpl": os.path.join(temp_dir, "photo.%(ext)s"), "quiet": True}
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            await asyncio.to_thread(ydl.download, [post_url])
                        downloaded = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, f))]
                        if downloaded:
                            img_path = downloaded[0]
                            photo_ok = True
                    except Exception:
                        pass

                if photo_ok and os.path.exists(img_path):
                    for chat_id in list(self.subscribers):
                        try:
                            photo_input = FSInputFile(img_path)
                            await self.bot.send_photo(
                                chat_id=chat_id,
                                photo=photo_input,
                                caption=caption_text,
                                parse_mode="HTML"
                            )
                            logger.info(f"Delivered photo to chat {chat_id} from {page_name}")
                            delivered = True
                        except Exception as e:
                            logger.error(f"Error sending photo to {chat_id}: {e}")
                else:
                    logger.error(f"Failed to obtain photo file for {post_url}")

        except Exception as e:
            logger.error(f"Error in media download & broadcast: {e}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        return delivered

    # -------------------------------------------------------------------
    # SINGLE CHECK PASS OVER ALL PAGES
    # -------------------------------------------------------------------
    async def check_pages_once(self, announce_empty: bool = False) -> int:
        """
        Runs one polite check cycle across all configured Facebook pages.
        Accurately monitors both main feed and reels tab for each page.
        Returns total number of new posts discovered & delivered.
        """
        if self.is_checking:
            logger.info("A check pass is already in progress, skipping duplicate call.")
            return 0

        self.is_checking = True
        new_items_count = 0
        self.last_check_time = time.time()

        try:
            logger.info(f"Starting check pass over {len(self.pages)} pages...")
            
            for page_url in list(self.pages):
                logger.info(f"Checking Facebook page: {page_url}")
                endpoints = self.get_target_endpoints(page_url)
                discovered_for_page: Dict[str, dict] = {}
                resolved_final_url = None

                for ep in endpoints:
                    html, final_url = await asyncio.to_thread(self._fetch_page_html, ep)
                    if final_url and not resolved_final_url:
                        resolved_final_url = final_url
                    if not html:
                        continue

                    items = self._extract_posts_from_html(html, page_url)
                    for it in items:
                        if it["clean_url"].endswith("/photo.php") and not it.get("photo_url"):
                            continue
                        pid = it["id"]
                        if pid not in discovered_for_page:
                            discovered_for_page[pid] = it
                        else:
                            existing = discovered_for_page[pid]
                            if not existing.get("title") and it.get("title"):
                                existing["title"] = it["title"]
                            if not existing.get("photo_url") and it.get("photo_url"):
                                existing["photo_url"] = it["photo_url"]
                            if not existing.get("creation_time") and it.get("creation_time"):
                                existing["creation_time"] = it["creation_time"]

                    await asyncio.sleep(random.uniform(2.0, 4.0))

                posts = list(discovered_for_page.values())
                posts.sort(key=lambda x: int(x.get("creation_time") or 0), reverse=True)
                logger.info(f"Found {len(posts)} posts/reels across feed & reels tab for {page_url}")

                page_key = normalize_page_url(page_url).lower()
                final_key = normalize_page_url(resolved_final_url).lower() if resolved_final_url else None
                is_uninitialized = (page_key not in self.initialized_pages and (not final_key or final_key not in self.initialized_pages))
                prev_check_time = self.page_last_checked.get(page_key, self.start_time)

                if is_uninitialized:
                    logger.info(f"Page {page_url} has not been initialized yet. Recording baseline of {len(posts)} existing posts.")
                    for post in posts:
                        self.mark_seen(post["id"], post)
                        self.mark_seen(post["clean_url"], post)
                    self.initialized_pages.add(page_key)
                    if final_key:
                        self.initialized_pages.add(final_key)
                    self.page_last_checked[page_key] = time.time()
                    self._save_json(self.initialized_pages_file, list(self.initialized_pages))
                    self._save_json(self.last_checked_file, self.page_last_checked)
                    await asyncio.sleep(random.uniform(3, 6))
                    continue

                for post in posts:
                    post_key = post["id"]
                    clean_url = post["clean_url"]

                    if self.is_seen(post_key) or self.is_seen(clean_url):
                        continue

                    # Strict timestamp filtering: only deliver posts/videos uploaded since last check to prevent old items from being sent
                    creation_time = post.get("creation_time")
                    if creation_time:
                        try:
                            c_time = int(creation_time)
                            if c_time < (prev_check_time - 60):
                                logger.info(f"Skipping old post {clean_url} from {page_url} (creation_time {c_time} < {int(prev_check_time - 60)})")
                                self.mark_seen(post_key, post)
                                self.mark_seen(clean_url, post)
                                continue
                        except Exception:
                            pass

                    # If video without GraphQL creation_time, verify upload timestamp via yt-dlp metadata
                    if post.get("type") == "video":
                        try:
                            ydl_quick_opts = {
                                "quiet": True,
                                "no_warnings": True,
                                "socket_timeout": 15,
                                "http_headers": {
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
                                }
                            }
                            with yt_dlp.YoutubeDL(ydl_quick_opts) as ydl:
                                q_info = await asyncio.to_thread(ydl.extract_info, clean_url, download=False)
                                v_ts = q_info.get("timestamp")
                                if v_ts and int(v_ts) < (prev_check_time - 60):
                                    logger.info(f"Skipping old video {clean_url} (uploaded at {v_ts} < check baseline {int(prev_check_time - 60)})")
                                    self.mark_seen(post_key, post)
                                    self.mark_seen(clean_url, post)
                                    continue
                        except Exception as ve:
                            logger.warning(f"Could not verify video upload timestamp for {clean_url}: {ve}")

                    # If photo without GraphQL creation_time, verify publication timestamp directly from photo page
                    if post.get("type") == "photo" and not creation_time:
                        try:
                            p_html, _ = await asyncio.to_thread(self._fetch_page_html, clean_url)
                            if p_html:
                                ts_m = re.findall(r'"creation_time":\s*(\d+)', p_html)
                                if ts_m:
                                    c_time = int(ts_m[0])
                                    post["creation_time"] = c_time
                                    if c_time < (prev_check_time - 60):
                                        logger.info(f"Skipping old photo {clean_url} (photo creation_time {c_time} < check baseline {int(prev_check_time - 60)})")
                                        self.mark_seen(post_key, post)
                                        self.mark_seen(clean_url, post)
                                        continue
                                else:
                                    # If timestamp cannot be determined, mark seen & skip to prevent sending old media
                                    logger.warning(f"Skipping unverified photo {clean_url}: creation_time absent")
                                    self.mark_seen(post_key, post)
                                    self.mark_seen(clean_url, post)
                                    continue
                        except Exception as pe:
                            logger.warning(f"Could not verify photo timestamp for {clean_url}: {pe}")
                            self.mark_seen(post_key, post)
                            self.mark_seen(clean_url, post)
                            continue

                    # NEW POST DETECTED!
                    logger.info(f"🔥 NEW UPLOAD DETECTED: {post['clean_url']} ({post['type']}) on {post.get('page_name', page_url)}")
                    delivered = await self._download_and_broadcast(post)
                    if delivered:
                        self.mark_seen(post_key, post)
                        self.mark_seen(clean_url, post)
                        new_items_count += 1
                    else:
                        logger.warning(f"Failed to deliver {clean_url}, will retry in next cycle.")

                # Record last check timestamp for this page
                self.page_last_checked[page_key] = time.time()
                self._save_json(self.last_checked_file, self.page_last_checked)

                # Polite delay between consecutive pages
                await asyncio.sleep(random.uniform(4.0, 8.0))

        except Exception as e:
            logger.error(f"Error during check cycle: {e}")
        finally:
            self.is_checking = False

        logger.info(f"Check pass completed. Discovered & delivered {new_items_count} new posts.")
        return new_items_count

    # -------------------------------------------------------------------
    # BACKGROUND SCHEDULER LOOP (WITH ADAPTIVE BACKOFF & RANDOM JITTER)
    # -------------------------------------------------------------------
    async def start_loop(self):
        """
        Continuous background loop running every 3 minutes (180 seconds).
        """
        self.running = True
        logger.info("Facebook auto-checker loop started (3-minute interval).")
        
        # Initial check after 8s warmup
        await asyncio.sleep(8)
        await self.check_pages_once()

        while self.running:
            try:
                current_interval = max(60, self.check_interval)

                if self.consecutive_errors > 0:
                    backoff_multiplier = min(self.consecutive_errors, 5)
                    backoff_delay = backoff_multiplier * 180
                    logger.warning(
                        f"Backing off for {backoff_delay}s due to {self.consecutive_errors} consecutive errors."
                    )
                    await asyncio.sleep(backoff_delay)
                else:
                    logger.info(f"Sleeping for {int(current_interval)}s (3 minutes) until next Facebook check...")
                    await asyncio.sleep(current_interval)

                await self.check_pages_once()

            except asyncio.CancelledError:
                logger.info("Auto-checker loop cancelled.")
                break
            except Exception as e:
                logger.error(f"Unexpected error in monitor loop: {e}")
                await asyncio.sleep(60)

    def stop(self):
        self.running = False
