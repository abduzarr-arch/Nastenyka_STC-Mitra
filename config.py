import logging
import os

import pytz

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

# DeepSeek model settings. deepseek-chat works for usual answers;
# deepseek-v4-pro/deepseek-reasoner can be enabled if available on your tariff.
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_THINKING = os.getenv("DEEPSEEK_THINKING", "false").strip().lower() in {"1", "true", "yes", "on", "да"}
DEEPSEEK_REASONING_EFFORT = os.getenv("DEEPSEEK_REASONING_EFFORT", "medium")

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
WORD_AI_PROVIDER = os.getenv("WORD_AI_PROVIDER", "deepseek").strip().lower()
WORD_OPENAI_MODEL = os.getenv("WORD_OPENAI_MODEL", "gpt-4o")

# Онлайн-поиск. Рекомендуемый режим: TAVILY_API_KEY + DeepSeek.
# Если TAVILY_API_KEY не задан, бот попробует OpenAI web_search через OPENAI_API_KEY.
ONLINE_SEARCH_ENABLED = os.getenv("ONLINE_SEARCH_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off", "нет"}
ONLINE_SEARCH_MODEL = os.getenv("ONLINE_SEARCH_MODEL", "gpt-5.5")
TAVILY_MAX_RESULTS = int(os.getenv("TAVILY_MAX_RESULTS", "5"))

# Режим работы в группах. По умолчанию бот отвечает в группе только на обращение к нему.
# Дополнительные слова/теги можно задать через запятую, например:
# GROUP_TRIGGER_WORDS=настенька,#настенька,бот
GROUP_TRIGGER_WORDS = [
    item.strip().lower()
    for item in os.getenv("GROUP_TRIGGER_WORDS", "").split(",")
    if item.strip()
]

GROUP_AGREEMENTS_LIMIT = int(os.getenv("GROUP_AGREEMENTS_LIMIT", "15"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
