# -*- coding: utf-8 -*-
"""
P2P Exchange Rate Telegram Bot
Production-grade: thread-safe, Render-ready, MongoDB-backed.
"""

import os
import logging
import threading
import time
import concurrent.futures
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, Any

import requests
from dotenv import load_dotenv
import telebot
from telebot import types
from flask import Flask, jsonify
from pymongo import MongoClient
import certifi

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

_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
if not _TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан в переменных окружения")

TELEGRAM_TOKEN: str    = _TOKEN
MY_ADMIN_ID: int       = 5266659205
ADMIN_LOG_CHAT_ID: int = -1003264764082
MOSCOW_TZ              = timezone(timedelta(hours=3))

RENDER_URL: str = os.getenv("RENDER_URL", "").rstrip("/")

_API_TIMEOUT  = 8
_API_MAX_WAIT = 12

_DATA_LOCK = threading.Lock()

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=True)

# ═══════════════════════════════════════════════════════════════════════
#  MONGODB
# ═══════════════════════════════════════════════════════════════════════
_MONGO_URI = os.getenv("MONGO_URI", "")
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
        )
        _mongo.server_info()
        _db       = _mongo["p2p_bot_db"]
        users_col = _db["users"]
        auto_col  = _db["auto_updates"]
        logger.info("✅ MongoDB Atlas: подключено")
    except Exception as _exc:
        logger.error("❌ MongoDB недоступна: %s", _exc)
else:
    logger.warning("⚠️  MONGO_URI не задан — данные только в RAM")

# ═══════════════════════════════════════════════════════════════════════
#  ЛОКАЛИЗАЦИЯ
# ═══════════════════════════════════════════════════════════════════════
LANGS: Dict[str, Dict[str, str]] = {
    "ru": {
        "btn_show":     "📊 Показать курс",
        "btn_auto":     "🔔 Автообновление",
        "btn_profile":  "👤 Профиль",
        "btn_feedback": "✍️ Отзыв",
        "btn_disable":  "🚫 Отключить уведомления",
        "btn_admin":    "🛠 Админка",
        "welcome":      "👋 Привет!\n\nВыбери нужный раздел ниже 👇",
        "error_fetch":  "⚠️ Не удалось получить данные. Попробуйте позже.",
        "rate_title":   "💱 <b>АКТУАЛЬНЫЕ КУРСЫ</b>",
        "updated":      "⏱ Обновлено:",
        "contact":      "💰 Обмен любых сумм и валют — по договорённости.\n📞 Контакт: @Abdulkhaiii",
        "auto_menu":    "Выбери частоту автообновления:",
        "auto_choose":  "⬇️ Выбери интервал:",
        "auto_off_msg": "Автообновление выключено.",
        "auto_on_msg":  "Уведомление включено каждые",
        "feedback_ask": "Напишите ваш отзыв одним сообщением:",
        "feedback_ok":  "✅ Спасибо! Ваш отзыв передан администратору.",
        "menu_updated": "🔄 Меню обновлено.",
        "prof_title":   "👤 <b>Ваш профиль</b>",
        "prof_reqs":    "Всего запросов:",
        "prof_last":    "Последний запрос:",
        "prof_join":    "В боте с:",
        "buy":          "Покупка",
        "sell":         "Продажа",
        "no_data":      "нет данных",
    },
    "en": {
        "btn_show":     "📊 Show Rates",
        "btn_auto":     "🔔 Auto-updates",
        "btn_profile":  "👤 Profile",
        "btn_feedback": "✍️ Feedback",
        "btn_disable":  "🚫 Disable Alerts",
        "btn_admin":    "🛠 Admin Panel",
        "welcome":      "👋 Hello!\n\nSelect a section below 👇",
        "error_fetch":  "⚠️ Failed to get data. Please try again later.",
        "rate_title":   "💱 <b>CURRENT RATES</b>",
        "updated":      "⏱ Updated:",
        "contact":      "💰 Exchange of any amounts — by agreement.\n📞 Contact: @Abdulkhaiii",
        "auto_menu":    "Select update frequency:",
        "auto_choose":  "⬇️ Choose interval:",
        "auto_off_msg": "Auto-updates disabled.",
        "auto_on_msg":  "Alerts enabled every",
        "feedback_ask": "Write your feedback in one message:",
        "feedback_ok":  "✅ Thank you! Feedback sent to admin.",
        "menu_updated": "🔄 Menu updated.",
        "prof_title":   "👤 <b>Your Profile</b>",
        "prof_reqs":    "Total requests:",
        "prof_last":    "Last request:",
        "prof_join":    "Member since:",
        "buy":          "Buy",
        "sell":         "Sell",
        "no_data":      "no data",
    },
}

