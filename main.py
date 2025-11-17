# -*- coding: utf-8 -*-
import os, logging, threading, time, re, concurrent.futures
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import requests
from dotenv import load_dotenv
import telebot
from telebot import types
from bs4 import BeautifulSoup

# ============== НАСТРОЙКИ ==============
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN в переменных окружения")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

ADMIN_LOG_CHAT_ID = -1003264764082
AUTO_INTERVAL_SECONDS = 60 * 60 * 24  # осталось как было (по сути = 24 часа)
MOSCOW_TZ = timezone(timedelta(hours=3))

BTN_SHOW = "📊 Показать курс"
BTN_AUTO = "🔔 Автообновление"
BTN_PROFILE = "👤 Профиль"

# автообновления теперь с настройкой частоты
AUTO_INTERVAL_1H = 60 * 60
AUTO_INTERVAL_5H = 5 * 60 * 60
AUTO_INTERVAL_24H = 24 * 60 * 60

# chat_id -> {"interval": seconds, "last": datetime}
AUTO_USERS = {}
USER_STATS = defaultdict(lambda: {"requests": 0, "last": None})

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============== УТИЛИТЫ ==============
def now_msk():
    return datetime.now(MOSCOW_TZ)

def fmt_num(v, d=2):
    return f"{v:,.{d}f}".replace(",", " ")

def log_to_channel(text):
    try:
        bot.send_message(ADMIN_LOG_CHAT_ID, text)
    except Exception:
        logger.exception("Ошибка логирования")

def update_user_stats(user):
    s = USER_STATS[user.id]
    s["requests"] += 1
    s["last"] = now_msk()

def log_user_action(user, action):
    log_to_channel(
        f"👤 @{user.username or 'без_username'} (ID {user.id})\n"
        f"🕒 {now_msk().strftime('%d.%m.%Y %H:%M:%S')} МСК\n➡️ {action}"
    )

def human_interval(seconds: int) -> str:
    if seconds == AUTO_INTERVAL_1H:
        return "каждый 1 час"
    if seconds == AUTO_INTERVAL_5H:
        return "каждые 5 часов"
    if seconds == AUTO_INTERVAL_24H:
        return "каждые 24 часа"
    hours = int(seconds // 3600)
    return f"каждые {hours} ч."

# ============== API ==============
def get_upbit_usdt_krw():
    cache = getattr(get_upbit_usdt_krw, "_cache", None)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://upbit.com/exchange?code=CRIX.UPBIT.KRW-USDT",
        }
        r = requests.get(
            "https://api.upbit.com/v1/ticker",
            params={"markets": "KRW-USDT"},
            headers=headers,
            timeout=4,
        )
        r.raise_for_status()
        data = r.json()
        price = float(data[0].get("trade_price", 0))
        get_upbit_usdt_krw._cache = price
        return price
    except Exception:
        return cache if cache else None

def get_bithumb_usdt_krw():
    cache = getattr(get_bithumb_usdt_krw, "_cache", None)
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(
            "https://api.bithumb.com/public/ticker/USDT_KRW",
            headers=headers,
            timeout=4,
        )
        r.raise_for_status()
        data = r.json()
        price = float(data["data"]["closing_price"])
        get_bithumb_usdt_krw._cache = price
        return price
    except Exception:
        return cache if cache else None

def get_krw_rub_from_google():
    cache = getattr(get_krw_rub_from_google, "_cache", None)
    last_time = getattr(get_krw_rub_from_google, "_last", 0)

    # Если есть кэш моложе 30 минут — возвращаем его
    if cache and (time.time() - last_time) < 1800:
        return cache

    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        url = "https://www.google.com/finance/quote/RUB-KRW?hl=en"
        r = requests.get(url, headers=headers, timeout=5)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        div = soup.find("div", class_="YMlKec fxKbKc")
        if div:
            val = float(div.text.replace(",", "").replace("₩", "").strip())
            rub_for_million = 1_000_000 / val
            get_krw_rub_from_google._cache = rub_for_million
            get_krw_rub_from_google._last = time.time()
            return rub_for_million
    except Exception:
        pass

    try:
        r = requests.get("https://open.er-api.com/v6/latest/RUB", timeout=5)
        data = r.json()
        if "KRW" in data["rates"]:
            krw_per_rub = data["rates"]["KRW"]
            rub_for_million = 1_000_000 / krw_per_rub
            get_krw_rub_from_google._cache = rub_for_million
            get_krw_rub_from_google._last = time.time()
            return rub_for_million
    except Exception:
        pass

    return cache if cache else None

