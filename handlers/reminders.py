"""Reminder handlers."""
import logging
import re
from datetime import datetime, timedelta
from typing import TypedDict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from database import get_session, Reminder, User, Chat, ReminderStatus
from utils.date_parser import parse_reminder_time, DateParseError
from utils.formatters import format_date, format_reminder_short
from utils.permissions import get_or_create_user, can_cancel_reminder
from config import settings

logger = logging.getLogger(__name__)

# Constants
PENDING_HASH_MODULO = 10000

# Message constants
MSG_REMIND_WHAT = "О чём напомнить?"
MSG_REMIND_WHEN = (
    'Не понял, когда напомнить. Укажи время, например: '
    '"через 30 минут", "завтра в 15:00", "в пятницу"'
)
MSG_REMIND_WHEN_WITH_BUTTONS = "⏰ Когда напомнить?"
MSG_NO_ACTIVE_REMINDERS = "🔔 Активных напоминаний нет"
MSG_NO_REMINDERS_TO_CANCEL = "Нет напоминаний для отмены"
MSG_REMINDER_NOT_FOUND = "Напоминание не найдено"
MSG_REMINDER_NOT_ACTIVE = "Это напоминание уже не активно"
MSG_CANCEL_NO_PERMISSION = "Отменить может только автор, получатель или админ"
MSG_SELECT_TO_CANCEL = "Выбери напоминание для отмены:"
MSG_PENDING_EXPIRED = "⏰ Предложение устарело. Создай напоминание заново."


class PendingReminderData(TypedDict, total=False):
    """Pending reminder data."""
    text: str
    recipient_id: int
    author_id: int
    chat_id: int


def _compute_reminder_hash(text: str, chat_id: int) -> str:
    """Compute hash for pending reminder."""
    return str(abs(hash(f"{text}:{chat_id}")) % PENDING_HASH_MODULO)


def _get_pending_reminder_key(reminder_hash: str) -> str:
    """Get bot_data key for pending reminder."""
    return f"pending_reminder_{reminder_hash}"


def _store_pending_reminder(
    context: ContextTypes.DEFAULT_TYPE,
    reminder_hash: str,
    data: PendingReminderData
) -> None:
    """Store pending reminder data."""
    context.bot_data[_get_pending_reminder_key(reminder_hash)] = data


def _get_pending_reminder(
    context: ContextTypes.DEFAULT_TYPE,
    reminder_hash: str
) -> Optional[PendingReminderData]:
    """Get pending reminder data."""
    return context.bot_data.get(_get_pending_reminder_key(reminder_hash))


def _delete_pending_reminder(
    context: ContextTypes.DEFAULT_TYPE,
    reminder_hash: str
) -> None:
    """Delete pending reminder data."""
    context.bot_data.pop(_get_pending_reminder_key(reminder_hash), None)


