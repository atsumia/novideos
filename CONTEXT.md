# Project Context & Architecture Ledger: NoVideos (@novidsbot)

## 1. Project Overview & Infrastructure
- **Bot Name / Username:** NoVideos (`@novidsbot`)
- **Repository:** `https://github.com/atsumia/novideos.git`
- **Hosting Platform:** Railway (CLI deployed, Docker container, service `novideos` in project `pleasant-compassion`)
- **Public URL:** `https://novideos-production.up.railway.app`
- **Telegram Bot API Token:** `9289734668:AAH-OyIuQT-sYT5l1J4ejDnViDhQ1tgtZ9g`
- **Core Stack:** Python 3.11, `aiogram` 3.x, `aiohttp`, `yt-dlp`, SQLite (`data/database.db`), `python-dotenv`, `ffmpeg`.

## 2. Inline Mode & Video Delivery Architecture (Fix for TikTok Photo Posts / MP3 Issue)
- **Root Cause of MP3 Audio Sending:**
  - TikTok has two distinct types of posts: regular videos (`/video/`) and photo slideshows (`/photo/`).
  - For photo slideshows, TikTok does not have a native MP4 file; TikWM returns `play` containing the background music track with `mime_type=audio_mpeg`.
  - When passing that URL directly to `InlineQueryResultVideo`, Telegram inspected the content headers and downgraded the message to an MP3 audio file.
- **Applied Fix & Unified Video Streaming:**
  1. **Slideshow Detection & FFmpeg Assembly:** `downloader.py` detects photo mode (`/photo/`, `images` list, or `mime_type=audio_mpeg`). It downloads the slide images and music, then runs an ultrafast `ffmpeg` concat demuxer rendering a 1080x1920 vertical MP4 video with the original audio in ~0.8–2s.
  2. **Dedicated Video Stream Endpoint (`/api/video/{video_id}.mp4`):** Built into `aiohttp` web server. All inline video results point to this verified endpoint with `Content-Type: video/mp4` and `Accept-Ranges: bytes`.
  3. **Background Pre-rendering:** As soon as an inline query arrives, `asyncio.create_task(downloader.ensure_video_file(info))` prepares the video so that when the user taps the inline result card, the MP4 file is already cached in `temp/` and streamed to Telegram immediately.
  4. **Instant Telegram CDN Serving (`InlineQueryResultCachedVideo`):** Once a video is sent in PM or cached, the `file_id` is reused for instant 0ms delivery without any server load.
  5. **Automated Temp Cleaner:** Periodic coroutine cleans up video files in `temp/` older than 20 minutes to keep Railway disk usage strictly bounded.
