import asyncio
import json
import os
import re
import shutil
import tempfile
import time as time_module
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.shared import Pt
from docx.text.paragraph import Paragraph

from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL, MAX_RESPONSE_TOKENS, logger
from database import add_to_conversation
from group_utils import get_dialog_key

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

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
        else:
            base = os.path.join(tempfile.gettempdir(), "mitra_word_cache")
    os.makedirs(base, exist_ok=True)
    return base


def remember_word_file(context, chat_id: int, source_path: str, file_name: str) -> Dict[str, Any]:
    old = context.user_data.get("last_word") if hasattr(context, "user_data") else None
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
    context.user_data["last_word"] = info
    context.user_data["awaiting_word_request"] = True
    return info


def _get_recent_word_context(context, max_age_seconds: int = 24 * 60 * 60) -> Optional[Dict[str, Any]]:
    info = context.user_data.get("last_word") if hasattr(context, "user_data") else None
    if not info:
        return None
    path = info.get("path")
    if not path or not os.path.exists(path):
        context.user_data.pop("last_word", None)
        context.user_data.pop("awaiting_word_request", None)
        return None
    if time_module.time() - float(info.get("saved_at") or 0) > max_age_seconds:
        context.user_data.pop("last_word", None)
        context.user_data.pop("awaiting_word_request", None)
        return None
    return info


