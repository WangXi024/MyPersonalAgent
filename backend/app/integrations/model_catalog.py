import json
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.domain.common import DomainError
from app.integrations.settings import runtime_settings


class ChatModel(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    name: str = Field(min_length=1, max_length=200)
    provider: Literal["bailian", "zhipu", "ark", "gateway"]


def configured_models() -> list[dict]:
    settings = runtime_settings()
    if settings.chat_model_catalog_path is None:
        return [{"id": settings.litellm_model_alias, "name": "默认模型", "provider": "gateway"}]
    try:
        models = TypeAdapter(list[ChatModel]).validate_python(
            json.loads(settings.chat_model_catalog_path.read_text())
        )
        if not models or len({model.id for model in models}) != len(models):
            raise ValueError()
    except (OSError, ValueError, ValidationError):
        raise DomainError("model_catalog_unavailable", "模型列表暂不可用，请稍后重试", 503) from None
    return [model.model_dump() for model in models]


def resolve_model(alias: str | None) -> str:
    alias = alias or runtime_settings().litellm_model_alias
    if alias not in {model["id"] for model in configured_models()}:
        raise DomainError("model_unavailable", "所选模型已不可用，请重新选择对话模型")
    return alias
