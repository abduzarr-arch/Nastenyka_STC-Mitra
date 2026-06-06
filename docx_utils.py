import asyncio
import json
import os
import re
import shutil
import tempfile
import time as time_module
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.text.paragraph import Paragraph
from openai import OpenAI

from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL, OPENAI_API_KEY, WORD_AI_PROVIDER, WORD_OPENAI_MODEL, logger
from database import add_to_conversation
from group_utils import get_dialog_key

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
openai_word_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

SUPPORTED_WORD_EXTENSIONS = (".docx",)
UNSUPPORTED_WORD_EXTENSIONS = (".doc", ".rtf", ".odt")


class WordProcessingError(Exception):
    pass


def is_word_file(file_name: str) -> bool:
    return (file_name or "").lower().endswith(SUPPORTED_WORD_EXTENSIONS + UNSUPPORTED_WORD_EXTENSIONS)


def is_supported_word_file(file_name: str) -> bool:
    return (file_name or "").lower().endswith(SUPPORTED_WORD_EXTENSIONS)


def looks_like_word_edit_request(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    edit_words = (
        "измени", "исправь", "поправь", "отредактируй", "добавь", "удали", "замени",
        "вставь", "перепиши", "дополни", "сократи", "расширь", "сформулируй в договоре",
        "добавить", "изменить", "правки", "правку", "новую редакцию", "этапность оплат",
        "порядок оплаты", "порядок расчетов", "раздел", "пункт", "пункты", "приложение",
    )
    file_words = ("word", "docx", "ворд", "документ", "договор", "акт", "письмо", "коммерческое")
    if any(word in lowered for word in edit_words):
        return True
    return "сделай" in lowered and any(word in lowered for word in file_words)


def is_create_word_request(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    create_words = ("создай", "сделай", "сформируй", "подготовь", "собери")
    if not any(word in lowered for word in create_words):
        return False
    # Не перехватываем общие управленческие задачи вида «подготовь РПЗ».
    return any(word in lowered for word in ("word", "docx", "ворд", "договор", "акт", "письмо", "протокол"))


def _safe_filename(name: str, default: str = "document_result.docx") -> str:
    name = (name or default).strip().replace("\\", "_").replace("/", "_")
    name = re.sub(r"[^\w\-.а-яА-ЯёЁ ]+", "_", name, flags=re.UNICODE).strip(" ._")
    if not name:
        name = default
    if not name.lower().endswith(".docx"):
        name += ".docx"
    return name[:120]


def _word_cache_dir() -> str:
    """Папка для последнего Word-файла пользователя.

    На Railway можно задать WORD_STORAGE_DIR=/data/word_cache.
    Если не задано, но есть DB_FILE=/data/bot_data.db, файлы кладутся рядом с базой.
    """
    explicit = os.getenv("WORD_STORAGE_DIR")
    if explicit:
        base = explicit
    else:
        db_file = os.getenv("DB_FILE")
        if db_file:
            base = os.path.join(os.path.dirname(os.path.abspath(db_file)), "word_cache")
        elif os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
            base = os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH"), "word_cache")
        else:
            base = os.path.join(tempfile.gettempdir(), "mitra_word_cache")
    os.makedirs(base, exist_ok=True)
    return base


def remember_word_file(context, chat_id: int, source_path: str, file_name: str, dialog_key: Optional[str] = None) -> Dict[str, Any]:
    dialog_key = str(dialog_key or chat_id)
    word_files = context.user_data.setdefault("word_files", {}) if hasattr(context, "user_data") else {}
    old = word_files.get(dialog_key)
    if old and old.get("path") and os.path.exists(old["path"]):
        try:
            os.unlink(old["path"])
        except OSError:
            pass

    safe_name = _safe_filename(file_name or "document.docx")
    stored_name = f"chat_{chat_id}_{uuid.uuid4().hex[:8]}_{safe_name}"
    stored_path = os.path.join(_word_cache_dir(), stored_name)
    shutil.copy2(source_path, stored_path)

    info = {"path": stored_path, "file_name": safe_name, "saved_at": time_module.time()}
    word_files[dialog_key] = info
    context.user_data["awaiting_word_request_by_dialog"] = {
        **context.user_data.get("awaiting_word_request_by_dialog", {}),
        dialog_key: True,
    }
    return info


def _get_recent_word_context(context, dialog_key: str, max_age_seconds: int = 24 * 60 * 60) -> Optional[Dict[str, Any]]:
    dialog_key = str(dialog_key)
    word_files = context.user_data.get("word_files", {}) if hasattr(context, "user_data") else {}
    info = word_files.get(dialog_key)
    if not info:
        return None
    path = info.get("path")
    if not path or not os.path.exists(path):
        word_files.pop(dialog_key, None)
        context.user_data.get("awaiting_word_request_by_dialog", {}).pop(dialog_key, None)
        return None
    if time_module.time() - float(info.get("saved_at") or 0) > max_age_seconds:
        word_files.pop(dialog_key, None)
        context.user_data.get("awaiting_word_request_by_dialog", {}).pop(dialog_key, None)
        return None
    return info


def _get_reference_documents(context, dialog_key: str, max_docs: int = 5) -> List[Dict[str, Any]]:
    if not hasattr(context, "user_data"):
        return []
    refs_by_dialog = context.user_data.get("reference_documents_by_dialog", {})
    items = refs_by_dialog.get(str(dialog_key), [])
    return list(items[-max_docs:])


def _remember_reference_document(context, dialog_key: str, file_name: str, text: str, max_docs: int = 8) -> None:
    if not text or not hasattr(context, "user_data"):
        return
    refs_by_dialog = context.user_data.setdefault("reference_documents_by_dialog", {})
    items = refs_by_dialog.setdefault(str(dialog_key), [])
    items.append(
        {
            "file_name": file_name or "document.docx",
            "text": text[:12000],
            "saved_at": time_module.time(),
        }
    )
    refs_by_dialog[str(dialog_key)] = items[-max_docs:]


def _clip_text_at_boundary(text: str, limit: int, suffix: str = "\n... [текст обрезан]") -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind("\n"), cut.rfind(". "), cut.rfind("; "), cut.rfind(", "), cut.rfind(" "))
    if boundary > max(0, limit - 300):
        cut = cut[:boundary].rstrip()
    return cut.rstrip() + suffix


def _format_reference_documents(refs: List[Dict[str, Any]], limit: int = 22000) -> str:
    if not refs:
        return ""
    parts: List[str] = []
    used = 0
    for idx, ref in enumerate(refs, start=1):
        name = str(ref.get("file_name") or f"reference_{idx}")
        text = str(ref.get("text") or "").strip()
        if not text:
            continue
        header = f"\n=== Reference document {idx}: {name} ===\n"
        remaining = limit - used - len(header)
        if remaining <= 0:
            break
        chunk = _clip_text_at_boundary(text, remaining, "\n... [часть опорного документа скрыта из-за лимита контекста]")
        parts.append(header + chunk)
        used += len(header) + len(chunk)
        if used >= limit:
            break
    return "\n".join(parts).strip()


def _is_word_followup_request(context, text: str, dialog_key: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    awaiting = context.user_data.get("awaiting_word_request_by_dialog", {}) if hasattr(context, "user_data") else {}
    if awaiting.get(str(dialog_key)):
        return True
    if looks_like_word_edit_request(text):
        return True
    return any(word in lowered for word in ("word", "docx", "ворд", "договор", "пункт", "раздел", "оплат", "расчет"))


def _trim_text(text: str, limit: int = 55000) -> str:
    if len(text) <= limit:
        return text
    return _clip_text_at_boundary(text, limit, "\n... [текст документа обрезан из-за размера файла]")


def document_to_text(path: str, max_paragraphs: int = 260, max_tables: int = 15, max_table_rows: int = 40, max_cell_chars: int = 1200) -> str:
    """Текстовое представление DOCX: абзацы + таблицы."""
    doc = Document(path)
    parts: List[str] = []

    paragraph_count = 0
    for idx, paragraph in enumerate(doc.paragraphs, start=1):
        text = (paragraph.text or "").strip()
        if not text:
            continue
        paragraph_count += 1
        if paragraph_count > max_paragraphs:
            parts.append(f"... показаны первые {max_paragraphs} непустых абзацев")
            break
        style = paragraph.style.name if paragraph.style is not None else ""
        parts.append(f"[P{idx}; style={style}] {text}")

    for t_idx, table in enumerate(doc.tables[:max_tables], start=1):
        parts.append(f"\n=== Таблица {t_idx}: {len(table.rows)} строк x {len(table.columns)} столбцов ===")
        for r_idx, row in enumerate(table.rows[:max_table_rows], start=1):
            values = []
            for cell in row.cells:
                cell_text = " ".join((p.text or "").strip() for p in cell.paragraphs if (p.text or "").strip())
                values.append(_clip_text_at_boundary(cell_text, max_cell_chars, " ... [ячейка обрезана из-за лимита]"))
            parts.append(" | ".join(values))
        if len(table.rows) > max_table_rows:
            parts.append(f"... показаны первые {max_table_rows} строк таблицы")

    if not parts:
        parts.append("[Документ не содержит извлекаемого текста или состоит из изображений/сканов]")
    return _trim_text("\n".join(parts))


def _call_deepseek_for_word(system_prompt: str, user_prompt: str, max_tokens: int = 3000, temperature: float = 0.1) -> str:
    if not DEEPSEEK_API_KEY:
        raise WordProcessingError("Не настроен DEEPSEEK_API_KEY")

    try:
        word_token_limit = int(os.getenv("WORD_MAX_RESPONSE_TOKENS", "8000"))
    except ValueError:
        word_token_limit = 8000

    data = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max(512, min(word_token_limit, max_tokens)),
        "temperature": temperature,
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    try:
        response = requests.post(DEEPSEEK_API_URL, headers=headers, json=data, timeout=140)
        response.raise_for_status()
        payload = response.json()
        choice = payload["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        finish_reason = choice.get("finish_reason") or "unknown"
        if not content.strip():
            raise WordProcessingError(f"ИИ вернул пустой ответ (finish_reason={finish_reason})")
        if finish_reason == "length" and not content.strip().endswith("}"):
            raise WordProcessingError("ИИ не успел завершить JSON-план правок (finish_reason=length)")
        return content
    except requests.HTTPError as e:
        detail = (getattr(e.response, "text", "") or "")[:500]
        status = getattr(e.response, "status_code", "unknown")
        raise WordProcessingError(f"ИИ-сервис вернул ошибку HTTP {status}. {detail}") from e
    except requests.RequestException as e:
        raise WordProcessingError(f"ИИ-сервис временно недоступен или не ответил: {e}") from e
    except (KeyError, IndexError, ValueError) as e:
        raise WordProcessingError("ИИ-сервис вернул ответ в неожиданном формате") from e


def _call_openai_for_word(system_prompt: str, user_prompt: str, max_tokens: int = 3000, temperature: float = 0.1) -> str:
    if not openai_word_client:
        raise WordProcessingError("Не настроен OPENAI_API_KEY для обработки Word")

    try:
        word_token_limit = int(os.getenv("WORD_MAX_RESPONSE_TOKENS", "8000"))
    except ValueError:
        word_token_limit = 8000

    try:
        response = openai_word_client.chat.completions.create(
            model=WORD_OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max(512, min(word_token_limit, max_tokens)),
            temperature=temperature,
        )
        choice = response.choices[0]
        content = choice.message.content or ""
        finish_reason = choice.finish_reason or "unknown"
        if not content.strip():
            raise WordProcessingError(f"OpenAI вернул пустой ответ (finish_reason={finish_reason})")
        if finish_reason == "length" and not content.strip().endswith("}"):
            raise WordProcessingError("OpenAI не успел завершить JSON-план правок (finish_reason=length)")
        return content
    except WordProcessingError:
        raise
    except Exception as e:
        raise WordProcessingError(f"OpenAI не смог обработать Word-запрос: {e}") from e


def _call_ai_for_word(system_prompt: str, user_prompt: str, max_tokens: int = 3000, temperature: float = 0.1) -> str:
    if WORD_AI_PROVIDER == "openai":
        return _call_openai_for_word(system_prompt, user_prompt, max_tokens=max_tokens, temperature=temperature)
    if WORD_AI_PROVIDER == "auto" and openai_word_client:
        try:
            return _call_openai_for_word(system_prompt, user_prompt, max_tokens=max_tokens, temperature=temperature)
        except WordProcessingError as openai_error:
            logger.warning(f"OpenAI Word fallback failed, trying DeepSeek: {openai_error}")
    return _call_deepseek_for_word(system_prompt, user_prompt, max_tokens=max_tokens, temperature=temperature)


def _extract_json(text: str) -> Dict[str, Any]:
    if not text:
        raise WordProcessingError("ИИ вернул пустой ответ")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise WordProcessingError("ИИ не смог сформировать корректный JSON-план правок") from None


def analyze_word_with_ai(path: str, file_name: str, question: str, user_id: str, reference_context: str = "") -> str:
    text = document_to_text(path)
    question = (question or "Кратко опиши, что находится в Word-документе.").strip()
    system_prompt = (
        "Ты — ассистент, который анализирует Word/DOCX документы. Отвечай на русском. "
        "Используй текст Word-документа и дополнительные документы-основания, если они переданы. "
        "Если часть документа не видна или это скан, честно скажи об этом."
    )
    user_prompt = (
        f"Файл: {file_name}\n\n"
        f"Текст DOCX:\n{text}\n\n"
        f"Дополнительные документы/контекст (ТЗ, КП, PDF, TXT), если были переданы:\n{reference_context or '[нет]'}\n\n"
        f"Вопрос пользователя:\n{question}"
    )
    answer = _call_ai_for_word(system_prompt, user_prompt, max_tokens=3000, temperature=0.2)
    add_to_conversation(user_id, "user", f"[Word {file_name}] {question}\n{text[:5000]}")
    add_to_conversation(user_id, "assistant", answer)
    return answer


def build_word_patch_with_ai(path: str, file_name: str, request_text: str, reference_context: str = "") -> Dict[str, Any]:
    document_text = document_to_text(path, max_paragraphs=220, max_tables=12, max_table_rows=35, max_cell_chars=900)
    reference_context = _clip_text_at_boundary(
        reference_context or "",
        14000,
        "\n... [контекст ТЗ/КП сокращен из-за лимита]",
    )
    system_prompt = """
Ты преобразуешь просьбу пользователя о правке Word/DOCX в безопасный JSON-план.
Отвечай ТОЛЬКО валидным JSON без markdown.
Не выдумывай реквизиты, суммы, даты и юридические условия, которых пользователь не дал. Если точных данных нет, используй понятные заполнители в квадратных скобках: [сумма], [процент], [дата], [этап].

Доступные действия:
1) append_section: добавить раздел в конец документа.
   {"type":"append_section","heading":"Этапность оплат","paragraphs":["1. ...","2. ..."]}
2) insert_after_heading: вставить новый раздел после существующего заголовка/абзаца, который содержит фразу.
   {"type":"insert_after_heading","heading_contains":"оплат","new_heading":"Этапность оплат","paragraphs":["..."]}
3) replace_paragraph_contains: заменить первый абзац, содержащий фразу, на один или несколько новых абзацев.
   {"type":"replace_paragraph_contains","contains":"старый текст или ключевая фраза","paragraphs":["новая редакция..."]}
4) append_paragraphs: добавить абзацы в конец документа без заголовка.
   {"type":"append_paragraphs","paragraphs":["..."]}
5) add_table: добавить таблицу в конец документа.
   {"type":"add_table","heading":"График оплат","headers":["Этап","Срок","Размер оплаты"],"rows":[["1","[дата]","[процент]"]]}

Верни объект:
{
  "need_clarification": false,
  "message": "короткое описание, что будет изменено",
  "output_filename": "имя_исправленного_файла.docx",
  "actions": [ ... ]
}

Правила:
- Если пользователь просит просто проанализировать документ, верни actions: [] и message с кратким пояснением.
- Если пользователь просит добавить этапность оплат, найди раздел про оплату/расчеты. Если такого раздела не видно — добавь новый раздел в конец: «Этапность оплат».
- Если пользователь просит изменить договор по ТЗ/КП/PDF и такие дополнительные документы переданы ниже, используй их как основание для правок. Не проси прислать ТЗ/КП повторно, если в блоке дополнительных документов уже есть релевантный текст.
- Для договора сохраняй деловой юридический стиль, но не утверждай, что это финальная юридическая редакция.
- Не удаляй большие фрагменты, если пользователь явно не просит.
- Не используй макросы, внешние ссылки и произвольный код.
""".strip()
    def make_user_prompt(ref_context: str) -> str:
        return (
            f"Файл: {file_name}\n\n"
            f"Текст DOCX:\n{document_text}\n\n"
            f"Дополнительные документы/контекст (ТЗ, КП, PDF, TXT), если были переданы:\n{ref_context or '[нет]'}\n\n"
            f"Просьба пользователя:\n{request_text}"
        )

    user_prompt = make_user_prompt(reference_context)
    try:
        raw = _call_ai_for_word(system_prompt, user_prompt, max_tokens=7000, temperature=0.0)
    except WordProcessingError as first_error:
        reduced_context = _clip_text_at_boundary(
            reference_context,
            9000,
            "\n... [контекст ТЗ/КП сокращен для повторной попытки]",
        ) if reference_context else ""
        try:
            raw = _call_ai_for_word(system_prompt, make_user_prompt(reduced_context), max_tokens=7000, temperature=0.0)
        except WordProcessingError:
            simple_system_prompt = """
Ты редактируешь DOCX по просьбе пользователя. Ответь только валидным JSON без markdown.
Верни объект:
{
  "need_clarification": false,
  "message": "что будет изменено",
  "output_filename": "edited.docx",
  "actions": [
    {"type":"replace_paragraph_contains","contains":"фраза из договора","paragraphs":["новый текст"]},
    {"type":"append_section","heading":"Название раздела","paragraphs":["текст"]}
  ]
}
Используй ТЗ/КП как основание. Если точное место замены не найдено, добавь новый раздел append_section.
Не проси ТЗ/КП повторно, если они есть в контексте. Не выдумывай неизвестные суммы и реквизиты.
""".strip()
            simple_request = (
                "Сделай минимальный JSON-план правок договора по ТЗ/КП.\n\n"
                f"DOCX:\n{_clip_text_at_boundary(document_text, 12000)}\n\n"
                f"ТЗ/КП:\n{_clip_text_at_boundary(reduced_context, 5000)}\n\n"
                f"Просьба:\n{request_text}\n\n"
                f"Первичная ошибка модели: {first_error}"
            )
            raw = _call_ai_for_word(simple_system_prompt, simple_request, max_tokens=5000, temperature=0.1)
    try:
        patch = _extract_json(raw)
    except WordProcessingError:
        fallback_text = _call_ai_for_word(
            "Ты помощник по договорам. На русском языке кратко перечисли конкретные правки договора по ТЗ/КП. Без markdown-таблиц.",
            (
                f"Договор:\n{_clip_text_at_boundary(document_text, 12000)}\n\n"
                f"ТЗ/КП:\n{_clip_text_at_boundary(reference_context, 7000)}\n\n"
                f"Просьба:\n{request_text}\n\n"
                "Дай готовый раздел с предлагаемыми изменениями для вставки в конец договора."
            ),
            max_tokens=2500,
            temperature=0.2,
        )
        patch = {
            "need_clarification": False,
            "message": "Не удалось надежно собрать точечный JSON-план замен, поэтому добавляю в договор раздел с предлагаемыми правками по ТЗ/КП.",
            "output_filename": f"{Path(file_name).stem}_edited.docx",
            "actions": [
                {
                    "type": "append_section",
                    "heading": "Предлагаемые правки по ТЗ и КП",
                    "paragraphs": [p.strip() for p in fallback_text.splitlines() if p.strip()][:25],
                }
            ],
        }
    if not isinstance(patch, dict):
        raise WordProcessingError("ИИ вернул не объект JSON")
    patch.setdefault("need_clarification", False)
    patch.setdefault("message", "Готовлю изменения в Word-файле.")
    patch.setdefault("actions", [])
    if not isinstance(patch.get("actions"), list):
        raise WordProcessingError("Поле actions должно быть списком")
    return patch


def _copy_paragraph_format(source: Optional[Paragraph], target: Paragraph) -> None:
    if source is None:
        return
    try:
        existing = target._p.pPr
        if existing is not None:
            target._p.remove(existing)
        if source._p.pPr is not None:
            target._p.insert(0, deepcopy(source._p.pPr))
    except Exception:
        pass
    try:
        if source.style is not None:
            target.style = source.style
    except Exception:
        pass


def _copy_run_format(source: Optional[Paragraph], target_run) -> None:
    if source is None:
        return
    ref_run = next((run for run in source.runs if run.text), None)
    if not ref_run:
        return
    try:
        existing = target_run._r.rPr
        if existing is not None:
            target_run._r.remove(existing)
        if ref_run._r.rPr is not None:
            target_run._r.insert(0, deepcopy(ref_run._r.rPr))
    except Exception:
        pass


def _set_paragraph_text_like(paragraph: Paragraph, text: str, reference: Optional[Paragraph] = None) -> None:
    saved_rpr = None
    if reference is not None:
        ref_run = next((run for run in reference.runs if run.text), None)
        if ref_run is not None and ref_run._r.rPr is not None:
            saved_rpr = deepcopy(ref_run._r.rPr)
    for run in paragraph.runs:
        run.text = ""
    if paragraph.runs:
        paragraph.runs[0].text = text
        if saved_rpr is not None:
            existing = paragraph.runs[0]._r.rPr
            if existing is not None:
                paragraph.runs[0]._r.remove(existing)
            paragraph.runs[0]._r.insert(0, saved_rpr)
        else:
            _copy_run_format(reference, paragraph.runs[0])
    else:
        run = paragraph.add_run(text)
        if saved_rpr is not None:
            run._r.insert(0, saved_rpr)
        else:
            _copy_run_format(reference, run)


def _last_text_paragraph(doc: Document) -> Optional[Paragraph]:
    for paragraph in reversed(doc.paragraphs):
        if (paragraph.text or "").strip():
            return paragraph
    return doc.paragraphs[-1] if doc.paragraphs else None


def _body_reference_after(anchor: Optional[Paragraph], doc: Document) -> Optional[Paragraph]:
    if anchor is not None:
        paragraphs = list(doc.paragraphs)
        idx = next((i for i, candidate in enumerate(paragraphs) if candidate._p is anchor._p), None)
        if idx is not None:
            for candidate in paragraphs[idx + 1:]:
                if (candidate.text or "").strip():
                    return candidate
        return anchor
    return _last_text_paragraph(doc)


def _insert_paragraph_after(paragraph: Paragraph, text: str = "", style: Optional[str] = None, format_source: Optional[Paragraph] = None) -> Paragraph:
    new_p = OxmlElement("w:p")
    paragraph._p.addnext(new_p)
    new_para = Paragraph(new_p, paragraph._parent)
    reference = format_source if format_source is not None else paragraph
    _copy_paragraph_format(reference, new_para)
    if style:
        try:
            new_para.style = style
        except Exception:
            pass
    if text:
        _set_paragraph_text_like(new_para, text, reference)
    return new_para


def _clear_and_set_paragraph(paragraph: Paragraph, text: str) -> None:
    # Сохраняем стиль абзаца, но очищаем старые run'ы.
    _set_paragraph_text_like(paragraph, text, paragraph)


def _find_paragraph_containing(doc: Document, needle: str) -> Optional[Paragraph]:
    needle = (needle or "").strip().lower()
    if not needle:
        return None
    for paragraph in doc.paragraphs:
        text = (paragraph.text or "").strip().lower()
        if needle in text:
            return paragraph
    # Более мягкий поиск по словам, если точной фразы нет.
    words = [w for w in re.split(r"\W+", needle) if len(w) >= 4]
    if not words:
        return None
    for paragraph in doc.paragraphs:
        text = (paragraph.text or "").strip().lower()
        matches = sum(1 for w in words if w in text)
        if matches >= max(1, min(2, len(words))):
            return paragraph
    return None


def _add_heading(doc: Document, heading: str, level: int = 2, after: Optional[Paragraph] = None) -> Paragraph:
    if after is not None:
        new_p = OxmlElement("w:p")
        after._p.addnext(new_p)
        paragraph = Paragraph(new_p, after._parent)
        try:
            paragraph.style = f"Heading {level}"
        except Exception:
            pass
        paragraph.add_run(heading)
        return paragraph
    return doc.add_heading(heading, level=level)


def _add_paragraphs(doc: Document, paragraphs: List[str], after: Optional[Paragraph] = None, format_source: Optional[Paragraph] = None) -> Optional[Paragraph]:
    last = after
    if format_source is not None:
        reference = format_source
    elif after is not None:
        reference = after
    else:
        reference = _last_text_paragraph(doc)
    for text in paragraphs or []:
        clean = str(text or "").strip()
        if clean:
            if last is not None:
                last = _insert_paragraph_after(last, clean, format_source=reference if reference is not None else last)
            else:
                p = doc.add_paragraph()
                _copy_paragraph_format(reference, p)
                _set_paragraph_text_like(p, clean, reference)
                last = p
    return last


def _add_table(doc: Document, heading: Optional[str], headers: List[str], rows: List[List[Any]], after: Optional[Paragraph] = None) -> None:
    if heading:
        after = _add_heading(doc, str(heading), level=2, after=after)
    headers = [str(h or "") for h in (headers or [])]
    rows = rows or []
    if not headers and rows:
        headers = [f"Колонка {i+1}" for i in range(max(len(r) for r in rows))]
    if not headers:
        return
    table_style = doc.tables[-1].style if doc.tables else "Table Grid"
    table = doc.add_table(rows=1, cols=len(headers))
    try:
        table.style = table_style
    except Exception:
        table.style = "Table Grid"
    for idx, header in enumerate(headers):
        table.rows[0].cells[idx].text = header
    for row in rows[:100]:
        cells = table.add_row().cells
        for idx in range(len(headers)):
            cells[idx].text = str(row[idx] if idx < len(row) and row[idx] is not None else "")


def apply_word_patch(input_path: str, output_path: str, patch: Dict[str, Any]) -> List[str]:
    doc = Document(input_path)
    changes: List[str] = []

    for action in patch.get("actions", []):
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")

        if action_type == "append_section":
            heading = str(action.get("heading") or "Новый раздел")
            body_ref = _last_text_paragraph(doc)
            heading_para = _add_heading(doc, heading, level=2)
            _add_paragraphs(doc, [str(p) for p in action.get("paragraphs", [])], after=heading_para, format_source=body_ref)
            changes.append(f"Добавлен раздел «{heading}»")

        elif action_type == "insert_after_heading":
            needle = str(action.get("heading_contains") or "")
            heading = str(action.get("new_heading") or action.get("heading") or "Новый раздел")
            anchor = _find_paragraph_containing(doc, needle)
            if not anchor:
                body_ref = _last_text_paragraph(doc)
                heading_para = _add_heading(doc, heading, level=2)
                _add_paragraphs(doc, [str(p) for p in action.get("paragraphs", [])], after=heading_para, format_source=body_ref)
                changes.append(f"Раздел «{heading}» добавлен в конец, так как не найден фрагмент «{needle}»")
            else:
                body_ref = _body_reference_after(anchor, doc)
                last = _insert_paragraph_after(anchor, heading, style="Heading 2", format_source=anchor)
                _add_paragraphs(doc, [str(p) for p in action.get("paragraphs", [])], after=last, format_source=body_ref)
                changes.append(f"Раздел «{heading}» вставлен после фрагмента «{needle}»")

        elif action_type == "replace_paragraph_contains":
            needle = str(action.get("contains") or "")
            paragraphs = [str(p) for p in action.get("paragraphs", []) if str(p or "").strip()]
            if not paragraphs:
                continue
            target = _find_paragraph_containing(doc, needle)
            if target is not None:
                _clear_and_set_paragraph(target, paragraphs[0])
                last = target
                for text in paragraphs[1:]:
                    last = _insert_paragraph_after(last, text, format_source=target)
                changes.append(f"Заменён абзац, содержащий «{needle}»")
            else:
                _add_paragraphs(doc, paragraphs, format_source=_last_text_paragraph(doc))
                changes.append(f"Не найден фрагмент «{needle}», новая редакция добавлена в конец")

        elif action_type == "append_paragraphs":
            paragraphs = [str(p) for p in action.get("paragraphs", [])]
            _add_paragraphs(doc, paragraphs, format_source=_last_text_paragraph(doc))
            changes.append(f"Добавлены абзацы: {len([p for p in paragraphs if p.strip()])}")

        elif action_type == "add_table":
            _add_table(doc, action.get("heading"), action.get("headers") or [], action.get("rows") or [])
            changes.append(f"Добавлена таблица «{action.get('heading') or 'без заголовка'}»")

    doc.save(output_path)
    return changes


def edit_word_with_ai(input_path: str, file_name: str, request_text: str, user_id: str, reference_context: str = "") -> Tuple[Optional[str], str, List[str]]:
    patch = build_word_patch_with_ai(input_path, file_name, request_text, reference_context)
    if patch.get("need_clarification"):
        return None, patch.get("message") or "Нужно уточнить, что именно изменить в Word-файле.", []

    actions = patch.get("actions", [])
    if not actions:
        answer = analyze_word_with_ai(input_path, file_name, request_text, user_id, reference_context)
        return None, answer, []

    base = Path(file_name).stem or "document"
    out_name = _safe_filename(patch.get("output_filename") or f"{base}_edited_{uuid.uuid4().hex[:6]}.docx")
    # Чтобы не перезаписать исходный файл даже при совпадении имени.
    if not Path(out_name).stem.endswith("_edited") and "edited" not in out_name.lower() and "исправ" not in out_name.lower():
        out_name = _safe_filename(f"{Path(out_name).stem}_edited.docx")
    output_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex[:6]}_{out_name}")
    changes = apply_word_patch(input_path, output_path, patch)
    message = patch.get("message") or "Готово, внесла изменения в Word-файл."

    add_to_conversation(user_id, "user", f"[Правка Word {file_name}] {request_text}")
    add_to_conversation(user_id, "assistant", message + "\n" + "\n".join(changes))
    return output_path, message, changes


def _build_word_from_spec(spec: Dict[str, Any], output_path: str) -> None:
    doc = Document()
    title = str(spec.get("title") or "Документ").strip()
    if title:
        p = doc.add_heading(title, level=1)
        try:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        except Exception:
            pass
    for section in spec.get("sections") or []:
        heading = str(section.get("heading") or "").strip()
        if heading:
            doc.add_heading(heading, level=int(section.get("level") or 2))
        _add_paragraphs(doc, [str(p) for p in section.get("paragraphs", [])])
        table = section.get("table")
        if isinstance(table, dict):
            _add_table(doc, None, table.get("headers") or [], table.get("rows") or [])
    doc.save(output_path)


def create_word_from_request(request_text: str) -> Tuple[str, str]:
    system_prompt = """
Ты создаешь структуру Word/DOCX-документа по просьбе пользователя. Отвечай ТОЛЬКО валидным JSON без markdown.
Верни объект:
{
  "filename": "имя_файла.docx",
  "message": "короткое описание файла",
  "title": "Заголовок документа",
  "sections": [
    {"heading":"Раздел", "level":2, "paragraphs":["..."], "table":{"headers":["..."],"rows":[["..."]]}}
  ]
}
Правила:
- Русский деловой стиль.
- Если это договор/акт/письмо, используй аккуратные формулировки и заполнители [ ... ] для неизвестных реквизитов, сумм и дат.
- Не больше 12 разделов.
""".strip()
    user_prompt = f"Создай Word/DOCX-документ по просьбе пользователя:\n{request_text}"
    raw = _call_ai_for_word(system_prompt, user_prompt, max_tokens=3500, temperature=0.2)
    spec = _extract_json(raw)
    file_name = _safe_filename(spec.get("filename") or "created_document.docx")
    output_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4().hex[:6]}_{file_name}")
    _build_word_from_spec(spec, output_path)
    return output_path, spec.get("message") or "Готово, создала Word-файл."


async def handle_word_document(update, context) -> None:
    doc_msg = update.message.document
    file_name = doc_msg.file_name or "document.docx"

    if not is_supported_word_file(file_name):
        await update.message.reply_text(
            "Пока поддерживаю редактирование только .docx. Если у вас .doc/.rtf/.odt — откройте файл в Word и сохраните как .docx, затем отправьте снова."
        )
        return

    user_request = (update.message.caption or "").strip()
    file = await context.bot.get_file(doc_msg.file_id)
    tmp_path = None
    out_path = None

    try:
        await update.message.reply_chat_action(action="typing")
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp_path = tmp.name
        await file.download_to_drive(tmp_path)

        user_id = get_dialog_key(update)
        remember_word_file(context, update.effective_chat.id, tmp_path, file_name, user_id)
        remembered_text = document_to_text(tmp_path, max_paragraphs=260, max_tables=15, max_table_rows=40)
        is_edit_request = looks_like_word_edit_request(user_request)
        if not is_edit_request:
            _remember_reference_document(context, user_id, file_name, remembered_text)
        reference_context = _format_reference_documents(_get_reference_documents(context, user_id))

        if not user_request:
            text = remembered_text
            add_to_conversation(user_id, "user", f"[Word {file_name}]\n{text[:5000]}")
            await update.message.reply_text(
                f"Word-файл {file_name} получен и прочитан.\n"
                "Я запомнила этот файл для текущего чата. Следующим сообщением напишите, что сделать, например:\n"
                "• Проанализируй договор и найди спорные места\n"
                "• Добавь раздел «Этапность оплат»\n"
                "• Замени пункт про сроки выполнения работ\n\n"
                "Я пришлю новую .docx-копию, исходный файл не перезаписывается."
            )
            return

        if is_edit_request:
            out_path, message, changes = await asyncio.to_thread(edit_word_with_ai, tmp_path, file_name, user_request, user_id, reference_context)
            if out_path:
                remember_word_file(context, update.effective_chat.id, out_path, os.path.basename(out_path), user_id)
                text_msg = message
                if changes:
                    text_msg += "\n\nЧто изменено:\n" + "\n".join(f"• {change}" for change in changes[:12])
                await update.message.reply_text(text_msg)
                with open(out_path, "rb") as f:
                    await update.message.reply_document(document=f, filename=os.path.basename(out_path))
            else:
                await update.message.reply_text(message)
        else:
            answer = await asyncio.to_thread(analyze_word_with_ai, tmp_path, file_name, user_request, user_id, reference_context)
            await update.message.reply_text(answer)

    except WordProcessingError as e:
        logger.exception(f"Word AI processing error for {file_name}: {e}")
        await update.message.reply_text(
            "Word-файл прочитан, но не удалось выполнить обработку через ИИ.\n"
            f"Причина: {e}\n\n"
            "Файл не обязательно поврежден. Часто это бывает из-за лимита контекста, сбоя API или некорректного JSON-ответа модели."
        )
    except Exception as e:
        logger.exception(f"Word processing error for {file_name}: {e}")
        await update.message.reply_text(
            "Не удалось обработать Word-файл. Проверьте, что это обычный .docx без пароля и повреждений."
        )
    finally:
        for path in (tmp_path, out_path):
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


async def handle_word_followup_text(update, context, text: str) -> bool:
    dialog_key = get_dialog_key(update)
    info = _get_recent_word_context(context, dialog_key)
    if not info or not _is_word_followup_request(context, text, dialog_key):
        return False

    request_text = (text or "").strip()
    if not request_text:
        return False

    user_id = dialog_key
    input_path = info["path"]
    file_name = info.get("file_name") or "document.docx"
    reference_context = _format_reference_documents(_get_reference_documents(context, dialog_key))
    out_path = None

    try:
        await update.message.reply_chat_action(action="typing")
        if looks_like_word_edit_request(request_text):
            await update.message.reply_text("Поняла. Вношу изменения в последний Word-файл и пришлю новую копию.")
            out_path, message, changes = await asyncio.to_thread(edit_word_with_ai, input_path, file_name, request_text, user_id, reference_context)
            if out_path:
                remember_word_file(context, update.effective_chat.id, out_path, os.path.basename(out_path), dialog_key)
                text_msg = message
                if changes:
                    text_msg += "\n\nЧто изменено:\n" + "\n".join(f"• {change}" for change in changes[:12])
                await update.message.reply_text(text_msg)
                with open(out_path, "rb") as f:
                    await update.message.reply_document(document=f, filename=os.path.basename(out_path))
            else:
                await update.message.reply_text(message)
        else:
            answer = await asyncio.to_thread(analyze_word_with_ai, input_path, file_name, request_text, user_id, reference_context)
            await update.message.reply_text(answer)
        context.user_data.get("awaiting_word_request_by_dialog", {}).pop(dialog_key, None)
        return True
    except WordProcessingError as e:
        logger.exception(f"Word followup AI processing error for {file_name}: {e}")
        await update.message.reply_text(
            "Последний Word-файл прочитан, но не удалось выполнить обработку через ИИ.\n"
            f"Причина: {e}"
        )
        return True
    except Exception as e:
        logger.exception(f"Word followup processing error for {file_name}: {e}")
        await update.message.reply_text(
            "Не удалось выполнить действие с последним Word-файлом. Попробуйте отправить файл ещё раз с подписью-командой."
        )
        return True
    finally:
        if out_path and os.path.exists(out_path):
            try:
                os.unlink(out_path)
            except OSError:
                pass


async def handle_create_word_text(update, context, text: str) -> bool:
    if not is_create_word_request(text):
        return False

    out_path = None
    try:
        await update.message.reply_chat_action(action="upload_document")
        out_path, message = await asyncio.to_thread(create_word_from_request, text)
        remember_word_file(context, update.effective_chat.id, out_path, os.path.basename(out_path), get_dialog_key(update))
        await update.message.reply_text(message)
        with open(out_path, "rb") as f:
            await update.message.reply_document(document=f, filename=os.path.basename(out_path))
        return True
    except Exception as e:
        logger.exception(f"Create Word error: {e}")
        await update.message.reply_text("Не удалось создать Word-файл. Попробуйте описать документ проще: тип документа, разделы и ключевые условия.")
        return True
    finally:
        if out_path and os.path.exists(out_path):
            try:
                os.unlink(out_path)
            except OSError:
                pass