def _build_time_selection_keyboard(reminder_hash: str) -> InlineKeyboardMarkup:
    """Build keyboard for time selection."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⏰ Через час",
                callback_data=f"reminder:time:1h:{reminder_hash}"
            ),
            InlineKeyboardButton(
                "🌙 Вечером",
                callback_data=f"reminder:time:evening:{reminder_hash}"
            ),
        ],
        [
            InlineKeyboardButton(
                "🌅 Завтра утром",
                callback_data=f"reminder:time:tomorrow:{reminder_hash}"
            ),
            InlineKeyboardButton(
                "⌨️ Ввести",
                callback_data=f"reminder:time:manual:{reminder_hash}"
            ),
        ],
    ])


async def remind_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle @bot напомни... mentions."""
    if update.effective_chat.type == "private":
        return
    
    message = update.message
    text = message.text
    user = update.effective_user
    chat = update.effective_chat
    
    # Extract the reminder request (everything after "напомни" when @bot is mentioned)
    pattern = rf"@{settings.bot_username}.*?напомни\s*"
    match = re.search(pattern, text, re.IGNORECASE)
    
    if not match:
        return
    
    reminder_text = text[match.end():].strip()
    
    if not reminder_text:
        await message.reply_text(MSG_REMIND_WHAT)
        return
    
    async with get_session() as session:
        # Ensure chat and user exist
        result = await session.execute(select(Chat).where(Chat.id == chat.id))
        db_chat = result.scalar_one_or_none()
        if not db_chat:
            db_chat = Chat(id=chat.id, title=chat.title, is_active=True)
            session.add(db_chat)
        
        author = await get_or_create_user(
            session, user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )
        
        # Parse recipient (default to author if "мне" or no recipient specified)
        recipient_id = user.id
        recipient_username = None
        
        # Check for @username in the reminder (not the bot)
        mentioned_username = None
        username_match = re.search(r"@(\w+)", reminder_text)
        if username_match:
            mentioned_username = username_match.group(1)
            if mentioned_username.lower() != settings.bot_username.lower():
                # Try to find user in database
                result = await session.execute(
                    select(User).where(User.username.ilike(mentioned_username))
                )
                recipient = result.scalar_one_or_none()
                
                if recipient:
                    recipient_id = recipient.id
                # If not found, keep author as recipient - @username will be in the text
        
        # Parse time
        # First, try to find time expressions
        time_patterns = [
            r"через\s+(?:\d+|полчаса|\w+)\s+(?:минут|мин|час|дн|день|дней)",
            r"через\s+минуту",
            r"через\s+минутку",
            r"через\s+час(?:ик)?",
            r"через\s+день",
            r"через\s+полчаса",
            r"завтра(?:\s+в\s+\d{1,2}(?::\d{2})?)?",
            r"послезавтра(?:\s+в\s+\d{1,2}(?::\d{2})?)?",
            r"в\s+(?:понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)(?:\s+в\s+\d{1,2}(?::\d{2})?)?",
            r"в\s+\d{1,2}(?::\d{2})?",
            r"\d{1,2}\.\d{1,2}(?:\.\d{2,4})?(?:\s+в\s+\d{1,2}(?::\d{2})?)?",
            r"\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+в\s+\d{1,2}(?::\d{2})?)?",
            r"утром|вечером|днём|днем",
        ]
        
        time_text = None
        reminder_content = reminder_text
        
        for pattern in time_patterns:
            match = re.search(pattern, reminder_text, re.IGNORECASE)
            if match:
                time_text = match.group(0)
                # Remove time from reminder content
                reminder_content = reminder_text[:match.start()] + reminder_text[match.end():]
                break
        
        if not time_text:
            # Store pending reminder and show time selection buttons
            reminder_hash = _compute_reminder_hash(reminder_content, chat.id)
            _store_pending_reminder(context, reminder_hash, {
                "text": reminder_content,
                "recipient_id": recipient_id,
                "author_id": user.id,
                "chat_id": chat.id,
            })

            keyboard = _build_time_selection_keyboard(reminder_hash)
            await message.reply_text(
                f'📝 "{reminder_content}"\n\n{MSG_REMIND_WHEN_WITH_BUTTONS}',
                reply_markup=keyboard
            )
            context.user_data["reminder_waiting_time"] = reminder_hash
            return
        
        try:
            remind_at = parse_reminder_time(time_text)
        except DateParseError as e:
            await message.reply_text(str(e))
            return
        
        # Clean up reminder content
        # Remove "мне" if present
        reminder_content = re.sub(r"\bмне\b", "", reminder_content, flags=re.IGNORECASE)
        # Only remove @username if recipient was found in database
        # Otherwise keep it to mention them in the reminder
        if mentioned_username and recipient_id != user.id:
            # Recipient found - can remove @username from text
            reminder_content = reminder_content.replace(f"@{mentioned_username}", "")
        # Clean up extra spaces
        reminder_content = " ".join(reminder_content.split()).strip()
        
        if not reminder_content:
            await message.reply_text(MSG_REMIND_WHAT)
            return
        
        # Create reminder
        reminder = Reminder(
            chat_id=chat.id,
            author_id=user.id,
            recipient_id=recipient_id,
            text=reminder_content,
            remind_at=remind_at,
        )
        session.add(reminder)
        await session.flush()
        
        # Format response
        time_str = format_date(remind_at, include_time=True)
        
        if recipient_id == user.id:
            response = f'✅ Напомню в {time_str}: "{reminder_content}"'
        else:
            result = await session.execute(select(User).where(User.id == recipient_id))
            recipient = result.scalar_one()
            response = f'✅ Напомню {recipient.display_name} {time_str}: "{reminder_content}"'
        
        reply = await message.reply_text(response)
        reminder.confirmation_message_id = reply.message_id


