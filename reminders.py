import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

from telegram.ext import ContextTypes

from config import TIMEZONE, logger
from database import (
    create_reminder,
    format_db_datetime,
    get_due_reminders,
    get_user_pending_reminders,
    mark_reminder_done,
)

REMINDER_WORDS = ("напомни", "напоминание", "напомнить")


def _clean_reminder_text(text: str) -> str:
    text = text.strip(" \n\t.,!?:;—-")
    text = re.sub(r"^(мне|пожалуйста|плиз|о том,? что)\s+", "", text, flags=re.IGNORECASE)
    text = text.strip(" \n\t.,!?:;—-")
    return text or "напоминание"


def _extract_reminder_text(original_text: str, fallback_start: Optional[int] = None) -> str:
    # Самый надежный вариант: «... что у меня совещание».
    match = re.search(r"\bчто\b\s+(.+)$", original_text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return _clean_reminder_text(match.group(1))

    if fallback_start is not None and fallback_start < len(original_text):
        return _clean_reminder_text(original_text[fallback_start:])

    return "напоминание"


def parse_reminder_request(text: str) -> Optional[Tuple[datetime, str]]:
    """Парсит простые русские просьбы о напоминании.

    Поддерживаются форматы:
    - «Напомни мне в 15:00 что у меня совещание»
    - «Напомни 25.12.2026 в 18:00 что сдать отчет»
    - «Напомни через 10 минут что позвонить»
    - «Напомни через 2 часа что проверить задачу»
    """
    if not text:
        return None

    lowered = text.lower()
    if not any(word in lowered for word in REMINDER_WORDS):
        return None

    now = datetime.now(TIMEZONE)

    relative = re.search(
        r"через\s+(\d{1,4})\s*(минут[уы]?|минута|мин|час(?:а|ов)?|ч|день|дня|дней|д)",
        lowered,
        flags=re.IGNORECASE,
    )
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        if unit.startswith("мин"):
            remind_at = now + timedelta(minutes=amount)
        elif unit.startswith("час") or unit == "ч":
            remind_at = now + timedelta(hours=amount)
        else:
            remind_at = now + timedelta(days=amount)
        reminder_text = _extract_reminder_text(text, relative.end())
        return remind_at, reminder_text

    absolute = re.search(
        r"(?:(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s*)?(?:в\s*)?(\d{1,2})[:.](\d{2})",
        lowered,
        flags=re.IGNORECASE,
    )
    if absolute:
        day_s, month_s, year_s, hour_s, minute_s = absolute.groups()
        hour = int(hour_s)
        minute = int(minute_s)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None

        if day_s and month_s:
            day = int(day_s)
            month = int(month_s)
            if year_s:
                year = int(year_s)
                if year < 100:
                    year += 2000
            else:
                year = now.year
        else:
            day = now.day
            month = now.month
            year = now.year

        try:
            remind_at = TIMEZONE.localize(datetime(year, month, day, hour, minute))
        except ValueError:
            return None

        # Если дату не указали, а время сегодня уже прошло, переносим на завтра.
        if not day_s and remind_at <= now:
            remind_at += timedelta(days=1)

        reminder_text = _extract_reminder_text(text, absolute.end())
        return remind_at, reminder_text

    return None


async def handle_reminder_request(update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    parsed = parse_reminder_request(text)
    if not parsed:
        return False

    remind_at, reminder_text = parsed
    reminder_id = create_reminder(update.effective_chat.id, reminder_text, remind_at)
    await update.message.reply_text(
        f"✅ Напоминание #{reminder_id} поставлено на {format_db_datetime(remind_at)} по МСК.\n"
        f"Текст: {reminder_text}"
    )
    return True


async def my_reminders_command(update, context: ContextTypes.DEFAULT_TYPE):
    reminders = get_user_pending_reminders(update.effective_chat.id)
    if not reminders:
        await update.message.reply_text("У вас нет активных напоминаний.")
        return

    message = "⏰ Ваши активные напоминания:\n"
    for reminder in reminders:
        message += f"#{reminder['id']} — {format_db_datetime(reminder['remind_at'])}: {reminder['text']}\n"
    await update.message.reply_text(message)


async def check_due_reminders(app):
    now = datetime.now(TIMEZONE)
    reminders = get_due_reminders(now)

    for reminder in reminders:
        try:
            await app.bot.send_message(
                chat_id=reminder["user_id"],
                text=f"⏰ Напоминание: {reminder['text']}",
            )
            mark_reminder_done(reminder["id"], status="sent")
        except Exception as e:
            logger.error(f"Не удалось отправить напоминание #{reminder['id']}: {e}")
            # Чтобы бот не пытался бесконечно слать одно и то же сообщение, помечаем как failed.
            mark_reminder_done(reminder["id"], status="failed")


def schedule_reminder_checker(app):
    job_queue = app.job_queue
    if not job_queue:
        logger.warning("JobQueue не запущен. Установите python-telegram-bot[job-queue]")
        return

    async def callback(_):
        await check_due_reminders(app)

    # Проверяем часто, чтобы напоминания приходили почти в назначенную минуту.
    job_queue.run_repeating(callback, interval=20, first=5)
    logger.info("Планировщик напоминаний запущен")
