"""Summary and subscription handlers."""
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select

from database import get_session, Message, Chat, Subscription, User, ChatMember
from utils.formatters import truncate_summary
from utils.permissions import get_or_create_user, is_admin
from llm.summarizer import summarize_messages

logger = logging.getLogger(__name__)

# Message constants
MSG_PRIVATE_ONLY = "Эта команда работает только в личных сообщениях"
MSG_NO_SUBSCRIPTIONS = (
    "У тебя нет активных подписок на чаты.\n"
    "Используй /subscribe, чтобы настроить подписки."
)
MSG_NO_CHATS_FOR_SUMMARY = "Нет чатов для саммаризации."
MSG_NO_CHATS_WITH_BOT = (
    "Ты не состоишь ни в одном чате с ботом.\n"
    "Добавь бота в рабочий чат, чтобы получать саммари."
)
MSG_NO_CHATS_AVAILABLE = "Нет доступных чатов для подписки."
MSG_SUBSCRIBE_PROMPT = "Подписки на саммари:\n\nНажми на чат, чтобы переключить."
MSG_NO_MESSAGES = "Переписок не было."


async def _format_messages_for_summary(
    session,
    messages: list[Message]
) -> list[str]:
    """Format messages with usernames for summarization."""
    if not messages:
        return []

    user_ids = list(set(m.user_id for m in messages))
    result = await session.execute(
        select(User).where(User.id.in_(user_ids))
    )
    users = {u.id: u for u in result.scalars().all()}

    formatted = []
    for msg in messages:
        user = users.get(msg.user_id)
        username = user.display_name if user else "Unknown"
        formatted.append(f"{username}: {msg.text}")
    return formatted


async def _build_subscription_keyboard(
    session,
    user_id: int
) -> Optional[InlineKeyboardMarkup]:
    """Build subscription toggle keyboard for user."""
    result = await session.execute(
        select(ChatMember)
        .where(
            ChatMember.user_id == user_id,
            ChatMember.left_at.is_(None)
        )
    )
    memberships = result.scalars().all()

    if not memberships:
        return None

    result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
    )
    subscriptions = {s.chat_id: s for s in result.scalars().all()}

    buttons = []
    for membership in memberships:
        result = await session.execute(
            select(Chat).where(Chat.id == membership.chat_id)
        )
        chat = result.scalar_one_or_none()
        if not chat or not chat.is_active:
            continue

        sub = subscriptions.get(chat.id)
        is_active = sub.is_active if sub else False
        status = "✅ вкл" if is_active else "❌ выкл"

        buttons.append([
            InlineKeyboardButton(
                f"📁 {chat.title} [{status}]",
                callback_data=f"subscribe:toggle:{chat.id}"
            )
        ])

    return InlineKeyboardMarkup(buttons) if buttons else None


async def summary_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /summary command - get chat summary."""
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    chat_id = update.effective_chat.id if chat_type != "private" else None
    
    if chat_id:
        # In group - summarize this chat
        await _summarize_chat(update, context, chat_id)
    else:
        # In DM - summarize subscribed chats
        await _summarize_subscribed_chats(update, context, user_id)


async def _summarize_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int
) -> None:
    """Summarize a single chat."""
    # Get messages from last 24 hours
    cutoff = datetime.utcnow() - timedelta(hours=24)
    today = datetime.utcnow().strftime("%d.%m.%Y")

    async with get_session() as session:
        result = await session.execute(
            select(Message)
            .where(
                Message.chat_id == chat_id,
                Message.is_bot_command == False,
                Message.created_at >= cutoff
            )
            .order_by(Message.created_at)
        )
        messages = result.scalars().all()

        if not messages:
            await update.message.reply_text(
                f"📊 Саммари за {today}:\n{MSG_NO_MESSAGES}"
            )
            return

        # Format messages for summarization
        formatted = await _format_messages_for_summary(session, messages)

        # Send "typing" indicator
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing"
        )

        # Generate summary
        summary = await summarize_messages(formatted)

        response = f"📊 Саммари за {today}:\n\n{summary}"
        response = truncate_summary(response)

        await update.message.reply_text(response, parse_mode="Markdown")


async def _summarize_subscribed_chats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int
) -> None:
    """Summarize all subscribed chats for user."""
    async with get_session() as session:
        # Get active subscriptions
        result = await session.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user_id,
                Subscription.is_active == True
            )
        )
        subscriptions = result.scalars().all()

        # Check if user is admin (gets all chats)
        user_is_admin = await is_admin(session, user_id)

        if not subscriptions and not user_is_admin:
            await update.message.reply_text(MSG_NO_SUBSCRIPTIONS)
            return

        chat_ids = [s.chat_id for s in subscriptions]

        # For admins, add all their chats
        if user_is_admin:
            result = await session.execute(
                select(ChatMember.chat_id)
                .where(
                    ChatMember.user_id == user_id,
                    ChatMember.left_at.is_(None)
                )
            )
            admin_chat_ids = [row[0] for row in result.all()]
            chat_ids = list(set(chat_ids + admin_chat_ids))

        if not chat_ids:
            await update.message.reply_text(MSG_NO_CHATS_FOR_SUMMARY)
            return

        cutoff = datetime.utcnow() - timedelta(hours=24)
        today = datetime.utcnow().strftime("%d.%m.%Y")

        lines = [f"📊 Саммари за {today}:\n"]

        for chat_id in chat_ids:
            result = await session.execute(
                select(Chat).where(Chat.id == chat_id)
            )
            chat = result.scalar_one_or_none()
            if not chat:
                continue

            # Get messages
            result = await session.execute(
                select(Message)
                .where(
                    Message.chat_id == chat_id,
                    Message.is_bot_command == False,
                    Message.created_at >= cutoff
                )
                .order_by(Message.created_at)
            )
            messages = result.scalars().all()

            lines.append(f"📁 {chat.title}:")

            if not messages:
                lines.append(f"{MSG_NO_MESSAGES}\n")
                continue

            # Format messages for summarization
            formatted = await _format_messages_for_summary(session, messages)

            # Generate summary
            summary = await summarize_messages(formatted)
            lines.append(summary + "\n")

        response = "\n".join(lines)
        response = truncate_summary(response)

        await update.message.reply_text(response, parse_mode="Markdown")


async def subscribe_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /subscribe command - manage summary subscriptions."""
    if update.effective_chat.type != "private":
        await update.message.reply_text(MSG_PRIVATE_ONLY)
        return

    user_id = update.effective_user.id
    user = update.effective_user

    async with get_session() as session:
        # Ensure user exists
        await get_or_create_user(
            session, user_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )

        # Check if user is in any chats
        result = await session.execute(
            select(ChatMember)
            .where(
                ChatMember.user_id == user_id,
                ChatMember.left_at.is_(None)
            )
        )
        memberships = result.scalars().all()

        if not memberships:
            await update.message.reply_text(MSG_NO_CHATS_WITH_BOT)
            return

        # Build keyboard using helper
        keyboard = await _build_subscription_keyboard(session, user_id)

        if not keyboard:
            await update.message.reply_text(MSG_NO_CHATS_AVAILABLE)
            return

        await update.message.reply_text(MSG_SUBSCRIBE_PROMPT, reply_markup=keyboard)