# ═══════════════════════════════════════════════════════════════════════
#  RAM-ХРАНИЛИЩЕ
# ═══════════════════════════════════════════════════════════════════════
USER_DATA:    Dict[int, Dict[str, Any]] = {}
AUTO_USERS:   Dict[int, Dict[str, Any]] = {}
ALL_USER_IDS: set                       = set()

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


# ═══════════════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════════════
def load_db() -> None:
    if users_col is None or auto_col is None:
        logger.warning("load_db: MongoDB недоступна, пропускаем")
        return
    try:
        logger.info("Загрузка данных из MongoDB...")
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
        users_col.update_one(
            {"_id": uid},
            {"$set": {
                "lang":       u["lang"],
                "requests":   u["requests"],
                "last":       u["last"].isoformat()   if u["last"]   else None,
                "joined":     u["joined"].isoformat() if u["joined"] else None,
                "first_name": u["first_name"],
                "username":   u["username"],
            }},
            upsert=True,
        )
    except Exception as exc:
        logger.error("save_user(%d): %s", uid, exc)


def save_user(uid: int) -> None:
    threading.Thread(target=_db_write_user, args=(uid,), daemon=True).start()


def _db_write_auto(uid: int) -> None:
    if auto_col is None:
        return
    try:
        with _DATA_LOCK:
            entry = AUTO_USERS.get(uid)
        if entry:
            auto_col.update_one(
                {"_id": uid},
                {"$set": {
                    "interval":   entry["interval"],
                    "last":       entry["last"].isoformat()       if entry.get("last")       else None,
                    "enabled_at": entry["enabled_at"].isoformat() if entry.get("enabled_at") else None,
                }},
                upsert=True,
            )
        else:
            auto_col.delete_one({"_id": uid})
    except Exception as exc:
        logger.error("save_auto(%d): %s", uid, exc)


def save_auto(uid: int) -> None:
    threading.Thread(target=_db_write_auto, args=(uid,), daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════
#  ПОЛЬЗОВАТЕЛЬ
# ═══════════════════════════════════════════════════════════════════════
def init_user(tg_user: types.User) -> None:
    uid = tg_user.id
    ALL_USER_IDS.add(uid)
    with _DATA_LOCK:
        if uid in USER_DATA:
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
                    "first_name": doc.get("first_name"),
                    "username":   doc.get("username"),
                }
                with _DATA_LOCK:
                    USER_DATA[uid] = profile
                logger.info("↩️  Профиль %d восстановлен из MongoDB", uid)
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
        return USER_DATA.get(uid, {}).get("lang", "ru")


# ═══════════════════════════════════════════════════════════════════════
#  ЛОГИРОВАНИЕ В КАНАЛ
# ═══════════════════════════════════════════════════════════════════════
def log_action(tg_user: types.User, action: str, result: Optional[str] = None) -> None:
    def _send() -> None:
        try:
            first = tg_user.first_name or ""
            last  = f" {tg_user.last_name}" if tg_user.last_name else ""
            nick  = f"@{tg_user.username}" if tg_user.username else "—"
            lines = [
                "⚙️ <b>Лог действия</b>",
                f"👤 {first}{last}  |  {nick}  |  <code>{tg_user.id}</code>",
                f"🔘 <b>{action}</b>",
            ]
            if result:
                lines.append(f"📊 {result}")
            lines.append(f"🕒 {now_msk().strftime('%d.%m.%Y %H:%M:%S')} МСК")
            bot.send_message(ADMIN_LOG_CHAT_ID, "\n".join(lines), parse_mode="HTML")
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════
#  UI
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


