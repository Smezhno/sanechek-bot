"""Intent execution handlers - bridge between classification and action."""
import logging
from typing import Dict, Any

from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select

from database import get_session, Chat, User
from utils.intent_helpers import IntentType, IntentResult
from utils.permissions import get_or_create_user


logger = logging.getLogger(__name__)


async def execute_intent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    intent_result: IntentResult
) -> None:
    """
    Execute intent action based on classification result.
    
    Args:
        update: Telegram update
        context: Bot context
        intent_result: Classified intent with extracted data
    """
    message = update.message
    user = update.effective_user
    chat = update.effective_chat
    
    if intent_result.intent_type == IntentType.TASK:
        await _execute_task_creation(message, user, chat, context, intent_result.extracted_data)
    
    elif intent_result.intent_type == IntentType.REMINDER:
        await _execute_reminder_creation(message, user, chat, context, intent_result.extracted_data)
    
    elif intent_result.intent_type == IntentType.QUESTION:
        await _execute_question(message, context, intent_result.extracted_data)


async def execute_intent_from_callback(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    intent_result: IntentResult,
    pending_data: Dict[str, Any]
) -> None:
    """Execute intent from confirmation callback."""
    chat_id = pending_data["chat_id"]
    user_id = pending_data["user_id"]
    
    # Get chat and user objects
    async with get_session() as session:
        result = await session.execute(select(Chat).where(Chat.id == chat_id))
        chat_obj = result.scalar_one_or_none()
        
        result = await session.execute(select(User).where(User.id == user_id))
        user_obj = result.scalar_one_or_none()
    
    if not chat_obj or not user_obj:
        await query.edit_message_text("❌ Ошибка: чат или пользователь не найден")
        return
    
    # Create mock objects for execution
    class MockMessage:
        def __init__(self, chat_id, message_id):
            self.chat_id = chat_id
            self.message_id = query.message.message_id
            self.text = ""
        
        async def reply_text(self, text, **kwargs):
            return await query.message.reply_text(text, **kwargs)
    
    class MockChat:
        def __init__(self, chat_id, chat_type, title):
            self.id = chat_id
            self.type = chat_type
            self.title = title
    
    class MockUser:
        def __init__(self, user_id, username, first_name, last_name):
            self.id = user_id
            self.username = username
            self.first_name = first_name
            self.last_name = last_name
    
    message = MockMessage(chat_id, query.message.message_id)
    chat = MockChat(chat_obj.id, "group" if chat_obj.id < 0 else "private", chat_obj.title)
    user = MockUser(user_obj.id, user_obj.username, user_obj.first_name, user_obj.last_name)
    
    await query.edit_message_text("⏳ Выполняю...")
    
    if intent_result.intent_type == IntentType.TASK:
        await _execute_task_creation(message, user, chat, context, intent_result.extracted_data)
    elif intent_result.intent_type == IntentType.REMINDER:
        await _execute_reminder_creation(message, user, chat, context, intent_result.extracted_data)
    elif intent_result.intent_type == IntentType.QUESTION:
        await _execute_question(message, context, intent_result.extracted_data)


async def _execute_task_creation(
    message, user, chat, context: ContextTypes.DEFAULT_TYPE, data: Dict[str, Any]
) -> None:
    """
    Execute task creation using existing mention_handler logic.
    Reuses code from handlers/mention_handler.py
    """
    from handlers.mention_handler import (
        _parse_mention_with_llm,
        _parse_mention_fallback,
        _has_api_key,
        _create_task,
        _format_task_confirmation,
        get_chat_members_cached
    )
    from database.models import RecurrenceType
    
    task_text = data.get("task_text", "")
    if not task_text:
        await message.reply_text("❌ Не удалось определить текст задачи")
        return
    
    try:
        async with get_session() as session:
            # Ensure user and chat exist
            await get_or_create_user(
                session, user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name
            )
            
            is_dm = chat.type == "private"
            
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
            
            # Parse task with LLM or fallback
            if _has_api_key():
                parsed = await _parse_mention_with_llm(
                    task_text,
                    members_str,
                    "dm" if is_dm else "group"
                )
            else:
                parsed = _parse_mention_fallback(task_text)
            
            # Extract parsed data
            final_task_text = parsed.get("task", task_text)
            assignee_name = parsed.get("assignee")
            deadline = parsed.get("deadline")
            recurrence = parsed.get("recurrence", RecurrenceType.NONE)
            
            # Create task
            task = await _create_task(
                session,
                {
                    "text": final_task_text,
                    "assignee_id": None,  # Will be resolved by mention_handler
                    "assignee_name": assignee_name,
                    "deadline": deadline,
                    "recurrence": recurrence,
                    "author_id": user.id,
                    "chat_id": chat.id
                },
                command_message_id=message.message_id
            )
            
            # Send confirmation
            confirmation = _format_task_confirmation(
                final_task_text,
                assignee_name,
                deadline,
                recurrence
            )
            
            reply = await message.reply_text(confirmation)
            task.confirmation_message_id = reply.message_id
            
    except Exception as e:
        logger.error(f"Error creating task from intent: {e}")
        await message.reply_text("❌ Произошла ошибка при создании задачи")


