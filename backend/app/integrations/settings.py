from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    temporal_address: str = Field("temporal:7233", validation_alias=AliasChoices("TEMPORAL_ADDRESS", "APP_TEMPORAL_ADDRESS"))
    temporal_namespace: str = Field("learning-agent", validation_alias=AliasChoices("TEMPORAL_NAMESPACE", "APP_TEMPORAL_NAMESPACE"))
    temporal_task_queue: str = Field("learning-agent", validation_alias=AliasChoices("TEMPORAL_TASK_QUEUE", "APP_TEMPORAL_TASK_QUEUE"))
    litellm_base_url: str = "http://litellm:4000/v1"
    litellm_api_key: SecretStr = SecretStr("")
    litellm_model_alias: str = "learning-agent"
    chat_model_catalog_path: Path | None = None
    litellm_admin_key_path: Path | None = None
    hindsight_base_url: str = "http://hindsight:8888"
    hindsight_api_key: SecretStr = SecretStr("")
    model_output_tokens: int = Field(6000, ge=256, le=16000)
    dispatcher_poll_seconds: float = 1.0
    research_enabled: bool = False
    research_provider: Literal["duckduckgo"] = "duckduckgo"
    research_timeout_seconds: float = Field(12, ge=1, le=30)
    research_max_results: int = Field(5, ge=1, le=8)
    research_max_calls: int = Field(6, ge=1, le=10)
    research_allowed_domains: str = ""
    research_fetch_max_bytes: int = Field(262144, ge=16384, le=1048576)
    research_fetch_max_chars: int = Field(12000, ge=1000, le=20000)
    sandbox_enabled: bool = False
    sandbox_address: str = "host.docker.internal:18443"
    sandbox_rpc_timeout_seconds: float = Field(10, ge=1, le=30)
    sandbox_execution_timeout_seconds: int = Field(30, ge=1, le=120)
    sandbox_memory_mb: int = Field(256, ge=128, le=1024)
    sandbox_deadline_seconds: int = Field(240, ge=60, le=900)
    sandbox_max_daily_executions: int = Field(10, ge=1, le=100)
    sandbox_max_concurrent_per_user: int = Field(2, ge=1, le=6)
    coding_enabled: bool = False
    coding_deadline_seconds: int = Field(1800, ge=120, le=1800)
    coding_max_daily_runs: int = Field(5, ge=1, le=50)
    coding_max_concurrent_per_user: int = Field(1, ge=1, le=3)
    verification_enabled: bool = False
    material_enabled: bool = False
    voice_enabled: bool = True
    voice_credential_key_path: Path = Path("/run/secrets/voice_credentials_key")
    voice_max_session_seconds: int = Field(300, ge=30, le=300)
    voice_daily_seconds: int = Field(900, ge=30, le=7200)
    voice_idle_seconds: int = Field(60, ge=10, le=60)
    voice_max_concurrent: int = Field(1, ge=1, le=3)
    voice_lease_seconds: int = Field(20, ge=10, le=60)


@lru_cache
def runtime_settings() -> RuntimeSettings:
    return RuntimeSettings()
