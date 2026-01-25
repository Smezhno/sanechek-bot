"""Base handler setup and common utilities."""
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ConversationHandler, filters
)

from config import settings


# Conversation states
class States:
    """Conversation states for multi-step commands."""
    # Task creation
    TASK_TEXT = 1
    TASK_ASSIGNEE = 2
    TASK_DEADLINE = 3
    TASK_RECURRENCE = 4
    
    # Task editing
    EDIT_FIELD = 10
    EDIT_VALUE = 11
    
    # Expense creation
    COST_AMOUNT = 20
    COST_DESCRIPTION = 21
    
    # Reminder cancellation
    CANCEL_REMINDER_SELECT = 30


def setup_handlers(app: Application) -> None:
    """Setup all bot handlers."""
    from handlers.start import (
        start_handler, help_handler, cancel_handler, app_handler,
        handle_new_chat_members, handle_left_chat_member,
        handle_message
    )
    from handlers.tasks import (
        get_task_conversation_handler,
        get_edit_conversation_handler,
        tasks_handler, mytasks_handler, done_handler,
        task_callback_handler, tasks_filter_callback
    )
    from handlers.expenses import get_cost_conversation_handler
    from handlers.reminders import (
        remind_handler, reminders_handler,
        reminder_callback_handler, reminder_time_input_handler
    )
    from handlers.summary import (
        summary_handler, subscribe_handler,
        subscribe_callback_handler
    )
    from handlers.admin import (
        setadmin_handler, removeadmin_handler, admins_handler
    )
    from handlers.ask import ask_handler, reply_to_bot_handler
    from handlers.sarcasm import sarcasm_handler
    from handlers.task_detector import analyze_for_tasks, suggest_task_callback, force_detect_handler, handle_task_details
    from handlers.mention_handler import mention_handler, mention_callback_handler
    from handlers.intent_router import intent_router_handler, intent_callback_handler
    
    # Basic commands
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("app", app_handler))
    
    # Conversation handlers (must be added before simple command handlers)
    app.add_handler(get_task_conversation_handler())
    app.add_handler(get_edit_conversation_handler())
    app.add_handler(get_cost_conversation_handler())
    
    # Task commands
    app.add_handler(CommandHandler("tasks", tasks_handler))
    app.add_handler(CommandHandler("mytasks", mytasks_handler))
    app.add_handler(CommandHandler("done", done_handler))
    
    # Expense commands are in conversation handler
    
    # Smart @bot mentions for task creation (must be BEFORE remind_handler)
    # Matches @bot <text> but NOT @bot ... напомни (reminders are separate)
    # IMPORTANT: Exclude commands to prevent intercepting /summary@bot, /subscribe@bot, etc.
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(rf"(?i)@{settings.bot_username}") & ~filters.Regex(r"(?i)напомни"),
        mention_handler
    ))

    # Reminder commands (matches @bot ... напомни with anything in between)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(rf"(?i)@{settings.bot_username}.*напомни"),
        remind_handler
    ))
    app.add_handler(CommandHandler("reminders", reminders_handler))
    
    # Summary commands
    app.add_handler(CommandHandler("summary", summary_handler))
    app.add_handler(CommandHandler("subscribe", subscribe_handler))
    
    # Admin commands
    app.add_handler(CommandHandler("setadmin", setadmin_handler))
    app.add_handler(CommandHandler("removeadmin", removeadmin_handler))
    app.add_handler(CommandHandler("admins", admins_handler))
    
    # Ask LLM command
    app.add_handler(CommandHandler("ask", ask_handler))
    
    # Force task detection (for testing)
    app.add_handler(CommandHandler("detect", force_detect_handler))
    
    # Intent router - auto-detect user intents from natural language (group -1, earliest)
    # Analyzes messages without commands and routes to appropriate actions
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        intent_router_handler
    ), group=-1)
    
    # Intent router for DMs
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        intent_router_handler
    ), group=-1)
    
    # Handle task details input (assignee for suggested tasks) - MUST BE FIRST
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        handle_task_details
    ), group=0)

    # Reply to bot = ask question (group -2, runs early to catch all bot replies)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.REPLY & ~filters.COMMAND,
        reply_to_bot_handler
    ), group=-2)
    
    # Handle time input for reminders (group 0)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        reminder_time_input_handler
    ), group=0)
    
    # Callback query handlers
    app.add_handler(CallbackQueryHandler(task_callback_handler, pattern=r"^task:"))
    app.add_handler(CallbackQueryHandler(tasks_filter_callback, pattern=r"^tasks:filter:"))
    app.add_handler(CallbackQueryHandler(reminder_callback_handler, pattern=r"^reminder:"))
    app.add_handler(CallbackQueryHandler(subscribe_callback_handler, pattern=r"^subscribe:"))
    app.add_handler(CallbackQueryHandler(suggest_task_callback, pattern=r"^suggest_task:"))
    app.add_handler(CallbackQueryHandler(mention_callback_handler, pattern=r"^mention:"))
    app.add_handler(CallbackQueryHandler(intent_callback_handler, pattern=r"^intent:"))
    
    # Chat member updates
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, 
        handle_new_chat_members
    ))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.LEFT_CHAT_MEMBER,
        handle_left_chat_member
    ))
    
    # Sarcastic responses to reactions (group 1 to run alongside other handlers)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS,
        sarcasm_handler
    ), group=1)
    
    # Store messages for summarization (group 2)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS,
        handle_message
    ), group=2)
    
    # Task detection from context (group 3, runs after message is stored)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        analyze_for_tasks
    ), group=3)



