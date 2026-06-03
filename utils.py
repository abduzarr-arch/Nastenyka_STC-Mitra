import requests
import tempfile
import os
import openai
from config import DEEPSEEK_API_KEY, OPENAI_API_KEY, logger, MAX_RESPONSE_TOKENS
from database import add_to_conversation, get_conversation_history

# Инициализация OpenAI (для Whisper)
openai.api_key = OPENAI_API_KEY

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

def ask_deepseek(prompt: str, user_id: str) -> str:
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    history = get_conversation_history(user_id, limit=20)
    messages = [{"role": "system", "content": "Ты – ИИ Настенька, ассистент компании ООО «НТЦ Митра». Отвечай на русском, будь вежлива, знай время."}]
    for msg in history:
        messages.append(msg)
    messages.append({"role": "user", "content": prompt})

    data = {
        "model": "deepseek-chat",
        "messages": messages,
        "max_tokens": MAX_RESPONSE_TOKENS,
        "temperature": 0.7
    }
    try:
        r = requests.post(DEEPSEEK_API_URL, headers=headers, json=data, timeout=90)
        r.raise_for_status()
        response = r.json()["choices"][0]["message"]["content"]
        add_to_conversation(user_id, "user", prompt)
        add_to_conversation(user_id, "assistant", response)
        return response
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")
        return "Извините, сервис временно недоступен."

async def handle_voice_message(update, context):
    """Распознает голосовое сообщение через OpenAI Whisper API."""
    voice = update.message.voice
    # Проверяем, что есть API-ключ
    if not OPENAI_API_KEY:
        await update.message.reply_text("Настройки распознавания голоса отсутствуют. Сообщите администратору.")
        return

    # Скачиваем голосовое сообщение
    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        # Открываем файл и отправляем в Whisper API
        with open(tmp_path, "rb") as audio_file:
            transcript = openai.Audio.transcribe(
                model="whisper-1",
                file=audio_file,
                language="ru"  # русский язык
            )
        recognized_text = transcript.text
        logger.info(f"Распознанный текст: {recognized_text}")

        # Сохраняем текст в историю как сообщение пользователя
        add_to_conversation(str(update.effective_user.id), "user", f"[Голосовое]: {recognized_text}")
        await update.message.reply_text(f"?? Распознано: {recognized_text}")
        # Здесь можно также сразу ответить на вопрос, если нужно
        # Например, обработать recognised_text как обычное сообщение
        # await handle_text(recognized_text, update, context)

    except Exception as e:
        logger.error(f"Whisper API error: {e}")
        await update.message.reply_text("Не удалось распознать голосовое сообщение. Попробуйте отправить текст.")
    finally:
        os.unlink(tmp_path)  # удаляем временный файл

async def handle_document(update, context):
    """Извлекает текст из PDF/DOCX/TXT."""
    doc = update.message.document
    file_name = doc.file_name
    if not (file_name.endswith('.pdf') or file_name.endswith('.docx') or file_name.endswith('.txt')):
        await update.message.reply_text("Поддерживаются только PDF, DOCX, TXT")
        return
    file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(file_name)[1], delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    text_content = ""
    if file_name.endswith('.txt'):
        with open(tmp_path, 'r', encoding='utf-8') as f:
            text_content = f.read()
    elif file_name.endswith('.pdf'):
        try:
            import fitz  # PyMuPDF
            with fitz.open(tmp_path) as pdf:
                for page in pdf:
                    text_content += page.get_text()
        except ImportError:
            text_content = "Для PDF нужна библиотека PyMuPDF, но она не установлена."
    elif file_name.endswith('.docx'):
        try:
            import docx
            docx_file = docx.Document(tmp_path)
            text_content = "\n".join([p.text for p in docx_file.paragraphs])
        except ImportError:
            text_content = "Для DOCX нужна библиотека python-docx, но она не установлена."

    os.unlink(tmp_path)
    if text_content:
        # Обрезаем слишком длинный текст, чтобы не перегружать базу
        if len(text_content) > 3000:
            text_content = text_content[:3000] + "..."
        add_to_conversation(str(update.effective_user.id), "user", f"[Файл {file_name}]:\n{text_content}")
        await update.message.reply_text(f"Файл {file_name} получен и прочитан. Спрашивайте о его содержимом.")
    else:
        await update.message.reply_text("Не удалось извлечь текст из файла.")