"""
Web API for Telegram Mini App.
Provides REST API endpoints for the web interface.
"""
import json
import hmac
import hashlib
import base64
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from database import get_session, User, Task, Expense, Reminder, Chat, Message
from database.models import TaskStatus, ReminderStatus
from sqlalchemy import select, func, and_
from utils.date_parser import parse_deadline, parse_reminder_time, DateParseError
from utils.formatters import format_date


app = FastAPI(title="Sanechek Mini App API")

# CORS middleware for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files (relative to project root)
import os
webapp_dir = os.path.join(os.path.dirname(__file__))
app.mount("/static", StaticFiles(directory=webapp_dir), name="static")


# Pydantic models
class TaskCreate(BaseModel):
    text: str
    assignee: Optional[str] = None
    deadline: Optional[str] = None
    recurrence: Optional[str] = "none"


class ExpenseCreate(BaseModel):
    amount: float
    description: str


class ReminderCreate(BaseModel):
    text: str
    time: str


def verify_telegram_webapp_data(init_data: str) -> Optional[Dict]:
    """
    Verify Telegram Web App init data.
    Returns user data if valid, None otherwise.
    """
    try:
        # Parse init data
        parsed_data = dict(parse_qsl(init_data))
        
        # Get hash
        received_hash = parsed_data.pop('hash', '')
        
        # Create data check string
        data_check_string = '\n'.join(
            f"{k}={v}" for k, v in sorted(parsed_data.items())
        )
        
        # Calculate secret key
        secret_key = hmac.new(
            b"WebAppData",
            settings.telegram_bot_token.encode(),
            hashlib.sha256
        ).digest()
        
        # Calculate hash
        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()
        
        # Verify
        if calculated_hash != received_hash:
            return None
        
        # Parse user data
        user_data = json.loads(parsed_data.get('user', '{}'))
        return user_data
        
    except Exception:
        return None


async def get_current_user(request: Request) -> Optional[User]:
    """Get current user from Telegram init data."""
    init_data = request.headers.get('X-Telegram-Init-Data', '')
    if not init_data:
        return None
    
    user_data = verify_telegram_webapp_data(init_data)
    if not user_data:
        return None
    
    user_id = user_data.get('id')
    if not user_id:
        return None
    
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.id == user_id)
        )
        return result.scalar_one_or_none()


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main HTML file."""
    import os
    webapp_dir = os.path.dirname(__file__)
    return FileResponse(os.path.join(webapp_dir, "index.html"))


@app.get("/api/tasks")
async def get_tasks(request: Request):
    """Get user's tasks."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    async with get_session() as session:
        # Get all tasks where user is assignee
        result = await session.execute(
            select(Task)
            .where(Task.assignee_id == user.id)
            .where(Task.status == TaskStatus.OPEN)
            .order_by(Task.deadline)
        )
        tasks = result.scalars().all()
        
        # Format tasks
        task_list = []
        for task in tasks:
            # Get assignee
            assignee_result = await session.execute(
                select(User).where(User.id == task.assignee_id)
            )
            assignee = assignee_result.scalar_one()
            
            task_list.append({
                "id": task.id,
                "text": task.text,
                "assignee": assignee.display_name,
                "deadline": task.deadline.isoformat(),
                "status": task.status.value,
                "recurrence": task.recurrence.value if task.recurrence else "none",
            })
        
        return task_list


@app.post("/api/tasks")
async def create_task_api(task_data: TaskCreate, request: Request):
    """Create a new task."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    async with get_session() as session:
        # Parse assignee
        assignee_id = user.id
        if task_data.assignee:
            # Try to find user by username or name
            assignee_result = await session.execute(
                select(User).where(
                    (User.username == task_data.assignee.replace('@', '')) |
                    (User.first_name.ilike(f"%{task_data.assignee}%"))
                )
            )
            assignee = assignee_result.scalar_one_or_none()
            if assignee:
                assignee_id = assignee.id
        
        # Parse deadline
        deadline = datetime.utcnow() + timedelta(days=1)
        if task_data.deadline:
            try:
                deadline = parse_deadline(task_data.deadline)
            except DateParseError:
                pass
        
        # Create task
        from database.models import RecurrenceType
        recurrence_map = {
            "none": RecurrenceType.NONE,
            "daily": RecurrenceType.DAILY,
            "weekdays": RecurrenceType.WEEKDAYS,
            "weekly": RecurrenceType.WEEKLY,
            "monthly": RecurrenceType.MONTHLY,
        }
        recurrence = recurrence_map.get(task_data.recurrence, RecurrenceType.NONE)
        
        # Get default chat (for now, use first chat user is in)
        chat_result = await session.execute(
            select(Chat).join(User).where(User.id == user.id).limit(1)
        )
        chat = chat_result.scalar_one_or_none()
        
        if not chat:
            raise HTTPException(status_code=400, detail="No chat found")
        
        task = Task(
            chat_id=chat.id,
            author_id=user.id,
            assignee_id=assignee_id,
            text=task_data.text,
            deadline=deadline,
            recurrence=recurrence,
        )
        session.add(task)
        await session.commit()
        
        return {"id": task.id, "message": "Task created"}


@app.post("/api/tasks/{task_id}/close")
async def close_task_api(task_id: int, request: Request):
    """Close a task."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    async with get_session() as session:
        result = await session.execute(
            select(Task).where(Task.id == task_id)
        )
        task = result.scalar_one_or_none()
        
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        
        if task.assignee_id != user.id and task.author_id != user.id:
            raise HTTPException(status_code=403, detail="Not authorized")
        
        task.status = TaskStatus.CLOSED
        task.closed_at = datetime.utcnow()
        task.closed_by = user.id
        
        await session.commit()
        
        return {"message": "Task closed"}


