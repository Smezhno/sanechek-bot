"""Smart mention handler - creates tasks via @bot <text>."""
import json
import logging
import re
from datetime import datetime
from typing import TypedDict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from database import get_session, Task, User, Chat, ChatMember
from database.models import TaskStatus, RecurrenceType
from llm.client import ask_llm
from config import settings
from utils.date_parser import parse_deadline, DateParseError
from utils.formatters import format_date
from utils.cache import get_chat_members_cached, find_member_by_username, find_members_by_name
from utils.permissions import get_or_create_user

logger = logging.getLogger(__name__)

# Constants
CONFIDENCE_THRESHOLD = 0.7
PENDING_HASH_MODULO = 10000
BUTTON_TEXT_MAX = 25

# Message constants
MSG_TASK_CREATED = "✅ Задача создана!"
MSG_TASK_NO_TEXT = "Не понял, какую задачу создать. Напиши текст после упоминания."
MSG_EXPIRED = "⏰ Предложение устарело"
MSG_USER_NOT_FOUND = "Не нашёл пользователя {name} в этом чате"
MSG_MULTIPLE_MATCHES = "Найдено несколько совпадений. Выбери исполнителя:"
MSG_ASK_DEADLINE = "⏰ Когда дедлайн?\nНапиши сообщением (например: завтра, через 3 дня)"
MSG_NO_API_KEY = "❌ LLM не настроен. Используй /task для создания задачи."


class ParsedMention(TypedDict, total=False):
    """Parsed mention data from LLM."""
    task: str
    assignee: Optional[str]
    deadline: Optional[str]
    recurrence: str
    confidence: float


class PendingTaskData(TypedDict, total=False):
    """Data for pending task creation."""
    text: str
    assignee_id: Optional[int]
    assignee_name: Optional[str]
    deadline: Optional[datetime]
    recurrence: RecurrenceType
    chat_id: int
    author_id: int
    is_dm: bool


# LLM Prompt for parsing mentions
PARSE_MENTION_SYSTEM_PROMPT = """Ты парсер задач. Извлекай компоненты из текста. Отвечай ТОЛЬКО JSON."""

PARSE_MENTION_PROMPT = """Разбери текст и извлеки компоненты задачи.

Текст: "{text}"
Участники чата: {members}
Контекст: {context}

Извлеки:
1. task - краткая формулировка задачи (2-7 слов)
2. assignee - @username, имя человека, "я" (если автор себе), или null
3. deadline - дата/время в исходной форме (например "завтра", "через 3 дня") или null
4. recurrence - тип повтора: none, daily, weekdays, weekly, weekly_monday, weekly_tuesday, weekly_wednesday, weekly_thursday, weekly_friday, weekly_saturday, weekly_sunday, monthly
5. confidence - уверенность в исполнителе от 0.0 до 1.0

Признаки самоназначения (confidence >= 0.7):
- "мне надо", "я должен", "я куплю", "я сделаю"
- глаголы первого лица

Признаки низкой уверенности (confidence < 0.7):
- "надо бы", "нужно" без указания кому
- общие обсуждения

Ответ ТОЛЬКО в формате JSON:
{{"task": "...", "assignee": "...", "deadline": "...", "recurrence": "none", "confidence": 0.0}}"""


# Recurrence patterns for detection
RECURRENCE_KEYWORDS = {
    RecurrenceType.DAILY: ["каждый день", "ежедневно"],
    RecurrenceType.WEEKDAYS: ["по будням", "в рабочие дни", "пн-пт"],
    RecurrenceType.WEEKLY: ["каждую неделю", "еженедельно"],
    RecurrenceType.WEEKLY_MONDAY: ["каждый понедельник", "по понедельникам"],
    RecurrenceType.WEEKLY_TUESDAY: ["каждый вторник", "по вторникам"],
    RecurrenceType.WEEKLY_WEDNESDAY: ["каждую среду", "по средам"],
    RecurrenceType.WEEKLY_THURSDAY: ["каждый четверг", "по четвергам"],
    RecurrenceType.WEEKLY_FRIDAY: ["каждую пятницу", "по пятницам"],
    RecurrenceType.WEEKLY_SATURDAY: ["каждую субботу", "по субботам"],
    RecurrenceType.WEEKLY_SUNDAY: ["каждое воскресенье", "по воскресеньям"],
    RecurrenceType.MONTHLY: ["каждый месяц", "ежемесячно"],
}


