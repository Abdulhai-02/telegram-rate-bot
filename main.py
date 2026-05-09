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

MY_ADMIN_ID = 5143360493  
ADMIN_LOG_CHAT_ID = -1003264764082
MOSCOW_TZ = timezone(timedelta(hours=3))

logging.basicConfig(level=logging.INFO)
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
        logger.info("✅ Связь с MongoDB Atlas установлена!")
    except Exception as e:
        logger.error(f"❌ ОШИБКА MONGODB: {e}")
        users_collection = None
        auto_collection = None
else:
    users_collection = None
    auto_collection = None

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
        'error_fetch': "⚠️ Не удалось получить курс.",
        'rate_title': "💱 <b>АКТУАЛЬНЫЕ КУРСЫ</b>",
        'updated': "⏱ Обновлено:",
        'contact': "💰 Обмен любых сумм — @Abdulkhaiii",
        'auto_menu': "Выбери частоту автообновления:",
        'auto_off_msg': "Автообновление выключено.",
        'auto_on_msg': "Включено уведомление:",
        'feedback_prompt': "Напишите ваш отзыв одним сообщением:",
        'feedback_thanks': "✅ Спасибо! Отзыв передан.",
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
        'error_fetch': "⚠️ Failed to get rates.",
        'rate_title': "💱 <b>CURRENT RATES</b>",
        'updated': "⏱ Updated:",
        'contact': "💰 Exchange — @Abdulkhaiii",
        'feedback_prompt': "Write your feedback:",
        'feedback_thanks': "✅ Thank you!",
        'auto_menu': "Select update frequency:",
        'auto_off_msg': "Auto-updates disabled.",
        'auto_on_msg': "Alerts enabled:",
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

# ============== СИНХРОНИЗАЦИЯ ==============
def load_db():
    if users_collection is None: return
    try:
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
                "last": datetime.fromisoformat(doc["last"]) if doc.get("last") else None
            }
        logger.info(f"✅ Данные загружены. Пользователей: {len(USER_DATA)}")
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки БД: {e}")

def save_user(uid):
    if users_collection is None or uid not in USER_DATA: return
    try:
        u = USER_DATA[uid]
        doc = {
            "lang": u["lang"], "requests": u["requests"],
            "last": u["last"].isoformat() if u["last"] else None,
            "joined": u["joined"].isoformat() if u["joined"] else None,
            "first_name": u["first_name"], "username": u["username"]
        }
        users_collection.update_one({"_id": uid}, {"$set": doc}, upsert=True)
    except: pass

def save_auto(uid):
    if auto_collection is None: return
    try:
        if uid in AUTO_USERS:
            a = AUTO_USERS[uid]
            doc = {"interval": a["interval"], "last": a["last"].isoformat() if a["last"] else None}
            auto_collection.update_one({"_id": uid}, {"$set": doc}, upsert=True)
        else:
            auto_collection.delete_one({"_id": uid})
    except: pass

def now_msk(): return datetime.now(MOSCOW_TZ)

def fmt_num(v, d=2):
    if v is None: return "—"
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
                        "first_name": doc.get("first_name"), "username": doc.get("username")
                    }
                    return 
            except: pass
        USER_DATA[uid] = {
            "lang": "ru", "requests": 0, "last": None,
            "joined": now_msk(), "first_name": user.first_name, "username": user.username
        }
        save_user(uid)

def log_action(user, action, result=None):
    try:
        name = user.first_name
        nick = f"@{user.username}" if user.username else f"ID: {user.id}"
        log_text = (f"⚙️ <b>Лог</b>\n👤 {name} ({nick})\n🔘 {action}")
        if result: log_text += f"\n📊 {result}"
        bot.send_message(ADMIN_LOG_CHAT_ID, log_text, parse_mode="HTML")
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
def fetch_all_rates():
    headers = {"User-Agent": "Mozilla/5.0"}
    def g_u():
        try: return float(requests.get("https://api.upbit.com/v1/ticker?markets=KRW-USDT", timeout=5).json()[0]["trade_price"])
        except: return None
    def g_b():
        try: return float(requests.get("https://api.bithumb.com/public/ticker/USDT_KRW", timeout=5).json()["data"]["closing_price"])
        except: return None
    def g_r():
        try: return 1_000_000 / requests.get("https://open.er-api.com/v6/latest/RUB", timeout=5).json()["rates"]["KRW"]
        except: return None
    def g_ab():
        try:
            d = requests.get("https://hub.abcex.io/api/v2/exchange/public/orderbook/depth?instrumentCode=USDTRUB", timeout=5).json()
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