# ═══════════════════════════════════════════════════════════════════════
#  API — ПОЛУЧЕНИЕ КУРСОВ 
# ═══════════════════════════════════════════════════════════════════════
_thread_local = threading.local()

def _get_session() -> requests.Session:
    """Thread-local сессия — потокобезопасный HTTP без кэширования."""
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        })
        _thread_local.session = s
    return _thread_local.session


def _fetch_upbit() -> Optional[float]:
    try:
        r = _get_session().get(
            f"https://api.upbit.com/v1/ticker?markets=KRW-USDT&_t={int(time.time() * 1000)}",
            timeout=_API_TIMEOUT,
        )
        r.raise_for_status()
        return float(r.json()[0]["trade_price"])
    except Exception as exc:
        logger.warning("Upbit: %s", exc)
        return None


def _fetch_bithumb() -> Optional[float]:
    try:
        r = _get_session().get(
            f"https://api.bithumb.com/v1/ticker?markets=KRW-USDT&_t={int(time.time() * 1000)}",
            timeout=_API_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            price = data[0].get("trade_price")
            if price is not None:
                return float(price)
    except Exception:
        pass
    try:
        r = _get_session().get(
            f"https://api.bithumb.com/public/ticker/USDT_KRW?_t={int(time.time() * 1000)}",
            timeout=_API_TIMEOUT,
        )
        r.raise_for_status()
        price = r.json()["data"]["closing_price"]
        if price:
            return float(price)
    except Exception as exc:
        logger.warning("Bithumb: %s", exc)
    return None


def _fetch_krw_rub() -> Optional[float]:
    """ГЛУБОКИЙ ПАРСИНГ GOOGLE FINANCE С 3-МЯ УРОВНЯМИ ЗАЩИТЫ ОТ СБОЕВ"""
    
    # Специфичные заголовки для Google, чтобы он думал, что мы из Chrome браузера
    google_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }

    try:
        # 1. Запрашиваем Google Finance
        url = f"https://www.google.com/finance/quote/KRW-RUB?hl=en&_t={int(time.time() * 1000)}"
        r = _get_session().get(url, headers=google_headers, timeout=_API_TIMEOUT)
        r.raise_for_status()

        # 2. Ищем курс по нескольким паттернам (Google постоянно меняет верстку!)
        
        # Паттерн А: Современный класс Google Finance
        match_a = re.search(r'class="YMlKec fxKbKc">([0-9.,]+)</div>', r.text)
        # Паттерн B: Старый атрибут data-last-price (на всякий случай)
        match_b = re.search(r'data-last-price="([0-9.]+)"', r.text)
        # Паттерн C: Скрытый мета-тег для поисковиков
        match_c = re.search(r'<meta itemprop="price" content="([0-9.]+)"', r.text)

        raw_price = None
        if match_a:
            raw_price = match_a.group(1).replace(',', '.') # Убираем запятые, если Google выдал русскую локаль
        elif match_b:
            raw_price = match_b.group(1)
        elif match_c:
            raw_price = match_c.group(1)

        # 3. Применяем курс, если он найден
        if raw_price:
            price = float(raw_price)
            if price > 0:
                return price * 1_000_000
    except Exception as exc:
        logger.warning("Google Finance не спарсился: %s", exc)

    # === РЕЗЕРВНЫЙ ВАРИАНТ, ЕСЛИ GOOGLE ВООБЩЕ ЛЕЖИТ ИЛИ ЗАБЛОКИРОВАЛ IP ===
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/KRWRUB=X?interval=1m&range=1d&_t={int(time.time() * 1000)}"
        r = _get_session().get(url, timeout=_API_TIMEOUT)
        r.raise_for_status()
        price = r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"]
        if price and float(price) > 0:
            return float(price) * 1_000_000
    except Exception as exc:
        logger.warning("Yahoo Finance также недоступен: %s", exc)

    return None