def _compute_hash(text: str) -> str:
    """Compute hash for storing pending data."""
    return str(abs(hash(text)) % PENDING_HASH_MODULO)


def _get_pending_key(task_hash: str) -> str:
    """Get bot_data key for pending task."""
    return f"mention_pending_{task_hash}"


def _store_pending_data(
    context: ContextTypes.DEFAULT_TYPE,
    task_hash: str,
    data: PendingTaskData
) -> None:
    """Store pending task data."""
    context.bot_data[_get_pending_key(task_hash)] = data


def _get_pending_data(
    context: ContextTypes.DEFAULT_TYPE,
    task_hash: str
) -> Optional[PendingTaskData]:
    """Get pending task data."""
    return context.bot_data.get(_get_pending_key(task_hash))


def _delete_pending_data(context: ContextTypes.DEFAULT_TYPE, task_hash: str) -> None:
    """Delete pending task data."""
    key = _get_pending_key(task_hash)
    context.bot_data.pop(key, None)


def _has_api_key() -> bool:
    """Check if LLM API is configured."""
    return bool(settings.openai_api_key or settings.yandex_gpt_api_key)


def _extract_mention_text(text: str) -> str:
    """Extract text after @bot mention, excluding the mention itself."""
    pattern = rf"@{settings.bot_username}\s*"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return text[match.end():].strip()
    return text.strip()


def _parse_recurrence_from_text(text: str) -> tuple[RecurrenceType, str]:
    """
    Extract recurrence type from text and return cleaned text.

    Returns tuple of (recurrence_type, cleaned_text).
    """
    text_lower = text.lower()

    for recurrence_type, keywords in RECURRENCE_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text_lower:
                # Remove keyword from text
                pattern = re.compile(re.escape(keyword), re.IGNORECASE)
                cleaned = pattern.sub("", text).strip()
                cleaned = " ".join(cleaned.split())  # normalize spaces
                return recurrence_type, cleaned

    return RecurrenceType.NONE, text


async def _parse_mention_with_llm(
    text: str,
    members_str: str,
    context_type: str
) -> ParsedMention:
    """Parse mention text using LLM."""
    try:
        prompt = PARSE_MENTION_PROMPT.format(
            text=text,
            members=members_str,
            context=context_type
        )

        response = await ask_llm(
            question=prompt,
            system_prompt=PARSE_MENTION_SYSTEM_PROMPT,
            max_tokens=200,
            temperature=0.2
        )

        # Extract JSON from response
        json_match = re.search(r'\{[^}]+\}', response)
        if json_match:
            data = json.loads(json_match.group())
            return ParsedMention(
                task=data.get("task", text),
                assignee=data.get("assignee"),
                deadline=data.get("deadline"),
                recurrence=data.get("recurrence", "none"),
                confidence=float(data.get("confidence", 0.5))
            )
    except Exception as e:
        logger.warning("LLM parsing failed: %s", e)

    # Fallback: use original text as task
    return ParsedMention(
        task=text,
        assignee=None,
        deadline=None,
        recurrence="none",
        confidence=0.5
    )


def _parse_mention_fallback(text: str) -> ParsedMention:
    """Fallback parsing without LLM."""
    # First, extract text after @bot mention
    task_text = _extract_mention_text(text)

    # Extract @username mentions (from the cleaned text)
    assignee = None
    username_match = re.search(r"@(\w+)", task_text)
    if username_match:
        username = username_match.group(1)
        if username.lower() != settings.bot_username.lower():
            assignee = f"@{username}"
            # Remove the @username from task text
            task_text = re.sub(rf"@{username}\s*", "", task_text, flags=re.IGNORECASE).strip()

    # Check for self-assignment patterns
    confidence = 0.5
    self_patterns = [
        r"\bмне\s+надо\b", r"\bя\s+должен\b", r"\bя\s+куплю\b",
        r"\bя\s+сделаю\b", r"\bмне\s+нужно\b"
    ]
    for pattern in self_patterns:
        if re.search(pattern, task_text, re.IGNORECASE):
            assignee = "я"
            confidence = 0.8
            break

    # Extract recurrence
    recurrence, clean_text = _parse_recurrence_from_text(task_text)

    return ParsedMention(
        task=clean_text if clean_text else task_text,
        assignee=assignee,
        deadline=None,
        recurrence=recurrence.value,
        confidence=confidence
    )


