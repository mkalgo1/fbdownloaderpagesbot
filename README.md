# Facebook Page Auto-Checker & Video Downloader Bot

A dedicated Telegram bot (`@fbdownloaderpagesvidsbot`) that continuously auto-tracks specified Facebook pages, grabs new video/reel/photo uploads, and delivers them directly into your Telegram chat with native H.264 HD streaming and 100% Camera Roll compatibility.

## Monitored Pages (Default)
1. `https://web.facebook.com/profile.php?id=61556328743295` (Paris France Tour)
2. `https://web.facebook.com/profile.php?id=100083193627339` (FX France Express)
3. `https://web.facebook.com/lavialyon` (La vie à Lyon France)

## Commands
* `/add` - Interactive prompt to add a new page (supports page URL, username, or any video/reel from that page)
* `/del <number>` - Remove a page from the monitor list
* `/pages` - View all monitored pages
* `/checknow` - Run an immediate check pass of all pages
* `/status` - View bot uptime, seen posts count, and subscriber statistics
* `/cancel` - Abort active `/add` flow
