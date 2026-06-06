import re
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from config import GROUP_AGREEMENTS_LIMIT, GROUP_TRIGGER_WORDS, logger
from database import create_group_agreement, format_db_datetime, get_group_agreements

GROUP_CHAT_TYPES = {"group", "supergroup"}


def get_dialog_key(update: Update) -> str:
    """Возвращает безопасный ключ истории диалога.

    В личке история хранится по пользователю.
    В группе история хранится отдельно для каждого пользователя внутри каждого чата,
    чтобы ответы одному сотруднику не попадали в контекст ответа другому сотруднику.
    """
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id if chat else 0
    user_id = user.id if user else 0
    if chat and chat.type in GROUP_CHAT_TYPES:
        return f"group:{chat_id}:user:{user_id}"
    return f"private:{user_id or chat_id}"


def is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in GROUP_CHAT_TYPES)


def _message_text(update: Update) -> str:
    msg = update.effective_message
    if not msg:
        return ""
    return msg.text or msg.caption or ""


async def _get_bot_user(context: ContextTypes.DEFAULT_TYPE):
    bot_user = context.bot_data.get("bot_user")
    if bot_user:
        return bot_user
    bot_user = await context.bot.get_me()
    context.bot_data["bot_user"] = bot_user
    return bot_user


async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    bot_user = await _get_bot_user(context)
    return (bot_user.username or "").lower()


async def is_addressed_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True, если сообщение нужно обрабатывать.

    В личных чатах бот работает как раньше. В группах — только если:
    - сообщение содержит @username_бота;
    - пользователь ответил на сообщение бота;
    - сообщение начинается с дополнительного триггера из GROUP_TRIGGER_WORDS.
    """
    if not is_group_chat(update):
        return True

    msg = update.effective_message
    if not msg:
        return False

    bot_user = await _get_bot_user(context)

    replied = msg.reply_to_message
    if replied and replied.from_user and replied.from_user.id == bot_user.id:
        return True

    raw_text = _message_text(update)
    text = raw_text.lower()
    bot_username = (bot_user.username or "").lower()

    # Надёжно проверяем @упоминание через entities. Это важнее простого поиска по строке,
    # потому что Telegram может прислать entity mention/text_mention.
    entities = list(getattr(msg, "entities", None) or []) + list(getattr(msg, "caption_entities", None) or [])
    for entity in entities:
        if entity.type == "mention":
            mentioned = raw_text[entity.offset: entity.offset + entity.length].lower()
            if bot_username and mentioned == f"@{bot_username}":
                return True
        elif entity.type == "text_mention" and getattr(entity, "user", None):
            if entity.user.id == bot_user.id:
                return True

    # Запасной вариант: обычный поиск по тексту/подписи.
    if bot_username and re.search(rf"(?<!\w)@{re.escape(bot_username)}(?!\w)", raw_text, flags=re.IGNORECASE):
        return True

    # Команда вида /ask@bot_username тоже считается обращением.
    if bot_username and re.match(rf"^/\w+@{re.escape(bot_username)}\b", raw_text, flags=re.IGNORECASE):
        return True

    for trigger in GROUP_TRIGGER_WORDS:
        if not trigger:
            continue
        if text.startswith(trigger) or f" {trigger}" in text:
            return True

    return False


async def clean_group_trigger_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: Optional[str] = None) -> str:
    text = text if text is not None else _message_text(update)
    if not text:
        return ""

    bot_username = await get_bot_username(context)
    if bot_username:
        text = re.sub(rf"@{re.escape(bot_username)}\b", "", text, flags=re.IGNORECASE)

    for trigger in GROUP_TRIGGER_WORDS:
        if not trigger:
            continue
        text = re.sub(rf"(^|\s){re.escape(trigger)}\b", " ", text, flags=re.IGNORECASE)

    return text.strip(" \n\t,.:;—-")


def _is_agreement_save_request(text: str) -> bool:
    lowered = (text or "").lower()
    save_words = ("запиши", "зафиксируй", "сохрани", "запомни", "добавь")
    agreement_words = ("договор", "договорён", "договорен", "решили", "решение", "итог встречи", "протокол")
    if any(w in lowered for w in save_words) and any(w in lowered for w in agreement_words):
        return True
    if lowered.startswith(("договоренность:", "договорённость:", "решение:", "итог:")):
        return True
    return False


def _is_agreement_list_request(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        phrase in lowered
        for phrase in (
            "какие договор", "покажи договор", "список договор", "последние договор",
            "что записано", "что зафиксировано", "покажи решения", "список решений",
            "договоренности", "договорённости",
        )
    ) and any(w in lowered for w in ("покажи", "какие", "список", "последние", "что"))


def _extract_agreement_text(text: str) -> str:
    result = text.strip()
    patterns = [
        r"^(запиши|зафиксируй|сохрани|запомни|добавь)\s+(договор[её]нность|решение|итог встречи|протокол)\s*[:\-—]?\s*",
        r"^(договоренность|договорённость|решение|итог)\s*[:\-—]\s*",
        r"^что\s+",
    ]
    for pattern in patterns:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE).strip()
    return result or text.strip()


async def send_group_agreements(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return

    agreements = get_group_agreements(chat.id, GROUP_AGREEMENTS_LIMIT)
    if not agreements:
        await update.effective_message.reply_text("Пока по этому чату договорённости не записаны.")
        return

    lines = ["📌 Последние договорённости по этому чату:"]
    for item in reversed(agreements):
        author = item.get("author_name") or item.get("username") or "участник"
        when = format_db_datetime(item.get("created_at"), empty="")
        lines.append(f"#{item['id']} · {when} · {author}: {item['text']}")

    await update.effective_message.reply_text("\n".join(lines))


async def agreements_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_group_agreements(update, context)


async def save_agreement_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args or []).strip()
    if not text:
        await update.effective_message.reply_text(
            "Напишите так: /save_agreement Иван подготовит акт до пятницы"
        )
        return
    await save_group_agreement(update, context, text)


async def save_group_agreement(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    author_name = " ".join(part for part in [user.first_name if user else None, user.last_name if user else None] if part)
    agreement_id = create_group_agreement(
        chat_id=chat.id if chat else 0,
        chat_title=chat.title if chat else None,
        message_id=msg.message_id if msg else None,
        user_id=user.id if user else None,
        username=user.username if user else None,
        author_name=author_name or (user.username if user else None),
        text=text,
    )
    logger.info("Создана договоренность #%s в чате %s", agreement_id, chat.id if chat else None)
    await msg.reply_text(f"✅ Зафиксировала договорённость #{agreement_id}: {text}")
    return agreement_id


async def maybe_handle_group_agreement(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not text:
        return False
    if _is_agreement_list_request(text):
        await send_group_agreements(update, context)
        return True
    if _is_agreement_save_request(text):
        await save_group_agreement(update, context, _extract_agreement_text(text))
        return True
    return False
