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
from flask import Flask

# ============== НАСТРОЙКИ ==============
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ВАШ ID для доступа к админке (Обязательно впишите свой)
MY_ADMIN_ID = 5266659205 
ADMIN_LOG_CHAT_ID = -1003264764082 
MOSCOW_TZ = timezone(timedelta(hours=3))

# Словари (только нужные разделы)
LANGS = {
    'ru': {
        'welcome': "👋 Выберите язык / Select language:",
        'menu': "Главное меню:",
        'rates': "📊 КУРСЫ",
        'auto': "🔔 УВЕДОМЛЕНИЯ",
        'feedback': "✍️ ОТЗЫВ",
        'profile': "👤 ПРОФИЛЬ",
        'admin': "🛠 АДМИНКА",
        'loading': "⚡️ <i>Загрузка данных...</i>",
        'feedback_prompt': "Напишите ваш отзыв или предложение одним сообщением:",
        'rate_title': "P2P МОНИТОРИНГ",
        'contact': "💳 Обмен: @Abdulkhaiii",
        'menu_updated': "🔄 Меню обновлено. Выберите нужный раздел."
    },
    'en': {
        'welcome': "👋 Select language:",
        'menu': "Main Menu:",
        'rates': "📊 RATES",
        'auto': "🔔 ALERTS",
        'feedback': "✍️ FEEDBACK",
        'profile': "👤 PROFILE",
        'admin': "🛠 ADMIN",
        'loading': "⚡️ <i>Fetching data...</i>",
        'feedback_prompt': "Write your feedback in one message:",
        'rate_title': "P2P MONITORING",
        'contact': "💳 Exchange: @Abdulkhaiii",
        'menu_updated': "🔄 Menu updated. Please select an option."
    }
}

USER_DATA = defaultdict(lambda: {'lang': 'ru', 'requests': 0})
AUTO_USERS = {}
ALL_USER_IDS = set() 

# ============== УТИЛИТЫ ==============
def now_msk(): return datetime.now(MOSCOW_TZ)

def fmt(v, d=2):
    if v is None: return "—"
    return f"{v:,.{d}f}".replace(",", " ").replace(".", ",")

def log_event(user, action, important=False):
    """Логируем только действия юзеров, автообновление идет тихо"""
    try:
        tag = "🔴 <b>ОТЗЫВ</b> 🔴\n" if important else "⚙️ Действие: "
        text = (f"{tag}<code>{action}</code>\n"
                f"👤 <b>{user.first_name}</b> (@{user.username or 'id'+str(user.id)})\n"
                f"🕒 {now_msk().strftime('%H:%M:%S')}")
        bot.send_message(ADMIN_LOG_CHAT_ID, text)
    except: pass

# ============== КЛАВИАТУРЫ ==============
def get_main_kb(uid):
    l = USER_DATA[uid]['lang']
    m = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    m.add(LANGS[l]['rates'], LANGS[l]['auto'])
    m.add(LANGS[l]['feedback'], LANGS[l]['profile'])
    if uid == MY_ADMIN_ID: 
        m.add(LANGS[l]['admin'])
    return m

# ============== ПАРСИНГ API ==============
def get_market_data():
    try:
        u = float(requests.get("https://api.upbit.com/v1/ticker?markets=KRW-USDT", timeout=5).json()[0]["trade_price"])
        bi = float(requests.get("https://api.bithumb.com/public/ticker/USDT_KRW", timeout=5).json()["data"]["closing_price"])
        ab = requests.get("https://hub.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB", timeout=5).json()
        return u, bi, float(ab["bid"][0]["price"]), float(ab["ask"][0]["price"])
    except: return None, None, None, None

# ============== ОБРАБОТЧИКИ ==============
@bot.message_handler(commands=['start'])
def start(m):
    ALL_USER_IDS.add(m.chat.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🇷🇺 Русский", callback_data="l_ru"),
           types.InlineKeyboardButton("🇬🇧 English", callback_data="l_en"))
    bot.send_message(m.chat.id, LANGS['ru']['welcome'], reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("l_"))
def lang_set(c):
    l = c.data.split("_")[1]
    USER_DATA[c.from_user.id]['lang'] = l
    bot.delete_message(c.message.chat.id, c.message.message_id)
    bot.send_message(c.message.chat.id, LANGS[l]['menu'], reply_markup=get_main_kb(c.from_user.id))
    log_event(c.from_user, "Выбрал язык и зашел в меню")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['rates'], LANGS['en']['rates']])
def rates(m):
    l = USER_DATA[m.from_user.id]['lang']
    st = bot.send_message(m.chat.id, LANGS[l]['loading'])
    u, bi, ab_b, ab_s = get_market_data()
    
    text = (f"<b>{LANGS[l]['rate_title']}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"🇰🇷 <b>KRW Markets</b>\n"
            f"◾ Upbit:   <code>{fmt(u, 0)} ₩</code>\n"
            f"◾ Bithumb: <code>{fmt(bi, 0)} ₩</code>\n\n"
            f"🇷🇺 <b>USDT / RUB</b>\n"
            f"◾ Buy:  <code>{fmt(ab_b, 2)} ₽</code>\n"
            f"◾ Sell: <code>{fmt(ab_s, 2)} ₽</code>\n"
            f"━━━━━━━━━━━━━━\n"
            f"⏱ {now_msk().strftime('%H:%M:%S')} MSK\n"
            f"<i>{LANGS[l]['contact']}</i>")
    bot.edit_message_text(text, m.chat.id, st.message_id)
    USER_DATA[m.from_user.id]['requests'] += 1
    log_event(m.from_user, "запрос курса")

