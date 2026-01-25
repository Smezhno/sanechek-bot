"""Ask LLM handler."""
from datetime import datetime, date
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select, func

from database import get_session, User
from database.models import Base
from sqlalchemy import String, Integer, BigInteger, Date
from sqlalchemy.orm import Mapped, mapped_column
from config import settings
from llm.client import ask_llm


# Daily limit per user
DAILY_LIMIT = 10


class AskUsage(Base):
    """Track /ask usage per user per day."""
    __tablename__ = "ask_usage"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    usage_date: Mapped[date] = mapped_column(Date, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


async def _process_question(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE, 
    question: str
) -> None:
    """Process a question to LLM with rate limiting."""
    user = update.effective_user
    
    async with get_session() as session:
        # Check daily limit
        today = date.today()
        
        result = await session.execute(
            select(AskUsage).where(
                AskUsage.user_id == user.id,
                AskUsage.usage_date == today
            )
        )
        usage = result.scalar_one_or_none()
        
        if usage and usage.count >= DAILY_LIMIT:
            await update.message.reply_text(
                f"Бро, всё, лимит на сегодня кончился. {DAILY_LIMIT} вопросов уже задал.\n"
                "Завтра приходи, разберём."
            )
            return
        
        # Update usage counter
        if usage:
            usage.count += 1
        else:
            usage = AskUsage(
                user_id=user.id,
                usage_date=today,
                count=1
            )
            session.add(usage)
        
        remaining = DAILY_LIMIT - usage.count
    
    # Send typing indicator
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing"
    )
    
    # Ask LLM
    try:
        response = await ask_llm(question)
        
        # Add remaining counter
        footer = f"\n\n_Осталось на сегодня: {remaining}_"
        
        await update.message.reply_text(
            response + footer,
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(
            "Чёт не получилось ответить. Попробуй позже, бро."
        )


async def ask_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ask command - ask a question to LLM."""
    question = " ".join(context.args) if context.args else ""
    
    if not question:
        await update.message.reply_text(
            "Вопрос-то где? Пиши так:\n"
            "`/ask Как приготовить борщ?`\n\n"
            "Или реплайни на любое моё сообщение — отвечу.",
            parse_mode="Markdown"
        )
        return
    
    await _process_question(update, context, question)


async def reply_to_bot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle replies to bot messages with smart intent detection.
    Routes to appropriate handler based on context:
    - Task editing if replying to task
    - Reminder editing if replying to reminder
    - Question answering for everything else
    """
    if not update.message or not update.message.text:
        return

    # Skip if in active conversation (ConversationHandler or waiting for input)
    if context.user_data.get("in_conversation") or \
       context.user_data.get("waiting_assignee_for") or \
       context.user_data.get("waiting_deadline_for") or \
       context.user_data.get("reminder_waiting_time"):
        return

    # Check if this is a reply to bot's message
    if not update.message.reply_to_message:
        return

    reply_to = update.message.reply_to_message
    if not reply_to.from_user or not reply_to.from_user.is_bot:
        return

    # Check if it's our bot
    if reply_to.from_user.id != context.bot.id:
        return

    # Skip commands
    text = update.message.text
    if text.startswith("/"):
        return

    # Get context to understand what user is replying to
    from handlers.reply_analyzer import get_reply_context
    from utils.intent_helpers import IntentType
    
    reply_context = await get_reply_context(update, context)
    
    # If replying to a task or reminder, use smart intent analysis
    if reply_context and reply_context.get("message_type") in ["task", "reminder"]:
        from handlers.reply_analyzer import analyze_reply
        
        # Analyze intent
        intent_result = await analyze_reply(update, context)
        
        # If clear edit or close intent, handle it
        if intent_result and intent_result.intent_type in [IntentType.EDIT_TASK, IntentType.EDIT_REMINDER, IntentType.CLOSE_TASK]:
            if intent_result.intent_type == IntentType.EDIT_TASK:
                await _handle_task_edit_from_reply(update, context, reply_context, intent_result)
                return
            elif intent_result.intent_type == IntentType.EDIT_REMINDER:
                await _handle_reminder_edit_from_reply(update, context, reply_context, intent_result)
                return
            elif intent_result.intent_type == IntentType.CLOSE_TASK:
                await _handle_task_close_from_reply(update, context, reply_context, intent_result)
                return
    
    # For all other cases (questions, dialog continuation, etc.) - answer as question
    await _process_question(update, context, text)
    


async def _handle_task_edit_from_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reply_context: dict,
    intent_result
) -> None:
    """Handle task editing from a reply."""
    from handlers.tasks import _process_inline_edit
    
    task = reply_context.get("task")
    if not task:
        await update.message.reply_text("❌ Задача не найдена")
        return
    
    # Build edit args from intent
    args = intent_result.new_value if intent_result.new_value else update.message.text
    
    # Call existing edit handler
    async with get_session() as session:
        await _process_inline_edit(update, context, session, task, args)


async def _handle_reminder_edit_from_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reply_context: dict,
    intent_result
) -> None:
    """Handle reminder editing from a reply."""
    from handlers.tasks import _process_reminder_edit
    
    reminder = reply_context.get("reminder")
    if not reminder:
        await update.message.reply_text("❌ Напоминание не найдено")
        return
    
    # Build edit args from intent
    args = intent_result.new_value if intent_result.new_value else update.message.text
    
    # Call existing edit handler
    async with get_session() as session:
        await _process_reminder_edit(update, context, session, reminder, args)


async def _handle_task_close_from_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reply_context: dict,
    intent_result
) -> None:
    """Handle task closing from a reply."""
    from database import get_session
    from database.models import User, TaskStatus
    from sqlalchemy import select
    from datetime import datetime
    
    task = reply_context.get("task")
    if not task:
        await update.message.reply_text("❌ Задача не найдена")
        return
    
    user_id = update.effective_user.id
    
    # Check permissions - only author or assignee can close
    if user_id not in [task.author_id, task.assignee_id]:
        async with get_session() as session:
            result = await session.execute(select(User).where(User.id == task.author_id))
            author = result.scalar_one_or_none()
            result = await session.execute(select(User).where(User.id == task.assignee_id))
            assignee = result.scalar_one_or_none()
            
            author_name = author.display_name if author else "автор"
            assignee_name = assignee.display_name if assignee else "исполнитель"
            
            await update.message.reply_text(
                f"❌ Закрыть задачу может только {author_name} или {assignee_name}"
            )
            return
    
    # If high confidence, close immediately; otherwise show confirmation
    if intent_result.confidence >= 0.8:
        # Close task directly
        async with get_session() as session:
            if task.status == TaskStatus.CLOSED:
                await update.message.reply_text("✅ Задача уже закрыта")
                return
            
            task.status = TaskStatus.CLOSED
            task.closed_at = datetime.utcnow()
            task.closed_by = user_id
            
            result = await session.execute(select(User).where(User.id == user_id))
            closer = result.scalar_one()
            
            await update.message.reply_text(
                f'✅ Задача закрыта: "{task.text}"\nСделал: {closer.display_name}'
            )
            await session.commit()
    else:
        # Show confirmation for lower confidence
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Да, закрыть", callback_data=f"task:close_confirm:{task.id}"),
                InlineKeyboardButton("❌ Нет", callback_data="task:close_cancel")
            ]
        ])
        
        await update.message.reply_text(
            f'Закрыть задачу "{task.text}"?',
            reply_markup=keyboard
        )

