# -*- coding: utf-8 -*-
import os
import logging
import threading
import time
import concurrent.futures
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests
from dotenv import load_dotenv
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from flask import Flask

# === ИМПОРТ MONGODB ===
from pymongo import MongoClient
import certifi

# ============== НАСТРОЙКИ ==============
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN в переменных окружения")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

MY_ADMIN_ID = 5266659205
ADMIN_LOG_CHAT_ID = -1003264764082
MOSCOW_TZ = timezone(timedelta(hours=3))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ ===
MONGO_URI = os.getenv("MONGO_URI")
if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=5000)
        mongo_client.server_info()
        db = mongo_client.p2p_bot_db
        users_collection = db.users
        auto_collection = db.auto_updates
        logger.info("✅ УСПЕХ: Связь с MongoDB Atlas установлена!")
    except Exception as e:
        logger.error(f"❌ ОШИБКА ПОДКЛЮЧЕНИЯ К MONGODB: {e}")
        users_collection = None
        auto_collection = None
else:
    users_collection = None
    auto_collection = None
    logger.warning("⚠️ MONGO_URI не найден.")

# ============== ЛОКАЛИЗАЦИЯ ==============
LANGS = {
    'ru': {
        'btn_show': "📊 Показать курс",
        'btn_auto': "🔔 Автообновление",
        'btn_profile': "👤 Профиль",
        'btn_feedback': "✍️ Отзыв",
        'btn_disable': "🚫 Отключить уведомления",
        'btn_admin': "🛠 Админка",
        'welcome': "👋 Привет!\n\nВыбери нужный раздел ниже 👇",
        'loading': "⏳ Загрузка курса",
        'error_fetch': "⚠️ Не удалось получить курс.\nПопробуйте позже.",
        'rate_title': "💱 <b>АКТУАЛЬНЫЕ КУРСЫ</b>",
        'updated': "⏱ Обновлено:",
        'contact': "💰 Обмен любых сумм и валют — по договоренности.\n📞 Контакт: @Abdulkhaiii",
        'auto_menu': "Выбери частоту автообновления:",
        'auto_off_msg': "Автообновление выключено.",
        'auto_on_msg': "Включено уведомление каждые:",
        'feedback_prompt': "Напишите ваш отзыв одним сообщением:",
        'feedback_thanks': "✅ Спасибо! Ваш отзыв передан администратору.",
        'menu_updated': "🔄 Меню обновлено.",
        'prof_title': "👤 <b>Ваш Профиль</b>",
        'prof_reqs': "Всего запросов курса:",
        'prof_last': "Последний запрос:",
        'prof_join': "В боте с:",
        'buy': "Покупка",
        'sell': "Продажа",
    },
    'en': {
        'btn_show': "📊 Show Rates",
        'btn_auto': "🔔 Auto-updates",
        'btn_profile': "👤 Profile",
        'btn_feedback': "✍️ Feedback",
        'btn_disable': "🚫 Disable Alerts",
        'btn_admin': "🛠 Admin Panel",
        'welcome': "👋 Hello!\n\nSelect a section below 👇",
        'loading': "⏳ Fetching rates",
        'error_fetch': "⚠️ Failed to get rates.\nPlease try again later.",
        'rate_title': "💱 <b>CURRENT RATES</b>",
        'updated': "⏱ Updated:",
        'contact': "💰 Exchange of any amounts — by agreement.\n📞 Contact: @Abdulkhaiii",
        'auto_menu': "Select update frequency:",
        'auto_off_msg': "Auto-updates disabled.",
        'auto_on_msg': "Alerts enabled every:",
        'feedback_prompt': "Write your feedback in one message:",
        'feedback_thanks': "✅ Thank you! Feedback sent to admin.",
        'menu_updated': "🔄 Menu updated.",
        'prof_title': "👤 <b>Your Profile</b>",
        'prof_reqs': "Total requests:",
        'prof_last': "Last request:",
        'prof_join': "Member since:",
        'buy': "Buy",
        'sell': "Sell",
    }
}

USER_DATA = {}
AUTO_USERS = {}
ALL_USER_IDS = set()

