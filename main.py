# -*- coding: utf-8 -*-
"""
P2P Exchange Rate Telegram Bot
Production-grade: thread-safe, Render-ready, MongoDB-backed.
"""

import os
import re
import html
import time
import secrets
import logging
import threading
import concurrent.futures
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, Any

import certifi
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
import telebot
from telebot import types
from flask import Flask, jsonify, request, abort
from pymongo import MongoClient

# ═══════════════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════════════
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("p2p_bot")

_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not _TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан в переменных окружения")

TELEGRAM_TOKEN: str    = _TOKEN
MY_ADMIN_ID: int       = 5266659205
ADMIN_LOG_CHAT_ID: int = -1003264764082
MOSCOW_TZ              = timezone(timedelta(hours=3))


def _resolve_render_url() -> str:
    """
    Автоматически определяет актуальный URL сервиса на Render.
    Приоритет у системной переменной RENDER_EXTERNAL_URL, чтобы при переносе
    на другой аккаунт старый RENDER_URL не ломал вебхук и self-ping.
    """
    auto_url   = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    manual_url = os.getenv("RENDER_URL", "").strip().rstrip("/")
    if auto_url:
        if manual_url and manual_url != auto_url:
            logger.warning(
                "⚠️ RENDER_URL (%s) отличается от системного RENDER_EXTERNAL_URL (%s). "
                "Используется актуальный адрес: %s",
                manual_url, auto_url, auto_url,
            )
        return auto_url
    return manual_url


RENDER_URL: str = _resolve_render_url()

ABCEX_API_KEY:    str = os.getenv("ABCEX_API_KEY", "").strip()
ABCEX_API_SECRET: str = os.getenv("ABCEX_API_SECRET", "").strip()
ABCEX_PROXY:      str = os.getenv("ABCEX_PROXY", "").strip()

_raw_secret = re.sub(r"[^A-Za-z0-9_-]", "", os.getenv("WEBHOOK_SECRET", "").strip())
WEBHOOK_SECRET: str = _raw_secret or secrets.token_urlsafe(32)
WEBHOOK_PATH:   str = f"/webhook/{TELEGRAM_TOKEN}"

_API_TIMEOUT  = 8
_API_MAX_WAIT = 12

_DATA_LOCK = threading.Lock()

# Постоянные пулы потоков
_API_POOL     = concurrent.futures.ThreadPoolExecutor(max_workers=12, thread_name_prefix="api_pool")
_WEBHOOK_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=10, thread_name_prefix="webhook_pool")
_BG_POOL      = concurrent.futures.ThreadPoolExecutor(max_workers=6,  thread_name_prefix="bg_io_pool")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=True)

if ABCEX_API_KEY:
    logger.info("✅ ABCEX_API_KEY обнаружен (длина=%d)", len(ABCEX_API_KEY))
if ABCEX_PROXY:
    logger.info("✅ ABCEX_PROXY задан (через %s)", ABCEX_PROXY.split("@")[-1])
if RENDER_URL:
    logger.info("🌐 Актуальный URL сервиса: %s", RENDER_URL)

# ═══════════════════════════════════════════════════════════════════════
#  MONGODB
# ═══════════════════════════════════════════════════════════════════════
_MONGO_URI = os.getenv("MONGO_URI", "").strip()
users_col  = None
auto_col   = None

if _MONGO_URI:
    try:
        _mongo = MongoClient(
            _MONGO_URI,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=5_000,
            socketTimeoutMS=10_000,
            maxPoolSize=10,
            retryWrites=True,
        )
        _mongo.server_info()
        _db       = _mongo["p2p_bot_db"]
        users_col = _db["users"]
        auto_col  = _db["auto_updates"]
        logger.info("✅ MongoDB Atlas: подключено")
    except Exception as _exc:
        logger.error("❌ MongoDB недоступна: %s", _exc)
else:
    logger.warning("⚠️ MONGO_URI не задан — данные только в RAM")

# ═══════════════════════════════════════════════════════════════════════
#  ЛОКАЛИЗАЦИЯ
# ═══════════════════════════════════════════════════════════════════════
LANGS: Dict[str, Dict[str, str]] = {
    "ru": {
        "btn_show":        "📊 Показать курс",
        "btn_auto":        "🔔 Автообновление",
        "btn_profile":     "👤 Профиль",
        "btn_feedback":    "✍️ Отзыв",
        "btn_disable":     "🚫 Отключить уведомления",
        "btn_admin":       "🛠 Админка",
        "btn_cancel":      "⬅️ Назад / Отмена",
        "btn_refresh_inl": "🔄 Обновить курс",
        "btn_auto_inl":    "🔔 Автообновление",
        "btn_lang_inl":    "🌐 Сменить язык",
        "btn_close_inl":   "✖️ Закрыть",
        "welcome":         "👋 Привет!\n\nВыбери нужный раздел ниже 👇",
        "error_fetch":     "⚠️ Не удалось получить данные. Попробуйте позже.",
        "rate_title":      "💱 <b>АКТУАЛЬНЫЕ КУРСЫ</b>",
        "updated":         "⏱ Обновлено:",
        "contact":         "💰 Обмен любых сумм и валют — по договорённости.\n📞 Контакт: @Abdulkhaiii",
        "auto_menu":       "🔔 <b>Настройка автообновления</b>",
        "auto_choose":     "⬇️ Выбери интервал отправки курса:",
        "auto_status_on":  "🟢 Сейчас включено: <b>каждые {h}H</b>",
        "auto_status_off": "🔴 Сейчас уведомления <b>выключены</b>",
        "auto_off_msg":    "Автообновление выключено.",
        "auto_on_msg":     "Уведомление включено каждые",
        "feedback_ask":    "✍️ Напишите ваш отзыв одним сообщением\n<i>(или нажмите «⬅️ Назад / Отмена» для возврата)</i>:",
        "feedback_ok":     "✅ Спасибо! Ваш отзыв передан администратору.",
        "action_canceled": "↩️ Действие отменено. Вы в главном меню.",
        "menu_updated":    "🔄 Меню обновлено.",
        "prof_title":      "👤 <b>Ваш профиль</b>",
        "prof_reqs":       "Всего запросов:",
        "prof_last":       "Последний запрос:",
        "prof_join":       "В боте с:",
        "prof_auto":       "Автообновление:",
        "buy":             "Покупка",
        "sell":            "Продажа",
        "no_data":         "нет данных",
    },
    "en": {
        "btn_show":        "📊 Show Rates",
        "btn_auto":        "🔔 Auto-updates",
        "btn_profile":     "👤 Profile",
        "btn_feedback":    "✍️ Feedback",
        "btn_disable":     "🚫 Disable Alerts",
        "btn_admin":       "🛠 Admin Panel",
        "btn_cancel":      "⬅️ Back / Cancel",
        "btn_refresh_inl": "🔄 Refresh Rates",
        "btn_auto_inl":    "🔔 Auto-updates",
        "btn_lang_inl":    "🌐 Change Language",
        "btn_close_inl":   "✖️ Close",
        "welcome":         "👋 Hello!\n\nSelect a section below 👇",
        "error_fetch":     "⚠️ Failed to get data. Please try again later.",
        "rate_title":      "💱 <b>CURRENT RATES</b>",
        "updated":         "⏱ Updated:",
        "contact":         "💰 Exchange of any amounts — by agreement.\n📞 Contact: @Abdulkhaiii",
        "auto_menu":       "🔔 <b>Auto-updates Settings</b>",
        "auto_choose":     "⬇️ Choose interval:",
        "auto_status_on":  "🟢 Currently enabled: <b>every {h}H</b>",
        "auto_status_off": "🔴 Alerts are currently <b>disabled</b>",
        "auto_off_msg":    "Auto-updates disabled.",
        "auto_on_msg":     "Alerts enabled every",
        "feedback_ask":    "✍️ Write your feedback in one message\n<i>(or press «⬅️ Back / Cancel» to return)</i>:",
        "feedback_ok":     "✅ Thank you! Feedback sent to admin.",
        "action_canceled": "↩️ Action canceled. Back to main menu.",
        "menu_updated":    "🔄 Menu updated.",
        "prof_title":      "👤 <b>Your Profile</b>",
        "prof_reqs":       "Total requests:",
        "prof_last":       "Last request:",
        "prof_join":       "Member since:",
        "prof_auto":       "Auto-updates:",
        "buy":             "Buy",
        "sell":            "Sell",
        "no_data":         "no data",
    },
}

# Множество всех служебных текстов кнопок для защиты от случайной отправки в отзывах/рассылке
_ALL_MENU_BUTTONS = {
    LANGS[lg][k]
    for lg in LANGS
    for k in ("btn_show", "btn_auto", "btn_profile", "btn_feedback", "btn_disable", "btn_admin", "btn_cancel")
}
_CANCEL_TEXTS = {
    LANGS["ru"]["btn_cancel"],
    LANGS["en"]["btn_cancel"],
    "/cancel",
    "отмена",
    "назад",
    "cancel",
    "back",
}

