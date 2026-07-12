"""
Telegram-бот "Нагадувач"
=========================

Що робить:
- У каналі/групі публікує (або надсилає по команді) повідомлення з кнопками
  "Нагадати через 3 дні", "через тиждень" тощо.
- Коли користувач тисне кнопку, бот записує нагадування в базу (SQLite)
  і в потрібний час пише йому в приватні повідомлення.

ВАЖЛИВО (обмеження Telegram):
- Бот не може писати першим людині, яка з ним ще не спілкувалась.
  Тому перед першим використанням людина має написати боту /start
  в особистих повідомленнях хоча б раз.
- Якщо людина ще не писала боту, бот попросить її це зробити
  (покаже кнопку-посилання на чат з ботом).

Встановлення залежностей:
    pip install python-telegram-bot==21.6 --break-system-packages

Запуск:
    export BOT_TOKEN="ваш_токен_від_BotFather"
    python bot.py
"""

import logging
import sqlite3
import os
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import Forbidden

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "reminders.db")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН_ТУТ")
# ID каналу/групи, куди бот публікуватиме повідомлення з кнопками.
# Дізнатись ID можна командою /channelid, написаною прямо в каналі.
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")

# Варіанти нагадувань: за скільки часу ДО дедлайну нагадати.
REMINDER_OPTIONS = [
    ("За день", timedelta(days=1)),
    ("За 3 дні", timedelta(days=3)),
    ("За тиждень", timedelta(weeks=1)),
]


# ---------- База даних ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            remind_at TEXT NOT NULL,
            sent INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def save_reminder(user_id: int, chat_id: int, text: str, remind_at: datetime) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO reminders (user_id, chat_id, text, remind_at) VALUES (?, ?, ?, ?)",
        (user_id, chat_id, text, remind_at.isoformat()),
    )
    conn.commit()
    reminder_id = cur.lastrowid
    conn.close()
    return reminder_id


def mark_sent(reminder_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()


def known_users_add(user_id: int):
    """Позначаємо, що користувач вже писав боту (може отримувати особисті)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS known_users (user_id INTEGER PRIMARY KEY)"
    )
    conn.execute("INSERT OR IGNORE INTO known_users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def save_pending_text(text: str, deadline: "datetime | None") -> int:
    """Зберігає повний текст поста і дедлайн окремо (кнопка не може містити довгий текст)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pending_texts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            deadline TEXT
        )"""
    )
    cur = conn.execute(
        "INSERT INTO pending_texts (text, deadline) VALUES (?, ?)",
        (text, deadline.isoformat() if deadline else None),
    )
    conn.commit()
    pending_id = cur.lastrowid
    conn.close()
    return pending_id


def get_pending(pending_id: int):
    """Повертає (text, deadline_datetime_or_None)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pending_texts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            deadline TEXT
        )"""
    )
    row = conn.execute(
        "SELECT text, deadline FROM pending_texts WHERE id = ?", (pending_id,)
    ).fetchone()
    conn.close()
    if not row:
        return "Нагадування", None
    text, deadline_str = row
    deadline = datetime.fromisoformat(deadline_str) if deadline_str else None
    return text, deadline


def is_known_user(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS known_users (user_id INTEGER PRIMARY KEY)"
    )
    row = conn.execute(
        "SELECT 1 FROM known_users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row is not None


# ---------- Хендлери команд ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start у приватному чаті — реєструє користувача як "відомого"."""
    known_users_add(update.effective_user.id)
    await update.message.reply_text(
        "Готово! Тепер я можу надсилати вам нагадування у приват. "
        "Поверніться до каналу й натисніть потрібну кнопку нагадування."
    )


async def channel_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Допоміжна команда: написати /channelid прямо в каналі/групі,
    щоб дізнатись його ID (знадобиться для змінної CHANNEL_ID).
    Працює і для звичайних повідомлень, і для постів у каналі.
    """
    msg = update.effective_message
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"ID цього чату: {update.effective_chat.id}",
    )


async def post_reminder_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда /remind — пишеться ПРИВАТНО боту (не в каналі!).

    Формат:
        /remind 18.07.2026
        [далі текст можливості — скільки завгодно рядків]

    Перший рядок після /remind — ДЕДЛАЙН у форматі ДД.ММ.РРРР.
    Кнопки "За 1 день / За 3 дні / За тиждень" рахуватимуть час
    саме ДО цього дедлайну, а не від моменту натискання.

    Якщо перший рядок не є датою — дедлайн не встановлюється,
    і бот працюватиме по-старому: рахуватиме від моменту натискання кнопки.
    """
    if update.effective_chat.type != "private":
        return

    if not CHANNEL_ID:
        await update.message.reply_text(
            "⚠️ Не налаштовано CHANNEL_ID. Напишіть /channelid прямо в каналі, "
            "щоб дізнатись його ID, і додайте його в змінні середовища Railway."
        )
        return

    full_text = update.message.text or ""
    # Прибираємо саму команду "/remind" (і можливий @botname після неї).
    after_command = full_text.split(None, 1)
    body = after_command[1] if len(after_command) > 1 else ""

    deadline = None
    lines = body.split("\n", 1)
    first_line = lines[0].strip()
    try:
        deadline_date = datetime.strptime(first_line, "%d.%
