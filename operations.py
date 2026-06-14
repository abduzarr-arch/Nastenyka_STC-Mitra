"""Операционное ядро бота Настеньки.

Модуль добавляет уровень выше обычного чата:
- проекты/объекты;
- операционные задачи и подзадачи;
- статусы и история обновлений;
- общая память рабочих групп без смешивания личных ответов пользователей;
- ежедневный контроль задач.

Идея: бот не должен просто помнить длинную переписку. Он должен переводить
рабочие сообщения в структурированные сущности, а в диалоге давать модели
только компактный релевантный контекст.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict, Any

from telegram import BotCommand, Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS, TIMEZONE, logger
from database import (
    create_operational_task,
    create_project,
    create_task_update,
    get_all_group_chats,
    get_employee_alias,
    get_group_chat_by_alias,
    get_operational_task,
    get_operational_task_by_last_message,
    get_operational_tasks,
    get_project_by_alias_or_name,
    get_projects,
    get_recent_team_events,
    get_task_updates,
    get_due_operational_tasks,
    mark_operational_task_done,
    save_team_memory_event,
    update_operational_task_control_ping,
    update_operational_task_status,
)
from group_utils import is_group_chat

DONE_WORDS = ("готово", "сделано", "выполнено", "завершено", "закрыл", "закрыто", "закончил")
CREATE_WORDS = ("поставь", "создай", "добавь", "заведи", "назначь", "необходимо", "нужно", "надо", "требуется")
STATUS_WORDS = ("статус", "что у нас", "как дела", "что по", "прогресс", "сводка", "горящ")
SUBTASK_WORDS = ("разбей", "подзадач", "этап", "план работ", "структур")
CONTROL_WORDS = ("контролируй", "уточняй", "спрашивай", "проверяй", "напоминай", "узнавай")

NO_CONTROL_WORDS = ("РЅРµ РєРѕРЅС‚СЂРѕР»РёСЂСѓР№", "Р±РµР· РєРѕРЅС‚СЂРѕР»СЏ", "РЅРµ СЃРїСЂР°С€РёРІР°Р№", "РЅРµ СѓС‚РѕС‡РЅСЏР№")
MANAGER_SUMMARY_WORDS = ("СЃРІРѕРґРєР° СЂСѓРєРѕРІРѕРґРёС‚РµР»СЏ", "С‡С‚Рѕ РєРѕРЅС‚СЂРѕР»РёСЂРѕРІР°С‚СЊ", "С‡С‚Рѕ РіРѕСЂРёС‚", "РіРґРµ СЂРёСЃРєРё", "Р±РµР· СЃС‚Р°С‚СѓСЃР°")
DONE_WORDS_RU = ("готово", "сделано", "выполнено", "завершено", "закрыл", "закрыто", "закончил")
BLOCKED_WORDS_RU = ("не начинал", "не приступал", "жду исходные", "блокер", "не могу", "заблокировано")
ACTIVE_WORDS_RU = ("в работе", "делаю", "занимаюсь", "готовлю", "процесс")
NO_CONTROL_WORDS = NO_CONTROL_WORDS + ("не контролируй", "без контроля", "не спрашивай", "не уточняй")
MANAGER_SUMMARY_WORDS = MANAGER_SUMMARY_WORDS + ("сводка руководителя", "что контролировать", "что горит", "где риски", "без статуса")
STATUS_WORDS = STATUS_WORDS + ("статус", "что у нас", "как дела", "что по", "прогресс", "сводка", "горящ", "горит", "риски")

DEFAULT_RPZ_SUBTASKS = [
    "Собрать и проверить исходные данные",
    "Сформировать структуру РПЗ",
    "Описать конструктивную схему объекта",
    "Описать нагрузки и исходные расчетные предпосылки",
    "Описать расчетную модель и принятые допущения",
    "Подготовить раздел с результатами расчета",
    "Описать оптимизацию/снижение объемов, если применимо",
    "Сформировать выводы и рекомендации",
    "Проверить оформление и комплектность",
    "Передать РПЗ на внутреннюю проверку",
]


def _now() -> datetime:
    return datetime.now(TIMEZONE)


def _is_admin_user(user_id: Optional[int]) -> bool:
    # Если ADMIN_IDS не задан, не блокируем прототип. В рабочем режиме лучше задать ADMIN_IDS.
    if not ADMIN_IDS:
        return True
    return bool(user_id and user_id in ADMIN_IDS)


def _clean_alias(value: str) -> str:
    value = (value or "").strip().lower().replace("@", "")
    value = value.replace("ё", "е")
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^0-9a-zа-я_\-]+", "", value, flags=re.IGNORECASE)
    return value.strip("_-")


def _human_user(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "участник"
    return " ".join(part for part in [user.first_name, user.last_name] if part) or user.username or str(user.id)


def _format_dt(value, empty: str = "—") -> str:
    if not value:
        return empty
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return value
    else:
        dt = value
    if dt.tzinfo is None:
        dt = TIMEZONE.localize(dt)
    return dt.astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")


def _contains_any(text: str, words: tuple) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in words)


def _extract_task_id(text: str) -> Optional[int]:
    m = re.search(r"#\s*(\d+)", text or "")
    if not m:
        m = re.search(r"(?:задач[аиуе]|task|id)\s*[:#]?\s*(\d+)", text or "", flags=re.IGNORECASE)
    return int(m.group(1)) if m else None


def _risk_from_text(text: str) -> str:
    lowered = (text or "").lower()
    high_words = ("срыв", "не смогу", "критич", "авари", "не успею", "остановилось")
    mid_words = ("проблем", "не успева", "блок", "жду", "задерж", "не хватает", "нет исходн")
    if any(word in lowered for word in high_words):
        return "высокий"
    if any(word in lowered for word in mid_words):
        return "средний"
    return "низкий"


def _days_since(value) -> Optional[int]:
    dt = value
    if not dt:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = TIMEZONE.localize(dt)
    return (_now() - dt.astimezone(TIMEZONE)).days


def _is_stale_task(task: dict, days: int = 2) -> bool:
    if task.get("status") == "done":
        return False
    last_status_at = task.get("last_status_at")
    if not last_status_at:
        created_days = _days_since(task.get("created_at"))
        return created_days is not None and created_days >= days
    status_days = _days_since(last_status_at)
    return status_days is not None and status_days >= days


def _build_yougile_manager_block() -> str:
    if os.getenv("YOUGILE_INCLUDE_IN_MANAGER_SUMMARY", "1").strip().lower() in {"0", "false", "no", "off"}:
        return ""
    try:
        from yougile_utils import build_yougile_manager_summary
        return build_yougile_manager_summary()
    except Exception as exc:
        logger.exception("Failed to build YouGile manager summary: %s", exc)
        return f"YouGile: не удалось получить данные: {exc}"


def _extract_deadline_text(text: str) -> Optional[str]:
    patterns = [
        r"\bк\s+([0-3]?\d\s+числ[ауо]?)",
        r"\bдо\s+([0-3]?\d\s+числ[ауо]?)",
        r"\bк\s+(понедельнику|вторнику|среде|четвергу|пятнице|субботе|воскресенью|пн|вт|ср|чт|пт|сб|вс)\b",
        r"\bдо\s+(понедельника|вторника|среды|четверга|пятницы|субботы|воскресенья|пн|вт|ср|чт|пт|сб|вс)\b",
        r"\bдо\s+(\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)",
        r"\bк\s+(\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text or "", flags=re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return None


def _extract_project_name(text: str) -> Optional[str]:
    patterns = [
        r"по\s+объекту\s+([^.,;\n]+)",
        r"по\s+проекту\s+([^.,;\n]+)",
        r"в\s+проекте\s+([^.,;\n]+)",
        r"объект\s+([^.,;\n]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text or "", flags=re.IGNORECASE)
        if m:
            obj = m.group(1).strip(" ,.:;—-")
            obj = re.split(r"\b(?:нужно|надо|необходимо|требуется|чтобы|что\s*бы)\b", obj, flags=re.IGNORECASE)[0]
            obj = obj.strip(" ,.:;—-")
            if obj:
                return obj
    return None


def _extract_chat_alias(text: str) -> Optional[str]:
    patterns = [
        r"в\s+чат\s+([A-Za-zА-Яа-яЁё0-9_\-]+)",
        r"в\s+чате\s+([A-Za-zА-Яа-яЁё0-9_\-]+)",
        r"чат\s+([A-Za-zА-Яа-яЁё0-9_\-]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text or "", flags=re.IGNORECASE)
        if m:
            return _clean_alias(m.group(1))
    return None


def _extract_target(text: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Возвращает display_name, username, user_id если известны."""
    username_match = re.search(r"@([A-Za-z0-9_]{4,32})", text or "")
    username = username_match.group(1) if username_match else None

    patterns = [
        r"(?:чтобы|что\s*бы)\s+(@?[A-Za-zА-Яа-яЁё0-9_\-]+)",
        r"(?:для|за)\s+(@?[A-Za-zА-Яа-яЁё0-9_\-]+)\s+(?:нужно|надо|необходимо)",
        r"(?:напиши|сообщи|передай|уточняй|спрашивай|проверяй)\s+(?:у\s+)?(@?[A-Za-zА-Яа-яЁё0-9_\-]+)",
        r"ответственный\s*[:\-—]?\s*(@?[A-Za-zА-Яа-яЁё0-9_\-]+)",
    ]
    name = None
    for pattern in patterns:
        m = re.search(pattern, text or "", flags=re.IGNORECASE)
        if m:
            raw = m.group(1).strip("@ ,.:;—-")
            if raw.lower() not in {"мне", "ему", "ей", "нам", "них", "него", "нее"}:
                name = raw
                break

    if not name and username:
        name = username
    if not name:
        return None, username, None

    alias = get_employee_alias(name) or (get_employee_alias(username) if username else None)
    if alias:
        return alias.get("display_name") or name, alias.get("username") or username, alias.get("user_id")
    return name, username, None


