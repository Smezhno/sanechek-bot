"""Task management handlers."""
import re
from datetime import datetime, timedelta
from typing import Optional
from dateutil.relativedelta import relativedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, 
    MessageHandler, CallbackQueryHandler, filters
)
from sqlalchemy import select, and_

from database import get_session, Task, User, Chat, ChatMember, TaskStatus
from handlers.base import States
from utils.date_parser import parse_deadline, DateParseError
from utils.formatters import format_task, format_task_short, format_date
from utils.permissions import (
    get_or_create_user, is_admin, can_close_task, can_edit_task,
    is_user_in_chat
)
from config import settings


async def task_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /task command - create a new task."""
    # Only works in groups
    if update.effective_chat.type == "private":
        await update.message.reply_text("Эта команда работает только в групповых чатах")
        return ConversationHandler.END
    
    user = update.effective_user
    chat = update.effective_chat
    args = " ".join(context.args) if context.args else ""
    
    async with get_session() as session:
        # Ensure chat and user exist
        result = await session.execute(select(Chat).where(Chat.id == chat.id))
        db_chat = result.scalar_one_or_none()
        if not db_chat:
            db_chat = Chat(id=chat.id, title=chat.title, is_active=True)
            session.add(db_chat)
        
        await get_or_create_user(
            session, user.id, 
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )
    
    # Store context for conversation
    context.user_data["in_conversation"] = True
    context.user_data["task_chat_id"] = chat.id
    context.user_data["task_author_id"] = user.id
    context.user_data["task_command_message_id"] = update.message.message_id
    
    if not args:
        # No arguments - ask for task text
        await update.message.reply_text("Что нужно сделать? Укажи ответным сообщением")
        return States.TASK_TEXT
    
    # Try to parse the full command
    parsed = await _parse_task_command(args, chat.id)
    
    if parsed["text"]:
        context.user_data["task_text"] = parsed["text"][:settings.max_task_length]
    else:
        await update.message.reply_text("Что нужно сделать? Укажи ответным сообщением")
        return States.TASK_TEXT
    
    if parsed["assignee_username"]:
        context.user_data["task_assignee_username"] = parsed["assignee_username"]
    else:
        await update.message.reply_text(
            "Кто исполнитель? Укажи ответным сообщением @username"
        )
        return States.TASK_ASSIGNEE
    
    if parsed["deadline"]:
        context.user_data["task_deadline"] = parsed["deadline"]
        return await _create_task(update, context)
    else:
        await update.message.reply_text(
            "Какой дедлайн? Укажи ответным сообщением (например: завтра, в пятницу, 15.02)"
        )
        return States.TASK_DEADLINE


async def _parse_task_command(text: str, chat_id: int) -> dict:
    """Parse task command arguments."""
    result = {
        "text": None,
        "assignee_username": None,
        "deadline": None,
        "deadline_text": None,
    }
    
    # Find @username
    username_match = re.search(r"@(\w+)", text)
    if username_match:
        result["assignee_username"] = username_match.group(1)
    
    # Try to find deadline at the end
    deadline_patterns = [
        r"(завтра|послезавтра|сегодня)",
        r"(в\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье))",
        r"((?:в\s+)?(?:пн|вт|ср|чт|пт|сб|вс))",
        r"(\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)",
        r"(\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))",
        r"(через\s+\d+\s+(?:дн|день|дней))",
    ]
    
    deadline_text = None
    for pattern in deadline_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            deadline_text = match.group(1)
            try:
                result["deadline"] = parse_deadline(deadline_text)
                result["deadline_text"] = deadline_text
            except DateParseError:
                pass
            break
    
    # Extract task text (everything except @username and deadline)
    task_text = text
    if result["assignee_username"]:
        task_text = task_text.replace(f"@{result['assignee_username']}", "").strip()
    if result["deadline_text"]:
        task_text = task_text.replace(result["deadline_text"], "").strip()
    
    # Clean up extra spaces
    task_text = " ".join(task_text.split())
    if task_text:
        result["text"] = task_text
    
    return result


async def receive_task_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive task text from user."""
    text = update.message.text.strip()
    
    if not text:
        await update.message.reply_text("Текст задачи не может быть пустым. Попробуй ещё раз:")
        return States.TASK_TEXT
    
    chat_id = context.user_data["task_chat_id"]
    
    # Try to parse task with LLM
    parsed = await _smart_parse_task(text, chat_id)
    
    context.user_data["task_text"] = parsed["task"][:settings.max_task_length]
    
    # Check if multiple candidates found
    if parsed.get("multiple_candidates") and len(parsed["multiple_candidates"]) > 1:
        candidates = parsed["multiple_candidates"]
        buttons = []
        for c in candidates[:5]:  # Max 5 options
            buttons.append([
                InlineKeyboardButton(
                    f"{c['name']} (@{c['username']})",
                    callback_data=f"task_assignee:{c['id']}:{c['username']}"
                )
            ])
        buttons.append([
            InlineKeyboardButton("❌ Другой", callback_data="task_assignee:other")
        ])
        
        await update.message.reply_text(
            f"🤔 Нашёл несколько подходящих людей. Кого имел в виду?",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return States.TASK_ASSIGNEE
    
    if parsed.get("assignee_id"):
        context.user_data["task_assignee_id"] = parsed["assignee_id"]
        context.user_data["task_assignee_username"] = parsed["assignee_username"]
        
        if parsed.get("deadline"):
            context.user_data["task_deadline"] = parsed["deadline"]
            return await _create_task(update, context)
        else:
            await update.message.reply_text(
                f"👤 Исполнитель: @{parsed['assignee_username']}\n\n"
                "Какой дедлайн? (например: завтра, в пятницу, 15.02)"
            )
            return States.TASK_DEADLINE
    
    await update.message.reply_text(
        "Кто исполнитель? Укажи ответным сообщением @username"
    )
    return States.TASK_ASSIGNEE


async def _smart_parse_task(text: str, chat_id: int) -> dict:
    """Parse task text using LLM to extract assignee and deadline."""
    from llm.client import ask_llm
    
    result = {
        "task": text,
        "assignee_id": None,
        "assignee_username": None,
        "assignee_name": None,
        "deadline": None,
    }
    
    # First check for @username in text
    username_match = re.search(r"@(\w+)", text)
    if username_match:
        username = username_match.group(1)
        async with get_session() as session:
            user_result = await session.execute(
                select(User).where(User.username == username)
            )
            user = user_result.scalar_one_or_none()
            if user:
                result["assignee_id"] = user.id
                result["assignee_username"] = username
                result["task"] = text.replace(f"@{username}", "").strip()
    
    # If no @username, try to extract name with LLM
    if not result["assignee_id"] and (settings.yandex_gpt_api_key or settings.openai_api_key):
        try:
            # Get chat members for context
            async with get_session() as session:
                members_result = await session.execute(
                    select(User).join(ChatMember).where(ChatMember.chat_id == chat_id)
                )
                members = members_result.scalars().all()
                
                if members:
                    members_list = ", ".join([
                        f"{m.first_name or ''} {m.last_name or ''} (@{m.username})" 
                        for m in members if m.username
                    ])
                    
                    prompt = f"""Из текста задачи определи исполнителя и саму задачу.

Участники чата: {members_list}

Текст: "{text}"

Ответь строго в формате:
ИСПОЛНИТЕЛЬ: @username (или "не указан", или "несколько:@user1,@user2" если подходят несколько)
ЗАДАЧА: текст задачи без имени исполнителя

Если имя похоже на одного из участников (Вася=Василий, Саша=Александр и т.д.), укажи его @username.
Если подходят несколько участников — перечисли всех через запятую."""

                    response = await ask_llm(
                        question=prompt,
                        system_prompt="Ты парсер задач. Отвечай строго в указанном формате.",
                        max_tokens=150,
                        temperature=0.1
                    )
                    
                    # Parse response
                    for line in response.split("\n"):
                        if "ИСПОЛНИТЕЛЬ:" in line.upper():
                            # Check for multiple matches
                            if "несколько" in line.lower() or "," in line:
                                usernames = re.findall(r"@(\w+)", line)
                                if len(usernames) > 1:
                                    # Store candidates for clarification
                                    result["multiple_candidates"] = []
                                    for username in usernames:
                                        for m in members:
                                            if m.username and m.username.lower() == username.lower():
                                                result["multiple_candidates"].append({
                                                    "id": m.id,
                                                    "username": m.username,
                                                    "name": f"{m.first_name or ''} {m.last_name or ''}".strip()
                                                })
                                                break
                            else:
                                match = re.search(r"@(\w+)", line)
                                if match:
                                    username = match.group(1)
                                    for m in members:
                                        if m.username and m.username.lower() == username.lower():
                                            result["assignee_id"] = m.id
                                            result["assignee_username"] = m.username
                                            break
                        elif "ЗАДАЧА:" in line.upper():
                            task = line.split(":", 1)[1].strip() if ":" in line else ""
                            if task:
                                result["task"] = task
        except Exception:
            pass  # Fallback to manual input
    
    # Try to parse deadline
    deadline_patterns = [
        r"(завтра|послезавтра|сегодня)",
        r"(через\s+\d+\s+(?:час|часа|часов|минут|минуты|дн|день|дней))",
    ]
    
    for pattern in deadline_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                result["deadline"] = parse_deadline(match.group(1))
                result["task"] = result["task"].replace(match.group(1), "").strip()
            except DateParseError:
                pass
            break
    
    # Clean up task text
    result["task"] = " ".join(result["task"].split())
    
    return result


async def receive_task_assignee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive task assignee from user."""
    text = update.message.text.strip()
    chat_id = context.user_data["task_chat_id"]
    
    # First, check for @username
    username_match = re.search(r"@(\w+)", text)
    
    async with get_session() as session:
        if username_match:
            username = username_match.group(1)
            result = await session.execute(
                select(User).where(User.username == username)
            )
            user = result.scalar_one_or_none()
            
            if user:
                is_member = await is_user_in_chat(session, user.id, chat_id)
                if is_member:
                    context.user_data["task_assignee_id"] = user.id
                    context.user_data["task_assignee_username"] = username
                    
                    await update.message.reply_text(
                        "Какой дедлайн? (например: завтра, в пятницу, 15.02)"
                    )
                    return States.TASK_DEADLINE
        
        # Try to find by name using LLM
        if settings.yandex_gpt_api_key or settings.openai_api_key:
            members_result = await session.execute(
                select(User).join(ChatMember).where(ChatMember.chat_id == chat_id)
            )
            members = members_result.scalars().all()
            
            if members:
                # Try exact or fuzzy match by name
                text_lower = text.lower().strip()
                matching = []
                
                for m in members:
                    first = (m.first_name or "").lower()
                    last = (m.last_name or "").lower()
                    full = f"{first} {last}".strip()
                    
                    # Check various matches
                    if (text_lower == first or 
                        text_lower == last or 
                        text_lower == full or
                        text_lower in first or
                        first.startswith(text_lower)):
                        matching.append(m)
                
                if len(matching) == 1:
                    # Exact match
                    user = matching[0]
                    context.user_data["task_assignee_id"] = user.id
                    context.user_data["task_assignee_username"] = user.username
                    
                    await update.message.reply_text(
                        f"👤 Исполнитель: @{user.username}\n\n"
                        "Какой дедлайн? (например: завтра, в пятницу, 15.02)"
                    )
                    return States.TASK_DEADLINE
                
                elif len(matching) > 1:
                    # Multiple matches - show buttons
                    buttons = []
                    for m in matching[:5]:
                        name = f"{m.first_name or ''} {m.last_name or ''}".strip()
                        buttons.append([
                            InlineKeyboardButton(
                                f"{name} (@{m.username})",
                                callback_data=f"task_assignee:{m.id}:{m.username}"
                            )
                        ])
                    buttons.append([
                        InlineKeyboardButton("❌ Другой", callback_data="task_assignee:other")
                    ])
                    
                    await update.message.reply_text(
                        "🤔 Нашёл несколько подходящих. Кого имел в виду?",
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                    return States.TASK_ASSIGNEE
                
                # No match by name in local DB - try LLM for nicknames
                from llm.client import ask_llm
                members_list = ", ".join([
                    f"{m.first_name or ''} {m.last_name or ''} (@{m.username})" 
                    for m in members if m.username
                ])
                
                try:
                    prompt = f"""Кто из участников соответствует имени "{text}"?
                    
Участники: {members_list}

Учитывай уменьшительные имена: Витя=Виктор, Саша=Александр, Давид=David, Дима=Дмитрий и т.д.

Ответь ТОЛЬКО @username одного человека или "не найден"."""

                    response = await ask_llm(
                        question=prompt,
                        system_prompt="Ты определяешь пользователя по имени. Отвечай только @username.",
                        max_tokens=50,
                        temperature=0.1
                    )
                    
                    found_match = re.search(r"@(\w+)", response)
                    if found_match:
                        username = found_match.group(1)
                        for m in members:
                            if m.username and m.username.lower() == username.lower():
                                context.user_data["task_assignee_id"] = m.id
                                context.user_data["task_assignee_username"] = m.username
                                
                                await update.message.reply_text(
                                    f"👤 Исполнитель: @{m.username}\n\n"
                                    "Какой дедлайн? (например: завтра, в пятницу, 15.02)"
                                )
                                return States.TASK_DEADLINE
                except Exception:
                    pass
    
    # Last resort - if text looks like username, try to verify via Telegram API
    potential_username = text.strip().replace("@", "")
    if potential_username and potential_username.isalnum():
        try:
            # Try to get chat member by username - this will work if user is in chat
            chat_member = await context.bot.get_chat_member(chat_id, f"@{potential_username}")
            if chat_member and chat_member.user:
                user = chat_member.user
                # Save to database for future
                async with get_session() as session:
                    db_user = await get_or_create_user(
                        session, user.id,
                        username=user.username,
                        first_name=user.first_name,
                        last_name=user.last_name
                    )
                    # Add to chat members
                    existing = await session.execute(
                        select(ChatMember).where(
                            ChatMember.chat_id == chat_id,
                            ChatMember.user_id == user.id
                        )
                    )
                    if not existing.scalar_one_or_none():
                        session.add(ChatMember(chat_id=chat_id, user_id=user.id))
                        await session.commit()
                
                context.user_data["task_assignee_id"] = user.id
                context.user_data["task_assignee_username"] = user.username or potential_username
                
                await update.message.reply_text(
                    f"👤 Исполнитель: @{user.username or potential_username}\n\n"
                    "Какой дедлайн? (например: завтра, в пятницу, 15.02)"
                )
                return States.TASK_DEADLINE
        except Exception:
            pass
    
    # Build helpful message
    known_names = []
    async with get_session() as session:
        members_result = await session.execute(
            select(User).join(ChatMember).where(ChatMember.chat_id == chat_id)
        )
        for m in members_result.scalars().all():
            name = m.first_name or ""
            if m.username:
                known_names.append(f"{name} (@{m.username})")
    
    hint = ""
    if known_names:
        hint = f"\n\nИзвестные мне участники:\n" + "\n".join(f"• {n}" for n in known_names[:5])
    
    await update.message.reply_text(
        f"🤷 Не нашёл «{text}» в чате.\n\n"
        f"Укажи точный @username (например: @Daviddobro88)"
        f"{hint}"
    )
    return States.TASK_ASSIGNEE


async def task_assignee_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle assignee selection from inline keyboard."""
    query = update.callback_query
    await query.answer()
    
    data = query.data.split(":")
    action = data[1] if len(data) > 1 else ""
    
    if action == "other":
        await query.edit_message_text(
            "Кто исполнитель? Укажи ответным сообщением @username"
        )
        return States.TASK_ASSIGNEE
    
    # Parse assignee_id and username
    try:
        assignee_id = int(data[1])
        assignee_username = data[2] if len(data) > 2 else ""
        
        context.user_data["task_assignee_id"] = assignee_id
        context.user_data["task_assignee_username"] = assignee_username
        
        await query.edit_message_text(
            f"👤 Исполнитель: @{assignee_username}\n\n"
            "Какой дедлайн? (например: завтра, в пятницу, 15.02)"
        )
        return States.TASK_DEADLINE
    except (ValueError, IndexError):
        await query.edit_message_text("Ошибка. Попробуй снова: /task")
        return ConversationHandler.END


async def receive_task_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive task deadline from user."""
    text = update.message.text.strip()
    
    try:
        deadline = parse_deadline(text)
        context.user_data["task_deadline"] = deadline
        
        # Ask about recurrence
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Каждый день", callback_data="recurrence:daily")],
            [InlineKeyboardButton("📅 Пн-Пт", callback_data="recurrence:weekdays")],
            [InlineKeyboardButton("📆 Каждую неделю", callback_data="recurrence:weekly")],
            [InlineKeyboardButton("🗓️ Каждый месяц", callback_data="recurrence:monthly")],
            [InlineKeyboardButton("➡️ Без повтора", callback_data="recurrence:none")],
        ])
        
        await update.message.reply_text(
            "🔄 Повторять задачу?",
            reply_markup=keyboard
        )
        return States.TASK_RECURRENCE
        
    except DateParseError as e:
        await update.message.reply_text(str(e))
        return States.TASK_DEADLINE


async def recurrence_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle recurrence selection."""
    query = update.callback_query
    await query.answer()
    
    recurrence = query.data.split(":")[1]
    context.user_data["task_recurrence"] = recurrence
    
    await query.edit_message_text(
        f"🔄 Повтор: {_get_recurrence_label(recurrence)}"
    )
    
    return await _create_task(update, context)


def _get_recurrence_label(recurrence: str) -> str:
    """Get human-readable recurrence label."""
    labels = {
        "none": "без повтора",
        "daily": "каждый день",
        "weekdays": "Пн-Пт",
        "weekly": "каждую неделю",
        "monthly": "каждый месяц",
    }
    return labels.get(recurrence, recurrence)


async def _create_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Create the task after all data is collected."""
    from database.models import RecurrenceType
    
    chat_id = context.user_data["task_chat_id"]
    author_id = context.user_data["task_author_id"]
    text = context.user_data["task_text"]
    deadline = context.user_data["task_deadline"]
    assignee_username = context.user_data.get("task_assignee_username")
    assignee_id = context.user_data.get("task_assignee_id")
    command_message_id = context.user_data.get("task_command_message_id")
    recurrence_str = context.user_data.get("task_recurrence", "none")
    
    # Map string to enum
    recurrence_map = {
        "none": RecurrenceType.NONE,
        "daily": RecurrenceType.DAILY,
        "weekdays": RecurrenceType.WEEKDAYS,
        "weekly": RecurrenceType.WEEKLY,
        "monthly": RecurrenceType.MONTHLY,
    }
    recurrence = recurrence_map.get(recurrence_str, RecurrenceType.NONE)
    
    async with get_session() as session:
        # Get assignee if we only have username
        if not assignee_id and assignee_username:
            result = await session.execute(
                select(User).where(User.username == assignee_username)
            )
            assignee = result.scalar_one_or_none()
            if assignee:
                assignee_id = assignee.id
        
        if not assignee_id:
            # Try to send message (could be callback or message)
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    "Не удалось найти исполнителя. Попробуй создать задачу заново."
                )
            else:
                await update.message.reply_text(
                    "Не удалось найти исполнителя. Попробуй создать задачу заново."
                )
            context.user_data.clear()
            return ConversationHandler.END
        
        # Create task
        task = Task(
            chat_id=chat_id,
            author_id=author_id,
            assignee_id=assignee_id,
            text=text,
            deadline=deadline,
            command_message_id=command_message_id,
            recurrence=recurrence,
        )
        session.add(task)
        await session.flush()
        
        # Get assignee for display
        result = await session.execute(select(User).where(User.id == assignee_id))
        assignee = result.scalar_one()
        
        deadline_str = format_date(deadline)
        recurrence_str = ""
        if recurrence != RecurrenceType.NONE:
            recurrence_str = f"\n🔄 Повтор: {_get_recurrence_label(recurrence.value)}"
        
        confirmation = (
            f'✅ Задача создана: "{text}"\n'
            f"Исполнитель: {assignee.display_name}\n"
            f"Дедлайн: {deadline_str}"
            f"{recurrence_str}"
        )
        
        # Send confirmation (could be from callback or message)
        if update.callback_query:
            await update.callback_query.edit_message_text(confirmation)
            reply = await context.bot.send_message(chat_id, "📌 Задача добавлена!")
        else:
            reply = await update.message.reply_text(confirmation)
        
        # Save confirmation message ID
        task.confirmation_message_id = reply.message_id
        
        # Try to notify assignee in DM
        if assignee_id != author_id:
            try:
                result = await session.execute(select(Chat).where(Chat.id == chat_id))
                chat = result.scalar_one()
                
                dm_text = (
                    f"📌 Новая задача!\n\n"
                    f'"{text}"\n'
                    f"Чат: {chat.title}\n"
                    f"Дедлайн: {deadline_str}"
                )
                
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Закрыть", callback_data=f"task:close:{task.id}"),
                    ]
                ])
                
                await context.bot.send_message(
                    chat_id=assignee_id,
                    text=dm_text,
                    reply_markup=keyboard
                )
                task.is_delivered = True
            except Exception:
                # Can't send DM - user hasn't started conversation with bot
                await update.message.reply_text(
                    f"{assignee.display_name}, напиши мне в ЛС, "
                    "чтобы получать задачи и напоминания"
                )
    
    context.user_data.clear()
    return ConversationHandler.END


async def tasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tasks command - list active tasks in chat."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Эта команда работает только в групповых чатах")
        return
    
    chat_id = update.effective_chat.id
    
    async with get_session() as session:
        result = await session.execute(
            select(Task)
            .where(
                Task.chat_id == chat_id,
                Task.status == TaskStatus.OPEN
            )
            .order_by(Task.deadline)
        )
        tasks = result.scalars().all()
        
        if not tasks:
            await update.message.reply_text("📋 Активных задач нет")
            return
        
        lines = ["📋 Активные задачи:\n"]
        
        for i, task in enumerate(tasks, 1):
            # Eager load assignee
            result = await session.execute(
                select(User).where(User.id == task.assignee_id)
            )
            task.assignee = result.scalar_one()
            
            lines.append(f"{i}. {format_task_short(task)}\n")
        
        await update.message.reply_text("\n".join(lines))


async def mytasks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mytasks command - list user's tasks."""
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    chat_id = update.effective_chat.id if chat_type != "private" else None
    
    async with get_session() as session:
        if chat_id:
            # In group - show tasks in this chat only
            result = await session.execute(
                select(Task)
                .where(
                    Task.assignee_id == user_id,
                    Task.chat_id == chat_id,
                    Task.status == TaskStatus.OPEN
                )
                .order_by(Task.deadline)
            )
            tasks = result.scalars().all()
            
            if not tasks:
                await update.message.reply_text("📋 У тебя нет активных задач в этом чате")
                return
            
            lines = ["📋 Твои задачи в этом чате:\n"]
            
            for i, task in enumerate(tasks, 1):
                deadline_str = format_date(task.deadline)
                overdue = " ⚠️ просрочена" if task.is_overdue else ""
                lines.append(f"{i}. {task.text} | Дедлайн: {deadline_str}{overdue}")
            
            await update.message.reply_text("\n".join(lines))
        else:
            # In DM - show all tasks grouped by chat
            result = await session.execute(
                select(Task)
                .where(
                    Task.assignee_id == user_id,
                    Task.status == TaskStatus.OPEN
                )
                .order_by(Task.deadline)
            )
            tasks = result.scalars().all()
            
            if not tasks:
                await update.message.reply_text("📋 У тебя нет активных задач")
                return
            
            # Group by chat
            by_chat = {}
            for task in tasks:
                if task.chat_id not in by_chat:
                    by_chat[task.chat_id] = []
                by_chat[task.chat_id].append(task)
            
            # Send each task as separate message with buttons
            for chat_id, chat_tasks in by_chat.items():
                result = await session.execute(
                    select(Chat).where(Chat.id == chat_id)
                )
                chat = result.scalar_one_or_none()
                chat_title = chat.title if chat else f"Чат {chat_id}"
                
                for task in chat_tasks:
                    result = await session.execute(
                        select(User).where(User.id == task.author_id)
                    )
                    author = result.scalar_one()
                    
                    deadline_str = format_date(task.deadline)
                    overdue = "\n⚠️ Просрочена!" if task.is_overdue else ""
                    
                    text = (
                        f"📌 {task.text}\n"
                        f"Чат: {chat_title}\n"
                        f"Автор: {author.display_name}\n"
                        f"Дедлайн: {deadline_str}{overdue}"
                    )
                    
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "✅ Закрыть", 
                                callback_data=f"task:close:{task.id}"
                            ),
                            InlineKeyboardButton(
                                "✏️ Редактировать", 
                                callback_data=f"task:edit:{task.id}"
                            ),
                        ]
                    ])
                    
                    await update.message.reply_text(text, reply_markup=keyboard)
            
            # Add button to show closed tasks
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "📋 Показать закрытые задачи",
                    callback_data="task:show_closed"
                )]
            ])
            await update.message.reply_text(
                "Это все твои активные задачи.",
                reply_markup=keyboard
            )