# ═══════════════════════════════════════════════════════════════════════
#  RAM-ХРАНИЛИЩЕ И КЭШ
# ═══════════════════════════════════════════════════════════════════════
USER_DATA:    Dict[int, Dict[str, Any]] = {}
AUTO_USERS:   Dict[int, Dict[str, Any]] = {}
ALL_USER_IDS: set                       = set()

_krw_google_cache: Dict[str, Any] = {"value": None, "updated": None}
_krw_google_lock       = threading.Lock()
_krw_google_refreshing = threading.Event()

# ═══════════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════
def now_msk() -> datetime:
    return datetime.now(MOSCOW_TZ)


def fmt_num(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{decimals}f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def fmt_dt(dt: Optional[datetime], seconds: bool = True) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d.%m.%Y  %H:%M:%S" if seconds else "%d.%m.%Y  %H:%M")


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MOSCOW_TZ)
        return dt
    except (ValueError, TypeError):
        return None


def _elapsed_sec(dt: Optional[datetime]) -> float:
    if dt is None:
        return float("inf")
    try:
        ref = dt if dt.tzinfo else dt.replace(tzinfo=MOSCOW_TZ)
        return max(0.0, (now_msk() - ref).total_seconds())
    except Exception:
        return float("inf")


def _safe_html(text: Optional[str], default: str = "—") -> str:
    if not text:
        return default
    return html.escape(str(text))


def _is_cancel_or_menu(text: Optional[str]) -> bool:
    if not text:
        return False
    t = text.strip()
    return t.lower() in _CANCEL_TEXTS or t in _ALL_MENU_BUTTONS or t.startswith("/")


# ═══════════════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════════════
def load_db() -> None:
    if users_col is None or auto_col is None:
        logger.warning("load_db: MongoDB недоступна, пропускаем")
        return
    try:
        logger.info("Загрузка данных из MongoDB...")
        with _DATA_LOCK:
            for doc in users_col.find():
                uid = doc["_id"]
                ALL_USER_IDS.add(uid)
                USER_DATA[uid] = {
                    "lang":       doc.get("lang", "ru"),
                    "requests":   int(doc.get("requests", 0)),
                    "last":       _parse_iso(doc.get("last")),
                    "joined":     _parse_iso(doc.get("joined")) or now_msk(),
                    "first_name": doc.get("first_name"),
                    "username":   doc.get("username"),
                }
            for doc in auto_col.find():
                uid = doc["_id"]
                ALL_USER_IDS.add(uid)
                AUTO_USERS[uid] = {
                    "interval":   int(doc["interval"]),
                    "last":       _parse_iso(doc.get("last"))       or now_msk(),
                    "enabled_at": _parse_iso(doc.get("enabled_at")) or now_msk(),
                }
        logger.info("✅ БД загружена: %d профилей, %d подписок", len(USER_DATA), len(AUTO_USERS))
    except Exception as exc:
        logger.error("❌ load_db: %s", exc)


def _db_write_user(uid: int) -> None:
    if users_col is None:
        return
    try:
        with _DATA_LOCK:
            u = USER_DATA.get(uid)
            if not u:
                return
            u_copy = u.copy()
        users_col.update_one(
            {"_id": uid},
            {"$set": {
                "lang":       u_copy["lang"],
                "requests":   u_copy["requests"],
                "last":       u_copy["last"].isoformat()   if u_copy.get("last")   else None,
                "joined":     u_copy["joined"].isoformat() if u_copy.get("joined") else None,
                "first_name": u_copy.get("first_name"),
                "username":   u_copy.get("username"),
            }},
            upsert=True,
        )
    except Exception as exc:
        logger.error("save_user(%d): %s", uid, exc)


def save_user(uid: int) -> None:
    _BG_POOL.submit(_db_write_user, uid)


def _db_write_auto(uid: int) -> None:
    if auto_col is None:
        return
    try:
        with _DATA_LOCK:
            entry = AUTO_USERS.get(uid)
            entry_copy = entry.copy() if entry else None
        if entry_copy:
            auto_col.update_one(
                {"_id": uid},
                {"$set": {
                    "interval":   entry_copy["interval"],
                    "last":       entry_copy["last"].isoformat()       if entry_copy.get("last")       else None,
                    "enabled_at": entry_copy["enabled_at"].isoformat() if entry_copy.get("enabled_at") else None,
                }},
                upsert=True,
            )
        else:
            auto_col.delete_one({"_id": uid})
    except Exception as exc:
        logger.error("save_auto(%d): %s", uid, exc)


def save_auto(uid: int) -> None:
    _BG_POOL.submit(_db_write_auto, uid)


# ═══════════════════════════════════════════════════════════════════════
#  ПОЛЬЗОВАТЕЛЬ
# ═══════════════════════════════════════════════════════════════════════
def init_user(tg_user: types.User) -> None:
    uid = tg_user.id
    with _DATA_LOCK:
        ALL_USER_IDS.add(uid)
        if uid in USER_DATA:
            changed = False
            if tg_user.first_name and USER_DATA[uid].get("first_name") != tg_user.first_name:
                USER_DATA[uid]["first_name"] = tg_user.first_name
                changed = True
            if tg_user.username != USER_DATA[uid].get("username"):
                USER_DATA[uid]["username"] = tg_user.username
                changed = True
            if changed:
                save_user(uid)
            return

    if users_col is not None:
        try:
            doc = users_col.find_one({"_id": uid})
            if doc:
                profile: Dict[str, Any] = {
                    "lang":       doc.get("lang", "ru"),
                    "requests":   int(doc.get("requests", 0)),
                    "last":       _parse_iso(doc.get("last")),
                    "joined":     _parse_iso(doc.get("joined")) or now_msk(),
                    "first_name": tg_user.first_name or doc.get("first_name"),
                    "username":   tg_user.username or doc.get("username"),
                }
                with _DATA_LOCK:
                    USER_DATA[uid] = profile
                logger.info("↩️ Профиль %d восстановлен из MongoDB", uid)
                return
        except Exception as exc:
            logger.error("init_user read(%d): %s", uid, exc)

    new_profile: Dict[str, Any] = {
        "lang":       "ru",
        "requests":   0,
        "last":       None,
        "joined":     now_msk(),
        "first_name": tg_user.first_name,
        "username":   tg_user.username,
    }
    with _DATA_LOCK:
        if uid not in USER_DATA:
            USER_DATA[uid] = new_profile
    save_user(uid)
    logger.info("🆕 Новый профиль: %d (%s)", uid, tg_user.first_name)


def get_lang(uid: int) -> str:
    with _DATA_LOCK:
        lang = USER_DATA.get(uid, {}).get("lang", "ru")
        return lang if lang in LANGS else "ru"


# ═══════════════════════════════════════════════════════════════════════
#  ЛОГИРОВАНИЕ В КАНАЛ
# ═══════════════════════════════════════════════════════════════════════
def log_action(tg_user: types.User, action: str, result: Optional[str] = None) -> None:
    def _send() -> None:
        try:
            first = _safe_html(tg_user.first_name, "")
            last  = f" {_safe_html(tg_user.last_name, '')}" if tg_user.last_name else ""
            nick  = f"@{_safe_html(tg_user.username)}" if tg_user.username else "—"
            lines = [
                "⚙️ <b>Лог действия</b>",
                f"👤 {first}{last}  |  {nick}  |  <code>{tg_user.id}</code>",
                f"🔘 <b>{_safe_html(action)}</b>",
            ]
            if result:
                lines.append(f"📊 {_safe_html(result)}")
            lines.append(f"🕒 {now_msk().strftime('%d.%m.%Y %H:%M:%S')} МСК")
            bot.send_message(ADMIN_LOG_CHAT_ID, "\n".join(lines), parse_mode="HTML")
        except Exception:
            pass

    _BG_POOL.submit(_send)


# ═══════════════════════════════════════════════════════════════════════
#  UI И КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════════════
def main_keyboard(uid: int) -> types.ReplyKeyboardMarkup:
    T  = LANGS[get_lang(uid)]
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(T["btn_show"],    T["btn_auto"])
    kb.row(T["btn_profile"], T["btn_feedback"])
    kb.row(T["btn_disable"])
    if uid == MY_ADMIN_ID:
        kb.row(T["btn_admin"])
    return kb


def cancel_reply_keyboard(uid: int) -> types.ReplyKeyboardMarkup:
    """Клавиатура с одной кнопкой «Назад / Отмена» во время ввода текста."""
    T  = LANGS[get_lang(uid)]
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(T["btn_cancel"])
    return kb


