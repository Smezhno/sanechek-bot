"""Expense tracking handlers."""
import logging
import re
from typing import Optional
from typing_extensions import TypedDict

from telegram import Update
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler,
    MessageHandler, filters
)
from sqlalchemy import select

from database import get_session, Expense, User, Chat
from handlers.base import States
from handlers.start import cancel_handler
from utils.categories import categorize_expense
from utils.formatters import format_amount
from utils.permissions import get_or_create_user
from config import settings

logger = logging.getLogger(__name__)

# Message constants
MSG_GROUP_ONLY = "Эта команда работает только в групповых чатах"
MSG_ASK_AMOUNT = "Какая сумма?"
MSG_ASK_DESCRIPTION = "Опиши расход"
MSG_AMOUNT_POSITIVE = "Сумма должна быть больше нуля"
MSG_AMOUNT_TOO_BIG = "Сумма слишком большая. Проверь, нет ли ошибки"
MSG_AMOUNT_INVALID = "Не понял сумму. Введи число"
MSG_DESCRIPTION_EMPTY = "Описание не может быть пустым. Попробуй ещё раз:"


class ParsedCost(TypedDict):
    """Result of parsing cost command."""
    amount: Optional[float]
    description: Optional[str]
    error: Optional[str]


async def cost_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cost command - add an expense."""
    # Only works in groups
    if update.effective_chat.type == "private":
        await update.message.reply_text(MSG_GROUP_ONLY)
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
    
    # Store context
    context.user_data["in_conversation"] = True
    context.user_data["cost_chat_id"] = chat.id
    context.user_data["cost_author_id"] = user.id
    
    if not args:
        # No arguments - ask for amount
        await update.message.reply_text(MSG_ASK_AMOUNT)
        return States.COST_AMOUNT

    # Try to parse amount and description
    parsed = _parse_cost_command(args)

    if parsed["amount"] is not None:
        context.user_data["cost_amount"] = parsed["amount"]

        if parsed["description"]:
            context.user_data["cost_description"] = parsed["description"]
            return await _create_expense(update, context)
        else:
            await update.message.reply_text(MSG_ASK_DESCRIPTION)
            return States.COST_DESCRIPTION
    else:
        if parsed["error"]:
            await update.message.reply_text(parsed["error"])
            return States.COST_AMOUNT
        await update.message.reply_text(MSG_ASK_AMOUNT)
        return States.COST_AMOUNT


def _parse_cost_command(text: str) -> ParsedCost:
    """Parse cost command arguments."""
    result: ParsedCost = {
        "amount": None,
        "description": None,
        "error": None,
    }

    # Try to extract amount from the beginning
    # Patterns: "5000", "5 000", "5000.00", "5000,00"
    amount_match = re.match(r"^([\d\s]+(?:[.,]\d{1,2})?)", text.strip())

    if amount_match:
        amount_str = amount_match.group(1).strip()
        # Clean up: remove spaces, replace comma with dot
        amount_str = amount_str.replace(" ", "").replace(",", ".")

        try:
            amount = float(amount_str)

            # Validate
            if amount <= 0:
                result["error"] = MSG_AMOUNT_POSITIVE
                return result

            if amount > settings.max_expense_amount:
                result["error"] = MSG_AMOUNT_TOO_BIG
                return result

            result["amount"] = amount

            # Description is everything after the amount
            description = text[amount_match.end():].strip()
            if description:
                result["description"] = description

        except ValueError:
            result["error"] = MSG_AMOUNT_INVALID

    return result


async def receive_cost_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive expense amount from user."""
    text = update.message.text.strip()

    parsed = _parse_cost_command(text)

    if parsed["amount"] is not None:
        context.user_data["cost_amount"] = parsed["amount"]

        # Check if description was included
        if parsed["description"]:
            context.user_data["cost_description"] = parsed["description"]
            return await _create_expense(update, context)

        await update.message.reply_text(MSG_ASK_DESCRIPTION)
        return States.COST_DESCRIPTION
    else:
        error = parsed["error"] or MSG_AMOUNT_INVALID
        await update.message.reply_text(error)
        return States.COST_AMOUNT


async def receive_cost_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive expense description from user."""
    text = update.message.text.strip()

    if not text:
        await update.message.reply_text(MSG_DESCRIPTION_EMPTY)
        return States.COST_DESCRIPTION

    context.user_data["cost_description"] = text
    return await _create_expense(update, context)


async def _create_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Create the expense after all data is collected."""
    chat_id = context.user_data["cost_chat_id"]
    author_id = context.user_data["cost_author_id"]
    amount = context.user_data["cost_amount"]
    description = context.user_data["cost_description"]

    # Auto-categorize
    category = categorize_expense(description)

    async with get_session() as session:
        expense = Expense(
            chat_id=chat_id,
            author_id=author_id,
            amount=amount,
            description=description,
            category=category,
        )
        session.add(expense)

    logger.debug(
        "Expense created: chat_id=%s, author_id=%s, amount=%s, category=%s",
        chat_id, author_id, amount, category
    )

    amount_str = format_amount(amount)
    await update.message.reply_text(
        f"💰 Расход добавлен: {amount_str} — {description} (категория: {category})"
    )

    context.user_data.clear()
    return ConversationHandler.END


def get_cost_conversation_handler() -> ConversationHandler:
    """Get conversation handler for expense creation."""
    return ConversationHandler(
        entry_points=[CommandHandler("cost", cost_handler)],
        states={
            States.COST_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cost_amount)
            ],
            States.COST_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cost_description)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        per_chat=True,
        per_user=True,
    )

