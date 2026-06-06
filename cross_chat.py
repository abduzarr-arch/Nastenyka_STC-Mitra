import re
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS, logger
from database import (
    get_all_group_chats,
    get_group_chat_by_alias,
    list_chat_members,
    register_user,
    upsert_chat_member,
    upsert_employee_alias,
    upsert_group_chat,
)
from group_utils import is_group_chat


def _is_admin_user(user_id: Optional[int]) -> bool:
    """Если ADMIN_IDS заполнен — отправлять в рабочие чаты могут только эти пользователи."""
    if not ADMIN_IDS:
        return True
    return bool(user_id and user_id in ADMIN_IDS)


def _clean_alias(alias: str) -> str:
    alias = (alias or "").strip().lower()
    alias = alias.replace("@", "")
    alias = re.sub(r"\s+", "_", alias)
    alias = re.sub(r"[^0-9a-zа-яё_\-]+", "", alias, flags=re.IGNORECASE)
    return alias.strip("_-")


def _default_alias_from_title(title: Optional[str], chat_id: int) -> str:
    base = _clean_alias(title or "рабочий_чат")
    return base or f"chat_{abs(chat_id)}"


def _normalize_word_for_compare(text: str) -> str:
    return _clean_alias(text).replace("_", " ")


def _format_chat_line(chat: dict) -> str:
    alias = chat.get("alias") or "—"
    title = chat.get("title") or "без названия"
    return f"• {alias} — {title}"


def _display_name_from_user(user) -> str:
    if not user:
        return ""
    return " ".join(part for part in [user.first_name, user.last_name] if part).strip() or user.username or str(user.id)


def remember_chat_member_from_user(chat_id: int, user, status: str = "member") -> bool:
    if not chat_id or not user or getattr(user, "is_bot", False):
        return False
    display_name = _display_name_from_user(user)
    register_user(user.id, user.username or "", user.first_name or "", user.last_name or "")
    upsert_chat_member(
        chat_id=chat_id,
        user_id=user.id,
        username=user.username or None,
        first_name=user.first_name or None,
        last_name=user.last_name or None,
        display_name=display_name,
        status=status,
    )
    if user.username:
        upsert_employee_alias(alias=user.username, username=user.username, display_name=display_name, user_id=user.id)
    if user.first_name:
        upsert_employee_alias(alias=user.first_name, username=user.username, display_name=display_name, user_id=user.id)
    if display_name and display_name != user.first_name:
        upsert_employee_alias(alias=display_name, username=user.username, display_name=display_name, user_id=user.id)
    return True


def remember_current_group_chat(update: Update, alias: Optional[str] = None) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not is_group_chat(update):
        return False
    chat_alias = _clean_alias(alias) if alias else _default_alias_from_title(chat.title, chat.id)
    if not alias:
        existing = get_group_chat_by_alias(chat_alias)
        if existing and int(existing.get("chat_id") or 0) != int(chat.id):
            chat_alias = f"{chat_alias}_{abs(chat.id)}"
    upsert_group_chat(
        chat_id=chat.id,
        title=chat.title or "Рабочий чат",
        chat_type=chat.type,
        alias=chat_alias,
        registered_by=user.id if user else None,
    )
    if user:
        remember_chat_member_from_user(chat.id, user)
    return True


async def remember_visible_chat_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not is_group_chat(update):
        return 0
    count = 0
    users = []
    if update.effective_user:
        users.append(update.effective_user)
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        users.append(msg.reply_to_message.from_user)
    if msg:
        for user in getattr(msg, "new_chat_members", None) or []:
            users.append(user)
        if getattr(msg, "left_chat_member", None):
            upsert_chat_member(
                chat_id=chat.id,
                user_id=msg.left_chat_member.id,
                username=msg.left_chat_member.username or None,
                first_name=msg.left_chat_member.first_name or None,
                last_name=msg.left_chat_member.last_name or None,
                display_name=_display_name_from_user(msg.left_chat_member),
                status="left",
            )
    seen = set()
    for user in users:
        if not user or user.id in seen:
            continue
        seen.add(user.id)
        if remember_chat_member_from_user(chat.id, user):
            count += 1
    return count