def cancel_inline_keyboard(uid: int, callback_data: str = "cancel_step") -> types.InlineKeyboardMarkup:
    """Инлайн-кнопка отмены под сообщением запроса ввода."""
    T  = LANGS[get_lang(uid)]
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(T["btn_cancel"], callback_data=callback_data))
    return kb


def rate_inline_keyboard(uid: int) -> types.InlineKeyboardMarkup:
    """Удобные кнопки прямо под сообщением с курсом."""
    T  = LANGS[get_lang(uid)]
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(T["btn_refresh_inl"], callback_data="rate_refresh"),
        types.InlineKeyboardButton(T["btn_auto_inl"],    callback_data="open_auto_menu"),
    )
    return kb


def admin_main_inline_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 Статистика",             callback_data="adm_stat"),
        types.InlineKeyboardButton("👥 База пользователей",     callback_data="adm_users_0"),
        types.InlineKeyboardButton("📢 Рассылка всем",          callback_data="adm_bc"),
        types.InlineKeyboardButton("🔔 Упр. подписками (тихо)", callback_data="adm_auto_menu"),
        types.InlineKeyboardButton("✖️ Закрыть панель",         callback_data="close_msg"),
    )
    return kb


# ═══════════════════════════════════════════════════════════════════════
#  API — ПОЛУЧЕНИЕ КУРСОВ
# ═══════════════════════════════════════════════════════════════════════
_thread_local = threading.local()