def get_abcex_usdt_rub():
    """
    Возвращает (buy_price, sell_price) для ABCEX USDT/RUB.
    buy_price  = лучшая цена покупки USDT (bid)
    sell_price = лучшая цена продажи USDT (ask)
    """
    url = "https://hub.abcex.io/api/v2/exchange/public/orderbook/depth"
    params = {
        "instrumentCode": "USDTRUB",
        "lang": "ru"
    }

    cache = getattr(get_abcex_usdt_rub, "_cache", None)
    last_time = getattr(get_abcex_usdt_rub, "_last", 0)

    # немного кэша, чтобы не ддосить (20 секунд)
    if cache and time.time() - last_time < 20:
        return cache

    try:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Origin": "https://abcex.io",
            "Referer": "https://abcex.io/"
        }

        r = requests.get(url, params=params, headers=headers, timeout=5)
        r.raise_for_status()
        data = r.json()

        bids = data.get("data", {}).get("bids", [])
        asks = data.get("data", {}).get("asks", [])

        if not bids or not asks:
            return (None, None)

        best_buy = float(bids[0][0])   # покупка USDT за RUB
        best_sell = float(asks[0][0])  # продажа USDT за RUB

        result = (best_buy, best_sell)
        get_abcex_usdt_rub._cache = result
        get_abcex_usdt_rub._last = time.time()
        return result

    except Exception:
        logger.exception("Ошибка получения ABCEX USDT/RUB")
        return cache if cache else (None, None)

# ============== ТЕКСТ КУРСА ==============
def build_rate_text(upbit, bithumb, rub, ab_buy=None, ab_sell=None):
    upbit_txt   = f"<b>{fmt_num(upbit, 0)} ₩</b>" if upbit else "<b>—</b>"
    bithumb_txt = f"<b>{fmt_num(bithumb, 0)} ₩</b>" if bithumb else "<b>—</b>"
    rub_txt     = f"<b>{fmt_num(rub, 2)} ₽</b>" if rub else "<b>—</b>"

    body = (
        "💱 <b><u>АКТУАЛЬНЫЕ КУРСЫ</u></b>\n"
        "──────────────────────\n"
        f"🟢 <b>UPBIT</b>       1 USDT = {upbit_txt}\n"
        f"🟡 <b>BITHUMB</b>  1 USDT = {bithumb_txt}\n"
    )

    # блок ABCEX, если удалось взять курс
    if ab_buy and ab_sell:
        body += (
            f"🟣 <b>ABCEX</b>\n"
            f"   Покупка: <b>{fmt_num(ab_buy, 2)} ₽</b>\n"
            f"   Продажа: <b>{fmt_num(ab_sell, 2)} ₽</b>\n"
        )

    body += (
        "──────────────────────\n"
        f"🇰🇷➡️🇷🇺   <b>1 000 000 ₩ ≈ {rub_txt}</b> (Google Finance)\n"
        "──────────────────────\n"
    )

    timestamp = now_msk().strftime("%d.%m.%Y, %H:%M")
    footer = f"🔁 <b>Данные обновлены {timestamp} (МСК)</b>\n\n"

    contact = (
        "💰 <b>Обмен любых сумм и других валют — по предварительной договорённости.</b>\n\n"
        "📞 <b>Контакт для обмена:</b> @Abdulkhaiii"
    )

    return body + footer + contact