def _fetch_abcex() -> Tuple[Optional[float], Optional[float]]:
    try:
        r = _get_session().get(
            f"https://hub.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB&_t={int(time.time() * 1000)}",
            timeout=_API_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
        return float(d["bid"][0]["price"]), float(d["ask"][0]["price"])
    except Exception as exc:
        logger.warning("ABCEX: %s", exc)
        return None, None


def _safe_future(future: concurrent.futures.Future, default: Any = None) -> Any:
    try:
        return future.result(timeout=_API_MAX_WAIT)
    except concurrent.futures.TimeoutError:
        logger.error("API future timeout (%ds)", _API_MAX_WAIT)
        future.cancel()
        return default
    except Exception as exc:
        logger.error("API future error: %s", exc)
        return default


def fetch_all_rates() -> Dict[str, Optional[float]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        fu = pool.submit(_fetch_upbit)
        fb = pool.submit(_fetch_bithumb)
        fr = pool.submit(_fetch_krw_rub)
        fa = pool.submit(_fetch_abcex)
        upbit   = _safe_future(fu)
        bithumb = _safe_future(fb)
        krw_rub = _safe_future(fr)
        abcex   = _safe_future(fa, default=(None, None))
    ab_buy, ab_sell = abcex if isinstance(abcex, tuple) else (None, None)
    return {"upbit": upbit, "bithumb": bithumb, "krw_rub": krw_rub, "ab_buy": ab_buy, "ab_sell": ab_sell}


def fetch_auto_rates() -> Dict[str, Optional[float]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        fu = pool.submit(_fetch_upbit)
        fr = pool.submit(_fetch_krw_rub)
        fa = pool.submit(_fetch_abcex)
        upbit   = _safe_future(fu)
        krw_rub = _safe_future(fr)
        abcex   = _safe_future(fa, default=(None, None))
    ab_buy, ab_sell = abcex if isinstance(abcex, tuple) else (None, None)
    return {"upbit": upbit, "krw_rub": krw_rub, "ab_buy": ab_buy, "ab_sell": ab_sell}


def build_rate_message(rates: Dict[str, Optional[float]], lang: str) -> str:
    T  = LANGS[lang]
    ts = now_msk().strftime("%d.%m.%Y  %H:%M:%S")
    return (
        f"{T['rate_title']}\n\n"
        f"🇰🇷 <b>USDT → KRW</b>\n"
        f"  ◾ UPBIT:    <b>{fmt_num(rates['upbit'],   0)} ₩</b>\n"
        f"  ◾ BITHUMB:  <b>{fmt_num(rates['bithumb'], 0)} ₩</b>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"🇷🇺 <b>USDT → RUB  (ABCEX)</b>\n"
        f"  ◾ {T['buy']}:   <b>{fmt_num(rates['ab_buy'],  2)} ₽</b>\n"
        f"  ◾ {T['sell']}:  <b>{fmt_num(rates['ab_sell'], 2)} ₽</b>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"🇰🇷➡️🇷🇺 <b>KRW → RUB</b>\n"
        f"  ◾ 1 000 000 ₩ → <b>{fmt_num(rates['krw_rub'], 2)} ₽</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{T['updated']} <b>{ts}</b>\n\n"
        f"{T['contact']}"
    )


def build_auto_message(rates: Dict[str, Optional[float]]) -> str:
    usdt_krw = rates.get("upbit")
    ab_buy   = rates.get("ab_buy")
    ab_sell  = rates.get("ab_sell")
    if ab_buy is not None and ab_sell is not None:
        usdt_rub: Optional[float] = (ab_buy + ab_sell) / 2
    elif ab_buy is not None:
        usdt_rub = ab_buy
    elif ab_sell is not None:
        usdt_rub = ab_sell
    else:
        usdt_rub = None
    krw_rub = rates.get("krw_rub")

    part_krw     = f"{fmt_num(usdt_krw, 0)} ₩" if usdt_krw is not None else "— ₩"
    part_rub     = f"{fmt_num(usdt_rub, 2)} ₽"  if usdt_rub is not None else "— ₽"
    part_krw_rub = f"{int(round(krw_rub)):,} ₽"  if krw_rub  is not None else "— ₽"
    return f"🔔 AUTO: {part_krw} | {part_rub} | {part_krw_rub}"


# ═══════════════════════════════════════════════════════════════════════
#  ОБРАБОТЧИКИ TELEGRAM
# ═══════════════════════════════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def cmd_start(m: types.Message) -> None:
    init_user(m.from_user)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
        types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
    )
    bot.send_message(m.chat.id, "🇷🇺 Выберите язык  /  🇬🇧 Select language:", reply_markup=kb)
    log_action(m.from_user, "/start")


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


# ─── Показать курс ──────────────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_show"], LANGS["en"]["btn_show"]
))
def msg_show_rate(m: types.Message) -> None:
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

    rates    = fetch_all_rates()
    has_data = any(v is not None for v in rates.values())

    try:
        bot.delete_message(m.chat.id, anim.message_id)
    except Exception:
        pass

    if not has_data:
        bot.send_message(m.chat.id, T["error_fetch"], parse_mode="HTML")
        log_action(m.from_user, "Курс", result="⚠️ все API недоступны")
        return

    bot.send_message(m.chat.id, build_rate_message(rates, lang), parse_mode="HTML")

    with _DATA_LOCK:
        USER_DATA[uid]["requests"] += 1
        USER_DATA[uid]["last"]      = now_msk()
    save_user(uid)
    log_action(m.from_user, "Показать курс",
               result=f"Upbit {fmt_num(rates['upbit'],0)} ₩ | ABCEX {fmt_num(rates['ab_buy'],2)} ₽")