async def done_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /done command - close a task (reply to task message)."""
    if not update.message.reply_to_message:
        await update.message.reply_text("Ответь на сообщение с задачей")
        return
    
    reply_to = update.message.reply_to_message
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    async with get_session() as session:
        # Find task by message ID
        result = await session.execute(
            select(Task).where(
                and_(
                    Task.chat_id == chat_id,
                    (Task.command_message_id == reply_to.message_id) | 
                    (Task.confirmation_message_id == reply_to.message_id)
                )
            )
        )
        task = result.scalar_one_or_none()
        
        if not task:
            await update.message.reply_text("Это не задача. Ответь на сообщение с задачей")
            return
        
        if task.status == TaskStatus.CLOSED:
            await update.message.reply_text("Эта задача уже закрыта")
            return
        
        # Check permissions
        if not await can_close_task(session, user_id, task):
            await update.message.reply_text(
                "Закрыть задачу может только исполнитель, автор или админ"
            )
            return
        
        # Close task
        task.status = TaskStatus.CLOSED
        task.closed_at = datetime.utcnow()
        task.closed_by = user_id
        
        # Create next recurring task if needed
        next_task = await _create_next_recurring_task(session, task)
        
        # Get user who closed
        result = await session.execute(select(User).where(User.id == user_id))
        closer = result.scalar_one()
        
        msg = f'✅ {closer.display_name} закрыл задачу "{task.text}"'
        if next_task:
            msg += f"\n🔄 Следующая: {format_date(next_task.deadline)}"
        
        await update.message.reply_text(msg)


async def _create_next_recurring_task(session, task: Task) -> Optional[Task]:
    """Create next instance of a recurring task."""
    from database.models import RecurrenceType
    from dateutil.relativedelta import relativedelta
    
    if task.recurrence == RecurrenceType.NONE:
        return None
    
    # Calculate next deadline
    current_deadline = task.deadline
    
    if task.recurrence == RecurrenceType.DAILY:
        next_deadline = current_deadline + timedelta(days=1)
    elif task.recurrence == RecurrenceType.WEEKDAYS:
        next_deadline = current_deadline + timedelta(days=1)
        # Skip weekends
        while next_deadline.weekday() >= 5:  # Saturday=5, Sunday=6
            next_deadline += timedelta(days=1)
    elif task.recurrence == RecurrenceType.WEEKLY:
        next_deadline = current_deadline + timedelta(weeks=1)
    elif task.recurrence == RecurrenceType.MONTHLY:
        next_deadline = current_deadline + relativedelta(months=1)
    else:
        return None
    
    # Create new task
    new_task = Task(
        chat_id=task.chat_id,
        author_id=task.author_id,
        assignee_id=task.assignee_id,
        text=task.text,
        deadline=next_deadline,
        recurrence=task.recurrence,
        parent_task_id=task.parent_task_id or task.id,
    )
    session.add(new_task)
    await session.flush()
    
    return new_task


async def edit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /edit command - edit a task (reply to task message)."""
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "В ЛС используй кнопки под задачей для редактирования"
        )
        return ConversationHandler.END
    
    if not update.message.reply_to_message:
        await update.message.reply_text("Ответь на сообщение с задачей")
        return ConversationHandler.END
    
    reply_to = update.message.reply_to_message
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    args = " ".join(context.args) if context.args else ""
    
    async with get_session() as session:
        # Find task
        result = await session.execute(
            select(Task).where(
                and_(
                    Task.chat_id == chat_id,
                    (Task.command_message_id == reply_to.message_id) | 
                    (Task.confirmation_message_id == reply_to.message_id)
                )
            )
        )
        task = result.scalar_one_or_none()
        
        if not task:
            await update.message.reply_text("Это не задача. Ответь на сообщение с задачей")
            return ConversationHandler.END
        
        if not await can_edit_task(session, user_id, task):
            await update.message.reply_text(
                "Редактировать задачу может только автор или админ"
            )
            return ConversationHandler.END
        
        context.user_data["edit_task_id"] = task.id
        
        # Try to parse inline edit command
        if args:
            return await _process_inline_edit(update, context, session, task, args)
        
        # No args - ask what to edit
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Текст", callback_data=f"task:edit_field:text:{task.id}"),
                InlineKeyboardButton("Дедлайн", callback_data=f"task:edit_field:deadline:{task.id}"),
                InlineKeyboardButton("Исполнитель", callback_data=f"task:edit_field:assignee:{task.id}"),
            ]
        ])
        
        await update.message.reply_text("Что изменить?", reply_markup=keyboard)
        return ConversationHandler.END


