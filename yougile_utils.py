import asyncio
import os
from datetime import time
from typing import Any, Dict, List, Optional

import requests
from telegram import BotCommand, Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS, TIMEZONE, logger
from internet_search import split_telegram_text


YOUGILE_API_BASE = os.getenv("YOUGILE_API_BASE", "https://ru.yougile.com/api-v2").rstrip("/")
YOUGILE_API_KEY = os.getenv("YOUGILE_API_KEY", "")
YOUGILE_COMPANY_ID = os.getenv("YOUGILE_COMPANY_ID", "")
YOUGILE_DEFAULT_COLUMN_ID = os.getenv("YOUGILE_DEFAULT_COLUMN_ID", "")
YOUGILE_DAILY_SUMMARY_TIME = os.getenv("YOUGILE_DAILY_SUMMARY_TIME", "").strip()


class YouGileError(Exception):
    pass


def _is_configured() -> bool:
    return bool(YOUGILE_API_KEY and YOUGILE_API_BASE)


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {YOUGILE_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _request(method: str, resource: str, params: Optional[dict] = None, json_body: Optional[dict] = None) -> Any:
    if not _is_configured():
        raise YouGileError("Не настроен YOUGILE_API_KEY или YOUGILE_API_BASE в Railway.")

    url = f"{YOUGILE_API_BASE}/{resource.lstrip('/')}"
    try:
        response = requests.request(method, url, headers=_headers(), params=params, json=json_body, timeout=60)
        if response.status_code >= 400:
            detail = response.text[:700]
            raise YouGileError(f"YouGile API вернул HTTP {response.status_code}: {detail}")
        if not response.text:
            return {}
        return response.json()
    except requests.RequestException as exc:
        raise YouGileError(f"Не удалось обратиться к YouGile API: {exc}") from exc
    except ValueError as exc:
        raise YouGileError("YouGile API вернул ответ не в JSON-формате.") from exc


def _content(payload: Any) -> List[dict]:
    if isinstance(payload, dict):
        items = payload.get("content")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _list_resource(resource: str, limit: int = 100, extra_params: Optional[dict] = None) -> List[dict]:
    items: List[dict] = []
    offset = 0
    page_limit = min(max(int(limit or 100), 1), 100)

    while len(items) < limit:
        params = {"limit": page_limit, "offset": offset}
        if extra_params:
            params.update({k: v for k, v in extra_params.items() if v not in (None, "")})
        payload = _request("GET", resource, params=params)
        page = _content(payload)
        items.extend(page)
        paging = payload.get("paging") if isinstance(payload, dict) else {}
        if not page or not paging or not paging.get("next"):
            break
        offset += page_limit

    return items[:limit]


def yougile_projects(limit: int = 100) -> List[dict]:
    return _list_resource("projects", limit=limit)


def yougile_boards(limit: int = 100) -> List[dict]:
    return _list_resource("boards", limit=limit)


def yougile_columns(limit: int = 200) -> List[dict]:
    return _list_resource("columns", limit=limit)


def yougile_tasks(limit: int = 100) -> List[dict]:
    return _list_resource("tasks", limit=limit)


def yougile_create_task(title: str, column_id: str, description: str = "") -> dict:
    body = {"title": title, "columnId": column_id}
    if description:
        body["description"] = description
    return _request("POST", "tasks", json_body=body)


def _text_value(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None:
            return str(value)
    return ""


def _task_title(task: dict) -> str:
    return _text_value(task, "title", "name") or "без названия"


def _task_id(task: dict) -> str:
    return _text_value(task, "id") or "без id"


def _task_status(task: dict) -> str:
    column = task.get("column")
    if isinstance(column, dict):
        return _text_value(column, "title", "name", "id")
    return _text_value(task, "columnId", "status", "archived") or "без статуса"


def _task_deadline(task: dict) -> str:
    deadline = task.get("deadline")
    if isinstance(deadline, dict):
        return _text_value(deadline, "deadline", "date", "value", "timestamp")
    return _text_value(task, "deadline", "dueDate", "date")


def _format_task_line(task: dict) -> str:
    title = _task_title(task)
    status = _task_status(task)
    deadline = _task_deadline(task)
    suffix = f" · срок: {deadline}" if deadline else ""
    return f"#{_task_id(task)} {title} · {status}{suffix}"


def _matches_task(task: dict, query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(str(v) for v in task.values() if isinstance(v, (str, int, float))).lower()
    return all(part in haystack for part in query.lower().split())


def build_yougile_tasks_report(query: str = "", limit: int = 25) -> str:
    tasks = [task for task in yougile_tasks(limit=200) if _matches_task(task, query)]
    if not tasks:
        return "В YouGile не нашла задач по этому запросу." if query else "В YouGile пока не нашла задач."

    lines = [f"Задачи YouGile: {len(tasks)} найдено"]
    for task in tasks[:limit]:
        lines.append(_format_task_line(task))
    if len(tasks) > limit:
        lines.append(f"... показаны первые {limit} из {len(tasks)}")
    return "\n".join(lines)


def build_yougile_structure_report() -> str:
    projects = yougile_projects(limit=100)
    boards = yougile_boards(limit=100)
    columns = yougile_columns(limit=200)

    lines = ["Структура YouGile"]
    lines.append(f"Проекты: {len(projects)}")
    for project in projects[:15]:
        lines.append(f"- {_text_value(project, 'title', 'name') or 'без названия'} · id={project.get('id')}")

    lines.append(f"\nДоски: {len(boards)}")
    for board in boards[:15]:
        lines.append(f"- {_text_value(board, 'title', 'name') or 'без названия'} · id={board.get('id')}")

    lines.append(f"\nКолонки: {len(columns)}")
    for column in columns[:30]:
        lines.append(f"- {_text_value(column, 'title', 'name') or 'без названия'} · id={column.get('id')}")

    lines.append("\nДля создания задачи нужен id колонки. Его можно указать в Railway как YOUGILE_DEFAULT_COLUMN_ID.")
    return "\n".join(lines)


def build_yougile_summary() -> str:
    tasks = yougile_tasks(limit=200)
    if not tasks:
        return "YouGile подключен, но задачи не найдены."

    active = [task for task in tasks if not task.get("archived")]
    with_deadline = [task for task in active if _task_deadline(task)]
    lines = [
        "Сводка YouGile",
        f"Всего задач в выборке: {len(tasks)}",
        f"Не архивных: {len(active)}",
        f"Со сроками: {len(with_deadline)}",
        "",
        "Последние/актуальные задачи:",
    ]
    for task in active[:15]:
        lines.append(_format_task_line(task))
    return "\n".join(lines)


def _parse_create_args(text: str) -> tuple[str, str, str]:
    raw = (text or "").strip()
    if "|" in raw:
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) >= 2:
            column_id = parts[0] or YOUGILE_DEFAULT_COLUMN_ID
            title = parts[1]
            description = " | ".join(parts[2:]).strip() if len(parts) > 2 else ""
            return column_id, title, description
    return YOUGILE_DEFAULT_COLUMN_ID, raw, ""


async def yougile_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not _is_configured():
            await update.effective_message.reply_text("YouGile не настроен: добавьте YOUGILE_API_KEY в Railway.")
            return
        summary = await asyncio.to_thread(build_yougile_summary)
        for part in split_telegram_text(summary):
            await update.effective_message.reply_text(part)
    except YouGileError as exc:
        await update.effective_message.reply_text(f"Ошибка YouGile: {exc}")


async def yougile_structure_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        report = await asyncio.to_thread(build_yougile_structure_report)
        for part in split_telegram_text(report):
            await update.effective_message.reply_text(part)
    except YouGileError as exc:
        await update.effective_message.reply_text(f"Ошибка YouGile: {exc}")


async def yougile_tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args or []).strip()
    try:
        report = await asyncio.to_thread(build_yougile_tasks_report, query, 30)
        for part in split_telegram_text(report):
            await update.effective_message.reply_text(part)
    except YouGileError as exc:
        await update.effective_message.reply_text(f"Ошибка YouGile: {exc}")


async def yougile_create_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args or []).strip()
    if not text:
        await update.effective_message.reply_text(
            "Формат: /yg_create columnId | Название задачи | Описание\n"
            "Если задан YOUGILE_DEFAULT_COLUMN_ID, можно проще: /yg_create Название задачи"
        )
        return

    column_id, title, description = _parse_create_args(text)
    if not column_id:
        await update.effective_message.reply_text(
            "Не знаю, в какую колонку YouGile создать задачу. Укажите columnId перед названием через | "
            "или задайте YOUGILE_DEFAULT_COLUMN_ID в Railway. Колонки можно посмотреть командой /yg_structure."
        )
        return
    if not title:
        await update.effective_message.reply_text("Не вижу название задачи.")
        return

    try:
        task = await asyncio.to_thread(yougile_create_task, title, column_id, description)
        await update.effective_message.reply_text(
            f"Создала задачу в YouGile: {_task_title(task)}\nID: {_task_id(task)}"
        )
    except YouGileError as exc:
        await update.effective_message.reply_text(f"Ошибка YouGile: {exc}")


