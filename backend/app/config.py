from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_", extra="ignore")
    database_url: str = Field(
        default="postgresql+asyncpg://app@postgres-core/app",
        validation_alias=AliasChoices("APP_DATABASE_URL", "DATABASE_URL"),
    )
    public_url: str = "http://localhost:8080"
    session_days: int = 14
    attachment_dir: Path = Path("/data/attachments")
    max_upload_bytes: int = 10 * 1024 * 1024
    event_poll_seconds: float = 1.0
    default_timezone: str = "Asia/Shanghai"
    temporal_address: str = "temporal:7233"
    temporal_namespace: str = "learning-agent"
    temporal_task_queue: str = "learning-agent"
    proactive_daily_limit: int = 2
    proactive_gap_hours: int = 4
    model_admin_username: str = ""

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()
