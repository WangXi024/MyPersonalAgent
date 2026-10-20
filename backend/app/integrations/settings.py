from functools import lru_cache

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
    hindsight_base_url: str = "http://hindsight:8888"
    hindsight_api_key: SecretStr = SecretStr("")
    model_output_tokens: int = Field(6000, ge=256, le=16000)
    dispatcher_poll_seconds: float = 1.0


@lru_cache
def runtime_settings() -> RuntimeSettings:
    return RuntimeSettings()
