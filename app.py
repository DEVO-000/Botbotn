"""
app.py v10 — для Render: только веб-сервер Mini App, без Telegram polling.

Запускать на Render с переменной окружения WEB_ONLY=1.
В этом режиме:
- Запускается ТОЛЬКО aiohttp-сервер бота (порт $PORT)
- Mini App HTML, /api/*, /health — доступны
- Telegram polling НЕ запускается (он работает на HF Spaces с тем же токеном)
- База восстанавливается из канала при старте

Использование:
  Render: WEB_ONLY=1 python app.py
"""
import os
import sys
import asyncio
import logging
import traceback

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("app")

WEB_ONLY = os.environ.get("WEB_ONLY", "").lower() in ("1", "true", "yes", "on")
log.info("[app] WEB_ONLY=%s — режим: %s", WEB_ONLY,
         "только веб-сервер (без polling)" if WEB_ONLY else "полный бот")

if not WEB_ONLY:
    log.error("[app] Этот app.py предназначен для Render с WEB_ONLY=1.")
    log.error("[app] На Render НЕ задавайте WEB_ONLY — будет конфликт токенов с HF.")
    log.error("[app] Добавьте Environment Variable WEB_ONLY=1 на Render.")
    sys.exit(1)

# Устанавливаем порт (Render передаёт свой через $PORT).
os.environ.setdefault("PORT", "8080")
log.info("[app] PORT=%s", os.environ.get("PORT"))

# Импортируем main.py (загружает все handlers, базы, telethon).
log.info("[app] Импортирую main.py ...")
try:
    import main as _bot_main
except Exception as e:
    log.exception("[app] Не удалось импортировать main: %s", e)
    sys.exit(1)

# Находим функцию start_keep_alive_server (она запускает aiohttp).
if not hasattr(_bot_main, "start_keep_alive_server"):
    log.error("[app] В main.py нет функции start_keep_alive_server().")
    sys.exit(1)


async def _web_only_main():
    """Запускает только веб-сервер бота без polling."""
    log.info("[app] Запускаю start_keep_alive_server() ...")

    # Эта функция поднимает aiohttp на $PORT и раздаёт Mini App + /api/*.
    # Она не запускает Telegram polling — это происходит в application.run_polling(),
    # который мы НЕ вызываем.
    try:
        await _bot_main.start_keep_alive_server()
    except Exception as e:
        log.exception("[app] start_keep_alive_server failed: %s", e)
        sys.exit(1)

    log.info("[app] Веб-сервер запущен на порту %s. Mini App доступен по публичному URL.",
             os.environ.get("PORT"))
    log.info("[app] База восстанавливается из канала %s в фоне ...",
             os.environ.get("STORAGE_CHANNELS"))

    # Держим корутину живой навсегда.
    while True:
        await asyncio.sleep(3600)


# Запускаем event loop.
try:
    asyncio.run(_web_only_main())
except KeyboardInterrupt:
    log.info("[app] KeyboardInterrupt, выходим ...")
except Exception as e:
    log.exception("[app] Фатальная ошибка: %s", e)
    sys.exit(1)