def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        retry = Retry(
            total=1,
            backoff_factor=0.3,
            status_forcelist=(502, 503, 504),
            allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Cookie": "SOCS=CAISHAgBEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjYwMTAxLjAxX3AxGgVydS1SVSgB; AEC=AVYB7coM1X; CONSENT=YES+cb.20260101-00-p0.ru+FX+999",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        })
        _thread_local.session = s
    return _thread_local.session


def _fetch_upbit() -> Optional[float]:
    try:
        r = _get_session().get("https://api.upbit.com/v1/ticker?markets=KRW-USDT", timeout=_API_TIMEOUT)
        r.raise_for_status()
        return float(r.json()[0]["trade_price"])
    except Exception as exc:
        logger.warning("Upbit Error: %s", exc)
        return None


def _fetch_bithumb() -> Optional[float]:
    try:
        r = _get_session().get("https://api.upbit.com/v1/ticker?markets=KRW-USDT", timeout=_API_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            val = float(data[0].get("trade_price", 0))
            if val > 0:
                return val
    except Exception:
        pass
    try:
        r = _get_session().get("https://api.bithumb.com/public/ticker/USDT_KRW", timeout=_API_TIMEOUT)
        r.raise_for_status()
        return float(r.json()["data"]["closing_price"])
    except Exception as exc:
        logger.warning("Bithumb Error: %s", exc)
    return None


# ── Интеллектуальный шлюз XE.com с проксированием (без возврата старого кэша при сбое) ──

def _fetch_krw_rub_google() -> Optional[float]:
    """Потокобезопасный шлюз: если все источники недоступны, возвращает строго None (без старого кэша)."""
    with _krw_google_lock:
        cached  = _krw_google_cache["value"]
        updated = _krw_google_cache["updated"]

    if cached is not None and updated is not None:
        if _elapsed_sec(updated) < 600:
            return cached

    if _krw_google_refreshing.is_set():
        logger.debug("_fetch_krw_rub_google: ожидание in-flight запроса")
        _krw_google_refreshing.wait(timeout=10)
        with _krw_google_lock:
            return _krw_google_cache["value"]

    _krw_google_refreshing.set()
    try:
        result = _refresh_krw_google()
    finally:
        _krw_google_refreshing.clear()

    if result is None:
        with _krw_google_lock:
            _krw_google_cache["value"]   = None
            _krw_google_cache["updated"] = None
        logger.warning("_fetch_krw_rub_google: все источники недоступны, возвращаем None")
        return None

    return result


def _refresh_krw_google() -> Optional[float]:
    """Высокоточный парсинг фиатного калькулятора XE.com через прокси (Замена Google)."""
    session = _get_session()

    def _clean_and_parse(raw_str: str) -> Optional[float]:
        clean = raw_str.replace(" ", "").replace("\xa0", "").replace("\u202f", "").strip()
        if "," in clean and "." in clean:
            clean = clean.replace(",", "")
        elif "," in clean:
            parts = clean.split(",")
            if len(parts[-1]) in (1, 2, 4):
                clean = clean.replace(",", ".")
            else:
                clean = clean.replace(",", "")
        clean = re.sub(r"[^\d.]", "", clean)
        try:
            return float(clean)
        except ValueError:
            return None

    def _save_cache(val: float, src: str) -> float:
        with _krw_google_lock:
            _krw_google_cache["value"]   = val
            _krw_google_cache["updated"] = now_msk()
        logger.info("[%s] KRW→RUB сохранён: %.0f ₽/млн KRW", src, val)
        return val

    proxies = {"http": ABCEX_PROXY, "https": ABCEX_PROXY} if ABCEX_PROXY else None

    # ── ВАЛ 1: XE.com Прямой калькулятор 1 000 000 KRW в RUB ──
    try:
        url_xe = f"https://www.xe.com/currencyconverter/convert/?Amount=1000000&From=KRW&To=RUB&_ts={int(time.time())}"
        r = session.get(url_xe, proxies=proxies, timeout=8)
        if r.status_code == 200:
            html_text = r.text

            candidates = re.findall(r"([\d.,\s]{5,12})(?:\s*Рублей|\s*Russian|\s*RUB|<)", html_text, re.IGNORECASE)
            for c in candidates:
                v = _clean_and_parse(c)
                if v and 35000 < v < 85000:
                    return _save_cache(v, "XE.com Калькулятор")

            m = re.search(r'class="[^"]*sc-[^"]*"[^>]*>([\d.,\s]+)<span>', html_text)
            if m:
                v = _clean_and_parse(m.group(1))
                if v and 35000 < v < 85000:
                    return _save_cache(v, "XE.com sc-Paragraph")
    except Exception as e:
        logger.warning("XE.com Вал 1 ошибка: %s", e)

    logger.warning("[XE.com] Основной узел недоступен. Запуск фиатного Fallback.")
    return _refresh_krw_google_fallback()


def _refresh_krw_google_fallback() -> Optional[float]:
    """Резервные чистые фиатные шлюзы реального времени."""
    session = _get_session()

    def _save(value: float, src: str) -> float:
        with _krw_google_lock:
            _krw_google_cache["value"]   = value
            _krw_google_cache["updated"] = now_msk()
        logger.info("[%s] KRW→RUB: %.2f", src, value)
        return value

    try:
        r = session.get(f"https://open.er-api.com/v6/latest/KRW?_ts={int(time.time())}", timeout=6)
        if r.status_code == 200:
            rub = r.json().get("rates", {}).get("RUB")
            if rub and float(rub) > 0:
                return _save(1_000_000 * float(rub), "Fallback-1 open.er-api")
    except Exception as e:
        logger.warning("[Fallback-1] %s", e)

    fawaz_urls = [
        "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/krw.json",
        "https://latest.currency-api.pages.dev/v1/currencies/krw.json",
        "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@1/v1/currencies/krw.json",
        "https://currency-api.pages.dev/v1/currencies/krw.json",
    ]
    for url in fawaz_urls:
        try:
            r = session.get(f"{url}?_ts={int(time.time())}", timeout=6)
            if r.status_code == 200:
                rub = r.json().get("krw", {}).get("rub")
                if rub and float(rub) > 0:
                    return _save(1_000_000 * float(rub), "Fallback-2 fawazahmed0")
        except Exception as e:
            logger.warning("[Fallback-2 %s] %s", url, e)

    try:
        r = session.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=6)
        if r.status_code == 200:
            krw = r.json().get("Valute", {}).get("KRW")
            if krw and float(krw.get("Value", 0)) > 0 and int(krw.get("Nominal", 1)) > 0:
                per_one_krw = float(krw["Value"]) / float(krw["Nominal"])
                return _save(1_000_000 * per_one_krw, "Fallback-4 ЦБ РФ")
    except Exception as e:
        logger.warning("[Fallback-4] %s", e)

    return None


def _krw_google_updater() -> None:
    """Фоновый воркер с экспоненциальным backoff при сбоях."""
    logger.info("krw_google_updater: запущен")
    backoff = 30
    while True:
        try:
            result = _refresh_krw_google()
            if result is not None:
                logger.info("krw_google_updater: %.0f ₽/млн KRW", result)
                backoff = 30
                time.sleep(10 * 60)
            else:
                logger.warning("krw_google_updater: все источники вернули None, backoff=%ds", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 600)
        except Exception as exc:
            logger.error("krw_google_updater crash: %s — backoff=%ds", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 600)


def _fetch_abcex() -> Tuple[Optional[float], Optional[float]]:
    """Парсинг стакана биржи ABCEX."""
    urls = [
        "https://hub.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB",
        "https://api.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB",
        "https://hub.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDT-RUB",
        "https://hub.abcex.com/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB",
    ]

    auth_headers: Dict[str, str] = {}
    if ABCEX_API_KEY:
        auth_headers["X-API-KEY"]     = ABCEX_API_KEY
        auth_headers["Authorization"] = f"Bearer {ABCEX_API_KEY}"

    proxies = {"http": ABCEX_PROXY, "https": ABCEX_PROXY} if ABCEX_PROXY else None

    def _extract_price(entry: Any) -> Optional[float]:
        if entry is None:
            return None
        if isinstance(entry, dict):
            for key in ("price", "rate", "p", "value", "px"):
                if key in entry:
                    try:
                        return float(entry[key])
                    except (TypeError, ValueError):
                        pass
        if isinstance(entry, (list, tuple)) and len(entry) >= 1:
            try:
                return float(entry[0])
            except (TypeError, ValueError):
                pass
        return None

    def _extract_side(data: dict, bid_keys: list, ask_keys: list):
        bids, asks = None, None
        nested = data.get("data") if isinstance(data.get("data"), dict) else None
        for b_key in bid_keys:
            src = nested if nested and b_key in nested else data
            val = src.get(b_key)
            if val and isinstance(val, (list, tuple)) and len(val) > 0:
                bids = val
                break
        for a_key in ask_keys:
            src = nested if nested and a_key in nested else data
            val = src.get(a_key)
            if val and isinstance(val, (list, tuple)) and len(val) > 0:
                asks = val
                break
        return bids, asks

    BID_KEYS = ["bid", "bids", "buy", "buys", "Bids", "BID"]
    ASK_KEYS = ["ask", "asks", "sell", "sells", "Asks", "ASK"]

    for url in urls:
        try:
            cache_bust = f"{'&' if '?' in url else '?'}_={int(time.time() * 1000)}"
            r = _get_session().get(
                url + cache_bust,
                timeout=_API_TIMEOUT,
                headers=auth_headers if auth_headers else None,
                proxies=proxies,
            )
            if r.status_code != 200:
                continue
            try:
                d = r.json()
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            bids, asks = _extract_side(d, BID_KEYS, ASK_KEYS)
            if not bids or not asks:
                continue
            b_price = _extract_price(bids[0])
            a_price = _extract_price(asks[0])
            if b_price and a_price and b_price > 0 and a_price > 0:
                logger.info("[ABCEX] ✅ bid=%.2f ask=%.2f ← %s", b_price, a_price, url)
                return float(b_price), float(a_price)
        except Exception as e:
            logger.error("[ABCEX] Ошибка %s: %s", url, e)
            continue

    return None, None


def _safe_future(future: concurrent.futures.Future, default: Any = None) -> Any:
    try:
        return future.result(timeout=_API_MAX_WAIT)
    except Exception:
        return default


def fetch_all_rates() -> Dict[str, Optional[float]]:
    fu = _API_POOL.submit(_fetch_upbit)
    fb = _API_POOL.submit(_fetch_bithumb)
    fg = _API_POOL.submit(_fetch_krw_rub_google)
    fa = _API_POOL.submit(_fetch_abcex)

    upbit      = _safe_future(fu)
    bithumb    = _safe_future(fb)
    krw_google = _safe_future(fg)
    abcex      = _safe_future(fa, default=(None, None))

    ab_buy, ab_sell = abcex if isinstance(abcex, tuple) else (None, None)

    usdt_rub_mid = None
    if ab_buy is not None and ab_sell is not None:
        usdt_rub_mid = (ab_buy + ab_sell) / 2
    elif ab_buy is not None:
        usdt_rub_mid = ab_buy
    elif ab_sell is not None:
        usdt_rub_mid = ab_sell

    if upbit and upbit > 0 and usdt_rub_mid is not None:
        krw_rub = (1_000_000 * usdt_rub_mid / upbit) * 0.994
    else:
        krw_rub = None

    return {
        "upbit":      upbit,
        "bithumb":    bithumb,
        "krw_rub":    krw_rub,
        "krw_google": krw_google,
        "ab_buy":     ab_buy,
        "ab_sell":    ab_sell,
    }


def fetch_auto_rates() -> Dict[str, Optional[float]]:
    fu = _API_POOL.submit(_fetch_upbit)
    fa = _API_POOL.submit(_fetch_abcex)
    fg = _API_POOL.submit(_fetch_krw_rub_google)

    upbit      = _safe_future(fu)
    abcex      = _safe_future(fa, default=(None, None))
    krw_google = _safe_future(fg)

    ab_buy, ab_sell = abcex if isinstance(abcex, tuple) else (None, None)
    return {"upbit": upbit, "ab_buy": ab_buy, "ab_sell": ab_sell, "krw_google": krw_google}


def build_rate_message(rates: Dict[str, Optional[float]], lang: str) -> str:
    T  = LANGS.get(lang, LANGS["ru"])
    ts = now_msk().strftime("%d.%m.%Y  %H:%M:%S")

    ab_buy_raw  = rates.get("ab_buy")
    ab_sell_raw = rates.get("ab_sell")
    krw_google  = rates.get("krw_google")
    upbit       = rates.get("upbit")

    def krw_from_usdt(usdt_price: Optional[float]) -> Optional[float]:
        if usdt_price and upbit and upbit > 0:
            return (1_000_000 * usdt_price / upbit) * 0.994
        return None

    krw_buy  = krw_from_usdt(ab_buy_raw)
    krw_sell = krw_from_usdt(ab_sell_raw)

    def krw_line_fmt(val: Optional[float]) -> str:
        if val is not None and krw_google is not None:
            return f"<b>{fmt_num(val, 0)} ₽</b>  ({fmt_num(krw_google, 0)} ₽)"
        if val is not None:
            return f"<b>{fmt_num(val, 0)} ₽</b>"
        return "—"

    return (
        f"{T['rate_title']}\n\n"
        f"🇰🇷 <b>USDT → KRW</b>\n"
        f"  ◾ UPBIT:    <b>{fmt_num(rates.get('upbit'),   0)} ₩</b>\n"
        f"  ◾ BITHUMB:  <b>{fmt_num(rates.get('bithumb'), 0)} ₩</b>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"🇷🇺 <b>USDT → RUB  (ABCEX)</b>\n"
        f"  🟢 {T['buy']}:   <b>{fmt_num(ab_buy_raw,  2)} ₽</b> (0-1%)\n"
        f"  🔴 {T['sell']}:  <b>{fmt_num(ab_sell_raw, 2)} ₽</b> (0-1%)\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"🇰🇷🔄🇷🇺 <b>KRW → RUB (кросс/Google)</b>\n"
        f"  🟢 {T['buy']}:   {krw_line_fmt(krw_buy)}\n"
        f"  🔴 {T['sell']}:  {krw_line_fmt(krw_sell)}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{T['updated']} <b>{ts} по МСК</b>\n\n"
        f"{T['contact']}"
    )


def build_auto_message(rates: Dict[str, Optional[float]]) -> str:
    usdt_krw   = rates.get("upbit")
    ab_buy     = rates.get("ab_buy")
    ab_sell    = rates.get("ab_sell")
    krw_google = rates.get("krw_google")

    if ab_buy is not None and ab_sell is not None:
        usdt_rub: Optional[float] = (ab_buy + ab_sell) / 2
    elif ab_buy is not None:
        usdt_rub = ab_buy
    elif ab_sell is not None:
        usdt_rub = ab_sell
    else:
        usdt_rub = None

    part_krw     = f"{fmt_num(usdt_krw, 0)} ₩"   if usdt_krw   is not None else "— ₩"
    part_rub     = f"{fmt_num(usdt_rub, 2)} ₽"   if usdt_rub   is not None else "— ₽"
    part_krw_rub = f"{fmt_num(krw_google, 0)} ₽" if krw_google is not None else "— ₽"
    return f"🔔 AUTO: {part_krw} | {part_rub} | {part_krw_rub}"


# ═══════════════════════════════════════════════════════════════════════
#  ОБЩИЕ INLINE-ДЕЙСТВИЯ (ОТМЕНА, ЗАКРЫТИЕ, СМЕНА ЯЗЫКА)
# ═══════════════════════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: c.data == "cancel_step")
def cb_cancel_step(c: types.CallbackQuery) -> None:
    init_user(c.from_user)
    uid  = c.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]
    bot.clear_step_handler_by_chat_id(c.message.chat.id)
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass
    bot.send_message(c.message.chat.id, T["action_canceled"], reply_markup=main_keyboard(uid))
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "close_msg")
def cb_close_msg(c: types.CallbackQuery) -> None:
    bot.clear_step_handler_by_chat_id(c.message.chat.id)
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass
    bot.answer_callback_query(c.id)


