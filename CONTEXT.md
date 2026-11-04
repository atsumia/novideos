# Project Context & Architecture Ledger: NoVideos (@novidsbot)

## 1. Project Overview & Infrastructure
- **Bot Name / Username:** NoVideos (`@novidsbot`)
- **Repository:** `https://github.com/atsumia/novideos.git`
- **Hosting Platform:** Railway (CLI deployed, Docker container, service `novideos` in project `pleasant-compassion`)
- **Public URL:** `https://novideos-production.up.railway.app`
- **Telegram Bot API Token:** Configured via Railway environment variables and local `.env` (Never commit tokens)
- **Core Stack:** Python 3.11, `aiogram` 3.x, `aiohttp`, `yt-dlp`, SQLite (`data/database.db`), `python-dotenv`, `ffmpeg`.

## 2. Inline Mode & Performance Architecture
- **Root Cause of High Latency / Search Drops:**
  1. **Token Conflict (`TelegramConflictError`):** An external process (bot name: `ВОТ ТАK.ВОТ — rot563...`) is polling `getUpdates` with the same token. This constantly terminates Railway's polling loop, putting it into 5s backoff sleeps. Incoming inline queries timeout on the Telegram client side (~2.5s limit).
  2. **Redundant HTTP Redirects:** `downloader.get_tiktok_info` previously called `resolve_canonical_url` before TikWM, wasting 1.5–2s.
  3. **Heavy Background Downloads on Keystrokes:** Previously spawned ffmpeg/video downloads for every typed character in inline search.
- **Applied Optimizations:**
  1. **Fast-path TikWM Extraction:** Calls TikWM directly on raw URLs (`vm.tiktok.com`, `vt.tiktok.com`) without redirect hops. TikWM resolves shortened links in ~0.8s.
  2. **Direct CDN Streaming for Normal Videos:** Regular videos use TikWM's direct watermark-free CDN MP4 (`download_url`), resulting in 0 CPU/bandwidth load on Railway and instant Telegram client delivery.
  3. **On-Demand Slideshow Assembly:** Photo slideshows are converted to vertical 1080x1920 MP4 via `ffmpeg` only on demand when requested through `/api/video/{video_id}.mp4`.
  4. **Telegram Edge Caching:** `answer_inline_query` sets `cache_time=300, is_personal=False`, allowing Telegram's global CDN to cache inline results for 5 minutes.
  5. **Clean Video Delivery:** All captions and inline buttons are removed (`caption=None, reply_markup=None`) for pure minimalist video output.
- **Required Action to Eliminate Conflicts:**
  - Revoke token in [@BotFather](https://t.me/BotFather) (`/revoke` for `@novidsbot`) to permanently kill the rogue polling process.
