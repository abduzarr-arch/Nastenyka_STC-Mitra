import base64
import mimetypes
import os
import tempfile
import time
from asyncio import to_thread

import requests
from openai import OpenAI

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    DEEPSEEK_REASONING_EFFORT,
    DEEPSEEK_THINKING,
    OPENAI_API_KEY,
    MAX_RESPONSE_TOKENS,
    VISION_MODEL,
    logger,
)
from database import add_to_conversation, get_conversation_history
from reminders import handle_reminder_request
from excel_utils import handle_excel_document, is_excel_file
from docx_utils import handle_word_document, is_word_file
from cross_chat import remember_current_group_chat, remember_visible_chat_participants
from group_utils import clean_group_trigger_text, get_dialog_key, is_addressed_to_bot, is_group_chat
from internet_search import answer_online, should_use_online_search

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def remember_reference_document(context, dialog_key: str, file_name: str, text: str, max_docs: int = 5) -> None:
    """Запоминает PDF/TXT как рабочий контекст для правки Word в этом диалоге."""
    if not text or not hasattr(context, "user_data"):
        return
    refs_by_dialog = context.user_data.setdefault("reference_documents_by_dialog", {})
    items = refs_by_dialog.setdefault(str(dialog_key), [])
    items.append(
        {
            "file_name": file_name or "document",
            "text": text[:12000],
            "saved_at": time.time(),
        }
    )
    refs_by_dialog[str(dialog_key)] = items[-max_docs:]


def ask_deepseek(prompt: str, user_id: str) -> str:
    if not DEEPSEEK_API_KEY:
        logger.error("DEEPSEEK_API_KEY is missing")
        return "Не настроен ключ DeepSeek. Сообщите администратору."

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    history = get_conversation_history(user_id, limit=20)
    messages = [
        {
            "role": "system",
            "content": "Ты – ИИ Настенька, ассистент компании ООО «НТЦ Митра». Отвечай на русском, будь вежлива. Важно: не обещай самостоятельно поставить напоминание. Если пользователь просит напоминание, а система не обработала его автоматически, попроси написать в формате: Напомни мне в ЧЧ:ММ что ...",
        }
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    data = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "max_tokens": MAX_RESPONSE_TOKENS,
        "temperature": 0.7,
    }
    if DEEPSEEK_THINKING:
        data["thinking"] = {"type": "enabled"}
        data["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT

    try:
        response = requests.post(DEEPSEEK_API_URL, headers=headers, json=data, timeout=90)
        response.raise_for_status()
        answer = response.json()["choices"][0]["message"]["content"]
        add_to_conversation(user_id, "user", prompt)
        add_to_conversation(user_id, "assistant", answer)
        return answer
    except Exception as e:
        logger.exception(f"DeepSeek error: {e}")
        return "Извините, сервис временно недоступен. Попробуйте позже."


async def handle_voice_message(update, context):
    if is_group_chat(update):
        remember_current_group_chat(update)
        await remember_visible_chat_participants(update, context)
    """Распознает голосовое сообщение через OpenAI Whisper и отправляет текст в DeepSeek."""
    if is_group_chat(update) and not await is_addressed_to_bot(update, context):
        return

    if not openai_client:
        await update.message.reply_text("Настройки распознавания голоса отсутствуют. Сообщите администратору.")
        return

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await file.download_to_drive(tmp_path)

        with open(tmp_path, "rb") as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="ru",
            )

        recognized_text = transcript.text.strip()
        logger.info(f"Распознанный текст: {recognized_text}")
        await update.message.reply_text(f"🎙 Распознано: {recognized_text}")

        if await handle_reminder_request(update, context, recognized_text):
            return

        if should_use_online_search(recognized_text):
            answer = await to_thread(answer_online, recognized_text, get_dialog_key(update))
        else:
            answer = await to_thread(ask_deepseek, recognized_text, get_dialog_key(update))
        await update.message.reply_text(answer)
    except Exception as e:
        logger.exception(f"Whisper API error: {e}")
        await update.message.reply_text("Не удалось распознать голосовое сообщение.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _guess_mime_type(file_name: str, fallback: str = "image/jpeg") -> str:
    mime_type, _ = mimetypes.guess_type(file_name or "")
    return mime_type or fallback


def analyze_image_with_openai(image_path: str, question: str, user_id: str, mime_type: str = "image/jpeg") -> str:
    """Отправляет изображение и вопрос в OpenAI vision-модель."""
    if not openai_client:
        logger.error("OPENAI_API_KEY is missing")
        return "Не настроен ключ OpenAI для анализа изображений. Сообщите администратору."

    question = (question or "Опиши, что изображено на картинке, и отметь важные детали.").strip()

    try:
        with open(image_path, "rb") as f:
            image_base64 = base64.b64encode(f.read()).decode("utf-8")

        response = openai_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты – ИИ Настенька, ассистент компании ООО «НТЦ Митра». "
                        "Отвечай на русском. Анализируй изображение внимательно, "
                        "но честно говори, если деталь плохо видна или уверенность низкая."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                        },
                    ],
                },
            ],
            max_tokens=min(MAX_RESPONSE_TOKENS, 2000),
            temperature=0.2,
        )

        answer = response.choices[0].message.content or "Не удалось получить ответ по изображению."
        add_to_conversation(user_id, "user", f"[Изображение] {question}")
        add_to_conversation(user_id, "assistant", answer)
        return answer
    except Exception as e:
        logger.exception(f"OpenAI vision error: {e}")
        return "Не удалось проанализировать изображение. Проверьте ключ OpenAI и доступность vision-модели."