def looks_like_yougile_request(text: str) -> bool:
    lowered = (text or "").lower()
    return "yougile" in lowered or "юджайл" in lowered or "югейл" in lowered


async def maybe_handle_yougile_request(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not looks_like_yougile_request(text):
        return False

    lowered = text.lower()
    try:
        if any(word in lowered for word in ("структур", "доск", "колон", "проект")):
            report = await asyncio.to_thread(build_yougile_structure_report)
        elif any(word in lowered for word in ("покажи", "список", "какие", "задач", "статус", "сводк")):
            report = await asyncio.to_thread(build_yougile_tasks_report, "", 25)
        else:
            return False

        for part in split_telegram_text(report):
            await update.effective_message.reply_text(part)
        return True
    except YouGileError as exc:
        await update.effective_message.reply_text(f"Ошибка YouGile: {exc}")
        return True


def yougile_bot_commands() -> List[BotCommand]:
    return [
        BotCommand("yg_status", "Сводка YouGile"),
        BotCommand("yg_structure", "Проекты, доски и колонки YouGile"),
        BotCommand("yg_tasks", "Список/поиск задач YouGile"),
        BotCommand("yg_create", "Создать задачу YouGile"),
    ]


async def send_yougile_daily_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_IDS or not _is_configured():
        return

    try:
        summary = await asyncio.to_thread(build_yougile_summary)
    except Exception as exc:
        logger.exception("YouGile daily summary error: %s", exc)
        summary = f"Не удалось получить сводку YouGile: {exc}"

    for admin_id in ADMIN_IDS:
        try:
            for part in split_telegram_text(summary):
                await context.bot.send_message(chat_id=admin_id, text=part)
        except Exception:
            logger.exception("Не удалось отправить YouGile-сводку админу %s", admin_id)


def schedule_yougile_checker(app) -> None:
    if not YOUGILE_DAILY_SUMMARY_TIME:
        return

    try:
        hour, minute = [int(part) for part in YOUGILE_DAILY_SUMMARY_TIME.split(":", 1)]
        run_time = time(hour=hour, minute=minute, tzinfo=TIMEZONE)
    except Exception:
        logger.warning("Некорректное YOUGILE_DAILY_SUMMARY_TIME=%s, ожидалось HH:MM", YOUGILE_DAILY_SUMMARY_TIME)
        return

    app.job_queue.run_daily(send_yougile_daily_summary, time=run_time, name="yougile_daily_summary")
