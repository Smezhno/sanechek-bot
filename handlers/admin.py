"""Admin management handlers."""
import re
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy import select

from database import get_session, User, ChatMember, Chat
from utils.permissions import get_or_create_user, is_admin, get_chat_admins


async def setadmin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /setadmin command - assign admin role."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Эта команда работает только в групповых чатах")
        return
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    args = " ".join(context.args) if context.args else ""
    
    async with get_session() as session:
        # Check if caller is admin
        if not await is_admin(session, user_id, chat_id):
            await update.message.reply_text("Назначать админов могут только админы")
            return
        
        # Parse target username
        username_match = re.search(r"@?(\w+)", args)
        if not username_match:
            await update.message.reply_text(
                "Укажи пользователя: /setadmin @username"
            )
            return
        
        username = username_match.group(1)
        
        # Find user
        result = await session.execute(
            select(User).where(User.username == username)
        )
        target_user = result.scalar_one_or_none()
        
        if not target_user:
            await update.message.reply_text("Пользователь не найден")
            return
        
        # Check if already admin
        if await is_admin(session, target_user.id, chat_id):
            await update.message.reply_text(f"@{username} уже админ")
            return
        
        # Get or create chat membership
        result = await session.execute(
            select(ChatMember)
            .where(
                ChatMember.user_id == target_user.id,
                ChatMember.chat_id == chat_id
            )
        )
        membership = result.scalar_one_or_none()
        
        if membership:
            membership.is_admin = True
        else:
            membership = ChatMember(
                user_id=target_user.id,
                chat_id=chat_id,
                is_admin=True
            )
            session.add(membership)
        
        await update.message.reply_text(f"✅ @{username} теперь админ")


async def removeadmin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /removeadmin command - remove admin role."""
    if update.effective_chat.type == "private":
        await update.message.reply_text("Эта команда работает только в групповых чатах")
        return
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    args = " ".join(context.args) if context.args else ""
    
    async with get_session() as session:
        # Check if caller is admin
        if not await is_admin(session, user_id, chat_id):
            await update.message.reply_text("Снимать админов могут только админы")
            return
        
        # Parse target username
        username_match = re.search(r"@?(\w+)", args)
        if not username_match:
            await update.message.reply_text(
                "Укажи пользователя: /removeadmin @username"
            )
            return
        
        username = username_match.group(1)
        
        # Find user
        result = await session.execute(
            select(User).where(User.username == username)
        )
        target_user = result.scalar_one_or_none()
        
        if not target_user:
            await update.message.reply_text("Пользователь не найден")
            return
        
        # Check if user is admin
        if not await is_admin(session, target_user.id, chat_id):
            await update.message.reply_text(f"@{username} не является админом")
            return
        
        # Check if trying to remove self and is last admin
        if target_user.id == user_id:
            admins = await get_chat_admins(session, chat_id)
            if len(admins) <= 1:
                await update.message.reply_text(
                    "Нельзя снять себя — ты единственный админ. "
                    "Сначала назначь другого"
                )
                return
        
        # Remove admin role
        # Check global admin flag
        if target_user.is_global_admin:
            await update.message.reply_text(
                f"@{username} — глобальный админ, его нельзя снять через команду"
            )
            return
        
        # Remove from chat membership
        result = await session.execute(
            select(ChatMember)
            .where(
                ChatMember.user_id == target_user.id,
                ChatMember.chat_id == chat_id
            )
        )
        membership = result.scalar_one_or_none()
        
        if membership:
            membership.is_admin = False
        
        await update.message.reply_text(f"✅ @{username} больше не админ")


async def admins_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /admins command - list admins."""
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    chat_id = update.effective_chat.id if chat_type != "private" else None
    
    async with get_session() as session:
        if chat_id:
            # In group - show admins for this chat
            admins = await get_chat_admins(session, chat_id)
            
            if not admins:
                await update.message.reply_text("👑 В этом чате нет назначенных админов")
                return
            
            # Check which admins are in this chat
            in_chat = []
            not_in_chat = []
            
            for admin in admins:
                result = await session.execute(
                    select(ChatMember)
                    .where(
                        ChatMember.user_id == admin.id,
                        ChatMember.chat_id == chat_id,
                        ChatMember.left_at.is_(None)
                    )
                )
                if result.scalar_one_or_none():
                    in_chat.append(admin)
                else:
                    not_in_chat.append(admin)
            
            lines = ["👑 Админы этого чата:"]
            
            if in_chat:
                admin_names = [a.display_name for a in in_chat]
                lines.append(", ".join(admin_names))
            else:
                lines.append("(нет админов в этом чате)")
            
            if not_in_chat:
                lines.append(f"\nЕщё {len(not_in_chat)} админ(ов) не состоят в этом чате.")
            
            await update.message.reply_text("\n".join(lines))
        
        else:
            # In DM - show admins for all user's chats
            result = await session.execute(
                select(ChatMember)
                .where(
                    ChatMember.user_id == user_id,
                    ChatMember.left_at.is_(None)
                )
            )
            memberships = result.scalars().all()
            
            if not memberships:
                await update.message.reply_text("Ты не состоишь ни в одном чате с ботом")
                return
            
            lines = ["👑 Админы:\n"]
            all_admins = set()
            
            for membership in memberships:
                result = await session.execute(
                    select(Chat).where(Chat.id == membership.chat_id)
                )
                chat = result.scalar_one_or_none()
                if not chat or not chat.is_active:
                    continue
                
                admins = await get_chat_admins(session, chat.id)
                admin_names = [a.display_name for a in admins]
                
                for admin in admins:
                    all_admins.add(admin.id)
                
                if admin_names:
                    lines.append(f'Чат "{chat.title}": {", ".join(admin_names)}')
                else:
                    lines.append(f'Чат "{chat.title}": (нет админов)')
            
            # Check for admins not in user's chats
            result = await session.execute(
                select(User).where(User.is_global_admin == True)
            )
            global_admins = result.scalars().all()
            
            not_in_chats = [a for a in global_admins if a.id not in all_admins]
            if not_in_chats:
                not_in_names = [a.display_name for a in not_in_chats]
                lines.append(f"\nНе состоят в твоих чатах: {', '.join(not_in_names)}")
            
            await update.message.reply_text("\n".join(lines))

