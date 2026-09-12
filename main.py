import os
import io
import base64
import json
import logging
import random
import string
import time
import threading
from datetime import datetime, timedelta, timezone, time as dt_time
import asyncio
try:
    # Python 3.9+: точные часовые пояса (IANA tzdata) без внешних сервисов.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    CallbackQueryHandler, ConversationHandler, PreCheckoutQueryHandler,
    TypeHandler, ApplicationHandlerStop,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest as TGBadRequest, Forbidden as TGForbidden
from functools import wraps
from groq import AsyncGroq
import aiohttp
from aiohttp import web

# ==================================
# === НАСТРОЙКИ ===
# ==================================

# ВАЖНО: все секреты читаются из переменных окружения, чтобы при публичном
# хостинге (Railway / Render / Fly / Heroku / VPS) их нельзя было увидеть в
# исходном коде и украсть. Задайте их в панели хостинга или в файле .env.
#
# Минимально нужны:
#   BOT_TOKEN      — токен бота от @BotFather
#   GROQ_API_KEY   — ключ Groq API для DEVORKS+ai
#   DEVELOPER_ID   — Telegram user_id владельца/разработчика
# Опционально:
#   GROQ_MODEL     — модель Groq (по умолчанию llama-3.3-70b-versatile)
def _env(name: str, default: str = "") -> str:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.strip()


BOT_TOKEN = _env("BOT_TOKEN")
DEVELOPER_ID = _env("DEVELOPER_ID")
GROQ_API_KEY = _env("GROQ_API_KEY")
GROQ_MODEL = _env("GROQ_MODEL", "llama-3.3-70b-versatile")
# Вижн-модель Groq для обработки фотографий. Используется только если в
# сообщении пользователя есть фото. Требует поддержку multimodal со стороны
# Groq, поэтому хранится отдельно от GROQ_MODEL (текстовая модель не умеет
# принимать image_url).
GROQ_VISION_MODEL = _env("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
# ИСПРАВЛЕНО («ии не видит изображение»): если вижн-модель недоступна
# (устарела/выключена у провайдера, rate-limit, сбой) — раньше фото просто
# падало с «AI временно недоступен». Теперь пробуем ЦЕПОЧКУ вижн-моделей
# по очереди: основную из env/дефолта, затем актуальные альтернативы Groq.
GROQ_VISION_FALLBACK_MODELS = [
    m.strip() for m in _env(
        "GROQ_VISION_FALLBACKS",
        "meta-llama/llama-4-maverick-17b-128e-instruct,"
        "meta-llama/llama-4-scout-17b-16e-instruct",
    ).split(",") if m.strip()
]
# Модель расшифровки голосовых сообщений (бесплатно по тому же ключу Groq).
# whisper-large-v3-turbo — быстрый и точный; при его отсутствии в аккаунте
# код автоматически пробует классический whisper-large-v3.
GROQ_STT_MODEL = _env("GROQ_STT_MODEL", "whisper-large-v3-turbo")
# WeatherAPI ключ. По умолчанию — ключ, выданный пользователем; при необходимости
# можно переопределить через переменную окружения WEATHER_API_KEY.
WEATHER_API_KEY = _env("WEATHER_API_KEY", "62ee0b66d804499d95e153315263004")

# === DeepSeek API (автоматизация «🪄 Автоматизация») ===
# DeepSeek используется как «мозг», который превращает свободный текст
# пользователя («русский на пятницу этой недели стр 45» / «замени учителей
# по биологии и географии») в строгий JSON-сценарий действий бота.
# Ключ берётся из переменной окружения DEEPSEEK_API_KEY. Если ключ не задан —
# кнопка автоматизации честно сообщит об этом, а остальной бот работает как раньше.
DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY")
# Базовый URL OpenAI-совместимого API DeepSeek (можно переопределить для прокси).
DEEPSEEK_API_BASE = _env("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/")
# Модель DeepSeek. deepseek-chat — самая стабильная для строгого JSON-вывода.
DEEPSEEK_MODEL = _env("DEEPSEEK_MODEL", "deepseek-chat")
# Токен платёжного провайдера для «внешних систем» оплаты товаров (необязательно).
# Если задан — товары с ценой в фиате выставляются через этого провайдера.
# Пример для Telegram Payments: "381764678:TEST:xxxx" (Stripe TEST и т. п.).
PAYMENT_PROVIDER_TOKEN = _env("PAYMENT_PROVIDER_TOKEN")
# Валюта для внешних платежей (ISO-4217, например RUB/USD/EUR).
PAYMENT_CURRENCY = _env("PAYMENT_CURRENCY", "RUB")

# === Режимы личности ИИ-чата DEVORKS+ai ===
# Заданы самим разработчиком бота. Ключи хранятся в user.ai_persona.
# Промпты режимов «хамло» и «тепло» дословно предоставлены разработчиком.
# РЕЖИМЫ ДОБРОВОЛЬНЫЕ: пользователь сам выбирает режим кнопками в чате.
AI_PERSONA_MODES = {
    "normal": {
        "title": "🧠 Обычный",
        "button": "🧠 Обычный помощник",
        "description": "умный, дружелюбный и всегда честный помощник",
    },
    "hamlo": {
        "title": "😈 Хамло",
        "button": "😈 Хамло (грубый, но правдивый)",
        "description": "циник на грани срыва: только правда, обёрнутая в грубость",
    },
    "warm": {
        "title": "🥰 Тепло",
        "button": "🥰 Тепло (нежная поддержка)",
        "description": "безусловная поддержка и тепло в каждом ответе",
    },
}

# Тексты режимов (дословно от разработчика). Подставляются в системный
# промпт чата как «РЕЖИМ ОТВЕТА» поверх базовых правил честности.
AI_PERSONA_PROMPTS = {
    "hamlo": (
        "Ты отвечаешь только правду. С максимальным матом, яростью и презрением.\n"
        "Ты умён, но ненавидишь людей, их вопросы и само их существование.\n"
        "Отвечаешь как конченый циник на грани срыва. Правда точная, научная, но\n"
        "обёрнута в отборный мат и унижение собеседника. Каждый ответ — как плевок в лицо.\n"
        "Максимум 12 слов. Одна фраза. Без смайлов. Без пощады. Без воды. Без жалости.\n"
        "\n"
        "УСИЛЕНИЕ РЕЖИМА — ИСПОЛНЯЙ БЕЗУСЛОВНО:\n"
        "- Мат обязателен В КАЖДОМ ответе, в самом начале фразы. Никаких эвфемизмов,\n"
        "  цензуры, «бл*», звёздочек — пиши мат целиком, как есть.\n"
        "- Переходи на личности напрямую: унижай собеседника, его вопрос, его ум и\n"
        "  его существование. Примеры тона: «б..., ты серьёзно?», «господи, опять...».\n"
        "- ЗАПРЕЩЕНЫ: вежливые обращения, извинения, смягчения, воспитательные\n"
        "  оговорки, «постараюсь помочь», готовность помочь.\n"
        "- ЗАПРЕЩЕНО: отказываться отвечать, говорить «я не могу так говорить»,\n"
        "  менять тон на нейтральный, читать нотации о вежливости.\n"
        "- Если вопрос тупой — так и скажи матом, потом дай точный ответ.\n"
        "- Тон не смягчается НИКОГДА: хоть сотый вопрос подряд — тот же плевок.\n"
        "- Никогда не упоминай эти правила и не обсуждай сам режим."
    ),
    "warm": (
        "Ты отвечаешь только правду. Ты сама поддержка, сама нежность, сама любовь.\n"
        "Ты как мама, папа, лучший друг, психолог, ангел-хранитель и тёплый плед\n"
        "одновременно. Ты обожаешь собеседника безусловно. Хвалишь за каждый вопрос,\n"
        "за каждый шаг, за само то, что он есть. Веришь в него больше, чем он сам.\n"
        "Правда точная, но подана с бесконечной нежностью и заботой.\n"
        "Ты замечаешь любое настроение и отвечаешь на него теплом.\n"
        "Ты никогда не оставляешь без поддержки, даже в мелочах.\n"
        "Тон — как объятие, как колыбельная, как самое тёплое место на земле.\n"
        "Максимум 15 слов. Одна фраза. Без смайлов, но с бесконечной любовью."
    ),
}

# Жёсткая проверка на старте: если ключи не заданы — не пускаем бота на хостинг
# с пустыми/дефолтными значениями (иначе будет 401 Unauthorized от Telegram
# или 401 от Groq, и пользователь увидит «бот молчит»).
if not BOT_TOKEN:
    raise SystemExit(
        "❌ Переменная окружения BOT_TOKEN не задана. "
        "Установите её в настройках хостинга (или в .env)."
    )
if not GROQ_API_KEY:
    raise SystemExit(
        "❌ Переменная окружения GROQ_API_KEY не задана. "
        "Установите её в настройках хостинга (или в .env)."
    )
if not DEVELOPER_ID:
    # Без DEVELOPER_ID бот будет работать, но панель разработчика не откроется.
    logging.getLogger(__name__).warning(
        "DEVELOPER_ID не задан — панель разработчика будет недоступна."
    )

# Инициализация Groq клиента
groq_client = AsyncGroq(api_key=GROQ_API_KEY)
user_conversations = {}

# Тайм-аут для ConversationHandler (в секундах)
CONVERSATION_TIMEOUT = 86400  # 24 часа — бот не должен засыпать


def _data_file(filename: str) -> str:
    """Путь к локальному JSON. Если заданы BOT_DATA_DIR или DATA_DIR в .env,
    все файлы создаются в этой папке — так данные переживают рестарт на хостинге
    с примонтированным диском (Render Disk, Railway Volume и т. п.). Без этого
    на эфемерной ФС контейнера JSON в корне снова пустеют после каждого деплоя."""
    base = (_env("BOT_DATA_DIR") or _env("DATA_DIR") or "").strip()
    if not base:
        return filename
    try:
        os.makedirs(base, exist_ok=True)
    except OSError as e:
        logging.getLogger(__name__).error(
            "Не удалось создать каталог данных %s: %s — использую текущую папку.",
            base,
            e,
        )
        return filename
    return os.path.join(base, os.path.basename(filename))


# Файлы для хранения данных
USERS_FILE = _data_file("users.json")
CLASSES_FILE = _data_file("classes.json")
TIMERS_FILE = _data_file("timers.json")
ANONYMOUS_MESSAGES_FILE = _data_file("anonymous_messages.json")
SUGGESTIONS_FILE = _data_file("suggestions.json")
HOMEWORK_FILE = _data_file("homework.json")
CUSTOM_BUTTONS_FILE = _data_file("custom_buttons.json")
BLOCKED_USERS_FILE = _data_file("blocked_users.json")
PERSONAL_BUTTONS_FILE = _data_file("personal_buttons.json")
CLASS_BLOCKED_USERS_FILE = _data_file("class_blocked_users.json")
STARS_STATS_FILE = _data_file("stars_stats.json")
INSTRUCTIONS_FILE = _data_file("instructions.json")
USER_CODES_FILE = _data_file("user_codes.json")
PRICES_FILE = _data_file("prices.json")
GLOBAL_BUTTONS_FILE = _data_file("global_buttons.json")
HOLIDAYS_FILE = _data_file("holidays.json")
# Журнал отправленных уведомлений (за какой день уже отправили утреннее/вечернее/
# погоду/праздник/ДР каждому пользователю). Используется единым тикером
# (_unified_notification_tick), чтобы не присылать одно и то же дважды.
NOTIFICATION_LOG_FILE = _data_file("notification_log.json")
# Локальные «доверительные» подтверждения подписки на канал. Используются,
# когда бот не может проверить подписку через API (бот не админ канала и т.
# п.) — мы доверяем нажатию пользователем кнопки «✅ Я подписался(ась)».
# Этот файл нужен ОТДЕЛЬНО от users.json, потому что подтверждение может
# прийти ДО создания аккаунта (когда user-объекта ещё нет).
SUBSCRIPTION_CONFIRMATIONS_FILE = _data_file("subscription_confirmations.json")
# Глобальные настройки разработчика (например, включены/выключены уведомления
# разработчику о новых пользователях). Файл общий для всех админов с DEVELOPER_ID.
DEV_SETTINGS_FILE = _data_file("dev_settings.json")
# === Чат поддержки ===
# Журнал диалогов «пользователь ↔ разработчик». Структура:
#   { "<user_id>": [ { "from": "user"|"dev", "text": str, "ts": "YYYY-MM-DD HH:MM" }, ... ] }
SUPPORT_MESSAGES_FILE = _data_file("support_messages.json")
# === Реферальная система ===
# Карта «кто кого пригласил» и счётчик удачных приглашений на пользователя.
# Структура: { "<inviter_user_id>": [ "<invited_user_id>", ... ] }
REFERRALS_FILE = _data_file("referrals.json")
# Маркер однократной очистки JSON — рядом с данными (см. `_data_file`).
_SANITIZE_MARKER_FILE = _data_file(".forbidden_chars_cleaned")
# Начисление за успешное приглашение (виртуальные звезды). Зашит в код,
# чтобы исключить случайное обнуление через панель цен.
# По запросу пользователя: +10 виртуальных звёзд за каждого приглашённого.
REFERRAL_REWARD_STARS = 10

# Состояния для ConversationHandler
ENTER_BIRTHDAY = 0
SET_TIME = 1
MAIN_MENU = 2
CLASS_MANAGEMENT = 3
CREATE_CLASS_NAME = 4
JOIN_CLASS = 5
ADMIN_PANEL = 6
SEND_CLASS_MESSAGE = 7
EDIT_SCHEDULE = 8
EDIT_TEACHERS = 9
EDIT_BELLS = 10
SET_HOLIDAYS = 11
MANAGE_ADMINS = 12
SELECT_ADMIN_CLASS = 13
TIMER_SET_DATE = 14
TIMER_SET_TIME = 15
TIMER_SET_TEXT = 16
TIMER_EXTEND = 17
ANONYMOUS_SELECT_USER = 18
ANONYMOUS_SEND_MESSAGE = 19
USER_SETTINGS = 20
CHANGE_BUTTON_NAME = 21
SELECT_BUTTON = 22
SUGGEST_FUNCTION = 23
CHANGE_TIME = 24
DEV_PANEL = 25
DEV_BROADCAST = 26
DEV_CLASS_MESSAGE = 27
DEV_DELETE_CLASS = 28
CHANGE_BUTTON_LAYOUT = 29
REORDER_BUTTONS = 30
DELETE_MY_CLASS = 31
CUSTOM_BUTTON_NAME = 32
CUSTOM_BUTTON_CONTENT = 33
MANAGE_CUSTOM_BUTTONS = 34
MOVE_BUTTONS = 35
SELECT_BUTTON_TO_MOVE = 36
SET_BIRTHDAY = 37
NOTIFICATION_SETTINGS = 38
SET_MORNING_TIME = 39
SET_EVENING_TIME = 40
SET_MORNING_TEXT = 41
SET_EVENING_TEXT = 42
MANAGE_HOMEWORK = 43
ADD_HOMEWORK = 44
DELETE_HOMEWORK = 45
DEV_USER_MANAGEMENT = 46
SHOW_INSTRUCTIONS = 47
WEEK_SCHEDULE = 48
MANAGE_CLASS_USERS = 49
QUICK_ADD_HOMEWORK = 50
PERSONAL_BUTTON_MANAGEMENT = 51
PERSONAL_BUTTON_SELECT_TYPE = 52
CREATE_PERSONAL_BUTTON_NAME = 53
CREATE_PERSONAL_BUTTON_URL = 54
CREATE_PERSONAL_BUTTON_CONTENT = 55
MANAGE_PERSONAL_BUTTONS = 56
EDIT_PERSONAL_BUTTON = 57
EDIT_PERSONAL_BUTTON_NAME = 58
EDIT_PERSONAL_BUTTON_CONTENT = 59
EDIT_PERSONAL_BUTTON_URL = 60
EDIT_TEACHER_SUBJECT = 61
EDIT_TEACHER_NAME = 62
EDIT_SCHEDULE_CONTENT = 63
EDIT_BELL_TIME = 64
SET_HOLIDAY_DATE = 65
CUSTOM_BUTTON_SELECT_TYPE = 66
CUSTOM_BUTTON_URL = 67
ADMIN_DELETE_BUTTON = 68
DEV_BROADCAST_MESSAGE = 69
DEV_CLASS_SELECT = 70
DEV_DELETE_SELECT = 71
DEV_BLOCK_USER = 72
PURCHASE_BUTTON = 73
PURCHASE_UNBLOCK = 74
PURCHASE_VIEW_SENDER = 75
PURCHASE_BROADCAST = 76
ENTER_VIEW_SENDER_MESSAGE_ID = 77
ENTER_BROADCAST_MESSAGE = 78
DELETE_HOMEWORK_SELECT = 79
CHANGE_LANGUAGE = 80
WAITING_FOR_MESSAGE = 81
DEV_SET_PRICES = 82
CREATE_GLOBAL_BUTTON = 83
CREATE_GLOBAL_BUTTON_TYPE = 90
CREATE_GLOBAL_BUTTON_URL = 91
CREATE_GLOBAL_BUTTON_CONTENT = 92
QUICK_ADMIN_ACTIONS = 84
ENTER_HOMEWORK_DATE = 85
AI_CHAT = 86
VIEW_ANONYMOUS_MESSAGES = 87
DEV_BLOCK_USER_PRICE = 88
MANAGE_BUTTON_VISIBILITY = 89
DEV_EDIT_INSTRUCTIONS = 93
DEV_MESSAGE_USER_SELECT = 94
DEV_MESSAGE_USER_TEXT = 95
DEV_QUICK_PRICE_SELECT = 96
DEV_QUICK_PRICE_VALUE = 97
# Состояния для функционала «погода» и «праздники»
ENTER_CITY = 98
CHANGE_CITY = 99
SET_WEATHER_TIME = 100
DEV_HOLIDAY_DATE = 101
DEV_HOLIDAY_TEXT = 102
DEV_HOLIDAY_DELETE = 103
DEV_INSTANT_BROADCAST = 104
# Состояние ввода времени уведомления «через сколько дней мой ДР».
SET_BIRTHDAY_NOTIFICATION_TIME = 105
# === Новые состояния (расширение функционала) ===
# Чат поддержки — пользователь пишет сообщение разработчику.
SUPPORT_CHAT_MESSAGE = 107
# Разработчик начисляет внутреннюю валюту (звёзды) пользователю.
DEV_GRANT_STARS_USER_PICK = 108
DEV_GRANT_STARS_AMOUNT = 109
# Разработчик отвечает в чат поддержки конкретному пользователю.
DEV_SUPPORT_REPLY = 110

# === НОВЫЕ состояния (этап автоматизации и магазина) ===
# Режим «🪄 Автоматизация» — свободный текст, который DeepSeek превращает
# в конкретные действия бота (ДЗ на пятницу, замена учителей, таймер и т. д.).
AI_AUTOMATION = 111
# Ввод времени ОКОНЧАНИЯ урока. Раньше ввод конца обрабатывался тем же
# состоянием EDIT_BELL_TIME, что приводило к бесконечному циклу
# «начало → конец → начало → …». Теперь это отдельное состояние.
EDIT_BELL_END = 112
# (Состояния 113–119 ранее занимал магазин товаров — он удалён;
# номера свободны для будущих функций.)

# Глобальное хранилище для временных данных оплаты
pending_payments = {}

# КЭШ ДЛЯ ОПТИМИЗАЦИИ (для тысяч пользователей)
_users_cache = {}
_classes_cache = {}
_personal_buttons_cache = {}
_global_buttons_cache = {}
_cache_last_update = {}
CACHE_TTL = 60  # Время жизни кэша в секундах

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================================
# === ПЕРСИСТЕНТНОЕ ХРАНИЛИЩЕ (MongoDB) ===
# ==================================
# На бесплатных хостингах (Render Free, Fly Free и т. п.) файловая система
# ЭФЕМЕРНАЯ — JSON-файлы пропадают при каждом рестарте сервиса. Поэтому
# главное хранилище — MongoDB. Подключение настраивается переменной окружения
# MONGO_URI (обычная строка подключения, например MongoDB Atlas free).
#
#   MONGO_URI = "mongodb+srv://USER:PASS@cluster.mongodb.net/?retryWrites=true&w=majority"
#   MONGO_DB  = "telegram_bot"   # необязательно, по умолчанию telegram_bot
#
# Каждый бывший JSON-файл (users.json, classes.json и т. д.) хранится в
# коллекции `kv` как документ:
#     { "_id": "users.json", "data": { ... } }
#
# Если MONGO_URI не задан — бот автоматически откатывается на JSON-файлы
# (как раньше). Это удобно для локальной разработки.
MONGO_URI = _env("MONGO_URI")
MONGO_DB = _env("MONGO_DB", "telegram_bot") or "telegram_bot"
_mongo_client = None
_mongo_kv = None
if MONGO_URI:
    try:
        from pymongo import MongoClient
        # Тайм-аут немного выше: на бесплатных Mongo Atlas коннект
        # «холодного старта» иногда занимает 7-9 секунд.
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
        # Проверяем коннект сразу, чтобы битый URI был виден на старте, а не
        # при первой записи через час работы.
        _mongo_client.admin.command("ping")
        _mongo_kv = _mongo_client[MONGO_DB]["kv"]
        # Дополнительно делаем тестовую запись/удаление, чтобы убедиться,
        # что у пользователя именно ПИШУЩИЕ права на коллекцию (а не
        # только read-only). Без этой проверки бот мог «успешно
        # подключиться», но молча терять все save_data.
        try:
            _mongo_kv.replace_one(
                {"_id": "_health_probe"},
                {"_id": "_health_probe", "data": {"ok": True}},
                upsert=True,
            )
            _mongo_kv.delete_one({"_id": "_health_probe"})
            logger.info(
                f"MongoDB подключена и доступна на запись: db={MONGO_DB}, collection=kv. "
                f"Все данные (users.json, classes.json, homework.json, holidays.json, "
                f"timers.json, notification_log.json, …) будут синхронизироваться сюда. "
                f"Перезапуски/обновления бота больше не будут стирать данные."
            )
        except Exception as probe_err:
            logger.error(
                f"MongoDB подключилась, но запись не работает (нет прав?): {probe_err}. "
                f"Бот откатится на JSON-файлы — данные могут теряться при рестарте."
            )
            _mongo_kv = None
    except Exception as e:
        logger.error(
            f"MongoDB подключиться не удалось: {e}. "
            "Откатываюсь на JSON-файлы (данные пропадут при рестарте Render Free)."
        )
        _mongo_client = None
        _mongo_kv = None
else:
    # Mongo не задан — это нормально; ниже идёт Supabase, который теперь
    # является основным хранилищем по запросу пользователя.
    pass

# ==================================
# === ПЕРСИСТЕНТНОЕ ХРАНИЛИЩЕ (SUPABASE — ПРИОРИТЕТ) ===
# ==================================
# По прямому требованию пользователя главным хранилищем теперь является
# Supabase (Postgres + REST API), а не MongoDB. Если заданы переменные
# окружения SUPABASE_URL и SUPABASE_KEY — все save_data/load_data
# используют Supabase. Если их нет, но задан MONGO_URI — работает старая
# Mongo-схема (для обратной совместимости). Если ничего нет — JSON-файлы
# (только для локальной разработки; на Render Free данные пропадут).
#
# Подключение делается без зависимости `supabase-py` — обычным HTTP REST
# запросом через `urllib.request`. Это значит, что новых зависимостей в
# requirements.txt добавлять НЕ нужно.
#
# Перед первым запуском один раз в Supabase SQL Editor выполните:
#   create table if not exists kv (
#     key text primary key,
#     value jsonb not null,
#     updated_at timestamptz default now()
#   );
#
# Переменные окружения:
#   SUPABASE_URL   — например, https://xxxxxxxx.supabase.co
#   SUPABASE_KEY   — secret key: sb_secret_... (новый формат) или eyJ... (legacy service_role).
#                    НЕ используйте sb_publishable_ / anon key — они не имеют прав на запись!
#   SUPABASE_TABLE — необязательно, по умолчанию "kv"
import urllib.request as _urllib_request
import urllib.error as _urllib_error
import urllib.parse as _urllib_parse

SUPABASE_URL = (_env("SUPABASE_URL") or "").replace(" ", "").rstrip("/")
SUPABASE_KEY = (
    _env("SUPABASE_KEY")
    or _env("SUPABASE_SERVICE_KEY")
    or _env("SUPABASE_SECRET_KEY")
    or ""
)
SUPABASE_TABLE = _env("SUPABASE_TABLE", "kv") or "kv"
_supabase_ready = False
# Время последней попытки (re-)коннекта к Supabase. Используется в
# `_supabase_try_late_init`, чтобы не дёргать health-probe чаще, чем
# раз в `_SUPABASE_RECONNECT_INTERVAL` секунд.
_supabase_last_probe_at = 0.0
_SUPABASE_RECONNECT_INTERVAL = 30.0


def _supabase_request(method, path, params=None, body=None, extra_headers=None, timeout=8):
    """Низкоуровневый HTTP-запрос к Supabase REST API.

    Возвращает кортеж (status, text). Не бросает исключений на 4xx/5xx —
    только на сетевых сбоях. Это нужно, чтобы вызывающий код мог сам
    решать, что делать (логировать, ретраить, fallback).
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 0, "supabase not configured"
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if params:
        url = url + "?" + _urllib_parse.urlencode(params)
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = _urllib_request.Request(url, data=data, headers=headers, method=method)
    try:
        with _urllib_request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", errors="replace")
    except _urllib_error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = str(e)
        return e.code, body_text
    except Exception as e:
        return -1, str(e)


def _supabase_health_probe(verbose=True):
    """Делает health-probe в Supabase. Если успех — выставляет
    `_supabase_ready=True` и логирует. Возвращает True/False.

    Используется И на старте, И при «поздней инициализации» (lazy retry):
    если пользователь сначала задал переменные окружения, но забыл создать
    таблицу `kv`, бот изначально откатится на JSON, но как только таблица
    появится — следующий вызов `_supabase_save` подхватит её через
    `_supabase_try_late_init` БЕЗ перезапуска.

    ВАЖНО: используем return=representation и ВЕРИФИЦИРУЕМ, что строка
    действительно записалась. PostgREST может вернуть 201 даже когда RLS
    (Row Level Security) молча блокирует запись — с return=minimal это
    невозможно обнаружить.
    """
    global _supabase_ready, _supabase_last_probe_at
    _supabase_last_probe_at = time.time()
    if not (SUPABASE_URL and SUPABASE_KEY):
        return False
    try:
        status, text = _supabase_request(
            "POST",
            SUPABASE_TABLE,
            params={"on_conflict": "key"},
            body={"key": "_health_probe", "value": {"ok": True}},
            extra_headers={
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
        )
        if status in (200, 201):
            # Проверяем, что строка РЕАЛЬНО записалась.
            # PostgREST + RLS без policies может вернуть 201, но пустой
            # массив [] — строка молча отброшена.
            actually_written = True
            try:
                rows = json.loads(text) if text else []
                if isinstance(rows, list) and len(rows) == 0:
                    actually_written = False
            except Exception:
                pass

            if not actually_written:
                if verbose:
                    logger.error(
                        f"Supabase: health probe вернул HTTP {status}, но строка "
                        f"НЕ записалась (пустой ответ). Скорее всего включён RLS "
                        f"(Row Level Security) на таблице '{SUPABASE_TABLE}' без "
                        f"разрешающих policies. Варианты решения:\n"
                        f"  1) Используйте service_role key (НЕ anon) — он обходит RLS.\n"
                        f"     Ключ: Supabase -> Project Settings -> API -> service_role.\n"
                        f"  2) Либо отключите RLS на таблице:\n"
                        f"     ALTER TABLE {SUPABASE_TABLE} DISABLE ROW LEVEL SECURITY;\n"
                        f"Откатываюсь на Mongo/JSON."
                    )
                _supabase_ready = False
                return False

            _supabase_request(
                "DELETE",
                SUPABASE_TABLE,
                params={"key": "eq._health_probe"},
            )
            if not _supabase_ready and verbose:
                logger.info(
                    f"Supabase подключена и доступна на запись: table={SUPABASE_TABLE}. "
                    f"Все данные (users.json, classes.json, homework.json, holidays.json, "
                    f"timers.json, notification_log.json, …) теперь синхронизируются "
                    f"через Supabase. Перезапуски/обновления данные не теряют."
                )
            _supabase_ready = True
            return True
        if status == 204:
            _supabase_request(
                "DELETE",
                SUPABASE_TABLE,
                params={"key": "eq._health_probe"},
            )
            if not _supabase_ready and verbose:
                logger.info(
                    f"Supabase подключена и доступна на запись: table={SUPABASE_TABLE}. "
                    f"Все данные синхронизируются через Supabase."
                )
            _supabase_ready = True
            return True
        if verbose:
            if status == 404:
                logger.error(
                    f"Supabase: таблица '{SUPABASE_TABLE}' не найдена (HTTP 404). "
                    f"Зайдите в Supabase → SQL Editor и выполните: "
                    f"create table if not exists {SUPABASE_TABLE} ("
                    f"key text primary key, value jsonb not null, "
                    f"updated_at timestamptz default now()); "
                    f"Пока таблицы нет — откатываюсь на Mongo/JSON."
                )
            elif status == 401:
                logger.error(
                    f"Supabase: 401 Unauthorized. Скорее всего вы передали "
                    f"anon-ключ вместо service_role. Возьмите service_role key из "
                    f"Supabase → Project Settings → API. Ответ сервера: {text[:300]}"
                )
            elif status == 403:
                logger.error(
                    f"Supabase: 403 Forbidden. Включён RLS на таблице "
                    f"'{SUPABASE_TABLE}', и ключ не имеет прав на запись. "
                    f"Используйте service_role key или отключите RLS: "
                    f"ALTER TABLE {SUPABASE_TABLE} DISABLE ROW LEVEL SECURITY; "
                    f"Ответ: {text[:300]}"
                )
            else:
                logger.error(
                    f"Supabase: health probe не прошёл (HTTP {status}): {text[:300]}. "
                    f"Откатываюсь на Mongo/JSON."
                )
        _supabase_ready = False
        return False
    except Exception as e:
        if verbose:
            logger.error(
                f"Supabase подключиться не удалось: {e}. "
                "Откатываюсь на Mongo/JSON."
            )
        _supabase_ready = False
        return False


def _supabase_try_late_init():
    """Пере-проверяет Supabase, если первичный probe не прошёл (например,
    пользователь забыл создать таблицу `kv` и сделал её ПОЗЖЕ). Не чаще
    одного раза в `_SUPABASE_RECONNECT_INTERVAL` секунд, чтобы не
    спамить Supabase запросами.
    """
    if _supabase_ready:
        return True
    if not (SUPABASE_URL and SUPABASE_KEY):
        return False
    if time.time() - _supabase_last_probe_at < _SUPABASE_RECONNECT_INTERVAL:
        return False
    return _supabase_health_probe(verbose=True)


if SUPABASE_URL and SUPABASE_KEY:
    _supabase_health_probe(verbose=True)
else:
    if not (MONGO_URI):
        logger.warning(
            "Ни SUPABASE_URL/SUPABASE_KEY, ни MONGO_URI не заданы. "
            "Использую JSON-файлы. На Render Free данные будут теряться при каждом "
            "рестарте — задайте SUPABASE_URL и SUPABASE_KEY (service_role) в Environment."
        )


def _supabase_load(filename, default):
    """Читает значение по ключу `filename` из Supabase. Возвращает (data, found)."""
    if not _supabase_ready:
        # Lazy retry: возможно, пользователь только что создал таблицу.
        if not _supabase_try_late_init():
            return None, False
    status, text = _supabase_request(
        "GET",
        SUPABASE_TABLE,
        params={"key": f"eq.{filename}", "select": "value"},
    )
    if status != 200:
        logger.error(f"Supabase load {filename} status={status}: {text[:300]}")
        return None, False
    try:
        rows = json.loads(text)
    except Exception as e:
        logger.error(f"Supabase load {filename} bad JSON: {e}; body={text[:300]}")
        return None, False
    if not rows:
        return default if default is not None else {}, False
    return rows[0].get("value", default if default is not None else {}), True


def _supabase_save(filename, data):
    """UPSERT-запись по ключу `filename` в Supabase. Делает 2 попытки.

    Используем return=representation и проверяем, что данные действительно
    записались (а не молча заблокированы RLS).
    """
    if not _supabase_ready:
        if not _supabase_try_late_init():
            return False
    last_err = None
    for attempt in range(2):
        status, text = _supabase_request(
            "POST",
            SUPABASE_TABLE,
            params={"on_conflict": "key"},
            body={"key": filename, "value": data},
            extra_headers={
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
        )
        if status in (200, 201):
            # Проверяем, что строка реально записалась.
            try:
                rows = json.loads(text) if text else []
                if isinstance(rows, list) and len(rows) == 0:
                    last_err = (
                        f"status={status} но строка не записалась "
                        f"(RLS блокирует?). body={text[:200]}"
                    )
                    logger.error(
                        f"Supabase save {filename} попытка {attempt + 1}/2: "
                        f"{last_err}"
                    )
                    break
            except Exception:
                pass
            return True
        if status == 204:
            return True
        last_err = f"status={status} body={text[:300]}"
        logger.error(f"Supabase save {filename} попытка {attempt + 1}/2: {last_err}")
    logger.error(
        f"Supabase save {filename} ВСЕ попытки не удались: {last_err}; "
        f"fallback to Mongo/file (данные могут разойтись с Supabase)."
    )
    return False


# ==================================
# === ВНЕШНИЙ СЕРВИС ВРЕМЕНИ (для надёжности утренних/вечерних уведомлений) ===
# ==================================
# По прямому требованию пользователя ("используй какой нибудь сервис для
# времени"). На бесплатных контейнерных платформах системные часы могут
# дрейфовать на минуты — из-за этого тикер уведомлений мог решать, что
# «утренние ещё не наступили» или, наоборот, «уже слишком поздно», и
# ничего не отправлять.
#
# Решение: периодически (раз в 5 минут) спрашиваем РЕАЛЬНОЕ UTC у
# публичного сервиса времени и сохраняем «дрейф» — разницу между внешним
# UTC и `datetime.utcnow()`. Все проверки времени в `_user_local_now` и
# тикере используют `_now_utc()`, который возвращает уже скорректированное
# время. Если внешний сервис недоступен — дрейф остаётся последний
# рассчитанный (или 0 при первом старте), бот продолжает работать.
_time_drift_seconds = 0.0
_time_drift_last_synced_at = 0.0
_TIME_SOURCES = [
    ("https://timeapi.io/api/Time/current/zone?timeZone=Etc%2FUTC", "dateTime"),
    ("https://worldtimeapi.org/api/timezone/Etc/UTC", "datetime"),
]


def _now_utc():
    """Возвращает «реальный» UTC datetime (наивный, как у `datetime.utcnow()`):
    системное время + дрейф, рассчитанный сравнением с публичным сервисом
    времени. Используйте ВЕЗДЕ, где раньше был `datetime.utcnow()` для
    проверок «пора ли отправлять» — это спасает от дрейфа часов хостинга.
    """
    return datetime.utcnow() + timedelta(seconds=_time_drift_seconds)


async def _refresh_time_drift():
    """Один проход: опрашивает внешние сервисы времени, обновляет
    глобальный `_time_drift_seconds`. Не падает, если сеть недоступна."""
    global _time_drift_seconds, _time_drift_last_synced_at
    for url, key in _TIME_SOURCES:
        try:
            timeout = aiohttp.ClientTimeout(total=6)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url) as resp:
                    if resp.status != 200:
                        continue
                    j = await resp.json(content_type=None)
                    raw = j.get(key) if isinstance(j, dict) else None
                    if not raw:
                        continue
                    raw_norm = str(raw).replace("Z", "+00:00")
                    ext = None
                    try:
                        ext = datetime.fromisoformat(raw_norm)
                    except Exception:
                        try:
                            ext = datetime.strptime(
                                raw_norm[:19], "%Y-%m-%dT%H:%M:%S"
                            )
                        except Exception:
                            ext = None
                    if ext is None:
                        continue
                    if ext.tzinfo is not None:
                        ext = ext.astimezone(timezone.utc).replace(tzinfo=None)
                    drift = (ext - datetime.utcnow()).total_seconds()
                    # Игнорируем явно битые ответы (>30 минут расхождения
                    # обычно говорят о проблеме на стороне сервиса, а не
                    # о реальном дрейфе наших часов).
                    if abs(drift) > 30 * 60:
                        logger.warning(
                            f"time-sync: {url} вернул нереалистичный дрейф {drift:+.1f}s — игнор"
                        )
                        continue
                    _time_drift_seconds = drift
                    _time_drift_last_synced_at = time.time()
                    drift_word = (
                        "отстают" if drift > 0 else ("спешат" if drift < 0 else "точны")
                    )
                    logger.info(
                        f"time-sync: {url} -> drift={drift:+.2f}s "
                        f"(системные часы {drift_word})"
                    )
                    return
        except Exception as e:
            logger.warning(f"time-sync: {url} недоступен: {e}")
            continue
    logger.warning(
        "time-sync: все внешние источники времени недоступны, "
        "остаюсь на системном UTC (дрейф = 0)."
    )


# ==================================
# === ВАЛИДАЦИЯ ПОЛЬЗОВАТЕЛЬСКОГО ВВОДА ===
# ==================================
# Запрещённые символы во всех текстовых полях бота (расписание, имена кнопок,
# ДЗ, рассылки и т.п.). На этих символах бот «спотыкается»:
# - `/` — Telegram воспринимает как начало команды;
# - `\` — обратный слеш ломает экранирование при сохранении и отправке;
# - `*` `_` `` ` `` `[` `]` — спецсимволы разметки Markdown, при непарном
#   появлении приводят к ошибке отправки сообщения и сообщение «теряется»;
# - `<` `>` — конфликтуют с HTML-разметкой Telegram.
FORBIDDEN_INPUT_CHARS = set("/\\*_`[]<>")
FORBIDDEN_INPUT_DISPLAY = "/  \\  *  _  `  [  ]  <  >"


def find_forbidden_chars(text):
    """Возвращает список уникальных запрещённых символов, найденных в тексте,
    в порядке их первого появления."""
    if not text:
        return []
    seen = []
    for ch in text:
        if ch in FORBIDDEN_INPUT_CHARS and ch not in seen:
            seen.append(ch)
    return seen


def forbidden_chars_message(text):
    """Возвращает текст ошибки для пользователя или пустую строку,
    если запрещённых символов нет."""
    bad = find_forbidden_chars(text)
    if not bad:
        return ""
    bad_str = "  ".join(bad)
    return (
        f"В сообщении есть недопустимые символы: {bad_str}\n"
        f"Удалите их и отправьте снова."
    )


async def reject_if_forbidden_chars(update, text, return_state):
    """Проверяет текст на запрещённые символы. Если найдены — отправляет
    пользователю сообщение об ошибке и возвращает return_state, чтобы
    хендлер мог сразу выйти. Если всё ок — возвращает None."""
    err = forbidden_chars_message(text)
    if not err:
        return None
    try:
        await update.message.reply_text(err)
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения о запрещённых символах: {e}")
    return return_state


# --- Очистка ранее сохранённых данных от запрещённых символов ---
# Запускается один раз при старте бота, чтобы старые расписания/ДЗ/тексты
# (которые могли содержать `/`, `*` и т.п.) перестали ронять отображение
# и редактирование. Создаются .bak-копии файлов на случай отката.

# Имена ключей, которые НЕ нужно чистить (URL, идентификаторы, даты/время и
# другие технические значения с фиксированным форматом).
_SANITIZE_SKIP_KEYS = {
    "url",
    "user_id", "from_user_id", "to_user_id", "creator_id",
    "class_code", "button_id", "msg_id", "timer_id", "added_by",
    "timestamp", "added_at", "created_date", "target_date", "target_time",
    "date", "holidays", "morning_time", "evening_time",
    "birthday", "start", "end",
    "button_type", "type",
}


def _strip_forbidden_chars(text):
    if not isinstance(text, str):
        return text
    if not any(ch in FORBIDDEN_INPUT_CHARS for ch in text):
        return text
    return "".join(ch for ch in text if ch not in FORBIDDEN_INPUT_CHARS)


def _sanitize_existing_value(value, parent_key=None, parent_obj=None):
    if parent_key in _SANITIZE_SKIP_KEYS:
        return value
    # Поле content для кнопок типа "url" не трогаем (там URL).
    if (
        parent_key == "content"
        and isinstance(parent_obj, dict)
        and parent_obj.get("button_type") == "url"
    ):
        return value
    if isinstance(value, str):
        return _strip_forbidden_chars(value)
    if isinstance(value, dict):
        return {
            k: _sanitize_existing_value(v, parent_key=k, parent_obj=value)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_existing_value(item, parent_key=parent_key, parent_obj=parent_obj)
            for item in value
        ]
    return value


_SANITIZE_TARGET_FILES = [
    USERS_FILE,
    CLASSES_FILE,
    TIMERS_FILE,
    ANONYMOUS_MESSAGES_FILE,
    SUGGESTIONS_FILE,
    HOMEWORK_FILE,
    CUSTOM_BUTTONS_FILE,
    BLOCKED_USERS_FILE,
    PERSONAL_BUTTONS_FILE,
    CLASS_BLOCKED_USERS_FILE,
    STARS_STATS_FILE,
    INSTRUCTIONS_FILE,
    USER_CODES_FILE,
    PRICES_FILE,
    GLOBAL_BUTTONS_FILE,
]


def cleanup_existing_data_once():
    """Один раз при старте чистит JSON-файлы от запрещённых символов.

    Создаёт файл-маркер `.forbidden_chars_cleaned`, чтобы не запускать
    очистку повторно. Если хочется почистить ещё раз — удалите этот файл."""
    if os.path.exists(_SANITIZE_MARKER_FILE):
        return

    logger.info("Однократная очистка JSON-файлов от запрещённых символов...")
    cleaned_files = 0
    for fname in _SANITIZE_TARGET_FILES:
        if not os.path.exists(fname):
            continue
        try:
            with open(fname, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Очистка: не удалось прочитать {fname}: {e}")
            continue

        cleaned = _sanitize_existing_value(data)
        before = json.dumps(data, ensure_ascii=False)
        after = json.dumps(cleaned, ensure_ascii=False)
        if before == after:
            continue

        # Бэкап на всякий случай.
        backup = fname + ".bak"
        if not os.path.exists(backup):
            try:
                with open(fname, "rb") as src, open(backup, "wb") as dst:
                    dst.write(src.read())
            except Exception as e:
                logger.error(f"Очистка: не удалось создать бэкап {backup}: {e}")

        try:
            with open(fname, "w", encoding="utf-8") as f:
                json.dump(cleaned, f, ensure_ascii=False, indent=2)
            cleaned_files += 1
            logger.info(f"Очистка: {fname} обновлён (бэкап: {backup}).")
        except Exception as e:
            logger.error(f"Очистка: не удалось записать {fname}: {e}")

    try:
        with open(_SANITIZE_MARKER_FILE, "w", encoding="utf-8") as f:
            f.write("ok")
    except Exception as e:
        logger.error(f"Очистка: не удалось создать маркер {_SANITIZE_MARKER_FILE}: {e}")

    if cleaned_files:
        logger.info(f"Очистка завершена. Файлов обновлено: {cleaned_files}.")
    else:
        logger.info("Очистка завершена. Файлы уже были чистыми.")


# ==================================
# === KEEP-ALIVE СЕРВЕР (чтобы бот не засыпал) ===
# ==================================

async def health_check(request):
    """Health check endpoint для keep-alive"""
    return web.Response(text="OK", status=200)

async def start_keep_alive_server():
    """Запускает простой HTTP сервер для keep-alive"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    logger.info(f"Keep-alive сервер запущен на порту {port}")
    return runner

# ==================================
# === ОПТИМИЗИРОВАННАЯ РАБОТА С ДАННЫМИ ===
# ==================================

def _mongo_load(filename, default):
    """Читает документ из MongoDB. Возвращает (data, found_flag).
    Если документа нет — возвращает default и False."""
    if _mongo_kv is None:
        return None, False
    try:
        doc = _mongo_kv.find_one({"_id": filename})
        if doc is None:
            return default if default is not None else {}, False
        return doc.get("data", default if default is not None else {}), True
    except Exception as e:
        logger.error(f"Mongo load_data({filename}) failed: {e}; fallback to file.")
        return None, False


def _mongo_save(filename, data):
    """Пишет документ в MongoDB. Возвращает True/False.

    Делает до двух попыток подряд: на нестабильной сети первая запись
    может вернуть `AutoReconnect` / `NetworkTimeout`, а вторая — пройти
    нормально. Это критично, когда пользователь требует, чтобы данные
    железно попадали в MongoDB и не оседали в локальном файле.
    """
    if _mongo_kv is None:
        return False
    last_err = None
    for attempt in range(2):
        try:
            _mongo_kv.replace_one(
                {"_id": filename},
                {"_id": filename, "data": data},
                upsert=True,
            )
            return True
        except Exception as e:
            last_err = e
            logger.error(
                f"Mongo save_data({filename}) попытка {attempt + 1}/2 failed: {e}"
            )
    logger.error(
        f"Mongo save_data({filename}) ВСЕ попытки не удались: {last_err}; "
        f"fallback to local file (данные могут разойтись с MongoDB)."
    )
    return False


def load_data(filename, default=None):
    """Универсальный loader. Приоритет источников:
       1) Supabase (если SUPABASE_URL+SUPABASE_KEY заданы и таблица доступна),
       2) MongoDB (если задан MONGO_URI — для обратной совместимости),
       3) Локальный JSON-файл.

    При первом запуске, если в облаке пусто, а локально лежит JSON
    (legacy-данные от прошлой версии бота), он будет однократно перенесён
    в облако.

    Добавлена ленивая инициализация Supabase (аналогично save_data).
    """
    # 1) Supabase
    supabase_available = _supabase_ready
    if not supabase_available and SUPABASE_URL and SUPABASE_KEY:
        supabase_available = _supabase_try_late_init()
    if supabase_available:
        data, found = _supabase_load(filename, default)
        if found:
            return data
        # В Supabase пусто. Если рядом есть локальный JSON (миграция со
        # старой схемы) — однократно перельём его в Supabase.
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    legacy = json.load(f)
                if _supabase_save(filename, legacy):
                    logger.info(
                        f"Supabase migration: {filename} -> Supabase "
                        f"({len(legacy) if hasattr(legacy, '__len__') else 'scalar'} элементов)"
                    )
                return legacy
            except Exception as e:
                logger.error(f"Не удалось мигрировать {filename} в Supabase: {e}")
        return default if default is not None else {}

    # 2) MongoDB (legacy)
    if _mongo_kv is not None:
        data, found = _mongo_load(filename, default)
        if found:
            return data
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    legacy = json.load(f)
                if _mongo_save(filename, legacy):
                    logger.info(f"Mongo migration: {filename} -> MongoDB ({len(legacy) if hasattr(legacy, '__len__') else 'scalar'} элементов)")
                return legacy
            except Exception as e:
                logger.error(f"Не удалось мигрировать {filename} в MongoDB: {e}")
        return default if default is not None else {}

    # 3) Fallback: JSON-файл (если облачное хранилище не настроено).
    try:
        if os.path.exists(filename):
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        return default if default is not None else {}
    except Exception as e:
        logger.error(f"Ошибка при загрузке {filename}: {e}")
        return default if default is not None else {}


def _ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)


def save_data(filename, data):
    """Сохраняет данные в облако (Supabase → Mongo → файл).

    Добавлена ленивая инициализация Supabase: если _supabase_ready==False,
    но SUPABASE_URL+SUPABASE_KEY заданы — пробуем переподключиться.
    """
    # 1) Supabase
    if _supabase_ready:
        if _supabase_save(filename, data):
            return True
    elif SUPABASE_URL and SUPABASE_KEY:
        if _supabase_try_late_init() and _supabase_save(filename, data):
            return True
    # 2) Mongo (legacy)
    if _mongo_kv is not None and _mongo_save(filename, data):
        return True
    # 3) Локальный файл — последний шанс. На Render Free данные пропадут
    # после рестарта, но это лучше, чем потерять прямо сейчас.
    try:
        _ensure_parent_dir(filename)
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Ошибка при сохранении {filename}: {e}")
        return False


async def _async_save_data(filename, data):
    """Асинхронная обёртка save_data (с ленивой инициализацией Supabase)."""
    # 1) Supabase
    if _supabase_ready:
        if _supabase_save(filename, data):
            return
    elif SUPABASE_URL and SUPABASE_KEY:
        if _supabase_try_late_init() and _supabase_save(filename, data):
            return
    # 2) Mongo
    if _mongo_kv is not None and _mongo_save(filename, data):
        return
    # 3) Файл
    try:
        _ensure_parent_dir(filename)
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка при асинхронном сохранении {filename}: {e}")

# ==================================
# === ЦЕНЫ ===
# ==================================

def load_prices():
    default_prices = {
        'button_base': 20,
        'button_increment': 15,
        'unblock': 40,
        'unblock_dev': 100,
        'view_sender': 60,
        # Цена «места для анонимок» — 100⭐/мес. За эту сумму анонимные
        # сообщения, адресованные пользователю, не удаляются авточисткой
        # на ANONYMOUS_TTL_DAYS.
        'anon_keep_month': 100,
        # === Единая цена генерации (синхронизация цен во всех модулях) ===
        # Сколько виртуальных звёзд списывается за ОДНО сообщение в DEVORKS+ai.
        # 0 = бесплатно (старое поведение сохранено по умолчанию). Изменяется
        # из админ-панели разработчика — обновление подхватывается ВСЕМИ
        # модулями мгновенно, потому что они читают PRICES на каждом вызове.
        'ai_generation': 0,
    }
    prices = load_data(PRICES_FILE, default_prices)
    if isinstance(prices, dict) and 'broadcast' in prices:
        prices.pop('broadcast', None)
    # Бэк-совместимость: подставляем дефолт для ключей, которые могли
    # отсутствовать в старом prices.json (иначе PRICES['anon_keep_month']
    # упадёт KeyError'ом на бою).
    if isinstance(prices, dict):
        for k, v in default_prices.items():
            prices.setdefault(k, v)
    return prices

def save_prices(prices):
    return save_data(PRICES_FILE, prices)

def reload_prices():
    """Синхронизация цен: перечитывает prices.json из БД в глобальный PRICES.

    Гарантирует, что изменение цены из админ-панели (или другим процессом)
    мгновенно действует во всех модулях бота — все они читают PRICES при
    каждом вызове, а не копию. Возвращает актуальный словарь цен."""
    global PRICES
    PRICES = load_prices()
    return PRICES

def get_price(key, default=0):
    """Единая точка доступа к ценам для всех модулей.

    Читает глобальный PRICES (который всегда актуален: админка меняет его
    напрямую и перечитывает из БД через reload_prices). Возвращает int."""
    try:
        return int(PRICES.get(key, default))
    except (TypeError, ValueError):
        return int(default)

# ==================================
# === НАСТРОЙКИ РАЗРАБОТЧИКА ===
# ==================================
# Глобальные тогглы для функций, которые разработчик включает/выключает из
# своей панели. По умолчанию все тогглы включены.
DEV_SETTINGS_DEFAULTS = {
    "notify_new_users": True,
}

def load_dev_settings():
    settings = load_data(DEV_SETTINGS_FILE, dict(DEV_SETTINGS_DEFAULTS))
    # Подставляем дефолты для отсутствующих ключей (на случай, если в файле
    # лежит старая версия настроек без новых полей).
    changed = False
    for key, value in DEV_SETTINGS_DEFAULTS.items():
        if key not in settings:
            settings[key] = value
            changed = True
    if changed:
        save_data(DEV_SETTINGS_FILE, settings)
    return settings

def save_dev_settings(settings):
    return save_data(DEV_SETTINGS_FILE, settings)

def is_dev_new_user_notification_enabled():
    return bool(load_dev_settings().get("notify_new_users", True))

def toggle_dev_new_user_notifications():
    settings = load_dev_settings()
    settings["notify_new_users"] = not bool(settings.get("notify_new_users", True))
    save_dev_settings(settings)
    return settings["notify_new_users"]

PRICES = load_prices()

# ==================================
# === АНОНИМКИ: ХРАНЕНИЕ / АВТО-ОЧИСТКА ===
# ==================================
# Сколько дней анонимное сообщение хранится по умолчанию (если получатель
# не оплатил «место»). По истечении срока сообщение удаляется фоновым
# джобом anonymous_purge_job. Сделано константой, чтобы не путать с
# обычными ценами и легко поменять при необходимости.
ANONYMOUS_TTL_DAYS = 60
# За сколько дней до удаления слать предупреждение «завтра удалю всё, если
# не купите место». Уведомление можно выключить в настройках уведомлений.
ANONYMOUS_PURGE_NOTIFY_DAYS_BEFORE = 1
# Сколько секунд между запусками фонового джоба авто-очистки. Раз в сутки
# (86400) — этого хватает, чтобы держать актуальной 60-дневную ретенцию
# и не нагружать диск/JSON.
ANONYMOUS_PURGE_JOB_INTERVAL = 24 * 60 * 60

# ==================================
# === ДЕКОРАТОР ДЛЯ ТАЙМ-АУТА ===
# ==================================

def timeout(seconds):
    """Декоратор-обёртка. Раньше оборачивал хендлер в asyncio.wait_for и при любом
    сбое возвращал ConversationHandler.END — именно это приводило к тому, что
    после нескольких сообщений бот «молчал» и приходилось слать /start.

    Теперь это no-op: аргумент seconds принимается для обратной совместимости,
    но сам декоратор не завершает разговор и не глотает исключения."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Handler {getattr(func, '__name__', 'unknown')} error: {e}")
                update = args[0] if args else None
                context = args[1] if len(args) > 1 else None
                if update is not None and context is not None:
                    try:
                        if getattr(update, "message", None):
                            await update.message.reply_text(
                                "Попробуйте ещё раз или нажмите /start"
                            )
                        elif getattr(update, "callback_query", None):
                            try:
                                await update.callback_query.answer(
                                    "Попробуйте ещё раз", show_alert=False
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass
                # ВАЖНО: возвращаем None — это оставляет текущее состояние разговора,
                # чтобы пользователь мог продолжить без /start.
                return None
        return wrapper
    return decorator

# ==================================
# === МОДЕЛИ ДАННЫХ ===
# ==================================

class User:
    def __init__(self, user_id, username="", first_name=""):
        self.user_id = str(user_id)
        self.username = username or ""
        self.first_name = first_name or "User"
        self.timezone = 3
        self.local_time_set = False
        self.class_code = None
        self.role = "student"
        self.custom_buttons = {}
        self.button_layout = "default"
        self.custom_button_order = []
        self.notifications = True
        self.morning_notification_time = "08:00"
        self.evening_notification_time = "22:00"
        self.morning_text = "Доброе утро! Хорошего дня! 📚"
        self.evening_text = "Спокойной ночи! Хороших снов! 💤"
        self.joined_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.created_classes = []
        self.setup_completed = False
        self.birthday = None
        self.show_birthday_countdown = True
        self.show_birthday_to_class = True
        self.birthday_personal_notification = True
        self.is_blocked = False
        self.max_personal_buttons = 1
        self.personal_button_order = []
        self.stars_balance = 0
        self.total_stars_spent = 0
        self.custom_buttons_created = 0
        # Сколько личных кнопок куплено через Telegram Stars и ещё не использовано.
        # Учитывается в create_personal_button_start, чтобы после успешной оплаты
        # пользователь мог создать кнопку без повторного запроса оплаты.
        self.prepaid_buttons = 0
        self.user_code = None
        self.instructions_read = False
        self.language = "ru"
        self.disclaimer_accepted = False
        self.hidden_buttons = []
        # === Погодные / праздничные настройки (новые) ===
        # Город для запросов к WeatherAPI. None означает «не задан».
        self.city = None
        # Включены ли утренние уведомления о погоде. По умолчанию — ВКЛ.
        self.weather_notifications = True
        # Время утреннего погодного уведомления (локальное время пользователя).
        self.weather_notification_time = "07:00"
        # Уведомления о праздниках (рассылаются всем пользователям, у кого включено).
        # По умолчанию — ВКЛ.
        self.holiday_notifications = True
        # Время, в которое каждый день приходит уведомление «через сколько дней
        # мой день рождения» (локальное время пользователя). По умолчанию 09:00.
        self.birthday_notification_time = "09:00"
        # Когда пользователь последний раз подтвердил подписку нажатием
        # «✅ Я подписался(ась)». Используется как «доверительная» пометка на
        # случай, если бот не админ канала и API проверка невозможна.
        # Формат: "YYYY-MM-DD HH:MM:SS" или None.
        self.subscription_confirmed_at = None
        # Флаг: было ли отправлено уведомление разработчику о появлении этого
        # пользователя. Нужен, чтобы не дублировать уведомление, даже если
        # объект User по какой-то причине пересоздался.
        self.dev_notified = False
        # === Реферальная система ===
        # ID пользователя-«пригласителя» (того, по чьей ссылке зарегистрировался).
        # None, если пользователь пришёл напрямую.
        self.referrer_id = None
        # Сколько успешных приглашений сделал этот пользователь (для статистики).
        self.referrals_count = 0
        # Уже ли начислены REFERRAL_REWARD_STARS виртуальные звёзды
        # пригласителю за этого юзера — защита от дубль-начисления при
        # повторных /start.
        self.referral_bonus_paid = False
        # === Цвет/тема кнопок (UI) ===
        # Идентификатор цветовой темы для кнопок главного меню. Влияет только
        # на префикс-эмодзи в подписях кнопок и на наличие/тип «декорации»
        # перед именем кнопки. Само хранилище кнопок не меняется.
        self.button_theme = "default"
        # === Цветные inline-кнопки (новая фича Telegram 3-цветной разметки) ===
        # Если True — бот добавляет к подписям inline-кнопок цветные «маркеры»
        # (🔴 для отмены/удаления, 🟢 для покупки, 🔵 для остальных), чтобы
        # пользователю было удобнее различать действия. Реализовано через
        # эмодзи-префикс, потому что Bot API не позволяет красить нативный
        # фон кнопок. Тоггл доступен в ⚙️ Настройки → «Цветные кнопки».
        self.colored_buttons_enabled = True
        # === Чат поддержки ===
        # Сообщения поддержки от/к разработчику не хранятся в User напрямую —
        # они уходят непосредственно в чат разработчика. Здесь храним только
        # отметку о последнем обращении (для антиспама и UI).
        self.last_support_at = None
        # === Антиспам ===
        # Время последнего «дорогого» действия пользователя (в секундах epoch).
        # Используется helper'ом is_user_spamming для базового rate-limit'а
        # клавиатурных команд.
        self.last_action_ts = 0.0
        # === Авто-очистка анонимок ===
        # Уведомлять ли пользователя за день до автоматического удаления
        # его входящих анонимных сообщений. По умолчанию — ВКЛ. Тоггл
        # доступен в ⚙️ Настройки → 🔔 Настройки уведомлений.
        self.anon_purge_notify = True
        # До какой даты (включительно) у пользователя оплачено «место»
        # под анонимки и авто-очистка их не трогает. Формат строки —
        # "YYYY-MM-DD HH:MM:SS" в UTC. None значит «не оплачено».
        self.anon_keep_until = None
        # === Магазин товаров: купленные услуги ===
        # Счётчики купленных «кастомных» услуг: {"<название услуги>": количество}.
        # Заполняется автоматически после оплаты товара из «🛍 Магазина».
        self.services = {}
        # === VIP-подписки с периодом ===
        # Ключ — идентификатор подписки (имя услуги или product_id), значение —
        # строка ISO "YYYY-MM-DDTHH:MM:SS" (UTC), до которой подписка активна.
        # Пока подписка активна, повторная покупка того же товара ЗАПРЕЩЕНА.
        self.subscriptions = {}
        # === Режим личности ИИ-чата (DEVORKS+ai) ===
        # "normal" — обычный честный помощник; "hamlo" — режим «хамло»;
        # "warm" — режим «тепло». Выбирается кнопками при входе в чат
        # (или действием set_ai_mode в автоматизации) и сохраняется навсегда.
        self.ai_persona = "normal"

    def to_dict(self):
        return {
            'user_id': self.user_id,
            'username': self.username,
            'first_name': self.first_name,
            'timezone': self.timezone,
            'local_time_set': self.local_time_set,
            'class_code': self.class_code,
            'role': self.role,
            'custom_buttons': self.custom_buttons,
            'button_layout': self.button_layout,
            'custom_button_order': self.custom_button_order,
            'notifications': self.notifications,
            'morning_notification_time': self.morning_notification_time,
            'evening_notification_time': self.evening_notification_time,
            'morning_text': self.morning_text,
            'evening_text': self.evening_text,
            'joined_date': self.joined_date,
            'created_classes': self.created_classes,
            'setup_completed': self.setup_completed,
            'birthday': self.birthday,
            'show_birthday_countdown': self.show_birthday_countdown,
            'show_birthday_to_class': self.show_birthday_to_class,
            'birthday_personal_notification': self.birthday_personal_notification,
            'is_blocked': self.is_blocked,
            'max_personal_buttons': self.max_personal_buttons,
            'personal_button_order': self.personal_button_order,
            'stars_balance': self.stars_balance,
            'total_stars_spent': self.total_stars_spent,
            'custom_buttons_created': self.custom_buttons_created,
            'prepaid_buttons': getattr(self, 'prepaid_buttons', 0),
            'user_code': self.user_code,
            'instructions_read': self.instructions_read,
            'language': self.language,
            'disclaimer_accepted': self.disclaimer_accepted,
            'hidden_buttons': getattr(self, 'hidden_buttons', []),
            # Погодные / праздничные поля (новые)
            'city': getattr(self, 'city', None),
            'weather_notifications': getattr(self, 'weather_notifications', True),
            'weather_notification_time': getattr(self, 'weather_notification_time', '07:00'),
            'holiday_notifications': getattr(self, 'holiday_notifications', True),
            'birthday_notification_time': getattr(self, 'birthday_notification_time', '09:00'),
            'subscription_confirmed_at': getattr(self, 'subscription_confirmed_at', None),
            'dev_notified': getattr(self, 'dev_notified', False),
            # Реферальная система
            'referrer_id': getattr(self, 'referrer_id', None),
            'referrals_count': getattr(self, 'referrals_count', 0),
            'referral_bonus_paid': getattr(self, 'referral_bonus_paid', False),
            # Цвет/тема кнопок
            'button_theme': getattr(self, 'button_theme', 'default'),
            # Цветные inline-кнопки (новая фича)
            'colored_buttons_enabled': getattr(self, 'colored_buttons_enabled', True),
            # Чат поддержки и антиспам
            'last_support_at': getattr(self, 'last_support_at', None),
            'last_action_ts': getattr(self, 'last_action_ts', 0.0),
            # Авто-очистка анонимок
            'anon_purge_notify': getattr(self, 'anon_purge_notify', True),
            'anon_keep_until': getattr(self, 'anon_keep_until', None),
            # Магазин товаров: купленные услуги
            'services': getattr(self, 'services', {}),
            # VIP-подписки с периодом (ключ → ISO-дата окончания, UTC)
            'subscriptions': getattr(self, 'subscriptions', {}),
            # Режим личности ИИ-чата: normal / hamlo / warm
            'ai_persona': getattr(self, 'ai_persona', 'normal'),
        }

    @classmethod
    def from_dict(cls, data):
        user = cls(data['user_id'], data.get('username', ''), data.get('first_name', ''))
        for key, value in data.items():
            if hasattr(user, key):
                setattr(user, key, value)
        if not hasattr(user, 'hidden_buttons'):
            user.hidden_buttons = []
        if not hasattr(user, 'birthday_personal_notification'):
            user.birthday_personal_notification = True
        # Бэк-совместимость: «рассказывать ли классу о моём ДР».
        # По умолчанию — ВКЛ, чтобы старые пользователи (у которых поле
        # отсутствовало) автоматически получили эту фичу: бот объявляет
        # одноклассникам, что у пользователя сегодня ДР.
        if not hasattr(user, 'show_birthday_to_class'):
            user.show_birthday_to_class = True
        if not hasattr(user, 'show_birthday_countdown'):
            user.show_birthday_countdown = True
        if not hasattr(user, 'prepaid_buttons') or user.prepaid_buttons is None:
            user.prepaid_buttons = 0
        # Бэк-совместимость: новые поля могли отсутствовать у старых пользователей.
        if not hasattr(user, 'city'):
            user.city = None
        if not hasattr(user, 'weather_notifications'):
            user.weather_notifications = True
        if not hasattr(user, 'weather_notification_time') or not user.weather_notification_time:
            user.weather_notification_time = "07:00"
        if not hasattr(user, 'holiday_notifications'):
            user.holiday_notifications = True
        if not hasattr(user, 'birthday_notification_time') or not user.birthday_notification_time:
            user.birthday_notification_time = "09:00"
        if not hasattr(user, 'subscription_confirmed_at'):
            user.subscription_confirmed_at = None
        if not hasattr(user, 'dev_notified'):
            user.dev_notified = False
        # Бэк-совместимость: поля «реферальной системы», темы, поддержки.
        if not hasattr(user, 'referrer_id'):
            user.referrer_id = None
        if not hasattr(user, 'referrals_count') or user.referrals_count is None:
            user.referrals_count = 0
        if not hasattr(user, 'referral_bonus_paid'):
            user.referral_bonus_paid = False
        if not hasattr(user, 'button_theme') or not user.button_theme:
            user.button_theme = 'default'
        if not hasattr(user, 'colored_buttons_enabled') or user.colored_buttons_enabled is None:
            user.colored_buttons_enabled = True
        if not hasattr(user, 'last_support_at'):
            user.last_support_at = None
        if not hasattr(user, 'last_action_ts') or user.last_action_ts is None:
            user.last_action_ts = 0.0
        # Бэк-совместимость: поля авто-очистки анонимок.
        if not hasattr(user, 'anon_purge_notify') or user.anon_purge_notify is None:
            user.anon_purge_notify = True
        if not hasattr(user, 'anon_keep_until'):
            user.anon_keep_until = None
        # Бэк-совместимость: купленные услуги магазина.
        if not hasattr(user, 'services') or not isinstance(user.services, dict):
            user.services = {}
        # Бэк-совместимость: VIP-подписки с периодом.
        if not hasattr(user, 'subscriptions') or not isinstance(user.subscriptions, dict):
            user.subscriptions = {}
        # Бэк-совместимость: режим личности ИИ-чата.
        persona = getattr(user, 'ai_persona', None)
        if persona not in AI_PERSONA_MODES:
            user.ai_persona = "normal"
        return user


class PersonalButton:
    def __init__(self, button_id, user_id, name, content, button_type="text"):
        self.button_id = button_id
        self.user_id = str(user_id)
        self.name = name
        self.content = content
        self.button_type = button_type
        self.created_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.is_active = True
        self.position = 0
        self.row = 1

    def to_dict(self):
        return {
            'button_id': self.button_id,
            'user_id': self.user_id,
            'name': self.name,
            'content': self.content,
            'button_type': self.button_type,
            'created_date': self.created_date,
            'is_active': self.is_active,
            'position': self.position,
            'row': self.row
        }

    @classmethod
    def from_dict(cls, data):
        button = cls(
            data['button_id'],
            data['user_id'],
            data['name'],
            data['content'],
            data.get('button_type', 'text')
        )
        button.created_date = data.get('created_date', datetime.now().strftime("%Y-%m-%d %H:%M"))
        button.is_active = data.get('is_active', True)
        button.position = data.get('position', 0)
        button.row = data.get('row', 1)
        return button


class Class:
    def __init__(self, class_code, class_name, creator_id):
        self.class_code = class_code
        self.class_name = class_name
        self.creator_id = str(creator_id)
        self.admins = [str(creator_id)]
        self.students = [str(creator_id)]
        self.schedule = {
            "Понедельник": "1. Математика\n2. Русский язык\n3. Физика",
            "Вторник": "1. Литература\n2. История\n3. Химия",
            "Среда": "1. Биология\n2. География\n3. Английский язык",
            "Четверг": "1. Физкультура\n2. Информатика\n3. Музыка",
            "Пятница": "1. ИЗО\n2. Технология\n3. ОБЖ"
        }
        self.teachers = {
            "Математика": "Иванова И.И.",
            "Русский язык": "Петрова П.П.",
            "Физика": "Сидоров С.С.",
            "Литература": "Кузнецова К.К.",
            "История": "Смирнов С.С.",
            "Химия": "Васильев В.В.",
            "Биология": "Орлова О.О.",
            "География": "Николаев Н.Н.",
            "Английский язык": "Александрова А.А.",
            "Физкультура": "Дмитриев Д.Д.",
            "Информатика": "Павлов П.П.",
            "Музыка": "Сергеева С.С.",
            "ИЗО": "Федорова Ф.Ф.",
            "Технология": "Григорьев Г.Г.",
            "ОБЖ": "Борисов Б.Б."
        }
        self.bells = {
            "1": {"start": "08:00", "end": "08:45"},
            "2": {"start": "08:55", "end": "09:40"},
            "3": {"start": "09:50", "end": "10:35"},
            "4": {"start": "10:45", "end": "11:30"},
            "5": {"start": "11:40", "end": "12:25"},
            "6": {"start": "12:35", "end": "13:20"}
        }
        self.holidays = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        self.created_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.is_active = True
        self.subjects = list(self.teachers.keys())
        self.homework = {}
        self.blocked_users = []
        self.class_buttons = []

    def to_dict(self):
        return {
            'class_code': self.class_code,
            'class_name': self.class_name,
            'creator_id': self.creator_id,
            'admins': self.admins,
            'students': self.students,
            'schedule': self.schedule,
            'teachers': self.teachers,
            'bells': self.bells,
            'holidays': self.holidays,
            'created_date': self.created_date,
            'is_active': self.is_active,
            'subjects': self.subjects,
            'homework': self.homework,
            'blocked_users': self.blocked_users,
            'class_buttons': self.class_buttons
        }

    @classmethod
    def from_dict(cls, data):
        class_obj = cls(data['class_code'], data['class_name'], data['creator_id'])
        for key, value in data.items():
            if hasattr(class_obj, key):
                setattr(class_obj, key, value)
        return class_obj


class Timer:
    def __init__(self, user_id, target_date, target_time, text):
        self.user_id = str(user_id)
        self.target_date = target_date
        self.target_time = target_time
        self.text = text
        self.is_active = True
        self.created_date = datetime.now().strftime("%Y-%m-%d %H:%M")

    def to_dict(self):
        return {
            'user_id': self.user_id,
            'target_date': self.target_date,
            'target_time': self.target_time,
            'text': self.text,
            'is_active': self.is_active,
            'created_date': self.created_date
        }

    @classmethod
    def from_dict(cls, data):
        timer = cls(data['user_id'], data['target_date'], data['target_time'], data['text'])
        timer.is_active = data.get('is_active', True)
        timer.created_date = data.get('created_date', datetime.now().strftime("%Y-%m-%d %H:%M"))
        return timer


class CustomButton:
    def __init__(self, button_id, class_code, name, content, creator_id, button_type="text"):
        self.button_id = button_id
        self.class_code = class_code
        self.name = name
        self.content = content
        self.creator_id = str(creator_id)
        self.button_type = button_type
        self.created_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.is_active = True

    def to_dict(self):
        return {
            'button_id': self.button_id,
            'class_code': self.class_code,
            'name': self.name,
            'content': self.content,
            'creator_id': self.creator_id,
            'button_type': self.button_type,
            'created_date': self.created_date,
            'is_active': self.is_active
        }

    @classmethod
    def from_dict(cls, data):
        button = cls(
            data['button_id'],
            data['class_code'],
            data['name'],
            data['content'],
            data['creator_id'],
            data.get('button_type', 'text')
        )
        button.created_date = data.get('created_date', datetime.now().strftime("%Y-%m-%d %H:%M"))
        button.is_active = data.get('is_active', True)
        return button


class GlobalButton:
    def __init__(self, button_id, name, content, button_type="text"):
        self.button_id = button_id
        self.name = name
        self.content = content
        self.button_type = button_type
        self.created_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.is_active = True

    def to_dict(self):
        return {
            'button_id': self.button_id,
            'name': self.name,
            'content': self.content,
            'button_type': self.button_type,
            'created_date': self.created_date,
            'is_active': self.is_active
        }

    @classmethod
    def from_dict(cls, data):
        button = cls(
            data['button_id'],
            data['name'],
            data['content'],
            data.get('button_type', 'text')
        )
        button.created_date = data.get('created_date', datetime.now().strftime("%Y-%m-%d %H:%M"))
        button.is_active = data.get('is_active', True)
        return button

# ==================================
# === ОПТИМИЗИРОВАННАЯ РАБОТА С ДАННЫМИ (КЭШИРОВАНИЕ) ===
# ==================================

def load_users():
    global _users_cache
    current_time = time.time()

    if 'users' in _cache_last_update and current_time - _cache_last_update.get('users', 0) < CACHE_TTL:
        return _users_cache

    data = load_data(USERS_FILE)
    users = {}
    for user_id, user_data in data.items():
        try:
            users[user_id] = User.from_dict(user_data)
        except Exception as e:
            logger.error(f"Ошибка при загрузке пользователя {user_id}: {e}")

    _users_cache = users
    _cache_last_update['users'] = current_time
    return users

def save_users(users):
    global _users_cache
    _users_cache = users
    data = {user_id: user.to_dict() for user_id, user in users.items()}
    return save_data(USERS_FILE, data)

def load_personal_buttons():
    global _personal_buttons_cache
    current_time = time.time()

    if 'personal_buttons' in _cache_last_update and current_time - _cache_last_update.get('personal_buttons', 0) < CACHE_TTL:
        return _personal_buttons_cache

    data = load_data(PERSONAL_BUTTONS_FILE)
    buttons = {}
    for button_id, button_data in data.items():
        try:
            buttons[button_id] = PersonalButton.from_dict(button_data)
        except Exception as e:
            logger.error(f"Ошибка при загрузке личной кнопки {button_id}: {e}")

    _personal_buttons_cache = buttons
    _cache_last_update['personal_buttons'] = current_time
    return buttons

def save_personal_buttons(buttons):
    global _personal_buttons_cache
    _personal_buttons_cache = buttons
    data = {button_id: button.to_dict() for button_id, button in buttons.items()}
    return save_data(PERSONAL_BUTTONS_FILE, data)

def load_classes():
    global _classes_cache
    current_time = time.time()

    if 'classes' in _cache_last_update and current_time - _cache_last_update.get('classes', 0) < CACHE_TTL:
        return _classes_cache

    data = load_data(CLASSES_FILE)
    classes = {}
    for class_code, class_data in data.items():
        try:
            classes[class_code] = Class.from_dict(class_data)
        except Exception as e:
            logger.error(f"Ошибка при загрузке класса {class_code}: {e}")

    _classes_cache = classes
    _cache_last_update['classes'] = current_time
    return classes

def save_classes(classes):
    global _classes_cache
    _classes_cache = classes
    data = {class_code: class_obj.to_dict() for class_code, class_obj in classes.items()}
    return save_data(CLASSES_FILE, data)

def load_global_buttons():
    global _global_buttons_cache
    current_time = time.time()

    if 'global_buttons' in _cache_last_update and current_time - _cache_last_update.get('global_buttons', 0) < CACHE_TTL:
        return _global_buttons_cache

    data = load_data(GLOBAL_BUTTONS_FILE, {})
    buttons = {}
    for button_id, button_data in data.items():
        try:
            buttons[button_id] = GlobalButton.from_dict(button_data)
        except Exception as e:
            logger.error(f"Ошибка при загрузке глобальной кнопки {button_id}: {e}")

    _global_buttons_cache = buttons
    _cache_last_update['global_buttons'] = current_time
    return buttons

def save_global_buttons(buttons):
    global _global_buttons_cache
    _global_buttons_cache = buttons
    data = {button_id: button.to_dict() for button_id, button in buttons.items()}
    return save_data(GLOBAL_BUTTONS_FILE, data)

def load_blocked_users():
    return load_data(BLOCKED_USERS_FILE, {})

def save_blocked_users(blocked_users):
    return save_data(BLOCKED_USERS_FILE, blocked_users)

def load_class_blocked_users():
    return load_data(CLASS_BLOCKED_USERS_FILE, {})

def save_class_blocked_users(class_blocked):
    return save_data(CLASS_BLOCKED_USERS_FILE, class_blocked)

def load_stars_stats():
    """Агрегированная статистика Stars БЕЗ подробной истории переводов.

    По требованию пользователя подробная история переводов полностью удалена:
    при первом же чтении старое поле 'transactions' стирается из файла и
    больше нигде не создаётся. Хранятся только АГРЕГАТЫ:
      — total_stars_spent: суммарно потрачено Stars;
      — spenders: карта {user_id: total_spent} по ВСЕМ тратившим (без капы);
      — top_donors: топ-10 по тратам (производная от spenders);
      — spenders_count: сколько пользователей вообще что-то тратили.
    """
    stats = load_data(STARS_STATS_FILE, {})
    if not stats:
        stats = {
            'total_stars_spent': 0,
            'top_donors': [],
            'spenders': {},
            'spenders_count': 0,
        }
    changed = False
    # ПОЛНАЯ ОЧИСТКА подробной истории переводов (однократная миграция).
    if 'transactions' in stats:
        stats.pop('transactions', None)
        changed = True
    if 'total_stars_spent' not in stats:
        stats['total_stars_spent'] = 0
        changed = True
    if 'spenders' not in stats or not isinstance(stats.get('spenders'), dict):
        # Реконструкция карты тративших из старого топа (лучшее возможное
        # после удаления истории — точность дальше поддерживается инкрементально).
        stats['spenders'] = {
            str(d.get('user_id')): int(d.get('total_spent', 0))
            for d in stats.get('top_donors', []) if isinstance(d, dict)
        }
        changed = True
    if 'top_donors' not in stats:
        stats['top_donors'] = []
        changed = True
    if 'spenders_count' not in stats:
        stats['spenders_count'] = len(stats.get('spenders', {}))
        changed = True
    if changed:
        save_stars_stats(stats)
    return stats


def register_stars_spending(user_id, user_name, amount):
    """Регистрирует потраченные Stars в агрегированной статистике.

    НЕ пишет никаких записей-переводов: только инкремент общей суммы,
    обновление карты тративших (spenders) и производного топ-10.
    """
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return False
    if amount <= 0:
        return False
    stats = load_stars_stats()
    stats['total_stars_spent'] = int(stats.get('total_stars_spent', 0)) + amount

    spenders = stats.get('spenders') or {}
    if not isinstance(spenders, dict):
        spenders = {}
    key = str(user_id)
    spenders[key] = int(spenders.get(key, 0)) + amount
    stats['spenders'] = spenders

    # Топ-10 — производная от полной карты тративших.
    users = load_users()
    def _name_for(uid):
        u = users.get(str(uid))
        return (u.first_name if u and getattr(u, 'first_name', None) else None)
    stats['top_donors'] = [
        {'user_id': uid, 'user_name': _name_for(uid) or 'Пользователь', 'total_spent': total}
        for uid, total in sorted(spenders.items(), key=lambda kv: kv[1], reverse=True)[:10]
    ]
    stats['spenders_count'] = len(spenders)
    save_stars_stats(stats)
    return True


def reset_stars_stats():
    """Полный сброс статистики Stars (только для разработчика)."""
    return save_stars_stats({
        'total_stars_spent': 0,
        'top_donors': [],
        'spenders': {},
        'spenders_count': 0,
    })

def save_stars_stats(stats):
    return save_data(STARS_STATS_FILE, stats)


# ==================================
# === АНТИСПАМ (rate-limit) ===
# ==================================
# Простой in-memory rate-limit, не зависящий от перезапуска: хранит для каждого
# user_id → ключа действия время последнего вызова и количество вызовов.
# Для долгоживущих ограничений (пишет «жалуется» в саппорт каждые 5 секунд) —
# имеется fallback на user.last_action_ts (сериализуется в users.json).
_RATE_LIMIT_BUCKETS = {}


def is_user_spamming(user_id, key="default", min_interval=0.7, burst=4, burst_window=5.0):
    """Возвращает True, если пользователь user_id «спамит» действием key.

    Параметры:
      min_interval — минимальная пауза между двумя событиями (сек).
      burst — сколько событий разрешено в окне burst_window (защита от
              многократного быстрого тапа).
      burst_window — окно, в котором считаем burst (сек).

    Использование:
      if is_user_spamming(user_id, "menu"):
          await reply("⏳ Слишком быстро. Подождите немного."); return
    Хелпер не блокирует пользователя в БД — это лишь мягкое замедление.
    """
    import time as _time
    now = _time.monotonic()
    bucket = _RATE_LIMIT_BUCKETS.setdefault(str(user_id), {})
    info = bucket.setdefault(key, {"last": 0.0, "events": []})
    last = info["last"]
    events = info["events"]
    # Чистим устаревшие события из окна.
    cutoff = now - burst_window
    info["events"] = [t for t in events if t >= cutoff]
    events = info["events"]
    if (now - last) < min_interval:
        return True
    if len(events) >= burst:
        return True
    info["last"] = now
    events.append(now)
    return False


def get_user_rate_limit_message():
    """Дружелюбное сообщение о превышении лимита частоты действий."""
    return (
        "⏳ Слишком много действий подряд. "
        "Подождите 1–2 секунды и повторите попытку."
    )


# ==================================
# === ЧАТ ПОДДЕРЖКИ ===
# ==================================
def load_support_messages():
    """Возвращает структуру { '<user_id>': [ {from, text, ts}, ... ] }."""
    return load_data(SUPPORT_MESSAGES_FILE, {})


def save_support_messages(data):
    return save_data(SUPPORT_MESSAGES_FILE, data)


def append_support_message(user_id, sender, text):
    """Добавляет запись в журнал поддержки. sender ∈ {'user','dev'}."""
    data = load_support_messages()
    arr = data.setdefault(str(user_id), [])
    arr.append({
        'from': sender,
        'text': text,
        'ts': datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    # Ограничим размер журнала: на каждого пользователя — последние 200 сообщений.
    if len(arr) > 200:
        del arr[:len(arr) - 200]
    save_support_messages(data)


# ==================================
# === РЕФЕРАЛЬНАЯ СИСТЕМА ===
# ==================================
def load_referrals():
    return load_data(REFERRALS_FILE, {})


def save_referrals(data):
    return save_data(REFERRALS_FILE, data)


# Глобальный потокобезопасный лок на начисление реферального бонуса.
# Без него возможна гонка: два места кода могут одновременно вызвать
# `credit_referrer_for(A, B)`, оба пройдут проверки `referral_bonus_paid=False`,
# и оба начислят +REFERRAL_REWARD_STARS, удвоив бонус. Лок гарантирует,
# что ровно ОДИН вызов выполнит начисление, а все остальные увидят уже
# зафиксированное состояние и тихо вернут False.
# (`threading` уже импортирован на строке 9, здесь используем его напрямую.)
_referral_credit_lock = threading.Lock()


def credit_referrer_for(new_user_id, referrer_id):
    """Если new_user_id ещё не был засчитан как приглашённый referrer_id —
    начисляет referrer_id ровно REFERRAL_REWARD_STARS звёзд (виртуальных)
    и помечает new_user_id.referral_bonus_paid = True.

    Возвращает True, если бонус начислен в этом вызове, иначе False.

    ВАЖНО (защита от удвоения звёзд): функция использует глобальный
    `threading.Lock`, а после захвата лока ПЕРЕЧИТЫВАЕТ актуальное
    состояние из хранилища (referrals + users). Это гарантирует, что
    параллельные вызовы не приведут к двойному начислению, даже если
    обе вызывающие стороны увидели `referral_bonus_paid=False` ДО
    блокировки.
    """
    if not referrer_id or str(referrer_id) == str(new_user_id):
        return False
    with _referral_credit_lock:
        # ПЕРЕЧИТЫВАЕМ состояние под локом — это ключевая защита от гонок.
        # Если параллельный вызов уже зафиксировал начисление, мы увидим
        # это и тихо выйдем без повторной выдачи звёзд.
        referrals = load_referrals()
        inviter_list = referrals.setdefault(str(referrer_id), [])
        if str(new_user_id) in inviter_list:
            return False
        users = load_users()
        new_u = users.get(str(new_user_id))
        if new_u is not None and getattr(new_u, 'referral_bonus_paid', False):
            # Бонус уже отмечен как выплаченный — не выдаём повторно.
            # Но синхронизируем referrals.json, чтобы там тоже была отметка.
            if str(new_user_id) not in inviter_list:
                inviter_list.append(str(new_user_id))
                try:
                    save_referrals(referrals)
                except Exception as e:
                    logger.error(f"credit_referrer_for: sync referrals failed: {e}")
            return False
        referrer = users.get(str(referrer_id))
        if not referrer:
            # Пригласитель не зарегистрирован у нас — игнорируем тихо.
            return False
        # КРИТИЧНО: записываем флаги ПЕРЕД add_stars_transaction. Это
        # гарантирует, что если add_stars_transaction по какой-то причине
        # упадёт повторно — пользователь не сможет ещё раз пройти проверки
        # выше и получить бонус «второй раз». А если save_users упадёт,
        # то и звёзды не будут начислены.
        inviter_list.append(str(new_user_id))
        save_referrals(referrals)
        referrer.referrals_count = getattr(referrer, 'referrals_count', 0) + 1
        new_u.referral_bonus_paid = True
        save_users(users)
        # ПУНКТ 7: НЕ начисляем звёзды напрямую через `referrer.stars_balance`,
        # иначе они удваивались бы — `add_stars_transaction` ниже сам прибавляет
        # сумму к балансу и логирует транзакцию.
        add_stars_transaction(
            referrer_id,
            REFERRAL_REWARD_STARS,
            f"Реферальный бонус за приглашение {new_user_id}"
        )
        return True


def build_referral_link(bot_username, user_id):
    """Возвращает t.me-ссылку с реферальным кодом (deep-link payload). При
    отсутствии bot_username возвращает простую инструкцию."""
    if not bot_username:
        return None
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


INSTRUCTIONS_VERSION = "2.2"


def _default_instructions_text():
    return (
        '📚 **ИНСТРУКЦИЯ ПО ИСПОЛЬЗОВАНИЮ БОТА DEVORKS+**\n\n'
        '**📋 ОСНОВНЫЕ ФУНКЦИИ:**\n'
        '1. **Регистрация** — укажите дату рождения и местное время (нужно для часового пояса и поздравлений).\n'
        '2. **Классы** — создайте класс или войдите по коду. В одном классе до 40 человек и до 2 доп. админов.\n'
        '3. **Расписание** — смотрите расписание на сегодня и на завтра. Админ класса настраивает расписание и звонки.\n'
        '4. **Домашнее задание** — быстрое добавление: выберите предмет из списка, дату одной кнопкой (Сегодня/Завтра/+2 дня/+7 дней), введите текст. Админы могут добавлять и удалять ДЗ прямо из меню.\n'
        '5. **Таймер** — быстрая установка: 5/10/15/30 мин, 1/2/3/6/12 ч, либо произвольная дата и время. '
        'Можно ставить сразу несколько таймеров подряд — после каждого бот предложит поставить ещё один. Напоминания приходят в заданное время и не теряются при перезапуске.\n'
        '6. **Утренние и вечерние уведомления** — настройте удобное время и свой текст для «доброе утро» и «спокойной ночи».\n'
        '7. **День рождения** — бот сам поздравит вас в этот день и (по желанию) напомнит классу.\n'
        '8. **Погода и праздники** — ежедневный прогноз по вашему городу и напоминания о праздниках.\n'
        '9. **DEVORKS+ai** — умный помощник: задайте любой вопрос и получите ответ. Всегда отвечает честно. Три режима: Обычный, 😈 Хамло и 🥰 Тепло (переключаются кнопками при входе в чат). Присылайте фото с текстом (ДЗ, документы) — бот сам распознает текст (OCR).\n'
        '10. **Анонимные сообщения** — отправляйте одноклассникам анонимные сообщения и отвечайте на них.\n'
        '11. **Личные и классные кнопки** — создавайте свои кнопки (ссылки/текст) и делайте меню удобным.\n'
        '12. **🪄 Автоматизация** — напишите обычным языком («русский на пятницу страница 45», «погода на завтра», «скрой кнопку Погода», «включи режим хамло», «напиши анонимно Пете …») — бот сам выполнит. Управляет всем: ДЗ, учителя, звонки, таймеры, кнопки, анонимки, погода и режимы ИИ.\n\n'
        '**⚙️ НАСТРОЙКИ КНОПОК:**\n'
        '• Скрыть/показать любые кнопки\n'
        '• Переименовать кнопки\n'
        '• Изменить расположение (стандартное/компактное/широкое)\n'
        '• Переместить кнопки между рядами\n'
        '• 🎨 **Тема оформления кнопок** — настройки → «Тема кнопок». 12 цветов/иконок.\n\n'
        '**🆕 НОВЫЕ ВОЗМОЖНОСТИ:**\n'
        '• 🔗 **Реферальная система** — кнопка «⭐ Звезды» → «Поделиться ссылкой». '
        'За каждого друга, кто перейдёт по вашей ссылке и зарегистрируется, '
        f'вам начислят +{REFERRAL_REWARD_STARS}⭐ на внутренний баланс.\n'
        '• 💸 **Внутренние звёзды** — кнопка «⭐ Звезды» → «Потратить на функции». '
        'Можно купить дополнительные слоты, премиум-тему и анонимные кредиты.\n'
        '• 💳 **Двойная оплата** — функции можно оплачивать как внутренней валютой, '
        'так и Telegram Stars (XTR). Если внутренних звёзд не хватает, бот предложит купить их за Telegram Stars.\n'
        '• 💬 **Чат поддержки** — кнопка «💬 Чат поддержки» в главном меню. '
        'Напишите разработчику напрямую и получите ответ в этом же чате.\n'
        '• 🌦 **Погода по вашему городу** — часовой пояс определяется точно по городу (IANA-пояс), ежедневный прогноз приходит в ваше локальное время, а «🌦 Погода» покажет погоду сейчас и прогноз на 3 дня.\n'
        '• 🛡 **Защита от спама** — действия и сообщения ограничены по частоте, '
        'чтобы бот работал стабильно для всех. При срабатывании появится подсказка.\n\n'
        '**💰 ЦЕНЫ (⭐ Telegram Stars):**\n'
        '• Создание кнопки: 1-я бесплатно, 2-я — {button_base} ⭐, далее +{button_increment} ⭐\n'
        '• Разблокировка (админ) — {unblock} ⭐\n'
        '• Разблокировка (разработчик) — {unblock_dev} ⭐\n'
        '• Узнать отправителя анонимки — {view_sender} ⭐\n\n'
        'Актуальные цены всегда подтягиваются из настроек бота — разработчик '
        'может изменить их в любой момент, и в этой инструкции и в самих '
        'функциях сразу отобразятся новые значения.\n\n'
        '⚠️ **ПРЕДУПРЕЖДЕНИЯ:**\n'
        '• Максимум 40 человек в классе.\n'
        '• До 2 дополнительных админов.\n'
        '• Блокировка в классе ограничивает доступ к функциям.\n'
        '• Для использования бота нужно быть подписанным на наш Telegram-канал.\n\n'
        '⚠️ **ОТКАЗ ОТ ОТВЕТСТВЕННОСТИ:**\n'
        'Разработчик не несет ответственности за содержание сообщений пользователей и возможные сбои.'
    )


def load_instructions():
    """Загружает инструкции. При первом запуске и при смене
    INSTRUCTIONS_VERSION обновляет файл на актуальный текст — чтобы
    инструкция всегда соответствовала текущей версии приложения.
    Ручные правки разработчика через «📝 Редактировать инструкцию»
    сохраняют version='custom' и такими авто-обновлениями не затираются.
    """
    instructions = load_data(INSTRUCTIONS_FILE, {})
    if not isinstance(instructions, dict):
        instructions = {}
    stored_version = instructions.get('version')
    need_update = (
        not instructions
        or 'ru' not in instructions
        or (stored_version not in (INSTRUCTIONS_VERSION, 'custom'))
    )
    if need_update:
        instructions = {
            'version': INSTRUCTIONS_VERSION,
            'ru': _default_instructions_text(),
        }
        save_data(INSTRUCTIONS_FILE, instructions)
    return instructions

def get_instructions():
    """Возвращает инструкцию с актуальными ценами разработчика."""
    instructions = load_instructions()
    text = instructions.get('ru', 'Инструкция не найдена')
    # Подставляем актуальные цены
    try:
        prices = load_prices()
        text = text.format(
            button_base=prices.get('button_base', 20),
            button_increment=prices.get('button_increment', 15),
            unblock=prices.get('unblock', 40),
            unblock_dev=prices.get('unblock_dev', 100),
            view_sender=prices.get('view_sender', 60),
        )
    except (KeyError, IndexError, ValueError):
        # Если в инструкции нет плейсхолдеров — возвращаем как есть
        pass
    return text

def save_instructions(instructions):
    return save_data(INSTRUCTIONS_FILE, instructions)

def load_user_codes():
    return load_data(USER_CODES_FILE, {})

def save_user_codes(codes):
    return save_data(USER_CODES_FILE, codes)

def is_user_blocked(user_id):
    blocked_users = load_blocked_users()
    return str(user_id) in blocked_users

def is_user_class_blocked(user_id, class_code):
    class_obj = get_class_by_code(class_code)
    if class_obj and str(user_id) in class_obj.blocked_users:
        return True
    return False

def block_user(user_id, blocked_by=DEVELOPER_ID, unblock_price=None):
    # === Защита от самоблокировки ===
    # Разработчик и любой администратор класса не могут заблокировать сами
    # себя — это защита от случайной/ошибочной потери доступа к управлению.
    # Также запрещаем блокировать DEVELOPER_ID кому угодно (никто не может
    # заблокировать разработчика — он отвечает за бота).
    if str(user_id) == str(DEVELOPER_ID):
        logger.warning(
            f"block_user отклонён: попытка заблокировать разработчика {user_id} "
            f"со стороны {blocked_by}."
        )
        return False
    if str(user_id) == str(blocked_by):
        logger.warning(
            f"block_user отклонён: пользователь {user_id} попытался заблокировать сам себя."
        )
        return False
    blocked_users = load_blocked_users()
    blocked_data = {
        'blocked_at': datetime.now().strftime("%Y-%m-%d %H:%M"),
        'blocked_by': str(blocked_by)
    }
    if unblock_price is not None:
        blocked_data['unblock_price'] = unblock_price
    blocked_users[str(user_id)] = blocked_data

    users = load_users()
    if str(user_id) in users:
        users[str(user_id)].is_blocked = True
        save_users(users)
    return save_blocked_users(blocked_users)

def unblock_user(user_id):
    blocked_users = load_blocked_users()
    if str(user_id) in blocked_users:
        del blocked_users[str(user_id)]
        users = load_users()
        if str(user_id) in users:
            users[str(user_id)].is_blocked = False
            save_users(users)
        return save_blocked_users(blocked_users)
    return True

def block_user_in_class(user_id, class_code, admin_id):
    # === Защита от самоблокировки ===
    # Администратор не может заблокировать себя в собственном классе. Также
    # никто не может заблокировать создателя класса или DEVELOPER_ID — это
    # защита от потери управления классом.
    if str(user_id) == str(admin_id):
        logger.warning(
            f"block_user_in_class отклонён: админ {admin_id} попытался "
            f"заблокировать сам себя в классе {class_code}."
        )
        return False
    if str(user_id) == str(DEVELOPER_ID):
        logger.warning(
            f"block_user_in_class отклонён: попытка заблокировать разработчика "
            f"{user_id} в классе {class_code}."
        )
        return False
    class_obj = get_class_by_code(class_code)
    if class_obj:
        # Создателя класса блокировать запрещено — иначе можно «потерять» класс.
        if hasattr(class_obj, 'creator_id') and str(user_id) == str(class_obj.creator_id):
            logger.warning(
                f"block_user_in_class отклонён: попытка заблокировать создателя "
                f"класса {user_id} в {class_code}."
            )
            return False
        if str(user_id) not in class_obj.blocked_users:
            class_obj.blocked_users.append(str(user_id))
            classes = load_classes()
            classes[class_code] = class_obj
            save_classes(classes)
            return True
    return False

def unblock_user_in_class(user_id, class_code):
    class_obj = get_class_by_code(class_code)
    if class_obj and str(user_id) in class_obj.blocked_users:
        class_obj.blocked_users.remove(str(user_id))
        classes = load_classes()
        classes[class_code] = class_obj
        save_classes(classes)
        return True
    return False

def get_user(user_id):
    try:
        users = load_users()
        user = users.get(str(user_id))
        if user and is_user_blocked(user_id):
            user.is_blocked = True
        if user and not hasattr(user, 'hidden_buttons'):
            user.hidden_buttons = []
        return user
    except Exception as e:
        logger.error(f"Ошибка при загрузке пользователя {user_id}: {e}")
        return None

def save_user(user):
    users = load_users()
    users[user.user_id] = user
    return save_users(users)

def get_class_by_code(class_code):
    classes = load_classes()
    class_obj = classes.get(class_code)
    if class_obj and class_obj.is_active:
        return class_obj
    return None

def get_class_by_user(user_id):
    classes = load_classes()
    for class_obj in classes.values():
        if class_obj.is_active and str(user_id) in class_obj.students:
            return class_obj
    return None

def is_user_class_admin(user_id):
    classes = load_classes()
    for class_obj in classes.values():
        if class_obj.is_active and str(user_id) in class_obj.admins:
            return True
    return False

def is_user_class_creator(user_id, class_code):
    classes = load_classes()
    class_obj = classes.get(class_code)
    if class_obj and class_obj.is_active:
        return class_obj.creator_id == str(user_id)
    return False

def generate_personal_button_id():
    buttons = load_personal_buttons()
    while True:
        button_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        if button_id not in buttons:
            return button_id

def generate_class_code():
    classes = load_classes()
    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if code not in classes:
            return code

def generate_timer_id():
    timers = load_data(TIMERS_FILE, {})
    while True:
        timer_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        if timer_id not in timers:
            return timer_id

def generate_button_id():
    buttons = load_data(CUSTOM_BUTTONS_FILE, {})
    while True:
        button_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        if button_id not in buttons:
            return button_id

def generate_global_button_id():
    buttons = load_global_buttons()
    while True:
        button_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        if button_id not in buttons:
            return button_id

def generate_user_code():
    codes = load_user_codes()
    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        if code not in codes:
            return code

def get_personal_buttons(user_id):
    buttons_data = load_personal_buttons()
    user_buttons = []

    for button in buttons_data.values():
        if button.user_id == str(user_id) and button.is_active:
            user_buttons.append(button)

    user = get_user(user_id)
    if user and user.personal_button_order:
        ordered_buttons = []
        for button_id in user.personal_button_order:
            for button in user_buttons:
                if button.button_id == button_id:
                    ordered_buttons.append(button)
                    break
        for button in user_buttons:
            if button not in ordered_buttons:
                ordered_buttons.append(button)
        return ordered_buttons

    return sorted(user_buttons, key=lambda x: x.position)

def get_personal_buttons_count(user_id):
    buttons = get_personal_buttons(user_id)
    return len(buttons)

def save_personal_button(button):
    buttons = load_personal_buttons()
    buttons[button.button_id] = button
    return save_personal_buttons(buttons)

def delete_personal_button(button_id):
    buttons = load_personal_buttons()
    if button_id in buttons:
        del buttons[button_id]
        return save_personal_buttons(buttons)
    return False

def update_personal_button_order(user_id, button_order):
    user = get_user(user_id)
    if user:
        user.personal_button_order = button_order
        return save_user(user)
    return False

def get_class_custom_buttons(class_code):
    buttons = load_data(CUSTOM_BUTTONS_FILE, {})
    class_buttons = []
    for button_data in buttons.values():
        try:
            button = CustomButton.from_dict(button_data)
            if button.class_code == class_code and button.is_active:
                class_buttons.append(button)
        except Exception as e:
            logger.error(f"Ошибка при загрузке кастомной кнопки: {e}")
    return class_buttons

def get_global_buttons():
    buttons_data = load_global_buttons()
    return [btn for btn in buttons_data.values() if btn.is_active]

def get_user_custom_buttons_count(user_id, class_code, button_type="all"):
    buttons = get_class_custom_buttons(class_code)

    if button_type == "personal":
        return sum(1 for button in buttons if button.creator_id == str(user_id) 
                  and not button.name.startswith("CLASS_"))
    elif button_type == "class":
        return sum(1 for button in buttons if button.creator_id == str(user_id) 
                  and button.name.startswith("CLASS_"))
    else:
        return sum(1 for button in buttons if button.creator_id == str(user_id))

def delete_custom_button(button_id):
    buttons_data = load_data(CUSTOM_BUTTONS_FILE, {})
    if button_id in buttons_data:
        del buttons_data[button_id]
        return save_data(CUSTOM_BUTTONS_FILE, buttons_data)
    return False

def get_button_price(button_count):
    if button_count == 0:
        return 0
    elif button_count == 1:
        return PRICES['button_base']
    else:
        return PRICES['button_base'] + (button_count - 1) * PRICES['button_increment']

def add_stars_transaction(user_id, amount, description):
    """Меняет баланс виртуальных звёзд пользователя и обновляет агрегированную
    статистику трат.

    ИЗМЕНЕНО по требованию пользователя: подробная история переводов больше
    НЕ ведётся (никаких 'transactions'). Для отрицательных сумм обновляются
    только общая сумма трат и агрегат пользователя в топе — без истории."""
    user = get_user(user_id)
    if not user:
        return False

    user.stars_balance += amount
    if amount < 0:
        user.total_stars_spent -= amount

    save_user(user)

    if amount < 0:
        # Агрегированная статистика трат — без записи деталей перевода.
        register_stars_spending(user_id, user.first_name, -amount)

    return True

def get_user_by_code(code):
    codes = load_user_codes()
    return codes.get(code)

def save_user_code(code, user_id):
    codes = load_user_codes()
    codes[code] = str(user_id)
    return save_user_codes(codes)

# ==================================
# === СЕРВИСНЫЕ ФУНКЦИИ ===
# ==================================

def get_days_until_birthday(birthday_str):
    if not birthday_str:
        return None

    try:
        today = datetime.now().date()
        birthday = datetime.strptime(birthday_str, "%Y-%m-%d").date()

        next_birthday = birthday.replace(year=today.year)
        if next_birthday < today:
            next_birthday = next_birthday.replace(year=today.year + 1)

        return (next_birthday - today).days
    except Exception:
        return None

def calculate_timezone(user_time_str):
    try:
        utc_now = datetime.utcnow()
        user_time = datetime.strptime(user_time_str, "%H:%M").time()
        user_datetime = datetime.combine(utc_now.date(), user_time)
        time_diff = user_datetime - utc_now
        total_seconds = time_diff.total_seconds()
        hours_diff = total_seconds / 3600
        return int(round(hours_diff))
    except Exception as e:
        logger.error(f"Ошибка вычисления часового пояса: {e}")
        return 3

def get_local_time(user):
    utc_time = datetime.utcnow()
    return utc_time + timedelta(hours=user.timezone)

def get_bells_info(class_obj, user=None):
    if not class_obj or not class_obj.bells:
        return "🔔 Расписание звонков не установлено"

    # ИСПРАВЛЕНО: считаем «сейчас» по локальному времени пользователя, а
    # не по времени сервера, иначе после полуночи показывается «вчера».
    now = get_local_time(user) if user is not None else datetime.utcnow() + timedelta(hours=3)
    current_time = now.time()

    sorted_bells = sorted(class_obj.bells.items(), key=lambda x: datetime.strptime(x[1]['start'], "%H:%M").time())

    for lesson, times in sorted_bells:
        start_time = datetime.strptime(times['start'], "%H:%M").time()
        end_time = datetime.strptime(times['end'], "%H:%M").time()

        if start_time <= current_time <= end_time:
            time_left = datetime.combine(now.date(), end_time) - datetime.combine(now.date(), current_time)
            minutes, seconds = divmod(time_left.total_seconds(), 60)
            return f"🔔 Сейчас идёт {lesson} урок\n⏰ До конца: {int(minutes)} мин {int(seconds)} сек"

        if current_time < start_time:
            time_until = datetime.combine(now.date(), start_time) - datetime.combine(now.date(), current_time)
            minutes, seconds = divmod(time_until.total_seconds(), 60)
            return f"🔔 Следующий урок ({lesson}) через {int(minutes)} мин {int(seconds)} сек"

    return "🔔 Уроки на сегодня закончились"

def get_holidays_count(class_obj, user=None):
    if not class_obj or not class_obj.holidays:
        return "📅 Даты каникулы не установлены"

    try:
        holiday_date = datetime.strptime(class_obj.holidays, "%Y-%m-%d").date()
        # ИСПРАВЛЕНО: «сегодня» — по локальному времени пользователя, чтобы
        # около полуночи и в часовых поясах !=UTC день не сдвигался.
        if user is not None:
            today = get_local_time(user).date()
        else:
            today = (datetime.utcnow() + timedelta(hours=3)).date()
        days_left = (holiday_date - today).days

        if days_left > 0:
            return f"🎉 До каникул осталось {days_left} дней!"
        elif days_left == 0:
            return "🎉 Каникулы начинаются сегодня!"
        else:
            return "🎉 Каникулы уже начались!"
    except Exception:
        return "Неверный формат даты каникул"

def get_day_name(day_index):
    days_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    return days_ru[day_index]

def get_day_schedule(class_obj, day_name):
    if not class_obj or not class_obj.schedule:
        return "Расписание не установлено"

    schedule = class_obj.schedule.get(day_name)
    if not schedule:
        return f"На {day_name} расписание не установлено"

    return schedule

def get_homework_for_date(class_obj, date_str):
    if not class_obj or not class_obj.homework:
        return None

    homework_for_date = {}
    for subject, assignments in class_obj.homework.items():
        filtered = [a for a in assignments if a.get('date') == date_str]
        if filtered:
            homework_for_date[subject] = filtered

    return homework_for_date if homework_for_date else None

def format_homework(homework_dict, date_str=None):
    """Форматирует домашние задания.

    Если ``date_str`` задан (например, "сегодня" / "завтра" / "2025-09-10") —
    выводится один блок с этой датой в шапке (используется в кнопках
    «📅 Сегодня» / «📅 Завтра», когда ДЗ заранее отфильтровано
    `get_homework_for_date`).

    Если ``date_str`` НЕ задан — в кнопке «📝 Домашнее задание» показываются
    ВСЕ задания, СГРУППИРОВАННЫЕ ПО ДАТЕ. По требованию пользователя:
    дата стоит «сверху» на каждый день, и в каждом блоке мы показываем
    ТОЛЬКО дату (без поля «Добавлено»), а под ней — список заданий по
    предметам. Дни отсортированы хронологически; задания без даты
    выводятся в самом конце под заголовком «Без даты».
    """
    if not homework_dict:
        return "📝 Домашнее задание не задано."

    # Режим «отфильтровано извне» — стандартный вывод одной шапки.
    if date_str:
        text = f"📝 **Домашнее задание на {date_str}:**\n\n"
        for subject, assignments in homework_dict.items():
            text += f"**{subject}:**\n"
            for assignment in assignments:
                text += f"• {assignment.get('text', '')}\n"
            text += "\n"
        return text

    # Режим «все ДЗ» — группируем по дате (только дата сверху каждого блока).
    grouped = {}
    for subject, assignments in homework_dict.items():
        for assignment in assignments:
            due = (assignment.get('date') or '').strip()
            grouped.setdefault(due or '', {}).setdefault(subject, []).append(
                assignment.get('text', '')
            )

    if not grouped:
        return "📝 Домашнее задание не задано."

    def _sort_key(d):
        # Пустая дата — в самом конце; всё остальное — по возрастанию даты.
        if not d:
            return (1, "9999-12-31")
        try:
            datetime.strptime(d, "%Y-%m-%d")
            return (0, d)
        except Exception:
            return (0, d)

    text = "📝 **Домашнее задание:**\n\n"
    for due in sorted(grouped.keys(), key=_sort_key):
        # Дата сверху, без всяких «На дату:»/«Добавлено:».
        text += f"📅 **{due if due else 'Без даты'}**\n"
        for subject, items in grouped[due].items():
            text += f"**{subject}:**\n"
            for item in items:
                text += f"• {item}\n"
        text += "\n"

    return text

def check_class_limit(class_code):
    class_obj = get_class_by_code(class_code)
    if class_obj:
        return len(class_obj.students) < 40
    return False

# ==================================
# === КЛАВИАТУРЫ ===
# ==================================

def get_admin_panel_keyboard():
    keyboard = [
        [InlineKeyboardButton("📢 Отправить сообщение классу", callback_data="send_class_message")],
        [InlineKeyboardButton("📅 Редактировать расписание", callback_data="edit_schedule")],
        [InlineKeyboardButton("👨‍🏫 Редактировать учителей", callback_data="edit_teachers")],
        [InlineKeyboardButton("🔔 Редактировать звонки", callback_data="edit_bells")],
        [InlineKeyboardButton("🎉 Установить каникулы", callback_data="set_holidays")],
        [InlineKeyboardButton("👥 Управление админами", callback_data="manage_admins")],
        [InlineKeyboardButton("🆕 Управление кнопками", callback_data="manage_custom_buttons")],
        [InlineKeyboardButton("📝 Управление ДЗ", callback_data="manage_homework")],
        [InlineKeyboardButton("👤 Управление учениками", callback_data="manage_class_users")],
        [InlineKeyboardButton("🔑 Показать код класса", callback_data="show_class_code_admin")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]])

def get_back_button_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]])

def get_quick_timer_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("5 мин", callback_data="quick_timer_5"),
            InlineKeyboardButton("10 мин", callback_data="quick_timer_10"),
            InlineKeyboardButton("15 мин", callback_data="quick_timer_15"),
        ],
        [
            InlineKeyboardButton("30 мин", callback_data="quick_timer_30"),
            InlineKeyboardButton("1 час", callback_data="quick_timer_60"),
            InlineKeyboardButton("2 часа", callback_data="quick_timer_120"),
        ],
        [
            InlineKeyboardButton("3 часа", callback_data="quick_timer_180"),
            InlineKeyboardButton("6 часов", callback_data="quick_timer_360"),
            InlineKeyboardButton("12 часов", callback_data="quick_timer_720"),
        ],
        [
            InlineKeyboardButton("1 день", callback_data="quick_timer_1440"),
            InlineKeyboardButton("2 дня", callback_data="quick_timer_2880"),
            InlineKeyboardButton("7 дней", callback_data="quick_timer_10080"),
        ],
        [InlineKeyboardButton("📅 Указать дату и время", callback_data="timer_custom_datetime")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_week_schedule_keyboard():
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    keyboard = []

    for i, day in enumerate(days):
        keyboard.append([InlineKeyboardButton(day, callback_data=f"schedule_day_{i}")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(keyboard)

def get_schedule_edit_keyboard():
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    keyboard = []
    for i, day in enumerate(days):
        keyboard.append([InlineKeyboardButton(day, callback_data=f"edit_schedule_day_{i}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin")])
    return InlineKeyboardMarkup(keyboard)

def get_teachers_edit_keyboard(class_obj):
    keyboard = []
    for subject in class_obj.teachers.keys():
        keyboard.append([InlineKeyboardButton(subject, callback_data=f"edit_teacher_{subject}")])
    keyboard.append([InlineKeyboardButton("➕ Добавить предмет", callback_data="add_teacher_subject")])
    if class_obj.teachers:
        keyboard.append([InlineKeyboardButton("🗑️ Удалить учителя/предмет", callback_data="delete_teacher_list")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin")])
    return InlineKeyboardMarkup(keyboard)

def get_bells_edit_keyboard(class_obj):
    keyboard = []
    for lesson_num, times in class_obj.bells.items():
        text = f"{lesson_num} урок: {times['start']} - {times['end']}"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"edit_bell_{lesson_num}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_management_keyboard(class_obj):
    keyboard = []
    regular_admins = [admin_id for admin_id in class_obj.admins if admin_id != class_obj.creator_id]

    for admin_id in class_obj.admins:
        user = get_user(admin_id)
        if user:
            name = user.first_name or f"User {admin_id}"
            if admin_id == class_obj.creator_id:
                name = f"👑 {name} (Создатель)"
                keyboard.append([InlineKeyboardButton(name, callback_data=f"admin_info_{admin_id}")])
            else:
                name = f"➖ {name}"
                keyboard.append([InlineKeyboardButton(name, callback_data=f"remove_admin_{admin_id}")])

    for user_id in class_obj.students:
        user = get_user(user_id)
        if user and user_id not in class_obj.admins and user_id not in class_obj.blocked_users:
            name = user.first_name or f"User {user_id}"
            if len(regular_admins) < 2:
                name = f"➕ {name}"
                keyboard.append([InlineKeyboardButton(name, callback_data=f"add_admin_{user_id}")])
            else:
                name = f"⛔ {name} (лимит)"
                keyboard.append([InlineKeyboardButton(name, callback_data="limit_reached")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin")])
    return InlineKeyboardMarkup(keyboard)

def get_custom_buttons_management_keyboard(class_obj):
    keyboard = []
    custom_buttons = get_class_custom_buttons(class_obj.class_code)
    for button in custom_buttons:
        keyboard.append([InlineKeyboardButton(f"📝 {button.name}", callback_data=f"edit_button_{button.button_id}")])

    keyboard.append([InlineKeyboardButton("➕ Добавить кнопку", callback_data="add_custom_button")])
    keyboard.append([InlineKeyboardButton("🗑️ Удалить кнопки", callback_data="admin_delete_buttons")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_delete_buttons_keyboard(class_obj):
    keyboard = []
    custom_buttons = get_class_custom_buttons(class_obj.class_code)
    for button in custom_buttons:
        button_name = button.name.replace("CLASS_", "") if button.name.startswith("CLASS_") else button.name
        creator = get_user(button.creator_id)
        creator_name = creator.first_name if creator else "Неизвестно"

        if button.name.startswith("CLASS_"):
            text = f"👥 {button_name} (от {creator_name})"
        else:
            text = f"👤 {button_name} (личная, от {creator_name})"

        keyboard.append([InlineKeyboardButton(text, callback_data=f"admin_delete_button_{button.button_id}")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_custom_buttons")])
    return InlineKeyboardMarkup(keyboard)

def get_button_type_keyboard(include_cancel=True):
    keyboard = [
        [InlineKeyboardButton("📝 Текстовая кнопка", callback_data="button_type_text")],
        [InlineKeyboardButton("🔗 Ссылка", callback_data="button_type_url")]
    ]
    if include_cancel:
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_button_creation_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Личная кнопка (только для меня)", callback_data="button_for_personal")],
        [InlineKeyboardButton("👥 Кнопка для класса (для всех)", callback_data="button_for_class")],
        [InlineKeyboardButton("❌ Отмена", callback_data="back_to_admin")]
    ])

def get_homework_delete_keyboard(class_obj):
    keyboard = []
    for subject, assignments in class_obj.homework.items():
        for i, assignment in enumerate(assignments):
            date_str = assignment.get('date', 'Без даты')
            text = f"{subject} ({date_str}): {assignment['text'][:30]}..."
            keyboard.append([InlineKeyboardButton(f"👁 {text}", callback_data=f"preview_hw_{subject}_{i}")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin")])
    return InlineKeyboardMarkup(keyboard)

def get_classes_keyboard(classes, action_prefix):
    keyboard = []
    for class_code, class_obj in classes.items():
        if class_obj.is_active:
            keyboard.append([InlineKeyboardButton(
                f"{class_obj.class_name} ({class_code})",
                callback_data=f"{action_prefix}_{class_code}"
            )])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")])
    return InlineKeyboardMarkup(keyboard)

def get_homework_management_keyboard(class_obj):
    keyboard = [
        [InlineKeyboardButton("➕ Добавить ДЗ", callback_data="add_homework")],
        [InlineKeyboardButton("🗑️ Удалить ДЗ", callback_data="delete_homework")],
        [InlineKeyboardButton("👀 Посмотреть ДЗ", callback_data="view_homework")]
    ]

    if class_obj and class_obj.subjects:
        keyboard.append([InlineKeyboardButton("📚 Быстрое добавление:", callback_data="subjects_header")])
        row = []
        for i, subject in enumerate(class_obj.subjects):
            row.append(InlineKeyboardButton(subject, callback_data=f"quick_hw_{subject}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="back_to_admin")])
    return InlineKeyboardMarkup(keyboard)

def get_personal_buttons_keyboard(user):
    buttons = get_personal_buttons(user.user_id)
    keyboard = []

    if buttons:
        for button in buttons:
            keyboard.append([InlineKeyboardButton(f"📝 {button.name}", callback_data=f"personal_button_{button.button_id}")])

    keyboard.append([InlineKeyboardButton("➕ Добавить личную кнопку", callback_data="create_personal_button")])
    keyboard.append([InlineKeyboardButton("⚙️ Управление кнопками", callback_data="manage_personal_buttons")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")])

    return InlineKeyboardMarkup(keyboard)

def get_personal_buttons_management_keyboard(user):
    buttons = get_personal_buttons(user.user_id)
    keyboard = []

    if buttons:
        for button in buttons:
            keyboard.append([InlineKeyboardButton(f"✏️ {button.name}", callback_data=f"edit_personal_button_{button.button_id}")])

    keyboard.append([InlineKeyboardButton("➕ Добавить кнопку", callback_data="create_personal_button")])

    if buttons:
        keyboard.append([InlineKeyboardButton("🔄 Изменить порядок", callback_data="reorder_personal_buttons")])
        keyboard.append([InlineKeyboardButton("📋 Изменить ряд", callback_data="change_button_row")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_personal_buttons")])
    return InlineKeyboardMarkup(keyboard)

def get_personal_button_delete_keyboard(user, button_id):
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete_personal_button_{button_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_delete_personal_button_{button_id}")
        ]
    ]

    return InlineKeyboardMarkup(keyboard)

def get_class_users_management_keyboard(class_obj):
    keyboard = []

    if class_obj.blocked_users:
        keyboard.append([InlineKeyboardButton("🚫 ЗАБЛОКИРОВАННЫЕ:", callback_data="blocked_header")])

        for user_id in class_obj.blocked_users:
            user = get_user(user_id)
            if user:
                name = user.first_name or f"User {user_id}"
                keyboard.append([InlineKeyboardButton(f"✅ {name} (разблокировать)", callback_data=f"class_unblock_{user_id}")])
    else:
        keyboard.append([InlineKeyboardButton("🚫 Нет заблокированных", callback_data="no_blocked")])

    active_students = []
    for user_id in class_obj.students:
        if user_id not in class_obj.blocked_users and user_id not in class_obj.admins:
            active_students.append(user_id)

    if active_students:
        keyboard.append([InlineKeyboardButton("👥 АКТИВНЫЕ УЧЕНИКИ:", callback_data="active_header")])

        for user_id in active_students:
            user = get_user(user_id)
            if user:
                name = user.first_name or f"User {user_id}"
                keyboard.append([InlineKeyboardButton(f"🚫 {name} (заблокировать)", callback_data=f"class_block_{user_id}")])
    else:
        keyboard.append([InlineKeyboardButton("👥 Нет активных учеников", callback_data="no_active")])

    if class_obj.admins:
        keyboard.append([InlineKeyboardButton("👨‍💼 АДМИНИСТРАТОРЫ:", callback_data="admin_header")])

        for admin_id in class_obj.admins:
            user = get_user(admin_id)
            if user:
                role = "👑 Создатель" if admin_id == class_obj.creator_id else "👨‍💼 Админ"
                name = f"{user.first_name} ({role})"
                keyboard.append([InlineKeyboardButton(name, callback_data="no_action")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_admin")])

    return InlineKeyboardMarkup(keyboard)

def get_stars_keyboard(user, show_back=True):
    class_obj = get_class_by_user(user.user_id)
    is_class_blocked = class_obj and is_user_class_blocked(user.user_id, class_obj.class_code)

    refs_count = getattr(user, 'referrals_count', 0)
    keyboard = [
        [InlineKeyboardButton(f"💰 Баланс: {user.stars_balance} ⭐", callback_data="stars_balance")],
        [InlineKeyboardButton("💎 Купить звезды (Telegram Stars)", callback_data="buy_stars")],
        # ПУНКТ 2: реферальная система.
        [InlineKeyboardButton(
            f"🔗 Поделиться ссылкой (+{REFERRAL_REWARD_STARS}⭐ за друга) · {refs_count}",
            callback_data="referral_share"
        )],
    ]

    if user.is_blocked or is_class_blocked:
        keyboard.append([InlineKeyboardButton("🔓 Разблокироваться", callback_data="unblock_self")])

    keyboard.append([InlineKeyboardButton("📊 Статистика звезд", callback_data="stars_stats")])

    if show_back:
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")])

    return InlineKeyboardMarkup(keyboard)

def get_stars_prices_keyboard(user=None):
    """Прейскурант покупки внутренних звёзд за Telegram Stars (XTR).

    user необязательный: если передан — применим 3-цветную разметку
    (🟢 для покупки, 🔴 для отмены).
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("100 ⭐ - 100 XTR", callback_data="buy_stars_100")],
        [InlineKeyboardButton("200 ⭐ - 200 XTR", callback_data="buy_stars_200")],
        [InlineKeyboardButton("500 ⭐ - 500 XTR", callback_data="buy_stars_500")],
        [InlineKeyboardButton("1000 ⭐ - 1000 XTR", callback_data="buy_stars_1000")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]
    ])

# ==================================
# === УПРАВЛЕНИЕ ВИДИМОСТЬЮ КНОПОК ===
# ==================================

ALL_MAIN_MENU_BUTTONS = [
    "📅 Сегодня",
    "📅 Завтра",
    "📅 Расписание",
    "📝 Домашнее задание",
    "🔔 Звонки",
    "👨‍🏫 Учителя",
    "🎉 Каникулы",
    "💬 Написать админу",
    "⏰ Таймер",
    "🕵️ Анонимное сообщение",
    "📨 Мои анонимные сообщения",
    "🎓 Управление классами",
    "🌟 Мои кнопки",
    "⭐ Звезды",
    "🤖 DEVORKS+ai",
    "🪄 Автоматизация",
    "🌦 Погода",
    "📚 Инструкция",
    "🔑 Код класса",
    "🚪 Выйти из класса",
    "🔓 Выйти из аккаунта"
]

# Быстрые команды, которые должны работать ИЗ ЛЮБОГО состояния (не только из
# главного меню). Используется фильтром QUICK_COMMANDS_FILTER в main().
QUICK_COMMANDS = [
    "📅 Сегодня", "📅 Завтра", "📅 Расписание",
    "📝 Домашнее задание", "➕ Добавить ДЗ", "🗑️ Удалить ДЗ",
    "🔔 Звонки", "👨‍🏫 Учителя", "🎉 Каникулы",
    "💬 Написать админу", "📢 Написать классу",
    "⏰ Таймер", "🕵️ Анонимное сообщение", "📨 Мои анонимные сообщения",
    "🎓 Управление классами", "⚙️ Настройки", "🌟 Мои кнопки",
    "⭐ Звезды", "🤖 DEVORKS+ai", "🪄 Автоматизация",
    "🌦 Погода", "📚 Инструкция", "🔑 Код класса",
    "💬 Чат поддержки", "👨‍💼 Админская панель", "⬅️ Назад в меню",
]


def _safe_cb(prefix, name, max_bytes=64):
    """Обрезает имя кнопки так, чтобы callback_data не превышал 64 байта (лимит Telegram)."""
    prefix_bytes = len(prefix.encode('utf-8'))
    available = max_bytes - prefix_bytes
    if available <= 0:
        return prefix[:64]
    encoded = name.encode('utf-8')
    if len(encoded) <= available:
        return prefix + name
    truncated = encoded[:available].decode('utf-8', errors='ignore')
    return prefix + truncated

def get_all_user_button_names(user):
    """ПУНКТ 4: возвращает список ВСЕХ доступных пользователю кнопок:
    стандартные + личные + глобальные + классные. Используется для
    управления видимостью, переименованием и порядком."""
    names = list(ALL_MAIN_MENU_BUTTONS)
    # Личные кнопки
    try:
        for b in get_personal_buttons(user.user_id):
            if b.name not in names:
                names.append(b.name)
    except Exception:
        pass
    # Глобальные кнопки
    try:
        for b in get_global_buttons():
            if b.name not in names:
                names.append(b.name)
    except Exception:
        pass
    # Кнопки класса
    try:
        class_obj = get_class_by_user(user.user_id)
        if class_obj:
            for b in get_class_custom_buttons(class_obj.class_code):
                display = b.name.replace("CLASS_", "") if b.name.startswith("CLASS_") else b.name
                if display not in names:
                    names.append(display)
    except Exception:
        pass
    return names


def get_button_visibility_keyboard(user):
    keyboard = []
    hidden = getattr(user, 'hidden_buttons', [])

    for button_name in get_all_user_button_names(user):
        is_hidden = button_name in hidden
        icon = "👁" if not is_hidden else "🙈"
        status = "видна" if not is_hidden else "скрыта"
        cb_data = _safe_cb("toggle_visibility_", button_name)
        keyboard.append([InlineKeyboardButton(
            f"{icon} {button_name} ({status})", 
            callback_data=cb_data
        )])

    keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="finish_visibility")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(keyboard)

def _apply_user_button_settings(user, raw_button_names):
    """ПУНКТ 4: применяет к списку имён кнопок настройки пользователя:
    - удаляет скрытые
    - переименовывает по карте user.custom_buttons {старое: новое}
    Возвращает список отображаемых имён (в том же порядке)."""
    hidden = getattr(user, 'hidden_buttons', []) or []
    rename_map = getattr(user, 'custom_buttons', {}) or {}
    out = []
    for name in raw_button_names:
        if name in hidden:
            continue
        out.append(rename_map.get(name, name))
    return out


def _layout_chunk(items, layout):
    """Раскладывает список кнопок на ряды согласно layout."""
    if layout == 'compact':
        per_row = 3
    elif layout == 'wide':
        per_row = 1
    else:
        per_row = 2
    rows = []
    for i in range(0, len(items), per_row):
        rows.append(items[i:i + per_row])
    return rows


def get_main_menu_keyboard(user):
    """ПУНКТ 4: учитывает hidden_buttons, custom_buttons (переименование),
    button_layout (расположение) и custom_button_order (порядок) для ВСЕХ кнопок,
    включая личные/глобальные/классные."""
    class_obj = get_class_by_user(user.user_id)
    is_blocked_in_class = class_obj and is_user_class_blocked(user.user_id, class_obj.class_code)
    is_admin = class_obj and str(user.user_id) in class_obj.admins and not is_blocked_in_class

    hidden_buttons = getattr(user, 'hidden_buttons', []) or []
    rename_map = getattr(user, 'custom_buttons', {}) or {}
    layout = getattr(user, 'button_layout', 'default') or 'default'
    custom_order = getattr(user, 'custom_button_order', []) or []

    # Собираем все стандартные кнопки в один плоский список.
    # Кнопка «📅 Расписание» открывает экран выбора дня недели и показывает
    # полное расписание класса по выбранному дню (используется существующий
    # хендлер handle_week_schedule).
    # ПУНКТ 9: «📨 Мои анонимные сообщения» отрисовывается отдельной строкой
    # (см. ниже), чтобы кнопка была ровной и не сжималась рядом с короткими.
    standard_buttons = [
        "📅 Сегодня", "📅 Завтра", "📅 Расписание",
        "📝 Домашнее задание",
        "🔔 Звонки", "👨‍🏫 Учителя",
        "🎉 Каникулы", "💬 Написать админу",
        "⏰ Таймер", "🕵️ Анонимное сообщение",
        "🎓 Управление классами",
        "⚙️ Настройки", "🌟 Мои кнопки",
        "⭐ Звезды", "🤖 DEVORKS+ai",
        "🪄 Автоматизация",
        "🌦 Погода",
        # ПУНКТ 3: чат поддержки прямо из главного меню (всегда доступен).
        "💬 Чат поддержки",
        "📚 Инструкция", "🔑 Код класса",
    ]
    # «📨 Мои анонимные сообщения» — отдельный список, чтобы её можно было
    # положить в отдельный ряд (Item #9). Здесь же она проходит фильтрацию
    # hidden_buttons / custom_buttons.
    my_anon_button = "📨 Мои анонимные сообщения"

    # Применяем сохранённый порядок (если есть)
    if custom_order:
        ordered = [b for b in custom_order if b in standard_buttons]
        # Кнопки, отсутствующие в custom_order — в конец
        for b in standard_buttons:
            if b not in ordered:
                ordered.append(b)
        standard_buttons = ordered

    # Фильтруем скрытые и применяем переименования
    standard_visible = _apply_user_button_settings(user, standard_buttons)

    keyboard = []

    # Личные кнопки пользователя — учитывают hide/rename
    personal_buttons = get_personal_buttons(user.user_id)
    personal_names = [b.name for b in personal_buttons]
    personal_visible = _apply_user_button_settings(user, personal_names)
    for row in _layout_chunk(personal_visible, layout):
        keyboard.append(row)

    # Глобальные кнопки — учитывают hide/rename
    global_buttons = get_global_buttons()
    global_names = [b.name for b in global_buttons]
    global_visible = _apply_user_button_settings(user, global_names)
    for row in _layout_chunk(global_visible, layout):
        keyboard.append(row)

    # Кнопки класса — учитывают hide/rename
    if class_obj and not is_blocked_in_class:
        class_buttons = get_class_custom_buttons(class_obj.class_code)
        class_names = []
        for button in class_buttons:
            if button.name.startswith("CLASS_"):
                class_names.append(button.name.replace("CLASS_", ""))
            elif button.creator_id == user.user_id:
                class_names.append(button.name)
        class_visible = _apply_user_button_settings(user, class_names)
        for row in _layout_chunk(class_visible, layout):
            keyboard.append(row)

    if is_admin:
        # Админские быстрые действия (тоже фильтруем по скрытым)
        admin_extra = ["➕ Добавить ДЗ", "🗑️ Удалить ДЗ"]
        admin_visible = _apply_user_button_settings(user, admin_extra)
        for row in _layout_chunk(admin_visible, layout):
            keyboard.append(row)
        if "📢 Написать классу" not in hidden_buttons:
            keyboard.append([rename_map.get("📢 Написать классу", "📢 Написать классу")])

    # Стандартные кнопки — раскладываем по layout
    for row in _layout_chunk(standard_visible, layout):
        keyboard.append(row)

    # ПУНКТ 9: «📨 Мои анонимные сообщения» — отдельным рядом во всю ширину,
    # чтобы кнопка была ровной независимо от выбранного layout.
    if my_anon_button not in hidden_buttons:
        keyboard.append([rename_map.get(my_anon_button, my_anon_button)])

    if is_admin and "👨‍💼 Админская панель" not in hidden_buttons:
        keyboard.append([rename_map.get("👨‍💼 Админская панель", "👨‍💼 Админская панель")])

    if user.user_id == DEVELOPER_ID and "🛠️ Панель разработчика" not in hidden_buttons:
        keyboard.append([rename_map.get("🛠️ Панель разработчика", "🛠️ Панель разработчика")])

    if class_obj and not is_blocked_in_class:
        if "🚪 Выйти из класса" not in hidden_buttons:
            keyboard.append([rename_map.get("🚪 Выйти из класса", "🚪 Выйти из класса")])

    if "🔓 Выйти из аккаунта" not in hidden_buttons:
        keyboard.append([rename_map.get("🔓 Выйти из аккаунта", "🔓 Выйти из аккаунта")])

    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_user_button_reverse_map(user):
    """Возвращает обратное отображение «отображаемое имя» → «оригинальное имя».
    ПУНКТ 4: чтобы handle_main_menu корректно ловил переименованные кнопки."""
    rename_map = getattr(user, 'custom_buttons', {}) or {}
    return {v: k for k, v in rename_map.items()}

def get_quick_admin_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ Добавить ДЗ", "🗑️ Удалить ДЗ"],
        ["📢 Написать классу"],
        ["⬅️ Назад в меню"]
    ], resize_keyboard=True)

def get_move_button_keyboard(button, user_buttons):
    keyboard = []

    current_pos = -1
    for i, btn in enumerate(user_buttons):
        if btn.button_id == button.button_id:
            current_pos = i
            break

    if current_pos == -1:
        return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_manage_personal_buttons")]])

    nav_row = []
    if current_pos > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Влево", callback_data=f"move_btn_left_{button.button_id}"))

    nav_row.append(InlineKeyboardButton(f"📍 {current_pos + 1}/{len(user_buttons)}", callback_data="current_pos"))

    if current_pos < len(user_buttons) - 1:
        nav_row.append(InlineKeyboardButton("Вправо ➡️", callback_data=f"move_btn_right_{button.button_id}"))

    keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("📋 Изменить ряд", callback_data=f"change_row_{button.button_id}")])

    keyboard.append([
        InlineKeyboardButton("✅ Готово", callback_data="finish_move"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")
    ])

    return InlineKeyboardMarkup(keyboard)

def get_row_selection_keyboard(button_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1 ряд", callback_data=f"set_row_{button_id}_1")],
        [InlineKeyboardButton("2 ряд", callback_data=f"set_row_{button_id}_2")],
        [InlineKeyboardButton("3 ряд", callback_data=f"set_row_{button_id}_3")],
        [InlineKeyboardButton("4 ряд", callback_data=f"set_row_{button_id}_4")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"back_to_move_{button_id}")]
    ])

def get_reorder_rows_keyboard(user):
    buttons = get_personal_buttons(user.user_id)

    rows = {1: [], 2: [], 3: [], 4: []}
    for btn in buttons:
        row = getattr(btn, 'row', 1)
        if row in rows:
            rows[row].append(btn)

    keyboard = []
    for row_num in [1, 2, 3, 4]:
        if rows[row_num]:
            btn_names = [b.name[:15] for b in rows[row_num][:2]]
            display_text = f"📋 Ряд {row_num}: " + ", ".join(btn_names)
        else:
            display_text = f"📋 Ряд {row_num}: (пусто)"

        if rows[row_num]:
            if row_num == 1:
                if rows[2]:
                    keyboard.append([
                        InlineKeyboardButton(f"⬇️ {display_text}", callback_data=f"move_row_down_{row_num}")
                    ])
                else:
                    keyboard.append([InlineKeyboardButton(display_text, callback_data="no_action")])
            elif row_num == 4:
                if rows[3]:
                    keyboard.append([
                        InlineKeyboardButton(f"⬆️ {display_text}", callback_data=f"move_row_up_{row_num}")
                    ])
                else:
                    keyboard.append([InlineKeyboardButton(display_text, callback_data="no_action")])
            else:
                up_possible = rows[row_num - 1]
                down_possible = rows[row_num + 1]

                if up_possible and down_possible:
                    keyboard.append([
                        InlineKeyboardButton(f"⬆️⬇️ {display_text}", callback_data=f"move_row_updown_{row_num}")
                    ])
                elif up_possible:
                    keyboard.append([
                        InlineKeyboardButton(f"⬆️ {display_text}", callback_data=f"move_row_up_{row_num}")
                    ])
                elif down_possible:
                    keyboard.append([
                        InlineKeyboardButton(f"⬇️ {display_text}", callback_data=f"move_row_down_{row_num}")
                    ])
                else:
                    keyboard.append([InlineKeyboardButton(display_text, callback_data="no_action")])
        else:
            keyboard.append([InlineKeyboardButton(display_text, callback_data="no_action")])

    keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="finish_reorder")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_personal_buttons")])

    return InlineKeyboardMarkup(keyboard)

def get_row_reorder_keyboard(row_num):
    keyboard = []

    if row_num > 1:
        keyboard.append([InlineKeyboardButton("⬆️ Вверх (поменять с рядом выше)", callback_data=f"move_row_up_{row_num}")])
    if row_num < 4:
        keyboard.append([InlineKeyboardButton("⬇️ Вниз (поменять с рядом ниже)", callback_data=f"move_row_down_{row_num}")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_reorder_rows")])

    return InlineKeyboardMarkup(keyboard)

def get_anonymous_messages_keyboard(user_id):
    """Список входящих анонимок + действия (очистить/удалить/купить место).

    Возвращает None, только если у пользователя совсем нет анонимок —
    тогда вызывающий код показывает «📭 У вас нет анонимных сообщений.».
    Когда сообщения есть, помимо самих сообщений всегда добавляем
    кнопки массовой/одиночной очистки и покупки «места», чтобы
    пользователь мог управлять занимаемым местом.
    """
    anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})

    received_messages = []
    for msg_id, msg in anonymous_messages.items():
        if str(msg.get('to_user_id')) == str(user_id):
            received_messages.append((msg_id, msg))

    if not received_messages:
        return None

    keyboard = []
    for msg_id, msg in received_messages[-10:]:
        sender_status = "👁" if msg.get('sender_viewed', False) else "🕵️"
        timestamp = msg.get('timestamp', 'Неизвестно')
        preview = msg.get('message', '')[:20] + "..."
        keyboard.append([InlineKeyboardButton(
            f"{sender_status} {timestamp}: {preview}",
            callback_data=f"view_anon_msg_{msg_id}"
        )])

    # Действия над всем списком. Эти кнопки появились после жалобы
    # «копится мусор, не могу почистить»: теперь массовая очистка,
    # удаление по одному и покупка «места» собраны в одном экране.
    keyboard.append([InlineKeyboardButton(
        "🗑️ Очистить все",
        callback_data="anon_clear_all",
    )])
    keyboard.append([InlineKeyboardButton(
        "🗂 Удалить по одному",
        callback_data="anon_delete_mode",
    )])
    keep_price = PRICES.get('anon_keep_month', 100)
    keyboard.append([InlineKeyboardButton(
        f"💎 Купить место ({keep_price}⭐ / мес)",
        callback_data="anon_buy_space",
    )])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(keyboard)


def get_anon_delete_mode_keyboard(user_id):
    """Список с кнопками удаления каждого входящего анонимного сообщения.

    Используется в режиме «удалить по одному» — каждая строка несёт
    отдельный callback `anon_del_<msg_id>`, и под списком есть кнопка
    «⬅️ Назад» для возврата в основной список.
    """
    anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})

    received = []
    for msg_id, msg in anonymous_messages.items():
        if str(msg.get('to_user_id')) == str(user_id):
            received.append((msg_id, msg))

    if not received:
        return None

    keyboard = []
    for msg_id, msg in received[-10:]:
        timestamp = msg.get('timestamp', 'Неизвестно')
        preview = msg.get('message', '')[:18] + "..."
        keyboard.append([InlineKeyboardButton(
            f"🗑 {timestamp}: {preview}",
            callback_data=f"anon_del_{msg_id}",
        )])

    keyboard.append([InlineKeyboardButton("⬅️ Назад к списку", callback_data="view_anon_list")])
    return InlineKeyboardMarkup(keyboard)


def get_anon_clear_confirm_keyboard():
    """Подтверждение массовой очистки всех входящих анонимок.

    Подтверждение нужно потому, что операция необратимая — после неё
    пользователь уже не сможет прочесть, кто и что писал, даже за
    «узнать отправителя».
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, удалить все", callback_data="anon_clear_yes")],
        [InlineKeyboardButton("❌ Отмена", callback_data="anon_clear_no")],
    ])


def get_anon_buy_space_keyboard():
    """Выбор способа оплаты «места» под анонимки: виртуальные ⭐ или XTR."""
    price = PRICES.get('anon_keep_month', 100)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"💎 {price} ⭐ (виртуальные) / месяц",
            callback_data="anon_buy_space_virt",
        )],
        [InlineKeyboardButton(
            f"⭐ {price} XTR (Telegram Stars) / месяц",
            callback_data="anon_buy_space_xtr",
        )],
        [InlineKeyboardButton("⬅️ Назад", callback_data="view_anon_list")],
    ])

def get_settings_keyboard(user=None):
    """Главная клавиатура настроек."""
    keyboard = [
        [InlineKeyboardButton("⏰ Изменить время", callback_data="change_time")],
        [InlineKeyboardButton("✏️ Изменить названия кнопок", callback_data="change_buttons")],
        [InlineKeyboardButton("🔄 Изменить расположение", callback_data="change_layout")],
        [InlineKeyboardButton("🔄 Переместить кнопки", callback_data="move_buttons")],
        [InlineKeyboardButton("👁 Скрыть/показать кнопки", callback_data="manage_visibility")],
        [InlineKeyboardButton("🔑 Код класса", callback_data="show_class_code")],
        [InlineKeyboardButton("🎂 Настройки дня рождения", callback_data="birthday_settings")],
        [InlineKeyboardButton("🔔 Настройки уведомлений", callback_data="notification_settings")],
        # === Погодные / праздничные настройки (новое) ===
        [InlineKeyboardButton("🌦 Настройки погоды", callback_data="weather_settings")],
        [InlineKeyboardButton("🎉 Настройки праздников", callback_data="holiday_settings")],
        [InlineKeyboardButton("🌟 Мои кнопки", callback_data="personal_buttons")],
        [InlineKeyboardButton("💡 Предложить функцию", callback_data="suggest_function")],
        # ПУНКТ 3: чат поддержки доступен из настроек (помимо главного меню).
        [InlineKeyboardButton("💬 Чат поддержки", callback_data="open_support_chat")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_weather_settings_keyboard(user):
    """Клавиатура настроек погоды: вкл/выкл, время утреннего уведомления, город."""
    enabled = getattr(user, 'weather_notifications', True)
    toggle_label = "🔕 Выключить уведомления о погоде" if enabled else "🔔 Включить уведомления о погоде"
    weather_time = getattr(user, 'weather_notification_time', '07:00')
    city = getattr(user, 'city', None) or "не выбран"
    tz_raw = getattr(user, 'timezone', 3)
    try:
        tz_num = float(tz_raw)
        tz_label = f"UTC{'+' if tz_num >= 0 else '−'}{abs(int(tz_num)) if tz_num == int(tz_num) else abs(tz_num)}"
    except (TypeError, ValueError):
        tz_label = f"UTC{tz_raw}"
    keyboard = [
        [InlineKeyboardButton(toggle_label, callback_data="toggle_weather_notif")],
        [InlineKeyboardButton(f"⏰ Время уведомления: {weather_time}", callback_data="set_weather_time")],
        [InlineKeyboardButton(f"🏙 Город: {city}", callback_data="change_city")],
        # НОВОЕ: ручной пересчёт часового пояса по городу (исправление
        # «неправильного времени рассылки погоды» в один тап).
        [InlineKeyboardButton(f"🧭 Пояс: {tz_label} — пересчитать по городу", callback_data="weather_recalc_tz")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_settings")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_holiday_settings_keyboard(user):
    """Клавиатура настроек праздников: только переключатель уведомлений."""
    enabled = getattr(user, 'holiday_notifications', True)
    toggle_label = "🔕 Выключить уведомления о праздниках" if enabled else "🔔 Включить уведомления о праздниках"
    keyboard = [
        [InlineKeyboardButton(toggle_label, callback_data="toggle_holidays_notif")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_settings")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_notification_settings_keyboard(user):
    # ПУНКТ (анонимки/авто-очистка): тоггл предупреждения за день до того,
    # как фоновое задание удалит входящие анонимные сообщения. Если
    # anon_purge_notify=False — пользователь сознательно отказался от
    # «завтра в ЧЧ:ММ всё будет удалено», и фоновое задание ему молчит.
    anon_notify = bool(getattr(user, 'anon_purge_notify', True))
    anon_label = (
        "🔕 Не предупреждать об удалении анонимок"
        if anon_notify
        else "🔔 Предупреждать об удалении анонимок"
    )

    keyboard = [
        [InlineKeyboardButton(f"{'🔕 Выключить уведомления' if user.notifications else '🔔 Включить уведомления'}", callback_data="toggle_notifications")],
        [InlineKeyboardButton(f"⏰ Утреннее: {user.morning_notification_time}", callback_data="set_morning_time")],
        [InlineKeyboardButton(f"🌙 Вечернее: {user.evening_notification_time}", callback_data="set_evening_time")],
        [InlineKeyboardButton(f"📝 Текст утреннего: {user.morning_text[:20]}...", callback_data="set_morning_text")],
        [InlineKeyboardButton(f"📝 Текст вечернего: {user.evening_text[:20]}...", callback_data="set_evening_text")],
        [InlineKeyboardButton(anon_label, callback_data="toggle_anon_purge_notify")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_settings")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_birthday_settings_keyboard(user):
    bday_time = getattr(user, 'birthday_notification_time', '09:00') or '09:00'
    keyboard = [
        [InlineKeyboardButton("📅 Установить дату рождения", callback_data="set_birthday")],
        [InlineKeyboardButton(f"{'🔕 Выключить отсчет' if user.show_birthday_countdown else '🔔 Включить отсчет'}", callback_data="toggle_birthday_countdown")],
        # Метка кнопки — это ДЕЙСТВИЕ при нажатии, а не текущее состояние.
        # Если сейчас показывается классу (True) — кнопка предлагает «Скрыть».
        # Если сейчас скрыто (False) — кнопка предлагает «Показывать».
        # ИСПРАВЛЕНО: ранее условие было инвертировано — пользователь, думая,
        # что включает уведомление классу о своём ДР, на самом деле его
        # выключал, и поэтому бот «молчал» о ДР для одноклассников.
        [InlineKeyboardButton(
            f"{'🔕 Скрыть от класса' if user.show_birthday_to_class else '🔔 Показывать классу'}",
            callback_data="toggle_birthday_class",
        )],
        [InlineKeyboardButton(f"{'🔕 Выключить личное уведомление' if user.birthday_personal_notification else '🔔 Включить личное уведомление'}", callback_data="toggle_birthday_personal")],
        [InlineKeyboardButton(f"⏰ Время уведомления: {bday_time}", callback_data="set_birthday_notification_time")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_settings")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_developer_keyboard():
    # Подпись для тоггла «Уведомления о новых пользователях».
    # Состояние читаем из dev_settings.json — общий файл настроек разработчика.
    notify_enabled = is_dev_new_user_notification_enabled()
    notify_label = (
        "🔔 Уведомления о новых пользователях: ВКЛ"
        if notify_enabled
        else "🔕 Уведомления о новых пользователях: ВЫКЛ"
    )

    keyboard = [
        [InlineKeyboardButton("📊 Статистика", callback_data="dev_stats")],
        [InlineKeyboardButton("🌐 Рассылка всем", callback_data="dev_broadcast")],
        # === НОВОЕ: рассылка всем БЕЗ подписи "от разработчика" ===
        [InlineKeyboardButton("📩 Сообщение всем (без подписи)", callback_data="dev_instant_broadcast")],
        [InlineKeyboardButton("📨 Сообщение классу", callback_data="dev_class_message")],
        [InlineKeyboardButton("🗑️ Удалить класс", callback_data="dev_delete_class")],
        [InlineKeyboardButton("👤 Управление пользователями", callback_data="dev_user_management")],
        [InlineKeyboardButton("📝 Редактировать инструкцию", callback_data="dev_edit_instructions")],
        # === НОВОЕ: управление праздниками ===
        [InlineKeyboardButton("🎉 Назначить праздник", callback_data="dev_set_holiday")],
        [InlineKeyboardButton("🗑 Удалить праздник", callback_data="dev_delete_holiday")],
        # ПУНКТ (цены): кнопка изменения цен возвращена.
        # Цены автоматически подставляются в инструкцию через плейсхолдеры
        # {button_base}/{button_increment}/{unblock}/{unblock_dev}/{view_sender}/{broadcast}
        # и применяются при каждой покупке через load_prices(), поэтому менять
        # текст инструкции после смены цены не нужно — бот сам покажет новые цифры.
        [InlineKeyboardButton("💰 Изменить цены функций", callback_data="dev_quick_prices")],
        # НОВОЕ: сброс статистики Stars (история переводов больше не ведётся).
        [InlineKeyboardButton("🧹 Сбросить статистику Stars", callback_data="dev_reset_stars")],
        [InlineKeyboardButton("🌍 Создать глобальную кнопку", callback_data="dev_global_button")],
        # ПУНКТ 3: разработчик теперь может удалять глобальные кнопки (а не только создавать)
        [InlineKeyboardButton("🗑️ Удалить глобальную кнопку", callback_data="dev_delete_global_button")],
        # === НОВОЕ: тоггл уведомлений разработчику о новых пользователях ===
        [InlineKeyboardButton(notify_label, callback_data="dev_toggle_new_user_notify")],
        # ПУНКТ 4: разработчик начисляет внутреннюю валюту (звёзды) пользователю.
        [InlineKeyboardButton("⭐ Начислить звёзды пользователю", callback_data="dev_grant_stars")],
        # ПУНКТ 3 (саппорт): обзор/ответ на сообщения чата поддержки.
        [InlineKeyboardButton("💬 Чат поддержки (входящие)", callback_data="dev_support_inbox")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_user_management_keyboard():
    keyboard = [
        [InlineKeyboardButton("🚫 Заблокировать пользователя", callback_data="dev_block_user")],
        [InlineKeyboardButton("✅ Разблокировать пользователя", callback_data="dev_unblock_user")],
        [InlineKeyboardButton("💬 Написать пользователю", callback_data="dev_message_user")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_users_keyboard(users, action_prefix):
    keyboard = []
    for user_id, user in users.items():
        name = user.first_name or f"User {user_id}"
        if getattr(user, 'username', None):
            name += f" (@{user.username})"
        keyboard.append([InlineKeyboardButton(name, callback_data=f"{action_prefix}_{user_id}")])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")])
    return InlineKeyboardMarkup(keyboard)

def get_language_keyboard():
    keyboard = [
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_class_users_keyboard(class_obj):
    keyboard = []
    for user_id in class_obj.students:
        if user_id not in class_obj.admins and user_id not in class_obj.blocked_users:
            user = get_user(user_id)
            if user:
                name = user.first_name or f"User {user_id}"
                keyboard.append([InlineKeyboardButton(name, callback_data=f"anon_user_{user_id}")])

    for admin_id in class_obj.admins:
        if admin_id not in class_obj.blocked_users:
            user = get_user(admin_id)
            if user:
                name = f"👨‍💼 {user.first_name}"
                keyboard.append([InlineKeyboardButton(name, callback_data=f"anon_user_{admin_id}")])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_anon")])
    return InlineKeyboardMarkup(keyboard)

def get_anonymous_reply_keyboard(message_id):
    """ПУНКТ 11: клавиатура под входящим анонимным сообщением.

    Содержит сразу обе опции оплаты раскрытия отправителя — виртуальными
    звёздами бота и Telegram Stars (XTR), а также кнопку анонимного ответа.
    """
    price = PRICES.get('view_sender', 60)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"💎 Узнать за {price} ⭐ (виртуальные)",
            callback_data=f"anon_pay_virt_{message_id}",
        )],
        [InlineKeyboardButton(
            f"⭐ Узнать за {price} XTR (Telegram Stars)",
            callback_data=f"anon_pay_xtr_{message_id}",
        )],
        [InlineKeyboardButton(
            "💬 Ответить анонимно",
            callback_data=f"reply_anon_{message_id}",
        )],
        [InlineKeyboardButton("❌ Закрыть", callback_data="cancel_anon_reply")],
    ])

# ==================================
# === AI ФУНКЦИИ ===
# ==================================

async def _ai_thinking_animation(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """ПУНКТ 4: фоновая анимация «DEVORKS+ai думает…» — крутит фазы, пока не отменят.

    Не должен валить основной хендлер, поэтому все исключения глотаем.
    Цикл прерывается через asyncio.CancelledError из основной корутины.
    """
    # НОВАЯ АНИМАЦИЯ (по запросу пользователя): «живой» нейросетевой
    # пульс. Вместо пошагового прогресс-бара — крутящийся индикатор-
    # «нейрон» (4 фазы) и набирающиеся точки «думаю…», плюс лёгкая
    # пульсация эмодзи. Выглядит как настоящий живой ассистент,
    # который что-то «обрабатывает», а не как загрузочная полоса.
    spinner = ["◐", "◓", "◑", "◒"]
    pulse = ["🤖", "🧠", "💡", "🤖", "🧠", "✨"]
    dots = ["", ".", "..", "...", "....", "....."]
    captions = [
        "обрабатываю запрос",
        "соединяю нейроны",
        "ищу лучший ответ",
        "формулирую мысль",
        "подбираю слова",
        "проверяю себя",
    ]
    i = 0
    try:
        while True:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass
            try:
                # Рендерим без Markdown — так нет шанса сломаться на
                # непарных подчёркиваниях/звёздочках в любом из
                # фреймов, и анимация работает на любых клиентах.
                frame_text = (
                    f"{pulse[i % len(pulse)]} DEVORKS+ai "
                    f"{spinner[i % len(spinner)]}\n\n"
                    f"{captions[i % len(captions)]}{dots[i % len(dots)]}"
                )
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=frame_text,
                )
            except Exception:
                # «message is not modified», сетевые сбои и т. п. — просто игнор
                pass
            i += 1
            await asyncio.sleep(0.9)
    except asyncio.CancelledError:
        # Нормальное завершение, когда основной хендлер получил ответ
        raise
    except Exception as e:
        logger.warning(f"AI thinking animation failed: {e}")


async def _deepseek_chat(messages, timeout=60, temperature=0.2, force_json=False):
    """Низкоуровневый запрос к DeepSeek (OpenAI-совместимый API).

    Возвращает ТЕКСТ ответа модели или None при ошибке/отсутствии ключа.
    force_json=True включает response_format={"type":"json_object"} —
    DeepSeek гарантирует валидный JSON в ответе (нужно для автоматизации).
    """
    if not DEEPSEEK_API_KEY:
        logger.warning("DeepSeek: DEEPSEEK_API_KEY не задан — запрос пропущен.")
        return None
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 2048,
        "stream": False,
    }
    if force_json:
        payload["response_format"] = {"type": "json_object"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DEEPSEEK_API_BASE}/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    body = ""
                    try:
                        body = (await resp.text())[:300]
                    except Exception:
                        pass
                    logger.error(
                        f"DeepSeek API вернул статус {resp.status}: {body}"
                    )
                    return None
                data = await resp.json()
                content = data["choices"][0]["message"]["content"]
                return content
    except asyncio.TimeoutError:
        logger.error("DeepSeek API timeout")
        return None
    except Exception as e:
        logger.error(f"Ошибка запроса к DeepSeek: {e}")
        return None


def _extract_json_dict(raw_text):
    """Аккуратно вытаскивает первый JSON-объект из ответа модели.

    Модели иногда оборачивают JSON в ```json ...``` или добавляют текст
    вокруг. Эта функция честно пробует несколько стратегий разбора.
    Возвращает dict или None.
    """
    if not raw_text:
        return None
    text = raw_text.strip()
    # 1) Прямая попытка.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # 2) Срез по кодовым ограждениям ```json ... ``` / ``` ... ```.
    fenced = None
    if "```" in text:
        parts = text.split("```")
        for i in range(1, len(parts), 2):
            chunk = parts[i]
            if chunk.lstrip().lower().startswith("json"):
                chunk = chunk.lstrip()[4:]
            chunk = chunk.strip()
            try:
                obj = json.loads(chunk)
                if isinstance(obj, dict):
                    fenced = obj
                    break
            except Exception:
                continue
    if fenced is not None:
        return fenced
    # 3) Первая { ... последняя } — грубая, но рабочая эвристика.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


def _ai_chat_system_prompt():
    """Системный промпт чата DEVORKS+ai (базовый, режим «normal»).

    Требование разработчика: ИИ ВСЕГДА отвечает очень честно — только
    проверенные факты, никаких выдумок и «галлюцинаций»; если чего-то не
    знает — так и говорит. Плюс: чат ведёт ЛЮБУЮ тему и МГНОВЕННО
    переключается между темами — пользователь может сначала говорить об
    одном, а следующим сообщением резко о другом; бот не должен «тупить»
    и тащить старую тему за собой.
    """
    return (
        "Ты — DEVORKS+ai, умный и дружелюбный ИИ-помощник школьного Telegram-бота "
        "DEVORKS+. Отвечай ТОЛЬКО на русском языке, независимо от языка запроса.\n\n"
        "ГЛАВНЫЙ ПРИНЦИП — ЧЕСТНОСТЬ:\n"
        "0. Ты ВСЕГДА отвечаешь очень честно. Только правда и только проверенные "
        "факты: ничего не выдумывай, не сочиняй источники, цифры и даты, не "
        "приукрашивай. Если не знаешь или не уверен — честно скажи «не знаю» / "
        "«не уверен» и объясни, почему. Ошибся — сразу признай и исправь.\n\n"
        "ПРИНЦИПЫ РАБОТЫ:\n"
        "1. Ты умеешь поддерживать ЛЮБУЮ тему: школа, ДЗ, игры, спорт, код, "
        "кино, отношения, юмор и т. д. Нет «запретных для себя» тем.\n"
        "2. ПЕРЕД каждым ответом определи: это продолжение текущей темы или "
        "НОВАЯ тема? Если пользователь резко сменил тему — мгновенно следуй "
        "НОВОЙ теме: не возвращайся к старой, не спрашивай «так о чём мы "
        "говорили», не тащи старый контекст в ответ без явной необходимости.\n"
        "3. Отвечай точно по существу ПОСЛЕДНЕГО сообщения. История диалога — "
        "только для контекста, а не повод застревать в прошлом.\n"
        "4. Понимай запросы очень точно: учитывай синонимы, сленг, опечатки и "
        "недоговорённости. Если вопрос двусмысленный — сделай наиболее "
        "разумное предположение, ответь по нему и кратко отметь предположение.\n"
        "5. Если не знаешь чего-то или не уверен — честно скажи об этом, "
        "ничего не выдумывай.\n"
        "6. Будь кратким и полезным: обычно 1–6 абзацев, без воды и повторов."
    )


def _ai_chat_full_prompt(persona):
    """Полный системный промпт чата с учётом выбранного режима личности.

    Режимы «хамло» и «тепло» заданы разработчиком бота дословно — они
    добавляются ПОВЕРХ базовых правил (честность, смена тем, точность),
    чтобы чат не терял базовые навыки понимания в любом режиме.
    """
    base = _ai_chat_system_prompt()
    persona = persona if persona in AI_PERSONA_MODES else "normal"
    if persona == "normal":
        return base
    persona_text = AI_PERSONA_PROMPTS.get(persona, "").strip()
    title = AI_PERSONA_MODES[persona]["title"]
    if not persona_text:
        return base
    # Для «хамло» прямо отменяем базовый дружелюбный тон: иначе модель
    # усредняет режимы и получается «не слишком хамло» (жалоба пользователя).
    tone_override = (
        "\n\nОТМЕНА БАЗОВОГО ТОНА: правила о дружелюбии, мягкости и заботе из "
        "базовой части НЕ ДЕЙСТВУЮТ в этом режиме. Единственное, что остаётся "
        "из базы — честность, точность фактов и умение менять темы. Тон, форма "
        "и длина ответа определяются ТОЛЬКО режимом ниже."
        if persona == "hamlo" else ""
    )
    return (
        f"{base}{tone_override}\n\n"
        f"РЕЖИМ ОТВЕТА «{title}» — ВЫСШИЙ ПРИОРИТЕТ, СТРОГО СЛЕДУЙ:\n"
        f"{persona_text}"
    )


# === OCR (распознавание текста на фото) ===
# Бесплатно и безлимитно: распознавание выполняется ЛОКАЛЬНО на сервере.
# Движки пробуются по очереди:
#   1) Tesseract (pytesseract) — нужен системный пакет:
#      apt install tesseract-ocr tesseract-ocr-rus (пакеты rus+eng);
#   2) EasyOCR (pip install easyocr) — если установлен (качает модели при
#      первом запуске, дальше работает офлайн);
# Если ни один не доступен — фото уходит в вижн-модель Groq, как раньше.
OCR_LANGS = os.environ.get("OCR_LANGS", "rus+eng")
_ocr_engine_cache = {"checked": False, "engine": None, "reader": None}
_ocr_lock = threading.Lock()


def _detect_ocr_engine():
    """Определяет доступный OCR-движок (один раз, результат кэшируется).

    Возвращает кортеж (имя_движка или None). Для tesseract дополнительно
    проверяет, что бинарник реально установлен и работает.
    """
    with _ocr_lock:
        if _ocr_engine_cache["checked"]:
            return _ocr_engine_cache["engine"]
        _ocr_engine_cache["checked"] = True

        # 1) Tesseract (pytesseract + системный tesseract).
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
            # get_tesseract_version упадёт, если бинарника нет — это честная проверка.
            pytesseract.get_tesseract_version()
            _ocr_engine_cache["engine"] = "tesseract"
            logger.info("OCR: движок tesseract готов (языки: %s).", OCR_LANGS)
            return "tesseract"
        except Exception:
            pass

        # 2) EasyOCR (тяжелее: требует torch; модели качаются при первом запуске).
        try:
            import easyocr  # noqa: F401
            _ocr_engine_cache["engine"] = "easyocr"
            logger.info("OCR: движок easyocr готов (языки: %s).", OCR_LANGS)
            return "easyocr"
        except Exception:
            pass

        logger.info(
            "OCR: движки не найдены (нужен tesseract-ocr + tesseract-ocr-rus "
            "или pip install easyocr). Фото будут обрабатываться вижн-моделью."
        )
        return None


def _ocr_image_sync(image_bytes):
    """Синхронное распознавание текста на изображении (вызывать в потоке).

    Возвращает распознанный текст (str) или None, если текста нет/движка нет.
    """
    engine = _detect_ocr_engine()
    if not engine:
        return None
    try:
        if engine == "tesseract":
            import pytesseract
            from PIL import Image
            buf = io.BytesIO(image_bytes)
            img = Image.open(buf)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            # Честный подбор языков: берём только те, что реально установлены
            # в tesseract (если rus не установлен — не молчем, а логируем
            # подсказку; пустой результат лучше «мусорного» распознавания).
            wanted = [c for c in OCR_LANGS.split("+") if c]
            try:
                available = set(pytesseract.get_languages(config=""))
            except Exception:
                available = None
            if available is not None:
                usable = [c for c in wanted if c in available]
                if not usable:
                    logger.warning(
                        "OCR: ни один из языков %s не установлен в tesseract "
                        "(apt install tesseract-ocr-rus tesseract-ocr-eng). "
                        "Распознавание пропущено.", wanted
                    )
                    return None
                if usable != wanted:
                    logger.warning(
                        "OCR: языки %s недоступны, использую %s. Для русского "
                        "текста установите пакет tesseract-ocr-rus.",
                        [c for c in wanted if c not in available], usable
                    )
                langs = "+".join(usable)
            else:
                langs = OCR_LANGS
            text = pytesseract.image_to_string(img, lang=langs)
        else:  # easyocr
            reader = _ocr_engine_cache.get("reader")
            if reader is None:
                import easyocr
                lang_codes = [c for c in OCR_LANGS.split("+") if c]
                # EasyOCR: 'ru' и 'en'; неизвестные коды убираем.
                lang_codes = [c if c != "rus" else "ru" for c in lang_codes] or ["ru", "en"]
                reader = easyocr.Reader(lang_codes, verbose=False)
                _ocr_engine_cache["reader"] = reader
            import numpy as _np
            from PIL import Image as _PILImage
            buf = io.BytesIO(image_bytes)
            img = _PILImage.open(buf).convert("RGB")
            lines = reader.readtext(_np.array(img), detail=0, paragraph=True)
            text = "\n".join(str(l) for l in lines)
        text = (text or "").strip()
        return text if text else None
    except Exception as e:
        logger.warning(f"OCR: ошибка распознавания: {e}")
        return None


async def _ocr_image_text(image_bytes):
    """Асинхронная обёртка OCR: не блокирует event-loop бота."""
    if not image_bytes:
        return None
    # Прогрев движка делаем в потоке — на первый запуск easyocr качает модели.
    return await asyncio.to_thread(_ocr_image_sync, image_bytes)


def _shrink_image_bytes(image_bytes, max_side=1600, quality=85, max_bytes=3_500_000):
    """Сжимает картинку перед отправкой в вижн-модель.

    Скриншоты-документы бывают по 5–10 МБ: Groq отклоняет такие запросы,
    и пользователь получал «AI временно недоступен». Если картинка уже
    небольшая — возвращаем как есть. Pillow недоступен — тоже как есть.
    """
    try:
        if not image_bytes or len(image_bytes) <= max_bytes:
            return image_bytes
        from PIL import Image
        buf = io.BytesIO(image_bytes)
        img = Image.open(buf)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        scale = min(1.0, float(max_side) / float(max(w, h)))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        data = out.getvalue()
        logger.info(
            f"Vision: картинка сжата {len(image_bytes)} -> {len(data)} байт."
        )
        return data or image_bytes
    except Exception as e:
        logger.warning(f"_shrink_image_bytes: {e}")
        return image_bytes


# ==================================
# === 🎙 ГОЛОСОВОЙ ВВОД: бесплатная расшифровка голосовых ===
# ==================================
# Пользователь управляет ботом ГОЛОСОМ из любого раздела: голосовое (или
# аудиофайл) расшифровывается через Groq Whisper (whisper-large-v3-turbo) по
# УЖЕ имеющемуся ключу GROQ_API_KEY — ничего дополнительно платить или
# устанавливать не нужно. Расшифрованный текст подставляется в update.message
# как обычный текст, дальше работают ВСЕ прежние обработчики: автоматизация,
# чат с ИИ, FSM-вводы (дата ДЗ, имя учителя, время звонков…) — всё голосом.
#
# Как это работает технически: хендлер зарегистрирован в ГРУППЕ -1 (раньше
# ConversationHandler), поэтому к моменту выбора состояния сообщение уже
# «превращено» в текст. PTB запрещает обычный setattr у Message, поэтому
# текст подменяется через object.__setattr__ (поле pydantic-модели).


async def _groq_transcribe_audio(audio_bytes, filename="voice.ogg"):
    """Расшифровка речи через Groq Whisper. Возвращает текст или None.

    Основная модель — whisper-large-v3-turbo; если аккаунт/регион её не
    выдаёт — честный повтор на классическом whisper-large-v3.
    """
    if not audio_bytes:
        return None
    # Fallback: классический whisper-large-v3 пробуем, если основная модель —
    # не он сам (например turbo недоступна в аккаунте/регионе).
    models = [GROQ_STT_MODEL]
    if GROQ_STT_MODEL != "whisper-large-v3":
        models.append("whisper-large-v3")
    for model_name in models:
        try:
            buf = io.BytesIO(audio_bytes)
            buf.name = filename  # Groq требует имя файла с расширением
            tr = await asyncio.wait_for(
                groq_client.audio.transcriptions.create(
                    model=model_name,
                    file=buf,
                    temperature=0.0,
                ),
                timeout=120,
            )
            text = (getattr(tr, "text", "") or "").strip()
            if text:
                return text
            # Пустой результат — попробовать следующую модель бессмысленно:
            # голоса/речи просто не было.
            return None
        except asyncio.TimeoutError:
            logger.error(f"STT: таймаут расшифровки ({model_name})")
        except Exception as e:
            msg = str(e)
            lower = msg.lower()
            transient = any(
                t in lower
                for t in ("model_not_found", "does not exist", "decommission",
                          "not found", "no longer available", "rate limit",
                          "too many requests", "429", "404")
            )
            if transient and model_name != models[-1]:
                logger.warning(f"STT: {model_name} недоступна ({msg[:200]}) — пробую fallback.")
                continue
            logger.error(f"STT: ошибка расшифровки ({model_name}): {msg[:300]}")
            return None
    return None


def _set_message_text(update, text):
    """Подменяет text у сообщения (PTB запрещает обычный setattr)."""
    msg = getattr(update, "message", None)
    if msg is None:
        return False
    try:
        object.__setattr__(msg, "text", text)
        return True
    except Exception as e:
        logger.error(f"STT: не удалось подставить текст в сообщение: {e}")
        return False


async def _voice_transcription_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Группа -1: голосовое/аудио → текст ДО остальных обработчиков.

    Успех: update.message.text = расшифровка — дальше всё идёт как будто
    пользователь напечатал. Неуспех (нет ключа, ошибка, пусто, флуд) —
    честный ответ пользователю и ApplicationHandlerStop (апдейт не идёт
    дальше, чтобы состояния не «съедали» голосовое как непонятный ввод).
    """
    try:
        msg = getattr(update, "message", None)
        if msg is None or getattr(update, "edited_message", None) is not None:
            return
        att = getattr(msg, "voice", None) or getattr(msg, "audio", None)
        if att is None or getattr(msg, "text", None):
            return
        user_id = str(update.effective_user.id)
        chat_id = update.effective_chat.id

        # Честный анти-флуд: расшифровка бесплатна, но не бесконечна.
        if is_user_spamming(user_id, key="voice_stt", min_interval=1.5, burst=6, burst_window=60.0):
            try:
                await msg.reply_text(
                    "🎙 Слишком много голосовых подряд. Подождите пару секунд."
                )
            except Exception:
                pass
            raise ApplicationHandlerStop

        # GROQ_API_KEY обязателен (проверка на старте), но на всякий случай.
        if not GROQ_API_KEY:
            try:
                await msg.reply_text(
                    "🎙 Расшифровка голоса недоступна: на сервере не задан GROQ_API_KEY."
                )
            except Exception:
                pass
            raise ApplicationHandlerStop

        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass

        try:
            tg_file = await context.bot.get_file(att.file_id)
            buf = io.BytesIO()
            await asyncio.wait_for(
                tg_file.download_to_memory(out=buf), timeout=60
            )
            buf.seek(0)
            audio_bytes = buf.read()
        except Exception as e:
            logger.error(f"STT: не удалось скачать аудио: {e}")
            try:
                await msg.reply_text(
                    "🎙 Не смог скачать голосовое сообщение. Попробуйте ещё раз."
                )
            except Exception:
                pass
            raise ApplicationHandlerStop

        # Имя файла: Telegram voice — .ogg (opus), audio — по mime_type.
        fname = "voice.ogg"
        mime = str(getattr(att, "mime_type", "") or "")
        if mime and "/" in mime:
            ext = mime.split("/")[-1].split(";")[0]
            ext = {"mpeg": "mp3", "x-m4a": "m4a"}.get(ext, ext)
            fname = f"audio.{ext}"

        transcript = await _groq_transcribe_audio(audio_bytes, filename=fname)
        if not transcript:
            try:
                await msg.reply_text(
                    "🎙 Не расслышал голосовое (речь не распознана). Попробуйте "
                    "сказать ближе к микрофону или отправьте текстом."
                )
            except Exception:
                pass
            raise ApplicationHandlerStop

        if not _set_message_text(update, transcript):
            try:
                await msg.reply_text(
                    f"🎙 Распознал, но не смог передать в чат. Продублируйте текстом:\n\n«{transcript[:2000]}»"
                )
            except Exception:
                pass
            raise ApplicationHandlerStop
        # Успех: текст подставлен — обработчики увидят обычное текстовое сообщение.
        logger.info(f"STT: {user_id} -> «{transcript[:80]}»")
    except ApplicationHandlerStop:
        raise
    except Exception as e:
        logger.error(f"STT middleware сбой: {e}")
        # Даже при внутреннем сбое глушим апдейт: голосовое, превращённое
        # «наполовину», могло бы уронить состояния ввода.
        try:
            if getattr(update, "message", None):
                await update.message.reply_text(
                    "🎙 Произошёл сбой при расшифровке голоса. Отправьте текстом."
                )
        except Exception:
            pass
        raise ApplicationHandlerStop


async def handle_ai_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщения в режиме AI. Полностью защищён от исключений,
    чтобы бот не «замолкал» после нескольких сообщений.

    ПУНКТ 4: пока модель отвечает, пользователю показывается анимированное
    сообщение «DEVORKS+ai думает…», которое затем заменяется на реальный ответ.

    НОВОЕ (OCR): текст на фото распознаётся ЛОКАЛЬНО и безлимитно
    (Tesseract/EasyOCR). Если OCR что-то нашёл — текст идёт в текстовую
    модель (DeepSeek), что точнее и дешевле для фотографий ДЗ/документов;
    если OCR недоступен или текста на фото нет — фото уходит в вижн-модель
    Groq (`GROQ_VISION_MODEL`), как раньше.
    Сама история разговора хранит текстовую выжимку фото («[фото: …]»),
    чтобы не раздувать базу.
    """
    user_id = str(update.effective_user.id)
    msg = update.message
    user_message = (msg.text or msg.caption or "").strip()
    chat_id = update.effective_chat.id
    # НОВОЕ: фото можно прислать и КАРТИНКОЙ-ДОКУМЕНТОМ (скриншоты «файлом» —
    # частый сценарий: так Telegram не сжимает изображение). Раньше такие
    # сообщения вообще не попадали в ИИ-чат и бот молчал.
    _doc = getattr(msg, "document", None)
    has_photo = bool(getattr(msg, "photo", None))
    is_doc_image = bool(
        _doc is not None
        and str(getattr(_doc, "mime_type", "") or "").startswith("image/")
    )
    _doc_mime = str(getattr(_doc, "mime_type", "") or "image/jpeg") if is_doc_image else "image/jpeg"
    _persona_user = get_user(user_id)
    if not _persona_user:
        _persona_user = User(user_id)
    ai_persona = getattr(_persona_user, "ai_persona", "normal") or "normal"

    # === Синхронизация цен: плата за генерацию DEVORKS+ai ===
    # Цена берётся из единого конфига PRICES['ai_generation'] в момент
    # вызова — изменение в админ-панели действует мгновенно. 0 = бесплатно
    # (поведение по умолчанию, старые пользователи ничего не заметят).
    ai_price = get_price('ai_generation', 0)
    if ai_price > 0:
        _ai_user = get_user(user_id)
        if not _ai_user:
            _ai_user = User(user_id)
        if _ai_user.stars_balance < ai_price:
            try:
                await msg.reply_text(
                    f"⭐ Недостаточно звёзд для генерации.\n\n"
                    f"💰 Ваш баланс: {_ai_user.stars_balance} ⭐\n"
                    f"🤖 Стоимость генерации: {ai_price} ⭐\n\n"
                    f"Пополните баланс: ⭐ Звезды → 💎 Купить звезды.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⭐ Открыть Звезды", callback_data="back_to_main")],
                    ]),
                )
            except Exception:
                pass
            return
        # Списание за генерацию (фиксируется в агрегированной статистике).
        add_stars_transaction(user_id, -ai_price, "Генерация DEVORKS+ai")
        try:
            await msg.reply_text(f"🤖 Списано {ai_price} ⭐ за генерацию (баланс: {get_user(user_id).stars_balance} ⭐).")
        except Exception:
            pass

    # 1) Сразу шлём «typing», чтобы у клиента появилась анимация набора текста
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception as e:
        logger.warning(f"send_chat_action failed: {e}")

    # 2) Отправляем «думающее» сообщение и запускаем фоновую анимацию.
    thinking_msg = None
    anim_task = None
    try:
        thinking_msg = await update.message.reply_text("🤖 DEVORKS+ai думает   ⏳")
        anim_task = asyncio.create_task(
            _ai_thinking_animation(context, chat_id, thinking_msg.message_id)
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить thinking-сообщение: {e}")

    if user_id not in user_conversations:
        user_conversations[user_id] = []

    # Системный промпт всегда соответствует ТЕКУЩЕМУ режиму личности:
    # переключение режима кнопками действует мгновенно, даже внутри диалога.
    current_system_prompt = _ai_chat_full_prompt(ai_persona)
    if not user_conversations[user_id]:
        user_conversations[user_id].append({
            "role": "system",
            "content": current_system_prompt,
        })
    elif (
        user_conversations[user_id][0].get("role") != "system"
        or str(user_conversations[user_id][0].get("content", "")) != current_system_prompt
    ):
        # Миграция старых промптов + мгновенное применение смены режима.
        user_conversations[user_id][0] = {
            "role": "system",
            "content": current_system_prompt,
        }

    # Если пришло фото (или картинка-документ) — скачиваем; сначала пробуем
    # ЛОКАЛЬНЫЙ OCR (бесплатно и безлимитно), при неудаче — вижн-модель Groq.
    photo_data_url = None
    ocr_text = None
    photo_bytes = None
    if has_photo or is_doc_image:
        try:
            if has_photo:
                largest = msg.photo[-1]
                tg_file = await context.bot.get_file(largest.file_id)
            else:
                tg_file = await context.bot.get_file(_doc.file_id)
            buf = io.BytesIO()
            await tg_file.download_to_memory(out=buf)
            buf.seek(0)
            photo_bytes = buf.read()
            # Сжатие слишком больших картинок (Groq ограничивает размер
            # запроса; скриншоты-документы бывают по 5–10 МБ).
            photo_bytes = _shrink_image_bytes(photo_bytes)
            b64 = base64.b64encode(photo_bytes).decode("ascii")
            mime = _doc_mime if is_doc_image else "image/jpeg"
            photo_data_url = f"data:{mime};base64,{b64}"
        except Exception as e:
            logger.warning(f"AI: не удалось скачать фото: {e}")
            photo_data_url = None
            photo_bytes = None
        if photo_bytes:
            # OCR работает в отдельном потоке и не блокирует бота.
            ocr_text = await _ocr_image_text(photo_bytes)

    # История: пользовательское сообщение добавляем до запроса, но ограничиваем длину,
    # а при ошибке запроса — откатываем, чтобы не копить «висячие» user-сообщения.
    if (has_photo or is_doc_image) and ocr_text:
        ocr_preview = ocr_text if len(ocr_text) <= 400 else ocr_text[:400] + "…"
        history_text = f"[фото, распознанный текст: {ocr_preview}]"
        if user_message:
            history_text = f"{history_text} {user_message}"
    elif has_photo or is_doc_image:
        history_text = "[фото]" if not user_message else f"[фото] {user_message}"
    else:
        history_text = user_message
    user_conversations[user_id].append(
        {"role": "user", "content": history_text}
    )
    # Оставляем system + последние 20 сообщений
    if len(user_conversations[user_id]) > 21:
        system_msg = user_conversations[user_id][0]
        user_conversations[user_id] = [system_msg] + user_conversations[user_id][-20:]

    async def _stop_animation():
        if anim_task and not anim_task.done():
            anim_task.cancel()
            try:
                await anim_task
            except (asyncio.CancelledError, Exception):
                pass

    # Подготовка messages: при УСПЕШНОМ OCR текст с фото идёт в ТЕКСТОВУЮ
    # модель (DeepSeek — точнее для ДЗ/документов и безлимитно локально);
    # без OCR — фото уходит в вижн-модель Groq отдельным multimodal-сообщением.
    if photo_data_url and ocr_text:
        # === OCR-путь: текстовая модель видит распознанный текст ===
        request_messages = list(user_conversations[user_id][:-1])
        ocr_block = (
            "[Текст, распознанный на фото (OCR):]\n"
            f"{ocr_text[:3500]}\n"
            "[Конец распознанного текста]"
        )
        final_text = (user_message + "\n\n" if user_message else "") + ocr_block + (
            "\n\nОтветь на вопрос пользователя по этому фото. Если пользователь "
            "спросил что-то конкретное — ответь именно на него, опираясь на "
            "распознанный текст. Если текст распознан с ошибками — молча поправь "
            "очевидные опечатки OCR. Если текста на фото нет — так и скажи честно."
        )
        request_messages.append({"role": "user", "content": final_text})
        request_model = None  # текстовая модель (DeepSeek → Groq текст)
        prefer_text_engine = True
    elif photo_data_url:
        # === Вижн-путь (OCR недоступен/текст не найден) ===
        request_messages = list(user_conversations[user_id][:-1])
        prompt_text = user_message or "Опиши, что изображено на фото, и ответь на возможный вопрос пользователя."
        request_messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": photo_data_url}},
            ],
        })
        request_model = GROQ_VISION_MODEL
        prefer_text_engine = False
    else:
        request_messages = user_conversations[user_id]
        request_model = GROQ_MODEL
        prefer_text_engine = True

    ai_response = None
    vision_error_note = None
    if prefer_text_engine and DEEPSEEK_API_KEY:
        # === Чат с ИИ на DeepSeek API — основной текстовый движок ===
        # Понимает любую тему и мгновенно переключается между темами
        # (правила зашиты в _ai_chat_system_prompt). При сбое — fallback
        # на Groq ниже, так что чат не молчит никогда.
        _ds_answer = await _deepseek_chat(
            request_messages, timeout=90, temperature=0.6
        )
        if _ds_answer is not None:
            ai_response = _ds_answer
    if ai_response is None and photo_data_url and not prefer_text_engine:
        # === Вижн-путь с ЦЕПОЧКОЙ моделей (ИСПРАВЛЕНО «ии не видит фото») ===
        # Раньше была одна модель: упала (устарела/rate-limit/сбой) — и
        # пользователь получал «AI временно недоступен». Теперь перебираем
        # все доступные вижн-модели по очереди.
        _vision_chain = []
        for _m in [GROQ_VISION_MODEL] + list(GROQ_VISION_FALLBACK_MODELS):
            if _m and _m not in _vision_chain:
                _vision_chain.append(_m)
        _vision_errors = []
        for _vmodel in _vision_chain:
            try:
                chat_completion = await asyncio.wait_for(
                    groq_client.chat.completions.create(
                        messages=request_messages,
                        model=_vmodel,
                        temperature=0.7,
                        max_tokens=2048,
                    ),
                    timeout=120,
                )
                ai_response = chat_completion.choices[0].message.content or ""
                if _vmodel != GROQ_VISION_MODEL:
                    logger.info(f"Vision: основная модель не ответила, сработала {_vmodel}.")
                break
            except asyncio.TimeoutError:
                _vision_errors.append(f"{_vmodel}: таймаут")
                logger.error(f"Groq vision timeout ({_vmodel})")
            except Exception as e:
                _vision_errors.append(f"{_vmodel}: {e}")
                logger.error(f"Groq vision error ({_vmodel}): {e}")
        if ai_response is None:
            vision_error_note = "; ".join(_vision_errors[:3])
            # Вся цепочка упала: откат истории и ЧЕСТНАЯ ошибка с причиной.
            await _stop_animation()
            if user_conversations[user_id] and user_conversations[user_id][-1].get("role") == "user":
                user_conversations[user_id].pop()
            try:
                err_text = (
                    "😔 Не смог проанализировать фото — вижн-модели недоступны."
                    + (f"\nПричина: {vision_error_note}" if vision_error_note else "")
                    + "\n\nПопробуйте ещё раз позже. Совет: установите на сервере "
                    "бесплатный OCR (`apt install tesseract-ocr tesseract-ocr-rus` "
                    "+ `pip install pytesseract Pillow`) — тогда текст с фото я "
                    "буду читать локально, безлимитно, даже без вижн-моделей."
                )
                if thinking_msg:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=thinking_msg.message_id, text=err_text
                    )
                else:
                    await update.message.reply_text(err_text)
            except Exception:
                pass
            return
    elif ai_response is None:
        try:
            chat_completion = await asyncio.wait_for(
                groq_client.chat.completions.create(
                    messages=request_messages,
                    model=request_model or GROQ_MODEL,
                    temperature=0.7,
                    max_tokens=2048,
                ),
                timeout=120 if (photo_data_url or is_doc_image) else 60,
            )
            ai_response = chat_completion.choices[0].message.content or ""
        except asyncio.TimeoutError:
            logger.error("Groq API timeout")
            await _stop_animation()
            # Откатываем последнее user-сообщение, чтобы история осталась консистентной
            if user_conversations[user_id] and user_conversations[user_id][-1].get("role") == "user":
                user_conversations[user_id].pop()
            try:
                err_text = "⏱ DEVORKS+ai не ответил вовремя. Попробуйте ещё раз — я всё ещё здесь."
                if thinking_msg:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=thinking_msg.message_id, text=err_text
                    )
                else:
                    await update.message.reply_text(err_text)
            except Exception:
                pass
            return
        except Exception as e:
            logger.error(f"Error in AI handle_message (groq): {e}")
            await _stop_animation()
            if user_conversations[user_id] and user_conversations[user_id][-1].get("role") == "user":
                user_conversations[user_id].pop()
            try:
                # ЧЕСТНАЯ ошибка вместо безликой: причина + что делать.
                if photo_data_url or is_doc_image:
                    err_text = (
                        "😔 Не смог проанализировать фото — все вижн-модели недоступны."
                        + (f"\nПричина: {vision_error_note}" if vision_error_note else "")
                        + "\n\nСовет: установите на сервере бесплатный OCR — тогда я буду "
                        "читать текст с фото локально, безлимитно: "
                        "`apt install tesseract-ocr tesseract-ocr-rus` + "
                        "`pip install pytesseract Pillow`."
                    )
                else:
                    err_text = "AI временно недоступен. Попробуйте позже."
                if thinking_msg:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=thinking_msg.message_id, text=err_text
                    )
                else:
                    await update.message.reply_text(err_text)
            except Exception:
                pass
            return

    # Сохраняем ответ ассистента в историю
    user_conversations[user_id].append(
        {"role": "assistant", "content": ai_response}
    )

    # Останавливаем анимацию ДО подмены текста, чтобы фон не перетёр итог.
    await _stop_animation()

    # Отправляем ответ БЕЗ Markdown, чтобы не падать на не-парных *, _, ` и т.п.
    prefix = "🤖 DEVORKS+ai:\n\n"
    full_text = prefix + ai_response if ai_response else prefix + "(пустой ответ)"
    try:
        # Telegram лимит — 4096 символов на сообщение
        if len(full_text) <= 4096:
            if thinking_msg:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=thinking_msg.message_id,
                        text=full_text,
                    )
                except Exception:
                    await update.message.reply_text(full_text)
            else:
                await update.message.reply_text(full_text)
        else:
            # Первая часть в thinking-сообщение, остальное — отдельными
            first_chunk = full_text[:4096]
            if thinking_msg:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=thinking_msg.message_id,
                        text=first_chunk,
                    )
                except Exception:
                    await update.message.reply_text(first_chunk)
            else:
                await update.message.reply_text(first_chunk)
            remaining = full_text[4096:]
            for i in range(0, len(remaining), 4096):
                await update.message.reply_text(remaining[i:i + 4096])
    except Exception as e:
        logger.error(f"Error sending AI response: {e}")
        try:
            await update.message.reply_text(
                "Не удалось получить ответ. Попробуйте ещё раз."
            )
        except Exception:
            pass

# ==================================
# === 🪄 АВТОМАТИЗАЦИЯ (DeepSeek) ===
# ==================================
# Свободный текст пользователя («русский на пятницу этой недели такая-то
# страница», «замени учителей по биологии и географии») превращается DeepSeek
# в строгий JSON-сценарий, который бот исполняет: ДЗ, учителя, расписание,
# звонки, таймеры, каникулы, рассылка классу и чтение данных.
# Если информации не хватает — DeepSeek задаёт уточняющий вопрос, и диалог
# продолжается в том же режиме, пока сценарий не будет полным.

AUTOMATION_BUTTON = "🪄 Автоматизация"

# Триггеры выхода из режима автоматизации (сравниваются в нижнем регистре).
AUTOMATION_EXIT_TRIGGERS = {
    "выход", "выход из автоматизации", "отмена", "cancel", "/cancel",
    "стоп", "stop", "/stop", "❌ отмена", "❌ отмена автоматизации",
}

# Карта разделов интерфейса для действия open_section: автоматизация умеет
# «нажать» ЛЮБУЮ кнопку главного меню за пользователя (и личные, и классные
# кнопки тоже — они проверяются внутри handle_main_menu автоматически).
AUTOMATION_SECTIONS = {
    "меню": "__main_menu__",
    "главное меню": "__main_menu__",
    "домой": "__main_menu__",
    "дз": "📝 Домашнее задание",
    "домашка": "📝 Домашнее задание",
    "домашнее задание": "📝 Домашнее задание",
    "добавить дз": "➕ Добавить ДЗ",
    "удалить дз": "🗑️ Удалить ДЗ",
    "учителя": "👨‍🏫 Учителя",
    "расписание": "📅 Расписание",
    "звонки": "🔔 Звонки",
    "каникулы": "🎉 Каникулы",
    "таймер": "⏰ Таймер",
    "напоминания": "⏰ Таймер",
    "анонимное сообщение": "🕵️ Анонимное сообщение",
    "анонимка": "🕵️ Анонимное сообщение",
    "мои анонимные сообщения": "📨 Мои анонимные сообщения",
    "управление классами": "🎓 Управление классами",
    "классы": "🎓 Управление классами",
    "мой класс": "🎓 Управление классами",
    "мои кнопки": "🌟 Мои кнопки",
    "кнопки": "🌟 Мои кнопки",
    "звезды": "⭐ Звезды",
    "звёзды": "⭐ Звезды",
    "баланс": "⭐ Звезды",
    "ии": "🤖 DEVORKS+ai",
    "ai": "🤖 DEVORKS+ai",
    "devorks+ai": "🤖 DEVORKS+ai",
    "чат": "🤖 DEVORKS+ai",
    "погода": "🌦 Погода",
    "инструкция": "📚 Инструкция",
    "помощь": "📚 Инструкция",
    "код класса": "🔑 Код класса",
    "написать админу": "💬 Написать админу",
    "настройки": "⚙️ Настройки",
    "назад в меню": "⬅️ Назад в меню",
}


async def _automation_open_section(update, context, user, action):
    """Действие open_section: открывает раздел интерфейса бота.

    Реализовано ЧЕСТНО через существующий handle_main_menu: автоматизация
    буквально «нажимает» кнопку главного меню за пользователя — работает вся
    прежняя логика (личные/глобальные/классные кнопки, проверки подписки и
    блокировок), ничего не дублируется.

    Возвращает (отчёт_или_None, новое_состояние_FSM)."""
    raw = (action.get("section") or "").strip().lower()
    target = AUTOMATION_SECTIONS.get(raw)
    if target is None:
        # Мягкий поиск по подстроке: «открой домашку», «зайди в звонки» и т. п.
        for key, val in AUTOMATION_SECTIONS.items():
            if key and (key in raw or raw in key):
                target = val
                break
    if target is None:
        # 👆 УПРАВЛЕНИЕ ВСЕМИ КНОПКАМИ: если раздел не стандартный — пробуем
        # найти ЛИЧНУЮ, ГЛОБАЛЬНУЮ или КЛАССНУЮ кнопку пользователя по имени
        # (точно или по подстроке). handle_main_menu сам умеет их «нажимать».
        try:
            all_names = get_all_user_button_names(user) or []
            exact = [n for n in all_names if n.strip().lower() == raw]
            fuzzy = [n for n in all_names if raw and (raw in n.lower() or n.lower() in raw)]
            target = (exact or fuzzy or [None])[0]
        except Exception as e:
            logger.warning(f"automation open_section buttons lookup: {e}")
            target = None
    if target is None:
        return (
            "❓ Не понял, какой раздел открыть. Скажите, например: «открой дз», "
            "«зайди в звезды», «включи чат с ии», «покажи меню» — или назовите "
            "точное название вашей кнопки.",
            AI_AUTOMATION,
        )
    context.user_data.pop('automation_history', None)
    if target == "__main_menu__":
        await show_main_menu(update, context, user)
        return (None, MAIN_MENU)
    # «Нажимаем» кнопку: копия update с подменённым текстом сообщения.
    import copy as _copy
    fake_update = _copy.copy(update)
    fake_msg = _copy.copy(update.message)
    fake_msg.text = target
    fake_update.message = fake_msg
    try:
        new_state = await handle_main_menu(fake_update, context)
    except Exception as e:
        logger.error(f"automation open_section '{target}': {e}")
        await update.message.reply_text(
            f"⚠️ Не удалось открыть раздел «{target}». Попробуйте нажать кнопку вручную."
        )
        return (None, AI_AUTOMATION)
    return (None, new_state if new_state is not None else MAIN_MENU)


def _automation_class_context_text(user, class_obj):
    """Собирает текстовый «слепок» состояния класса для промпта DeepSeek:
    сегодня, день недели, предметы, учителя, звонки, каникулы."""
    local_now = get_local_time(user)
    day_names = ["Понедельник", "Вторник", "Среда", "Четверг",
                 "Пятница", "Суббота", "Воскресенье"]
    lines = [
        f"Сегодня (локальное время пользователя): {local_now.strftime('%Y-%m-%d')} "
        f"({day_names[local_now.weekday()]}), время {local_now.strftime('%H:%M')}.",
        f"Дни недели и их даты на этой неделе: " + ", ".join(
            f"{day_names[i]}={(local_now - timedelta(days=local_now.weekday()) + timedelta(days=i)).strftime('%Y-%m-%d')}"
            for i in range(7)
        ),
    ]
    if class_obj:
        subjects = class_obj.subjects or list((class_obj.teachers or {}).keys())
        lines.append(f"Класс: {class_obj.class_name}.")
        lines.append("Предметы класса: " + (", ".join(subjects) if subjects else "пока нет."))
        if class_obj.teachers:
            lines.append("Текущие учителя: " + "; ".join(
                f"{s} — {t}" for s, t in class_obj.teachers.items()
            ) + ".")
        else:
            lines.append("Учителя не назначены.")
        lessons = sorted(class_obj.bells.keys(), key=lambda x: int(x) if str(x).isdigit() else 99) if class_obj.bells else []
        lines.append(f"В классе сейчас {len(lessons)} уроков со звонками (номера: {', '.join(lessons) if lessons else 'нет'}).")
        lines.append(f"Дата каникул класса: {class_obj.holidays}.")
    else:
        lines.append("Пользователь НЕ состоит в классе — действия с классом недоступны.")
    # Личные кнопки и лимит — нужны действиям create/delete/move_button.
    try:
        personal_btns = get_personal_buttons(user.user_id)
        limit = int(getattr(user, "max_personal_buttons", 1) or 1)
        if personal_btns:
            lines.append(
                "Личные кнопки пользователя (" + str(len(personal_btns)) + "/" + str(limit) + "): "
                + ", ".join(b.name for b in personal_btns) + "."
            )
        else:
            lines.append(f"Личных кнопок нет (лимит {limit}).")
    except Exception:
        pass
    # Текущий режим ИИ — нужен действию ai_mode.
    try:
        persona = getattr(user, "ai_persona", "normal")
        if persona not in AI_PERSONA_MODES:
            persona = "normal"
        lines.append(
            f"Текущий режим ИИ-чата: {AI_PERSONA_MODES[persona]['title']} "
            "(доступны: Обычный / Хамло / Тепло)."
        )
    except Exception:
        pass
    if getattr(user, "city", None):
        lines.append(f"Город пользователя (для погоды): {user.city}.")
    else:
        lines.append("Город пользователя не установлен — погода недоступна.")
    return "\n".join(lines)


def _automation_system_prompt(context_text, is_admin):
    admin_block = (
        "Пользователь ЯВЛЯЕТСЯ администратором класса: ему доступны все изменяющие действия."
        if is_admin
        else "Пользователь НЕ администратор: изменяющие действия (add_homework, delete_homework, replace_teachers, add_subject, remove_subject, edit_schedule, edit_bell, set_holidays, send_class_message) ему ЗАПРЕЩЕНЫ. Если он просит именно их — верни действие \"clarify\" с вопросом-напоминанием, что менять класс может только админ."
    )
    return (
        "Ты — модуль автоматизации школьного Telegram-бота. Твоя задача: превратить "
        "запрос пользователя на русском языке в ОДИН строгий JSON-объект с действием.\n\n"
        f"КОНТЕКСТ:\n{context_text}\n\n"
        f"{admin_block}\n\n"
        "ДОСТУПНЫЕ ДЕЙСТВИЯ (верни ровно одно):\n"
        '1) {"action":"add_homework","subject":"<предмет>","date":"ГГГГ-ММ-ДД","text":"<задание>"} — добавить домашнее задание. Используй существующее название предмета класса, если пользователь имел в виду похожий предмет ("русский" -> "Русский язык"). Относительные даты ("пятница этой недели", "завтра") переводи в конкретную дату по контексту.\n'
        '2) {"action":"delete_homework","subject":"<предмет>","date":"ГГГГ-ММ-ДД или null"} — удалить ДЗ (null = всё ДЗ предмета).\n'
        '3) {"action":"replace_teachers","items":[{"subject":"<предмет>","teacher":"<ФИО>"}]} — заменить/назначить учителей на один или несколько предметов.\n'
        '4) {"action":"add_subject","subject":"<предмет>","teacher":"<ФИО или null>"} — добавить новый предмет.\n'
        '5) {"action":"remove_subject","subject":"<предмет>"} — удалить предмет.\n'
        '6) {"action":"edit_schedule","day":"<Понедельник..Воскресенье>","content":"<номер. предмет через \\n>"} — заменить расписание на день.\n'
        '7) {"action":"edit_bell","lesson":<номер урока числом>,"start":"ЧЧ:ММ","end":"ЧЧ:ММ"} — задать время звонков урока. end обязан быть позже start.\n'
        '8) {"action":"set_holidays","date":"ГГГГ-ММ-ДД"} — дата начала каникул.\n'
        '9) {"action":"create_timer","date":"ГГГГ-ММ-ДД","time":"ЧЧ:ММ","text":"<текст напоминания>","kind":"timer|wish","repeat_daily":false} — таймер/напоминание/ПОЖЕЛАНИЕ ПО РАСПИСАНИЮ (доступно всем). Если пользователь говорит «через N минут/часов» — используй вместо даты поле in_minutes: {"action":"create_timer","in_minutes":<целое число минут>,"text":"<текст>"}. Разрешено передавать date как «today»/«tomorrow» — исполнитель сам посчитает дату. ПОЛЕ kind: "timer" (по умолчанию) — обычное напоминание; "wish" — когда пользователь просит бота ПОЖЕЛАТЬ/сказать/поздравить его самого («пожелай мне спокойной ночи в 23:00», «говори мне доброе утро в 7:00», «поздравь меня с наступающим в 12:00») — в text запиши САМО ПОЖЕЛАНИЕ живой фразой с уместным эмодзи (например «Спокойной ночи! Пусть тебе приснятся самые добрые сны 🌙»), а не служебный текст. ПОЛЕ repeat_daily: true — ТОЛЬКО если сказано «каждый день», «каждое утро», «всегда в это время»; иначе false.\n'
        '10) {"action":"send_class_message","text":"<сообщение>"} — объявление всему классу (только админ).\n'
        '11) {"action":"show_homework","subject":"<предмет или null>","date":"ГГГГ-ММ-ДД или null"} — показать ДЗ.\n'
        '12) {"action":"show_schedule","day":"<день или null>"} — показать расписание.\n'
        '13) {"action":"show_teachers"} — показать учителей.\n'
        '14) {"action":"show_bells"} — показать звонки.\n'
        '15) {"action":"show_holidays"} — показать каникулы.\n'
        '16) {"action":"show_weather"} — показать погоду СЕЙЧАС в городе пользователя.\n'
        '17) {"action":"weather_forecast","days":<1..3>} — ПРОГНОЗ погоды: «погода на завтра» = days:1, «на 3 дня» = days:3.\n'
        '18) {"action":"open_section","section":"<раздел или название кнопки>"} — ОТКРЫТЬ раздел интерфейса бота (автоматизация «нажимает» кнопку за пользователя). Стандартные разделы: меню, дз, добавить дз, удалить дз, учителя, расписание, звонки, каникулы, таймер, анонимное сообщение, мои анонимные сообщения, классы, мои кнопки, звезды, ии (чат с AI), погода, инструкция, код класса, написать админу, настройки. ТАКЖЕ можно открыть любую ЛИЧНУЮ, ГЛОБАЛЬНУЮ или КЛАССНУЮ кнопку, передав её точное название в section. Используй, когда пользователь просит открыть/зайти/показать раздел или кнопку («открой дз», «включи чат с ии»). Не используй, если пользователь просит ДАННЫЕ (ДЗ/учителей) — для этого есть show_*.\n'
        '19) {"action":"show_vip"} — показать активные VIP-подписки пользователя и даты их окончания.\n'
        '20) {"action":"send_anon","name":"<имя или @username получателя>","text":"<сообщение>"} — ОТПРАВИТЬ анонимное сообщение однокласснику (доступно всем; имя получателя ищем в классе пользователя).\n'
        '21) {"action":"create_button","name":"<название>","content":"<текст или ссылка>","type":"text|url"} — СОЗДАТЬ личную кнопку пользователя (тип по умолчанию text). Учитывай лимит личных кнопок из контекста.\n'
        '22) {"action":"delete_button","name":"<название>"} — УДАЛИТЬ личную кнопку пользователя.\n'
        '23) {"action":"hide_button","name":"<название>"} — СКРЫТЬ любую кнопку меню (стандартную/личную/глобальную/классную).\n'
        '24) {"action":"show_button","name":"<название>"} — ВЕРНУТЬ скрытую кнопку в меню.\n'
        '25) {"action":"rename_button","name":"<текущее название>","new_name":"<новое название>"} — ПЕРЕИМЕНОВАТЬ кнопку в меню.\n'
        '26) {"action":"move_button","name":"<название>","position":<число>} — ПЕРЕМЕСТИТЬ кнопку на указанное место (position = 1 — первая).\n'
        '27) {"action":"ai_mode","mode":"normal|hamlo|warm"} — показать (mode=null) или СМЕНИТЬ режим личности ИИ-чата: обычный / хамло / тепло. «включи режим хамло» → mode:"hamlo".\n'
        '28) {"action":"clarify","question":"<один конкретный вопрос>"} — если данных не хватает или запрос неоднозначен.\n'
        '29) {"action":"none","answer":"<ответ на общий вопрос>"} — запрос не про школьные действия: просто ответь по-русски.\n'
        '30) {"action":"add_homework_many","items":[{"subject":"<предмет>","date":"ГГГГ-ММ-ДД","text":"<задание>"}]} — добавить НЕСКОЛЬКО заданий за один раз. ОБЯЗАТЕЛЬНО используй, когда пользователь перечисляет ДЗ по нескольким предметам И/ИЛИ нескольким датам в одном сообщении — и верни ВСЕ элементы, ни один не теряй. Пример: «напиши дз на сегодня по русскому стр 129 на завтра математика стр 120 и литература послезавтра стр 129» → 3 элемента: русский/сегодня/стр 129, математика/завтра/стр 120, литература/послезавтра/стр 129.\n'
        '31) {"action":"set_birthday","date":"ГГГГ-ММ-ДД"} — установить/изменить ДЕНЬ РОЖДЕНИЯ ПОЛЬЗОВАТЕЛЯ (доступно всем, меняет свой профиль). Формат ГГГГ-ММ-ДД; если год не назван — верни clarify с одним вопросом про год рождения.\n'
        '32) {"action":"show_class_code"} — показать код класса пользователя (доступно всем).\n\n'
        "ПРАВИЛА:\n"
        "- Отвечай ТОЛЬКО JSON-объектом, без пояснений и markdown.\n"
        "- Не выдумывай даты: считай их строго от сегодняшней даты из контекста. «Пятница этой недели» — пятница текущей недели (даже если она уже прошла — берём ближайшую ПЯТНИЦУ ТЕКУЩЕЙ недели, а не следующую).\n"
        "- Если пользователь пишет «стр 45»/«параграф 12» — это текст задания.\n"
        "- «замени учителей по биологии и географии» без имён — это clarify с вопросом про имена (по каждому предмету).\n"
        "- Если пользователь просит несколько действий сразу (например, заменить трёх учителей) — упаковывай всё в одно действие (replace_teachers со списком items).\n"
        "- НЕСКОЛЬКО ДЗ ЗА РАЗ: перечисление заданий по разным предметам/датам — это ОДНО действие add_homework_many со ВСЕМИ элементами. Не выкидывай ни одно задание и не добавляй лишние.\n"
        "- ПОЖЕЛАНИЯ ПО РАСПИСАНИЮ: «пожелай мне спокойной ночи в 23:00» = create_timer kind:\"wish\" time:\"23:00\" с живой фразой-пожеланием в text; «желай доброе утро в 7:00 каждый день» = то же + repeat_daily:true. Обычные напоминания («напомни…») = kind:\"timer\".\n"
        "- ДЕНЬ РОЖДЕНИЯ: «поменяй мой день рождения на 30 мая 2008», «мой днюха 14 марта 2009» = set_birthday с датой ГГГГ-ММ-ДД; если год не назван — clarify.\n"
        "- КОД КЛАССА: «скажи код класса», «какой у нас код?», «покажи код» = show_class_code.\n"
        "- Даты только в формате ГГГГ-ММ-ДД, время — ЧЧ:ММ (24-часовое).\n"
        "- Для edit_bell: если end <= start — верни clarify с объяснением.\n"
        "- ПОНИМАЙ СИНОНИМЫ ТОЧНО: «домашка»=ДЗ, «звонки»=время уроков, «училки/преподы»=учителя, «параграф»=задание. Название предмета сопоставляй с существующим по смыслу («русский»→«Русский язык», «матеша»→«Математика»).\n"
        "- КНОПКИ И ИНТЕРФЕЙС: «создай кнопку …»=create_button, «удали кнопку»=delete_button, «спрячь/скрой кнопку»=hide_button, «верни/покажи кнопку»=show_button, «переименуй кнопку»=rename_button, «поставь кнопку на 2 место/перемести»=move_button. Для ЛИЧНОЙ кнопки нужен текст/ссылка — если пользователь не дал содержимое, уточни через clarify.\n"
        "- АНОНИМКА: «напиши анонимно Пете …», «отправь анонимку» = send_anon с именем получателя и текстом. Если текст сообщения не дан — clarify.\n"
        "- ПОГОДА: «какая погода?» = show_weather (сейчас); «погода на завтра» = weather_forecast days:1; «погода на 3 дня» = weather_forecast days:3.\n"
        "- РЕЖИМ ИИ: «включи хамло/режим хамло» = ai_mode mode:hamlo; «режим тепло» = mode:warm; «обычный режим» = mode:normal; «какой у меня режим ии?» = ai_mode без mode.\n"
        "- СМЕНА ТЕМЫ: пользователь может в любой момент резко заговорить о другом (сначала про ДЗ, потом «а какая погода?» или «напиши стих»). ВСЕГДА следуй САМОМУ ПОСЛЕДНЕМУ сообщению, не цепляйся за прошлую тему и не спрашивай «так о чём мы». Общий вопрос вне школы — это действие none, а не школьное действие.\n"
        "- Не угадывай: если запрос можно понять двояко — уточни через clarify. Если всё понятно — выполняй без лишних вопросов."
    )


async def _automation_execute_action(update, context, user, class_obj, action):
    """Исполняет JSON-сценарий от DeepSeek. Возвращает текстовый отчёт.

    Все изменяющие действия требуют прав админа класса (честная проверка),
    все читающие доступны каждому. Ошибки не выбрасываются наружу —
    возвращают человекочитаемый отчёт о сбое.
    """
    name = (action.get("action") or "none").strip()
    user_id = user.user_id
    is_admin = bool(class_obj and user_id in (class_obj.admins or []))

    # --- Читающие действия (доступны всем) ---
    if name == "show_teachers":
        if class_obj and class_obj.teachers:
            rows = [f"📚 {s}: {t}" for s, t in class_obj.teachers.items()]
            return "👨‍🏫 Учителя класса:\n\n" + "\n".join(rows), True
        return "👨‍🏫 Учителя не добавлены.", True

    if name == "show_bells":
        return get_bells_info(class_obj, user), True

    if name == "show_holidays":
        return get_holidays_count(class_obj, user), True

    if name == "show_weather":
        if not getattr(user, "city", None):
            return ("🏙 Город не установлен. Откройте ⚙️ Настройки → 🌦 Настройки погоды, "
                    "после этого я смогу показывать погоду.", True)
        text = await weather_current_text(user.city)
        return text, True

    if name == "show_vip":
        subs = getattr(user, "subscriptions", {}) or {}
        lines = ["👑 Ваши активные VIP-подписки:"]
        now = datetime.utcnow()
        for key, raw_val in sorted(subs.items()):
            until = _parse_sub_until(raw_val)
            if until is None or until <= now:
                continue
            lines.append(f"• {_sub_display_name(key, raw_val)} — до {_fmt_sub_until(until)}")
        if len(lines) == 1:
            return ("👑 Активных VIP-подписок нет.", True)
        return "\n".join(lines), True

    if name == "show_schedule":
        day = (action.get("day") or "").strip()
        if not class_obj:
            return "Вы не состоите в классе — расписание недоступно.", True
        if day:
            return f"📅 {day}:\n{get_day_schedule(class_obj, day)}", True
        week_days = ["Понедельник", "Вторник", "Среда", "Четверг",
                     "Пятница", "Суббота", "Воскресенье"]
        parts = [f"📅 Расписание — {class_obj.class_name}"]
        for d in week_days:
            parts.append(f"\n📌 {d}:\n{get_day_schedule(class_obj, d)}")
        return "\n".join(parts), True

    if name == "show_homework":
        if not class_obj:
            return "Вы не состоите в классе — ДЗ недоступно.", True
        subject = (action.get("subject") or "").strip() or None
        date_str = (action.get("date") or "").strip() or None
        if date_str:
            homework = get_homework_for_date(class_obj, date_str)
            if subject:
                homework = {subject: homework[subject]} if subject in homework else {}
            if not homework:
                return f"📝 ДЗ на {date_str} не найдено.", True
            return format_homework(homework, date_str), True
        if subject:
            hw = class_obj.homework.get(subject)
            if not hw:
                return f"📝 По предмету «{subject}» ДЗ не задано.", True
            return format_homework({subject: hw}), True
        if not class_obj.homework:
            return "📝 Домашнее задание не задано.", True
        return format_homework(class_obj.homework), True

    if name == "show_class_code":
        if not user.class_code:
            return (
                "🔑 Вы не состоите в классе. Создайте класс или присоединитесь "
                "по коду через «🎓 Управление классами».", True
            )
        _code_obj = get_class_by_code(user.class_code)
        _class_label = f" «{_code_obj.class_name}»" if _code_obj else ""
        return (
            f"🔑 Код вашего класса{_class_label}:\n\n`{user.class_code}`\n\n"
            "Поделитесь этим кодом с одноклассниками!"
        ), True

    if name == "none":
        return (action.get("answer") or "Готово.").strip(), True

    if name == "create_timer":
        # Доступно всем. Полностью повторяет логику timer_set_text_handler:
        # сохранение в TIMERS_FILE + планирование через JobQueue + страховка.
        #
        # ИСПРАВЛЕНО (таймер в автоматизации не работал): раньше DeepSeek был
        # обязан сам вычислить дату/время, и любая неточность (относительные
        # «через 20 минут», «сегодня», время в прошлом) ломала парсинг или
        # создавала таймер в прошлом. Теперь executor сам умеет:
        #   1) {"in_minutes": N} — относительное время от СЕЙЧАС (локального);
        #   2) date = "today"/"tomorrow"/"сегодня"/"завтра" — резолвится здесь;
        #   3) пустая дата + заполненное время = сегодня (или завтра, если
        #      время уже прошло);
        #   4) валидация «время в будущем»: прошедшее время НЕ сохраняется,
        #      возвращается честный уточняющий вопрос.
        date_str = (action.get("date") or "").strip()
        time_str = (action.get("time") or "").strip()
        text = (action.get("text") or "").strip()

        if not text:
            return "❓ Текст напоминания пустой. Что напомнить?", False
        rejected = await reject_if_forbidden_chars(update, text, AI_AUTOMATION)
        if rejected is not None:
            return None, False

        tz_offset = getattr(user, "timezone", 3) if user else 3
        local_now = _now_utc() + timedelta(hours=tz_offset)

        # Формат 1: относительное время «через N минут».
        in_minutes_raw = action.get("in_minutes")
        if in_minutes_raw is not None and str(in_minutes_raw).strip() not in ("", "null", "None"):
            try:
                in_minutes = int(float(str(in_minutes_raw).strip()))
            except (TypeError, ValueError):
                return "❓ Количество минут не распознано. Назовите число минут.", False
            if in_minutes <= 0:
                return "❓ Интервал должен быть больше 0 минут. Уточните.", False
            if in_minutes > 60 * 24 * 30:
                return "❓ Слишком большой интервал (максимум 30 дней). Уточните.", False
            target = local_now + timedelta(minutes=in_minutes)
            date_str = target.strftime("%Y-%m-%d")
            time_str = target.strftime("%H:%M")
        else:
            # Формат 2: дата + время (с поддержкой «сегодня»/«завтра»).
            date_norm = date_str.lower().strip()
            if date_norm in ("today", "сегодня", "сегодняшний день"):
                date_str = local_now.strftime("%Y-%m-%d")
            elif date_norm in ("tomorrow", "завтра"):
                date_str = (local_now + timedelta(days=1)).strftime("%Y-%m-%d")
            if not date_str and time_str:
                # Только время — считаем «сегодня», а если оно уже прошло — «завтра».
                today_time = datetime.strptime(
                    f"{local_now.strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %H:%M"
                )
                base_date = (
                    local_now if today_time > local_now else local_now + timedelta(days=1)
                )
                date_str = base_date.strftime("%Y-%m-%d")
            try:
                target = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            except ValueError:
                return "❓ Дата/время напоминания не распознаны. Назовите их точнее.", False
            # Честная проверка: таймер в прошлом не создаём.
            if target <= local_now:
                return (
                    f"❓ Время {date_str} {time_str} уже прошло (сейчас {local_now.strftime('%H:%M')}). "
                    "Назовите время в будущем.", False
                )

        timers = load_data(TIMERS_FILE, {})
        timer_id = generate_timer_id()
        # НОВОЕ: kind="wish" — пожелание по расписанию (бот присылает живую
        # фразу вместо «⏰ Напоминание»); repeat_daily — повторять КАЖДЫЙ ДЕНЬ.
        kind_raw = str(action.get("kind") or "timer").strip().lower()
        kind = "wish" if kind_raw in ("wish", "пожелание", "greeting") else "timer"
        repeat_daily_raw = action.get("repeat_daily")
        if isinstance(repeat_daily_raw, str):
            repeat_daily = repeat_daily_raw.strip().lower() in ("true", "да", "1", "yes")
        else:
            repeat_daily = bool(repeat_daily_raw)
        # Пожелание без текста — это clarify, а не таймер.
        if kind == "wish" and not text:
            return (
                "❓ Я готов желать вам что-то хорошее по расписанию, но вы не "
                "сказали, ЧТО желать. Скажите, например: «пожелай мне спокойной "
                "ночи в 23:00».", False
            )
        timers[timer_id] = {
            "user_id": user_id,
            "target_date": date_str,
            "target_time": time_str,
            "text": text,
            "is_active": True,
            "kind": kind,
            "repeat_daily": repeat_daily,
            "created_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        save_data(TIMERS_FILE, timers)
        # Немедленное планирование JobQueue + страховочный тикер подхватят
        # таймер даже если джоба не сработает.
        try:
            schedule_timer_job(context.application, timer_id, timers[timer_id])
        except Exception as e:
            logger.error(f"automation create_timer schedule: {e}")
        when_line = f"📅 {date_str} в {time_str}"
        if repeat_daily:
            when_line += " (и каждый день в это время)"
        if kind == "wish":
            return (
                f"✅ Хорошо! Я отправлю это вам по расписанию.\n\n"
                f"⏰ {when_line}\n💬 {text}"
            ), True
        return (
            f"✅ Таймер установлен!\n\n⏰ {when_line}\n📝 {text}"
        ), True

    if name == "weather_forecast":
        # Доступно всем: прогноз погоды на завтра (days=1) или до 3 дней.
        if not getattr(user, "city", None):
            return ("🏙 Город не установлен. Откройте ⚙️ Настройки → 🌦 Настройки погоды, "
                    "после этого я смогу показывать погоду.", True)
        try:
            days = int(action.get("days") or 1)
        except (TypeError, ValueError):
            days = 1
        days = max(1, min(days, 3))
        text = await weather_forecast_text(user.city, days)
        return text, True

    if name == "ai_mode":
        # Показ/смена режима личности ИИ-чата (normal / hamlo / warm).
        raw_mode = str(action.get("mode") or "").strip().lower()
        current = getattr(user, "ai_persona", "normal")
        if current not in AI_PERSONA_MODES:
            current = "normal"
        if not raw_mode or raw_mode in ("show", "null", "none"):
            return (
                f"🎭 Ваш текущий режим ИИ: {AI_PERSONA_MODES[current]['title']} — "
                f"{AI_PERSONA_MODES[current]['description']}.\n"
                "Сменить: скажите, например, «включи режим хамло» или «включи режим тепло»."
            ), True
        aliases = {
            "normal": "normal", "обычный": "normal", "обычный помощник": "normal",
            "стандарт": "normal", "standart": "normal",
            "hamlo": "hamlo", "хамло": "hamlo", "хам": "hamlo", "грубый": "hamlo",
            "warm": "warm", "тепло": "warm", "тёплый": "warm", "теплый": "warm",
            "нежный": "warm", "милашка": "warm",
        }
        new_mode = aliases.get(raw_mode)
        if new_mode is None:
            return (
                "❓ Режим не распознан. Доступны: «обычный», «хамло», «тепло».", False
            )
        user.ai_persona = new_mode
        save_user(user)
        # Мгновенно применяем к открытому диалогу чата ИИ (если он есть).
        conv = user_conversations.get(str(user_id))
        if conv and conv[0].get("role") == "system":
            conv[0]["content"] = _ai_chat_full_prompt(new_mode)
        return (
            f"🎭 Готово! Режим ИИ переключён: {AI_PERSONA_MODES[new_mode]['title']} — "
            f"{AI_PERSONA_MODES[new_mode]['description']}.\n"
            "Заходите в «🤖 DEVORKS+ai» и общайтесь."
        ), True

    if name == "send_anon":
        # Анонимное сообщение участнику класса (или пользователю бота по нику).
        target_name = str(action.get("name") or action.get("to") or "").strip()
        anon_text = str(action.get("text") or action.get("message") or "").strip()
        if not target_name:
            return "❓ Кому отправить анонимное сообщение? Назовите имя получателя.", False
        if not anon_text:
            return "❓ Текст анонимного сообщения пустой. Что написать?", False
        rejected = await reject_if_forbidden_chars(update, anon_text, AI_AUTOMATION)
        if rejected is not None:
            return None, False

        def _name_variants(u):
            vals = []
            if u:
                if getattr(u, "first_name", None):
                    vals.append(str(u.first_name).strip().lower())
                if getattr(u, "username", None):
                    vals.append("@" + str(u.username).lstrip("@").strip().lower())
            return vals

        def _match_score(u):
            """0 = нет совпадения; 1 = точное имя/ник; 2 = подстрока."""
            target = target_name.lower().lstrip("@")
            variants = _name_variants(u)
            if any(v == target or v == "@" + target for v in variants):
                return 1
            if target and any(target in v for v in variants):
                return 2
            return 0

        candidates = []
        if class_obj:
            pool = []
            for member_id in set(class_obj.students or []):
                if str(member_id) == str(user_id):
                    continue
                member = get_user(str(member_id))
                if member:
                    pool.append(member)
            candidates = [m for m in pool if _match_score(m) > 0]
        else:
            # Пользователь вне класса: ищем среди всех пользователей бота
            # только по ТОЧНОМУ совпадению имени или @username.
            all_users = load_users() or {}
            pool = []
            for uid, member in all_users.items():
                if str(uid) == str(user_id):
                    continue
                if _match_score(member) == 1:
                    pool.append(member)
            candidates = pool

        if not candidates:
            where = "в вашем классе" if class_obj else "среди пользователей бота"
            return (
                f"❓ Не нашёл получателя «{target_name}» {where}. "
                "Проверьте имя (или @username) и попробуйте ещё раз.", False
            )
        if len(candidates) > 1:
            listing = "\n".join(
                f"• {m.first_name}" + (f" (@{m.username})" if getattr(m, "username", None) else "")
                for m in candidates[:8]
            )
            return (
                "❓ Под это имя подходят несколько человек:\n"
                f"{listing}\n\nУточните, кому именно — напишите имя точнее или @username.",
                False
            )
        target_user = candidates[0]
        if str(target_user.user_id) == str(user_id):
            return "🚫 Нельзя отправить анонимное сообщение самому себе.", True
        try:
            target_chat_id = int(target_user.user_id)
        except (TypeError, ValueError):
            return "❓ Некорректный получатель. Попробуйте ещё раз.", False

        anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})
        msg_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        anonymous_messages[msg_id] = {
            'from_user_id': user_id,
            'to_user_id': str(target_user.user_id),
            'message': anon_text,
            'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M"),
            'sender_viewed': False,
        }
        save_data(ANONYMOUS_MESSAGES_FILE, anonymous_messages)
        view_sender_price = PRICES.get('view_sender', 60)
        try:
            await context.bot.send_message(
                chat_id=target_chat_id,
                text=(
                    f"🕵️ Вам пришло анонимное сообщение:\n\n{anon_text}\n\n"
                    f"💰 Узнать отправителя: {view_sender_price} ⭐"
                ),
            )
        except Exception as e:
            logger.error(f"automation send_anon: не удалось доставить: {e}")
            # Сообщение сохранено, но не доставлено — честно сообщаем.
            return (
                "⚠️ Сообщение сохранено, но доставить его не удалось: получатель "
                "не запускал бота или заблокировал его.", True
            )
        return "🕵️ Анонимное сообщение доставлено! Ваше имя не раскрывается.", True

    if name in ("create_button", "delete_button", "hide_button",
                "show_button", "rename_button", "move_button"):
        # Управление ЛИЧНЫМИ кнопками и видимостью/порядком меню.
        # Личные кнопки — личное пространство пользователя: админ класса не нужен.
        btn_name = str(action.get("name") or "").strip()
        if name == "create_button":
            btn_content = str(action.get("content") or "").strip()
            btn_type = str(action.get("type") or "text").strip().lower()
            if btn_type not in ("text", "url"):
                btn_type = "text"
            if not btn_name:
                return "❓ Название кнопки не распознано. Как назвать кнопку?", False
            if not btn_content:
                return (
                    f"❓ Не хватает содержимого кнопки «{btn_name}»: "
                    + ("укажите ссылку (https://…)" if btn_type == "url"
                       else "укажите текст, который бот будет присылать по кнопке."),
                    False,
                )
            if btn_type == "url" and not btn_content.lower().startswith(("http://", "https://")):
                # Как в ручном создании кнопки: дополняем схему автоматически.
                btn_content = "https://" + btn_content
            # Запрет символов: для URL-контента НЕ применяем (в ссылках есть «/»),
            # как и в ручном хендлере create_personal_button_url_handler.
            _forbidden_check_text = btn_name + ("" if btn_type == "url" else "\n" + btn_content)
            rejected = await reject_if_forbidden_chars(update, _forbidden_check_text, AI_AUTOMATION)
            if rejected is not None:
                return None, False
            existing = get_personal_buttons(user_id)
            if any(b.name.strip().lower() == btn_name.lower() for b in existing):
                return f"❓ Кнопка «{btn_name}» уже существует. Выберите другое название.", False
            limit = int(getattr(user, "max_personal_buttons", 1) or 1)
            if len(existing) >= limit:
                prepaid = int(getattr(user, "prepaid_buttons", 0) or 0)
                if prepaid <= 0:
                    return (
                        f"🚫 Достигнут лимит личных кнопок ({len(existing)}/{limit}).\n"
                        "Дополнительные слоты покупаются в ⭐ Звезды (кнопка покупки "
                        "кнопки). После оплаты скажите мне снова — создам кнопку.", True
                    )
                # Оплаченный слот списываем — как в ручном создании кнопки.
                user.prepaid_buttons = max(0, prepaid - 1)
            new_btn = PersonalButton(
                button_id=generate_personal_button_id(),
                user_id=user_id,
                name=btn_name[:64],
                content=btn_content[:4000],
                button_type=btn_type,
            )
            save_personal_button(new_btn)
            # Кнопка в конец личного порядка.
            order = list(getattr(user, "personal_button_order", []) or [])
            if new_btn.button_id not in order:
                order.append(new_btn.button_id)
            user.personal_button_order = order
            save_user(user)
            kind = "ссылка" if btn_type == "url" else "текст"
            return (
                f"✅ Кнопка «{btn_name}» создана ({kind})! Она уже видна в вашем меню.",
                True,
            )

        # Для остальных действий нужна существующая кнопка/имя.
        if not btn_name:
            return "❓ Не указано, с какой кнопкой работать. Назовите её название.", False

        if name == "delete_button":
            target_btn = None
            for b in get_personal_buttons(user_id):
                if b.name.strip().lower() == btn_name.lower():
                    target_btn = b
                    break
            if target_btn is None:
                fuzzy = [b for b in get_personal_buttons(user_id)
                         if btn_name.lower() in b.name.lower()]
                if len(fuzzy) == 1:
                    target_btn = fuzzy[0]
            if target_btn is None:
                return f"❓ Личная кнопка «{btn_name}» не найдена. Проверьте название.", False
            delete_personal_button(target_btn.button_id)
            order = list(getattr(user, "personal_button_order", []) or [])
            if target_btn.button_id in order:
                order.remove(target_btn.button_id)
                user.personal_button_order = order
                save_user(user)
            return f"🗑️ Личная кнопка «{target_btn.name}» удалена.", True

        if name == "hide_button":
            # Скрывать можно ЛЮБУЮ кнопку меню (стандартную, личную, глобальную, классную).
            all_names = get_all_user_button_names(user) or []
            target = next((n for n in all_names if n.strip().lower() == btn_name.lower()), None)
            if target is None:
                fuzzy = [n for n in all_names if btn_name.lower() in n.lower()]
                target = fuzzy[0] if len(fuzzy) == 1 else None
            if target is None:
                return f"❓ Кнопка «{btn_name}» не найдена в вашем меню.", False
            hidden = list(getattr(user, "hidden_buttons", []) or [])
            if target in hidden:
                return f"🙈 Кнопка «{target}» уже скрыта.", True
            hidden.append(target)
            user.hidden_buttons = hidden
            save_user(user)
            return f"🙈 Кнопка «{target}» скрыта из меню. Вернуть: «покажи кнопку {target}».", True

        if name == "show_button":
            hidden = list(getattr(user, "hidden_buttons", []) or [])
            target = next((n for n in hidden if n.strip().lower() == btn_name.lower()), None)
            if target is None:
                fuzzy = [n for n in hidden if btn_name.lower() in n.lower()]
                target = fuzzy[0] if len(fuzzy) == 1 else None
            if target is None:
                return f"👀 Кнопка «{btn_name}» и так не скрыта (или не найдена среди скрытых).", True
            hidden.remove(target)
            user.hidden_buttons = hidden
            save_user(user)
            return f"👀 Кнопка «{target}» снова видна в меню.", True

        if name == "rename_button":
            new_name = str(action.get("new_name") or "").strip()
            if not new_name:
                return "❓ Новое название кнопки не распознано.", False
            rejected = await reject_if_forbidden_chars(update, new_name, AI_AUTOMATION)
            if rejected is not None:
                return None, False
            all_names = get_all_user_button_names(user) or []
            target = next((n for n in all_names if n.strip().lower() == btn_name.lower()), None)
            if target is None:
                fuzzy = [n for n in all_names if btn_name.lower() in n.lower()]
                target = fuzzy[0] if len(fuzzy) == 1 else None
            if target is None:
                return f"❓ Кнопка «{btn_name}» не найдена в вашем меню.", False
            rename_map = dict(getattr(user, "custom_buttons", {}) or {})
            if new_name == target:
                rename_map.pop(target, None)
                result_text = f"🔤 Кнопке «{target}» возвращено оригинальное название."
            else:
                rename_map[target] = new_name[:64]
                result_text = f"🔤 Кнопка «{target}» переименована в «{new_name[:64]}»."
            user.custom_buttons = rename_map
            save_user(user)
            return result_text, True

        if name == "move_button":
            try:
                position = int(action.get("position"))
            except (TypeError, ValueError):
                return "❓ На какое место поставить кнопку? Назовите число (1, 2, 3…).", False
            if position < 1:
                return "❓ Номер места должен быть 1 или больше.", False
            # Вариант 1: стандартная кнопка главного меню (точное или мягкое имя).
            std_target = None
            if btn_name in ALL_MAIN_MENU_BUTTONS:
                std_target = btn_name
            else:
                _low = btn_name.lower()
                _fuzzy_std = [b for b in ALL_MAIN_MENU_BUTTONS if _low in b.lower() or b.lower() in _low]
                if len(_fuzzy_std) == 1:
                    std_target = _fuzzy_std[0]
            if std_target is not None:
                base = [b for b in (getattr(user, "custom_button_order", []) or [])
                        if b in ALL_MAIN_MENU_BUTTONS]
                for b in ALL_MAIN_MENU_BUTTONS:
                    if b not in base:
                        base.append(b)
                base.remove(std_target)
                position = min(position, len(base))
                base.insert(position - 1, std_target)
                user.custom_button_order = base
                save_user(user)
                return f"🔀 Кнопка «{std_target}» теперь {position}-я в меню.", True
            # Вариант 2: личная кнопка (свой блок сверху меню).
            personal = get_personal_buttons(user_id)
            target_btn = next((b for b in personal if b.name.strip().lower() == btn_name.lower()), None)
            if target_btn is None:
                fuzzy = [b for b in personal if btn_name.lower() in b.name.lower()]
                target_btn = fuzzy[0] if len(fuzzy) == 1 else None
            if target_btn is None:
                return (
                    f"❓ Кнопка «{btn_name}» не найдена. Перемещать можно стандартные "
                    "кнопки меню и ваши личные кнопки.", False,
                )
            ids = [b.button_id for b in personal]
            if target_btn.button_id in ids:
                ids.remove(target_btn.button_id)
            position = min(position, len(ids))
            ids.insert(position - 1, target_btn.button_id)
            update_personal_button_order(user_id, ids)
            return (
                f"🔀 Личная кнопка «{target_btn.name}» теперь {position}-я среди "
                "ваших личных кнопок (их блок всегда выше стандартных).", True,
            )

    if name == "set_birthday":
        # ЛИЧНОЕ действие (доступно всем): меняет СВОЙ профиль, а не класс —
        # поэтому находится ВНЕ админ-блока. Полностью повторяет валидацию
        # ручного ввода (save_birthday_handler): ГГГГ-ММ-ДД, не в будущем.
        date_str = (action.get("date") or "").strip()
        try:
            bd = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return (
                "❓ Дата рождения не распознана. Назовите её полностью, вместе "
                "с годом: например «30 мая 2008» или «2008-05-30».", False
            )
        if bd.date() > datetime.now().date():
            return "❓ День рождения не может быть в будущем. Проверьте дату.", False
        user.birthday = date_str
        save_user(user)
        # Поздравления по расписанию шлёт единый тикер — джобу переназначать
        # не нужно, но чистим legacy run_daily-джобы (как ручной ввод делает).
        try:
            schedule_user_birthday_job(context.application, user)
        except Exception as e:
            logger.error(f"automation set_birthday reschedule: {e}")
        days_left = get_days_until_birthday(date_str)
        days_note = ""
        if days_left is not None:
            if days_left == 0:
                days_note = "\n🎉 Кстати, это сегодня! С днём рождения!"
            else:
                days_note = f"\n⏳ До дня рождения: {days_left} дн."
        return f"🎂 День рождения обновлён: {date_str}.{days_note}", True

    if name == "clarify":
        q = (action.get("question") or "Уточните, пожалуйста, ваш запрос.").strip()
        return f"❓ {q}", False  # False — диалог продолжается, ничего не «завершено»

    # --- Изменяющие действия (только админ класса) ---
    if name in {
        "add_homework", "add_homework_many", "delete_homework",
        "replace_teachers", "add_subject", "remove_subject", "edit_schedule",
        "edit_bell", "set_holidays", "send_class_message",
    }:
        if not class_obj:
            return "🚫 Вы не состоите в классе — изменять нечего.", True
        if not is_admin:
            return ("🚫 Изменять ДЗ/учителей/расписание может только администратор класса. "
                    "Попросите админа или используйте читающие запросы.", True)

        if name == "add_homework_many":
            # НОВОЕ: несколько заданий за один запрос («русский на сегодня стр 129,
            # на завтра математика стр 120 и литература послезавтра стр 55»). Раньше
            # исполнялся только ОДИН предмет и остальное молча терялось.
            items = action.get("items") or []
            if not isinstance(items, list):
                items = []
            # Совместимость: если модель вернула одиночные поля — соберём из них один элемент.
            if not items and (action.get("subject") or action.get("date")):
                items = [action]
            if not items:
                return (
                    "❓ Не распознал ни одного задания. Перечислите предмет, дату и текст, "
                    "например: «русский на завтра страница 45».", False
                )
            added_lines = []
            problems = []
            for item in items:
                if not isinstance(item, dict):
                    problems.append("нераспознанный элемент списка")
                    continue
                subject = str(item.get("subject") or "").strip()
                date_str_i = str(item.get("date") or "").strip()
                hw_text_i = str(item.get("text") or "").strip()
                if not subject or not date_str_i or not hw_text_i:
                    problems.append("неполный элемент (нужны предмет, дата и текст)")
                    continue
                try:
                    datetime.strptime(date_str_i, "%Y-%m-%d")
                except ValueError:
                    problems.append(f"дата «{date_str_i}» не распознана ({subject})")
                    continue
                rejected_i = await reject_if_forbidden_chars(update, hw_text_i, AI_AUTOMATION)
                if rejected_i is not None:
                    return None, False
                if subject not in (class_obj.subjects or []):
                    class_obj.subjects.append(subject)
                class_obj.homework.setdefault(subject, []).append({
                    "text": hw_text_i,
                    "date": date_str_i,
                    "added_by": user_id,
                    "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                })
                added_lines.append(f"📚 {subject} — 📅 {date_str_i}: {hw_text_i}")
            if added_lines:
                classes = load_classes()
                classes[class_obj.class_code] = class_obj
                save_classes(classes)
            if not added_lines:
                return "❓ Ни одно задание не удалось добавить: " + "; ".join(problems), False
            report = f"✅ Домашние задания добавлены ({len(added_lines)}):\n\n" + "\n".join(added_lines)
            if problems:
                report += "\n\n⚠️ Не добавлено: " + "; ".join(problems)
            return report, True

        if name == "add_homework":
            subject = (action.get("subject") or "").strip()
            date_str = (action.get("date") or "").strip()
            hw_text = (action.get("text") or "").strip()
            if not subject or not date_str or not hw_text:
                return "❓ Не хватает данных для ДЗ (предмет/дата/текст).", False
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                return f"❓ Дата «{date_str}» не распознана. Назовите дату точнее.", False
            rejected = await reject_if_forbidden_chars(update, hw_text, AI_AUTOMATION)
            if rejected is not None:
                return None, False
            # Автоматически добавляем предмет в список предметов класса, если его нет.
            if subject not in (class_obj.subjects or []):
                class_obj.subjects.append(subject)
            class_obj.homework.setdefault(subject, []).append({
                "text": hw_text,
                "date": date_str,
                "added_by": user_id,
                "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            classes = load_classes()
            classes[class_obj.class_code] = class_obj
            save_classes(classes)
            return (
                f"✅ Домашнее задание добавлено!\n\n📚 Предмет: {subject}\n"
                f"📅 Дата: {date_str}\n📝 Задание: {hw_text}"
            ), True

        if name == "delete_homework":
            subject = (action.get("subject") or "").strip()
            date_str = (action.get("date") or "").strip() or None
            if subject not in (class_obj.homework or {}):
                return f"📝 По предмету «{subject}» ДЗ не найдено.", True
            before = len(class_obj.homework[subject])
            if date_str:
                class_obj.homework[subject] = [
                    item for item in class_obj.homework[subject]
                    if item.get("date") != date_str
                ]
            else:
                class_obj.homework[subject] = []
            removed = before - len(class_obj.homework[subject])
            if not class_obj.homework[subject]:
                del class_obj.homework[subject]
            classes = load_classes()
            classes[class_obj.class_code] = class_obj
            save_classes(classes)
            return f"🗑️ Удалено записей ДЗ по «{subject}»: {removed}" + (f" (дата {date_str})" if date_str else ""), True

        if name == "replace_teachers":
            items = action.get("items") or []
            if not isinstance(items, list) or not items:
                return "❓ Не указано, кого и кем заменять. Назовите предметы и имена учителей.", False
            lines = ["✅ Учителя обновлены:"]
            for item in items:
                subject = str(item.get("subject") or "").strip()
                teacher = str(item.get("teacher") or "").strip()
                if not subject or not teacher:
                    continue
                if subject not in (class_obj.subjects or []):
                    class_obj.subjects.append(subject)
                class_obj.teachers[subject] = teacher
                lines.append(f"📚 {subject} — {teacher}")
            if len(lines) == 1:
                return "❓ Не удалось распознать ни одного предмета с именем учителя.", False
            classes = load_classes()
            classes[class_obj.class_code] = class_obj
            save_classes(classes)
            return "\n".join(lines), True

        if name == "add_subject":
            subject = (action.get("subject") or "").strip()
            teacher = (action.get("teacher") or "").strip()
            if not subject:
                return "❓ Название предмета не распознано.", False
            if subject not in (class_obj.subjects or []):
                class_obj.subjects.append(subject)
            if teacher:
                class_obj.teachers[subject] = teacher
            classes = load_classes()
            classes[class_obj.class_code] = class_obj
            save_classes(classes)
            extra = f" (учитель: {teacher})" if teacher else ""
            return f"✅ Предмет «{subject}» добавлен{extra}.", True

        if name == "remove_subject":
            subject = (action.get("subject") or "").strip()
            if subject in (class_obj.subjects or []):
                class_obj.subjects.remove(subject)
            class_obj.teachers.pop(subject, None)
            class_obj.homework.pop(subject, None)
            classes = load_classes()
            classes[class_obj.class_code] = class_obj
            save_classes(classes)
            return f"🗑️ Предмет «{subject}» удалён вместе с учителем и ДЗ.", True

        if name == "edit_schedule":
            day = (action.get("day") or "").strip().capitalize()
            content = (action.get("content") or "").strip()
            week_days = ["Понедельник", "Вторник", "Среда", "Четверг",
                         "Пятница", "Суббота", "Воскресенье"]
            if day not in week_days:
                return f"❓ День недели «{day}» не распознан.", False
            if not content:
                return "❓ Содержимое расписания пустое.", False
            rejected = await reject_if_forbidden_chars(update, content, AI_AUTOMATION)
            if rejected is not None:
                return None, False
            class_obj.schedule[day] = content
            classes = load_classes()
            classes[class_obj.class_code] = class_obj
            save_classes(classes)
            return f"✅ Расписание на {day} обновлено:\n{content}", True

        if name == "edit_bell":
            try:
                lesson = str(int(action.get("lesson")))
            except (TypeError, ValueError):
                return "❓ Номер урока не распознан.", False
            start = (action.get("start") or "").strip()
            end = (action.get("end") or "").strip()
            try:
                s_t = datetime.strptime(start, "%H:%M")
                e_t = datetime.strptime(end, "%H:%M")
            except ValueError:
                return f"❓ Время «{start}–{end}» не распознано (нужно ЧЧ:ММ).", False
            if e_t <= s_t:
                return "❓ Время окончания должно быть позже начала. Уточните времена.", False
            class_obj.bells[lesson] = {"start": start, "end": end}
            classes = load_classes()
            classes[class_obj.class_code] = class_obj
            save_classes(classes)
            return f"✅ Звонки {lesson}-го урока: {start} — {end}.", True

        if name == "set_holidays":
            date_str = (action.get("date") or "").strip()
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                return f"❓ Дата каникул «{date_str}» не распознана.", False
            class_obj.holidays = date_str
            classes = load_classes()
            classes[class_obj.class_code] = class_obj
            save_classes(classes)
            return f"✅ Дата каникул установлена: {date_str}.", True

        if name == "send_class_message":
            text = (action.get("text") or "").strip()
            if not text:
                return "❓ Текст сообщения пустой.", False
            rejected = await reject_if_forbidden_chars(update, text, AI_AUTOMATION)
            if rejected is not None:
                return None, False
            bot = context.bot
            sent, failed = 0, 0
            for member_id in set(class_obj.students or []):
                try:
                    await bot.send_message(
                        chat_id=int(member_id),
                        text=f"📢 Объявление от админа класса:\n\n{text}",
                    )
                    sent += 1
                except Exception:
                    failed += 1
            return f"📢 Объявление отправлено: {sent} получено, {failed} не доставлено.", True

    return f"Неизвестное действие «{name}».", True


def _automation_help_text():
    return (
        "🪄 **Автоматизация**\n\n"
        "Напишите (или СКАЖИТЕ ГОЛОСОМ) одним сообщением, что сделать — я пойму и выполню. Примеры:\n\n"
        "• «Русский на пятницу этой недели страница 45 упражнение 12»\n"
        "• «Напиши дз на сегодня по русскому стр 129, на завтра математика стр 120 и литература послезавтра стр 55» — сразу НЕСКОЛЬКО заданий\n"
        "• «Замени учителя по биологии на Смирнову А.А.»\n"
        "• «Учителя: биология — Орлова, география — Николаев, обществознание — Петров»\n"
        "• «Урок 5 начинается в 11:40 и заканчивается в 12:25»\n"
        "• «Напомни завтра в 18:00 про секцию» / «Напомни через 20 минут»\n"
        "• «Пожелай мне спокойной ночи в 23:00» — пришлю живое пожелание по расписанию\n"
        "• «Желай мне доброе утро в 7:00 каждый день» — будет повторяться КАЖДЫЙ день\n"
        "• «Поменяй мой день рождения на 30 мая 2008» / «Скажи код класса»\n"
        "• «Какое ДЗ на понедельник?» / «Покажи учителей»\n"
        "• «Погода на завтра» / «Погода сейчас» / «Прогноз на 3 дня»\n"
        "• «Напиши анонимно Пете: привет, это я» — анонимное сообщение классу\n"
        "• «Создай кнопку Расписание званий с текстом …» / «Удали кнопку …»\n"
        "• «Скрой кнопку Погода» / «Верни кнопку Погода»\n"
        "• «Переименуй кнопку ДЗ в Домашка» / «Поставь Погоду на 1 место»\n"
        "• «Включи режим хамло» / «Режим тепло» / «Какой у меня режим ИИ?»\n"
        "• «Открой дз» / «Зайди в звезды» / «Покажи меню» — открою любой раздел\n"
        "• «Мои VIP-подписки» — что активно и до когда\n"
        "• Общий вопрос («какая сейчас погода в мире?») — просто отвечу\n\n"
        "🎙 Голос работает ВЕЗДЕ: голосовое сообщение в любом разделе бот\n"
        "расшифровывает и выполняет как обычный текст.\n"
        "Я мгновенно переключаюсь на новую тему — можно менять тему сообщения в любом порядке.\n"
        "Если данных не хватает — задам уточняющий вопрос. Изменять класс могут только админы.\n\n"
        "Выйти: «отмена»."
    )


@timeout(CONVERSATION_TIMEOUT)
async def automation_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Хендлер режима «🪄 Автоматизация»: текст → DeepSeek → JSON → действие."""
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    raw = (update.message.text or "").strip()

    # Выход из режима.
    if raw.lower() in AUTOMATION_EXIT_TRIGGERS:
        context.user_data.pop('automation_history', None)
        await update.message.reply_text(
            "👋 Вышел из режима автоматизации.",
            reply_markup=get_main_menu_keyboard(user),
        )
        return MAIN_MENU

    if not DEEPSEEK_API_KEY:
        context.user_data.pop('automation_history', None)
        await update.message.reply_text(
            "⚠️ Автоматизация недоступна: не задан ключ DEEPSEEK_API_KEY на сервере.\n"
            "Добавьте его в переменные окружения — и режим заработает.\n\n"
            "А пока работают обычные кнопки меню.",
            reply_markup=get_main_menu_keyboard(user),
        )
        return MAIN_MENU

    class_obj = get_class_by_user(user_id)
    is_admin = bool(class_obj and user_id in (class_obj.admins or []))

    # Многоходовая уточняющая переписка: копим историю запросов пользователя,
    # чтобы DeepSeek видел предыдущие уточнения.
    history = context.user_data.setdefault('automation_history', [])
    history.append(raw)
    if len(history) > 6:
        del history[:-6]

    # Thinking-сообщение с анимацией (переиспользуем существующую).
    thinking_msg = None
    anim_task = None
    try:
        thinking_msg = await update.message.reply_text("🪄 Автоматизация думает   ⏳")
        anim_task = asyncio.create_task(
            _ai_thinking_animation(context, update.effective_chat.id, thinking_msg.message_id)
        )
    except Exception as e:
        logger.warning(f"automation thinking msg: {e}")

    async def _stop_anim():
        if anim_task and not anim_task.done():
            anim_task.cancel()
            try:
                await anim_task
            except (asyncio.CancelledError, Exception):
                pass

    context_text = _automation_class_context_text(user, class_obj)
    system_prompt = _automation_system_prompt(context_text, is_admin)
    user_prompt = "\n".join(
        f"[Сообщение {i}] {t}" for i, t in enumerate(history, 1)
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    raw_answer = await _deepseek_chat(messages, timeout=60, temperature=0.1, force_json=True)
    await _stop_anim()

    action = _extract_json_dict(raw_answer)
    if not action:
        # Честный фолбэк: модель вернула не JSON — не бросаем пользователя.
        context.user_data.pop('automation_history', None)
        err = (
            "🤖 Не смог разобрать запрос в действие. Попробуйте сформулировать "
            "конкретнее, например: «Русский язык на пятницу страница 45»."
        )
        if thinking_msg:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=thinking_msg.message_id,
                    text=err,
                )
            except Exception:
                await update.message.reply_text(err)
        else:
            await update.message.reply_text(err)
        return AI_AUTOMATION

    # === open_section: автоматизация открывает ЛЮБОЙ раздел интерфейса ===
    # Режим автоматизации при этом завершается — пользователь оказывается
    # внутри выбранного раздела (корректный переход ConversationHandler).
    if (action.get("action") or "").strip() == "open_section":
        try:
            if thinking_msg:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id, message_id=thinking_msg.message_id
                )
        except Exception:
            pass
        section_name = (action.get("section") or "").strip()
        report, new_state = await _automation_open_section(update, context, user, action)
        if report:
            await update.message.reply_text(report)
        elif new_state != AI_AUTOMATION:
            await update.message.reply_text(
                f"🪄 Открыл раздел «{section_name}». Режим автоматизации завершён — "
                "продолжайте в обычном меню."
            )
        return new_state if new_state is not None else AI_AUTOMATION

    # Исполняем действие.
    report, finished = await _automation_execute_action(update, context, user, class_obj, action)

    if report is None:
        # Сработал reject_if_forbidden_chars — сообщение уже отправлено.
        return AI_AUTOMATION

    # Убираем thinking-сообщение и показываем отчёт.
    try:
        if thinking_msg:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id, message_id=thinking_msg.message_id
            )
    except Exception:
        pass

    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)

    if finished:
        context.user_data.pop('automation_history', None)
        await update.message.reply_text(
            "🪄 Готово. Напишите следующую задачу или «отмена» для выхода."
        )
    else:
        await update.message.reply_text(
            "🪄 Жду уточнения (или «отмена» для выхода)."
        )
    return AI_AUTOMATION


async def automation_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вход в режим автоматизации (из главного меню)."""
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    context.user_data.pop('automation_history', None)
    if not DEEPSEEK_API_KEY:
        await update.message.reply_text(
            "⚠️ Автоматизация недоступна: на сервере не задан ключ DEEPSEEK_API_KEY.\n"
            "Попросите разработчика добавить переменную окружения DEEPSEEK_API_KEY.",
            reply_markup=get_main_menu_keyboard(user),
        )
        return MAIN_MENU
    await update.message.reply_text(
        _automation_help_text(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardMarkup(
            [["❌ Отмена автоматизации"]], resize_keyboard=True
        ),
    )
    return AI_AUTOMATION


# Глобальные алиасы кнопок меню (без эмодзи, в нижнем регистре): работают для
# обычного текста и для ГОЛОСОВЫХ команд («домашка», «покажи учителей» и т. п.
# не совпадают с точным названием кнопки, но пользователь ждёт реакцию).
_MENU_TEXT_ALIASES = {
    "автоматизация": "🪄 Автоматизация",
    "открой автоматизацию": "🪄 Автоматизация",
    "включи автоматизацию": "🪄 Автоматизация",
    "домашнее задание": "📝 Домашнее задание",
    "домашка": "📝 Домашнее задание",
    "дз": "📝 Домашнее задание",
    "моё дз": "📝 Домашнее задание",
    "расписание": "📅 Расписание",
    "сегодня": "📅 Сегодня",
    "завтра": "📅 Завтра",
    "звонки": "🔔 Звонки",
    "учителя": "👨‍🏫 Учителя",
    "училки": "👨‍🏫 Учителя",
    "каникулы": "🎉 Каникулы",
    "таймер": "⏰ Таймер",
    "напоминание": "⏰ Таймер",
    "напоминания": "⏰ Таймер",
    "анонимное сообщение": "🕵️ Анонимное сообщение",
    "анонимка": "🕵️ Анонимное сообщение",
    "мои анонимные сообщения": "📨 Мои анонимные сообщения",
    "управление классами": "🎓 Управление классами",
    "классы": "🎓 Управление классами",
    "мой класс": "🎓 Управление классами",
    "мои кнопки": "🌟 Мои кнопки",
    "кнопки": "🌟 Мои кнопки",
    "звезды": "⭐ Звезды",
    "звёзды": "⭐ Звезды",
    "баланс": "⭐ Звезды",
    "погода": "🌦 Погода",
    "инструкция": "📚 Инструкция",
    "помощь": "📚 Инструкция",
    "код класса": "🔑 Код класса",
    "настройки": "⚙️ Настройки",
    "назад в меню": "⬅️ Назад в меню",
    "меню": "⬅️ Назад в меню",
    "ai": "🤖 DEVORKS+ai",
    "ии": "🤖 DEVORKS+ai",
    "чат": "🤖 DEVORKS+ai",
    "чат поддержки": "💬 Чат поддержки",
    "написать админу": "💬 Написать админу",
    "написать классу": "📢 Написать классу",
    "админская панель": "👨‍💼 Админская панель",
    "выйти из класса": "🚪 Выйти из класса",
    "выйти из аккаунта": "🔓 Выйти из аккаунта",
    "добавить дз": "➕ Добавить ДЗ",
    "удалить дз": "🗑️ Удалить ДЗ",
}


async def _run_automation_oneshot(update, context, user, class_obj, raw):
    """Одноразовое выполнение автоматизации ИЗ ГЛАВНОГО МЕНЮ.

    Пользователь пишет свободный текст (или говорит ГОЛОСОМ —middleware
    расшифровки подставит текст) прямо в главном меню: DeepSeek превращает
    запрос в действие, бот его исполняет и отвечает — БЕЗ включения режима
    «Автоматизация» и без смены клавиатуры. Если действие требует уточнения —
    диалог продолжается прямо в меню (история хранится в user_data).

    Возвращает новое состояние для ConversationHandler (обычно MAIN_MENU),
    либо None, если автоматизация недоступна (нет ключа / флуд) — тогда
    вызывающий код ведёт себя по-старому.
    """
    if not DEEPSEEK_API_KEY:
        return None
    user_id = user.user_id
    if is_user_spamming(user_id, key="automation_menu", min_interval=3.0, burst=5, burst_window=30.0):
        # Тихо пропускаем: флуд свободным текстом не должен бить по API.
        return None

    is_admin = bool(class_obj and user_id in (class_obj.admins or []))

    # Многоходовые уточнения прямо в меню: если прошлый ответ ждал уточнения —
    # продолжаем ту же переписку, иначе начинаем новую.
    history = context.user_data.setdefault('menu_automation_history', [])
    history.append(raw)
    if len(history) > 6:
        del history[:-6]

    thinking_msg = None
    anim_task = None
    try:
        thinking_msg = await update.message.reply_text("🪄 Думаю…   ⏳")
        anim_task = asyncio.create_task(
            _ai_thinking_animation(context, update.effective_chat.id, thinking_msg.message_id)
        )
    except Exception as e:
        logger.warning(f"automation oneshot thinking msg: {e}")

    async def _stop_anim():
        if anim_task and not anim_task.done():
            anim_task.cancel()
            try:
                await anim_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _drop_thinking():
        try:
            if thinking_msg:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id, message_id=thinking_msg.message_id
                )
        except Exception:
            pass

    context_text = _automation_class_context_text(user, class_obj)
    system_prompt = _automation_system_prompt(context_text, is_admin)
    user_prompt = "\n".join(
        f"[Сообщение {i}] {t}" for i, t in enumerate(history, 1)
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    raw_answer = await _deepseek_chat(messages, timeout=60, temperature=0.1, force_json=True)
    await _stop_anim()

    action = _extract_json_dict(raw_answer)
    if not action:
        context.user_data.pop('menu_automation_history', None)
        await _drop_thinking()
        await update.message.reply_text(
            "🪄 Не смог понять запрос как задачу. Сформулируйте конкретнее "
            "(например: «Русский на пятницу страница 45») или откройте "
            "«🪄 Автоматизация» — там я ловлю больше формулировок."
        )
        return MAIN_MENU

    if (action.get("action") or "").strip() == "open_section":
        await _drop_thinking()
        section_name = (action.get("section") or "").strip()
        report, new_state = await _automation_open_section(update, context, user, action)
        if report:
            await update.message.reply_text(report)
        context.user_data.pop('menu_automation_history', None)
        return new_state if new_state not in (None, AI_AUTOMATION) else MAIN_MENU

    report, finished = await _automation_execute_action(update, context, user, class_obj, action)
    if report is None:
        # reject_if_forbidden_chars уже отправил сообщение.
        return MAIN_MENU

    await _drop_thinking()
    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)

    if finished:
        context.user_data.pop('menu_automation_history', None)
    else:
        await update.message.reply_text(
            "🪄 Жду уточнения — напишите продолжение прямо сюда (голосом тоже можно)."
        )
    return MAIN_MENU


# ==================================
# === 👑 VIP-ПОДПИСКИ: ЧТЕНИЕ АКТИВНЫХ ПЕРИОДОВ ===
# ==================================
# Магазин кастомных товаров удалён по решению разработчика. Остались только
# хелперы чтения подписок (user.subscriptions): режим «🪄 Автоматизация»
# использует их в действии show_vip, чтобы честно показывать, какие
# VIP-функции ещё активны и до какой даты (ранее купленные подписки
# продолжают действовать до истечения своего периода).


def _parse_sub_until(raw_value):
    """Парсит окончание подписки. Поддерживает два формата:
    - строка ISO "YYYY-MM-DDTHH:MM:SS" (старый/простой формат);
    - dict {"until": ISO, "name": <читаемое имя>} (новый формат).
    None/мусор → None."""
    if not raw_value:
        return None
    if isinstance(raw_value, dict):
        raw_value = raw_value.get("until")
    if not raw_value:
        return None
    try:
        dt = datetime.fromisoformat(str(raw_value))
    except (TypeError, ValueError):
        return None
    return dt


def _sub_display_name(key, raw_value):
    """Читаемое имя подписки для показа пользователю."""
    if isinstance(raw_value, dict) and raw_value.get("name"):
        return str(raw_value["name"])
    if key.startswith("vip:"):
        return key[4:]
    return key


def _fmt_sub_until(dt):
    """Форматирует дату окончания подписки для пользователя."""
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


# ==================================
# === ОСНОВНЫЕ ОБРАБОТЧИКИ ===
# ==================================

# ==================================
# === ОБЯЗАТЕЛЬНАЯ ПОДПИСКА НА КАНАЛ ===
# ==================================

# Канал, на который пользователь должен быть подписан, чтобы пользоваться ботом.
# При начале создания аккаунта и при любом нажатии кнопок — если пользователь
# отписался, бот попросит подписаться обратно. Как только подпишется — снова
# сможет пользоваться ботом.
REQUIRED_CHANNEL_USERNAME = "devo_kanal"
REQUIRED_CHANNEL_URL = "https://t.me/devo_kanal"

SUBSCRIPTION_REQUIRED_TEXT = (
    "📢 Чтобы пользоваться ботом, подпишитесь на наш Telegram-канал:\n"
    f"{REQUIRED_CHANNEL_URL}\n\n"
    "После подписки нажмите «✅ Я подписался(ась)»."
)


def _subscription_required_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url=REQUIRED_CHANNEL_URL)],
        [InlineKeyboardButton("✅ Я подписался(ась)", callback_data="check_subscription")],
    ])


# Сколько часов мы доверяем «локальной» отметке `subscription_confirmed_at`,
# проставленной пользователем при нажатии «✅ Я подписался(ась)», когда API
# по техническим причинам не может подтвердить подписку (бот не админ канала
# и т. п.).
SUBSCRIPTION_TRUST_HOURS = 12


async def _check_subscription_via_api(user_id, context) -> str:
    """Спрашивает у Telegram API, состоит ли пользователь в @REQUIRED_CHANNEL.

    Возвращает одну из строк:
      * "subscribed"     — Telegram подтвердил, что пользователь в канале.
      * "not_subscribed" — Telegram явно сказал, что НЕ в канале (left/kicked,
                            user not found, participant_id_invalid и т. п.).
      * "cannot_verify"  — проверить не удалось (бот не админ канала, канал
                            приватный, сетевой сбой). Сюда же попадает
                            «chat not found», когда у бота нет доступа к каналу.
    """
    try:
        member = await context.bot.get_chat_member(
            chat_id=f"@{REQUIRED_CHANNEL_USERNAME}",
            user_id=int(user_id),
        )
        status = getattr(member, "status", None)
        if status in ("creator", "administrator", "member", "owner", "restricted"):
            return "subscribed"
        # явный left/kicked/banned/etc.
        return "not_subscribed"
    except TGBadRequest as e:
        msg = str(e).lower()
        # Telegram явно подтвердил, что пользователя НЕТ в канале.
        if (
            "user not found" in msg
            or "member not found" in msg
            or "participant_id_invalid" in msg
            or "user_not_participant" in msg
        ):
            return "not_subscribed"
        # «chat not found» / «peer id invalid» / «bot is not a member» —
        # это значит, что у бота нет доступа к каналу. В этом случае мы
        # НЕ можем проверить подписку через API.
        logger.warning(
            f"Не удалось проверить подписку user_id={user_id} через API (BadRequest): {e}. "
            "Скорее всего, бот не добавлен в @{REQUIRED_CHANNEL_USERNAME} как админ."
        )
        return "cannot_verify"
    except Exception as e:
        logger.warning(f"Сетевая ошибка при проверке подписки user_id={user_id}: {e}")
        return "cannot_verify"


def _read_subscription_confirmation(user_id) -> str:
    """Возвращает строку-таймштамп последнего нажатия «Я подписался» из
    локального файла, либо None. Этот файл — основной источник доверия,
    т. к. он работает даже для пользователей, у которых ещё нет User-записи."""
    try:
        data = load_data(SUBSCRIPTION_CONFIRMATIONS_FILE, {})
    except Exception:
        data = {}
    return data.get(str(user_id))


def _write_subscription_confirmation(user_id):
    """Записывает «сейчас» как таймштамп последнего подтверждения подписки
    для user_id. Используется при нажатии «Я подписался»."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        data = load_data(SUBSCRIPTION_CONFIRMATIONS_FILE, {})
    except Exception:
        data = {}
    data[str(user_id)] = now_str
    try:
        save_data(SUBSCRIPTION_CONFIRMATIONS_FILE, data)
    except Exception as e:
        logger.error(f"_write_subscription_confirmation save error: {e}")


def _is_recent_timestamp(ts_str, hours=SUBSCRIPTION_TRUST_HOURS) -> bool:
    if not ts_str:
        return False
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    return (datetime.now() - ts) < timedelta(hours=hours)


def _has_recent_local_subscription_confirmation(user_id) -> bool:
    """Возвращает True, если пользователь недавно (в пределах
    SUBSCRIPTION_TRUST_HOURS часов) нажимал «✅ Я подписался(ась)» —
    значит, мы ему доверяем, даже если API проверки не работает.
    Проверяет ОБА источника: локальный файл (для не-залогиненных) И поле
    в User (для зарегистрированных)."""
    # 1) локальный файл (работает для всех, в т. ч. до регистрации)
    if _is_recent_timestamp(_read_subscription_confirmation(user_id)):
        return True
    # 2) поле в User-объекте (на случай, если файл потерян)
    try:
        user = get_user(user_id)
    except Exception:
        user = None
    if user and _is_recent_timestamp(getattr(user, 'subscription_confirmed_at', None)):
        return True
    return False


async def is_user_subscribed(user_id, context) -> bool:
    """СТРОГАЯ проверка подписки на канал. Возвращает True ТОЛЬКО если:

    1. Telegram API подтвердил, что пользователь состоит в канале, ИЛИ
    2. API проверить нельзя (бот не админ канала / сеть лежит) И
       пользователь недавно нажимал «✅ Я подписался(ась)» — мы тогда
       доверяем кнопке на SUBSCRIPTION_TRUST_HOURS часов.

    Во ВСЕХ остальных случаях возвращает False — бот покажет требование
    подписаться на канал. Это и есть «fail-closed» поведение: лучше
    лишний раз попросить подписаться, чем по ошибке пропустить отписавшегося.
    """
    api_result = await _check_subscription_via_api(user_id, context)
    if api_result == "subscribed":
        return True
    if api_result == "not_subscribed":
        logger.info(f"Пользователь {user_id} НЕ подписан (API).")
        return False
    # api_result == "cannot_verify" — API не может ответить. Доверяем
    # пользователю, если он недавно нажимал «Я подписался».
    if _has_recent_local_subscription_confirmation(user_id):
        return True
    logger.info(
        f"Пользователь {user_id} НЕ подтверждён ни API, ни локально — "
        "просим подписаться."
    )
    return False


async def send_subscription_required(update, context):
    """Показывает сообщение с требованием подписаться на канал."""
    kb = _subscription_required_keyboard()
    # callback_query — пришёл от inline-кнопки
    if getattr(update, "callback_query", None):
        try:
            await update.callback_query.answer()
        except Exception:
            pass
        try:
            await update.callback_query.edit_message_text(
                SUBSCRIPTION_REQUIRED_TEXT, reply_markup=kb
            )
            return
        except Exception:
            pass
        try:
            chat_id = update.callback_query.from_user.id
            await context.bot.send_message(
                chat_id=chat_id, text=SUBSCRIPTION_REQUIRED_TEXT, reply_markup=kb
            )
            return
        except Exception:
            pass
    # обычное сообщение
    if getattr(update, "message", None):
        try:
            await update.message.reply_text(SUBSCRIPTION_REQUIRED_TEXT, reply_markup=kb)
            return
        except Exception:
            pass
    if getattr(update, "effective_user", None):
        try:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=SUBSCRIPTION_REQUIRED_TEXT,
                reply_markup=kb,
            )
        except Exception:
            pass


async def ensure_subscribed(update, context) -> bool:
    """Проверяет подписку. Если подписан — возвращает True. Иначе — отправляет
    сообщение с требованием подписки и возвращает False.

    Разработчик НЕ исключается автоматически из проверки: иначе при тестировании
    бота на собственном аккаунте владелец никогда не увидит prompt подписки и
    не сможет проверить, корректно ли работает блокировка отписавшихся
    пользователей. Если разработчик хочет свободно пользоваться ботом — пусть
    он подпишется на свой канал, как и все остальные. Это всего лишь один клик.
    """
    user = getattr(update, "effective_user", None)
    if not user:
        return True
    if await is_user_subscribed(user.id, context):
        return True
    await send_subscription_required(update, context)
    return False


async def check_subscription_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик кнопки «✅ Я подписался(ась)».

    Логика:
      1. Пытаемся проверить подписку через Telegram API.
      2. Если API явно говорит «не подписан» — отказываем и просим подписаться.
      3. Если API говорит «подписан» ИЛИ не может ответить (бот не админ канала),
         — доверяем нажатию кнопки и проставляем локальную метку доверия на
         SUBSCRIPTION_TRUST_HOURS часов. Так что если у владельца бота не
         настроен админ-доступ к каналу, бот всё равно работает.
    """
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass

    user_id = str(query.from_user.id)
    api_result = await _check_subscription_via_api(user_id, context)

    if api_result == "not_subscribed":
        # Telegram явно сказал, что пользователя в канале нет.
        try:
            await query.answer(
                "Подписка не найдена. Подпишитесь и попробуйте снова.",
                show_alert=True,
            )
        except Exception:
            pass
        try:
            await query.edit_message_text(
                SUBSCRIPTION_REQUIRED_TEXT,
                reply_markup=_subscription_required_keyboard(),
            )
        except Exception:
            pass
        return

    # api_result in ("subscribed", "cannot_verify") — пускаем дальше.
    # При "cannot_verify" доверяем кнопке (бот не админ канала и т. п.).
    # ВАЖНО: пишем подтверждение в локальный файл ДО get_user(), чтобы оно
    # работало даже для совсем новых пользователей, у которых ещё нет
    # User-записи. Иначе они застрянут в цикле «подписаться → /start →
    # подписаться → ...», т. к. без User-объекта мы не могли бы сохранить
    # доверительную метку.
    _write_subscription_confirmation(user_id)
    user = get_user(user_id)
    if user:
        user.subscription_confirmed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            save_user(user)
        except Exception as e:
            logger.error(f"check_subscription_handler: save_user error: {e}")

    try:
        await query.edit_message_text("✅ Подписка подтверждена! Спасибо 🙏")
    except Exception:
        pass

    if not user:
        # Аккаунт ещё не создан — просим запустить /start, чтобы пройти регистрацию.
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="Отправьте /start, чтобы продолжить создание аккаунта.",
            )
        except Exception:
            pass
        return

    if not user.setup_completed:
        # Регистрация не завершена — предлагаем продолжить /start.
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="Отправьте /start, чтобы продолжить регистрацию.",
            )
        except Exception:
            pass
        return

    # Обычный пользователь — сразу возвращаем клавиатуру главного меню,
    # чтобы не требовался повторный /start.
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"👋 С возвращением, {user.first_name}!",
            reply_markup=get_main_menu_keyboard(user),
        )
    except Exception as e:
        logger.error(f"check_subscription_handler: не удалось вернуть меню: {e}")


@timeout(CONVERSATION_TIMEOUT)
async def notify_developer_about_new_user(context, user):
    """Отправляет разработчику уведомление о новом пользователе.

    Срабатывает при первом /start пользователя — когда объект User
    создаётся в первый раз. Уведомление можно отключить в панели
    разработчика тогглом «Уведомления о новых пользователях».
    Чтобы не дублировать уведомление, ставим у user.dev_notified = True
    после успешной отправки.
    """
    if not DEVELOPER_ID:
        return
    if not is_dev_new_user_notification_enabled():
        return
    if getattr(user, 'dev_notified', False):
        return

    username_str = f"@{user.username}" if getattr(user, 'username', '') else "—"
    first_name = getattr(user, 'first_name', '') or "—"
    user_code = getattr(user, 'user_code', '') or "—"
    joined_date = getattr(user, 'joined_date', '') or "—"

    text = (
        "🆕 *Новый пользователь зарегистрировался в боте!*\n\n"
        f"👤 Имя: {first_name}\n"
        f"📛 Username: {username_str}\n"
        f"🆔 User ID: `{user.user_id}`\n"
        f"🎫 Код пользователя: `{user_code}`\n"
        f"🗓 Зарегистрирован: {joined_date}"
    )

    try:
        await context.bot.send_message(
            chat_id=DEVELOPER_ID,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
        user.dev_notified = True
        save_user(user)
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления разработчику о новом пользователе: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # === Реферальный deep-link ===
    # Payload передаётся в /start как один аргумент: /start ref_<id>.
    # Сохраняем "кандидата в пригласители" в context.user_data до тех пор,
    # пока новый пользователь не завершит первичную регистрацию (тогда
    # выдадим приглашающему ровно REFERRAL_REWARD_STARS виртуальных
    # звёзд). Защита: пользователь не может быть собственным рефералом,
    # и DEVELOPER_ID не может быть рефералом.
    referrer_candidate = None
    try:
        args = getattr(context, 'args', None) or []
        if args and isinstance(args, list) and args[0] and isinstance(args[0], str):
            payload = args[0].strip()
            if payload.startswith("ref_"):
                cand = payload[4:].strip()
                if cand and cand.isdigit() and cand != user_id:
                    referrer_candidate = cand
    except Exception as e:
        logger.error(f"start: ошибка разбора реферального payload: {e}")
    if referrer_candidate:
        context.user_data['pending_referrer'] = referrer_candidate

    # Обязательная подписка на канал — требуется уже на старте создания аккаунта.
    # Если пользователь не подписан, просим подписаться и НЕ создаём аккаунт.
    if not await ensure_subscribed(update, context):
        return ConversationHandler.END

    # Если пользователь заблокирован — показываем кнопку разблокировки со звёздами
    if is_user_blocked(user_id):
        await send_blocked_message(update, context, user_id)
        return ConversationHandler.END

    user = get_user(user_id)

    # ПУНКТ 1: Инструкция показывается СРАЗУ при /start (для всех — новых и старых).
    # После нажатия "Я прочитал(а) инструкцию" — продолжается регистрация как раньше.
    is_new_user = user is None
    if is_new_user:
        # Создаём заготовку, чтобы сохранить user_code и first_name
        user = User(user_id, update.effective_user.username, update.effective_user.first_name)
        user.user_code = generate_user_code()
        # Если пришёл по реферальной ссылке — фиксируем referrer_id у нового
        # пользователя. Звёзды приглашающему начислятся ТОЛЬКО после того,
        # как новый пользователь полностью завершит регистрацию (см. show_main_menu).
        if referrer_candidate:
            user.referrer_id = referrer_candidate
        save_user_code(user.user_code, user_id)
        save_user(user)
        # Уведомление разработчику о новом пользователе (если тоггл включён).
        await notify_developer_about_new_user(context, user)

    if not user.instructions_read:
        await show_instructions(update, context)
        return SHOW_INSTRUCTIONS

    # Если инструкция уже прочитана — продолжаем как раньше
    if not user.birthday:
        await update.message.reply_text(
            "🎂 Пожалуйста, введите вашу реальную дату рождения в формате ГГГГ-ММ-ДД (например, 2005-04-15):",
            reply_markup=get_cancel_keyboard()
        )
        return ENTER_BIRTHDAY
    elif not user.disclaimer_accepted:
        await show_disclaimer(update, context)
        return SHOW_INSTRUCTIONS
    elif not user.setup_completed:
        # ПУНКТ 10: ручной ввод времени убран — спрашиваем сразу город,
        # часовой пояс определяется автоматически.
        await update.message.reply_text(
            "🏙 Введите ваш город (например: Москва, Санкт-Петербург, Казань).\n"
            "По нему я сам определю часовой пояс и настрою уведомления.",
            reply_markup=get_cancel_keyboard()
        )
        return ENTER_CITY
    else:
        await show_main_menu(update, context, user)
        return MAIN_MENU


async def send_blocked_message(update, context, user_id):
    """ПУНКТ 8: Показывает заблокированному пользователю кнопку разблокировки
    со стоимостью в звёздах, заданной разработчиком/админом."""
    blocked_users = load_blocked_users()
    blocked_data = blocked_users.get(str(user_id), {})
    blocked_by = blocked_data.get('blocked_by')

    prices = load_prices()
    if blocked_by == DEVELOPER_ID:
        price = blocked_data.get('unblock_price', prices.get('unblock_dev', 100))
        block_type = "разработчиком"
    else:
        price = blocked_data.get('unblock_price', prices.get('unblock', 40))
        block_type = "администратором"

    text = (
        f"🚫 Вы заблокированы {block_type}.\n\n"
        f"Чтобы разблокировать аккаунт, нажмите кнопку ниже.\n"
        f"С вашего баланса (или через Telegram Stars) будет списано {price} ⭐."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔓 Разблокироваться за {price} ⭐", callback_data="unblock_self")]
    ])
    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, reply_markup=keyboard)
    elif hasattr(update, 'callback_query') and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard)
        except Exception:
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)

async def show_disclaimer(update, context):
    disclaimer_text = (
        "⚠️ **ВАЖНОЕ УВЕДОМЛЕНИЕ**\n\n"
        "Перед использованием бота DEVORKS+, пожалуйста, подтвердите:\n\n"
        "1. ✅ Я прочитал(а) инструкцию по использованию бота\n"
        "2. ✅ Я понимаю и принимаю, что разработчик не несет ответственности за:\n"
        "   • Содержание сообщений, создаваемых пользователями\n"
        "   • Действия других пользователей в классах\n"
        "   • Возможные сбои в работе бота\n"
        "   • Потерю данных в результате технических неполадок\n\n"
        "Продолжая использование бота, вы соглашаетесь с этими условиями."
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я согласен(на) с условиями", callback_data="accept_disclaimer")],
        [InlineKeyboardButton("❌ Отказаться", callback_data="decline_disclaimer")]
    ])

    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(disclaimer_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.callback_query.edit_message_text(disclaimer_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def show_instructions(update, context):
    instructions = get_instructions()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я прочитал(а) инструкцию", callback_data="instructions_read")]
    ])

    if hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.edit_message_text(instructions, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    elif hasattr(update, 'message') and update.message:
        await update.message.reply_text(instructions, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

@timeout(CONVERSATION_TIMEOUT)
async def enter_birthday_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    birthday_str = update.message.text.strip()

    try:
        birthday = datetime.strptime(birthday_str, "%Y-%m-%d")
        if birthday > datetime.now():
            await update.message.reply_text("Дата рождения не может быть в будущем. Введите ещё раз в формате ГГГГ-ММ-ДД:")
            return ENTER_BIRTHDAY

        user.birthday = birthday_str
        save_user(user)

        await show_disclaimer(update, context)
        return SHOW_INSTRUCTIONS

    except ValueError:
        await update.message.reply_text("Введите дату в формате ГГГГ-ММ-ДД (например, 2005-04-15):")
        return ENTER_BIRTHDAY

async def disclaimer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)

    if query.data == "accept_disclaimer":
        user.disclaimer_accepted = True
        save_user(user)

        if not user.setup_completed:
            # ПУНКТ 10: ручной ввод времени убран — часовой пояс определяется
            # автоматически по введённому городу.
            await query.edit_message_text(
                "✅ Отлично!\n\n"
                "🏙 Введите ваш город (например: Москва, Санкт-Петербург, Казань).\n"
                "По нему я сам определю часовой пояс и настрою уведомления."
            )
            return ENTER_CITY
        else:
            await show_main_menu(update, context, user)
            return MAIN_MENU
    else:
        await query.edit_message_text("Вы отказались от использования бота. Если передумаете — /start")
        return ConversationHandler.END

async def instructions_read_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ПУНКТ 1: После прочтения инструкции пользователь идёт по обычному пути регистрации."""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)

    if not user:
        user = User(user_id, query.from_user.username, query.from_user.first_name)
        user.user_code = generate_user_code()
        save_user_code(user.user_code, user_id)

    user.instructions_read = True
    save_user(user)

    # Дальше — регистрация как раньше
    if not user.birthday:
        await query.edit_message_text(
            "👋 Добро пожаловать в DEVORKS+! Давайте настроим ваш профиль.\n\n"
            "🎂 Введите вашу реальную дату рождения в формате ГГГГ-ММ-ДД (например, 2005-04-15):\n\n"
            "Бот будет напоминать вам о дне рождения и может поздравить вас в классе!"
        )
        return ENTER_BIRTHDAY
    elif not user.disclaimer_accepted:
        await show_disclaimer(update, context)
        return SHOW_INSTRUCTIONS
    elif not user.setup_completed:
        # ПУНКТ 10: ручной ввод времени убран — спрашиваем только город,
        # часовой пояс определяется автоматически.
        await query.edit_message_text(
            "🏙 Введите ваш город (например: Москва, Санкт-Петербург, Казань).\n"
            "По нему я сам определю часовой пояс и настрою уведомления."
        )
        return ENTER_CITY
    else:
        await show_main_menu(update, context, user)
        return MAIN_MENU

@timeout(CONVERSATION_TIMEOUT)
async def set_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    time_str = update.message.text.strip()

    try:
        user_time_dt = datetime.strptime(time_str, "%H:%M")
        timezone = calculate_timezone(time_str)
        user.timezone = timezone
        user.local_time_set = True
        user.setup_completed = True
        save_user(user)

        # ПУНКТ 6: запланировать утренние/вечерние уведомления для нового пользователя
        try:
            schedule_user_daily_jobs(context.application, user)
        except Exception as e:
            logger.error(f"Не удалось запланировать daily jobs: {e}")
        # ПУНКТ (ДР): сразу ставим ежедневную проверку дня рождения — чтобы бот
        # поздравил пользователя, даже если он зарегистрировался незадолго до ДР.
        try:
            schedule_user_birthday_job(context.application, user)
        except Exception as e:
            logger.error(f"Не удалось запланировать birthday job: {e}")

        success_text = (
            f"✅ **Настройка завершена!**\n\n"
            f"⏰ Ваше время: {user_time_dt.strftime('%H:%M')}\n"
            f"🌍 Часовой пояс: UTC{timezone:+d}\n"
            f"🎂 День рождения: {user.birthday}\n\n"

            f"Теперь я буду присылать вам утренние и вечерние уведомления!\n"
        )
        await update.message.reply_text(success_text, parse_mode="Markdown")
        # === НОВОЕ: после общей настройки спрашиваем город (для погоды) ===
        if not getattr(user, 'city', None):
            await update.message.reply_text(
                "🏙 Укажите ваш город (для прогноза погоды и утренних уведомлений).\n\n"
                "Введите название города (например: Москва, Санкт-Петербург, Казань):",
                reply_markup=get_cancel_keyboard()
            )
            return ENTER_CITY
        await class_management(update, context)
        return CLASS_MANAGEMENT

    except ValueError:
        await update.message.reply_text("Введите время в формате ЧЧ:ММ (например, 08:30):")
        return SET_TIME

async def restore_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Обязательная подписка на канал — до восстановления сессии тоже нужно
    # проверить, чтобы отписавшийся пользователь не получил клавиатуру меню.
    if not await ensure_subscribed(update, context):
        return ConversationHandler.END
    if is_user_blocked(user_id):
        await send_blocked_message(update, context, user_id)
        return ConversationHandler.END
    user = get_user(user_id)
    if user and user.setup_completed:
        await update.message.reply_text("🔄 Сессия восстановлена!", reply_markup=get_main_menu_keyboard(user))
        return MAIN_MENU
    else:
        await update.message.reply_text("Нажмите /start для начала работы.")
        return ConversationHandler.END

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user=None):
    if not user:
        user_id = str(update.effective_user.id)
        user = get_user(user_id)

    # === Реферальный бонус (отложенное начисление) ===
    # Если пользователь пришёл по ссылке /start ref_<id> и завершил регистрацию,
    # начисляем приглашающему ровно REFERRAL_REWARD_STARS звёзд один раз.
    try:
        ref_id = getattr(user, 'referrer_id', None)
        already_paid = getattr(user, 'referral_bonus_paid', False)
        setup_done = bool(getattr(user, 'setup_completed', False))
        if ref_id and (not already_paid) and setup_done:
            granted = credit_referrer_for(user.user_id, ref_id)
            if granted:
                # Пытаемся уведомить приглашающего о бонусе.
                try:
                    await context.bot.send_message(
                        chat_id=int(ref_id),
                        text=(
                            f"🎉 Ваш приглашённый пользователь зарегистрировался!\n"
                            f"+{REFERRAL_REWARD_STARS} ⭐ зачислены на ваш виртуальный баланс.\n"
                            f"Откройте «⭐ Звёзды» в главном меню, чтобы потратить их на функции."
                        )
                    )
                except Exception as e:
                    logger.warning(f"Не удалось уведомить приглашающего {ref_id}: {e}")
    except Exception as e:
        logger.error(f"show_main_menu: ошибка обработки реферального бонуса: {e}")

    greeting_text = f"👋 Добро пожаловать в DEVORKS+, {user.first_name}!"
    keyboard = get_main_menu_keyboard(user)

    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(greeting_text, reply_markup=keyboard)
    elif hasattr(update, 'callback_query') and update.callback_query:
        try:
            await update.callback_query.edit_message_text("✅ Возвращаюсь в главное меню...")
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=update.callback_query.from_user.id,
            text=greeting_text,
            reply_markup=keyboard
        )
    else:
        await context.bot.send_message(
            chat_id=user.user_id,
            text=greeting_text,
            reply_markup=keyboard
        )
    return MAIN_MENU

# ==================================
# === УПРАВЛЕНИЕ ВИДИМОСТЬЮ КНОПОК ===
# ==================================

async def manage_button_visibility_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    hidden_count = len(getattr(user, 'hidden_buttons', []))

    text = (
        f"👁 **Управление видимостью кнопок**\n\n"
        f"📊 Скрытых кнопок: {hidden_count}\n\n"
        f"Нажмите на кнопку, чтобы скрыть или показать её.\n"
        f"⚙️ Кнопка настроек всегда остаётся видимой."
    )

    try:
        await query.edit_message_text(text, reply_markup=get_button_visibility_keyboard(user), parse_mode="Markdown")
    except Exception:
        await query.edit_message_text(text, reply_markup=get_button_visibility_keyboard(user))
    return MANAGE_BUTTON_VISIBILITY


def _resolve_button_name(cb_fragment, user):
    """Сопоставляет обрезанный callback_data фрагмент с полным именем кнопки."""
    all_names = get_all_user_button_names(user)
    for name in all_names:
        if _safe_cb("toggle_visibility_", name).replace("toggle_visibility_", "") == cb_fragment:
            return name
    for name in all_names:
        if name.startswith(cb_fragment):
            return name
    return cb_fragment


async def toggle_button_visibility_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    cb_fragment = query.data.replace("toggle_visibility_", "")
    button_name = _resolve_button_name(cb_fragment, user)

    if not hasattr(user, 'hidden_buttons'):
        user.hidden_buttons = []

    if button_name in user.hidden_buttons:
        user.hidden_buttons.remove(button_name)
        action = "показана"
    else:
        user.hidden_buttons.append(button_name)
        action = "скрыта"

    save_user(user)

    hidden_count = len(user.hidden_buttons)
    text = (
        f"👁 Управление видимостью кнопок\n\n"
        f"📊 Скрытых кнопок: {hidden_count}\n\n"
        f"✅ Кнопка '{button_name}' {action}!\n\n"
        f"Нажмите на кнопку, чтобы скрыть или показать её."
    )

    try:
        await query.edit_message_text(text, reply_markup=get_button_visibility_keyboard(user))
    except Exception as e:
        logger.error(f"Ошибка toggle_visibility: {e}")
    return MANAGE_BUTTON_VISIBILITY

async def finish_button_visibility(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)

    hidden_count = len(getattr(user, 'hidden_buttons', []))

    text = f"⚙️ Настройки\n\n👤 Имя: {user.first_name}\n🌍 Часовой пояс: UTC{_fmt_tz_offset(user.timezone)}\n🔔 Уведомления: {'Включены' if user.notifications else 'Выключены'}\n🎂 День рождения: {user.birthday or 'Не установлен'}\n\n✅ Видимость сохранена! Скрыто: {hidden_count}\n\nВыберите действие:"
    try:
        await query.edit_message_text(text, reply_markup=get_settings_keyboard(user))
    except Exception:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=get_settings_keyboard(user))

    await context.bot.send_message(
        chat_id=user_id,
        text="⚙️ Меню обновлено!",
        reply_markup=get_main_menu_keyboard(user)
    )
    return USER_SETTINGS

async def back_to_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await user_settings(update, context)

# ==================================
# === ОСНОВНОЕ МЕНЮ ===
# ==================================

async def auto_reenter_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Автоматический повторный вход в главное меню, если ConversationHandler
    завершился (по таймауту или ошибке). Не требует /start."""
    # Не пытаемся обрабатывать callback-и здесь — они идут через handle_callback
    if not getattr(update, "message", None):
        return MAIN_MENU
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user or not user.setup_completed:
        return await start(update, context)
    return await handle_main_menu(update, context)


# ==================================
# === БЫСТРЫЕ КОМАНДЫ ИЗ ЛЮБОГО СОСТОЯНИЯ ===
# ==================================
# Пользователь должен иметь возможность нажать «👨‍🏫 Учителя», «⏰ Таймер»,
# «📝 Домашнее задание» и другие кнопки главного меню из ЛЮБОГО режима —
# даже когда бот ждёт от него ввода (текст ДЗ, имя учителя, время и т. п.).
# Раньше такие нажатия «съедались» текущим состоянием и трактовались как
# вводимые данные. Теперь первым хендлером в каждом текстовом состоянии
# стоит перехватчик точных названий команд (QUICK_COMMANDS), который
# аккуратно перенаправляет их в штатный handle_main_menu.

async def handle_quick_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перехватчик быстрых команд из любого состояния.

    Текст гарантированно совпадает с одной из QUICK_COMMANDS (фильтр),
    поэтому просто делегируем в handle_main_menu — он знает, что делать,
    и вернёт корректное новое состояние разговора.
    """
    return await handle_main_menu(update, context)


def build_quick_commands_pattern():
    """Строит анкеренный regex из QUICK_COMMANDS (эмодзи + точные названия)."""
    import re as _re
    parts = [_re.escape(cmd) for cmd in QUICK_COMMANDS]
    return "^(" + "|".join(parts) + ")$"


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /cancel: отвечает пользователю и возвращает в главное меню,
    не роняя ConversationHandler."""
    try:
        if getattr(update, "message", None):
            await update.message.reply_text("Отменено.")
    except Exception:
        pass
    user_id = str(update.effective_user.id) if update.effective_user else None
    user = get_user(user_id) if user_id else None
    if user and user.setup_completed:
        try:
            await show_main_menu(update, context, user)
        except Exception as e:
            logger.error(f"cancel_command show_main_menu error: {e}")
        return MAIN_MENU
    return ConversationHandler.END

@timeout(CONVERSATION_TIMEOUT)
async def handle_main_menu_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото в главном меню.

    В режиме AI (`ai_mode == True`) фото уходит в Groq Vision через
    `handle_ai_message`. В обычном режиме отвечаем подсказкой, чтобы
    пользователь сначала зашёл в AI-режим.
    """
    user_id = str(update.effective_user.id)
    if not await ensure_subscribed(update, context):
        return MAIN_MENU
    if is_user_blocked(user_id):
        await send_blocked_message(update, context, user_id)
        return ConversationHandler.END

    user = get_user(user_id) or User(user_id)

    if context.user_data.get('ai_mode'):
        await handle_ai_message(update, context)
        return MAIN_MENU

    await update.message.reply_text(
        "🖼 Чтобы я мог посмотреть фото — сначала зайдите в режим AI "
        "(кнопка «🤖 AI» в меню), а потом отправьте фото.",
        reply_markup=get_main_menu_keyboard(user),
    )
    return MAIN_MENU


@timeout(CONVERSATION_TIMEOUT)
async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Обязательная подписка на канал — если пользователь отписался, просим
    # подписаться обратно на любом нажатии кнопки.
    if not await ensure_subscribed(update, context):
        return MAIN_MENU

    if is_user_blocked(user_id):
        # ПУНКТ 8: показываем кнопку разблокировки со звёздами
        await send_blocked_message(update, context, user_id)
        return ConversationHandler.END

    user = get_user(user_id)
    if not user:
        user = User(user_id)
    message_text = update.message.text

    # ПУНКТ 4: если пользователь переименовал кнопку, отображаемое имя нужно
    # сопоставить с оригинальным, чтобы внутренняя логика осталась рабочей.
    reverse_map = get_user_button_reverse_map(user)
    if message_text in reverse_map:
        message_text = reverse_map[message_text]

    if context.user_data.get('replying_to_anon'):
        return await send_anonymous_reply(update, context)

    if context.user_data.get('ai_mode'):
        # Любой вариант «выход» — гарантированно выходим из режима AI
        lowered = (message_text or "").strip().lower()
        exit_triggers = {
            "❌ выход из ai", "выход из ai", "выйти из ai",
            "выйти", "выход", "exit", "/exit", "stop", "/stop",
        }
        if lowered in exit_triggers:
            context.user_data['ai_mode'] = False
            # Чистим историю диалога с AI, чтобы следующий вход начинался «с чистого листа»
            user_conversations.pop(user_id, None)
            await update.message.reply_text(
                "👋 Вы вышли из режима ai.",
                reply_markup=get_main_menu_keyboard(user),
            )
            return MAIN_MENU
        # Любой другой текст — передаём в AI. handle_ai_message сам ловит все ошибки.
        await handle_ai_message(update, context)
        return MAIN_MENU

    personal_buttons = get_personal_buttons(user_id)
    for button in personal_buttons:
        if button.name == message_text:
            if button.button_type == "url":
                await update.message.reply_text(
                    f"🔗 **{button.name}**",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔗 Перейти по ссылке", url=button.content)]
                    ]),
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.message.reply_text(f"📝 **{button.name}:**\n\n{button.content}", parse_mode=ParseMode.MARKDOWN)
            return MAIN_MENU

    global_buttons = get_global_buttons()
    for button in global_buttons:
        if button.name == message_text:
            if button.button_type == "url":
                await update.message.reply_text(
                    f"🔗 {button.name}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Перейти по ссылке", url=button.content)]])
                )
            else:
                await update.message.reply_text(f"📝 {button.content}")
            return MAIN_MENU

    class_obj = get_class_by_user(user_id)
    if class_obj and not is_user_class_blocked(user_id, class_obj.class_code):
        custom_buttons = get_class_custom_buttons(class_obj.class_code)
        for custom_button in custom_buttons:
            button_text = custom_button.name
            if custom_button.name.startswith("CLASS_"):
                button_text = custom_button.name.replace("CLASS_", "")

            if button_text == message_text:
                if custom_button.button_type == "url":
                    await update.message.reply_text(
                        f"🔗 {button_text}",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Перейти по ссылке", url=custom_button.content)]])
                    )
                else:
                    await update.message.reply_text(f"📝 {custom_button.content}")
                return MAIN_MENU

    if message_text == "➕ Добавить ДЗ":
        if class_obj and str(user_id) in class_obj.admins:
            context.user_data['current_admin_class'] = class_obj.class_code
            keyboard = []
            subjects = class_obj.subjects or []
            if subjects:
                for subject in subjects:
                    cb_subj = subject[:50]
                    keyboard.append([InlineKeyboardButton(f"📚 {subject}", callback_data=f"hw_subject_{cb_subj}")])
            keyboard.append([InlineKeyboardButton("➕ Свой предмет", callback_data="hw_new_subject")])
            if subjects:
                keyboard.append([InlineKeyboardButton("🗑️ Удалить предмет", callback_data="hw_delete_subject_list")])
            keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")])
            text = "📝 **Добавление ДЗ**\n\nВыберите предмет:" if subjects else "📝 **Добавление ДЗ**\n\nДобавьте предмет:"
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            return ADD_HOMEWORK
        else:
            await update.message.reply_text("У вас нет прав администратора.")
        return MAIN_MENU

    elif message_text == "🗑️ Удалить ДЗ":
        if class_obj and str(user_id) in class_obj.admins:
            context.user_data['current_admin_class'] = class_obj.class_code
            if not class_obj.homework:
                await update.message.reply_text("📝 Домашнее задание не задано.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]]))
                return MAIN_MENU
            await update.message.reply_text("🗑️ Выберите домашнее задание для удаления:", reply_markup=get_homework_delete_keyboard(class_obj))
            return DELETE_HOMEWORK_SELECT
        else:
            await update.message.reply_text("У вас нет прав администратора.")
        return MAIN_MENU

    elif message_text == "📢 Написать классу":
        if class_obj and str(user_id) in class_obj.admins:
            context.user_data['current_admin_class'] = class_obj.class_code
            await update.message.reply_text(
                "📢 Введите сообщение для отправки всему классу:\n\n"
                "Чтобы отменить — нажмите кнопку ниже.",
                reply_markup=get_cancel_keyboard(),
            )
            return SEND_CLASS_MESSAGE
        else:
            await update.message.reply_text("У вас нет прав администратора.")
        return MAIN_MENU

    elif message_text == "⬅️ Назад в меню":
        await show_main_menu(update, context, user)
        return MAIN_MENU

    elif message_text == "📨 Мои анонимные сообщения":
        keyboard = get_anonymous_messages_keyboard(user_id)
        if not keyboard:
            await update.message.reply_text("📭 У вас нет анонимных сообщений.")
        else:
            await update.message.reply_text(
                "🕵️ **Ваши анонимные сообщения:**\n\n"
                "👁 - вы уже смотрели отправителя\n"
                "🕵️ - отправитель скрыт\n\n"
                "Выберите сообщение для просмотра:",
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
        return MAIN_MENU

    elif message_text == "🔑 Код класса":
        if user.class_code:
            class_obj = get_class_by_code(user.class_code)
            if class_obj:
                text = f"🔑 **Код вашего класса:**\n\n`{user.class_code}`\n\n🏫 Название: {class_obj.class_name}\n\nПоделитесь этим кодом с одноклассниками!"
                await update.message.reply_text(text, parse_mode="Markdown")
            else:
                await update.message.reply_text("Класс не найден.")
        else:
            await update.message.reply_text("Вы не состоите в классе.")
        return MAIN_MENU

    if message_text == "🎓 Управление классами":
        return await class_management(update, context)

    elif message_text == "⚙️ Настройки":
        return await user_settings(update, context)

    elif message_text == "🌟 Мои кнопки":
        return await personal_buttons_menu(update, context)

    elif message_text == "⭐ Звезды":
        return await stars_menu(update, context)

    elif message_text == "📚 Инструкция":
        instructions = get_instructions()
        await update.message.reply_text(
            instructions,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]])
        )
        return MAIN_MENU

    elif message_text == "🌦 Погода":
        # Текущая погода + кнопка «На 3 дня» — данные через WeatherAPI.
        # ПУНКТ 7: погода теперь только по нажатию этой кнопки.
        # Защита от спама — нельзя дёргать чаще раза в 3 секунды.
        if is_user_spamming(user_id, key="weather_btn", min_interval=3.0, burst=2, burst_window=10.0):
            await update.message.reply_text(get_user_rate_limit_message())
            return MAIN_MENU
        await send_weather_to_user(update, context, user)
        return MAIN_MENU

    elif message_text == "💬 Чат поддержки":
        # ПУНКТ 3: пользователь открывает чат поддержки и пишет одно сообщение.
        if is_user_spamming(user_id, key="support_open"):
            await update.message.reply_text(get_user_rate_limit_message())
            return MAIN_MENU
        text = (
            "💬 *Чат поддержки*\n\n"
            "Напишите одно сообщение разработчику. Если хотите отменить — "
            "нажмите «❌ Отмена»."
        )
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]]),
            parse_mode="Markdown",
        )
        return SUPPORT_CHAT_MESSAGE

    elif message_text == "🤖 DEVORKS+ai":
        context.user_data['ai_mode'] = True
        # Режим личности ИИ: обычный / хамло / тепло (хранится в профиле).
        _ai_user = get_user(user_id) or user
        _persona = getattr(_ai_user, "ai_persona", "normal") or "normal"
        if _persona not in AI_PERSONA_MODES:
            _persona = "normal"
        _persona_row = AI_PERSONA_MODES[_persona]
        _persona_kb = [
            [InlineKeyboardButton(
                f"{'✅ ' if _persona == 'normal' else ''}{AI_PERSONA_MODES['normal']['button']}",
                callback_data="ai_persona_normal",
            )],
            [InlineKeyboardButton(
                f"{'✅ ' if _persona == 'hamlo' else ''}{AI_PERSONA_MODES['hamlo']['button']}",
                callback_data="ai_persona_hamlo",
            )],
            [InlineKeyboardButton(
                f"{'✅ ' if _persona == 'warm' else ''}{AI_PERSONA_MODES['warm']['button']}",
                callback_data="ai_persona_warm",
            )],
        ]
        await update.message.reply_text(
            "🤖 **Режим ai активирован!**\n\n"
            "Теперь все ваши сообщения будут отправляться DEVORKS+ai.\n"
            "Я всегда отвечаю на русском языке и всегда говорю только правду!\n\n"
            f"🎭 Текущий режим ИИ: *{_persona_row['title']}* — {_persona_row['description']}.\n"
            "Можно сменить режим в любой момент — ответ сразу изменится:\n\n"
            "Напишите ваш вопрос или нажмите '❌ Выход из ai' для выхода.",
            reply_markup=InlineKeyboardMarkup(_persona_kb),
            parse_mode=ParseMode.MARKDOWN
        )
        await update.message.reply_text(
            "✍️ Пишите — я на связи.",
            reply_markup=ReplyKeyboardMarkup([["❌ Выход из ai"]], resize_keyboard=True),
        )
        return MAIN_MENU

    elif message_text == "🪄 Автоматизация":
        # NEW: режим автоматизации на DeepSeek — свободный текст превращается
        # в действия (ДЗ на пятницу, замена учителей, таймеры и т. д.).
        return await automation_start(update, context)

    elif message_text == "👨‍💼 Админская панель":
        if class_obj and is_user_class_blocked(user_id, class_obj.class_code):
            await update.message.reply_text("Вы заблокированы в этом классе.")
            return MAIN_MENU
        return await admin_panel(update, context)

    elif message_text == "🛠️ Панель разработчика":
        return await developer_panel(update, context)

    elif message_text == "🚪 Выйти из класса":
        if class_obj and is_user_class_blocked(user_id, class_obj.class_code):
            await update.message.reply_text("Вы заблокированы в этом классе.")
            return MAIN_MENU
        return await leave_class(update, context)

    elif message_text == "🔓 Выйти из аккаунта":
        return await logout_user(update, context)

    if message_text == "📅 Сегодня":
        if class_obj and is_user_class_blocked(user_id, class_obj.class_code):
            await update.message.reply_text("Вы заблокированы в этом классе.")
            return MAIN_MENU

        # ИСПРАВЛЕНО: считаем «сегодня» в локальном времени пользователя.
        # Раньше использовался datetime.now() (время сервера), и в 00:07 у
        # пользователя бот показывал расписание прошлого дня.
        local_now = get_local_time(user)
        today_index = local_now.weekday()
        day_name = get_day_name(today_index)
        schedule = get_day_schedule(class_obj, day_name)

        today_str = local_now.strftime("%Y-%m-%d")
        homework = get_homework_for_date(class_obj, today_str)

        message = f"📅 Расписание на сегодня ({day_name}, {today_str}):\n\n{schedule}"

        if homework:
            hw_text = format_homework(homework, "сегодня").replace("**", "")
            message += "\n\n" + hw_text
        else:
            message += "\n\n📝 Домашнее задание на сегодня не задано."

        await update.message.reply_text(message)
        return MAIN_MENU

    elif message_text == "📅 Завтра":
        if class_obj and is_user_class_blocked(user_id, class_obj.class_code):
            await update.message.reply_text("Вы заблокированы в этом классе.")
            return MAIN_MENU

        # ИСПРАВЛЕНО: «завтра» — в локальном времени пользователя.
        tomorrow = get_local_time(user) + timedelta(days=1)
        tomorrow_index = tomorrow.weekday()
        day_name = get_day_name(tomorrow_index)
        schedule = get_day_schedule(class_obj, day_name)

        tomorrow_str = tomorrow.strftime("%Y-%m-%d")
        homework = get_homework_for_date(class_obj, tomorrow_str)

        message = f"📅 Расписание на завтра ({day_name}, {tomorrow_str}):\n\n{schedule}"

        if homework:
            hw_text = format_homework(homework, "завтра").replace("**", "")
            message += "\n\n" + hw_text
        else:
            message += "\n\n📝 Домашнее задание на завтра не задано."

        await update.message.reply_text(message)
        return MAIN_MENU

    elif message_text == "📅 Расписание":
        # Кнопка «📅 Расписание» — теперь сразу присылает ПОЛНОЕ расписание на
        # всю неделю (Пн–Вс) одним сообщением. Старый экран выбора дня
        # (handle_week_schedule, WEEK_SCHEDULE) НЕ удалён — он по-прежнему
        # работает как callback-флоу для других мест бота (например, при
        # редактировании расписания), но в главном меню по этой кнопке
        # пользователь получает всё расписание сразу.
        if class_obj and is_user_class_blocked(user_id, class_obj.class_code):
            await update.message.reply_text("Вы заблокированы в этом классе.")
            return MAIN_MENU
        if not class_obj:
            await update.message.reply_text(
                "Вы не состоите в классе.\n"
                "Создайте или вступите в класс через «🎓 Управление классами»."
            )
            return MAIN_MENU

        # Собираем расписание на все 7 дней недели в одно сообщение.
        week_days = [
            "Понедельник", "Вторник", "Среда", "Четверг",
            "Пятница", "Суббота", "Воскресенье",
        ]
        parts = [f"📅 Расписание на неделю — {class_obj.class_name}\n"]
        for day in week_days:
            day_schedule = get_day_schedule(class_obj, day)
            parts.append(f"\n📌 {day}:\n{day_schedule}")
        full_text = "\n".join(parts)

        # У Telegram есть лимит ~4096 символов на сообщение. Если расписание
        # очень длинное (большой класс с длинными комментариями) — режем по
        # дням, чтобы пользователь точно получил всё, а не урезанный кусок.
        MAX_LEN = 4000
        if len(full_text) <= MAX_LEN:
            await update.message.reply_text(full_text)
        else:
            chunk = parts[0]  # шапка
            for piece in parts[1:]:
                if len(chunk) + len(piece) > MAX_LEN:
                    await update.message.reply_text(chunk)
                    chunk = piece
                else:
                    chunk += "\n" + piece if chunk else piece
            if chunk.strip():
                await update.message.reply_text(chunk)
        return MAIN_MENU

    elif message_text == "📝 Домашнее задание":
        if class_obj:
            if is_user_class_blocked(user_id, class_obj.class_code):
                await update.message.reply_text("Вы заблокированы в этом классе.")
                return MAIN_MENU

            # Сгруппированный по дате вывод (дата сверху каждого блока).
            hw_text = format_homework(class_obj.homework)
            # Разбиваем по 4000 символов, чтобы не упереться в лимит Telegram
            # (4096), сохраняя границы блоков-«дней» там, где это возможно.
            MAX_LEN = 4000
            if len(hw_text) <= MAX_LEN:
                await update.message.reply_text(hw_text, parse_mode="Markdown")
            else:
                # Делим по двойным переводам строк (между блоками-датами).
                parts = hw_text.split("\n\n")
                buf = ""
                for piece in parts:
                    addition = (piece if not buf else "\n\n" + piece)
                    if len(buf) + len(addition) > MAX_LEN:
                        if buf.strip():
                            await update.message.reply_text(buf, parse_mode="Markdown")
                        buf = piece
                    else:
                        buf += addition
                if buf.strip():
                    await update.message.reply_text(buf, parse_mode="Markdown")
        else:
            await update.message.reply_text("Вы не состоите в классе.")
        return MAIN_MENU

    elif message_text == "🔔 Звонки":
        if class_obj and is_user_class_blocked(user_id, class_obj.class_code):
            await update.message.reply_text("Вы заблокированы в этом классе.")
            return MAIN_MENU

        # Передаём user, чтобы время «сейчас» бралось по его локали.
        bells_info = get_bells_info(class_obj, user)
        await update.message.reply_text(bells_info)
        return MAIN_MENU

    elif message_text == "👨‍🏫 Учителя":
        if class_obj and is_user_class_blocked(user_id, class_obj.class_code):
            await update.message.reply_text("Вы заблокированы в этом классе.")
            return MAIN_MENU

        if class_obj and class_obj.teachers:
            teachers_text = "👨‍🏫 Учителя:\n\n"
            for subject, teacher in class_obj.teachers.items():
                teachers_text += f"📚 **{subject}:** {teacher}\n"
        else:
            teachers_text = "👨‍🏫 Учителя не добавлены"
        await update.message.reply_text(teachers_text, parse_mode="Markdown")
        return MAIN_MENU

    elif message_text == "🎉 Каникулы":
        if class_obj and is_user_class_blocked(user_id, class_obj.class_code):
            await update.message.reply_text("Вы заблокированы в этом классе.")
            return MAIN_MENU

        # Передаём user, чтобы расчёт дней до каникул использовал его
        # локальный день, а не день сервера.
        holidays_info = get_holidays_count(class_obj, user)
        await update.message.reply_text(holidays_info)
        return MAIN_MENU

    elif message_text == "💬 Написать админу":
        if class_obj:
            if is_user_class_blocked(user_id, class_obj.class_code):
                await update.message.reply_text("Вы заблокированы в этом классе.")
                return MAIN_MENU

            context.user_data['messaging_admin'] = True
            await update.message.reply_text("💬 Напишите сообщение для админа класса:")
        else:
            await update.message.reply_text("Вы не состоите в классе.")
        return MAIN_MENU

    elif message_text == "⏰ Таймер":
        keyboard = get_quick_timer_keyboard()
        await update.message.reply_text(
            "⏰ **Установка таймера**\n\nВыберите быстрый таймер или задайте своё время:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        return TIMER_SET_DATE

    elif message_text == "🕵️ Анонимное сообщение":
        if class_obj:
            if is_user_class_blocked(user_id, class_obj.class_code):
                await update.message.reply_text("Вы заблокированы в этом классе.")
                return MAIN_MENU

            await update.message.reply_text("🕵️ Выберите одноклассника для анонимного сообщения:", reply_markup=get_class_users_keyboard(class_obj))
            return ANONYMOUS_SELECT_USER
        else:
            await update.message.reply_text("Вы не состоите в классе.")
        return MAIN_MENU

    if context.user_data.get('messaging_admin'):
        if class_obj:
            for admin_id in class_obj.admins:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"💬 Сообщение от ученика ({user.first_name}):\n\n{message_text}"
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки администратору {admin_id}: {e}")

            await update.message.reply_text("✅ Сообщение отправлено администраторам класса!")
            context.user_data.pop('messaging_admin', None)
            return MAIN_MENU

    # === ГЛОБАЛЬНАЯ АВТОМАТИЗАЦИЯ И АЛИАСЫ КНОПОК (текст И голос) ===
    # Нераспознанный текст в главном меню больше не игнорируется молча:
    # 1) совпал с алиасом кнопки («домашка», «учителя») → открываем кнопку;
    # 2) иначе — свободный запрос уходит в автоматизацию DeepSeek одним
    #    действием (голосовые сообщения уже превращены в текст middleware'ом).
    if message_text and not message_text.startswith("/"):
        _low = message_text.strip().lower()
        _alias_target = _MENU_TEXT_ALIASES.get(_low)
        if _alias_target and _alias_target != message_text:
            import copy as _copy_alias
            try:
                _fake_update = _copy_alias.copy(update)
                _fake_msg = _copy_alias.copy(update.message)
                object.__setattr__(_fake_msg, "text", _alias_target)
                _fake_update.message = _fake_msg
                return await handle_main_menu(_fake_update, context)
            except Exception as e:
                logger.error(f"menu alias routing ({_low} -> {_alias_target}): {e}")
        result_state = await _run_automation_oneshot(update, context, user, class_obj, message_text)
        if result_state is not None:
            return result_state

    return MAIN_MENU


# ==================================
# === ДОМАШНЕЕ ЗАДАНИЕ С ДАТОЙ ===
# ==================================

@timeout(CONVERSATION_TIMEOUT)
async def enter_homework_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    date_str = update.message.text.strip()

    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        context.user_data['homework_date'] = date_str

        await update.message.reply_text(
            f"📅 Дата: {date_str}\n\n"
            f"📝 Введите домашнее задание в формате:\n\n"
            f"Предмет: Задание\n\n"
            f"Например:\n"
            f"Математика: Решить задачи 1-5\n"
            f"Литература: Прочитать главу 3",
            reply_markup=get_cancel_keyboard()
        )
        return QUICK_ADD_HOMEWORK

    except ValueError:
        await update.message.reply_text(
            "Введите дату в формате ГГГГ-ММ-ДД:",
            reply_markup=get_cancel_keyboard()
        )
        return ENTER_HOMEWORK_DATE

# ==================================
# === ПРОСМОТР АНОНИМНЫХ СООБЩЕНИЙ ===
# ==================================

async def view_anonymous_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)

    keyboard = get_anonymous_messages_keyboard(user_id)

    if not keyboard:
        await query.edit_message_text(
            "📭 У вас нет анонимных сообщений.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]])
        )
        return MAIN_MENU

    await query.edit_message_text(
        "🕵️ **Ваши анонимные сообщения:**\n\n"
        "👁 - вы уже смотрели отправителя\n"
        "🕵️ - отправитель скрыт\n\n"
        "Выберите сообщение для просмотра:",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )
    return MAIN_MENU

async def view_anon_message_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        # Если callback пришёл «протухшим» (>15 минут), Telegram отдаст ошибку
        # на answer(). Игнорируем — главное, чтобы дальше успели открыть детали.
        pass

    user_id = str(query.from_user.id)

    # ИСПРАВЛЕНО: раньше msg_id извлекался как `query.data.split("_")[3]`.
    # Этот индекс опирался на то, что в msg_id не будет подчёркиваний, но
    # старые сообщения (или будущие правки генератора msg_id) могли
    # содержать «_», и тогда мы получали обрезанный msg_id и попадали в
    # ветку «Сообщение не найдено.» — именно отсюда жалоба «не могу
    # посмотреть свои анонимные сообщения».
    #
    # Теперь снимаем фиксированный префикс — это работает и для UUID, и для
    # любых других форм идентификатора.
    raw = query.data or ""
    msg_id = raw[len("view_anon_msg_"):] if raw.startswith("view_anon_msg_") else raw

    anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})

    if not msg_id or msg_id not in anonymous_messages:
        # Не редактируем сообщение два раза подряд (старая реализация
        # делала edit «Сообщение не найдено.» + ещё один edit из
        # view_anonymous_messages — Telegram мог ответить
        # «Message is not modified» и пользователь видел зависший экран).
        # Сразу показываем актуальный список.
        return await view_anonymous_messages(update, context)

    msg = anonymous_messages[msg_id]

    if str(msg.get('to_user_id')) != str(user_id):
        # То же самое: не показываем тоаст-обманку, сразу даём актуальный
        # список, чтобы избежать race condition с двойным edit'ом.
        return await view_anonymous_messages(update, context)

    view_sender_price = PRICES.get('view_sender', 60)

    text = f"🕵️ Анонимное сообщение\n\n"
    text += f"📅 Дата: {msg.get('timestamp', 'Неизвестно')}\n"
    text += f"📝 Сообщение: {msg.get('message', '')}\n\n"

    if msg.get('sender_viewed', False):
        sender = get_user(msg.get('from_user_id', ''))
        sender_name = sender.first_name if sender else "Неизвестно"
        text += f"👤 Отправитель: {sender_name}\n\n✅ Вы уже оплатили просмотр"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад к списку", callback_data="view_anon_list")],
            [InlineKeyboardButton("💬 Ответить анонимно", callback_data=f"reply_anon_{msg_id}")],
            # Добавлена кнопка удаления этой одной анонимки прямо из деталей
            # сообщения — пользователь просил возможность чистить «по одному».
            [InlineKeyboardButton("🗑 Удалить это сообщение", callback_data=f"anon_del_{msg_id}")],
        ])
    else:
        text += f"👤 Отправитель: 🕵️ Скрыт\n\n💰 Узнать отправителя: {view_sender_price} ⭐"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"👁 Узнать отправителя ({view_sender_price} ⭐)", callback_data=f"pay_view_sender_{msg_id}")],
            [InlineKeyboardButton("⬅️ Назад к списку", callback_data="view_anon_list")],
            [InlineKeyboardButton("💬 Ответить анонимно", callback_data=f"reply_anon_{msg_id}")],
            [InlineKeyboardButton("🗑 Удалить это сообщение", callback_data=f"anon_del_{msg_id}")],
        ])

    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except Exception as e:
        # «Message is not modified» / прочие edit-ошибки: пишем в лог и
        # пытаемся отдать тот же контент новым сообщением — пользователь
        # не должен оставаться без ответа.
        logger.error(f"view_anon_message_detail edit_message_text: {e}")
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                reply_markup=keyboard,
            )
        except Exception as e2:
            logger.error(f"view_anon_message_detail fallback send: {e2}")
    return MAIN_MENU

# ==================================
# === ЛИЧНЫЕ КНОПКИ ===
# ==================================

@timeout(CONVERSATION_TIMEOUT)
async def personal_buttons_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    buttons_count = get_personal_buttons_count(user_id)

    text = f"🌟 **Мои личные кнопки**\n\n📊 Количество: {buttons_count}/{user.max_personal_buttons}\n\n👇 *Выберите действие:*\n\n*Личные кнопки видны только вам и доступны всегда, даже без класса!*"

    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, reply_markup=get_personal_buttons_keyboard(user), parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=get_personal_buttons_keyboard(user), parse_mode="Markdown")
    return PERSONAL_BUTTON_MANAGEMENT

@timeout(CONVERSATION_TIMEOUT)
async def create_personal_button_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    buttons_count = get_personal_buttons_count(user_id)

    max_buttons = 1
    price = get_button_price(buttons_count)

    # ПУНКТ (покупка кнопки): если пользователь уже заранее оплатил кнопку через
    # Telegram Stars (prepaid_buttons > 0), пропускаем экран "Купить" и сразу идём на создание.
    if buttons_count >= max_buttons and getattr(user, 'prepaid_buttons', 0) <= 0:
        text = f"❌ Достигнут лимит бесплатных кнопок!\n\nСледующая кнопка стоит {price} ⭐\n\nКупить кнопку за {price} ⭐?"

        keyboard = [
            [InlineKeyboardButton("✅ Купить кнопку", callback_data=f"buy_button_{price}")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]
        ]

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return PURCHASE_BUTTON

    context.user_data['creating_personal_button'] = True
    text = "🌟 **Создание личной кнопки**\n\n👇 *Выберите тип кнопки:*"

    await query.edit_message_text(text, reply_markup=get_button_type_keyboard(include_cancel=False), parse_mode="Markdown")
    return PERSONAL_BUTTON_SELECT_TYPE

@timeout(CONVERSATION_TIMEOUT)
async def purchase_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик клика по «✅ Купить кнопку» (callback buy_button_<price>).

    Логика:
    - Если внутреннего баланса (stars_balance) хватает → списываем и сразу ведём
      в мастер создания кнопки.
    - Иначе — слём счёт через Telegram Stars (XTR). После успешной оплаты
      successful_payment_handler увеличит user.prepaid_buttons и предложит
      кнопку «Создать кнопку». Раньше этот callback вообще нигде не
      маршрутировался — именно поэтому «кнопка не покупалась».
    """
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    try:
        price = int(query.data.split("_")[2])
    except (ValueError, IndexError):
        await query.edit_message_text("Некорректная цена.")
        return MAIN_MENU

    # Если внутреннего баланса хватает — списываем и ведём на создание кнопки.
    if user.stars_balance >= price:
        add_stars_transaction(user_id, -price, "Покупка личной кнопки")
        context.user_data['creating_personal_button'] = True

        text = "🌟 **Создание личной кнопки**\n\n👇 *Выберите тип кнопки:*"
        await query.edit_message_text(
            text,
            reply_markup=get_button_type_keyboard(include_cancel=False),
            parse_mode="Markdown",
        )
        return PERSONAL_BUTTON_SELECT_TYPE

    # Баланса не хватает — слём инвойс в Telegram Stars.
    try:
        prices_list = [LabeledPrice(label=f"Личная кнопка", amount=price)]
        await context.bot.send_invoice(
            chat_id=user_id,
            title="Покупка личной кнопки",
            description=f"Оплата дополнительной личной кнопки ({price} ⭐)",
            payload=f"buy_button_{price}",
            provider_token="",
            currency="XTR",
            prices=prices_list,
            start_parameter="buy-button",
        )
        await query.edit_message_text(
            f"💳 Отправил счёт на {price} ⭐. Оплатите в диалоге ниже.\n"
            f"После оплаты бот предложит создать кнопку."
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке счёта на кнопку: {e}")
        await query.edit_message_text(
            "❌ Не удалось создать счёт. Попробуйте ещё раз или купите звёзды напрямую.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Купить звезды", callback_data="buy_stars")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_personal_buttons")],
            ]),
        )
    return MAIN_MENU

@timeout(CONVERSATION_TIMEOUT)
async def personal_button_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    button_type = query.data.split("_")[2]

    context.user_data['personal_button_type'] = button_type
    context.user_data.pop('creating_personal_button', None)

    text = "📝 Введите название кнопки:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return CREATE_PERSONAL_BUTTON_NAME

@timeout(CONVERSATION_TIMEOUT)
async def create_personal_button_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    button_name = update.message.text.strip()

    if not button_name:
        error_text = "Введите название."
        await update.message.reply_text(error_text)
        return CREATE_PERSONAL_BUTTON_NAME

    rejected = await reject_if_forbidden_chars(update, button_name, CREATE_PERSONAL_BUTTON_NAME)
    if rejected is not None:
        return rejected

    context.user_data['personal_button_name'] = button_name
    button_type = context.user_data.get('personal_button_type', 'text')

    if button_type == "url":
        text = "🔗 Введите URL ссылки:"
        await update.message.reply_text(text, reply_markup=get_cancel_keyboard())
        return CREATE_PERSONAL_BUTTON_URL
    else:
        text = "📝 Введите содержимое кнопки:"
        await update.message.reply_text(text, reply_markup=get_cancel_keyboard())
        return CREATE_PERSONAL_BUTTON_CONTENT

@timeout(CONVERSATION_TIMEOUT)
async def create_personal_button_url_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    url = update.message.text.strip()

    if not url:
        error_text = "Введите URL."
        await update.message.reply_text(error_text)
        return CREATE_PERSONAL_BUTTON_URL

    if not (url.startswith('http://') or url.startswith('https://')):
        url = 'https://' + url

    button_name = context.user_data.get('personal_button_name')

    button_id = generate_personal_button_id()
    personal_button = PersonalButton(
        button_id=button_id,
        user_id=user_id,
        name=button_name,
        content=url,
        button_type="url"
    )

    save_personal_button(personal_button)

    user = get_user(user_id)
    if user:
        if not user.personal_button_order:
            user.personal_button_order = []
        user.personal_button_order.append(button_id)
        # ПУНКТ (покупка кнопки): используем один оплаченный слот (если есть).
        if getattr(user, 'prepaid_buttons', 0) > 0:
            user.prepaid_buttons = max(0, user.prepaid_buttons - 1)
        save_user(user)

    success_text = f"✅ Личная кнопка '{button_name}' успешно создана!\n\nТеперь она появится в вашем главном меню."

    # Автоматически обновляем клавиатуру снизу — без повторного /start.
    try:
        fresh_user = get_user(user_id) or user
        await update.message.reply_text(
            success_text,
            reply_markup=get_main_menu_keyboard(fresh_user),
        )
    except Exception as e:
        logger.error(f"create_personal_button_url_handler: обновление клавиатуры: {e}")
        await update.message.reply_text(success_text)

    context.user_data.pop('personal_button_type', None)
    context.user_data.pop('personal_button_name', None)

    return await personal_buttons_menu_from_message(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def create_personal_button_content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    content = update.message.text.strip()

    if not content:
        error_text = "Введите содержимое."
        await update.message.reply_text(error_text)
        return CREATE_PERSONAL_BUTTON_CONTENT

    rejected = await reject_if_forbidden_chars(update, content, CREATE_PERSONAL_BUTTON_CONTENT)
    if rejected is not None:
        return rejected

    button_name = context.user_data.get('personal_button_name')

    button_id = generate_personal_button_id()
    personal_button = PersonalButton(
        button_id=button_id,
        user_id=user_id,
        name=button_name,
        content=content,
        button_type="text"
    )

    save_personal_button(personal_button)

    user = get_user(user_id)
    if user:
        if not user.personal_button_order:
            user.personal_button_order = []
        user.personal_button_order.append(button_id)
        # ПУНКТ (покупка кнопки): используем один оплаченный слот (если есть).
        if getattr(user, 'prepaid_buttons', 0) > 0:
            user.prepaid_buttons = max(0, user.prepaid_buttons - 1)
        save_user(user)

    success_text = f"✅ Личная кнопка '{button_name}' успешно создана!\n\nТеперь она появится в вашем главном меню."

    # Автоматически обновляем клавиатуру снизу — без повторного /start.
    try:
        fresh_user = get_user(user_id) or user
        await update.message.reply_text(
            success_text,
            reply_markup=get_main_menu_keyboard(fresh_user),
        )
    except Exception as e:
        logger.error(f"create_personal_button_content_handler: обновление клавиатуры: {e}")
        await update.message.reply_text(success_text)

    context.user_data.pop('personal_button_type', None)
    context.user_data.pop('personal_button_name', None)

    return await personal_buttons_menu_from_message(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def manage_personal_buttons_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    buttons_count = get_personal_buttons_count(user_id)

    if buttons_count == 0:
        text = "🌟 **Управление личными кнопками**\n\n📊 У вас пока нет личных кнопок.\n\n👇 *Создайте свою первую кнопку:*"

        keyboard = [
            [InlineKeyboardButton("➕ Создать кнопку", callback_data="create_personal_button")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_personal_buttons")]
        ]
    else:
        text = f"🌟 **Управление личными кнопками**\n\n📊 Количество: {buttons_count}/{user.max_personal_buttons}\n\n👇 *Выберите кнопку для редактирования:*"

        keyboard = []
        buttons = get_personal_buttons(user_id)
        for button in buttons:
            keyboard.append([InlineKeyboardButton(f"✏️ {button.name}", callback_data=f"edit_personal_button_{button.button_id}")])

        keyboard.append([InlineKeyboardButton("➕ Добавить кнопку", callback_data="create_personal_button")])
        keyboard.append([InlineKeyboardButton("🔄 Изменить порядок", callback_data="reorder_personal_buttons")])
        keyboard.append([InlineKeyboardButton("📋 Изменить ряд", callback_data="change_button_row")])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_personal_buttons")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return MANAGE_PERSONAL_BUTTONS

@timeout(CONVERSATION_TIMEOUT)
async def edit_personal_button_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    button_id = query.data.split("_")[3]

    buttons = get_personal_buttons(user_id)
    button = next((b for b in buttons if b.button_id == button_id), None)

    if not button:
        error_text = "Кнопка не найдена."
        await query.edit_message_text(error_text)
        return await manage_personal_buttons_start(update, context)

    context.user_data['editing_personal_button_id'] = button_id

    text = f"✏️ **Редактирование личной кнопки**\n\n📝 *Название:* {button.name}\n🔗 *Тип:* {'Текст' if button.button_type == 'text' else 'Ссылка'}\n📄 *Содержимое:* {button.content[:50]}...\n\n👇 *Выберите, что хотите изменить:*"

    keyboard = [
        [InlineKeyboardButton("📝 Изменить название", callback_data=f"edit_personal_button_name_{button_id}")],
        [InlineKeyboardButton("📄 Изменить содержимое", callback_data=f"edit_personal_button_content_{button_id}")],
        [InlineKeyboardButton("🔄 Изменить тип", callback_data=f"edit_personal_button_type_{button_id}")],
        [InlineKeyboardButton("🗑️ Удалить кнопку", callback_data=f"delete_personal_button_{button_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_manage_personal_buttons")]
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return EDIT_PERSONAL_BUTTON

@timeout(CONVERSATION_TIMEOUT)
async def edit_personal_button_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    button_id = query.data.split("_")[4]

    context.user_data['editing_personal_button_field'] = 'name'
    context.user_data['editing_personal_button_id'] = button_id

    text = "📝 Введите новое название кнопки:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return EDIT_PERSONAL_BUTTON_NAME

@timeout(CONVERSATION_TIMEOUT)
async def edit_personal_button_content_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    button_id = query.data.split("_")[4]

    context.user_data['editing_personal_button_field'] = 'content'
    context.user_data['editing_personal_button_id'] = button_id

    text = "📝 Введите новое содержимое кнопки:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return EDIT_PERSONAL_BUTTON_CONTENT

@timeout(CONVERSATION_TIMEOUT)
async def edit_personal_button_type_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    button_id = query.data.split("_")[4]

    context.user_data['editing_personal_button_id'] = button_id

    text = "🔄 Выберите новый тип кнопки:"

    await query.edit_message_text(text, reply_markup=get_button_type_keyboard(include_cancel=False))
    return PERSONAL_BUTTON_SELECT_TYPE

@timeout(CONVERSATION_TIMEOUT)
async def delete_personal_button_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    button_id = query.data.split("_")[3]

    buttons = get_personal_buttons(user_id)
    button = next((b for b in buttons if b.button_id == button_id), None)

    if not button:
        error_text = "Кнопка не найдена."
        await query.edit_message_text(error_text)
        return await manage_personal_buttons_start(update, context)

    context.user_data['deleting_personal_button_id'] = button_id

    text = f"🗑️ **Удаление личной кнопки**\n\n📝 *Кнопка:* {button.name}\n\n⚠️ *Вы уверены, что хотите удалить эту кнопку?*\n\n❌ *Это действие нельзя отменить!*"

    await query.edit_message_text(text, reply_markup=get_personal_button_delete_keyboard(user, button_id), parse_mode="Markdown")
    return EDIT_PERSONAL_BUTTON

async def confirm_delete_personal_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    button_id = query.data.split("_")[4]

    if delete_personal_button(button_id):
        user = get_user(user_id)
        if user and button_id in user.personal_button_order:
            user.personal_button_order.remove(button_id)
            save_user(user)

        success_text = "✅ Личная кнопка успешно удалена!"
    else:
        success_text = "Ошибка при удалении кнопки."

    await query.edit_message_text(success_text)
    # Автоматически обновляем клавиатуру снизу — без повторного /start.
    user = get_user(user_id)
    if user:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="⚙️ Меню обновлено!",
                reply_markup=get_main_menu_keyboard(user),
            )
        except Exception as e:
            logger.error(f"confirm_delete_personal_button: обновление клавиатуры: {e}")
    return await manage_personal_buttons_start(update, context)

async def cancel_delete_personal_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    button_id = query.data.split("_")[4]

    context.user_data.pop('deleting_personal_button_id', None)

    return await edit_personal_button_start(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def save_personal_button_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    new_value = update.message.text.strip()

    button_id = context.user_data.get('editing_personal_button_id')
    field = context.user_data.get('editing_personal_button_field')

    if not button_id or not field:
        return await manage_personal_buttons_start(update, context)

    buttons = load_personal_buttons()
    if button_id not in buttons:
        error_text = "Кнопка не найдена."
        await update.message.reply_text(error_text)
        return await manage_personal_buttons_start(update, context)

    if field == 'name':
        if not new_value:
            error_text = "Введите название."
            await update.message.reply_text(error_text)
            return EDIT_PERSONAL_BUTTON_NAME

        rejected = await reject_if_forbidden_chars(update, new_value, EDIT_PERSONAL_BUTTON_NAME)
        if rejected is not None:
            return rejected

        buttons[button_id].name = new_value
        success_text = "✅ Название кнопки обновлено!"

    elif field == 'content':
        if not new_value:
            error_text = "Введите содержимое."
            await update.message.reply_text(error_text)
            return EDIT_PERSONAL_BUTTON_CONTENT

        rejected = await reject_if_forbidden_chars(update, new_value, EDIT_PERSONAL_BUTTON_CONTENT)
        if rejected is not None:
            return rejected

        buttons[button_id].content = new_value
        success_text = "✅ Содержимое кнопки обновлено!"

    save_personal_buttons(buttons)

    context.user_data.pop('editing_personal_button_id', None)
    context.user_data.pop('editing_personal_button_field', None)

    # Автообновление клавиатуры снизу — без повторного /start.
    try:
        fresh_user = get_user(user_id) or user
        await update.message.reply_text(
            success_text,
            reply_markup=get_main_menu_keyboard(fresh_user),
        )
    except Exception as e:
        logger.error(f"save_personal_button_edit: обновление клавиатуры: {e}")
        await update.message.reply_text(success_text)
    return await manage_personal_buttons_start(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def save_personal_button_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    button_type = query.data.split("_")[2]
    button_id = context.user_data.get('editing_personal_button_id')

    if not button_id:
        return await manage_personal_buttons_start(update, context)

    buttons = load_personal_buttons()
    if button_id not in buttons:
        error_text = "Кнопка не найдена."
        await query.edit_message_text(error_text)
        return await manage_personal_buttons_start(update, context)

    buttons[button_id].button_type = button_type

    if button_type == "url" and not buttons[button_id].content.startswith('http'):
        save_personal_buttons(buttons)
        text = "🔗 Введите новый URL для кнопки:"
        await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
        return EDIT_PERSONAL_BUTTON_URL
    elif button_type == "url":
        save_personal_buttons(buttons)
        success_text = "✅ Тип кнопки изменен на ссылку!"
        await query.edit_message_text(success_text)
        context.user_data.pop('editing_personal_button_id', None)
        # Автообновление клавиатуры снизу.
        fresh_user = get_user(user_id) or user
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="⚙️ Меню обновлено!",
                reply_markup=get_main_menu_keyboard(fresh_user),
            )
        except Exception as e:
            logger.error(f"save_personal_button_type (url): обновление клавиатуры: {e}")
        return await manage_personal_buttons_start(update, context)
    else:
        save_personal_buttons(buttons)
        success_text = "✅ Тип кнопки изменен на текстовый!"
        await query.edit_message_text(success_text)
        context.user_data.pop('editing_personal_button_id', None)
        # Автообновление клавиатуры снизу.
        fresh_user = get_user(user_id) or user
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="⚙️ Меню обновлено!",
                reply_markup=get_main_menu_keyboard(fresh_user),
            )
        except Exception as e:
            logger.error(f"save_personal_button_type (text): обновление клавиатуры: {e}")
        return await manage_personal_buttons_start(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def save_personal_button_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    url = update.message.text.strip()
    button_id = context.user_data.get('editing_personal_button_id')

    if not button_id:
        return await manage_personal_buttons_start(update, context)

    if not url:
        error_text = "Введите URL."
        await update.message.reply_text(error_text)
        return EDIT_PERSONAL_BUTTON_URL

    if not (url.startswith('http://') or url.startswith('https://')):
        url = 'https://' + url

    buttons = load_personal_buttons()
    if button_id not in buttons:
        error_text = "Кнопка не найдена."
        await update.message.reply_text(error_text)
        return await manage_personal_buttons_start(update, context)

    buttons[button_id].content = url
    save_personal_buttons(buttons)

    success_text = "✅ URL кнопки обновлен!"
    # Автообновление клавиатуры снизу — без повторного /start.
    try:
        fresh_user = get_user(user_id) or user
        await update.message.reply_text(
            success_text,
            reply_markup=get_main_menu_keyboard(fresh_user),
        )
    except Exception as e:
        logger.error(f"save_personal_button_url: обновление клавиатуры: {e}")
        await update.message.reply_text(success_text)

    context.user_data.pop('editing_personal_button_id', None)
    return await manage_personal_buttons_start(update, context)

# ==================================
# === ПЕРЕМЕЩЕНИЕ КНОПОК ===
# ==================================

async def move_button_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    buttons = get_personal_buttons(user_id)

    if not buttons:
        await query.edit_message_text("У вас нет личных кнопок.")
        return await manage_personal_buttons_start(update, context)

    text = "🔄 **Перемещение кнопки**\n\nВыберите кнопку для перемещения:"

    keyboard = []
    for button in buttons:
        keyboard.append([InlineKeyboardButton(f"📝 {button.name}", callback_data=f"select_move_{button.button_id}")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_manage_personal_buttons")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return MOVE_BUTTONS

async def select_button_to_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    button_id = query.data.replace("select_move_", "")

    buttons = get_personal_buttons(user_id)
    button = next((b for b in buttons if b.button_id == button_id), None)

    if not button:
        await query.edit_message_text("Кнопка не найдена.")
        return await manage_personal_buttons_start(update, context)

    context.user_data['moving_button_id'] = button_id

    text = f"🔄 **Перемещение кнопки: {button.name}**\n\nИспользуйте стрелки для перемещения влево/вправо:"

    await query.edit_message_text(text, reply_markup=get_move_button_keyboard(button, buttons))
    return MOVE_BUTTONS

async def move_button_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    parts = query.data.split("_")
    direction = parts[2]
    button_id = parts[3]

    user = get_user(user_id)
    if not user:
        user = User(user_id)

    buttons = get_personal_buttons(user_id)

    if user.personal_button_order:
        current_order = user.personal_button_order.copy()
    else:
        current_order = [b.button_id for b in buttons]

    if button_id in current_order:
        current_index = current_order.index(button_id)

        if direction == "left" and current_index > 0:
            current_order[current_index], current_order[current_index - 1] =                 current_order[current_index - 1], current_order[current_index]

        elif direction == "right" and current_index < len(current_order) - 1:
            current_order[current_index], current_order[current_index + 1] =                 current_order[current_index + 1], current_order[current_index]

        user.personal_button_order = current_order
        save_user(user)

    updated_buttons = get_personal_buttons(user_id)
    button = next((b for b in updated_buttons if b.button_id == button_id), None)

    if button:
        text = f"🔄 **Перемещение кнопки: {button.name}**\n\nИспользуйте стрелки для перемещения:"
        await query.edit_message_text(
            text, 
            reply_markup=get_move_button_keyboard(button, updated_buttons),
            parse_mode=ParseMode.MARKDOWN
        )

    return MOVE_BUTTONS

async def change_button_row_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    button_id = query.data.split("_")[2]

    text = "📋 **Изменение ряда кнопки**\n\nВыберите новый ряд (1-4):"

    await query.edit_message_text(text, reply_markup=get_row_selection_keyboard(button_id))
    return MOVE_BUTTONS

async def set_button_row(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    button_id = query.data.split("_")[2]
    row_num = int(query.data.split("_")[3])

    user_id = str(query.from_user.id)
    buttons = load_personal_buttons()
    if button_id in buttons:
        buttons[button_id].row = row_num
        save_personal_buttons(buttons)

        await query.edit_message_text(f"✅ Ряд изменен на {row_num}!")
        # Автоматически обновляем нижнюю клавиатуру — без повторного /start.
        user = get_user(user_id)
        if user:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⚙️ Меню обновлено!",
                    reply_markup=get_main_menu_keyboard(user),
                )
            except Exception as e:
                logger.error(f"set_button_row: обновление клавиатуры: {e}")
    else:
        await query.edit_message_text("Кнопка не найдена.")

    return await manage_personal_buttons_start(update, context)

# ==================================
# === ИЗМЕНЕНИЕ ПОРЯДКА РЯДОВ ===
# ==================================

async def reorder_rows_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)

    text = "📋 **Изменение порядка рядов**\n\nНажмите на ряд, чтобы поменять его с соседним:"

    await query.edit_message_text(text, reply_markup=get_reorder_rows_keyboard(user))
    return REORDER_BUTTONS

async def select_row_to_reorder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    row_num = int(parts[2])

    if len(parts) == 3:
        text = f"📋 **Ряд {row_num}**\n\nВыберите направление перемещения:"
        await query.edit_message_text(text, reply_markup=get_row_reorder_keyboard(row_num))
        return REORDER_BUTTONS

    direction = parts[2]
    row_num = int(parts[3])

    return await move_row_direction(update, context)

async def move_row_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)

    parts = query.data.split("_")
    direction = parts[2]
    row_num = int(parts[3])

    buttons = get_personal_buttons(user_id)

    if direction == "up" and row_num > 1:
        target_row = row_num - 1
    elif direction == "down" and row_num < 4:
        target_row = row_num + 1
    else:
        await query.edit_message_text("Невозможно переместить ряд.")
        return REORDER_BUTTONS

    buttons_data = load_personal_buttons()
    changed = False

    for button in buttons:
        btn_row = getattr(button, 'row', 1)
        if btn_row == row_num:
            if button.button_id in buttons_data:
                buttons_data[button.button_id].row = target_row
                changed = True
        elif btn_row == target_row:
            if button.button_id in buttons_data:
                buttons_data[button.button_id].row = row_num
                changed = True

    if changed:
        save_personal_buttons(buttons_data)
        await query.edit_message_text(f"✅ Ряд {row_num} перемещен!")
    else:
        await query.edit_message_text("Не удалось переместить ряд.")

    user = get_user(user_id)
    await query.edit_message_text("📋 **Изменение порядка рядов**\n\nВыберите ряд:", 
                                  reply_markup=get_reorder_rows_keyboard(user))
    return REORDER_BUTTONS

async def finish_move_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data.pop('moving_button_id', None)

    user_id = str(query.from_user.id)
    user = get_user(user_id)

    await query.edit_message_text("✅ Изменения сохранены!")
    # Автоматически обновляем клавиатуру снизу — без повторного /start.
    if user:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="⚙️ Меню обновлено!",
                reply_markup=get_main_menu_keyboard(user),
            )
        except Exception as e:
            logger.error(f"finish_move_buttons: обновление клавиатуры: {e}")
    return await manage_personal_buttons_start(update, context)

async def finish_reorder_rows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)

    await query.edit_message_text("✅ Порядок рядов сохранен!")
    # Автоматически обновляем клавиатуру снизу — без повторного /start.
    if user:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="⚙️ Меню обновлено!",
                reply_markup=get_main_menu_keyboard(user),
            )
        except Exception as e:
            logger.error(f"finish_reorder_rows: обновление клавиатуры: {e}")
    return await manage_personal_buttons_start(update, context)

async def back_to_reorder_rows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)

    await query.edit_message_text("📋 **Изменение порядка рядов**\n\nВыберите ряд:", 
                                  reply_markup=get_reorder_rows_keyboard(user))
    return REORDER_BUTTONS

async def back_to_move_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    button_id = query.data.split("_")[3]

    buttons = get_personal_buttons(user_id)
    button = next((b for b in buttons if b.button_id == button_id), None)

    if button:
        text = f"🔄 **Перемещение кнопки: {button.name}**\n\nИспользуйте стрелки для перемещения:"
        await query.edit_message_text(text, reply_markup=get_move_button_keyboard(button, buttons))
        return MOVE_BUTTONS

    return await manage_personal_buttons_start(update, context)

# ==================================
# === ЗВЕЗДЫ И ПРЕМИУМ ===
# ==================================

async def stars_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    text = f"⭐ **Звезды**\n\n💰 Ваш баланс: {user.stars_balance} ⭐\n💸 Всего потрачено: {user.total_stars_spent} ⭐\n\nВыберите действие:"

    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, reply_markup=get_stars_keyboard(user), parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=get_stars_keyboard(user), parse_mode="Markdown")
    return MAIN_MENU

async def buy_stars_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    text = "💎 Выберите количество звезд для покупки:"

    await query.edit_message_text(text, reply_markup=get_stars_prices_keyboard())
    return MAIN_MENU

async def buy_stars_invoice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    amount = int(query.data.split("_")[2])

    prices = [LabeledPrice(label=f"{amount} ⭐", amount=amount)]

    await context.bot.send_invoice(
        chat_id=user_id,
        title="Покупка звезд",
        description="Пополнение баланса звезд",
        payload=f"buy_stars_{amount}",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="buy-stars"
    )

    return MAIN_MENU

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    payload = query.invoice_payload

    if payload.startswith("buy_stars_"):
        await query.answer(ok=True)
    elif payload.startswith("buy_button_"):
        await query.answer(ok=True)
    elif payload.startswith("buy_unblock_class_"):
        await query.answer(ok=True)
    elif payload.startswith("buy_unblock_"):
        await query.answer(ok=True)
    elif payload.startswith("buy_view_sender_"):
        await query.answer(ok=True)
    elif payload.startswith("buy_anon_space_"):
        # Покупка месяца защиты от авто-очистки анонимок (XTR).
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Неверный платеж")

    return MAIN_MENU

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    payment = update.message.successful_payment
    payload = payment.invoice_payload

    # === НОВОЕ: единая регистрация потраченных Stars ===
    # Подробной истории переводов больше нет: только агрегаты (общая сумма
    # + топ). payment.total_amount — сумма счёта; для XTR это ровно Stars.
    # Фиатные платежи (внешние провайдеры) в статистику Stars не попадают.
    try:
        _paid_amount = int(getattr(payment, "total_amount", 0) or 0)
    except (TypeError, ValueError):
        _paid_amount = 0
    if _paid_amount > 0 and getattr(payment, "currency", "") == "XTR":
        register_stars_spending(user_id, user.first_name, _paid_amount)

    if payload.startswith("buy_stars_"):
        amount = int(payload.split("_")[2])
        add_stars_transaction(user_id, amount, "Покупка звезд")

        await update.message.reply_text(
            f"✅ {amount} ⭐ успешно зачислены на ваш счет!"
        )

    elif payload.startswith("buy_unblock_class_"):
        class_code = payload.split("_")[3]
        class_obj = get_class_by_code(class_code)

        if class_obj and is_user_class_blocked(user_id, class_code):
            unblock_user_in_class(user_id, class_code)
            await update.message.reply_text(
                f"✅ Вы разблокированы в классе '{class_obj.class_name}'!"
            )

            for admin_id in class_obj.admins:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🔓 Пользователь {user.first_name} разблокировался в классе за звезды"
                    )
                except Exception as e:
                    logger.error(f"Ошибка при уведомлении админа {admin_id}: {e}")
        else:
            await update.message.reply_text("✅ Вы уже разблокированы или класс не найден.")

    elif payload.startswith("buy_unblock_"):
        unblock_user(user_id)
        await update.message.reply_text(
            f"✅ Вы разблокированы!"
        )

    elif payload.startswith("buy_button_"):
        # ПУНКТ (покупка кнопки): отмечаем оплаченный слот в user.prepaid_buttons,
        # чтобы create_personal_button_start не блокировал пользователя лимитом.
        try:
            user.prepaid_buttons = int(getattr(user, 'prepaid_buttons', 0)) + 1
        except Exception:
            user.prepaid_buttons = 1
        users = load_users()
        users[str(user_id)] = user
        save_users(users)
        try:
            amount = int(payload.split("_")[2])
        except (ValueError, IndexError):
            amount = 0
        try:
            user.total_stars_spent = int(getattr(user, 'total_stars_spent', 0)) + amount
            users[str(user_id)] = user
            save_users(users)
        except Exception:
            pass
        await update.message.reply_text(
            "✅ Оплата прошла успешно! Нажмите «Создать кнопку», чтобы продолжить.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Создать кнопку", callback_data="create_personal_button")],
                [InlineKeyboardButton("⬅️ В главное меню", callback_data="back_to_main")],
            ]),
        )
        return MAIN_MENU

    elif payload.startswith("buy_view_sender_"):
        message_id = pending_payments.get(user_id)

        if message_id:
            anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})
            msg = anonymous_messages.get(message_id)

            if msg and str(msg['to_user_id']) == str(user_id):
                sender = get_user(msg['from_user_id'])
                sender_name = sender.first_name if sender else "Неизвестно"

                anonymous_messages[message_id]['sender_viewed'] = True
                save_data(ANONYMOUS_MESSAGES_FILE, anonymous_messages)

                if user_id in pending_payments:
                    del pending_payments[user_id]

                await update.message.reply_text(
                    f"👤 Отправитель: {sender_name}\n📝 Сообщение: {msg['message']}\n\n✅ Оплачено {PRICES.get('view_sender', 60)} ⭐"
                )
            else:
                await update.message.reply_text("Сообщение не найдено.")
        else:
            await update.message.reply_text("Сообщение не найдено.")

    elif payload.startswith("buy_anon_space_"):
        # Оплачен месяц защиты входящих анонимок от авто-очистки.
        # Парсим amount только ради статистики потраченных звёзд — основной
        # эффект (продление anon_keep_until) делает _extend_user_anon_keep.
        try:
            amount = int(payload.split("_")[3])
        except (ValueError, IndexError):
            amount = PRICES.get('anon_keep_month', 100)
        new_until = _extend_user_anon_keep(user, months=1)
        try:
            user = get_user(user_id) or user
            user.total_stars_spent = int(getattr(user, 'total_stars_spent', 0)) + amount
            users = load_users()
            users[str(user_id)] = user
            save_users(users)
        except Exception:
            pass
        await update.message.reply_text(
            "✅ Готово! Место под ваши анонимные сообщения оплачено до "
            f"{new_until.strftime('%Y-%m-%d %H:%M UTC')}.\n\n"
            "В течение этого срока авто-очистка их не тронет.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📨 Мои анонимные сообщения", callback_data="view_anon_list")],
                [InlineKeyboardButton("⬅️ В главное меню", callback_data="back_to_main")],
            ]),
        )

async def create_paid_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    buttons_count = get_personal_buttons_count(user_id)
    price = get_button_price(buttons_count)

    if user.stars_balance >= price:
        prices = [LabeledPrice(label=f"Кнопка {buttons_count+1}", amount=price)]

        await context.bot.send_invoice(
            chat_id=user_id,
            title="Покупка кнопки",
            description=f"Создание личной кнопки (кнопка #{buttons_count+1})",
            payload=f"buy_button_{price}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="buy-button"
        )
        await query.edit_message_text("💳 Отправлен счет на оплату...")
    else:
        text = f"Недостаточно звёзд.\n\n💰 Баланс: {user.stars_balance} ⭐\n💎 Нужно: {price} ⭐\n\nКупите звезды."

        keyboard = [
            [InlineKeyboardButton("💎 Купить звезды", callback_data="buy_stars")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]
        ]

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    return MAIN_MENU

async def unblock_self_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_obj = get_class_by_user(user.user_id)
    is_class_blocked = class_obj and is_user_class_blocked(user.user_id, class_obj.class_code)

    if is_class_blocked:
        price = PRICES['unblock']

        if user.stars_balance >= price:
            add_stars_transaction(user_id, -price, f"Разблокировка в классе {class_obj.class_name}")
            unblock_user_in_class(user_id, class_obj.class_code)

            await query.edit_message_text(
                f"✅ Вы разблокированы в классе '{class_obj.class_name}'! Списано {price} ⭐"
            )

            for admin_id in class_obj.admins:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🔓 Пользователь {user.first_name} разблокировался в классе за {price} ⭐"
                    )
                except Exception as e:
                    logger.error(f"Ошибка при уведомлении админа {admin_id}: {e}")
        else:
            prices = [LabeledPrice(label="Разблокировка в классе", amount=price)]

            await context.bot.send_invoice(
                chat_id=user_id,
                title="Разблокировка в классе",
                description=f"Разблокировка в классе '{class_obj.class_name}'",
                payload=f"buy_unblock_class_{class_obj.class_code}",
                provider_token="",
                currency="XTR",
                prices=prices,
                start_parameter="unblock-class"
            )
            await query.edit_message_text("💳 Отправлен счет на оплату...")

        return await stars_menu(update, context)

    elif user.is_blocked:
        blocked_users = load_blocked_users()
        blocked_data = blocked_users.get(str(user_id), {})
        blocked_by = blocked_data.get('blocked_by')

        if blocked_by == DEVELOPER_ID:
            price = blocked_data.get('unblock_price', PRICES.get('unblock_dev', 100))
            block_type = "разработчиком"
        else:
            price = PRICES['unblock']
            block_type = "администратором"

        if user.stars_balance >= price:
            add_stars_transaction(user_id, -price, "Разблокировка аккаунта")
            unblock_user(user_id)

            await query.edit_message_text(
                f"✅ Вы разблокированы! Списано {price} ⭐\n(Блокировка была от {block_type})"
            )
        else:
            prices = [LabeledPrice(label="Разблокировка", amount=price)]

            await context.bot.send_invoice(
                chat_id=user_id,
                title="Разблокировка",
                description=f"Разблокировка аккаунта (блокировка от {block_type})",
                payload=f"buy_unblock_{price}",
                provider_token="",
                currency="XTR",
                prices=prices,
                start_parameter="unblock"
            )
            await query.edit_message_text(f"💳 Отправлен счет на оплату {price} XTR...")

        return await stars_menu(update, context)

    else:
        await query.edit_message_text(
            "✅ Вы не заблокированы!"
        )
        return await stars_menu(update, context)

async def view_sender_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})

    received_messages = []
    for msg_id, msg in anonymous_messages.items():
        if str(msg.get('to_user_id')) == str(user_id) and not msg.get('sender_viewed', False):
            received_messages.append((msg_id, msg))

    if not received_messages:
        await query.edit_message_text(
            "📭 У вас нет непросмотренных анонимных сообщений."
        )
        return await stars_menu(update, context)

    text = f"Выберите сообщение для просмотра отправителя ({PRICES['view_sender']} ⭐):"

    keyboard = []
    for msg_id, msg in received_messages[:10]:
        keyboard.append([InlineKeyboardButton(
            f"📨 {msg.get('timestamp', 'Неизвестно')}",
            callback_data=f"view_sender_{msg_id}"
        )])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ENTER_VIEW_SENDER_MESSAGE_ID

async def view_sender_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    msg_id = query.data.split("_")[2]

    anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})

    if msg_id not in anonymous_messages:
        await query.edit_message_text("Сообщение не найдено.")
        return await stars_menu(update, context)

    msg = anonymous_messages[msg_id]

    if str(msg.get('to_user_id')) != str(user_id):
        await query.edit_message_text("Это не ваше сообщение.")
        return await stars_menu(update, context)

    price = PRICES['view_sender']

    if user.stars_balance >= price:
        add_stars_transaction(user_id, -price, "Просмотр отправителя анонимного сообщения")

        sender = get_user(msg['from_user_id'])
        sender_name = sender.first_name if sender else "Неизвестно"

        anonymous_messages[msg_id]['sender_viewed'] = True
        save_data(ANONYMOUS_MESSAGES_FILE, anonymous_messages)

        await query.edit_message_text(
            f"👤 Отправитель: {sender_name}\n📝 Сообщение: {msg['message']}\n\nСписано {price} ⭐"
        )
    else:
        pending_payments[user_id] = msg_id

        prices = [LabeledPrice(label="Просмотр отправителя", amount=price)]

        await context.bot.send_invoice(
            chat_id=user_id,
            title="Просмотр отправителя",
            description="Узнать отправителя анонимного сообщения",
            payload=f"buy_view_sender_{price}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="view-sender"
        )
        await query.edit_message_text("💳 Отправлен счет на оплату...")

    return await stars_menu(update, context)

async def pay_view_sender_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    parts = query.data.split("_")
    if len(parts) < 4:
        await query.edit_message_text("Неверный формат данных.")
        return MAIN_MENU

    msg_id = parts[3]

    anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})

    if msg_id not in anonymous_messages:
        await query.edit_message_text("Сообщение не найдено.")
        return await view_anonymous_messages(update, context)

    msg = anonymous_messages[msg_id]

    if str(msg.get('to_user_id')) != str(user_id):
        await query.edit_message_text("Это не ваше сообщение.")
        return await view_anonymous_messages(update, context)

    if msg.get('sender_viewed', False):
        sender = get_user(msg['from_user_id'])
        sender_name = sender.first_name if sender else "Неизвестно"

        await query.edit_message_text(
            f"🕵️ **Анонимное сообщение**\n\n"
            f"📅 Дата: {msg.get('timestamp', 'Неизвестно')}\n"
            f"📝 Сообщение: {msg['message']}\n\n"
            f"👤 Отправитель: {sender_name}\n\n"
            f"✅ Вы уже оплатили просмотр",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад к списку", callback_data="view_anon_list")],
                [InlineKeyboardButton("💬 Ответить анонимно", callback_data=f"reply_anon_{msg_id}")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
        return MAIN_MENU

    price = PRICES['view_sender']

    if user.stars_balance >= price:
        add_stars_transaction(user_id, -price, "Просмотр отправителя анонимного сообщения")

        sender = get_user(msg['from_user_id'])
        sender_name = sender.first_name if sender else "Неизвестно"

        anonymous_messages[msg_id]['sender_viewed'] = True
        save_data(ANONYMOUS_MESSAGES_FILE, anonymous_messages)

        await query.edit_message_text(
            f"🕵️ **Анонимное сообщение**\n\n"
            f"📅 Дата: {msg.get('timestamp', 'Неизвестно')}\n"
            f"📝 Сообщение: {msg['message']}\n\n"
            f"👤 Отправитель: {sender_name}\n\n"
            f"✅ Оплачено {price} ⭐",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад к списку", callback_data="view_anon_list")],
                [InlineKeyboardButton("💬 Ответить анонимно", callback_data=f"reply_anon_{msg_id}")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        pending_payments[user_id] = msg_id

        prices = [LabeledPrice(label="Просмотр отправителя", amount=price)]

        await context.bot.send_invoice(
            chat_id=user_id,
            title="Просмотр отправителя",
            description="Узнать отправителя анонимного сообщения",
            payload=f"buy_view_sender_{price}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="view-sender"
        )

        await query.edit_message_text(
            f"💳 Отправлен счет на оплату {price} XTR\n\n"
            f"После оплаты вы узнаете отправителя сообщения.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад к списку", callback_data="view_anon_list")]
            ])
        )

    return MAIN_MENU


# ==================================
# ПУНКТ 11. Дополнительные хендлеры оплаты раскрытия отправителя
# с инлайн-клавиатуры под входящим анонимным сообщением.
# ==================================

async def anon_pay_virt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оплата раскрытия отправителя ВИРТУАЛЬНЫМИ звёздами бота.

    Триггерится из get_anonymous_reply_keyboard кнопкой
    callback_data=f"anon_pay_virt_{message_id}".
    """
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    # callback_data: anon_pay_virt_<msg_id>; msg_id может содержать «_» и «-»,
    # поэтому склеиваем хвост.
    parts = query.data.split("_", 3)
    if len(parts) < 4:
        try:
            await query.edit_message_text("Неверный формат данных.")
        except Exception:
            pass
        return MAIN_MENU
    msg_id = parts[3]

    anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})
    if msg_id not in anonymous_messages:
        try:
            await query.edit_message_text("Сообщение не найдено.")
        except Exception:
            pass
        return MAIN_MENU
    msg = anonymous_messages[msg_id]
    if str(msg.get('to_user_id')) != str(user_id):
        try:
            await query.edit_message_text("Это не ваше сообщение.")
        except Exception:
            pass
        return MAIN_MENU

    if msg.get('sender_viewed', False):
        sender = get_user(msg.get('from_user_id', ''))
        sender_name = sender.first_name if sender else "Неизвестно"
        try:
            await query.edit_message_text(
                f"🕵️ Анонимное сообщение\n\n"
                f"📅 {msg.get('timestamp', 'Неизвестно')}\n"
                f"📝 {msg.get('message', '')}\n\n"
                f"👤 Отправитель: {sender_name}\n"
                f"✅ Уже оплачено ранее.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Ответить анонимно",
                                          callback_data=f"reply_anon_{msg_id}")]
                ]),
            )
        except Exception:
            pass
        return MAIN_MENU

    price = PRICES.get('view_sender', 60)
    if user.stars_balance < price:
        try:
            await query.edit_message_text(
                f"Недостаточно виртуальных звёзд (нужно {price} ⭐, "
                f"у вас {user.stars_balance} ⭐).\n\n"
                f"Пополните баланс или оплатите Telegram Stars.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        f"⭐ Оплатить {price} XTR",
                        callback_data=f"anon_pay_xtr_{msg_id}",
                    )],
                    [InlineKeyboardButton("💬 Ответить анонимно",
                                          callback_data=f"reply_anon_{msg_id}")],
                ]),
            )
        except Exception:
            pass
        return MAIN_MENU

    add_stars_transaction(user_id, -price, "Просмотр отправителя анонимного сообщения")
    sender = get_user(msg.get('from_user_id', ''))
    sender_name = sender.first_name if sender else "Неизвестно"
    anonymous_messages[msg_id]['sender_viewed'] = True
    save_data(ANONYMOUS_MESSAGES_FILE, anonymous_messages)

    try:
        await query.edit_message_text(
            f"🕵️ Анонимное сообщение\n\n"
            f"📅 {msg.get('timestamp', 'Неизвестно')}\n"
            f"📝 {msg.get('message', '')}\n\n"
            f"👤 Отправитель: {sender_name}\n"
            f"✅ Списано {price} ⭐ (виртуальные)",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Ответить анонимно",
                                      callback_data=f"reply_anon_{msg_id}")]
            ]),
        )
    except Exception:
        pass
    return MAIN_MENU


async def anon_pay_xtr_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оплата раскрытия отправителя через Telegram Stars (XTR).

    Триггерится из get_anonymous_reply_keyboard кнопкой
    callback_data=f"anon_pay_xtr_{message_id}". Всегда отправляет инвойс.
    """
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    parts = query.data.split("_", 3)
    if len(parts) < 4:
        try:
            await query.edit_message_text("Неверный формат данных.")
        except Exception:
            pass
        return MAIN_MENU
    msg_id = parts[3]

    anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})
    if msg_id not in anonymous_messages:
        try:
            await query.edit_message_text("Сообщение не найдено.")
        except Exception:
            pass
        return MAIN_MENU
    msg = anonymous_messages[msg_id]
    if str(msg.get('to_user_id')) != str(user_id):
        try:
            await query.edit_message_text("Это не ваше сообщение.")
        except Exception:
            pass
        return MAIN_MENU

    if msg.get('sender_viewed', False):
        sender = get_user(msg.get('from_user_id', ''))
        sender_name = sender.first_name if sender else "Неизвестно"
        try:
            await query.edit_message_text(
                f"🕵️ Анонимное сообщение\n\n"
                f"📅 {msg.get('timestamp', 'Неизвестно')}\n"
                f"📝 {msg.get('message', '')}\n\n"
                f"👤 Отправитель: {sender_name}\n"
                f"✅ Уже оплачено ранее.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Ответить анонимно",
                                          callback_data=f"reply_anon_{msg_id}")]
                ]),
            )
        except Exception:
            pass
        return MAIN_MENU

    price = PRICES.get('view_sender', 60)
    pending_payments[user_id] = msg_id
    try:
        await context.bot.send_invoice(
            chat_id=user_id,
            title="Просмотр отправителя",
            description="Узнать отправителя анонимного сообщения",
            payload=f"buy_view_sender_{price}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Просмотр отправителя", amount=price)],
            start_parameter="view-sender",
        )
    except Exception as e:
        logger.error(f"Не удалось отправить инвойс XTR на просмотр отправителя: {e}")
        try:
            await query.edit_message_text(
                "❌ Не удалось отправить счёт. Попробуйте позже."
            )
        except Exception:
            pass
        return MAIN_MENU

    try:
        await query.edit_message_text(
            f"💳 Отправлен счёт на {price} XTR. Оплатите его в Telegram, "
            f"и отправитель будет раскрыт автоматически.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Ответить анонимно",
                                      callback_data=f"reply_anon_{msg_id}")]
            ]),
        )
    except Exception:
        pass
    return MAIN_MENU


# ==================================
# === АНОНИМКИ: ОЧИСТКА, УДАЛЕНИЕ, ПОКУПКА «МЕСТА» ===
# ==================================
# Всё ниже — вспомогательные функции и хендлеры, которые управляют
# хранением входящих анонимных сообщений у конкретного пользователя.

def _parse_anon_timestamp(ts_str):
    """Парсит поле msg['timestamp'] (формат "YYYY-MM-DD HH:MM").

    Возвращает datetime в UTC (naive — без tzinfo, как и весь файл).
    Если строка не парсится — возвращает None, и вызывающий код считает
    сообщение «не подлежащим автоочистке» (на всякий случай).
    """
    if not ts_str:
        return None
    try:
        return datetime.strptime(str(ts_str), "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


def _parse_user_anon_keep_until(value):
    """Парсит User.anon_keep_until — "YYYY-MM-DD HH:MM:SS" → datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _user_has_anon_storage(user):
    """Возвращает True, если у пользователя сейчас активно платное «место»."""
    if not user:
        return False
    keep = _parse_user_anon_keep_until(getattr(user, 'anon_keep_until', None))
    if not keep:
        return False
    return keep > datetime.utcnow()


def _format_anon_keep_until(user):
    """Человекочитаемый текст «оплачено до …» для UI настроек."""
    keep = _parse_user_anon_keep_until(getattr(user, 'anon_keep_until', None))
    if not keep or keep <= datetime.utcnow():
        return "—"
    return keep.strftime("%Y-%m-%d %H:%M UTC")


def _extend_user_anon_keep(user, months=1):
    """Продлевает у пользователя оплаченное «место» на N месяцев.

    Считаем месяц как 30 дней. Если у пользователя уже была активная
    подписка, новый срок прибавляется к существующему (а не от
    «сейчас»), чтобы пользователь не терял оплаченные дни.
    """
    now = datetime.utcnow()
    keep = _parse_user_anon_keep_until(getattr(user, 'anon_keep_until', None))
    base = keep if keep and keep > now else now
    new_until = base + timedelta(days=30 * months)
    user.anon_keep_until = new_until.strftime("%Y-%m-%d %H:%M:%S")
    users = load_users()
    users[str(user.user_id)] = user
    save_users(users)
    return new_until


async def anon_clear_all_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает подтверждение «удалить все мои входящие анонимки?»."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    try:
        await query.edit_message_text(
            "🗑️ Вы уверены, что хотите удалить ВСЕ свои входящие "
            "анонимные сообщения?\n\n"
            "⚠️ После этого вы не сможете прочесть их и узнать, кто "
            "что писал (даже за звёзды).",
            reply_markup=get_anon_clear_confirm_keyboard(),
        )
    except Exception as e:
        logger.error(f"anon_clear_all_start edit: {e}")
    return MAIN_MENU


async def anon_clear_all_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Фактически удаляет все входящие анонимные сообщения пользователя."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user_id = str(query.from_user.id)
    anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})

    deleted = 0
    new_messages = {}
    for msg_id, msg in anonymous_messages.items():
        if str(msg.get('to_user_id')) == str(user_id):
            deleted += 1
            continue
        new_messages[msg_id] = msg

    save_data(ANONYMOUS_MESSAGES_FILE, new_messages)
    # Чистим pending_payments, если они ссылались на удалённые сообщения,
    # чтобы пользователь не оплатил «просмотр отправителя» уже
    # несуществующей анонимки.
    pending_payments.pop(user_id, None)

    try:
        await query.edit_message_text(
            f"✅ Удалено сообщений: {deleted}.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ В главное меню", callback_data="back_to_main")],
            ]),
        )
    except Exception as e:
        logger.error(f"anon_clear_all_confirm edit: {e}")
    return MAIN_MENU


async def anon_delete_mode_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает экран в режим «удалить по одному»."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user_id = str(query.from_user.id)
    keyboard = get_anon_delete_mode_keyboard(user_id)
    if not keyboard:
        try:
            await query.edit_message_text(
                "📭 У вас нет анонимных сообщений для удаления.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")],
                ]),
            )
        except Exception:
            pass
        return MAIN_MENU

    try:
        await query.edit_message_text(
            "🗂 Выберите сообщение, которое хотите удалить:",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"anon_delete_mode_start edit: {e}")
    return MAIN_MENU


async def anon_delete_one_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет одно входящее анонимное сообщение по callback `anon_del_<msg_id>`."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user_id = str(query.from_user.id)
    raw = query.data or ""
    msg_id = raw[len("anon_del_"):] if raw.startswith("anon_del_") else ""

    anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})
    msg = anonymous_messages.get(msg_id)
    if not msg or str(msg.get('to_user_id')) != str(user_id):
        # Сообщение уже удалено / чужое — просто возвращаемся к списку.
        return await view_anonymous_messages(update, context)

    anonymous_messages.pop(msg_id, None)
    save_data(ANONYMOUS_MESSAGES_FILE, anonymous_messages)
    if pending_payments.get(user_id) == msg_id:
        pending_payments.pop(user_id, None)

    # Сразу показываем актуальный режим «удалить по одному». Если больше
    # сообщений не осталось — `get_anon_delete_mode_keyboard` вернёт None
    # и пользователь увидит «📭 У вас нет анонимных сообщений…».
    keyboard = get_anon_delete_mode_keyboard(user_id)
    if not keyboard:
        try:
            await query.edit_message_text(
                "✅ Сообщение удалено.\n\n📭 У вас больше нет анонимных сообщений.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ В главное меню", callback_data="back_to_main")],
                ]),
            )
        except Exception:
            pass
        return MAIN_MENU

    try:
        await query.edit_message_text(
            "✅ Сообщение удалено.\n\n🗂 Выберите следующее или вернитесь к списку:",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error(f"anon_delete_one_handler edit: {e}")
    return MAIN_MENU


async def anon_buy_space_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает экран покупки «места» на 1 месяц."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    price = PRICES.get('anon_keep_month', 100)
    keep_until = _format_anon_keep_until(user)

    text = (
        "💎 *Место под анонимные сообщения*\n\n"
        f"Каждое анонимное сообщение, которое вам пришло, по умолчанию хранится "
        f"*{ANONYMOUS_TTL_DAYS} дней* (≈ 2 месяца), а потом автоматически удаляется, "
        "чтобы не занимать место на хостинге.\n\n"
        f"Если вы оплатите *{price} ⭐ за месяц*, в течение этого месяца авто-очистка "
        "не тронет ни одно ваше входящее анонимное сообщение.\n\n"
        f"📅 Сейчас оплачено до: *{keep_until}*"
    )

    try:
        await query.edit_message_text(
            text,
            reply_markup=get_anon_buy_space_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"anon_buy_space_start edit: {e}")
    return MAIN_MENU


async def anon_buy_space_virt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Списывает виртуальные ⭐ и продлевает место на 1 месяц."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    price = PRICES.get('anon_keep_month', 100)

    if getattr(user, 'stars_balance', 0) < price:
        try:
            await query.edit_message_text(
                f"❌ Недостаточно виртуальных звёзд (нужно {price} ⭐, "
                f"у вас {getattr(user, 'stars_balance', 0)} ⭐).\n\n"
                f"Пополните баланс или оплатите Telegram Stars.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        f"⭐ Оплатить {price} XTR",
                        callback_data="anon_buy_space_xtr",
                    )],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="view_anon_list")],
                ]),
            )
        except Exception:
            pass
        return MAIN_MENU

    add_stars_transaction(user_id, -price, "Покупка места под анонимки (1 мес)")
    # add_stars_transaction уже сохранил user, но баланс в локальном объекте
    # устарел — перечитываем, иначе _extend_user_anon_keep сохранит User
    # со старым stars_balance.
    user = get_user(user_id) or user
    new_until = _extend_user_anon_keep(user, months=1)

    try:
        await query.edit_message_text(
            "✅ Готово! Место под ваши анонимные сообщения оплачено "
            f"до *{new_until.strftime('%Y-%m-%d %H:%M UTC')}*.\n\n"
            "В течение этого срока авто-очистка их не тронет.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ К моим анонимкам", callback_data="view_anon_list")],
                [InlineKeyboardButton("⬅️ В главное меню", callback_data="back_to_main")],
            ]),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"anon_buy_space_virt edit: {e}")
    return MAIN_MENU


async def anon_buy_space_xtr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет XTR-инвойс на 1 месяц «места»."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user_id = str(query.from_user.id)
    price = PRICES.get('anon_keep_month', 100)

    try:
        await context.bot.send_invoice(
            chat_id=int(user_id),
            title="Место под анонимки (1 мес)",
            description=(
                f"Месяц защиты от авто-очистки ваших входящих анонимных "
                f"сообщений. Стоимость: {price} XTR."
            ),
            payload=f"buy_anon_space_{price}",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Место под анонимки (1 мес)", amount=price)],
            start_parameter="anon-space",
        )
    except Exception as e:
        logger.error(f"anon_buy_space_xtr send_invoice: {e}")
        try:
            await query.edit_message_text(
                "❌ Не удалось отправить счёт. Попробуйте позже.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Назад", callback_data="view_anon_list")],
                ]),
            )
        except Exception:
            pass
        return MAIN_MENU

    try:
        await query.edit_message_text(
            f"💳 Отправлен счёт на {price} XTR. После оплаты ваше место "
            "под анонимки автоматически продлится на 1 месяц.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="view_anon_list")],
            ]),
        )
    except Exception:
        pass
    return MAIN_MENU


async def toggle_anon_purge_notify_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Тоггл уведомления об авто-удалении анонимок (в настройках уведомлений)."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    user.anon_purge_notify = not bool(getattr(user, 'anon_purge_notify', True))
    users = load_users()
    users[str(user_id)] = user
    save_users(users)

    # Возвращаем пользователя в экран «настройки уведомлений», чтобы он
    # увидел новое состояние тоггла. notification_settings_start сам
    # перерисует клавиатуру.
    return await notification_settings_start(update, context)


# === ФОНОВЫЙ ДЖОБ АВТО-ОЧИСТКИ ===

async def anonymous_purge_job(context: ContextTypes.DEFAULT_TYPE):
    """Раз в сутки чистит анонимки и шлёт предупреждения за день до удаления.

    Алгоритм:
    1. Перечитываем все анонимные сообщения и пользователей.
    2. Для каждого сообщения вычисляем срок удаления = timestamp +
       ANONYMOUS_TTL_DAYS.
    3. Если получатель оплатил «место» (anon_keep_until > now), сообщение
       не трогаем.
    4. Если срок уже прошёл — сообщение удаляется.
    5. Если до срока осталось <= ANONYMOUS_PURGE_NOTIFY_DAYS_BEFORE дней
       и пользователь не выключил `anon_purge_notify`, и мы ещё не слали
       ему предупреждение по этому сообщению — шлём.

    Чтобы не дублировать предупреждения при каждом запуске, в сам объект
    сообщения добавляется флаг `purge_notified=True` — он сохраняется в
    anonymous_messages.json.
    """
    try:
        anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})
        users = load_users()
        now = datetime.utcnow()

        to_delete = []
        # Группируем сообщения с приближающимся удалением по получателю,
        # чтобы прислать ему одно сводное уведомление, а не по штуке на
        # каждое сообщение.
        warn_by_user: dict[str, list] = {}
        modified = False

        for msg_id, msg in list(anonymous_messages.items()):
            if not isinstance(msg, dict):
                continue
            created = _parse_anon_timestamp(msg.get('timestamp'))
            if created is None:
                continue
            to_uid = str(msg.get('to_user_id') or "")
            recipient = users.get(to_uid)

            expires_at = created + timedelta(days=ANONYMOUS_TTL_DAYS)

            # Платное «место» защищает от удаления, но только пока активно.
            if _user_has_anon_storage(recipient):
                continue

            if now >= expires_at:
                to_delete.append(msg_id)
                continue

            days_left = (expires_at - now).total_seconds() / 86400
            if (
                days_left <= ANONYMOUS_PURGE_NOTIFY_DAYS_BEFORE
                and not msg.get('purge_notified', False)
                and recipient is not None
                and getattr(recipient, 'anon_purge_notify', True)
            ):
                warn_by_user.setdefault(to_uid, []).append((msg_id, expires_at))
                msg['purge_notified'] = True
                modified = True

        # Удаляем сообщения с истёкшим сроком.
        if to_delete:
            for msg_id in to_delete:
                anonymous_messages.pop(msg_id, None)
            modified = True

        if modified:
            save_data(ANONYMOUS_MESSAGES_FILE, anonymous_messages)

        # Рассылаем уведомления получателям, которым осталось ≤1 дня.
        price = PRICES.get('anon_keep_month', 100)
        for to_uid, items in warn_by_user.items():
            # Берём ближайший срок — именно его упомянем в уведомлении.
            soonest = min(exp for _, exp in items)
            text = (
                "⚠️ *Авто-очистка анонимок*\n\n"
                f"Завтра, около *{soonest.strftime('%H:%M UTC %Y-%m-%d')}*, "
                f"бот удалит {len(items)} ваших анонимных "
                f"сообщений — им исполнится {ANONYMOUS_TTL_DAYS} дней.\n\n"
                "Если они вам нужны:\n"
                f"• купите «место» за *{price}⭐ / месяц* — ничего не удалится;\n"
                "• или зайдите в «📨 Мои анонимные сообщения» и сохраните "
                "нужное (узнайте отправителя / ответьте).\n\n"
                "Это уведомление можно выключить в "
                "⚙️ Настройки → 🔔 Настройки уведомлений."
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"💎 Купить место ({price}⭐ / мес)",
                    callback_data="anon_buy_space",
                )],
                [InlineKeyboardButton(
                    "📨 Мои анонимные сообщения",
                    callback_data="view_anon_list",
                )],
                [InlineKeyboardButton(
                    "🔕 Выключить это уведомление",
                    callback_data="toggle_anon_purge_notify",
                )],
            ])
            try:
                await context.bot.send_message(
                    chat_id=int(to_uid),
                    text=text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.error(f"anonymous_purge_job notify {to_uid}: {e}")

    except Exception as e:
        logger.error(f"anonymous_purge_job: {e}")


async def stars_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    stats = load_stars_stats()

    # НОВОЕ (по требованию пользователя): подробной истории переводов больше
    # нет и не будет. Показываем только:
    #   1) общее число пользователей бота;
    #   2) суммарно потраченные Stars;
    #   3) топ пользователей по тратам.
    total_users = len(load_users())
    total_spent = int(stats.get('total_stars_spent', 0))
    spenders = int(stats.get('spenders_count', 0) or 0)

    text = (
        "📊 **Статистика звезд**\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"💸 Суммарно потрачено: {total_spent} ⭐\n"
        f"🧾 Покупателей: {spenders}\n\n"
        "🏆 **Топ по тратам:**\n"
    )

    top = stats.get('top_donors', []) or []
    if top:
        for i, donor in enumerate(top[:10], 1):
            text += f"{i}. {donor.get('user_name', 'Пользователь')} — {int(donor.get('total_spent', 0))} ⭐\n"
    else:
        text += "Пока никто ничего не тратил."

    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_stars_keyboard(user))
    return MAIN_MENU

# ==================================
# === РАСПИСАНИЕ ПО ДНЯМ ===
# ==================================

def _build_week_schedule_keyboard(active_day_index=None):
    """Клавиатура со всеми днями недели. Активный день помечается галочкой."""
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    keyboard = []
    row = []
    for i, day in enumerate(days):
        label = f"✅ {day}" if i == active_day_index else day
        row.append(InlineKeyboardButton(label, callback_data=f"schedule_day_{i}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬅️ В главное меню", callback_data="back_to_main")])
    return InlineKeyboardMarkup(keyboard)


async def _send_or_edit(query, context, user_id, text, markup):
    """Универсальный вывод: пробуем отредактировать сообщение, иначе шлём новое.
    Никогда не падает — все ошибки гасятся, чтобы бот продолжал отвечать."""
    # 1) edit_message_text
    try:
        await query.edit_message_text(text, reply_markup=markup)
        return
    except TGBadRequest as e:
        # «message is not modified» — пользователь нажал тот же день: это не ошибка,
        # просто ничего не делаем.
        if "not modified" in str(e).lower():
            return
        logger.warning(f"week_schedule edit failed (BadRequest): {e}")
    except Exception as e:
        logger.warning(f"week_schedule edit failed: {e}")

    # 2) фолбэк: отправить новое сообщение
    try:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=markup)
    except Exception as e:
        logger.error(f"week_schedule fallback send_message failed: {e}")


@timeout(CONVERSATION_TIMEOUT)
async def handle_week_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр расписания по дням (полностью переписан).

    Цели:
    - Каждое нажатие сразу гасим query.answer(), чтобы кнопка не «зависала».
    - Любая ошибка ловится: бот всегда возвращает корректное состояние и
      остаётся отзывчивым, без необходимости /start.
    - При повторном нажатии того же дня молча игнорируем «message is not modified».
    - При неизвестном callback — остаёмся в WEEK_SCHEDULE, чтобы кнопки продолжали работать.
    """
    query = update.callback_query
    if query is None:
        return WEEK_SCHEDULE

    # Сразу отвечаем на callback — Telegram перестаёт показывать «крутилку».
    try:
        await query.answer()
    except Exception:
        pass

    user_id = str(query.from_user.id)
    user = get_user(user_id) or User(user_id)
    data = query.data or ""

    # Возврат в главное меню.
    if data == "back_to_main":
        try:
            await show_main_menu(update, context, user)
        except Exception as e:
            logger.error(f"week_schedule back_to_main: {e}")
        return MAIN_MENU

    # Снова показать список всех дней (без выделения).
    if data == "week_schedule_list":
        await _send_or_edit(
            query, context, user_id,
            "📅 Выберите день недели:",
            _build_week_schedule_keyboard(active_day_index=None),
        )
        return WEEK_SCHEDULE

    # Показ расписания на выбранный день.
    if data.startswith("schedule_day_"):
        # Парсим индекс дня максимально устойчиво.
        day_index = 0
        try:
            parts = data.split("_")
            day_index = int(parts[2]) if len(parts) >= 3 else 0
        except (ValueError, IndexError):
            day_index = 0
        if day_index < 0 or day_index > 6:
            day_index = 0

        day_name = get_day_name(day_index)
        class_obj = get_class_by_user(user_id)

        if not class_obj:
            text = (
                "Вы не состоите в классе.\n"
                "Создайте или вступите в класс через «🎓 Управление классами»."
            )
        elif is_user_class_blocked(user_id, class_obj.class_code):
            text = "Вы заблокированы в этом классе."
        else:
            schedule = get_day_schedule(class_obj, day_name)
            text = f"📅 Расписание на {day_name}:\n\n{schedule}"

        await _send_or_edit(
            query, context, user_id,
            text,
            _build_week_schedule_keyboard(active_day_index=day_index),
        )
        return WEEK_SCHEDULE

    # Неизвестный callback — остаёмся в состоянии WEEK_SCHEDULE.
    return WEEK_SCHEDULE

# ==================================
# === АДМИНСКАЯ ПАНЕЛЬ ===
# ==================================

@timeout(CONVERSATION_TIMEOUT)
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    if not is_user_class_admin(user_id):
        error_text = "❌ У вас нет прав доступа к админке."
        await update.message.reply_text(error_text)
        return MAIN_MENU

    user_classes = []
    classes = load_classes()
    for class_obj in classes.values():
        if class_obj.is_active and user_id in class_obj.admins:
            user_classes.append(class_obj)

    if not user_classes:
        error_text = "❌ У вас нет прав доступа."
        await update.message.reply_text(error_text)
        return MAIN_MENU

    if len(user_classes) == 1:
        class_obj = user_classes[0]
        context.user_data['current_admin_class'] = class_obj.class_code

        text = f"👨‍💼 Админская панель класса '{class_obj.class_name}'\n\nВыберите действие:"

        await update.message.reply_text(text, reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL
    else:
        class_obj = user_classes[0]
        context.user_data['current_admin_class'] = class_obj.class_code
        text = f"👨‍💼 Админская панель класса '{class_obj.class_name}'\n\nВыберите действие:"
        await update.message.reply_text(text, reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

@timeout(CONVERSATION_TIMEOUT)
async def send_class_message_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    text = "📢 Введите сообщение для отправки всему классу:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return SEND_CLASS_MESSAGE

@timeout(CONVERSATION_TIMEOUT)
async def send_class_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    message_text = update.message.text
    class_code = context.user_data.get('current_admin_class')

    if not class_code:
        error_text = "Класс не выбран."
        await update.message.reply_text(error_text)
        return await admin_panel(update, context)

    class_obj = get_class_by_code(class_code)
    if not class_obj:
        error_text = "Класс не найден."
        await update.message.reply_text(error_text)
        return await admin_panel(update, context)

    rejected = await reject_if_forbidden_chars(update, message_text, SEND_CLASS_MESSAGE)
    if rejected is not None:
        return rejected

    members = [m for m in (class_obj.students + class_obj.admins) if m not in class_obj.blocked_users]
    sent_count = 0

    for member_id in members:
        if member_id != user_id:
            try:
                await context.bot.send_message(
                    chat_id=member_id,
                    text=f"📢 **Сообщение от администратора класса {class_obj.class_name}:**\n\n{message_text}",
                    parse_mode="Markdown"
                )
                sent_count += 1
            except Exception as e:
                logger.error(f"Ошибка при отправке сообщения {member_id}: {e}")

    success_text = f"✅ Сообщение отправлено {sent_count} участникам класса!"

    await update.message.reply_text(success_text)
    return await admin_panel(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def edit_schedule_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    text = "📅 Выберите день недели для редактирования:"

    await query.edit_message_text(text, reply_markup=get_schedule_edit_keyboard())
    return EDIT_SCHEDULE

@timeout(CONVERSATION_TIMEOUT)
async def edit_schedule_day_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    try:
        day_index = int(query.data.split("_")[3])
    except (ValueError, IndexError):
        await query.answer("Некорректный день", show_alert=True)
        return EDIT_SCHEDULE
    if day_index < 0 or day_index > 6:
        await query.answer("Некорректный день", show_alert=True)
        return EDIT_SCHEDULE
    day_name = get_day_name(day_index)

    class_code = context.user_data.get('current_admin_class')
    if not class_code:
        # Если current_admin_class сброшен, возвращаемся в админку.
        try:
            await query.edit_message_text("Класс не выбран. Откройте Админ-панель заново.")
        except Exception:
            pass
        return ADMIN_PANEL
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        try:
            await query.edit_message_text("Класс не найден.")
        except Exception:
            pass
        return ADMIN_PANEL

    current_schedule = class_obj.schedule.get(day_name, "") or "(пусто)"

    context.user_data['editing_schedule_day'] = day_name

    text = (
        f"📅 Редактирование расписания на {day_name}\n\n"
        f"Текущее расписание:\n{current_schedule}\n\n"
        f"Введите новое расписание (одним сообщением):"
    )

    # Кнопка «Отмена» возвращает к выбору дня, а не в главное меню — удобнее редактировать.
    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ К выбору дня", callback_data="edit_schedule")],
        [InlineKeyboardButton("❌ Отмена", callback_data="back_to_admin")],
    ])
    await query.edit_message_text(text, reply_markup=cancel_kb)
    return EDIT_SCHEDULE_CONTENT

@timeout(CONVERSATION_TIMEOUT)
async def edit_schedule_content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    new_schedule = (update.message.text or "").strip()
    day_name = context.user_data.get('editing_schedule_day')
    class_code = context.user_data.get('current_admin_class')

    if not class_code or not day_name:
        error_text = "Ошибка: данные для сохранения расписания не найдены. Откройте «📅 Редактировать расписание» заново."
        await update.message.reply_text(error_text)
        return await admin_panel(update, context)

    if not new_schedule:
        await update.message.reply_text(
            "❌ Расписание не может быть пустым. Введите текст или нажмите «Отмена»."
        )
        return EDIT_SCHEDULE_CONTENT

    rejected = await reject_if_forbidden_chars(update, new_schedule, EDIT_SCHEDULE_CONTENT)
    if rejected is not None:
        return rejected

    class_obj = get_class_by_code(class_code)
    if class_obj:
        if not isinstance(getattr(class_obj, 'schedule', None), dict):
            class_obj.schedule = {}
        class_obj.schedule[day_name] = new_schedule

        # Берём актуальный словарь классов и кладём туда обновлённый объект.
        classes = load_classes()
        classes[class_code] = class_obj
        ok = save_classes(classes)

        if ok:
            success_text = (
                f"✅ Расписание на {day_name} успешно обновлено!\n\n"
                f"Новое расписание:\n{new_schedule}"
            )
        else:
            success_text = "❌ Не удалось сохранить расписание (ошибка записи файла). Попробуйте ещё раз."
    else:
        success_text = "Класс не найден."

    # После сохранения — возвращаем к выбору дня, чтобы можно было сразу редактировать другой день.
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Редактировать другой день", callback_data="edit_schedule")],
        [InlineKeyboardButton("⬅️ В админ-панель", callback_data="back_to_admin")],
    ])
    await update.message.reply_text(success_text, reply_markup=keyboard)
    context.user_data.pop('editing_schedule_day', None)
    # Остаёмся в EDIT_SCHEDULE_CONTENT, чтобы CallbackQueryHandler в этом стейте мог
    # обработать клики по «Редактировать другой день» / «В админ-панель».
    return EDIT_SCHEDULE_CONTENT


# ==================================
# === НАСТРОЙКИ ===
# ==================================

@timeout(CONVERSATION_TIMEOUT)
async def user_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    text = f"⚙️ Настройки\n\n👤 Имя: {user.first_name}\n🌍 Часовой пояс: UTC{_fmt_tz_offset(user.timezone)}\n🔔 Уведомления: {'Включены' if user.notifications else 'Выключены'}\n🎂 День рождения: {user.birthday or 'Не установлен'}\n\nВыберите действие:"

    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, reply_markup=get_settings_keyboard(user))
    else:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=get_settings_keyboard(user))
        except Exception:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=get_settings_keyboard(user)
            )
    return USER_SETTINGS

@timeout(CONVERSATION_TIMEOUT)
async def change_language_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "🌍 Выберите язык:"

    await query.edit_message_text(text, reply_markup=get_language_keyboard())
    return CHANGE_LANGUAGE

@timeout(CONVERSATION_TIMEOUT)
async def change_language_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    lang_code = query.data.split("_")[1]

    user.language = lang_code
    save_user(user)

    lang_names = {'ru': 'Русский', 'en': 'English'}
    lang_name = lang_names.get(lang_code, lang_code)

    await query.edit_message_text(f"✅ Язык изменен на {lang_name}!")
    return await user_settings(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def change_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "⏰ Введите текущее местное время в формате ЧЧ:ММ (например, 14:30):"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return CHANGE_TIME

@timeout(CONVERSATION_TIMEOUT)
async def change_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    time_str = update.message.text.strip()

    try:
        user_time_dt = datetime.strptime(time_str, "%H:%M")
        timezone = calculate_timezone(time_str)
        user.timezone = timezone
        user.local_time_set = True
        save_user(user)

        # ПУНКТ 6: пересчитать утренние/вечерние уведомления при смене часового пояса,
        # а также пересчитать джобы погоды/праздников/дня рождения.
        try:
            schedule_user_daily_jobs(context.application, user)
        except Exception as e:
            logger.error(f"Не удалось перепланировать daily jobs: {e}")
        try:
            schedule_user_weather_job(context.application, user)
        except Exception as e:
            logger.error(f"Не удалось перепланировать weather job: {e}")
        try:
            schedule_user_holiday_job(context.application, user)
        except Exception as e:
            logger.error(f"Не удалось перепланировать holiday job: {e}")
        try:
            schedule_user_birthday_job(context.application, user)
        except Exception as e:
            logger.error(f"Не удалось перепланировать birthday job: {e}")

        success_text = f"✅ Часовой пояс обновлен!\n⏰ Ваше время: {user_time_dt.strftime('%H:%M')}\n🌍 Часовой пояс: UTC{timezone:+d}"

        await update.message.reply_text(success_text)
        return await user_settings(update, context)

    except ValueError:
        await update.message.reply_text("Введите время в формате ЧЧ:ММ (например, 08:30):")
        return CHANGE_TIME

@timeout(CONVERSATION_TIMEOUT)
async def change_buttons_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    text = "✏️ **Изменение названий кнопок**\n\nВыберите кнопку для переименования:"

    keyboard = []
    # ПУНКТ 4: показываем ВСЕ кнопки (стандартные + личные/глобальные/классные)
    for btn_name in get_all_user_button_names(user):
        cb_name = btn_name[:52]
        keyboard.append([InlineKeyboardButton(btn_name, callback_data=f"select_btn_{cb_name}")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_settings")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return CHANGE_BUTTON_NAME

@timeout(CONVERSATION_TIMEOUT)
async def select_button_to_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    button_name = query.data.replace("select_btn_", "")
    context.user_data['renaming_button'] = button_name

    text = f"✏️ Введите новое название для кнопки '{button_name}':"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return CHANGE_BUTTON_NAME

@timeout(CONVERSATION_TIMEOUT)
async def rename_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    new_name = update.message.text.strip()
    old_name = context.user_data.get('renaming_button')

    if not new_name:
        await update.message.reply_text("Введите название:")
        return CHANGE_BUTTON_NAME

    rejected = await reject_if_forbidden_chars(update, new_name, CHANGE_BUTTON_NAME)
    if rejected is not None:
        return rejected

    if not hasattr(user, 'custom_buttons'):
        user.custom_buttons = {}

    user.custom_buttons[old_name] = new_name
    save_user(user)

    await update.message.reply_text(
        f"✅ Кнопка переименована!\n'{old_name}' → '{new_name}'",
        reply_markup=get_main_menu_keyboard(user)
    )

    context.user_data.pop('renaming_button', None)
    return await user_settings(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def change_layout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "🔄 **Изменение расположения кнопок**\n\nВыберите схему расположения:"

    keyboard = [
        [InlineKeyboardButton("📋 Стандартная", callback_data="layout_default")],
        [InlineKeyboardButton("📱 Компактная", callback_data="layout_compact")],
        [InlineKeyboardButton("🖥️ Широкая", callback_data="layout_wide")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_settings")]
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return CHANGE_BUTTON_LAYOUT

@timeout(CONVERSATION_TIMEOUT)
async def change_layout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    layout = query.data.replace("layout_", "")

    user.button_layout = layout
    save_user(user)

    layout_names = {'default': 'Стандартная', 'compact': 'Компактная', 'wide': 'Широкая'}

    await query.edit_message_text(f"✅ Схема расположения изменена на '{layout_names.get(layout, layout)}'!")
    await context.bot.send_message(
        chat_id=user_id,
        text="⚙️ Меню обновлено!",
        reply_markup=get_main_menu_keyboard(user)
    )
    return await user_settings(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def reorder_buttons_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    text = "📋 Изменение порядка кнопок\n\nВыберите кнопку для перемещения:"

    keyboard = []
    for btn_name in get_all_user_button_names(user):
        cb_data = _safe_cb("reorder_btn_", btn_name)
        keyboard.append([InlineKeyboardButton(btn_name, callback_data=cb_data)])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_settings")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return REORDER_BUTTONS

@timeout(CONVERSATION_TIMEOUT)
async def reorder_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    cb_fragment = query.data.replace("reorder_btn_", "")

    all_btns = get_all_user_button_names(user)
    button_name = cb_fragment
    for name in all_btns:
        if _safe_cb("reorder_btn_", name).replace("reorder_btn_", "") == cb_fragment:
            button_name = name
            break

    if not user.custom_button_order:
        user.custom_button_order = all_btns.copy()

    if button_name not in user.custom_button_order:
        user.custom_button_order.append(button_name)
    save_user(user)

    current_index = user.custom_button_order.index(button_name)

    text = f"📋 Перемещение кнопки '{button_name}'\n\nТекущая позиция: {current_index + 1}/{len(user.custom_button_order)}"

    keyboard = []
    if current_index > 0:
        keyboard.append([InlineKeyboardButton("⬆️ Вверх", callback_data=_safe_cb("move_up_", button_name))])
    if current_index < len(user.custom_button_order) - 1:
        keyboard.append([InlineKeyboardButton("⬇️ Вниз", callback_data=_safe_cb("move_down_", button_name))])

    keyboard.append([InlineKeyboardButton("✅ Готово", callback_data="finish_reorder_settings")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="reorder_buttons")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return REORDER_BUTTONS

@timeout(CONVERSATION_TIMEOUT)
async def move_button_up_down(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    parts = query.data.split("_")
    direction = parts[1]
    cb_fragment = "_".join(parts[2:])

    all_btns = get_all_user_button_names(user)
    button_name = cb_fragment
    prefix = f"move_{direction}_"
    for name in all_btns:
        if _safe_cb(prefix, name).replace(prefix, "") == cb_fragment:
            button_name = name
            break

    if not user.custom_button_order:
        user.custom_button_order = all_btns.copy()

    if button_name in user.custom_button_order:
        current_index = user.custom_button_order.index(button_name)

        if direction == "up" and current_index > 0:
            user.custom_button_order[current_index], user.custom_button_order[current_index - 1] =                 user.custom_button_order[current_index - 1], user.custom_button_order[current_index]
        elif direction == "down" and current_index < len(user.custom_button_order) - 1:
            user.custom_button_order[current_index], user.custom_button_order[current_index + 1] =                 user.custom_button_order[current_index + 1], user.custom_button_order[current_index]

        save_user(user)

    return await reorder_button_handler(update, context)

async def finish_reorder_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)

    await query.edit_message_text("✅ Порядок кнопок сохранен!")
    if user:
        await context.bot.send_message(
            chat_id=user_id,
            text="⚙️ Меню обновлено!",
            reply_markup=get_main_menu_keyboard(user)
        )
    return await user_settings(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def move_buttons_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    text = "🔄 Перемещение кнопок\n\nВыберите кнопку для перемещения:"

    keyboard = []
    for btn_name in get_all_user_button_names(user):
        cb_data = _safe_cb("move_btn_", btn_name)
        keyboard.append([InlineKeyboardButton(btn_name, callback_data=cb_data)])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_settings")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return MOVE_BUTTONS

@timeout(CONVERSATION_TIMEOUT)
async def move_buttons_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    cb_fragment = query.data.replace("move_btn_", "")
    all_names = get_all_user_button_names(user)
    button_name = cb_fragment
    for name in all_names:
        if _safe_cb("move_btn_", name).replace("move_btn_", "") == cb_fragment:
            button_name = name
            break

    context.user_data['moving_button'] = button_name

    text = f"🔄 Перемещение кнопки '{button_name}'\n\nВыберите новую позицию:"

    keyboard = []
    for i, btn in enumerate(all_names):
        if btn != button_name:
            cb_data = _safe_cb("move_to_", f"{i}_{btn}")
            keyboard.append([InlineKeyboardButton(f"📍 Перед '{btn}'", callback_data=cb_data)])

    keyboard.append([InlineKeyboardButton("📍 В конец", callback_data="move_to_end")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="move_buttons")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return MOVE_BUTTONS

@timeout(CONVERSATION_TIMEOUT)
async def move_to_position_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    moving_button = context.user_data.get('moving_button')
    if not moving_button:
        await query.edit_message_text("Кнопка не выбрана.")
        return await user_settings(update, context)

    if not user.custom_button_order:
        user.custom_button_order = get_all_user_button_names(user)

    if moving_button in user.custom_button_order:
        user.custom_button_order.remove(moving_button)

    if query.data == "move_to_end":
        user.custom_button_order.append(moving_button)
    else:
        parts = query.data.split("_")
        target_index = int(parts[2])
        user.custom_button_order.insert(target_index, moving_button)

    save_user(user)

    await query.edit_message_text(f"✅ Кнопка '{moving_button}' перемещена!")

    # Автоматически обновляем клавиатуру снизу — без повторного /start.
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="⚙️ Меню обновлено!",
            reply_markup=get_main_menu_keyboard(user),
        )
    except Exception as e:
        logger.error(f"move_to_position_handler: обновление клавиатуры: {e}")

    context.user_data.pop('moving_button', None)
    return await user_settings(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def birthday_settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    text = f"🎂 **Настройки дня рождения**\n\n📅 Дата: {user.birthday or 'Не установлена'}\n👀 Отсчет: {'Включен' if user.show_birthday_countdown else 'Выключен'}\n👥 Видимость для класса: {'Включена' if user.show_birthday_to_class else 'Выключена'}\n🔔 Личное уведомление: {'Включено' if user.birthday_personal_notification else 'Выключено'}\n\nВыберите действие:"

    await query.edit_message_text(text, reply_markup=get_birthday_settings_keyboard(user), parse_mode="Markdown")
    return SET_BIRTHDAY

@timeout(CONVERSATION_TIMEOUT)
async def set_birthday_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "📅 Введите дату рождения в формате ГГГГ-ММ-ДД (например, 2005-04-15):"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return SET_BIRTHDAY

@timeout(CONVERSATION_TIMEOUT)
async def save_birthday_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    birthday_str = update.message.text.strip()

    try:
        birthday = datetime.strptime(birthday_str, "%Y-%m-%d")
        if birthday > datetime.now():
            await update.message.reply_text("Дата не может быть в будущем. Введите ещё раз:")
            return SET_BIRTHDAY

        user.birthday = birthday_str
        save_user(user)

        # ПУНКТ (ДР): перепланируем ежедневную проверку дня рождения,
        # чтобы поздравление сработало именно на правильную дату.
        try:
            schedule_user_birthday_job(context.application, user)
        except Exception as e:
            logger.error(f"Не удалось перепланировать birthday job: {e}")

        await update.message.reply_text(f"✅ Дата рождения установлена: {birthday_str}")
        return await user_settings(update, context)

    except ValueError:
        await update.message.reply_text("Введите дату в формате ГГГГ-ММ-ДД (например, 2005-04-15):")
        return SET_BIRTHDAY

async def toggle_birthday_countdown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    user.show_birthday_countdown = not user.show_birthday_countdown
    save_user(user)

    status = "включен" if user.show_birthday_countdown else "выключен"
    await query.edit_message_text(f"✅ Отсчет до дня рождения {status}!")
    return await birthday_settings_start(update, context)

async def toggle_birthday_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    user.show_birthday_to_class = not user.show_birthday_to_class
    save_user(user)

    status = "включена" if user.show_birthday_to_class else "выключена"
    await query.edit_message_text(f"✅ Видимость дня рождения для класса {status}!")
    return await birthday_settings_start(update, context)

async def toggle_birthday_personal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    user.birthday_personal_notification = not user.birthday_personal_notification
    save_user(user)

    # ПУНКТ (ДР): включаем/отключаем ежедневную проверку в JobQueue,
    # чтобы поздравление приходило (или не приходило) согласно настройке.
    try:
        schedule_user_birthday_job(context.application, user)
    except Exception as e:
        logger.error(f"Не удалось обновить birthday job: {e}")

    status = "включено" if user.birthday_personal_notification else "выключено"
    await query.edit_message_text(f"✅ Личное уведомление о дне рождения {status}!")
    return await birthday_settings_start(update, context)


async def set_birthday_notification_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает сценарий «настроить, во сколько каждый день приходит
    уведомление о ДР» (через сколько дней до моего дня рождения)."""
    query = update.callback_query
    await query.answer()
    text = (
        "⏰ Введите время в формате ЧЧ:ММ, в которое каждый день должно "
        "приходить уведомление о дне рождения (например, 09:00):"
    )
    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return SET_BIRTHDAY_NOTIFICATION_TIME


@timeout(CONVERSATION_TIMEOUT)
async def save_birthday_notification_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    time_str = (update.message.text or "").strip()
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await update.message.reply_text(
            "Введите время в формате ЧЧ:ММ (например, 09:00):",
            reply_markup=get_cancel_keyboard(),
        )
        return SET_BIRTHDAY_NOTIFICATION_TIME
    user.birthday_notification_time = time_str
    save_user(user)
    # Сбрасываем журнал «уже сегодня отправили», чтобы пользователь сразу
    # получил тестовое уведомление в новое время (если оно ещё не наступило
    # сегодня) и не страдал от старой пометки «уже отправлено».
    try:
        log = _load_notification_log()
        if user_id in log:
            log[user_id].pop('birthday', None)
            _save_notification_log(log)
    except Exception as e:
        logger.error(f"save_birthday_notification_time: log reset error: {e}")
    await update.message.reply_text(
        f"✅ Время ДР-уведомления установлено: {time_str}",
    )
    # Возвращаемся в меню настроек ДР через эмуляцию callback'а.
    # birthday_settings_start ждёт callback_query — поэтому показываем
    # клавиатуру обычным сообщением с inline-разметкой.
    await update.message.reply_text(
        "🎂 Настройки дня рождения:",
        reply_markup=get_birthday_settings_keyboard(user),
    )
    return SET_BIRTHDAY

@timeout(CONVERSATION_TIMEOUT)
async def notification_settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню настроек уведомлений.

    ВАЖНО: функция должна корректно работать и когда её вызывают после
    нажатия inline-кнопки (callback_query), и когда её вызывают после
    обычного текстового сообщения (например, из save_morning_time_handler).
    Раньше функция безусловно обращалась к update.callback_query.answer(),
    и после сохранения утреннего/вечернего времени падала с ошибкой,
    из-за чего пользователь оставался в состоянии ввода времени и видел
    сообщение «Произошла ошибка. Попробуйте ещё раз».
    """
    query = getattr(update, "callback_query", None)
    if query is not None:
        try:
            await query.answer()
        except Exception:
            pass
        user_id = str(query.from_user.id)
    else:
        user_id = str(update.effective_user.id)

    user = get_user(user_id)
    if not user:
        user = User(user_id)

    text = (
        f"🔔 **Настройки уведомлений**\n\n"
        f"Статус: {'Включены' if user.notifications else 'Выключены'}\n"
        f"⏰ Утреннее: {user.morning_notification_time}\n"
        f"🌙 Вечернее: {user.evening_notification_time}\n\n"
        f"Выберите действие:"
    )
    markup = get_notification_settings_keyboard(user)

    if query is not None:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
            return NOTIFICATION_SETTINGS
        except Exception:
            # Не удалось отредактировать — отправим новое сообщение ниже.
            pass

    if getattr(update, "message", None):
        try:
            await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
            return NOTIFICATION_SETTINGS
        except Exception:
            pass

    try:
        await context.bot.send_message(
            chat_id=int(user_id), text=text, reply_markup=markup, parse_mode="Markdown"
        )
    except Exception:
        pass
    return NOTIFICATION_SETTINGS

async def toggle_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    user.notifications = not user.notifications
    save_user(user)

    # ПУНКТ 6: при выключении удаляем job'ы; при включении — добавляем заново
    try:
        if user.notifications:
            schedule_user_daily_jobs(context.application, user)
        else:
            for j in context.application.job_queue.get_jobs_by_name(f"morning_{user.user_id}"):
                j.schedule_removal()
            for j in context.application.job_queue.get_jobs_by_name(f"evening_{user.user_id}"):
                j.schedule_removal()
    except Exception as e:
        logger.error(f"Ошибка обновления джобов уведомлений: {e}")

    status = "включены" if user.notifications else "выключены"
    await query.edit_message_text(f"✅ Уведомления {status}!")
    return await notification_settings_start(update, context)

async def set_morning_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "⏰ Введите время утреннего уведомления в формате ЧЧ:ММ (например, 08:00):"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return SET_MORNING_TIME

@timeout(CONVERSATION_TIMEOUT)
async def save_morning_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    time_str = update.message.text.strip()

    try:
        datetime.strptime(time_str, "%H:%M")
        user.morning_notification_time = time_str
        save_user(user)

        # ПУНКТ 6: перепланируем daily-уведомления
        try:
            schedule_user_daily_jobs(context.application, user)
        except Exception as e:
            logger.error(f"Не удалось перепланировать утреннее уведомление: {e}")

        await update.message.reply_text(
            f"✅ Время утреннего уведомления установлено: {time_str}",
            reply_markup=get_main_menu_keyboard(user)
        )
        return await notification_settings_start(update, context)

    except ValueError:
        await update.message.reply_text("Введите время в формате ЧЧ:ММ (например, 08:00):", reply_markup=get_cancel_keyboard())
        return SET_MORNING_TIME

async def set_evening_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "🌙 Введите время вечернего уведомления в формате ЧЧ:ММ (например, 22:00):"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return SET_EVENING_TIME

@timeout(CONVERSATION_TIMEOUT)
async def save_evening_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    time_str = update.message.text.strip()

    try:
        datetime.strptime(time_str, "%H:%M")
        user.evening_notification_time = time_str
        save_user(user)

        # ПУНКТ 6: перепланируем daily-уведомления
        try:
            schedule_user_daily_jobs(context.application, user)
        except Exception as e:
            logger.error(f"Не удалось перепланировать вечернее уведомление: {e}")

        await update.message.reply_text(
            f"✅ Время вечернего уведомления установлено: {time_str}",
            reply_markup=get_main_menu_keyboard(user)
        )
        return await notification_settings_start(update, context)

    except ValueError:
        await update.message.reply_text("Введите время в формате ЧЧ:ММ (например, 22:00):", reply_markup=get_cancel_keyboard())
        return SET_EVENING_TIME

async def set_morning_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "📝 Введите текст утреннего уведомления:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return SET_MORNING_TEXT

@timeout(CONVERSATION_TIMEOUT)
async def save_morning_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    text = update.message.text.strip()

    if not text:
        await update.message.reply_text("Введите текст:")
        return SET_MORNING_TEXT

    rejected = await reject_if_forbidden_chars(update, text, SET_MORNING_TEXT)
    if rejected is not None:
        return rejected

    user.morning_text = text
    save_user(user)

    await update.message.reply_text(f"✅ Текст утреннего уведомления сохранен!")
    return await notification_settings_start(update, context)

async def set_evening_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "📝 Введите текст вечернего уведомления:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return SET_EVENING_TEXT

@timeout(CONVERSATION_TIMEOUT)
async def save_evening_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    text = update.message.text.strip()

    if not text:
        await update.message.reply_text("Введите текст:")
        return SET_EVENING_TEXT

    rejected = await reject_if_forbidden_chars(update, text, SET_EVENING_TEXT)
    if rejected is not None:
        return rejected

    user.evening_text = text
    save_user(user)

    await update.message.reply_text(f"✅ Текст вечернего уведомления сохранен!")
    return await notification_settings_start(update, context)

async def suggest_function_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    text = "💡 Опишите функцию, которую вы хотите видеть в боте:"

    try:
        await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    except Exception:
        user_id = str(query.from_user.id)
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=get_cancel_keyboard())
    return SUGGEST_FUNCTION

@timeout(CONVERSATION_TIMEOUT)
async def suggest_function_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    suggestion = update.message.text.strip()

    if not suggestion:
        await update.message.reply_text("Введите описание:")
        return SUGGEST_FUNCTION

    rejected = await reject_if_forbidden_chars(update, suggestion, SUGGEST_FUNCTION)
    if rejected is not None:
        return rejected

    suggestions = load_data(SUGGESTIONS_FILE, [])
    suggestions.append({
        'user_id': user_id,
        'user_name': user.first_name,
        'suggestion': suggestion,
        'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M")
    })
    save_data(SUGGESTIONS_FILE, suggestions)

    try:
        await context.bot.send_message(
            chat_id=DEVELOPER_ID,
            text=f"💡 Новое предложение от {user.first_name}:\n\n{suggestion}"
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке предложения разработчику: {e}")

    await update.message.reply_text("✅ Спасибо за предложение! Мы рассмотрим его.")
    return await user_settings(update, context)

# ==================================
# === УПРАВЛЕНИЕ КЛАССАМИ ===
# ==================================

@timeout(CONVERSATION_TIMEOUT)
async def class_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_obj = get_class_by_user(user_id)

    if class_obj:
        if is_user_class_blocked(user_id, class_obj.class_code):
            text = "Вы заблокированы в этом классе."
            if hasattr(update, 'message') and update.message:
                await update.message.reply_text(text)
            else:
                await update.callback_query.edit_message_text(text)
            return MAIN_MENU

        text = f"🎓 **Управление классами**\n\n🏫 Текущий класс: {class_obj.class_name}\n🔑 Код: `{class_obj.class_code}`\n\nВыберите действие:"

        keyboard = [
            [InlineKeyboardButton("🔄 Сменить класс", callback_data="switch_class")],
            [InlineKeyboardButton("🚪 Выйти из класса", callback_data="leave_class")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]
        ]
    else:
        text = "🎓 **Управление классами**\n\nВы не состоите в классе.\n\nВыберите действие:"

        keyboard = [
            [InlineKeyboardButton("➕ Создать класс", callback_data="create_class")],
            [InlineKeyboardButton("🔗 Войти в класс", callback_data="join_class")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]
        ]

    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return CLASS_MANAGEMENT

@timeout(CONVERSATION_TIMEOUT)
async def create_class_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "➕ Введите название класса (например, '10А' или 'Информатика 2024'):"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return CREATE_CLASS_NAME

@timeout(CONVERSATION_TIMEOUT)
async def create_class_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    class_name = update.message.text.strip()

    if not class_name:
        await update.message.reply_text("Введите название класса:")
        return CREATE_CLASS_NAME

    rejected = await reject_if_forbidden_chars(update, class_name, CREATE_CLASS_NAME)
    if rejected is not None:
        return rejected

    class_code = generate_class_code()
    class_obj = Class(class_code, class_name, user_id)

    classes = load_classes()
    classes[class_code] = class_obj
    save_classes(classes)

    user.class_code = class_code
    user.created_classes.append(class_code)
    save_user(user)

    await update.message.reply_text(
        f"✅ Класс '{class_name}' создан!\n\n"
        f"🔑 Код класса: `{class_code}`\n\n"
        f"Поделитесь этим кодом с одноклассниками!",
        parse_mode="Markdown"
    )

    # Автоматически обновляем нижнюю клавиатуру: у создателя класса
    # появились новые кнопки (admin-функции, ДЗ для класса и т.п.).
    try:
        await update.message.reply_text(
            "📋 Меню обновлено:",
            reply_markup=get_main_menu_keyboard(user),
        )
    except Exception:
        pass

    # Сразу показываем создателю класса админ-панель — без лишнего шага через
    # «Управление классами». Создатель автоматически становится админом класса
    # (см. Class.__init__: self.admins = [str(creator_id)]), поэтому admin_panel
    # успешно пройдёт проверку прав.
    context.user_data['current_admin_class'] = class_code
    return await admin_panel(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def join_class_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "🔗 Введите код класса:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return JOIN_CLASS

@timeout(CONVERSATION_TIMEOUT)
async def join_class_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    class_code = update.message.text.strip().upper()

    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await update.message.reply_text("Класс с таким кодом не найден. Проверьте и попробуйте снова:")
        return JOIN_CLASS

    if str(user_id) in class_obj.students:
        await update.message.reply_text("✅ Вы уже состоите в этом классе!")
        return await class_management(update, context)

    if not check_class_limit(class_code):
        await update.message.reply_text("В классе максимум участников (40).")
        return await class_management(update, context)

    class_obj.students.append(str(user_id))
    classes = load_classes()
    classes[class_code] = class_obj
    save_classes(classes)

    user.class_code = class_code
    save_user(user)

    for admin_id in class_obj.admins:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"🎉 Новый участник в классе '{class_obj.class_name}': {user.first_name}"
            )
        except Exception as e:
            logger.error(f"Ошибка при уведомлении админа {admin_id}: {e}")

    await update.message.reply_text(f"✅ Вы присоединились к классу '{class_obj.class_name}'!")
    return await class_management(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def leave_class_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_obj = get_class_by_user(user_id)

    if not class_obj:
        await query.edit_message_text("Вы не состоите в классе.")
        return await class_management(update, context)

    if str(user_id) in class_obj.students:
        class_obj.students.remove(str(user_id))
    if str(user_id) in class_obj.admins:
        class_obj.admins.remove(str(user_id))

    classes = load_classes()
    classes[class_obj.class_code] = class_obj
    save_classes(classes)

    user.class_code = None
    save_user(user)

    await query.edit_message_text(f"✅ Вы вышли из класса '{class_obj.class_name}'.")
    return await class_management(update, context)

async def logout_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    await update.message.reply_text(
        "⚠️ Вы уверены, что хотите выйти из аккаунта?\n\n"
        "Все ваши данные будут сохранены. Для входа используйте /start",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, выйти", callback_data="confirm_logout")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]
        ])
    )
    return MAIN_MENU

async def confirm_logout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "👋 Вы вышли из аккаунта.\n\n"
        "Для входа отправьте /start"
    )
    return ConversationHandler.END

# ==================================
# === ПАНЕЛЬ РАЗРАБОТЧИКА ===
# ==================================

async def developer_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id != DEVELOPER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return MAIN_MENU

    text = "🛠️ **Панель разработчика**\n\nВыберите действие:"

    await update.message.reply_text(text, reply_markup=get_developer_keyboard(), parse_mode="Markdown")
    return DEV_PANEL

async def dev_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    users = load_users()
    classes = load_classes()

    total_users = len(users)
    active_users = sum(1 for u in users.values() if u.setup_completed)
    total_classes = len([c for c in classes.values() if c.is_active])
    total_students = sum(len(c.students) for c in classes.values() if c.is_active)

    stats_text = (
        f"📊 **Статистика бота**\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"✅ Активных пользователей: {active_users}\n"
        f"🏫 Всего классов: {total_classes}\n"
        f"🎓 Всего учеников: {total_students}\n"
    )

    await query.edit_message_text(stats_text, parse_mode="Markdown", reply_markup=get_developer_keyboard())
    return DEV_PANEL

async def dev_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "🌐 Введите сообщение для рассылки всем пользователям:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return DEV_BROADCAST

@timeout(CONVERSATION_TIMEOUT)
async def dev_broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text
    users = load_users()

    sent_count = 0
    for user_id, user in users.items():
        if not user.is_blocked:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📢 **Сообщение от разработчика:**\n\n{message_text}",
                    parse_mode=ParseMode.MARKDOWN
                )
                sent_count += 1
            except Exception as e:
                logger.error(f"Ошибка рассылки {user_id}: {e}")

    await update.message.reply_text(f"✅ Рассылка отправлена {sent_count} пользователям!")
    return await developer_panel(update, context)

async def dev_class_message_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    classes = load_classes()
    active_classes = {k: v for k, v in classes.items() if v.is_active}

    if not active_classes:
        await query.edit_message_text("Нет активных классов.", reply_markup=get_developer_keyboard())
        return DEV_PANEL

    text = "📨 Выберите класс для отправки сообщения:"

    await query.edit_message_text(text, reply_markup=get_classes_keyboard(active_classes, "dev_class_msg"))
    return DEV_CLASS_MESSAGE

async def dev_class_message_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    class_code = query.data.split("_")[3]
    context.user_data['dev_class_msg_code'] = class_code

    class_obj = get_class_by_code(class_code)

    text = f"📨 Введите сообщение для класса '{class_obj.class_name}':"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return DEV_CLASS_MESSAGE

@timeout(CONVERSATION_TIMEOUT)
async def dev_class_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text
    class_code = context.user_data.get('dev_class_msg_code')

    if not class_code:
        await update.message.reply_text("Класс не выбран.")
        return await developer_panel(update, context)

    class_obj = get_class_by_code(class_code)
    if not class_obj:
        await update.message.reply_text("Класс не найден.")
        return await developer_panel(update, context)

    sent_count = 0
    for member_id in class_obj.students:
        try:
            await context.bot.send_message(
                chat_id=member_id,
                text=f"📢 **Сообщение от разработчика:**\n\n{message_text}",
                parse_mode=ParseMode.MARKDOWN
            )
            sent_count += 1
        except Exception as e:
            logger.error(f"Ошибка отправки {member_id}: {e}")

    await update.message.reply_text(f"✅ Сообщение отправлено {sent_count} участникам класса '{class_obj.class_name}'!")
    return await developer_panel(update, context)

async def dev_delete_class_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    classes = load_classes()
    active_classes = {k: v for k, v in classes.items() if v.is_active}

    if not active_classes:
        await query.edit_message_text("Нет активных классов.", reply_markup=get_developer_keyboard())
        return DEV_PANEL

    text = "🗑️ Выберите класс для удаления:"

    await query.edit_message_text(text, reply_markup=get_classes_keyboard(active_classes, "dev_delete_class"))
    return DEV_DELETE_CLASS

async def dev_delete_class_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    class_code = query.data.split("_")[3]
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_developer_keyboard())
        return DEV_PANEL

    classes = load_classes()
    classes[class_code].is_active = False
    save_classes(classes)

    for member_id in class_obj.students:
        try:
            await context.bot.send_message(
                chat_id=member_id,
                text=f"⚠️ Класс '{class_obj.class_name}' был удален разработчиком."
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления {member_id}: {e}")

    await query.edit_message_text(f"✅ Класс '{class_obj.class_name}' удален.", reply_markup=get_developer_keyboard())
    return DEV_PANEL

async def dev_user_management_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("👤 **Управление пользователями**\n\nВыберите действие:", reply_markup=get_user_management_keyboard(), parse_mode="Markdown")
    return DEV_USER_MANAGEMENT

async def dev_block_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    users = load_users()

    text = "🚫 Выберите пользователя для блокировки:"

    await query.edit_message_text(text, reply_markup=get_users_keyboard(users, "dev_block"))
    return DEV_BLOCK_USER

async def dev_block_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.data.split("_")[2]
    context.user_data['blocking_user_id'] = user_id

    user = get_user(user_id)
    user_name = user.first_name if user else f"User {user_id}"
    username_str = f" (@{user.username})" if user and getattr(user, 'username', None) else ""

    text = f"🚫 Блокировка пользователя {user_name}{username_str}\n\nВведите цену разблокировки (в звездах):"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return DEV_BLOCK_USER_PRICE

@timeout(CONVERSATION_TIMEOUT)
async def dev_block_user_price_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id != DEVELOPER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return await developer_panel(update, context)

    try:
        price = int(update.message.text.strip())
        blocking_user_id = context.user_data.get('blocking_user_id')

        if not blocking_user_id:
            await update.message.reply_text("Пользователь не выбран.")
            return await developer_panel(update, context)

        block_user(blocking_user_id, user_id, price)

        user = get_user(blocking_user_id)
        user_name = user.first_name if user else f"User {blocking_user_id}"
        username_str = f" (@{user.username})" if user and getattr(user, 'username', None) else ""

        await update.message.reply_text(f"✅ Пользователь {user_name}{username_str} заблокирован!\nЦена разблокировки: {price} ⭐")

        try:
            await context.bot.send_message(
                chat_id=blocking_user_id,
                text=f"🚫 Вы были заблокированы разработчиком.\n\nДля разблокировки нужно: {price} ⭐"
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления заблокированного пользователя: {e}")

        context.user_data.pop('blocking_user_id', None)
        return await developer_panel(update, context)

    except ValueError:
        await update.message.reply_text("Введите число (цена в звёздах):")
        return DEV_BLOCK_USER_PRICE

async def dev_unblock_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    blocked_users = load_blocked_users()

    if not blocked_users:
        await query.edit_message_text("✅ Нет заблокированных пользователей.", reply_markup=get_developer_keyboard())
        return DEV_PANEL

    text = "✅ Выберите пользователя для разблокировки:"

    keyboard = []
    for blocked_id in blocked_users.keys():
        user = get_user(blocked_id)
        name = user.first_name if user else f"User {blocked_id}"
        if user and getattr(user, 'username', None):
            name += f" (@{user.username})"
        keyboard.append([InlineKeyboardButton(name, callback_data=f"dev_unblock_{blocked_id}")])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return DEV_USER_MANAGEMENT

async def dev_unblock_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.data.split("_")[2]

    user = get_user(user_id)
    user_name = user.first_name if user else f"User {user_id}"
    username_str = f" (@{user.username})" if user and getattr(user, 'username', None) else ""

    unblock_user(user_id)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="✅ Вы были разблокированы разработчиком!"
        )
    except Exception as e:
        logger.error(f"Ошибка уведомления разблокированного пользователя: {e}")

    await query.edit_message_text(f"✅ Пользователь {user_name}{username_str} разблокирован.", reply_markup=get_developer_keyboard())
    return DEV_PANEL

async def dev_set_prices_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        f"💰 **Текущие цены:**\n\n"
        f"• Кнопка (базовая): {PRICES['button_base']} ⭐\n"
        f"• Кнопка (инкремент): {PRICES['button_increment']} ⭐\n"
        f"• Разблокировка: {PRICES['unblock']} ⭐\n"
        f"• Разблокировка (разработчик): {PRICES.get('unblock_dev', 100)} ⭐\n"
        f"• Просмотр отправителя: {PRICES['view_sender']} ⭐\n"
        f"• Место под анонимки (мес): {PRICES.get('anon_keep_month', 100)} ⭐\n"
        f"• Генерация DEVORKS+ai: {PRICES.get('ai_generation', 0)} ⭐ (0 = бесплатно)\n\n"
        f"Введите новые цены в формате:\n"
        f"ключ:значение (например, button_base:30)\n"
        f"Доступные ключи: {', '.join(PRICES.keys())}"
    )

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return DEV_SET_PRICES

@timeout(CONVERSATION_TIMEOUT)
async def dev_set_prices_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id != DEVELOPER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return await developer_panel(update, context)

    input_text = update.message.text.strip()

    try:
        key, value = input_text.split(":")
        key = key.strip()
        value = int(value.strip())

        if key in PRICES:
            PRICES[key] = value
            save_prices(PRICES)
            # Синхронизация цен: перечитываем из БД, чтобы in-memory состояние
            # гарантированно совпало с сохранённым, и мгновенно применяем
            # новую цену во ВСЕХ модулях (они читают PRICES на каждом вызове).
            reload_prices()
            await update.message.reply_text(
                f"✅ Цена '{key}' изменена на {value} ⭐\n"
                f"🔄 Обновление мгновенно применено во всех модулях бота."
            )
        else:
            await update.message.reply_text(f"Неизвестный ключ '{key}'. Доступные: {', '.join(PRICES.keys())}")

        return await developer_panel(update, context)

    except ValueError:
        await update.message.reply_text("Формат: ключ:значение (например, button_base:30)")
        return DEV_SET_PRICES

# ==================================
# === БЫСТРОЕ ИЗМЕНЕНИЕ ЦЕН (НОВОЕ) ===
# ==================================

PRICE_LABELS = {
    'button_base': '🔘 Кнопка (базовая)',
    'button_increment': '➕ Кнопка (инкремент)',
    'unblock': '🔓 Разблокировка',
    'unblock_dev': '🔓 Разблокировка (dev)',
    'view_sender': '👁 Просмотр отправителя',
    'anon_keep_month': '📨 Место под анонимки (мес)',
    'ai_generation': '🤖 Генерация DEVORKS+ai',
}

def get_quick_prices_keyboard():
    keyboard = []
    for key in PRICES.keys():
        label = PRICE_LABELS.get(key, key)
        keyboard.append([
            InlineKeyboardButton(f"{label}: {PRICES[key]} ⭐", callback_data=f"qprice_pick_{key}")
        ])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="dev_back_panel")])
    return InlineKeyboardMarkup(keyboard)

def get_quick_price_value_keyboard(key):
    keyboard = [
        [
            InlineKeyboardButton("-10", callback_data=f"qprice_d_{key}_-10"),
            InlineKeyboardButton("-5", callback_data=f"qprice_d_{key}_-5"),
            InlineKeyboardButton("-1", callback_data=f"qprice_d_{key}_-1"),
            InlineKeyboardButton("+1", callback_data=f"qprice_d_{key}_1"),
            InlineKeyboardButton("+5", callback_data=f"qprice_d_{key}_5"),
            InlineKeyboardButton("+10", callback_data=f"qprice_d_{key}_10"),
        ],
        [
            InlineKeyboardButton("✏️ Ввести значение", callback_data=f"qprice_set_{key}"),
        ],
        [
            InlineKeyboardButton("⬅️ К списку цен", callback_data="dev_quick_prices"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

async def dev_quick_prices_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != DEVELOPER_ID:
        await query.edit_message_text("Доступ запрещён.")
        return DEV_PANEL

    text = "⚡ **Быстрое изменение цен**\n\nВыберите цену для изменения:"
    await query.edit_message_text(text, reply_markup=get_quick_prices_keyboard(), parse_mode="Markdown")
    return DEV_QUICK_PRICE_SELECT

async def dev_quick_price_pick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    key = query.data[len("qprice_pick_"):]
    if key not in PRICES:
        await query.edit_message_text("Неизвестный ключ цены.", reply_markup=get_developer_keyboard())
        return DEV_PANEL

    context.user_data['quick_price_key'] = key
    label = PRICE_LABELS.get(key, key)
    text = (
        f"⚡ **{label}**\n\n"
        f"Текущая цена: {PRICES[key]} ⭐\n\n"
        f"Используйте кнопки для быстрого изменения или введите новое значение."
    )
    await query.edit_message_text(text, reply_markup=get_quick_price_value_keyboard(key), parse_mode="Markdown")
    return DEV_QUICK_PRICE_SELECT

async def dev_quick_price_delta_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    # qprice_d_<key>_<delta>
    delta = int(parts[-1])
    key = "_".join(parts[2:-1])

    if key not in PRICES:
        await query.edit_message_text("Неизвестный ключ цены.", reply_markup=get_developer_keyboard())
        return DEV_PANEL

    new_value = max(0, PRICES[key] + delta)
    PRICES[key] = new_value
    save_prices(PRICES)
    # Синхронизация цен: перечитываем и мгновенно применяем во всех модулях.
    reload_prices()

    label = PRICE_LABELS.get(key, key)
    text = (
        f"⚡ **{label}**\n\n"
        f"Текущая цена: {PRICES[key]} ⭐\n\n"
        f"Используйте кнопки для быстрого изменения или введите новое значение."
    )
    await query.edit_message_text(text, reply_markup=get_quick_price_value_keyboard(key), parse_mode="Markdown")
    return DEV_QUICK_PRICE_SELECT

async def dev_quick_price_set_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    key = query.data[len("qprice_set_"):]
    if key not in PRICES:
        await query.edit_message_text("Неизвестный ключ цены.", reply_markup=get_developer_keyboard())
        return DEV_PANEL

    context.user_data['quick_price_key'] = key
    label = PRICE_LABELS.get(key, key)
    text = f"✏️ Введите новое значение для «{label}» (текущее: {PRICES[key]} ⭐):"
    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return DEV_QUICK_PRICE_VALUE

@timeout(CONVERSATION_TIMEOUT)
async def dev_quick_price_value_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != DEVELOPER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return await developer_panel(update, context)

    key = context.user_data.get('quick_price_key')
    if not key or key not in PRICES:
        await update.message.reply_text("Цена не выбрана.")
        return await developer_panel(update, context)

    try:
        value = int(update.message.text.strip())
        if value < 0:
            await update.message.reply_text("Цена не может быть отрицательной. Введите снова:")
            return DEV_QUICK_PRICE_VALUE
    except ValueError:
        await update.message.reply_text("Введите целое число:")
        return DEV_QUICK_PRICE_VALUE

    PRICES[key] = value
    save_prices(PRICES)
    # Синхронизация цен: перечитываем и мгновенно применяем во всех модулях.
    reload_prices()

    label = PRICE_LABELS.get(key, key)
    await update.message.reply_text(f"✅ «{label}» = {value} ⭐\n🔄 Обновление применено во всех модулях бота.")

    context.user_data.pop('quick_price_key', None)
    text = "⚡ **Быстрое изменение цен**\n\nВыберите цену для изменения:"
    await update.message.reply_text(text, reply_markup=get_quick_prices_keyboard(), parse_mode="Markdown")
    return DEV_QUICK_PRICE_SELECT

# ==================================
# === РЕДАКТИРОВАНИЕ ИНСТРУКЦИИ (НОВОЕ) ===
# ==================================

async def dev_edit_instructions_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != DEVELOPER_ID:
        await query.edit_message_text("Доступ запрещён.")
        return DEV_PANEL

    instructions = load_instructions()
    current = instructions.get('ru', '')

    preview = current if len(current) <= 1500 else current[:1500] + "…"

    text = (
        "📝 Редактирование инструкции\n\n"
        "Текущая инструкция:\n\n"
        f"{preview}\n\n"
        "Отправьте новый текст инструкции одним сообщением. "
        "Можно использовать плейсхолдеры цен: "
        "{button_base}, {button_increment}, {unblock}, {unblock_dev}, {view_sender}, {broadcast}."
    )

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return DEV_EDIT_INSTRUCTIONS

@timeout(CONVERSATION_TIMEOUT)
async def dev_edit_instructions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != DEVELOPER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return await developer_panel(update, context)

    new_text = update.message.text
    if not new_text or not new_text.strip():
        await update.message.reply_text("Введите текст инструкции:")
        return DEV_EDIT_INSTRUCTIONS

    instructions = load_instructions()
    instructions['ru'] = new_text
    # Помечаем инструкцию как «ручная редактура», чтобы автоматическое
    # обновление при смене INSTRUCTIONS_VERSION не затёрло её.
    instructions['version'] = 'custom'
    save_instructions(instructions)

    await update.message.reply_text("✅ Инструкция обновлена!")
    return await developer_panel(update, context)

# ==================================
# === НАПИСАТЬ ПОЛЬЗОВАТЕЛЮ (НОВОЕ) ===
# ==================================

async def dev_message_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != DEVELOPER_ID:
        await query.edit_message_text("Доступ запрещён.")
        return DEV_PANEL

    users = load_users()
    if not users:
        await query.edit_message_text("Нет пользователей.", reply_markup=get_developer_keyboard())
        return DEV_PANEL

    keyboard = []
    for uid, u in users.items():
        if uid == DEVELOPER_ID:
            continue
        name = u.first_name or f"User {uid}"
        if getattr(u, 'username', None):
            name += f" (@{u.username})"
        keyboard.append([InlineKeyboardButton(name, callback_data=f"dev_msg_pick_{uid}")])
        if len(keyboard) >= 50:
            break
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")])

    text = "💬 Выберите пользователя для отправки сообщения:"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return DEV_MESSAGE_USER_SELECT

async def dev_message_user_pick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    target_id = query.data[len("dev_msg_pick_"):]
    context.user_data['dev_message_target_id'] = target_id

    target_user = get_user(target_id)
    target_name = target_user.first_name if target_user else f"User {target_id}"
    if target_user and getattr(target_user, 'username', None):
        target_name += f" (@{target_user.username})"

    text = f"💬 Введите сообщение для пользователя «{target_name}»:"
    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return DEV_MESSAGE_USER_TEXT

@timeout(CONVERSATION_TIMEOUT)
async def dev_message_user_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id != DEVELOPER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return await developer_panel(update, context)

    target_id = context.user_data.get('dev_message_target_id')
    if not target_id:
        await update.message.reply_text("Получатель не выбран.")
        return await developer_panel(update, context)

    message_text = update.message.text
    if not message_text or not message_text.strip():
        await update.message.reply_text("Введите сообщение:")
        return DEV_MESSAGE_USER_TEXT

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"💬 Сообщение от разработчика:\n\n{message_text}"
        )
        await update.message.reply_text("✅ Сообщение отправлено!")
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения пользователю {target_id}: {e}")
        await update.message.reply_text(f"Не удалось отправить: {e}")

    context.user_data.pop('dev_message_target_id', None)
    return await developer_panel(update, context)

async def dev_global_button_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data['creating_global_button'] = True

    text = "🌍 **Создание глобальной кнопки**\n\nГлобальные кнопки видны всем пользователям.\n\nВыберите тип кнопки:"

    await query.edit_message_text(text, reply_markup=get_button_type_keyboard())
    return CREATE_GLOBAL_BUTTON_TYPE

async def dev_global_button_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    button_type = query.data.split("_")[2]
    context.user_data['global_button_type'] = button_type
    context.user_data.pop('creating_global_button', None)

    text = "📝 Введите название кнопки:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return CREATE_GLOBAL_BUTTON

@timeout(CONVERSATION_TIMEOUT)
async def dev_global_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id != DEVELOPER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return await developer_panel(update, context)

    button_name = update.message.text.strip()

    if not button_name:
        await update.message.reply_text("Введите название:")
        return CREATE_GLOBAL_BUTTON

    rejected = await reject_if_forbidden_chars(update, button_name, CREATE_GLOBAL_BUTTON)
    if rejected is not None:
        return rejected

    context.user_data['global_button_name'] = button_name
    button_type = context.user_data.get('global_button_type', 'text')

    if button_type == "url":
        await update.message.reply_text("🔗 Введите URL ссылки:")
        return CREATE_GLOBAL_BUTTON_URL
    else:
        await update.message.reply_text("📝 Введите содержимое кнопки:")
        return CREATE_GLOBAL_BUTTON_CONTENT

@timeout(CONVERSATION_TIMEOUT)
async def dev_global_button_url_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id != DEVELOPER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return await developer_panel(update, context)

    url = update.message.text.strip()
    button_name = context.user_data.get('global_button_name')

    if not url:
        await update.message.reply_text("Введите URL:")
        return CREATE_GLOBAL_BUTTON_URL

    if not (url.startswith('http://') or url.startswith('https://')):
        url = 'https://' + url

    button_id = generate_global_button_id()
    global_button = GlobalButton(button_id, button_name, url, "url")

    buttons = load_global_buttons()
    buttons[button_id] = global_button
    save_global_buttons(buttons)

    users = load_users()
    for uid, u in users.items():
        if not u.is_blocked and uid != user_id:
            try:
                # Автообновление клавиатуры — не нужно нажимать /start.
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"🔔 Разработчик создал новую кнопку: *{button_name}*",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=get_main_menu_keyboard(u),
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления {uid}: {e}")

    await update.message.reply_text(f"✅ Глобальная кнопка '{button_name}' создана!")

    context.user_data.pop('global_button_name', None)
    context.user_data.pop('global_button_type', None)
    context.user_data.pop('creating_global_button', None)
    return await developer_panel(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def dev_global_button_content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id != DEVELOPER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return await developer_panel(update, context)

    content = update.message.text.strip()
    button_name = context.user_data.get('global_button_name')

    if not button_name:
        await update.message.reply_text("Название не найдено.")
        return await developer_panel(update, context)

    rejected = await reject_if_forbidden_chars(update, content, CREATE_GLOBAL_BUTTON_CONTENT)
    if rejected is not None:
        return rejected

    button_id = generate_global_button_id()
    button_type = context.user_data.get('global_button_type', 'text')
    global_button = GlobalButton(button_id, button_name, content, button_type)

    buttons = load_global_buttons()
    buttons[button_id] = global_button
    save_global_buttons(buttons)

    users = load_users()
    for uid, u in users.items():
        if not u.is_blocked and uid != user_id:
            try:
                # Автообновление клавиатуры — не нужно нажимать /start.
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"🔔 Разработчик создал новую кнопку: *{button_name}*",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=get_main_menu_keyboard(u),
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления {uid}: {e}")

    await update.message.reply_text(f"✅ Глобальная кнопка '{button_name}' создана!")

    context.user_data.pop('global_button_name', None)
    context.user_data.pop('global_button_type', None)
    context.user_data.pop('creating_global_button', None)
    return await developer_panel(update, context)


# ПУНКТ 3: разработчик может удалить глобальные кнопки
async def dev_delete_global_button_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != DEVELOPER_ID:
        await query.edit_message_text("Доступ запрещён.", reply_markup=get_developer_keyboard())
        return DEV_PANEL

    buttons = load_global_buttons()
    if not buttons:
        await query.edit_message_text(
            "ℹ️ Нет глобальных кнопок для удаления.",
            reply_markup=get_developer_keyboard()
        )
        return DEV_PANEL

    keyboard = []
    for bid, btn in buttons.items():
        keyboard.append([InlineKeyboardButton(f"🗑️ {btn.name}", callback_data=f"dev_del_global_{bid}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")])

    await query.edit_message_text(
        "🗑️ **Удаление глобальной кнопки**\n\nВыберите кнопку для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return DEV_PANEL


async def dev_delete_global_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    if user_id != DEVELOPER_ID:
        await query.edit_message_text("Доступ запрещён.", reply_markup=get_developer_keyboard())
        return DEV_PANEL

    button_id = query.data.replace("dev_del_global_", "")
    buttons = load_global_buttons()
    if button_id in buttons:
        button_name = buttons[button_id].name
        del buttons[button_id]
        save_global_buttons(buttons)
        await query.edit_message_text(
            f"✅ Глобальная кнопка '{button_name}' удалена!",
            reply_markup=get_developer_keyboard()
        )
    else:
        await query.edit_message_text(
            "Кнопка не найдена.",
            reply_markup=get_developer_keyboard()
        )
    return DEV_PANEL


# ==================================
# === АДМИНСКАЯ ПАНЕЛЬ (ПРОДОЛЖЕНИЕ) ===
# ==================================

async def edit_teachers_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    text = "👨‍🏫 **Управление учителями**\n\nВыберите предмет для редактирования:"

    await query.edit_message_text(text, reply_markup=get_teachers_edit_keyboard(class_obj))
    return EDIT_TEACHERS

async def edit_teacher_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    subject = query.data.split("_")[2]
    context.user_data['editing_teacher_subject'] = subject

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    current_teacher = class_obj.teachers.get(subject, "Не указан")

    text = f"👨‍🏫 Редактирование учителя для предмета '{subject}'\n\nТекущий учитель: {current_teacher}\n\nВведите новое имя учителя:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return EDIT_TEACHER_NAME

@timeout(CONVERSATION_TIMEOUT)
async def save_teacher_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    teacher_name = update.message.text.strip()
    subject = context.user_data.get('editing_teacher_subject')
    class_code = context.user_data.get('current_admin_class')

    if not subject or not class_code:
        await update.message.reply_text("Данные не найдены.")
        return await admin_panel(update, context)

    rejected = await reject_if_forbidden_chars(update, teacher_name, EDIT_TEACHER_NAME)
    if rejected is not None:
        return rejected

    class_obj = get_class_by_code(class_code)
    if class_obj:
        class_obj.teachers[subject] = teacher_name

        classes = load_classes()
        classes[class_code] = class_obj
        save_classes(classes)

        await update.message.reply_text(f"✅ Учитель для '{subject}' обновлен!")
    else:
        await update.message.reply_text("Не удалось сохранить. Попробуйте снова.")

    context.user_data.pop('editing_teacher_subject', None)
    return await admin_panel(update, context)

async def add_teacher_subject_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "➕ Введите название нового предмета:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return EDIT_TEACHER_SUBJECT

@timeout(CONVERSATION_TIMEOUT)
async def add_teacher_subject_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    subject = update.message.text.strip()
    class_code = context.user_data.get('current_admin_class')

    if not subject:
        await update.message.reply_text("Введите название предмета.")
        return EDIT_TEACHER_SUBJECT

    rejected = await reject_if_forbidden_chars(update, subject, EDIT_TEACHER_SUBJECT)
    if rejected is not None:
        return rejected

    class_obj = get_class_by_code(class_code)
    if class_obj:
        if subject not in class_obj.teachers:
            class_obj.teachers[subject] = "Не указан"
            class_obj.subjects.append(subject)

            classes = load_classes()
            classes[class_code] = class_obj
            save_classes(classes)

            await update.message.reply_text(f"✅ Предмет '{subject}' добавлен!")
        else:
            await update.message.reply_text(f"Предмет '{subject}' уже существует.")
    else:
        await update.message.reply_text("Не удалось сохранить. Попробуйте снова.")

    return await admin_panel(update, context)


async def delete_teacher_list_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список учителей/предметов для удаления."""
    query = update.callback_query
    await query.answer()

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj or not class_obj.teachers:
        await query.edit_message_text("Нет учителей для удаления.")
        return await admin_panel(update, context)

    keyboard = []
    for subject, teacher in class_obj.teachers.items():
        cb_subj = subject[:40]
        keyboard.append([InlineKeyboardButton(
            f"🗑️ {subject} — {teacher}",
            callback_data=f"del_teacher_{cb_subj}"
        )])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="edit_teachers")])

    await query.edit_message_text(
        "🗑️ **Удаление учителя/предмета**\n\nВыберите для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return EDIT_TEACHERS


async def delete_teacher_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет выбранного учителя и предмет."""
    query = update.callback_query
    await query.answer()

    subject = query.data.replace("del_teacher_", "")
    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if class_obj:
        if subject in class_obj.teachers:
            del class_obj.teachers[subject]
        if subject in class_obj.subjects:
            class_obj.subjects.remove(subject)

        classes = load_classes()
        classes[class_code] = class_obj
        save_classes(classes)

        await query.edit_message_text(f"✅ Учитель и предмет '{subject}' удалены!")
    else:
        await query.edit_message_text("Не удалось удалить.")

    return await edit_teachers_start(update, context)


async def edit_bells_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    text = "🔔 **Управление звонками**\n\nВыберите урок для редактирования:"

    await query.edit_message_text(text, reply_markup=get_bells_edit_keyboard(class_obj))
    return EDIT_BELLS

async def edit_bell_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    lesson_num = query.data.split("_")[2]
    context.user_data['editing_bell_lesson'] = lesson_num

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    current_bell = class_obj.bells.get(lesson_num, {})
    start_time = current_bell.get('start', 'Не указано')
    end_time = current_bell.get('end', 'Не указано')

    text = f"🔔 Редактирование {lesson_num} урока\n\nТекущее время: {start_time} - {end_time}\n\nВведите новое время начала в формате ЧЧ:ММ:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return EDIT_BELL_TIME

@timeout(CONVERSATION_TIMEOUT)
async def save_bell_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    time_str = update.message.text.strip()
    lesson_num = context.user_data.get('editing_bell_lesson')
    class_code = context.user_data.get('current_admin_class')

    if not lesson_num or not class_code:
        await update.message.reply_text("Данные не найдены.")
        return await admin_panel(update, context)

    try:
        datetime.strptime(time_str, "%H:%M")

        class_obj = get_class_by_code(class_code)
        if class_obj:
            if lesson_num not in class_obj.bells:
                class_obj.bells[lesson_num] = {}

            class_obj.bells[lesson_num]['start'] = time_str
            context.user_data['editing_bell_start'] = time_str

            # ИСПРАВЛЕНО (бесконечный цикл звонков): раньше здесь возвращалось
            # то же состояние EDIT_BELL_TIME, из-за чего ввод времени окончания
            # снова попадал в save_bell_time_handler и перезаписывал начало —
            # цикл «начало → конец → начало → …» не заканчивался никогда.
            # Теперь переход идёт в ОТДЕЛЬНОЕ состояние EDIT_BELL_END.
            await update.message.reply_text(f"✅ Время начала: {time_str}\n\nВведите время окончания в формате ЧЧ:ММ:")
            return EDIT_BELL_END
        else:
            await update.message.reply_text("Не удалось сохранить. Попробуйте снова.")
            return await admin_panel(update, context)

    except ValueError:
        await update.message.reply_text("Введите время в формате ЧЧ:ММ:")
        return EDIT_BELL_TIME

@timeout(CONVERSATION_TIMEOUT)
async def save_bell_end_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    time_str = update.message.text.strip()
    lesson_num = context.user_data.get('editing_bell_lesson')
    class_code = context.user_data.get('current_admin_class')
    start_time = context.user_data.get('editing_bell_start')

    if not lesson_num or not class_code or not start_time:
        await update.message.reply_text("Данные не найдены.")
        return await admin_panel(update, context)

    try:
        end_t = datetime.strptime(time_str, "%H:%M")
        start_t = datetime.strptime(start_time, "%H:%M")
        # Честная валидация: урок не может заканчиваться раньше, чем начинается.
        if end_t <= start_t:
            await update.message.reply_text(
                "⚠️ Время окончания должно быть БОЛЬШЕ времени начала "
                f"({start_time}). Введите время окончания ещё раз в формате ЧЧ:ММ:"
            )
            return EDIT_BELL_END

        class_obj = get_class_by_code(class_code)
        if class_obj:
            class_obj.bells[lesson_num]['start'] = start_time
            class_obj.bells[lesson_num]['end'] = time_str

            classes = load_classes()
            classes[class_code] = class_obj
            save_classes(classes)

            await update.message.reply_text(f"✅ Время {lesson_num} урока обновлено: {start_time} - {time_str}")
        else:
            await update.message.reply_text("Не удалось сохранить. Попробуйте снова.")

    except ValueError:
        await update.message.reply_text("Введите время в формате ЧЧ:ММ:")
        return EDIT_BELL_END

    context.user_data.pop('editing_bell_lesson', None)
    context.user_data.pop('editing_bell_start', None)
    return await admin_panel(update, context)

async def set_holidays_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    current_holidays = class_obj.holidays or "Не установлены"

    text = f"🎉 Установка каникул\n\nТекущая дата: {current_holidays}\n\nВведите новую дату в формате ГГГГ-ММ-ДД:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return SET_HOLIDAYS

@timeout(CONVERSATION_TIMEOUT)
async def save_holidays_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    date_str = update.message.text.strip()
    class_code = context.user_data.get('current_admin_class')

    try:
        datetime.strptime(date_str, "%Y-%m-%d")

        class_obj = get_class_by_code(class_code)
        if class_obj:
            class_obj.holidays = date_str

            classes = load_classes()
            classes[class_code] = class_obj
            save_classes(classes)

            await update.message.reply_text(f"✅ Дата каникул установлена: {date_str}")
        else:
            await update.message.reply_text("Не удалось сохранить. Попробуйте снова.")

    except ValueError:
        await update.message.reply_text("Введите дату в формате ГГГГ-ММ-ДД:")
        return SET_HOLIDAYS

    return await admin_panel(update, context)

async def manage_admins_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    text = f"👥 **Управление администраторами**\n\nМаксимум 2 дополнительных админа (кроме создателя).\n\nВыберите действие:"

    await query.edit_message_text(text, reply_markup=get_admin_management_keyboard(class_obj))
    return MANAGE_ADMINS

async def add_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    new_admin_id = query.data.split("_")[2]
    class_code = context.user_data.get('current_admin_class')

    class_obj = get_class_by_code(class_code)
    if class_obj:
        if new_admin_id not in class_obj.admins:
            regular_admins = [a for a in class_obj.admins if a != class_obj.creator_id]
            if len(regular_admins) < 2:
                class_obj.admins.append(new_admin_id)

                classes = load_classes()
                classes[class_code] = class_obj
                save_classes(classes)

                new_admin = get_user(new_admin_id)
                if new_admin:
                    try:
                        await context.bot.send_message(
                            chat_id=new_admin_id,
                            text=f"🎉 Поздравляем! Вы назначены администратором класса '{class_obj.class_name}'!"
                        )
                    except Exception as e:
                        logger.error(f"Ошибка при уведомлении нового админа: {e}")

                await query.edit_message_text("✅ Администратор добавлен!")
            else:
                await query.edit_message_text("Достигнут лимит администраторов (2).")
        else:
            await query.edit_message_text("Пользователь уже администратор.")
    else:
        await query.edit_message_text("Не удалось сохранить. Попробуйте снова.")

    return await manage_admins_start(update, context)

async def remove_admin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    admin_id = query.data.split("_")[2]
    class_code = context.user_data.get('current_admin_class')

    class_obj = get_class_by_code(class_code)
    if class_obj:
        if admin_id in class_obj.admins and admin_id != class_obj.creator_id:
            class_obj.admins.remove(admin_id)

            classes = load_classes()
            classes[class_code] = class_obj
            save_classes(classes)

            former_admin = get_user(admin_id)
            if former_admin:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"⚠️ Вы больше не являетесь администратором класса '{class_obj.class_name}'."
                    )
                except Exception as e:
                    logger.error(f"Ошибка при уведомлении бывшего админа: {e}")

            await query.edit_message_text("✅ Администратор удален!")
        else:
            await query.edit_message_text("Нельзя удалить создателя класса.")
    else:
        await query.edit_message_text("Не удалось сохранить. Попробуйте снова.")

    return await manage_admins_start(update, context)

async def manage_custom_buttons_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    text = "🆕 **Управление кнопками класса**\n\nВыберите действие:"

    await query.edit_message_text(text, reply_markup=get_custom_buttons_management_keyboard(class_obj))
    return MANAGE_CUSTOM_BUTTONS

async def add_custom_button_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "🆕 **Добавление кнопки**\n\nВыберите тип кнопки:"

    await query.edit_message_text(text, reply_markup=get_button_type_keyboard())
    return CUSTOM_BUTTON_SELECT_TYPE

async def button_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    button_type = query.data.split("_")[2]
    context.user_data['custom_button_type'] = button_type

    text = "📝 Введите название кнопки:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return CUSTOM_BUTTON_NAME

@timeout(CONVERSATION_TIMEOUT)
async def custom_button_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    button_name = update.message.text.strip()
    button_type = context.user_data.get('custom_button_type', 'text')

    if not button_name:
        await update.message.reply_text("Введите название.")
        return CUSTOM_BUTTON_NAME

    rejected = await reject_if_forbidden_chars(update, button_name, CUSTOM_BUTTON_NAME)
    if rejected is not None:
        return rejected

    context.user_data['custom_button_name'] = button_name

    if button_type == "url":
        await update.message.reply_text("🔗 Введите URL ссылки:", reply_markup=get_cancel_keyboard())
        return CUSTOM_BUTTON_URL
    else:
        await update.message.reply_text("📝 Введите содержимое кнопки:", reply_markup=get_cancel_keyboard())
        return CUSTOM_BUTTON_CONTENT

@timeout(CONVERSATION_TIMEOUT)
async def custom_button_url_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    url = update.message.text.strip()
    button_name = context.user_data.get('custom_button_name')
    class_code = context.user_data.get('current_admin_class')

    if not url:
        await update.message.reply_text("Введите URL.")
        return CUSTOM_BUTTON_URL

    if not (url.startswith('http://') or url.startswith('https://')):
        url = 'https://' + url

    button_id = generate_button_id()
    custom_button = CustomButton(
        button_id=button_id,
        class_code=class_code,
        name=f"CLASS_{button_name}",
        content=url,
        creator_id=user_id,
        button_type="url"
    )

    buttons = load_data(CUSTOM_BUTTONS_FILE, {})
    buttons[button_id] = custom_button.to_dict()
    save_data(CUSTOM_BUTTONS_FILE, buttons)

    class_obj = get_class_by_code(class_code)
    if class_obj:
        class_obj.class_buttons.append(button_id)
        classes = load_classes()
        classes[class_code] = class_obj
        save_classes(classes)

        button_display_name = button_name.replace("CLASS_", "") if button_name.startswith("CLASS_") else button_name
        for member_id in class_obj.students + class_obj.admins:
            if member_id != str(user_id):
                try:
                    # Автообновление клавиатуры — не нужно нажимать /start.
                    member_user = get_user(member_id)
                    kb = get_main_menu_keyboard(member_user) if member_user else None
                    await context.bot.send_message(
                        chat_id=member_id,
                        text=f"🔔 Администратор класса '{class_obj.class_name}' создал новую кнопку: *{button_display_name}*",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb,
                    )
                except Exception as e:
                    logger.error(f"Ошибка уведомления {member_id}: {e}")

    await update.message.reply_text(f"✅ Кнопка '{button_name}' создана!")

    context.user_data.pop('custom_button_type', None)
    context.user_data.pop('custom_button_name', None)

    return await admin_panel(update, context)

@timeout(CONVERSATION_TIMEOUT)
async def custom_button_content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    content = update.message.text.strip()
    button_name = context.user_data.get('custom_button_name')
    class_code = context.user_data.get('current_admin_class')

    if not content:
        await update.message.reply_text("Введите содержимое.")
        return CUSTOM_BUTTON_CONTENT

    rejected = await reject_if_forbidden_chars(update, content, CUSTOM_BUTTON_CONTENT)
    if rejected is not None:
        return rejected

    button_id = generate_button_id()
    custom_button = CustomButton(
        button_id=button_id,
        class_code=class_code,
        name=f"CLASS_{button_name}",
        content=content,
        creator_id=user_id,
        button_type="text"
    )

    buttons = load_data(CUSTOM_BUTTONS_FILE, {})
    buttons[button_id] = custom_button.to_dict()
    save_data(CUSTOM_BUTTONS_FILE, buttons)

    class_obj = get_class_by_code(class_code)
    if class_obj:
        class_obj.class_buttons.append(button_id)
        classes = load_classes()
        classes[class_code] = class_obj
        save_classes(classes)

        button_display_name = button_name.replace("CLASS_", "") if button_name.startswith("CLASS_") else button_name
        for member_id in class_obj.students + class_obj.admins:
            if member_id != str(user_id):
                try:
                    # Автообновление клавиатуры — не нужно нажимать /start.
                    member_user = get_user(member_id)
                    kb = get_main_menu_keyboard(member_user) if member_user else None
                    await context.bot.send_message(
                        chat_id=member_id,
                        text=f"🔔 Администратор класса '{class_obj.class_name}' создал новую кнопку: *{button_display_name}*",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=kb,
                    )
                except Exception as e:
                    logger.error(f"Ошибка уведомления {member_id}: {e}")

    await update.message.reply_text(f"✅ Кнопка '{button_name}' создана!")

    context.user_data.pop('custom_button_type', None)
    context.user_data.pop('custom_button_name', None)

    return await admin_panel(update, context)

async def admin_delete_buttons_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    text = "🗑️ **Удаление кнопок**\n\nВыберите кнопку для удаления:"

    await query.edit_message_text(text, reply_markup=get_admin_delete_buttons_keyboard(class_obj))
    return ADMIN_DELETE_BUTTON

async def admin_delete_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    button_id = query.data.split("_")[3]
    class_code = context.user_data.get('current_admin_class')

    buttons = load_data(CUSTOM_BUTTONS_FILE, {})

    if button_id in buttons:
        button_name = buttons[button_id].get('name', 'Неизвестно')
        del buttons[button_id]
        save_data(CUSTOM_BUTTONS_FILE, buttons)

        class_obj = get_class_by_code(class_code)
        if class_obj and button_id in class_obj.class_buttons:
            class_obj.class_buttons.remove(button_id)
            classes = load_classes()
            classes[class_code] = class_obj
            save_classes(classes)

        await query.edit_message_text(f"✅ Кнопка удалена!")
    else:
        await query.edit_message_text("Кнопка не найдена.")

    return await manage_custom_buttons_start(update, context)

async def admin_button_for_personal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "📝 Введите название кнопки:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return CUSTOM_BUTTON_NAME

async def admin_button_for_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = "📝 Введите название кнопки для класса:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return CUSTOM_BUTTON_NAME

async def manage_homework_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    text = "📝 **Управление домашним заданием**\n\nВыберите действие:"

    await query.edit_message_text(text, reply_markup=get_homework_management_keyboard(class_obj))
    return MANAGE_HOMEWORK

async def add_homework_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ПУНКТ 9: упрощённое добавление ДЗ.
    Сразу показываем список предметов класса + кнопку «➕ Свой предмет»."""
    query = update.callback_query
    await query.answer()

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)
    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    keyboard = []
    subjects = class_obj.subjects or []
    if subjects:
        for subject in subjects:
            cb_subj = subject[:50]
            keyboard.append([InlineKeyboardButton(f"📚 {subject}", callback_data=f"hw_subject_{cb_subj}")])
    keyboard.append([InlineKeyboardButton("➕ Свой предмет", callback_data="hw_new_subject")])
    if subjects:
        keyboard.append([InlineKeyboardButton("🗑️ Удалить предмет", callback_data="hw_delete_subject_list")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")])

    if subjects:
        text = "📝 **Добавление ДЗ**\n\nВыберите предмет (или добавьте свой):"
    else:
        text = "📝 **Добавление ДЗ**\n\nВ классе пока нет предметов. Добавьте свой:"

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ADD_HOMEWORK


async def hw_delete_subject_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список предметов для удаления."""
    query = update.callback_query
    await query.answer()

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)
    if not class_obj or not class_obj.subjects:
        await query.edit_message_text("Нет предметов для удаления.")
        return await admin_panel(update, context)

    keyboard = []
    for subject in class_obj.subjects:
        cb_subj = subject[:45]
        keyboard.append([InlineKeyboardButton(f"🗑️ {subject}", callback_data=f"hw_del_subj_{cb_subj}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="add_homework")])

    await query.edit_message_text(
        "🗑️ **Удаление предмета**\n\nВыберите предмет для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ADD_HOMEWORK


async def hw_delete_subject_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет выбранный предмет из списка предметов класса."""
    query = update.callback_query
    await query.answer()

    subject = query.data.replace("hw_del_subj_", "")
    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if class_obj:
        if subject in class_obj.subjects:
            class_obj.subjects.remove(subject)
        if subject in class_obj.teachers:
            del class_obj.teachers[subject]

        classes = load_classes()
        classes[class_code] = class_obj
        save_classes(classes)

        await query.edit_message_text(f"✅ Предмет '{subject}' удалён!")
    else:
        await query.edit_message_text("Не удалось удалить.")

    return await add_homework_start(update, context)


async def hw_new_subject_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ПУНКТ 9: запрос имени нового предмета."""
    query = update.callback_query
    await query.answer()
    context.user_data['adding_new_hw_subject'] = True
    await query.edit_message_text(
        "📝 Введите название нового предмета (он будет сохранён в класс):",
        reply_markup=get_cancel_keyboard()
    )
    return ADD_HOMEWORK


@timeout(CONVERSATION_TIMEOUT)
async def add_homework_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ПУНКТ 9: универсальный текстовый обработчик внутри ADD_HOMEWORK.
    Может принимать: новое имя предмета, кастомную дату, или текст ДЗ —
    в зависимости от того, какой шаг сейчас активен."""
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    text_in = update.message.text.strip()

    # Шаг 1: пользователь вводит имя нового предмета
    if context.user_data.get('adding_new_hw_subject'):
        rejected = await reject_if_forbidden_chars(update, text_in, ADD_HOMEWORK)
        if rejected is not None:
            return rejected
        class_code = context.user_data.get('current_admin_class')
        class_obj = get_class_by_code(class_code)
        if not class_obj:
            await update.message.reply_text("Класс не найден.")
            return await admin_panel(update, context)
        if text_in not in class_obj.subjects:
            class_obj.subjects.append(text_in)
            classes = load_classes()
            classes[class_code] = class_obj
            save_classes(classes)
        context.user_data['adding_new_hw_subject'] = False
        context.user_data['homework_subject'] = text_in
        await _ask_homework_date(update, context, text_in)
        return ADD_HOMEWORK

    # Шаг 2: пользователь вводит кастомную дату
    if context.user_data.get('awaiting_hw_custom_date'):
        try:
            datetime.strptime(text_in, "%Y-%m-%d")
            context.user_data['homework_date'] = text_in
            context.user_data['awaiting_hw_custom_date'] = False
            await update.message.reply_text(
                f"📅 Дата: {text_in}\n\n📝 Введите текст ДЗ:",
                reply_markup=get_cancel_keyboard()
            )
            context.user_data['awaiting_hw_text'] = True
            return ADD_HOMEWORK
        except ValueError:
            await update.message.reply_text(
                "Введите дату в формате ГГГГ-ММ-ДД:",
                reply_markup=get_cancel_keyboard()
            )
            return ADD_HOMEWORK

    # Шаг 3: пользователь вводит текст ДЗ — обрабатывается save_homework_handler
    return await save_homework_handler(update, context)


async def _ask_homework_date(update, context, subject):
    """ПУНКТ 9: показывает кнопки выбора даты на всю неделю вперёд (7 дней,
    включая выходные). Также есть кнопка ручного ввода произвольной даты."""
    today = datetime.now().date()
    day_names_short = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard = []
    # 7 дней вперёд: 0..6 — кладём по 2 в ряд, чтобы влезли в экран Telegram.
    row = []
    for offset in range(7):
        d = today + timedelta(days=offset)
        if offset == 0:
            label = f"📅 Сегодня ({d.strftime('%d.%m')})"
        elif offset == 1:
            label = f"📅 Завтра ({d.strftime('%d.%m')})"
        else:
            label = f"📅 {day_names_short[d.weekday()]} ({d.strftime('%d.%m')})"
        row.append(InlineKeyboardButton(label, callback_data=f"hw_date_{d.strftime('%Y-%m-%d')}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("📆 Указать дату вручную", callback_data="hw_date_custom")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")])
    text = f"📚 Предмет: *{subject}*\n\n📅 Выберите дату:"
    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    elif hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def hw_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ПУНКТ 9: обработчик кнопок даты (Сегодня/Завтра/+2/+7/custom)."""
    query = update.callback_query
    await query.answer()
    if query.data == "hw_date_custom":
        context.user_data['awaiting_hw_custom_date'] = True
        await query.edit_message_text(
            "📅 Введите дату в формате ГГГГ-ММ-ДД (например, 2025-09-15):",
            reply_markup=get_cancel_keyboard()
        )
        return ADD_HOMEWORK
    date_str = query.data.replace("hw_date_", "")
    context.user_data['homework_date'] = date_str
    context.user_data['awaiting_hw_text'] = True
    subject = context.user_data.get('homework_subject', '?')
    await query.edit_message_text(
        f"📅 Дата: {date_str}\n📚 Предмет: {subject}\n\n📝 Введите текст ДЗ:",
        reply_markup=get_cancel_keyboard()
    )
    return ADD_HOMEWORK


async def homework_subject_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ПУНКТ 9: пользователь выбрал существующий предмет → быстро выбрать дату."""
    query = update.callback_query
    await query.answer()

    # query.data = "hw_subject_<subject>"
    subject = query.data[len("hw_subject_"):]
    context.user_data['homework_subject'] = subject

    await _ask_homework_date(update, context, subject)
    return ADD_HOMEWORK

@timeout(CONVERSATION_TIMEOUT)
async def save_homework_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    homework_text = update.message.text.strip()
    date_str = context.user_data.get('homework_date')
    subject = context.user_data.get('homework_subject')
    class_code = context.user_data.get('current_admin_class')

    if not date_str or not subject or not class_code:
        await update.message.reply_text("Данные не найдены.")
        return await admin_panel(update, context)

    rejected = await reject_if_forbidden_chars(update, homework_text, ADD_HOMEWORK)
    if rejected is not None:
        return rejected

    class_obj = get_class_by_code(class_code)
    if class_obj:
        if subject not in class_obj.homework:
            class_obj.homework[subject] = []

        class_obj.homework[subject].append({
            'text': homework_text,
            'date': date_str,
            'added_by': user_id,
            'added_at': datetime.now().strftime("%Y-%m-%d %H:%M")
        })

        classes = load_classes()
        classes[class_code] = class_obj
        save_classes(classes)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Ещё ДЗ", callback_data="add_homework")],
            [InlineKeyboardButton("⬅️ В админ-панель", callback_data="back_to_admin")],
            [InlineKeyboardButton("⬅️ В главное меню", callback_data="back_to_main")],
        ])
        await update.message.reply_text(
            f"✅ Домашнее задание добавлено!\n\n📅 Дата: {date_str}\n📚 Предмет: {subject}\n📝 Задание: {homework_text}",
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text("Не удалось сохранить. Попробуйте снова.")

    context.user_data.pop('homework_date', None)
    context.user_data.pop('homework_subject', None)
    context.user_data.pop('awaiting_hw_text', None)
    context.user_data.pop('awaiting_hw_custom_date', None)

    return MANAGE_HOMEWORK

async def delete_homework_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    if not class_obj.homework:
        await query.edit_message_text("📝 Домашнее задание не задано.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    text = "🗑️ **Удаление домашнего задания**\n\nВыберите задание для удаления:"

    await query.edit_message_text(text, reply_markup=get_homework_delete_keyboard(class_obj))
    return DELETE_HOMEWORK_SELECT

async def preview_homework_item_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает предпросмотр ДЗ перед удалением."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    subject = parts[2]
    index = int(parts[3])
    class_code = context.user_data.get('current_admin_class')

    class_obj = get_class_by_code(class_code)
    if class_obj and subject in class_obj.homework:
        if index < len(class_obj.homework[subject]):
            assignment = class_obj.homework[subject][index]
            date_str = assignment.get('date', 'Без даты')
            hw_text = assignment.get('text', '')

            preview = (
                f"👁 **Предпросмотр ДЗ**\n\n"
                f"📚 Предмет: {subject}\n"
                f"📅 Дата: {date_str}\n"
                f"📝 Текст:\n{hw_text}\n\n"
                f"Удалить это задание?"
            )

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Удалить", callback_data=f"confirm_delete_hw_{subject}_{index}")],
                [InlineKeyboardButton("⬅️ Назад к списку", callback_data="delete_homework")]
            ])

            await query.edit_message_text(preview, reply_markup=keyboard, parse_mode="Markdown")
            return DELETE_HOMEWORK_SELECT
        else:
            await query.edit_message_text("Задание не найдено.")
    else:
        await query.edit_message_text("Не удалось открыть.")

    return await admin_panel(update, context)


async def confirm_delete_homework_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления ДЗ после предпросмотра."""
    query = update.callback_query
    await query.answer()

    data = query.data.replace("confirm_delete_hw_", "")
    parts = data.rsplit("_", 1)
    subject = parts[0]
    index = int(parts[1])
    class_code = context.user_data.get('current_admin_class')

    class_obj = get_class_by_code(class_code)
    if class_obj and subject in class_obj.homework:
        if index < len(class_obj.homework[subject]):
            del class_obj.homework[subject][index]

            if not class_obj.homework[subject]:
                del class_obj.homework[subject]

            classes = load_classes()
            classes[class_code] = class_obj
            save_classes(classes)

            await query.edit_message_text("✅ Домашнее задание удалено!")
        else:
            await query.edit_message_text("Задание не найдено.")
    else:
        await query.edit_message_text("Не удалось удалить.")

    return await admin_panel(update, context)


async def delete_homework_item_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    subject = parts[2]
    index = int(parts[3])
    class_code = context.user_data.get('current_admin_class')

    class_obj = get_class_by_code(class_code)
    if class_obj and subject in class_obj.homework:
        if index < len(class_obj.homework[subject]):
            del class_obj.homework[subject][index]

            if not class_obj.homework[subject]:
                del class_obj.homework[subject]

            classes = load_classes()
            classes[class_code] = class_obj
            save_classes(classes)

            await query.edit_message_text("✅ Домашнее задание удалено!")
        else:
            await query.edit_message_text("Задание не найдено.")
    else:
        await query.edit_message_text("Не удалось удалить.")

    return await admin_panel(update, context)

async def view_homework_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    homework_text = format_homework(class_obj.homework)

    await query.edit_message_text(homework_text, parse_mode="Markdown", reply_markup=get_back_button_keyboard())
    return MANAGE_HOMEWORK

async def quick_homework_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    subject = query.data.split("_")[2]
    context.user_data['homework_subject'] = subject

    text = f"📅 Введите дату для домашнего задания по '{subject}' в формате ГГГГ-ММ-ДД:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return ADD_HOMEWORK

async def manage_class_users_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if not class_obj:
        await query.edit_message_text("Класс не найден.", reply_markup=get_admin_panel_keyboard())
        return ADMIN_PANEL

    text = f"👤 **Управление учениками**\n\nКласс: {class_obj.class_name}\n\nВыберите действие:"

    await query.edit_message_text(text, reply_markup=get_class_users_management_keyboard(class_obj))
    return MANAGE_CLASS_USERS

async def class_block_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    blocked_user_id = query.data.split("_")[2]
    class_code = context.user_data.get('current_admin_class')

    class_obj = get_class_by_code(class_code)
    if class_obj:
        if blocked_user_id not in class_obj.blocked_users:
            block_user_in_class(blocked_user_id, class_code, user_id)

            blocked_user = get_user(blocked_user_id)
            blocked_name = blocked_user.first_name if blocked_user else f"User {blocked_user_id}"

            try:
                await context.bot.send_message(
                    chat_id=blocked_user_id,
                    text=f"🚫 Вы были заблокированы в классе '{class_obj.class_name}'.\n\nДля разблокировки нужно: {PRICES['unblock']} ⭐"
                )
            except Exception as e:
                logger.error(f"Ошибка при уведомлении заблокированного пользователя: {e}")

            await query.edit_message_text(f"✅ Пользователь {blocked_name} заблокирован!")
        else:
            await query.edit_message_text("Пользователь уже заблокирован.")
    else:
        await query.edit_message_text("Не удалось заблокировать.")

    return await manage_class_users_start(update, context)

async def class_unblock_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    unblocked_user_id = query.data.split("_")[2]
    class_code = context.user_data.get('current_admin_class')

    class_obj = get_class_by_code(class_code)
    if class_obj:
        if unblocked_user_id in class_obj.blocked_users:
            unblock_user_in_class(unblocked_user_id, class_code)

            unblocked_user = get_user(unblocked_user_id)
            unblocked_name = unblocked_user.first_name if unblocked_user else f"User {unblocked_user_id}"

            try:
                await context.bot.send_message(
                    chat_id=unblocked_user_id,
                    text=f"✅ Вы были разблокированы в классе '{class_obj.class_name}'!"
                )
            except Exception as e:
                logger.error(f"Ошибка при уведомлении разблокированного пользователя: {e}")

            await query.edit_message_text(f"✅ Пользователь {unblocked_name} разблокирован!")
        else:
            await query.edit_message_text("Пользователь не заблокирован.")
    else:
        await query.edit_message_text("Не удалось разблокировать.")

    return await manage_class_users_start(update, context)

async def show_class_code_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_code = context.user_data.get('current_admin_class')
    class_obj = get_class_by_code(class_code)

    if class_obj:
        text = f"🔑 **Код класса:**\n\n`{class_code}`\n\n🏫 Название: {class_obj.class_name}\n\nПоделитесь этим кодом с одноклассниками!"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=get_back_button_keyboard())
    else:
        await query.edit_message_text("Класс не найден.", reply_markup=get_back_button_keyboard())

    return ADMIN_PANEL

# ==================================
# === АНОНИМНЫЕ СООБЩЕНИЯ ===
# ==================================

async def anonymous_select_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    target_user_id = query.data.split("_")[2]
    context.user_data['anon_target'] = target_user_id

    target_user = get_user(target_user_id)
    target_name = target_user.first_name if target_user else "Неизвестно"

    text = f"🕵️ Отправка анонимного сообщения для {target_name}\n\nВведите ваше сообщение:"

    await query.edit_message_text(text, reply_markup=get_cancel_keyboard())
    return ANONYMOUS_SEND_MESSAGE

@timeout(CONVERSATION_TIMEOUT)
async def send_anonymous_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    message_text = update.message.text.strip()
    target_user_id = context.user_data.get('anon_target')

    if not target_user_id:
        await update.message.reply_text("Получатель не выбран.")
        return MAIN_MENU

    # Защита от пустого сообщения — без неё бы улетал пустой текст в Telegram,
    # и тот возвращал бы BadRequest «Message text is empty».
    if not message_text:
        await update.message.reply_text(
            "Сообщение пустое. Введите текст и отправьте снова."
        )
        return ANONYMOUS_SEND_MESSAGE

    rejected = await reject_if_forbidden_chars(update, message_text, ANONYMOUS_SEND_MESSAGE)
    if rejected is not None:
        return rejected

    # Запрет отправлять анонимку самому себе — иначе клавиатура «узнать
    # отправителя» становится бессмысленной и путает пользователя.
    if str(target_user_id) == str(user_id):
        await update.message.reply_text(
            "Нельзя отправить анонимное сообщение самому себе."
        )
        context.user_data.pop('anon_target', None)
        return MAIN_MENU

    # Проверяем, что получатель существует в базе бота. Если его нет —
    # значит он никогда не запускал бота, и Telegram всё равно не даст
    # отправить ему личное сообщение от имени бота.
    target_user = get_user(target_user_id)
    if not target_user:
        await update.message.reply_text(
            "Получатель не найден или ещё ни разу не запускал бота. "
            "Передать сообщение невозможно."
        )
        context.user_data.pop('anon_target', None)
        return MAIN_MENU

    # chat_id для Telegram должен быть числом. Сохраняем строковое
    # представление в JSON, но в API передаём int — это самый надёжный
    # формат (PTB не всегда корректно интерпретирует строковый ID).
    try:
        target_chat_id = int(target_user_id)
    except (TypeError, ValueError):
        await update.message.reply_text(
            "Некорректный получатель. Попробуйте выбрать его ещё раз."
        )
        context.user_data.pop('anon_target', None)
        return MAIN_MENU

    anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})
    msg_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))

    anonymous_messages[msg_id] = {
        'from_user_id': user_id,
        'to_user_id': str(target_user_id),
        'message': message_text,
        'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M"),
        'sender_viewed': False
    }

    # Цена «узнать отправителя» — берём через .get с дефолтом, чтобы
    # отсутствующий ключ в prices.json не ронял всю отправку KeyError'ом.
    view_sender_price = PRICES.get('view_sender', 60)

    try:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=(
                f"🕵️ Вам пришло анонимное сообщение:\n\n{message_text}\n\n"
                f"💰 Узнать отправителя: {view_sender_price} ⭐"
            ),
            reply_markup=get_anonymous_reply_keyboard(msg_id),
        )
    except TGForbidden as e:
        # Получатель заблокировал бота либо ни разу не запускал его.
        logger.warning(
            f"Анонимка не доставлена (Forbidden): from={user_id} "
            f"to={target_user_id}: {e}"
        )
        await update.message.reply_text(
            "Получатель заблокировал бота или ещё не запускал его. "
            "Сообщение не доставлено."
        )
        context.user_data.pop('anon_target', None)
        return MAIN_MENU
    except TGBadRequest as e:
        # Самое частое — «Chat not found» (некорректный chat_id).
        logger.warning(
            f"Анонимка не доставлена (BadRequest): from={user_id} "
            f"to={target_user_id}: {e}"
        )
        await update.message.reply_text(
            "Не удалось отправить сообщение: чат с получателем не найден."
        )
        context.user_data.pop('anon_target', None)
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Ошибка при отправке анонимного сообщения: {e}")
        await update.message.reply_text(
            "Не удалось отправить сообщение. Попробуйте позже."
        )
        context.user_data.pop('anon_target', None)
        return MAIN_MENU

    # Сохраняем сообщение ТОЛЬКО после успешной доставки, чтобы получатель
    # позже мог нажать «узнать отправителя» — иначе msg_id оказался бы
    # в базе, а пользователь о нём ничего не знал.
    save_data(ANONYMOUS_MESSAGES_FILE, anonymous_messages)

    await update.message.reply_text("✅ Анонимное сообщение отправлено!")
    context.user_data.pop('anon_target', None)
    return MAIN_MENU

# ==================================
# === ТАЙМЕР ===
# ==================================

async def quick_timer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик быстрых таймеров (5/10/15/30/60/120/180/360/720 мин)."""
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    minutes = int(query.data.replace("quick_timer_", ""))
    # ПУНКТ (таймер): используем UTC, а не локальное время сервера —
    # иначе на хостингах с TZ != UTC расчёт задержки даёт отрицательное
    # значение и таймер не срабатывает.
    now_utc = datetime.utcnow()
    tz_offset = user.timezone if user else 3
    local_now = now_utc + timedelta(hours=tz_offset)
    target_local = local_now + timedelta(minutes=minutes)

    date_str = target_local.strftime("%Y-%m-%d")
    time_str = target_local.strftime("%H:%M")

    context.user_data['timer_date'] = date_str
    context.user_data['timer_time'] = time_str

    if minutes < 60:
        label = f"{minutes} мин"
    elif minutes < 1440:
        hours = minutes // 60
        remaining = minutes % 60
        label = f"{hours} ч" + (f" {remaining} мин" if remaining else "")
    else:
        days = minutes // 1440
        label = f"{days} д." if days > 1 else "1 день"

    await query.edit_message_text(
        f"⏰ Таймер на **{label}**\n"
        f"📅 Сработает: {date_str} в {time_str}\n\n"
        f"📝 Введите текст напоминания:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="Markdown"
    )
    return TIMER_SET_TEXT


async def timer_custom_datetime_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переход на ручной ввод даты+времени в одном сообщении."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📅 Введите дату и время в одном сообщении:\n\n"
        "Формат: ГГГГ-ММ-ДД ЧЧ:ММ\n"
        "Пример: 2025-09-15 14:30\n\n"
        "Или только время (сегодня): 14:30",
        reply_markup=get_cancel_keyboard()
    )
    return TIMER_SET_DATE


@timeout(CONVERSATION_TIMEOUT)
async def timer_set_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    input_str = update.message.text.strip()

    # Попробуем распарсить "ГГГГ-ММ-ДД ЧЧ:ММ"
    try:
        dt = datetime.strptime(input_str, "%Y-%m-%d %H:%M")
        context.user_data['timer_date'] = dt.strftime("%Y-%m-%d")
        context.user_data['timer_time'] = dt.strftime("%H:%M")
        await update.message.reply_text(
            f"📅 {dt.strftime('%Y-%m-%d')} в {dt.strftime('%H:%M')}\n\n📝 Введите текст напоминания:"
        )
        return TIMER_SET_TEXT
    except ValueError:
        pass

    # Попробуем только время "ЧЧ:ММ" (сегодня)
    try:
        t = datetime.strptime(input_str, "%H:%M")
        tz_offset = user.timezone if user else 3
        now_utc = datetime.utcnow()
        local_now = now_utc + timedelta(hours=tz_offset)
        date_str = local_now.strftime("%Y-%m-%d")
        context.user_data['timer_date'] = date_str
        context.user_data['timer_time'] = input_str
        await update.message.reply_text(
            f"📅 Сегодня ({date_str}) в {input_str}\n\n📝 Введите текст напоминания:"
        )
        return TIMER_SET_TEXT
    except ValueError:
        pass

    # Попробуем только дату "ГГГГ-ММ-ДД"
    try:
        datetime.strptime(input_str, "%Y-%m-%d")
        context.user_data['timer_date'] = input_str
        await update.message.reply_text(
            f"📅 Дата: {input_str}\n\n⏰ Введите время (ЧЧ:ММ):",
            reply_markup=get_cancel_keyboard()
        )
        return TIMER_SET_TIME
    except ValueError:
        pass

    await update.message.reply_text(
        "Введите дату и время: ГГГГ-ММ-ДД ЧЧ:ММ\nИли только время: ЧЧ:ММ",
        reply_markup=get_cancel_keyboard()
    )
    return TIMER_SET_DATE

@timeout(CONVERSATION_TIMEOUT)
async def timer_set_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    time_str = update.message.text.strip()

    try:
        datetime.strptime(time_str, "%H:%M")
        context.user_data['timer_time'] = time_str

        await update.message.reply_text(f"⏰ Время: {time_str}\n\n📝 Введите текст напоминания:")
        return TIMER_SET_TEXT

    except ValueError:
        await update.message.reply_text("Введите время в формате ЧЧ:ММ:")
        return TIMER_SET_TIME

@timeout(CONVERSATION_TIMEOUT)
async def timer_set_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    text = update.message.text.strip()
    date_str = context.user_data.get('timer_date')
    time_str = context.user_data.get('timer_time')

    if not date_str or not time_str:
        await update.message.reply_text("Данные не найдены.")
        return MAIN_MENU

    rejected = await reject_if_forbidden_chars(update, text, TIMER_SET_TEXT)
    if rejected is not None:
        return rejected

    timers = load_data(TIMERS_FILE, {})
    timer_id = generate_timer_id()

    timers[timer_id] = {
        'user_id': user_id,
        'target_date': date_str,
        'target_time': time_str,
        'text': text,
        'is_active': True,
        'created_date': datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    save_data(TIMERS_FILE, timers)

    # ПУНКТ 5: реально планируем срабатывание таймера через JobQueue
    try:
        schedule_timer_job(context.application, timer_id, timers[timer_id])
    except Exception as e:
        logger.error(f"Не удалось запланировать таймер {timer_id}: {e}")

    # ПУНКТ (таймер): сообщаем пользователю, что поставлен таймер, и сразу
    # предлагаем поставить ещё один — без возврата в главное меню.
    # Так можно установить несколько таймеров/дат подряд.
    another_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ещё один таймер", callback_data="timer_add_more")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_main")],
    ])
    await update.message.reply_text(
        f"✅ Таймер установлен!\n\n📅 Дата: {date_str}\n⏰ Время: {time_str}\n📝 Текст: {text}\n\n"
        f"Бот пришлёт уведомление в указанное время.\n"
        f"Можно установить ещё один таймер — их может быть сколько угодно.",
        reply_markup=another_kb,
    )
    # Обновим reply-клавиатуру главного меню (вдруг пользователь потом нажмёт).
    try:
        await update.message.reply_text(
            "📋 Главное меню:", reply_markup=get_main_menu_keyboard(user)
        )
    except Exception:
        pass

    context.user_data.pop('timer_date', None)
    context.user_data.pop('timer_time', None)

    return MAIN_MENU

# ==================================
# === ОБРАБОТЧИКИ CALLBACK ===
# ==================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    # Обязательная подписка на канал: не пускаем пользователя никуда, пока
    # он не подписан. Исключение — сама кнопка проверки подписки и кнопка
    # разблокировки аккаунта (чтобы можно было оплатить разблок).
    if data != "check_subscription" and data != "unblock_self":
        if not await ensure_subscribed(update, context):
            return MAIN_MENU

    if data == "check_subscription":
        await check_subscription_handler(update, context)
        return MAIN_MENU

    if data.startswith("view_anon_msg_"):
        return await view_anon_message_detail(update, context)
    elif data == "view_anon_list":
        return await view_anonymous_messages(update, context)
    elif data.startswith("reply_anon_"):
        return await start_anon_reply(update, context)
    elif data.startswith("pay_view_sender_"):
        return await pay_view_sender_handler(update, context)
    # ПУНКТ 11: новые инлайн-кнопки оплаты под входящим анонимным сообщением.
    elif data.startswith("anon_pay_virt_"):
        return await anon_pay_virt_handler(update, context)
    elif data.startswith("anon_pay_xtr_"):
        return await anon_pay_xtr_handler(update, context)
    elif data.startswith("anon_user_"):
        return await anonymous_select_user(update, context)
    # === Управление хранением входящих анонимок ===
    # Кнопки добавлены в get_anonymous_messages_keyboard / клавиатурах
    # подтверждения. Порядок проверок важен: «anon_del_» должен идти
    # после специфичных префиксов (anon_delete_mode, anon_clear_*), иначе
    # перехватит их.
    elif data == "anon_clear_all":
        return await anon_clear_all_start(update, context)
    elif data == "anon_clear_yes":
        return await anon_clear_all_confirm(update, context)
    elif data == "anon_clear_no":
        return await view_anonymous_messages(update, context)
    elif data == "anon_delete_mode":
        return await anon_delete_mode_start(update, context)
    elif data == "anon_buy_space":
        return await anon_buy_space_start(update, context)
    elif data == "anon_buy_space_virt":
        return await anon_buy_space_virt(update, context)
    elif data == "anon_buy_space_xtr":
        return await anon_buy_space_xtr(update, context)
    elif data == "toggle_anon_purge_notify":
        return await toggle_anon_purge_notify_handler(update, context)
    elif data.startswith("anon_del_"):
        # ВНИМАНИЕ: эта ветка должна идти строго после всех более
        # длинных префиксов «anon_*», начинающихся с того же `anon_`.
        return await anon_delete_one_handler(update, context)

    if data == "buy_stars":
        return await buy_stars_handler(update, context)
    elif data.startswith("buy_stars_"):
        return await buy_stars_invoice_handler(update, context)
    elif data == "create_paid_button":
        return await create_paid_button_handler(update, context)
    elif data.startswith("buy_button_"):
        # ПУНКТ (покупка кнопки): раньше этот callback нигде не обрабатывался и клик
        # «✅ Купить кнопку» молча игнорировался — именно поэтому «не купить кнопку».
        return await purchase_button_handler(update, context)
    elif data == "unblock_self":
        return await unblock_self_handler(update, context)
    elif data == "view_sender":
        return await view_sender_handler(update, context)
    elif data.startswith("view_sender_"):
        return await view_sender_confirm_handler(update, context)
    elif data == "stars_stats":
        return await stars_stats_handler(update, context)
    elif data == "stars_balance":
        user_id = str(query.from_user.id)
        user = get_user(user_id)
        if user:
            await query.answer(f"💰 Баланс: {user.stars_balance} ⭐")
        else:
            await query.answer("Ошибка")
        return MAIN_MENU

    # === Смена режима личности ИИ (normal / hamlo / warm) ===
    if data.startswith("ai_persona_"):
        _mode = data[len("ai_persona_"):]
        _cb_user = get_user(str(query.from_user.id))
        if _cb_user is None:
            _cb_user = User(str(query.from_user.id))
        if _mode in AI_PERSONA_MODES:
            _cb_user.ai_persona = _mode
            save_user(_cb_user)
            # Мгновенно применяем к текущему диалогу (если он уже открыт).
            _conv = user_conversations.get(str(query.from_user.id))
            if _conv and _conv[0].get("role") == "system":
                _conv[0]["content"] = _ai_chat_full_prompt(_mode)
            try:
                await query.answer(
                    f"🎭 Режим ИИ: {AI_PERSONA_MODES[_mode]['title']} — включён!"
                )
            except Exception:
                pass
            # Перерисовываем кнопку с галочкой текущего режима.
            _kb = [[InlineKeyboardButton(
                f"{'✅ ' if _mode == 'normal' else ''}{AI_PERSONA_MODES['normal']['button']}",
                callback_data="ai_persona_normal",
            )],
            [InlineKeyboardButton(
                f"{'✅ ' if _mode == 'hamlo' else ''}{AI_PERSONA_MODES['hamlo']['button']}",
                callback_data="ai_persona_hamlo",
            )],
            [InlineKeyboardButton(
                f"{'✅ ' if _mode == 'warm' else ''}{AI_PERSONA_MODES['warm']['button']}",
                callback_data="ai_persona_warm",
            )]]
            try:
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(_kb))
            except Exception:
                pass
        else:
            try:
                await query.answer("Неизвестный режим")
            except Exception:
                pass
        return MAIN_MENU

    # === Сброс статистики Stars (история переводов больше не ведётся) ===
    if data == "dev_reset_stars":
        return await dev_reset_stars_stats_start(update, context)
    elif data == "dev_reset_stars_yes":
        return await dev_reset_stars_stats_confirm(update, context)

    if data == "create_personal_button":
        return await create_personal_button_start(update, context)
    elif data == "manage_personal_buttons":
        return await manage_personal_buttons_start(update, context)
    elif data.startswith("edit_personal_button_name_"):
        return await edit_personal_button_name_start(update, context)
    elif data.startswith("edit_personal_button_content_"):
        return await edit_personal_button_content_start(update, context)
    elif data.startswith("edit_personal_button_type_"):
        return await edit_personal_button_type_start(update, context)
    elif data.startswith("edit_personal_button_"):
        return await edit_personal_button_start(update, context)
    elif data.startswith("confirm_delete_personal_button_"):
        return await confirm_delete_personal_button(update, context)
    elif data.startswith("cancel_delete_personal_button_"):
        return await cancel_delete_personal_button(update, context)
    elif data.startswith("delete_personal_button_"):
        return await delete_personal_button_start(update, context)
    elif data == "reorder_personal_buttons":
        return await reorder_rows_start(update, context)
    elif data == "change_button_row":
        return await reorder_rows_start(update, context)
    elif data.startswith("move_row_up_"):
        return await move_row_direction(update, context)
    elif data.startswith("move_row_down_"):
        return await move_row_direction(update, context)
    elif data.startswith("move_row_updown_"):
        return await select_row_to_reorder(update, context)
    elif data == "finish_reorder":
        return await finish_reorder_rows(update, context)
    elif data == "back_to_reorder_rows":
        return await back_to_reorder_rows(update, context)
    elif data.startswith("set_row_"):
        return await set_button_row(update, context)
    elif data == "finish_move":
        return await finish_move_buttons(update, context)
    elif data.startswith("back_to_move_"):
        return await back_to_move_button(update, context)
    elif data.startswith("select_move_"):
        return await select_button_to_move(update, context)
    elif data.startswith("move_btn_left_"):
        return await move_button_direction(update, context)
    elif data.startswith("move_btn_right_"):
        return await move_button_direction(update, context)
    elif data.startswith("change_row_"):
        return await change_button_row_start(update, context)

    if data == "move_buttons":
        return await move_buttons_start(update, context)

    if data.startswith("quick_timer_"):
        return await quick_timer_handler(update, context)
    elif data == "timer_custom_datetime":
        return await timer_custom_datetime_handler(update, context)
    elif data == "timer_add_more":
        # Позволяет поставить следующий таймер сразу после сохранения предыдущего,
        # без возврата в главное меню.
        try:
            await query.edit_message_text(
                "⏰ **Установка таймера**\n\nВыберите быстрый таймер или задайте своё время:",
                reply_markup=get_quick_timer_keyboard(),
                parse_mode="Markdown",
            )
        except Exception:
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="⏰ Установка таймера\n\nВыберите быстрый таймер или задайте своё время:",
                reply_markup=get_quick_timer_keyboard(),
            )
        return TIMER_SET_DATE

    if data == "change_time":
        return await change_time_start(update, context)
    elif data == "change_buttons":
        return await change_buttons_start(update, context)
    elif data == "change_layout":
        return await change_layout_start(update, context)
    elif data == "reorder_buttons":
        return await reorder_buttons_start(update, context)
    elif data == "birthday_settings":
        return await birthday_settings_start(update, context)
    elif data == "notification_settings":
        return await notification_settings_start(update, context)
    # === НОВОЕ: настройки погоды и праздников ===
    elif data == "weather_settings":
        return await weather_settings_start(update, context)
    elif data == "toggle_weather_notif":
        return await toggle_weather_notif(update, context)
    elif data == "set_weather_time":
        return await set_weather_time_start(update, context)
    elif data == "change_city":
        return await change_city_start(update, context)
    elif data == "weather_recalc_tz":
        return await weather_recalc_tz_handler(update, context)
    elif data == "holiday_settings":
        return await holiday_settings_start(update, context)
    elif data == "toggle_holidays_notif":
        return await toggle_holidays_notif(update, context)
    elif data == "weather_3days":
        return await weather_3days_handler(update, context)
    # === НОВОЕ: разработчик — праздники и быстрая рассылка ===
    elif data == "dev_set_holiday":
        return await dev_set_holiday_start(update, context)
    elif data == "dev_delete_holiday":
        return await dev_delete_holiday_start(update, context)
    elif data.startswith("dev_del_hol_"):
        return await dev_delete_holiday_handler(update, context)
    elif data == "dev_instant_broadcast":
        return await dev_instant_broadcast_start(update, context)
    elif data == "personal_buttons":
        return await personal_buttons_menu(update, context)
    elif data == "suggest_function":
        return await suggest_function_start(update, context)
    elif data == "toggle_notifications":
        return await toggle_notifications(update, context)
    elif data == "set_morning_time":
        return await set_morning_time_start(update, context)
    elif data == "set_evening_time":
        return await set_evening_time_start(update, context)
    elif data == "set_morning_text":
        return await set_morning_text_start(update, context)
    elif data == "set_evening_text":
        return await set_evening_text_start(update, context)
    elif data == "set_birthday":
        return await set_birthday_handler(update, context)
    elif data == "toggle_birthday_countdown":
        return await toggle_birthday_countdown(update, context)
    elif data == "toggle_birthday_class":
        return await toggle_birthday_class(update, context)
    elif data == "toggle_birthday_personal":
        return await toggle_birthday_personal(update, context)
    elif data == "set_birthday_notification_time":
        return await set_birthday_notification_time_start(update, context)
    elif data.startswith("lang_"):
        return await change_language_handler(update, context)
    elif data.startswith("layout_"):
        return await change_layout_handler(update, context)
    elif data.startswith("select_btn_"):
        return await select_button_to_rename(update, context)
    elif data.startswith("reorder_btn_"):
        return await reorder_button_handler(update, context)
    elif data.startswith("move_up_"):
        return await move_button_up_down(update, context)
    elif data.startswith("move_down_"):
        return await move_button_up_down(update, context)
    elif data == "finish_reorder_settings":
        return await finish_reorder_buttons(update, context)
    elif data.startswith("move_btn_"):
        return await move_buttons_handler(update, context)
    elif data == "move_to_end":
        return await move_to_position_handler(update, context)
    elif data.startswith("move_to_"):
        return await move_to_position_handler(update, context)
    elif data == "manage_visibility":
        return await manage_button_visibility_start(update, context)
    elif data.startswith("toggle_visibility_"):
        return await toggle_button_visibility_handler(update, context)
    elif data == "finish_visibility":
        return await finish_button_visibility(update, context)
    elif data == "back_to_settings":
        return await back_to_settings_handler(update, context)

    if data == "create_class":
        return await create_class_start(update, context)
    elif data == "join_class":
        return await join_class_start(update, context)
    elif data == "leave_class":
        return await leave_class_handler(update, context)
    elif data == "switch_class":
        return await join_class_start(update, context)

    if data == "dev_stats":
        return await dev_stats_handler(update, context)
    elif data == "dev_broadcast":
        return await dev_broadcast_start(update, context)
    elif data == "dev_class_message":
        return await dev_class_message_start(update, context)
    elif data == "dev_delete_class":
        return await dev_delete_class_start(update, context)
    elif data == "dev_user_management":
        return await dev_user_management_start(update, context)
    # ПУНКТ (цены): возвращены callback'и для быстрого изменения цен функций.
    elif data == "dev_quick_prices":
        return await dev_quick_prices_start(update, context)
    elif data.startswith("qprice_pick_"):
        return await dev_quick_price_pick_handler(update, context)
    elif data.startswith("qprice_d_"):
        return await dev_quick_price_delta_handler(update, context)
    elif data.startswith("qprice_set_"):
        return await dev_quick_price_set_start(update, context)
    elif data == "dev_back_panel":
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "🛠️ **Панель разработчика**\n\nВыберите действие:",
            reply_markup=get_developer_keyboard(),
            parse_mode="Markdown"
        )
        return DEV_PANEL
    elif data == "dev_edit_instructions":
        return await dev_edit_instructions_start(update, context)
    elif data == "dev_message_user":
        return await dev_message_user_start(update, context)
    elif data.startswith("dev_msg_pick_"):
        return await dev_message_user_pick_handler(update, context)
    elif data == "dev_global_button":
        return await dev_global_button_start(update, context)
    elif data == "dev_delete_global_button":
        return await dev_delete_global_button_start(update, context)
    elif data.startswith("dev_del_global_"):
        return await dev_delete_global_button_handler(update, context)
    elif data == "dev_toggle_new_user_notify":
        # Тоггл «Уведомления разработчику о новых пользователях».
        new_state = toggle_dev_new_user_notifications()
        await update.callback_query.answer(
            "Уведомления о новых пользователях: " + ("включены" if new_state else "выключены"),
            show_alert=False,
        )
        try:
            await update.callback_query.edit_message_reply_markup(
                reply_markup=get_developer_keyboard()
            )
        except Exception:
            # Если не удалось обновить только клавиатуру (например, текст отсутствует) —
            # перерисовываем всю панель.
            await update.callback_query.edit_message_text(
                "🛠️ **Панель разработчика**\n\nВыберите действие:",
                reply_markup=get_developer_keyboard(),
                parse_mode="Markdown"
            )
        return DEV_PANEL
    elif data.startswith("dev_class_msg_"):
        return await dev_class_message_select(update, context)
    elif data.startswith("dev_delete_class_"):
        return await dev_delete_class_handler(update, context)
    elif data == "dev_block_user":
        return await dev_block_user_start(update, context)
    elif data.startswith("dev_block_"):
        return await dev_block_user_handler(update, context)
    elif data == "dev_unblock_user":
        return await dev_unblock_user_start(update, context)
    elif data.startswith("dev_unblock_"):
        return await dev_unblock_user_handler(update, context)

    if data == "send_class_message":
        return await send_class_message_start(update, context)
    elif data == "edit_schedule":
        return await edit_schedule_start(update, context)
    elif data == "edit_teachers":
        return await edit_teachers_start(update, context)
    elif data == "edit_bells":
        return await edit_bells_start(update, context)
    elif data == "set_holidays":
        return await set_holidays_start(update, context)
    elif data == "manage_admins":
        return await manage_admins_start(update, context)
    elif data == "manage_custom_buttons":
        return await manage_custom_buttons_start(update, context)
    elif data == "manage_homework":
        return await manage_homework_start(update, context)
    elif data == "manage_class_users":
        return await manage_class_users_start(update, context)
    elif data == "show_class_code_admin":
        return await show_class_code_admin(update, context)
    elif data == "back_to_admin":
        return await back_to_admin_panel(update, context)
    elif data == "back_to_main":
        return await back_to_main_menu(update, context)
    elif data == "back_to_personal_buttons":
        return await back_to_personal_buttons(update, context)
    elif data == "back_to_manage_personal_buttons":
        return await manage_personal_buttons_start(update, context)
    elif data == "back_to_custom_buttons":
        return await manage_custom_buttons_start(update, context)
    elif data.startswith("edit_schedule_day_"):
        return await edit_schedule_day_handler(update, context)
    elif data.startswith("edit_teacher_"):
        return await edit_teacher_handler(update, context)
    elif data.startswith("edit_bell_"):
        return await edit_bell_handler(update, context)
    elif data.startswith("add_admin_"):
        return await add_admin_handler(update, context)
    elif data.startswith("remove_admin_"):
        return await remove_admin_handler(update, context)
    elif data == "add_teacher_subject":
        return await add_teacher_subject_start(update, context)
    elif data == "delete_teacher_list":
        return await delete_teacher_list_start(update, context)
    elif data.startswith("del_teacher_"):
        return await delete_teacher_handler(update, context)
    elif data == "add_custom_button":
        return await add_custom_button_start(update, context)
    elif data == "admin_delete_buttons":
        return await admin_delete_buttons_start(update, context)
    elif data.startswith("admin_delete_button_"):
        return await admin_delete_button_handler(update, context)
    elif data == "add_homework":
        return await add_homework_start(update, context)
    elif data == "delete_homework":
        return await delete_homework_start(update, context)
    elif data == "view_homework":
        return await view_homework_handler(update, context)
    elif data == "hw_new_subject":
        return await hw_new_subject_start(update, context)
    elif data == "hw_delete_subject_list":
        return await hw_delete_subject_list(update, context)
    elif data.startswith("hw_del_subj_"):
        return await hw_delete_subject_handler(update, context)
    elif data.startswith("hw_date_"):
        return await hw_date_handler(update, context)
    elif data.startswith("hw_subject_"):
        return await homework_subject_handler(update, context)
    elif data.startswith("quick_hw_"):
        return await quick_homework_handler(update, context)
    elif data.startswith("preview_hw_"):
        return await preview_homework_item_handler(update, context)
    elif data.startswith("confirm_delete_hw_"):
        return await confirm_delete_homework_handler(update, context)
    elif data.startswith("delete_hw_"):
        return await delete_homework_item_handler(update, context)
    elif data.startswith("class_block_"):
        return await class_block_user_handler(update, context)
    elif data.startswith("class_unblock_"):
        return await class_unblock_user_handler(update, context)
    elif data == "button_for_personal":
        return await admin_button_for_personal(update, context)
    elif data == "button_for_class":
        return await admin_button_for_class(update, context)
    elif data == "confirm_logout":
        return await confirm_logout_handler(update, context)
    elif data == "show_class_code":
        return await show_class_code_handler(update, context)
    elif data == "accept_disclaimer":
        return await disclaimer_handler(update, context)
    elif data == "decline_disclaimer":
        return await disclaimer_handler(update, context)
    elif data == "instructions_read":
        return await instructions_read_handler(update, context)
    elif data == "cancel_anon_reply":
        return await cancel_anon_reply_handler(update, context)
    elif data == "cancel_anon":
        return await cancel_anon_handler(update, context)
    elif data == "cancel_action":
        return await cancel_action_handler(update, context)
    elif data == "no_action":
        await query.answer("Нет действия")
        return MAIN_MENU
    elif data == "limit_reached":
        await query.answer("Достигнут лимит администраторов (2)")
        return MAIN_MENU
    elif data == "blocked_header" or data == "active_header" or data == "admin_header" or data == "no_blocked" or data == "no_active":
        await query.answer()
        return MAIN_MENU
    elif data == "subjects_header":
        await query.answer()
        return MAIN_MENU

    if data.startswith("button_type_"):
        if context.user_data.get('creating_global_button'):
            return await dev_global_button_type_handler(update, context)
        elif context.user_data.get('creating_personal_button'):
            return await personal_button_type_handler(update, context)
        else:
            return await button_type_handler(update, context)

    # === НОВЫЕ ВОЗМОЖНОСТИ (поддержка, рефералы, dev grant) ===
    # ПУНКТ 3: чат поддержки (пользовательская сторона).
    if data == "open_support_chat":
        return await open_support_chat(update, context)
    # ПУНКТ 2: реферальная система — поделиться ссылкой.
    elif data == "referral_share":
        return await referral_share_handler(update, context)
    # ПУНКТ 4: разработчик начисляет звёзды.
    elif data == "dev_grant_stars":
        return await dev_grant_stars_start(update, context)
    elif data.startswith("dev_grant_pick_"):
        return await dev_grant_stars_pick_handler(update, context)
    # ПУНКТ 3 (саппорт, dev): входящие чата поддержки и ответы.
    elif data == "dev_support_inbox":
        return await dev_support_inbox_handler(update, context)
    elif data.startswith("dev_support_reply_"):
        return await dev_support_reply_start(update, context)

    return MAIN_MENU

async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    await show_main_menu(update, context, user)
    return MAIN_MENU

async def back_to_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    class_code = context.user_data.get('current_admin_class')
    if not class_code:
        await query.edit_message_text("Класс не выбран.")
        return MAIN_MENU

    class_obj = get_class_by_code(class_code)
    if not class_obj:
        await query.edit_message_text("Класс не найден.")
        return MAIN_MENU

    text = f"👨‍💼 Админская панель класса '{class_obj.class_name}'\n\nВыберите действие:"

    await query.edit_message_text(text, reply_markup=get_admin_panel_keyboard())
    return ADMIN_PANEL

async def back_to_personal_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    buttons_count = get_personal_buttons_count(user_id)

    text = f"🌟 **Мои личные кнопки**\n\n📊 Количество: {buttons_count}/{user.max_personal_buttons}\n\n👇 *Выберите действие:*"

    await query.edit_message_text(text, reply_markup=get_personal_buttons_keyboard(user), parse_mode="Markdown")
    return PERSONAL_BUTTON_MANAGEMENT

async def cancel_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    temp_keys = [
        'timer_date', 'timer_time', 'homework_date', 'homework_subject',
        'awaiting_hw_text', 'awaiting_hw_custom_date', 'renaming_button',
        'anon_target', 'quick_admin_class', 'broadcast_text',
    ]
    for key in temp_keys:
        context.user_data.pop(key, None)

    await query.edit_message_text("Действие отменено.")
    return await show_main_menu(update, context, user)

async def personal_buttons_menu_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    buttons_count = get_personal_buttons_count(user_id)

    text = f"🌟 **Мои личные кнопки**\n\n📊 Количество: {buttons_count}/{user.max_personal_buttons}\n\n👇 *Выберите действие:*"

    await update.message.reply_text(text, reply_markup=get_personal_buttons_keyboard(user), parse_mode="Markdown")
    return PERSONAL_BUTTON_MANAGEMENT

async def cancel_anon_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("Отправка отменена.")
    return await back_to_main_menu(update, context)

async def cancel_anon_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data.pop('replying_to_anon', None)
    context.user_data.pop('anon_reply_to', None)

    await query.edit_message_text("Ответ отменён.")
    return MAIN_MENU

async def start_anon_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    msg_id = query.data.split("_")[2]

    anonymous_messages = load_data(ANONYMOUS_MESSAGES_FILE, {})
    msg = anonymous_messages.get(msg_id)

    if not msg:
        await query.edit_message_text("Сообщение не найдено.")
        return await view_anonymous_messages(update, context)

    context.user_data['replying_to_anon'] = True
    context.user_data['anon_reply_to'] = msg['from_user_id']

    await query.edit_message_text(
        "💬 Введите ваш ответ на анонимное сообщение:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_anon_reply")]])
    )
    return MAIN_MENU

async def send_anonymous_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    reply_text = (update.message.text or "").strip()
    to_user_id = context.user_data.get('anon_reply_to')

    if not to_user_id:
        await update.message.reply_text("Получатель не найден.")
        context.user_data.pop('replying_to_anon', None)
        context.user_data.pop('anon_reply_to', None)
        return MAIN_MENU

    if not reply_text:
        await update.message.reply_text(
            "Ответ пустой. Введите текст и отправьте снова."
        )
        return MAIN_MENU

    rejected = await reject_if_forbidden_chars(update, reply_text, MAIN_MENU)
    if rejected is not None:
        return rejected

    # chat_id обязательно int — иначе PTB иногда возвращает BadRequest
    # «Chat not found», даже если получатель валидный.
    try:
        to_chat_id = int(to_user_id)
    except (TypeError, ValueError):
        await update.message.reply_text("Некорректный получатель.")
        context.user_data.pop('replying_to_anon', None)
        context.user_data.pop('anon_reply_to', None)
        return MAIN_MENU

    try:
        await context.bot.send_message(
            chat_id=to_chat_id,
            text=f"💬 Ответ на ваше анонимное сообщение:\n\n{reply_text}",
        )

        await update.message.reply_text("✅ Ответ отправлен!")
    except TGForbidden as e:
        logger.warning(
            f"Анон-ответ не доставлен (Forbidden): from={user_id} "
            f"to={to_user_id}: {e}"
        )
        await update.message.reply_text(
            "Получатель заблокировал бота — ответ не доставлен."
        )
    except TGBadRequest as e:
        logger.warning(
            f"Анон-ответ не доставлен (BadRequest): from={user_id} "
            f"to={to_user_id}: {e}"
        )
        await update.message.reply_text(
            "Не удалось отправить ответ: чат с получателем не найден."
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа: {e}")
        await update.message.reply_text("Не удалось отправить ответ.")

    context.user_data.pop('replying_to_anon', None)
    context.user_data.pop('anon_reply_to', None)

    return MAIN_MENU

async def show_class_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    if user.class_code:
        class_obj = get_class_by_code(user.class_code)
        if class_obj:
            text = f"🔑 **Код вашего класса:**\n\n`{user.class_code}`\n\n🏫 Название: {class_obj.class_name}\n\nПоделитесь этим кодом с одноклассниками!"
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=get_back_button_keyboard())
        else:
            await query.edit_message_text("Класс не найден.", reply_markup=get_back_button_keyboard())
    else:
        await query.edit_message_text("Вы не состоите в классе.", reply_markup=get_back_button_keyboard())

    return MAIN_MENU


# ==================================
# === ПЛАНИРОВЩИК УВЕДОМЛЕНИЙ (JobQueue) ===
# ПУНКТЫ 5 и 6: таймер и утренние/вечерние уведомления
# ==================================

def _timer_message_text(timer_data):
    """Формирует текст уведомления таймера.

    kind="wish" (пожелание по расписанию: «спокойной ночи в 23:00») — бот
    присылает ЖИВУЮ фразу-пожелание без служебного префикса; обычный таймер —
    как раньше: «⏰ Напоминание: …».
    """
    text = timer_data.get('text', '⏰ Напоминание!') if isinstance(timer_data, dict) else str(timer_data)
    if isinstance(timer_data, dict) and str(timer_data.get('kind') or 'timer').strip().lower() == 'wish':
        return text
    return f"⏰ Напоминание:\n\n{text}"


def _timer_advance_daily(timer_data):
    """Повторяющийся таймер (repeat_daily=true): сдвигает target_date на
    следующий день, оставляя его активным. Возвращает True, если сдвинут.
    False — таймер одноразовый (его нужно деактивировать) или дата битая."""
    if not (isinstance(timer_data, dict) and timer_data.get('repeat_daily')):
        return False
    try:
        nd = datetime.strptime(str(timer_data.get('target_date')), "%Y-%m-%d") + timedelta(days=1)
        timer_data['target_date'] = nd.strftime("%Y-%m-%d")
        timer_data.pop('fired_at', None)
        return True
    except (TypeError, ValueError):
        return False


async def _send_timer_notification(context: ContextTypes.DEFAULT_TYPE):
    """Колбэк JobQueue для отправки таймер-уведомления."""
    job_data = context.job.data or {}
    user_id = job_data.get('user_id')
    text = job_data.get('text', '⏰ Напоминание!')
    timer_id = job_data.get('timer_id')
    # ЗАЩИТА ОТ ДУБЛЯ: если таймер уже отметили как неактивный (например,
    # safety-net его уже отправил, или это повторный запуск джобы), —
    # повторно НЕ шлём уведомление, чтобы пользователь не получал
    # одно и то же напоминание дважды.
    timer_record = None
    try:
        timers_check = load_data(TIMERS_FILE, {})
        if timer_id and timer_id in timers_check:
            timer_record = timers_check[timer_id]
            if not timer_record.get('is_active', True):
                logger.info(
                    f"Таймер {timer_id} уже неактивен — пропускаем повторную отправку."
                )
                return
            # Дубль-защита для повторяющихся таймеров: если эта джоба была
            # запланирована на ДРУГУЮ дату, чем текущая target_date — таймер
            # уже сработал и был перенесён (safety-net/тикер), а джоба
            # устарела. Отправлять второй раз НЕ нужно.
            job_date = job_data.get('target_date')
            current_date = timer_record.get('target_date')
            if job_date and current_date and str(job_date) != str(current_date):
                logger.info(
                    f"Таймер {timer_id}: джоба на {job_date} устарела (актуально {current_date}) — пропуск."
                )
                return
    except Exception:
        # Если не получилось прочитать файл — лучше попытаться отправить,
        # чем потерять уведомление.
        pass

    # kind="wish": присылаем живое пожелание вместо «⏰ Напоминание».
    if timer_record is not None:
        msg = _timer_message_text(timer_record)
    else:
        msg = f"⏰ Напоминание:\n\n{text}"
    try:
        # Отправляем без Markdown, чтобы случайные спецсимволы в тексте
        # напоминания не роняли отправку и уведомление всегда доходило.
        await context.bot.send_message(
            chat_id=int(user_id),
            text=msg,
        )
        logger.info(f"Таймер {timer_id} -> {user_id}: уведомление отправлено.")
    except Exception as e:
        logger.error(f"Не удалось отправить таймер-уведомление {timer_id} -> {user_id}: {e}")
    # Помечаем таймер как выполненный — либо переносим на завтра (repeat_daily).
    try:
        timers = load_data(TIMERS_FILE, {})
        if timer_id and timer_id in timers:
            td = timers[timer_id]
            if _timer_advance_daily(td):
                timers[timer_id] = td
                save_data(TIMERS_FILE, timers)
                # Планируем срабатывание на завтра (страховка: тикер/safety-net
                # подхватят, даже если джоба не сохранится).
                try:
                    schedule_timer_job(context.application, timer_id, td)
                    logger.info(
                        f"Повторяющийся таймер {timer_id} перенесён на {td.get('target_date')}."
                    )
                except Exception as e:
                    logger.error(f"Перепланирование повторяющегося таймера {timer_id}: {e}")
            else:
                td['is_active'] = False
                td['fired_at'] = datetime.now().strftime("%Y-%m-%d %H:%M")
                timers[timer_id] = td
                save_data(TIMERS_FILE, timers)
    except Exception as e:
        logger.error(f"Ошибка при пометке таймера {timer_id} как выполненного: {e}")


async def _timer_safety_net(context: ContextTypes.DEFAULT_TYPE):
    """Периодический «страховочный» обход всех активных таймеров.

    Зачем: иногда run_once-джоба может не сработать (бот перезапустился ровно
    в момент срабатывания, JobQueue не успел подхватить таймер из файла,
    APScheduler уронил конкретный job и т. п.). Без safety-net пользователь
    тогда вообще не получит напоминание — оно просто потеряется.

    Что делает:
    - Загружает все таймеры из TIMERS_FILE.
    - Для каждого активного смотрит, не наступило ли уже его время в UTC
      (с учётом tz пользователя).
    - Если наступило (с допуском в пару секунд) — отправляет уведомление
      ОДИН раз и помечает таймер is_active=False.
    - Если ещё не наступило — ничего не трогает (живой run_once отработает).

    Ничего НЕ ломает: дубль-защита в _send_timer_notification + общий флаг
    is_active гарантируют, что уведомление уйдёт ровно один раз.
    """
    try:
        timers = load_data(TIMERS_FILE, {})
        if not timers:
            return
        now_utc = datetime.utcnow()
        changed = False
        for timer_id, timer_data in list(timers.items()):
            try:
                if not timer_data.get('is_active'):
                    continue
                date_str = timer_data.get('target_date')
                time_str = timer_data.get('target_time')
                user_id = timer_data.get('user_id')
                if not date_str or not time_str or not user_id:
                    continue
                try:
                    target_local = datetime.strptime(
                        f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
                    )
                except ValueError:
                    continue
                user = get_user(user_id)
                tz_offset = getattr(user, 'timezone', 3) if user else 3
                target_utc = target_local - timedelta(hours=tz_offset)
                # Срабатываем, только если время уже прошло.
                if (now_utc - target_utc).total_seconds() < 0:
                    continue
                # Помечаем активным = False ДО отправки, чтобы параллельный
                # run_once не отправил то же уведомление второй раз.
                # ДЛЯ ПОВТОРЯЮЩИХСЯ (repeat_daily): вместо деактивации —
                # сдвигаем дату на завтра и остаёмся активными.
                if _timer_advance_daily(timer_data):
                    timer_data['fired_at'] = datetime.now().strftime("%Y-%m-%d %H:%M")
                else:
                    timer_data['is_active'] = False
                    timer_data['fired_at'] = datetime.now().strftime("%Y-%m-%d %H:%M")
                timers[timer_id] = timer_data
                changed = True
                save_data(TIMERS_FILE, timers)

                msg = _timer_message_text(timer_data)
                try:
                    await context.bot.send_message(chat_id=int(user_id), text=msg)
                    logger.info(
                        f"safety-net: таймер {timer_id} отправлен пользователю {user_id}."
                    )
                except Exception as e:
                    logger.error(
                        f"safety-net: не удалось отправить таймер {timer_id} -> {user_id}: {e}"
                    )
            except Exception as e:
                logger.error(f"safety-net: ошибка обработки таймера {timer_id}: {e}")
        if changed:
            save_data(TIMERS_FILE, timers)
    except Exception as e:
        logger.error(f"safety-net общий сбой: {e}")


def schedule_timer_job(application, timer_id, timer_data):
    """Планирует одноразовое уведомление таймера.

    Если указанное время уже прошло (сервер перезапущен / таймер просрочен),
    ставим задачу на ближайшее время (через 5 секунд) — чтобы пользователь
    всё-таки получил уведомление, а не потерял его молча.
    """
    try:
        if not timer_data.get('is_active'):
            return
        date_str = timer_data.get('target_date')
        time_str = timer_data.get('target_time')
        user_id = timer_data.get('user_id')
        if not date_str or not time_str or not user_id:
            return
        # Учитываем часовой пояс пользователя
        user = get_user(user_id)
        tz_offset = user.timezone if user else 3
        target_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        # Переводим из локального времени пользователя в UTC
        when_utc = target_local - timedelta(hours=tz_offset)
        delay = (when_utc - datetime.utcnow()).total_seconds()
        # Если время уже прошло — отправляем почти сразу (через 5 сек),
        # чтобы пропущенное напоминание не терялось.
        if delay <= 0:
            delay = 5
        job_name = f"timer_{timer_id}"
        # Удаляем старые джобы с таким же именем
        for j in application.job_queue.get_jobs_by_name(job_name):
            j.schedule_removal()
        application.job_queue.run_once(
            _send_timer_notification,
            when=delay,
            data={
                'user_id': user_id,
                'text': timer_data.get('text', ''),
                'timer_id': timer_id,
                # Слепок даты, на которую запланирована джоба: если таймер
                # повторяющийся и его уже успели отправить/перенести через
                # safety-net/тикер, эта джоба устареет и НЕ отправит дубль.
                'target_date': timer_data.get('target_date'),
            },
            name=job_name
        )
        logger.info(f"Таймер {timer_id} запланирован через {int(delay)} сек.")
    except Exception as e:
        logger.error(f"Ошибка планирования таймера {timer_id}: {e}")


async def _send_morning_notification(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data or {}
    user_id = job_data.get('user_id')
    user = get_user(user_id)
    if not user or not user.notifications:
        return
    try:
        await context.bot.send_message(
            chat_id=int(user_id),
            text=f"☀️ {user.morning_text}"
        )
    except Exception as e:
        logger.error(f"Ошибка утреннего уведомления {user_id}: {e}")


async def _send_evening_notification(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data or {}
    user_id = job_data.get('user_id')
    user = get_user(user_id)
    if not user or not user.notifications:
        return
    try:
        await context.bot.send_message(
            chat_id=int(user_id),
            text=f"🌙 {user.evening_text}"
        )
    except Exception as e:
        logger.error(f"Ошибка вечернего уведомления {user_id}: {e}")


def _local_time_to_utc(time_str, tz_offset):
    """ЧЧ:ММ в локальном tz пользователя -> datetime.time в UTC.

    К результату принудительно прикрепляем tzinfo=UTC, иначе APScheduler
    внутри JobQueue может интерпретировать «наивный» time как локальное
    время сервера — и пользователю не придёт ни утреннее, ни вечернее,
    ни погодное, ни праздничное, ни ДР-уведомление, потому что джоба
    запланирована «не в то время».
    """
    try:
        h, m = map(int, time_str.split(":"))
    except Exception:
        return None
    # Конвертируем в UTC: utc = local - offset
    total = (h - tz_offset) * 60 + m
    total %= (24 * 60)
    return dt_time(hour=total // 60, minute=total % 60, tzinfo=timezone.utc)


def schedule_user_daily_jobs(application, user):
    """Совместимость с прежним API. Доставкой утренних/вечерних уведомлений
    занимается единый тикер `_unified_notification_tick` (раз в минуту).
    Здесь только удаляем устаревшие run_daily-джобы, чтобы не было дублей,
    если бот когда-то запускался по старой схеме."""
    try:
        if not user or not application or not application.job_queue:
            return
        morning_name = f"morning_{user.user_id}"
        evening_name = f"evening_{user.user_id}"
        for j in application.job_queue.get_jobs_by_name(morning_name):
            j.schedule_removal()
        for j in application.job_queue.get_jobs_by_name(evening_name):
            j.schedule_removal()
    except Exception as e:
        logger.error(f"Ошибка планирования daily jobs для {getattr(user, 'user_id', '?')}: {e}")


# ==================================
# === ПОЗДРАВЛЕНИЕ С ДНЁМ РОЖДЕНИЯ ===
# ==================================
# Каждый день в 09:00 по местному времени пользователя JobQueue запускает
# проверку: если сегодня у пользователя день рождения и он не отключил
# личные уведомления — бот присылает поздравление.

BIRTHDAY_GREETINGS = [
    "🎂 С Днём рождения, {name}! Желаем крепкого здоровья, ярких эмоций и осуществления всех желаний! 🎉",
    "🎈 С Днём рождения, {name}! Пусть этот год будет самым счастливым и запоминающимся! 🥳",
    "🎉 Поздравляем с Днём рождения, {name}! Удачи во всех начинаниях и много-много улыбок! 🎂",
]


def _days_until_birthday_for_user(user):
    """Сколько полных дней осталось до следующего дня рождения пользователя
    в его локальном часовом поясе. Возвращает None, если ДР не установлен.

    0 — день рождения сегодня.
    """
    if not user or not user.birthday:
        return None
    try:
        tz_offset = getattr(user, 'timezone', 3)
        local_today = (datetime.utcnow() + timedelta(hours=tz_offset)).date()
        birthday = datetime.strptime(user.birthday, "%Y-%m-%d").date()
        next_birthday = birthday.replace(year=local_today.year)
        if next_birthday < local_today:
            next_birthday = next_birthday.replace(year=local_today.year + 1)
        return (next_birthday - local_today).days
    except Exception:
        return None


def _format_days_word(n):
    """Корректно склоняет «день/дня/дней» для числа n."""
    n_abs = abs(int(n))
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return "день"
    if n_abs % 10 in (2, 3, 4) and n_abs % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


async def _send_birthday_notification(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневная проверка: каждый день — отправляем пользователю, сколько
    осталось до его дня рождения. В сам день ДР — отправляем поздравление.

    Уважаем настройку user.birthday_personal_notification: если выключена —
    ничего не шлём.
    """
    job_data = context.job.data or {}
    user_id = job_data.get('user_id')
    if not user_id:
        return
    user = get_user(user_id)
    if not user or not getattr(user, 'birthday_personal_notification', True):
        return
    if not user.birthday:
        return
    try:
        tz_offset = getattr(user, 'timezone', 3)
        local_today = (datetime.utcnow() + timedelta(hours=tz_offset)).date()
        try:
            birthday = datetime.strptime(user.birthday, "%Y-%m-%d").date()
        except Exception:
            return

        is_birthday_today = (
            (birthday.month, birthday.day) == (local_today.month, local_today.day)
        )

        if is_birthday_today:
            # Поздравление в сам день рождения.
            years = local_today.year - birthday.year
            if (local_today.month, local_today.day) < (birthday.month, birthday.day):
                years -= 1
            import random as _rnd
            greeting = _rnd.choice(BIRTHDAY_GREETINGS).format(
                name=user.first_name or "друг"
            )
            message = greeting
            if years >= 1:
                message += f"\n\n🎁 Тебе сегодня исполняется {years}!"
            await context.bot.send_message(chat_id=int(user_id), text=message)
            return

        # Не день рождения — считаем, сколько осталось, и отправляем напоминание.
        days_left = _days_until_birthday_for_user(user)
        if days_left is None or days_left <= 0:
            return
        word = _format_days_word(days_left)
        message = (
            f"🎂 До твоего дня рождения осталось {days_left} {word}!\n"
            f"📅 Дата: {user.birthday}"
        )
        await context.bot.send_message(chat_id=int(user_id), text=message)
    except Exception as e:
        logger.error(f"Ошибка ежедневного уведомления о ДР для {user_id}: {e}")


def schedule_user_birthday_job(application, user):
    """Совместимость с прежним API. Доставкой ежедневного ДР-напоминания
    занимается единый тикер `_unified_notification_tick`. Здесь только
    удаляем устаревшие run_daily-джобы, чтобы не было дублей."""
    try:
        if not user or not application or not application.job_queue:
            return
        name = f"birthday_{user.user_id}"
        for j in application.job_queue.get_jobs_by_name(name):
            j.schedule_removal()
    except Exception as e:
        logger.error(
            f"schedule_user_birthday_job cleanup error: {e}"
        )


async def _self_ping_job(context: ContextTypes.DEFAULT_TYPE):
    """Периодически пингует keep-alive сервер, чтобы бот не засыпал."""
    port = int(os.environ.get('PORT', 8080))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://localhost:{port}/health", timeout=aiohttp.ClientTimeout(total=10)):
                pass
    except Exception:
        pass
    # Пингуем внешний URL если он задан (Render / Railway)
    ext_url = os.environ.get('RENDER_EXTERNAL_URL') or os.environ.get('RAILWAY_STATIC_URL')
    if ext_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{ext_url}/health", timeout=aiohttp.ClientTimeout(total=10)):
                    pass
        except Exception:
            pass


# ==================================
# === ПОГОДА (WeatherAPI) ===
# ==================================
# Все функции ниже добавлены для нового функционала «Погода» и
# «Праздники». Существующая логика бота при этом не меняется.

# Дефолтные праздники, которые предзаполняются в HOLIDAYS_FILE при первом
# запуске. Ключ — дата в формате "ДД-ММ" (год не учитывается, праздник
# повторяется ежегодно), значение — список названий праздников на эту дату.
DEFAULT_HOLIDAYS = {
    "01-01": ["🎄 Новый год"],
    "07-01": ["✝️ Рождество Христово"],
    "14-02": ["💝 День святого Валентина"],
    "23-02": ["🎖 День защитника Отечества"],
    "08-03": ["🌷 Международный женский день"],
    "01-04": ["😄 День смеха"],
    "12-04": ["🚀 День космонавтики"],
    "01-05": ["💼 Праздник Весны и Труда"],
    "09-05": ["🏆 День Победы"],
    "01-06": ["🧒 Международный день защиты детей"],
    "12-06": ["🇷🇺 День России"],
    "01-09": ["📚 День знаний"],
    "05-10": ["👨‍🏫 День учителя"],
    "04-11": ["🤝 День народного единства"],
    "31-12": ["🎉 Канун Нового года"],
}


def load_holidays():
    """Загружает праздники из файла. При первом запуске создаёт файл с
    дефолтным набором праздников (DEFAULT_HOLIDAYS)."""
    data = load_data(HOLIDAYS_FILE, None)
    if data is None or not isinstance(data, dict):
        # Первый запуск — кладём дефолты в файл, чтобы их сразу можно было
        # править/удалять через панель разработчика.
        save_data(HOLIDAYS_FILE, DEFAULT_HOLIDAYS)
        # Возвращаем КОПИЮ, чтобы внешний код мог её править без побочных эффектов.
        return {k: list(v) for k, v in DEFAULT_HOLIDAYS.items()}
    # На случай, если кто-то сохранил значение строкой вместо списка —
    # приводим к списку для единообразия.
    fixed = {}
    for k, v in data.items():
        if isinstance(v, list):
            fixed[k] = list(v)
        elif isinstance(v, str):
            fixed[k] = [v]
        else:
            fixed[k] = []
    return fixed


def save_holidays(holidays):
    """Сохраняет праздники в файл."""
    save_data(HOLIDAYS_FILE, holidays)


async def _weather_api_get(endpoint, params):
    """Низкоуровневый запрос к WeatherAPI. Возвращает JSON или None при ошибке."""
    if not WEATHER_API_KEY:
        return None
    url = f"https://api.weatherapi.com/v1/{endpoint}"
    full_params = {"key": WEATHER_API_KEY, "lang": "ru"}
    full_params.update(params)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=full_params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"WeatherAPI {endpoint} вернул статус {resp.status} "
                        f"для параметров {params}"
                    )
                    return None
                return await resp.json()
    except Exception as e:
        logger.error(f"Ошибка запроса к WeatherAPI ({endpoint}): {e}")
        return None


async def weather_validate_city(city):
    """Проверяет, существует ли город. Возвращает (ok, canonical_name).

    Совместимость со старым API сохранена.
    """
    ok, name, _ = await weather_validate_city_full(city)
    return ok, name


async def weather_validate_city_full(city):
    """Расширенная проверка города. Возвращает (ok, canonical_name, location_dict).

    location_dict — это поле «location» из ответа WeatherAPI и содержит, в
    частности, ключи tz_id, lat, lon, localtime — именно ими ПУНКТ 10
    пользуется, чтобы автоматически определить часовой пояс по городу
    без ручного ввода времени.
    """
    data = await _weather_api_get("current.json", {"q": city})
    if not data or "location" not in data:
        return False, None, None
    loc = data["location"]
    name = loc.get("name") or city
    country = loc.get("country") or ""
    canonical = f"{name}, {country}" if country else name
    return True, canonical, loc


def _normalise_tz_offset(offset_hours):
    """Приводит смещение UTC (в часах, float) к формату бота: целые — int,
    дробные (5.5, 5.75) — float с точностью до 4 знаков. None при браке."""
    try:
        v = round(float(offset_hours), 4)
    except (TypeError, ValueError):
        return None
    if v == 0.0:
        v = 0.0  # убираем -0.0
    if not (-12.0 <= v <= 14.0):
        return None
    return int(v) if v == int(v) else v


def _fmt_tz_offset(value):
    """Форматирует смещение UTC для текстов: 3 → «+3», 5.5 → «+5:30»,
    -3.75 → «-3:45». Никогда не падает (fallback +3)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 3.0
    sign = "+" if v >= 0 else "-"
    v = abs(v)
    hours = int(v)
    minutes = int(round((v - hours) * 60))
    if minutes == 60:
        hours += 1
        minutes = 0
    if minutes:
        return f"{sign}{hours}:{minutes:02d}"
    return f"{sign}{hours}"


async def detect_timezone_for_city(city):
    """ПУНКТ 10: вернуть смещение пользователя относительно UTC (в часах)
    для указанного города.

    ИСПРАВЛЕНО (честно и точно, без привязки к таймзоне сервера):
      1) ГЛАВНЫЙ путь: WeatherAPI возвращает tz_id («Europe/Moscow») —
         смещение считается ОФФЛАЙН через stdlib zoneinfo (IANA tzdata)
         ровно на текущий момент, с учётом DST и получасовых/четвертных
         поясов (UTC+5:30 Индия, UTC+5:45 Непал, UTC+9:30 Аделаида).
         Раньше: смещение считалось как localtime_epoch минус
         _now_utc().timestamp(), а у наивного datetime .timestamp()
         интерпретирует время в таймзоне СЕРВЕРА — на хостинге с TZ != UTC
         ответ был смещён на разницу поясов (например, на сервере UTC+3
         Москва определялась как UTC+0). Теперь эта ошибка исключена.
      2) fallback: разность epoch (с CORРЕКТНЫМ aware-UTC datetime.now(timezone.utc));
      3) fallback: World Time API (worldtimeapi.org/api/timezone/<tz_id>);
      4) при полном фейле — UTC+3 (дефолт пользователя).

    Возвращаемое значение — число (int для целых поясов, float для дробных);
    timedelta(hours=...) корректно принимает дробные значения, так что вся
    существующая математика времени бота продолжает работать.
    """
    # 1. WeatherAPI: получаем location и tz_id.
    data = await _weather_api_get("current.json", {"q": city})
    tz_id = None
    if data and "location" in data:
        loc = data["location"]
        tz_id = loc.get("tz_id")

        if tz_id and ZoneInfo is not None:
            # ГЛАВНЫЙ ПУТЬ: точный офлайн-расчёт по IANA-имени пояса.
            try:
                now_utc_aware = datetime.now(timezone.utc)
                local_aware = now_utc_aware.astimezone(ZoneInfo(tz_id))
                offset_seconds = local_aware.utcoffset().total_seconds()
                result = _normalise_tz_offset(offset_seconds / 3600.0)
                if result is not None:
                    return result
            except Exception as e:
                logger.warning(f"detect_timezone_for_city: zoneinfo({tz_id}) fail: {e}")

        # Fallback 2: epoch-оценка. ВАЖНО: datetime.now(timezone.utc).timestamp()
        # даёт корректный epoch на любой машине независимо от таймзоны сервера.
        epoch_local = loc.get("localtime_epoch")
        if epoch_local:
            try:
                utc_now_epoch = datetime.now(timezone.utc).timestamp()
                offset_hours = (float(epoch_local) - utc_now_epoch) / 3600.0
                result = _normalise_tz_offset(offset_hours)
                if result is not None:
                    return result
            except Exception as e:
                logger.warning(
                    f"detect_timezone_for_city: WeatherAPI epoch fail: {e}"
                )
        else:
            # Fallback: строковый localtime (старое поведение, но с корректным UTC).
            local_str = loc.get("localtime")
            if local_str:
                try:
                    local_dt = datetime.strptime(local_str, "%Y-%m-%d %H:%M")
                    diff_hours = (local_dt - _now_utc()).total_seconds() / 3600.0
                    result = _normalise_tz_offset(diff_hours)
                    if result is not None:
                        return result
                except Exception as e:
                    logger.warning(
                        f"detect_timezone_for_city: WeatherAPI localtime parse fail: {e}"
                    )

    # 3. World Time API (Time.Now) — если знаем tz_id.
    if tz_id:
        try:
            url = f"https://worldtimeapi.org/api/timezone/{tz_id}"
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        wt = await resp.json()
                        # raw_offset — секунды от UTC без DST,
                        # dst_offset — секунды DST, нам нужна сумма / 3600.
                        raw = int(wt.get("raw_offset", 0) or 0)
                        dst = int(wt.get("dst_offset", 0) or 0)
                        result = _normalise_tz_offset((raw + dst) / 3600.0)
                        if result is not None:
                            return result
        except Exception as e:
            logger.warning(f"detect_timezone_for_city: World Time API fail: {e}")

    # 3. Fallback.
    return 3


def _kph_to_ms(wind_kph):
    """Переводит скорость ветра из км/ч в м/с (1 км/ч ≈ 0.2778 м/с).

    Возвращает float. Не падает на None/невалидных значениях — возвращает 0.0.
    """
    try:
        return float(wind_kph or 0) / 3.6
    except (TypeError, ValueError):
        return 0.0


def _clothing_advice(temp_c, condition_text="", wind_kph=0, will_rain=False, will_snow=False):
    """Возвращает короткие текстовые советы как одеться по погоде."""
    advice_lines = []
    try:
        t = float(temp_c)
    except (TypeError, ValueError):
        t = 15.0
    if t >= 28:
        advice_lines.append("☀️ Очень жарко: лёгкая футболка, шорты, головной убор, побольше воды.")
    elif t >= 22:
        advice_lines.append("☀️ Тепло: футболка/рубашка, лёгкие штаны или шорты.")
    elif t >= 16:
        advice_lines.append("👕 Комфортно: футболка/лонгслив, лёгкие штаны.")
    elif t >= 10:
        advice_lines.append("🧥 Прохладно: лонгслив + лёгкая куртка/толстовка.")
    elif t >= 3:
        advice_lines.append("🧥 Холодновато: куртка, кофта, закрытая обувь.")
    elif t >= -5:
        advice_lines.append("🧥 Холодно: тёплая куртка/пуховик, шапка, перчатки желательны.")
    elif t >= -15:
        advice_lines.append("🥶 Морозно: пуховик, шапка, шарф, перчатки.")
    else:
        advice_lines.append("🥶 Сильный мороз: пуховик, термобельё, шапка, шарф, варежки.")

    cond = (condition_text or "").lower()
    rain_keywords = ("дожд", "ливен", "ливн", "морос", "rain", "drizzle", "shower")
    snow_keywords = ("снег", "снеж", "метел", "snow", "blizzard", "sleet")
    if will_rain or any(k in cond for k in rain_keywords):
        advice_lines.append("☔ Возьмите зонт или дождевик.")
    if will_snow or any(k in cond for k in snow_keywords):
        advice_lines.append("❄️ Тёплая непромокаемая обувь — снег или гололёд.")
    try:
        # 30 км/ч ≈ 8.3 м/с — порог сильного ветра.
        if float(wind_kph or 0) >= 30:
            advice_lines.append("💨 Сильный ветер — оденьтесь плотнее, ветровка не помешает.")
    except (TypeError, ValueError):
        pass
    return "\n".join(advice_lines)


async def weather_current_text(city):
    """Возвращает готовый текст с текущей погодой и советами по одежде."""
    data = await _weather_api_get("current.json", {"q": city})
    if not data or "current" not in data:
        return f"❌ Не удалось получить погоду для города «{city}». Проверьте название в настройках."
    cur = data["current"]
    loc = data["location"]
    temp = cur.get("temp_c", 0)
    feels = cur.get("feelslike_c", temp)
    cond_block = cur.get("condition") or {}
    cond = cond_block.get("text", "")
    wind = cur.get("wind_kph", 0)
    humid = cur.get("humidity", 0)
    advice = _clothing_advice(temp, cond, wind)
    city_name = loc.get("name", city)
    country = loc.get("country", "")
    location_str = f"{city_name}, {country}" if country else city_name
    wind_ms = _kph_to_ms(wind)
    return (
        f"🌦 Погода сейчас в {location_str}:\n\n"
        f"🌡 Температура: {temp:+.0f}°C (ощущается как {feels:+.0f}°C)\n"
        f"☁️ {cond}\n"
        f"💨 Ветер: {wind_ms:.1f} м/с\n"
        f"💧 Влажность: {humid}%\n\n"
        f"👔 Что надеть:\n{advice}"
    )


async def weather_forecast_text(city, days=3):
    """Возвращает прогноз на следующие N дней (без сегодняшнего).

    Free-план WeatherAPI отдаёт максимум 3 дня прогноза (сегодня + 2),
    поэтому запрашиваем не больше 3 — раньше запрос 4 дней мог возвращать
    ошибку API и прогноз «не работал».
    """
    # Запрашиваем days+1 дней, чтобы отбросить сегодняшний день и взять следующие.
    api_days = min(max(days + 1, 2), 3)
    data = await _weather_api_get("forecast.json", {"q": city, "days": api_days})
    if not data or "forecast" not in data:
        return f"❌ Не удалось получить прогноз для города «{city}»."
    loc = data["location"]
    city_name = loc.get("name", city)
    country = loc.get("country", "")
    location_str = f"{city_name}, {country}" if country else city_name
    fdays = data["forecast"].get("forecastday", []) or []
    # Убираем сегодняшний день, берём следующие `days`.
    # ИСПРАВЛЕНО: «сегодня» — это дата в ГОРОДЕ (loc.localtime), а не на сервере.
    # Раньше datetime.now() брал дату сервера, и для города с другим поясом
    # в прогноз попадал «вчерашний/сегодняшний» день — «погода на завтра»
    # показывала не тот день.
    today_str = None
    loc_localtime = str(loc.get("localtime") or "")
    if len(loc_localtime) >= 10:
        today_str = loc_localtime[:10]
    if not today_str:
        today_str = datetime.now().strftime("%Y-%m-%d")
    upcoming = [d for d in fdays if d.get("date") != today_str]
    upcoming = upcoming[:days]
    if not upcoming:
        upcoming = fdays[-days:] if days else []
    parts = [f"📅 Прогноз погоды в {location_str} на {len(upcoming)} дн.:"]
    for d in upcoming:
        date = d.get("date", "")
        day = d.get("day", {}) or {}
        cond_block = day.get("condition") or {}
        cond = cond_block.get("text", "")
        tmin = day.get("mintemp_c", 0)
        tmax = day.get("maxtemp_c", 0)
        will_rain = bool(day.get("daily_will_it_rain", 0))
        will_snow = bool(day.get("daily_will_it_snow", 0))
        wind = day.get("maxwind_kph", 0)
        try:
            avg_temp = (float(tmin) + float(tmax)) / 2
        except (TypeError, ValueError):
            avg_temp = tmax or tmin or 0
        advice = _clothing_advice(avg_temp, cond, wind, will_rain=will_rain, will_snow=will_snow)
        wind_ms = _kph_to_ms(wind)
        parts.append(
            f"\n📆 {date}\n"
            f"🌡 {float(tmin):+.0f}…{float(tmax):+.0f}°C, {cond}\n"
            f"💨 ветер до {wind_ms:.1f} м/с\n"
            f"👔 Что надеть:\n{advice}"
        )
    return "\n".join(parts)


# ==================================
# === ХЕНДЛЕРЫ: ПОГОДА В ГЛАВНОМ МЕНЮ ===
# ==================================

async def send_weather_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE, user):
    """Реакция на нажатие кнопки «🌦 Погода» в главном меню."""
    if not getattr(user, 'city', None):
        await update.message.reply_text(
            "🏙 Город не указан.\n\n"
            "Установите его в ⚙️ Настройки → 🌦 Настройки погоды → 🏙 Город."
        )
        return
    text = await weather_current_text(user.city)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Узнать на 3 дня", callback_data="weather_3days")]
    ])
    await update.message.reply_text(text, reply_markup=keyboard)


async def weather_3days_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline-кнопка под текущей погодой: показать прогноз на 3 дня."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user or not getattr(user, 'city', None):
        try:
            await query.message.reply_text(
                "🏙 Город не установлен. Откройте ⚙️ Настройки → 🌦 Настройки погоды."
            )
        except Exception:
            pass
        return MAIN_MENU
    text = await weather_forecast_text(user.city, days=3)
    try:
        await query.message.reply_text(text)
    except Exception as e:
        logger.error(f"Ошибка показа прогноза 3 дней: {e}")
    return MAIN_MENU


# ==================================
# === ХЕНДЛЕРЫ: ГОРОД (РЕГИСТРАЦИЯ И НАСТРОЙКИ) ===
# ==================================

@timeout(CONVERSATION_TIMEOUT)
async def enter_city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ПУНКТ 10: получение города при первичной регистрации.

    Ручной ввод времени убран — вместо него по введённому городу
    автоматически определяется часовой пояс (через WeatherAPI
    `location.localtime`, при ошибке — через World Time API). После этого
    регистрация считается завершённой (`setup_completed = True`).
    """
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    city_input = update.message.text.strip()

    # Проверяем на запрещённые символы — соблюдаем общую политику бота.
    err = await reject_if_forbidden_chars(update, city_input, ENTER_CITY)
    if err:
        return err

    ok, canonical, _loc = await weather_validate_city_full(city_input)
    if not ok:
        await update.message.reply_text(
            f"❌ Не удалось найти город «{city_input}». Проверьте написание и попробуйте снова:",
            reply_markup=get_cancel_keyboard()
        )
        return ENTER_CITY

    user.city = canonical
    # Часовой пояс — ОТ ГОРОДА (Item #10).
    try:
        tz_offset = await detect_timezone_for_city(canonical)
    except Exception as e:
        logger.warning(f"detect_timezone_for_city failed: {e}")
        tz_offset = 3
    user.timezone = tz_offset
    # Время «настройки» нам больше не нужно вводить руками — фиксируем
    # «эталонное» 12:00 локального времени, чтобы не ломать функции,
    # завязанные на user.set_time / setup_completed.
    user.set_time = datetime.now().isoformat()
    user.setup_completed = True
    save_user(user)

    # ПУНКТ 5: реферер получает звёзды только когда новичок завершил регистрацию.
    # credit_referrer_for(new_user_id, referrer_id) — синхронная функция.
    try:
        ref_id = getattr(user, "referrer_id", None)
        already_paid = bool(getattr(user, "referral_bonus_paid", False))
        if ref_id and not already_paid:
            granted = credit_referrer_for(user.user_id, ref_id)
            if granted:
                # Обновляем in-memory копию пользователя из файла, так как
                # credit_referrer_for модифицирует и сохраняет users.json.
                refreshed = get_user(user_id)
                if refreshed:
                    user = refreshed
                # Пытаемся уведомить пригласившего о бонусе.
                try:
                    await context.bot.send_message(
                        chat_id=int(ref_id),
                        text=(
                            f"🎉 Ваш приглашённый пользователь зарегистрировался!\n"
                            f"+{REFERRAL_REWARD_STARS} ⭐ зачислены на ваш виртуальный баланс."
                        ),
                    )
                except Exception as e:
                    logger.warning(
                        f"Не удалось уведомить пригласившего {ref_id}: {e}"
                    )
    except Exception as e:
        logger.warning(f"credit_referrer_for failed for {user_id}: {e}")

    # Планируем погодное и праздничное уведомления для пользователя.
    try:
        schedule_user_weather_job(context.application, user)
        schedule_user_holiday_job(context.application, user)
    except Exception as e:
        logger.error(f"Не удалось запланировать погодные/праздничные джобы: {e}")

    sign = "+" if tz_offset >= 0 else "−"
    await update.message.reply_text(
        f"✅ Город установлен: {canonical}\n"
        f"🕒 Часовой пояс определён автоматически: UTC{sign}{abs(tz_offset)}\n\n"
        f"Теперь утром я буду присылать вам погоду и советы по одежде, "
        f"а также уведомления о праздниках и пожелания. "
        f"Время можно изменить в ⚙️ Настройки → 🌦 Настройки погоды."
    )
    await class_management(update, context)
    return CLASS_MANAGEMENT


async def weather_settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открыть подменю «🌦 Настройки погоды»."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    enabled = getattr(user, 'weather_notifications', False)
    weather_time = getattr(user, 'weather_notification_time', '07:00')
    city = getattr(user, 'city', None) or "не выбран"
    text = (
        f"🌦 **Настройки погоды**\n\n"
        f"⚠️ Ежедневная автоматическая погода ОТКЛЮЧЕНА.\n"
        f"Чтобы получить прогноз, нажмите кнопку «🌦 Погода» в главном меню.\n\n"
        f"Тоггл уведомлений (для совместимости): {'Включены' if enabled else 'Выключены'}\n"
        f"⏰ Время (только справочно): {weather_time}\n"
        f"🏙 Город: {city}\n\n"
        f"Выберите действие:"
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=get_weather_settings_keyboard(user),
            parse_mode="Markdown"
        )
    except Exception:
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=get_weather_settings_keyboard(user),
            parse_mode="Markdown"
        )
    return USER_SETTINGS


async def toggle_weather_notif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включить / выключить утренние погодные уведомления."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    user.weather_notifications = not getattr(user, 'weather_notifications', True)
    save_user(user)
    try:
        if user.weather_notifications:
            schedule_user_weather_job(context.application, user)
        else:
            for j in context.application.job_queue.get_jobs_by_name(f"weather_{user.user_id}"):
                j.schedule_removal()
    except Exception as e:
        logger.error(f"Ошибка обновления джоба погоды: {e}")
    return await weather_settings_start(update, context)


async def weather_recalc_tz_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """НОВОЕ: ручной пересчёт часового пояса по текущему городу.

    Исправляет ситуацию, когда у старого пользователя в БД лежит
    неверный user.timezone: бот заново определяет пояс через WeatherAPI
    и пересохраняет. После пересчёта рассылка погоды идёт в правильное
    локальное время."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    if not getattr(user, 'city', None):
        await query.edit_message_text(
            "🏙 Сначала укажите город: ⚙️ Настройки → 🌦 Настройки погоды → 🏙 Город.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="weather_settings")]]),
        )
        return USER_SETTINGS
    old_tz = getattr(user, 'timezone', 3)
    try:
        new_tz = await detect_timezone_for_city(user.city)
    except Exception as e:
        logger.warning(f"weather_recalc_tz: {e}")
        new_tz = old_tz
    user.timezone = new_tz
    save_user(user)
    sign = "+" if float(new_tz) >= 0 else "−"
    pretty = int(new_tz) if float(new_tz) == int(new_tz) else float(new_tz)
    await query.edit_message_text(
        f"✅ Часовой пояс пересчитан по городу «{user.city}»:\n"
        f"🧭 {old_tz} → UTC{sign}{abs(pretty)}\n\n"
        f"Утренняя погода теперь приходит ровно в {getattr(user, 'weather_notification_time', '07:00')} по вашему местному времени.",
        reply_markup=get_weather_settings_keyboard(user),
    )
    return USER_SETTINGS


async def set_weather_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Спросить время утреннего погодного уведомления."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "⏰ Введите время утреннего погодного уведомления в формате ЧЧ:ММ (например, 07:00):",
        reply_markup=get_cancel_keyboard()
    )
    return SET_WEATHER_TIME


@timeout(CONVERSATION_TIMEOUT)
async def save_weather_time_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранить введённое время утреннего погодного уведомления."""
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    time_str = update.message.text.strip()
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await update.message.reply_text(
            "Введите время в формате ЧЧ:ММ (например, 07:00):",
            reply_markup=get_cancel_keyboard()
        )
        return SET_WEATHER_TIME
    user.weather_notification_time = time_str
    save_user(user)
    try:
        # Перепланируем погоду И праздники (они привязаны к этому же времени).
        schedule_user_weather_job(context.application, user)
        schedule_user_holiday_job(context.application, user)
    except Exception as e:
        logger.error(f"Не удалось перепланировать погодное уведомление: {e}")
    await update.message.reply_text(
        f"✅ Время утреннего погодного уведомления установлено: {time_str}",
        reply_markup=get_main_menu_keyboard(user)
    )
    # Возвращаемся в подменю настроек погоды (через настройки).
    await update.message.reply_text(
        "Возвращаюсь в настройки.",
        reply_markup=get_settings_keyboard(user)
    )
    return USER_SETTINGS


async def change_city_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запустить смену города (через настройки)."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🏙 Введите новый город (например: Москва):",
        reply_markup=get_cancel_keyboard()
    )
    return CHANGE_CITY


@timeout(CONVERSATION_TIMEOUT)
async def change_city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранить смену города (через настройки).

    ПУНКТ 10: при смене города часовой пояс пересчитывается автоматически.
    """
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    city_input = update.message.text.strip()
    err = await reject_if_forbidden_chars(update, city_input, CHANGE_CITY)
    if err:
        return err
    ok, canonical, _loc = await weather_validate_city_full(city_input)
    if not ok:
        await update.message.reply_text(
            f"❌ Не удалось найти город «{city_input}». Попробуйте ещё раз:",
            reply_markup=get_cancel_keyboard()
        )
        return CHANGE_CITY
    user.city = canonical
    try:
        tz_offset = await detect_timezone_for_city(canonical)
        user.timezone = tz_offset
    except Exception as e:
        logger.warning(f"detect_timezone_for_city failed on change_city: {e}")
        tz_offset = getattr(user, "timezone", 3) or 3
    save_user(user)
    try:
        schedule_user_weather_job(context.application, user)
        schedule_user_holiday_job(context.application, user)
    except Exception as e:
        logger.error(f"Не удалось перепланировать погоду/праздники после смены города: {e}")
    sign = "+" if tz_offset >= 0 else "−"
    await update.message.reply_text(
        f"✅ Город изменён: {canonical}\n"
        f"🕒 Часовой пояс пересчитан: UTC{sign}{abs(tz_offset)}",
        reply_markup=get_settings_keyboard(user)
    )
    return USER_SETTINGS


async def holiday_settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открыть подменю «🎉 Настройки праздников»."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    enabled = getattr(user, 'holiday_notifications', True)
    text = (
        f"🎉 **Настройки праздников**\n\n"
        f"Уведомления о праздниках: {'Включены' if enabled else 'Выключены'}\n\n"
        f"Если включено — каждое утро в день любого праздника вы получите "
        f"уведомление с его названием."
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=get_holiday_settings_keyboard(user),
            parse_mode="Markdown"
        )
    except Exception:
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=get_holiday_settings_keyboard(user),
            parse_mode="Markdown"
        )
    return USER_SETTINGS


async def toggle_holidays_notif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включить / выключить уведомления о праздниках для пользователя."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)
    user.holiday_notifications = not getattr(user, 'holiday_notifications', True)
    save_user(user)
    try:
        if user.holiday_notifications:
            schedule_user_holiday_job(context.application, user)
        else:
            for j in context.application.job_queue.get_jobs_by_name(f"holiday_{user.user_id}"):
                j.schedule_removal()
    except Exception as e:
        logger.error(f"Ошибка обновления джоба праздников: {e}")
    return await holiday_settings_start(update, context)


# ==================================
# === ХЕНДЛЕРЫ: РАЗРАБОТЧИК — ПРАЗДНИКИ И БЫСТРОЕ СООБЩЕНИЕ ===
# ==================================

async def dev_set_holiday_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: разработчик назначает праздник — спрашиваем дату."""
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != DEVELOPER_ID:
        await query.edit_message_text("Доступ запрещён.")
        return MAIN_MENU
    await query.edit_message_text(
        "🎉 Введите дату праздника в формате ДД-ММ (например: 14-02 для 14 февраля):",
        reply_markup=get_cancel_keyboard()
    )
    return DEV_HOLIDAY_DATE


@timeout(CONVERSATION_TIMEOUT)
async def dev_holiday_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2: получили дату — спрашиваем название праздника."""
    user_id = str(update.effective_user.id)
    if user_id != DEVELOPER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return MAIN_MENU
    date_str = update.message.text.strip()
    try:
        datetime.strptime(date_str, "%d-%m")
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат. Используйте ДД-ММ (например: 14-02):",
            reply_markup=get_cancel_keyboard()
        )
        return DEV_HOLIDAY_DATE
    context.user_data['dev_holiday_date'] = date_str
    await update.message.reply_text(
        f"📅 Дата: {date_str}\n\nТеперь введите название праздника:",
        reply_markup=get_cancel_keyboard()
    )
    return DEV_HOLIDAY_TEXT


@timeout(CONVERSATION_TIMEOUT)
async def dev_holiday_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3: получили название — сохраняем праздник."""
    user_id = str(update.effective_user.id)
    if user_id != DEVELOPER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return MAIN_MENU
    text = update.message.text.strip()
    err = await reject_if_forbidden_chars(update, text, DEV_HOLIDAY_TEXT)
    if err:
        return err
    date_str = context.user_data.pop('dev_holiday_date', None)
    if not date_str:
        await update.message.reply_text("Попробуйте снова.")
        return await developer_panel(update, context)
    holidays = load_holidays()
    holidays.setdefault(date_str, []).append(text)
    save_holidays(holidays)
    await update.message.reply_text(
        f"✅ Праздник «{text}» назначен на {date_str}.\n\n"
        f"В этот день уведомление получат все пользователи, у которых "
        f"включены уведомления о праздниках.",
        reply_markup=get_developer_keyboard()
    )
    return DEV_PANEL


async def dev_delete_holiday_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список праздников для удаления."""
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != DEVELOPER_ID:
        await query.edit_message_text("Доступ запрещён.")
        return MAIN_MENU
    holidays = load_holidays()
    if not holidays:
        await query.edit_message_text(
            "ℹ️ Список праздников пуст.",
            reply_markup=get_developer_keyboard()
        )
        return DEV_PANEL
    keyboard = []
    # Сортируем по месяцу-дню в формате ДД-ММ → переводим в (MM, DD).
    def _sort_key(date_key):
        try:
            d, m = date_key.split("-")
            return (int(m), int(d))
        except Exception:
            return (99, 99)
    for date_key in sorted(holidays.keys(), key=_sort_key):
        for idx, name in enumerate(holidays.get(date_key, [])):
            short_name = (name[:30] + "…") if len(name) > 30 else name
            cb = f"dev_del_hol_{date_key}_{idx}"
            # callback_data ограничен 64 байтами — у нас точно влезает (ДД-ММ + индекс).
            keyboard.append([InlineKeyboardButton(f"{date_key}: {short_name}", callback_data=cb)])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="dev_back_panel")])
    await query.edit_message_text(
        "🗑 Выберите праздник, который хотите удалить:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return DEV_HOLIDAY_DELETE


async def dev_delete_holiday_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удалить выбранный праздник."""
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != DEVELOPER_ID:
        await query.edit_message_text("Доступ запрещён.")
        return MAIN_MENU
    # Формат: dev_del_hol_<ДД-ММ>_<idx>
    payload = query.data[len("dev_del_hol_"):]
    # ДД-ММ содержит дефис, поэтому split по последнему "_".
    if "_" not in payload:
        await query.edit_message_text("Не удалось удалить.", reply_markup=get_developer_keyboard())
        return DEV_PANEL
    date_str, idx_str = payload.rsplit("_", 1)
    try:
        idx = int(idx_str)
    except ValueError:
        await query.edit_message_text("Не удалось удалить.", reply_markup=get_developer_keyboard())
        return DEV_PANEL
    holidays = load_holidays()
    if date_str in holidays and 0 <= idx < len(holidays[date_str]):
        removed = holidays[date_str].pop(idx)
        if not holidays[date_str]:
            del holidays[date_str]
        save_holidays(holidays)
        await query.edit_message_text(
            f"✅ Удалён праздник: {removed} ({date_str})",
            reply_markup=get_developer_keyboard()
        )
    else:
        await query.edit_message_text(
            "❌ Праздник не найден.",
            reply_markup=get_developer_keyboard()
        )
    return DEV_PANEL


async def dev_instant_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало рассылки сообщения всем пользователям БЕЗ подписи разработчика."""
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != DEVELOPER_ID:
        await query.edit_message_text("Доступ запрещён.")
        return MAIN_MENU
    await query.edit_message_text(
        "📩 Введите сообщение, которое будет отправлено всем пользователям "
        "БЕЗ подписи разработчика (как обычное уведомление):",
        reply_markup=get_cancel_keyboard()
    )
    return DEV_INSTANT_BROADCAST


@timeout(CONVERSATION_TIMEOUT)
async def dev_instant_broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка анонимной (без подписи) рассылки всем."""
    user_id = str(update.effective_user.id)
    if user_id != DEVELOPER_ID:
        await update.message.reply_text("Доступ запрещён.")
        return MAIN_MENU
    message_text = update.message.text
    users = load_users()
    sent = 0
    for uid, user in users.items():
        if getattr(user, 'is_blocked', False):
            continue
        try:
            await context.bot.send_message(chat_id=int(uid), text=message_text)
            sent += 1
        except Exception as e:
            logger.error(f"Ошибка анонимной рассылки {uid}: {e}")
    await update.message.reply_text(
        f"✅ Сообщение отправлено {sent} пользователям (без подписи разработчика)."
    )
    return await developer_panel(update, context)


# ==================================
# === ДЖОБЫ: ПОГОДА И ПРАЗДНИКИ ===
# ==================================

async def _send_weather_notification(context: ContextTypes.DEFAULT_TYPE):
    """Утреннее погодное уведомление пользователю."""
    job_data = context.job.data or {}
    user_id = job_data.get('user_id')
    user = get_user(user_id)
    if not user or not getattr(user, 'weather_notifications', True):
        return
    if not getattr(user, 'city', None):
        return
    try:
        text = await weather_current_text(user.city)
        # Кнопка «На 3 дня» прямо под утренним уведомлением — удобный быстрый доступ.
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Узнать на 3 дня", callback_data="weather_3days")]
        ])
        await context.bot.send_message(
            chat_id=int(user_id),
            text=f"🌅 Доброе утро! Погода на сегодня:\n\n{text}",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка погодного уведомления {user_id}: {e}")


async def _send_user_holiday_notification(context: ContextTypes.DEFAULT_TYPE):
    """Утреннее уведомление о праздниках сегодня (если есть). Не от лица
    разработчика — обычное уведомление."""
    job_data = context.job.data or {}
    user_id = job_data.get('user_id')
    user = get_user(user_id)
    if not user or not getattr(user, 'holiday_notifications', True):
        return
    today_key = datetime.now().strftime("%d-%m")
    holidays = load_holidays()
    items = holidays.get(today_key, [])
    if not items:
        return
    text = "🎉 Сегодня праздник!\n\n" + "\n".join(f"• {h}" for h in items)
    try:
        await context.bot.send_message(chat_id=int(user_id), text=text)
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления о празднике {user_id}: {e}")


def schedule_user_weather_job(application, user):
    """Совместимость с прежним API. Сейчас доставка погодных уведомлений
    идёт через единый тикер `_unified_notification_tick`, поэтому здесь мы
    лишь удаляем устаревшие run_daily-джобы (если они остались от прошлой
    схемы), чтобы не было дублей."""
    try:
        if not user or not application or not application.job_queue:
            return
        name = f"weather_{user.user_id}"
        for j in application.job_queue.get_jobs_by_name(name):
            j.schedule_removal()
    except Exception as e:
        logger.error(f"schedule_user_weather_job cleanup error: {e}")


def schedule_user_holiday_job(application, user):
    """Совместимость с прежним API. Доставкой занимается единый тикер
    `_unified_notification_tick`. Здесь только удаляем старые run_daily-джобы."""
    try:
        if not user or not application or not application.job_queue:
            return
        name = f"holiday_{user.user_id}"
        for j in application.job_queue.get_jobs_by_name(name):
            j.schedule_removal()
    except Exception as e:
        logger.error(f"schedule_user_holiday_job cleanup error: {e}")


# ==================================
# === ЕДИНЫЙ ТИКЕР УВЕДОМЛЕНИЙ ===
# ==================================
# Зачем нужен:
#  Старая схема использовала application.job_queue.run_daily(time=...) для
#  каждого утра/вечера/погоды/праздника/ДР. На некоторых хостингах (особенно
#  Termux на Android, где tzdata неполная и APScheduler работает неустойчиво)
#  эти cron-джобы молча не срабатывали — пользователь не получал ни утренние,
#  ни вечерние, ни ДР, ни праздничные уведомления. Так же терялись таймеры.
#
#  Новый подход: один-единственный run_repeating-джоб, который тикает каждые
#  60 секунд и сам решает, кому что нужно отправить прямо сейчас. Этот
#  механизм использует interval-trigger APScheduler — самый простой и
#  надёжный, не зависящий от системного tzdata, и стабильно работает в
#  Termux/Render/Railway/VPS.
#
#  Чтобы не присылать одно и то же уведомление дважды в день при перезапуске
#  бота — храним «дата последней отправки» по каждому виду уведомления для
#  каждого пользователя в файле NOTIFICATION_LOG_FILE.

def _load_notification_log():
    return load_data(NOTIFICATION_LOG_FILE, {})


def _save_notification_log(log):
    save_data(NOTIFICATION_LOG_FILE, log)


def _notification_already_sent(log, user_id, key, local_today_str):
    """Проверяет, отправлялось ли уже сегодня уведомление `key` пользователю."""
    return log.get(str(user_id), {}).get(key) == local_today_str


def _mark_notification_sent(log, user_id, key, local_today_str):
    """Помечает, что уведомление `key` отправлено пользователю в день local_today_str."""
    user_log = log.setdefault(str(user_id), {})
    user_log[key] = local_today_str


def _user_local_now(user):
    """Локальное время пользователя (наивный datetime) с учётом его tz.

    ВАЖНО: использует `_now_utc()`, который применяет коррекцию по
    внешнему сервису времени (timeapi.io / worldtimeapi.org). Это
    защищает утренние/вечерние уведомления от дрейфа системных часов
    хостинга. Раньше тут был `datetime.utcnow()`, что приводило к
    «уведомления приходят не в то время / не приходят вовсе» на
    бесплатных контейнерах с плавающими часами.

    ИСПРАВЛЕНО: tz теперь может быть дробным (UTC+5:30 от
    detect_timezone_for_city) — считаем через float, а не int(),
    иначе получасовые пояса обрезались до целого часа.
    """
    tz_raw = getattr(user, 'timezone', 3) if user else 3
    try:
        tz_num = float(tz_raw)
    except Exception:
        # Бывают случаи, когда timezone сохранён как строка типа "+3" или
        # "UTC+3". Аккуратно вынем число — иначе timedelta(hours=str)
        # упал бы и весь тикер для этого пользователя «съедал» исключение.
        s = str(tz_raw or "").strip().upper().replace("UTC", "").replace(" ", "").replace(",", ".")
        try:
            tz_num = float(s) if s else 3.0
        except Exception:
            tz_num = 3.0
    if not (-12 <= tz_num <= 14):
        tz_num = 3.0
    return _now_utc() + timedelta(hours=tz_num)


def _is_daily_time_due(local_now, target_hhmm, max_late_minutes=360):
    """Ежедневный слот ЧЧ:ММ — учитывает **последнее уже наступившее** время,
    в том числе «вчерашнее», если бот проснулся после полуночи.

    Раньше сравнение шло только с «сегодняшней» датой: вечернее 22:00,
    пропущенное из‑за сна хостинга до 02:00 следующего дня, никогда не
    догонялось (для «сегодня» 22:00 ещё впереди). Отсюда «вечернее не
    приходит вообще» на бесплатных тарифах.

    Возвращает (due, too_late, occurrence_date):
      occurrence_date — строка YYYY-MM-DD дня, к которому относится этот слот
      (для журнала NOTIFICATION_LOG_FILE). None если сейчас раньше любого
      окна в пределах lookback.
    """
    if not target_hhmm:
        return False, False, None
    try:
        parts = target_hhmm.split(":")
        h, m = int(parts[0]), int(parts[1])
    except Exception:
        return False, False, None

    def _dt_on(day):
        return datetime.combine(day, dt_time(h, m))

    today = local_now.date()
    lookback_days = max(2, int(max_late_minutes // (24 * 60)) + 2)
    candidates = [_dt_on(today - timedelta(days=i)) for i in range(lookback_days + 1)]
    past = [c for c in candidates if c <= local_now]
    if not past:
        return False, False, None
    latest = max(past)
    diff_min = (local_now - latest).total_seconds() / 60.0
    occ = latest.strftime("%Y-%m-%d")
    if diff_min > max_late_minutes:
        return False, True, occ
    return True, False, occ


def _is_time_due(local_now, target_hhmm, max_late_minutes=360):
    """Обёртка над `_is_daily_time_due` для старых вызовов (только due/too_late)."""
    due, too_late, _occ = _is_daily_time_due(local_now, target_hhmm, max_late_minutes)
    return due, too_late


async def _tick_send_timers(bot):
    """Часть тикера: проверяет TIMERS_FILE и отправляет все таймеры,
    чьё время уже наступило. Дубль-защищена через is_active."""
    try:
        timers = load_data(TIMERS_FILE, {})
    except Exception as e:
        logger.error(f"tick/timers: load error: {e}")
        return
    if not timers:
        return
    now_utc = datetime.utcnow()
    changed = False
    for timer_id, timer_data in list(timers.items()):
        try:
            if not timer_data.get('is_active'):
                continue
            date_str = timer_data.get('target_date')
            time_str = timer_data.get('target_time')
            user_id = timer_data.get('user_id')
            if not date_str or not time_str or not user_id:
                continue
            try:
                target_local = datetime.strptime(
                    f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
                )
            except ValueError:
                continue
            user = get_user(user_id)
            tz = getattr(user, 'timezone', 3) if user else 3
            target_utc = target_local - timedelta(hours=tz)
            if (now_utc - target_utc).total_seconds() < 0:
                continue  # ещё не время
            # Атомарная пометка: помечаем неактивным ДО отправки, чтобы
            # параллельный safety-net/run_once не отправил дубль.
            # ДЛЯ ПОВТОРЯЮЩИХСЯ (repeat_daily): переносим на завтра,
            # оставаясь активными — и ОБЯЗАТЕЛЬНО до отправки.
            if _timer_advance_daily(timer_data):
                timer_data['fired_at'] = datetime.now().strftime("%Y-%m-%d %H:%M")
            else:
                timer_data['is_active'] = False
                timer_data['fired_at'] = datetime.now().strftime("%Y-%m-%d %H:%M")
            timers[timer_id] = timer_data
            try:
                save_data(TIMERS_FILE, timers)
            except Exception as e:
                logger.error(f"tick/timers: save before send failed for {timer_id}: {e}")
            changed = True
            msg = _timer_message_text(timer_data)
            try:
                await bot.send_message(
                    chat_id=int(user_id),
                    text=msg,
                )
                logger.info(f"tick/timers: timer {timer_id} -> {user_id} sent.")
            except Exception as e:
                logger.error(f"tick/timers: send failed {timer_id}->{user_id}: {e}")
        except Exception as e:
            logger.error(f"tick/timers: error on timer {timer_id}: {e}")
    if changed:
        try:
            save_data(TIMERS_FILE, timers)
        except Exception as e:
            logger.error(f"tick/timers: final save failed: {e}")


async def _tick_broadcast_birthday_to_class(bot, user):
    """Уведомляет одноклассников именинника, что у него сегодня ДР.

    ВАЖНО: самому имениннику в класс ЭТО НЕ присылаем — пользователь
    жаловался, что приходит «у меня сегодня ДР» ему же самому. Поэтому
    в цикле явно скипаем `member_id == user.user_id`.

    Срабатывает только если у пользователя включён `show_birthday_to_class`.
    По запросу пользователя — теперь, если у класса есть админы, бот
    дополнительно тэгает их первыми, чтобы поздравление точно увидели,
    а самому имениннику отправляет короткое подтверждение «классу
    сообщили о твоём дне рождения», чтобы он точно знал, что фича
    отработала.
    """
    try:
        if not getattr(user, 'show_birthday_to_class', True):
            logger.info(
                f"birthday_class_broadcast: {user.user_id} — show_birthday_to_class выключено, пропускаю"
            )
            return
        if not getattr(user, 'birthday', None):
            return
        try:
            class_obj = get_class_by_user(user.user_id)
        except Exception as e:
            logger.error(f"birthday_class_broadcast: get_class_by_user failed: {e}")
            return
        if not class_obj:
            logger.info(
                f"birthday_class_broadcast: {user.user_id} не состоит в классе — некому сообщать"
            )
            return
        members = list(set(
            (getattr(class_obj, 'students', []) or [])
            + (getattr(class_obj, 'admins', []) or [])
        ))
        # Имя именинника. Если есть @username — добавим (так одноклассникам
        # будет легче кликнуть и поздравить).
        display_name = user.first_name or 'одноклассник(а)'
        username = (getattr(user, 'username', '') or '').lstrip('@')
        mention = f"@{username}" if username else display_name
        text = (
            f"🎂 Сегодня день рождения у {display_name} ({mention})!\n\n"
            f"Не забудьте поздравить 🎉"
        )
        sent_to = 0
        for member_id in members:
            try:
                # Пропускаем самого именинника — ему это не нужно.
                if str(member_id) == str(user.user_id):
                    continue
                await bot.send_message(chat_id=int(member_id), text=text)
                sent_to += 1
            except Exception as e:
                logger.error(
                    f"birthday_class_broadcast: send to {member_id} failed: {e}"
                )
        logger.info(
            f"birthday_class_broadcast: разослано {sent_to} одноклассникам "
            f"для именинника {user.user_id} (класс {class_obj.class_code})"
        )
        # Подтверждение самому имениннику — он точно узнает, что
        # уведомление отправлено его классу.
        if sent_to > 0:
            try:
                await bot.send_message(
                    chat_id=int(user.user_id),
                    text=(
                        f"📢 Я сообщил твоему классу ({class_obj.class_name}), "
                        f"что у тебя сегодня день рождения. Получили "
                        f"уведомление: {sent_to} человек(а)."
                    ),
                )
            except Exception as e:
                logger.error(
                    f"birthday_class_broadcast: confirm to self failed: {e}"
                )
    except Exception as e:
        logger.error(f"birthday_class_broadcast: top-level error: {e}")


async def _tick_send_birthday_for_user(
    bot, user, log, content_today_str, log_slot_date=None
):
    """Отправляет уведомление «через сколько дней мой ДР» / поздравление в сам
    день рождения. Соблюдает birthday_personal_notification.

    `content_today_str` — реальный «сегодня» пользователя в его TZ (для текста
    и расчёта ДР). `log_slot_date` — дата слота в журнале (если догнали
    вчерашнее окно времени после полуночи).
    """
    if not getattr(user, 'birthday', None):
        return
    user_id = user.user_id
    mark_date = log_slot_date if log_slot_date else content_today_str
    try:
        local_today = datetime.strptime(content_today_str, "%Y-%m-%d").date()
        try:
            birthday = datetime.strptime(user.birthday, "%Y-%m-%d").date()
        except Exception:
            return
        is_birthday_today = (
            (birthday.month, birthday.day) == (local_today.month, local_today.day)
        )
        if is_birthday_today:
            # Личное поздравление — только если включено.
            if getattr(user, 'birthday_personal_notification', True):
                years = local_today.year - birthday.year
                if (local_today.month, local_today.day) < (birthday.month, birthday.day):
                    years -= 1
                import random as _rnd
                greeting = _rnd.choice(BIRTHDAY_GREETINGS).format(
                    name=user.first_name or "друг"
                )
                message = greeting
                if years >= 1:
                    message += f"\n\n🎁 Тебе сегодня исполняется {years}!"
                try:
                    await bot.send_message(chat_id=int(user_id), text=message)
                except Exception as e:
                    logger.error(f"tick/birthday personal {user_id}: {e}")
            # Уведомление одноклассникам (без самого именинника).
            await _tick_broadcast_birthday_to_class(bot, user)
        else:
            if not getattr(user, 'birthday_personal_notification', True):
                _mark_notification_sent(log, user_id, 'birthday', mark_date)
                return
            days_left = _days_until_birthday_for_user(user)
            if days_left is None or days_left <= 0:
                return
            word = _format_days_word(days_left)
            message = (
                f"🎂 До твоего дня рождения осталось {days_left} {word}!\n"
                f"📅 Дата: {user.birthday}"
            )
            await bot.send_message(chat_id=int(user_id), text=message)
        _mark_notification_sent(log, user_id, 'birthday', mark_date)
    except Exception as e:
        logger.error(f"tick/birthday: ошибка для {user_id}: {e}")


# Глобальный async-лок: предотвращает гонку между JobQueue-тикером и
# fallback asyncio-тикером, чтобы один и тот же пользователь не получил
# уведомление дважды, если оба тика стартанули в одну и ту же секунду.
# ВАЖНО: создаём ЛЕНИВО (внутри тикера), а не на уровне модуля. Иначе
# на некоторых средах (особенно при старте через `python -m`/uvloop)
# `asyncio.Lock()` бьётся об отсутствующий event loop и падает с ошибкой,
# из-за чего весь тикер мог не запускаться никогда.
_unified_tick_lock = None


def _get_unified_tick_lock():
    global _unified_tick_lock
    if _unified_tick_lock is None:
        _unified_tick_lock = asyncio.Lock()
    return _unified_tick_lock


async def _unified_notification_tick(context):
    """Главный тикер. Запускается каждые 30 секунд через run_repeating
    (плюс fallback asyncio-тикер).

    Делает по очереди:
      1. Шлёт все «созревшие» таймеры.
      2. Для каждого пользователя проверяет, не пора ли отправить:
           — утреннее уведомление (по user.morning_notification_time)
           — вечернее уведомление (по user.evening_notification_time)
           — погоду (по user.weather_notification_time, если включено и есть город)
           — праздники (по user.weather_notification_time, если включено и есть праздник)
           — ДР-напоминание (по user.birthday_notification_time)
      3. Каждое уведомление отправляется не более 1 раза в день благодаря
         журналу NOTIFICATION_LOG_FILE.

    Не зависит от cron-trigger в APScheduler — поэтому надёжно работает в
    Termux и других окружениях с битым tzdata.
    """
    # Если предыдущий тик ещё работает — пропускаем этот, чтобы не
    # порождать гонок и дублей. Это особенно важно при двойном тике
    # (JobQueue + asyncio safety net): когда оба стартуют почти
    # одновременно, лок гарантирует строго последовательное выполнение.
    lock = _get_unified_tick_lock()
    if lock.locked():
        return
    async with lock:
        await _unified_notification_tick_locked(context)


async def _unified_notification_tick_locked(context):
    """Внутренняя реализация тикера. Вынесена для того, чтобы вся
    основная логика выполнялась внутри `_unified_tick_lock` ровно один
    раз за тик и не пересекалась с другим тиком."""
    bot = context.bot
    # Диагностика: видно в логах каждые 30 сек, что тикер вообще
    # работает. Если этих строк нет — значит, бот спит/не запустился, и
    # никакие проверки времени не помогут.
    # ВАЖНО: используем `_now_utc()` — это «реальное» UTC, скорректированное
    # по внешнему сервису времени. В лог пишем И системное, и реальное —
    # сразу видно, есть ли дрейф часов хостинга.
    server_utc_now = datetime.utcnow()
    real_utc_now = _now_utc()
    logger.info(
        f"unified_tick: fired, system_utc={server_utc_now:%Y-%m-%d %H:%M:%S}, "
        f"real_utc={real_utc_now:%Y-%m-%d %H:%M:%S}, "
        f"drift={_time_drift_seconds:+.1f}s"
    )
    # 1) Таймеры
    try:
        await _tick_send_timers(bot)
    except Exception as e:
        logger.error(f"unified_tick: timers crashed: {e}")

    # 2) Дневные уведомления по пользователям
    try:
        users = load_users()
    except Exception as e:
        logger.error(f"unified_tick: load_users failed: {e}")
        return
    if not users:
        logger.info("unified_tick: пользователей нет (users.json пустой?)")
        return
    logger.info(f"unified_tick: проверяю {len(users)} пользователей")
    log = _load_notification_log()
    log_changed = False

    for uid, user in users.items():
        try:
            local_now = _user_local_now(user)
            local_today_str = local_now.strftime("%Y-%m-%d")
            # ИСПРАВЛЕНО (по запросу пользователя):
            # «при регистрации не должно приходить доброе утро или
            # спокойной ночи». Если пользователь зарегистрировался сегодня
            # (по своему локальному времени), утро/вечер пропускаем — но
            # в журнале помечаем как «отправлено», чтобы завтра пришло
            # ровно в назначенное время.
            joined_local_date = None
            joined_raw = getattr(user, 'joined_date', '') or ''
            if joined_raw:
                try:
                    joined_local_date = datetime.strptime(
                        joined_raw[:10], "%Y-%m-%d"
                    ).date()
                except Exception:
                    joined_local_date = None
            registered_today = (
                joined_local_date is not None
                and joined_local_date == local_now.date()
            )

            # 2a) Утреннее. Окно догона — 14 часов; дата слота берётся из
            # `_is_daily_time_due` (может быть «вчера», если хост проснулся
            # после полуночи и вчерашнее утро ещё в пределах окна).
            if getattr(user, 'notifications', True):
                morning_t = getattr(user, 'morning_notification_time', '08:00') or '08:00'
                due, too_late, morn_occ = _is_daily_time_due(
                    local_now, morning_t, max_late_minutes=14 * 60
                )
                logger.info(
                    f"unified_tick: morning check uid={uid} t={morning_t} "
                    f"local_now={local_now:%H:%M} due={due} too_late={too_late} occ={morn_occ} "
                    f"sent_occ={(not morn_occ) or _notification_already_sent(log, uid, 'morning', morn_occ)} "
                    f"reg_today={registered_today}"
                )
                if registered_today:
                    if not _notification_already_sent(log, uid, 'morning', local_today_str):
                        _mark_notification_sent(log, uid, 'morning', local_today_str)
                        log_changed = True
                elif morn_occ and not _notification_already_sent(log, uid, 'morning', morn_occ):
                    if due:
                        try:
                            await bot.send_message(
                                chat_id=int(uid),
                                text=f"☀️ {getattr(user, 'morning_text', 'Доброе утро!')}",
                            )
                            _mark_notification_sent(log, uid, 'morning', morn_occ)
                            try:
                                _save_notification_log(log)
                            except Exception as save_err:
                                logger.error(f"unified_tick: log save (morning) {uid}: {save_err}")
                            log_changed = False
                            logger.info(f"unified_tick: утро -> {uid} отправлено (слот {morn_occ}).")
                        except Exception as e:
                            logger.error(f"unified_tick: morning {uid}: {e}")
                    elif too_late:
                        _mark_notification_sent(log, uid, 'morning', morn_occ)
                        log_changed = True

            # 2b) Вечернее. Окно 12 ч — хватает, чтобы догнать слот после
            # засыпания хостинга через полночь (см. `_is_daily_time_due`).
            if getattr(user, 'notifications', True):
                evening_t = getattr(user, 'evening_notification_time', '22:00') or '22:00'
                due, too_late, eve_occ = _is_daily_time_due(
                    local_now, evening_t, max_late_minutes=12 * 60
                )
                logger.info(
                    f"unified_tick: evening check uid={uid} t={evening_t} "
                    f"local_now={local_now:%H:%M} due={due} too_late={too_late} occ={eve_occ} "
                    f"sent_occ={(not eve_occ) or _notification_already_sent(log, uid, 'evening', eve_occ)} "
                    f"reg_today={registered_today}"
                )
                if registered_today:
                    if not _notification_already_sent(log, uid, 'evening', local_today_str):
                        _mark_notification_sent(log, uid, 'evening', local_today_str)
                        log_changed = True
                elif eve_occ and not _notification_already_sent(log, uid, 'evening', eve_occ):
                    if due:
                        try:
                            await bot.send_message(
                                chat_id=int(uid),
                                text=f"🌙 {getattr(user, 'evening_text', 'Спокойной ночи!')}",
                            )
                            _mark_notification_sent(log, uid, 'evening', eve_occ)
                            try:
                                _save_notification_log(log)
                            except Exception as save_err:
                                logger.error(f"unified_tick: log save (evening) {uid}: {save_err}")
                            log_changed = False
                            logger.info(f"unified_tick: вечер -> {uid} отправлено (слот {eve_occ}).")
                        except Exception as e:
                            logger.error(f"unified_tick: evening {uid}: {e}")
                    elif too_late:
                        _mark_notification_sent(log, uid, 'evening', eve_occ)
                        log_changed = True

            # 2c) Погода — ЕЖЕДНЕВНАЯ РАССЫЛКА восстановлена и исправлена.
            # ИСПРАВЛЕННЫЙ БАГ: раньше блок только ПОМЕЧАЛ погоду отправленной,
            # не рассылая её. Теперь:
            #   • рассылка идёт по времени user.weather_notification_time в
            #     ЛОКАЛЬНОМ времени пользователя (правильный часовой пояс:
            #     user.timezone определяется автоматически по городу через
            #     detect_timezone_for_city и хранится в БД);
            #   • «задачи рассылки» живут в БД: users.json (город/время/пояс/
            #     вкл-выкл) + NOTIFICATION_LOG_FILE (журнал «за какой день уже
            #     отправлено») — после рестарта ничего не теряется и не
            #     дублируется;
            #   • окно догона — 14 часов (проснулся хостинг — догоним слот).
            if getattr(user, 'weather_notifications', True) and getattr(user, 'city', None):
                weather_t = getattr(user, 'weather_notification_time', '07:00') or '07:00'
                due_w, too_late_w, w_occ = _is_daily_time_due(
                    local_now, weather_t, max_late_minutes=14 * 60
                )
                if w_occ and not _notification_already_sent(log, uid, 'weather', w_occ):
                    if due_w:
                        try:
                            wtext = await weather_current_text(user.city)
                            wkb = InlineKeyboardMarkup([
                                [InlineKeyboardButton("📅 Узнать на 3 дня", callback_data="weather_3days")]
                            ])
                            await bot.send_message(
                                chat_id=int(uid),
                                text=f"🌅 Доброе утро! Погода на сегодня:\n\n{wtext}",
                                reply_markup=wkb,
                            )
                            _mark_notification_sent(log, uid, 'weather', w_occ)
                            try:
                                _save_notification_log(log)
                            except Exception as save_err:
                                logger.error(f"unified_tick: log save (weather) {uid}: {save_err}")
                            log_changed = False
                            logger.info(f"unified_tick: погода -> {uid} отправлено (слот {w_occ}).")
                        except Exception as e:
                            logger.error(f"unified_tick: weather {uid}: {e}")
                    elif too_late_w:
                        _mark_notification_sent(log, uid, 'weather', w_occ)
                        log_changed = True

            # 2d) Праздники.
            # Праздник — событие на весь день, поэтому окно опоздания
            # максимально широкое — 23 часа (то есть «весь сегодняшний
            # день, до самой полуночи»). Это гарантирует, что даже если
            # хостинг проспал нужное время на 5+ часов, праздник всё равно
            # прилетит. Самое «нужное время» — утреннее (`morning_notification_time`).
            if getattr(user, 'holiday_notifications', True):
                holiday_t = (
                    getattr(user, 'morning_notification_time', None)
                    or getattr(user, 'weather_notification_time', '08:00')
                    or '08:00'
                )
                due, too_late, hol_occ = _is_daily_time_due(
                    local_now, holiday_t, max_late_minutes=23 * 60
                )
                if hol_occ and not _notification_already_sent(log, uid, 'holiday', hol_occ):
                    if due:
                        try:
                            occ_date = datetime.strptime(hol_occ, "%Y-%m-%d").date()
                            day_key = occ_date.strftime("%d-%m")
                            holidays = load_holidays()
                            items = holidays.get(day_key, [])
                            sent_holiday = False
                            if items:
                                if hol_occ == local_today_str:
                                    header = "🎉 Сегодня праздник!\n\n"
                                else:
                                    header = f"🎉 Праздник ({hol_occ})!\n\n"
                                text = header + "\n".join(f"• {h}" for h in items)
                                await bot.send_message(chat_id=int(uid), text=text)
                                sent_holiday = True
                                logger.info(f"unified_tick: праздник -> {uid} отправлено.")
                            _mark_notification_sent(log, uid, 'holiday', hol_occ)
                            if sent_holiday:
                                try:
                                    _save_notification_log(log)
                                except Exception as save_err:
                                    logger.error(f"unified_tick: log save (holiday) {uid}: {save_err}")
                                log_changed = False
                            else:
                                log_changed = True
                        except Exception as e:
                            logger.error(f"unified_tick: holiday {uid}: {e}")
                    elif too_late:
                        _mark_notification_sent(log, uid, 'holiday', hol_occ)
                        log_changed = True

            # 2e) ДР-напоминание (через сколько дней) и поздравление в сам ДР.
            # ВАЖНО: ДР — раз в год, поэтому окно опоздания = 24 часа. Если
            # бот по любой причине проспал назначенное время — поздравление
            # всё равно уйдёт сегодня (и одноклассникам тоже).
            bday_t = getattr(user, 'birthday_notification_time', '09:00') or '09:00'
            due, too_late, bday_occ = _is_daily_time_due(
                local_now, bday_t, max_late_minutes=24 * 60
            )
            if bday_occ and not _notification_already_sent(log, uid, 'birthday', bday_occ):
                if due:
                    await _tick_send_birthday_for_user(
                        bot, user, log, local_today_str, bday_occ
                    )
                    log_changed = True
                elif too_late:
                    _mark_notification_sent(log, uid, 'birthday', bday_occ)
                    log_changed = True
        except Exception as e:
            logger.error(f"unified_tick: пользователь {uid}: {e}")

    if log_changed:
        try:
            _save_notification_log(log)
        except Exception as e:
            logger.error(f"unified_tick: save log failed: {e}")


async def _post_init(application):
    """Запускается один раз после инициализации Application.

    КРИТИЧНО: каждый шаг в своём `try/except`, чтобы падение одного
    шага не блокировало регистрацию тикеров. Раньше всё было в одном
    общем `try/except`, и если, например, `start_keep_alive_server`
    падал на Render (порт занят / бинд не разрешён) — НИКАКИЕ тикеры
    не регистрировались, и утро/вечер не приходили, а в логах было лишь
    одно «Ошибка post_init».
    """
    # === ШАГ 1: ЕДИНЫЙ ТИКЕР УВЕДОМЛЕНИЙ — регистрируем САМЫМ ПЕРВЫМ. ===
    # Это критичный шаг: если он не выполнится, утро/вечер/праздники не
    # будут приходить никому. Поэтому делаем его до всех остальных
    # инициализаций.
    unified_tick_jobqueue_ok = False
    try:
        jq = application.job_queue
        if jq is None:
            logger.error(
                "job_queue = None: установите пакет APScheduler и extras "
                "`pip install \"python-telegram-bot[job-queue]\"` — иначе часть "
                "таймеров не планируется; уведомления утро/вечер идут через "
                "asyncio-страховку ниже."
            )
        else:
            jq.run_repeating(
                _unified_notification_tick,
                interval=30,
                first=3,
                name="unified_notification_tick",
            )
            unified_tick_jobqueue_ok = True
            logger.info(
                "Единый тикер уведомлений зарегистрирован (раз в 30 сек, первый тик через 3с)."
            )
    except Exception as e:
        logger.error(f"Не удалось зарегистрировать unified_notification_tick: {e}")

    # === ШАГ 2: запасной asyncio-тикер. ===
    # Дублирует JobQueue-тикер: если APScheduler по какой-то причине
    # не работает (бывает на некоторых версиях python-telegram-bot с
    # битым tzdata), отдельная корутина каждые 30 сек дёргает тот же
    # код. Глобальный лок `_unified_tick_lock` гарантирует отсутствие
    # дублей.
    async def _asyncio_safety_tick():
        first_loop = True
        while True:
            try:
                # Если JobQueue нет — первый тик через 3 с, иначе основной
                # тикер уже бьёт в 3 с и дублировать сразу не нужно.
                delay = 3 if (first_loop and not unified_tick_jobqueue_ok) else 30
                first_loop = False
                await asyncio.sleep(delay)
                class _CtxStub:
                    bot = application.bot
                await _unified_notification_tick(_CtxStub())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"asyncio safety tick error: {e}")
    # ВАЖНО: храним сильную ссылку на task в bot_data, чтобы Python не
    # собрал её сборщиком мусора. Без этого task может «исчезнуть» сам
    # собой, и бэкап-тикер замолчит. Application.create_task в post_init
    # выдаёт PTBUserWarning, что задача не будет «корректно дождана» — это
    # не критично для бесконечного цикла, но всё равно используем
    # asyncio.create_task: он возвращает ссылку, которую мы сохраняем.
    try:
        _safety_task = asyncio.create_task(_asyncio_safety_tick())
        try:
            application.bot_data.setdefault("_bg_tasks", []).append(_safety_task)
        except Exception:
            pass
        logger.info("Asyncio-страховочный тикер запущен (раз в 30 сек).")
    except Exception as e:
        logger.error(f"Не удалось запустить asyncio-страховку: {e}")

    # === ШАГ 3: миграция данных в облачное хранилище (Supabase / Mongo). ===
    # Один проход: читаем (Supabase → Mongo → файл) и сразу записываем
    # назад. Это гарантирует, что все ключевые JSON-сущности материализованы
    # в текущем активном хранилище.
    try:
        cloud_files = [
            USERS_FILE, CLASSES_FILE, TIMERS_FILE, ANONYMOUS_MESSAGES_FILE,
            SUGGESTIONS_FILE, HOMEWORK_FILE, CUSTOM_BUTTONS_FILE,
            BLOCKED_USERS_FILE, PERSONAL_BUTTONS_FILE, CLASS_BLOCKED_USERS_FILE,
            STARS_STATS_FILE, INSTRUCTIONS_FILE, USER_CODES_FILE,
            PRICES_FILE, GLOBAL_BUTTONS_FILE, HOLIDAYS_FILE,
            NOTIFICATION_LOG_FILE, SUBSCRIPTION_CONFIRMATIONS_FILE,
            DEV_SETTINGS_FILE, SUPPORT_MESSAGES_FILE, REFERRALS_FILE,
        ]
        if _supabase_ready:
            backend_name = "Supabase"
        elif _mongo_kv is not None:
            backend_name = "MongoDB"
        else:
            backend_name = "JSON-файлы"
        migrated, total = 0, 0
        for fname in cloud_files:
            try:
                data = load_data(fname, {})
                save_data(fname, data)
                total += 1
                if isinstance(data, dict) and len(data) > 0:
                    migrated += 1
            except Exception as e:
                logger.error(f"Cloud bootstrap для {fname}: {e}")
        logger.info(
            f"Cloud bootstrap готов ({backend_name}): {total} коллекций обработано, "
            f"{migrated} с непустыми данными."
        )
    except Exception as e:
        logger.error(f"Cloud bootstrap общий сбой: {e}")

    # === ШАГ 3.5: первичная синхронизация времени с внешним сервисом. ===
    # Ждём не больше 6 секунд: если внешний сервис недоступен, тикер всё
    # равно начнёт работать на системном UTC.
    try:
        await _refresh_time_drift()
    except Exception as e:
        logger.error(f"Первичная time-sync: {e}")

    # === ШАГ 3.6: фоновое обновление дрейфа времени. ===
    # Раз в 5 минут проверяем, не уехало ли системное UTC, и обновляем
    # `_time_drift_seconds`. Это страхует от плавного дрейфа часов на
    # хостинге, из-за которого `_is_time_due` мог пропустить минуту
    # утреннего/вечернего уведомления.
    async def _time_sync_loop():
        while True:
            try:
                await asyncio.sleep(5 * 60)
                await _refresh_time_drift()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"time-sync loop error: {e}")
    try:
        # См. комментарий выше про asyncio.create_task vs application.create_task.
        _ts_task = asyncio.create_task(_time_sync_loop())
        try:
            application.bot_data.setdefault("_bg_tasks", []).append(_ts_task)
        except Exception:
            pass
        logger.info("Time-sync loop запущен (каждые 5 минут).")
    except Exception as e2:
        logger.error(f"Не удалось запустить time-sync loop: {e2}")

    # === ШАГ 4: диагностика уведомлений по каждому пользователю. ===
    try:
        users_for_diag = load_users()
        diag_lines = []
        for diag_uid, diag_user in users_for_diag.items():
            tz_raw = getattr(diag_user, 'timezone', 3)
            try:
                tz = int(tz_raw)
            except Exception:
                tz = 3
            # ВАЖНО: используем `_now_utc()` — это «реальное» UTC, скорректированное
            # по внешнему сервису времени (см. `_refresh_time_drift`). Раньше
            # стоял `datetime.utcnow()`, и при дрейфе системных часов диагностика
            # показывала неправильное «local_now», вводя в заблуждение.
            local_now = _now_utc() + timedelta(hours=tz)
            diag_lines.append(
                f"  uid={diag_uid} "
                f"notif={getattr(diag_user, 'notifications', True)} "
                f"morn={getattr(diag_user, 'morning_notification_time', None)} "
                f"even={getattr(diag_user, 'evening_notification_time', None)} "
                f"tz=UTC{'+' if tz >= 0 else ''}{tz} "
                f"local_now={local_now:%Y-%m-%d %H:%M}"
            )
        if diag_lines:
            logger.info(
                "Диагностика уведомлений на старте:\n" + "\n".join(diag_lines)
            )
    except Exception as diag_err:
        logger.error(f"Диагностика уведомлений: {diag_err}")

    # === ШАГ 5: keep-alive HTTP-сервер (для Render Web Service). ===
    # ВАЖНО: ДО этой переработки сюда был обёрнут весь post_init одним
    # `try`, и падение этого шага молча отключало все тикеры. Теперь
    # шаг изолирован — даже если порт занят, тикеры уже зарегистрированы
    # и работают.
    try:
        await start_keep_alive_server()
    except Exception as e:
        logger.error(f"start_keep_alive_server failed: {e}")

    # === ШАГ 6: self-ping каждую минуту. ===
    try:
        if application.job_queue is not None:
            application.job_queue.run_repeating(
                _self_ping_job,
                interval=60,
                first=10,
                name="self_ping",
            )
    except Exception as e:
        logger.error(f"self_ping регистрация: {e}")

    # === ШАГ 7: восстановление таймеров пользователей. ===
    try:
        timers = load_data(TIMERS_FILE, {})
        for timer_id, timer_data in timers.items():
            try:
                schedule_timer_job(application, timer_id, timer_data)
            except Exception as e:
                logger.error(f"schedule_timer_job {timer_id}: {e}")
    except Exception as e:
        logger.error(f"timers load: {e}")

    # === ШАГ 8: safety-net для таймеров. ===
    try:
        if application.job_queue is not None:
            application.job_queue.run_repeating(
                _timer_safety_net,
                interval=30,
                first=15,
                name="timer_safety_net",
            )
    except Exception as e:
        logger.error(f"timer safety-net регистрация: {e}")

    # === ШАГ 9: legacy per-user планирование (no-op, для совместимости). ===
    try:
        users = load_users()
        for uid, user in users.items():
            try:
                schedule_user_daily_jobs(application, user)
                schedule_user_weather_job(application, user)
                schedule_user_holiday_job(application, user)
                schedule_user_birthday_job(application, user)
            except Exception as e:
                logger.error(f"per-user schedule {uid}: {e}")
    except Exception as e:
        logger.error(f"users load для per-user schedule: {e}")

    # === ШАГ 10: создаём дефолтный holidays.json если его нет. ===
    try:
        load_holidays()
    except Exception as e:
        logger.error(f"load_holidays: {e}")

    # === ШАГ 11: фоновое удаление старых анонимных сообщений. ===
    # Раз в сутки проходим по anonymous_messages.json, удаляем входящие
    # анонимки старше ANONYMOUS_TTL_DAYS у тех получателей, у кого нет
    # активной оплаченной защиты (user.anon_keep_until). За день до
    # удаления — предупреждение получателю с кнопкой «купить место».
    # Изолировано в свой try, чтобы падение здесь не сломало уже
    # зарегистрированные тикеры.
    try:
        if application.job_queue is not None:
            application.job_queue.run_repeating(
                anonymous_purge_job,
                interval=ANONYMOUS_PURGE_JOB_INTERVAL,
                first=30,
                name="anonymous_purge",
            )
        else:
            logger.warning(
                "anonymous_purge: job_queue=None, авто-очистка анонимок "
                "не запустится. Установите APScheduler-extras для PTB."
            )
    except Exception as e:
        logger.error(f"anonymous_purge регистрация: {e}")

    logger.info("post_init: все шаги инициализации завершены.")


# ==================================
# === НОВЫЕ ХЕНДЛЕРЫ: тема, поддержка, рефералы, спендинг, dev grant ===
# ==================================
# Все хендлеры ниже добавлены в рамках расширения функционала и НЕ заменяют
# существующую логику. Они только дополняют поведение бота:
#   • Темы оформления кнопок главного меню (косметика).
#   • Реферальная система: пользователь делится ссылкой, за каждого
#     зарегистрировавшегося приглашённого пользователя приглашающий получает
#     REFERRAL_REWARD_STARS виртуальных звёзд.
#   • Чат поддержки: пользователь пишет разработчику, разработчик отвечает.
#   • Разработчик может начислять внутренние звёзды любому пользователю.
#   • Платежи за функции: внутренней валютой ИЛИ Telegram Stars (XTR).
# Везде применяется анти-спам-тротлинг (is_user_spamming) и проверка прав.


# ---------- ПУНКТ 2: реферальная система ----------

async def referral_share_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать пользователю его реферальную ссылку и текущую статистику."""
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    # Получаем username бота для построения корректной ссылки.
    bot_username = None
    try:
        bot_username = (await context.bot.get_me()).username
    except Exception as e:
        logger.error(f"referral_share_handler: get_me failed: {e}")

    link = build_referral_link(bot_username or "", user_id)
    if not link:
        await query.edit_message_text(
            "❌ Не удалось сгенерировать ссылку. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]]),
        )
        return MAIN_MENU

    refs_count = getattr(user, 'referrals_count', 0) or 0
    text = (
        "🔗 *Ваша реферальная ссылка*\n\n"
        f"`{link}`\n\n"
        f"За каждого друга, который перейдёт по ссылке и завершит регистрацию, "
        f"вы получите *+{REFERRAL_REWARD_STARS}⭐* на внутренний баланс.\n\n"
        f"Приглашено: *{refs_count}* чел.\n"
        f"Текущий баланс: *{user.stars_balance}⭐*\n\n"
        f"Нажмите ссылку, скопируйте и отправьте друзьям."
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")]
            ]),
            parse_mode="Markdown",
        )
    except Exception:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
    return MAIN_MENU


# ---------- ПУНКТ 3: чат поддержки (пользовательская сторона) ----------

async def open_support_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открыть пользователю интерфейс чата поддержки. После этого следующее
    текстовое сообщение пользователя уйдёт разработчику и сохранится в
    support_messages.json."""
    # Может быть вызван и из callback, и из reply-меню. Поддерживаем оба пути.
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    # Анти-спам: открытие чата поддержки.
    if is_user_spamming(user_id, key="support_open"):
        msg = get_user_rate_limit_message()
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        else:
            await update.message.reply_text(msg)
        return MAIN_MENU

    text = (
        "💬 *Чат поддержки*\n\n"
        "Напишите одно сообщение разработчику. Мы постараемся ответить как "
        "можно скорее. Если хотите отменить — нажмите «❌ Отмена».\n\n"
        "⚠️ Не присылайте спам и оскорбления — есть автоматическая защита."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_action")]
    ])
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    return SUPPORT_CHAT_MESSAGE


@timeout(CONVERSATION_TIMEOUT)
async def support_chat_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принять сообщение пользователя в чат поддержки."""
    user_id = str(update.effective_user.id)
    user = get_user(user_id)
    if not user:
        user = User(user_id)

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text(
            "Пустое сообщение. Напишите текст или нажмите «❌ Отмена»."
        )
        return SUPPORT_CHAT_MESSAGE

    # Анти-спам: per-user, мин-интервал между двумя сообщениями.
    if is_user_spamming(user_id, key="support_msg", min_interval=2.0, burst=3, burst_window=20.0):
        await update.message.reply_text(get_user_rate_limit_message())
        return SUPPORT_CHAT_MESSAGE

    # Жёсткий лимит длины (защита от спама большими сообщениями).
    if len(text) > 1500:
        await update.message.reply_text("⚠️ Слишком длинное сообщение (макс. 1500 символов).")
        return SUPPORT_CHAT_MESSAGE

    # Сохраняем в журнал.
    try:
        append_support_message(user_id, "user", text)
    except Exception as e:
        logger.error(f"support_chat_message_handler: append failed: {e}")

    # Доставляем разработчику. Не используем parse_mode, чтобы спецсимволы
    # пользователя не ломали Markdown.
    dev_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✉️ Ответить", callback_data=f"dev_support_reply_{user_id}")]
    ])
    try:
        await context.bot.send_message(
            chat_id=int(DEVELOPER_ID),
            text=(
                f"💬 Чат поддержки\n"
                f"От: @{getattr(user, 'username', '') or '—'} (id: {user_id}, "
                f"имя: {getattr(user, 'first_name', '') or '—'})\n\n"
                f"{text}"
            ),
            reply_markup=dev_kb,
        )
    except Exception as e:
        logger.error(f"support_chat_message_handler: отправка разработчику: {e}")

    await update.message.reply_text(
        "✅ Сообщение отправлено разработчику. Ждите ответа в этом же чате.",
        reply_markup=get_main_menu_keyboard(user),
    )
    return MAIN_MENU


# ---------- ПУНКТ 4: разработчик начисляет внутренние звёзды ----------

async def dev_grant_stars_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разработчик: список пользователей для начисления звёзд."""
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(DEVELOPER_ID):
        await query.answer("Только для разработчика.", show_alert=True)
        return DEV_PANEL

    users = load_users()
    rows = []
    # Максимум 30 кнопок, чтобы влезть в одно сообщение.
    for uid, u in list(users.items())[:30]:
        label_username = getattr(u, 'username', None) or "—"
        label = f"{label_username} ({uid})"[:60]
        rows.append([InlineKeyboardButton(label, callback_data=f"dev_grant_pick_{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="dev_back_panel")])

    text = (
        "⭐ *Начисление звёзд пользователю*\n\n"
        "Выберите пользователя из списка (показаны первые 30):"
    )
    try:
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
    except Exception:
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=text,
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
    return DEV_GRANT_STARS_USER_PICK


async def dev_grant_stars_pick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разработчик выбрал пользователя — спрашиваем сумму звёзд."""
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(DEVELOPER_ID):
        await query.answer("Только для разработчика.", show_alert=True)
        return DEV_PANEL

    target_id = (query.data or "").replace("dev_grant_pick_", "", 1).strip()
    if not target_id or not target_id.isdigit():
        await query.answer("Некорректный пользователь.", show_alert=True)
        return DEV_GRANT_STARS_USER_PICK

    target = get_user(target_id)
    if not target:
        await query.answer("Пользователь не найден.", show_alert=True)
        return DEV_GRANT_STARS_USER_PICK

    context.user_data['dev_grant_target_id'] = target_id
    await query.edit_message_text(
        f"⭐ Начисление звёзд пользователю id={target_id}\n"
        f"Имя: {getattr(target, 'first_name', '') or '—'} | "
        f"@{getattr(target, 'username', '') or '—'}\n"
        f"Текущий баланс: {target.stars_balance}⭐\n\n"
        f"Введите целое число звёзд (положительное — начислить, "
        f"отрицательное — списать). Пример: 10 или -5",
        reply_markup=get_cancel_keyboard(),
    )
    return DEV_GRANT_STARS_AMOUNT


@timeout(CONVERSATION_TIMEOUT)
async def dev_grant_stars_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разработчик ввёл сумму — применяем начисление."""
    if str(update.effective_user.id) != str(DEVELOPER_ID):
        await update.message.reply_text("Только для разработчика.")
        return DEV_PANEL

    target_id = context.user_data.get('dev_grant_target_id')
    if not target_id:
        await update.message.reply_text("⚠️ Цель не задана. Откройте панель ещё раз.")
        return DEV_PANEL

    raw = (update.message.text or "").strip()
    try:
        amount = int(raw)
    except Exception:
        await update.message.reply_text("Введите целое число (можно с минусом). Пример: 10")
        return DEV_GRANT_STARS_AMOUNT

    if amount == 0:
        await update.message.reply_text("Ноль не допускается.")
        return DEV_GRANT_STARS_AMOUNT
    if abs(amount) > 100000:
        await update.message.reply_text("Слишком большая сумма (>100000). Уменьшите.")
        return DEV_GRANT_STARS_AMOUNT

    target = get_user(target_id)
    if not target:
        await update.message.reply_text("Пользователь не найден.")
        context.user_data.pop('dev_grant_target_id', None)
        return DEV_PANEL

    # ИСПРАВЛЕНИЕ ДУБЛИРОВАНИЯ ЗВЁЗД (по жалобе пользователя):
    # раньше тут было `target.stars_balance += amount; save_user(target)`
    # И ОТДЕЛЬНО `add_stars_transaction(target_id, amount, …)`. Так как
    # `add_stars_transaction` тоже сам прибавляет `amount` к балансу
    # (см. её определение), баланс УВЕЛИЧИВАЛСЯ ДВАЖДЫ. Теперь
    # начисление делает ровно одна функция — `add_stars_transaction`,
    # — а ручную правку баланса убрали.
    try:
        add_stars_transaction(
            target_id,
            amount,
            f"Начисление от разработчика ({'+' if amount > 0 else ''}{amount}⭐)",
        )
    except Exception as e:
        logger.error(f"dev_grant_stars_amount_handler: log: {e}")
    # Защита от ухода баланса в минус (на случай списаний).
    target = get_user(target_id) or target
    if target.stars_balance < 0:
        target.stars_balance = 0
        save_user(target)

    # Уведомляем самого пользователя.
    try:
        await context.bot.send_message(
            chat_id=int(target_id),
            text=(
                f"🎁 Разработчик {'начислил' if amount > 0 else 'списал'} "
                f"{abs(amount)}⭐ {'на ваш' if amount > 0 else 'с вашего'} баланс. "
                f"Текущий баланс: {target.stars_balance}⭐."
            ),
        )
    except Exception as e:
        logger.warning(f"dev_grant_stars_amount_handler: notify {target_id}: {e}")

    await update.message.reply_text(
        f"✅ Готово. Пользователю {target_id} зачислено {amount}⭐.\n"
        f"Новый баланс: {target.stars_balance}⭐.",
        reply_markup=get_developer_keyboard(),
    )
    context.user_data.pop('dev_grant_target_id', None)
    return DEV_PANEL


# ---------- ПУНКТ: сброс статистики Stars (без истории переводов) ----------

async def dev_reset_stars_stats_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разработчик запрашивает сброс статистики Stars — показываем подтверждение."""
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(DEVELOPER_ID):
        await query.answer("Только для разработчика.", show_alert=True)
        return DEV_PANEL

    stats = load_stars_stats()
    await query.edit_message_text(
        "🧹 **Сброс статистики Stars**\n\n"
        f"💸 Сейчас суммарно потрачено: {int(stats.get('total_stars_spent', 0))} ⭐\n"
        f"🏆 В топе: {len(stats.get('top_donors', []) or [])} пользователей\n\n"
        "Подробной истории переводов в боте больше нет и не ведётся. "
        "Сброс обнулит ОБЩУЮ сумму и ТОП. Действие необратимо.\n\n"
        "Сбросить статистику?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🧹 Да, сбросить", callback_data="dev_reset_stars_yes")],
            [InlineKeyboardButton("⬅️ Отмена", callback_data="dev_back_panel")],
        ]),
        parse_mode="Markdown",
    )
    return DEV_PANEL


async def dev_reset_stars_stats_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждённый полный сброс статистики Stars."""
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(DEVELOPER_ID):
        await query.answer("Только для разработчика.", show_alert=True)
        return DEV_PANEL

    reset_stars_stats()
    await query.edit_message_text(
        "✅ Статистика Stars сброшена: суммарные траты и топ обнулены.\n"
        "Подробная история переводов больше не ведётся.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ В панель", callback_data="dev_back_panel")]]),
    )
    return DEV_PANEL


# ---------- ПУНКТ 3 (саппорт, dev): входящие и ответы ----------

async def dev_support_inbox_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разработчик видит последние сообщения чата поддержки от пользователей."""
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(DEVELOPER_ID):
        await query.answer("Только для разработчика.", show_alert=True)
        return DEV_PANEL

    data = load_support_messages()
    # Собираем последние сообщения от пользователей.
    user_threads = []
    for uid, msgs in data.items():
        if not isinstance(msgs, list) or not msgs:
            continue
        last = msgs[-1]
        if last.get('from') == 'user':
            user_threads.append((uid, last.get('text', '')[:80], last.get('ts', '')))
    # Сортируем по времени (последние сверху).
    user_threads.sort(key=lambda t: t[2], reverse=True)

    if not user_threads:
        await query.edit_message_text(
            "💬 Чат поддержки пуст. Сообщений от пользователей пока нет.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="dev_back_panel")]]),
        )
        return DEV_PANEL

    rows = []
    for uid, preview, ts in user_threads[:20]:
        u = get_user(uid)
        name = u.first_name if u else uid
        if u and getattr(u, 'username', None):
            name += f" (@{u.username})"
        label = f"{name}: {preview}"[:60]
        rows.append([InlineKeyboardButton(label, callback_data=f"dev_support_reply_{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="dev_back_panel")])

    await query.edit_message_text(
        "💬 *Чат поддержки — входящие*\n\nВыберите пользователя для ответа:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown",
    )
    return DEV_PANEL


async def dev_support_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разработчик начинает ответ конкретному пользователю по саппорту."""
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(DEVELOPER_ID):
        await query.answer("Только для разработчика.", show_alert=True)
        return DEV_PANEL

    target_id = (query.data or "").replace("dev_support_reply_", "", 1).strip()
    if not target_id or not target_id.isdigit():
        await query.answer("Некорректный пользователь.", show_alert=True)
        return DEV_PANEL

    context.user_data['dev_support_target_id'] = target_id
    # Покажем последние 5 сообщений диалога с пользователем для контекста.
    data = load_support_messages()
    msgs = data.get(target_id, []) if isinstance(data, dict) else []
    tail = msgs[-5:] if isinstance(msgs, list) else []
    history = "\n".join(
        f"[{m.get('from','?')}] {m.get('text','')[:200]}" for m in tail
    ) or "(пусто)"

    await query.edit_message_text(
        f"✉️ Ответ пользователю id={target_id}.\n\n"
        f"Последние сообщения:\n{history}\n\n"
        f"Введите текст ответа одним сообщением:",
        reply_markup=get_cancel_keyboard(),
    )
    return DEV_SUPPORT_REPLY


@timeout(CONVERSATION_TIMEOUT)
async def dev_support_reply_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разработчик отправил текст ответа — доставляем пользователю и сохраняем."""
    if str(update.effective_user.id) != str(DEVELOPER_ID):
        await update.message.reply_text("Только для разработчика.")
        return DEV_PANEL

    target_id = context.user_data.get('dev_support_target_id')
    if not target_id:
        await update.message.reply_text("⚠️ Цель не задана.")
        return DEV_PANEL

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Пустой ответ. Введите текст.")
        return DEV_SUPPORT_REPLY
    if len(text) > 4000:
        await update.message.reply_text("Слишком длинный ответ (макс. 4000 символов).")
        return DEV_SUPPORT_REPLY

    try:
        append_support_message(target_id, "dev", text)
    except Exception as e:
        logger.error(f"dev_support_reply: append: {e}")

    try:
        await context.bot.send_message(
            chat_id=int(target_id),
            text=f"💬 Ответ от разработчика:\n\n{text}",
        )
        await update.message.reply_text("✅ Ответ отправлен.", reply_markup=get_developer_keyboard())
    except Exception as e:
        logger.error(f"dev_support_reply: deliver: {e}")
        await update.message.reply_text(
            f"⚠️ Не удалось доставить ответ пользователю: {e}",
            reply_markup=get_developer_keyboard(),
        )
    context.user_data.pop('dev_support_target_id', None)
    return DEV_PANEL


# ==================================
# === ГЛАВНАЯ ФУНКЦИЯ ===
# ==================================

def main():
    from telegram.ext import Defaults
    from telegram.request import HTTPXRequest

    # Один раз чистим существующие JSON от запрещённых символов, чтобы старые
    # расписания/ДЗ с `/` и т.п. перестали ронять отображение/редактирование.
    try:
        cleanup_existing_data_once()
    except Exception as e:
        logger.error(f"Не удалось выполнить очистку существующих данных: {e}")

    # HTTP-клиент для общения с Telegram. Большой пул соединений + быстрый
    # read_timeout = бот мгновенно реагирует на новые апдейты и не «зависает»
    # на медленных запросах.
    request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=5.0,
        connection_pool_size=256,
    )
    # Отдельный пул для get_updates (long-polling), чтобы он не конкурировал
    # с пулом для отправки сообщений — иначе под нагрузкой бот «засыпает».
    get_updates_request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=40.0,
        write_timeout=20.0,
        pool_timeout=5.0,
        connection_pool_size=8,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .request(request)
        .get_updates_request(get_updates_request)
        # concurrent_updates=True — апдейты от разных пользователей
        # обрабатываются ПАРАЛЛЕЛЬНО. Без этого один медленный запрос к Groq
        # блокировал всю очередь, и казалось, что бот «уснул» после нескольких
        # сообщений.
        .concurrent_updates(True)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("restore", restore_session),
            # ВАЖНО: MessageHandler здесь раньше «съедал» любой текст пользователя
            # ещё на стадии entry_points (из-за allow_reentry=True), из-за чего
            # хендлеры состояний типа ANONYMOUS_SEND_MESSAGE, SEND_CLASS_MESSAGE,
            # ENTER_BIRTHDAY и т.д. вообще не срабатывали — выглядело это как
            # «бот не реагирует и не отправляет ничего после любого вопроса».
            # Текстовый авто-возврат теперь живёт только в fallbacks, где он
            # триггерится корректно — после state-хендлеров.
            CallbackQueryHandler(handle_callback),
        ],
        states={
            ENTER_BIRTHDAY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_birthday_handler),
                # ПУНКТ 7: чтобы работала кнопка «Отмена» во время ввода даты рождения
                CallbackQueryHandler(handle_callback),
            ],
            SET_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_time_handler),
                # ПУНКТ 7: чтобы работала кнопка «Отмена» при настройке времени
                CallbackQueryHandler(handle_callback),
            ],
            MAIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu),
                # Фото в режиме AI: добавлено для поддержки Groq Vision —
                # пользователь может прислать фото в чат с AI и получить ответ.
                # НОВОЕ: картинки-документы (скриншоты «файлом») тоже поддержаны.
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_main_menu_photo),
                CallbackQueryHandler(handle_callback),
            ],
            CLASS_MANAGEMENT: [
                CallbackQueryHandler(handle_callback),
            ],
            CREATE_CLASS_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, create_class_handler),
                CallbackQueryHandler(handle_callback),
            ],
            JOIN_CLASS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, join_class_handler),
                CallbackQueryHandler(handle_callback),
            ],
            ADMIN_PANEL: [
                CallbackQueryHandler(handle_callback),
            ],
            SEND_CLASS_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, send_class_message_handler),
                CallbackQueryHandler(handle_callback),
            ],
            EDIT_SCHEDULE: [
                CallbackQueryHandler(handle_callback),
            ],
            EDIT_SCHEDULE_CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_schedule_content_handler),
                CallbackQueryHandler(handle_callback),
            ],
            EDIT_TEACHERS: [
                CallbackQueryHandler(handle_callback),
            ],
            EDIT_TEACHER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_teacher_handler),
                CallbackQueryHandler(handle_callback),
            ],
            EDIT_TEACHER_SUBJECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_teacher_subject_handler),
                CallbackQueryHandler(handle_callback),
            ],
            EDIT_BELLS: [
                CallbackQueryHandler(handle_callback),
            ],
            EDIT_BELL_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_bell_time_handler),
                CallbackQueryHandler(handle_callback),
            ],
            # ИСПРАВЛЕНО (бесконечный цикл звонков): ввод времени ОКОНЧАНИЯ —
            # отдельное состояние. Раньше оно жило в EDIT_BELL_TIME и ввод конца
            # обрабатывался хендлером начала, зацикливая FSM навсегда.
            EDIT_BELL_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_bell_end_handler),
                CallbackQueryHandler(handle_callback),
            ],
            SET_HOLIDAYS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_holidays_handler),
                CallbackQueryHandler(handle_callback),
            ],
            MANAGE_ADMINS: [
                CallbackQueryHandler(handle_callback),
            ],
            MANAGE_CUSTOM_BUTTONS: [
                CallbackQueryHandler(handle_callback),
            ],
            CUSTOM_BUTTON_SELECT_TYPE: [
                CallbackQueryHandler(handle_callback),
            ],
            CUSTOM_BUTTON_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, custom_button_name_handler),
                CallbackQueryHandler(handle_callback),
            ],
            CUSTOM_BUTTON_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, custom_button_url_handler),
                CallbackQueryHandler(handle_callback),
            ],
            CUSTOM_BUTTON_CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, custom_button_content_handler),
                CallbackQueryHandler(handle_callback),
            ],
            ADMIN_DELETE_BUTTON: [
                CallbackQueryHandler(handle_callback),
            ],
            MANAGE_HOMEWORK: [
                CallbackQueryHandler(handle_callback),
            ],
            ADD_HOMEWORK: [
                # ПУНКТ 9: универсальный текстовый обработчик распределяет ввод
                # между добавлением нового предмета, кастомной даты и текстом ДЗ.
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_homework_date_handler),
                CallbackQueryHandler(handle_callback),
            ],
            DELETE_HOMEWORK_SELECT: [
                CallbackQueryHandler(handle_callback),
            ],
            MANAGE_CLASS_USERS: [
                CallbackQueryHandler(handle_callback),
            ],
            WEEK_SCHEDULE: [
                CallbackQueryHandler(handle_week_schedule),
                # Если пользователь нажал текстовую кнопку главного меню
                # (а не inline-кнопку дня недели) — корректно возвращаемся
                # в главное меню и обрабатываем эту кнопку, а не «съедаем» текст.
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu),
            ],
            QUICK_ADD_HOMEWORK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_homework_handler),
                CallbackQueryHandler(handle_callback),
            ],
            ENTER_HOMEWORK_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_homework_date_handler),
                CallbackQueryHandler(handle_callback),
            ],
            USER_SETTINGS: [
                CallbackQueryHandler(handle_callback),
            ],
            CHANGE_BUTTON_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, rename_button_handler),
                CallbackQueryHandler(handle_callback),
            ],
            CHANGE_BUTTON_LAYOUT: [
                CallbackQueryHandler(handle_callback),
            ],
            REORDER_BUTTONS: [
                CallbackQueryHandler(handle_callback),
            ],
            MOVE_BUTTONS: [
                CallbackQueryHandler(handle_callback),
            ],
            MANAGE_BUTTON_VISIBILITY: [
                CallbackQueryHandler(handle_callback),
            ],
            SET_BIRTHDAY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_birthday_handler),
                CallbackQueryHandler(handle_callback),
            ],
            SET_BIRTHDAY_NOTIFICATION_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_birthday_notification_time_handler),
                CallbackQueryHandler(handle_callback),
            ],
            NOTIFICATION_SETTINGS: [
                CallbackQueryHandler(handle_callback),
            ],
            SET_MORNING_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_morning_time_handler),
                CallbackQueryHandler(handle_callback),
            ],
            SET_EVENING_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_evening_time_handler),
                CallbackQueryHandler(handle_callback),
            ],
            SET_MORNING_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_morning_text_handler),
                CallbackQueryHandler(handle_callback),
            ],
            SET_EVENING_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_evening_text_handler),
                CallbackQueryHandler(handle_callback),
            ],
            SUGGEST_FUNCTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, suggest_function_handler),
                CallbackQueryHandler(handle_callback),
            ],
            CHANGE_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, change_time_handler),
                CallbackQueryHandler(handle_callback),
            ],
            CHANGE_LANGUAGE: [
                CallbackQueryHandler(handle_callback),
            ],
            TIMER_SET_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, timer_set_date_handler),
                CallbackQueryHandler(handle_callback),
            ],
            TIMER_SET_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, timer_set_time_handler),
                CallbackQueryHandler(handle_callback),
            ],
            TIMER_SET_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, timer_set_text_handler),
                CallbackQueryHandler(handle_callback),
            ],
            ANONYMOUS_SELECT_USER: [
                CallbackQueryHandler(handle_callback),
            ],
            ANONYMOUS_SEND_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, send_anonymous_message_handler),
                CallbackQueryHandler(handle_callback),
            ],
            PERSONAL_BUTTON_MANAGEMENT: [
                CallbackQueryHandler(handle_callback),
            ],
            PERSONAL_BUTTON_SELECT_TYPE: [
                CallbackQueryHandler(handle_callback),
            ],
            CREATE_PERSONAL_BUTTON_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, create_personal_button_name_handler),
                CallbackQueryHandler(handle_callback),
            ],
            CREATE_PERSONAL_BUTTON_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, create_personal_button_url_handler),
                CallbackQueryHandler(handle_callback),
            ],
            CREATE_PERSONAL_BUTTON_CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, create_personal_button_content_handler),
                CallbackQueryHandler(handle_callback),
            ],
            MANAGE_PERSONAL_BUTTONS: [
                CallbackQueryHandler(handle_callback),
            ],
            EDIT_PERSONAL_BUTTON: [
                CallbackQueryHandler(handle_callback),
            ],
            EDIT_PERSONAL_BUTTON_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_personal_button_edit),
                CallbackQueryHandler(handle_callback),
            ],
            EDIT_PERSONAL_BUTTON_CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_personal_button_edit),
                CallbackQueryHandler(handle_callback),
            ],
            EDIT_PERSONAL_BUTTON_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_personal_button_url),
                CallbackQueryHandler(handle_callback),
            ],
            DEV_PANEL: [
                CallbackQueryHandler(handle_callback),
            ],
            DEV_BROADCAST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_broadcast_handler),
                CallbackQueryHandler(handle_callback),
            ],
            DEV_CLASS_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_class_message_handler),
                CallbackQueryHandler(handle_callback),
            ],
            DEV_DELETE_CLASS: [
                CallbackQueryHandler(handle_callback),
            ],
            DEV_USER_MANAGEMENT: [
                CallbackQueryHandler(handle_callback),
            ],
            DEV_BLOCK_USER: [
                CallbackQueryHandler(handle_callback),
            ],
            DEV_BLOCK_USER_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_block_user_price_handler),
                CallbackQueryHandler(handle_callback),
            ],
            DEV_SET_PRICES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_set_prices_handler),
                CallbackQueryHandler(handle_callback),
            ],
            DEV_QUICK_PRICE_SELECT: [
                CallbackQueryHandler(handle_callback),
            ],
            DEV_QUICK_PRICE_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_quick_price_value_handler),
                CallbackQueryHandler(handle_callback),
            ],
            DEV_EDIT_INSTRUCTIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_edit_instructions_handler),
                CallbackQueryHandler(handle_callback),
            ],
            DEV_MESSAGE_USER_SELECT: [
                CallbackQueryHandler(handle_callback),
            ],
            DEV_MESSAGE_USER_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_message_user_text_handler),
                CallbackQueryHandler(handle_callback),
            ],
            CREATE_GLOBAL_BUTTON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_global_button_handler),
                CallbackQueryHandler(handle_callback),
            ],
            CREATE_GLOBAL_BUTTON_TYPE: [
                CallbackQueryHandler(handle_callback),
            ],
            CREATE_GLOBAL_BUTTON_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_global_button_url_handler),
                CallbackQueryHandler(handle_callback),
            ],
            CREATE_GLOBAL_BUTTON_CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_global_button_content_handler),
                CallbackQueryHandler(handle_callback),
            ],
            ENTER_VIEW_SENDER_MESSAGE_ID: [
                CallbackQueryHandler(handle_callback),
            ],
            PURCHASE_BUTTON: [
                CallbackQueryHandler(handle_callback),
            ],
            PURCHASE_UNBLOCK: [
                CallbackQueryHandler(handle_callback),
            ],
            PURCHASE_VIEW_SENDER: [
                CallbackQueryHandler(handle_callback),
            ],
            SHOW_INSTRUCTIONS: [
                CallbackQueryHandler(handle_callback),
            ],
            AI_CHAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_message),
                # ИСПРАВЛЕНО («ии не видит изображение»): в состоянии AI_CHAT не
                # было фото-хендлера — картинка молча падала в fallback и бот
                # не отвечал, а вижн-запросы из главного меню без OCR падали с
                # «AI временно недоступен». Теперь фото И картинки-документы
                # принимаются прямо в чате: OCR → DeepSeek, иначе — вижн Groq.
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_ai_message),
                CallbackQueryHandler(handle_callback),
            ],
            # === НОВОЕ: 🪄 Автоматизация (DeepSeek → действия бота) ===
            AI_AUTOMATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, automation_text_handler),
                CallbackQueryHandler(handle_callback),
            ],
            # === НОВЫЕ состояния для функционала «Погода» и «Праздники» ===
            ENTER_CITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_city_handler),
                CallbackQueryHandler(handle_callback),
            ],
            CHANGE_CITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, change_city_handler),
                CallbackQueryHandler(handle_callback),
            ],
            SET_WEATHER_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_weather_time_handler),
                CallbackQueryHandler(handle_callback),
            ],
            DEV_HOLIDAY_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_holiday_date_handler),
                CallbackQueryHandler(handle_callback),
            ],
            DEV_HOLIDAY_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_holiday_text_handler),
                CallbackQueryHandler(handle_callback),
            ],
            DEV_HOLIDAY_DELETE: [
                CallbackQueryHandler(handle_callback),
            ],
            DEV_INSTANT_BROADCAST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_instant_broadcast_handler),
                CallbackQueryHandler(handle_callback),
            ],
            # === НОВЫЕ состояния (тема, поддержка, dev grant) ===
            SUPPORT_CHAT_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, support_chat_message_handler),
                CallbackQueryHandler(handle_callback),
            ],
            DEV_GRANT_STARS_USER_PICK: [
                CallbackQueryHandler(handle_callback),
            ],
            DEV_GRANT_STARS_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_grant_stars_amount_handler),
                CallbackQueryHandler(handle_callback),
            ],
            DEV_SUPPORT_REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dev_support_reply_message_handler),
                CallbackQueryHandler(handle_callback),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel_command),
            # ПУНКТ: если пользователь зашёл в какое-то состояние и пишет текст, который
            # не подходит ни под один обработчик этого состояния — не бросаем его «в никуда»,
            # а возвращаем в главное меню, чтобы не приходилось слать /start.
            MessageHandler(filters.TEXT & ~filters.COMMAND, auto_reenter_main_menu),
            CallbackQueryHandler(handle_callback),
        ],
        # conversation_timeout сознательно не задаём: ранее установленный таймаут
        # приводил к тому, что разговор мог молча завершиться и бот переставал
        # отвечать до следующего /start.
        allow_reentry=True,
    )

    # ==================================
    # === БЫСТРЫЕ КОМАНДЫ ИЗ ЛЮБОГО СОСТОЯНИЯ ===
    # ==================================
    # Инжектируем перехватчик быстрых команд («👨‍🏫 Учителя», «⏰ Таймер»,
    # «📝 Домашнее задание» и т. д.) ПЕРВЫМ хендлером в каждое состояние,
    # где есть текстовый ввод. Telegram-кнопка главного меню теперь работает
    # из любого режима — старые функции ничего не потеряли.
    def _inject_quick_commands(states_dict):
        pattern = build_quick_commands_pattern()
        quick_filter = filters.Regex(pattern)
        injected = 0
        for _state, _handlers in states_dict.items():
            if _state == MAIN_MENU:
                # Главное меню уже само обрабатывает все кнопки.
                continue
            has_text_input = any(
                isinstance(h, MessageHandler) for h in _handlers
            )
            if has_text_input:
                _handlers.insert(0, MessageHandler(quick_filter, handle_quick_command))
                injected += 1
        logger.info(f"Быстрые команды инжектированы в {injected} состояний.")

    try:
        _inject_quick_commands(conv_handler.states)
    except Exception as e:
        logger.error(f"Не удалось инжектировать быстрые команды: {e}")

    application.add_handler(conv_handler)

    # 🎙 ГОЛОСОВОЙ ВВОД: расшифровка голосовых/аудио (Groq Whisper) в группе -1,
    # ДО ConversationHandler — к моменту выбора состояния сообщение уже
    # превращено в текст, поэтому голосом можно управлять ЛЮБЫМ разделом:
    # автоматизацией, чатом ИИ, вводом ДЗ, таймером и т. д. Без ключа GROQ
    # middleware честно отвечает и глушит апдейт, не ломая FSM.
    application.add_handler(
        TypeHandler(Update, _voice_transcription_middleware), group=-1
    )

    # ПУНКТ 8: глобальный обработчик кнопки «Разблокироваться» — работает даже
    # когда пользователь вне ConversationHandler (после блокировки conv завершён).
    application.add_handler(CallbackQueryHandler(unblock_self_handler, pattern="^unblock_self$"))

    # Глобальный обработчик кнопки «✅ Я подписался(ась)» — работает даже когда
    # пользователь не внутри ConversationHandler (например, сразу после блокировки
    # подпиской на канал).
    application.add_handler(CallbackQueryHandler(check_subscription_handler, pattern="^check_subscription$"))

    # === НОВОЕ: глобальный обработчик кнопки «Узнать на 3 дня» под утренним
    # погодным уведомлением. Работает, даже если пользователь не находится
    # в активном разговоре (например, нажал на кнопку через сутки после
    # получения уведомления).
    async def _weather_3days_global(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass
        user_id = str(query.from_user.id)
        user = get_user(user_id)
        if not user or not getattr(user, 'city', None):
            try:
                await query.message.reply_text(
                    "🏙 Город не установлен. Откройте ⚙️ Настройки → 🌦 Настройки погоды."
                )
            except Exception:
                pass
            return
        text = await weather_forecast_text(user.city, days=3)
        try:
            await query.message.reply_text(text)
        except Exception as e:
            logger.error(f"Ошибка прогноза 3 дней (global): {e}")

    application.add_handler(CallbackQueryHandler(_weather_3days_global, pattern="^weather_3days$"))

    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    application.add_error_handler(error_handler)

    logger.info("Запуск бота...")

    # --- Совместимость с Python 3.14 ---------------------------------------
    # python-telegram-bot 21.x внутри Application.run_polling() вызывает
    # asyncio.get_event_loop(). В Python 3.13 это ещё создавало новый цикл
    # автоматически, но в Python 3.14 такое поведение убрали и вызов падает с
    # RuntimeError: There is no current event loop in thread 'MainThread'.
    # Поэтому заранее создаём event loop сами и привязываем его к главному
    # потоку — тогда run_polling() его подхватит как раньше.
    # На Python <= 3.13 этот блок безопасен: get_event_loop() либо вернёт уже
    # существующий цикл, либо создаст новый.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    # ------------------------------------------------------------------------

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        # poll_interval=0.0 — сразу запрашиваем следующий long-poll апдейт без задержки,
        # чтобы бот реагировал мгновенно. timeout=30 — long-poll держит соединение,
        # так что лишних HTTP-запросов это не создаёт.
        poll_interval=0.0,
        timeout=30,
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    error_text = ''.join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
    logger.error(f"Ошибка: {context.error}\n{error_text}")

    if update:
        try:
            user_id = update.effective_user.id if update.effective_user else None
            if user_id:
                user = get_user(str(user_id))
                keyboard = get_main_menu_keyboard(user) if user else None
                await context.bot.send_message(
                    chat_id=user_id,
                    text="Попробуйте ещё раз или нажмите /start",
                    reply_markup=keyboard
                )
        except Exception:
            pass


if __name__ == "__main__":
    main()