def _extract_task_title(text: str, target_name: Optional[str] = None) -> str:
    source = text.strip()
    # Убираем хвост контроля.
    source = re.split(r"\b(?:пожалуйста\s+)?(?:уточняй|спрашивай|проверяй|контролируй|напоминай|узнавай)\b", source, flags=re.IGNORECASE)[0]
    # Убираем адресацию чата.
    source = re.sub(r"\b(?:в\s+)?чат(?:е)?\s+[A-Za-zА-Яа-яЁё0-9_\-]+[:,]?\s*", "", source, flags=re.IGNORECASE)
    # Убираем вводную про объект.
    source = re.sub(r"^по\s+(?:объекту|проекту)\s+[^.,;\n]+?\s+(?:необходимо|нужно|надо|требуется)\s+", "", source, flags=re.IGNORECASE)
    if target_name:
        source = re.sub(rf"^(?:чтобы|что\s*бы)\s+@?{re.escape(target_name)}\s+", "", source, flags=re.IGNORECASE)
    source = re.sub(r"^(?:поставь|создай|добавь|заведи|назначь)\s+(?:задачу|поручение)?\s*[:\-—]?\s*", "", source, flags=re.IGNORECASE)
    source = source.strip(" .,:;—-\n")
    if len(source) > 180:
        source = source[:177].rstrip() + "..."
    return source or "Операционная задача"