# ============== СИНХРОНИЗАЦИЯ С БАЗОЙ ==============
def load_db():
    if users_collection is None:
        return
    try:
        logger.info("⏳ Загрузка профилей из MongoDB...")
        for doc in users_collection.find():
            uid = doc["_id"]
            ALL_USER_IDS.add(uid)
            USER_DATA[uid] = {
                "lang": doc.get("lang", "ru"),
                "requests": doc.get("requests", 0),
                "last": datetime.fromisoformat(doc["last"]) if doc.get("last") else None,
                "joined": datetime.fromisoformat(doc["joined"]) if doc.get("joined") else now_msk(),
                "first_name": doc.get("first_name"),
                "username": doc.get("username")
            }
        for doc in auto_collection.find():
            AUTO_USERS[doc["_id"]] = {
                "interval": doc["interval"],
                "last": datetime.fromisoformat(doc["last"]) if doc.get("last") else now_msk(),
                "enabled_at": datetime.fromisoformat(doc["enabled_at"]) if doc.get("enabled_at") else now_msk()
            }
        logger.info(f"✅ База загружена! Юзеров: {len(USER_DATA)}, Автообновлений: {len(AUTO_USERS)}")
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки БД: {e}")

def save_user(uid):
    if users_collection is None or uid not in USER_DATA:
        return
    try:
        u = USER_DATA[uid]
        doc = {
            "lang": u["lang"],
            "requests": u["requests"],
            "last": u["last"].isoformat() if u["last"] else None,
            "joined": u["joined"].isoformat() if u["joined"] else None,
            "first_name": u["first_name"],
            "username": u["username"]
        }
        users_collection.update_one({"_id": uid}, {"$set": doc}, upsert=True)
    except Exception as e:
        logger.error(f"Ошибка сохранения пользователя {uid}: {e}")

def save_auto(uid):
    """
    Сохраняет или удаляет запись об автообновлении пользователя в MongoDB.
    Вызывается при любом изменении: пользователем, администратором или системой.
    """
    if auto_collection is None:
        return
    try:
        if uid in AUTO_USERS:
            a = AUTO_USERS[uid]
            doc = {
                "interval": a["interval"],
                "last": a["last"].isoformat() if a.get("last") else None,
                "enabled_at": a["enabled_at"].isoformat() if a.get("enabled_at") else now_msk().isoformat()
            }
            auto_collection.update_one({"_id": uid}, {"$set": doc}, upsert=True)
        else:
            auto_collection.delete_one({"_id": uid})
    except Exception as e:
        logger.error(f"Ошибка сохранения автообновления для {uid}: {e}")

# ============== УТИЛИТЫ ==============
def now_msk():
    return datetime.now(MOSCOW_TZ)

def fmt_num(v, d=2):
    if v is None:
        return "—"
    return f"{v:,.{d}f}".replace(",", " ")

def init_user(user):
    uid = user.id
    ALL_USER_IDS.add(uid)
    if uid not in USER_DATA:
        if users_collection is not None:
            try:
                doc = users_collection.find_one({"_id": uid})
                if doc:
                    USER_DATA[uid] = {
                        "lang": doc.get("lang", "ru"),
                        "requests": doc.get("requests", 0),
                        "last": datetime.fromisoformat(doc["last"]) if doc.get("last") else None,
                        "joined": datetime.fromisoformat(doc["joined"]) if doc.get("joined") else now_msk(),
                        "first_name": doc.get("first_name"),
                        "username": doc.get("username")
                    }
                    logger.info(f"⬇️ Профиль {uid} восстановлен из MongoDB.")
                    return
            except Exception as e:
                logger.error(f"Ошибка проверки {uid} в MongoDB: {e}")
        USER_DATA[uid] = {
            "lang": "ru",
            "requests": 0,
            "last": None,
            "joined": now_msk(),
            "first_name": user.first_name,
            "username": user.username
        }
        save_user(uid)
        logger.info(f"🆕 Новый профиль для {uid}")