@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_profile"], LANGS["en"]["btn_profile"]
))
def msg_profile(m: types.Message) -> None:
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]
    with _DATA_LOCK:
        d = USER_DATA[uid].copy()
    nick = f"@{d['username']}" if d.get("username") else (d.get("first_name") or "—")
    text = (
        f"{T['prof_title']}\n\n"
        f"<b>ID:</b> <code>{uid}</code>\n"
        f"<b>Ник:</b> {nick}\n"
        f"<b>{T['prof_join']}</b> {fmt_dt(d.get('joined'))}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>{T['prof_reqs']}</b> {d.get('requests', 0)}\n"
        f"<b>{T['prof_last']}</b> {fmt_dt(d.get('last'))}"
    )
    bot.send_message(m.chat.id, text, parse_mode="HTML", reply_markup=main_keyboard(uid))
    log_action(m.from_user, "Профиль")


@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_feedback"], LANGS["en"]["btn_feedback"]
))
def msg_feedback_start(m: types.Message) -> None:
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]
    bot.send_message(m.chat.id, "✍️", reply_markup=main_keyboard(uid))
    reply = bot.send_message(m.chat.id, T["feedback_ask"],
                             reply_markup=types.ForceReply(selective=True))
    bot.register_next_step_handler(reply, _feedback_receive)
    log_action(m.from_user, "Отзыв — начало")


def _feedback_receive(m: types.Message) -> None:
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]
    if m.text and m.text.strip():
        first = m.from_user.first_name or ""
        last  = f" {m.from_user.last_name}" if m.from_user.last_name else ""
        nick  = f"@{m.from_user.username}" if m.from_user.username else "—"
        try:
            bot.send_message(
                ADMIN_LOG_CHAT_ID,
                f"🔴 <b>НОВЫЙ ОТЗЫВ</b>\n"
                f"👤 {first}{last}  |  {nick}  |  <code>{uid}</code>\n"
                f"💬 <i>{m.text}</i>\n"
                f"🕒 {now_msk().strftime('%d.%m.%Y %H:%M:%S')} МСК",
                parse_mode="HTML",
            )
        except Exception:
            pass
    bot.send_message(m.chat.id, T["feedback_ok"], reply_markup=main_keyboard(uid))


