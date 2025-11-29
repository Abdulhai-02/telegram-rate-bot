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

ADMIN_LOG_CHAT_ID = -1003264764082
MOSCOW_TZ = timezone(timedelta(hours=3))

BTN_SHOW = "📊 Показать курс"
BTN_AUTO = "🔔 Автообновление"
BTN_PROFILE = "👤 Профиль"
BTN_DISABLE = "🚫 Отключить уведомления"

AUTO_INTERVAL_1H = 60 * 60
AUTO_INTERVAL_5H = 5 * 60 * 60
AUTO_INTERVAL_24H = 24 * 60 * 60

AUTO_USERS = {}
USER_STATS = defaultdict(lambda: {"requests": 0, "last": None})

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def now_msk():
    return datetime.now(MOSCOW_TZ)

def fmt_num(v, d=2):
    return f"{v:,.{d}f}".replace(",", " ")

def log_to_channel(text):
    try:
        bot.send_message(ADMIN_LOG_CHAT_ID, text)
    except:
        pass

def update_user_stats(user):
    USER_STATS[user.id]["requests"] += 1
    USER_STATS[user.id]["last"] = now_msk()

def log_user_action(user, action):
    try:
        log_to_channel(
            f"👤 @{user.username or 'без_username'} (ID {user.id})\n"
            f"🕒 {now_msk().strftime('%d.%m.%Y %H:%M:%S')} МСК\n➡️ {action}"
        )
    except:
        pass

def human_interval(s):
    if s == AUTO_INTERVAL_1H: return "каждый 1 час"
    if s == AUTO_INTERVAL_5H: return "каждые 5 часов"
    if s == AUTO_INTERVAL_24H: return "каждые 24 часа"
    return f"каждые {s//3600} ч."

# ============== API ==============