def log_action(user, action, result=None):
    try:
        name = user.first_name
        if user.last_name:
            name += f" {user.last_name}"
        nick = f"@{user.username}" if user.username else "Нет ника"
        time_str = now_msk().strftime('%H:%M:%S')
        log_text = (
            f"⚙️ <b>Лог действия</b>\n"
            f"👤 Имя: {name}\n"
            f"🔗 Ник / ID: {nick} | <code>{user.id}</code>\n"
            f"🔘 Действие: <b>{action}</b>\n"
        )
        if result:
            log_text += f"📊 Результат: {result}\n"
        log_text += f"🕒 Время: {time_str} МСК"
        bot.send_message(ADMIN_LOG_CHAT_ID, log_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка отправки лога: {e}")

# ============== UI ==============
def main_keyboard(uid):
    l = USER_DATA.get(uid, {}).get("lang", "ru")
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row(LANGS[l]['btn_show'], LANGS[l]['btn_auto'])
    m.row(LANGS[l]['btn_profile'], LANGS[l]['btn_feedback'])
    m.row(LANGS[l]['btn_disable'])
    if uid == MY_ADMIN_ID:
        m.row(LANGS[l]['btn_admin'])
    return m

# ============== API ==============
def fetch_all_rates():
    headers = {"User-Agent": "Mozilla/5.0"}

    def g_u():
        try:
            return float(requests.get("https://api.upbit.com/v1/ticker?markets=KRW-USDT", headers=headers, timeout=5).json()[0]["trade_price"])
        except:
            return None

    def g_b():
        try:
            return float(requests.get("https://api.bithumb.com/public/ticker/USDT_KRW", headers=headers, timeout=5).json()["data"]["closing_price"])
        except:
            return None

    def g_r():
        try:
            return 1_000_000 / requests.get("https://open.er-api.com/v6/latest/RUB", timeout=5).json()["rates"]["KRW"]
        except:
            return None

    def g_ab():
        try:
            d = requests.get("https://hub.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB", headers=headers, timeout=5).json()
            return float(d["bid"][0]["price"]), float(d["ask"][0]["price"])
        except:
            return None, None

    with concurrent.futures.ThreadPoolExecutor() as ex:
        fu = ex.submit(g_u)
        fb = ex.submit(g_b)
        fr = ex.submit(g_r)
        fab = ex.submit(g_ab)
        return fu.result(), fb.result(), fr.result(), fab.result()

def build_rate_message(u, b, r, ab_buy, ab_sell, lang):
    """Формирует красивое сообщение с курсами на нужном языке."""
    l = LANGS[lang]
    timestamp = now_msk().strftime("%d.%m.%Y, %H:%M:%S")
    text = (
        f"{l['rate_title']}\n\n"
        f"🇰🇷 <b>USDT → KRW</b>\n"
        f"  ◾ UPBIT: <b>{fmt_num(u, 0)} ₩</b>\n"
        f"  ◾ BITHUMB: <b>{fmt_num(b, 0)} ₩</b>\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"🇷🇺 <b>USDT → RUB (ABCEX)</b>\n"
        f"  ◾ {l['buy']}: <b>{fmt_num(ab_buy, 2)} ₽</b>\n"
        f"  ◾ {l['sell']}: <b>{fmt_num(ab_sell, 2)} ₽</b>\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"🇰🇷➡️🇷🇺 <b>KRW → RUB</b>\n"
        f"  ◾ 1 000 000 ₩ → <b>{fmt_num(r, 2)} ₽</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"⏱ {l['updated']} <b>{timestamp}</b>\n\n"
        f"{l['contact']}"
    )
    return text

# ============== ОБРАБОТЧИКИ ==============
@bot.message_handler(commands=["start"])
def start_handler(m):
    init_user(m.from_user)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
        types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
    )
    bot.send_message(m.chat.id, "🇷🇺 Выберите язык / 🇬🇧 Select language:", reply_markup=kb)
    log_action(m.from_user, "Команда /start")

@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def lang_set(c):
    l = c.data.split("_")[1]
    init_user(c.from_user)
    USER_DATA[c.from_user.id]['lang'] = l
    save_user(c.from_user.id)
    bot.delete_message(c.message.chat.id, c.message.message_id)
    bot.send_message(c.message.chat.id, LANGS[l]['welcome'], reply_markup=main_keyboard(c.from_user.id))
    log_action(c.from_user, "Выбор языка", result=f"{'Русский' if l == 'ru' else 'English'}")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_show'], LANGS['en']['btn_show']])