async def handle_photo_message(update, context):
    if is_group_chat(update):
        remember_current_group_chat(update)
        await remember_visible_chat_participants(update, context)
    """Анализирует фото из Telegram через OpenAI vision-модель."""
    if not update.message:
        return
    if is_group_chat(update) and not await is_addressed_to_bot(update, context):
        return

    photo = update.message.photo[-1] if update.message.photo else None
    if not photo:
        await update.message.reply_text("Не удалось получить изображение.")
        return

    question = update.message.caption or "Опиши, что изображено на картинке."
    if is_group_chat(update):
        question = await clean_group_trigger_text(update, context, question) or "Опиши, что изображено на картинке."
    file = await context.bot.get_file(photo.file_id)
    tmp_path = None

    try:
        await update.message.reply_chat_action(action="typing")
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        await file.download_to_drive(tmp_path)

        answer = await to_thread(
            analyze_image_with_openai,
            tmp_path,
            question,
            get_dialog_key(update),
            "image/jpeg",
        )
        await update.message.reply_text(answer)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_image_document(update, context):
    if is_group_chat(update):
        remember_current_group_chat(update)
        await remember_visible_chat_participants(update, context)
    """Анализирует изображение, отправленное как файл/документ."""
    doc = update.message.document
    file_name = doc.file_name or "image"
    question = update.message.caption or "Опиши, что изображено на картинке."
    if is_group_chat(update):
        question = await clean_group_trigger_text(update, context, question) or "Опиши, что изображено на картинке."
    file = await context.bot.get_file(doc.file_id)
    suffix = os.path.splitext(file_name)[1] or ".jpg"
    mime_type = doc.mime_type or _guess_mime_type(file_name)
    tmp_path = None

    try:
        await update.message.reply_chat_action(action="typing")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await file.download_to_drive(tmp_path)

        answer = await to_thread(
            analyze_image_with_openai,
            tmp_path,
            question,
            get_dialog_key(update),
            mime_type,
        )
        await update.message.reply_text(answer)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_document(update, context):
    if is_group_chat(update):
        remember_current_group_chat(update)
        await remember_visible_chat_participants(update, context)
    """Извлекает текст из PDF/DOCX/TXT, анализирует изображения и обрабатывает Excel-файлы."""
    if is_group_chat(update) and not await is_addressed_to_bot(update, context):
        return

    doc = update.message.document
    file_name = doc.file_name or ""
    lower_name = file_name.lower()

    if doc.mime_type and doc.mime_type.startswith("image/"):
        await handle_image_document(update, context)
        return

    if lower_name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        await handle_image_document(update, context)
        return

    if is_excel_file(file_name):
        await handle_excel_document(update, context)
        return

    if is_word_file(file_name):
        await handle_word_document(update, context)
        return

    if not lower_name.endswith((".pdf", ".txt")):
        await update.message.reply_text("Поддерживаются PDF/TXT, Word .DOCX, Excel .XLSX и изображения JPG/PNG/WEBP/GIF")
        return

    file = await context.bot.get_file(doc.file_id)
    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=os.path.splitext(file_name)[1], delete=False) as tmp:
            tmp_path = tmp.name
        await file.download_to_drive(tmp_path)

        text_content = ""
        if lower_name.endswith(".txt"):
            for encoding in ["utf-8", "cp1251", "koi8-r", "latin-1"]:
                try:
                    with open(tmp_path, "r", encoding=encoding) as f:
                        text_content = f.read()
                    break
                except UnicodeDecodeError:
                    continue
            if not text_content:
                with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
                    text_content = f.read()
                text_content = "[Предупреждение: часть символов потеряна]\n" + text_content

        elif lower_name.endswith(".pdf"):
            import fitz
            with fitz.open(tmp_path) as pdf:
                text_content = "\n".join(page.get_text() for page in pdf)

        if text_content:
            dialog_key = get_dialog_key(update)
            remember_reference_document(context, dialog_key, file_name, text_content)
            if len(text_content) > 3000:
                text_content = text_content[:3000] + "..."
            add_to_conversation(dialog_key, "user", f"[Файл {file_name}]:\n{text_content}")
            await update.message.reply_text(f"Файл {file_name} получен и прочитан. Спрашивайте о его содержимом.")
        else:
            await update.message.reply_text("Не удалось извлечь текст из файла.")
    except Exception as e:
        logger.exception(f"Ошибка извлечения текста из {file_name}: {e}")
        await update.message.reply_text("Не удалось извлечь текст из файла.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