@app.get("/api/expenses")
async def get_expenses(request: Request):
    """Get user's expenses."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    async with get_session() as session:
        # Get expenses from last 30 days
        cutoff = datetime.utcnow() - timedelta(days=30)
        
        result = await session.execute(
            select(Expense)
            .where(Expense.user_id == user.id)
            .where(Expense.created_at >= cutoff)
            .order_by(Expense.created_at.desc())
        )
        expenses = result.scalars().all()
        
        # Calculate stats
        today = datetime.utcnow().date()
        today_start = datetime.combine(today, datetime.min.time())
        
        today_result = await session.execute(
            select(func.sum(Expense.amount))
            .where(Expense.user_id == user.id)
            .where(Expense.created_at >= today_start)
        )
        today_total = today_result.scalar() or 0
        
        month_start = datetime.utcnow().replace(day=1)
        month_result = await session.execute(
            select(func.sum(Expense.amount))
            .where(Expense.user_id == user.id)
            .where(Expense.created_at >= month_start)
        )
        month_total = month_result.scalar() or 0
        
        expense_list = []
        for expense in expenses:
            expense_list.append({
                "id": expense.id,
                "amount": float(expense.amount),
                "description": expense.description,
                "category": expense.category,
                "created_at": expense.created_at.isoformat(),
            })
        
        return {
            "expenses": expense_list,
            "stats": {
                "today": float(today_total),
                "month": float(month_total),
            }
        }


@app.post("/api/expenses")
async def create_expense_api(expense_data: ExpenseCreate, request: Request):
    """Create a new expense."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    async with get_session() as session:
        # Auto-categorize expense
        from utils.categories import categorize_expense
        category = categorize_expense(expense_data.description)
        
        expense = Expense(
            user_id=user.id,
            amount=expense_data.amount,
            description=expense_data.description,
            category=category,
        )
        session.add(expense)
        await session.commit()
        
        return {"id": expense.id, "message": "Expense created"}


@app.get("/api/reminders")
async def get_reminders(request: Request):
    """Get user's reminders."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    async with get_session() as session:
        result = await session.execute(
            select(Reminder)
            .where(Reminder.recipient_id == user.id)
            .where(Reminder.status == ReminderStatus.PENDING)
            .order_by(Reminder.remind_at)
        )
        reminders = result.scalars().all()
        
        reminder_list = []
        for reminder in reminders:
            reminder_list.append({
                "id": reminder.id,
                "text": reminder.text,
                "remind_at": reminder.remind_at.isoformat(),
            })
        
        return reminder_list


@app.post("/api/reminders")
async def create_reminder_api(reminder_data: ReminderCreate, request: Request):
    """Create a new reminder."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    async with get_session() as session:
        # Parse time
        try:
            remind_at = parse_reminder_time(reminder_data.time)
        except DateParseError as e:
            raise HTTPException(status_code=400, detail=str(e))
        
        # Get default chat
        chat_result = await session.execute(
            select(Chat).join(User).where(User.id == user.id).limit(1)
        )
        chat = chat_result.scalar_one_or_none()
        
        if not chat:
            raise HTTPException(status_code=400, detail="No chat found")
        
        reminder = Reminder(
            chat_id=chat.id,
            author_id=user.id,
            recipient_id=user.id,
            text=reminder_data.text,
            remind_at=remind_at,
        )
        session.add(reminder)
        await session.commit()
        
        return {"id": reminder.id, "message": "Reminder created"}


@app.get("/api/summary")
async def get_summary(request: Request):
    """Get chat summary."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    async with get_session() as session:
        # Get first chat user is in
        chat_result = await session.execute(
            select(Chat).join(User).where(User.id == user.id).limit(1)
        )
        chat = chat_result.scalar_one_or_none()
        
        if not chat:
            return {"text": "Нет чатов для саммаризации"}
        
        # Get messages from last 24 hours
        cutoff = datetime.utcnow() - timedelta(hours=24)
        
        result = await session.execute(
            select(Message)
            .where(Message.chat_id == chat.id)
            .where(Message.is_bot_command == False)
            .where(Message.created_at >= cutoff)
            .order_by(Message.created_at)
        )
        messages = result.scalars().all()
        
        if not messages:
            return {"text": "Переписок не было"}
        
        # Format messages
        formatted = []
        for msg in messages:
            user_result = await session.execute(
                select(User).where(User.id == msg.user_id)
            )
            msg_user = user_result.scalar_one_or_none()
            username = msg_user.display_name if msg_user else "Unknown"
            formatted.append(f"{username}: {msg.text}")
        
        # Generate summary
        from llm.summarizer import summarize_messages
        summary_text = await summarize_messages(formatted)
        
        return {"text": summary_text}

