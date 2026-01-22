"""Database models for Sanechek bot."""
from datetime import datetime
from enum import Enum
from typing import Optional, List
from sqlalchemy import (
    String, Integer, BigInteger, Boolean, DateTime, 
    ForeignKey, Numeric, Text, Enum as SQLEnum
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.connection import Base


class TaskStatus(str, Enum):
    """Task status enum."""
    OPEN = "open"
    CLOSED = "closed"


class RecurrenceType(str, Enum):
    """Task recurrence type."""
    NONE = "none"
    DAILY = "daily"           # Каждый день
    WEEKDAYS = "weekdays"     # Пн-Пт
    WEEKLY = "weekly"         # Каждую неделю (тот же день)
    MONTHLY = "monthly"       # Каждый месяц (та же дата)


class ReminderStatus(str, Enum):
    """Reminder status enum."""
    PENDING = "pending"
    SENT = "sent"
    CANCELLED = "cancelled"


class User(Base):
    """User model."""
    __tablename__ = "users"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user_id
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_global_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Relationships
    authored_tasks: Mapped[List["Task"]] = relationship(
        "Task", back_populates="author", foreign_keys="Task.author_id"
    )
    assigned_tasks: Mapped[List["Task"]] = relationship(
        "Task", back_populates="assignee", foreign_keys="Task.assignee_id"
    )
    expenses: Mapped[List["Expense"]] = relationship("Expense", back_populates="author")
    subscriptions: Mapped[List["Subscription"]] = relationship("Subscription", back_populates="user")
    chat_memberships: Mapped[List["ChatMember"]] = relationship("ChatMember", back_populates="user")
    authored_reminders: Mapped[List["Reminder"]] = relationship(
        "Reminder", back_populates="author", foreign_keys="Reminder.author_id"
    )
    received_reminders: Mapped[List["Reminder"]] = relationship(
        "Reminder", back_populates="recipient", foreign_keys="Reminder.recipient_id"
    )
    
    @property
    def display_name(self) -> str:
        """Get user display name."""
        if self.username:
            return f"@{self.username}"
        if self.first_name:
            return self.first_name
        return f"User {self.id}"


class Chat(Base):
    """Chat model."""
    __tablename__ = "chats"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram chat_id
    title: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    bot_added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Relationships
    tasks: Mapped[List["Task"]] = relationship("Task", back_populates="chat")
    expenses: Mapped[List["Expense"]] = relationship("Expense", back_populates="chat")
    subscriptions: Mapped[List["Subscription"]] = relationship("Subscription", back_populates="chat")
    members: Mapped[List["ChatMember"]] = relationship("ChatMember", back_populates="chat")
    reminders: Mapped[List["Reminder"]] = relationship("Reminder", back_populates="chat")
    messages: Mapped[List["Message"]] = relationship("Message", back_populates="chat")


class ChatMember(Base):
    """Chat member model - tracks user membership in chats."""
    __tablename__ = "chat_members"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id"))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    left_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="chat_memberships")
    chat: Mapped["Chat"] = relationship("Chat", back_populates="members")


class Task(Base):
    """Task model."""
    __tablename__ = "tasks"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id"))
    author_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    assignee_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    deadline: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[TaskStatus] = mapped_column(
        SQLEnum(TaskStatus), default=TaskStatus.OPEN
    )
    is_delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    
    # Message IDs for tracking (for /done reply)
    command_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    confirmation_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    
    # Recurrence
    recurrence: Mapped[RecurrenceType] = mapped_column(
        SQLEnum(RecurrenceType), default=RecurrenceType.NONE
    )
    parent_task_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=True)
    
    # Relationships
    chat: Mapped["Chat"] = relationship("Chat", back_populates="tasks")
    author: Mapped["User"] = relationship(
        "User", back_populates="authored_tasks", foreign_keys=[author_id]
    )
    assignee: Mapped["User"] = relationship(
        "User", back_populates="assigned_tasks", foreign_keys=[assignee_id]
    )
    
    @property
    def is_overdue(self) -> bool:
        """Check if task is overdue."""
        return self.status == TaskStatus.OPEN and datetime.utcnow() > self.deadline


class Expense(Base):
    """Expense model."""
    __tablename__ = "expenses"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id"))
    author_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Relationships
    chat: Mapped["Chat"] = relationship("Chat", back_populates="expenses")
    author: Mapped["User"] = relationship("User", back_populates="expenses")


class Subscription(Base):
    """Subscription model - tracks summary subscriptions."""
    __tablename__ = "subscriptions"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="subscriptions")
    chat: Mapped["Chat"] = relationship("Chat", back_populates="subscriptions")


class Reminder(Base):
    """Reminder model."""
    __tablename__ = "reminders"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id"))
    author_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    recipient_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    remind_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[ReminderStatus] = mapped_column(
        SQLEnum(ReminderStatus), default=ReminderStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancelled_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    
    # Message ID for cancellation
    confirmation_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    
    # Relationships
    chat: Mapped["Chat"] = relationship("Chat", back_populates="reminders")
    author: Mapped["User"] = relationship(
        "User", back_populates="authored_reminders", foreign_keys=[author_id]
    )
    recipient: Mapped["User"] = relationship(
        "User", back_populates="received_reminders", foreign_keys=[recipient_id]
    )


class Message(Base):
    """Message model - stores chat messages for summarization."""
    __tablename__ = "messages"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger)  # Telegram message_id
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_bot_command: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    
    # Relationships
    chat: Mapped["Chat"] = relationship("Chat", back_populates="messages")

