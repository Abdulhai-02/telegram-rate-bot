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

# ============== НАСТРОЙКИ ==============
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ВАШ ID для доступа к админке
MY_ADMIN_ID = 5266659205  
ADMIN_LOG_CHAT_ID = -1003264764082
MOSCOW_TZ = timezone(timedelta(hours=3))

AUTO_INTERVAL_1H = 3600
AUTO_INTERVAL_5H = 18000
AUTO_INTERVAL_24H = 86400

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
        'usdt_krw': "🇰🇷 <b>USDT → KRW</b>",
        'usdt_rub': "🇷🇺 <b>USDT → RUB (ABCEX)</b>",
        'krw_rub': "🇰🇷➡️🇷🇺 <b>KRW → RUB</b>",
        'buy': "Покупка:",
        'sell': "Продажа:",
        'updated': "⏱ Обновлено:",
        'contact': "💰 Обмен любых сумм и валют — по договоренности.\n📞 Контакт: @Abdulkhaiii",
        'auto_menu': "Выбери частоту автообновления курса:",
        'auto_curr': "Сейчас:",
        'auto_1h': "каждый 1 час",
        'auto_5h': "каждые 5 часов",
        'auto_24h': "каждые 24 часа",
        'auto_off_btn': "🔕 Выключить автообновление",
        'auto_off_msg': "🔕 Автообновление выключено.",
        'auto_on_msg': "🔔 Автообновление включено:",
        'prof_title': "👤 <b>Профиль</b>",
        'prof_reqs': "Запросов курса:",
        'prof_last': "Последний запрос:",
        'prof_join': "Дата регистрации:",
        'feedback_prompt': "Напишите ваш отзыв или предложение одним сообщением. Администратор увидит его анонимно для других, но свяжется с вами при необходимости:",
        'feedback_thanks': "✅ Спасибо! Ваш отзыв передан администратору.",
        'menu_updated': "🔄 Меню обновлено."
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
        'usdt_krw': "🇰🇷 <b>USDT → KRW</b>",
        'usdt_rub': "🇷🇺 <b>USDT → RUB (ABCEX)</b>",
        'krw_rub': "🇰🇷➡️🇷🇺 <b>KRW → RUB</b>",
        'buy': "Buy:",
        'sell': "Sell:",
        'updated': "⏱ Updated:",
        'contact': "💰 Exchange of any amounts and currencies — by agreement.\n📞 Contact: @Abdulkhaiii",
        'auto_menu': "Select the frequency for auto-updates:",
        'auto_curr': "Currently:",
        'auto_1h': "every 1 hour",
        'auto_5h': "every 5 hours",
        'auto_24h': "every 24 hours",
        'auto_off_btn': "🔕 Disable auto-updates",
        'auto_off_msg': "🔕 Auto-updates disabled.",
        'auto_on_msg': "🔔 Auto-updates enabled:",
        'prof_title': "👤 <b>Profile</b>",
        'prof_reqs': "Total requests:",
        'prof_last': "Last request:",
        'prof_join': "Joined date:",
        'feedback_prompt': "Write your feedback or suggestion in one message. The admin will receive it safely:",
        'feedback_thanks': "✅ Thank you! Your feedback has been sent to the admin.",
        'menu_updated': "🔄 Menu updated."
    }
}

USER_DATA = {}
AUTO_USERS = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============== УТИЛИТЫ ==============
def now_msk(): return datetime.now(MOSCOW_TZ)

def fmt_num(v, d=2):
    if v is None: return "—"
    return f"{v:,.{d}f}".replace(",", " ")

def init_user(user):
    if user.id not in USER_DATA:
        USER_DATA[user.id] = {
            "lang": "ru", "requests": 0, "last": None,
            "joined": now_msk(), "first_name": user.first_name, "username": user.username
        }

def log_to_channel(text):
    try: bot.send_message(ADMIN_LOG_CHAT_ID, text, parse_mode="HTML")
    except: pass

# ============== КЛАВИАТУРЫ ==============
def main_keyboard(uid):
    l = USER_DATA.get(uid, {}).get("lang", "ru")
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row(LANGS[l]['btn_show'], LANGS[l]['btn_auto'])
    m.row(LANGS[l]['btn_profile'], LANGS[l]['btn_feedback'])
    m.row(LANGS[l]['btn_disable'])
    if uid == MY_ADMIN_ID: m.row(LANGS[l]['btn_admin'])
    return m