async def remember_chat_administrators(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    if not chat or not is_group_chat(update):
        return 0
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
    except Exception as exc:
        logger.info("Не удалось получить администраторов чата %s: %s", chat.id, exc)
        return 0
    count = 0
    for member in admins:
        user = getattr(member, "user", None)
        if remember_chat_member_from_user(chat.id, user, status=getattr(member, "status", "administrator")):
            count += 1
    return count


async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    member_update = update.chat_member or update.my_chat_member
    if not chat or not is_group_chat(update) or not member_update:
        return
    remember_current_group_chat(update)
    user = getattr(member_update.new_chat_member, "user", None)
    status = getattr(member_update.new_chat_member, "status", "member")
    remember_chat_member_from_user(chat.id, user, status=status)


def format_group_chats_for_prompt(limit: int = 30) -> str:
    chats = get_all_group_chats()
    if not chats:
        return ""
    lines = []
    for chat in chats[:limit]:
        lines.append(f"- {chat.get('alias')}: {chat.get('title') or 'без названия'} (chat_id={chat.get('chat_id')})")
    return "\n".join(lines)


def format_chat_members_for_prompt(limit: int = 80) -> str:
    members = list_chat_members(limit=limit)
    if not members:
        return ""
    lines = []
    for member in members:
        name = member.get("display_name") or member.get("first_name") or member.get("username") or member.get("user_id")
        username = f"@{member['username']}" if member.get("username") else "без username"
        chat = member.get("chat_alias") or member.get("chat_title") or member.get("chat_id")
        status = member.get("status") or "member"
        lines.append(f"- {name} ({username}), чат: {chat}, status={status}")
    return "\n".join(lines)


def _looks_like_chat_list_request(text: str) -> bool:
    lowered = (text or "").lower()
    return (
        ("чат" in lowered or "групп" in lowered)
        and any(phrase in lowered for phrase in (
            "в каких", "где ты", "список", "какие чаты", "чаты где", "чаты в которых", "куда можешь",
            "куда ты можешь", "куда отправить", "куда написать",
        ))
    )


def _looks_like_member_list_request(text: str) -> bool:
    lowered = (text or "").lower()
    return (
        any(word in lowered for word in ("участник", "сотрудник", "людей", "кого ты знаешь", "кого знаешь"))
        and any(word in lowered for word in ("список", "какие", "кто", "покажи", "знаешь"))
    )


async def maybe_handle_chat_registry_request(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not (_looks_like_chat_list_request(text) or _looks_like_member_list_request(text)):
        return False

    user = update.effective_user
    if not _is_admin_user(user.id if user else None):
        await update.effective_message.reply_text("Список рабочих чатов доступен только администраторам бота из ADMIN_IDS.")
        return True

    if is_group_chat(update):
        remember_current_group_chat(update)
        await remember_visible_chat_participants(update, context)

    if _looks_like_member_list_request(text):
        members = list_chat_members(chat_id=update.effective_chat.id if is_group_chat(update) else None, limit=80)
        if not members:
            await update.effective_message.reply_text(
                "Пока я не знаю участников. Я начну запоминать людей, когда увижу их сообщения или события добавления в рабочих чатах."
            )
            return True
        lines = []
        for member in members:
            name = member.get("display_name") or member.get("first_name") or member.get("username") or member.get("user_id")
            username = f"@{member['username']}" if member.get("username") else "без username"
            chat = member.get("chat_alias") or member.get("chat_title") or member.get("chat_id")
            lines.append(f"• {name} — {username} — {chat}")
        await update.effective_message.reply_text("Я знаю таких участников:\n" + "\n".join(lines))
        return True

    chats = get_all_group_chats()
    if not chats:
        await update.effective_message.reply_text(
            "Пока я не знаю ни одного рабочего чата. Добавьте меня в нужные группы и напишите там /bind_chat короткое_имя."
        )
        return True

    await update.effective_message.reply_text(
        "Я знаю такие рабочие чаты:\n"
        + "\n".join(_format_chat_line(c) for c in chats)
        + "\n\nДля межчатового поручения можно писать, например: «напиши в чат проект_лигoвский, что нужен статус по расчетам»."
    )
    return True


async def bind_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Привязать текущий групповой чат к короткому имени/алиасу."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not is_group_chat(update):
        await msg.reply_text("Эту команду нужно выполнить именно в рабочем групповом чате.")
        return

    if not _is_admin_user(user.id if user else None):
        await msg.reply_text("Привязать рабочий чат может только администратор бота из ADMIN_IDS.")
        return

    raw_alias = " ".join(context.args or []).strip()
    alias = _clean_alias(raw_alias) if raw_alias else _default_alias_from_title(chat.title, chat.id)
    if not alias:
        await msg.reply_text("Укажите короткое имя чата, например: /bind_chat рабочий")
        return

    remember_current_group_chat(update, alias=alias)
    await msg.reply_text(
        f"✅ Этот чат привязан как: {alias}\n"
        f"Теперь в личке можно писать, например:\n"
        f"/send_to_chat {alias} Вадим, нужно успеть сделать РПЗ по СОШ к 11 числу"
    )


async def chat_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    title = chat.title or "личный чат"
    await update.effective_message.reply_text(
        f"Chat ID: {chat.id}\n"
        f"Тип: {chat.type}\n"
        f"Название: {title}"
    )


async def group_chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_admin_user(user.id if user else None):
        await update.effective_message.reply_text("Список рабочих чатов доступен только администраторам бота.")
        return

    chats = get_all_group_chats()
    if not chats:
        await update.effective_message.reply_text(
            "Пока нет привязанных рабочих чатов.\n"
            "Добавьте бота в группу и выполните там: /bind_chat рабочий"
        )
        return
    await update.effective_message.reply_text("📌 Привязанные рабочие чаты:\n" + "\n".join(_format_chat_line(c) for c in chats))


def _extract_recipient_from_natural(text: str, alias: str) -> Optional[str]:
    # Пример: «Напиши Вадиму в рабочий чат, что ...» -> «Вадим»
    pattern = rf"^(?:напиши|отправь|сообщи|передай)\s+(.+?)\s+в\s+(?:чат\s+)?{re.escape(alias)}(?:\s+чат|\s+чате)?\b"
    m = re.search(pattern, text, flags=re.IGNORECASE)
    if not m:
        pattern = r"^(?:напиши|отправь|сообщи|передай)\s+(.+?)\s+в\s+.+?чат\b"
        m = re.search(pattern, text, flags=re.IGNORECASE)
    if not m:
        return None
    recipient = m.group(1).strip(" ,.:;—-")
    if not recipient:
        return None
    # Небольшое удобство для распространенного случая «Вадиму» -> «Вадим».
    if re.fullmatch(r"[А-Яа-яЁёA-Za-z]{3,30}", recipient) and recipient.lower().endswith("у"):
        recipient = recipient[:-1]
    return recipient


def _extract_message_from_natural(text: str, recipient: Optional[str] = None) -> str:
    original = text.strip()

    # Приоритет: всё после «что ...».
    m = re.search(r"\bчто\b\s*[:\-—,]?\s*(.+)$", original, flags=re.IGNORECASE | re.DOTALL)
    if m:
        body = m.group(1).strip()
        if recipient:
            return f"{recipient}, {body}"
        return body

    # Второй вариант: всё после двоеточия.
    if ":" in original:
        body = original.split(":", 1)[1].strip()
        if body:
            return body

    # Третий вариант: убираем вводные слова «отправь/напиши в чат ...».
    body = re.sub(r"^(?:напиши|отправь|сообщи|передай)\s+", "", original, flags=re.IGNORECASE).strip()
    body = re.sub(r"\bв\s+(?:чат\s+)?[\wа-яё_\- ]+(?:\s+чат|\s+чате)?\b\s*[,.:;—-]*", "", body, flags=re.IGNORECASE).strip()
    return body or original


def _find_target_chat_in_text(text: str) -> Tuple[Optional[dict], Optional[str]]:
    chats = get_all_group_chats()
    if not chats:
        return None, None

    lowered = text.lower()
    normalized = _normalize_word_for_compare(lowered)

    # Сначала ищем явное совпадение по alias или названию.
    candidates = []
    for chat in chats:
        alias = (chat.get("alias") or "").lower()
        title = (chat.get("title") or "").lower()
        alias_norm = _normalize_word_for_compare(alias)
        title_norm = _normalize_word_for_compare(title)
        score = 0
        if alias and re.search(rf"\b{re.escape(alias)}\b", lowered):
            score = max(score, len(alias) + 100)
        if alias_norm and alias_norm in normalized:
            score = max(score, len(alias_norm) + 80)
        if title_norm and title_norm in normalized:
            score = max(score, len(title_norm) + 60)
        if score:
            candidates.append((score, chat, alias or alias_norm))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1], candidates[0][2]

    # Если привязан только один чат и пользователь явно пишет «в чат» — используем его.
    if len(chats) == 1 and re.search(r"\bв\s+(?:рабочий\s+)?чат\b|\bв\s+групп", lowered):
        return chats[0], chats[0].get("alias")

    return None, None


def _looks_like_cross_chat_request(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered.startswith(("напиши", "отправь", "сообщи", "передай")):
        return False
    return "чат" in lowered or "групп" in lowered


async def send_to_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user

    if not _is_admin_user(user.id if user else None):
        await msg.reply_text("Отправлять сообщения в рабочие чаты могут только администраторы бота из ADMIN_IDS.")
        return

    if is_group_chat(update):
        await msg.reply_text("Эту команду лучше использовать в личке с ботом.")
        return

    if len(context.args or []) < 2:
        await msg.reply_text(
            "Использование:\n"
            "/send_to_chat рабочий Вадим, нужно успеть сделать РПЗ по СОШ к 11 числу"
        )
        return

    alias = _clean_alias(context.args[0])
    text = " ".join(context.args[1:]).strip()
    chat = get_group_chat_by_alias(alias)
    if not chat:
        await msg.reply_text(
            f"Чат с именем {alias} не найден.\n"
            "Проверьте список: /group_chats\n"
            "Или привяжите чат из группы: /bind_chat рабочий"
        )
        return

    try:
        await context.bot.send_message(chat_id=chat["chat_id"], text=text)
        await msg.reply_text(f"✅ Отправила в чат «{chat.get('title') or alias}»." )
    except Exception as exc:
        logger.error("Не удалось отправить сообщение в чат %s: %s", chat.get("chat_id"), exc)
        await msg.reply_text(
            "Не удалось отправить сообщение в этот чат. Проверьте, что бот всё ещё добавлен в группу "
            "и ему не запрещено отправлять сообщения."
        )


async def maybe_handle_private_group_send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Естественная команда из лички: «Напиши Вадиму в рабочий чат, что ...»."""
    if is_group_chat(update):
        return False
    if not _looks_like_cross_chat_request(text):
        return False

    msg = update.effective_message
    user = update.effective_user
    if not _is_admin_user(user.id if user else None):
        await msg.reply_text("Отправлять сообщения в рабочие чаты могут только администраторы бота из ADMIN_IDS.")
        return True

    chat, alias = _find_target_chat_in_text(text)
    if not chat:
        await msg.reply_text(
            "Я поняла, что нужно написать в рабочий чат, но не нашла привязанный чат.\n"
            "Сначала в нужной группе выполните: /bind_chat рабочий\n"
            "Потом из лички можно писать: Напиши Вадиму в рабочий чат, что ..."
        )
        return True

    recipient = _extract_recipient_from_natural(text, alias or "")
    outgoing_text = _extract_message_from_natural(text, recipient=recipient).strip()

    if not outgoing_text or len(outgoing_text) < 2:
        await msg.reply_text(
            "Я нашла чат, но не поняла текст сообщения. Надёжный формат:\n"
            f"/send_to_chat {chat.get('alias')} Вадим, нужно успеть сделать РПЗ по СОШ к 11 числу"
        )
        return True

    try:
        await context.bot.send_message(chat_id=chat["chat_id"], text=outgoing_text)
        await msg.reply_text(
            f"✅ Отправила в чат «{chat.get('title') or chat.get('alias')}»:\n\n{outgoing_text}"
        )
    except Exception as exc:
        logger.error("Не удалось отправить сообщение в чат %s: %s", chat.get("chat_id"), exc)
        await msg.reply_text(
            "Не удалось отправить сообщение. Проверьте, что бот добавлен в этот групповой чат "
            "и у него есть право отправлять сообщения."
        )
    return True
