# -*- coding: utf-8 -*-
"""
P2P Exchange Rate Bot — Production Grade
"""

import os
import logging
import threading
import time
import concurrent.futures
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import requests
from dotenv import load_dotenv
import telebot
from telebot import types
from flask import Flask
from pymongo import MongoClient
import certifi

# ═══════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN: str    = os.environ["TELEGRAM_TOKEN"]
MY_ADMIN_ID: int       = 5266659205
ADMIN_LOG_CHAT_ID: int = -1003264764082
MOSCOW_TZ              = timezone(timedelta(hours=3))

_DATA_LOCK = threading.Lock()   # потокобезопасность для USER_DATA / AUTO_USERS

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML", threaded=True)

# ═══════════════════════════════════════════════════════════════
#  MONGODB
# ═══════════════════════════════════════════════════════════════
_MONGO_URI = os.getenv("MONGO_URI")
users_col  = None
auto_col   = None

if _MONGO_URI:
    try:
        _client = MongoClient(
            _MONGO_URI,
            tlsCAFile=certifi.where(),
            serverSelectionTimeoutMS=5_000,
            connectTimeoutMS=5_000,
            socketTimeoutMS=10_000,
        )
        _client.server_info()
        _db       = _client.p2p_bot_db
        users_col = _db.users
        auto_col  = _db.auto_updates
        users_col.create_index("_id", unique=True)
        auto_col.create_index("_id",  unique=True)
        logger.info("MongoDB Atlas: соединение установлено")
    except Exception as exc:
        logger.error("MongoDB недоступна: %s", exc)
else:
    logger.warning("MONGO_URI не задан — данные только в RAM")