# ============== API ==============
def fetch_all_rates():
    def get_u():
        try: return float(requests.get("https://api.upbit.com/v1/ticker?markets=KRW-USDT", timeout=4).json()[0]["trade_price"])
        except: return None
    def get_b():
        try: return float(requests.get("https://api.bithumb.com/public/ticker/USDT_KRW", timeout=4).json()["data"]["closing_price"])
        except: return None
    def get_r():
        try: return 1_000_000 / requests.get("https://open.er-api.com/v6/latest/RUB", timeout=5).json()["rates"]["KRW"]
        except: return None
    def get_ab():
        try:
            d = requests.get("https://hub.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB", timeout=4).json()
            return float(d["bid"][0]["price"]), float(d["ask"][0]["price"])
        except: return None, None

    with concurrent.futures.ThreadPoolExecutor() as ex:
        return ex.submit(get_u).result(), ex.submit(get_b).result(), ex.submit(get_r).result(), ex.submit(get_ab).result()

# ============== ОБРАБОТЧИКИ ==============
@bot.message_handler(commands=["start"])
def start_handler(m):
    init_user(m.from_user)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
           types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"))
    bot.send_message(m.chat.id, "Выберите язык / Select language:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def lang_set(c):
    l = c.data.split("_")[1]
    init_user(c.from_user)
    USER_DATA[c.from_user.id]['lang'] = l
    bot.delete_message(c.message.chat.id, c.message.message_id)
    bot.send_message(c.message.chat.id, LANGS[l]['welcome'], reply_markup=main_keyboard(c.from_user.id))

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_show'], LANGS['en']['btn_show']])
def show_rate(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    msg = bot.send_message(m.chat.id, f"{LANGS[l]['loading']}...")
    
    u, b, r, (ab_buy, ab_sell) = fetch_all_rates()
    
    if not any([u, b, r, ab_buy]):
        bot.edit_message_text(LANGS[l]['error_fetch'], m.chat.id, msg.message_id)
        return

    timestamp = now_msk().strftime("%d.%m.%Y, %H:%M")
    text = (
        f"{LANGS[l]['rate_title']}\n\n"
        f"{LANGS[l]['usdt_krw']}\n◾ UPBIT: <b>{fmt_num(u,0)} ₩</b>\n◾ BITHUMB: <b>{fmt_num(b,0)} ₩</b>\n━━━━━━━━━━━━━━\n\n"
        f"{LANGS[l]['usdt_rub']}\n◾ {LANGS[l]['buy']} <b>{fmt_num(ab_buy,2)} ₽</b>\n◾ {LANGS[l]['sell']} <b>{fmt_num(ab_sell,2)} ₽</b>\n━━━━━━━━━━━━━━\n\n"
        f"{LANGS[l]['krw_rub']}\n◾ 1 000 000 ₩ → <b>{fmt_num(r,2)} ₽</b>\n━━━━━━━━━━━━━━\n"
        f"{LANGS[l]['updated']} <b>{timestamp}</b>\n\n{LANGS[l]['contact']}"
    )
    bot.edit_message_text(text, m.chat.id, msg.message_id, parse_mode="HTML")
    
    # ЛОГ В КАНАЛ С РЕЗУЛЬТАТАМИ
    log_to_channel(
        f"📊 <b>Запрос курса</b>\n"
        f"👤 @{m.from_user.username or 'N/A'} | ID: <code>{m.from_user.id}</code>\n"
        f"👤 Имя: {m.from_user.first_name}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🇰🇷 KRW: {fmt_num(u,0)} / {fmt_num(b,0)}\n"
        f"🇷🇺 RUB: {fmt_num(ab_buy,2)} / {fmt_num(ab_sell,2)}\n"
        f"🕒 {now_msk().strftime('%H:%M:%S')}"
    )
    USER_DATA[m.from_user.id]["requests"] += 1
    USER_DATA[m.from_user.id]["last"] = now_msk()

# --- ОТЗЫВЫ ---
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_feedback'], LANGS['en']['btn_feedback']])
def feedback_start(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    msg = bot.send_message(m.chat.id, LANGS[l]['feedback_prompt'], reply_markup=types.ForceReply())
    bot.register_next_step_handler(msg, feedback_save)

def feedback_save(m):
    l = USER_DATA[m.from_user.id]['lang']
    if m.text:
        # Лог отзыва для админа (с данными пользователя)
        log_to_channel(
            f"🔴 <b>НОВЫЙ ОТЗЫВ</b>\n"
            f"👤 @{m.from_user.username or 'N/A'} | ID: <code>{m.from_user.id}</code>\n"
            f"👤 Имя: {m.from_user.first_name}\n"
            f"━━━━━━━━━━━━━━\n"
            f"💬 Текст: <i>{m.text}</i>"
        )
        bot.send_message(m.chat.id, LANGS[l]['feedback_thanks'], reply_markup=main_keyboard(m.from_user.id))

# ============== ОСТАЛЬНЫЕ ФУНКЦИИ (АВТО, ПРОФИЛЬ, АДМИНКА) ==============
# (Код аналогичен прошлому, но оптимизирован под новые логи)

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_auto'], LANGS['en']['btn_auto']])
def toggle_auto(m):
    init_user(m.from_user); l = USER_DATA[m.from_user.id]['lang']
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("1H", callback_data="auto_3600"), types.InlineKeyboardButton("5H", callback_data="auto_18000"), types.InlineKeyboardButton("24H", callback_data="auto_86400"))
    if m.chat.id in AUTO_USERS: kb.row(types.InlineKeyboardButton(LANGS[l]['auto_off_btn'], callback_data="auto_0"))
    bot.send_message(m.chat.id, LANGS[l]['auto_menu'], reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("auto_"))
def auto_callback(c):
    l = USER_DATA[c.from_user.id]['lang']; val = int(c.data.split("_")[1])
    if val == 0:
        AUTO_USERS.pop(c.message.chat.id, None)
        bot.edit_message_text(LANGS[l]['auto_off_msg'], c.message.chat.id, c.message.message_id)
    else:
        AUTO_USERS[c.message.chat.id] = {"interval": val, "last": now_msk()}
        bot.edit_message_text(f"{LANGS[l]['auto_on_msg']} {val//3600}H.", c.message.chat.id, c.message.message_id)

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_profile'], LANGS['en']['btn_profile']])
def profile(m):
    init_user(m.from_user); l = USER_DATA[m.from_user.id]['lang']; d = USER_DATA[m.from_user.id]
    txt = f"{LANGS[l]['prof_title']}\n\nID: <code>{m.from_user.id}</code>\n{LANGS[l]['prof_join']} {d['joined'].strftime('%d.%m.%Y')}\n{LANGS[l]['prof_reqs']} {d['requests']}"
    bot.send_message(m.chat.id, txt)

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_admin'], LANGS['en']['btn_admin']] and m.from_user.id == MY_ADMIN_ID)
def admin_panel(m):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📊 Статистика", callback_data="adm_stat"), types.InlineKeyboardButton("📢 Рассылка всем", callback_data="adm_bc"))
    bot.send_message(m.chat.id, "🛠 <b>Админ-панель</b>", reply_markup=kb)

# ============== ЗАПУСК ==============
app = Flask(__name__)
@app.route('/')
def home(): return "OK", 200

def auto_worker():
    while True:
        time.sleep(60)
        now = now_msk()
        for cid, cfg in list(AUTO_USERS.items()):
            if (now - cfg["last"]).total_seconds() >= cfg["interval"]:
                try:
                    u, b, r, (ab_b, ab_s) = fetch_all_rates()
                    l = USER_DATA.get(cid, {}).get("lang", "ru")
                    bot.send_message(cid, f"🔔 <b>AUTO:</b> {fmt_num(u,0)} ₩ | {fmt_num(ab_b,2)} ₽")
                    AUTO_USERS[cid]["last"] = now
                except: pass

if __name__ == "__main__":
    threading.Thread(target=auto_worker, daemon=True).start()
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    while True:
        try: bot.infinity_polling(skip_pending=True)
        except: time.sleep(5)