# ============== АВТООБНОВЛЕНИЕ ==============
def auto_update_loop():
    while True:
        # проверяем раз в минуту
        time.sleep(60)
        if not AUTO_USERS:
            continue
        try:
            now = now_msk()
            # отправляем только с 08:00 до 23:00 МСК
            if now.hour < 8 or now.hour >= 23:
                continue

            with concurrent.futures.ThreadPoolExecutor() as ex:
                fu_u = ex.submit(get_upbit_usdt_krw)
                fu_b = ex.submit(get_bithumb_usdt_krw)
                fu_r = ex.submit(get_krw_rub_from_google)
                fu_ab = ex.submit(get_abcex_usdt_rub)
                u, b, r = fu_u.result(), fu_b.result(), fu_r.result()
                ab_buy, ab_sell = fu_ab.result()

            if not any([u, b, r, ab_buy, ab_sell]):
                continue

            txt = build_rate_text(u, b, r, ab_buy=ab_buy, ab_sell=ab_sell)

            for chat_id, cfg in list(AUTO_USERS.items()):
                interval = cfg.get("interval", AUTO_INTERVAL_24H)
                last = cfg.get("last")

                if last and (now - last).total_seconds() < interval:
                    continue

                try:
                    bot.send_message(chat_id, txt)
                    AUTO_USERS[chat_id]["last"] = now
                except Exception as e:
                    logger.warning(f"Не удалось отправить автообновление {chat_id}: {e}")
                    # если пользователь удалился/заблокировал — удаляем из списка
                    if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
                        AUTO_USERS.pop(chat_id, None)

            log_to_channel(
                f"⏱ Автообновление {len(AUTO_USERS)} пользователей "
                f"{now.strftime('%H:%M:%S')}"
            )
        except Exception:
            logger.exception("Ошибка автообновления")

# ============== КНОПКИ ==============
def main_keyboard():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row(BTN_SHOW, BTN_AUTO)
    m.row(BTN_PROFILE)
    return m

@bot.message_handler(commands=["start","help"])
def start_handler(m):
    bot.send_message(m.chat.id, "👋 Привет!\n\nВыбери нужный раздел ниже 👇", reply_markup=main_keyboard())
    log_user_action(m.from_user, "нажал /start")

def ensure_keyboard(m):
    try:
        bot.send_message(m.chat.id, " ", reply_markup=main_keyboard())
    except:
        pass

# ============== ПОКАЗ КУРСА ==============
@bot.message_handler(func=lambda m: m.text == BTN_SHOW)
def show_rate(m):
    ensure_keyboard(m)
    log_user_action(m.from_user, "нажал «Показать курс»")
    chat_id = m.chat.id

    msg = bot.send_message(chat_id, "⏳ Загрузка курса, ожидайте пожалуйста...")

    stop = {"run": True}
    def animate():
        dots = [".", "..", "..."]
        i = 0
        while stop["run"]:
            try:
                bot.edit_message_text(f"⏳ Загрузка курса{dots[i%3]}\nОжидайте пожалуйста...", chat_id, msg.message_id)
            except:
                break
            i += 1
            time.sleep(0.6)
    threading.Thread(target=animate, daemon=True).start()

    # Параллельное получение курсов
    with concurrent.futures.ThreadPoolExecutor() as ex:
        fu_u = ex.submit(get_upbit_usdt_krw)
        fu_b = ex.submit(get_bithumb_usdt_krw)
        fu_r = ex.submit(get_krw_rub_from_google)
        fu_ab = ex.submit(get_abcex_usdt_rub)
        u, b, r = fu_u.result(), fu_b.result(), fu_r.result()
        ab_buy, ab_sell = fu_ab.result()

    stop["run"] = False
    time.sleep(0.5)

    if not any([u, b, r, ab_buy, ab_sell]):
        bot.edit_message_text("⚠️ Сейчас не удалось получить курс.\nПопробуйте чуть позже.", chat_id, msg.message_id)
        log_user_action(m.from_user, "ошибка получения курса")
        return

    txt = build_rate_text(u, b, r, ab_buy=ab_buy, ab_sell=ab_sell)
    bot.edit_message_text(txt, chat_id, msg.message_id, parse_mode="HTML")

    update_user_stats(m.from_user)
    log_to_channel(
        f"📊 Курс @{m.from_user.username or 'без_username'} ({m.from_user.id})\n"
        f"🕒 {now_msk().strftime('%H:%M:%S')} МСК\n"
        f"Upbit: {fmt_num(u,0) if u else '—'} | "
        f"Bithumb: {fmt_num(b,0) if b else '—'} | "
        f"Google: {fmt_num(r,2) if r else '—'} ₽ | "
        f"ABCEX buy/sell: "
        f"{fmt_num(ab_buy,2) if ab_buy else '—'} / {fmt_num(ab_sell,2) if ab_sell else '—'} ₽"
    )

