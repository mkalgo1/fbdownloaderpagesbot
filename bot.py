import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import Dict, Optional, Tuple

import aiohttp
from aiohttp import web
import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv

# Load local environment
load_dotenv()

from fb_monitor import FacebookMonitor, DEFAULT_PAGES, resolve_facebook_page, normalize_page_url

# -------------------------------------------------------------------
# LOGGING SETUP
# -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("MediaDownloaderBot")

# -------------------------------------------------------------------
# ENVIRONMENT & THRESHOLDS
# -------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN environment variable is missing! Halting execution.")
    sys.exit(1)

MAX_TELEGRAM_BYTES = 48 * 1024 * 1024       # 48 MB safety margin for 50 MB Telegram API limit
TARGET_COMPRESSED_BYTES = 44 * 1024 * 1024  # 44 MB target for large video compression
MAX_VIDEO_DURATION_SECONDS = 3600           # 60 minutes limit
MAX_CONCURRENT_TASKS = 3                    # Concurrency semaphore
PORT = int(os.getenv("PORT", "8080"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "180"))

# Target chat ID from env if provided
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID")) if os.getenv("TARGET_CHAT_ID") else None

URL_REGEX = re.compile(r"https?://(?:www\.)?\S+", re.IGNORECASE)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# Initialize Facebook Monitor
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
fb_monitor = FacebookMonitor(
    bot=bot,
    data_dir=BASE_DIR,
    target_chat_ids=[TARGET_CHAT_ID] if TARGET_CHAT_ID else [],
    check_interval=CHECK_INTERVAL_SECONDS,
    pages=DEFAULT_PAGES,
)


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
        return {"name": "X (Twitter)", "emoji": "🐦", "desc": "X / Twitter Video"}
    elif "reddit.com" in url_l or "redd.it" in url_l:
        return {"name": "Reddit", "emoji": "👽", "desc": "Reddit Video"}
    elif "facebook.com" in url_l or "fb.watch" in url_l:
        return {"name": "Facebook", "emoji": "📘", "desc": "Facebook Video"}
    elif "twitch.tv" in url_l:
        return {"name": "Twitch", "emoji": "🟣", "desc": "Twitch Clip"}
    elif "pinterest.com" in url_l or "pin.it" in url_l:
        return {"name": "Pinterest", "emoji": "📌", "desc": "Pinterest Video"}
    elif "vimeo.com" in url_l:
        return {"name": "Vimeo", "emoji": "🎬", "desc": "Vimeo Video"}
    elif "dailymotion.com" in url_l or "dai.ly" in url_l:
        return {"name": "Dailymotion", "emoji": "📹", "desc": "Dailymotion Video"}
    else:
        return {"name": "Web", "emoji": "🌐", "desc": "Universal Web Video"}


# -------------------------------------------------------------------
# KEEP-ALIVE & WEBHOOK HTTP SERVER (RUNS ON RENDER 2)
# -------------------------------------------------------------------
async def ping_handler(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "pong",
        "message": "Facebook Auto-Checker & Media Downloader Bot is running 24/7",
        "monitored_pages": len(fb_monitor.pages),
        "subscribers": len(fb_monitor.subscribers),
        "seen_posts_count": len(fb_monitor.seen_posts),
        "initialized_pages": len(fb_monitor.initialized_pages),
    })

async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "healthy",
        "is_checking": fb_monitor.is_checking,
        "consecutive_errors": fb_monitor.consecutive_errors,
        "active_downloads": MAX_CONCURRENT_TASKS - DOWNLOAD_SEMAPHORE._value
    })

async def root_handler(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "alive",
        "service": "Facebook Auto-Checker & Media Downloader Bot",
        "endpoints": {"ping": "/ping", "health": "/health", "webhook": "/api/webhook"}
    })

def create_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/ping", ping_handler)
    app.router.add_get("/health", health_handler)

    # Register Telegram webhook handler directly in aiohttp
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_handler.register(app, path="/api/webhook")
    setup_application(app, dp, bot=bot)

    return app

async def start_web_server() -> web.AppRunner:
    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Keep-alive & Webhook HTTP server listening on http://0.0.0.0:{PORT}")
    return runner