@bot.callback_query_handler(func=lambda c: c.data.startswith("lang_"))
def lang_set(c):
    l = c.data.split("_")[1]
    init_user(c.from_user)
    USER_DATA[c.from_user.id]['lang'] = l
    save_user(c.from_user.id)
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
    text = (
        f"{LANGS[l]['rate_title']}\n\n"
        f"🇰🇷 <b>USDT → KRW</b>\n◾ UPBIT: <b>{fmt_num(u,0)} ₩</b>\n◾ BITHUMB: <b>{fmt_num(b,0)} ₩</b>\n\n"
        f"🇷🇺 <b>USDT → RUB (ABCEX)</b>\n◾ Покупка: <b>{fmt_num(ab_buy,2)} ₽</b>\n\n"
        f"🇰🇷➡️🇷🇺 <b>KRW → RUB</b>\n◾ 1 000 000 ₩ → <b>{fmt_num(r,2)} ₽</b>\n\n"
        f"⏱ Обновлено: <b>{now_msk().strftime('%H:%M')}</b>\n{LANGS[l]['contact']}"
    )
    bot.edit_message_text(text, m.chat.id, msg.message_id, parse_mode="HTML")
    USER_DATA[m.from_user.id]["requests"] += 1
    USER_DATA[m.from_user.id]["last"] = now_msk()
    save_user(m.from_user.id)