async def subscribe_callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle subscription toggle callbacks."""
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    action = data[1]

    if action == "toggle":
        chat_id = int(data[2])
        user_id = update.effective_user.id

        async with get_session() as session:
            # Get or create subscription
            result = await session.execute(
                select(Subscription)
                .where(
                    Subscription.user_id == user_id,
                    Subscription.chat_id == chat_id
                )
            )
            subscription = result.scalar_one_or_none()

            if subscription:
                subscription.is_active = not subscription.is_active
            else:
                subscription = Subscription(
                    user_id=user_id,
                    chat_id=chat_id,
                    is_active=True
                )
                session.add(subscription)

            await session.flush()

            # Rebuild keyboard using helper
            keyboard = await _build_subscription_keyboard(session, user_id)
            if keyboard:
                await query.edit_message_reply_markup(reply_markup=keyboard)


async def send_daily_summaries(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send daily summaries to all subscribed users (called by scheduler)."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    today = datetime.utcnow().strftime("%d.%m.%Y")

    async with get_session() as session:
        # Get all active subscriptions
        result = await session.execute(
            select(Subscription)
            .where(Subscription.is_active == True)
        )
        subscriptions = result.scalars().all()

        # Group by user
        by_user: dict[int, list[int]] = {}
        for sub in subscriptions:
            if sub.user_id not in by_user:
                by_user[sub.user_id] = []
            by_user[sub.user_id].append(sub.chat_id)

        # Also include admins
        result = await session.execute(
            select(User).where(User.is_global_admin == True)
        )
        admins = result.scalars().all()

        for admin in admins:
            result = await session.execute(
                select(ChatMember.chat_id)
                .where(
                    ChatMember.user_id == admin.id,
                    ChatMember.left_at.is_(None)
                )
            )
            admin_chats = [row[0] for row in result.all()]

            if admin.id not in by_user:
                by_user[admin.id] = []
            by_user[admin.id] = list(set(by_user[admin.id] + admin_chats))

        for user_id, chat_ids in by_user.items():
            lines = [f"📊 Саммари за {today}:\n"]

            for chat_id in chat_ids:
                result = await session.execute(
                    select(Chat).where(Chat.id == chat_id)
                )
                chat = result.scalar_one_or_none()
                if not chat or not chat.is_active:
                    continue

                # Get messages
                result = await session.execute(
                    select(Message)
                    .where(
                        Message.chat_id == chat_id,
                        Message.is_bot_command == False,
                        Message.created_at >= cutoff
                    )
                    .order_by(Message.created_at)
                )
                messages = result.scalars().all()

                lines.append(f"📁 {chat.title}:")

                if not messages:
                    lines.append(f"{MSG_NO_MESSAGES}\n")
                    continue

                # Format messages for summarization
                formatted = await _format_messages_for_summary(session, messages)

                # Generate summary
                summary = await summarize_messages(formatted)
                lines.append(summary + "\n")

            if len(lines) > 1:  # Has at least one chat
                response = "\n".join(lines)
                response = truncate_summary(response)

                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=response,
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    # User might have blocked the bot
                    logger.debug("Failed to send summary to user %s: %s", user_id, e)

