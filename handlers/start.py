"""Start, help, and basic command handlers."""
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes, ConversationHandler
from sqlalchemy import select

from database import get_session, User, Chat, ChatMember, Message
from utils.permissions import get_or_create_user
from config import settings

logger = logging.getLogger(__name__)

# Message constants
MSG_CANCELLED = "Отменено"
MSG_NOTHING_TO_CANCEL = "Нечего отменять"
MSG_SUBSCRIBE_HINT = "Используй /subscribe, чтобы настроить, по каким чатам получать саммари"
MSG_OPEN_APP = "📱 Открой веб-интерфейс:"


# Welcome messages
WELCOME_DM = """Привет! Я Sanechek — ассистент для рабочих чатов.

Что я умею:
📋 Управлять задачами — создавай, отслеживай, получай напоминания
📊 Саммаризировать переписки — ежедневные отчёты по чатам  
💰 Учитывать расходы — фиксируй траты с автокатегоризацией

Добавь меня в рабочий чат, чтобы начать.

Команды: /help"""

HELP_GROUP = """Команды чата:

/task <текст> @исполнитель <дедлайн> — создать задачу
/tasks — активные задачи чата
/mytasks — мои задачи в этом чате
/done — закрыть задачу (реплай)
/edit — редактировать задачу (реплай)
/cost <сумма> <описание> — добавить расход
/summary — саммари чата за сегодня
/reminders — активные напоминания
/admins — список админов

Напоминания:
@{bot_username} напомни мне через 30 минут позвонить Ване
@{bot_username} напомни @vasya завтра в 15:00 отправить отчёт

Для админов:
/setadmin @user — назначить админа
/removeadmin @user — снять админа"""

HELP_DM = """Команды:

/app — открыть веб-интерфейс
/mytasks — все мои задачи
/reminders — мои напоминания
/summary — саммари по подпискам
/subscribe — управление подписками на чаты
/admins — список админов по всем чатам

Задачи можно закрывать и редактировать кнопками ниже."""


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    if update.effective_chat.type != "private":
        return
    
    user = update.effective_user
    
    # Save user to database
    async with get_session() as session:
        await get_or_create_user(
            session, 
            user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )
    
    await update.message.reply_text(WELCOME_DM)

    # Suggest subscription setup
    await update.message.reply_text(MSG_SUBSCRIBE_HINT)


async def app_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /app command - open Mini App."""
    web_app_button = InlineKeyboardButton(
        text="🚀 Открыть приложение",
        web_app=WebAppInfo(url=settings.mini_app_url)
    )

    keyboard = InlineKeyboardMarkup([[web_app_button]])

    await update.message.reply_text(MSG_OPEN_APP, reply_markup=keyboard)


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    chat_type = update.effective_chat.type
    
    if chat_type == "private":
        await update.message.reply_text(HELP_DM)
    else:
        help_text = HELP_GROUP.format(bot_username=settings.bot_username)
        await update.message.reply_text(help_text)


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel command - cancel current conversation."""
    # Check if we're in a conversation
    if context.user_data.get("in_conversation"):
        context.user_data.clear()
        await update.message.reply_text(MSG_CANCELLED)
        return ConversationHandler.END
    else:
        await update.message.reply_text(MSG_NOTHING_TO_CANCEL)
        return ConversationHandler.END


async def handle_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle new chat members - track when bot or users join."""
    if not update.message or not update.message.new_chat_members:
        return
    
    chat = update.effective_chat
    bot_id = context.bot.id
    
    async with get_session() as session:
        # Check if bot was added
        for member in update.message.new_chat_members:
            if member.id == bot_id:
                # Bot was added to chat
                result = await session.execute(
                    select(Chat).where(Chat.id == chat.id)
                )
                db_chat = result.scalar_one_or_none()
                
                if db_chat:
                    db_chat.is_active = True
                    db_chat.title = chat.title
                else:
                    db_chat = Chat(
                        id=chat.id,
                        title=chat.title,
                        is_active=True,
                        bot_added_at=datetime.utcnow()
                    )
                    session.add(db_chat)
            else:
                # Regular user joined
                user = await get_or_create_user(
                    session, 
                    member.id,
                    username=member.username,
                    first_name=member.first_name,
                    last_name=member.last_name
                )
                
                # Check if already a member
                result = await session.execute(
                    select(ChatMember).where(
                        ChatMember.user_id == member.id,
                        ChatMember.chat_id == chat.id
                    )
                )
                existing = result.scalar_one_or_none()
                
                if existing:
                    existing.left_at = None
                    existing.joined_at = datetime.utcnow()
                else:
                    chat_member = ChatMember(
                        user_id=member.id,
                        chat_id=chat.id
                    )
                    session.add(chat_member)


async def handle_left_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle when members leave chat."""
    if not update.message or not update.message.left_chat_member:
        return
    
    chat = update.effective_chat
    left_member = update.message.left_chat_member
    bot_id = context.bot.id
    
    async with get_session() as session:
        if left_member.id == bot_id:
            # Bot was removed
            result = await session.execute(
                select(Chat).where(Chat.id == chat.id)
            )
            db_chat = result.scalar_one_or_none()
            if db_chat:
                db_chat.is_active = False
        else:
            # Regular user left
            result = await session.execute(
                select(ChatMember).where(
                    ChatMember.user_id == left_member.id,
                    ChatMember.chat_id == chat.id
                )
            )
            chat_member = result.scalar_one_or_none()
            if chat_member:
                chat_member.left_at = datetime.utcnow()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store messages for summarization."""
    if not update.message or not update.message.text:
        return
    
    # Skip bot commands for summarization
    text = update.message.text
    is_command = text.startswith("/")
    
    chat = update.effective_chat
    user = update.effective_user
    
    async with get_session() as session:
        # Ensure chat exists
        result = await session.execute(
            select(Chat).where(Chat.id == chat.id)
        )
        db_chat = result.scalar_one_or_none()
        
        if not db_chat:
            db_chat = Chat(
                id=chat.id,
                title=chat.title,
                is_active=True
            )
            session.add(db_chat)
            await session.flush()
        
        # Ensure user exists
        db_user = await get_or_create_user(
            session,
            user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )
        
        # Ensure chat membership
        result = await session.execute(
            select(ChatMember).where(
                ChatMember.user_id == user.id,
                ChatMember.chat_id == chat.id
            )
        )
        membership = result.scalar_one_or_none()
        
        if not membership:
            membership = ChatMember(
                user_id=user.id,
                chat_id=chat.id
            )
            session.add(membership)
        elif membership.left_at:
            membership.left_at = None
        
        # Store message
        message = Message(
            message_id=update.message.message_id,
            chat_id=chat.id,
            user_id=user.id,
            text=text,
            is_bot_command=is_command
        )
        session.add(message)

