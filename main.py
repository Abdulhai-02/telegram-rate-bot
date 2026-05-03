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

ADMIN_LOG_CHAT_ID = -1003264764082 
MOSCOW_TZ = timezone(timedelta(hours=3))

# Словари (RU / EN)
LANGS = {
    'ru': {
        'welcome': "👋 Добро пожаловать! Выберите язык для работы:",
        'main_menu': "Выберите раздел:",
        'rates': "📊 КУРСЫ И СПРЕД",
        'auto': "🔔 УВЕДОМЛЕНИЯ",
        'feedback': "✍️ ОТЗЫВ",
        'profile': "👤 АККАУНТ",
        'loading': "⚡️ <i>Синхронизация с биржами Кореи и РФ...</i>",
        'feedback_prompt': "Пожалуйста, напишите ваш отзыв или предложение:",
        'feedback_thanks': "✅ Ваш отзыв отправлен! Спасибо за помощь в развитии.",
        'rate_title': "P2P МОНИТОРИНГ",
        'spread': "Разница бирж (Spread):",
        'contact': "💳 Обмен: @Abdulkhaiii"
    },
    'en': {
        'welcome': "👋 Welcome! Select your language:",
        'main_menu': "Main Menu:",
        'rates': "📊 RATES & SPREAD",
        'auto': "🔔 ALERTS",
        'feedback': "✍️ FEEDBACK",
        'profile': "👤 ACCOUNT",
        'loading': "⚡️ <i>Syncing Korea & RU exchanges...</i>",
        'feedback_prompt': "Please write your feedback or suggestion:",
        'feedback_thanks': "✅ Feedback sent! Thank you for your support.",
        'rate_title': "P2P MONITORING",
        'spread': "Market Spread:",
        'contact': "💳 Exchange: @Abdulkhaiii"
    }
}

USER_DATA = defaultdict(lambda: {'lang': None, 'requests': 0})
AUTO_USERS = {}

# ============== UTILS ==============
def now_msk():
    return datetime.now(MOSCOW_TZ)

def fmt(v, d=2):
    if v is None: return "—"
    return f"{v:,.{d}f}".replace(",", " ").replace(".", ",")

def log_event(user, action, is_feedback=False):
    try:
        tag = "🔴 <b>ОТЗЫВ</b> 🔴\n" if is_feedback else "⚙️ Action: "
        text = (f"{tag}<code>{action}</code>\n"
                f"👤 <b>{user.first_name}</b> (@{user.username or 'id'+str(user.id)})\n"
                f"🕒 {now_msk().strftime('%H:%M:%S')}")
        bot.send_message(ADMIN_LOG_CHAT_ID, text)
    except: pass

# ============== KEYBOARDS ==============
def get_lang_kb():
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("🇷🇺 Русский", callback_data="setlang_ru"),
          types.InlineKeyboardButton("🇬🇧 English", callback_data="setlang_en"))
    return m

def get_main_kb(uid):
    l = USER_DATA[uid]['lang'] or 'ru'
    m = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    m.add(LANGS[l]['rates'], LANGS[l]['auto'])
    m.add(LANGS[l]['feedback'], LANGS[l]['profile'])
    return m

# ============== MARKET ENGINE ==============
class MarketAPI:
    @staticmethod
    def fetch():
        def get_upbit():
            return float(requests.get("https://api.upbit.com/v1/ticker?markets=KRW-USDT", timeout=4).json()[0]["trade_price"])
        def get_bithumb():
            return float(requests.get("https://api.bithumb.com/public/ticker/USDT_KRW", timeout=4).json()["data"]["closing_price"])
        def get_abcex():
            d = requests.get("https://hub.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB", timeout=4).json()
            return float(d["bid"][0]["price"]), float(d["ask"][0]["price"])
        def get_rub_krw():
            r = requests.get("https://open.er-api.com/v6/latest/RUB", timeout=4).json()
            return 1_000_000 / r["rates"]["KRW"]

        with concurrent.futures.ThreadPoolExecutor() as ex:
            f_u, f_b, f_a, f_k = ex.submit(get_upbit), ex.submit(get_bithumb), ex.submit(get_abcex), ex.submit(get_rub_krw)
            return f_u.result(), f_b.result(), f_a.result(), f_k.result()

