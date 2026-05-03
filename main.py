# -*- coding: utf-8 -*-
import os
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

# ============== CONFIGURATION ==============
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ВАШ ID (Обязательно проверьте его корректность)
MY_ADMIN_ID = 5143360493  
ADMIN_LOG_CHAT_ID = -1003264764082
MOSCOW_TZ = timezone(timedelta(hours=3))

# Словари локализации
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
        'auto_menu': "Выбери частоту автообновления:",
        'auto_off_msg': "🔕 Автообновление выключено.",
        'auto_on_msg': "🔔 Включено уведомление:",
        'feedback_prompt': "Напишите ваш отзыв одним сообщением:",
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
        'contact': "💰 Exchange of any amounts — by agreement.\n📞 Contact: @Abdulkhaiii",
        'feedback_prompt': "Write your feedback in one message:",
        'feedback_thanks': "✅ Thank you! Feedback sent to admin.",
        'auto_menu': "Select update frequency:",
        'auto_off_msg': "🔕 Auto-updates disabled.",
        'auto_on_msg': "🔔 Alerts enabled:",
        'menu_updated': "🔄 Menu updated."
    }
}

# База данных (В памяти - очищается при перезагрузке)
USER_DATA = {}
AUTO_USERS = {}
ALL_USER_IDS = set() 

# ============== UTILS ==============
def now_msk(): return datetime.now(MOSCOW_TZ)

def fmt_num(v, d=2):
    if v is None: return "—"
    return f"{v:,.{d}f}".replace(",", " ")

def init_user(user):
    """Сбор данных о пользователе и его ID"""
    uid = user.id
    ALL_USER_IDS.add(uid)
    if uid not in USER_DATA:
        USER_DATA[uid] = {
            "lang": "ru", "requests": 0, "last": None,
            "joined": now_msk(), "name": user.first_name, "nick": user.username
        }

def log_to_channel(text):
    try: bot.send_message(ADMIN_LOG_CHAT_ID, text, parse_mode="HTML")
    except: pass

# ============== UI ==============
def main_keyboard(uid):
    l = USER_DATA.get(uid, {}).get("lang", "ru")
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row(LANGS[l]['btn_show'], LANGS[l]['btn_auto'])
    m.row(LANGS[l]['btn_profile'], LANGS[l]['btn_feedback'])
    m.row(LANGS[l]['btn_disable'])
    if uid == MY_ADMIN_ID: m.row(LANGS[l]['btn_admin'])
    return m

# ============== API ==============
def fetch_rates():
    def g_u():
        try: return float(requests.get("https://api.upbit.com/v1/ticker?markets=KRW-USDT", timeout=4).json()[0]["trade_price"])
        except: return None
    def g_b():
        try: return float(requests.get("https://api.bithumb.com/public/ticker/USDT_KRW", timeout=4).json()["data"]["closing_price"])
        except: return None
    def g_r():
        try: return 1_000_000 / requests.get("https://open.er-api.com/v6/latest/RUB", timeout=5).json()["rates"]["KRW"]
        except: return None
    def g_ab():
        try:
            d = requests.get("https://hub.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB", timeout=4).json()
            return float(d["bid"][0]["price"]), float(d["ask"][0]["price"])
        except: return None, None

    with concurrent.futures.ThreadPoolExecutor() as ex:
        return ex.submit(g_u).result(), ex.submit(g_b).result(), ex.submit(g_r).result(), ex.submit(g_ab).result()

# ============== HANDLERS ==============
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
    
    u, b, r, (ab_buy, ab_sell) = fetch_rates()
    
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
    
    # ОТЧЕТ В ЛОГИ
    log_to_channel(
        f"📊 <b>Запрос курса</b>\n"
        f"👤 @{m.from_user.username or 'N/A'} (ID: <code>{m.from_user.id}</code>)\n"
        f"📈 Upbit: {fmt_num(u,0)} | ABCEX Buy: {fmt_num(ab_buy,2)}\n"
        f"🕒 {now_msk().strftime('%H:%M:%S')}"
    )
    USER_DATA[m.from_user.id]["requests"] += 1
    USER_DATA[m.from_user.id]["last"] = now_msk()

