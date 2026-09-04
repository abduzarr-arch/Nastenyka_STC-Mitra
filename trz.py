"""Structured collection and reporting of employee time entries (ТРЗ)."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from telegram import BotCommand, Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS, TIMEZONE
from database import (
    create_time_entry,
    delete_time_entry,
    get_time_entries,
    get_time_entry,
)


def _is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)


def _today() -> date:
    return datetime.now(TIMEZONE).date()


def _format_hours(value: float) -> str:
    rounded = round(float(value), 2)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _parse_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _clean_field(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip(" \t\r\n.;,-"))


def _clean_template_value(value: str) -> str:
    value = (value or "").strip()
    value = value.strip('"\'«»“”` ')
    value = value.strip("* ")
    return _clean_field(value)


def _extract_hours(value: str) -> Optional[float]:
    match = re.search(r"(?<!\d)(\d{1,4}(?:[.,]\d{1,2})?)(?!\d)", value or "")
    if not match:
        return None
    hours = float(match.group(1).replace(",", "."))
    if hours <= 0 or hours > 1000:
        return None
    return hours


def _looks_like_labeled_time_entry(text: str) -> bool:
    """Recognize the employee-facing multiline TRZ form, even without /trz."""
    lowered = (text or "").lower().replace("ё", "е")
    return (
        "дата работ" in lowered
        and ("объект" in lowered or "проект" in lowered)
        and ("трз" in lowered or "трудозатрат" in lowered)
        and ("список работ" in lowered or "работ выполн" in lowered)
    )


def _extract_labeled_field(text: str, label_pattern: str) -> Optional[str]:
    """Extract a value written before ``- label`` in a form line.

    Quoted values may span several lines, which is useful for the work list.
    """
    match = re.search(
        rf"(?:^|\r?\n)\s*(?P<value>\"[^\"]*\"|«[^»]*»|[^\r\n]+?)"
        rf"\s*[\-–—]\s*(?:{label_pattern})",
        text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _clean_template_value(match.group("value")) if match else None


def _parse_labeled_time_entry(text: str) -> Tuple[Optional[Dict], Optional[str]]:
    date_text = _extract_labeled_field(text, r"дата\s+работ\b")
    project_name = _extract_labeled_field(text, r"(?:объект|проект)\b")
    hours_text = _extract_labeled_field(text, r"(?:трз\b|трудозатрат\w*)")
    task_name = _extract_labeled_field(
        text,
        r"(?:список\s+работ\b|работ\w*\s+выполн\w*)",
    )
    if not all((date_text, project_name, hours_text, task_name)):
        return None, (
            "Не смогла разобрать заполненный шаблон ТРЗ. Проверьте четыре поля: "
            "дата работ, объект, ТРЗ в часах и список выполненных работ."
        )

    work_date = _parse_date(date_text)
    hours = _extract_hours(hours_text)
    return {
        "project_name": project_name,
        "task_name": task_name,
        "hours": hours,
        "work_date": work_date,
    }, None


def parse_time_entry(text: str) -> Tuple[Optional[Dict], Optional[str]]:
    raw = (text or "").strip()
    if _looks_like_labeled_time_entry(raw):
        parsed, error = _parse_labeled_time_entry(raw)
        if error:
            return None, error
        project_name = parsed["project_name"]
        task_name = parsed["task_name"]
        hours = parsed["hours"]
        work_date = parsed["work_date"]
    else:
        raw = re.sub(r"^/trz(?:@\w+)?\s*", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"^(?:трз|трудозатраты)\s*[:\-]?\s*", "", raw, flags=re.IGNORECASE).strip()
        if not raw:
            return None, "Укажите объект, задачу и часы."

        parts = [_clean_field(part) for part in raw.split("|")]
        if len(parts) >= 3:
            project_name, task_name = parts[0], parts[1]
            hours = _extract_hours(parts[2])
            work_date = _parse_date(parts[3]) if len(parts) >= 4 and parts[3] else _today()
        else:
            natural = re.match(
                r"^(?:по\s+)?(?:объект(?:у|а)?|проект(?:у|а)?)?\s*[:\-]?\s*"
                r"(?P<project>.+?)[,;]\s*"
                r"(?:задач(?:а|е|у)?\s*[:\-]?\s*)?"
                r"(?P<task>.+?)[,;]\s*"
                r"(?P<hours>\d{1,4}(?:[.,]\d{1,2})?)\s*"
                r"(?:ч(?:\.|ас(?:а|ов)?)?)"
                r"(?:\s*(?:за|от)\s*(?P<date>\d{1,2}\.\d{1,2}\.\d{2,4}))?\s*$",
                raw,
                flags=re.IGNORECASE,
            )
            if not natural:
                return None, (
                    "Не смогла разобрать запись. Используйте формат:\n"
                    "ТРЗ: Объект | Задача | Часы\n"
                    "Например: ТРЗ: Лиговский | расчёт плиты | 6"
                )
            project_name = _clean_field(natural.group("project"))
            task_name = _clean_field(natural.group("task"))
            hours = _extract_hours(natural.group("hours"))
            date_text = natural.group("date")
            work_date = _parse_date(date_text) if date_text else _today()

    if not project_name or not task_name:
        return None, "Название объекта и задачи не должны быть пустыми."
    if hours is None:
        return None, "Укажите трудозатраты числом от 0,1 до 1000 часов."
    if work_date is None:
        return None, "Не смогла разобрать дату. Используйте формат ДД.ММ.ГГГГ."
    if work_date > _today():
        return None, "Нельзя записать фактические трудозатраты будущей датой."

    return {
        "project_name": project_name[:200],
        "task_name": task_name[:500],
        "hours": hours,
        "work_date": work_date,
    }, None


def looks_like_time_entry(text: str) -> bool:
    value = (text or "").strip().lower()
    if _looks_like_labeled_time_entry(value):
        return True
    if not re.match(r"^(?:трз|трудозатраты)\b", value):
        return False
    return "|" in value or bool(
        re.search(r"\d{1,4}(?:[.,]\d{1,2})?\s*(?:ч(?:\.|ас(?:а|ов)?)?)\b", value)
    )


def _period_from_text(text: str, default: str = "month") -> Tuple[date, date, str]:
    lowered = (text or "").lower()
    today = _today()

    dates = re.findall(r"\b(\d{1,2}\.\d{1,2}\.\d{2,4})\b", lowered)
    if len(dates) >= 2:
        start, end = _parse_date(dates[0]), _parse_date(dates[1])
        if start and end:
            if start > end:
                start, end = end, start
            return start, end, f"{start:%d.%m.%Y} - {end:%d.%m.%Y}"
    if len(dates) == 1:
        selected = _parse_date(dates[0])
        if selected:
            return selected, selected, f"{selected:%d.%m.%Y}"

    if "вчера" in lowered:
        selected = today - timedelta(days=1)
        return selected, selected, "за вчера"
    if "сегодня" in lowered or "день" in lowered:
        return today, today, "за сегодня"
    if "7 д" in lowered or "последн" in lowered and "недел" in lowered:
        start = today - timedelta(days=6)
        return start, today, "за последние 7 дней"
    if "недел" in lowered:
        start = today - timedelta(days=today.weekday())
        return start, today, "за текущую неделю"
    if "год" in lowered:
        start = today.replace(month=1, day=1)
        return start, today, "за текущий год"
    if "месяц" in lowered:
        start = today.replace(day=1)
        return start, today, "за текущий месяц"

    start = today.replace(day=1)
    if default == "week":
        start = today - timedelta(days=today.weekday())
        return start, today, "за текущую неделю"
    return start, today, "за текущий месяц"


def _entry_display_name(entry: Dict) -> str:
    return entry.get("display_name") or (
        f"@{entry['username']}" if entry.get("username") else f"ID {entry.get('user_id')}"
    )


def _project_key(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower().replace("ё", "е"))


def build_time_report(
    start: date,
    end: date,
    label: str,
    user_id: Optional[int] = None,
    project_query: Optional[str] = None,
    include_entries: bool = True,
) -> str:
    entries = get_time_entries(
        start.isoformat(),
        end.isoformat(),
        user_id=user_id,
    )
    if project_query:
        project_key_query = _project_key(project_query)
        entries = [
            entry
            for entry in entries
            if project_key_query in _project_key(entry.get("project_name"))
        ]
    scope = f"ТРЗ {label}"
    if project_query:
        scope += f", объект «{project_query}»"
    if not entries:
        return scope + "\nЗаписей нет."

    by_project: Dict[str, float] = defaultdict(float)
    project_labels: Dict[str, str] = {}
    by_person: Dict[str, float] = defaultdict(float)
    project_people: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    person_labels: Dict[str, str] = {}
    total = 0.0
    for entry in entries:
        project = entry["project_name"]
        project_key = _project_key(project)
        project_labels.setdefault(project_key, project)
        person_key = str(entry.get("user_id"))
        person_labels.setdefault(person_key, _entry_display_name(entry))
        hours = float(entry["hours"])
        total += hours
        by_project[project_key] += hours
        by_person[person_key] += hours
        project_people[project_key][person_key] += hours

    lines = [scope, f"Всего: {_format_hours(total)} ч · записей: {len(entries)}", "", "По объектам:"]
    for project_key, hours in sorted(by_project.items(), key=lambda item: (-item[1], item[0])):
        project = project_labels[project_key]
        people = "; ".join(
            f"{person_labels[person_key]}: {_format_hours(person_hours)} ч"
            for person_key, person_hours in sorted(
                project_people[project_key].items(),
                key=lambda item: (-item[1], person_labels[item[0]].lower()),
            )
        )
        lines.append(f"• {project}: {_format_hours(hours)} ч")
        lines.append(f"  {people}")

    lines.append("\nПо специалистам:")
    for person_key, hours in sorted(
        by_person.items(),
        key=lambda item: (-item[1], person_labels[item[0]].lower()),
    ):
        lines.append(f"• {person_labels[person_key]}: {_format_hours(hours)} ч")

    if include_entries:
        lines.append("\nПоследние записи:")
        for entry in entries[:12]:
            lines.append(
                f"#{entry['id']} · {entry['work_date']} · {_entry_display_name(entry)} · "
                f"{entry['project_name']} · {entry['task_name']} · {_format_hours(entry['hours'])} ч"
            )
    return "\n".join(lines)


def build_compact_time_summary(days: int = 7) -> str:
    today = _today()
    start = today - timedelta(days=max(1, int(days)) - 1)
    entries = get_time_entries(start.isoformat(), today.isoformat())
    if not entries:
        return "ТРЗ за последние 7 дней: записей нет."
    by_project: Dict[str, float] = defaultdict(float)
    project_labels: Dict[str, str] = {}
    total = 0.0
    for entry in entries:
        hours = float(entry["hours"])
        total += hours
        project_key = _project_key(entry["project_name"])
        project_labels.setdefault(project_key, entry["project_name"])
        by_project[project_key] += hours
    lines = [f"ТРЗ за последние 7 дней: {_format_hours(total)} ч"]
    for project_key, hours in sorted(by_project.items(), key=lambda item: -item[1])[:10]:
        lines.append(f"• {project_labels[project_key]}: {_format_hours(hours)} ч")
    return "\n".join(lines)


async def _save_entry(update: Update, text: str) -> bool:
    parsed, error = parse_time_entry(text)
    message = update.effective_message
    if error:
        await message.reply_text(error)
        return True

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        await message.reply_text("Не удалось определить сотрудника или чат.")
        return True

    result = create_time_entry(
        user_id=user.id,
        username=user.username,
        display_name=user.full_name,
        chat_id=chat.id,
        chat_title=chat.title,
        project_name=parsed["project_name"],
        task_name=parsed["task_name"],
        hours=parsed["hours"],
        work_date=parsed["work_date"].isoformat(),
        source_message_id=message.message_id,
    )
    if not result["created"]:
        await message.reply_text(f"Эта запись уже сохранена как ТРЗ #{result['id']}.")
        return True

    await message.reply_text(
        f"Записала ТРЗ #{result['id']}:\n"
        f"Объект: {parsed['project_name']}\n"
        f"Задача: {parsed['task_name']}\n"
        f"Трудозатраты: {_format_hours(parsed['hours'])} ч\n"
        f"Дата: {parsed['work_date']:%d.%m.%Y}"
    )
    return True


async def trz_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args or []).strip()
    if not text:
        await update.effective_message.reply_text(
            "Запись трудозатрат:\n"
            "/trz Объект | Задача | Часы\n\n"
            "Пример:\n"
            "/trz Лиговский | расчёт плиты перекрытия | 6\n\n"
            "Дата необязательна. Для другой даты добавьте четвёртое поле:\n"
            "/trz Лиговский | расчёт плиты | 6 | 25.07.2026"
        )
        return
    await _save_entry(update, text)


async def maybe_handle_time_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not looks_like_time_entry(text):
        return False
    return await _save_entry(update, text)


def _looks_like_report_request(text: str) -> bool:
    if _looks_like_labeled_time_entry(text):
        return False
    lowered = (text or "").lower()
    if "трз" not in lowered and "трудозатрат" not in lowered:
        return False
    return any(
        marker in lowered
        for marker in (
            "сколько",
            "сводк",
            "отчет",
            "отчёт",
            "покажи",
            "какие",
            "дай",
            "выведи",
            "итог",
            "по объект",
            "по специалист",
            "за неделю",
            "за месяц",
            "за сегодня",
        )
    )


def _extract_project_query(text: str) -> Optional[str]:
    match = re.search(
        r"(?:по|для)\s+(?:объекту|объекта|проекту|проекта)\s+[«\"]?"
        r"(?P<project>.+?)[»\"]?"
        r"(?=\s+(?:за|с)\s+(?:текущ|последн|сегодня|вчера|недел|месяц|год|\d)|$)",
        text or "",
        flags=re.IGNORECASE,
    )
    return _clean_field(match.group("project")) if match else None


async def _reply_report(message, report: str) -> None:
    from internet_search import split_telegram_text
    for part in split_telegram_text(report):
        await message.reply_text(part)


async def trz_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_admin(user.id if user else None):
        await update.effective_message.reply_text("Общая сводка ТРЗ доступна только руководителю из ADMIN_IDS.")
        return
    query = " ".join(context.args or [])
    start, end, label = _period_from_text(query, default="month")
    project_query = _extract_project_query(query)
    await _reply_report(
        update.effective_message,
        build_time_report(start, end, label, project_query=project_query),
    )


async def my_trz_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    query = " ".join(context.args or [])
    start, end, label = _period_from_text(query, default="week")
    await _reply_report(
        update.effective_message,
        build_time_report(start, end, label, user_id=user.id, include_entries=True),
    )


async def trz_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    match = re.search(r"\d+", " ".join(context.args or []))
    if not match:
        await message.reply_text("Укажите номер записи, например: /trz_delete 15")
        return
    entry = get_time_entry(int(match.group()))
    if not entry:
        await message.reply_text("Запись ТРЗ не найдена.")
        return
    user = update.effective_user
    if not user or (entry["user_id"] != user.id and not _is_admin(user.id)):
        await message.reply_text("Удалить запись может её автор или руководитель.")
        return
    delete_time_entry(entry["id"])
    await message.reply_text(f"Запись ТРЗ #{entry['id']} удалена.")


async def maybe_handle_trz_request(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if looks_like_time_entry(text):
        return await _save_entry(update, text)
    if not _looks_like_report_request(text):
        return False

    user = update.effective_user
    start, end, label = _period_from_text(text, default="month")
    project_query = _extract_project_query(text)
    if _is_admin(user.id if user else None):
        report = build_time_report(start, end, label, project_query=project_query)
    else:
        report = build_time_report(
            start,
            end,
            label,
            user_id=user.id if user else None,
            project_query=project_query,
        )
    await _reply_report(update.effective_message, report)
    return True


def trz_bot_commands() -> List[BotCommand]:
    return [
        BotCommand("trz", "Записать трудозатраты"),
        BotCommand("my_trz", "Мои трудозатраты"),
        BotCommand("trz_report", "Сводка ТРЗ для руководителя"),
        BotCommand("trz_delete", "Удалить ошибочную запись ТРЗ"),
    ]
