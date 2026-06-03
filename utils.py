import os
import tempfile
from asyncio import to_thread

import requests
from openai import OpenAI

from config import DEEPSEEK_API_KEY, OPENAI_API_KEY, MAX_RESPONSE_TOKENS, logger
from database import add_to_conversation, get_conversation_history

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


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
            "content": "Ты – ИИ Настенька, ассистент компании ООО «НТЦ Митра». Отвечай на русском, будь вежлива.",
        }
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    data = {
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": MAX_RESPONSE_TOKENS,
        "temperature": 0.7,
    }

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
    """Распознает голосовое сообщение через OpenAI Whisper и отправляет текст в DeepSeek."""
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

        answer = await to_thread(ask_deepseek, recognized_text, str(update.effective_chat.id))
        await update.message.reply_text(answer)
    except Exception as e:
        logger.exception(f"Whisper API error: {e}")
        await update.message.reply_text("Не удалось распознать голосовое сообщение.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_document(update, context):
    """Извлекает текст из PDF/DOCX/TXT с поддержкой разных кодировок."""
    doc = update.message.document
    file_name = doc.file_name or ""
    lower_name = file_name.lower()

    if not lower_name.endswith((".pdf", ".docx", ".txt")):
        await update.message.reply_text("Поддерживаются только PDF/DOCX/TXT")
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

        elif lower_name.endswith(".docx"):
            import docx
            docx_file = docx.Document(tmp_path)
            text_content = "\n".join(p.text for p in docx_file.paragraphs)

        if text_content:
            if len(text_content) > 3000:
                text_content = text_content[:3000] + "..."
            add_to_conversation(str(update.effective_chat.id), "user", f"[Файл {file_name}]:\n{text_content}")
            await update.message.reply_text(f"Файл {file_name} получен и прочитан. Спрашивайте о его содержимом.")
        else:
            await update.message.reply_text("Не удалось извлечь текст из файла.")
    except Exception as e:
        logger.exception(f"Ошибка извлечения текста из {file_name}: {e}")
        await update.message.reply_text("Не удалось извлечь текст из файла.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
