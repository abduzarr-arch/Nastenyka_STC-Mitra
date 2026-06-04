import logging
import os

import pytz

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

ADMIN_IDS = []
for item in os.getenv("ADMIN_IDS", "").split(","):
    item = item.strip()
    if not item:
        continue
    try:
        ADMIN_IDS.append(int(item))
    except ValueError:
        # Неверные значения в ADMIN_IDS просто пропускаем, чтобы бот не падал при старте.
        pass

BOT_NAME = os.getenv("BOT_NAME", "ИИ Настенька")
COMPANY_NAME = os.getenv("COMPANY_NAME", "ООО «НТЦ Митра»")
TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "Europe/Moscow"))

MAX_RESPONSE_TOKENS = int(os.getenv("MAX_RESPONSE_TOKENS", "4000"))
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o-mini")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