def get_upbit_usdt_krw():
    cache = getattr(get_upbit_usdt_krw, "_cache", None)
    try:
        r = requests.get(
            "https://api.upbit.com/v1/ticker",
            params={"markets": "KRW-USDT"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=4,
        )
        r.raise_for_status()
        data = r.json()
        price = float(data[0]["trade_price"])
        get_upbit_usdt_krw._cache = price
        return price
    except:
        return cache

def get_bithumb_usdt_krw():
    cache = getattr(get_bithumb_usdt_krw, "_cache", None)
    try:
        r = requests.get(
            "https://api.bithumb.com/public/ticker/USDT_KRW",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=4,
        )
        data = r.json()
        price = float(data["data"]["closing_price"])
        get_bithumb_usdt_krw._cache = price
        return price
    except:
        return cache
        def get_krw_rub_from_google():
    cache = getattr(get_krw_rub_from_google, "_cache", None)
    last = getattr(get_krw_rub_from_google, "_last", 0)

    if cache and time.time() - last < 1800:
        return cache

    try:
        r = requests.get(
            "https://www.google.com/finance/quote/RUB-KRW?hl=en",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5
        )
        soup = BeautifulSoup(r.text, "html.parser")
        div = soup.find("div", class_="YMlKec fxKbKc")
        if div:
            v = float(div.text.replace(",", "").replace("₩", ""))
            million = 1_000_000 / v
            get_krw_rub_from_google._cache = million
            get_krw_rub_from_google._last = time.time()
            return million
    except:
        pass

    try:
        r = requests.get("https://open.er-api.com/v6/latest/RUB", timeout=5)
        data = r.json()
        if "KRW" in data["rates"]:
            krw_per_rub = data["rates"]["KRW"]
            million = 1_000_000 / krw_per_rub
            get_krw_rub_from_google._cache = million
            get_krw_rub_from_google._last = time.time()
            return million
    except:
        pass

    return cache


def get_abcex_usdt_rub():
    cache = getattr(get_abcex_usdt_rub, "_cache", None)
    last = getattr(get_abcex_usdt_rub, "_last", 0)

    if cache and time.time() - last < 15:
        return cache

    try:
        r = requests.get(
            "https://hub.abcex.io/api/v2/exchange/public/orderbook/depth",
            params={"instrumentCode": "USDTRUB", "lang": "ru"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=4,
        )
        data = r.json()
        asks = data.get("ask") or []
        bids = data.get("bid") or []
        best_sell = float(asks[0]["price"])
        best_buy = float(bids[0]["price"])
        result = (best_buy, best_sell)
        get_abcex_usdt_rub._cache = result
        get_abcex_usdt_rub._last = time.time()
        return result
    except:
        return cache or (None, None)


# ============== ТЕКСТ КУРСА ==============
def build_rate_text(upbit, bithumb, rub, ab_buy=None, ab_sell=None):
    upbit_txt   = f"{fmt_num(upbit, 0)} ₩" if upbit else "—"
    bithumb_txt = f"{fmt_num(bithumb, 0)} ₩" if bithumb else "—"
    rub_txt     = f"{fmt_num(rub, 2)} ₽" if rub else "—"

    ab_buy_txt  = f"{fmt_num(ab_buy, 2)} ₽" if ab_buy else "—"
    ab_sell_txt = f"{fmt_num(ab_sell, 2)} ₽" if ab_sell else "—"

    timestamp = now_msk().strftime("%d.%m.%Y, %H:%M")

    text = (
        "💱 <b>АКТУАЛЬНЫЕ КУРСЫ</b>\n\n"

        "🇰🇷 <b>USDT → KRW</b>\n"
        f"◾ UPBIT:      <b>{upbit_txt}</b>\n"
        f"◾ BITHUMB:    <b>{bithumb_txt}</b>\n"
        "━━━━━━━━━━━━━━\n\n"

        "🇷🇺 <b>USDT → RUB (ABCEX)</b>\n"
        f"◾ Покупка:    <b>{ab_buy_txt}</b>\n"
        f"◾ Продажа:    <b>{ab_sell_txt}</b>\n"
        "━━━━━━━━━━━━━━\n\n"

        "🇰🇷➡️🇷🇺 <b>KRW → RUB</b>\n"
        f"◾ 1 000 000 ₩ → <b>{rub_txt}</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"⏱ Обновлено: <b>{timestamp} (МСК)</b>\n\n"

        "💰 Обмен любых сумм и валют — по договоренности.\n"
        "📞 Контакт: @Abdulkhaiii"
    )
    return text


# ============== АВТО-ОБНОВЛЕНИЕ ==============
def auto_update_loop():
    while True:
        time.sleep(60)
        if not AUTO_USERS:
            continue

        try:
            now = now_msk()
            if now.hour < 8 or now.hour >= 23:
                continue

            with concurrent.futures.ThreadPoolExecutor() as ex:
                fu_u = ex.submit(get_upbit_usdt_krw)
                fu_b = ex.submit(get_bithumb_usdt_krw)
                fu_r = ex.submit(get_krw_rub_from_google)
                fu_ab = ex.submit(get_abcex_usdt_rub)

                u = fu_u.result()
                b = fu_b.result()
                r = fu_r.result()
                ab_buy, ab_sell = fu_ab.result()

            if not any([u, b, r, ab_buy, ab_sell]):
                continue

            txt = build_rate_text(u, b, r, ab_buy, ab_sell)

            for chat_id, cfg in list(AUTO_USERS.items()):
                interval = cfg["interval"]
                last = cfg["last"]

                if last and (now - last).total_seconds() < interval:
                    continue

                try:
                    bot.send_message(chat_id, txt)
                    AUTO_USERS[chat_id]["last"] = now
                except Exception as e:
                    if "blocked" in str(e).lower():
                        AUTO_USERS.pop(chat_id, None)

            log_to_channel(
                f"⏱ Автообновление ({len(AUTO_USERS)} пользователей) – "
                f"{now.strftime('%H:%M:%S')}"
            )

        except Exception:
            logger.exception("Ошибка автообновления")
            # ============== КНОПКИ ==============
def main_keyboard():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row(BTN_SHOW, BTN_AUTO)
    m.row(BTN_PROFILE, BTN_DISABLE)
    return m


def ensure_keyboard(m):
    """Гарантированно отправляет новую клавиатуру старым пользователям"""
    try:
        bot.send_message(m.chat.id, " ", reply_markup=main_keyboard())
    except:
        pass


# ============== /START ==============
@bot.message_handler(commands=["start", "help"])
def start_handler(m):
    ensure_keyboard(m)
    bot.send_message(
        m.chat.id,
        "👋 Привет!\n\nВыбери нужный раздел ниже 👇",
        reply_markup=main_keyboard()
    )
    log_user_action(m.from_user, "нажал /start")


# ============== ОБНОВЛЕНИЕ КНОПОК ДЛЯ ВСЕХ ==============
@bot.message_handler(func=lambda m: True)
def update_keyboard_global(m):
    """Каждое сообщение обновляет клавиатуру (старые пользователи получат новые кнопки)"""
    ensure_keyboard(m)


# ============== ОТКЛЮЧЕНИЕ УВЕДОМЛЕНИЙ ==============
@bot.message_handler(func=lambda m: m.text == BTN_DISABLE)
def disable_notifications(m):
    cid = m.chat.id
    if cid in AUTO_USERS:
        AUTO_USERS.pop(cid, None)
        bot.send_message(cid, "🔕 Уведомления отключены.")
        log_user_action(m.from_user, "отключил уведомления")
    else:
        bot.send_message(cid, "Уведомления уже выключены.")


# ============== ПОКАЗ КУРСА ==============
@bot.message_handler(func=lambda m: m.text == BTN_SHOW)
def show_rate(m):
    ensure_keyboard(m)
    log_user_action(m.from_user, "нажал «Показать курс»")
    cid = m.chat.id

    msg = bot.send_message(cid, "⏳ Загрузка курса, ожидайте...")

    stop = {"run": True}

    def anim():
        dots = [".", "..", "..."]
        i = 0
        while stop["run"]:
            try:
                bot.edit_message_text(
                    f"⏳ Загрузка курса{dots[i % 3]}...",
                    cid, msg.message_id
                )
            except:
                break
            i += 1
            time.sleep(0.6)

    threading.Thread(target=anim, daemon=True).start()

    with concurrent.futures.ThreadPoolExecutor() as ex:
        fu_u = ex.submit(get_upbit_usdt_krw)
        fu_b = ex.submit(get_bithumb_usdt_krw)
        fu_r = ex.submit(get_krw_rub_from_google)
        fu_ab = ex.submit(get_abcex_usdt_rub)

        u = fu_u.result()
        b = fu_b.result()
        r = fu_r.result()
        ab_buy, ab_sell = fu_ab.result()

    stop["run"] = False
    time.sleep(0.4)

    if not any([u, b, r, ab_buy, ab_sell]):
        bot.edit_message_text(
            "⚠️ Не удалось получить курс.\nПопробуйте позже.",
            cid,
            msg.message_id
        )
        return

    txt = build_rate_text(u, b, r, ab_buy, ab_sell)

    bot.edit_message_text(txt, cid, msg.message_id, parse_mode="HTML")
    update_user_stats(m.from_user)

    log_to_channel(
        f"📊 Курс @{m.from_user.username or 'без_username'} ({m.from_user.id})\n"
        f"🕒 {now_msk().strftime('%H:%M:%S')} МСК\n"
        f"Upbit: {fmt_num(u, 0) if u else '—'} | "
        f"Bithumb: {fmt_num(b, 0) if b else '—'} | "
        f"Google: {fmt_num(r, 2) if r else '—'} ₽ | "
        f"ABCEX buy/sell: "
        f"{fmt_num(ab_buy, 2) if ab_buy else '—'} / {fmt_num(ab_sell, 2) if ab_sell else '—'} ₽"
    )


# ============== АВТО-ОБНОВЛЕНИЕ НАСТРОЕК ==============
@bot.message_handler(func=lambda m: m.text == BTN_AUTO)
def toggle_auto(m):
    ensure_keyboard(m)
    cid = m.chat.id

    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("⏱ Каждый 1 час", callback_data="auto_1h"),
        types.InlineKeyboardButton("⏱ Каждые 5 часов", callback_data="auto_5h"),
    )
    kb.row(
        types.InlineKeyboardButton("🕛 Раз в 24 часа (с 08:00 МСК)", callback_data="auto_24h"),
    )
    if cid in AUTO_USERS:
        kb.row(
            types.InlineKeyboardButton("🔕 Выключить автообновление", callback_data="auto_off")
        )

    text = "Выбери частоту автообновления курса:"
    if cid in AUTO_USERS:
        cur_int = AUTO_USERS[cid].get("interval", AUTO_INTERVAL_24H)
        text += f"\nСейчас: {human_interval(cur_int)}."

    bot.send_message(cid, text, reply_markup=kb)
    log_user_action(m.from_user, "открыл настройки автообновления")


@bot.callback_query_handler(func=lambda c: c.data.startswith("auto_"))
def auto_callback(c):
    cid = c.message.chat.id
    data = c.data

    if data == "auto_off":
        AUTO_USERS.pop(cid, None)
        bot.answer_callback_query(c.id, "Автообновление выключено")
        bot.send_message(cid, "🔕 Автообновление выключено.")
        log_user_action(c.from_user, "выключил автообновление")
        return

    now = now_msk()

    if data == "auto_1h":
        interval = AUTO_INTERVAL_1H
        label = "каждый 1 час"
        last = now
    elif data == "auto_5h":
        interval = AUTO_INTERVAL_5H
        label = "каждые 5 часов"
        last = now
    else:
        interval = AUTO_INTERVAL_24H
        label = "каждые 24 часа"
        next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now.hour >= 8:
            next_run += timedelta(days=1)
        last = next_run - timedelta(seconds=interval)

    AUTO_USERS[cid] = {"interval": interval, "last": last}
    bot.answer_callback_query(c.id, "Настройки сохранены")
    bot.send_message(cid, f"🔔 Автообновление включено: {label}.")
    log_user_action(c.from_user, f"включил автообновление ({label})")


# ============== ПРОФИЛЬ ==============
@bot.message_handler(func=lambda m: m.text == BTN_PROFILE)
def profile(m):
    ensure_keyboard(m)
    s = USER_STATS[m.from_user.id]
    last = s["last"].strftime("%d.%m.%Y %H:%M:%S") if s["last"] else "—"

    if m.from_user.username:
        nick = f"@{m.from_user.username}"
    else:
        full_name = " ".join(filter(None, [m.from_user.first_name, m.from_user.last_name]))
        nick = full_name or "без имени"

    txt = (
        f"👤 <b>Профиль</b>\n\n"
        f"Ник: {nick}\n"
        f"ID: <code>{m.from_user.id}</code>\n\n"
        f"Запросов курса: {s['requests']}\n"
        f"Последний запрос: {last} (МСК)"
    )
    bot.send_message(m.chat.id, txt)
    log_user_action(m.from_user, "открыл профиль")


# ============== АНТИ-СОН ДЛЯ RENDER ==============
def keep_awake():
    url = "https://telegram-rate-bot-ooc6.onrender.com"
    while True:
        try:
            requests.get(url, timeout=5)
            print(f"[keep_alive] Pinged {url}")
        except Exception as e:
            print(f"[keep_alive] Ошибка пинга: {e}")
        time.sleep(600)


# ============== FAKE WEB SERVER ==============
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running OK", 200

def run_web():
    port = int(os.environ.get("PORT", 10000))
    print(f"[web] Using PORT={port}")
    app.run(host="0.0.0.0", port=port)


# ============== ЗАПУСК БОТА ==============
def main():
    threading.Thread(target=auto_update_loop, daemon=True).start()
    threading.Thread(target=keep_awake, daemon=True).start()
    threading.Thread(target=run_web, daemon=True).start()

    logger.info("Бот запущен.")
    log_to_channel("🚀 Бот перезапущен и готов к работе")

    while True:
        try:
            bot.infinity_polling(skip_pending=True)
        except ApiTelegramException as e:
            if "409" in str(e):
                logger.error("⚠️ 409 Conflict, пробуем снова через 10 сек")
                time.sleep(10)
                continue
            logger.exception("ApiTelegramException, пауза 15 сек")
            time.sleep(15)
        except Exception:
            logger.exception("Ошибка в polling, пауза 15 сек")
            time.sleep(15)


if __name__ == "__main__":
    try:
        try:
            bot.send_message(ADMIN_LOG_CHAT_ID, "♻️ Бот успешно перезапущен и готов к работе!")
        except Exception as e:
            print(f"Ошибка отправки админу: {e}")
        main()
    except Exception as e:
        logging.exception("❌ Фатальная ошибка")
        try:
            bot.send_message(ADMIN_LOG_CHAT_ID, f"⚠️ Ошибка запуска:\n{e}")
        except:
            pass