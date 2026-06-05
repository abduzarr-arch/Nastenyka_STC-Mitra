"""Контроль поручений в рабочих чатах.

Что умеет модуль:
- из лички принять естественную команду руководителя: «По объекту X нужно, чтобы Дима ... уточняй раз в день»;
- найти привязанный групповой чат по alias/названию;
- отправить первое сообщение в группу;
- сохранить контроль в SQLite и ежедневно уточнять статус;
- остановить контроль командой /stop_followup или ответом сотрудника «готово/сделано» на сообщение бота.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS, TIMEZONE, logger
from database import (
    create_controlled_task,
    format_db_datetime,
    get_all_group_chats,
    get_controlled_task_by_last_message,
    get_due_controlled_tasks,
    get_employee_alias,
    list_controlled_tasks,
    list_employee_aliases,
    mark_controlled_task_done,
    update_controlled_task_ping,
    upsert_employee_alias,
)
from group_utils import is_group_chat


DONE_WORDS = ("готово", "сделано", "выполнено", "закрыл", "закрыто", "закончил", "завершил")
FOLLOW_WORDS = ("уточняй", "спрашивай", "проверяй", "контролируй", "напоминай", "узнавай")
DAILY_WORDS = ("раз в день", "каждый день", "ежедневно", "ежедневный", "ежедневная")


def _is_admin_user(user_id: Optional[int]) -> bool:
    if not ADMIN_IDS:
        return True
    return bool(user_id and user_id in ADMIN_IDS)


def _clean_alias(value: str) -> str:
    value = (value or "").strip().lower().replace("@", "")
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^0-9a-zа-яё_\-]+", "", value, flags=re.IGNORECASE)
    return value.strip("_-")


def _normalize_text(value: str) -> str:
    return _clean_alias(value).replace("_", " ")


def _display_target(task: dict) -> str:
    username = (task.get("target_username") or "").strip().lstrip("@")
    name = (task.get("target_name") or "сотрудник").strip()
    if username:
        return f"@{username}"
    return name


def _extract_target(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Возвращает (имя, username)."""
    m_username = re.search(r"@([A-Za-z0-9_]{4,32})", text or "")
    username = m_username.group(1) if m_username else None

    patterns = [
        r"(?:чтобы|что\s*бы)\s+(@?[A-Za-zА-Яа-яЁё0-9_\-]+)",
        r"(?:уточняй|спрашивай|проверяй|контролируй|напоминай|узнавай)\s+у\s+(@?[A-Za-zА-Яа-яЁё0-9_\-]+)",
        r"(?:напиши|сообщи|передай)\s+(@?[A-Za-zА-Яа-яЁё0-9_\-]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text or "", flags=re.IGNORECASE)
        if m:
            raw = m.group(1).strip("@ ,.:;—-")
            if raw.lower() in {"мне", "ему", "ей", "нам", "них"}:
                continue
            if raw.startswith("@"):
                raw = raw[1:]
            name = raw
            if username:
                # Если в тексте есть @username, используем его для уведомления, а имя оставляем человекочитаемым.
                name = raw if not raw.startswith(username) else username
            return name, username

    if username:
        return username, username
    return None, None


def _extract_object(text: str) -> Optional[str]:
    m = re.search(r"по\s+объекту\s+([^.,;\n]+)", text or "", flags=re.IGNORECASE)
    if not m:
        return None
    obj = m.group(1).strip(" ,.:;—-")
    # Отсекаем хвосты вида «необходимо чтобы...»
    obj = re.split(r"\b(?:необходимо|нужно|надо|требуется)\b", obj, flags=re.IGNORECASE)[0].strip(" ,.:;—-")
    return obj or None


def _extract_deadline_text(text: str) -> Optional[str]:
    # Простая человекочитаемая фиксация срока. Это не дата для планировщика, а часть текста поручения.
    patterns = [
        r"\bк\s+([0-3]?\d\s+числ[ауо]?)",
        r"\bк\s+(понедельнику|вторнику|среде|четвергу|пятнице|субботе|воскресенью|пн|вт|ср|чт|пт|сб|вс)\b",
        r"\bдо\s+([0-3]?\d\s+числ[ауо]?)",
        r"\bдо\s+(понедельника|вторника|среды|четверга|пятницы|субботы|воскресенья|пн|вт|ср|чт|пт|сб|вс)\b",
        r"\bдо\s+(\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text or "", flags=re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return None


def _clean_task_text(text: str, target_name: Optional[str]) -> str:
    main = re.split(r"\b(?:пожалуйста\s+)?(?:уточняй|спрашивай|проверяй|контролируй|напоминай|узнавай)\b", text, flags=re.IGNORECASE)[0]
    main = main.strip(" .,:;—-\n")

    # Убираем вводную часть, оставляя смысл поручения.
    main = re.sub(r"^по\s+объекту\s+([^.,;\n]+?)\s+(?:необходимо|нужно|надо|требуется)\s+", "", main, flags=re.IGNORECASE)
    if target_name:
        tn = re.escape(target_name.lstrip("@"))
        main = re.sub(rf"^(?:чтобы|что\s*бы)\s+@?{tn}\s+", "", main, flags=re.IGNORECASE)
        main = re.sub(rf"^@?{tn}\s+", "", main, flags=re.IGNORECASE)

    replacements = {
        "подготовил": "подготовить",
        "сделал": "сделать",
        "закончил": "закончить",
        "проверил": "проверить",
        "отправил": "отправить",
        "посчитал": "посчитать",
        "оформил": "оформить",
    }
    words = main.split()
    if words:
        first = words[0].lower().strip(" ,.:;—-")
        if first in replacements:
            words[0] = replacements[first]
            main = " ".join(words)

    return main.strip(" .,:;—-") or text.strip()


def _find_target_chat(text: str) -> Tuple[Optional[dict], Optional[str], Optional[str]]:
    """Ищет привязанный чат по alias/названию в тексте.

    Возвращает (chat, alias, error_text). Если чат один и пользователь не указал alias,
    используем единственный чат. Если чатов несколько и alias не найден — возвращаем подсказку.
    """
    chats = get_all_group_chats()
    if not chats:
        return None, None, "Пока нет привязанных рабочих чатов. Добавьте бота в группу и выполните там: /bind_chat спартака"

    lowered = (text or "").lower()
    normalized = _normalize_text(lowered)
    candidates = []
    for chat in chats:
        alias = (chat.get("alias") or "").lower()
        title = (chat.get("title") or "").lower()
        alias_norm = _normalize_text(alias)
        title_norm = _normalize_text(title)
        score = 0
        if alias and re.search(rf"\b{re.escape(alias)}\b", lowered):
            score = max(score, 200 + len(alias))
        if alias_norm and alias_norm in normalized:
            score = max(score, 180 + len(alias_norm))
        if title_norm and title_norm in normalized:
            score = max(score, 120 + len(title_norm))
        if score:
            candidates.append((score, chat, alias or alias_norm))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1], candidates[0][2], None

    if len(chats) == 1:
        return chats[0], chats[0].get("alias"), None

    aliases = ", ".join(c.get("alias") or "—" for c in chats[:20])
    return None, None, (
        "Я поняла поручение, но не поняла, в какой рабочий чат писать. "
        f"Укажите alias чата в тексте. Сейчас привязаны: {aliases}\n\n"
        "Пример: По объекту спартака нужно, чтобы Дима подготовил РПЗ к пн. Уточняй у него раз в день."
    )


def looks_like_control_request(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in FOLLOW_WORDS) and (
        any(word in lowered for word in DAILY_WORDS)
        or "раз в" in lowered
        or "кажд" in lowered
        or "контрол" in lowered
    )


def _next_daily_time(hour: int = 10, minute: int = 0) -> datetime:
    now = datetime.now(TIMEZONE)
    next_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_at <= now + timedelta(seconds=30):
        next_at += timedelta(days=1)
    return next_at


def _first_message_text(task: dict) -> str:
    target = _display_target(task)
    lines = [f"{target}, поручение от руководителя:"]
    if task.get("object_name"):
        lines.append(f"Объект: {task['object_name']}.")
    lines.append(task.get("task_text") or "Поручение без описания")
    if task.get("deadline_text"):
        lines.append(f"Срок: {task['deadline_text']}.")
    lines.append("Я буду периодически уточнять статус по этому вопросу.")
    lines.append(f"ID контроля: #{task['id']}")
    return "\n".join(lines)


def _followup_message_text(task: dict) -> str:
    target = _display_target(task)
    lines = [f"{target}, уточните, пожалуйста, как продвигается поручение #{task['id']}:"]
    if task.get("object_name"):
        lines.append(f"Объект: {task['object_name']}.")
    lines.append(task.get("task_text") or "Поручение без описания")
    if task.get("deadline_text"):
        lines.append(f"Срок: {task['deadline_text']}.")
    lines.append("Если готово — ответьте на это сообщение: готово.")
    return "\n".join(lines)


async def maybe_handle_control_request(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Обрабатывает естественную команду из лички на постановку контроля."""
    if is_group_chat(update):
        return False
    if not looks_like_control_request(text):
        return False

    msg = update.effective_message
    user = update.effective_user
    if not _is_admin_user(user.id if user else None):
        await msg.reply_text("Ставить контроль поручений могут только администраторы бота из ADMIN_IDS.")
        return True

    chat, alias, error = _find_target_chat(text)
    if error:
        await msg.reply_text(error)
        return True

    target_name, target_username = _extract_target(text)
    if not target_name:
        await msg.reply_text(
            "Я поняла, что нужно контролировать поручение, но не поняла сотрудника.\n"
            "Надёжный формат: По объекту спартака нужно, чтобы Дима подготовил РПЗ к пн. "
            "Уточняй у него раз в день."
        )
        return True

    employee = get_employee_alias(target_name)
    if employee:
        target_username = target_username or employee.get("username")
        target_name = employee.get("display_name") or target_name

    object_name = _extract_object(text)
    deadline_text = _extract_deadline_text(text)
    task_text = _clean_task_text(text, target_name)
    next_check_at = _next_daily_time()

    task_id = create_controlled_task(
        created_by=user.id if user else None,
        target_name=target_name,
        target_username=target_username,
        target_user_id=employee.get("user_id") if employee else None,
        chat_id=chat["chat_id"],
        chat_alias=chat.get("alias") or alias,
        chat_title=chat.get("title"),
        object_name=object_name,
        task_text=task_text,
        deadline_text=deadline_text,
        cadence_days=1,
        next_check_at=next_check_at,
    )

    task = {
        "id": task_id,
        "target_name": target_name,
        "target_username": target_username,
        "object_name": object_name,
        "task_text": task_text,
        "deadline_text": deadline_text,
    }

    try:
        sent = await context.bot.send_message(chat_id=chat["chat_id"], text=_first_message_text(task))
        # Первое сообщение тоже запоминаем: если сотрудник ответит «готово», контроль закроется.
        update_controlled_task_ping(task_id, next_check_at=next_check_at, last_message_id=sent.message_id, last_check_at=None)
        await msg.reply_text(
            f"✅ Поручение #{task_id} поставлено на контроль.\n"
            f"Чат: {chat.get('title') or chat.get('alias')}\n"
            f"Сотрудник: {_display_target(task)}\n"
            f"Следующее уточнение: {format_db_datetime(next_check_at)}"
        )
    except Exception as exc:
        logger.error("Не удалось отправить первое сообщение по поручению в чат %s: %s", chat.get("chat_id"), exc)
        mark_controlled_task_done(task_id, status="send_error")
        await msg.reply_text(
            "Поручение распознано, но я не смогла написать в рабочий чат. "
            "Проверьте, что бот добавлен в группу и имеет право отправлять сообщения."
        )
    return True


async def followups_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_admin_user(user.id if user else None):
        await update.effective_message.reply_text("Список контролей доступен только администраторам бота.")
        return

    tasks = list_controlled_tasks(status="active", limit=30)
    if not tasks:
        await update.effective_message.reply_text("Активных контролей поручений нет.")
        return

    lines = ["📌 Активные контроли поручений:"]
    for task in tasks:
        target = _display_target(task)
        chat = task.get("chat_alias") or task.get("chat_title") or task.get("chat_id")
        lines.append(
            f"#{task['id']} — {target}, чат {chat}, след. уточнение {format_db_datetime(task.get('next_check_at'))}\n"
            f"   {task.get('task_text') or '—'}"
        )
    await update.effective_message.reply_text("\n".join(lines))


async def stop_followup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_admin_user(user.id if user else None):
        await update.effective_message.reply_text("Останавливать контроли могут только администраторы бота.")
        return
    if not context.args:
        await update.effective_message.reply_text("Использование: /stop_followup ID")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("ID должен быть числом")
        return
    changed = mark_controlled_task_done(task_id, status="stopped")
    if changed:
        await update.effective_message.reply_text(f"✅ Контроль поручения #{task_id} остановлен.")
    else:
        await update.effective_message.reply_text(f"Поручение #{task_id} не найдено.")


async def bind_employee_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_admin_user(user.id if user else None):
        await update.effective_message.reply_text("Привязывать сотрудников могут только администраторы бота.")
        return
    if len(context.args or []) < 2:
        await update.effective_message.reply_text("Использование: /bind_employee Дима @username")
        return
    alias = context.args[0].strip()
    username = context.args[1].strip().lstrip("@")
    display_name = alias.strip()
    upsert_employee_alias(alias=alias, username=username, display_name=display_name, user_id=None, created_by=user.id if user else None)
    await update.effective_message.reply_text(
        f"✅ Сотрудник привязан: {alias} → @{username}\n"
        "Теперь в поручениях можно писать просто имя, а бот будет упоминать username."
    )


async def employees_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_admin_user(user.id if user else None):
        await update.effective_message.reply_text("Список сотрудников доступен только администраторам бота.")
        return
    employees = list_employee_aliases()
    if not employees:
        await update.effective_message.reply_text("Пока нет привязанных сотрудников. Пример: /bind_employee Дима @username")
        return
    lines = ["👤 Привязанные сотрудники:"]
    for emp in employees:
        alias = emp.get("alias")
        username = emp.get("username")
        display_name = emp.get("display_name") or alias
        lines.append(f"• {display_name} ({alias}) → @{username}" if username else f"• {display_name} ({alias})")
    await update.effective_message.reply_text("\n".join(lines))


async def maybe_handle_assignment_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Если сотрудник ответил «готово» на контрольное сообщение бота — закрываем контроль."""
    if not is_group_chat(update):
        return False
    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        return False
    lowered = (text or "").strip().lower()
    if not any(word in lowered for word in DONE_WORDS):
        return False

    task = get_controlled_task_by_last_message(update.effective_chat.id, msg.reply_to_message.message_id)
    if not task:
        return False

    mark_controlled_task_done(task["id"], status="done")
    await msg.reply_text(f"✅ Зафиксировала: поручение #{task['id']} закрыто как выполненное.")
    return True


async def check_controlled_tasks(app) -> None:
    now = datetime.now(TIMEZONE)
    due_tasks = get_due_controlled_tasks(now)
    for task in due_tasks:
        try:
            sent = await app.bot.send_message(chat_id=task["chat_id"], text=_followup_message_text(task))
            next_at = now + timedelta(days=int(task.get("cadence_days") or 1))
            # Ставим следующую проверку на то же время суток, но завтра/через cadence_days.
            next_at = next_at.replace(second=0, microsecond=0)
            update_controlled_task_ping(
                task["id"],
                next_check_at=next_at,
                last_message_id=sent.message_id,
                last_check_at=now,
            )
        except Exception as exc:
            logger.error("Не удалось отправить контроль поручения #%s: %s", task.get("id"), exc)
            # Не закрываем контроль навсегда, попробуем ещё раз позже через час.
            update_controlled_task_ping(
                task["id"],
                next_check_at=now + timedelta(hours=1),
                last_message_id=task.get("last_message_id"),
                last_check_at=task.get("last_check_at"),
            )


def schedule_controlled_tasks_checker(app) -> None:
    job_queue = app.job_queue
    if not job_queue:
        logger.warning("JobQueue не запущен. Установите python-telegram-bot[job-queue]")
        return

    async def callback(_):
        await check_controlled_tasks(app)

    job_queue.run_repeating(callback, interval=60, first=15)
    logger.info("Планировщик контроля поручений запущен")