async def self_ping_worker():
    """Auto-ping background loop if PING_URL or RENDER_EXTERNAL_URL is set."""
    target_url = os.getenv("PING_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if not target_url:
        return

    if not target_url.endswith("/ping"):
        target_url = target_url.rstrip("/") + "/ping"

    logger.info(f"Self-ping worker initialized for target: {target_url}")
    await asyncio.sleep(60)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(target_url, timeout=15) as resp:
                    logger.info(f"Keep-alive self-ping sent -> Status: {resp.status}")
            except Exception as e:
                logger.warning(f"Self-ping failed (non-critical): {e}")
            await asyncio.sleep(10 * 60)


# -------------------------------------------------------------------
# BASELINE SEEDING HELPER
# -------------------------------------------------------------------
async def seed_page_baseline(page_url: str):
    """Fetches existing posts for a newly added page across feed & reels tab and seeds them so only new uploads trigger notifications."""
    try:
        endpoints = fb_monitor.get_target_endpoints(page_url)
        total_seeded = 0
        for ep in endpoints:
            html, _ = await asyncio.to_thread(fb_monitor._fetch_page_html, ep)
            if html:
                posts = fb_monitor._extract_posts_from_html(html, page_url)
                for post in posts:
                    if post["clean_url"].endswith("/photo.php") and not post.get("photo_url"):
                        continue
                    fb_monitor.mark_seen(post["id"], post)
                    fb_monitor.mark_seen(post["clean_url"], post)
                    total_seeded += 1
            await asyncio.sleep(2.0)

        p_key = normalize_page_url(page_url).lower()
        fb_monitor.initialized_pages.add(p_key)
        fb_monitor.page_last_checked[p_key] = time.time()

        # Also register numeric canonical URL in initialized set if present
        id_m = re.search(r"id=(\d+)", page_url) or re.search(r"/people/[^/]+/(\d+)", page_url)
        if id_m:
            canon_key = f"https://web.facebook.com/profile.php?id={id_m.group(1)}".lower()
            fb_monitor.initialized_pages.add(canon_key)
            fb_monitor.page_last_checked[canon_key] = time.time()

        fb_monitor._save_json(fb_monitor.initialized_pages_file, list(fb_monitor.initialized_pages))
        fb_monitor._save_json(fb_monitor.last_checked_file, fb_monitor.page_last_checked)
        logger.info(f"Baseline seeded for {page_url}: recorded {total_seeded} existing posts across feed & reels.")
    except Exception as e:
        logger.warning(f"Error seeding baseline for {page_url}: {e}")


# -------------------------------------------------------------------
# FFPROBE & FFMPEG ENGINE
# -------------------------------------------------------------------
def get_media_duration(file_path: str) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        file_path
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        meta = json.loads(res.stdout)
        return float(meta["format"]["duration"])
    except Exception as e:
        logger.error(f"Duration inspection error: {e}")
        return 0.0


def run_adaptive_compression(input_path: str, output_path: str, duration: float) -> bool:
    if duration <= 0:
        return False

    target_bits = TARGET_COMPRESSED_BYTES * 8
    audio_kbit = 96
    total_audio_bits = audio_kbit * 1000 * duration
    usable_video_bits = target_bits - total_audio_bits

    if usable_video_bits <= 0:
        logger.warning("Media duration exceeds bandwidth limit for 50 MB cap.")
        return False

    calculated_video_kbit = int((usable_video_bits / duration) / 1000)
    video_kbit = max(180, min(calculated_video_kbit, 2600))

    scale_expr = "scale='min(1280,iw)':-2"
    if video_kbit < 600:
        scale_expr = "scale='min(854,iw)':-2"

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-b:v", f"{video_kbit}k",
        "-maxrate", f"{int(video_kbit * 1.35)}k",
        "-bufsize", f"{int(video_kbit * 2.2)}k",
        "-preset", "veryfast",
        "-vf", scale_expr,
        "-c:a", "aac",
        "-b:a", f"{audio_kbit}k",
        "-movflags", "+faststart",
        output_path
    ]

    try:
        subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
        return os.path.exists(output_path) and os.path.getsize(output_path) <= MAX_TELEGRAM_BYTES
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg conversion failed: {e.stderr.decode('utf-8', errors='ignore')}")
        return False


