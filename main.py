"""
Sanechek - Telegram assistant for work chats.

Features:
- Task management with deadlines and assignees
- Expense tracking with auto-categorization
- Chat summarization
- Reminders and notifications
- Admin management
"""
import asyncio
import logging
from telegram.ext import Application

from config import settings
from database import init_db
from handlers.base import setup_handlers
from services.scheduler import setup_scheduler


# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Post-initialization callback."""
    # Initialize database
    await init_db()
    logger.info("Database initialized")
    
    # Set bot commands
    from telegram import BotCommand
    commands = [
        BotCommand("start", "Начать работу с ботом"),
        BotCommand("help", "Показать справку"),
        BotCommand("app", "Открыть веб-интерфейс"),
        BotCommand("task", "Создать задачу"),
        BotCommand("tasks", "Активные задачи чата"),
        BotCommand("mytasks", "Мои задачи"),
        BotCommand("done", "Закрыть задачу (реплай)"),
        BotCommand("edit", "Редактировать задачу (реплай)"),
        BotCommand("cost", "Добавить расход"),
        BotCommand("summary", "Саммари переписки"),
        BotCommand("subscribe", "Управление подписками"),
        BotCommand("reminders", "Активные напоминания"),
        BotCommand("admins", "Список админов"),
        BotCommand("setadmin", "Назначить админа"),
        BotCommand("removeadmin", "Снять админа"),
        BotCommand("cancel", "Отменить текущую операцию"),
        BotCommand("ask", "Задать вопрос ИИ (2 в день)"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands set")


def main() -> None:
    """Run the bot."""
    logger.info("Starting Sanechek bot...")
    
    # Create application
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .build()
    )
    
    # Setup handlers
    setup_handlers(application)
    logger.info("Handlers configured")
    
    # Setup scheduler
    setup_scheduler(application)
    logger.info("Scheduler configured")
    
    # Run the bot
    logger.info("Bot is running!")
    application.run_polling(allowed_updates=["message", "callback_query", "chat_member"])


if __name__ == "__main__":
    main()

