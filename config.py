"""Configuration settings for Sanechek bot."""
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List
import os


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Telegram
    telegram_bot_token: str = Field(..., env="TELEGRAM_BOT_TOKEN")
    bot_username: str = Field("sanechek_bot", env="BOT_USERNAME")
    
    # OpenAI for summarization (also supports x.ai, OpenRouter, etc.)
    openai_api_key: str = Field("", env="OPENAI_API_KEY")
    openai_base_url: str = Field("https://api.openai.com/v1", env="OPENAI_BASE_URL")
    openai_model: str = Field("gpt-4o-mini", env="OPENAI_MODEL")
    
    # Initial admins (comma-separated user IDs)
    initial_admins_str: str = Field("", env="INITIAL_ADMINS")
    
    @property
    def initial_admins(self) -> List[int]:
        if not self.initial_admins_str:
            return []
        return [int(x.strip()) for x in self.initial_admins_str.split(",") if x.strip()]
    
    # Timezone
    timezone: str = Field("Asia/Vladivostok", env="TIMEZONE")
    
    # Summary settings
    summary_time: str = Field("12:00", env="SUMMARY_TIME")
    
    # Database
    database_url: str = Field(
        "sqlite+aiosqlite:///./sanechek.db", 
        env="DATABASE_URL"
    )
    
    # Limits
    max_task_length: int = 500
    max_expense_amount: int = 100_000_000
    closed_tasks_retention_days: int = 30
    max_reminder_months: int = 3
    min_reminder_minutes: int = 1
    
    # Reminder timing
    task_reminder_hours_before: int = 4
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()