# --- ОТЗЫВ ---
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_feedback'], LANGS['en']['btn_feedback']])
def feedback_start(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    msg = bot.send_message(m.chat.id, LANGS[l]['feedback_prompt'], reply_markup=types.ForceReply())
    bot.register_next_step_handler(msg, feedback_save)

def feedback_save(m):
    l = USER_DATA[m.from_user.id]['lang']
    if m.text:
        log_to_channel(
            f"🔴 <b>ОТЗЫВ</b>\n"
            f"👤 @{m.from_user.username or 'N/A'} (ID: <code>{m.from_user.id}</code>)\n"
            f"💬 Текст: {m.text}"
        )
        bot.send_message(m.chat.id, LANGS[l]['feedback_thanks'], reply_markup=main_keyboard(m.from_user.id))

# --- АДМИНКА ---
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_admin'], LANGS['en']['btn_admin']] and m.from_user.id == MY_ADMIN_ID)
def admin_panel(m):
    init_user(m.from_user)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="adm_stat"),
        types.InlineKeyboardButton("📢 Рассылка всем", callback_data="adm_bc"),
        types.InlineKeyboardButton("✉️ Личное сообщение", callback_data="adm_pm")
    )
    bot.send_message(m.chat.id, "🛠 <b>Админ-панель</b>", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def admin_cb(c):
    if c.from_user.id != MY_ADMIN_ID: return
    action = c.data.split("_")[1]
    
    if action == "stat":
        txt = (f"📊 <b>Статистика</b>\n\n"
               f"Юзеров в базе: {len(ALL_USER_IDS)}\n"
               f"Активных подписок: {len(AUTO_USERS)}\n"
               f"Всего запросов: {sum(u['requests'] for u in USER_DATA.values())}")
        bot.send_message(c.message.chat.id, txt)
        
    elif action == "bc":
        msg = bot.send_message(c.message.chat.id, "Введите текст для всех:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, do_bc)
        
    elif action == "pm":
        msg = bot.send_message(c.message.chat.id, "Введите ID юзера:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, do_pm_step1)

def do_bc(m):
    count = 0
    for uid in ALL_USER_IDS:
        try:
            bot.send_message(uid, f"📢 <b>ОБЪЯВЛЕНИЕ:</b>\n\n{m.text}")
            count += 1
        except: pass
    bot.send_message(m.chat.id, f"✅ Отправлено {count} чел.")

def do_pm_step1(m):
    try:
        tid = int(m.text.strip())
        msg = bot.send_message(m.chat.id, f"Текст для {tid}:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, lambda s: do_pm_step2(s, tid))
    except: bot.send_message(m.chat.id, "❌ Ошибка ID.")

def do_pm_step2(m, tid):
    try:
        bot.send_message(tid, f"✉️ <b>СООБЩЕНИЕ ОТ АДМИНА:</b>\n\n{m.text}")
        bot.send_message(m.chat.id, "✅ Доставлено.")
    except: bot.send_message(m.chat.id, "❌ Не доставлено.")

# ============== ПРОЧЕЕ ==============
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_auto'], LANGS['en']['btn_auto']])
def toggle_auto(m):
    init_user(m.from_user); l = USER_DATA[m.from_user.id]['lang']
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("1H", callback_data="auto_3600"), types.InlineKeyboardButton("5H", callback_data="auto_18000"), types.InlineKeyboardButton("24H", callback_data="auto_86400"))
    if m.chat.id in AUTO_USERS: kb.row(types.InlineKeyboardButton("🚫 OFF", callback_data="auto_0"))
    bot.send_message(m.chat.id, LANGS[l]['auto_menu'], reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("auto_"))
def auto_callback(c):
    init_user(c.from_user); l = USER_DATA[c.from_user.id]['lang']; val = int(c.data.split("_")[1])
    if val == 0:
        AUTO_USERS.pop(c.message.chat.id, None)
        bot.edit_message_text(LANGS[l]['auto_off_msg'], c.message.chat.id, c.message.message_id)
    else:
        AUTO_USERS[c.message.chat.id] = {"interval": val, "last": now_msk()}
        bot.edit_message_text(f"{LANGS[l]['auto_on_msg']} {val//3600}H.", c.message.chat.id, c.message.message_id)

@bot.message_handler(func=lambda m: True)
def auto_update_kb(m):
    """Исправление для старых пользователей: обновление кнопок"""
    init_user(m.from_user); l = USER_DATA[m.from_user.id]['lang']
    bot.send_message(m.chat.id, LANGS[l]['menu_updated'], reply_markup=main_keyboard(m.from_user.id))

# ============== LAUNCH ==============
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
                    u, b, r, (ab_b, ab_s) = fetch_rates()
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
