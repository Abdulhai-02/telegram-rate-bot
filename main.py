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
from bs4 import BeautifulSoup
from flask import Flask

# ============== НАСТРОЙКИ ==============
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN в переменных окружения")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ВАШ ID для доступа к админке
MY_ADMIN_ID = 5266659205  
ADMIN_LOG_CHAT_ID = -1003264764082
MOSCOW_TZ = timezone(timedelta(hours=3))

AUTO_INTERVAL_1H = 60 * 60
AUTO_INTERVAL_5H = 5 * 60 * 60
AUTO_INTERVAL_24H = 24 * 60 * 60

# ============== ЛОКАЛИЗАЦИЯ ==============
LANGS = {
    'ru': {
        'btn_show': "📊 Показать курс",
        'btn_auto': "🔔 Автообновление",
        'btn_profile': "👤 Профиль",
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
        'prof_join': "Дата регистрации:"
    },
    'en': {
        'btn_show': "📊 Show Rates",
        'btn_auto': "🔔 Auto-updates",
        'btn_profile': "👤 Profile",
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
        'prof_join': "Joined date:"
    }
}

# ============== БАЗА ДАННЫХ В ПАМЯТИ ==============
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
            "lang": "ru",
            "requests": 0,
            "last": None,
            "joined": now_msk(),
            "first_name": user.first_name,
            "username": user.username
        }

def log_to_channel(text):
    try: bot.send_message(ADMIN_LOG_CHAT_ID, text)
    except: pass

def log_user_action(user, action):
    try:
        log_to_channel(
            f"👤 @{user.username or 'без_username'} (ID {user.id})\n"
            f"🕒 {now_msk().strftime('%d.%m.%Y %H:%M:%S')} МСК\n➡️ {action}"
        )
    except: pass

# ============== КЛАВИАТУРЫ ==============
def main_keyboard(uid):
    l = USER_DATA.get(uid, {}).get("lang", "ru")
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row(LANGS[l]['btn_show'], LANGS[l]['btn_auto'])
    m.row(LANGS[l]['btn_profile'], LANGS[l]['btn_disable'])
    if uid == MY_ADMIN_ID:
        m.row(LANGS[l]['btn_admin'])
    return m

def ensure_keyboard(m):
    try: bot.send_message(m.chat.id, "👇", reply_markup=main_keyboard(m.from_user.id))
    except: pass

