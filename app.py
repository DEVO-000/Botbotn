import os, sys, logging, asyncio
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
import telegram.ext
_orig_run_polling = telegram.ext.Application.run_polling
def _web_only_run_polling(self, *args, **kwargs):
    log.info("[app] WEB_ONLY: run_polling() перехвачен")
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    log.info("[app] Вызываю initialize() ...")
    loop.run_until_complete(self.initialize())
    log.info("[app] ✅ initialize() завершён (Bot готов)")
    post_init = getattr(self, "_post_init", None)
    if post_init is None:
        log.error("[app] _post_init не найден в application!")
    else:
        log.info("[app] Вызываю _post_init (база + веб-сервер) ...")
        try:
            loop.run_until_complete(post_init(self))
            log.info("[app] ✅ _post_init завершён — база восстановлена, веб-сервер запущен")
        except Exception as e:
            log.exception("[app] _post_init failed: %s", e)
    log.info("[app] WEB_ONLY: polling НЕ запускается (он на HF Spaces)")
    log.info("[app] ✅ Mini App доступен по публичному URL Render")
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        log.info("[app] Останавливаюсь...")
        try:
            loop.run_until_complete(self.shutdown())
        except Exception:
            pass
telegram.ext.Application.run_polling = _web_only_run_polling
log.info("[app] run_polling monkey-patched для WEB_ONLY режима")
log.info("[app] Запускаю main.main() ...")
try:
    import main as _bot_main
    _bot_main.main()
except Exception as e:
    log.exception("[app] Фатальная ошибка: %s", e)
    sys.exit(1)