def _is_word_followup_request(context, text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    if context.user_data.get("awaiting_word_request"):
        return True
    if looks_like_word_edit_request(text):
        return True
    return any(word in lowered for word in ("word", "docx", "ворд", "договор", "пункт", "раздел", "оплат", "расчет"))


def _trim_text(text: str, limit: int = 55000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [текст документа обрезан из-за размера файла]"


def document_to_text(path: str, max_paragraphs: int = 260, max_tables: int = 15, max_table_rows: int = 40) -> str:
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
                values.append(cell_text[:180])
            parts.append(" | ".join(values))
        if len(table.rows) > max_table_rows:
            parts.append(f"... показаны первые {max_table_rows} строк таблицы")

    if not parts:
        parts.append("[Документ не содержит извлекаемого текста или состоит из изображений/сканов]")
    return _trim_text("\n".join(parts))


def _call_deepseek_for_word(system_prompt: str, user_prompt: str, max_tokens: int = 3000, temperature: float = 0.1) -> str:
    if not DEEPSEEK_API_KEY:
        raise WordProcessingError("Не настроен DEEPSEEK_API_KEY")

    data = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": min(MAX_RESPONSE_TOKENS, max_tokens),
        "temperature": temperature,
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    response = requests.post(DEEPSEEK_API_URL, headers=headers, json=data, timeout=140)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


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
        raise


def analyze_word_with_ai(path: str, file_name: str, question: str, user_id: str) -> str:
    text = document_to_text(path)
    question = (question or "Кратко опиши, что находится в Word-документе.").strip()
    system_prompt = (
        "Ты — ассистент, который анализирует Word/DOCX документы. Отвечай на русском. "
        "Используй только текст документа, который дан пользователем. Если часть документа не видна или это скан, честно скажи об этом."
    )
    user_prompt = f"Файл: {file_name}\n\nТекст DOCX:\n{text}\n\nВопрос пользователя:\n{question}"
    answer = _call_deepseek_for_word(system_prompt, user_prompt, max_tokens=3000, temperature=0.2)
    add_to_conversation(user_id, "user", f"[Word {file_name}] {question}\n{text[:5000]}")
    add_to_conversation(user_id, "assistant", answer)
    return answer


def build_word_patch_with_ai(path: str, file_name: str, request_text: str) -> Dict[str, Any]:
    document_text = document_to_text(path, max_paragraphs=320, max_tables=20, max_table_rows=60)
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
- Для договора сохраняй деловой юридический стиль, но не утверждай, что это финальная юридическая редакция.
- Не удаляй большие фрагменты, если пользователь явно не просит.
- Не используй макросы, внешние ссылки и произвольный код.
""".strip()
    user_prompt = f"Файл: {file_name}\n\nТекст DOCX:\n{document_text}\n\nПросьба пользователя:\n{request_text}"
    raw = _call_deepseek_for_word(system_prompt, user_prompt, max_tokens=3600, temperature=0.0)
    patch = _extract_json(raw)
    if not isinstance(patch, dict):
        raise WordProcessingError("ИИ вернул не объект JSON")
    patch.setdefault("need_clarification", False)
    patch.setdefault("message", "Готовлю изменения в Word-файле.")
    patch.setdefault("actions", [])
    if not isinstance(patch.get("actions"), list):
        raise WordProcessingError("Поле actions должно быть списком")
    return patch


def _insert_paragraph_after(paragraph: Paragraph, text: str = "", style: Optional[str] = None) -> Paragraph:
    new_p = OxmlElement("w:p")
    paragraph._p.addnext(new_p)
    new_para = Paragraph(new_p, paragraph._parent)
    if style:
        try:
            new_para.style = style
        except Exception:
            pass
    if text:
        new_para.add_run(text)
    return new_para


def _clear_and_set_paragraph(paragraph: Paragraph, text: str) -> None:
    # Сохраняем стиль абзаца, но очищаем старые run'ы.
    for run in paragraph.runs:
        run.text = ""
    if paragraph.runs:
        paragraph.runs[0].text = text
    else:
        paragraph.add_run(text)


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


def _add_heading(doc: Document, heading: str, level: int = 2) -> Paragraph:
    paragraph = doc.add_heading(heading, level=level)
    return paragraph


def _add_paragraphs(doc: Document, paragraphs: List[str]) -> None:
    for text in paragraphs or []:
        clean = str(text or "").strip()
        if clean:
            p = doc.add_paragraph(clean)
            try:
                p.paragraph_format.space_after = Pt(6)
            except Exception:
                pass


def _add_table(doc: Document, heading: Optional[str], headers: List[str], rows: List[List[Any]]) -> None:
    if heading:
        _add_heading(doc, str(heading), level=2)
    headers = [str(h or "") for h in (headers or [])]
    rows = rows or []
    if not headers and rows:
        headers = [f"Колонка {i+1}" for i in range(max(len(r) for r in rows))]
    if not headers:
        return
    table = doc.add_table(rows=1, cols=len(headers))
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
            _add_heading(doc, heading, level=2)
            _add_paragraphs(doc, [str(p) for p in action.get("paragraphs", [])])
            changes.append(f"Добавлен раздел «{heading}»")

        elif action_type == "insert_after_heading":
            needle = str(action.get("heading_contains") or "")
            heading = str(action.get("new_heading") or action.get("heading") or "Новый раздел")
            anchor = _find_paragraph_containing(doc, needle)
            if not anchor:
                _add_heading(doc, heading, level=2)
                _add_paragraphs(doc, [str(p) for p in action.get("paragraphs", [])])
                changes.append(f"Раздел «{heading}» добавлен в конец, так как не найден фрагмент «{needle}»")
            else:
                last = _insert_paragraph_after(anchor, heading, style="Heading 2")
                for paragraph_text in action.get("paragraphs", []) or []:
                    last = _insert_paragraph_after(last, str(paragraph_text or ""))
                changes.append(f"Раздел «{heading}» вставлен после фрагмента «{needle}»")

        elif action_type == "replace_paragraph_contains":
            needle = str(action.get("contains") or "")
            paragraphs = [str(p) for p in action.get("paragraphs", []) if str(p or "").strip()]
            if not paragraphs:
                continue
            target = _find_paragraph_containing(doc, needle)
            if target:
                _clear_and_set_paragraph(target, paragraphs[0])
                last = target
                for text in paragraphs[1:]:
                    last = _insert_paragraph_after(last, text)
                changes.append(f"Заменён абзац, содержащий «{needle}»")
            else:
                _add_paragraphs(doc, paragraphs)
                changes.append(f"Не найден фрагмент «{needle}», новая редакция добавлена в конец")

        elif action_type == "append_paragraphs":
            paragraphs = [str(p) for p in action.get("paragraphs", [])]
            _add_paragraphs(doc, paragraphs)
            changes.append(f"Добавлены абзацы: {len([p for p in paragraphs if p.strip()])}")

        elif action_type == "add_table":
            _add_table(doc, action.get("heading"), action.get("headers") or [], action.get("rows") or [])
            changes.append(f"Добавлена таблица «{action.get('heading') or 'без заголовка'}»")

    doc.save(output_path)
    return changes


def edit_word_with_ai(input_path: str, file_name: str, request_text: str, user_id: str) -> Tuple[Optional[str], str, List[str]]:
    patch = build_word_patch_with_ai(input_path, file_name, request_text)
    if patch.get("need_clarification"):
        return None, patch.get("message") or "Нужно уточнить, что именно изменить в Word-файле.", []

    actions = patch.get("actions", [])
    if not actions:
        answer = analyze_word_with_ai(input_path, file_name, request_text, user_id)
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
    raw = _call_deepseek_for_word(system_prompt, user_prompt, max_tokens=3500, temperature=0.2)
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
        remember_word_file(context, update.effective_chat.id, tmp_path, file_name)

        if not user_request:
            text = document_to_text(tmp_path, max_paragraphs=40, max_tables=4)
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

        if looks_like_word_edit_request(user_request):
            out_path, message, changes = await asyncio.to_thread(edit_word_with_ai, tmp_path, file_name, user_request, user_id)
            if out_path:
                remember_word_file(context, update.effective_chat.id, out_path, os.path.basename(out_path))
                text_msg = message
                if changes:
                    text_msg += "\n\nЧто изменено:\n" + "\n".join(f"• {change}" for change in changes[:12])
                await update.message.reply_text(text_msg)
                with open(out_path, "rb") as f:
                    await update.message.reply_document(document=f, filename=os.path.basename(out_path))
            else:
                await update.message.reply_text(message)
        else:
            answer = await asyncio.to_thread(analyze_word_with_ai, tmp_path, file_name, user_request, user_id)
            await update.message.reply_text(answer)

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
    info = _get_recent_word_context(context)
    if not info or not _is_word_followup_request(context, text):
        return False

    request_text = (text or "").strip()
    if not request_text:
        return False

    user_id = get_dialog_key(update)
    input_path = info["path"]
    file_name = info.get("file_name") or "document.docx"
    out_path = None

    try:
        await update.message.reply_chat_action(action="typing")
        if looks_like_word_edit_request(request_text):
            await update.message.reply_text("Поняла. Вношу изменения в последний Word-файл и пришлю новую копию.")
            out_path, message, changes = await asyncio.to_thread(edit_word_with_ai, input_path, file_name, request_text, user_id)
            if out_path:
                remember_word_file(context, update.effective_chat.id, out_path, os.path.basename(out_path))
                text_msg = message
                if changes:
                    text_msg += "\n\nЧто изменено:\n" + "\n".join(f"• {change}" for change in changes[:12])
                await update.message.reply_text(text_msg)
                with open(out_path, "rb") as f:
                    await update.message.reply_document(document=f, filename=os.path.basename(out_path))
            else:
                await update.message.reply_text(message)
        else:
            answer = await asyncio.to_thread(analyze_word_with_ai, input_path, file_name, request_text, user_id)
            await update.message.reply_text(answer)
        context.user_data["awaiting_word_request"] = False
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
        remember_word_file(context, update.effective_chat.id, out_path, os.path.basename(out_path))
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