# ============== HANDLERS ==============
@bot.message_handler(commands=['start'])
def start(m):
    # Если язык не выбран, всегда предлагаем выбор
    if USER_DATA[m.from_user.id]['lang'] is None:
        bot.send_message(m.chat.id, LANGS['ru']['welcome'], reply_markup=get_lang_kb())
    else:
        bot.send_message(m.chat.id, LANGS[USER_DATA[m.from_user.id]['lang']]['main_menu'], 
                         reply_markup=get_main_kb(m.from_user.id))

@bot.callback_query_handler(func=lambda c: c.data.startswith("setlang_"))
def callback_lang(c):
    l = c.data.split("_")[1]
    USER_DATA[c.from_user.id]['lang'] = l
    bot.delete_message(c.message.chat.id, c.message.message_id)
    bot.send_message(c.message.chat.id, LANGS[l]['main_menu'], reply_markup=get_main_kb(c.from_user.id))
    log_event(c.from_user, f"Language set: {l}")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['rates'], LANGS['en']['rates']])
def show_rates(m):
    l = USER_DATA[m.from_user.id]['lang'] or 'ru'
    status = bot.send_message(m.chat.id, LANGS[l]['loading'])
    
    try:
        u, bi, (ab_b, ab_s), kr = MarketAPI.fetch()
        spread = abs(u - bi) if u and bi else 0
        
        text = (
            f"<b>{LANGS[l]['rate_title']}</b>\n"
            f"<pre>Time: {now_msk().strftime('%H:%M:%S')}</pre>\n\n"
            f"🇰🇷 <b>KRW Markets:</b>\n"
            f"├ Upbit:   <code>{fmt(u, 0)} ₩</code>\n"
            f"└ Bithumb: <code>{fmt(bi, 0)} ₩</code>\n"
            f"📉 {LANGS[l]['spread']} <code>{fmt(spread, 0)} ₩</code>\n\n"
            f"🇷🇺 <b>ABCEX RUB:</b>\n"
            f"├ Buy:  <code>{fmt(ab_b, 2)} ₽</code>\n"
            f"└ Sell: <code>{fmt(ab_s, 2)} ₽</code>\n\n"
            f"🔄 <b>1M ₩ ≈</b> <code>{fmt(kr, 0)} ₽</code>\n\n"
            f"<i>{LANGS[l]['contact']}</i>"
        )
        bot.edit_message_text(text, m.chat.id, status.message_id)
        USER_DATA[m.from_user.id]['requests'] += 1
        log_event(m.from_user, "Check rates")
    except:
        bot.edit_message_text("❌ Connection Error", m.chat.id, status.message_id)

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['feedback'], LANGS['en']['feedback']])
def feedback_init(m):
    l = USER_DATA[m.from_user.id]['lang'] or 'ru'
    msg = bot.send_message(m.chat.id, LANGS[l]['feedback_prompt'], reply_markup=types.ForceReply())
    bot.register_next_step_handler(msg, feedback_process)

def feedback_process(m):
    l = USER_DATA[m.from_user.id]['lang'] or 'ru'
    if m.text:
        log_event(m.from_user, m.text, is_feedback=True)
        bot.send_message(m.chat.id, LANGS[l]['feedback_thanks'], reply_markup=get_main_kb(m.from_user.id))

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['profile'], LANGS['en']['profile']])
def profile(m):
    l = USER_DATA[m.from_user.id]['lang'] or 'ru'
    txt = (f"👤 {LANGS[l]['profile']}\n\n"
           f"🆔 ID: <code>{m.from_user.id}</code>\n"
           f"📊 Requests: <b>{USER_DATA[m.from_user.id]['requests']}</b>")
    bot.send_message(m.chat.id, txt, reply_markup=get_main_kb(m.from_user.id))

# ============== BACKGROUND WORKER ==============
def worker():
    while True:
        time.sleep(60)
        now = now_msk()
        if now.hour >= 23 or now.hour < 8: continue
        
        for cid, d in list(AUTO_USERS.items()):
            if (now - d['last']).total_seconds() >= d['interval']:
                try:
                    # Авто-рассылка без логов в админ-чат
                    u, bi, (ab_b, ab_s), kr = MarketAPI.fetch()
                    text = f"🔔 <b>AUTO RATE</b>\n\n🇰🇷 KRW: {fmt(u,0)} ₩\n🇷🇺 RUB: {fmt(ab_b,2)} ₽"
                    bot.send_message(cid, text)
                    AUTO_USERS[cid]['last'] = now
                except: pass

# ============== LAUNCH ==============
app = Flask(__name__)
@app.route('/')
def h(): return "Bot Online", 200

if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    
    while True:
        try:
            bot.infinity_polling(skip_pending=True)
        except Exception as e:
            time.sleep(5)
