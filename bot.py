import os
import sys
import time
import signal
import asyncio
from typing import Dict, Tuple, List, Any, Optional

from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineQuery,
    InlineQueryResultVideo,
    InlineQueryResultCachedVideo,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChosenInlineResult,
    FSInputFile,
    BotCommand,
    BotCommandScopeDefault,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import database
import downloader

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    print("❌ ОШИБКА: BOT_TOKEN не задан в переменных окружения или файле .env!")
    sys.exit(1)

PORT = int(os.getenv("PORT", 8080))
BASE_URL = os.getenv("BASE_URL", "https://novideos-production.up.railway.app").rstrip('/')

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Кэш inline-запросов: {query_norm: (timestamp, [results])}
_INLINE_QUERY_CACHE: Dict[str, Tuple[float, List[Any]]] = {}
_INLINE_CACHE_TTL = 900  # 15 минут
_INLINE_CACHE_MAX = 500

# Кэш активных метаданных видео для эндпоинта стриминга: {video_id: info_dict}
_ACTIVE_VIDEO_INFO: Dict[str, Dict[str, Any]] = {}


@dp.message(CommandStart())
async def handle_start(message: types.Message):
    user = message.from_user
    database.register_user(user.id, user.username, user.first_name)
    
    bot_info = await bot.get_me()
    bot_username = bot_info.username or "novidsbot"

    text = (
        f"🎬 <b>Привет, {user.first_name or 'друг'}!</b>\n\n"
        f"Я бот <b>NoVideos</b> — скачиваю видео и слайдшоу из TikTok без водяного знака.\n\n"
        f"🚀 <b>Как мной пользоваться:</b>\n"
        f"1️⃣ <b>В этом чате:</b> просто отправь ссылку на TikTok.\n"
        f"2️⃣ <b>В любом чате или группе (инлайн):</b> напиши в поле ввода:\n"
        f"<code>@{bot_username} ссылка_на_видео</code>\n"
        f"и выбери видео из списка — видео сразу отправится в чат!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔎 Попробовать инлайн в чате", switch_inline_query="")
    ]])
    await message.answer(text, reply_markup=kb)


@dp.message(Command("stats"))
async def handle_stats(message: types.Message):
    stats = database.get_stats()
    text = (
        "📊 <b>Статистика NoVideos:</b>\n\n"
        f"👥 Пользователей: <b>{stats['total_users']}</b>\n"
        f"💾 Видео в кэше Telegram: <b>{stats['total_cached']}</b>\n"
        f"📥 Всего скачиваний: <b>{stats['total_downloads']}</b>"
    )
    await message.answer(text)


