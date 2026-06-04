"""Онлайн-поиск для Telegram-бота.

Схема работы:
1) Если есть TAVILY_API_KEY, бот ищет свежие источники через Tavily,
   а итоговый ответ формирует через DeepSeek с переданным контекстом.
2) Если TAVILY_API_KEY нет, но есть OPENAI_API_KEY, бот использует встроенный
   web_search OpenAI как запасной вариант.

Это сделано специально: у DeepSeek на сайте есть кнопка Internet Search, но
в обычном API нет простого параметра "включить интернет". Поэтому для бота
нужен внешний поисковый инструмент.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from openai import OpenAI

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_MODEL,
    DEEPSEEK_REASONING_EFFORT,
    DEEPSEEK_THINKING,
    MAX_RESPONSE_TOKENS,
    ONLINE_SEARCH_ENABLED,
    ONLINE_SEARCH_MODEL,
    OPENAI_API_KEY,
    TAVILY_API_KEY,
    TAVILY_MAX_RESULTS,
    TIMEZONE,
    logger,
)
from database import add_to_conversation
from group_utils import clean_group_trigger_text, is_addressed_to_bot, is_group_chat

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"

_OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

EXPLICIT_SEARCH_PATTERNS = [
    r"^/search\b",
    r"\bнайди\s+(?:в\s+)?интернет",
    r"\bпоищи\s+(?:в\s+)?интернет",
    r"\bпосмотри\s+(?:в\s+)?интернет",
    r"\bпроверь\s+(?:онлайн|в\s+интернет)",
    r"\bпоиск\s+(?:онлайн|в\s+интернет)",
    r"\bс\s+уч[её]том\s+актуальн",
    r"\bактуальн(?:ая|ые|ый|ое|ую)?\s+(?:информация|данные|цены|новости)",
]

SOFT_CURRENT_MARKERS = [
    "сегодня",
    "сейчас",
    "на сегодня",
    "актуально",
    "последние новости",
    "свежие новости",
    "новости",
    "цена",
    "цены",
    "курс",
    "котировки",
    "расписание",
    "закон",
    "изменения",
    "2026",
]


def _bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "да", "on"}


def should_use_online_search(text: str) -> bool:
    """Определяет, нужно ли включать онлайн-поиск для обычного сообщения."""
    if not ONLINE_SEARCH_ENABLED:
        return False
    if not text:
        return False

    low = text.lower().strip()
    if any(re.search(pattern, low) for pattern in EXPLICIT_SEARCH_PATTERNS):
        return True

    # Не включаем интернет на каждую бытовую фразу, только если есть явный намек
    # на свежие или проверяемые данные.
    return any(marker in low for marker in SOFT_CURRENT_MARKERS)


def _clean_query(text: str) -> str:
    query = re.sub(r"^/search(?:@\w+)?\s*", "", text.strip(), flags=re.IGNORECASE)
    query = re.sub(r"^(найди|поищи|посмотри|проверь)\s+(в\s+интернете|онлайн|в\s+интернет)?\s*", "", query, flags=re.IGNORECASE)
    return query.strip() or text.strip()


def _looks_like_news_query(query: str) -> bool:
    low = query.lower()
    return any(word in low for word in ["новости", "сегодня", "последние", "свежие", "произошло"])


def search_tavily(query: str) -> List[Dict[str, str]]:
    """Возвращает список источников Tavily: title/url/content."""
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY не задан")

    payload: Dict[str, Any] = {
        "query": query,
        "search_depth": "basic",
        "max_results": max(1, min(TAVILY_MAX_RESULTS, 10)),
        "include_answer": False,
        "include_raw_content": False,
        "topic": "news" if _looks_like_news_query(query) else "general",
    }

    response = requests.post(
        TAVILY_SEARCH_URL,
        headers={
            "Authorization": f"Bearer {TAVILY_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    results = []
    for item in data.get("results", []):
        title = str(item.get("title") or "Без названия").strip()
        url = str(item.get("url") or "").strip()
        content = str(item.get("content") or item.get("snippet") or "").strip()
        if url:
            results.append({"title": title, "url": url, "content": content[:1200]})
    return results


def _format_sources_for_prompt(results: List[Dict[str, str]]) -> str:
    lines = []
    for idx, item in enumerate(results, start=1):
        lines.append(
            f"Источник {idx}: {item['title']}\n"
            f"URL: {item['url']}\n"
            f"Фрагмент: {item.get('content') or 'нет фрагмента'}"
        )
    return "\n\n".join(lines)


def _format_sources_for_user(results: List[Dict[str, str]]) -> str:
    lines = []
    for idx, item in enumerate(results[:TAVILY_MAX_RESULTS], start=1):
        title = item.get("title") or "Источник"
        url = item.get("url") or ""
        lines.append(f"{idx}. {title}\n{url}")
    return "\n".join(lines)


def answer_with_deepseek_and_sources(query: str, user_id: str) -> str:
    """Ищет источники через Tavily и формирует ответ через DeepSeek."""
    if not DEEPSEEK_API_KEY:
        return "Не настроен DEEPSEEK_API_KEY. Для режима Tavily + DeepSeek нужен ключ DeepSeek."

    results = search_tavily(query)
    if not results:
        return "Не нашла подходящих результатов в интернете. Попробуйте сформулировать запрос точнее."

    today = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M %Z")
    sources_block = _format_sources_for_prompt(results)

    system_prompt = (
        "Ты – ИИ Настенька, ассистент компании ООО «НТЦ Митра». "
        "Отвечай на русском. Пользователь просит ответ с учетом онлайн-поиска. "
        "Используй только переданные источники и явно отделяй факты из источников от своих выводов. "
        "Если источники не подтверждают часть ответа, честно скажи об этом. "
        "В конце добавь короткий блок 'Источники' со списком номеров источников, на которые опирался ответ."
    )

    user_prompt = (
        f"Текущая дата и время: {today}\n\n"
        f"Вопрос пользователя:\n{query}\n\n"
        f"Найденные онлайн-источники:\n{sources_block}\n\n"
        "Сформируй краткий, практичный и проверяемый ответ."
    )

    data: Dict[str, Any] = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": MAX_RESPONSE_TOKENS,
        "temperature": 0.2,
    }
    if DEEPSEEK_THINKING:
        data["thinking"] = {"type": "enabled"}
        data["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT

    response = requests.post(
        DEEPSEEK_API_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json=data,
        timeout=120,
    )
    response.raise_for_status()
    answer = response.json()["choices"][0]["message"]["content"]

    add_to_conversation(user_id, "user", f"[Онлайн-поиск] {query}")
    add_to_conversation(user_id, "assistant", answer)

    source_list = _format_sources_for_user(results)
    return f"{answer}\n\n🔎 Проверенные источники:\n{source_list}"


def answer_with_openai_web_search(query: str, user_id: str) -> str:
    """Запасной вариант: OpenAI Responses API со встроенным web_search."""
    if not _OPENAI_CLIENT:
        return "Не настроен OPENAI_API_KEY для онлайн-поиска."

    today = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M %Z")
    prompt = (
        "Ты – ИИ Настенька, ассистент компании ООО «НТЦ Митра». "
        "Ответь на русском с учетом актуального онлайн-поиска. "
        "По возможности укажи источники/ссылки.\n\n"
        f"Текущая дата и время: {today}\n"
        f"Запрос: {query}"
    )

    try:
        response = _OPENAI_CLIENT.responses.create(
            model=ONLINE_SEARCH_MODEL,
            tools=[{"type": "web_search"}],
            input=prompt,
        )
    except Exception as first_error:
        logger.warning(f"OpenAI web_search failed, trying legacy web_search_preview: {first_error}")
        response = _OPENAI_CLIENT.responses.create(
            model=ONLINE_SEARCH_MODEL,
            tools=[{"type": "web_search_preview"}],
            input=prompt,
        )

    answer = getattr(response, "output_text", None)
    if not answer:
        answer = str(response)

    add_to_conversation(user_id, "user", f"[Онлайн-поиск OpenAI] {query}")
    add_to_conversation(user_id, "assistant", answer)
    return answer


def answer_online(query: str, user_id: str) -> str:
    """Единая точка входа для онлайн-ответа."""
    query = _clean_query(query)
    try:
        if TAVILY_API_KEY:
            return answer_with_deepseek_and_sources(query, user_id)
        if OPENAI_API_KEY:
            return answer_with_openai_web_search(query, user_id)
        return (
            "Онлайн-поиск пока не настроен. Добавьте в Railway Variables либо TAVILY_API_KEY "
            "для режима Tavily + DeepSeek, либо OPENAI_API_KEY для OpenAI web search."
        )
    except Exception as e:
        logger.exception(f"Online search error: {e}")
        return "Не удалось выполнить онлайн-поиск. Проверьте ключи API и логи Railway."


def split_telegram_text(text: str, limit: int = 3900) -> List[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < 1000:
            cut = limit
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


async def search_command(update, context) -> None:
    """Команда /search запрос — принудительный онлайн-поиск."""
    if is_group_chat(update) and not await is_addressed_to_bot(update, context):
        return

    raw_text = update.message.text or ""
    query = _clean_query(raw_text)
    if not query or query.lower() in {"/search", "поиск"}:
        await update.message.reply_text("Напишите так: /search что нужно найти в интернете")
        return

    if is_group_chat(update):
        query = await clean_group_trigger_text(update, context, query) or query

    await update.message.reply_chat_action(action="typing")
    answer = await asyncio.to_thread(answer_online, query, str(update.effective_chat.id))
    for part in split_telegram_text(answer):
        await update.message.reply_text(part)