# ═══════════════════════════════════════════════════════════════════════
#  АВТООБНОВЛЕНИЕ — ПОЛЬЗОВАТЕЛЬ
# ═══════════════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_auto"], LANGS["en"]["btn_auto"]
))
def msg_auto_menu(m: types.Message) -> None:
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]
    kb   = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("⏰ 1H",  callback_data="auto_3600"),
        types.InlineKeyboardButton("⏰ 5H",  callback_data="auto_18000"),
        types.InlineKeyboardButton("⏰ 24H", callback_data="auto_86400"),
    )
    with _DATA_LOCK:
        is_active = uid in AUTO_USERS
    if is_active:
        kb.row(types.InlineKeyboardButton("🚫 Выключить", callback_data="auto_0"))
    bot.send_message(m.chat.id, T["auto_menu"], reply_markup=main_keyboard(uid))
    bot.send_message(m.chat.id, T["auto_choose"], reply_markup=kb)
    log_action(m.from_user, "Меню авто")


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
        resp = f"✅ {T['auto_off_msg']}"
        log_action(c.from_user, "Авто OFF")
    else:
        ts = now_msk()
        with _DATA_LOCK:
            AUTO_USERS[uid] = {"interval": val, "last": ts, "enabled_at": ts}
        save_auto(uid)
        resp = f"✅ {T['auto_on_msg']} {val // 3600}H."
        log_action(c.from_user, "Авто ON", result=f"{val // 3600}H")
    try:
        bot.edit_message_text(resp, c.message.chat.id, c.message.message_id, parse_mode="HTML")
    except Exception:
        pass
    bot.answer_callback_query(c.id)


@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_disable"], LANGS["en"]["btn_disable"]
))
def msg_disable_auto(m: types.Message) -> None:
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]
    with _DATA_LOCK:
        AUTO_USERS.pop(uid, None)
    save_auto(uid)
    bot.send_message(m.chat.id, f"✅ 🔕 {T['auto_off_msg']}",
                     parse_mode="HTML", reply_markup=main_keyboard(uid))
    log_action(m.from_user, "Отключил уведомления")