# ═══════════════════════════════════════════════════════════════════════
#  ОБРАБОТЧИКИ TELEGRAM
# ═══════════════════════════════════════════════════════════════════════
@bot.message_handler(commands=["start"])
def cmd_start(m: types.Message) -> None:
    bot.clear_step_handler_by_chat_id(m.chat.id)
    init_user(m.from_user)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
        types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
    )
    bot.send_message(m.chat.id, "🇷🇺 Выберите язык  /  🇬🇧 Select language:", reply_markup=kb)
    log_action(m.from_user, "/start")


@bot.message_handler(commands=["cancel"])
@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_cancel"], LANGS["en"]["btn_cancel"]
))
def msg_cancel_any(m: types.Message) -> None:
    bot.clear_step_handler_by_chat_id(m.chat.id)
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    bot.send_message(m.chat.id, LANGS[lang]["action_canceled"], reply_markup=main_keyboard(uid))


@bot.callback_query_handler(func=lambda c: c.data == "open_lang_menu")
def cb_open_lang_menu(c: types.CallbackQuery) -> None:
    init_user(c.from_user)
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
        types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
    )
    kb.row(types.InlineKeyboardButton(LANGS[get_lang(c.from_user.id)]["btn_close_inl"], callback_data="close_msg"))
    bot.send_message(c.message.chat.id, "🇷🇺 Выберите язык  /  🇬🇧 Select language:", reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def cb_lang(c: types.CallbackQuery) -> None:
    lang = c.data.split("_", 1)[1]
    if lang not in LANGS:
        bot.answer_callback_query(c.id)
        return
    init_user(c.from_user)
    uid = c.from_user.id
    with _DATA_LOCK:
        USER_DATA[uid]["lang"] = lang
    save_user(uid)
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass
    bot.send_message(c.message.chat.id, LANGS[lang]["welcome"], reply_markup=main_keyboard(uid))
    bot.answer_callback_query(c.id)
    log_action(c.from_user, "Язык", result="🇷🇺 RU" if lang == "ru" else "🇬🇧 EN")


@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_show"], LANGS["en"]["btn_show"]
))
def msg_show_rate(m: types.Message) -> None:
    bot.clear_step_handler_by_chat_id(m.chat.id)
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]

    bot.send_message(m.chat.id, "⏳", reply_markup=main_keyboard(uid))

    anim = bot.send_message(m.chat.id, ".")
    time.sleep(0.4)
    try:
        bot.edit_message_text("..", m.chat.id, anim.message_id)
    except Exception:
        pass
    time.sleep(0.4)
    try:
        bot.edit_message_text("...", m.chat.id, anim.message_id)
    except Exception:
        pass

    # Сбрасываем кэш перед ручным запросом, чтобы брать строго живой курс без старой страховки
    with _krw_google_lock:
        _krw_google_cache["value"]   = None
        _krw_google_cache["updated"] = None

    rates    = fetch_all_rates()
    has_data = any(v is not None for v in rates.values())

    try:
        bot.delete_message(m.chat.id, anim.message_id)
    except Exception:
        pass

    if not has_data:
        bot.send_message(m.chat.id, T["error_fetch"], parse_mode="HTML", reply_markup=rate_inline_keyboard(uid))
        log_action(m.from_user, "Курс", result="⚠️ все API недоступны")
        return

    bot.send_message(
        m.chat.id,
        build_rate_message(rates, lang),
        parse_mode="HTML",
        reply_markup=rate_inline_keyboard(uid),
    )

    with _DATA_LOCK:
        USER_DATA[uid]["requests"] += 1
        USER_DATA[uid]["last"]      = now_msk()
    save_user(uid)
    log_action(
        m.from_user,
        "Показать курс",
        result=f"Upbit {fmt_num(rates['upbit'], 0)} ₩ | ABCEX {fmt_num(rates['ab_buy'], 2)} ₽",
    )


@bot.callback_query_handler(func=lambda c: c.data == "rate_refresh")
def cb_rate_refresh(c: types.CallbackQuery) -> None:
    """Инлайн-кнопка «🔄 Обновить курс» прямо под сообщением курса."""
    init_user(c.from_user)
    uid  = c.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]

    bot.answer_callback_query(c.id, "⏳ Обновляем курс...")

    with _krw_google_lock:
        _krw_google_cache["value"]   = None
        _krw_google_cache["updated"] = None

    rates    = fetch_all_rates()
    has_data = any(v is not None for v in rates.values())

    if not has_data:
        bot.send_message(c.message.chat.id, T["error_fetch"], parse_mode="HTML")
        return

    new_text = build_rate_message(rates, lang)
    try:
        bot.edit_message_text(
            new_text,
            c.message.chat.id,
            c.message.message_id,
            parse_mode="HTML",
            reply_markup=rate_inline_keyboard(uid),
        )
    except Exception:
        bot.send_message(
            c.message.chat.id,
            new_text,
            parse_mode="HTML",
            reply_markup=rate_inline_keyboard(uid),
        )

    with _DATA_LOCK:
        USER_DATA[uid]["requests"] += 1
        USER_DATA[uid]["last"]      = now_msk()
    save_user(uid)
    log_action(
        c.from_user,
        "Обновить курс (inline)",
        result=f"Upbit {fmt_num(rates['upbit'], 0)} ₩ | ABCEX {fmt_num(rates['ab_buy'], 2)} ₽",
    )


@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_profile"], LANGS["en"]["btn_profile"]
))
def msg_profile(m: types.Message) -> None:
    bot.clear_step_handler_by_chat_id(m.chat.id)
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]
    with _DATA_LOCK:
        d = USER_DATA.get(uid, {}).copy()
        a = AUTO_USERS.get(uid)
        a_copy = a.copy() if a else None

    raw_nick  = f"@{d['username']}" if d.get("username") else (d.get("first_name") or "—")
    nick      = _safe_html(raw_nick)
    auto_line = f"🟢 {a_copy['interval'] // 3600}H" if a_copy else "🔴 OFF"

    text = (
        f"{T['prof_title']}\n\n"
        f"<b>ID:</b> <code>{uid}</code>\n"
        f"<b>Ник:</b> {nick}\n"
        f"<b>{T['prof_join']}</b> {fmt_dt(d.get('joined'))}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>{T['prof_reqs']}</b> {d.get('requests', 0)}\n"
        f"<b>{T['prof_last']}</b> {fmt_dt(d.get('last'))}\n"
        f"<b>{T['prof_auto']}</b> {auto_line}"
    )
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(T["btn_lang_inl"], callback_data="open_lang_menu"),
        types.InlineKeyboardButton(T["btn_auto_inl"], callback_data="open_auto_menu"),
    )
    bot.send_message(m.chat.id, text, parse_mode="HTML", reply_markup=kb)
    log_action(m.from_user, "Профиль")


# ═══════════════════════════════════════════════════════════════════════
#  ОТЗЫВЫ С КНОПКОЙ «НАЗАД / ОТМЕНА»
# ═══════════════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_feedback"], LANGS["en"]["btn_feedback"]
))
def msg_feedback_start(m: types.Message) -> None:
    bot.clear_step_handler_by_chat_id(m.chat.id)
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]

    # Показываем нижнюю клавиатуру с кнопкой «⬅️ Назад / Отмена»
    bot.send_message(m.chat.id, "✍️", reply_markup=cancel_reply_keyboard(uid))
    # И дополнительно инлайн-кнопку отмены под текстом
    reply = bot.send_message(
        m.chat.id,
        T["feedback_ask"],
        parse_mode="HTML",
        reply_markup=cancel_inline_keyboard(uid),
    )
    bot.register_next_step_handler(reply, _feedback_receive)
    log_action(m.from_user, "Отзыв — начало")


def _feedback_receive(m: types.Message) -> None:
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]

    # Если пользователь нажал «⬅️ Назад / Отмена» или любую кнопку меню
    if not m.text or _is_cancel_or_menu(m.text):
        bot.send_message(m.chat.id, T["action_canceled"], reply_markup=main_keyboard(uid))
        log_action(m.from_user, "Отзыв — отменён")
        return

    text_clean = m.text.strip()
    if text_clean:
        first = _safe_html(m.from_user.first_name, "")
        last  = f" {_safe_html(m.from_user.last_name, '')}" if m.from_user.last_name else ""
        nick  = f"@{_safe_html(m.from_user.username)}" if m.from_user.username else "—"
        try:
            bot.send_message(
                ADMIN_LOG_CHAT_ID,
                f"🔴 <b>НОВЫЙ ОТЗЫВ</b>\n"
                f"👤 {first}{last}  |  {nick}  |  <code>{uid}</code>\n"
                f"💬 <i>{_safe_html(text_clean)}</i>\n"
                f"🕒 {now_msk().strftime('%d.%m.%Y %H:%M:%S')} МСК",
                parse_mode="HTML",
            )
        except Exception:
            pass
    bot.send_message(m.chat.id, T["feedback_ok"], reply_markup=main_keyboard(uid))
    log_action(m.from_user, "Отзыв — отправлен")


