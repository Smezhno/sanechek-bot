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
DAILY_LIMIT = 2


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
                f"🚫 Лимит исчерпан! Ты уже задал {DAILY_LIMIT} вопроса сегодня.\n"
                "Приходи завтра 😉"
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
        footer = f"\n\n_Осталось вопросов сегодня: {remaining}_"
        
        await update.message.reply_text(
            response + footer,
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(
            "😔 Не удалось получить ответ. Попробуй позже."
        )


async def ask_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ask command - ask a question to LLM."""
    question = " ".join(context.args) if context.args else ""
    
    if not question:
        await update.message.reply_text(
            "Задай вопрос после команды:\n"
            "`/ask Как приготовить борщ?`\n\n"
            "Или ответь на любое моё сообщение с вопросом 🙂",
            parse_mode="Markdown"
        )
        return
    
    await _process_question(update, context, question)


async def reply_to_bot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle replies to bot messages - treat as questions."""
    if not update.message or not update.message.text:
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
    
    # Skip very short messages (likely just reactions)
    if len(text) < 5:
        return
    
    # Process as question
    await _process_question(update, context, text)