# ============== АДМИНКА ==============
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_admin'], LANGS['en']['btn_admin']] and m.from_user.id == MY_ADMIN_ID)
def admin_panel(m):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📊 Статистика", callback_data="adm_stat"),
        types.InlineKeyboardButton("👥 Список юзеров", callback_data="adm_users"),
        types.InlineKeyboardButton("📢 Рассылка всем", callback_data="adm_bc"),
        types.InlineKeyboardButton("🔔 Упр. подписками", callback_data="adm_auto_menu")
    )
    bot.send_message(m.chat.id, "🛠 <b>Админ-панель</b>", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def admin_cb(c):
    if c.from_user.id != MY_ADMIN_ID: return
    action = c.data[4:] # Получаем действие после 'adm_'
    
    if action == "stat":
        reqs = sum(u['requests'] for u in USER_DATA.values())
        txt = f"📊 <b>Стат</b>\nЮзеров: {len(ALL_USER_IDS)}\nАвто: {len(AUTO_USERS)}\nЗапросов: {reqs}"
        bot.send_message(c.message.chat.id, txt)
        
    elif action == "auto_menu":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Включить всем", callback_data="adm_auto_all"))
        kb.add(types.InlineKeyboardButton("👤 Подключить по ID", callback_data="adm_auto_id"))
        bot.edit_message_text("Управление подписками:", c.message.chat.id, c.message.message_id, reply_markup=kb)

    elif action == "auto_all":
        kb = types.InlineKeyboardMarkup()
        kb.row(types.InlineKeyboardButton("1H", callback_data="adm_all_3600"),
               types.InlineKeyboardButton("5H", callback_data="adm_all_18000"),
               types.InlineKeyboardButton("24H", callback_data="adm_all_86400"))
        bot.edit_message_text("⏳ Выберите интервал для ВСЕХ пользователей:", c.message.chat.id, c.message.message_id, reply_markup=kb)

    elif action.startswith("all_"):
        val = int(action.split("_")[1])
        hours = val // 3600
        count = 0
        for uid in ALL_USER_IDS:
            try:
                AUTO_USERS[uid] = {"interval": val, "last": now_msk()}
                save_auto(uid)
                count += 1
            except: pass
        bot.edit_message_text(f"✅ <b>Успешно!</b> Подписка ({hours}ч.) активирована для {count} юзеров.", c.message.chat.id, c.message.message_id, parse_mode="HTML")

    elif action == "auto_id":
        msg = bot.send_message(c.message.chat.id, "Введите ID пользователя:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, adm_auto_step_id)

    elif action == "users":
        txt = "👥 <b>Юзеры:</b>\n"
        for uid, d in list(USER_DATA.items())[-20:]:
            nick = f"@{d['username']}" if d['username'] else d['first_name']
            txt += f"• <code>{uid}</code> | {nick}\n"
        bot.send_message(c.message.chat.id, txt)

    elif action == "bc":
        msg = bot.send_message(c.message.chat.id, "Текст рассылки:", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, do_bc)

def adm_auto_step_id(m):
    try:
        target_id = int(m.text.strip())
        msg = bot.send_message(m.chat.id, "Введите интервал в часах (например, 1, 5 или 24):", reply_markup=types.ForceReply())
        bot.register_next_step_handler(msg, lambda s: adm_auto_final(s, target_id))
    except: bot.send_message(m.chat.id, "❌ Ошибка ID.")

def adm_auto_final(m, tid):
    try:
        hours = int(m.text.strip())
        AUTO_USERS[tid] = {"interval": hours * 3600, "last": now_msk()}
        save_auto(tid)
        bot.send_message(m.chat.id, f"✅ <b>Успешно!</b> Для {tid} включено обновление каждые {hours}ч.")
    except: bot.send_message(m.chat.id, "❌ Ошибка. Проверьте ID и интервал.")

def do_bc(m):
    count = 0
    for uid in ALL_USER_IDS:
        try:
            bot.send_message(uid, f"📢 <b>УВЕДОМЛЕНИЕ:</b>\n\n{m.text}")
            count += 1
        except: pass
    bot.send_message(m.chat.id, f"✅ Отправлено {count} чел.")

# ============== УВЕДОМЛЕНИЯ ==============
@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_auto'], LANGS['en']['btn_auto']])
def toggle_auto(m):
    init_user(m.from_user); l = USER_DATA[m.from_user.id]['lang']
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("1H", callback_data="auto_3600"),
           types.InlineKeyboardButton("5H", callback_data="auto_18000"),
           types.InlineKeyboardButton("24H", callback_data="auto_86400"))
    if m.chat.id in AUTO_USERS: kb.row(types.InlineKeyboardButton("🚫 OFF", callback_data="auto_0"))
    bot.send_message(m.chat.id, LANGS[l]['auto_menu'], reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("auto_"))
def auto_callback(c):
    init_user(c.from_user); l = USER_DATA[c.from_user.id]['lang']
    val = int(c.data.split("_")[1])
    if val == 0:
        AUTO_USERS.pop(c.message.chat.id, None); save_auto(c.message.chat.id)
        bot.edit_message_text(f"✅ <b>Успешно!</b> {LANGS[l]['auto_off_msg']}", c.message.chat.id, c.message.message_id, parse_mode="HTML")
    else:
        AUTO_USERS[c.message.chat.id] = {"interval": val, "last": now_msk()}; save_auto(c.message.chat.id)
        bot.edit_message_text(f"✅ <b>Успешно!</b> {LANGS[l]['auto_on_msg']} {val//3600}H.", c.message.chat.id, c.message.message_id, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_profile'], LANGS['en']['btn_profile']])
def show_profile(m):
    init_user(m.from_user); l = USER_DATA[m.from_user.id]['lang']; d = USER_DATA[m.from_user.id]
    txt = (f"👤 <b>Профиль</b>\nID: <code>{m.from_user.id}</code>\n{LANGS[l]['prof_reqs']} {d['requests']}")
    bot.send_message(m.chat.id, txt, reply_markup=main_keyboard(m.from_user.id))

@bot.message_handler(func=lambda m: m.text in [LANGS['ru']['btn_disable'], LANGS['en']['btn_disable']])
def disable_notif(m):
    AUTO_USERS.pop(m.chat.id, None); save_auto(m.chat.id)
    bot.send_message(m.chat.id, "✅ <b>Успешно!</b> 🔕 Выключено.", parse_mode="HTML")

@bot.message_handler(func=lambda m: True)
def any_msg(m):
    init_user(m.from_user); l = USER_DATA[m.from_user.id]['lang']
    bot.send_message(m.chat.id, LANGS[l]['menu_updated'], reply_markup=main_keyboard(m.from_user.id))

# ============== ФОН И ЗАПУСК ==============
def auto_worker():
    while True:
        time.sleep(60)
        now = now_msk()
        for cid, cfg in list(AUTO_USERS.items()):
            if (now - cfg["last"]).total_seconds() >= cfg["interval"]:
                try:
                    u, b, r, (ab_b, ab_s) = fetch_all_rates()
                    l = USER_DATA.get(cid, {}).get("lang", "ru")
                    text = (f"🔔 <b>АВТО-КУРС</b>\n\n"
                            f"🇰🇷 Upbit: {fmt_num(u,0)} ₩\n"
                            f"🇷🇺 ABCEX: {fmt_num(ab_b,2)} ₽\n"
                            f"🔄 1М ₩ ≈ <b>{fmt_num(r,2)} ₽</b>")
                    bot.send_message(cid, text, parse_mode="HTML")
                    AUTO_USERS[cid]["last"] = now; save_auto(cid)
                except: pass

app = Flask(__name__)
@app.route('/')
def home(): return "OK", 200

if __name__ == "__main__":
    try: bot.remove_webhook()
    except: pass
    load_db()
    threading.Thread(target=auto_worker, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True).start()
    while True:
        try: bot.infinity_polling(timeout=60)
        except: time.sleep(10)