# ═══════════════════════════════════════════════════════════════════════
#  АВТООБНОВЛЕНИЕ — ПОЛЬЗОВАТЕЛЬ
# ═══════════════════════════════════════════════════════════════════════
def _build_auto_menu_payload(uid: int) -> Tuple[str, types.InlineKeyboardMarkup]:
    lang = get_lang(uid)
    T    = LANGS[lang]
    with _DATA_LOCK:
        entry = AUTO_USERS.get(uid)
        cur_interval = entry["interval"] if entry else None

    if cur_interval:
        status_line = T["auto_status_on"].format(h=cur_interval // 3600)
    else:
        status_line = T["auto_status_off"]

    text = f"{T['auto_menu']}\n{status_line}\n\n{T['auto_choose']}"

    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(
            f"{'✅ ' if cur_interval == 3600 else '⏰ '}1H",
            callback_data="auto_3600",
        ),
        types.InlineKeyboardButton(
            f"{'✅ ' if cur_interval == 18000 else '⏰ '}5H",
            callback_data="auto_18000",
        ),
        types.InlineKeyboardButton(
            f"{'✅ ' if cur_interval == 86400 else '⏰ '}24H",
            callback_data="auto_86400",
        ),
    )
    if cur_interval:
        kb.row(types.InlineKeyboardButton(T["btn_disable"], callback_data="auto_0"))
    kb.row(types.InlineKeyboardButton(T["btn_close_inl"], callback_data="close_msg"))
    return text, kb


@bot.message_handler(commands=["auto"])
@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_auto"], LANGS["en"]["btn_auto"]
))
def msg_auto_menu(m: types.Message) -> None:
    bot.clear_step_handler_by_chat_id(m.chat.id)
    init_user(m.from_user)
    uid = m.from_user.id
    text, kb = _build_auto_menu_payload(uid)
    bot.send_message(m.chat.id, text, parse_mode="HTML", reply_markup=kb)
    log_action(m.from_user, "Меню авто")


@bot.callback_query_handler(func=lambda c: c.data == "open_auto_menu")
def cb_open_auto_menu(c: types.CallbackQuery) -> None:
    init_user(c.from_user)
    uid = c.from_user.id
    text, kb = _build_auto_menu_payload(uid)
    bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("auto_"))
def cb_auto(c: types.CallbackQuery) -> None:
    init_user(c.from_user)
    uid  = c.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]
    try:
        val = int(c.data.split("_", 1)[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(c.id)
        return
    if val == 0:
        with _DATA_LOCK:
            AUTO_USERS.pop(uid, None)
        save_auto(uid)
        resp = f"✅ 🔕 {T['auto_off_msg']}"
        log_action(c.from_user, "Авто OFF")
    else:
        ts = now_msk()
        with _DATA_LOCK:
            AUTO_USERS[uid] = {"interval": val, "last": ts, "enabled_at": ts}
        save_auto(uid)
        resp = f"✅ 🔔 {T['auto_on_msg']} <b>{val // 3600}H</b>."
        log_action(c.from_user, "Авто ON", result=f"{val // 3600}H")

    _, kb = _build_auto_menu_payload(uid)
    try:
        bot.edit_message_text(resp, c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    bot.answer_callback_query(c.id)


@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_disable"], LANGS["en"]["btn_disable"]
))
def msg_disable_auto(m: types.Message) -> None:
    bot.clear_step_handler_by_chat_id(m.chat.id)
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]
    with _DATA_LOCK:
        AUTO_USERS.pop(uid, None)
    save_auto(uid)
    bot.send_message(
        m.chat.id,
        f"✅ 🔕 {T['auto_off_msg']}",
        parse_mode="HTML",
        reply_markup=main_keyboard(uid),
    )
    log_action(m.from_user, "Отключил уведомления")


# ═══════════════════════════════════════════════════════════════════════
#  АДМИН-ПАНЕЛЬ С КНОПКАМИ «НАЗАД» И «ОТМЕНА»
# ═══════════════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: (
    m.from_user.id == MY_ADMIN_ID
    and m.text in (LANGS["ru"]["btn_admin"], LANGS["en"]["btn_admin"])
))
def msg_admin_panel(m: types.Message) -> None:
    bot.clear_step_handler_by_chat_id(m.chat.id)
    init_user(m.from_user)
    bot.send_message(m.chat.id, "🛠 <b>Админ-панель</b>", reply_markup=admin_main_inline_keyboard())
    log_action(m.from_user, "Открыл Админ-панель")


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def cb_admin(c: types.CallbackQuery) -> None:
    if c.from_user.id != MY_ADMIN_ID:
        bot.answer_callback_query(c.id, "⛔️ Доступ запрещён")
        return
    action = c.data[4:]

    if action == "back":
        bot.clear_step_handler_by_chat_id(c.message.chat.id)
        bot.answer_callback_query(c.id)
        try:
            bot.edit_message_text(
                "🛠 <b>Админ-панель</b>",
                c.message.chat.id,
                c.message.message_id,
                parse_mode="HTML",
                reply_markup=admin_main_inline_keyboard(),
            )
        except Exception:
            bot.send_message(c.message.chat.id, "🛠 <b>Админ-панель</b>", reply_markup=admin_main_inline_keyboard())

    elif action == "stat":
        with _DATA_LOCK:
            n_u = len(ALL_USER_IDS)
            n_s = len(AUTO_USERS)
            n_r = sum(u.get("requests", 0) for u in USER_DATA.values())
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("🔄 Обновить", callback_data="adm_stat"),
            types.InlineKeyboardButton("⬅️ Назад",    callback_data="adm_back"),
        )
        stat_text = (
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Пользователей:  <b>{n_u}</b>\n"
            f"🔔 Подписок:       <b>{n_s}</b>\n"
            f"📈 Запросов курса: <b>{n_r}</b>\n"
            f"🕒 {now_msk().strftime('%d.%m.%Y %H:%M:%S')} МСК"
        )
        bot.answer_callback_query(c.id)
        try:
            bot.edit_message_text(stat_text, c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, stat_text, parse_mode="HTML", reply_markup=kb)

    elif action.startswith("users_"):
        try:
            page = int(action.split("_", 1)[1])
        except ValueError:
            page = 0
        PG = 8
        with _DATA_LOCK:
            all_ids = sorted(ALL_USER_IDS)
        total = len(all_ids)
        s = page * PG
        e = min(s + PG, total)
        chunk = all_ids[s:e]
        if not chunk:
            bot.answer_callback_query(c.id, "Список пуст")
            return
        txt = f"👥 <b>Пользователи ({s+1}–{e} из {total})</b>\n\n"
        kb  = types.InlineKeyboardMarkup(row_width=1)
        for uid in chunk:
            with _DATA_LOCK:
                d    = USER_DATA.get(uid, {})
                icon = "🟢" if uid in AUTO_USERS else "🔴"
            raw_nick = f"@{d.get('username')}" if d.get("username") else (d.get("first_name") or str(uid))
            txt += f"{icon} <code>{uid}</code>  {_safe_html(raw_nick)}\n"
            kb.add(types.InlineKeyboardButton(f"🔍 {raw_nick}", callback_data=f"adm_detail_{uid}"))
        nav = []
        if page > 0:
            nav.append(types.InlineKeyboardButton("◀️", callback_data=f"adm_users_{page-1}"))
        if e < total:
            nav.append(types.InlineKeyboardButton("▶️", callback_data=f"adm_users_{page+1}"))
        if nav:
            kb.row(*nav)
        kb.row(types.InlineKeyboardButton("⬅️ Назад в админку", callback_data="adm_back"))
        bot.answer_callback_query(c.id)
        try:
            bot.edit_message_text(txt, c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, txt, parse_mode="HTML", reply_markup=kb)

    elif action.startswith("detail_"):
        try:
            uid = int(action.split("_", 1)[1])
        except ValueError:
            bot.answer_callback_query(c.id)
            return
        with _DATA_LOCK:
            d = USER_DATA.get(uid, {}).copy()
            a = AUTO_USERS.get(uid)
            a_copy = a.copy() if a else None
        raw_nick = f"@{d['username']}" if d.get("username") else (d.get("first_name") or "—")
        nick = _safe_html(raw_nick)
        auto_info = (
            f"\n🟢 <b>Автообновление: ВКЛЮЧЕНО</b>\n"
            f"  ⏱ Интервал:           <b>каждые {a_copy['interval']//3600}H</b>\n"
            f"  📅 Включено:           {fmt_dt(a_copy.get('enabled_at'))}\n"
            f"  📤 Последняя отправка: {fmt_dt(a_copy.get('last'))}"
        ) if a_copy else "\n🔴 <b>Автообновление: ВЫКЛЮЧЕНО</b>"
        text = (
            f"🔍 <b>Карточка пользователя</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>ID:</b>       <code>{uid}</code>\n"
            f"<b>Ник:</b>      {nick}\n"
            f"<b>Язык:</b>     {'🇷🇺 RU' if d.get('lang', 'ru') == 'ru' else '🇬🇧 EN'}\n"
            f"<b>В боте с:</b> {fmt_dt(d.get('joined'))}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>Запросов:</b>  {d.get('requests', 0)}\n"
            f"<b>Последний:</b> {fmt_dt(d.get('last'))}"
            f"{auto_info}"
        )
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("◀️ К списку",        callback_data="adm_users_0"),
            types.InlineKeyboardButton("⬅️ В админ-панель", callback_data="adm_back"),
        )
        bot.answer_callback_query(c.id)
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)

    elif action == "auto_menu":
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("✅ Включить всем",      callback_data="adm_auto_all"),
            types.InlineKeyboardButton("👤 Подключить по ID",    callback_data="adm_auto_id"),
            types.InlineKeyboardButton("⬅️ Назад в админку",    callback_data="adm_back"),
        )
        bot.answer_callback_query(c.id)
        bot.edit_message_text(
            "⚙️ <b>Управление подписками</b>\n\nПользователи <b>не получат</b> уведомлений.",
            c.message.chat.id,
            c.message.message_id,
            parse_mode="HTML",
            reply_markup=kb,
        )

    elif action == "auto_all":
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("1H",  callback_data="adm_all_3600"),
            types.InlineKeyboardButton("5H",  callback_data="adm_all_18000"),
            types.InlineKeyboardButton("24H", callback_data="adm_all_86400"),
        )
        kb.row(types.InlineKeyboardButton("⬅️ Назад", callback_data="adm_auto_menu"))
        bot.answer_callback_query(c.id)
        bot.edit_message_text(
            "⏳ Выберите интервал для <b>всех</b>:",
            c.message.chat.id,
            c.message.message_id,
            parse_mode="HTML",
            reply_markup=kb,
        )

    elif action.startswith("all_"):
        try:
            val = int(action.split("_", 1)[1])
        except ValueError:
            bot.answer_callback_query(c.id)
            return
        hours = val // 3600
        ts    = now_msk()
        count = 0
        with _DATA_LOCK:
            ids_copy = set(ALL_USER_IDS)
        for uid in ids_copy:
            try:
                with _DATA_LOCK:
                    AUTO_USERS[uid] = {"interval": val, "last": ts, "enabled_at": ts}
                save_auto(uid)
                count += 1
            except Exception as exc:
                logger.error("Тихая подписка %d: %s", uid, exc)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ Назад в админку", callback_data="adm_back"))
        bot.answer_callback_query(c.id)
        bot.edit_message_text(
            f"✅ Подписка {hours}H для <b>{count}</b> чел.",
            c.message.chat.id,
            c.message.message_id,
            parse_mode="HTML",
            reply_markup=kb,
        )
        log_action(c.from_user, f"Массовая подписка {hours}H", result=f"{count} чел.")

    elif action == "auto_id":
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, "👤 Ввод ID:", reply_markup=cancel_reply_keyboard(c.from_user.id))
        msg = bot.send_message(
            c.message.chat.id,
            "Введите <b>ID</b> пользователя (или нажмите «⬅️ Назад / Отмена»):",
            parse_mode="HTML",
            reply_markup=cancel_inline_keyboard(c.from_user.id),
        )
        bot.register_next_step_handler(msg, _adm_auto_get_id)

    elif action == "bc":
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, "📢 Подготовка рассылки:", reply_markup=cancel_reply_keyboard(c.from_user.id))
        msg = bot.send_message(
            c.message.chat.id,
            "✏️ Введите текст рассылки (или нажмите «⬅️ Назад / Отмена»):",
            reply_markup=cancel_inline_keyboard(c.from_user.id),
        )
        bot.register_next_step_handler(msg, _adm_broadcast)

    else:
        bot.answer_callback_query(c.id)