# ═══════════════════════════════════════════════════════════════
#  ЛОКАЛИЗАЦИЯ
# ═══════════════════════════════════════════════════════════════
LANGS: dict = {
    "ru": {
        "btn_show":     "📊 Показать курс",
        "btn_auto":     "🔔 Автообновление",
        "btn_profile":  "👤 Профиль",
        "btn_feedback": "✍️ Отзыв",
        "btn_disable":  "🚫 Отключить уведомления",
        "btn_admin":    "🛠 Админка",
        "welcome":      "👋 Привет!\n\nВыбери нужный раздел ниже 👇",
        "loading":      "⏳ Загружаю курсы...",
        "error_fetch":  "⚠️ Не удалось получить курс. Попробуйте позже.",
        "rate_title":   "💱 <b>АКТУАЛЬНЫЕ КУРСЫ</b>",
        "updated":      "⏱ Обновлено:",
        "contact":      "💰 Обмен любых сумм и валют — по договорённости.\n📞 Контакт: @Abdulkhaiii",
        "auto_menu":    "Выбери частоту автообновления:",
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
        "loading":      "⏳ Fetching rates...",
        "error_fetch":  "⚠️ Failed to get rates. Please try again later.",
        "rate_title":   "💱 <b>CURRENT RATES</b>",
        "updated":      "⏱ Updated:",
        "contact":      "💰 Exchange of any amounts — by agreement.\n📞 Contact: @Abdulkhaiii",
        "auto_menu":    "Select update frequency:",
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

# ═══════════════════════════════════════════════════════════════
#  RAM-ХРАНИЛИЩЕ
# ═══════════════════════════════════════════════════════════════
USER_DATA:   dict = {}
AUTO_USERS:  dict = {}
ALL_USER_IDS: set = set()

# ═══════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════
def now_msk() -> datetime:
    return datetime.now(MOSCOW_TZ)


def fmt_num(value, decimals: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{value:,.{decimals}f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Парсит ISO-строку → datetime с timezone (всегда МСК если нет tz)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MOSCOW_TZ)
        return dt
    except ValueError:
        return None


def _elapsed(dt: Optional[datetime]) -> float:
    """Секунд прошло с dt. Безопасно для naive/aware datetime."""
    if dt is None:
        return float("inf")
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MOSCOW_TZ)
        return (now_msk() - dt).total_seconds()
    except Exception:
        return float("inf")


def fmt_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%d.%m.%Y  %H:%M:%S") if dt else "—"


# ═══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════
def load_db() -> None:
    if users_col is None:
        return
    try:
        logger.info("Загрузка данных из MongoDB...")
        for doc in users_col.find():
            uid = doc["_id"]
            ALL_USER_IDS.add(uid)
            USER_DATA[uid] = {
                "lang":       doc.get("lang", "ru"),
                "requests":   doc.get("requests", 0),
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
        logger.info("БД загружена: %d профилей, %d подписок", len(USER_DATA), len(AUTO_USERS))
    except Exception as exc:
        logger.error("Ошибка загрузки БД: %s", exc)


def save_user(uid: int) -> None:
    if users_col is None or uid not in USER_DATA:
        return
    def _w():
        try:
            with _DATA_LOCK:
                u = USER_DATA.get(uid)
            if not u:
                return
            users_col.update_one({"_id": uid}, {"$set": {
                "lang":       u["lang"],
                "requests":   u["requests"],
                "last":       u["last"].isoformat()   if u["last"]   else None,
                "joined":     u["joined"].isoformat() if u["joined"] else None,
                "first_name": u["first_name"],
                "username":   u["username"],
            }}, upsert=True)
        except Exception as exc:
            logger.error("save_user %d: %s", uid, exc)
    threading.Thread(target=_w, daemon=True).start()


def save_auto(uid: int) -> None:
    if auto_col is None:
        return
    def _w():
        try:
            with _DATA_LOCK:
                entry = AUTO_USERS.get(uid)
            if entry:
                auto_col.update_one({"_id": uid}, {"$set": {
                    "interval":   entry["interval"],
                    "last":       entry["last"].isoformat()       if entry.get("last")       else None,
                    "enabled_at": entry["enabled_at"].isoformat() if entry.get("enabled_at") else None,
                }}, upsert=True)
            else:
                auto_col.delete_one({"_id": uid})
        except Exception as exc:
            logger.error("save_auto %d: %s", uid, exc)
    threading.Thread(target=_w, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
#  ИНИЦИАЛИЗАЦИЯ ПОЛЬЗОВАТЕЛЯ
# ═══════════════════════════════════════════════════════════════
def init_user(tg_user) -> None:
    uid = tg_user.id
    ALL_USER_IDS.add(uid)
    with _DATA_LOCK:
        if uid in USER_DATA:
            return
    if users_col is not None:
        try:
            doc = users_col.find_one({"_id": uid})
            if doc:
                profile = {
                    "lang":       doc.get("lang", "ru"),
                    "requests":   doc.get("requests", 0),
                    "last":       _parse_iso(doc.get("last")),
                    "joined":     _parse_iso(doc.get("joined")) or now_msk(),
                    "first_name": doc.get("first_name"),
                    "username":   doc.get("username"),
                }
                with _DATA_LOCK:
                    USER_DATA[uid] = profile
                logger.info("Профиль %d восстановлен из MongoDB", uid)
                return
        except Exception as exc:
            logger.error("init_user чтение %d: %s", uid, exc)
    profile = {
        "lang":       "ru",
        "requests":   0,
        "last":       None,
        "joined":     now_msk(),
        "first_name": tg_user.first_name,
        "username":   tg_user.username,
    }
    with _DATA_LOCK:
        USER_DATA[uid] = profile
    save_user(uid)
    logger.info("Новый профиль: %d (%s)", uid, tg_user.first_name)


def get_lang(uid: int) -> str:
    with _DATA_LOCK:
        return USER_DATA.get(uid, {}).get("lang", "ru")


# ═══════════════════════════════════════════════════════════════
#  ЛОГИРОВАНИЕ В КАНАЛ
# ═══════════════════════════════════════════════════════════════
def log_action(tg_user, action: str, result: Optional[str] = None) -> None:
    def _s():
        try:
            name = (tg_user.first_name or "") + (f" {tg_user.last_name}" if tg_user.last_name else "")
            nick = f"@{tg_user.username}" if tg_user.username else "—"
            text = (
                f"⚙️ <b>Лог</b>\n"
                f"👤 {name}  |  {nick}  |  <code>{tg_user.id}</code>\n"
                f"🔘 <b>{action}</b>\n"
            )
            if result:
                text += f"📊 {result}\n"
            text += f"🕒 {now_msk().strftime('%H:%M:%S')} МСК"
            bot.send_message(ADMIN_LOG_CHAT_ID, text, parse_mode="HTML")
        except Exception:
            pass
    threading.Thread(target=_s, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
#  UI
# ═══════════════════════════════════════════════════════════════
def main_keyboard(uid: int) -> types.ReplyKeyboardMarkup:
    l  = get_lang(uid)
    T  = LANGS[l]
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(T["btn_show"],    T["btn_auto"])
    kb.row(T["btn_profile"], T["btn_feedback"])
    kb.row(T["btn_disable"])
    if uid == MY_ADMIN_ID:
        kb.row(T["btn_admin"])
    return kb


# ═══════════════════════════════════════════════════════════════
#  API — ПОЛУЧЕНИЕ КУРСОВ
# ═══════════════════════════════════════════════════════════════
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (P2PBot/3.0)"})
_TIMEOUT = 7


def _upbit() -> Optional[float]:
    try:
        r = _SESSION.get("https://api.upbit.com/v1/ticker?markets=KRW-USDT", timeout=_TIMEOUT)
        r.raise_for_status()
        return float(r.json()[0]["trade_price"])
    except Exception as e:
        logger.warning("Upbit: %s", e)
        return None


def _bithumb() -> Optional[float]:
    """Пробуем v1 (новый) → fallback на v2 (старый)."""
    # Новый endpoint Bithumb v1
    try:
        r = _SESSION.get("https://api.bithumb.com/v1/ticker?markets=KRW-USDT", timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return float(data[0]["trade_price"])
    except Exception:
        pass
    # Fallback: старый endpoint
    try:
        r = _SESSION.get("https://api.bithumb.com/public/ticker/USDT_KRW", timeout=_TIMEOUT)
        r.raise_for_status()
        return float(r.json()["data"]["closing_price"])
    except Exception as e:
        logger.warning("Bithumb: %s", e)
        return None


def _krw_rub() -> Optional[float]:
    try:
        r = _SESSION.get("https://open.er-api.com/v6/latest/RUB", timeout=_TIMEOUT)
        r.raise_for_status()
        krw = r.json()["rates"]["KRW"]
        return 1_000_000 / krw if krw else None
    except Exception as e:
        logger.warning("ExchangeRate: %s", e)
        return None


def _abcex() -> Tuple[Optional[float], Optional[float]]:
    try:
        r = _SESSION.get(
            "https://hub.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB",
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
        return float(d["bid"][0]["price"]), float(d["ask"][0]["price"])
    except Exception as e:
        logger.warning("ABCEX: %s", e)
        return None, None


def fetch_all_rates() -> dict:
    """Параллельный запрос всех источников. Никогда не бросает исключение."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        fu = ex.submit(_upbit)
        fb = ex.submit(_bithumb)
        fr = ex.submit(_krw_rub)
        fa = ex.submit(_abcex)
        upbit   = fu.result()
        bithumb = fb.result()
        krw_rub = fr.result()
        ab_buy, ab_sell = fa.result()
    return {
        "upbit":   upbit,
        "bithumb": bithumb,
        "krw_rub": krw_rub,
        "ab_buy":  ab_buy,
        "ab_sell": ab_sell,
    }


def build_rate_message(rates: dict, lang: str) -> str:
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


# ═══════════════════════════════════════════════════════════════
#  ОБРАБОТЧИКИ
# ═══════════════════════════════════════════════════════════════

# ── /start ───────────────────────────────────────────────────
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


# ── Язык ─────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def cb_lang(c: types.CallbackQuery) -> None:
    lang = c.data.split("_", 1)[1]
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
    log_action(c.from_user, "Язык", result="RU" if lang == "ru" else "EN")


# ── Показать курс ────────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_show"], LANGS["en"]["btn_show"]
))
def msg_show_rate(m: types.Message) -> None:
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]

    # Сразу отправляем ответ с клавиатурой (клавиатура не пропадёт)
    placeholder = bot.send_message(m.chat.id, T["loading"], reply_markup=main_keyboard(uid))

    rates    = fetch_all_rates()
    has_data = any(v is not None for v in rates.values())

    if not has_data:
        try:
            bot.edit_message_text(T["error_fetch"], m.chat.id, placeholder.message_id)
        except Exception:
            bot.send_message(m.chat.id, T["error_fetch"])
        log_action(m.from_user, "Курс", result="все API недоступны")
        return

    text = build_rate_message(rates, lang)
    try:
        # edit_message_text НЕ трогает reply_markup основной клавиатуры
        bot.edit_message_text(text, m.chat.id, placeholder.message_id, parse_mode="HTML")
    except Exception as exc:
        logger.warning("edit_message_text: %s", exc)
        bot.send_message(m.chat.id, text, parse_mode="HTML")

    with _DATA_LOCK:
        USER_DATA[uid]["requests"] += 1
        USER_DATA[uid]["last"]      = now_msk()
    save_user(uid)
    log_action(m.from_user, "Курс",
               result=f"Upbit {fmt_num(rates['upbit'],0)} ₩ | ABCEX {fmt_num(rates['ab_buy'],2)} ₽")


# ── Профиль ──────────────────────────────────────────────────
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
    nick = f"@{d['username']}" if d["username"] else (d["first_name"] or "—")
    text = (
        f"{T['prof_title']}\n\n"
        f"<b>ID:</b> <code>{uid}</code>\n"
        f"<b>Ник:</b> {nick}\n"
        f"<b>{T['prof_join']}</b> {fmt_dt(d['joined'])}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>{T['prof_reqs']}</b> {d['requests']}\n"
        f"<b>{T['prof_last']}</b> {fmt_dt(d['last'])}"
    )
    bot.send_message(m.chat.id, text, parse_mode="HTML", reply_markup=main_keyboard(uid))
    log_action(m.from_user, "Профиль")


# ── Отзыв ────────────────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text in (
    LANGS["ru"]["btn_feedback"], LANGS["en"]["btn_feedback"]
))
def msg_feedback_start(m: types.Message) -> None:
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]
    bot.send_message(m.chat.id, "✍️", reply_markup=main_keyboard(uid))
    reply = bot.send_message(m.chat.id, T["feedback_ask"], reply_markup=types.ForceReply(selective=True))
    bot.register_next_step_handler(reply, _feedback_receive)
    log_action(m.from_user, "Отзыв — начало")


def _feedback_receive(m: types.Message) -> None:
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]
    if m.text and m.text.strip():
        name = (m.from_user.first_name or "") + (f" {m.from_user.last_name}" if m.from_user.last_name else "")
        nick = f"@{m.from_user.username}" if m.from_user.username else "—"
        try:
            bot.send_message(
                ADMIN_LOG_CHAT_ID,
                f"🔴 <b>НОВЫЙ ОТЗЫВ</b>\n"
                f"👤 {name}  |  {nick}  |  <code>{uid}</code>\n"
                f"💬 <i>{m.text}</i>\n"
                f"🕒 {now_msk().strftime('%d.%m.%Y %H:%M:%S')} МСК",
                parse_mode="HTML",
            )
        except Exception:
            pass
    bot.send_message(m.chat.id, T["feedback_ok"], reply_markup=main_keyboard(uid))


# ═══════════════════════════════════════════════════════════════
#  АВТООБНОВЛЕНИЕ
# ═══════════════════════════════════════════════════════════════
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
        types.InlineKeyboardButton("1H",  callback_data="auto_3600"),
        types.InlineKeyboardButton("5H",  callback_data="auto_18000"),
        types.InlineKeyboardButton("24H", callback_data="auto_86400"),
    )
    with _DATA_LOCK:
        active = uid in AUTO_USERS
    if active:
        kb.row(types.InlineKeyboardButton("🚫 Выключить", callback_data="auto_0"))
    bot.send_message(m.chat.id, T["auto_menu"], reply_markup=main_keyboard(uid))
    bot.send_message(m.chat.id, "⬇️ Выбери интервал:", reply_markup=kb)
    log_action(m.from_user, "Меню авто")


@bot.callback_query_handler(func=lambda c: c.data.startswith("auto_"))
def cb_auto(c: types.CallbackQuery) -> None:
    init_user(c.from_user)
    uid  = c.from_user.id
    lang = get_lang(uid)
    T    = LANGS[lang]
    val  = int(c.data.split("_", 1)[1])
    if val == 0:
        with _DATA_LOCK:
            AUTO_USERS.pop(uid, None)
        save_auto(uid)
        resp = f"✅ {T['auto_off_msg']}"
        log_action(c.from_user, "Авто OFF")
    else:
        now = now_msk()
        with _DATA_LOCK:
            AUTO_USERS[uid] = {"interval": val, "last": now, "enabled_at": now}
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
    bot.send_message(m.chat.id, f"✅ 🔕 {T['auto_off_msg']}", parse_mode="HTML", reply_markup=main_keyboard(uid))
    log_action(m.from_user, "Отключил уведомления")


# ═══════════════════════════════════════════════════════════════
#  АДМИН-ПАНЕЛЬ
# ═══════════════════════════════════════════════════════════════
@bot.message_handler(func=lambda m: (
    m.from_user.id == MY_ADMIN_ID and
    m.text in (LANGS["ru"]["btn_admin"], LANGS["en"]["btn_admin"])
))
def msg_admin_panel(m: types.Message) -> None:
    init_user(m.from_user)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 Статистика",             callback_data="adm_stat"),
        types.InlineKeyboardButton("👥 База пользователей",      callback_data="adm_users_0"),
        types.InlineKeyboardButton("📢 Рассылка всем",          callback_data="adm_bc"),
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

    # ── Статистика ─────────────────────────────────
    if action == "stat":
        with _DATA_LOCK:
            total_u = len(ALL_USER_IDS)
            total_s = len(AUTO_USERS)
            total_r = sum(u["requests"] for u in USER_DATA.values())
        bot.answer_callback_query(c.id)
        bot.send_message(
            c.message.chat.id,
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Пользователей:  <b>{total_u}</b>\n"
            f"🔔 Подписок:       <b>{total_s}</b>\n"
            f"📈 Запросов курса: <b>{total_r}</b>\n"
            f"🕒 {now_msk().strftime('%d.%m.%Y %H:%M:%S')} МСК",
            parse_mode="HTML",
        )

    # ── Список пользователей ────────────────────────
    elif action.startswith("users_"):
        page = int(action.split("_", 1)[1])
        pg   = 8
        with _DATA_LOCK:
            ids = sorted(ALL_USER_IDS)
        total = len(ids)
        s     = page * pg
        e     = min(s + pg, total)
        chunk = ids[s:e]

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
            bot.edit_message_text(txt, c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, txt, parse_mode="HTML", reply_markup=kb)

    # ── Карточка пользователя ───────────────────────
    elif action.startswith("detail_"):
        uid = int(action.split("_", 1)[1])
        with _DATA_LOCK:
            d = USER_DATA.get(uid, {}).copy()
            a = AUTO_USERS.get(uid)
        nick = f"@{d.get('username')}" if d.get("username") else (d.get("first_name") or "—")
        auto_block = (
            f"\n🟢 <b>Автообновление: ВКЛЮЧЕНО</b>\n"
            f"  ⏱ Интервал:            <b>каждые {a['interval'] // 3600}H</b>\n"
            f"  📅 Включено:           {fmt_dt(a.get('enabled_at'))}\n"
            f"  📤 Последняя отправка: {fmt_dt(a.get('last'))}"
        ) if a else "\n🔴 <b>Автообновление: ВЫКЛЮЧЕНО</b>"

        text = (
            f"🔍 <b>Карточка пользователя</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>ID:</b>   <code>{uid}</code>\n"
            f"<b>Ник:</b>  {nick}\n"
            f"<b>Язык:</b> {'🇷🇺 RU' if d.get('lang') == 'ru' else '🇬🇧 EN'}\n"
            f"<b>В боте с:</b> {fmt_dt(d.get('joined'))}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>Запросов:</b> {d.get('requests', 0)}\n"
            f"<b>Последний:</b> {fmt_dt(d.get('last'))}"
            f"{auto_block}"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("◀️ К списку", callback_data="adm_users_0"))
        bot.answer_callback_query(c.id)
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)

    # ── Меню тихих подписок ─────────────────────────
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
        bot.edit_message_text(
            "⏳ Выберите интервал для <b>всех</b>:",
            c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb,
        )

    elif action.startswith("all_"):
        val   = int(action.split("_", 1)[1])
        hours = val // 3600
        now   = now_msk()
        count = 0
        with _DATA_LOCK:
            ids = set(ALL_USER_IDS)
        for uid in ids:
            try:
                with _DATA_LOCK:
                    AUTO_USERS[uid] = {"interval": val, "last": now, "enabled_at": now}
                save_auto(uid)
                count += 1
            except Exception as exc:
                logger.error("Тихая подписка %d: %s", uid, exc)
        bot.edit_message_text(
            f"✅ Подписка {hours}H активирована для <b>{count}</b> чел.",
            c.message.chat.id, c.message.message_id, parse_mode="HTML",
        )
        log_action(c.from_user, f"Массовая подписка {hours}H", result=f"{count} чел.")

    elif action == "auto_id":
        bot.answer_callback_query(c.id)
        msg = bot.send_message(
            c.message.chat.id, "Введите <b>ID</b> пользователя:",
            parse_mode="HTML", reply_markup=types.ForceReply(selective=True),
        )
        bot.register_next_step_handler(msg, _adm_auto_get_id)

    elif action == "bc":
        bot.answer_callback_query(c.id)
        msg = bot.send_message(
            c.message.chat.id, "✏️ Введите текст рассылки:",
            reply_markup=types.ForceReply(selective=True),
        )
        bot.register_next_step_handler(msg, _adm_broadcast)


def _adm_auto_get_id(m: types.Message) -> None:
    try:
        tid = int(m.text.strip())
    except ValueError:
        bot.send_message(m.chat.id, "❌ Некорректный ID.", reply_markup=main_keyboard(m.from_user.id))
        return
    msg = bot.send_message(
        m.chat.id, f"Интервал в <b>часах</b> для <code>{tid}</code> (1/5/24):",
        parse_mode="HTML", reply_markup=types.ForceReply(selective=True),
    )
    bot.register_next_step_handler(msg, lambda s: _adm_auto_set(s, tid))


def _adm_auto_set(m: types.Message, tid: int) -> None:
    try:
        hours = int(m.text.strip())
    except ValueError:
        bot.send_message(m.chat.id, "❌ Введите число.", reply_markup=main_keyboard(m.from_user.id))
        return
    now = now_msk()
    with _DATA_LOCK:
        AUTO_USERS[tid] = {"interval": hours * 3600, "last": now, "enabled_at": now}
    save_auto(tid)
    bot.send_message(
        m.chat.id,
        f"✅ <code>{tid}</code> — подписка каждые <b>{hours}H</b>.",
        parse_mode="HTML", reply_markup=main_keyboard(m.from_user.id),
    )
    log_action(m.from_user, f"Тихая подписка {tid}", result=f"{hours}H")


def _adm_broadcast(m: types.Message) -> None:
    if not m.text or not m.text.strip():
        bot.send_message(m.chat.id, "❌ Пустое сообщение.", reply_markup=main_keyboard(m.from_user.id))
        return
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
        f"✅ Рассылка завершена.\n📤 Доставлено: <b>{sent}</b>  ❌ Ошибок: <b>{failed}</b>",
        parse_mode="HTML", reply_markup=main_keyboard(m.from_user.id),
    )
    log_action(m.from_user, "Рассылка", result=f"sent={sent} failed={failed}")


# ── Fallback ─────────────────────────────────────────────────
@bot.message_handler(func=lambda m: True)
def msg_fallback(m: types.Message) -> None:
    init_user(m.from_user)
    uid  = m.from_user.id
    lang = get_lang(uid)
    bot.send_message(m.chat.id, LANGS[lang]["menu_updated"], reply_markup=main_keyboard(uid))


# ═══════════════════════════════════════════════════════════════
#  ФОНОВЫЙ ВОРКЕР АВТООБНОВЛЕНИЯ
# ═══════════════════════════════════════════════════════════════
def _auto_worker() -> None:
    logger.info("auto_worker запущен")
    while True:
        time.sleep(60)
        now = now_msk()

        # Собираем только тех, у кого вышел интервал
        with _DATA_LOCK:
            due = {
                cid: cfg.copy()
                for cid, cfg in AUTO_USERS.items()
                if _elapsed(cfg.get("last")) >= cfg["interval"]
            }
        if not due:
            continue

        # Один общий запрос к API для всего цикла
        rates    = fetch_all_rates()
        has_data = any(v is not None for v in rates.values())
        if not has_data:
            logger.warning("auto_worker: API недоступен")
            continue

        for cid, cfg in due.items():
            try:
                lang = get_lang(cid)
                text = (
                    f"🔔 <b>{'АВТО-КУРС' if lang == 'ru' else 'AUTO-RATE'}</b>\n\n"
                    f"🇰🇷 Upbit:  <b>{fmt_num(rates['upbit'],   0)} ₩</b>\n"
                    f"🇷🇺 ABCEX:  <b>{fmt_num(rates['ab_buy'], 2)} ₽</b>\n"
                    f"🔄 1M ₩ ≈ <b>{fmt_num(rates['krw_rub'], 2)} ₽</b>\n"
                    f"⏱ {now.strftime('%H:%M:%S')} МСК"
                )
                bot.send_message(cid, text, parse_mode="HTML")
                with _DATA_LOCK:
                    if cid in AUTO_USERS:
                        AUTO_USERS[cid]["last"] = now
                save_auto(cid)
            except Exception as exc:
                if any(kw in str(exc).lower() for kw in ("blocked", "not found", "deactivated", "forbidden")):
                    logger.warning("Пользователь %d заблокировал бота", cid)
                    with _DATA_LOCK:
                        AUTO_USERS.pop(cid, None)
                    save_auto(cid)
                else:
                    logger.error("auto_worker %d: %s", cid, exc)


# ═══════════════════════════════════════════════════════════════
#  FLASK — HEALTH CHECK
# ═══════════════════════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    with _DATA_LOCK:
        u = len(ALL_USER_IDS)
        s = len(AUTO_USERS)
    return {"status": "ok", "users": u, "subs": s, "time": now_msk().strftime("%Y-%m-%d %H:%M:%S MSK")}, 200


# ═══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    try:
        bot.remove_webhook()
    except Exception:
        pass

    load_db()

    threading.Thread(target=_auto_worker, daemon=True, name="auto_worker").start()

    port = int(os.environ.get("PORT", 10_000))
    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=port, use_reloader=False, debug=False),
        daemon=True, name="flask",
    ).start()
    logger.info("Flask на порту %d", port)

    logger.info("Бот запущен")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=20, skip_pending=True)
        except Exception as exc:
            logger.error("polling упал: %s — перезапуск через 10с", exc)
            time.sleep(10)