async def _execute_reminder_creation(
    message, user, chat, context: ContextTypes.DEFAULT_TYPE, data: Dict[str, Any]
) -> None:
    """
    Execute reminder creation using existing remind_handler logic.
    Reuses code from handlers/reminders.py
    """
    from handlers.reminders import (
        _to_utc,
        _build_time_selection_keyboard,
        _compute_reminder_hash,
        _store_pending_reminder
    )
    from database.models import Reminder
    from utils.date_parser import parse_reminder_time, DateParseError
    from utils.formatters import format_date
    
    reminder_text = data.get("reminder_text", "")
    reminder_time = data.get("reminder_time", "")
    
    if not reminder_text:
        await message.reply_text("❌ Не удалось определить текст напоминания")
        return
    
    # If no time specified, ask user
    if not reminder_time or reminder_time in ["не указан", "нет", ""]:
        reminder_hash = _compute_reminder_hash(reminder_text, chat.id)
        _store_pending_reminder(context, reminder_hash, {
            "text": reminder_text,
            "recipient_id": user.id,
            "author_id": user.id,
            "chat_id": chat.id,
        })
        
        keyboard = _build_time_selection_keyboard(reminder_hash)
        await message.reply_text(
            f'📝 "{reminder_text}"\n\n⏰ Когда напомнить?',
            reply_markup=keyboard
        )
        context.user_data["reminder_waiting_time"] = reminder_hash
        return
    
    # Try to parse time
    try:
        remind_at = parse_reminder_time(reminder_time)
    except DateParseError as e:
        await message.reply_text(f"❌ Не понял время: {e}")
        return
    
    # Create reminder
    try:
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
            
            reminder = Reminder(
                chat_id=chat.id,
                author_id=user.id,
                recipient_id=user.id,
                text=reminder_text,
                remind_at=_to_utc(remind_at),
                command_message_id=message.message_id
            )
            session.add(reminder)
            await session.flush()
            
            time_str = format_date(remind_at, include_time=True)
            response = f'✅ Напомню в {time_str}: "{reminder_text}"'
            
            reply = await message.reply_text(response)
            reminder.confirmation_message_id = reply.message_id
            
    except Exception as e:
        logger.error(f"Error creating reminder from intent: {e}")
        await message.reply_text("❌ Произошла ошибка при создании напоминания")


async def _execute_question(
    message, context: ContextTypes.DEFAULT_TYPE, data: Dict[str, Any]
) -> None:
    """
    Execute question answering using existing ask_handler logic.
    Reuses code from handlers/ask.py
    """
    from handlers.ask import _process_question
    
    question = data.get("question", message.text if hasattr(message, 'text') else "")
    
    if not question:
        return
    
    # Create mock update for _process_question
    class MockUpdate:
        def __init__(self, msg, usr, cht):
            self.message = msg
            self.effective_user = usr
            self.effective_chat = cht
    
    class MockEffectiveUser:
        def __init__(self, usr):
            self.id = usr.id
            self.username = getattr(usr, 'username', None)
            self.first_name = getattr(usr, 'first_name', None)
    
    class MockEffectiveChat:
        def __init__(self, cht):
            self.id = cht.id
            self.type = cht.type
    
    mock_update = MockUpdate(
        message,
        MockEffectiveUser(message.from_user if hasattr(message, 'from_user') else type('obj', (object,), {'id': 0})()),
        MockEffectiveChat(type('obj', (object,), {'id': message.chat_id, 'type': 'group'})())
    )
    
    await _process_question(mock_update, context, question)

