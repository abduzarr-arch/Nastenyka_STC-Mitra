import os
import logging
import pytz

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")   # <-- новый ключ для Whisper
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]

BOT_NAME = "ИИ Настенька"
COMPANY_NAME = "ООО «НТЦ Митра»"
TIMEZONE = pytz.timezone('Europe/Moscow')

MAX_CONTEXT_TOKENS = 900_000
MAX_RESPONSE_TOKENS = 8000

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)