@dp.inline_query()
async def inline_search_handler(inline_query: InlineQuery):
    t0 = time.time()
    query = inline_query.query.strip()
    
    bot_info = await bot.get_me()
    bot_username = bot_info.username or "novidsbot"

    # 1. Если поле ввода пустое
    if not query:
        help_article = InlineQueryResultArticle(
            id="help_prompt",
            title="📥 Вставь ссылку на TikTok",
            description="Например: https://vm.tiktok.com/... или https://www.tiktok.com/@.../video/...",
            thumbnail_url="https://images.unsplash.com/photo-1611162617213-7d7a39e9b1d7?w=128&q=80",
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"💡 <b>Как скачать видео через @{bot_username}:</b>\n\n"
                    f"Вставь ссылку на видео из TikTok в строке ввода после <code>@{bot_username} </code>.\n\n"
                    f"Пример:\n<code>@{bot_username} https://vm.tiktok.com/...</code>"
                ),
                parse_mode="HTML"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⚡️ Попробовать", switch_inline_query_current_chat="https://vm.tiktok.com/")
            ]])
        )
        await inline_query.answer([help_article], cache_time=5, is_personal=True)
        return

    # 2. Проверяем, есть ли ссылка на TikTok в тексте
    found_urls = downloader.find_tiktok_urls(query)
    if not found_urls:
        no_url_article = InlineQueryResultArticle(
            id="no_tiktok_url",
            title="⚠️ Ссылка на TikTok не распознана",
            description="Отправь ссылку формата vm.tiktok.com или tiktok.com/@user/video/...",
            thumbnail_url="https://images.unsplash.com/photo-1594322436404-5a0526db4d13?w=128&q=80",
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"⚠️ Не удалось обнаружить ссылку на TikTok: <i>{query[:40]}</i>\n\n"
                    f"Используй ссылку формата <code>https://vm.tiktok.com/...</code>"
                ),
                parse_mode="HTML"
            )
        )
        await inline_query.answer([no_url_article], cache_time=5, is_personal=True)
        return

    target_url = found_urls[0]
    # Очищаем трекинг-параметры (?_t=..., ?_r=...) для точных попаданий в кэш
    q_norm = target_url.split('?')[0].lower().rstrip('/')
    now = time.time()

    # 3. Кэш запросов в оперативной памяти
    if q_norm in _INLINE_QUERY_CACHE:
        cached_time, cached_results = _INLINE_QUERY_CACHE[q_norm]
        if now - cached_time < _INLINE_CACHE_TTL:
            elapsed = time.time() - t0
            print(f"⚡️ [INLINE CACHE HIT] {target_url} in {elapsed:.3f}s")
            await inline_query.answer(cached_results, cache_time=300, is_personal=False)
            return

    results = []

    try:
        # 4. Проверяем базу SQLite на готовый Telegram file_id
        cached_record = database.get_cached_video(target_url)
        if not cached_record:
            extracted_id = downloader.extract_video_id_from_url(target_url)
            if extracted_id:
                cached_record = database.get_cached_video(extracted_id)

        if cached_record and cached_record.get('file_id'):
            # Мгновенная отдача через Telegram CDN (чистый видеофайл без текста и кнопок)
            file_id = cached_record['file_id']
            title = cached_record.get('title') or "TikTok видео"
            author = cached_record.get('author') or "TikTok"

            results.append(
                InlineQueryResultCachedVideo(
                    id=f"cv_{cached_record['tiktok_id']}"[:50],
                    video_file_id=file_id,
                    title=f"📹 {title[:40]}",
                    description=f"@{author}",
                    caption=None,
                    reply_markup=None
                )
            )
            elapsed = time.time() - t0
            print(f"✅ [INLINE DB HIT] Cached file_id returned for {target_url} in {elapsed:.3f}s")

        else:
            # 5. Получаем инфо о видео / слайдшоу
            info = await downloader.get_tiktok_info(target_url)
            if info:
                video_id = info['video_id']
                title = info['title'] or "TikTok видео"
                author = info['author'] or "TikTok"
                cover_url = info.get('cover_url') or "https://images.unsplash.com/photo-1611162617213-7d7a39e9b1d7?w=256&q=80"
                duration = info.get('duration') or 0
                canon_url = info.get('canonical_url') or target_url

                _ACTIVE_VIDEO_INFO[video_id] = info

                # Сохраняем в базу метаданные
                database.save_cached_video(
                    tiktok_id=video_id,
                    canonical_url=canon_url,
                    file_id=None,
                    title=title,
                    author=author,
                    duration=duration,
                    cover_url=cover_url
                )

                # Для обычного видео отдаем прямой CDN URL без нагрузки на наш сервер
                # Для слайдшоу отдаем локальный эндпоинт стриминга с ffmpeg-рендерингом
                is_slideshow = info.get('is_slideshow', False)
                if is_slideshow or not info.get('download_url'):
                    video_stream_url = f"{BASE_URL}/api/video/{video_id}.mp4"
                else:
                    video_stream_url = info['download_url']

                results.append(
                    InlineQueryResultVideo(
                        id=f"v_{video_id}"[:50],
                        video_url=video_stream_url,
                        mime_type="video/mp4",
                        thumbnail_url=cover_url,
                        title=f"📹 {title[:40]}",
                        description=f"@{author}",
                        caption=None,
                        reply_markup=None
                    )
                )
                elapsed = time.time() - t0
                print(f"✅ [INLINE STREAM] Result ready for {target_url} in {elapsed:.2f}s (slideshow={is_slideshow})")

            else:
                results.append(
                    InlineQueryResultArticle(
                        id="failed_to_load",
                        title="❌ Не удалось получить видео",
                        description="Возможно, видео удалено или является приватным",
                        thumbnail_url="https://images.unsplash.com/photo-1594322436404-5a0526db4d13?w=128&q=80",
                        input_message_content=InputTextMessageContent(
                            message_text="❌ Не удалось загрузить видео из TikTok. Проверь доступность ссылки.",
                            parse_mode="HTML"
                        )
                    )
                )

        if len(_INLINE_QUERY_CACHE) >= _INLINE_CACHE_MAX:
            oldest = next(iter(_INLINE_QUERY_CACHE))
            _INLINE_QUERY_CACHE.pop(oldest, None)
        _INLINE_QUERY_CACHE[q_norm] = (now, results)

        await inline_query.answer(results, cache_time=300, is_personal=False)

    except Exception as e:
        print(f"❌ [INLINE HANDLER ERROR]: {e}")
        try:
            await inline_query.answer([], cache_time=5, is_personal=True)
        except Exception:
            pass