def _extract_progress(text: str) -> Optional[int]:
    m = re.search(r"(\d{1,3})\s*%", text or "")
    if not m:
        m = re.search(r"прогресс\s*[:\-—]?\s*(\d{1,3})", text or "", flags=re.IGNORECASE)
    if m:
        value = max(0, min(100, int(m.group(1))))
        return value
    lowered = (text or "").lower()
    if any(word in lowered for word in DONE_WORDS) or any(word in lowered for word in DONE_WORDS_RU):
        return 100
    return None


def _status_from_text(text: str, progress: Optional[int] = None) -> str:
    lowered = (text or "").lower()
    if progress == 100 or any(word in lowered for word in DONE_WORDS) or any(word in lowered for word in DONE_WORDS_RU):
        return "done"
    if any(w in lowered for w in BLOCKED_WORDS_RU):
        return "blocked"
    if any(w in lowered for w in ("не начинал", "не приступал", "жду исходные", "блокер", "не могу")):
        return "blocked"
    if any(w in lowered for w in ("в работе", "делаю", "занимаюсь", "готовлю", "процесс")):
        return "active"
    return "active"


def _risk_from_task(task: dict) -> str:
    progress = int(task.get("progress") or 0)
    status = task.get("status") or "active"
    last_status_at = task.get("last_status_at")
    if status == "done" or progress >= 100:
        return "низкий"
    if status == "blocked":
        return "высокий"
    if progress < 30 and task.get("deadline_text"):
        return "средний"
    # Если давно не было обновления — риск повышается.
    if last_status_at:
        try:
            dt = datetime.fromisoformat(str(last_status_at)) if isinstance(last_status_at, str) else last_status_at
            if dt.tzinfo is None:
                dt = TIMEZONE.localize(dt)
            if (_now() - dt).days >= 2:
                return "средний"
        except Exception:
            pass
    return "низкий"


def _effective_risk(task: dict) -> str:
    progress = int(task.get("progress") or 0)
    status = task.get("status") or "active"
    explicit = task.get("risk_level")
    if status == "done" or progress >= 100:
        return "низкий"
    if status == "blocked":
        return "высокий"
    if explicit in {"средний", "высокий"}:
        return explicit
    if progress < 30 and task.get("deadline_text"):
        return "средний"
    if _is_stale_task(task, days=2):
        return "средний"
    return "низкий"


def _find_or_create_project(name: Optional[str], created_by: Optional[int] = None, chat_alias: Optional[str] = None) -> Optional[dict]:
    if not name:
        return None
    project = get_project_by_alias_or_name(name)
    if project:
        return project
    alias = _clean_alias(name)
    project_id = create_project(alias=alias, name=name.strip(), description=None, default_chat_alias=chat_alias, created_by=created_by)
    return get_project_by_alias_or_name(alias) or {"id": project_id, "alias": alias, "name": name}


def _choose_chat_for_task(text: str, project: Optional[dict], update: Update) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    # 1) Явный alias в тексте.
    alias = _extract_chat_alias(text)
    if alias:
        chat = get_group_chat_by_alias(alias)
        if chat:
            return chat["chat_id"], chat["alias"], chat.get("title")
    # 2) default_chat_alias у проекта.
    if project and project.get("default_chat_alias"):
        chat = get_group_chat_by_alias(project["default_chat_alias"])
        if chat:
            return chat["chat_id"], chat["alias"], chat.get("title")
    # 3) Если команда дана прямо в группе — привязываем к текущей группе.
    if is_group_chat(update) and update.effective_chat:
        chat = update.effective_chat
        return chat.id, None, chat.title
    # 4) Если проект похож на alias чата — используем его.
    if project:
        chat = get_group_chat_by_alias(project.get("alias") or "")
        if chat:
            return chat["chat_id"], chat["alias"], chat.get("title")
    # 5) Если один групповой чат привязан — используем его как fallback.
    chats = get_all_group_chats()
    if len(chats) == 1:
        chat = chats[0]
        return chat["chat_id"], chat["alias"], chat.get("title")
    return None, None, None


def looks_like_task_creation(text: str) -> bool:
    lowered = (text or "").lower()
    has_create = any(word in lowered for word in CREATE_WORDS)
    has_person = bool(re.search(r"(?:чтобы|что\s*бы|напиши|сообщи|ответственный)\s+@?[A-Za-zА-Яа-яЁё0-9_\-]+", text or "", flags=re.IGNORECASE))
    has_task = any(word in lowered for word in ("задач", "поруч", "сдел", "подготов", "законч", "рпз", "смет", "расчет", "расчёт"))
    return has_create and (has_person or has_task)