async def _process_inline_edit(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE,
    session,
    task: Task,
    args: str
) -> int:
    """Process inline edit command like '/edit дедлайн завтра'."""
    args_lower = args.lower()
    
    changes = []
    
    # Check for deadline
    if "дедлайн" in args_lower:
        deadline_text = args_lower.split("дедлайн", 1)[1].strip()
        # Remove other keywords
        for keyword in ["исполнитель", "текст"]:
            if keyword in deadline_text:
                deadline_text = deadline_text.split(keyword)[0].strip()
        
        try:
            new_deadline = parse_deadline(deadline_text)
            task.deadline = new_deadline
            changes.append(f"Новый дедлайн: {format_date(new_deadline)}")
        except DateParseError as e:
            await update.message.reply_text(f"Ошибка в дедлайне: {e}")
            return ConversationHandler.END
    
    # Check for assignee
    if "исполнитель" in args_lower:
        assignee_text = args_lower.split("исполнитель", 1)[1].strip()
        username_match = re.search(r"@?(\w+)", assignee_text)
        
        if username_match:
            username = username_match.group(1)
            result = await session.execute(
                select(User).where(User.username == username)
            )
            new_assignee = result.scalar_one_or_none()
            
            if new_assignee:
                task.assignee_id = new_assignee.id
                changes.append(f"Новый исполнитель: {new_assignee.display_name}")
            else:
                await update.message.reply_text("Пользователь не найден")
                return ConversationHandler.END
    
    # Check for text
    if "текст" in args_lower:
        text_content = args_lower.split("текст", 1)[1].strip()
        # Use original case for text
        text_idx = args.lower().find("текст")
        new_text = args[text_idx + 5:].strip()
        
        # Remove other keywords from end
        for keyword in ["дедлайн", "исполнитель"]:
            if keyword in new_text.lower():
                new_text = new_text[:new_text.lower().find(keyword)].strip()
        
        if new_text:
            task.text = new_text[:settings.max_task_length]
            changes.append(f'Новый текст: "{task.text}"')
    
    if not changes:
        await update.message.reply_text(
            "Неизвестное поле. Доступно: дедлайн, исполнитель, текст"
        )
        return ConversationHandler.END
    
    # Notify about changes
    result = await session.execute(select(User).where(User.id == task.assignee_id))
    assignee = result.scalar_one()
    
    response = f'✏️ Задача изменена: "{task.text}"\n'
    response += "\n".join(changes)
    response += f"\n{assignee.display_name}, обрати внимание"
    
    await update.message.reply_text(response)
    
    context.user_data.clear()
    return ConversationHandler.END