# -------------------------------------------------------------------
# SYNC DOWNLOAD & EXTRACTION PIPELINE
# -------------------------------------------------------------------
def download_and_process_sync(url: str, working_dir: str) -> Tuple[Optional[str], Dict]:
    out_template = os.path.join(working_dir, "raw_stream.%(ext)s")
    cookies_file = os.getenv("FB_COOKIES_FILE", os.path.join(BASE_DIR, "cookies.txt"))

    ydl_opts = {
        "outtmpl": out_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
    }
    if os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info and info["entries"]:
            info = info["entries"][0]

    downloaded = [
        os.path.join(working_dir, f)
        for f in os.listdir(working_dir)
        if not f.endswith(".temp") and not f.endswith(".part") and os.path.isfile(os.path.join(working_dir, f))
    ]

    if not downloaded:
        return None, info

    mp4_files = [f for f in downloaded if f.lower().endswith(".mp4")]
    raw_path = mp4_files[0] if mp4_files else downloaded[0]
    raw_size = os.path.getsize(raw_path)

    if raw_size <= MAX_TELEGRAM_BYTES:
        return raw_path, info

    dur = float(info.get("duration") or get_media_duration(raw_path))
    if dur > MAX_VIDEO_DURATION_SECONDS:
        raise ValueError(f"Video duration ({int(dur // 60)}m) exceeds max allowed limit (60m).")

    comp_path = os.path.join(working_dir, f"compressed_{uuid.uuid4().hex[:6]}.mp4")
    ok = run_adaptive_compression(raw_path, comp_path, dur)

    if ok and os.path.exists(comp_path):
        return comp_path, info

    return raw_path, info


# -------------------------------------------------------------------
# BOT COMMAND HANDLERS
# -------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(msg: Message):
    fb_monitor.add_subscriber(msg.chat.id)
    pages_text = "\n".join([f"• `{p}`" for p in fb_monitor.pages])
    await msg.reply(
        "🤖 **Facebook Auto-Checker & Media Downloader Bot**\n\n"
        "✅ **You are now registered!**\n"
        "Whenever any monitored Facebook page uploads a new video, reel, or photo, "
        "it will be automatically downloaded and delivered straight here!\n\n"
        "📋 **Currently Monitored Pages:**\n"
        f"{pages_text}\n\n"
        "🛠 **Commands:**\n"
        "• `/status` - Check monitor status & seen post count\n"
        "• `/checknow` - Run an immediate check of all pages\n"
        "• `/pages` - List all monitored pages\n"
        "• `/add` or `/addpage <url>` - Add a Facebook page to monitor\n"
        "• `/del` or `/delpage <url>` - Remove a page from monitor\n\n"
        "💡 *You can also paste any video link from Instagram, TikTok, YouTube, Reddit, Facebook, Pinterest to download it on demand!*"
    )


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    last_check_str = "Never"
    if fb_monitor.last_check_time:
        import datetime
        dt = datetime.datetime.fromtimestamp(fb_monitor.last_check_time)
        last_check_str = dt.strftime("%Y-%m-%d %H:%M:%S")

    await msg.reply(
        "📊 **Bot & Monitor Status**\n\n"
        f"• **Status:** {'🔄 Checking now...' if fb_monitor.is_checking else '🟢 Idle / Monitoring'}\n"
        f"• **Monitored Pages:** {len(fb_monitor.pages)}\n"
        f"• **Initialized Pages:** {len(fb_monitor.initialized_pages)}\n"
        f"• **Total Posts Seen in DB:** {len(fb_monitor.seen_posts)}\n"
        f"• **Subscribed Chats:** {len(fb_monitor.subscribers)}\n"
        f"• **Last Check Pass:** {last_check_str}\n"
        f"• **Check Interval:** ~{fb_monitor.check_interval}s (with anti-block jitter)\n"
        f"• **Consecutive Errors:** {fb_monitor.consecutive_errors}"
    )


