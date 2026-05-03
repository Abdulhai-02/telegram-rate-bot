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

# ============== НАСТРОЙКИ ==============
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN!")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

ADMIN_LOG_CHAT_ID = -1003264764082 
MOSCOW_TZ = timezone(timedelta(hours=3))

# Словари перевода (RU и EN)
LANGS = {
    'ru': {
        'welcome': "👋 Выберите язык / Select language:",
        'main_msg': "Главное меню:",
        'rates': "📊 КУРСЫ",
        'auto': "🔔 УВЕДОМЛЕНИЯ",
        'feedback': "✍️ ОТЗЫВ",
        'profile': "👤 АККАУНТ",
        'loading': "⚡️ <i>Синхронизация с биржами...</i>",
        'feedback_prompt': "Напишите ваш отзыв или предложение одним сообщением:",
        'feedback_thanks': "✅ Спасибо! Ваш отзыв передан администратору.",
        'rate_title': "МОНИТОРИНГ КУРСОВ",
        'auto_menu': "Выберите частоту автоматических отчетов:",
        'auto_off': "🔕 Уведомления отключены.",
        'auto_on': "✅ Автообновление включено.",
        'contact': "💳 Обмен: @Abdulkhaiii"
    },
    'en': {
        'welcome': "👋 Select language:",
        'main_msg': "Main Menu:",
        'rates': "📊 RATES",
        'auto': "🔔 ALERTS",
        'feedback': "✍️ FEEDBACK",
        'profile': "👤 PROFILE",
        'loading': "⚡️ <i>Syncing with exchanges...</i>",
        'feedback_prompt': "Write your feedback or suggestion in one message:",
        'feedback_thanks': "✅ Thank you! Your feedback has been sent to the admin.",
        'rate_title': "EXCHANGE RATES",
        'auto_menu': "Select the frequency for automatic updates:",
        'auto_off': "🔕 Alerts disabled.",
        'auto_on': "✅ Auto-updates enabled.",
        'contact': "💳 Exchange: @Abdulkhaiii"
    }
}

# Базы данных в памяти
USER_DATA = defaultdict(lambda: {'lang': 'ru', 'requests': 0})
AUTO_USERS = {}

# ============== УТИЛИТЫ ==============
def now_msk():
    return datetime.now(MOSCOW_TZ)

def fmt_money(v, d=2):
    if v is None: return "—"
    return f"{v:,.{d}f}".replace(",", " ").replace(".", ",")

def log_event(user, action, is_important=False):
    """Отправляет логи только о реальных действиях пользователей"""
    try:
        prefix = "🔴 <b>ОТЗЫВ</b> 🔴\n" if is_important else "⚙️ Действие: "
        text = (f"{prefix}<code>{action}</code>\n"
                f"👤 <b>{user.first_name}</b> (@{user.username or 'id'+str(user.id)})\n"
                f"🕒 {now_msk().strftime('%H:%M:%S')}")
        bot.send_message(ADMIN_LOG_CHAT_ID, text)
    except Exception as e:
        print(f"Ошибка логирования: {e}")

# ============== ИНТЕРФЕЙС ==============
def lang_keyboard():
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
          types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"))
    return m

def main_keyboard(uid):
    l = USER_DATA[uid]['lang']
    m = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    m.add(LANGS[l]['rates'], LANGS[l]['auto'])
    m.add(LANGS[l]['feedback'], LANGS[l]['profile'])
    return m

# ============== ПАРСИНГ API ==============
class MarketAPI:
    @staticmethod
    def get_upbit():
        try:
            return float(requests.get("https://api.upbit.com/v1/ticker?markets=KRW-USDT", timeout=4).json()[0]["trade_price"])
        except: return None

    @staticmethod
    def get_bithumb():
        try:
            return float(requests.get("https://api.bithumb.com/public/ticker/USDT_KRW", timeout=4).json()["data"]["closing_price"])
        except: return None

    @staticmethod
    def get_abcex():
        try:
            data = requests.get("https://hub.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB", timeout=4).json()
            return float(data["bid"][0]["price"]), float(data["ask"][0]["price"])
        except: return None, None

    @staticmethod
    def get_krw_rub():
        try:
            r = requests.get("https://open.er-api.com/v6/latest/RUB", timeout=4).json()
            return 1_000_000 / r["rates"]["KRW"]
        except: return None

def build_rate_msg(uid, is_auto=False):
    l = USER_DATA[uid]['lang']
    with concurrent.futures.ThreadPoolExecutor() as ex:
        f_up = ex.submit(MarketAPI.get_upbit)
        f_bi = ex.submit(MarketAPI.get_bithumb)
        f_kr = ex.submit(MarketAPI.get_krw_rub)
        f_ab = ex.submit(MarketAPI.get_abcex)

        u, bi, kr, (ab_b, ab_s) = f_up.result(), f_bi.result(), f_kr.result(), f_ab.result()

    ts = now_msk().strftime("%H:%M:%S")
    title = f"🔔 AUTO {LANGS[l]['rate_title']}" if is_auto else LANGS[l]['rate_title']
    
    msg = (
        f"<b>{title}</b>\n"
        f"<pre>Time: {ts} MSK</pre>\n\n"
        f"🇰🇷 <b>USDT → KRW</b>\n"
        f"├ Upbit:   <code>{fmt_money(u, 0)} ₩</code>\n"
        f"└ Bithumb: <code>{fmt_money(bi, 0)} ₩</code>\n\n"
        f"🇷🇺 <b>USDT → RUB (ABCEX)</b>\n"
        f"├ Buy:  <code>{fmt_money(ab_b, 2)} ₽</code>\n"
        f"└ Sell: <code>{fmt_money(ab_s, 2)} ₽</code>\n\n"
        f"🔄 <b>KRW → RUB</b>\n"
        f"└ 1 000 000 ₩ ≈ <code>{fmt_money(kr, 0)} ₽</code>\n\n"
        f"<i>{LANGS[l]['contact']}</i>"
    )
    return msg

