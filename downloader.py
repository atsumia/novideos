import os
import re
import uuid
import time
import asyncio
import subprocess
import aiohttp
from typing import Optional, Dict, Any, List

TIKTOK_URL_REGEX = re.compile(
    r'https?://(?:(?:www|vt|vm|m|t)\.)?(?:tiktok\.com|douyin\.com)/[^\s]+',
    re.IGNORECASE
)
TIKTOK_VIDEO_ID_REGEX = re.compile(r'/(?:video|photo)/(\d+)', re.IGNORECASE)

DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
    'Referer': 'https://www.tiktok.com/',
}

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(5)
_VIDEO_BUILD_LOCKS: Dict[str, asyncio.Lock] = {}
_LOCK_ACCESS_LOCK = asyncio.Lock()


async def get_video_lock(video_id: str) -> asyncio.Lock:
    async with _LOCK_ACCESS_LOCK:
        if video_id not in _VIDEO_BUILD_LOCKS:
            _VIDEO_BUILD_LOCKS[video_id] = asyncio.Lock()
        return _VIDEO_BUILD_LOCKS[video_id]


def find_tiktok_urls(text: str) -> List[str]:
    """Finds all TikTok links in a text string."""
    if not text:
        return []
    matches = TIKTOK_URL_REGEX.findall(text)
    clean_urls = []
    for match in matches:
        clean = match.split('?')[0] if '?' in match and ('/video/' in match or '/photo/' in match) else match
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
                play_url = item.get('play') or item.get('wmplay') or ""

                if play_url.startswith('//'):
                    play_url = f"https:{play_url}"
                elif play_url and not play_url.startswith('http'):
                    play_url = f"https://www.tikwm.com{play_url}"

                cover_url = item.get('cover') or item.get('origin_cover') or ""
                if cover_url.startswith('//'):
                    cover_url = f"https:{cover_url}"

                video_id = str(item.get('id') or extract_video_id_from_url(url) or uuid.uuid4().hex[:12])
                author_data = item.get('author') or {}
                author_name = author_data.get('nickname') or author_data.get('unique_id') or "TikTok Creator"
                title = item.get('title') or "TikTok Video"
                duration = int(item.get('duration') or 0)
                size_bytes = int(item.get('size') or 0)

                raw_images = item.get('images') or []
                clean_images = []
                for img in raw_images:
                    if img.startswith('//'):
                        clean_images.append(f"https:{img}")
                    else:
                        clean_images.append(img)

                # Определяем, является ли пост фото-слайдшоу
                is_slideshow = bool(clean_images) or ('mime_type=audio_mpeg' in play_url) or ('/photo/' in url)
                if is_slideshow and not clean_images and cover_url:
                    clean_images = [cover_url]

                music_url = item.get('music') or play_url
                if music_url.startswith('//'):
                    music_url = f"https:{music_url}"

                return {
                    'video_id': video_id,
                    'title': title.strip(),
                    'author': author_name.strip(),
                    'is_slideshow': is_slideshow,
                    'images': clean_images,
                    'music_url': music_url,
                    'download_url': play_url if not is_slideshow else None,
                    'cover_url': cover_url,
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
            'is_slideshow': False,
            'images': [],
            'music_url': None,
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
    """Resolves and extracts TikTok video/photo info."""
    canonical_url = await resolve_canonical_url(raw_url)

    info = await fetch_via_tikwm(canonical_url)
    if not info and canonical_url != raw_url:
        info = await fetch_via_tikwm(raw_url)

    if not info:
        info = await fetch_via_ytdlp(canonical_url)

    if info:
        info['canonical_url'] = canonical_url
        info['original_url'] = raw_url

    return info


async def _download_file(url: str, dest_path: str, timeout_sec: int = 20) -> bool:
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    with open(dest_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            f.write(chunk)
                    return os.path.exists(dest_path) and os.path.getsize(dest_path) > 100
    except Exception as e:
        print(f"⚠️ [_download_file error]: {e} ({url[:60]})")
    return False


def _render_slideshow_ffmpeg(image_paths: List[str], audio_path: Optional[str], output_path: str) -> bool:
    """Uses ffmpeg to stitch images and audio into a vertical 1080x1920 MP4 video."""
    try:
        temp_dir = os.path.dirname(output_path)
        list_file = os.path.join(temp_dir, f"concat_{uuid.uuid4().hex[:6]}.txt")

        # Время показа одного слайда (3 секунды)
        slide_dur = 3.0
        with open(list_file, 'w', encoding='utf-8') as f:
            for p in image_paths:
                f.write(f"file '{os.path.abspath(p)}'\n")
                f.write(f"duration {slide_dur}\n")
            if image_paths:
                f.write(f"file '{os.path.abspath(image_paths[-1])}'\n")

        total_dur = max(5, int(len(image_paths) * slide_dur))

        # Собираем команду ffmpeg
        vf_filter = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,format=yuv420p"

        if audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
            cmd = [
                'ffmpeg', '-y',
                '-f', 'concat', '-safe', '0', '-i', list_file,
                '-i', audio_path,
                '-vf', vf_filter,
                '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'stillimage',
                '-c:a', 'aac', '-b:a', '128k',
                '-t', str(total_dur),
                '-shortest',
                output_path
            ]
        else:
            cmd = [
                'ffmpeg', '-y',
                '-f', 'concat', '-safe', '0', '-i', list_file,
                '-vf', vf_filter,
                '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'stillimage',
                '-t', str(total_dur),
                output_path
            ]

        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cleanup_file(list_file)

        if p.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            return True
        else:
            print(f"❌ [FFMPEG ERROR]: {p.stderr.decode('utf-8', errors='ignore')[:300]}")
            return False
    except Exception as e:
        print(f"❌ [_render_slideshow_ffmpeg error]: {e}")
        return False


async def ensure_video_file(info: Dict[str, Any], temp_dir: str = "temp") -> Optional[str]:
    """
    Ensures that a valid vertical MP4 video file exists for this video/photo post.
    Converts photo slideshows to video or downloads direct MP4.
    """
    video_id = info['video_id']
    target_mp4 = os.path.join(temp_dir, f"video_{video_id}.mp4")

    if os.path.exists(target_mp4) and os.path.getsize(target_mp4) > 1024:
        return target_mp4

    lock = await get_video_lock(video_id)
    async with lock:
        if os.path.exists(target_mp4) and os.path.getsize(target_mp4) > 1024:
            return target_mp4

        os.makedirs(temp_dir, exist_ok=True)
        is_slideshow = info.get('is_slideshow', False)

        if is_slideshow:
            images = info.get('images') or []
            if not images and info.get('cover_url'):
                images = [info['cover_url']]

            temp_images = []
            for idx, img_url in enumerate(images[:10]):  # Максимум 10 слайдов
                ext = 'png' if '.png' in img_url.lower() else 'jpeg'
                local_img = os.path.join(temp_dir, f"slide_{video_id}_{idx}.{ext}")
                ok = await _download_file(img_url, local_img)
                if ok:
                    temp_images.append(local_img)

            music_file = None
            if info.get('music_url'):
                music_file = os.path.join(temp_dir, f"audio_{video_id}.mp3")
                await _download_file(info['music_url'], music_file)

            if not temp_images:
                print(f"❌ [SLIDESHOW ERROR]: Failed to download any images for {video_id}")
                return None

            print(f"🎬 [FFMPEG] Rendering {len(temp_images)} slides into video for {video_id}...")
            success = await asyncio.to_thread(_render_slideshow_ffmpeg, temp_images, music_file, target_mp4)

            # Очищаем временные исходники
            for img in temp_images:
                cleanup_file(img)
            cleanup_file(music_file)

            if success and os.path.exists(target_mp4):
                return target_mp4
            return None

        else:
            # Обычное видео
            download_url = info.get('download_url')
            if not download_url:
                return None

            success = await _download_file(download_url, target_mp4, timeout_sec=30)
            if success and os.path.exists(target_mp4):
                return target_mp4
            cleanup_file(target_mp4)
            return None


def cleanup_file(file_path: Optional[str]) -> None:
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception as e:
            print(f"⚠️ [CLEANUP ERROR]: {e}")