# --- УВЕДОМЛЕНИЯ ---
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['auto'], LANGS['en']['auto']])
def auto_menu(m):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("1H", callback_data="auto_3600"),
           types.InlineKeyboardButton("5H", callback_data="auto_18000"))
    kb.add(types.InlineKeyboardButton("🚫 OFF", callback_data="auto_0"))
    bot.send_message(m.chat.id, "Настройка уведомлений / Alert settings:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("auto_"))
def auto_set(c):
    val = int(c.data.split("_")[1])
    cid = c.message.chat.id
    if val == 0:
        AUTO_USERS.pop(cid, None)
        bot.edit_message_text("🔕 Выключено / Disabled", cid, c.message.message_id)
        log_event(c.from_user, "отключил авто-отчеты")
    else:
        AUTO_USERS[cid] = {'interval': val, 'last': now_msk()}
        bot.edit_message_text(f"✅ Включено / Enabled ({val//3600}H)", cid, c.message.message_id)
        log_event(c.from_user, f"включил авто-отчеты ({val//3600}h)")

# --- ОТЗЫВ ---
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['feedback'], LANGS['en']['feedback']])
def feedback_msg(m):
    l = USER_DATA[m.from_user.id]['lang']
    msg = bot.send_message(m.chat.id, LANGS[l]['feedback_prompt'], reply_markup=types.ForceReply())
    bot.register_next_step_handler(msg, feedback_send)

def feedback_send(m):
    l = USER_DATA[m.from_user.id]['lang']
    log_event(m.from_user, m.text, important=True)
    bot.send_message(m.chat.id, "✅ Done!", reply_markup=get_main_kb(m.from_user.id))

# --- ПРОФИЛЬ ---
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['profile'], LANGS['en']['profile']])
def show_profile(m):
    l = USER_DATA[m.from_user.id]['lang']
    txt = (f"{LANGS[l]['profile']}\n\n"
           f"🆔 ID: <code>{m.from_user.id}</code>\n"
           f"📊 Запросов / Requests: <b>{USER_DATA[m.from_user.id]['requests']}</b>")
    bot.send_message(m.chat.id, txt, reply_markup=get_main_kb(m.from_user.id))

# --- АДМИНКА ---
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['admin'], LANGS['en']['admin']] and m.from_user.id == MY_ADMIN_ID)
def admin_panel(m):
    stats = f"🛠 <b>АДМИН ПАНЕЛЬ</b>\n\nВсего пользователей за сессию: {len(ALL_USER_IDS)}\nАктивных рассылок: {len(AUTO_USERS)}"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📢 Рассылка (Broadcast)", callback_data="adm_bc"))
    bot.send_message(m.chat.id, stats, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "adm_bc")
def broadcast_prompt(c):
    msg = bot.send_message(c.message.chat.id, "Введите текст для всех пользователей:", reply_markup=types.ForceReply())
    bot.register_next_step_handler(msg, broadcast_run)

def broadcast_run(m):
    count = 0
    for uid in ALL_USER_IDS:
        try:
            bot.send_message(uid, f"📢 <b>УВЕДОМЛЕНИЕ:</b>\n\n{m.text}")
            count += 1
        except: pass
    bot.send_message(m.chat.id, f"✅ Успешно отправлено {count} чел.")

# --- ПЕРЕХВАТЧИК ДЛЯ СТАРЫХ ПОЛЬЗОВАТЕЛЕЙ (ОБНОВЛЕНИЕ МЕНЮ) ---
@bot.message_handler(func=lambda m: True)
def update_old_users(m):
    ALL_USER_IDS.add(m.chat.id)
    l = USER_DATA[m.from_user.id]['lang']
    # При любом старом/непонятном сообщении выдаем актуальную клавиатуру
    bot.send_message(m.chat.id, LANGS[l]['menu_updated'], reply_markup=get_main_kb(m.from_user.id))

# ============== ФОНОВАЯ РАССЫЛКА ==============
def auto_worker():
    while True:
        time.sleep(60)
        now = now_msk()
        if now.hour >= 23 or now.hour < 8: continue
        
        for cid, d in list(AUTO_USERS.items()):
            if (now - d['last']).total_seconds() >= d['interval']:
                try:
                    u, bi, ab_b, ab_s = get_market_data()
                    text = f"🔔 <b>AUTO RATE</b>\n\n🇰🇷 KRW (Upbit): {fmt(u,0)} ₩\n🇷🇺 RUB (ABCEX): {fmt(ab_b,2)} ₽"
                    bot.send_message(cid, text)
                    AUTO_USERS[cid]['last'] = now
                except: pass

# ============== ЗАПУСК ==============
app = Flask(__name__)
@app.route('/')
def h(): return "Bot Online", 200

if __name__ == "__main__":
    threading.Thread(target=auto_worker, daemon=True).start()
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    
    print("Бот запущен. Ожидание запросов...")
    while True:
        try:
            bot.infinity_polling(skip_pending=True)
        except Exception as e:
            time.sleep(5)
