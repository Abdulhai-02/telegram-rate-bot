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
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN в переменных окружения")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# ВАШ ID (Проверьте его корректность)
MY_ADMIN_ID = 5143360493  
ADMIN_LOG_CHAT_ID = -1003264764082
MOSCOW_TZ = timezone(timedelta(hours=3))

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
        'auto_menu': "Выбери частоту автообновления:",
        'auto_off_msg': "🔕 Автообновление выключено.",
        'auto_on_msg': "🔔 Включено уведомление:",
        'feedback_prompt': "Напишите ваш отзыв одним сообщением:",
        'feedback_thanks': "✅ Спасибо! Ваш отзыв передан администратору.",
        'menu_updated': "🔄 Меню обновлено.",
        'prof_title': "👤 <b>Ваш Профиль</b>",
        'prof_reqs': "Всего запросов курса:",
        'prof_last': "Последний запрос:",
        'prof_join': "В боте с:"
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
        'auto_menu': "Select update frequency:",
        'auto_off_msg': "🔕 Auto-updates disabled.",
        'auto_on_msg': "🔔 Alerts enabled:",
        'menu_updated': "🔄 Menu updated.",
        'prof_title': "👤 <b>Your Profile</b>",
        'prof_reqs': "Total requests:",
        'prof_last': "Last request:",
        'prof_join': "Member since:"
    }
}

USER_DATA = {}
AUTO_USERS = {}
ALL_USER_IDS = set() 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============== УТИЛИТЫ И ЛОГИРОВАНИЕ ==============
def now_msk(): return datetime.now(MOSCOW_TZ)

def fmt_num(v, d=2):
    if v is None: return "—"
    return f"{v:,.{d}f}".replace(",", " ")

def init_user(user):
    uid = user.id
    ALL_USER_IDS.add(uid)
    if uid not in USER_DATA:
        USER_DATA[uid] = {
            "lang": "ru", "requests": 0, "last": None,
            "joined": now_msk(), "first_name": user.first_name, "username": user.username
        }

def log_action(user, action, result=None):
    try:
        name = user.first_name
        if user.last_name: name += f" {user.last_name}"
        nick = f"@{user.username}" if user.username else "Нет ника"
        time_str = now_msk().strftime('%H:%M:%S')

        log_text = (
            f"⚙️ <b>Лог действия</b>\n"
            f"👤 Имя: {name}\n"
            f"🔗 Ник / ID: {nick} | <code>{user.id}</code>\n"
            f"🔘 Кнопка: <b>{action}</b>\n"
        )
        if result: log_text += f"📊 Результат: {result}\n"
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
    if uid == MY_ADMIN_ID: m.row(LANGS[l]['btn_admin'])
    return m

# ============== API ==============
def fetch_all_rates():
    headers = {"User-Agent": "Mozilla/5.0"}
    
    def g_u():
        try: return float(requests.get("https://api.upbit.com/v1/ticker?markets=KRW-USDT", headers=headers, timeout=5).json()[0]["trade_price"])
        except: return None
    def g_b():
        try: return float(requests.get("https://api.bithumb.com/public/ticker/USDT_KRW", headers=headers, timeout=5).json()["data"]["closing_price"])
        except: return None
    def g_r():
        try: return 1_000_000 / requests.get("https://open.er-api.com/v6/latest/RUB", timeout=5).json()["rates"]["KRW"]
        except: return None
    def g_ab():
        try:
            d = requests.get("https://hub.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB", headers=headers, timeout=5).json()
            return float(d["bid"][0]["price"]), float(d["ask"][0]["price"])
        except: return None, None

    with concurrent.futures.ThreadPoolExecutor() as ex:
        return ex.submit(g_u).result(), ex.submit(g_b).result(), ex.submit(g_r).result(), ex.submit(g_ab).result()

# ============== ОБРАБОТЧИКИ ==============
@bot.message_handler(commands=["start"])
def start_handler(m):
    init_user(m.from_user)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
           types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"))
    bot.send_message(m.chat.id, "🇷🇺 Выберите язык / 🇬🇧 Select language:", reply_markup=kb)
    log_action(m.from_user, "Команда /start")