@dp.chosen_inline_result()
async def handle_chosen_inline_result(chosen: ChosenInlineResult):
    user_id = chosen.from_user.id
    database.increment_downloads(user_id, "inline_download", chosen.result_id)
    print(f"🎉 [CHOSEN INLINE] User {user_id} sent result {chosen.result_id}")


@dp.message(F.text)
async def handle_direct_message(message: types.Message):
    user = message.from_user
    database.register_user(user.id, user.username, user.first_name)
    text = message.text.strip()

    found_urls = downloader.find_tiktok_urls(text)
    if not found_urls:
        await message.answer(
            "💡 <b>Отправь мне ссылку на видео из TikTok</b>, например:\n"
            "<code>https://vm.tiktok.com/...</code>\n\n"
            "Или используй меня в любом чате: <code>@novidsbot ссылка</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔎 Использовать инлайн", switch_inline_query="")
            ]])
        )
        return

    target_url = found_urls[0]
    status_msg = await message.answer("⏳ <i>Подготавливаю видео...</i>")

    try:
        # Проверяем кэш базы данных
        cached = database.get_cached_video(target_url)
        if not cached:
            vid = downloader.extract_video_id_from_url(target_url)
            if vid:
                cached = database.get_cached_video(vid)

        if cached and cached.get('file_id'):
            # Чистая отправка видео без текста и кнопок
            await message.answer_video(
                video=cached['file_id']
            )
            database.increment_downloads(user.id, "pm_download", cached['tiktok_id'])
            await status_msg.delete()
            return

        # Парсим инфо
        await status_msg.edit_text("⏳ <i>Обрабатываю видео...</i>")
        info = await downloader.get_tiktok_info(target_url)
        if not info:
            await status_msg.edit_text("❌ Не удалось загрузить видео. Возможно, оно приватное или удалено.")
            return

        video_id = info['video_id']
        title = info['title'] or "TikTok видео"
        author = info['author'] or "TikTok"
        duration = info.get('duration') or 0
        cover_url = info.get('cover_url') or ""
        canon_url = info.get('canonical_url') or target_url

        if info.get('is_slideshow'):
            await status_msg.edit_text("🎬 <i>Сборка видео из слайдшоу...</i>")
        else:
            await status_msg.edit_text("⏳ <i>Скачиваю видеофайл...</i>")

        video_file = await downloader.ensure_video_file(info)
        if not video_file or not os.path.exists(video_file):
            await status_msg.edit_text("❌ Ошибка при подготовке видеофайла.")
            return

        await status_msg.edit_text("🚀 <i>Отправляю видео...</i>")

        # Чистая отправка видео без текста и кнопок
        sent_msg = await message.answer_video(
            video=FSInputFile(video_file),
            duration=duration if duration > 0 else None
        )

        tg_file_id = sent_msg.video.file_id if sent_msg.video else None
        database.save_cached_video(
            tiktok_id=video_id,
            canonical_url=canon_url,
            file_id=tg_file_id,
            title=title,
            author=author,
            duration=duration,
            cover_url=cover_url
        )
        database.increment_downloads(user.id, "pm_download", video_id)
        await status_msg.delete()

    except Exception as e:
        print(f"❌ [PM HANDLER ERROR]: {e}")
        await status_msg.edit_text(f"⚠️ Произошла ошибка при обработке: {e}")


