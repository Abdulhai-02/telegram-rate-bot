import os
from dotenv import load_dotenv
import telebot

# Загружаем токен из .env
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# Обработка сообщений именно из КАНАЛОВ
@bot.channel_post_handler(func=lambda m: True)
def get_channel_id(message):
    print(f"\n📡 Chat ID канала: {message.chat.id}\n")
    bot.send_message(message.chat.id, f"📡 Chat ID: <code>{message.chat.id}</code>")

print("✅ Бот запущен. Отправь любое сообщение в канал, где он админ.")
bot.infinity_polling()