def looks_like_status_request(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in STATUS_WORDS) and any(word in lowered for word in ("задач", "объект", "проект", "сотруд", "дима", "егор", "вадим", "все", "сегодня", "недел", "рпз", "смет", "расчет", "расчёт"))


def looks_like_subtask_request(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in SUBTASK_WORDS) and ("задач" in lowered or "рпз" in lowered or re.search(r"#\d+", lowered))


def looks_like_task_status_update(text: str) -> bool:
    lowered = (text or "").lower()
    if not _extract_task_id(text):
        return False
    status_markers = (
        "%", "готов", "сделан", "выполн", "в работе", "делаю", "жду", "проблем", "задерж",
        "не усп", "блок", "исходн", "статус", "прогресс", "закрыл", "закрыто",
    )
    return any(marker in lowered for marker in status_markers)


def _task_line(task: dict, show_project: bool = True) -> str:
    responsible = task.get("assigned_to_name") or task.get("assigned_to_username") or "не назначен"
    if task.get("assigned_to_username"):
        responsible = f"@{task['assigned_to_username']}"
    project = f" · {task.get('project_name')}" if show_project and task.get("project_name") else ""
    deadline = f" · срок {task.get('deadline_text')}" if task.get("deadline_text") else ""
    risk = _effective_risk(task)
    return f"#{task['id']} {task['title']}{project} · {responsible} · {task.get('progress', 0)}% · {task.get('status')}{deadline} · риск: {risk}"


def build_tasks_report(filter_text: Optional[str] = None, limit: int = 25) -> str:
    text = filter_text or ""
    project_name = _extract_project_name(text)
    employee_name = None
    target_name, username, _ = _extract_target(text)
    if target_name:
        employee_name = target_name
    elif "вадим" in text.lower():
        employee_name = "вадим"
    elif "дима" in text.lower():
        employee_name = "дима"
    elif "егор" in text.lower():
        employee_name = "егор"

    project_id = None
    if project_name:
        project = get_project_by_alias_or_name(project_name)
        if project:
            project_id = project["id"]

    tasks = get_operational_tasks(project_id=project_id, assigned_to=employee_name, status=None, limit=limit)
    active = [t for t in tasks if t.get("status") != "done"]
    done = [t for t in tasks if t.get("status") == "done"]

    if not tasks:
        return "По этому фильтру задач пока не нашла. Можно создать задачу обычным текстом или командой /op_task."

    lines = ["📊 Статус задач:"]
    if active:
        lines.append("\nАктивные:")
        for task in active[:limit]:
            lines.append("— " + _task_line(task))
    if done:
        lines.append("\nЗавершённые:")
        for task in done[:5]:
            lines.append("— " + _task_line(task))

    risky = [t for t in active if _effective_risk(t) in {"средний", "высокий"}]
    if risky:
        lines.append("\n⚠️ Требуют внимания:")
        for task in risky[:8]:
            lines.append("— " + _task_line(task))
    return "\n".join(lines)


def build_manager_summary(limit: int = 40) -> str:
    tasks = get_operational_tasks(status=None, parent_task_id_marker='top', limit=300)
    active = [t for t in tasks if t.get("status") != "done"]
    done = [t for t in tasks if t.get("status") == "done"]
    risky = [t for t in active if _effective_risk(t) == "высокий"]
    attention = [t for t in active if _effective_risk(t) == "средний"]
    stale = [t for t in active if _is_stale_task(t, days=2)]
    due_control = [
        t for t in active
        if t.get("control_enabled") and t.get("next_check_at") and t.get("next_check_at") <= _now()
    ]

    by_project: Dict[str, int] = {}
    for task in active:
        project = task.get("project_name") or "без объекта"
        by_project[project] = by_project.get(project, 0) + 1
    yougile_block = _build_yougile_manager_block()

    lines = [
        "Сводка руководителя",
        f"Активных задач: {len(active)} · завершенных: {len(done)} · высокий риск: {len(risky)} · без свежего статуса: {len(stale)}",
    ]

    if due_control:
        lines.append("\nНужно запросить статус сейчас:")
        for task in due_control[:8]:
            lines.append("— " + _task_line(task))

    if risky:
        lines.append("\nВысокий риск:")
        for task in risky[:8]:
            lines.append("— " + _task_line(task))

    if stale:
        lines.append("\nНет свежего статуса больше 2 дней:")
        for task in stale[:10]:
            last = _format_dt(task.get("last_status_at"), "статуса еще не было")
            lines.append(f"— {_task_line(task)} · последний статус: {last}")

    if attention:
        lines.append("\nСредний риск / требует внимания:")
        for task in attention[:8]:
            if task not in stale:
                lines.append("— " + _task_line(task))

    if by_project:
        lines.append("\nАктивные задачи по объектам:")
        for project, count in sorted(by_project.items(), key=lambda item: item[1], reverse=True)[:10]:
            lines.append(f"— {project}: {count}")

    if yougile_block:
        lines.append("\n" + yougile_block)

    if not active and not yougile_block:
        lines.append("\nАктивных задач нет. Можно ставить задачи обычным текстом: «Настя, по объекту ... нужно, чтобы ... сделал ... к пятнице»")
    elif not active:
        lines.append("\nВо внутреннем задачнике Настеньки активных задач нет; выше показаны данные из YouGile.")
    else:
        lines.append("\nБлижайшее действие: запросить статусы по блоку «нет свежего статуса» и отдельно разобрать высокий риск.")

    return "\n".join(lines[:limit + 20])