@dp.message(Command("checknow"))
async def cmd_checknow(msg: Message):
    if fb_monitor.is_checking:
        await msg.reply("⏳ A check is already in progress. Please wait a moment.")
        return

    status_msg = await msg.reply("🔎 Checking Facebook pages for new uploads now...")
    count = await fb_monitor.check_pages_once()
    await status_msg.edit_text(
        f"✅ Check pass finished!\n"
        f"• Pages inspected: {len(fb_monitor.pages)}\n"
        f"• New uploads discovered & delivered: {count}"
    )


@dp.message(Command("pages"))
async def cmd_pages(msg: Message):
    if not fb_monitor.pages:
        await msg.reply("No Facebook pages are currently being monitored.")
        return
    pages_list = "\n".join([f"{idx+1}. `{p}`" for idx, p in enumerate(fb_monitor.pages)])
    await msg.reply(f"📋 **Monitored Facebook Pages ({len(fb_monitor.pages)}):**\n\n{pages_list}")


LOCAL_USER_STATES = {}


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message):
    if msg.chat.id in LOCAL_USER_STATES:
        LOCAL_USER_STATES.pop(msg.chat.id, None)
        await msg.reply("❌ Operation cancelled.")


@dp.message(Command("add", "addpage"))
async def cmd_addpage(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) > 1:
        raw_input = parts[1].strip()
        status_msg = await msg.reply("🔍 **Detecting & verifying Facebook page credentials...**")
        p_url, p_name, err = await asyncio.to_thread(resolve_facebook_page, raw_input)
        if p_url:
            fb_monitor.add_page(p_url)
            fb_monitor.add_subscriber(msg.chat.id)
            asyncio.create_task(seed_page_baseline(p_url))
            pages_list = "\n".join([f"{idx+1}. `{p}`" for idx, p in enumerate(fb_monitor.pages)])
            await status_msg.edit_text(
                f"✅ **Successfully Added to Auto-Tracker!**\n\n"
                f"📘 **Page Name:** *{p_name}*\n"
                f"🔗 **Canonical URL:** `{p_url}`\n\n"
                f"⏱ **Checking Frequency:** Every 3 minutes\n"
                f"🔔 All current posts are being indexed into the baseline. Any new videos, reels, and photos uploaded from now on will be automatically delivered here in HD!\n\n"
                f"📋 **Monitored Pages ({len(fb_monitor.pages)}):**\n{pages_list}"
            )
        else:
            await status_msg.edit_text(f"❌ Could not detect Facebook page credentials.\nDetail: {err or 'Invalid link or username'}")
    else:
        LOCAL_USER_STATES[msg.chat.id] = "awaiting_add_page"
        await msg.reply(
            "➕ **Add Facebook Page to Auto-Tracker**\n\n"
            "What page would you like to add? You can reply with:\n\n"
            "1️⃣ **Page Link:** e.g. `https://facebook.com/pagename` or `https://facebook.com/profile.php?id=...`\n"
            "2️⃣ **Page Username / Slug:** e.g. `lefigaro` or `@lavialyon`\n"
            "3️⃣ **Any Video / Reel Link from that page:** e.g. `https://facebook.com/reel/123456...`\n"
            "    *(The bot will automatically detect the creator page and its ID!)*\n\n"
            "👉 *Send your page or video link now, or send `/cancel` to abort.*"
        )


@dp.message(Command("del", "delpage"))
async def cmd_delpage(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) > 1:
        target = parts[1].strip()
        removed = fb_monitor.remove_page(target)
        if removed:
            pages_list = "\n".join([f"{idx+1}. `{p}`" for idx, p in enumerate(fb_monitor.pages)])
            await msg.reply(f"🗑 Removed page from monitor: `{target}`\n\n📋 **Remaining ({len(fb_monitor.pages)}):**\n{pages_list}")
        else:
            await msg.reply(f"❌ Could not find a matching page to remove.")
    else:
        pages_list = "\n".join([f"{idx+1}. `{p}`" for idx, p in enumerate(fb_monitor.pages)])
        await msg.reply(f"🗑 **Remove a Monitored Page**\n\nSend `/del <number or url>` to remove a page:\n\n{pages_list}")