# ============== ПРОЧИЕ КНОПКИ ==============
@bot.message_handler(func=lambda m: m.text == BTN_AUTO)
def toggle_auto(m):
    ensure_keyboard(m)
    chat_id = m.chat.id

    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("⏱ Каждый 1 час", callback_data="auto_1h"),
        types.InlineKeyboardButton("⏱ Каждые 5 часов", callback_data="auto_5h"),
    )
    kb.row(
        types.InlineKeyboardButton("🕛 Раз в 24 часа", callback_data="auto_24h"),
    )
    if chat_id in AUTO_USERS:
        kb.row(
            types.InlineKeyboardButton("🔕 Выключить автообновление", callback_data="auto_off")
        )

    text = "Выбери частоту автообновления курса:"
    if chat_id in AUTO_USERS:
        cur_int = AUTO_USERS[chat_id].get("interval", AUTO_INTERVAL_24H)
        text += f"\nСейчас: {human_interval(cur_int)}."

    bot.send_message(chat_id, text, reply_markup=kb)
    log_user_action(m.from_user, "открыл настройки автообновления")

@bot.callback_query_handler(func=lambda c: c.data.startswith("auto_"))
def auto_callback(c):
    chat_id = c.message.chat.id
    data = c.data

    if data == "auto_off":
        AUTO_USERS.pop(chat_id, None)
        bot.answer_callback_query(c.id, "Автообновление выключено")
        bot.send_message(chat_id, "🔕 Автообновление выключено.")
        log_user_action(c.from_user, "выключил автообновление")
        return

    if data == "auto_1h":
        interval = AUTO_INTERVAL_1H
        label = "каждый 1 час"
    elif data == "auto_5h":
        interval = AUTO_INTERVAL_5H
        label = "каждые 5 часов"
    else:
        interval = AUTO_INTERVAL_24H
        label = "каждые 24 часа"

    AUTO_USERS[chat_id] = {"interval": interval, "last": None}
    bot.answer_callback_query(c.id, "Настройки сохранены")
    bot.send_message(chat_id, f"🔔 Автообновление включено: {label}.")
    log_user_action(c.from_user, f"включил автообновление ({label})")

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

# ============== ЗАПУСК ==============
def main():
    threading.Thread(target=auto_update_loop, daemon=True).start()
    logger.info("Бот запущен.")
    log_to_channel("🚀 Бот перезапущен и готов к работе")
    bot.infinity_polling(skip_pending=True)

# ============== АНТИ-СОН ДЛЯ RENDER ==============
import threading, time, requests

def keep_awake():
    """Автоматический пинг Render, чтобы бот не засыпал."""
    url = "https://telegram-rate-bot-ooc6.onrender.com"  # <-- вставь свой Render URL
    while True:
        try:
            requests.get(url, timeout=5)
            print(f"[keep_alive] Pinged {url}")
        except Exception as e:
            print(f"[keep_alive] Ошибка пинга: {e}")
        time.sleep(600)  # каждые 10 минут (600 сек)

# Запуск в отдельном потоке
threading.Thread(target=keep_awake, daemon=True).start()

# ============== ФЕЙКОВЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER ==============
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running OK", 200

def run_web():
    """Фейковый веб-сервер, чтобы Render не ругался на порты."""
    app.run(host="0.0.0.0", port=10000)

# Запуск веб-сервера в фоне
threading.Thread(target=run_web, daemon=True).start()

if __name__ == "__main__":
    try:
        # Отправляем уведомление в Telegram (только при запуске)
        admin_id = -1003264764082  # сюда можешь указать свой Telegram ID или ID канала логов
        try:
            bot.send_message(admin_id, "♻️ Бот успешно перезапущен и готов к работе!")
        except Exception as e:
            print(f"Ошибка при отправке уведомления администратору: {e}")

        # Запуск основного цикла
        main()

    except Exception as e:
        # Если при старте что-то пошло не так — логируем
        logging.exception("❌ Ошибка при запуске бота")
        try:
            bot.send_message(admin_id, f"⚠️ Ошибка при запуске бота:\n{e}")
        except:
            pass