async def reminders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reminders command - list active reminders."""
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    chat_id = update.effective_chat.id if chat_type != "private" else None
    
    async with get_session() as session:
        if chat_id:
            # In group - show reminders in this chat
            result = await session.execute(
                select(Reminder)
                .where(
                    Reminder.chat_id == chat_id,
                    Reminder.status == ReminderStatus.PENDING
                )
                .order_by(Reminder.remind_at)
            )
            reminders = result.scalars().all()
            
            if not reminders:
                await update.message.reply_text(MSG_NO_ACTIVE_REMINDERS)
                return

            lines = ["🔔 Напоминания в этом чате:\n"]
            
            for i, reminder in enumerate(reminders, 1):
                # Get recipient and author
                result = await session.execute(
                    select(User).where(User.id == reminder.recipient_id)
                )
                recipient = result.scalar_one()
                
                result = await session.execute(
                    select(User).where(User.id == reminder.author_id)
                )
                author = result.scalar_one()
                
                time_str = format_date(reminder.remind_at, include_time=True)
                
                lines.append(
                    f'{i}. "{reminder.text}" — {time_str}\n'
                    f'   Кому: {recipient.display_name} | Создал: {author.display_name}\n'
                )
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "Отменить напоминание",
                    callback_data="reminder:cancel_menu"
                )]
            ])
            
            await update.message.reply_text("\n".join(lines), reply_markup=keyboard)
        
        else:
            # In DM - show all user's reminders grouped by chat
            result = await session.execute(
                select(Reminder)
                .where(
                    Reminder.recipient_id == user_id,
                    Reminder.status == ReminderStatus.PENDING
                )
                .order_by(Reminder.remind_at)
            )
            reminders = result.scalars().all()
            
            if not reminders:
                await update.message.reply_text(MSG_NO_ACTIVE_REMINDERS)
                return

            # Group by chat
            by_chat = {}
            for reminder in reminders:
                if reminder.chat_id not in by_chat:
                    by_chat[reminder.chat_id] = []
                by_chat[reminder.chat_id].append(reminder)
            
            lines = ["🔔 Твои напоминания:\n"]
            
            counter = 1
            for chat_id, chat_reminders in by_chat.items():
                result = await session.execute(
                    select(Chat).where(Chat.id == chat_id)
                )
                chat = result.scalar_one_or_none()
                chat_title = chat.title if chat else f"Чат {chat_id}"
                
                lines.append(f'\nЧат "{chat_title}":')
                
                for reminder in chat_reminders:
                    lines.append(f"{counter}. {format_reminder_short(reminder)}")
                    counter += 1
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "Отменить напоминание",
                    callback_data="reminder:cancel_menu"
                )]
            ])
            
            await update.message.reply_text("\n".join(lines), reply_markup=keyboard)


async def reminder_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle reminder-related callback queries."""
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    action = data[1]

    if action == "cancel_menu":
        await _show_cancel_menu(update, context)

    elif action == "cancel":
        reminder_id = int(data[2])
        await _cancel_reminder(update, context, reminder_id)

    elif action == "time":
        time_option = data[2]
        reminder_hash = data[3]
        await _handle_time_selection(update, context, time_option, reminder_hash)