def build_team_context(update: Update, current_text: str, limit_events: int = 8, limit_tasks: int = 8) -> str:
    """Компактный контекст для LLM в групповых чатах и личных запросах.

    Он включает не чужие ответы дословно, а рабочую картину: активные задачи,
    недавние события и договоренности. Это помогает помнить командный контекст,
    но не заставляет модель копировать предыдущий ответ другому человеку.
    """
    chat = update.effective_chat
    chat_id = chat.id if chat and is_group_chat(update) else None
    project_name = _extract_project_name(current_text)
    project_id = None
    if project_name:
        project = get_project_by_alias_or_name(project_name)
        if project:
            project_id = project["id"]

    tasks = get_operational_tasks(project_id=project_id, chat_id=chat_id, status="active", limit=limit_tasks)
    events = get_recent_team_events(chat_id=chat_id, project_id=project_id, limit=limit_events * 2)
    # В общий контекст не кладем прошлые ответы бота дословно, чтобы не провоцировать дублирование
    # ответа одному сотруднику при разговоре с другим. Оставляем только пользовательские события,
    # задачи, статусы и договоренности.
    events = [e for e in events if e.get("event_type") != "assistant_answer"][:limit_events]

    lines: List[str] = []
    if tasks:
        lines.append("Активные структурированные задачи, известные боту:")
        for task in tasks:
            lines.append("- " + _task_line(task))
    if events:
        lines.append("Недавние рабочие события/договорённости, записанные ботом:")
        for event in events:
            author = event.get("author_name") or event.get("username") or "участник"
            when = _format_dt(event.get("created_at"), "")
            lines.append(f"- {when} · {author}: {event.get('content')}")
    if not lines:
        return ""
    return "\n".join(lines)


async def project_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Формат: /project alias Название объекта\n"
            "Пример: /project спартака ЖК Спартака"
        )
        return
    alias = _clean_alias(context.args[0])
    name = " ".join(context.args[1:]).strip() or context.args[0]
    chat_alias = _extract_chat_alias(" ".join(context.args[1:]))
    project_id = create_project(alias=alias, name=name, description=None, default_chat_alias=chat_alias, created_by=update.effective_user.id if update.effective_user else None)
    await update.effective_message.reply_text(f"✅ Проект/объект сохранён: #{project_id} {name} (alias: {alias})")


async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    projects = get_projects(limit=50)
    if not projects:
        await update.effective_message.reply_text("Проекты/объекты пока не заведены. Создайте: /project спартака ЖК Спартака")
        return
    lines = ["🏗 Проекты/объекты:"]
    for p in projects:
        suffix = f" · чат: {p['default_chat_alias']}" if p.get("default_chat_alias") else ""
        lines.append(f"#{p['id']} {p['name']} (alias: {p['alias']}){suffix}")
    await update.effective_message.reply_text("\n".join(lines))


async def op_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args or []).strip()
    if not text:
        await update.effective_message.reply_text(
            "Формат: /op_task По объекту спартака нужно, чтобы Дима подготовил РПЗ к пн. Уточняй каждый день"
        )
        return
    await create_operational_task_from_text(update, context, text, explicit=True)


async def tasks_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args or []).strip()
    await update.effective_message.reply_text(build_tasks_report(text))


async def task_detail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Формат: /op_task_info ID")
        return
    try:
        task_id = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.effective_message.reply_text("ID задачи должен быть числом.")
        return
    task = get_operational_task(task_id)
    if not task:
        await update.effective_message.reply_text(f"Задача #{task_id} не найдена.")
        return
    updates = get_task_updates(task_id, limit=8)
    lines = [
        f"📌 Задача #{task['id']}: {task['title']}",
        f"Проект: {task.get('project_name') or '—'}",
        f"Ответственный: {task.get('assigned_to_name') or task.get('assigned_to_username') or '—'}",
        f"Статус: {task.get('status')} · прогресс {task.get('progress', 0)}% · риск {_effective_risk(task)}",
        f"Срок: {task.get('deadline_text') or _format_dt(task.get('due_date'))}",
        f"Описание: {task.get('description') or '—'}",
    ]
    if updates:
        lines.append("\nИстория статусов:")
        for upd in updates:
            when = _format_dt(upd.get("created_at"), "")
            author = upd.get("author_name") or upd.get("username") or "участник"
            progress = f" · {upd['progress']}%" if upd.get("progress") is not None else ""
            lines.append(f"— {when} · {author}{progress}: {upd.get('update_text')}")
    await update.effective_message.reply_text("\n".join(lines))


async def task_update_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args or []) < 2:
        await update.effective_message.reply_text("Формат: /op_update ID 70% комментарий")
        return
    try:
        task_id = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.effective_message.reply_text("ID задачи должен быть числом.")
        return
    update_text = " ".join(context.args[1:]).strip()
    await save_task_status_update(update, task_id, update_text)


