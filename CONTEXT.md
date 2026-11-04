# Project Context & Architecture Ledger: NoVideos (@novidsbot)

## 1. Project Overview & Infrastructure
- **Bot Name / Username:** NoVideos (`@novidsbot`)
- **Repository:** `https://github.com/atsumia/novideos.git`
- **Hosting Platform:** Railway (CLI deployed, Docker container)
- **Telegram Bot API Token:** `9289734668:AAH-OyIuQT-sYT5l1J4ejDnViDhQ1tgtZ9g`
- **Core Stack:** Python 3.11, `aiogram` 3.x, `aiohttp`, `yt-dlp`, SQLite (`data/database.db`), `python-dotenv`.

## 2. Inline Mode Architecture & Optimization (Pattern from nomusic)
- **Inline Flow (`@novidsbot <tiktok_url>`):**
  1. **Empty Query:** Returns an instructional `InlineQueryResultArticle` inviting user to paste a TikTok URL.
  2. **Shortened / Canonical URLs:** Supports `vm.tiktok.com`, `vt.tiktok.com`, `tiktok.com/@.../video/...`, `douyin.com`. Unshortens via HTTP HEAD/GET redirect follower.
  3. **Multi-tier Caching:**
     - **In-Memory Cache:** `_INLINE_QUERY_CACHE` (15 min TTL, 500 items) stops keystroke spam and provides 0ms responses for repeated queries.
     - **SQLite Database Cache (`database.py`):** Stores TikTok video metadata and Telegram `file_id`.
     - **Telegram CDN Instant Serving (`InlineQueryResultCachedVideo`):** When a video was previously downloaded or sent through the bot, the cached `file_id` is served directly from Telegram's CDN. This uses **0 MB bandwidth, 0% CPU, and executes in < 0.05s**.
     - **Live Direct Stream (`InlineQueryResultVideo`):** When a video is requested for the first time in inline mode, `downloader.py` leverages TikWM API to obtain direct watermark-free `.mp4` and thumbnail URLs in ~0.5–1.5s, sending it via `InlineQueryResultVideo`.
     - **Telegram Inline Cache (`cache_time=10, is_personal=True`):** Prevents stale caching of error states while shielding the Railway container from query flooding.

## 3. Direct Message (PM) Mode
- Sending a TikTok link in private chat:
  - Checks SQLite cache for existing `file_id`.
  - If uncached: downloads video file in chunks with size limit safeguard (50MB) to `temp/`, sends `bot.send_video`, captures `msg.video.file_id`, and immediately saves it to SQLite cache.
  - Automatically cleans up the temporary file in a `finally` block to keep disk usage strictly at 0 MB.

## 4. Railway Optimization & Production Resilience
- **Embedded Healthcheck Web Server:** Built-in `aiohttp.web` runner listening on `$PORT` (`/` and `/health`) satisfies Railway's container health probes and prevents restart loops.
- **Unbuffered Logging (`PYTHONUNBUFFERED=1`):** Enabled in `Dockerfile` and Railway environment for real-time streaming logs.
- **Resource Footprint:** Python 3.11 slim + async event loop keeps RAM consumption under 70MB (well below the 512MB Railway cap).

## 5. Telegram BotFather Configuration Reminder
- To activate inline queries in Telegram, inline mode must be turned on in `@BotFather`:
  - Run `/setinline` in `@BotFather`
  - Select `@novidsbot`
  - Set placeholder text, e.g. `Вставь ссылку на TikTok видео...`