# ============== ОБРАБОТЧИКИ ==============
@bot.message_handler(commands=['start'])
def start_cmd(m):
    bot.send_message(m.chat.id, LANGS['ru']['welcome'], reply_markup=lang_keyboard())
    log_event(m.from_user, "запустил бота /start")

@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def set_lang(c):
    l = c.data.split("_")[1]
    USER_DATA[c.from_user.id]['lang'] = l
    bot.delete_message(c.message.chat.id, c.message.message_id)
    bot.send_message(c.message.chat.id, LANGS[l]['main_msg'], reply_markup=main_keyboard(c.from_user.id))
    log_event(c.from_user, f"выбрал язык: {l}")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['rates'], LANGS['en']['rates']])
def send_rates_cmd(m):
    l = USER_DATA[m.from_user.id]['lang']
    st = bot.send_message(m.chat.id, LANGS[l]['loading'])
    try:
        text = build_rate_msg(m.from_user.id)
        bot.edit_message_text(text, m.chat.id, st.message_id)
        USER_DATA[m.from_user.id]['requests'] += 1
        log_event(m.from_user, "запросил курсы")
    except Exception as e:
        bot.edit_message_text("❌ Data fetch error / Ошибка получения данных", m.chat.id, st.message_id)

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['auto'], LANGS['en']['auto']])
def auto_settings(m):
    l = USER_DATA[m.from_user.id]['lang']
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("1H / 1 Час", callback_data="set_3600"),
           types.InlineKeyboardButton("5H / 5 Часов", callback_data="set_18000"))
    kb.add(types.InlineKeyboardButton("24H / 24 Часа", callback_data="set_86400"))
    kb.add(types.InlineKeyboardButton("🚫 OFF / Отключить", callback_data="set_0"))
    
    bot.send_message(m.chat.id, LANGS[l]['auto_menu'], reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("set_"))
def handle_auto_set(c):
    seconds = int(c.data.split("_")[1])
    cid = c.message.chat.id
    l = USER_DATA[c.from_user.id]['lang']
    
    if seconds == 0:
        AUTO_USERS.pop(cid, None)
        log_event(c.from_user, "отключил автоуведомления")
        bot.edit_message_text(LANGS[l]['auto_off'], cid, c.message.message_id)
    else:
        AUTO_USERS[cid] = {"interval": seconds, "last": now_msk()}
        log_event(c.from_user, f"включил уведомления ({seconds//3600}ч)")
        bot.edit_message_text(LANGS[l]['auto_on'], cid, c.message.message_id)

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['feedback'], LANGS['en']['feedback']])
def feedback_start(m):
    l = USER_DATA[m.from_user.id]['lang']
    msg = bot.send_message(m.chat.id, LANGS[l]['feedback_prompt'], reply_markup=types.ForceReply())
    bot.register_next_step_handler(msg, feedback_save)

def feedback_save(m):
    l = USER_DATA[m.from_user.id]['lang']
    if m.text:
        log_event(m.from_user, m.text, is_important=True)
        bot.send_message(m.chat.id, LANGS[l]['feedback_thanks'])

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['profile'], LANGS['en']['profile']])
def view_profile(m):
    l = USER_DATA[m.from_user.id]['lang']
    uid = m.from_user.id
    msg = (f"{LANGS[l]['profile']}\n\n"
           f"🆔 ID: <code>{uid}</code>\n"
           f"📊 Requests: <b>{USER_DATA[uid]['requests']}</b>")
    bot.send_message(m.chat.id, msg)

# ============== ФОНОВЫЕ ПРОЦЕССЫ ==============
def auto_worker():
    """Рассылка по таймеру (АБСОЛЮТНО БЕЗ ЛОГИРОВАНИЯ В АДМИН ЧАТ)"""
    while True:
        time.sleep(60)
        now = now_msk()
        if now.hour >= 23 or now.hour < 8: continue # Пауза с 23:00 до 08:00 МСК
        
        for cid, data in list(AUTO_USERS.items()):
            if (now - data['last']).total_seconds() >= data['interval']:
                try:
                    text = build_rate_msg(cid, is_auto=True)
                    bot.send_message(cid, text)
                    AUTO_USERS[cid]['last'] = now
                except ApiTelegramException as e:
                    if "blocked" in str(e).lower():
                        AUTO_USERS.pop(cid, None)
                except Exception:
                    pass

# ============== ЗАПУСК СЕРВЕРА ==============
app = Flask(__name__)
@app.route('/')
def health_check(): return "P2P Bot is Active", 200

if __name__ == "__main__":
    # 1. Запуск потока авто-рассылки
    threading.Thread(target=auto_worker, daemon=True).start()
    
    # 2. Запуск фейкового веб-сервера (чтобы Render не глушил контейнер)
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    
    print("🚀 P2P Бот успешно запущен!")
    
    # 3. Бесперебойный опрос Telegram (пропуск старых сообщений при рестарте)
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60)
        except Exception as e:
            print(f"Критическая ошибка Polling: {e}. Перезапуск через 5 сек...")
            time.sleep(5)