async def subtask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args or []) < 2:
        await update.effective_message.reply_text("Формат: /subtask ID текст подзадачи")
        return
    try:
        parent_id = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.effective_message.reply_text("ID родительской задачи должен быть числом.")
        return
    task = get_operational_task(parent_id)
    if not task:
        await update.effective_message.reply_text(f"Родительская задача #{parent_id} не найдена.")
        return
    title = " ".join(context.args[1:]).strip()
    sub_id = create_operational_task(
        project_id=task.get("project_id"),
        parent_task_id=parent_id,
        title=title,
        description=None,
        assigned_to_name=task.get("assigned_to_name"),
        assigned_to_username=task.get("assigned_to_username"),
        assigned_to_user_id=task.get("assigned_to_user_id"),
        assigned_by=update.effective_user.id if update.effective_user else None,
        chat_alias=task.get("chat_alias"),
        chat_id=task.get("chat_id"),
        deadline_text=task.get("deadline_text"),
        due_date=None,
        status="active",
        progress=0,
        priority=task.get("priority") or "normal",
        risk_level="низкий",
        control_enabled=False,
        control_cadence_days=1,
        next_check_at=None,
    )
    await update.effective_message.reply_text(f"✅ Подзадача #{sub_id} добавлена к задаче #{parent_id}: {title}")


async def save_task_status_update(update: Update, task_id: int, update_text: str) -> None:
    task = get_operational_task(task_id)
    if not task:
        await update.effective_message.reply_text(f"Задача #{task_id} не найдена.")
        return
    progress = _extract_progress(update_text)
    status = _status_from_text(update_text, progress)
    risk = "низкий"
    if any(w in update_text.lower() for w in ("проблем", "не успев", "блок", "жду", "задерж")):
        risk = "средний"
    if any(w in update_text.lower() for w in ("не успею", "срыв", "не смогу", "критич")):
        risk = "высокий"
    risk = _risk_from_text(update_text)
    update_operational_task_status(task_id, progress=progress, status=status, risk_level=risk, last_status_text=update_text)
    create_task_update(
        task_id=task_id,
        user_id=update.effective_user.id if update.effective_user else None,
        username=update.effective_user.username if update.effective_user else None,
        author_name=_human_user(update),
        update_text=update_text,
        progress=progress,
        status=status,
        risk_level=risk,
    )
    if status == "done":
        mark_operational_task_done(task_id)
    await update.effective_message.reply_text(f"✅ Статус задачи #{task_id} записан: {update_text}")


async def create_operational_task_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, explicit: bool = False) -> bool:
    user = update.effective_user
    if not _is_admin_user(user.id if user else None) and not is_group_chat(update):
        await update.effective_message.reply_text("Создавать операционные задачи могут только администраторы из ADMIN_IDS.")
        return True

    target_name, target_username, target_user_id = _extract_target(text)
    project_name = _extract_project_name(text)
    chat_alias = _extract_chat_alias(text)
    project = _find_or_create_project(project_name, created_by=user.id if user else None, chat_alias=chat_alias)
    chat_id, chat_alias_final, chat_title = _choose_chat_for_task(text, project, update)
    deadline_text = _extract_deadline_text(text)
    title = _extract_task_title(text, target_name)
    lowered_text = (text or "").lower()
    control_enabled = bool(target_name) and not any(word in lowered_text for word in NO_CONTROL_WORDS)
    cadence_days = 1
    if "раз в неделю" in text.lower() or "еженед" in text.lower():
        cadence_days = 7
    next_check_at = _now() + timedelta(days=cadence_days) if control_enabled else None

    if not target_name:
        # Не отдаем это DeepSeek, если явно просили создать задачу: лучше спросить понятный формат.
        await update.effective_message.reply_text(
            "Я вижу постановку задачи, но не понял ответственного. Надёжный формат:\n"
            "По объекту Спартака нужно, чтобы Дима подготовил РПЗ к пн. Уточняй каждый день."
        )
        return True

    task_id = create_operational_task(
        project_id=project.get("id") if project else None,
        parent_task_id=None,
        title=title,
        description=text,
        assigned_to_name=target_name,
        assigned_to_username=target_username,
        assigned_to_user_id=target_user_id,
        assigned_by=user.id if user else None,
        chat_alias=chat_alias_final or chat_alias,
        chat_id=chat_id,
        deadline_text=deadline_text,
        due_date=None,
        status="active",
        progress=0,
        priority="normal",
        risk_level="низкий",
        control_enabled=control_enabled,
        control_cadence_days=cadence_days,
        next_check_at=next_check_at,
    )

    # Если это РПЗ — автоматически создаем типовые подзадачи. Это можно потом отключить/поменять.
    created_subtasks = []
    if "рпз" in text.lower() or "расчетно-поясн" in text.lower() or "расчётно-поясн" in text.lower():
        for item in DEFAULT_RPZ_SUBTASKS:
            sub_id = create_operational_task(
                project_id=project.get("id") if project else None,
                parent_task_id=task_id,
                title=item,
                description=None,
                assigned_to_name=target_name,
                assigned_to_username=target_username,
                assigned_to_user_id=target_user_id,
                assigned_by=user.id if user else None,
                chat_alias=chat_alias_final or chat_alias,
                chat_id=chat_id,
                deadline_text=deadline_text,
                due_date=None,
                status="active",
                progress=0,
                priority="normal",
                risk_level="низкий",
                control_enabled=False,
                control_cadence_days=1,
                next_check_at=None,
            )
            created_subtasks.append(sub_id)

    save_team_memory_event(
        chat_id=chat_id or (update.effective_chat.id if update.effective_chat else None),
        chat_title=chat_title or (update.effective_chat.title if update.effective_chat else None),
        user_id=user.id if user else None,
        username=user.username if user else None,
        author_name=_human_user(update),
        event_type="task_created",
        content=f"Создана задача #{task_id}: {title}. Ответственный: {target_name}. Срок: {deadline_text or 'не задан'}. Контроль: {'да' if control_enabled else 'нет'}.",
        task_id=task_id,
        project_id=project.get("id") if project else None,
    )

    # Пишем сотруднику в рабочий чат, если чат найден.
    sent_note = ""
    if chat_id:
        mention = f"@{target_username}" if target_username else target_name
        lines = [f"{mention}, поставлена задача #{task_id}: {title}"]
        if project:
            lines.append(f"Объект: {project.get('name')}")
        if deadline_text:
            lines.append(f"Срок: {deadline_text}")
        if control_enabled:
            lines.append(f"Я буду уточнять статус каждые {cadence_days} дн.")
        if created_subtasks:
            lines.append(f"Подзадач создано: {len(created_subtasks)}")
        try:
            sent = await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
            if control_enabled:
                update_operational_task_control_ping(task_id, next_check_at=next_check_at, last_message_id=sent.message_id, last_check_at=_now())
            sent_note = f"\nСообщение отправлено в чат {chat_alias_final or chat_title or chat_id}."
        except Exception as exc:
            logger.exception("Не удалось отправить задачу в чат %s: %s", chat_id, exc)
            sent_note = f"\n⚠️ Задачу сохранила, но не смогла отправить в чат: {exc}"
    else:
        sent_note = "\n⚠️ Задачу сохранила, но рабочий чат не найден. Привяжите чат через /bind_chat alias."

    await update.effective_message.reply_text(
        f"✅ Создала операционную задачу #{task_id}: {title}\n"
        f"Ответственный: {target_name}\n"
        f"Объект: {project.get('name') if project else 'не указан'}\n"
        f"Срок: {deadline_text or 'не задан'}\n"
        f"Контроль: {'да' if control_enabled else 'нет'}\n"
        f"Подзадачи: {len(created_subtasks)}{sent_note}"
    )
    return True


