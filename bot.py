#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import re
from datetime import datetime

from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from config import TELEGRAM_TOKEN, DEEPSEEK_API_KEY, ADMIN_IDS, TIMEZONE, COMPANY_NAME, BOT_NAME, logger
from database import init_db, is_user_registered, register_user, get_all_registered_users, create_task, get_user_tasks, update_task_progress
from tasks import check_all_tasks, schedule_task_checker
from utils import handle_voice_message, handle_document, ask_deepseek

# --- Команды бота ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not is_user_registered(chat_id):
        register_user(chat_id, user.username or "", user.first_name or "", user.last_name or "")
        logger.info(f"Новый пользователь зарегистрирован: ID {chat_id}")
        await update.message.reply_text(
            f"Здравствуйте, {user.first_name or user.username or 'гость'}! "
            f"Меня зовут **{BOT_NAME}**, я виртуальный ассистент руководителя и сотрудников **{COMPANY_NAME}**. "
            f"Я помогу вам с задачами, напоминаниями и контролем проектов."
        )
    else:
        await update.message.reply_text(f"С возвращением, {user.first_name or user.username}! Чем могу помочь?")

    await context.bot.set_my_commands([
        BotCommand("help", "Показать список команд"),
        BotCommand("new_task", "Создать новую задачу"),
        BotCommand("my_tasks", "Мои активные задачи"),
        BotCommand("task_info", "Информация о задаче"),
        BotCommand("task_progress", "Обновить прогресс задачи"),
        BotCommand("assign_task", "Назначить задачу сотруднику"),
        BotCommand("get_contact_info", "Мои контактные данные"),
    ])

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "?? *Доступные команды:*\n"
        "/start — Регистрация\n"
        "/help — Это сообщение\n"
        "/new_task — Создать задачу\n"
        "/my_tasks — Список ваших задач\n"
        "/task_info — Детали задачи\n"
        "/task_progress — Отметить прогресс\n"
        "/assign_task — Назначить задачу другому\n"
        "/get_contact_info — Показать мои данные\n\n"
        "?? *Голосовые:* отправьте голосовое — я распознаю.\n"
        "?? *Файлы:* загрузите PDF/DOCX/TXT — извлеку текст."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def new_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(
        "Опишите задачу в формате:\n"
        "`Название | Описание | ДД.ММ.ГГГГ ЧЧ:ММ | @username_ответственного`\n"
        "Пример: `Отчет | Подготовить квартальный отчет | 25.12.2025 18:00 | @kotlyarov`"
    )
    context.user_data['awaiting_task'] = True

async def handle_new_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_task'):
        return False
    text = update.message.text
    parts = [p.strip() for p in text.split('|')]
    if len(parts) < 4:
        await update.message.reply_text("Неверный формат. Используйте: Название | Описание | Дата время | @username")
        context.user_data['awaiting_task'] = False
        return True

    title, description, due_str, assignee_username = parts[0], parts[1], parts[2], parts[3]
    if not assignee_username.startswith('@'):
        assignee_username = '@' + assignee_username
    # Поиск пользователя по username
    from database import get_user_id_by_username
    assignee_id = get_user_id_by_username(assignee_username)
    if not assignee_id:
        await update.message.reply_text(f"Пользователь {assignee_username} не зарегистрирован в боте. Попросите его написать /start")
        context.user_data['awaiting_task'] = False
        return True

    due_date = None
    try:
        due_date = datetime.strptime(due_str, "%d.%m.%Y %H:%M")
        due_date = TIMEZONE.localize(due_date)
    except:
        await update.message.reply_text("Дата не распознана, задача создана без дедлайна")

    task_id = create_task(title, description, assignee_id, update.effective_user.id, due_date)
    await update.message.reply_text(f"? Задача #{task_id} создана и назначена {assignee_username}")
    context.user_data['awaiting_task'] = False
    return True

async def my_tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = get_user_tasks(user_id)
    if not tasks:
        await update.message.reply_text("У вас нет активных задач.")
        return
    msg = "?? *Ваши задачи:*\n"
    for t in tasks:
        due = t['due_date'].strftime("%d.%m.%Y %H:%M") if t['due_date'] else "без срока"
        msg += f"#{t['id']} *{t['title']}* — {t['progress']}% (до {due})\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def task_progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /task_progress ID_задачи процент")
        return
    try:
        task_id = int(args[0])
        progress = int(args[1])
        if progress < 0 or progress > 100:
            raise ValueError
        update_task_progress(task_id, progress, update.effective_user.id)
        await update.message.reply_text(f"Прогресс задачи #{task_id} обновлён до {progress}%")
    except:
        await update.message.reply_text("Ошибка: укажите числовой ID и процент от 0 до 100")

async def get_contact_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Ваш ID: {user.id}\n"
        f"Username: @{user.username or 'нет'}\n"
        f"Имя: {user.first_name or 'нет'}\n"
        f"Фамилия: {user.last_name or 'нет'}"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если ожидаем создание задачи
    if await handle_new_task(update, context):
        return

    user_message = update.message.text
    chat_id = update.effective_chat.id
    await update.message.reply_chat_action(action="typing")
    response = ask_deepseek(user_message, str(chat_id))
    await update.message.reply_text(response)

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("new_task", new_task_command))
    app.add_handler(CommandHandler("my_tasks", my_tasks_command))
    app.add_handler(CommandHandler("task_progress", task_progress_command))
    app.add_handler(CommandHandler("get_contact_info", get_contact_info))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Планировщик проверки задач (каждые 10 минут)
    schedule_task_checker(app)

    logger.info("Бот Настенька запущен")
    app.run_polling()

if __name__ == '__main__':
    main()