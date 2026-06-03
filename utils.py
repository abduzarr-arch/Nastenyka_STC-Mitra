import requests
import tempfile
import os
import openai
from config import DEEPSEEK_API_KEY, OPENAI_API_KEY, logger, MAX_RESPONSE_TOKENS
from database import add_to_conversation, get_conversation_history

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
    if not OPENAI_API_KEY:
        await update.message.reply_text("Настройки распознавания голоса отсутствуют. Сообщите администратору.")
        return

    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as audio_file:
            transcript = openai.Audio.transcribe(
                model="whisper-1",
                file=audio_file,
                language="ru"
            )
        recognized_text = transcript.text
        logger.info(f"Распознанный текст: {recognized_text}")
        add_to_conversation(str(update.effective_user.id), "user", f"[Голосовое]: {recognized_text}")
        await update.message.reply_text(f"?? Распознано: {recognized_text}")
    except Exception as e:
        logger.error(f"Whisper API error: {e}")
        await update.message.reply_text("Не удалось распознать голосовое сообщение.")
    finally:
        os.unlink(tmp_path)

async def handle_document(update, context):
    """Извлекает текст из PDF/DOCX/TXT с поддержкой разных кодировок."""
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
    try:
        if file_name.endswith('.txt'):
            for encoding in ['utf-8', 'cp1251', 'latin-1', 'koi8-r']:
                try:
                    with open(tmp_path, 'r', encoding=encoding) as f:
                        text_content = f.read()
                    break
                except UnicodeDecodeError:
                    continue
            if not text_content:
                with open(tmp_path, 'r', encoding='utf-8', errors='ignore') as f:
                    text_content = f.read()
                text_content = "[Предупреждение: часть символов потеряна]\n" + text_content
        
        elif file_name.endswith('.pdf'):
            import fitz
            with fitz.open(tmp_path) as pdf:
                for page in pdf:
                    text_content += page.get_text()
        
        elif file_name.endswith('.docx'):
            import docx
            docx_file = docx.Document(tmp_path)
            text_content = "\n".join([p.text for p in docx_file.paragraphs])
    
    except Exception as e:
        logger.error(f"Ошибка извлечения текста из {file_name}: {e}")
        text_content = ""
    finally:
        os.unlink(tmp_path)
    
    if text_content:
        if len(text_content) > 3000:
            text_content = text_content[:3000] + "..."
        add_to_conversation(str(update.effective_user.id), "user", f"[Файл {file_name}]:\n{text_content}")
        await update.message.reply_text(f"Файл {file_name} получен и прочитан. Спрашивайте о его содержимом.")
    else:
        await update.message.reply_text("Не удалось извлечь текст из файла.")