async def maybe_handle_operational_request(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()

    # Ответ сотрудника на контрольное сообщение бота.
    if is_group_chat(update) and await maybe_handle_operational_reply(update, context, text):
        return True

    # Создание задачи естественным языком.
    if looks_like_task_creation(text):
        return await create_operational_task_from_text(update, context, text)

    if looks_like_task_status_update(text):
        task_id = _extract_task_id(text)
        if task_id:
            await save_task_status_update(update, task_id, text)
            return True

    # Статус/сводки.
    if looks_like_status_request(text):
        if _contains_any(text, MANAGER_SUMMARY_WORDS):
            await update.effective_message.reply_text(build_manager_summary())
        else:
            await update.effective_message.reply_text(build_tasks_report(text))
        return True

    # Авто-разбивка задачи на подзадачи.
    if looks_like_subtask_request(text):
        m = re.search(r"#(\d+)", text)
        if not m:
            await update.effective_message.reply_text("Укажите номер задачи для разбивки, например: разбей задачу #12 на подзадачи")
            return True
        task_id = int(m.group(1))
        task = get_operational_task(task_id)
        if not task:
            await update.effective_message.reply_text(f"Задача #{task_id} не найдена.")
            return True
        created = []
        template = DEFAULT_RPZ_SUBTASKS if "рпз" in (task.get("title") or "").lower() + lowered else [
            "Уточнить исходные данные",
            "Подготовить рабочий материал",
            "Согласовать промежуточный результат",
            "Передать на проверку",
            "Внести правки и закрыть задачу",
        ]
        for item in template:
            created.append(create_operational_task(
                project_id=task.get("project_id"), parent_task_id=task_id, title=item, description=None,
                assigned_to_name=task.get("assigned_to_name"), assigned_to_username=task.get("assigned_to_username"),
                assigned_to_user_id=task.get("assigned_to_user_id"), assigned_by=update.effective_user.id if update.effective_user else None,
                chat_alias=task.get("chat_alias"), chat_id=task.get("chat_id"), deadline_text=task.get("deadline_text"),
                due_date=None, status="active", progress=0, priority="normal", risk_level="низкий",
                control_enabled=False, control_cadence_days=1, next_check_at=None,
            ))
        await update.effective_message.reply_text(f"✅ Разбила задачу #{task_id} на {len(created)} подзадач.")
        return True

    return False


async def maybe_handle_operational_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Если сотрудник ответил на контрольное сообщение бота — записываем статус."""
    msg = update.effective_message
    if not msg or not msg.reply_to_message or not update.effective_chat:
        return False
    task = get_operational_task_by_last_message(update.effective_chat.id, msg.reply_to_message.message_id)
    if not task:
        return False
    progress = _extract_progress(text)
    status = _status_from_text(text, progress)
    if any(word in text.lower() for word in DONE_WORDS):
        progress = 100
        status = "done"
    risk = "низкий"
    if any(w in text.lower() for w in ("не успева", "проблем", "блок", "жду", "задерж")):
        risk = "средний"
    if any(w in text.lower() for w in ("срыв", "не смогу", "критич")):
        risk = "высокий"
    risk = _risk_from_text(text)
    update_operational_task_status(task["id"], progress=progress, status=status, risk_level=risk, last_status_text=text)
    create_task_update(
        task_id=task["id"],
        user_id=update.effective_user.id if update.effective_user else None,
        username=update.effective_user.username if update.effective_user else None,
        author_name=_human_user(update),
        update_text=text,
        progress=progress,
        status=status,
        risk_level=risk,
    )
    if status == "done":
        mark_operational_task_done(task["id"])
        await msg.reply_text(f"✅ Зафиксировала завершение задачи #{task['id']}.")
    else:
        next_at = _now() + timedelta(days=int(task.get("control_cadence_days") or 1))
        update_operational_task_control_ping(task["id"], next_check_at=next_at, last_check_at=_now())
        await msg.reply_text(f"✅ Статус задачи #{task['id']} записан. Следующий контроль: {_format_dt(next_at)}")
    return True


async def remember_addressed_group_message(update: Update, text: str, event_type: str = "addressed_message") -> None:
    if not is_group_chat(update) or not text:
        return
    chat = update.effective_chat
    user = update.effective_user
    project_name = _extract_project_name(text)
    project = get_project_by_alias_or_name(project_name) if project_name else None
    save_team_memory_event(
        chat_id=chat.id,
        chat_title=chat.title,
        user_id=user.id if user else None,
        username=user.username if user else None,
        author_name=_human_user(update),
        event_type=event_type,
        content=text[:2000],
        task_id=None,
        project_id=project.get("id") if project else None,
    )


async def remember_assistant_answer(update: Update, answer: str) -> None:
    if not is_group_chat(update) or not answer:
        return
    chat = update.effective_chat
    save_team_memory_event(
        chat_id=chat.id,
        chat_title=chat.title,
        user_id=None,
        username="bot",
        author_name="ИИ Настенька",
        event_type="assistant_answer",
        content=answer[:2000],
        task_id=None,
        project_id=None,
    )


async def schedule_operational_task_checker(app) -> None:
    pass


async def send_daily_manager_summary(context) -> None:
    summary = build_manager_summary()
    from internet_search import split_telegram_text
    bot = context.bot
    for admin_id in ADMIN_IDS:
        try:
            for part in split_telegram_text(summary):
                await bot.send_message(chat_id=admin_id, text=part)
        except Exception as exc:
            logger.exception("Failed to send daily manager summary to %s: %s", admin_id, exc)


def schedule_operational_checker(app) -> None:
    job_queue = app.job_queue
    if not job_queue:
        logger.warning("JobQueue недоступен: проверьте python-telegram-bot[job-queue]")
        return

    async def callback(_):
        await check_due_operational_tasks(app)

    job_queue.run_repeating(callback, interval=300, first=20)
    summary_time = os.getenv("DAILY_SUMMARY_TIME", "").strip()
    if summary_time and ADMIN_IDS:
        try:
            hour, minute = [int(part) for part in summary_time.split(":", 1)]
            run_at = TIMEZONE.localize(datetime(2000, 1, 1, hour, minute)).timetz()
            job_queue.run_daily(send_daily_manager_summary, time=run_at)
            logger.info("Daily manager summary scheduled at %s", summary_time)
        except Exception as exc:
            logger.warning("Invalid DAILY_SUMMARY_TIME=%s: %s", summary_time, exc)
    logger.info("Планировщик операционных задач запущен")


async def check_due_operational_tasks(app) -> None:
    tasks = get_due_operational_tasks(_now())
    for task in tasks:
        if not task.get("chat_id"):
            continue
        responsible = task.get("assigned_to_name") or "коллеги"
        if task.get("assigned_to_username"):
            responsible = f"@{task['assigned_to_username']}"
        lines = [f"{responsible}, уточните, пожалуйста, статус по задаче #{task['id']}:"]
        if task.get("project_name"):
            lines.append(f"Объект: {task['project_name']}")
        lines.append(task.get("title") or "Задача без названия")
        if task.get("deadline_text"):
            lines.append(f"Срок: {task['deadline_text']}")
        lines.append("Ответьте на это сообщение коротко: что сделано, примерный %, есть ли блокеры.")
        try:
            sent = await app.bot.send_message(chat_id=task["chat_id"], text="\n".join(lines))
            next_at = _now() + timedelta(days=int(task.get("control_cadence_days") or 1))
            update_operational_task_control_ping(task["id"], next_check_at=next_at, last_message_id=sent.message_id, last_check_at=_now())
        except Exception as exc:
            logger.exception("Не удалось отправить контроль по задаче %s: %s", task.get("id"), exc)


async def daily_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from internet_search import split_telegram_text
    for part in split_telegram_text(build_manager_summary()):
        await update.effective_message.reply_text(part)


def operational_bot_commands() -> List[BotCommand]:
    return [
        BotCommand("project", "Добавить объект/проект"),
        BotCommand("projects", "Список объектов/проектов"),
        BotCommand("op_task", "Создать операционную задачу"),
        BotCommand("tasks", "Сводка операционных задач"),
        BotCommand("op_task_info", "Детали операционной задачи"),
        BotCommand("op_update", "Обновить статус операционной задачи"),
        BotCommand("subtask", "Добавить подзадачу"),
        BotCommand("daily_summary", "Сводка задач и рисков"),
    ]