async def task_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle task-related callback queries."""
    query = update.callback_query
    await query.answer()
    
    data = query.data.split(":")
    action = data[1]
    
    if action == "close":
        task_id = int(data[2])
        await _close_task_callback(update, context, task_id)
    
    elif action == "edit":
        task_id = int(data[2])
        # Show edit options
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Текст", callback_data=f"task:edit_field:text:{task_id}"),
                InlineKeyboardButton("Дедлайн", callback_data=f"task:edit_field:deadline:{task_id}"),
                InlineKeyboardButton("Исполнитель", callback_data=f"task:edit_field:assignee:{task_id}"),
            ],
            [InlineKeyboardButton("« Назад", callback_data=f"task:back:{task_id}")]
        ])
        await query.edit_message_reply_markup(reply_markup=keyboard)
    
    elif action == "edit_field":
        field = data[2]
        task_id = int(data[3])
        context.user_data["edit_task_id"] = task_id
        context.user_data["edit_field"] = field
        context.user_data["in_conversation"] = True
        
        prompts = {
            "text": "Введи новый текст задачи:",
            "deadline": "Введи новый дедлайн (например: завтра, в пятницу, 15.02):",
            "assignee": "Введи нового исполнителя (@username):",
        }
        
        await query.message.reply_text(prompts.get(field, "Введи новое значение:"))
    
    elif action == "show_closed":
        await _show_closed_tasks(update, context)
    
    elif action == "back":
        task_id = int(data[2])
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Закрыть", callback_data=f"task:close:{task_id}"),
                InlineKeyboardButton("✏️ Редактировать", callback_data=f"task:edit:{task_id}"),
            ]
        ])
        await query.edit_message_reply_markup(reply_markup=keyboard)


async def _close_task_callback(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE,
    task_id: int
) -> None:
    """Close task from callback button."""
    query = update.callback_query
    user_id = update.effective_user.id
    
    async with get_session() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        
        if not task:
            await query.edit_message_text("Задача не найдена")
            return
        
        if task.status == TaskStatus.CLOSED:
            await query.edit_message_text("Эта задача уже закрыта")
            return
        
        if not await can_close_task(session, user_id, task):
            await query.answer(
                "Закрыть задачу может только исполнитель, автор или админ",
                show_alert=True
            )
            return
        
        # Close task
        task.status = TaskStatus.CLOSED
        task.closed_at = datetime.utcnow()
        task.closed_by = user_id
        
        # Create next recurring task if needed
        next_task = await _create_next_recurring_task(session, task)
        
        result = await session.execute(select(User).where(User.id == user_id))
        closer = result.scalar_one()
        
        # Update message
        msg = f'✅ Задача закрыта: "{task.text}"\nЗакрыл: {closer.display_name}'
        if next_task:
            msg += f"\n🔄 Следующая: {format_date(next_task.deadline)}"
        
        await query.edit_message_text(msg)
        
        # Notify in chat
        result = await session.execute(select(Chat).where(Chat.id == task.chat_id))
        chat = result.scalar_one()
        
        chat_msg = f'✅ {closer.display_name} закрыл задачу "{task.text}"'
        if next_task:
            chat_msg += f"\n🔄 Следующая: {format_date(next_task.deadline)}"
        
        try:
            await context.bot.send_message(
                chat_id=task.chat_id,
                text=chat_msg
            )
        except Exception:
            pass  # Chat might be unavailable


async def _show_closed_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's closed tasks."""
    query = update.callback_query
    user_id = update.effective_user.id
    
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=settings.closed_tasks_retention_days)
    
    async with get_session() as session:
        result = await session.execute(
            select(Task)
            .where(
                Task.assignee_id == user_id,
                Task.status == TaskStatus.CLOSED,
                Task.closed_at >= cutoff
            )
            .order_by(Task.closed_at.desc())
            .limit(10)
        )
        tasks = result.scalars().all()
        
        if not tasks:
            await query.message.reply_text("Нет закрытых задач за последние 30 дней")
            return
        
        lines = ["📋 Закрытые задачи:\n"]
        
        for task in tasks:
            result = await session.execute(select(Chat).where(Chat.id == task.chat_id))
            chat = result.scalar_one_or_none()
            chat_title = chat.title if chat else "Неизвестный чат"
            
            closed_str = format_date(task.closed_at)
            lines.append(f"✓ {task.text}\n  Чат: {chat_title} | Закрыта: {closed_str}\n")
        
        await query.message.reply_text("\n".join(lines))