# -------------------------------------------------------------------
# DIRECT ON-DEMAND LINK DOWNLOADER (UNIVERSAL)
# -------------------------------------------------------------------
@dp.message(F.text)
async def process_link(msg: Message):
    if LOCAL_USER_STATES.get(msg.chat.id) == "awaiting_add_page":
        LOCAL_USER_STATES.pop(msg.chat.id, None)
        status_msg = await msg.reply("🔍 **Detecting Facebook page credentials from your input...**")
        p_url, p_name, err = await asyncio.to_thread(resolve_facebook_page, msg.text)
        if p_url:
            fb_monitor.add_page(p_url)
            fb_monitor.add_subscriber(msg.chat.id)
            asyncio.create_task(seed_page_baseline(p_url))
            pages_list = "\n".join([f"{idx+1}. `{p}`" for idx, p in enumerate(fb_monitor.pages)])
            await status_msg.edit_text(
                f"✅ **Successfully Added to Auto-Tracker!**\n\n"
                f"📘 **Page Name:** *{p_name}*\n"
                f"🔗 **Canonical URL:** `{p_url}`\n\n"
                f"⏱ **Checking Frequency:** Every 3 minutes\n"
                f"🔔 Existing posts are now recorded in the baseline. Any new videos, reels, and photos uploaded from now on will be automatically delivered here in HD!\n\n"
                f"📋 **Monitored Pages ({len(fb_monitor.pages)}):**\n{pages_list}"
            )
        else:
            await status_msg.edit_text(f"❌ Could not detect Facebook page credentials.\nDetail: {err or 'Invalid link or username'}")
        return

    match = URL_REGEX.search(msg.text)
    if not match:
        return

    url = match.group(0)
    platform = detect_platform(url)

    if platform.get("name") != "Facebook":
        await msg.reply(
            "ℹ️ **Dedicated Facebook Auto-Tracker Bot**\n\n"
            "This bot (@fbdownloaderpagesvidsbot) is dedicated exclusively to auto-tracking Facebook pages.\n\n"
            "💡 For on-demand downloads from **TikTok, Instagram, Pinterest, YouTube, X, Reddit**, please use your main downloader bot: **@vidsmkdownloaderbot**!"
        )
        return

    # Intelligent detection: If user simply pastes a Facebook Page or Profile link directly
    is_media_link = any(x in url.lower() for x in ["/reel/", "/videos/", "watch", "fb.watch", "/video.php", "/photo", "fbid="])
    if not is_media_link:
        status_msg = await msg.reply("🔍 **Detected Facebook Page link! Resolving credentials & setting up auto-tracker...**")
        p_url, p_name, err = await asyncio.to_thread(resolve_facebook_page, url)
        if p_url:
            fb_monitor.add_page(p_url)
            fb_monitor.add_subscriber(msg.chat.id)
            asyncio.create_task(seed_page_baseline(p_url))
            pages_list = "\n".join([f"{idx+1}. `{p}`" for idx, p in enumerate(fb_monitor.pages)])
            await status_msg.edit_text(
                f"✅ **Facebook Page Detected & Added to Auto-Tracker!**\n\n"
                f"📘 **Page Name:** *{p_name}*\n"
                f"🔗 **Canonical Page URL:** `{p_url}`\n\n"
                f"⏱ **Monitoring Active:** The bot automatically checks this page every 3 minutes.\n"
                f"🔔 Any new videos, reels, or photos uploaded from this moment forward will be automatically delivered to you here in HD!\n\n"
                f"📋 **All Monitored Pages ({len(fb_monitor.pages)}):**\n{pages_list}"
            )
            return
        else:
            await status_msg.edit_text(f"❌ Could not resolve Facebook page credentials: {err or 'Invalid page link'}")
            return
        await msg.reply(
            "ℹ️ **Dedicated Facebook Auto-Tracker Bot**\n\n"
            "This bot (@fbdownloaderpagesvidsbot) is dedicated exclusively to auto-tracking Facebook pages.\n\n"
            "💡 For on-demand downloads from **TikTok, Instagram, Pinterest, YouTube, X, Reddit**, please use your main downloader bot: **@vidsmkdownloaderbot**!"
        )
        return

    status_msg = await msg.reply(
        f"🔎 **Platform Detected:** {platform['emoji']} *{platform['desc']}*\n"
        f"⏳ Analyzing media stream..."
    )

    async with DOWNLOAD_SEMAPHORE:
        temp_dir = tempfile.mkdtemp(prefix="worker_media_")

        try:
            await status_msg.edit_text(f"📥 Downloading {platform['emoji']} {platform['name']} stream & checking size...")
            final_path, info = await asyncio.to_thread(download_and_process_sync, url, temp_dir)

            if not final_path or not os.path.exists(final_path):
                await status_msg.edit_text(f"❌ Could not download video from {platform['name']}. The link may be private or invalid.")
                return

            size = os.path.getsize(final_path)
            if size > (50 * 1024 * 1024):
                await status_msg.edit_text(
                    f"⚠️ Video size ({size // (1024 * 1024)} MB) exceeds Telegram's 50 MB upload limit."
                )
                return

            await status_msg.edit_text(f"🚀 Uploading {platform['name']} media to Telegram...")

            title = info.get("title", f"{platform['name']} Video")
            duration = int(info.get("duration", 0)) if info.get("duration") else None
            width = int(info.get("width")) if info.get("width") else None
            height = int(info.get("height")) if info.get("height") else None

            if not width or not height or not duration:
                ew, eh, ed = fb_monitor.get_exact_video_dimensions(final_path)
                width = width or ew or None
                height = height or eh or None
                duration = duration or ed or None

            payload = FSInputFile(final_path, filename=f"video_{uuid.uuid4().hex[:6]}.mp4")

            await msg.reply_video(
                video=payload,
                caption=f"🎬 **{platform['emoji']} {platform['name']}**\n\n📌 {title[:120]}",
                duration=duration,
                width=width,
                height=height,
                supports_streaming=True
            )

            await status_msg.delete()

        except yt_dlp.utils.DownloadError as de:
            clean_err = str(de).split(";")[0].replace("ERROR:", "").strip()
            await status_msg.edit_text(f"❌ Extraction error: {clean_err}")
        except ValueError as ve:
            await status_msg.edit_text(f"⚠️ {str(ve)}")
        except Exception as e:
            logger.error(f"Handler processing error: {e}")
            await status_msg.edit_text("❌ An unexpected error occurred while processing the video.")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