async def _resolve_assignee_dm(
    parsed: ParsedMention,
    author_id: int,
    session
) -> tuple[Optional[int], Optional[str], bool]:
    """
    Resolve assignee for DM context.

    Returns (assignee_id, assignee_name, needs_buttons).
    """
    assignee = parsed.get("assignee")

    # Explicit @username
    if assignee and assignee.startswith("@"):
        result = await session.execute(
            select(User).where(User.username.ilike(assignee[1:]))
        )
        user = result.scalar_one_or_none()
        if user:
            return user.id, user.display_name, False
        return None, assignee, False

    # Default to author in DM
    return author_id, None, False


async def _resolve_assignee_group(
    parsed: ParsedMention,
    author_id: int,
    chat_id: int,
    session
) -> tuple[Optional[int], Optional[str], bool]:
    """
    Resolve assignee for group chat context.

    Returns (assignee_id, assignee_name, needs_buttons).
    """
    assignee = parsed.get("assignee")
    confidence = parsed.get("confidence", 0.5)

    # 1. Explicit @username
    if assignee and assignee.startswith("@"):
        member = await find_member_by_username(chat_id, assignee[1:], session)
        if member:
            return member.user_id, member.display_name, False
        # Not found in chat
        return None, assignee, False

    # 2. Name lookup
    if assignee and assignee not in ["я", None]:
        matches = await find_members_by_name(chat_id, assignee, session)
        if len(matches) == 1:
            return matches[0].user_id, matches[0].display_name, False
        if len(matches) > 1:
            # Multiple matches - need buttons
            return None, None, True
        # No matches found
        return None, assignee, False

    # 3. Self-assignment with high confidence
    if assignee == "я" and confidence >= CONFIDENCE_THRESHOLD:
        return author_id, None, False

    # 4. Low confidence - need buttons
    if assignee == "я" and confidence < CONFIDENCE_THRESHOLD:
        return None, None, True

    # 5. No assignee specified - create without assignee
    return None, None, False


def _build_assignee_buttons(
    task_hash: str,
    author_id: int,
    author_name: str,
    matches: list = None
) -> InlineKeyboardMarkup:
    """Build assignee selection buttons."""
    buttons = [
        [InlineKeyboardButton(
            f"👤 Мне ({author_name})",
            callback_data=f"mention:assignee:{author_id}:{task_hash}"
        )]
    ]

    if matches:
        for member in matches[:4]:
            buttons.append([
                InlineKeyboardButton(
                    f"👤 {member.display_name}",
                    callback_data=f"mention:assignee:{member.user_id}:{task_hash}"
                )
            ])

    buttons.append([
        InlineKeyboardButton(
            "🚫 Без исполнителя",
            callback_data=f"mention:no_assignee:{task_hash}"
        )
    ])

    return InlineKeyboardMarkup(buttons)


