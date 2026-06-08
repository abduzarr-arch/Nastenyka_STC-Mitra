#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
from datetime import datetime

from telegram import BotCommand, Update
from telegram.ext import (
    ApplicationBuilder,
    ChatMemberHandler,
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
from docx_utils import handle_create_word_text, handle_word_followup_text
from group_utils import (
    agreements_command,
    clean_group_trigger_text,
    get_dialog_key,
    is_addressed_to_bot,
    is_group_chat,
    maybe_handle_group_agreement,
    save_agreement_command,
)
from cross_chat import (
    bind_chat_command,
    chat_info_command,
    format_chat_members_for_prompt,
    format_group_chats_for_prompt,
    handle_chat_member_update,
    group_chats_command,
    maybe_handle_chat_registry_request,
    maybe_handle_private_group_send,
    remember_chat_administrators,
    remember_current_group_chat,
    remember_visible_chat_participants,
    send_to_chat_command,
)
from tasks import schedule_task_checker
from assignments import (
    bind_employee_command,
    employees_command,
    followups_command,
    maybe_handle_assignment_reply,
    maybe_handle_control_request,
    schedule_controlled_tasks_checker,
    stop_followup_command,
)
from internet_search import answer_online, search_command, should_use_online_search, split_telegram_text
from operations import (
    build_team_context,
    daily_summary_command,
    maybe_handle_operational_request,
    op_task_command,
    operational_bot_commands,
    project_command,
    projects_command,
    remember_addressed_group_message,
    schedule_operational_checker,
    subtask_command,
    task_detail_command as op_task_detail_command,
    task_update_command as op_task_update_command,
    tasks_report_command,
)
from utils import ask_deepseek, handle_document, handle_photo_message, handle_voice_message
from yougile_utils import (
    maybe_handle_yougile_request,
    schedule_yougile_checker,
    yougile_bot_commands,
    yougile_create_task_command,
    yougile_status_command,
    yougile_structure_command,
    yougile_tasks_command,
)


def _looks_like_drafting_request(text: str) -> bool:
    lowered = (text or "").lower()
    write_markers = ("напиши", "составь", "подготовь", "сформулируй", "сделай текст", "набросай", "письмо")
    document_markers = ("письмо", "ответ", "заказчик", "клиент", "коммерческое предложение", "кп", "объяснить")
    if any(marker in lowered for marker in write_markers) and any(marker in lowered for marker in document_markers):
        return True
    return "надо объяснить заказчику" in lowered or "текст письма" in lowered or "ответ заказчику" in lowered


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
        BotCommand("agreements", "Договорённости чата"),
        BotCommand("bind_chat", "Привязать рабочий чат"),
        BotCommand("group_chats", "Список рабочих чатов"),
        BotCommand("send_to_chat", "Отправить сообщение в рабочий чат"),
        BotCommand("followups", "Активные контроли поручений"),
        BotCommand("stop_followup", "Остановить контроль поручения"),
        BotCommand("bind_employee", "Привязать имя сотрудника к username"),
        BotCommand("employees", "Список привязанных сотрудников"),
        BotCommand("save_agreement", "Записать договорённость"),
        BotCommand("task_info", "Информация о задаче"),
        BotCommand("task_progress", "Обновить прогресс задачи"),
        BotCommand("get_contact_info", "Мои контактные данные"),
        BotCommand("search", "Поиск в интернете"),
        BotCommand("ask", "Задать вопрос боту в группе"),
        *operational_bot_commands(),
        *yougile_bot_commands(),
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
        "/agreements — Последние договорённости в текущем чате\n"
        "/bind_chat имя — Привязать текущий групповой чат, например /bind_chat рабочий\n"
        "/group_chats — Список привязанных рабочих чатов\n"
        "/send_to_chat имя текст — Отправить сообщение в рабочий чат из лички\n"
        "/followups — Активные контроли поручений\n"
        "/stop_followup ID — Остановить контроль поручения\n"
        "/bind_employee Дима @username — Привязать имя сотрудника к Telegram username\n"
        "/employees — Список привязанных сотрудников\n"
        "/save_agreement текст — Записать договорённость вручную\n"
        "/task_info ID — Детали задачи\n"
        "/task_progress ID процент — Отметить прогресс\n"
        "/get_contact_info — Показать мои данные\n"
        "/search запрос — Найти актуальную информацию в интернете\n"
        "/ask вопрос — Задать вопрос боту в группе, например /ask что решили по РПЗ\n\n"
        "⏰ Напоминания: напишите, например, «Напомни мне в 15:00 что у меня совещание».\n"
        "🎙 Голосовые: отправьте голосовое — я распознаю и отвечу.\n"
        "🖼 Изображения: отправьте фото с подписью-вопросом — я отвечу по картинке.\n"
        "📄 Файлы: загрузите PDF/TXT/Word/Excel — извлеку текст или подготовлю изменённый файл.\n"
        "🌐 Онлайн-поиск: /search запрос или фразы вроде «проверь в интернете».\n"
        "👥 В группах: отвечаю на /ask@username_бота, ответ на моё сообщение или @упоминание, если оно доставляется Telegram.\n"
        "📣 Из лички могу отправить сообщение в привязанный рабочий чат: /send_to_chat рабочий текст\n"
        "🕘 Контроль поручений: «По объекту спартака нужно, чтобы Дима подготовил РПЗ к пн. Уточняй у него раз в день».\n"
        "📝 Word: отправьте .docx и затем напишите, что изменить — пришлю новую исправленную копию.\n"
        "🏗 Операционное ядро: /project, /op_task, /tasks, /op_update, /subtask, /daily_summary.\n"
        "👥 Командный контекст: в группе запоминаю адресованные мне задачи/статусы и использую их как общий рабочий контекст, но историю ответов веду отдельно по каждому участнику."
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


async def process_text_request(update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
    """Единая обработка текстового запроса после проверки, что бот действительно вызвали."""
    user_message = (user_message or "").strip()

    if not user_message:
        await update.message.reply_text("Я на связи. Напишите вопрос после упоминания бота или используйте /ask вопрос.")
        return

    if is_group_chat(update) and await maybe_handle_assignment_reply(update, context, user_message):
        return

    if is_group_chat(update):
        await remember_addressed_group_message(update, user_message)

    if is_group_chat(update) and await maybe_handle_group_agreement(update, context, user_message):
        return

    is_drafting_request = _looks_like_drafting_request(user_message)

    if not is_drafting_request:
        if await maybe_handle_operational_request(update, context, user_message):
            return

        if await maybe_handle_control_request(update, context, user_message):
            return

    if await handle_reminder_request(update, context, user_message):
        return

    # Если пользователь до этого отправил Word без подписи, следующее сообщение
    # считаем командой/вопросом к этому файлу.
    if await handle_word_followup_text(update, context, user_message):
        return

    # Если пользователь до этого отправил Excel без подписи, следующее сообщение
    # считаем командой/вопросом к этому файлу.
    if await handle_excel_followup_text(update, context, user_message):
        return

    if await handle_create_word_text(update, context, user_message):
        return

    if await handle_create_excel_text(update, context, user_message):
        return

    if await maybe_handle_private_group_send(update, context, user_message):
        return

    if await maybe_handle_chat_registry_request(update, context, user_message):
        return

    if await maybe_handle_yougile_request(update, context, user_message):
        return

    dialog_key = get_dialog_key(update)
    await update.message.reply_chat_action(action="typing")

    prompt = user_message
    if is_drafting_request:
        prompt = (
            "Пользователь просит подготовить деловой текст/письмо. "
            "Не ставь задачу и не спрашивай ответственного. "
            "Сразу дай готовый текст письма на русском языке, в деловом стиле, с темой письма при необходимости. "
            "Если есть технические риски, сформулируй их понятно для заказчика без лишней категоричности.\n\n"
            f"Запрос пользователя:\n{user_message}"
        )
    team_context = build_team_context(update, user_message)
    chats_context = format_group_chats_for_prompt()
    members_context = format_chat_members_for_prompt()
    if is_drafting_request:
        if team_context:
            prompt += f"\n\nРабочий контекст из базы задач/договорённостей:\n{team_context}"
        if members_context:
            prompt += f"\n\nИзвестные участники рабочих чатов:\n{members_context}"
    elif is_group_chat(update):
        user = update.effective_user
        author = (user.full_name if user else "участник")
        context_block = f"\n\nОбщий рабочий контекст, известный боту по этому чату/проекту:\n{team_context}" if team_context else ""
        members_block = f"\n\nИзвестные участники рабочих чатов:\n{members_context}" if members_context else ""
        prompt = (
            f"Сообщение из рабочего группового чата от {author}:\n"
            f"{user_message}"
            f"{context_block}"
            f"{members_block}\n\n"
            "Правила командного диалога: ты видишь только сообщения, адресованные тебе, и структурированные записи памяти. "
            "Используй общий контекст, если он помогает, но не копируй дословно ответ, предназначенный другому участнику. "
            "Отвечай на текущий вопрос этого пользователя, при необходимости кратко подсвечивай релевантный контекст: «по этому объекту уже есть задача #...». "
            "Не утверждай, что прочитала всю переписку группы."
        )
    elif team_context:
        prompt = (
            f"Запрос пользователя:\n{user_message}\n\n"
            f"Рабочий контекст из базы задач/договорённостей:\n{team_context}\n\n"
            f"Известные участники рабочих чатов:\n{members_context or '[нет]'}\n\n"
            "Ответь с учетом структурированного контекста, не выдумывая статусы, которых нет в базе."
        )
    elif chats_context or members_context:
        prompt = (
            f"Запрос пользователя:\n{user_message}\n\n"
            f"Известные рабочие чаты, куда бот может отправлять сообщения и напоминания:\n{chats_context or '[нет]'}\n\n"
            f"Известные участники рабочих чатов:\n{members_context or '[нет]'}\n\n"
            "Если пользователь спрашивает про чаты или межчатовые поручения, опирайся только на этот список. "
            "Если пользователь называет человека по имени, используй список участников/username как подсказку. "
            "Не утверждай, что не участвуешь в чатах, если список не пуст."
        )

    if should_use_online_search(user_message):
        response = await asyncio.to_thread(answer_online, user_message, dialog_key)
    else:
        response = await asyncio.to_thread(ask_deepseek, prompt, dialog_key)
    for part in split_telegram_text(response):
        await update.message.reply_text(part)


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Явная команда для групп с включённым privacy mode: /ask@bot вопрос."""
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text(
            "Напишите вопрос после команды. Например:\n"
            "/ask что решили по РПЗ?\n\n"
            "В группах с включённым Privacy Mode обычное @упоминание может не доходить до бота; "
            "команда /ask@username_бота доставляется надёжнее."
        )
        return
    await process_text_request(update, context, text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # В группах не обрабатываем обычную переписку. Бот отвечает только,
    # если его упомянули, ответили на его сообщение или использовали команду.
    if is_group_chat(update):
        remember_current_group_chat(update)
        await remember_visible_chat_participants(update, context)
    if is_group_chat(update) and not await is_addressed_to_bot(update, context):
        return

    raw_message = update.message.text or ""
    user_message = await clean_group_trigger_text(update, context, raw_message) if is_group_chat(update) else raw_message

    if await handle_new_task(update, context):
        return

    await process_text_request(update, context, user_message)


async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat and chat.type in {"group", "supergroup"}:
        remember_current_group_chat(update)
        await handle_chat_member_update(update, context)
        await remember_chat_administrators(update, context)

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
    app.add_handler(CommandHandler("agreements", agreements_command))
    app.add_handler(CommandHandler("save_agreement", save_agreement_command))
    app.add_handler(CommandHandler("bind_chat", bind_chat_command))
    app.add_handler(CommandHandler("group_chats", group_chats_command))
    app.add_handler(CommandHandler("send_to_chat", send_to_chat_command))
    app.add_handler(CommandHandler("followups", followups_command))
    app.add_handler(CommandHandler("stop_followup", stop_followup_command))
    app.add_handler(CommandHandler("bind_employee", bind_employee_command))
    app.add_handler(CommandHandler("employees", employees_command))
    app.add_handler(CommandHandler("chat_info", chat_info_command))
    app.add_handler(CommandHandler("task_info", task_info_command))
    app.add_handler(CommandHandler("task_progress", task_progress_command))
    app.add_handler(CommandHandler("get_contact_info", get_contact_info))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("ask", ask_command))
    app.add_handler(CommandHandler("project", project_command))
    app.add_handler(CommandHandler("projects", projects_command))
    app.add_handler(CommandHandler("op_task", op_task_command))
    app.add_handler(CommandHandler("tasks", tasks_report_command))
    app.add_handler(CommandHandler("op_task_info", op_task_detail_command))
    app.add_handler(CommandHandler("op_update", op_task_update_command))
    app.add_handler(CommandHandler("subtask", subtask_command))
    app.add_handler(CommandHandler("daily_summary", daily_summary_command))
    app.add_handler(CommandHandler("yg_status", yougile_status_command))
    app.add_handler(CommandHandler("yg_structure", yougile_structure_command))
    app.add_handler(CommandHandler("yg_tasks", yougile_tasks_command))
    app.add_handler(CommandHandler("yg_create", yougile_create_task_command))
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    schedule_task_checker(app)
    schedule_reminder_checker(app)
    schedule_controlled_tasks_checker(app)
    schedule_operational_checker(app)
    schedule_yougile_checker(app)

    logger.info("Бот Настенька запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