def get_task_conversation_handler() -> ConversationHandler:
    """Get conversation handler for task creation."""
    return ConversationHandler(
        entry_points=[CommandHandler("task", task_handler)],
        states={
            States.TASK_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_task_text)
            ],
            States.TASK_ASSIGNEE: [
                CallbackQueryHandler(task_assignee_callback, pattern=r"^task_assignee:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_task_assignee)
            ],
            States.TASK_DEADLINE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_task_deadline)
            ],
            States.TASK_RECURRENCE: [
                CallbackQueryHandler(recurrence_callback, pattern=r"^recurrence:")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        per_chat=True,
        per_user=True,
    )


def get_edit_conversation_handler() -> ConversationHandler:
    """Get conversation handler for task editing."""
    return ConversationHandler(
        entry_points=[CommandHandler("edit", edit_handler)],
        states={
            States.EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edit_value)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        per_chat=True,
        per_user=True,
    )


async def receive_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive edited value from user."""
    task_id = context.user_data.get("edit_task_id")
    field = context.user_data.get("edit_field")
    value = update.message.text.strip()
    
    if not task_id or not field:
        context.user_data.clear()
        return ConversationHandler.END
    
    async with get_session() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        
        if not task:
            await update.message.reply_text("Задача не найдена")
            context.user_data.clear()
            return ConversationHandler.END
        
        if field == "text":
            task.text = value[:settings.max_task_length]
            await update.message.reply_text(f'✏️ Текст обновлён: "{task.text}"')
        
        elif field == "deadline":
            try:
                new_deadline = parse_deadline(value)
                task.deadline = new_deadline
                await update.message.reply_text(
                    f"✏️ Дедлайн обновлён: {format_date(new_deadline)}"
                )
            except DateParseError as e:
                await update.message.reply_text(str(e))
                return States.EDIT_VALUE
        
        elif field == "assignee":
            username_match = re.search(r"@?(\w+)", value)
            if username_match:
                username = username_match.group(1)
                result = await session.execute(
                    select(User).where(User.username == username)
                )
                new_assignee = result.scalar_one_or_none()
                
                if new_assignee:
                    task.assignee_id = new_assignee.id
                    await update.message.reply_text(
                        f"✏️ Исполнитель обновлён: {new_assignee.display_name}"
                    )
                else:
                    await update.message.reply_text("Пользователь не найден")
                    return States.EDIT_VALUE
    
    context.user_data.clear()
    return ConversationHandler.END


# Import cancel_handler
from handlers.start import cancel_handler