def _adm_auto_get_id(m: types.Message) -> None:
    uid = m.from_user.id
    if not m.text or _is_cancel_or_menu(m.text):
        bot.send_message(m.chat.id, LANGS[get_lang(uid)]["action_canceled"], reply_markup=main_keyboard(uid))
        return
    try:
        tid = int(m.text.strip())
    except ValueError:
        bot.send_message(m.chat.id, "❌ Некорректный ID. Действие отменено.", reply_markup=main_keyboard(uid))
        return
    msg = bot.send_message(
        m.chat.id,
        f"Интервал в <b>часах</b> (1/5/24) для <code>{tid}</code> (или нажмите «⬅️ Назад / Отмена»):",
        parse_mode="HTML",
        reply_markup=cancel_inline_keyboard(uid),
    )
    bot.register_next_step_handler(msg, lambda s: _adm_auto_set(s, tid))


def _adm_auto_set(m: types.Message, tid: int) -> None:
    uid = m.from_user.id
    if not m.text or _is_cancel_or_menu(m.text):
        bot.send_message(m.chat.id, LANGS[get_lang(uid)]["action_canceled"], reply_markup=main_keyboard(uid))
        return
    try:
        hours = int(m.text.strip())
        if hours <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(m.chat.id, "❌ Введите положительное число. Действие отменено.", reply_markup=main_keyboard(uid))
        return
    ts = now_msk()
    with _DATA_LOCK:
        ALL_USER_IDS.add(tid)
        AUTO_USERS[tid] = {"interval": hours * 3600, "last": ts, "enabled_at": ts}
    save_auto(tid)
    bot.send_message(
        m.chat.id,
        f"✅ <code>{tid}</code> — каждые <b>{hours}H</b>.",
        parse_mode="HTML",
        reply_markup=main_keyboard(uid),
    )
    log_action(m.from_user, f"Тихая подписка {tid}", result=f"{hours}H")


def _adm_broadcast(m: types.Message) -> None:
    uid = m.from_user.id
    if not m.text or _is_cancel_or_menu(m.text):
        bot.send_message(m.chat.id, LANGS[get_lang(uid)]["action_canceled"], reply_markup=main_keyboard(uid))
        return

    bc_text = m.text.strip()
    if not bc_text:
        bot.send_message(m.chat.id, "❌ Пустое сообщение.", reply_markup=main_keyboard(uid))
        return

    def _do_broadcast() -> None:
        with _DATA_LOCK:
            ids = set(ALL_USER_IDS)
        sent = failed = 0
        for target_id in ids:
            try:
                bot.send_message(target_id, f"📢 <b>Уведомление</b>\n\n{bc_text}", parse_mode="HTML")
                sent += 1
                time.sleep(0.05)
            except Exception:
                failed += 1
        bot.send_message(
            m.chat.id,
            f"✅ Готово. Доставлено: <b>{sent}</b>  ❌ Ошибок: <b>{failed}</b>",
            parse_mode="HTML",
            reply_markup=main_keyboard(uid),
        )
        log_action(m.from_user, "Рассылка", result=f"sent={sent} failed={failed}")

    threading.Thread(target=_do_broadcast, daemon=True, name="broadcast").start()
    bot.send_message(m.chat.id, "⏳ Рассылка запущена...", reply_markup=main_keyboard(uid))


@bot.message_handler(func=lambda m: True)
def msg_fallback(m: types.Message) -> None:
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    bot.send_message(m.chat.id, LANGS[lang]["menu_updated"], reply_markup=main_keyboard(uid))


