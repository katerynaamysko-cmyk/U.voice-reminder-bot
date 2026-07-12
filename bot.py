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
        deadline_date = datetime.strptime(first_line, "%d.%m.%Y")
        deadline = deadline_date.replace(hour=9, minute=0, second=0, microsecond=0)
        text = lines[1].lstrip("\n") if len(lines) > 1 else "Нагадати про це?"
    except ValueError:
        # Перший рядок — не дата, вважаємо, що дедлайну немає, весь body — це текст.
        text = body if body else "Нагадати про це?"

    if not text.strip():
        text = "Нагадати про це?"

    pending_id = save_pending_text(text, deadline)

    bot_username = (await context.bot.get_me()).username
    keyboard = []
    now = datetime.now()
    any_valid = False
    for i, (label, delta) in enumerate(REMINDER_OPTIONS):
        if deadline:
            would_remind_at = deadline - delta
            if would_remind_at <= now:
                # Цей варіант вже запізно пропонувати — дедлайн занадто близько.
                continue
        keyboard.append([InlineKeyboardButton(label, callback_data=f"remind|{i}|{pending_id}")])
        any_valid = True

    if not any_valid:
        # Або дедлайну немає (тоді всі варіанти валідні — цей блок не спрацює),
        # або дедлайн вже настільки близько, що жоден варіант "за X до дедлайну" не підходить.
        keyboard = [
            [InlineKeyboardButton(label, callback_data=f"remind|{i}|{pending_id}")]
            for i, (label, _) in enumerate(REMINDER_OPTIONS)
        ]

    keyboard.append(
        [InlineKeyboardButton("▶️ Активувати нагадування (тиснути раз)", url=f"https://t.me/{bot_username}?start=go")]
    )

    deadline_line = f"\n\n⏰ Дедлайн: {deadline.strftime('%d.%m.%Y')}" if deadline else ""
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=f"{text}{deadline_line}\n\n📌 Оберіть, коли нагадати:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=False,
    )
    confirmation = "✅ Опубліковано в каналі."
    if not deadline:
        confirmation += (
            "\n\nℹ️ Дедлайн не вказано (перший рядок мав бути датою ДД.ММ.РРРР) — "
            "нагадування рахуватимуться від моменту натискання кнопки, а не від дедлайну."
        )
    await update.message.reply_text(confirmation)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    user = query.from_user
    _, idx_str, pending_id_str = query.data.split("|", 2)
    idx = int(idx_str)
    pending_id = int(pending_id_str)
    text, deadline = get_pending(pending_id)
    label, delta = REMINDER_OPTIONS[idx]

    if not is_known_user(user.id):
        await query.answer(
            "Спершу натисніть кнопку '▶️ Активувати нагадування' під цим постом.",
            show_alert=True,
        )
        return

    now = datetime.now()
    if deadline:
        remind_at = deadline - delta
    else:
        # Дедлайн не вказаний — рахуємо від моменту натискання (старий режим).
        remind_at = now + delta

    if remind_at <= now:
        await query.answer(
            "⏰ Цей варіант вже запізно обирати — дедлайн занадто близько.",
            show_alert=True,
        )
        return

    reminder_id = save_reminder(user.id, user.id, text, remind_at)
    delay_seconds = (remind_at - now).total_seconds()

    context.job_queue.run_once(
        send_reminder,
        when=delay_seconds,
        data={"reminder_id": reminder_id, "user_id": user.id, "text": text},
        name=f"reminder_{reminder_id}",
    )

    when_str = remind_at.strftime("%d.%m.%Y о %H:%M")
    await context.bot.send_message(
        chat_id=user.id,
        text=f"✅ Нагадаю вам про цю можливість {when_str}.",
    )
    await query.answer("Нагадування встановлено ✅")


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    try:
        await context.bot.send_message(
            chat_id=job_data["user_id"],
            text=f"🔔 Нагадування: {job_data['text']}",
        )
        mark_sent(job_data["reminder_id"])
    except Forbidden:
        logger.warning(
            "Не вдалось надіслати нагадування user_id=%s: бот заблокований користувачем.",
            job_data["user_id"],
        )


async def restore_pending_jobs(app: Application):
    """Після рестарту бота повторно ставимо в чергу ще не надіслані нагадування."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, user_id, text, remind_at FROM reminders WHERE sent = 0"
    ).fetchall()
    conn.close()

    now = datetime.now()
    for reminder_id, user_id, text, remind_at_str in rows:
        remind_at = datetime.fromisoformat(remind_at_str)
        delay = (remind_at - now).total_seconds()
        if delay < 0:
            delay = 0
        app.job_queue.run_once(
            send_reminder,
            when=delay,
            data={"reminder_id": reminder_id, "user_id": user_id, "text": text},
            name=f"reminder_{reminder_id}",
        )


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("remind", post_reminder_prompt))
    # CommandHandler за замовчуванням не завжди ловить пости в каналі (channel_post),
    # тому окремо реєструємо /channelid через MessageHandler з явним фільтром.
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^/channelid") & (filters.UpdateType.MESSAGE | filters.UpdateType.CHANNEL_POST),
            channel_id_cmd,
        )
    )
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^remind\|"))

    app.post_init = restore_pending_jobs

    logger.info("Бот запущено.")
    app.run_polling()


if __name__ == "__main__":
    main()
