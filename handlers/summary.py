"""Summary and subscription handlers."""
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import select, and_

from database import get_session, Message, Chat, Subscription, User, ChatMember
from utils.formatters import truncate_summary
from utils.permissions import get_or_create_user, is_admin
from llm.summarizer import summarize_messages
from config import settings


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
            today = datetime.utcnow().strftime("%d.%m.%Y")
            await update.message.reply_text(
                f"📊 Саммари за {today}:\nПереписок не было."
            )
            return
        
        # Get usernames for messages
        user_ids = list(set(m.user_id for m in messages))
        result = await session.execute(
            select(User).where(User.id.in_(user_ids))
        )
        users = {u.id: u for u in result.scalars().all()}
        
        # Format messages for summarization
        formatted = []
        for msg in messages:
            user = users.get(msg.user_id)
            username = user.display_name if user else "Unknown"
            formatted.append(f"{username}: {msg.text}")
        
        # Send "typing" indicator
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing"
        )
        
        # Generate summary
        summary = await summarize_messages(formatted)
        
        today = datetime.utcnow().strftime("%d.%m.%Y")
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
            await update.message.reply_text(
                "У тебя нет активных подписок на чаты.\n"
                "Используй /subscribe, чтобы настроить подписки."
            )
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
            await update.message.reply_text("Нет чатов для саммаризации.")
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
                lines.append("Переписок не было.\n")
                continue
            
            # Get usernames
            user_ids = list(set(m.user_id for m in messages))
            result = await session.execute(
                select(User).where(User.id.in_(user_ids))
            )
            users = {u.id: u for u in result.scalars().all()}
            
            # Format messages
            formatted = []
            for msg in messages:
                user = users.get(msg.user_id)
                username = user.display_name if user else "Unknown"
                formatted.append(f"{username}: {msg.text}")
            
            # Generate summary
            summary = await summarize_messages(formatted)
            lines.append(summary + "\n")
        
        response = "\n".join(lines)
        response = truncate_summary(response)

        await update.message.reply_text(response, parse_mode="Markdown")


async def subscribe_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /subscribe command - manage summary subscriptions."""
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Эта команда работает только в личных сообщениях"
        )
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
        
        # Get chats where user is a member
        result = await session.execute(
            select(ChatMember)
            .where(
                ChatMember.user_id == user_id,
                ChatMember.left_at.is_(None)
            )
        )
        memberships = result.scalars().all()
        
        if not memberships:
            await update.message.reply_text(
                "Ты не состоишь ни в одном чате с ботом.\n"
                "Добавь бота в рабочий чат, чтобы получать саммари."
            )
            return
        
        # Get current subscriptions
        result = await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
        )
        subscriptions = {s.chat_id: s for s in result.scalars().all()}
        
        # Build button list
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
        
        if not buttons:
            await update.message.reply_text(
                "Нет доступных чатов для подписки."
            )
            return
        
        keyboard = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(
            "Подписки на саммари:\n\nНажми на чат, чтобы переключить.",
            reply_markup=keyboard
        )


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
            
            # Rebuild the keyboard
            result = await session.execute(
                select(ChatMember)
                .where(
                    ChatMember.user_id == user_id,
                    ChatMember.left_at.is_(None)
                )
            )
            memberships = result.scalars().all()
            
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
            
            keyboard = InlineKeyboardMarkup(buttons)
            await query.edit_message_reply_markup(reply_markup=keyboard)


async def send_daily_summaries(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send daily summaries to all subscribed users (called by scheduler)."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    
    async with get_session() as session:
        # Get all active subscriptions
        result = await session.execute(
            select(Subscription)
            .where(Subscription.is_active == True)
        )
        subscriptions = result.scalars().all()
        
        # Group by user
        by_user = {}
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
        
        today = datetime.utcnow().strftime("%d.%m.%Y")
        
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
                    lines.append("Переписок не было.\n")
                    continue
                
                # Get usernames
                msg_user_ids = list(set(m.user_id for m in messages))
                result = await session.execute(
                    select(User).where(User.id.in_(msg_user_ids))
                )
                users = {u.id: u for u in result.scalars().all()}
                
                # Format messages
                formatted = []
                for msg in messages:
                    user = users.get(msg.user_id)
                    username = user.display_name if user else "Unknown"
                    formatted.append(f"{username}: {msg.text}")
                
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
                except Exception:
                    # User might have blocked the bot
                    pass