@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def lang_set(c):
    l = c.data.split("_")[1]
    init_user(c.from_user)
    USER_DATA[c.from_user.id]['lang'] = l
    bot.delete_message(c.message.chat.id, c.message.message_id)
    bot.send_message(c.message.chat.id, LANGS[l]['welcome'], reply_markup=main_keyboard(c.from_user.id))
    log_action(c.from_user, f"Выбор языка ({'Русский' if l == 'ru' else 'English'})")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_show'], LANGS['en']['btn_show']])
def show_rate(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    msg = bot.send_message(m.chat.id, f"{LANGS[l]['loading']}...")
    
    u, b, r, (ab_buy, ab_sell) = fetch_all_rates()
    
    if not any([u, b, r, ab_buy]):
        bot.edit_message_text(LANGS[l]['error_fetch'], m.chat.id, msg.message_id)
        log_action(m.from_user, "Показать курс", result="⚠️ Ошибка API")
        return

    timestamp = now_msk().strftime("%d.%m.%Y, %H:%M")
    text = (
        f"{LANGS[l]['rate_title']}\n\n"
        f"🇰🇷 <b>USDT → KRW</b>\n◾ UPBIT: <b>{fmt_num(u,0)} ₩</b>\n◾ BITHUMB: <b>{fmt_num(b,0)} ₩</b>\n━━━━━━━━━━━━━━\n\n"
        f"🇷🇺 <b>USDT → RUB (ABCEX)</b>\n◾ Покупка: <b>{fmt_num(ab_buy,2)} ₽</b>\n◾ Продажа: <b>{fmt_num(ab_sell,2)} ₽</b>\n━━━━━━━━━━━━━━\n\n"
        f"🇰🇷➡️🇷🇺 <b>KRW → RUB</b>\n◾ 1 000 000 ₩ → <b>{fmt_num(r,2)} ₽</b>\n━━━━━━━━━━━━━━\n"
        f"⏱ Обновлено: <b>{timestamp}</b>\n\n{LANGS[l]['contact']}"
    )
    bot.edit_message_text(text, m.chat.id, msg.message_id, parse_mode="HTML")
    
    res_log = f"Upbit {fmt_num(u,0)} ₩ | ABCEX {fmt_num(ab_buy,2)} ₽"
    log_action(m.from_user, "Показать курс", result=res_log)
    
    USER_DATA[m.from_user.id]["requests"] += 1
    USER_DATA[m.from_user.id]["last"] = now_msk()

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_profile'], LANGS['en']['btn_profile']])
def show_profile(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    d = USER_DATA[m.from_user.id]
    
    last = d["last"].strftime("%d.%m.%Y %H:%M") if d["last"] else "—"
    join = d["joined"].strftime("%d.%m.%Y")
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
    bot.send_message(m.chat.id, txt, parse_mode="HTML", reply_markup=main_keyboard(m.from_user.id))
    log_action(m.from_user, "Профиль")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_feedback'], LANGS['en']['btn_feedback']])
def feedback_start(m):
    init_user(m.from_user)
    l = USER_DATA[m.from_user.id]['lang']
    msg = bot.send_message(m.chat.id, LANGS[l]['feedback_prompt'], reply_markup=types.ForceReply())
    bot.register_next_step_handler(msg, feedback_save)
    log_action(m.from_user, "Нажал кнопку 'Отзыв'")

def feedback_save(m):
    l = USER_DATA[m.from_user.id]['lang']
    if m.text:
        name = m.from_user.first_name
        if m.from_user.last_name: name += f" {m.from_user.last_name}"
        nick = f"@{m.from_user.username}" if m.from_user.username else "Нет"
        log_text = (
            f"🔴 <b>НОВЫЙ ОТЗЫВ</b>\n"
            f"👤 Имя: {name}\n"
            f"🔗 Ник / ID: {nick} | <code>{m.from_user.id}</code>\n"
            f"💬 Текст: <i>{m.text}</i>\n"
            f"🕒 Время: {now_msk().strftime('%H:%M:%S')} МСК"
        )
        bot.send_message(ADMIN_LOG_CHAT_ID, log_text, parse_mode="HTML")
        bot.send_message(m.chat.id, LANGS[l]['feedback_thanks'], reply_markup=main_keyboard(m.from_user.id))

# ============== АДМИНКА ==============
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_admin'], LANGS['en']['btn_admin']] and m.from_user.id == MY_ADMIN_ID)
def admin_panel(m):
    init_user(m.from_user)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="adm_stat"),
        types.InlineKeyboardButton("👥 База пользователей", callback_data="adm_users"),
        types.InlineKeyboardButton("📢 Рассылка всем", callback_data="adm_bc"),
        types.InlineKeyboardButton("✉️ Сообщение юзеру", callback_data="adm_pm")
    )
    bot.send_message(m.chat.id, "🛠 <b>Админ-панель</b>", reply_markup=kb)
    log_action(m.from_user, "Открыл Админ-панель")

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def admin_cb(c):
    if c.from_user.id != MY_ADMIN_ID: return
    action = c.data.split("_")[1]
    
    if action == "stat":
        reqs = sum(u['requests'] for u in USER_DATA.values())
        txt = f"📊 <b>Статистика</b>\n\nЮзеров в сессии: {len(ALL_USER_IDS)}\nАктивных подписок: {len(AUTO_USERS)}\nВсего запросов: {reqs}"
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, txt)
        
    elif action == "users":
        txt = "👥 <b>Пользователи за сессию:</b>\n\n"
        for uid, d in USER_DATA.items():
            nick = f"@{d['username']}" if d['username'] else d['first_name']
            txt += f"• <code>{uid}</code> | {nick}\n"
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, txt[:4000])
        
    elif action == "bc":
        msg = bot.send_message(c.message.chat.id, "Введите текст рассылки:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, do_bc)
        
    elif action == "pm":
        msg = bot.send_message(c.message.chat.id, "Введите ID юзера:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, do_pm_id)

def do_bc(m):
    count = 0
    for uid in ALL_USER_IDS:
        try:
            bot.send_message(uid, f"📢 <b>УВЕДОМЛЕНИЕ:</b>\n\n{m.text}")
            count += 1
        except: pass
    bot.send_message(m.chat.id, f"✅ Отправлено {count} чел.")
    log_action(m.from_user, "Рассылка всем", result=f"Успешно для {count} чел.")

def do_pm_id(m):
    try:
        tid = int(m.text.strip())
        msg = bot.send_message(m.chat.id, f"Введите сообщение для {tid}:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, lambda s: do_pm_send(s, tid))
    except: bot.send_message(m.chat.id, "❌ Ошибка ID.")

def do_pm_send(m, tid):
    try:
        bot.send_message(tid, f"✉️ <b>СООБЩЕНИЕ ОТ АДМИНА:</b>\n\n{m.text}")
        bot.send_message(m.chat.id, "✅ Доставлено.")
        log_action(m.from_user, f"ЛС для {tid}", result="Успешно")
    except: 
        bot.send_message(m.chat.id, "❌ Ошибка отправки.")

# ============== НАСТРОЙКА УВЕДОМЛЕНИЙ ==============
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_auto'], LANGS['en']['btn_auto']])
def toggle_auto(m):
    init_user(m.from_user); l = USER_DATA[m.from_user.id]['lang']
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("1H", callback_data="auto_3600"),
           types.InlineKeyboardButton("5H", callback_data="auto_18000"),
           types.InlineKeyboardButton("24H", callback_data="auto_86400"))
    if m.chat.id in AUTO_USERS: kb.row(types.InlineKeyboardButton("🚫 OFF", callback_data="auto_0"))
    bot.send_message(m.chat.id, LANGS[l]['auto_menu'], reply_markup=kb)
    log_action(m.from_user, "Открыл меню Автообновления")

@bot.callback_query_handler(func=lambda c: c.data.startswith("auto_"))
def auto_callback(c):
    init_user(c.from_user); l = USER_DATA[c.from_user.id]['lang']
    val = int(c.data.split("_")[1])
    if val == 0:
        AUTO_USERS.pop(c.message.chat.id, None)
        bot.edit_message_text(LANGS[l]['auto_off_msg'], c.message.chat.id, c.message.message_id)
        log_action(c.from_user, "Автообновление", result="Отключено")
    else:
        AUTO_USERS[c.message.chat.id] = {"interval": val, "last": now_msk()}
        bot.edit_message_text(f"{LANGS[l]['auto_on_msg']} {val//3600}H.", c.message.chat.id, c.message.message_id)
        log_action(c.from_user, "Автообновление", result=f"Включено ({val//3600}H)")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_disable'], LANGS['en']['btn_disable']])
def disable_notifications(m):
    init_user(m.from_user); l = USER_DATA[m.from_user.id]['lang']
    if m.chat.id in AUTO_USERS:
        AUTO_USERS.pop(m.chat.id, None)
        bot.send_message(m.chat.id, LANGS[l]['auto_off_msg'])
        log_action(m.from_user, "Отключил уведомления через меню")

@bot.message_handler(func=lambda m: True)
def auto_update_kb(m):
    init_user(m.from_user); l = USER_DATA[m.from_user.id]['lang']
    bot.send_message(m.chat.id, LANGS[l]['menu_updated'], reply_markup=main_keyboard(m.from_user.id))

# ============== ФОН И ЗАПУСК ==============
def keep_awake():
    url = "https://telegram-rate-bot-ooc6.onrender.com"
    while True:
        try: requests.get(url, timeout=5)
        except: pass
        time.sleep(600)

def auto_worker():
    while True:
        time.sleep(60)
        now = now_msk()
        for cid, cfg in list(AUTO_USERS.items()):
            if (now - cfg["last"]).total_seconds() >= cfg["interval"]:
                try:
                    # ПОЛУЧАЕМ ВСЕ ДАННЫЕ ВКЛЮЧАЯ КУРС РУБЛЯ
                    u, b, r, (ab_b, ab_s) = fetch_all_rates()
                    l = USER_DATA.get(cid, {}).get("lang", "ru")
                    
                    if l == 'ru':
                        text = (f"🔔 <b>АВТО-КУРС</b>\n\n"
                                f"🇰🇷 Upbit: {fmt_num(u,0)} ₩\n"
                                f"🇷🇺 ABCEX: {fmt_num(ab_b,2)} ₽\n"
                                f"🔄 1М ₩ ≈ <b>{fmt_num(r,2)} ₽</b>")
                    else:
                        text = (f"🔔 <b>AUTO-RATE</b>\n\n"
                                f"🇰🇷 Upbit: {fmt_num(u,0)} ₩\n"
                                f"🇷🇺 ABCEX: {fmt_num(ab_b,2)} ₽\n"
                                f"🔄 1M ₩ ≈ <b>{fmt_num(r,2)} ₽</b>")
                    
                    bot.send_message(cid, text, parse_mode="HTML")
                    AUTO_USERS[cid]["last"] = now
                except: pass

app = Flask(__name__)
@app.route('/')
def home(): return "Бот работает и не спит!", 200

if __name__ == "__main__":
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception as e:
        logger.error(f"Ошибка удаления вебхука: {e}")

    threading.Thread(target=auto_worker, daemon=True).start()
    threading.Thread(target=keep_awake, daemon=True).start()
    
    port = int(os.environ.get("PORT", 10000))
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False), daemon=True).start()
    
    logger.info("Бот запущен...")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60)
        except ApiTelegramException as e:
            logger.error(f"Ошибка API: {e}")
            time.sleep(10)
        except Exception as e:
            logger.error(f"Критическая ошибка: {e}")
            time.sleep(10)