# ---------------- WEB SERVER & VIDEO STREAMING ENDPOINT ----------------
async def handle_api_video(request):
    """Streams the generated/downloaded MP4 video file to Telegram servers."""
    video_id = request.match_info.get("video_id", "").strip()
    if video_id.endswith(".mp4"):
        video_id = video_id[:-4]

    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET, OPTIONS, HEAD',
        'Accept-Ranges': 'bytes'
    }
    if request.method == 'OPTIONS':
        return web.Response(headers=headers)

    target_mp4 = os.path.join("temp", f"video_{video_id}.mp4")

    # Если файла еще нет — проверяем метаданные и генерируем/скачиваем
    if not (os.path.exists(target_mp4) and os.path.getsize(target_mp4) > 1024):
        info = _ACTIVE_VIDEO_INFO.get(video_id)
        if not info:
            row = database.get_cached_video(video_id)
            if row and row.get('canonical_url'):
                info = await downloader.get_tiktok_info(row['canonical_url'])

        if not info and video_id.isdigit():
            info = await downloader.get_tiktok_info(f"https://www.tiktok.com/@a/video/{video_id}")

        if info:
            await downloader.ensure_video_file(info)

    if os.path.exists(target_mp4) and os.path.getsize(target_mp4) > 1024:
        return web.FileResponse(
            target_mp4,
            headers={
                **headers,
                'Content-Type': 'video/mp4'
            }
        )

    return web.Response(text="Video not ready or not found", status=404, headers=headers)


async def handle_health(request):
    return web.json_response({"status": "healthy", "service": "novideos"})


async def handle_root(request):
    return web.json_response({
        "status": "online",
        "bot": "@novidsbot",
        "description": "TikTok Video Downloader Bot with Inline Mode"
    })


def create_web_app():
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    app.router.add_route("*", "/api/video/{video_id}", handle_api_video)
    return app


async def start_web_server():
    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🌐 [WEB SERVER] Listening on port {PORT}")


async def periodic_temp_cleanup():
    """Cleans up files in temp/ older than 20 minutes to keep disk clean."""
    while True:
        try:
            await asyncio.sleep(600)  # каждые 10 минут
            now = time.time()
            temp_dir = "temp"
            if os.path.exists(temp_dir):
                for f in os.listdir(temp_dir):
                    p = os.path.join(temp_dir, f)
                    if os.path.isfile(p):
                        # старше 20 минут (1200 сек)
                        if now - os.path.getmtime(p) > 1200:
                            downloader.cleanup_file(p)
        except Exception as e:
            print(f"⚠️ [TEMP CLEANUP ERROR]: {e}")


async def run_webhook_server():
    secret = os.getenv("WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("WEBHOOK_SECRET is required for webhook mode")

    app = create_web_app()
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=secret,
        handle_in_background=True,
    ).register(app, path="/telegram/webhook")
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"🌐 [WEB SERVER] Listening on port {PORT} (webhook mode)")

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopped.set)
        except NotImplementedError:
            pass

    try:
        commands = [
            BotCommand(command="start", description="Главное меню и инструкция"),
            BotCommand(command="stats", description="Статистика использования"),
        ]
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

        webhook_url = f"{BASE_URL.rstrip('/')}/telegram/webhook"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=secret,
            allowed_updates=["message", "inline_query", "chosen_inline_result"],
            drop_pending_updates=False,
        )
        print(f"✅ Webhook configured: {webhook_url}")
        await stopped.wait()
    finally:
        # Keep the webhook registered so incoming requests can wake an idle machine.
        await runner.cleanup()


async def main():
    database.init_db()
    asyncio.create_task(periodic_temp_cleanup())

    bot_info = await bot.get_me()
    print(f"🤖 [BOT STARTED] @{bot_info.username} ({bot_info.first_name})")

    if os.getenv("BOT_TRANSPORT", "polling") == "webhook":
        await run_webhook_server()
        return

    commands = [
        BotCommand(command="start", description="Главное меню и инструкция"),
        BotCommand(command="stats", description="Статистика использования"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

    await start_web_server()

    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass

    await dp.start_polling(
        bot,
        allowed_updates=["message", "inline_query", "chosen_inline_result"]
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Бот остановлен.")
