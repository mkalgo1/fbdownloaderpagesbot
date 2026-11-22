import asyncio
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
    "https://web.facebook.com/profile.php?id=61556328743295",
    "https://web.facebook.com/profile.php?id=100083193627339",
    "https://web.facebook.com/lavialyon",
]

MAX_TELEGRAM_BYTES = 48 * 1024 * 1024       # 48 MB safe cap
TARGET_COMPRESSED_BYTES = 44 * 1024 * 1024  # 44 MB compression target

IMPERSONATE_PROFILES = ["chrome120", "chrome119", "safari17_0", "chrome110"]


class FacebookMonitor:
    def __init__(
        self,
        bot: Bot,
        data_dir: str = ".",
        target_chat_ids: Optional[List[int]] = None,
        check_interval: int = 360,
        pages: Optional[List[str]] = None,
    ):
        self.bot = bot
        self.data_dir = data_dir
        self.check_interval = max(60, check_interval)
        self.pages = list(pages or DEFAULT_PAGES)
        self.target_chat_ids = set(target_chat_ids or [])
        
        self.seen_file = os.path.join(self.data_dir, "seen_posts.json")
        self.subscribers_file = os.path.join(self.data_dir, "subscribers.json")
        self.pages_file = os.path.join(self.data_dir, "monitored_pages.json")
        
        self.seen_posts: Dict[str, dict] = self._load_json(self.seen_file, default={})
        self.subscribers: Set[int] = set(self._load_json(self.subscribers_file, default=[]))
        if self.target_chat_ids:
            self.subscribers.update(self.target_chat_ids)
            
        saved_pages = self._load_json(self.pages_file, default=None)
        if saved_pages and isinstance(saved_pages, list):
            self.pages = saved_pages

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
                os.remove(temp_path)

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
        if clean_url not in self.pages:
            self.pages.append(clean_url)
            self._save_json(self.pages_file, self.pages)
            return True
        return False

    def remove_page(self, page_url: str) -> bool:
        clean_url = page_url.strip()
        matched = [p for p in self.pages if clean_url in p]
        if matched:
            for p in matched:
                self.pages.remove(p)
            self._save_json(self.pages_file, self.pages)
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

    # -------------------------------------------------------------------
    # ANTI-BLOCK REQUEST DISPATCHER (TLS Impersonation + Random Jitter)
    # -------------------------------------------------------------------
    def _fetch_page_html(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Fetches the Facebook page HTML using curl_cffi with browser impersonation.
        Returns (html_text, final_resolved_url).
        """
        impersonate_choice = random.choice(IMPERSONATE_PROFILES)
        cookies_file = os.getenv("FB_COOKIES_FILE", os.path.join(self.data_dir, "cookies.txt"))
        
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

        cookies = {}
        if os.path.exists(cookies_file):
            try:
                # Load netscape or simple cookies if available
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
                resp = session.get(url, headers=headers, cookies=cookies, timeout=20, allow_redirects=True)
            else:
                resp = cffi_requests.get(url, headers=headers, cookies=cookies, timeout=20, allow_redirects=True)

            if resp.status_code == 200:
                self.consecutive_errors = 0
                final_url = str(resp.url)
                # If we were redirected to a canonical URL, update it for future requests
                if final_url and final_url != url and "profile.php" in url and "people" in final_url:
                    clean_final = final_url.split("?")[0]
                    idx = self.pages.index(url) if url in self.pages else -1
                    if idx != -1:
                        self.pages[idx] = clean_final
                        self._save_json(self.pages_file, self.pages)
                        logger.info(f"Updated canonical page URL: {url} -> {clean_final}")
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
        Scans all Relay / GraphQL <script type="application/json"> blocks inside
        Facebook's HTML to locate posts, reels, videos, and photos.
        """
        scripts = re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html)
        discovered: Dict[str, dict] = {}

        # Derive page name if available
        page_name = "Facebook Page"
        title_match = re.search(r'<title>([^<]+)</title>', html)
        if title_match:
            raw_title = title_match.group(1).replace(" | Facebook", "").replace(" - Facebook", "").strip()
            if raw_title and "log in" not in raw_title.lower() and "error" not in raw_title.lower():
                page_name = raw_title

        def parse_recursive(obj):
            if isinstance(obj, dict):
                permalink = obj.get("permalink_url") or obj.get("url")
                post_id = obj.get("post_id") or obj.get("id")
                creation_time = obj.get("creation_time")
                
                # Check for message / caption
                text = ""
                message_obj = obj.get("message")
                if isinstance(message_obj, dict):
                    text = message_obj.get("text", "")
                elif isinstance(message_obj, str):
                    text = message_obj

                # Detect if it's a media post
                if permalink and isinstance(permalink, str):
                    # Clean URL query params like ?comment_id=...
                    clean_link = permalink.split("?")[0].rstrip("/")
                    
                    is_reel = "/reel/" in clean_link
                    is_video = "/videos/" in clean_link or "watch" in clean_link
                    is_photo = "/photo.php" in permalink or "/posts/" in clean_link or "/photos/" in clean_link

                    if is_reel or is_video or is_photo:
                        key = str(post_id) if post_id else clean_link
                        
                        # Find photo url if photo post
                        photo_url = None
                        if is_photo:
                            attachments = obj.get("attachments") or []
                            for att in attachments:
                                media = att.get("media") or {}
                                image = media.get("image") or {}
                                if image.get("uri"):
                                    photo_url = image.get("uri")
                                    break

                        if key not in discovered:
                            discovered[key] = {
                                "id": key,
                                "url": permalink,
                                "clean_url": clean_link,
                                "type": "video" if (is_reel or is_video) else "photo",
                                "title": text,
                                "creation_time": creation_time,
                                "page_name": page_name,
                                "page_url": page_url,
                                "photo_url": photo_url,
                            }

                for v in obj.values():
                    parse_recursive(v)
            elif isinstance(obj, list):
                for item in obj:
                    parse_recursive(item)

        for scr in scripts:
            try:
                data = json.loads(scr)
                parse_recursive(data)
            except Exception:
                continue

        # Sort posts by creation time descending if available
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

    async def _download_and_broadcast(self, post: dict):
        """
        Downloads the media and sends it to all subscribers.
        """
        if not self.subscribers:
            logger.warning("No subscriber chat IDs registered to receive media. Run /start in the bot chat.")
            return

        post_url = post["url"]
        post_type = post["type"]
        page_name = post["page_name"]
        raw_caption = post.get("title") or ""

        # Construct informative caption
        caption_lines = [
            f"🔔 **New {post_type.capitalize()} Uploaded!**",
            f"📘 **Page:** {page_name}",
        ]
        if raw_caption:
            caption_lines.append(f"📌 {raw_caption[:200]}")
        caption_lines.append(f"\n🔗 [Original Link]({post_url})")
        caption_text = "\n".join(caption_lines)

        temp_dir = tempfile.mkdtemp(prefix="fb_download_")
        try:
            if post_type == "video":
                video_path, thumb_path, info = await asyncio.to_thread(
                    self._download_and_process_video, post_url, temp_dir
                )
                if not video_path or not os.path.exists(video_path):
                    logger.error(f"Failed to download video from {post_url}")
                    return

                size = os.path.getsize(video_path)
                if size > (50 * 1024 * 1024):
                    logger.warning(f"Video {post_url} size ({size // 1048576} MB) exceeds 50MB limit.")
                    for chat_id in list(self.subscribers):
                        try:
                            await self.bot.send_message(
                                chat_id,
                                f"⚠️ New video on **{page_name}** exceeds Telegram's 50 MB limit.\n\n🔗 Watch here: {post_url}"
                            )
                        except Exception as ce:
                            logger.error(f"Send warning error to {chat_id}: {ce}")
                    return

                thumb_input = FSInputFile(thumb_path) if thumb_path and os.path.exists(thumb_path) else None
                duration = int(info.get("duration", 0)) if info.get("duration") else None
                width = int(info.get("width")) if info.get("width") else None
                height = int(info.get("height")) if info.get("height") else None

                # Fallback to direct inspection to guarantee exact native aspect ratio
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
                            parse_mode="Markdown",
                            thumbnail=thumb_input,
                            duration=duration,
                            width=width,
                            height=height,
                            supports_streaming=True
                        )
                        logger.info(f"Delivered video to chat {chat_id} from {page_name}")
                    except Exception as e:
                        logger.error(f"Error sending video to {chat_id}: {e}")

            elif post_type == "photo":
                photo_target = post.get("photo_url") or post_url
                # Try downloading photo
                img_path = os.path.join(temp_dir, "photo.jpg")
                if post.get("photo_url"):
                    import requests
                    r = requests.get(post["photo_url"], timeout=15)
                    with open(img_path, "wb") as pf:
                        pf.write(r.content)
                else:
                    # Use yt-dlp to extract image
                    ydl_opts = {"outtmpl": os.path.join(temp_dir, "photo.%(ext)s"), "quiet": True}
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([post_url])
                    downloaded = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, f))]
                    img_path = downloaded[0] if downloaded else None

                if img_path and os.path.exists(img_path):
                    for chat_id in list(self.subscribers):
                        try:
                            photo_input = FSInputFile(img_path)
                            await self.bot.send_photo(
                                chat_id=chat_id,
                                photo=photo_input,
                                caption=caption_text,
                                parse_mode="Markdown"
                            )
                            logger.info(f"Delivered photo to chat {chat_id} from {page_name}")
                        except Exception as e:
                            logger.error(f"Error sending photo to {chat_id}: {e}")

        except Exception as e:
            logger.error(f"Error in media download & broadcast: {e}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------
    # SINGLE CHECK PASS OVER ALL PAGES
    # -------------------------------------------------------------------
    async def check_pages_once(self, announce_empty: bool = False) -> int:
        """
        Runs one polite check cycle across all configured Facebook pages.
        Returns total number of new posts discovered.
        """
        if self.is_checking:
            logger.info("A check pass is already in progress, skipping duplicate call.")
            return 0

        self.is_checking = True
        new_items_count = 0
        self.last_check_time = time.time()

        try:
            logger.info(f"Starting check pass over {len(self.pages)} pages...")
            
            for page_url in self.pages:
                logger.info(f"Checking Facebook page: {page_url}")
                html, final_url = await asyncio.to_thread(self._fetch_page_html, page_url)
                
                if not html:
                    logger.warning(f"Could not retrieve HTML for {page_url}")
                    # Stagger delay before next page
                    await asyncio.sleep(random.uniform(5, 10))
                    continue

                posts = self._extract_posts_from_html(html, page_url)
                logger.info(f"Found {len(posts)} posts/reels in payload for {page_url}")

                # If it's the very first time running and seen_posts is empty,
                # mark existing posts as seen to avoid flooding historical content,
                # unless explicitly desired.
                first_run = len(self.seen_posts) == 0
                
                for post in posts:
                    post_key = post["id"]
                    clean_url = post["clean_url"]

                    if self.is_seen(post_key) or self.is_seen(clean_url):
                        continue

                    if first_run:
                        # Record baseline without spamming
                        logger.info(f"Baseline post recorded: {post['clean_url']}")
                        self.mark_seen(post_key, post)
                        self.mark_seen(clean_url, post)
                        continue

                    # NEW POST DETECTED!
                    logger.info(f"🔥 NEW UPLOAD DETECTED: {post['clean_url']} ({post['type']})")
                    self.mark_seen(post_key, post)
                    self.mark_seen(clean_url, post)
                    new_items_count += 1

                    # Download & send directly to the Telegram bot chat
                    await self._download_and_broadcast(post)

                # Polite delay between consecutive pages to avoid pattern detection
                await asyncio.sleep(random.uniform(6.0, 14.0))

        except Exception as e:
            logger.error(f"Error during check cycle: {e}")
        finally:
            self.is_checking = False

        logger.info(f"Check pass completed. Discovered {new_items_count} new posts.")
        return new_items_count

    # -------------------------------------------------------------------
    # BACKGROUND SCHEDULER LOOP (WITH ADAPTIVE BACKOFF & RANDOM JITTER)
    # -------------------------------------------------------------------
    async def start_loop(self):
        """
        Continuous polite background loop.
        Automatically avoids Facebook bot blocks by:
        - Introducing ±60s random jitter between checks.
        - Backing off exponentially if Facebook throttles (consecutive_errors).
        """
        self.running = True
        logger.info("Facebook auto-checker loop started.")
        
        # Initial check after 10s warmup
        await asyncio.sleep(10)
        await self.check_pages_once()

        while self.running:
            try:
                # Calculate sleep duration with jitter
                jitter = random.uniform(-45.0, 75.0)
                current_interval = self.check_interval + jitter

                # Exponential backoff if consecutive errors occurred
                if self.consecutive_errors > 0:
                    backoff_multiplier = min(self.consecutive_errors, 5)
                    backoff_delay = backoff_multiplier * 300  # up to 25 mins backoff
                    logger.warning(
                        f"Backing off for {backoff_delay}s due to {self.consecutive_errors} consecutive errors."
                    )
                    await asyncio.sleep(backoff_delay)
                else:
                    logger.info(f"Sleeping for {int(current_interval)}s until next Facebook check...")
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
