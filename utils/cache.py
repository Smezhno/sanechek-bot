"""Cache utilities for chat members."""
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import ChatMember, User


class CachedMember(NamedTuple):
    """Cached chat member data."""
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    display_name: str


# In-memory cache: chat_id -> (members, cached_at)
_members_cache: dict[int, tuple[list[CachedMember], datetime]] = {}
CACHE_TTL = 300  # 5 minutes


async def get_chat_members_cached(
    chat_id: int,
    session: AsyncSession,
    force: bool = False
) -> list[CachedMember]:
    """
    Get chat members with caching.

    Args:
        chat_id: Telegram chat ID
        session: Database session
        force: Force cache refresh

    Returns:
        List of CachedMember objects
    """
    now = datetime.utcnow()

    if not force and chat_id in _members_cache:
        members, cached_at = _members_cache[chat_id]
        if now - cached_at < timedelta(seconds=CACHE_TTL):
            return members

    # Load from database
    members = await _load_members(chat_id, session)
    _members_cache[chat_id] = (members, now)
    return members


async def _load_members(chat_id: int, session: AsyncSession) -> list[CachedMember]:
    """Load chat members from database."""
    result = await session.execute(
        select(ChatMember, User)
        .join(User, ChatMember.user_id == User.id)
        .where(
            ChatMember.chat_id == chat_id,
            ChatMember.left_at.is_(None)
        )
    )
    rows = result.all()

    members = []
    for membership, user in rows:
        members.append(CachedMember(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            display_name=user.display_name
        ))

    return members


def invalidate_cache(chat_id: int) -> None:
    """Invalidate cache for a specific chat."""
    _members_cache.pop(chat_id, None)


def invalidate_all_cache() -> None:
    """Invalidate all cached data."""
    _members_cache.clear()


async def find_member_by_username(
    chat_id: int,
    username: str,
    session: AsyncSession
) -> CachedMember | None:
    """Find a member by username (case-insensitive)."""
    members = await get_chat_members_cached(chat_id, session)
    username_lower = username.lower()

    for member in members:
        if member.username and member.username.lower() == username_lower:
            return member

    return None


async def find_members_by_name(
    chat_id: int,
    name: str,
    session: AsyncSession
) -> list[CachedMember]:
    """
    Find members by first/last name (fuzzy matching).

    Returns list of matching members (may be empty, one, or multiple).
    """
    members = await get_chat_members_cached(chat_id, session)
    name_lower = name.lower().strip()

    matches = []
    for member in members:
        # Check first name
        if member.first_name and name_lower in member.first_name.lower():
            matches.append(member)
            continue

        # Check last name
        if member.last_name and name_lower in member.last_name.lower():
            matches.append(member)
            continue

        # Check full name
        full_name = f"{member.first_name or ''} {member.last_name or ''}".strip().lower()
        if name_lower in full_name:
            matches.append(member)

    return matches
