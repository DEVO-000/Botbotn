"""app.py v13 — для Render: восстанавливает базу + запускает веб-сервер.

v12 запускал ТОЛЬКО веб-сервер без восстановления базы → 401 на все API.
v13 monkey-patch'ит run_polling() → вызывает initialize() (восстанавливает
базу из канала + запускает веб-сервер через _post_init), но НЕ запускает
polling (он работает на HF Spaces).
"""
import os, sys, logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', stream=sys.stdout, force=True)
log = logging.getLogger("app")

IS_RENDER = os.environ.get("RENDER", "") != ""
WEB_ONLY = IS_RENDER or os.environ.get("WEB_ONLY", "").lower() in ("1", "true", "yes", "on")

_bt = os.environ.get("BOT_TOKEN", "")
log.info("[app] IS_RENDER=%s, WEB_ONLY=%s", IS_RENDER, WEB_ONLY)
if _bt:
    log.info("[app] BOT_TOKEN ending: ...%s (length=%d)", _bt[-10:], len(_bt))
log.info("[app] STORAGE_CHANNELS=%s", os.environ.get("STORAGE_CHANNELS", "(NE ZADAN)"))
log.info("[app] DEVELOPER_ID=%s", os.environ.get("DEVELOPER_ID", "(NE ZADAN)"))
log.info("[app] MINIAPP_URL=%s", os.environ.get("MINIAPP_URL", "(NE ZADAN)"))

if not WEB_ONLY:
    log.error("[app] WEB_ONLY=1 required on Render.")
    sys.exit(1)

# === КЛЮЧЕВОЙ ФИКС: monkey-patch run_polling ============================
# run_polling() внутри вызывает initialize() (который запускает _post_init —
# восстановление базы + веб-сервер), а потом start() (polling).
# Мы заменяем run_polling так, чтобы initialize() вызывался, а polling — нет.
import telegram.ext
import asyncio

_orig_run_polling = telegram.ext.Application.run_polling

def _web_only_run_polling(self, *args, **kwargs):
    """Заменяет run_polling: вызывает initialize() (база + веб-сервер),
    но НЕ запускает polling."""
    log.info("[app] WEB_ONLY: run_polling() перехвачен — инициализирую без polling")
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    # initialize() вызывает _post_init → восстановление базы + start_keep_alive_server
    loop.run_until_complete(self.initialize())
    log.info("[app] ✅ initialize() завершён — база восстановлена, веб-сервер запущен")
    log.info("[app] Mini App доступен по публичному URL Render")

    # НЕ вызываем self.start() (polling) — он работает на HF Spaces
    # Держим event loop живым
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        log.info("[app] Останавливаюсь...")
        loop.run_until_complete(self.shutdown())

telegram.ext.Application.run_polling = _web_only_run_polling
log.info("[app] run_polling monkey-patched для WEB_ONLY режима")

# === Запускаем main() ===================================================
# main() создаст Application и вызовет run_polling() (наш patched вариант)
log.info("[app] Запускаю main.main() ...")
try:
    import main as _bot_main
    _bot_main.main()
except Exception as e:
    log.exception("[app] Фатальная ошибка: %s", e)
    sys.exit(1)