def _build_deadline_buttons(task_hash: str) -> InlineKeyboardMarkup:
    """Build deadline selection buttons."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📅 Без дедлайна",
                callback_data=f"mention:no_deadline:{task_hash}"
            )
        ]
    ])


def _recurrence_from_string(value: str) -> RecurrenceType:
    """Convert string to RecurrenceType."""
    try:
        return RecurrenceType(value)
    except ValueError:
        return RecurrenceType.NONE


async def _create_task(
    session,
    data: PendingTaskData,
) -> Task:
    """Create task from pending data."""
    task = Task(
        chat_id=data["chat_id"],
        author_id=data["author_id"],
        assignee_id=data.get("assignee_id"),
        text=data["text"],
        deadline=data.get("deadline"),
        recurrence=data.get("recurrence", RecurrenceType.NONE),
        status=TaskStatus.OPEN
    )
    session.add(task)
    await session.flush()
    return task


def _format_task_confirmation(
    text: str,
    assignee_name: Optional[str],
    deadline: Optional[datetime],
    recurrence: RecurrenceType
) -> str:
    """Format task creation confirmation message."""
    lines = [MSG_TASK_CREATED, "", f"📌 {text}"]

    if assignee_name:
        lines.append(f"👤 {assignee_name}")
    else:
        lines.append("👤 Без исполнителя")

    if deadline:
        lines.append(f"📅 {format_date(deadline, include_time=True)}")
    else:
        lines.append("📅 Без дедлайна")

    if recurrence != RecurrenceType.NONE:
        recurrence_labels = {
            RecurrenceType.DAILY: "Ежедневно",
            RecurrenceType.WEEKDAYS: "По будням",
            RecurrenceType.WEEKLY: "Еженедельно",
            RecurrenceType.WEEKLY_MONDAY: "По понедельникам",
            RecurrenceType.WEEKLY_TUESDAY: "По вторникам",
            RecurrenceType.WEEKLY_WEDNESDAY: "По средам",
            RecurrenceType.WEEKLY_THURSDAY: "По четвергам",
            RecurrenceType.WEEKLY_FRIDAY: "По пятницам",
            RecurrenceType.WEEKLY_SATURDAY: "По субботам",
            RecurrenceType.WEEKLY_SUNDAY: "По воскресеньям",
            RecurrenceType.MONTHLY: "Ежемесячно",
        }
        lines.append(f"🔁 {recurrence_labels.get(recurrence, str(recurrence))}")

    return "\n".join(lines)


# ========== Main Handlers ==========

async def mention_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle @bot mentions for smart task creation."""
    if not update.message or not update.message.text:
        return

    message = update.message
    text = message.text
    user = update.effective_user
    chat = update.effective_chat

    # Extract text after mention
    mention_text = _extract_mention_text(text)
    if not mention_text:
        await message.reply_text(MSG_TASK_NO_TEXT)
        return

    is_dm = chat.type == "private"

    async with get_session() as session:
        # Ensure user and chat exist
        await get_or_create_user(
            session, user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )

        if not is_dm:
            result = await session.execute(select(Chat).where(Chat.id == chat.id))
            db_chat = result.scalar_one_or_none()
            if not db_chat:
                db_chat = Chat(id=chat.id, title=chat.title, is_active=True)
                session.add(db_chat)

        # Get chat members for context
        members_str = "личное сообщение"
        if not is_dm:
            members = await get_chat_members_cached(chat.id, session)
            members_str = ", ".join([m.display_name for m in members[:10]])

        # Parse mention with LLM or fallback
        if _has_api_key():
            parsed = await _parse_mention_with_llm(
                mention_text,
                members_str,
                "dm" if is_dm else "group"
            )
        else:
            parsed = _parse_mention_fallback(mention_text)

        # Get task text
        task_text = parsed.get("task", mention_text)
        if not task_text:
            task_text = mention_text

        # Parse recurrence
        recurrence = _recurrence_from_string(parsed.get("recurrence", "none"))

        # Parse deadline if provided
        deadline = None
        deadline_str = parsed.get("deadline")
        if deadline_str:
            try:
                deadline = parse_deadline(deadline_str)
            except DateParseError:
                pass

        # Resolve assignee
        if is_dm:
            assignee_id, assignee_name, needs_buttons = await _resolve_assignee_dm(
                parsed, user.id, session
            )
        else:
            assignee_id, assignee_name, needs_buttons = await _resolve_assignee_group(
                parsed, user.id, chat.id, session
            )

        # If assignee_id resolved, get their name
        if assignee_id and not assignee_name:
            result = await session.execute(select(User).where(User.id == assignee_id))
            assignee_user = result.scalar_one_or_none()
            if assignee_user:
                assignee_name = assignee_user.display_name

        # Generate hash for pending data
        task_hash = _compute_hash(f"{chat.id}:{user.id}:{task_text}")

        # Store pending data
        pending_data: PendingTaskData = {
            "text": task_text,
            "assignee_id": assignee_id,
            "assignee_name": assignee_name,
            "deadline": deadline,
            "recurrence": recurrence,
            "chat_id": chat.id,
            "author_id": user.id,
            "is_dm": is_dm,
        }
        _store_pending_data(context, task_hash, pending_data)

        # Need to choose assignee?
        if needs_buttons:
            matches = []
            assignee_text = parsed.get("assignee")
            if assignee_text and assignee_text not in ["я", None]:
                matches = await find_members_by_name(chat.id, assignee_text, session)

            author_name = user.first_name or user.username or "Мне"
            keyboard = _build_assignee_buttons(task_hash, user.id, author_name, matches)

            await message.reply_text(
                f"📌 {task_text}\n\n👤 Кто исполнитель?",
                reply_markup=keyboard
            )
            return

        # Need deadline and don't have one?
        if deadline is None:
            keyboard = _build_deadline_buttons(task_hash)
            display_assignee = assignee_name or "Без исполнителя"

            await message.reply_text(
                f"📌 {task_text}\n"
                f"👤 {display_assignee}\n\n"
                f"{MSG_ASK_DEADLINE}",
                reply_markup=keyboard
            )
            context.user_data["mention_waiting_deadline"] = task_hash
            return

        # Have everything - create task immediately
        task = await _create_task(session, pending_data)

        confirmation = _format_task_confirmation(
            task_text, assignee_name, deadline, recurrence
        )
        await message.reply_text(confirmation)
        _delete_pending_data(context, task_hash)