# ═══════════════════════════════════════════════════════════════════════
#  АДМИН-ПАНЕЛЬ
# ═══════════════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: (
    m.from_user.id == MY_ADMIN_ID
    and m.text in (LANGS["ru"]["btn_admin"], LANGS["en"]["btn_admin"])
))
def msg_admin_panel(m: types.Message) -> None:
    init_user(m.from_user)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 Статистика",              callback_data="adm_stat"),
        types.InlineKeyboardButton("👥 База пользователей",       callback_data="adm_users_0"),
        types.InlineKeyboardButton("📢 Рассылка всем",           callback_data="adm_bc"),
        types.InlineKeyboardButton("🔔 Упр. подписками (тихо)", callback_data="adm_auto_menu"),
    )
    bot.send_message(m.chat.id, "🛠 <b>Админ-панель</b>", reply_markup=kb)
    log_action(m.from_user, "Открыл Админ-панель")


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def cb_admin(c: types.CallbackQuery) -> None:
    if c.from_user.id != MY_ADMIN_ID:
        bot.answer_callback_query(c.id, "⛔️ Доступ запрещён")
        return
    action = c.data[4:]

    if action == "stat":
        with _DATA_LOCK:
            n_u = len(ALL_USER_IDS)
            n_s = len(AUTO_USERS)
            n_r = sum(u.get("requests", 0) for u in USER_DATA.values())
        bot.answer_callback_query(c.id)
        bot.send_message(
            c.message.chat.id,
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Пользователей:  <b>{n_u}</b>\n"
            f"🔔 Подписок:       <b>{n_s}</b>\n"
            f"📈 Запросов курса: <b>{n_r}</b>\n"
            f"🕒 {now_msk().strftime('%d.%m.%Y %H:%M:%S')} МСК",
            parse_mode="HTML",
        )

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
            nick = f"@{d.get('username')}" if d.get("username") else (d.get("first_name") or str(uid))
            txt += f"{icon} <code>{uid}</code>  {nick}\n"
            kb.add(types.InlineKeyboardButton(f"🔍 {nick}", callback_data=f"adm_detail_{uid}"))
        nav = []
        if page > 0:
            nav.append(types.InlineKeyboardButton("◀️", callback_data=f"adm_users_{page-1}"))
        if e < total:
            nav.append(types.InlineKeyboardButton("▶️", callback_data=f"adm_users_{page+1}"))
        if nav:
            kb.row(*nav)
        bot.answer_callback_query(c.id)
        try:
            bot.edit_message_text(txt, c.message.chat.id, c.message.message_id,
                                  parse_mode="HTML", reply_markup=kb)
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
        nick = f"@{d.get('username')}" if d.get("username") else (d.get("first_name") or "—")
        auto_info = (
            f"\n🟢 <b>Автообновление: ВКЛЮЧЕНО</b>\n"
            f"  ⏱ Интервал:            <b>каждые {a['interval']//3600}H</b>\n"
            f"  📅 Включено:            {fmt_dt(a.get('enabled_at'))}\n"
            f"  📤 Последняя отправка: {fmt_dt(a.get('last'))}"
        ) if a else "\n🔴 <b>Автообновление: ВЫКЛЮЧЕНО</b>"
        text = (
            f"🔍 <b>Карточка пользователя</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>ID:</b>       <code>{uid}</code>\n"
            f"<b>Ник:</b>      {nick}\n"
            f"<b>Язык:</b>     {'🇷🇺 RU' if d.get('lang')=='ru' else '🇬🇧 EN'}\n"
            f"<b>В боте с:</b> {fmt_dt(d.get('joined'))}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>Запросов:</b>  {d.get('requests',0)}\n"
            f"<b>Последний:</b> {fmt_dt(d.get('last'))}"
            f"{auto_info}"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("◀️ К списку", callback_data="adm_users_0"))
        bot.answer_callback_query(c.id)
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                                  parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)

    elif action == "auto_menu":
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("✅ Включить всем",    callback_data="adm_auto_all"),
            types.InlineKeyboardButton("👤 Подключить по ID", callback_data="adm_auto_id"),
        )
        bot.edit_message_text(
            "⚙️ <b>Управление подписками</b>\n\nПользователи <b>не получат</b> уведомлений.",
            c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb,
        )

    elif action == "auto_all":
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("1H",  callback_data="adm_all_3600"),
            types.InlineKeyboardButton("5H",  callback_data="adm_all_18000"),
            types.InlineKeyboardButton("24H", callback_data="adm_all_86400"),
        )
        bot.edit_message_text("⏳ Выберите интервал для <b>всех</b>:",
                              c.message.chat.id, c.message.message_id,
                              parse_mode="HTML", reply_markup=kb)

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
        bot.edit_message_text(
            f"✅ Подписка {hours}H для <b>{count}</b> чел.",
            c.message.chat.id, c.message.message_id, parse_mode="HTML",
        )
        log_action(c.from_user, f"Массовая подписка {hours}H", result=f"{count} чел.")

    elif action == "auto_id":
        bot.answer_callback_query(c.id)
        msg = bot.send_message(c.message.chat.id, "Введите <b>ID</b> пользователя:",
                               parse_mode="HTML", reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, _adm_auto_get_id)

    elif action == "bc":
        bot.answer_callback_query(c.id)
        msg = bot.send_message(c.message.chat.id, "✏️ Введите текст рассылки:",
                               reply_markup=types.ForceReply(selective=True))
        bot.register_next_step_handler(msg, _adm_broadcast)

    else:
        bot.answer_callback_query(c.id)


def _adm_auto_get_id(m: types.Message) -> None:
    if not m.text:
        return
    try:
        tid = int(m.text.strip())
    except ValueError:
        bot.send_message(m.chat.id, "❌ Некорректный ID.", reply_markup=main_keyboard(m.from_user.id))
        return
    msg = bot.send_message(
        m.chat.id,
        f"Интервал в <b>часах</b> (1/5/24) для <code>{tid}</code>:",
        parse_mode="HTML", reply_markup=types.ForceReply(selective=True),
    )
    bot.register_next_step_handler(msg, lambda s: _adm_auto_set(s, tid))


def _adm_auto_set(m: types.Message, tid: int) -> None:
    if not m.text:
        return
    try:
        hours = int(m.text.strip())
        if hours <= 0:
            raise ValueError
    except ValueError:
        bot.send_message(m.chat.id, "❌ Введите положительное число.",
                         reply_markup=main_keyboard(m.from_user.id))
        return
    ts = now_msk()
    with _DATA_LOCK:
        AUTO_USERS[tid] = {"interval": hours * 3600, "last": ts, "enabled_at": ts}
    save_auto(tid)
    bot.send_message(
        m.chat.id,
        f"✅ <code>{tid}</code> — каждые <b>{hours}H</b>.",
        parse_mode="HTML", reply_markup=main_keyboard(m.from_user.id),
    )
    log_action(m.from_user, f"Тихая подписка {tid}", result=f"{hours}H")