# -------------------------------------------------------------------
# APPLICATION ENTRYPOINT
# -------------------------------------------------------------------
async def main():
    logger.info("Initializing bot worker, web server, and Facebook auto-checker...")
    web_runner = await start_web_server()
    asyncio.create_task(self_ping_worker())
    
    # Launch Facebook Monitor background loop
    monitor_task = asyncio.create_task(fb_monitor.start_loop())

    # Detect execution environment
    render_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL")
    if not render_url and (os.getenv("RENDER") or os.getenv("PORT") == "8080"):
        render_url = "https://fbdownloaderpagesbot.onrender.com"

    force_polling = "--poll" in sys.argv or os.getenv("FORCE_POLLING", "").lower() in ("1", "true")

    if render_url and not force_polling:
        webhook_target = f"{render_url.rstrip('/')}/api/webhook"
        logger.info(f"Configuring Telegram webhook for Render: {webhook_target}")
        await bot.set_webhook(
            url=webhook_target,
            allowed_updates=["message"],
            drop_pending_updates=True
        )
        logger.info(f"✅ Webhook successfully registered: {webhook_target}")
        try:
            # Keep process running while aiohttp handles webhooks & pings
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            fb_monitor.stop()
            monitor_task.cancel()
            await web_runner.cleanup()
    else:
        logger.info("Starting Telegram long polling mode...")
        await bot.delete_webhook(drop_pending_updates=True)
        try:
            await dp.start_polling(bot)
        finally:
            fb_monitor.stop()
            monitor_task.cancel()
            await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