def show_rate(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    # FIX: всегда передаём reply_markup чтобы кнопки не пропадали
    msg = bot.send_message(m.chat.id, f"{LANGS[l]['loading']}...", reply_markup=main_keyboard(m.from_user.id))

    u, b, r, (ab_buy, ab_sell) = fetch_all_rates()

    if not any([u, b, r, ab_buy]):
        bot.edit_message_text(LANGS[l]['error_fetch'], m.chat.id, msg.message_id)
        log_action(m.from_user, "Показать курс", result="⚠️ Ошибка API")
        return

    text = build_rate_message(u, b, r, ab_buy, ab_sell, l)
    bot.edit_message_text(text, m.chat.id, msg.message_id, parse_mode="HTML")

    log_action(m.from_user, "Показать курс", result=f"Upbit {fmt_num(u, 0)} ₩ | ABCEX {fmt_num(ab_buy, 2)} ₽")
    USER_DATA[m.from_user.id]["requests"] += 1
    USER_DATA[m.from_user.id]["last"] = now_msk()
    save_user(m.from_user.id)

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_profile'], LANGS['en']['btn_profile']])
def show_profile(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    d = USER_DATA[m.from_user.id]

    last = d["last"].strftime("%d.%m.%Y %H:%M:%S") if d["last"] else "—"
    join = d["joined"].strftime("%d.%m.%Y %H:%M:%S") if d["joined"] else "—"
    nick = f"@{d['username']}" if d['username'] else d['first_name']

    txt = (
        f"{LANGS[l]['prof_title']}\n\n"
        f"<b>ID:</b> <code>{m.from_user.id}</code>\n"
        f"<b>Ник:</b> {nick}\n"
        f"<b>{LANGS[l]['prof_join']}</b> {join}\n"
        f"━━━━━━━━━━━━━━\n"
        f"<b>{LANGS[l]['prof_reqs']}</b> {d['requests']}\n"
        f"<b>{LANGS[l]['prof_last']}</b> {last}"
    )
    # FIX: всегда передаём reply_markup
    bot.send_message(m.chat.id, txt, parse_mode="HTML", reply_markup=main_keyboard(m.from_user.id))
    log_action(m.from_user, "Профиль")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_feedback'], LANGS['en']['btn_feedback']])
def feedback_start(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    # FIX: сначала восстанавливаем клавиатуру, потом просим отзыв
    bot.send_message(m.chat.id, "...", reply_markup=main_keyboard(m.from_user.id))
    msg = bot.send_message(m.chat.id, LANGS[l]['feedback_prompt'], reply_markup=types.ForceReply())
    bot.register_next_step_handler(msg, feedback_save)
    log_action(m.from_user, "Нажал 'Отзыв'")

def feedback_save(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    if m.text:
        name = m.from_user.first_name
        if m.from_user.last_name:
            name += f" {m.from_user.last_name}"
        nick = f"@{m.from_user.username}" if m.from_user.username else "Нет"
        log_text = (
            f"🔴 <b>НОВЫЙ ОТЗЫВ</b>\n"
            f"👤 Имя: {name}\n"
            f"🔗 Ник / ID: {nick} | <code>{m.from_user.id}</code>\n"
            f"💬 Текст: <i>{m.text}</i>\n"
            f"🕒 Время: {now_msk().strftime('%H:%M:%S')} МСК"
        )
        bot.send_message(ADMIN_LOG_CHAT_ID, log_text, parse_mode="HTML")
    # FIX: всегда возвращаем клавиатуру после отзыва
    bot.send_message(m.chat.id, LANGS[l]['feedback_thanks'], reply_markup=main_keyboard(m.from_user.id))

# ============== АВТООБНОВЛЕНИЕ (ПОЛЬЗОВАТЕЛЬ) ==============
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_auto'], LANGS['en']['btn_auto']])
def toggle_auto(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("1H", callback_data="auto_3600"),
        types.InlineKeyboardButton("5H", callback_data="auto_18000"),
        types.InlineKeyboardButton("24H", callback_data="auto_86400")
    )
    if m.chat.id in AUTO_USERS:
        kb.row(types.InlineKeyboardButton("🚫 OFF", callback_data="auto_0"))
    # FIX: возвращаем клавиатуру перед inline-меню
    bot.send_message(m.chat.id, LANGS[l]['auto_menu'], reply_markup=main_keyboard(m.from_user.id))
    bot.send_message(m.chat.id, "⬇️", reply_markup=kb)
    log_action(m.from_user, "Открыл меню Автообновления")

@bot.callback_query_handler(func=lambda c: c.data.startswith("auto_"))
def auto_callback(c):
    init_user(c.from_user)
    l = USER_DATA[c.from_user.id]['lang']
    val = int(c.data.split("_")[1])

    if val == 0:
        AUTO_USERS.pop(c.message.chat.id, None)
        save_auto(c.message.chat.id)  # FIX: сохраняем удаление в БД
        bot.edit_message_text(
            f"✅ <b>Успешно!</b> {LANGS[l]['auto_off_msg']}",
            c.message.chat.id, c.message.message_id, parse_mode="HTML"
        )
        log_action(c.from_user, "Автообновление", result="Отключено пользователем")
    else:
        now = now_msk()
        AUTO_USERS[c.message.chat.id] = {
            "interval": val,
            "last": now,
            "enabled_at": now  # FIX: фиксируем время включения
        }
        save_auto(c.message.chat.id)  # FIX: сохраняем в БД сразу
        bot.edit_message_text(
            f"✅ <b>Успешно!</b> {LANGS[l]['auto_on_msg']} {val // 3600}H.",
            c.message.chat.id, c.message.message_id, parse_mode="HTML"
        )
        log_action(c.from_user, "Автообновление", result=f"Включено ({val // 3600}H)")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_disable'], LANGS['en']['btn_disable']])
def disable_notif(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    AUTO_USERS.pop(m.chat.id, None)
    save_auto(m.chat.id)  # FIX: сохраняем удаление
    # FIX: всегда возвращаем клавиатуру
    bot.send_message(
        m.chat.id,
        f"✅ <b>Успешно!</b> 🔕 {LANGS[l]['auto_off_msg']}",
        parse_mode="HTML",
        reply_markup=main_keyboard(m.from_user.id)
    )
    log_action(m.from_user, "Отключил уведомления")

# ============== АДМИНКА ==============
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_admin'], LANGS['en']['btn_admin']] and m.from_user.id == MY_ADMIN_ID)
def admin_panel(m):
    init_user(m.from_user)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="adm_stat"),
        types.InlineKeyboardButton("👥 База пользователей", callback_data="adm_users_0"),
        types.InlineKeyboardButton("📢 Рассылка всем", callback_data="adm_bc"),
        types.InlineKeyboardButton("🔔 Упр. подписками (Тихо)", callback_data="adm_auto_menu")
    )
    bot.send_message(m.chat.id, "🛠 <b>Админ-панель</b>", reply_markup=kb)
    log_action(m.from_user, "Открыл Админ-панель")

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def admin_cb(c):
    if c.from_user.id != MY_ADMIN_ID:
        return

    action = c.data[4:]

    if action == "stat":
        reqs = sum(u['requests'] for u in USER_DATA.values())
        txt = (
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Юзеров в базе: <b>{len(ALL_USER_IDS)}</b>\n"
            f"🔔 Активных подписок: <b>{len(AUTO_USERS)}</b>\n"
            f"📈 Всего запросов: <b>{reqs}</b>"
        )
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, txt)

    elif action.startswith("users_"):
        # Список пользователей с кнопкой «Подробнее» напротив каждого
        page = int(action.split("_")[1])
        page_size = 10
        all_ids = list(ALL_USER_IDS)
        total = len(all_ids)
        start = page * page_size
        end = min(start + page_size, total)
        chunk = all_ids[start:end]

        txt = f"👥 <b>Пользователи ({start + 1}–{end} из {total}):</b>\n\n"
        kb = types.InlineKeyboardMarkup(row_width=2)

        for uid in chunk:
            d = USER_DATA.get(uid, {})
            nick = f"@{d.get('username')}" if d.get('username') else (d.get('first_name') or str(uid))
            auto_icon = "🟢" if uid in AUTO_USERS else "🔴"
            txt += f"{auto_icon} <code>{uid}</code> | {nick}\n"
            kb.add(types.InlineKeyboardButton(f"🔍 {nick}", callback_data=f"adm_detail_{uid}"))

        # Навигация
        nav_buttons = []
        if page > 0:
            nav_buttons.append(types.InlineKeyboardButton("◀️ Назад", callback_data=f"adm_users_{page - 1}"))
        if end < total:
            nav_buttons.append(types.InlineKeyboardButton("▶️ Далее", callback_data=f"adm_users_{page + 1}"))
        if nav_buttons:
            kb.row(*nav_buttons)

        bot.answer_callback_query(c.id)
        try:
            bot.edit_message_text(txt, c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, txt, parse_mode="HTML", reply_markup=kb)

    elif action.startswith("detail_"):
        # Детальная карточка пользователя
        uid = int(action.split("_")[1])
        d = USER_DATA.get(uid, {})
        a = AUTO_USERS.get(uid)

        nick = f"@{d.get('username')}" if d.get('username') else (d.get('first_name') or "—")
        join_str = d["joined"].strftime("%d.%m.%Y %H:%M:%S") if d.get("joined") else "—"
        last_str = d["last"].strftime("%d.%m.%Y %H:%M:%S") if d.get("last") else "—"

        if a:
            hours = a["interval"] // 3600
            enabled_str = a["enabled_at"].strftime("%d.%m.%Y %H:%M:%S") if a.get("enabled_at") else "—"
            last_send_str = a["last"].strftime("%d.%m.%Y %H:%M:%S") if a.get("last") else "—"
            auto_block = (
                f"\n🟢 <b>Автообновление: ВКЛЮЧЕНО</b>\n"
                f"  ⏱ Интервал: каждые <b>{hours}H</b>\n"
                f"  📅 Включено: {enabled_str}\n"
                f"  📤 Последняя отправка: {last_send_str}"
            )
        else:
            auto_block = "\n🔴 <b>Автообновление: ВЫКЛЮЧЕНО</b>"

        txt = (
            f"🔍 <b>Карточка пользователя</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"<b>ID:</b> <code>{uid}</code>\n"
            f"<b>Ник:</b> {nick}\n"
            f"<b>Язык:</b> {'🇷🇺 RU' if d.get('lang') == 'ru' else '🇬🇧 EN'}\n"
            f"<b>В боте с:</b> {join_str}\n"
            f"━━━━━━━━━━━━━━\n"
            f"<b>Всего запросов:</b> {d.get('requests', 0)}\n"
            f"<b>Последний запрос:</b> {last_str}"
            f"{auto_block}"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("◀️ Назад к списку", callback_data="adm_users_0"))

        bot.answer_callback_query(c.id)
        try:
            bot.edit_message_text(txt, c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, txt, parse_mode="HTML", reply_markup=kb)

    elif action == "auto_menu":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Включить всем", callback_data="adm_auto_all"))
        kb.add(types.InlineKeyboardButton("👤 Подключить по ID", callback_data="adm_auto_id"))
        bot.edit_message_text(
            "⚙️ <b>Управление подписками (Тихий режим):</b>\n\nПользователи НЕ получат уведомлений о подключении.",
            c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb
        )

    elif action == "auto_all":
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("1H", callback_data="adm_all_3600"),
            types.InlineKeyboardButton("5H", callback_data="adm_all_18000"),
            types.InlineKeyboardButton("24H", callback_data="adm_all_86400")
        )
        bot.edit_message_text(
            "⏳ Выберите интервал для <b>ВСЕХ</b> пользователей:",
            c.message.chat.id, c.message.message_id, parse_mode="HTML", reply_markup=kb
        )

    elif action.startswith("all_"):
        val = int(action.split("_")[1])
        hours = val // 3600
        count = 0
        now = now_msk()
        for uid in ALL_USER_IDS:
            try:
                AUTO_USERS[uid] = {
                    "interval": val,
                    "last": now,
                    "enabled_at": now  # FIX: фиксируем время тихого включения
                }
                save_auto(uid)  # FIX: сохраняем каждую подписку в БД
                count += 1
            except Exception as e:
                logger.error(f"Ошибка тихой подписки для {uid}: {e}")
        bot.edit_message_text(
            f"✅ <b>Успешно!</b> Тихая подписка ({hours}ч.) активирована для {count} юзеров.",
            c.message.chat.id, c.message.message_id, parse_mode="HTML"
        )
        log_action(c.from_user, f"Массовая подписка ({hours}ч)", result=f"Успешно для {count} чел.")

    elif action == "auto_id":
        bot.answer_callback_query(c.id)
        msg = bot.send_message(c.message.chat.id, "Введите ID пользователя для тихой подписки:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, adm_auto_step_id)

    elif action == "bc":
        bot.answer_callback_query(c.id)
        msg = bot.send_message(c.message.chat.id, "Введите текст рассылки:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, do_bc)

def adm_auto_step_id(m):
    try:
        target_id = int(m.text.strip())
        msg = bot.send_message(m.chat.id, f"Введите интервал в часах (1, 5 или 24) для {target_id}:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, lambda s: adm_auto_final(s, target_id))
    except ValueError:
        bot.send_message(m.chat.id, "❌ Некорректный ID.", reply_markup=main_keyboard(m.from_user.id))

def adm_auto_final(m, tid):
    try:
        hours = int(m.text.strip())
        interval_seconds = hours * 3600
        now = now_msk()
        AUTO_USERS[tid] = {
            "interval": interval_seconds,
            "last": now,
            "enabled_at": now  # FIX: фиксируем время включения администратором
        }
        save_auto(tid)  # FIX: сохраняем в БД
        bot.send_message(
            m.chat.id,
            f"✅ <b>Успешно!</b> Для ID <code>{tid}</code> ТИХО включено автообновление каждые {hours}ч.",
            parse_mode="HTML", reply_markup=main_keyboard(m.from_user.id)
        )
        log_action(m.from_user, f"Тихая подписка по ID ({tid})", result=f"Интервал {hours}ч")
    except ValueError:
        bot.send_message(m.chat.id, "❌ Интервал должен быть числом.", reply_markup=main_keyboard(m.from_user.id))

def do_bc(m):
    count = 0
    for uid in ALL_USER_IDS:
        try:
            bot.send_message(uid, f"📢 <b>УВЕДОМЛЕНИЕ:</b>\n\n{m.text}", parse_mode="HTML")
            count += 1
        except Exception:
            pass
    bot.send_message(m.chat.id, f"✅ Рассылка отправлена {count} чел.", reply_markup=main_keyboard(m.from_user.id))
    log_action(m.from_user, "Рассылка всем", result=f"Доставлено: {count}")

# ============== ЛЮБОЕ ДРУГОЕ СООБЩЕНИЕ ==============
@bot.message_handler(func=lambda m: True)
def any_msg(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    # FIX: всегда возвращаем клавиатуру
    bot.send_message(m.chat.id, LANGS[l]['menu_updated'], reply_markup=main_keyboard(m.from_user.id))

# ============== ФОНОВЫЙ ВОРКЕР АВТООБНОВЛЕНИЯ ==============
def auto_worker():
    """Фоновый поток: отправляет курсы подписчикам по расписанию."""
    while True:
        time.sleep(60)
        now = now_msk()
        for cid, cfg in list(AUTO_USERS.items()):
            try:
                last = cfg.get("last")
                if last and (now - last).total_seconds() >= cfg["interval"]:
                    u, b, r, (ab_b, ab_s) = fetch_all_rates()
                    l = USER_DATA.get(cid, {}).get("lang", "ru")
                    text = (
                        f"🔔 <b>{'АВТО-КУРС' if l == 'ru' else 'AUTO-RATE'}</b>\n\n"
                        f"🇰🇷 Upbit: <b>{fmt_num(u, 0)} ₩</b>\n"
                        f"🇷🇺 ABCEX: <b>{fmt_num(ab_b, 2)} ₽</b>\n"
                        f"🔄 1М ₩ ≈ <b>{fmt_num(r, 2)} ₽</b>\n"
                        f"⏱ {now.strftime('%H:%M:%S')} МСК"
                    )
                    bot.send_message(cid, text, parse_mode="HTML")
                    # FIX: обновляем время и сохраняем в БД после каждой отправки
                    AUTO_USERS[cid]["last"] = now
                    save_auto(cid)
            except Exception as e:
                err_str = str(e).lower()
                if "blocked" in err_str or "not found" in err_str or "deactivated" in err_str:
                    logger.warning(f"Пользователь {cid} заблокировал бота. Удаляем из авто.")
                    AUTO_USERS.pop(cid, None)
                    save_auto(cid)
                else:
                    logger.error(f"Ошибка авто-рассылки для {cid}: {e}")

# ============== FLASK + ЗАПУСК ==============
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот работает стабильно!", 200

if __name__ == "__main__":
    try:
        bot.remove_webhook()
    except Exception:
        pass

    load_db()

    threading.Thread(target=auto_worker, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True).start()

    logger.info("🤖 Бот запущен и готов к работе...")

    while True:
        try:
            bot.infinity_polling(timeout=60)
        except Exception as e:
            logger.error(f"Критическая ошибка Telegram API: {e}")
            time.sleep(10)