# ═══════════════════════════════════════════════════════════════════════
#  ФОНОВЫЙ ВОРКЕР: АВТООБНОВЛЕНИЕ КУРСОВ
# ═══════════════════════════════════════════════════════════════════════
def _auto_worker() -> None:
    logger.info("🔄 auto_worker: запущен")
    while True:
        try:
            time.sleep(60)
            now = now_msk()
            with _DATA_LOCK:
                due = {
                    cid: cfg.copy()
                    for cid, cfg in AUTO_USERS.items()
                    if _elapsed_sec(cfg.get("last")) >= cfg["interval"]
                }
            if not due:
                continue
            rates    = fetch_auto_rates()
            has_data = any(v is not None for v in rates.values())
            if not has_data:
                logger.warning("auto_worker: все API недоступны")
                continue
            text = build_auto_message(rates)
            for cid in due:
                try:
                    bot.send_message(cid, text, parse_mode="HTML")
                    with _DATA_LOCK:
                        if cid in AUTO_USERS:
                            AUTO_USERS[cid]["last"] = now
                    save_auto(cid)
                    time.sleep(0.05)
                except Exception as exc:
                    err = str(exc).lower()
                    if any(kw in err for kw in ("blocked", "not found", "deactivated", "forbidden", "chat not found")):
                        logger.warning("auto_worker: %d заблокировал бота — удаляем", cid)
                        with _DATA_LOCK:
                            AUTO_USERS.pop(cid, None)
                        save_auto(cid)
                    else:
                        logger.error("auto_worker %d: %s", cid, exc)
        except Exception as exc:
            logger.critical("auto_worker crashed: %s — перезапуск через 30с", exc)
            time.sleep(30)


# ═══════════════════════════════════════════════════════════════════════
#  ФОНОВЫЙ ВОРКЕР: АНТИ-СОН ДЛЯ RENDER FREE TIER
# ═══════════════════════════════════════════════════════════════════════
def _anti_sleep_worker() -> None:
    _INTERVAL   = 10 * 60
    _PING_AGENT = requests.Session()
    _PING_AGENT.headers.update({"User-Agent": "RenderKeepAlive/1.0"})

    def _ping_self() -> None:
        if not RENDER_URL:
            return
        try:
            r = _PING_AGENT.get(f"{RENDER_URL}/ping", timeout=15)
            logger.info("🏓 anti_sleep self-ping (%s): HTTP %d", RENDER_URL, r.status_code)
        except Exception as exc:
            logger.warning("🏓 anti_sleep self-ping fail: %s", exc)

    logger.info("🏓 anti_sleep: запущен (интервал %d мин)", _INTERVAL // 60)
    time.sleep(20)
    _ping_self()

    while True:
        try:
            time.sleep(_INTERVAL)
            _ping_self()
        except Exception as exc:
            logger.error("anti_sleep_worker error: %s", exc)
            time.sleep(60)


# ═══════════════════════════════════════════════════════════════════════
#  ЗАХВАТ ТОКЕНА У СТАРОГО КОНТЕЙНЕРА
# ═══════════════════════════════════════════════════════════════════════
def _force_takeover_token() -> None:
    base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    try:
        r = requests.post(f"{base_url}/deleteWebhook", params={"drop_pending_updates": "true"}, timeout=10)
        logger.info("Takeover: deleteWebhook → HTTP %d | %s", r.status_code, r.text[:120])
    except Exception as exc:
        logger.warning("Takeover: deleteWebhook failed: %s", exc)

    for attempt in range(1, 4):
        try:
            r = requests.post(f"{base_url}/getUpdates", json={"offset": -1, "timeout": 0, "limit": 1}, timeout=15)
            if r.status_code == 200:
                logger.info("Takeover [%d/3]: getUpdates OK — токен наш", attempt)
            elif r.status_code == 409:
                logger.warning("Takeover [%d/3]: 409 — старый контейнер жив, ждём", attempt)
            else:
                logger.warning("Takeover [%d/3]: HTTP %d | %s", attempt, r.status_code, r.text[:120])
        except Exception as exc:
            logger.warning("Takeover [%d/3]: %s", attempt, exc)
        time.sleep(3)
    logger.info("✅ Takeover завершён")


# ═══════════════════════════════════════════════════════════════════════
#  УСТАНОВКА WEBHOOK И POLLING FALLBACK
# ═══════════════════════════════════════════════════════════════════════
def _setup_webhook() -> bool:
    if not RENDER_URL:
        logger.warning("⚠️ RENDER_URL не задан — webhook невозможен, fallback на polling")
        return False

    webhook_url = f"{RENDER_URL}{WEBHOOK_PATH}"
    for attempt in range(1, 4):
        try:
            result = bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET,
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"],
                max_connections=40,
            )
            if result:
                logger.info("✅ Webhook зарегистрирован: %s", webhook_url)
                try:
                    info = bot.get_webhook_info()
                    logger.info(
                        "ℹ️ Webhook info: url=%s pending=%d last_error=%s",
                        info.url,
                        info.pending_update_count or 0,
                        info.last_error_message or "—",
                    )
                except Exception:
                    pass
                return True
            logger.error("❌ set_webhook вернул False (попытка %d/3)", attempt)
        except Exception as exc:
            logger.error("❌ set_webhook исключение (попытка %d/3): %s", attempt, exc)
        time.sleep(2)
    return False


def _polling_fallback() -> None:
    """Резервный режим Long Polling, если Webhook недоступен или не задан RENDER_URL."""
    logger.info("🔄 Запуск резервного режима Long Polling...")
    try:
        bot.remove_webhook()
    except Exception:
        pass
    while True:
        try:
            bot.infinity_polling(
                timeout=20,
                long_polling_timeout=10,
                skip_pending=True,
                allowed_updates=["message", "callback_query"],
            )
        except Exception as exc:
            logger.error("Polling error: %s — перезапуск через 5с", exc)
            time.sleep(5)


def _notify_startup() -> None:
    try:
        bot.send_message(
            ADMIN_LOG_CHAT_ID,
            "🚀 <b>Система управления курсами успешно перезагружена на Render и готова к работе!</b>\n\n"
            f"🌐 <b>Активный узел:</b> <code>{_safe_html(RENDER_URL or 'polling-mode')}</code>\n"
            "<i>Все новые лог-сессии перенаправлены в этот канал.</i>",
            parse_mode="HTML",
        )
        logger.info("Уведомление о перезагрузке отправлено в лог-канал")
    except Exception as exc:
        logger.warning("Не удалось отправить пуш в лог-канал: %s", exc)


# ═══════════════════════════════════════════════════════════════════════
#  FLASK СЕРВЕР
# ═══════════════════════════════════════════════════════════════════════
flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET", "HEAD"])
def health() -> Any:
    with _DATA_LOCK:
        n_u = len(ALL_USER_IDS)
        n_s = len(AUTO_USERS)
    with _krw_google_lock:
        cache_val = _krw_google_cache["value"]
        cache_upd = _krw_google_cache["updated"]
    return jsonify({
        "status":     "ok",
        "mode":       "webhook" if RENDER_URL else "polling",
        "render_url": RENDER_URL,
        "users":      n_u,
        "subs":       n_s,
        "time":       now_msk().strftime("%Y-%m-%d %H:%M:%S MSK"),
        "krw_google_cache": {
            "value":   cache_val,
            "updated": cache_upd.isoformat() if cache_upd else None,
            "age_sec": _elapsed_sec(cache_upd),
        },
    }), 200


@flask_app.route("/ping", methods=["GET", "HEAD"])
def ping_endpoint() -> Any:
    return jsonify({"pong": True, "time": now_msk().isoformat()}), 200


def _process_single_update(update: telebot.types.Update) -> None:
    try:
        bot.process_new_updates([update])
    except Exception as exc:
        logger.error("Ошибка обработки апдейта: %s", exc)


@flask_app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook() -> Any:
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secrets.compare_digest(received_secret, WEBHOOK_SECRET):
        logger.warning("Webhook: неверный secret token (источник: %s)", request.remote_addr)
        abort(403)

    if request.headers.get("Content-Type", "").split(";")[0] != "application/json":
        abort(400)

    try:
        raw = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(raw)
        if update is None:
            abort(400)
    except Exception as exc:
        logger.error("Webhook: ошибка парсинга апдейта: %s", exc)
        abort(400)

    _WEBHOOK_POOL.submit(_process_single_update, update)
    return "", 200


# ═══════════════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ═══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    _force_takeover_token()
    load_db()

    threading.Thread(target=_auto_worker,        daemon=True, name="auto_worker").start()
    threading.Thread(target=_anti_sleep_worker,  daemon=True, name="anti_sleep").start()
    threading.Thread(target=_krw_google_updater, daemon=True, name="krw_google").start()

    webhook_ok = _setup_webhook()

    threading.Thread(target=_notify_startup, daemon=True, name="startup_notify").start()

    port = int(os.environ.get("PORT", 10_000))

    if webhook_ok:
        logger.info("🌐 Старт Flask в webhook-режиме на порту %d", port)
        try:
            flask_app.run(host="0.0.0.0", port=port, use_reloader=False, debug=False, threaded=True)
        except Exception as exc:
            logger.critical("Flask упал: %s", exc)
    else:
        threading.Thread(
            target=lambda: flask_app.run(host="0.0.0.0", port=port, use_reloader=False, debug=False, threaded=True),
            daemon=True,
            name="flask_fallback",
        ).start()
        logger.info("🌐 Flask на порту %d (вспомогательный)", port)
        _polling_fallback()