async def mention_callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle mention-related callback queries."""
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    action = data[1]
    task_hash = data[-1]

    pending = _get_pending_data(context, task_hash)
    if not pending:
        await query.edit_message_text(MSG_EXPIRED)
        return

    if action == "assignee":
        assignee_id = int(data[2])

        async with get_session() as session:
            result = await session.execute(select(User).where(User.id == assignee_id))
            assignee_user = result.scalar_one_or_none()

            if assignee_user:
                pending["assignee_id"] = assignee_user.id
                pending["assignee_name"] = assignee_user.display_name
            else:
                pending["assignee_id"] = assignee_id
                pending["assignee_name"] = None

        _store_pending_data(context, task_hash, pending)

        # Ask for deadline
        display_assignee = pending.get("assignee_name") or "Выбран"
        keyboard = _build_deadline_buttons(task_hash)

        await query.edit_message_text(
            f"📌 {pending['text']}\n"
            f"👤 {display_assignee}\n\n"
            f"{MSG_ASK_DEADLINE}",
            reply_markup=keyboard
        )
        context.user_data["mention_waiting_deadline"] = task_hash
        return

    if action == "no_assignee":
        pending["assignee_id"] = None
        pending["assignee_name"] = None
        _store_pending_data(context, task_hash, pending)

        keyboard = _build_deadline_buttons(task_hash)

        await query.edit_message_text(
            f"📌 {pending['text']}\n"
            f"👤 Без исполнителя\n\n"
            f"{MSG_ASK_DEADLINE}",
            reply_markup=keyboard
        )
        context.user_data["mention_waiting_deadline"] = task_hash
        return

    if action == "no_deadline":
        # Create task without deadline
        async with get_session() as session:
            pending["deadline"] = None
            task = await _create_task(session, pending)

            confirmation = _format_task_confirmation(
                pending["text"],
                pending.get("assignee_name"),
                None,
                pending.get("recurrence", RecurrenceType.NONE)
            )
            await query.edit_message_text(confirmation)

        _delete_pending_data(context, task_hash)
        context.user_data.pop("mention_waiting_deadline", None)
        return


async def mention_deadline_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle deadline text input for mention-created tasks."""
    if not update.message or not update.message.text:
        return

    task_hash = context.user_data.get("mention_waiting_deadline")
    if not task_hash:
        return

    pending = _get_pending_data(context, task_hash)
    if not pending:
        context.user_data.pop("mention_waiting_deadline", None)
        return

    text = update.message.text.strip()

    try:
        deadline = parse_deadline(text)
    except DateParseError as e:
        await update.message.reply_text(
            f"❌ Не понял дату: {e}\n\n"
            f"Попробуй ещё раз (например: завтра, через 3 дня, в пятницу)",
            reply_markup=_build_deadline_buttons(task_hash)
        )
        return

    # Create task with deadline
    async with get_session() as session:
        pending["deadline"] = deadline
        task = await _create_task(session, pending)

        confirmation = _format_task_confirmation(
            pending["text"],
            pending.get("assignee_name"),
            deadline,
            pending.get("recurrence", RecurrenceType.NONE)
        )
        await update.message.reply_text(confirmation)

    _delete_pending_data(context, task_hash)
    context.user_data.pop("mention_waiting_deadline", None)
