import os
import sys
import time
import asyncio
from typing import Dict, Tuple, List, Any

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

import database
import downloader

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    print("❌ ОШИБКА: BOT_TOKEN не задан в переменных окружения или файле .env!")
    sys.exit(1)

PORT = int(os.getenv("PORT", 8080))

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Кэш inline-запросов в оперативной памяти: {query_norm: (timestamp, [results])}
_INLINE_QUERY_CACHE: Dict[str, Tuple[float, List[Any]]] = {}
_INLINE_CACHE_TTL = 900  # 15 минут
_INLINE_CACHE_MAX = 500


def get_video_inline_keyboard(canonical_url: str) -> InlineKeyboardMarkup:
    buttons = []
    if canonical_url and canonical_url.startswith("http"):
        buttons.append(InlineKeyboardButton(text="🔗 В TikTok", url=canonical_url))
    buttons.append(InlineKeyboardButton(text="⚡️ Скачать ещё", switch_inline_query=""))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


@dp.message(CommandStart())
async def handle_start(message: types.Message):
    user = message.from_user
    database.register_user(user.id, user.username, user.first_name)
    
    bot_info = await bot.get_me()
    bot_username = bot_info.username or "novidsbot"

    text = (
        f"🎬 <b>Привет, {user.first_name or 'друг'}!</b>\n\n"
        f"Я бот <b>NoVideos</b> — мгновенно скачиваю видео из TikTok без водяного знака.\n\n"
        f"🚀 <b>Как мной пользоваться:</b>\n"
        f"1️⃣ <b>В этом чате:</b> просто отправь ссылку на TikTok (или поделись видео из приложения).\n"
        f"2️⃣ <b>В любом чате или группе (инлайн):</b> напиши в поле ввода:\n"
        f"<code>@{bot_username} ссылка_на_видео</code>\n"
        f"и нажми на появившуюся карточку — видео сразу отправится в чат!"
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
    user_id = inline_query.from_user.id
    
    bot_info = await bot.get_me()
    bot_username = bot_info.username or "novidsbot"

    # 1. Если поле ввода пустое — подсказываем как пользоваться
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
                    f"Пример:\n<code>@{bot_username} https://vm.tiktok.com/ZM...</code>"
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
                    f"⚠️ Не удалось обнаружить корректную ссылку на TikTok в запросе: <i>{query[:40]}</i>\n\n"
                    f"Пожалуйста, используй ссылку вида <code>https://vm.tiktok.com/...</code>"
                ),
                parse_mode="HTML"
            )
        )
        await inline_query.answer([no_url_article], cache_time=5, is_personal=True)
        return

    target_url = found_urls[0]
    q_norm = target_url.lower()
    now = time.time()

    # 3. Проверяем кэш запросов в оперативной памяти
    if q_norm in _INLINE_QUERY_CACHE:
        cached_time, cached_results = _INLINE_QUERY_CACHE[q_norm]
        if now - cached_time < _INLINE_CACHE_TTL:
            elapsed = time.time() - t0
            print(f"⚡️ [INLINE CACHE HIT] {target_url} in {elapsed:.3f}s")
            await inline_query.answer(cached_results, cache_time=15, is_personal=True)
            return

    results = []

    try:
        # 4. Проверяем базу SQLite — возможно, видео уже скачивалось и есть Telegram file_id
        cached_record = database.get_cached_video(target_url)
        if not cached_record:
            extracted_id = downloader.extract_video_id_from_url(target_url)
            if extracted_id:
                cached_record = database.get_cached_video(extracted_id)

        if cached_record and cached_record.get('file_id'):
            # МГНОВЕННЫЙ ОТВЕТ ЧЕРЕЗ TELEGRAM CDN (0 нагрузки на сервер, 0мс)
            file_id = cached_record['file_id']
            title = cached_record.get('title') or "TikTok видео"
            author = cached_record.get('author') or "TikTok"
            canon_url = cached_record.get('canonical_url') or target_url

            caption = (
                f"🎬 <b>{title}</b>\n"
                f"👤 <b>Автор:</b> {author}\n\n"
                f"🤖 Скачано без водяного знака через @{bot_username}"
            )
            kb = get_video_inline_keyboard(canon_url)

            results.append(
                InlineQueryResultCachedVideo(
                    id=f"cv_{cached_record['tiktok_id']}"[:50],
                    video_file_id=file_id,
                    title=f"📹 Скачать: {title[:40]}",
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=kb
                )
            )
            elapsed = time.time() - t0
            print(f"✅ [INLINE DB HIT] Cached file_id returned for {target_url} in {elapsed:.3f}s")

        else:
            # 5. Если в базе нет file_id — получаем прямую ссылку без водяного знака
            info = await downloader.get_tiktok_info(target_url)
            if info and info.get('download_url'):
                video_id = info['video_id']
                title = info['title'] or "TikTok видео"
                author = info['author'] or "TikTok"
                cover_url = info.get('cover_url') or "https://images.unsplash.com/photo-1611162617213-7d7a39e9b1d7?w=256&q=80"
                duration = info.get('duration') or 0
                download_url = info['download_url']
                canon_url = info.get('canonical_url') or target_url

                # Сохраняем метаданные в базу
                database.save_cached_video(
                    tiktok_id=video_id,
                    canonical_url=canon_url,
                    file_id=None,
                    title=title,
                    author=author,
                    duration=duration,
                    cover_url=cover_url
                )

                caption = (
                    f"🎬 <b>{title}</b>\n"
                    f"👤 <b>Автор:</b> {author}\n\n"
                    f"🤖 Скачано без водяного знака через @{bot_username}"
                )
                kb = get_video_inline_keyboard(canon_url)

                results.append(
                    InlineQueryResultVideo(
                        id=f"v_{video_id}"[:50],
                        video_url=download_url,
                        mime_type="video/mp4",
                        thumbnail_url=cover_url,
                        title=f"📹 Скачать: {title[:40]}",
                        description=f"👤 @{author} | ⏱ {duration} сек",
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=kb
                    )
                )
                elapsed = time.time() - t0
                print(f"✅ [INLINE LIVE PARSED] Direct stream prepared for {target_url} in {elapsed:.2f}s")

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

        # Сохраняем в кэш оперативной памяти
        if len(_INLINE_QUERY_CACHE) >= _INLINE_CACHE_MAX:
            oldest = next(iter(_INLINE_QUERY_CACHE))
            _INLINE_QUERY_CACHE.pop(oldest, None)
        _INLINE_QUERY_CACHE[q_norm] = (now, results)

        await inline_query.answer(results, cache_time=10, is_personal=True)

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

    # Поиск ссылок TikTok в тексте
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
    status_msg = await message.answer("⏳ <i>Подготавливаю видео без водяного знака...</i>")

    bot_info = await bot.get_me()
    bot_username = bot_info.username or "novidsbot"

    temp_file = None
    try:
        # Проверяем, есть ли уже cached file_id в базе
        cached = database.get_cached_video(target_url)
        if not cached:
            vid = downloader.extract_video_id_from_url(target_url)
            if vid:
                cached = database.get_cached_video(vid)

        if cached and cached.get('file_id'):
            # Быстрая отправка из Telegram CDN
            canon_url = cached.get('canonical_url') or target_url
            title = cached.get('title') or "TikTok видео"
            author = cached.get('author') or "TikTok"
            caption = (
                f"🎬 <b>{title}</b>\n"
                f"👤 <b>Автор:</b> {author}\n\n"
                f"🤖 Скачано без водяного знака через @{bot_username}"
            )
            kb = get_video_inline_keyboard(canon_url)
            await message.answer_video(
                video=cached['file_id'],
                caption=caption,
                reply_markup=kb
            )
            database.increment_downloads(user.id, "pm_download", cached['tiktok_id'])
            await status_msg.delete()
            return

        # Если file_id нет — парсим инфо
        await status_msg.edit_text("⏳ <i>Скачиваю видео с серверов TikTok...</i>")
        info = await downloader.get_tiktok_info(target_url)
        if not info or not info.get('download_url'):
            await status_msg.edit_text("❌ Не удалось загрузить видео. Возможно, оно приватное или удалено.")
            return

        video_id = info['video_id']
        download_url = info['download_url']
        title = info['title'] or "TikTok видео"
        author = info['author'] or "TikTok"
        duration = info.get('duration') or 0
        cover_url = info.get('cover_url') or ""
        canon_url = info.get('canonical_url') or target_url

        # Скачиваем во временный файл для отправки
        temp_file = await downloader.download_video_file(download_url, video_id)
        if not temp_file or not os.path.exists(temp_file):
            await status_msg.edit_text("❌ Ошибка при сохранении видеофайла. Попробуйте другую ссылку.")
            return

        await status_msg.edit_text("🚀 <i>Отправляю видео...</i>")
        caption = (
            f"🎬 <b>{title}</b>\n"
            f"👤 <b>Автор:</b> {author}\n\n"
            f"🤖 Скачано без водяного знака через @{bot_username}"
        )
        kb = get_video_inline_keyboard(canon_url)

        sent_msg = await message.answer_video(
            video=FSInputFile(temp_file),
            caption=caption,
            duration=duration if duration > 0 else None,
            reply_markup=kb
        )

        # Сохраняем полученный Telegram file_id в SQLite для мгновенной отдачи в будущем
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
    finally:
        downloader.cleanup_file(temp_file)


# ---------------- WEB SERVER FOR RAILWAY HEALTHCHECK ----------------
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
    return app


async def start_web_server():
    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🌐 [WEB SERVER] Listening on port {PORT}")


async def main():
    database.init_db()

    # Устанавливаем команды бота
    commands = [
        BotCommand(command="start", description="Главное меню и инструкция"),
        BotCommand(command="stats", description="Статистика использования"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

    bot_info = await bot.get_me()
    print(f"🤖 [BOT STARTED] @{bot_info.username} ({bot_info.first_name})")

    # Запускаем встроенный веб-сервер для Railway
    await start_web_server()

    # Запускаем polling
    await dp.start_polling(
        bot,
        allowed_updates=["message", "inline_query", "chosen_inline_result"]
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Бот остановлен.")