# ============== API ==============
def get_upbit_usdt_krw():
    try:
        r = requests.get("https://api.upbit.com/v1/ticker", params={"markets": "KRW-USDT"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=4)
        return float(r.json()[0]["trade_price"])
    except: return None

def get_bithumb_usdt_krw():
    try:
        r = requests.get("https://api.bithumb.com/public/ticker/USDT_KRW", headers={"User-Agent": "Mozilla/5.0"}, timeout=4)
        return float(r.json()["data"]["closing_price"])
    except: return None

def get_krw_rub_from_google():
    try:
        r = requests.get("https://open.er-api.com/v6/latest/RUB", timeout=5)
        return 1_000_000 / r.json()["rates"]["KRW"]
    except: return None

def get_abcex_usdt_rub():
    try:
        r = requests.get("https://hub.abcex.io/api/v2/exchange/public/orderbook/depth", params={"instrumentCode": "USDTRUB", "lang": "ru"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=4)
        data = r.json()
        return float(data["bid"][0]["price"]), float(data["ask"][0]["price"])
    except: return None, None

# ============== ТЕКСТ КУРСА ==============
def build_rate_text(lang, upbit, bithumb, rub, ab_buy=None, ab_sell=None):
    d = LANGS[lang]
    timestamp = now_msk().strftime("%d.%m.%Y, %H:%M")
    
    u_txt = f"{fmt_num(upbit, 0)} ₩" if upbit else "—"
    b_txt = f"{fmt_num(bithumb, 0)} ₩" if bithumb else "—"
    r_txt = f"{fmt_num(rub, 2)} ₽" if rub else "—"
    ab_b_txt = f"{fmt_num(ab_buy, 2)} ₽" if ab_buy else "—"
    ab_s_txt = f"{fmt_num(ab_sell, 2)} ₽" if ab_sell else "—"

    text = (
        f"{d['rate_title']}\n\n"
        f"{d['usdt_krw']}\n"
        f"◾ UPBIT:      <b>{u_txt}</b>\n"
        f"◾ BITHUMB:    <b>{b_txt}</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        f"{d['usdt_rub']}\n"
        f"◾ {d['buy']}    <b>{ab_b_txt}</b>\n"
        f"◾ {d['sell']}    <b>{ab_s_txt}</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        f"{d['krw_rub']}\n"
        f"◾ 1 000 000 ₩ → <b>{r_txt}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"{d['updated']} <b>{timestamp} (МСК)</b>\n\n"
        f"{d['contact']}"
    )
    return text

# ============== ОБРАБОТЧИКИ ==============
@bot.message_handler(commands=["start"])
def start_handler(m):
    init_user(m.from_user)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
           types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"))
    bot.send_message(m.chat.id, "🇷🇺 Выберите язык / 🇬🇧 Select language:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def lang_set(c):
    l = c.data.split("_")[1]
    init_user(c.from_user)
    USER_DATA[c.from_user.id]['lang'] = l
    bot.delete_message(c.message.chat.id, c.message.message_id)
    bot.send_message(c.message.chat.id, LANGS[l]['welcome'], reply_markup=main_keyboard(c.from_user.id))
    log_user_action(c.from_user, f"выбрал язык: {l}")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_show'], LANGS['en']['btn_show']])
def show_rate(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    cid = m.chat.id
    
    msg = bot.send_message(cid, f"{LANGS[l]['loading']}, ожидайте...")
    stop = {"run": True}

    def anim():
        dots = [".", "..", "..."]
        i = 0
        while stop["run"]:
            try: bot.edit_message_text(f"{LANGS[l]['loading']}{dots[i%3]}...", cid, msg.message_id)
            except: break
            i += 1
            time.sleep(0.6)

    threading.Thread(target=anim, daemon=True).start()

    with concurrent.futures.ThreadPoolExecutor() as ex:
        f_u, f_b, f_r, f_ab = ex.submit(get_upbit_usdt_krw), ex.submit(get_bithumb_usdt_krw), ex.submit(get_krw_rub_from_google), ex.submit(get_abcex_usdt_rub)
        u, b, r, (ab_buy, ab_sell) = f_u.result(), f_b.result(), f_r.result(), f_ab.result()

    stop["run"] = False
    time.sleep(0.4)

    if not any([u, b, r, ab_buy, ab_sell]):
        bot.edit_message_text(LANGS[l]['error_fetch'], cid, msg.message_id)
        return

    txt = build_rate_text(l, u, b, r, ab_buy, ab_sell)
    bot.edit_message_text(txt, cid, msg.message_id, parse_mode="HTML")
    
    USER_DATA[m.from_user.id]["requests"] += 1
    USER_DATA[m.from_user.id]["last"] = now_msk()
    log_user_action(m.from_user, "запросил курс")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_auto'], LANGS['en']['btn_auto']])
def toggle_auto(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    cid = m.chat.id

    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton(f"⏱ 1H", callback_data="auto_3600"),
           types.InlineKeyboardButton(f"⏱ 5H", callback_data="auto_18000"),
           types.InlineKeyboardButton(f"🕛 24H", callback_data="auto_86400"))
    
    if cid in AUTO_USERS:
        kb.row(types.InlineKeyboardButton(LANGS[l]['auto_off_btn'], callback_data="auto_0"))

    text = f"{LANGS[l]['auto_menu']}"
    if cid in AUTO_USERS:
        v = AUTO_USERS[cid]['interval']
        hh = f"{LANGS[l][f'auto_{v//3600}h']}" if v in [3600, 18000, 86400] else f"{v//3600}H"
        text += f"\n{LANGS[l]['auto_curr']} {hh}"

    bot.send_message(cid, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("auto_"))
def auto_callback(c):
    init_user(c.from_user)
    l = USER_DATA[c.from_user.id]['lang']
    cid = c.message.chat.id
    val = int(c.data.split("_")[1])

    if val == 0:
        AUTO_USERS.pop(cid, None)
        bot.edit_message_text(LANGS[l]['auto_off_msg'], cid, c.message.message_id)
        log_user_action(c.from_user, "выключил автообновление")
    else:
        AUTO_USERS[cid] = {"interval": val, "last": now_msk()}
        hh = f"{LANGS[l][f'auto_{val//3600}h']}"
        bot.edit_message_text(f"{LANGS[l]['auto_on_msg']} {hh}.", cid, c.message.message_id)
        log_user_action(c.from_user, f"включил авто ({val//3600}H)")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_disable'], LANGS['en']['btn_disable']])
def disable_notifications(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    if m.chat.id in AUTO_USERS:
        AUTO_USERS.pop(m.chat.id, None)
        bot.send_message(m.chat.id, LANGS[l]['auto_off_msg'])
        log_user_action(m.from_user, "отключил уведомления через меню")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_profile'], LANGS['en']['btn_profile']])
def profile(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    d = USER_DATA[m.from_user.id]
    
    last = d["last"].strftime("%d.%m.%Y %H:%M") if d["last"] else "—"
    join = d["joined"].strftime("%d.%m.%Y")
    nick = f"@{d['username']}" if d['username'] else d['first_name']

    txt = (
        f"{LANGS[l]['prof_title']}\n\n"
        f"ID: <code>{m.from_user.id}</code>\n"
        f"Nick: {nick}\n"
        f"{LANGS[l]['prof_join']} {join}\n\n"
        f"{LANGS[l]['prof_reqs']} {d['requests']}\n"
        f"{LANGS[l]['prof_last']} {last}"
    )
    bot.send_message(m.chat.id, txt)

# ============== РАСШИРЕННАЯ АДМИНКА ==============
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_admin'], LANGS['en']['btn_admin']] and m.from_user.id == MY_ADMIN_ID)
def admin_panel(m):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="adm_stat"),
        types.InlineKeyboardButton("👥 База пользователей", callback_data="adm_users"),
        types.InlineKeyboardButton("📢 Рассылка всем", callback_data="adm_bc"),
        types.InlineKeyboardButton("✉️ Сообщение юзеру", callback_data="adm_pm")
    )
    bot.send_message(m.chat.id, "🛠 <b>Панель управления</b>", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def admin_cb(c):
    if c.from_user.id != MY_ADMIN_ID: return
    action = c.data.split("_")[1]
    
    if action == "stat":
        reqs = sum(u['requests'] for u in USER_DATA.values())
        txt = f"📊 <b>Статистика</b>\n\nВсего пользователей: {len(USER_DATA)}\nАктивных рассылок: {len(AUTO_USERS)}\nВсего запросов курса: {reqs}"
        bot.send_message(c.message.chat.id, txt)
        
    elif action == "users":
        txt = "👥 <b>Список пользователей:</b>\n\n"
        for uid, d in USER_DATA.items():
            nick = f"@{d['username']}" if d['username'] else d['first_name']
            txt += f"• <code>{uid}</code> | {nick} | {d['joined'].strftime('%d.%m')}\n"
        # Если юзеров много, телеграм не пропустит больше 4096 символов, режем
        bot.send_message(c.message.chat.id, txt[:4000])
        
    elif action == "bc":
        msg = bot.send_message(c.message.chat.id, "Введите текст для массовой рассылки:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, do_broadcast)
        
    elif action == "pm":
        msg = bot.send_message(c.message.chat.id, "Введите ID пользователя:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, do_pm_step1)

def do_broadcast(m):
    count = 0
    for uid in USER_DATA:
        try:
            bot.send_message(uid, f"📢 <b>УВЕДОМЛЕНИЕ:</b>\n\n{m.text}")
            count += 1
        except: pass
    bot.send_message(m.chat.id, f"✅ Отправлено {count} пользователям.")

def do_pm_step1(m):
    try:
        target_id = int(m.text.strip())
        msg = bot.send_message(m.chat.id, f"Напишите текст сообщения для {target_id}:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, lambda step2: do_pm_step2(step2, target_id))
    except:
        bot.send_message(m.chat.id, "❌ Неверный ID.")

def do_pm_step2(m, target_id):
    try:
        bot.send_message(target_id, f"✉️ <b>СООБЩЕНИЕ ОТ АДМИНА:</b>\n\n{m.text}")
        bot.send_message(m.chat.id, "✅ Сообщение успешно доставлено.")
    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Ошибка отправки (Возможно, бот заблокирован).")

# ============== АВТООБНОВЛЕНИЕ ==============
def auto_update_loop():
    while True:
        time.sleep(60)
        if not AUTO_USERS: continue

        now = now_msk()
        try:
            with concurrent.futures.ThreadPoolExecutor() as ex:
                f_u, f_b, f_r, f_ab = ex.submit(get_upbit_usdt_krw), ex.submit(get_bithumb_usdt_krw), ex.submit(get_krw_rub_from_google), ex.submit(get_abcex_usdt_rub)
                u, b, r, (ab_buy, ab_sell) = f_u.result(), f_b.result(), f_r.result(), f_ab.result()

            if not any([u, b, r, ab_buy, ab_sell]): continue

            # Кэшируем текст для обоих языков, чтобы не собирать его для каждого юзера
            txt_ru = build_rate_text('ru', u, b, r, ab_buy, ab_sell)
            txt_en = build_rate_text('en', u, b, r, ab_buy, ab_sell)

            for cid, cfg in list(AUTO_USERS.items()):
                if (now - cfg["last"]).total_seconds() < cfg["interval"]: continue

                l = USER_DATA.get(cid, {}).get("lang", "ru")
                try:
                    bot.send_message(cid, txt_ru if l == 'ru' else txt_en, parse_mode="HTML")
                    AUTO_USERS[cid]["last"] = now
                except Exception as e:
                    if "blocked" in str(e).lower():
                        AUTO_USERS.pop(cid, None)
        except Exception: pass

# ============== ЗАПУСК ==============
def keep_awake():
    url = "https://telegram-rate-bot-ooc6.onrender.com"
    while True:
        try: requests.get(url, timeout=5)
        except: pass
        time.sleep(600)

app = Flask(__name__)
@app.route('/')
def home(): return "OK", 200

if __name__ == "__main__":
    threading.Thread(target=auto_update_loop, daemon=True).start()
    threading.Thread(target=keep_awake, daemon=True).start()
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True).start()

    print("🚀 Бот запущен.")
    while True:
        try: bot.infinity_polling(skip_pending=True)
        except Exception: time.sleep(5)