def _adm_broadcast(m: types.Message) -> None:
    if not m.text or not m.text.strip():
        bot.send_message(m.chat.id, "❌ Пустое сообщение.",
                         reply_markup=main_keyboard(m.from_user.id))
        return
    def _do_broadcast() -> None:
        with _DATA_LOCK:
            ids = set(ALL_USER_IDS)
        sent = failed = 0
        for uid in ids:
            try:
                bot.send_message(uid, f"📢 <b>Уведомление</b>\n\n{m.text}", parse_mode="HTML")
                sent += 1
                time.sleep(0.05)
            except Exception:
                failed += 1
        bot.send_message(
            m.chat.id,
            f"✅ Готово. Доставлено: <b>{sent}</b>  ❌ Ошибок: <b>{failed}</b>",
            parse_mode="HTML", reply_markup=main_keyboard(m.from_user.id),
        )
        log_action(m.from_user, "Рассылка", result=f"sent={sent} failed={failed}")
    threading.Thread(target=_do_broadcast, daemon=True).start()
    bot.send_message(m.chat.id, "⏳ Рассылка запущена...",
                     reply_markup=main_keyboard(m.from_user.id))


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
                except Exception as exc:
                    err = str(exc).lower()
                    if any(kw in err for kw in ("blocked","not found","deactivated","forbidden","chat not found")):
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
    
    _PING_AGENT.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })

    if not RENDER_URL:
        logger.error("🚨 КРИТИЧЕСКАЯ ОШИБКА АНТИ-СНА: Переменная RENDER_URL не задана!")
        return 

    def _ping_self() -> None:
        try:
            url = f"{RENDER_URL}/ping?t={int(time.time())}"
            r = _PING_AGENT.get(url, timeout=15)
            logger.info("🏓 anti_sleep self-ping: HTTP %d", r.status_code)
        except Exception as exc:
            logger.warning("🏓 anti_sleep self-ping fail: %s", exc)

    logger.info("🏓 anti_sleep: запущен (интервал %d мин). Цель: %s", _INTERVAL // 60, RENDER_URL)

    time.sleep(5)
    _ping_self()

    while True:
        try:
            time.sleep(_INTERVAL)
            _ping_self()
        except Exception as exc:
            logger.error("anti_sleep_worker error: %s", exc)
            time.sleep(60)


# ═══════════════════════════════════════════════════════════════════════
#  FLASK
# ═══════════════════════════════════════════════════════════════════════
flask_app = Flask(__name__)


@flask_app.route("/")
def health() -> Any:
    with _DATA_LOCK:
        n_u = len(ALL_USER_IDS)
        n_s = len(AUTO_USERS)
    return jsonify({
        "status": "ok",
        "users":  n_u,
        "subs":   n_s,
        "time":   now_msk().strftime("%Y-%m-%d %H:%M:%S MSK"),
    }), 200


@flask_app.route("/ping")
def ping_endpoint() -> Any:
    return jsonify({"pong": True, "time": now_msk().isoformat()}), 200


# ═══════════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        logger.info("Webhook снят")
    except Exception as exc:
        logger.warning("remove_webhook: %s", exc)

    load_db()

    threading.Thread(target=_auto_worker,       daemon=True, name="auto_worker").start()
    threading.Thread(target=_anti_sleep_worker, daemon=True, name="anti_sleep").start()

    port = int(os.environ.get("PORT", 10_000))
    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=port, use_reloader=False, debug=False),
        daemon=True, name="flask",
    ).start()
    logger.info("🌐 Flask на порту %d", port)

    logger.info("🤖 Бот запущен")
    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=20,
                skip_pending=True,
                allowed_updates=["message", "callback_query"],
            )
        except Exception as exc:
            logger.error("polling упал: %s — перезапуск через 10с", exc)
            time.sleep(10)
