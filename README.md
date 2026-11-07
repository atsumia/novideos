# NoVideos Bot (@novidsbot) 🎬

Быстрый, легковесный и оптимизированный Telegram-бот для скачивания видео и слайдшоу из TikTok без водяного знака с поддержкой **инлайн-режима**.

---

## 🚀 Продакшн на Instabox

Развёрнут на Instabox, машина `vanya-agent` (`m_agqzxzix6jgflu7f2lvxmwjsc4`).

- **Публичный домен / Webhook:** https://18081-agqzxzix6jgflu7f2lvxmwjsc4.instabox.tech
- **Каталог на машине:** `/data/novideos/app`
- **База данных SQLite:** `/data/novideos/data/database.db`
- **Конфигурация:** `/data/novideos/app/deployment.env` (0600, исключён из Git и Docker-образа)
- **Транспорт:** HTTPS Webhook `/telegram/webhook` с проверкой `WEBHOOK_SECRET`
- **Keepalive:** GitHub Actions workflow каждые 5 минут + wake-контроллер с VPS

### Управление на Instabox:
```bash
cd /data/novideos/app
bash deploy.sh
docker compose -p novideos logs --tail 100 -f
docker compose -p novideos restart
```

---

## 🛠 Локальный запуск (Polling)

1. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```
2. Создайте `.env`:
   ```env
   BOT_TOKEN=your_token
   PORT=8080
   BOT_TRANSPORT=polling
   ```
3. Запустите:
   ```bash
   python bot.py
   ```
