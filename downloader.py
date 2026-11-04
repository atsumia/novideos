import os
import re
import uuid
import asyncio
import aiohttp
from typing import Optional, Dict, Any, List

TIKTOK_URL_REGEX = re.compile(
    r'https?://(?:(?:www|vt|vm|m|t)\.)?(?:tiktok\.com|douyin\.com)/[^\s]+',
    re.IGNORECASE
)
TIKTOK_VIDEO_ID_REGEX = re.compile(r'/video/(\d+)', re.IGNORECASE)

DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
    'Referer': 'https://www.tiktok.com/',
}

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(5)


def find_tiktok_urls(text: str) -> List[str]:
    """Finds all TikTok links in a text string."""
    if not text:
        return []
    matches = TIKTOK_URL_REGEX.findall(text)
    clean_urls = []
    for match in matches:
        clean = match.split('?')[0] if '?' in match and ('/video/' in match) else match
        clean = clean.rstrip(').,;!?')
        if clean not in clean_urls:
            clean_urls.append(clean)
    return clean_urls


async def resolve_canonical_url(url: str) -> str:
    """Follows redirects for shortened links (vm.tiktok.com, vt.tiktok.com)."""
    try:
        timeout = aiohttp.ClientTimeout(total=7)
        async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
            async with session.get(url, allow_redirects=True, timeout=timeout) as resp:
                return str(resp.url)
    except Exception as e:
        print(f"⚠️ [RESOLVE URL ERROR]: {e}")
        return url


def extract_video_id_from_url(url: str) -> Optional[str]:
    match = TIKTOK_VIDEO_ID_REGEX.search(url)
    if match:
        return match.group(1)
    return None


async def fetch_via_tikwm(url: str) -> Optional[Dict[str, Any]]:
    """Fetches video metadata and direct CDN stream URL via TikWM API."""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
            async with session.post(
                'https://www.tikwm.com/api/',
                data={'url': url, 'hd': 1},
                timeout=timeout
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if data.get('code') != 0:
                    return None
                
                item = data.get('data', {})
                play_url = item.get('play') or item.get('wmplay')
                if not play_url:
                    return None

                # TikWM may return protocol-relative URLs
                if play_url.startswith('//'):
                    play_url = f"https:{play_url}"
                elif not play_url.startswith('http'):
                    play_url = f"https://www.tikwm.com{play_url}"

                cover_url = item.get('cover') or item.get('origin_cover')
                if cover_url and cover_url.startswith('//'):
                    cover_url = f"https:{cover_url}"

                video_id = str(item.get('id') or extract_video_id_from_url(url) or uuid.uuid4().hex[:12])
                author_data = item.get('author') or {}
                author_name = author_data.get('nickname') or author_data.get('unique_id') or "TikTok Creator"
                title = item.get('title') or "TikTok Video"
                duration = int(item.get('duration') or 0)
                size_bytes = int(item.get('size') or 0)

                return {
                    'video_id': video_id,
                    'title': title.strip(),
                    'author': author_name.strip(),
                    'download_url': play_url,
                    'cover_url': cover_url or "",
                    'duration': duration,
                    'size': size_bytes,
                    'source': 'tikwm'
                }
    except Exception as e:
        print(f"⚠️ [TIKWM API ERROR]: {e}")
        return None


async def fetch_via_ytdlp(url: str) -> Optional[Dict[str, Any]]:
    """Fallback metadata extractor using yt-dlp."""
    def _extract():
        import yt_dlp
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'skip_download': True,
            'socket_timeout': 10,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.to_thread(_extract)
        if not info:
            return None

        # Find best mp4 video stream
        direct_url = info.get('url')
        if not direct_url and 'formats' in info:
            for f in reversed(info['formats']):
                if f.get('ext') == 'mp4' and f.get('url'):
                    direct_url = f['url']
                    break

        if not direct_url:
            return None

        video_id = str(info.get('id') or extract_video_id_from_url(url) or uuid.uuid4().hex[:12])
        title = info.get('title') or "TikTok Video"
        author = info.get('uploader') or info.get('channel') or "TikTok Creator"
        cover = info.get('thumbnail') or ""
        duration = int(info.get('duration') or 0)

        return {
            'video_id': video_id,
            'title': title.strip(),
            'author': author.strip(),
            'download_url': direct_url,
            'cover_url': cover,
            'duration': duration,
            'size': 0,
            'source': 'ytdlp'
        }
    except Exception as e:
        print(f"⚠️ [YT-DLP EXTRACTION ERROR]: {e}")
        return None


async def get_tiktok_info(raw_url: str) -> Optional[Dict[str, Any]]:
    """Resolves and extracts TikTok video information with sub-second fallback."""
    canonical_url = await resolve_canonical_url(raw_url)
    
    # Primary: TikWM API (Fastest: ~0.5s - 1.5s, clean waterless MP4)
    info = await fetch_via_tikwm(canonical_url)
    if not info and canonical_url != raw_url:
        info = await fetch_via_tikwm(raw_url)

    # Secondary: yt-dlp fallback
    if not info:
        info = await fetch_via_ytdlp(canonical_url)

    if info:
        info['canonical_url'] = canonical_url
        info['original_url'] = raw_url

    return info


async def download_video_file(
    download_url: str,
    video_id: str,
    output_dir: str = "temp",
    max_bytes: int = 50 * 1024 * 1024
) -> Optional[str]:
    """Downloads video file locally for Telegram upload in PM mode."""
    os.makedirs(output_dir, exist_ok=True)
    temp_filename = f"tt_{video_id}_{uuid.uuid4().hex[:6]}.mp4"
    target_path = os.path.join(output_dir, temp_filename)

    async with DOWNLOAD_SEMAPHORE:
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
                async with session.get(download_url, timeout=timeout) as resp:
                    if resp.status != 200:
                        return None
                    
                    downloaded = 0
                    with open(target_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                print(f"⚠️ [DOWNLOAD ABORTED]: File exceeds {max_bytes} bytes")
                                cleanup_file(target_path)
                                return None
                            f.write(chunk)
            
            if os.path.exists(target_path) and os.path.getsize(target_path) > 1024:
                return target_path
            cleanup_file(target_path)
            return None
        except Exception as e:
            print(f"❌ [DOWNLOAD VIDEO ERROR]: {e}")
            cleanup_file(target_path)
            return None


def cleanup_file(file_path: Optional[str]) -> None:
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            print(f"⚠️ [CLEANUP ERROR]: {e}")
