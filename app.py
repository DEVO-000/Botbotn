import os, sys, asyncio, logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', stream=sys.stdout, force=True)
log = logging.getLogger("app")
IS_RENDER = os.environ.get("RENDER", "") != ""
WEB_ONLY = IS_RENDER or os.environ.get("WEB_ONLY", "").lower() in ("1", "true", "yes", "on")
log.info("[app] IS_RENDER=%s, WEB_ONLY=%s", IS_RENDER, WEB_ONLY)
_bt = os.environ.get("BOT_TOKEN", "")
if _bt:
    log.info("[app] BOT_TOKEN ending: ...%s (length=%d)", _bt[-10:], len(_bt))
else:
    log.error("[app] BOT_TOKEN NE ZADAN!")
log.info("[app] STORAGE_CHANNELS=%s", os.environ.get("STORAGE_CHANNELS", "(NE ZADAN)"))
log.info("[app] DEVELOPER_ID=%s", os.environ.get("DEVELOPER_ID", "(NE ZADAN)"))
log.info("[app] MINIAPP_URL=%s", os.environ.get("MINIAPP_URL", "(NE ZADAN)"))
if not WEB_ONLY:
    log.error("[app] WEB_ONLY=1 required on Render.")
    sys.exit(1)
log.info("[app] Режим: только веб-сервер Mini App (без polling)")
os.environ.setdefault("PORT", "8080")
log.info("[app] PORT=%s", os.environ.get("PORT"))
log.info("[app] Импортирую main.py ...")
try:
    import main as _bot_main
except Exception as e:
    log.exception("[app] import main failed: %s", e)
    sys.exit(1)
_bot_token_in_main = getattr(_bot_main, "BOT_TOKEN", "")
if _bot_token_in_main:
    log.info("[app] main.py BOT_TOKEN ending: ...%s", _bot_token_in_main[-10:])
    if _bot_token_in_main != _bt:
        log.error("[app] BOT_TOKEN env != main.py!")
else:
    log.error("[app] main.py NE vidit BOT_TOKEN!")
if not hasattr(_bot_main, "start_keep_alive_server"):
    log.error("[app] net start_keep_alive_server v main.py")
    sys.exit(1)
async def _web_only_main():
    log.info("[app] Запускаю start_keep_alive_server() ...")
    try:
        await _bot_main.start_keep_alive_server()
    except Exception as e:
        log.exception("[app] start_keep_alive_server failed: %s", e)
        sys.exit(1)
    log.info("[app] ✅ Веб-сервер запущен на порту %s.", os.environ.get("PORT"))
    log.info("[app] Mini App доступен по публичному URL Render.")
    while True:
        await asyncio.sleep(3600)
try:
    asyncio.run(_web_only_main())
except KeyboardInterrupt:
    log.info("[app] KeyboardInterrupt ...")
except Exception as e:
    log.exception("[app] fatal: %s", e)
    sys.exit(1)
