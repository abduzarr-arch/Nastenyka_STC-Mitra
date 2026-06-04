#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
from datetime import datetime

from telegram import BotCommand, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import BOT_NAME, COMPANY_NAME, TELEGRAM_TOKEN, TIMEZONE, logger
from database import (
    create_task,
    format_db_datetime,
    get_task_by_id,
    get_user_id_by_username,
    get_user_tasks,
    init_db,
    is_user_registered,
    register_user,
    update_task_progress,
)
from reminders import handle_reminder_request, my_reminders_command, schedule_reminder_checker
from excel_utils import handle_create_excel_text, handle_excel_followup_text
from tasks import schedule_task_checker
from utils import ask_deepseek, handle_document, handle_photo_message, handle_voice_message


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not is_user_registered(chat_id):
        register_user(chat_id, user.username or "", user.first_name or "", user.last_name or "")
        logger.info(f"Новый пользователь зарегистрирован: ID {chat_id}")
        await update.message.reply_text(
            f"Здравствуйте, {user.first_name or user.username or 'гость'}!\n"
            f"Меня зовут {BOT_NAME}, я виртуальный ассистент руководителя и сотрудников {COMPANY_NAME}.\n"
            "Я помогу вам с задачами, напоминаниями и контролем проектов."
        )
    else:
        await update.message.reply_text(f"С возвращением, {user.first_name or user.username}! Чем могу помочь?")

    await context.bot.set_my_commands([
        BotCommand("help", "Показать список команд"),
        BotCommand("new_task", "Создать новую задачу"),
        BotCommand("assign_task", "Назначить задачу сотруднику"),
        BotCommand("my_tasks", "Мои активные задачи"),
        BotCommand("my_reminders", "Мои активные напоминания"),
        BotCommand("task_info", "Информация о задаче"),
        BotCommand("task_progress", "Обновить прогресс задачи"),
        BotCommand("get_contact_info", "Мои контактные данные"),
    ])


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📌 Доступные команды:\n"
        "/start — Регистрация\n"
        "/help — Это сообщение\n"
        "/new_task — Создать задачу\n"
        "/assign_task — Назначить задачу сотруднику\n"
        "/my_tasks — Список ваших задач\n"
        "/my_reminders — Список активных напоминаний\n"
        "/task_info ID — Детали задачи\n"
        "/task_progress ID процент — Отметить прогресс\n"
        "/get_contact_info — Показать мои данные\n\n"
        "⏰ Напоминания: напишите, например, «Напомни мне в 15:00 что у меня совещание».\n"
        "🎙 Голосовые: отправьте голосовое — я распознаю и отвечу.\n"
        "🖼 Изображения: отправьте фото с подписью-вопросом — я отвечу по картинке.\n"
        "📄 Файлы: загрузите PDF/DOCX/TXT — извлеку текст."
    )
    await update.message.reply_text(help_text)


async def new_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Опишите задачу в формате:\n"
        "Название | Описание | ДД.ММ.ГГГГ ЧЧ:ММ | @username_ответственного\n"
        "Пример: Отчет | Подготовить квартальный отчет | 25.12.2026 18:00 | @kotlyarov"
    )
    context.user_data["awaiting_task"] = True


async def assign_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await new_task_command(update, context)


async def handle_new_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_task"):
        return False

    text = update.message.text or ""
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 4:
        await update.message.reply_text("Неверный формат. Используйте: Название | Описание | Дата время | @username")
        context.user_data["awaiting_task"] = False
        return True

    title, description, due_str, assignee_username = parts[0], parts[1], parts[2], parts[3]
    if not assignee_username.startswith("@"):
        assignee_username = "@" + assignee_username

    assignee_id = get_user_id_by_username(assignee_username)
    if not assignee_id:
        await update.message.reply_text(
            f"Пользователь {assignee_username} не зарегистрирован в боте. Попросите его написать /start"
        )
        context.user_data["awaiting_task"] = False
        return True

    due_date = None
    try:
        due_date = datetime.strptime(due_str, "%d.%m.%Y %H:%M")
        due_date = TIMEZONE.localize(due_date)
    except ValueError:
        await update.message.reply_text("Дата не распознана, задача создана без дедлайна")

    task_id = create_task(title, description, assignee_id, update.effective_user.id, due_date)
    await update.message.reply_text(f"✅ Задача #{task_id} создана и назначена {assignee_username}")
    context.user_data["awaiting_task"] = False
    return True


async def my_tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = get_user_tasks(update.effective_user.id)
    if not tasks:
        await update.message.reply_text("У вас нет активных задач.")
        return

    msg = "📌 Ваши задачи:\n"
    for task in tasks:
        due = format_db_datetime(task.get("due_date"))
        msg += f"#{task['id']} {task['title']} — {task['progress']}% (до {due})\n"
    await update.message.reply_text(msg)


async def task_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /task_info ID_задачи")
        return

    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID задачи должен быть числом")
        return

    task = get_task_by_id(task_id)
    if not task:
        await update.message.reply_text(f"Задача #{task_id} не найдена")
        return

    await update.message.reply_text(
        f"Задача #{task['id']}: {task['title']}\n"
        f"Описание: {task.get('description') or '—'}\n"
        f"Ответственный: {task['assigned_to']}\n"
        f"Срок: {format_db_datetime(task.get('due_date'))}\n"
        f"Статус: {task['status']}\n"
        f"Прогресс: {task['progress']}%"
    )


async def task_progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /task_progress ID_задачи процент")
        return

    try:
        task_id = int(args[0])
        progress = int(args[1])
        if not 0 <= progress <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Ошибка: укажите числовой ID и процент от 0 до 100")
        return

    updated = update_task_progress(task_id, progress, update.effective_user.id)
    if updated:
        await update.message.reply_text(f"Прогресс задачи #{task_id} обновлён до {progress}%")
    else:
        await update.message.reply_text("Задача не найдена или назначена не вам")


async def get_contact_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Ваш ID: {user.id}\n"
        f"Username: @{user.username or 'нет'}\n"
        f"Имя: {user.first_name or 'нет'}\n"
        f"Фамилия: {user.last_name or 'нет'}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await handle_new_task(update, context):
        return

    user_message = update.message.text or ""
    if await handle_reminder_request(update, context, user_message):
        return

    # Если пользователь до этого отправил Excel без подписи, следующее сообщение
    # считаем командой/вопросом к этому файлу.
    if await handle_excel_followup_text(update, context, user_message):
        return

    if await handle_create_excel_text(update, context, user_message):
        return

    chat_id = update.effective_chat.id
    await update.message.reply_chat_action(action="typing")
    response = await asyncio.to_thread(ask_deepseek, user_message, str(chat_id))
    await update.message.reply_text(response)


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN не задан в переменных Railway")

    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("new_task", new_task_command))
    app.add_handler(CommandHandler("assign_task", assign_task_command))
    app.add_handler(CommandHandler("my_tasks", my_tasks_command))
    app.add_handler(CommandHandler("my_reminders", my_reminders_command))
    app.add_handler(CommandHandler("task_info", task_info_command))
    app.add_handler(CommandHandler("task_progress", task_progress_command))
    app.add_handler(CommandHandler("get_contact_info", get_contact_info))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    schedule_task_checker(app)
    schedule_reminder_checker(app)

    logger.info("Бот Настенька запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
