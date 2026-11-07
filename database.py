import os
import sqlite3
import threading
from typing import Optional, Dict, Any

DB_PATH = os.getenv("DB_PATH") or os.getenv("DATABASE_PATH", "data/database.db")
_lock = threading.Lock()


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                tiktok_id TEXT PRIMARY KEY,
                canonical_url TEXT,
                file_id TEXT,
                title TEXT,
                author TEXT,
                duration INTEGER,
                cover_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                downloads_count INTEGER DEFAULT 0,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                target_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_videos_canonical ON videos (canonical_url)")
        conn.commit()
        conn.close()


def get_cached_video(video_key: str) -> Optional[Dict[str, Any]]:
    """Retrieves cached video record by tiktok_id or canonical_url."""
    if not video_key:
        return None
    with _lock:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM videos WHERE tiktok_id = ? OR canonical_url = ? LIMIT 1",
            (video_key, video_key)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(row)
        return None


def save_cached_video(
    tiktok_id: str,
    canonical_url: str,
    file_id: Optional[str],
    title: str = "",
    author: str = "",
    duration: int = 0,
    cover_url: str = ""
) -> None:
    """Upserts video information and telegram file_id."""
    with _lock:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO videos (tiktok_id, canonical_url, file_id, title, author, duration, cover_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tiktok_id) DO UPDATE SET
                file_id = COALESCE(excluded.file_id, videos.file_id),
                canonical_url = COALESCE(excluded.canonical_url, videos.canonical_url),
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE videos.title END,
                author = CASE WHEN excluded.author != '' THEN excluded.author ELSE videos.author END,
                duration = CASE WHEN excluded.duration > 0 THEN excluded.duration ELSE videos.duration END,
                cover_url = CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE videos.cover_url END
        """, (tiktok_id, canonical_url, file_id, title, author, duration, cover_url))
        conn.commit()
        conn.close()


def register_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    with _lock:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_active = CURRENT_TIMESTAMP
        """, (user_id, username or "", first_name or ""))
        conn.commit()
        conn.close()


def increment_downloads(user_id: int, action: str, target_id: str = "") -> None:
    with _lock:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users
            SET downloads_count = downloads_count + 1,
                last_active = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (user_id,))
        cursor.execute("""
            INSERT INTO stats (user_id, action, target_id)
            VALUES (?, ?, ?)
        """, (user_id, action, target_id))
        conn.commit()
        conn.close()


def get_stats() -> Dict[str, int]:
    with _lock:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM videos")
        total_cached = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM stats WHERE action IN ('inline_download', 'pm_download')")
        total_downloads = cursor.fetchone()[0]
        conn.close()
        return {
            "total_users": total_users,
            "total_cached": total_cached,
            "total_downloads": total_downloads
        }
