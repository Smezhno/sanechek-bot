"""Permission checking utilities."""
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import User, ChatMember, Task, Reminder
from config import settings


async def get_or_create_user(
    session: AsyncSession, 
    user_id: int, 
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None
) -> User:
    """Get existing user or create new one."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if user is None:
        # Check if user is initial admin
        is_admin = user_id in settings.initial_admins
        
        user = User(
            id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            is_global_admin=is_admin
        )
        session.add(user)
        await session.flush()
    else:
        # Update user info if changed
        if username and user.username != username:
            user.username = username
        if first_name and user.first_name != first_name:
            user.first_name = first_name
        if last_name and user.last_name != last_name:
            user.last_name = last_name
    
    return user


async def is_admin(session: AsyncSession, user_id: int, chat_id: Optional[int] = None) -> bool:
    """
    Check if user is admin.
    
    Args:
        session: Database session
        user_id: Telegram user ID
        chat_id: Optional chat ID to check chat-specific admin rights
    
    Returns:
        True if user is admin (global or chat-specific)
    """
    # Check if initial admin
    if user_id in settings.initial_admins:
        return True
    
    # Check global admin flag
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    
    if user and user.is_global_admin:
        return True
    
    # Check chat-specific admin
    if chat_id:
        result = await session.execute(
            select(ChatMember).where(
                ChatMember.user_id == user_id,
                ChatMember.chat_id == chat_id,
                ChatMember.left_at.is_(None)
            )
        )
        member = result.scalar_one_or_none()
        if member and member.is_admin:
            return True
    
    return False


async def can_close_task(session: AsyncSession, user_id: int, task: Task) -> bool:
    """
    Check if user can close a task.
    Allowed: assignee, author, admin
    """
    # Assignee or author can always close
    if user_id == task.assignee_id or user_id == task.author_id:
        return True
    
    # Admin can close
    return await is_admin(session, user_id, task.chat_id)


async def can_edit_task(session: AsyncSession, user_id: int, task: Task) -> bool:
    """
    Check if user can edit a task.
    Allowed: author, admin
    """
    # Author can always edit
    if user_id == task.author_id:
        return True
    
    # Admin can edit
    return await is_admin(session, user_id, task.chat_id)


async def can_cancel_reminder(session: AsyncSession, user_id: int, reminder: Reminder) -> bool:
    """
    Check if user can cancel a reminder.
    Allowed: recipient, author, admin
    """
    # Recipient or author can cancel
    if user_id == reminder.recipient_id or user_id == reminder.author_id:
        return True
    
    # Admin can cancel
    return await is_admin(session, user_id, reminder.chat_id)


async def get_chat_admins(session: AsyncSession, chat_id: int) -> list[User]:
    """Get all admins for a chat."""
    # Get global admins
    result = await session.execute(
        select(User).where(User.is_global_admin == True)
    )
    admins = list(result.scalars().all())
    admin_ids = {a.id for a in admins}
    
    # Get chat-specific admins
    result = await session.execute(
        select(ChatMember).where(
            ChatMember.chat_id == chat_id,
            ChatMember.is_admin == True,
            ChatMember.left_at.is_(None)
        )
    )
    chat_members = result.scalars().all()
    
    for member in chat_members:
        if member.user_id not in admin_ids:
            result = await session.execute(
                select(User).where(User.id == member.user_id)
            )
            user = result.scalar_one_or_none()
            if user:
                admins.append(user)
                admin_ids.add(user.id)
    
    return admins


async def is_user_in_chat(session: AsyncSession, user_id: int, chat_id: int) -> bool:
    """Check if user is a member of the chat."""
    result = await session.execute(
        select(ChatMember).where(
            ChatMember.user_id == user_id,
            ChatMember.chat_id == chat_id,
            ChatMember.left_at.is_(None)
        )
    )
    return result.scalar_one_or_none() is not None

