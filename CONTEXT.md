# Project Context & Architecture Ledger: NoVideos (@novidsbot)

## 1. Project Overview & Infrastructure
- **Bot Name / Username:** NoVideos (`@novidsbot`)
- **Repository:** `https://github.com/atsumia/novideos.git`
- **Hosting Platform:** Instabox, `vanya-agent` (`m_agqzxzix6jgflu7f2lvxmwjsc4`), migrated 2026-11-07.
- **Public Domain:** `https://18081-agqzxzix6jgflu7f2lvxmwjsc4.instabox.tech`
- **Telegram Bot API Token:** Configured via `/data/novideos/app/deployment.env` (Never commit tokens)
- **Core Stack:** Python 3.11, `aiogram` 3.x, `aiohttp`, `yt-dlp`, SQLite (`/data/novideos/data/database.db`), `python-dotenv`, `ffmpeg`.

## Current Deployment (2026-11-07)
- **Path Isolation:** Dedicated persistent directory on Instabox:
  - Application & Docker Compose: `/data/novideos/app`
  - Persistent SQLite Database: `/data/novideos/data/database.db`
  - Ephemeral cache/transcode: `/app/temp` inside container (cleaned periodically)
- **Transport Architecture:** Telegram HTTPS Webhook registered at `/telegram/webhook` with `WEBHOOK_SECRET`. Incoming Telegram webhook requests wake the VM if paused.
- **Port Routing:** Published public Instabox port 18081 mapped to container port 8080 (`0.0.0.0:18081->8080/tcp`).
- **Keepalive & Wake Automation:**
  - GitHub Actions keepalive workflow pinging `/health` every 5 minutes (`.github/workflows/instabox-keepalive.yml`).
  - Sprintbox VPS (`186.246.47.218`) lease renewal via `novpn-instabox-wake.timer` every 15 seconds.
- **Railway Decommissioning:** Disconnected GitHub source repo from Railway service `novideos` (project `pleasant-compassion`); service is offline with 0 active deployments.

## 2. Inline Mode & Performance Architecture
- **Fast-path TikWM Extraction:** Calls TikWM directly on raw URLs (`vm.tiktok.com`, `vt.tiktok.com`) without redirect hops (~0.8s).
- **Direct CDN Streaming for Normal Videos:** Regular videos use TikWM's direct watermark-free CDN MP4 (`download_url`), resulting in zero CPU/bandwidth load on Instabox and instant Telegram client delivery.
- **On-Demand Slideshow Assembly:** Photo slideshows are converted to vertical 1080x1920 MP4 via `ffmpeg` only on demand when requested through `/api/video/{video_id}.mp4`.
- **Telegram Edge Caching:** `answer_inline_query` sets `cache_time=300, is_personal=False`, allowing Telegram's global CDN to cache inline results for 5 minutes.
- **Clean Video Delivery:** All captions and inline buttons are removed (`caption=None, reply_markup=None`) for pure minimalist video output.