async def _handle_time_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    time_option: str,
    reminder_hash: str
) -> None:
    """Handle time selection from buttons."""
    query = update.callback_query
    pending = _get_pending_reminder(context, reminder_hash)

    if not pending:
        await query.edit_message_text(MSG_PENDING_EXPIRED)
        return

    now = datetime.utcnow()

    # Calculate remind_at based on option
    if time_option == "1h":
        remind_at = now + timedelta(hours=1)
    elif time_option == "evening":
        # Set to 19:00 today or tomorrow if already past
        remind_at = now.replace(hour=19, minute=0, second=0, microsecond=0)
        if remind_at <= now:
            remind_at += timedelta(days=1)
    elif time_option == "tomorrow":
        # Set to 9:00 tomorrow
        remind_at = (now + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
    elif time_option == "manual":
        # Ask user to enter time manually
        await query.edit_message_text(
            f'📝 "{pending["text"]}"\n\n'
            f"Введи время напоминания (например: через 2 часа, завтра в 15:00):"
        )
        context.user_data["reminder_waiting_time"] = reminder_hash
        return
    else:
        await query.edit_message_text(MSG_PENDING_EXPIRED)
        return

    # Create reminder
    async with get_session() as session:
        reminder = Reminder(
            chat_id=pending["chat_id"],
            author_id=pending["author_id"],
            recipient_id=pending["recipient_id"],
            text=pending["text"],
            remind_at=remind_at,
        )
        session.add(reminder)
        await session.flush()

        # Get recipient for display
        if pending["recipient_id"] == pending["author_id"]:
            recipient_text = ""
        else:
            result = await session.execute(
                select(User).where(User.id == pending["recipient_id"])
            )
            recipient = result.scalar_one_or_none()
            recipient_text = f" для {recipient.display_name}" if recipient else ""

        time_str = format_date(remind_at, include_time=True)
        await query.edit_message_text(
            f'✅ Напомню{recipient_text} {time_str}:\n"{pending["text"]}"'
        )

    _delete_pending_reminder(context, reminder_hash)
    context.user_data.pop("reminder_waiting_time", None)


async def reminder_time_input_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle manual time input for reminders."""
    if not update.message or not update.message.text:
        return

    reminder_hash = context.user_data.get("reminder_waiting_time")
    if not reminder_hash:
        return

    pending = _get_pending_reminder(context, reminder_hash)
    if not pending:
        context.user_data.pop("reminder_waiting_time", None)
        return

    text = update.message.text.strip()

    try:
        remind_at = parse_reminder_time(text)
    except DateParseError as e:
        await update.message.reply_text(
            f"❌ Не понял время: {e}\n\n"
            "Попробуй ещё раз (например: через 2 часа, завтра в 15:00)"
        )
        return

    # Create reminder
    async with get_session() as session:
        reminder = Reminder(
            chat_id=pending["chat_id"],
            author_id=pending["author_id"],
            recipient_id=pending["recipient_id"],
            text=pending["text"],
            remind_at=remind_at,
        )
        session.add(reminder)
        await session.flush()

        # Get recipient for display
        if pending["recipient_id"] == pending["author_id"]:
            recipient_text = ""
        else:
            result = await session.execute(
                select(User).where(User.id == pending["recipient_id"])
            )
            recipient = result.scalar_one_or_none()
            recipient_text = f" для {recipient.display_name}" if recipient else ""

        time_str = format_date(remind_at, include_time=True)
        await update.message.reply_text(
            f'✅ Напомню{recipient_text} {time_str}:\n"{pending["text"]}"'
        )

    _delete_pending_reminder(context, reminder_hash)
    context.user_data.pop("reminder_waiting_time", None)


async def _show_cancel_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show menu to select reminder to cancel."""
    query = update.callback_query
    user_id = update.effective_user.id
    
    async with get_session() as session:
        # Get user's reminders (as recipient or author)
        result = await session.execute(
            select(Reminder)
            .where(
                (Reminder.recipient_id == user_id) | (Reminder.author_id == user_id),
                Reminder.status == ReminderStatus.PENDING
            )
            .order_by(Reminder.remind_at)
            .limit(10)
        )
        reminders = result.scalars().all()
        
        if not reminders:
            await query.edit_message_text(MSG_NO_REMINDERS_TO_CANCEL)
            return

        buttons = []
        for reminder in reminders:
            text_preview = reminder.text[:30] + "..." if len(reminder.text) > 30 else reminder.text
            buttons.append([
                InlineKeyboardButton(
                    f'❌ "{text_preview}"',
                    callback_data=f"reminder:cancel:{reminder.id}"
                )
            ])
        
        buttons.append([
            InlineKeyboardButton("« Назад", callback_data="reminder:back")
        ])
        
        keyboard = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(
            MSG_SELECT_TO_CANCEL,
            reply_markup=keyboard
        )


async def _cancel_reminder(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reminder_id: int
) -> None:
    """Cancel a reminder."""
    query = update.callback_query
    user_id = update.effective_user.id
    
    async with get_session() as session:
        result = await session.execute(
            select(Reminder).where(Reminder.id == reminder_id)
        )
        reminder = result.scalar_one_or_none()
        
        if not reminder:
            await query.edit_message_text(MSG_REMINDER_NOT_FOUND)
            return

        if reminder.status != ReminderStatus.PENDING:
            await query.edit_message_text(MSG_REMINDER_NOT_ACTIVE)
            return

        # Check permissions
        if not await can_cancel_reminder(session, user_id, reminder):
            await query.answer(MSG_CANCEL_NO_PERMISSION, show_alert=True)
            return
        
        # Cancel reminder
        reminder.status = ReminderStatus.CANCELLED
        reminder.cancelled_at = datetime.utcnow()
        reminder.cancelled_by = user_id
        
        await query.edit_message_text(
            f'✅ Напоминание отменено: "{reminder.text}"'
        )


async def send_reminder(
    context: ContextTypes.DEFAULT_TYPE,
    reminder: Reminder
) -> None:
    """Send a reminder notification (called by scheduler).

    Smart delivery: first try DM, then group chat if DM fails.
    """
    async with get_session() as session:
        # Refresh reminder from DB
        result = await session.execute(
            select(Reminder).where(Reminder.id == reminder.id)
        )
        reminder = result.scalar_one_or_none()

        if not reminder or reminder.status != ReminderStatus.PENDING:
            return

        # Get recipient and author
        result = await session.execute(
            select(User).where(User.id == reminder.recipient_id)
        )
        recipient = result.scalar_one()

        result = await session.execute(
            select(User).where(User.id == reminder.author_id)
        )
        author = result.scalar_one()

        # Get chat for context
        result = await session.execute(
            select(Chat).where(Chat.id == reminder.chat_id)
        )
        chat = result.scalar_one_or_none()
        chat_title = chat.title if chat else "чат"

        # Format message for DM (includes chat context)
        if reminder.author_id != reminder.recipient_id:
            dm_text = (
                f"⏰ Напоминание из чата \"{chat_title}\":\n\n"
                f"{reminder.text}\n\n"
                f"(создал {author.display_name})"
            )
        else:
            dm_text = (
                f"⏰ Напоминание из чата \"{chat_title}\":\n\n"
                f"{reminder.text}"
            )

        # Format message for group chat
        if reminder.author_id != reminder.recipient_id:
            group_text = (
                f"⏰ {recipient.display_name}, напоминаю: {reminder.text}\n"
                f"(создал {author.display_name})"
            )
        else:
            group_text = f"⏰ {recipient.display_name}, напоминаю: {reminder.text}"

        # Try DM first
        dm_sent = False
        try:
            await context.bot.send_message(
                chat_id=recipient.id,
                text=dm_text
            )
            dm_sent = True
            logger.debug("Reminder %s sent to DM of user %s", reminder.id, recipient.id)
        except Exception as e:
            logger.debug(
                "Failed to send reminder %s to DM of user %s: %s",
                reminder.id, recipient.id, e
            )

        # If DM failed, send to group
        if not dm_sent:
            try:
                await context.bot.send_message(
                    chat_id=reminder.chat_id,
                    text=group_text
                )
                logger.debug(
                    "Reminder %s sent to group chat %s",
                    reminder.id, reminder.chat_id
                )
            except Exception as e:
                logger.debug(
                    "Failed to send reminder %s to chat %s: %s",
                    reminder.id, reminder.chat_id, e
                )
                return

        reminder.status = ReminderStatus.SENT
        reminder.sent_at = datetime.utcnow()

